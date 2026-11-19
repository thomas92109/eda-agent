# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The Purchasing BOM CSV and its tool, end to end with synthetic data.

Altium and both distributors are the stand-ins of
``test_purchasing_workflow``: nothing leaves the machine. Files go to a
tmp folder through EDA_AGENT_EXPORT_DIR.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

import pytest

from eda_agent.export import destination, purchasing_config as pc
from eda_agent.export.purchasing_bom_csv import ColumnSelectionError
from eda_agent.export.purchasing_bom_export import (
    COLUMNS,
    DEFAULT_COLUMNS,
    build_purchasing_bom_csv,
    resolve_columns,
)
from eda_agent.export.purchasing_lines import build_purchase_lines
from tests.test_purchasing_lines import analysis, entry, hit, miss, off
from tests.test_purchasing_workflow import (
    _CAP,
    _RES,
    _altium,
    _stocked,
    _tools,
)


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path / "exports"))
    return tmp_path / "exports"


def lines_for(*entries, **kw):
    return build_purchase_lines(
        analysis(*entries, **kw), pc.resolve_settings(),
        now=datetime(2026, 11, 18, 12, tzinfo=timezone.utc))["lines"]


def table(text: str):
    assert text.startswith("﻿") is False       # the BOM is added on write
    return list(csv.DictReader(io.StringIO(text, newline="")))


# ---- columns (PUR-4, PUR-5, TS-P04, TS-P05) -------------------------------------------

def test_the_default_columns_and_the_header():
    assert resolve_columns(None) == list(DEFAULT_COLUMNS)
    built = build_purchasing_bom_csv(lines_for(entry(dk=hit(off()))))
    assert built["headers"] == [COLUMNS[c] for c in DEFAULT_COLUMNS]
    assert built["text"].endswith("\r\n") and "\r\n" in built["text"]


def test_every_documented_column_exists():
    wanted = {"bom_quantity", "required_qty", "purchase_qty", "surplus",
              "part_number", "manufacturer", "description",
              "reference_designators", "footprint", "distributor",
              "distributor_sku", "packaging", "unit_price", "extended_cost",
              "currency", "stock", "lifecycle", "purchasing_status",
              "review_reasons", "quantity_rule", "price_break",
              "retrieved_at"}
    assert wanted <= set(COLUMNS)
    built = build_purchasing_bom_csv(lines_for(entry(dk=hit(off()))),
                                     columns=sorted(COLUMNS))
    row = table(built["text"])[0]
    assert row["Distributor"] == "digikey" and row["Currency"] == "USD"
    assert row["Footprint"] == "0603" and row["Packaging"] == "Cut Tape"
    assert row["Data Retrieved"].startswith("2026-")


def test_columns_are_written_in_the_order_given():
    cols = ["purchasing_status", "part_number", "purchase_qty"]
    built = build_purchasing_bom_csv(lines_for(entry(dk=hit(off()))),
                                     columns=cols)
    assert built["headers"] == ["Purchasing Status", "Part Number",
                                "Purchase Qty"]


@pytest.mark.parametrize("bad", [[], ["nope"], ["part_number", "part_number"],
                                 "part_number", 3, ["part_number", 4]])
def test_a_bad_column_choice_is_refused(bad):
    with pytest.raises(ColumnSelectionError):
        resolve_columns(bad)


def test_include_dnp_needs_a_status_column():
    with pytest.raises(ColumnSelectionError):
        resolve_columns(["part_number"], include_dnp=True)
    assert resolve_columns(None, include_dnp=True)


def test_configured_columns_are_the_default_and_arguments_win():
    assert resolve_columns(None, ["part_number"]) == ["part_number"]
    assert resolve_columns(["surplus"], ["part_number"]) == ["surplus"]
    with pytest.raises(ColumnSelectionError):
        resolve_columns(None, ["bogus"])


# ---- rows ----------------------------------------------------------------------------------

def test_a_blocked_line_is_a_row_with_its_reasons_and_no_quantity_to_buy():
    lines = lines_for(
        entry(maker="A", mpn="1", dk=hit(off("D")), mo=hit(off("M"))),
        entry(maker="B", mpn="2", dk=hit(off("D2", stock=0)),
              mo=hit(off("M2", stock=0))),
        unresolved=[{"designator": "C9", "reason": "no Part Number parameter",
                     "description": "CAP", "manufacturer": "Acme",
                     "part_number": ""}])
    built = build_purchasing_bom_csv(lines, columns=[
        "required_qty", "purchase_qty", "part_number", "reference_designators",
        "purchasing_status", "review_reasons", "review_detail",
        "distributor"])
    rows = table(built["text"])
    assert len(rows) == 3
    ok, empty, unresolved = rows
    assert ok["Purchasing Status"] == "ready" and ok["Purchase Qty"]
    assert empty["Purchasing Status"] == "blocked"
    assert empty["Purchase Qty"] == "" and empty["Distributor"] == ""
    assert empty["Required Qty"] == "2" and "OUT_OF_STOCK" in empty[
        "Review Reasons"]
    assert unresolved["Reference Designator"] == "C9"
    assert unresolved["Purchasing Status"] == "blocked"
    assert "no Part Number parameter" in unresolved["Review Detail"]
    assert built["counts"]["blocked"] == 2
    assert any("BLOCKED" in w for w in built["warnings"])


def test_reasons_are_listed_blocking_first_then_by_code():
    lines = lines_for(entry(dk=hit(off(stock=None, moq=10),
                                   lifecycle="nrnd", text="NRND")))
    cell = table(build_purchasing_bom_csv(
        lines, columns=["review_reasons"])["text"])[0]["Review Reasons"]
    parts = cell.split(" | ")
    assert parts == sorted(parts, key=lambda c: (
        {"MOQ_ADJUSTED": 2}.get(c, 1), c)) or len(parts) >= 3
    assert "LIFECYCLE_NRND" in parts and "STOCK_NOT_STATED" in parts


def test_a_twenty_times_surplus_row_shows_it_and_stays_ready():
    lines = lines_for(entry(qty=25, dk=hit(off(
        ladder=((1, 0.10), (1000, 0.011))))))
    row = table(build_purchasing_bom_csv(lines, columns=[
        "required_qty", "purchase_qty", "surplus", "purchasing_status",
        "quantity_rule", "extended_cost"])["text"])[0]
    assert (row["Required Qty"], row["Purchase Qty"], row["Surplus"]) == (
        "50", "1000", "950")
    assert row["Purchasing Status"] == "ready"
    assert row["Quantity Rule"].startswith("paid_upgrade: ")
    assert row["Extended Cost"] == "11.0000"


# TS-P10
def test_distributor_text_is_neutralized_like_the_boms_own():
    lines = lines_for(entry(
        maker="=cmd()", mpn="0012345678", description="@SUM(1)",
        dk=hit(off("-1234-ND"), match="partial", maker="+Evil")))
    built = build_purchasing_bom_csv(lines, columns=[
        "manufacturer", "part_number", "description", "distributor_sku",
        "review_detail"])
    row = table(built["text"])[0]
    assert row["Manufacturer"] == "'=cmd()"
    assert row["Description"] == "'@SUM(1)"
    assert row["Distributor SKU"] == "'-1234-ND"
    assert row["Review Detail"].startswith("MANUFACTURER_PARTIAL_MATCH")
    assert row["Part Number"] == "0012345678"        # kept, only warned about
    text = " ".join(built["warnings"])
    assert "leading apostrophe" in text and "Part Number" in text


def test_the_bom_count_cross_check():
    lines = lines_for(entry(qty=3, dk=hit(off())))
    ok = build_purchasing_bom_csv(
        lines, bom_summary={"fitted_components": 3, "components": 3})
    assert not any("CROSS-CHECK" in w for w in ok["warnings"])
    bad = build_purchasing_bom_csv(
        lines, bom_summary={"fitted_components": 4, "components": 4})
    assert any("CROSS-CHECK FAILED" in w for w in bad["warnings"])


# ---- the tool -----------------------------------------------------------------------------

def call(**kw):
    kw.setdefault("project_path", "DemoBoard.PrjPcb")
    return asyncio.run(_tools()["part_purchasing_export_csv"](**kw))


def read(reply):
    with open(reply["output_path"], "rb") as handle:
        raw = handle.read()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"),
                                           newline="")))


def test_the_tool_exports_every_line_verified_field_by_field(
        monkeypatch, folder):
    dk, mo = _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    out = call(columns=["bom_quantity", "required_qty", "purchase_qty",
                        "surplus", "part_number", "manufacturer",
                        "reference_designators", "distributor",
                        "distributor_sku", "unit_price", "extended_cost",
                        "currency", "stock", "lifecycle", "purchasing_status",
                        "review_reasons", "price_break"])
    assert out["ok"], out
    assert out["output_path"].endswith("DemoBoard_purchase_BOM_2x.csv")
    assert out["output_path"].startswith(str(folder))
    assert "text" not in out and "lines" not in out        # never the BOM
    rows = read(out)
    # R1-R3, C1, U1 fitted (R4 and C2 are not fitted in the Lite variant),
    # plus the unresolved J1
    assert out["rows"] == len(rows) == 4
    by = {r["Part Number"]: r for r in rows if r["Part Number"]}
    res, cap = by[_RES], by["GRM155R71C104KA88D"]
    for row in rows:
        assert int(row["Required Qty"]) == int(row["BOM Qty"]) * 2
    assert (res["BOM Qty"], res["Required Qty"], res["Purchase Qty"]) == (
        "3", "6", "10")
    assert res["Distributor"] == "digikey"
    assert res["Distributor SKU"] == "311-10.0KHRCT-ND"
    assert res["Unit Price"] == "0.028" and res["Extended Cost"] == "0.2800"
    assert res["Price Break"] == "10" and res["Surplus"] == "4"
    assert res["Reference Designator"] == "R1, R2, R3"
    assert (cap["Required Qty"], cap["Purchase Qty"]) == ("2", "10")
    assert res["Purchasing Status"] == "ready" and res["Currency"] == "USD"
    unresolved = next(r for r in rows if r["Reference Designator"] == "J1")
    assert unresolved["Purchasing Status"] == "blocked"
    assert unresolved["Required Qty"] == "2" and unresolved["Purchase Qty"] == ""
    assert "UNRESOLVED_COMPONENT" in unresolved["Review Reasons"]
    assert sum(int(r["BOM Qty"]) for r in rows
               if r["Part Number"]) == out["counts"]["bom_quantity"] == 5
    assert out["totals"]["by_currency"]["USD"]["lines"] == 3
    assert out["settings"]["assemblies"] == {"value": 2, "source": "config"}
    assert out["settings"]["config_file"]["created_now"] is True
    assert (folder / "config.toml").is_file()
    assert out["summary"]["by_status"]["blocked"] >= 1
    assert "project.get_focused" not in altium.sent       # name came from path


def test_no_cart_or_list_is_asked_for(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    _altium(monkeypatch)
    assert call()["ok"]
    from eda_agent.tools import purchasing
    import inspect
    source = inspect.getsource(purchasing)
    for word in ("MouserCart", "DigiKeyLists", "create_cart", "add_items",
                 "create_list", "mouser_cart", "digikey_lists"):
        assert word not in source


def test_arguments_override_the_file_and_the_reply_says_so(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    out = call(assemblies=5, price_break_window=1, extra_spend_flat="0",
               columns=["required_qty"])
    assert out["ok"] and out["assemblies"] == 5
    assert out["settings"]["assemblies"] == {"value": 5, "source": "argument"}
    assert out["settings"]["extra_spend_flat"]["value"] == "0"
    assert out["settings"]["extra_spend_percent"]["source"] == "config"
    assert {r["Required Qty"] for r in read(out)} == {"15", "5"}


@pytest.mark.parametrize("kw", [
    dict(assemblies=0), dict(assemblies=True), dict(extra_spend_flat=-1),
    dict(price_break_window=1.5), dict(prefer="farnell"),
    dict(columns=["nope"]), dict(columns=[]), dict(limit=0),
    dict(include_dnp=True, columns=["part_number"]),
])
def test_bad_arguments_are_refused_before_anything_is_read(monkeypatch, kw,
                                                           folder):
    dk, mo = _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    out = call(**kw)
    assert out["ok"] is False and out["reason"]
    assert altium.sent == [] and dk.asked == [] and mo.asked == []
    assert not list(folder.glob("**/*.csv"))


def test_a_cut_short_read_is_refused_before_a_distributor_is_asked(
        monkeypatch, folder):
    dk, mo = _stocked(monkeypatch)
    _altium(monkeypatch, limit_hit=True)
    out = call()
    assert out["ok"] is False and out["complete"] is False
    assert "allow_incomplete=True" in out["reason"]
    assert ".." not in out["reason"] and dk.asked == [] and mo.asked == []
    assert not list(folder.glob("**/*.csv"))


def test_allow_incomplete_writes_it_with_every_row_blocked(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch, limit_hit=True)
    out = call(allow_incomplete=True, columns=[
        "purchasing_status", "review_reasons"])
    assert out["ok"] and out["complete"] is False
    assert any("INCOMPLETE" in w for w in out["warnings"])
    for row in read(out):
        assert row["Purchasing Status"] == "blocked"
        assert "BOM_INCOMPLETE" in row["Review Reasons"]


def test_dnp_rows_need_the_status_and_are_not_bought(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    _altium(monkeypatch)
    plain = call(columns=["part_number"])
    asked = len(dk.asked)
    out = call(include_dnp=True, columns=[
        "required_qty", "purchase_qty", "reference_designators",
        "purchasing_status"])
    assert out["ok"] and out["include_dnp"] is True
    rows = read(out)
    dnp = [r for r in rows if r["Purchasing Status"] == "not_fitted"]
    assert sorted(r["Reference Designator"] for r in dnp) == ["C2", "R4"]
    assert all(r["Required Qty"] == "0" and r["Purchase Qty"] == "0"
               for r in dnp)
    assert out["rows"] == len(read(plain)) + 2
    assert len(dk.asked) - asked == asked       # no extra lookups for DNP rows


def test_an_export_is_never_overwritten(monkeypatch, folder):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    monkeypatch.setattr(destination, "_now",
                        lambda: datetime(2026, 11, 18, 15, 30, 0))
    first = call(columns=["part_number"])
    second = call(columns=["part_number"])
    assert first["output_path"] != second["output_path"]
    assert second["output_path"].endswith(
        "DemoBoard_purchase_BOM_2x_20261118_153000.csv")
    assert any("nothing was overwritten" in w for w in second["warnings"])
    assert len(list((folder / "bom").glob("*.csv"))) == 2


def test_a_path_inside_the_repository_is_refused(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    inside = str(destination.repo_root() / "docs" / "leak.csv")
    out = call(output_path=inside)
    assert out["ok"] is False and "repository" in out["reason"].lower()
    assert not (destination.repo_root() / "docs" / "leak.csv").exists()


def test_lines_given_directly_do_not_touch_altium(monkeypatch):
    _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    out = call(project_path=None, lines=[
        {"manufacturer": "Yageo", "part_number": _RES, "quantity": 10,
         "description": '=HYPERLINK("x")',
         "designators": [f"R{n}" for n in range(101, 111)]}],
        columns=["description", "required_qty", "purchase_qty"])
    assert out["ok"] and altium.sent == []
    assert out["output_path"].endswith("purchasing_purchase_BOM_2x.csv")
    row = read(out)[0]
    assert row["Description"] == "'=HYPERLINK(\"x\")"
    assert row["Required Qty"] == "20"


def test_the_default_preference_is_digikey_and_lowest_is_selectable(
        monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    cols = ["part_number", "distributor", "purchasing_status"]
    default = read(call(columns=cols))
    assert {r["Distributor"] for r in default if r["Distributor"]} \
        == {"digikey"}
    lowest = read(call(columns=cols, prefer="lowest"))
    assert any(r["Distributor"] == "mouser" for r in lowest)


def test_a_project_that_cannot_be_read_is_reported(monkeypatch):
    _stocked(monkeypatch)
    from eda_agent import bridge as bridge_module

    class _Broken:
        async def send_command_async(self, command, params, **kw):
            return {"error": "no project"}

    monkeypatch.setattr(bridge_module, "get_bridge", lambda: _Broken())
    out = call()
    assert out["ok"] is False and "could not be read" in out["reason"]
