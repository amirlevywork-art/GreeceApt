"""Flats left out of the ranking for their time on the market or thin photo evidence."""

from __future__ import annotations

from datetime import date

from greeceapt.scoring.listing_filters import (
    MAX_DAYS_ON_MARKET,
    MIN_USABLE_PHOTOS,
    ON_MARKET_TOO_LONG,
    TOO_FEW_USABLE_PHOTOS,
    apply_listing_filters,
)
from greeceapt.scoring.ranking import rank_deals
from greeceapt.scoring.yield_model import YieldAssumptions
from tests.test_rent_yield import FlatRentModel

AS_OF = date(2026, 11, 13)


def _flat(**over) -> dict:
    return {"url": "https://www.spitogatos.gr/en/property/1", "publication_date": "2026-11-01",
            "visual_status_v2": "accepted", "visual_accepted_count": 6, **over}


def test_thresholds_are_four_months_and_three_usable_photos():
    assert (MAX_DAYS_ON_MARKET, MIN_USABLE_PHOTOS) == (180, 3)


def test_flat_renewed_recently_but_first_published_long_ago_is_too_long_on_the_market():
    """Agencies renew ads, which resets publication_date; the first publication counts."""
    flat = _flat(publication_date="2026-06-22", earliest_publication_date="2024-07-12")
    counts = apply_listing_filters([flat], as_of=AS_OF)
    assert flat["unavailable_reason"] == ON_MARKET_TOO_LONG and counts[ON_MARKET_TOO_LONG] == 1


def test_six_months_exactly_is_kept_one_day_more_is_not():
    kept = _flat(earliest_publication_date="2026-05-17")       # 180 days before AS_OF
    dropped = _flat(earliest_publication_date="2026-05-16")    # 181 days
    apply_listing_filters([kept, dropped], as_of=AS_OF)
    assert "unavailable_reason" not in kept
    assert dropped["unavailable_reason"] == ON_MARKET_TOO_LONG


def test_flat_judged_on_fewer_than_three_usable_photos_is_left_out():
    thin = _flat(visual_accepted_count=2)
    enough = _flat(visual_accepted_count=3)
    from_features = _flat(visual_accepted_count=None, layer_1_features_v2='{"accepted_count": 1}')
    counts = apply_listing_filters([thin, enough, from_features], as_of=AS_OF)
    assert thin["unavailable_reason"] == from_features["unavailable_reason"] == TOO_FEW_USABLE_PHOTOS
    assert "unavailable_reason" not in enough and counts[TOO_FEW_USABLE_PHOTOS] == 2


def test_flats_already_out_keep_their_reason_and_unknowns_are_not_judged():
    gone = _flat(unavailable_reason="no_longer_listed", earliest_publication_date="2024-01-01")
    no_date = _flat(publication_date=None)
    unscored = _flat(visual_status_v2="insufficient_evidence", visual_accepted_count=0)
    apply_listing_filters([gone, no_date, unscored], as_of=AS_OF)
    assert gone["unavailable_reason"] == "no_longer_listed"
    assert "unavailable_reason" not in no_date and "unavailable_reason" not in unscored


def test_filtered_flat_is_unranked_with_its_reason():
    base = {
        "neighborhood": "Ampelokipoi", "price_eur": 50_000.0, "area_sqm": 46.0, "floor": 2.0,
        "floor_class": "upper", "floor_conflict": 0, "floor_conflict_adjacent": 0,
        "visual_status_v2": "accepted", "visual_score_v2": 9.0, "visual_accepted_count": 6,
        "duplicate_review_status": "none", "bedrooms": 1, "bathrooms": 1,
        "description": "Renovated one bedroom apartment close to the metro with new kitchen and bathroom.",
        "latitude": 37.99, "longitude": 23.76,
    }
    fresh = {**base, "url": "https://www.spitogatos.gr/en/property/2", "opportunity_id": "opp_2",
             "source_urls": ["https://www.spitogatos.gr/en/property/2"], "earliest_publication_date": "2026-10-01"}
    old = {**base, "url": "https://www.spitogatos.gr/en/property/3", "opportunity_id": "opp_3",
           "source_urls": ["https://www.spitogatos.gr/en/property/3"], "earliest_publication_date": "2025-01-01"}
    apply_listing_filters([fresh, old], as_of=AS_OF)
    ranked, unranked = rank_deals([fresh, old], {}, YieldAssumptions(), rent_model=FlatRentModel())
    assert [deal.url for deal in ranked] == [fresh["url"]]
    assert [deal.estimate.exclusion_reason for deal in unranked] == [ON_MARKET_TOO_LONG]


def test_flat_smaller_than_any_rental_the_model_learned_from_is_left_out():
    from greeceapt.scoring.listing_filters import AREA_BELOW_RENT_DATA, MIN_AREA_SQM

    assert MIN_AREA_SQM == 20
    tiny, ok = _flat(area_sqm=12.0), _flat(area_sqm=20.0)
    counts = apply_listing_filters([tiny, ok], as_of=AS_OF)
    assert tiny["unavailable_reason"] == AREA_BELOW_RENT_DATA and counts[AREA_BELOW_RENT_DATA] == 1
    assert "unavailable_reason" not in ok


def test_flat_whose_photos_show_no_living_space_is_not_ranked():
    """Center #1 (Nov 2026): a store room whose photos the model read as bathroom, hallway and
    'other' only — no kitchen, living room or bedroom."""
    from greeceapt.scoring.listing_filters import NO_LIVING_SPACE_SEEN

    storage = _flat(layer_1_features_v2='{"room_scores": {"bathroom": 5.5, "hallway": 6.0, "other": 6.0}}')
    home = _flat(layer_1_features_v2={"room_scores": {"bathroom": 8.0, "kitchen": 8.0}})
    unknown = _flat()                                                   # no room data: not judged
    counts = apply_listing_filters([storage, home, unknown], as_of=AS_OF)
    assert storage["unavailable_reason"] == NO_LIVING_SPACE_SEEN and counts[NO_LIVING_SPACE_SEEN] == 1
    assert "unavailable_reason" not in home and "unavailable_reason" not in unknown
