"""The rent a sale ad says the flat is let for now is its rent floor (phrases from Nov 2026 ads)."""

from __future__ import annotations

import pytest

from greeceapt.scoring.stated_rent import stated_rent


@pytest.mark.parametrize(
    ("text", "rent"),
    [
        ("The property is proposed as an investment. It is rented for €300 per month.", 300),
        ("The property is currently leased until 30/06/2028 with a monthly rent of €240.", 240),
        ("Currently leased at €500/month; the tenants can remain if the new owner wishes.", 500),
        ("💰 Return: • already rented • monthly rent: €450 📍 ready property", 450),
        ("Already rented up to 31st of Jan 2028 for €320,00 monthly with a 6,8% yield", 320),
        ("aluminum frames, next to glass, rented for 400€, security door", 400),
        ("investment property (return 8%, current rent €400), special offer", 400),
        ("Τιμή: €62.000. Μισθωμένο 350€. Be Greek", 350),
        ("χαμηλό κόστος διαβίωσης. Μισθωμένο 500 ευρώ", 500),
        ("with the possibility of installing an individual boiler. Rented 370 euro until the end of July.", 370),
        ("price 70.000€ (negotiable), rented ( 420€, return 7,20% ), code 656", 420),
        ("A tenant who pays the rent regularly, providing immediate rental income of €425 per month.", 425),
    ],
)
def test_a_current_lease_is_read(text, rent):
    assert stated_rent(text, 60_000.0) == rent


@pytest.mark.parametrize(
    "text",
    [
        "A good investment with an estimated rental income of around €300 per month.",
        "Ideal for investment, as it was previously rented at €300 per month.",
        "Potential rental income of approximately €300/month, with an estimated yield above 7%.",
        "The apartment is very good for investment since you can rent it 400euro/month",
        "The tenant is leaving so it sells faster; the monthly rent was €280.",
        "επενδύστε σε ένα διαμέρισμα που μπορεί να σας αποφέρει ένα ενοίκιο της τάξης των 400 ευρώ",
        "ιδανικό για επένδυση γραφείο ιατρείο είχε ενοίκιο 450 ευρώ",
        "Delivered renovated and furnished, with an expected rent of about €470 per month.",
        "Ideal for investment, with a monthly rental income of €400. Price negotiable.",   # not said to be let
        "Price €65.000, common charges €30 per month.",
        "",
        None,
    ],
)
def test_an_estimate_a_past_lease_or_no_lease_is_not(text):
    assert stated_rent(text, 60_000.0) is None


def test_a_rent_that_cannot_be_right_for_the_price_is_ignored():
    assert stated_rent("It is rented for €1,500 per month.", 40_000.0) is None    # 45 % a year
    assert stated_rent("It is rented for €50 per month.", 40_000.0) is None


def test_the_stated_rent_is_the_floor_of_the_conservative_rent():
    from greeceapt.scoring.rent_yield import estimate_rent_yield
    from greeceapt.scoring.yield_model import YieldAssumptions, estimate_yield
    from tests.test_rent_yield import FlatRentModel, _row

    plain = _row("plain", price=55_000.0)
    let = _row("let", price=55_000.0, description=plain["description"] + " It is currently rented for €600 per month.")
    low = _row("low", price=55_000.0, description=plain["description"] + " It is currently rented for €250 per month.")
    model = FlatRentModel()
    base = estimate_rent_yield(plain, estimate_yield(plain, YieldAssumptions()), model)
    up = estimate_rent_yield(let, estimate_yield(let, YieldAssumptions()), model)
    down = estimate_rent_yield(low, estimate_yield(low, YieldAssumptions()), model)
    assert up.rent_lo == 600 and up.rent_hat >= 600 and up.net_yield_lo > base.net_yield_lo
    assert up.costs["rent_stated"] == 600 and up.costs["rent_model_lo"] == pytest.approx(base.rent_lo)
    assert down.rent_lo == pytest.approx(base.rent_lo)      # a floor: a low lease never lowers it
    assert down.costs["rent_stated"] == 250 and "rent_stated" not in base.costs


def test_a_flat_let_for_at_least_the_models_rent_is_kept_with_its_tenant_not_renovated():
    """Decided 2026-11-16: keep the tenant, so the estimated renovation leaves the cost."""
    from greeceapt.scoring.ranking import rank_deals
    from greeceapt.scoring.yield_model import YieldAssumptions
    from tests.test_rent_yield import FlatRentModel, _row

    base = _row("x", price=55_000.0, score=7.0)              # a medium renovation by the photos
    let = _row("let", price=55_000.0, score=7.0, description=base["description"] + " It is currently rented for €600 per month.")
    low = _row("low", price=55_000.0, score=7.0, description=base["description"] + " It is currently rented for €200 per month.")
    ranked, _ = rank_deals([base, let, low], {}, YieldAssumptions(), rent_model=FlatRentModel())
    deals = {d.url: d for d in ranked}
    assert deals["x"].estimate.renovation_level == "medium" and deals["low"].estimate.renovation_level == "medium"
    kept = deals["let"]
    assert kept.estimate.renovation_level == "none" and kept.estimate.renovation_budget == 0
    assert kept.estimate.all_in_cost == pytest.approx(55_000.0 * 1.08)
    assert kept.rent_yield.rent_lo == 600 and kept.rent_yield.rent_basis == "as_is"
    assert "let_with_tenant" in kept.estimate.risk_flags and not any(f.startswith("renovation_") for f in kept.estimate.risk_flags)
    # beside it, the year if the tenant leaves and the flat is renovated and let again
    alt = kept.rent_yield.costs["if_renovated"]
    assert alt["renovation_level"] == "medium" and alt["renovation_eur"] == deals["x"].estimate.renovation_budget
    assert alt["all_in_cost"] == pytest.approx(deals["x"].estimate.all_in_cost)
    assert alt["rent_lo"] == pytest.approx(deals["x"].rent_yield.rent_lo)          # market rent, no lease floor
    assert alt["net_yield_lo"] == pytest.approx(deals["x"].rent_yield.net_yield_lo)
    assert "if_renovated" not in deals["low"].rent_yield.costs
