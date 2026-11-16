"""Opportunities left out of the ranking for their market history or thin photo evidence.

* ``on_market_too_long`` — first published more than ``MAX_DAYS_ON_MARKET`` days before the
  run, counted from the earliest publication across every advertisement of the flat. Agencies
  renew their ads, which resets ``publication_date`` (the 180-day stale filter in Layer 0 and
  the merge only sees that one), so a flat on sale for two years can look a month old.
* ``too_few_usable_photos`` — the image model accepted fewer than ``MIN_USABLE_PHOTOS`` photos,
  so its condition score (and with it the renovation cost) rests on one or two pictures.
* ``area_below_rent_data`` — smaller than ``MIN_AREA_SQM``: 4 of 5,888 rentals (Nov 2026) are,
  so the rent model has nothing to learn their rent from and only extrapolates.
* ``no_living_space_seen`` — the image model read the photos room by room and none was a kitchen,
  living room or bedroom (only bathroom, hallway, "other"): a store room or a shop, not a home
  (Center #1, Nov 2026: warehouse shelving, a street door, a toilet — ranked first at 7.8%).
  It was a check on the top 5 only; a flat like that has no rent to estimate, so it is not ranked.

All stay in the output, unranked, with their reason. A flat already unavailable keeps its own
reason; an unknown date or photo count is not judged.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Mapping, MutableMapping, Sequence

from greeceapt.scoring.seller_signals import seller_signals
from greeceapt.scoring.yield_model import _accepted_image_count

logger = logging.getLogger(__name__)

MAX_DAYS_ON_MARKET = 180   # decided 2026-11-16 (was 120)
MIN_USABLE_PHOTOS = 3
MIN_AREA_SQM = 20
ON_MARKET_TOO_LONG = "on_market_too_long"
TOO_FEW_USABLE_PHOTOS = "too_few_usable_photos"
AREA_BELOW_RENT_DATA = "area_below_rent_data"
NO_LIVING_SPACE_SEEN = "no_living_space_seen"
_LIVING_ROOMS = frozenset({"kitchen", "living", "bedroom"})


def no_living_space_seen(listing: Mapping) -> bool:
    """True when the photos were read room by room and none was a kitchen, living room or bedroom."""
    features = listing.get("layer_1_features_v2")
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except ValueError:
            return False
    rooms = (features or {}).get("room_scores") if isinstance(features, Mapping) else None
    return bool(rooms) and not (set(rooms) & _LIVING_ROOMS)


def _number(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def apply_listing_filters(candidates: Sequence[MutableMapping], *, as_of: date) -> dict[str, int]:
    """Mark filtered opportunities with ``unavailable_reason``; returns a count per reason."""
    counts = {ON_MARKET_TOO_LONG: 0, TOO_FEW_USABLE_PHOTOS: 0, AREA_BELOW_RENT_DATA: 0, NO_LIVING_SPACE_SEEN: 0}
    for listing in candidates:
        if listing.get("unavailable_reason"):
            continue
        days = seller_signals(listing, as_of=as_of).days_on_market
        area = _number(listing.get("area_sqm"))
        if days is not None and days > MAX_DAYS_ON_MARKET:
            reason = ON_MARKET_TOO_LONG
        elif area is not None and 0 < area < MIN_AREA_SQM:
            reason = AREA_BELOW_RENT_DATA
        elif listing.get("visual_status_v2") == "accepted" and (
            (count := _accepted_image_count(listing)) is not None and count < MIN_USABLE_PHOTOS
        ):
            reason = TOO_FEW_USABLE_PHOTOS
        elif no_living_space_seen(listing):
            reason = NO_LIVING_SPACE_SEEN
        else:
            continue
        listing["unavailable_reason"] = reason
        counts[reason] += 1
    logger.info(
        "Listing filters: %s on the market more than %s days, %s under %s m², %s with fewer than %s usable photos, "
        "%s with no kitchen, living room or bedroom in any photo.",
        counts[ON_MARKET_TOO_LONG], MAX_DAYS_ON_MARKET, counts[AREA_BELOW_RENT_DATA], MIN_AREA_SQM,
        counts[TOO_FEW_USABLE_PHOTOS], MIN_USABLE_PHOTOS, counts[NO_LIVING_SPACE_SEEN],
    )
    return counts
