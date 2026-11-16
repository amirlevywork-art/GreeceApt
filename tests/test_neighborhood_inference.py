"""Flats filed under a catch-all area name get the neighborhood of their nearest ads."""

from __future__ import annotations

from greeceapt.scoring.neighborhood_inference import K_NEAREST, infer_neighborhoods


def _grid(name, lat, lon, n):
    return [(name, lat + 0.0002 * i, lon) for i in range(n)]


def test_center_flat_takes_the_neighborhood_most_nearby_ads_are_in():
    reference = _grid("Neos Kosmos", 37.9590, 23.7280, 9) + _grid("Kinosargous", 37.9600, 23.7290, 6) \
        + _grid("Kipseli", 38.0000, 23.7400, 20)
    flat = {"url": "a", "neighborhood": "Center", "latitude": 37.9592, "longitude": 23.7281}
    named = {"url": "b", "neighborhood": "Kipseli", "latitude": 38.0, "longitude": 23.74}
    nowhere = {"url": "c", "neighborhood": "Center", "latitude": None, "longitude": None}
    assert infer_neighborhoods([flat, named, nowhere], reference) == 1
    assert flat["neighborhood"] == "Neos Kosmos" and flat["neighborhood_listed"] == "Center"
    assert f"9 of the {K_NEAREST} nearest ads are in Neos Kosmos" in flat["neighborhood_note"]
    assert named["neighborhood"] == "Kipseli" and "neighborhood_note" not in named
    assert nowhere["neighborhood"] == "Center"                       # no coordinates: kept


def test_sale_flats_with_a_specific_name_also_count_as_reference():
    others = [{"url": f"s{i}", "neighborhood": "Pagkrati", "latitude": 37.97 + 0.0001 * i, "longitude": 23.75}
              for i in range(K_NEAREST)]
    flat = {"url": "x", "neighborhood": "Center", "latitude": 37.9701, "longitude": 23.7501}
    assert infer_neighborhoods([flat, *others]) == 1
    assert flat["neighborhood"] == "Pagkrati"
