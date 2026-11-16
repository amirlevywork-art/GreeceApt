"""Flats with no tracked advertisement leave the ranking; the others rank on a live ad."""

from __future__ import annotations

import json

from greeceapt.scoring import availability
from greeceapt.scoring.availability import apply_availability
from greeceapt.scoring.ranking import rank_deals
from greeceapt.scoring.yield_model import YieldAssumptions
from tests.test_rent_yield import FlatRentModel

XE = "https://www.xe.gr/property/d/property-for-sale/900100/flat"
SPITO_A = "https://www.spitogatos.gr/en/property/1110001"
SPITO_B = "https://www.spitogatos.gr/en/property/1110002"
SPITO_C = "https://www.spitogatos.gr/en/property/1110003"


def _flat(url, *members, price=50_000.0, area=40.0, **over) -> dict:
    return {"url": url, "source_urls": list(members) or [url], "price_eur": price, "area_sqm": area, **over}


def _ad(url, price, area=40, **over) -> dict:
    return {"url": url, "price_eur": price, "area_sqm": area, **over}


def _tracked(*ads) -> dict:
    return {ad["url"]: ad for ad in ads}


def test_flat_advertised_only_on_xe_is_unavailable_while_xe_is_not_collected():
    flat = _flat(XE)
    counts = apply_availability([flat], tracked={}, xe_tracked=False)
    assert flat["unavailable_reason"] == "xe_only_untracked" and counts["xe_only_untracked"] == 1

    counts = apply_availability([flat], tracked={}, xe_tracked=True)
    assert "unavailable_reason" not in flat and counts["live"] == 1


def test_flat_whose_spitogatos_ads_are_all_gone_is_no_longer_listed():
    gone = _tracked(_ad(SPITO_A, 50_000, gone_since="2026-11-12T10:00:00+00:00"))
    alone, with_xe = _flat(SPITO_A), _flat(XE, XE, SPITO_A)
    counts = apply_availability([alone, with_xe], tracked=gone, xe_tracked=False)
    assert alone["unavailable_reason"] == with_xe["unavailable_reason"] == "no_longer_listed"
    assert counts["no_longer_listed"] == 2 and alone["price_eur"] == 50_000.0


def test_flat_also_on_xe_ranks_on_its_live_spitogatos_ad():
    flat = _flat(XE, XE, SPITO_A, price=45_000.0, area=40.0, price_pre_reduction=None)
    tracked = _tracked(_ad(SPITO_A, 48_000, 42, price_pre_reduction=50_000))
    counts = apply_availability([flat], tracked=tracked, xe_tracked=False)
    assert counts["rebased"] == 1 and "unavailable_reason" not in flat
    assert (flat["url"], flat["price_eur"], flat["area_sqm"]) == (SPITO_A, 48_000.0, 42.0)
    assert flat["price_pre_reduction"] == 50_000
    assert flat["source_urls"] == [XE, SPITO_A]                     # membership is not rewritten


def test_cheapest_live_ad_wins_over_a_gone_cheaper_one():
    flat = _flat(SPITO_A, SPITO_A, SPITO_B, SPITO_C, price=40_000.0)
    tracked = _tracked(
        _ad(SPITO_A, 40_000, gone_since="2026-11-12T10:00:00+00:00"),
        _ad(SPITO_B, 47_000, 41),
        _ad(SPITO_C, 46_000, 39),
    )
    apply_availability([flat], tracked=tracked, xe_tracked=False)
    assert (flat["url"], flat["price_eur"], flat["area_sqm"]) == (SPITO_C, 46_000.0, 39.0)


def test_flat_with_every_ad_live_is_left_as_advertised():
    flat = _flat(SPITO_A, SPITO_A, SPITO_B, price=44_000.0)
    tracked = _tracked(_ad(SPITO_A, 44_000), _ad(SPITO_B, 49_000))
    counts = apply_availability([flat], tracked=tracked, xe_tracked=False)
    assert counts == {"live": 1, "rebased": 0, "no_longer_listed": 0, "xe_only_untracked": 0}
    assert (flat["url"], flat["price_eur"]) == (SPITO_A, 44_000.0)


def test_ad_the_store_never_saw_is_not_judged(tmp_path):
    assert availability.load_tracked_ads(tmp_path / "missing.json") == {}
    flat = _flat(SPITO_A)
    apply_availability([flat], tracked={}, xe_tracked=False)
    assert "unavailable_reason" not in flat


def test_recorded_offer_prices_a_live_ad_that_is_not_in_the_store():
    other = "https://example.org/flat/7"
    flat = _flat(XE, XE, other, price=45_000.0, advertised_offers=[
        {"url": XE, "price_eur": 45_000.0, "area_sqm": 40.0},
        {"url": other, "price_eur": 52_000.0, "area_sqm": 43.0},
    ])
    apply_availability([flat], tracked={}, xe_tracked=False)
    assert (flat["url"], flat["price_eur"], flat["area_sqm"]) == (other, 52_000.0, 43.0)


def test_store_is_read_from_the_data_path_and_xe_follows_the_sources_setting(monkeypatch):
    from greeceapt.storage import paths

    paths.SPITOGATOS_LISTINGS_JSON.write_text(
        json.dumps([_ad(SPITO_A, 50_000, gone_since="2026-11-12T10:00:00+00:00")]), encoding="utf-8"
    )
    monkeypatch.setattr(availability, "xe_is_collected", lambda: False)   # the real setting
    gone, xe_only = _flat(SPITO_A), _flat(XE)
    apply_availability([gone, xe_only])
    assert gone["unavailable_reason"] == "no_longer_listed"
    assert xe_only["unavailable_reason"] == "xe_only_untracked"

    monkeypatch.setattr(availability, "xe_is_collected", lambda: True)    # older fixtures
    apply_availability([xe_only])
    assert "unavailable_reason" not in xe_only


def _scored(url, *, price, **over) -> dict:
    return {
        "url": url, "opportunity_id": f"opp_{url[-7:]}", "source_urls": [url], "neighborhood": "Ampelokipoi",
        "price_eur": price, "area_sqm": 46.0, "floor": 2.0, "floor_class": "upper", "floor_conflict": 0,
        "floor_conflict_adjacent": 0, "visual_status_v2": "accepted", "visual_score_v2": 9.0,
        "visual_accepted_count": 6, "duplicate_review_status": "none", "bedrooms": 1, "bathrooms": 1,
        "description": "Renovated one bedroom apartment close to the metro with new kitchen and bathroom.",
        "latitude": 37.99, "longitude": 23.76, **over,
    }


def test_unavailable_flat_is_unranked_with_its_reason():
    listed = _scored("https://www.spitogatos.gr/en/property/1120001", price=52_000.0)
    gone = _scored("https://www.spitogatos.gr/en/property/1120002", price=40_000.0,
                   unavailable_reason="no_longer_listed")
    ranked, unranked = rank_deals([listed, gone], {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [deal.url for deal in ranked] == [listed["url"]]
    assert [(deal.url, deal.estimate.exclusion_reason) for deal in unranked] == [(gone["url"], "no_longer_listed")]


def test_has_live_ad_follows_the_tracked_ads():
    gone = _tracked(_ad(SPITO_A, 50_000, gone_since="2026-11-12T10:00:00+00:00"))
    assert not availability.has_live_ad(_flat(XE), gone, xe_tracked=False)
    assert availability.has_live_ad(_flat(XE), gone, xe_tracked=True)
    assert not availability.has_live_ad(_flat(SPITO_A), gone, xe_tracked=False)
    assert availability.has_live_ad(_flat(XE, XE, SPITO_B), gone, xe_tracked=False)
    check = availability.live_ad_check(tracked=gone, xe_tracked=False)
    assert check(_flat(SPITO_B)) and not check(_flat(XE))
