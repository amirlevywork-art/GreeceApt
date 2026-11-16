"""Filesystem locations for all pipeline SQLite databases and the data directory."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"

INGESTED_DB = DATA_DIR / "ingested_listings.db"
UPDATED_DB = DATA_DIR / "updated_listings.db"
FINAL_DB = DATA_DIR / "final_deals.db"

# Merge + scraper JSON (single source for subprocesses and pipeline)
LISTINGS_JSON = DATA_DIR / "listings.json"
DELETED_LISTINGS_JSON = DATA_DIR / "deleted_listings.json"
STALE_LISTINGS_JSON = DATA_DIR / "stale_listings.json"
XE_LISTINGS_JSON = DATA_DIR / "xe_listings.json"
SPITOGATOS_LISTINGS_JSON = DATA_DIR / "spitogatos_listings.json"
# Written when a Spitogatos search sweep reaches its last page (see collect.spitogatos.scrape).
SPITOGATOS_SWEEP_JSON = DATA_DIR / "spitogatos_sweep.json"
# Summary of the latest Spitogatos collector run, trusted or not (counts and changed URLs).
SPITOGATOS_LAST_RUN_JSON = DATA_DIR / "spitogatos_last_run.json"
# The manual ``./update`` command: one line per run, the latest "what changed" report,
# its lock, and the data copies it takes before touching anything.
UPDATE_LOG_JSONL = DATA_DIR / "update_log.jsonl"
UPDATE_REPORT_MD = DATA_DIR / "update_report.md"
UPDATE_LOCK = DATA_DIR / "update.lock"
BACKUPS_DIR = DATA_DIR / "backups"
# Per-run snapshots of every ranking method, for checking them against later delistings.
RANK_HISTORY_DB = DATA_DIR / "rank_history.sqlite3"
RANK_HISTORY_REPORT = DATA_DIR / "rank_history_report.md"
# Standalone {photo_url: phash} cache. Survives listings.json being rebuilt or wiped,
# so a fresh scrape does not re-download every image just to re-hash it.
PHASH_CACHE_JSON = DATA_DIR / "phash_cache.json"
# Opportunity collapse: pending duplicate reviews, human decisions, persistent identities.
OPPORTUNITY_REVIEW_JSON = DATA_DIR / "opportunity_review.json"
OPPORTUNITY_DECISIONS_JSON = DATA_DIR / "opportunity_decisions.json"
OPPORTUNITY_REGISTRY_DB = DATA_DIR / "opportunity_registry.sqlite3"


def ensure_data_dir() -> None:
    """Create ``data/`` under the project root if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
