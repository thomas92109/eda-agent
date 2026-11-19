# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Prepare, approve, commit: a DigiKey list and a Mouser cart, synthetic.

No distributor is contacted. The Mouser double ADDS to a line, as the real
cart does, and can lose its reply after applying a write, which is the case
a retry must survive. One test drives the real MouserCart client against a
simulated wire to prove the orchestrator speaks the client's real API.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from eda_agent.export import destination, purchasing_cart as cart
from eda_agent.export import purchasing_config as pc
from eda_agent.export.purchasing_lines import build_purchase_lines
from eda_agent.tools import purchasing_cart as cart_tools
from tests.test_purchasing_lines import analysis, entry, hit, miss, off
from tests.test_purchasing_workflow import _altium, _stocked

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR", str(tmp_path / "exports"))
    return tmp_path / "exports"


# ---- doubles --------------------------------------------------------------------------

class FakeMouser:
    """A cart service: carts by key, inserts that add, replies that can be lost."""

    def __init__(self):
        self.carts: dict[str, dict[str, int]] = {}
        self.calls: list = []
        self.minimum: dict[str, tuple[int, int]] = {}
        self.checked: list = []
        self.lose_reply_after_apply = 0
        self.empty_reads = 0
        self.apply_shortfall = 0
        self.n = 0

    def prepare_items(self, items):
        self.calls.append(("prepare", [dict(i) for i in items]))
        return [dict(i, item=n) for n, i in enumerate(items)]

    def unorderable(self, ready):
        self.checked.extend((r["sku"], r["quantity"]) for r in ready)
        out = []
        for r in ready:
            moq, mult = self.minimum.get(r["sku"], (1, 1))
            if r["quantity"] < moq or r["quantity"] % mult:
                out.append({"item": r["item"], "sku": r["sku"],
                            "problems": [f"quantity {r['quantity']} is below "
                                         f"Mouser's minimum of {moq}"]})
        return out

    def _apply(self, key, items):
        for i in items:
            self.carts[key][i["sku"].casefold()] = self.carts[key].get(
                i["sku"].casefold(), 0) + i["quantity"] - self.apply_shortfall

    def create_cart(self, items, check_products=True):
        self.calls.append(("create", [dict(i) for i in items], check_products))
        self.n += 1
        key = f"00000000-0000-0000-0000-{self.n:012d}"
        self.carts[key] = {}
        self._apply(key, items)
        if self.lose_reply_after_apply:
            self.lose_reply_after_apply -= 1
            raise TimeoutError("the reply was lost")
        return {"cart_key": key, "created": True}

    def add_items(self, key, items, check_products=True):
        self.calls.append(("add", key, [dict(i) for i in items],
                           check_products))
        self._apply(key, items)
        if self.lose_reply_after_apply:
            self.lose_reply_after_apply -= 1
            raise TimeoutError("the reply was lost")
        return {"cart_key": key, "created": False}

    def get_cart(self, key):
        self.calls.append(("get", key))
        if self.empty_reads:
            self.empty_reads -= 1
            return {"cart_key": key, "items": []}
        return {"cart_key": key, "items": [
            {"sku": s, "quantity": q} for s, q in self.carts[key].items()]}

    def adds(self):
        return [c for c in self.calls if c[0] in ("add", "create")]


class FakeLists:
    def __init__(self):
        self.lists: dict[str, list] = {}
        self.calls: list = []
        self.n = 0
        self.lose_reply_after_apply = 0

    def create_list(self, name, visibility="private"):
        self.calls.append(("create", name))
        self.n += 1
        lid = f"list-{self.n}"
        self.lists[lid] = []
        return {"list_id": lid, "name": name}

    def get_list(self, lid):
        self.calls.append(("get", lid))
        return {"list_id": lid, "items": [dict(p) for p in self.lists[lid]]}

    def add_items(self, lid, items, check_products=True):
        self.calls.append(("add", lid, [dict(i) for i in items]))
        for i in items:
            self.lists[lid].append({
                "sku": i["sku"], "requested_part_number": i["sku"],
                "quantity": i["quantity"],
                "reference_designator": ",".join(i["designators"])})
        if self.lose_reply_after_apply:
            self.lose_reply_after_apply -= 1
            raise TimeoutError("the reply was lost")
        return {"list_id": lid}


@pytest.fixture
def mouser(monkeypatch):
    fake = FakeMouser()
    monkeypatch.setattr(cart, "make_client",
                        lambda d: fake if d == "mouser" else FakeLists())
    return fake


@pytest.fixture
def lists(monkeypatch):
    fake = FakeLists()
    monkeypatch.setattr(cart, "make_client",
                        lambda d: fake if d == "digikey" else FakeMouser())
    return fake


# ---- proposals ---------------------------------------------------------------------------

def settings(**args):
    args.setdefault("assemblies", 1)
    args.setdefault("overage_factor", 1)
    args.setdefault("prefer", "mouser")
    return pc.resolve_settings(args)


def purchase_lines(*entries, **kw):
    s = kw.pop("settings", None) or settings()
    return build_purchase_lines(analysis(*entries, **kw), s, now=NOW)["lines"]


def mouser_line(qty=25, sku="MO-1", **offer):
    return entry(maker="Acme", mpn="ACM-1", qty=qty,
                 designators=[f"R{n}" for n in range(1, qty + 1)],
                 mo=hit(off(sku, **offer)))


def proposal_for(*lines, distributor="mouser", target=None, include=()):
    target = target or cart.normalize_target(
        distributor, cart_key="K-1" if distributor == "mouser" else "",
        list_id="" if distributor == "mouser" else "list-9")
    return cart.build_proposal(lines, distributor, target, include)


def mouser_proposal(qty=25, cart_key="00000000-0000-0000-0000-000000000009",
                    **offer):
    target = cart.normalize_target("mouser", cart_key=cart_key)
    return cart.build_proposal(purchase_lines(mouser_line(qty, **offer)),
                               "mouser", target)


# ---- targets (CART-3) ------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    dict(distributor="farnell"),
    dict(distributor="mouser"),                                  # no cart named
    dict(distributor="mouser", cart_key="K", create_new=True),
    dict(distributor="mouser", cart_key="K", list_id="L"),
    dict(distributor="digikey"),
    dict(distributor="digikey", create_new=True),                # no name
    dict(distributor="digikey", list_id="L", create_new=True,
         list_name="x"),
    dict(distributor="digikey", cart_key="K", list_id="L"),
])
def test_a_target_is_never_guessed(args):
    with pytest.raises(cart.CartError):
        cart.normalize_target(**args)


def test_valid_targets():
    assert cart.normalize_target("mouser", cart_key="K")["cart_key"] == "K"
    assert cart.normalize_target("mouser", create_new=True)["create_new"]
    assert cart.normalize_target("digikey", list_id="L")["list_id"] == "L"
    t = cart.normalize_target("digikey", create_new=True, list_name=" My  List ")
    assert t["list_name"] == "My List"


# ---- eligibility (REV-5, CART-4, TS-R20, TS-R22) ----------------------------------------

def test_only_ready_lines_go_in_and_blocked_never_do():
    s = settings(prefer="mouser")
    lines = purchase_lines(
        entry(maker="A", mpn="1", designators=["R1"],
              mo=hit(off("READY-1"))),
        entry(maker="B", mpn="2", designators=["R2"],
              mo=hit(off("REVIEW-1", stock=None))),
        entry(maker="C", mpn="3", designators=["R3"],
              mo=hit(off("EOL-1"), lifecycle="end_of_life", text="EOL")),
        entry(maker="D", mpn="4", designators=["R4"],
              mo=hit(off("EMPTY-1", stock=0))),
        entry(maker="E", mpn="5", designators=["R5"], dk=hit(off("DK-5"))),
        unresolved=[{"designator": "C9", "reason": "no Part Number parameter"}],
        settings=s)
    ids = {pl.line.part_number: pl.line.line_id for pl in lines}
    p = proposal_for(*lines)
    assert [i["sku"] for i in p["items"]] == ["READY-1"]
    why = {e["line_id"]: e["why"] for e in p["excluded"]}
    assert "needs review" in why[ids["2"]] and "include_lines" in why[ids["2"]]
    assert why[ids["3"]].startswith("blocked") and "LIFECYCLE_EOL" in why[ids["3"]]
    assert why[ids["4"]].startswith("blocked")
    assert why[ids["5"]] == "assigned to digikey"
    assert any("unresolved" in w for w in why.values())
    assert len(p["items"]) + len(p["excluded"]) == len(lines)


def test_a_review_line_goes_in_only_when_named_and_a_blocked_one_never():
    lines = purchase_lines(
        entry(maker="B", mpn="2", mo=hit(off("REVIEW-1", stock=None))),
        entry(maker="C", mpn="3", mo=hit(off("EOL-1"), lifecycle="obsolete",
                                          text="Obsolete")))
    review = next(l for l in lines if l.line.part_number == "2").line.line_id
    eol = next(l for l in lines if l.line.part_number == "3").line.line_id
    p = proposal_for(*lines, include=[review, eol, "L999"])
    assert [i["sku"] for i in p["items"]] == ["REVIEW-1"]
    assert p["items"][0]["selected_for_review"] is True
    assert any("never added, even when named" in e["why"]
               for e in p["excluded"])
    assert any("L999" in w for w in p["warnings"])


def test_a_setting_cannot_make_an_end_of_life_line_eligible(folder):
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text(
        '[review.severity]\nLIFECYCLE_EOL = "info"\nLIFECYCLE_OBSOLETE = '
        '"info"\nDISTRIBUTOR_DISCONTINUED = "info"\n', encoding="utf-8")
    for lc in ("end_of_life", "obsolete", "discontinued"):
        lines = purchase_lines(entry(mo=hit(off("X-1"), lifecycle=lc)))
        p = proposal_for(*lines, include=[lines[0].line.line_id])
        assert p["items"] == []


# ---- the fingerprint (CART-2, TS-C02, TS-C03) ------------------------------------------------

def test_the_same_input_gives_the_same_id():
    assert mouser_proposal()["proposal_id"] == mouser_proposal()["proposal_id"]


@pytest.mark.parametrize("change", [
    dict(ladder=((1, 0.11),)),                       # unit price
    dict(currency="EUR"),                            # currency
    dict(sku="MO-2"),                                # sku
    dict(packaging=("Reel",)),                       # packaging
])
def test_any_figure_that_moves_changes_the_id(change):
    assert mouser_proposal(**change)["proposal_id"] \
        != mouser_proposal()["proposal_id"]


def test_quantity_lifecycle_designators_and_target_change_the_id():
    base = mouser_proposal()["proposal_id"]
    assert mouser_proposal(qty=26)["proposal_id"] != base
    lines = purchase_lines(mouser_line())
    target = cart.normalize_target("mouser", cart_key="00000000-0000-0000-0000-000000000009")
    p = cart.build_proposal(lines, "mouser", target)
    other = cart.build_proposal(purchase_lines(entry(
        maker="Acme", mpn="ACM-1", qty=25,
        designators=[f"X{n}" for n in range(1, 26)],
        mo=hit(off("MO-1")))), "mouser", target)
    assert other["proposal_id"] != p["proposal_id"]
    eol = cart.build_proposal(purchase_lines(entry(
        maker="Acme", mpn="ACM-1", qty=25,
        designators=[f"R{n}" for n in range(1, 26)],
        mo=hit(off("MO-1"), lifecycle="nrnd", text="NRND"))), "mouser", target)
    assert eol["items"] == [] or eol["proposal_id"] != p["proposal_id"]
    moved = cart.build_proposal(lines, "mouser",
                                cart.normalize_target("mouser", cart_key="K2"))
    assert moved["proposal_id"] != p["proposal_id"]


def test_the_lifecycle_status_is_part_of_the_fingerprint():
    target = cart.normalize_target("mouser", cart_key="K")
    ids = set()
    for lc, text in (("active", "Active"), ("preliminary", "Preliminary")):
        lines = purchase_lines(entry(
            mo=hit(off("MO-1"), lifecycle=lc, text=text)))
        ids.add(cart.build_proposal(lines, "mouser", target)["proposal_id"])
    assert len(ids) == 2


# ---- Mouser commits (CART-5 .. CART-12) ---------------------------------------------------------

KEY = "00000000-0000-0000-0000-000000000009"


def commit(p, client, cart_state_checked=False):
    return cart.commit_mouser(p, client, cart.Journal(p), cart_state_checked)


def test_a_new_cart_gets_the_target_and_is_verified(mouser):
    target = cart.normalize_target("mouser", create_new=True)
    p = cart.build_proposal(purchase_lines(mouser_line(25)), "mouser", target)
    out = commit(p, mouser)
    assert out["ok"] and out["created"] and out["verified"]
    key = out["cart_key"]
    assert mouser.carts[key] == {"mo-1": 25}
    assert out["items"][0]["status"] == "added"
    assert mouser.calls[-1] == ("get", key)               # read back


def test_the_cart_holds_ten_and_the_target_is_twenty_five_so_fifteen_are_added(
        mouser):
    mouser.carts[KEY] = {"mo-1": 10}
    out = commit(mouser_proposal(25), mouser)
    assert out["ok"] and out["verified"]
    assert mouser.carts[KEY] == {"mo-1": 25}
    add = [c for c in mouser.calls if c[0] == "add"][0]
    assert add[2][0]["quantity"] == 15 and add[3] is False
    assert out["items"][0]["had"] == 10 and out["items"][0]["added"] == 15


def test_a_cart_at_target_gets_nothing(mouser):
    mouser.carts[KEY] = {"mo-1": 25}
    out = commit(mouser_proposal(25), mouser)
    assert out["ok"] and out["verified"] and mouser.adds() == []
    assert out["items"][0]["status"] == "satisfied"


def test_a_delta_below_the_minimum_is_checked_as_the_target_total(mouser):
    mouser.carts[KEY] = {"mo-1": 20}
    mouser.minimum["MO-1"] = (10, 5)
    out = commit(mouser_proposal(25), mouser)         # delta 5 < MOQ 10
    assert out["ok"], out
    assert ("MO-1", 25) in mouser.checked and ("MO-1", 5) not in mouser.checked
    assert mouser.carts[KEY]["mo-1"] == 25


def test_a_target_the_catalogue_refuses_writes_nothing(mouser):
    mouser.carts[KEY] = {"mo-1": 2}
    mouser.minimum["MO-1"] = (100, 100)
    out = commit(mouser_proposal(25), mouser)
    assert out["ok"] is False and out["problems"] and mouser.adds() == []


def test_a_retry_after_a_lost_reply_adds_nothing_twice(mouser):
    mouser.carts[KEY] = {"mo-1": 10}
    mouser.lose_reply_after_apply = 1
    p = mouser_proposal(25)
    first = commit(p, mouser)
    assert first["ok"] is False and first["outcome"] == "unknown"
    assert mouser.carts[KEY]["mo-1"] == 25            # it was applied
    second = commit(p, mouser)                        # same proposal again
    assert second["ok"] and second["verified"]
    assert mouser.carts[KEY]["mo-1"] == 25            # not 40
    assert second["items"][0]["status"] == "satisfied"
    assert len([c for c in mouser.calls if c[0] == "add"]) == 1


def test_the_journal_is_written_before_the_first_write_and_updated_after(
        mouser, folder):
    mouser.carts[KEY] = {"mo-1": 10}
    mouser.lose_reply_after_apply = 1
    p = mouser_proposal(25)
    commit(p, mouser)
    path = folder / "purchasing" / "journal" / f"{p['proposal_id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    kinds = [e["event"] for e in data["events"]]
    assert kinds == ["adding", "outcome_unknown"]
    assert data["events"][0]["deltas"] == {"MO-1": 15}
    assert data["events"][0]["expected_after"] == {"MO-1": 25}
    commit(p, mouser)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["events"][-1]["event"] == "readback"
    assert data["confirmed"] == {"mo-1": 25}
    assert data["items"][0]["target_quantity"] == 25


def test_a_cart_creation_that_may_have_happened_is_never_repeated(mouser):
    mouser.lose_reply_after_apply = 1
    target = cart.normalize_target("mouser", create_new=True)
    p = cart.build_proposal(purchase_lines(mouser_line(25)), "mouser", target)
    first = commit(p, mouser)
    assert first["ok"] is False and first["outcome"] == "unknown"
    assert len(mouser.carts) == 1
    second = commit(p, mouser)
    assert second["ok"] is False and "NOT created" in second["reason"]
    assert len(mouser.carts) == 1                       # still one cart
    assert len([c for c in mouser.calls if c[0] == "create"]) == 1


def test_an_empty_readback_is_an_unknown_state_not_a_failure_to_retry(mouser):
    mouser.carts[KEY] = {}
    out = commit(mouser_proposal(25), mouser)
    assert out["ok"] is False and out["outcome"] == "unknown"
    assert "UNKNOWN" in out["reason"] and mouser.adds() == []
    out = commit(mouser_proposal(25), mouser, cart_state_checked=True)
    assert out["ok"] and mouser.carts[KEY] == {"mo-1": 25}


def test_a_cart_that_reads_lower_than_the_journal_is_unknown(mouser):
    mouser.carts[KEY] = {"mo-1": 10}
    p = mouser_proposal(25)
    assert commit(p, mouser)["ok"]
    mouser.carts[KEY] = {"mo-1": 3}                    # emptied behind our back
    refused = commit(p, mouser)
    assert refused["ok"] is False and "holds less than the journal" in refused["reason"]
    assert mouser.carts[KEY] == {"mo-1": 3}
    ok = commit(p, mouser, cart_state_checked=True)
    assert ok["ok"] and mouser.carts[KEY]["mo-1"] == 25


def test_a_cart_holding_more_than_the_target_is_left_alone(mouser):
    mouser.carts[KEY] = {"mo-1": 40}
    out = commit(mouser_proposal(25), mouser)
    row = out["items"][0]
    assert row["status"] == "over_target" and row["verified"] is False
    assert mouser.carts[KEY] == {"mo-1": 40} and mouser.adds() == []
    assert "nothing here lowers" in row["note"]
    assert out["verified"] is False


def test_a_readback_that_differs_is_not_verified(mouser):
    mouser.carts[KEY] = {"mo-1": 10}
    mouser.apply_shortfall = 3
    out = commit(mouser_proposal(25), mouser)
    row = out["items"][0]
    assert row["verified"] is False and row["status"] == "short"
    assert row["now"] == 22 and out["verified"] is False


def test_the_designators_are_not_sent_to_mouser_but_are_reported(mouser):
    mouser.carts[KEY] = {"mo-1": 1}
    out = commit(mouser_proposal(25), mouser)
    assert "reference designators" in out["not_sent_to_mouser"]
    assert out["note"].startswith("Nothing was ordered")


# ---- DigiKey commits (CART-11, TS-C14, TS-C15) -------------------------------------------------

def digikey_proposal(qty=25, list_id="list-1", **offer):
    lines = purchase_lines(entry(
        maker="Acme", mpn="ACM-1", qty=qty,
        designators=[f"R{n}" for n in range(1, qty + 1)],
        dk=hit(off("DK-1", **offer))), settings=settings(prefer="digikey"))
    target = cart.normalize_target("digikey", list_id=list_id)
    return cart.build_proposal(lines, "digikey", target)


def commit_dk(p, client, checked=False):
    return cart.commit_digikey(p, client, cart.Journal(p), checked)


def test_a_new_list_is_created_filled_and_read_back(lists):
    lines = purchase_lines(entry(qty=3, dk=hit(off("DK-1"))),
                           settings=settings(prefer="digikey"))
    target = cart.normalize_target("digikey", create_new=True,
                                   list_name="DemoBoard")
    p = cart.build_proposal(lines, "digikey", target)
    out = commit_dk(p, lists)
    assert out["ok"] and out["created"] and out["verified"]
    assert lists.lists[out["list_id"]][0]["quantity"] == 3
    assert [c[0] for c in lists.calls] == ["create", "get", "add", "get"]


def test_a_line_already_in_the_list_is_satisfied_not_added_again(lists):
    lists.lists["list-1"] = [{"sku": "DK-1", "requested_part_number": "DK-1",
                              "quantity": 25,
                              "reference_designator": ",".join(
                                  f"R{n}" for n in range(1, 26))}]
    out = commit_dk(digikey_proposal(25), lists)
    assert out["ok"] and out["verified"]
    assert out["items"][0]["status"] == "satisfied"
    assert not any(c[0] == "add" for c in lists.calls)


def test_a_line_with_another_quantity_is_reported_and_left_alone(lists):
    lists.lists["list-1"] = [{"sku": "DK-1", "requested_part_number": "DK-1",
                              "quantity": 10, "reference_designator": ""}]
    out = commit_dk(digikey_proposal(25), lists)
    row = out["items"][0]
    assert row["status"] == "conflict" and row["verified"] is False
    assert "left as it is" in row["note"]
    assert lists.lists["list-1"][0]["quantity"] == 10
    assert not any(c[0] == "add" for c in lists.calls)


def test_a_retry_after_a_lost_reply_does_not_add_the_line_twice(lists):
    lists.lists["list-1"] = []
    lists.lose_reply_after_apply = 1
    p = digikey_proposal(25)
    first = commit_dk(p, lists)
    assert first["ok"] is False and first["outcome"] == "unknown"
    second = commit_dk(p, lists)
    assert second["ok"] and second["verified"]
    assert len(lists.lists["list-1"]) == 1


def test_a_list_creation_that_may_have_happened_is_not_repeated(lists):
    target = cart.normalize_target("digikey", create_new=True, list_name="X")
    p = cart.build_proposal(purchase_lines(
        entry(dk=hit(off("DK-1"))), settings=settings(prefer="digikey")),
        "digikey", target)
    j = cart.Journal(p)
    j.event("create_started", name="X")                # the reply was lost
    out = cart.commit_digikey(p, lists, cart.Journal(p))
    assert out["ok"] is False and "NOT created" in out["reason"]
    assert lists.n == 0


def test_a_list_that_lost_what_the_journal_held_is_unknown(lists):
    lists.lists["list-1"] = []
    p = digikey_proposal(3)
    assert commit_dk(p, lists)["ok"]
    lists.lists["list-1"] = []                         # emptied behind our back
    out = commit_dk(p, lists)
    assert out["ok"] is False and out["outcome"] == "unknown"
    assert commit_dk(p, lists, checked=True)["ok"]


# ---- the real MouserCart client on a simulated wire (CART-1, TS-C01, TS-C17) ------------------

class _Wire:
    """Mouser's cart service, as the real client sees it: one ``_call``."""

    def __init__(self):
        self.carts = {"00000000-0000-0000-0000-000000000009": {}}
        self.sent: list = []

    def reply(self, key):
        return {"CartKey": key, "CurrencyCode": "USD", "CartItems": [
            {"MouserPartNumber": sku, "Quantity": q, "Manufacturer": "Acme",
             "MfrPartNumber": "ACM-1"}
            for sku, q in self.carts[key].items()],
            "TotalItemCount": sum(self.carts[key].values())}

    def call(self, client, url, query, body=None):
        if body is None:
            return self.reply(query["cartKey"])
        self.sent.append(body)
        key = body.get("CartKey") or "00000000-0000-0000-0000-00000000000a"
        self.carts.setdefault(key, {})
        for line in body["CartItems"]:
            sku = line["MouserPartNumber"]
            self.carts[key][sku] = self.carts[key].get(sku, 0) + line["Quantity"]
        return self.reply(key)


def test_the_real_client_is_driven_with_its_own_api(monkeypatch):
    from eda_agent.libimport.providers.mouser_cart import MouserCart

    wire = _Wire()
    monkeypatch.setattr(MouserCart, "_call",
                        lambda self, url, query, body=None: wire.call(
                            self, url, query, body))
    monkeypatch.setattr(MouserCart, "unorderable", lambda self, ready: [])
    key = "00000000-0000-0000-0000-000000000009"
    wire.carts[key] = {"MO-1": 10}
    p = mouser_proposal(25, cart_key=key)
    out = cart.commit_mouser(p, MouserCart(), cart.Journal(p))
    assert out["ok"] and out["verified"], out
    assert wire.carts[key]["MO-1"] == 25
    body = wire.sent[0]
    assert body["CartKey"] == key
    assert body["CartItems"] == [{"MouserPartNumber": "MO-1",
                                  "Quantity": 15}]     # the delta, nothing else
    for line in body["CartItems"]:
        assert set(line) <= {"MouserPartNumber", "Quantity",
                             "CustomerPartNumber", "PackagingChoice"}
    again = cart.commit_mouser(p, MouserCart(), cart.Journal(p))
    assert again["ok"] and len(wire.sent) == 1         # nothing added again


def test_make_client_returns_the_existing_clients():
    from eda_agent.libimport.providers.digikey_lists import DigiKeyLists
    from eda_agent.libimport.providers.mouser_cart import MouserCart
    assert isinstance(cart.make_client("mouser"), MouserCart)
    assert isinstance(cart.make_client("digikey"), DigiKeyLists)


# ---- journal and files (CART-15, TS-C19) ---------------------------------------------------------

def test_the_journal_is_under_the_export_folder_and_holds_no_credential(
        mouser, folder, monkeypatch):
    monkeypatch.setenv("MOUSER_CART_API_KEY", "sentinel-credential-123")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "sentinel-secret-456")
    mouser.carts[KEY] = {"mo-1": 10}
    p = mouser_proposal(25)
    out = commit(p, mouser)
    path = folder / "purchasing" / "journal" / f"{p['proposal_id']}.json"
    assert path.is_file() and out["journal"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert "sentinel" not in text and "sentinel" not in json.dumps(out)


def test_the_journal_refuses_a_folder_inside_the_repository(monkeypatch):
    monkeypatch.setenv("EDA_AGENT_EXPORT_DIR",
                       str(destination.repo_root() / "docs" / "exports"))
    with pytest.raises(destination.ExportPathError):
        cart.Journal(mouser_proposal())
    assert not (destination.repo_root() / "docs" / "exports").exists()


# ---- the tools (CART-2, CART-3, CART-14, TS-C02, TS-C03, TS-C18) ------------------------------------

def cart_tool_functions():
    captured = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    cart_tools.register_purchasing_cart_tools(_Capture())
    return captured


def run(name, **kw):
    kw.setdefault("project_path", "DemoBoard.PrjPcb")
    return asyncio.run(cart_tool_functions()[name](**kw))


def test_prepare_reads_only_and_writes_nothing(monkeypatch, folder):
    _stocked(monkeypatch)
    _altium(monkeypatch)

    def boom(distributor):
        raise AssertionError("prepare reached for a distributor client")

    monkeypatch.setattr(cart, "make_client", boom)
    out = run("part_purchasing_cart_prepare", distributor="digikey",
              list_id="list-1")
    assert out["ok"], out
    items = out["proposal"]["items"]
    # the capacitor needs review (DigiKey says "Murata Electronics", the BOM
    # "Murata": a partial match), so it is not in the proposal unless named
    assert {i["sku"] for i in items} == {
        "311-10.0KHRCT-ND", "296-LM358DRCT-ND"}
    assert all(i["status"] == "ready" for i in items)
    excluded = out["proposal"]["excluded"]
    assert any("unresolved" in e["why"] for e in excluded)
    cap = next(e for e in excluded if e["part_number"] == "GRM155R71C104KA88D")
    assert "needs review" in cap["why"] and cap["line_id"] in cap["why"]
    assert "MANUFACTURER_PARTIAL_MATCH" in cap["reasons"]
    assert out["proposal_id"] == out["proposal"]["proposal_id"]
    assert not (folder / "purchasing").exists()          # no journal
    # equal input, equal id
    again = run("part_purchasing_cart_prepare", distributor="digikey",
                list_id="list-1")
    assert again["proposal_id"] == out["proposal_id"]


def test_prepare_always_asks_the_distributors_afresh(monkeypatch):
    from eda_agent.libimport.providers import _cache
    seen = []
    real = _cache.fresh

    def spy(force=True):
        seen.append(force)
        return real(force)

    _stocked(monkeypatch)
    _altium(monkeypatch)
    monkeypatch.setattr(_cache, "fresh", spy)
    assert run("part_purchasing_cart_prepare", distributor="digikey",
               list_id="L")["ok"]
    assert seen and all(seen)


def test_commit_needs_approval_a_matching_id_and_changes_nothing_otherwise(
        monkeypatch, folder):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "x", "requested_part_number": "x",
                             "quantity": 1, "reference_designator": ""}]
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    prepared = run("part_purchasing_cart_prepare", distributor="digikey",
                   list_id="list-1")
    pid = prepared["proposal_id"]
    base = dict(distributor="digikey", list_id="list-1")

    refused = run("part_purchasing_cart_commit", proposal_id=pid, **base)
    assert refused["ok"] is False and "approved must be True" in refused["reason"]
    refused = run("part_purchasing_cart_commit", proposal_id=pid,
                  approved="yes", **base)
    assert refused["ok"] is False
    refused = run("part_purchasing_cart_commit", proposal_id="0" * 32,
                  approved=True, **base)
    assert refused["ok"] is False and refused["changed"] is True
    assert refused["new_proposal_id"] == pid
    refused = run("part_purchasing_cart_commit", proposal_id="",
                  approved=True, **base)
    assert refused["ok"] is False
    assert fake.calls == [] and not (folder / "purchasing").exists()


def test_commit_refuses_when_a_price_has_moved_since_prepare(monkeypatch):
    dk, mo = _stocked(monkeypatch)
    _altium(monkeypatch)
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "x", "requested_part_number": "x",
                             "quantity": 1, "reference_designator": ""}]
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    pid = run("part_purchasing_cart_prepare", distributor="digikey",
              list_id="list-1")["proposal_id"]
    for hit_ in dk.hits:                       # the distributor changes a price
        for offer in hit_.sourcing["offers"]:
            for step in offer["price_breaks"]:
                step["unit_price"] = step["unit_price"] * 1.5
    out = run("part_purchasing_cart_commit", proposal_id=pid, approved=True,
              distributor="digikey", list_id="list-1")
    assert out["ok"] is False and out["changed"] is True
    assert out["new_proposal_id"] != pid and out["proposal"]["items"]
    assert fake.calls == []                    # nothing written, nothing read


def test_commit_writes_the_approved_proposal_and_verifies_it(monkeypatch,
                                                              folder):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    fake = FakeLists()
    fake.lists["list-1"] = [{"sku": "x", "requested_part_number": "x",
                             "quantity": 1, "reference_designator": ""}]
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    args = dict(distributor="digikey", list_id="list-1")
    pid = run("part_purchasing_cart_prepare", **args)["proposal_id"]
    out = run("part_purchasing_cart_commit", proposal_id=pid, approved=True,
              **args)
    assert out["ok"] and out["verified"] and out["proposal_id"] == pid
    assert out["summary"]["added"] == 2
    skus = {p["sku"] for p in fake.lists["list-1"]}
    assert {"311-10.0KHRCT-ND", "296-LM358DRCT-ND"} <= skus
    assert "490-GRM155-CT-ND" not in skus               # review: not named
    assert (folder / "purchasing" / "journal" / f"{pid}.json").is_file()
    again = run("part_purchasing_cart_commit", proposal_id=pid, approved=True,
                **args)
    assert again["ok"] and again["summary"]["satisfied"] == 2
    assert len(fake.lists["list-1"]) == 3                       # no duplicates
    # naming the review line is an explicit choice, and a different proposal
    cap_id = next(e["line_id"] for e in run(
        "part_purchasing_cart_prepare", **args)["proposal"]["excluded"]
        if e["part_number"] == "GRM155R71C104KA88D")
    named = run("part_purchasing_cart_prepare", include_lines=[cap_id], **args)
    assert named["proposal_id"] != pid
    assert "490-GRM155-CT-ND" in {i["sku"] for i in named["proposal"]["items"]}
    third = run("part_purchasing_cart_commit",
                proposal_id=named["proposal_id"], approved=True,
                include_lines=[cap_id], **args)
    assert third["ok"] and third["summary"] == {
        "items": 3, "added": 1, "satisfied": 2, "conflict": 0,
        "over_target": 0, "short": 0, "not_in_list": 0, "verified": 3}


def test_a_mouser_commit_through_the_tools_is_additive_safe(monkeypatch):
    _stocked(monkeypatch)
    _altium(monkeypatch)
    fake = FakeMouser()
    key = "00000000-0000-0000-0000-000000000009"
    fake.carts[key] = {"zzz": 1}
    monkeypatch.setattr(cart, "make_client", lambda d: fake)
    args = dict(distributor="mouser", cart_key=key, prefer="mouser")
    prepared = run("part_purchasing_cart_prepare", **args)
    assert prepared["ok"] and prepared["proposal"]["items"]
    pid = prepared["proposal_id"]
    fake.lose_reply_after_apply = 1
    first = run("part_purchasing_cart_commit", proposal_id=pid,
                approved=True, **args)
    assert first["ok"] is False and first["outcome"] == "unknown"
    snapshot = dict(fake.carts[key])
    second = run("part_purchasing_cart_commit", proposal_id=pid,
                 approved=True, **args)
    assert second["ok"] and second["verified"]
    assert fake.carts[key] == snapshot                   # nothing doubled


def test_bad_targets_and_settings_are_refused_before_anything_is_read(
        monkeypatch):
    dk, mo = _stocked(monkeypatch)
    altium = _altium(monkeypatch)
    for kw in (dict(distributor="mouser"), dict(distributor="digikey"),
               dict(distributor="digikey", list_id="L", assemblies=0),
               dict(distributor="farnell")):
        out = run("part_purchasing_cart_prepare", **kw)
        assert out["ok"] is False and out["reason"]
    assert altium.sent == [] and dk.asked == [] and mo.asked == []


def test_the_tools_are_classified_and_never_order():
    from eda_agent.tools.metadata import READONLY, SILENT, interaction_of
    assert interaction_of("part_purchasing_cart_prepare") == READONLY
    assert interaction_of("part_purchasing_cart_commit") == SILENT
    assert interaction_of("part_purchasing_export_csv") == SILENT
    import inspect
    from eda_agent.export import purchasing_cart
    from eda_agent.tools import purchasing_cart as tool_module
    for module in (purchasing_cart, tool_module):
        source = inspect.getsource(module).lower()
        for word in ("checkout", "/order", "orderhistory", "payment"):
            # the words appear only to say they are never used
            assert f"client.{word}" not in source
            assert f"def {word}" not in source


def test_the_read_modules_still_name_no_cart_client():
    import inspect
    from eda_agent.export import (
        purchasing_bom_export,
        purchasing_config,
        purchasing_lines,
        purchasing_model,
        purchasing_quantity,
        purchasing_review,
    )
    for module in (purchasing_bom_export, purchasing_config, purchasing_lines,
                   purchasing_model, purchasing_quantity, purchasing_review):
        source = inspect.getsource(module)
        for word in ("mouser_cart", "digikey_lists", "create_cart",
                     "add_items", "create_list", "MouserCart",
                     "DigiKeyLists"):
            assert word not in source, (module.__name__, word)
