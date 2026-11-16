"""Which advertisements of an opportunity are still tracked, and at what price.

The ingest database accumulates: a flat stays in it after its advertisement left the site, and
flats first found on XE stay after XE collection was turned off. Before ranking, every
opportunity is checked against what the collectors still follow:

* a Spitogatos advertisement is live unless the store stamped it ``gone_since`` (a trusted
  search sweep did not see it — sold, withdrawn, or priced out of the searched range);
* an XE advertisement is never live: XE is no longer collected (2026-11-16), so nothing checks
  that it still exists or what it costs today.

An opportunity with no live advertisement stays in the output, unranked, with the reason
``no_longer_listed`` or ``xe_only_untracked``. One with only some of its advertisements live is
ranked on the cheapest live one: price, area, price-cut field and the link come from that ad
(the Spitogatos store's current values; the ad's recorded offer otherwise).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

from greeceapt.storage import paths
from greeceapt.storage.util import normalize_listing_url

logger = logging.getLogger(__name__)

NO_LONGER_LISTED = "no_longer_listed"
XE_ONLY_UNTRACKED = "xe_only_untracked"
REASONS = (NO_LONGER_LISTED, XE_ONLY_UNTRACKED)


def _key(url: object) -> str:
    return normalize_listing_url(str(url)) or str(url)


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def is_xe(url: str) -> bool:
    return _host(url).endswith("xe.gr")


def is_spitogatos(url: str) -> bool:
    return _host(url).endswith("spitogatos.gr")


def load_tracked_ads(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Spitogatos store rows by normalized URL; empty when the store is missing or unreadable."""
    path = path or paths.SPITOGATOS_LISTINGS_JSON
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}
    return {_key(row["url"]): row for row in rows if isinstance(row, dict) and row.get("url")}


def xe_is_collected() -> bool:
    """XE is no longer collected; tests of the older fixtures switch this on."""
    return False


def is_live(url: str, tracked: Mapping[str, Mapping[str, Any]], *, xe_tracked: bool) -> bool:
    if is_xe(url):
        return xe_tracked
    if is_spitogatos(url):
        row = tracked.get(_key(url))
        return row is None or not row.get("gone_since")  # an ad the store never saw is not judged
    return True


def _number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _members(listing: Mapping) -> list[str]:
    urls = list(listing.get("source_urls") or []) + [listing.get("url")]
    return sorted({_key(url) for url in urls if url})


def has_live_ad(
    listing: Mapping, tracked: Mapping[str, Mapping[str, Any]], *, xe_tracked: bool
) -> bool:
    """True while at least one of the opportunity's advertisements is still followed."""
    return any(is_live(url, tracked, xe_tracked=xe_tracked) for url in _members(listing))


def live_ad_check(
    *, tracked: Mapping[str, Mapping[str, Any]] | None = None, xe_tracked: bool | None = None
) -> Callable[[Mapping], bool]:
    """``has_live_ad`` bound to the current store and sources setting (loaded once)."""
    tracked = load_tracked_ads() if tracked is None else tracked
    xe_tracked = xe_is_collected() if xe_tracked is None else xe_tracked
    return lambda listing: has_live_ad(listing, tracked, xe_tracked=xe_tracked)


def _live_offers(
    listing: Mapping, live: Sequence[str], tracked: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Price and area of each live ad: the store's current values, else the ad's recorded offer."""
    recorded = {
        _key(offer.get("url")): offer
        for offer in listing.get("advertised_offers") or []
        if isinstance(offer, Mapping) and offer.get("url")
    }
    offers = []
    for url in live:
        row = tracked.get(url) if is_spitogatos(url) else None
        source = row if row is not None else recorded.get(url)
        if source is None:
            continue
        price = _number(source.get("price_eur"))
        if price is None:
            continue
        offers.append({
            "url": url, "price_eur": price, "area_sqm": _number(source.get("area_sqm")),
            "price_pre_reduction": source.get("price_pre_reduction"), "from_store": row is not None,
        })
    return sorted(offers, key=lambda offer: (offer["price_eur"], offer["url"]))


def apply_availability(
    candidates: Sequence[MutableMapping],
    *,
    tracked: Mapping[str, Mapping[str, Any]] | None = None,
    xe_tracked: bool | None = None,
) -> dict[str, int]:
    """Mark opportunities with no live ad and re-base the others on their cheapest live ad.

    Mutates ``candidates``; returns how many were left as they are (``live``), re-based
    (``rebased``), or marked unavailable (one count per reason).
    """
    tracked = load_tracked_ads() if tracked is None else tracked
    xe_tracked = xe_is_collected() if xe_tracked is None else xe_tracked
    counts = {"live": 0, "rebased": 0, NO_LONGER_LISTED: 0, XE_ONLY_UNTRACKED: 0}
    for listing in candidates:
        members = _members(listing)
        live = [url for url in members if is_live(url, tracked, xe_tracked=xe_tracked)]
        if not live:
            reason = XE_ONLY_UNTRACKED if members and all(is_xe(url) for url in members) else NO_LONGER_LISTED
            listing["unavailable_reason"] = reason
            counts[reason] += 1
            continue
        listing.pop("unavailable_reason", None)
        offers = _live_offers(listing, live, tracked) if len(live) < len(members) else []
        if not offers:
            counts["live"] += 1
            continue
        best = offers[0]
        listing["price_eur"] = best["price_eur"]
        if best["area_sqm"] is not None:
            listing["area_sqm"] = best["area_sqm"]
        if best["from_store"]:
            listing["price_pre_reduction"] = best["price_pre_reduction"]
        listing["url"] = best["url"]
        counts["rebased"] += 1
    logger.info(
        "Availability: %s opportunities as advertised, %s re-based on their cheapest live ad, "
        "%s no longer listed, %s only on XE (no longer collected).",
        counts["live"], counts["rebased"], counts[NO_LONGER_LISTED], counts[XE_ONLY_UNTRACKED],
    )
    return counts
