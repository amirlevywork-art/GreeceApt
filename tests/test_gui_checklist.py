"""'Before you buy' pre-filled from the ad, the photo verdicts and the rentals nearby."""

from __future__ import annotations

import sqlite3

from greeceapt.gui.checklist import comparable_rentals, prefill, verdicts_of


def _rentals(tmp_path, rows):
    db = tmp_path / "rentals.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE listings (url TEXT, rent_eur REAL, area_sqm REAL, floor_class TEXT, "
                 "latitude REAL, longitude REAL, furnished INTEGER, last_seen_at TEXT)")
    conn.executemany("INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def _by_key(items):
    return {item["key"]: item for item in items}


def test_items_are_filled_from_the_ad_text(tmp_path):
    db = _rentals(tmp_path, [])
    deal = {"floor_class": "semi_basement", "area_sqm": 40.0, "rent_lo_eur": 380.0, "renovation_level": "none"}
    ad = {"description": "Bright semi-basement, legalized under 4495, energy class C, κοινόχρηστα 25€ το μήνα.",
          "energy_class": "C", "latitude": 37.99, "longitude": 23.74}
    items = _by_key(prefill(deal, ad, [], rentals_db=db))
    assert items["permit"]["status"] == "found" and "4495" in items["permit"]["note"]
    assert items["energy"]["status"] == "found" and "class C" in items["energy"]["note"]
    assert items["common"]["status"] == "found" and "€25" in items["common"]["note"]
    assert items["light"]["status"] == "found"
    assert items["rent"]["status"] == "unknown"                       # no rentals nearby


def test_unknowns_and_warnings(tmp_path):
    db = _rentals(tmp_path, [])
    deal = {"floor_class": "basement", "area_sqm": 30.0, "renovation_level": "full", "renovation_eur": 15000,
            "visual_score": 5.0, "risk_flags": ["floor_conflict_adjacent"]}
    ad = {"description": "Basement used as storage, ως αποθήκη, with some υγρασία.", "energy_class": "N/A",
          "latitude": 37.99, "longitude": 23.74}
    verdicts = [{"visible_damage": "major", "kitchen_age": "dated", "bathroom_age": "dated"}]
    items = _by_key(prefill(deal, ad, verdicts, rentals_db=db))
    assert items["permit"]["status"] == "warn"                        # auxiliary use on paper?
    assert items["energy"]["status"] == "unknown"
    assert items["common"]["status"] == "unknown"
    assert items["damp"]["status"] == "warn"
    assert items["renovation"]["status"] == "warn" and "kitchen dated" in items["renovation"]["note"]
    assert items["floor"]["status"] == "warn"
    assert "light" in items                                           # below ground: light and height asked


def test_rent_check_uses_similar_rentals_nearby(tmp_path):
    db = _rentals(tmp_path, [
        ("r1", 450, 42, "semi_basement", 37.9901, 23.7401, 0, None),   # ~15 m, similar
        ("r2", 500, 38, "basement", 37.9905, 23.7405, 1, None),        # ~60 m, similar
        ("r3", 900, 41, "upper", 37.9902, 23.7402, 0, None),           # above ground: not comparable
        ("r4", 420, 80, "semi_basement", 37.9903, 23.7403, 0, None),   # twice the size
        ("r5", 430, 40, "semi_basement", 38.0200, 23.7400, 0, None),   # 3 km away
    ])
    comps = comparable_rentals(37.99, 23.74, 40.0, "semi_basement", rentals_db=db)
    assert [c["url"] for c in comps] == ["r1", "r2"]
    items = _by_key(prefill({"floor_class": "semi_basement", "area_sqm": 40.0, "rent_lo_eur": 480.0},
                            {"latitude": 37.99, "longitude": 23.74}, [], rentals_db=db))
    assert items["rent"]["status"] == "warn" and "€475" in items["rent"]["note"]   # nearby asks less than assumed
    assert len(items["rent"]["comparables"]) == 2


def test_verdicts_come_from_accepted_photos_only():
    features = {"image_scores": [{"status": "accepted", "verdict": {"visible_damage": "none"}},
                                 {"status": "rejected", "verdict": {"visible_damage": "major"}}]}
    assert verdicts_of(features) == [{"visible_damage": "none"}]


def test_spitogatos_energy_codes_are_the_greek_letters_in_latin():
    """Nov 2026: code 8 ads say "F", code 9 ads say "G" (it had been read as N/A)."""
    from greeceapt.collect.spitogatos.parse import LEGACY_ENERGY_CLASS, decode_energy_class

    assert [decode_energy_class(c) for c in (1, 4, 7, 8, 9, 10, 11, None, "x")] == \
        ["A+", "B", "E", "F", "G", "Exempt", None, None, None]
    assert LEGACY_ENERGY_CLASS["Z"] == "F" and LEGACY_ENERGY_CLASS["N/A"] == "G"
