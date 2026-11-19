# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Close part number candidates through the whole workflow, synthetic data.

The BOM here has one part that matches exactly, one whose number differs
from the distributor's only by punctuation (and whose maker the distributor
abbreviates), and one that has three suffixed variants listed. Nothing is
used until the user approves an exact pair; the schematic's own number is
never changed.
"""

from __future__ import annotations

import asyncio
import csv
import io

import pytest

from eda_agent.export import purchasing_cart as cart
from eda_agent.export import purchasing_config as pc
from eda_agent.export import purchasing_matching as pm
from eda_agent.export import purchasing_sources
from eda_agent.export.purchasing_bom_export import (
    CLOSE_MATCH_HEADERS,
    build_close_matches_csv,
)
from eda_agent.export.purchasing_lines import build_purchase_lines
from eda_agent.export.purchasing_model import PurchasingStatus as S
from eda_agent.export.purchasing_sources import enrich_bom
from tests.test_purchasing_cart import FakeLists, cart_tool_functions
from tests.test_purchasing_workflow import (
    _Shop,
    _altium,
    _hit,
    _offer,
    _part,
    _tools,
)


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path / "exports"))
    return tmp_path / "exports"


class _Listing(_Shop):
    """A distributor that also answers a begins-with search and records the
    keyword searches it was given."""

    def __init__(self, name, hits=()):
        super().__init__(name, hits)
        self.queries: list[str] = []

    def search(self, query, limit=20):
        self.queries.append(query)
        return super().search(query, limit)

    def _post(self, url, request):
        asked = request["SearchByPartRequest"]
        if asked.get("partSearchOptions") == "BeginsWith":
            self.queries.append("begins:" + asked["mouserPartNumber"])
            return [h for h in self.hits if h.mpn.casefold().startswith(
                asked["mouserPartNumber"].casefold())]
        return self._answer(asked["mouserPartNumber"])


def listing(provider, mpn, maker, description, sku, packaging, stock=800,
            ladder=((1, 0.50), (100, 0.30))):
    hit = _hit(provider, mpn, maker, [_offer(
        sku, list(ladder), stock=stock, packaging=packaging)], stock=stock)
    hit.description = description
    return hit


def distributors(monkeypatch, with_mouser_variant=True):
    dk = _Listing("digikey", [
        listing("digikey", "ACM-R1", "Acme", "RES 10K", "ACM-R1-ND",
                ("Cut Tape (CT)",), 5000, ((1, 0.10),)),
        listing("digikey", "USB4110-GF-A", "GCT",
                "USB-C receptacle 24 position right angle", "GCT-USB-ND",
                ("Cut Tape (CT)",), 1200, ((1, 1.20), (100, 0.95))),
        listing("digikey", "ABC123-TR", "Acme", "IC WIDGET 3V TAPE REEL",
                "ABC123-TR-ND", ("Tape & Reel (TR)",)),
        listing("digikey", "ABC123A5", "Acme", "IC WIDGET 3V 5 PIN",
                "ABC123A5-ND", ("Tray",)),
        listing("digikey", "ABC123XYZ789", "Acme", "IC WIDGET 3V EXTENDED",
                "ABC123XYZ-ND", ("Tray",)),
    ])
    mouser_hits = [listing("mouser", "ACM-R1", "Acme", "RES 10K", "603-R1",
                           ("Reel",), 9000, ((1, 0.12),))]
    if with_mouser_variant:
        mouser_hits.append(listing(
            "mouser", "ABC123-TR", "Acme", "IC WIDGET 3V TAPE REEL",
            "603-ABC123-TR", ("Reel",), 600))
    mo = _Listing("mouser", mouser_hits)
    monkeypatch.setattr(purchasing_sources, "_providers", lambda: {
        "digikey": (dk, purchasing_sources._digikey_hits),
        "mouser": (mo, purchasing_sources._mouser_hits)})
    return dk, mo


BOM = [
    {"manufacturer": "Acme", "part_number": "ACM-R1", "description": "RES 10K",
     "quantity": 2, "designators": ["R1", "R2"]},
    {"manufacturer": "Global Connector Technology",
     "part_number": "USB4110GFA",
     "description": "USB-C receptacle 24 position", "quantity": 1,
     "designators": ["J8"]},
    {"manufacturer": "Acme", "part_number": "ABC123",
     "description": "IC WIDGET 3V", "quantity": 1, "designators": ["U9"]},
]
USB = pm.approval_key("USB4110GFA", "USB4110-GF-A")


def analysed(**kw):
    found = enrich_bom(BOM, 2, find_close_matches=True, **kw)
    return {"build_quantity": 2, "bom_complete": True, "unresolved": [],
            "inconsistencies": [], **found}


def lines_of(analysis, **args):
    return build_purchase_lines(analysis, pc.resolve_settings(args or None))


def by_number(result):
    return {pl.line.part_number: pl for pl in result["lines"]}


def codes(pl):
    return {r.code for r in pl.reasons}


# ---- finding candidates -------------------------------------------------------------------

def test_candidates_are_found_labelled_and_none_is_used(monkeypatch):
    dk, mo = distributors(monkeypatch)
    analysis = analysed()
    entries = {e["part_number"]: e for e in analysis["lines"]}
    assert entries["ACM-R1"]["close_matches"] == []         # exact: no search
    usb = entries["USB4110GFA"]["close_matches"]
    assert [(c["candidate_mpn"], c["label"]) for c in usb] == [
        ("USB4110-GF-A", pm.FORMATTING)]
    assert usb[0]["manufacturer"] == "GCT" and usb[0]["stock"] == 1200
    assert usb[0]["manufacturer_override"] is True
    assert not usb[0]["approved"]
    widget = entries["ABC123"]["close_matches"]
    assert [(c["distributor"], c["candidate_mpn"], c["label"])
            for c in widget] == [
        ("digikey", "ABC123-TR", pm.LIKELY),
        ("mouser", "ABC123-TR", pm.LIKELY),
        ("digikey", "ABC123A5", pm.POSSIBLE),
        ("digikey", "ABC123XYZ789", pm.UNCERTAIN)]
    # nothing was used: both lines stay unmatched
    for part in ("USB4110GFA", "ABC123"):
        assert {d["status"] for d in entries[part]["distributors"].values()} \
            == {"not_found"}
    result = lines_of(analysis)
    pls = by_number(result)
    for part in ("USB4110GFA", "ABC123"):
        assert pls[part].status is S.BLOCKED and pls[part].offer is None
        assert codes(pls[part]) >= {"MPN_NOT_FOUND", "CLOSE_MATCH_AVAILABLE"}
        assert pls[part].matched_mpn == ""
    assert pls["ACM-R1"].status is S.READY


def test_the_search_is_only_for_lines_with_no_exact_match(monkeypatch):
    dk, mo = distributors(monkeypatch)
    enrich_bom([BOM[0]], 2, find_close_matches=True)
    assert dk.queries == [] and mo.queries == []


def test_without_the_option_nothing_extra_is_searched_or_returned(monkeypatch):
    dk, mo = distributors(monkeypatch)
    found = enrich_bom(BOM, 2)
    assert "close_match_notes" not in found
    assert all("close_matches" not in line for line in found["lines"])
    assert "USB4110" not in dk.queries and not any(
        q.startswith("begins:") for q in mo.queries)


def test_the_punctuation_search_uses_the_short_prefix_and_mouser_begins_with(
        monkeypatch):
    dk, mo = distributors(monkeypatch)
    enrich_bom([BOM[1], BOM[2]], 2, find_close_matches=True)
    assert "USB4110" in dk.queries and "ABC123" in dk.queries
    assert "begins:ABC123" in mo.queries and "begins:USB4110GFA" in mo.queries


def test_a_candidate_with_another_makers_name_is_listed_as_uncertain(
        monkeypatch):
    dk, mo = distributors(monkeypatch)
    dk.hits.append(listing("digikey", "ZED900-TR", "Zeta Corp",
                           "IC ZED", "ZED-ND", ("Tape & Reel (TR)",)))
    entry = enrich_bom([{"manufacturer": "Acme", "part_number": "ZED900",
                         "description": "IC ZED", "quantity": 1,
                         "designators": ["U1"]}], 2,
                       find_close_matches=True)["lines"][0]
    cand = entry["close_matches"][0]
    assert cand["label"] == pm.UNCERTAIN
    assert any("names differ" in d for d in cand["uncertainty"])


# ---- approval (the only way a candidate is used) --------------------------------------------

def test_an_approved_pair_is_used_and_the_boms_number_is_kept(monkeypatch):
    distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "USB4110GFA", "candidate_mpn": "USB4110-GF-A"}])
    analysis = analysed(approved_variants=approvals)
    result = lines_of(analysis)
    pl = by_number(result)["USB4110GFA"]
    assert pl.line.part_number == "USB4110GFA"            # Altium's number
    assert pl.matched_mpn == "USB4110-GF-A"
    assert pl.offer is not None and pl.offer.sku == "GCT-USB-ND"
    assert pl.mpn_variant["label"] == pm.FORMATTING
    assert {"APPROVED_MPN_VARIANT", "MANUFACTURER_PARTIAL_MATCH"} <= codes(pl)
    assert pl.status is S.REVIEW        # the maker name differs: confirm it
    cand = result["close_matches"][0]
    assert cand["approved"] and cand["used"]
    # the other line was not approved, so it is still unused
    assert by_number(result)["ABC123"].offer is None


def test_approving_one_variant_does_not_approve_another(monkeypatch):
    distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "ABC123", "candidate_mpn": "ABC123A5"}])
    result = lines_of(analysed(approved_variants=approvals))
    pl = by_number(result)["ABC123"]
    assert pl.matched_mpn == "ABC123A5" and pl.offer.distributor == "digikey"
    states = {(c["distributor"], c["candidate_mpn"]):
              (c["approved"], c.get("used", False))
              for c in result["close_matches"] if c["original_mpn"] == "ABC123"}
    assert states[("digikey", "ABC123A5")] == (True, True)
    assert states[("digikey", "ABC123-TR")] == (False, False)
    assert states[("mouser", "ABC123-TR")] == (False, False)


def test_an_approved_packaging_variant_is_ordinary_but_marked(monkeypatch):
    distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "ABC123", "candidate_mpn": "ABC123-TR"}])
    pl = by_number(lines_of(analysed(approved_variants=approvals)))["ABC123"]
    assert pl.matched_mpn == "ABC123-TR" and pl.status is S.READY
    assert "APPROVED_MPN_VARIANT" in codes(pl)
    assert pl.decision.purchase_qty >= pl.required_qty == 2
    assert pl.offer.sku == "ABC123-TR-ND"


def test_an_approval_for_another_spelling_of_the_candidate_does_not_apply(
        monkeypatch):
    distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "USB4110GFA", "candidate_mpn": "USB4110GF-A"}])
    analysis = analysed(approved_variants=approvals)
    assert by_number(lines_of(analysis))["USB4110GFA"].offer is None
    assert any("matched no close candidate" in n
               for n in analysis["close_match_notes"])


def test_an_approval_for_an_exactly_matched_line_is_noted_and_unused(
        monkeypatch):
    distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "ACM-R1", "candidate_mpn": "ACM-R1-TR"}])
    analysis = analysed(approved_variants=approvals)
    assert any("found exactly" in n for n in analysis["close_match_notes"])
    assert by_number(lines_of(analysis))["ACM-R1"].matched_mpn == ""


def test_an_approved_number_the_distributor_will_not_read_is_not_used(
        monkeypatch):
    dk, mo = distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "ABC123", "candidate_mpn": "ABC123A5"}])
    dk.fail["ABC123A5"] = RuntimeError("boom")
    result = lines_of(analysed(approved_variants=approvals))
    pl = by_number(result)["ABC123"]
    assert pl.offer is None
    cand = next(c for c in result["close_matches"]
                if c["candidate_mpn"] == "ABC123A5")
    assert cand["approved"] and cand["used"] is False
    assert "could not be read" in cand["approval_note"]


# ---- the consolidated list and its file -------------------------------------------------------

def test_all_candidates_are_listed_together_most_likely_first(monkeypatch):
    distributors(monkeypatch)
    table = lines_of(analysed())["close_matches"]
    assert [c["label"] for c in table] == [
        pm.FORMATTING, pm.LIKELY, pm.LIKELY, pm.POSSIBLE, pm.UNCERTAIN]
    first = table[0]
    for key in ("original_mpn", "candidate_mpn", "manufacturer", "difference",
                "label", "meaning", "distributor", "stock", "unit_price",
                "currency", "evidence", "uncertainty", "line_id",
                "designators", "bom_manufacturer"):
        assert key in first, key
    assert first["line_id"] == "L002" and first["designators"] == ["J8"]
    assert [c["difference"] for c in table[1:]] == ["-TR", "-TR", "A5",
                                                    "XYZ789"]


def test_the_companion_csv_has_the_columns_asked_for_and_the_approval_state(
        monkeypatch):
    distributors(monkeypatch)
    approvals = pm.parse_approvals([
        {"original_mpn": "ABC123", "candidate_mpn": "ABC123A5"}])
    table = lines_of(analysed(approved_variants=approvals))["close_matches"]
    built = build_close_matches_csv(table)
    assert built["headers"] == list(CLOSE_MATCH_HEADERS)
    for needed in ("Original MPN", "Candidate MPN", "Manufacturer",
                   "Suffix Difference", "Qualifier", "Stock", "Unit Price",
                   "Evidence", "Uncertainty", "Approval"):
        assert needed in built["headers"]
    rows = list(csv.DictReader(io.StringIO(built["text"], newline="")))
    assert [r["Qualifier"] for r in rows] == [
        pm.FORMATTING, pm.LIKELY, pm.LIKELY, pm.POSSIBLE, pm.UNCERTAIN]
    approval = {r["Candidate MPN"]: r["Approval"] for r in rows}
    assert approval["ABC123A5"] == "approved and used"
    assert approval["USB4110-GF-A"] == "not decided: not used"
    assert built["text"].endswith("\r\n")


def test_the_companion_csv_neutralizes_formulas():
    row = {"original_mpn": "A1", "candidate_mpn": "=A1+B2", "label": pm.POSSIBLE,
           "difference": "+1", "manufacturer": "@x", "evidence": [],
           "uncertainty": [], "distributor": "digikey"}
    text = build_close_matches_csv([row])["text"]
    assert "'=A1+B2" in text and "'+1" in text and "'@x" in text


# ---- through the tools -----------------------------------------------------------------------------

BOARD = [
    _part("R1", "Acme", "ACM-R1", "RES 10K"),
    _part("R2", "Acme", "ACM-R1", "RES 10K"),
    _part("J8", "Global Connector Technology", "USB4110GFA",
          "USB-C receptacle 24 position"),
    _part("U9", "Acme", "ABC123", "IC WIDGET 3V"),
]


def export(**kw):
    kw.setdefault("project_path", "DemoBoard.PrjPcb")
    return asyncio.run(_tools()["part_purchasing_export_csv"](**kw))


def read(path):
    raw = open(path, "rb").read()
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def test_the_export_lists_candidates_writes_the_companion_and_uses_none(
        monkeypatch, folder):
    distributors(monkeypatch)
    altium = _altium(monkeypatch, board=BOARD)
    out = export(columns=["part_number", "matched_mpn", "mpn_match",
                          "purchasing_status", "review_reasons"])
    assert out["ok"], out
    assert out["close_match_counts"] == {
        pm.FORMATTING: 1, pm.LIKELY: 2, pm.POSSIBLE: 1, pm.UNCERTAIN: 1}
    assert [c["label"] for c in out["close_matches"]][0] == pm.FORMATTING
    assert any("NONE is used" in w for w in out["warnings"])
    rows = {r["Part Number"]: r for r in read(out["output_path"])}
    assert rows["USB4110GFA"]["Purchasing Status"] == "blocked"
    assert rows["USB4110GFA"]["Matched MPN"] == ""
    assert "CLOSE_MATCH_AVAILABLE" in rows["ABC123"]["Review Reasons"]
    companion = read(out["close_matches_file"])
    assert len(companion) == 5
    assert out["close_matches_file"].endswith("_close_matches.csv")
    assert out["close_matches_file"].startswith(str(folder))
    # the schematic was only read
    assert set(altium.sent) <= {
        "project.get_bom", "project.get_active_variant",
        "project.get_variants", "project.get_component_info_batch"}


def test_the_export_uses_an_approved_pair_and_shows_the_number_ordered(
        monkeypatch):
    distributors(monkeypatch)
    _altium(monkeypatch, board=BOARD)
    out = export(columns=["part_number", "matched_mpn", "mpn_match",
                          "distributor_sku", "purchasing_status"],
                 approve_mpn_variants=[
                     {"original_mpn": "USB4110GFA",
                      "candidate_mpn": "USB4110-GF-A"},
                     {"original_mpn": "ABC123",
                      "candidate_mpn": "ABC123-TR"}])
    assert out["ok"], out
    rows = {r["Part Number"]: r for r in read(out["output_path"])}
    usb = rows["USB4110GFA"]
    assert usb["Matched MPN"] == "USB4110-GF-A"
    assert usb["MPN Match"] == "approved variant (Formatting Match)"
    assert usb["Distributor SKU"] == "GCT-USB-ND"
    assert usb["Purchasing Status"] == "review"
    assert rows["ABC123"]["Purchasing Status"] == "ready"
    assert rows["ACM-R1"]["MPN Match"] == "exact"
    states = {(c["original_mpn"], c["candidate_mpn"]): c.get("used", False)
              for c in out["close_matches"]}
    assert states[("USB4110GFA", "USB4110-GF-A")] is True
    assert states[("ABC123", "ABC123A5")] is False


def test_a_file_without_the_matched_column_warns_when_variants_are_used(
        monkeypatch):
    distributors(monkeypatch)
    _altium(monkeypatch, board=BOARD)
    out = export(columns=["part_number", "purchase_qty"],
                 approve_mpn_variants=[{"original_mpn": "ABC123",
                                        "candidate_mpn": "ABC123-TR"}])
    assert any("lacks the column(s) matched_mpn, mpn_approval, mpn_audit" in w
               for w in out["warnings"])


def test_a_substitution_that_is_not_a_close_variant_is_refused_before_a_read(
        monkeypatch):
    dk, mo = distributors(monkeypatch)
    altium = _altium(monkeypatch, board=BOARD)
    for bad in ([{"original_mpn": "ABC123", "candidate_mpn": "XYZ999"}],
                [{"original_mpn": "ABC123"}], "ABC123=ABC123-TR"):
        out = export(approve_mpn_variants=bad)
        assert out["ok"] is False and out["reason"]
    assert altium.sent == [] and dk.asked == [] and mo.asked == []


def test_the_default_columns_show_the_matched_number():
    from eda_agent.export.purchasing_bom_export import DEFAULT_COLUMNS
    assert DEFAULT_COLUMNS.index("matched_mpn") \
        == DEFAULT_COLUMNS.index("part_number") + 1


# ---- the cart ------------------------------------------------------------------------------------------

def prepare(**kw):
    kw.setdefault("project_path", "DemoBoard.PrjPcb")
    kw.setdefault("distributor", "digikey")
    kw.setdefault("list_id", "list-1")
    return asyncio.run(cart_tool_functions()[
        "part_purchasing_cart_prepare"](**kw))


def commit(**kw):
    kw.setdefault("project_path", "DemoBoard.PrjPcb")
    kw.setdefault("distributor", "digikey")
    kw.setdefault("list_id", "list-1")
    return asyncio.run(cart_tool_functions()[
        "part_purchasing_cart_commit"](**kw))


APPROVE_TR = [{"original_mpn": "ABC123", "candidate_mpn": "ABC123-TR"}]


def test_an_unapproved_candidate_is_never_in_a_proposal(monkeypatch):
    distributors(monkeypatch)
    _altium(monkeypatch, board=BOARD)
    out = prepare()
    assert out["ok"], out
    assert [i["sku"] for i in out["proposal"]["items"]] == ["ACM-R1-ND"]
    assert len(out["close_matches"]) == 5
    assert any(e["part_number"] == "ABC123" for e in out["proposal"]["excluded"])


def test_an_approved_variant_enters_with_both_numbers_and_changes_the_id(
        monkeypatch):
    distributors(monkeypatch)
    _altium(monkeypatch, board=BOARD)
    plain = prepare()
    approved = prepare(approve_mpn_variants=APPROVE_TR)
    assert approved["ok"], approved
    assert approved["proposal_id"] != plain["proposal_id"]
    item = next(i for i in approved["proposal"]["items"]
                if i["bom_mpn"] == "ABC123")
    assert item["ordered_mpn"] == "ABC123-TR"
    assert item["manufacturer_part_number"] == "ABC123-TR"
    assert item["sku"] == "ABC123-TR-ND"
    assert item["mpn_variant"]["label"] == pm.LIKELY


def test_the_fingerprint_covers_the_number_ordered():
    from decimal import Decimal
    item = dict(sku="X-ND", quantity=5, packaging="Cut Tape",
                unit_price=Decimal("0.1"), currency="USD", lifecycle="active",
                designators=["U1"], ordered_mpn="ABC123", bom_mpn="ABC123")
    target = cart.normalize_target("digikey", list_id="L")
    one = cart.fingerprint("digikey", target, [item])
    two = cart.fingerprint("digikey", target,
                           [{**item, "ordered_mpn": "ABC123-TR"}])
    assert one != two


def test_commit_needs_the_same_approvals_as_prepare(monkeypatch):
    distributors(monkeypatch)
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "z", "requested_part_number": "z",
                             "quantity": 1, "reference_designator": ""}]
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    _altium(monkeypatch, board=BOARD)
    pid = prepare(approve_mpn_variants=APPROVE_TR)["proposal_id"]
    refused = commit(proposal_id=pid, approved=True)           # no approvals
    assert refused["ok"] is False and refused["changed"] is True
    assert fake.calls == []
    done = commit(proposal_id=pid, approved=True,
                  approve_mpn_variants=APPROVE_TR)
    assert done["ok"] and done["verified"], done
    assert {p["sku"] for p in fake.lists["list-1"]} >= {"ABC123-TR-ND",
                                                        "ACM-R1-ND"}


def test_bad_approvals_are_refused_by_the_cart_tools_before_any_read(
        monkeypatch):
    dk, mo = distributors(monkeypatch)
    altium = _altium(monkeypatch, board=BOARD)
    out = prepare(approve_mpn_variants=[{"original_mpn": "A", "candidate_mpn": "B"}])
    assert out["ok"] is False and out["problems"]
    assert altium.sent == [] and dk.asked == [] and mo.asked == []
