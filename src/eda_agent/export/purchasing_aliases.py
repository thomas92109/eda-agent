# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Ordering numbers a MANUFACTURER documents as the same part.

Some makers sell one part under several orderable numbers that differ only in
a code they define (a KEMET packaging C-Spec, for example). A distributor then
lists one of them and the BOM names another. This module knows which pairs a
manufacturer's own document says are the same part, and nothing else.

Two kinds of knowledge, both needing evidence:

* a FAMILY RULE: a manufacturer's documented part number structure, with a
  table of the ordering codes that document defines. Two numbers are aliases
  only when they have the same family part (every character before the code),
  the family is one the document describes, BOTH codes are listed in the
  document's table, each code exists for the part's size, and both codes are of
  the same documented grade. A code that is not in the table is not an alias of
  anything, however similar it looks.
* an EXPLICIT ALIAS: a pair (or set) of numbers a manufacturer document names
  as equivalent.

There is no generic suffix substitution here. A pattern is never trusted on its
own, and an entry without evidence (document, revision, location, quotation)
is refused by ``registry_problems``. To add a pair, read the maker's document
and put what it says in an ``Evidence``.

Nothing in this module reads a file, calls a distributor or approves
anything. An alias is advice with evidence. Whether it is used is decided by
``purchasing_decisions``: a project's rejection beats everything, a project's
manual approval applies, and the ``[matching] auto_approve_verified_aliases``
option of ``config.toml`` accepts a verified alias without being asked again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

__all__ = [
    "Alias",
    "EXPLICIT_ALIASES",
    "Evidence",
    "ExplicitAlias",
    "FamilyRule",
    "KEMET_COMMERCIAL_C0G",
    "OrderingCode",
    "RULES",
    "alias_numbers",
    "find_alias",
    "is_registered_pair",
    "normalize",
    "registry_problems",
]


def normalize(mpn: Any) -> str:
    """A part number as compared here: no spaces, upper case. Punctuation is
    kept, because a number with other punctuation is a different string."""
    return "".join(str(mpn or "").split()).upper()


def _words(text: Any) -> set[str]:
    return set(re.sub(r"[^0-9a-z]+", " ", str(text or "").casefold()).split())


# ---- evidence ------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """What a manufacturer document says, and where."""
    document: str
    revision: str
    url: str
    location: str
    quote: str

    def text(self) -> str:
        return (f"{self.document} ({self.revision}), {self.location}: "
                f"\"{self.quote}\" <{self.url}>")

    def complete(self) -> bool:
        return all(str(v).strip() for v in (
            self.document, self.revision, self.url, self.location,
            self.quote))


@dataclass(frozen=True)
class OrderingCode:
    """One ordering code a maker's document defines."""
    code: str
    meaning: str
    grade: str                          # codes of one grade may be aliases
    sizes: Optional[frozenset]          # case sizes it exists for; None: all
    evidence: Evidence


@dataclass(frozen=True)
class FamilyRule:
    """A manufacturer's documented number structure and its code table."""
    id: str
    maker_words: frozenset              # a word of the maker's name
    summary: str
    pattern: "re.Pattern[str]"          # groups: family, size, code
    codes: Mapping[str, OrderingCode]
    structure: Evidence


@dataclass(frozen=True)
class ExplicitAlias:
    """Numbers a manufacturer document names as the same part."""
    id: str
    maker_words: frozenset
    numbers: tuple
    meaning: str
    evidence: Evidence


@dataclass(frozen=True)
class Alias:
    """A verified alias: the BOM's number and the one to look up."""
    rule: str
    method: str
    original_mpn: str
    alias_mpn: str
    difference: str
    meaning: str
    summary: str
    evidence: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule, "method": self.method, "verified": True,
            "original_mpn": self.original_mpn, "alias_mpn": self.alias_mpn,
            "difference": self.difference, "meaning": self.meaning,
            "summary": self.summary,
            "evidence": [e.text() for e in self.evidence]}


# ---- KEMET commercial C0G, from KEMET's own datasheet ----------------------------

_KEMET_DOC = ("KEMET Surface Mount Multilayer Ceramic Chip Capacitors "
              "(SMD MLCCs), C0G Dielectric, 10 - 250 VDC (Commercial Grade)")
_KEMET_REV = "C1003_C0G 2/20/2025"
_KEMET_URL = "https://yageogroup.com/content/datasheet/asset/file/KEM_C1003_C0G_SMD"

_SMALL = frozenset({"0201", "0402", "0603"})
_LARGE = frozenset({"0805", "1206", "1210", "1805", "1808", "1812", "1825",
                    "2220", "2225"})
_GRADE = "commercial, unmarked, tape and reel"


def _kemet(location: str, quote: str) -> Evidence:
    return Evidence(_KEMET_DOC, _KEMET_REV, _KEMET_URL, location, quote)


#: Codes read from the table "Packaging C-Spec Ordering Options Table" on page
#: 2 of the datasheet above. Only codes that table lists are here. 7867 is NOT:
#: that datasheet does not mention it, and it is added only with a KEMET
#: document that does (the C-Spec Reference Guide, for example).
KEMET_COMMERCIAL_C0G = FamilyRule(
    id="kemet-commercial-c0g-cspec",
    maker_words=frozenset({"kemet"}),
    summary=("KEMET commercial C0G SMD MLCC: characters 1-14 name the part, "
             "characters 15 on are the packaging C-Spec"),
    pattern=re.compile(
        r"^(?P<family>C(?P<size>0201|0402|0603|0805|1206|1210|1805|1808|1812"
        r"|1825|2220|2225)C\d{3}[BCDFGJKM][843512A]GAC)(?P<code>[A-Z0-9]*)$"),
    codes={
        "TU": OrderingCode(
            "TU", "7 inch reel, unmarked", _GRADE, None,
            _kemet("page 2, Packaging C-Spec Ordering Options Table",
                   "7\" Reel/Unmarked TU")),
        "7411": OrderingCode(
            "7411", "13 inch reel, unmarked (EIA 0603 and smaller)", _GRADE,
            _SMALL,
            _kemet("page 2, Packaging C-Spec Ordering Options Table",
                   "13\" Reel/Unmarked 7411 (EIA 0603 and smaller case "
                   "sizes)")),
        "7210": OrderingCode(
            "7210", "13 inch reel, unmarked (EIA 0805 and larger)", _GRADE,
            _LARGE,
            _kemet("page 2, Packaging C-Spec Ordering Options Table",
                   "13\" Reel/Unmarked 7210 (EIA 0805 and larger case "
                   "sizes)")),
    },
    structure=_kemet(
        "page 1 Ordering Information; page 11 note 1",
        "The \"Packaging C-Spec\" is a 4 to 8 digit code which identifies "
        "the packaging type and/or product grade. When ordering, the proper "
        "code must be included in the 15th through 22nd character positions "
        "of the ordering code."),
)

#: The family rules and explicit aliases in force. Tests replace these.
RULES: tuple = (KEMET_COMMERCIAL_C0G,)
EXPLICIT_ALIASES: tuple = ()


# ---- looking up ---------------------------------------------------------------------

def _parse(rule: FamilyRule, mpn: str):
    found = rule.pattern.match(normalize(mpn))
    return None if found is None else found.groupdict()


def _in_scope(maker_words: frozenset, maker: Any) -> bool:
    return bool(maker_words & _words(maker))


def _family_alias(rule: FamilyRule, maker: Any, original: str, other: str
                  ) -> Optional[Alias]:
    if not _in_scope(rule.maker_words, maker):
        return None
    a, b = _parse(rule, original), _parse(rule, other)
    if a is None or b is None or a["family"] != b["family"]:
        return None
    ca, cb = rule.codes.get(a["code"]), rule.codes.get(b["code"])
    if ca is None or cb is None or ca.code == cb.code \
            or ca.grade != cb.grade:
        return None
    size = a["size"]
    if any(c.sizes is not None and size not in c.sizes for c in (ca, cb)):
        return None
    return Alias(
        rule=rule.id, method="manufacturer ordering-code rule",
        original_mpn=str(original), alias_mpn=str(other),
        difference=f"ordering code {ca.code} -> {cb.code}",
        meaning=(f"the same part, a different documented packaging: "
                 f"{ca.meaning} ({ca.code}) and {cb.meaning} ({cb.code}); "
                 "the distributor's own SKU, minimum, packaging, stock and "
                 "price breaks are used for the matched number"),
        summary=rule.summary,
        evidence=(rule.structure, ca.evidence, cb.evidence))


def _explicit_alias(item: ExplicitAlias, maker: Any, original: str,
                    other: str) -> Optional[Alias]:
    if not _in_scope(item.maker_words, maker):
        return None
    numbers = {normalize(n) for n in item.numbers}
    a, b = normalize(original), normalize(other)
    if a == b or a not in numbers or b not in numbers:
        return None
    return Alias(
        rule=item.id, method="manufacturer alias",
        original_mpn=str(original), alias_mpn=str(other),
        difference=f"{original} -> {other}", meaning=item.meaning,
        summary="a pair the manufacturer's document names as equivalent",
        evidence=(item.evidence,))


def find_alias(maker: Any, original: Any, candidate: Any
               ) -> Optional[Alias]:
    """The verified alias that makes ``candidate`` the BOM's ``original`` for
    ``maker``, or None. A number that is merely similar is None."""
    original, candidate = str(original or ""), str(candidate or "")
    if not original or not candidate \
            or normalize(original) == normalize(candidate):
        return None
    for rule in RULES:
        found = _family_alias(rule, maker, original, candidate)
        if found is not None:
            return found
    for item in EXPLICIT_ALIASES:
        found = _explicit_alias(item, maker, original, candidate)
        if found is not None:
            return found
    return None


def alias_numbers(maker: Any, original: Any) -> list[Alias]:
    """Every number the registry says is an alias of the BOM's ``original``,
    for a distributor to be asked about by that exact number."""
    original = str(original or "")
    out: list[Alias] = []
    seen: set[str] = set()

    def add(found: Optional[Alias]) -> None:
        if found is not None and normalize(found.alias_mpn) not in seen:
            seen.add(normalize(found.alias_mpn))
            out.append(found)

    for rule in RULES:
        parsed = _parse(rule, original)
        if parsed is None or not _in_scope(rule.maker_words, maker):
            continue
        for code in rule.codes:
            if code == parsed["code"]:
                continue
            add(_family_alias(rule, maker, original,
                              parsed["family"] + code))
    for item in EXPLICIT_ALIASES:
        for number in item.numbers:
            add(_explicit_alias(item, maker, original, number))
    return out


def is_registered_pair(original: Any, candidate: Any) -> bool:
    """Whether the registry links two numbers for ANY maker of its entries.

    A syntax gate for an approval request only: whether a distributor really
    lists the candidate, and under which maker, is checked against the BOM.
    """
    for rule in RULES:
        for maker in rule.maker_words:
            if _family_alias(rule, maker, str(original), str(candidate)):
                return True
    for item in EXPLICIT_ALIASES:
        for maker in item.maker_words:
            if _explicit_alias(item, maker, str(original), str(candidate)):
                return True
    return False


# ---- keeping the registry honest --------------------------------------------------------

def registry_problems(rules: Optional[tuple] = None,
                      explicit: Optional[tuple] = None) -> list[str]:
    """What is wrong with the registry: every entry needs complete evidence."""
    rules = RULES if rules is None else rules
    explicit = EXPLICIT_ALIASES if explicit is None else explicit
    out: list[str] = []
    for rule in rules:
        if not rule.maker_words:
            out.append(f"{rule.id}: no maker")
        if not rule.structure.complete():
            out.append(f"{rule.id}: the structure has incomplete evidence")
        if len(rule.codes) < 2:
            out.append(f"{rule.id}: fewer than two codes, so no alias")
        for code, entry in rule.codes.items():
            if entry.code != code:
                out.append(f"{rule.id}: code key {code!r} names "
                           f"{entry.code!r}")
            if not entry.evidence.complete():
                out.append(f"{rule.id}: code {code} has incomplete evidence")
            elif code.upper() not in entry.evidence.quote.upper():
                out.append(f"{rule.id}: the quotation for {code} does not "
                           "contain the code")
    for item in explicit:
        if not item.maker_words:
            out.append(f"{item.id}: no maker")
        if len({normalize(n) for n in item.numbers}) < 2:
            out.append(f"{item.id}: fewer than two numbers")
        if not item.evidence.complete():
            out.append(f"{item.id}: incomplete evidence")
    return out
