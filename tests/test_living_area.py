"""Living area when the listed area adds an attic, terrace or storage (phrases from Nov 2026 ads)."""

from __future__ import annotations

import pytest

from greeceapt.scoring.living_area import apply_living_area, living_area


@pytest.mark.parametrize(
    ("text", "listed", "living"),
    [
        ("A ground-floor apartment with a total area of 54 square meters. It is spread over two levels, "
         "consisting of 30 square meters of ground-floor space and a 24-square-meter loft.", 54, 30),
        ("Renovated and fully furnished penthouse studio 15 sq.m. on Patmou street. It features air "
         "conditioning, a large 20 sq.m. terrace.", 35, 15),
        ("Διαμέρισμα 42 τ.μ. με πατάρι 8 τ.μ.", 50, 42),
    ],
)
def test_main_plus_extra_equal_to_the_listed_area_gives_the_living_area(text, listed, living):
    assert living_area(text, listed) == living


@pytest.mark.parametrize(
    ("text", "listed"),
    [
        ("Semi-basement apartment 63 sq.m. with 9 sq.m. storage room.", 63),        # storage not in the 63
        ("A beautiful shared garden of approximately 30 square meters.", 20),
        ("Studio 28 sq.m. with a balcony.", 28),
        ("", 40),
    ],
)
def test_extra_space_outside_the_listed_area_changes_nothing(text, listed):
    assert living_area(text, listed) is None


def test_the_listed_area_is_kept_beside_the_living_area():
    flat = {"area_sqm": 54.0, "description": "30 square meters of ground-floor space and a 24-square-meter loft."}
    assert apply_living_area([flat]) == 1
    assert (flat["area_sqm"], flat["area_listed"]) == (30.0, 54.0)
