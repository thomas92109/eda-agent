# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Who authorized a close part number match, and for which project.

A close candidate (``purchasing_matching``) is used only when it is
authorized. There are three ways, from the most specific to the most general:

* a MANUAL decision, approve or reject, which belongs to ONE Altium project.
  It is kept locally under the export folder, outside Git, in a file named by
  the project's identifier. The identifier is a hash of the canonical,
  absolute ``.PrjPcb`` path, never of the file name alone: two projects both
  called ``Board.PrjPcb`` in different folders are two projects. A manual
  decision never applies to another project, and a store file that names
  another project is refused.
* an approval given for one CALL only (``approve_mpn_variants``): nothing is
  remembered.
* a GLOBAL rule from ``config.toml`` (``[matching] auto_approve``), which
  applies to every project: only ``formatting`` and ``likely`` can be named,
  and only for a candidate whose manufacturer name is identical. A rule holds
  no part number and no project.
* the independent option ``[matching] auto_approve_verified_aliases``, which
  accepts a VERIFIED ALIAS (``purchasing_aliases``: numbers the manufacturer
  documents as the same part) under the BOM's own manufacturer, and only when
  it is the one alias listed at that distributor. Off unless the file says so.

A project's manual rejection beats everything, the global rule and the alias
option included, for that project only. A manual decision is bound to what it was made about: the
component's original manufacturer and part number, and the candidate's
spelling and manufacturer. If any of them is no longer the same the decision
is STALE: it is reported and not applied, never silently. These decisions
authorize one thing only (which part number may be looked up and ordered).
They are not part of any purchasing plan or cart fingerprint and authorize
no quantity, price or destination.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from eda_agent.atomicfile import replace_with_retry

from . import purchasing_matching as pm
from .destination import working_file

__all__ = [
    "Authorizer",
    "DecisionError",
    "DecisionStore",
    "GLOBAL_RULES",
    "ProjectScope",
    "canonical_project_path",
    "scope_for",
]

#: What a global rule in config.toml may name, and the label it authorizes.
GLOBAL_RULES = {"formatting": pm.FORMATTING, "likely": pm.LIKELY}
_BY_LABEL = {label: key for key, label in GLOBAL_RULES.items()}


class DecisionError(ValueError):
    """A decision or a store that cannot be used. Nothing was changed."""


# ---- the project -------------------------------------------------------------

@dataclass(frozen=True)
class ProjectScope:
    id: str      # 16 hex characters, from the canonical project path
    path: str    # that path, for the audit trail
    name: str


def canonical_project_path(path: Any) -> Optional[tuple[str, str]]:
    """``(display, key)`` for an absolute project path, or None.

    Symbolic links and ``..`` are resolved and, on Windows, case and slash
    direction are folded, so the same file always gives the same key. A path
    that is not absolute names no project: a bare file name would be the
    same for two different projects.
    """
    text = str(path or "").strip()
    if not text or not os.path.isabs(text):
        return None
    real = os.path.realpath(text)
    return real, os.path.normcase(real).replace("\\", "/")


def scope_for(path: Any, name: str = "") -> Optional[ProjectScope]:
    found = canonical_project_path(path)
    if found is None:
        return None
    display, key = found
    return ProjectScope(
        id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
        path=display, name=name or os.path.basename(display))


# ---- keys ----------------------------------------------------------------------

def _mk(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _pk(text: Any) -> str:
    return "".join(str(text or "").split()).casefold()


def decision_id(maker: Any, original: Any, candidate: Any) -> str:
    raw = f"{_mk(maker)}|{_pk(original)}|{_pk(candidate)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- the store -----------------------------------------------------------------------

class DecisionStore:
    """The manual decisions of ONE project, in one file under the export
    folder: ``purchasing/mpn_decisions/<project id>.json``."""

    def __init__(self, scope: ProjectScope, create: bool = False):
        self.scope = scope
        self.path: Path = working_file(
            "purchasing/mpn_decisions", f"{scope.id}.json", create=create)
        self.data: dict[str, Any] = {
            "version": 1, "project_id": scope.id,
            "project_path": scope.path, "project_name": scope.name,
            "decisions": []}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DecisionError(
                    f"the decisions file {self.path.name} cannot be read "
                    f"({exc}); no decision is applied") from None
            if not isinstance(loaded, dict) \
                    or loaded.get("project_id") != scope.id \
                    or loaded.get("version") != 1 \
                    or not isinstance(loaded.get("decisions"), list):
                raise DecisionError(
                    f"the decisions file {self.path.name} does not belong to "
                    f"this project (it names project "
                    f"{loaded.get('project_id') if isinstance(loaded, dict) else '?'}"
                    f", this is {scope.id}); it was NOT used, so no decision "
                    "from it applies here")
            self.data = loaded
            self.data["project_path"] = scope.path
            self.data["project_name"] = scope.name

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return list(self.data["decisions"])

    def put(self, decision: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Add a decision, replacing one about the same component and
        candidate. Returns the replaced one."""
        previous = None
        kept = []
        for old in self.data["decisions"]:
            if old["id"] == decision["id"]:
                previous = old
            else:
                kept.append(old)
        kept.append(decision)
        self.data["decisions"] = kept
        return previous

    def remove(self, ids: Iterable[str]) -> int:
        wanted = set(ids)
        before = len(self.data["decisions"])
        self.data["decisions"] = [d for d in self.data["decisions"]
                                  if d["id"] not in wanted]
        return before - len(self.data["decisions"])

    def save(self) -> None:
        target = working_file("purchasing/mpn_decisions",
                              f"{self.scope.id}.json", create=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        replace_with_retry(tmp, target)


def make_decision(kind: str, entry: dict[str, Any], cand: dict[str, Any],
                  note: str = "") -> dict[str, Any]:
    """A manual decision about ``cand`` for the BOM line ``entry``, with the
    identity it is bound to."""
    return {
        "id": decision_id(entry["manufacturer"], entry["part_number"],
                          cand["candidate_mpn"]),
        "decision": kind, "method": "manual",
        "original_manufacturer": entry["manufacturer"],
        "original_mpn": entry["part_number"],
        "candidate_mpn": cand["candidate_mpn"],
        "candidate_manufacturer": cand["manufacturer"],
        "label": cand["label"], "kind": cand["kind"],
        "difference": cand["difference"],
        "designators": list(entry.get("designators") or []),
        "distributors": sorted({cand["distributor"]}),
        "decided_at": _now(), "note": " ".join(str(note or "").split()),
    }


# ---- deciding ----------------------------------------------------------------------------

class Authorizer:
    """Decides, for each close candidate, whether it is authorized.

    Order: the project's manual rejection, the project's manual approval, an
    approval given for this call, a global rule. A manual decision that is
    stale (see the module text) is reported and skipped, and the later
    authorizations are then tried on their own merits.
    """

    def __init__(self, decisions: Iterable[dict[str, Any]] = (),
                 call_approvals: Optional[dict] = None,
                 auto_approve: Iterable[str] = (),
                 scope: Optional[ProjectScope] = None,
                 auto_approve_aliases: bool = False):
        self.decisions = list(decisions)
        self.call_approvals = dict(call_approvals or {})
        self.auto = tuple(k for k in auto_approve if k in GLOBAL_RULES)
        self.auto_aliases = bool(auto_approve_aliases)
        self.scope = scope
        self._by_key = {(_mk(d["original_manufacturer"]),
                         _pk(d["original_mpn"]), _pk(d["candidate_mpn"])): d
                        for d in self.decisions}
        self._origin: dict[str, tuple[str, str]] = {}
        self._touched: dict[str, str] = {}      # id -> "valid" | stale reason
        self._searched: set[tuple[str, str]] = set()
        self._exact: set[tuple[str, str]] = set()

    # -- before the lookups --------------------------------------------------
    def start(self, bom_lines: Iterable[dict[str, Any]]) -> None:
        """Compare each decision's original with the BOM as it is now."""
        lines = list(bom_lines)
        current = {(_mk(l.get("manufacturer")), _pk(l.get("part_number")))
                   for l in lines}
        by_designator: dict[str, tuple[str, str]] = {}
        for line in lines:
            for name in line.get("designators") or []:
                by_designator[str(name)] = (
                    str(line.get("manufacturer") or ""),
                    str(line.get("part_number") or ""))
        for d in self.decisions:
            key = (_mk(d["original_manufacturer"]), _pk(d["original_mpn"]))
            if key in current:
                self._origin[d["id"]] = ("current", "")
                continue
            moved = [by_designator[n] for n in d.get("designators") or []
                     if n in by_designator]
            if moved:
                maker, mpn = moved[0]
                self._origin[d["id"]] = (
                    "changed",
                    f"the component's manufacturer or part number changed: "
                    f"the BOM now has {maker!r} {mpn!r} where "
                    f"{d['original_manufacturer']!r} {d['original_mpn']!r} "
                    "was decided")
            else:
                self._origin[d["id"]] = (
                    "missing", "no line of this BOM has that manufacturer "
                    "and part number any more")

    # -- one candidate -------------------------------------------------------
    def judge(self, entry: dict[str, Any], cand: dict[str, Any]
              ) -> dict[str, Any]:
        key = (_mk(entry["manufacturer"]), _pk(entry["part_number"]),
               _pk(cand["candidate_mpn"]))
        d = self._by_key.get(key)
        if d is not None and self._origin.get(d["id"], ("", ""))[0] \
                == "current":
            if pm._maker_key(cand["manufacturer"]) \
                    != pm._maker_key(d["candidate_manufacturer"]):
                self._touched[d["id"]] = (
                    f"the proposed match is no longer the same: "
                    f"{cand['candidate_mpn']!r} is now listed by "
                    f"{cand['manufacturer']!r}, it was decided as "
                    f"{d['candidate_manufacturer']!r}")
            else:
                self._touched[d["id"]] = "valid"
                return {
                    "status": ("rejected" if d["decision"] == "rejected"
                               else "approved"),
                    "method": ("project rejection"
                               if d["decision"] == "rejected"
                               else "project approval"),
                    "decision_id": d["id"], "decided_at": d["decided_at"],
                    "note": d.get("note", ""),
                    "project_id": self.scope.id if self.scope else None}
        pair = pm.approval_key(cand["original_mpn"], cand["candidate_mpn"])
        if pair in self.call_approvals:
            return {"status": "approved", "method": "this call only",
                    "decision_id": None, "decided_at": _now(), "note": ""}
        alias = cand.get("alias") or {}
        if self.auto_aliases and cand.get("label") == pm.VERIFIED_ALIAS \
                and alias.get("verified") \
                and cand.get("manufacturer_match") == "exact" \
                and not cand.get("manufacturer_override") \
                and not cand.get("alias_ambiguous"):
            # Only a manufacturer-documented alias, under the BOM's own
            # manufacturer, and the only such alias at that distributor. A
            # project's rejection or manual decision was handled above.
            return {"status": "approved",
                    "method": "verified manufacturer alias (automatic)",
                    "source": "config.toml [matching] "
                              "auto_approve_verified_aliases = true",
                    "decision_id": None, "decided_at": None,
                    "alias_rule": alias.get("rule"),
                    "evidence": list(alias.get("evidence") or []),
                    "note": "accepted without a manual approval because the "
                            "manufacturer documents these numbers as the "
                            "same part"}
        rule = _BY_LABEL.get(cand.get("label"))
        if rule in self.auto and cand.get("manufacturer_match") == "exact" \
                and not cand.get("manufacturer_override"):
            return {"status": "approved",
                    "method": f"global rule: {cand['label']}",
                    "decision_id": None, "decided_at": None,
                    "note": f"[matching] auto_approve names {rule!r} in "
                            "config.toml"}
        return {"status": "none", "method": "", "decision_id": None,
                "decided_at": None, "note": ""}

    def note_line(self, entry: dict[str, Any], searched: bool) -> None:
        key = (_mk(entry["manufacturer"]), _pk(entry["part_number"]))
        if searched:
            self._searched.add(key)
        elif any(d.get("status") == "matched" and not d.get("mpn_variant")
                 for d in entry["distributors"].values()):
            self._exact.add(key)

    # -- after ---------------------------------------------------------------
    def finalize(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "project": ({"id": self.scope.id, "path": self.scope.path,
                         "name": self.scope.name} if self.scope else None),
            "global_rules": list(self.auto),
            "applied": [], "stale": [], "unused": [], "unchecked": [],
            "warnings": []}
        for d in self.decisions:
            key = (_mk(d["original_manufacturer"]), _pk(d["original_mpn"]))
            state, why = self._origin.get(d["id"], ("missing", ""))
            brief = {"id": d["id"], "decision": d["decision"],
                     "original_mpn": d["original_mpn"],
                     "candidate_mpn": d["candidate_mpn"],
                     "decided_at": d["decided_at"]}
            if state == "changed":
                reason = why
            elif state == "missing":
                report["unused"].append({**brief, "reason": why})
                continue
            elif key in self._exact:
                report["unused"].append({
                    **brief, "reason": "the original number now matches "
                    "exactly, so no variant is needed"})
                continue
            elif key not in self._searched:
                report["unchecked"].append({
                    **brief, "reason": "the close search did not run for "
                    "this line (a distributor could not answer)"})
                continue
            else:
                touched = self._touched.get(d["id"])
                if touched == "valid":
                    report["applied"].append(brief)
                    continue
                reason = touched or (
                    "the proposed match is no longer the same: "
                    f"{d['candidate_mpn']!r} is no longer listed by any "
                    "distributor")
            report["stale"].append({**brief, "reason": reason})
            report["warnings"].append(
                f"the {d['decision']} of {d['original_mpn']!r} to "
                f"{d['candidate_mpn']!r} is STALE and was not applied: "
                f"{reason}. Decide again, or revoke it")
        return report
