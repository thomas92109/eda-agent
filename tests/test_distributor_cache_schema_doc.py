# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""docs/DISTRIBUTOR_CACHE_SCHEMA.md against the database the code makes.

A schema document goes wrong quietly: a column is added and the page
still describes the old table. Each test here creates a database through
the cache's own code, in the test's temporary folder, and compares it
with what the page says.
"""

import contextlib
import pathlib
import re
import sqlite3

import pytest

from eda_agent.libimport.providers import _cache

_DOC = (pathlib.Path(__file__).resolve().parents[1]
        / "docs" / "DISTRIBUTOR_CACHE_SCHEMA.md")


def _text():
    return _DOC.read_text(encoding="utf-8")


def _section(title):
    """The text of one ``## `` section of the page."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)",
                      _text(), re.M | re.S)
    assert match, f"the page has no section {title!r}"
    return match.group(1)


def _sql_blocks(text):
    return [block.strip() for block in
            re.findall(r"```sql\n(.*?)```", text, re.S)]


def _table(text, first_header):
    """The rows of the markdown table whose first header is given."""
    rows, inside = [], False
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0] == first_header:
            inside = True
            continue
        if inside and line.startswith("|"):
            if not set(line) <= set("|-: "):
                rows.append([c.replace("`", "") for c in cells])
        elif inside:
            break
    assert rows, f"the page has no table starting with {first_header!r}"
    return rows


def _squeeze(sql):
    return " ".join(sql.replace("IF NOT EXISTS ", "").split()).rstrip(";")


@pytest.fixture
def database():
    """A database made by the cache's own code, with one row in it."""
    _cache._write("k" * 64, "mouser",
                  "https://api.mouser.com/api/v1/search/partnumber",
                  '{"SearchByPartRequest":{"mouserPartNumber":"X"}}',
                  '{"account":"0123456789abcdef"}', ["pricing", "product"],
                  {"SearchResults": {"Parts": [
                      {"ManufacturerPartNumber": "X-1"}]}}, 1000.0, 2000.0)
    with contextlib.closing(_cache._connect()) as conn:
        yield conn


def test_the_ddl_on_the_page_is_the_ddl_the_code_runs():
    (documented,) = _sql_blocks(_section("SQL DDL"))
    assert [" ".join(s.split()) for s in documented.split(";") if s.strip()] \
        == [" ".join(s.split()) for s in _cache._SCHEMA.split(";")
            if s.strip()]


def test_the_ddl_on_the_page_is_what_the_database_holds(database):
    (documented,) = _sql_blocks(_section("SQL DDL"))
    wanted = sorted(_squeeze(s) for s in documented.split(";") if s.strip())
    held = sorted(_squeeze(row[0]) for row in database.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert held == wanted
    assert [row[0] for row in database.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")] == [
        "responses"]


def test_the_column_table_is_what_table_info_reports(database):
    documented = [
        (name, kind, allowed == "yes", None if default == "none" else default,
         key == "primary key")
        for name, kind, allowed, default, key, _purpose
        in _table(_section("Table definitions"), "Column")]
    actual = [(name, kind, not notnull, default, bool(pk))
              for _cid, name, kind, notnull, default, pk
              in database.execute("PRAGMA table_info(responses)")]
    assert documented == actual
    assert all(purpose for *_, purpose
               in _table(_section("Table definitions"), "Column"))
    assert list(database.execute(
        "PRAGMA foreign_key_list(responses)")) == []


def test_the_index_table_is_what_index_list_reports(database):
    documented = sorted(
        (name, columns.split(", "), unique == "yes")
        for name, columns, unique, _purpose
        in _table(_section("Table definitions"), "Index"))
    actual = sorted(
        (name, [row[2] for row in database.execute(
            f"PRAGMA index_info({name})")], bool(unique))
        for _seq, name, unique, _origin, _partial
        in database.execute("PRAGMA index_list(responses)"))
    assert documented == actual


def test_the_schema_version_and_settings_on_the_page_are_the_real_ones(
        database):
    overview = _section("Database overview")
    stated = re.search(r"\*\*Schema version: (\d+)\*\*", overview)
    assert stated and int(stated.group(1)) == _cache._SCHEMA_VERSION
    settings = {name: value for name, value, _where
                in _table(overview, "Setting")}
    assert database.execute("PRAGMA user_version").fetchone()[0] \
        == int(settings["user_version"]) == _cache._SCHEMA_VERSION
    assert database.execute("PRAGMA journal_mode").fetchone()[0] \
        == settings["journal_mode"].lower() == "wal"
    assert f"{database.execute('PRAGMA busy_timeout').fetchone()[0]} ms" \
        == settings["busy_timeout"]
    assert {1: "NORMAL"}[database.execute(
        "PRAGMA synchronous").fetchone()[0]] == settings["synchronous"]
    assert settings["foreign_keys"] == "not used"
    assert _cache._path().name in overview


def test_the_lifetimes_on_the_page_are_the_real_ones():
    seconds = {"days": 86400, "hours": 3600}
    documented = {}
    for kind, lifetime, _when in _table(_section("Cache lifecycle"), "Kind"):
        count, unit = lifetime.split()
        documented[kind] = int(count) * seconds[unit]
    assert documented == _cache.TTL_SECONDS


def test_every_query_on_the_page_runs(database):
    queries = [q for section in ("Example queries", "Schema verification")
               for q in _sql_blocks(_section(section))]
    assert len(queries) >= 10
    for query in queries:
        assert not re.search(
            r"\b(insert|update|delete|drop|alter|create)\b", query, re.I)
        database.execute(query).fetchall()
    # And the ones that list components find the row that is there.
    listing, lookup = queries[0], queries[1]
    database.execute("UPDATE responses SET expires_at = unixepoch() + 60")
    assert database.execute(listing).fetchall() == [
        ("mouser", "X", "pricing,product")]
    assert database.execute(lookup).fetchall()[0][:2] == ("mouser", "X-1")
