# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The purchasing workflow: a BOM, what the distributors offer, a plan.

Two read-only tools. ``part_purchasing_analyze`` takes the purchasing
BOM of the Altium project (or lines given directly) and asks DigiKey
and Mouser about each exact manufacturer part number.
``part_purchasing_plan`` turns that into a proposal: what to order, in
what quantity, from which distributor, at what price.

Neither writes anything. No cart is made and no list is touched; that
is what part_cart_create and part_list_add are for, by hand, after a
person has read the plan.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["register_purchasing_tools"]


async def _analysis(build_quantity: Any, project_path: Optional[str],
                    variant: Optional[str], include_not_fitted: bool,
                    lines: Any, refresh: bool, limit: int) -> dict[str, Any]:
    """The enriched BOM, or an ``ok: False`` reply saying why not."""
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

    found = enrich_bom(bom_lines, build_quantity, refresh=bool(refresh))
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
    }


def register_purchasing_tools(mcp):
    """Register the purchasing analysis and planning tools."""

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
