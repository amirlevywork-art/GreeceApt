"""Spitogatos API URLs, search payload, and concurrency (no I/O)."""

from __future__ import annotations

import asyncio
import os

from greeceapt.storage.paths import (
    SPITOGATOS_LAST_RUN_JSON,
    SPITOGATOS_LISTINGS_JSON,
    SPITOGATOS_SWEEP_JSON,
)

LISTINGS_JSON = SPITOGATOS_LISTINGS_JSON
SWEEP_JSON = SPITOGATOS_SWEEP_JSON
LAST_RUN_JSON = SPITOGATOS_LAST_RUN_JSON

SEARCH_URL = (
    "https://www.spitogatos.gr/en/for_sale-homes/athens-center/"
    "with_photo/last_update_6months/maxprice-70000"
)
SEARCH_API = "https://www.spitogatos.gr/n_api/v1/properties/search-results"
DETAIL_API = "https://www.spitogatos.gr/n_api/v1/properties/{id}"

SEARCH_PAYLOAD: dict = {
    "listingType": "sale",
    "category": "residential",
    "priceLow": 30000,
    "priceHigh": 70000,
    "areaIDs": [100],
    "withPhotos": True,
    "lastUpdateMonths": 6,
    "sortBy": "rankingscore",
    "sortOrder": "desc",
}

DETAIL_SEM = asyncio.Semaphore(max(1, int(os.getenv("SPITOGATOS_DETAIL_CONCURRENCY", "12"))))
# Detail fetches run in batches of this size; the store is saved after each batch.
ROTATE_EVERY = int(os.getenv("SPITOGATOS_ROTATE_EVERY", "100"))
# Set SPITOGATOS_MAX_OFFSET=30 to fetch only 1 page (offset 0) for testing.
MAX_OFFSET = int(os.getenv("SPITOGATOS_MAX_OFFSET", "10000"))
PAGE_SIZE = 30
# Exit code of the collector CLI when the site refused it (HTTP 403/405/429).
EXIT_BLOCKED = 3
# A search page that fails (timeout, 5xx) is asked again this many times in all; a refusal
# (HTTP 403/405/429) is never asked again.
PAGE_ATTEMPTS = 3

# A sweep may mark stored advertisements as gone only when it is trusted: it saw at least
# MIN_COVERAGE of the site's own result count and at least MIN_VS_PREVIOUS of what the previous
# trusted sweep saw. Result pages shift while they are read, so a pass that comes up short is
# repeated (MAX_PASSES in all) and the passes are united.
MIN_COVERAGE = 0.98
MIN_VS_PREVIOUS = 0.70
MAX_PASSES = 2
