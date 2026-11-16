# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""DigiKey and Mouser against their live APIs. Opt-in.

A plain ``pytest`` skips every test here. They run only with
``EDA_AGENT_LIVE_DISTRIBUTORS=1`` and the provider's credentials set,
and then make a few read-only search calls for LM358DR, a public part
both distributors stock. Each run spends a handful of calls from the
key's daily allowance (1000 a day at each distributor).

What they hold is the shape, not the numbers: stock and prices move, so
the assertions are that a figure is there and is a number.
"""

from __future__ import annotations

import os

import pytest

from eda_agent.libimport.providers.base import AmbiguousPart
from eda_agent.libimport.providers.distributors import (
    DigiKeyProvider,
    MouserProvider,
)

_PART = "LM358DR"


def _live(cls):
    wanted = os.environ.get("EDA_AGENT_LIVE_DISTRIBUTORS") == "1"
    missing = [v for v in cls.env_vars if not os.environ.get(v, "").strip()]
    return pytest.mark.skipif(
        not wanted or bool(missing),
        reason=("live distributor tests are opt-in: set "
                "EDA_AGENT_LIVE_DISTRIBUTORS=1 and "
                + " and ".join(cls.env_vars)))


pytestmark = pytest.mark.network


def _check(sourcing, distributor):
    assert sourcing["distributor"] == distributor
    assert sourcing["product_url"].startswith("https://")
    assert isinstance(sourcing["stock"], int)
    assert sourcing["retrieved_at"].endswith("Z")
    assert sourcing["lead_time"]["weeks"] is not None
    assert sourcing["offers"]
    for offer in sourcing["offers"]:
        assert offer["distributor_pn"]
        assert isinstance(offer["moq"], int)
        assert offer["currency"] and len(offer["currency"]) == 3
        assert offer["price_breaks"]
        for entry in offer["price_breaks"]:
            assert entry["quantity"] >= 1 and entry["unit_price"] > 0


def _texas(hits):
    return next(h for h in hits if h.mpn == _PART
                and "texas" in h.manufacturer.lower())


@_live(DigiKeyProvider)
def test_digikey_live_search_and_fetch():
    provider = DigiKeyProvider()
    hit = _texas(provider.search(_PART, 5))
    _check(hit.sourcing, "digikey")
    assert hit.datasheet.startswith("https://")
    assert hit.sourcing["lifecycle"]["status"] == "active"
    assert len(hit.sourcing["offers"]) >= 2, "one offer per packaging"

    number = hit.sourcing["offers"][0]["distributor_pn"]
    detail = provider.fetch(number)
    assert detail["mpn"] == _PART
    assert detail["sourcing"]["freshness"].startswith("live")
    _check(detail["sourcing"], "digikey")

    # Two makers share the number, so neither is chosen.
    with pytest.raises(AmbiguousPart) as excinfo:
        provider.fetch(_PART)
    assert len(excinfo.value.candidates) >= 2
    assert hit.ref in [c["ref"] for c in excinfo.value.candidates]


@_live(MouserProvider)
def test_mouser_live_search_and_fetch():
    provider = MouserProvider()
    hit = _texas(provider.search(_PART, 5))
    _check(hit.sourcing, "mouser")
    # Mouser states no status for a part in production: not "active".
    assert hit.sourcing["lifecycle"]["status"] in (None, "new")

    number = hit.sourcing["offers"][0]["distributor_pn"]
    detail = provider.fetch(number)
    assert detail["mpn"] == _PART and "ambiguous" not in detail
    _check(detail["sourcing"], "mouser")
