"""Within-neighborhood ranking, pre-ranking collapse and actual market discount."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from greeceapt.scoring.ranking import (
    market_discount_pct,
    prepare_candidates,
    rank_deals,
)
from greeceapt.scoring.yield_model import YieldAssumptions
from greeceapt.collect.fingerprints import PhotoFingerprint
from tests.test_rent_yield import FlatRentModel

AS_OF = date(2026, 11, 2)
TEXT = (
    "Renovated one bedroom apartment close to the metro with new kitchen, new bathroom, "
    "security door and aluminium frames."
)


def _fp(label: str) -> dict:
    full = int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)
    center = int(hashlib.sha256(f"c:{label}".encode()).hexdigest()[:16], 16)
    return PhotoFingerprint("phash-v2", full, center).to_json()


def _row(url: str, *, price=50_000.0, area=46.0, score=9.0, floor_class="upper", floor=2.0,
         hood="Ampelokipoi", photos=None, **over) -> dict:
    photos = photos if photos is not None else [f"{url}-p{i}" for i in range(3)]
    row = {
        "url": url,
        "opportunity_id": "opp_" + hashlib.sha1(url.encode()).hexdigest()[:16],
        "neighborhood": hood,
        "price_eur": price,
        "area_sqm": area,
        "floor": floor,
        "floor_class": floor_class,
        "floor_conflict": 0,
        "floor_conflict_adjacent": 0,
        "visual_status_v2": "accepted",
        "visual_score_v2": score,
        "visual_accepted_count": 6,
        "duplicate_review_status": "none",
        "description": TEXT,
        "bedrooms": 1,
        "bathrooms": 1,
        "source_urls": [url],
        "photo_urls": photos,
        "photo_fingerprints": {p: _fp(p) for p in photos},
        "latitude": 37.99, "longitude": 23.76,
    }
    row.update(over)
    return row


def test_higher_yield_outranks_prettier_listing():
    pretty = _row("https://a/pretty", price=70_000.0, score=9.5)
    plain = _row("https://a/plain", price=45_000.0, score=8.0)
    ranked, unranked = rank_deals([pretty, plain], {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [deal.url for deal in ranked] == ["https://a/plain", "https://a/pretty"]
    assert unranked == []


def test_tie_buckets_are_transitive():
    # Net yields 0.01 % apart may straddle buckets; ordering must be a strict weak order.
    rows = [_row(f"https://t/{i}", price=50_000.0 + 40 * i, score=8.0) for i in range(3)]
    ranked, _ = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    keys = [deal.net_yield_sort_key for deal in ranked]
    assert keys == sorted(keys)
    buckets = [key[0] for key in keys]
    assert buckets == sorted(buckets)


def test_ties_within_a_bucket_break_by_confidence_then_risk_then_condition():
    sure = _row("https://t/sure", score=8.0)
    unsure = _row("https://t/unsure", score=8.0, description="Short.")
    ranked, _ = rank_deals([unsure, sure], {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [deal.url for deal in ranked] == ["https://t/sure", "https://t/unsure"]


def test_pending_duplicates_and_unknown_floors_are_unranked_with_reasons():
    rows = [
        _row("https://u/pending", duplicate_review_status="pending"),
        _row("https://u/floor", floor_class=None, floor=None),
        _row("https://u/ok"),
    ]
    ranked, unranked = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [deal.url for deal in ranked] == ["https://u/ok"]
    assert {deal.url: deal.estimate.exclusion_reason for deal in unranked} == {
        "https://u/pending": "duplicate_review_pending",
        "https://u/floor": "floor_unknown",
    }
    assert all(deal.estimate.all_in_cost is None for deal in unranked)


def test_opportunity_appears_once_in_ranked_output():
    a = _row("https://d/1")
    b = _row("https://d/2", opportunity_id=a["opportunity_id"])
    ranked, unranked = rank_deals([a, b], {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert len(ranked) == 1
    assert unranked[0].estimate.exclusion_reason == "duplicate_opportunity_id"


def test_actual_market_discount():
    assert market_discount_pct(50_000 / 46, 1.45, 1883.3) == pytest.approx(16.31, abs=0.01)
    assert market_discount_pct(3_000.0, 1.0, 2_000.0) == pytest.approx(-50.0)
    assert market_discount_pct(1_000.0, 1.0, None) is None
    assert market_discount_pct(1_000.0, 1.0, 0.0) is None


def test_thin_baseline_never_blocks_yield_ranking():
    ranked, _ = rank_deals([_row("https://k/1", hood="Kipseli")], {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert len(ranked) == 1
    assert ranked[0].market_discount_pct is None


def test_market_discount_uses_floor_adjusted_baseline():
    ranked, _ = rank_deals(
        [_row("https://m/1", floor_class="basement", floor=-1.0)], {"Ampelokipoi": 1883.3}, YieldAssumptions(), rent_model=FlatRentModel()
    )
    assert ranked[0].market_discount_pct == pytest.approx(16.31, abs=0.01)


def test_renovation_budgets_apply_by_opportunity():
    row = _row("https://r/1")
    ranked, _ = rank_deals([row], {}, YieldAssumptions(), rent_model=FlatRentModel(),
                           renovation_budgets={row["opportunity_id"]: 10_000.0})
    assert ranked[0].estimate.all_in_cost == pytest.approx(64_000.0)


# ── Pre-ranking collapse (pass b) ─────────────────────────────────────────────

def test_leftover_absorbed_row_is_removed():
    survivor = _row("https://s/1", source_urls=["https://s/1", "https://s/old"])
    leftover = _row("https://s/old", price=55_000.0)
    candidates, violations = prepare_candidates([survivor, leftover], {})
    assert [row["url"] for row in candidates] == ["https://s/1"]
    assert violations == []


def test_same_property_from_different_runs_collapses():
    photos = ["shared-1", "shared-2", "shared-3"]
    a = _row("https://x/1", photos=photos)
    b = _row("https://y/1", photos=photos, price=51_000.0)
    candidates, _ = prepare_candidates([a, b], {})
    assert len(candidates) == 1
    assert sorted(candidates[0]["source_urls"]) == ["https://x/1", "https://y/1"]


def test_lookalike_hard_negative_stays_separate():
    a = _row("https://x/1")
    b = _row("https://y/1", floor_class="basement", floor=-1.0)
    candidates, _ = prepare_candidates([a, b], {})
    assert len(candidates) == 2


def test_rows_are_rebuilt_from_raw_json_with_db_columns_winning():
    raw = {"url": "https://j/1", "description": TEXT, "photo_fingerprints": {}, "price_eur": 1.0}
    db_row = {"url": "https://j/1", "price_eur": 50_000.0, "raw_json": json.dumps(raw),
              "source_urls_json": json.dumps(["https://j/1"])}
    candidates, _ = prepare_candidates([db_row], {})
    assert candidates[0]["price_eur"] == 50_000.0
    assert candidates[0]["description"] == TEXT


def test_rows_from_before_floor_resolution_are_resolved_on_rebuild():
    legacy_spitogatos = {"url": "https://www.spitogatos.gr/en/property/1", "source": "spitogatos",
                         "floor": 2, "raw_json": json.dumps({"description": "Floor: Semi-basement"})}
    legacy_xe = {"url": "https://www.xe.gr/x/1", "source": "xe", "floor": 3, "raw_json": "{}"}
    candidates, _ = prepare_candidates([legacy_spitogatos, legacy_xe], {})
    by_url = {row["url"]: row for row in candidates}
    assert by_url["https://www.spitogatos.gr/en/property/1"]["floor_class"] == "semi_basement"
    assert (by_url["https://www.xe.gr/x/1"]["floor_class"], by_url["https://www.xe.gr/x/1"]["floor"]) == ("upper", 3.0)


def test_ranks_restart_in_every_neighborhood_and_never_compare_across():
    rows = [
        _row("https://n/a1", hood="Ampelokipoi", price=50_000.0),
        _row("https://n/a2", hood="Ampelokipoi", price=60_000.0),
        _row("https://n/k1", hood="Kipseli", price=90_000.0),
    ]
    ranked, _ = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [(deal.neighborhood, deal.rank_in_neighborhood, deal.url) for deal in ranked] == [
        ("Ampelokipoi", 1, "https://n/a1"),
        ("Ampelokipoi", 2, "https://n/a2"),
        ("Kipseli", 1, "https://n/k1"),
    ]
