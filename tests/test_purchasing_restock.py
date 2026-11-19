# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""When stock may come back: incoming lots and lead times in the Purchasing BOM.

Only what a distributor states is reported (Mouser's incoming lots, the
manufacturer's lead time); nothing is estimated. Synthetic data only.
"""

from __future__ import annotations

import csv
import io

import pytest

from eda_agent.export import purchasing_config as pc
from eda_agent.export import purchasing_sources
from eda_agent.export.purchasing_bom_export import (
    COLUMNS,
    DEFAULT_COLUMNS,
    build_purchasing_bom_csv,
)
from eda_agent.export.purchasing_lines import build_purchase_lines
from eda_agent.export.purchasing_sources import enrich_bom
from tests.test_purchasing_lines import (
    analysis,
    build,
    entry,
    hit,
    miss,
    off,
    only,
)
from tests.test_purchasing_workflow import _Shop, _hit, _offer

LOTS = [{"quantity": 52, "date": "2027-01-19"}]
COLS = ["part_number", "purchasing_status", "restock_date", "restock_qty",
        "restock_covered", "restock_detail"]


@pytest.fixture(autouse=True)
def _folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path))


def rows_of(result, columns=COLS):
    text = build_purchasing_bom_csv(result["lines"], columns)["text"]
    return list(csv.DictReader(io.StringIO(text, newline="")))


def mouser_empty(on_order, qty=14, stock=0, **extra):
    return entry(qty=qty, mo=hit(off("MO-1", stock=stock), stock=stock,
                                 on_order=on_order, **extra))


# ---- what a line carries --------------------------------------------------------------

def test_a_line_short_of_stock_carries_the_incoming_lots_and_when_they_cover():
    pl = only(build(analysis(mouser_empty(
        [{"quantity": 10, "date": "2027-04-22"},
         {"quantity": 52, "date": "2027-01-19"},
         {"quantity": 30, "date": "2027-06-01"}]))))
    (row,) = pl.restock                      # needs 28: lots sorted by date
    assert row["distributor"] == "mouser" and row["required"] == 28
    assert [l["date"] for l in row["incoming"]] == [
        "2027-01-19", "2027-04-22", "2027-06-01"]
    assert row["covers_by"] == "2027-01-19"          # 52 >= 28


def test_coverage_is_cumulative_and_counts_stock_already_there():
    pl = only(build(analysis(mouser_empty(
        [{"quantity": 10, "date": "2027-02-01"},
         {"quantity": 10, "date": "2027-03-01"}], stock=15))))
    # 15 now (< 28); 25 after Feb 1; 35 after Mar 1
    assert pl.restock[0]["in_stock"] == 15
    assert pl.restock[0]["covers_by"] == "2027-03-01"


def test_lots_that_never_cover_the_requirement_say_so():
    pl = only(build(analysis(mouser_empty(
        [{"quantity": 5, "date": "2027-02-01"}]))))
    assert pl.restock[0]["covers_by"] is None
    (row,) = rows_of({"lines": [pl]})
    assert row["Next Restock Date"] == "2027-02-01"
    assert row["Next Restock Qty"] == "5"
    assert row["Full Qty Available By"] == \
        "not covered by the incoming stock listed"
    assert "do not cover the 28 needed" in row["Restock Detail"]


def test_a_lot_without_a_date_is_kept_and_never_given_one():
    pl = only(build(analysis(mouser_empty([{"quantity": 100,
                                            "date": None}]))))
    (row,) = rows_of({"lines": [pl]})
    assert pl.restock[0]["covers_by"] == "date not stated"
    assert row["Next Restock Date"] == "" and row["Next Restock Qty"] == ""
    assert row["Full Qty Available By"] == "date not stated"
    assert "100 on a date not stated" in row["Restock Detail"]


def test_digikey_gives_no_lots_so_only_its_lead_time_is_quoted():
    pl = only(build(analysis(entry(
        qty=2, dk=hit(off("DK-1", stock=0), stock=0, lead_time_weeks=17,
                      manufacturer_stock=0)))))
    (row,) = rows_of({"lines": [pl]})
    assert row["Next Restock Date"] == "" and row["Next Restock Qty"] == ""
    assert row["Full Qty Available By"] == "no incoming stock listed"
    assert "digikey: 0 in stock; no incoming stock listed" in \
        row["Restock Detail"]
    assert "manufacturer lead time 17 weeks" in row["Restock Detail"]
    assert row["Purchasing Status"] == "blocked"


def test_the_earliest_lot_over_both_distributors_is_the_next_restock():
    pl = only(build(analysis(entry(
        qty=14,
        dk=hit(off("DK-1", stock=0), stock=0, lead_time_weeks=17),
        mo=hit(off("MO-1", stock=0), stock=0,
               on_order=[{"quantity": 52, "date": "2027-01-19"}])))))
    (row,) = rows_of({"lines": [pl]})
    assert {r["distributor"] for r in pl.restock} == {"digikey", "mouser"}
    assert row["Next Restock Date"] == "2027-01-19"
    assert row["Next Restock Qty"] == "52"
    assert row["Full Qty Available By"] == "2027-01-19"
    assert "digikey: 0 in stock" in row["Restock Detail"] \
        and "mouser: 0 in stock; incoming 52 on 2027-01-19" in \
        row["Restock Detail"]


def test_the_detail_names_the_requirement_and_when_it_was_read():
    pl = only(build(analysis(mouser_empty(LOTS))))
    detail = rows_of({"lines": [pl]})[0]["Restock Detail"]
    assert "covers the 28 needed by 2027-01-19" in detail
    assert "read 2026-11-18" in detail


# ---- lines that can be bought carry nothing -------------------------------------------

def test_a_line_that_can_be_bought_has_no_restock_cells():
    result = build(analysis(entry(
        qty=2, dk=hit(off("DK-1", stock=5000), stock=5000, on_order=LOTS))))
    (row,) = rows_of(result)
    assert only(result).restock == ()
    assert row["Next Restock Date"] == row["Next Restock Qty"] == ""
    assert row["Full Qty Available By"] == row["Restock Detail"] == ""


def test_a_line_bought_at_the_other_distributor_has_none():
    pl = only(build(analysis(entry(
        qty=2, dk=hit(off("DK-1", stock=0), stock=0),
        mo=hit(off("MO-1", stock=9000), stock=9000, on_order=LOTS)))))
    assert pl.offer.distributor == "mouser" and pl.restock == ()


def test_a_line_nobody_matched_has_none():
    pl = only(build(analysis(entry(dk=miss("not_found"),
                                   mo=miss("not_found")))))
    assert pl.restock == ()


# ---- from the distributor's answer to the CSV -------------------------------------

def test_the_source_layer_keeps_mouser_lots_and_digikey_lead_time(monkeypatch):
    mouser = _hit("mouser", "ACM-1", "Acme", [_offer("MO-1", [(1, 0.1)],
                                                      stock=0)], stock=0)
    mouser.sourcing["on_order"] = [{"quantity": 52, "date": "2027-01-19"},
                                   "junk"]
    mouser.sourcing["manufacturer_stock"] = 0
    digikey = _hit("digikey", "ACM-1", "Acme", [_offer("DK-1", [(1, 0.1)],
                                                        stock=0)], stock=0)
    digikey.sourcing["on_order"] = None
    digikey.sourcing["lead_time"] = {"raw": "17", "weeks": 17}
    monkeypatch.setattr(purchasing_sources, "_providers", lambda: {
        "digikey": (_Shop("digikey", [digikey]),
                    purchasing_sources._digikey_hits),
        "mouser": (_Shop("mouser", [mouser]),
                   purchasing_sources._mouser_hits)})
    found = enrich_bom([{"manufacturer": "Acme", "part_number": "ACM-1",
                         "description": "x", "quantity": 7,
                         "designators": ["R1"]}], 2)
    line = found["lines"][0]
    assert line["distributors"]["mouser"]["on_order"] == [
        {"quantity": 52, "date": "2027-01-19"}]
    assert line["distributors"]["digikey"]["on_order"] == []
    assert line["distributors"]["digikey"]["lead_time_weeks"] == 17
    full = {"build_quantity": 2, "bom_complete": True, "unresolved": [],
            "inconsistencies": [], **found}
    pl = only(build_purchase_lines(full, pc.resolve_settings(None)))
    rows = {r["distributor"]: r for r in pl.restock}
    assert rows["mouser"]["covers_by"] == "2027-01-19"      # needs 14
    assert rows["digikey"]["lead_time_weeks"] == 17


def test_the_new_columns_exist_and_the_first_three_are_defaults():
    for name in ("restock_date", "restock_qty", "restock_covered",
                 "restock_detail"):
        assert name in COLUMNS
    assert {"restock_date", "restock_qty", "restock_covered"} \
        <= set(DEFAULT_COLUMNS)
    assert "restock_detail" not in DEFAULT_COLUMNS
    assert COLUMNS["restock_date"] == "Next Restock Date"
    assert pc.TEMPLATE.count("restock_date") == 1
    assert pc.resolve_settings(None).warnings == ()      # the template parses


def test_the_restock_quantity_is_written_as_a_number_not_a_string():
    pl = only(build(analysis(mouser_empty(LOTS))))
    built = build_purchasing_bom_csv([pl], COLS)
    assert built["warnings"] and not any(
        "reinterpret" in w for w in built["warnings"])
    assert ",52," in built["text"]
