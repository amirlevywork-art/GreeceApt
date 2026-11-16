"""Freeze the anonymised November 2026 replay input used by the ranking tests.

Reads a production ``data/`` directory read-only and writes
``tests/fixtures/november_ranking_input_2026.json``: every scored listing (``listings``) plus
the listings Layer 2 removed only for being in a thin neighborhood, with just the fields the
floor resolver, dedupe, vision and yield code need.

Nothing the agencies wrote is copied: the ad's title and description are left out (only what
the ad says about the condition — "Renovated" / "Needs renovation" — is kept), and photo URLs
become stable opaque ids. Cookies, raw API payloads and images are never copied.

Usage:
    python scripts/freeze_november_fixture.py --data-dir PATH --out-dir tests/fixtures
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from greeceapt.scoring.condition_text import publisher_condition

LISTING_FIELDS = (
    "url", "source", "source_urls", "neighborhood", "municipality", "price_eur", "area_sqm",
    "floor", "condition", "bedrooms", "bathrooms", "year_built", "energy_class",
    "publication_date", "earliest_publication_date", "latest_publication_date", "scraped_at",
    "latitude", "longitude", "photo_urls", "photo_fingerprints",
)
VISION_COLUMNS = ("visual_status_v2", "visual_score_v2", "visual_confidence_v2")
_CONDITION_WORDS = {"needs_renovation": "Needs renovation", "renovated": "Renovated"}


def photo_id(url: str) -> str:
    return "photo-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _listing_record(raw_json: str | None, row: sqlite3.Row, removal_reason: str | None) -> dict:
    raw = json.loads(raw_json or "{}")
    record = {field: raw.get(field) for field in LISTING_FIELDS}
    # The DB columns are what Layers 0–2 actually saw; prefer them over the scrape dict.
    for column in ("url", "source", "neighborhood", "price_eur", "area_sqm", "floor",
                   "bedrooms", "bathrooms", "year_built", "scraped_at", "condition"):
        if column in row.keys() and row[column] is not None:
            record[column] = row[column]
    for column in VISION_COLUMNS:
        record[column] = row[column] if column in row.keys() else None
    features = json.loads(row["layer_1_features_v2"]) if row["layer_1_features_v2"] else {}
    record["visual_accepted_count"] = features.get("accepted_count")
    if record.get("source") == "spitogatos" and record.get("floor") is not None:
        # Spitogatos floorNumber is an offset code, not a storey; keep it explicitly.
        record["floor_raw_code"] = int(float(record["floor"]))
    # What the ad says about the condition, without its text.
    record["condition"] = _CONDITION_WORDS.get(publisher_condition({**raw, **record}))
    record["title"] = record["description"] = ""
    record["photo_urls"] = [photo_id(u) for u in record.get("photo_urls") or []]
    record["photo_fingerprints"] = {photo_id(k): v for k, v in (record.get("photo_fingerprints") or {}).items()}
    record["removal_reason"] = removal_reason
    return record


def freeze(data_dir: Path, out_dir: Path) -> int:
    updated = sqlite3.connect(f"file:{data_dir / 'updated_listings.db'}?mode=ro", uri=True)
    updated.row_factory = sqlite3.Row
    try:
        rows = [
            _listing_record(row["raw_json"], row, None)
            for row in updated.execute("SELECT * FROM listings ORDER BY url")
        ]
        rows += [
            _listing_record(row["raw_json"], row, row["removal_reason"])
            for row in updated.execute(
                "SELECT * FROM removed_listings WHERE removal_reason LIKE 'Thin neighborhood%' "
                "ORDER BY url"
            )
        ]
    finally:
        updated.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "november_ranking_input_2026.json").write_text(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return len(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("tests/fixtures"))
    args = parser.parse_args(argv)
    print(f"froze {freeze(args.data_dir, args.out_dir)} listings into {args.out_dir}")


if __name__ == "__main__":
    main()
