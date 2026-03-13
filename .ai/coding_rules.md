# GreeceApt — Coding Rules

## Language and Style

- Python 3.11+. Use `X | Y` unions, `list[T]` / `dict[K,V]` generics — no `from typing import Optional/Dict/List`.
- `from __future__ import annotations` is used in pipeline files — keep it there.
- Private module-level constants use a leading underscore (e.g., `_SCHEMA`, `_build_score_update_sql`).
- No docstrings on trivial functions. Add docstrings only when the logic is non-obvious.
- Add comments on SQL blocks and scoring formulas — these are complex and need explanation.
- No backwards-compatibility shims (renaming unused vars, re-exporting removed names, etc.).

---

## Database Rules

### listings.db (raw store)
- Schema lives in `db/core.py` — `create_tables()` creates it with `CREATE TABLE IF NOT EXISTS`.
- Inserts use `INSERT OR REPLACE` keyed on `url TEXT UNIQUE` (normalized URL).
- `normalize_xe_item()` maps raw scraper dicts to DB row format — all field mapping happens here.
- Never skip url normalization. Call `normalize_listing_url()` from `helpers.py` on every URL before insert.
- `raw_json` column stores the full original dict as JSON — preserve it for debugging.

### db_updated.db (scored output)
- Built fresh every run (`dst_db.unlink()` first) — never accumulate or merge.
- Source DB is opened read-only (`uri=True`, `mode=ro`).
- Column presence is always checked via `PRAGMA table_info` before use — no assumptions.
- Only insert rows with status: `deal`, `needs_review`, `no_market`, `unknown_neighborhood`.
- Never insert `not_deal` or `broken_candidate` — they are counted only.

---

## Neighborhood / Location Rules

- **neighborhood**: canonical name only. No prefixes (Ano/Kato/Nea/Neo), no junk.
- **area**: stores ONLY valid modifiers: `Ano`, `Kato`, `Nea`, `Neo`, `Palaio`.
- **Agia / Agios / Agioi are NOT prefixes** — never strip them from the neighborhood name.
- All spelling variants must be mapped through `NEIGHBORHOOD_CANONICAL` in `ingest.py`.
- Canonical names in `ingest.py` must exactly match the keys in `NEIGHBORHOOD_SCORES` in `create_updated_db.py`.
- When adding a new neighborhood: add it to both maps in both files simultaneously.

---

## Scraper Rules

- Bot verification check (`is_verification_page`) must be called before any user-prompt about timeouts.
- Never change `wait_until="domcontentloaded"` back to `"networkidle"` for listing detail pages — this was the key speedup.
- Concurrent detail fetches are limited to `DETAIL_CONCURRENCY = 5` via `asyncio.Semaphore` — do not increase.
- Group URL resolution uses a separate semaphore of 3 — keep it lower than detail concurrency.
- `photos_count` should fall back to `len(photo_urls)` if the gallery CSS selector fails.
- Pagination state (`state.json`) must be written after each page — enables resumable runs.
- URL deduplication is done by normalized path (query params stripped by `normalize_listing_url`).

---

## Scoring Rules

- Weights must always sum to 1.00:
  - `W_HOOD=0.36`, `W_DISCOUNT=0.32`, `W_FLOOR=0.19`, `W_SIZE=0.13`
- Renovation bonus is flat (not weighted): +6 for reno ≥ 2020, +3 for reno ≥ 2010.
- Area modifier (Ano/Kato) is flat (not weighted): Ano=+2, Kato=−2.
- `year_built`, `listing_age`, and `energy_class` are intentionally excluded from scoring.
- `_build_score_update_sql()` builds the entire UPDATE SQL — do not inline SQL into `create_updated_db()`.
- All score components use SQL `CASE` expressions with `NULL`-safe fallback to a neutral value (typically 50).

---

## Market Computation Rules

- Market median uses IQR trimming (`IQR_K=2`, `IQR_MIN_N=8` minimum samples).
- Listings below `BROKEN_FACTOR × median_raw` (0.70) are excluded from the clean set.
- Listings below `SUSPECT_FACTOR × median_raw` (0.50) are marked `broken_candidate`.
- `DEAL_MAX_RATIO = 0.80` — listings must be ≤ 80% of market_psqm to qualify as a deal.
- Only listings with `used_for_market=1` contributed to the market benchmark.
- Do not change `RECENT_DAYS=180` or `MIN_PHOTOS=5` without understanding the knock-on effect on sample sizes per neighborhood.

---

## What Not to Do

- Do not add error handling for internal invariants (e.g., don't `try/except` around `NEIGHBORHOOD_SCORES[hood]` — if a key is missing it should crash loudly).
- Do not add features not requested — no extra columns, no new export formats, no optional flags.
- Do not create new files for one-time logic — extend an existing module.
- Do not remove `raw_json` from the listings table — it is the audit trail.
- Do not touch `areas_external.py` functions from the main pipeline — they are manual-use utilities only.
