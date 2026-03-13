"""
Listings DB: schema, connection, and insert logic.
Stores scraped XE.gr listings in data/listings.db.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from greeceapt.utils.helpers import normalize_listing_url

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "listings.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def create_tables():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        headline TEXT,
        price_eur REAL,
        price_per_sqm REAL,
        area_sqm REAL,
        neighborhood TEXT,
        area TEXT,
        address_raw TEXT,
        bedrooms INTEGER,
        bathrooms INTEGER,
        floor INTEGER,
        year_built INTEGER,
        renovation_year INTEGER,
        energy_class TEXT,
        photos_count INTEGER,
        photo_urls_json TEXT,
        publication_date TEXT,
        scraped_at TEXT,
        raw_json TEXT,
        updated_at TEXT,
        neighborhood_score INTEGER
    );
    """)

    # Migrate existing DBs: add new columns if they don't exist yet
    existing = {row[1] for row in c.execute("PRAGMA table_info(listings)")}
    for col, coltype in [("headline", "TEXT"), ("price_per_sqm", "REAL")]:
        if col not in existing:
            c.execute(f"ALTER TABLE listings ADD COLUMN {col} {coltype}")

    conn.commit()
    conn.close()
    print("✔ listings table ready.")


def normalize_xe_item(raw: dict) -> dict:
    """Convert raw scraper dict to DB row format."""
    url = normalize_listing_url(raw.get("url"))
    photo_urls = raw.get("photo_urls") or []
    if not isinstance(photo_urls, list):
        photo_urls = []
    return {
        "url": url,
        "headline": raw.get("Headline"),
        "price_eur": raw.get("price_eur"),
        "price_per_sqm": raw.get("price_per_sqm"),
        "area_sqm": raw.get("area_sqm"),
        "neighborhood": raw.get("neighborhood"),
        "area": raw.get("area"),
        "address_raw": raw.get("address_raw"),
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "floor": raw.get("floor"),
        "year_built": raw.get("year_built"),
        "renovation_year": raw.get("renovation_year"),
        "energy_class": raw.get("energy_class"),
        "photos_count": raw.get("photos_count"),
        "photo_urls_json": json.dumps(photo_urls, ensure_ascii=False),
        "publication_date": raw.get("publication_date"),
        "scraped_at": raw.get("scraped_at"),
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "updated_at": datetime.utcnow().isoformat(),
        "neighborhood_score": None,
    }


def insert_listings(items: list[dict]):
    """Insert or replace a batch of listings in a single transaction."""
    create_tables()
    inserted = skipped = 0
    conn = get_connection()
    c = conn.cursor()
    try:
        for raw in items:
            item = normalize_xe_item(raw)
            if not item.get("url"):
                skipped += 1
                continue
            c.execute("""
                INSERT OR REPLACE INTO listings (
                    url, headline, price_eur, price_per_sqm, area_sqm,
                    neighborhood, area, address_raw,
                    bedrooms, bathrooms, floor, year_built, renovation_year, energy_class,
                    photos_count, photo_urls_json, publication_date, scraped_at,
                    raw_json, updated_at, neighborhood_score
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                item["url"], item.get("headline"), item["price_eur"], item.get("price_per_sqm"),
                item["area_sqm"], item["neighborhood"], item["area"], item["address_raw"],
                item["bedrooms"], item["bathrooms"], item["floor"], item["year_built"],
                item["renovation_year"], item["energy_class"], item["photos_count"],
                item["photo_urls_json"], item["publication_date"], item["scraped_at"],
                item["raw_json"], item["updated_at"], item["neighborhood_score"],
            ))
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    print(f"✔ inserted {inserted} listings into DB (skipped {skipped} missing url)")
