"""Pending duplicate pairs for the GUI, and saving a same / different decision."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from greeceapt.storage import paths
from greeceapt.collect.opportunity_dedupe import load_decisions, merge_decisions, write_decisions

MAX_PHOTOS = 12
_listings_cache: dict[str, Any] = {"mtime": None, "by_url": {}}


def _listings_by_url(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    if _listings_cache["mtime"] != mtime:
        rows = json.loads(path.read_text(encoding="utf-8"))
        by_url: dict[str, dict] = {}
        for row in rows if isinstance(rows, list) else []:
            for url in [row.get("url"), *(row.get("source_urls") or [])]:
                if url:
                    by_url.setdefault(str(url), row)
        _listings_cache.update(mtime=mtime, by_url=by_url)
    return _listings_cache["by_url"]


def _reviews(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def pending(*, review_path: Path | None = None, decisions_path: Path | None = None,
            listings_path: Path | None = None, held_urls: set[str] | None = None) -> list[dict[str, Any]]:
    """Undecided pairs, those holding a flat out of the ranking first."""
    decisions = load_decisions(decisions_path or paths.OPPORTUNITY_DECISIONS_JSON)
    listings = _listings_by_url(listings_path or paths.LISTINGS_JSON)
    held = held_urls or set()
    out = []
    for review in _reviews(review_path or paths.OPPORTUNITY_REVIEW_JSON):
        decided = decisions.get(review.get("pair_key"))
        if decided and decided.get("identity") == review.get("identity"):
            continue
        sides = []
        for url in review.get("urls") or []:
            listing = listings.get(url) or {}
            sides.append({
                "url": url,
                **(review.get("fields") or {}).get(url, {}),
                "title": listing.get("title"),
                "description": listing.get("description"),
                "photos": [str(p) for p in (listing.get("photo_urls") or [])][:MAX_PHOTOS],
            })
        out.append({
            "pair_key": review.get("pair_key"), "reason": review.get("reason"),
            "signals": review.get("signals") or [], "photo_matches": review.get("photo_matches"),
            "description_similarity": review.get("description_similarity"),
            "holds_ranking": any(url in held for url in review.get("urls") or []),
            "sides": sides,
        })
    out.sort(key=lambda r: (not r["holds_ranking"], str(r["pair_key"])))
    return out


def decide(pair_key: str, decision: str, *, review_path: Path | None = None,
           decisions_path: Path | None = None, today: date | None = None) -> dict[str, Any]:
    """Record one decision; it applies from the next update or re-score."""
    if decision not in {"same", "different"}:
        raise ValueError("decision must be 'same' or 'different'")
    reviews = _reviews(review_path or paths.OPPORTUNITY_REVIEW_JSON)
    if not any(r.get("pair_key") == pair_key for r in reviews):
        raise KeyError(pair_key)
    path = decisions_path or paths.OPPORTUNITY_DECISIONS_JSON
    payload = merge_decisions(load_decisions(path), {pair_key: decision}, reviews,
                              decided_on=(today or date.today()).isoformat())
    write_decisions(path, payload)
    return payload["decisions"][pair_key]


def held_urls(flats: list[Mapping[str, Any]]) -> set[str]:
    """Advertisements of flats kept out of the ranking by a pending duplicate review."""
    out: set[str] = set()
    for flat in flats:
        if flat.get("exclusion_reason") == "duplicate_review_pending":
            out.update(str(u) for u in [flat.get("url"), *(flat.get("source_urls") or [])] if u)
    return out
