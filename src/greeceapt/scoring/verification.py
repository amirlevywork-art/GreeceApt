"""
Checks for traps found in the November data, on every ranked flat.

The rank (``rank_in_neighborhood``) is kept as computed; a flat that fails a check gets
``needs_check`` with its reasons (the GUI's "Needs a check" list and the Check tag). Nothing
is deleted.

Checks:
* ``location_mismatch`` — the description places the flat in a different known neighborhood
  ("in Ampelokipi - Pentagon" on a listing tagged Gizi) **that lies far from the flat's
  coordinates** (``FAR_KM``). Agents often name the next area or the umbrella one ("kolonos -
  kolokinthous", "patisia plateia koliatsou", "attiki"): of 37 such flags in Nov 2026, 36 named an
  area within 1.5 km of the flat, which sat at the centre of its own neighborhood. The rent is
  priced from the coordinates, so only a far-away name says the location may be wrong;
* ``price_outlier`` — price per m² below half the neighborhood median (≥ 5 ranked flats),
  typical of data errors or special legal situations;
* ``not_habitable`` — unfinished, or missing kitchen / bathroom.

A flat whose photos show no kitchen, living room or bedroom is not ranked at all
(``listing_filters.no_living_space_seen``). The "Before you buy" list is filled in by the GUI
(``gui/checklist.py``).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import replace
import math
from difflib import SequenceMatcher
from typing import Iterable, Mapping, Sequence

from greeceapt.storage.neighborhoods import canonical_neighborhood, load_aliases
from greeceapt.scoring.ranking import RankedDeal
from greeceapt.collect.floor_resolver import normalize_text

FAR_KM = 2.0                 # a named area this far from the flat really disagrees with its location
MIN_CENTER_POINTS = 5        # ads needed to place a neighborhood's centre
PRICE_OUTLIER_SHARE = 0.5
PRICE_OUTLIER_MIN_FLATS = 5
_MIN_TOKEN = 5

_NOT_HABITABLE_RE = re.compile(
    r"unfinished|ημιτελ\w*|under construction|υπο κατασκευη"
    r"|without (?:a )?(?:kitchen|bathroom)|χωρισ (?:κουζινα|μπανιο)"
    r"|necessary to install the kitchen|needs? (?:a )?(?:new )?kitchen to be installed"
)
_LOCATION_LEAD = r"(?:\bin|\blocated in|\bsituated in|\barea of|στην περιοχη|στην|στο|στα)\s+(?:the\s+)?(?:downtown\s+athens,\s+in\s+)?"

def _tokens(name: str) -> set[str]:
    return {token for token in re.split(r"[^\w]+", normalize_text(name)) if len(token) >= _MIN_TOKEN}


def _name_patterns(known: Iterable[str]) -> list[tuple[re.Pattern[str], str]]:
    """(pattern, canonical name) for every known name and alias, longest first."""
    names: dict[str, str] = {}
    for name in known:
        names.setdefault(normalize_text(name), canonical_neighborhood(name) or name)
        for part in re.split(r"\s+-\s+", name):
            if len(part) >= _MIN_TOKEN:
                names.setdefault(normalize_text(part), canonical_neighborhood(name) or name)
    for alias, target in load_aliases().items():
        names.setdefault(normalize_text(alias), target)
    # Common transliteration variants seen in descriptions.
    names.setdefault("ampelokipi", canonical_neighborhood("Ampelokipoi") or "Ampelokipoi")
    names.setdefault("victoria square", canonical_neighborhood("Victoria Square") or "Victoria Square")
    patterns = []
    for text in sorted(names, key=len, reverse=True):
        patterns.append((re.compile(_LOCATION_LEAD + re.escape(text) + r"(?!\w)"), names[text]))
    return patterns


def _similar(left: set[str], right: set[str]) -> bool:
    """Shared name token, allowing transliteration variants (exarcheia / exarchia)."""
    return any(SequenceMatcher(None, a, b).ratio() >= 0.8 for a in left for b in right)


def location_mismatch(description: object, own: str, known: Sequence[str], *,
                      patterns: list[tuple[re.Pattern[str], str]] | None = None) -> str | None:
    """The other neighborhood the description places the flat in, or ``None``.

    Not a mismatch when the named area shares a (similarly spelled) token with the flat's own
    neighborhood, or when the description also mentions the flat's own neighborhood — agents
    often name a parent or adjacent area next to the exact one.
    """
    text = normalize_text(description)
    if not text:
        return None
    own_tokens = _tokens(own)
    words = set(re.split(r"[^\w]+", text))
    for pattern, name in patterns if patterns is not None else _name_patterns(known):
        if pattern.search(text):
            if name == own or _similar(_tokens(name), own_tokens):
                return None
            if _similar({w for w in words if len(w) >= _MIN_TOKEN}, own_tokens):
                return None
            return name
    return None


def neighborhood_centers(points: Iterable[tuple[str, float, float]]) -> dict[str, tuple[float, float]]:
    """Median (latitude, longitude) of each canonical neighborhood with enough located ads."""
    by_hood: dict[str, list[tuple[float, float]]] = {}
    for name, lat, lon in points:
        hood = canonical_neighborhood(name) or name
        where = _coords({"latitude": lat, "longitude": lon})
        if hood and where is not None:
            by_hood.setdefault(hood, []).append(where)
    return {
        hood: (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
        for hood, pts in by_hood.items() if len(pts) >= MIN_CENTER_POINTS
    }


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (a[0] - b[0]) * 110.57
    dx = (a[1] - b[1]) * 111.32 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def _coords(listing: Mapping) -> tuple[float, float] | None:
    try:
        lat, lon = float(listing.get("latitude")), float(listing.get("longitude"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(lat) or math.isnan(lon) else (lat, lon)


def not_habitable(description: object) -> bool:
    from greeceapt.scoring.condition_text import construction_is_about_infrastructure

    text = normalize_text(description)
    return any(not construction_is_about_infrastructure(text, m) for m in _NOT_HABITABLE_RE.finditer(text))


def verify_ranked(
    ranked: Sequence[RankedDeal],
    listings_by_url: Mapping[str, Mapping],
    known_neighborhoods: Sequence[str],
    centers: Mapping[str, tuple[float, float]] | None = None,
) -> list[RankedDeal]:
    """``centers`` (``neighborhood_centers``): with them, a named area counts only when it lies
    more than ``FAR_KM`` from the flat; without them every other area named counts."""
    psqm: dict[str, list[float]] = {}
    for deal in ranked:
        if deal.price_eur and deal.area_sqm:
            psqm.setdefault(deal.neighborhood, []).append(deal.price_eur / deal.area_sqm)
    medians = {
        hood: statistics.median(values)
        for hood, values in psqm.items()
        if len(values) >= PRICE_OUTLIER_MIN_FLATS
    }

    patterns = _name_patterns(known_neighborhoods)
    out: list[RankedDeal] = []
    for deal in ranked:
        reasons: list[str] = []
        listing = listings_by_url.get(deal.url) or {}
        description = listing.get("description")
        other = location_mismatch(description, deal.neighborhood, known_neighborhoods, patterns=patterns)
        if other and centers is not None:
            flat, named = _coords(listing), centers.get(canonical_neighborhood(other) or other)
            if flat is None or named is None or _km(flat, named) <= FAR_KM:
                other = None          # a neighbouring or umbrella name, or nothing to measure
        if other:
            reasons.append(f"location_mismatch: description says {other}")
        median = medians.get(deal.neighborhood)
        if median and deal.price_eur and deal.area_sqm and deal.price_eur / deal.area_sqm < PRICE_OUTLIER_SHARE * median:
            reasons.append("price_outlier")
        if not_habitable(description):
            reasons.append("not_habitable")
        out.append(replace(deal, needs_check=bool(reasons), check_reasons=tuple(reasons)))
    return out
