"""
Within-neighborhood yield ranking (no cross-neighborhood comparison).

Opportunities are ranked **inside their own neighborhood** by the yield kept after every cost
and tax, at the conservative signed rent of the calibrated rent model (``greeceapt.rent``):

    net_yield_lo = (a year of rent_lo − every cost − income tax) / (price × 1.08 + renovation)

(``rent_yield``), one money figure with no hand-tuned weights. A flat the model cannot price
(no coordinates, or no rent model at all) is returned unranked with ``rent_<reason>``; a flat
with no advertisement that is still tracked (``availability``) with ``no_longer_listed`` /
``xe_only_untracked``.

Net yields fall into 0.01 % buckets so tie-breaking is a strict weak order; inside a bucket:
higher confidence, fewer risk flags, better accepted condition, lower all-in cost, then
canonical URL. Listings without a reproducible estimate are
returned separately with their exclusion reason — never silently dropped.

``prepare_candidates`` rebuilds listing dicts from ``updated_listings.db`` rows and runs the
opportunity collapse again over the **whole** candidate pool, because the ingest database
accumulates listings across runs and separately merged groups of one flat may share no URL.

The sale-market discount is secondary context:
    market_discount_pct = (1 − listing_psqm × floor_multiplier / baseline_psqm) × 100
positive = cheaper than the floor-adjusted neighborhood baseline; ``None`` when the sale
sample is too thin.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from greeceapt.scoring.market_analytics import floor_multiplier
from greeceapt.scoring.rent_yield import RentYield, estimate_rent_yield, renovation_alternative
from greeceapt.scoring.yield_model import YieldAssumptions, YieldEstimate, estimate_yield, let_as_is
from greeceapt.collect.floor_resolver import apply_floor_resolution
from greeceapt.collect.opportunity_dedupe import collapse_opportunities
from greeceapt.storage.neighborhoods import canonical_neighborhood
from greeceapt.storage.util import normalize_listing_url

CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
NET_YIELD_DECIMALS = 4     # net yields equal to 0.01 % share a bucket

_JSON_COLUMNS = {
    "source_urls_json": "source_urls",
    "advertised_offers_json": "advertised_offers",
    "floor_evidence_json": "floor_evidence",
    "photo_urls_json": "photo_urls",
}


@dataclass(frozen=True)
class RankedDeal:
    opportunity_id: str
    url: str
    source_urls: tuple[str, ...]
    neighborhood: str
    price_eur: float | None
    area_sqm: float | None
    floor: float | None
    floor_class: str | None
    floor_evidence: tuple[Mapping[str, Any], ...]
    visual_score: float | None
    estimate: YieldEstimate
    market_discount_pct: float | None
    title: str | None = None
    rank_in_neighborhood: int | None = None
    # Verification stage (scoring/verification.py)
    needs_check: bool = False
    check_reasons: tuple[str, ...] = ()
    # Display-only negotiation signals (scoring/seller_signals.py)
    price_cut_pct: float | None = None
    days_on_market: int | None = None
    first_published: str | None = None   # earliest advertisement of the flat (ISO date)
    # Calibrated rent model (greeceapt.rent): euro rent and the conservative net yield ranked on
    rent_yield: RentYield | None = None
    # Set when a catch-all area name was replaced by the neighborhood of the nearest ads
    neighborhood_note: str | None = None
    # The ad's area when it adds an attic, terrace or storage (``area_sqm`` is the living area)
    area_listed: float | None = None

    def _tie_break(self) -> tuple:
        estimate = self.estimate
        return (
            CONFIDENCE_RANK.get(estimate.confidence, 9),
            len(estimate.risk_flags),
            -(self.visual_score or 0.0),
            estimate.all_in_cost or math.inf,
            self.url,
        )

    @property
    def net_yield_sort_key(self) -> tuple:
        """Net yield order (the rank)."""
        net = self.rent_yield.net_yield_lo if self.rent_yield else None
        if net is None:
            return (math.inf, *self._tie_break())
        return (-round(net, NET_YIELD_DECIMALS), *self._tie_break())


def market_discount_pct(listing_psqm: float | None, floor_mult: float, baseline: float | None) -> float | None:
    if listing_psqm is None or baseline is None or baseline <= 0 or listing_psqm <= 0:
        return None
    return (1.0 - listing_psqm * floor_mult / baseline) * 100.0


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


# ── Candidate preparation (pass b) ────────────────────────────────────────────

def _as_dict(row: Any) -> dict:
    if isinstance(row, Mapping):
        return dict(row)
    return {key: row[key] for key in row.keys()}  # sqlite3.Row


def rebuild_listing(row: Any) -> dict:
    """Listing dict from a DB row: ``raw_json`` underneath, non-null DB columns on top."""
    data = _as_dict(row)
    try:
        listing = json.loads(data.get("raw_json") or "{}")
    except (TypeError, ValueError):
        listing = {}
    if not isinstance(listing, dict):
        listing = {}
    for key, value in data.items():
        if key == "raw_json" or value is None:
            continue
        if key in _JSON_COLUMNS:
            try:
                listing[_JSON_COLUMNS[key]] = json.loads(value)
            except (TypeError, ValueError):
                pass
            continue
        listing[key] = value
    listing.setdefault("source_urls", [listing.get("url")])
    if listing.get("neighborhood"):
        listing.setdefault("neighborhood_raw", listing["neighborhood"])
        listing["neighborhood"] = canonical_neighborhood(listing["neighborhood"])
    if listing.get("floor_class") is None and not listing.get("floor_evidence"):
        # Rows ingested before floor resolution existed (raw Spitogatos codes included).
        apply_floor_resolution(listing)
    else:
        refresh_floor(listing)
    return listing


_FLOOR_FIELDS = ("floor", "floor_class", "floor_conflict", "floor_conflict_adjacent", "floor_evidence")


def refresh_floor(listing: dict) -> None:
    """Re-read the floor with the current rules, so a rule change applies at the next re-score.

    Floors are resolved when ads are merged; without this a new rule (e.g. "situated on level -1
    (basement)") would wait for the next update. The stored result is kept when the evidence
    no longer resolves to anything (legacy rows with a bare code 0).
    """
    trial = {key: value for key, value in listing.items() if key not in _FLOOR_FIELDS}
    trial["floor"] = listing.get("floor")
    resolution = apply_floor_resolution(trial)
    if not any(item.source != "numeric" for item in resolution.evidence):
        return
    if resolution.floor_class is None and not resolution.conflict:
        return
    for key in _FLOOR_FIELDS:
        listing[key] = trial.get(key)


def _members(listing: Mapping) -> set[str]:
    urls = list(listing.get("source_urls") or []) + [listing.get("url")]
    return {normalize_listing_url(str(url)) or str(url) for url in urls if url}


def _drop_leftovers(listings: list[dict]) -> list[dict]:
    """Drop rows that are already a member of another, larger opportunity."""
    members = [_members(row) for row in listings]
    urls = [normalize_listing_url(str(row.get("url"))) or str(row.get("url")) for row in listings]
    kept: list[dict] = []
    for index, row in enumerate(listings):
        absorbed = any(
            other != index
            and urls[index] in members[other]
            and urls[index] != urls[other]
            and (len(members[other]) > len(members[index])
                 or (len(members[other]) == len(members[index]) and urls[other] < urls[index]))
            for other in range(len(listings))
        )
        if not absorbed:
            kept.append(row)
    return kept


def prepare_candidates(
    rows: Iterable[Any],
    decisions: Mapping[str, dict],
    *,
    blocked_components: Sequence[frozenset[str]] = (),
    is_tracked: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], list[str]]:
    """Rebuild, drop leftovers, collapse over the whole pool, then guard URL uniqueness.

    Returns ``(candidates, guard_violation_urls)``; violating candidates are kept but marked so
    they rank as ``duplicate_guard_violation``.
    """
    listings = _drop_leftovers([rebuild_listing(row) for row in rows])
    collapsed = collapse_opportunities(
        listings, dict(decisions), blocked_components=blocked_components, is_tracked=is_tracked
    ).listings
    owners: dict[str, list[int]] = {}
    for index, listing in enumerate(collapsed):
        for url in _members(listing):
            owners.setdefault(url, []).append(index)
    violating = sorted(url for url, indices in owners.items() if len(indices) > 1)
    for url in violating:
        for index in owners[url]:
            collapsed[index]["duplicate_guard_violation"] = True
    return collapsed, violating


# ── Ranking ───────────────────────────────────────────────────────────────────

def _deal(listing: Mapping, estimate: YieldEstimate, baselines: Mapping[str, float]) -> RankedDeal:
    price, area = _number(listing.get("price_eur")), _number(listing.get("area_sqm"))
    floor = _number(listing.get("floor"))
    hood = str(listing.get("neighborhood") or "").strip()
    psqm = price / area if price and area and area > 0 else None
    url = str(listing.get("url") or "")
    return RankedDeal(
        opportunity_id=str(listing.get("opportunity_id") or url),
        url=url,
        source_urls=tuple(sorted(_members(listing))),
        neighborhood=hood,
        price_eur=price,
        area_sqm=area,
        floor=floor,
        floor_class=listing.get("floor_class"),
        floor_evidence=tuple(listing.get("floor_evidence") or ()),
        visual_score=_number(listing.get("visual_score_v2")),
        estimate=estimate,
        market_discount_pct=market_discount_pct(psqm, floor_multiplier(floor), baselines.get(hood)),
        title=listing.get("title"),
        neighborhood_note=listing.get("neighborhood_note"),
        area_listed=_number(listing.get("area_listed")),
    )


def _unranked(estimate: YieldEstimate, reason: str) -> YieldEstimate:
    return replace(
        estimate, all_in_cost=None, confidence="unranked", exclusion_reason=reason, evidence_warnings=(),
    )


def rank_deals(
    candidates: Sequence[Mapping],
    sale_baselines: Mapping[str, float],
    assumptions: YieldAssumptions,
    *,
    renovation_budgets: Mapping[str, float] | None = None,
    rent_model=None,
) -> tuple[list[RankedDeal], list[RankedDeal]]:
    """Rank every opportunity within its neighborhood.

    Returns ``(ranked, unranked)``; ``ranked`` is ordered by neighborhood, then
    ``rank_in_neighborhood`` (1 = best, by net yield). Without ``rent_model`` nothing can be
    priced, so every flat is unranked (``rent_no_rent_model``).
    """
    budgets = renovation_budgets or {}
    deals: list[RankedDeal] = []
    for listing in candidates:
        opportunity_id = str(listing.get("opportunity_id") or listing.get("url") or "")
        estimate = estimate_yield(
            listing, assumptions, renovation_budget=float(budgets.get(opportunity_id, 0.0))
        )
        if listing.get("unavailable_reason"):
            # No advertisement of this flat is tracked any more (``scoring.availability``).
            estimate = _unranked(estimate, str(listing["unavailable_reason"]))
        elif listing.get("duplicate_guard_violation") and estimate.exclusion_reason is None:
            estimate = _unranked(estimate, "duplicate_guard_violation")
        rent = estimate_rent_yield(listing, estimate, rent_model)
        lease, model_lo = rent.costs.get("rent_stated"), rent.costs.get("rent_model_lo")
        if lease is not None and model_lo is not None and lease >= model_lo:
            kept = let_as_is(estimate, listing)       # keep the tenant: no renovation to pay for
            if kept is not estimate:
                alternative = renovation_alternative(listing, estimate, rent_model)
                estimate, rent = kept, estimate_rent_yield(listing, kept, rent_model)
                if alternative is not None:           # shown beside it: renovate and re-let instead
                    rent = replace(rent, costs={**rent.costs, "if_renovated": alternative})
        deal = replace(_deal(listing, estimate, sale_baselines), rent_yield=rent)
        if estimate.exclusion_reason is None and rent.net_yield_lo is None:
            deal = replace(deal, estimate=_unranked(estimate, f"rent_{rent.unavailable_reason}"))
        deals.append(deal)

    ranked: list[RankedDeal] = []
    unranked: list[RankedDeal] = []
    seen: set[str] = set()
    by_hood: dict[str, list[RankedDeal]] = {}
    for deal in sorted(deals, key=lambda item: item.net_yield_sort_key):
        if deal.estimate.exclusion_reason is not None:
            unranked.append(deal)
        elif deal.opportunity_id in seen:
            unranked.append(replace(deal, estimate=_unranked(deal.estimate, "duplicate_opportunity_id")))
        else:
            seen.add(deal.opportunity_id)
            by_hood.setdefault(deal.neighborhood, []).append(deal)
    for hood in sorted(by_hood):
        ranked.extend(replace(deal, rank_in_neighborhood=rank) for rank, deal in enumerate(by_hood[hood], start=1))
    unranked.sort(key=lambda item: (str(item.estimate.exclusion_reason), item.url))
    return ranked, unranked
