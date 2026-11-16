"""
Cost and condition of a sale flat before its rent is estimated (``scoring/rent_yield.py``).

    all_in_cost = price × (1 + acquisition_rate) + renovation (supplied or estimated)

* **Renovation** — a flat that needs work is charged its renovation and then earns rent at
  ``RENOVATION_TARGET_SCORE`` (Qwen 8.0, the minimal comfortably lettable state, not a showroom),
  so it is not penalized twice. The photos alone decide it. The levels follow thresholds
  calibrated against 102 hand-labelled flats (Qwen scores about 1.8 points more generously than
  a human eye): "full" below 6.6, "medium" (kitchen or bathroom work) 6.6–7.4, "paint" 7.4–8.0,
  let as is from 8.0. The **cost moves smoothly with the score** (decided 2026-11-16): €/m² and the
  minimum job are interpolated between ``RENOVATION_ANCHORS`` — €500/m² (at least €12,000) at
  6.2 and below, €250/m² (at least €6,000) at 7.0, €40/m² at 7.7, nothing at 8.0 — so a flat
  at 7.38 no longer costs €8,500 more than one at 7.42. The minimum is there because a new
  bathroom and kitchen(ette) cost about the same in 12 m² as in 40 m². A user-supplied budget
  replaces the estimate.

A listing missing any rank-critical input gets ``exclusion_reason`` instead of a value.
Confidence describes evidence quality (no warning → high, one → medium, two or more → low);
risk flags such as basement or needs-renovation are reported separately.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Mapping

from greeceapt.scoring.condition_text import commercial_premises, publisher_condition
from greeceapt.collect.opportunity_dedupe import normalize_description

MIN_CONDITION_SCORE = 3.0
# Condition factor (score, factor) anchors, linear in between, 1.00 from 8.0 up: how far two
# condition scores are apart in rent terms (``vision.reliability``).
CONDITION_ANCHORS: tuple[tuple[float, float], ...] = ((3.0, 0.75), (4.0, 0.85), (6.0, 0.95), (8.0, 1.00))
# Floor factors: a flat with no usable floor is not ranked; Layer 1 orders its work by them.
FLOOR_FACTORS = {"basement": 0.70, "semi_basement": 0.80, "ground": 0.90, "elevated_ground": 0.90}
UPPER_LOW_FACTOR = 1.00   # floors 1–2
UPPER_HIGH_FACTOR = 1.02  # floors 3+
# Calibrated on 102 top-ranked sale flats labelled by eye from their photos (2026-11-10): Qwen 7
# looks like a human 5.5, so the old 5.0 / 6.0 cut-offs called 49 of 63 flats that need work "as
# is" (45/102 levels right). These cut-offs get 77/102 right (68–77 leave-one-out). Qwen cannot
# tell paint-only from kitchen/bathroom work (4 vs 28 labels, same scores), so the 6.6–7.4 band is
# charged the more likely medium job and the band just below 8.0 a cheap repaint.
FULL_RENOVATION_BELOW_SCORE = 6.6
MEDIUM_RENOVATION_BELOW_SCORE = 7.4
PAINT_BELOW_SCORE = 8.0
# Where each level's rate applies in full; the cost is linear in the score between them, so it
# has no step at the level thresholds (the middle of the full band's upper part, the middle of
# the medium band, the middle of the paint band).
FULL_RATE_AT_SCORE = 6.2
MEDIUM_RATE_AT_SCORE = 7.0
PAINT_RATE_AT_SCORE = 7.7
# A kitchen or bathroom the photos score below this needs its own job even when the flat as a
# whole looks fine (decided 2026-11-16): the overall score averages the rooms, so one bad kitchen
# among three clean bedrooms read 8+. Ads say "needs renovation" 5× as often for such flats
# (8.6 % vs 1.6 % of 412 scored flats, Nov 2026).
ROOM_JOB_BELOW_SCORE = 6.6
# A renovation does the least that pays: it brings the flat up to this Qwen score, the minimal
# comfortably lettable state (a human ~6.5; rentals median 8.9), which then becomes its condition
# (the condition factor here, the same score in the calibrated rent model).
RENOVATION_TARGET_SCORE = 8.0
SPARSE_DESCRIPTION_CHARS = 80
PRICE_SPREAD_WARNING = 0.02
THIN_VISUAL_EVIDENCE = 4
BASE_EXCLUDED_ASSUMPTIONS = ("market_rent_level", "income_tax", "enfia", "financing", "legal_use")


@dataclass(frozen=True)
class YieldAssumptions:
    acquisition_rate: float = 0.08
    full_renovation_eur_per_sqm: float = 500.0
    medium_renovation_eur_per_sqm: float = 250.0
    paint_eur_per_sqm: float = 40.0
    full_renovation_min_eur: float = 12_000.0
    medium_renovation_min_eur: float = 6_000.0
    # One room's job at a basic, rental standard (Greek price guides, Nov 2026): a 4–5 m² bathroom
    # €2,500–5,500 basic / €4,800–7,500 mid; a kitchenette (2–3 m of melamine units, laminate top,
    # plumbing, basic appliances) about €2,500–4,000, an 8–10 m² kitchen €3,500–5,000 economy.
    kitchen_job_eur: float = 4_000.0
    bathroom_job_eur: float = 4_500.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.acquisition_rate < 1.0) or math.isnan(self.acquisition_rate):
            raise ValueError(f"acquisition_rate must be in [0, 1), got {self.acquisition_rate}")
        if min(self.full_renovation_eur_per_sqm, self.medium_renovation_eur_per_sqm, self.paint_eur_per_sqm,
               self.full_renovation_min_eur, self.medium_renovation_min_eur) < 0:
            raise ValueError("renovation rates must be non-negative")

    def to_json(self) -> dict:
        return {
            "acquisition_rate": self.acquisition_rate,
            "full_renovation_eur_per_sqm": self.full_renovation_eur_per_sqm,
            "medium_renovation_eur_per_sqm": self.medium_renovation_eur_per_sqm,
            "paint_eur_per_sqm": self.paint_eur_per_sqm,
            "full_renovation_min_eur": self.full_renovation_min_eur,
            "medium_renovation_min_eur": self.medium_renovation_min_eur,
            "kitchen_job_eur": self.kitchen_job_eur,
            "bathroom_job_eur": self.bathroom_job_eur,
            "renovation_anchors": [list(anchor) for anchor in renovation_anchors(self)],
        }


@dataclass(frozen=True)
class YieldEstimate:
    all_in_cost: float | None
    confidence: str
    risk_flags: tuple[str, ...]
    evidence_warnings: tuple[str, ...]
    exclusion_reason: str | None
    renovation_budget: float = 0.0
    renovation_level: str = "none"  # none | paint | medium | full | supplied
    excluded_assumptions: tuple[str, ...] = field(default=BASE_EXCLUDED_ASSUMPTIONS)
    condition_score: float | None = None   # score rent is earned at: own, or the renovation target
    acquisition_cost: float | None = None  # price + acquisition costs, before any renovation


# ── Factors ───────────────────────────────────────────────────────────────────

def floor_factor(floor_class: str | None, floor_value: float | None) -> float | None:
    if floor_class in FLOOR_FACTORS:
        return FLOOR_FACTORS[floor_class]
    if floor_class == "upper" and floor_value is not None:
        return UPPER_HIGH_FACTOR if floor_value >= 3 else UPPER_LOW_FACTOR
    return None


def condition_factor(score: float | None) -> float | None:
    if score is None or math.isnan(score) or score < MIN_CONDITION_SCORE:
        return None
    if score >= CONDITION_ANCHORS[-1][0]:
        return CONDITION_ANCHORS[-1][1]
    for (left_score, left_factor), (right_score, right_factor) in zip(CONDITION_ANCHORS, CONDITION_ANCHORS[1:]):
        if left_score <= score <= right_score:
            fraction = (score - left_score) / (right_score - left_score)
            return left_factor + fraction * (right_factor - left_factor)
    return None  # pragma: no cover - anchors cover [3, 8]


def renovation_anchors(assumptions: "YieldAssumptions") -> tuple[tuple[float, float, float], ...]:
    """(score, €/m², minimum €) points the renovation cost is interpolated between."""
    return (
        (FULL_RATE_AT_SCORE, assumptions.full_renovation_eur_per_sqm, assumptions.full_renovation_min_eur),
        (MEDIUM_RATE_AT_SCORE, assumptions.medium_renovation_eur_per_sqm, assumptions.medium_renovation_min_eur),
        (PAINT_RATE_AT_SCORE, assumptions.paint_eur_per_sqm, 0.0),
        (PAINT_BELOW_SCORE, 0.0, 0.0),
    )


def renovation_cost(score: float, area: float, assumptions: "YieldAssumptions") -> float:
    """Euros to bring a flat at Qwen ``score`` up to the target: continuous in the score."""
    anchors = renovation_anchors(assumptions)
    if score >= anchors[-1][0]:
        return 0.0
    if score <= anchors[0][0]:
        rate, minimum = anchors[0][1], anchors[0][2]
    else:
        for (s0, r0, m0), (s1, r1, m1) in zip(anchors, anchors[1:]):
            if s0 <= score <= s1:
                t = (score - s0) / (s1 - s0)
                rate, minimum = r0 + t * (r1 - r0), m0 + t * (m1 - m0)
                break
    return max(rate * area, minimum)


def renovation_level(score: float) -> str:
    if score < FULL_RENOVATION_BELOW_SCORE:
        return "full"
    if score < MEDIUM_RENOVATION_BELOW_SCORE:
        return "medium"
    if score < PAINT_BELOW_SCORE:
        return "paint"
    return "none"


def room_scores(listing: Mapping[str, object]) -> dict[str, float]:
    """Per-room condition scores of the image model (``layer_1_features_v2.room_scores``)."""
    import json

    raw = listing.get("layer_1_features_v2")
    try:
        features = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return {}
    scores = (features or {}).get("room_scores") or {}
    return {str(k): float(v) for k, v in scores.items() if isinstance(v, (int, float))}


def room_jobs(listing: Mapping[str, object], assumptions: "YieldAssumptions") -> dict[str, float]:
    """``{"kitchen": €, "bathroom": €}`` for the rooms the photos say need their own job."""
    rooms, jobs = room_scores(listing), {}
    for room, cost in (("kitchen", assumptions.kitchen_job_eur), ("bathroom", assumptions.bathroom_job_eur)):
        if rooms.get(room) is not None and rooms[room] < ROOM_JOB_BELOW_SCORE:
            jobs[room] = cost
    return jobs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _truthy(value: object) -> bool:
    return bool(value) and str(value).strip().lower() not in {"0", "false", "none"}


def _offers(listing: Mapping[str, object]) -> list[Mapping]:
    offers: object = listing.get("advertised_offers")
    if offers is None and listing.get("advertised_offers_json"):
        try:
            offers = json.loads(str(listing["advertised_offers_json"]))
        except (TypeError, ValueError):
            offers = None
    if not isinstance(offers, list):
        return []
    return [offer for offer in offers if isinstance(offer, Mapping)]


def source_price_spread(listing: Mapping[str, object]) -> float | None:
    prices = [p for p in (_number(offer.get("price_eur")) for offer in _offers(listing)) if p and p > 0]
    if len(prices) < 2:
        return None
    return (max(prices) - min(prices)) / min(prices)


def _accepted_image_count(listing: Mapping[str, object]) -> int | None:
    count = _number(listing.get("visual_accepted_count"))
    if count is not None:
        return int(count)
    raw = listing.get("layer_1_features_v2")
    if raw:
        try:
            value = json.loads(str(raw)).get("accepted_count")
        except (TypeError, ValueError, AttributeError):
            return None
        return int(value) if isinstance(value, (int, float)) else None
    return None


def suspicious_room_counts(listing: Mapping[str, object], area: float) -> bool:
    bedrooms, bathrooms = _number(listing.get("bedrooms")), _number(listing.get("bathrooms"))
    if (bedrooms is not None and bedrooms < 0) or (bathrooms is not None and bathrooms < 0):
        return True
    if bedrooms is not None and bedrooms > max(6, math.floor(area / 8)):
        return True
    if bathrooms is not None and bathrooms > max(4, math.floor(area / 15)):
        return True
    return False


def _excluded_assumptions(renovation_budget: float, assumptions: YieldAssumptions) -> tuple[str, ...]:
    # Renovation is either supplied or estimated, so it is no longer an excluded assumption.
    return BASE_EXCLUDED_ASSUMPTIONS


def _excluded(reason: str, risks: set[str], renovation_budget: float, assumptions: YieldAssumptions) -> YieldEstimate:
    return YieldEstimate(
        None, "unranked", tuple(sorted(risks)), (), reason,
        renovation_budget=renovation_budget,
        excluded_assumptions=_excluded_assumptions(renovation_budget, assumptions),
    )


# ── Estimate ──────────────────────────────────────────────────────────────────

def estimate_yield(
    listing: Mapping[str, object],
    assumptions: YieldAssumptions,
    *,
    renovation_budget: float = 0.0,
) -> YieldEstimate:
    risks: set[str] = set()
    floor_class = listing.get("floor_class")
    if floor_class == "basement":
        risks.add("basement")
    elif floor_class == "semi_basement":
        risks.add("semi_basement")
    if _truthy(listing.get("floor_conflict_adjacent")):
        risks.add("floor_conflict_adjacent")
    stated_condition = publisher_condition(listing)
    if stated_condition == "needs_renovation":
        risks.add("needs_renovation")
    if listing.get("area_listed") is not None:
        risks.add("area_includes_extra_space")

    price, area = _number(listing.get("price_eur")), _number(listing.get("area_sqm"))
    if price is None or price <= 0:
        return _excluded("invalid_price", risks, renovation_budget, assumptions)
    if area is None or area <= 0:
        return _excluded("invalid_area", risks, renovation_budget, assumptions)
    if renovation_budget < 0 or math.isnan(renovation_budget):
        return _excluded("negative_renovation_budget", risks, renovation_budget, assumptions)
    if not str(listing.get("neighborhood") or "").strip():
        return _excluded("neighborhood_unknown", risks, renovation_budget, assumptions)
    if commercial_premises(listing.get("description")):
        return _excluded("commercial_premises", risks, renovation_budget, assumptions)
    if str(listing.get("duplicate_review_status") or "none") == "pending":
        risks.add("duplicate_review_pending")
        return _excluded("duplicate_review_pending", risks, renovation_budget, assumptions)
    if _truthy(listing.get("floor_conflict")):
        risks.add("floor_conflict")
        return _excluded("floor_conflict", risks, renovation_budget, assumptions)
    if floor_factor(str(floor_class) if floor_class else None, _number(listing.get("floor"))) is None:
        return _excluded("floor_unknown", risks, renovation_budget, assumptions)
    if listing.get("visual_status_v2") != "accepted":
        return _excluded("condition_unaccepted", risks, renovation_budget, assumptions)
    score = _number(listing.get("visual_score_v2"))
    if score is None or score < MIN_CONDITION_SCORE:
        return _excluded("condition_below_minimum", risks, renovation_budget, assumptions)
    # The photos decide the renovation (design decision 2026-11-16): an ad saying "needs renovation"
    # stays a risk flag but no longer forces a full renovation over a good image score
    # (19 ranked flats, Nov 2026, e.g. Kolonaki #1 at condition 8.0 charged €16,000).
    if renovation_budget > 0:
        level, renovation = "supplied", renovation_budget
    else:
        level, renovation = renovation_level(score), renovation_cost(score, area, assumptions)
        jobs = room_jobs(listing, assumptions) if level in ("none", "paint") else {}
        if jobs:      # a bad kitchen or bathroom in an otherwise fine flat: that room's job too
            level, renovation = "medium", renovation + sum(jobs.values())
            risks.update(f"{room}_work" for room in jobs)
    # A renovated flat earns rent at its target score, or its own when that is better (no double
    # penalty: its cost is charged).
    rent_score = score if level == "none" else max(score, RENOVATION_TARGET_SCORE)
    if level != "none":
        risks.add(f"renovation_{level}")

    warnings: list[str] = []
    spread = source_price_spread(listing)
    if spread is not None and spread > PRICE_SPREAD_WARNING:
        warnings.append("source_price_spread")
        risks.add("source_price_conflict")
    if len(normalize_description(listing.get("description"))) < SPARSE_DESCRIPTION_CHARS:
        warnings.append("sparse_description")
        risks.add("sparse_description")
    if suspicious_room_counts(listing, area):
        warnings.append("suspicious_room_counts")
        risks.add("suspicious_room_counts")
    if "floor_conflict_adjacent" in risks:
        warnings.append("floor_conflict_adjacent")
    accepted = _accepted_image_count(listing)
    if accepted is not None and accepted < THIN_VISUAL_EVIDENCE:
        warnings.append("thin_visual_evidence")
        risks.add("thin_visual_evidence")

    acquisition = price * (1 + assumptions.acquisition_rate)
    confidence = "high" if not warnings else "medium" if len(warnings) == 1 else "low"
    return YieldEstimate(
        all_in_cost=acquisition + renovation,
        confidence=confidence,
        risk_flags=tuple(sorted(risks)),
        evidence_warnings=tuple(warnings),
        exclusion_reason=None,
        renovation_budget=renovation,
        renovation_level=level,
        excluded_assumptions=_excluded_assumptions(renovation_budget, assumptions),
        condition_score=rent_score,
        acquisition_cost=acquisition,
    )


ESTIMATED_RENOVATIONS = frozenset({"paint", "medium", "full"})


def let_as_is(estimate: YieldEstimate, listing: Mapping[str, object]) -> YieldEstimate:
    """The flat is kept with its sitting tenant: no renovation, rent at its own condition.

    Decided 2026-11-16: when the ad says the flat is let now for at least the model's rent, the
    buyer keeps the tenant and does not renovate, so the estimated renovation leaves the cost.
    A renovation budget the user supplied is kept as given.
    """
    from dataclasses import replace

    if estimate.renovation_level not in ESTIMATED_RENOVATIONS or estimate.acquisition_cost is None:
        return estimate
    return replace(
        estimate,
        all_in_cost=estimate.acquisition_cost,
        renovation_budget=0.0,
        renovation_level="none",
        condition_score=_number(listing.get("visual_score_v2")),
        risk_flags=tuple(sorted({f for f in estimate.risk_flags if not f.startswith("renovation_")} | {"let_with_tenant"})),
    )
