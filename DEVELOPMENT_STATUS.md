# Development status: distributor sourcing and the purchasing BOM

Last updated 2026-11-16, as a handover for the next working session. This is
a working note for the fork's `feature/distributor-pricing` branch. Leave it
out of any pull request meant for upstream.

## Where things stand

- Repository: `C:\Projects\eda-agent`, branch `feature/distributor-pricing`,
  tracking `origin` (the fork). `upstream` is the original project; nothing
  is pushed there.
- Three commits sit on top of `e5f3469` (`upstream/main`, version 0.8.2):
  1. `f7b5769` "Add DigiKey and Mouser sourcing improvements" (session 1).
  2. `120e1f6` "Add compact purchasing BOM support" (session 2), pushed to
     `origin`.
  3. "Add Description to the purchasing BOM" (session 3), pushed to
     `origin`.
- No pull request has been opened.

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

### Live validation (SensorBoard project, read-only)

Same method as session 2: dev code against the live bridge, four read
commands, nothing written.

- 393 placed, 374 fitted, 19 not fitted, 0 unresolved, quantities sum to 374.
- 103 line items (104 in session 2; the design was edited in between).
- 101 lines carry a description. One has none in the design. One has two
  descriptions across its four components and was reported, with none chosen.
- Reply size 23.2 KB (18.4 KB without descriptions), against 113.6 KB.

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

### Live validation (SensorBoard project, read-only)

Run against the project open in Altium, with the active variant.

| Check | Result |
|---|---|
| Placed components | 393, matching the full BOM |
| Fitted | 374, matching the full variant BOM |
| Not fitted | 19, the same designators as full mode |
| Line items | 104 |
| Unresolved | 0 |
| Sum of line quantities | 374 |
| Each designator appears once | yes |
| Reply size | 18.4 KB, against 113.6 KB for the full variant BOM |

It also reported seven part numbers used on two footprints each.

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
- Keep proprietary design data out of the repository: no project paths, nets,
  or real BOM contents in code, tests, fixtures or this file. Counts are fine.
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

1. Run `git status` and `git log --oneline -3`; confirm the branch is
   `feature/distributor-pricing` with the two commits above and a clean tree.
2. Restart the altium MCP server so it loads the purchasing BOM code (reconnect
   it with `/mcp`, or restart Claude Code).
3. Call `proj_get_bom` with `mode="purchasing"` and the SensorBoard project
   path through the MCP server; confirm the summary matches the live
   validation table. Call it without `mode` and confirm the full BOM is
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
