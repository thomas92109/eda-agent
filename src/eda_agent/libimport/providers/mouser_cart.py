# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Mouser carts: put parts in a cart and read the cart back.

It creates a cart, adds parts to a cart whose key is given, and reads a
cart. It does not order: nothing in this module touches Mouser's Order
or Order History API, a payment, an address or a checkout. A cart is
bought, if ever, by a person on Mouser's site.

Written against Mouser's published API description
(api.mouser.com/api/docs/V1, read 2026-11-16). What it states:

* A cart is named by a ``CartKey``. ``POST /api/v1/cart/items/insert``
  adds parts; the reply is the whole cart, with its key.
  ``GET /api/v1/cart`` reads one. The key of the API goes in the query
  as ``apiKey``, and it is a different key from the Search API's:
  ``MOUSER_CART_API_KEY``.
* A part to add (``CartItemRequest``) has four fields and no others:
  ``MouserPartNumber`` (at most 80 characters), ``Quantity``,
  ``CustomerPartNumber`` (at most 21 characters, no ``*``) and
  ``PackagingChoice`` (``None``, ``Cut_Tape``, ``MouseReel`` or
  ``FullReel``). There is NO field for reference designators, a
  description or a note. Those stay in this project's result and are
  never sent, under any other name either.
* A cart line (``OrderLine``) answers with ``MouserPartNumber``,
  ``MfrPartNumber``, ``Manufacturer``, ``Quantity``,
  ``CartItemCustPartNumber``, ``PackagingChoice``, prices, the sales
  minimum, multiple and maximum, and its own ``Errors`` and
  ``InfoMessages``.
* Errors arrive in an ``Errors`` array, at the top of the reply or on a
  line, and can arrive under HTTP 200.

Run live on 2026-11-16, one cart with one part: an insert with an empty
``CartKey`` made a cart and answered with its key; the part number,
quantity and a ``CustomerPartNumber`` of 18 characters came back as
sent (the last as ``CartItemCustPartNumber``); with no
``PackagingChoice`` sent, Mouser chose ``Cut_Tape``; a tariff arrived
as an entry of ``additionalFees``; ``InfoMessages`` and
``SalesMaximumOrderQty`` were null; and reading the cart by its key
gave the same reply as the insert.

What neither the description nor that run settles, and so is checked on
every call rather than assumed: what Mouser does with a quantity below
the minimum or off the multiple, and what adding a number already in
the cart does to that line. Each added part is found again in the cart
and compared.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from eda_agent.libimport.providers._distributor import (
    DistributorProvider,
    _says_rate_limited,
    first_string,
)
from eda_agent.libimport.providers._sourcing import to_int, to_number
from eda_agent.libimport.providers.base import (
    ProviderError,
    ProviderUnavailable,
)

__all__ = ["CartInputError", "MouserCart"]

#: Limits Mouser's API description states for a part to add.
PART_NUMBER_CHARS = 80
CUSTOMER_PART_NUMBER_CHARS = 21
PACKAGING_CHOICES = ("None", "Cut_Tape", "MouseReel", "FullReel")

#: What Mouser's cart has a field for.
_SENT_FIELDS = ("sku", "quantity", "customer_part_number", "packaging")
#: What it has no field for. Accepted, returned with the item, never sent.
_KEPT_FIELDS = ("designators", "description", "manufacturer",
                "manufacturer_part_number")
_ITEM_FIELDS = _SENT_FIELDS + _KEPT_FIELDS

_CART_KEY = re.compile(
    r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

#: Parts sent in one request. Mouser publishes no limit.
_ADD_BATCH = 50

UNSUPPORTED_NOTE = (
    "Mouser's cart has no field for reference designators, a description "
    "or a note. They are returned here with each item and were not sent.")


class CartInputError(ProviderError):
    """What was asked cannot be sent as it stands. Nothing was sent."""

    def __init__(self, message: str, problems: Optional[list] = None):
        super().__init__(message)
        self.problems = problems or []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] \
        if isinstance(value, list) else []


def _text(value: Any) -> str:
    """Designators or a text as one line, every one of them."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(" ".join(str(v).split()) for v in value
                        if str(v).strip())
    return " ".join(str(value).split())


def _errors(source: Any) -> list[str]:
    """Mouser's error entries as text: what, and about which field."""
    out = []
    for entry in _dicts(source.get("Errors")
                        if isinstance(source, dict) else None):
        message = first_string(entry, ("Message",)) \
            or first_string(entry, ("ResourceKey",))
        code = first_string(entry, ("Code",))
        field = first_string(entry, ("PropertyName",))
        text = ": ".join(filter(None, (field, message))) or code
        if code and code not in text:
            text = f"{text} ({code})"
        if text:
            out.append(text)
    return out


def _line_summary(line: dict[str, Any]) -> dict[str, Any]:
    """One cart line as Mouser holds it."""
    fees = [{"code": first_string(f, ("code",)) or None,
             "amount": to_number(f.get("amount")),
             "extended_amount": to_number(f.get("extendedAmount"))}
            for f in _dicts(line.get("additionalFees"))]
    return {
        "sku": str(line.get("MouserPartNumber") or ""),
        "manufacturer": str(line.get("Manufacturer") or ""),
        "manufacturer_part_number": str(line.get("MfrPartNumber") or ""),
        "mouser_description": str(line.get("Description") or ""),
        "quantity": to_int(line.get("Quantity")),
        # str() and nothing else: compared exactly as Mouser stored it.
        "customer_part_number": str(
            line.get("CartItemCustPartNumber") or ""),
        "packaging": first_string(line, ("PackagingChoice",)) or None,
        "unit_price": to_number(line.get("UnitPrice")),
        "extended_price": to_number(line.get("ExtendedPrice")),
        "stock": to_int(line.get("MouserATS")),
        "moq": to_int(line.get("SalesMinimumOrderQty")),
        "multiple": to_int(line.get("SalesMultipleQty")),
        "maximum": to_int(line.get("SalesMaximumOrderQty")),
        "parts_per_reel": to_int(line.get("PartsPerReel")),
        "lifecycle": first_string(line, ("LifeCycle",)) or None,
        "additional_fees": fees,
        "messages": [str(m) for m in line.get("InfoMessages") or []
                     if str(m).strip()]
        if isinstance(line.get("InfoMessages"), list) else [],
        "errors": _errors(line),
    }


def _cart_summary(reply: Any) -> dict[str, Any]:
    data = reply if isinstance(reply, dict) else {}
    items = [_line_summary(line) for line in _dicts(data.get("CartItems"))]
    return {
        "cart_key": first_string(data, ("CartKey",)),
        "currency": first_string(data, ("CurrencyCode",)) or None,
        "merchandise_total": to_number(data.get("MerchandiseTotal")),
        "additional_fees_total": to_number(data.get("additionalFeesTotal")),
        "total_item_count": to_int(data.get("TotalItemCount")),
        "items": items,
        "count": len(items),
        "errors": _errors(data),
    }


class MouserCart(DistributorProvider):
    """A Mouser cart, through the Cart API. Never an order."""

    name = "mouser"
    description = (
        "Mouser Cart API: create a cart, add parts with quantities and a "
        "customer part number, read it back. Needs MOUSER_CART_API_KEY, "
        "the key of Mouser's Cart and Order API (not the search key).")
    env_vars = ("MOUSER_CART_API_KEY",)

    _CART_URL = "https://api.mouser.com/api/v1/cart"
    _INSERT_URL = "https://api.mouser.com/api/v1/cart/items/insert"

    # ---- one call ------------------------------------------------------

    def _reject_error_payload(self, payload: Any) -> None:
        """Left to ``_call``: a cart's ``Errors`` are mostly about a
        part or a quantity, not about the key, and each has to be read."""

    def _call(self, url: str, query: dict[str, str],
              body: Any = None) -> dict[str, Any]:
        creds = self._credentials()
        try:
            reply = self._request(
                self._query(url, {"apiKey": creds["MOUSER_CART_API_KEY"],
                                  **query}),
                method="GET" if body is None else "POST",
                body=None if body is None
                else json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"})
        except ProviderUnavailable:
            raise
        except ProviderError as exc:
            detail = getattr(exc, "detail", "")
            if _says_key_problem(detail):
                raise ProviderUnavailable(
                    "mouser rejected the key in MOUSER_CART_API_KEY: "
                    f"{detail}. It is the key of the Cart and Order API, "
                    "which is not the Search API's key.") from None
            raise
        if not isinstance(reply, dict):
            raise ProviderError(
                "mouser answered the cart call with something other than "
                "a cart")
        problems = _errors(reply)
        text = "; ".join(problems)
        if problems and _says_rate_limited(text):
            raise ProviderUnavailable(
                f"mouser rate-limited this client: {text[:300]}")
        if problems and _says_key_problem(text):
            raise ProviderUnavailable(
                f"mouser rejected the key in MOUSER_CART_API_KEY: "
                f"{text[:300]}. It is the key of the Cart and Order API, "
                "which is not the Search API's key.")
        return reply

    # ---- reading -----------------------------------------------------------

    def get_cart(self, cart_key: str) -> dict[str, Any]:
        """The cart as Mouser holds it. An unknown key is an error."""
        key = _cart_key(cart_key)
        cart = _cart_summary(self._call(self._CART_URL, {"cartKey": key}))
        if cart["errors"] and not cart["items"]:
            raise ProviderError(
                "mouser could not read that cart: "
                + "; ".join(cart["errors"])[:400])
        cart["cart_key"] = cart["cart_key"] or key
        return cart

    # ---- adding --------------------------------------------------------------

    def prepare_items(self, items: Any) -> list[dict[str, Any]]:
        """``items`` checked and made ready to send, or refused whole.

        Every problem of every item is collected, so one call says all
        that is wrong. Nothing is sent from here.
        """
        if not isinstance(items, list) or not items:
            raise CartInputError("items is a non-empty list of parts")
        ready, problems = [], []
        seen: dict[str, int] = {}
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append({"item": position,
                                 "problems": ["not an object"]})
                continue
            wrong = []
            unknown = sorted(set(item) - set(_ITEM_FIELDS))
            if unknown:
                wrong.append(
                    "unknown field(s) " + ", ".join(map(repr, unknown))
                    + "; an item has " + ", ".join(_ITEM_FIELDS))
            sku = str(item.get("sku") or "").strip()
            if not sku:
                wrong.append("no sku (the Mouser part number)")
            elif len(sku) > PART_NUMBER_CHARS:
                wrong.append(f"sku is {len(sku)} characters; Mouser takes "
                             f"{PART_NUMBER_CHARS}")
            elif sku.casefold() in seen:
                wrong.append(
                    f"the same sku as item {seen[sku.casefold()]}; one "
                    "cart line per part number, so give one item with "
                    "the whole quantity")
            else:
                seen[sku.casefold()] = position
            quantity = item.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) \
                    or quantity < 1:
                wrong.append(f"quantity {quantity!r} is not a whole number "
                             "of 1 or more")
            customer = item.get("customer_part_number")
            customer = "" if customer is None else str(customer).strip()
            if len(customer) > CUSTOMER_PART_NUMBER_CHARS:
                wrong.append(
                    f"customer_part_number is {len(customer)} characters; "
                    f"Mouser takes {CUSTOMER_PART_NUMBER_CHARS}. It is not "
                    "shortened here")
            if "*" in customer:
                wrong.append("customer_part_number contains '*', which "
                             "Mouser does not take")
            packaging = item.get("packaging")
            if packaging is not None:
                match = {p.casefold(): p for p in PACKAGING_CHOICES}.get(
                    str(packaging).strip().casefold())
                if match is None:
                    wrong.append(
                        f"no packaging {packaging!r}; it is one of "
                        + ", ".join(PACKAGING_CHOICES))
                packaging = match
            if wrong:
                problems.append({"item": position, "sku": sku,
                                 "problems": wrong})
                continue
            ready.append({
                "item": position, "sku": sku, "quantity": quantity,
                "customer_part_number": customer, "packaging": packaging,
                "designators": _text(item.get("designators")),
                "description": _text(item.get("description")),
                "manufacturer": _text(item.get("manufacturer")),
                "manufacturer_part_number": _text(
                    item.get("manufacturer_part_number")),
            })
        if problems:
            raise CartInputError(
                f"{len(problems)} of {len(items)} items cannot be sent as "
                "they stand; nothing was added", problems)
        return ready

    def unorderable(self, ready: list[dict[str, Any]]) -> list[dict]:
        """The items Mouser's catalogue says cannot be ordered as asked.

        Asked of the Search API (``MOUSER_API_KEY``), one read per sku:
        the sku must be a Mouser part number, exactly, and the quantity
        must meet its minimum and its multiple. A quantity is never
        rounded here.
        """
        from eda_agent.libimport.providers.distributors import MouserProvider

        from eda_agent.libimport.providers import _cache

        catalogue = MouserProvider()
        out = []
        for entry in ready:
            # Asked of Mouser now, never answered from the cache: this
            # decides what goes into the cart.
            with _cache.fresh():
                hits = catalogue._post(catalogue._PART_URL, {
                    "SearchByPartRequest": {
                        "mouserPartNumber": entry["sku"],
                        "partSearchOptions": "Exact"}})
            offers = [offer for hit in hits
                      for offer in (hit.sourcing or {}).get("offers") or []]
            mine = [o for o in offers
                    if str(o.get("distributor_pn") or "").casefold()
                    == entry["sku"].casefold()]
            wrong = []
            if not mine:
                numbers = sorted({str(o.get("distributor_pn"))
                                  for o in offers if o.get("distributor_pn")})
                wrong.append(
                    "not a Mouser part number"
                    + ("; Mouser's numbers for it are " + ", ".join(numbers)
                       if numbers else "; Mouser has no part with it"))
            else:
                moq, multiple = mine[0].get("moq"), mine[0].get("multiple")
                if moq and entry["quantity"] < moq:
                    wrong.append(f"quantity {entry['quantity']} is below "
                                 f"Mouser's minimum of {moq}")
                if multiple and entry["quantity"] % multiple:
                    wrong.append(f"quantity {entry['quantity']} is not a "
                                 f"multiple of {multiple}")
            if wrong:
                out.append({"item": entry["item"], "sku": entry["sku"],
                            "problems": wrong})
        return out

    def create_cart(self, items: Any,
                    check_products: bool = True) -> dict[str, Any]:
        """A NEW cart holding ``items``, read back and compared."""
        return self._insert("", items, check_products)

    def add_items(self, cart_key: str, items: Any,
                  check_products: bool = True) -> dict[str, Any]:
        """Add ``items`` to the cart ``cart_key`` names, and compare.

        The key is required and must be a cart that exists: a call that
        cannot name its cart never makes a new one.
        """
        return self._insert(_cart_key(cart_key), items, check_products)

    def _insert(self, cart_key: str, items: Any,
                check_products: bool) -> dict[str, Any]:
        ready = self.prepare_items(items)
        if check_products:
            refused = self.unorderable(ready)
            if refused:
                raise CartInputError(
                    f"{len(refused)} of {len(ready)} items cannot be "
                    "ordered as asked; nothing was added", refused)
        before: dict[str, int] = {}
        if cart_key:
            for line in self.get_cart(cart_key)["items"]:
                before[line["sku"].casefold()] = line["quantity"] or 0

        key = cart_key
        sent = 0
        failure = None
        replies: list[dict[str, Any]] = []
        for start in range(0, len(ready), _ADD_BATCH):
            batch = ready[start:start + _ADD_BATCH]
            lines = []
            for entry in batch:
                line: dict[str, Any] = {
                    "MouserPartNumber": entry["sku"],
                    "Quantity": entry["quantity"]}
                if entry["customer_part_number"]:
                    line["CustomerPartNumber"] = entry["customer_part_number"]
                if entry["packaging"]:
                    line["PackagingChoice"] = entry["packaging"]
                lines.append(line)
            try:
                reply = self._call(self._INSERT_URL, {}, {
                    "CartKey": key, "CartItems": lines})
            except ProviderError as exc:
                # Before anything was sent, a key or a limit problem is
                # the whole answer. After that, what was sent has to be
                # reported with it.
                if isinstance(exc, ProviderUnavailable) and not sent:
                    raise
                failure = str(exc)
                break
            replies.append(reply)
            sent += len(batch)
            key = key or first_string(reply, ("CartKey",))
            if not key:
                failure = ("mouser answered without a cart key, so the "
                           "cart cannot be read back or added to; "
                           + ("; ".join(_errors(reply))[:300]
                              or "it gave no reason"))
                break

        out: dict[str, Any] = {"cart_key": key or None,
                               "created": bool(key) and not cart_key,
                               "sent": sent}
        if not key:
            out.update(added=0, verified=0, items=[])
            out["failed"] = failure
            out["not_sent"] = [{"item": e["item"], "sku": e["sku"]}
                               for e in ready]
            return out

        cart = self.get_cart(key)
        # What the insert said about a part, when the read-back no
        # longer says it: a refused part is simply absent from the cart.
        said: dict[str, list[str]] = {}
        for reply in replies:
            for problem in _errors(reply):
                said.setdefault("", []).append(problem)
            for line in _dicts(reply.get("CartItems")):
                number = str(line.get("MouserPartNumber") or "").casefold()
                for problem in _errors(line):
                    said.setdefault(number, []).append(problem)
        report = _compare(ready[:sent], cart["items"], before, said)
        out.update({
            "added": sum(1 for r in report if r["in_cart"]),
            "verified": sum(1 for r in report if r["verified"]),
            "items": report,
            "cart": {k: v for k, v in cart.items() if k != "items"},
            "cart_total_lines": cart["count"],
            "not_sent_to_mouser": UNSUPPORTED_NOTE,
        })
        if said.get(""):
            out["mouser_errors"] = list(dict.fromkeys(said[""]))
        if failure:
            out["failed"] = (
                f"mouser refused a batch after {sent} of {len(ready)} "
                f"items were sent: {failure}")
            out["not_sent"] = [{"item": e["item"], "sku": e["sku"]}
                               for e in ready[sent:]]
        return out


def _cart_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        raise CartInputError(
            "which cart: pass its cart_key. A new cart is made only by "
            "part_cart_create")
    if not _CART_KEY.fullmatch(key):
        raise CartInputError(
            "that is not a cart key; one looks like "
            "00000000-0000-0000-0000-000000000000")
    return key


def _says_key_problem(text: str) -> bool:
    low = str(text or "").lower()
    return "apikey" in low.replace(" ", "").replace("_", "") \
        or "unauthorized" in low


def _compare(sent: list[dict[str, Any]], lines: list[dict[str, Any]],
             before: dict[str, int],
             said: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Each sent item beside the cart line it became, with differences."""
    by_sku = {line["sku"].casefold(): line for line in lines}
    report = []
    for entry in sent:
        sku = entry["sku"].casefold()
        line = by_sku.get(sku)
        row: dict[str, Any] = {
            "item": entry["item"], "sku": entry["sku"],
            "quantity": entry["quantity"],
            "customer_part_number": entry["customer_part_number"],
            "packaging": entry["packaging"],
            # Kept here because Mouser's cart has nowhere to put them.
            "designators": entry["designators"],
            "description": entry["description"],
            "manufacturer": entry["manufacturer"],
            "manufacturer_part_number": entry["manufacturer_part_number"],
        }
        differences = []
        if line is None:
            differences.append("not in the cart after adding")
            differences.extend(said.get(sku, []))
        else:
            row["cart_line"] = line
            had = before.get(sku)
            if line["quantity"] == entry["quantity"] and not had:
                pass
            elif had and line["quantity"] == had + entry["quantity"]:
                row["note"] = (
                    f"the cart already held {had} of this part; it now "
                    f"holds {line['quantity']}")
            else:
                differences.append(
                    f"quantity: sent {entry['quantity']}"
                    + (f" to a line that held {had}" if had else "")
                    + f", the cart holds {line['quantity']}"
                    + (f" (minimum {line['moq']}, multiple "
                       f"{line['multiple']})"
                       if line["moq"] or line["multiple"] else ""))
            if entry["customer_part_number"] \
                    != line["customer_part_number"]:
                differences.append(
                    f"customer part number: sent "
                    f"{entry['customer_part_number']!r}, the cart holds "
                    f"{line['customer_part_number']!r}")
            if entry["packaging"] and line["packaging"] \
                    and entry["packaging"].casefold() \
                    != line["packaging"].casefold():
                differences.append(
                    f"packaging: asked for {entry['packaging']}, the cart "
                    f"holds {line['packaging']}")
            for field, label in (
                    ("manufacturer", "manufacturer"),
                    ("manufacturer_part_number", "manufacturer part number")):
                if entry[field] and entry[field].casefold() \
                        != line[field].casefold():
                    differences.append(
                        f"{label}: the BOM says {entry[field]!r}, Mouser's "
                        f"part is {line[field]!r}")
            differences.extend(line["errors"])
        row["differences"] = list(dict.fromkeys(differences))
        row["in_cart"] = line is not None
        row["verified"] = line is not None and not row["differences"]
        report.append(row)
    return report
