"""
db_conductor.py — Single entry point for all pipeline database interactions.

Covers updated_listings.db (Layer 0, Layer 1 operations) and
final_deals.db (scoring output). Never call sqlite3 directly from
pipeline layers — use this module instead.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3

from greeceapt.vision.ollama import PROMPT_VERSION, VisionConfig
from greeceapt.vision.types import SCHEMA_VERSION, ListingVisionResult

from greeceapt.storage import paths
from greeceapt.storage.util import parse_photo_urls_json

logger = logging.getLogger(__name__)

# Module-level bindings so tests can ``monkeypatch.setattr(db_conductor, \"INGESTED_DB\", ...)``.
DATA_DIR = paths.DATA_DIR
INGESTED_DB = paths.INGESTED_DB
UPDATED_DB = paths.UPDATED_DB
FINAL_DB = paths.FINAL_DB


# The image prefilter (MobileCLIP) was removed on 2026-11-16; it was never switched on. Stored
# results keep its two contract columns, always ('off', NULL), so they stay comparable.
FILTER_MODE = "off"


# ── Connection factories ──────────────────────────────────────────────────────

def connect_updated() -> sqlite3.Connection:
    return sqlite3.connect(UPDATED_DB)


def connect_updated_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{UPDATED_DB}?mode=ro", uri=True)




# ── DB lifecycle ──────────────────────────────────────────────────────────────

# Layer 1 results copied from the previous updated_listings.db when a listing is unchanged.
_CARRY_COLUMNS = (
    "visual_score_v2", "visual_confidence_v2", "visual_status_v2", "layer_1_features_v2",
    "vision_schema_version", "vision_model_version", "vision_prompt_version",
    "vision_filter_version", "vision_filter_mode",
)


def photo_set_hash(photo_urls_json: str | None) -> str:
    """Order-insensitive hash of a listing's photo URLs."""
    import hashlib

    urls = sorted(set(parse_photo_urls_json(photo_urls_json)))
    return hashlib.sha256(json.dumps(urls).encode("utf-8")).hexdigest()


# Columns added after results were first stored (the image prefilter, 2026-11-04). A database
# scored before then has none of them; every result was produced with the prefilter off.
_CARRY_DEFAULTS = {"vision_filter_version": "NULL", "vision_filter_mode": "'off'"}


def _previous_layer1_results(path) -> dict[str, tuple]:
    """{url: (photo_set_hash, *carry columns)} from listings + removed_listings of ``path``."""
    results: dict[str, tuple] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in ("listings", "removed_listings"):
                columns = set(_table_columns(conn, table))
                if "visual_status_v2" not in columns:
                    continue  # never scored
                missing = [c for c in _CARRY_COLUMNS if c not in columns and c not in _CARRY_DEFAULTS]
                if missing:
                    logger.warning(
                        "Layer 1 carry-over skipped for %s: previous table lacks %s.",
                        table, ", ".join(missing),
                    )
                    continue
                selected = ", ".join(
                    column if column in columns else f"{_CARRY_DEFAULTS[column]} AS {column}"
                    for column in _CARRY_COLUMNS
                )
                for row in conn.execute(
                    f"SELECT url, photo_urls_json, {selected} FROM {table} "
                    "WHERE visual_status_v2 IS NOT NULL"
                ):
                    results.setdefault(row[0], (photo_set_hash(row[1]), *row[2:]))
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        logger.warning(
            "Layer 1 carry-over skipped: previous %s unreadable (%s).", path, exc
        )
        return {}
    return results


def copy_ingested_to_updated() -> int:
    """Rebuild updated_listings.db from ingested_listings.db.

    Layer 1 results of the previous updated DB are carried over for every listing whose URL
    and photo set are unchanged, so a re-run does not re-download photos just to hit the
    vision cache. Version columns are carried too, so Layer 1 still re-scores rows produced
    by another model, prompt, schema or filter contract.
    """
    if not INGESTED_DB.exists():
        raise FileNotFoundError(f"Source DB not found: {INGESTED_DB}")
    previous = UPDATED_DB.with_name(UPDATED_DB.name + ".previous")
    carried_results: dict[str, tuple] = {}
    if UPDATED_DB.exists():
        os.replace(UPDATED_DB, previous)
        carried_results = _previous_layer1_results(previous)

    src = sqlite3.connect(f"file:{INGESTED_DB}?mode=ro", uri=True)
    try:
        col_types = _table_column_types(src, "listings")
        cols = list(col_types)
        rows = src.execute("SELECT * FROM listings").fetchall()
    finally:
        src.close()

    dst = sqlite3.connect(UPDATED_DB)
    try:
        # Carry the source types across. Declaring everything TEXT would give the copy
        # TEXT affinity, forcing a CAST in every downstream comparison and aggregate.
        col_defs = ", ".join(f"{c} {col_types[c] or 'TEXT'}" for c in cols)
        dst.execute(f"CREATE TABLE IF NOT EXISTS listings ({col_defs})")
        placeholders = ", ".join("?" * len(cols))
        col_str = ", ".join(cols)
        dst.executemany(
            f"INSERT OR IGNORE INTO listings ({col_str}) VALUES ({placeholders})",
            rows,
        )
        if carried_results:
            _ensure_layer1_columns(dst)
            assignments = ", ".join(f"{column} = ?" for column in _CARRY_COLUMNS)
            updates = []
            for url, photos_json in dst.execute("SELECT url, photo_urls_json FROM listings").fetchall():
                previous_row = carried_results.get(url)
                if previous_row and previous_row[0] == photo_set_hash(photos_json):
                    updates.append((*previous_row[1:], url))
            dst.executemany(f"UPDATE listings SET {assignments} WHERE url = ?", updates)
            logger.info(
                "Layer 1 carry-over: %s of %s listings keep their previous result.",
                len(updates), len(rows),
            )
        dst.commit()
    finally:
        dst.close()

    previous.unlink(missing_ok=True)
    return len(rows)




# ── Layer 0: table setup ──────────────────────────────────────────────────────

def setup_layer0_tables(conn: sqlite3.Connection) -> dict[str, bool]:
    """
    Ensure Layer 1 processing columns and removed_listings table exist.
    Returns a dict of which optional source columns are present in listings.
    """
    _ensure_layer1_columns(conn)
    _ensure_removed_table(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    return {
        "has_pub_date":     "publication_date" in cols,
        "has_scraped_at":   "scraped_at"        in cols,
        "has_neighborhood": "neighborhood"      in cols,
    }


def _ensure_layer1_columns(conn: sqlite3.Connection) -> None:
    """Add Layer 1 tracking columns to listings if not already present."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    for col, typedef in [
        ("layer_1_processed", "INTEGER DEFAULT 0"),
        ("visual_score",      "REAL"),
        ("layer_1_features",  "TEXT"),
        ("visual_score_v2",      "REAL"),
        ("visual_confidence_v2", "REAL"),
        ("visual_status_v2",     "TEXT"),
        ("layer_1_features_v2",  "TEXT"),
        ("vision_schema_version", "TEXT"),
        ("vision_model_version",  "TEXT"),
        ("vision_prompt_version", "TEXT"),
        ("vision_filter_version", "TEXT"),
        ("vision_filter_mode", "TEXT"),
        ("visual_score_v1_backup", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {typedef}")
    conn.commit()


def _ensure_removed_table(conn: sqlite3.Connection) -> None:
    src_types  = _table_column_types(conn, "listings")
    src_cols   = list(src_types)
    audit_cols = ["removal_reason", "layer_origin"]
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='removed_listings'"
    )
    if not cur.fetchone():
        col_defs = ", ".join(
            f"{c} {src_types.get(c, 'TEXT')}" for c in src_cols
        ) + ", " + ", ".join(f"{c} TEXT" for c in audit_cols)
        cur.execute(f"CREATE TABLE removed_listings ({col_defs})")
    else:
        existing = {row[1] for row in cur.execute("PRAGMA table_info(removed_listings)")}
        for col in src_cols + audit_cols:
            if col not in existing:
                cur.execute(f"ALTER TABLE removed_listings ADD COLUMN {col} TEXT")
    conn.commit()


# ── Layer 0: data access ──────────────────────────────────────────────────────

def get_listings_for_layer0(
    conn: sqlite3.Connection,
    col_info: dict[str, bool],
) -> list[tuple]:
    pub_date_col     = "publication_date" if col_info["has_pub_date"]     else "NULL"
    scraped_at_col   = "scraped_at"       if col_info["has_scraped_at"]   else "NULL"
    neighborhood_col = "neighborhood"     if col_info["has_neighborhood"] else "NULL"
    return conn.execute(
        f"SELECT id, photo_urls_json, {pub_date_col}, {scraped_at_col}, {neighborhood_col} "
        "FROM listings"
    ).fetchall()


def move_listing_to_removed(
    conn: sqlite3.Connection,
    row_id: int | str,
    layer_origin: str,
    reason: str,
    *,
    commit: bool = True,
) -> bool:
    """Move one listing from listings → removed_listings. Returns True on success."""
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE id = ?", (row_id,))
    row = cur.fetchone()
    if row is None:
        return False

    cols = _table_columns(conn, "listings")
    all_cols = cols + ["removal_reason", "layer_origin"]
    placeholders = ", ".join("?" * len(all_cols))
    cur.execute(
        f"INSERT INTO removed_listings ({', '.join(all_cols)}) VALUES ({placeholders})",
        list(row) + [reason, layer_origin],
    )
    cur.execute("DELETE FROM listings WHERE id = ?", (row_id,))
    if commit:
        conn.commit()
    return True


def move_listings_to_removed(
    conn: sqlite3.Connection,
    removals: list[tuple[int | str, str]],
    layer_origin: str,
) -> int:
    """
    Batch version of :func:`move_listing_to_removed` — one PRAGMA, one SELECT, one commit.

    ``removals`` is ``[(row_id, reason), ...]``. Ids with no matching row are skipped.
    Returns the number of rows actually moved.
    """
    if not removals:
        return 0

    reason_by_id = {str(rid): reason for rid, reason in removals}
    cols = _table_columns(conn, "listings")
    all_cols = cols + ["removal_reason", "layer_origin"]
    placeholders = ", ".join("?" * len(all_cols))
    id_idx = cols.index("id")

    cur = conn.cursor()
    qmarks = ", ".join("?" * len(reason_by_id))
    rows = cur.execute(
        f"SELECT * FROM listings WHERE CAST(id AS TEXT) IN ({qmarks})",
        list(reason_by_id),
    ).fetchall()
    if not rows:
        return 0

    payload = [
        list(row) + [reason_by_id[str(row[id_idx])], layer_origin]
        for row in rows
    ]
    cur.executemany(
        f"INSERT INTO removed_listings ({', '.join(all_cols)}) VALUES ({placeholders})",
        payload,
    )
    cur.executemany(
        "DELETE FROM listings WHERE id = ?", [(row[id_idx],) for row in rows]
    )
    conn.commit()
    return len(rows)


# ── Layer 1: data access ──────────────────────────────────────────────────────

def get_listings_for_layer1_v2(
    conn: sqlite3.Connection,
    *,
    schema_version: str = SCHEMA_VERSION,
    model_version: str | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> list[tuple[str, list[str]]]:
    """Return retryable or stale V2 listings for the current scoring contract."""
    _ensure_layer1_columns(conn)
    resolved_model = model_version or VisionConfig().model
    rows = conn.execute(
        """
        SELECT id, photo_urls_json FROM listings
        WHERE visual_status_v2 IS NULL
           OR visual_status_v2 != 'accepted'
           OR vision_schema_version IS NOT ?
           OR vision_model_version IS NOT ?
           OR vision_prompt_version IS NOT ?
           OR vision_filter_version IS NOT NULL
           OR vision_filter_mode IS NOT ?
        ORDER BY id
        """,
        (schema_version, resolved_model, prompt_version, FILTER_MODE),
    ).fetchall()
    result = []
    for listing_id, photo_urls_json in rows:
        urls = parse_photo_urls_json(photo_urls_json)
        if urls:
            result.append((str(listing_id), urls))
    return result


def _v2_features(
    result: ListingVisionResult,
    *,
    schema_version: str,
    model_version: str,
    prompt_version: str,
) -> dict:
    return {
        "schema_version": schema_version,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "condition_score": result.condition_score,
        "effective_confidence": result.effective_confidence,
        "status": result.status,
        "room_scores": result.room_scores,
        "accepted_count": result.accepted_count,
        "rejected_count": result.rejected_count,
        "retried_count": result.retried_count,
        "cache_hit_count": result.cache_hit_count,
        "duplicate_mapping": result.duplicate_mapping,
        "source_urls": list(result.source_urls),
        "image_provenance": list(result.image_provenance),
        "image_scores": [
            {
                "condition_score": image.condition_score,
                "evidence_coverage": image.evidence_coverage,
                "effective_confidence": image.effective_confidence,
                "status": image.status,
                "room_type": image.room_type,
                "verdict": image.verdict.model_dump() if image.verdict else None,
            }
            for image in result.image_scores
        ],
    }


def save_visual_audit_v2_results(
    conn: sqlite3.Connection,
    listing_id: str | int,
    result: ListingVisionResult,
    *,
    promote: bool,
    commit: bool = True,
    schema_version: str = SCHEMA_VERSION,
    model_version: str | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> None:
    """Persist V2 in shadow columns and optionally update the compatibility score."""
    _ensure_layer1_columns(conn)
    resolved_model = model_version or VisionConfig().model
    features = _v2_features(
        result, schema_version=schema_version, model_version=resolved_model, prompt_version=prompt_version,
    )
    if promote:
        compatibility_score = (
            result.condition_score if result.status == "accepted" else None
        )
        conn.execute(
            """
            UPDATE listings
            SET visual_score_v1_backup = COALESCE(visual_score_v1_backup, visual_score),
                visual_score = ?, visual_score_v2 = ?, visual_confidence_v2 = ?,
                visual_status_v2 = ?, layer_1_features_v2 = ?,
                vision_schema_version = ?, vision_model_version = ?,
                vision_prompt_version = ?, vision_filter_version = NULL,
                vision_filter_mode = ?
            WHERE id = ?
            """,
            (
                compatibility_score,
                result.condition_score,
                result.effective_confidence,
                result.status,
                json.dumps(features, sort_keys=True),
                schema_version,
                resolved_model,
                prompt_version,
                FILTER_MODE,
                str(listing_id),
            ),
        )
    else:
        conn.execute(
            """
            UPDATE listings
            SET visual_score_v2 = ?, visual_confidence_v2 = ?,
                visual_status_v2 = ?, layer_1_features_v2 = ?,
                vision_schema_version = ?, vision_model_version = ?,
                vision_prompt_version = ?, vision_filter_version = NULL,
                vision_filter_mode = ?
            WHERE id = ?
            """,
            (
                result.condition_score,
                result.effective_confidence,
                result.status,
                json.dumps(features, sort_keys=True),
                schema_version,
                resolved_model,
                prompt_version,
                FILTER_MODE,
                str(listing_id),
            ),
        )
    if commit:
        conn.commit()


def promote_current_v2_results(
    conn: sqlite3.Connection,
    *,
    schema_version: str = SCHEMA_VERSION,
    model_version: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    commit: bool = True,
) -> int:
    """Promote accepted shadow rows for this exact contract without reinference."""
    _ensure_layer1_columns(conn)
    resolved_model = model_version or VisionConfig().model
    cursor = conn.execute(
        """
        UPDATE listings
        SET visual_score_v1_backup = COALESCE(visual_score_v1_backup, visual_score),
            visual_score = visual_score_v2
        WHERE visual_status_v2 = 'accepted'
          AND visual_score_v2 IS NOT NULL
          AND vision_schema_version IS ?
          AND vision_model_version IS ?
          AND vision_prompt_version IS ?
          AND vision_filter_version IS NULL
          AND vision_filter_mode IS ?
        """,
        (schema_version, resolved_model, prompt_version, FILTER_MODE),
    )
    if commit:
        conn.commit()
    return cursor.rowcount


def _validate_column(conn: sqlite3.Connection, column: str) -> str:
    if column not in _table_columns(conn, "listings"):
        raise ValueError(f"unknown listings column: {column}")
    return column


def remove_low_condition_listings(
    conn: sqlite3.Connection,
    threshold: float,
    *,
    score_column: str,
    status_column: str,
    layer_name: str = "Layer 2",
) -> int:
    """Remove only known, accepted low-condition listings; unknowns remain retryable."""
    score_column = _validate_column(conn, score_column)
    status_column = _validate_column(conn, status_column)
    rows = conn.execute(
        f"""
        SELECT id, {score_column} FROM listings
        WHERE {status_column} = 'accepted'
          AND {score_column} IS NOT NULL
          AND CAST({score_column} AS REAL) < ?
        """,
        (threshold,),
    ).fetchall()
    removals = [
        (
            listing_id,
            f"Condition score ({float(score):.2f}) below threshold ({threshold:.1f})",
        )
        for listing_id, score in rows
    ]
    return move_listings_to_removed(conn, removals, layer_name)


def get_layer1_priority_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """{listing id: price/area/floor/neighborhood} used to order Layer 1 work top-first."""
    columns = set(_table_columns(conn))
    floor_class = "floor_class" if "floor_class" in columns else "NULL AS floor_class"
    rows = conn.execute(
        f"SELECT id, price_eur, area_sqm, floor, {floor_class}, neighborhood FROM listings"
    ).fetchall()
    return {
        str(r[0]): {"price_eur": r[1], "area_sqm": r[2], "floor": r[3], "floor_class": r[4], "neighborhood": r[5]}
        for r in rows
    }


# ── Scoring: data access ──────────────────────────────────────────────────────

def get_all_listings_for_scoring(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM listings").fetchall()


def get_neighborhood_counts(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        """
        SELECT neighborhood, COUNT(*) AS cnt
        FROM listings
        WHERE neighborhood IS NOT NULL AND TRIM(neighborhood) != ''
        GROUP BY neighborhood
        """
    ).fetchall()


# ── Scoring: write ────────────────────────────────────────────────────────────
# final_deals.db is written only by ``greeceapt.storage.final_db.write_final_database_atomic``.


# ── Internal helpers ──────────────────────────────────────────────────────────

def _table_columns(conn: sqlite3.Connection, table: str = "listings") -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_column_types(conn: sqlite3.Connection, table: str = "listings") -> dict[str, str]:
    """Ordered ``{column: declared_type}`` so copies keep numeric affinity."""
    return {row[1]: (row[2] or "TEXT") for row in conn.execute(f"PRAGMA table_info({table})")}
