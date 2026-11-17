# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Mouser carts: creating one, adding to one and reading one back.

Everything here talks to a made-up Mouser that keeps carts in memory.
The components are made up too. No request leaves the machine.
"""

import asyncio
import inspect
import io
import json
import urllib.error
import urllib.parse

import pytest

from eda_agent.libimport.providers import mouser_cart
from eda_agent.libimport.providers.base import (
    ProviderError,
    ProviderUnavailable,
)
from eda_agent.libimport.providers.mouser_cart import (
    CartInputError,
    MouserCart,
)

_URLOPEN = "eda_agent.libimport.providers._distributor.urllib.request.urlopen"

_CART_API_KEY = "11111111-2222-3333-4444-555555555555"
_SEARCH_API_KEY = "99999999-8888-7777-6666-555555555555"
_KEY_1 = "aaaaaaaa-0000-0000-0000-000000000001"

#: The made-up catalogue: Mouser number to (MPN, maker, text, min, mult,
#: unit price).
_CATALOGUE = {
    "603-RC0603FR-0710KL": ("RC0603FR-0710KL", "YAGEO",
                            "Thick Film Resistors - SMD 10 kOhms", 1, 1, 0.017),
    "595-LM358DR": ("LM358DR", "Texas Instruments",
                    "Operational Amplifiers - Op Amps Dual", 1, 1, 0.31),
    "81-GRM155R71C104KA8D": ("GRM155R71C104KA88D", "Murata",
                             "Multilayer Ceramic Capacitors", 10, 10, 0.004),
}


class _Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Mouser:
    """A Mouser that searches a small catalogue and keeps carts."""

    def __init__(self, monkeypatch):
        self.requests = []
        self.carts = {}
        #: Queue of (status, payload) answered in place of a cart call.
        self.fail_next = []
        #: Characters Mouser keeps of a customer part number; None: all.
        self.customer_limit = None
        #: True: a quantity is raised to the part's multiple, silently.
        self.round_up = False
        #: True: an insert answers with no CartKey at all.
        self.no_key = False
        monkeypatch.setattr(_URLOPEN, self)

    def calls(self, text, method=None):
        return [r for r in self.requests if text in r.full_url
                and method in (None, r.get_method())]

    def _fail(self, request, status, payload):
        raise urllib.error.HTTPError(
            request.full_url, status, "failed", {},
            io.BytesIO(json.dumps(payload).encode("utf-8")))

    def _cart(self, key, errors=()):
        lines = self.carts[key]
        return {
            "Errors": list(errors), "CartKey": key, "CurrencyCode": "USD",
            "CartItems": lines, "TotalItemCount": len(lines),
            "additionalFeesTotal": 0.0,
            "MerchandiseTotal": round(
                sum(l["ExtendedPrice"] for l in lines), 4)}

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = urllib.parse.urlsplit(request.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        if url.path == "/api/v1/search/partnumber":
            assert query["apiKey"] == _SEARCH_API_KEY
            number = body["SearchByPartRequest"]["mouserPartNumber"]
            parts = [
                {"MouserPartNumber": sku, "ManufacturerPartNumber": mpn,
                 "Manufacturer": maker, "Description": text,
                 "Min": str(low), "Mult": str(multiple), "PriceBreaks": []}
                for sku, (mpn, maker, text, low, multiple, _)
                in _CATALOGUE.items()
                if number.casefold() in (sku.casefold(), mpn.casefold())]
            return _Reply(json.dumps({"Errors": [], "SearchResults": {
                "NumberOfResult": len(parts), "Parts": parts}}).encode())
        assert url.path.startswith("/api/v1/cart"), request.full_url
        if query.get("apiKey") != _CART_API_KEY:
            return _Reply(json.dumps({"Errors": [{
                "Id": 0, "Code": "Invalid",
                "Message": "Invalid unique identifier.",
                "PropertyName": "API Key"}]}).encode())
        if self.fail_next:
            status, payload = self.fail_next.pop(0)
            if status == 200:
                return _Reply(json.dumps(payload).encode())
            self._fail(request, status, payload)
        if url.path == "/api/v1/cart" and method == "GET":
            key = query["cartKey"]
            if key not in self.carts:
                return _Reply(json.dumps({"Errors": [{
                    "Id": 0, "Code": "NotFound",
                    "Message": "Cart not found.",
                    "PropertyName": "CartKey"}],
                    "CartItems": []}).encode())
            return _Reply(json.dumps(self._cart(key)).encode())
        assert (url.path, method) == ("/api/v1/cart/items/insert", "POST")
        if self.no_key:
            return _Reply(json.dumps({"Errors": [], "CartItems": []}).encode())
        key = body["CartKey"]
        if not key:
            key = f"aaaaaaaa-0000-0000-0000-{len(self.carts) + 1:012d}"
            self.carts[key] = []
        elif key not in self.carts:
            return _Reply(json.dumps({"Errors": [{
                "Id": 0, "Code": "NotFound", "Message": "Cart not found.",
                "PropertyName": "CartKey"}], "CartItems": []}).encode())
        errors = []
        for wanted in body["CartItems"]:
            assert set(wanted) <= {"MouserPartNumber", "Quantity",
                                   "CustomerPartNumber", "PackagingChoice"}
            number = wanted["MouserPartNumber"]
            known = _CATALOGUE.get(number)
            if not known:
                errors.append({
                    "Id": 0, "Code": "InvalidMouserPartNumber",
                    "Message": f"Part number {number} was not found.",
                    "PropertyName": "MouserPartNumber"})
                continue
            mpn, maker, text, low, multiple, price = known
            quantity = wanted["Quantity"]
            if self.round_up:
                quantity = max(low, -(-quantity // multiple) * multiple)
            customer = wanted.get("CustomerPartNumber", "")
            if self.customer_limit is not None:
                customer = customer[:self.customer_limit]
            held = next((l for l in self.carts[key]
                         if l["MouserPartNumber"] == number), None)
            if held:
                held["Quantity"] += quantity
                held["ExtendedPrice"] = round(held["Quantity"] * price, 4)
                continue
            self.carts[key].append({
                "Errors": [], "MouserATS": 5000, "Quantity": quantity,
                "PackagingChoice": wanted.get("PackagingChoice", "None"),
                "InfoMessages": [], "MouserPartNumber": number,
                "MfrPartNumber": mpn, "Description": text,
                "CartItemCustPartNumber": customer, "UnitPrice": price,
                "ExtendedPrice": round(quantity * price, 4),
                "LifeCycle": "", "Manufacturer": maker,
                "SalesMultipleQty": multiple, "SalesMinimumOrderQty": low,
                "SalesMaximumOrderQty": 0, "additionalFees": [],
                "sent": wanted})
        return _Reply(json.dumps(self._cart(key, errors)).encode())


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("MOUSER_CART_API_KEY", _CART_API_KEY)
    monkeypatch.setenv("MOUSER_API_KEY", _SEARCH_API_KEY)


@pytest.fixture
def mouser(monkeypatch):
    return _Mouser(monkeypatch)


def _item(sku="603-RC0603FR-0710KL", quantity=10, **more):
    return {"sku": sku, "quantity": quantity, **more}


# ---- creating a cart ----------------------------------------------------------

def test_a_cart_is_created_with_the_exact_number_and_quantity(mouser):
    out = MouserCart().create_cart([_item(
        customer_part_number="TEST-R101-R110",
        designators=["R101", "R102", "R103"],
        description="RES 10K 1% 0603", manufacturer="Yageo",
        manufacturer_part_number="RC0603FR-0710KL")])
    assert out["cart_key"] == _KEY_1 and out["created"] is True
    assert (out["sent"], out["added"], out["verified"]) == (1, 1, 1)
    insert = mouser.calls("/cart/items/insert", "POST")
    assert len(insert) == 1
    # A new cart is asked for with an empty key, and only the four
    # fields Mouser's cart has are sent.
    assert json.loads(insert[0].data) == {"CartKey": "", "CartItems": [{
        "MouserPartNumber": "603-RC0603FR-0710KL", "Quantity": 10,
        "CustomerPartNumber": "TEST-R101-R110"}]}
    row = out["items"][0]
    assert row["differences"] == [] and row["verified"] is True
    assert row["cart_line"]["sku"] == "603-RC0603FR-0710KL"
    assert row["cart_line"]["quantity"] == 10
    assert row["cart_line"]["customer_part_number"] == "TEST-R101-R110"
    assert row["cart_line"]["manufacturer"] == "YAGEO"
    assert row["cart_line"]["manufacturer_part_number"] == "RC0603FR-0710KL"
    assert row["cart_line"]["unit_price"] == 0.017
    assert row["cart_line"]["extended_price"] == 0.17
    assert out["cart"]["currency"] == "USD"
    assert out["cart"]["merchandise_total"] == 0.17
    # The cart was read back by its key after the insert.
    read = mouser.calls("/api/v1/cart?", "GET")
    assert len(read) == 1 and f"cartKey={_KEY_1}" in read[0].full_url


def test_designators_and_description_are_kept_and_never_sent(mouser):
    out = MouserCart().create_cart([_item(
        designators="R101, R102, R103", description="RES 10K 1% 0603")])
    row = out["items"][0]
    assert row["designators"] == "R101, R102, R103"
    assert row["description"] == "RES 10K 1% 0603"
    assert "no field for reference designators" in out["not_sent_to_mouser"]
    for request in mouser.calls("/cart"):
        text = request.full_url + (request.data or b"").decode()
        assert "R101" not in text and "RES 10K" not in text
    assert row["cart_line"]["customer_part_number"] == ""


def test_the_cart_key_is_the_cart_api_key_never_the_search_key(mouser):
    MouserCart().create_cart([_item()])
    for request in mouser.calls("/cart"):
        assert f"apiKey={_CART_API_KEY}" in request.full_url
        assert _SEARCH_API_KEY not in request.full_url


def test_packaging_is_sent_when_asked_and_checked(mouser):
    out = MouserCart().create_cart([_item(packaging="cut_tape")])
    sent = mouser.carts[_KEY_1][0]["sent"]
    assert sent["PackagingChoice"] == "Cut_Tape"
    assert out["items"][0]["cart_line"]["packaging"] == "Cut_Tape"
    assert out["verified"] == 1


def test_malformed_items_are_refused_whole_and_no_cart_is_made(mouser):
    with pytest.raises(CartInputError) as caught:
        MouserCart().create_cart([
            _item(),
            _item(sku="  "),
            _item("595-LM358DR", quantity=0),
            _item("595-LM358DR", quantity="3"),
            _item("595-LM358DR", quantity=True),
            _item("595-LM358DR", quantity=2.5),
            _item("595-LM358DR", comment="pull-up"),
            _item(),                                     # the same sku
            _item("595-LM358DR", customer_part_number="X" * 22),
            _item("595-LM358DR", customer_part_number="A*B"),
            _item("595-LM358DR", packaging="Tube"),
            _item("X" * 81),
            "603-RC0603FR-0710KL",
        ])
    problems = {p["item"]: " ".join(p["problems"])
                for p in caught.value.problems}
    assert sorted(problems) == list(range(1, 13))
    assert "no sku" in problems[1]
    assert "not a whole number" in problems[2] and "'3'" in problems[3]
    assert "unknown field(s) 'comment'" in problems[6]
    assert "the same sku as item 0" in problems[7]
    assert "22 characters; Mouser takes 21" in problems[8]
    assert "not shortened" in problems[8]
    assert "contains '*'" in problems[9]
    assert "no packaging 'Tube'" in problems[10]
    assert "81 characters" in problems[11]
    assert "nothing was added" in str(caught.value)
    assert mouser.requests == [] and mouser.carts == {}
    for bad in ([], None, {"sku": "X"}):
        with pytest.raises(CartInputError):
            MouserCart().create_cart(bad)


def test_a_customer_part_number_of_21_characters_is_sent_whole(mouser):
    out = MouserCart().create_cart([_item(customer_part_number="X" * 21)])
    assert mouser.carts[_KEY_1][0]["CartItemCustPartNumber"] == "X" * 21
    assert out["verified"] == 1


# ---- what the catalogue says first --------------------------------------------

def test_a_number_that_is_not_a_mouser_number_makes_no_cart(mouser):
    with pytest.raises(CartInputError) as caught:
        MouserCart().create_cart([
            _item(),
            _item("RC0603FR-0710KL"),          # a manufacturer number
            _item("000-NO-SUCH-PART"),
        ])
    problems = {p["item"]: p["problems"][0] for p in caught.value.problems}
    assert sorted(problems) == [1, 2]
    assert "Mouser's numbers for it are 603-RC0603FR-0710KL" in problems[1]
    assert "Mouser has no part with it" in problems[2]
    assert mouser.calls("/cart") == [] and mouser.carts == {}


def test_a_quantity_below_the_minimum_or_off_the_multiple_is_refused(mouser):
    for quantity, said in ((5, "below Mouser's minimum of 10"),
                           (25, "not a multiple of 10")):
        with pytest.raises(CartInputError) as caught:
            MouserCart().create_cart(
                [_item("81-GRM155R71C104KA8D", quantity)])
        assert said in caught.value.problems[0]["problems"][0]
    assert mouser.calls("/cart") == []
    out = MouserCart().create_cart([_item("81-GRM155R71C104KA8D", 30)])
    assert out["verified"] == 1


def test_without_the_catalogue_check_mouser_itself_is_believed(mouser):
    out = MouserCart().create_cart(
        [_item(), _item("000-NO-SUCH-PART")], check_products=False)
    assert mouser.calls("/search/") == []
    assert (out["sent"], out["added"], out["verified"]) == (2, 1, 1)
    row = out["items"][1]
    assert row["in_cart"] is False and row["verified"] is False
    assert row["differences"][0] == "not in the cart after adding"
    assert any("000-NO-SUCH-PART was not found" in e
               for e in out["mouser_errors"])


# ---- what Mouser did with it ----------------------------------------------------

def test_a_quantity_mouser_changed_is_reported(mouser):
    mouser.round_up = True
    out = MouserCart().create_cart(
        [_item("81-GRM155R71C104KA8D", 25)], check_products=False)
    row = out["items"][0]
    assert row["differences"] == [
        "quantity: sent 25, the cart holds 30 (minimum 10, multiple 10)"]
    assert row["verified"] is False and out["verified"] == 0


def test_a_customer_part_number_mouser_changed_is_reported(mouser):
    mouser.customer_limit = 8
    out = MouserCart().create_cart(
        [_item(customer_part_number="TEST-R101-R110")])
    assert out["items"][0]["differences"] == [
        "customer part number: sent 'TEST-R101-R110', the cart holds "
        "'TEST-R10'"]


def test_a_part_that_is_not_what_the_bom_says_is_reported(mouser):
    out = MouserCart().create_cart([_item(
        manufacturer="Vishay", manufacturer_part_number="CRCW060310K0FKEA")])
    assert out["items"][0]["differences"] == [
        "manufacturer: the BOM says 'Vishay', Mouser's part is 'YAGEO'",
        "manufacturer part number: the BOM says 'CRCW060310K0FKEA', "
        "Mouser's part is 'RC0603FR-0710KL'"]
    assert out["verified"] == 0


def test_an_answer_without_a_cart_key_is_a_failure_not_a_cart(mouser):
    mouser.no_key = True
    out = MouserCart().create_cart([_item()])
    assert out["cart_key"] is None and out["created"] is False
    assert out["added"] == 0 and "without a cart key" in out["failed"]
    assert out["not_sent"] == [{"item": 0, "sku": "603-RC0603FR-0710KL"}]
    assert mouser.calls("/api/v1/cart?", "GET") == []


# ---- adding to a cart ---------------------------------------------------------

def test_parts_are_added_to_the_cart_that_was_named(mouser):
    cart = MouserCart()
    key = cart.create_cart([_item()])["cart_key"]
    out = cart.add_items(key, [_item("595-LM358DR", 2)])
    assert out["cart_key"] == key and out["created"] is False
    assert (out["sent"], out["added"], out["verified"]) == (1, 1, 1)
    assert out["cart_total_lines"] == 2 and len(mouser.carts) == 1
    insert = json.loads(mouser.calls("/cart/items/insert", "POST")[1].data)
    assert insert == {"CartKey": key, "CartItems": [
        {"MouserPartNumber": "595-LM358DR", "Quantity": 2}]}


def test_adding_a_part_already_in_the_cart_says_what_it_held(mouser):
    cart = MouserCart()
    key = cart.create_cart([_item()])["cart_key"]
    out = cart.add_items(key, [_item(quantity=5)])
    row = out["items"][0]
    assert row["cart_line"]["quantity"] == 15
    assert row["note"] == ("the cart already held 10 of this part; it now "
                           "holds 15")
    assert row["differences"] == [] and row["verified"] is True


def test_adding_needs_a_key_and_never_makes_a_cart(mouser):
    cart = MouserCart()
    for key in ("", "   ", None):
        with pytest.raises(CartInputError, match="which cart"):
            cart.add_items(key, [_item()])
    with pytest.raises(CartInputError, match="not a cart key"):
        cart.add_items("my cart", [_item()])
    assert mouser.requests == [] and mouser.carts == {}
    # A well-formed key Mouser does not know: an error, and no new cart.
    with pytest.raises(ProviderError, match="Cart not found"):
        cart.add_items("bbbbbbbb-0000-0000-0000-000000000009", [_item()])
    assert mouser.carts == {}
    assert mouser.calls("/cart/items/insert") == []


def test_a_batch_mouser_refuses_says_what_was_and_was_not_sent(
        mouser, monkeypatch):
    monkeypatch.setattr(mouser_cart, "_ADD_BATCH", 1)
    cart = MouserCart()
    real = mouser.__call__
    posts = []

    def flaky(request, timeout=None):
        if "/cart/items/insert" in request.full_url:
            posts.append(request)
            if len(posts) == 2:
                mouser.fail_next = [(400, {"Errors": [{
                    "Code": "Bad", "Message": "Bad part"}]})]
        return real(request, timeout)

    monkeypatch.setattr(_URLOPEN, flaky)
    out = cart.create_cart(
        [_item(), _item("595-LM358DR", 2),
         _item("81-GRM155R71C104KA8D", 10)], check_products=False)
    assert (out["sent"], out["added"], out["verified"]) == (1, 1, 1)
    assert "after 1 of 3 items were sent" in out["failed"]
    assert "Bad part" in out["failed"]
    assert [n["item"] for n in out["not_sent"]] == [1, 2]
    # The second batch went to the cart the first one made.
    assert json.loads(posts[1].data)["CartKey"] == out["cart_key"]
    assert len(mouser.carts) == 1


# ---- reading a cart -----------------------------------------------------------

def test_a_cart_reads_back_as_mouser_holds_it(mouser):
    cart = MouserCart()
    key = cart.create_cart([_item(customer_part_number="BAG-7")])["cart_key"]
    got = cart.get_cart(key)
    assert (got["cart_key"], got["count"], got["currency"]) == (
        key, 1, "USD")
    assert got["items"] == [{
        "sku": "603-RC0603FR-0710KL", "manufacturer": "YAGEO",
        "manufacturer_part_number": "RC0603FR-0710KL",
        "mouser_description": "Thick Film Resistors - SMD 10 kOhms",
        "quantity": 10, "customer_part_number": "BAG-7",
        "packaging": "None", "unit_price": 0.017, "extended_price": 0.17,
        "stock": 5000, "moq": 1, "multiple": 1, "maximum": 0,
        "parts_per_reel": None, "lifecycle": None, "additional_fees": [],
        "messages": [], "errors": []}]


def test_a_cart_that_does_not_exist_is_an_error_not_an_empty_cart(mouser):
    with pytest.raises(ProviderError, match="Cart not found"):
        MouserCart().get_cart("bbbbbbbb-0000-0000-0000-000000000009")
    with pytest.raises(CartInputError):
        MouserCart().get_cart("")


# ---- failures -----------------------------------------------------------------

def test_a_missing_or_refused_key_says_which_key(mouser, monkeypatch):
    monkeypatch.setenv("MOUSER_CART_API_KEY", "cccccccc-0000-0000-0000-"
                       "000000000000")
    with pytest.raises(ProviderUnavailable) as caught:
        MouserCart().create_cart([_item()], check_products=False)
    text = str(caught.value)
    assert "MOUSER_CART_API_KEY" in text and "not the Search API" in text
    assert "cccccccc" not in text
    monkeypatch.delenv("MOUSER_CART_API_KEY")
    with pytest.raises(ProviderUnavailable, match="MOUSER_CART_API_KEY"):
        MouserCart().get_cart(_KEY_1)


def test_a_rate_limit_and_a_server_error_are_reported_as_such(mouser):
    mouser.fail_next = [(403, {"Errors": [{
        "Code": "TooManyRequests",
        "Message": "Maximum calls per minute exceeded."}]})]
    with pytest.raises(ProviderUnavailable, match="rate-limited"):
        MouserCart().get_cart(_KEY_1)
    mouser.fail_next = [(200, {"Errors": [{
        "Code": "TooManyRequests",
        "Message": "Maximum calls per day exceeded."}]})]
    with pytest.raises(ProviderUnavailable, match="rate-limited"):
        MouserCart().get_cart(_KEY_1)
    mouser.fail_next = [(500, {"Errors": [{"Message": "Internal error"}]})]
    with pytest.raises(ProviderError, match="HTTP 500: Internal error"):
        MouserCart().get_cart(_KEY_1)


def test_no_key_is_ever_returned(mouser):
    cart = MouserCart()
    made = cart.create_cart([_item()])
    mouser.fail_next = [(400, {"Errors": [{
        "Message": f"Bad request for apiKey {_CART_API_KEY}"}]})]
    with pytest.raises(ProviderError) as caught:
        cart.get_cart(made["cart_key"])
    for text in (json.dumps(made), json.dumps(cart.get_cart(
            made["cart_key"])), str(caught.value)):
        assert _CART_API_KEY not in text and _SEARCH_API_KEY not in text


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


def test_the_tools_create_add_and_read_back(mouser):
    tools = _tools()
    made = asyncio.run(tools["part_cart_create"](items=[_item(
        customer_part_number="TEST-R101-R110", designators=["R101"])]))
    assert made["ok"] and made["provider"] == "mouser"
    assert made["created"] is True and made["verified"] == 1
    added = asyncio.run(tools["part_cart_add"](
        cart_key=made["cart_key"], items=[_item("595-LM358DR", 2)]))
    assert added["ok"] and added["created"] is False
    got = asyncio.run(tools["part_cart_get"](cart_key=made["cart_key"]))
    assert got["ok"] and got["count"] == 2
    assert [i["quantity"] for i in got["items"]] == [10, 2]
    assert got["items"][0]["customer_part_number"] == "TEST-R101-R110"


def test_the_tools_answer_failures_without_raising(mouser, monkeypatch):
    tools = _tools()
    out = asyncio.run(tools["part_cart_create"](
        items=[_item()], provider="digikey"))
    assert out["ok"] is False and "only mouser" in out["reason"]
    out = asyncio.run(tools["part_cart_create"](items=[_item(quantity=0)]))
    assert out["ok"] is False and out["problems"][0]["item"] == 0
    out = asyncio.run(tools["part_cart_add"](cart_key="", items=[_item()]))
    assert out["ok"] is False and "which cart" in out["reason"]
    out = asyncio.run(tools["part_cart_get"](
        cart_key="bbbbbbbb-0000-0000-0000-000000000009"))
    assert out["ok"] is False and "Cart not found" in out["reason"]
    assert mouser.carts == {}
    monkeypatch.delenv("MOUSER_CART_API_KEY")
    out = asyncio.run(tools["part_cart_get"](cart_key=_KEY_1))
    assert out["ok"] is False and "MOUSER_CART_API_KEY" in out["unavailable"]


def test_nothing_here_orders_or_checks_out(mouser):
    """The module names two addresses, both under /cart, and no request
    of a whole create, add and read goes anywhere else."""
    source = inspect.getsource(mouser_cart)
    addresses = [line.split('"')[1] for line in source.splitlines()
                 if "https://" in line and "_URL" in line]
    assert addresses == ["https://api.mouser.com/api/v1/cart",
                         "https://api.mouser.com/api/v1/cart/items/insert"]
    for word in ("/order", "orderhistory", "SubmitOrder", "payment",
                 "checkout"):
        assert word.lower() not in source.lower().replace(
            "a payment, an address or a checkout", ""), word
    cart = MouserCart()
    key = cart.create_cart([_item()])["cart_key"]
    cart.add_items(key, [_item("595-LM358DR", 1)])
    cart.get_cart(key)
    for request in mouser.requests:
        path = urllib.parse.urlsplit(request.full_url).path
        assert path in ("/api/v1/cart", "/api/v1/cart/items/insert",
                        "/api/v1/search/partnumber"), path
