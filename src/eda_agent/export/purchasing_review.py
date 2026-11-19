# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Why a purchasing line needs a person: the review reason catalogue.

Every sourcing problem is a ``ReviewReason`` with a stable code and one of
three severities. A line may carry several. The status of the line comes
from them (``purchasing_model.derive_status``).

Some severities can be changed in ``config.toml`` and some cannot. A reason
that says the part cannot be bought as it stands (no match, no stock, no
price, a part at the end of its life) is fixed: no setting, argument or
override can make such a line ready, so a configuration change can never let
an end of life, obsolete or discontinued part slip into a cart.

Nothing here substitutes a part. A distributor's suggested replacement is
text on the line and never an order.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .purchasing_model import Lifecycle, ReviewReason, Severity

__all__ = [
    "CATALOGUE",
    "FIXED_CODES",
    "lifecycle_reasons",
    "make_reason",
    "validate_override",
]

_FIXED: tuple = ()
_ANY = ("info", "review", "blocking")
_SURPLUS = ("info", "review")

#: code -> (default severity, source, severities a user may choose, text)
CATALOGUE: dict[str, tuple] = {
    "UNRESOLVED_COMPONENT": (Severity.BLOCKING, "bom", _FIXED,
                             "the component has no usable Manufacturer and Part Number"),
    "MISSING_MANUFACTURER": (Severity.BLOCKING, "bom", _FIXED, "no usable Manufacturer"),
    "MISSING_MPN": (Severity.BLOCKING, "bom", _FIXED, "no usable Part Number"),
    "MPN_NOT_FOUND": (Severity.BLOCKING, "distributor", _FIXED,
                      "no distributor lists the exact manufacturer part number"),
    "MANUFACTURER_MISMATCH": (Severity.BLOCKING, "distributor", _FIXED,
                              "the number exists only under another manufacturer"),
    "AMBIGUOUS_MANUFACTURER": (Severity.BLOCKING, "distributor", _FIXED,
                               "two manufacturers could each be the BOM's"),
    "MANUFACTURER_PARTIAL_MATCH": (Severity.REVIEW, "distributor", _ANY,
                                   "the manufacturer names agree only as a prefix"),
    "BOM_CONFLICT": (Severity.REVIEW, "bom", _ANY, "the BOM data for this line conflicts"),
    "BOM_INCOMPLETE": (Severity.BLOCKING, "bom", _FIXED,
                       "Altium's read of the project was cut short; quantities may be short"),
    "DUPLICATE_SKU": (Severity.BLOCKING, "quantity", _FIXED,
                      "two BOM lines resolve to one distributor number"),
    "OUT_OF_STOCK": (Severity.BLOCKING, "distributor", _FIXED,
                     "no matched distributor has the part in stock"),
    "INSUFFICIENT_STOCK": (Severity.BLOCKING, "distributor", _FIXED,
                           "no matched distributor has enough stock for the requirement"),
    "STOCK_NOT_STATED": (Severity.REVIEW, "distributor", _ANY,
                         "the distributor states no stock for this offer"),
    "OVERAGE_LIMITED_BY_STOCK": (Severity.INFO, "quantity", _ANY,
                                 "the overage was reduced to what stock allows"),
    "OVERAGE_SKIPPED_CURRENCY": (Severity.INFO, "quantity", _ANY,
                                 "no overage: the offer is not in the overage currency"),
    "ALLOWANCE_FLAT_SKIPPED": (Severity.INFO, "quantity", _ANY,
                               "the fixed allowance was left out: the offer is not in the allowance currency"),
    "FALLBACK_DISTRIBUTOR": (Severity.INFO, "distributor", _ANY,
                             "the preferred distributor could not supply the line"),
    "MISSING_PRICE": (Severity.BLOCKING, "distributor", _FIXED, "no usable price"),
    "MISSING_CURRENCY": (Severity.BLOCKING, "distributor", _FIXED, "a price with no currency"),
    "CURRENCY_MIXED": (Severity.REVIEW, "distributor", _ANY,
                       "the distributors price in different currencies; no cost comparison was made"),
    "MOQ_ADJUSTED": (Severity.INFO, "quantity", _ANY, "the quantity was raised to the minimum"),
    "MULTIPLE_ADJUSTED": (Severity.INFO, "quantity", _ANY,
                          "the quantity was raised to the order multiple"),
    "MOQ_NOT_STATED": (Severity.REVIEW, "quantity", _ANY,
                       "no minimum is stated; the first price break was taken"),
    "MULTIPLE_NOT_STATED": (Severity.REVIEW, "quantity", _ANY,
                            "no multiple is stated; the reel's standard package was taken"),
    "PACKAGING_REEL_WHOLE": (Severity.INFO, "quantity", _ANY, "a full reel is sold whole"),
    "PURCHASE_LIMIT_EXCEEDED": (Severity.BLOCKING, "distributor", _FIXED,
                                "the requirement exceeds the distributor's maximum order quantity"),
    "LARGE_SURPLUS": (Severity.INFO, "quantity", _SURPLUS,
                      "the purchase quantity is far above the requirement"),
    "LIFECYCLE_NRND": (Severity.REVIEW, "lifecycle", _ANY, "not recommended for new designs"),
    "LIFECYCLE_LAST_TIME_BUY": (Severity.REVIEW, "lifecycle", _ANY, "last time buy"),
    "LIFECYCLE_EOL": (Severity.BLOCKING, "lifecycle", _FIXED, "end of life"),
    "LIFECYCLE_OBSOLETE": (Severity.BLOCKING, "lifecycle", _FIXED, "obsolete"),
    "DISTRIBUTOR_DISCONTINUED": (Severity.BLOCKING, "lifecycle", _FIXED,
                                 "discontinued by the distributor"),
    "LIFECYCLE_UNKNOWN": (Severity.REVIEW, "lifecycle", _ANY,
                          "no distributor states a lifecycle status this project knows"),
    "LIFECYCLE_NOT_STATED": (Severity.INFO, "lifecycle", _ANY,
                             "one distributor states no lifecycle status"),
    "LIFECYCLE_PRELIMINARY": (Severity.INFO, "lifecycle", _ANY, "preliminary or new product"),
    "DISTRIBUTOR_UNAVAILABLE": (Severity.INFO, "access", _ANY,
                                "one distributor could not answer"),
    "NO_DISTRIBUTOR_DATA": (Severity.BLOCKING, "access", _FIXED,
                            "no distributor answered for this line"),
    "STALE_PRICING": (Severity.INFO, "distributor", _ANY,
                      "the figures were reused from the cache and are old"),
    "CLOSE_MATCH_AVAILABLE": (Severity.INFO, "distributor", _ANY,
                              "close part number candidates are listed; none is used until approved"),
    "CLOSE_MATCH_REJECTED": (Severity.INFO, "distributor", _ANY,
                             "every close candidate was rejected for this project"),
    "APPROVED_MPN_VARIANT": (Severity.INFO, "distributor", _ANY,
                             "a close variant of the BOM's part number, approved by the user, is ordered"),
    "SETUP_QUANTITY_RAISED": (Severity.INFO, "quantity", _ANY,
                              "the quantity was raised to a feeder or setup minimum"),
}

#: Codes whose severity no configuration can change.
FIXED_CODES = frozenset(code for code, row in CATALOGUE.items() if not row[2])


def validate_override(code: str, level: str
                      ) -> tuple[Optional[Severity], Optional[str]]:
    """The severity ``level`` gives ``code``, or None with the reason it is
    refused. A fixed code is never changed."""
    row = CATALOGUE.get(code)
    if row is None:
        return None, f"unknown review code {code!r}"
    if not isinstance(level, str) or level.strip().lower() not in _ANY:
        return None, (f"{code}: severity {level!r} is not one of "
                      + ", ".join(_ANY))
    chosen = level.strip().lower()
    if not row[2]:
        return None, (f"{code} is fixed at {row[0].value}: a line with "
                      "this reason can never be made ready or cart eligible "
                      "by a setting")
    if chosen not in row[2]:
        return None, (f"{code} may be {' or '.join(row[2])}, not {chosen}")
    return Severity(chosen), None


def make_reason(code: str, message: Optional[str] = None,
                overrides: Optional[Mapping[str, Severity]] = None
                ) -> ReviewReason:
    """The reason ``code``, with the user's severity when one is allowed."""
    default, source, allowed, text = CATALOGUE[code]
    severity = default
    if overrides and allowed and code in overrides:
        severity = overrides[code]
    return ReviewReason(code, severity, message or text, source)


def lifecycle_reasons(found: Mapping[str, Lifecycle],
                      overrides: Optional[Mapping[str, Severity]] = None
                      ) -> list[ReviewReason]:
    """The lifecycle reasons for a part, from every distributor that matched
    it. The part's lifecycle belongs to the part, so a distributor saying
    end of life counts even when the line is bought at the other.

    A distributor that states nothing is not "unknown" when another states a
    status (Mouser sends null for a part in production); it is unknown only
    when nobody does, or when the text given is not one this project knows.
    """
    said: dict[str, list[str]] = {}
    unstated: list[str] = []
    stated_any = False
    for name, lc in found.items():
        if lc is None:
            unstated.append(name)
            continue
        status = lc.status
        if lc.end_of_life is True or status == "end_of_life":
            said.setdefault("LIFECYCLE_EOL", []).append(name)
        if status == "obsolete":
            said.setdefault("LIFECYCLE_OBSOLETE", []).append(name)
        if lc.discontinued is True or status == "discontinued":
            said.setdefault("DISTRIBUTOR_DISCONTINUED", []).append(name)
        if status == "nrnd":
            said.setdefault("LIFECYCLE_NRND", []).append(name)
        if status == "last_time_buy":
            said.setdefault("LIFECYCLE_LAST_TIME_BUY", []).append(
                name + (f" (last buy {lc.last_buy_date})"
                        if lc.last_buy_date else ""))
        if status in ("preliminary", "new"):
            said.setdefault("LIFECYCLE_PRELIMINARY", []).append(name)
        if status is not None:
            stated_any = True
        elif lc.raw:
            said.setdefault("LIFECYCLE_UNKNOWN", []).append(
                f"{name} says {lc.raw!r}")
        else:
            unstated.append(name)
    if unstated and not said.get("LIFECYCLE_UNKNOWN"):
        if stated_any:
            said.setdefault("LIFECYCLE_NOT_STATED", []).extend(unstated)
        elif len(unstated) == len(found):
            said.setdefault("LIFECYCLE_UNKNOWN", []).append(
                ", ".join(unstated) + " state none")
    out = []
    for code, who in said.items():
        base = CATALOGUE[code][3]
        out.append(make_reason(code, f"{base} ({'; '.join(who)})", overrides))
    return out
