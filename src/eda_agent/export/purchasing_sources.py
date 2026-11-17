# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A purchasing BOM with what DigiKey and Mouser say about each line.

Read-only. It asks the two catalogues about the exact manufacturer part
number of every BOM line and reports what each answered: the orderable
numbers, their packaging, stock, minimum, multiple and price breaks, and
the lifecycle. It writes nothing anywhere: no cart, no list.

The one rule that shapes it: a line is the part the design names, and
nothing else. A distributor's product is taken for a line only when its
manufacturer part number is the line's, character for character apart
from case and spaces, and its manufacturer is the line's. Anything
short of that is reported (``manufacturer_mismatch``, ``ambiguous``,
``not_found``) with what the distributor does have, and is never
ordered in the line's place.

Replies come through the distributor cache, so a second look at the
same BOM within four hours costs no API call. Each line says whether
its figures were read now or reused, and when they were read.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from eda_agent.libimport.providers import _cache
from eda_agent.libimport.providers.base import (
    PartHit,
    ProviderError,
    ProviderUnavailable,
)

__all__ = ["DISTRIBUTORS", "enrich_bom", "lines_from_input",
           "manufacturer_match"]

DISTRIBUTORS = ("digikey", "mouser")

#: Words that say what kind of company it is, not which.
_COMPANY_WORDS = {"inc", "incorporated", "corp", "corporation", "co",
                  "company", "ltd", "limited", "llc", "gmbh", "ag", "sa",
                  "nv", "bv", "plc", "the"}

_LINE_FIELDS = ("manufacturer", "part_number", "quantity", "designators",
                "description")


class BomInputError(ValueError):
    """The lines given in place of a project's BOM cannot be used."""

    def __init__(self, message: str, problems: Optional[list] = None):
        super().__init__(message)
        self.problems = problems or []


def _mpn_key(text: Any) -> str:
    return "".join(str(text or "").split()).casefold()


def _maker_words(text: Any) -> list[str]:
    words = re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold()).split()
    return [w for w in words if w not in _COMPANY_WORDS]


def manufacturer_match(ours: Any, theirs: Any) -> str:
    """How a distributor's manufacturer name compares with the BOM's.

    ``exact``: the same name, apart from case, punctuation and words
    like Inc. ``partial``: one name begins with the whole of the other
    ("Murata" and "Murata Electronics", "Vishay" and "Vishay / Dale"),
    which is the same company under a longer name often enough to
    accept and uncertain enough to say. ``different``: anything else,
    an abbreviation included: "TI" is not matched to "Texas
    Instruments" here.
    """
    a, b = _maker_words(ours), _maker_words(theirs)
    if not a or not b:
        return "different"
    if a == b:
        return "exact"
    short, long = sorted((a, b), key=len)
    return "partial" if long[:len(short)] == short else "different"


def lines_from_input(lines: Any) -> list[dict[str, Any]]:
    """BOM lines given directly, checked, in the purchasing BOM's shape.

    Each is ``manufacturer``, ``part_number`` and either ``quantity`` or
    ``designators`` (both, when they agree), with an optional
    ``description``. Refused whole when any line is malformed.
    """
    if not isinstance(lines, list) or not lines:
        raise BomInputError("lines is a non-empty list of BOM lines")
    out, problems = [], []
    seen: dict[tuple[str, str], int] = {}
    for position, line in enumerate(lines):
        if not isinstance(line, dict):
            problems.append({"line": position, "problems": ["not an object"]})
            continue
        wrong = []
        unknown = sorted(set(line) - set(_LINE_FIELDS))
        if unknown:
            wrong.append("unknown field(s) " + ", ".join(map(repr, unknown))
                         + "; a line has " + ", ".join(_LINE_FIELDS))
        maker = " ".join(str(line.get("manufacturer") or "").split())
        number = " ".join(str(line.get("part_number") or "").split())
        if not maker:
            wrong.append("no manufacturer")
        if not number:
            wrong.append("no part_number")
        designators = line.get("designators")
        if isinstance(designators, str):
            designators = [d.strip() for d in
                           re.split(r"[,\s]+", designators) if d.strip()]
        elif designators is None:
            designators = []
        elif isinstance(designators, (list, tuple)):
            designators = [str(d).strip() for d in designators
                           if str(d).strip()]
        else:
            wrong.append("designators is a list or a comma separated text")
            designators = []
        quantity = line.get("quantity")
        if quantity is None:
            quantity = len(designators)
        if isinstance(quantity, bool) or not isinstance(quantity, int) \
                or quantity < 1:
            wrong.append(f"quantity {line.get('quantity')!r} is not a whole "
                         "number of 1 or more, and there are no designators "
                         "to count")
        elif designators and quantity != len(designators):
            wrong.append(f"quantity {quantity} but {len(designators)} "
                         "designators")
        key = (maker.casefold(), _mpn_key(number))
        if maker and number and key in seen:
            wrong.append(f"the same part as line {seen[key]}; one line per "
                         "manufacturer and part number")
        elif maker and number:
            seen[key] = position
        if wrong:
            problems.append({"line": position, "manufacturer": maker,
                             "part_number": number, "problems": wrong})
            continue
        out.append({"manufacturer": maker, "part_number": number,
                    "description": " ".join(
                        str(line.get("description") or "").split()),
                    "quantity": quantity, "designators": designators,
                    "footprints": []})
    if problems:
        raise BomInputError(
            f"{len(problems)} of {len(lines)} lines cannot be used as they "
            "stand; nothing was looked up", problems)
    return out


# ---- asking one distributor -------------------------------------------------

def _digikey_hits(provider: Any, number: str) -> list[PartHit]:
    """DigiKey's products for a manufacturer number.

    ProductDetails answers one product. A number it refuses (two makers
    share it, or it is not a number DigiKey resolves) is searched for
    instead.
    """
    try:
        hit = provider._details(number)
        return [hit] if hit else []
    except ProviderUnavailable:
        raise
    except ProviderError as exc:
        if getattr(exc, "status", None) not in (400, 404):
            raise
    return provider.search(number, 50)


def _mouser_hits(provider: Any, number: str) -> list[PartHit]:
    return provider._post(provider._PART_URL, {
        "SearchByPartRequest": {"mouserPartNumber": number,
                                "partSearchOptions": "Exact"}})


def _providers() -> dict[str, tuple[Any, Callable[[Any, str], list]]]:
    from eda_agent.libimport.providers.distributors import (
        DigiKeyProvider,
        MouserProvider,
    )
    return {"digikey": (DigiKeyProvider(), _digikey_hits),
            "mouser": (MouserProvider(), _mouser_hits)}


def _offers(hit: PartHit) -> list[dict[str, Any]]:
    out = []
    for offer in (hit.sourcing or {}).get("offers") or []:
        if not offer.get("distributor_pn"):
            continue
        specific = offer.get("provider_specific") or {}
        out.append({
            "sku": offer["distributor_pn"],
            "packaging": list(offer.get("packaging") or []),
            "stock": offer.get("stock"),
            "moq": offer.get("moq"),
            "multiple": offer.get("multiple"),
            "standard_package": offer.get("standard_package"),
            "currency": offer.get("currency"),
            "price_breaks": list(offer.get("price_breaks") or []),
            # DigiKey's charge for winding a Digi-Reel, once per line.
            "reeling_fee": specific.get("digi_reel_fee") or None,
        })
    return out


def _candidate(hit: PartHit, match: str) -> dict[str, Any]:
    return {"manufacturer": hit.manufacturer, "mpn": hit.mpn,
            "manufacturer_match": match,
            "skus": [o["sku"] for o in _offers(hit)]}


def _read_one(name: str, provider: Any, lookup: Callable,
              maker: str, number: str) -> dict[str, Any]:
    """What one distributor has for one BOM line."""
    hits = [h for h in lookup(provider, str(number).strip())
            if _mpn_key(h.mpn) == _mpn_key(number)]
    if not hits:
        return {"status": "not_found",
                "reason": f"{name} lists no product with the manufacturer "
                          f"part number {number!r}"}
    judged = [(hit, manufacturer_match(maker, hit.manufacturer))
              for hit in hits]
    accepted = [(h, m) for h, m in judged if m != "different"]
    if not accepted:
        return {
            "status": "manufacturer_mismatch",
            "reason": (f"{name} lists {number!r} under "
                       + ", ".join(sorted({h.manufacturer or "?"
                                           for h in hits}))
                       + f", not {maker!r}. Nothing is ordered in its "
                       "place; correct the BOM's manufacturer if this is "
                       "the part"),
            "candidates": [_candidate(h, m) for h, m in judged]}
    # An exact name beats a partial one; partial ones stand only when no
    # exact one exists.
    if any(m == "exact" for _, m in accepted):
        accepted = [(h, m) for h, m in accepted if m == "exact"]
    makers = {" ".join(_maker_words(h.manufacturer)) for h, _ in accepted}
    if len(makers) > 1:
        return {
            "status": "ambiguous",
            "reason": (f"{name} lists {number!r} under {len(makers)} "
                       "manufacturers that could each be the BOM's "
                       f"{maker!r}; none was chosen"),
            "candidates": [_candidate(h, m) for h, m in accepted]}

    hit, match = accepted[0]
    if name == "digikey" and (hit.sourcing or {}).get("freshness", "") \
            .find("search index") >= 0:
        # Found through the search, whose figures can lag: read the one
        # product itself.
        numbers = [o["sku"] for o in _offers(hit)]
        try:
            live = provider._details(numbers[0]) if numbers else None
        except ProviderUnavailable:
            raise
        except ProviderError:
            live = None
        # Taken only when it is the same product: the number, the maker,
        # and the orderable number it was asked by.
        if live is not None and _mpn_key(live.mpn) == _mpn_key(number) \
                and manufacturer_match(maker, live.manufacturer) \
                != "different" \
                and numbers[0] in {o["sku"] for o in _offers(live)}:
            hit = live
    sourcing = hit.sourcing or {}
    offers: list[dict[str, Any]] = []
    for other, _ in accepted:
        source = hit if other is accepted[0][0] else other
        for offer in _offers(source):
            if offer["sku"] not in {o["sku"] for o in offers}:
                offers.append(offer)
    held = sourcing.get("cache") or {}
    entry: dict[str, Any] = {
        "status": "matched",
        "manufacturer": hit.manufacturer,
        "manufacturer_match": match,
        "mpn": hit.mpn,
        "distributor_description": hit.description,
        "lifecycle": (sourcing.get("lifecycle") or {}).get("status"),
        "lifecycle_text": (sourcing.get("lifecycle") or {}).get("raw"),
        "stock": sourcing.get("stock"),
        "lead_time_weeks": (sourcing.get("lead_time") or {}).get("weeks"),
        "product_url": sourcing.get("product_url"),
        "offers": offers,
        "cached": bool(held.get("hit")),
        "retrieved_at": sourcing.get("retrieved_at"),
    }
    if match == "partial":
        entry["note"] = (f"the BOM says {maker!r} and {name} says "
                         f"{hit.manufacturer!r}; taken as the same maker")
    if not offers:
        entry["note"] = f"{name} lists the part with no orderable number"
    return entry


def enrich_bom(bom_lines: list[dict[str, Any]], build_quantity: int = 1,
               refresh: bool = False,
               distributors: tuple = DISTRIBUTORS) -> dict[str, Any]:
    """Every BOM line with what each distributor says about it.

    ``bom_lines`` are purchasing BOM lines (manufacturer, part_number,
    description, quantity, designators). Returns ``lines`` and
    ``distributors``, the latter saying for each how many lines it was
    asked about, how many answers were reused from the cache, and why
    it stopped if it did.

    A distributor that cannot answer (no credential, unreachable, rate
    limited) is not asked again in this call: its remaining lines are
    ``not_checked`` with the reason, not ``not_found``.
    """
    sources = {name: pair for name, pair in _providers().items()
               if name in distributors}
    state = {name: {"asked": 0, "matched": 0, "from_cache": 0,
                    "read_now": 0}
             for name in sources}
    stopped: dict[str, str] = {}
    lines = []
    with _cache.fresh(refresh):
        for position, line in enumerate(bom_lines):
            per_board = int(line.get("quantity") or 0)
            entry: dict[str, Any] = {
                "line": position,
                "manufacturer": line.get("manufacturer", ""),
                "part_number": line.get("part_number", ""),
                "description": line.get("description", ""),
                "designators": list(line.get("designators") or []),
                "quantity_per_board": per_board,
                "required_quantity": per_board * build_quantity,
                "distributors": {},
            }
            for name, (provider, lookup) in sources.items():
                if name in stopped:
                    entry["distributors"][name] = {
                        "status": "not_checked", "reason": stopped[name]}
                    continue
                state[name]["asked"] += 1
                try:
                    found = _read_one(name, provider, lookup,
                                      entry["manufacturer"],
                                      entry["part_number"])
                except ProviderUnavailable as exc:
                    stopped[name] = str(exc)
                    found = {"status": "unavailable", "reason": str(exc)}
                except ProviderError as exc:
                    found = {"status": "error", "reason": str(exc)}
                except Exception as exc:  # noqa: BLE001 - one line only
                    found = {"status": "error",
                             "reason": f"{type(exc).__name__}: {exc}"}
                if found["status"] == "matched":
                    state[name]["matched"] += 1
                    state[name]["from_cache" if found["cached"]
                                else "read_now"] += 1
                entry["distributors"][name] = found
            lines.append(entry)
    for name in sources:
        state[name]["ok"] = name not in stopped
        if name in stopped:
            state[name]["stopped"] = stopped[name]
    return {"lines": lines, "distributors": state}
