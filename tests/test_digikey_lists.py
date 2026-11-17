# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""DigiKey MyLists: sign-in, list creation, adding parts and read-back.

Everything here talks to a made-up DigiKey that keeps lists in memory.
The components are made up too. No request leaves the machine.
"""

import asyncio
import io
import json
import urllib.error
import urllib.parse

import pytest

from eda_agent.libimport.providers import digikey_lists
from eda_agent.libimport.providers.digikey_lists import (
    DigiKeyLists,
    ListInputError,
    join_designators,
)
from eda_agent.libimport.providers.base import (
    ProviderError,
    ProviderUnavailable,
)

_URLOPEN = "eda_agent.libimport.providers._distributor.urllib.request.urlopen"

#: The catalogue of the made-up DigiKey: DigiKey number to product.
_CATALOGUE = {
    "311-10.0KHRCT-ND": ("RC0603FR-0710KL", "YAGEO",
                         "RES 10K OHM 1% 1/10W 0603"),
    "311-10.0KHRTR-ND": ("RC0603FR-0710KL", "YAGEO",
                         "RES 10K OHM 1% 1/10W 0603"),
    "296-1395-1-ND": ("LM358DR", "Texas Instruments",
                      "IC OPAMP GP 2 CIRCUIT 8SOIC"),
}


class _Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _DigiKey:
    """A DigiKey that issues tokens and keeps MyLists in memory."""

    def __init__(self, monkeypatch):
        self.requests = []
        self.lists = {}
        self.codes = {"good-code"}
        self.refresh_tokens = set()
        self.access_tokens = set()
        self.issued = 0
        self.token_requests = []
        #: Queue of (status, payload) answered in place of a MyLists call.
        self.fail_next = []
        #: Characters DigiKey keeps of a customer reference; None is all.
        self.reference_limit = None
        self.quantity_multiple = 1
        self.rename_lists = False
        monkeypatch.setattr(_URLOPEN, self)

    # -- helpers the tests read --
    def calls(self, text, method=None):
        return [r for r in self.requests if text in r.full_url
                and method in (None, r.get_method())]

    def _fail(self, request, status, payload):
        raise urllib.error.HTTPError(
            request.full_url, status, "failed", {},
            io.BytesIO(json.dumps(payload).encode("utf-8")))

    def _tokens(self):
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.access_tokens = {access}
        self.refresh_tokens = {refresh}
        return {"access_token": access, "refresh_token": refresh,
                "expires_in": 1799, "refresh_token_expires_in": 7775999,
                "token_type": "Bearer"}

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = urllib.parse.urlsplit(request.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        body = request.data
        if url.path == "/v1/oauth2/token":
            form = dict(urllib.parse.parse_qsl(body.decode("ascii")))
            self.token_requests.append(form)
            if form["grant_type"] == "authorization_code":
                if form["code"] not in self.codes:
                    self._fail(request, 400, {"error": "invalid_grant"})
                self.codes.discard(form["code"])
                return _Reply(json.dumps(self._tokens()).encode())
            if form["grant_type"] == "refresh_token":
                if form["refresh_token"] not in self.refresh_tokens:
                    self._fail(request, 400, {"error": "invalid_grant"})
                return _Reply(json.dumps(self._tokens()).encode())
            if form["grant_type"] == "client_credentials":
                return _Reply(json.dumps({
                    "access_token": "two-legged", "expires_in": 599}).encode())
        if url.path.startswith("/products/v4/search/"):
            number = urllib.parse.unquote(url.path.split("/")[4])
            # A manufacturer number answers with its product, as the
            # real ProductDetails does.
            for sku, (mpn, maker, text) in _CATALOGUE.items():
                if number.casefold() in (sku.casefold(), mpn.casefold()):
                    variations = [
                        {"DigiKeyProductNumber": other,
                         "PackageType": {"Name": "Cut Tape (CT)"}}
                        for other, entry in _CATALOGUE.items()
                        if entry[0] == mpn]
                    return _Reply(json.dumps({"Product": {
                        "ManufacturerProductNumber": mpn,
                        "Manufacturer": {"Name": maker},
                        "Description": {"ProductDescription": text},
                        "ProductVariations": variations}}).encode())
            self._fail(request, 404, {"title": "Not Found",
                                      "detail": "The product was not found"})
        assert url.path.startswith("/mylists/v1/"), request.full_url
        token = request.get_header("Authorization", "")[len("Bearer "):]
        if token not in self.access_tokens:
            self._fail(request, 401, {
                "StatusCode": 401, "ErrorMessage": "Bearer token expired"})
        if self.fail_next:
            status, payload = self.fail_next.pop(0)
            self._fail(request, status, payload)
        steps = [urllib.parse.unquote(s)
                 for s in url.path[len("/mylists/v1/"):].split("/")]
        method = request.get_method()
        if steps == ["lists"] and method == "POST":
            wanted = json.loads(body)
            list_id = f"LIST{len(self.lists) + 1}"
            name = wanted["ListName"] + (" (2)" if self.rename_lists else "")
            self.lists[list_id] = {
                "Id": list_id, "ListName": name, "CanEdit": True,
                "ListSettings": wanted.get("ListSettings", {}), "parts": [],
                "request": wanted}
            return _Reply(json.dumps(list_id).encode())
        if steps == ["lists"] and method == "GET":
            return _Reply(json.dumps(
                [self._data(l) for l in self.lists.values()]).encode())
        held = self.lists.get(steps[1])
        if held is None:
            self._fail(request, 404, {
                "StatusCode": 404, "ErrorMessage": "List not found"})
        if len(steps) == 2 and method == "GET":
            return _Reply(json.dumps(self._data(held)).encode())
        if steps[2:] == ["parts"] and method == "GET":
            start, limit = int(query["startIndex"]), int(query["limit"])
            return _Reply(json.dumps({
                "PartsList": held["parts"][start:start + limit],
                "TotalParts": len(held["parts"])}).encode())
        if steps[2:] == ["parts"] and method == "POST":
            ids = []
            for wanted in json.loads(body):
                unique = f"U{sum(len(l['parts']) for l in self.lists.values()) + 1}"
                number = wanted["RequestedPartNumber"]
                known = _CATALOGUE.get(number)
                reference = wanted.get("CustomerReference", "")
                if self.reference_limit is not None:
                    reference = reference[:self.reference_limit]
                asked = wanted["Quantities"][0]["Quantity"]
                multiple = self.quantity_multiple
                held["parts"].append({
                    "UniqueId": unique,
                    "RequestedPartNumber": number,
                    "DigiKeyPartNumber": number if known else "",
                    "ManufacturerPartNumber": known[0] if known else "",
                    "Manufacturer": known[1] if known else "",
                    "Description": known[2] if known else "",
                    "CustomerReference": reference,
                    "ReferenceDesignator": wanted.get(
                        "ReferenceDesignator", ""),
                    "Notes": wanted.get("Notes", ""),
                    "SelectedQuantityIndex": 0,
                    "Quantities": [{
                        "QuantityRequested":
                            -(-asked // multiple) * multiple,
                        "SelectedPackType": "CutTape"}],
                    "Flags": {"IsMatched": bool(known)},
                    "sent": wanted,
                })
                ids.append(unique)
            return _Reply(json.dumps(ids).encode())
        raise AssertionError(f"unexpected {method} {request.full_url}")

    @staticmethod
    def _data(held):
        return {"Id": held["Id"], "ListName": held["ListName"],
                "ListSettings": held["ListSettings"],
                "TotalParts": len(held["parts"]), "CanEdit": True}


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("DIGIKEY_TOKEN_FILE", str(tmp_path / "tokens.json"))
    for var in ("DIGIKEY_REDIRECT_URI", "DIGIKEY_ACCOUNT_ID",
                "DIGIKEY_LOCALE_SITE", "DIGIKEY_LOCALE_LANGUAGE",
                "DIGIKEY_LOCALE_CURRENCY"):
        monkeypatch.delenv(var, raising=False)
    digikey_lists._PENDING_STATES.clear()
    from eda_agent.libimport.providers import distributors
    distributors._DIGIKEY_TOKENS.clear()


@pytest.fixture
def digikey(monkeypatch):
    return _DigiKey(monkeypatch)


def _signed_in(digikey):
    lists = DigiKeyLists()
    lists.finish_login("good-code")
    return lists


def _item(sku="311-10.0KHRCT-ND", quantity=3, designators=("R1", "R2", "R3"),
          **more):
    return {"sku": sku, "quantity": quantity,
            "designators": list(designators),
            "description": "RES 10K 1% 0603", **more}


# ---- signing in -------------------------------------------------------------

def test_the_sign_in_address_names_the_app_and_its_redirect(digikey):
    start = DigiKeyLists().authorize_url()
    address = urllib.parse.urlsplit(start["authorize_url"])
    query = dict(urllib.parse.parse_qsl(address.query))
    assert address.netloc == "api.digikey.com"
    assert address.path == "/v1/oauth2/authorize"
    assert query["response_type"] == "code"
    assert query["client_id"] == "test-client-id"
    assert query["redirect_uri"] == "https://localhost" == start["redirect_uri"]
    assert len(query["state"]) >= 20
    assert "test-client-secret" not in start["authorize_url"]
    assert digikey.requests == [], "nothing is sent to make the address"


def test_the_returned_address_is_exchanged_and_the_sign_in_kept(
        digikey, monkeypatch):
    monkeypatch.setenv("DIGIKEY_REDIRECT_URI", "https://localhost:8139/cb")
    lists = DigiKeyLists()
    start = lists.authorize_url()
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        start["authorize_url"]).query))["state"]
    status = lists.finish_login(
        f"https://localhost:8139/cb?code=good-code&state={state}")
    assert status["signed_in"] is True and status["sign_in_lasts_days"] == 89
    assert digikey.token_requests == [{
        "client_id": "test-client-id", "client_secret": "test-client-secret",
        "grant_type": "authorization_code", "code": "good-code",
        "redirect_uri": "https://localhost:8139/cb"}]
    # A new client object, as every tool call builds, is still signed in.
    assert DigiKeyLists().status()["signed_in"] is True


def test_an_address_from_another_sign_in_is_refused(digikey):
    lists = DigiKeyLists()
    lists.authorize_url()
    with pytest.raises(ProviderUnavailable, match="does not belong"):
        lists.finish_login("https://localhost/?code=good-code&state=forged")
    with pytest.raises(ProviderUnavailable, match="does not belong"):
        lists.finish_login("https://localhost/?code=good-code")
    assert digikey.token_requests == [], "the code was never sent"


def test_access_denied_and_a_spent_code_say_what_to_do(digikey):
    lists = DigiKeyLists()
    with pytest.raises(ProviderUnavailable, match="did not grant access"):
        lists.finish_login("https://localhost/?error=access_denied&state=x")
    with pytest.raises(ProviderUnavailable, match="one minute"):
        lists.finish_login("spent-code")
    assert lists.status() == {"signed_in": False}


def test_no_token_or_secret_is_ever_returned_or_left_readable(
        digikey, tmp_path):
    lists = _signed_in(digikey)
    created = lists.create_list("Bench build")
    added = lists.add_items(created["list_id"], [_item()])
    shown = json.dumps([lists.status(), created, added,
                        lists.get_list(created["list_id"]), lists.lists()])
    for secret in ("access-1", "refresh-1", "test-client-secret"):
        assert secret not in shown
    stored = (tmp_path / "tokens.json").read_text("utf-8")
    assert "test-client-secret" not in stored
    import os
    if os.name == "nt":
        assert json.loads(stored)["sealed"] == "dpapi"
        import base64
        assert b"refresh-1" not in base64.b64decode(json.loads(stored)["data"])


def test_without_a_sign_in_the_call_says_to_sign_in(digikey):
    with pytest.raises(ProviderUnavailable, match="part_list_login"):
        DigiKeyLists().lists()
    assert digikey.requests == []


def test_missing_credentials_are_named(monkeypatch, digikey):
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET")
    with pytest.raises(ProviderUnavailable, match="DIGIKEY_CLIENT_SECRET"):
        DigiKeyLists().authorize_url()


# ---- tokens that run out ----------------------------------------------------

def test_an_expired_access_token_is_refreshed_and_the_new_one_kept(
        digikey, monkeypatch):
    lists = _signed_in(digikey)
    lists.create_list("One")
    real = digikey_lists.time.time
    monkeypatch.setattr(digikey_lists.time, "time", lambda: real() + 1800)
    assert len(DigiKeyLists().lists()) == 1
    assert [t["grant_type"] for t in digikey.token_requests] == [
        "authorization_code", "refresh_token"]
    assert digikey.token_requests[1]["refresh_token"] == "refresh-1"
    # The refresh replaced the refresh token; the next one uses the new.
    monkeypatch.setattr(digikey_lists.time, "time", lambda: real() + 3700)
    assert len(DigiKeyLists().lists()) == 1
    assert digikey.token_requests[2]["refresh_token"] == "refresh-2"


def test_a_token_digikey_refuses_early_is_renewed_once(digikey):
    lists = _signed_in(digikey)
    digikey.access_tokens = set()          # revoked on DigiKey's side
    assert lists.lists() == []
    assert len(digikey.calls("/mylists/v1/lists")) == 2
    assert digikey.token_requests[-1]["grant_type"] == "refresh_token"


def test_a_sign_in_that_has_lapsed_asks_for_a_new_one(digikey, monkeypatch):
    lists = _signed_in(digikey)
    digikey.access_tokens, digikey.refresh_tokens = set(), set()
    with pytest.raises(ProviderUnavailable, match="Sign in again"):
        lists.lists()


def test_a_refusal_with_a_fresh_token_names_the_subscription(digikey):
    lists = _signed_in(digikey)
    digikey.fail_next = [(403, {"StatusCode": 403,
                                "ErrorMessage": "Forbidden"})]
    with pytest.raises(ProviderUnavailable) as caught:
        lists.lists()
    assert "subscribed to the MyLists API" in str(caught.value)
    assert "Forbidden" in str(caught.value)


def test_a_rate_limit_and_a_server_error_are_reported_as_such(digikey):
    lists = _signed_in(digikey)
    digikey.fail_next = [(429, {"ErrorMessage": "BurstLimit exceeded"})]
    with pytest.raises(ProviderUnavailable, match="rate-limited"):
        lists.lists()
    digikey.fail_next = [(500, {"StatusCode": 500,
                                "ErrorMessage": "Something broke"})]
    with pytest.raises(ProviderError, match="HTTP 500: Something broke"):
        lists.lists()


# ---- creating a list --------------------------------------------------------

def test_a_list_is_created_private_and_set_to_keep_what_is_entered(
        digikey, monkeypatch):
    monkeypatch.setenv("DIGIKEY_ACCOUNT_ID", "12345")
    created = _signed_in(digikey).create_list("  Bench   build ")
    assert created["list_id"] == "LIST1" and created["name"] == "Bench build"
    assert created["visibility"] == "Private"
    assert "differences" not in created
    assert digikey.lists["LIST1"]["request"] == {
        "ListName": "Bench build", "Source": "external",
        "ListSettings": {"Visibility": "Private",
                         "PackagePreference": "EnteredPartNumber",
                         "AutoCorrectQuantities": False,
                         "AttritionEnabled": False,
                         "AutoPopulateCref": False}}
    post = digikey.calls("/mylists/v1/lists", "POST")[0]
    assert post.get_header("X-digikey-client-id") == "test-client-id"
    assert post.get_header("X-digikey-account-id") == "12345"


def test_a_name_digikey_changed_is_reported(digikey):
    digikey.rename_lists = True
    created = _signed_in(digikey).create_list("Bench build")
    assert created["name"] == "Bench build (2)"
    assert created["differences"] == [
        "DigiKey stored the name as 'Bench build (2)', not 'Bench build'"]


def test_a_list_needs_a_name_and_a_known_visibility(digikey):
    lists = _signed_in(digikey)
    with pytest.raises(ListInputError, match="needs a name"):
        lists.create_list("   ")
    with pytest.raises(ListInputError, match="private, readonly"):
        lists.create_list("X", visibility="public")
    assert digikey.calls("/mylists/v1/") == []


# ---- adding parts -------------------------------------------------------------

def test_designators_are_joined_whole():
    assert join_designators(["R1", " R2 ", "r1", "", "R10"]) == "R1,R2,R10"
    assert join_designators("R1, R2  and R3") == "R1, R2 and R3"
    assert join_designators(None) == ""
    many = [f"C{n}" for n in range(1, 61)]
    assert join_designators(many).split(",") == many


def test_parts_are_added_with_sku_quantity_and_both_references(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [
        _item(),
        _item("296-1395-1-ND", 2, ["U1", "U2"],
              description="IC OPAMP DUAL SOIC-8",
              manufacturer="Texas Instruments"),
    ])
    assert (out["sent"], out["added"], out["verified"]) == (2, 2, 2)
    assert out["list_total"] == 2
    sent = [p["sent"] for p in digikey.lists[list_id]["parts"]]
    assert sent[0] == {
        "RequestedPartNumber": "311-10.0KHRCT-ND",
        "CustomerReference": "R1,R2,R3", "ReferenceDesignator": "R1,R2,R3",
        "Quantities": [{"Quantity": 3}], "SelectedQuantityIndex": 0,
        "Notes": "RES 10K 1% 0603"}
    assert sent[1]["ManufacturerName"] == "Texas Instruments"
    assert out["items"][1] == {
        "item": 1, "sku": "296-1395-1-ND", "quantity": 2,
        "customer_reference": "U1,U2", "reference_designator": "U1,U2",
        "description": "IC OPAMP DUAL SOIC-8", "unique_id": "U2",
        "digikey_description": "IC OPAMP GP 2 CIRCUIT 8SOIC",
        "differences": []}


def test_the_two_references_can_be_set_apart_from_the_designators(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item(
        customer_reference="Bag 7", reference_designator="")])
    part = digikey.lists[list_id]["parts"][0]
    assert part["CustomerReference"] == "Bag 7"
    assert part["ReferenceDesignator"] == ""
    assert out["items"][0]["differences"] == []


def test_the_list_reads_back_what_was_added(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    lists.add_items(list_id, [_item()])
    got = lists.get_list(list_id)
    assert (got["name"], got["count"], got["visibility"]) == (
        "Bench build", 1, "Private")
    assert got["items"] == [{
        "unique_id": "U1", "requested_part_number": "311-10.0KHRCT-ND",
        "sku": "311-10.0KHRCT-ND",
        "manufacturer_part_number": "RC0603FR-0710KL",
        "manufacturer": "YAGEO", "matched": True, "quantity": 3,
        "calculated_quantity": None, "pack_type": "CutTape",
        "quantities": [3], "customer_reference": "R1,R2,R3",
        "reference_designator": "R1,R2,R3", "notes": "RES 10K 1% 0603",
        "digikey_description": "RES 10K OHM 1% 1/10W 0603",
        "status": None, "quantity_available": None}]
    assert [l["list_id"] for l in lists.lists()] == ["LIST1"]


def test_a_long_reference_is_sent_whole_and_warned_about(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    names = [f"C{n}" for n in range(101, 141)]
    out = lists.add_items(list_id, [_item(quantity=40, designators=names)])
    whole = ",".join(names)
    assert len(whole) > 48
    assert digikey.lists[list_id]["parts"][0]["sent"][
        "CustomerReference"] == whole
    row = out["items"][0]
    assert row["customer_reference"] == whole and row["differences"] == []
    assert f"{len(whole)} characters" in row["label_warning"]
    assert "sent whole" in row["label_warning"]


def test_a_reference_digikey_cut_short_is_reported_never_hidden(digikey):
    digikey.reference_limit = 20
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    names = [f"R{n}" for n in range(1, 13)]
    out = lists.add_items(list_id, [_item(quantity=12, designators=names)])
    whole = ",".join(names)
    assert out["verified"] == 0
    assert out["items"][0]["differences"] == [
        f"customer reference: sent {whole!r} ({len(whole)} characters), "
        f"list holds {whole[:20]!r} (20)"]
    assert out["items"][0]["reference_designator"] == whole


def test_a_quantity_digikey_changed_is_reported(digikey):
    digikey.quantity_multiple = 10
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item()])
    assert out["items"][0]["differences"] == [
        "quantity: sent 3, list holds 10"]


def test_malformed_items_are_refused_whole_before_anything_is_sent(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    with pytest.raises(ListInputError) as caught:
        lists.add_items(list_id, [
            _item(),
            _item(sku="  "),
            _item(quantity=0),
            _item(quantity="3"),
            _item(quantity=True),
            _item(quantity=2.5),
            _item(comment="10k pull-up"),
            "311-10.0KHRCT-ND",
        ])
    problems = {p["item"]: " ".join(p["problems"])
                for p in caught.value.problems}
    assert sorted(problems) == [1, 2, 3, 4, 5, 6, 7]
    assert "no sku" in problems[1]
    assert "not a whole number" in problems[2] and "'3'" in problems[3]
    assert "unknown field(s) 'comment'" in problems[6]
    assert "nothing was added" in str(caught.value)
    assert digikey.lists[list_id]["parts"] == []
    assert digikey.calls("/parts") == [] and digikey.calls("/products/") == []
    for bad in ([], None, {"sku": "X"}):
        with pytest.raises(ListInputError):
            lists.add_items(list_id, bad)


def test_a_number_that_is_not_a_digikey_number_adds_nothing(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    with pytest.raises(ListInputError) as caught:
        lists.add_items(list_id, [
            _item(),
            _item("NO-SUCH-PART-ND"),
            _item("RC0603FR-0710KL"),      # a manufacturer number
            _item("no-such-part-nd"),
        ])
    problems = {p["item"]: p["problems"][0] for p in caught.value.problems}
    assert sorted(problems) == [1, 2, 3]
    assert problems[1] == "DigiKey has no product with this number"
    assert "311-10.0KHRCT-ND, 311-10.0KHRTR-ND" in problems[2]
    assert digikey.lists[list_id]["parts"] == []
    assert digikey.calls("/parts", "POST") == []
    # One catalogue read per distinct number, whatever its case.
    assert len(digikey.calls("/products/")) == 3


def test_an_unmatched_part_is_reported_when_the_check_is_off(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item("NO-SUCH-PART-ND")],
                          check_products=False)
    assert digikey.calls("/products/") == []
    assert out["added"] == 1 and out["verified"] == 0
    assert out["items"][0]["differences"] == [
        "DigiKey did not match this number to a product",
        "sku: sent 'NO-SUCH-PART-ND', list holds ''"]


def test_a_batch_digikey_refuses_says_what_was_and_was_not_sent(
        digikey, monkeypatch):
    monkeypatch.setattr(digikey_lists, "_ADD_BATCH", 2)
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    items = [_item(designators=[f"R{n}"], quantity=1) for n in range(1, 6)]
    # Batch one goes through; DigiKey answers batch two with a 400.
    real = digikey.__call__
    posts = []

    def flaky(request, timeout=None):
        if request.get_method() == "POST" and "/parts" in request.full_url:
            posts.append(request)
            if len(posts) == 2:
                digikey.fail_next = [(400, {
                    "StatusCode": 400, "ErrorMessage": "Bad part"})]
        return real(request, timeout)

    monkeypatch.setattr(_URLOPEN, flaky)
    out = lists.add_items(list_id, items, check_products=False)
    assert (out["sent"], out["added"], out["verified"]) == (2, 2, 2)
    assert "after 2 of 5 items were sent" in out["failed"]
    assert "Bad part" in out["failed"]
    assert [n["item"] for n in out["not_sent"]] == [2, 3, 4]


def test_a_large_list_is_read_in_pages_and_added_in_batches(
        digikey, monkeypatch):
    monkeypatch.setattr(digikey_lists, "_ADD_BATCH", 4)
    monkeypatch.setattr(digikey_lists, "_READ_PAGE", 3)
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    items = [_item(designators=[f"R{n}"], quantity=n) for n in range(1, 11)]
    out = lists.add_items(list_id, items)
    assert (out["sent"], out["added"], out["verified"]) == (10, 10, 10)
    assert len(digikey.calls("/parts", "POST")) == 3
    got = lists.get_list(list_id)
    assert [i["quantity"] for i in got["items"]] == list(range(1, 11))
    assert [i["reference_designator"] for i in got["items"]] == [
        f"R{n}" for n in range(1, 11)]


def test_a_list_that_does_not_exist_is_an_error_not_an_empty_list(digikey):
    lists = _signed_in(digikey)
    with pytest.raises(ProviderError, match="HTTP 404: List not found"):
        lists.get_list("NOPE")
    with pytest.raises(ProviderError, match="HTTP 404"):
        lists.add_items("NOPE", [_item()])


# ---- the tools ------------------------------------------------------------------

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


def test_the_tools_sign_in_create_add_and_read_back(digikey):
    tools = _tools()
    first = asyncio.run(tools["part_list_login"]())
    assert first["ok"] and first["signed_in"] is False
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        first["authorize_url"]).query))["state"]
    done = asyncio.run(tools["part_list_login"](
        returned_url=f"https://localhost/?code=good-code&state={state}"))
    assert done == {"ok": True, "provider": "digikey", "signed_in": True,
                    "sign_in_lasts_days": 89,
                    "note": "each use renews the sign-in for 90 days"}
    again = asyncio.run(tools["part_list_login"]())
    assert again["signed_in"] is True and "authorize_url" not in again

    made = asyncio.run(tools["part_list_create"](name="Bench build"))
    assert made["ok"] and made["visibility"] == "Private"
    added = asyncio.run(tools["part_list_add"](
        list_id=made["list_id"], items=[_item()]))
    assert added["ok"] and added["verified"] == 1
    got = asyncio.run(tools["part_list_get"](list_id=made["list_id"]))
    assert got["items"][0]["customer_reference"] == "R1,R2,R3"
    assert got["items"][0]["quantity"] == 3
    every = asyncio.run(tools["part_list_get"]())
    assert every["count"] == 1 and every["lists"][0]["name"] == "Bench build"


def test_the_tools_answer_failures_without_raising(digikey):
    tools = _tools()
    out = asyncio.run(tools["part_list_get"]())
    assert out["ok"] is False and "part_list_login" in out["unavailable"]
    out = asyncio.run(tools["part_list_create"](name="X", provider="mouser"))
    assert out["ok"] is False and "only digikey" in out["reason"]
    _signed_in(digikey)
    made = asyncio.run(tools["part_list_create"](name="Bench build"))
    out = asyncio.run(tools["part_list_add"](
        list_id=made["list_id"], items=[_item(quantity=0)]))
    assert out["ok"] is False and out["problems"][0]["item"] == 0
    out = asyncio.run(tools["part_list_get"](list_id="NOPE"))
    assert out["ok"] is False and "404" in out["reason"]


def test_nothing_here_orders_or_reaches_a_cart():
    """The module's only DigiKey paths are OAuth and MyLists."""
    import inspect

    source = inspect.getsource(digikey_lists).lower()
    for word in ("/ordering", "/orderdetails", "cart", "checkout",
                 "/quoting"):
        assert word not in source.replace("a cart, a quote", ""), word


def test_a_rejected_input_names_the_field_digikey_objected_to(digikey):
    """The message alone ("Error(s) in Parameters.") names nothing; the
    details and the validation errors beside it do."""
    lists = _signed_in(digikey)
    digikey.fail_next = [(400, {
        "StatusCode": 400, "ErrorMessage": "Error(s) in Parameters.",
        "ErrorDetails": "The input model is invalid",
        "RequestId": "22da819d-9517-4a2c-a8c1-0195ea2e7878",
        "ValidationErrors": [
            {"Field": "ListSettings.PackagePreference",
             "Message": "The value is not valid."},
            {"Field": "X-DIGIKEY-Account-Id", "Message": "is required"}]})]
    with pytest.raises(ProviderError) as caught:
        lists.create_list("Bench build")
    text = str(caught.value)
    assert "HTTP 400: Error(s) in Parameters.; The input model is invalid" \
        in text
    assert "ListSettings.PackagePreference: The value is not valid." in text
    assert "X-DIGIKEY-Account-Id: is required" in text
    assert "access-1" not in text and "test-client-secret" not in text
    assert digikey.lists == {}
