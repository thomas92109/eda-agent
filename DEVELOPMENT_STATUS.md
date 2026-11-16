# Development status: DigiKey and Mouser sourcing data

Written 2026-11-16 as a handover for the next working session. This is a
working note, not part of the patch: leave it out of any commit meant for
upstream.

## Where things stand

- Repository: `C:\Projects\eda-agent`, branch `feature/distributor-pricing`,
  HEAD `e5f3469` (identical to `upstream/main`, version 0.8.2).
- **Nothing is committed, pushed or stashed.** All work is in the working tree.
- The owner has approved the approach and the implementation. A commit needs
  their explicit go-ahead.

## Completed

`part_search` and `part_fetch` now return a `sourcing` object on DigiKey and
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
- **`verified_live`** is now true for DigiKey and Mouser.

## Files

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

Diff size: 7 tracked files, 658 insertions and 99 deletions, plus the new
files.

## Test results

All run with `C:\Projects\eda-agent\env\Scripts\python.exe`.

- **Distributor, parts and tool-surface tests:** 861 passed.
- **Live tests** (`EDA_AGENT_LIVE_DISTRIBUTORS=1`, LM358DR): 2 passed.
- **End-to-end MCP** against the dev server over stdio: `part_search` returned
  10 hits with distinct refs, every ref fetched back its own product, and both
  `digikey:LM358DR` and `mouser:LM358DR` returned the ambiguity reply.
- **Full offline suite:** 10,232 passed, 331 skipped, 10 failed, 7 errors.

### Known failures (not from this patch)

The 17 failures were compared against the unmodified tree once, after the
first implementation round, and were identical there. The same 17 failed
after the ambiguity correction. They are in:

- `tests/test_kicad_corpus.py` and `tests/test_kicad_accepts_our_output.py`:
  KiCad-library tests on this machine
- `tests/test_updates.py`: three errors with "Access is denied" on a launcher
- `tests/test_bridge_handlers_reachable.py`: one failure

Related: `env\Scripts\eda-agent.exe` cannot be executed ("Access is denied").
The cause was not investigated. Start the dev server through the venv Python
instead (see Security precautions).

## Remaining stable-reference issue

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

Other limitations:

- DigiKey has no order-multiple field, so `multiple` is always null there.
- Lifecycle wording, lead-time units and Mouser packaging labels are matched
  in English only; other locales keep raw text with a null reading.
- Not exercised live: obsolete parts, a real rate-limit response, a non-USD
  account, the DigiKey stale-token retry.
- Element14, Nexar and TME still use the old first-match fetch.
- PricingOptionsByQuantity is not used.

## Security precautions

- Credentials come only from `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` and
  `MOUSER_API_KEY`. Never print, log or commit them. The diff, new files and
  fixtures were scanned for the actual values after the first round and were
  clean; the fixtures have not changed since. Re-scan before committing.
- Search and product-detail endpoints only. No ordering, cart or checkout.
- Do not modify the global EDA Agent 0.8.1 install, the Claude MCP
  configuration, or Altium documents.
- **Starting a dev server rewrites a pointer file.** By default that is
  `C:\ProgramData\eda-agent\workspace-path.txt`, which the live Altium bridge
  reads. Always start a dev server with `EDA_AGENT_WORKSPACE` and
  `EDA_AGENT_POINTER_FILE` set to a scratch location, plus
  `EDA_AGENT_NO_DASHBOARD=1` and `EDA_AGENT_UPDATE_CHECK=0`.
- Launch command used: `env\Scripts\python.exe -c "import sys; from
  eda_agent.server import main; sys.argv = ['eda-agent', '--no-dashboard'];
  sys.exit(main())"`.
- Live calls count against 1,000 a day at each distributor.
- The scratch capture and end-to-end scripts lived in a session temp folder
  and may be gone; the fixtures in the repository are the durable copy.

## Next steps for the next session

1. Run `git status` and confirm the branch is `feature/distributor-pricing`
   with the files listed above and no commits beyond `e5f3469`.
2. Ask the owner how to settle the stable-reference issue: keep the current
   behaviour, or always use the distributor number as `part_id` for DigiKey
   and Mouser.
3. If a change is chosen, make it in `_distinct_ids` in `_distributor.py` and
   update the ref assertions in `tests/test_distributor_sourcing.py` and the
   "One number, several parts" paragraph in `docs/PART_SOURCING.md`.
4. Re-run the relevant tests:
   `env\Scripts\python.exe -m pytest tests/test_distributor_sourcing.py
   tests/test_distributor_providers.py tests/test_part_providers.py
   tests/test_tool_description_budget.py -q`
5. Re-scan the diff for credential values, then show the owner the diff
   summary.
6. Commit only on the owner's explicit approval, excluding this file. Ask
   whether they want one commit or a split (code, tests, docs). Do not push.
