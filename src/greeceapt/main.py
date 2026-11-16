"""
main.py — Top-level pipeline orchestrator.

  1. Scrape — ``run_all``: Spitogatos (XE is no longer collected) →
     floors resolved → photo dedupe → opportunity collapse → ``data/listings.json`` (with
     persistent opportunity IDs).
  2. Ingest — ``listings.json`` → ``data/ingested_listings.db``.
  3. AI — Layer 0 (SQL metadata), Layer 1 (Qwen V2), Layer 2 (condition gate); then a monthly
     rent-model refresh (rentals re-scraped and retrained once the model is 30 days old).
  4. Scoring — opportunity re-collapse, within-neighborhood net-yield ranking, trap checks
     → ``data/final_deals.db`` (published atomically).

CLI:
  python -m greeceapt.main [--as-of YYYY-MM-DD] [--renovation-budgets PATH]
  python -m greeceapt.main --score-only [--as-of YYYY-MM-DD] [--renovation-budgets PATH]

``--as-of`` is the run date recorded in the results (default: today, captured once at start). ``--renovation-budgets`` is a JSON object keyed by opportunity ID or
any member listing URL, with finite non-negative euro amounts. Both are validated before any
stage runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path

from greeceapt.logging_config import configure_root_logging

configure_root_logging()
logger = logging.getLogger(__name__)


def run_score_only(*, as_of: date, renovation_budgets: dict[str, float]) -> None:
    """Stage 4 only: read ``updated_listings.db``, write ``final_deals.db``."""
    logger.info("=== GreeceApt: Scoring only (Stage 4), as_of=%s ===", as_of)
    from greeceapt.scoring.scoring_conductor import run as scoring_run

    scoring_run(as_of=as_of, renovation_budgets=renovation_budgets)
    logger.info("=== Scoring complete ===")


def run_full_pipeline(
    *, as_of: date, renovation_budgets: dict[str, float], interim_every: int = 200
) -> None:
    logger.info("=== GreeceApt Pipeline Start (as_of=%s) ===", as_of)

    logger.info("--- Stage 1: Scrape (enabled sources, deduplicated) ---")
    from greeceapt.collect.run_all import main as scrape_all

    asyncio.run(scrape_all())

    logger.info("--- Stage 2: Ingest ---")
    from greeceapt.storage.ingest import load_json
    from greeceapt.storage import insert_listings

    listings = load_json()
    insert_listings(listings)

    logger.info("--- Stage 3: AI ---")
    from greeceapt.photos.ai_conductor import run as ai_run
    from greeceapt.scoring.scoring_conductor import run as scoring_run

    def interim(scored: int) -> None:
        logger.info("--- Interim ranking after %s Layer 1 listings ---", scored)
        scoring_run(as_of=as_of, renovation_budgets=renovation_budgets)

    ai_run(interim_every=interim_every, on_interim=interim)

    logger.info("--- Stage 3b: Rent model (monthly refresh) ---")
    from greeceapt.rent.refresh import refresh_if_due

    refresh_if_due()

    logger.info("--- Stage 4: Score ---")

    scoring_run(as_of=as_of, renovation_budgets=renovation_budgets)

    logger.info("=== GreeceApt Pipeline Complete ===")


def _parse_as_of(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--as-of must be YYYY-MM-DD, got {value!r}") from exc


def load_renovation_budgets(path: Path) -> dict[str, float]:
    """JSON object {opportunity_id or listing URL: finite non-negative euros}."""
    def reject_constant(name: str) -> float:
        raise ValueError(f"{name} is not a valid amount")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read renovation budgets from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("renovation budgets must be a JSON object")
    budgets: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"renovation budget for {key!r} must be a number")
        amount = float(value)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError(f"renovation budget for {key!r} must be finite and non-negative")
        budgets[str(key)] = amount
    return budgets


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="GreeceApt full pipeline or scoring-only.")
    parser.add_argument(
        "--score-only",
        action="store_true",
        help="Run only scoring (Stage 4). Expects data/updated_listings.db from prior AI run.",
    )
    parser.add_argument(
        "--as-of", type=_parse_as_of, default=None,
        help="Run date recorded in the results (default: today, captured once).",
    )
    parser.add_argument(
        "--interim-every", type=int, default=200,
        help="Full pipeline: publish an interim ranking every N Layer 1 listings (0 = off).",
    )
    parser.add_argument(
        "--renovation-budgets", type=Path, default=None,
        help="JSON object keyed by opportunity ID or listing URL with non-negative euro amounts.",
    )
    args = parser.parse_args(argv if argv is not None else None)

    as_of = args.as_of or date.today()
    budgets: dict[str, float] = {}
    if args.renovation_budgets is not None:
        try:
            budgets = load_renovation_budgets(args.renovation_budgets)
        except ValueError as exc:
            parser.error(str(exc))

    if args.score_only:
        run_score_only(as_of=as_of, renovation_budgets=budgets)
    else:
        if args.interim_every < 0:
            parser.error("--interim-every must be >= 0")
        run_full_pipeline(as_of=as_of, renovation_budgets=budgets, interim_every=args.interim_every)


if __name__ == "__main__":
    main(sys.argv[1:])
