# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Distributor and aggregator catalogues.

Five sources of part IDENTITY: manufacturer part number, datasheet,
lifecycle and stock. None of them yields a symbol or a footprint, which
is why they all declare ``kind = "catalogue"`` and no importable format.

They matter because of what this project measures everything against.
``lib_audit_footprint_vs_datasheet`` and the design checks all want a
datasheet URL, and until now nothing here could find one: the library
providers know a symbol's name but not the part's paperwork.

Every endpoint below was probed live before being written down. The
comment on each class records what it answered, because "I checked" is
worth nothing without the result. Three further candidates were probed
and DROPPED for answering 404 on the recalled URL rather than being
shipped as plausible guesses.

WHAT IS AND IS NOT VERIFIED HERE. The endpoints exist: measured. The
auth mechanisms follow each vendor's published scheme. DigiKey and
Mouser have been read live with a credential and state
``verified_live = True``; for the other three the response parsing has
NOT been exercised against a live credentialed reply, which each states
through ``verified_live = False`` and which the provider catalogue
reports. The parsers are written to survive a renamed field rather than
assume one, so a shape drift degrades a hit instead of losing the whole
search.

DigiKey and Mouser hits also carry ``sourcing``: stock, lifecycle, lead
time and one offer per orderable number with its packaging and price
breaks (``_sourcing`` holds the shape). Both are read-only search APIs;
nothing here orders anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any

from eda_agent.libimport.providers import _cache
from eda_agent.libimport.providers._distributor import (
    DistributorProvider,
    first_string,
)
from eda_agent.libimport.providers._sourcing import (
    lead_time,
    lifecycle,
    now_utc,
    price_break,
    to_int,
    to_number,
)
from eda_agent.libimport.providers.base import PartHit

__all__ = [
    "DigiKeyProvider",
    "Element14Provider",
    "MouserProvider",
    "NexarProvider",
    "TmeProvider",
]


def _dicts(value: Any) -> list[dict[str, Any]]:
    """The dict entries of ``value`` when it is a list, else none.

    A reply whose list arrived as null, a string or a list of scalars
    then yields nothing for that field and the rest of the hit stands.
    """
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _priced(entries: list[dict[str, Any]], *fields: str) -> bool:
    """Whether any entry states a price or a stock figure."""
    return any(entry.get(field) not in (None, "", [])
               for entry in entries for field in fields)


def _digikey_kinds(search: bool, reply: Any):
    """What a Product Information reply contains, for the cache; None
    for one that is not to be kept."""
    if not isinstance(reply, dict):
        return None
    if search:
        listed = [reply.get(key) for key in ("ExactMatches", "Products",
                                             "products")]
        if not any(isinstance(entry, list) for entry in listed):
            return None
        products = [p for entry in listed for p in _dicts(entry)]
        kinds = [_cache.SEARCH]
    else:
        if not isinstance(reply.get("Product"), dict):
            return None
        products = [reply["Product"]]
        kinds = [_cache.PRODUCT]
    variations = [v for p in products
                  for v in _dicts(p.get("ProductVariations"))]
    if _priced(products, "QuantityAvailable", "UnitPrice") or _priced(
            variations, "StandardPricing", "MyPricing",
            "QuantityAvailableforPackageType"):
        kinds.append(_cache.PRICING)
    return kinds


def _mouser_kinds(keyword: bool, reply: Any):
    """What a Search API reply contains, for the cache; None for one
    that is not to be kept. An exact lookup that found nothing is not
    kept: "no such part" should not outlive the part's arrival."""
    results = reply.get("SearchResults") if isinstance(reply, dict) else None
    if not isinstance(results, dict) \
            or not isinstance(results.get("Parts"), list):
        return None
    parts = _dicts(results["Parts"])
    if not parts and not keyword:
        return None
    kinds = [_cache.SEARCH if keyword else _cache.PRODUCT]
    if _priced(parts, "PriceBreaks", "AvailabilityInStock", "Availability",
               "AvailabilityOnOrder", "FactoryStock"):
        kinds.append(_cache.PRICING)
    return kinds


def _from_cache(hits: list, held: Any) -> list:
    """Mark hits built from a held reply with when it was really read."""
    if held:
        for hit in hits:
            sourcing = getattr(hit, "sourcing", None)
            if not isinstance(sourcing, dict):
                continue
            sourcing["retrieved_at"] = held["fetched_at"]
            sourcing["freshness"] = (
                f"cached: read at {held['fetched_at']} and reused until "
                f"{held['expires_at']}; refresh reads it again. As read "
                f"then: {sourcing.get('freshness')}")
            sourcing["cache"] = {"hit": True, **held}
    return hits


#: DigiKey access tokens by client id, with the time each stops being
#: used. Module level because a fresh provider is built for every call.
_DIGIKEY_TOKENS: dict[str, tuple[str, float]] = {}


class DigiKeyProvider(DistributorProvider):
    """Digi-Key's Product Information V4: search and product details.

    Read live with a credential on 2026-11-16, LM358DR: KeywordSearch
    returns each product with one ProductVariation per packaging, each
    with its own DigiKey number, stock, minimum and price breaks, and
    the currency once for the whole reply in ``SearchLocaleUsed``.
    ProductDetails answers a DigiKey number, and answers HTTP 404
    "Duplicate Products found" for a manufacturer number two makers
    share. Nothing in the reply states an order multiple.
    """

    name = "digikey"
    description = (
        "Digi-Key catalogue: MPN, datasheet, lifecycle, stock, lead time "
        "and price breaks per packaging. Identity and sourcing only, no "
        "symbol or footprint. Needs an OAuth client from the Digi-Key "
        "developer portal via DIGIKEY_CLIENT_ID and "
        "DIGIKEY_CLIENT_SECRET; DIGIKEY_LOCALE_SITE, _LANGUAGE and "
        "_CURRENCY pick the market.")
    env_vars = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET")
    verified_live = True

    _TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    _SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
    _DETAILS_URL = ("https://api.digikey.com/products/v4/search/"
                    "{number}/productdetails")

    #: Optional market selection, sent only when set. Unset, DigiKey
    #: uses US, en and USD; a value it does not accept is replaced by
    #: its default, and the reply's ``locale`` shows what it used.
    _LOCALE_HEADERS = (
        ("DIGIKEY_LOCALE_SITE", "X-DIGIKEY-Locale-Site"),
        ("DIGIKEY_LOCALE_LANGUAGE", "X-DIGIKEY-Locale-Language"),
        ("DIGIKEY_LOCALE_CURRENCY", "X-DIGIKEY-Locale-Currency"),
    )

    def _token(self) -> str:
        """A bearer token, requested once and reused until it is due.

        The client-credentials grant has no refresh token: a token lasts
        ten minutes and the next one is simply asked for.
        """
        creds = self._credentials()
        held = _DIGIKEY_TOKENS.get(creds["DIGIKEY_CLIENT_ID"])
        if held and held[1] > time.monotonic():
            return held[0]
        body = urllib.parse.urlencode({
            "client_id": creds["DIGIKEY_CLIENT_ID"],
            "client_secret": creds["DIGIKEY_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        }).encode("ascii")
        payload = self._request(
            self._TOKEN_URL, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = first_string(payload, ("access_token",))
        if not token:
            raise self._no_token()
        lifetime = to_number(payload.get("expires_in")) or 0
        if lifetime > 120:
            _DIGIKEY_TOKENS[creds["DIGIKEY_CLIENT_ID"]] = (
                token, time.monotonic() + lifetime - 60)
        return token

    def _no_token(self):
        from eda_agent.libimport.providers.base import ProviderUnavailable
        return ProviderUnavailable(
            f"{self.name} accepted the credential exchange but returned no "
            f"access_token")

    def _api(self, url: str, body: dict[str, Any] | None = None) -> Any:
        """One authorised call. A refused token is replaced once."""
        import os

        from eda_agent.libimport.providers.base import ProviderError

        creds = self._credentials()
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                "Content-Type": "application/json",
            }
            for var, header in self._LOCALE_HEADERS:
                value = os.environ.get(var, "").strip()
                if value:
                    headers[header] = value
            try:
                return self._request(
                    url, method="POST" if body is not None else "GET",
                    body=(json.dumps(body).encode("utf-8")
                          if body is not None else None),
                    headers=headers)
            except ProviderError as exc:
                # A token DigiKey no longer accepts comes back as 401,
                # or as 400 naming the Bearer token.
                status = getattr(exc, "status", None)
                stale = status == 401 or (
                    status == 400
                    and "bearer" in getattr(exc, "detail", "").lower())
                if not stale or attempt == 2:
                    raise
                _DIGIKEY_TOKENS.pop(creds["DIGIKEY_CLIENT_ID"], None)

    def _product_hit(self, product: Any, locale: Any,
                     live: bool) -> PartHit | None:
        """One Product as a hit with its sourcing, or None without an MPN."""
        if not isinstance(product, dict):
            return None
        mpn = first_string(product,
                           ("ManufacturerProductNumber",),
                           ("ManufacturerPartNumber",),
                           ("Description", "ProductDescription"))
        if not mpn:
            return None
        locale = locale if isinstance(locale, dict) else {}
        currency = first_string(locale, ("Currency",)) or None
        offers = []
        for variation in _dicts(product.get("ProductVariations")):
            packaging = first_string(variation, ("PackageType", "Name"))
            breaks = [
                price_break(b.get("BreakQuantity"), b.get("UnitPrice"))
                for b in _dicts(variation.get("StandardPricing"))]
            offers.append({
                "distributor_pn": first_string(
                    variation, ("DigiKeyProductNumber",)) or None,
                "packaging": [packaging] if packaging else [],
                "stock": to_int(
                    variation.get("QuantityAvailableforPackageType")),
                "moq": to_int(variation.get("MinimumOrderQuantity")),
                # DigiKey states no order multiple.
                "multiple": None,
                "standard_package": to_int(
                    variation.get("StandardPackage")),
                "currency": currency,
                "price_breaks": [b for b in breaks if b],
                "provider_specific": {
                    "digi_reel_fee": to_number(variation.get("DigiReelFee")),
                    "marketplace": variation.get("MarketPlace"),
                    "tariff_active": variation.get("TariffActive"),
                    "supplier": first_string(
                        variation, ("Supplier", "Name")) or None,
                },
            })
        sourcing = {
            "distributor": self.name,
            "product_url": first_string(product, ("ProductUrl",)) or None,
            "stock": to_int(product.get("QuantityAvailable")),
            "manufacturer_stock": to_int(
                product.get("ManufacturerPublicQuantity")),
            "on_order": None,
            "lifecycle": lifecycle(
                first_string(product, ("ProductStatus", "Status")),
                discontinued=product.get("Discontinued"),
                end_of_life=product.get("EndOfLife")),
            "lead_time": lead_time(
                product.get("ManufacturerLeadWeeks"), "weeks"),
            "suggested_replacement": None,
            "offers": offers,
            "locale": {
                "site": first_string(locale, ("Site",)) or None,
                "language": first_string(locale, ("Language",)) or None,
                "currency": currency,
            },
            "retrieved_at": now_utc(),
            "freshness": (
                "live: ProductDetails" if live else
                "search index: Digi-Key says KeywordSearch may be up to "
                "24 h old; part_fetch reads live"),
            "provider_specific": {
                "last_buy_date": first_string(
                    product, ("DateLastBuyChance",)) or None,
                "normally_stocking": product.get("NormallyStocking"),
                "back_order_not_allowed": product.get(
                    "BackOrderNotAllowed"),
                "ncnr": product.get("Ncnr"),
            },
        }
        return self._hit(
            mpn,
            mpn=mpn,
            manufacturer=first_string(product,
                                      ("Manufacturer", "Name"),
                                      ("Manufacturer", "Value")),
            description=first_string(
                product, ("Description", "ProductDescription"),
                ("DetailedDescription",)),
            datasheet=first_string(product, ("DatasheetUrl",),
                                   ("PrimaryDatasheet",)),
            sourcing=sourcing,
        )

    def _read(self, url: str, body: dict[str, Any] | None,
              search: bool) -> tuple[Any, Any]:
        """``_api`` through the cache: the reply, and when it was read
        if it was held. The locale and the account are part of the key,
        because both change the prices."""
        import os

        creds = self._credentials()
        context = {
            "account": _cache.account_fingerprint(
                creds["DIGIKEY_CLIENT_ID"]),
            "locale": {var: os.environ.get(var, "").strip().upper()
                       for var, _ in self._LOCALE_HEADERS},
        }
        return _cache.cached_call(
            self.name, url, body or {}, context,
            lambda: self._api(url, body),
            kinds=lambda reply: _digikey_kinds(search, reply))

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        payload, held = self._read(self._SEARCH_URL, {
            "Keywords": str(query),
            "Limit": int(limit),
            "Offset": 0,
        }, search=True)
        if not isinstance(payload, dict):
            return []
        locale = payload.get("SearchLocaleUsed")
        # ExactMatches first: a short page can leave an exact match out
        # of Products, and the same product is usually in both.
        products = (_dicts(payload.get("ExactMatches"))
                    + _dicts(payload.get("Products")
                             or payload.get("products")))
        hits: list[PartHit] = []
        seen = set()
        for product in products:
            hit = self._product_hit(product, locale, live=False)
            if hit is None:
                continue
            key = (hit.mpn.lower(), hit.manufacturer.lower())
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
        return self._distinct_ids(_from_cache(hits[:limit], held))

    def _details(self, number: str) -> PartHit | None:
        payload, held = self._read(self._DETAILS_URL.format(
            number=urllib.parse.quote(str(number), safe="")), None,
            search=False)
        if not isinstance(payload, dict):
            return None
        hit = self._product_hit(payload.get("Product"),
                                payload.get("SearchLocaleUsed"), live=True)
        return _from_cache([hit], held)[0] if hit else None

    def fetch(self, part_id: str) -> dict[str, Any]:
        """Live detail for a manufacturer or DigiKey part number.

        ProductDetails answers one product. A number it does not
        resolve is found through the search instead and then read live
        by its DigiKey number; one that two makers share is refused as
        ambiguous with both listed.
        """
        from eda_agent.libimport.providers.base import (
            ProviderError,
            ProviderUnavailable,
        )

        try:
            hit = self._details(part_id)
            hits = [hit] if hit else []
        except ProviderUnavailable:
            raise
        except ProviderError as exc:
            if getattr(exc, "status", None) not in (400, 404):
                raise
            hits = self._matching(self.search(part_id, 50), part_id)
            if len(hits) == 1:
                numbers = [o["distributor_pn"]
                           for o in hits[0].sourcing["offers"]
                           if o["distributor_pn"]]
                try:
                    hits = [(numbers and self._details(numbers[0]))
                            or hits[0]]
                except ProviderUnavailable:
                    raise
                except ProviderError:
                    # The search copy still answers; its freshness line
                    # says it is the index and not the live read.
                    pass
        if not hits:
            raise ProviderError(f"{self.name} has no part {part_id!r}")
        return self._detail(hits)


class MouserProvider(DistributorProvider):
    """Mouser's Search API: keyword and part-number search.

    Probed: ``POST /api/v1/search/keyword`` answered HTTP **200** with
    ``{"Errors":[{"Code":"Invalid",...}]}`` to a bogus key. That is the
    reason ``_reject_error_payload`` exists in the base class: judged on
    status alone, a rejected credential here is indistinguishable from a
    search that ran and matched nothing.

    Read live with a credential on 2026-11-16, LM358DR: a price break
    carries a numeric ``RawPrice`` beside the formatted ``Price``; a
    part Mouser does not stock has ``AvailabilityInStock`` null and no
    price breaks; ``LifecycleStatus`` is null on a part in production.
    The part-number search takes a Mouser or a manufacturer number, and
    one Mouser number covers every packaging the part ships in.
    """

    name = "mouser"
    description = (
        "Mouser catalogue: MPN, datasheet, lifecycle, stock, lead time "
        "and price breaks. Identity and sourcing only, no symbol or "
        "footprint. Needs a search API key from the Mouser developer "
        "portal via MOUSER_API_KEY; prices follow that key's account.")
    env_vars = ("MOUSER_API_KEY",)
    verified_live = True

    _SEARCH_URL = "https://api.mouser.com/api/v1/search/keyword"
    _PART_URL = "https://api.mouser.com/api/v1/search/partnumber"

    def _post(self, url: str, request: dict[str, Any]) -> list[PartHit]:
        creds = self._credentials()
        # The address is kept without its query, where the key travels;
        # the account the prices belong to is told apart by a
        # fingerprint of the key.
        payload, held = _cache.cached_call(
            self.name, url, request,
            {"account": _cache.account_fingerprint(creds["MOUSER_API_KEY"])},
            lambda: self._request(
                self._query(url, {"apiKey": creds["MOUSER_API_KEY"]}),
                method="POST", body=json.dumps(request).encode("utf-8"),
                headers={"Content-Type": "application/json"}),
            kinds=lambda reply: _mouser_kinds(
                url == self._SEARCH_URL, reply))
        results = payload.get("SearchResults") \
            if isinstance(payload, dict) else None
        parts = results.get("Parts") if isinstance(results, dict) else None
        hits = [self._part_hit(part) for part in _dicts(parts)]
        return _from_cache([hit for hit in hits if hit], held)

    def _part_hit(self, part: dict[str, Any]) -> PartHit | None:
        """One Part as a hit with its sourcing, or None without an MPN."""
        mpn = first_string(part, ("ManufacturerPartNumber",))
        if not mpn:
            return None
        breaks = [
            price_break(b.get("Quantity"), b.get("RawPrice"), b.get("Price"))
            for b in _dicts(part.get("PriceBreaks"))]
        currency = ""
        for entry in _dicts(part.get("PriceBreaks")):
            currency = currency or first_string(entry, ("Currency",))
        packaging: list[str] = []
        standard_package = None
        for attribute in _dicts(part.get("ProductAttributes")):
            label = first_string(attribute, ("AttributeName",))
            value = first_string(attribute, ("AttributeValue",))
            # English labels only: a key registered in another language
            # gets them translated, and packaging then stays empty.
            if label == "Packaging" and value:
                packaging.append(value)
            elif label == "Standard Pack Qty":
                standard_package = to_int(value)
        on_order = [
            {"quantity": to_int(entry.get("Quantity")),
             "date": first_string(entry, ("Date",))[:10] or None}
            for entry in _dicts(part.get("AvailabilityOnOrder"))]
        discontinued = str(part.get("IsDiscontinued") or "").strip().lower()
        offer = {
            "distributor_pn": first_string(
                part, ("MouserPartNumber",)) or None,
            # One Mouser number covers every packaging listed.
            "packaging": packaging,
            "stock": to_int(part.get("AvailabilityInStock")),
            "moq": to_int(part.get("Min")),
            "multiple": to_int(part.get("Mult")),
            "standard_package": standard_package,
            "currency": currency or None,
            "price_breaks": [b for b in breaks if b],
            "provider_specific": {
                "mousereel_offered": part.get("Reeling"),
            },
        }
        sourcing = {
            "distributor": self.name,
            "product_url": first_string(part, ("ProductDetailUrl",)) or None,
            "stock": to_int(part.get("AvailabilityInStock")),
            "manufacturer_stock": to_int(part.get("FactoryStock")),
            "on_order": on_order,
            "lifecycle": lifecycle(
                part.get("LifecycleStatus"),
                discontinued={"true": True, "yes": True, "false": False,
                              "no": False}.get(discontinued)),
            "lead_time": lead_time(part.get("LeadTime")),
            "suggested_replacement": first_string(
                part, ("SuggestedReplacement",)) or None,
            "offers": [offer],
            # Mouser states no locale; the key's account sets it.
            "locale": {"site": None, "language": None,
                       "currency": currency or None},
            "retrieved_at": now_utc(),
            "freshness": ("as Mouser's Search API reported it at "
                          "retrieved_at"),
            "provider_specific": {
                "availability_text": first_string(
                    part, ("Availability",)) or None,
                "alternate_packagings": [
                    first_string(entry, ("APMfrPN",))
                    for entry in _dicts(part.get("AlternatePackagings"))],
                "surcharge_messages": [
                    first_string(entry, ("message",))
                    for entry in _dicts(part.get("SurchargeMessages"))],
                "restriction_message": first_string(
                    part, ("RestrictionMessage",)) or None,
            },
        }
        return self._hit(
            mpn,
            mpn=mpn,
            manufacturer=first_string(part, ("Manufacturer",)),
            description=first_string(part, ("Description",)),
            datasheet=first_string(part, ("DataSheetUrl",)),
            sourcing=sourcing,
        )

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        return self._distinct_ids(self._post(self._SEARCH_URL, {
            "SearchByKeywordRequest": {
                "keyword": str(query),
                "records": int(limit),
                "startingRecord": 0,
            }
        })[:limit])

    def fetch(self, part_id: str) -> dict[str, Any]:
        """Detail for a Mouser or manufacturer part number.

        Asked of the part-number search, which matches the number
        exactly, then of the keyword search when that finds nothing.
        """
        from eda_agent.libimport.providers.base import ProviderError

        hits = self._matching(self._post(self._PART_URL, {
            "SearchByPartRequest": {
                "mouserPartNumber": str(part_id),
                "partSearchOptions": "Exact",
            }
        }), part_id)
        if not hits:
            hits = self._matching(self.search(part_id, 50), part_id)
        if not hits:
            raise ProviderError(f"{self.name} has no part {part_id!r}")
        return self._detail(hits)


class NexarProvider(DistributorProvider):
    """Nexar, the API behind Octopart.

    Probed: ``POST https://api.nexar.com/graphql`` answered a valid
    GraphQL body to an unauthenticated ``{__typename}`` introspection,
    and ``https://identity.nexar.com/connect/token`` answered 400 to an
    empty grant. Both exist; real part queries need the token.
    """

    name = "nexar"
    description = (
        "Nexar (Octopart) part search: MPN, manufacturer, description "
        "and datasheet link. Identity only: no prices, stock, symbol or "
        "footprint. Needs NEXAR_CLIENT_ID and NEXAR_CLIENT_SECRET from "
        "your own Nexar app, whose API terms are yours to keep.")
    env_vars = ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET")

    _TOKEN_URL = "https://identity.nexar.com/connect/token"
    _API_URL = "https://api.nexar.com/graphql"

    _QUERY = """
    query SearchMpn($q: String!, $limit: Int!) {
      supSearchMpn(q: $q, limit: $limit) {
        results {
          part {
            mpn
            manufacturer { name }
            shortDescription
            bestDatasheet { url }
          }
        }
      }
    }
    """

    def _token(self) -> str:
        creds = self._credentials()
        body = urllib.parse.urlencode({
            "client_id": creds["NEXAR_CLIENT_ID"],
            "client_secret": creds["NEXAR_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        }).encode("ascii")
        payload = self._request(
            self._TOKEN_URL, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = first_string(payload, ("access_token",))
        if not token:
            from eda_agent.libimport.providers.base import ProviderUnavailable
            raise ProviderUnavailable(
                f"{self.name} returned no access_token")
        return token

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        token = self._token()
        body = json.dumps({
            "query": self._QUERY,
            "variables": {"q": str(query), "limit": int(limit)},
        }).encode("utf-8")
        payload = self._request(
            self._API_URL, method="POST", body=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        results = (((payload.get("data") or {}).get("supSearchMpn") or {})
                   .get("results") or [])
        hits = []
        for entry in results[:limit]:
            part = (entry or {}).get("part") or {}
            mpn = first_string(part, ("mpn",))
            if not mpn:
                continue
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(part, ("manufacturer", "name")),
                description=first_string(part, ("shortDescription",)),
                datasheet=first_string(part, ("bestDatasheet", "url")),
            ))
        return hits


class Element14Provider(DistributorProvider):
    """element14 / Farnell / Newark product search.

    Probed: ``GET /catalog/products`` answered HTTP 403 with the body
    ``Developer Inactive``, so the path exists and gates on the key.
    """

    name = "element14"
    description = (
        "element14 (Farnell, Newark) catalogue: MPN, datasheet and "
        "stock. Identity only, no symbol or footprint. Needs a product "
        "search key via ELEMENT14_API_KEY; set ELEMENT14_STORE to pick "
        "a regional store.")
    env_vars = ("ELEMENT14_API_KEY",)

    _SEARCH_URL = "https://api.element14.com/catalog/products"
    #: Regional storefront. element14 serves different catalogues per
    #: store, so this changes which parts and prices come back.
    _DEFAULT_STORE = "uk.farnell.com"

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        import os

        creds = self._credentials()
        store = os.environ.get("ELEMENT14_STORE", "").strip() \
            or self._DEFAULT_STORE
        url = self._query(self._SEARCH_URL, {
            "term": f"any:{query}",
            "storeInfo.id": store,
            "callInfo.apiKey": creds["ELEMENT14_API_KEY"],
            "callInfo.responseDataFormat": "json",
            "resultsSettings.numberOfResults": str(int(limit)),
            "resultsSettings.offset": "0",
            "resultsSettings.responseGroup": "large",
        })
        payload = self._request(url)
        products = ((payload.get("premierFarnellPartNumberReturn") or {})
                    .get("products") or [])
        hits = []
        for product in products[:limit]:
            mpn = first_string(product, ("translatedManufacturerPartNumber",),
                               ("sku",))
            if not mpn:
                continue
            datasheets = product.get("datasheets") or []
            datasheet = ""
            if isinstance(datasheets, list) and datasheets:
                datasheet = first_string(datasheets[0], ("url",))
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(product, ("brandName",),
                                          ("vendorName",)),
                description=first_string(
                    product, ("displayName",),
                    ("productOverview", "description")),
                datasheet=datasheet,
            ))
        return hits


class TmeProvider(DistributorProvider):
    """TME (Transfer Multisort Elektronik) product search.

    Probed: ``POST /Products/Search.json`` answered HTTP 403 with
    ``{"Status":"E_ACTION_FORBIDDEN"}``, so the path exists and gates on
    the signed request.

    TME signs every call: HMAC-SHA1 over the method, the URL and the
    sorted parameters, base64 encoded. That is implemented here but, like
    every parser in this module, has not been exercised against a live
    credentialed reply.
    """

    name = "tme"
    description = (
        "TME catalogue: MPN, datasheet and stock, strongest on European "
        "availability. Identity only, no symbol or footprint. Needs "
        "TME_TOKEN and TME_SECRET; requests are HMAC signed.")
    env_vars = ("TME_TOKEN", "TME_SECRET")

    _SEARCH_URL = "https://api.tme.eu/Products/Search.json"

    def _sign(self, url: str, params: dict[str, str], secret: str) -> str:
        """TME's HMAC-SHA1 request signature.

        The signed string is METHOD&url&params, each percent-encoded as a
        whole, with parameters sorted by key.
        """
        encoded = urllib.parse.urlencode(sorted(params.items()))
        base = "&".join([
            "POST",
            urllib.parse.quote(url, safe=""),
            urllib.parse.quote(encoded, safe=""),
        ])
        digest = hmac.new(secret.encode("utf-8"), base.encode("utf-8"),
                          hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        import os

        creds = self._credentials()
        params = {
            "Token": creds["TME_TOKEN"],
            "SearchPlain": str(query),
            "Country": os.environ.get("TME_COUNTRY", "").strip() or "GB",
            "Language": "EN",
        }
        params["ApiSignature"] = self._sign(
            self._SEARCH_URL, params, creds["TME_SECRET"])
        body = urllib.parse.urlencode(params).encode("utf-8")
        payload = self._request(
            self._SEARCH_URL, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        products = ((payload.get("Data") or {}).get("ProductList") or [])
        hits = []
        for product in products[:limit]:
            mpn = first_string(product, ("OriginalSymbol",), ("Symbol",))
            if not mpn:
                continue
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(product, ("Producer",)),
                description=first_string(product, ("Description",)),
                # TME returns documents from a separate endpoint; the
                # search reply carries a product page rather than a
                # datasheet. Stated as a page, not passed off as one.
                datasheet=first_string(product, ("ProductInformationPage",)),
            ))
        return hits
