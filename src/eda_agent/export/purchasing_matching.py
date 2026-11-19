# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Close manufacturer part number candidates, and how likely each is.

The purchasing workflow matches the BOM's manufacturer part number exactly
(case and spaces aside). When no distributor has it, a distributor often
lists a number that differs only a little: the same letters and digits with
different punctuation, or the same number with something added at the end.
This module sorts those near misses into labelled candidates for a PERSON to
judge. The labels are advisory. A candidate is never used in a calculation,
a CSV row's purchase or a cart unless the user has approved that exact
original and candidate pair, and the original number from the schematic is
never changed.

The labels, in order of likelihood:

* ``Formatting Match``: the letters and digits are identical and only the
  punctuation differs (``USB4110GFA`` and ``USB4110-GF-A``), and the
  manufacturer is the same. Verified by a real listing, but still a
  different string, so it still needs approval.
* ``Likely Match``: the candidate adds a suffix that is a recognised
  packaging marker (tape and reel), the distributor's own packaging for the
  candidate agrees with that meaning, the manufacturer is identical and
  nothing contradicts it.
* ``Possible Match``: the candidate adds a short suffix whose meaning is not
  verified, or a recognised marker that the distributor does not confirm.
* ``Uncertain Match``: the suffix is long, continues the digits of the base
  number, or the manufacturer's name differs.

Suffix length is one input among several and never decides alone, and no
percentage is ever given: a label, the evidence for it and what is still
doubtful. Descriptions are compared as weak evidence only (distributors
abbreviate them) and never change a label.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from .purchasing_model import to_decimal
from .purchasing_plan import price_offer

__all__ = [
    "FORMATTING",
    "LABELS",
    "LIKELY",
    "LONG_SUFFIX",
    "MatchInputError",
    "POSSIBLE",
    "maker_alias",
    "UNCERTAIN",
    "alnum",
    "approval_key",
    "classify",
    "order",
    "parse_approvals",
    "prefix_query",
]

FORMATTING = "Formatting Match"
LIKELY = "Likely Match"
POSSIBLE = "Possible Match"
UNCERTAIN = "Uncertain Match"
LABELS = (FORMATTING, LIKELY, POSSIBLE, UNCERTAIN)

#: A suffix this long (letters and digits) is "substantially longer": one
#: reason, with others, to call a candidate uncertain.
LONG_SUFFIX = 5

#: Suffixes that mark packaging and nothing else, whichever the maker. A
#: marker alone proves nothing: it only counts when the distributor's own
#: packaging for the candidate agrees.
PACKAGING_SUFFIXES = {
    "TR": "tape and reel",
    "REEL": "reel",
}

#: Conventions of single makers, keyed by a word of the maker's name. They
#: are trusted exactly as far as the generic ones: they only help when the
#: distributor's packaging confirms them. Extend with evidence, not memory.
MAKER_CONVENTIONS = {
    "diodes": {"7": "7 inch tape and reel", "13": "13 inch tape and reel"},
    "texas": {"R": "tape and reel", "T": "small tape and reel"},
}

#: Makers a distributor often lists under a short name. Each row is one
#: company's names, compared without punctuation or words like Inc. A pair
#: is evidence of identity, never proof, and is said so.
MAKER_ALIASES = (
    ("gct", "global connector technology"),
    ("ti", "texas instruments"),
    ("adi", "analog devices"),
    ("onsemi", "on semiconductor"),
)
_COMPANY = {"inc", "incorporated", "corp", "corporation", "co", "company",
            "ltd", "limited", "llc", "gmbh", "ag", "sa", "nv", "bv", "plc",
            "the"}

_REEL_WORDS = ("reel", "tape")
_STOP = {"the", "and", "for", "with", "pos", "position", "type"}


class MatchInputError(ValueError):
    """An approval list that cannot be used. Nothing was looked up."""

    def __init__(self, message: str, problems: Optional[list] = None):
        super().__init__(message)
        self.problems = problems or []


def alnum(text: Any) -> str:
    """Only the letters and digits of ``text``, lower case."""
    return re.sub(r"[^0-9a-z]+", "", str(text or "").casefold())


def _maker_key(text: Any) -> str:
    words = re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold()).split()
    return "".join(w for w in words if w not in _COMPANY)


def maker_alias(ours: Any, theirs: Any) -> bool:
    """True when the two names are a known short and long form of one
    company (``GCT`` and ``Global Connector Technology``)."""
    a, b = _maker_key(ours), _maker_key(theirs)
    if not a or not b or a == b:
        return False
    for names in MAKER_ALIASES:
        keys = {_maker_key(n) for n in names}
        if a in keys and b in keys:
            return True
    return False


def _words(text: Any) -> set[str]:
    return {w for w in re.findall(r"[0-9a-z]+", str(text or "").casefold())
            if len(w) >= 2 and w not in _STOP}


def prefix_query(mpn: str) -> Optional[str]:
    """A shorter search term that a distributor's keyword search would
    match when the full number's punctuation differs: the leading letters
    and the first run of digits (``USB4110GFA`` gives ``USB4110``)."""
    found = re.match(r"[A-Za-z]*\d+", re.sub(r"[^0-9A-Za-z]+", "", mpn))
    text = found.group(0) if found else ""
    return text if len(text) >= 4 and alnum(text) != alnum(mpn) else None


def _suffix_of(candidate: str, base_key: str) -> str:
    """The part of ``candidate`` after the letters and digits of the BOM
    number, punctuation included (``-TR``)."""
    seen = 0
    for index, char in enumerate(candidate):
        if char.isalnum():
            if seen == len(base_key):
                # include a separator that came just before this character
                start = index
                while start > 0 and not candidate[start - 1].isalnum():
                    start -= 1
                return candidate[start:]
            seen += 1
    return candidate[len(candidate):] if seen == len(base_key) else ""


def _punctuation_difference(bom: str, cand: str) -> str:
    def marks(text: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in text:
            if not c.isalnum() and not c.isspace():
                out[c] = out.get(c, 0) + 1
        return out

    a, b = marks(bom), marks(cand)
    parts = []
    for char in sorted(set(a) | set(b)):
        delta = b.get(char, 0) - a.get(char, 0)
        if delta > 0:
            parts.append(f"adds {delta} x {char!r}")
        elif delta < 0:
            parts.append(f"drops {-delta} x {char!r}")
    return "punctuation only: " + (", ".join(parts) or "spacing")


def _packaging_confirmed(offers: Sequence[dict[str, Any]]) -> list[str]:
    """The packagings the distributor lists for the candidate that are tape
    or reel."""
    found = []
    for offer in offers:
        for text in offer.get("packaging") or []:
            if any(w in str(text).casefold() for w in _REEL_WORDS):
                found.append(str(text))
    return list(dict.fromkeys(found))


def _meaning(maker: str, suffix_key: str) -> Optional[str]:
    if suffix_key.upper() in PACKAGING_SUFFIXES:
        return PACKAGING_SUFFIXES[suffix_key.upper()]
    for word in re.findall(r"[0-9a-z]+", str(maker or "").casefold()):
        table = MAKER_CONVENTIONS.get(word)
        if table and suffix_key.upper() in table:
            return table[suffix_key.upper()]
    return None


def _description_agreement(bom: str, cand: str) -> str:
    a, b = _words(bom), _words(cand)
    if not a or not b:
        return "unknown"
    share = len(a & b) / len(a)
    return "agree" if share >= 0.5 else "disagree" if share < 0.2 else "partly"


def _price(offers: Sequence[dict[str, Any]], quantity: int
           ) -> dict[str, Any]:
    """What it would cost to buy ``quantity`` of the candidate, from the
    cheapest packaging that can really supply it.

    Every offer (one orderable SKU and packaging) is priced on its own
    terms by ``purchasing_plan.price_offer``: the minimum order and the
    multiple raise the quantity first, the price is the one at the quantity
    actually ordered (never a price break above or below it), a reeling fee
    counts, and a packaging without the stock is passed over. So a reel with
    a 1,200 minimum is priced for 1,200, not for ``quantity``. Returns
    ``unit_price`` (a Decimal), ``currency``, ``order_quantity``,
    ``extended_price``, ``sku``, ``packaging`` and ``note``, each None when
    no offer can supply it.
    """
    best: Optional[dict[str, Any]] = None
    for offer in offers:
        priced = price_offer(offer, quantity, allow_overbuy=False)
        if not priced.get("usable") or priced.get("stock_short"):
            continue
        key = (priced["extended_price"], priced["order_quantity"],
               str(priced.get("sku") or ""))
        if best is None or key < best["key"]:
            best = {"key": key, "priced": priced}
    if best is None:
        return {"unit_price": None, "currency": None, "order_quantity": None,
                "extended_price": None, "sku": None, "packaging": None,
                "note": "no packaging is priced and in stock for the "
                        "quantity"}
    p = best["priced"]
    return {"unit_price": to_decimal(p["unit_price"]),
            "currency": p.get("currency"),
            "order_quantity": p["order_quantity"],
            "extended_price": to_decimal(p["extended_price"]),
            "sku": p.get("sku"),
            "packaging": ", ".join(str(x) for x in p.get("packaging") or []),
            "note": "; ".join(p.get("notes") or [])}


def classify(*, bom_manufacturer: str, bom_mpn: str, bom_description: str,
             candidate_mpn: str, candidate_manufacturer: str,
             candidate_description: str, manufacturer_match: str,
             offers: Sequence[dict[str, Any]], distributor: str,
             required_quantity: int = 1, stock: Optional[int] = None
             ) -> Optional[dict[str, Any]]:
    """The close-match record for one distributor listing, or None when it
    is not a close match.

    A candidate is close only when its letters and digits equal the BOM's
    (``Formatting Match``) or begin with them and go on (a suffix), and its
    manufacturer is the BOM's or agrees as a prefix. Anything else, an exact
    match included, is not returned.
    """
    base, cand = alnum(bom_mpn), alnum(candidate_mpn)
    if not base or not cand \
            or manufacturer_match not in ("exact", "partial", "alias",
                                          "different"):
        return None
    if "".join(str(candidate_mpn).split()).casefold() \
            == "".join(str(bom_mpn).split()).casefold():
        return None                                  # exact: not close
    evidence: list[str] = []
    doubts: list[str] = []
    if manufacturer_match == "exact":
        evidence.append("the manufacturer name is identical")
    elif manufacturer_match == "partial":
        doubts.append("the manufacturer names agree only as a prefix "
                      f"({bom_manufacturer!r} and {candidate_manufacturer!r})")
    elif manufacturer_match == "alias":
        doubts.append("the manufacturer names differ but are a known short "
                      f"and long form ({bom_manufacturer!r} and "
                      f"{candidate_manufacturer!r}); confirm it is one company")
    else:
        doubts.append(f"the manufacturer names differ ({bom_manufacturer!r} "
                      f"and {candidate_manufacturer!r}): this may be the same "
                      "company under another name, or another company's part")
    agreement = _description_agreement(bom_description, candidate_description)
    if agreement == "agree":
        evidence.append("the descriptions agree")
    elif agreement == "partly":
        doubts.append("the descriptions agree only in part")
    elif agreement == "disagree":
        # Weak on its own: distributors abbreviate ("CONN RCPT USB2.0 TYPEC"),
        # so this is shown and never changes a label.
        doubts.append("the descriptions share almost no words (weak "
                      "evidence, since distributors abbreviate, but compare "
                      "them)")
    else:
        doubts.append("a description is missing, so the descriptions could "
                      "not be compared")

    if cand == base:
        kind, suffix = "punctuation", ""
        difference = _punctuation_difference(bom_mpn, candidate_mpn)
        meaning = "formatting only: the letters and digits are the same"
        label = FORMATTING
        evidence.append("the letters and digits are identical; only the "
                        "punctuation differs, and the distributor lists "
                        "this number")
    elif cand.startswith(base):
        kind = "suffix"
        suffix = _suffix_of(candidate_mpn, base)
        suffix_key = cand[len(base):]
        difference = suffix or suffix_key
        separated = bool(suffix) and not suffix[0].isalnum()
        known = _meaning(candidate_manufacturer or bom_manufacturer,
                         suffix_key)
        reel = _packaging_confirmed(offers)
        if base[-1].isdigit() and suffix_key[0].isdigit() and not separated:
            label = UNCERTAIN
            meaning = "continues the digits of the base number"
            doubts.append("the suffix continues the digits of the BOM's "
                          "number with no separator, so it may be a "
                          "different part rather than a variant")
        elif known:
            meaning = f"recognised packaging marker: {known}"
            if reel:
                evidence.append(f"{suffix_key.upper()} is a recognised "
                                f"packaging marker ({known}) and "
                                f"{distributor} lists its packaging as "
                                + ", ".join(reel))
                label = LIKELY
            else:
                doubts.append(f"{suffix_key.upper()} is a recognised "
                              f"packaging marker ({known}) but "
                              f"{distributor} lists no tape or reel "
                              "packaging to confirm it")
                label = POSSIBLE
            if manufacturer_match != "exact" and label == LIKELY:
                label = POSSIBLE
        elif len(suffix_key) >= LONG_SUFFIX:
            label = UNCERTAIN
            meaning = "unrecognised, long suffix"
            doubts.append(f"the suffix {suffix or suffix_key!r} is "
                          f"{len(suffix_key)} characters and is not a known "
                          "packaging marker; it is probably a different "
                          "variant")
        else:
            label = POSSIBLE
            meaning = "unrecognised suffix: a variant until verified"
            doubts.append(f"the suffix {suffix or suffix_key!r} is not a "
                          "known packaging marker; what it means must be "
                          "verified in the manufacturer's ordering "
                          "information")
    else:
        return None

    if manufacturer_match == "different":
        # The maker is not verified, so nothing here can be likelier than
        # uncertain, whatever the number looks like.
        label = UNCERTAIN
    priced = _price(offers, required_quantity)
    return {
        "distributor": distributor, "kind": kind, "label": label,
        "original_mpn": bom_mpn, "candidate_mpn": candidate_mpn,
        "manufacturer": candidate_manufacturer,
        "manufacturer_match": manufacturer_match,
        "manufacturer_override": manufacturer_match in ("alias", "different"),
        "suffix": suffix, "difference": difference, "meaning": meaning,
        "stock": stock,
        "unit_price": priced["unit_price"],
        "price_at_quantity": priced["order_quantity"],
        "required_quantity": required_quantity,
        "order_quantity": priced["order_quantity"],
        "extended_price": priced["extended_price"],
        "price_sku": priced["sku"], "price_packaging": priced["packaging"],
        "price_note": priced["note"],
        "currency": priced["currency"],
        "skus": [o["sku"] for o in offers if o.get("sku")],
        "evidence": evidence, "uncertainty": doubts,
        "approved": False,
    }


def order(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most likely first: formatting, likely, possible, uncertain; within a
    label an identical manufacturer first, then in stock, then by number."""
    def key(c: dict[str, Any]):
        stock = c.get("stock")
        return (LABELS.index(c["label"]),
                0 if c.get("manufacturer_match") == "exact" else 1,
                0 if isinstance(stock, int) and stock > 0 else 1,
                str(c["original_mpn"]).casefold(),
                str(c["candidate_mpn"]).casefold(),
                str(c.get("distributor")))
    return sorted(candidates, key=key)


def approval_key(original: Any, candidate: Any) -> tuple[str, str]:
    """What an approval is keyed by: the original by its letters and digits
    (the BOM may space it differently) and the candidate by its exact
    spelling, because the exact string is what would be ordered. Approving
    ``USB4110-GF-A`` does not approve ``USB4110GF-A``."""
    return alnum(original), "".join(str(candidate or "").split()).casefold()


def parse_approvals(raw: Any) -> dict[tuple[str, str], dict[str, str]]:
    """The user's approved pairs, checked. Each is
    ``{"original_mpn": ..., "candidate_mpn": ...}``. Refused whole on any
    problem; ``None`` is no approvals."""
    if raw is None:
        return {}
    if not isinstance(raw, (list, tuple)):
        raise MatchInputError(
            "approve_mpn_variants is a list of "
            "{original_mpn, candidate_mpn} pairs")
    out: dict[tuple[str, str], dict[str, str]] = {}
    problems = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append({"item": position, "problems": ["not an object"]})
            continue
        wrong = []
        unknown = sorted(set(item) - {"original_mpn", "candidate_mpn"})
        if unknown:
            wrong.append("unknown field(s) " + ", ".join(map(repr, unknown))
                         + "; a pair has original_mpn and candidate_mpn")
        original = " ".join(str(item.get("original_mpn") or "").split())
        candidate = " ".join(str(item.get("candidate_mpn") or "").split())
        if not original or not candidate:
            wrong.append("both original_mpn and candidate_mpn are needed")
        elif alnum(original) == alnum(candidate) \
                and original.casefold() == candidate.casefold():
            wrong.append("the candidate is the original")
        elif not alnum(candidate).startswith(alnum(original)):
            wrong.append("the candidate must have the original's letters "
                         "and digits, with at most a suffix added; nothing "
                         "else is ever substituted")
        if wrong:
            problems.append({"item": position, "problems": wrong})
            continue
        out[approval_key(original, candidate)] = {
            "original_mpn": original, "candidate_mpn": candidate}
    if problems:
        raise MatchInputError(
            f"{len(problems)} approval(s) cannot be used; nothing was "
            "looked up", problems)
    return out
