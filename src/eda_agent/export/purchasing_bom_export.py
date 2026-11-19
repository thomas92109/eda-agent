# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The Purchasing BOM as a CSV: pure formatting, no Altium, no disk.

One row per purchase line (``purchasing_lines.build_purchase_lines``): every
fitted BOM line, every unresolved component, and, only when asked, the
not-fitted parts. A line that cannot be bought is a row too, with its status
and every reason; nothing is hidden because it cannot be ordered.

It is a different file from the Standard BOM (``purchasing_bom_csv``), which
stays as it is: one assembly, five columns, no distributor. The text rules
are the Standard BOM's own: the ``csv`` module writes it, a cell a
spreadsheet would run as a formula gets a leading apostrophe (distributor
text included, since it is not ours), and values Excel would reinterpret are
counted, not changed.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional, Sequence

from .purchasing_bom_csv import (
    _EXCEL_CELL_LIMIT,
    ColumnSelectionError,
    _excel_reinterprets,
    _some,
    neutralize_formula,
)
from .purchasing_model import PurchaseLine, PurchasingStatus, Severity

__all__ = [
    "COLUMNS",
    "DEFAULT_COLUMNS",
    "DELIMITER",
    "CLOSE_MATCH_HEADERS",
    "build_close_matches_csv",
    "build_purchasing_bom_csv",
    "dnp_rows",
    "resolve_columns",
]

#: Column identifier -> header.
COLUMNS: dict[str, str] = {
    "bom_quantity": "BOM Qty",
    "required_qty": "Required Qty",
    "purchase_qty": "Purchase Qty",
    "surplus": "Surplus",
    "part_number": "Part Number",
    "matched_mpn": "Matched MPN",
    "mpn_match": "MPN Match",
    "mpn_approval": "MPN Approval",
    "mpn_audit": "MPN Approval Audit",
    "mpn_method": "MPN Match Method",
    "mpn_source": "MPN Approval Source",
    "mpn_evidence": "MPN Evidence",
    "manufacturer": "Manufacturer",
    "description": "Description",
    "reference_designators": "Reference Designator",
    "footprint": "Footprint",
    "distributor": "Distributor",
    "distributor_sku": "Distributor SKU",
    "packaging": "Packaging",
    "unit_price": "Unit Price",
    "extended_cost": "Extended Cost",
    "currency": "Currency",
    "stock": "Stock",
    "restock_date": "Next Restock Date",
    "restock_qty": "Next Restock Qty",
    "restock_covered": "Full Qty Available By",
    "restock_detail": "Restock Detail",
    "lifecycle": "Lifecycle",
    "purchasing_status": "Purchasing Status",
    "review_reasons": "Review Reasons",
    "review_detail": "Review Detail",
    "quantity_rule": "Quantity Rule",
    "price_break": "Price Break",
    "retrieved_at": "Data Retrieved",
}

DEFAULT_COLUMNS: tuple[str, ...] = (
    "required_qty", "purchase_qty", "surplus", "part_number", "matched_mpn",
    "mpn_approval", "mpn_audit", "manufacturer", "description",
    "reference_designators", "distributor", "distributor_sku",
    "unit_price", "extended_cost", "stock", "restock_date", "restock_qty",
    "restock_covered", "lifecycle", "purchasing_status", "review_reasons",
    "quantity_rule")

#: Between the reasons of one row.
DELIMITER = " | "

_NUMBERS = {"bom_quantity", "required_qty", "purchase_qty", "surplus",
            "unit_price", "extended_cost", "stock", "price_break",
            "restock_qty"}
_RANK = {Severity.BLOCKING: 0, Severity.REVIEW: 1, Severity.INFO: 2}
#: ISO dates (yyyy-mm-dd). Excel showing one as a date is the point, not a
#: corruption, so they are left out of the "Excel would reinterpret" count.
_DATES = {"restock_date", "restock_covered"}


def _valid() -> str:
    return ", ".join(COLUMNS)


def resolve_columns(columns: Optional[Sequence[str]],
                    default: Optional[Sequence[str]] = None,
                    include_dnp: bool = False) -> list[str]:
    """The ordered column identifiers to write.

    ``None`` is ``default`` (the configured columns) or the built-in set.
    Anything else must be a non-empty list of the identifiers in
    ``COLUMNS``, each at most once, written in the order given. An unknown,
    repeated or empty choice is refused; nothing is corrected or added.
    With ``include_dnp`` the list must name ``purchasing_status`` so a
    not-fitted row cannot be taken for a part to buy.
    """
    from_config = columns is None and default is not None
    source = columns if columns is not None else (
        default if default is not None else DEFAULT_COLUMNS)
    label = "the configured purchasing_columns" if from_config else "columns"
    if isinstance(source, (str, bytes)) \
            or not isinstance(source, (list, tuple)):
        raise ColumnSelectionError(
            f"{label} must be a list of column identifiers, not "
            f"{type(source).__name__}; they are: {_valid()}")
    if not source:
        raise ColumnSelectionError(
            f"{label} is empty. Name at least one of: {_valid()}")
    unknown = [c for c in source if not isinstance(c, str)
               or c not in COLUMNS]
    repeated = sorted({c for c in source if isinstance(c, str)
                       and list(source).count(c) > 1})
    problems = []
    if unknown:
        problems.append("unknown: " + ", ".join(repr(c) for c in unknown))
    if repeated:
        problems.append("listed more than once: "
                        + ", ".join(repr(c) for c in repeated))
    if problems:
        raise ColumnSelectionError(
            f"{label} refused ({'; '.join(problems)}). The identifiers are: "
            + _valid())
    chosen = list(source)
    if include_dnp and "purchasing_status" not in chosen:
        raise ColumnSelectionError(
            "include_dnp=True needs 'purchasing_status' in columns, so a "
            "not-fitted row cannot be mistaken for a part to buy. Columns "
            "are never added for you.")
    return chosen


def dnp_rows(dnp_bom: dict[str, Any], not_fitted: Sequence[str]
             ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """The not-fitted parts as lines to list, from the second BOM read.

    Each is the line of ``dnp_bom`` cut down to the not-fitted designators on
    it. Returns the lines and, for the not-fitted designators that have no
    line, why: kept out of the BOM by Component Kind, unresolved, or in none
    of Altium's lists.
    """
    wanted = {str(d) for d in not_fitted}
    covered: set[str] = set()
    rows = []
    for line in dnp_bom.get("lines") or []:
        mine = [str(d) for d in line.get("designators") or []
                if str(d) in wanted]
        if mine:
            covered.update(mine)
            rows.append({**line, "designators": mine})
    by_kind = {str(d) for d in dnp_bom.get("not_in_bom") or []}
    unresolved = {str(u.get("designator", ""))
                  for u in dnp_bom.get("unresolved") or []}
    missing: dict[str, list[str]] = {"not_in_bom": [], "unresolved": [],
                                     "unclassified": []}
    for d in not_fitted:
        d = str(d)
        if d in covered:
            continue
        key = ("not_in_bom" if d in by_kind else
               "unresolved" if d in unresolved else "unclassified")
        missing[key].append(d)
    return rows, missing


def _money(value: Optional[Decimal], places: str = "0.0001") -> str:
    if value is None:
        return ""
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP),
                  "f")


def _ordered(pl: PurchaseLine):
    return sorted(pl.reasons, key=lambda r: (_RANK[r.severity], r.code))


def _weeks(value: Any) -> str:
    return format(value, "g") if isinstance(value, (int, float)) else ""


def _restock_cells(pl: PurchaseLine) -> dict[str, Any]:
    """When stock may come back, for a line that is short of it. Only what a
    distributor states: its incoming lots (Mouser) and the manufacturer's
    lead time. DigiKey's catalogue gives no incoming lots, so for it the
    detail says so and quotes the lead time. Blank for a line that can be
    bought or that has no matched distributor."""
    rows = pl.restock
    if not rows:
        return {"restock_date": "", "restock_qty": "", "restock_covered": "",
                "restock_detail": ""}
    dated = [(lot["date"], lot["quantity"], r["distributor"])
             for r in rows for lot in r["incoming"] if lot["date"]]
    first = min(dated) if dated else None
    any_lots = any(r["incoming"] for r in rows)
    real = [r["covers_by"] for r in rows
            if r["covers_by"] and r["covers_by"] != "date not stated"]
    if real:
        covered = min(real)
    elif any(r["covers_by"] for r in rows):
        covered = "date not stated"
    elif any_lots:
        covered = "not covered by the incoming stock listed"
    else:
        covered = "no incoming stock listed"
    parts = []
    for r in rows:
        text = [f"{r['distributor']}: {r['in_stock']} in stock"]
        if r["incoming"]:
            text.append("incoming " + ", ".join(
                f"{lot['quantity']} on {lot['date'] or 'a date not stated'}"
                for lot in r["incoming"]))
            if r["covers_by"]:
                text.append(f"covers the {r['required']} needed by "
                            f"{r['covers_by']}")
            else:
                text.append(f"the incoming lots do not cover the "
                            f"{r['required']} needed")
        else:
            text.append("no incoming stock listed")
        if _weeks(r.get("lead_time_weeks")):
            text.append("manufacturer lead time "
                        f"{_weeks(r['lead_time_weeks'])} weeks")
        if r.get("manufacturer_stock"):
            text.append(f"manufacturer stock {r['manufacturer_stock']}")
        if r.get("retrieved_at"):
            text.append(f"read {str(r['retrieved_at'])[:10]}")
        parts.append("; ".join(text))
    return {"restock_date": first[0] if first else "",
            "restock_qty": first[1] if first else "",
            "restock_covered": covered,
            "restock_detail": " | ".join(parts)}


def _method(v: Optional[dict]) -> str:
    """How the ordered number was matched to the BOM's."""
    if not v:
        return ""
    alias = v.get("alias") or {}
    if alias:
        return f"{alias['method']} {alias['rule']} ({v['label']})"
    return f"{v['label']}: {v['meaning']}"


def _source(v: Optional[dict]) -> str:
    """Who or what authorized it: a decision, a call, or a config option."""
    a = (v or {}).get("approval") or {}
    if not v:
        return ""
    return (f"{a['method']}: {a['source']}" if a.get("source")
            else str(a.get("method", "")))


def _evidence(v: Optional[dict]) -> str:
    alias = (v or {}).get("alias") or {}
    return " || ".join(alias.get("evidence") or [])


def _reason_token(pl: PurchaseLine, reason: Any) -> str:
    """A reason's code. An applied alias carries the two numbers with it, so
    the difference is visible whatever columns the file has."""
    v = pl.mpn_variant
    if reason.code == "VERIFIED_MPN_ALIAS" and v:
        return (f"{reason.code} (BOM {v['original_mpn']} -> ordered "
                f"{v['candidate_mpn']})")
    return reason.code


def _audit(pl: PurchaseLine) -> str:
    """Who authorized the number ordered, and what it replaced."""
    v = pl.mpn_variant
    if not v:
        return ""
    a = v.get("approval") or {}
    parts = [f"original {v['original_mpn']} ({v['bom_manufacturer']})",
             f"matched {v['candidate_mpn']} ({v['distributor_manufacturer']})",
             str(v["label"])]
    if v.get("alias"):
        parts.append(f"matching method: {_method(v)}")
        parts.append(f"approval source: {_source(v)}")
    parts.append(f"authorized by {a.get('method', '?')}")
    if a.get("decided_at"):
        parts.append(f"decided {a['decided_at']}")
    if a.get("decision_id"):
        parts.append(f"decision {a['decision_id']}")
    if a.get("project_id"):
        parts.append(f"project {a['project_id']}")
    if a.get("method") == "this call only":
        parts.append("not remembered")
    if a.get("note"):
        parts.append(str(a["note"]))
    if v.get("alias"):
        parts.append(f"evidence: {_evidence(v)}")
    return "; ".join(parts)


def _cells(pl: PurchaseLine) -> dict[str, Any]:
    d, o = pl.decision, pl.offer
    bought = o is not None and d is not None and d.shortfall is None
    reasons = _ordered(pl)
    rule = ""
    if bought:
        rule = f"{d.rule}: {d.explanation}"
    elif pl.status is PurchasingStatus.NOT_FITTED:
        rule = "not fitted: nothing to buy"
    elif d is not None:
        rule = f"not bought: {d.explanation}"
    return {
        "bom_quantity": pl.line.bom_quantity,
        "required_qty": pl.required_qty,
        "purchase_qty": (d.purchase_qty if bought else
                         0 if pl.status is PurchasingStatus.NOT_FITTED else ""),
        "surplus": d.surplus if bought else "",
        "part_number": pl.line.part_number,
        "matched_mpn": pl.matched_mpn,
        "mpn_approval": ((pl.mpn_variant or {}).get("approval") or {}).get(
            "method", ""),
        "mpn_audit": _audit(pl),
        "mpn_method": _method(pl.mpn_variant),
        "mpn_source": _source(pl.mpn_variant),
        "mpn_evidence": _evidence(pl.mpn_variant),
        "mpn_match": (f"approved variant ({pl.mpn_variant['label']})"
                      if pl.mpn_variant else "exact" if bought else ""),
        "manufacturer": pl.line.manufacturer,
        "description": pl.line.description,
        "reference_designators": ", ".join(pl.line.designators),
        "footprint": ", ".join(pl.line.footprints),
        "distributor": o.distributor if bought else "",
        "distributor_sku": o.sku if bought else "",
        "packaging": ", ".join(o.packaging) if bought else "",
        "unit_price": format(d.unit_price, "f") if bought else "",
        "extended_cost": _money(d.extended_cost) if bought else "",
        "currency": (o.currency or "") if bought else "",
        "stock": o.stock if bought and o.stock is not None else "",
        **_restock_cells(pl),
        "lifecycle": pl.lifecycle,
        "purchasing_status": pl.status.value,
        "review_reasons": DELIMITER.join(_reason_token(pl, r)
                                         for r in reasons),
        "review_detail": DELIMITER.join(f"{r.code}: {r.message}"
                                        for r in reasons),
        "quantity_rule": rule,
        "price_break": d.break_quantity if bought else "",
        "retrieved_at": (o.retrieved_at or "") if bought else "",
    }


def build_purchasing_bom_csv(lines: Sequence[PurchaseLine],
                             columns: Optional[Sequence[str]] = None,
                             default_columns: Optional[Sequence[str]] = None,
                             include_dnp: bool = False,
                             bom_summary: Optional[dict[str, Any]] = None
                             ) -> dict[str, Any]:
    """CSV text for the purchase lines, with counts and warnings.

    Returns ``text`` (the CSV), ``columns``, ``headers``, ``rows``,
    ``counts`` and ``warnings``. ``bom_summary`` is Altium's own count
    (``fitted_components``, ``components``); with it the BOM Qty column is
    checked against it. Raises ``ColumnSelectionError`` for a column choice
    that cannot be honoured.
    """
    chosen = resolve_columns(columns, default_columns, include_dnp)
    rows = [pl for pl in lines
            if include_dnp or pl.status is not PurchasingStatus.NOT_FITTED]

    table: list[list[str]] = []
    neutralized = 0
    reinterpreted: Counter[str] = Counter()
    too_long = 0
    for pl in rows:
        cells = _cells(pl)
        out = []
        for column in chosen:
            value = cells[column]
            if column in _NUMBERS and not isinstance(value, str):
                out.append(str(value))
                continue
            text, changed = neutralize_formula(str(value))
            neutralized += changed
            if column not in _NUMBERS and column != "purchasing_status" \
                    and column not in _DATES and _excel_reinterprets(text):
                reinterpreted[COLUMNS[column]] += 1
            too_long += len(text) > _EXCEL_CELL_LIMIT
            out.append(text)
        table.append(out)

    headers = [COLUMNS[c] for c in chosen]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(table)

    variants = [pl for pl in rows if pl.mpn_variant]
    absent = [c for c in ("matched_mpn", "mpn_approval", "mpn_audit")
              if c not in chosen]
    if variants and absent:
        warnings_early = (
            f"{len(variants)} row(s) order an approved close variant of the "
            "BOM's part number, but this file lacks the column(s) "
            + ", ".join(absent) + ": add them so the number ordered and who "
            "authorized it are in the file")
    else:
        warnings_early = ""
    aliased = [pl for pl in rows if (pl.mpn_variant or {}).get("alias")]
    fitted = [pl for pl in lines if pl.line.kind == "fitted"]
    unresolved = [pl for pl in lines if pl.line.kind == "unresolved"]
    by_status = Counter(pl.status.value for pl in rows)
    counts = {
        "rows": len(rows),
        "fitted_lines": len(fitted),
        "unresolved_components": len(unresolved),
        "not_fitted_rows": sum(1 for pl in rows
                               if pl.status is PurchasingStatus.NOT_FITTED),
        "bom_quantity": sum(pl.line.bom_quantity for pl in fitted),
        "ready": by_status.get("ready", 0),
        "review": by_status.get("review", 0),
        "blocked": by_status.get("blocked", 0),
    }

    warnings: list[str] = [warnings_early] if warnings_early else []
    if aliased:
        warnings.append(
            f"{len(aliased)} row(s) order a VERIFIED MANUFACTURER ALIAS, not "
            "the BOM's own number (BOM -> ordered): "
            + "; ".join(
                f"{pl.line.part_number} -> {pl.matched_mpn} "
                f"({_source(pl.mpn_variant)})" for pl in aliased)
            + ". Each row's Review Reasons names it as VERIFIED_MPN_ALIAS; "
            "the MPN Approval Audit column holds the method and the "
            "manufacturer's evidence")
    if counts["blocked"]:
        warnings.append(
            f"{counts['blocked']} row(s) are BLOCKED and must not be ordered "
            "as they stand: "
            + _some([pl.line.part_number or ", ".join(pl.line.designators)
                     for pl in rows
                     if pl.status is PurchasingStatus.BLOCKED]))
    if counts["review"]:
        warnings.append(
            f"{counts['review']} row(s) need a person to look at them before "
            "they are ordered (status review)")
    if neutralized:
        warnings.append(
            f"{neutralized} cell(s) began with =, +, - or @ (or a tab or "
            "line break) and got a leading apostrophe so a spreadsheet does "
            "not run them as formulas; no other value was changed")
    if reinterpreted:
        warnings.append(
            "Excel opening this file directly would reinterpret some values "
            "(leading zeros dropped, long numbers, exponents, dates): "
            + ", ".join(f"{name} {n} cell(s)"
                        for name, n in reinterpreted.items())
            + ". Import those columns as Text (Data > From Text/CSV); the "
            "file itself holds the exact values")
    if too_long:
        warnings.append(f"{too_long} cell(s) are longer than "
                        f"{_EXCEL_CELL_LIMIT} characters; Excel cuts a cell "
                        "at 32767")
    if bom_summary:
        fitted_components = bom_summary.get("fitted_components")
        if isinstance(fitted_components, int):
            expected = fitted_components - len(unresolved)
            if counts["bom_quantity"] != expected:
                warnings.append(
                    f"CROSS-CHECK FAILED: the fitted lines add up to "
                    f"{counts['bom_quantity']} part(s) per assembly but "
                    f"Altium's counts say {expected}. Do not order from this "
                    "file until that is explained")
    return {"text": buffer.getvalue(), "columns": chosen, "headers": headers,
            "rows": len(rows), "counts": counts, "warnings": warnings}


#: The companion file of close part number candidates.
CLOSE_MATCH_HEADERS = (
    "Original MPN", "Candidate MPN", "Manufacturer", "Suffix Difference",
    "Qualifier", "What The Difference Means", "Distributor", "Stock",
    "Unit Price", "Currency", "Required Qty", "Order Qty", "Extended Price",
    "Priced Packaging", "Priced SKU", "Pricing Note", "Distributor SKU",
    "Approval",
    "Approval Method", "Decision Date", "Decision ID", "Evidence", "Uncertainty", "Line", "Reference Designator",
    "BOM Manufacturer", "Matching Method", "Approval Source",
    "Manufacturer Evidence")


def build_close_matches_csv(close_matches: Sequence[dict[str, Any]]
                            ) -> dict[str, Any]:
    """CSV text for the close candidates, in the order given (most likely
    first). Advice for a person: an approval is what makes one count."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CLOSE_MATCH_HEADERS)
    for c in close_matches:
        unit = c.get("unit_price")
        cells = [
            c.get("original_mpn", ""), c.get("candidate_mpn", ""),
            c.get("manufacturer", ""), c.get("difference", ""),
            c.get("label", ""), c.get("meaning", ""),
            c.get("distributor", ""),
            "" if c.get("stock") is None else str(c["stock"]),
            "" if unit is None else format(unit, "f"),
            c.get("currency") or "",
            str(c.get("required_quantity") or ""),
            str(c.get("order_quantity") or ""),
            "" if c.get("extended_price") is None
            else format(c["extended_price"], "f"),
            c.get("price_packaging") or "", c.get("price_sku") or "",
            c.get("price_note") or "",
            ", ".join(c.get("skus") or []),
            ("approved and used" if c.get("used") else
             "approved, not usable" if c.get("approved") else
             "rejected for this project: not used" if c.get("rejected") else
             "not decided: not used"),
            (c.get("decision") or {}).get("method", ""),
            (c.get("decision") or {}).get("decided_at") or "",
            (c.get("decision") or {}).get("decision_id") or "",
            "; ".join(c.get("evidence") or []),
            "; ".join(c.get("uncertainty") or []),
            c.get("line_id", ""), ", ".join(c.get("designators") or []),
            c.get("bom_manufacturer", ""),
            (f"{c['alias']['method']} {c['alias']['rule']}"
             if c.get("alias") else c.get("label", "")),
            (c.get("decision") or {}).get("source", ""),
            " || ".join((c.get("alias") or {}).get("evidence") or []),
        ]
        writer.writerow([neutralize_formula(str(x))[0] for x in cells])
    return {"text": buffer.getvalue(), "headers": list(CLOSE_MATCH_HEADERS),
            "rows": len(close_matches)}
