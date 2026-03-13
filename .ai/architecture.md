# GreeceApt — Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│                      USER / BROWSER                      │
│  - Solves CAPTCHA manually when prompted                 │
│  - Confirms verification is complete via terminal        │
└────────────────────────┬────────────────────────────────┘
                         │ (interactive, one-time)
                         ▼
┌─────────────────────────────────────────────────────────┐
│              cookie_manager.py                           │
│  Playwright browser → captures session cookies           │
│  Output: data/cookies.json                               │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              scrape_xe.py                                │
│  1. Load cookies, build XE.gr search URL                 │
│  2. Paginate search results (scroll + Next button)       │
│  3. Collect listing URLs (dedup by normalized path)      │
│  4. Fetch listing detail pages (async, max 5 concurrent) │
│  5. Parse HTML → structured listing dicts                │
│  6. Merge with existing listings.json (dedup by url)     │
│  Output: data/listings.json, data/state.json             │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              ingest.py                                   │
│  1. Load listings.json                                   │
│  2. Canonicalize neighborhoods (147-entry map)           │
│  3. Split Ano/Kato/Nea/Neo prefix → area column          │
│  4. INSERT OR REPLACE into listings.db                   │
│  Output: data/listings.db                                │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              create_updated_db.py                        │
│  1. Read listings.db (read-only)                         │
│  2. Compute IQR-trimmed market median per neighborhood   │
│  3. Classify each listing (deal/not_deal/needs_review/…) │
│  4. Insert qualifying rows into db_updated.db            │
│  5. Run SQL UPDATE to compute final_score                 │
│  Output: data/db_updated.db                              │
└─────────────────────────────────────────────────────────┘
```

---

## Module Relationships

```
scrape_xe.py
  └── uses: cookie_manager.py (load_cookies)
  └── uses: url_builder.py (build_xe_url)
  └── uses: helpers.py (normalize_listing_url)

ingest.py
  └── uses: db/core.py (create_tables, insert_listings)
  └── uses: helpers.py (extract_area_prefix, strip_area_prefix)

create_updated_db.py
  └── standalone — reads listings.db via sqlite3 directly

db/core.py
  └── uses: helpers.py (normalize_listing_url)

db/areas_external.py
  └── uses: db/core.py (get_connection)
  └── NOTE: NOT used by the main pipeline — manual/ad-hoc utility only
```

---

## Database Schema

### `data/listings.db` — Raw Store

```sql
CREATE TABLE listings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT UNIQUE,          -- normalized, dedup key
    headline         TEXT,
    price_eur        REAL,
    price_per_sqm    REAL,
    area_sqm         REAL,
    neighborhood     TEXT,                 -- canonical name (post-ingest)
    area             TEXT,                 -- Ano/Kato/Nea/Neo only (or NULL)
    address_raw      TEXT,
    bedrooms         INTEGER,
    bathrooms        INTEGER,
    floor            INTEGER,
    year_built       INTEGER,
    renovation_year  INTEGER,
    energy_class     TEXT,
    photos_count     INTEGER,
    photo_urls_json  TEXT,                 -- JSON array of photo URLs
    publication_date TEXT,                 -- ISO date (YYYY-MM-DD)
    scraped_at       TEXT,                 -- ISO datetime
    raw_json         TEXT,                 -- full original dict (audit trail)
    updated_at       TEXT,                 -- last upsert timestamp
    neighborhood_score INTEGER
);
```

### `data/db_updated.db` — Scored Output

All columns from `listings.db` plus:

```sql
neighborhood_canon        TEXT,   -- same as neighborhood (post-canon)
neighborhood_mod          TEXT,   -- Ano/Kato/Nea/Neo/Palaio (scoring modifier)
listing_age_days          INTEGER,
listing_psqm              REAL,   -- price_eur / area_sqm
neighborhood_market_psqm  REAL,   -- IQR-trimmed median for that neighborhood
final_score               REAL,   -- 0–100 investment score
status                    TEXT,   -- deal / needs_review / no_market / unknown_neighborhood
used_for_market           INTEGER -- 1 if contributed to market median
```

Also contains:
```sql
CREATE TABLE areas_external (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    neighborhood       TEXT UNIQUE,
    neighborhood_score INTEGER
);
```

---

## Data Flow Detail

### Scraper Detail Flow

```
build_xe_url()
    → Playwright navigates to search results (domcontentloaded)
    → Scroll to bottom (600ms interval) to trigger lazy-loaded ads
    → Collect all <a> href listing URLs from the page
    → Check for "Next" button → navigate to next page
    → Save state.json (last_page)
    → For each new URL (not in existing listings.json):
        → asyncio.Semaphore(5) → open detail page (domcontentloaded)
        → Wait for CSS selectors: characteristics, basic-info, statistics
        → BeautifulSoup parse → extract all fields
        → If group listing: resolve group → pick lowest price variant
    → Merge results with existing listings.json (keyed by normalized url)
```

### Scoring Flow

```
For each neighborhood in TOP_NEIGHBORHOODS:
    1. Pull all recent (180d), valid (≥5 photos) listings
    2. Compute psqm = price_eur / area_sqm
    3. Sort → IQR trim (K=2, skip if <8 samples)
    4. Remove listings below 70% of raw median
    5. market_psqm = median(clean set)

For each candidate listing:
    listing_psqm = price_eur / area_sqm
    if listing_psqm < 50% of median → broken_candidate (skip)
    if listing_psqm < 70% of median → needs_review (insert)
    if listing_psqm ≤ 80% of market_psqm → deal (insert)
    else → not_deal (skip)

SQL UPDATE sets final_score:
    0.36 × neighborhood_score      (0–100, from NEIGHBORHOOD_SCORES dict)
  + 0.32 × discount_score         (0–100, how far below market psqm)
  + 0.19 × floor_score            (basement=20 → 5th+=98)
  + 0.13 × size_score             (25–55sqm=100, sweet spot)
  + mod_adj                       (Ano=+2, Kato=−2, flat)
  + renovation_bonus              (≥2020=+6, ≥2010=+3, flat)
```

---

## Key Design Decisions

| Decision | Reason |
|----------|--------|
| Two separate DBs (raw + scored) | Audit trail: raw data is never destroyed by the scoring run |
| `INSERT OR REPLACE` on url | Idempotent ingestion — re-running ingest is safe |
| db_updated.db rebuilt from scratch each run | Scoring parameters change frequently; stale rows cause confusion |
| `domcontentloaded` instead of `networkidle` | 3–5× faster scraping; XE.gr's dynamic content is not needed |
| IQR trimming for market median | Prevents a few data errors from making a whole neighborhood look cheap |
| Manual CAPTCHA, not automation | XE.gr bot detection is sophisticated; human solve is reliable |
| Neighborhood canonicalization at ingest | Keeps scoring logic clean; scoring only sees canonical names |
| Flat renovation/mod bonuses | Small adjustment that doesn't distort the weighted score |
