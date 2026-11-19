# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""How many to buy of one offer. Pure arithmetic, ``Decimal`` throughout.

``decide_quantity(offer, required, policy)`` starts from the requirement and
works up, never down:

1. the floor: the requirement raised to the minimum order and the order
   multiple;
2. overage on an inexpensive part (a factor, up to a unit price), rounded up
   exactly and reduced to what stock allows;
3. the baseline cost of that quantity and the spending allowance,
   ``baseline * (1 + extra_spend_percent / 100) + extra_spend_flat``;
4. every price break above the one in force as a candidate. A candidate that
   stock, a maximum order quantity or the order multiple rules out is
   rejected and uses no position;
5. an upgrade that costs LESS than the baseline is taken from anywhere on the
   ladder, at any unit price and any size of increase, the cheapest first;
6. otherwise an upgrade that costs at most the allowance, among the first
   ``price_break_window`` feasible, distinct breaks, the furthest one.

There is no limit on how far the quantity may grow relative to the
requirement: only stock, the maximum order quantity, the minimum, the
multiple and the packaging limit it, and the allowance and the window bound
what a paid upgrade may cost and how far it may look. The requirement is a
floor that no branch goes under. Every candidate, taken or refused, and the
reason, is kept in the decision.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Optional

from .purchasing_model import (
    Candidate,
    PriceBreak,
    QuantityDecision,
    QuantityPolicy,
    SupplierOffer,
)

__all__ = ["allowance", "decide_quantity", "usable_ladder"]

ZERO = Decimal(0)


def _dec(value) -> Decimal:
    """``value`` as an exact Decimal; a float by its shortest form."""
    if isinstance(value, Decimal):
        return value
    return Decimal(repr(value) if isinstance(value, float) else str(value))


def usable_ladder(offer: SupplierOffer) -> list[PriceBreak]:
    """The offer's price breaks, lowest quantity first, usable ones only."""
    ladder = []
    for entry in offer.price_breaks or ():
        quantity, price = entry.quantity, entry.unit_price
        if isinstance(quantity, int) and not isinstance(quantity, bool) \
                and quantity >= 1 and isinstance(price, Decimal) \
                and price.is_finite() and price > 0:
            ladder.append(entry)
    return sorted(ladder, key=lambda b: (b.quantity, b.unit_price))


def _price_at(ladder: list[PriceBreak], quantity: int) -> PriceBreak:
    found = ladder[0]
    for entry in ladder:
        if entry.quantity <= quantity:
            found = entry
    return found


def _up(quantity: int, minimum: int, multiple: int) -> int:
    wanted = max(quantity, minimum)
    return -(-wanted // multiple) * multiple


def allowance(baseline_cost: Decimal, policy: QuantityPolicy,
              offer_currency: Optional[str]) -> tuple[Decimal, bool]:
    """The highest cost a paid upgrade may have, and whether the fixed
    amount was left out because the offer is in another currency."""
    flat = _dec(policy.extra_spend_flat)
    skipped = False
    if offer_currency and offer_currency.upper() \
            != policy.extra_spend_currency.upper():
        flat, skipped = ZERO, bool(flat)
    return (baseline_cost * (1 + _dec(policy.extra_spend_percent) / 100)
            + flat, skipped)


def _shortfall(required: int, floor: int, why: str, text: str,
               ladder: list[PriceBreak], fee: Decimal, flags: list[str],
               limits: list[str]) -> QuantityDecision:
    unit = cost = None
    if ladder and floor >= 1:
        unit = _price_at(ladder, floor).unit_price
        cost = floor * unit + fee
    return QuantityDecision(
        required_qty=required, floor_qty=floor, baseline_qty=floor,
        baseline_cost=cost, allowance=None, purchase_qty=floor,
        unit_price=unit, extended_cost=cost,
        break_quantity=_price_at(ladder, floor).quantity if ladder else None,
        surplus=floor - required, surplus_ratio=Decimal(floor) / required,
        rule="required", explanation=text, extra_spend=None,
        shortfall=why, flags=tuple(flags), candidates=(),
        limits_applied=tuple(limits))


def decide_quantity(offer: SupplierOffer, required: int,
                    policy: QuantityPolicy) -> QuantityDecision:
    """What to order of ``offer`` to cover ``required``, and why.

    ``decision.shortfall`` is None when the offer can supply the
    requirement. When it cannot (no price, no currency, stock or a maximum
    order quantity below the floor) the decision still names the quantity
    that would be needed, and nothing smaller is ever proposed.
    """
    if isinstance(required, bool) or not isinstance(required, int) \
            or required < 1:
        raise ValueError(f"required quantity {required!r} is not a whole "
                         "number of 1 or more")
    flags: list[str] = []
    limits: list[str] = []
    ladder = usable_ladder(offer)
    fee = offer.reeling_fee if isinstance(offer.reeling_fee, Decimal) \
        and offer.reeling_fee > 0 else ZERO

    if not ladder:
        return _shortfall(required, required, "no_price",
                          "the distributor states no usable price", ladder,
                          fee, flags, limits)
    if not offer.currency:
        return _shortfall(required, required, "no_currency",
                          "the distributor states no currency for its price",
                          ladder, fee, flags, limits)

    minimum = offer.moq if isinstance(offer.moq, int) \
        and not isinstance(offer.moq, bool) and offer.moq >= 1 else None
    if minimum is None:
        minimum = ladder[0].quantity
        if minimum > 1:
            flags.append("MOQ_NOT_STATED")
    minimum = max(minimum, ladder[0].quantity)

    multiple = offer.multiple if isinstance(offer.multiple, int) \
        and not isinstance(offer.multiple, bool) and offer.multiple >= 1 \
        else None
    packs = [str(p).casefold() for p in offer.packaging or ()]
    reel = len(packs) == 1 and ("tape & reel" in packs[0]
                                or packs[0] in ("reel", "full reel"))
    if multiple is None:
        package = offer.standard_package
        if reel and isinstance(package, int) and package > 1:
            multiple = package
            flags.extend(("MULTIPLE_NOT_STATED", "PACKAGING_REEL_WHOLE"))
            limits.append(f"a reel is sold whole: multiple {package}")
        else:
            multiple = 1
    elif multiple > 1:
        limits.append(f"order multiple {multiple}")
    if minimum > 1:
        limits.append(f"minimum order {minimum}")

    floor = _up(required, minimum, multiple)
    if floor > max(required, minimum):
        flags.append("MULTIPLE_ADJUSTED")
    if minimum > required:
        flags.append("MOQ_ADJUSTED")

    stock = offer.stock if isinstance(offer.stock, int) \
        and not isinstance(offer.stock, bool) else None
    maximum = offer.max_order_qty if isinstance(offer.max_order_qty, int) \
        and not isinstance(offer.max_order_qty, bool) \
        and offer.max_order_qty >= 1 else None
    if stock is None:
        flags.append("STOCK_NOT_STATED")
    ceilings = [(x, name) for x, name in ((stock, "stock"),
                                          (maximum, "maximum order quantity"))
                if x is not None]
    limit = min(ceilings)[0] if ceilings else None
    limit_name = min(ceilings)[1] if ceilings else ""
    if limit is not None:
        limits.append(f"{limit_name} {limit}")

    def cost(quantity: int) -> Decimal:
        return quantity * _price_at(ladder, quantity).unit_price + fee

    if limit is not None and floor > limit:
        if stock == 0:
            why, text = "out_of_stock", "the offer is out of stock"
        elif stock is not None and floor > stock:
            why = "insufficient_stock"
            text = f"{floor} are needed and {stock} are in stock"
        else:
            why = "purchase_limit"
            text = (f"{floor} are needed and the maximum order quantity is "
                    f"{maximum}")
        return _shortfall(required, floor, why, text, ladder, fee, flags,
                          limits)

    # ---- overage ---------------------------------------------------------
    baseline = floor
    factor = _dec(policy.overage_factor)
    if factor > 1:
        if (offer.currency or "").upper() \
                != policy.overage_currency.upper():
            flags.append("OVERAGE_SKIPPED_CURRENCY")
        elif _price_at(ladder, floor).unit_price \
                <= _dec(policy.overage_max_unit_price):
            target = int((Decimal(required) * factor)
                         .to_integral_value(rounding=ROUND_CEILING))
            baseline = _up(target, minimum, multiple)
            baseline = max(baseline, floor)
            if limit is not None and baseline > limit:
                baseline = max(floor, (limit // multiple) * multiple)
                flags.append("OVERAGE_LIMITED_BY_STOCK")

    base_cost = cost(baseline)
    allowed, flat_skipped = allowance(base_cost, policy, offer.currency)
    if flat_skipped:
        flags.append("ALLOWANCE_FLAT_SKIPPED")

    # ---- candidates ------------------------------------------------------
    in_force = _price_at(ladder, baseline).quantity
    seen: set[int] = {baseline}
    rows: list[dict] = []
    for entry in ladder:
        if entry.quantity <= in_force:
            continue
        quantity = _up(entry.quantity, minimum, multiple)
        if quantity in seen:
            rows.append({"quantity": quantity, "skip": "same quantity as an "
                         "earlier candidate"})
            continue
        seen.add(quantity)
        row = {"quantity": quantity, "cost": cost(quantity),
               "price": _price_at(ladder, quantity)}
        if limit is not None and quantity > limit:
            row["rejected"] = (f"above {limit_name} {limit}")
        rows.append(row)
    rows = [r for r in rows if "skip" not in r]

    position = 0
    for row in rows:
        if "rejected" in row:
            continue
        position += 1
        row["position"] = position
        if row["cost"] < base_cost:
            row["kind"] = "free_upgrade"
        else:
            row["kind"] = "paid_upgrade"
            if position > policy.price_break_window:
                row["rejected"] = (
                    f"outside the look-ahead of {policy.price_break_window} "
                    "feasible break(s)")
            elif row["cost"] > allowed:
                row["rejected"] = (f"cost {row['cost']:f} is over the "
                                   f"allowance {allowed:f}")

    free = [r for r in rows if r.get("kind") == "free_upgrade"
            and "rejected" not in r]
    paid = [r for r in rows if r.get("kind") == "paid_upgrade"
            and "rejected" not in r]
    chosen_row = None
    if free:
        chosen_row = min(free, key=lambda r: (r["cost"], r["quantity"]))
    elif paid:
        chosen_row = max(paid, key=lambda r: r["quantity"])

    if chosen_row is not None:
        quantity, total = chosen_row["quantity"], chosen_row["cost"]
        rule = chosen_row["kind"]
    else:
        quantity, total = baseline, base_cost
        if baseline > floor:
            rule = "overage"
        elif floor > required:
            rule = "moq" if minimum > required else "multiple"
        else:
            rule = "required"
    unit = _price_at(ladder, quantity)

    candidates: list[Candidate] = []
    candidates.append(Candidate(
        floor, _price_at(ladder, floor).unit_price, cost(floor),
        _price_at(ladder, floor).quantity, "floor",
        "chosen" if chosen_row is None and baseline == floor and quantity
        == floor else "accepted", "the requirement raised to the minimum "
        "and the multiple"))
    if baseline != floor:
        candidates.append(Candidate(
            baseline, _price_at(ladder, baseline).unit_price, base_cost,
            in_force, "overage",
            "chosen" if chosen_row is None else "accepted",
            "the requirement with overage; the baseline for the allowance"))
    for row in rows:
        kind = row.get("kind") or (
            "free_upgrade" if row["cost"] < base_cost else "paid_upgrade")
        if "rejected" in row:
            verdict, why = "rejected", row["rejected"]
        elif row is chosen_row:
            verdict = "chosen"
            why = ("costs less than the baseline" if kind == "free_upgrade"
                   else "within the allowance, the furthest break in reach")
        else:
            verdict = "accepted"
            why = ("costs less than the baseline, but a cheaper one was "
                   "taken" if kind == "free_upgrade" else
                   "within the allowance, but a cheaper or further one "
                   "was taken")
        price = row.get("price") or _price_at(ladder, row["quantity"])
        candidates.append(Candidate(
            row["quantity"], price.unit_price, row.get("cost", cost(
                row["quantity"])), price.quantity, kind, verdict, why))

    extra = total - base_cost
    if rule == "free_upgrade":
        explanation = (f"{quantity} at the {unit.quantity} break costs "
                       f"{total:f}, less than the baseline {base_cost:f} for "
                       f"{baseline}")
    elif rule == "paid_upgrade":
        explanation = (f"{quantity} at the {unit.quantity} break costs "
                       f"{total:f}, within the allowance of {allowed:f} over "
                       f"the baseline {base_cost:f} for {baseline}")
    elif rule == "overage":
        explanation = (f"{required} needed; {quantity} bought with overage "
                       f"at {unit.unit_price:f} each")
    elif rule in ("moq", "multiple"):
        explanation = (f"{required} needed; {quantity} is the least that can "
                       "be ordered")
    else:
        explanation = f"{required} needed and ordered"

    return QuantityDecision(
        required_qty=required, floor_qty=floor, baseline_qty=baseline,
        baseline_cost=base_cost, allowance=allowed, purchase_qty=quantity,
        unit_price=unit.unit_price, extended_cost=total,
        break_quantity=unit.quantity, surplus=quantity - required,
        surplus_ratio=Decimal(quantity) / required, rule=rule,
        explanation=explanation, extra_spend=extra, shortfall=None,
        flags=tuple(dict.fromkeys(flags)), candidates=tuple(candidates),
        limits_applied=tuple(limits))
