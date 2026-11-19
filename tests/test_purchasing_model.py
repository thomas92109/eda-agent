# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The shared purchasing types: status derivation, Decimal reading, JSON."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from eda_agent.export import purchasing_model as m
from eda_agent.export.purchasing_review import (
    CATALOGUE,
    FIXED_CODES,
    lifecycle_reasons,
    make_reason,
    validate_override,
)

R = lambda sev: m.ReviewReason("X", sev, "x", "bom")  # noqa: E731


# TS-R18
@pytest.mark.parametrize("severities,expected", [
    ([], m.PurchasingStatus.READY),
    ([m.Severity.INFO], m.PurchasingStatus.READY),
    ([m.Severity.INFO, m.Severity.REVIEW], m.PurchasingStatus.REVIEW),
    ([m.Severity.REVIEW, m.Severity.BLOCKING], m.PurchasingStatus.BLOCKED),
    ([m.Severity.BLOCKING], m.PurchasingStatus.BLOCKED),
])
def test_status_comes_from_the_reasons(severities, expected):
    assert m.derive_status([R(s) for s in severities]) is expected


def test_a_not_fitted_row_has_no_status_to_derive():
    assert m.derive_status([R(m.Severity.BLOCKING)], "not_fitted") \
        is m.PurchasingStatus.NOT_FITTED


@pytest.mark.parametrize("value,expected", [
    (1.1, Decimal("1.1")), ("1.10", Decimal("1.10")), (5, Decimal(5)),
    (" 2 ", Decimal(2)), (0.1 + 0.2, Decimal("0.30000000000000004")),
])
def test_decimal_reading_is_exact(value, expected):
    assert m.to_decimal(value) == expected


@pytest.mark.parametrize("value", [None, True, False, "x", "", "NaN",
                                   "Infinity", float("inf")])
def test_non_numbers_are_none(value):
    assert m.to_decimal(value) is None


def test_json_keeps_decimals_exact_and_enums_plain():
    line = m.BomLine("L001", "fitted", "Acme", "ACM-R-0100",
                     designators=("R1", "R2"), bom_quantity=2)
    out = m.to_jsonable({"line": line, "cost": Decimal("0.1250"),
                         "status": m.PurchasingStatus.READY})
    text = json.dumps(out)
    assert out["cost"] == "0.1250" and out["status"] == "ready"
    assert out["line"]["designators"] == ["R1", "R2"]
    assert "0.125" in text


def test_the_model_has_no_quantity_multiplier():
    fields = {f for f in m.QuantityPolicy.__dataclass_fields__}
    assert not any("multiplier" in f for f in fields)
    assert m.QuantityPolicy().price_break_window == 2


def test_cost_only_is_no_overage_and_no_paid_upgrade():
    p = m.QuantityPolicy.cost_only()
    assert p.overage_factor == 1 and p.price_break_window == 0
    assert p.extra_spend_percent == 0 and p.extra_spend_flat == 0


# ---- the catalogue ---------------------------------------------------------------

def test_every_reason_code_has_a_row_and_a_source():
    assert len(CATALOGUE) >= 35
    for code, (sev, source, allowed, text) in CATALOGUE.items():
        assert code == code.upper() and text and source


def test_the_end_of_life_reasons_are_fixed_and_blocking():
    for code in ("LIFECYCLE_EOL", "LIFECYCLE_OBSOLETE",
                 "DISTRIBUTOR_DISCONTINUED", "UNRESOLVED_COMPONENT",
                 "OUT_OF_STOCK", "INSUFFICIENT_STOCK", "MISSING_PRICE",
                 "MPN_NOT_FOUND", "NO_DISTRIBUTOR_DATA", "BOM_INCOMPLETE"):
        assert code in FIXED_CODES
        assert make_reason(code, overrides={code: m.Severity.INFO}).severity \
            is m.Severity.BLOCKING


def test_a_tunable_reason_takes_the_override():
    r = make_reason("LIFECYCLE_NRND", overrides={"LIFECYCLE_NRND":
                                                 m.Severity.INFO})
    assert r.severity is m.Severity.INFO
    assert validate_override("LARGE_SURPLUS", "blocking")[0] is None
    assert validate_override("LARGE_SURPLUS", "review")[0] is m.Severity.REVIEW


# ---- lifecycle recognition ------------------------------------------------------------

L = m.Lifecycle


def _codes(found, **kw):
    return sorted(r.code for r in lifecycle_reasons(found, **kw))


@pytest.mark.parametrize("lc,code", [
    (L("nrnd", "Not For New Designs"), "LIFECYCLE_NRND"),
    (L("last_time_buy", "Last Time Buy"), "LIFECYCLE_LAST_TIME_BUY"),
    (L("end_of_life", "End of Life"), "LIFECYCLE_EOL"),
    (L("obsolete", "Obsolete"), "LIFECYCLE_OBSOLETE"),
    (L("discontinued", "Discontinued at Digi-Key"), "DISTRIBUTOR_DISCONTINUED"),
    (L("active", "Active", discontinued=True), "DISTRIBUTOR_DISCONTINUED"),
    (L("active", "Active", end_of_life=True), "LIFECYCLE_EOL"),
    (L("preliminary", "Preliminary"), "LIFECYCLE_PRELIMINARY"),
    (L(None, "Wibble"), "LIFECYCLE_UNKNOWN"),
    (L(None, None), "LIFECYCLE_UNKNOWN"),
])
def test_each_lifecycle_state_is_recognised(lc, code):
    assert code in _codes({"digikey": lc})


def test_an_active_part_has_no_lifecycle_reason():
    assert _codes({"digikey": L("active", "Active"),
                   "mouser": L("active", "Active")}) == []


def test_mouser_null_beside_active_is_neither_unknown_nor_reported():
    codes = _codes({"digikey": L("active", "Active"), "mouser": L(None, None)})
    assert codes == []


def test_a_silent_distributor_is_reported_when_the_other_is_not_active():
    codes = _codes({"digikey": L("nrnd", "NRND"), "mouser": L(None, None)})
    assert codes == ["LIFECYCLE_NRND", "LIFECYCLE_NOT_STATED"]         or set(codes) == {"LIFECYCLE_NRND", "LIFECYCLE_NOT_STATED"}


def test_another_distributors_end_of_life_counts():
    codes = _codes({"digikey": L("active", "Active"),
                    "mouser": L("end_of_life", "End of Life")})
    assert codes == ["LIFECYCLE_EOL"]


def test_last_buy_date_is_in_the_message():
    r = lifecycle_reasons({"digikey": L("last_time_buy", "x",
                                        last_buy_date="2027-01-31")})[0]
    assert "2027-01-31" in r.message


def test_the_docs_list_every_review_code():
    from pathlib import Path
    docs = (Path(__file__).resolve().parents[1] / "docs"
            / "PART_SOURCING.md").read_text(encoding="utf-8")
    missing = [code for code in CATALOGUE if f"`{code}`" not in docs]
    assert not missing, f"PART_SOURCING.md does not list {missing}"
