# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Purchase lines: every BOM line with what to buy, where, and why.

``build_purchase_lines`` turns the enriched BOM (``enrich_bom``, through the
analysis the purchasing tools build) into one ``PurchaseLine`` per BOM line.
It reads nothing and writes nothing.

No line is ever dropped. A fitted line a distributor cannot supply is kept,
``blocked``, with every reason; an unresolved component is a line of its own;
a line is never split, never bought in part, and never filled with another
manufacturer's part.

Each distributor's offers are decided by the quantity engine
(``decide_quantity``). The line goes to the preferred distributor (DigiKey by
default) when it can supply the requirement, to the other, marked
``FALLBACK_DISTRIBUTOR``, when it cannot, and to the cheaper one under
``prefer="lowest"``. If neither can supply the requirement the line is
blocked and keeps the quantity it would need.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from .purchasing_config import Settings
from .purchasing_model import (
    BomLine,
    Lifecycle,
    PriceBreak,
    PurchaseLine,
    PurchasingStatus,
    QuantityDecision,
    ReviewReason,
    SupplierOffer,
    derive_status,
    to_decimal,
    to_jsonable,
)
from .purchasing_matching import order as order_candidates
from .purchasing_quantity import decide_quantity
from .purchasing_review import lifecycle_reasons, make_reason

__all__ = ["build_purchase_lines", "offers_from_entry", "summarize"]

_ACCESS = ("unavailable", "error", "not_checked")
_ORDER = ("digikey", "mouser")


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def offers_from_entry(name: str, found: dict[str, Any]) -> list[SupplierOffer]:
    """The ``SupplierOffer`` s of one matched distributor entry."""
    lifecycle = Lifecycle(
        status=found.get("lifecycle"), raw=found.get("lifecycle_text"),
        discontinued=found.get("lifecycle_discontinued"),
        end_of_life=found.get("lifecycle_end_of_life"),
        last_buy_date=found.get("last_buy_date"))
    out = []
    for raw in found.get("offers") or []:
        breaks = []
        for entry in raw.get("price_breaks") or []:
            price = to_decimal(entry.get("unit_price"))
            quantity = _int(entry.get("quantity"))
            if quantity is not None and price is not None:
                breaks.append(PriceBreak(quantity, price))
        fee = to_decimal(raw.get("reeling_fee"))
        out.append(SupplierOffer(
            distributor=name, sku=str(raw.get("sku") or ""),
            packaging=tuple(raw.get("packaging") or ()),
            currency=raw.get("currency"), stock=_int(raw.get("stock")),
            moq=_int(raw.get("moq")), multiple=_int(raw.get("multiple")),
            standard_package=_int(raw.get("standard_package")),
            max_order_qty=_int(raw.get("max_order_qty")),
            price_breaks=tuple(breaks),
            reeling_fee=fee if fee is not None and fee > 0 else None,
            lifecycle=lifecycle,
            manufacturer=str(found.get("manufacturer") or ""),
            manufacturer_match=str(found.get("manufacturer_match") or "exact"),
            retrieved_at=found.get("retrieved_at"),
            cached=bool(found.get("cached")),
            product_url=found.get("product_url")))
    return out


class _Option:
    """What one distributor can do for one line."""

    def __init__(self, name: str, found: dict[str, Any], required: int,
                 settings: Settings):
        self.name = name
        self.status = str(found.get("status") or "")
        self.reason = str(found.get("reason") or "")
        self.found = found
        self.decisions: list[tuple[SupplierOffer, QuantityDecision]] = []
        self.lifecycle: Optional[Lifecycle] = None
        self.best: Optional[tuple[SupplierOffer, QuantityDecision]] = None
        if self.status != "matched":
            return
        offers = offers_from_entry(name, found)
        if offers:
            self.lifecycle = offers[0].lifecycle
        else:
            self.lifecycle = Lifecycle(
                status=found.get("lifecycle"), raw=found.get("lifecycle_text"),
                discontinued=found.get("lifecycle_discontinued"),
                end_of_life=found.get("lifecycle_end_of_life"),
                last_buy_date=found.get("last_buy_date"))
        policy = settings.policy()
        for offer in offers:
            self.decisions.append(
                (offer, decide_quantity(offer, required, policy)))
        feasible = [(o, d) for o, d in self.decisions if d.feasible]
        if feasible:
            self.best = min(feasible, key=lambda od: (
                od[1].extended_cost, od[1].purchase_qty, od[0].sku))

    @property
    def matched(self) -> bool:
        return self.status == "matched"

    @property
    def feasible(self) -> bool:
        return self.best is not None

    def why_not(self) -> str:
        """One sentence: why this distributor cannot supply the line."""
        if not self.matched:
            return f"{self.name}: {self.reason or self.status}"
        if not self.decisions:
            return f"{self.name} lists the part with no orderable number"
        texts = sorted({d.explanation for _, d in self.decisions})
        return f"{self.name}: " + "; ".join(texts)

    def reference(self) -> Optional[QuantityDecision]:
        """The decision to show for a line this option cannot supply."""
        stock = [d for _, d in self.decisions
                 if d.shortfall in ("out_of_stock", "insufficient_stock",
                                    "purchase_limit")]
        pool = stock or [d for _, d in self.decisions]
        return min(pool, key=lambda d: d.floor_qty) if pool else None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"distributor": self.name,
                               "status": self.status}
        if self.best is not None:
            offer, d = self.best
            out.update(sku=offer.sku, purchase_qty=d.purchase_qty,
                       unit_price=d.unit_price, extended_cost=d.extended_cost,
                       currency=offer.currency, rule=d.rule)
        else:
            out["reason"] = self.why_not()
        return out


def _conflicts(entry: dict[str, Any],
               inconsistencies: list[dict[str, Any]]) -> list[str]:
    maker = str(entry.get("manufacturer") or "").casefold()
    number = str(entry.get("part_number") or "").casefold()
    names = {str(d).casefold() for d in entry.get("designators") or []}
    out = []
    for item in inconsistencies:
        issue = str(item.get("issue") or "an inconsistency")
        if str(item.get("part_number") or "").casefold() == number and (
                "manufacturers" not in item
                or maker in {str(m).casefold()
                             for m in item.get("manufacturers") or []}) and (
                not item.get("manufacturer")
                or str(item["manufacturer"]).casefold() == maker):
            out.append(issue)
        elif item.get("designator") and \
                str(item["designator"]).casefold() in names:
            out.append(issue)
    return out


def _age_hours(stamp: Any, now: datetime) -> Optional[Decimal]:
    try:
        then = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return Decimal((now - then).total_seconds()) / 3600


def _sourcing_reasons(options: list[_Option], settings: Settings
                      ) -> list[ReviewReason]:
    """Why no distributor can supply the line."""
    sev = settings.severity
    out: list[ReviewReason] = []
    seen: set[str] = set()

    def add(code: str, message: str) -> None:
        if code not in seen:
            seen.add(code)
            out.append(make_reason(code, message, sev))

    matched = [o for o in options if o.matched]
    access = [o for o in options if o.status in _ACCESS]
    for o in options:
        if o.status == "not_found":
            add("MPN_NOT_FOUND", o.why_not())
        elif o.status == "manufacturer_mismatch":
            add("MANUFACTURER_MISMATCH", o.why_not())
        elif o.status == "ambiguous":
            add("AMBIGUOUS_MANUFACTURER", o.why_not())
    if matched:
        by_code: dict[str, list[str]] = {}
        stock_short = []
        for o in matched:
            if not o.decisions:
                by_code.setdefault("MISSING_PRICE", []).append(o.why_not())
                continue
            for _, d in o.decisions:
                if d.shortfall in ("out_of_stock", "insufficient_stock"):
                    stock_short.append((o, d))
                elif d.shortfall == "purchase_limit":
                    by_code.setdefault("PURCHASE_LIMIT_EXCEEDED", []).append(
                        f"{o.name}: {d.explanation}")
                elif d.shortfall == "no_price":
                    by_code.setdefault("MISSING_PRICE", []).append(
                        f"{o.name}: {d.explanation}")
                elif d.shortfall == "no_currency":
                    by_code.setdefault("MISSING_CURRENCY", []).append(
                        f"{o.name}: {d.explanation}")
        if stock_short:
            everyone_empty = all(d.shortfall == "out_of_stock"
                                 for _, d in stock_short)
            detail = "; ".join(sorted({f"{o.name}: {d.explanation}"
                                       for o, d in stock_short}))
            add("OUT_OF_STOCK" if everyone_empty else "INSUFFICIENT_STOCK",
                detail)
        for code, texts in by_code.items():
            add(code, "; ".join(sorted(set(texts))))
    if access and not matched and not seen:
        add("NO_DISTRIBUTOR_DATA", "; ".join(o.why_not() for o in access))
    elif access:
        add("DISTRIBUTOR_UNAVAILABLE", "; ".join(o.why_not() for o in access))
    if not out:
        add("NO_DISTRIBUTOR_DATA", "no distributor was asked about this line")
    return out


def _line_from_entry(position: int, entry: dict[str, Any],
                     settings: Settings) -> BomLine:
    return BomLine(
        line_id=f"L{position + 1:03d}", kind="fitted",
        manufacturer=str(entry.get("manufacturer") or ""),
        part_number=str(entry.get("part_number") or ""),
        description=str(entry.get("description") or ""),
        designators=tuple(str(d) for d in entry.get("designators") or ()),
        footprints=tuple(str(f) for f in entry.get("footprints") or ()),
        bom_quantity=int(entry.get("quantity_per_board") or 0))


def _decide_line(bom: BomLine, entry: dict[str, Any], required: int,
                 settings: Settings, conflicts: list[str],
                 incomplete: bool, now: datetime) -> PurchaseLine:
    sev = settings.severity
    options = [_Option(name, found, required, settings)
               for name, found in (entry.get("distributors") or {}).items()
               if name in settings.enabled]
    by_name = {o.name: o for o in options}
    feasible = [o for o in options if o.feasible]
    reasons: list[ReviewReason] = []
    chosen: Optional[_Option] = None

    prefer = settings.prefer
    if feasible:
        if prefer in _ORDER:
            first = by_name.get(prefer)
            if first is not None and first.feasible:
                chosen = first
            else:
                chosen = feasible[0] if len(feasible) == 1 else min(
                    feasible, key=lambda o: _ORDER.index(o.name)
                    if o.name in _ORDER else 9)
                why = first.why_not() if first is not None \
                    else f"{prefer} is not enabled"
                reasons.append(make_reason(
                    "FALLBACK_DISTRIBUTOR",
                    f"bought at {chosen.name}: {why}", sev))
        else:
            currencies = {o.best[0].currency for o in feasible}
            if len(currencies) == 1:
                chosen = min(feasible, key=lambda o: (
                    o.best[1].extended_cost, o.best[1].purchase_qty,
                    o.best[0].sku))
            else:
                chosen = min(feasible, key=lambda o: _ORDER.index(o.name)
                             if o.name in _ORDER else 9)
                reasons.append(make_reason(
                    "CURRENCY_MIXED",
                    "the distributors price in "
                    + ", ".join(sorted(str(c) for c in currencies))
                    + "; no cost comparison was made, so "
                    + chosen.name + " was taken in preference order", sev))

    offer: Optional[SupplierOffer] = None
    decision: Optional[QuantityDecision] = None
    variant: Optional[dict[str, Any]] = None
    if chosen is not None:
        offer, decision = chosen.best
        variant = chosen.found.get("mpn_variant")
        if variant:
            reasons.append(make_reason(
                "APPROVED_MPN_VARIANT",
                f"the BOM says {bom.part_number!r}; {variant['candidate_mpn']!r}"
                f" is ordered ({variant['label']}, {variant['meaning']}), "
                f"authorized by {variant['approval']['method']}. The "
                "schematic's number is unchanged", sev))
        for flag in decision.flags:
            reasons.append(make_reason(flag, None, sev))
        if variant and variant.get("manufacturer_differs"):
            reasons.append(make_reason(
                "MANUFACTURER_PARTIAL_MATCH",
                f"the BOM says {variant['bom_manufacturer']!r}; "
                f"{offer.distributor} lists the approved variant under "
                f"{variant['distributor_manufacturer']!r}. The user approved "
                "that pair; confirm it is one company", sev))
        elif offer.manufacturer_match == "partial":
            reasons.append(make_reason(
                "MANUFACTURER_PARTIAL_MATCH",
                f"the BOM says {bom.manufacturer!r}, {offer.distributor} "
                f"says {offer.manufacturer!r}", sev))
        ratio = settings.large_surplus_ratio
        if ratio is not None and decision.surplus_ratio > ratio:
            reasons.append(make_reason(
                "LARGE_SURPLUS",
                f"{decision.purchase_qty} bought for {required} needed "
                f"({decision.surplus_ratio.quantize(Decimal('0.1'))} times; "
                f"warning above {ratio}). {decision.explanation}", sev))
        if offer.cached:
            age = _age_hours(offer.retrieved_at, now)
            if age is not None and age > settings.stale_pricing_hours:
                reasons.append(make_reason(
                    "STALE_PRICING",
                    f"the {offer.distributor} figures were read "
                    f"{age.quantize(Decimal('0.1'))} hours ago", sev))
        others = [o for o in options if o is not chosen
                  and o.status in _ACCESS]
        if others:
            reasons.append(make_reason(
                "DISTRIBUTOR_UNAVAILABLE",
                "; ".join(o.why_not() for o in others), sev))
    else:
        reasons.extend(_sourcing_reasons(options, settings))
        close = entry.get("close_matches") or []
        waiting = [c for c in close
                   if not c.get("used") and not c.get("rejected")]
        turned_down = [c for c in close if c.get("rejected")]
        if turned_down and not waiting:
            reasons.append(make_reason(
                "CLOSE_MATCH_REJECTED",
                f"{len(turned_down)} close candidate(s) were rejected for "
                "this project; they are not used and not asked about again",
                sev))
        if waiting:
            reasons.append(make_reason(
                "CLOSE_MATCH_AVAILABLE",
                f"{len(waiting)} close candidate(s) are listed, the most "
                f"likely a {waiting[0]['label']}; none is used until the "
                "user approves it", sev))

    lifecycles = {o.name: o.lifecycle for o in options if o.matched}
    reasons.extend(lifecycle_reasons(lifecycles, sev))
    if conflicts:
        reasons.append(make_reason(
            "BOM_CONFLICT", "; ".join(sorted(set(conflicts))), sev))
    if incomplete:
        reasons.append(make_reason("BOM_INCOMPLETE", None, sev))

    reference = decision
    if reference is None:
        refs = [r for r in (o.reference() for o in options if o.matched) if r]
        reference = min(refs, key=lambda d: d.floor_qty) if refs else None
    replacement = next((str(o.found.get("suggested_replacement"))
                        for o in options
                        if o.found.get("suggested_replacement")), None)
    alternatives = tuple(o.summary() for o in options if o is not chosen)
    shown = chosen.lifecycle if chosen is not None else next(
        (o.lifecycle for o in options if o.matched and o.lifecycle), None)
    words = (shown.raw or shown.status or "") if shown else ""
    return PurchaseLine(
        line=bom, required_qty=required, offer=offer, decision=reference,
        status=derive_status(reasons), reasons=tuple(reasons),
        alternatives=alternatives, suggested_replacement=replacement,
        lifecycle=words,
        matched_mpn=(variant or {}).get("candidate_mpn", ""),
        mpn_variant=variant)


def _unresolved_line(position: int, entry: dict[str, Any],
                     settings: Settings, incomplete: bool) -> PurchaseLine:
    text = str(entry.get("reason") or "no usable Manufacturer and Part Number")
    sev = settings.severity
    reasons = [make_reason("UNRESOLVED_COMPONENT", text, sev)]
    if re.search(r"manufacturer", text, re.I):
        reasons.append(make_reason("MISSING_MANUFACTURER", None, sev))
    if re.search(r"part number", text, re.I):
        reasons.append(make_reason("MISSING_MPN", None, sev))
    if incomplete:
        reasons.append(make_reason("BOM_INCOMPLETE", None, sev))
    bom = BomLine(
        line_id=f"U{position + 1:03d}", kind="unresolved",
        manufacturer=str(entry.get("manufacturer") or ""),
        part_number=str(entry.get("part_number") or ""),
        description=str(entry.get("description") or ""),
        designators=(str(entry.get("designator") or ""),),
        footprints=((str(entry["footprint"]),) if entry.get("footprint")
                    else ()),
        bom_quantity=1, data_problems=(text,))
    return PurchaseLine(
        line=bom, required_qty=settings.assemblies, offer=None,
        decision=None, status=derive_status(reasons), reasons=tuple(reasons))


def _not_fitted_line(position: int, entry: dict[str, Any]) -> PurchaseLine:
    bom = BomLine(
        line_id=f"D{position + 1:03d}", kind="not_fitted",
        manufacturer=str(entry.get("manufacturer") or ""),
        part_number=str(entry.get("part_number") or ""),
        description=str(entry.get("description") or ""),
        designators=tuple(str(d) for d in entry.get("designators") or ()),
        footprints=tuple(str(f) for f in entry.get("footprints") or ()),
        bom_quantity=0)
    return PurchaseLine(line=bom, required_qty=0, offer=None, decision=None,
                        status=PurchasingStatus.NOT_FITTED)


def build_purchase_lines(analysis: dict[str, Any], settings: Settings,
                         dnp_lines: Optional[list[dict[str, Any]]] = None,
                         now: Optional[datetime] = None) -> dict[str, Any]:
    """Every BOM line as a ``PurchaseLine``, with totals and warnings.

    ``analysis`` is what ``part_purchasing_analyze`` returns, asked with
    ``build_quantity`` equal to ``settings.assemblies``. ``dnp_lines`` are
    not-fitted parts to list (required 0, not looked up).
    """
    now = now or datetime.now(timezone.utc)
    incomplete = not analysis.get("bom_complete", True)
    inconsistencies = [i for i in analysis.get("inconsistencies") or []
                       if isinstance(i, dict)]
    lines: list[PurchaseLine] = []
    for position, entry in enumerate(analysis.get("lines") or []):
        bom = _line_from_entry(position, entry, settings)
        required = bom.bom_quantity * settings.assemblies
        lines.append(_decide_line(bom, entry, required, settings,
                                  _conflicts(entry, inconsistencies),
                                  incomplete, now))
    for position, entry in enumerate(analysis.get("unresolved") or []):
        lines.append(_unresolved_line(position, entry, settings, incomplete))
    for position, entry in enumerate(dnp_lines or []):
        lines.append(_not_fitted_line(position, entry))

    # One distributor number on two lines: the cart takes one line per
    # number and merging would change the price break.
    owners: dict[tuple[str, str], list[int]] = {}
    for index, pl in enumerate(lines):
        if pl.offer is not None:
            owners.setdefault((pl.offer.distributor, pl.offer.sku.casefold()),
                              []).append(index)
    for (name, sku), where in owners.items():
        if len(where) > 1:
            names = ", ".join(lines[i].line.line_id for i in where)
            for i in where:
                reasons = (*lines[i].reasons, make_reason(
                    "DUPLICATE_SKU", f"{name} number {sku} is on lines "
                    f"{names}", settings.severity))
                lines[i] = dataclasses.replace(
                    lines[i], reasons=reasons, status=derive_status(reasons))

    return {
        "lines": lines,
        "summary": summarize(lines, settings),
        "totals": _totals(lines),
        "settings": settings.report(),
        "warnings": [*settings.warnings,
                     *(analysis.get("close_match_notes") or [])],
        "close_matches": close_match_table(analysis),
    }


def close_match_table(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Every close candidate of the whole BOM in one list, most likely
    first, each with the line it belongs to. Advice only."""
    rows = []
    for position, entry in enumerate(analysis.get("lines") or []):
        for cand in entry.get("close_matches") or []:
            rows.append({
                **cand, "line_id": f"L{position + 1:03d}",
                "bom_manufacturer": entry.get("manufacturer", ""),
                "designators": list(entry.get("designators") or []),
                "required_quantity": entry.get("required_quantity")})
    return order_candidates(rows)


def summarize(lines: list[PurchaseLine], settings: Settings
              ) -> dict[str, Any]:
    counts = {s.value: 0 for s in PurchasingStatus}
    for pl in lines:
        counts[pl.status.value] += 1
    fitted = [pl for pl in lines if pl.line.kind == "fitted"]
    return {
        "rows": len(lines),
        "fitted_lines": len(fitted),
        "unresolved_components": sum(
            1 for pl in lines if pl.line.kind == "unresolved"),
        "not_fitted_rows": counts["not_fitted"],
        "bom_quantity": sum(pl.line.bom_quantity for pl in fitted),
        "required_quantity": sum(pl.required_qty for pl in fitted),
        "assemblies": settings.assemblies,
        "by_status": counts,
    }


def _totals(lines: list[PurchaseLine]) -> dict[str, Any]:
    """Money per currency and per distributor. Never added across
    currencies."""
    by_currency: dict[str, dict[str, Any]] = {}
    by_distributor: dict[str, dict[str, Any]] = {}
    for pl in lines:
        if pl.offer is None or pl.decision is None \
                or pl.decision.extended_cost is None:
            continue
        cur = pl.offer.currency or "?"
        cost = pl.decision.extended_cost
        for bucket, key in ((by_currency, cur),
                            (by_distributor, f"{pl.offer.distributor} {cur}")):
            row = bucket.setdefault(key, {"lines": 0, "extended_cost": Decimal(0)})
            row["lines"] += 1
            row["extended_cost"] += cost
    for bucket in (by_currency, by_distributor):
        for row in bucket.values():
            row["extended_cost"] = row["extended_cost"].quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "by_currency": to_jsonable(by_currency),
        "by_distributor": to_jsonable(by_distributor),
        "combined": (None if len(by_currency) != 1 else
                     to_jsonable(next(iter(by_currency.values()))
                                 ["extended_cost"])),
        "note": ("Component prices as the distributors state them. "
                 "Shipping, taxes, tariffs and fees are not included."
                 + ("" if len(by_currency) <= 1 else
                    " Several currencies: they are not added together.")),
    }
