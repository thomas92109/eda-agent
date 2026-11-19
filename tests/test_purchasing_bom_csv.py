# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The purchasing BOM as a CSV: exact columns, Altium's values, no overwrite.

The formatter is pure, so most of this runs with no Altium. The tool tests
drive proj_export_purchasing_bom_csv through the real purchasing BOM
builder with a fake bridge. Every part, maker, designator and board here is
made up. Every file is written under pytest's tmp_path.
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from eda_agent.export.destination import EXPORT_DIR_ENV, repo_root
from eda_agent.export.purchasing_bom_csv import (
    COLUMNS,
    DEFAULT_COLUMNS,
    ColumnSelectionError,
    build_purchasing_csv,
    neutralize_formula,
    resolve_columns,
)
from eda_agent.tools import project

DEFAULT_HEADERS = ["Quantity", "Part Number", "Description", "Manufacturer",
                   "Reference Designator"]
LEADING_ZERO = "0012345678"


def _line(maker, number, designators, description="", footprints=("R0603",)):
    return {"manufacturer": maker, "part_number": number,
            "description": description, "quantity": len(designators),
            "designators": list(designators), "footprints": list(footprints)}


def _bom(lines, not_fitted=(), unresolved=(), not_in_bom=(),
         inconsistencies=()):
    fitted = sum(line["quantity"] for line in lines) + len(unresolved)
    return {"summary": {"components": fitted + len(not_fitted) + len(not_in_bom),
                        "fitted_components": fitted,
                        "complete": True},
            "lines": list(lines), "unresolved": list(unresolved),
            "not_fitted": list(not_fitted), "not_in_bom": list(not_in_bom),
            "inconsistencies": list(inconsistencies)}


# In the order the builder sorts them: maker, then part number.
_LINES = [
    _line("Acme Passives", "AP-0402-100N", ["C1", "C2"], "CAP CER 0.1UF",
          ("C0402",)),
    _line("Acme Passives", "AP-0603-10K", ["R1", "R2", "R10"], "RES 10K",
          ("R0603", "R0603-ALT")),
    _line("Globex Semiconductor", "GS-LDO-33", ["U1"], "IC REG LDO",
          ("MSOP-8",)),
]


def _parsed(text):
    return list(csv.reader(text.splitlines()))


# ---- columns ------------------------------------------------------------------

def test_the_default_is_exactly_the_five_columns_in_order():
    built = build_purchasing_csv(_bom(_LINES))
    rows = _parsed(built["text"])
    assert rows[0] == DEFAULT_HEADERS
    assert built["columns"] == list(DEFAULT_COLUMNS) == [
        "quantity", "part_number", "description", "manufacturer",
        "reference_designators"]
    assert rows[1] == ["2", "AP-0402-100N", "CAP CER 0.1UF", "Acme Passives",
                       "C1, C2"]
    assert rows[2] == ["3", "AP-0603-10K", "RES 10K", "Acme Passives",
                       "R1, R2, R10"]
    assert all(len(r) == 5 for r in rows)


def test_leaving_columns_out_is_the_same_as_none_and_as_the_default_list():
    bom = _bom(_LINES)
    assert build_purchasing_csv(bom, None)["text"] \
        == build_purchasing_csv(bom, list(DEFAULT_COLUMNS))["text"]


@pytest.mark.parametrize("columns, headers", [
    (["part_number", "quantity"], ["Part Number", "Quantity"]),
    (["manufacturer", "part_number", "footprint", "fitted_status"],
     ["Manufacturer", "Part Number", "Footprint", "Fitted / DNP Status"]),
    (["reference_designators"], ["Reference Designator"]),
    (["fitted_status", "quantity", "description"],
     ["Fitted / DNP Status", "Quantity", "Description"]),
    (list(COLUMNS), ["Quantity", "Part Number", "Description",
                     "Manufacturer", "Reference Designator", "Footprint",
                     "Fitted / DNP Status"]),
])
def test_custom_columns_are_exactly_those_in_the_order_asked(columns, headers):
    built = build_purchasing_csv(_bom(_LINES), columns)
    rows = _parsed(built["text"])
    assert rows[0] == headers and built["headers"] == headers
    assert built["columns"] == columns
    assert all(len(r) == len(columns) for r in rows)


def test_custom_columns_carry_the_right_values():
    rows = _parsed(build_purchasing_csv(
        _bom(_LINES), ["footprint", "fitted_status", "part_number"])["text"])
    assert rows[2] == ["R0603, R0603-ALT", "Fitted", "AP-0603-10K"]
    assert rows[3] == ["MSOP-8", "Fitted", "GS-LDO-33"]


@pytest.mark.parametrize("columns, fragment", [
    ([], "empty"),
    (["quantity", "colour"], "unknown: 'colour'"),
    (["quantity", "quantity"], "listed more than once: 'quantity'"),
    (["Quantity"], "unknown: 'Quantity'"),
    ([""], "unknown: ''"),
    (["quantity", 3], "not text: 3"),
    ("quantity", "must be a list"),
    ({"quantity": 1}, "must be a list"),
])
def test_unknown_duplicate_and_empty_selections_are_refused(columns, fragment):
    with pytest.raises(ColumnSelectionError, match=fragment):
        resolve_columns(columns)
    with pytest.raises(ColumnSelectionError):
        build_purchasing_csv(_bom(_LINES), columns)


# ---- Altium's values ----------------------------------------------------------

def test_every_designator_and_every_value_is_kept():
    crowd = [f"C{n}" for n in range(1, 601)]
    bom = _bom([_line("Acme Passives", "AP-0402-100N", crowd,
                      "CAP 100nF ±10%, X7R")])
    rows = _parsed(build_purchasing_csv(bom)["text"])
    assert rows[1][0] == "600"
    assert rows[1][4].split(", ") == crowd
    assert rows[1][2] == "CAP 100nF ±10%, X7R"


def test_the_file_is_utf8_with_a_mark_and_crlf_for_excel():
    from eda_agent.export.purchasing_bom_csv import ENCODING
    text = build_purchasing_csv(_bom(_LINES[:1]))["text"]
    assert text.count("\r\n") == 2 and "\n" not in text.replace("\r\n", "")
    assert text.encode(ENCODING).startswith(b"\xef\xbb\xbf")


def test_a_leading_zero_part_number_is_written_exactly():
    bom = _bom([_line("Initech Connectors", LEADING_ZERO, ["J1"], "Connector",
                      ("HDR-2X7",))])
    built = build_purchasing_csv(bom)
    raw = built["text"].encode("utf-8")
    assert f",{LEADING_ZERO},".encode() in raw
    assert f"'{LEADING_ZERO}".encode() not in raw
    assert f'"{LEADING_ZERO}'.encode() not in raw
    assert _parsed(built["text"])[1][1] == LEADING_ZERO


# ---- DNP: the case that must never pass for a part to buy -----------------------

def _dnp_case():
    fitted = _bom([_line("Acme Passives", "AP-0603-10K", ["R1", "R2"], "RES 10K"),
                   _line("Acme Passives", "AP-0402-100N", ["C1"], "CAP")],
                  not_fitted=["R3", "R4", "C9"])
    everything = _bom([
        _line("Acme Passives", "AP-0603-10K", ["R1", "R2", "R3", "R4"], ""),
        _line("Acme Passives", "AP-0402-100N", ["C1", "C9"], "CAP")])
    return fitted, everything


_WITH_STATUS = ["quantity", "part_number", "reference_designators",
                "fitted_status"]


def test_dnp_parts_are_left_out_and_the_default_is_unchanged_when_not_asked():
    fitted, everything = _dnp_case()
    plain = build_purchasing_csv(fitted)
    assert _parsed(plain["text"])[0] == DEFAULT_HEADERS
    designators = [d for r in _parsed(plain["text"])[1:]
                   for d in r[4].split(", ")]
    assert sorted(designators) == ["C1", "R1", "R2"]
    assert plain["totals"]["dnp_rows"] == 0
    assert plain["totals"]["not_fitted_components"] == 3
    assert plain["totals"]["dnp_not_in_bom_components"] is None
    # Handing over the second read changes nothing unless DNP is asked for.
    same = build_purchasing_csv(fitted, None, everything, include_dnp=False)
    assert same["text"] == plain["text"]


def test_dnp_rows_are_marked_dnp_and_their_purchase_quantity_is_zero():
    fitted, everything = _dnp_case()
    built = build_purchasing_csv(fitted, _WITH_STATUS, everything)
    rows = _parsed(built["text"])[1:]
    assert sorted(rows) == sorted([
        ["2", "AP-0603-10K", "R1, R2", "Fitted"],
        ["0", "AP-0603-10K", "R3, R4", "DNP"],
        ["1", "AP-0402-100N", "C1", "Fitted"],
        ["0", "AP-0402-100N", "C9", "DNP"]])
    status = _WITH_STATUS.index("fitted_status")
    quantity = _WITH_STATUS.index("quantity")
    assert all(r[quantity] == "0" for r in rows if r[status] == "DNP")
    assert all(int(r[quantity]) > 0 for r in rows if r[status] == "Fitted")
    # What a buyer would order is the fitted quantity and nothing more.
    assert sum(int(r[quantity]) for r in rows) == 3
    assert built["totals"]["fitted_quantity"] == 3
    assert built["totals"]["dnp_rows"] == 2
    assert built["totals"]["dnp_components"] == 3
    assert not built["warnings"]


def test_dnp_requested_in_the_asked_order_without_adding_columns():
    fitted, everything = _dnp_case()
    asked = ["fitted_status", "reference_designators", "part_number"]
    built = build_purchasing_csv(fitted, asked, everything)
    assert built["columns"] == asked
    assert _parsed(built["text"])[0] == [
        "Fitted / DNP Status", "Reference Designator", "Part Number"]


@pytest.mark.parametrize("columns", [
    None, list(DEFAULT_COLUMNS), ["quantity", "part_number"],
    ["reference_designators", "footprint"]])
def test_include_dnp_without_fitted_status_is_refused(columns):
    fitted, everything = _dnp_case()
    for call in (lambda: resolve_columns(columns, include_dnp=True),
                 lambda: build_purchasing_csv(fitted, columns, everything),
                 lambda: build_purchasing_csv(fitted, columns, None,
                                              include_dnp=True)):
        with pytest.raises(ColumnSelectionError) as refused:
            call()
        assert "fitted_status" in str(refused.value)
        assert "never added for you" in str(refused.value)


def test_the_refusal_explains_the_unset_columns_case():
    with pytest.raises(ColumnSelectionError, match="columns is not set"):
        resolve_columns(None, include_dnp=True)
    with pytest.raises(ColumnSelectionError, match="not among the columns"):
        resolve_columns(["quantity"], include_dnp=True)


def test_a_fitted_row_keeps_its_own_description_beside_a_dnp_row():
    fitted, everything = _dnp_case()
    rows = _parsed(build_purchasing_csv(
        fitted, ["part_number", "description", "fitted_status"],
        everything)["text"])[1:]
    assert ["AP-0603-10K", "RES 10K", "Fitted"] in rows
    assert ["AP-0603-10K", "", "DNP"] in rows


def test_dnp_requested_with_nothing_not_fitted_invents_no_rows():
    built = build_purchasing_csv(_bom(_LINES), _WITH_STATUS, _bom(_LINES))
    assert built["totals"]["dnp_rows"] == 0 and built["rows"] == 3
    assert built["totals"]["dnp_not_in_bom_components"] == 0


# ---- what each excluded part is, from Altium's own lists ---------------------------

def test_not_fitted_parts_are_told_apart_from_altiums_lists():
    fitted = _bom([_line("Acme Passives", "AP-0603-10K", ["R1"], "RES")],
                  not_fitted=["R3", "R4", "C9", "X5"])
    everything = _bom(
        [_line("Acme Passives", "AP-0603-10K", ["R1", "R3"], "RES")],
        unresolved=[{"designator": "C9", "reason": "no Part Number"}],
        not_in_bom=["R4"])
    built = build_purchasing_csv(fitted, _WITH_STATUS, everything)
    totals = built["totals"]
    assert totals["not_fitted_components"] == 4
    assert totals["dnp_components"] == 1
    assert totals["dnp_not_in_bom_components"] == 1
    assert totals["dnp_unresolved_components"] == 1
    assert totals["dnp_unclassified_components"] == 1
    assert (totals["dnp_components"] + totals["dnp_not_in_bom_components"]
            + totals["dnp_unresolved_components"]
            + totals["dnp_unclassified_components"]) == 4
    said = {w for w in built["warnings"]}
    kind = next(w for w in said if "R4" in w)
    assert "Component Kind" in kind and "Manufacturer" not in kind
    missing = next(w for w in said if "C9" in w)
    assert "Manufacturer or Part Number" in missing
    assert "Component Kind" not in missing
    unknown = next(w for w in said if "X5" in w)
    assert "none of Altium's lists" in unknown
    assert "Manufacturer" not in unknown and "Component Kind" not in unknown


def test_the_three_kinds_of_exclusion_are_counted_apart_for_fitted_parts_too():
    bom = _bom(_LINES, not_fitted=["R7", "R8"], not_in_bom=["MH1", "MH2", "MH3"],
               unresolved=[{"designator": "R9", "reason": "no Part Number"}])
    totals = build_purchasing_csv(bom)["totals"]
    assert totals["not_fitted_components"] == 2
    assert totals["not_in_bom_components"] == 3
    assert totals["unresolved_components"] == 1
    assert totals["fitted_quantity"] == 6
    assert (totals["fitted_quantity"] + totals["unresolved_components"]
            + totals["not_in_bom_components"]
            + totals["not_fitted_components"]) == totals["components"] == 12


def test_a_part_that_is_both_dnp_and_not_in_bom_is_not_called_unresolved():
    fitted = _bom([_line("Acme Passives", "AP-0603-10K", ["R1"], "RES")],
                  not_fitted=["TP1"])
    everything = _bom([_line("Acme Passives", "AP-0603-10K", ["R1"], "RES")],
                      not_in_bom=["TP1"])
    built = build_purchasing_csv(fitted, _WITH_STATUS, everything)
    assert built["totals"]["dnp_not_in_bom_components"] == 1
    assert built["totals"]["dnp_unresolved_components"] == 0
    assert not any("Manufacturer" in w for w in built["warnings"])


def test_counts_that_do_not_add_up_to_the_components_say_so():
    bom = _bom(_LINES, not_fitted=["R7"])
    bom["summary"]["components"] = 50
    assert any("CROSS-CHECK FAILED" in w and "50 components" in w
               for w in build_purchasing_csv(bom)["warnings"])


# ---- spreadsheets ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "=cmd|' /C calc'!A0", "+1+1", "-5V LDO", "@SUM(A1:A9)", "\t=1+1",
    "\r=1+1", "  =1+1"])
def test_a_value_a_spreadsheet_would_run_gets_an_apostrophe(text):
    safe, changed = neutralize_formula(text)
    assert changed and safe == "'" + text


@pytest.mark.parametrize("text", [
    "", "Acme", "AP-0603-10K", "5-1", "1+1 amp", "a=b", "C1, C2", "'=x",
    "±1%"])
def test_other_values_are_not_touched(text):
    assert neutralize_formula(text) == (text, False)


def test_formula_cells_are_neutralized_in_the_file_and_counted():
    bom = _bom([_line("=HYPERLINK(\"http://x\")", "+CMD", ["R1"], "@ev"),
                _line("Acme Passives", "AP-0603", ["R2"], "RES")])
    built = build_purchasing_csv(bom)
    rows = _parsed(built["text"])
    assert rows[1][1:4] == ["AP-0603", "RES", "Acme Passives"] or rows[2][1:4] \
        == ["AP-0603", "RES", "Acme Passives"]
    everything = [cell for r in rows[1:] for cell in r]
    assert "'+CMD" in everything and "'@ev" in everything
    assert "'=HYPERLINK(\"http://x\")" in everything
    assert not any(c[:1] in "=+@" for c in everything if c)
    assert any("3 cell(s)" in w and "apostrophe" in w
               for w in built["warnings"])
    assert rows[1][0].isdigit() and rows[2][0].isdigit()


def test_values_excel_would_reinterpret_are_counted_not_altered():
    bom = _bom([_line("Initech Connectors", LEADING_ZERO, ["J1"], "Connector",
                      ("0402",)),
                _line("X", "1E5", ["R1"], "d"),
                _line("Y", "5-1", ["R2"], "d"),
                _line("Z", "AP-0603", ["R3"], "d")])
    built = build_purchasing_csv(bom, ["part_number", "footprint"])
    rows = _parsed(built["text"])
    assert [LEADING_ZERO, "0402"] in rows and ["1E5", "R0603"] in rows
    warning = next(w for w in built["warnings"] if "reinterpret" in w)
    assert "Part Number 3 cell(s)" in warning and "Footprint 1" in warning


def test_plain_values_raise_no_excel_warning():
    assert not build_purchasing_csv(_bom(_LINES))["warnings"]


# ---- warnings and totals ----------------------------------------------------------

def test_what_is_not_in_the_file_is_in_the_warnings():
    unresolved = [{"designator": "R9", "reason": "no Part Number"}]
    bom = _bom(_LINES, unresolved=unresolved, not_in_bom=["MH1"],
               inconsistencies=[{"issue": "one part on several footprints"}])
    built = build_purchasing_csv(bom)
    warnings = " | ".join(built["warnings"])
    assert "R9" in warnings and "NOT in this CSV" in warnings
    assert "MH1" in warnings and "Component Kind" in warnings
    assert "1 x one part on several footprints" in warnings
    assert built["totals"]["unresolved_components"] == 1
    assert "R9" not in built["text"]


def test_an_empty_description_is_reported_only_when_it_is_exported():
    bom = _bom([_line("Umbrella Hardware", "UH-STANDOFF", ["H1"], "")])
    assert any("empty Description" in w
               for w in build_purchasing_csv(bom)["warnings"])
    assert not build_purchasing_csv(bom, ["part_number"])["warnings"]


def test_totals_agree_with_altiums_counts():
    built = build_purchasing_csv(_bom(_LINES, not_fitted=["R5"]))
    totals = built["totals"]
    assert totals["bom_lines"] == 3 and totals["fitted_quantity"] == 6
    assert totals["fitted_components"] == 6 and totals["components"] == 7
    assert not built["warnings"]


def test_rows_that_do_not_add_up_to_altiums_count_say_so():
    bom = _bom(_LINES)
    bom["summary"]["fitted_components"] = 99
    assert any("CROSS-CHECK FAILED" in w
               for w in build_purchasing_csv(bom)["warnings"])


# ---- the tool ---------------------------------------------------------------------

def _comp(designator, maker, number, description="", footprint="R0603",
          kind=None):
    params = {"Manufacturer": maker, "Part Number": number,
              "Description": description}
    if kind:
        params["Component Kind"] = kind
    return {"designator": designator, "footprint": footprint,
            "parameters": params}


_BOARD = [
    _comp("R1", "Acme Passives", "AP-0603-10K", "RES 10K"),
    _comp("R2", "Acme Passives", "AP-0603-10K", "RES 10K"),
    _comp("R3", "Acme Passives", "AP-0603-10K", "RES 10K"),
    _comp("C1", "Acme Passives", "AP-0402-100N", "CAP 0.1UF", "C0402"),
    _comp("J1", "Initech Connectors", LEADING_ZERO, "Connector", "HDR-2X7"),
    _comp("U1", "Globex Semiconductor", "GS-LDO-33", "IC LDO", "MSOP-8"),
]
_PARTS = ("AP-0603-10K", "AP-0402-100N", "GS-LDO-33", LEADING_ZERO)
_VARIANT = {"name": "Lite", "description": "", "variations": [
    {"designator": "R3", "kind": "Not Fitted", "alternate_part": "",
     "parameters": []}]}


class _Bridge:
    """Altium, as far as the purchasing BOM and the project name go."""

    def __init__(self, board=_BOARD, variants=(_VARIANT,), active="Lite",
                 limit_hit=False, skipped_nil=0):
        self.board, self.variants, self.active = board, list(variants), active
        self.limit_hit, self.skipped_nil = limit_hit, skipped_nil
        self.sent = []

    async def send_command_async(self, command, params=None, **kwargs):
        self.sent.append((command, dict(params or {})))
        if command == "project.get_bom":
            rows = [{"designator": c["designator"], "footprint": c["footprint"]}
                    for c in self.board]
            return {"components": rows, "count": len(rows),
                    "limit": int(params["limit"]),
                    "limit_hit": self.limit_hit,
                    "skipped_nil": self.skipped_nil}
        if command == "project.get_active_variant":
            return {"name": self.active, "description": ""}
        if command == "project.get_variants":
            return {"variants": self.variants, "count": len(self.variants)}
        if command == "project.get_component_info_batch":
            asked = params["designators"].split("~~")
            found = [{"designator": c["designator"],
                      "parameters": c["parameters"]}
                     for c in self.board if c["designator"] in asked]
            return {"components": found, "matched": len(found),
                    "requested": len(asked), "not_found": []}
        if command == "project.get_focused":
            return {"project_name": "DemoBoard.PrjPcb",
                    "project_path": r"C:\x\DemoBoard.PrjPcb"}
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
    return captured["proj_export_purchasing_bom_csv"]


@pytest.fixture
def exports(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "exports"
    monkeypatch.setenv(EXPORT_DIR_ENV, str(folder))
    return folder


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def test_the_default_export_is_the_five_columns_in_the_export_folder(
        monkeypatch, exports):
    run = _tool(monkeypatch, _Bridge())
    out = asyncio.run(run())
    assert out["ok"], out
    path = Path(out["output_path"])
    assert path == exports / "bom" / "DemoBoard_purchasing_BOM.csv"
    rows = _read(path)
    assert rows[0] == DEFAULT_HEADERS and out["headers"] == DEFAULT_HEADERS
    by_part = {r[1]: r for r in rows[1:]}
    assert by_part["AP-0603-10K"] == [
        "2", "AP-0603-10K", "RES 10K", "Acme Passives", "R1, R2"]
    assert by_part[LEADING_ZERO][0] == "1"
    assert out["rows"] == len(rows) - 1 == 4
    assert out["variant"] == "Lite" and not out["include_dnp"]
    assert out["totals"]["fitted_quantity"] == 5
    assert out["totals"]["not_fitted_components"] == 1
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_a_leading_zero_part_number_is_exact_in_the_raw_file(
        monkeypatch, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge())())
    raw = Path(out["output_path"]).read_bytes()
    assert f",{LEADING_ZERO},".encode() in raw
    assert f"'{LEADING_ZERO}".encode() not in raw
    assert f'"{LEADING_ZERO}'.encode() not in raw
    assert any("reinterpret" in w and "Part Number 1 cell(s)" in w
               for w in out["warnings"]), "Excel's habit is warned about"


def test_the_reply_is_a_summary_not_the_bom(monkeypatch, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge())())
    text = json.dumps(out)
    assert "lines" not in out
    for secret in (*_PARTS, "R1, R2"):
        assert secret not in text


STAMP = "20261117_153000"


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime

    from eda_agent.export import destination
    monkeypatch.setattr(destination, "_now",
                        lambda: datetime(2026, 11, 17, 15, 30, 0))


def test_a_second_export_never_replaces_the_first(monkeypatch, exports, clock):
    run = _tool(monkeypatch, _Bridge())
    first = asyncio.run(run())
    before = Path(first["output_path"]).read_bytes()
    assert Path(first["output_path"]).name == "DemoBoard_purchasing_BOM.csv"
    second = asyncio.run(run())
    assert second["ok"]
    assert Path(second["output_path"]).name         == f"DemoBoard_purchasing_BOM_{STAMP}.csv"
    third = asyncio.run(run())
    assert Path(third["output_path"]).name         == f"DemoBoard_purchasing_BOM_{STAMP}_2.csv"
    assert Path(first["output_path"]).read_bytes() == before
    assert any("already exists" in w and f"_{STAMP}.csv" in w
               and "nothing was overwritten" in w for w in second["warnings"])
    assert not any("already exists" in w for w in first["warnings"])


def test_an_explicit_path_that_exists_gets_a_timestamp_untouched(
        monkeypatch, tmp_path, exports, clock):
    target = tmp_path / "mine" / "bom.csv"
    target.parent.mkdir()
    target.write_text("keep me", encoding="utf-8")
    out = asyncio.run(_tool(monkeypatch, _Bridge())(output_path=str(target)))
    assert out["ok"], out
    assert Path(out["output_path"]) == tmp_path / "mine" / f"bom_{STAMP}.csv"
    assert target.read_text(encoding="utf-8") == "keep me"
    assert _read(out["output_path"])[0] == DEFAULT_HEADERS
    fresh = tmp_path / "new" / "deeper" / "bom.csv"
    made = asyncio.run(_tool(monkeypatch, _Bridge())(output_path=str(fresh)))
    assert made["ok"] and fresh.is_file()
    assert Path(made["output_path"]) == fresh
    assert not any("already exists" in w for w in made["warnings"])


def test_a_path_inside_the_public_repo_is_refused_and_nothing_is_written(
        monkeypatch, exports):
    root = repo_root()
    out = asyncio.run(_tool(monkeypatch, _Bridge())(
        output_path=str(root / "docs" / ".." / "purchasing.csv")))
    assert not out["ok"] and "public repository" in out["reason"]
    assert not (root / "purchasing.csv").exists()


def test_a_folder_is_not_a_file_name(monkeypatch, tmp_path, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge())(output_path=str(tmp_path)))
    assert not out["ok"] and "is a folder" in out["reason"]


def test_custom_columns_through_the_tool(monkeypatch, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge())(
        columns=["manufacturer", "reference_designators", "footprint",
                 "fitted_status"]))
    assert out["ok"]
    rows = _read(out["output_path"])
    assert rows[0] == ["Manufacturer", "Reference Designator", "Footprint",
                       "Fitted / DNP Status"]
    assert ["Acme Passives", "R1, R2", "R0603", "Fitted"] in rows
    assert all(len(r) == 4 for r in rows)


@pytest.mark.parametrize("columns", [[], ["quantity", "quantity"], ["nope"],
                                     "quantity"])
def test_bad_columns_are_refused_before_altium_is_asked(
        monkeypatch, exports, columns):
    bridge = _Bridge()
    out = asyncio.run(_tool(monkeypatch, bridge)(columns=columns))
    assert not out["ok"] and "columns" in out["reason"]
    assert bridge.sent == [] and not exports.exists()


@pytest.mark.parametrize("columns", [None, ["quantity", "part_number"],
                                     list(DEFAULT_COLUMNS)])
def test_include_dnp_without_fitted_status_is_refused_before_altium_is_asked(
        monkeypatch, exports, columns):
    bridge = _Bridge()
    out = asyncio.run(_tool(monkeypatch, bridge)(
        include_dnp=True, columns=columns))
    assert not out["ok"]
    assert "fitted_status" in out["reason"]
    assert "never added for you" in out["reason"]
    assert bridge.sent == [] and not exports.exists()


def test_without_include_dnp_the_default_stays_the_five_columns(
        monkeypatch, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge())(include_dnp=False))
    assert out["ok"] and out["headers"] == DEFAULT_HEADERS
    assert out["columns"] == list(DEFAULT_COLUMNS)


def test_dnp_rows_come_only_when_asked_for(monkeypatch, exports):
    bridge = _Bridge()
    run = _tool(monkeypatch, bridge)
    plain = asyncio.run(run(columns=["reference_designators"]))
    assert [r[0] for r in _read(plain["output_path"])[1:]].count("R3") == 0
    assert [c for c, _ in bridge.sent].count("project.get_bom") == 1

    bridge.sent.clear()
    columns = ["quantity", "part_number", "reference_designators",
               "fitted_status"]
    out = asyncio.run(run(include_dnp=True, columns=columns))
    assert out["ok"] and out["include_dnp"] and out["columns"] == columns
    rows = _read(out["output_path"])[1:]
    assert ["0", "AP-0603-10K", "R3", "DNP"] in rows
    assert ["2", "AP-0603-10K", "R1, R2", "Fitted"] in rows
    assert sum(int(r[0]) for r in rows) == 5, "DNP adds nothing to order"
    assert out["totals"]["dnp_rows"] == 1
    assert out["totals"]["dnp_components"] == 1
    assert out["totals"]["fitted_quantity"] == 5
    assert [c for c, _ in bridge.sent].count("project.get_bom") == 2


def test_nothing_not_fitted_means_no_second_read(monkeypatch, exports):
    bridge = _Bridge(variants=[], active="[No Variations]")
    out = asyncio.run(_tool(monkeypatch, bridge)(
        include_dnp=True, columns=["part_number", "fitted_status"]))
    assert out["ok"] and out["totals"]["dnp_rows"] == 0
    assert out["totals"]["dnp_not_in_bom_components"] == 0
    assert [c for c, _ in bridge.sent].count("project.get_bom") == 1


def test_not_fitted_and_not_in_bom_are_told_apart_through_the_tool(
        monkeypatch, exports):
    """A DNP part whose Component Kind keeps it out of the BOM is not
    called unresolved, and a fitted one with the same kind is counted
    on its own."""
    board = _BOARD + [
        _comp("TP1", "", "", "", "TP", kind="Standard (No BOM)"),
        _comp("TP2", "", "", "", "TP", kind="Standard (No BOM)"),
        _comp("R9", "Acme Passives", ""),
    ]
    variant = {"name": "Lite", "description": "", "variations": [
        {"designator": "R3", "kind": "Not Fitted", "alternate_part": "",
         "parameters": []},
        {"designator": "TP1", "kind": "Not Fitted", "alternate_part": "",
         "parameters": []}]}
    out = asyncio.run(_tool(monkeypatch, _Bridge(board, [variant]))(
        include_dnp=True, columns=["quantity", "part_number", "fitted_status"]))
    assert out["ok"], out
    t = out["totals"]
    assert t["not_fitted_components"] == 2          # R3 and TP1: DNP
    assert t["not_in_bom_components"] == 1          # TP2: fitted, kept out
    assert t["unresolved_components"] == 1          # R9: no part number
    assert t["dnp_components"] == 1                 # R3 has a DNP row
    assert t["dnp_not_in_bom_components"] == 1      # TP1: DNP and not in BOM
    assert t["dnp_unresolved_components"] == 0
    assert t["dnp_unclassified_components"] == 0
    assert t["fitted_quantity"] + t["unresolved_components"] \
        + t["not_in_bom_components"] + t["not_fitted_components"] \
        == t["components"] == len(board)
    tp1 = next(w for w in out["warnings"]
               if "TP1" in w and "no DNP row" in w)
    assert "Component Kind" in tp1 and "Manufacturer" not in tp1
    assert any("R9" in w and "NOT in this CSV" in w for w in out["warnings"])
    assert any("TP2" in w and "Component Kind" in w for w in out["warnings"])


def test_a_cut_short_read_is_refused_by_default_and_the_message_is_clean(
        monkeypatch, exports):
    bridge = _Bridge(limit_hit=True)
    run = _tool(monkeypatch, bridge)
    import inspect
    assert inspect.signature(run).parameters["allow_incomplete"].default is False
    refused = asyncio.run(run())
    assert not refused["ok"] and not refused["complete"]
    assert "incomplete" in refused["reason"] and "limit=1000" in refused["reason"]
    assert ".." not in refused["reason"]
    assert refused["reason"].endswith("to write it anyway.")
    assert not exports.exists()
    allowed = asyncio.run(run(allow_incomplete=True))
    assert allowed["ok"] and not allowed["complete"]
    incomplete = [w for w in allowed["warnings"] if w.startswith("INCOMPLETE")]
    assert len(incomplete) == 1 and "do not order" in incomplete[0]
    assert not any(".." in w for w in allowed["warnings"])


def test_an_unread_part_of_the_project_is_refused_with_a_clean_message(
        monkeypatch, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge(skipped_nil=2))())
    assert not out["ok"] and not out["complete"]
    assert "2 object(s)" in out["reason"] and ".." not in out["reason"]
    assert not exports.exists()


def test_a_component_without_a_part_number_is_warned_about_not_exported(
        monkeypatch, exports):
    board = _BOARD + [_comp("R9", "Acme Passives", "")]
    out = asyncio.run(_tool(monkeypatch, _Bridge(board))())
    assert out["ok"] and out["totals"]["unresolved_components"] == 1
    assert any("R9" in w and "NOT in this CSV" in w for w in out["warnings"])
    assert all("R9" not in r[4].split(", ") for r in _read(out["output_path"])[1:])


def test_a_formula_in_altiums_data_is_defused_in_the_file(
        monkeypatch, exports):
    board = [_comp("R1", "Acme Passives", "=2+2", "@evil")]
    out = asyncio.run(_tool(monkeypatch, _Bridge(
        board, variants=[], active="[No Variations]"))())
    rows = _read(out["output_path"])
    assert rows[1] == ["1", "'=2+2", "'@evil", "Acme Passives", "R1"]
    assert any("apostrophe" in w for w in out["warnings"])


def test_an_empty_bom_writes_nothing(monkeypatch, exports):
    out = asyncio.run(_tool(monkeypatch, _Bridge(
        [], variants=[], active="[No Variations]"))())
    assert not out["ok"] and "no lines" in out["reason"]
    assert not exports.exists()


def test_the_default_name_uses_the_project_path_when_one_is_given(
        monkeypatch, exports):
    bridge = _Bridge()
    out = asyncio.run(_tool(monkeypatch, bridge)(
        project_path=r"C:\work\Lamp Board.PrjPcb"))
    assert Path(out["output_path"]).name == "Lamp_Board_purchasing_BOM.csv"
    assert "project.get_focused" not in [c for c, _ in bridge.sent]
