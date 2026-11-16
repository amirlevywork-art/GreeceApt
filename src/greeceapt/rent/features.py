"""
Hedonic rent features — one definition shared by rentals (training) and sale listings (scoring).

Only inputs that exist for both sides are features. Rental-only signals that bias asking rents
(furnished, days on market, price cut) are *controls*: they are fitted, then set to 0 at
prediction time, so a sale flat is priced as "unfurnished, freshly listed".
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Mapping

import numpy as np


VISUAL_CENTER = 6.5
AREA_CENTER = 40.0

FEATURES: tuple[str, ...] = (
    "log_area",
    "floor_basement",
    "floor_semi_basement",
    "floor_ground",
    "floor_upper_3_4",
    "floor_upper_5p",
    "built_1970_1981",
    "built_1982_2005",
    "built_2006p",
    "year_unknown",
    # No "renovated" / "needs renovation" from the ad text (design decision 2026-11-16): condition
    # comes from the photos ("visual") only. The text added +5.6 % to a renovated basement that
    # the image score had already rated 9.1, and that flat lets for €400 against €536 predicted.
    "energy_good",
    "energy_poor",
    "two_plus_bedrooms",
    "visual",
    "visual_missing",
    # controls (zeroed when predicting)
    "furnished",
    "log_days_on_market",
    "price_reduced",
)
CONTROLS: frozenset[str] = frozenset({"furnished", "log_days_on_market", "price_reduced"})

LABELS: dict[str, str] = {
    "log_area": "area elasticity (×log area)",
    "floor_basement": "basement vs 1st–2nd floor",
    "floor_semi_basement": "semi-basement vs 1st–2nd floor",
    "floor_ground": "ground / raised ground vs 1st–2nd floor",
    "floor_upper_3_4": "3rd–4th floor vs 1st–2nd floor",
    "floor_upper_5p": "5th floor+ vs 1st–2nd floor",
    "built_1970_1981": "built 1970–81 vs before 1970",
    "built_1982_2005": "built 1982–2005 vs before 1970",
    "built_2006p": "built 2006+ vs before 1970",
    "year_unknown": "year unknown vs before 1970",
    "energy_good": "energy class A+–B vs C/D/unknown",
    "energy_poor": "energy class E–G vs C/D/unknown",
    "two_plus_bedrooms": "2+ bedrooms (same area)",
    "visual": "+1 point visual condition (Qwen V2)",
    "visual_missing": "no visual score (not in sample)",
    "furnished": "furnished (control)",
    "log_days_on_market": "×log(1+days on market) (control)",
    "price_reduced": "price already cut (control)",
}


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def accepted_visual(listing: Mapping[str, Any]) -> float | None:
    if listing.get("visual_status_v2") != "accepted":
        return None
    return _num(listing.get("visual_score_v2"))


def feature_row(
    listing: Mapping[str, Any],
    *,
    for_prediction: bool,
    visual_override: float | None = None,
    as_of: date | None = None,
) -> dict[str, float]:
    """Feature dict for one listing. ``for_prediction`` zeroes the rental-only controls."""
    area = _num(listing.get("area_sqm")) or AREA_CENTER
    floor_class = listing.get("floor_class")
    storey = _num(listing.get("floor"))
    year = _num(listing.get("year_built"))
    energy = str(listing.get("energy_class") or "").upper().replace(" ", "")
    bedrooms = _num(listing.get("bedrooms"))
    visual = visual_override if visual_override is not None else accepted_visual(listing)

    row = {
        "log_area": math.log(area / AREA_CENTER),
        "floor_basement": float(floor_class == "basement"),
        "floor_semi_basement": float(floor_class == "semi_basement"),
        "floor_ground": float(floor_class in {"ground", "elevated_ground"}),
        "floor_upper_3_4": float(floor_class == "upper" and storey is not None and 3 <= storey < 5),
        "floor_upper_5p": float(floor_class == "upper" and storey is not None and storey >= 5),
        "built_1970_1981": float(year is not None and 1970 <= year < 1982),
        "built_1982_2005": float(year is not None and 1982 <= year < 2006),
        "built_2006p": float(year is not None and year >= 2006),
        "year_unknown": float(year is None),
        "energy_good": float(energy in {"A++", "A+", "A", "B+", "B"}),
        "energy_poor": float(energy in {"E", "F", "G", "Z", "H", "ZH"}),   # Ζ = F, Η = G
        "two_plus_bedrooms": float(bedrooms is not None and bedrooms >= 2),
        "visual": (visual - VISUAL_CENTER) if visual is not None else 0.0,
        "visual_missing": float(visual is None),
        "furnished": 0.0,
        "log_days_on_market": 0.0,
        "price_reduced": 0.0,
    }
    if not for_prediction:
        row["furnished"] = float(_num(listing.get("furnished")) == 1)
        first = _date(listing.get("first_publish_date"))
        seen = as_of or _date(listing.get("last_seen_at")) or _date(listing.get("scraped_at"))
        if first and seen and seen >= first:
            row["log_days_on_market"] = math.log1p((seen - first).days)
        row["price_reduced"] = float(_num(listing.get("price_reduced")) == 1)
    return row


def matrix(rows: list[dict[str, float]]) -> np.ndarray:
    return np.array([[r[name] for name in FEATURES] for r in rows], dtype=float)


# ── Location ──────────────────────────────────────────────────────────────────

def coords_km(lat: np.ndarray, lon: np.ndarray, lat0: float = 37.98) -> np.ndarray:
    """Equirectangular projection to km (accurate to <0.1 % across Athens)."""
    return np.column_stack((lon * 111.32 * math.cos(math.radians(lat0)), lat * 110.57))


def knn_mean(
    ref_xy: np.ndarray,
    ref_values: np.ndarray,
    query_xy: np.ndarray,
    k: int,
    *,
    exclude_self: bool,
    chunk: int = 512,
) -> np.ndarray:
    """Mean of ``ref_values`` over each query's k nearest reference points.

    ``exclude_self`` (query set == reference set) drops the point itself — leave-one-out, so a
    training row never sees its own rent through the location feature.
    """
    out = np.empty(len(query_xy))
    take = k + 1 if exclude_self else k
    take = min(take, len(ref_xy))
    for start in range(0, len(query_xy), chunk):
        q = query_xy[start:start + chunk]
        d2 = ((q[:, None, :] - ref_xy[None, :, :]) ** 2).sum(axis=2)
        if exclude_self:
            idx = np.arange(start, start + len(q))
            d2[np.arange(len(q)), idx] = np.inf
        nearest = np.argpartition(d2, take - 1, axis=1)[:, :take]
        if exclude_self:
            # the self column is inf → it lands last among `take`; drop the farthest
            dist = np.take_along_axis(d2, nearest, axis=1)
            order = np.argsort(dist, axis=1)[:, : take - 1]
            nearest = np.take_along_axis(nearest, order, axis=1)
        out[start:start + len(q)] = ref_values[nearest].mean(axis=1)
    return out
