"""Tests for greeceapt.scoring."""

from greeceapt.scoring.market_analytics import compute_neighborhood_baselines, floor_multiplier
from greeceapt.scoring.util import trimmed_mean


def test_trimmed_mean_symmetric():
    assert trimmed_mean([1.0, 2.0, 3.0, 100.0], 0.25) == 2.5


def test_floor_multiplier_bands():
    assert floor_multiplier(None) == 1.0
    assert floor_multiplier(-1) == 1.45
    assert floor_multiplier(0) == 1.10
    assert floor_multiplier(1) == 1.0
    assert floor_multiplier(5) == 0.90


def test_compute_neighborhood_baselines():
    listings = [
        {
            "neighborhood": "HoodA",
            "price_eur": 100_000 + i * 500,
            "area_sqm": 50,
            "floor": 1,
        }
        for i in range(10)
    ]
    hood_bs, excluded, min_n = compute_neighborhood_baselines(listings, trim_pct=0.0)
    assert "HoodA" in hood_bs
    assert hood_bs["HoodA"] > 0
    assert excluded == 0
    assert min_n == 10


def test_compute_neighborhood_baselines_skips_small_hood():
    listings = [
        {
            "neighborhood": "Tiny",
            "price_eur": 100_000,
            "area_sqm": 50,
            "floor": 1,
        }
        for _ in range(5)
    ]
    listings += [
        {
            "neighborhood": f"Other{i}",
            "price_eur": 110_000 + i * 100,
            "area_sqm": 50,
            "floor": 1,
        }
        for i in range(5)
    ]
    hood_bs, excluded, min_n = compute_neighborhood_baselines(listings, trim_pct=0.0)
    assert not hood_bs
    assert excluded == 6
    assert min_n == 10


