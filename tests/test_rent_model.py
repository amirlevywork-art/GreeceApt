import math

import numpy as np
import pytest

from greeceapt.rent.features import coords_km, feature_row, knn_mean
from greeceapt.rent.model import RentModel, fit, select_and_fit

TRUE = {"log_area": 0.7, "floor_semi_basement": -0.15, "visual": 0.05, "furnished": 0.10}


def _synthetic(n=900, seed=3, noise=0.05):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        lat = 37.95 + rng.uniform(0, 0.06)
        lon = 23.70 + rng.uniform(0, 0.06)
        area = float(rng.uniform(22, 75))
        semi = rng.random() < 0.25
        visual = float(rng.uniform(4, 9)) if rng.random() < 0.6 else None
        furnished = rng.random() < 0.4
        # rent level rises toward the north-east corner: a smooth spatial field
        location = 0.8 * (lat - 37.95) / 0.06 + 0.4 * (lon - 23.70) / 0.06
        log_rent = (
            math.log(450)
            + TRUE["log_area"] * math.log(area / 40)
            + TRUE["floor_semi_basement"] * semi
            + TRUE["visual"] * ((visual or 6.5) - 6.5)
            + TRUE["furnished"] * furnished
            + 0.3 * location
            + rng.normal(0, noise)
        )
        rows.append({
            "id": i, "rent_eur": math.exp(log_rent), "area_sqm": area,
            "latitude": lat, "longitude": lon,
            "floor_class": "semi_basement" if semi else "upper", "floor": -0.5 if semi else 2.0,
            "year_built": 1975, "publisher_condition": None,
            "visual_status_v2": "accepted" if visual is not None else None,
            "visual_score_v2": visual, "furnished": 1 if furnished else 0,
            "hood_l4": f"h{int((lat - 37.95) / 0.015)}{int((lon - 23.70) / 0.015)}",
        })
    return rows


def test_knn_mean_leave_one_out_excludes_self():
    xy = coords_km(np.array([37.0, 37.0001, 37.0002, 38.0]), np.array([23.0, 23.0, 23.0, 23.0]))
    values = np.array([100.0, 1.0, 1.0, 50.0])
    loo = knn_mean(xy, values, xy, 2, exclude_self=True)
    assert loo[0] == pytest.approx(1.0)          # never its own 100
    assert knn_mean(xy, values, xy[:1], 1, exclude_self=False)[0] == pytest.approx(100.0)


def test_fit_recovers_known_effects():
    rows = _synthetic()
    model = fit(rows, alpha=0.3, k=15)
    raw = model.raw_coefficients()
    assert raw["log_area"] == pytest.approx(TRUE["log_area"], abs=0.05)
    assert raw["floor_semi_basement"] == pytest.approx(TRUE["floor_semi_basement"], abs=0.03)
    assert raw["visual"] == pytest.approx(TRUE["visual"], abs=0.01)
    assert raw["furnished"] == pytest.approx(TRUE["furnished"], abs=0.03)
    assert raw["location"] > 0.5                  # the spatial field is picked up


def test_prediction_zeroes_controls_and_round_trips_json():
    rows = _synthetic(400)
    model = fit(rows, alpha=1.0, k=10)
    listing = dict(rows[0], furnished=1)
    assert feature_row(listing, for_prediction=True)["furnished"] == 0.0
    clone = RentModel.from_json(model.to_json())
    assert clone.predict_rent(listing) == pytest.approx(model.predict_rent(listing))
    better = clone.predict_rent(listing, visual_override=9.0)[0]
    worse = clone.predict_rent(listing, visual_override=4.0)[0]
    assert better > worse


def test_select_and_fit_beats_baseline_and_bound_covers_90pct():
    rows = _synthetic(600, noise=0.08)
    model = select_and_fit(rows, alphas=(0.3, 3.0), ks=(10, 20))
    m = model.metrics
    assert m["random_cv"]["mape"] < m["baseline_hood_median_psqm"]["mape"]
    assert m["random_cv"]["rmse_log"] < m["no_location_term"]["rmse_log"]
    assert m["random_cv_lower_bound_coverage"] == pytest.approx(0.90, abs=0.02)
    central, low = model.predict_rent(rows[0])
    assert low < central
    names = [c["feature"] for c in model.coefficients]
    assert names[-1] == "location" and len(names) == len(model.features)


def test_pair_metadata_and_photo_rules():
    from greeceapt.rent.pairs import metadata_candidates, shared_photo_count
    from greeceapt.collect.fingerprints import PhotoFingerprint

    sale = {"latitude": 37.99, "longitude": 23.74, "area_sqm": 45.0, "floor_class": "upper", "floor": 2.0}
    rentals = [
        dict(sale, latitude=37.991),                   # ~110 m away, same flat → candidate
        dict(sale, area_sqm=52.0),                     # area off by 13 %
        dict(sale, floor=3.0),                         # different storey
        dict(sale, latitude=38.02),                    # 3 km away
    ]
    assert [(i, j) for i, j, _ in metadata_candidates([sale], rentals)] == [(0, 0)]

    def fp(full, center):
        return PhotoFingerprint("phash-v2", full, center)

    far_a, far_b = 0xFFFF_FFFF_0000_0000, 0x0000_0000_FFFF_FFFF   # 64 bits apart
    watermarked = 2**40 ^ 0b11_1111_1111                             # 10 bits off: near match
    a = [fp(0b1111, 0b1010), fp(2**40, 2**41), fp(far_a, far_a), fp(2**50, 2**51)]
    b = [fp(0b1110, 0b1010), fp(watermarked, 2**60), fp(far_b, far_b), fp(2**50 ^ 0x7FF, 2**51 ^ 0x7FF)]
    assert shared_photo_count(a, b) == 2          # the 11-bit pair is out; one-to-one
    assert shared_photo_count(a[:1], [b[0], b[0]]) == 1


def test_pairs_of_one_flat_count_once():
    from greeceapt.rent.pairs import Pair, group_flats

    def pair(sale, rent, price=70_000.0, rent_eur=550.0):
        return Pair(sale, rent, price, rent_eur, 35.0, 0.1, 4)

    # one flat: 2 sale ads × 2 rent ads (agency copies); a second flat on its own
    pairs = [pair("s1", "r1"), pair("s1", "r2", rent_eur=530.0), pair("s2", "r2", price=68_000.0, rent_eur=530.0),
             pair("s3", "r3", 50_000.0, 400.0)]
    flats = group_flats(pairs)
    assert [f.sale_urls for f in flats] == [("s1", "s2"), ("s3",)]
    assert flats[0].rent_urls == ("r1", "r2")
    assert flats[0].price_eur == 69_000.0 and flats[0].rent_eur == 540.0
    assert flats[1].gross_yield == pytest.approx(12 * 400 / 50_000)


def test_basement_discount_is_never_milder_than_the_semi_basement_one():
    """Few basements reach the rental market and those are the good ones (36 of 5,218 in
    Nov 2026, fitted -12.6% vs semi-basement -20.5%): the fit is capped at the semi-basement
    effect, and every other flat's prediction is unchanged."""
    rows = _synthetic()
    for row in rows[:12]:                      # a handful of basements that rent like upper floors
        if row["floor_class"] == "upper":
            row["floor_class"], row["floor"] = "basement", -1.0
            row["rent_eur"] *= 1.10
    model = fit(rows, alpha=0.3, k=15)
    raw = model.raw_coefficients()
    assert raw["floor_basement"] == pytest.approx(raw["floor_semi_basement"])
    assert "floor_basement capped at floor_semi_basement" in model.meta.get("constraints", [])

    unconstrained = fit(rows, alpha=0.3, k=15, monotone_floors=False)
    upper = next(row for row in rows if row["floor_class"] == "upper")
    assert model.predict_log([upper])[0] == pytest.approx(unconstrained.predict_log([upper])[0])


def test_basement_discount_stronger_than_semi_basement_is_kept():
    rows = _synthetic()
    for row in rows[:40]:
        if row["floor_class"] == "upper":
            row["floor_class"], row["floor"] = "basement", -1.0
            row["rent_eur"] *= 0.70
    model = fit(rows, alpha=0.3, k=15)
    raw = model.raw_coefficients()
    assert raw["floor_basement"] < raw["floor_semi_basement"]
    assert not model.meta.get("constraints")


def test_validate_counts_each_flat_once_and_applies_the_trained_bound():
    from greeceapt.rent.pairs import Pair, validate

    rows = [dict(r, url=f"r{r['id']}") for r in _synthetic(400)]
    sale = {k: v for k, v in rows[0].items() if k not in ("rent_eur", "url")}
    sale_b = dict(sale, url="s2")
    # one flat behind two sale ads and two rent ads (the second rent ad a copy of the first)
    pairs = [Pair("s1", "r0", 60_000.0, rows[0]["rent_eur"], sale["area_sqm"], 0.0, 4),
             Pair("s2", "r1", 60_000.0, rows[0]["rent_eur"], sale["area_sqm"], 0.0, 4)]
    pairs.append(Pair("s1", "r1", 60_000.0, rows[0]["rent_eur"], sale["area_sqm"], 0.0, 4))
    result = validate(pairs, {"s1": dict(sale, url="s1"), "s2": sale_b}, rows,
                      alpha=1.0, k=10, trained=RentModel(1.0, 10, [], [], [], [], 0.0, [], [],
                                                         lower_log_residual=-0.2))
    assert result["n"] == 1 and result["n_pairs"] == 3
    flat = result["flats"][0]
    assert flat["predicted_rent_lo"] == pytest.approx(flat["predicted_rent"] * math.exp(-0.2))
    assert flat["abs_pct_error"] < 0.15            # held out of training, still close


def test_conservative_bound_is_set_per_group_and_widened_where_rentals_are_sparse():
    from greeceapt.rent.model import bound_group

    rows = _synthetic(900)
    model = select_and_fit(rows, alphas=(1.0,), ks=(10,))
    # groups with enough rentals get their own 10th percentile, each ~90 % covered
    assert set(model.lower_by_group) == {"scored/above", "scored/below", "unscored/above", "unscored/below"} - {
        g for g, c in model.metrics["group_lower_bound_coverage"].items() if c["n"] < 100}
    for g, c in model.metrics["group_lower_bound_coverage"].items():
        if g in model.lower_by_group:
            assert c["coverage"] == pytest.approx(0.90, abs=0.02)

    upper = dict(rows[0], floor_class="upper", visual_status_v2="accepted", visual_score_v2=7.0)
    assert bound_group(upper) == "scored/above"
    assert bound_group(dict(upper, visual_status_v2=None)) == "unscored/above"
    assert bound_group(dict(upper, visual_status_v2=None), visual_override=8.0) == "scored/above"
    assert bound_group(dict(upper, floor_class="semi_basement")) == "scored/below"

    model.lower_by_group = {"scored/above": -0.15}
    model.sparse_lower_by_group = {"scored/above": -0.25}
    model.lower_log_residual = -0.2
    assert model.lower_bound(upper) == -0.15
    assert model.lower_bound(dict(upper, floor_class="semi_basement")) == -0.2   # group too small
    central, low = model.predict_rent(upper)
    assert low == pytest.approx(central * math.exp(-0.15))

    far = dict(upper, latitude=38.30, longitude=23.70)                          # ~35 km north
    assert not model.is_sparse(upper) and model.is_sparse(far)
    assert model.lower_bound(far) == -0.25


def test_model_json_without_group_bounds_uses_the_overall_bound():
    rows = _synthetic(300)
    model = fit(rows, alpha=1.0, k=10)
    payload = model.to_json()
    for key in ("lower_by_group", "sparse_lower_by_group", "sparse_km"):
        payload.pop(key)
    payload["lower_log_residual"] = -0.2
    old = RentModel.from_json(payload)
    central, low = old.predict_rent(rows[0])
    assert low == pytest.approx(central * math.exp(-0.2))
