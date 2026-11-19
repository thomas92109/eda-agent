# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Manufacturer-verified MPN aliases, end to end, synthetic data.

The KEMET numbers here are the ones KEMET's C1003_C0G datasheet documents
(TU, 7411, 7210). KEMET 7867 is deliberately absent from the registry: no
document read for it lists that code, so it must NOT be treated as an alias.
Every distributor here is a made-up one; nothing is ordered or written.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from eda_agent.export import purchasing_aliases as pa
from eda_agent.export import purchasing_cart as cart
from eda_agent.export import purchasing_config as pc
from eda_agent.export import purchasing_decisions as pd
from eda_agent.export import purchasing_matching as pm
from eda_agent.export import purchasing_sources
from eda_agent.export.purchasing_lines import build_purchase_lines
from eda_agent.export.purchasing_model import PurchasingStatus as S
from eda_agent.export.purchasing_sources import enrich_bom
from tests.test_purchasing_cart import FakeLists
from tests.test_purchasing_close_matches import (
    _Listing,
    commit,
    export,
    prepare,
    read,
)
from tests.test_purchasing_mpn_decisions import decide
from tests.test_purchasing_workflow import _altium, _hit, _offer, _part

BOM_MPN = "C0603C439C5GAC7411"        # 13 inch reel, per KEMET's table
SOLD_MPN = "C0603C439C5GACTU"         # 7 inch reel, what DigiKey lists
DESC = "CAP CER 4.3PF 50V NPO 0603"
KEMET_BOM = [{"manufacturer": "KEMET", "part_number": BOM_MPN,
              "description": DESC, "quantity": 2,
              "designators": ["C1", "C2"]}]
KEMET_BOARD = [_part("C1", "KEMET", BOM_MPN, DESC),
               _part("C2", "KEMET", BOM_MPN, DESC)]
PAIR = {"original_mpn": BOM_MPN, "candidate_mpn": SOLD_MPN}
AUTO = "verified manufacturer alias (automatic)"
CONFIG_ON = "[matching]\nauto_approve_verified_aliases = true\n"


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    out = tmp_path / "exports"
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(out))
    return out


def write_config(folder, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.toml").write_text(text, encoding="utf-8")


def listed(provider, mpn, maker, offers, stock=4363, description=DESC):
    hit = _hit(provider, mpn, maker, offers, stock=stock)
    hit.description = description
    return hit


def kemet_shops(monkeypatch, maker="KEMET", mouser_exact_under_yageo=True):
    """DigiKey sells the 7 inch reel number with its real terms: a reel with
    a 4000 minimum and cut tape. Mouser lists the BOM's own number under
    YAGEO with nothing to buy, and the 7 inch number under KEMET."""
    dk = _Listing("digikey", [listed("digikey", SOLD_MPN, maker, [
        _offer("399-TU-TR-ND", [(4000, 0.14521), (12000, 0.1335)],
               stock=4000, moq=4000, packaging=("Tape & Reel (TR)",),
               standard_package=4000),
        _offer("399-TU-CT-ND", [(1, 0.56), (10, 0.34), (50, 0.2526),
                                (100, 0.2256)], stock=4363)])])
    mouser = [listed("mouser", SOLD_MPN, maker, [
        _offer("80-C0603C439C5G", [(1, 0.56), (10, 0.34), (100, 0.219)],
               stock=7935, multiple=1, standard_package=4000,
               packaging=("Reel", "Cut Tape", "MouseReel"))], stock=7935)]
    if mouser_exact_under_yageo:
        mouser.append(listed("mouser", BOM_MPN, "YAGEO", [], stock=0))
    mo = _Listing("mouser", mouser)
    monkeypatch.setattr(purchasing_sources, "_providers", lambda: {
        "digikey": (dk, purchasing_sources._digikey_hits),
        "mouser": (mo, purchasing_sources._mouser_hits)})
    return dk, mo


def analyse(bom=KEMET_BOM, **kw):
    found = enrich_bom(bom, 2, find_close_matches=True, **kw)
    return {"build_quantity": 2, "bom_complete": True, "unresolved": [],
            "inconsistencies": [], **found}


def built(analysis, **args):
    return build_purchase_lines(analysis, pc.resolve_settings(args or None))


def only(result):
    assert len(result["lines"]) == 1
    return result["lines"][0]


def codes(pl):
    return {r.code for r in pl.reasons}


def auto():
    return pd.Authorizer(auto_approve_aliases=True)


# ---- the registry only says what a manufacturer document says -------------------------------

def test_every_registry_entry_carries_complete_evidence():
    assert pa.registry_problems() == []
    for code, entry in pa.KEMET_COMMERCIAL_C0G.codes.items():
        assert code in entry.evidence.quote
        assert entry.evidence.url.startswith("https://")
        assert entry.evidence.revision and entry.evidence.location


def test_an_entry_without_evidence_is_refused():
    thin = pa.ExplicitAlias(
        "x", frozenset({"acme"}), ("A-1", "A-2"), "same part",
        pa.Evidence("", "", "", "", ""))
    assert any("incomplete evidence" in p
               for p in pa.registry_problems((), (thin,)))
    one = pa.ExplicitAlias(
        "y", frozenset({"acme"}), ("A-1",), "same part",
        pa.Evidence("d", "r", "u", "l", "q"))
    assert any("fewer than two" in p for p in pa.registry_problems((), (one,)))


@pytest.mark.parametrize("a, b", [
    ("C0603C439C5GAC7411", "C0603C439C5GACTU"),
    ("C0603C439C5GACTU", "C0603C439C5GAC7411"),
    ("C0805C104J5GAC7210", "C0805C104J5GACTU"),
    ("c0603c439c5gactu ", "C0603C439C5GAC7411"),
])
def test_documented_kemet_codes_are_aliases_both_ways(a, b):
    found = pa.find_alias("KEMET", a, b)
    assert found is not None and found.method == (
        "manufacturer ordering-code rule")
    assert len(found.evidence) == 3
    assert "C-Spec" in found.evidence[0].quote or "Packaging" in \
        found.evidence[0].quote


@pytest.mark.parametrize("a, b, why", [
    ("C0603C439C5GAC7867", "C0603C439C5GACTU", "7867 is not documented"),
    ("C0603C439C5GACTU", "C0603C439C5GAC7210", "7210 is for 0805 and larger"),
    ("C0805C104J5GAC7411", "C0805C104J5GACTU", "7411 is for 0603 and smaller"),
    ("C0603C439C5GACAUTO7411", "C0603C439C5GACTU", "automotive is a grade"),
    ("C0603C439C5GACTU", "C0603C569C5GAC7411", "another capacitance"),
    ("C0603C439C5GACTU", "C0402C439C5GAC7411", "another case size"),
    ("C0603C439C5GACTU", "C0603C439C5GACTU", "the same number"),
    ("C0603C439C5GAC", "C0603C439C5GACTU", "blank is not in the table"),
    ("ABC123", "ABC123-TR", "a generic suffix is not an alias"),
])
def test_similar_numbers_are_not_aliases(a, b, why):
    assert pa.find_alias("KEMET", a, b) is None, why


def test_the_rule_is_bound_to_its_manufacturer():
    assert pa.find_alias("Murata", BOM_MPN, SOLD_MPN) is None
    assert pa.find_alias("YAGEO", BOM_MPN, SOLD_MPN) is None
    assert pa.find_alias("KEMET Electronics Corp", BOM_MPN, SOLD_MPN)


def test_alias_numbers_are_derived_only_from_documented_codes():
    assert [a.alias_mpn for a in pa.alias_numbers("KEMET", BOM_MPN)] \
        == [SOLD_MPN]
    assert [a.alias_mpn for a in pa.alias_numbers(
        "KEMET", "C0805C104J5GACTU")] == ["C0805C104J5GAC7210"]
    assert pa.alias_numbers("KEMET", "C0603C439C5GAC7867") == []
    assert pa.alias_numbers("Murata", BOM_MPN) == []


def test_a_pair_is_registered_only_when_documented():
    assert pa.is_registered_pair(BOM_MPN, SOLD_MPN)
    assert not pa.is_registered_pair("C0603C439C5GAC7867", SOLD_MPN)
    parsed = pm.parse_approvals([PAIR])
    assert pm.approval_key(BOM_MPN, SOLD_MPN) in parsed
    with pytest.raises(pm.MatchInputError):
        pm.parse_approvals([{"original_mpn": "C0603C439C5GAC7867",
                             "candidate_mpn": SOLD_MPN}])


# ---- how a candidate is classified ----------------------------------------------------------

def classify(maker="KEMET", match="exact", **kw):
    return pm.classify(
        bom_manufacturer="KEMET", bom_mpn=kw.get("bom", BOM_MPN),
        bom_description=DESC, candidate_mpn=kw.get("cand", SOLD_MPN),
        candidate_manufacturer=maker, candidate_description=DESC,
        manufacturer_match=match, offers=[], distributor="digikey")


def test_a_verified_alias_is_labelled_and_carries_its_evidence():
    c = classify()
    assert c["label"] == pm.VERIFIED_ALIAS and c["kind"] == "alias"
    assert pm.LABELS[0] == pm.VERIFIED_ALIAS
    assert c["alias"]["verified"] is True
    assert c["alias"]["rule"] == "kemet-commercial-c0g-cspec"
    text = " ".join(c["evidence"])
    assert "7\" Reel/Unmarked TU" in text and "13\" Reel/Unmarked 7411" in text
    assert "C1003_C0G 2/20/2025" in text
    assert c["difference"] == "ordering code 7411 -> TU"
    assert not c["approved"]


def test_an_alias_listed_under_another_manufacturer_is_only_uncertain():
    c = classify(maker="Zeta Corp", match="different")
    assert c["label"] == pm.UNCERTAIN
    assert any("another manufacturer" in d for d in c["uncertainty"])


def test_an_undocumented_difference_is_no_alias_candidate_at_all():
    assert classify(bom="C0603C439C5GAC7867") is None


# ---- through the pipeline: automatic acceptance off ------------------------------------------------

def test_off_by_default_the_alias_is_listed_for_a_manual_approval(monkeypatch):
    kemet_shops(monkeypatch)
    analysis = analyse()
    entry = analysis["lines"][0]
    assert [(c["distributor"], c["candidate_mpn"], c["label"])
            for c in entry["close_matches"]] == [
        ("digikey", SOLD_MPN, pm.VERIFIED_ALIAS),
        ("mouser", SOLD_MPN, pm.VERIFIED_ALIAS)]
    assert not any(c["approved"] for c in entry["close_matches"])
    assert {d["status"] for d in entry["distributors"].values()} \
        == {"not_found", "manufacturer_mismatch"}
    pl = only(built(analysis))
    assert pl.status is S.BLOCKED and pl.offer is None
    assert {"CLOSE_MATCH_AVAILABLE"} <= codes(pl)
    assert "VERIFIED_MPN_ALIAS" not in codes(pl)


def test_a_manual_project_approval_applies_with_the_option_off(monkeypatch):
    kemet_shops(monkeypatch)
    first = analyse()
    entry, cand = first["lines"][0], first["lines"][0]["close_matches"][0]
    approved = pd.make_decision("approved", entry, cand, "checked KEMET")
    pl = only(built(analyse(authorizer=pd.Authorizer([approved]))))
    assert pl.matched_mpn == SOLD_MPN and pl.line.part_number == BOM_MPN
    assert pl.mpn_variant["approval"]["method"] == "project approval"
    assert "VERIFIED_MPN_ALIAS" in codes(pl)


def test_an_approval_for_the_call_applies_the_alias_once(monkeypatch):
    kemet_shops(monkeypatch)
    approvals = pm.parse_approvals([PAIR])
    pl = only(built(analyse(approved_variants=approvals)))
    assert pl.matched_mpn == SOLD_MPN
    assert pl.mpn_variant["approval"]["method"] == "this call only"


# ---- automatic acceptance on ----------------------------------------------------------------------------

def test_on_a_verified_alias_is_accepted_without_asking(monkeypatch):
    dk, mo = kemet_shops(monkeypatch)
    analysis = analyse(authorizer=auto())
    result = built(analysis)
    pl = only(result)
    assert pl.line.part_number == BOM_MPN            # the schematic's number
    assert pl.matched_mpn == SOLD_MPN
    v = pl.mpn_variant
    assert v["approval"]["method"] == AUTO
    assert "auto_approve_verified_aliases" in v["approval"]["source"]
    assert v["alias"]["rule"] == "kemet-commercial-c0g-cspec"
    assert any("13\" Reel/Unmarked 7411" in e for e in v["alias"]["evidence"])
    assert "VERIFIED_MPN_ALIAS" in codes(pl)
    assert not codes(pl) & {"MPN_NOT_FOUND", "MANUFACTURER_MISMATCH",
                            "CLOSE_MATCH_AVAILABLE"}
    assert pl.status is S.READY       # information never changes the status
    cand = result["close_matches"][0]
    assert cand["approved"] and cand["used"]


def test_the_actual_sku_minimum_packaging_stock_and_prices_are_used(
        monkeypatch):
    kemet_shops(monkeypatch)
    pl = only(built(analyse(authorizer=auto()), prefer="digikey"))
    assert pl.offer.distributor == "digikey"
    # not the 4000 reel (minimum 4000): the cut tape, as the catalogue lists
    assert pl.offer.sku == "399-TU-CT-ND"
    assert pl.offer.packaging == ("Cut Tape (CT)",) and pl.offer.stock == 4363
    d = pl.decision
    assert pl.required_qty == 4 and d.purchase_qty >= 4
    ladder = {1: Decimal("0.56"), 10: Decimal("0.34"), 50: Decimal("0.2526"),
              100: Decimal("0.2256")}
    assert d.unit_price == ladder[d.break_quantity]
    assert d.extended_cost == d.unit_price * d.purchase_qty


def test_the_alias_is_also_found_where_the_boms_number_is_under_another_maker(
        monkeypatch):
    kemet_shops(monkeypatch)
    entry = analyse(authorizer=auto())["lines"][0]
    mouser = entry["distributors"]["mouser"]
    assert mouser["status"] == "matched" and mouser["mpn"] == SOLD_MPN
    assert mouser["mpn_variant"]["alias"]["verified"] is True
    assert entry["distributors"]["digikey"]["status"] == "matched"


def test_a_resolved_alias_is_not_mpn_not_found_even_when_it_cannot_be_bought(
        monkeypatch):
    dk, mo = kemet_shops(monkeypatch)
    for hit in dk.hits + mo.hits:
        for offer in hit.sourcing["offers"]:
            offer["stock"] = 0
    pl = only(built(analyse(authorizer=auto())))
    assert pl.status is S.BLOCKED
    assert "OUT_OF_STOCK" in codes(pl)
    assert not codes(pl) & {"MPN_NOT_FOUND", "MANUFACTURER_MISMATCH"}


def test_the_option_never_approves_what_is_not_a_verified_alias(monkeypatch):
    dk, mo = kemet_shops(monkeypatch)
    dk.hits.append(listed("digikey", "ABC123-TR", "Acme", [
        _offer("ABC-TR-ND", [(1, 0.1)])], description="IC WIDGET"))
    bom = [{"manufacturer": "Acme", "part_number": "ABC123",
            "description": "IC WIDGET", "quantity": 1, "designators": ["U1"]}]
    entry = analyse(bom, authorizer=auto())["lines"][0]
    assert entry["close_matches"][0]["label"] == pm.LIKELY
    assert not entry["close_matches"][0]["approved"]
    assert all(d["status"] == "not_found"
               for d in entry["distributors"].values())


# ---- unverified suffix differences ------------------------------------------------------------------------

def test_kemet_7867_is_not_accepted_without_documented_evidence(monkeypatch):
    kemet_shops(monkeypatch)
    bom = [{**KEMET_BOM[0], "part_number": "C0603C439C5GAC7867"}]
    entry = analyse(bom, authorizer=auto())["lines"][0]
    assert entry["close_matches"] == []
    pl = only(built(analyse(bom, authorizer=auto())))
    assert pl.status is S.BLOCKED and pl.offer is None
    assert "MPN_NOT_FOUND" in codes(pl)
    assert pl.matched_mpn == ""


def test_a_registry_entry_makes_a_new_documented_code_work(monkeypatch):
    """Adding 7867 needs only evidence from a KEMET document: no code change."""
    kemet_shops(monkeypatch)
    quote = ("Standard Reeling, Commercial Grade, 7\" reel, paper tape, "
             "0201, 0402 and 0603: 7867")
    code = pa.OrderingCode(
        "7867", "7 inch reel, paper tape (0603 and smaller)",
        pa._GRADE, pa._SMALL,
        pa.Evidence("TEST DOCUMENT", "test", "https://example.test/doc",
                    "test table", quote))
    rule = pa.FamilyRule(
        **{**pa.KEMET_COMMERCIAL_C0G.__dict__,
           "codes": {**pa.KEMET_COMMERCIAL_C0G.codes, "7867": code}})
    monkeypatch.setattr(pa, "RULES", (rule,))
    assert pa.registry_problems() == []
    found = pa.find_alias("KEMET", "C0603C439C5GAC7867", SOLD_MPN)
    assert found is not None
    bom = [{**KEMET_BOM[0], "part_number": "C0603C439C5GAC7867"}]
    pl = only(built(analyse(bom, authorizer=auto())))
    assert pl.matched_mpn == SOLD_MPN and "VERIFIED_MPN_ALIAS" in codes(pl)


# ---- conflicting manufacturers --------------------------------------------------------------------------

def test_a_conflicting_manufacturer_is_never_accepted_automatically(
        monkeypatch):
    kemet_shops(monkeypatch, maker="Zeta Corp", mouser_exact_under_yageo=False)
    entry = analyse(authorizer=auto())["lines"][0]
    cands = entry["close_matches"]
    assert cands and {c["label"] for c in cands} == {pm.UNCERTAIN}
    assert not any(c["approved"] for c in cands)
    pl = only(built(analyse(authorizer=auto())))
    assert pl.status is S.BLOCKED and pl.offer is None


def test_an_alias_of_another_makers_part_is_not_even_looked_for(monkeypatch):
    dk, mo = kemet_shops(monkeypatch)
    bom = [{**KEMET_BOM[0], "manufacturer": "Murata Electronics"}]
    entry = analyse(bom, authorizer=auto())["lines"][0]
    assert entry["close_matches"] == []
    assert SOLD_MPN not in dk.asked and SOLD_MPN not in mo.asked


def test_a_partial_manufacturer_name_needs_a_person(monkeypatch):
    kemet_shops(monkeypatch, maker="KEMET Electronics Corporation",
                mouser_exact_under_yageo=False)
    entry = analyse(authorizer=auto())["lines"][0]
    cands = entry["close_matches"]
    assert {c["label"] for c in cands} == {pm.VERIFIED_ALIAS}
    assert {c["manufacturer_match"] for c in cands} <= {"partial", "exact"}
    for cand in cands:
        if cand["manufacturer_match"] == "partial":
            assert not cand["approved"]


# ---- multiple candidates --------------------------------------------------------------------------------

def acme_alias(monkeypatch, numbers):
    item = pa.ExplicitAlias(
        "acme-widget", frozenset({"acme"}), numbers, "the same widget",
        pa.Evidence("Acme ordering guide", "rev B", "https://example.test/g",
                    "table 3", "ACM-100 is also sold as ZQ-7 and ZQ-8"))
    monkeypatch.setattr(pa, "EXPLICIT_ALIASES", (item,))


def widget_shops(monkeypatch, *aliases):
    dk = _Listing("digikey", [listed("digikey", n, "Acme", [
        _offer(f"{n}-ND", [(1, 0.2)])], description="IC WIDGET")
        for n in aliases])
    mo = _Listing("mouser", [])
    monkeypatch.setattr(purchasing_sources, "_providers", lambda: {
        "digikey": (dk, purchasing_sources._digikey_hits),
        "mouser": (mo, purchasing_sources._mouser_hits)})
    return dk, mo


WIDGET = [{"manufacturer": "Acme", "part_number": "ACM-100",
           "description": "IC WIDGET", "quantity": 1, "designators": ["U1"]}]


def test_an_explicit_alias_is_accepted_when_it_is_the_only_one(monkeypatch):
    acme_alias(monkeypatch, ("ACM-100", "ZQ-7", "ZQ-8"))
    widget_shops(monkeypatch, "ZQ-7")
    pl = only(built(analyse(WIDGET, authorizer=auto())))
    assert pl.matched_mpn == "ZQ-7"
    assert pl.mpn_variant["alias"]["method"] == "manufacturer alias"
    assert pl.mpn_variant["approval"]["method"] == AUTO


def test_two_verified_aliases_at_one_distributor_need_a_person(monkeypatch):
    acme_alias(monkeypatch, ("ACM-100", "ZQ-7", "ZQ-8"))
    widget_shops(monkeypatch, "ZQ-7", "ZQ-8")
    analysis = analyse(WIDGET, authorizer=auto())
    cands = analysis["lines"][0]["close_matches"]
    assert {c["candidate_mpn"] for c in cands} == {"ZQ-7", "ZQ-8"}
    assert all(c["alias_ambiguous"] and not c["approved"] for c in cands)
    assert all(any("none is accepted automatically" in u
                   for u in c["uncertainty"]) for c in cands)
    pl = only(built(analysis))
    assert pl.offer is None and "CLOSE_MATCH_AVAILABLE" in codes(pl)
    # the person picks one
    chosen = pm.parse_approvals([{"original_mpn": "ACM-100",
                                  "candidate_mpn": "ZQ-8"}])
    pl = only(built(analyse(WIDGET, authorizer=pd.Authorizer(
        call_approvals=chosen, auto_approve_aliases=True))))
    assert pl.matched_mpn == "ZQ-8"
    assert pl.mpn_variant["approval"]["method"] == "this call only"


# ---- a project's rejection beats the option ----------------------------------------------------------------

def test_a_project_rejection_overrides_automatic_acceptance(monkeypatch):
    kemet_shops(monkeypatch)
    first = analyse()
    entry, cand = first["lines"][0], first["lines"][0]["close_matches"][0]
    rejected = pd.make_decision("rejected", entry, cand, "wrong reel")
    second = analyse(authorizer=pd.Authorizer(
        [rejected], auto_approve_aliases=True))
    cands = second["lines"][0]["close_matches"]
    assert cands and all(c.get("rejected") and not c["approved"]
                         for c in cands)
    pl = only(built(second))
    assert pl.offer is None and "CLOSE_MATCH_REJECTED" in codes(pl)
    assert pl.matched_mpn == ""


def test_a_rejection_in_one_project_does_not_reach_another(monkeypatch,
                                                           folder, tmp_path):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    a = str(tmp_path / "WorkA" / "Board.PrjPcb")
    b = str(tmp_path / "WorkB" / "Board.PrjPcb")
    cols = ["part_number", "matched_mpn", "mpn_approval", "purchasing_status"]
    assert decide(action="reject", pairs=[PAIR], project_path=a)["ok"]
    out_a = export(project_path=a, columns=cols)
    out_b = export(project_path=b, columns=cols)
    row_a, row_b = read(out_a["output_path"])[0], read(out_b["output_path"])[0]
    assert row_a["Matched MPN"] == "" and row_a["Purchasing Status"] == "blocked"
    assert row_b["Matched MPN"] == SOLD_MPN
    assert row_b["MPN Approval"] == AUTO


# ---- the config option ---------------------------------------------------------------------------------------

def test_the_option_defaults_to_false_when_absent(folder):
    write_config(folder, "[assemblies]\ncount = 4\n")
    s = pc.resolve_settings()
    assert s.matching_auto_approve_verified_aliases is False
    assert s.report()["auto_approve_verified_aliases"] == {
        "value": False, "source": "default"}


def test_the_option_is_read_from_the_file(folder):
    write_config(folder, CONFIG_ON)
    s = pc.resolve_settings()
    assert s.matching_auto_approve_verified_aliases is True
    assert s.warnings == ()
    assert s.report()["auto_approve_verified_aliases"] == {
        "value": True, "source": "config"}


def test_it_is_independent_of_the_other_global_rule(folder):
    write_config(folder, '[matching]\nauto_approve = ["likely"]\n')
    s = pc.resolve_settings()
    assert s.matching_auto_approve == ("likely",)
    assert s.matching_auto_approve_verified_aliases is False
    write_config(folder, CONFIG_ON)
    s = pc.resolve_settings()
    assert s.matching_auto_approve == ()
    assert s.matching_auto_approve_verified_aliases is True


@pytest.mark.parametrize("bad", ['"yes"', "1", "[true]"])
def test_a_value_that_is_not_true_or_false_means_false_and_warns(folder, bad):
    write_config(folder, f"[matching]\nauto_approve_verified_aliases = {bad}\n")
    s = pc.resolve_settings()
    assert s.matching_auto_approve_verified_aliases is False
    assert any("must be true or false" in w for w in s.warnings)


def test_the_template_documents_the_option_off(folder):
    assert "auto_approve_verified_aliases = false" in pc.TEMPLATE
    s = pc.resolve_settings()           # creates the file from the template
    assert s.config_created and s.warnings == ()
    assert s.matching_auto_approve_verified_aliases is False


def test_an_existing_config_is_never_rewritten(folder):
    text = "[assemblies]\ncount = 4\n"
    write_config(folder, text)
    s = pc.resolve_settings()
    assert s.config_created is False
    assert (folder / "config.toml").read_text(encoding="utf-8") == text


# ---- the Purchasing BOM records every applied alias --------------------------------------------------------

OLD_COLUMNS = ["required_qty", "purchase_qty", "part_number", "manufacturer",
               "description", "reference_designators", "distributor",
               "distributor_sku", "unit_price", "extended_cost", "stock",
               "lifecycle", "purchasing_status", "review_reasons",
               "quantity_rule"]


def test_an_older_column_selection_still_shows_the_discrepancy(
        monkeypatch, folder):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    out = export(columns=OLD_COLUMNS)
    assert out["ok"], out
    row = read(out["output_path"])[0]
    assert row["Part Number"] == BOM_MPN
    assert f"VERIFIED_MPN_ALIAS (BOM {BOM_MPN} -> ordered {SOLD_MPN})" \
        in row["Review Reasons"]
    assert row["Purchasing Status"] == "ready"
    text = " ".join(out["warnings"])
    assert "VERIFIED MANUFACTURER ALIAS" in text
    assert f"{BOM_MPN} -> {SOLD_MPN}" in text and AUTO in text
    assert "lacks the column(s) matched_mpn, mpn_approval, mpn_audit" in text


def test_the_audit_holds_original_matched_method_source_and_evidence(
        monkeypatch, folder):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    out = export(columns=["part_number", "matched_mpn", "mpn_approval",
                          "mpn_audit", "mpn_method", "mpn_source",
                          "mpn_evidence", "distributor_sku", "unit_price",
                          "stock", "review_detail"])
    assert out["ok"], out
    row = read(out["output_path"])[0]
    assert row["Matched MPN"] == SOLD_MPN and row["MPN Approval"] == AUTO
    audit = row["MPN Approval Audit"]
    for part in (f"original {BOM_MPN} (KEMET)", f"matched {SOLD_MPN} (KEMET)",
                 "Verified Alias", "matching method: manufacturer "
                 "ordering-code rule kemet-commercial-c0g-cspec",
                 f"approval source: {AUTO}: config.toml [matching] "
                 "auto_approve_verified_aliases = true",
                 "evidence: ", "C1003_C0G 2/20/2025"):
        assert part in audit, part
    assert "kemet-commercial-c0g-cspec" in row["MPN Match Method"]
    assert "auto_approve_verified_aliases" in row["MPN Approval Source"]
    assert "7\" Reel/Unmarked TU" in row["MPN Evidence"]
    assert row["Distributor SKU"] == "399-TU-CT-ND" and row["Unit Price"]
    assert "VERIFIED_MPN_ALIAS" in row["Review Detail"]
    assert "manufacturer ordering-code rule" in row["Review Detail"]
    assert "does not appear" not in audit
    companion = read(out["close_matches_file"])
    assert companion and companion[0]["Candidate MPN"] == SOLD_MPN
    assert companion[0]["Qualifier"] == pm.VERIFIED_ALIAS
    assert "kemet-commercial-c0g-cspec" in companion[0]["Matching Method"]
    assert "7411" in companion[0]["Manufacturer Evidence"]


def test_without_the_option_the_export_lists_the_alias_for_approval(
        monkeypatch, folder):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    out = export(columns=OLD_COLUMNS)
    row = read(out["output_path"])[0]
    assert row["Purchasing Status"] == "blocked"
    assert "VERIFIED_MPN_ALIAS" not in row["Review Reasons"]
    assert "CLOSE_MATCH_AVAILABLE" in row["Review Reasons"]
    assert out["close_match_counts"] == {pm.VERIFIED_ALIAS: 2}
    assert not any("VERIFIED MANUFACTURER ALIAS" in w for w in out["warnings"])


def test_a_row_without_an_alias_reads_exactly_as_before(monkeypatch, folder):
    from tests.test_purchasing_close_matches import BOARD, distributors
    distributors(monkeypatch)
    _altium(monkeypatch, board=BOARD)
    out = export(columns=["part_number", "review_reasons"],
                 approve_mpn_variants=[{"original_mpn": "ABC123",
                                        "candidate_mpn": "ABC123-TR"}])
    rows = {r["Part Number"]: r for r in read(out["output_path"])}
    assert "VERIFIED_MPN_ALIAS" not in rows["ABC123"]["Review Reasons"]
    assert "APPROVED_MPN_VARIANT" in rows["ABC123"]["Review Reasons"]
    assert "(" not in rows["ABC123"]["Review Reasons"]
    assert not any("VERIFIED MANUFACTURER ALIAS" in w for w in out["warnings"])


# ---- carts and lists carry the same decision ---------------------------------------------------------------

def test_the_proposal_carries_the_alias_and_the_distributors_number(
        monkeypatch, folder):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    out = prepare()
    assert out["ok"], out
    item = out["proposal"]["items"][0]
    assert item["bom_mpn"] == BOM_MPN and item["ordered_mpn"] == SOLD_MPN
    assert item["manufacturer_part_number"] == SOLD_MPN
    assert item["sku"] == "399-TU-CT-ND"
    assert item["mpn_alias"] == {
        "rule": "kemet-commercial-c0g-cspec",
        "method": "manufacturer ordering-code rule",
        "approval_method": AUTO,
        "approval_source": "config.toml [matching] "
                           "auto_approve_verified_aliases = true"}
    assert item["status"] == "ready" and not out["proposal"]["excluded"]


def test_the_mouser_proposal_asks_for_the_number_ordered(monkeypatch, folder):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    out = prepare(distributor="mouser", list_id="", prefer="mouser",
                  cart_key="00000000-0000-0000-0000-000000000009")
    assert out["ok"], out
    item = out["proposal"]["items"][0]
    assert item["sku"] == "80-C0603C439C5G"
    assert item["manufacturer_part_number"] == SOLD_MPN
    assert item["mpn_alias"]["approval_method"] == AUTO


def test_the_fingerprint_covers_how_the_alias_was_matched_and_accepted():
    item = dict(sku="X-ND", quantity=4, packaging="Cut Tape (CT)",
                unit_price=Decimal("0.56"), currency="USD",
                lifecycle="active", designators=["C1", "C2"],
                ordered_mpn=SOLD_MPN, bom_mpn=BOM_MPN)
    target = cart.normalize_target("digikey", list_id="L")
    fp = lambda i: cart.fingerprint("digikey", target, [i])  # noqa: E731
    automatic = {"rule": "r", "method": "m", "approval_method": AUTO,
                 "approval_source": "config.toml"}
    manual = {**automatic, "approval_method": "project approval",
              "approval_source": ""}
    other_rule = {**automatic, "rule": "r2"}
    assert fp({**item, "mpn_alias": None}) == fp(item)   # unchanged for all else
    ids = {fp(item), fp({**item, "mpn_alias": automatic}),
           fp({**item, "mpn_alias": manual}),
           fp({**item, "mpn_alias": other_rule})}
    assert len(ids) == 4


def test_switching_the_option_is_a_new_proposal(monkeypatch, folder):
    kemet_shops(monkeypatch)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    automatic = prepare()
    manual = prepare(approve_mpn_variants=None)
    assert automatic["proposal_id"] == manual["proposal_id"]
    write_config(folder, "[matching]\nauto_approve_verified_aliases = false\n")
    off = prepare()
    assert off["proposal"]["items"] == []
    assert off["proposal_id"] != automatic["proposal_id"]
    by_hand = prepare(approve_mpn_variants=[PAIR])
    assert by_hand["proposal"]["items"][0]["mpn_alias"]["approval_method"] \
        == "this call only"
    assert by_hand["proposal_id"] != automatic["proposal_id"]


def test_a_digikey_list_commit_uses_the_alias_and_the_journal_records_it(
        monkeypatch, folder):
    kemet_shops(monkeypatch)
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "z", "requested_part_number": "z",
                             "quantity": 1, "reference_designator": ""}]
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    _altium(monkeypatch, board=KEMET_BOARD)
    write_config(folder, CONFIG_ON)
    pid = prepare()["proposal_id"]
    done = commit(proposal_id=pid, approved=True)
    assert done["ok"] and done["verified"], done
    assert "399-TU-CT-ND" in {p["sku"] for p in fake.lists["list-1"]}
    journal = (folder / "purchasing" / "journal" / f"{pid}.json").read_text(
        encoding="utf-8")
    assert BOM_MPN in journal and SOLD_MPN in journal
    assert "kemet-commercial-c0g-cspec" in journal and AUTO in journal
    # asking again with the option turned off is a different proposal
    write_config(folder, "[matching]\nauto_approve_verified_aliases = false\n")
    refused = commit(proposal_id=pid, approved=True)
    assert refused["ok"] is False and refused["changed"] is True
