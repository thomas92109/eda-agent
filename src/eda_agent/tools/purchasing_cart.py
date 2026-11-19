# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Put the Purchasing BOM into a DigiKey list or a Mouser cart, with approval.

Two tools, in two steps a person is part of:

* ``part_purchasing_cart_prepare`` READS ONLY. It builds the Purchasing BOM
  from fresh distributor figures (never the cache) and returns a proposal:
  the items that would go in, the lines that would not and why, and a
  ``proposal_id`` that fingerprints every price, quantity, SKU, packaging,
  currency, lifecycle status and designator.
* ``part_purchasing_cart_commit`` WRITES to one distributor account, only
  with ``approved=True`` and the ``proposal_id`` of a proposal the user has
  read. It rebuilds the proposal from fresh figures first and writes
  nothing if the id no longer matches.

Neither places an order or checks out. The existing ``part_cart_*`` and
``part_list_*`` tools stay as they are; the commit uses their clients.
"""

from __future__ import annotations

from typing import Any, Optional, Union

__all__ = ["register_purchasing_cart_tools"]

_Number = Optional[Union[str, int, float]]


async def _prepare(distributor: str, cart_key: str, list_id: str,
                   create_new: bool, list_name: str,
                   include_lines: Optional[list[str]],
                   project_path: Optional[str], variant: Optional[str],
                   lines: Any, limit: int, allow_incomplete: bool,
                   setting_args: dict[str, Any],
                   approve_mpn_variants: Any = None) -> dict[str, Any]:
    from ..export import purchasing_config as pc
    from ..export.purchasing_cart import (
        CartError,
        build_proposal,
        normalize_target,
    )
    from ..export.purchasing_matching import MatchInputError, parse_approvals
    from .purchasing import _pipeline

    try:
        approvals = parse_approvals(approve_mpn_variants)
    except MatchInputError as exc:
        return {"ok": False, "reason": str(exc), "problems": exc.problems}
    try:
        target = normalize_target(distributor, cart_key, list_id, create_new,
                                  list_name)
        settings = pc.resolve_settings(setting_args)
    except CartError as exc:
        return {"ok": False, "reason": str(exc)}
    except pc.SettingError as exc:
        return {"ok": False, "problems": exc.problems,
                "reason": "settings refused: " + "; ".join(exc.problems)}
    # Fresh figures, always: what is approved is what the distributor says
    # now, not what it said hours ago.
    piped = await _pipeline(settings, project_path, variant, lines, True,
                            limit, allow_incomplete, False, approvals)
    if not piped.get("ok"):
        return piped
    name = str(distributor).strip().lower()
    proposal = build_proposal(piped["built"]["lines"], name, target,
                              include_lines or ())
    return {"ok": True, "proposal": proposal, "built": piped["built"],
            "analysis": piped["analysis"], "settings": settings,
            "project": piped.get("project")}


def register_purchasing_cart_tools(mcp):
    """Register the cart and list handoff tools."""

    @mcp.tool()
    async def part_purchasing_cart_prepare(
        distributor: str = "digikey",
        cart_key: str = "",
        list_id: str = "",
        create_new: bool = False,
        list_name: str = "",
        include_lines: Optional[list[str]] = None,
        project_path: Optional[str] = None,
        variant: Optional[str] = None,
        lines: Optional[list[dict[str, Any]]] = None,
        limit: int = 1000,
        allow_incomplete: bool = False,
        approve_mpn_variants: Optional[list[dict[str, str]]] = None,
        assemblies: Optional[int] = None,
        prefer: Optional[str] = None,
        overage_factor: _Number = None,
        overage_max_unit_price: _Number = None,
        overage_currency: Optional[str] = None,
        extra_spend_percent: _Number = None,
        extra_spend_flat: _Number = None,
        extra_spend_currency: Optional[str] = None,
        price_break_window: Optional[int] = None,
        large_surplus_ratio: _Number = None,
    ) -> dict[str, Any]:
        """PREPARE a DigiKey list or Mouser cart from the Purchasing BOM.

        READS ONLY: no cart, list or account is touched. It builds the
        Purchasing BOM (same rules and settings as
        part_purchasing_export_csv) from FRESH distributor figures, and
        returns the proposal a person approves before
        part_purchasing_cart_commit writes anything.

        Which lines go in: a line whose purchase is at ``distributor`` and
        whose status is ``ready``. A ``review`` line goes in only when its
        line id is in ``include_lines``. A ``blocked`` line (unresolved
        component, no match, out of stock, no price, end of life, obsolete,
        discontinued) NEVER goes in, even if named, and no part is ever
        substituted. Every excluded line is returned with why. To buy
        everything at one distributor, set ``prefer`` to it.

        Where it goes: Mouser needs ``cart_key`` (an existing cart) or
        ``create_new=True``; DigiKey needs ``list_id`` or
        ``create_new=True`` with ``list_name``. DigiKey has saved lists,
        not carts: open the list in the account to make a cart. Needs the
        distributors' credentials for the live reads.

        Args:
            distributor: digikey or mouser.
            cart_key, list_id, create_new, list_name: The target.
            include_lines: Line ids (L003...) of review lines to add.
            approve_mpn_variants: [{original_mpn, candidate_mpn}] the user
                approved after reading the close candidates. Only these are
                used; the BOM's number is unchanged.
            project_path, variant, lines, limit, allow_incomplete: As
                part_purchasing_export_csv.
            (others): The quantity settings, as part_purchasing_export_csv.

        Returns:
            {"ok", "proposal_id", "proposal": {"items", "excluded",
            "summary", "target"}, "bom_summary", "settings", "warnings",
            "next"}. Each item has the SKU, quantity, packaging, unit price,
            currency, lifecycle, designators and status. Show the user
            the items, the excluded lines and the totals (shipping, taxes
            and fees are not included), and commit only after they agree.
        """
        from ..export.purchasing_model import to_jsonable

        out = await _prepare(
            distributor, cart_key, list_id, create_new, list_name,
            include_lines, project_path, variant, lines, limit,
            allow_incomplete, {
                "assemblies": assemblies, "prefer": prefer,
                "overage_factor": overage_factor,
                "overage_max_unit_price": overage_max_unit_price,
                "overage_currency": overage_currency,
                "extra_spend_percent": extra_spend_percent,
                "extra_spend_flat": extra_spend_flat,
                "extra_spend_currency": extra_spend_currency,
                "price_break_window": price_break_window,
                "large_surplus_ratio": large_surplus_ratio},
            approve_mpn_variants)
        if not out.get("ok"):
            return out
        proposal, built = out["proposal"], out["built"]
        return {
            "ok": True, "proposal_id": proposal["proposal_id"],
            "proposal": to_jsonable(proposal),
            "close_matches": to_jsonable(built["close_matches"]),
            "project": out.get("project"),
            "mpn_decisions": to_jsonable(
                out["analysis"].get("mpn_decisions")),
            "bom_summary": built["summary"], "settings": built["settings"],
            "warnings": [*built["warnings"], *proposal["warnings"]],
            "next": ("Show the user the items, the excluded lines and the "
                     "totals. Only after they approve, call "
                     "part_purchasing_cart_commit with this proposal_id, "
                     "the same target and include_lines, and approved=True."),
        }

    @mcp.tool()
    async def part_purchasing_cart_commit(
        proposal_id: str,
        approved: bool = False,
        distributor: str = "digikey",
        cart_key: str = "",
        list_id: str = "",
        create_new: bool = False,
        list_name: str = "",
        include_lines: Optional[list[str]] = None,
        cart_state_checked: bool = False,
        project_path: Optional[str] = None,
        variant: Optional[str] = None,
        lines: Optional[list[dict[str, Any]]] = None,
        limit: int = 1000,
        allow_incomplete: bool = False,
        approve_mpn_variants: Optional[list[dict[str, str]]] = None,
        assemblies: Optional[int] = None,
        prefer: Optional[str] = None,
        overage_factor: _Number = None,
        overage_max_unit_price: _Number = None,
        overage_currency: Optional[str] = None,
        extra_spend_percent: _Number = None,
        extra_spend_flat: _Number = None,
        extra_spend_currency: Optional[str] = None,
        price_break_window: Optional[int] = None,
        large_surplus_ratio: _Number = None,
    ) -> dict[str, Any]:
        """COMMIT an approved proposal to a DigiKey list or Mouser cart.

        WRITES TO ONE DISTRIBUTOR ACCOUNT, and only after the user has read
        the proposal from part_purchasing_cart_prepare and said yes. Set
        ``approved=True`` only then, never on your own judgment. It places
        no order and checks out nothing: no tool in this server can.

        Every argument, ``approve_mpn_variants`` included, must be the
        one given to prepare. The proposal is
        rebuilt from fresh figures first; if ANY price, currency, lifecycle
        status, SKU, packaging, quantity or designator differs, the id no
        longer matches, NOTHING is written, and the new proposal is
        returned for the user to read again.

        Mouser inserts ADD to a line, so this works to TARGET TOTALS: it
        reads the cart, adds only what is missing, and never lowers or
        removes a quantity. Committing the same proposal again after a
        timeout is therefore safe. What was meant and what was seen is kept
        in a journal in the export folder.

        A cart that reads back empty, or lower than the journal says it
        held, is an UNKNOWN state (a Mouser cart has read back empty after
        being opened in a browser): nothing is written until the user has
        looked at the cart and you pass ``cart_state_checked=True``.
        A cart creation that may have happened is never repeated: the
        user finds the cart and a new proposal names its ``cart_key``.

        Every item is read back from the distributor and reported
        ``verified`` or not. A DigiKey line already in the list with
        another quantity is reported and left alone.

        Returns:
            {"ok", "distributor", "cart_key" or "list_id", "created",
            "items", "summary", "verified", "journal", "proposal_id"}, or
            {"ok": False, "reason"} with nothing written (or, when
            ``outcome`` is "unknown", with what happened unknown: read the
            cart or list before anything else).
        """
        from ..export import purchasing_cart as cart
        from ..export.destination import ExportPathError
        from ..export.purchasing_model import to_jsonable

        if approved is not True:
            return {"ok": False, "reason": (
                "approved must be True, and only after the user has read "
                "the proposal from part_purchasing_cart_prepare and "
                "agreed. Nothing was written.")}
        if not str(proposal_id or "").strip():
            return {"ok": False, "reason": (
                "proposal_id is required: the id from "
                "part_purchasing_cart_prepare. Nothing was written.")}
        out = await _prepare(
            distributor, cart_key, list_id, create_new, list_name,
            include_lines, project_path, variant, lines, limit,
            allow_incomplete, {
                "assemblies": assemblies, "prefer": prefer,
                "overage_factor": overage_factor,
                "overage_max_unit_price": overage_max_unit_price,
                "overage_currency": overage_currency,
                "extra_spend_currency": extra_spend_currency,
                "extra_spend_percent": extra_spend_percent,
                "extra_spend_flat": extra_spend_flat,
                "price_break_window": price_break_window,
                "large_surplus_ratio": large_surplus_ratio},
            approve_mpn_variants)
        if not out.get("ok"):
            return out
        proposal = out["proposal"]
        if proposal["proposal_id"] != str(proposal_id).strip():
            return {
                "ok": False, "changed": True,
                "reason": (
                    "the proposal is not the one that was approved: a "
                    "price, currency, lifecycle status, SKU, packaging, "
                    "quantity or designator moved, or an argument differs "
                    "from prepare's. Nothing was written. The new proposal "
                    "is below; the user must read it and approve its "
                    "proposal_id."),
                "new_proposal_id": proposal["proposal_id"],
                "proposal": to_jsonable(proposal)}
        name = proposal["distributor"]
        try:
            journal = cart.Journal(proposal)
        except ExportPathError as exc:
            return {"ok": False, "reason": str(exc)}
        client = cart.make_client(name)
        run = cart.commit_mouser if name == "mouser" else cart.commit_digikey
        result = run(proposal, client, journal, bool(cart_state_checked))
        result["proposal_id"] = proposal["proposal_id"]
        return to_jsonable(result)
