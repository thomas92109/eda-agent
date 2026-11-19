# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Provider-neutral part search and fetch.

Two tools rather than one per source, so adding a provider never grows
the tool surface (see the tool-count concern in issue #10), and so no
single source occupies a privileged name.
"""

from __future__ import annotations

from typing import Any

__all__ = ["register_parts_tools"]



#: Above this many characters the provider's detail is written to the
#: workspace and the reply carries its outline. An EasyEDA part's raw shapes
#: came to 89 KB, which Claude Code set aside in a file under the user's
#: profile and showed the path of.
_DETAIL_INLINE_CHARS = 20_000


def _outline(value: Any, depth: int = 0) -> Any:
    """``value`` with its short scalars kept and everything bulky counted."""
    if isinstance(value, dict):
        if depth >= 2:
            return f"<{len(value)} keys>"
        return {k: _outline(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return f"<{len(value)} entries>"
    if isinstance(value, str) and len(value) > 300:
        return f"<text, {len(value)} characters>"
    return value


def _fit_detail(detail: Any, provider: str, part_id: str) -> dict[str, Any]:
    """Nothing when ``detail`` fits one reply; else its outline in place of
    it, and the whole of it written to the workspace (``detail_path``)."""
    import json

    from ..config import get_config

    text = json.dumps(detail, default=str)
    if len(text) <= _DETAIL_INLINE_CHARS:
        return {}
    folder = get_config().workspace_dir / "parts"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{provider}_{part_id}")
    path = folder / f"{safe}.json"
    path.write_text(text, encoding="utf-8")
    return {"detail": _outline(detail), "detail_trimmed": len(text),
            "detail_path": str(path),
            "detail_note": ("the provider's full detail is in detail_path; "
                            "summary holds what a part choice needs")}

def register_parts_tools(mcp):
    """Register the provider-neutral part tools with the MCP server."""

    @mcp.tool()
    async def part_search(
        query: str = "",
        limit_per_provider: int = 20,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Search EVERY enabled part provider and merge the results.

        All providers count equally; hits are ordered by provider then
        part, which is NOT relevance. A provider that cannot answer says
        why ("unavailable" is not "no such part"). Each hit has a
        ``kind``: "library" gives geometry (symbol or footprint),
        "catalogue" gives identity (MPN, datasheet, stock) and no
        geometry. Remote providers need the internet; local ones still
        answer without it. An empty query lists the providers.

        A digikey or mouser hit also has ``sourcing``: ``stock``,
        ``manufacturer_stock``, ``lifecycle`` (``status`` active, new,
        preliminary, nrnd, last_time_buy, end_of_life, obsolete or
        discontinued), ``lead_time``, ``product_url``, ``retrieved_at``
        and ``offers``, one per orderable number: ``distributor_pn``,
        ``packaging``, ``stock``, ``moq``, ``multiple``, ``currency``
        and ``price_breaks`` ({quantity, unit_price}). null means the
        distributor did not say: a null status is not "active".
        ``freshness`` says how current the figures are; a search can
        lag. digikey and mouser replies are reused from a local cache
        for up to 4 hours (``sourcing.cache`` and ``retrieved_at`` then
        say when they were read); ``refresh=True`` asks the
        distributor again. Search by MPN, keyword or
        distributor number. Two products sharing an MPN (two makers)
        get distinct refs, by distributor number instead of MPN.

        DATASHEET DISCIPLINE: a hit is a lead. Blank ``provenance`` or
        ``license`` means unknown; audit an imported footprint with
        lib_audit_footprint_vs_datasheet before trusting it.

        Args:
            query: Substring of part name, manufacturer or keywords.
            limit_per_provider: Cap per provider.

        Returns:
            {"ok", "count", "providers": {name: status}, "hits",
            "by_mpn"} (the same part across providers; wildcard part
            numbers are not merged), or {"ok", "providers"} for an empty
            query.
        """
        from eda_agent.libimport.providers import (
            available_providers,
            search_all,
        )

        if not str(query).strip():
            return {
                "ok": True,
                "providers": [
                    {
                        "name": p.name,
                        "description": p.description,
                        # "library" = geometry you can place.
                        # "catalogue" = identity and a datasheet, with
                        # nothing to import.
                        "kind": getattr(p, "kind", "library"),
                        # False = the endpoint was probed live but the
                        # request and response shapes have never been
                        # exercised with a real credential. Stated, not
                        # assumed.
                        "verified_live": bool(
                            getattr(p, "verified_live", True)),
                        "formats": list(getattr(p, "formats", ())),
                        # Which EDA tool can actually consume a fetch.
                        # There is an EasyEDA->Altium converter here but
                        # NO KiCad->Altium path, so a kicad-only source
                        # is a dead end for an Altium user and should
                        # say so before they spend time on it.
                        "usable_in": list(getattr(p, "usable_in", ())),
                    }
                    for p in available_providers()
                ],
                "note": ("No provider is a default; part_search queries "
                         "all of them equally. Select a subset with "
                         "EDA_AGENT_PART_PROVIDERS."),
            }

        from eda_agent.libimport.providers import _cache

        with _cache.fresh(refresh):
            result = search_all(query, limit_per_provider)
        result["ok"] = True
        if not result["count"]:
            # Distinguish "everything answered and found nothing" from
            # "nothing could answer", which look identical otherwise.
            reachable = [n for n, s in result["providers"].items()
                         if s.get("ok")]
            result["note"] = (
                f"no matches from {len(reachable)} reachable provider(s)"
                if reachable else
                "NO provider could answer; this is not evidence the part "
                "does not exist")
        return result

    @mcp.tool()
    async def part_fetch(
        part_id: str,
        provider: str = "",
        download_dir: str = "",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch one part's detail, by the ``ref`` a search returned.

        The ``ref`` is ``provider:part_id``; pass it through. A bare id
        with no ``provider`` and no known prefix is refused, never
        guessed; ``provider`` wins over a prefix.

        For digikey and mouser the id is an MPN or a ``distributor_pn``
        from ``sourcing.offers``, and ``detail.sourcing`` holds the
        stock, lifecycle and price breaks (see part_search), read live
        or reused from the cache for up to 4 hours: ``retrieved_at``
        says when, and ``refresh=True`` reads live. An MPN
        that names several products there is never resolved to one:
        the reply is {"ok": False, "ambiguous": True, "candidates"},
        each candidate with its ``ref``, manufacturer and distributor
        numbers. Fetch the candidate's ``ref``.

        ``download_dir`` also writes the provider's library files there.
        A downloaded KiCad file in a newer format than the local KiCad
        reads comes back with a ``*_warning`` entry.

        Returns the provider's own detail plus a normalised summary
        (mpn, manufacturer, datasheet, provenance, license) where the
        provider supplies one, and ``files`` when a download was asked
        for.
        """
        from eda_agent.libimport.providers import (
            AmbiguousPart,
            ProviderError,
            ProviderUnavailable,
            available_providers,
            get_provider,
        )

        # Unpack a "provider:part_id" ref. Split on the FIRST colon and
        # only when the prefix names a REGISTERED provider: part ids
        # legitimately contain colons ("Device:R",
        # "Lib.SchLib::Comp"), so an unconditional split would corrupt
        # them. An explicit provider argument wins.
        if not provider:
            prefix, sep, rest = (part_id or "").partition(":")
            if sep and rest:
                known = {p.name for p in available_providers()}
                if prefix in known:
                    provider, part_id = prefix, rest

        if not provider:
            return {
                "ok": False,
                "reason": (
                    f"cannot tell which source {part_id!r} came from. Pass "
                    "the ref from part_search (it looks like "
                    "'provider:part_id'), or name the provider. This is "
                    "not guessed: picking a source would make one of them "
                    "the silent default for every fetch."
                ),
            }

        try:
            source = get_provider(provider)
        except ProviderError as exc:
            return {"ok": False, "reason": str(exc)}

        from eda_agent.libimport.providers import _cache

        try:
            with _cache.fresh(refresh):
                detail = source.fetch(part_id)
        except ProviderUnavailable as exc:
            return {"ok": False, "unavailable": str(exc),
                    "provider": source.name}
        except AmbiguousPart as exc:
            return {"ok": False, "ambiguous": True, "reason": str(exc),
                    "provider": source.name, "part_id": part_id,
                    "candidates": exc.candidates}
        except ProviderError as exc:
            return {"ok": False, "reason": str(exc), "provider": source.name}

        summary = None
        describe = getattr(source, "describe", None)
        if callable(describe):
            try:
                summary = describe(part_id).to_dict()
            except Exception:  # noqa: BLE001 - detail already succeeded
                summary = None

        result: dict[str, Any] = {
            "ok": True,
            "provider": source.name,
            "part_id": part_id,
            "summary": summary,
            "detail": detail,
        }
        result.update(_fit_detail(detail, source.name, part_id))

        if download_dir:
            download = getattr(source, "download", None)
            if not callable(download):
                result["files"] = {}
                result["download_note"] = (
                    f"{source.name} offers no downloadable files")
            else:
                try:
                    result["files"] = download(part_id, download_dir)
                except (ProviderError, OSError) as exc:
                    # The detail already succeeded; report the download
                    # failure without discarding what did work.
                    result["files"] = {}
                    result["download_error"] = str(exc)
        return result

    def _lists(provider: str):
        """The list client for ``provider``, or the reply refusing it."""
        if str(provider or "").strip().lower() != "digikey":
            return None, {"ok": False, "reason": (
                f"no saved-list support for {provider!r}; only digikey "
                "(MyLists) has it")}
        from eda_agent.libimport.providers.digikey_lists import DigiKeyLists

        return DigiKeyLists(), None

    def _list_call(provider: str, work) -> dict[str, Any]:
        """Run ``work`` on the list client and word its failures."""
        from eda_agent.libimport.providers import (
            ProviderError,
            ProviderUnavailable,
        )
        from eda_agent.libimport.providers.digikey_lists import (
            ListInputError,
        )

        lists, refused = _lists(provider)
        if refused:
            return refused
        try:
            return {"ok": True, "provider": lists.name, **work(lists)}
        except ListInputError as exc:
            out = {"ok": False, "reason": str(exc), "provider": lists.name}
            if exc.problems:
                out["problems"] = exc.problems
            return out
        except ProviderUnavailable as exc:
            return {"ok": False, "unavailable": str(exc),
                    "provider": lists.name}
        except ProviderError as exc:
            return {"ok": False, "reason": str(exc), "provider": lists.name}
        except OSError as exc:
            return {"ok": False, "provider": lists.name, "reason": (
                f"the sign-in file could not be read or written: {exc}")}

    @mcp.tool()
    async def part_list_login(
        returned_url: str = "",
        provider: str = "digikey",
    ) -> dict[str, Any]:
        """Sign in to a distributor account so its saved lists can be used.

        DigiKey MyLists works only for a signed-in user (three-legged
        OAuth), once per 90 days. Two steps:

        1. Call with no ``returned_url``: the reply has ``signed_in``
           and, when not signed in, ``authorize_url``. The user opens it
           in a browser, signs in and allows access.
        2. The browser ends on the app's redirect address
           (https://localhost unless DIGIKEY_REDIRECT_URI names another),
           usually a page that cannot load. Call again with that page's
           whole address as ``returned_url``. The code in it lasts ONE
           MINUTE and works once.

        If a minute is too short through a chat, the user can run
        ``python -m eda_agent.libimport.providers.digikey_lists login``
        in a terminal instead.

        The tokens are saved in the user's profile (DIGIKEY_TOKEN_FILE
        moves the file) and are never returned. Needs DIGIKEY_CLIENT_ID
        and DIGIKEY_CLIENT_SECRET for an app subscribed to the MyLists
        API with that redirect address registered.

        Returns:
            {"ok", "signed_in", "sign_in_lasts_days"} and, to start a
            sign-in, "authorize_url", "redirect_uri" and "next".
        """
        def work(lists):
            if str(returned_url or "").strip():
                return lists.finish_login(returned_url)
            status = lists.status()
            if status.get("signed_in"):
                return status
            return {**status, **lists.authorize_url(), "next": (
                "open authorize_url, sign in, then call part_list_login "
                "with returned_url set to the address the browser ended "
                "on, within one minute")}

        return _list_call(provider, work)

    @mcp.tool()
    async def part_list_create(
        name: str,
        visibility: str = "private",
        provider: str = "digikey",
    ) -> dict[str, Any]:
        """Create an empty saved parts list in the distributor account.

        WRITES TO THE USER'S DIGIKEY ACCOUNT: create a list only when
        the user asked for it. It is a list, not an order: nothing here
        touches a cart or buys anything.

        The list is private unless ``visibility`` is "readonly" or
        "readwrite" (shared with co-workers of the account). It is set
        to keep part numbers, quantities and references as entered.

        Returns:
            {"ok", "list_id", "name", "visibility", ...} as DigiKey
            stored it; ``differences`` when that is not what was asked.
            Needs part_list_login first.
        """
        return _list_call(
            provider, lambda lists: lists.create_list(name, visibility))

    @mcp.tool()
    async def part_list_add(
        list_id: str,
        items: list[dict[str, Any]],
        check_products: bool = True,
        provider: str = "digikey",
    ) -> dict[str, Any]:
        """Add parts to a saved list, then read it back and compare.

        WRITES TO THE USER'S DIGIKEY ACCOUNT. Adds to a list; orders
        nothing.

        Each item: ``sku`` (the exact DigiKey part number, a
        ``distributor_pn`` from part_search; it picks the packaging),
        ``quantity`` (whole, 1 or more) and ``designators`` (a list, or
        a string taken as written). The designators, all of them, comma
        separated, become the part's ReferenceDesignator and, as far as
        they fit, its CustomerReference (DigiKey prints it on the bag
        label); ``customer_reference`` or ``reference_designator`` set
        one of them otherwise. ``description`` is the component's
        Description parameter, kept in the part's notes;
        ``manufacturer`` is optional. No other field is accepted: a
        component's Comment is never sent.

        DigiKey keeps 48 characters of a CustomerReference and drops
        the rest. So the designators go into it joined by commas with
        no spaces (``R101,R102``): all of them when that fits in 48,
        otherwise as many whole ones as fit before a closing
        ``,+MORE`` (``+MORE`` alone when not even the first fits),
        never part of one. The item then has
        ``customer_reference_omitted`` (the designators left out) and
        a ``customer_reference_warning``. Tell the user when that
        happens. The ReferenceDesignator is sent whole and as given,
        and ``designators`` in the result is that full text.

        A ``customer_reference`` that is itself a designator list is
        fitted the same way. Any other wording is sent as it is, and
        refused, with nothing added, when it is over 48 characters:
        it is not shortened here.

        Refused whole, with nothing added, when an item is malformed or
        its ``sku`` is not a DigiKey part number (``check_products``
        asks the catalogue, one read per distinct sku; a manufacturer
        number is not accepted for a sku).

        Returns:
            {"ok", "list_id", "sent", "added", "verified", "items",
            "list_total"}. Each item has what was sent, ``unique_id``,
            ``digikey_description``, ``differences`` (empty when the
            list holds exactly what was sent) and ``verified``.
            ``sku_verified`` is true only when the packaging DigiKey
            selected in the list has the ``sku`` that was sent;
            ``sku_unverified`` says why when DigiKey named no selected
            packaging. ``failed`` and ``not_sent`` when DigiKey refused
            part of it.
        """
        return _list_call(provider, lambda lists: lists.add_items(
            list_id, items, check_products=check_products))

    @mcp.tool()
    async def part_list_get(
        list_id: str = "",
        provider: str = "digikey",
    ) -> dict[str, Any]:
        """Read a saved parts list, or list the account's lists.

        With no ``list_id``: {"ok", "lists": [{list_id, name,
        visibility, total_parts, ...}], "count", "complete"}. Every
        list is read, page by page. ``complete`` false, with
        ``incomplete`` saying why, means there may be more lists than
        were returned: do not conclude from it that a list is absent.

        With one: the list and its ``items``, each with ``sku`` (the
        orderable DigiKey number of the packaging selected in the list;
        empty when DigiKey named none), ``base_part_number`` (DigiKey's
        number for the product, often the reel whatever the packaging:
        not the number to order by), ``pack_type``,
        ``quantity``, ``customer_reference`` and
        ``reference_designator`` exactly as stored, ``notes`` (where
        part_list_add keeps the component's Description),
        ``digikey_description``, ``manufacturer``,
        ``manufacturer_part_number``, ``matched`` (false: DigiKey found
        no product for the number), ``status`` and
        ``quantity_available``. Reads only. Needs part_list_login first.
        """
        def work(lists):
            if str(list_id or "").strip():
                return lists.get_list(list_id)
            return lists.all_lists()

        return _list_call(provider, work)

    def _cart_call(provider: str, work) -> dict[str, Any]:
        """Run ``work`` on the cart client and word its failures."""
        from eda_agent.libimport.providers import (
            ProviderError,
            ProviderUnavailable,
        )
        from eda_agent.libimport.providers.mouser_cart import (
            CartInputError,
            MouserCart,
        )

        if str(provider or "").strip().lower() != "mouser":
            return {"ok": False, "reason": (
                f"no cart support for {provider!r}; only mouser has it. "
                "DigiKey has saved lists instead: part_list_create")}
        try:
            return {"ok": True, "provider": "mouser", **work(MouserCart())}
        except CartInputError as exc:
            out = {"ok": False, "reason": str(exc), "provider": "mouser"}
            if exc.problems:
                out["problems"] = exc.problems
            return out
        except ProviderUnavailable as exc:
            return {"ok": False, "unavailable": str(exc),
                    "provider": "mouser"}
        except ProviderError as exc:
            return {"ok": False, "reason": str(exc), "provider": "mouser"}

    @mcp.tool()
    async def part_cart_create(
        items: list[dict[str, Any]],
        check_products: bool = True,
        provider: str = "mouser",
    ) -> dict[str, Any]:
        """Create a NEW Mouser cart holding the given parts.

        WRITES TO MOUSER: makes a cart only when the user asked for
        one. It is a cart, not an order: nothing here checks out, pays
        or submits, and no tool in this server can.

        Each item: ``sku`` (the exact Mouser part number, a
        ``distributor_pn`` from part_search; a manufacturer number is
        refused) and ``quantity`` (whole, 1 or more; never rounded
        here). Optional: ``customer_part_number`` (at most 21
        characters, no ``*``; refused when longer, not shortened) and
        ``packaging`` (None, Cut_Tape, MouseReel or FullReel).

        Mouser's cart has NO field for reference designators, a
        description or a note. ``designators``, ``description``,
        ``manufacturer`` and ``manufacturer_part_number`` are accepted
        and returned with each item, and are not sent. When given, the
        last two are compared with the part Mouser put in the cart.

        Refused whole, with nothing sent and no cart made, when an
        item is malformed, a ``sku`` appears twice, or
        (``check_products``, one catalogue read per sku, needs
        MOUSER_API_KEY) a ``sku`` is not a Mouser part number or its
        quantity is below the minimum or off the multiple.

        Returns:
            {"ok", "cart_key", "created", "sent", "added", "verified",
            "items", "cart"}. Keep ``cart_key``: it is the only way
            back to the cart. Each item has what was asked,
            ``cart_line`` (Mouser's part, quantity, prices, minimum
            and multiple as read back), ``differences`` and
            ``verified``. Needs MOUSER_CART_API_KEY.
        """
        return _cart_call(provider, lambda cart: cart.create_cart(
            items, check_products=check_products))

    @mcp.tool()
    async def part_cart_add(
        cart_key: str,
        items: list[dict[str, Any]],
        check_products: bool = True,
        provider: str = "mouser",
    ) -> dict[str, Any]:
        """Add parts to the Mouser cart that ``cart_key`` names.

        WRITES TO MOUSER. Adds to that cart and orders nothing.
        ``cart_key`` is required, from part_cart_create or the user;
        without one nothing is sent and no cart is made. Never guess a
        key or reuse one from another task: it would change someone
        else's cart.

        ``items`` and ``check_products`` are as for part_cart_create.
        A part already in the cart is not replaced: the reply says
        what the line held before and holds now.

        Returns:
            As part_cart_create, with ``created`` false.
        """
        return _cart_call(provider, lambda cart: cart.add_items(
            cart_key, items, check_products=check_products))

    @mcp.tool()
    async def part_cart_get(
        cart_key: str,
        provider: str = "mouser",
    ) -> dict[str, Any]:
        """Read a Mouser cart by its ``cart_key``. Reads only.

        Returns:
            {"ok", "cart_key", "currency", "merchandise_total",
            "total_item_count", "count", "items", "errors"}. Each item
            has ``sku`` (the Mouser part number), ``manufacturer``,
            ``manufacturer_part_number``, ``mouser_description``,
            ``quantity``, ``customer_part_number``, ``packaging``,
            ``unit_price``, ``extended_price``, ``stock``, ``moq``,
            ``multiple``, ``maximum``, ``messages`` and ``errors``.
            Reference designators are not in a Mouser cart. Needs
            MOUSER_CART_API_KEY.
        """
        return _cart_call(provider, lambda cart: cart.get_cart(cart_key))
