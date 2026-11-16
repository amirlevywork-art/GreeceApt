import json

from greeceapt.rent import store
from greeceapt.rent.records import build_record

SEARCH_ROW = {
    "id": 21208834,
    "price": 600,
    "sq_meters": 55,
    "floorNumber": 2,
    "rooms": 2,
    "latitude": 37.955479,
    "longitude": 23.721819,
    "firstPublishDate": "2026-11-03 17:43:28",
    "modified": "2026-11-05 09:00:00",
    "priceReduced": False,
    "geography": "Center (Athens - Center)",
}
DETAIL = {
    "id": 21208834,
    "sq_meters": 55,
    "price": 600,
    "year_of_construction": 1980,
    "latitude": 37.955479,
    "longitude": 23.721819,
    "geocodeType": "offset",
    "geographiesByLevel": {
        "2": {"name": "Athens - Center"},
        "4": {"name": "Neos Kosmos"},
        "5": {"name": "Center"},
    },
    "floorNumber": 2,
    "rooms": 2,
    "no_of_bathrooms": 1,
    "description": "This apartment is built on the semi basement and sized about 55sqm. Fully furnished.",
    "energyClass": 6,
    "heatingController": 2,
    "heatingMedium": 1,
    "elevator": "0",
    "furnished": "yes",
    "renovated": "1",
    "renovationYear": 2023,
    "requiresRenovation": "0",
    "commonExpenses": 30,
    "images": [{"large": "https://m1.spitogatos.gr/a_1024x768.jpg"}, {"medium": "https://m1/b.jpg"}],
}


def test_build_record_decodes_floor_code_and_flags():
    record = build_record(SEARCH_ROW, DETAIL)
    assert record["rent_eur"] == 600.0
    assert record["area_sqm"] == 55.0
    assert record["floor_class"] == "semi_basement"
    assert record["floor"] == -0.5
    assert record["hood_l4"] == "Neos Kosmos"
    assert record["furnished"] == 1 and record["elevator"] == 0
    assert record["flag_renovated"] == 1 and record["flag_requires_renovation"] == 0
    assert record["renovation_year"] == 2023 and record["year_built"] == 1980
    assert record["energy_class"] == "D"
    assert record["first_publish_date"] == "2026-11-03"
    assert record["price_reduced"] == 0
    assert json.loads(record["photo_urls_json"])[0].endswith("1024x768.jpg")
    assert "images" not in json.loads(record["raw_json"])["detail"]


def test_store_upsert_keeps_vision_columns(tmp_path):
    conn = store.connect(tmp_path / "rentals.db")
    record = build_record(SEARCH_ROW, DETAIL)
    store.upsert_records(conn, [record])
    conn.execute("UPDATE listings SET in_vision_sample = 1")
    conn.commit()
    store.upsert_records(conn, [{**record, "rent_eur": 650.0}])
    row = store.all_rows(conn)
    assert len(row) == 1
    assert row[0]["rent_eur"] == 650.0
    assert row[0]["in_vision_sample"] == 1
