"""Rental scrape, sample and model settings (no I/O)."""

from __future__ import annotations

import os

from greeceapt.storage.paths import DATA_DIR

RENTALS_DB = DATA_DIR / "rentals.db"
RENT_MODEL_JSON = DATA_DIR / "rent_model.json"
RENT_MODEL_REPORT = DATA_DIR / "rent_model_report.md"
# One line per training run: when, how many rentals, accuracy, every coefficient (trend tracking).
RENT_MODEL_HISTORY = DATA_DIR / "rent_model_history.jsonl"
# The full pipeline re-scrapes rentals and retrains once the model is this old.
REFRESH_AFTER_DAYS = int(os.getenv("RENT_REFRESH_DAYS", "30"))
# Train on rentals seen in a sweep within this window of the newest one: older ads are gone and
# their asking rents date from an older market.
TRAIN_WINDOW_DAYS = 183

# Spitogatos rental search: Athens-Center, broader than the buy segment so the size and
# floor effects are estimated on both sides of it.
RENT_SEARCH_PAYLOAD: dict = {
    "listingType": "rent",
    "category": "residential",
    "priceLow": 150,
    "priceHigh": 1500,
    "livingAreaLow": 18,
    "livingAreaHigh": 90,
    "areaIDs": [100],
    "withPhotos": True,
    "lastUpdateMonths": 6,
    "sortBy": "rankingscore",
    "sortOrder": "desc",
}
PAGE_SIZE = 30
SEARCH_CONCURRENCY = max(1, int(os.getenv("RENT_SEARCH_CONCURRENCY", "6")))
DETAIL_CONCURRENCY = max(1, int(os.getenv("RENT_DETAIL_CONCURRENCY", "8")))
MAX_OFFSET = int(os.getenv("RENT_MAX_OFFSET", "20000"))
SAVE_EVERY = 100

# Vision sample: the segment the buy side lives in, stratified by neighborhood.
SAMPLE_SIZE = int(os.getenv("RENT_SAMPLE_SIZE", "1500"))
SAMPLE_MIN_PER_HOOD = 8
SAMPLE_SEED = 20261105
SEGMENT_AREA = (18.0, 75.0)
SEGMENT_RENT = (200.0, 900.0)

# Training filter (sanity bounds on €/m²/month; outside is almost always a typo).
TRAIN_RENT_PSQM = (4.0, 40.0)

# Rebalanced sample: size bands weighted like the ranked sale flats (``final_deals.db``), so the
# condition effect is learned on flats like the ones being bought. Cheap rentals are scarce on
# the rental side and all go in first.
SIZE_BANDS = ((18.0, 30.0), (30.0, 40.0), (40.0, 50.0), (50.0, 60.0), (60.0, 75.0))
DEFAULT_BAND_SHARES = (0.23, 0.32, 0.22, 0.17, 0.06)  # ranked sale flats, 2026-11-06
CHEAP_RENT = 400.0
