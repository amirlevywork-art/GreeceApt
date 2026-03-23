# update_db.py
#
# Build a filtered/scored DB from data/listings.db -> data/db_updated.db
#
# Market estimation  = only clean comparable listings (IQR-trimmed, recency filtered)
# Deal hunting       = ALL listings, including suspicious ones
#
# Output DB adds:
#   neighborhood_canon, neighborhood_mod, listing_age_days,
#   listing_psqm, neighborhood_market_psqm, final_score,
#   status, used_for_market
#
# status values:
#   deal               - below market_psqm * DEAL_MAX_RATIO (good deal)
#   not_deal           - above deal threshold but valid
#   needs_review       - psqm < BROKEN_FACTOR * median_raw (suspicious, keep for review)
#   broken_candidate   - psqm < SUSPECT_FACTOR * median_raw (very likely data error)
#   no_market          - neighborhood has insufficient comparables
#   unknown_neighborhood - not in TOP_NEIGHBORHOODS
#
# used_for_market = 1 if listing was included in the neighborhood benchmark computation
#
# Scoring weights:
#   Neighborhood 36%, Discount 32%, Floor 19%, Size 13%

from __future__ import annotations

import sqlite3
from pathlib import Path
from datetime import datetime, date
from statistics import median



# ============================================================
# Neighborhood investment scores (CANONICAL names only)
# ============================================================
NEIGHBORHOOD_SCORES = {
    # ------------------------------------------------------------
    # TIER 1: "The Cash-Flow & Growth Kings" (ציון 90+)
    # ------------------------------------------------------------
    "Kypseli": 98,           # מנצחת השוק: שילוב של תשואה גבוהה (5.4%+), ג'נטריפיקציה ומטרו 4.
    "Agios Panteleimonas": 92, # Deep Value: מחירי כניסה נמוכים (€1,850/מ"ר) המאפשרים תשואה מקסימלית.

    # ------------------------------------------------------------
    # TIER 2: "Strong Momentum" (ציון 85-89)
    # ------------------------------------------------------------
    "Neos Kosmos": 89,       # פוטנציאל עליית ערך אדיר בשל קרבה לקוקאקי וצמיחה של 9%-12% בשנה.
    "Kolonos": 88,           # מנוע צמיחה מערבי: מחירי כניסה נוחים עם עלייה יציבה בביקוש.
    "Zografou": 86,          # חסינות מיתון: ביקוש קשיח של סטודנטים וסגל רפואי, נהנית מהרחבת המטרו.
    "Exarcheia": 85,         # מוקד משיכה לנוודים דיגיטליים; תנופת בנייה סביב תחנת המטרו החדשה.

    # ------------------------------------------------------------
    # TIER 3: "Safe Haven & Blue Chip" (ציון 70-84)
    # ------------------------------------------------------------
    "Pagkrati": 80,          # נזילות מקסימלית: "דיל" כאן נמכר תוך ימים, אך קשה למצוא מציאות מתחת ל-60k.
    "Ionia": 64,             # אזור משפחתי סולידי, פחות פוטנציאל לזינוק במחיר ביחס למרכז.
    "Agia Paraskevi": 60,    # פרבר מבוקש, אך התשואות נמוכות יותר מהמרכז בתקציב נתון זה.

    # ------------------------------------------------------------
    # TIER 4: "Premium but Low Yield" (ציון מתחת ל-60)
    # ------------------------------------------------------------
    "Smyrni": 67,            # שכונה יוקרתית; ב-60k קשה למצוא נכס שאינו קומת מרתף.
    "Kolonaki": 45,          # יוקרה מקסימלית, אך התשואה הנמוכה בעיר (~3.9%) והמחיר למ"ר הגבוה ביותר.
}
TOP_NEIGHBORHOODS = list(NEIGHBORHOOD_SCORES.keys())


# ============================================================
# Filters / knobs
# ============================================================
RECENT_DAYS = 180

# Photos filter is used only if photos_count exists
MIN_PHOTOS = 5  # >= MIN_PHOTOS

# Suspicious low-price thresholds (as fraction of neighborhood median_raw psqm)
# Below SUSPECT_FACTOR: almost certainly a data error → broken_candidate
# Below BROKEN_FACTOR:  suspicious but possible → needs_review
SUSPECT_FACTOR = 0.65
BROKEN_FACTOR  = 0.75

# Keep listings below this ratio of market_psqm as "deals"
DEAL_MAX_RATIO = 0.80

# Modifiers: only these affect score (Agios/Agia/Agioi are NOT modifiers)
VALID_MODS = {"Ano", "Kato", "Nea", "Neo", "Palaio"}
MOD_ADJUST = {"Ano": +2.0, "Kato": -2.0, "Nea": 0.0, "Neo": 0.0, "Palaio": 0.0}

# Outlier trimming for market stats
IQR_MIN_N = 8
IQR_K = 2


# ============================================================
# Scoring weights
# ============================================================
W_HOOD     = 0.25
W_DISCOUNT = 0.40  # discount from market is the core deal signal
W_FLOOR    = 0.22
W_SIZE     = 0.13
# Sum = 1.00; renovation_year is a flat bonus (not weighted)


# ============================================================
# Helpers
# ============================================================
def _safe_float(x):
    try:
        return float(x)
    except Exception:
        return None


def parse_iso_date_maybe(s: str | None) -> date | None:
    """Supports 'YYYY-MM-DD' or ISO datetime 'YYYY-MM-DDTHH:MM:SS...'."""
    if not s:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    try:
        return datetime.strptime(txt[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def extract_mod_from_area(area_value: str | None) -> str | None:
    """Accept only Ano/Kato/Nea/Neo/Palaio as scoring modifiers."""
    if not area_value:
        return None
    s = str(area_value).strip()
    return s if s in VALID_MODS else None


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile, p in [0,1], expects sorted list."""
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = p * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _trim_iqr(vals: list[float]) -> list[float]:
    """Trim outliers from both sides using IQR filter (if enough samples)."""
    if len(vals) < IQR_MIN_N:
        return vals
    s = sorted(vals)
    q1 = _percentile(s, 0.25)
    q3 = _percentile(s, 0.75)
    iqr = q3 - q1
    low = q1 - IQR_K * iqr
    high = q3 + IQR_K * iqr
    return [x for x in s if low <= x <= high]


# ============================================================
# Market stats (robust median psqm per neighborhood)
# Only CLEAN comparables are used: recent, with photos, IQR-trimmed,
# and above the broken-floor threshold.
# ============================================================
def compute_hood_market_median(
    cur: sqlite3.Cursor,
    *,
    has_pub_date: bool,
    has_scraped_at: bool,
    has_photos_count: bool,
    recent_days: int,
):
    """
    Returns dict:
      hood -> {
        median_raw, floor_broken, floor_suspect, median_clean, market_psqm, n_raw, n_clean
      }

    Market computation:
      1) values = psqm list (recent, with photos)
      2) median_raw
      3) trim IQR (both sides) -> vals_trim
      4) remove broken low tail (< BROKEN_FACTOR * median_raw) -> vals_clean
      5) median_clean => market_psqm
    """
    where = [
        "price_eur IS NOT NULL",
        "area_sqm IS NOT NULL",
        "CAST(area_sqm AS REAL) > 0",
        "neighborhood IS NOT NULL",
        "TRIM(neighborhood) != ''",
    ]

    if has_pub_date:
        where += [
            "publication_date IS NOT NULL",
            f"julianday(publication_date) >= julianday('now', '-{recent_days} day')",
        ]
    elif has_scraped_at:
        where += [
            "scraped_at IS NOT NULL",
            f"julianday(scraped_at) >= julianday('now', '-{recent_days} day')",
        ]

    if has_photos_count:
        where += [
            "photos_count IS NOT NULL",
            f"CAST(photos_count AS INTEGER) >= {MIN_PHOTOS}",
        ]

    cur.execute(
        f"""
        SELECT neighborhood, price_eur, area_sqm
        FROM listings
        WHERE {' AND '.join(where)}
        """
    )
    rows = cur.fetchall()

    hood_vals: dict[str, list[float]] = {h: [] for h in TOP_NEIGHBORHOODS}

    for hood, price_eur, area_sqm in rows:
        hood_c = str(hood).strip()
        if hood_c not in TOP_NEIGHBORHOODS:
            continue
        p = _safe_float(price_eur)
        a = _safe_float(area_sqm)
        if p is None or a is None or a <= 0:
            continue
        psqm = p / a
        if psqm > 0:
            hood_vals[hood_c].append(psqm)

    stats = {}
    for hood in TOP_NEIGHBORHOODS:
        vals = hood_vals.get(hood, [])
        n_raw = len(vals)
        if not vals:
            stats[hood] = {
                "n_raw": 0,
                "median_raw": None,
                "floor_broken": None,
                "floor_suspect": None,
                "n_clean": 0,
                "median_clean": None,
                "market_psqm": None,
            }
            continue

        vals_sorted = sorted(vals)
        median_raw = median(vals_sorted)

        # Trim both sides (upper+lower outliers) using IQR
        vals_trim = _trim_iqr(vals_sorted)

        # Remove broken low tail relative to median_raw
        floor_broken = BROKEN_FACTOR * median_raw if median_raw > 0 else None
        floor_suspect = SUSPECT_FACTOR * median_raw if median_raw > 0 else None
        if floor_broken is not None:
            vals_clean = [x for x in vals_trim if x >= floor_broken]
        else:
            vals_clean = vals_trim

        n_clean = len(vals_clean)
        median_clean = median(vals_clean) if vals_clean else None

        market = median_clean if median_clean is not None else median_raw

        stats[hood] = {
            "n_raw": n_raw,
            "median_raw": median_raw,
            "floor_broken": floor_broken,
            "floor_suspect": floor_suspect,
            "n_clean": n_clean,
            "median_clean": median_clean,
            "market_psqm": market,
        }

    return stats


def _build_score_update_sql(
    *,
    has_floor_col: bool,
    has_renovation_col: bool,
) -> str:
    """
    Build the SQL UPDATE statement that computes final_score for every listing.
    Extracted here so create_updated_db() stays readable.
    """
    discount_ratio = """
      CASE
        WHEN neighborhood_market_psqm IS NULL OR neighborhood_market_psqm <= 0 THEN NULL
        ELSE (neighborhood_market_psqm - listing_psqm) / neighborhood_market_psqm
      END"""

    discount_score = f"""
      CASE
        WHEN ({discount_ratio}) IS NULL THEN 50
        WHEN ({discount_ratio}) >= 0.30 THEN 100
        WHEN ({discount_ratio}) >= 0.25 THEN 90
        WHEN ({discount_ratio}) >= 0.20 THEN 80
        WHEN ({discount_ratio}) >= 0.15 THEN 70
        WHEN ({discount_ratio}) >= 0.10 THEN 55
        WHEN ({discount_ratio}) >= 0.05 THEN 45
        WHEN ({discount_ratio}) >= 0.00 THEN 35
        ELSE 20
      END"""

    floor_score = """
      CASE
        WHEN floor IS NULL OR TRIM(floor) = '' THEN 40
        WHEN CAST(floor AS INTEGER) < 0   THEN 20
        WHEN CAST(floor AS INTEGER) = 0   THEN 30
        WHEN CAST(floor AS INTEGER) = 1   THEN 50
        WHEN CAST(floor AS INTEGER) = 2   THEN 72
        WHEN CAST(floor AS INTEGER) = 3   THEN 84
        WHEN CAST(floor AS INTEGER) = 4   THEN 91
        WHEN CAST(floor AS INTEGER) = 5   THEN 95
        ELSE 98
      END""" if has_floor_col else "50"

    # 25-55 sqm is the Athens LTR sweet spot (studio / 1BR)
    size_score = """
      CASE
        WHEN area_sqm IS NULL OR TRIM(area_sqm) = '' THEN 50
        WHEN CAST(area_sqm AS REAL) < 15               THEN 15
        WHEN CAST(area_sqm AS REAL) < 20               THEN 40
        WHEN CAST(area_sqm AS REAL) < 25               THEN 60
        WHEN CAST(area_sqm AS REAL) >= 25 AND CAST(area_sqm AS REAL) <= 55 THEN 100
        WHEN CAST(area_sqm AS REAL) <= 70              THEN 85
        WHEN CAST(area_sqm AS REAL) <= 90              THEN 70
        WHEN CAST(area_sqm AS REAL) <= 120             THEN 50
        ELSE 30
      END"""

    mod_adj = f"""
      CASE
        WHEN neighborhood_mod = 'Ano'    THEN {MOD_ADJUST['Ano']}
        WHEN neighborhood_mod = 'Kato'   THEN {MOD_ADJUST['Kato']}
        WHEN neighborhood_mod = 'Nea'    THEN {MOD_ADJUST['Nea']}
        WHEN neighborhood_mod = 'Neo'    THEN {MOD_ADJUST['Neo']}
        WHEN neighborhood_mod = 'Palaio' THEN {MOD_ADJUST['Palaio']}
        ELSE 0
      END"""

    # Flat bonus (not weighted) — recently renovated properties are more attractive
    renovation_bonus = """
      CASE
        WHEN renovation_year IS NULL OR TRIM(renovation_year) = '' THEN 0
        WHEN CAST(renovation_year AS INTEGER) >= 2020 THEN 6
        WHEN CAST(renovation_year AS INTEGER) >= 2010 THEN 3
        ELSE 0
      END""" if has_renovation_col else "0"

    return f"""
        UPDATE listings
        SET final_score = (
          {W_HOOD}       * COALESCE(CAST(neighborhood_score AS REAL), 0)
          + {W_DISCOUNT} * ({discount_score})
          + {W_FLOOR}    * ({floor_score})
          + {W_SIZE}     * ({size_score})
          + ({mod_adj})
          + ({renovation_bonus})
        )
        WHERE neighborhood_market_psqm IS NOT NULL
          AND listing_psqm IS NOT NULL;
    """


def _classify_listing(
    listing_psqm: float,
    market_psqm: float | None,
    floor_broken: float | None,
    floor_suspect: float | None,
) -> tuple[str, int]:
    """
    Returns (status, used_for_market).

    Status priority (most suspicious first):
      broken_candidate < needs_review < deal < not_deal
    """
    if market_psqm is None or market_psqm <= 0:
        return "no_market", 0

    if floor_suspect is not None and listing_psqm < floor_suspect:
        return "broken_candidate", 0

    if floor_broken is not None and listing_psqm < floor_broken:
        return "needs_review", 0

    used_for_market = 1
    if listing_psqm <= market_psqm * DEAL_MAX_RATIO:
        return "deal", used_for_market
    return "not_deal", used_for_market


# ============================================================
# Main DB builder
# ============================================================
def create_updated_db(src_db: Path, dst_db: Path) -> None:
    if dst_db.exists():
        dst_db.unlink()

    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_db)

    s_cur = src.cursor()
    d_cur = dst.cursor()

    # --- source schema ---
    s_cur.execute("PRAGMA table_info(listings)")
    src_columns = [c[1] for c in s_cur.fetchall()]
    if not src_columns:
        raise RuntimeError("No columns found in source 'listings' table.")

    required = {"id", "url", "neighborhood", "price_eur", "area_sqm"}
    missing = [c for c in required if c not in src_columns]
    if missing:
        raise RuntimeError(f"Missing required columns: {missing}")

    has_pub_date      = "publication_date" in src_columns
    has_scraped_at    = "scraped_at" in src_columns
    has_photos_count  = "photos_count" in src_columns
    has_area_col      = "area" in src_columns
    has_floor_col      = "floor" in src_columns
    has_renovation_col = "renovation_year" in src_columns

    src_has_score_col = "neighborhood_score" in src_columns
    dst_columns = list(src_columns)
    if not src_has_score_col:
        dst_columns.append("neighborhood_score")

    idx_neighborhood = src_columns.index("neighborhood")
    idx_price       = src_columns.index("price_eur")
    idx_area_sqm    = src_columns.index("area_sqm")
    idx_pub_date    = src_columns.index("publication_date") if has_pub_date else None
    idx_scraped_at  = src_columns.index("scraped_at") if has_scraped_at else None
    idx_area_prefix = src_columns.index("area") if has_area_col else None
    idx_score_dst   = dst_columns.index("neighborhood_score")

    # --- compute neighborhood market medians ---
    print("[INFO] Computing neighborhood market MEDIAN psqm (robust) ...")
    hood_stats = compute_hood_market_median(
        s_cur,
        has_pub_date=has_pub_date,
        has_scraped_at=has_scraped_at,
        has_photos_count=has_photos_count,
        recent_days=RECENT_DAYS,
    )

    # --- create areas_external ---
    d_cur.execute(
        """
        CREATE TABLE areas_external (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            neighborhood TEXT UNIQUE,
            neighborhood_score INTEGER
        )
        """
    )
    for n, sc in NEIGHBORHOOD_SCORES.items():
        d_cur.execute(
            "INSERT INTO areas_external (neighborhood, neighborhood_score) VALUES (?, ?)",
            (n, sc),
        )

    # --- destination listings table ---
    create_cols = ", ".join(f"{c} TEXT" for c in dst_columns)
    d_cur.execute(
        f"""
        CREATE TABLE listings (
            {create_cols},
            neighborhood_canon TEXT,
            neighborhood_mod TEXT,
            listing_age_days INTEGER,
            listing_psqm REAL,
            neighborhood_market_psqm REAL,
            final_score REAL,
            status TEXT,
            used_for_market INTEGER DEFAULT 0
        )
        """
    )

    # --- select all candidates (basic validity only, no price filtering) ---
    where_parts = [
        "price_eur IS NOT NULL",
        "area_sqm IS NOT NULL",
        "CAST(area_sqm AS REAL) > 0",
        "neighborhood IS NOT NULL",
        "TRIM(neighborhood) != ''",
    ]

    if has_pub_date:
        where_parts += [
            "publication_date IS NOT NULL",
            f"julianday(publication_date) >= julianday('now', '-{RECENT_DAYS} day')",
        ]
    elif has_scraped_at:
        where_parts += [
            "scraped_at IS NOT NULL",
            f"julianday(scraped_at) >= julianday('now', '-{RECENT_DAYS} day')",
        ]

    if has_photos_count:
        where_parts += [
            "photos_count IS NOT NULL",
            f"CAST(photos_count AS INTEGER) >= {MIN_PHOTOS}",
        ]

    s_cur.execute(
        f"""
        SELECT *
        FROM listings
        WHERE {' AND '.join(where_parts)}
        ORDER BY CAST(id AS INTEGER) DESC
        """
    )
    rows = s_cur.fetchall()

    today = date.today()

    status_counts: dict[str, int] = {}
    invalid_skipped = 0

    for row in rows:
        values = list(row)
        if not src_has_score_col:
            values.append(None)

        hood = str(values[idx_neighborhood]).strip()

        if hood not in TOP_NEIGHBORHOODS:
            status = "unknown_neighborhood"
            final_values = values + [
                hood,   # neighborhood_canon
                None,   # neighborhood_mod
                None,   # listing_age_days
                None,   # listing_psqm
                None,   # neighborhood_market_psqm
                None,   # final_score
                status,
                0,      # used_for_market
            ]
            d_cur.execute(
                f"INSERT INTO listings VALUES ({','.join('?' * len(final_values))})",
                final_values,
            )
            status_counts[status] = status_counts.get(status, 0) + 1
            continue

        values[idx_score_dst] = NEIGHBORHOOD_SCORES.get(hood)

        p = _safe_float(values[idx_price])
        a = _safe_float(values[idx_area_sqm])
        if p is None or a is None or a <= 0:
            invalid_skipped += 1
            continue
        listing_psqm = p / a

        st = hood_stats.get(hood) or {}
        market_psqm  = st.get("market_psqm")
        floor_broken = st.get("floor_broken")
        floor_suspect = st.get("floor_suspect")

        status, used_for_market = _classify_listing(
            listing_psqm, market_psqm, floor_broken, floor_suspect
        )

        if status in ("not_deal", "broken_candidate"):
            status_counts[status] = status_counts.get(status, 0) + 1
            continue

        hood_mod = extract_mod_from_area(values[idx_area_prefix]) if idx_area_prefix is not None else None

        age_days = None
        if idx_pub_date is not None and values[idx_pub_date]:
            d0 = parse_iso_date_maybe(values[idx_pub_date])
            if d0:
                age_days = (today - d0).days
        elif idx_scraped_at is not None and values[idx_scraped_at]:
            d0 = parse_iso_date_maybe(values[idx_scraped_at])
            if d0:
                age_days = (today - d0).days

        final_values = values + [
            hood,                                                # neighborhood_canon
            hood_mod,                                           # neighborhood_mod
            age_days,                                           # listing_age_days
            float(listing_psqm),
            float(market_psqm) if market_psqm is not None else None,
            None,                                               # final_score (filled by UPDATE)
            status,
            used_for_market,
        ]

        d_cur.execute(
            f"INSERT INTO listings VALUES ({','.join('?' * len(final_values))})",
            final_values,
        )
        status_counts[status] = status_counts.get(status, 0) + 1

    # Compute final_score for all listings that have market context
    d_cur.execute(
        _build_score_update_sql(
            has_floor_col=has_floor_col,
            has_renovation_col=has_renovation_col,
        )
    )

    dst.commit()
    src.close()
    dst.close()

    inserted = sum(v for k, v in status_counts.items() if k not in ("not_deal", "broken_candidate"))
    print(f"[SUCCESS] DB created: {dst_db}")
    print(f"[STATS] total_inserted={inserted}")
    print(f"[STATS] invalid_skipped={invalid_skipped}  (missing price/area)")
    for s in ("deal", "not_deal", "needs_review", "broken_candidate", "no_market", "unknown_neighborhood"):
        n = status_counts.get(s, 0)
        if n:
            print(f"[STATS] {s}={n}")
    print(
        "[INFO] scoring cols: "
        f"floor={'yes' if has_floor_col else 'no'}, "
        f"renovation_year={'yes' if has_renovation_col else 'no'}"
    )


def main():
    root = Path(__file__).resolve().parents[3]
    src_db = root / "data" / "listings.db"
    dst_db = root / "data" / "db_updated.db"

    print(f"[INFO] Source DB: {src_db}")
    print(f"[INFO] Destination DB: {dst_db}")
    create_updated_db(src_db, dst_db)


if __name__ == "__main__":
    main()
