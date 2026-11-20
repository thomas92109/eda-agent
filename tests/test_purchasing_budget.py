# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The Budgeting BOM, with synthetic data only.

The pure part is driven with the enriched-BOM shapes of
``test_purchasing_lines``; the tool with the made-up Altium and
distributors of ``test_purchasing_workflow``. Nothing leaves the machine and
files go to a tmp folder through EDA_AGENT_EXPORT_DIR.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from eda_agent.export import purchasing_config as pc
from eda_agent.export.purchasing_budget import (
    BUDGET_HEADERS,
    SUMMARY_PRICED,
    SUMMARY_SUBTOTAL,
    SUMMARY_TOTAL,
    SUMMARY_UNPRICED,
    budget_lines,
    budget_totals,
    build_budget_csv,
)
from eda_agent.libimport.providers import _cache, _distributor, distributors
from eda_agent.libimport.providers.base import CacheMiss
from tests.test_purchasing_lines import analysis, entry, hit, miss, off
from tests.test_purchasing_workflow import (
    _AMP_LINE,
    _URLOPEN,
    _altium,
    _stocked,
    _tools,
    recorded,  # noqa: F401 - a fixture, used by name
)

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path / "exports"))
    return tmp_path / "exports"


def budget(*entries, prefer=None, assemblies=None, **kw):
    args = {k: v for k, v in (("prefer", prefer),
                              ("assemblies", assemblies)) if v is not None}
    return budget_lines(analysis(*entries, **kw),
                        pc.resolve_settings(args or None), now=NOW)


def one(*entries, **kw):
    lines = budget(*entries, **kw)
    assert len(lines) == 1
    return lines[0]


# ---- quantity and price breaks -------------------------------------------------

def test_the_price_break_the_quantity_reaches_is_used():
    # 3 per board x 2 boards = 6, below the 10 break.
    bl = one(entry(qty=3, dk=hit(off(ladder=((1, 0.10), (10, 0.09))))))
    assert (bl.required_qty, bl.budget_qty) == (6, 6)
    assert bl.unit_price == D("0.10") and bl.extended_cost == D("0.60")
    assert bl.break_quantity == 1


def test_the_minimum_order_raises_the_budget_quantity():
    bl = one(entry(qty=2, dk=hit(off(moq=10, ladder=((10, 0.03),
                                                     (100, 0.011))))))
    assert (bl.required_qty, bl.budget_qty) == (4, 10)
    assert bl.extended_cost == D("0.30")


def test_a_higher_break_is_taken_only_when_it_costs_less():
    cheaper = one(entry(qty=4, dk=hit(off(ladder=((1, 0.10), (10, 0.02))))))
    assert (cheaper.budget_qty, cheaper.extended_cost) == (10, D("0.20"))
    dearer = one(entry(qty=4, dk=hit(off(ladder=((1, 0.10), (10, 0.09))))))
    assert (dearer.budget_qty, dearer.extended_cost) == (8, D("0.80"))


def test_the_cheapest_offer_of_a_distributor_is_used():
    bl = one(entry(qty=2, dk=hit(
        off("REEL", moq=2500, ladder=((2500, 0.01),)),
        off("CUT", ladder=((1, 0.20),)))))
    assert bl.sku == "CUT" and bl.extended_cost == D("0.80")


# ---- stock never blocks a price ---------------------------------------------------

@pytest.mark.parametrize("stock,status", [(0, "out of stock"), (1, "short"),
                                          (None, "not stated"),
                                          (10000, "in stock")])
def test_stock_is_shown_but_never_blocks_the_price(stock, status):
    bl = one(entry(qty=2, dk=hit(off(stock=stock, ladder=((1, 0.50),)))))
    assert bl.priced and bl.extended_cost == D("2.00")
    assert bl.stock_status == status
    flagged = any("priced for budgeting only" in n for n in bl.notes)
    assert flagged is (status in ("short", "out of stock"))


def test_a_purchase_limit_does_not_block_the_price_either():
    found = hit(off(ladder=((1, 1.0),)))
    found["offers"][0]["max_order_qty"] = 1
    assert one(entry(qty=5, dk=found)).extended_cost == D("10.0")


# ---- where the price comes from -------------------------------------------------

def test_current_cached_and_missing_prices_are_told_apart():
    lines = budget(
        entry(mpn="A", dk=hit(off(), cached=False)),
        entry(mpn="B", dk=hit(off(), cached=True,
                              retrieved="2026-11-18T10:00:00Z")),
        entry(mpn="C", dk=miss("not_found"), mo=miss("not_found")))
    assert [bl.price_source for bl in lines] == ["current", "cached",
                                                 "missing"]
    assert lines[1].retrieved_at == "2026-11-18T10:00:00Z"
    assert lines[2].extended_cost is None and lines[2].unit_price is None
    assert "digikey: not_found" in " ".join(lines[2].notes)


def test_an_old_cached_price_is_noted():
    bl = one(entry(dk=hit(off(), cached=True,
                          retrieved="2026-11-17T12:00:00Z")))
    assert bl.price_source == "cached"
    assert any("24.0 hours ago" in n for n in bl.notes)


def test_a_listing_without_a_usable_price_is_missing():
    bl = one(entry(dk=hit(off(ladder=())), mo=hit(off(currency=None))))
    assert bl.price_source == "missing"
    assert "no usable price" in " ".join(bl.notes)


# ---- substitutions -------------------------------------------------------------------

def test_an_unapproved_close_candidate_is_never_priced():
    e = entry(dk=miss("not_found"), mo=miss("not_found"))
    e["close_matches"] = [{"label": "Formatting Match", "used": False,
                           "candidate_mpn": "ACM-R-0100X"}]
    bl = one(e)
    assert bl.price_source == "missing" and bl.extended_cost is None
    assert "not approved" in " ".join(bl.notes)


def test_a_variant_without_approval_is_never_priced():
    bl = one(entry(dk=hit(off(), mpn_variant={
        "approved": False, "candidate_mpn": "ACM-R-0100X",
        "label": "Possible Match"})))
    assert bl.price_source == "missing"


def test_an_authorized_variant_is_priced_and_labelled():
    bl = one(entry(dk=hit(off(ladder=((1, 0.25),)), mpn_variant={
        "approved": True, "candidate_mpn": "ACM-R-0100-T",
        "label": "Formatting Match",
        "approval": {"method": "global rule: Formatting Match"}})))
    assert bl.priced and bl.matched_mpn == "ACM-R-0100-T"
    assert bl.mpn_basis.startswith("approved variant (Formatting Match")
    exact = one(entry(dk=hit(off())))
    assert exact.mpn_basis == "exact" and exact.matched_mpn == ""


# ---- which distributor ---------------------------------------------------------------

def test_the_preferred_distributor_falls_back_with_a_note():
    bl = one(entry(dk=miss("not_found"), mo=hit(off("MO-1"))),
             prefer="digikey")
    assert bl.distributor == "mouser"
    assert any(n.startswith("priced at mouser: digikey") for n in bl.notes)


def test_lowest_takes_the_cheaper_distributor():
    bl = one(entry(dk=hit(off("DK", ladder=((1, 0.30),))),
                   mo=hit(off("MO", ladder=((1, 0.20),)))), prefer="lowest")
    assert bl.distributor == "mouser"


# ---- every component is a row; totals ---------------------------------------------

def test_unresolved_components_are_rows_without_a_price():
    lines = budget(entry(dk=hit(off())), unresolved=[{
        "designator": "J9", "reason": "no Part Number parameter",
        "manufacturer": "Acme", "part_number": ""}])
    assert len(lines) == 2
    assert lines[1].kind == "unresolved" and not lines[1].priced
    assert lines[1].required_qty == 2
    assert "no Part Number" in lines[1].notes[0]


def test_an_incomplete_subtotal_is_never_called_the_total():
    totals = budget_totals(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=miss("not_found"), mo=miss("not_found"))))
    assert totals["priced_subtotal"] == {"USD": "2.00"}
    assert totals["unpriced_lines"] == 1 and totals["priced_lines"] == 1
    assert totals["estimated_total"] is None
    assert totals["pricing_complete"] is False
    assert "not the total BOM cost" in totals["note"]


def test_a_fully_priced_bom_has_an_estimated_total():
    totals = budget_totals(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=hit(off(stock=0, ladder=((1, 0.50),))))))
    assert totals["estimated_total"] == "3.00"
    assert totals["pricing_complete"] is True
    assert totals["priced_but_stock_short"] == 1


def test_currencies_are_never_added_together():
    totals = budget_totals(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=hit(off(currency="EUR", ladder=((1, 1.00),))))))
    assert totals["priced_subtotal"] == {"USD": "2.00", "EUR": "2.00"}
    assert totals["estimated_total"] is None


# ---- the CSV -------------------------------------------------------------------------

_SUMMARY = {SUMMARY_SUBTOTAL, SUMMARY_PRICED, SUMMARY_UNPRICED, SUMMARY_TOTAL}


def split(text):
    """The CSV's line rows, and its summary rows by label (a list each)."""
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    lines = [r for r in rows if r["Line"] not in _SUMMARY]
    summary = {}
    for r in rows:
        if r["Line"] in _SUMMARY:
            summary.setdefault(r["Line"], []).append(r)
    return lines, summary


def test_the_csv_has_every_row_and_leaves_missing_prices_empty():
    built = build_budget_csv(budget(
        entry(mpn="A", maker="=evil()", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=miss("not_found"), mo=miss("not_found"))))
    rows, _ = split(built["text"])
    assert built["headers"] == list(BUDGET_HEADERS) and len(rows) == 2
    assert built["rows"] == 2
    priced, missing = rows
    assert priced["Extended Cost"] == "2.0000"
    assert priced["Price Source"] == "current"
    assert priced["Manufacturer"] == "'=evil()"
    assert missing["Price Source"] == "missing"
    assert missing["Unit Price"] == missing["Extended Cost"] == ""
    assert any("no price" in w for w in built["warnings"])


def test_the_csv_ends_with_a_priced_subtotal_never_called_the_total():
    built = build_budget_csv(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=miss("not_found"), mo=miss("not_found"))))
    _, summary = split(built["text"])
    (subtotal,) = summary[SUMMARY_SUBTOTAL]
    assert (subtotal["Extended Cost"], subtotal["Currency"]) == ("2.00", "USD")
    assert "NOT the total BOM cost" in subtotal["Description"]
    assert "not the total BOM cost" in subtotal["Notes"]
    assert summary[SUMMARY_PRICED][0]["BOM Qty"] == "1"
    assert summary[SUMMARY_UNPRICED][0]["BOM Qty"] == "1"
    assert SUMMARY_TOTAL not in summary
    assert built["totals"]["estimated_total"] is None


def test_the_csv_gives_an_estimated_total_only_when_every_line_is_priced():
    built = build_budget_csv(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=hit(off(stock=0, ladder=((1, 0.50),))))))
    _, summary = split(built["text"])
    (total,) = summary[SUMMARY_TOTAL]
    assert (total["Extended Cost"], total["Currency"]) == ("3.00", "USD")
    assert summary[SUMMARY_SUBTOTAL][0]["Extended Cost"] == "3.00"
    assert summary[SUMMARY_UNPRICED][0]["BOM Qty"] == "0"
    assert "NOT" not in summary[SUMMARY_SUBTOTAL][0]["Description"]


def test_an_unresolved_component_keeps_the_csv_from_having_a_total():
    built = build_budget_csv(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        unresolved=[{"designator": "J9", "reason": "no Part Number",
                     "manufacturer": "Acme", "part_number": ""}]))
    _, summary = split(built["text"])
    assert SUMMARY_TOTAL not in summary
    assert summary[SUMMARY_UNPRICED][0]["BOM Qty"] == "1"


def test_the_csv_gives_one_subtotal_per_currency_and_no_total():
    built = build_budget_csv(budget(
        entry(mpn="A", dk=hit(off(ladder=((1, 1.00),)))),
        entry(mpn="B", dk=hit(off(currency="EUR", ladder=((1, 1.00),))))))
    _, summary = split(built["text"])
    assert {(r["Currency"], r["Extended Cost"])
            for r in summary[SUMMARY_SUBTOTAL]} == {("USD", "2.00"),
                                                    ("EUR", "2.00")}
    assert SUMMARY_TOTAL not in summary


# ---- strict cache_only: no distributor is ever asked --------------------------------

@pytest.fixture
def tripwire(monkeypatch):
    """Records every attempt to reach DigiKey or Mouser: the HTTP layer,
    the shared transport, DigiKey's token and authorised calls. A test
    passes only if this stays empty."""
    calls = []

    def wire(request, timeout=None):
        calls.append(("urlopen", getattr(request, "full_url", request)))
        raise AssertionError("a distributor was asked under cache_only")

    def record(label):
        def method(self, *args, **kwargs):
            calls.append((label, args[:1]))
            raise AssertionError(f"{label} was called under cache_only")
        return method

    monkeypatch.setattr(_URLOPEN, wire)
    monkeypatch.setattr(_distributor.DistributorProvider, "_request",
                        record("_request"))
    monkeypatch.setattr(distributors.DigiKeyProvider, "_token",
                        record("digikey._token"))
    monkeypatch.setattr(distributors.DigiKeyProvider, "_api",
                        record("digikey._api"))
    return calls


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("MOUSER_API_KEY", "test-mouser-key")
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE", "1")
    for var, _ in distributors.DigiKeyProvider._LOCALE_HEADERS:
        monkeypatch.delenv(var, raising=False)


_UNCACHED_LINE = {"manufacturer": "Acme", "part_number": "ACM-R-0100",
                  "quantity": 1, "designators": ["R9"],
                  "description": "RES 100R 0603"}


def budget_rows(out):
    with open(out["output_path"], encoding="utf-8-sig", newline="") as f:
        rows, summary = split(f.read())
    return {r["Part Number"]: r for r in rows}, summary


def test_cache_only_never_calls_fetch_and_raises_a_miss(credentials):
    def fetch():
        raise AssertionError("fetch was called under cache_only")

    with _cache.cache_only():
        with pytest.raises(CacheMiss):
            _cache.cached_call("mouser", "u", {"q": 1}, {}, fetch,
                               kinds=lambda r: ["pricing"])
        with _cache.fresh():        # fresh never overrides cache_only
            with pytest.raises(CacheMiss):
                _cache.cached_call("mouser", "u", {"q": 1}, {}, fetch,
                                   kinds=lambda r: ["pricing"])
    assert not _cache.cache_only_active()


def test_cache_only_with_the_cache_turned_off_asks_nobody(monkeypatch,
                                                          credentials,
                                                          tripwire):
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE", "0")
    out = run(lines=_AMP_LINE, cache_only=True, project_path=None)
    assert out["ok"], out
    assert tripwire == []
    assert out["totals"]["priced_lines"] == 0


def test_cache_only_on_an_empty_cache_asks_nobody(credentials, tripwire):
    out = run(lines=[*_AMP_LINE, _UNCACHED_LINE], cache_only=True,
              project_path=None)
    assert out["ok"], out
    assert tripwire == []
    rows, summary = budget_rows(out)
    for row in rows.values():
        assert row["Price Source"] == "missing"
        assert "not_cached" in row["Notes"]
    assert SUMMARY_TOTAL not in summary
    assert out["settings"]["cache_only"] is True
    for name in ("digikey", "mouser"):
        assert out["distributors"][name]["not_cached"] == 2
        assert out["distributors"][name]["read_now"] == 0
    assert any(w.startswith("cache_only:") for w in out["warnings"])


def test_cache_only_prices_from_the_cache_and_asks_nobody(recorded,
                                                         monkeypatch):
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE", "1")
    # A normal run fills the cache from the recorded replies.
    seeded = asyncio.run(_tools()["part_purchasing_analyze"](
        lines=_AMP_LINE))
    assert seeded["ok"] and recorded.requests
    # Then every way to the distributors is cut.
    calls = []
    monkeypatch.setattr(_URLOPEN, lambda request, timeout=None: (
        calls.append(request.full_url), AssertionError("asked"))[1])
    monkeypatch.setattr(_distributor.DistributorProvider, "_request",
                        lambda self, url, **kw: calls.append(url))
    monkeypatch.setattr(distributors.DigiKeyProvider, "_token",
                        lambda self: calls.append("token"))

    out = run(lines=[*_AMP_LINE, _UNCACHED_LINE], cache_only=True,
              project_path=None)
    assert out["ok"], out
    assert calls == []
    rows, summary = budget_rows(out)
    amp, res = rows["LM358DR"], rows["ACM-R-0100"]
    assert amp["Price Source"] == "cached" and amp["Extended Cost"]
    assert res["Price Source"] == "missing" and "not_cached" in res["Notes"]
    assert SUMMARY_TOTAL not in summary
    assert summary[SUMMARY_UNPRICED][0]["BOM Qty"] == "1"


def test_cache_only_and_refresh_are_refused_together(credentials, tripwire):
    out = run(lines=_AMP_LINE, cache_only=True, refresh=True,
              project_path=None)
    assert out["ok"] is False and "contradict" in out["reason"]
    assert tripwire == []


# ---- the tool ---------------------------------------------------------------------------

def run(**kw):
    kw.setdefault("project_path", "DemoBoard.PrjPcb")
    return asyncio.run(_tools()["part_purchasing_budget_export_csv"](**kw))


def test_the_tool_writes_the_budget_to_the_export_folder(monkeypatch,
                                                         folder):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    out = run()
    assert out["ok"], out
    assert out["output_path"].startswith(str(folder / "bom"))
    assert out["output_path"].endswith("DemoBoard_budget_BOM_2x.csv")
    assert "text" not in out and "lines" not in out       # never the BOM
    with open(out["output_path"], encoding="utf-8-sig", newline="") as f:
        rows, summary = split(f.read())
    # R1-R3, C1 and U1 fitted in the Lite variant, plus the unresolved J1.
    assert out["rows"] == len(rows) == 4
    assert out["summary_rows"] == sum(len(v) for v in summary.values())
    assert SUMMARY_TOTAL not in summary
    assert out["totals"]["unpriced_lines"] == 1
    assert out["totals"]["estimated_total"] is None
    assert set(out["totals"]["priced_subtotal"]) == {"USD"}
    assert any("not the total BOM cost" in w for w in out["warnings"])
    # The purchasing export was not written.
    assert not list((folder / "bom").glob("*purchase_BOM*"))


def test_parts_not_fitted_in_the_variant_are_left_out(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    out = run()
    assert out["ok"], out
    assert out["variant"] == "Lite"
    rows, _ = budget_rows(out)
    designators = {d.strip() for r in rows.values()
                   for d in r["Reference Designator"].split(",")}
    # R4 and C2 are Not Fitted in the Lite variant: no row, no quantity.
    assert "R4" not in designators and "C2" not in designators
    res, cap = rows["RC0603FR-0710KL"], rows["GRM155R71C104KA88D"]
    assert (res["Reference Designator"], res["BOM Qty"]) == ("R1, R2, R3", "3")
    assert (cap["Reference Designator"], cap["BOM Qty"]) == ("C1", "1")
    assert res["Required Qty"] == "6"           # 3 per board x 2 boards


def test_the_tool_can_spare_digikey(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    _altium(monkeypatch)
    out = run(distributors=["mouser"])
    assert out["ok"], out
    assert dk.asked == [] and mo.asked
    assert out["settings"]["distributors"] == ["mouser"]


@pytest.mark.parametrize("bad", [[], ["farnell"], ["mouser", "x"]])
def test_an_unknown_distributor_is_refused(monkeypatch, bad):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    out = run(distributors=bad)
    assert out["ok"] is False and "refused" in out["reason"]
