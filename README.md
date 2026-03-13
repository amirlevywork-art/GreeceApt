# GreeceApt

Athens apartment listings scraper and deal finder. Scrapes XE.gr, ingests to SQLite, and builds a filtered/scored database of investment opportunities.

## Structure

```
src/greeceapt/
  scraper/     # XE.gr scraping
  cookies/     # Cookie capture for XE
  utils/       # URL builder
  db/          # listings.db schema, inserts, areas_external
  pipeline/    # ingest (JSON→DB), create_updated_db (filtered deals)

data/          # Runtime artifacts only (JSON, SQLite, cookies, state)
```

## Pipeline

1. **Scrape** → `data/listings.json`
2. **Ingest** → `data/listings.db`
3. **Create updated DB** → `data/db_updated.db` (filtered deals with scores)

## Run (from project root)

```bash
# Ensure dependencies and PYTHONPATH
export PYTHONPATH=src

# 1. Capture cookies (first time or when expired)
python -m greeceapt.cookies.cookie_manager

# 2. Scrape listings
python -m greeceapt.scraper.scrape_xe

# 3. Ingest JSON → SQLite
python -m greeceapt.pipeline.ingest

# 4. Build filtered deal DB
python -m greeceapt.pipeline.create_updated_db
```

## Dependencies

- playwright
- beautifulsoup4
