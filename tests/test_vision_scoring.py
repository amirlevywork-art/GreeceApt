"""Behavior tests for deterministic, evidence-based image scoring."""

import pytest
from pydantic import ValidationError

from greeceapt.vision.scoring import aggregate_listing, score_verdict
from greeceapt.vision.types import ImageScore, ImageVerdict


def _verdict(**overrides) -> ImageVerdict:
    values = {
        "room_type": "living",
        "is_interior": True,
        "visible_damage": "none",
        "wall_condition": "fresh",
        "flooring_condition": "modern",
        "kitchen_age": "not_applicable",
        "bathroom_age": "not_applicable",
        "windows_age": "modern",
        "renovation_signals": "clear",
        "image_quality": "good",
        "confidence": 0.8,
    }
    values.update(overrides)
    return ImageVerdict(**values)


def test_extra_keys_are_rejected():
    with pytest.raises(ValidationError):
        _verdict(aesthetic_score=9)


def test_unknown_only_verdict_has_no_numeric_score():
    result = score_verdict(
        _verdict(
            visible_damage="unclear",
            wall_condition="unclear",
            flooring_condition="unclear",
            windows_age="unclear",
            renovation_signals="unclear",
        )
    )

    assert result.condition_score is None
    assert result.evidence_coverage == 0.0
    assert result.status == "insufficient_evidence"


def test_kitchen_excludes_bathroom_weight_from_coverage():
    result = score_verdict(
        _verdict(
            room_type="kitchen",
            kitchen_age="modern",
            bathroom_age="not_applicable",
            windows_age="unclear",
        )
    )

    # Known weight = damage 1.4 + walls 1.2 + floor 1.0 + kitchen 1.3 + renovation 1.2.
    # Applicable weight also includes windows 0.8, but never bathroom 1.3.
    assert result.evidence_coverage == pytest.approx(6.1 / 6.9)


def test_living_room_excludes_both_room_specific_weights():
    result = score_verdict(
        _verdict(
            room_type="living",
            kitchen_age="unclear",
            bathroom_age="unclear",
            windows_age="unclear",
        )
    )

    # Known weight = 4.8; applicable weight = 4.8 + windows 0.8.
    assert result.evidence_coverage == pytest.approx(4.8 / 5.6)


def test_known_design_example_has_exact_score_coverage_and_confidence():
    result = score_verdict(
        _verdict(
            room_type="kitchen",
            visible_damage="none",
            wall_condition="worn",
            flooring_condition="dated_ok",
            kitchen_age="original",
            bathroom_age="not_applicable",
            windows_age="unclear",
            renovation_signals="partial",
            confidence=0.8,
        )
    )

    # (1.4*10 + 1.2*6 + 1.0*6 + 1.3*3 + 1.2*6) / 6.1 = 6.2786...
    assert result.condition_score == 6.28
    assert result.evidence_coverage == pytest.approx(6.1 / 6.9)
    assert result.effective_confidence == pytest.approx(
        0.25 * 0.8 + 0.75 * (6.1 / 6.9)
    )
    assert result.status == "accepted"


def test_dark_image_reduces_confidence_not_condition_score():
    good = score_verdict(_verdict(image_quality="good"))
    dark = score_verdict(_verdict(image_quality="dark"))

    assert dark.condition_score == good.condition_score
    assert dark.effective_confidence == pytest.approx(good.effective_confidence * 0.8)


def test_invalid_image_has_zero_confidence_and_null_score():
    result = score_verdict(_verdict(image_quality="invalid"))

    assert result.condition_score is None
    assert result.effective_confidence == 0.0
    assert result.status == "invalid_image"


def _image_score(
    score: float | None,
    *,
    room_type: str = "living",
    confidence: float = 1.0,
    status: str = "accepted",
    is_interior: bool = True,
) -> ImageScore:
    room_specific = {}
    if room_type == "bathroom":
        room_specific["bathroom_age"] = "dated"
    elif room_type == "kitchen":
        room_specific["kitchen_age"] = "dated"
    verdict = _verdict(
        room_type=room_type,
        is_interior=is_interior,
        confidence=confidence,
        **room_specific,
    )
    return ImageScore(
        condition_score=score,
        evidence_coverage=1.0 if score is not None else 0.0,
        effective_confidence=confidence,
        status=status,
        room_type=room_type,
        verdict=verdict,
    )


def test_listing_uses_confidence_weighted_median_within_room():
    result = aggregate_listing(
        [
            _image_score(2.0, confidence=0.2),
            _image_score(8.0, confidence=0.8),
        ]
    )

    assert result.room_scores == {"living": 8.0}
    assert result.condition_score == 8.0


def test_listing_combines_rooms_with_fixed_room_weights():
    result = aggregate_listing(
        [
            _image_score(2.0, room_type="bathroom"),
            _image_score(8.0, room_type="living"),
        ]
    )

    assert result.condition_score == pytest.approx((2.0 * 1.3 + 8.0) / 2.3)
    assert result.effective_confidence == 1.0


def test_listing_excludes_non_interior_invalid_and_unknown_images():
    result = aggregate_listing(
        [
            _image_score(7.0, room_type="living"),
            _image_score(7.0, room_type="bedroom"),
            _image_score(1.0, room_type="exterior", is_interior=False),
            _image_score(1.0, room_type="floor_plan", is_interior=False),
            _image_score(None, status="invalid_image"),
            _image_score(None, status="insufficient_evidence"),
        ]
    )

    assert result.condition_score == 7.0
    assert result.accepted_count == 2
    assert result.rejected_count == 4
    assert set(result.room_scores) == {"living", "bedroom"}


def test_duplicate_angles_do_not_change_room_balanced_score():
    base = aggregate_listing(
        [_image_score(4.0, room_type="living"), _image_score(8.0, room_type="bathroom")]
    )
    with_duplicate_angles = aggregate_listing(
        [
            _image_score(4.0, room_type="living"),
            _image_score(4.0, room_type="living"),
            _image_score(4.0, room_type="living"),
            _image_score(8.0, room_type="bathroom"),
        ]
    )

    assert with_duplicate_angles.condition_score == base.condition_score


def test_listing_requires_two_images_when_single_image_confidence_is_low():
    result = aggregate_listing([_image_score(8.0, confidence=0.84)])

    assert result.condition_score is None
    assert result.status == "insufficient_evidence"


def test_one_high_confidence_image_satisfies_listing_exception():
    result = aggregate_listing([_image_score(8.0, confidence=0.85)])

    assert result.condition_score == 8.0
    assert result.status == "accepted"
