"""Euro rent and yields from the calibrated rent model, and the net-yield rank."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from greeceapt.storage.final_db import write_final_database_atomic
from greeceapt.scoring.ranking import rank_deals
from greeceapt.scoring.rent_yield import DEFAULT_COSTS, annual_costs, estimate_rent_yield
from greeceapt.scoring.yield_model import YieldAssumptions, estimate_yield

TEXT = "Bright one bedroom apartment close to the metro with kitchen, bathroom and balcony doors."


class FlatRentModel:
    """€12/m² × area, ×(1 + 0.05 per visual point above 6.5); conservative = 90 %."""

    def __init__(self):
        self.calls = []

    def predict_rent(self, listing, *, visual_override=None):
        self.calls.append((dict(listing), visual_override))
        visual = visual_override if visual_override is not None else float(listing["visual_score_v2"])
        rent = 12.0 * float(listing["area_sqm"]) * (1 + 0.05 * (visual - 6.5))
        return rent, rent * 0.9


def _row(url, *, price: float | None = 50_000.0, area=40.0, score=8.5, hood="Kypseli", **over):
    row = {
        "url": url,
        "opportunity_id": "opp_" + hashlib.sha1(url.encode()).hexdigest()[:16],
        "neighborhood": hood, "price_eur": price, "area_sqm": area,
        "floor": 2.0, "floor_class": "upper", "floor_conflict": 0, "floor_conflict_adjacent": 0,
        "visual_status_v2": "accepted", "visual_score_v2": score, "visual_accepted_count": 6,
        "duplicate_review_status": "none", "description": TEXT, "bedrooms": 1, "bathrooms": 1,
        "source_urls": [url], "latitude": 37.99, "longitude": 23.74,
    }
    row.update(over)
    return row


def test_yield_formula_uses_all_in_cost():
    listing = _row("a", price=55_000.0)
    estimate = estimate_yield(listing, YieldAssumptions())
    result = estimate_rent_yield(listing, estimate, FlatRentModel())
    asked = 12.0 * 40 * (1 + 0.05 * 2.0)
    signed = 1 - DEFAULT_COSTS.asking_to_achieved           # asking rent → signed rent
    assert result.rent_basis == "as_is"
    assert result.rent_hat == pytest.approx(asked * signed)
    assert result.rent_lo == pytest.approx(asked * 0.9 * signed)
    assert result.gross_yield == pytest.approx(12 * asked * 0.9 * signed / estimate.all_in_cost)  # same rent as net
    year = annual_costs(asked * 0.9 * signed, 40.0)
    assert result.net_yield_lo == pytest.approx(year["net"] / estimate.all_in_cost)
    assert result.net_yield_lo_before_tax == pytest.approx(year["before_tax"] / estimate.all_in_cost)
    assert result.costs["rent_asked_lo"] == pytest.approx(asked * 0.9)


def test_renovation_case_earns_post_renovation_rent():
    model = FlatRentModel()
    listing = _row("b", score=4.5)  # below the full-renovation threshold
    estimate = estimate_yield(listing, YieldAssumptions())
    assert estimate.renovation_level == "full"
    result = estimate_rent_yield(listing, estimate, model)
    assert result.rent_basis == "post_renovation"
    subject, override = model.calls[-1]
    assert override == 8.0 and "publisher_condition" not in subject


def test_medium_renovation_earns_rent_at_the_target_score():
    model = FlatRentModel()
    listing = _row("medium", score=7.0)
    estimate = estimate_yield(listing, YieldAssumptions())
    assert estimate.renovation_level == "medium"
    estimate_rent_yield(listing, estimate, model)
    subject, override = model.calls[-1]
    assert override == 8.0 and "publisher_condition" not in subject


def test_repaint_earns_rent_at_eight_without_the_renovated_label():
    model = FlatRentModel()
    listing = _row("paint", score=7.7)
    estimate = estimate_yield(listing, YieldAssumptions())
    assert estimate.renovation_level == "paint"
    assert estimate.renovation_budget == pytest.approx(40.0 * 40)
    result = estimate_rent_yield(listing, estimate, model)
    subject, override = model.calls[-1]
    assert result.rent_basis == "post_renovation"
    assert override == 8.0 and "publisher_condition" not in subject


def test_missing_coordinates_and_unranked_are_reported_not_guessed():
    model = FlatRentModel()
    listing = _row("c", latitude=None)
    estimate = estimate_yield(listing, YieldAssumptions())
    assert estimate_rent_yield(listing, estimate, model).unavailable_reason == "no_coordinates"
    bad = _row("d", price=None)
    assert estimate_rent_yield(bad, estimate_yield(bad, YieldAssumptions()), model).unavailable_reason == "unranked"
    assert estimate_rent_yield(listing, estimate, None).unavailable_reason == "no_rent_model"


def test_primary_rank_orders_by_conservative_net_yield_and_reaches_final_db(tmp_path):
    rows = [
        _row("cheap", price=40_000.0),
        _row("pricey", price=69_000.0),
        _row("pretty", price=52_000.0, score=9.5),
        _row("elsewhere", price=45_000.0, hood="Pagkrati"),
    ]
    ranked, unranked = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert not unranked
    kypseli = [d for d in ranked if d.neighborhood == "Kypseli"]
    assert [d.url for d in kypseli] == ["cheap", "pretty", "pricey"]  # output is in rank order
    assert [d.rank_in_neighborhood for d in kypseli] == [1, 2, 3]
    nets = [d.rent_yield.net_yield_lo for d in kypseli]
    assert nets == sorted(nets, reverse=True)
    assert {d.rank_in_neighborhood for d in ranked if d.neighborhood == "Pagkrati"} == {1}

    target = tmp_path / "final_deals.db"
    write_final_database_atomic(target, ranked, unranked, [], {"as_of": "2026-11-05"})
    conn = sqlite3.connect(target)
    top = conn.execute(
        "SELECT url, rank_in_neighborhood, net_yield_lo FROM deals WHERE neighborhood = 'Kypseli' "
        "ORDER BY rank_in_neighborhood"
    ).fetchall()
    basis = {r[0] for r in conn.execute("SELECT rent_basis FROM deals")}
    conn.close()
    assert [r[0] for r in top] == ["cheap", "pretty", "pricey"]
    assert all(r[2] > 0 for r in top) and basis == {"as_is"}


def test_without_a_rent_model_nothing_is_ranked():
    rows = [_row("x", price=40_000.0), _row("y", price=60_000.0, score=9.9)]
    ranked, unranked = rank_deals(rows, {}, YieldAssumptions())
    assert ranked == []
    assert {d.estimate.exclusion_reason for d in unranked} == {"rent_no_rent_model"}


def test_a_flat_the_model_cannot_price_is_unranked_not_guessed():
    rows = [_row("located", price=40_000.0), _row("nowhere", price=41_000.0, latitude=None)]
    ranked, unranked = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [d.url for d in ranked] == ["located"]
    assert [(d.url, d.estimate.exclusion_reason) for d in unranked] == [("nowhere", "rent_no_coordinates")]


def test_final_db_shows_score_and_cost_before_and_after_renovation(tmp_path):
    rows = [_row("worn", price=40_000.0, score=5.5), _row("fine", price=50_000.0, score=8.5)]
    ranked, unranked = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    target = tmp_path / "final_deals.db"
    write_final_database_atomic(target, ranked, unranked, [], {"as_of": "2026-11-08"})
    conn = sqlite3.connect(target)
    got = {r[0]: r[1:] for r in conn.execute(
        "SELECT url, visual_score, visual_score_after, cost_before_renovation, renovation_eur, all_in_cost "
        "FROM method_ranks")}
    conn.close()
    assert got["worn"] == pytest.approx((5.5, 8.0, 43_200.0, 20_000.0, 63_200.0))
    assert got["fine"] == pytest.approx((8.5, 8.5, 54_000.0, 0.0, 54_000.0))


def test_every_cost_and_the_tax_come_off_the_ranked_yield():
    """Decided 2026-11-16: management (a month's rent), ENFIA, accountant and the 15 % tax count;
    furnishing is left to whoever lets the flat, so it changes nothing."""
    class FurnishedPremiumModel(FlatRentModel):
        def raw_coefficients(self):
            return {"furnished": 0.13}

    year = annual_costs(400.0, 46.0)
    assert year["rent"] == 4_800 and year["vacancy"] == 200 and year["collected"] == 4_600
    assert year["management"] == 400 and year["maintenance"] == pytest.approx(240)
    assert year["enfia"] == pytest.approx(161) and year["accountant"] == 300 and year["insurance"] == 100
    assert year["income_tax"] == pytest.approx(0.15 * 0.95 * 4_600)
    assert year["net"] == pytest.approx(4_600 - 400 - 240 - 100 - 161 - 300 - 0.15 * 0.95 * 4_600)

    listing = _row("a", price=55_000.0)
    estimate = estimate_yield(listing, YieldAssumptions())
    plain = estimate_rent_yield(listing, estimate, FlatRentModel())
    premium = estimate_rent_yield(listing, estimate, FurnishedPremiumModel())
    assert plain.net_yield_lo < plain.net_yield_lo_before_tax < plain.gross_yield == 12 * plain.rent_lo / estimate.all_in_cost
    assert premium.net_yield_lo == plain.net_yield_lo and premium.rent_lo == plain.rent_lo
