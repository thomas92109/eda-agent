# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A purchasing proposal from an enriched BOM. Arithmetic only.

Given what each distributor offers for each BOM line
(``purchasing_sources.enrich_bom``), this works out what to order and
where: the quantity that satisfies the minimum and the multiple, the
price break that applies, whether a larger break is cheaper in total,
whether the stock covers it, and which distributor gets the line.

It reads nothing and writes nothing. Every figure in a plan comes from
an offer; nothing here estimates a price, a shipping cost, a tax or a
tariff, and a line with no usable offer is listed as unpurchasable with
the reason, never dropped.

One BOM line goes to one distributor, whole. Splitting a line across
two is not done.

What is assumed, and said in the plan wherever it is used:

* A DigiKey tape-and-reel offer states no order multiple. Its standard
  package is taken as the multiple, because a reel is sold whole.
* An offer that states no minimum is taken to start at its first price
  break, and one that states no multiple at 1.
* Offers of one part in different packagings are different purchases.
  Each is priced on its own terms, a Digi-Reel with its reeling fee,
  and the cheapest valid one for the quantity is chosen.
"""

from __future__ import annotations

import math
from typing import Any, Optional

__all__ = ["PREFERENCES", "plan_purchases", "price_offer"]

PREFERENCES = ("lowest", "digikey", "mouser")

#: Lifecycle states worth a warning on a plan line.
_AGEING = {"nrnd": "not recommended for new designs",
           "last_time_buy": "last-time buy",
           "end_of_life": "end of life",
           "obsolete": "obsolete",
           "discontinued": "discontinued"}

NOT_INCLUDED = (
    "Component prices only, as the distributors' catalogues state them. "
    "Shipping, taxes, tariffs and handling fees are not included: the "
    "catalogue APIs do not give them. Mouser adds a tariff per line in "
    "its cart where one applies.")


def _money(value: float) -> float:
    return round(value + 0.0, 5)


def _breaks(offer: dict[str, Any]) -> list[tuple[int, float]]:
    """The offer's price ladder, lowest quantity first, usable entries
    only."""
    ladder = []
    for entry in offer.get("price_breaks") or []:
        quantity, price = entry.get("quantity"), entry.get("unit_price")
        if isinstance(quantity, int) and not isinstance(quantity, bool) \
                and quantity >= 1 and isinstance(price, (int, float)) \
                and not isinstance(price, bool) and price > 0:
            ladder.append((quantity, float(price)))
    return sorted(ladder)


def _unit_price(ladder: list[tuple[int, float]],
                quantity: int) -> Optional[tuple[int, float]]:
    """The break that prices ``quantity``: the largest one not above
    it. None below the first break."""
    found = None
    for at, price in ladder:
        if at <= quantity:
            found = (at, price)
    return found


def _up_to_multiple(quantity: int, multiple: int) -> int:
    return int(math.ceil(quantity / multiple) * multiple)


def price_offer(offer: dict[str, Any], required: int,
                allow_overbuy: bool = True) -> dict[str, Any]:
    """What ordering ``required`` of one offer comes to.

    Returns ``usable`` and, when it is, ``order_quantity``,
    ``unit_price``, ``price_break_quantity``, ``extended_price`` (the
    reeling fee included), ``surplus`` and ``notes``; when it is not,
    ``reason``. ``stock_short`` is true when the offer's stated stock
    does not cover the order quantity.

    With ``allow_overbuy`` a larger price break is taken when the whole
    order then costs less, and the plan says how much it saves and how
    many parts are left over.
    """
    ladder = _breaks(offer)
    out: dict[str, Any] = {
        "sku": offer.get("sku"), "packaging": offer.get("packaging") or [],
        "currency": offer.get("currency"), "stock": offer.get("stock"),
        "moq": offer.get("moq"), "multiple": offer.get("multiple")}
    if not ladder:
        return {**out, "usable": False,
                "reason": "the distributor states no price for it"}
    if not offer.get("currency"):
        return {**out, "usable": False,
                "reason": "the distributor states no currency for its price"}
    notes = []
    minimum = offer.get("moq")
    if not isinstance(minimum, int) or minimum < 1:
        minimum = ladder[0][0]
        notes.append(f"no minimum stated; taken from the first price "
                     f"break, {minimum}")
    minimum = max(minimum, ladder[0][0])
    multiple = offer.get("multiple")
    if not isinstance(multiple, int) or multiple < 1:
        package = offer.get("standard_package")
        reel = any("tape & reel" in str(p).casefold()
                   or str(p).casefold() in ("reel", "full reel")
                   for p in out["packaging"]) and len(out["packaging"]) == 1
        if reel and isinstance(package, int) and package > 1:
            multiple = package
            notes.append(f"no multiple stated; a reel is sold whole, so "
                         f"the standard package of {package} is used")
        else:
            multiple = 1
    fee = offer.get("reeling_fee")
    fee = float(fee) if isinstance(fee, (int, float)) and fee > 0 else 0.0

    def cost(quantity: int) -> float:
        return quantity * _unit_price(ladder, quantity)[1] + fee

    base = _up_to_multiple(max(required, minimum), multiple)
    chosen = base
    if allow_overbuy:
        for at, _ in ladder:
            candidate = _up_to_multiple(max(at, minimum), multiple)
            if candidate > chosen and cost(candidate) < cost(chosen) - 1e-9:
                chosen = candidate
    at, price = _unit_price(ladder, chosen)
    out.update({
        "usable": True, "order_quantity": chosen, "unit_price": price,
        "price_break_quantity": at,
        "extended_price": _money(cost(chosen)),
        "surplus": chosen - required,
    })
    if fee:
        out["reeling_fee"] = fee
        notes.append(f"includes a reeling fee of {fee:g}")
    if base > required:
        why = []
        if minimum > required:
            why.append(f"minimum {minimum}")
        if multiple > 1:
            why.append(f"multiple {multiple}")
        notes.append(f"{required} needed; {base} is the least that can be "
                     f"ordered ({', '.join(why)})")
    if chosen > base:
        out["overbuy"] = {
            "instead_of": base,
            "instead_of_price": _money(cost(base)),
            "saves": _money(cost(base) - cost(chosen))}
        notes.append(
            f"{chosen} at the {at} price break costs less in total than "
            f"{base} ({_money(cost(chosen))} against {_money(cost(base))})")
    stock = offer.get("stock")
    out["stock_short"] = isinstance(stock, int) and stock < chosen
    if out["stock_short"] and chosen > base and stock >= base:
        # The larger break is not in stock; the plain quantity is.
        return price_offer(offer, required, allow_overbuy=False)
    if stock is None:
        notes.append("the distributor states no stock for this packaging")
    out["notes"] = notes
    return out


def _best(options: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The cheapest, then the smallest, then by number: one answer for
    one input, always."""
    if not options:
        return None
    return min(options, key=lambda o: (o["extended_price"],
                                       o["order_quantity"], str(o["sku"])))


def _distributor_option(name: str, found: dict[str, Any], required: int,
                        allow_overbuy: bool) -> dict[str, Any]:
    """One distributor's best purchase for a line, or why it has none."""
    status = found.get("status")
    if status != "matched":
        return {"distributor": name, "purchasable": False,
                "status": status, "reason": found.get("reason")
                or f"{name}: {status}",
                **({"candidates": found["candidates"]}
                   if found.get("candidates") else {})}
    priced = [price_offer(offer, required, allow_overbuy)
              for offer in found.get("offers") or []]
    usable = [p for p in priced if p["usable"]]
    in_stock = [p for p in usable if not p["stock_short"]]
    considered = [
        {k: p.get(k) for k in ("sku", "packaging", "order_quantity",
                               "unit_price", "extended_price", "stock",
                               "stock_short", "reason") if k in p}
        for p in priced]
    base = {"distributor": name, "status": status, "offers_considered":
            considered, "manufacturer": found.get("manufacturer"),
            "lifecycle": found.get("lifecycle"),
            "cached": found.get("cached"),
            "retrieved_at": found.get("retrieved_at")}
    pick = _best(in_stock)
    if pick is not None:
        notes = list(pick.pop("notes", []))
        if found.get("note"):
            notes.append(found["note"])
        if found.get("lifecycle") in _AGEING:
            notes.append(f"lifecycle: {_AGEING[found['lifecycle']]}")
        pick.pop("usable", None)
        pick.pop("stock_short", None)
        return {**base, "purchasable": True, **pick, "notes": notes}
    if usable:
        short = _best(usable)
        return {**base, "purchasable": False, "status": "insufficient_stock",
                "reason": (f"{name} has {short['stock']} of "
                           f"{short['sku']} in stock and "
                           f"{short['order_quantity']} are needed")}
    if not priced:
        return {**base, "purchasable": False, "status": "no_offer",
                "reason": f"{name} lists the part with no orderable number"}
    return {**base, "purchasable": False, "status": "no_pricing",
            "reason": f"{name}: " + "; ".join(sorted({
                str(p.get("reason")) for p in priced}))}


def plan_purchases(analysis: dict[str, Any], prefer: str = "lowest",
                   allow_overbuy: bool = True) -> dict[str, Any]:
    """A purchasing proposal for an enriched BOM.

    ``prefer`` is ``lowest`` (each line goes where its extended price is
    lowest), or ``digikey`` or ``mouser`` (each line goes there when it
    can be bought there, and to the other, marked as a fallback, when it
    cannot).

    Returns ``lines`` (every BOM line, each ``planned`` with its
    ``choice`` or ``unpurchasable`` with ``problems``), ``orders`` (the
    planned lines per distributor), ``totals`` and ``complete``.
    """
    wanted = str(prefer or "").strip().lower()
    if wanted not in PREFERENCES:
        raise ValueError(f"no preference {prefer!r}; it is one of "
                         + ", ".join(PREFERENCES))
    lines, unpurchasable = [], []
    orders: dict[str, list[dict[str, Any]]] = {}
    for entry in analysis.get("lines") or []:
        required = entry["required_quantity"]
        options = [
            _distributor_option(name, found, required, allow_overbuy)
            for name, found in (entry.get("distributors") or {}).items()]
        buyable = [o for o in options if o["purchasable"]]
        line: dict[str, Any] = {
            key: entry[key] for key in (
                "line", "manufacturer", "part_number", "description",
                "designators", "quantity_per_board", "required_quantity")}
        choice, notes = None, []
        if buyable:
            currencies = sorted({o["currency"] for o in buyable})
            preferred = [o for o in buyable if o["distributor"] == wanted]
            if preferred:
                choice = preferred[0]
            elif wanted != "lowest":
                choice = _best(buyable) if len(currencies) == 1 else None
                why = next((o["reason"] for o in options
                            if o["distributor"] == wanted
                            and not o["purchasable"]),
                           f"{wanted} was not asked")
                if choice is not None:
                    notes.append(
                        f"fallback: not bought at {wanted} ({why})")
            elif len(currencies) == 1:
                choice = _best(buyable)
                tied = [o for o in buyable if o is not choice
                        and o["extended_price"] == choice["extended_price"]]
                if tied:
                    notes.append(
                        "the same price at "
                        + ", ".join(o["distributor"] for o in tied)
                        + "; the smaller order, then the part number, "
                        "decided")
            if choice is None:
                line["problems"] = [
                    "the distributors price this line in different "
                    f"currencies ({', '.join(currencies)}), which are not "
                    "compared here; name a distributor with prefer"]
        if choice is not None:
            line["status"] = "planned"
            line["choice"] = {**{k: v for k, v in choice.items()
                                 if k not in ("purchasable", "status",
                                              "offers_considered")},
                              "notes": list(choice.get("notes", [])) + notes}
            orders.setdefault(choice["distributor"], []).append({
                "line": entry["line"], "sku": choice["sku"],
                "quantity": choice["order_quantity"],
                "designators": entry["designators"],
                "description": entry["description"],
                "manufacturer": entry["manufacturer"],
                "manufacturer_part_number": entry["part_number"],
                "extended_price": choice["extended_price"],
                "currency": choice["currency"]})
        else:
            line["status"] = "unpurchasable"
            line.setdefault("problems", [
                str(o.get("reason")) for o in options] or [
                "no distributor was asked about this line"])
            unpurchasable.append(entry["line"])
        line["alternatives"] = [
            {k: v for k, v in option.items() if k != "notes"}
            for option in options
            if choice is None or option is not choice]
        lines.append(line)

    totals: dict[str, Any] = {"distributors": {}}
    currencies = set()
    for name, items in sorted(orders.items()):
        by_currency: dict[str, float] = {}
        for item in items:
            by_currency[item["currency"]] = by_currency.get(
                item["currency"], 0.0) + item["extended_price"]
        currencies.update(by_currency)
        totals["distributors"][name] = {
            "lines": len(items),
            "subtotal": {c: round(v, 2) for c, v in by_currency.items()}}
    if len(currencies) == 1:
        currency = next(iter(currencies))
        totals["component_subtotal"] = {currency: round(sum(
            t["subtotal"][currency]
            for t in totals["distributors"].values()), 2)}
    elif currencies:
        totals["component_subtotal"] = None
        totals["note"] = ("the plan is priced in several currencies ("
                          + ", ".join(sorted(currencies))
                          + "); they are not added together")
    else:
        totals["component_subtotal"] = None
    totals["not_included"] = NOT_INCLUDED

    unresolved = list(analysis.get("unresolved") or [])
    bom_complete = analysis.get("bom_complete", True)
    return {
        "prefer": wanted,
        "allow_overbuy": bool(allow_overbuy),
        "build_quantity": analysis.get("build_quantity"),
        "complete": bool(lines) and not unpurchasable and not unresolved
        and bool(bom_complete),
        "summary": {
            "bom_lines": len(lines),
            "planned": len(lines) - len(unpurchasable),
            "unpurchasable": len(unpurchasable),
            "unresolved_components": len(unresolved),
            "bom_complete": bool(bom_complete),
        },
        "lines": lines,
        "unpurchasable_lines": unpurchasable,
        "unresolved": unresolved,
        "orders": orders,
        "totals": totals,
    }
