# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Project-scoped MPN decisions: no leakage, reuse, staleness, audit.

Two unrelated Altium projects, both called Board.PrjPcb and in different
folders, carry identical part numbers. A manual approval or rejection made in
one must never reach the other. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
from pathlib import Path

import pytest

from eda_agent.export import destination
from eda_agent.export import purchasing_cart as cart
from eda_agent.export import purchasing_config as pc
from eda_agent.export import purchasing_decisions as pd
from eda_agent.export import purchasing_matching as pm
from tests.test_purchasing_cart import FakeLists, FakeMouser, cart_tool_functions
from tests.test_purchasing_close_matches import (
    BOARD,
    commit,
    distributors,
    export,
    listing,
    prepare,
    read,
)
from tests.test_purchasing_workflow import _Altium, _altium, _part, _tools

KEY = "00000000-0000-0000-0000-000000000009"
READS = {"project.get_bom", "project.get_active_variant",
         "project.get_variants", "project.get_component_info_batch"}
TR = {"original_mpn": "ABC123", "candidate_mpn": "ABC123-TR"}
A5 = {"original_mpn": "ABC123", "candidate_mpn": "ABC123A5"}
XYZ = {"original_mpn": "ABC123", "candidate_mpn": "ABC123XYZ789"}
USB = {"original_mpn": "USB4110GFA", "candidate_mpn": "USB4110-GF-A"}
COLS = ["part_number", "matched_mpn", "mpn_approval", "mpn_audit",
        "purchasing_status", "review_reasons"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    out = tmp_path / "exports"
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(out))
    a = str(tmp_path / "WorkA" / "Board.PrjPcb")
    b = str(tmp_path / "WorkB" / "Board.PrjPcb")
    return {"out": out, "A": a, "B": b, "tmp": tmp_path}


def decide(**kw):
    return asyncio.run(_tools()["part_purchasing_mpn_decisions"](**kw))


def rows_of(project, **kw):
    out = export(project_path=project, columns=COLS, **kw)
    assert out["ok"], out
    return out, {r["Part Number"]: r for r in read(out["output_path"])}


def store_file(world, project):
    return (world["out"] / "purchasing" / "mpn_decisions"
            / f"{pd.scope_for(project).id}.json")


def setup(monkeypatch):
    dk, mo = distributors(monkeypatch)
    altium = _altium(monkeypatch, board=BOARD)
    return dk, mo, altium


# ---- the project's identity -------------------------------------------------------------

def test_the_identity_is_the_canonical_path_not_the_file_name(world):
    a, b = pd.scope_for(world["A"]), pd.scope_for(world["B"])
    assert a.name == b.name == "Board.PrjPcb"
    assert a.id != b.id and len(a.id) == 16
    same = os.path.join(world["tmp"], "WorkA", "x", "..", "Board.PrjPcb")
    assert pd.scope_for(same).id == a.id
    if sys.platform == "win32":
        loud = world["A"].upper().replace("\\", "/")
        assert pd.scope_for(loud).id == a.id
    assert pd.scope_for("Board.PrjPcb") is None          # a bare name names none
    assert pd.scope_for("") is None and pd.scope_for(None) is None


def test_the_store_is_under_the_export_folder_and_never_the_repository(world):
    scope = pd.scope_for(world["A"])
    store = pd.DecisionStore(scope, create=False)
    assert not (world["out"] / "purchasing").exists()       # a read made no folder
    assert str(store.path).startswith(str(world["out"]))
    assert destination.repo_root() not in store.path.parents


def test_a_store_in_the_repository_is_refused(monkeypatch, world):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR",
                       str(destination.repo_root() / "docs" / "x"))
    with pytest.raises(destination.ExportPathError):
        pd.DecisionStore(pd.scope_for(world["A"]), create=True)


def test_a_file_that_names_another_project_is_refused(world):
    a, b = pd.scope_for(world["A"]), pd.scope_for(world["B"])
    store = pd.DecisionStore(a, create=True)
    store.put({"id": "x", "decision": "approved", "original_manufacturer": "M",
               "original_mpn": "P", "candidate_mpn": "P-TR",
               "candidate_manufacturer": "M", "decided_at": "t"})
    store.save()
    target = pd.DecisionStore(b, create=True).path
    target.write_bytes(store.path.read_bytes())            # copied into B's name
    with pytest.raises(pd.DecisionError) as info:
        pd.DecisionStore(b)
    assert "does not belong to this project" in str(info.value)
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(pd.DecisionError):
        pd.DecisionStore(b)


# ---- no leakage between projects ---------------------------------------------------------------

def test_an_approval_made_in_one_project_never_reaches_another(world,
                                                               monkeypatch):
    dk, mo, altium = setup(monkeypatch)
    done = decide(action="approve", pairs=[TR], project_path=world["A"])
    assert done["ok"] and done["recorded"][0]["decision"] == "approved"
    assert done["project"]["id"] == pd.scope_for(world["A"]).id
    out_a, a = rows_of(world["A"])
    assert a["ABC123"]["Matched MPN"] == "ABC123-TR"
    assert a["ABC123"]["MPN Approval"] == "project approval"
    out_b, b = rows_of(world["B"])
    assert b["ABC123"]["Matched MPN"] == ""
    assert b["ABC123"]["Purchasing Status"] == "blocked"
    assert "CLOSE_MATCH_AVAILABLE" in b["ABC123"]["Review Reasons"]
    assert "ABC123-TR" in {c["candidate_mpn"] for c in out_b["close_matches"]}
    assert all(not c["approved"] for c in out_b["close_matches"])
    assert store_file(world, world["A"]).is_file()
    assert not store_file(world, world["B"]).exists()
    assert set(altium.sent) <= READS                       # Altium was only read


def test_a_rejection_made_in_one_project_never_reaches_another(world,
                                                               monkeypatch):
    setup(monkeypatch)
    done = decide(action="reject", pairs=[TR, A5, XYZ], project_path=world["A"],
                  note="not our part")
    assert done["ok"] and len(done["recorded"]) == 3
    out_a, a = rows_of(world["A"])
    assert a["ABC123"]["Purchasing Status"] == "blocked"
    assert "CLOSE_MATCH_REJECTED" in a["ABC123"]["Review Reasons"]
    assert "CLOSE_MATCH_AVAILABLE" not in a["ABC123"]["Review Reasons"]
    mine = [c for c in out_a["close_matches"] if c["original_mpn"] == "ABC123"]
    assert all(c["rejected"] and c["decision"]["method"] == "project rejection"
               for c in mine)
    assert not any(c["approved"] for c in mine)
    # the other project is asked, as if nothing had been decided
    out_b, b = rows_of(world["B"])
    assert "CLOSE_MATCH_AVAILABLE" in b["ABC123"]["Review Reasons"]
    assert "CLOSE_MATCH_REJECTED" not in b["ABC123"]["Review Reasons"]
    assert not any(c.get("rejected") for c in out_b["close_matches"])
    assert any("rejected for this project" in line for line in
               open(out_a["close_matches_file"], encoding="utf-8-sig"))


def test_opposite_decisions_in_two_projects_are_independent(world, monkeypatch):
    setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    assert decide(action="reject", pairs=[TR], project_path=world["B"])["ok"]
    _, a = rows_of(world["A"])
    out_b, b = rows_of(world["B"])
    assert a["ABC123"]["Matched MPN"] == "ABC123-TR"
    assert b["ABC123"]["Matched MPN"] == ""
    assert next(c for c in out_b["close_matches"]
                if c["candidate_mpn"] == "ABC123-TR")["rejected"]
    assert decide(action="revoke", pairs=[TR], project_path=world["B"])["ok"]
    out_b, b = rows_of(world["B"])
    assert not any(c.get("rejected") for c in out_b["close_matches"])
    _, a = rows_of(world["A"])                             # A is untouched
    assert a["ABC123"]["Matched MPN"] == "ABC123-TR"


def test_the_same_project_path_spelled_differently_shares_its_decisions(
        world, monkeypatch):
    setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    other = os.path.join(world["tmp"], "WorkA", "sub", "..", "Board.PrjPcb")
    _, rows = rows_of(other)
    assert rows["ABC123"]["Matched MPN"] == "ABC123-TR"


# ---- reuse across the tools of one project ----------------------------------------------------------

def test_a_project_approval_is_reused_by_every_tool_without_asking_again(
        world, monkeypatch):
    dk, mo, altium = setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    A = world["A"]
    # export and a later analysis
    _, rows = rows_of(A)
    assert rows["ABC123"]["MPN Approval"] == "project approval"
    status = decide(action="status", project_path=A)
    assert [d["id"] for d in status["mpn_decisions"]["applied"]]
    # a DigiKey list
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "z", "requested_part_number": "z",
                             "quantity": 1, "reference_designator": ""}]
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    plan = prepare(project_path=A)
    item = next(i for i in plan["proposal"]["items"] if i["bom_mpn"] == "ABC123")
    assert item["ordered_mpn"] == "ABC123-TR"
    assert item["mpn_variant"]["approval"]["method"] == "project approval"
    done = commit(project_path=A, proposal_id=plan["proposal_id"],
                  approved=True)
    assert done["ok"] and done["verified"], done
    assert "ABC123-TR-ND" in {p["sku"] for p in fake.lists["list-1"]}
    journal = json.loads(next((world["out"] / "purchasing" / "journal")
                              .glob("*.json")).read_text(encoding="utf-8"))
    mine = next(i for i in journal["items"] if i["bom_mpn"] == "ABC123")
    assert mine["ordered_mpn"] == "ABC123-TR"
    assert mine["mpn_authorized_by"] == "project approval"
    # a Mouser cart
    mouser = FakeMouser()
    mouser.carts[KEY] = {"zzz": 1}
    monkeypatch.setattr(cart, "make_client", lambda d: mouser)
    args = dict(project_path=A, distributor="mouser", cart_key=KEY,
                prefer="mouser", list_id="")
    plan = prepare(**args)
    assert "ABC123-TR" in {i["ordered_mpn"] for i in plan["proposal"]["items"]}
    assert commit(proposal_id=plan["proposal_id"], approved=True, **args)["ok"]
    assert "603-abc123-tr" in mouser.carts[KEY]
    # the other project gets none of it
    other = prepare(project_path=world["B"])
    assert "ABC123-TR" not in {i["ordered_mpn"]
                               for i in other["proposal"]["items"]}
    assert any(e["part_number"] == "ABC123"
               for e in other["proposal"]["excluded"])


def test_an_mpn_approval_authorizes_no_quantity_price_or_destination(
        world, monkeypatch):
    dk, mo, _ = setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "z", "requested_part_number": "z",
                             "quantity": 1, "reference_designator": ""}]
    fake.lists["list-2"] = list(fake.lists["list-1"])
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    A = world["A"]
    plan = prepare(project_path=A)
    pid = plan["proposal_id"]
    # a price moves: the stored approval does not approve the new figures
    for hit in dk.hits:
        for offer in hit.sourcing["offers"]:
            for step in offer["price_breaks"]:
                step["unit_price"] = step["unit_price"] * 2
    refused = commit(project_path=A, proposal_id=pid, approved=True)
    assert refused["ok"] is False and refused["changed"] is True
    assert fake.calls == []
    # a different destination is a different proposal, whatever was approved
    other = prepare(project_path=A, list_id="list-2")
    assert other["proposal_id"] != prepare(project_path=A)["proposal_id"]
    assert commit(project_path=A, proposal_id=pid, approved=True,
                  list_id="list-2")["ok"] is False
    # a changed quantity is a different proposal too
    more = prepare(project_path=A, assemblies=5)
    assert more["proposal_id"] != prepare(project_path=A)["proposal_id"]


def test_the_cart_fingerprint_does_not_depend_on_how_the_mpn_was_authorized(
        world, monkeypatch):
    setup(monkeypatch)
    by_call = prepare(project_path="DemoBoard.PrjPcb", approve_mpn_variants=[TR])
    assert by_call["ok"]
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    by_store = prepare(project_path=world["A"])
    item = lambda plan: next(  # noqa: E731
        i for i in plan["proposal"]["items"] if i["bom_mpn"] == "ABC123")
    assert item(by_call)["mpn_variant"]["approval"]["method"] == "this call only"
    assert item(by_store)["mpn_variant"]["approval"]["method"] \
        == "project approval"
    assert by_call["proposal_id"] == by_store["proposal_id"]


# ---- the global rule in config.toml -----------------------------------------------------------------------

def write_config(world, text):
    world["out"].mkdir(parents=True, exist_ok=True)
    (world["out"] / "config.toml").write_text(text, encoding="utf-8")


def test_a_global_rule_applies_to_every_project_without_a_manual_decision(
        world, monkeypatch):
    setup(monkeypatch)
    write_config(world, '[matching]\nauto_approve = ["likely", "formatting"]\n')
    for project in (world["A"], world["B"]):
        out, rows = rows_of(project)
        row = rows["ABC123"]
        assert row["Matched MPN"] == "ABC123-TR"
        assert row["MPN Approval"] == "global rule: Likely Match"
        assert "config.toml" in row["MPN Approval Audit"]
        assert not store_file(world, project).exists()
    # a maker whose name merely resembles the BOM's is never authorized by a
    # global rule: it needs a person
    _, rows = rows_of(world["A"])
    assert rows["USB4110GFA"]["Matched MPN"] == ""
    assert rows["USB4110GFA"]["Purchasing Status"] == "blocked"


def test_a_projects_rejection_beats_the_global_rule_in_that_project_only(
        world, monkeypatch):
    setup(monkeypatch)
    write_config(world, '[matching]\nauto_approve = ["likely"]\n')
    assert decide(action="reject", pairs=[TR], project_path=world["A"])["ok"]
    _, a = rows_of(world["A"])
    _, b = rows_of(world["B"])
    assert a["ABC123"]["Matched MPN"] == ""
    assert b["ABC123"]["Matched MPN"] == "ABC123-TR"
    assert b["ABC123"]["MPN Approval"] == "global rule: Likely Match"


def test_only_formatting_and_likely_can_be_named_globally(world):
    write_config(world, '[matching]\nauto_approve = ["possible", "uncertain", '
                        '"likely", "everything"]\n')
    s = pc.resolve_settings()
    assert s.matching_auto_approve == ("likely",)
    text = " ".join(s.warnings)
    assert "possible" in text and "uncertain" in text and "everything" in text
    assert "needs a person" in text


def test_the_template_has_the_rule_empty_and_no_part_numbers(world):
    assert "auto_approve = []" in pc.TEMPLATE
    s = pc.resolve_settings()
    assert s.matching_auto_approve == () and s.warnings == ()


# ---- stale decisions -----------------------------------------------------------------------------------------

def board_with(**changes):
    parts = [_part("R1", "Acme", "ACM-R1", "RES 10K"),
             _part("R2", "Acme", "ACM-R1", "RES 10K"),
             _part("J1", "Global Connector Technology", "USB4110GFA",
                   "USB-C receptacle 24 position"),
             _part("U9", changes.get("maker", "Acme"),
                   changes.get("mpn", "ABC123"), "IC WIDGET 3V")]
    return parts


def stale_of(out):
    return out["mpn_decisions"]["stale"]


def test_a_decision_is_stale_when_the_components_part_number_changes(
        world, monkeypatch):
    dk, mo = distributors(monkeypatch)
    _altium(monkeypatch, board=BOARD)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    _altium(monkeypatch, board=board_with(mpn="ABC-123"))     # U9 changed
    out, rows = rows_of(world["A"])
    assert rows["ABC-123"]["Matched MPN"] == ""               # not applied
    stale = stale_of(out)
    assert len(stale) == 1 and "changed" in stale[0]["reason"]
    assert any("STALE and was not applied" in w for w in out["warnings"])
    assert not any(c["approved"] for c in out["close_matches"])


def test_a_decision_is_stale_when_the_components_manufacturer_changes(
        world, monkeypatch):
    dk, mo = distributors(monkeypatch)
    dk.hits.append(listing("digikey", "ABC123-TR", "Acme Industries",
                           "IC WIDGET 3V TAPE REEL", "AI-TR-ND",
                           ("Tape & Reel (TR)",)))
    _altium(monkeypatch, board=BOARD)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    _altium(monkeypatch, board=board_with(maker="Acme Industries"))
    out, rows = rows_of(world["A"])
    assert rows["ABC123"]["Matched MPN"] == ""
    assert "manufacturer or part number changed" in stale_of(out)[0]["reason"]


def test_a_decision_is_stale_when_the_proposed_match_is_no_longer_the_same(
        world, monkeypatch):
    dk, mo, _ = setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    for hit in dk.hits + mo.hits:                    # the distributors relist it
        if hit.mpn == "ABC123-TR":
            hit.manufacturer = "Zeta Corp"
    out, rows = rows_of(world["A"])
    assert rows["ABC123"]["Matched MPN"] == ""
    assert "no longer the same" in stale_of(out)[0]["reason"]
    assert "Zeta Corp" in stale_of(out)[0]["reason"]


def test_a_decision_is_stale_when_the_candidate_is_no_longer_listed(
        world, monkeypatch):
    dk, mo, _ = setup(monkeypatch)
    assert decide(action="approve", pairs=[A5], project_path=world["A"])["ok"]
    dk.hits[:] = [h for h in dk.hits if h.mpn != "ABC123A5"]
    out, rows = rows_of(world["A"])
    assert "no longer listed" in stale_of(out)[0]["reason"]
    assert rows["ABC123"]["Matched MPN"] == ""


def test_a_stale_rejection_is_reported_and_no_longer_hides_the_candidate(
        world, monkeypatch):
    dk, mo, _ = setup(monkeypatch)
    assert decide(action="reject", pairs=[TR], project_path=world["A"])["ok"]
    for hit in dk.hits + mo.hits:
        if hit.mpn == "ABC123-TR":
            hit.manufacturer = "Zeta Corp"
    out, _ = rows_of(world["A"])
    assert len(stale_of(out)) == 1 and stale_of(out)[0]["decision"] == "rejected"
    shown = [c for c in out["close_matches"] if c["candidate_mpn"] == "ABC123-TR"]
    assert shown and not any(c.get("rejected") for c in shown)


def test_a_stale_decision_does_not_stop_an_independent_global_authorization(
        world, monkeypatch):
    dk, mo, _ = setup(monkeypatch)
    write_config(world, '[matching]\nauto_approve = ["likely"]\n')
    assert decide(action="approve", pairs=[A5], project_path=world["A"])["ok"]
    dk.hits[:] = [h for h in dk.hits if h.mpn != "ABC123A5"]
    out, rows = rows_of(world["A"])
    assert stale_of(out)                                  # the manual one
    assert rows["ABC123"]["MPN Approval"] == "global rule: Likely Match"


def test_a_decision_for_a_component_now_matching_exactly_is_unused_not_stale(
        world, monkeypatch):
    dk, mo, _ = setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    dk.hits.append(listing("digikey", "ABC123", "Acme", "IC WIDGET 3V",
                           "ABC123-ND", ("Cut Tape (CT)",)))
    out, rows = rows_of(world["A"])
    assert rows["ABC123"]["Matched MPN"] == ""            # exact: nothing to vary
    assert rows["ABC123"]["Purchasing Status"] != "blocked"
    assert out["mpn_decisions"]["stale"] == []
    assert "matches exactly" in out["mpn_decisions"]["unused"][0]["reason"]


# ---- the audit trail in the CSV ------------------------------------------------------------------------------------

def test_the_csv_keeps_the_original_number_the_method_and_the_audit(world,
                                                                    monkeypatch):
    setup(monkeypatch)
    assert decide(action="approve", pairs=[USB], project_path=world["A"],
                  note="checked the GCT datasheet")["ok"]
    out, rows = rows_of(world["A"], approve_mpn_variants=[TR])
    usb, widget = rows["USB4110GFA"], rows["ABC123"]
    assert usb["Part Number"] == "USB4110GFA"             # Altium's own number
    assert usb["Matched MPN"] == "USB4110-GF-A"
    assert usb["MPN Approval"] == "project approval"
    audit = usb["MPN Approval Audit"]
    scope = pd.scope_for(world["A"])
    for needle in ("original USB4110GFA (Global Connector Technology)",
                   "matched USB4110-GF-A (GCT)", "Formatting Match",
                   "authorized by project approval", "decided 20",
                   "decision ", f"project {scope.id}",
                   "checked the GCT datasheet"):
        assert needle in audit, needle
    assert usb["Purchasing Status"] == "review"           # the maker name differs
    assert widget["Part Number"] == "ABC123"
    assert widget["MPN Approval"] == "this call only"
    assert "not remembered" in widget["MPN Approval Audit"]
    # the companion file carries the decision too
    companion = read(out["close_matches_file"])
    used = next(r for r in companion if r["Candidate MPN"] == "USB4110-GF-A")
    assert used["Approval Method"] == "project approval"
    assert used["Decision ID"] and used["Decision Date"].startswith("20")


def test_the_default_columns_carry_the_method_and_the_audit(world, monkeypatch):
    from eda_agent.export.purchasing_bom_export import DEFAULT_COLUMNS
    assert {"matched_mpn", "mpn_approval", "mpn_audit"} <= set(DEFAULT_COLUMNS)
    setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    out = export(project_path=world["A"])
    row = next(r for r in read(out["output_path"])
               if r["Part Number"] == "ABC123")
    assert row["MPN Approval"] == "project approval" and row["Matched MPN"]


# ---- when there is no project to scope to ------------------------------------------------------------------------------

def test_without_an_identifiable_project_nothing_is_remembered(world,
                                                               monkeypatch):
    setup(monkeypatch)
    out = decide(action="approve", pairs=[TR], project_path="Board.PrjPcb")
    assert out["ok"] is False and "none can be identified" in out["reason"]
    assert not (world["out"] / "purchasing").exists()
    shown = export(project_path="Board.PrjPcb", columns=COLS,
                   approve_mpn_variants=[TR])                # per call still works
    assert shown["ok"] and shown["project"]["scoped"] is False
    assert "not an absolute path" in shown["project"]["why"]
    rows = {r["Part Number"]: r for r in read(shown["output_path"])}
    assert rows["ABC123"]["MPN Approval"] == "this call only"


def test_lines_given_directly_have_no_project(world, monkeypatch):
    distributors(monkeypatch)
    out = export(project_path=None, columns=COLS, lines=[
        {"manufacturer": "Acme", "part_number": "ABC123", "quantity": 1,
         "description": "IC WIDGET 3V", "designators": ["U9"]}])
    assert out["ok"] and out["project"]["scoped"] is False
    assert "lines were given directly" in out["project"]["why"]


class _Focused(_Altium):
    """Altium that names its focused project, and can change it mid-run."""

    def __init__(self, paths, **kw):
        super().__init__(**kw)
        self.paths = list(paths)

    async def send_command_async(self, command, params=None, **kwargs):
        if command == "project.get_focused":
            self.sent.append(command)
            path = self.paths.pop(0) if len(self.paths) > 1 else self.paths[0]
            return {"project_name": os.path.basename(path),
                    "project_path": path, "document_count": 3}
        return await super().send_command_async(command, params, **kwargs)


def test_the_focused_project_is_used_when_no_path_is_given(world, monkeypatch):
    from eda_agent import bridge as bridge_module
    distributors(monkeypatch)
    focus = _Focused([world["A"]], board=BOARD)
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: focus)
    done = decide(action="approve", pairs=[TR])
    assert done["ok"], done
    out = export(project_path=None, columns=COLS)
    assert out["ok"] and out["project"]["id"] == pd.scope_for(world["A"]).id
    rows = {r["Part Number"]: r for r in read(out["output_path"])}
    assert rows["ABC123"]["MPN Approval"] == "project approval"
    other = _Focused([world["B"]], board=BOARD)
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: other)
    out = export(project_path=None, columns=COLS)
    rows = {r["Part Number"]: r for r in read(out["output_path"])}
    assert rows["ABC123"]["Matched MPN"] == ""


def test_a_focus_change_during_the_run_applies_and_writes_nothing(world,
                                                                  monkeypatch):
    from eda_agent import bridge as bridge_module
    distributors(monkeypatch)
    focus = _Focused([world["A"], world["B"]], board=BOARD)
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: focus)
    out = export(project_path=None, columns=COLS)
    assert out["ok"] is False and "focused project changed" in out["reason"]
    assert not list(world["out"].glob("bom/*.csv"))


# ---- the tool itself ------------------------------------------------------------------------------------------------------

def test_list_revoke_and_replace(world, monkeypatch):
    setup(monkeypatch)
    A = world["A"]
    assert decide(action="reject", pairs=[TR], project_path=A)["ok"]
    listed = decide(action="list", project_path=A)
    assert [d["decision"] for d in listed["decisions"]] == ["rejected"]
    assert listed["project"]["scoped"] and listed["file"].endswith(".json")
    replaced = decide(action="approve", pairs=[TR], project_path=A)
    assert replaced["recorded"][0]["replaced"] == "rejected"
    listed = decide(action="list", project_path=A)
    assert [d["decision"] for d in listed["decisions"]] == ["approved"]
    d = listed["decisions"][0]
    assert d["method"] == "manual" and d["designators"] == ["U9"]
    assert d["original_manufacturer"] == "Acme" and d["label"] == pm.LIKELY
    assert decide(action="revoke", pairs=[TR], project_path=A)["revoked"] == 1
    assert decide(action="list", project_path=A)["decisions"] == []
    gone = decide(action="revoke", pairs=[TR], project_path=A)
    assert gone["ok"] is False and "no stored decision" in gone["reason"]


def test_a_pair_that_is_not_a_candidate_now_is_refused(world, monkeypatch):
    setup(monkeypatch)
    out = decide(action="approve", pairs=[
        {"original_mpn": "ABC123", "candidate_mpn": "ABC123-NOPE"}],
        project_path=world["A"])
    assert out["ok"] is False and out["refused"][0]["why"]
    assert not store_file(world, world["A"]).exists()


def test_bad_requests_are_refused_before_anything_is_read(world, monkeypatch):
    dk, mo, altium = setup(monkeypatch)
    for kw in (dict(action="explode"), dict(action="approve"),
               dict(action="approve", pairs=[{"original_mpn": "A",
                                              "candidate_mpn": "B"}]),
               dict(action="reject", pairs="ABC123=ABC123-TR")):
        out = decide(project_path=world["A"], **kw)
        assert out["ok"] is False and out["reason"]
    assert altium.sent == [] and dk.asked == [] and mo.asked == []


def test_the_stored_file_holds_no_credential_and_no_quantity(world, monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "sentinel-credential-123")
    setup(monkeypatch)
    assert decide(action="approve", pairs=[TR], project_path=world["A"])["ok"]
    text = store_file(world, world["A"]).read_text(encoding="utf-8")
    assert "sentinel" not in text
    data = json.loads(text)
    assert set(data) == {"version", "project_id", "project_path",
                         "project_name", "decisions"}
    for key in ("quantity", "price", "unit_price", "sku", "distributor_sku"):
        assert key not in data["decisions"][0]
    assert data["project_id"] == pd.scope_for(world["A"]).id


# ---- the authorizer on its own -----------------------------------------------------------------------------------------------------

def cand(label=pm.LIKELY, match="exact", override=False, mpn="ABC123-TR",
         maker="Acme"):
    return {"original_mpn": "ABC123", "candidate_mpn": mpn,
            "manufacturer": maker, "label": label,
            "manufacturer_match": match, "manufacturer_override": override}


ENTRY = {"manufacturer": "Acme", "part_number": "ABC123",
         "designators": ["U9"], "distributors": {}}


def manual(kind, maker="Acme", mpn="ABC123", candidate="ABC123-TR",
           cmaker="Acme"):
    return {"id": pd.decision_id(maker, mpn, candidate), "decision": kind,
            "original_manufacturer": maker, "original_mpn": mpn,
            "candidate_mpn": candidate, "candidate_manufacturer": cmaker,
            "designators": ["U9"], "decided_at": "2026-11-18T00:00:00Z",
            "note": ""}


def judge(decisions=(), call=None, auto=(), c=None):
    a = pd.Authorizer(decisions, call, auto, pd.scope_for(
        os.path.join(os.getcwd(), "P.PrjPcb")))
    a.start([ENTRY])
    return a.judge(ENTRY, c or cand())


def test_the_order_of_authorization():
    call = {pm.approval_key("ABC123", "ABC123-TR"): {}}
    assert judge()["status"] == "none"
    assert judge(call=call)["method"] == "this call only"
    assert judge(auto=("likely",))["method"] == "global rule: Likely Match"
    assert judge([manual("approved")], call, ("likely",))["method"] \
        == "project approval"
    assert judge([manual("rejected")], call, ("likely",))["status"] \
        == "rejected"


def test_a_global_rule_needs_the_named_label_and_an_identical_maker():
    assert judge(auto=("formatting",))["status"] == "none"          # a likely
    assert judge(auto=("likely",), c=cand(match="partial"))["status"] == "none"
    assert judge(auto=("likely",), c=cand(match="alias", override=True)
                 )["status"] == "none"
    assert judge(auto=("likely",), c=cand(label=pm.POSSIBLE))["status"] \
        == "none"
    assert judge(auto=("likely",), c=cand(label=pm.UNCERTAIN))["status"] \
        == "none"
    assert judge(auto=("possible", "uncertain"))["status"] == "none"
    assert judge(auto=("formatting",), c=cand(label=pm.FORMATTING)
                 )["method"] == "global rule: Formatting Match"


def test_a_decision_is_bound_to_the_original_and_the_spelling_of_the_candidate():
    other_spelling = cand(mpn="ABC123-tr")      # case differs: same spelling key
    assert judge([manual("approved")], c=other_spelling)["status"] == "approved"
    assert judge([manual("approved")], c=cand(mpn="ABC123 -TR")
                 )["status"] == "approved"      # spaces never matter
    assert judge([manual("approved")], c=cand(mpn="ABC123-T-R"))["status"] \
        == "none"                               # punctuation does
    assert judge([manual("approved", maker="Other")])["status"] == "none"
    assert judge([manual("approved", mpn="ABC-123")])["status"] == "none"
