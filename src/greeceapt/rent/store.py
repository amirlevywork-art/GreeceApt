"""
rentals.db — one ``listings`` table of rental records.

The table is deliberately named ``listings`` with an integer ``id`` so the Layer 1 V2 helpers in
``db_conductor`` (work queue, result persistence) run on it unchanged: rentals are scored by the
exact same vision contract as sale listings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from greeceapt.rent import config as cfg
from greeceapt.rent.records import RECORD_COLUMNS

_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("in_vision_sample", "INTEGER DEFAULT 0"),
    ("sample_stratum", "TEXT"),
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
)
_DATA_COLUMNS = [name for name, _ in RECORD_COLUMNS if name != "url"]


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or cfg.RENTALS_DB)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    cols = ", ".join(f"{name} {typedef}" for name, typedef in RECORD_COLUMNS + _EXTRA_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS listings (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    for name, typedef in RECORD_COLUMNS + _EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {typedef.replace(' UNIQUE', '')}")
    conn.commit()


def known_urls(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT url FROM listings")}


def touch_seen(conn: sqlite3.Connection, url: str, now: str, search_fields: dict[str, Any]) -> None:
    """Re-seen rental: refresh the volatile search-owned fields only (price, price cut)."""
    conn.execute(
        """
        UPDATE listings SET last_seen_at = ?, rent_eur = COALESCE(?, rent_eur),
            price_reduced = COALESCE(?, price_reduced),
            price_pre_reduction = COALESCE(?, price_pre_reduction)
        WHERE url = ?
        """,
        (now, search_fields.get("rent_eur"), search_fields.get("price_reduced"),
         search_fields.get("price_pre_reduction"), url),
    )


def upsert_records(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> int:
    """Insert new rentals; existing URLs get their data columns replaced (vision columns kept)."""
    count = 0
    placeholders = ", ".join("?" for _ in _DATA_COLUMNS)
    updates = ", ".join(f"{name} = excluded.{name}" for name in _DATA_COLUMNS)
    for record in records:
        now = record.get("scraped_at")
        conn.execute(
            f"""
            INSERT INTO listings (url, {", ".join(_DATA_COLUMNS)}, first_seen_at, last_seen_at)
            VALUES (?, {placeholders}, ?, ?)
            ON CONFLICT(url) DO UPDATE SET {updates}, last_seen_at = excluded.last_seen_at
            """,
            (record["url"], *(record.get(name) for name in _DATA_COLUMNS), now, now),
        )
        count += 1
    conn.commit()
    return count


def all_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM listings ORDER BY id")]
