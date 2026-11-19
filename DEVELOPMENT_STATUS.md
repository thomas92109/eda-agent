# Development status: distributor sourcing and the purchasing BOM

Last updated 2026-11-19, as a handover for the next working session. This is
a working note for the fork. Leave it out of any pull request meant for
upstream. Keep real project names, paths, designators and counts out of it.

## Where things stand (handoff)

- Repository `C:\Projects\eda-agent`, fork `thomas92109/eda-agent` (`origin`
  is the only remote configured; there is no `upstream` remote).
- All the work below is one squash commit on `main`, on top of `e5f3469`
  (version 0.8.2), published to `origin/main`. The remote branch
  `feature/distributor-pricing` still exists, at an older tip with the
  original, unsquashed history, and so does its local copy (6 commits ahead
  of it). Neither has been touched or deleted.
- The repository has a local-only author email; the global one is a company
  address. Check `git config --show-scope --get-regexp user` before
  committing.
- No pull request has been opened.

### Completed

- DigiKey and Mouser part data (stock, lifecycle, price breaks, restock
  dates) with a local cache; DigiKey MyLists; Mouser carts.
- Purchasing BOM, plan, CSV export, MPN decisions and close matches, verified
  manufacturer aliases, cart prepare and commit (`part_purchasing_*`),
  configured by `config.toml` in `EDA_AGENT_EXPORT_DIR`.
- Privacy safeguards: export-folder guard, `.gitignore`, README and
  `CLAUDE.md` rules, synthetic examples.
- `docs/INSTALL.md`, an agent-guided install, linked from README and
  `CLAUDE.md`.

### Known issues

- Four tests fail on a clean `main` as well and are unrelated:
  `test_bridge_handlers_reachable::test_scan_finds_the_bundle_is_excluded`
  and three `test_updates` launcher tests (permission errors).
- `proj_get_bom` in purchasing mode and the `part_purchasing_*` tools are not
  runnable in the Altium simulator. `sch_preview_template` is answered by it
  but gives wrong results there (no pin geometry), so it is not labelled
  simulator-covered.
- `docs/INSTALL.md` was checked against the CLI but never run end to end on a
  clean machine. The README link to it works only once it is on GitHub `main`.
- The background update check compares with the upstream project, not the
  fork.
- The remote feature branch's older history still carries an earlier author
  email and, in two commits, a real project name with aggregate figures. Not
  rewritten, by decision.

## Session 6: Mouser cart and the distributor cache

Both pieces are in one commit on top of `b6746b0`, pushed to `origin`.

### Mouser cart

Three tools, `part_cart_create`, `part_cart_add` and `part_cart_get`, in
`src/eda_agent/libimport/providers/mouser_cart.py`. They use
`MOUSER_CART_API_KEY`, which is not the Search API key. Scope is the cart
only: nothing touches Mouser's Order API, a payment or a checkout.

- A cart line has four fields in Mouser's API description:
  `MouserPartNumber`, `Quantity`, `CustomerPartNumber` (21 characters, no
  `*`) and `PackagingChoice`. **There is no field for reference designators
  or notes.** Designators, description, manufacturer and MPN are returned
  with each item and never sent.
- Before an insert each `sku` is read from the Search API: it must be a
  Mouser part number exactly, and the quantity must meet its minimum and
  multiple. Nothing is rounded.
- After an insert the cart is read back by its key and each item is
  compared. `part_cart_add` needs an explicit `cart_key` and never makes a
  cart.
- **Live, 2026-11-16:** one cart, one part (a 10 kΩ 0603 resistor, quantity
  10, an 18-character customer part number). Created on the first attempt
  with an empty `CartKey`; part number, quantity, customer part number,
  manufacturer and MPN came back as sent; Mouser chose `Cut_Tape` when no
  packaging was asked for. The test cart was left in place.
- **Live, 2026-11-17:** a part already in a cart is added to, not replaced:
  a line at 10 with 10 more added reads back as one line at 20. Two parts
  made two lines, and the totals were correct. Still not seen live: what
  Mouser does with a quantity it does not accept. No tool lowers a quantity
  or removes a line.
- **Interoperability limitation, reproduced 2026-11-17:** a cart with two
  lines (quantity 20 and 5, total $0.84) was opened in a browser, not edited,
  and left with the Back button. The next API readback of the same cart key
  succeeded with no error but showed zero items and $0.00. This is not
  confirmed as a Mouser API bug, and its cause is unknown. Until it is
  understood, finish API changes and checks before opening a cart on the
  website, and check the site before retrying an insert. Details are in
  `docs/PART_SOURCING.md` under "A cart at Mouser".
- Tests: `tests/test_mouser_cart.py`, 25.

### Distributor cache

DigiKey and Mouser catalogue replies are kept in one SQLite file,
`%LOCALAPPDATA%\eda-agent\distributor-cache.sqlite3`, by
`src/eda_agent/libimport/providers/_cache.py`. The schema, the key and the
lifecycle are in `docs/DISTRIBUTOR_CACHE_SCHEMA.md`.

- **Where it sits:** around the raw reply in `DigiKeyProvider._read` (keyword
  search and product details) and `MouserProvider._post` (keyword and
  part-number search). Nothing else goes through it: not the token exchange,
  not MyLists, not the cart.
- **One table, `responses`:** key, distributor, endpoint, request, context,
  kinds, response, fetched_at, expires_at. WAL, 5 s busy timeout, one
  connection per operation, `user_version` 1. A file of another version is
  emptied, not migrated.
- **Key:** SHA-256 of distributor, endpoint without its query, the request as
  sorted JSON, and a context holding DigiKey's locale settings and a 16-hex
  fingerprint of the credential. No credential is stored.
- **Lifetime by content:** product 30 days, search 24 hours, pricing or
  availability 4 hours, shortest wins. Every real reply carries prices, so
  rows last 4 hours.
- **Forced fresh:** `part_search` and `part_fetch` take `refresh`. The checks
  before a cart insert and a list add always read live
  (`_cache.fresh()`).
- **`part_fetch` is no longer always live.** It reuses a reply for up to 4
  hours; a reused hit has `sourcing.cache` and a `freshness` starting with
  `cached:`.
- **Failure:** any SQLite or file error is a miss; the distributor is called.
- **Tests never use the real file:** an autouse fixture in
  `tests/conftest.py` turns the cache off and points its file into the
  test's temporary folder. `tests/test_distributor_cache.py` turns it on for
  itself.
- **Schema document is tested:** `tests/test_distributor_cache_schema_doc.py`
  compares the page's DDL, column and index tables, version, settings and
  lifetimes with a database the code creates, and runs every query on it.
- **Live, 2026-11-16:** one part at each distributor through `part_fetch`:
  first lookup 1 call (DigiKey 2, with the token), repeat 0 calls,
  `refresh=true` 1 call, repeat 0 calls. The file held no key, secret or
  token.
- Tests: 59 in `test_distributor_cache.py`, 7 in the schema test; 947 passed
  with the related distributor, cart, list and tool-surface tests. The full
  suite was not run.

### Open

- Reconnect the altium MCP server to load both.
- DigiKey part numbers are keyed as written, so letter case makes separate
  rows.
- Expired rows go only when something is written; there is no size limit.
- The purchasing BOM does not call the distributors yet.

## Session 5: DigiKey MyLists works live (create, add, read back)

### Live result

Run against the owner's DigiKey account on 2026-11-16, with the owner's
approval for one test list and one test part.

- **List creation works.** The 400 from session 4 was DigiKey requiring
  `ListSettings.ColumnPreferences`, which its Swagger file does not mark as
  required. Sending an empty array (`[]`) is accepted. A private list named
  `EDA Agent API Test` was created on the first attempt.
- **Adding a part works.** One part was added on the first attempt: the
  cut-tape number of a 10 kΩ 0603 resistor, quantity 10, ten designators,
  and a description marked as test data.
- **Read-back:** requested number, quantity, notes, manufacturer, MPN and
  `ReferenceDesignator` (58 characters) came back exactly as sent.
  `CustomerReference` came back cut to its first 48 characters.
- The test list and its one part are still in the account. No tool deletes
  a list; remove it on DigiKey's site when it is no longer wanted.

### What the live run showed, and what changed because of it

- **Exact SKU.** A list part's own `DigiKeyPartNumber` is the product's
  base number (the reel, for this part), whatever packaging was entered.
  The packaging in the list is
  `Quantities[SelectedQuantityIndex].PackOptions[SelectedPackOptionIndex]`,
  and that entry's `DigiKeyPartNumber` is the orderable number. `sku` is now
  read from there and the base number is returned as `base_part_number`. An
  item is `verified` only when the selected number is the one sent; when
  DigiKey names no selected packaging it is not verified and
  `sku_unverified` says why.
- **All lists.** `part_list_get` without a list id returned 50 of the
  account's 84 lists and did not say so. It now reads 100 at a time until
  an empty page, at most 20 requests, and returns `complete`, with
  `incomplete` giving the reason when it stopped early. `startIndex` is an
  item offset (checked read-only).
- **Customer reference.** Because DigiKey keeps 48 characters, the
  designators go in joined by commas without spaces: all of them when they
  fit, otherwise as many whole ones as fit before a closing `,+MORE`
  (`+MORE` alone when not even the first fits). The designators left out
  are returned in `customer_reference_omitted` with a
  `customer_reference_warning`; `designators` in the result and DigiKey's
  `ReferenceDesignator` keep the full text as given. A `customer_reference`
  given outright is fitted the same way only when it is a designator list;
  any other wording over 48 characters is refused, not shortened.

### Files

- `src/eda_agent/libimport/providers/digikey_lists.py`
- `src/eda_agent/tools/parts.py` (docstrings, and `part_list_get` returns
  the paged result)
- `tests/test_digikey_lists.py` (53 tests)
- `docs/PART_SOURCING.md`, `DEVELOPMENT_STATUS.md`

No DelphiScript change and no bridge redeployment.

### Test results

Only `tests/test_digikey_lists.py` was run for the final change: 53 passed.

### Remaining limitations

- **The 48-character limit is one observation**, not a published figure,
  and it is what DigiKey's forum gives for the bag label. It is one
  constant, `CUSTOMER_REFERENCE_CHARS`.
- **The `,+MORE` form has not been sent to DigiKey.** It was written after
  the live run and is covered by offline tests only. The test part in the
  account still holds the cut value from before.
- **Privacy is not confirmed.** The list was requested as `Private`. The
  all-lists endpoint reports `private`, but it reports that for every list
  in the account. The single-list endpoint reports `readOnly`, and reports
  `readOnly` or `readWrite` for the owner's other lists, never private. The
  API has no call to change it. Check the list's sharing on DigiKey's site.
- **Whether the customer reference reaches the bag label** is still known
  only from DigiKey's forum.
- A designator is recognised as up to six letters, digits and at most one
  trailing letter. A given `customer_reference` with names such as `J1_2`
  counts as free wording.
- The MCP server must be reconnected to load this code.

## Session 4: DigiKey MyLists (as first written; see session 5)

Session 5 changed the reference handling and the SKU check described
here and resolved the failing list creation. Where the two disagree,
session 5 is current.

### Completed

Four tools create a saved list in a DigiKey account, add parts to it and
read it back. Scope is MyLists only: no cart, quote, checkout or ordering,
and no connection to the Altium BOM yet.

- `part_list_login`: three-legged OAuth sign-in in two steps (get the
  address, pass back the address the browser ended on). A terminal version
  exists for when the code's one minute is too short:
  `python -m eda_agent.libimport.providers.digikey_lists login`.
- `part_list_create`: private by default; settings ask DigiKey to keep the
  entered part number, quantity and reference.
- `part_list_add`: items of `sku`, `quantity`, `designators`, optional
  `customer_reference`, `reference_designator`, `description`,
  `manufacturer`. Reads the list back and reports per-item `differences`.
- `part_list_get`: one list with its items, or all lists.

Behaviour worth knowing:

- **References:** all designators, comma separated, go into both
  `CustomerReference` and `ReferenceDesignator`. Nothing is shortened. A
  customer reference over 48 characters gets a warning.
- **Description:** the Altium `Description` goes into the part's `Notes`,
  because DigiKey's request has no description field. Any field outside the
  seven above, `comment` included, refuses the call.
- **Invalid products:** each distinct `sku` is read from Product Information
  first; one that is not a DigiKey part number refuses the whole call with
  nothing added. A manufacturer number is not accepted as a `sku`.
- **Tokens:** access 30 minutes, refresh 90 days and replaced at every use.
  Stored in `%LOCALAPPDATA%\eda-agent\digikey-tokens.json`, DPAPI-sealed,
  never returned or logged. A refused token is renewed once; a lapsed
  sign-in says to sign in again.

### What DigiKey's documentation says, and does not

Read from the MyLists v1 Swagger file and the OAuth pages on 2026-11-16.

- MyLists is three-legged OAuth only. The redirect address must be https and
  match the registered one exactly; `https://localhost` is DigiKey's
  suggestion.
- The part to add has no DigiKey-number field as such (it goes in
  `RequestedPartNumber`), no description field, and **no documented length
  limit** on `CustomerReference`, `ReferenceDesignator` or `Notes`.
- **Bag labels:** nothing official. DigiKey forum posts say the list's
  customer reference carries into the cart and prints on the label, and
  that about 48 characters fit. Nothing says the reference designator
  prints.
- A list holds up to 1,000 parts (DigiKey blog). No per-request limit is
  published; parts are sent 50 at a time.
- What an unknown part number does is undocumented; the read-back has
  `Flags.IsMatched`.

### Files

- `src/eda_agent/libimport/providers/digikey_lists.py` (new): OAuth, token
  file, MyLists client, compare-after-add
- `src/eda_agent/tools/parts.py`: the four tools
- `src/eda_agent/tools/metadata.py`: three of them marked as writing
- `tests/test_digikey_lists.py` (new): 32 tests against an in-memory DigiKey
- `src/eda_agent/libimport/providers/_distributor.py`: fuller error
  detail
- `tests/test_altium_no_raise.py`: the four tools set aside, as
  `part_search` is
- `docs/PART_SOURCING.md`, `docs/TOOL_REFERENCE.md` (regenerated, 4 lines)

### Test results

- **New tests:** 32 passed.
- **Tool surface and distributor tests** (21 files): 1,600 passed.
- The full suite was not rerun.

### Live state

- **Sign-in works.** The owner signed in; a sign-in is on file.
- **List creation fails.** The owner's `part_list_create` call returned HTTP
  400 "Error(s) in Parameters." No list was created and nothing has been
  added or read back.
- The request matches the published spec: endpoint, headers, `Source`
  (`external`) and every `ListSettings` field and value. The cause is not
  known yet. In order of likelihood:
  1. No `X-DIGIKEY-Account-Id` was sent (`DIGIKEY_ACCOUNT_ID` is unset).
     The spec calls it optional; forum reports show CreateList answering
     400 without an account id.
  2. The server rejects a partial `ListSettings` (no `ColumnPreferences`).
  3. `Source: "external"` is not open to third-party apps.
- **Error reporting fixed:** the shared `_error_detail` kept only
  `ErrorMessage` and dropped `ErrorDetails` and `ValidationErrors`, which
  is where DigiKey names the field. Both are now reported (400-character
  cap, credentials still struck out).
- **Next:** with the owner's approval, retry the creation once unchanged to
  read the full error; then set `DIGIKEY_ACCOUNT_ID`, or drop the field the
  error names. The MCP server must be restarted to load the new reporting.

Still unverified against the real service: the creation settings, whether
creation returns a bare id, the real length limit of the two references,
and whether DigiKey changes a quantity to a pack multiple.

### Known limitations

- No tool deletes a list, renames it, or edits or removes a part. A test
  list has to be removed on DigiKey's site.
- Adding does not merge: the same `sku` added twice is two entries.
- Designators are not abbreviated into ranges (`R1-R8`), so a line with
  many of them exceeds what a label fits.
- One quantity per part. DigiKey's assembly count (`assemblies`) and
  attrition are not used.
- DigiKey only; `provider` exists so Mouser can follow.
- The sandbox host is not supported.

## Session 3: description in the purchasing BOM

### Completed

Each purchasing BOM line, and each unresolved entry, now carries
`description`, read from the component's `Description` parameter.

- **Source:** the `Description` parameter only, name matched ignoring case.
  The comment never stands in for it. The library convention is
  `Manufacturer`, `Part Number`, `Description`; `Comment` holds design notes.
- **Grouping is unchanged:** still `Manufacturer` + `Part Number`. A part
  with no description is still ordered, with `description` empty.
- **Disagreement:** when the components of one line carry different
  descriptions (one with none counts as different), the line's `description`
  is empty and `inconsistencies` gains "one part with several descriptions,
  none chosen", listing each description with its designators.
- **Variant:** a variant's own `Description` value replaces the base one. An
  alternate part has a description only when its variant entry states one.
- **Compatibility:** `description` is an added key; nothing was renamed or
  removed, and `mode="full"` is untouched.

The bridge already returns `Description` among the parameters of
`project.get_component_info_batch`, so this is again Python-only. **No bridge
change, no redeployment.**

### Files

- `src/eda_agent/export/purchasing_bom.py`, `src/eda_agent/tools/project.py`
  (docstring only), `docs/PART_SOURCING.md`, `tests/test_purchasing_bom.py`
  (5 new tests, 3 updated; 32 in all)

### Test results

Only the relevant tests were run: `test_purchasing_bom.py`,
`test_tool_description_budget.py` and `test_docstring_references.py`, 40
passed. The full suite was not rerun.

### Live validation (a real project, read-only)

Same method as session 2: dev code against the live bridge, four read
commands, nothing written. The project is not named here and its figures are
not recorded.

- Placed, fitted and not fitted components matched the full BOM, nothing was
  unresolved, and the line quantities summed to the fitted total.
- The line count differed by one from session 2 because the design was edited
  in between.
- Nearly every line carried a description. One had none in the design. One
  had two different descriptions across its components and was reported, with
  none chosen.
- The reply was several times smaller than the full variant BOM.

### Known limitations

- A variant that changes a component's `Part Number` without also stating a
  `Description` keeps the base part's description on the new line.
- Descriptions are compared as written apart from spacing, so a difference in
  case alone is reported as a disagreement.
- Still not called through the MCP server itself, which needs a restart to
  load the new code.

## Session 2: compact purchasing BOM

### Completed

`proj_get_bom` takes `mode="purchasing"` and returns the BOM to order from.
With `mode` omitted the tool behaves exactly as before.

- **Identification:** only the schematic parameters `Manufacturer` and
  `Part Number`, matched ignoring case. Comment, description, library
  reference and supplier numbers never stand in for them.
- **Lines:** fitted components grouped by manufacturer and part number, each
  with `quantity`, `designators` and `footprints`.
- **Unresolved:** a fitted component with either parameter missing, empty or a
  placeholder (`N.A`, `TBD`, `-`) is listed with the reason. Nothing is
  guessed and nothing is dropped.
- **Variant:** builds the project's active variant unless `variant` names
  one; `"[No Variations]"` is the base design. Not-fitted parts are excluded
  and listed. A variant's own values for the two parameters replace the base
  ones. If the active variant cannot be read, the call is refused.
- **Component Kind:** fitted components marked `Standard (No BOM)`, `Net Tie`
  or `Graphical` go to `not_in_bom`.
- **Inconsistencies** are reported, not merged away: one part number under
  several manufacturers (kept as separate lines), one part on several
  footprints, a manufacturer spelled several ways (counted as one line), a
  repeated designator, a variant entry for a designator that is not placed.
- **Completeness:** `summary.complete` is false, with `incomplete_reason`,
  when the read stopped at `limit` or Altium returned part of the project
  empty.
- **Accounting:** every placed component is in exactly one of `lines`,
  `unresolved`, `not_fitted` and `not_in_bom`.

It is a Python-only change. It combines four existing bridge commands
(`project.get_bom`, `project.get_active_variant`, `project.get_variants`,
`project.get_component_info_batch` without pins or nets, 200 designators per
call). **The DelphiScript bridge was not changed and needs no redeployment.**

### Files

- `src/eda_agent/export/purchasing_bom.py` (new): grouping, unresolved and
  inconsistency logic
- `src/eda_agent/tools/project.py`: `mode` argument, the purchasing path,
  docstring
- `docs/PART_SOURCING.md`: "The purchasing BOM of an Altium project" section
- `tests/test_purchasing_bom.py` (new): 27 tests

### Test results

- **New tests:** 27 passed.
- **Related existing tests** (BOM, variant, tool surface, metadata): 962
  passed.
- **Full offline suite:** 10,259 passed, 331 skipped, 10 failed, 7 errors.
  The 17 failures are the same ones, in the same four files, as in session 1
  (see "Known failures" below).

### Live validation (a real project, read-only)

Run against the project open in Altium, with the active variant. The project
is not named here and its figures are not recorded.

| Check | Result |
|---|---|
| Placed components | matched the full BOM |
| Fitted | matched the full variant BOM |
| Not fitted | the same designators as full mode |
| Unresolved | none |
| Sum of line quantities | equal to the fitted total |
| Each designator appears once | yes |
| Reply size | several times smaller than the full variant BOM |

It also reported a few part numbers used on more than one footprint.

The MCP server running at the time still held the old code, so the new tool
function was run from the dev environment against the live bridge. That was a
second client on the same workspace. It sent only the four read commands and
wrote nothing to Altium. **The new mode has not yet been called through the
MCP server itself.**

### Known limitations

- Variant parameter overrides and alternate parts are covered by offline
  tests only. The validated project's variant has neither.
- An alternate part is ordered only when its variant entry states both
  parameters; otherwise it is unresolved. It is not known whether Altium
  always stores an alternate's parameters in the variant entry.
- It is not confirmed whether Altium's compiled parameters already reflect
  the active variant. If they do, a BOM requested for a different variant
  could start from the active variant's values.
- Manufacturer names are compared as written, apart from case and spacing.
  Different spellings of one company are different manufacturers here.
- `limit` still defaults to 1,000. A larger board returns `complete: false`
  and says to raise it.
- Two parameters of identical name on one component cannot be told apart,
  because the bridge returns parameters as a name-to-value object.
- Shopping-cart creation is not implemented, by design.

### Environment notes from this session

- The altium MCP server reported version 0.8.2, where it had reported 0.8.1
  earlier in the day.
- Altium was running bridge script 2026.11.10.02 while the script on disk is
  2026.11.16.1 ("stale script cache"). This predates the purchasing BOM work
  and was left alone. Reloading means closing and reopening
  `Altium_API.PrjScr` in Altium and running `StartMCPServer`; do it only with
  the owner's approval.

## Session 1: DigiKey and Mouser sourcing data

Committed as `f7b5769`.

### Completed

`part_search` and `part_fetch` return a `sourcing` object on DigiKey and
Mouser hits. Existing reply keys are unchanged; hits from other providers
serialize exactly as before.

- **Fields:** distributor, product URL, stock, manufacturer stock, on-order
  quantities (Mouser), lifecycle (normalized status plus raw text), lead time,
  suggested replacement, locale, `retrieved_at`, `freshness`, and `offers`.
- **Offers:** one per orderable number, each with `distributor_pn`,
  `packaging`, `stock`, `moq`, `multiple`, `standard_package`, `currency` and
  `price_breaks`. DigiKey reel, cut tape and Digi-Reel stay three offers.
- **Exact lookup:** `part_fetch` uses DigiKey ProductDetails and Mouser's
  part-number search (`Exact`), with keyword search as fallback. It accepts an
  MPN or a distributor part number.
- **DigiKey auth and locale:** token cached until near expiry and replaced once
  if refused; optional `DIGIKEY_LOCALE_SITE`, `DIGIKEY_LOCALE_LANGUAGE`,
  `DIGIKEY_LOCALE_CURRENCY`.
- **Errors:** rate limits are no longer reported as a bad key (Mouser signals
  them with HTTP 403); credentials echoed in an error body are struck out.
- **Missing data:** null means the distributor did not say. A null lifecycle is
  never "active"; an ambiguous price string stays as `price_text`.
- **Ambiguous MPNs:** an MPN shared by several products (LM358DR is made by
  Texas Instruments and Rohm) is never resolved to one. `part_fetch` returns
  `{"ok": false, "ambiguous": true, "candidates": [...]}`. Search hits that
  share an MPN get distinct refs by distributor number, for example
  `digikey:296-LM358DRTR-ND` and `mouser:595-LM358DR`.
- **`verified_live`** is true for DigiKey and Mouser.

### Files

Modified:

- `src/eda_agent/libimport/providers/base.py`: `sourcing` field on `PartHit`,
  `AmbiguousPart` error
- `src/eda_agent/libimport/providers/__init__.py`: exports `AmbiguousPart`
- `src/eda_agent/libimport/providers/_distributor.py`: error classification,
  ambiguity refusal, distinct ids
- `src/eda_agent/libimport/providers/distributors.py`: DigiKey and Mouser
  classes only
- `src/eda_agent/tools/parts.py`: ambiguity reply and docstrings
- `docs/PART_SOURCING.md`: provider rows and a "Distributor sourcing data"
  section
- `tests/test_distributor_providers.py`: the `verified_live` assertion

New:

- `src/eda_agent/libimport/providers/_sourcing.py`: normalizers
- `tests/test_distributor_sourcing.py`: offline tests
- `tests/test_distributor_live.py`: opt-in live tests
- `tests/fixtures/distributors/`: eight sanitized JSON replies
- `DEVELOPMENT_STATUS.md`: this file

### Test results

All run with `C:\Projects\eda-agent\env\Scripts\python.exe`.

- **Distributor, parts and tool-surface tests:** 861 passed.
- **Live tests** (`EDA_AGENT_LIVE_DISTRIBUTORS=1`, LM358DR): 2 passed.
- **End-to-end MCP** against the dev server over stdio: `part_search` returned
  10 hits with distinct refs, every ref fetched back its own product, and both
  `digikey:LM358DR` and `mouser:LM358DR` returned the ambiguity reply.
- **Full offline suite:** 10,232 passed, 331 skipped, 10 failed, 7 errors.

### Known failures (not from either patch)

The 17 failures were compared against the unmodified tree once, in session 1,
and were identical there. The same 17, in the same files, failed after every
later change. They are in:

- `tests/test_kicad_corpus.py` and `tests/test_kicad_accepts_our_output.py`:
  KiCad-library tests on this machine
- `tests/test_updates.py`: three errors with "Access is denied" on a launcher
- `tests/test_bridge_handlers_reachable.py`: one failure

Related: `env\Scripts\eda-agent.exe` cannot be executed ("Access is denied").
The cause was not investigated. Start the dev server through the venv Python
instead (see Security precautions).

### Remaining stable-reference issue

A hit's ref depends on the search result it came from. A hit gets
`provider:MPN` unless another hit from the same distributor in that same
result shares the MPN; only then does it get `provider:<distributor number>`.

Consequences:

- The same product can be `digikey:LM358DR` in one search and
  `digikey:296-LM358DRTR-ND` in another, for example when a low
  `limit_per_provider` shows only one of two makers.
- A stored `provider:MPN` ref can later return the ambiguity reply instead of
  a product. That is safe (nothing wrong is selected) but not stable.
- For DigiKey the distinct ref uses the first packaging's number, so the ref
  names a packaging although it stands for the whole product.

Not yet decided with the owner. The obvious option is to always use the
distributor number as `part_id` for DigiKey and Mouser hits, which makes refs
stable but changes `part_id` for every such hit (the `mpn` field would still
carry the MPN, and MPN fetches would still work). For BOM work today, store
the `distributor_pn`, not the ref.

### Other limitations

- DigiKey has no order-multiple field, so `multiple` is always null there.
- Lifecycle wording, lead-time units and Mouser packaging labels are matched
  in English only; other locales keep raw text with a null reading.
- Not exercised live: obsolete parts, a real rate-limit response, a non-USD
  account, the DigiKey stale-token retry.
- Element14, Nexar and TME still use the old first-match fetch.
- PricingOptionsByQuantity is not used.

## Security precautions

- Credentials come only from `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` and
  `MOUSER_API_KEY`. Never print, log or commit them. Scan staged changes for
  the actual values before every commit.
- Search and product-detail endpoints only. No ordering, cart or checkout.
- Keep proprietary design data out of the repository: no project, document or
  library names, no project paths, nets or designators of a real design, no
  real BOM contents, and no counts or sizes taken from a real design, in code,
  tests, fixtures, commit messages or this file. Describe a live validation
  generically. A public part number used as a test example is fine when
  nothing ties it to a project.
- Do not modify Altium documents, component libraries or the Claude MCP
  configuration.
- **Starting a dev server rewrites a pointer file.** By default that is
  `C:\ProgramData\eda-agent\workspace-path.txt`, which the live Altium bridge
  reads. To run a dev server that must not touch the live bridge, set
  `EDA_AGENT_WORKSPACE` and `EDA_AGENT_POINTER_FILE` to a scratch location,
  plus `EDA_AGENT_NO_DASHBOARD=1` and `EDA_AGENT_UPDATE_CHECK=0`.
- Launch command used: `env\Scripts\python.exe -c "import sys; from
  eda_agent.server import main; sys.argv = ['eda-agent', '--no-dashboard'];
  sys.exit(main())"`.
- Live distributor calls count against 1,000 a day at each distributor.
- Scratch scripts and captures from both sessions lived in session temp
  folders and may be gone; the fixtures in the repository are the durable
  copy.

## Next steps for the next session

1. Run `git status` and `git log --oneline -5`; confirm the branch and that
   `main` matches `origin/main` (`git fetch` first). Decide with the owner
   what happens to the remote `feature/distributor-pricing` branch. For DigiKey MyLists, start
   from "Remaining limitations" under session 5; for the Mouser cart and the
   cache, from "Open" under session 6.
2. Restart the altium MCP server so it loads the purchasing BOM code (reconnect
   it with `/mcp`, or restart Claude Code).
3. Call `proj_get_bom` with `mode="purchasing"` and the path of a real
   project (keep that path out of this file) through the MCP server; confirm
   the summary matches the live validation table. Call it without `mode` and confirm the full BOM is
   unchanged.
4. Exercise variant overrides on a project whose variant varies
   `Part Number` or uses an alternate part, and settle the two open variant
   questions under "Known limitations".
5. Ask the owner how to settle the stable-reference issue from session 1:
   keep the current behaviour, or always use the distributor number as
   `part_id` for DigiKey and Mouser.
6. The planned follow-on is feeding purchasing BOM lines into `part_search`
   and `part_fetch` for availability and pricing. DigiKey and Mouser cart
   APIs are out of scope until the owner asks for them.
7. Do not commit, push or open a pull request without the owner's approval.
