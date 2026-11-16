"""Deterministic scoring of observable apartment-condition evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from greeceapt.vision.types import ImageScore, ImageVerdict, ListingVisionResult

ATTRIBUTE_LEVELS: dict[str, dict[str, float]] = {
    "visible_damage": {"none": 10.0, "minor": 6.0, "major": 2.0},
    "wall_condition": {"fresh": 10.0, "worn": 6.0, "peeling_cracked": 2.0},
    "flooring_condition": {"modern": 10.0, "dated_ok": 6.0, "worn": 3.0},
    "kitchen_age": {"modern": 10.0, "dated": 6.0, "original": 3.0},
    "bathroom_age": {"modern": 10.0, "dated": 6.0, "original": 3.0},
    "windows_age": {"modern": 9.0, "dated": 5.0},
    "renovation_signals": {"clear": 10.0, "partial": 6.0, "none": 4.0},
}

ATTRIBUTE_WEIGHTS: dict[str, float] = {
    "visible_damage": 1.4,
    "wall_condition": 1.2,
    "flooring_condition": 1.0,
    "kitchen_age": 1.3,
    "bathroom_age": 1.3,
    "windows_age": 0.8,
    "renovation_signals": 1.2,
}

QUALITY_MULTIPLIERS: dict[str, float] = {
    "good": 1.0,
    "dark": 0.8,
    "blurry": 0.65,
    "obstructed": 0.6,
    "invalid": 0.0,
}

ROOM_WEIGHTS: dict[str, float] = {
    "bathroom": 1.3,
    "kitchen": 1.3,
    "living": 1.0,
    "bedroom": 1.0,
    "hallway": 0.7,
    "other": 0.8,
}

MIN_EVIDENCE_ATTRIBUTES = 2
MIN_EVIDENCE_COVERAGE = 0.35


def _applicable_attributes(verdict: ImageVerdict) -> tuple[str, ...]:
    attrs = [
        "visible_damage",
        "wall_condition",
        "flooring_condition",
        "windows_age",
        "renovation_signals",
    ]
    if verdict.room_type == "kitchen":
        attrs.append("kitchen_age")
    elif verdict.room_type == "bathroom":
        attrs.append("bathroom_age")
    return tuple(attrs)


def score_verdict(verdict: ImageVerdict) -> ImageScore:
    """Convert visible evidence into a condition score without model discretion."""
    if verdict.image_quality == "invalid":
        return ImageScore(
            condition_score=None,
            evidence_coverage=0.0,
            effective_confidence=0.0,
            status="invalid_image",
            room_type=verdict.room_type,
            verdict=verdict,
        )

    applicable = _applicable_attributes(verdict)
    possible_weight = sum(ATTRIBUTE_WEIGHTS[attr] for attr in applicable)
    weighted_score = 0.0
    observed_weight = 0.0
    observed_count = 0
    for attr in applicable:
        value = getattr(verdict, attr)
        level = ATTRIBUTE_LEVELS[attr].get(value)
        if level is None:
            continue
        weight = ATTRIBUTE_WEIGHTS[attr]
        weighted_score += weight * level
        observed_weight += weight
        observed_count += 1

    coverage = observed_weight / possible_weight if possible_weight else 0.0
    quality = QUALITY_MULTIPLIERS[verdict.image_quality]
    effective_confidence = quality * (0.25 * verdict.confidence + 0.75 * coverage)

    if observed_count < MIN_EVIDENCE_ATTRIBUTES or coverage < MIN_EVIDENCE_COVERAGE:
        condition_score = None
        status = "insufficient_evidence"
    else:
        condition_score = round(weighted_score / observed_weight, 2)
        condition_score = max(1.0, min(10.0, condition_score))
        status = "accepted"

    return ImageScore(
        condition_score=condition_score,
        evidence_coverage=coverage,
        effective_confidence=effective_confidence,
        status=status,
        room_type=verdict.room_type,
        verdict=verdict,
    )


def _weighted_median(images: Sequence[ImageScore]) -> float:
    ordered = sorted(images, key=lambda image: float(image.condition_score or 0.0))
    total_weight = sum(image.effective_confidence for image in ordered)
    threshold = total_weight / 2.0
    cumulative = 0.0
    for image in ordered:
        cumulative += image.effective_confidence
        if cumulative >= threshold:
            return float(image.condition_score)
    return float(ordered[-1].condition_score)


def aggregate_listing(images: Sequence[ImageScore]) -> ListingVisionResult:
    """Aggregate distinct image scores without letting photo count weight rooms."""
    eligible: list[ImageScore] = []
    for image in images:
        verdict = image.verdict
        if (
            image.status == "accepted"
            and image.condition_score is not None
            and image.room_type in ROOM_WEIGHTS
            and verdict is not None
            and verdict.is_interior
        ):
            eligible.append(image)

    rooms: dict[str, list[ImageScore]] = defaultdict(list)
    for image in eligible:
        rooms[image.room_type].append(image)

    room_scores: dict[str, float] = {}
    room_confidences: dict[str, float] = {}
    for room_type, room_images in rooms.items():
        room_scores[room_type] = _weighted_median(room_images)
        room_confidences[room_type] = sum(
            image.effective_confidence for image in room_images
        ) / len(room_images)

    contribution_weights = {
        room_type: ROOM_WEIGHTS[room_type] * room_confidences[room_type]
        for room_type in room_scores
    }
    contribution_total = sum(contribution_weights.values())
    candidate_score = (
        sum(
            room_scores[room_type] * contribution_weights[room_type]
            for room_type in room_scores
        )
        / contribution_total
        if contribution_total
        else None
    )

    enough_evidence = len(eligible) >= 2 or (
        len(eligible) == 1 and eligible[0].effective_confidence >= 0.85
    )
    condition_score = candidate_score if enough_evidence else None

    fixed_weight_total = sum(ROOM_WEIGHTS[room_type] for room_type in room_scores)
    listing_confidence = (
        sum(
            ROOM_WEIGHTS[room_type] * room_confidences[room_type]
            for room_type in room_scores
        )
        / fixed_weight_total
        if fixed_weight_total
        else 0.0
    )

    if condition_score is not None:
        status = "accepted"
    elif not eligible and any(image.status == "model_error" for image in images):
        status = "model_error"
    else:
        status = "insufficient_evidence"

    return ListingVisionResult(
        condition_score=condition_score,
        effective_confidence=listing_confidence,
        status=status,
        image_scores=tuple(images),
        room_scores=room_scores,
        accepted_count=len(eligible),
        rejected_count=len(images) - len(eligible),
    )
