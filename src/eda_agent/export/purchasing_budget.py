# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The Budgeting BOM: what a build would cost, line by line. Pure.

It answers "how much will this build cost?", not "what can be ordered
today?". It reads the same enriched BOM as the Purchasing BOM
(``purchasing_sources.enrich_bom`` through the purchasing tools) and the
same quantity engine prices it, with two differences:

* stock and the maximum order quantity are left out, so a part that is out
  of stock or short still has its price (its stock is shown beside it);
* the policy is cost only (``QuantityPolicy.cost_only``): the requirement
  raised to the minimum and the multiple, and a higher price break only
  when it costs less. No overage, no paid upgrade.

Every fitted line and every unresolved component is a row. A line nobody
prices is a row with an empty price and the reason. A price comes only
from a distributor listing the BOM's own number, or a close variant that
was authorized (a project decision, an approval for the call or a global
rule; ``mpn_variant`` with ``approved``). An unapproved close candidate is
never priced.

Each price says where it came from: ``current`` (read from the
distributor in this call), ``cached`` (reused from the distributor cache,
with the time it was read) or ``missing``. The money is a PRICED
SUBTOTAL; it is called a total only when every line is priced in one
currency.

Nothing here reads a file, calls a distributor or writes anything. The
Purchasing BOM, its optimizer and the carts are not touched.
"""

from __future__ import annotations

import csv
import dataclasses
import io
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional, Sequence

from .purchasing_bom_csv import (
    _excel_reinterprets,
    _some,
    neutralize_formula,
)
from .purchasing_config import Settings
from .purchasing_lines import offers_from_entry
from .purchasing_model import (
    QuantityDecision,
    QuantityPolicy,
    SupplierOffer,
    to_jsonable,
)
from .purchasing_quantity import decide_quantity

__all__ = [
    "BUDGET_HEADERS",
    "PRICE_CACHED",
    "PRICE_CURRENT",
    "PRICE_MISSING",
    "SUMMARY_PRICED",
    "SUMMARY_SUBTOTAL",
    "SUMMARY_TOTAL",
    "SUMMARY_UNPRICED",
    "BudgetLine",
    "budget_lines",
    "budget_totals",
    "build_budget_csv",
]

PRICE_CURRENT = "current"
PRICE_CACHED = "cached"
PRICE_MISSING = "missing"

_ORDER = ("digikey", "mouser")


@dataclass(frozen=True)
class BudgetLine:
    line_id: str
    kind: str                       # fitted | unresolved
    manufacturer: str
    part_number: str
    description: str
    designators: tuple
    bom_quantity: int
    required_qty: int
    price_source: str               # current | cached | missing
    budget_qty: Optional[int] = None
    unit_price: Optional[Decimal] = None
    break_quantity: Optional[int] = None
    extended_cost: Optional[Decimal] = None
    currency: str = ""
    distributor: str = ""
    sku: str = ""
    packaging: tuple = ()
    matched_mpn: str = ""
    mpn_basis: str = ""             # exact | approved variant (...)
    retrieved_at: str = ""
    stock: Optional[int] = None
    stock_status: str = ""          # in stock | short | out of stock |
    #                                 not stated
    notes: tuple = ()

    @property
    def priced(self) -> bool:
        return self.price_source != PRICE_MISSING


def _age_hours(stamp: Any, now: datetime) -> Optional[Decimal]:
    try:
        then = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return Decimal((now - then).total_seconds()) / 3600


def _stock_status(stock: Optional[int], quantity: int) -> str:
    if stock is None:
        return "not stated"
    if stock <= 0:
        return "out of stock"
    if stock < quantity:
        return "short"
    return "in stock"


def _priced_offers(name: str, found: dict[str, Any], required: int,
                   policy: QuantityPolicy
                   ) -> list[tuple[SupplierOffer, QuantityDecision]]:
    """Every offer of one matched distributor that has a price for
    ``required``, stock and the purchase limit left out."""
    out = []
    for offer in offers_from_entry(name, found):
        unlimited = dataclasses.replace(offer, stock=None, max_order_qty=None)
        decision = decide_quantity(unlimited, required, policy)
        if decision.feasible:
            out.append((offer, decision))
    return out


def _why_unpriced(name: str, found: dict[str, Any]) -> str:
    status = str(found.get("status") or "not asked")
    if status == "matched":
        return f"{name}: listed, but with no usable price and currency"
    reason = str(found.get("reason") or "").strip()
    return f"{name}: {status}" + (f" ({reason})" if reason else "")


def _choose(best: dict[str, tuple], prefer: str) -> Optional[str]:
    """The distributor whose price the budget uses."""
    if not best:
        return None
    if prefer in best:
        return prefer
    currencies = {offer.currency for offer, _, _ in best.values()}
    if prefer == "lowest" and len(currencies) == 1:
        return min(best, key=lambda n: (best[n][1].extended_cost,
                                        best[n][1].purchase_qty,
                                        best[n][0].sku))
    return min(best, key=lambda n: _ORDER.index(n) if n in _ORDER else 9)


def _fitted_line(position: int, entry: dict[str, Any], settings: Settings,
                 now: datetime) -> BudgetLine:
    per_board = int(entry.get("quantity_per_board") or 0)
    required = per_board * settings.assemblies
    common = dict(
        line_id=f"L{position + 1:03d}", kind="fitted",
        manufacturer=str(entry.get("manufacturer") or ""),
        part_number=str(entry.get("part_number") or ""),
        description=str(entry.get("description") or ""),
        designators=tuple(str(d) for d in entry.get("designators") or ()),
        bom_quantity=per_board, required_qty=required)
    if required < 1:
        return BudgetLine(**common, price_source=PRICE_MISSING,
                          notes=("the line has no quantity to price",))

    policy = QuantityPolicy.cost_only(settings.assemblies)
    best: dict[str, tuple] = {}
    unpriced: list[str] = []
    for name, found in (entry.get("distributors") or {}).items():
        if name not in settings.enabled:
            continue
        variant = found.get("mpn_variant")
        if found.get("status") != "matched" or (
                variant and not variant.get("approved")):
            unpriced.append(_why_unpriced(name, found))
            continue
        offers = _priced_offers(name, found, required, policy)
        if not offers:
            unpriced.append(_why_unpriced(name, found))
            continue
        offer, decision = min(offers, key=lambda od: (
            od[1].extended_cost, od[1].purchase_qty, od[0].sku))
        best[name] = (offer, decision, found)

    close = [c for c in entry.get("close_matches") or [] if not c.get("used")]
    chosen = _choose(best, settings.prefer)
    if chosen is None:
        notes = list(unpriced) or ["no distributor was asked about this line"]
        if close:
            notes.append(f"{len(close)} close part number candidate(s) are "
                         "not approved, so none is priced")
        return BudgetLine(**common, price_source=PRICE_MISSING,
                          notes=tuple(notes))

    offer, decision, found = best[chosen]
    notes: list[str] = []
    if settings.prefer in _ORDER and chosen != settings.prefer:
        notes.append(f"priced at {chosen}: "
                     + next((u for u in unpriced
                             if u.startswith(settings.prefer + ":")),
                            f"{settings.prefer} is not enabled"))
    variant = found.get("mpn_variant") or {}
    basis = "exact"
    if variant:
        approval = variant.get("approval") or {}
        basis = (f"approved variant ({variant.get('label', '')}; "
                 f"{approval.get('method', 'approved')})")
    if offer.manufacturer_match == "partial":
        notes.append(f"{chosen} names the maker {offer.manufacturer!r}; "
                     "taken as the BOM's")
    cached = bool(found.get("cached"))
    age = _age_hours(found.get("retrieved_at"), now)
    if cached and age is not None and age > settings.stale_pricing_hours:
        notes.append(f"cached price read {age.quantize(Decimal('0.1'))} "
                     "hours ago")
    status = _stock_status(offer.stock, decision.purchase_qty)
    if status in ("short", "out of stock"):
        notes.append(f"stock {status}: priced for budgeting only")
    return BudgetLine(
        **common, price_source=PRICE_CACHED if cached else PRICE_CURRENT,
        budget_qty=decision.purchase_qty, unit_price=decision.unit_price,
        break_quantity=decision.break_quantity,
        extended_cost=decision.extended_cost,
        currency=offer.currency or "", distributor=chosen, sku=offer.sku,
        packaging=tuple(offer.packaging),
        matched_mpn=str(variant.get("candidate_mpn") or ""),
        mpn_basis=basis, retrieved_at=str(found.get("retrieved_at") or ""),
        stock=offer.stock, stock_status=status, notes=tuple(notes))


def _unresolved_line(position: int, entry: dict[str, Any],
                     settings: Settings) -> BudgetLine:
    text = str(entry.get("reason")
               or "no usable Manufacturer and Part Number")
    return BudgetLine(
        line_id=f"U{position + 1:03d}", kind="unresolved",
        manufacturer=str(entry.get("manufacturer") or ""),
        part_number=str(entry.get("part_number") or ""),
        description=str(entry.get("description") or ""),
        designators=(str(entry.get("designator") or ""),),
        bom_quantity=1, required_qty=settings.assemblies,
        price_source=PRICE_MISSING, notes=(f"unresolved: {text}",))


def budget_lines(analysis: dict[str, Any], settings: Settings,
                 now: Optional[datetime] = None) -> list[BudgetLine]:
    """Every fitted line and unresolved component of ``analysis`` (what the
    purchasing tools build with ``find_close_matches``), priced for
    ``settings.assemblies`` boards where a distributor gives a price."""
    now = now or datetime.now(timezone.utc)
    out = [_fitted_line(i, e, settings, now)
           for i, e in enumerate(analysis.get("lines") or [])]
    out += [_unresolved_line(i, e, settings)
            for i, e in enumerate(analysis.get("unresolved") or [])]
    return out


def budget_totals(lines: Sequence[BudgetLine]) -> dict[str, Any]:
    """The priced subtotal per currency and what is missing from it.

    ``estimated_total`` is set only when every line is priced and in one
    currency; otherwise it is None and ``note`` says why."""
    sources = Counter(bl.price_source for bl in lines)
    subtotal: dict[str, Decimal] = {}
    priced_lines: dict[str, int] = {}
    for bl in lines:
        if bl.priced and bl.extended_cost is not None:
            cur = bl.currency or "?"
            subtotal[cur] = subtotal.get(cur, Decimal(0)) + bl.extended_cost
            priced_lines[cur] = priced_lines.get(cur, 0) + 1
    rounded = {cur: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
               for cur, value in subtotal.items()}
    unpriced = sources.get(PRICE_MISSING, 0)
    complete = unpriced == 0 and len(rounded) == 1
    if complete:
        note = ("Every line is priced: an estimate for the components only. "
                "Shipping, taxes, tariffs and fees are not included.")
    elif unpriced:
        note = (f"PRICED SUBTOTAL ONLY: {unpriced} line(s) have no price, so "
                "this is not the total BOM cost. Shipping, taxes, tariffs and "
                "fees are not included either.")
    else:
        note = ("Several currencies: they are not added together, so there "
                "is no single total.")
    return {
        "lines": len(lines),
        "priced_lines": len(lines) - unpriced,
        "unpriced_lines": unpriced,
        "by_price_source": {s: sources.get(s, 0) for s in (
            PRICE_CURRENT, PRICE_CACHED, PRICE_MISSING)},
        "priced_but_stock_short": sum(
            1 for bl in lines
            if bl.priced and bl.stock_status in ("short", "out of stock")),
        "priced_subtotal": to_jsonable(rounded),
        "priced_lines_by_currency": priced_lines,
        "estimated_total": (to_jsonable(next(iter(rounded.values())))
                            if complete else None),
        "currency": next(iter(rounded)) if len(rounded) == 1 else None,
        "pricing_complete": complete,
        "note": note,
    }


#: The file's columns, in order.
BUDGET_HEADERS = (
    "Line", "Part Number", "Manufacturer", "Description",
    "Reference Designator", "BOM Qty", "Required Qty", "Budget Qty",
    "Unit Price", "Price Break", "Extended Cost", "Currency", "Price Source",
    "Data Retrieved", "Distributor", "Distributor SKU", "Packaging",
    "Matched MPN", "MPN Basis", "Stock", "Stock Status", "Notes")

_NUMERIC = {"BOM Qty", "Required Qty", "Budget Qty", "Unit Price",
            "Price Break", "Extended Cost", "Stock"}


def _row(bl: BudgetLine) -> list[Any]:
    def number(value: Any) -> str:
        return "" if value is None else (
            format(value, "f") if isinstance(value, Decimal) else str(value))

    cost = "" if bl.extended_cost is None else format(
        bl.extended_cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        "f")
    return [bl.line_id, bl.part_number, bl.manufacturer, bl.description,
            ", ".join(bl.designators), bl.bom_quantity, bl.required_qty,
            number(bl.budget_qty), number(bl.unit_price),
            number(bl.break_quantity), cost, bl.currency, bl.price_source,
            bl.retrieved_at, bl.distributor, bl.sku, ", ".join(bl.packaging),
            bl.matched_mpn, bl.mpn_basis, number(bl.stock), bl.stock_status,
            " | ".join(bl.notes)]


#: The ``Line`` cell of each summary row at the end of the CSV.
SUMMARY_SUBTOTAL = "PRICED SUBTOTAL"
SUMMARY_PRICED = "PRICED LINES"
SUMMARY_UNPRICED = "UNPRICED LINES"
SUMMARY_TOTAL = "ESTIMATED TOTAL"


def _summary_rows(totals: dict[str, Any]) -> list[list[Any]]:
    """The rows that end the CSV, from ``budget_totals``.

    One PRICED SUBTOTAL row per currency (its amount in Extended Cost),
    the priced and unpriced line counts (in BOM Qty), and an ESTIMATED
    TOTAL row only when every line is priced in one currency. An
    incomplete subtotal says so in its own row: it is never labelled or
    placed as the total."""
    def row(label: str, **cells: Any) -> list[Any]:
        values = {"Line": label, **cells}
        return [values.get(h, "") for h in BUDGET_HEADERS]

    lines = totals["lines"]
    complete = totals["pricing_complete"]
    caveat = "" if complete else totals["note"]
    out = []
    for cur, amount in totals["priced_subtotal"].items():
        out.append(row(
            SUMMARY_SUBTOTAL,
            Description=(f"Sum of the priced lines in {cur} "
                         f"({totals['priced_lines_by_currency'][cur]} "
                         f"line(s))"
                         + ("" if complete else "; NOT the total BOM cost")),
            **{"Extended Cost": amount, "Currency": cur, "Notes": caveat}))
    out.append(row(SUMMARY_PRICED,
                   Description=f"Lines with a price, of {lines}",
                   **{"BOM Qty": totals["priced_lines"]}))
    out.append(row(SUMMARY_UNPRICED,
                   Description=f"Lines with no price, of {lines}; not in "
                               "any subtotal",
                   **{"BOM Qty": totals["unpriced_lines"]}))
    if complete:
        out.append(row(
            SUMMARY_TOTAL,
            Description="Every line priced: components only",
            **{"Extended Cost": totals["estimated_total"],
               "Currency": totals["currency"], "Notes": totals["note"]}))
    return out


def build_budget_csv(lines: Sequence[BudgetLine]) -> dict[str, Any]:
    """CSV text for the budget lines: ``text``, ``headers``, ``rows`` (the
    line rows), ``summary_rows``, ``totals`` and ``warnings``.

    The line rows come first, then a blank line and the summary rows
    (``_summary_rows``): the priced subtotal per currency, the priced and
    unpriced counts, and the estimated total only when every line,
    fitted and unresolved, is priced in one currency. The same text rules
    as the Purchasing BOM: a cell a spreadsheet would run as a formula gets
    a leading apostrophe, and values Excel would reinterpret are counted,
    not changed."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(BUDGET_HEADERS)
    neutralized = 0
    reinterpreted: Counter[str] = Counter()

    def write(values: Sequence[Any]) -> None:
        nonlocal neutralized
        out = []
        for header, value in zip(BUDGET_HEADERS, values):
            if header in _NUMERIC:
                out.append(str(value))
                continue
            text, changed = neutralize_formula(str(value))
            neutralized += changed
            if header in ("Part Number", "Matched MPN", "Distributor SKU") \
                    and _excel_reinterprets(text):
                reinterpreted[header] += 1
            out.append(text)
        writer.writerow(out)

    for bl in lines:
        write(_row(bl))
    totals = budget_totals(lines)
    summary = _summary_rows(totals)
    writer.writerow([])
    for values in summary:
        write(values)
    warnings: list[str] = []
    if neutralized:
        warnings.append(
            f"{neutralized} cell(s) began with =, +, - or @ (or a tab or line "
            "break) and got a leading apostrophe so a spreadsheet does not "
            "run them as formulas; no other value was changed")
    if reinterpreted:
        warnings.append(
            "Excel opening this file directly would reinterpret some values: "
            + ", ".join(f"{name} {n} cell(s)"
                        for name, n in reinterpreted.items())
            + ". Import those columns as Text; the file holds the exact "
            "values")
    missing = [bl for bl in lines if not bl.priced]
    if missing:
        warnings.append(
            f"{len(missing)} line(s) have no price and are not in the "
            "subtotal: "
            + _some([bl.part_number or ", ".join(bl.designators)
                     for bl in missing]))
    return {"text": buffer.getvalue(), "headers": list(BUDGET_HEADERS),
            "rows": len(lines), "summary_rows": len(summary),
            "totals": totals, "warnings": warnings}
