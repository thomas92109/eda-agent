# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""DigiKey and Mouser hits carry what it takes to buy the part.

A hit used to be a part number, a maker, a description and a datasheet.
The distributors also say what is in stock, what it costs at each
quantity, in which packaging, under which ordering number and whether
the part is still made, and all of that was read and dropped.

The replies under ``tests/fixtures/distributors`` are the live ones for
LM358DR, read on 2026-11-16 and cut down: a part Texas Instruments and
Rohm both make under the same number, in stock at one and not the
other. Nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import io
import json
import pathlib
import urllib.error

import pytest

from eda_agent.libimport.providers import _sourcing, distributors, search_all
from eda_agent.libimport.providers.base import (
    AmbiguousPart,
    PartHit,
    ProviderError,
    ProviderUnavailable,
)
from eda_agent.libimport.providers.distributors import (
    DigiKeyProvider,
    MouserProvider,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "distributors"
_URLOPEN = "eda_agent.libimport.providers._distributor.urllib.request.urlopen"

_LEGACY_KEYS = ["ref", "provider", "part_id", "mpn", "manufacturer",
                "package", "description", "datasheet", "provenance",
                "license"]


def _fixture(name: str):
    return json.loads((_FIXTURES / f"{name}.json").read_text("utf-8"))


class _Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Wire:
    """Answers each request by the first route whose text is in its URL.

    A route's answer is a payload, an ``(http status, payload)`` pair
    for a failure, or a list of those used one per call.
    """

    def __init__(self, monkeypatch, routes):
        self.routes = dict(routes)
        self.requests = []
        monkeypatch.setattr(_URLOPEN, self)

    def calls(self, part: str) -> list:
        return [r for r in self.requests if part in r.full_url]

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        for part, answer in self.routes.items():
            if part not in request.full_url:
                continue
            if isinstance(answer, list):
                answer = answer.pop(0) if len(answer) > 1 else answer[0]
            if isinstance(answer, tuple):
                status, payload = answer[0], answer[1]
                headers = answer[2] if len(answer) > 2 else {}
                raise urllib.error.HTTPError(
                    request.full_url, status, "failed", headers,
                    io.BytesIO(json.dumps(payload).encode("utf-8")))
            return _Reply(json.dumps(answer).encode("utf-8"))
        raise AssertionError(f"unexpected request to {request.full_url}")


_TOKEN = {"access_token": "token-one", "expires_in": 599,
          "token_type": "Bearer"}


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    """Stand-in keys, no ambient locale, and no token left from a test."""
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("MOUSER_API_KEY", "test-mouser-key")
    for var, _ in DigiKeyProvider._LOCALE_HEADERS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("EDA_AGENT_PART_PROVIDERS", raising=False)
    distributors._DIGIKEY_TOKENS.clear()
    yield
    distributors._DIGIKEY_TOKENS.clear()


def _digikey(monkeypatch, **more):
    return _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "search/keyword": _fixture("digikey_keyword_lm358dr"),
        **more,
    })


def _mouser(monkeypatch, **more):
    return _Wire(monkeypatch, {
        "search/keyword": _fixture("mouser_keyword_lm358dr"),
        **more,
    })


def _by_maker(hits, word):
    return next(h for h in hits if word.lower() in h.manufacturer.lower())


# ---- DigiKey: keyword search -------------------------------------------

def test_digikey_search_keeps_identity_and_adds_sourcing(monkeypatch):
    _digikey(monkeypatch)
    hits = DigiKeyProvider().search("LM358DR", 20)

    # The same product is in ExactMatches and in Products: once each.
    assert sorted((h.mpn, h.manufacturer) for h in hits) == [
        ("LM358DR", "Rohm Semiconductor"),
        ("LM358DR", "Texas Instruments"),
        ("LM358DR2G", "onsemi"),
    ]
    ti = _by_maker(hits, "texas")
    assert ti.mpn == "LM358DR"
    assert ti.description == "IC OP AMP 2CH 8-SOIC"
    assert ti.datasheet.startswith("https://www.ti.com/")
    sourcing = ti.sourcing
    assert sourcing["distributor"] == "digikey"
    assert sourcing["product_url"].startswith(
        "https://www.digikey.com/en/products/detail/texas-instruments/")
    assert sourcing["stock"] == 72199
    assert sourcing["manufacturer_stock"] == 0
    assert sourcing["lead_time"] == {"raw": "26", "weeks": 26.0}
    assert sourcing["locale"] == {"site": "US", "language": "en",
                                  "currency": "USD"}
    assert "24 h" in sourcing["freshness"]
    assert sourcing["retrieved_at"].endswith("Z")


def test_digikey_packagings_stay_separate_offers(monkeypatch):
    """Reel, cut tape and Digi-Reel are three numbers with three prices."""
    _digikey(monkeypatch)
    ti = _by_maker(DigiKeyProvider().search("LM358DR"), "texas")
    offers = {o["distributor_pn"]: o for o in ti.sourcing["offers"]}

    assert sorted(offers) == ["296-LM358DRCT-ND", "296-LM358DRDKR-ND",
                              "296-LM358DRTR-ND"]
    reel, cut = offers["296-LM358DRTR-ND"], offers["296-LM358DRCT-ND"]
    assert reel["packaging"] == ["Tape & Reel (TR)"]
    assert cut["packaging"] == ["Cut Tape (CT)"]
    assert (reel["moq"], cut["moq"]) == (2500, 1)
    assert (reel["standard_package"], reel["stock"]) == (2500, 72199)
    assert reel["price_breaks"][0] == {"quantity": 2500,
                                       "unit_price": 0.10436}
    assert cut["price_breaks"][0] == {"quantity": 1, "unit_price": 0.26}
    assert len(reel["price_breaks"]) == 6 and len(cut["price_breaks"]) == 7
    assert {o["currency"] for o in offers.values()} == {"USD"}
    assert offers["296-LM358DRDKR-ND"]["provider_specific"][
        "digi_reel_fee"] == 7.0


def test_digikey_states_no_order_multiple(monkeypatch):
    """The API has no such field, so the standard pack is not passed off
    as one."""
    _digikey(monkeypatch)
    ti = _by_maker(DigiKeyProvider().search("LM358DR"), "texas")
    assert {o["multiple"] for o in ti.sourcing["offers"]} == {None}


def test_digikey_lifecycle_is_read_per_product(monkeypatch):
    _digikey(monkeypatch)
    hits = DigiKeyProvider().search("LM358DR")
    assert _by_maker(hits, "texas").sourcing["lifecycle"] == {
        "status": "active", "raw": "Active",
        "discontinued": False, "end_of_life": False}
    rohm = _by_maker(hits, "rohm").sourcing
    assert rohm["lifecycle"]["status"] == "nrnd"
    assert rohm["lifecycle"]["raw"] == "Not For New Designs"
    # In stock as cut tape, none on the reel: stock is per offer.
    assert {o["distributor_pn"]: o["stock"] for o in rohm["offers"]} == {
        "LM358DRTR-ND": 0, "LM358DRCT-ND": 1395, "LM358DRDKR-ND": 1395}


# ---- DigiKey: token and locale -----------------------------------------

def test_digikey_token_is_asked_for_once(monkeypatch):
    wire = _digikey(monkeypatch)
    DigiKeyProvider().search("LM358DR")
    DigiKeyProvider().search("LM358DR")
    assert len(wire.calls("oauth2/token")) == 1
    assert len(wire.calls("search/keyword")) == 2


def test_digikey_replaces_a_token_the_api_refuses(monkeypatch):
    wire = _Wire(monkeypatch, {
        "oauth2/token": [_TOKEN, {**_TOKEN, "access_token": "token-two"}],
        "search/keyword": [(401, {"title": "Unauthorized", "status": 401}),
                           _fixture("digikey_keyword_lm358dr")],
    })
    assert DigiKeyProvider().search("LM358DR")
    sent = [r.get_header("Authorization")
            for r in wire.calls("search/keyword")]
    assert sent == ["Bearer token-one", "Bearer token-two"]


def test_digikey_credentials_refused_twice_is_a_key_problem(monkeypatch):
    _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "search/keyword": (401, {"title": "Unauthorized", "status": 401}),
    })
    with pytest.raises(ProviderUnavailable) as excinfo:
        DigiKeyProvider().search("LM358DR")
    assert "DIGIKEY_CLIENT_ID" in str(excinfo.value)


def test_digikey_locale_is_sent_only_when_configured(monkeypatch):
    wire = _digikey(monkeypatch)
    DigiKeyProvider().search("LM358DR")
    plain = wire.calls("search/keyword")[0]
    assert not [h for h in plain.header_items() if "locale" in h[0].lower()]

    monkeypatch.setenv("DIGIKEY_LOCALE_SITE", "DE")
    monkeypatch.setenv("DIGIKEY_LOCALE_CURRENCY", "EUR")
    DigiKeyProvider().search("LM358DR")
    headers = {k.lower(): v
               for k, v in wire.calls("search/keyword")[1].header_items()}
    assert headers["x-digikey-locale-site"] == "DE"
    assert headers["x-digikey-locale-currency"] == "EUR"
    assert "x-digikey-locale-language" not in headers


# ---- DigiKey: fetch ------------------------------------------------------

def test_digikey_fetch_by_its_own_number_reads_product_details(monkeypatch):
    wire = _digikey(monkeypatch, **{
        "296-LM358DRCT-ND/productdetails":
            _fixture("digikey_details_296-lm358drct-nd")})
    detail = DigiKeyProvider().fetch("296-LM358DRCT-ND")

    assert not wire.calls("search/keyword"), "a details call is enough"
    assert detail["mpn"] == "LM358DR"
    assert detail["manufacturer"] == "Texas Instruments"
    assert detail["kind"] == "catalogue" and detail["files"] == {}
    assert detail["sourcing"]["freshness"] == "live: ProductDetails"
    assert len(detail["sourcing"]["offers"]) == 3
    assert "ambiguous" not in detail and "other_matches" not in detail


def test_digikey_fetch_of_a_number_two_makers_share_picks_neither(
        monkeypatch):
    """Measured: ProductDetails answers LM358DR with 404 "Duplicate
    Products found". The fetch is refused with both makers listed, each
    with the ref and the ordering numbers that name it alone."""
    wire = _digikey(monkeypatch, **{
        "search/LM358DR/productdetails":
            (404, _fixture("digikey_details_duplicate_404"))})
    with pytest.raises(AmbiguousPart) as excinfo:
        DigiKeyProvider().fetch("LM358DR")

    assert "None was chosen" in str(excinfo.value)
    ti, rohm = excinfo.value.candidates
    assert (ti["manufacturer"], ti["mpn"]) == ("Texas Instruments",
                                               "LM358DR")
    assert ti["ref"] == "digikey:296-LM358DRTR-ND"
    assert ti["offers"] == [
        {"distributor_pn": "296-LM358DRTR-ND",
         "packaging": ["Tape & Reel (TR)"]},
        {"distributor_pn": "296-LM358DRCT-ND",
         "packaging": ["Cut Tape (CT)"]},
        {"distributor_pn": "296-LM358DRDKR-ND",
         "packaging": ["Digi-Reel®"]},
    ]
    assert rohm["manufacturer"] == "Rohm Semiconductor"
    assert rohm["ref"] == "digikey:LM358DRTR-ND"
    # One details call (the 404) and one search: no product was read.
    assert len(wire.calls("productdetails")) == 1
    assert len(wire.calls("search/keyword")) == 1


def test_digikey_products_sharing_an_mpn_get_distinct_refs(monkeypatch):
    """Two makers' LM358DR must not both be ``digikey:LM358DR``. A part
    whose MPN is its own keeps the MPN ref it always had."""
    _digikey(monkeypatch)
    hits = DigiKeyProvider().search("LM358DR")
    refs = {h.manufacturer: h.ref for h in hits}
    assert refs == {
        "Texas Instruments": "digikey:296-LM358DRTR-ND",
        "Rohm Semiconductor": "digikey:LM358DRTR-ND",
        "onsemi": "digikey:LM358DR2G",
    }
    assert all(h.mpn in ("LM358DR", "LM358DR2G") for h in hits)


def test_digikey_any_packaging_number_fetches_its_one_product(monkeypatch):
    """Reel, cut tape and Digi-Reel numbers all name the TI part."""
    live = _fixture("digikey_details_296-lm358drct-nd")
    _digikey(monkeypatch, productdetails=live)
    for number in ("296-LM358DRTR-ND", "296-LM358DRCT-ND",
                   "296-LM358DRDKR-ND"):
        detail = DigiKeyProvider().fetch(number)
        assert detail["manufacturer"] == "Texas Instruments"
        assert number in [o["distributor_pn"]
                          for o in detail["sourcing"]["offers"]]
        assert len(detail["sourcing"]["offers"]) == 3


def test_digikey_fetch_by_an_mpn_only_one_maker_uses_still_works(
        monkeypatch):
    """The MPN ref of before: 404 from details, one match in the search,
    read live by its DigiKey number."""
    live = _fixture("digikey_details_296-lm358drct-nd")
    live["Product"]["ManufacturerProductNumber"] = "LM358DR2G"
    wire = _digikey(monkeypatch, **{
        "search/LM358DR2G/productdetails":
            (404, _fixture("digikey_details_missing_404")),
        "LM358DR2GOSTR-ND/productdetails": live})
    detail = DigiKeyProvider().fetch("LM358DR2G")
    assert detail["mpn"] == "LM358DR2G"
    assert detail["sourcing"]["freshness"] == "live: ProductDetails"
    assert len(wire.calls("productdetails")) == 2


def test_digikey_fetch_of_an_absent_part_is_an_error(monkeypatch):
    _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "productdetails": (404, _fixture("digikey_details_missing_404")),
        "search/keyword": {"Products": [], "ExactMatches": [],
                           "ProductsCount": 0},
    })
    with pytest.raises(ProviderError) as excinfo:
        DigiKeyProvider().fetch("NOSUCHPART-ZZZ-999")
    assert not isinstance(excinfo.value, ProviderUnavailable)
    assert "no part" in str(excinfo.value)


def test_digikey_rate_limit_names_the_wait(monkeypatch):
    _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "search/keyword": (429, {"ErrorMessage": "BurstLimit exceeded",
                                 "StatusCode": 429}, {"Retry-After": "17"}),
    })
    with pytest.raises(ProviderUnavailable) as excinfo:
        DigiKeyProvider().search("LM358DR")
    message = str(excinfo.value)
    assert "rate-limited" in message and "17 s" in message
    assert "may still exist" in message


# ---- Mouser: keyword search ---------------------------------------------

def test_mouser_search_keeps_identity_and_adds_sourcing(monkeypatch):
    _mouser(monkeypatch)
    hits = MouserProvider().search("LM358DR", 20)
    assert [h.mpn for h in hits] == ["LM358DR", "LM358DR", "LM358DR2G"]

    ti = _by_maker(hits, "texas")
    sourcing = ti.sourcing
    assert sourcing["distributor"] == "mouser"
    assert sourcing["product_url"].startswith(
        "https://www.mouser.com/en/ProductDetail/Texas-Instruments/")
    assert sourcing["stock"] == 50881
    assert sourcing["manufacturer_stock"] == 0
    assert sourcing["lead_time"] == {"raw": "182 Days", "weeks": 26.0}
    assert sourcing["on_order"] == [
        {"quantity": 97500, "date": "2027-04-15"},
        {"quantity": 110000, "date": "2027-08-09"}]
    (offer,) = sourcing["offers"]
    assert offer["distributor_pn"] == "595-LM358DR"
    assert offer["packaging"] == ["Reel", "Cut Tape", "MouseReel"]
    assert (offer["moq"], offer["multiple"]) == (1, 1)
    assert offer["standard_package"] == 2500
    assert offer["currency"] == "USD"
    assert len(offer["price_breaks"]) == 9
    assert offer["price_breaks"][0] == {"quantity": 1, "unit_price": 0.26}
    assert offer["price_breaks"][-1] == {"quantity": 5000,
                                         "unit_price": 0.103}


def test_mouser_missing_lifecycle_is_not_active(monkeypatch):
    """Mouser sends null for a part in production. That is "not stated"."""
    _mouser(monkeypatch)
    ti = _by_maker(MouserProvider().search("LM358DR"), "texas")
    assert ti.sourcing["lifecycle"] == {
        "status": None, "raw": None,
        "discontinued": None, "end_of_life": None}


def test_mouser_part_it_does_not_stock_has_nothing_invented(monkeypatch):
    """The Rohm LM358DR: no stock figure, no prices, a replacement."""
    _mouser(monkeypatch)
    rohm = _by_maker(MouserProvider().search("LM358DR"), "rohm").sourcing
    (offer,) = rohm["offers"]
    assert rohm["stock"] is None and offer["stock"] is None
    assert rohm["manufacturer_stock"] is None
    assert offer["price_breaks"] == [] and offer["currency"] is None
    assert (offer["moq"], offer["multiple"]) == (2500, 2500)
    assert rohm["lifecycle"]["status"] == "nrnd"
    assert rohm["lifecycle"]["raw"] == "Not Recommended for New Designs"
    assert rohm["suggested_replacement"] == "755-LM358FJ-GE2"
    assert rohm["on_order"] == []


def test_mouser_price_text_is_read_only_without_the_number(monkeypatch):
    reply = _fixture("mouser_partnumber_595-lm358dr")
    part = reply["SearchResults"]["Parts"][0]
    part["PriceBreaks"] = [
        {"Quantity": 1, "Price": "0,285 €", "Currency": "EUR"},
        {"Quantity": 2000, "Price": "1.234 €", "Currency": "EUR"},
        {"Quantity": 5000, "Price": "$9.99", "RawPrice": 0.05,
         "Currency": "EUR"},
    ]
    _Wire(monkeypatch, {"search/keyword": reply})
    (offer,) = MouserProvider().search("LM358DR")[0].sourcing["offers"]
    assert offer["currency"] == "EUR"
    assert offer["price_breaks"] == [
        {"quantity": 1, "unit_price": 0.285},
        # Thousands or decimals? Left as text, not guessed.
        {"quantity": 2000, "unit_price": None, "price_text": "1.234 €"},
        {"quantity": 5000, "unit_price": 0.05},
    ]


# ---- Mouser: fetch -------------------------------------------------------

def test_mouser_fetch_asks_the_part_number_search_exactly(monkeypatch):
    wire = _mouser(monkeypatch, **{
        "search/partnumber": _fixture("mouser_partnumber_595-lm358dr")})
    detail = MouserProvider().fetch("595-LM358DR")

    assert not wire.calls("search/keyword")
    (call,) = wire.calls("search/partnumber")
    assert json.loads(call.data) == {"SearchByPartRequest": {
        "mouserPartNumber": "595-LM358DR", "partSearchOptions": "Exact"}}
    assert detail["mpn"] == "LM358DR"
    assert detail["kind"] == "catalogue" and detail["files"] == {}
    assert detail["sourcing"]["offers"][0]["distributor_pn"] == "595-LM358DR"
    assert "ambiguous" not in detail


def test_mouser_fetch_of_a_number_two_makers_share_picks_neither(
        monkeypatch):
    _mouser(monkeypatch, **{
        "search/partnumber": _fixture("mouser_partnumber_lm358dr")})
    with pytest.raises(AmbiguousPart) as excinfo:
        MouserProvider().fetch("LM358DR")
    assert [(c["ref"], c["manufacturer"], c["mpn"])
            for c in excinfo.value.candidates] == [
        ("mouser:595-LM358DR", "Texas Instruments", "LM358DR"),
        ("mouser:755-LM358DR", "ROHM Semiconductor", "LM358DR")]
    assert excinfo.value.candidates[0]["offers"] == [{
        "distributor_pn": "595-LM358DR",
        "packaging": ["Reel", "Cut Tape", "MouseReel"]}]


def test_mouser_products_sharing_an_mpn_get_distinct_refs(monkeypatch):
    _mouser(monkeypatch)
    hits = MouserProvider().search("LM358DR")
    assert {h.manufacturer: h.ref for h in hits} == {
        "Texas Instruments": "mouser:595-LM358DR",
        "ROHM Semiconductor": "mouser:755-LM358DR",
        "onsemi": "mouser:LM358DR2G",
    }


def test_mouser_fetch_of_an_absent_part_is_an_error(monkeypatch):
    empty = _fixture("mouser_partnumber_missing")
    wire = _Wire(monkeypatch, {"search/partnumber": empty,
                               "search/keyword": empty})
    with pytest.raises(ProviderError) as excinfo:
        MouserProvider().fetch("NOSUCHPART-ZZZ-999")
    assert not isinstance(excinfo.value, ProviderUnavailable)
    assert len(wire.calls("search/keyword")) == 1, (
        "the keyword search is the fallback, as fetch always worked")


def test_mouser_call_limit_is_not_reported_as_a_bad_key(monkeypatch):
    """Mouser refuses a client over its limit with HTTP 403. Read as a
    rejected key, that sends the user to replace one that works."""
    _Wire(monkeypatch, {"search/keyword": (403, {"Errors": [{
        "Id": 0, "Code": "TooManyRequests",
        "Message": "Maximum calls per day exceeded.",
        "ResourceKey": "MaxCallPerDay"}]})})
    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("LM358DR")
    message = str(excinfo.value)
    assert "rate-limited" in message and "may still exist" in message
    assert "rejected the credential" not in message


def test_a_plain_403_is_still_a_key_problem(monkeypatch):
    _Wire(monkeypatch, {"search/keyword": (403, {"Message": "Forbidden"})})
    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("LM358DR")
    assert "MOUSER_API_KEY" in str(excinfo.value)


def test_a_failure_body_that_echoes_a_key_does_not_leak_it(monkeypatch):
    _Wire(monkeypatch, {"search/keyword": (500, {
        "detail": "bad request for apiKey=test-mouser-key"})})
    with pytest.raises(ProviderError) as excinfo:
        MouserProvider().search("LM358DR")
    assert "test-mouser-key" not in str(excinfo.value)
    assert "HTTP 500" in str(excinfo.value)


# ---- replies that are not the documented shape ---------------------------

@pytest.mark.parametrize("payload", [
    {"Products": "none"},
    {"Products": [None, 7, "x"], "ExactMatches": None},
    {"Products": [{"ManufacturerProductNumber": "X1",
                   "ProductVariations": "n/a", "ProductStatus": "Active",
                   "QuantityAvailable": "plenty",
                   "ManufacturerLeadWeeks": None}]},
    {"Products": [{"ManufacturerProductNumber": "X1", "ProductVariations": [
        None, {"StandardPricing": [None, {"BreakQuantity": "many"},
                                   {"BreakQuantity": 10}]}]}],
     "SearchLocaleUsed": "US"},
    [],
])
def test_digikey_survives_a_malformed_reply(monkeypatch, payload):
    _Wire(monkeypatch, {"oauth2/token": _TOKEN,
                        "search/keyword": [payload]})
    hits = DigiKeyProvider().search("X1")
    for hit in hits:
        json.dumps(hit.to_dict())
        assert hit.sourcing["stock"] is None
        assert hit.sourcing["lifecycle"]["status"] is None
        for offer in hit.sourcing["offers"]:
            assert offer["currency"] is None
            assert all(b["unit_price"] is None
                       for b in offer["price_breaks"])


@pytest.mark.parametrize("payload", [
    {"SearchResults": None},
    {"SearchResults": {"Parts": "none"}},
    {"SearchResults": {"Parts": [None, {"Manufacturer": "no number"}]}},
    {"SearchResults": {"Parts": [{
        "ManufacturerPartNumber": "X1", "PriceBreaks": "call",
        "ProductAttributes": None, "AvailabilityOnOrder": [None, 3],
        "AvailabilityInStock": "None", "Min": "", "Mult": None,
        "LeadTime": "26 Tage", "LifecycleStatus": "Abgekuendigt"}]}},
    [],
])
def test_mouser_survives_a_malformed_reply(monkeypatch, payload):
    _Wire(monkeypatch, {"search/keyword": [payload]})
    hits = MouserProvider().search("X1")
    for hit in hits:
        json.dumps(hit.to_dict())
        (offer,) = hit.sourcing["offers"]
        assert offer["stock"] is None and offer["moq"] is None
        assert offer["price_breaks"] == [] and offer["packaging"] == []
        # A unit and a status this does not know keep their text only.
        assert hit.sourcing["lead_time"] == {"raw": "26 Tage",
                                             "weeks": None}
        assert hit.sourcing["lifecycle"]["status"] is None
        assert hit.sourcing["lifecycle"]["raw"] == "Abgekuendigt"


# ---- the readers ---------------------------------------------------------

@pytest.mark.parametrize("text, value", [
    ("$0.26", 0.26), ("$623.98", 623.98), ("$9,999.99", 9999.99),
    ("0,285 €", 0.285), ("€ 2,10", 2.10), ("87,10 €", 87.10),
    ("6.817,20 kr", 6817.20), ("1 234,50 Ft", 1234.50), ("$0.103", 0.103),
    ("1,234,567", 1234567.0), ("12", 12.0),
    ("1.234 kr", None), ("$1,000", None), ("call", None), ("", None),
    (None, None),
])
def test_parse_price(text, value):
    assert _sourcing.parse_price(text) == value


@pytest.mark.parametrize("raw, unit, expected", [
    ("26", "weeks", {"raw": "26", "weeks": 26.0}),
    ("6 weeks", "", {"raw": "6 weeks", "weeks": 6.0}),
    ("182 Days", "", {"raw": "182 Days", "weeks": 26.0}),
    ("0 Days", "", {"raw": "0 Days", "weeks": 0.0}),
    ("226 Tage", "", {"raw": "226 Tage", "weeks": None}),
    ("26", "", {"raw": "26", "weeks": None}),
    ("", "weeks", None), (None, "", None),
])
def test_lead_time(raw, unit, expected):
    assert _sourcing.lead_time(raw, unit) == expected


@pytest.mark.parametrize("raw, status", [
    ("Active", "active"), ("Not For New Designs", "nrnd"),
    ("Not Recommended for New Designs", "nrnd"),
    ("Last Time Buy", "last_time_buy"), ("End of Life", "end_of_life"),
    ("Obsolete", "obsolete"), ("Discontinued at Digi-Key", "discontinued"),
    ("Preliminary", "preliminary"), ("New Product", "new"),
    ("Factory Special Order", None), ("", None), (None, None),
])
def test_lifecycle_words(raw, status):
    assert _sourcing.lifecycle(raw)["status"] == status


def test_whole_numbers_only():
    assert _sourcing.to_int("50881") == 50881
    assert _sourcing.to_int(2500.0) == 2500
    assert _sourcing.to_int("26 Days") is None
    assert _sourcing.to_int(True) is None
    assert _sourcing.to_int(None) is None


# ---- what reaches a caller ------------------------------------------------

def test_a_hit_without_sourcing_serializes_as_it_always_did():
    hit = PartHit(provider="kicad_local", part_id="Device:R", mpn="R")
    assert list(hit.to_dict()) == _LEGACY_KEYS


def test_a_distributor_hit_keeps_every_old_key_and_adds_one(monkeypatch):
    _mouser(monkeypatch)
    out = MouserProvider().search("LM358DR")[0].to_dict()
    assert list(out) == _LEGACY_KEYS + ["sourcing"]
    assert "no symbol or footprint" in out["provenance"]
    assert json.loads(json.dumps(out)) == out


def _tools():
    from eda_agent.tools.parts import register_parts_tools

    captured = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_parts_tools(_Capture())
    return captured


def _both(monkeypatch, **more):
    monkeypatch.setenv("EDA_AGENT_PART_PROVIDERS", "digikey,mouser")
    return _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "digikey.com/products/v4/search/keyword":
            _fixture("digikey_keyword_lm358dr"),
        "mouser.com/api/v1/search/keyword":
            _fixture("mouser_keyword_lm358dr"),
        **more,
    })


def test_part_search_reply_carries_sourcing_from_both(monkeypatch):
    _both(monkeypatch)
    out = asyncio.run(_tools()["part_search"](query="LM358DR"))

    assert out["ok"] is True and out["count"] == 6
    assert {h["provider"] for h in out["hits"]} == {"digikey", "mouser"}
    for hit in out["hits"]:
        assert hit["kind"] == "catalogue" and hit["import_with"] == []
        assert hit["sourcing"]["offers"], hit["ref"]
    assert out["providers"]["digikey"]["verified_live"] is True
    assert json.loads(json.dumps(out)) == out
    numbers = {o["distributor_pn"] for h in out["hits"]
               for o in h["sourcing"]["offers"]}
    assert {"296-LM358DRCT-ND", "595-LM358DR"} <= numbers


def test_every_search_ref_is_distinct_and_fetches_its_own_product(
        monkeypatch):
    """The workflow a BOM line runs: search, then fetch a hit by its ref.
    No two hits share a ref, and each ref returns the product it was on."""
    live = _fixture("digikey_details_296-lm358drct-nd")
    _both(monkeypatch, **{
        "296-LM358DRTR-ND/productdetails": live,
        "search/partnumber": _fixture("mouser_partnumber_595-lm358dr")})
    tools = _tools()
    hits = asyncio.run(tools["part_search"](query="LM358DR"))["hits"]
    refs = [h["ref"] for h in hits]
    assert len(set(refs)) == len(refs) == 6

    for ref in ("digikey:296-LM358DRTR-ND", "mouser:595-LM358DR"):
        assert ref in refs
        out = asyncio.run(tools["part_fetch"](part_id=ref))
        assert out["ok"] is True
        assert out["detail"]["manufacturer"] == "Texas Instruments"


def test_part_fetch_of_a_shared_mpn_lists_the_candidates(monkeypatch):
    _both(monkeypatch, **{
        "productdetails": (404, _fixture("digikey_details_duplicate_404")),
        "search/partnumber": _fixture("mouser_partnumber_lm358dr")})
    fetch = _tools()["part_fetch"]
    for provider, refs in (
            ("digikey", ["digikey:296-LM358DRTR-ND",
                         "digikey:LM358DRTR-ND"]),
            ("mouser", ["mouser:595-LM358DR", "mouser:755-LM358DR"])):
        out = asyncio.run(fetch(part_id=f"{provider}:LM358DR"))
        assert out["ok"] is False and out["ambiguous"] is True
        assert "detail" not in out, "no product may be chosen"
        assert out["provider"] == provider and out["part_id"] == "LM358DR"
        assert [c["ref"] for c in out["candidates"]] == refs
        assert all(c["manufacturer"] and c["offers"]
                   for c in out["candidates"])
        assert json.loads(json.dumps(out)) == out


def test_one_distributor_failing_leaves_the_other(monkeypatch):
    _both(monkeypatch, **{
        "oauth2/token": (401, {"title": "Unauthorized"})})
    result = search_all("LM358DR")
    assert result["providers"]["digikey"]["ok"] is False
    assert "unavailable" in result["providers"]["digikey"]
    assert result["providers"]["mouser"]["ok"] is True
    assert {h["provider"] for h in result["hits"]} == {"mouser"}


def test_part_fetch_reply_carries_sourcing(monkeypatch):
    _both(monkeypatch, **{
        "productdetails": _fixture("digikey_details_296-lm358drct-nd"),
        "search/partnumber": _fixture("mouser_partnumber_595-lm358dr")})
    fetch = _tools()["part_fetch"]

    digikey = asyncio.run(fetch(part_id="digikey:296-LM358DRCT-ND"))
    mouser = asyncio.run(fetch(part_id="595-LM358DR", provider="mouser"))
    for out, number in ((digikey, "296-LM358DRCT-ND"),
                        (mouser, "595-LM358DR")):
        assert out["ok"] is True and out["summary"] is None
        assert "detail_path" not in out, "the detail fits one reply"
        offers = out["detail"]["sourcing"]["offers"]
        assert number in [o["distributor_pn"] for o in offers]
        assert all(o["price_breaks"] for o in offers)
        assert json.loads(json.dumps(out)) == out
