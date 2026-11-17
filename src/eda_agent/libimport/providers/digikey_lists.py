# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""DigiKey MyLists: saved parts lists in a DigiKey account.

The one place here that WRITES to a distributor account. It creates a
list, adds parts to it and reads it back. It does not order: nothing in
this module touches a cart, a quote or DigiKey's Ordering API.

Written against DigiKey's published MyLists v1 Swagger file
(developer.digikey.com/node/2654/oas-download, read 2026-11-16) and its
OAuth pages. What those state, and what they leave open:

* MyLists takes a three-legged token only. The user signs in to DigiKey
  in a browser once; the code that comes back is exchanged for an access
  token (30 minutes) and a refresh token (90 days). Every refresh
  REPLACES the refresh token, and the old one stops working, so the new
  one is saved before anything else happens.
* The redirect address must be https and must match the one registered
  for the app to the character. DigiKey suggests ``https://localhost``,
  where nothing listens: the browser shows a failed page whose address
  carries the code, and the user copies that address back.
* A part to add (``RequestedPart``) has no field for a DigiKey number as
  such, no description and no length limit on any field. The number
  goes in ``RequestedPartNumber``; what DigiKey made of it comes back as
  ``DigiKeyPartNumber``. Because no limit is published, nothing here
  shortens a reference: everything is sent whole and compared with what
  DigiKey stored, and a difference is reported.
* What an unknown part number does is not documented. The read-back
  carries ``Flags.IsMatched``.

The tokens are kept in a file in the user's profile, never in the
workspace Altium reads, never in a reply and never in a log. On Windows
the file is sealed with the user's DPAPI key.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from eda_agent.libimport.providers._distributor import (
    DistributorProvider,
    first_string,
)
from eda_agent.libimport.providers._sourcing import to_int, to_number
from eda_agent.libimport.providers.base import (
    ProviderError,
    ProviderUnavailable,
)

__all__ = ["DigiKeyLists", "ListInputError", "join_designators"]

#: A customer reference longer than this is reported before it is sent.
#: DigiKey publishes no limit; 48 is what its forum says fits the bag
#: label, which is the reason the reference is sent at all.
LABEL_REFERENCE_CHARS = 48

#: Parts sent in one request. DigiKey publishes no limit per request and
#: says a list holds up to 1,000 parts.
_ADD_BATCH = 50
_READ_PAGE = 100

#: The fields of one item to add. Anything else is refused by name, so a
#: ``comment`` cannot arrive and be mistaken for something to send.
_ITEM_FIELDS = ("sku", "quantity", "designators", "customer_reference",
                "reference_designator", "description", "manufacturer")

_VISIBILITY = {"private": "Private", "readonly": "ReadOnly",
               "readwrite": "ReadWrite"}

#: Sign-ins started and not finished: state to the time it lapses.
_PENDING_STATES: dict[str, float] = {}


class ListInputError(ProviderError):
    """What was asked cannot be sent as it stands. Nothing was sent."""

    def __init__(self, message: str, problems: Optional[list] = None):
        super().__init__(message)
        self.problems = problems or []


def join_designators(designators: Any) -> str:
    """Designators as one reference: every one of them, comma separated.

    A list keeps its order and loses repeats; a string is taken as
    written. Nothing is abbreviated into ranges and nothing is dropped.
    """
    if designators is None:
        return ""
    if isinstance(designators, str):
        return " ".join(designators.split())
    seen, out = set(), []
    for one in designators:
        text = " ".join(str(one).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return ",".join(out)


# ---- the token file ---------------------------------------------------------

def _token_path() -> Path:
    named = os.environ.get("DIGIKEY_TOKEN_FILE", "").strip()
    if named:
        return Path(named)
    root = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(root) if root else Path.home() / ".config"
    return base / "eda-agent" / "digikey-tokens.json"


def _dpapi(data: bytes, seal: bool) -> bytes:
    """``data`` sealed or opened with the Windows user's own key."""
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(buffer,
                                          ctypes.POINTER(ctypes.c_char)))
    result = _Blob()
    call = crypt32.CryptProtectData if seal else crypt32.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 0,
                ctypes.byref(result)):
        raise OSError("Windows could not "
                      + ("seal" if seal else "open")
                      + " the DigiKey token file for this user")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _read_store() -> dict[str, Any]:
    """Every client's tokens, or nothing when the file is absent or is
    not one this user can open."""
    try:
        wrapper = json.loads(_token_path().read_text("utf-8"))
        raw = base64.b64decode(wrapper["data"])
        if wrapper.get("sealed") == "dpapi":
            raw = _dpapi(raw, seal=False)
        store = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    return store if isinstance(store, dict) else {}


def _write_store(store: dict[str, Any]) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(store).encode("utf-8")
    sealed = "none"
    if os.name == "nt":
        raw, sealed = _dpapi(raw, seal=True), "dpapi"
    text = json.dumps({"format": 1, "sealed": sealed,
                       "data": base64.b64encode(raw).decode("ascii")})
    scratch = path.with_name(path.name + ".new")
    scratch.write_text(text, encoding="utf-8")
    try:
        os.chmod(scratch, 0o600)
    except OSError:
        pass
    os.replace(scratch, path)


# ---- reading DigiKey's replies ----------------------------------------------

def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] \
        if isinstance(value, list) else []


def _list_summary(data: dict[str, Any]) -> dict[str, Any]:
    settings = data.get("ListSettings") \
        if isinstance(data.get("ListSettings"), dict) else {}
    return {
        "list_id": first_string(data, ("Id",), ("ListId",)),
        "name": first_string(data, ("ListName",)),
        "visibility": first_string(settings, ("Visibility",)) or None,
        "package_preference": first_string(
            settings, ("PackagePreference",)) or None,
        "auto_correct_quantities": settings.get("AutoCorrectQuantities"),
        "auto_populate_customer_reference": settings.get("AutoPopulateCref"),
        "total_parts": to_int(data.get("TotalParts")),
        "created": first_string(data, ("DateCreated",)) or None,
        "modified": first_string(data, ("DateModified",)) or None,
        "can_edit": data.get("CanEdit"),
    }


def _part_summary(part: dict[str, Any]) -> dict[str, Any]:
    """One part of a list as DigiKey holds it, references untouched."""
    flags = part.get("Flags") if isinstance(part.get("Flags"), dict) else {}
    quantities = _dicts(part.get("Quantities"))
    index = to_int(part.get("SelectedQuantityIndex")) or 0
    chosen = quantities[index] if 0 <= index < len(quantities) else \
        (quantities[0] if quantities else {})
    maker = part.get("Manufacturer")
    return {
        "unique_id": first_string(part, ("UniqueId",)),
        "requested_part_number": str(part.get("RequestedPartNumber") or ""),
        "sku": str(part.get("DigiKeyPartNumber") or ""),
        "manufacturer_part_number": str(
            part.get("ManufacturerPartNumber") or ""),
        "manufacturer": first_string(maker, ("Name",), ("Value",))
        if isinstance(maker, dict) else str(maker or ""),
        "matched": flags.get("IsMatched"),
        "quantity": to_int(chosen.get("QuantityRequested")),
        "calculated_quantity": to_int(chosen.get("CalculatedQuantity")),
        "pack_type": first_string(chosen, ("SelectedPackType",)) or None,
        "quantities": [to_int(q.get("QuantityRequested"))
                       for q in quantities],
        # str() and nothing else: a reference is compared and reported
        # exactly as DigiKey stored it.
        "customer_reference": str(part.get("CustomerReference") or ""),
        "reference_designator": str(part.get("ReferenceDesignator") or ""),
        "notes": str(part.get("Notes") or ""),
        "digikey_description": str(part.get("Description") or ""),
        "status": first_string(part, ("PartStatus",)) or None,
        "quantity_available": to_int(part.get("QuantityAvailable")),
    }


class DigiKeyLists(DistributorProvider):
    """A DigiKey account's saved lists, through MyLists v1."""

    name = "digikey"
    description = (
        "DigiKey MyLists: create a list, add parts with quantities and "
        "references, read it back. Needs DIGIKEY_CLIENT_ID and "
        "DIGIKEY_CLIENT_SECRET for an app subscribed to MyLists, and a "
        "one-time sign-in.")
    env_vars = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET")

    _AUTHORIZE_URL = "https://api.digikey.com/v1/oauth2/authorize"
    _TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    _BASE_URL = "https://api.digikey.com/mylists/v1"
    _DEFAULT_REDIRECT = "https://localhost"

    # ---- signing in ----------------------------------------------------

    def redirect_uri(self) -> str:
        return os.environ.get("DIGIKEY_REDIRECT_URI", "").strip() \
            or self._DEFAULT_REDIRECT

    def authorize_url(self) -> dict[str, Any]:
        """The address to sign in at, and what to do with the result."""
        creds = self._credentials()
        now = time.time()
        for state in [s for s, due in _PENDING_STATES.items() if due < now]:
            del _PENDING_STATES[state]
        state = secrets.token_urlsafe(24)
        _PENDING_STATES[state] = now + 900
        return {
            "authorize_url": self._query(self._AUTHORIZE_URL, {
                "response_type": "code",
                "client_id": creds["DIGIKEY_CLIENT_ID"],
                "redirect_uri": self.redirect_uri(),
                "state": state}),
            "redirect_uri": self.redirect_uri(),
        }

    def finish_login(self, returned: str) -> dict[str, Any]:
        """Exchange what the sign-in returned for tokens, and keep them.

        ``returned`` is the whole address the browser ended on, or the
        code alone. An address must carry the state this process issued.
        """
        text = str(returned or "").strip()
        code = text
        if "code=" in text or "error=" in text or "://" in text:
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(text).query or text.partition("?")[2])
            if query.get("error"):
                raise ProviderUnavailable(
                    "DigiKey did not grant access: "
                    + " ".join(query["error"]
                               + query.get("error_description", []))[:200])
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not state or _PENDING_STATES.pop(state, 0) < time.time():
                raise ProviderUnavailable(
                    "that address does not belong to a sign-in this "
                    "session started, or the sign-in is over 15 minutes "
                    "old. Start again for a new address.")
        if not code:
            raise ProviderUnavailable(
                "no code in what was passed. Pass the whole address the "
                "browser ended on; it contains code=...")
        creds = self._credentials()
        self._exchange({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": self.redirect_uri()}, creds)
        return self.status()

    def _exchange(self, grant: dict[str, str],
                  creds: dict[str, str]) -> dict[str, Any]:
        """Ask the token endpoint, save what it gives, return the record."""
        body = urllib.parse.urlencode({
            "client_id": creds["DIGIKEY_CLIENT_ID"],
            "client_secret": creds["DIGIKEY_CLIENT_SECRET"],
            **grant}).encode("ascii")
        try:
            payload = self._request(
                self._TOKEN_URL, method="POST", body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        except ProviderError as exc:
            status = getattr(exc, "status", None)
            if status not in (400, 401):
                raise
            if grant["grant_type"] == "refresh_token":
                raise ProviderUnavailable(
                    f"DigiKey no longer accepts the saved sign-in (HTTP "
                    f"{status}). Sign in again with part_list_login.") \
                    from None
            raise ProviderUnavailable(
                f"DigiKey refused the sign-in code (HTTP {status}). A code "
                "lasts one minute and works once, and the redirect address "
                f"must be the one registered for the app, exactly: "
                f"{self.redirect_uri()}. Start again for a new address.") \
                from None
        access = first_string(payload, ("access_token",))
        refresh = first_string(payload, ("refresh_token",))
        if not access or not refresh:
            raise ProviderUnavailable(
                "DigiKey answered the token request without an access and "
                "a refresh token")
        now = time.time()
        record = {
            "access_token": access,
            "access_expires_at": now + (to_number(
                payload.get("expires_in")) or 0),
            "refresh_token": refresh,
            # DigiKey's FAQ: 90 days unless exchanged first.
            "refresh_expires_at": now + (to_number(
                payload.get("refresh_token_expires_in")) or 90 * 86400),
        }
        # Saved before use: the previous refresh token is already dead.
        store = _read_store()
        store[creds["DIGIKEY_CLIENT_ID"]] = record
        _write_store(store)
        return record

    def status(self) -> dict[str, Any]:
        """Whether a sign-in is on file. Never the tokens themselves."""
        creds = self._credentials()
        record = _read_store().get(creds["DIGIKEY_CLIENT_ID"])
        if not isinstance(record, dict) or not record.get("refresh_token"):
            return {"signed_in": False}
        left = float(record.get("refresh_expires_at") or 0) - time.time()
        return {"signed_in": left > 0,
                "sign_in_lasts_days": max(0, int(left // 86400)),
                "note": "each use renews the sign-in for 90 days"}

    def _token(self, renew: bool = False) -> str:
        creds = self._credentials()
        # Read each time: another process may have refreshed, and its
        # refresh made the token this one remembers worthless.
        record = _read_store().get(creds["DIGIKEY_CLIENT_ID"])
        if not isinstance(record, dict) or not record.get("refresh_token"):
            raise ProviderUnavailable(
                "no DigiKey sign-in on file. MyLists needs the account "
                "owner to sign in once: call part_list_login.")
        if not renew and record.get("access_token") and \
                float(record.get("access_expires_at") or 0) - 60 > time.time():
            return str(record["access_token"])
        record = self._exchange({"grant_type": "refresh_token",
                                 "refresh_token": record["refresh_token"]},
                                creds)
        return str(record["access_token"])

    # ---- one call --------------------------------------------------------

    def _call(self, method: str, path: str,
              query: Optional[dict[str, Any]] = None,
              body: Any = None, account: bool = False) -> Any:
        """One MyLists call. A token DigiKey refuses is renewed once."""
        creds = self._credentials()
        url = self._BASE_URL + path
        if query:
            url = self._query(url, {k: str(v) for k, v in query.items()})
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self._token(renew=attempt == 2)}",
                "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            account_id = os.environ.get("DIGIKEY_ACCOUNT_ID", "").strip()
            if account and account_id:
                headers["X-DIGIKEY-Account-Id"] = account_id
            try:
                return self._request(
                    url, method=method, headers=headers,
                    body=(json.dumps(body).encode("utf-8")
                          if body is not None else None))
            except ProviderError as exc:
                status = getattr(exc, "status", None)
                if status == 401 and attempt == 1:
                    continue
                if status in (401, 403):
                    detail = getattr(exc, "detail", "")
                    raise ProviderUnavailable(
                        f"DigiKey refused the MyLists call (HTTP {status}"
                        + (f": {detail}" if detail else "")
                        + ") with a fresh token. The usual causes: the app "
                        "behind DIGIKEY_CLIENT_ID is not subscribed to the "
                        "MyLists API in DigiKey's developer portal, or the "
                        "signed-in user may not use this list or account "
                        "(DIGIKEY_ACCOUNT_ID).") from None
                raise
        raise AssertionError("unreachable")

    # ---- lists -------------------------------------------------------------

    def lists(self, limit: int = 50) -> list[dict[str, Any]]:
        reply = self._call("GET", "/lists",
                           {"startIndex": 0, "limit": int(limit)},
                           account=True)
        return [_list_summary(entry) for entry in _dicts(reply)]

    def create_list(self, name: str,
                    visibility: str = "private") -> dict[str, Any]:
        """Create an empty list and report it as DigiKey stored it.

        The settings ask DigiKey to keep what is entered: the part number
        as entered, the quantity as entered, the reference as entered.
        """
        wanted = " ".join(str(name or "").split())
        if not wanted:
            raise ListInputError("a list needs a name")
        level = _VISIBILITY.get(str(visibility or "").strip().lower())
        if level is None:
            raise ListInputError(
                f"no visibility {visibility!r}; it is private, readonly or "
                "readwrite")
        reply = self._call("POST", "/lists", account=True, body={
            "ListName": wanted,
            "Source": "external",
            "ListSettings": {
                "Visibility": level,
                "PackagePreference": "EnteredPartNumber",
                "AutoCorrectQuantities": False,
                "AttritionEnabled": False,
                "AutoPopulateCref": False,
            },
        })
        list_id = reply.strip() if isinstance(reply, str) else \
            first_string(reply, ("Id",), ("ListId",))
        if not list_id:
            raise ProviderError(
                "DigiKey answered the list creation without a list id; "
                "whether a list was created is not known. Look with "
                "part_list_get before creating another.")
        created = _list_summary(
            self._call("GET", f"/lists/{_seg(list_id)}", account=True) or {})
        created["list_id"] = created["list_id"] or list_id
        notes = []
        if created["name"] != wanted:
            notes.append(f"DigiKey stored the name as {created['name']!r}, "
                         f"not {wanted!r}")
        if created["visibility"] != level:
            notes.append(f"asked for {level}, DigiKey reports "
                         f"{created['visibility']}")
        if notes:
            created["differences"] = notes
        return created

    def parts(self, list_id: str) -> list[dict[str, Any]]:
        """Every part of a list, read page by page."""
        found: list[dict[str, Any]] = []
        while True:
            reply = self._call("GET", f"/lists/{_seg(list_id)}/parts", {
                "startIndex": len(found), "limit": _READ_PAGE})
            page = _dicts(reply.get("PartsList")) \
                if isinstance(reply, dict) else []
            found.extend(page)
            total = to_int(reply.get("TotalParts")) \
                if isinstance(reply, dict) else None
            if not page or len(page) < _READ_PAGE \
                    or (total is not None and len(found) >= total):
                break
        return [_part_summary(part) for part in found]

    def get_list(self, list_id: str) -> dict[str, Any]:
        if not str(list_id or "").strip():
            raise ListInputError("which list: pass its list_id")
        info = _list_summary(
            self._call("GET", f"/lists/{_seg(list_id)}", account=True) or {})
        info["list_id"] = info["list_id"] or str(list_id).strip()
        items = self.parts(list_id)
        info["items"] = items
        info["count"] = len(items)
        return info

    # ---- adding parts --------------------------------------------------------

    def prepare_items(self, items: Any) -> list[dict[str, Any]]:
        """``items`` checked and made ready to send, or refused whole.

        Every problem of every item is collected, so one call says all
        that is wrong. Nothing is sent from here.
        """
        if not isinstance(items, list) or not items:
            raise ListInputError("items is a non-empty list of parts")
        ready, problems = [], []
        for position, item in enumerate(items):
            wrong = []
            if not isinstance(item, dict):
                problems.append({"item": position,
                                 "problems": ["not an object"]})
                continue
            unknown = sorted(set(item) - set(_ITEM_FIELDS))
            if unknown:
                wrong.append(
                    "unknown field(s) " + ", ".join(map(repr, unknown))
                    + "; an item has " + ", ".join(_ITEM_FIELDS))
            sku = str(item.get("sku") or "").strip()
            if not sku:
                wrong.append("no sku (the DigiKey part number)")
            quantity = item.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) \
                    or quantity < 1:
                wrong.append(f"quantity {quantity!r} is not a whole number "
                             "of 1 or more")
            designators = join_designators(item.get("designators"))
            customer = item.get("customer_reference")
            customer = designators if customer is None \
                else " ".join(str(customer).split())
            reference = item.get("reference_designator")
            reference = designators if reference is None \
                else " ".join(str(reference).split())
            if wrong:
                problems.append({"item": position, "sku": sku,
                                 "problems": wrong})
                continue
            ready.append({
                "item": position, "sku": sku, "quantity": quantity,
                "customer_reference": customer,
                "reference_designator": reference,
                "description": " ".join(
                    str(item.get("description") or "").split()),
                "manufacturer": " ".join(
                    str(item.get("manufacturer") or "").split()),
            })
        if problems:
            raise ListInputError(
                f"{len(problems)} of {len(items)} items cannot be sent as "
                "they stand; nothing was added", problems)
        return ready

    def unknown_skus(self, ready: list[dict[str, Any]]) -> list[dict]:
        """The items whose sku is not an orderable DigiKey number.

        Asked of Product Information, one live read per distinct sku. A
        manufacturer number is not accepted in its place: it can name
        several packagings and several makers.
        """
        from eda_agent.libimport.providers.distributors import (
            DigiKeyProvider,
        )

        catalogue = DigiKeyProvider()
        verdict: dict[str, str] = {}
        for entry in ready:
            key = entry["sku"].casefold()
            if key in verdict:
                continue
            try:
                hit = catalogue._details(entry["sku"])
            except ProviderUnavailable:
                raise
            except ProviderError as exc:
                if getattr(exc, "status", None) not in (400, 404):
                    raise
                hit = None
            numbers = [str(o.get("distributor_pn") or "") for o in
                       ((hit.sourcing or {}).get("offers") or [])] \
                if hit else []
            if hit is None:
                verdict[key] = "DigiKey has no product with this number"
            elif key not in {n.casefold() for n in numbers}:
                verdict[key] = (
                    "not a DigiKey part number of this product; its "
                    "numbers are " + ", ".join(n for n in numbers if n))
            else:
                verdict[key] = ""
        return [{"item": e["item"], "sku": e["sku"],
                 "problems": [verdict[e["sku"].casefold()]]}
                for e in ready if verdict[e["sku"].casefold()]]

    def add_items(self, list_id: str, items: Any,
                  check_products: bool = True) -> dict[str, Any]:
        """Add parts to a list, then read the list and compare.

        Refused whole, with nothing sent, when an item is malformed or
        (``check_products``) its sku is not a DigiKey part number. After
        sending, each item is found again in the list and its sku,
        quantity and both references are compared with what was sent.
        """
        if not str(list_id or "").strip():
            raise ListInputError("which list: pass its list_id")
        ready = self.prepare_items(items)
        if check_products:
            unknown = self.unknown_skus(ready)
            if unknown:
                raise ListInputError(
                    f"{len(unknown)} of {len(ready)} items name no DigiKey "
                    "part number; nothing was added", unknown)

        before = {p["unique_id"] for p in self.parts(list_id)}
        returned: list[Any] = []
        sent = 0
        failure = None
        for start in range(0, len(ready), _ADD_BATCH):
            batch = ready[start:start + _ADD_BATCH]
            body = []
            for entry in batch:
                part: dict[str, Any] = {
                    "RequestedPartNumber": entry["sku"],
                    "CustomerReference": entry["customer_reference"],
                    "ReferenceDesignator": entry["reference_designator"],
                    "Quantities": [{"Quantity": entry["quantity"]}],
                    "SelectedQuantityIndex": 0,
                }
                if entry["manufacturer"]:
                    part["ManufacturerName"] = entry["manufacturer"]
                if entry["description"]:
                    part["Notes"] = entry["description"]
                body.append(part)
            try:
                reply = self._call(
                    "POST", f"/lists/{_seg(list_id)}/parts", body=body)
            except ProviderError as exc:
                failure = str(exc)
                break
            sent += len(batch)
            returned.extend(reply if isinstance(reply, list) else [])

        now = self.parts(list_id)
        fresh = [p for p in now if p["unique_id"] not in before]
        report = _compare(ready[:sent], fresh, returned)
        out: dict[str, Any] = {
            "list_id": str(list_id).strip(),
            "sent": sent,
            "added": len(fresh),
            "verified": sum(1 for r in report if not r["differences"]),
            "items": report,
            "list_total": len(now),
        }
        if failure:
            out["failed"] = (
                f"DigiKey refused a batch after {sent} of {len(ready)} "
                f"items were sent: {failure}")
            out["not_sent"] = [{"item": e["item"], "sku": e["sku"]}
                               for e in ready[sent:]]
        return out


def _seg(value: Any) -> str:
    return urllib.parse.quote(str(value).strip(), safe="")


def _compare(sent: list[dict[str, Any]], fresh: list[dict[str, Any]],
             returned: list[Any]) -> list[dict[str, Any]]:
    """Each sent item beside the list part it became, with differences.

    Paired by the ids DigiKey returned when there is one per item, else
    by part number and references, and reported unpaired rather than
    paired by position alone.
    """
    by_id = {p["unique_id"]: p for p in fresh if p["unique_id"]}
    free = list(fresh)
    ids = [str(r) for r in returned] \
        if len(returned) == len(sent) \
        and all(isinstance(r, str) and r in by_id for r in returned) else None
    report = []
    for position, entry in enumerate(sent):
        part = None
        if ids:
            part = by_id[ids[position]]
        else:
            sku = entry["sku"].casefold()
            same = [p for p in free
                    if sku in (p["requested_part_number"].casefold(),
                               p["sku"].casefold())]
            exact = [p for p in same if p["reference_designator"]
                     == entry["reference_designator"]
                     and p["customer_reference"]
                     == entry["customer_reference"]]
            part = (exact or same or [None])[0]
        if part in free:
            free.remove(part)
        row: dict[str, Any] = {
            "item": entry["item"], "sku": entry["sku"],
            "quantity": entry["quantity"],
            "customer_reference": entry["customer_reference"],
            "reference_designator": entry["reference_designator"],
            "description": entry["description"],
        }
        differences = []
        if part is None:
            differences.append("not found in the list after adding")
        else:
            row["unique_id"] = part["unique_id"]
            row["digikey_description"] = part["digikey_description"]
            if part["matched"] is False:
                differences.append("DigiKey did not match this number to a "
                                   "product")
            if part["sku"].casefold() != entry["sku"].casefold():
                differences.append(
                    f"sku: sent {entry['sku']!r}, list holds "
                    f"{part['sku']!r}")
            if part["quantity"] != entry["quantity"]:
                differences.append(
                    f"quantity: sent {entry['quantity']}, list holds "
                    f"{part['quantity']}")
            for field, label in (
                    ("customer_reference", "customer reference"),
                    ("reference_designator", "reference designator")):
                if part[field] != entry[field]:
                    differences.append(
                        f"{label}: sent {entry[field]!r} "
                        f"({len(entry[field])} characters), list holds "
                        f"{part[field]!r} ({len(part[field])})")
            if entry["description"] and part["notes"] != entry["description"]:
                differences.append(
                    f"description (kept in the part's notes): sent "
                    f"{entry['description']!r}, list holds {part['notes']!r}")
        row["differences"] = differences
        if len(entry["customer_reference"]) > LABEL_REFERENCE_CHARS:
            row["label_warning"] = (
                f"the customer reference is "
                f"{len(entry['customer_reference'])} characters. DigiKey "
                f"publishes no limit; its forum says about "
                f"{LABEL_REFERENCE_CHARS} fit a bag label. It was sent "
                "whole.")
        report.append(row)
    return report


def _main(argv: Optional[list[str]] = None) -> int:
    """Sign in from a terminal: ``python -m ...digikey_lists login``.

    For when a code's one minute is too short to pass through a chat.
    Prints the address, reads the address the browser ended on, and
    prints only whether it worked.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    lists = DigiKeyLists()
    try:
        if args[:1] == ["status"]:
            print(json.dumps(lists.status()))
            return 0
        if args[:1] != ["login"]:
            print("usage: python -m eda_agent.libimport.providers."
                  "digikey_lists login | status")
            return 2
        start = lists.authorize_url()
        print("Open this address, sign in to DigiKey and allow access:\n")
        print(start["authorize_url"])
        print(f"\nThe browser then goes to {start['redirect_uri']} and may "
              "show a page that cannot load. Copy that page's whole address "
              "and paste it here within one minute.")
        print(json.dumps(lists.finish_login(input("\nAddress: "))))
        return 0
    except ProviderError as exc:
        print(f"failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
