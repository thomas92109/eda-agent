# Part sourcing

Every part provider the server can query, what each one returns, and
which credentials it needs. No provider is enabled by default and there
is no fallback order.

Back to the [README](../README.md).

`part_search` queries **every** enabled part provider and merges the results,
each hit attributed to the source that found it. `part_fetch` then pulls one
part's detail from a provider you name.

Sources come in **two kinds**, merged into one result but never conflated. A
**library** provider yields geometry you can place. A **catalogue** provider
yields part identity, a datasheet and stock, and no geometry at all. Every hit
carries its `kind`, because discovering that a distributor hit has no symbol
after picking the part is the expensive way to learn it. They are not tiers and
neither is a fallback for the other: a catalogue tells you *which* part to use
and hands you the datasheet every check here measures against, and a library
tells you whether you can draw it.

| Provider | Kind | What it is | Network | Credential |
|---|---|---|---|---|
| `altium_local` | library | The `.SchLib` libraries already on this machine. The only source that answers whether you **already own** a part, which is what stops an import creating a second symbol with a slightly different name. Reads the OLE files directly, so it works with Altium closed. Nothing to download: a hit is already an Altium symbol. Point it with `EDA_AGENT_ALTIUM_LIBRARIES` | no | no |
| `digikey` | catalogue | Digi-Key's catalogue: MPN, datasheet, lifecycle, stock, lead time and price breaks per packaging (see [Distributor sourcing data](#distributor-sourcing-data)). Needs an OAuth client from their developer portal via `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`; `DIGIKEY_LOCALE_SITE`, `DIGIKEY_LOCALE_LANGUAGE` and `DIGIKEY_LOCALE_CURRENCY` pick the market | yes | yes |
| `easyeda` | library | EasyEDA / LCSC component data. Fetch by LCSC part number works; **search is unavailable** because the upstream endpoint was withdrawn | yes | no |
| `element14` | catalogue | element14 (Farnell, Newark): MPN, datasheet and stock. Needs `ELEMENT14_API_KEY`; `ELEMENT14_STORE` picks the regional storefront, which changes the catalogue you see | yes | yes |
| `kicad_local` | library | The libraries KiCad installed on this machine. A fetch resolves the symbol's footprint reference against the installed `.pretty` libraries and the footprint's model reference against the `3dmodels` tree, so a hit can be a whole part with a 3D body (18003 of the 22728 symbols shipped with KiCad 10.0.1 name a footprint). No MPN or datasheet for most entries | no | no |
| `mouser` | catalogue | Mouser's catalogue: MPN, datasheet, lifecycle, stock, lead time and price breaks (see [Distributor sourcing data](#distributor-sourcing-data)). Needs `MOUSER_API_KEY`; prices and currency follow that key's account | yes | yes |
| `nexar` | catalogue | Nexar, the API behind Octopart: part number, manufacturer, description and datasheet link, no prices or stock. Needs `NEXAR_CLIENT_ID` and `NEXAR_CLIENT_SECRET` | yes | yes |
| `partreel` | library | An open registry of verified KiCad parts, no login and no key: `/api/v1/parts.json` serves 21,657 parts. Yields KiCad files, usable in Altium through `lib_kicad_import`. Points at PartReel the way the Digi-Key client points at Digi-Key; `PARTS_REGISTRY_URL` redirects it at any API-compatible registry, since the API shape is the contract rather than the host. Run by a third party, proposed in issue #12 by its operator | yes | no |
| `public_libraries` | library | Openly published KiCad libraries, no login and no key: KiCad's own 12,011 footprints, Digi-Key's 936, and JLCPCB's 20 symbol libraries plus footprints. Mostly **land patterns rather than symbols**, which is what you want once the part is chosen but its geometry is not. Indexed through GitHub's documented API, one request per repository, cached on disk for a week. `EDA_AGENT_CACHE_DIR` relocates the cache | yes | no |
| `tme` | catalogue | TME: MPN, datasheet and stock, strongest on European availability. Requests are HMAC signed; needs `TME_TOKEN` and `TME_SECRET`, and `TME_COUNTRY` selects the market | yes | yes |

**No provider is a default and none is preferred.** All are searched equally,
and the merged order is alphabetical by provider then part, which is not a
relevance ranking: do not read the first hit as the best one. `part_fetch`
requires the provider name rather than supplying one, so a fetch always states
which source it trusts. Tests enforce this rather than leaving it to
convention, because a default parameter or a preference sort would quietly make
one source the answer to every query.

A provider that cannot answer reports **why** instead of returning nothing.
"The endpoint is gone" and "no such part exists" are different answers, and
only one of them is a reason to stop looking.

Select a subset with `EDA_AGENT_PART_PROVIDERS=kicad_local,altium_local` (the
variable selects, it never ranks). `PARTS_REGISTRY_URL` names the registry the
`partreel` client queries; there is no default, so that source stays off until
you choose one. Call `part_search` with an empty query to list the providers
and who operates them.

**Four of the ten answer on a fresh install**, none of them needing a
credential: `altium_local` and `kicad_local` read libraries already on disk,
`partreel` queries an open registry, and `public_libraries` indexes openly
published KiCad libraries from GitHub. That is deliberately more than one, so
no single free source is load-bearing. The five catalogues each need their own
credential and none ships with one, and EasyEDA's search endpoint was withdrawn
upstream (fetch by LCSC part number still works). Every source that cannot answer reports itself unavailable, names the
environment variable it wants, and says so per provider in the search result
rather than folding into an empty list.

A client pointing at its own service is not a preference: the Digi-Key client
points at Digi-Key, and `partreel` points at PartReel. What neutrality means
here is the absence of RANKING, and that is enforced by tests rather than left
to intent. No source is consulted as a fallback when another returns thin
results, none can reach the front of a merged list, and there is deliberately
no "default provider" setting to point anywhere.

**What was verified, and what was not.** Every catalogue endpoint above was
probed live before it was written into the code: a 401 or 403 proves the host
and path exist and refused only for lack of a credential. Three further
candidates were probed and **dropped** for answering 404 on the recalled URL
rather than being shipped as plausible guesses. What that probing could *not*
establish, without a paid credential, is the request and response shapes. So
a catalogue publishes `verified_live: false` until its client has actually run
against the live API, and the parsers are written to degrade a single hit on a
renamed field rather than assume a shape and lose the whole search. `digikey`
and `mouser` have run with a credential and publish `true`; `element14`,
`nexar` and `tme` have not.

## Distributor sourcing data

A `digikey` or `mouser` hit carries a `sourcing` object beside its identity, in
`part_search` hits and in `part_fetch`'s `detail`. Both clients only read the
distributors' search APIs: nothing here orders, quotes or fills a cart.

| Field | What it is |
|---|---|
| `distributor` | `digikey` or `mouser` |
| `product_url` | The distributor's product page |
| `stock` | Units the distributor has now |
| `manufacturer_stock` | Units at the manufacturer, where the distributor reports it (Mouser's factory stock, Digi-Key's manufacturer public quantity) |
| `on_order` | Mouser's incoming stock, `[{quantity, date}]`; `null` from Digi-Key, which does not report it |
| `lifecycle` | `status` is one of `active`, `new`, `preliminary`, `nrnd`, `last_time_buy`, `end_of_life`, `obsolete`, `discontinued`, or `null`; `raw` is the distributor's own wording; `discontinued` and `end_of_life` are its flags |
| `lead_time` | `raw` as given and `weeks` when the unit is clear |
| `suggested_replacement` | Mouser's suggestion, when it makes one |
| `offers` | One per orderable number: `distributor_pn`, `packaging`, `stock`, `moq`, `multiple`, `standard_package`, `currency`, `price_breaks` (`[{quantity, unit_price}]`) and `provider_specific` |
| `locale` | The site, language and currency the figures are for |
| `retrieved_at`, `freshness` | When the reply was read (UTC) and how current the distributor says it is |

**`null` means the distributor did not say.** Mouser sends no lifecycle status
for most parts in production, so a `null` status is "not stated", never
"active". A part with no price breaks has an empty list, not a price of zero.
A lifecycle wording or lead-time unit this code does not know (a key registered
in another language gets translated ones) keeps its `raw` text and a `null`
reading.

**Packagings stay separate where the distributor separates them.** Digi-Key
sells tape and reel, cut tape and Digi-Reel under three numbers with three
price ladders, so they are three offers. Mouser sells every packaging of a part
under one number, so it is one offer whose `packaging` lists them. Digi-Key
states no order multiple, so `multiple` is `null` there and `standard_package`
is reported on its own.

**How current the figures are.** Digi-Key says its keyword search may be up to
24 hours old, and `freshness` says so on a search hit; `part_fetch` reads its
ProductDetails, which is live. Mouser makes no statement, so its `freshness`
names only the time of reading. Check `retrieved_at` before relying on a stock
figure.

**One number, several parts.** A manufacturer part number is not always one
part: `LM358DR` is made by Texas Instruments and by Rohm, and both distributors
list both. A hit's `ref` is `provider:MPN` as long as that names one product in
the result; where two hits share the MPN, each takes its distributor number
instead (`digikey:296-LM358DRTR-ND`, `mouser:595-LM358DR`), so no two products
carry the same `ref`. `part_fetch` takes either form, and any `distributor_pn`
from `sourcing.offers`. Given an MPN that names several products it picks none:
the reply is `{"ok": false, "ambiguous": true, "candidates": [...]}`, each
candidate with its `ref`, manufacturer, MPN and distributor numbers per
packaging.

Mouser prices come from its numeric `RawPrice`. Where a reply has only the
formatted text, it is read when it can mean one thing (`0,285 €`, `$9,999.99`)
and otherwise kept as `price_text` with a `null` price: `1.234` is one and a
bit in one locale and over a thousand in another.

### The cache of distributor replies

DigiKey and Mouser each allow about 1,000 calls a day, and a BOM asks about
the same parts repeatedly. Replies to their read-only catalogue calls are
kept in one SQLite file, `%LOCALAPPDATA%\eda-agent\distributor-cache.sqlite3`
(`EDA_AGENT_DISTRIBUTOR_CACHE_FILE` moves it), and reused:

| What a reply contains | Kept for |
|---|---|
| Product information only | 30 days |
| Search results | 24 hours |
| Pricing or availability | 4 hours |

A reply holding several of these is kept for the shortest. Both distributors
send prices and stock with every product, so in practice an entry lasts 4
hours. A hit built from a kept reply says so: `sourcing.cache` is
`{hit, fetched_at, expires_at}`, `retrieved_at` is when it was really read,
and `freshness` starts with `cached:`. A reply read from the distributor is
exactly as it was before the cache existed.

- `part_search` and `part_fetch` take `refresh=true` to ask the distributor
  again; the new reply replaces the kept one.
- `EDA_AGENT_DISTRIBUTOR_CACHE=0` turns the cache off.
- Entries are kept apart by distributor, endpoint, every request parameter,
  DigiKey's locale settings and the account, which is told apart by a short
  one-way fingerprint of the key. No key, secret or token is written.
- Never kept: a failed call, an error under HTTP 200, a reply of the wrong
  shape, an exact Mouser lookup that found nothing, a token exchange, and
  anything to do with a cart or a saved list.
- The check made before a part goes into a Mouser cart or a DigiKey list
  always asks the distributor, whatever is kept.
- If the file cannot be opened, read or written, the call goes to the
  distributor as if there were no cache.

`python -m eda_agent.libimport.providers._cache stats` shows what is kept;
`... clear` (optionally `digikey` or `mouser`) empties it.

The database itself (its one table, how a cache key is made, what happens on
a database error, and queries for looking inside it) is described in
[DISTRIBUTOR_CACHE_SCHEMA.md](DISTRIBUTOR_CACHE_SCHEMA.md).

## The purchasing BOM of an Altium project

`proj_get_bom` with `mode="purchasing"` returns the BOM to order from: what to
search the distributors for, and how many. It reads two schematic parameters
from every placed component, `Manufacturer` and `Part Number` (names matched
ignoring case), and nothing else stands in for them: not the comment, the
description, the library reference or a supplier number. The reply carries no
pins or nets; on a 393-part board it came to 23 KB where the full BOM is
over 100 KB.

| Field | What it is |
|---|---|
| `summary` | `variant` and `variant_source`, the counts, `complete` and `truncated` |
| `lines` | One per manufacturer and part number: `manufacturer`, `part_number`, `description`, `quantity`, `designators`, `footprints` |
| `unresolved` | Fitted components that cannot be ordered as they stand: `designator`, `manufacturer`, `part_number`, `description`, `footprint`, `reason` |
| `not_fitted` | Designators the variant leaves off the board |
| `not_in_bom` | Fitted designators whose Component Kind keeps them out of the BOM (`Standard (No BOM)`, `Net Tie`, `Graphical`) |
| `inconsistencies` | Things to look at before ordering, none of them merged away silently |

Every placed component is in exactly one of `lines`, `unresolved`, `not_fitted`
and `not_in_bom`, so the counts in `summary` add up to `components`.

**The variant.** It builds the project's active variant unless `variant` names
one; `variant="[No Variations]"` is the base design. A not-fitted part is left
out and listed. A variant's own values for the two parameters replace the base
ones. An alternate part is ordered only when its variant entry states both
parameters, because the base part's manufacturer and number are not the
alternate's; otherwise it is `unresolved`. If the active variant cannot be
read the call is refused: a BOM for the wrong variant has wrong quantities and
looks right.

**Nothing is guessed.** A component with a parameter missing, empty, or holding
a placeholder (`N.A`, `TBD`, `-`) is `unresolved` with the reason. One part
number under two manufacturers stays two lines. A manufacturer spelled two ways
(`YAGEO`, `Yageo`) is one line, and the spellings are reported.

**The description.** A line's `description` is the components' `Description`
parameter: what the part is, for the person reading the order. It identifies
nothing, so it never changes the grouping, and the comment never stands in for
it, because a comment holds notes about the design. A part with no description
is still ordered, with `description` empty. When the components of one line
disagree, counting one with none against one with some, the line's
`description` is empty and `inconsistencies` lists each description with the
designators that carry it; none is picked. A variant's own value for
`Description` replaces the base one, and an alternate part has a description
only when its variant entry states one.

**`complete` false means do not order from it.** It is false when the read
stopped at `limit` with components left over (`truncated`), or when Altium
returned part of the project empty; `incomplete_reason` says which.

## The purchasing BOM as a CSV file

This is the Standard BOM: one assembly, no distributor, five default columns.
For assemblies, quantities, prices and review reasons see
[The Purchasing BOM](#the-purchasing-bom-assemblies-quantities-and-review).

`proj_export_purchasing_bom_csv` writes the same BOM to a CSV file. It reads
the project the way `proj_get_bom(mode="purchasing")` does and writes Altium's
own values with Python's `csv` module: nothing is retyped, guessed, merged or
shortened, and every reference designator is kept. It contacts no distributor
and changes no Altium document. The reply is counts and warnings, never the BOM.

**Columns.** Left out, `columns` gives exactly these five, in this order:

`Quantity`, `Part Number`, `Description`, `Manufacturer`, `Reference Designator`

Pass `columns` for another selection. It is an ordered list of identifiers, and
the file has exactly those columns in that order:

| Identifier | Header |
|---|---|
| `quantity` | Quantity |
| `part_number` | Part Number |
| `description` | Description |
| `manufacturer` | Manufacturer |
| `reference_designators` | Reference Designator |
| `footprint` | Footprint (a line on several footprints lists them all) |
| `fitted_status` | Fitted / DNP Status |

An unknown, repeated or empty selection is refused and nothing is written.
Identifiers are matched exactly.

**DNP parts.** Left out by default, and the default five columns are exactly
as above. With `include_dnp=True` each not-fitted part is on a row of its own
with status `DNP` and `Quantity` 0: it is listed, not bought. Only the
not-fitted designators of that part are on the row. Because a DNP row must
never pass for a part to buy, `include_dnp=True` requires `fitted_status` in
`columns`. Without it, the default five included, the call is refused before
Altium is read, and no column is ever added for you. This reads the project a
second time, with the same variant, so the fitted rows are the ones the
default export writes.

**What Altium says each excluded part is.** Three different things keep a
part out of the file, and they are counted apart in `totals`, from Altium's
own lists and never inferred:

| `totals` field | Meaning |
|---|---|
| `not_fitted_components` | DNP: the variant does not fit them |
| `not_in_bom_components` | Fitted, but their Component Kind (Altium's Not in BOM setting) keeps them out |
| `unresolved_components` | Fitted, but without a usable `Manufacturer` or `Part Number` |

With `include_dnp` the not-fitted parts are divided further: `dnp_components`
(on a DNP row), `dnp_not_in_bom_components` (not fitted and also kept out by
Component Kind, so with no row), `dnp_unresolved_components` and
`dnp_unclassified_components` (in none of Altium's lists). Those last three are
`null` when the not-fitted parts were not read. Each group without a row is
named in `warnings`.

**Where it goes, and never over an old file.** The file is written to the
`bom` folder of the export folder (`EDA_AGENT_EXPORT_DIR`, else
`EDA Agent/exports` under the user's home) as `<project>_purchasing_BOM.csv`.
A path given in `output_path` is used as given, and refused when it is inside
the public repository. The naming rule is the same for both, and is shared by
the netlist, variant matrix, stackup and JLC assembly exports:

| The name | What is written |
|---|---|
| is free | `DemoBoard_purchasing_BOM.csv` |
| exists | `DemoBoard_purchasing_BOM_20261117_153000.csv` (local time, before the extension) |
| and that exists too | `DemoBoard_purchasing_BOM_20261117_153000_2.csv`, then `_3`, and so on |

A JLC assembly export writes two files, and they always share one suffix: if
either `<board>_BOM_JLCPCB.csv` or `<board>_CPL_JLCPCB.csv` exists, both are
written with the same stamp (and the same `_2`, `_3`). The file is created
exclusively, so even one that appears between choosing the name and writing is
not replaced; a write that fails removes the file it had created. When the
name differs from the one asked for, `warnings` says so.

**What it will not write quietly.** A read cut short (`complete` false) is
refused unless `allow_incomplete=True`, and then `warnings` says the quantities
may be short. Fitted components with no usable `Manufacturer` or `Part Number`,
components kept out of the BOM by their Component Kind, and every
inconsistency Altium reports are named in `warnings`: they are not in the file.
After writing, the rows' quantities are checked against Altium's counts.

**Spreadsheets.** The file is UTF-8 with a byte order mark and CRLF line ends,
so Excel opens it directly. A cell that starts with `=`, `+`, `-` or `@` (or a
tab or line break) would be run as a formula, so it gets a leading apostrophe;
that is the only change made to any value, and `warnings` counts it. Excel
also reinterprets some values when it opens a CSV directly: a part number
`0012345678` loses its zeros, `1E5` becomes a number, `5-1` a date. The file holds
the exact text; `warnings` counts such cells, and importing the column as Text
(Data > From Text/CSV) keeps them.

## Saved lists in a DigiKey account (MyLists)

Four tools put parts into a DigiKey saved list and read it back. A list is
not an order: nothing here touches a cart, a quote or DigiKey's Ordering API.

| Tool | What it does |
|---|---|
| `part_list_login` | Signs the account owner in, once per 90 days |
| `part_list_create` | Creates an empty list, private unless asked otherwise |
| `part_list_add` | Adds parts, then reads the list and compares it with what was sent |
| `part_list_get` | Reads one list, or lists the account's lists |

**Setting up.** MyLists accepts only a three-legged OAuth token, so the app
behind `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET` needs two things in
DigiKey's developer portal: a subscription to the **MyLists** API, and a
registered redirect address. `https://localhost` is assumed; set
`DIGIKEY_REDIRECT_URI` when the app registers another, character for
character. `DIGIKEY_ACCOUNT_ID` is sent as `X-DIGIKEY-Account-Id` when set,
for a user with several accounts.

**Signing in.** `part_list_login` returns an address. The user opens it,
signs in to DigiKey and allows access; the browser then goes to the redirect
address, normally a page that cannot load, and that page's whole address is
passed back as `returned_url`. The code in it lasts one minute and works
once. Where a minute is too short to pass through a chat, run
`python -m eda_agent.libimport.providers.digikey_lists login` in a terminal.

The access token lasts 30 minutes and is renewed without anyone noticing.
The refresh token lasts 90 days and DigiKey replaces it at every renewal, so
it is saved the moment it arrives. Tokens live in
`%LOCALAPPDATA%\eda-agent\digikey-tokens.json` (`DIGIKEY_TOKEN_FILE` moves
it), sealed with the Windows user's DPAPI key; elsewhere the file is readable
by its owner only. No token appears in a reply or a log.

**An item to add.**

| Field | What it is |
|---|---|
| `sku` | The exact DigiKey part number. It selects the packaging, so a manufacturer number is refused in its place |
| `quantity` | A whole number, 1 or more |
| `designators` | A list, or a string taken as written |
| `customer_reference`, `reference_designator` | Set one of the two references to something other than the designators |
| `description` | The component's `Description` parameter, kept in the part's `Notes` |
| `manufacturer` | Optional |

No other field is accepted, so a component's `Comment` cannot be sent.

**A designator is never cut in two.** The designators, all of them, comma
separated, go into `ReferenceDesignator` whole and as given. DigiKey keeps
only 48 characters of a `CustomerReference` and drops the rest without
saying so (observed live on 2026-11-16; its API description states no
limit). So the customer reference gets the designators joined by commas
with no spaces, to leave the label as much room as possible:

| Designators | `CustomerReference` |
|---|---|
| `R1, R2, R3` | `R1,R2,R3` |
| `R101` to `R109` (44 characters joined) | all nine |
| `R101` to `R110` (49 joined) | `R101,R102,R103,R104,R105,R106,R107,R108,+MORE` |
| one designator of 49 characters | `+MORE` |

When they do not all fit, it ends in `,+MORE`, and room for that ending is
kept free: in the third row `R109` would fit in 48 characters on its own,
but not with `,+MORE` after it. The item then says what was left out:
`customer_reference_omitted` lists the designators,
`customer_reference_warning` explains, and `designators` holds the full
text as given. What the list then holds is compared with what was sent, and
a difference is reported per item under `differences`.

A `customer_reference` given outright is fitted the same way when it is a
list of designators (`R1`, `C101`, `TP12`, `U3A`). Any other wording is the
user's own and is sent as written; over 48 characters it is refused, with
nothing added, because shortening it could change what it says.

**The exact orderable number.** DigiKey's own number on a list part is the
product's base number, often the reel, whatever packaging was entered. The
packaging in the list is the selected pack option of the part's quantity,
and `sku` is read from there; the base number is returned separately as
`base_part_number`. An item is `verified` only when that selected number is
the `sku` that was sent. When DigiKey names no selected packaging the item
is not verified and `sku_unverified` says so.

**All of the account's lists.** `part_list_get` without a `list_id` reads
the lists 100 at a time until DigiKey returns an empty page, up to 20
requests. `complete` is false, with `incomplete` giving the reason, when it
stopped before the end.

**Whether the reference reaches the bag label** is stated only on DigiKey's
forum, not in its API documentation: the list's customer reference is carried
into the cart's customer reference, which is printed on the label. Nothing
found says the reference designator is printed.

**Bad input adds nothing.** A malformed item refuses the whole call, naming
every problem of every item. With `check_products` (the default) each
distinct `sku` is first read from the catalogue, and one that is not a
DigiKey part number refuses the whole call too. If DigiKey rejects a batch
part-way, the reply says how many items were sent and lists the rest under
`not_sent`.

**What comes back.** `part_list_add` returns each item as sent, with the
`unique_id` DigiKey gave it, DigiKey's own `digikey_description`, and
`differences`, empty when the list holds exactly the `sku`, quantity and
references that were sent. `part_list_get` returns each part's `sku`,
`quantity`, `customer_reference`, `reference_designator`, `notes`,
`manufacturer_part_number`, `matched` and stock.

**Access policy of the hosts, checked rather than assumed.** Every host these
providers reach was checked for a `robots.txt` before anything was built
against it. That check changed the design once: `gitlab.com/robots.txt` carries
`Disallow: /api/v*`, and the GitLab API is where KiCad's canonical *symbol*
repository lives, so `public_libraries` does not touch GitLab and serves
footprints from GitHub instead. KiCad's symbols are already covered by
`kicad_local`, which reads them off disk. GitHub's API is used as documented,
with a User-Agent naming this project rather than impersonating a browser, one
recursive request per repository instead of directory walking, a week-long disk
cache so repeat searches cost nothing, and rate limiting treated as "back off"
rather than "no results". PartReel's `robots.txt` allows all agents and names
`ClaudeBot` and `GPTBot` explicitly. TrustedParts was evaluated and **dropped**:
it returns 403 to non-browser clients, so access is by arrangement rather than
open, and nothing here works around that.

One measured behaviour is worth naming, because it is the failure this layer
exists to prevent: **Mouser answers an invalid API key with HTTP 200** and an
`Errors` array. A client that judged success by status code would report a
rejected credential as a search that ran and found nothing. Payload-level error
detection is in the shared base class, not in five copies, and a mutation test
confirms removing it breaks the guard.

Each hit carries `formats`, `usable_in` and `import_with`: the tool that turns
that hit into a real part on the active backend. Those live on the provider,
not on the part, so without them a result gives no way to tell a lead from a
dead end. `import_with` is derived from the same map that gates a provider's
Altium claim, so it can only ever name an importer that exists, and it comes
back empty (never a guess) for a format nothing reads.

A hit is a lead, not a verified part. The `provenance` and `license` fields
report what the provider claims about where its geometry came from, and a blank
means unknown rather than permissive. Audit any imported footprint against the
manufacturer land pattern with `lib_audit_footprint_vs_datasheet` before
trusting it.

Pass `download_dir` to `part_fetch` to also write a provider's library files
there (off by default, since it writes to disk). Only known artefact kinds are
taken, and each is saved with the extension this code expects rather than one
read out of the payload.

Downloaded files are checked against the KiCad installed on this machine,
because a registry can publish a **newer** s-expression format than your KiCad
can open. Measured against the live service: PartReel ships format `20260206`
while KiCad 10.0.1 writes `20251024`, and KiCad's symbol parser refuses the
newer file outright ("Unable to load library") though its footprint parser
accepts it. Any such file comes back with a `*_warning` naming both versions,
rather than looking like a clean download.

## Planning a purchase from the BOM

Two read-only tools connect the purchasing BOM to the distributors. Neither
makes a cart or a list; a person reads the plan and then uses
`part_cart_create` or `part_list_add`. They keep their own defaults
(`build_quantity` 1, `prefer` lowest); the configured workflow with
assemblies, overage, allowance and review reasons is
[The Purchasing BOM](#the-purchasing-bom-assemblies-quantities-and-review).

| Tool | What it does |
|---|---|
| `part_purchasing_analyze` | The purchasing BOM, each line with what DigiKey and Mouser offer for its exact part number |
| `part_purchasing_plan` | A proposal: for each line one distributor, one orderable number, a quantity and a price |

**The BOM.** By default the purchasing BOM of the focused Altium project, as
`proj_get_bom(mode="purchasing")` builds it: the active variant's fitted
parts, grouped by Manufacturer and Part Number, with their Description and
designators. `include_not_fitted` counts the parts the variant leaves off.
`lines` gives the lines directly instead, and then Altium is not asked.
`build_quantity` multiplies every line.

**Exact parts only.** A distributor's product is taken for a line only when
its manufacturer part number is the line's (case and spaces aside) and its
manufacturer is the line's. A name that begins with the BOM's ("Murata" and
"Murata Electronics") is accepted and noted; an abbreviation ("TI") is not.
Otherwise the line is `not_found`, `manufacturer_mismatch` or `ambiguous`,
with what the distributor does list, and **no other part is ever put in its
place**.

**The plan.** For each line and each distributor, every packaging is priced
on its own terms:

- the quantity is the requirement raised to the minimum and then to the
  multiple; a reel with no stated multiple is sold whole;
- the price is the break that applies, and with `allow_overbuy` a larger
  break is taken when the whole line then costs less (the line says what it
  saves and how many are left over);
- a Digi-Reel's reeling fee is part of its price;
- an offer whose stock does not cover the quantity is not chosen.

Each line then goes whole to one distributor: the cheapest (`prefer`
`lowest`), or the one named (`digikey`, `mouser`), falling back to the other
with a note when the named one cannot sell it. A line is never split across
distributors. Prices in two currencies are not compared.

**Nothing is left out.** Every BOM line is in the plan, `planned` or
`unpurchasable` with the reason; components the BOM could not resolve are
listed beside it; `complete` is false whenever any part of the board is not
in the plan. `orders` lists the planned lines per distributor with their full
designators, ready for a list or a cart. `totals` gives each distributor's
subtotal and the combined component subtotal. **Shipping, taxes, tariffs and
fees are not included**; the catalogue APIs do not give them.

Distributor replies come through the cache: each line says whether its
figures were `cached` and when they were read, and `refresh` reads them
again.

## A cart at Mouser

Three tools put parts into a Mouser cart and read it back. A cart is not an
order: nothing here touches Mouser's Order API, a payment or a checkout, and
the cart is bought, if ever, by a person on Mouser's site.

| Tool | What it does |
|---|---|
| `part_cart_create` | Makes a new cart holding the given parts, and returns its `cart_key` |
| `part_cart_add` | Adds parts to the cart a `cart_key` names; without a key it does nothing |
| `part_cart_get` | Reads a cart by its `cart_key` |

They need `MOUSER_CART_API_KEY`, the key of Mouser's Cart and Order API. It
is a different key from the Search API's `MOUSER_API_KEY`. The `cart_key` is
the only way back to a cart, so keep it.

**What a cart line can hold** is what Mouser's API description gives it, four
fields and no others:

| Item field | Sent as | Limit |
|---|---|---|
| `sku` | `MouserPartNumber` | The exact Mouser part number, at most 80 characters |
| `quantity` | `Quantity` | A whole number, 1 or more |
| `customer_part_number` | `CustomerPartNumber` | Optional. At most 21 characters, no `*`; longer is refused, not shortened |
| `packaging` | `PackagingChoice` | Optional. `None`, `Cut_Tape`, `MouseReel` or `FullReel` |

**Mouser's cart has no field for reference designators, a description or a
note.** An item may carry `designators`, `description`, `manufacturer` and
`manufacturer_part_number`; they come back with the item in the result and
are never sent. When the last two are given they are compared with the part
Mouser put in the cart.

**Bad input makes no cart.** A malformed item, or the same `sku` twice,
refuses the whole call. With `check_products` (the default, one Search API
read per `sku`) so does a `sku` that is not a Mouser part number, or a
quantity below the part's minimum or off its multiple. A quantity is never
rounded here.

**What comes back.** After the insert the cart is read again by its key, and
each item is reported beside its `cart_line`: Mouser's part number,
manufacturer, manufacturer part number, quantity, customer part number,
packaging, prices, stock, minimum and multiple. `differences` is empty, and
`verified` true, when the cart holds what was asked. A part that was already
in the cart is not replaced; the item says what the line held before and
holds now.

Run live on 2026-11-16 with one part: the part number, quantity and customer
part number came back as sent, and Mouser chose `Cut_Tape` when no packaging
was asked for. What Mouser does with a quantity it does not accept has not
been seen live.

Adding a part that is already in the cart adds to its quantity. It does not
replace the line: a line at 10 that receives 10 more reads back as one line at
20, and the item says it "now holds 20". Nothing in the integration can
lower a quantity or remove a line, so a wrong quantity has to be corrected on
Mouser's site.

**Interoperability limitation: a cart opened in the browser can read back
empty.** Reproduced on 2026-11-17 with one cart holding two resistor lines,
built and checked through the API (create, insert, and readback all
succeeded):

| Step | Result |
|---|---|
| Before the browser | Two lines: one at quantity 20 and one at quantity 5, merchandise total $0.84 |
| Cart opened in a browser, not edited, left with the browser's Back button | Not changed by the user |
| API readback of the same cart key afterwards | Succeeded, with no error, but zero items and merchandise total $0.00 |

The lines were not reported as removed or changed by the API, and the cart
key was still valid, so the cause is not known. The sequence shows only that
the API readback changed after the browser visit, not what the browser did.
This is a reproducible limitation of using the website and the API on the
same cart. It is not a confirmed Mouser API bug. Until it is understood:

- Finish cart modifications and verification through the API before opening
  that cart in a browser.
- Treat an empty readback as possibly a site-side change, not as proof that
  the API insert failed. Check the cart on Mouser's site before retrying an
  insert, so that quantities are not doubled.

## The Purchasing BOM: assemblies, quantities and review

Three things are easy to confuse:

| | Standard BOM | Purchasing BOM | Cart or list |
|---|---|---|---|
| Tool | `proj_export_purchasing_bom_csv` | `part_purchasing_export_csv` | `part_purchasing_cart_prepare`, then `part_purchasing_cart_commit` |
| Assemblies | always one | configurable, default 2 | as the Purchasing BOM |
| Distributor | never asked | read only: DigiKey and Mouser | one account is written, after approval |
| Result | a CSV, five default columns | a CSV with quantities, prices, status, reasons | a DigiKey list or a Mouser cart |

The Standard BOM is unchanged. Nothing here orders anything or checks out.

### What a row is

`part_purchasing_export_csv` writes one row per BOM line: every fitted line,
every unresolved component (one row each, status `blocked`, with Altium's
own reason), and, with `include_dnp`, the parts the variant does not fit
(Required Qty 0, status `not_fitted`, not looked up, not bought). A line
that cannot be bought is a row with its status and every reason: nothing is
hidden, split, or filled with another manufacturer's part. Only the exact
manufacturer part number of the BOM is used, unless the user approves a close
variant of it ([below](#close-part-number-matches)). A suggested replacement
is shown as text and never ordered.

Columns are chosen and ordered with `columns`, as for the Standard BOM:
`bom_quantity`, `required_qty`, `purchase_qty`, `surplus`, `part_number`,
`manufacturer`, `description`, `reference_designators`, `footprint`,
`distributor`, `distributor_sku`, `packaging`, `unit_price`, `extended_cost`,
`currency`, `stock`, `restock_date`, `restock_qty`, `restock_covered`,
`restock_detail`, `lifecycle`, `purchasing_status`, `review_reasons`,
`review_detail`, `quantity_rule`, `price_break`, `retrieved_at`. Left out,
the configured columns are used, or a built-in set. The file is UTF-8 with a
byte order mark and CRLF line ends, text from a distributor is neutralized
against spreadsheet formulas, and it goes through the same export folder,
repository guard and never-overwrite naming as the Standard BOM. The default
name is `<project>_purchase_BOM_<n>x.csv`.

Totals in the reply are per currency and never added across currencies.
Shipping, taxes, tariffs and fees are not included.

### When stock may come back

A line that cannot be bought for lack of stock says when it may be. The columns
are filled only on such a line (no distributor can supply the requirement) and
only with what a distributor states: nothing is estimated.

| Column | Meaning |
|---|---|
| `restock_date` | The earliest dated incoming lot at any distributor (`yyyy-mm-dd`) |
| `restock_qty` | The quantity of that lot |
| `restock_covered` | The date the stock now plus the incoming lots listed so far first reaches the requirement, or `not covered by the incoming stock listed`, `no incoming stock listed`, or `date not stated` |
| `restock_detail` | Per distributor: stock now, every incoming lot, whether and when they cover the requirement, the manufacturer's lead time and public stock, and when it was read |

Mouser lists incoming lots (quantity and date). DigiKey's catalogue gives none,
so for DigiKey the detail says "no incoming stock listed" and quotes the
manufacturer's lead time in weeks, which is a manufacturer figure and not a
date. A lot is a distributor's statement and can slip: check it before relying on
it. The dates are written as ISO dates and are not counted among the values
Excel would reinterpret. `restock_date`, `restock_qty` and `restock_covered` are
in the built-in columns and the template for a new `config.toml`; an existing
file keeps its own column list, so add them there (or pass `columns`) to see
them.

### How the quantity is chosen

Required Qty is the BOM quantity times the number of assemblies. Purchase Qty
starts there and is never below it. All arithmetic is exact (`Decimal`).

1. It is raised to the offer's minimum order and order multiple (a reel sold
   whole counts its standard package).
2. An inexpensive part (unit price at or below `overage_max_unit_price`, in the
   overage currency) gets overage: `ceil(required * overage_factor)`.
3. The cost of that quantity is the baseline. A price break above the one in
   force is then looked at:
   * A break that costs LESS than the baseline is taken wherever it is on the
     price ladder, at any unit price and however far above the requirement it
     lies. The cheapest wins.
   * Otherwise a break that costs at most
     `baseline * (1 + extra_spend_percent / 100) + extra_spend_flat` is taken,
     among the next `price_break_window` feasible breaks, the furthest one.
     This holds for expensive parts as well as cheap ones.
4. There is no limit on how many times the requirement the quantity may reach.
   Stock, a maximum order quantity, the minimum, the multiple and the
   packaging are never exceeded, and a break stock rules out uses no position
   of the look-ahead.

Worked example (defaults, 25 per assembly, 2 assemblies, so 50 required): at
0.10 each up to 999 and 0.011 from 1000, overage gives a baseline of 55 at 5.50
and an allowance of 11.05; 1000 at 0.011 costs 11.00, so 1000 are bought, 20
times the requirement, and the row is `ready` with the surplus and the reason
shown in it.

Stock decides what is possible. If a distributor's stock cannot cover the
requirement, the other is tried; if neither can, the line is blocked, kept,
and shows the quantity it would need, with no partial purchase. If stock
covers the requirement but not the overage, the overage shrinks to the
largest valid quantity stock allows.

### Which distributor

`prefer` is `digikey` by default: DigiKey gets the line whenever it can supply
the requirement and give an acceptable offer, and Mouser, marked
`FALLBACK_DISTRIBUTOR`, when it cannot. `mouser` swaps the roles and `lowest`
takes the lower final cost (with two currencies the preference order decides,
and the line says so). A line is never split between distributors.

### Close part number matches

When no distributor lists a line's exact manufacturer part number, a
distributor often lists one that differs a little: the same letters and
digits with other punctuation (the BOM says `ABC123X`, the distributor
`ABC-123-X`), or the same number with something added at the end. Those near
misses are found and sorted for a person to judge. They are **advice, never
permission**: a candidate is used in a purchase calculation, a CSV row or a
cart only when the user has approved that exact original and candidate pair,
and the number in the Altium schematic is never changed (no Altium document is
touched).

The search runs only for a line no distributor matched exactly, and keeps only
what a distributor lists whose letters and digits equal the BOM's number or
begin with them and go on. Exact matches come first and are never searched
for alternatives.

| Label | What it means | Needs |
|---|---|---|
| Verified Alias | The manufacturer's own document says the two numbers are the same part ([below](#verified-manufacturer-aliases)). | Evidence in the registry, and the manufacturer the BOM's. |
| Formatting Match | The letters and digits are identical; only punctuation differs (`USB4110GFA` and `USB4110-GF-A`). | A real listing, and the manufacturer the same (or a known short name). |
| Likely Match | The candidate adds a suffix that is a recognised packaging marker, such as `-TR` for tape and reel. | The suffix is a recognised marker, the distributor's own packaging for the candidate is tape or reel, the manufacturer is identical, and nothing contradicts it. |
| Possible Match | A short suffix whose meaning is not verified (`ABC123A5`), or a recognised marker the distributor does not confirm. | Verify the variant in the manufacturer's ordering information. |
| Uncertain Match | A long suffix (5 or more letters and digits) that is not a packaging marker, a suffix that continues the digits of the base number with no separator (`LM358` and `LM3584`), or a manufacturer name that differs. | Treat as a probably different part until proved otherwise. |

Suffix length is only one input and never decides alone: a four letter
`-REEL` that the distributor confirms is Likely, a one letter unknown suffix is
only Possible. No percentage or score is given: each candidate has its label,
the evidence for it and what is still doubtful. Packaging markers (`TR`,
`REEL`, and a few makers' own, such as Diodes' `-7` and `-13` or TI's `R`) only
count when the distributor's packaging confirms them. The manufacturer name is
evidence, not a filter: a known short form (`GCT` for Global Connector
Technology) is noted and treated like a partial match, and any other differing
name is listed but never rated above Uncertain. Descriptions are compared too,
but as weak evidence only (distributors abbreviate them), so they are shown and
never change a label.

All candidates are presented together at the end of the analysis, most likely
first (verified alias, formatting, likely, possible, uncertain; then identical manufacturer,
then in stock), in the reply (`close_matches`) and in a companion file next to
the CSV (`<name>_close_matches.csv`) with, for each: the original number, the
candidate, the manufacturer, the suffix difference, the label, what the
difference means, the distributor, stock, unit price at the quantity needed,
the SKU, whether it is approved, the evidence and the doubts, and the line and
designators it belongs to.

#### Who authorizes a match, and for which project

A candidate is used only when it is authorized, in this order:

1. **The project's manual rejection** beats everything, for that project only.
2. **The project's manual approval.**
3. **An approval given for one call** (`approve_mpn_variants`): nothing is
   remembered.
4. **A global rule** in `config.toml` (below), which applies to every project.
5. **The verified alias option** `[matching] auto_approve_verified_aliases`
   (below), for a manufacturer-documented alias only.

Manual approvals and rejections belong to ONE Altium project and never reach
another. A project is identified by a hash of the canonical, absolute path of
its `.PrjPcb` file (symbolic links and `..` resolved, case and slashes folded on
Windows), never by the file name alone: two projects that are both called
`Board.PrjPcb`, in different folders, are two projects, and the same part
number in the second one asks again. A project that cannot be identified (a
relative `project_path`, or BOM `lines` given directly) has no manual
decisions: only a global rule or an approval for the call applies.

Decisions are kept locally, outside Git, in the export folder
(`EDA_AGENT_EXPORT_DIR`): `purchasing/mpn_decisions/<project id>.json`. The file
holds part numbers, the decision, when and how, and the project path; it holds
no quantity, price, SKU or credential. A file that names another project is
refused. They are recorded with `part_purchasing_mpn_decisions`:

```
part_purchasing_mpn_decisions(action="status")           # the candidates and decisions
part_purchasing_mpn_decisions(action="approve", pairs=[
    {"original_mpn": "USB4110GFA", "candidate_mpn": "USB4110-GF-A"}],
    note="checked the datasheet")
part_purchasing_mpn_decisions(action="reject", pairs=[...])
part_purchasing_mpn_decisions(action="revoke", pairs=[...])   # also "list"
```

A pair must be a candidate found now (its letters and digits equal the original's
or begin with them, or it is a registered manufacturer alias of it). It names the original number and the candidate's exact
spelling: another spelling of the candidate is not decided. Once recorded, the
Purchasing BOM export, a later analysis, the DigiKey list and the Mouser cart
tools use it without being asked again, with no argument to pass.

A manual decision is bound to what it was made about: the component's
manufacturer and part number, and the candidate's spelling and manufacturer.
If any of them is no longer the same it is **stale**: reported (in the reply's
`mpn_decisions` and as a warning) and NOT applied, never silently. That covers
a changed manufacturer or part number in the schematic, a candidate that a
distributor now lists under another maker, and a candidate that is no longer
listed. A stale decision is not deleted: decide again, or revoke it. A decision
for a number that now matches exactly is reported as unused. A stale manual
decision does not block an authorization from a global rule or the call, which
stand on their own.

These decisions authorize one thing: which part number may be looked up and
ordered. They are not part of any purchasing plan or cart fingerprint, and
authorize no quantity, price or destination: a previously approved number
still needs a fresh approval of the proposal's figures, and a different list or
cart is a different proposal.

An approved line is read like any exact match. Its row keeps the BOM's number in
Part Number, shows the number ordered in Matched MPN, and records in MPN
Approval how it was authorized (`project approval`, `this call only` or
`global rule: Likely Match`) and in MPN Approval Audit the original number and
maker, the matched number and maker, the label, the method, when it was decided,
the decision id, the project id and the note. The companion file has the same
for every candidate, rejected ones included. The line carries
`APPROVED_MPN_VARIANT`; when the distributor's name for the maker differs from
the BOM's it is a `review` line, so it must be named in `include_lines` for a
cart. In a cart proposal the item shows both numbers and the distributor is
asked to hold the number actually ordered. Without a decision a line with
candidates stays `blocked` (`MPN_NOT_FOUND`) with the information reason
`CLOSE_MATCH_AVAILABLE`; when every candidate was rejected it carries
`CLOSE_MATCH_REJECTED` instead and is not asked about again.

#### Global rules

`config.toml` may authorize some matches for every project:

```
[matching]
auto_approve = ["likely", "formatting"]
```

Only `formatting` (punctuation differs) and `likely` (a confirmed packaging
suffix) can be named, and only for a candidate whose manufacturer name is
identical: a known short form, a differing name, a possible or an uncertain
match always needs a person. Anything else in the list is refused with a
warning. The rule holds no part number and no project, the default is empty,
and a project's manual rejection overrides it for that project.

#### Verified manufacturer aliases

Some makers sell one part under several orderable numbers that differ only in a
code they define. KEMET's packaging C-Spec is the first case: `C0603C439C5GACTU`
and `C0603C439C5GAC7411` are the same 4.3 pF part on a 7 inch and a 13 inch
reel. A distributor may list one while the BOM names the other, and the generic
suffix search cannot see it (the numbers do not begin alike).

The registry (`src/eda_agent/export/purchasing_aliases.py`) holds only what a
manufacturer document says, each entry with its evidence: the document, its
revision, the page or table and the quotation. Two kinds of entry exist:

* a **family rule**: a documented number structure and the table of ordering
  codes the document defines. Two numbers are aliases only when every character
  before the code is identical, both codes are in the document's table, each
  code exists for the part's case size and both are of the same documented grade.
  A code that is not in the table is an alias of nothing, however similar the
  numbers look. For KEMET commercial C0G the table is `TU` (7 inch reel,
  unmarked), `7411` (13 inch reel, unmarked, EIA 0603 and smaller) and `7210`
  (13 inch reel, unmarked, EIA 0805 and larger), read from KEMET's
  `C1003_C0G` datasheet of 2/20/2025. An automotive code such as `AUTO7411` is a
  different grade and is no alias.
* an **explicit alias**: numbers a manufacturer document names as equivalent.

There is no generic suffix substitution. KEMET `7867` is **not** registered: the
C1003_C0G datasheet does not mention it, so `C0603C439C5GAC7867` is not an alias
of `C0603C439C5GACTU` until a KEMET document that does is added as evidence.

A verified alias is found by asking each distributor for the alias numbers
by their exact number (also where the BOM's own number exists only under another
maker), and is then labelled `Verified Alias`. It is used like any approved
candidate: the distributor's actual SKU, minimum, multiple, packaging, stock and
price breaks are read for the matched number, and the quantity engine works from
those. The schematic's number is never changed.

```
[matching]
auto_approve_verified_aliases = true
```

* **Off (the default, also when the key is absent).** The alias is listed as a
  candidate and needs the usual per-project approval with
  `part_purchasing_mpn_decisions`; it is not used before that.
* **On.** A verified alias is accepted without being asked again when the
  distributor lists it under the BOM's own manufacturer (an identical name) and
  it is the only verified alias listed at that distributor. Two or more at one
  distributor, a differing manufacturer name or a known short form are left for a
  person. The option is independent of `auto_approve` and never touches a
  candidate that is not a verified alias.
* **A project's rejection overrides it**, for that project only, as it overrides
  every other authorization. A manual approval of the project is applied first.
* An existing `config.toml` is never rewritten, so the key is added by hand.

An applied alias is recorded in the Purchasing BOM wherever the columns are:

* **Review Reasons** carries `VERIFIED_MPN_ALIAS (BOM <number> -> ordered <number>)`,
  an information reason, so the difference is visible even with a column
  selection made before aliases existed. The line is not `MPN_NOT_FOUND` or
  `MANUFACTURER_MISMATCH` only because the ordering numbers differ.
* The export reply and `warnings` name every alias applied, with who accepted it.
* **MPN Approval Audit** holds the original number and maker, the matched number
  and maker, the label, the matching method and rule, the approval source, who
  authorized it and the manufacturer's evidence. The columns `mpn_method`, `mpn_source`
  and `mpn_evidence` hold the method, the approval source and the evidence on
  their own, and `matched_mpn` and `mpn_approval` work as for any approved
  variant. The companion `_close_matches.csv` has the same.
* In a DigiKey list or Mouser cart proposal the item shows both numbers, the
  distributor is asked to hold the number ordered, and the approval fingerprint
  includes the rule, the method and the approval source: switching the option, or
  approving manually instead, is a new proposal that needs its own approval.

### Settings: config.toml

The first time a purchasing tool needs it, `config.toml` is created in the
export folder (`EDA_AGENT_EXPORT_DIR`, else `EDA Agent/exports` under the
home folder), with the defaults and comments. It is never rewritten: edit it
freely. For each setting separately, an argument of the same name overrides
the file, and the file overrides the default; every reply says which one a
value came from.

| Setting (file key) | Argument | Default |
|---|---|---|
| `assemblies.count` | `assemblies` | 2 |
| `overage.factor` | `overage_factor` | 1.10 |
| `overage.max_unit_price` | `overage_max_unit_price` | 5.00 |
| `overage.currency` | `overage_currency` | USD |
| `upgrade.extra_spend_percent` | `extra_spend_percent` | 10 |
| `upgrade.extra_spend_flat` | `extra_spend_flat` | 5.00 |
| `upgrade.extra_spend_currency` | `extra_spend_currency` | USD |
| `upgrade.price_break_window` | `price_break_window` | 2 |
| `distributors.prefer` | `prefer` | digikey |
| `review.large_surplus_ratio` | `large_surplus_ratio` | not set (no warning) |
| `matching.auto_approve_verified_aliases` | none (file only) | false |

The percentage, the fixed amount and the window are independent, and zero is
valid for each. The fixed amount applies only to offers in its currency. The
file never holds credentials or design data: credentials stay in the
environment variables, a key that looks like one is refused, and an invalid or
unknown key is reported and ignored, never fatal. `[review.severity]` may
change the severity of the reasons marked below, and never of the fixed ones.

### Status and review reasons

A row's status comes from its reasons: any `blocking` reason makes it
`blocked`, otherwise any `review` reason makes it `review`, otherwise it is
`ready`; `info` never changes it. A part may carry several reasons. A large
surplus alone never blocks a line or asks for approval. With
`large_surplus_ratio` set, a row above it gets `LARGE_SURPLUS`, information
only (a user may raise it to review, never to blocking).

The fixed reasons cannot be lowered by any setting, so an end of life,
obsolete or discontinued part can never become eligible for a cart through a
configuration change.

| Code | Default | Can a setting change it | Meaning |
|---|---|---|---|
| `UNRESOLVED_COMPONENT` | blocking | no, fixed | The component has no usable Manufacturer and Part Number |
| `MISSING_MANUFACTURER` | blocking | no, fixed | No usable Manufacturer |
| `MISSING_MPN` | blocking | no, fixed | No usable Part Number |
| `MPN_NOT_FOUND` | blocking | no, fixed | No distributor lists the exact manufacturer part number |
| `MANUFACTURER_MISMATCH` | blocking | no, fixed | The number exists only under another manufacturer |
| `AMBIGUOUS_MANUFACTURER` | blocking | no, fixed | Two manufacturers could each be the BOM's |
| `MANUFACTURER_PARTIAL_MATCH` | review | yes | The manufacturer names agree only as a prefix |
| `BOM_CONFLICT` | review | yes | The BOM data for this line conflicts |
| `BOM_INCOMPLETE` | blocking | no, fixed | Altium's read of the project was cut short; quantities may be short |
| `DUPLICATE_SKU` | blocking | no, fixed | Two BOM lines resolve to one distributor number |
| `OUT_OF_STOCK` | blocking | no, fixed | No matched distributor has the part in stock |
| `INSUFFICIENT_STOCK` | blocking | no, fixed | No matched distributor has enough stock for the requirement |
| `STOCK_NOT_STATED` | review | yes | The distributor states no stock for this offer |
| `OVERAGE_LIMITED_BY_STOCK` | info | yes | The overage was reduced to what stock allows |
| `OVERAGE_SKIPPED_CURRENCY` | info | yes | No overage: the offer is not in the overage currency |
| `ALLOWANCE_FLAT_SKIPPED` | info | yes | The fixed allowance was left out: the offer is not in the allowance currency |
| `FALLBACK_DISTRIBUTOR` | info | yes | The preferred distributor could not supply the line |
| `MISSING_PRICE` | blocking | no, fixed | No usable price |
| `MISSING_CURRENCY` | blocking | no, fixed | A price with no currency |
| `CURRENCY_MIXED` | review | yes | The distributors price in different currencies; no cost comparison was made |
| `MOQ_ADJUSTED` | info | yes | The quantity was raised to the minimum |
| `MULTIPLE_ADJUSTED` | info | yes | The quantity was raised to the order multiple |
| `MOQ_NOT_STATED` | review | yes | No minimum is stated; the first price break was taken |
| `MULTIPLE_NOT_STATED` | review | yes | No multiple is stated; the reel's standard package was taken |
| `PACKAGING_REEL_WHOLE` | info | yes | A full reel is sold whole |
| `PURCHASE_LIMIT_EXCEEDED` | blocking | no, fixed | The requirement exceeds the distributor's maximum order quantity |
| `LARGE_SURPLUS` | info | info or review | The purchase quantity is far above the requirement |
| `LIFECYCLE_NRND` | review | yes | Not recommended for new designs |
| `LIFECYCLE_LAST_TIME_BUY` | review | yes | Last time buy |
| `LIFECYCLE_EOL` | blocking | no, fixed | End of life |
| `LIFECYCLE_OBSOLETE` | blocking | no, fixed | Obsolete |
| `DISTRIBUTOR_DISCONTINUED` | blocking | no, fixed | Discontinued by the distributor |
| `LIFECYCLE_UNKNOWN` | review | yes | No distributor states a lifecycle status this project knows |
| `LIFECYCLE_NOT_STATED` | info | yes | One distributor states no lifecycle status and no other distributor says active (Mouser's silence beside DigiKey's Active is not reported) |
| `LIFECYCLE_PRELIMINARY` | info | yes | Preliminary or new product |
| `DISTRIBUTOR_UNAVAILABLE` | info | yes | One distributor could not answer |
| `NO_DISTRIBUTOR_DATA` | blocking | no, fixed | No distributor answered for this line |
| `STALE_PRICING` | info | yes | The figures were reused from the cache and are old |
| `CLOSE_MATCH_AVAILABLE` | info | yes | Close part number candidates are listed; none is used until approved |
| `CLOSE_MATCH_REJECTED` | info | yes | Every close candidate was rejected for this project |
| `APPROVED_MPN_VARIANT` | info | yes | A close variant of the BOM's part number, approved by the user, is ordered |
| `VERIFIED_MPN_ALIAS` | info | yes | The manufacturer documents the ordered number as the same part as the BOM's; both numbers are shown |
| `SETUP_QUANTITY_RAISED` | info | yes | The quantity was raised to a feeder or setup minimum |

### Putting the Purchasing BOM in a list or cart

Two steps, with a person between them. Neither places an order.

1. `part_purchasing_cart_prepare` READS ONLY. It builds the Purchasing BOM from
   fresh distributor figures (never the cache) and returns a proposal: the
   items that would go in, the lines that would not and why, the totals, and a
   `proposal_id`.
2. `part_purchasing_cart_commit` writes to one account, only with
   `approved=True` and the `proposal_id` of a proposal the user has read.

Which lines go in: a `ready` line bought at the chosen distributor. A
`review` line goes in only when its line id is named in `include_lines`. A
`blocked` line (unresolved, no match, out of stock, no price, end of life,
obsolete, discontinued) never goes in, even when named. To buy everything at
one distributor, set `prefer` to it. Where it goes: Mouser needs `cart_key`
(an existing cart) or `create_new=True`; DigiKey needs `list_id`, or
`create_new=True` with `list_name`. A DigiKey list is not a cart: it is opened
in the account to make one.

The `proposal_id` is a hash of the distributor, the target, and for every item
its SKU, quantity, packaging, unit price, currency, lifecycle status and
designators. `commit` rebuilds the proposal from fresh figures first; if the id
no longer matches, nothing is written and the new proposal is returned for the
user to read again.

Mouser adds to a line when a part is inserted again, so a commit works to
target totals: it reads the cart, adds only what is missing, and never lowers
or removes a quantity. Committing the same proposal again after a timeout
adds nothing twice. A journal in the export folder (`purchasing/journal`, one
file per proposal, no credentials) keeps what was meant and what was seen, and
the journal is written before the first write. Every item is read back and
reported as verified or not.

Unknown states are handled conservatively. A cart that reads back empty, or
lower than the journal says it held (a Mouser cart has read back empty after
being opened in a browser), is not touched until the user has looked at it and
the call passes `cart_state_checked=True`. A cart or list creation that may
have happened is never repeated: the user finds it and prepares a new proposal
naming its key. A DigiKey line already in the list with another quantity is
reported and left alone.

### Example calls

A Standard BOM (one assembly, five columns, no distributor):

```
proj_export_purchasing_bom_csv()
```

A Purchasing BOM for 5 boards, DigiKey preferred, a wider allowance, and the
columns a buyer wants:

```
part_purchasing_export_csv(assemblies=5, extra_spend_percent=25,
    columns=["required_qty", "purchase_qty", "part_number", "manufacturer",
             "distributor", "distributor_sku", "unit_price", "extended_cost",
             "purchasing_status", "review_reasons"])
```

The same, using a close part number the user checked (see above): record the
decision once for the project, then export as usual (or approve for one call
with `approve_mpn_variants=[{"original_mpn": "ABC123", "candidate_mpn":
"ABC123-TR"}]`):

```
part_purchasing_mpn_decisions(action="approve", pairs=[
    {"original_mpn": "ABC123", "candidate_mpn": "ABC123-TR"}])
part_purchasing_export_csv()
```

Preparing a Mouser cart (then, after the user agrees, the commit with the
returned id):

```
part_purchasing_cart_prepare(distributor="mouser", prefer="mouser",
    create_new=True)
part_purchasing_cart_commit(proposal_id="<the id returned>", approved=True,
    distributor="mouser", prefer="mouser", create_new=True)
```

## Getting a KiCad-format part into Altium

Two of the three providers publish KiCad format, so on the Altium side a hit
would otherwise be a dead end. `lib_kicad_import` reads `.kicad_sym` and
`.kicad_mod` and produces the same thing `lib_easyeda_import` does: with
`target=altium`, an ordered plan of this server's own library tools, run
tool by tool. Altium's binary library formats are never synthesized.

The two importers share one neutral geometry model and one Altium emitter, so
they cannot drift apart: a fix to pad shapes or arc handling lands in both. The
s-expression reader is written here rather than taken as a dependency, which
keeps the escaping rules (a footprint named `2.5"`, a description containing
parentheses) verifiable instead of trusted.

Eight things about the format are silent when handled wrongly, and all eight
produce something that still looks like a converted part:

- **Derived symbols.** Over half of KiCad's standard entries (12209 of 22728 in
  10.0.1) carry no geometry at all: they are `(extends "PARENT")` and inherit
  the parent's pins and body, restating only the properties that differ. That
  link is followed, with the child's own values winning and everything it does
  not restate inherited. Not following it yields a part with no pins.
- **Multi-part components.** A quad gate keeps each gate in its own unit, all
  drawn at the same coordinates. They convert in one call to a real Altium
  multi-part symbol (`part_count` plus per-pin `owner_part_id`), not to N
  symbols to merge by hand; pass `unit=N` to take a single sub-part instead.
  Merging units into a flat symbol would stack every unit's pins on the same
  points and still look converted.

  Supply rails come across whichever way the source expresses them. A symbol
  that puts them in **unit 0** (shared by every unit; 678 unit-0 sub-symbols
  in KiCad 10.0.1 carry pins) maps straight onto Altium's `owner_part_id=0`,
  so a CD4001 becomes four gates sharing one Vdd/Vss. A symbol that gives them
  their **own unit** instead keeps that structure, and a warning names the
  shared alternative with the exact edit rather than silently reinterpreting
  what the file says. Both forms are legitimate; only one of them is what the
  file actually contains.
- **Pin electrical type.** Carried across rather than flattened, because it is
  what ERC reasons about: an open-collector output recorded as passive stops
  ERC asking for its pull-up, and two of them driving one net stops being a
  reported conflict. KiCad's `open_collector`, `open_emitter` and `tri_state`
  map to Altium's `open_collector`, `open_emitter` and `hiz` (1827, 119 and
  1858 pins respectively in KiCad 10.0.1). `no_connect` becomes passive, and
  that one is a genuine gap rather than a choice: Altium's pin vocabulary has
  no "not connected" (an unused pin carries a No-ERC directive instead).
- **Hidden pins.** Kept, and kept hidden (5378 of the 106032 pin definitions
  in KiCad 10.0.1 are hidden). Dropping them would lose real supply and
  no-connect pins; showing them would clutter every symbol that hides them.
  Both the modern `(hide yes)` and the older bare `hide` spelling are read,
  since a library can predate the KiCad that opens it.
- **Body styles.** `NAME_1_1` and `NAME_1_2` are the same unit drawn two ways
  (KiCad's DeMorgan alternate), with the same pins. Taking both duplicates
  every pin, so one style is converted and the other is reported.
- **Rounded rectangle pads.** Used by 116 of the 206 SMD footprints sampled
  from KiCad 10.0.1, and Altium supports the shape natively, so it is not
  flattened to a plain rectangle. The corner value does not carry over
  directly: Altium's
  documentation defines its percentage against *half* the shortest pad side,
  while KiCad's `roundrect_rratio` is measured against the *whole* shorter
  side, so the conversion is a factor of two. Multiplying by 100 would halve
  every corner radius and look entirely plausible.
- **Y axis.** `.kicad_sym` is Y-up like the neutral model, `.kicad_mod` is
  Y-down. A sign error mirrors the land pattern.
- **Pin angles and arcs.** KiCad's pin angle already matches the neutral
  convention and passes through untouched (EasyEDA's is 180 degrees off, and
  the asymmetry is deliberate). Arcs are stored as start/mid/end, so the radius
  and sweep are recovered from the circle through those three points.

A local hit can also carry its **3D body**. KiCad ships STEP models and
Altium's linker takes STEP, so `part_fetch` resolves the footprint's model
reference against the installed `3dmodels` tree and `lib_kicad_import` adds a
`lib_link_3d_model` step for it (while the `.PcbLib` is still the active
document, which is where that tool has to run). The path has to be one that
resolved: the tool loads the file, so a guess would either fail on execution or
attach the wrong shape. An unresolved reference is reported instead.

Generated KiCad files are checked against **KiCad's own parser** (`kicad-cli`),
not just re-read by this code. That matters because this reader is lenient by
design and a round trip through it cannot see output KiCad refuses: the writer
once emitted two graphic-style tokens on an inverted pin, which read back as a
merely-missing bubble here and as `Unable to load library` in KiCad. Those
tests skip when KiCad is absent.

Anything with no faithful Altium equivalent comes back in `warnings` rather
than being quietly approximated. The library API takes one hole diameter and
no plating flag, so a **slotted** drill is emitted round and an **unplated**
hole is emitted plated: both are the right size and the wrong thing, and
neither would show up anywhere downstream. Custom and trapezoid pads are
emitted as their bounding rectangle.

**Solder-paste and mask apertures are never emitted as pads.** Fine-pitch
chip footprints subdivide paste with apertures that carry no copper (332 of
the 6902 pads in KiCad 10.0.1's sampled libraries), and an Altium pad always
carries copper, so emitting one would short the pads the aperture exists to
subdivide. They are skipped because of their layer, not because they usually
lack a designator, and reported as apertures with the advice to draw them as
paste-layer regions.

**Active-low and clock pin markers** are read but cannot be written. `ISch_Pin`
has the slots (`Symbol_OuterEdge` for the inversion bubble, `Symbol_InnerEdge`
for the clock wedge) and `lib_add_pins` has no field for either, so setting
them needs a bridge change. They are reported per part rather than dropped
silently, because an active-low pin drawn plain states the opposite of the
truth. Both of KiCad's spellings count: `inverted` (the bubble) and `*_low`
(IEEE's wedge) mean the same thing.

These checks live in the shared emitter, so they apply to `lib_easyeda_import`
too.

Check the result against the manufacturer land pattern with
`lib_audit_footprint_vs_datasheet` before using it.

## KiCad

KiCad support talks to a running KiCad over its API socket, so - unlike the Altium side - there are no scripts to install and no extra packages. Requirements: KiCad 10, its API enabled (Preferences → Plugins → Enable KiCad API), and the board open in the PCB editor. KiCad 10.0 answers board requests only, so schematic work goes through the `.kicad_sch` file and `kicad-cli`.

The KiCad backend covers, at parity with what KiCad's API and CLI expose:

- **Review** - an EDA-agnostic design review (annotation, connectivity, shorts, decoupling, net classes) that runs the same engine on the PCB and, via the netlist, on the schematic; plus a one-call `kicad_full_review` that adds DRC, ERC, and schematic↔PCB comparison.
- **Checks** - geometric DRC and schematic ERC via KiCad's own `kicad-cli`.
- **Reads** - footprints, pads, tracks, vias, zones, shapes, text, stackup, layers, net classes, board outline, project info, netlist, and a consolidated BOM.
- **Exports** - every `kicad-cli` format: Gerbers, drill, STEP/GLB/VRML/STL/3D-PDF, PDF/SVG/DXF, position files, IPC-2581, ODB++, IPC-D-356, plus schematic BOM/netlist/PDF/SVG.
- **Authoring** - place/move/rotate/lock components, edit values, and create tracks, vias, zones, text, and graphics.
- **Calculators** - the same trace-width, impedance, termination, length-match, and thermal-via sizing tools as the Altium backend (pure physics, EDA-independent).

The neutral tools (`review_design`, `run_drc`, `run_erc`, `get_board_info`, `list_components`, `list_nets`) work on whichever backend is active.

> If you'd rather not register the script globally, you can also open `Altium_API.PrjScr` via **File > Open...** and launch `StartMCPServer` from the **Run Script...** dialog the same way; the dialog picks up any loaded script project.

