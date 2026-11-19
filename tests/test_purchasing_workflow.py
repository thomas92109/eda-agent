# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The purchasing workflow: enriching a BOM and proposing what to buy.

The distributors here are made up, or are the recorded replies under
``tests/fixtures/distributors``; Altium is a stand-in that answers four
read commands. No request leaves the machine, and nothing here can make
a cart or a list.
"""

import asyncio
import inspect
import io
import json
import pathlib
import urllib.error
from datetime import datetime

import pytest

from eda_agent.export import purchasing_plan, purchasing_sources
from eda_agent.export.purchasing_plan import plan_purchases, price_offer
from eda_agent.export.purchasing_sources import (
    BomInputError,
    enrich_bom,
    lines_from_input,
    manufacturer_match,
)
from eda_agent.libimport.providers import distributors
from eda_agent.libimport.providers.base import (
    PartHit,
    ProviderError,
    ProviderUnavailable,
)
from eda_agent.tools import purchasing

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "distributors"
_URLOPEN = "eda_agent.libimport.providers._distributor.urllib.request.urlopen"


# ---- made-up distributors -----------------------------------------------------

def _offer(sku, breaks, stock=1000, moq=1, multiple=None,
           packaging=("Cut Tape (CT)",), standard_package=1,
           currency="USD", fee=None):
    return {"distributor_pn": sku, "packaging": list(packaging),
            "stock": stock, "moq": moq, "multiple": multiple,
            "standard_package": standard_package, "currency": currency,
            "price_breaks": [{"quantity": q, "unit_price": p}
                             for q, p in breaks],
            "provider_specific": {"digi_reel_fee": fee}}


def _hit(provider, mpn, maker, offers, lifecycle="active", stock=1000,
         cached=False):
    sourcing = {
        "distributor": provider, "stock": stock,
        "lifecycle": {"status": lifecycle, "raw": lifecycle},
        "lead_time": {"weeks": 12}, "product_url": "https://example.test/p",
        "offers": offers, "retrieved_at": "2026-11-16T10:00:00Z",
        "freshness": "live: ProductDetails"}
    if cached:
        sourcing["cache"] = {"hit": True,
                             "fetched_at": "2026-11-16T10:00:00Z",
                             "expires_at": "2026-11-16T14:00:00Z"}
    return PartHit(provider=provider, part_id=mpn, mpn=mpn,
                   manufacturer=maker, description=f"{mpn} as sold",
                   sourcing=sourcing)


class _Shop:
    """One distributor: a list of hits, answered by exact number."""

    _PART_URL = "part-url"

    def __init__(self, name, hits=()):
        self.name, self.hits, self.asked = name, list(hits), []
        self.fail = {}        # part number -> exception to raise

    def _answer(self, number):
        self.asked.append(number)
        if number in self.fail:
            raise self.fail[number]
        return [h for h in self.hits if h.mpn.casefold() == number.casefold()
                or any(o["distributor_pn"] == number
                       for o in h.sourcing["offers"])]

    # DigiKey's two reads.
    def _details(self, number):
        found = self._answer(number)
        if len(found) != 1:
            error = ProviderError("digikey returned HTTP 404")
            error.status = 404
            raise error
        return found[0]

    def search(self, query, limit=20):
        return [h for h in self.hits
                if query.casefold() in h.mpn.casefold()]

    # Mouser's one.
    def _post(self, url, request):
        return self._answer(
            request["SearchByPartRequest"]["mouserPartNumber"])


_RES = "RC0603FR-0710KL"
_CAP = "GRM155R71C104KA88D"
_AMP = "LM358DR"


def _shops(monkeypatch, digikey=(), mouser=()):
    dk, mo = _Shop("digikey", digikey), _Shop("mouser", mouser)
    monkeypatch.setattr(purchasing_sources, "_providers", lambda: {
        "digikey": (dk, purchasing_sources._digikey_hits),
        "mouser": (mo, purchasing_sources._mouser_hits)})
    return dk, mo


def _stocked(monkeypatch):
    """Three parts at both distributors, with different terms."""
    return _shops(monkeypatch, digikey=[
        _hit("digikey", _RES, "YAGEO", [
            _offer("311-10.0KHRTR-ND", [(5000, 0.005)], stock=50000,
                   moq=5000, packaging=("Tape & Reel (TR)",),
                   standard_package=5000),
            _offer("311-10.0KHRCT-ND",
                   [(1, 0.10), (10, 0.028), (100, 0.0136)], stock=50000),
            _offer("311-10.0KHRDKR-ND",
                   [(1, 0.10), (10, 0.028), (100, 0.0136)], stock=50000,
                   packaging=("Digi-Reel®",), fee=7.0)]),
        _hit("digikey", _CAP, "Murata Electronics", [
            _offer("490-GRM155-CT-ND", [(1, 0.10), (10, 0.02)], stock=400)]),
        _hit("digikey", _AMP, "Texas Instruments", [
            _offer("296-LM358DRCT-ND", [(1, 0.26), (10, 0.175),
                                        (100, 0.1322)], stock=70000)]),
    ], mouser=[
        _hit("mouser", _RES, "YAGEO", [
            _offer("603-RC0603FR-0710KL",
                   [(1, 0.12), (10, 0.017), (100, 0.012)], stock=6000000,
                   multiple=1, packaging=("Reel", "Cut Tape", "MouseReel"),
                   standard_package=5000)]),
        _hit("mouser", _CAP, "Murata", [
            _offer("81-GRM155R71C104KA8D", [(10, 0.03), (100, 0.011)],
                   stock=90000, moq=10, multiple=10)]),
        _hit("mouser", _AMP, "Texas Instruments", [
            _offer("595-LM358DR", [(1, 0.26), (10, 0.175), (100, 0.133)],
                   stock=50000, multiple=1)]),
    ])


_BOM = [
    {"manufacturer": "Yageo", "part_number": _RES,
     "description": "RES 10K 1% 0603", "quantity": 10,
     "designators": [f"R{n}" for n in range(101, 111)]},
    {"manufacturer": "Murata", "part_number": _CAP,
     "description": "CAP 100N 16V 0402", "quantity": 4,
     "designators": ["C1", "C2", "C3", "C4"]},
    {"manufacturer": "Texas Instruments", "part_number": _AMP,
     "description": "IC OPAMP DUAL SOIC-8", "quantity": 1,
     "designators": ["U1"]},
]


def _analysis(build_quantity=1, lines=None, **more):
    found = enrich_bom(lines or _BOM, build_quantity)
    return {"build_quantity": build_quantity, "bom_complete": True,
            "unresolved": [], **found, **more}


def _line(result, number):
    return next(l for l in result["lines"] if l["part_number"] == number)


# ---- pricing one offer ----------------------------------------------------------

_LADDER = [(1, 0.10), (10, 0.05), (100, 0.02)]


@pytest.mark.parametrize("required,quantity,at,unit,extended", [
    (1, 1, 1, 0.10, 0.10),
    (9, 10, 10, 0.05, 0.50),       # 9 at 0.10 is 0.90; 10 at 0.05 is 0.50
    (10, 10, 10, 0.05, 0.50),
    (30, 30, 10, 0.05, 1.50),
    (40, 100, 100, 0.02, 2.00),    # 40 at 0.05 is 2.00: not cheaper, so...
    (41, 100, 100, 0.02, 2.00),    # ...only from 41 does 100 win
    (100, 100, 100, 0.02, 2.00),
    (250, 250, 100, 0.02, 5.00),
])
def test_the_price_break_that_applies_and_the_overbuy_that_pays(
        required, quantity, at, unit, extended):
    if required == 40:
        quantity, at, unit, extended = 40, 10, 0.05, 2.00
    got = price_offer(_offer("X", _LADDER, stock=100000)
                      | {"sku": "X"}, required)
    assert got["usable"] is True
    assert (got["order_quantity"], got["price_break_quantity"],
            got["unit_price"], got["extended_price"]) == (
        quantity, at, unit, extended)
    assert got["surplus"] == quantity - required


def test_an_overbuy_says_what_it_saves_and_can_be_turned_off():
    offer = {"sku": "X", "price_breaks": [
        {"quantity": q, "unit_price": p} for q, p in _LADDER],
        "currency": "USD", "stock": 5000, "moq": 1, "multiple": 1}
    got = price_offer(offer, 80)
    assert got["order_quantity"] == 100 and got["surplus"] == 20
    assert got["overbuy"] == {"instead_of": 80, "instead_of_price": 4.0,
                              "saves": 2.0}
    assert "100 at the 100 price break costs less" in got["notes"][-1]
    plain = price_offer(offer, 80, allow_overbuy=False)
    assert (plain["order_quantity"], plain["extended_price"]) == (80, 4.0)
    assert "overbuy" not in plain


def test_an_overbuy_that_is_not_in_stock_is_not_proposed():
    offer = {"sku": "X", "price_breaks": [
        {"quantity": q, "unit_price": p} for q, p in _LADDER],
        "currency": "USD", "stock": 90, "moq": 1, "multiple": 1}
    got = price_offer(offer, 80)
    assert got["order_quantity"] == 80 and got["stock_short"] is False
    assert "overbuy" not in got


@pytest.mark.parametrize("required,moq,multiple,expected", [
    (3, 10, None, 10),        # raised to the minimum
    (3, 1, 5, 5),             # raised to the multiple
    (12, 10, 10, 20),         # above the minimum, onto the multiple
    (20, 10, 10, 20),
    (21, 10, 10, 30),
    (1, 2500, 2500, 2500),
])
def test_minimum_and_multiple(required, moq, multiple, expected):
    offer = {"sku": "X", "currency": "USD", "stock": 100000, "moq": moq,
             "multiple": multiple,
             "price_breaks": [{"quantity": 1, "unit_price": 0.01}]}
    got = price_offer(offer, required, allow_overbuy=False)
    assert got["order_quantity"] == expected
    assert got["extended_price"] == round(expected * 0.01, 5)
    if expected > required:
        assert f"{required} needed; {expected} is the least" in \
            " ".join(got["notes"])


def test_a_quantity_below_the_first_break_is_raised_to_it():
    offer = {"sku": "X", "currency": "USD", "stock": 99999, "moq": None,
             "multiple": None,
             "price_breaks": [{"quantity": 25, "unit_price": 0.2}]}
    got = price_offer(offer, 4)
    assert got["order_quantity"] == 25 and got["extended_price"] == 5.0
    assert "no minimum stated" in got["notes"][0]


def test_a_reel_is_sold_whole():
    offer = {"sku": "TR", "currency": "USD", "stock": 99999, "moq": 5000,
             "multiple": None, "standard_package": 5000,
             "packaging": ["Tape & Reel (TR)"],
             "price_breaks": [{"quantity": 5000, "unit_price": 0.005}]}
    got = price_offer(offer, 6000)
    assert got["order_quantity"] == 10000
    assert "a reel is sold whole" in " ".join(got["notes"])
    # Cut tape with the same figures has no such multiple.
    cut = price_offer({**offer, "packaging": ["Cut Tape (CT)"]}, 6000)
    assert cut["order_quantity"] == 6000


def test_a_reeling_fee_is_part_of_the_price():
    offer = {"sku": "DKR", "currency": "USD", "stock": 999, "moq": 1,
             "multiple": None, "reeling_fee": 7.0,
             "packaging": ["Digi-Reel®"],
             "price_breaks": [{"quantity": 1, "unit_price": 0.10}]}
    got = price_offer(offer, 10)
    assert got["extended_price"] == 8.0 and got["reeling_fee"] == 7.0


@pytest.mark.parametrize("change,reason", [
    ({"price_breaks": []}, "states no price"),
    ({"price_breaks": [{"quantity": 1, "unit_price": None}]},
     "states no price"),
    ({"price_breaks": [{"quantity": 0, "unit_price": 0.1}]},
     "states no price"),
    ({"currency": None}, "states no currency"),
])
def test_an_offer_without_a_price_is_not_priced(change, reason):
    offer = {"sku": "X", "currency": "USD", "stock": 10, "moq": 1,
             "price_breaks": [{"quantity": 1, "unit_price": 0.1}], **change}
    got = price_offer(offer, 5)
    assert got["usable"] is False and reason in got["reason"]
    assert "extended_price" not in got


def test_stock_is_checked_against_the_order_quantity():
    offer = {"sku": "X", "currency": "USD", "moq": 1, "multiple": None,
             "price_breaks": [{"quantity": 1, "unit_price": 0.1}]}
    assert price_offer({**offer, "stock": 10}, 10)["stock_short"] is False
    assert price_offer({**offer, "stock": 9}, 10)["stock_short"] is True
    unknown = price_offer({**offer, "stock": None}, 10)
    assert unknown["stock_short"] is False
    assert "states no stock" in " ".join(unknown["notes"])


# ---- matching a line to a distributor's product -------------------------------

@pytest.mark.parametrize("ours,theirs,expected", [
    ("Yageo", "YAGEO", "exact"),
    ("Texas Instruments", "Texas Instruments Inc.", "exact"),
    ("Murata", "Murata Electronics", "partial"),
    ("Vishay", "Vishay / Dale", "partial"),
    ("TE Connectivity", "TE Connectivity Passive Product", "partial"),
    ("TI", "Texas Instruments", "different"),
    ("Rohm", "Texas Instruments", "different"),
    ("ROHM Semiconductor", "Rohm", "partial"),
    ("", "Yageo", "different"),
])
def test_how_manufacturer_names_compare(ours, theirs, expected):
    assert manufacturer_match(ours, theirs) == expected
    assert manufacturer_match(theirs, ours) == expected


def test_the_bom_is_multiplied_by_the_boards_to_build(monkeypatch):
    _stocked(monkeypatch)
    for boards in (1, 3, 250):
        found = enrich_bom(_BOM, boards)
        assert [(l["quantity_per_board"], l["required_quantity"])
                for l in found["lines"]] == [
            (10, 10 * boards), (4, 4 * boards), (1, boards)]


def test_a_line_keeps_its_description_and_every_designator(monkeypatch):
    _stocked(monkeypatch)
    line = enrich_bom(_BOM, 5)["lines"][0]
    assert line["description"] == "RES 10K 1% 0603"
    assert line["designators"] == [f"R{n}" for n in range(101, 111)]
    assert (line["manufacturer"], line["part_number"]) == ("Yageo", _RES)


def test_a_matched_line_carries_the_distributors_offers(monkeypatch):
    _stocked(monkeypatch)
    line = enrich_bom(_BOM, 1)["lines"][0]
    digikey, mouser = (line["distributors"][n] for n in ("digikey", "mouser"))
    assert digikey["status"] == mouser["status"] == "matched"
    assert digikey["manufacturer_match"] == "exact"
    assert [o["sku"] for o in digikey["offers"]] == [
        "311-10.0KHRTR-ND", "311-10.0KHRCT-ND", "311-10.0KHRDKR-ND"]
    assert digikey["offers"][2]["reeling_fee"] == 7.0
    assert mouser["offers"] == [{
        "sku": "603-RC0603FR-0710KL",
        "packaging": ["Reel", "Cut Tape", "MouseReel"], "stock": 6000000,
        "moq": 1, "multiple": 1, "standard_package": 5000,
        "currency": "USD", "reeling_fee": None,
        "price_breaks": [{"quantity": 1, "unit_price": 0.12},
                         {"quantity": 10, "unit_price": 0.017},
                         {"quantity": 100, "unit_price": 0.012}]}]
    assert mouser["lifecycle"] == "active" and mouser["cached"] is False
    assert mouser["retrieved_at"] == "2026-11-16T10:00:00Z"
    # "Murata" in the BOM, "Murata Electronics" at DigiKey: taken, and said.
    cap = enrich_bom(_BOM, 1)["lines"][1]["distributors"]["digikey"]
    assert cap["manufacturer_match"] == "partial"
    assert "taken as the same maker" in cap["note"]


def test_a_near_part_number_is_never_taken(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    near = [{"manufacturer": "Yageo", "part_number": "RC0603FR-0710K",
             "quantity": 1, "designators": ["R1"]},
            {"manufacturer": "Yageo", "part_number": "RC0603FR-0710KLX",
             "quantity": 1, "designators": ["R2"]}]
    found = enrich_bom(near, 1)
    for line in found["lines"]:
        for answer in line["distributors"].values():
            assert answer["status"] == "not_found"
            assert "offers" not in answer
    plan = plan_purchases({"build_quantity": 1, **found})
    assert plan["summary"] == {
        "bom_lines": 2, "planned": 0, "unpurchasable": 2,
        "unresolved_components": 0, "bom_complete": True}
    assert plan["orders"] == {} and plan["complete"] is False
    # Case and spaces alone are not a different number.
    spaced = enrich_bom([{"manufacturer": "Yageo", "quantity": 1,
                          "part_number": " rc0603fr-0710kl "}], 1)
    assert spaced["lines"][0]["distributors"]["mouser"]["status"] == "matched"


def test_another_makers_part_with_the_same_number_is_not_taken(monkeypatch):
    _shops(monkeypatch,
           digikey=[_hit("digikey", _AMP, "Rohm Semiconductor", [
               _offer("LM358DRCT-ND", [(1, 0.2)])])],
           mouser=[_hit("mouser", _AMP, "Texas Instruments", [
               _offer("595-LM358DR", [(1, 0.26)])])])
    bom = [{"manufacturer": "TI", "part_number": _AMP, "quantity": 2,
            "designators": ["U1", "U2"]}]
    found = enrich_bom(bom, 1)
    for name, maker in (("digikey", "Rohm Semiconductor"),
                        ("mouser", "Texas Instruments")):
        answer = found["lines"][0]["distributors"][name]
        assert answer["status"] == "manufacturer_mismatch"
        assert f"under {maker}, not 'TI'" in answer["reason"]
        assert answer["candidates"][0]["manufacturer"] == maker
    plan = plan_purchases({"build_quantity": 1, **found})
    line = plan["lines"][0]
    assert line["status"] == "unpurchasable" and "choice" not in line
    assert len(line["problems"]) == 2 and plan["orders"] == {}


def test_a_number_two_makers_could_both_be_is_ambiguous(monkeypatch):
    _shops(monkeypatch, mouser=[
        _hit("mouser", "X100", "Vishay Dale", [_offer("71-X100", [(1, 1)])]),
        _hit("mouser", "X100", "Vishay Semiconductors",
             [_offer("78-X100", [(1, 2)])])])
    found = enrich_bom([{"manufacturer": "Vishay", "part_number": "X100",
                         "quantity": 1}], 1)
    answer = found["lines"][0]["distributors"]["mouser"]
    assert answer["status"] == "ambiguous"
    assert sorted(c["manufacturer"] for c in answer["candidates"]) == [
        "Vishay Dale", "Vishay Semiconductors"]
    # The exact name settles it.
    exact = enrich_bom([{"manufacturer": "Vishay Dale",
                         "part_number": "X100", "quantity": 1}], 1)
    answer = exact["lines"][0]["distributors"]["mouser"]
    assert answer["status"] == "matched"
    assert [o["sku"] for o in answer["offers"]] == ["71-X100"]


# ---- the plan -------------------------------------------------------------------

def test_each_line_goes_where_it_costs_least(monkeypatch):
    _stocked(monkeypatch)
    plan = plan_purchases(_analysis(1))
    assert plan["complete"] is True and plan["prefer"] == "lowest"
    res, cap, amp = (_line(plan, n)["choice"] for n in (_RES, _CAP, _AMP))
    # Resistor: Mouser 10 at 0.017 against DigiKey cut tape 10 at 0.028.
    assert (res["distributor"], res["sku"], res["order_quantity"],
            res["unit_price"], res["extended_price"]) == (
        "mouser", "603-RC0603FR-0710KL", 10, 0.017, 0.17)
    # Capacitor: 4 are needed. DigiKey sells 4 at 0.10 (0.40), but 10 at
    # its 0.02 break cost 0.20, less than 4 do; Mouser's least is 10 at
    # 0.03.
    assert (cap["distributor"], cap["order_quantity"], cap["unit_price"],
            cap["extended_price"], cap["surplus"]) == (
        "digikey", 10, 0.02, 0.2, 6)
    assert cap["overbuy"] == {"instead_of": 4, "instead_of_price": 0.4,
                              "saves": 0.2}
    assert "costs less in total" in " ".join(cap["notes"])
    mouser_cap = _line(plan, _CAP)["alternatives"][0]
    assert (mouser_cap["distributor"], mouser_cap["order_quantity"],
            mouser_cap["extended_price"]) == ("mouser", 10, 0.3)
    # Op amp: the same price at both; the smaller order, then the part
    # number, decide, and the plan says so.
    assert (amp["distributor"], amp["extended_price"]) == ("digikey", 0.26)
    assert "the same price at mouser" in " ".join(amp["notes"])
    assert plan["totals"]["distributors"] == {
        "digikey": {"lines": 2, "subtotal": {"USD": 0.46}},
        "mouser": {"lines": 1, "subtotal": {"USD": 0.17}}}
    assert plan["totals"]["component_subtotal"] == {"USD": 0.63}
    assert "Shipping, taxes, tariffs" in plan["totals"]["not_included"]


def test_packagings_are_priced_on_their_own_terms(monkeypatch):
    _stocked(monkeypatch)
    # Ten: the reel's minimum is 5000 and the Digi-Reel adds 7.00.
    ten = plan_purchases(_analysis(1), prefer="digikey")
    choice = _line(ten, _RES)["choice"]
    assert (choice["sku"], choice["packaging"], choice["extended_price"]) \
        == ("311-10.0KHRCT-ND", ["Cut Tape (CT)"], 0.28)
    considered = {o["sku"]: o for o in
                  _line(ten, _RES)["alternatives"] and
                  purchasing_plan._distributor_option(
                      "digikey", enrich_bom(_BOM, 1)["lines"][0]
                      ["distributors"]["digikey"], 10, True)
                  ["offers_considered"]}
    assert considered["311-10.0KHRTR-ND"]["order_quantity"] == 5000
    assert considered["311-10.0KHRDKR-ND"]["extended_price"] == 7.28
    # Five hundred boards: 5000 needed, and now the reel is the cheapest.
    many = plan_purchases(_analysis(500), prefer="digikey")
    choice = _line(many, _RES)["choice"]
    assert (choice["sku"], choice["order_quantity"],
            choice["extended_price"]) == ("311-10.0KHRTR-ND", 5000, 25.0)


def test_a_preferred_distributor_gets_what_it_can_sell(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    plan = plan_purchases(_analysis(1), prefer="digikey")
    assert {l["choice"]["distributor"] for l in plan["lines"]} == {"digikey"}
    assert plan["totals"]["distributors"] == {
        "digikey": {"lines": 3, "subtotal": {"USD": 0.74}}}
    assert _line(plan, _CAP)["choice"]["order_quantity"] == 10
    # Without overbuying, 4 capacitors at 0.10.
    plain = plan_purchases(_analysis(1), prefer="digikey",
                           allow_overbuy=False)
    assert _line(plain, _CAP)["choice"]["order_quantity"] == 4
    assert plain["totals"]["component_subtotal"] == {"USD": 0.94}
    # Mouser's offer is still shown beside it.
    other = _line(plan, _RES)["alternatives"]
    assert [o["distributor"] for o in other] == ["mouser"]
    assert other[0]["extended_price"] == 0.17

    # What DigiKey cannot sell goes to Mouser, and the line says why.
    dk.hits = [h for h in dk.hits if h.mpn != _AMP]
    plan = plan_purchases(_analysis(1), prefer="digikey")
    amp = _line(plan, _AMP)["choice"]
    assert amp["distributor"] == "mouser"
    assert "fallback: not bought at digikey" in " ".join(amp["notes"])
    assert sorted(plan["totals"]["distributors"]) == ["digikey", "mouser"]


def test_an_unknown_preference_is_refused():
    with pytest.raises(ValueError, match="lowest, digikey, mouser"):
        plan_purchases({"lines": []}, prefer="cheapest")


def test_stock_decides_before_price(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    # Mouser is cheaper for the resistor but has 5 of them.
    mo.hits[0].sourcing["offers"][0]["stock"] = 5
    plan = plan_purchases(_analysis(1))
    line = _line(plan, _RES)
    assert line["choice"]["distributor"] == "digikey"
    short = line["alternatives"][0]
    assert short["status"] == "insufficient_stock"
    assert "has 5 of 603-RC0603FR-0710KL in stock and 10 are needed" in \
        short["reason"]
    # Neither has enough: the line is not bought, and says so twice.
    for offer in dk.hits[0].sourcing["offers"]:
        offer["stock"] = 0
    plan = plan_purchases(_analysis(1))
    line = _line(plan, _RES)
    assert line["status"] == "unpurchasable" and "choice" not in line
    assert len(line["problems"]) == 2
    assert all("in stock" in p for p in line["problems"])
    assert plan["unpurchasable_lines"] == [0] and plan["complete"] is False
    assert plan["summary"]["planned"] == 2


def test_a_part_with_no_price_is_unpurchasable_not_free(monkeypatch):
    _shops(monkeypatch, mouser=[_hit("mouser", _AMP, "Texas Instruments", [
        _offer("595-LM358DR", [], stock=None)])])
    plan = plan_purchases(_analysis(1, lines=[_BOM[2]]))
    line = plan["lines"][0]
    assert line["status"] == "unpurchasable"
    assert sorted(line["problems"]) == [
        "digikey lists no product with the manufacturer part number "
        "'LM358DR'", "mouser: the distributor states no price for it"]
    assert plan["totals"]["component_subtotal"] is None
    assert plan["totals"]["distributors"] == {}


def test_prices_in_two_currencies_are_not_compared(monkeypatch):
    _shops(monkeypatch,
           digikey=[_hit("digikey", _AMP, "Texas Instruments", [
               _offer("296-LM358DRCT-ND", [(1, 0.26)])])],
           mouser=[_hit("mouser", _AMP, "Texas Instruments", [
               _offer("595-LM358DR", [(1, 0.20)], currency="EUR")])])
    plan = plan_purchases(_analysis(1, lines=[_BOM[2]]))
    line = plan["lines"][0]
    assert line["status"] == "unpurchasable"
    assert "different currencies (EUR, USD)" in line["problems"][0]
    # Naming a distributor is a decision, and is followed.
    plan = plan_purchases(_analysis(1, lines=[_BOM[2]]), prefer="mouser")
    assert plan["lines"][0]["choice"]["currency"] == "EUR"
    assert plan["totals"]["component_subtotal"] == {"EUR": 0.2}


def test_no_line_is_ever_left_out_of_a_plan(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    dk.hits, mo.hits = dk.hits[:1], mo.hits[:1]      # only the resistor
    analysis = _analysis(2, unresolved=[
        {"designator": "J1", "manufacturer": "", "part_number": "",
         "description": "", "footprint": "HDR", "reason": "no Manufacturer"}])
    plan = plan_purchases(analysis)
    assert [l["part_number"] for l in plan["lines"]] == [_RES, _CAP, _AMP]
    assert [l["status"] for l in plan["lines"]] == [
        "planned", "unpurchasable", "unpurchasable"]
    assert plan["unpurchasable_lines"] == [1, 2]
    assert plan["unresolved"][0]["designator"] == "J1"
    assert plan["complete"] is False
    assert plan["summary"] == {
        "bom_lines": 3, "planned": 1, "unpurchasable": 2,
        "unresolved_components": 1, "bom_complete": True}
    for line in plan["lines"]:
        assert line["designators"] and line["required_quantity"] == \
            2 * line["quantity_per_board"]


def test_a_short_bom_makes_no_complete_plan(monkeypatch):
    _stocked(monkeypatch)
    plan = plan_purchases(_analysis(1, bom_complete=False))
    assert plan["summary"]["planned"] == 3 and plan["complete"] is False
    assert plan["summary"]["bom_complete"] is False


def test_the_orders_keep_what_a_list_or_a_cart_will_need(monkeypatch):
    _stocked(monkeypatch)
    plan = plan_purchases(_analysis(1), prefer="digikey")
    first = plan["orders"]["digikey"][0]
    assert first == {
        "line": 0, "sku": "311-10.0KHRCT-ND", "quantity": 10,
        "designators": [f"R{n}" for n in range(101, 111)],
        "description": "RES 10K 1% 0603", "manufacturer": "Yageo",
        "manufacturer_part_number": _RES, "extended_price": 0.28,
        "currency": "USD"}


def test_an_ageing_part_is_planned_with_a_warning(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    mo.hits[2].sourcing["lifecycle"] = {"status": "nrnd", "raw": "NRND"}
    plan = plan_purchases(_analysis(1), prefer="mouser")
    assert "lifecycle: not recommended for new designs" in \
        _line(plan, _AMP)["choice"]["notes"]


# ---- when a distributor does not answer -----------------------------------------

def test_one_distributor_failing_leaves_the_other_working(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    mo.fail[_CAP] = ProviderUnavailable(
        "mouser rate-limited this client (HTTP 403)")
    found = enrich_bom(_BOM, 1)
    statuses = [l["distributors"]["mouser"]["status"] for l in found["lines"]]
    assert statuses == ["matched", "unavailable", "not_checked"]
    assert "rate-limited" in found["lines"][2]["distributors"]["mouser"][
        "reason"]
    # Mouser was not asked again after it stopped answering.
    assert mo.asked == [_RES, _CAP]
    assert found["distributors"]["mouser"] == {
        "asked": 2, "matched": 1, "from_cache": 0, "read_now": 1,
        "ok": False, "stopped": "mouser rate-limited this client (HTTP 403)"}
    assert found["distributors"]["digikey"]["ok"] is True
    assert [l["distributors"]["digikey"]["status"]
            for l in found["lines"]] == ["matched"] * 3
    plan = plan_purchases({"build_quantity": 1, **found})
    assert plan["complete"] is True
    assert [l["choice"]["distributor"] for l in plan["lines"]] == [
        "mouser", "digikey", "digikey"]
    assert _line(plan, _CAP)["alternatives"][0]["status"] == "unavailable"


def test_an_error_on_one_line_does_not_stop_the_rest(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    dk.fail[_CAP] = ProviderError("digikey returned HTTP 500")
    mo.fail[_CAP] = RuntimeError("boom")
    found = enrich_bom(_BOM, 1)
    cap = found["lines"][1]["distributors"]
    assert cap["digikey"] == {"status": "error",
                              "reason": "digikey returned HTTP 500"}
    assert cap["mouser"] == {"status": "error",
                             "reason": "RuntimeError: boom"}
    assert found["lines"][2]["distributors"]["digikey"]["status"] == "matched"
    assert found["lines"][2]["distributors"]["mouser"]["status"] == "matched"
    plan = plan_purchases({"build_quantity": 1, **found})
    assert plan["unpurchasable_lines"] == [1]


def test_both_distributors_down_is_said_not_read_as_no_such_part(
        monkeypatch):
    dk, mo = _stocked(monkeypatch)
    for shop in (dk, mo):
        shop.fail[_RES] = ProviderUnavailable(f"{shop.name} unreachable")
    found = enrich_bom(_BOM, 1)
    assert all(a["status"] in ("unavailable", "not_checked")
               for l in found["lines"] for a in l["distributors"].values())
    plan = plan_purchases({"build_quantity": 1, **found})
    assert plan["summary"]["planned"] == 0
    assert "unreachable" in " ".join(plan["lines"][0]["problems"])


# ---- lines given directly -------------------------------------------------------

def test_lines_given_directly_are_checked():
    good = lines_from_input([
        {"manufacturer": " Yageo ", "part_number": _RES,
         "designators": "R1, R2 R3", "description": "RES  10K"},
        {"manufacturer": "Murata", "part_number": _CAP, "quantity": 4}])
    assert good[0] == {
        "manufacturer": "Yageo", "part_number": _RES,
        "description": "RES 10K", "quantity": 3,
        "designators": ["R1", "R2", "R3"], "footprints": []}
    assert good[1]["quantity"] == 4 and good[1]["designators"] == []
    with pytest.raises(BomInputError) as caught:
        lines_from_input([
            {"manufacturer": "Yageo", "part_number": _RES, "quantity": 1},
            {"manufacturer": "", "part_number": _RES, "quantity": 1},
            {"manufacturer": "Yageo", "part_number": "", "quantity": 1},
            {"manufacturer": "Yageo", "part_number": "A"},
            {"manufacturer": "Yageo", "part_number": "B", "quantity": 2,
             "designators": ["R1"]},
            {"manufacturer": "YAGEO", "part_number": _RES.lower(),
             "quantity": 1},
            {"manufacturer": "Yageo", "part_number": "C", "quantity": 1,
             "sku": "311-X"},
            "RC0603"])
    problems = {p["line"]: " ".join(p["problems"])
                for p in caught.value.problems}
    assert sorted(problems) == [1, 2, 3, 4, 5, 6, 7]
    assert "no manufacturer" in problems[1]
    assert "no part_number" in problems[2]
    assert "no designators to count" in problems[3]
    assert "quantity 2 but 1 designators" in problems[4]
    assert "the same part as line 0" in problems[5]
    assert "unknown field(s) 'sku'" in problems[6]
    for bad in ([], None, "x"):
        with pytest.raises(BomInputError):
            lines_from_input(bad)


# ---- the tools, with Altium as the source ---------------------------------------

def _tools():
    captured = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    purchasing.register_purchasing_tools(_Capture())
    return captured


def _part(designator, maker, number, description="", kind="Standard"):
    params = {"Comment": "x", "Description": description,
              "Component Kind": kind}
    if maker is not None:
        params["Manufacturer"] = maker
    if number is not None:
        params["Part Number"] = number
    return {"designator": designator, "footprint": "FP",
            "parameters": params}


_BOARD = (
    [_part(f"R{n}", "Yageo", _RES, "RES 10K 1% 0603") for n in range(1, 5)]
    + [_part("C1", "Murata", _CAP, "CAP 100N 16V 0402"),
       _part("C2", "Murata", _CAP, "CAP 100N 16V 0402"),
       _part("U1", "Texas Instruments", _AMP, "IC OPAMP DUAL SOIC-8"),
       _part("J1", None, None, "HEADER"),
       _part("FD1", "N.A", "N.A", kind="Standard (No BOM)")])
_VARIANT = {"name": "Lite", "description": "", "variations": [
    {"designator": "R4", "kind": "Not Fitted", "alternate_part": "",
     "parameters": []},
    {"designator": "C2", "kind": "Not Fitted", "alternate_part": "",
     "parameters": []}]}


class _Altium:
    """Altium, as far as the purchasing BOM's four read commands go."""

    def __init__(self, board=_BOARD, active="Lite", limit_hit=False):
        self.board, self.active, self.limit_hit = board, active, limit_hit
        self.sent = []

    async def send_command_async(self, command, params, **kwargs):
        self.sent.append(command)
        if command == "project.get_bom":
            return {"components": [
                {"designator": c["designator"], "footprint": c["footprint"]}
                for c in self.board], "limit_hit": self.limit_hit,
                "skipped_nil": 0}
        if command == "project.get_active_variant":
            return {"name": self.active}
        if command == "project.get_variants":
            return {"variants": [_VARIANT]}
        if command == "project.get_component_info_batch":
            asked = params["designators"].split("~~")
            return {"components": [c for c in self.board
                                   if c["designator"] in asked]}
        raise AssertionError(f"a purchasing tool sent {command}")


def _altium(monkeypatch, **more):
    from eda_agent import bridge as bridge_module

    altium = _Altium(**more)
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: altium)
    return altium


def test_the_analysis_reads_the_fitted_parts_of_the_active_variant(
        monkeypatch):
    _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    out = asyncio.run(_tools()["part_purchasing_analyze"](build_quantity=3))
    assert out["ok"] and out["source"] == "altium"
    assert out["bom_summary"]["variant"] == "Lite"
    by_number = {l["part_number"]: l for l in out["lines"]}
    # R4 and C2 are not fitted in this variant and are not bought.
    assert by_number[_RES]["designators"] == ["R1", "R2", "R3"]
    assert (by_number[_RES]["quantity_per_board"],
            by_number[_RES]["required_quantity"]) == (3, 9)
    assert by_number[_CAP]["designators"] == ["C1"]
    assert by_number[_CAP]["required_quantity"] == 3
    assert by_number[_RES]["description"] == "RES 10K 1% 0603"
    assert out["not_fitted"] == ["C2", "R4"]
    assert out["not_in_bom"] == ["FD1"]
    assert [u["designator"] for u in out["unresolved"]] == ["J1"]
    assert out["summary"] == {
        "bom_lines": 3, "matched_at_both": 3, "matched_at_one": 0,
        "matched_at_none": 0, "unresolved_components": 1,
        "not_fitted_components": 2}
    # Only the four read commands were sent to Altium.
    assert set(altium.sent) == {
        "project.get_bom", "project.get_active_variant",
        "project.get_variants", "project.get_component_info_batch"}


def test_parts_not_fitted_can_be_counted_when_asked(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    out = asyncio.run(_tools()["part_purchasing_analyze"](
        include_not_fitted=True))
    by_number = {l["part_number"]: l for l in out["lines"]}
    assert by_number[_RES]["designators"] == ["R1", "R2", "R3", "R4"]
    assert by_number[_CAP]["quantity_per_board"] == 2
    assert out["not_fitted"] == []
    assert out["bom_summary"]["not_fitted_included"] is True


def test_the_plan_tool_proposes_and_says_what_it_could_not(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    out = asyncio.run(_tools()["part_purchasing_plan"](
        build_quantity=10, prefer="mouser"))
    assert out["ok"] and out["build_quantity"] == 10
    assert out["summary"] == {
        "bom_lines": 3, "planned": 3, "unpurchasable": 0,
        "unresolved_components": 1, "bom_complete": True}
    # J1 has no part number, so the plan is not the whole board.
    assert out["complete"] is False
    assert out["unresolved"][0]["designator"] == "J1"
    res = _line(out, _RES)
    assert res["required_quantity"] == 30
    assert res["choice"]["distributor"] == "mouser"
    assert res["choice"]["order_quantity"] == 30
    assert res["choice"]["extended_price"] == 0.51
    assert out["totals"]["distributors"]["mouser"]["lines"] == 3
    assert out["not_fitted"] == ["C2", "R4"]


def test_a_bom_cut_short_is_carried_into_the_plan(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch, limit_hit=True)
    analysis = asyncio.run(_tools()["part_purchasing_analyze"]())
    assert analysis["bom_complete"] is False
    plan = asyncio.run(_tools()["part_purchasing_plan"]())
    assert plan["complete"] is False
    assert plan["summary"]["bom_complete"] is False


def test_bad_arguments_are_refused_before_anything_is_asked(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    tools = _tools()
    for quantity in (0, -1, 2.5, "3", True, None):
        out = asyncio.run(tools["part_purchasing_analyze"](
            build_quantity=quantity))
        assert out["ok"] is False and "build_quantity" in out["reason"]
    out = asyncio.run(tools["part_purchasing_plan"](prefer="arrow"))
    assert out["ok"] is False and "lowest, digikey, mouser" in out["reason"]
    out = asyncio.run(tools["part_purchasing_plan"](
        lines=[{"manufacturer": "Yageo"}]))
    assert out["ok"] is False and out["problems"][0]["line"] == 0
    assert altium.sent == [] and dk.asked == [] and mo.asked == []


def test_a_project_that_cannot_be_read_is_reported(monkeypatch):
    _stocked(monkeypatch)

    class Broken:
        async def send_command_async(self, command, params, **kwargs):
            return {"error": "no project"}

    from eda_agent import bridge as bridge_module
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: Broken())
    out = asyncio.run(_tools()["part_purchasing_plan"]())
    assert out["ok"] is False
    assert "purchasing BOM could not be read" in out["reason"]


def test_lines_given_directly_do_not_touch_altium(monkeypatch):
    _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    out = asyncio.run(_tools()["part_purchasing_plan"](
        build_quantity=2, lines=[
            {"manufacturer": "Yageo", "part_number": _RES,
             "designators": ["R1", "R2"]}]))
    assert out["ok"] and out["source"] == "lines" and altium.sent == []
    assert out["lines"][0]["required_quantity"] == 4
    assert out["complete"] is True


# ---- the real providers, on recorded replies ------------------------------------

class _Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Wire:
    """Answers each request by the first route whose text is in its URL."""

    def __init__(self, monkeypatch, routes):
        self.routes, self.requests = dict(routes), []
        monkeypatch.setattr(_URLOPEN, self)

    def calls(self, part):
        return [r for r in self.requests if part in r.full_url]

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        for part, answer in self.routes.items():
            if part not in request.full_url:
                continue
            if isinstance(answer, tuple):
                raise urllib.error.HTTPError(
                    request.full_url, answer[0], "failed", {},
                    io.BytesIO(json.dumps(answer[1]).encode("utf-8")))
            return _Reply(json.dumps(answer).encode("utf-8"))
        raise AssertionError(f"unexpected request to {request.full_url}")


def _fixture(name):
    return json.loads((_FIXTURES / f"{name}.json").read_text("utf-8"))


@pytest.fixture
def recorded(monkeypatch):
    """Both distributors answering about LM358DR as they really did."""
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("MOUSER_API_KEY", "test-mouser-key")
    for var, _ in distributors.DigiKeyProvider._LOCALE_HEADERS:
        monkeypatch.delenv(var, raising=False)
    distributors._DIGIKEY_TOKENS.clear()
    yield _Wire(monkeypatch, {
        "oauth2/token": {"access_token": "t", "expires_in": 599},
        # Two makers share the number, so DigiKey refuses it by number...
        "search/LM358DR/productdetails":
            (404, _fixture("digikey_details_duplicate_404")),
        # ...and answers for its own number.
        "productdetails": _fixture("digikey_details_296-lm358drct-nd"),
        "products/v4/search/keyword": _fixture("digikey_keyword_lm358dr"),
        "api/v1/search/partnumber": _fixture("mouser_partnumber_lm358dr"),
    })
    distributors._DIGIKEY_TOKENS.clear()


_AMP_LINE = [{"manufacturer": "Texas Instruments", "part_number": _AMP,
              "quantity": 2, "designators": ["U1", "U2"],
              "description": "IC OPAMP DUAL SOIC-8"}]


def test_a_number_two_makers_share_is_resolved_by_the_boms_maker(recorded):
    found = enrich_bom(_AMP_LINE, 5)
    line = found["lines"][0]
    digikey, mouser = (line["distributors"][n] for n in ("digikey", "mouser"))
    assert digikey["status"] == mouser["status"] == "matched"
    assert digikey["manufacturer"] == mouser["manufacturer"] == \
        "Texas Instruments"
    # Texas Instruments' numbers only: none of Rohm's.
    assert [o["sku"] for o in digikey["offers"]] == [
        "296-LM358DRTR-ND", "296-LM358DRCT-ND", "296-LM358DRDKR-ND"]
    assert [o["sku"] for o in mouser["offers"]] == ["595-LM358DR"]
    # DigiKey's figures were read from the product itself, not the search.
    assert len(recorded.calls("296-LM358DRTR-ND/productdetails")) == 1
    plan = plan_purchases({"build_quantity": 5, **found})
    choice = plan["lines"][0]["choice"]
    assert plan["lines"][0]["required_quantity"] == 10
    assert choice["order_quantity"] == 10 and choice["unit_price"] == 0.175
    assert choice["extended_price"] == 1.75
    assert choice["sku"] in ("296-LM358DRCT-ND", "595-LM358DR")


def test_rohms_part_is_bought_only_when_the_bom_says_rohm(recorded):
    rohm = [{**_AMP_LINE[0], "manufacturer": "Rohm"}]
    found = enrich_bom(rohm, 1)
    digikey, mouser = (found["lines"][0]["distributors"][n]
                       for n in ("digikey", "mouser"))
    assert digikey["manufacturer"] == "Rohm Semiconductor"
    assert digikey["manufacturer_match"] == "partial"
    assert [o["sku"] for o in mouser["offers"]] == ["755-LM358DR"]
    # Mouser does not stock Rohm's: no price, so it is not proposed there.
    plan = plan_purchases({"build_quantity": 1, **found}, prefer="mouser")
    choice = plan["lines"][0]["choice"]
    assert choice["distributor"] == "digikey"
    assert "fallback: not bought at mouser" in " ".join(choice["notes"])
    # And a maker neither lists is a mismatch at both, bought nowhere.
    other = enrich_bom([{**_AMP_LINE[0], "manufacturer": "onsemi"}], 1)
    assert {a["status"] for a in other["lines"][0]["distributors"].values()} \
        == {"manufacturer_mismatch"}


def test_a_second_analysis_is_answered_from_the_cache(recorded, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE", "1")
    tools = _tools()
    first = asyncio.run(tools["part_purchasing_analyze"](lines=_AMP_LINE))
    asked = len(recorded.requests)
    assert asked > 0
    assert first["distributors"]["mouser"] == {
        "asked": 1, "matched": 1, "from_cache": 0, "read_now": 1, "ok": True}
    assert first["lines"][0]["distributors"]["mouser"]["cached"] is False

    again = asyncio.run(tools["part_purchasing_analyze"](lines=_AMP_LINE))
    # The one call made again: DigiKey refuses a number two makers share
    # with a 404, and a failed call is never kept. The search and both
    # products' figures came from the cache.
    assert [r.full_url for r in recorded.requests[asked:]] == [
        "https://api.digikey.com/products/v4/search/LM358DR/productdetails"]
    asked = len(recorded.requests)
    for name in ("digikey", "mouser"):
        assert again["distributors"][name]["from_cache"] == 1
        assert again["distributors"][name]["read_now"] == 0
        answer = again["lines"][0]["distributors"][name]
        assert answer["cached"] is True
        # When the reply was first read, not now. A fresh hit is stamped
        # as it is built and the cache entry as it is written, so the two
        # can fall either side of a second.
        then = datetime.strptime(
            first["lines"][0]["distributors"][name]["retrieved_at"],
            "%Y-%m-%dT%H:%M:%SZ")
        held = datetime.strptime(answer["retrieved_at"],
                                 "%Y-%m-%dT%H:%M:%SZ")
        assert abs((held - then).total_seconds()) <= 2
        assert answer["offers"] == \
            first["lines"][0]["distributors"][name]["offers"]

    fresh = asyncio.run(tools["part_purchasing_plan"](
        lines=_AMP_LINE, refresh=True))
    assert len(recorded.requests) > asked
    assert fresh["distributors"]["mouser"]["read_now"] == 1
    assert fresh["lines"][0]["choice"]["cached"] is False


def test_nothing_in_the_workflow_can_write_a_cart_or_a_list(recorded):
    asyncio.run(_tools()["part_purchasing_plan"](lines=_AMP_LINE))
    for request in recorded.requests:
        assert "/cart" not in request.full_url
        assert "/mylists" not in request.full_url
        assert "/order" not in request.full_url
    for module in (purchasing, purchasing_plan, purchasing_sources):
        source = inspect.getsource(module)
        for word in ("mouser_cart", "digikey_lists", "create_cart",
                     "add_items", "create_list", "MouserCart",
                     "DigiKeyLists"):
            assert word not in source, (module.__name__, word)
