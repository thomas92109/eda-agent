# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The purchasing workflow: a BOM, what the distributors offer, a plan.

Three read-only tools. ``part_purchasing_analyze`` takes the purchasing
BOM of the Altium project (or lines given directly) and asks DigiKey
and Mouser about each exact manufacturer part number.
``part_purchasing_plan`` turns that into a proposal: what to order, in
what quantity, from which distributor, at what price.
``part_purchasing_export_csv`` writes the Purchasing BOM, the configured
version of that proposal with a status and review reasons for every line,
to a CSV file in the export folder.

None of them touches a distributor account. No cart is made and no list
is touched; that is what part_cart_create and part_list_add are for (or
the prepare and commit tools), after a person has read the plan. The CSV
tool writes one file, in the export folder, and nothing else.
"""

from __future__ import annotations

from typing import Any, Optional, Union

__all__ = ["register_purchasing_tools"]


async def _analysis(build_quantity: Any, project_path: Optional[str],
                    variant: Optional[str], include_not_fitted: bool,
                    lines: Any, refresh: bool, limit: int,
                    distributors: Optional[tuple] = None,
                    refuse_incomplete: bool = False,
                    find_close_matches: bool = False,
                    approved_variants: Optional[dict] = None,
                    authorizer: Any = None) -> dict[str, Any]:
    """The enriched BOM, or an ``ok: False`` reply saying why not.

    ``distributors`` limits who is asked (both when omitted).
    ``refuse_incomplete`` refuses a cut-short Altium read before any
    distributor is asked. ``find_close_matches`` adds the labelled close
    part number candidates for lines no distributor has exactly;
    ``approved_variants`` are the user's approved original and candidate
    pairs (``purchasing_matching.parse_approvals``)."""
    from ..export.purchasing_sources import (
        BomInputError,
        enrich_bom,
        lines_from_input,
    )

    if isinstance(build_quantity, bool) or not isinstance(build_quantity, int) \
            or build_quantity < 1:
        return {"ok": False, "reason": (
            f"build_quantity {build_quantity!r} is not a whole number of "
            "boards, 1 or more")}

    if lines is not None:
        try:
            bom_lines = lines_from_input(lines)
        except BomInputError as exc:
            return {"ok": False, "reason": str(exc),
                    "problems": exc.problems}
        bom: dict[str, Any] = {
            "summary": {"line_items": len(bom_lines), "complete": True},
            "unresolved": [], "not_fitted": [], "not_in_bom": [],
            "inconsistencies": []}
        source = "lines"
    else:
        from ..bridge import get_bridge
        from .project import purchasing_bom_of_project

        bom = await purchasing_bom_of_project(
            get_bridge(), project_path, limit, variant,
            include_not_fitted=bool(include_not_fitted))
        if not isinstance(bom, dict) or bom.get("ok") is False \
                or not isinstance(bom.get("lines"), list):
            return {"ok": False, "reason": (
                "the project's purchasing BOM could not be read: "
                + str((bom or {}).get("reason") if isinstance(bom, dict)
                      else bom)), "bom_reply": bom}
        bom_lines = bom["lines"]
        source = "altium"
        read = bom.get("summary") or {}
        if refuse_incomplete and not read.get("complete", True):
            why = str(read.get("incomplete_reason")
                      or "no reason was given").strip().rstrip(".")
            return {"ok": False, "complete": False,
                    "incomplete_reason": read.get("incomplete_reason"),
                    "reason": (
                        f"Altium's read of the project is incomplete: {why}. "
                        "Nothing was looked up or written. Pass "
                        "allow_incomplete=True to go on anyway.")}

    extra: dict[str, Any] = {}
    if distributors:
        extra["distributors"] = tuple(distributors)
    if find_close_matches:
        extra["find_close_matches"] = True
        extra["approved_variants"] = approved_variants or {}
        extra["authorizer"] = authorizer
    found = enrich_bom(bom_lines, build_quantity, refresh=bool(refresh),
                       **extra)
    summary = bom.get("summary") or {}
    counts = {"matched_at_both": 0, "matched_at_one": 0, "matched_at_none": 0}
    for line in found["lines"]:
        matched = sum(1 for d in line["distributors"].values()
                      if d["status"] == "matched")
        counts[("matched_at_none", "matched_at_one",
                "matched_at_both")[min(matched, 2)]] += 1
    return {
        "ok": True,
        "source": source,
        "build_quantity": build_quantity,
        "bom_summary": summary,
        "bom_complete": bool(summary.get("complete", True)),
        "summary": {"bom_lines": len(found["lines"]), **counts,
                    "unresolved_components": len(bom.get("unresolved") or []),
                    "not_fitted_components": len(bom.get("not_fitted") or [])},
        "distributors": found["distributors"],
        "lines": found["lines"],
        "unresolved": bom.get("unresolved") or [],
        "not_fitted": bom.get("not_fitted") or [],
        "not_in_bom": bom.get("not_in_bom") or [],
        "inconsistencies": bom.get("inconsistencies") or [],
        **({"close_match_notes": found.get("close_match_notes") or [],
            "mpn_decisions": found.get("mpn_decisions")}
           if find_close_matches else {}),
    }


async def _identify_project(project_path: Optional[str], lines: Any):
    """The project a manual MPN decision belongs to, or None and why not.

    It is the canonical absolute path of the ``.PrjPcb`` file (the given
    path, or the focused project's), never the file name alone.
    """
    from ..export.purchasing_decisions import scope_for

    if lines is not None:
        return None, ("lines were given directly, so there is no project to "
                      "keep decisions for")
    if project_path:
        scope = scope_for(project_path)
        if scope is None:
            return None, (f"project_path {project_path!r} is not an absolute "
                          "path to the .PrjPcb file, so no project can be "
                          "identified")
        return scope, ""
    from ..bridge import get_bridge

    try:
        focused = await get_bridge().send_command_async("project.get_focused")
    except Exception as exc:  # noqa: BLE001 - the BOM read will say more
        return None, f"the focused project could not be read ({exc})"
    path = focused.get("project_path") if isinstance(focused, dict) else None
    scope = scope_for(path, str(focused.get("project_name") or "")) \
        if path else None
    return scope, ("" if scope else "Altium named no focused project with "
                   "an absolute path")


def _project_info(scope: Any, why: str) -> dict[str, Any]:
    if scope is None:
        return {"scoped": False, "why": why,
                "note": "manual decisions are neither read nor remembered; "
                        "global rules and approvals for this call still apply"}
    return {"scoped": True, "id": scope.id, "path": scope.path,
            "name": scope.name}


async def _pipeline(settings: Any, project_path: Optional[str],
                    variant: Optional[str], lines: Any, refresh: bool,
                    limit: int, allow_incomplete: bool = False,
                    include_dnp: bool = False,
                    approvals: Optional[dict] = None) -> dict[str, Any]:
    """Altium BOM, both distributors, the quantity engine and the review
    engine: the Purchasing BOM's lines. Reads only.

    ``settings`` is already resolved (``purchasing_config.resolve_settings``).
    Returns ``{"ok": True, "analysis", "built", "dnp_missing"}`` or an
    ``ok: False`` reply with nothing looked up past the first problem.
    """
    from ..export.purchasing_bom_export import dnp_rows
    from ..export.purchasing_lines import build_purchase_lines

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return {"ok": False, "reason": (
            f"limit {limit!r} is not a whole number, 1 or more")}
    if include_dnp and lines is not None:
        return {"ok": False, "reason": (
            "include_dnp needs the project's variant: it cannot be used "
            "with lines given directly")}
    from ..export.purchasing_decisions import (
        Authorizer,
        DecisionError,
        DecisionStore,
    )

    scope, why = await _identify_project(project_path, lines)
    decisions: list = []
    store_notes: list[str] = []
    if scope is not None:
        try:
            decisions = DecisionStore(scope).decisions
        except DecisionError as exc:
            store_notes.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - e.g. an unusable folder
            store_notes.append(f"the project's decisions could not be "
                               f"read ({exc}); none is applied")
    authorizer = Authorizer(
        decisions, approvals, settings.matching_auto_approve, scope,
        auto_approve_aliases=settings.matching_auto_approve_verified_aliases)
    analysis = await _analysis(
        settings.assemblies, project_path, variant, False, lines, refresh,
        limit, distributors=settings.enabled,
        refuse_incomplete=not allow_incomplete, find_close_matches=True,
        authorizer=authorizer)
    if not analysis.get("ok"):
        return analysis
    if scope is not None and not project_path:
        # The focused project must be the one that was read and decided for.
        again, _ = await _identify_project(None, None)
        if again is None or again.id != scope.id:
            return {"ok": False, "reason": (
                "the focused project changed while the BOM was being read, "
                "so the decisions applied may belong to another project. "
                "Nothing was written; run it again.")}
    analysis["close_match_notes"] = [*store_notes,
                                     *(analysis.get("close_match_notes") or [])]

    dnp_lines = dnp_missing = None
    if include_dnp and analysis.get("not_fitted"):
        from ..bridge import get_bridge
        from .project import purchasing_bom_of_project

        # The same variant, by the name the first read settled on, so a
        # variant switched in between cannot mix two builds.
        second = await purchasing_bom_of_project(
            get_bridge(), project_path, limit,
            (analysis.get("bom_summary") or {}).get("variant") or variant,
            include_not_fitted=True)
        if not isinstance(second, dict) or second.get("ok") is False \
                or not isinstance(second.get("lines"), list):
            return {"ok": False, "reason": (
                "the not-fitted parts could not be read: "
                + str(second.get("reason") if isinstance(second, dict)
                      else second))}
        dnp_lines, dnp_missing = dnp_rows(second, analysis["not_fitted"])
    built = build_purchase_lines(analysis, settings, dnp_lines)
    return {"ok": True, "analysis": analysis, "built": built,
            "dnp_missing": dnp_missing, "project": _project_info(scope, why)}


def register_purchasing_tools(mcp):
    """Register the purchasing analysis, planning and Purchasing BOM tools."""

    @mcp.tool()
    async def part_purchasing_analyze(
        build_quantity: int = 1,
        project_path: Optional[str] = None,
        variant: Optional[str] = None,
        include_not_fitted: bool = False,
        lines: Optional[list[dict[str, Any]]] = None,
        refresh: bool = False,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """The purchasing BOM with what DigiKey and Mouser offer per line.

        READS ONLY: no cart, no list, nothing ordered. It takes the
        purchasing BOM of the Altium project (proj_get_bom,
        mode="purchasing": fitted parts of the active variant, grouped
        by exact Manufacturer + Part Number) and asks both distributors
        about each line's manufacturer part number.

        A distributor's product is matched to a line only when its
        manufacturer part number is the line's exactly and its
        manufacturer is the line's. NO OTHER PART IS EVER SUBSTITUTED:
        a near number or another maker is reported, not used.

        Args:
            build_quantity: Boards to build; each line's quantity is
                multiplied by it.
            project_path: The project; the focused one when omitted.
            variant: A variant by name; the active one when omitted.
            include_not_fitted: Count the parts the variant does not
                fit as well. Off by default.
            lines: BOM lines given directly, in place of the project:
                [{manufacturer, part_number, quantity or designators,
                description}]. Altium is then not asked.
            refresh: Ask the distributors again instead of reusing
                replies kept for up to 4 hours.
            limit: Most components to read from the project.

        Returns:
            {"ok", "source", "build_quantity", "bom_complete",
            "summary", "distributors", "lines", "unresolved",
            "not_fitted", "not_in_bom", "inconsistencies"}. Each line
            has ``manufacturer``, ``part_number``, ``description``,
            ``designators``, ``quantity_per_board``,
            ``required_quantity`` and ``distributors``: for digikey and
            mouser a ``status`` (matched, not_found,
            manufacturer_mismatch, ambiguous, unavailable, error,
            not_checked) with a ``reason`` or, when matched,
            ``offers`` (``sku``, ``packaging``, ``stock``, ``moq``,
            ``multiple``, ``standard_package``, ``currency``,
            ``price_breaks``), ``lifecycle``, ``stock``, ``cached``
            and ``retrieved_at``. ``distributors`` at the top says how
            many answers were reused from the cache and why a
            distributor stopped answering. ``bom_complete`` false
            means the quantities are short: do not order from it.
        """
        return await _analysis(build_quantity, project_path, variant,
                               include_not_fitted, lines, refresh, limit)

    @mcp.tool()
    async def part_purchasing_plan(
        build_quantity: int = 1,
        prefer: str = "lowest",
        allow_overbuy: bool = True,
        project_path: Optional[str] = None,
        variant: Optional[str] = None,
        include_not_fitted: bool = False,
        lines: Optional[list[dict[str, Any]]] = None,
        refresh: bool = False,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """A purchasing proposal for the BOM: what to order, where.

        READS ONLY. It runs part_purchasing_analyze (same arguments)
        and proposes, for every BOM line, one distributor, one exact
        orderable number and a quantity. A PROPOSAL TO REVIEW: it
        makes no cart and no list, and a person decides.

        Each line goes whole to one distributor. The quantity is the
        requirement raised to the minimum and the multiple; a larger
        price break is taken when the whole line then costs less
        (``allow_overbuy``), and the line says what it saves and how
        many are left over. Each packaging is priced on its own terms
        (a Digi-Reel with its fee) and must be in stock for the whole
        quantity.

        Args:
            prefer: "lowest" (lowest extended price per line),
                "digikey" or "mouser" (that distributor where the line
                can be bought there; the other, marked as a fallback,
                where it cannot).
            allow_overbuy: Take a larger price break when it lowers
                the line's total.
            (others): As part_purchasing_analyze.

        Returns:
            {"ok", "complete", "summary", "lines",
            "unpurchasable_lines", "unresolved", "orders", "totals",
            "distributors"}. Every BOM line is in ``lines``:
            ``status`` planned, with ``choice`` (``distributor``,
            ``sku``, ``packaging``, ``order_quantity``,
            ``unit_price``, ``price_break_quantity``,
            ``extended_price``, ``currency``, ``surplus``,
            ``overbuy``, ``notes``), or unpurchasable, with
            ``problems``; ``alternatives`` holds what the other
            distributor offered. ``designators`` are kept whole on
            every line. ``orders`` lists the planned lines per
            distributor. ``totals`` has each distributor's subtotal
            and the combined component subtotal; shipping, taxes and
            fees are NOT included. ``complete`` false means some part
            of the BOM is not in the plan: say which.
        """
        from ..export.purchasing_plan import PREFERENCES, plan_purchases

        wanted = str(prefer or "").strip().lower()
        if wanted not in PREFERENCES:
            return {"ok": False, "reason": (
                f"no preference {prefer!r}; prefer is one of "
                + ", ".join(PREFERENCES))}
        analysis = await _analysis(build_quantity, project_path, variant,
                                   include_not_fitted, lines, refresh, limit)
        if not analysis.get("ok"):
            return analysis
        plan = plan_purchases(analysis, wanted, bool(allow_overbuy))
        return {
            "ok": True, "source": analysis["source"], **plan,
            "distributors": analysis["distributors"],
            "not_fitted": analysis["not_fitted"],
            "inconsistencies": analysis["inconsistencies"],
        }

    @mcp.tool()
    async def part_purchasing_mpn_decisions(
        action: str = "status",
        pairs: Optional[list[dict[str, str]]] = None,
        note: str = "",
        project_path: Optional[str] = None,
        variant: Optional[str] = None,
        limit: int = 1000,
        allow_incomplete: bool = False,
    ) -> dict[str, Any]:
        """Approve, reject, revoke or review close part number matches.

        A decision belongs to ONE Altium project, named by the canonical
        absolute path of its .PrjPcb file (never the file name alone), and is
        kept under the export folder, outside Git. It applies to that
        project's Purchasing BOM export, analysis, DigiKey list and Mouser
        cart tools without being asked again, and to no other project: the
        same part number in another project asks again. Only a global rule in
        config.toml ([matching] auto_approve: formatting, likely) applies to
        every project, and a project's rejection beats it there.

        A decision is bound to the component's manufacturer and part number
        and to the candidate's spelling and maker. If any changes it is
        STALE: reported, never applied. It authorizes which part number may
        be ordered, never a quantity, price or destination; carts still need
        their own approval. Altium is only read.

        Actions: ``status`` (default; reads the BOM, lists candidates, their
        authorization and stale decisions), ``list`` (the stored decisions
        only), ``approve`` or ``reject`` (record the ``pairs`` after
        checking each is a candidate found now), ``revoke`` (delete them).

        Args:
            action: status, list, approve, reject or revoke.
            pairs: [{original_mpn, candidate_mpn}] for approve, reject and
                revoke (the candidate spelled as the distributor lists it).
            note: A reason, kept with the decision.
            project_path: The project's absolute .PrjPcb path; the focused
                project when omitted.
            variant, limit, allow_incomplete: As the other purchasing tools.

        Returns:
            {"ok", "project", ...}: status gives ``close_matches`` and
            ``mpn_decisions`` (applied, stale, unused); approve and reject
            give ``recorded`` and ``refused``.
        """
        from ..export import purchasing_config as pc
        from ..export.purchasing_decisions import (
            Authorizer,
            DecisionError,
            DecisionStore,
            make_decision,
        )
        from ..export.purchasing_lines import close_match_table
        from ..export.purchasing_matching import (
            MatchInputError,
            alnum,
            approval_key,
            parse_approvals,
        )
        from ..export.purchasing_model import to_jsonable

        what = str(action or "").strip().lower()
        if what not in ("status", "list", "approve", "reject", "revoke"):
            return {"ok": False, "reason": (
                f"no action {action!r}; it is status, list, approve, reject "
                "or revoke")}
        wanted: dict = {}
        if what in ("approve", "reject", "revoke"):
            try:
                wanted = parse_approvals(pairs)
            except MatchInputError as exc:
                return {"ok": False, "reason": str(exc),
                        "problems": exc.problems}
            if not wanted:
                return {"ok": False, "reason": (
                    f"{what} needs pairs: [{{original_mpn, candidate_mpn}}]")}
        scope, why = await _identify_project(project_path, None)
        if scope is None:
            return {"ok": False, "reason": (
                "decisions belong to one project and none can be identified: "
                f"{why}. Pass project_path (the absolute .PrjPcb path) or "
                "focus the project in Altium.")}
        try:
            store = DecisionStore(scope)
        except DecisionError as exc:
            return {"ok": False, "reason": str(exc)}
        project = _project_info(scope, "")

        if what == "list":
            return {"ok": True, "project": project, "file": str(store.path),
                    "decisions": to_jsonable(store.decisions)}
        if what == "revoke":
            drop = [d["id"] for d in store.decisions
                    if (alnum(d["original_mpn"]),
                        "".join(d["candidate_mpn"].split()).casefold())
                    in wanted]
            if not drop:
                return {"ok": False, "project": project, "reason": (
                    "no stored decision of this project matches those pairs")}
            store.remove(drop)
            store.save()
            return {"ok": True, "project": project, "revoked": len(drop)}

        try:
            settings = pc.resolve_settings(None)
        except pc.SettingError as exc:
            return {"ok": False, "reason": str(exc)}
        authorizer = Authorizer(
            store.decisions, {}, settings.matching_auto_approve, scope,
            auto_approve_aliases=(
                settings.matching_auto_approve_verified_aliases))
        analysis = await _analysis(
            settings.assemblies, project_path, variant, False, None, False,
            limit, distributors=settings.enabled,
            refuse_incomplete=not allow_incomplete, find_close_matches=True,
            authorizer=authorizer)
        if not analysis.get("ok"):
            return analysis
        if not project_path:
            again, _ = await _identify_project(None, None)
            if again is None or again.id != scope.id:
                return {"ok": False, "reason": (
                    "the focused project changed while the BOM was read; "
                    "nothing was recorded")}
        if what == "status":
            return {"ok": True, "project": project,
                    "close_matches": to_jsonable(close_match_table(analysis)),
                    "mpn_decisions": to_jsonable(analysis["mpn_decisions"]),
                    "warnings": analysis["close_match_notes"]}

        found: dict = {}
        for entry in analysis["lines"]:
            for cand in entry.get("close_matches") or []:
                key = approval_key(cand["original_mpn"], cand["candidate_mpn"])
                if key in wanted:
                    found.setdefault(key, []).append((entry, cand))
        recorded, refused = [], []
        kind = "approved" if what == "approve" else "rejected"
        for key, given in wanted.items():
            if key not in found:
                refused.append({**given, "why": (
                    "not among the close candidates found for this project "
                    "now (the original number matches exactly, or no "
                    "distributor lists that candidate)")})
                continue
            entry, cand = found[key][0]
            decision = make_decision(kind, entry, cand, note)
            decision["distributors"] = sorted({c["distributor"]
                                               for _, c in found[key]})
            replaced = store.put(decision)
            recorded.append({
                **{k: decision[k] for k in (
                    "id", "decision", "original_mpn", "candidate_mpn",
                    "label", "decided_at")},
                "replaced": (replaced or {}).get("decision")})
        if recorded:
            store.save()
        return {"ok": bool(recorded), "project": project, "recorded": recorded,
                "refused": refused,
                "reason": None if recorded else "nothing was recorded",
                "note": ("Applies to this project only, and only while the "
                         "original number and the candidate stay the same. "
                         "It authorizes no quantity, price or destination.")}

    @mcp.tool()
    async def part_purchasing_export_csv(
        output_path: str = "",
        assemblies: Optional[int] = None,
        columns: Optional[list[str]] = None,
        prefer: Optional[str] = None,
        include_dnp: bool = False,
        project_path: Optional[str] = None,
        variant: Optional[str] = None,
        lines: Optional[list[dict[str, Any]]] = None,
        refresh: bool = False,
        limit: int = 1000,
        allow_incomplete: bool = False,
        approve_mpn_variants: Optional[list[dict[str, str]]] = None,
        overage_factor: Optional[Union[str, int, float]] = None,
        overage_max_unit_price: Optional[Union[str, int, float]] = None,
        overage_currency: Optional[str] = None,
        extra_spend_percent: Optional[Union[str, int, float]] = None,
        extra_spend_flat: Optional[Union[str, int, float]] = None,
        extra_spend_currency: Optional[str] = None,
        price_break_window: Optional[int] = None,
        large_surplus_ratio: Optional[Union[str, int, float]] = None,
    ) -> dict[str, Any]:
        """Write the PURCHASING BOM to a CSV file: what to buy, where, why.

        Not the Standard BOM (proj_export_purchasing_bom_csv: one assembly,
        no distributor). Multiplies by the assembly count, asks DigiKey and
        Mouser about each exact manufacturer part number, chooses
        quantities, writes one row per BOM line. READS ONLY: no cart, list
        or Altium change. The reply has counts, never the BOM.

        Every fitted line and unresolved component is a row. A line that
        cannot be bought (no match, no stock, no price, end of life,
        obsolete, discontinued) is a ``blocked`` row with every reason;
        nothing is dropped, split or substituted.

        Quantity: Required Qty is BOM quantity times ``assemblies``;
        Purchase Qty is never below it, is raised to the minimum and
        multiple, gets overage on cheap parts, and may go to a higher
        price break: always when that costs LESS, else when it costs at most
        ``baseline * (1 + extra_spend_percent / 100) + extra_spend_flat``
        among the next ``price_break_window`` feasible breaks. No multiple
        limit; stock and packaging are never exceeded. ``prefer`` (digikey
        default) gets a line it can supply, else the other; neither: blocked.

        CLOSE MPNs: a line no distributor has exactly gets candidates (same
        letters and digits with other punctuation, or a suffix), labelled
        Formatting, Likely, Possible or Uncertain Match with evidence and
        doubts, listed together in ``close_matches`` and a companion CSV.
        Advice only: none is used until ``approve_mpn_variants`` names that
        exact original_mpn and candidate_mpn. The BOM's number is unchanged.

        Settings come from config.toml in the export folder (created on
        first use) and the argument of the same name overrides each. The
        file goes to its ``bom`` folder, NEVER overwritten; a repository
        path is refused.

        Args:
            output_path: A file to write; empty picks the default name.
            assemblies: Boards to build (default 2).
            columns: Ordered identifiers (part_number, matched_mpn,
                purchase_qty, extended_cost, review_reasons...; unknown is
                refused); unset: the configured set.
            prefer: digikey, mouser or lowest.
            include_dnp: Also list not-fitted parts (not bought).
            project_path, variant, lines, refresh, limit: As the other
                purchasing tools.
            allow_incomplete: Go on after a cut-short read (rows blocked).
            approve_mpn_variants: [{original_mpn, candidate_mpn}].
            (others): The quantity settings above.

        Returns:
            {"ok", "output_path", "rows", "summary", "counts", "totals",
            "settings", "close_matches", "close_matches_file",
            "warnings"}, or {"ok": False, "reason"}. Totals are per
            currency, without shipping, taxes or fees.
        """
        import os

        from ..export import purchasing_config as pc
        from ..export.destination import ExportPathError, claimed, export_file
        from ..export.purchasing_bom_csv import ENCODING, ColumnSelectionError
        from ..export.purchasing_bom_export import (
            build_close_matches_csv,
            build_purchasing_bom_csv,
            resolve_columns,
        )
        from ..export.purchasing_matching import (
            MatchInputError,
            parse_approvals,
        )
        from ..export.purchasing_model import to_jsonable

        try:
            approvals = parse_approvals(approve_mpn_variants)
        except MatchInputError as exc:
            return {"ok": False, "reason": str(exc),
                    "problems": exc.problems}
        try:
            settings = pc.resolve_settings({
                "assemblies": assemblies, "prefer": prefer,
                "overage_factor": overage_factor,
                "overage_max_unit_price": overage_max_unit_price,
                "overage_currency": overage_currency,
                "extra_spend_percent": extra_spend_percent,
                "extra_spend_flat": extra_spend_flat,
                "extra_spend_currency": extra_spend_currency,
                "price_break_window": price_break_window,
                "large_surplus_ratio": large_surplus_ratio})
        except pc.SettingError as exc:
            return {"ok": False, "problems": exc.problems,
                    "reason": "settings refused: " + "; ".join(exc.problems)}
        default_columns = settings.purchasing_columns
        notes: list[str] = []
        try:
            resolve_columns(columns, default_columns, bool(include_dnp))
        except ColumnSelectionError as exc:
            if columns is None and default_columns is not None:
                notes.append(f"{exc}; the built-in columns are used")
                default_columns = None
                try:
                    resolve_columns(None, None, bool(include_dnp))
                except ColumnSelectionError as again:
                    return {"ok": False, "reason": str(again)}
            else:
                return {"ok": False, "reason": str(exc)}

        piped = await _pipeline(settings, project_path, variant, lines,
                                refresh, limit, allow_incomplete,
                                include_dnp, approvals)
        if not piped.get("ok"):
            return piped
        analysis, built = piped["analysis"], piped["built"]
        result = build_purchasing_bom_csv(
            built["lines"], columns, default_columns, bool(include_dnp),
            analysis.get("bom_summary") if analysis["source"] == "altium"
            else None)
        if not result["rows"]:
            return {"ok": False, "reason": (
                "the Purchasing BOM has no rows. Nothing was written."),
                "summary": built["summary"], "warnings": built["warnings"]}

        warnings = [*notes, *built["warnings"], *result["warnings"]]
        if analysis.get("not_in_bom"):
            warnings.append(
                f"{len(analysis['not_in_bom'])} fitted component(s) are kept "
                "out of the BOM by their Component Kind (Altium's Not in BOM "
                "setting) and are not in this CSV")
        missing = piped.get("dnp_missing") or {}
        for key, text in (("not_in_bom", "are also kept out of the BOM by "
                           "their Component Kind"),
                          ("unresolved", "have no usable Manufacturer or "
                           "Part Number"),
                          ("unclassified", "are in none of Altium's lists")):
            if missing.get(key):
                warnings.append(
                    f"{len(missing[key])} not-fitted component(s) {text}, so "
                    "they have no DNP row")
        complete = bool(analysis.get("bom_complete", True))
        if not complete:
            warnings.append(
                "INCOMPLETE: Altium's read was cut short. Quantities may be "
                "short; do not order from this file")

        stem = ""
        if analysis["source"] == "lines":
            stem = "purchasing"
        elif project_path:
            stem = os.path.splitext(os.path.basename(str(project_path)))[0]
        else:
            from ..bridge import get_bridge
            focused = await get_bridge().send_command_async(
                "project.get_focused")
            if isinstance(focused, dict):
                stem = os.path.splitext(
                    str(focused.get("project_name") or ""))[0]
        stem = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in stem.strip()) or "project"
        name = f"{stem}_purchase_BOM_{settings.assemblies}x.csv"
        wanted = os.path.basename(output_path) if output_path else name
        try:
            out = export_file(output_path, "bom", name)
        except ExportPathError as exc:
            return {"ok": False, "reason": str(exc)}
        except OSError as exc:
            return {"ok": False,
                    "reason": f"could not create the file: {exc}"}
        try:
            with claimed([out]):
                out.write_bytes(result["text"].encode(ENCODING))
        except OSError as exc:
            return {"ok": False, "reason": (
                f"could not write {out}: {exc}. The partial file was "
                "removed.")}
        if out.name != wanted:
            warnings.append(
                f"{wanted} already exists, so this export was written as "
                f"{out.name}; nothing was overwritten")
        close = built["close_matches"]
        companion = None
        if close:
            extra_csv = build_close_matches_csv(close)
            try:
                path2 = export_file(
                    str(out.parent / f"{out.stem}_close_matches.csv"), "bom",
                    f"{out.stem}_close_matches.csv")
                with claimed([path2]):
                    path2.write_bytes(extra_csv["text"].encode(ENCODING))
                companion = os.path.abspath(path2)
            except (ExportPathError, OSError) as exc:
                warnings.append(
                    f"the close part number candidates are in this reply "
                    f"but their file could not be written: {exc}")
            waiting = sum(1 for c in close if not c.get("used"))
            if waiting:
                warnings.append(
                    f"{waiting} close part number candidate(s) are listed "
                    "for review and NONE is used: approve one with "
                    "approve_mpn_variants after checking it")
        by_label: dict[str, int] = {}
        for c in close:
            by_label[c["label"]] = by_label.get(c["label"], 0) + 1
        return {
            "ok": True, "output_path": os.path.abspath(out),
            "close_matches": to_jsonable(close),
            "close_match_counts": by_label,
            "close_matches_file": companion,
            "project": piped.get("project"),
            "mpn_decisions": to_jsonable(analysis.get("mpn_decisions")),
            "rows": result["rows"], "columns": result["columns"],
            "headers": result["headers"], "assemblies": settings.assemblies,
            "variant": (analysis.get("bom_summary") or {}).get("variant"),
            "include_dnp": bool(include_dnp), "complete": complete,
            "summary": built["summary"], "counts": result["counts"],
            "totals": built["totals"], "settings": built["settings"],
            "distributors": analysis["distributors"], "warnings": warnings,
        }
