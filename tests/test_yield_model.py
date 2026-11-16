"""Cost and condition of a sale flat: exclusions, renovation, confidence and risk."""

from __future__ import annotations

import pytest

from greeceapt.scoring.condition_text import publisher_condition
from greeceapt.scoring.yield_model import (
    RENOVATION_TARGET_SCORE,
    YieldAssumptions,
    condition_factor,
    estimate_yield,
    floor_factor,
    renovation_cost,
)

LONG_TEXT = (
    "Renovated one bedroom apartment close to the metro with new kitchen, new bathroom, "
    "security door and aluminium frames."
)


def _listing(**over) -> dict:
    row = {
        "url": "https://www.spitogatos.gr/en/property/1121148326",
        "neighborhood": "Ampelokipoi",
        "price_eur": 50_000.0,
        "area_sqm": 46.0,
        "floor": -1.0,
        "floor_class": "basement",
        "floor_conflict": False,
        "floor_conflict_adjacent": False,
        "visual_status_v2": "accepted",
        "visual_score_v2": 9.0,
        "visual_accepted_count": 6,
        "duplicate_review_status": "none",
        "description": LONG_TEXT,
        "bedrooms": 1,
        "bathrooms": 1,
        "advertised_offers": [{"url": "x", "price_eur": 50_000.0, "area_sqm": 46.0}],
    }
    row.update(over)
    return row


# ── Factors ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("floor_class", "value", "factor"),
    [
        ("basement", -1.0, 0.70), ("semi_basement", -0.5, 0.80), ("ground", 0.0, 0.90),
        ("elevated_ground", 0.5, 0.90), ("upper", 1.0, 1.00), ("upper", 2.0, 1.00),
        ("upper", 3.0, 1.02), ("upper", 7.0, 1.02),
    ],
)
def test_floor_factors(floor_class, value, factor):
    assert floor_factor(floor_class, value) == factor


@pytest.mark.parametrize(
    ("score", "factor"),
    [(3.0, 0.75), (4.0, 0.85), (5.0, 0.90), (6.0, 0.95), (7.0, 0.975), (8.0, 1.0), (9.0, 1.0), (10.0, 1.0)],
)
def test_condition_factor_interpolates_between_anchors(score, factor):
    assert condition_factor(score) == pytest.approx(factor, abs=1e-12)


def test_condition_factor_is_monotonic_and_undefined_below_three():
    values = [condition_factor(3.0 + step / 100) for step in range(0, 701)]
    assert all(later >= earlier for earlier, later in zip(values, values[1:]))
    assert condition_factor(2.99) is None


# ── Exclusions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("over", "renovation", "reason"),
    [
        ({"price_eur": 0}, 0.0, "invalid_price"),
        ({"price_eur": None}, 0.0, "invalid_price"),
        ({"area_sqm": -5}, 0.0, "invalid_area"),
        ({}, -1.0, "negative_renovation_budget"),
        ({"neighborhood": None}, 0.0, "neighborhood_unknown"),
        ({"duplicate_review_status": "pending"}, 0.0, "duplicate_review_pending"),
        ({"floor_conflict": True, "floor_class": None, "floor": None}, 0.0, "floor_conflict"),
        ({"floor_class": None, "floor": None}, 0.0, "floor_unknown"),
        ({"visual_status_v2": "insufficient_evidence"}, 0.0, "condition_unaccepted"),
        ({"visual_score_v2": 2.9}, 0.0, "condition_below_minimum"),
    ],
)
def test_exclusions_give_no_value(over, renovation, reason):
    estimate = estimate_yield(_listing(**over), YieldAssumptions(), renovation_budget=renovation)
    assert estimate.exclusion_reason == reason
    assert estimate.all_in_cost is None
    assert estimate.confidence == "unranked"


# ── Arithmetic ────────────────────────────────────────────────────────────────

def test_reference_arithmetic():
    estimate = estimate_yield(_listing(), YieldAssumptions())
    assert estimate.all_in_cost == pytest.approx(54_000.0, rel=1e-12)
    assert estimate.renovation_level == "none"


@pytest.mark.parametrize(
    ("over", "level", "cost"),
    [
        ({"visual_score_v2": 9.0}, "none", 0.0),
        ({"visual_score_v2": 8.0}, "none", 0.0),   # rentable as is: no renovation is charged
        ({"visual_score_v2": 7.7}, "paint", 40.0 * 46),      # each level's rate applies in full
        ({"visual_score_v2": 7.0}, "medium", 250.0 * 46),    # mid-band; the cost is linear between
        ({"visual_score_v2": 6.2}, "full", 500.0 * 46),
        ({"visual_score_v2": 4.0}, "full", 500.0 * 46),
        ({"visual_score_v2": 7.4}, "paint", (130.0 * 46)),   # 7.0 → 7.7: €250 → €40 per m²
        ({"visual_score_v2": 6.6}, "medium", 375.0 * 46),    # 6.2 → 7.0: €500 → €250 per m²
        # the photos decide (design decision 2026-11-16): the ad's words stay a risk flag only
        ({"visual_score_v2": 9.0, "condition": "Needs renovation"}, "none", 0.0),
        ({"visual_score_v2": 9.0, "description": "Γκαρσονιέρα σε ημιτελή κατάσταση, 2ου ορόφου."}, "none", 0.0),
    ],
)
def test_renovation_levels(over, level, cost):
    estimate = estimate_yield(_listing(**over), YieldAssumptions())
    assert estimate.renovation_level == level
    assert estimate.renovation_budget == pytest.approx(cost)
    assert estimate.all_in_cost == pytest.approx(54_000.0 + cost)
    assert estimate.acquisition_cost == pytest.approx(54_000.0)  # cost before any renovation
    if level != "none":
        # renovated up to the target, which sets the rent (no double penalty)
        assert estimate.condition_score == RENOVATION_TARGET_SCORE == 8.0
        assert f"renovation_{level}" in estimate.risk_flags
    else:
        assert estimate.condition_score == over["visual_score_v2"]


def test_renovation_cost_has_no_step_at_the_level_thresholds():
    """Decided 2026-11-16: a flat at 7.38 cost €8,500 more than one at 7.42 (medium vs paint)."""
    assumptions = YieldAssumptions()
    for area in (12.0, 25.0, 46.0, 75.0):
        costs = [renovation_cost(3.0 + step / 100, area, assumptions) for step in range(0, 520)]
        assert all(later <= earlier for earlier, later in zip(costs, costs[1:]))   # better → cheaper
        assert max(earlier - later for earlier, later in zip(costs, costs[1:])) < 100 + 4.0 * area   # 0.01 apart
    assert renovation_cost(7.38, 40.0, assumptions) - renovation_cost(7.42, 40.0, assumptions) < 600


def test_supplied_budget_replaces_the_estimate():
    estimate = estimate_yield(_listing(visual_score_v2=4.0), YieldAssumptions(), renovation_budget=10_000.0)
    assert estimate.renovation_level == "supplied"
    assert estimate.all_in_cost == pytest.approx(64_000.0)
    assert estimate.condition_score == RENOVATION_TARGET_SCORE


def test_excluded_assumptions_are_explicit():
    assert estimate_yield(_listing(), YieldAssumptions()).excluded_assumptions == (
        "market_rent_level", "income_tax", "enfia", "financing", "legal_use",
    )


def test_assumptions_validation():
    with pytest.raises(ValueError):
        YieldAssumptions(acquisition_rate=-0.01)
    with pytest.raises(ValueError):
        YieldAssumptions(paint_eur_per_sqm=-1)


# ── Confidence and risk ───────────────────────────────────────────────────────

def test_full_evidence_is_high_confidence():
    estimate = estimate_yield(_listing(floor_class="upper", floor=2.0), YieldAssumptions())
    assert estimate.confidence == "high" and estimate.evidence_warnings == ()


def test_basement_and_needs_renovation_are_risks_not_confidence_warnings():
    estimate = estimate_yield(_listing(condition="Needs renovation"), YieldAssumptions())
    assert estimate.confidence == "high"
    assert {"basement", "needs_renovation"} <= set(estimate.risk_flags)


@pytest.mark.parametrize(
    ("over", "warning"),
    [
        ({"description": "Short."}, "sparse_description"),
        ({"bedrooms": 9}, "suspicious_room_counts"),
        ({"floor_conflict_adjacent": True}, "floor_conflict_adjacent"),
        ({"visual_accepted_count": 3}, "thin_visual_evidence"),
        ({"advertised_offers": [{"price_eur": 50_000.0}, {"price_eur": 51_500.0}]}, "source_price_spread"),
    ],
)
def test_single_warning_gives_medium_confidence(over, warning):
    estimate = estimate_yield(_listing(**over), YieldAssumptions())
    assert estimate.evidence_warnings == (warning,)
    assert estimate.confidence == "medium"


def test_two_warnings_give_low_confidence():
    assert estimate_yield(_listing(description="Short.", bedrooms=9), YieldAssumptions()).confidence == "low"


@pytest.mark.parametrize(
    ("bedrooms", "bathrooms", "area", "suspicious"),
    [(6, 1, 30.0, False), (7, 1, 30.0, True), (10, 1, 80.0, False), (11, 1, 80.0, True),
     (1, 4, 30.0, False), (1, 5, 30.0, True), (-1, 1, 30.0, True)],
)
def test_room_count_formula(bedrooms, bathrooms, area, suspicious):
    estimate = estimate_yield(_listing(bedrooms=bedrooms, bathrooms=bathrooms, area_sqm=area), YieldAssumptions())
    assert ("suspicious_room_counts" in estimate.risk_flags) is suspicious


def test_risk_flags_are_sorted():
    estimate = estimate_yield(_listing(description="Short.", condition="Needs renovation"), YieldAssumptions())
    assert list(estimate.risk_flags) == sorted(estimate.risk_flags)


# ── Publisher condition text ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ({"condition": "Needs renovation"}, "needs_renovation"),
        ({"condition": "Renovated"}, "renovated"),
        ({"description": "Apartment that needs renovation, great price."}, "needs_renovation"),
        ({"description": "Ιδανικό για επενδυτή, προς ανακαίνιση."}, "needs_renovation"),
        ({"description": "Χρήζει ανακαίνισης."}, "needs_renovation"),
        ({"description": "Fully renovated in 2023 with new floors."}, "renovated"),
        ({"description": "Πλήρως ανακαινισμένο διαμέρισμα."}, "renovated"),
        ({"description": "Renovation of the building facade was completed in 2020."}, None),
        ({"description": "Possibility of renovation with a bank loan."}, None),
        ({"description": "Bright flat near the metro."}, None),
        ({"description": "The fact that it does not require renovation is a plus."}, None),
        ({"description": "Δεν χρειάζεται ανακαίνιση."}, None),
        ({"description": "No heating | needs renovation | studio."}, "needs_renovation"),
    ],
)
def test_publisher_condition(listing, expected):
    assert publisher_condition(listing) == expected


def test_renovated_text_never_lowers_cost_or_raises_the_factor():
    estimate = estimate_yield(_listing(condition="Renovated", visual_score_v2=7.5), YieldAssumptions())
    assert estimate.renovation_level == "paint"  # the publisher's word does not cancel the repaint
    as_is = estimate_yield(_listing(condition="Renovated", visual_score_v2=8.0), YieldAssumptions())
    assert as_is.renovation_level == "none" and as_is.condition_score == 8.0


# ── Commercial premises listed as homes ───────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "For sale 2 identical 30sqm shops suitable for Golden Visa that can be converted into apartments.",
        "36 sq.m. Commercial Space with Loft Conversion Potential – Fokionos Negri, Kypseli",
        "A ground-floor commercial space of 46 sq.m. is offered for sale in a location with easy accessibility",
        "PLATEIA ATTIKHS, Store room,, Sale, 55 sq.m, year built 1965, 1 rooms, Semibasement",
        "Κύπρου και Δροσοπούλου 59, με αποθήκη 5,22 τ.μ. με κουζινάκι, πρώην κατάστημα, με μικρό wc",
        "This is a shop that has been converted into an independent residence.",
        "Athens, on Aristotelous Street, an apartment for sale from a shop, 32 sq.m. ground floor",
    ],
)
def test_commercial_premises_are_not_ranked(text):
    from greeceapt.scoring.condition_text import commercial_premises

    assert commercial_premises(text)
    estimate = estimate_yield(_listing(description=text + " " + LONG_TEXT), YieldAssumptions())
    assert estimate.exclusion_reason == "commercial_premises"


@pytest.mark.parametrize(
    "text",
    [
        "Located in a quiet neighborhood with easy access to public transport, shops, and essential services.",
        "close to all kinds of shops for daily needs",
        "Floor Number: Semi Basement,  For Commercial Use,  Facade,  Pets Allowed,  Kitchens: 1",
        "Renovated, For Students, Suitable for Commercial Use, Price: 50.000€.",
        "For more information please contact our office.",
        "Πιστοποιημένο Μεσιτικό Γραφείο -- Για ευκαιρίες σε παρόμοια ακίνητα",
        "Grocery stores and local markets are located within a 2–3 minute walk",
        "ιδανικό για ιδιοκατοίκηση, επένδυση, Airbnb ή επαγγελματική χρήση",
        "έχει κ αποθήκη στο υπόγειο",
    ],
)
def test_homes_near_shops_or_fit_for_business_use_stay_ranked(text):
    from greeceapt.scoring.condition_text import commercial_premises

    assert not commercial_premises(text)


@pytest.mark.parametrize(
    ("area", "score", "level", "cost"),
    [
        (12.0, 4.0, "full", 12_000.0),     # a bathroom and kitchenette cost the same in 12 m²
        (30.0, 4.0, "full", 15_000.0),     # above the minimum the per-m² rate applies
        (20.0, 7.0, "medium", 6_000.0),
        (30.0, 7.0, "medium", 7_500.0),
        (20.0, 7.7, "paint", 800.0),       # paint has no minimum
    ],
)
def test_small_flats_pay_a_minimum_for_kitchen_and_bathroom_work(area, score, level, cost):
    estimate = estimate_yield(_listing(area_sqm=area, visual_score_v2=score), YieldAssumptions())
    assert (estimate.renovation_level, estimate.renovation_budget) == (level, pytest.approx(cost))


def test_a_metro_line_under_construction_is_not_the_flat():
    """Plateia Kipselis #1 (Nov 2026): a fully renovated flat was charged a full renovation because
    its ad says the nearby Line 4 metro station "is currently under construction"."""
    from greeceapt.scoring.condition_text import publisher_condition
    from greeceapt.scoring.verification import not_habitable

    metro = ("A bright, fully renovated 30 sq.m. apartment in Kypseli. It lies 300 meters from the "
             "\"Kypseli Square\" metro station (line 4), which is currently under construction.")
    assert publisher_condition({"description": metro}) == "renovated"
    assert not not_habitable(metro)
    greek = "Ανακαινισμένο διαμέρισμα, κοντά στον σταθμό του μετρό που είναι υπό κατασκευή."
    assert publisher_condition({"description": greek}) == "renovated"
    shell = "Apartment of 40 sq.m., under construction, delivery in 2027."
    assert publisher_condition({"description": shell}) == "needs_renovation" and not_habitable(shell)


def test_needs_renovation_in_the_ad_is_a_risk_not_a_renovation():
    estimate = estimate_yield(_listing(visual_score_v2=8.0, condition="Needs renovation"), YieldAssumptions())
    assert estimate.renovation_level == "none" and "needs_renovation" in estimate.risk_flags
    poor = estimate_yield(_listing(visual_score_v2=5.0, condition="Needs renovation"), YieldAssumptions())
    assert poor.renovation_level == "full"                       # the photos agree: renovated


# ── A bad kitchen or bathroom in an otherwise fine flat ───────────────────────

def _rooms(**scores) -> str:
    import json
    return json.dumps({"room_scores": scores})


@pytest.mark.parametrize(
    ("score", "rooms", "level", "extra", "flags"),
    [
        (8.6, {"kitchen": 6.0, "bathroom": 9.0, "bedroom": 9.0}, "medium", 4_000.0, {"kitchen_work"}),
        (8.3, {"kitchen": 9.0, "bathroom": 6.4}, "medium", 4_500.0, {"bathroom_work"}),
        (7.7, {"kitchen": 5.5, "bathroom": 6.0}, "medium", 8_500.0, {"kitchen_work", "bathroom_work"}),
        (8.6, {"kitchen": 6.7, "bathroom": 9.0}, "none", 0.0, set()),          # 6.6 and up: no job
        (7.0, {"kitchen": 5.0}, "medium", None, set()),                         # medium already has it
        (8.6, {"bedroom": 9.0}, "none", 0.0, set()),                            # no kitchen photo
    ],
)
def test_a_bad_kitchen_or_bathroom_gets_its_own_job(score, rooms, level, extra, flags):
    from greeceapt.scoring.yield_model import renovation_cost

    estimate = estimate_yield(_listing(visual_score_v2=score, layer_1_features_v2=_rooms(**rooms)), YieldAssumptions())
    plain = estimate_yield(_listing(visual_score_v2=score), YieldAssumptions())
    assert estimate.renovation_level == level
    if extra is not None:
        assert estimate.renovation_budget == pytest.approx(renovation_cost(score, 46.0, YieldAssumptions()) + extra)
    else:
        assert estimate.renovation_budget == plain.renovation_budget
    assert flags == {f for f in estimate.risk_flags if f.endswith("_work")}
    if flags:     # the rest of the flat stays as good as it is: rent at its own score, not 8.0
        assert estimate.condition_score == max(score, RENOVATION_TARGET_SCORE)
