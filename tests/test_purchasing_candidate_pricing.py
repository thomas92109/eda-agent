# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Pricing of a close candidate and of a line: packaging SKUs, minimum order,
price breaks and the quantity actually ordered stay separate.

Regression: a close-match preview showed DigiKey's tape-and-reel price
(0.76752, whose first break is 1,200) for a need of 4 pieces, when the cut
tape at 4 pieces is 1.27. The offers below are the real ones DigiKey and
Mouser returned for GCT USB4110-GF-A on 2026-11-18.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from eda_agent.export import purchasing_matching as pm
from eda_agent.export.purchasing_bom_export import (
    CLOSE_MATCH_HEADERS,
    build_close_matches_csv,
)
from eda_agent.export.purchasing_model import (
    PriceBreak,
    QuantityPolicy,
    SupplierOffer,
)
from eda_agent.export.purchasing_plan import price_offer
from eda_agent.export.purchasing_quantity import decide_quantity

CUT_LADDER = [(1, 1.27), (10, 1.076), (25, 1.0088), (50, 0.9606),
              (100, 0.9147), (250, 0.8574), (500, 0.81644)]


def _o(sku, packaging, stock, moq, ladder, multiple=None, package=None,
       fee=None, currency="USD"):
    return {"sku": sku, "packaging": packaging, "stock": stock, "moq": moq,
            "multiple": multiple, "standard_package": package,
            "currency": currency, "reeling_fee": fee,
            "price_breaks": [{"quantity": q, "unit_price": p}
                             for q, p in ladder]}


def digikey():
    return [
        _o("2073-USB4110-GF-A-2-ND", ["Tape & Reel (TR)"], 225600, 1200,
           [(1200, 0.76752), (2400, 0.73766)]),
        _o("2073-USB4110-GF-A-1-ND", ["Cut Tape (CT)"], 226423, 1, CUT_LADDER),
        _o("2073-USB4110-GF-A-6-ND", ["Digi-Reel®"], 226423, 1, CUT_LADDER,
           package=1, fee=7),
    ]


def mouser():
    return [_o("640-USB4110-GF-A", ["Reel", "Cut Tape", "MouseReel"], 200330,
               1, [(1, 1.27), (10, 1.08), (25, 0.961), (100, 0.915),
                   (250, 0.858), (500, 0.817), (1200, 0.738), (2400, 0.732),
                   (4800, 0.723)], multiple=1, package=1200)]


def classify(offers, distributor, required=4):
    return pm.classify(
        bom_manufacturer="Global Connector Technology", bom_mpn="USB4110GFA",
        bom_description="USB-C receptacle", candidate_mpn="USB4110-GF-A",
        candidate_manufacturer="GCT", candidate_description="CONN RCP",
        manufacturer_match="alias", offers=offers, distributor=distributor,
        required_quantity=required, stock=226000)


def test_digikey_preview_prices_the_cut_tape_not_the_reel_break():
    found = classify(digikey(), "digikey")
    assert found["unit_price"] == Decimal("1.27")
    assert found["unit_price"] != Decimal("0.76752")
    assert found["order_quantity"] == 4 and found["required_quantity"] == 4
    assert found["extended_price"] == Decimal("5.08")
    assert found["price_sku"] == "2073-USB4110-GF-A-1-ND"
    assert "Cut Tape" in found["price_packaging"]


def test_mouser_preview_prices_the_quantity_needed():
    found = classify(mouser(), "mouser")
    assert found["unit_price"] == Decimal("1.27")
    assert found["order_quantity"] == 4
    assert found["extended_price"] == Decimal("5.08")


def test_a_reel_only_candidate_is_priced_for_the_whole_reel():
    reel_only = digikey()[:1]
    found = classify(reel_only, "digikey")
    assert found["order_quantity"] == 1200
    assert found["unit_price"] == Decimal("0.76752")
    assert found["extended_price"] == Decimal("921.024")
    assert found["required_quantity"] == 4


def test_a_packaging_without_the_stock_is_passed_over():
    offers = digikey()
    offers[1]["stock"] = 2          # cut tape cannot supply 4
    offers[2]["stock"] = 2          # nor can the Digi-Reel
    found = classify(offers, "digikey")
    assert found["price_sku"] == "2073-USB4110-GF-A-2-ND"
    assert found["order_quantity"] == 1200


def test_no_packaging_that_can_supply_it_gives_no_price():
    offers = digikey()
    for o in offers:
        o["stock"] = 0
    found = classify(offers, "digikey")
    assert found["unit_price"] is None and found["extended_price"] is None
    assert found["order_quantity"] is None and found["price_sku"] is None
    assert "in stock" in found["price_note"]


def test_the_price_break_used_is_the_one_for_the_quantity_ordered():
    found = classify(digikey(), "digikey", required=30)
    assert found["unit_price"] == Decimal("1.0088")   # the 25 break
    assert found["order_quantity"] == 30
    found = classify(digikey(), "digikey", required=10)
    assert found["unit_price"] == Decimal("1.076")


def test_the_reeling_fee_makes_a_digi_reel_lose_to_cut_tape_at_equal_price():
    digi_reel = price_offer(digikey()[2], 4, allow_overbuy=False)
    cut = price_offer(digikey()[1], 4, allow_overbuy=False)
    assert digi_reel["extended_price"] == 12.08       # 4 * 1.27 + 7
    assert cut["extended_price"] == 5.08
    reel = price_offer(digikey()[0], 4, allow_overbuy=False)
    assert reel["order_quantity"] == 1200 and reel["unit_price"] == 0.76752


def _supplier(raw):
    return SupplierOffer(
        distributor="digikey", sku=raw["sku"], packaging=tuple(raw["packaging"]),
        currency=raw["currency"], stock=raw["stock"], moq=raw["moq"],
        multiple=raw["multiple"], standard_package=raw["standard_package"],
        price_breaks=tuple(PriceBreak(q["quantity"], Decimal(repr(q["unit_price"])))
                           for q in raw["price_breaks"]),
        reeling_fee=None if raw["reeling_fee"] is None
        else Decimal(raw["reeling_fee"]))


def test_the_line_quantity_keeps_each_packaging_on_its_own_terms():
    policy = QuantityPolicy(assemblies=1, overage_factor=Decimal(1),
                            extra_spend_percent=Decimal(0),
                            extra_spend_flat=Decimal(0), price_break_window=0)
    reel, cut, digi = (_supplier(o) for o in digikey())
    # The reel is sold from 1,200: a need of 4 becomes 1,200 at ITS break.
    d = decide_quantity(reel, 4, policy)
    assert d.purchase_qty == 1200 and d.unit_price == Decimal("0.76752")
    assert "MOQ_ADJUSTED" in d.flags
    # Cut tape prices 4 pieces at the 1-piece break, no minimum applies.
    d = decide_quantity(cut, 4, policy)
    assert d.purchase_qty == 4 and d.unit_price == Decimal("1.27")
    assert d.extended_cost == Decimal("5.08")
    # Digi-Reel: same ladder, plus its reeling fee.
    d = decide_quantity(digi, 4, policy)
    assert d.purchase_qty == 4 and d.unit_price == Decimal("1.27")
    assert d.extended_cost == Decimal("12.08")


def test_the_companion_csv_says_which_packaging_and_quantity_was_priced():
    found = classify(digikey(), "digikey")
    found.update(line_id="L011", designators=["J1"],
                 bom_manufacturer="Global Connector Technology")
    built = build_close_matches_csv([found])
    assert built["headers"] == list(CLOSE_MATCH_HEADERS)
    row = next(csv.DictReader(io.StringIO(built["text"], newline="")))
    assert row["Unit Price"] == "1.27"
    assert row["Required Qty"] == "4" and row["Order Qty"] == "4"
    assert row["Extended Price"] == "5.08"
    assert row["Priced SKU"] == "2073-USB4110-GF-A-1-ND"
    assert "Cut Tape" in row["Priced Packaging"]
