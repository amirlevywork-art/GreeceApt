"""Trap checks on every ranked flat."""

from __future__ import annotations

from greeceapt.scoring.ranking import rank_deals
from greeceapt.scoring.verification import (
    location_mismatch,
    not_habitable,
    verify_ranked,
)
from greeceapt.scoring.yield_model import YieldAssumptions
from tests.test_rent_yield import FlatRentModel

TEXT = (
    "Renovated one bedroom apartment close to the metro with new kitchen, new bathroom, "
    "security door and aluminium frames."
)
KNOWN = ["Ampelokipoi", "Gizi - Pedion Areos", "Agiou Meletiou - Victorias Square - Marni",
         "Exarchia - Neapoli", "Kipseli", "Patisia", "Kato Patisia"]


def _row(name, *, hood="Kipseli", price=50_000.0, area=40.0, description=TEXT, floor_class="upper", floor=2.0):
    return {
        "url": f"https://v/{name}", "opportunity_id": f"opp_{name}", "neighborhood": hood,
        "price_eur": price, "area_sqm": area, "floor": floor, "floor_class": floor_class,
        "visual_status_v2": "accepted", "visual_score_v2": 8.0, "description": description,
        "bedrooms": 1, "bathrooms": 1, "latitude": 37.99, "longitude": 23.74,
    }


# ── Individual checks (phrases from real November listings) ───────────────────

def test_location_mismatch_from_real_phrases():
    assert location_mismatch(
        "Discover the opportunity in downtown Athens, in Ampelokipi - Pentagon. 50sqm apartment.",
        "Gizi - Pedion Areos", KNOWN,
    ) == "Ampelokipoi"
    assert location_mismatch(
        "For sale is a bright semi-basement apartment of 47 sq.m. in Victoria Square.",
        "Exarchia - Neapoli", KNOWN,
    ) == "Agiou Meletiou - Victorias Square - Marni"


def test_location_mentions_that_are_not_mismatches():
    # Own neighborhood, a neighborhood sharing a name token, and a landmark-style mention.
    assert location_mismatch("Apartment in Kipseli near the park.", "Kipseli", KNOWN) is None
    assert location_mismatch("Bright flat in Patisia.", "Kato Patisia", KNOWN) is None
    assert location_mismatch("Close to Ampelokipoi metro station.", "Gizi - Pedion Areos", KNOWN) is None
    assert location_mismatch(None, "Kipseli", KNOWN) is None


def test_not_habitable_phrases():
    assert not_habitable("Γκαρσονιέρα σε ημιτελή κατάσταση, 2ου ορόφου.")
    assert not_habitable("It is necessary to install the kitchen and the bathroom tiles.")
    assert not_habitable("Studio without a kitchen.")
    assert not not_habitable("New kitchen and new bathroom.")


# ── Stage behavior ────────────────────────────────────────────────────────────

def _verified(rows):
    ranked, _ = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    return verify_ranked(ranked, {row["url"]: row for row in rows}, KNOWN)


def test_failing_flat_keeps_its_rank_and_is_flagged():
    rows = [
        _row("odd", price=30_000.0, description="Discover the opportunity, in Ampelokipi - Pentagon. " + TEXT),
        _row("b", price=50_000.0), _row("c", price=55_000.0),
    ]
    deals = {deal.url.rsplit("/", 1)[-1]: deal for deal in _verified(rows)}
    assert deals["odd"].rank_in_neighborhood == 1
    assert deals["odd"].needs_check and "location_mismatch" in deals["odd"].check_reasons[0]
    assert not deals["b"].needs_check and deals["b"].rank_in_neighborhood == 2


def test_price_outlier_needs_five_flats_in_the_neighborhood():
    rows = [_row("cheap", price=20_000.0)] + [_row(f"n{i}", price=60_000.0 + i) for i in range(4)]
    deals = {deal.url.rsplit("/", 1)[-1]: deal for deal in _verified(rows)}
    assert deals["cheap"].needs_check and deals["cheap"].check_reasons == ("price_outlier",)
    small = [_row("cheap", price=20_000.0), _row("x", price=60_000.0)]
    assert not any(deal.needs_check for deal in _verified(small))


def test_every_ranked_flat_is_checked_not_only_the_top():
    rows = [_row(f"f{i}", price=50_000.0 + 1_000 * i) for i in range(8)]
    rows[7]["description"] = "Unfinished flat. " + TEXT
    deals = _verified(rows)
    assert [deal.url for deal in deals if deal.needs_check] == ["https://v/f7"]


def test_spelling_variants_and_own_area_mentions_are_not_mismatches():
    known = KNOWN + ["Exarcheia", "Attiki", "Leoforos Liosion"]
    assert location_mismatch(
        "Apartment for sale in exarchia - neapoli exarcheia, 34 sq.m.", "Exarcheia", known
    ) is None
    assert location_mismatch(
        "Property code 1851372 - apartment for sale in attiki leof. liosion for € 70.000.",
        "Leoforos Liosion", known,
    ) is None
    assert location_mismatch(
        "Apartment for sale, floor: semi-basement, in zografou. The apartment is 37 sq.m.",
        "Kipseli", known + ["Zografou"],
    ) == "Zografou"


def test_a_neighbouring_area_name_is_not_a_mismatch_a_far_one_is():
    """Nov 2026: 36 of 37 location flags named an area within 1.5 km of the flat."""
    from greeceapt.scoring.verification import neighborhood_centers

    near = _row("near", description="Apartment for sale in Patisia, close to the metro. " + TEXT)
    far = _row("far", description="Apartment for sale in Goudi, close to the park. " + TEXT)
    for row in (near, far):
        row.update(latitude=38.0, longitude=23.735)                     # in Kipseli
    centers = neighborhood_centers(
        [("Kipseli", 38.0, 23.735)] * 5 + [("Patisia", 38.008, 23.735)] * 5 + [("Goudi", 37.985, 23.77)] * 5
    )
    ranked, _ = rank_deals([near, far], {}, YieldAssumptions(), rent_model=FlatRentModel())
    rows = {row["url"]: row for row in (near, far)}
    checked = {d.url.rsplit("/", 1)[-1]: d for d in verify_ranked(ranked, rows, KNOWN + ["Goudi"], centers)}
    assert not checked["near"].needs_check                               # Patisia is ~0.9 km away
    assert checked["far"].check_reasons == ("location_mismatch: description says Goudi",)   # ~3.4 km
    assert neighborhood_centers([("Kipseli", "", None)] * 9) == {}      # unlocated ads place nothing
