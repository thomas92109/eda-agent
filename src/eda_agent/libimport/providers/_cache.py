# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A cache of distributor replies, kept in one SQLite file.

DigiKey and Mouser each allow about 1,000 calls a day, and a BOM asks
about the same parts again and again. A reply to a read-only catalogue
call is kept here and given back until it is too old to trust.

What is kept, and for how long, is decided by what a reply CONTAINS:

* product information (identity, datasheet, lifecycle): 30 days;
* search results: 24 hours;
* pricing or availability: 4 hours.

A reply that holds several of these lives as long as the shortest. That
is the common case: both distributors return prices and stock with
every product, so most entries last 4 hours.

What is never kept: a failed call, an error delivered under HTTP 200, a
reply of the wrong shape, an exact lookup that found nothing, and
anything that changes state (a cart, a saved list, a token exchange).
Those do not come through here at all.

No credential is written. The address is stored without its query,
which is where Mouser's key travels. The account a reply was priced
for is told apart by a short one-way fingerprint of the key, not by the
key.

The cache is never allowed to cost an answer. If the file cannot be
opened, read or written, the call goes to the distributor as if there
were no cache.

``python -m eda_agent.libimport.providers._cache stats`` shows what is
held; ``... clear`` empties it.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

__all__ = [
    "PRICING", "PRODUCT", "SEARCH", "TTL_SECONDS", "account_fingerprint",
    "cache_only", "cache_only_active", "cached_call", "clear", "enabled",
    "forcing_fresh", "fresh", "stats", "ttl_for",
]

_LOG = logging.getLogger(__name__)

PRODUCT = "product"
SEARCH = "search"
PRICING = "pricing"

#: How long a reply holding each kind of content may be reused.
TTL_SECONDS = {
    PRODUCT: 30 * 24 * 3600,
    SEARCH: 24 * 3600,
    PRICING: 4 * 3600,
}

#: Bumped when the table changes; an older file is emptied, not migrated.
_SCHEMA_VERSION = 1
_BUSY_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key         TEXT PRIMARY KEY,
    distributor TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    request     TEXT NOT NULL,
    context     TEXT NOT NULL,
    kinds       TEXT NOT NULL,
    response    TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS responses_expiry ON responses (expires_at);
"""

#: Cache files this process has already prepared.
_READY: set[str] = set()

#: Set while a caller wants the distributor asked, whatever is held.
_FRESH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "eda_agent_distributor_cache_fresh", default=False)


def ttl_for(kinds: Iterable[str]) -> int:
    """Seconds a reply with these kinds of content may be reused: the
    shortest of them."""
    return min(TTL_SECONDS[kind] for kind in kinds)


def enabled() -> bool:
    """False when ``EDA_AGENT_DISTRIBUTOR_CACHE`` turns the cache off."""
    return os.environ.get("EDA_AGENT_DISTRIBUTOR_CACHE", "").strip().lower() \
        not in ("0", "off", "false", "no")


@contextlib.contextmanager
def fresh(force: bool = True) -> Iterator[None]:
    """Within this block every call asks the distributor.

    What comes back still replaces what was held, so the next ordinary
    call gets the newer reply. Used for a caller's ``refresh`` and for
    every check made before something is put in a cart or a list.
    """
    token = _FRESH.set(bool(force) or _FRESH.get())
    try:
        yield
    finally:
        _FRESH.reset(token)


def forcing_fresh() -> bool:
    return _FRESH.get()


#: Set while a caller wants held replies only, and no distributor asked.
_CACHE_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "eda_agent_distributor_cache_only", default=False)


@contextlib.contextmanager
def cache_only(strict: bool = True) -> Iterator[None]:
    """Within this block no distributor is asked: STRICT CACHE-ONLY.

    A held reply is given back as usual. Anything else raises
    ``base.CacheMiss`` without calling ``fetch``: a reply not held, an
    expired one, a cache that is turned off or cannot be read. It wins
    over ``fresh``. The distributors' transport refuses too
    (``_distributor.DistributorProvider._request``), so a call that does
    not come through here (a token exchange) is not sent either.
    """
    token = _CACHE_ONLY.set(bool(strict) or _CACHE_ONLY.get())
    try:
        yield
    finally:
        _CACHE_ONLY.reset(token)


def cache_only_active() -> bool:
    return _CACHE_ONLY.get()


def _miss(distributor: str, why: str) -> Exception:
    from eda_agent.libimport.providers.base import CacheMiss

    return CacheMiss(f"{distributor}: {why} (cache_only: no request was "
                     "sent)")


def account_fingerprint(*credentials: str) -> str:
    """A short one-way mark of the account a reply was made for.

    Prices follow the account behind a key, so two keys must not share
    an entry. The mark is 16 hex characters of a SHA-256: enough to tell
    accounts apart, nothing a key can be recovered from.
    """
    joined = "\x1f".join(str(c or "") for c in credentials)
    return hashlib.sha256(
        b"eda-agent distributor cache\x1f"
        + joined.encode("utf-8")).hexdigest()[:16]


def _path() -> Path:
    named = os.environ.get("EDA_AGENT_DISTRIBUTOR_CACHE_FILE", "").strip()
    if named:
        return Path(named)
    root = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(root) if root else Path.home() / ".cache"
    return base / "eda-agent" / "distributor-cache.sqlite3"


def _connect() -> sqlite3.Connection:
    """A connection of this call's own, with the table in place.

    One connection per operation: nothing is shared between threads or
    processes but the file, and SQLite's own locking, in WAL mode with a
    busy timeout, is what keeps concurrent writers apart.
    """
    path = _path()
    name = str(path)
    if name not in _READY:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(name, timeout=_BUSY_MS / 1000,
                           isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        if name not in _READY:
            # Once per process and file: WAL is a property of the file
            # and stays; the table is made, or remade when the file is
            # from another version of this module.
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != _SCHEMA_VERSION:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DROP TABLE IF EXISTS responses")
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                conn.execute("COMMIT")
            conn.executescript(_SCHEMA)
            _READY.add(name)
    except BaseException:
        conn.close()
        _READY.discard(name)
        raise
    return conn


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _key(distributor: str, endpoint: str, request: str, context: str) -> str:
    return hashlib.sha256("\x1f".join(
        (distributor, endpoint, request, context)).encode("utf-8")).hexdigest()


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _read(key: str, now: float) -> Optional[tuple[Any, float, float]]:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT response, fetched_at, expires_at FROM responses "
            "WHERE key = ? AND expires_at > ?", (key, now)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0]), float(row[1]), float(row[2])
        except (ValueError, TypeError):
            # A row that cannot be read is not an answer. Drop it.
            conn.execute("DELETE FROM responses WHERE key = ?", (key,))
            return None


def _write(key: str, distributor: str, endpoint: str, request: str,
           context: str, kinds: list[str], payload: Any,
           now: float, expires: float) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO responses (key, distributor, endpoint, "
            "request, context, kinds, response, fetched_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, distributor, endpoint, request, context,
             ",".join(kinds), text, now, expires))
        conn.execute("DELETE FROM responses WHERE expires_at <= ?", (now,))
        conn.execute("COMMIT")


def cached_call(
    distributor: str,
    endpoint: str,
    request: Any,
    context: dict[str, Any],
    fetch: Callable[[], Any],
    *,
    kinds: Callable[[Any], Optional[Iterable[str]]],
) -> tuple[Any, Optional[dict[str, Any]]]:
    """The reply to one read-only call, from the cache when it holds one.

    Returns ``(reply, None)`` when the distributor was asked, and
    ``(reply, {"fetched_at", "expires_at"})`` when the reply was held.

    ``request`` is the call's parameters and ``context`` whatever else
    changes the answer (locale, currency, the account's fingerprint);
    both are part of the key, with ``distributor`` and ``endpoint``.
    ``kinds`` reads a fresh reply and says what it contains, which sets
    how long it is kept; None means "not something to keep".

    ``fetch`` raising is not caught: a failed call is never stored and
    reaches the caller exactly as it would without a cache.

    Under ``cache_only`` ``fetch`` is never called: what is not held
    raises ``base.CacheMiss``.
    """
    strict = cache_only_active()
    if not enabled():
        if strict:
            raise _miss(distributor, "the distributor cache is turned off")
        return fetch(), None
    try:
        wanted = _canonical(request)
        setting = _canonical(context)
        key = _key(distributor, endpoint, wanted, setting)
    except Exception as exc:  # noqa: BLE001 - never cost an answer
        _LOG.debug("distributor cache: no key (%s)", exc)
        if strict:
            raise _miss(distributor, "the request has no cache key") from exc
        return fetch(), None

    if strict or not forcing_fresh():
        try:
            held = _read(key, time.time())
        except (sqlite3.Error, OSError) as exc:
            _LOG.debug("distributor cache: read failed (%s)", exc)
            # The file may have been replaced or emptied under us:
            # prepare it again next time.
            _READY.discard(str(_path()))
            held = None
        if held is not None:
            payload, fetched_at, expires_at = held
            return payload, {"fetched_at": _iso(fetched_at),
                             "expires_at": _iso(expires_at)}
    if strict:
        raise _miss(distributor, "not in the cache, or expired")

    payload = fetch()
    try:
        found = kinds(payload)
        content = sorted(set(found)) if found else []
        if content:
            now = time.time()
            _write(key, distributor, endpoint, wanted, setting, content,
                   payload, now, now + ttl_for(content))
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        _LOG.debug("distributor cache: write failed (%s)", exc)
        _READY.discard(str(_path()))
    return payload, None


def stats() -> dict[str, Any]:
    """What the cache holds now. Counts and times only."""
    out: dict[str, Any] = {"file": str(_path()), "enabled": enabled()}
    try:
        with contextlib.closing(_connect()) as conn:
            now = time.time()
            out["entries"] = [
                {"distributor": row[0], "endpoint": row[1], "kinds": row[2],
                 "live": row[3], "expired": row[4]}
                for row in conn.execute(
                    "SELECT distributor, endpoint, kinds, "
                    "SUM(expires_at > ?), SUM(expires_at <= ?) "
                    "FROM responses GROUP BY distributor, endpoint, kinds "
                    "ORDER BY distributor, endpoint", (now, now))]
    except (sqlite3.Error, OSError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def clear(distributor: str = "") -> int:
    """Forget every held reply, or one distributor's. Returns how many."""
    with contextlib.closing(_connect()) as conn:
        if distributor:
            cursor = conn.execute(
                "DELETE FROM responses WHERE distributor = ?",
                (distributor,))
        else:
            cursor = conn.execute("DELETE FROM responses")
        return cursor.rowcount


def _main(argv: Optional[list[str]] = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["stats"]:
        print(json.dumps(stats(), indent=1))
        return 0
    if args[:1] == ["clear"]:
        print(json.dumps({"removed": clear(*args[1:2])}))
        return 0
    print("usage: python -m eda_agent.libimport.providers._cache "
          "stats | clear [digikey|mouser]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
