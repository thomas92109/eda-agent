# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The purchasing BOM as a CSV: pure formatting, no Altium and no disk.

``build_purchasing_csv`` takes what ``proj_get_bom(mode="purchasing")``
returns and turns it into CSV text, with the columns the caller chose, in
the order chosen. Every value is Altium's own: nothing is guessed, merged
or shortened, and every reference designator is kept. The file is produced
by the ``csv`` module, never typed.

Parts the variant does not fit (DNP) are left out unless asked for. When
they are asked for, each is a row of its own with the status ``DNP`` and a
purchase quantity of 0, and the ``fitted_status`` column is required so a
DNP row can never be mistaken for a part to buy.

Two things are done to the text and both are reported in ``warnings``:

* A cell that a spreadsheet would run as a formula (it starts with ``=``,
  ``+``, ``-`` or ``@``, or with a tab or line break) gets a leading
  apostrophe. That is the usual defence against CSV formula injection; it
  is the only change made to any value.
* Values Excel would quietly reinterpret when the file is opened directly
  (a part number ``0012345678`` losing its zeros, ``1E5`` becoming a
  number, ``5-1`` becoming a date) are counted, not altered.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from typing import Any, Optional, Sequence

__all__ = [
    "COLUMNS",
    "DEFAULT_COLUMNS",
    "DNP",
    "ENCODING",
    "FITTED",
    "ColumnSelectionError",
    "build_purchasing_csv",
    "neutralize_formula",
    "resolve_columns",
]

#: Column identifier -> header. The header for ``reference_designators`` is
#: singular on purpose: it is the name the buyer's template uses.
COLUMNS: dict[str, str] = {
    "quantity": "Quantity",
    "part_number": "Part Number",
    "description": "Description",
    "manufacturer": "Manufacturer",
    "reference_designators": "Reference Designator",
    "footprint": "Footprint",
    "fitted_status": "Fitted / DNP Status",
}

#: What an export has when ``columns`` is left out, in this order.
DEFAULT_COLUMNS: tuple[str, ...] = (
    "quantity", "part_number", "description", "manufacturer",
    "reference_designators")

FITTED = "Fitted"
DNP = "DNP"

#: UTF-8 with a byte order mark: Excel reads a CSV as the system code page
#: unless the mark tells it otherwise, which garbles characters like the
#: plus-minus sign in a resistor description.
ENCODING = "utf-8-sig"

#: Excel cannot hold more than 32767 characters in a cell.
_EXCEL_CELL_LIMIT = 32000
_NAMES_SHOWN = 20
_FORMULA_LEADERS = ("=", "+", "-", "@")
_EXPONENT = re.compile(r"^[+-]?\d+(\.\d*)?[eE][+-]?\d+$")
_DATE_LIKE = re.compile(r"^\d{1,4}[-/.]\d{1,4}([-/.]\d{1,4})?$")


class ColumnSelectionError(ValueError):
    """A ``columns`` choice that cannot be honoured."""


def _valid() -> str:
    return ", ".join(COLUMNS)


def resolve_columns(columns: Optional[Sequence[str]],
                    include_dnp: bool = False) -> list[str]:
    """The ordered column identifiers to export.

    ``None`` is the default five. Anything else must be a non-empty list of
    the identifiers in ``COLUMNS``, each at most once; the order given is
    the order written. Nothing is corrected or guessed: an unknown,
    repeated or empty choice is refused with the reason.

    With ``include_dnp`` the list must also name ``fitted_status``, so the
    default five (which have no status column) are refused too. A column is
    never added for the caller.
    """
    if columns is None:
        chosen = list(DEFAULT_COLUMNS)
    else:
        if isinstance(columns, (str, bytes)) \
                or not isinstance(columns, (list, tuple)):
            raise ColumnSelectionError(
                "columns must be a list of column identifiers, not "
                f"{type(columns).__name__}; the identifiers are: {_valid()}")
        if not columns:
            raise ColumnSelectionError(
                "columns is empty. Name at least one of: " + _valid()
                + ", or leave columns out for the default "
                + ", ".join(DEFAULT_COLUMNS))
        not_text = [c for c in columns if not isinstance(c, str)]
        unknown = [c for c in columns
                   if isinstance(c, str) and c not in COLUMNS]
        repeated: list[str] = []
        seen: set[str] = set()
        for c in columns:
            if isinstance(c, str):
                if c in seen and c not in repeated:
                    repeated.append(c)
                seen.add(c)
        problems = []
        if not_text:
            problems.append("not text: "
                            + ", ".join(repr(c) for c in not_text))
        if unknown:
            problems.append("unknown: "
                            + ", ".join(repr(c) for c in unknown))
        if repeated:
            problems.append("listed more than once: "
                            + ", ".join(repr(c) for c in repeated))
        if problems:
            raise ColumnSelectionError(
                "columns refused (" + "; ".join(problems)
                + "). The identifiers are: " + _valid())
        chosen = list(columns)
    if include_dnp and "fitted_status" not in chosen:
        why = ("columns is not set, and the default five columns have no "
               "status column" if columns is None
               else "it is not among the columns given")
        raise ColumnSelectionError(
            f"include_dnp=True needs 'fitted_status' in columns ({why}), so "
            "a DNP row cannot be mistaken for a part to buy. Add it, for "
            "example columns=[" + ", ".join(
                repr(c) for c in (*DEFAULT_COLUMNS, "fitted_status"))
            + "]. Columns are never added for you.")
    return chosen


def neutralize_formula(text: str) -> tuple[str, bool]:
    """``text`` made safe to open in a spreadsheet, and whether it changed.

    A value a spreadsheet would run as a formula gets a leading apostrophe.
    Leading spaces are looked through, since a spreadsheet may too.
    """
    if text and (text[0] in "\t\r\n"
                 or text.lstrip(" ")[:1] in _FORMULA_LEADERS):
        return "'" + text, True
    return text, False


def _excel_reinterprets(text: str) -> bool:
    """True when Excel, opening the CSV directly, would change this value."""
    if not text or text[0] == "'":
        return False
    if text.isdigit():
        return text[0] == "0" and len(text) > 1 or len(text) >= 12
    return bool(_EXPONENT.match(text) or _DATE_LIKE.match(text))


def _row(line: dict[str, Any], status: str, designators: list[str],
         quantity: int) -> dict[str, Any]:
    return {
        "quantity": quantity,
        "part_number": str(line.get("part_number") or ""),
        "description": str(line.get("description") or ""),
        "manufacturer": str(line.get("manufacturer") or ""),
        "reference_designators": ", ".join(str(d) for d in designators),
        "footprint": ", ".join(str(f) for f in line.get("footprints") or []),
        "fitted_status": status,
    }


def _some(names: list[str]) -> str:
    shown = ", ".join(names[:_NAMES_SHOWN])
    more = len(names) - _NAMES_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def build_purchasing_csv(bom: dict[str, Any],
                         columns: Optional[Sequence[str]] = None,
                         dnp_bom: Optional[dict[str, Any]] = None,
                         include_dnp: Optional[bool] = None
                         ) -> dict[str, Any]:
    """CSV text for a purchasing BOM, with totals and warnings.

    ``bom`` is the purchasing BOM of the fitted parts. ``dnp_bom`` is the
    same BOM built with the not-fitted parts counted as fitted
    (``purchasing_bom_of_project(..., include_not_fitted=True)``). It is
    needed only to put DNP parts in the file; ``include_dnp`` says whether
    to, and defaults to whether ``dnp_bom`` was given.

    The fitted rows are always ``bom``'s own lines. A DNP row is a not-fitted
    part's line from ``dnp_bom``, cut down to the not-fitted designators on
    it, with status ``DNP`` and quantity 0: it is listed, not bought.

    Each not-fitted part is accounted for from Altium's own lists and never
    inferred: on a DNP row, or kept out of the BOM by its Component Kind
    (``not_in_bom``), or ``unresolved`` (no usable Manufacturer or Part
    Number), or, should Altium name none of these, unclassified.

    Returns ``text`` (the CSV), ``columns``, ``headers``, ``rows`` (data
    rows), ``totals`` and ``warnings``. Raises ``ColumnSelectionError``
    for a column choice that cannot be honoured, which includes
    ``include_dnp`` without ``fitted_status``.
    """
    include = (dnp_bom is not None) if include_dnp is None else bool(include_dnp)
    chosen = resolve_columns(columns, include_dnp=include)
    summary = bom.get("summary") or {}

    rows = [_row(line, FITTED, list(line.get("designators") or []),
                 int(line.get("quantity") or 0))
            for line in bom.get("lines") or []]
    fitted_quantity = sum(r["quantity"] for r in rows)
    fitted_row_count = len(rows)

    not_fitted = [str(d) for d in bom.get("not_fitted") or []]
    dnp_rows: list[dict[str, Any]] = []
    on_a_row: set[str] = set()
    kept_out: list[str] = []
    no_data: list[str] = []
    unclassified: list[str] = []
    if include and not_fitted:
        source = dnp_bom or {}
        wanted = set(not_fitted)
        for line in source.get("lines") or []:
            mine = [str(d) for d in line.get("designators") or []
                    if str(d) in wanted]
            if mine:
                on_a_row.update(mine)
                dnp_rows.append(_row(line, DNP, mine, 0))
        by_kind = {str(d) for d in source.get("not_in_bom") or []}
        unresolved_ids = {str(u.get("designator", ""))
                          for u in source.get("unresolved") or []}
        for d in not_fitted:
            if d in on_a_row:
                continue
            if d in by_kind:
                kept_out.append(d)
            elif d in unresolved_ids:
                no_data.append(d)
            else:
                unclassified.append(d)
    rows.extend(dnp_rows)
    rows.sort(key=lambda r: (r["manufacturer"].casefold(),
                             r["part_number"].casefold(),
                             r["fitted_status"] != FITTED))

    table: list[list[str]] = []
    neutralized = 0
    reinterpreted: Counter[str] = Counter()
    too_long = 0
    for row in rows:
        cells = []
        for column in chosen:
            if column == "quantity":
                cells.append(str(row["quantity"]))
                continue
            text, changed = neutralize_formula(str(row[column]))
            neutralized += changed
            if column != "fitted_status" and _excel_reinterprets(text):
                reinterpreted[COLUMNS[column]] += 1
            too_long += len(text) > _EXCEL_CELL_LIMIT
            cells.append(text)
        table.append(cells)

    headers = [COLUMNS[c] for c in chosen]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(table)

    unresolved = bom.get("unresolved") or []
    not_in_bom = bom.get("not_in_bom") or []
    fitted_components = summary.get("fitted_components")
    # The breakdown of the not-fitted parts is known only when the second
    # read was made; otherwise it is None, not a guess.
    known = include
    totals = {
        "components": summary.get("components"),
        "fitted_components": fitted_components,
        "bom_lines": len(bom.get("lines") or []),
        "fitted_rows": fitted_row_count,
        "fitted_quantity": fitted_quantity,
        "unresolved_components": len(unresolved),
        "not_in_bom_components": len(not_in_bom),
        "not_fitted_components": len(not_fitted),
        "dnp_rows": len(dnp_rows),
        "dnp_components": len(on_a_row),
        "dnp_not_in_bom_components": len(kept_out) if known else None,
        "dnp_unresolved_components": len(no_data) if known else None,
        "dnp_unclassified_components": len(unclassified) if known else None,
    }

    warnings: list[str] = []
    if unresolved:
        warnings.append(
            f"{len(unresolved)} fitted component(s) have no usable "
            "Manufacturer or Part Number and are NOT in this CSV: "
            + _some([str(u.get("designator", "")) for u in unresolved]))
    if not_in_bom:
        warnings.append(
            f"{len(not_in_bom)} fitted component(s) are kept out of the BOM "
            "by their Component Kind (Altium's Not in BOM setting) and are "
            "not in this CSV: " + _some([str(d) for d in not_in_bom]))
    if kept_out:
        warnings.append(
            f"{len(kept_out)} not-fitted component(s) are also kept out of "
            "the BOM by their Component Kind (Altium's Not in BOM setting), "
            "so they have no DNP row: " + _some(kept_out))
    if no_data:
        warnings.append(
            f"{len(no_data)} not-fitted component(s) have no usable "
            "Manufacturer or Part Number according to Altium, so they have "
            "no DNP row: " + _some(no_data))
    if unclassified:
        warnings.append(
            f"{len(unclassified)} not-fitted component(s) are in none of "
            "Altium's lists (BOM line, not in BOM, unresolved), so they "
            "have no DNP row: " + _some(unclassified))
    issues = Counter(str(i.get("issue", "")) for i in
                     bom.get("inconsistencies") or [])
    if issues:
        warnings.append(
            "Altium's BOM data has inconsistencies, kept as Altium has "
            "them: " + "; ".join(f"{n} x {issue}"
                                 for issue, n in issues.items()))
    if "description" in chosen:
        blank = sum(1 for r in rows if not r["description"])
        if blank:
            warnings.append(f"{blank} row(s) have an empty Description")
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
        warnings.append(
            f"{too_long} cell(s) are longer than {_EXCEL_CELL_LIMIT} "
            "characters; Excel cuts a cell at 32767")
    if isinstance(fitted_components, int):
        expected = fitted_components - len(unresolved)
        if fitted_quantity != expected:
            warnings.append(
                f"CROSS-CHECK FAILED: the rows add up to {fitted_quantity} "
                f"fitted part(s) but Altium's counts say {expected}. Do not "
                "order from this file until that is explained")
    components = summary.get("components")
    if isinstance(components, int):
        accounted = (fitted_quantity + len(unresolved) + len(not_in_bom)
                     + len(not_fitted))
        if accounted != components:
            warnings.append(
                f"CROSS-CHECK FAILED: fitted, unresolved, not-in-BOM and "
                f"not-fitted parts add up to {accounted} but Altium counts "
                f"{components} components. Do not order from this file "
                "until that is explained")

    return {"text": buffer.getvalue(), "columns": chosen, "headers": headers,
            "rows": len(rows), "totals": totals, "warnings": warnings}
