"""
Read-only views of the pipeline's outputs for the GUI.

Every SQLite read opens its own ``mode=ro`` connection and closes it at once: scoring refuses to
publish ``final_deals.db`` while another connection holds a read transaction on it.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from greeceapt.storage import paths

RETRAIN_NOTICE_MIN = 50
_JSON_FIELDS = (
    "check_reasons_json", "source_urls_json", "floor_evidence_json",
    "excluded_assumptions_json", "evidence_warnings_json", "risk_flags_json", "assumptions_json",
    "costs_json",
)
_OBJECT_FIELDS = {"assumptions_json", "costs_json"}


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return default


_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def plain_text(value: object) -> str | None:
    """Ad text as plain text: line breaks kept, other HTML tags and entities removed."""
    if value is None:
        return None
    text = html.unescape(_TAG.sub("", _BR.sub("\n", str(value))))
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _deal(row: Mapping[str, Any], *, ranked: bool) -> dict[str, Any]:
    deal = dict(row)
    for key in _JSON_FIELDS:
        if key in deal:
            deal[key.removesuffix("_json")] = _loads(deal.pop(key), {} if key in _OBJECT_FIELDS else [])
    deal.pop("assumptions", None)  # identical for every flat: served once in /api/meta
    deal["ranked"] = ranked
    price, area = deal.get("price_eur"), deal.get("area_sqm")
    deal["price_psqm"] = price / area if price and area else None
    return deal


def flats(final_db: Path | None = None) -> list[dict[str, Any]]:
    """Every ranked and unranked opportunity of the last scoring run (empty before the first)."""
    path = final_db or paths.FINAL_DB
    if not path.exists():
        return []
    with closing(_ro(path)) as conn:
        ranked = [_deal(r, ranked=True) for r in conn.execute("SELECT * FROM deals")]
        unranked = [_deal(r, ranked=False) for r in conn.execute("SELECT * FROM unranked_deals")]
    return ranked + unranked


LIVING_ROOMS = ("living", "bedroom", "kitchen")


def cover_photo(listing: Mapping[str, Any]) -> str | None:
    """The photo a list row shows: the first room the image model accepted, not a floor plan."""
    photos = _photos(listing)
    for wanted in (lambda p: p.get("status") == "accepted" and p.get("room") in LIVING_ROOMS,
                   lambda p: p.get("status") == "accepted",
                   lambda p: True):
        for photo in photos:
            if wanted(photo):
                return photo["url"]
    return None


def thumbnails(updated_db: Path | None = None) -> dict[str, str]:
    """Cover photo URL per opportunity (and per ad URL), for list rows."""
    path = updated_db or paths.UPDATED_DB
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with closing(_ro(path)) as conn:
        for table in ("listings", "removed_listings"):
            try:
                rows = conn.execute(
                    f"SELECT opportunity_id, url, photo_urls_json, layer_1_features_v2 FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                cover = cover_photo(dict(row))
                if cover:
                    for key in (row["opportunity_id"], row["url"]):
                        if key:
                            out.setdefault(str(key), cover)
    return out


def meta(final_db: Path | None = None) -> dict[str, Any]:
    """``ranking_metadata`` (as_of, assumptions, counts, rent model…) plus neighborhoods."""
    path = final_db or paths.FINAL_DB
    if not path.exists():
        return {}
    with closing(_ro(path)) as conn:
        out = {r["key"]: _loads(r["value_json"], None) for r in conn.execute("SELECT key, value_json FROM ranking_metadata")}
        out["neighborhoods"] = [dict(r) for r in conn.execute("SELECT * FROM neighborhoods ORDER BY name")]
    out["built_at"] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return out


def ranking_built_at(final_db: Path | None = None) -> str | None:
    """When ``final_deals.db`` was last published (cheap: one stat, no read)."""
    path = final_db or paths.FINAL_DB
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path.exists() else None


def _photos(listing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Photo URLs with the image model's verdict per photo, when it can be matched."""
    urls = [str(u) for u in _loads(listing.get("photo_urls_json"), [])]
    features = _loads(listing.get("layer_1_features_v2"), {}) or {}
    provenance = features.get("image_provenance") or []
    scores = features.get("image_scores") or []
    aligned = len(scores) == len(provenance)
    by_url: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(provenance):
        info = {"status": item.get("status"), "error": item.get("error_category")}
        if aligned:
            score = scores[index]
            info.update(room=score.get("room_type"), score=score.get("condition_score"))
        if item.get("source_url"):
            by_url[str(item["source_url"])] = info
    return [{"url": url, **by_url.get(url, {})} for url in urls]


def _listing_rows(conn: sqlite3.Connection, opportunity_id: str, urls: Sequence[str]) -> list[dict[str, Any]]:
    """Advertisement rows of an opportunity: by its id or any of its ads' URLs, best first.

    The ranking can name an ad that a later update dropped or merged under another id, so
    every member URL is tried; a row with photos and a description is preferred.
    """
    wanted = [u for u in urls if u]
    marks = ",".join("?" * len(wanted)) or "''"
    rows: list[dict[str, Any]] = []
    for table in ("listings", "removed_listings"):
        try:
            rows += [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE opportunity_id = ? OR url IN ({marks})", (opportunity_id, *wanted))]
        except sqlite3.OperationalError:
            continue
    first = wanted[0] if wanted else None
    rows.sort(key=lambda r: (not _loads(r.get("photo_urls_json"), []), r.get("url") != first, r.get("url") or ""))
    return rows


def flat_detail(opportunity_id: str, *, final_db: Path | None = None, updated_db: Path | None = None
                ) -> dict[str, Any] | None:
    """One opportunity: its ranking row, the advertisement behind it, photos and description."""
    deal = next((d for d in flats(final_db) if d.get("opportunity_id") == opportunity_id), None)
    if deal is None:
        return None
    listing: dict[str, Any] = {}
    path = updated_db or paths.UPDATED_DB
    if path.exists():
        with closing(_ro(path)) as conn:
            rows = _listing_rows(conn, opportunity_id, [deal.get("url"), *(deal.get("source_urls") or [])])
        if rows:
            listing = rows[0]
    raw = _loads(listing.get("raw_json"), {}) or {}
    features = _loads(listing.get("layer_1_features_v2"), {}) or {}
    from greeceapt.gui.checklist import prefill, verdicts_of

    known = {"title": listing.get("title") or raw.get("title"), "description": raw.get("description"),
             "energy_class": listing.get("energy_class"),
             "latitude": raw.get("latitude"), "longitude": raw.get("longitude")}
    return {
        "checklist": prefill(deal, known, verdicts_of(features)) if deal.get("ranked") else [],
        "deal": deal,
        "listing": {
            "url": listing.get("url") or deal.get("url"),
            "title": listing.get("title") or raw.get("title") or deal.get("title"),
            "description": plain_text(raw.get("description") or listing.get("description")),
            "year_built": listing.get("year_built"), "renovation_year": listing.get("renovation_year"),
            "bedrooms": listing.get("bedrooms"), "bathrooms": listing.get("bathrooms"),
            "energy_class": listing.get("energy_class"), "heating_type": listing.get("heating_type"),
            "address": listing.get("address_raw"), "neighborhood_raw": listing.get("neighborhood_raw"),
            "publication_date": listing.get("publication_date"),
            "latitude": raw.get("latitude"), "longitude": raw.get("longitude"),
            "visual_confidence": listing.get("visual_confidence_v2"),
        },
        "photos": _photos(listing),
        "room_scores": features.get("room_scores") or {},
    }


# ── Files ──────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def history() -> dict[str, Any]:
    from greeceapt.rent import config as rent_cfg

    pairs_path = paths.DATA_DIR / "rent_sale_pairs.json"
    return {
        "updates": list(reversed(_read_jsonl(paths.DATA_DIR / "update_log.jsonl"))),
        "update_report": _read_text(paths.UPDATE_REPORT_MD),
        "rent_model_report": _read_text(rent_cfg.RENT_MODEL_REPORT),
        "rent_model_history": _read_jsonl(rent_cfg.RENT_MODEL_HISTORY),
        "pairs": _loads(_read_text(pairs_path), None),
        "rank_history_report": _read_text(paths.DATA_DIR / "rank_history_report.md"),
    }


def last_update_text() -> str:
    from greeceapt.update import format_status

    return format_status()


def notices(final_db: Path | None = None) -> list[dict[str, str]]:
    """Things worth doing now, each with the action that does it."""
    from greeceapt.rent import config as rent_cfg

    out: list[dict[str, str]] = []
    model_path = rent_cfg.RENT_MODEL_JSON
    if model_path.exists() and rent_cfg.RENTALS_DB.exists():
        try:
            model_meta = json.loads(model_path.read_text(encoding="utf-8")).get("meta") or {}
        except ValueError:
            model_meta = {}
        trained_scored = (model_meta.get("vision") or {}).get("n_visual")
        with closing(_ro(rent_cfg.RENTALS_DB)) as conn:
            scored_now = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE visual_status_v2 = 'accepted'").fetchone()[0]
        # n_visual counts training rows (duplicates dropped), so only a clear gap is reported
        if isinstance(trained_scored, int) and scored_now - trained_scored >= RETRAIN_NOTICE_MIN:
            out.append({"text": f"{scored_now - trained_scored:,} rentals were scored by the image model since "
                                "the rent model was trained.", "action": "rent_train"})
    model_mtime = model_path.stat().st_mtime if model_path.exists() else None
    path = final_db or paths.FINAL_DB
    if model_mtime and path.exists() and path.stat().st_mtime < model_mtime:
        out.append({"text": "The rent model is newer than the ranking.", "action": "score_only"})
    try:
        pending = len(json.loads((paths.DATA_DIR / "opportunity_review.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pending = 0
    if pending:
        out.append({"text": f"{pending} possible duplicate pairs wait for a decision.", "action": "review"})
    return out


def neighborhood_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Best and median conservative net yield of the ranked flats per neighborhood."""
    by: dict[str, list[float]] = {}
    for row in rows:
        if row.get("ranked") and row.get("net_yield_lo") is not None:
            by.setdefault(str(row.get("neighborhood")), []).append(float(row["net_yield_lo"]))
    out = {}
    for hood, values in by.items():
        values.sort()
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        out[hood] = {"best": values[-1], "median": median, "n": len(values)}
    return out
