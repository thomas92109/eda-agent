# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The SQLite cache of DigiKey and Mouser replies.

Every test has its own cache file in its own temporary folder, and the
distributors are made up: the replies are the recorded ones under
``tests/fixtures/distributors``. No request leaves the machine and the
user's real cache is never opened.
"""

import asyncio
import copy
import io
import json
import pathlib
import sqlite3
import threading
import urllib.error

import pytest

from eda_agent.libimport.providers import _cache, distributors
from eda_agent.libimport.providers.base import (
    ProviderError,
    ProviderUnavailable,
)
from eda_agent.libimport.providers.distributors import (
    DigiKeyProvider,
    MouserProvider,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "distributors"
_URLOPEN = "eda_agent.libimport.providers._distributor.urllib.request.urlopen"

_MOUSER_KEY = "11111111-aaaa-bbbb-cccc-000000000001"
_CART_KEY = "22222222-aaaa-bbbb-cccc-000000000002"
_DIGIKEY_ID = "digikey-client-id-under-test"
_DIGIKEY_SECRET = "digikey-client-secret-under-test"
_TOKEN = {"access_token": "bearer-token-under-test", "expires_in": 599,
          "token_type": "Bearer"}
_HOUR = 3600


def _fixture(name):
    return json.loads((_FIXTURES / f"{name}.json").read_text("utf-8"))


class _Reply(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Wire:
    """Answers each request by the first route whose text is in its URL.

    An answer is a payload, an ``(http status, payload)`` pair for a
    failure, a list of those used one per call, or a function of the
    request.
    """

    def __init__(self, monkeypatch, routes):
        self.routes = dict(routes)
        self.requests = []
        monkeypatch.setattr(_URLOPEN, self)

    def calls(self, part):
        return [r for r in self.requests if part in r.full_url]

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        for part, answer in self.routes.items():
            if part not in request.full_url:
                continue
            if callable(answer):
                answer = answer(request)
            if isinstance(answer, list):
                answer = answer.pop(0) if len(answer) > 1 else answer[0]
            if isinstance(answer, tuple):
                raise urllib.error.HTTPError(
                    request.full_url, answer[0], "failed", {},
                    io.BytesIO(json.dumps(answer[1]).encode("utf-8")))
            return _Reply(json.dumps(answer).encode("utf-8"))
        raise AssertionError(f"unexpected request to {request.full_url}")


class _Clock:
    """``time.time`` for the cache, moved by hand."""

    def __init__(self, monkeypatch):
        self.now = 1_800_000_000.0
        monkeypatch.setattr(_cache.time, "time", lambda: self.now)

    def forward(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    """The cache on, in this test's own file; stand-in credentials."""
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE", "1")
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE_FILE",
                       str(tmp_path / "cache.sqlite3"))
    monkeypatch.setenv("MOUSER_API_KEY", _MOUSER_KEY)
    monkeypatch.setenv("MOUSER_CART_API_KEY", _CART_KEY)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", _DIGIKEY_ID)
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", _DIGIKEY_SECRET)
    for var, _ in DigiKeyProvider._LOCALE_HEADERS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("EDA_AGENT_PART_PROVIDERS", raising=False)
    distributors._DIGIKEY_TOKENS.clear()
    yield
    distributors._DIGIKEY_TOKENS.clear()


@pytest.fixture
def clock(monkeypatch):
    return _Clock(monkeypatch)


def _db(tmp_path):
    return tmp_path / "cache.sqlite3"


def _rows(tmp_path):
    with sqlite3.connect(_db(tmp_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM responses ORDER BY fetched_at, key")]


class _Source:
    """A fetch that counts its calls and answers with their number."""

    def __init__(self, kinds=(_cache.PRICING,)):
        self.calls = 0
        self.kinds = kinds

    def get(self, distributor="mouser", endpoint="https://x/search",
            request=None, context=None, kinds=None):
        def fetch():
            self.calls += 1
            return {"answer": self.calls}

        found = self.kinds if kinds is None else kinds
        return _cache.cached_call(
            distributor, endpoint, request or {"q": "LM358"},
            context or {"account": "a"}, fetch, kinds=lambda reply: found)


# ---- the cache itself ---------------------------------------------------------

def test_a_miss_asks_and_a_hit_does_not(clock):
    source = _Source()
    first, held = source.get()
    assert first == {"answer": 1} and held is None
    clock.forward(600)
    second, held = source.get()
    assert second == {"answer": 1} and source.calls == 1
    assert held == {"fetched_at": "2027-01-15T08:00:00Z",
                    "expires_at": "2027-01-15T12:00:00Z"}


def test_how_long_each_kind_of_reply_is_kept():
    assert _cache.TTL_SECONDS == {
        "product": 30 * 24 * _HOUR, "search": 24 * _HOUR,
        "pricing": 4 * _HOUR}
    assert _cache.ttl_for(["product"]) == 30 * 24 * _HOUR
    assert _cache.ttl_for(["search"]) == 24 * _HOUR
    assert _cache.ttl_for(["pricing"]) == 4 * _HOUR
    # A reply holding several lives as long as the shortest.
    assert _cache.ttl_for(["search", "pricing"]) == 4 * _HOUR
    assert _cache.ttl_for(["product", "pricing"]) == 4 * _HOUR
    assert _cache.ttl_for(["product", "search"]) == 24 * _HOUR


@pytest.mark.parametrize("kinds,hours", [
    (("product",), 30 * 24), (("search",), 24), (("pricing",), 4),
    (("search", "pricing"), 4), (("product", "pricing"), 4)])
def test_an_entry_is_used_until_it_expires_and_not_after(
        clock, kinds, hours):
    source = _Source(kinds)
    source.get()
    clock.forward(hours * _HOUR - 1)
    assert source.get()[1] is not None and source.calls == 1
    clock.forward(1)
    reply, held = source.get()
    assert held is None and reply == {"answer": 2} and source.calls == 2
    # The new reply replaced the old one and is the one now held.
    assert source.get() == ({"answer": 2}, {
        "fetched_at": _cache._iso(clock.now),
        "expires_at": _cache._iso(clock.now + hours * _HOUR)})


def test_an_expired_entry_is_removed_when_something_is_written(
        clock, tmp_path):
    source = _Source()
    source.get(request={"q": "old"})
    clock.forward(5 * _HOUR)
    source.get(request={"q": "new"})
    assert [json.loads(r["request"]) for r in _rows(tmp_path)] == [
        {"q": "new"}]


def test_a_fresh_lookup_asks_again_and_replaces_what_was_held(clock):
    source = _Source()
    source.get()
    with _cache.fresh():
        assert _cache.forcing_fresh()
        reply, held = source.get()
    assert reply == {"answer": 2} and held is None
    assert not _cache.forcing_fresh()
    assert source.get()[0] == {"answer": 2} and source.calls == 2
    # fresh(False) is the ordinary call, and does not undo an outer one.
    with _cache.fresh(False):
        assert source.get()[1] is not None
    with _cache.fresh():
        with _cache.fresh(False):
            assert _cache.forcing_fresh()
    assert source.calls == 2


@pytest.mark.parametrize("value", ["0", "off", "false", "No"])
def test_the_cache_can_be_turned_off(monkeypatch, tmp_path, value):
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE", value)
    source = _Source()
    assert source.get() == ({"answer": 1}, None)
    assert source.get() == ({"answer": 2}, None)
    assert not _db(tmp_path).exists()


def test_a_failed_call_is_not_kept_and_reaches_the_caller(tmp_path):
    def failing():
        raise ProviderError("mouser returned HTTP 500")

    for _ in range(2):
        with pytest.raises(ProviderError, match="HTTP 500"):
            _cache.cached_call("mouser", "https://x", {}, {}, failing,
                               kinds=lambda reply: ["pricing"])
    assert _rows(tmp_path) == []


@pytest.mark.parametrize("kinds", [None, [], ()])
def test_a_reply_that_is_not_to_be_kept_is_not_kept(tmp_path, kinds):
    source = _Source(kinds)
    assert source.get() == ({"answer": 1}, None)
    assert source.get() == ({"answer": 2}, None)
    assert not _db(tmp_path).exists() or _rows(tmp_path) == []


def test_entries_are_kept_apart_by_everything_that_changes_the_answer():
    source = _Source()
    base = dict(distributor="mouser", endpoint="https://x/search",
                request={"q": "LM358", "records": 20},
                context={"account": "a", "currency": "USD"})
    different = [
        dict(base, distributor="digikey"),
        dict(base, endpoint="https://x/partnumber"),
        dict(base, request={"q": "LM358", "records": 50}),     # quantity
        dict(base, request={"q": "LM324", "records": 20}),
        dict(base, context={"account": "a", "currency": "EUR"}),
        dict(base, context={"account": "b", "currency": "USD"}),
        dict(base, context={"account": "a", "currency": "USD",
                            "site": "DE"}),
    ]
    assert source.get(**base)[0] == {"answer": 1}
    for number, other in enumerate(different, start=2):
        assert source.get(**other) == ({"answer": number}, None)
    # Each is then answered from its own entry.
    assert source.get(**base)[0] == {"answer": 1}
    for number, other in enumerate(different, start=2):
        assert source.get(**other)[0] == {"answer": number}
    assert source.calls == 1 + len(different)


def test_the_order_of_parameters_does_not_make_a_new_entry():
    source = _Source()
    source.get(request={"a": 1, "b": {"x": 1, "y": 2}})
    assert source.get(request={"b": {"y": 2, "x": 1}, "a": 1})[1]
    assert source.calls == 1


def test_what_a_row_holds(clock, tmp_path):
    source = _Source(("search", "pricing"))
    source.get(request={"keyword": "LM358", "records": 20},
               context={"account": "abc", "currency": "USD"})
    (row,) = _rows(tmp_path)
    assert row["distributor"] == "mouser"
    assert row["endpoint"] == "https://x/search"
    assert json.loads(row["request"]) == {"keyword": "LM358", "records": 20}
    assert json.loads(row["context"]) == {"account": "abc",
                                          "currency": "USD"}
    assert row["kinds"] == "pricing,search"
    assert json.loads(row["response"]) == {"answer": 1}
    assert row["fetched_at"] == clock.now
    assert row["expires_at"] == clock.now + 4 * _HOUR


def test_the_file_is_in_wal_mode_with_a_busy_timeout(tmp_path):
    _Source().get()
    conn = _cache._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()
    with sqlite3.connect(_db(tmp_path)) as plain:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_default_file_is_in_the_users_profile_not_the_repository(
        monkeypatch, tmp_path):
    monkeypatch.delenv("EDA_AGENT_DISTRIBUTOR_CACHE_FILE")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert _cache._path() == (tmp_path / "AppData" / "eda-agent"
                              / "distributor-cache.sqlite3")
    repository = pathlib.Path(__file__).resolve().parents[1]
    assert repository not in _cache._path().parents


# ---- when the database fails ----------------------------------------------------

def test_a_file_that_is_not_a_database_costs_no_answer(tmp_path):
    _db(tmp_path).write_bytes(b"this is not a SQLite file" * 50)
    source = _Source()
    assert source.get() == ({"answer": 1}, None)
    assert source.get() == ({"answer": 2}, None)
    assert _db(tmp_path).read_bytes().startswith(b"this is not")


def test_a_cache_file_that_cannot_be_opened_costs_no_answer(
        monkeypatch, tmp_path):
    folder = tmp_path / "a-folder"
    folder.mkdir()
    monkeypatch.setenv("EDA_AGENT_DISTRIBUTOR_CACHE_FILE", str(folder))
    source = _Source()
    assert source.get() == ({"answer": 1}, None)
    assert source.get() == ({"answer": 2}, None)


@pytest.mark.parametrize("broken", ["_read", "_write", "_connect"])
def test_a_database_error_falls_back_to_the_distributor(
        monkeypatch, broken):
    source = _Source()
    source.get()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(_cache, broken, fail)
    reply, held = source.get()
    if broken == "_write":
        # Reading still works; only a new reply cannot be kept.
        assert held is not None and source.calls == 1
        with _cache.fresh():
            assert source.get() == ({"answer": 2}, None)
    else:
        assert (reply, held) == ({"answer": 2}, None)
        assert source.get() == ({"answer": 3}, None)


def test_a_row_that_cannot_be_read_is_dropped_and_asked_again(tmp_path):
    source = _Source()
    source.get()
    with sqlite3.connect(_db(tmp_path)) as conn:
        conn.execute("UPDATE responses SET response = '{not json'")
    assert source.get() == ({"answer": 2}, None)
    assert source.get()[0] == {"answer": 2} and source.calls == 2
    assert len(_rows(tmp_path)) == 1


def test_a_file_from_another_version_is_emptied_not_misread(tmp_path):
    with sqlite3.connect(_db(tmp_path)) as conn:
        conn.execute("CREATE TABLE responses (key TEXT, something TEXT)")
        conn.execute("INSERT INTO responses VALUES ('k', 'old')")
        conn.execute("PRAGMA user_version = 99")
    source = _Source()
    assert source.get() == ({"answer": 1}, None)
    assert source.get()[1] is not None
    assert [r["distributor"] for r in _rows(tmp_path)] == ["mouser"]


def test_many_threads_at_once_leave_a_sound_database(tmp_path):
    errors = []
    answers = {}

    def work(worker):
        try:
            for step in range(25):
                number = (worker * 7 + step) % 12
                reply, _ = _cache.cached_call(
                    "mouser" if number % 2 else "digikey",
                    "https://x/search", {"q": number}, {"account": "a"},
                    lambda number=number: {"part": number},
                    kinds=lambda reply: ["pricing"])
                answers.setdefault(number, set()).add(json.dumps(reply))
        except Exception as exc:  # noqa: BLE001 - the test reports it
            errors.append(repr(exc))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    # Every key only ever answered with its own reply.
    assert answers == {n: {json.dumps({"part": n})} for n in range(12)}
    with sqlite3.connect(_db(tmp_path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM responses").fetchone()[0] == 12


def test_stats_and_clear(tmp_path):
    source = _Source()
    source.get(distributor="mouser")
    source.get(distributor="digikey")
    held = _cache.stats()
    assert held["file"] == str(_db(tmp_path)) and held["enabled"] is True
    assert [(e["distributor"], e["live"]) for e in held["entries"]] == [
        ("digikey", 1), ("mouser", 1)]
    assert _cache.clear("mouser") == 1
    assert [e["distributor"] for e in _cache.stats()["entries"]] == [
        "digikey"]
    assert _cache.clear() == 1 and _cache.stats()["entries"] == []


# ---- Mouser through the cache -------------------------------------------------

def _mouser(monkeypatch, **more):
    return _Wire(monkeypatch, {
        "search/keyword": _fixture("mouser_keyword_lm358dr"),
        "search/partnumber": _fixture("mouser_partnumber_595-lm358dr"),
        **more})


def test_a_mouser_search_is_asked_once_and_then_held(monkeypatch, clock):
    wire = _mouser(monkeypatch)
    first = MouserProvider().search("LM358DR")
    clock.forward(_HOUR)
    second = MouserProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 1
    assert [h.to_dict()["mpn"] for h in first] == [
        h.to_dict()["mpn"] for h in second]
    # A fresh reply is exactly what it was before there was a cache.
    assert all("cache" not in h.sourcing for h in first)
    assert not first[0].sourcing["freshness"].startswith("cached")
    # A held one says when it was really read.
    for hit in second:
        assert hit.sourcing["cache"] == {
            "hit": True, "fetched_at": "2027-01-15T08:00:00Z",
            "expires_at": "2027-01-15T12:00:00Z"}
        assert hit.sourcing["retrieved_at"] == "2027-01-15T08:00:00Z"
        assert hit.sourcing["freshness"].startswith(
            "cached: read at 2027-01-15T08:00:00Z")
    held = copy.deepcopy([h.to_dict() for h in second])
    for entry, fresh in zip(held, first):
        for key in ("cache", "retrieved_at", "freshness"):
            entry["sourcing"].pop(key, None)
        plain = fresh.to_dict()
        for key in ("retrieved_at", "freshness"):
            plain["sourcing"].pop(key)
        assert entry == plain


def test_mouser_prices_are_kept_four_hours(monkeypatch, clock, tmp_path):
    wire = _mouser(monkeypatch)
    MouserProvider().search("LM358DR")
    MouserProvider().fetch("595-LM358DR")
    rows = {r["endpoint"].rsplit("/", 1)[1]: r for r in _rows(tmp_path)}
    assert rows["keyword"]["kinds"] == "pricing,search"
    assert rows["partnumber"]["kinds"] == "pricing,product"
    for row in rows.values():
        assert row["expires_at"] - row["fetched_at"] == 4 * _HOUR
    clock.forward(4 * _HOUR)
    MouserProvider().search("LM358DR")
    MouserProvider().fetch("595-LM358DR")
    assert len(wire.calls("search/keyword")) == 2
    assert len(wire.calls("search/partnumber")) == 2


def test_a_mouser_search_with_no_prices_is_kept_a_day(
        monkeypatch, clock, tmp_path):
    reply = _fixture("mouser_keyword_lm358dr")
    for part in reply["SearchResults"]["Parts"]:
        for field in ("PriceBreaks", "AvailabilityInStock", "Availability",
                      "AvailabilityOnOrder", "FactoryStock"):
            part.pop(field, None)
    _mouser(monkeypatch, **{"search/keyword": reply})
    MouserProvider().search("LM358DR")
    (row,) = _rows(tmp_path)
    assert row["kinds"] == "search"
    assert row["expires_at"] - row["fetched_at"] == 24 * _HOUR


def test_a_mouser_search_that_found_nothing_is_kept_but_a_lookup_is_not(
        monkeypatch, tmp_path):
    empty = _fixture("mouser_partnumber_missing")
    wire = _mouser(monkeypatch, **{"search/keyword": empty,
                                   "search/partnumber": empty})
    provider = MouserProvider()
    assert provider.search("NO-SUCH-THING") == []
    assert provider.search("NO-SUCH-THING") == []
    assert len(wire.calls("search/keyword")) == 1
    for _ in range(2):
        provider._post(provider._PART_URL, {"SearchByPartRequest": {
            "mouserPartNumber": "NO-SUCH", "partSearchOptions": "Exact"}})
    assert len(wire.calls("search/partnumber")) == 2
    assert [r["kinds"] for r in _rows(tmp_path)] == ["search"]


def test_a_mouser_error_under_http_200_is_not_kept(monkeypatch, tmp_path):
    refused = {"Errors": [{"Code": "Invalid",
                           "Message": "Invalid unique identifier.",
                           "PropertyName": "API Key"}]}
    wire = _mouser(monkeypatch, **{"search/keyword": [
        refused, _fixture("mouser_keyword_lm358dr")]})
    with pytest.raises(ProviderUnavailable):
        MouserProvider().search("LM358DR")
    assert not _db(tmp_path).exists() or _rows(tmp_path) == []
    assert MouserProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 2


@pytest.mark.parametrize("reply", [
    None, "text", {"SearchResults": None}, {"SearchResults": {"Parts": None}},
    {"unexpected": True}])
def test_a_mouser_reply_of_the_wrong_shape_is_not_kept(
        monkeypatch, tmp_path, reply):
    wire = _mouser(monkeypatch, **{"search/keyword": reply})
    for _ in range(2):
        assert MouserProvider().search("LM358DR") == []
    assert len(wire.calls("search/keyword")) == 2
    assert not _db(tmp_path).exists() or _rows(tmp_path) == []


def test_a_failed_mouser_call_is_not_kept(monkeypatch, tmp_path):
    wire = _mouser(monkeypatch, **{"search/keyword": [
        (500, {"Errors": [{"Message": "Internal error"}]}),
        _fixture("mouser_keyword_lm358dr")]})
    with pytest.raises(ProviderError, match="HTTP 500"):
        MouserProvider().search("LM358DR")
    assert MouserProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 2


def test_mouser_entries_differ_by_query_limit_and_account(monkeypatch):
    wire = _mouser(monkeypatch)
    provider = MouserProvider()
    provider.search("LM358DR", 20)
    provider.search("LM358DR", 20)
    assert len(wire.calls("search/keyword")) == 1
    provider.search("LM358DR", 5)          # another quantity of results
    provider.search("LM324", 20)           # another part
    assert len(wire.calls("search/keyword")) == 3
    # Another key is another account, with its own prices.
    monkeypatch.setenv("MOUSER_API_KEY",
                       "33333333-aaaa-bbbb-cccc-000000000003")
    MouserProvider().search("LM358DR", 20)
    assert len(wire.calls("search/keyword")) == 4
    monkeypatch.setenv("MOUSER_API_KEY", _MOUSER_KEY)
    MouserProvider().search("LM358DR", 20)
    assert len(wire.calls("search/keyword")) == 4


# ---- DigiKey through the cache ------------------------------------------------

def _digikey(monkeypatch, **more):
    return _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "search/keyword": _fixture("digikey_keyword_lm358dr"),
        "productdetails": _fixture("digikey_details_296-lm358drct-nd"),
        **more})


def test_digikey_details_are_asked_once_and_then_held(monkeypatch, clock):
    wire = _digikey(monkeypatch)
    first = DigiKeyProvider().fetch("296-LM358DRCT-ND")
    clock.forward(3 * _HOUR)
    second = DigiKeyProvider().fetch("296-LM358DRCT-ND")
    assert len(wire.calls("productdetails")) == 1
    assert first["sourcing"]["freshness"] == "live: ProductDetails"
    assert "cache" not in first["sourcing"]
    assert second["sourcing"]["cache"]["hit"] is True
    assert second["sourcing"]["retrieved_at"] == "2027-01-15T08:00:00Z"
    assert second["sourcing"]["freshness"] == (
        "cached: read at 2027-01-15T08:00:00Z and reused until "
        "2027-01-15T12:00:00Z; refresh reads it again. As read then: "
        "live: ProductDetails")
    assert second["sourcing"]["offers"] == first["sourcing"]["offers"]
    assert second["mpn"] == first["mpn"]
    clock.forward(_HOUR)
    DigiKeyProvider().fetch("296-LM358DRCT-ND")
    assert len(wire.calls("productdetails")) == 2


def test_digikey_search_and_details_with_prices_are_kept_four_hours(
        monkeypatch, tmp_path):
    _digikey(monkeypatch)
    DigiKeyProvider().search("LM358DR")
    DigiKeyProvider().fetch("296-LM358DRCT-ND")
    rows = sorted(_rows(tmp_path), key=lambda r: r["kinds"])
    assert [r["kinds"] for r in rows] == ["pricing,product",
                                          "pricing,search"]
    for row in rows:
        assert row["expires_at"] - row["fetched_at"] == 4 * _HOUR


def test_digikey_product_information_alone_is_kept_thirty_days(
        monkeypatch, tmp_path):
    reply = _fixture("digikey_details_296-lm358drct-nd")
    product = reply["Product"]
    for field in ("QuantityAvailable", "UnitPrice"):
        product.pop(field, None)
    for variation in product.get("ProductVariations", []):
        for field in ("StandardPricing", "MyPricing",
                      "QuantityAvailableforPackageType"):
            variation.pop(field, None)
    _digikey(monkeypatch, productdetails=reply)
    DigiKeyProvider().fetch("296-LM358DRCT-ND")
    (row,) = _rows(tmp_path)
    assert row["kinds"] == "product"
    assert row["expires_at"] - row["fetched_at"] == 30 * 24 * _HOUR


def test_digikey_entries_differ_by_locale_currency_and_account(monkeypatch):
    wire = _digikey(monkeypatch)
    DigiKeyProvider().search("LM358DR")
    DigiKeyProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 1
    monkeypatch.setenv("DIGIKEY_LOCALE_CURRENCY", "EUR")
    DigiKeyProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 2
    monkeypatch.setenv("DIGIKEY_LOCALE_SITE", "DE")
    DigiKeyProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 3
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "another-client-id")
    distributors._DIGIKEY_TOKENS.clear()
    DigiKeyProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 4
    # And each is still held under its own setting.
    DigiKeyProvider().search("LM358DR")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", _DIGIKEY_ID)
    monkeypatch.delenv("DIGIKEY_LOCALE_SITE")
    monkeypatch.delenv("DIGIKEY_LOCALE_CURRENCY")
    DigiKeyProvider().search("LM358DR")
    assert len(wire.calls("search/keyword")) == 4


def test_a_digikey_failure_is_not_kept(monkeypatch, tmp_path):
    wire = _digikey(monkeypatch, productdetails=[
        (404, _fixture("digikey_details_missing_404")),
        (404, _fixture("digikey_details_missing_404")),
        _fixture("digikey_details_296-lm358drct-nd")])
    provider = DigiKeyProvider()
    for _ in range(2):
        with pytest.raises(ProviderError):
            provider._details("NO-SUCH-PART-ND")
    assert not _db(tmp_path).exists() or _rows(tmp_path) == []
    assert provider._details("NO-SUCH-PART-ND") is not None
    assert len(wire.calls("productdetails")) == 3


@pytest.mark.parametrize("reply", [None, "text", {"Product": None}, {}])
def test_a_digikey_reply_of_the_wrong_shape_is_not_kept(
        monkeypatch, tmp_path, reply):
    wire = _digikey(monkeypatch, productdetails=reply)
    for _ in range(2):
        assert DigiKeyProvider()._details("296-LM358DRCT-ND") is None
    assert len(wire.calls("productdetails")) == 2
    assert not _db(tmp_path).exists() or _rows(tmp_path) == []


def test_the_two_distributors_do_not_share_entries(monkeypatch, tmp_path):
    wire = _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "api.digikey.com/products/v4/search/keyword":
            _fixture("digikey_keyword_lm358dr"),
        "api.mouser.com/api/v1/search/keyword":
            _fixture("mouser_keyword_lm358dr")})
    for _ in range(2):
        assert DigiKeyProvider().search("LM358DR")[0].provider == "digikey"
        assert MouserProvider().search("LM358DR")[0].provider == "mouser"
    assert len(wire.calls("api.digikey.com/products")) == 1
    assert len(wire.calls("api.mouser.com")) == 1
    assert sorted(r["distributor"] for r in _rows(tmp_path)) == [
        "digikey", "mouser"]
    assert _cache.clear("mouser") == 1
    DigiKeyProvider().search("LM358DR")
    MouserProvider().search("LM358DR")
    assert len(wire.calls("api.digikey.com/products")) == 1
    assert len(wire.calls("api.mouser.com")) == 2


def test_no_credential_is_written_to_the_file(monkeypatch, tmp_path):
    _Wire(monkeypatch, {
        "oauth2/token": _TOKEN,
        "api.digikey.com/products/v4/search/keyword":
            _fixture("digikey_keyword_lm358dr"),
        "productdetails": _fixture("digikey_details_296-lm358drct-nd"),
        "api.mouser.com/api/v1/search/keyword":
            _fixture("mouser_keyword_lm358dr"),
        "api.mouser.com/api/v1/search/partnumber":
            _fixture("mouser_partnumber_595-lm358dr")})
    DigiKeyProvider().search("LM358DR")
    DigiKeyProvider().fetch("296-LM358DRCT-ND")
    MouserProvider().search("LM358DR")
    MouserProvider().fetch("595-LM358DR")
    rows = _rows(tmp_path)
    assert len(rows) == 4
    for row in rows:
        assert "?" not in row["endpoint"] and "apiKey" not in row["endpoint"]
    stored = b""
    for path in tmp_path.glob("cache.sqlite3*"):
        stored += path.read_bytes()
    for secret in (_MOUSER_KEY, _CART_KEY, _DIGIKEY_ID, _DIGIKEY_SECRET,
                   _TOKEN["access_token"]):
        assert secret.encode() not in stored, secret
    # The token exchange is not a catalogue read and is not kept.
    assert all("oauth2" not in row["endpoint"] for row in rows)
    assert _cache.account_fingerprint(_MOUSER_KEY) != \
        _cache.account_fingerprint(_DIGIKEY_ID)
    assert len(_cache.account_fingerprint(_MOUSER_KEY)) == 16


# ---- the tools --------------------------------------------------------------------

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


def test_part_fetch_reuses_a_reply_and_refresh_asks_again(monkeypatch):
    monkeypatch.setenv("EDA_AGENT_PART_PROVIDERS", "mouser")
    wire = _mouser(monkeypatch)
    tools = _tools()
    first = asyncio.run(tools["part_fetch"](part_id="mouser:595-LM358DR"))
    again = asyncio.run(tools["part_fetch"](part_id="mouser:595-LM358DR"))
    assert first["ok"] and again["ok"]
    assert len(wire.calls("search/partnumber")) == 1
    assert "cache" not in first["detail"]["sourcing"]
    assert again["detail"]["sourcing"]["cache"]["hit"] is True
    fresh = asyncio.run(tools["part_fetch"](
        part_id="mouser:595-LM358DR", refresh=True))
    assert len(wire.calls("search/partnumber")) == 2
    assert "cache" not in fresh["detail"]["sourcing"]
    # The same keys as ever, whether or not it was held.
    assert set(first) == set(again) == set(fresh)
    assert set(first["detail"]) == set(again["detail"])


def test_part_search_reuses_a_reply_and_refresh_asks_again(monkeypatch):
    monkeypatch.setenv("EDA_AGENT_PART_PROVIDERS", "mouser")
    wire = _mouser(monkeypatch)
    tools = _tools()
    first = asyncio.run(tools["part_search"](query="LM358DR"))
    again = asyncio.run(tools["part_search"](query="LM358DR"))
    assert first["count"] == again["count"] > 0
    assert len(wire.calls("search/keyword")) == 1
    assert again["hits"][0]["sourcing"]["cache"]["hit"] is True
    asyncio.run(tools["part_search"](query="LM358DR", refresh=True))
    assert len(wire.calls("search/keyword")) == 2
    assert set(first) == set(again)


def test_a_failure_through_the_tools_reads_as_it_always_did(monkeypatch):
    monkeypatch.setenv("EDA_AGENT_PART_PROVIDERS", "mouser")
    monkeypatch.delenv("MOUSER_API_KEY")
    out = asyncio.run(_tools()["part_fetch"](part_id="mouser:595-LM358DR"))
    assert out["ok"] is False and "MOUSER_API_KEY" in out["unavailable"]


# ---- buying is checked against the distributor, never the cache --------------------

def test_a_mouser_cart_check_asks_mouser_whatever_is_held(monkeypatch):
    from eda_agent.libimport.providers.mouser_cart import MouserCart

    line = {"Errors": [], "MouserPartNumber": "595-LM358DR",
            "MfrPartNumber": "LM358DR", "Manufacturer": "Texas Instruments",
            "Quantity": 5, "CartItemCustPartNumber": "",
            "PackagingChoice": "Cut_Tape", "UnitPrice": 0.3,
            "ExtendedPrice": 1.5}
    cart = {"Errors": [], "CartKey": "aaaaaaaa-0000-0000-0000-000000000001",
            "CurrencyCode": "USD", "CartItems": [line]}
    wire = _mouser(monkeypatch, **{"/api/v1/cart": cart})
    # The part is in the cache from an ordinary lookup.
    MouserProvider().fetch("595-LM358DR")
    MouserProvider().fetch("595-LM358DR")
    assert len(wire.calls("search/partnumber")) == 1
    out = MouserCart().create_cart([{"sku": "595-LM358DR", "quantity": 5}])
    assert out["verified"] == 1
    # The check before the cart was a real call, not the held reply.
    assert len(wire.calls("search/partnumber")) == 2
    # And nothing the cart said was put in the cache.
    assert all("/cart" not in row["endpoint"]
               for row in _all_rows(monkeypatch))


def test_a_digikey_list_check_asks_digikey_whatever_is_held(monkeypatch):
    from eda_agent.libimport.providers.digikey_lists import DigiKeyLists

    wire = _digikey(monkeypatch)
    DigiKeyProvider().fetch("296-LM358DRCT-ND")
    DigiKeyProvider().fetch("296-LM358DRCT-ND")
    assert len(wire.calls("productdetails")) == 1
    ready = [{"item": 0, "sku": "296-LM358DRCT-ND"}]
    assert DigiKeyLists().unknown_skus(ready) == []
    assert len(wire.calls("productdetails")) == 2
    assert DigiKeyLists().unknown_skus(ready) == []
    assert len(wire.calls("productdetails")) == 3


def _all_rows(monkeypatch):
    import os

    with sqlite3.connect(os.environ["EDA_AGENT_DISTRIBUTOR_CACHE_FILE"]) \
            as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM responses")]
