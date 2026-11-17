# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""proj_get_bom's purchasing mode: the BOM to order from.

The full BOM of a 393-part board came to about 100 KB, most of it pins
and nets, and carried neither of the two parameters that say what to
buy. Purchasing mode returns one line per ``Manufacturer`` and
``Part Number`` with a quantity, built for a variant, and puts every
component it cannot identify in ``unresolved`` with the reason. A wrong
line here is an order for the wrong part, so nothing is guessed and
nothing is dropped.
"""
from __future__ import annotations

import asyncio
import json

from eda_agent.export.purchasing_bom import purchasing_bom
from eda_agent.tools import project


def _part(designator, maker="Yageo", number="RC0603FR-0710KL",
          footprint="RESC1608L", **more):
    params = {"Comment": "10k", "Description": "RES 10K 1% 0603",
              "Component Kind": "Standard", "Library Reference": "Res1",
              "Supplier Part Number 1": "311-10.0KHRCT-ND"}
    if maker is not None:
        params["Manufacturer"] = maker
    if number is not None:
        params["Part Number"] = number
    params.update(more)
    return {"designator": designator, "footprint": footprint,
            "parameters": params}


def _line(out, number, maker=None):
    return next(line for line in out["lines"]
                if line["part_number"] == number
                and maker in (None, line["manufacturer"]))


def _why(out, designator):
    return next(u for u in out["unresolved"]
                if u["designator"] == designator)


# ---- grouping -------------------------------------------------------------

def test_parts_sharing_maker_and_number_are_one_line():
    out = purchasing_bom([
        _part("R10"), _part("R2"), _part("R1"),
        _part("U1", "Texas Instruments", "OPA837IDBVR", "SOT-23-6"),
    ])
    assert out["lines"] == [
        {"manufacturer": "Texas Instruments", "part_number": "OPA837IDBVR",
         "description": "RES 10K 1% 0603",
         "quantity": 1, "designators": ["U1"], "footprints": ["SOT-23-6"]},
        {"manufacturer": "Yageo", "part_number": "RC0603FR-0710KL",
         "description": "RES 10K 1% 0603", "quantity": 3, "designators": ["R1", "R2", "R10"],
         "footprints": ["RESC1608L"]},
    ]
    assert out["unresolved"] == [] and out["inconsistencies"] == []
    assert out["counts"] == {
        "components": 4, "fitted_components": 4, "line_items": 2,
        "unresolved_components": 0, "not_fitted_components": 0,
        "not_in_bom_components": 0}


def test_one_part_number_from_two_makers_stays_two_lines():
    out = purchasing_bom([
        _part("U1", "Texas Instruments", "LM358DR", "SOIC-8"),
        _part("U2", "Texas Instruments", "LM358DR", "SOIC-8"),
        _part("U3", "Rohm Semiconductor", "LM358DR", "SOIC-8"),
    ])
    assert [(l["manufacturer"], l["quantity"], l["designators"])
            for l in out["lines"]] == [
        ("Rohm Semiconductor", 1, ["U3"]),
        ("Texas Instruments", 2, ["U1", "U2"])]
    assert out["inconsistencies"] == [{
        "issue": "one part number under several manufacturers, "
                 "kept as separate lines",
        "part_number": "LM358DR",
        "manufacturers": ["Rohm Semiconductor", "Texas Instruments"]}]


def test_only_the_two_parameters_identify_a_part():
    """Same comment, description, library reference and supplier number:
    still two parts, because the part numbers differ. The description
    is carried on each line and nothing else is."""
    out = purchasing_bom([_part("R1"), _part("R2", number="RC0603FR-0710KP")])
    assert [l["quantity"] for l in out["lines"]] == [1, 1]
    text = json.dumps(out)
    for other in ("10k", "Res1", "311-10.0KHRCT-ND"):
        assert other not in text


# ---- the description --------------------------------------------------------

def test_a_line_carries_the_description_parameter():
    out = purchasing_bom([
        _part("R1"), _part("R2", Description="  RES 10K	1%  0603 "),
        _part("U1", "Texas Instruments", "OPA837IDBVR", "SOT-23-6",
              Description="IC OPAMP VFB 105MHZ SOT23-6")])
    assert _line(out, "RC0603FR-0710KL")["description"] == "RES 10K 1% 0603"
    assert _line(out, "OPA837IDBVR")["description"] ==         "IC OPAMP VFB 105MHZ SOT23-6"
    assert out["inconsistencies"] == []


def test_the_comment_never_stands_in_for_a_missing_description():
    blank = _part("R1", Description="   ", Comment="10k pull-up, see note 4")
    absent = _part("R2", Comment="DNP on rev A")
    del absent["parameters"]["Description"]
    out = purchasing_bom([blank, absent])
    line = _line(out, "RC0603FR-0710KL")
    assert line["quantity"] == 2, "a part with no description is still ordered"
    assert line["description"] == ""
    assert out["unresolved"] == [] and out["inconsistencies"] == []
    assert "note 4" not in json.dumps(out) and "rev A" not in json.dumps(out)


def test_descriptions_that_differ_are_reported_and_none_is_chosen():
    out = purchasing_bom([
        _part("R10"), _part("R2"),
        _part("R3", Description="RES 10K OHM 1% 1/10W 0603"),
        _part("R4", Description="")])
    line = _line(out, "RC0603FR-0710KL")
    assert line["quantity"] == 4, "the grouping does not depend on it"
    assert line["designators"] == ["R2", "R3", "R4", "R10"]
    assert line["description"] == ""
    assert out["inconsistencies"] == [{
        "issue": "one part with several descriptions, none chosen",
        "manufacturer": "Yageo", "part_number": "RC0603FR-0710KL",
        "descriptions": [
            {"description": "", "designators": ["R4"]},
            {"description": "RES 10K 1% 0603", "designators": ["R2", "R10"]},
            {"description": "RES 10K OHM 1% 1/10W 0603",
             "designators": ["R3"]}]}]


def test_an_unresolved_component_keeps_its_description():
    out = purchasing_bom([_part("R2", maker=None)])
    assert _why(out, "R2")["description"] == "RES 10K 1% 0603"


def test_a_variant_states_the_description_of_what_it_changes():
    variant = {"name": "Lite", "variations": [
        {"designator": "R1", "kind": "Fitted", "alternate_part": "",
         "parameters": [{"name": "Part Number", "value": "RC0603FR-074K7L"},
                        {"name": "description", "value": "RES 4K7 1% 0603"}]},
        {"designator": "U1", "kind": "Alternate", "alternate_part": "LDO_B",
         "parameters": [{"name": "Manufacturer", "value": "Microchip"},
                        {"name": "Part Number",
                         "value": "MCP1700T-3302E/TT"}]}]}
    out = purchasing_bom([
        _part("R1"),
        _part("U1", "Texas Instruments", "TLV70033DDCR", "SOT-23-5A",
              Description="IC REG LINEAR 3.3V 200MA SOT23-5")], variant)
    assert _line(out, "RC0603FR-074K7L")["description"] == "RES 4K7 1% 0603"
    # The base part's description is not the alternate's.
    assert _line(out, "MCP1700T-3302E/TT")["description"] == ""


def test_parameter_names_match_ignoring_case_and_values_are_trimmed():
    part = {"designator": "R1", "footprint": "RESC1608L", "parameters": {
        "MANUFACTURER": "  Yageo ", "part number": "RC0603FR-0710KL\t"}}
    out = purchasing_bom([part, _part("R2")])
    assert _line(out, "RC0603FR-0710KL")["designators"] == ["R1", "R2"]


def test_a_maker_spelled_two_ways_is_one_line_and_is_reported():
    out = purchasing_bom([_part("C1", "YAGEO", "CC0402KRX5R9BB104"),
                          _part("C2", "Yageo", "CC0402KRX5R9BB104")])
    assert _line(out, "CC0402KRX5R9BB104")["quantity"] == 2
    assert out["inconsistencies"] == [{
        "issue": "manufacturer spelled several ways, counted as one",
        "part_number": "CC0402KRX5R9BB104",
        "spellings": ["YAGEO", "Yageo"]}]


def test_one_part_on_two_footprints_lists_both_and_is_reported():
    out = purchasing_bom([_part("R1"), _part("R2", footprint="RESC1005L")])
    line = _line(out, "RC0603FR-0710KL")
    assert line["quantity"] == 2
    assert line["footprints"] == ["RESC1005L", "RESC1608L"]
    assert out["inconsistencies"][0]["issue"] == \
        "one part on several footprints"


# ---- what cannot be identified ----------------------------------------------

def test_a_missing_or_blank_parameter_is_unresolved_never_guessed():
    out = purchasing_bom([
        _part("R1"),
        _part("R2", maker=None),
        _part("R3", number=None),
        _part("R4", maker="   "),
        _part("R5", number=""),
        _part("TP1", "N.A", "N.A", "testpad"),
        _part("R6", maker=None, number=None),
    ])
    assert [l["designators"] for l in out["lines"]] == [["R1"]]
    assert _why(out, "R2") == {
        "designator": "R2", "manufacturer": "",
        "part_number": "RC0603FR-0710KL",
        "description": "RES 10K 1% 0603", "footprint": "RESC1608L",
        "reason": "no Manufacturer parameter"}
    assert _why(out, "R3")["reason"] == "no Part Number parameter"
    assert _why(out, "R3")["manufacturer"] == "Yageo"
    assert _why(out, "R4")["reason"] == "Manufacturer is empty"
    assert _why(out, "R5")["reason"] == "Part Number is empty"
    assert _why(out, "TP1")["reason"] == (
        "Manufacturer is a placeholder (N.A); "
        "Part Number is a placeholder (N.A)")
    assert _why(out, "R6")["reason"] == (
        "no Manufacturer parameter; no Part Number parameter")
    assert out["counts"]["fitted_components"] == 7
    assert out["counts"]["unresolved_components"] == 6


def test_parameters_that_could_not_be_read_are_unresolved():
    out = purchasing_bom([{"designator": "U9", "footprint": "FTG256",
                           "parameters": None}])
    assert out["lines"] == []
    assert _why(out, "U9")["reason"] == \
        "its parameters could not be read from the project"


def test_a_parameter_set_twice_with_different_values_is_unresolved():
    part = _part("R1")
    part["parameters"]["part number"] = "SOMETHING-ELSE"
    out = purchasing_bom([part])
    assert _why(out, "R1")["reason"] == \
        "Part Number is set twice with different values"


def test_a_repeated_designator_is_counted_once():
    """The same record twice is one part. Two records that disagree are
    one part nobody can name, not two parts and not a silent pick."""
    out = purchasing_bom([
        _part("R1"), _part("R1"),
        _part("R2"), _part("R2", number="RC0603FR-0722KL"),
        _part("R3"),
    ])
    assert _line(out, "RC0603FR-0710KL")["designators"] == ["R1", "R3"]
    assert _why(out, "R2")["reason"] == \
        "designator is listed 2 times with different data"
    assert out["counts"]["components"] == 3
    assert out["counts"]["fitted_components"] == 3
    assert {"issue": "designator listed more than once, counted once",
            "designator": "R1"} in out["inconsistencies"]


def test_a_component_kind_outside_the_bom_is_not_ordered():
    out = purchasing_bom([
        _part("R1"),
        _part("FD1", "N.A", "N.A", "Fiducial",
              **{"Component Kind": "Standard (No BOM)"}),
        _part("NT1", None, None, "NetTie", **{"Component Kind": "Net Tie"}),
        _part("NT2", **{"Component Kind": "Net Tie (In BOM)"}),
        _part("M1", **{"Component Kind": "Mechanical"}),
    ])
    assert out["not_in_bom"] == ["FD1", "NT1"]
    assert out["unresolved"] == []
    assert _line(out, "RC0603FR-0710KL")["designators"] == ["M1", "NT2", "R1"]
    assert out["counts"]["fitted_components"] == 3
    assert out["counts"]["not_in_bom_components"] == 2


# ---- variants -----------------------------------------------------------------

_VARIANT = {"name": "Lite", "description": "Low cost", "variations": [
    {"designator": "r2", "kind": "Not Fitted", "alternate_part": "",
     "parameters": []},
    {"designator": "R3", "kind": "Fitted", "alternate_part": "",
     "parameters": [{"name": "part number", "value": "RC0603FR-074K7L"},
                    {"name": "Comment", "value": "4k7"}]},
    {"designator": "U1", "kind": "Alternate", "alternate_part": "LDO_B",
     "alternate_link": {"symbol_reference": "LDO_B", "footprint": "SOT-23-5",
                        "same_footprint": False},
     "parameters": [{"name": "Manufacturer", "value": "Microchip"},
                    {"name": "Part Number", "value": "MCP1700T-3302E/TT"}]},
    {"designator": "U2", "kind": "Alternate", "alternate_part": "LDO_C",
     "alternate_link": {"symbol_reference": "LDO_C", "footprint": "SOT-23-5",
                        "same_footprint": True},
     "parameters": [{"name": "Comment", "value": "LDO C"}]},
    {"designator": "R99", "kind": "Not Fitted", "alternate_part": "",
     "parameters": []},
]}
_BOARD = [
    _part("R1"), _part("R2"), _part("R3"),
    _part("U1", "Texas Instruments", "TLV70033DDCR", "SOT-23-5A"),
    _part("U2", "Texas Instruments", "TLV70033DDCR", "SOT-23-5A"),
]


def test_a_variant_leaves_out_what_it_does_not_fit():
    out = purchasing_bom(_BOARD, _VARIANT)
    assert out["not_fitted"] == ["R2"]
    assert "R2" not in json.dumps(out["lines"])
    assert out["counts"]["components"] == 5
    assert out["counts"]["fitted_components"] == 4
    assert out["counts"]["not_fitted_components"] == 1
    base = purchasing_bom(_BOARD)
    assert base["not_fitted"] == []
    assert _line(base, "RC0603FR-0710KL")["quantity"] == 3


def test_a_variant_value_replaces_the_base_part_number():
    out = purchasing_bom(_BOARD, _VARIANT)
    assert _line(out, "RC0603FR-0710KL")["designators"] == ["R1"]
    varied = _line(out, "RC0603FR-074K7L")
    assert varied["manufacturer"] == "Yageo", "the base maker still stands"
    assert varied["designators"] == ["R3"]


def test_an_alternate_is_ordered_only_when_the_variant_names_it():
    out = purchasing_bom(_BOARD, _VARIANT)
    alternate = _line(out, "MCP1700T-3302E/TT")
    assert alternate["manufacturer"] == "Microchip"
    assert alternate["designators"] == ["U1"]
    assert alternate["footprints"] == ["SOT-23-5"]
    # U2's alternate names no maker or number, and the base part's are
    # not the alternate's: ordering TLV70033DDCR for it would be wrong.
    assert "TLV70033DDCR" not in json.dumps(out["lines"])
    assert "alternate part in this variant" in _why(out, "U2")["reason"]


def test_a_variant_entry_for_an_absent_designator_is_reported():
    out = purchasing_bom(_BOARD, _VARIANT)
    assert {"issue": "variant entry for a designator the project does "
                     "not place", "designator": "R99"} in out["inconsistencies"]


def test_every_component_is_counted_exactly_once():
    board = _BOARD + [
        _part("R4", maker=None),
        _part("FD1", "N.A", "N.A", **{"Component Kind": "Standard (No BOM)"})]
    out = purchasing_bom(board, _VARIANT)
    counts = out["counts"]
    ordered = sum(line["quantity"] for line in out["lines"])
    assert ordered + counts["unresolved_components"] == \
        counts["fitted_components"]
    assert counts["fitted_components"] + counts["not_fitted_components"] \
        + counts["not_in_bom_components"] == counts["components"] == 7
    everywhere = [d for line in out["lines"] for d in line["designators"]] \
        + [u["designator"] for u in out["unresolved"]] \
        + out["not_fitted"] + out["not_in_bom"]
    assert sorted(everywhere) == sorted(c["designator"] for c in board)


# ---- the tool -------------------------------------------------------------------

class _Bridge:
    """Altium, as far as these four commands go."""

    def __init__(self, board, variants=(), active="[No Variations]",
                 limit_hit=False, skipped_nil=0, unreadable=()):
        self.board, self.variants, self.active = board, list(variants), active
        self.limit_hit, self.skipped_nil = limit_hit, skipped_nil
        self.unreadable = set(unreadable)
        self.sent = []

    async def send_command_async(self, command, params, **kwargs):
        self.sent.append((command, dict(params)))
        if command == "project.get_bom":
            rows = [{"designator": c["designator"], "comment": "10k",
                     "footprint": c["footprint"], "lib_ref": "Res1",
                     "pins": [{"pin": "1", "name": "1", "net": "VCC_3V3"},
                              {"pin": "2", "name": "2", "net": "GND"}]}
                    for c in self.board]
            return {"components": rows, "count": len(rows),
                    "limit": int(params["limit"]),
                    "limit_hit": self.limit_hit, "repeats_skipped": 0,
                    "skipped_nil": self.skipped_nil}
        if command == "project.get_active_variant":
            return {"name": self.active, "description": ""}
        if command == "project.get_variants":
            return {"variants": self.variants, "count": len(self.variants)}
        if command == "project.get_component_info_batch":
            asked = params["designators"].split("~~")
            assert params["with_pin_nets"] == "false"
            found = [dict(c, pins=[{"pin": "1", "name": "1"}])
                     for c in self.board if c["designator"] in asked
                     and c["designator"] not in self.unreadable]
            return {"components": found, "matched": len(found),
                    "requested": len(asked), "not_found": []}
        raise AssertionError(f"unexpected command {command}")


def _tool(monkeypatch, bridge):
    monkeypatch.setattr(project, "get_bridge", lambda: bridge)
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    project.register_project_tools(_Mcp())
    return captured["proj_get_bom"]


def test_the_default_mode_is_the_full_bom_it_always_was(monkeypatch):
    bridge = _Bridge(_BOARD)
    tool = _tool(monkeypatch, bridge)
    for kwargs in ({}, {"mode": "full"}):
        bridge.sent.clear()
        out = asyncio.run(tool(**kwargs))
        assert [c for c, _ in bridge.sent] == ["project.get_bom"]
        assert out["count"] == 5 and len(out["components"]) == 5
        assert out["components"][0]["pins"][0]["net"] == "VCC_3V3"
        assert "_datasheet_guidance" in out
        assert "summary" not in out and "lines" not in out


def test_an_unknown_mode_is_refused_before_altium(monkeypatch):
    bridge = _Bridge(_BOARD)
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="shopping"))
    assert out["ok"] is False and "purchasing" in out["reason"]
    assert bridge.sent == []


def test_purchasing_mode_builds_the_active_variant(monkeypatch):
    bridge = _Bridge(_BOARD, [_VARIANT], active="Lite")
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    assert out["mode"] == "purchasing"
    assert out["summary"] == {
        "variant": "Lite", "variant_source": "active", "components": 5,
        "fitted_components": 4, "line_items": 3,
        "unresolved_components": 1, "not_fitted_components": 1,
        "not_in_bom_components": 0, "complete": True, "truncated": False}
    assert out["not_fitted"] == ["R2"]
    assert [c for c, _ in bridge.sent] == [
        "project.get_bom", "project.get_active_variant",
        "project.get_variants", "project.get_component_info_batch"]


def test_purchasing_mode_carries_no_pins_nets_or_other_fields(monkeypatch):
    bridge = _Bridge(_BOARD, [_VARIANT], active="Lite")
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    text = json.dumps(out)
    for absent in ("pins", "VCC_3V3", "GND", "lib_ref", "comment", "Res1",
                   "10k", "Supplier", "_datasheet"):
        assert absent not in text, absent
    assert set(out) == {"mode", "summary", "lines", "unresolved",
                        "not_fitted", "not_in_bom", "inconsistencies"}
    assert set(out["lines"][0]) == {"manufacturer", "part_number",
                                    "description", "quantity",
                                    "designators", "footprints"}
    assert _line(out, "RC0603FR-0710KL")["description"] == "RES 10K 1% 0603"


def test_purchasing_mode_with_no_active_variant_is_the_base_design(
        monkeypatch):
    bridge = _Bridge(_BOARD, [_VARIANT])
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    assert out["summary"]["variant"] == "[No Variations]"
    assert out["summary"]["fitted_components"] == 5
    assert out["not_fitted"] == []
    assert "project.get_variants" not in [c for c, _ in bridge.sent]


def test_a_named_variant_wins_over_the_active_one(monkeypatch):
    bridge = _Bridge(_BOARD, [_VARIANT], active="[No Variations]")
    tool = _tool(monkeypatch, bridge)
    out = asyncio.run(tool(mode="purchasing", variant="low cost"))
    assert out["summary"]["variant"] == "Lite"
    assert out["summary"]["variant_source"] == "requested"
    assert "project.get_active_variant" not in [c for c, _ in bridge.sent]

    bridge = _Bridge(_BOARD, [_VARIANT], active="Lite")
    out = asyncio.run(_tool(monkeypatch, bridge)(
        mode="purchasing", variant="[No Variations]"))
    assert out["summary"]["variant"] == "[No Variations]"
    assert out["summary"]["not_fitted_components"] == 0


def test_a_variant_that_cannot_be_settled_is_refused_not_assumed(
        monkeypatch):
    """A BOM for the wrong variant has wrong quantities and looks fine."""
    bridge = _Bridge(_BOARD, [_VARIANT], active="")
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    assert out["ok"] is False and "active variant" in out["reason"]
    assert "lines" not in out

    bridge = _Bridge(_BOARD, [_VARIANT], active="Lite")
    out = asyncio.run(_tool(monkeypatch, bridge)(
        mode="purchasing", variant="Full"))
    assert out["ok"] is False and out["variants"] == ["Lite"]


def test_a_bom_cut_short_is_never_called_complete(monkeypatch):
    bridge = _Bridge(_BOARD[:2], limit_hit=True)
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing", limit=2))
    assert out["summary"]["complete"] is False
    assert out["summary"]["truncated"] is True
    assert "limit=2" in out["summary"]["incomplete_reason"]
    assert bridge.sent[0] == ("project.get_bom", {"limit": "2"})

    bridge = _Bridge(_BOARD, skipped_nil=3)
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    assert out["summary"]["complete"] is False
    assert out["summary"]["truncated"] is False
    assert "3 object(s)" in out["summary"]["incomplete_reason"]

    bridge = _Bridge(_BOARD)
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    assert out["summary"]["complete"] is True
    assert "incomplete_reason" not in out["summary"]


def test_a_component_whose_parameters_did_not_come_back_is_unresolved(
        monkeypatch):
    bridge = _Bridge(_BOARD, unreadable={"U2"})
    out = asyncio.run(_tool(monkeypatch, bridge)(mode="purchasing"))
    assert [u["designator"] for u in out["unresolved"]] == ["U2"]
    assert out["summary"]["fitted_components"] == 5
    assert out["summary"]["complete"] is True, (
        "every component is accounted for; U2 is listed, not lost")


def test_a_large_board_is_read_in_batches_and_stays_small(monkeypatch):
    board = [_part(f"R{i}", number=f"RC0603FR-07{i % 40}KL")
             for i in range(1, 451)]
    bridge = _Bridge(board)
    out = asyncio.run(_tool(monkeypatch, bridge)(
        mode="purchasing", project_path="C:\\b\\b.PrjPcb"))
    batches = [p for c, p in bridge.sent
               if c == "project.get_component_info_batch"]
    assert [len(p["designators"].split("~~")) for p in batches] == [200, 200, 50]
    assert all(p["project_path"] == "C:\\b\\b.PrjPcb" for _, p in bridge.sent)
    assert out["summary"]["fitted_components"] == 450
    assert out["summary"]["line_items"] == 40
    assert sum(l["quantity"] for l in out["lines"]) == 450

    full = asyncio.run(_tool(monkeypatch, _Bridge(board))())
    assert len(json.dumps(out)) * 5 < len(json.dumps(full))


def test_a_bom_that_cannot_be_read_is_refused(monkeypatch):
    class _Dead(_Bridge):
        async def send_command_async(self, command, params, **kwargs):
            return {"success": False, "error": "no project"}

    out = asyncio.run(_tool(monkeypatch, _Dead(_BOARD))(mode="purchasing"))
    assert out["ok"] is False and "could not be read" in out["reason"]
