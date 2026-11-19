# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""What a distributor says about buying a part, in one shape.

A catalogue hit carries a ``sourcing`` dict beside its identity: stock,
lifecycle, lead time and one ``offer`` per orderable number, each with
its own packaging, minimum quantity and price breaks. The helpers here
turn the distributors' differing spellings into that shape.

Every helper returns ``None`` for a value the distributor did not give
or that cannot be read with certainty. A missing lifecycle is not
"active", an empty price list is not a price of zero, and a price string
that could be read two ways is left as text.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "lead_time",
    "lifecycle",
    "now_utc",
    "parse_price",
    "price_break",
    "to_int",
    "to_number",
]


def now_utc() -> str:
    """When a reply was read, as ISO 8601 UTC to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_number(value: Any) -> float | None:
    """``value`` as a number when it is one or plainly spells one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    """``value`` as a whole number: 5, 5.0 and "5" count, "5 Days" does not."""
    number = to_number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def parse_price(text: Any) -> float | None:
    """A formatted price ("$9,999.99", "0,285 €", "6.817,20 kr") as a number.

    The decimal mark is the last separator when both kinds appear. One
    separator followed by exactly three digits reads as a thousands mark
    in one locale and a decimal mark in another ("1.234"), so it is left
    unread unless the whole part is zero ("0.123").
    """
    digits = re.sub(r"[^\d.,]", "", str(text if text is not None else ""))
    digits = digits.strip(".,")
    if not digits:
        return None
    dot, comma = digits.rfind("."), digits.rfind(",")
    if dot >= 0 and comma >= 0:
        decimal = "." if dot > comma else ","
    else:
        mark = "." if dot >= 0 else ","
        if mark not in digits:
            decimal = ""
        elif digits.count(mark) > 1:
            decimal = ""
        else:
            whole, _, fraction = digits.partition(mark)
            if len(fraction) == 3 and whole.strip("0"):
                return None
            decimal = mark
    for mark in ".,":
        if mark != decimal:
            digits = digits.replace(mark, "")
    try:
        return float(digits.replace(decimal, ".") if decimal else digits)
    except ValueError:
        return None


def price_break(quantity: Any, unit_price: Any = None,
                price_text: Any = None) -> dict[str, Any] | None:
    """One price break, or None when it names no quantity.

    ``unit_price`` is the distributor's numeric price when it sends one;
    the formatted ``price_text`` is read only without it, and is kept in
    the break when it could not be read.
    """
    qty = to_int(quantity)
    if qty is None:
        return None
    price = to_number(unit_price)
    if price is None and price_text is not None:
        price = parse_price(price_text)
    out: dict[str, Any] = {"quantity": qty, "unit_price": price}
    if price is None and price_text:
        out["price_text"] = str(price_text)
    return out


def lead_time(raw: Any, unit: str = "") -> dict[str, Any] | None:
    """A lead time as given, with its length in weeks when that is clear.

    ``unit`` is what a bare number counts ("weeks" for DigiKey's
    ManufacturerLeadWeeks). A unit written in the text wins; a unit this
    does not know (a translated one) leaves ``weeks`` empty.
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    weeks = None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)", text)
    if match:
        word = (match.group(2) or unit).lower()
        if word.startswith("week"):
            weeks = float(match.group(1))
        elif word.startswith("day"):
            weeks = round(float(match.group(1)) / 7, 1)
    return {"raw": text, "weeks": weeks}


#: The distributors' own words for a status, lower case, and the one
#: word used here. A status missing from this table keeps its raw text
#: and gets no word rather than a guessed one.
_STATUS_WORDS = {
    "active": "active",
    "new product": "new",
    "new at mouser": "new",
    "preliminary": "preliminary",
    "not for new designs": "nrnd",
    "not recommended for new designs": "nrnd",
    "last time buy": "last_time_buy",
    "end of life": "end_of_life",
    "obsolete": "obsolete",
    "discontinued at digi-key": "discontinued",
    "discontinued at digikey": "discontinued",
    "discontinued": "discontinued",
}


def lifecycle(raw: Any, *, discontinued: Any = None,
              end_of_life: Any = None) -> dict[str, Any]:
    """Lifecycle as the distributor states it.

    ``status`` is one of active, new, preliminary, nrnd, last_time_buy,
    end_of_life, obsolete, discontinued, or None when the distributor
    gave no status or one not known here. None is "not stated", never
    "active". ``discontinued`` and ``end_of_life`` are the distributor's
    own flags, None when it sent none.
    """
    text = str(raw).strip() if raw is not None else ""
    return {
        "status": _STATUS_WORDS.get(text.lower()),
        "raw": text or None,
        "discontinued": discontinued if isinstance(discontinued, bool) else None,
        "end_of_life": end_of_life if isinstance(end_of_life, bool) else None,
    }
