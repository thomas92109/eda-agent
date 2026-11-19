# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The quantity engine, against the worked examples in the requirements.

Synthetic ladders only. The scenario ids (TS-Q..) are the ones in
docs/PURCHASING_WORKFLOW_REQUIREMENTS.md.
"""

from __future__ import annotations

import random
from dataclasses import replace
from decimal import Decimal as D

import pytest

from eda_agent.export.purchasing_model import (
    Lifecycle,
    PriceBreak,
    QuantityPolicy,
    SupplierOffer,
)
from eda_agent.export.purchasing_plan import price_offer
from eda_agent.export.purchasing_quantity import allowance, decide_quantity

POLICY = QuantityPolicy()          # 10 percent, 5.00 flat, window 2, overage 1.10


def offer(ladder, **kw) -> SupplierOffer:
    breaks = tuple(PriceBreak(q, D(str(p))) for q, p in ladder)
    base = dict(distributor="digikey", sku="ACM-ND", currency="USD",
                packaging=("Cut Tape",), price_breaks=breaks)
    base.update(kw)
    return SupplierOffer(**base)


def decide(ladder, required, policy=POLICY, **kw):
    return decide_quantity(offer(ladder, **kw), required, policy)


def p(**kw) -> QuantityPolicy:
    return replace(POLICY, **kw)


# ---- TS-Q01 .. TS-Q12 -----------------------------------------------------------

def test_the_defaults_and_no_multiplier():
    assert POLICY.overage_factor == D("1.10")
    assert POLICY.overage_max_unit_price == D("5.00")
    assert POLICY.extra_spend_percent == 10
    assert POLICY.extra_spend_flat == D("5.00")
    assert POLICY.extra_spend_currency == "USD"
    assert POLICY.price_break_window == 2
    assert not any("multiplier" in f for f in POLICY.__dataclass_fields__)


@pytest.mark.parametrize("required,expected", [
    (100, 110),    # float 100 * 1.1 is 110.00000000000001: ceil would be 111
    (110, 121),    # same trap: float gives 122
    (5, 6), (3, 4), (10, 11), (30, 33),
])
def test_overage_is_exact_and_rounded_up(required, expected):
    assert decide([(1, "0.10")], required).purchase_qty == expected


def test_a_quantity_below_the_first_break_and_the_minimum_is_raised_to_both():
    d = decide([(10, "0.50"), (100, "0.40")], 4, moq=None)
    assert d.floor_qty == 10 and d.purchase_qty >= 10
    assert "MOQ_NOT_STATED" in d.flags


def test_minimum_order():
    d = decide([(1, "0.50")], 4, moq=10)
    assert d.floor_qty == 10 and d.purchase_qty == 10      # overage 5 < MOQ
    assert "MOQ_ADJUSTED" in d.flags and d.rule == "moq"


def test_order_multiple():
    d = decide([(1, "0.20")], 11, multiple=5)
    assert d.floor_qty == 15 and d.purchase_qty == 15       # ceil(12.1) = 13 -> 15
    assert "MULTIPLE_ADJUSTED" in d.flags


def test_a_reel_is_sold_whole_and_says_so():
    d = decide([(1, "0.20")], 50, packaging=("Tape & Reel (TR)",),
               standard_package=3000)
    assert d.floor_qty == 3000 and d.purchase_qty == 3000
    assert {"MULTIPLE_NOT_STATED", "PACKAGING_REEL_WHOLE"} <= set(d.flags)
    assert any("reel" in x for x in d.limits_applied)


def test_a_reeling_fee_is_in_every_candidates_cost():
    d = decide([(1, "1.00"), (100, "0.40")], 60, reeling_fee=D("7.00"))
    assert d.baseline_cost == 66 * D("1.00") + 7
    assert all(c.extended_cost >= c.quantity * c.unit_price + 7
               for c in d.candidates)
    assert d.extended_cost == 100 * D("0.40") + 7


def test_stock_between_the_floor_and_the_baseline_reduces_the_overage():
    d = decide([(1, "0.10")], 100, stock=105)
    assert d.purchase_qty == 105 and "OVERAGE_LIMITED_BY_STOCK" in d.flags
    d = decide([(1, "0.10")], 100, stock=100)
    assert d.purchase_qty == 100


def test_the_reduced_overage_respects_the_multiple():
    d = decide([(1, "0.10")], 100, multiple=10, stock=108)
    assert d.purchase_qty == 100 and d.purchase_qty % 10 == 0
    d = decide([(1, "0.10")], 100, multiple=10, stock=115)
    assert d.purchase_qty == 110


def test_a_maximum_order_quantity_rejects_a_candidate_and_says_why():
    d = decide([(1, "2.00"), (1000, "0.50")], 45, max_order_qty=500)
    assert d.purchase_qty == 50
    rejected = [c for c in d.candidates if c.verdict == "rejected"]
    assert rejected and "maximum order quantity 500" in rejected[0].why


def test_never_below_the_requirement_over_generated_ladders():
    rng = random.Random(11)
    for _ in range(400):
        required = rng.randint(1, 600)
        cuts = sorted(rng.sample(range(2, 3000), rng.randint(0, 4)))
        price = D("5.00")
        ladder = [(1, price)]
        for c in cuts:
            price = (price * D(rng.choice(("0.9", "0.5", "0.2", "1.0")))
                     ).quantize(D("0.0001")) or D("0.0001")
            ladder.append((c, price))
        d = decide(ladder, required,
                   policy=p(extra_spend_percent=rng.choice((0, 10, 80)),
                            price_break_window=rng.randint(0, 3)),
                   moq=rng.choice((None, 1, 25)),
                   multiple=rng.choice((None, 5, 100)),
                   stock=rng.choice((None, 50, 400, 100000)),
                   max_order_qty=rng.choice((None, 90, 5000)))
        assert d.purchase_qty >= required
        if d.shortfall is None:
            assert d.purchase_qty >= d.floor_qty
            assert d.extended_cost is not None
        assert d.surplus == d.purchase_qty - required


# ---- TS-Q13 .. TS-Q20: free and paid upgrades --------------------------------------

def test_free_upgrade_small_part():
    d = decide([(1, "1.00"), (100, "0.40")], 60)
    assert (d.purchase_qty, d.rule, d.baseline_cost) == (100, "free_upgrade",
                                                         66)


def test_free_upgrade_expensive_part_at_any_distance():
    d = decide([(1, "12.00"), (250, "2.00")], 60)
    assert (d.purchase_qty, d.rule) == (250, "free_upgrade")
    assert d.baseline_cost == 720 and d.extended_cost == 500
    assert d.surplus_ratio > 4      # 4.2 times: no cap exists


def test_free_upgrade_beyond_the_window_is_found():
    d = decide([(1, "2.00"), (100, "1.10"), (250, "0.60"), (1000, "0.08")], 45)
    assert (d.purchase_qty, d.rule, d.extended_cost) == (1000, "free_upgrade",
                                                        80)


def test_paid_upgrade_expensive_part():
    d = decide([(1, "12.00"), (250, "3.00")], 60)
    assert (d.purchase_qty, d.rule) == (250, "paid_upgrade")
    assert d.baseline_cost == 720 and d.allowance == D("797.000")
    assert d.extended_cost == 750 and d.extra_spend == 30


def test_paid_upgrade_cheap_passive_at_twenty_times_the_requirement():
    d = decide([(1, "0.10"), (1000, "0.011")], 50)
    assert d.baseline_qty == 55 and d.baseline_cost == D("5.50")
    assert d.allowance == D("11.050")
    assert (d.purchase_qty, d.rule) == (1000, "paid_upgrade")
    assert d.extended_cost == D("11.000")
    assert d.surplus_ratio == 20 and d.surplus == 950


def test_paid_upgrade_modest():
    d = decide([(1, "2.00"), (100, "1.10")], 45)
    assert (d.baseline_qty, d.baseline_cost, d.allowance) == (50, 100, 115)
    assert (d.purchase_qty, d.extended_cost) == (100, 110)


def test_the_furthest_paid_break_wins():
    d = decide([(1, "2.00"), (60, "1.80"), (100, "1.12")], 45)
    assert (d.purchase_qty, d.extended_cost) == (100, 112)


def test_a_free_upgrade_beats_a_paid_one_at_a_further_break():
    d = decide([(1, "2.00"), (60, "1.50"), (200, "0.55")], 45)
    assert (d.purchase_qty, d.rule, d.extended_cost) == (60, "free_upgrade",
                                                        90)
    far = [c for c in d.candidates if c.quantity == 200][0]
    assert far.kind == "paid_upgrade" and far.verdict == "accepted"


LADDER_W = [(1, "2.00"), (100, "1.10"), (250, "0.60"), (1000, "0.11")]


def test_the_window_limits_paid_upgrades():
    d = decide(LADDER_W, 45)
    assert d.purchase_qty == 100                   # break 3 (1000) is outside 2
    d = decide(LADDER_W, 45, p(price_break_window=3))
    assert d.purchase_qty == 1000                  # now looked at, furthest
    d = decide(LADDER_W, 45, p(price_break_window=0))
    assert d.purchase_qty == 50 and d.rule == "overage"


def test_window_zero_still_finds_a_free_upgrade():
    d = decide([(1, "2.00"), (100, "0.50")], 45, p(price_break_window=0))
    assert (d.purchase_qty, d.rule) == (100, "free_upgrade")


# ---- TS-Q21 .. TS-Q23 and TS-Q39 .. TS-Q42: limits ----------------------------------

def test_candidates_above_stock_are_rejected_and_the_best_feasible_is_taken():
    d = decide([(1, "2.00"), (100, "1.10"), (1000, "0.05")], 45, stock=800)
    assert d.purchase_qty == 100
    assert [c.verdict for c in d.candidates if c.quantity == 1000] == [
        "rejected"]


def test_an_infeasible_break_uses_no_window_position():
    # R=45: baseline 50, B = 100, A = 115. Break 900 is above stock 500 and
    # is rejected; breaks 110, 120 and 130 cost 104.5, 108 and 114.4 (paid).
    # 900 is first on the ladder: if it took a position, 120 would be out.
    ladder = [(1, "2.00"), (900, "0.001"), (110, "0.95"), (120, "0.90"),
              (130, "0.88")]
    d = decide(ladder, 45, stock=500)
    rejected = [c for c in d.candidates if c.verdict == "rejected"]
    assert [c.quantity for c in rejected if "above stock" in c.why] == [900]
    assert d.purchase_qty == 120      # the furthest of the two in reach


def test_two_breaks_rounding_to_one_quantity_are_one_position():
    ladder = [(1, "2.00"), (90, "1.15"), (95, "1.14"), (100, "1.12")]
    d = decide(ladder, 45, multiple=100)
    upgrades = [c.quantity for c in d.candidates
                if c.kind not in ("floor", "overage")]
    assert upgrades == []              # every break is the baseline's 100
    ladder = [(1, "2.00"), (150, "1.00"), (160, "0.99"), (250, "0.80")]
    d = decide(ladder, 45, multiple=100)
    upgrades = [c.quantity for c in d.candidates
                if c.kind not in ("floor", "overage")]
    assert upgrades == [200, 300]      # 150 and 160 both round to 200


def test_free_and_paid_candidates_above_stock_are_rejected():
    d = decide([(1, "12.00"), (250, "2.00")], 60, stock=200)
    assert d.purchase_qty == 60 and d.rule == "required"
    d = decide([(1, "12.00"), (250, "3.00")], 60, stock=200)
    assert d.purchase_qty == 60


def test_a_candidate_off_the_multiple_is_rounded_up_and_recosted():
    d = decide([(1, "2.00"), (60, "1.40")], 45, multiple=25)
    assert d.floor_qty == 50 and d.baseline_qty == 50
    assert d.purchase_qty == 75                   # 60 -> 75, 75 x 1.40 = 105
    assert d.extended_cost == D("105.00")


def test_the_requirement_not_in_stock_is_a_shortfall_with_no_smaller_quantity():
    d = decide([(1, "0.10")], 100, stock=60)
    assert d.shortfall == "insufficient_stock" and d.purchase_qty == 100
    d = decide([(1, "0.10")], 100, stock=0)
    assert d.shortfall == "out_of_stock" and d.purchase_qty == 100
    d = decide([(1, "0.10")], 100, max_order_qty=50)
    assert d.shortfall == "purchase_limit" and d.purchase_qty == 100
    d = decide([(1, "0.10")], 100, multiple=100, stock=150) # 100 fits
    assert d.shortfall is None
    d = decide([(1, "0.10")], 120, multiple=100, stock=150) # 200 needed
    assert d.shortfall == "insufficient_stock" and d.purchase_qty == 200


def test_no_price_and_no_currency_are_shortfalls():
    assert decide([], 5).shortfall == "no_price"
    assert decide([(1, "0")], 5).shortfall == "no_price"
    assert decide([(1, "0.10")], 5, currency=None).shortfall == "no_currency"


# ---- TS-Q24 .. TS-Q30: the allowance --------------------------------------------------

def test_exact_boundary_accepted_and_one_cent_over_refused():
    d = decide([(1, "1.00"), (100, "0.534")], 40)
    assert d.baseline_cost == D("44.00") and d.allowance == D("53.400")
    assert (d.purchase_qty, d.extended_cost) == (100, D("53.400"))
    d = decide([(1, "1.00"), (100, "0.5341")], 40)
    assert d.purchase_qty == 44 and d.extended_cost == D("44.00")


ALLOW_LADDER = [(1, "2.00"), (100, "1.12")]
NO_OVERAGE = dict(overage_factor=D(1))


@pytest.mark.parametrize("pct,flat,allowed,result", [
    (0, "0", "100", 50),
    (10, "5.00", "115.00", 100),
    (25, "0", "125", 100),
    (0, "20.00", "120", 100),
    (5, "2.50", "107.50", 50),
    (0, "12.00", "112", 100),
    (0, "11.99", "111.99", 50),
])
def test_allowance_matrix(pct, flat, allowed, result):
    pol = p(extra_spend_percent=D(pct), extra_spend_flat=D(flat), **NO_OVERAGE)
    d = decide(ALLOW_LADDER, 50, pol)
    assert d.baseline_cost == 100
    assert d.allowance == D(allowed)
    assert d.purchase_qty == result


def test_zero_allowance_accepts_an_equal_cost_and_refuses_a_dearer_one():
    zero = p(extra_spend_percent=D(0), extra_spend_flat=D(0), **NO_OVERAGE)
    d = decide([(1, "2.00"), (100, "1.00")], 50, zero)
    assert (d.purchase_qty, d.rule, d.extra_spend) == (100, "paid_upgrade", 0)
    d = decide([(1, "2.00"), (100, "1.01")], 50, zero)
    assert d.purchase_qty == 50


def test_all_zero_settings_leave_only_free_upgrades():
    zero = p(extra_spend_percent=D(0), extra_spend_flat=D(0),
             price_break_window=0, **NO_OVERAGE)
    assert decide([(1, "2.00"), (100, "1.10")], 50, zero).purchase_qty == 50
    assert decide([(1, "2.00"), (100, "0.90")], 50, zero).purchase_qty == 100


def test_the_flat_amount_applies_only_in_its_currency():
    d = decide([(1, "2.00"), (100, "1.12")], 50, p(**NO_OVERAGE),
               currency="EUR")
    assert d.allowance == D("110.00")          # 100 * 1.10, no flat 5.00
    assert "ALLOWANCE_FLAT_SKIPPED" in d.flags
    assert d.purchase_qty == 50                # 112 is over 110


def test_overage_is_skipped_in_another_currency():
    d = decide([(1, "0.10")], 100, currency="EUR")
    assert d.purchase_qty == 100 and "OVERAGE_SKIPPED_CURRENCY" in d.flags


def test_a_lookahead_of_zero_percent_and_flat_independent():
    base = decide(ALLOW_LADDER, 50, p(extra_spend_percent=D(0),
                                      extra_spend_flat=D(12), **NO_OVERAGE))
    assert base.purchase_qty == 100


def test_allowance_function_is_exact():
    cost = D("0.30")
    allowed, skipped = allowance(cost, p(extra_spend_flat=D(0)), "USD")
    assert allowed == D("0.33") and not skipped


@pytest.mark.parametrize("pct", ["10", 10, 10.0])
def test_percent_forms_give_the_same_allowance(pct):
    from eda_agent.export.purchasing_config import parse_setting
    pol = p(extra_spend_percent=parse_setting("extra_spend_percent", pct),
            extra_spend_flat=D(0))
    assert allowance(D(100), pol, "USD")[0] == D(110)


# ---- remaining behaviour --------------------------------------------------------------

def test_truncated_baseline_of_the_script_no_longer_refuses_an_upgrade():
    d = decide([(1, "2.00"), (60, "1.80"), (130, "0.87")], 45)
    assert d.baseline_cost == 100          # the script's truncated one was 98
    assert (d.purchase_qty, d.extended_cost) == (130, D("113.10"))


def test_deterministic_and_ties_go_to_the_smaller_quantity():
    ladder = [(1, "4.00"), (50, "2.50"), (100, "1.00"), (200, "0.50")]
    a = decide(ladder, 45)
    assert a == decide(ladder, 45)
    # baseline 50 x 2.50 = 125; 100 x 1.00 and 200 x 0.50 both cost 100, free:
    # equal cost, the smaller quantity wins
    assert (a.purchase_qty, a.rule, a.extended_cost) == (100, "free_upgrade",
                                                         100)
    assert [c for c in a.candidates if c.quantity == 200][0].verdict \
        == "accepted"


def test_every_candidate_has_a_verdict_and_a_reason_and_the_decision_explains():
    d = decide([(1, "2.00"), (100, "1.10"), (250, "0.60")], 45)
    assert d.explanation
    for c in d.candidates:
        assert c.verdict in ("chosen", "accepted", "rejected") and c.why
    assert sum(c.verdict == "chosen" for c in d.candidates) == 1
    assert any(c.verdict == "rejected" for c in d.candidates)


def test_a_large_surplus_is_reported_not_policed():
    d = decide([(1, "0.10"), (1000, "0.011")], 50)
    assert d.surplus_ratio == 20 and d.shortfall is None


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2"])
def test_a_bad_requirement_is_refused(bad):
    with pytest.raises(ValueError):
        decide([(1, "0.10")], bad)


# ---- TS-Q36: the cost-only policy is the first planner ----------------------------------

def _legacy_offer(ladder, **kw):
    out = {"sku": "ACM-ND", "packaging": ["Cut Tape"], "stock": None,
           "moq": None, "multiple": None, "standard_package": None,
           "currency": "USD", "reeling_fee": None,
           "price_breaks": [{"quantity": q, "unit_price": float(pr)}
                            for q, pr in ladder]}
    out.update(kw)
    return out


def test_the_cost_only_policy_reproduces_the_first_planner():
    rng = random.Random(5)
    pol = QuantityPolicy.cost_only()
    for _ in range(300):
        required = rng.randint(1, 500)
        cuts = sorted(rng.sample(range(2, 4000), rng.randint(0, 4)))
        price = D(rng.choice(("0.05", "1.25", "6.00", "12.50")))
        ladder = [(1, price)]
        for c in cuts:
            price = (price * D(rng.choice(("0.9", "0.6", "0.3", "1.0")))
                     ).quantize(D("0.0001")) or D("0.0001")
            ladder.append((c, price))
        extra = dict(moq=rng.choice((None, 1, 20)),
                     multiple=rng.choice((None, 5, 50)),
                     reeling_fee=rng.choice((None, 7.0)))
        old = price_offer(_legacy_offer(ladder, **extra), required, True)
        fee = extra["reeling_fee"]
        new = decide_quantity(
            offer(ladder, reeling_fee=None if fee is None else D(str(fee)),
                  moq=extra["moq"], multiple=extra["multiple"]),
            required, pol)
        assert old["usable"] and new.shortfall is None
        assert new.purchase_qty == old["order_quantity"], (ladder, required,
                                                           extra)
        assert float(new.unit_price) == pytest.approx(old["unit_price"])
        assert float(new.extended_cost) == pytest.approx(
            old["extended_price"], abs=1e-4)


def test_lifecycle_does_not_reach_the_engine():
    a = decide([(1, "0.10")], 10)
    b = decide([(1, "0.10")], 10, lifecycle=Lifecycle("obsolete", "Obsolete"))
    assert a.purchase_qty == b.purchase_qty
