"""Per-run reliability report for the Qwen condition score."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greeceapt.vision.reliability import reliability_report

FIXTURE = Path(__file__).parent / "fixtures" / "november_ranking_input_2026.json"
BASELINE = Path(__file__).parent / "fixtures" / "vision_reliability_baseline_2026-11.json"


def _row(score, *, price=50_000, area=40.0, year=1970, condition=None, description=None):
    return {"visual_status_v2": "accepted", "visual_score_v2": score, "price_eur": price,
            "area_sqm": area, "year_built": year, "condition": condition, "description": description}


def test_duplicate_agreement():
    rows = [_row(8.0), _row(7.0), _row(5.0, price=60_000), _row(5.2, price=60_000)]
    report = reliability_report(rows)["duplicate_agreement"]
    assert report["pairs"] == 2
    assert report["median_abs_delta"] == pytest.approx(0.6)
    assert report["share_delta_above_1"] == 0.0


def test_validity_against_publisher_text():
    rows = [_row(9.0, condition="Renovated"), _row(5.0, condition="Needs renovation"), _row(7.0)]
    validity = reliability_report(rows)["validity"]
    assert validity["renovated"]["n"] == 1 and validity["renovated"]["median"] == 9.0
    assert validity["needs_renovation"]["median"] == 5.0


def test_distribution_ignores_unaccepted_rows():
    rows = [_row(9.0), _row(3.0), {"visual_status_v2": "insufficient_evidence", "visual_score_v2": None}]
    dist = reliability_report(rows)["distribution"]
    assert dist["n"] == 2 and dist["share_ge_8"] == 0.5 and dist["share_lt_4"] == 0.5


def test_november_baseline_is_reproduced():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert reliability_report(rows) == json.loads(BASELINE.read_text(encoding="utf-8"))
