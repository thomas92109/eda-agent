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
    fit_customer_reference,
    is_designator_list,
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
        #: False: a part comes back with no pack options at all.
        self.pack_options = True
        #: True: DigiKey selects the reel whatever number was entered.
        self.select_reel = False
        #: True: every page of lists is the first page.
        self.ignore_start_index = False
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
            # As the real MyLists answers: its Swagger file calls
            # nothing in ListSettings required, the service does.
            if not isinstance((wanted.get("ListSettings") or {})
                              .get("ColumnPreferences"), list):
                self._fail(request, 400, {
                    "StatusCode": 400,
                    "ErrorMessage": "Error(s) in Parameters.",
                    "ValidationErrors": [{
                        "Field": "ListSettings.ColumnPreferences",
                        "Message": "The ColumnPreferences field is "
                                   "required."}]})
            list_id = f"LIST{len(self.lists) + 1}"
            name = wanted["ListName"] + (" (2)" if self.rename_lists else "")
            self.lists[list_id] = {
                "Id": list_id, "ListName": name, "CanEdit": True,
                "ListSettings": wanted.get("ListSettings", {}), "parts": [],
                "request": wanted}
            return _Reply(json.dumps(list_id).encode())
        if steps == ["lists"] and method == "GET":
            start, limit = int(query["startIndex"]), int(query["limit"])
            if self.ignore_start_index:
                start = 0
            every = [self._data(l) for l in self.lists.values()]
            return _Reply(json.dumps(every[start:start + limit]).encode())
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
                # As the real MyLists answers: the part's own number is
                # the product's base number (the reel when there is
                # one); the packaging entered is the selected option.
                packs = [sku for sku, entry in _CATALOGUE.items()
                         if known and entry[0] == known[0]]
                reel = next((s for s in packs if s.endswith("TR-ND")), None)
                base = (reel or number) if known else ""
                options = [{"DigiKeyPartNumber": sku,
                            "PackType": "TR" if sku == reel else "CT"}
                           for sku in packs] if self.pack_options else []
                selected = -1
                if options:
                    selected = packs.index(
                        reel if self.select_reel and reel else number)
                held["parts"].append({
                    "UniqueId": unique,
                    "RequestedPartNumber": number,
                    "DigiKeyPartNumber": base,
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
                        "SelectedPackType": "",
                        "SelectedPackOptionIndex": selected,
                        "PackOptions": options}],
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
                         "ColumnPreferences": [],
                         "AutoCorrectQuantities": False,
                         "AttritionEnabled": False,
                         "AutoPopulateCref": False}}
    post = digikey.calls("/mylists/v1/lists", "POST")[0]
    assert post.get_header("X-digikey-client-id") == "test-client-id"
    assert post.get_header("X-digikey-account-id") == "12345"


def test_a_list_is_created_with_the_column_preferences_digikey_requires(
        digikey):
    """Live, 2026-11-16: a create without them was refused with
    "ListSettings.ColumnPreferences: The ColumnPreferences field is
    required." It is an array of {ColumnName, IsVisible}."""
    created = _signed_in(digikey).create_list("Bench build")
    assert created["list_id"] == "LIST1"
    sent = json.loads(digikey.calls("/mylists/v1/lists", "POST")[0].data)
    columns = sent["ListSettings"]["ColumnPreferences"]
    assert isinstance(columns, list)
    assert all(set(c) == {"ColumnName", "IsVisible"} for c in columns)


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
        "designators": "U1,U2",
        "customer_reference": "U1,U2", "reference_designator": "U1,U2",
        "description": "IC OPAMP DUAL SOIC-8", "unique_id": "U2",
        "digikey_description": "IC OPAMP GP 2 CIRCUIT 8SOIC",
        "sku_source": digikey_lists._SKU_FIELD,
        "differences": [], "sku_verified": True, "verified": True}


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
        "sku_source": "Quantities[SelectedQuantityIndex].PackOptions"
                      "[SelectedPackOptionIndex].DigiKeyPartNumber",
        "base_part_number": "311-10.0KHRTR-ND",
        "manufacturer_part_number": "RC0603FR-0710KL",
        "manufacturer": "YAGEO", "matched": True, "quantity": 3,
        "calculated_quantity": None, "pack_type": "CT",
        "quantities": [3], "customer_reference": "R1,R2,R3",
        "reference_designator": "R1,R2,R3", "notes": "RES 10K 1% 0603",
        "digikey_description": "RES 10K OHM 1% 1/10W 0603",
        "status": None, "quantity_available": None}]
    assert [l["list_id"] for l in lists.lists()] == ["LIST1"]


# ---- the exact orderable number ---------------------------------------------

def test_the_cut_tape_number_is_read_from_the_selected_pack_option(digikey):
    """Live, 2026-11-16: 311-10.0KHRCT-ND was added and the part's own
    DigiKeyPartNumber came back as the reel, 311-10.0KHRTR-ND, with the
    cut tape as the selected pack option. That was reported as a
    difference; it is what was asked for."""
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item()])
    held = digikey.lists[list_id]["parts"][0]
    assert held["DigiKeyPartNumber"] == "311-10.0KHRTR-ND"
    row = out["items"][0]
    assert row["differences"] == [] and row["sku_verified"] is True
    assert row["sku_source"] == (
        "Quantities[SelectedQuantityIndex].PackOptions"
        "[SelectedPackOptionIndex].DigiKeyPartNumber")
    assert out["verified"] == 1
    part = lists.get_list(list_id)["items"][0]
    assert part["sku"] == "311-10.0KHRCT-ND"
    assert part["base_part_number"] == "311-10.0KHRTR-ND"
    assert part["pack_type"] == "CT"


def test_another_packaging_selected_is_a_difference(digikey):
    digikey.select_reel = True
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item()])
    row = out["items"][0]
    assert row["differences"] == [
        "sku: sent '311-10.0KHRCT-ND', the packaging selected in the list "
        "is '311-10.0KHRTR-ND'"]
    assert row["sku_verified"] is False and out["verified"] == 0


def test_no_selected_packaging_is_never_verified(digikey):
    """The base number alone establishes nothing, even when it happens
    to be the number that was sent."""
    digikey.pack_options = False
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item(), _item("311-10.0KHRTR-ND")])
    assert out["added"] == 2 and out["verified"] == 0
    for row in out["items"]:
        assert row["differences"] == [] and row["sku_verified"] is False
        assert row["verified"] is False
        assert "'311-10.0KHRTR-ND'" in row["sku_unverified"]
        assert "sku_source" not in row
    part = lists.get_list(list_id)["items"][1]
    assert part["sku"] == "" and part["sku_source"] is None
    assert part["base_part_number"] == "311-10.0KHRTR-ND"


@pytest.mark.parametrize("index", [None, -1, 7, "x"])
def test_a_pack_option_index_that_points_nowhere_selects_nothing(index):
    summary = digikey_lists._part_summary({
        "DigiKeyPartNumber": "311-10.0KHRTR-ND",
        "Quantities": [{"QuantityRequested": 10,
                        "SelectedPackOptionIndex": index,
                        "PackOptions": [
                            {"DigiKeyPartNumber": "311-10.0KHRCT-ND"}]}]})
    assert summary["sku"] == "" and summary["sku_source"] is None


# ---- the customer reference -------------------------------------------------

_TEN = "R101, R102, R103, R104, R105, R106, R107, R108, R109, R110"
_EIGHT = "R101,R102,R103,R104,R105,R106,R107,R108"


def test_a_customer_reference_that_fits_is_sent_whole_without_spaces():
    assert fit_customer_reference("R1,R2") == ("R1,R2", [])
    assert fit_customer_reference("R1, R2, R3") == ("R1,R2,R3", [])
    assert fit_customer_reference("R1 R2  R3") == ("R1,R2,R3", [])
    assert fit_customer_reference(["R1", " R2 "]) == ("R1,R2", [])
    assert fit_customer_reference(None) == ("", [])
    # Exactly 48 characters once the spaces are gone is a fit: 56 as
    # written, and every designator is on the label.
    written = _EIGHT.replace(",", ", ") + ", R1090000"
    assert len(written) == 56
    exact = _EIGHT + ",R1090000"
    assert len(exact) == 48
    assert fit_customer_reference(written) == (exact, [])
    assert fit_customer_reference(exact.split(",")) == (exact, [])


def test_an_overflowing_reference_ends_in_more():
    # One character over: the last designator goes and +MORE says so.
    over = _EIGHT + ",R10900000"
    assert len(over) == 49
    assert fit_customer_reference(over) == (
        _EIGHT + ",+MORE", ["R10900000"])
    # However it was written, the label reads the same.
    names = [f"R{n}" for n in range(101, 111)]
    for given in (names, ",".join(names), ", ".join(names),
                  " ".join(names)):
        assert fit_customer_reference(given) == (
            "R101,R102,R103,R104,R105,R106,R107,R108,+MORE",
            ["R109", "R110"])


def test_room_for_more_can_cost_a_designator_that_would_have_fit():
    """R109 fits in 48 characters, but not with ",+MORE" after it: it
    is left out and named, rather than the ending being squeezed."""
    nine = _EIGHT + ",R109"
    assert len(nine) <= 48 and len(nine + ",+MORE") > 48
    got, omitted = fit_customer_reference(_TEN)
    assert got == _EIGHT + ",+MORE" and len(got) == 45
    assert omitted == ["R109", "R110"]


def test_a_single_designator_too_long_is_never_cut():
    assert fit_customer_reference(["X" * 49]) == ("+MORE", ["X" * 49])
    assert fit_customer_reference(["X" * 49, "R1"]) == (
        "+MORE", ["X" * 49, "R1"])
    # 45 characters fit alone, but not once others follow and +MORE is
    # needed; the reference is then +MORE and nothing else.
    assert fit_customer_reference(["X" * 45]) == ("X" * 45, [])
    assert fit_customer_reference(["X" * 45, "R12"]) == (
        "+MORE", ["X" * 45, "R12"])


def test_a_fitted_reference_keeps_its_promises_whatever_the_list():
    for separator in (",", ", ", " "):
        for width in (1, 2, 3, 5, 9):
            for count in range(1, 40):
                names = [f"R{str(n).zfill(width)}" for n in range(count)]
                compact = ",".join(names)
                got, omitted = fit_customer_reference(separator.join(names))
                assert len(got) <= 48 and " " not in got
                if len(compact) <= 48:
                    assert (got, omitted) == (compact, [])
                    continue
                assert got.endswith(",+MORE") and omitted
                kept = got[:-len(",+MORE")]
                assert kept.split(",") + omitted == names
                assert not kept.endswith(",")


def test_a_long_reference_keeps_whole_designators_and_names_the_rest(
        digikey):
    """Live, 2026-11-16: DigiKey stored 48 of 58 characters and ended
    the reference in the middle of the list, with nothing to show it."""
    digikey.reference_limit = 48
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(
        list_id, [{**_item(quantity=10), "designators": _TEN}])
    sent = digikey.lists[list_id]["parts"][0]["sent"]
    assert sent["CustomerReference"] == _EIGHT + ",+MORE"
    # The reference designator is the text as given, spaces and all.
    assert sent["ReferenceDesignator"] == _TEN
    row = out["items"][0]
    assert row["designators"] == _TEN
    assert row["reference_designator"] == _TEN
    assert row["customer_reference"] == sent["CustomerReference"]
    assert row["customer_reference_omitted"] == ["R109", "R110"]
    assert "2 designator(s) were left out of it: R109, R110" in \
        row["customer_reference_warning"]
    assert "+MORE" in row["customer_reference_warning"]
    assert row["differences"] == [] and out["verified"] == 1
    held = lists.get_list(list_id)["items"][0]
    assert held["customer_reference"] == sent["CustomerReference"]
    assert held["reference_designator"] == _TEN


def test_a_reference_that_fits_carries_no_warning(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    row = lists.add_items(list_id, [
        {**_item(), "designators": "R1, R2, R3"}])["items"][0]
    assert row["designators"] == "R1, R2, R3"
    assert row["reference_designator"] == "R1, R2, R3"
    assert row["customer_reference"] == "R1,R2,R3"
    assert "customer_reference_omitted" not in row
    assert "customer_reference_warning" not in row
    assert row["differences"] == []


def test_what_counts_as_a_designator_list():
    for text in ("R1", "R1,R2", "R101, C5, U3A, TP12", "R1 R2 R3", _TEN):
        assert is_designator_list(text), text
    for text in ("", "Bag 7", "Bench build bag 7, second shelf", "R1-R5",
                 "R1, R2 and R3", "PO 4471, R1", "X" * 49, "10k"):
        assert not is_designator_list(text), text


def test_a_customer_reference_given_as_designators_is_fitted(digikey):
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    out = lists.add_items(list_id, [_item(
        customer_reference=_TEN, reference_designator="R1,R2,R3")])
    row = out["items"][0]
    assert row["customer_reference"] == _EIGHT + ",+MORE"
    assert row["customer_reference_omitted"] == ["R109", "R110"]
    assert row["reference_designator"] == "R1,R2,R3"
    assert row["differences"] == []


def test_a_customer_reference_in_the_users_words_is_kept_or_refused(
        digikey):
    """Words of the user's own are not reworded: 48 characters go as
    they are, 49 are refused and nothing is sent."""
    lists = _signed_in(digikey)
    list_id = lists.create_list("Bench build")["list_id"]
    fits = "Bench build bag 7, second shelf, left drawer, #2"
    assert len(fits) == 48
    row = lists.add_items(
        list_id, [_item(customer_reference=fits)])["items"][0]
    assert row["customer_reference"] == fits
    assert "customer_reference_omitted" not in row
    assert digikey.lists[list_id]["parts"][0]["CustomerReference"] == fits
    posts = len(digikey.calls("/parts", "POST"))
    for long in (fits + "2", "X" * 49, "R1-R40, see the build sheet for "
                 "the rest of this assembly"):
        with pytest.raises(ListInputError) as caught:
            lists.add_items(list_id, [_item(customer_reference=long)])
        problem = caught.value.problems[0]["problems"][0]
        assert f"customer_reference is {len(long)} characters" in problem
        assert "DigiKey keeps 48" in problem
        assert "not a list of reference designators" in problem
    assert len(digikey.calls("/parts", "POST")) == posts
    assert len(digikey.lists[list_id]["parts"]) == 1


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
        "DigiKey did not match this number to a product"]
    assert out["items"][0]["sku_verified"] is False
    assert "no selected packaging" in out["items"][0]["sku_unverified"]


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


# ---- every list of the account ----------------------------------------------

def _many_lists(digikey, count):
    lists = _signed_in(digikey)
    for number in range(count):
        lists.create_list(f"List {number}")
    return lists


def test_every_list_is_read_not_the_first_fifty(digikey, monkeypatch):
    """Live, 2026-11-16: the account held 84 lists and 50 came back,
    with nothing to say there were more."""
    monkeypatch.setattr(digikey_lists, "_LIST_PAGE", 50)
    lists = _many_lists(digikey, 84)
    out = lists.all_lists()
    assert out["count"] == 84 and out["complete"] is True
    assert "incomplete" not in out
    assert [l["name"] for l in out["lists"]] == [
        f"List {n}" for n in range(84)]
    starts = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        r.full_url).query))["startIndex"]
        for r in digikey.calls("/mylists/v1/lists?", "GET")]
    assert starts[-3:] == ["0", "50", "84"]
    assert len(lists.lists()) == 84


def test_reading_lists_stops_at_the_safety_limit_and_says_so(
        digikey, monkeypatch):
    monkeypatch.setattr(digikey_lists, "_LIST_PAGE", 2)
    monkeypatch.setattr(digikey_lists, "_LIST_MAX_PAGES", 3)
    out = _many_lists(digikey, 7).all_lists()
    assert out["count"] == 6 and out["complete"] is False
    assert "stopped after 3 requests of 2 lists" in out["incomplete"]
    assert "6 lists were read" in out["incomplete"]


def test_a_repeated_page_of_lists_is_reported_not_looped_on(
        digikey, monkeypatch):
    monkeypatch.setattr(digikey_lists, "_LIST_PAGE", 3)
    digikey.ignore_start_index = True
    out = _many_lists(digikey, 5).all_lists()
    assert out["count"] == 3 and out["complete"] is False
    assert "lists already read" in out["incomplete"]
    assert len(digikey.calls("/mylists/v1/lists?", "GET")) == 2


def test_the_tool_says_when_the_lists_are_incomplete(digikey, monkeypatch):
    monkeypatch.setattr(digikey_lists, "_LIST_PAGE", 2)
    monkeypatch.setattr(digikey_lists, "_LIST_MAX_PAGES", 2)
    _many_lists(digikey, 5)
    every = asyncio.run(_tools()["part_list_get"]())
    assert every["ok"] and every["count"] == 4
    assert every["complete"] is False and "may hold more" in \
        every["incomplete"]


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
    assert every["complete"] is True


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
