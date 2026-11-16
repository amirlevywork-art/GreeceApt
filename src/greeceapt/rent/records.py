"""Turn a Spitogatos rental (search row + detail payload) into one flat record. Pure."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from greeceapt.storage.neighborhoods import canonical_neighborhood
from greeceapt.scoring.condition_text import publisher_condition
from greeceapt.collect.floor_resolver import resolve_floor
from greeceapt.collect.spitogatos import parse as P

RECORD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("url", "TEXT UNIQUE"),
    ("spitogatos_id", "TEXT"),
    ("rent_eur", "REAL"),
    ("area_sqm", "REAL"),
    ("neighborhood", "TEXT"),
    ("neighborhood_raw", "TEXT"),
    ("hood_l4", "TEXT"),
    ("latitude", "REAL"),
    ("longitude", "REAL"),
    ("geocode_type", "TEXT"),
    ("floor", "REAL"),
    ("floor_class", "TEXT"),
    ("floor_conflict", "INTEGER"),
    ("bedrooms", "INTEGER"),
    ("bathrooms", "INTEGER"),
    ("year_built", "INTEGER"),
    ("renovation_year", "INTEGER"),
    ("energy_class", "TEXT"),
    ("heating_type", "TEXT"),
    ("furnished", "INTEGER"),
    ("elevator", "INTEGER"),
    ("flag_renovated", "INTEGER"),
    ("flag_requires_renovation", "INTEGER"),
    ("publisher_condition", "TEXT"),
    ("common_expenses", "REAL"),
    ("first_publish_date", "TEXT"),
    ("modified_date", "TEXT"),
    ("price_reduced", "INTEGER"),
    ("price_pre_reduction", "REAL"),
    ("title", "TEXT"),
    ("description", "TEXT"),
    ("photo_urls_json", "TEXT"),
    ("photos_count", "INTEGER"),
    ("scraped_at", "TEXT"),
    ("raw_json", "TEXT"),
)


def listing_url(pid: object) -> str:
    return f"https://www.spitogatos.gr/en/property/11{pid}"


def _flag(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "yes", "true"}:
        return 1
    if text in {"0", "no", "false"}:
        return 0
    return None


def _int(value: object) -> int | None:
    number = P.clean_float(value)
    return int(number) if number is not None else None


def _date(value: object) -> str | None:
    dt = P.parse_spitogatos_datetime(value)
    return dt.date().isoformat() if dt else None


def _level_name(geo: Mapping[str, Any] | None, level: str) -> str | None:
    if not isinstance(geo, Mapping):
        return None
    entry = geo.get(level)
    if isinstance(entry, Mapping) and entry.get("name"):
        return str(entry["name"]).strip() or None
    return None


def build_record(search_row: Mapping[str, Any], detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge the search row (price, dates, price cuts) with the detail payload (everything else).

    Search owns price and price-cut signals; detail owns structure, photos and text.
    """
    d: Mapping[str, Any] = detail or {}
    pid = search_row.get("id") or d.get("id")
    geo = d.get("geographiesByLevel")
    neighborhood_raw, _ = P.neighborhood_from_geo(geo if isinstance(geo, dict) else {})
    if not neighborhood_raw:
        text = search_row.get("geography")
        neighborhood_raw = str(text).split("(")[0].strip() if isinstance(text, str) else None
    hood_l4 = _level_name(geo, "4") or neighborhood_raw

    title = d.get("title") or d.get("name") or d.get("heading")
    description = d.get("description") or search_row.get("description")
    resolution = resolve_floor(
        structured_text=None,
        title=title,
        description=description,
        spitogatos_code=d.get("floorNumber", search_row.get("floorNumber")),
    )
    photos = P.photo_urls_from_images(d.get("images") or [])

    record: dict[str, Any] = {
        "url": listing_url(pid),
        "spitogatos_id": str(pid) if pid is not None else None,
        "rent_eur": P.clean_float(search_row.get("price", d.get("price"))),
        "area_sqm": P.clean_float(d.get("sq_meters", search_row.get("sq_meters"))),
        "neighborhood": canonical_neighborhood(neighborhood_raw),
        "neighborhood_raw": neighborhood_raw,
        "hood_l4": canonical_neighborhood(hood_l4),
        "latitude": P.clean_float(d.get("latitude", search_row.get("latitude"))),
        "longitude": P.clean_float(d.get("longitude", search_row.get("longitude"))),
        "geocode_type": d.get("geocodeType") or search_row.get("geocodeType"),
        "floor": resolution.value,
        "floor_class": resolution.floor_class,
        "floor_conflict": int(bool(resolution.conflict)),
        "bedrooms": _int(d.get("rooms", search_row.get("rooms"))),
        "bathrooms": _int(d.get("no_of_bathrooms", search_row.get("no_of_bathrooms"))),
        "year_built": P.clean_year(d.get("year_of_construction")),
        "renovation_year": P.clean_year(d.get("renovationYear")),
        "energy_class": P.decode_energy_class(d.get("energyClass")),
        "heating_type": P.decode_heating(d.get("heatingController"), d.get("heatingMedium")),
        "furnished": _flag(d.get("furnished")),
        "elevator": _flag(d.get("elevator")),
        "flag_renovated": _flag(d.get("renovated")),
        "flag_requires_renovation": _flag(d.get("requiresRenovation")),
        "publisher_condition": publisher_condition({"description": description}),
        "common_expenses": P.clean_float(d.get("commonExpenses")),
        "first_publish_date": _date(search_row.get("firstPublishDate") or d.get("firstPublishDate")),
        "modified_date": _date(search_row.get("modified") or d.get("modified")),
        "price_reduced": _flag(search_row.get("priceReduced")) or 0,
        "price_pre_reduction": P.clean_float(search_row.get("pricePreReduction")),
        "title": str(title).strip() if title else None,
        "description": description,
        "photo_urls_json": json.dumps(photos, ensure_ascii=False),
        "photos_count": len(photos),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "raw_json": json.dumps(
            {"search": dict(search_row), "detail": {k: v for k, v in d.items() if k != "images"}},
            ensure_ascii=False,
            default=str,
        ),
    }
    return record
