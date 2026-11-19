# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The shared shapes of the purchasing workflow.

Frozen dataclasses with ``Decimal`` money and ``int`` quantities. Nothing
here reads a file, calls a distributor or holds a credential. The Purchasing
BOM, the quantity engine, the review engine and the cart proposal all speak
these types, so one meaning of "purchase quantity" or "status" exists.

Status is derived from the review reasons and never set by hand: any
blocking reason makes a line ``blocked``, otherwise any review reason makes
it ``review``, otherwise it is ``ready``. Information never changes it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Optional

__all__ = [
    "BomLine",
    "Candidate",
    "Lifecycle",
    "PriceBreak",
    "PurchaseLine",
    "PurchasingStatus",
    "QuantityDecision",
    "QuantityPolicy",
    "ReviewReason",
    "Severity",
    "SupplierOffer",
    "derive_status",
    "to_decimal",
    "to_jsonable",
]


class Severity(str, Enum):
    BLOCKING = "blocking"
    REVIEW = "review"
    INFO = "info"


class PurchasingStatus(str, Enum):
    READY = "ready"            # orderable, nothing a person must look at
    REVIEW = "review"          # orderable, but a person should look first
    BLOCKED = "blocked"        # cannot be ordered as it stands
    NOT_FITTED = "not_fitted"  # only with include_dnp; nothing to buy


def to_decimal(value: Any) -> Optional[Decimal]:
    """``value`` as an exact ``Decimal``, or None when it is not a finite
    number. A float is read through its shortest ``repr`` so ``1.1`` is
    exactly 1.1; a bool is never a number here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(repr(value) if isinstance(value, float)
                         else str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


@dataclass(frozen=True)
class PriceBreak:
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class Lifecycle:
    status: Optional[str] = None        # active, new, preliminary, nrnd,
    #                                     last_time_buy, end_of_life,
    #                                     obsolete, discontinued, or None
    raw: Optional[str] = None           # the distributor's own text
    discontinued: Optional[bool] = None
    end_of_life: Optional[bool] = None
    last_buy_date: Optional[str] = None


@dataclass(frozen=True)
class SupplierOffer:
    distributor: str
    sku: str
    packaging: tuple = ()
    currency: Optional[str] = None
    stock: Optional[int] = None          # None means not stated, never zero
    moq: Optional[int] = None
    multiple: Optional[int] = None
    standard_package: Optional[int] = None
    max_order_qty: Optional[int] = None  # None when the catalogue states none
    price_breaks: tuple = ()
    reeling_fee: Optional[Decimal] = None
    lifecycle: Lifecycle = Lifecycle()
    manufacturer: str = ""
    manufacturer_match: str = "exact"
    retrieved_at: Optional[str] = None
    cached: bool = False
    product_url: Optional[str] = None


@dataclass(frozen=True)
class BomLine:
    line_id: str                 # "L001"; an unresolved component is "U001"
    kind: str                    # "fitted" | "unresolved" | "not_fitted"
    manufacturer: str = ""
    part_number: str = ""
    description: str = ""
    designators: tuple = ()
    footprints: tuple = ()
    bom_quantity: int = 0        # per assembly; 1 per unresolved component
    data_problems: tuple = ()    # Altium's reason text, unchanged


@dataclass(frozen=True)
class QuantityPolicy:
    assemblies: int = 2
    overage_factor: Decimal = Decimal("1.10")
    overage_max_unit_price: Decimal = Decimal("5.00")
    overage_currency: str = "USD"
    extra_spend_percent: Decimal = Decimal("10")
    extra_spend_flat: Decimal = Decimal("5.00")
    extra_spend_currency: str = "USD"
    price_break_window: int = 2
    large_surplus_ratio: Optional[Decimal] = None
    # No quantity multiplier exists: quantity is never limited relative to
    # the requirement, only by stock, maximum order quantity, MOQ, order
    # multiple and packaging.

    @classmethod
    def cost_only(cls, assemblies: int = 1) -> "QuantityPolicy":
        """What the first planner does: no overage, no paid upgrade, only
        the cheaper break found anywhere on the ladder."""
        return cls(assemblies=assemblies, overage_factor=Decimal(1),
                   extra_spend_percent=Decimal(0),
                   extra_spend_flat=Decimal(0), price_break_window=0)


@dataclass(frozen=True)
class Candidate:
    quantity: int
    unit_price: Decimal
    extended_cost: Decimal
    break_quantity: int
    kind: str       # floor | overage | free_upgrade | paid_upgrade
    verdict: str    # chosen | accepted | rejected
    why: str


@dataclass(frozen=True)
class QuantityDecision:
    required_qty: int
    floor_qty: int
    baseline_qty: int
    baseline_cost: Optional[Decimal]
    allowance: Optional[Decimal]
    purchase_qty: int
    unit_price: Optional[Decimal]
    extended_cost: Optional[Decimal]
    break_quantity: Optional[int]
    surplus: int
    surplus_ratio: Decimal
    rule: str            # required | moq | multiple | overage | free_upgrade
    #                      | paid_upgrade
    explanation: str
    extra_spend: Optional[Decimal]
    shortfall: Optional[str]    # None | out_of_stock | insufficient_stock |
    #                             purchase_limit | no_price | no_currency
    flags: tuple = ()
    candidates: tuple = ()
    limits_applied: tuple = ()

    @property
    def feasible(self) -> bool:
        return self.shortfall is None


@dataclass(frozen=True)
class ReviewReason:
    code: str
    severity: Severity
    message: str
    source: str     # bom | distributor | quantity | lifecycle | access


def derive_status(reasons: Iterable[ReviewReason],
                  kind: str = "fitted") -> PurchasingStatus:
    """The status the reasons give. A not-fitted row has no status to
    derive: nothing is bought."""
    if kind == "not_fitted":
        return PurchasingStatus.NOT_FITTED
    severities = {r.severity for r in reasons}
    if Severity.BLOCKING in severities:
        return PurchasingStatus.BLOCKED
    if Severity.REVIEW in severities:
        return PurchasingStatus.REVIEW
    return PurchasingStatus.READY


@dataclass(frozen=True)
class PurchaseLine:
    line: BomLine
    required_qty: int
    offer: Optional[SupplierOffer]
    decision: Optional[QuantityDecision]
    status: PurchasingStatus
    reasons: tuple = ()
    alternatives: tuple = ()
    suggested_replacement: Optional[str] = None
    lifecycle: str = ""       # the distributor's own words, for display
    # The manufacturer part number actually ordered when the user approved a
    # close variant of the BOM's own (``line.part_number`` is never changed).
    matched_mpn: str = ""
    mpn_variant: Optional[dict] = None
    # When a line cannot be bought for lack of stock: one dict per matched
    # distributor with its stock, incoming lots, lead time and the date its
    # incoming stock first covers the requirement (see purchasing_lines).
    restock: tuple = ()


def to_jsonable(value: Any) -> Any:
    """``value`` as JSON-safe data: ``Decimal`` as an exact string, enums as
    their value, dataclasses as dicts, tuples as lists."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return value
