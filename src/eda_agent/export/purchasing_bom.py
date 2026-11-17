# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A project BOM as a buyer reads it: one line per part to order.

Takes the placed components with their schematic parameters and, when a
variant is built, that variant from ``proj_list_variants``. A fitted
component is identified by two parameters and nothing else,
``Manufacturer`` and ``Part Number``. Components sharing both are one
line with a quantity; a component lacking either is listed as unresolved
with the reason, never guessed from its comment, description or library
reference and never dropped.

Every placed component lands in exactly one place: a line, ``unresolved``,
``not_fitted`` (the variant leaves it off) or ``not_in_bom`` (its
Component Kind keeps it out of the BOM).

A line also carries the components' ``Description`` parameter, which
says what the part is and identifies nothing. The comment never stands
in for it. Components of one line that disagree on it are reported, and
the line then carries no description rather than one picked from them.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .variant_bom import apply_variant

__all__ = ["purchasing_bom"]

MANUFACTURER = "Manufacturer"
PART_NUMBER = "Part Number"
DESCRIPTION = "Description"

#: Values written into a required field to say there is none. A part
#: number of "N.A" orders nothing, so it is unresolved like a blank.
_PLACEHOLDERS = {"n.a", "n.a.", "n/a", "na", "tbd", "?", "-", "none"}

#: Altium's Component Kind values that keep a component out of the BOM.
#: A kind not listed here stays in: an unknown kind is reported, not
#: dropped.
_NOT_IN_BOM_KINDS = {"standard (no bom)", "net tie", "graphical"}


def _key(text: str) -> str:
    """``text`` for comparing: case and runs of spaces do not count."""
    return " ".join(str(text).split()).casefold()


def _natural(designator: str) -> list[Any]:
    """Sort key putting C2 before C10."""
    return [int(piece) if piece.isdigit() else piece.casefold()
            for piece in re.split(r"(\d+)", designator)]


def _read(params: dict[str, Any], name: str) -> tuple[Optional[str], bool]:
    """The parameter ``name``, matched ignoring case, and whether two
    spellings of the name carry different values. None when absent."""
    values = [" ".join(str(v).split()) for k, v in params.items()
              if _key(k) == _key(name)]
    if not values:
        return None, False
    return values[0], len(set(values)) > 1


def _problem(label: str, value: Optional[str], clash: bool) -> Optional[str]:
    """Why ``value`` cannot identify a part, or None when it can."""
    if clash:
        return f"{label} is set twice with different values"
    if value is None:
        return f"no {label} parameter"
    if not value:
        return f"{label} is empty"
    if value.casefold() in _PLACEHOLDERS:
        return f"{label} is a placeholder ({value})"
    return None


def _one_per_designator(components: list[dict[str, Any]]):
    """Each designator once, and what was wrong with the ones repeated.

    Returns the components to count, the designators whose records
    disagree (counted once, as unresolved) and the designators listed
    more than once with the same data (counted once).
    """
    by_designator: dict[str, list[dict[str, Any]]] = {}
    for comp in components:
        by_designator.setdefault(
            str(comp.get("designator", "")).strip().casefold(), []).append(comp)
    kept, clashing, repeated = [], {}, []
    for records in by_designator.values():
        kept.append(records[0])
        if len(records) == 1:
            continue
        name = str(records[0].get("designator", "")).strip()
        same = all((r.get("footprint"), r.get("parameters"))
                   == (records[0].get("footprint"), records[0].get("parameters"))
                   for r in records[1:])
        if same:
            repeated.append(name)
        else:
            clashing[name.casefold()] = len(records)
    return kept, clashing, repeated


def purchasing_bom(components: list[dict[str, Any]],
                   variant: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The purchasing BOM of ``components`` as ``variant`` builds them.

    Each component is ``{"designator", "footprint", "parameters"}``,
    ``parameters`` being its schematic parameters by name, or None when
    they could not be read. ``variant`` is one entry of
    ``proj_list_variants``, or None for the base design.

    Returns ``lines`` (manufacturer, part_number, description, quantity,
    designators, footprints), ``unresolved`` (designator, manufacturer,
    part_number, description, footprint, reason), ``not_fitted``,
    ``not_in_bom``, ``inconsistencies`` and ``counts``. A line's
    description is empty when its components have none or disagree; the
    disagreement is in ``inconsistencies`` with who says what.
    """
    kept, clashing, repeated = _one_per_designator(components)
    if variant is not None:
        applied = apply_variant(kept, variant)
        built, not_fitted = applied["components"], applied["not_fitted"]
        stray = applied["entries_not_in_bom"]
    else:
        built, not_fitted, stray = kept, [], []

    lines: dict[tuple[str, str], dict[str, Any]] = {}
    spellings: dict[tuple[str, str], set[str]] = {}
    described: dict[tuple[str, str], dict[str, list[str]]] = {}
    unresolved: list[dict[str, Any]] = []
    not_in_bom: list[str] = []
    fitted = 0
    for comp in built:
        designator = str(comp.get("designator", "")).strip()
        footprint = str(comp.get("footprint", "") or "").strip()
        params = comp.get("parameters")
        base = dict(params) if isinstance(params, dict) else {}
        kind, _ = _read(base, "Component Kind")
        if kind and kind.casefold() in _NOT_IN_BOM_KINDS:
            not_in_bom.append(designator)
            continue
        fitted += 1

        varied = comp.get("varied_parameters") or {}
        alternate = comp.get("variation") == "alternate"
        # An alternate is a different part: the base part's maker and
        # number are not its own, so only the variant's values count.
        source = {} if alternate else base
        for name, value in varied.items():
            source = {k: v for k, v in source.items() if _key(k) != _key(name)}
            source[name] = value
        maker, maker_clash = _read(source, MANUFACTURER)
        number, number_clash = _read(source, PART_NUMBER)
        description = _read(source, DESCRIPTION)[0] or ""

        reasons = []
        if not designator:
            reasons.append("component has no designator")
        if designator.casefold() in clashing:
            reasons.append(f"designator is listed {clashing[designator.casefold()]}"
                           " times with different data")
        if not isinstance(params, dict):
            reasons.append("its parameters could not be read from the project")
        elif alternate and (maker is None or number is None):
            reasons.append(
                "alternate part in this variant, and the variant does not "
                f"state its {MANUFACTURER} and {PART_NUMBER}; the base "
                "part's values are not the alternate's")
        else:
            for label, value, clash in ((MANUFACTURER, maker, maker_clash),
                                        (PART_NUMBER, number, number_clash)):
                problem = _problem(label, value, clash)
                if problem:
                    reasons.append(problem)
        if reasons:
            unresolved.append({"designator": designator,
                               "manufacturer": maker or "",
                               "part_number": number or "",
                               "description": description,
                               "footprint": footprint,
                               "reason": "; ".join(reasons)})
            continue

        key = (_key(maker), _key(number))
        line = lines.setdefault(key, {
            "manufacturer": maker, "part_number": number,
            "description": "", "quantity": 0,
            "designators": [], "footprints": []})
        line["quantity"] += 1
        line["designators"].append(designator)
        if footprint and footprint not in line["footprints"]:
            line["footprints"].append(footprint)
        spellings.setdefault(key, set()).add(maker)
        described.setdefault(key, {}).setdefault(
            description, []).append(designator)

    inconsistencies: list[dict[str, Any]] = []
    makers_of: dict[str, list[str]] = {}
    for (_, number_key), line in lines.items():
        makers_of.setdefault(number_key, []).append(line["manufacturer"])
    for key, line in lines.items():
        line["designators"].sort(key=_natural)
        line["footprints"].sort()
        if len(line["footprints"]) > 1:
            inconsistencies.append({
                "issue": "one part on several footprints",
                "manufacturer": line["manufacturer"],
                "part_number": line["part_number"],
                "footprints": line["footprints"]})
        if len(spellings[key]) > 1:
            inconsistencies.append({
                "issue": "manufacturer spelled several ways, counted as one",
                "part_number": line["part_number"],
                "spellings": sorted(spellings[key])})
        if len(described[key]) == 1:
            line["description"] = next(iter(described[key]))
        else:
            # Which one is right is not ours to say, so the line names
            # none and every description is kept with its designators.
            inconsistencies.append({
                "issue": "one part with several descriptions, none chosen",
                "manufacturer": line["manufacturer"],
                "part_number": line["part_number"],
                "descriptions": [
                    {"description": text,
                     "designators": sorted(names, key=_natural)}
                    for text, names in sorted(described[key].items())]})
        makers = makers_of[key[1]]
        if len(makers) > 1 and line["manufacturer"] == sorted(makers)[0]:
            inconsistencies.append({
                "issue": "one part number under several manufacturers, "
                         "kept as separate lines",
                "part_number": line["part_number"],
                "manufacturers": sorted(makers)})
    for name in sorted(repeated, key=_natural):
        inconsistencies.append({
            "issue": "designator listed more than once, counted once",
            "designator": name})
    for name in stray:
        inconsistencies.append({
            "issue": "variant entry for a designator the project does not place",
            "designator": name})

    ordered = sorted(lines.values(), key=lambda line: (
        line["manufacturer"].casefold(), line["part_number"].casefold()))
    unresolved.sort(key=lambda entry: _natural(entry["designator"]))
    return {
        "lines": ordered,
        "unresolved": unresolved,
        "not_fitted": sorted(not_fitted, key=_natural),
        "not_in_bom": sorted(not_in_bom, key=_natural),
        "inconsistencies": inconsistencies,
        "counts": {
            "components": len(kept),
            "fitted_components": fitted,
            "line_items": len(ordered),
            "unresolved_components": len(unresolved),
            "not_fitted_components": len(not_fitted),
            "not_in_bom_components": len(not_in_bom),
        },
    }
