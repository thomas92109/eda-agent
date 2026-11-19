# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Purchase lines: distributor choice, stock limits, review reasons, status.

Synthetic data only (Acme, Globex, Initech, Umbrella).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from eda_agent.export import purchasing_config as pc
from eda_agent.export.purchasing_lines import build_purchase_lines
from eda_agent.export.purchasing_model import PurchasingStatus as S


@pytest.fixture(autouse=True)
def _export_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path))


def settings(**args):
    return pc.resolve_settings(args or None)


def off(sku="ACM-1", stock=10000, ladder=((1, 0.10),), moq=1, multiple=None,
        currency="USD", packaging=("Cut Tape",), standard_package=None,
        fee=None):
    return {"sku": sku, "packaging": list(packaging), "stock": stock,
            "moq": moq, "multiple": multiple,
            "standard_package": standard_package, "currency": currency,
            "price_breaks": [{"quantity": q, "unit_price": p}
                             for q, p in ladder], "reeling_fee": fee}


def hit(*offers, lifecycle="active", text="Active", maker="Acme", match="exact",
        cached=False, retrieved="2026-11-18T08:00:00Z", **extra):
    return {"status": "matched", "manufacturer": maker,
            "manufacturer_match": match, "lifecycle": lifecycle,
            "lifecycle_text": text, "offers": list(offers), "cached": cached,
            "retrieved_at": retrieved, **extra}


def miss(status, reason="x"):
    return {"status": status, "reason": reason}


def entry(dk=None, mo=None, maker="Acme", mpn="ACM-R-0100", qty=1,
          designators=("R1",), description="RES"):
    dist = {}
    if dk is not None:
        dist["digikey"] = dk
    if mo is not None:
        dist["mouser"] = mo
    return {"line": 0, "manufacturer": maker, "part_number": mpn,
            "description": description, "designators": list(designators),
            "footprints": ["0603"], "quantity_per_board": qty,
            "required_quantity": qty * 2, "distributors": dist}


def analysis(*entries, unresolved=(), inconsistencies=(), complete=True):
    return {"lines": list(entries), "unresolved": list(unresolved),
            "inconsistencies": list(inconsistencies),
            "bom_complete": complete, "not_fitted": [], "not_in_bom": []}


def build(an, **args):
    return build_purchase_lines(an, settings(**args),
                                now=datetime(2026, 11, 18, 12, 0,
                                             tzinfo=timezone.utc))


def only(result):
    assert len(result["lines"]) == 1
    return result["lines"][0]


def codes(pl):
    return {r.code for r in pl.reasons}


# ---- distributor choice (PUR-18, TS-P18) ---------------------------------------------

def test_digikey_is_preferred_even_when_mouser_is_cheaper():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1", ladder=((1, 0.20),))),
        mo=hit(off("MO-1", ladder=((1, 0.05),)))))))
    assert pl.offer.distributor == "digikey" and pl.status is S.READY
    assert "FALLBACK_DISTRIBUTOR" not in codes(pl)


def test_mouser_is_the_fallback_when_digikey_is_short():
    pl = only(build(analysis(entry(
        qty=100, dk=hit(off("DK-1", stock=50)),
        mo=hit(off("MO-1", stock=5000))))))
    assert pl.offer.distributor == "mouser"
    assert "FALLBACK_DISTRIBUTOR" in codes(pl)
    assert pl.status is S.READY                    # information only


@pytest.mark.parametrize("digikey", [
    miss("not_found", "no such part"), miss("unavailable", "no credential"),
    hit(off("DK-1", ladder=())), hit(off("DK-1", currency=None)),
])
def test_mouser_is_the_fallback_when_digikey_gives_no_acceptable_offer(digikey):
    pl = only(build(analysis(entry(dk=digikey, mo=hit(off("MO-1"))))))
    assert pl.offer.distributor == "mouser"
    assert "FALLBACK_DISTRIBUTOR" in codes(pl)


def test_prefer_mouser_swaps_the_roles():
    pl = only(build(analysis(entry(dk=hit(off("DK-1")), mo=hit(off("MO-1")))),
                    prefer="mouser"))
    assert pl.offer.distributor == "mouser"


def test_lowest_picks_the_cheaper_final_cost():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1", ladder=((1, 0.20),))),
        mo=hit(off("MO-1", ladder=((1, 0.05),))))), prefer="lowest"))
    assert pl.offer.distributor == "mouser"
    assert pl.alternatives[0]["distributor"] == "digikey"


def test_lowest_with_two_currencies_takes_preference_order_and_says_so():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1")), mo=hit(off("MO-1", currency="EUR")))),
        prefer="lowest"))
    assert pl.offer.distributor == "digikey"
    assert "CURRENCY_MIXED" in codes(pl) and pl.status is S.REVIEW


def test_a_disabled_distributor_is_ignored(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[distributors]\nenabled = ["mouser"]\n', encoding="utf-8")
    pl = only(build(analysis(entry(dk=hit(off("DK-1")), mo=hit(off("MO-1"))))))
    assert pl.offer.distributor == "mouser"


# ---- stock-limited quantities (TS-Q40, TS-Q41) ------------------------------------------

def test_neither_distributor_can_supply_so_the_line_is_blocked_and_kept():
    r = build(analysis(entry(
        qty=50, dk=hit(off("DK-1", stock=60)),
        mo=hit(off("MO-1", stock=80)))))
    pl = only(r)
    assert pl.status is S.BLOCKED and pl.offer is None
    assert "INSUFFICIENT_STOCK" in codes(pl)
    # the quantity it would need is shown; nothing smaller, no partial
    assert pl.decision.purchase_qty == 100 and pl.required_qty == 100
    assert "digikey: 100 are needed and 60 are in stock" in next(
        x.message for x in pl.reasons if x.code == "INSUFFICIENT_STOCK")
    assert r["totals"]["by_currency"] == {}


def test_out_of_stock_everywhere():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1", stock=0)), mo=hit(off("MO-1", stock=0))))))
    assert codes(pl) >= {"OUT_OF_STOCK"} and pl.status is S.BLOCKED
    assert "INSUFFICIENT_STOCK" not in codes(pl)


def test_one_short_and_one_empty_is_insufficient():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1", stock=0)), mo=hit(off("MO-1", stock=1))))))
    assert "INSUFFICIENT_STOCK" in codes(pl) and "OUT_OF_STOCK" not in codes(pl)


def test_the_overage_shrinks_to_stock_and_the_line_stays_ready():
    pl = only(build(analysis(entry(qty=50, dk=hit(off("DK-1", stock=105))))))
    assert pl.offer.distributor == "digikey"
    assert pl.decision.purchase_qty == 105
    assert "OVERAGE_LIMITED_BY_STOCK" in codes(pl) and pl.status is S.READY


# ---- review reasons (REV-1 .. REV-3) --------------------------------------------------------

def test_an_unresolved_component_is_a_blocked_line_of_its_own():
    r = build(analysis(
        unresolved=[{"designator": "C9", "manufacturer": "",
                     "part_number": "", "description": "CAP",
                     "footprint": "0402",
                     "reason": "no Manufacturer parameter; Part Number is empty"}]))
    pl = only(r)
    assert pl.line.kind == "unresolved" and pl.line.designators == ("C9",)
    assert pl.required_qty == 2 and pl.status is S.BLOCKED
    assert {"UNRESOLVED_COMPONENT", "MISSING_MANUFACTURER",
            "MISSING_MPN"} <= codes(pl)
    assert "no Manufacturer parameter" in pl.reasons[0].message


def test_no_line_is_dropped_whatever_goes_wrong():
    r = build(analysis(
        entry(maker="A", mpn="1", dk=hit(off()), mo=hit(off("M"))),
        entry(maker="B", mpn="2", dk=miss("not_found"), mo=miss("not_found")),
        entry(maker="C", mpn="3", dk=hit(off(stock=0)), mo=hit(off(stock=0))),
        entry(maker="D", mpn="4", dk=miss("unavailable"),
              mo=miss("unavailable")),
        unresolved=[{"designator": "C1", "reason": "no Part Number parameter"}]))
    assert len(r["lines"]) == 5
    assert r["summary"]["by_status"] == {"ready": 1, "review": 0, "blocked": 4,
                                         "not_fitted": 0}
    assert r["summary"]["fitted_lines"] == 4
    assert r["summary"]["unresolved_components"] == 1


def test_not_found_mismatch_and_ambiguous_are_blocking_and_substitute_nothing():
    for status, code in (("not_found", "MPN_NOT_FOUND"),
                         ("manufacturer_mismatch", "MANUFACTURER_MISMATCH"),
                         ("ambiguous", "AMBIGUOUS_MANUFACTURER")):
        pl = only(build(analysis(entry(dk=miss(status, "why"),
                                       mo=miss(status, "why")))))
        assert code in codes(pl) and pl.status is S.BLOCKED
        assert pl.offer is None


def test_both_distributors_down_is_no_data_and_one_down_is_information():
    pl = only(build(analysis(entry(dk=miss("unavailable", "no key"),
                                   mo=miss("error", "boom")))))
    assert "NO_DISTRIBUTOR_DATA" in codes(pl) and pl.status is S.BLOCKED
    pl = only(build(analysis(entry(dk=miss("unavailable", "no key"),
                                   mo=hit(off("MO-1"))))))
    assert pl.offer.distributor == "mouser"
    assert {"DISTRIBUTOR_UNAVAILABLE", "FALLBACK_DISTRIBUTOR"} <= codes(pl)
    assert pl.status is S.READY


def test_one_not_found_and_one_unreachable_reports_both():
    pl = only(build(analysis(entry(dk=miss("not_found"),
                                   mo=miss("unavailable", "rate limited")))))
    assert {"MPN_NOT_FOUND", "DISTRIBUTOR_UNAVAILABLE"} <= codes(pl)


@pytest.mark.parametrize("kw,code", [
    (dict(ladder=()), "MISSING_PRICE"),
    (dict(ladder=((1, 0),)), "MISSING_PRICE"),
    (dict(currency=None), "MISSING_CURRENCY"),
])
def test_a_missing_price_or_currency_blocks(kw, code):
    pl = only(build(analysis(entry(dk=hit(off(**kw)), mo=hit(off("M", **kw))))))
    assert code in codes(pl) and pl.status is S.BLOCKED


def test_packaging_reasons_are_information_but_an_assumed_value_is_review():
    pl = only(build(analysis(entry(qty=3, dk=hit(
        off(moq=10, multiple=5))))))
    assert {"MOQ_ADJUSTED"} <= codes(pl) and pl.status is S.READY
    pl = only(build(analysis(entry(qty=3, dk=hit(
        off(moq=None, ladder=((10, 0.10),)))))))
    assert "MOQ_NOT_STATED" in codes(pl) and pl.status is S.REVIEW
    pl = only(build(analysis(entry(qty=3, dk=hit(
        off(moq=1, multiple=None, packaging=("Tape & Reel (TR)",),
            standard_package=3000))))))
    assert {"PACKAGING_REEL_WHOLE", "MULTIPLE_NOT_STATED"} <= codes(pl)
    assert pl.decision.purchase_qty == 3000


def test_a_stock_not_stated_is_review():
    pl = only(build(analysis(entry(dk=hit(off(stock=None))))))
    assert "STOCK_NOT_STATED" in codes(pl) and pl.status is S.REVIEW


def test_a_partial_manufacturer_match_is_review():
    pl = only(build(analysis(entry(dk=hit(off(), match="partial",
                                          maker="Acme Electronics")))))
    assert "MANUFACTURER_PARTIAL_MATCH" in codes(pl)
    assert pl.status is S.REVIEW


def test_bom_conflicts_reach_the_line_they_concern():
    inc = [{"issue": "one part with several descriptions, none chosen",
            "manufacturer": "Acme", "part_number": "ACM-R-0100"},
           {"issue": "one part number under several manufacturers, kept as "
                     "separate lines", "part_number": "ACM-R-0100",
            "manufacturers": ["Acme", "Globex"]},
           {"issue": "designator listed more than once, counted once",
            "designator": "R1"},
           {"issue": "one part on several footprints", "manufacturer": "Other",
            "part_number": "ZZZ"}]
    pl = only(build(analysis(entry(dk=hit(off())), inconsistencies=inc)))
    msg = next(r.message for r in pl.reasons if r.code == "BOM_CONFLICT")
    assert "several descriptions" in msg and "several manufacturers" in msg
    assert "more than once" in msg and "footprints" not in msg
    assert pl.status is S.REVIEW


def test_an_incomplete_read_blocks_every_line():
    r = build(analysis(entry(dk=hit(off())),
                       unresolved=[{"designator": "C1", "reason": "x"}],
                       complete=False))
    assert all("BOM_INCOMPLETE" in codes(pl) and pl.status is S.BLOCKED
               for pl in r["lines"])


def test_one_part_can_carry_many_reasons():
    pl = only(build(analysis(entry(
        qty=3, dk=hit(off(stock=None, moq=10), lifecycle="nrnd",
                      text="Not For New Designs", match="partial",
                      maker="Acme Co")))))
    assert {"MOQ_ADJUSTED", "STOCK_NOT_STATED", "LIFECYCLE_NRND",
            "MANUFACTURER_PARTIAL_MATCH"} <= codes(pl)
    assert len(pl.reasons) >= 4 and pl.status is S.REVIEW


# ---- lifecycle (REV-6, TS-R09 .. TS-R14, TS-R22) ---------------------------------------------

@pytest.mark.parametrize("kw,code,status", [
    (dict(lifecycle="nrnd", text="Not For New Designs"), "LIFECYCLE_NRND",
     S.REVIEW),
    (dict(lifecycle="last_time_buy", text="Last Time Buy",
          last_buy_date="2027-03-31"), "LIFECYCLE_LAST_TIME_BUY", S.REVIEW),
    (dict(lifecycle="end_of_life", text="End of Life"), "LIFECYCLE_EOL",
     S.BLOCKED),
    (dict(lifecycle="obsolete", text="Obsolete"), "LIFECYCLE_OBSOLETE",
     S.BLOCKED),
    (dict(lifecycle="discontinued", text="Discontinued at Digi-Key"),
     "DISTRIBUTOR_DISCONTINUED", S.BLOCKED),
    (dict(lifecycle="active", text="Active", lifecycle_discontinued=True),
     "DISTRIBUTOR_DISCONTINUED", S.BLOCKED),
    (dict(lifecycle="active", text="Active", lifecycle_end_of_life=True),
     "LIFECYCLE_EOL", S.BLOCKED),
    (dict(lifecycle=None, text="Wibble"), "LIFECYCLE_UNKNOWN", S.REVIEW),
    (dict(lifecycle=None, text=None), "LIFECYCLE_UNKNOWN", S.REVIEW),
])
def test_each_lifecycle_state_sets_its_status(kw, code, status):
    pl = only(build(analysis(entry(dk=hit(off(), **kw)))))
    assert code in codes(pl) and pl.status is status
    # the offer is still shown: a blocked part is kept, not dropped
    assert pl.offer is not None


def test_an_end_of_life_part_stays_blocked_whatever_the_settings_say(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[review.severity]\nLIFECYCLE_EOL = "info"\n'
        'LIFECYCLE_OBSOLETE = "info"\nDISTRIBUTOR_DISCONTINUED = "info"\n',
        encoding="utf-8")
    for lc, code in (("end_of_life", "LIFECYCLE_EOL"),
                     ("obsolete", "LIFECYCLE_OBSOLETE"),
                     ("discontinued", "DISTRIBUTOR_DISCONTINUED")):
        pl = only(build(analysis(entry(dk=hit(off(), lifecycle=lc)))))
        assert code in codes(pl) and pl.status is S.BLOCKED


def test_mouser_null_with_digikey_active_is_not_unknown():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1")), mo=hit(off("MO-1"), lifecycle=None, text=None)))))
    assert "LIFECYCLE_NOT_STATED" in codes(pl)
    assert "LIFECYCLE_UNKNOWN" not in codes(pl) and pl.status is S.READY


def test_an_end_of_life_flag_at_the_other_distributor_counts():
    pl = only(build(analysis(entry(
        dk=hit(off("DK-1")),
        mo=hit(off("MO-1"), lifecycle="end_of_life", text="EOL")))))
    assert pl.offer.distributor == "digikey"
    assert "LIFECYCLE_EOL" in codes(pl) and pl.status is S.BLOCKED


def test_a_suggested_replacement_is_text_and_never_the_offer():
    pl = only(build(analysis(entry(dk=hit(
        off("DK-1"), lifecycle="obsolete", text="Obsolete"),
        mo=hit(off("MO-1"), suggested_replacement="GLX-9999")))))
    assert pl.suggested_replacement == "GLX-9999"
    assert "GLX-9999" not in {pl.offer.sku, pl.line.part_number}


# ---- surplus is reported, not policed (REV-8) -----------------------------------------------

LADDER_20X = ((1, 0.10), (1000, 0.011))


def test_a_twenty_times_surplus_is_ready_and_unremarked():
    pl = only(build(analysis(entry(qty=25, dk=hit(off(ladder=LADDER_20X))))))
    assert pl.decision.purchase_qty == 1000 and pl.required_qty == 50
    assert pl.decision.surplus_ratio == 20
    assert pl.status is S.READY and pl.reasons == ()
    assert pl.decision.explanation


def test_the_surplus_warning_is_configurable_and_never_blocks(tmp_path):
    an = analysis(entry(qty=25, dk=hit(off(ladder=LADDER_20X))))
    assert "LARGE_SURPLUS" in codes(only(build(an, large_surplus_ratio=10)))
    pl = only(build(an, large_surplus_ratio=10))
    assert pl.status is S.READY                    # information only
    assert "LARGE_SURPLUS" not in codes(only(build(an, large_surplus_ratio=20)))
    (tmp_path / "config.toml").write_text(
        '[review.severity]\nLARGE_SURPLUS = "review"\n', encoding="utf-8")
    assert only(build(an, large_surplus_ratio=10)).status is S.REVIEW
    (tmp_path / "config.toml").write_text(
        '[review.severity]\nLARGE_SURPLUS = "blocking"\n', encoding="utf-8")
    r = build(an, large_surplus_ratio=10)
    assert only(r).status is S.READY
    assert any("LARGE_SURPLUS" in w for w in r["warnings"])


# ---- duplicates, staleness, totals ------------------------------------------------------------

def test_two_lines_on_one_distributor_number_are_both_blocked():
    r = build(analysis(
        entry(maker="Acme", mpn="1", dk=hit(off("SAME-ND"))),
        entry(maker="Acme Co", mpn="1", dk=hit(off("same-nd")))))
    assert all("DUPLICATE_SKU" in codes(pl) and pl.status is S.BLOCKED
               for pl in r["lines"])


def test_cached_figures_that_are_old_get_a_note():
    old = (datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
           - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pl = only(build(analysis(entry(dk=hit(off(), cached=True, retrieved=old)))))
    assert "STALE_PRICING" in codes(pl) and pl.status is S.READY
    pl = only(build(analysis(entry(dk=hit(off(), cached=False, retrieved=old)))))
    assert "STALE_PRICING" not in codes(pl)


def test_totals_are_per_currency_and_never_combined():
    r = build(analysis(
        entry(maker="A", mpn="1", dk=hit(off("D1", ladder=((1, 1.00),)))),
        entry(maker="B", mpn="2", dk=miss("not_found"),
              mo=hit(off("M2", currency="EUR", ladder=((1, 2.00),))))))
    t = r["totals"]
    assert set(t["by_currency"]) == {"USD", "EUR"} and t["combined"] is None
    # USD: 2 needed, overage 1.10 gives 3 at 1.00; EUR: no overage, 2 at 2.00
    assert t["by_currency"]["USD"]["extended_cost"] == "3.00"
    assert t["by_currency"]["EUR"]["extended_cost"] == "4.00"


def test_a_single_currency_has_a_combined_total():
    r = build(analysis(entry(dk=hit(off(ladder=((1, 1.00),))))),
             overage_factor=1)
    assert r["totals"]["combined"] == "2.00"
    assert "Shipping" in r["totals"]["note"]


def test_the_result_carries_the_settings_and_their_sources():
    r = build(analysis(entry(dk=hit(off()))), price_break_window=1)
    assert r["settings"]["price_break_window"] == {"value": 1,
                                                   "source": "argument"}


def test_dnp_rows_are_listed_with_nothing_to_buy():
    r = build_purchase_lines(
        analysis(entry(dk=hit(off()))), settings(),
        dnp_lines=[{"manufacturer": "Acme", "part_number": "X",
                    "designators": ["R9"], "footprints": ["0603"]}])
    dnp = r["lines"][-1]
    assert dnp.status is S.NOT_FITTED and dnp.required_qty == 0
    assert dnp.offer is None and dnp.reasons == ()
    assert r["summary"]["not_fitted_rows"] == 1
