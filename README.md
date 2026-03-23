# GreeceApt

An Athens apartment deal finder. Scrapes XE.gr for apartments in the **€30,000–€60,000** range, stores them in SQLite, and ranks them using a scoring model built around neighborhood investment grade and discount from the local market median.

The goal is to surface underpriced properties suitable for long-term rental (LTR) — studios and 1BR apartments (25–55 sqm) in strong Athens neighborhoods.

---

## Why I Built This

Athens has one of the most interesting small-apartment investment markets in Europe right now — high rental yields, ongoing gentrification in several neighborhoods, and prices that are still recovering from the 2010s crisis. But sifting through hundreds of listings manually to find the real deals is time-consuming. This tool automates that process.

---

## Tech Stack

- **Python 3.11+**
- **Playwright** — async browser automation for scraping (handles cookies, scrolling, bot checks)
- **BeautifulSoup + lxml** — HTML parsing
- **SQLite** — two-stage storage (raw listings DB + scored output DB)

---

## How It Works

```
1. Cookie Capture    → Authenticate with XE.gr via a real browser session
2. Scrape            → Paginate search results + fetch listing detail pages (async, 5 concurrent)
3. Ingest            → Normalize neighborhoods + load into SQLite
4. Score & Filter    → Compute market medians, classify deals, write scored output DB
```

Each stage outputs an artifact that feeds the next. The scraper is resumable — it tracks the last page scraped in `data/state.json`.

---

## Project Structure

```
src/greeceapt/
├── scraper/            # XE.gr scraper (pagination, detail fetch, bot detection)
├── cookies/            # Cookie capture and session management
├── utils/              # URL normalization, area prefix helpers, URL builder
├── db/                 # listings.db schema, insert logic, scoring pipeline
└── pipeline/
    └── ingest.py            # JSON → listings.db (with neighborhood canonicalization)

data/                   # Runtime files — not committed (see .gitignore)
.ai/                    # AI agent context (architecture, coding rules, project notes)
```

---

## Setup

**1. Clone and create a virtual environment:**

```bash
git clone https://github.com/your-username/GreeceApt.git
cd GreeceApt
python3 -m venv venv
source venv/bin/activate
```

**2. Install dependencies:**

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Running the Pipeline

```bash
export PYTHONPATH=src

# Step 1: Capture XE.gr session cookies (first run, or when expired)
python -m greeceapt.cookies.cookie_manager

# Step 2: Scrape listings → data/listings.json
python -m greeceapt.scraper.scrape_xe

# Step 3: Ingest into SQLite → data/listings.db
python -m greeceapt.pipeline.ingest

# Step 4: Build scored deal database → data/db_updated.db
python -m greeceapt.db.create_updated_db
```

Steps 2–4 can be re-run any time. Step 1 only needs to be repeated when cookies expire.

---

## Scoring Model

Each listing in the output DB receives a `final_score` (0–100):

| Factor | Weight | What it measures |
|---|---|---|
| Neighborhood grade | 36% | Investment tier (96 for Kypseli → 45 for Kolonaki) |
| Market discount | 32% | How far below the local median price/sqm |
| Floor | 19% | Higher floors score better (basement = 20, 5th+ = 98) |
| Size | 13% | Sweet spot is 25–55 sqm for LTR studios/1BR |
| Renovation bonus | flat | +6 if renovated ≥2020, +3 if ≥2010 |
| Area modifier | flat | +2 for Ano (upper), −2 for Kato (lower) |

**Deal classification:**
- `deal` — priced ≤80% of neighborhood market median
- `needs_review` — psqm < 70% of median (suspicious, worth checking manually)
- `no_market` — not enough comparable listings to compute a benchmark
- `unknown_neighborhood` — neighborhood not in the scoring list

Listings above the deal threshold (`not_deal`) and clear data errors (`broken_candidate`) are excluded from the output DB.

---

## Notes

- The scraper uses real browser cookies to avoid bot detection. The cookie capture step opens a real browser window where you may need to solve a CAPTCHA.
- All data files (`cookies.json`, `listings.db`, `db_updated.db`) are gitignored — they stay local only.
- The neighborhood scoring table covers 22 Athens neighborhoods across 4 investment tiers.
