# GreeceApt — Project Context

## Purpose
Athens apartment deal finder. Scrapes XE.gr for listings in the €30,000–€60,000 range,
ingests them into SQLite, computes neighborhood investment scores, and produces a filtered
database of properties priced significantly below the local market median.

Target: Studios and 1BR apartments (25–55 sqm) suitable for long-term rental (LTR) in Athens.

---

## Tech Stack

| Layer       | Technology |
|-------------|------------|
| Scraping    | Playwright (async), BeautifulSoup + lxml |
| Storage     | SQLite (two separate databases) |
| Language    | Python 3.11+ |
| Project     | `src/` layout, no framework |

---

## Main Files and Responsibilities

```
src/greeceapt/
├── scraper/scrape_xe.py          # XE.gr scraper — pagination, detail fetch, state
├── cookies/cookie_manager.py     # Cookie capture, loading, expiry detection
├── utils/helpers.py              # URL normalization, area prefix extraction
├── utils/url_builder.py          # Builds XE.gr search URL with query params
├── db/core.py                    # listings.db schema, insert logic
├── pipeline/ingest.py            # JSON → listings.db with neighborhood canonicalization
└── db/create_updated_db.py       # listings.db → db_updated.db with scoring & filtering
```

---

## Data Pipeline

```
Stage 1 — Cookie Capture (one-time / on expiry)
  cookie_manager.py → data/cookies.json
  Interactive: user solves CAPTCHA in real browser window

Stage 2 — Scraping
  scrape_xe.py reads cookies.json, paginates XE.gr search results,
  concurrently fetches listing detail pages (max 5 at once),
  writes/merges output to data/listings.json
  Resumable via data/state.json (tracks last_page)

Stage 3 — Ingestion
  ingest.py loads listings.json,
  canonicalizes neighborhood names (147-entry map),
  splits Ano/Kato/Nea/Neo prefix into the `area` column,
  inserts/replaces into data/listings.db

Stage 4 — Scoring & Filtering
  create_updated_db.py reads listings.db (read-only),
  computes IQR-trimmed market median psqm per neighborhood,
  classifies each listing (deal / not_deal / needs_review / broken_candidate / no_market),
  scores deals on 4 weighted factors + flat renovation bonus,
  writes data/db_updated.db (only deal / needs_review / no_market / unknown_neighborhood rows)
```

---

## Runtime Data Files

| File                  | Purpose |
|-----------------------|---------|
| `data/cookies.json`   | XE.gr browser session cookies |
| `data/state.json`     | Last scraped page number (resume state) |
| `data/listings.json`  | Raw scraped listings (JSON array) |
| `data/listings.db`    | SQLite: all ingested raw listings |
| `data/db_updated.db`  | SQLite: filtered + scored investment targets |

---

## Run Order

```bash
# First run only (or when cookies expire):
python -m greeceapt.cookies.cookie_manager

# Each scrape session:
python -m greeceapt.scraper.scrape_xe
python -m greeceapt.pipeline.ingest
python -m greeceapt.db.create_updated_db
```

Set `PYTHONPATH=src` (or use `pip install -e .`) before running.
