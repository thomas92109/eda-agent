# Purchasing workflow: requirements and plan

Status: implemented on branch `feature/distributor-pricing`, milestones M1
to M5, not yet committed (see section 13 for what exists, the deviations
from this text and the outstanding limitations). It began as a proposal
written from an inspection of the code at commit `083c4fd`. No live
distributor call, cart change or BOM export was made in writing or testing it.

All part numbers, prices, makers and quantities in this document are
synthetic.

Revision 2 records these clarified rules, which replace the first draft:

- Paid price-break upgrades are allowed for expensive and inexpensive parts
  alike, bounded only by the spending allowance and the look-ahead window.
- There is no multiplier limit on quantity. The "2" in the original script
  was a look-ahead of two price breaks, not twice the requirement.
- Cost-saving upgrades are considered along the whole price ladder.
- A large surplus alone never blocks a line or asks for approval. It is
  always reported, and an optional warning is configurable.
- The spending allowance is three independent settings, each overridable per
  call.
- A feeder or setup minimum quantity is recorded as a future requirement.

Revision 3 (implementation start) settles the open questions that blocked
the quantity engine:

- Only feasible, distinct price-break opportunities count toward the paid
  look-ahead. A break above stock or above the maximum order quantity, or one
  that rounds to a quantity already listed, does not use a position (this
  replaces the recommendation of A2 and settles A5).
- Stock-limited quantities follow section 5.5.1: the other distributor is
  tried before a line is blocked, an overage shrinks to the largest valid
  quantity stock allows, and a blocked line never gets a partial purchase.
- The default distributor preference is DigiKey, with Mouser as the fallback
  when DigiKey cannot supply the requirement or give an acceptable offer.
  Lowest cost stays selectable (section 5.9).
- End of life, obsolete and distributor discontinued parts are blocking, and
  configuration cannot lower those severities, so they can never become
  cart eligible through a settings change.
- The approval fingerprint of a cart proposal covers prices, currency,
  lifecycle status, SKU, packaging and quantities (CART-2).
- Implementation status is in section 13.

## 1. Goal and scope

Extend the existing purchasing features into three clearly separate
workflows, without duplicating what exists:

| Workflow | One line summary | Talks to distributors | Writes outside the export folder |
|---|---|---|---|
| Standard BOM | One assembly, five default columns, nothing else | No | No |
| Purchasing BOM | N assemblies, purchase quantities, prices, status | Reads only | No |
| Cart / list handoff | Put approved lines into a DigiKey list or Mouser cart | Writes, after explicit approval | Yes, one distributor account |

Out of scope: placing orders, checkout, payment, shipping or tax estimates,
automatic substitution of parts, splitting one BOM line across two
distributors, and any change to Altium documents.

## 2. What exists today

### 2.1 Reusable as is

| Capability | Where | Reuse |
|---|---|---|
| Altium purchasing BOM (fitted lines grouped by Manufacturer + Part Number, `unresolved`, `not_fitted`, `not_in_bom`, `inconsistencies`) | `export/purchasing_bom.py`, `tools/project.py` (`purchasing_bom_of_project`) | The only source of BOM lines. No second extraction. |
| Standard BOM CSV with ordered columns, DNP rules, formula neutralization, Excel warnings, count cross-checks | `export/purchasing_bom_csv.py`, `proj_export_purchasing_bom_csv` | Unchanged. Its helpers (`neutralize_formula`, `ENCODING`, `ColumnSelectionError`, `resolve_columns` pattern) are reused by the new export. |
| Export folder, repo guard, never-overwrite naming, exclusive creation, cleanup on failure | `export/destination.py` (`export_dir`, `export_file`, `claimed`, `validated_path`) | All new files go through it. |
| Exact-match distributor lookup, manufacturer comparison (`exact`, `partial`, `different`), `ambiguous` and `manufacturer_mismatch` handling, per-line `not_checked` when a distributor is down | `export/purchasing_sources.py` (`enrich_bom`, `lines_from_input`) | Unchanged contract. Extended only to keep two more lifecycle flags (section 2.3). |
| Offer normalization: stock, MOQ, multiple, standard package, price breaks, currency, reeling fee, lifecycle status, lead time | `libimport/providers/_sourcing.py`, `distributors.py` | Source of every `SupplierOffer` field. |
| SQLite lookup cache, 4 hour pricing TTL, `fresh()` bypass | `libimport/providers/_cache.py` | Unchanged. Cart preparation bypasses it. |
| Per-offer pricing, overbuy to a cheaper break, stock check, reel handling, distributor choice, currency refusal, totals | `export/purchasing_plan.py` (`price_offer`, `plan_purchases`) | Behavior is kept as the `cost_only` policy of the new engine (section 5.10). |
| Read-only tools | `tools/purchasing.py` (`part_purchasing_analyze`, `part_purchasing_plan`) | Unchanged arguments and defaults. |
| DigiKey MyLists client with input checks, batch add, read-back and compare | `libimport/providers/digikey_lists.py`, `part_list_*` tools | Primitive for the DigiKey handoff. |
| Mouser Cart client with input checks, MOQ and multiple check, read-back and compare, additive-insert detection | `libimport/providers/mouser_cart.py`, `part_cart_*` tools | Primitive for the Mouser handoff. |

### 2.2 Missing

| Gap | Consequence |
|---|---|
| No assembly-count configuration; `build_quantity` defaults to 1 | Cannot reproduce the baseline script's default of 2 without an argument every time. |
| No overage factor, no unit price limit for overage, no bounded paid upgrade | Quantities are only raised to MOQ, multiple, and to a cheaper price break. |
| Arithmetic is `float`, with a `1e-9` tolerance | Rounding is not exact (section 3). |
| A distributor with too little stock is dropped whole (`insufficient_stock`) and the line shows only `problems` | No proposed quantity and no structured reason for the review. |
| `unresolved` components are listed beside the plan, not as plan lines | The Purchasing BOM would silently lose them. |
| `_read_one` keeps only `lifecycle.status` and `lifecycle.raw` | The `discontinued` and `end_of_life` flags, DigiKey's last-buy date and Mouser's `SuggestedReplacement` text are read but dropped. |
| Lifecycle `None` means two things | "Not stated" and "unknown to our word table" are not told apart; Mouser sends null for parts in production. |
| One free-text `notes` list per choice | No stable, filterable review reason codes and no severity. |
| No purchasing configuration file | Nothing stores preferences. |
| No approval step, no idempotency record, no target-state cart logic | A retried Mouser insert adds again. |
| No CSV for the Purchasing BOM | Only the Standard BOM exports. |

### 2.3 Naming trap

`proj_export_purchasing_bom_csv` is, in effect, the **Standard BOM**: one
assembly, no distributor. It was named before the workflows were separated.
Renaming it would break existing users, so it stays and its docstring will
say "Standard BOM". The Purchasing BOM gets a new tool name (section 7).

## 3. The baseline script and its defects

The behavior to preserve is the user's earlier purchasing script
(`mouser_search2.py`, kept outside this repository). Its constants:

| Constant | Value | Meaning |
|---|---|---|
| assemblies | 2 | Boards to build |
| `scrapfactor` | 1.10 | Overage on quantity |
| `scrapfactor_max_price` | 5 | Overage applies only if unit price is at most this |
| `price_increase_factor` + `lin_increase` | 1.10, 5 | Spending allowance: baseline x 1.10 + 5 |
| paid break window | 2 | Up to two more price breaks (kept as `price_break_window`) |
| `max_qty_increase_factor` | 2 | Read as a quantity cap by the code. Not carried over: the intent was the two-break look-ahead, not a limit on quantity (sections 5.3 and 10) |

Defects to correct, each with a regression test:

| # | Defect in the script | Correct behavior |
|---|---|---|
| B1 | `int(qty * 1.1)` truncates: 5 needed gives 5, 3 gives 3. Overage is lost on small quantities. | `ceil` of an exact decimal product: 5 gives 6, 3 gives 4. |
| B2 | Float product: 100 x 1.1 is `110.00000000000001`; a naive `ceil` gives 111. 110 x 1.1 gives 122, not 121. | `Decimal` throughout: 100 gives 110, 110 gives 121. |
| B3 | The overage quantity is priced at the unit price of the original quantity, not of the overage quantity. | Price what is actually ordered. |
| B4 | A quantity below the first price break is priced at the first break and no MOQ is enforced. | Raise to MOQ and to the first break, then to the multiple. |
| B5 | Order multiples and standard package sizes are ignored. | Every candidate is rounded up to the multiple. |
| B6 | A quantity cap of 2 x is checked against the first step's quantity while the second step's quantity is what gets chosen, so the cap does its job inconsistently. | No quantity multiple limit exists. The look-ahead window and the allowance are the only bounds on a paid upgrade, applied the same way to every candidate. |
| B7 | The allowance test uses a baseline cost made with a truncated quantity (B1), so it is a little too tight and the result changes at the margin (TS-Q33). | The baseline is the exact overage quantity. |
| B8 | Stock is compared with the original quantity, not the chosen one, before adding to the cart. | A candidate above stock is infeasible. |
| B9 | Only two consecutive breaks are ever looked at, so a cheaper break three steps up is never found, even when it saves money. | Cost-reducing breaks are found anywhere on the ladder (section 5). |
| B10 | Part match is a substring test on the manufacturer part number. | Exact match as already implemented in `purchasing_sources.py`. No substitution. |
| B11 | A part not found keeps the row but gives it no reason code and no quantity. | Every line gets a status and a reason. |
| B12 | Retries and the cart insert are additive and not recorded. | Section 6. |

## 4. Shared data model

Minimal, frozen, in one module. Money and prices are `Decimal`; quantities
are `int`. No field holds a credential or a file path.

```python
class Severity(str, Enum):   BLOCKING = "blocking"; REVIEW = "review"; INFO = "info"
class PurchasingStatus(str, Enum):
    READY = "ready"            # orderable, nothing a person must look at
    REVIEW = "review"          # orderable, but a person should look first
    BLOCKED = "blocked"        # cannot be ordered as it stands
    NOT_FITTED = "not_fitted"  # only with include_dnp; nothing to buy

@dataclass(frozen=True)
class PriceBreak:      quantity: int; unit_price: Decimal

@dataclass(frozen=True)
class Lifecycle:
    status: str | None          # active, new, preliminary, nrnd, last_time_buy,
                                # end_of_life, obsolete, discontinued, or None
    raw: str | None             # the distributor's own text
    discontinued: bool | None   # the distributor's own flag
    end_of_life: bool | None
    last_buy_date: str | None

@dataclass(frozen=True)
class SupplierOffer:
    distributor: str            # "digikey" | "mouser"
    sku: str                    # the orderable number
    packaging: tuple[str, ...]
    currency: str | None
    stock: int | None           # None means not stated, never zero
    moq: int | None
    multiple: int | None
    standard_package: int | None
    max_order_qty: int | None   # None when the catalogue states none
    price_breaks: tuple[PriceBreak, ...]
    reeling_fee: Decimal | None
    lifecycle: Lifecycle
    manufacturer: str           # as the distributor states it
    manufacturer_match: str     # exact | partial
    retrieved_at: str
    cached: bool
    product_url: str | None

@dataclass(frozen=True)
class BomLine:
    line_id: str                # stable within a run: "L001"; unresolved: "U001"
    kind: str                   # "fitted" | "unresolved" | "not_fitted"
    manufacturer: str
    part_number: str
    description: str
    designators: tuple[str, ...]
    footprints: tuple[str, ...]
    bom_quantity: int           # per assembly; 1 for each unresolved component
    data_problems: tuple[str, ...]   # Altium's reason text, unchanged

@dataclass(frozen=True)
class QuantityPolicy:
    assemblies: int
    overage_factor: Decimal
    overage_max_unit_price: Decimal
    overage_currency: str
    extra_spend_percent: Decimal
    extra_spend_flat: Decimal
    extra_spend_currency: str
    paid_break_window: int      # whole number, 0 or more; 0 turns paid upgrades off
    large_surplus_ratio: Decimal | None   # None: LARGE_SURPLUS is not raised
    # No quantity multiplier exists. Quantity is never limited relative to
    # the requirement; only stock, maximum order quantity, MOQ, multiple and
    # packaging limit it.

@dataclass(frozen=True)
class Candidate:
    quantity: int; unit_price: Decimal; extended_cost: Decimal
    break_quantity: int
    kind: str       # "floor" | "overage" | "free_upgrade" | "paid_upgrade"
    verdict: str    # "chosen" | "accepted" | "rejected"
    why: str        # for example "above stock 800", "cost 750 exceeds allowance 797"

@dataclass(frozen=True)
class QuantityDecision:
    required_qty: int
    floor_qty: int              # required, raised to MOQ and multiple
    baseline_qty: int           # floor, raised by overage
    baseline_cost: Decimal | None
    allowance: Decimal | None
    purchase_qty: int
    unit_price: Decimal | None
    extended_cost: Decimal | None   # reeling fee included
    break_quantity: int | None
    surplus: int                # purchase_qty - required_qty
    surplus_ratio: Decimal      # purchase_qty / required_qty, exact
    rule: str                   # required | moq | multiple | overage |
                                # free_upgrade | paid_upgrade
    explanation: str            # one sentence: why this quantity, in numbers
    extra_spend: Decimal        # extended_cost - baseline_cost, may be negative
    shortfall: str | None       # None, or why the offer cannot supply the
                                # requirement: out_of_stock | insufficient_stock |
                                # purchase_limit | no_price | no_currency
    flags: tuple[str, ...]      # informational codes the review engine turns
                                # into reasons (MOQ_ADJUSTED, ...)
    candidates: tuple[Candidate, ...]
    limits_applied: tuple[str, ...]

@dataclass(frozen=True)
class ReviewReason:
    code: str                   # stable, upper case, from the catalogue in 4.1
    severity: Severity
    message: str                # one sentence, no markup
    source: str                 # bom | distributor | quantity | lifecycle | access

@dataclass(frozen=True)
class PurchaseLine:
    line: BomLine
    required_qty: int           # bom_quantity x assemblies
    offer: SupplierOffer | None
    decision: QuantityDecision | None
    status: PurchasingStatus
    reasons: tuple[ReviewReason, ...]
    alternatives: tuple[dict, ...]   # what the other distributor offered
    suggested_replacement: str | None   # text only, never ordered
    lifecycle: str              # the distributor's own words, for display
```

Status is derived, never set by hand: any `BLOCKING` reason gives
`BLOCKED`; otherwise any `REVIEW` reason gives `REVIEW`; otherwise `READY`.
`INFO` reasons never change the status.

Serialization: new blocks serialize `Decimal` as strings (`"0.1250"`).
Fields that existing tools already return as JSON numbers stay numbers
(section 9, D7).

### 4.1 Review reason catalogue

A part may carry several. Default severities are in the table; only the
rows marked "tunable" can be changed in `config.toml`. The rest are a safety
floor that no configuration can lower.

| Code | Default | Tunable | Raised when |
|---|---|---|---|
| `UNRESOLVED_COMPONENT` | blocking | no | Altium reports the component with no usable Manufacturer or Part Number; the reason text from Altium is kept |
| `MISSING_MANUFACTURER`, `MISSING_MPN` | blocking | no | Sub-codes of the above, when the text says which |
| `MPN_NOT_FOUND` | blocking | no | No distributor lists the exact manufacturer part number |
| `MANUFACTURER_MISMATCH` | blocking | no | The number exists only under another maker |
| `AMBIGUOUS_MANUFACTURER` | blocking | no | Two makers could each be the BOM's |
| `MANUFACTURER_PARTIAL_MATCH` | review | yes | Names agree only as a prefix ("Murata" and "Murata Electronics") |
| `BOM_CONFLICT` | review | yes | `inconsistencies` names the line: several descriptions, several footprints, maker spelled several ways, one number under several makers |
| `BOM_INCOMPLETE` | blocking | no | The Altium read was cut short; applies to every line |
| `DUPLICATE_SKU` | blocking | no | Two BOM lines resolve to one distributor number; the cart takes one line per number and merging would change the price break |
| `OUT_OF_STOCK` | blocking | no | Stock is stated as 0 at every distributor that matched the part |
| `INSUFFICIENT_STOCK` | blocking | no | Stock is stated and below the required order quantity at every distributor that matched the part |
| `STOCK_NOT_STATED` | review | yes | The distributor gives no stock for the chosen offer |
| `OVERAGE_LIMITED_BY_STOCK` | info | yes | Overage was reduced to what stock allows |
| `OVERAGE_SKIPPED_CURRENCY` | info | yes | Overage was not applied because the offer is not in the overage currency |
| `ALLOWANCE_FLAT_SKIPPED` | info | yes | The fixed allowance was left out because the offer is not in the allowance currency |
| `FALLBACK_DISTRIBUTOR` | info | yes | The preferred distributor could not supply the line and the other was used |
| `MISSING_PRICE` | blocking | no | No usable price break, or a price of zero or less |
| `MISSING_CURRENCY` | blocking | no | A price with no currency |
| `CURRENCY_MIXED` | review | yes | The two distributors price in different currencies, so no cross comparison was made |
| `MOQ_ADJUSTED` | info | yes | Quantity raised to the minimum |
| `MULTIPLE_ADJUSTED` | info | yes | Quantity raised to the order multiple |
| `MOQ_NOT_STATED`, `MULTIPLE_NOT_STATED` | review | yes | A value was assumed (first break; reel as multiple) |
| `PACKAGING_REEL_WHOLE` | info | yes | The chosen packaging is a full reel sold whole (a fact about the offer, not a reaction to surplus) |
| `PURCHASE_LIMIT_EXCEEDED` | blocking | no | The required quantity exceeds the distributor's stated maximum order quantity |
| `LARGE_SURPLUS` | info | info or review only, never blocking | `purchase_qty / required_qty` is above `large_surplus_ratio`. Not raised when the ratio is unset, which is the default (section 5.8) |
| `SETUP_QUANTITY_RAISED` | info | yes | Future (section 6.7): the quantity was raised to a feeder or setup minimum |
| `LIFECYCLE_NRND` | review | yes | Not recommended for new designs |
| `LIFECYCLE_LAST_TIME_BUY` | review | yes | Last time buy |
| `LIFECYCLE_EOL` | blocking | no | End of life, from the status text or the distributor's end-of-life flag |
| `LIFECYCLE_OBSOLETE` | blocking | no | Obsolete |
| `DISTRIBUTOR_DISCONTINUED` | blocking | no | The distributor's own discontinued flag or status says discontinued |
| `LIFECYCLE_UNKNOWN` | review | yes | No distributor states a status, or the text is not one this project knows |
| `LIFECYCLE_NOT_STATED` | info | yes | One distributor sends none and no other says active (Mouser sends null for parts in production; beside an active DigiKey it is not reported) |
| `LIFECYCLE_PRELIMINARY` | info | yes | Preliminary or new product |
| `DISTRIBUTOR_UNAVAILABLE` | info | yes | One distributor could not answer (no credential, unreachable, rate limited); the other did |
| `NO_DISTRIBUTOR_DATA` | blocking | no | No distributor answered for this line |
| `STALE_PRICING` | info | yes | The figures were reused from the cache and are older than the configured age |

## 5. Quantity optimization

### 5.1 Terms

| Term | Definition |
|---|---|
| Required quantity `R` | BOM quantity per assembly x assembly count. Never reduced. |
| MOQ `m`, multiple `k` | As the offer states them. An unstated MOQ is the first price break; an unstated multiple is 1, or the standard package for a single-packaging reel offer. These assumptions are reported. |
| `round_up(x)` | `ceil(max(x, m) / k) * k` |
| Ladder | The offer's price breaks, ascending, usable entries only |
| `price(Q)` | Unit price of the largest break not above `Q` |
| `cost(Q)` | `Q * price(Q)` plus the reeling fee, if any |
| Floor `Q0` | `round_up(R)` |
| Baseline `Q1` | `Q0`, or `round_up(ceil(R * overage_factor))` when overage applies |
| Baseline cost `B` | `cost(Q1)` |
| `p` | `extra_spend_percent`, a decimal, 0 or more |
| `f` | `extra_spend_flat`, a decimal, 0 or more, in `extra_spend_currency` |
| `w` | `price_break_window`, a whole number, 0 or more |
| Allowance `A` | `B * (1 + p / 100) + f`, the highest cost a paid upgrade may have |

All arithmetic is `Decimal`. A float is converted through its shortest
`repr`, never through `Decimal(float)`. Intermediate values are exact.
Rounding is applied only for display: line costs to 4 decimal places, totals
once, to 2 decimal places, `ROUND_HALF_UP`.

### 5.2 The spending allowance

`p`, `f` and `w` are three independent settings. Each is read from
`config.toml` (`upgrade.extra_spend_percent`, `upgrade.extra_spend_flat`,
`upgrade.price_break_window`) and can be overridden for one call by a tool
argument of the same name. Changing one never changes another.

- `A = B * (1 + p / 100) + f`. With the defaults (10 and 5.00), `A = B * 1.10 + 5.00`.
- Zero is valid for each. `p = 0` and `f = 0` give `A = B`: only an upgrade
  whose cost equals the baseline cost is accepted, and nothing dearer.
  `w = 0` turns paid upgrades off; cost-saving upgrades are unaffected.
- Negative values, non-numbers and booleans are refused before any Altium
  or distributor call. `w` must be a whole number.
- `f` applies only to an offer priced in `extra_spend_currency`. For any
  other currency the flat term is zero, the percentage term still applies,
  and the line says so.
- The allowance is per line. There is no budget for the whole order.
- Unit price plays no part. The overage price limit decides only whether
  overage applies (step 2); it never decides whether an upgrade is allowed.

### 5.3 Algorithm (per offer)

1. **Floor.** `Q0 = round_up(R)`. If `Q0` is above stated stock or the
   maximum order quantity, the offer is short: stop and report the shortfall
   with `purchase_qty = Q0`. The line is not blocked yet: the other
   distributor is tried (5.5.1).
2. **Overage.** If `overage_factor > 1`, the offer currency equals
   `overage_currency`, and `price(Q0) <= overage_max_unit_price`, then
   `Q1 = round_up(ceil(R * overage_factor))`. Otherwise `Q1 = Q0`. If `Q1`
   exceeds stock or the maximum order quantity, lower it to the largest
   valid quantity not below `Q0` and raise `OVERAGE_LIMITED_BY_STOCK`.
3. **Baseline.** `B = cost(Q1)` and `A` as in 5.2.
4. **Candidates.** Every price break above the one in force at `Q1` gives a
   candidate `round_up(break quantity)`, deduplicated. A candidate that
   breaks a hard limit (step 5) is rejected and recorded, and does not count
   as an opportunity. The feasible, distinct candidates are numbered 1, 2, 3
   and so on upward. Within one break, cost only rises with quantity, so
   break boundaries are the only quantities worth testing.
5. **Hard limits** apply to every candidate, free or paid: stated stock,
   stated maximum order quantity, MOQ, order multiple, standard package and
   the offer's packaging rules (a reel sold whole, a reeling fee). A candidate
   that breaks one is rejected, with the reason recorded.
6. **Free upgrades.** Any candidate with `cost(Q) < B` (strictly lower), at
   any break number, at any unit price, however large the quantity increase.
   The window does not apply. Among free upgrades the lowest cost wins; a tie
   goes to the smaller quantity.
7. **Paid upgrades.** Feasible candidates numbered 1 to `w` with
   `B <= cost(Q) <= A` (both inclusive), at any unit price, however large
   the quantity increase (10 times the requirement or more is allowed when
   the cost fits). Among them the furthest break, which is the largest
   quantity, wins, matching the original script.
8. **Selection.** If any free upgrade exists, take it, even when a paid
   upgrade sits at a further break. Otherwise take the furthest qualifying
   paid upgrade. Otherwise `Q1`.
9. **Record.** Every candidate, accepted or rejected, is stored with its
   reason in `QuantityDecision.candidates`, and the decision carries a
   one sentence `explanation` with the numbers (for example "1000 at the
   1000 break costs 11.00, within the allowance of 11.05 over the baseline
   of 5.50").

### 5.4 Never below the requirement

No path lowers a quantity under `Q0`. Stock, a maximum order quantity or
a missing price may make a line blocked; they never make it smaller. A
blocked line still shows the quantity that would be needed.

### 5.5 Interaction with limits

| Limit | Floor | Overage | Free upgrade | Paid upgrade |
|---|---|---|---|---|
| Stated stock | blocks the line | lowers the overage | rejects the candidate | rejects the candidate |
| Maximum order quantity | blocks the line | lowers the overage | rejects the candidate | rejects the candidate |
| MOQ, multiple, standard package | raises | raises | raises | raises |
| Packaging rules and reeling fee | priced in | priced in | priced in | priced in |
| Spending allowance `A` | no | no | no (already cheaper) | yes |
| Look-ahead window `w` | no | no | no (whole ladder) | yes |
| Unit price limit for overage | no | yes | no | no |
| Multiple of the requirement | no limit | no limit | no limit | no limit |

In words: hard supply limits (stock, maximum order quantity, MOQ, multiple,
packaging) are never relaxed. Beyond them, quantity is not limited. What
bounds a paid upgrade is its cost (`A`) and how far it looks (`w`). What
bounds a free upgrade is that it must cost less. A 10 times surplus of a
cheap passive is accepted when the cost is within `A`, and a 4 times surplus
of an expensive part likewise.

### 5.5.1 Stock-limited quantities

1. If one distributor's stock cannot cover the requirement (`Q0`), the other
   distributor is checked. If neither can supply `Q0`, the line is blocked
   (`OUT_OF_STOCK` when every matched distributor states stock 0, otherwise
   `INSUFFICIENT_STOCK`) and kept for manual review.
2. A blocked line never gets a partial purchase: no smaller quantity, no
   split between distributors. It shows the quantity it would need and is
   never cart eligible.
3. If stock covers `Q0` but not the full overage, the overage is reduced to
   the largest quantity that stock allows and that is a multiple of the order
   multiple, never below `Q0` (`OVERAGE_LIMITED_BY_STOCK`).
4. If stock prevents reaching a higher price break, that break is rejected
   and the best feasible quantity among the others is taken.
5. Stock and the maximum order quantity act as one limit, `min` of the two.
   An unstated limit is no limit; an unstated stock is reported
   (`STOCK_NOT_STATED`).

### 5.6 Worked examples (all synthetic, verified with exact arithmetic)

Defaults unless stated: overage 1.10 up to a 5.00 unit price, `p` = 10,
`f` = 5.00, `w` = 2.

| Case | Ladder (qty: unit price) | R | Result | Why |
|---|---|---|---|---|
| Float trap | 1: 0.10 | 100 | 110, not 111 | `Decimal`; float 100 x 1.1 is 110.00000000000001 |
| Truncation | 1: 0.10 | 5 | 6 | script gave 5 (B1) |
| Expensive part, no overage | 1: 6.25 | 4 | 4 | price above 5, no overage |
| MOQ | 1: 0.50, MOQ 10 | 4 | 10 | overage 5 raised to MOQ |
| Multiple | 1: 0.20, multiple 5 | 11 | 15 | ceil(12.1) = 13, raised to 15 |
| Free, small | 1: 1.00, 100: 0.40 | 60 | 100 | B = 66 x 1.00 = 66; 100 x 0.40 = 40 |
| Free, expensive | 1: 12.00, 250: 2.00 | 60 | 250 | B = 720; 250 x 2.00 = 500, lower, so free; 4.2 times the need |
| Paid, expensive | 1: 12.00, 250: 3.00 | 60 | 250 | B = 720, A = 797; 250 x 3.00 = 750 is within A |
| Paid, cheap passive, 10 times or more | 1: 0.10, 1000: 0.011 | 50 | 1000 | Q1 = 55, B = 5.50, A = 11.05; 1000 x 0.011 = 11.00; 20 times R, 18.2 times Q1 |
| Paid, modest | 1: 2.00, 100: 1.10 | 45 | 100 | Q1 = ceil(49.5) = 50, B = 100, A = 115; cost 110 |
| Furthest paid wins | 1: 2.00, 60: 1.80, 100: 1.12 | 45 | 100 | costs 108 and 112, both within 115 |
| Free beats a further paid | 1: 2.00, 60: 1.50, 200: 0.55 | 45 | 60 | 60 costs 90, below B = 100, so free; 200 costs 110 and would qualify as paid, but free wins |
| Free beyond the window | 1: 2.00, 100: 1.10, 250: 0.60, 1000: 0.08 | 45 | 1000 | break 3 is outside `w` = 2, but 1000 x 0.08 = 80 is below B = 100 |
| Window limits paid | 1: 2.00, 100: 1.10, 250: 0.60, 1000: 0.11 | 45 | 100 | break 1 (100) costs 110 and fits; break 2 costs 150 and fails; break 3 costs 110 and fits but is outside `w` = 2, so 100 is the furthest it may reach |
| Window 3 | same ladder, `w` = 3 | 45 | 1000 | break 3 is now looked at and is the furthest |
| Effect of exact baseline (B7) | 1: 2.00, 60: 1.80, 130: 0.87 | 45 | 130 | cost 113.10; the script's truncated baseline gave A = 112.80 and refused it; exact A = 115 accepts |
| Exact boundary, accepted | 1: 1.00, 100: 0.534 | 40 | 100 | Q1 = 44, B = 44.00, A = 53.40; cost is exactly 53.400 |
| Exact boundary, refused | 1: 1.00, 100: 0.5341 | 40 | 44 | cost 53.41 is one cent over A |

### 5.7 Allowance parameter examples

Ladder 1: 2.00, 100: 1.12; R = 50; overage off (`overage_factor` = 1.00),
so `Q1` = 50 and `B` = 100.00. The upgrade to 100 costs 112.00.

| `p` | `f` | `A` | Result | Why |
|---|---|---|---|---|
| 0 | 0 | 100.00 | 50 | zero allowance: only an equal cost passes |
| 10 | 5.00 | 115.00 | 100 | defaults |
| 25 | 0 | 125.00 | 100 | percentage only |
| 0 | 20.00 | 120.00 | 100 | fixed amount only |
| 5 | 2.50 | 107.50 | 50 | 112.00 is over A |
| 0 | 12.00 | 112.00 | 100 | cost equals A exactly |
| 0 | 11.99 | 111.99 | 50 | one cent under the cost |

With `p` = 0 and `f` = 0, ladder 1: 2.00, 100: 1.00 (cost 100.00 = B) gives
100: equal cost passes with no extra spend. At 100: 1.01 (cost 101.00) it
stays at 50.

### 5.8 Surplus is reported, not policed

A line whose purchase quantity is above the requirement is not a problem by
that fact. Extra passive components are often wanted: a pick-and-place
machine loses parts when a feeder is loaded and set up, and a spare reel
position costs little. So:

- Every purchase row reports `surplus`, `surplus_ratio`, `rule` and the
  `explanation`, whatever the size of the surplus (Surplus and Quantity Rule
  columns in the CSV).
- A large surplus alone never blocks a line, never lowers its status and never
  makes it need manual approval. A cheap passive at 10 times or more of the
  requirement stays `ready` if nothing else is wrong with it.
- An optional warning is configurable: `review.large_surplus_ratio`
  (`large_surplus_ratio` as an argument). When set and exceeded
  (`purchase_qty / required_qty` strictly above it), the line gets
  `LARGE_SURPLUS`, information only. A user may raise its severity to
  `review` in `[review.severity]`, and may never set it to `blocking`.
  Unset by default.

### 5.9 Choosing among offers and distributors

Reuse the existing structure: each offer is decided independently
(different packaging is a different purchase); the in-stock offers are
ranked by `extended_cost`, then `purchase_qty`, then `sku`. A line goes whole
to one distributor, never split.

`prefer` is `digikey` by default in the configured workflow. DigiKey gets
the line whenever it can supply the requirement and give an acceptable offer
(matched, priced, in stock for `Q0`); otherwise Mouser is used and the line
carries `FALLBACK_DISTRIBUTOR` with DigiKey's reason. `mouser` is the same
with the roles swapped. `lowest` picks the lowest final extended cost among
the distributors that can supply the line; it needs one currency, and with
two currencies the preference order (DigiKey, then Mouser) decides and the
line carries `CURRENCY_MIXED`. The existing `part_purchasing_plan` keeps its
own default of `lowest`.

### 5.10 Relationship to the existing planner

`price_offer` today corresponds to steps 1, 4, 5, 6 and 8 with no overage
and no paid window, with `float` arithmetic. It already takes any cheaper
break anywhere on the ladder, so the free upgrade rule is existing behavior;
the overage, the paid window and the allowance are what is new. The new
engine takes a `QuantityPolicy`. `QuantityPolicy.cost_only()` (overage 1, paid window 0)
must reproduce every assertion in `tests/test_purchasing_workflow.py` that
concerns `price_offer`, apart from the documented float fixes. The existing
tools keep that policy by default; the Purchasing BOM uses the configured
one.

## 6. Functional requirements and acceptance criteria

Each requirement has an identifier. Test scenarios (TS) are defined in
section 8.

### 6.1 Standard BOM

| ID | Requirement | Acceptance |
|---|---|---|
| STD-1 | Always one assembly. The tool has no assembly argument and quantities are per assembly. | TS-S01: no signature parameter named like `assemblies` or `build_quantity`. |
| STD-2 | Default columns, in order: Quantity, Part Number, Description, Manufacturer, Reference Designator. | TS-S02: header row equals the five names in order. |
| STD-3 | No distributor call, price, stock, lifecycle or optimization. | TS-S03: with the network and `enrich_bom` patched to raise, export succeeds. |
| STD-4 | Existing behavior and column selection are unchanged. | TS-S04: a characterization test, written first, pins the current CSV bytes for a synthetic BOM; it passes before and after every milestone. |
| STD-5 | The new export cannot change the Standard BOM columns, even by sharing helpers. | TS-S05: importing the purchasing export does not alter `purchasing_bom_csv.COLUMNS`. |

### 6.2 Purchasing BOM

| ID | Requirement | Acceptance |
|---|---|---|
| PUR-1 | Assembly count is configurable: argument, else `config.toml`, else 2. It must be a whole number, 1 or more. | TS-P01, TS-P02 |
| PUR-2 | Required Qty = BOM Qty x assemblies, exactly. | TS-P03 |
| PUR-3 | Purchase Qty comes from the quantity engine (section 5). It is never below Required Qty. | TS-Q01 to TS-Q38 |
| PUR-4 | Columns available: BOM Qty, Required Qty, Purchase Qty, Surplus, Part Number, Manufacturer, Description, Reference Designator, Footprint, Distributor, Distributor SKU, Packaging, Unit Price, Extended Cost, Currency, Stock, Lifecycle, Purchasing Status, Review Reasons, Quantity Rule, Price Break, Data Retrieved. | TS-P04 |
| PUR-5 | Column selection and order follow the Standard BOM's rules: exact identifiers, refused on unknown, duplicate, empty or non-list, validated before any Altium or distributor call. | TS-P05 |
| PUR-6 | Every legitimate fitted line is a row: row count equals fitted line count plus unresolved components, and the BOM Qty column sums to the fitted component count. | TS-P06 |
| PUR-7 | Unresolved components are rows, one per component, status `blocked`, reason `UNRESOLVED_COMPONENT` carrying Altium's text, Required Qty = assemblies. They are never dropped and never guessed. | TS-P07 |
| PUR-8 | A blocked or reviewed line is exported with its reasons; nothing is hidden because it cannot be bought. | TS-P08 |
| PUR-9 | Review Reasons holds every reason code, ordered by severity then code, separated by a fixed delimiter, with the message in a second optional column. | TS-P09 |
| PUR-10 | Every text cell, including distributor-supplied text (descriptions, reasons, manufacturer names), is neutralized for spreadsheet formulas and checked for Excel reinterpretation, as the Standard BOM does. | TS-P10 |
| PUR-11 | The export makes no cart or list call and uses no write endpoint. | TS-P11: recorded requests contain no cart, list or order URL; the module source names none. |
| PUR-12 | The reply carries counts, totals per currency, warnings and the path, never the BOM. A total is never added across currencies. | TS-P12 |
| PUR-13 | An incomplete Altium read is refused unless `allow_incomplete`, and every row then carries `BOM_INCOMPLETE`. | TS-P13 |
| PUR-14 | DNP parts are excluded; with `include_dnp` they are rows with Required Qty 0, status `not_fitted`, and are not looked up. | TS-P14 |
| PUR-15 | The file goes through `export_file`: export folder, repo guard, never overwritten, timestamp collision names. | TS-P15 |
| PUR-16 | Surplus and Quantity Rule are populated on every purchase row, and a large surplus does not change a row's status. | TS-P16 |
| PUR-18 | The default preference is DigiKey with Mouser as the fallback; `lowest` stays selectable. | TS-P18 |
| PUR-17 | The tool takes the quantity and allowance settings as arguments named as in CFG-10, each overriding the file independently. | TS-P17 |

### 6.3 Quantity optimization

| ID | Requirement | Acceptance |
|---|---|---|
| QTY-1 | Defaults: assemblies 2, overage factor 1.10, overage price limit 5.00 USD, `extra_spend_percent` 10, `extra_spend_flat` 5.00, `extra_spend_currency` USD, `price_break_window` 2. There is no quantity multiplier. | TS-Q01 |
| QTY-2 | Decimal arithmetic end to end; ceil on exact products. | TS-Q02 to TS-Q04, TS-Q32, TS-Q33 |
| QTY-3 | Actual price breaks, MOQ, multiple, stock and maximum order quantity are used; nothing is estimated. | TS-Q05 to TS-Q11 |
| QTY-4 | Never below Required Qty, in any branch. | TS-Q12 |
| QTY-5 | A feasible quantity with strictly lower cost is accepted from any break on the whole ladder, at any unit price and any size of increase, beyond the paid window too. | TS-Q13 to TS-Q15 |
| QTY-6 | A paid upgrade is allowed at any unit price, expensive or inexpensive, at the first `price_break_window` feasible, distinct breaks above the one in force (a break stock or a maximum order quantity rules out, or one that rounds to a quantity already listed, uses no position), when its cost is at least the baseline cost and at most the allowance. No limit applies to how much the quantity grows. | TS-Q16 to TS-Q18, TS-Q20 |
| QTY-7 | A cost-saving upgrade is preferred to a paid one, even when the paid one is at a further break. Among paid upgrades the furthest break wins. | TS-Q18, TS-Q19 |
| QTY-8 | The allowance is `B * (1 + extra_spend_percent / 100) + extra_spend_flat`. The percentage, the fixed amount and the window are independent settings; each comes from `config.toml` and is overridden by a tool argument of the same name. Zero is valid for each. | TS-Q24 to TS-Q28, TS-Q30 |
| QTY-9 | Boundaries are inclusive: cost equal to the allowance is accepted and one cent over is not. A cost equal to the baseline is accepted as a paid upgrade at zero extra spend. | TS-Q24, TS-Q26, TS-Q27 |
| QTY-10 | The fixed amount applies only to offers in `extra_spend_currency`; the percentage applies in any currency. The line says when the fixed amount was left out. | TS-Q29 |
| QTY-11 | Invalid settings (negative, non-numeric, boolean, a window that is not a whole number) are refused before any Altium or distributor call. | TS-Q31 |
| QTY-12 | Stock, MOQ, order multiple, packaging and maximum order quantity are preserved for every candidate, free or paid. | TS-Q10, TS-Q11, TS-Q21 to TS-Q23 |
| QTY-13 | Surplus and the reason for the quantity are always reported and never block a line or require approval. | TS-Q37, TS-Q38 |
| QTY-14 | The decision is deterministic: the same input gives the same output, and ties are broken by cost, then quantity, then SKU. | TS-Q34 |
| QTY-15 | Every candidate considered, and why it was refused, is in the decision record, with a one sentence explanation. | TS-Q35 |
| QTY-16 | The cost-only policy reproduces the existing planner's results. | TS-Q36 |
| QTY-17 | Stock-limited quantities follow 5.5.1: the other distributor is tried, an overage shrinks to the largest valid quantity, an unreachable break is skipped, a blocked line is kept with no partial purchase. | TS-Q39 to TS-Q42 |

### 6.4 Manual review

| ID | Requirement | Acceptance |
|---|---|---|
| REV-1 | Every sourcing problem is a reason on a Purchasing BOM row. | TS-R01 to TS-R16, one per catalogue code |
| REV-2 | A line may carry several reasons. | TS-R17 |
| REV-3 | Status is derived from the reasons by the rule in section 4. | TS-R18 |
| REV-4 | No automatic substitution. A suggested replacement is shown as text, never ordered. | TS-R19 |
| REV-5 | Blocked and unresolved lines are never cart eligible; review lines only by explicit per-line choice. | TS-R20, TS-C04 |
| REV-6 | Lifecycle: the four states named in the request, the distributor discontinued flag, and unknown are each recognized; Mouser null with DigiKey active is not "unknown". | TS-R09 to TS-R14 |
| REV-7 | Overrides in `config.toml` may move only the rows marked tunable in 4.1. A blocking safety floor cannot be lowered. | TS-F07 |
| REV-9 | EOL, obsolete and distributor discontinued are blocking and no configuration, argument or severity override can lower them, so such a line is never cart eligible. | TS-F15, TS-R22 |
| REV-8 | A large surplus alone never blocks a line, never makes it need manual approval and never makes it ineligible for a cart. `LARGE_SURPLUS` is information (or review if the user raises it), never blocking, and only when `large_surplus_ratio` is set. | TS-Q37, TS-Q38, TS-R21 |

### 6.5 Configuration

| ID | Requirement | Acceptance |
|---|---|---|
| CFG-1 | `config.toml` is created in the folder `export_dir()` returns when missing, on first use of a purchasing tool, by exclusive creation. It is never overwritten or rewritten by the tool. | TS-F01, TS-F02 |
| CFG-2 | It follows `EDA_AGENT_EXPORT_DIR`, including its repo guard. | TS-F03 |
| CFG-3 | Precedence, highest first: explicit tool argument, `config.toml`, built-in default, decided for each setting on its own. Existing environment variables (distributor credentials, `EDA_AGENT_EXPORT_DIR`, `EDA_AGENT_DISTRIBUTOR_CACHE`, `DIGIKEY_TOKEN_FILE`) behave as they do now and are not mirrored into the file. | TS-F04, TS-F12 |
| CFG-4 | The file holds preferences only. Unknown keys are kept and warned about; keys that look like credentials (`key`, `secret`, `token`, `password`) are refused and warned about; no part numbers, designators or prices are accepted. | TS-F05, TS-F06 |
| CFG-5 | A broken file never stops a tool: the default is used for each bad value, and the reply lists the file, the key and the reason. | TS-F08 |
| CFG-6 | A read-only or invalid export folder does not stop a read tool; defaults are used and the warning says why. | TS-F09 |
| CFG-7 | Every reply that depends on a setting reports its effective value and where it came from (`argument`, `config`, `default`). | TS-F10 |
| CFG-8 | Numbers are read through `Decimal(str(value))`, so `1.1` is exactly 1.1. Strings and numbers are both accepted. | TS-F11 |
| CFG-9 | A committed file never contains a machine path. The template is built in code. | covered by `tests/test_no_client_design_data.py` |
| CFG-10 | Settings that may be overridden per call have the argument name of their key: `assemblies`, `overage_factor`, `overage_max_unit_price`, `overage_currency`, `extra_spend_percent`, `extra_spend_flat`, `extra_spend_currency`, `price_break_window`, `large_surplus_ratio`. Each is validated before any Altium or distributor call. The existing `part_purchasing_plan` and `part_purchasing_analyze` keep their arguments and defaults; the configured workflow is reached through `part_purchasing_export_csv` and `part_purchasing_cart_prepare`. | TS-F12, TS-P17 |
| CFG-11 | The allowance keys keep their names: `upgrade.extra_spend_percent`, `upgrade.extra_spend_flat`, `upgrade.extra_spend_currency`, `upgrade.price_break_window`. A removed key from the first draft (`max_quantity_multiplier`, `free_upgrade_max_multiplier`) is treated as unknown: kept, warned about, ignored. | TS-F13 |

Proposed template (written once; comments only; values are the defaults):

```toml
# EDA Agent purchasing preferences. Created automatically and never
# rewritten by the tool: edit it freely.
# Do not put API keys, passwords or design data here. Credentials stay in
# environment variables.
# Every value below can be overridden for one call by a tool argument of
# the same name as its key.
schema_version = 1

[assemblies]
count = 2

[overage]                      # for inexpensive parts only
factor = "1.10"
max_unit_price = "5.00"        # overage applies at or below this unit price
currency = "USD"

[upgrade]
# A price-break upgrade that costs more than the baseline is allowed when its
# cost is at most:   baseline_cost * (1 + extra_spend_percent / 100) + extra_spend_flat
# The three values are independent. Zero is allowed. Unit price and the
# size of the quantity increase do not matter, only this cost and the
# look-ahead. An upgrade that costs LESS is always taken, anywhere on the
# price ladder.
extra_spend_percent = "10"
extra_spend_flat = "5.00"
extra_spend_currency = "USD"   # the flat amount applies to offers in this currency
price_break_window = 2         # how many breaks above the current one a paid upgrade may reach; 0 turns paid upgrades off

[distributors]
prefer = "digikey"             # digikey or mouser (the other is the fallback), or lowest
enabled = ["digikey", "mouser"]

[review]
stale_pricing_hours = 4
# large_surplus_ratio = "10"   # unset: no warning. Extra parts are reported in
                               # every row either way; this only adds a note
                               # when purchase_qty / required_qty is above it.

[review.severity]              # only codes marked tunable may be changed
# LIFECYCLE_NRND = "review"
# LARGE_SURPLUS = "review"     # "info" or "review"; never "blocking"

# [setup]                      # FUTURE (section 6.7). Not read yet.
# min_quantity = { R = 100, C = 100 }   # per designator prefix, for feeder setup

[export]
purchasing_columns = ["required_qty", "purchase_qty", "surplus",
                      "part_number", "manufacturer", "description",
                      "reference_designators", "distributor",
                      "distributor_sku", "unit_price", "extended_cost",
                      "stock", "lifecycle", "purchasing_status",
                      "review_reasons", "quantity_rule"]
```

### 6.6 Cart and list handoff

| ID | Requirement | Acceptance |
|---|---|---|
| CART-1 | Reuse the existing clients (`MouserCart`, `DigiKeyLists`) and their input checks and read-back. No second HTTP path. | TS-C01: the new module imports them; a patched transport shows the same requests as `part_cart_add` and `part_list_add`. |
| CART-2 | Two steps. `prepare` is read-only and produces a proposal with a `proposal_id`: a hash of distributor, target, and for each line its SKU, quantity, packaging, unit price, currency, lifecycle status and designators. A change in any of them gives a different id. `commit` writes. | TS-C02, TS-C03 |
| CART-3 | `commit` needs the target (existing Mouser `cart_key`, existing DigiKey `list_id`, or an explicit `create_new`), `approved=True`, and the `proposal_id`. It re-reads the distributors fresh; if the recomputed proposal differs, nothing is written and the new proposal is returned. | TS-C03 |
| CART-4 | Eligible lines: status `ready`. Review lines only when listed in `include_lines`. Blocked, unresolved and `not_fitted` lines never. Each excluded line is returned with its reasons. | TS-C04 |
| CART-5 | Mouser inserts are additive. Commit works to a **target total** per SKU: it reads the cart, adds only the missing amount, and never lowers or removes. | TS-C05 to TS-C07 |
| CART-6 | Before any write the plan is stored in a journal. A retry with the same `proposal_id` reads back first and computes the delta from the journal's target, so a retry adds nothing it already added. | TS-C08, TS-C09 |
| CART-7 | If a cart creation was started and no key was recorded, a retry refuses to create another cart and asks for the key. | TS-C10 |
| CART-8 | A readback that is empty or lower than what the journal says was added is treated as unknown state, not as proof of failure, and is not retried automatically (the documented browser interoperability limitation). | TS-C11 |
| CART-9 | A cart holding more than the target is reported and left alone. | TS-C12 |
| CART-10 | After writing, every line is read back and compared by SKU, quantity and packaging; the reply says `verified` per line and overall. A mismatch is reported as such. | TS-C13 |
| CART-11 | DigiKey lists: a line that already exists with the same SKU, quantity and designators is skipped as satisfied; a line with the same SKU and a different quantity is reported and not changed (no update call exists). | TS-C14, TS-C15 |
| CART-12 | MOQ and multiple are checked against the target total, not against the delta. | TS-C16 |
| CART-13 | Designators are kept in the DigiKey reference fields and in the journal; Mouser has no field for them and the reply says so (existing behavior). | TS-C17 |
| CART-14 | The commit tool is classified as writing (`SILENT`, like `part_cart_add`); `prepare` stays read-only. Neither can check out, pay or order. | TS-C18 |
| CART-15 | The journal and any plan files live under the export folder, never in the repository, and contain no credentials. | TS-C19 |

### 6.7 Future requirement: feeder and setup quantities

Status: not part of milestones M1 to M5. Recorded so the design leaves room.

Rationale: extra passive components can be desirable for pick-and-place
feeder setup. Loading a feeder and setting up a machine consumes parts
(leader tape, calibration, pickup retries), and a build can need more than
the requirement even when no price break pays for it.

| ID | Requirement | Acceptance |
|---|---|---|
| FUT-1 | An optional minimum setup quantity per part class, independent of price-break optimization. It is off unless configured. | FT-1 |
| FUT-2 | It is a floor on the quantity: `Q0 = round_up(max(R, min_setup_quantity))` for a line it applies to, before overage and upgrades, so the optimization starts from it. | FT-2 |
| FUT-3 | A line raised by it carries `SETUP_QUANTITY_RAISED` (information) and its rule is `setup`. It never blocks and never needs approval. | FT-3 |
| FUT-4 | It does not use the spending allowance and does not count as extra spend: it is part of the baseline. | FT-4 |
| FUT-5 | Classes are matched by a rule the user writes in `config.toml` (for example designator prefix), never guessed from a description. A line matching no rule is untouched. | FT-5 |
| FUT-6 | A minimum above stock or the maximum order quantity makes the line blocked, as any requirement does. | FT-6 |

Open points are listed in section 10 (A8). No code, key or column is added
for this until they are settled; the commented `[setup]` block in the
template only marks the place.

## 7. Milestones

Five milestones. Each has a pure core, uses only fakes (the existing
`_Shop`, `_Wire` and `_Altium` test doubles), needs no live API, and ends
with tests that pass on their own. The order is a dependency order, except
that M4 and M5 are independent of each other.

### M1: Model, configuration and a safety net

Scope: the data model (section 4), `config.toml` loading and creation,
and characterization tests that pin today's behavior before anything moves.

Deliverables:
- `purchasing_model.py`, `purchasing_config.py`.
- Characterization tests for the Standard BOM CSV and for `part_purchasing_plan` output.
- A `settings` block (effective value and source) as a reusable helper.

Tests: STD-4, STD-5, CFG-1 to CFG-11, REV-7, status derivation, Decimal
serialization, independent allowance settings.

Exit: all existing purchasing tests pass unchanged; new tests pass; no
tool signature changed.

### M2: Quantity engine

Scope: section 5 as pure functions over `SupplierOffer` and
`QuantityPolicy`. No I/O.

Deliverables: `purchasing_quantity.py` with `decide_quantity(offer, required,
policy) -> QuantityDecision` and `QuantityPolicy.cost_only()`.

Tests: QTY-1 to QTY-16, every row of 5.6 and 5.7, the allowance matrix
(different percentages, fixed amounts, zero allowances, exact boundaries),
property tests (never below required, deterministic, exact multiples, a
paid upgrade never above the allowance, inclusive boundaries, free
upgrades always found, no limit on quantity growth), and the equivalence
suite against the existing `price_offer` assertions.

Exit: equivalence suite green; no other module imports the engine yet.

### M3: Review engine and plan integration

Scope: reason catalogue and status derivation; carry the lifecycle flags
through `enrich_bom`; turn the analysis into `PurchaseLine`s including
unresolved components; choose offers and distributors with the engine.

Deliverables:
- `purchasing_review.py`.
- `purchasing_sources.py`: keep `discontinued`, `end_of_life`, last buy date, suggested replacement, maximum order quantity where stated.
- `purchasing_lines.py`: `build_purchase_lines(analysis, settings)`. `purchasing_plan.py` is left as it was (section 13).

Tests: REV-1 to REV-6, PUR-6 to PUR-8, the existing `test_purchasing_workflow.py` unchanged, and one test per reason code.

Exit: `part_purchasing_analyze` and `part_purchasing_plan` return the same
shape as before, plus the new fields, and the existing tests pass.

### M4: Purchasing BOM export

Scope: CSV for the Purchasing BOM, and the tool.

Deliverables:
- `purchasing_bom_export.py` (column registry, row building, cross-checks), reusing the Standard BOM's CSV helpers.
- Tool `part_purchasing_export_csv` in `tools/purchasing.py`.
- Docs.

Tests: PUR-1 to PUR-15, STD-1 to STD-5 again, spreadsheet safety on
distributor-supplied text, naming collisions through `export_file`.

Exit: one end-to-end test from synthetic Altium data and fake distributors
to a CSV that is verified field by field programmatically.

### M5: Approval and cart handoff

Scope: prepare, approve, commit, journal, readback, for both distributors.

Deliverables:
- `purchasing_cart.py` (proposal, hash, delta logic, journal), outside the three modules guarded against cart words.
- Tools `part_purchasing_cart_prepare` (read-only) and `part_purchasing_cart_commit` (writes).
- `destination.py`: a validated working-file helper for the journal.
- Metadata, parity and docs entries.

Tests: CART-1 to CART-15, including a simulated timeout after a successful
Mouser insert, an empty readback, a duplicate commit, and a changed price
between prepare and commit.

Exit: with a fake Mouser that doubles on retry, a retried commit leaves
the cart at the target.

## 8. Synthetic test scenarios

Data is invented: makers Acme, Globex, Initech, Umbrella; project
`DemoBoard`; part numbers such as `ACM-R-0100`. No real BOM text.

### 8.1 Standard BOM

| ID | Scenario |
|---|---|
| TS-S01 | Tool signature has no assembly parameter |
| TS-S02 | Default header is the five names in order |
| TS-S03 | Network and `enrich_bom` patched to raise; export succeeds |
| TS-S04 | Characterization bytes match for a fixed synthetic BOM |
| TS-S05 | `COLUMNS` and `DEFAULT_COLUMNS` of the Standard BOM unchanged after importing the new modules |

### 8.2 Purchasing BOM

| ID | Scenario |
|---|---|
| TS-P01 | No argument, no file: 2 assemblies |
| TS-P02 | `assemblies=0`, `True`, `"3"` and `2.5` refused before any read |
| TS-P03 | BOM Qty 7, 2 assemblies: Required Qty 14 |
| TS-P04 | Each column identifier produces its header and values |
| TS-P05 | Unknown, duplicate, empty column and non-list are refused; order is kept |
| TS-P06 | Five fitted lines and two unresolved components give seven rows; BOM Qty sums to the component count |
| TS-P07 | Unresolved component row has blank maker, status `blocked`, Altium's reason text |
| TS-P08 | Out-of-stock and unmatched parts are rows with reasons |
| TS-P09 | A line with three reasons lists all three in severity order |
| TS-P10 | A distributor description beginning `=HYPERLINK(` is neutralized; `1E5` and `0012345678` warn |
| TS-P11 | No cart, list or order URL is requested |
| TS-P12 | Prices in two currencies give two totals and no combined total |
| TS-P13 | A cut-short read is refused; with `allow_incomplete` every row has `BOM_INCOMPLETE` |
| TS-P14 | DNP row with `include_dnp`: Required Qty 0, no lookup call recorded |
| TS-P15 | Name taken: timestamp suffix, then `_2`; repo path refused |
| TS-P16 | A row at 20 times its requirement shows Surplus and Quantity Rule and stays `ready` |
| TS-P18 | DigiKey preferred; Mouser used with `FALLBACK_DISTRIBUTOR` when DigiKey is short, unmatched or unpriced; `lowest` picks the cheaper |
| TS-P17 | Each argument named in CFG-10 overrides the file on its own; the others keep their file values |

### 8.3 Quantity engine

| ID | Scenario |
|---|---|
| TS-Q01 | Defaults equal the values in QTY-1, and no multiplier setting exists |
| TS-Q02 | R = 100: 110 |
| TS-Q03 | R = 110: 121 |
| TS-Q04 | R = 5: 6; R = 3: 4 |
| TS-Q05 | Quantity below the first break and below MOQ: raised to both |
| TS-Q06 | MOQ 10, R 4: 10 |
| TS-Q07 | Multiple 5, R 11: 15 |
| TS-Q08 | Reel sold whole: the standard package is the multiple, and it is reported |
| TS-Q09 | Reeling fee included in every candidate's cost |
| TS-Q10 | Stock between floor and baseline: overage reduced, `OVERAGE_LIMITED_BY_STOCK` |
| TS-Q11 | Maximum order quantity below a candidate: candidate rejected, reason recorded |
| TS-Q12 | Property: purchase quantity is never below required, over generated ladders |
| TS-Q13 | Free upgrade, small part: 60 gives 100 |
| TS-Q14 | Free upgrade, expensive part (12.00 to 2.00): 250, 4.2 times the need |
| TS-Q15 | Free upgrade at break 3, beyond a window of 2: 1000 |
| TS-Q16 | Paid upgrade, expensive part (12.00 to 3.00): 250 |
| TS-Q17 | Paid upgrade, cheap passive, 20 times the requirement: 1000; no block, status unchanged |
| TS-Q18 | Furthest paid break wins: 100 |
| TS-Q19 | Free beats a paid upgrade at a further break: 60, not 200 |
| TS-Q20 | Window of 2 ignores a third-break upgrade that fits the allowance; window 3 takes it; window 0 takes no paid upgrade but still finds a free one |
| TS-Q21 | Free and paid candidates above stock are rejected; the line stays at baseline or takes the next feasible one |
| TS-Q22 | A candidate off the order multiple is rounded up, re-costed, then compared |
| TS-Q23 | Each packaging is decided on its own terms; the cheapest valid one wins |
| TS-Q24 | Exact boundary: cost 53.400 against allowance 53.400 accepted; 53.41 refused |
| TS-Q25 | Percentages 0, 10, 25 and 5 on the 5.7 ladder give the results in the table |
| TS-Q26 | Fixed amounts only: 20.00 accepted; 12.00 accepted exactly; 11.99 refused |
| TS-Q27 | Zero allowance: cost equal to the baseline accepted at zero extra spend; 101.00 against 100.00 refused |
| TS-Q28 | Percentage zero, fixed zero, window zero together: baseline only, free upgrades still found |
| TS-Q29 | Offer in another currency: fixed amount left out, percentage kept, the line says so |
| TS-Q30 | Percentage, fixed amount and window each overridden by an argument on its own, then by the file, then by the default |
| TS-Q31 | Negative, non-numeric and boolean settings and a non-integer window refused before any read |
| TS-Q32 | Exactness: `"10"`, `10` and `10.0` give the same allowance; a baseline of 0.30 at 10 percent gives 0.33 exactly |
| TS-Q33 | Exact baseline: ladder 1: 2.00, 60: 1.80, 130: 0.87, R 45 gives 130 (the script's truncated baseline refused it) |
| TS-Q34 | Two equal-cost candidates: smaller quantity; two equal offers: lower SKU |
| TS-Q35 | Every candidate has a verdict and a reason; the decision has an explanation with the numbers |
| TS-Q36 | Cost-only policy reproduces the legacy planner's results |
| TS-Q37 | A row at 20 times its requirement is `ready`, has no review reason and no approval is asked |
| TS-Q39 | Stock rules out break 1: it uses no window position, so breaks 2 and 3 are the two looked at |
| TS-Q40 | Stock covers the requirement but not the overage: overage becomes the largest multiple stock allows, not below the requirement |
| TS-Q41 | Stock short at DigiKey: Mouser supplies; short at both: blocked with no quantity change and no partial purchase |
| TS-Q42 | Two breaks round to one quantity: one position |
| TS-Q38 | `large_surplus_ratio` unset: no `LARGE_SURPLUS`; set to 10: raised above 10, not at exactly 10; severity may be `review`, never `blocking` |

### 8.4 Review

| ID | Code or case |
|---|---|
| TS-R01 | `OUT_OF_STOCK` |
| TS-R02 | `INSUFFICIENT_STOCK`, with the required quantity still shown |
| TS-R03 | Missing manufacturer; missing MPN; both |
| TS-R04 | Ambiguous maker |
| TS-R05 | `MANUFACTURER_MISMATCH` |
| TS-R06 | `MISSING_PRICE`, price zero, no currency |
| TS-R07 | `MOQ_ADJUSTED`, `MULTIPLE_ADJUSTED`, `PACKAGING_REEL_WHOLE` |
| TS-R08 | `BOM_CONFLICT` from each kind of inconsistency |
| TS-R09 | `LIFECYCLE_NRND` |
| TS-R10 | `LIFECYCLE_LAST_TIME_BUY` |
| TS-R11 | `LIFECYCLE_EOL` and `LIFECYCLE_OBSOLETE` |
| TS-R12 | `DISTRIBUTOR_DISCONTINUED` from the flag with an "active" text |
| TS-R13 | `LIFECYCLE_UNKNOWN`: no status anywhere; unrecognized text |
| TS-R14 | Mouser null with DigiKey active: no lifecycle reason at all (neither `LIFECYCLE_NOT_STATED` nor unknown) |
| TS-R15 | `DUPLICATE_SKU` |
| TS-R16 | `NO_DISTRIBUTOR_DATA` when both are down; `DISTRIBUTOR_UNAVAILABLE` when one is |
| TS-R17 | One part with four reasons keeps all four |
| TS-R18 | Status derivation matrix: blocking, review, info only, none |
| TS-R19 | A suggested replacement appears as text and is never in an order |
| TS-R20 | Blocked and review lines are excluded from the proposal |
| TS-R22 | An EOL, obsolete or discontinued line stays blocked with `[review.severity]` set to `info`, and is not in a cart proposal |
| TS-R21 | `LARGE_SURPLUS`: information by default, `review` when raised, a `blocking` override refused with a warning |

### 8.5 Configuration

| ID | Scenario |
|---|---|
| TS-F01 | Missing file is created with the template; a second call leaves the bytes alone |
| TS-F02 | An existing file is never rewritten, not even to add keys |
| TS-F03 | Folder follows `EDA_AGENT_EXPORT_DIR`; a folder inside the repo is refused |
| TS-F04 | Argument beats file beats default, per setting |
| TS-F05 | A `[credentials]` table or a key named `api_key` is refused and warned |
| TS-F06 | Unknown keys are kept and reported |
| TS-F07 | `UNRESOLVED_COMPONENT = "info"` is ignored with a warning; `LIFECYCLE_NRND = "blocking"` is honored |
| TS-F08 | Invalid TOML: all defaults, one warning naming the line |
| TS-F09 | Read-only folder: tool still runs, warning says why |
| TS-F10 | Reply reports value and source for each setting used |
| TS-F11 | `1.1` as a number and as a string both give exactly 1.1 |
| TS-F12 | `extra_spend_percent`, `extra_spend_flat`, `extra_spend_currency` and `price_break_window` are each set in the file and overridden by a call argument independently |
| TS-F13 | `max_quantity_multiplier` in an existing file is kept, warned about and ignored |
| TS-F15 | A severity override lowering `LIFECYCLE_EOL`, `LIFECYCLE_OBSOLETE` or `DISTRIBUTOR_DISCONTINUED` is refused with a warning |
| TS-F14 | The commented `[setup]` block is ignored; an uncommented one is reported as not yet supported |

### 8.6 Cart handoff

| ID | Scenario |
|---|---|
| TS-C01 | Same requests as `part_cart_add` and `part_list_add` |
| TS-C02 | `prepare` writes nothing and returns a stable `proposal_id` for equal input |
| TS-C03 | `commit` without approval, with a wrong id, or after a change of price, currency, lifecycle status, SKU, packaging or quantity writes nothing |
| TS-C04 | Only `ready` lines; a review line only when named; blocked never |
| TS-C05 | Empty cart: adds the target |
| TS-C06 | Cart holds 10, target 25: adds 15 |
| TS-C07 | Cart holds target: adds nothing |
| TS-C08 | Fake Mouser times out after applying the insert; retry adds nothing |
| TS-C09 | Journal written before the first write; updated after |
| TS-C10 | Create started with no key recorded: retry refuses to create |
| TS-C11 | Readback empty after a prior add: reported as unknown, no automatic retry |
| TS-C12 | Cart holds more than target: reported, unchanged |
| TS-C13 | Readback differs in quantity or packaging: not `verified` |
| TS-C14 | DigiKey line already present: skipped as satisfied |
| TS-C15 | DigiKey line present with another quantity: reported, unchanged |
| TS-C16 | Delta below MOQ is not refused when the target total is valid |
| TS-C17 | Designators reported as not sent to Mouser |
| TS-C18 | Metadata: commit not read-only; prepare read-only |
| TS-C19 | Journal under the export folder; repo path refused |

### 8.7 Future (feeder and setup quantities)

| ID | Scenario |
|---|---|
| FT-1 | Nothing configured: no line changes |
| FT-2 | Prefix R set to 100: a line needing 40 starts from 100 |
| FT-3 | The line carries `SETUP_QUANTITY_RAISED` and rule `setup`; status is unchanged |
| FT-4 | The baseline is the raised quantity, so the allowance is computed from it |
| FT-5 | A part with no matching rule is untouched, whatever its description says |
| FT-6 | A minimum above stock blocks the line |

## 9. Compatibility risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | `proj_export_purchasing_bom_csv` is the Standard BOM by function but not by name. | Keep the name; clarify the docstring; add a characterization test; new tool has a distinct name. |
| R2 | `build_quantity` defaults to 1 in the existing tools; the new default is 2. | The existing tools keep 1. Only new tools read the configured count. Documented in both docstrings. |
| R3 | `float` to `Decimal` can change an existing result in a boundary case, and Decimal is not JSON serializable. | Convert at the boundary; legacy fields stay numbers; equivalence suite; any intended change is listed in the commit. |
| R4 | The existing guard `test_nothing_in_the_workflow_can_write_a_cart_or_a_list` forbids cart words in `purchasing`, `purchasing_plan` and `purchasing_sources`. | Keep cart code in `purchasing_cart.py` and the new cart tool module, extend the guard to the new read modules, add a positive test for the cart module. |
| R5 | Generated and guarded artifacts: `docs/TOOL_REFERENCE.md` (generated; writes CRLF on Windows and must be converted back to LF), README tool counts, kicad parity entries (reason at most 100 characters), tool metadata (an export tool must not be read-only), the no-dash and no-user-path guards. | Each milestone that adds a tool regenerates and checks these, and runs `git diff --check`. |
| R6 | `test_on_disk_registrars_are_wired` already fails on `HEAD` (`register_purchasing_tools` is wired through `parts.py`, the test looks in `__init__.py`). | New registrars follow the same wiring and the pre-existing failure is reported, not hidden, in each milestone. |
| R7 | Cached prices (4 hours) can be older than an ordering decision needs. | Export may use the cache and stamps `retrieved_at`; `prepare` and `commit` bypass it. |
| R8 | Mouser sends null lifecycle for parts in production. | No lifecycle reason when another distributor says active; `LIFECYCLE_NOT_STATED` (info) when another states a non-active status; `LIFECYCLE_UNKNOWN` only when nobody states one. |
| R9 | Mouser has no maximum order quantity in the search offer and DigiKey no order multiple. | `None` means not stated; the engine never invents one; the cart read-back (Mouser's `SalesMaximumOrderQty`) is a second check. |
| R10 | Mouser inserts are additive, nothing here lowers or removes, and a cart opened in a browser can read back empty. | Target-total logic, a journal, unknown-state handling, no automatic retry after an empty readback (CART-5 to CART-9). |
| R11 | The Mouser cart check refuses a quantity below the minimum, which a delta add can be. | The orchestrator checks the target total itself and calls the existing add with its own check turned off, with a test that the total is still checked. |
| R12 | Two BOM lines can resolve to one distributor number (a maker spelled two ways). | `DUPLICATE_SKU`, blocking; merging would change the price break and is left to a person. |
| R13 | Distributor text is untrusted input to a spreadsheet. | All cells neutralized, not only BOM-sourced ones (PUR-10). |
| R14 | The flat 5.00 allowance and the 5.00 overage price limit are in a currency. | Applied only to offers in the configured currency; otherwise the percentage part alone, with a reason. |
| R15 | `config.toml` creation is a local write from tools that are classified read-only. | It writes only a preferences file in the export folder; creation failure is non-fatal; decision D8. |
| R16 | Journals and CSVs hold supplier and quantity data. | Under the export folder only, through the repo guard, never committed; `CLAUDE.md` already says so. |
| R17 | The journal must be updated in place, which the never-overwrite export rule forbids. | A separate, validated working-file helper in `destination.py`, atomic replace, keyed by `proposal_id`. |
| R18 | Cached lookups may not carry the new lifecycle flags. | M3 verifies the cached payload holds them and adds the flags to the cached shape if not; the cache schema version rule applies. |
| R19 | With no quantity multiplier, a paid upgrade can buy 10 or more times the requirement, and a free upgrade any amount. | A paid upgrade is bounded in money by `A` and in reach by `w`; a free upgrade lowers cost. Surplus, rule and explanation are always shown, and the warning threshold is configurable (section 5.8). |
| R20 | A large fixed amount with a large window can make paid upgrades frequent. | The three settings are independent and visible in every reply (CFG-7), so a user sees which one admitted an upgrade. |
| R21 | A first-draft file or note may mention `max_quantity_multiplier`. | CFG-11: treated as an unknown key, warned about, ignored. |

## 10. Unresolved decisions

Each has a recommendation. D3 to D5 were settled by the clarified rules
(revision 2) and are kept here as a record. The open ones are D2, D6 to D15
and the questions A1 to A10 at the end of this section; none blocks M1,
and A1, A2, A4 and A5 should be settled before M2 is finished.

| ID | Question | Recommendation |
|---|---|---|
| D1 | Tool names: `part_purchasing_export_csv`, `part_purchasing_cart_prepare`, `part_purchasing_cart_commit`? Keep `part_purchasing_plan` on the cost-only policy by default, with an optional `policy="configured"`? | Yes to both. Unify the two policies later, not now. |
| D2 | Which price decides whether overage applies: the price at `Q0` (this document), or at the original `R` as the script did? | At `Q0`: it is the price actually paid at the quantity ordered. Differs from the script only when MOQ moves the price break. |
| D3 | Baseline for the allowance: the overage-adjusted `Q1`, or the plain requirement? | Resolved in part: there is no quantity cap any more, so the baseline only feeds the allowance. Still `Q1` (the script's baseline); see A1. |
| D4 | When a free upgrade and a paid upgrade both exist? | Resolved: the free upgrade is preferred, even over a paid one at a further break. Among paid upgrades the furthest break wins. |
| D5 | Cap on quantity, and handling of surplus? | Resolved: no multiplier cap of any kind. Surplus is always reported, never blocks or needs approval, and an optional `large_surplus_ratio` warning (information, default unset) is configurable. |
| D6 | Lifecycle severities | Resolved (revision 3): EOL, obsolete and distributor discontinued are blocking and fixed; NRND, last time buy and unknown are review and tunable. |
| D7 | Decimal in JSON: strings in new blocks, numbers in legacy fields? | Yes. Numbers lose exactness; strings keep it. |
| D8 | When is `config.toml` created: first use of a purchasing tool (this document), or when the server starts? | First use. It avoids writing to a folder the user never asked to use. |
| D9 | Allow splitting one line across two distributors? | No, as today. Revisit with real cost data. |
| D10 | Should review lines such as NRND be cart eligible by default? | No; only by naming them in `include_lines`. |
| D11 | Implement lowering or removing cart quantities (Mouser has update and remove endpoints; the client does not call them)? | No. Out of scope; report and leave. |
| D12 | Journal location and retention. | `purchasing/journal` under the export folder, kept until the user deletes it; one file per `proposal_id`. |
| D13 | Module placement: `export/purchasing_*.py` (this document, matching the existing files) or a new `purchasing/` package? | Keep `export/purchasing_*.py` until the module count justifies a package. |
| D14 | DigiKey has lists, not carts; the user turns a list into a cart on the site. Is "cart handoff" satisfied by a verified list for DigiKey? | Yes, stated plainly in the tool docstrings and reply. |
| D15 | Other currencies: allow a configured flat amount per currency instead of one currency? | Not now. One currency, others get the percentage part only. |


### Remaining ambiguities

| ID | Question | Recommendation |
|---|---|---|
| A1 | The allowance formula says "baseline cost". Is that `cost(Q1)`, the cost of the overage-adjusted quantity (as the script computed it, once the truncation is fixed), or the cost of the plain requirement? | `cost(Q1)`. It keeps the script's behavior. |
| A2 | Does the window of 2 count price breaks as the distributor lists them, or only feasible ones? | Resolved (revision 3): only feasible, distinct breaks count. |
| A3 | A candidate whose cost equals the baseline cost is neither cheaper nor dearer. It is treated as paid and accepted, even with a zero allowance. Wanted? | Yes: more parts for no more money. |
| A4 | A free upgrade is "cheaper than the baseline". Should it instead have to be cheaper than the plain requirement's cost, when overage has raised the baseline above that? | Compare with the baseline `cost(Q1)`. The user pays no more than the overage plan already did. |
| A5 | When the furthest break is above stock, what happens? | Resolved (revision 3): the break is skipped and the best feasible quantity is taken. |
| A6 | Default for `large_surplus_ratio`: unset (this document), or a number such as 10? Is a ratio the right measure, or an amount of money or a count of units? | Unset, and a ratio. The surplus is in every row regardless. |
| A7 | For an offer in a currency other than USD, the flat allowance is left out and overage is not applied. Should a flat amount and a price limit be given per currency instead? | Not now (D15). |
| A8 | Feeder and setup minimums: which parts count as passives, how are classes written, does a setup minimum come before overage, and does it count in the baseline? | Rules written by the user (designator prefix or footprint), a floor before overage, part of the baseline. Deferred. |
| A9 | Across distributors under `lowest`, rank by final extended cost, or baseline cost? | Final extended cost. Under the default `digikey` preference the question does not arise: DigiKey is used when it can supply. |
| A10 | The allowance is per line. Should there be an optional cap for the whole order? | No, as in the script. |

## 11. Files and modules

### 11.1 Add

| File | Purpose | Milestone |
|---|---|---|
| `src/eda_agent/export/purchasing_model.py` | Section 4 dataclasses, enums, status derivation, Decimal helpers | M1 |
| `src/eda_agent/export/purchasing_config.py` | Template, creation, loading, precedence, validation, effective-settings report | M1 |
| `src/eda_agent/export/purchasing_quantity.py` | Section 5 engine and policies | M2 |
| `src/eda_agent/export/purchasing_review.py` | Reason catalogue, severities, lifecycle recognition | M3 |
| `src/eda_agent/export/purchasing_lines.py` | `build_purchase_lines`: offers, distributor choice, reasons, status, unresolved lines, totals | M3 |
| `src/eda_agent/export/purchasing_bom_export.py` | Purchasing BOM column registry, rows, cross-checks, CSV | M4 |
| `src/eda_agent/export/purchasing_cart.py` | Proposal, hash, target-total delta, journal, readback summary | M5 |
| `src/eda_agent/tools/purchasing_cart.py` | `part_purchasing_cart_prepare` and `part_purchasing_cart_commit` | M5 |
| `tests/test_purchasing_model.py`, `test_purchasing_config.py`, `test_purchasing_quantity.py`, `test_purchasing_lines.py`, `test_purchasing_bom_export.py`, `test_purchasing_cart.py` | One per module | M1 to M5 |
| `tests/test_standard_bom_unchanged.py` | Characterization of the Standard BOM | M1 |

### 11.2 Modify

| File | Change | Milestone |
|---|---|---|
| `src/eda_agent/export/purchasing_sources.py` | Keep the lifecycle flags, last buy date, suggested replacement and maximum order quantity in each matched entry | M3 |
| `src/eda_agent/tools/purchasing.py` | New export tool; optional `policy` and `assemblies` on the new tool only | M4 |
| `src/eda_agent/tools/__init__.py` | Wire `register_purchasing_tools` (moved out of `parts.py`) and the new `register_purchasing_cart_tools` | M5 |
| `src/eda_agent/tools/parts.py` | Drop the indirect call to `register_purchasing_tools` | M5 |
| `src/eda_agent/export/destination.py` | Working-file helper for the journal and the config path, both through `validated_path` | M1, M5 |
| `src/eda_agent/export/purchasing_bom_csv.py` | Docstring only: say "Standard BOM". Export helpers it already offers are imported, not changed | M4 |
| `src/eda_agent/tools/project.py` | Docstring of `proj_export_purchasing_bom_csv` only | M4 |
| `src/eda_agent/tools/metadata.py` | `part_purchasing_cart_commit` as `SILENT`; export tool not read-only | M4, M5 |
| `tests/test_altium_no_raise.py`, `tests/test_readme_prose_names_real_tools.py` | Set the three new tools aside (they reach networks); name `part_number` as a column identifier, not a tool | M4, M5 |
| `docs/PART_SOURCING.md` | Sections for the Purchasing BOM, the quantity rules, review reasons, configuration, handoff | M1 to M5 |
| `docs/TOOL_REFERENCE.md` | Regenerated (LF), README counts | M4, M5 |
| `tests/test_purchasing_workflow.py` | Extend the cart-word guard to the new read modules | M3 |
| `CLAUDE.md` | One line: purchasing journals and `config.toml` live under the export folder | M5 |

### 11.3 Not modified

`libimport/providers/mouser_cart.py`, `digikey_lists.py` and `_cache.py`
keep their behavior. The provider clients are the primitives; the new code
adds logic around them and does not change them. `config.py` and
`mcp_config.json` (the Altium bridge's runtime file) are unrelated and stay
untouched; `config.toml` is a separate file in a separate folder.

## 12. Safety constraints for every milestone

- No live distributor call and no cart or list change in any test. Tests use the existing fakes and recorded-request doubles.
- No Altium document is modified. The BOM is read through the existing tools only.
- No proprietary BOM data in the repository, the docs, the tests or the conversation. Synthetic data only.
- No API credential in `config.toml`, a journal, a CSV, a log line or a reply.
- Outputs go through `export_file`; the journal and config through `validated_path`.
- Each milestone is committed separately, with relevant tests only and `git diff --check`; the full suite is run by the owner, not as part of a milestone.

## 13. Implementation status

All five milestones are implemented and tested with synthetic data. No live
distributor call, cart or list write, or BOM export was made. Nothing is
committed.

| Milestone | Status | New and changed files | Tests |
|---|---|---|---|
| M1 Model, configuration, safety net | Implemented | `export/purchasing_model.py`, `export/purchasing_config.py`, `export/purchasing_review.py` (catalogue), `tests/test_standard_bom_unchanged.py` | `test_purchasing_model.py`, `test_purchasing_config.py`, `test_standard_bom_unchanged.py` |
| M2 Quantity engine | Implemented | `export/purchasing_quantity.py` | `test_purchasing_quantity.py` (every worked example of 5.6 and 5.7, the allowance matrix, boundaries, property tests, equivalence with the first planner) |
| M3 Review engine and integration | Implemented | `export/purchasing_review.py`, `export/purchasing_lines.py`, `export/purchasing_sources.py` (lifecycle flags, footprints) | `test_purchasing_lines.py` |
| M4 Purchasing BOM export | Implemented | `export/purchasing_bom_export.py`, `tools/purchasing.py` (`part_purchasing_export_csv`, shared pipeline, early refusal of a cut-short read) | `test_purchasing_bom_export.py` |
| M5 Approval and cart handoff | Implemented | `export/purchasing_cart.py`, `tools/purchasing_cart.py`, `export/destination.py` (`working_file`), `tools/__init__.py`, `tools/parts.py`, `tools/metadata.py` | `test_purchasing_cart.py` |

Also changed: `docs/PART_SOURCING.md` (new sections, generated review code
table), `docs/TOOL_REFERENCE.md` (regenerated, 1043 tools), `CLAUDE.md`.

### Deviations from the text above

- `purchasing_plan.py` (`price_offer`, `plan_purchases`) was not rewritten to
  delegate to the new engine (5.10 and R3). It is untouched, so
  `part_purchasing_plan` and `part_purchasing_analyze` behave exactly as before.
  The cost-only policy of the engine is proven equal to `price_offer` by a
  property test over generated ladders (TS-Q36) instead.
- `part_purchasing_plan` got no `policy` argument (CFG-10 was amended). The
  configured pipeline is `part_purchasing_export_csv` and the cart tools.
- Review logic lives in `purchasing_lines.py` and `purchasing_review.py`; the
  per-code scenarios are in `test_purchasing_lines.py` and
  `test_purchasing_model.py`, and a test checks that `PART_SOURCING.md` lists
  every reason code.
- `QuantityDecision.unit_price`, `extended_cost`, `baseline_cost`, `allowance`
  and `break_quantity` are optional (a shortfall may have no price), and
  `PurchaseLine` carries `lifecycle` and `suggested_replacement` (section 4).
- Tool registration moved: `register_purchasing_tools` was called from
  `tools/parts.py` and the repository's own wiring test (which already failed
  at the start of this work) flagged it. Both purchasing registrars are now
  wired in `tools/__init__.py`, and that test passes.
- No kicad parity entries were needed: the new tools are part tools,
  registered for every backend.
- `max_order_qty` is modelled and enforced, but no distributor offer states
  one yet, so it is always unset in real data.
- The `lines` mode of the Purchasing BOM has no Altium counts to cross-check.
- 6.7 (feeder and setup quantities) is not implemented, by design.

### Outstanding limitations

- The tools were verified with test doubles and a simulated Mouser wire, not
  against the live distributors. A first live read-only run of
  `part_purchasing_cart_prepare` is the next check; live writes need a
  throwaway cart.
- Cross-distributor ranking under `prefer="lowest"` compares final extended
  cost in one currency only.
- A DigiKey list line with another quantity, and a Mouser cart holding more
  than the target, are reported and left for the user: nothing here lowers,
  updates or removes.
- A Mouser readback that is empty, or lower than the journal, stops the commit
  until the user has looked (`cart_state_checked`).

## 14. Close part number matching (revision 4)

Exact matching stays first and unchanged. When no distributor has a line's
exact manufacturer part number, near misses are found, labelled, listed
together and used only on explicit approval. The user-facing description is in
`docs/PART_SOURCING.md` (Close part number matches).

| ID | Requirement | Acceptance |
|---|---|---|
| MPN-1 | The search runs only for lines no distributor matched exactly, never for exact matches, and only when asked for. The existing analysis tools do not ask. | `test_the_search_is_only_for_lines_with_no_exact_match`, `test_without_the_option_nothing_extra_is_searched_or_returned` |
| MPN-2 | A candidate is a distributor listing whose letters and digits equal the BOM's number (`Formatting Match`) or begin with them and go on (a suffix). Nothing else is a candidate. | `test_these_are_not_close_matches` |
| MPN-3 | Labels: Formatting Match, Likely Match, Possible Match, Uncertain Match. Likely needs a recognised packaging marker, the distributor's own packaging confirming it, an identical manufacturer and nothing contradicting. | `test_a_confirmed_packaging_suffix_is_likely`, `test_an_unverified_variant_suffix_is_possible`, `test_a_much_longer_suffix_is_uncertain`, `test_punctuation_only_is_its_own_verified_class` |
| MPN-4 | Suffix length never decides alone, and no percentage or score is ever produced: a label, the evidence and the doubts. | `test_a_known_marker_is_likely_even_when_it_is_four_letters_long`, `test_the_result_has_a_label_and_evidence_but_no_percentage` |
| MPN-5 | A suffix that continues the base number's digits, or a long unrecognised suffix, lowers the label. Descriptions are weak evidence (distributors abbreviate): they are shown as evidence or doubt and never change a label. | `test_a_suffix_that_continues_the_number_is_uncertain`, `test_descriptions_are_shown_as_weak_evidence_and_never_move_a_label`, `test_no_description_cannot_make_a_candidate_more_likely` |
| MPN-6 | The manufacturer name is evidence: a known short form counts as a partial match, any other differing name caps the label at Uncertain. | `test_a_different_maker_name_is_listed_but_never_likelier_than_uncertain`, `test_a_known_short_name_counts_like_a_partial_match_and_says_so` |
| MPN-7 | Every candidate of the BOM is presented together, most likely first, with original and candidate number, manufacturer, suffix difference, qualifier, stock, price, evidence and uncertainty, in the reply and in a companion CSV. | `test_all_candidates_are_listed_together_most_likely_first`, `test_the_companion_csv_has_the_columns_asked_for_and_the_approval_state` |
| MPN-8 | A candidate is used in a calculation, a CSV row or a cart only when the user approved that exact original and candidate pair. An approval of another spelling, or of a number that is not a close variant, does nothing or is refused. | `test_an_approved_pair_is_used_and_the_boms_number_is_kept`, `test_approving_one_variant_does_not_approve_another`, `test_an_approval_for_another_spelling_of_the_candidate_does_not_apply`, `test_a_substitution_that_is_not_a_close_variant_is_refused_before_a_read` |
| MPN-9 | The BOM's own number is preserved in every output and no Altium document is modified. An approved line shows the number ordered in a separate column. | `test_an_approved_pair_is_used_and_the_boms_number_is_kept`, `test_the_export_uses_an_approved_pair_and_shows_the_number_ordered` |
| MPN-10 | An approved variant whose maker name differs is a review line; every approved variant carries `APPROVED_MPN_VARIANT`; an unapproved line with candidates carries `CLOSE_MATCH_AVAILABLE` and stays blocked. | `test_an_unapproved_candidate_is_never_in_a_proposal`, `test_candidates_are_found_labelled_and_none_is_used` |
| MPN-11 | In a cart proposal an approved item shows both numbers, the distributor is asked to hold the number ordered, and the fingerprint covers it. | `test_an_approved_variant_enters_with_both_numbers_and_changes_the_id`, `test_commit_needs_the_same_approvals_as_prepare` |

Tests: `tests/test_purchasing_matching.py` (classification, labels, evidence,
ordering, approvals, aliases) and `tests/test_purchasing_close_matches.py`
(the workflow end to end, the export, the companion file and the cart). A
mutation check (bypassing the approval gate) fails 12 of the 24 workflow tests.

Deviations and limits: punctuation-only matches also need approval, because
they are still a different string (revision 5 makes approvals persistent per
project; an approval given in a call is still per call); the prefix keyword
search is a heuristic and can miss a distributor's listing; the packaging and
maker tables (`purchasing_matching.py`) are small and meant to be extended
with evidence.

Files (revision 4). New: `src/eda_agent/export/purchasing_matching.py`,
`tests/test_purchasing_matching.py`, `tests/test_purchasing_close_matches.py`.
Changed: `export/purchasing_sources.py` (`enrich_bom` takes
`find_close_matches` and `approved_variants`), `export/purchasing_lines.py`
(reasons, matched number, the consolidated list), `export/purchasing_model.py`
(`PurchaseLine.matched_mpn`, `mpn_variant`), `export/purchasing_review.py` (two
reasons), `export/purchasing_bom_export.py` (Matched MPN and MPN Match columns,
the companion file), `export/purchasing_cart.py` (both numbers in an item and
in the fingerprint), `export/purchasing_config.py` (default columns),
`tools/purchasing.py` and `tools/purchasing_cart.py` (`approve_mpn_variants`),
`docs/PART_SOURCING.md`, `docs/TOOL_REFERENCE.md`.

## 15. Project-scoped MPN decisions (revision 5)

Manual approvals and rejections of close part number matches belong to the one
Altium project they were made in. Global rules in `config.toml` may apply to
every project. The user-facing description is in `docs/PART_SOURCING.md` (Who
authorizes a match, and for which project).

| ID | Requirement | Acceptance |
|---|---|---|
| SCOPE-1 | A project is identified by a hash of the canonical, absolute `.PrjPcb` path, not the file name: the same name in two folders is two projects; the same file spelled differently is one; a relative path or `lines` input names none. | `test_the_identity_is_the_canonical_path_not_the_file_name`, `test_lines_given_directly_have_no_project` |
| SCOPE-2 | Manual decisions are kept locally under `EDA_AGENT_EXPORT_DIR` (`purchasing/mpn_decisions/<project id>.json`), never in the repository, holding no quantity, price, SKU or credential. A file that names another project is refused. | `test_the_store_is_under_the_export_folder_and_never_the_repository`, `test_a_store_in_the_repository_is_refused`, `test_a_file_that_names_another_project_is_refused`, `test_the_stored_file_holds_no_credential_and_no_quantity` |
| SCOPE-3 | A manual approval or rejection never applies to another project, including one with identical part numbers and the same file name. | `test_an_approval_made_in_one_project_never_reaches_another`, `test_a_rejection_made_in_one_project_never_reaches_another`, `test_opposite_decisions_in_two_projects_are_independent` |
| SCOPE-4 | Within one project a decision is reused by the Purchasing BOM export, a later analysis, the DigiKey list and the Mouser cart tools, with no argument and no repeated approval. | `test_a_project_approval_is_reused_by_every_tool_without_asking_again`, `test_the_same_project_path_spelled_differently_shares_its_decisions` |
| SCOPE-5 | Another project needs its own approval unless a global rule in `config.toml` independently authorizes the match. Only `formatting` and `likely` can be named, only for an identical manufacturer name; a project's rejection beats the rule in that project only. | `test_a_global_rule_applies_to_every_project_without_a_manual_decision`, `test_a_projects_rejection_beats_the_global_rule_in_that_project_only`, `test_only_formatting_and_likely_can_be_named_globally`, `test_a_global_rule_needs_the_named_label_and_an_identical_maker` |
| SCOPE-6 | A decision is stale, reported and not applied, when the component's manufacturer or part number changes, the candidate's spelling or manufacturer changes, or the candidate is no longer listed. A stale decision does not block an independent global or per-call authorization. A decision for a number that now matches exactly is unused. | `test_a_decision_is_stale_when_the_components_part_number_changes`, `..._manufacturer_changes`, `..._the_proposed_match_is_no_longer_the_same`, `..._the_candidate_is_no_longer_listed`, `test_a_stale_rejection_is_reported_and_no_longer_hides_the_candidate`, `test_a_stale_decision_does_not_stop_an_independent_global_authorization`, `test_a_decision_for_a_component_now_matching_exactly_is_unused_not_stale` |
| SCOPE-7 | Project MPN approvals are separate from plan and cart fingerprints: a stored approval authorizes no changed quantity, price or destination, and the fingerprint does not depend on how the MPN was authorized. | `test_an_mpn_approval_authorizes_no_quantity_price_or_destination`, `test_the_cart_fingerprint_does_not_depend_on_how_the_mpn_was_authorized` |
| SCOPE-8 | The CSV keeps the original MPN, the matched MPN, the approval method and an audit string (original and matched number and maker, label, method, date, decision id, project id, note); the companion file carries the same for every candidate. | `test_the_csv_keeps_the_original_number_the_method_and_the_audit`, `test_the_default_columns_carry_the_method_and_the_audit` |
| SCOPE-9 | Altium documents are only read. The focused project must not change during a run. | `test_an_approval_made_in_one_project_never_reaches_another` (reads only), `test_a_focus_change_during_the_run_applies_and_writes_nothing` |

Precedence: the project's manual rejection, the project's manual approval, an
approval given for the call, then a global rule. Mutation checks (identify a
project by file name only; ignore a changed candidate maker; let an approval
override a rejection) each fail between 2 and 9 of the tests above.

Files. New: `export/purchasing_decisions.py`,
`tests/test_purchasing_mpn_decisions.py`. Changed: `export/purchasing_sources.py`
(an `Authorizer` judges each candidate), `export/purchasing_config.py` (the
`[matching]` rule), `export/purchasing_bom_export.py` (MPN Approval and MPN
Approval Audit columns, decision columns in the companion file),
`export/purchasing_lines.py`, `export/purchasing_review.py`
(`CLOSE_MATCH_REJECTED`), `export/purchasing_cart.py` (the journal keeps who
authorized the number), `export/destination.py` (`working_file` can read
without creating folders), `tools/purchasing.py` (project identification,
`part_purchasing_mpn_decisions`), `tools/purchasing_cart.py`, `tools/metadata.py`,
`docs/PART_SOURCING.md`, `docs/TOOL_REFERENCE.md`.

Limits: decisions are recorded one project at a time and are not exported or
merged between machines; a project moved to another folder is a new project
(its decisions stay under the old identifier until revoked there).
