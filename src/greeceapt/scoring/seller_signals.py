"""
Display-only negotiation signals (never part of the ranking).

* price_cut_pct — how far the asking price is below the publisher's earlier price
  (Spitogatos ``price_pre_reduction``); flag ``price_cut`` from 5 %.
* days_on_market — days since the earliest known publication (merged across duplicate ads),
  relative to the run's explicit ``as_of``; flag ``long_on_market`` from 90 days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

PRICE_CUT_FLAG_PCT = 5.0
LONG_ON_MARKET_DAYS = 90


@dataclass(frozen=True)
class SellerSignals:
    price_cut_pct: float | None
    days_on_market: int | None
    flags: tuple[str, ...]
    first_published: str | None = None   # ISO date of the flat's earliest advertisement


def _number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def seller_signals(listing: Mapping[str, object], *, as_of: date) -> SellerSignals:
    price, before = _number(listing.get("price_eur")), _number(listing.get("price_pre_reduction"))
    cut = (before - price) / before * 100 if price and before and before > price else None

    published = _date(listing.get("earliest_publication_date")) or _date(listing.get("publication_date"))
    days = (as_of - published).days if published and published <= as_of else None

    flags = []
    if cut is not None and cut >= PRICE_CUT_FLAG_PCT:
        flags.append("price_cut")
    if days is not None and days >= LONG_ON_MARKET_DAYS:
        flags.append("long_on_market")
    return SellerSignals(cut, days, tuple(flags), published.isoformat() if published else None)
