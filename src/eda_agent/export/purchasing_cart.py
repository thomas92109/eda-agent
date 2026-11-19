# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""From purchase lines to a DigiKey list or a Mouser cart, with approval.

Three parts, none of which orders or checks out anything:

* ``build_proposal`` decides which lines may go in and fingerprints them.
  A line goes in only when it is ``ready``, or ``review`` and named in
  ``include_lines``. A blocked line (unresolved, out of stock, end of life,
  obsolete, discontinued, no price...) never goes in, however it is asked
  for. The ``proposal_id`` is a hash of the distributor, the target, and
  for every item its SKU, quantity, packaging, unit price, currency,
  lifecycle status and designators: if any of them moves, the id does.
* ``Journal`` is the persistent record of what a commit meant to do and
  what it saw, under the export folder.
* ``commit_mouser`` and ``commit_digikey`` write through the existing
  ``MouserCart`` and ``DigiKeyLists`` clients, to TARGET TOTALS. A Mouser
  insert adds to a line, so a commit reads the cart first and adds only
  what is missing; a retry therefore adds nothing it already added. A cart
  that reads back lower than the journal says it held, or empty, is an
  unknown state and is not touched without the user saying they checked
  it. Nothing is lowered or removed. Every result is read back.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

from eda_agent.atomicfile import replace_with_retry

from .destination import working_file
from .purchasing_model import (
    PurchaseLine,
    PurchasingStatus,
    Severity,
    to_jsonable,
)
from .purchasing_review import FIXED_CODES

__all__ = [
    "CartError",
    "DISTRIBUTORS",
    "Journal",
    "build_proposal",
    "commit_digikey",
    "commit_mouser",
    "make_client",
    "normalize_target",
]

DISTRIBUTORS = ("digikey", "mouser")
_MORE = re.compile(r",?\+MORE$", re.IGNORECASE)


class CartError(ValueError):
    """What was asked cannot be done as it stands. Nothing was written."""


# ---- the target and the proposal ------------------------------------------------

def normalize_target(distributor: Any, cart_key: Any = "", list_id: Any = "",
                     create_new: bool = False,
                     list_name: Any = "") -> dict[str, Any]:
    """Where a proposal would go, checked. An existing cart or list, or a
    new one when ``create_new`` says so."""
    name = str(distributor or "").strip().lower()
    if name not in DISTRIBUTORS:
        raise CartError(f"no distributor {distributor!r}; it is digikey "
                        "(a saved list) or mouser (a cart)")
    cart_key = str(cart_key or "").strip()
    list_id = str(list_id or "").strip()
    list_name = " ".join(str(list_name or "").split())
    if name == "mouser":
        if list_id or list_name:
            raise CartError("list_id and list_name are for DigiKey; Mouser "
                            "takes a cart_key")
        if cart_key and create_new:
            raise CartError("give a cart_key or create_new=True, not both")
        if not cart_key and not create_new:
            raise CartError(
                "which cart: pass cart_key (an existing Mouser cart), or "
                "create_new=True for a new one. A cart is never guessed.")
        return {"kind": "mouser_cart", "cart_key": cart_key or None,
                "create_new": not cart_key}
    if cart_key:
        raise CartError("cart_key is for Mouser; DigiKey takes a list_id")
    if list_id and (create_new or list_name):
        raise CartError("give a list_id, or create_new=True with a "
                        "list_name, not both")
    if not list_id and not (create_new and list_name):
        raise CartError(
            "which list: pass list_id (an existing DigiKey list), or "
            "create_new=True with a list_name. A list is never guessed.")
    return {"kind": "digikey_list", "list_id": list_id or None,
            "list_name": list_name or None, "create_new": not list_id}


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def fingerprint(distributor: str, target: dict[str, Any],
                items: Sequence[dict[str, Any]]) -> str:
    """The approval fingerprint: every figure a person approved."""
    body = {
        "distributor": distributor,
        "target": {"kind": target["kind"],
                   "id": target.get("cart_key") or target.get("list_id")
                   or ("new:" + str(target.get("list_name") or ""))},
        "items": sorted(({
            "sku": i["sku"], "quantity": i["quantity"],
            "packaging": i["packaging"], "unit_price": i["unit_price"],
            "currency": i["currency"], "lifecycle": i["lifecycle"],
            "designators": list(i["designators"]),
            # The number ordered and the BOM's own: an approved variant
            # changes the first and never the second.
            "ordered_mpn": i["ordered_mpn"], "bom_mpn": i["bom_mpn"],
        } for i in items), key=lambda x: x["sku"].casefold()),
    }
    text = json.dumps(_canonical(body), sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _eligibility(pl: PurchaseLine, distributor: str,
                 include: set[str]) -> tuple[bool, str]:
    codes = ", ".join(sorted({r.code for r in pl.reasons})) or "no reasons"
    if pl.status is PurchasingStatus.NOT_FITTED:
        return False, "not fitted: nothing to buy"
    if pl.line.kind == "unresolved":
        return False, "unresolved component: no usable Manufacturer and Part Number"
    hard = [r for r in pl.reasons
            if r.code in FIXED_CODES and r.severity is Severity.BLOCKING]
    if pl.status is PurchasingStatus.BLOCKED or hard:
        why = "blocked: " + codes
        if pl.line.line_id in include:
            why += " (blocked lines are never added, even when named)"
        return False, why
    if pl.offer is None or pl.decision is None or not pl.decision.feasible:
        return False, "no offer to buy"
    if pl.offer.distributor != distributor:
        return False, f"assigned to {pl.offer.distributor}"
    if pl.status is PurchasingStatus.REVIEW \
            and pl.line.line_id not in include:
        return False, (f"needs review ({codes}); name {pl.line.line_id} in "
                       "include_lines to add it")
    return True, ""


def build_proposal(lines: Sequence[PurchaseLine], distributor: str,
                   target: dict[str, Any],
                   include_lines: Sequence[str] = ()) -> dict[str, Any]:
    """The proposal for ``distributor``: items, exclusions and the id."""
    include = {str(x).strip() for x in include_lines or () if str(x).strip()}
    known = {pl.line.line_id for pl in lines}
    warnings = []
    unknown = sorted(include - known)
    if unknown:
        warnings.append("include_lines names no such line: "
                        + ", ".join(unknown))
    items: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for pl in lines:
        ok, why = _eligibility(pl, distributor, include)
        if not ok:
            excluded.append({
                "line_id": pl.line.line_id, "status": pl.status.value,
                "manufacturer": pl.line.manufacturer,
                "part_number": pl.line.part_number,
                "designators": list(pl.line.designators),
                "reasons": sorted({r.code for r in pl.reasons}),
                "why": why})
            continue
        offer, d = pl.offer, pl.decision
        items.append({
            "line_id": pl.line.line_id, "sku": offer.sku,
            "quantity": d.purchase_qty,
            "packaging": ", ".join(offer.packaging),
            "unit_price": d.unit_price, "currency": offer.currency,
            "extended_cost": d.extended_cost,
            "lifecycle": offer.lifecycle.status or offer.lifecycle.raw or "",
            "designators": list(pl.line.designators),
            "description": pl.line.description,
            "manufacturer": pl.line.manufacturer,
            # What the distributor will be asked to hold, and compared
            # with: the approved variant's number when there is one.
            "manufacturer_part_number": pl.matched_mpn or pl.line.part_number,
            "ordered_mpn": pl.matched_mpn or pl.line.part_number,
            "bom_mpn": pl.line.part_number,
            "mpn_variant": pl.mpn_variant,
            "status": pl.status.value,
            "selected_for_review": pl.status is PurchasingStatus.REVIEW,
            "reasons": sorted({r.code for r in pl.reasons}),
            "surplus": d.surplus, "rule": d.rule})
    totals: dict[str, Decimal] = {}
    for item in items:
        totals[item["currency"]] = totals.get(
            item["currency"], Decimal(0)) + item["extended_cost"]
    return {
        "proposal_id": fingerprint(distributor, target, items),
        "distributor": distributor, "target": target,
        "items": items, "excluded": excluded,
        "summary": {
            "items": len(items), "excluded": len(excluded),
            "review_items": sum(1 for i in items if i["selected_for_review"]),
            "by_currency": to_jsonable({
                c: v.quantize(Decimal("0.01")) for c, v in totals.items()}),
            "note": ("Component prices as the distributor states them; "
                     "shipping, taxes and fees are not included. Nothing "
                     "is ordered: this only fills a "
                     + ("list" if distributor == "digikey" else "cart")
                     + " for a person to review and buy."),
        },
        "warnings": warnings,
    }


def make_client(distributor: str) -> Any:
    """The existing client for ``distributor`` (tests replace this)."""
    if distributor == "mouser":
        from eda_agent.libimport.providers.mouser_cart import MouserCart
        return MouserCart()
    from eda_agent.libimport.providers.digikey_lists import DigiKeyLists
    return DigiKeyLists()


# ---- the journal -------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Journal:
    """What a commit meant to do and what it saw, kept between attempts.

    One file per ``proposal_id`` under the export folder. It holds part
    numbers and quantities, never a credential, and is replaced atomically.
    """

    def __init__(self, proposal: dict[str, Any]):
        self.path: Path = working_file("purchasing/journal",
                                       f"{proposal['proposal_id']}.json")
        self.data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}
        if not self.data:
            self.data = {
                "version": 1, "proposal_id": proposal["proposal_id"],
                "distributor": proposal["distributor"],
                "target": proposal["target"], "created_at": _now(),
                "items": [{"line_id": i["line_id"], "sku": i["sku"],
                           "target_quantity": i["quantity"],
                           "designators": i["designators"],
                           "bom_mpn": i["bom_mpn"],
                           "ordered_mpn": i["ordered_mpn"],
                           "mpn_authorized_by": ((i.get("mpn_variant") or {})
                                                 .get("approval") or {}
                                                 ).get("method", "")}
                          for i in proposal["items"]],
                "cart_key": None, "list_id": None, "confirmed": {},
                "events": []}

    def has(self, kind: str) -> bool:
        return any(e.get("event") == kind for e in self.data["events"])

    def event(self, kind: str, **detail: Any) -> None:
        self.data["events"].append(
            {"at": _now(), "event": kind, **to_jsonable(detail)})
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(to_jsonable(self.data), indent=2),
                       encoding="utf-8")
        replace_with_retry(tmp, self.path)


# ---- shared result helpers ----------------------------------------------------------------

def _refusal(reason: str, **more: Any) -> dict[str, Any]:
    return {"ok": False, "reason": reason, **more}


def _unknown(journal: Journal, doing: str, exc: Exception) -> dict[str, Any]:
    journal.event("outcome_unknown", doing=doing, error=str(exc)[:300])
    return _refusal(
        f"{doing} failed ({type(exc).__name__}: {str(exc)[:300]}), and what "
        "the distributor did is not known. Read the cart or list "
        "(part_cart_get or part_list_get) before anything else. Committing "
        "the same proposal again is safe: it adds only what is missing.",
        outcome="unknown", journal=str(journal.path))


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {"items": len(rows)}
    for key in ("added", "satisfied", "conflict", "over_target", "short",
                "not_in_list"):
        out[key] = sum(1 for r in rows if r["status"] == key)
    out["verified"] = sum(1 for r in rows if r["verified"])
    return out


# ---- Mouser ---------------------------------------------------------------------------------

def _mouser_quantities(cart: dict[str, Any]) -> dict[str, int]:
    return {str(line.get("sku") or "").casefold(): int(line.get("quantity") or 0)
            for line in cart.get("items") or []}


def _send_item(item: dict[str, Any], quantity: int) -> dict[str, Any]:
    return {"sku": item["sku"], "quantity": quantity,
            "designators": item["designators"],
            "description": item["description"],
            "manufacturer": item["manufacturer"],
            "manufacturer_part_number": item["manufacturer_part_number"]}


def commit_mouser(proposal: dict[str, Any], client: Any, journal: Journal,
                  cart_state_checked: bool = False) -> dict[str, Any]:
    """Bring a Mouser cart to the proposal's target totals, and verify."""
    items = proposal["items"]
    if not items:
        return _refusal("the proposal has no items; nothing to add")
    key = proposal["target"].get("cart_key") or journal.data.get("cart_key")
    created = False

    if not key:
        if journal.has("create_started"):
            return _refusal(
                "a cart creation for this proposal was started earlier and "
                "no cart key was recorded, so Mouser may already hold that "
                "cart. Another one is NOT created. Find it on Mouser's site "
                "and prepare a new proposal with its cart_key.",
                outcome="unknown", journal=str(journal.path))
        try:
            ready = client.prepare_items([_send_item(i, i["quantity"])
                                          for i in items])
            refused = client.unorderable(ready)
        except Exception as exc:  # noqa: BLE001 - refusals say what is wrong
            return _refusal(f"Mouser cannot take these items: {exc}",
                            problems=getattr(exc, "problems", []))
        if refused:
            return _refusal(
                f"{len(refused)} of {len(items)} items cannot be ordered as "
                "asked; no cart was created", problems=refused)
        journal.event("create_started", items=len(items))
        try:
            reply = client.create_cart(
                [_send_item(i, i["quantity"]) for i in items],
                check_products=False)
        except Exception as exc:  # noqa: BLE001
            return _unknown(journal, "creating the cart", exc)
        key = reply.get("cart_key")
        if not key:
            journal.event("create_no_key", reply=str(reply)[:300])
            return _refusal(
                "Mouser answered without a cart key, so the cart cannot be "
                "read back or found again. Check Mouser's site; nothing "
                "more was sent.", outcome="unknown",
                journal=str(journal.path))
        created = True
        journal.data["cart_key"] = key
        journal.event("create_ok", cart_key=key)

    before: dict[str, int] = {}
    if not created:
        try:
            cart = client.get_cart(key)
        except Exception as exc:  # noqa: BLE001
            return _refusal(f"the cart could not be read: {exc}. Nothing "
                            "was written.")
        before = _mouser_quantities(cart)
        confirmed = journal.data.get("confirmed") or {}
        lower = {sku: (qty, before.get(sku, 0))
                 for sku, qty in confirmed.items()
                 if before.get(sku, 0) < qty}
        if not cart_state_checked and (lower or not before):
            what = ("is empty" if not before else
                    "holds less than the journal says: " + ", ".join(
                        f"{s} {have} of {want}"
                        for s, (want, have) in sorted(lower.items())))
            return _refusal(
                f"the cart {what}. Mouser carts have read back empty after "
                "being opened in a browser, so this is an UNKNOWN state, "
                "not proof that nothing was added, and nothing was written. "
                "Look at the cart on Mouser's site; if the readback is "
                "right, call again with cart_state_checked=True and the "
                "missing amounts are added from what the cart holds now.",
                outcome="unknown", journal=str(journal.path))

    rows: list[dict[str, Any]] = []
    to_add = []
    for item in items:
        have = before.get(item["sku"].casefold(), 0)
        target = item["quantity"]
        row = {"line_id": item["line_id"], "sku": item["sku"],
               "target_quantity": target, "had": have, "added": 0,
               "now": None, "verified": False, "status": "pending",
               "note": ""}
        if created:
            row["had"] = 0
            row["added"] = target
        elif have == target:
            row["status"] = "satisfied"
        elif have > target:
            row["status"] = "over_target"
            row["note"] = (f"the cart holds {have}, more than the {target} "
                           "proposed; it is left as it is (nothing here "
                           "lowers or removes a quantity)")
        else:
            to_add.append((item, target - have))
            row["added"] = target - have
        rows.append(row)

    if to_add:
        try:
            ready = client.prepare_items([_send_item(i, i["quantity"])
                                          for i, _ in to_add])
            refused = client.unorderable(ready)
        except Exception as exc:  # noqa: BLE001
            return _refusal(f"Mouser cannot take these items: {exc}",
                            problems=getattr(exc, "problems", []))
        if refused:
            return _refusal(
                f"{len(refused)} of {len(to_add)} items cannot be ordered "
                "at their target quantity; nothing was added",
                problems=refused)
        journal.event("adding", deltas={i["sku"]: d for i, d in to_add},
                      expected_after={i["sku"]: i["quantity"]
                                      for i, _ in to_add})
        try:
            reply = client.add_items(
                key, [_send_item(i, d) for i, d in to_add],
                check_products=False)
        except Exception as exc:  # noqa: BLE001
            return _unknown(journal, "adding to the cart", exc)
        if isinstance(reply, dict) and reply.get("failed"):
            journal.event("add_partial", failed=str(reply["failed"])[:300])

    try:
        after = _mouser_quantities(client.get_cart(key))
    except Exception as exc:  # noqa: BLE001
        return _unknown(journal, "reading the cart back", exc)
    for row in rows:
        now = after.get(row["sku"].casefold(), 0)
        row["now"] = now
        target = row["target_quantity"]
        if now == target:
            row["verified"] = True
            if row["status"] == "pending":
                row["status"] = "added"
        elif row["status"] == "over_target" and now >= target:
            pass
        else:
            row["status"] = "short" if now < target else "over_target"
            row["note"] = row["note"] or (
                f"the cart holds {now} after the commit, the target is "
                f"{target}")
    journal.data["confirmed"] = {
        r["sku"].casefold(): r["now"] for r in rows if r["verified"]}
    journal.event("readback", rows=_summary(rows))
    summary = _summary(rows)
    return {
        "ok": True, "distributor": "mouser", "cart_key": key,
        "created": created, "items": rows, "summary": summary,
        "verified": summary["verified"] == len(rows),
        "journal": str(journal.path),
        "not_sent_to_mouser": (
            "Mouser's cart has no field for reference designators or a "
            "description; they are in the journal and this reply."),
        "note": "Nothing was ordered. Check the cart on Mouser's site.",
    }


# ---- DigiKey ----------------------------------------------------------------------------------

def _names(text: Any) -> frozenset:
    cleaned = _MORE.sub("", str(text or "").strip())
    return frozenset(t for t in re.split(r"[,\s]+", cleaned) if t)


def _lines_by_sku(parts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        sku = str(part.get("sku") or part.get("requested_part_number")
                  or "").casefold()
        out.setdefault(sku, []).append(part)
    return out


def _same_line(item: dict[str, Any], part: dict[str, Any]) -> bool:
    if part.get("quantity") != item["quantity"]:
        return False
    refs = _names(part.get("reference_designator"))
    return not refs or refs == frozenset(item["designators"])


def commit_digikey(proposal: dict[str, Any], client: Any, journal: Journal,
                   cart_state_checked: bool = False) -> dict[str, Any]:
    """Bring a DigiKey list to the proposal's lines, and verify.

    A line already present with the same SKU and quantity (and designators)
    is left alone. A line with the same SKU and another quantity is
    reported and not changed: no update call exists here.
    """
    items = proposal["items"]
    if not items:
        return _refusal("the proposal has no items; nothing to add")
    target = proposal["target"]
    list_id = target.get("list_id") or journal.data.get("list_id")
    created = False

    if not list_id:
        if journal.has("create_started"):
            return _refusal(
                "a list creation for this proposal was started earlier and "
                "no list id was recorded, so DigiKey may already hold that "
                "list. Another one is NOT created. Find it in the account "
                "(part_list_get) and prepare a new proposal with its "
                "list_id.", outcome="unknown", journal=str(journal.path))
        journal.event("create_started", name=target.get("list_name"))
        try:
            made = client.create_list(target["list_name"], "private")
        except Exception as exc:  # noqa: BLE001
            return _unknown(journal, "creating the list", exc)
        list_id = made.get("list_id")
        if not list_id:
            journal.event("create_no_id", reply=str(made)[:300])
            return _refusal("DigiKey answered without a list id; check the "
                            "account (part_list_get). Nothing more was sent.",
                            outcome="unknown", journal=str(journal.path))
        created = True
        journal.data["list_id"] = list_id
        journal.event("create_ok", list_id=list_id)

    try:
        existing = client.get_list(list_id).get("items") or []
    except Exception as exc:  # noqa: BLE001
        return _refusal(f"the list could not be read: {exc}. Nothing was "
                        "written.")
    held = _lines_by_sku(existing)
    confirmed = journal.data.get("confirmed") or {}
    missing = [sku for sku in confirmed if sku not in held]
    # An empty list is normal (it may have just been created); a list that
    # no longer holds what the journal says it did is an unknown state.
    if not created and not cart_state_checked and missing:
        return _refusal(
            "the list no longer holds " + ", ".join(sorted(missing))
            + ", which the journal says it did. That is an UNKNOWN state, "
            "not proof that nothing was added, and nothing was written. "
            "Look at the list in the account; if the readback is right, "
            "call again with cart_state_checked=True.", outcome="unknown",
            journal=str(journal.path))

    rows: list[dict[str, Any]] = []
    to_add = []
    for item in items:
        mine = held.get(item["sku"].casefold(), [])
        row = {"line_id": item["line_id"], "sku": item["sku"],
               "target_quantity": item["quantity"], "had": sum(
                   p.get("quantity") or 0 for p in mine), "added": 0,
               "now": None, "verified": False, "status": "pending",
               "note": ""}
        if any(_same_line(item, p) for p in mine):
            row["status"] = "satisfied"
        elif mine:
            row["status"] = "conflict"
            row["note"] = (
                f"the list already holds this part at "
                f"{', '.join(str(p.get('quantity')) for p in mine)}; the "
                f"proposal says {item['quantity']}. It is left as it is: "
                "change it in the account")
        else:
            to_add.append(item)
            row["added"] = item["quantity"]
        rows.append(row)

    if to_add:
        journal.event("adding", items={i["sku"]: i["quantity"]
                                       for i in to_add})
        try:
            client.add_items(list_id, [{
                "sku": i["sku"], "quantity": i["quantity"],
                "designators": i["designators"],
                "description": i["description"],
                "manufacturer": i["manufacturer"]} for i in to_add],
                check_products=True)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ListInputError":
                return _refusal(f"DigiKey cannot take these items: {exc}. "
                                "Nothing was added.",
                                problems=getattr(exc, "problems", []))
            return _unknown(journal, "adding to the list", exc)

    try:
        fresh = _lines_by_sku(client.get_list(list_id).get("items") or [])
    except Exception as exc:  # noqa: BLE001
        return _unknown(journal, "reading the list back", exc)
    by_sku = {i["sku"].casefold(): i for i in items}
    for row in rows:
        item = by_sku[row["sku"].casefold()]
        mine = fresh.get(row["sku"].casefold(), [])
        row["now"] = sum(p.get("quantity") or 0 for p in mine)
        if any(_same_line(item, p) for p in mine):
            row["verified"] = row["status"] != "conflict"
            if row["status"] == "pending":
                row["status"] = "added"
        elif row["status"] != "conflict":
            row["status"] = "not_in_list" if not mine else "short"
            row["note"] = row["note"] or "the list does not hold what was sent"
    journal.data["confirmed"] = {
        r["sku"].casefold(): r["now"] for r in rows if r["verified"]}
    journal.event("readback", rows=_summary(rows))
    summary = _summary(rows)
    return {
        "ok": True, "distributor": "digikey", "list_id": list_id,
        "created": created, "items": rows, "summary": summary,
        "verified": summary["verified"] == len(rows),
        "journal": str(journal.path),
        "note": ("Nothing was ordered. A DigiKey list is not a cart: open "
                 "it in the account to add it to a cart."),
    }
