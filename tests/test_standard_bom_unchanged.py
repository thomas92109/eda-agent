# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The Standard BOM and the first planner are pinned.

The purchasing workflow adds modules beside them. These tests fail if any
later change alters the Standard BOM (one assembly, five default columns, no
distributor) or the shape the existing read-only planner returns. Synthetic
data only.
"""

from __future__ import annotations

import inspect

from eda_agent.export import purchasing_bom_csv as csvmod
from eda_agent.export.purchasing_plan import plan_purchases

BOM = {
    "summary": {"components": 7, "fitted_components": 6, "complete": True},
    "lines": [
        {"manufacturer": "Acme", "part_number": "ACM-R-0100",
         "description": "RES 100 OHM", "quantity": 3,
         "designators": ["R1", "R2", "R3"], "footprints": ["0603"]},
        {"manufacturer": "Globex", "part_number": "0012345678",
         "description": '=HYPERLINK("x")', "quantity": 2,
         "designators": ["U1", "U2"], "footprints": ["SOIC8"]},
    ],
    "unresolved": [{"designator": "C9", "reason": "no Part Number parameter"}],
    "not_fitted": [], "not_in_bom": ["TP1"], "inconsistencies": [],
}

GOLDEN = (
    "Quantity,Part Number,Description,Manufacturer,Reference Designator\r\n"
    '3,ACM-R-0100,RES 100 OHM,Acme,"R1, R2, R3"\r\n'
    '2,0012345678,"\'=HYPERLINK(""x"")",Globex,"U1, U2"\r\n')


# TS-S02, TS-S04
def test_the_standard_bom_bytes_are_pinned():
    built = csvmod.build_purchasing_csv(BOM)
    assert built["text"] == GOLDEN
    assert built["headers"] == ["Quantity", "Part Number", "Description",
                                "Manufacturer", "Reference Designator"]
    assert not any("CROSS-CHECK" in w for w in built["warnings"])


# TS-S05
def test_the_columns_survive_importing_the_purchasing_modules():
    import eda_agent.export.purchasing_bom_export  # noqa: F401
    import eda_agent.export.purchasing_cart  # noqa: F401
    import eda_agent.export.purchasing_config  # noqa: F401
    import eda_agent.export.purchasing_lines  # noqa: F401
    import eda_agent.export.purchasing_quantity  # noqa: F401
    assert list(csvmod.DEFAULT_COLUMNS) == [
        "quantity", "part_number", "description", "manufacturer",
        "reference_designators"]
    assert list(csvmod.COLUMNS) == [
        "quantity", "part_number", "description", "manufacturer",
        "reference_designators", "footprint", "fitted_status"]


# TS-S01
def test_the_standard_bom_tool_has_no_assembly_argument():
    from eda_agent.tools import project
    source = inspect.getsource(project)
    start = source.index("async def proj_export_purchasing_bom_csv(")
    signature = source[start:source.index(") -> dict", start)]
    for word in ("assemblies", "build_quantity", "prefer", "distributor"):
        assert word not in signature


# TS-S03
def test_building_the_standard_csv_touches_no_distributor(monkeypatch):
    from eda_agent.export import purchasing_sources

    def boom(*a, **k):
        raise AssertionError("a distributor was asked")

    monkeypatch.setattr(purchasing_sources, "enrich_bom", boom)
    monkeypatch.setattr(purchasing_sources, "_providers", boom)
    assert csvmod.build_purchasing_csv(BOM)["text"] == GOLDEN


ANALYSIS = {
    "build_quantity": 2, "bom_complete": True, "unresolved": [],
    "lines": [{
        "line": 0, "manufacturer": "Acme", "part_number": "ACM-R-0100",
        "description": "d", "designators": ["R1"], "quantity_per_board": 1,
        "required_quantity": 2,
        "distributors": {"digikey": {
            "status": "matched", "manufacturer": "Acme",
            "lifecycle": "active", "cached": False, "retrieved_at": "t",
            "offers": [{"sku": "A-ND", "packaging": ["Cut Tape"],
                        "stock": 100, "moq": 1, "multiple": None,
                        "standard_package": None, "currency": "USD",
                        "price_breaks": [{"quantity": 1, "unit_price": 0.1}],
                        "reeling_fee": None}]}}}],
}


def test_the_first_planner_keeps_its_shape():
    plan = plan_purchases(ANALYSIS, "lowest")
    assert sorted(plan) == [
        "allow_overbuy", "build_quantity", "complete", "lines", "orders",
        "prefer", "summary", "totals", "unpurchasable_lines", "unresolved"]
    line = plan["lines"][0]
    assert sorted(line) == [
        "alternatives", "choice", "description", "designators", "line",
        "manufacturer", "part_number", "quantity_per_board",
        "required_quantity", "status"]
    assert sorted(line["choice"]) == [
        "cached", "currency", "distributor", "extended_price", "lifecycle",
        "manufacturer", "moq", "multiple", "notes", "order_quantity",
        "packaging", "price_break_quantity", "retrieved_at", "sku", "stock",
        "surplus", "unit_price"]
    assert line["choice"]["order_quantity"] == 2


def test_the_first_planner_signatures_and_defaults_are_unchanged():
    from eda_agent.tools import purchasing
    source = inspect.getsource(purchasing)
    assert "build_quantity: int = 1," in source
    assert 'prefer: str = "lowest",' in source
    assert "allow_overbuy: bool = True," in source
