"""
Scraper-wide helpers: XE search URLs, merge/dedupe math, and listing image hashing.

Used by ``run_all`` (merge), ``scraper/xe/``, and ``scraper/spitogatos/``. Keeps orchestration files thinner.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from greeceapt.storage import paths
from greeceapt.collect.fingerprints import PhotoFingerprint, fingerprint_image

# ── Incremental scrape JSON stores ───────────────────────────────────────────


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def touch_listing_updated_at(row: dict[str, Any]) -> None:
    """Set ``updated_at`` to current UTC ISO on the dict already held in the store."""
    row["updated_at"] = utc_now_iso()


def upsert_listing_in_url_store(
    store: dict[str, dict[str, Any]],
    canonical_url: str,
    fresh_row: dict[str, Any],
) -> str:
    """
    If ``canonical_url`` is already in ``store``, only ``updated_at`` is refreshed.
    Otherwise ``fresh_row`` is copied in and gets ``updated_at`` (and ``scraped_at`` if missing).
    Returns ``"touched"`` or ``"inserted"``.
    """
    if canonical_url in store:
        touch_listing_updated_at(store[canonical_url])
        return "touched"
    row = dict(fresh_row)
    now = utc_now_iso()
    row.setdefault("updated_at", now)
    row.setdefault("scraped_at", now)
    store[canonical_url] = row
    return "inserted"


# ── Scrape output filters (XE + Spitogatos) ───────────────────────────────────


def parse_publication_date_string(value: Any) -> datetime | None:
    """Parse ``publication_date`` text (``YYYY-MM-DD`` or ISO). Returns None if unknown."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    if s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Merge / dedupe (run_all) ─────────────────────────────────────────────────

IMAGE_CONCURRENCY = 10
STALE_DAYS = 180

FILL_FROM_OTHER_ON_MERGE = (
    "title", "description", "address_raw", "area_sqm", "bedrooms", "bathrooms",
    "floor", "floor_class", "year_built", "renovation_year", "energy_class", "heating_type",
    "neighborhood", "municipality", "latitude", "longitude", "price_per_sqm",
)

_stale_cutoff_cache: dict[int, datetime] = {}


def publication_stale_cutoff_utc(days: int = STALE_DAYS) -> datetime:
    if days not in _stale_cutoff_cache:
        _stale_cutoff_cache[days] = datetime.now(timezone.utc) - timedelta(days=days)
    return _stale_cutoff_cache[days]


def parse_listing_publication_utc(listing: dict) -> datetime | None:
    return parse_publication_date_string(listing.get("publication_date"))


def is_listing_stale_by_publication(listing: dict, days: int = STALE_DAYS) -> bool:
    dt = parse_listing_publication_utc(listing)
    return dt is not None and dt < publication_stale_cutoff_utc(days)


def load_standalone_phash_cache(cache_json: Path | None = None) -> dict[str, PhotoFingerprint]:
    """Load legacy strings and versioned URL→fingerprint cache records."""
    path = cache_json if cache_json is not None else paths.PHASH_CACHE_JSON
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cache: dict[str, PhotoFingerprint] = {}
    for url, value in data.items():
        fingerprint = PhotoFingerprint.from_json(value)
        if url and fingerprint is not None:
            cache[str(url)] = fingerprint
    return cache


def save_standalone_phash_cache(
    cache: dict[str, PhotoFingerprint | str],
    cache_json: Path | None = None,
) -> None:
    """Persist versioned records atomically so a wiped listing store costs no re-downloads."""
    path = cache_json if cache_json is not None else paths.PHASH_CACHE_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    serialized = {}
    for url, value in cache.items():
        fingerprint = value if isinstance(value, PhotoFingerprint) else PhotoFingerprint.from_json(value)
        if fingerprint is not None:
            serialized[url] = fingerprint.to_json()
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(serialized, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_url_hash_cache_from_listings_json(output_json: Path) -> dict[str, PhotoFingerprint]:
    """
    Build ``{photo_url: phash hex}`` for the merge.

    Seeded from the standalone cache (which outlives ``listings.json``), then
    overlaid with ``photo_url_hashes`` from an existing ``listings.json`` when present.
    """
    cache = load_standalone_phash_cache()
    if not output_json.exists():
        return cache
    try:
        with output_json.open(encoding="utf-8") as f:
            data = json.load(f)
        for listing in data:
            for url, h in (listing.get("photo_url_hashes") or {}).items():
                fingerprint = PhotoFingerprint.from_json(h)
                current = cache.get(url)
                if fingerprint is not None and (
                    current is None or fingerprint.is_complete_v2 or not current.is_complete_v2
                ):
                    cache[url] = fingerprint
            for url, value in (listing.get("photo_fingerprints") or {}).items():
                fingerprint = PhotoFingerprint.from_json(value)
                current = cache.get(url)
                if fingerprint is not None and (
                    current is None or fingerprint.is_complete_v2 or not current.is_complete_v2
                ):
                    cache[url] = fingerprint
    except Exception:
        return cache
    return cache


async def compute_listing_photo_fingerprints(
    photo_urls: list[str],
    url_cache: dict[str, PhotoFingerprint],
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict[str, PhotoFingerprint]:
    """Return complete v2 fingerprints, lazily upgrading full-only legacy entries."""
    result: dict[str, PhotoFingerprint] = {}
    to_download: list[str] = []
    for url in photo_urls:
        cached = url_cache.get(url)
        if cached is not None and cached.is_complete_v2:
            result[url] = cached
        else:
            to_download.append(url)

    async def _one(url: str) -> tuple[str, PhotoFingerprint | None]:
        async with sem:
            try:
                resp = await client.get(url, timeout=15.0)
                resp.raise_for_status()
                with Image.open(io.BytesIO(resp.content)) as image:
                    return url, fingerprint_image(image)
            except Exception:
                return url, None

    if to_download:
        downloads = await asyncio.gather(*(_one(u) for u in to_download))
        for url, fingerprint in downloads:
            if fingerprint is not None:
                result[url] = fingerprint
                url_cache[url] = fingerprint
    return result


def merge_duplicate_listing_into_kept(
    existing: dict,
    incoming: dict,
    *,
    stale_days: int = STALE_DAYS,
) -> None:
    """Merge ``incoming`` into ``existing`` in-place (Quad-Lock survivor rules).

    The survivor's ``price_eur`` and ``area_sqm`` always come from the same advertisement,
    and every advertisement's own price/area is kept in ``advertised_offers``.
    """
    offers = {
        str(offer.get("url")): offer
        for offer in _advertised_offers(existing) + _advertised_offers(incoming)
    }
    cutoff = publication_stale_cutoff_utc(stale_days)
    existing_dt = parse_listing_publication_utc(existing)
    incoming_dt = parse_listing_publication_utc(incoming)
    existing_stale = existing_dt is not None and existing_dt < cutoff
    incoming_stale = incoming_dt is not None and incoming_dt < cutoff

    def _take_financial_basis_from_incoming() -> None:
        existing["price_eur"] = incoming.get("price_eur")
        if incoming.get("area_sqm") is not None:
            existing["area_sqm"] = incoming.get("area_sqm")
        existing["publication_date"] = incoming.get("publication_date")

    if existing_stale and not incoming_stale:
        _take_financial_basis_from_incoming()
    elif not existing_stale and not incoming_stale:
        e_price = existing.get("price_eur")
        i_price = incoming.get("price_eur")
        if i_price is not None and (e_price is None or i_price < e_price):
            _take_financial_basis_from_incoming()

    # Publication dates: keep BOTH ends rather than collapsing to one value.
    # ``publication_date`` was previously only copied inside the price branches above, so a
    # dated row absorbed into an undated one silently lost its date (measured: 15% coverage
    # on merged rows vs 100% on Spitogatos alone) — and Layer 0's staleness filter reads it.
    # earliest → how long the property has really been on the market (days-on-market signal);
    # latest   → freshness, which is what the staleness filter wants.
    _dates = [
        d for d in (
            existing_dt,
            incoming_dt,
            parse_publication_date_string(existing.get("earliest_publication_date")),
            parse_publication_date_string(incoming.get("earliest_publication_date")),
            parse_publication_date_string(existing.get("latest_publication_date")),
            parse_publication_date_string(incoming.get("latest_publication_date")),
        ) if d is not None
    ]
    if _dates:
        existing["earliest_publication_date"] = min(_dates).date().isoformat()
        existing["latest_publication_date"] = max(_dates).date().isoformat()
        existing["publication_date"] = max(_dates).date().isoformat()

    seen: set[str] = set(existing.get("source_urls") or [])
    seen.update(incoming.get("source_urls") or [])
    existing["source_urls"] = sorted(seen)

    e_src = existing.get("source", "")
    i_src = incoming.get("source", "")
    if e_src != i_src and e_src and i_src:
        existing["source"] = "xe & spitogatos"

    for field in FILL_FROM_OTHER_ON_MERGE:
        if existing.get(field) is None and incoming.get(field) is not None:
            existing[field] = incoming[field]

    existing_photos = existing.get("photo_urls") or []
    incoming_photos = incoming.get("photo_urls") or []
    if len(incoming_photos) > len(existing_photos):
        existing["photo_urls"] = incoming_photos
    # Recount unconditionally — photos_count was only refreshed inside the branch above,
    # so a survivor kept a stale count whenever the absorbed row had fewer photos.
    existing["photos_count"] = len(existing.get("photo_urls") or [])

    existing["photo_url_hashes"] = {
        **(existing.get("photo_url_hashes") or {}),
        **(incoming.get("photo_url_hashes") or {}),
    }
    existing["photo_fingerprints"] = {
        **(existing.get("photo_fingerprints") or {}),
        **(incoming.get("photo_fingerprints") or {}),
    }
    all_hashes: set[str] = set(existing.get("image_hashes") or [])
    all_hashes.update(incoming.get("image_hashes") or [])
    existing["image_hashes"] = sorted(all_hashes)
    existing["advertised_offers"] = sorted(
        offers.values(),
        key=lambda offer: (offer.get("price_eur") is None, offer.get("price_eur") or 0, str(offer.get("url"))),
    )


def _advertised_offers(listing: dict) -> list[dict]:
    """The listing's own advertisement(s): price and area exactly as each ad states them."""
    if listing.get("advertised_offers"):
        return [dict(offer) for offer in listing["advertised_offers"]]
    return [{
        "url": listing.get("url"),
        "source": listing.get("source"),
        "price_eur": listing.get("price_eur"),
        "area_sqm": listing.get("area_sqm"),
        "floor_class": listing.get("floor_class"),
    }]
