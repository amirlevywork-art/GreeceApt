"""
scoring_conductor.py — Stage 4: within-neighborhood yield ranking → ``final_deals.db``.

Flow:
  1. Rebuild candidates from ``updated_listings.db`` and collapse duplicate advertisements
     again over the whole pool (the ingest DB accumulates across runs).
  2. Attach persistent opportunity IDs (pass b of the registry); mark opportunities with no
     advertisement that is still tracked and re-base the others on their cheapest live ad.
  3. Sale baselines (secondary market-discount context) from one row per opportunity.
  4. Rank every opportunity inside its neighborhood by the net yield from the calibrated rent
     model; keep unranked opportunities with their reason; check every ranked flat for traps.
  5. Publish atomically (lock, no sidecar deletion); commit the registry only afterwards.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Mapping

from greeceapt.storage import paths
from greeceapt.storage.util import normalize_listing_url

logger = logging.getLogger(__name__)

# Module-level so tests and replays can point them elsewhere.
OPPORTUNITY_DECISIONS_JSON = paths.OPPORTUNITY_DECISIONS_JSON
OPPORTUNITY_REGISTRY_DB = paths.OPPORTUNITY_REGISTRY_DB


class RenovationBudgetError(ValueError):
    """A renovation budget key is unknown or conflicts with another key."""


def resolve_renovation_budgets(
    budgets: Mapping[str, float] | None,
    candidates: list[dict],
    registry,
) -> dict[str, float]:
    """Map budgets keyed by opportunity ID (aliases allowed) or any member URL to opportunities."""
    if not budgets:
        return {}
    by_url: dict[str, str] = {}
    known_ids = {str(row.get("opportunity_id")) for row in candidates}
    for row in candidates:
        for url in list(row.get("source_urls") or []) + [row.get("url")]:
            if url:
                by_url[normalize_listing_url(str(url)) or str(url)] = str(row["opportunity_id"])
    resolved: dict[str, float] = {}
    for key, amount in budgets.items():
        target = by_url.get(normalize_listing_url(key) or key)
        if target is None:
            canonical = registry.resolve(key) if registry is not None else None
            target = canonical if canonical in known_ids else (key if key in known_ids else None)
        if target is None:
            raise RenovationBudgetError(f"renovation budget key {key!r} matches no current opportunity")
        if target in resolved and resolved[target] != float(amount):
            raise RenovationBudgetError(
                f"conflicting renovation budgets for opportunity {target}: {resolved[target]} vs {amount}"
            )
        resolved[target] = float(amount)
    return resolved


def load_rent_model():
    from greeceapt.rent.train import load

    try:
        return load()
    except Exception as exc:  # a broken model file must not stop the ranking
        logger.error("Rent model could not be loaded (%s); continuing without it.", exc)
        return None


def warn_on_vision_contract_mismatch(rent_model, candidates) -> None:
    """The visual coefficient only transfers if rentals and sale flats share a vision contract."""
    trained = rent_model.meta.get("vision", {})
    sale = {
        (row.get("vision_model_version"), row.get("vision_prompt_version"))
        for row in candidates if row.get("visual_status_v2") == "accepted"
    }
    if sale and (trained.get("model"), trained.get("prompt")) not in sale:
        logger.warning(
            "Rent model visual scores came from %s/%s but sale listings use %s — "
            "re-score one side before trusting the condition effect.",
            trained.get("model"), trained.get("prompt"), sorted(sale, key=str),
        )


def rent_model_metadata(rent_model) -> dict | None:
    from greeceapt.scoring.rent_yield import DEFAULT_COSTS
    from greeceapt.scoring.yield_model import RENOVATION_TARGET_SCORE

    if rent_model is None:
        return None
    return {
        "trained_at": rent_model.meta.get("trained_at"),
        "n_train": rent_model.meta.get("n_train"),
        "vision": rent_model.meta.get("vision"),
        "alpha": rent_model.alpha,
        "k": rent_model.k,
        "random_cv": rent_model.metrics.get("random_cv"),
        "lower_bound_multiplier": math.exp(rent_model.lower_log_residual),
        "lower_bound_multiplier_by_group": {
            g: math.exp(v) for g, v in (getattr(rent_model, "lower_by_group", None) or {}).items()},
        "sparse_km": getattr(rent_model, "sparse_km", None),
        "costs": DEFAULT_COSTS.to_json(),
        "renovation_target_score": RENOVATION_TARGET_SCORE,
    }


def run(
    *,
    as_of: date | None = None,
    renovation_budgets: Mapping[str, float] | None = None,
) -> None:
    from greeceapt.photos.layer_2_aesthetic_filter import MIN_CONDITION_SCORE
    from greeceapt.storage import db_conductor
    from greeceapt.storage.final_db import write_final_database_atomic
    from greeceapt.scoring.availability import apply_availability, live_ad_check
    from greeceapt.scoring.listing_filters import apply_listing_filters
    from greeceapt.scoring.living_area import apply_living_area
    from greeceapt.scoring.market_analytics import compute_neighborhood_baselines
    from greeceapt.scoring.neighborhood_inference import infer_neighborhoods, rental_reference
    from greeceapt.scoring.ranking import prepare_candidates, rank_deals
    from greeceapt.storage.neighborhoods import NEIGHBORHOOD_CANONICAL
    from greeceapt.scoring.seller_signals import seller_signals
    from greeceapt.scoring.verification import neighborhood_centers, verify_ranked
    from greeceapt.vision.reliability import reliability_report
    from greeceapt.scoring.yield_model import BASE_EXCLUDED_ASSUMPTIONS, YieldAssumptions
    from greeceapt.collect.opportunity_dedupe import load_decisions
    from greeceapt.collect.opportunity_registry import OpportunityRegistry, attach_opportunity_ids

    as_of = as_of or date.today()

    src = db_conductor.connect_updated_readonly()
    try:
        rows = db_conductor.get_all_listings_for_scoring(src)
        hood_counts = {
            str(name).strip(): count for name, count in db_conductor.get_neighborhood_counts(src)
        }
    finally:
        src.close()

    decisions = load_decisions(OPPORTUNITY_DECISIONS_JSON)
    candidates, violations = prepare_candidates(rows, decisions, is_tracked=live_ad_check())
    logger.info(
        "Candidates: %s DB rows → %s opportunities (%s URL guard violations).",
        len(rows), len(candidates), len(violations),
    )

    registry = OpportunityRegistry(OPPORTUNITY_REGISTRY_DB)
    try:
        attach_opportunity_ids(registry, candidates, now=datetime.now(timezone.utc))
        rentals_located = rental_reference()
        infer_neighborhoods(candidates, rentals_located)
        availability = apply_availability(candidates)
        extra_space = apply_living_area(candidates)
        logger.info("Living area: %s flats listed with an attic, terrace or storage in their area.", extra_space)
        apply_listing_filters(candidates, as_of=as_of)
        budgets = resolve_renovation_budgets(renovation_budgets, candidates, registry)

        baseline_rows = [
            row for row in candidates
            if row.get("visual_status_v2") == "accepted"
            and row.get("visual_score_v2") is not None
            and float(row["visual_score_v2"]) >= MIN_CONDITION_SCORE
            and not row.get("floor_conflict")
        ]
        baselines, thin_hoods, min_n = compute_neighborhood_baselines(baseline_rows)
        logger.info(
            "Sale baselines: %s neighborhoods (n >= %s); %s thin neighborhoods keep yield ranking "
            "without a market-discount field.", len(baselines), min_n, thin_hoods,
        )

        assumptions = YieldAssumptions()
        rent_model = load_rent_model()
        if rent_model is None:
            logger.warning("No data/rent_model.json — no flat can be priced, so none is ranked "
                           "(python -m greeceapt.rent train).")
        else:
            warn_on_vision_contract_mismatch(rent_model, candidates)
        ranked, unranked = rank_deals(
            candidates, baselines, assumptions, renovation_budgets=budgets, rent_model=rent_model,
        )
        sale_located = [
            (row.get("neighborhood"), row.get("latitude"), row.get("longitude")) for row in candidates
            if row.get("neighborhood") and row.get("latitude") is not None and row.get("longitude") is not None
        ]
        ranked = verify_ranked(
            ranked,
            {str(row.get("url")): row for row in candidates},
            sorted({str(row.get("neighborhood")) for row in candidates if row.get("neighborhood")}),
            neighborhood_centers([*rentals_located, *sale_located]),
        )
        by_url = {str(row.get("url")): row for row in candidates}

        def with_signals(deal):
            signals = seller_signals(by_url.get(deal.url, {}), as_of=as_of)
            return replace(deal, price_cut_pct=signals.price_cut_pct, days_on_market=signals.days_on_market,
                           first_published=signals.first_published)

        ranked = [with_signals(deal) for deal in ranked]
        unranked = [with_signals(deal) for deal in unranked]   # "on the market over 4 months" shows how long
        flagged = [deal for deal in ranked if deal.needs_check]
        logger.info("Verification: %s of %s ranked flats need a manual check.", len(flagged), len(ranked))

        hoods = sorted({str(row.get("neighborhood") or "").strip() for row in candidates} - {""})
        ranked_counts: dict[str, int] = {}
        for deal in ranked:
            ranked_counts[deal.neighborhood] = ranked_counts.get(deal.neighborhood, 0) + 1
        neighborhood_rows = [
            (
                name,
                hood_counts.get(name, 0),
                ranked_counts.get(name, 0),
                round(baselines[name], 1) if name in baselines else None,
                NEIGHBORHOOD_CANONICAL.get(name),
            )
            for name in hoods
        ]

        metadata = {
            "ranking": "within-neighborhood net yield after every cost and tax (rent model)",
            "as_of": as_of.isoformat(),
            "assumptions": assumptions.to_json(),
            "excluded_assumptions": list(BASE_EXCLUDED_ASSUMPTIONS) + ["renovation (unless supplied)"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "db_rows": len(rows), "opportunities": len(candidates),
                "ranked": len(ranked), "unranked": len(unranked),
                "url_guard_violations": len(violations),
            },
            "availability": availability,
            "renovation_budgets": budgets,
            "vision_reliability": reliability_report(candidates),
            "rent_model": rent_model_metadata(rent_model),
        }
        write_final_database_atomic(db_conductor.FINAL_DB, ranked, unranked, neighborhood_rows, metadata)
        registry.commit()
    except BaseException:
        registry.rollback()
        raise
    finally:
        registry.close()

    _track_rankings(as_of)
    logger.info(
        "final_deals.db: %s ranked in %s neighborhoods, %s unranked.",
        len(ranked), len({deal.neighborhood for deal in ranked}), len(unranked),
    )


def _track_rankings(as_of: date) -> None:
    """Snapshot this run's method ranks and check older snapshots against delistings."""
    from greeceapt.storage import db_conductor, paths
    from greeceapt.scoring import rank_history

    count = rank_history.record_snapshot(db_conductor.FINAL_DB, paths.RANK_HISTORY_DB, as_of)
    logger.info("Rank snapshot %s: %s flats → %s", as_of, count, paths.RANK_HISTORY_DB.name)
    try:
        rank_history.log_and_write()
    except (OSError, ValueError, KeyError) as exc:  # a bad sweep/listings file must not fail scoring
        logger.warning("Delisting check skipped: %s", exc)


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    run()
