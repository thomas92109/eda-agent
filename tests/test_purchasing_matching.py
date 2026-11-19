# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Close manufacturer part number candidates: labels, evidence, approvals.

Synthetic parts only (Acme, Diodes-style and TI-style maker names are used
because the maker conventions are keyed on a word of the name).
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from eda_agent.export import purchasing_matching as pm

REEL = [{"sku": "ACM-TR-ND", "packaging": ["Tape & Reel (TR)"], "currency": "USD",
         "price_breaks": [{"quantity": 1, "unit_price": 0.50},
                          {"quantity": 100, "unit_price": 0.30}]}]
TRAY = [{"sku": "ACM-TY-ND", "packaging": ["Tray"], "currency": "USD",
         "price_breaks": [{"quantity": 1, "unit_price": 2.0}]}]


def cls(bom="ABC123", cand="ABC123-TR", maker="Acme", cmaker=None,
        match="exact", offers=REEL, bom_desc="IC WIDGET 3V SOIC-8",
        cand_desc="IC WIDGET 3V SOIC-8 TAPE REEL", qty=10, stock=500):
    return pm.classify(
        bom_manufacturer=maker, bom_mpn=bom, bom_description=bom_desc,
        candidate_mpn=cand, candidate_manufacturer=cmaker or maker,
        candidate_description=cand_desc, manufacturer_match=match,
        offers=offers, distributor="digikey", required_quantity=qty,
        stock=stock)


# ---- the examples in the request -----------------------------------------------------

def test_a_confirmed_packaging_suffix_is_likely():
    c = cls("ABC123", "ABC123-TR")
    assert c["label"] == pm.LIKELY and c["kind"] == "suffix"
    assert c["suffix"] == "-TR" and c["difference"] == "-TR"
    assert "tape and reel" in c["meaning"]
    text = " ".join(c["evidence"])
    assert "recognised packaging marker" in text and "Tape & Reel" in text
    assert "identical" in text


def test_an_unverified_variant_suffix_is_possible():
    c = cls("ABC123", "ABC123A5", offers=TRAY)
    assert c["label"] == pm.POSSIBLE and c["suffix"] == "A5"
    assert any("must be verified" in d for d in c["uncertainty"])


def test_a_much_longer_suffix_is_uncertain():
    c = cls("ABC123", "ABC123XYZ789", offers=TRAY)
    assert c["label"] == pm.UNCERTAIN and c["suffix"] == "XYZ789"
    assert any("probably a different variant" in d for d in c["uncertainty"])


def test_punctuation_only_is_its_own_verified_class():
    c = cls("USB4110GFA", "USB4110-GF-A", maker="Global Connector Technology",
            cmaker="GCT", match="partial", offers=TRAY,
            bom_desc="USB-C receptacle 24 position",
            cand_desc="USB-C receptacle 24 position right angle")
    assert c["label"] == pm.FORMATTING and c["kind"] == "punctuation"
    assert c["suffix"] == "" and "adds 2 x '-'" in c["difference"]
    assert any("letters and digits are identical" in e for e in c["evidence"])
    assert any("prefix" in d for d in c["uncertainty"])    # maker only partial


# ---- suffix length is never the only input ----------------------------------------------

def test_a_known_marker_is_likely_even_when_it_is_four_letters_long():
    c = cls("ABC123", "ABC123-REEL")
    assert c["label"] == pm.LIKELY


def test_a_short_unknown_suffix_is_only_possible_and_a_long_one_uncertain():
    assert cls("ABC123", "ABC123Z", offers=TRAY)["label"] == pm.POSSIBLE
    assert cls("ABC123", "ABC123ZZZZ", offers=TRAY)["label"] == pm.POSSIBLE
    assert cls("ABC123", "ABC123ZZZZZ", offers=TRAY)["label"] == pm.UNCERTAIN


def test_a_recognised_marker_the_distributor_does_not_confirm_is_possible():
    c = cls("ABC123", "ABC123-TR", offers=TRAY)
    assert c["label"] == pm.POSSIBLE
    assert any("lists no tape or reel packaging" in d for d in c["uncertainty"])


def test_a_partial_manufacturer_caps_a_packaging_match_at_possible():
    c = cls("ABC123", "ABC123-TR", match="partial", cmaker="Acme Electronics")
    assert c["label"] == pm.POSSIBLE


def test_a_suffix_that_continues_the_number_is_uncertain():
    c = cls("LM358", "LM3584", offers=REEL)
    assert c["label"] == pm.UNCERTAIN
    assert any("continues the digits" in d for d in c["uncertainty"])
    # with a separator the boundary is clear, so it is an ordinary suffix
    assert cls("LM358", "LM358-4", offers=TRAY)["label"] == pm.POSSIBLE


def test_descriptions_are_shown_as_weak_evidence_and_never_move_a_label():
    for desc in ("IC WIDGET 3V SOIC-8 TAPE REEL", "CONN HDR 40POS"):
        likely = cls("ABC123", "ABC123-TR", cand_desc=desc)
        assert likely["label"] == pm.LIKELY
        possible = cls("ABC123", "ABC123A5", offers=TRAY, cand_desc=desc)
        assert possible["label"] == pm.POSSIBLE
    odd = cls("ABC123", "ABC123A5", offers=TRAY, cand_desc="CONN HDR 40POS")
    assert any("share almost no words" in d and "weak" in d
               for d in odd["uncertainty"])
    same = cls("ABC123", "ABC123A5", offers=TRAY)
    assert "the descriptions agree" in same["evidence"]


def test_no_description_cannot_make_a_candidate_more_likely():
    base = cls("ABC123", "ABC123A5", offers=TRAY, cand_desc="")
    assert base["label"] == pm.POSSIBLE
    assert any("could not be compared" in d for d in base["uncertainty"])


# ---- maker conventions --------------------------------------------------------------------

def test_a_maker_convention_counts_only_with_the_makers_name_and_reel_packaging():
    diodes = cls("DMN6075S", "DMN6075S-7", maker="Diodes Incorporated")
    assert diodes["label"] == pm.LIKELY and "7 inch" in diodes["meaning"]
    assert cls("DMN6075S", "DMN6075S-7", maker="Acme")["label"] == pm.POSSIBLE
    assert cls("DMN6075S", "DMN6075S-7", maker="Diodes Incorporated",
               offers=TRAY)["label"] == pm.POSSIBLE
    ti = cls("LM358D", "LM358DR", maker="Texas Instruments")
    assert ti["label"] == pm.LIKELY and ti["suffix"] == "R"
    assert cls("LM358D", "LM358DR", maker="Acme")["label"] == pm.POSSIBLE


# ---- what is not a close match ----------------------------------------------------------------

@pytest.mark.parametrize("bom,cand,match", [
    ("ABC123", "ABC123", "exact"),                 # exact
    ("ABC123", "abc 123", "exact"),                # exact apart from case and space
    ("ABC123", "ABC12", "exact"),                  # shorter than the BOM's
    ("ABC123", "XABC123", "exact"),                # a prefix is not a suffix
    ("ABC123", "ABD123", "exact"),                 # a different number
])
def test_these_are_not_close_matches(bom, cand, match):
    assert cls(bom, cand, match=match) is None


def test_the_result_has_a_label_and_evidence_but_no_percentage():
    c = cls("ABC123", "ABC123-TR")
    for key in c:
        assert not any(w in key for w in ("percent", "confidence", "score",
                                          "probability"))
    assert c["label"] in pm.LABELS
    assert not any("%" in str(x) for x in c["evidence"] + c["uncertainty"])


def test_stock_and_price_are_for_the_quantity_needed():
    c = cls(qty=10)
    assert c["unit_price"] == D("0.5") and c["currency"] == "USD"
    assert cls(qty=500)["unit_price"] == D("0.3")
    assert cls(qty=500)["price_at_quantity"] == 500
    assert c["stock"] == 500 and c["skus"] == ["ACM-TR-ND"]
    assert cls(offers=[])["unit_price"] is None


# ---- order --------------------------------------------------------------------------------------

def test_candidates_are_listed_most_likely_first():
    cands = [
        cls("A1", "A1XYZ789", offers=TRAY),
        cls("A2", "A2A5", offers=TRAY),
        cls("A3", "A3-TR"),
        cls("USB4110GFA", "USB4110-GF-A", offers=TRAY),
    ]
    got = [c["label"] for c in pm.order(cands)]
    assert got == [pm.FORMATTING, pm.LIKELY, pm.POSSIBLE, pm.UNCERTAIN]


def test_within_a_label_identical_maker_then_stock_then_number():
    a = cls("B1", "B1A", offers=TRAY, stock=0)
    b = cls("B1", "B1B", offers=TRAY, stock=900)
    c = cls("B1", "B1C", offers=TRAY, stock=900, match="partial",
            cmaker="Acme Co")
    d = cls("B0", "B0D", offers=TRAY, stock=900)
    assert [x["candidate_mpn"] for x in pm.order([a, b, c, d])] == [
        "B0D", "B1B", "B1A", "B1C"]


# ---- helpers ----------------------------------------------------------------------------------------

def test_prefix_query_finds_a_term_a_keyword_search_would_match():
    assert pm.prefix_query("USB4110GFA") == "USB4110"
    assert pm.prefix_query("DMN6075S-7") == "DMN6075"
    assert pm.prefix_query("LM358") is None            # already the prefix
    assert pm.prefix_query("AB1") is None              # too short to be useful


@pytest.mark.parametrize("text,key", [("ABC-123/x", "abc123x"), ("", ""),
                                      (None, "")])
def test_alnum(text, key):
    assert pm.alnum(text) == key


# ---- approvals ------------------------------------------------------------------------------------------

def test_approvals_are_checked_and_keyed_by_letters_and_digits():
    got = pm.parse_approvals([{"original_mpn": "USB4110GFA",
                               "candidate_mpn": "USB4110-GF-A"},
                              {"original_mpn": " ABC123 ",
                               "candidate_mpn": "abc123-tr"}])
    # the exact spelling of the candidate is what is approved
    assert pm.approval_key("USB4110GFA", "USB4110GF-A") not in got
    assert pm.approval_key("USB4110GFA", "usb4110gfa") not in got
    assert pm.approval_key("USB4110GFA", "USB4110-GF-A") in got
    assert pm.approval_key("USB4110 GFA", "usb4110-gf-a") in got
    assert pm.approval_key("abc 123", "ABC123-TR") in got
    assert pm.parse_approvals(None) == {} and pm.parse_approvals([]) == {}


@pytest.mark.parametrize("bad", [
    "x", {"a": 1}, [1], [{"original_mpn": "A"}],
    [{"original_mpn": "A", "candidate_mpn": "A"}],
    [{"original_mpn": "ABC123", "candidate_mpn": "XYZ999"}],      # not a variant
    [{"original_mpn": "ABC123", "candidate_mpn": "ABC12"}],       # shorter
    [{"original_mpn": "A1", "candidate_mpn": "A1-X", "price": 1}],
])
def test_a_substitution_that_is_not_a_close_variant_is_refused(bad):
    with pytest.raises(pm.MatchInputError):
        pm.parse_approvals(bad)


# ---- manufacturer identity is evidence, not a filter ---------------------------------

def test_a_different_maker_name_is_listed_but_never_likelier_than_uncertain():
    for cand in ("ABC123-TR", "ABC123A5", "ABC123"[:6] + "-"):
        c = cls("ABC123", cand, match="different", cmaker="Zeta Corp")
        if c is None:
            continue
        assert c["label"] == pm.UNCERTAIN
        assert c["manufacturer_override"] is True
        assert any("names differ" in d for d in c["uncertainty"])
    fmt = cls("ABC-123", "ABC123", match="different", cmaker="Zeta Corp")
    assert fmt["label"] == pm.UNCERTAIN and fmt["kind"] == "punctuation"


def test_a_known_short_name_counts_like_a_partial_match_and_says_so():
    c = cls("USB4110GFA", "USB4110-GF-A", maker="Global Connector Technology",
            cmaker="GCT", match="alias", offers=TRAY)
    assert c["label"] == pm.FORMATTING
    assert c["manufacturer_override"] is True
    assert any("known short and long form" in d for d in c["uncertainty"])
    s = cls("ABC123", "ABC123-TR", match="alias", cmaker="ACM")
    assert s["label"] == pm.POSSIBLE             # a packaging match is capped


@pytest.mark.parametrize("a,b,expected", [
    ("GCT", "Global Connector Technology", True),
    ("Global Connector Technology, Inc.", "gct", True),
    ("TI", "Texas Instruments Inc", True),
    ("TI", "Analog Devices", False),
    ("Acme", "Acme", False),                      # identical is not an alias
    ("", "GCT", False),
])
def test_maker_aliases(a, b, expected):
    assert pm.maker_alias(a, b) is expected
