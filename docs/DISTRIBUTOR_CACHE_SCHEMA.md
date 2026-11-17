# Distributor cache: database schema

This describes the SQLite database behind the cache of DigiKey and Mouser
replies. What the cache is for and how to use it from the tools is in
[PART_SOURCING.md](PART_SOURCING.md#the-cache-of-distributor-replies). The
code is `src/eda_agent/libimport/providers/_cache.py`.

`tests/test_distributor_cache_schema_doc.py` reads this file and fails when
the DDL, the column table, the schema version or an example query below no
longer matches the database the code creates. Change the code and this file
together.

## Database overview

**Purpose.** One row per reply to a read-only catalogue call (DigiKey keyword
search and product details, Mouser keyword and part-number search), so that
asking about the same part again within its lifetime costs no API call.
Nothing else is stored: no carts, no saved lists, no tokens, no settings.

**Location.** One file, shared by both distributors, outside the repository:

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\eda-agent\distributor-cache.sqlite3` |
| Elsewhere | `~/.cache/eda-agent/distributor-cache.sqlite3` |

`EDA_AGENT_DISTRIBUTOR_CACHE_FILE` names another file.
`EDA_AGENT_DISTRIBUTOR_CACHE=0` turns the cache off, and then the file is
neither opened nor created. In WAL mode SQLite keeps two more files beside
it while it is in use, with the same name plus `-wal` and `-shm`.

**SQLite configuration.**

| Setting | Value | Where it is set |
|---|---|---|
| `journal_mode` | `WAL` | Once per process and file. It is a property of the file and persists |
| `busy_timeout` | `5000` ms | Every connection |
| `synchronous` | `NORMAL` | Every connection |
| `foreign_keys` | not used | There is one table and no foreign key |
| `user_version` | `1` | When the table is created |

Each cache operation opens its own connection and closes it. Nothing is
shared between threads or processes except the file, and SQLite's locking
keeps writers apart: a writer that finds the file locked waits up to the busy
timeout. Writes run in a `BEGIN IMMEDIATE` transaction.

**Schema version: 1**, held in `PRAGMA user_version`.

**Migration strategy: there is none, on purpose.** The file holds nothing
that cannot be fetched again. When the code opens a file whose `user_version`
is not the version it expects, it drops the `responses` table, sets
`user_version`, and creates the table anew. The cost is one cold cache. To
change the schema, change the DDL and raise `_SCHEMA_VERSION` in `_cache.py`,
and update this file.

## Table definitions

There is one table.

### `responses`

| Column | Type | Null allowed | Default | Key | Purpose |
|---|---|---|---|---|---|
| `key` | TEXT | yes | none | primary key | SHA-256, as 64 hex characters, of distributor, endpoint, request and context. See [Cache key generation](#cache-key-generation) |
| `distributor` | TEXT | no | none | | `digikey` or `mouser` |
| `endpoint` | TEXT | no | none | | The address called, without its query string |
| `request` | TEXT | no | none | | The call's parameters as normalized JSON |
| `context` | TEXT | no | none | | What else changes the answer, as normalized JSON: the account fingerprint and, for DigiKey, the locale settings |
| `kinds` | TEXT | no | none | | What the reply contains, sorted and comma separated, from `product`, `search` and `pricing`. It decided the lifetime |
| `response` | TEXT | no | none | | The distributor's reply, as JSON, exactly as parsed |
| `fetched_at` | REAL | no | none | | When the reply was read from the distributor: Unix time in seconds, UTC |
| `expires_at` | REAL | no | none | | When the row stops being used: `fetched_at` plus the lifetime |

Notes on the constraints:

- `key` is declared `TEXT PRIMARY KEY` without `NOT NULL`, and SQLite allows
  NULL in such a column. The code always writes a key, so it is never NULL in
  practice.
- No column has a default. Every insert supplies all nine.
- There is no foreign key, no `UNIQUE` constraint other than the primary key,
  and no `CHECK`.

**Indexes.**

| Index | Columns | Unique | Purpose |
|---|---|---|---|
| `sqlite_autoindex_responses_1` | `key` | yes | Made by SQLite for the primary key; the lookup of every cache read |
| `responses_expiry` | `expires_at` | no | Removing expired rows |

## SQL DDL

These are the statements the code runs, from `_SCHEMA` in `_cache.py`:

```sql
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
```

SQLite stores them in `sqlite_master` without the `IF NOT EXISTS`.

## Cache key generation

```
key = SHA-256( distributor + US + endpoint + US + request + US + context )
```

`US` is the unit separator character (0x1F), and the result is written as 64
lowercase hex characters. Two calls share a row only when all four parts are
the same.

**Normalization.** `request` and `context` are each written as JSON with the
keys of every object sorted, no spaces, and non-ASCII characters kept as they
are. So the order in which parameters were given makes no difference, and any
difference in a value makes a different key. Values are not otherwise
changed: text keeps its case and its spacing.

**What each part holds.**

| Part | DigiKey | Mouser |
|---|---|---|
| `distributor` | `digikey` | `mouser` |
| `endpoint` | `https://api.digikey.com/products/v4/search/keyword`, or `https://api.digikey.com/products/v4/search/<number>/productdetails` | `https://api.mouser.com/api/v1/search/keyword` or `.../search/partnumber` |
| `request` | Search: `{"Keywords", "Limit", "Offset"}`. Product details: `{}`, because the number is in the endpoint | The request body: `{"SearchByKeywordRequest": {"keyword", "records", "startingRecord"}}` or `{"SearchByPartRequest": {"mouserPartNumber", "partSearchOptions"}}` |
| `context` | `{"account", "locale": {"DIGIKEY_LOCALE_SITE", "DIGIKEY_LOCALE_LANGUAGE", "DIGIKEY_LOCALE_CURRENCY"}}` | `{"account"}` |

**How each concern reaches the key.**

| Concern | How it is in the key |
|---|---|
| Distributor | The `distributor` part. The two never share a row |
| Endpoint | The `endpoint` part. A keyword search and an exact lookup of the same text are different rows |
| Quantity | Neither distributor's search takes an order quantity: a reply carries the whole price ladder. The quantities that are sent, the number of results asked for (`Limit`, `records`) and the offset, are in `request` |
| Currency and locale | DigiKey: the three locale settings, upper-cased, in `context`; an unset one is an empty string. Mouser's Search API takes no locale or currency: they follow the account |
| Account | `context.account`, a fingerprint of the credential the call was made with. Prices follow the account, so another key gets its own rows |

**How credentials are excluded.**

- The `endpoint` is the address without its query string. Mouser's key
  travels in the query (`?apiKey=...`) and is added only when the request is
  sent, after the key was computed.
- DigiKey's bearer token, client id and client secret travel in headers and
  in the token exchange. Headers are not part of the key or the row, and the
  token exchange does not go through the cache.
- `context.account` is the first 16 hex characters of the SHA-256 of a fixed
  label followed by the credential (Mouser's API key, DigiKey's client id).
  It tells accounts apart and cannot be turned back into the credential.
- `response` is the reply body. Neither distributor's catalogue reply
  contains a credential.

A test writes replies through both providers and then searches the bytes of
the database for every configured key, secret and token.

## Cache lifecycle

**Lifetimes.** What a reply contains decides how long its row is used:

| Kind | Lifetime | When a reply has it |
|---|---|---|
| `product` | 30 days | An exact product lookup: DigiKey product details, Mouser part-number search |
| `search` | 24 hours | A keyword search |
| `pricing` | 4 hours | Any product in the reply states a price or a stock figure |

A reply with several kinds gets the shortest lifetime. Both distributors send
prices and stock with every product, so nearly every row is
`pricing,product` or `pricing,search` and lasts 4 hours.

**Miss.** No row has the key, or the row has expired. The distributor is
called. If the call succeeds and the reply is one to keep, it is written with
`INSERT OR REPLACE`, and in the same transaction every expired row in the
table is deleted. The caller gets the reply as it always did.

**Hit.** A row has the key and `expires_at` is later than now. The stored
reply is returned and the distributor is not called. Hits built from it are
marked: `sourcing.cache` is `{hit, fetched_at, expires_at}`,
`sourcing.retrieved_at` is the stored `fetched_at`, and `sourcing.freshness`
starts with `cached:`.

**Expiry.** A row whose `expires_at` has passed is never returned, whether or
not it has been deleted yet. Expired rows are deleted when the next row is
written. Nothing deletes them on a schedule and there is no size limit.

**Forced refresh.** `part_search` and `part_fetch` take `refresh=true`.
Inside a forced refresh the read is skipped and the distributor is called;
the new reply replaces the row, so the next ordinary call gets the newer
data. The checks made before a part goes into a Mouser cart or a DigiKey
list always run this way, whatever the caller asked for.

**Not stored.**

- A call that failed: an HTTP error, a timeout, a rate limit. The error
  reaches the caller unchanged.
- An error delivered under HTTP 200, which Mouser does for a refused key.
- A reply that is not the expected shape.
- An exact Mouser lookup that found no part, so that "no such part" is asked
  again next time. A keyword search that found nothing is kept, as a search.
- Token exchanges, and every call of the cart and saved-list modules. Those
  do not go through the cache at all.

**Database errors.** The cache may not cost an answer:

| What goes wrong | What happens |
|---|---|
| The file cannot be opened or created | The distributor is called, on every call, as if there were no cache |
| The file is not a SQLite database | The same. The file is left as it is |
| A read fails (locked past the timeout, I/O error) | Treated as a miss |
| A write fails | The reply is still returned; it is just not kept |
| A row's `response` is not valid JSON | The row is deleted and the call is a miss |
| The file is from another schema version | The table is dropped and recreated |

After a failed read or write the file is prepared again on the next call, so
a file that was deleted or replaced while the process ran is picked up.

To start over, delete the file (and its `-wal` and `-shm` companions) while
nothing is using it, or run
`python -m eda_agent.libimport.providers._cache clear`.

## Example queries

Open the file with any SQLite client, for example
`sqlite3 "%LOCALAPPDATA%\eda-agent\distributor-cache.sqlite3"`. These only
read. They use `unixepoch()` and the JSON functions, which need SQLite 3.38
or later.

List cached components, by what was asked for:

```sql
SELECT distributor,
       COALESCE(
           json_extract(request, '$.SearchByPartRequest.mouserPartNumber'),
           json_extract(request, '$.SearchByKeywordRequest.keyword'),
           json_extract(request, '$.Keywords'),
           replace(replace(endpoint,
               'https://api.digikey.com/products/v4/search/', ''),
               '/productdetails', '')) AS asked_for,
       kinds
  FROM responses
 WHERE expires_at > unixepoch()
 ORDER BY distributor, asked_for;
```

The manufacturer part number an exact lookup returned:

```sql
SELECT distributor,
       COALESCE(
           json_extract(response, '$.Product.ManufacturerProductNumber'),
           json_extract(response,
               '$.SearchResults.Parts[0].ManufacturerPartNumber')) AS mpn,
       datetime(fetched_at, 'unixepoch') AS fetched_utc
  FROM responses
 WHERE kinds LIKE '%product%'
 ORDER BY distributor, mpn;
```

View entries with their expiration:

```sql
SELECT distributor, endpoint, request, kinds,
       datetime(fetched_at, 'unixepoch') AS fetched_utc,
       datetime(expires_at, 'unixepoch') AS expires_utc,
       CAST((expires_at - unixepoch()) / 60 AS INTEGER) AS minutes_left
  FROM responses
 ORDER BY expires_at;
```

Count entries by distributor:

```sql
SELECT distributor,
       COUNT(*) AS entries,
       SUM(expires_at > unixepoch()) AS live,
       SUM(expires_at <= unixepoch()) AS expired,
       SUM(length(response)) AS response_characters
  FROM responses
 GROUP BY distributor;
```

Find expired entries that have not been removed yet:

```sql
SELECT distributor, endpoint, request,
       datetime(expires_at, 'unixepoch') AS expired_utc
  FROM responses
 WHERE expires_at <= unixepoch()
 ORDER BY expires_at;
```

Inspect the most recent updates:

```sql
SELECT datetime(fetched_at, 'unixepoch') AS fetched_utc,
       distributor, endpoint, request, kinds, length(response) AS characters
  FROM responses
 ORDER BY fetched_at DESC
 LIMIT 20;
```

Check the size of the database:

```sql
SELECT page_count * page_size AS bytes,
       freelist_count * page_size AS unused_bytes
  FROM pragma_page_count(), pragma_page_size(), pragma_freelist_count();
```

## Schema verification

The schema above was checked against a database the code had created, on
2026-11-16, after the live test that left one DigiKey and one Mouser row in
it. To repeat the check:

```sql
SELECT type, name, sql FROM sqlite_master ORDER BY type DESC, name;
```

```sql
PRAGMA table_info(responses);
```

```sql
PRAGMA index_list(responses);
```

What they returned:

- `sqlite_master` held the `responses` table, the `responses_expiry` index,
  and `sqlite_autoindex_responses_1` with no SQL of its own. The table's and
  the index's SQL were the statements under [SQL DDL](#sql-ddl) without
  `IF NOT EXISTS`.
- `PRAGMA table_info` listed the nine columns in the order of the table
  above, `key` as the primary key and nullable, the other eight `NOT NULL`,
  no defaults, types as documented.
- `journal_mode` was `wal` and `user_version` was `1`.

The same check runs as a test, `tests/test_distributor_cache_schema_doc.py`.
It creates a database through the code and compares it with this file: the
DDL block against `_SCHEMA` and against `sqlite_master`, the column table
against `PRAGMA table_info`, the index table against `PRAGMA index_list`, the
stated schema version against `_SCHEMA_VERSION` and `user_version`, the
stated settings against the pragmas, and it runs every query in this file.
