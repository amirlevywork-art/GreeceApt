"""Tests for greeceapt.storage (core + db_conductor + util)."""

import json
import sqlite3

import pytest

from greeceapt.storage import core as db_core
from greeceapt.storage import db_conductor
from greeceapt.storage.util import (
    normalize_listing_url,
    parse_photo_urls_json,
    resolve_neighborhood,
)
from greeceapt.vision.scoring import aggregate_listing, score_verdict
from greeceapt.vision.types import ImageVerdict


def test_normalize_listing_url_strips_query():
    url = "https://www.xe.gr/property/123?utm=1#frag"
    assert normalize_listing_url(url) == "https://www.xe.gr/property/123"


def test_normalize_listing_url_empty():
    assert normalize_listing_url(None) is None
    assert normalize_listing_url("") is None


def test_resolve_neighborhood_athens_prefers_area():
    assert resolve_neighborhood("Athens", "Pagkrati") == "Pagkrati"


def test_resolve_neighborhood_compound_nea_smyrni():
    assert resolve_neighborhood("Nea", "Smyrni") == "Nea Smyrni"


def test_parse_photo_urls_json():
    assert parse_photo_urls_json(None) == []
    assert parse_photo_urls_json("[]") == []
    assert parse_photo_urls_json('["https://a"]') == ["https://a"]
    assert parse_photo_urls_json("{") == []


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Route all pipeline DB paths to a temporary directory."""
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_core, "DB_PATH", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_conductor, "INGESTED_DB", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "UPDATED_DB", tmp_path / "updated_listings.db")
    monkeypatch.setattr(db_conductor, "FINAL_DB", tmp_path / "final_deals.db")
    yield tmp_path


def test_normalize_listing_json_roundtrip():
    raw = {
        "url": "https://example.com/l/1?q=1",
        "title": "Test",
        "price_eur": 100_000,
        "area_sqm": 50.0,
        "neighborhood": "Pagkrati",
        "photo_urls": ["https://img/a.jpg", "https://img/b.jpg"],
        "photos_count": 2,
        "source": "xe",
    }
    row = db_core.normalize_listing(raw)
    assert row["url"] == "https://example.com/l/1"
    assert "photo_urls" not in row
    urls = json.loads(row["photo_urls_json"])
    assert len(urls) == 2
    assert json.loads(row["raw_json"])["title"] == "Test"


def test_insert_listings_skips_missing_url(isolated_db):
    db_core.create_tables()
    db_core.insert_listings([{"url": None, "price_eur": 1}, {"url": "", "price_eur": 2}])
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_insert_listings_persists(isolated_db):
    db_core.insert_listings(
        [
            {
                "url": "https://xe.gr/x/1",
                "title": "A",
                "price_eur": 200_000,
                "area_sqm": 55,
                "neighborhood": "Pagkrati",
                "photos_count": 3,
                "photo_urls": ["a", "b", "c"],
                "scraped_at": "2026-01-01",
            }
        ]
    )
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        row = conn.execute("SELECT url, neighborhood FROM listings").fetchone()
    finally:
        conn.close()
    assert row[0] == "https://xe.gr/x/1"
    assert row[1] == "Pagkrati"


def test_insert_listings_price_history_on_change(isolated_db):
    base = {
        "url": "https://xe.gr/x/price",
        "title": "P",
        "price_eur": 100_000,
        "area_sqm": 40,
        "neighborhood": "Pagkrati",
        "photos_count": 0,
        "photo_urls": [],
        "scraped_at": "2026-01-01",
    }
    db_core.insert_listings([base])
    db_core.insert_listings([{**base, "price_eur": 105_000, "scraped_at": "2026-01-02"}])
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        n_hist = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        row = conn.execute("SELECT price_eur FROM listings WHERE url = ?", (base["url"],)).fetchone()
    finally:
        conn.close()
    assert n_hist == 1
    assert row[0] == 105_000


def test_price_history_records_drop_and_rise(isolated_db):
    """price_history must log BOTH directions with correct old/new (it is a history, not a deal filter)."""
    base = {
        "url": "https://spitogatos.gr/en/property/1199",
        "title": "H",
        "price_eur": 70_000,
        "area_sqm": 40,
        "neighborhood": "Pagkrati",
        "photos_count": 0,
        "photo_urls": [],
        "scraped_at": "2026-01-01",
        "source": "spitogatos",
    }
    db_core.insert_listings([base])
    db_core.insert_listings([{**base, "price_eur": 65_000, "scraped_at": "2026-01-02"}])  # drop
    db_core.insert_listings([{**base, "price_eur": 68_000, "scraped_at": "2026-01-03"}])  # rise
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        events = conn.execute(
            "SELECT old_price_eur, new_price_eur FROM price_history "
            "JOIN listings ON listings.id = price_history.listing_id "
            "WHERE listings.url = ? ORDER BY recorded_at",
            (base["url"],),
        ).fetchall()
        current = conn.execute(
            "SELECT price_eur FROM listings WHERE url = ?", (base["url"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert events == [(70_000, 65_000), (65_000, 68_000)]
    assert current == 68_000


def _seed_one(url="https://xe.gr/x/seed", **over):
    row = {
        "url": url,
        "title": "S",
        "price_eur": 150_000,
        "area_sqm": 40,
        "neighborhood": "Pagkrati",
        "photos_count": 1,
        "photo_urls": ["x"],
        "scraped_at": "2026-01-02",
    }
    row.update(over)
    return row


def test_copy_ingested_preserves_column_types(isolated_db):
    """Numeric columns must stay numeric — an all-TEXT copy forces CAST on every consumer."""
    db_core.insert_listings([_seed_one()])
    db_conductor.copy_ingested_to_updated()
    conn = sqlite3.connect(db_conductor.UPDATED_DB)
    try:
        declared = {r[1]: r[2].upper() for r in conn.execute("PRAGMA table_info(listings)")}
        stored = conn.execute(
            "SELECT typeof(price_eur), typeof(area_sqm), typeof(url) FROM listings"
        ).fetchone()
    finally:
        conn.close()
    assert declared["price_eur"] == "REAL"
    assert declared["area_sqm"] == "REAL"
    assert declared["id"] == "INTEGER"
    assert declared["url"] == "TEXT"
    assert stored == ("real", "real", "text")


def test_move_listings_to_removed_batch(isolated_db):
    db_core.insert_listings([_seed_one(f"https://xe.gr/x/b{i}") for i in range(5)])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        db_conductor.setup_layer0_tables(conn)
        ids = [r[0] for r in conn.execute("SELECT id FROM listings").fetchall()]
        n = db_conductor.move_listings_to_removed(
            conn, [(ids[0], "reason A"), (ids[1], "reason B")], "Layer 0"
        )
        assert n == 2
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
        reasons = {
            r[0] for r in conn.execute("SELECT removal_reason FROM removed_listings").fetchall()
        }
        assert reasons == {"reason A", "reason B"}
        origins = {
            r[0] for r in conn.execute("SELECT layer_origin FROM removed_listings").fetchall()
        }
        assert origins == {"Layer 0"}
    finally:
        conn.close()


def test_move_listings_to_removed_ignores_unknown_ids(isolated_db):
    db_core.insert_listings([_seed_one("https://xe.gr/x/only")])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        db_conductor.setup_layer0_tables(conn)
        assert db_conductor.move_listings_to_removed(conn, [(99999, "nope")], "Layer 0") == 0
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    finally:
        conn.close()


def test_copy_ingested_to_updated(isolated_db):
    db_core.insert_listings(
        [
            {
                "url": "https://xe.gr/x/2",
                "title": "B",
                "price_eur": 150_000,
                "area_sqm": 40,
                "neighborhood": "Pagkrati",
                "photos_count": 1,
                "photo_urls": ["x"],
                "scraped_at": "2026-01-02",
            }
        ]
    )
    n = db_conductor.copy_ingested_to_updated()
    assert n == 1
    conn = sqlite3.connect(db_conductor.UPDATED_DB)
    try:
        assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    finally:
        conn.close()


def _listing_vision_result(*, confidence=0.9, unknown=False):
    verdict = ImageVerdict(
        room_type="living",
        is_interior=True,
        visible_damage="unclear" if unknown else "none",
        wall_condition="unclear" if unknown else "fresh",
        flooring_condition="unclear" if unknown else "modern",
        kitchen_age="not_applicable",
        bathroom_age="not_applicable",
        windows_age="unclear" if unknown else "modern",
        renovation_signals="unclear" if unknown else "clear",
        image_quality="good",
        confidence=confidence,
    )
    return aggregate_listing([score_verdict(verdict)])


def test_existing_database_gains_v2_columns_without_losing_v1_data(isolated_db):
    conn = sqlite3.connect(db_core.DB_PATH)
    conn.execute(
        "CREATE TABLE listings (id INTEGER PRIMARY KEY, url TEXT, visual_score REAL, layer_1_features TEXT)"
    )
    conn.execute("INSERT INTO listings VALUES (1, 'https://x', 6.5, '{\"v1\": true}')")
    conn.commit()
    conn.close()

    db_core.create_tables()

    conn = sqlite3.connect(db_core.DB_PATH)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    row = conn.execute(
        "SELECT visual_score, layer_1_features FROM listings WHERE id = 1"
    ).fetchone()
    conn.close()
    assert {
        "visual_score_v2",
        "visual_confidence_v2",
        "visual_status_v2",
        "layer_1_features_v2",
        "vision_schema_version",
        "vision_model_version",
        "vision_prompt_version",
        "vision_filter_version",
        "vision_filter_mode",
        "visual_score_v1_backup",
    } <= columns
    assert row == (6.5, '{"v1": true}')


def test_shadow_v2_persistence_leaves_v1_score_and_round_trips_features():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY, visual_score REAL)")
    conn.execute("INSERT INTO listings VALUES (1, 4.2)")
    db_conductor.setup_layer0_tables(conn)
    result = _listing_vision_result()

    db_conductor.save_visual_audit_v2_results(
        conn, 1, result, promote=False
    )
    row = conn.execute(
        "SELECT visual_score, visual_score_v2, visual_confidence_v2, visual_status_v2, "
        "layer_1_features_v2, vision_schema_version, vision_model_version, "
        "vision_prompt_version FROM listings"
    ).fetchone()
    features = json.loads(row[4])

    assert row[0] == 4.2
    assert row[1] == result.condition_score
    assert row[2] == result.effective_confidence
    assert row[3] == "accepted"
    assert row[5] == "condition-v2"
    assert row[6] == "qwen3-vl:4b-instruct"
    assert row[7] == "condition-v2-p3"
    assert features["schema_version"] == "condition-v2"
    assert features["image_scores"][0]["verdict"]["room_type"] == "living"
    conn.close()


def test_promoted_v2_copies_only_accepted_score_and_keeps_unknown_null():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY, visual_score REAL)")
    conn.executemany("INSERT INTO listings VALUES (?, ?)", [(1, 4.0), (2, 4.0)])
    db_conductor.setup_layer0_tables(conn)

    accepted = _listing_vision_result()
    unknown = _listing_vision_result(confidence=0.2, unknown=True)
    db_conductor.save_visual_audit_v2_results(conn, 1, accepted, promote=True)
    db_conductor.save_visual_audit_v2_results(conn, 2, unknown, promote=True)

    rows = conn.execute(
        "SELECT id, visual_score, visual_score_v2, visual_status_v2 FROM listings ORDER BY id"
    ).fetchall()
    assert rows[0] == (1, accepted.condition_score, accepted.condition_score, "accepted")
    assert rows[1] == (2, None, None, "insufficient_evidence")
    conn.close()


def test_v2_work_selection_retries_unknown_and_refreshes_contract_changes():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE listings (id INTEGER PRIMARY KEY, photo_urls_json TEXT)"
    )
    db_conductor.setup_layer0_tables(conn)
    photos = '["img://one"]'
    conn.executemany(
        "INSERT INTO listings (id, photo_urls_json, visual_status_v2, "
        "vision_schema_version, vision_model_version, vision_prompt_version, "
        "vision_filter_version, vision_filter_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, photos, None, None, None, None, None, None),
            (2, photos, "accepted", "condition-v2", "qwen3-vl:4b-instruct", "condition-v2-p3", None, "off"),
            (3, photos, "model_error", "condition-v2", "qwen3-vl:4b-instruct", "condition-v2-p3", None, "off"),
            (4, photos, "accepted", "condition-v2", "qwen3-vl:4b-instruct", "condition-v2-p2", None, "off"),
        ],
    )

    work = db_conductor.get_listings_for_layer1_v2(conn)

    assert [listing_id for listing_id, _ in work] == ["1", "3", "4"]
    conn.close()


def test_v2_work_selection_rescores_rows_from_a_filter_contract():
    """The image prefilter is gone; a row scored with it on (never in production) is redone."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY, photo_urls_json TEXT)")
    db_conductor.setup_layer0_tables(conn)
    contract = ("accepted", "condition-v2", "qwen3-vl:4b-instruct", "condition-v2-p3")
    conn.executemany(
        "INSERT INTO listings (id, photo_urls_json, visual_status_v2, "
        "vision_schema_version, vision_model_version, vision_prompt_version, "
        "vision_filter_version, vision_filter_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, '["img://one"]', *contract, None, "off"),
            (2, '["img://two"]', *contract, "mobileclip-v1", "shadow"),
            (3, '["img://three"]', *contract, "mobileclip-v1", "active"),
            (4, '["img://four"]', *contract, "stale-filter", "active"),
        ],
    )

    work = db_conductor.get_listings_for_layer1_v2(conn)

    assert [listing_id for listing_id, _ in work] == ["2", "3", "4"]
    conn.close()


def test_off_persistence_uses_null_filter_version():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY, visual_score REAL)")
    conn.execute("INSERT INTO listings VALUES (1, 4.2)")
    db_conductor.setup_layer0_tables(conn)

    db_conductor.save_visual_audit_v2_results(
        conn, 1, _listing_vision_result(), promote=False
    )

    assert conn.execute(
        "SELECT vision_filter_version, vision_filter_mode FROM listings WHERE id = 1"
    ).fetchone() == (None, "off")
    conn.close()


def test_promoting_current_shadow_results_preserves_v1_backup():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE listings (id INTEGER PRIMARY KEY, visual_score REAL)"
    )
    conn.execute("INSERT INTO listings VALUES (1, 4.2)")
    db_conductor.setup_layer0_tables(conn)
    db_conductor.save_visual_audit_v2_results(
        conn, 1, _listing_vision_result(), promote=False
    )

    promoted = db_conductor.promote_current_v2_results(conn)
    row = conn.execute(
        "SELECT visual_score, visual_score_v1_backup FROM listings WHERE id = 1"
    ).fetchone()

    assert promoted == 1
    assert row == (_listing_vision_result().condition_score, 4.2)
    conn.close()


def test_floor_resolution_fields_round_trip_through_ingest_and_copy(isolated_db):
    row = _seed_one("https://xe.gr/x/floor")
    row.update(
        floor=-0.5,
        floor_class="semi_basement",
        floor_conflict=False,
        floor_conflict_adjacent=False,
        floor_evidence=[{"source": "structured", "raw_value": "Semi-basement",
                         "parsed_value": -0.5, "floor_classes": ["semi_basement"]}],
    )
    db_core.insert_listings([row])
    db_conductor.copy_ingested_to_updated()
    conn = sqlite3.connect(db_conductor.UPDATED_DB)
    try:
        floor, floor_class, conflict, adjacent, evidence = conn.execute(
            "SELECT floor, floor_class, floor_conflict, floor_conflict_adjacent, "
            "floor_evidence_json FROM listings"
        ).fetchone()
    finally:
        conn.close()
    assert floor == -0.5  # fractional floors survive INTEGER affinity
    assert (floor_class, conflict, adjacent) == ("semi_basement", 0, 0)
    assert json.loads(evidence)[0]["raw_value"] == "Semi-basement"


def test_existing_database_gains_floor_resolution_columns(isolated_db):
    conn = sqlite3.connect(db_core.DB_PATH)
    conn.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, floor INTEGER)")
    conn.commit()
    conn.close()
    db_core.create_tables()
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    finally:
        conn.close()
    assert {"floor_class", "floor_conflict", "floor_conflict_adjacent", "floor_evidence_json"} <= columns


def test_opportunity_fields_round_trip_through_ingest(isolated_db):
    row = _seed_one("https://xe.gr/x/opp")
    row.update(
        opportunity_id="opp_0123456789abcdef",
        source_urls=["https://xe.gr/x/opp", "https://spitogatos.gr/p/1"],
        advertised_offers=[{"url": "https://xe.gr/x/opp", "price_eur": 150000, "area_sqm": 40}],
        duplicate_review_status="pending",
    )
    db_core.insert_listings([row])
    conn = sqlite3.connect(db_core.DB_PATH)
    try:
        stored = conn.execute(
            "SELECT opportunity_id, source_urls_json, advertised_offers_json, duplicate_review_status "
            "FROM listings"
        ).fetchone()
    finally:
        conn.close()
    assert stored[0] == "opp_0123456789abcdef"
    assert json.loads(stored[1]) == row["source_urls"]
    assert json.loads(stored[2])[0]["price_eur"] == 150000
    assert stored[3] == "pending"


# ── Carry Layer 1 results across runs (speed plan B6) ─────────────────────────

def _score_all(conn, status="accepted", score=8.0, model="qwen3-vl:4b-instruct"):
    db_conductor._ensure_layer1_columns(conn)
    conn.execute(
        "UPDATE listings SET visual_status_v2 = ?, visual_score_v2 = ?, vision_model_version = ?, "
        "layer_1_features_v2 = '{\"accepted_count\": 5}'",
        (status, score, model),
    )
    conn.commit()


def test_unchanged_listing_keeps_its_layer1_result(isolated_db):
    db_core.insert_listings([_seed_one("https://xe.gr/x/keep")])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    _score_all(conn)
    conn.close()

    carried = db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        row = conn.execute("SELECT visual_status_v2, visual_score_v2, layer_1_features_v2 FROM listings").fetchone()
    finally:
        conn.close()
    assert carried == 1
    assert row == ("accepted", 8.0, '{"accepted_count": 5}')
    assert not db_conductor.UPDATED_DB.with_name(db_conductor.UPDATED_DB.name + ".previous").exists()


def test_changed_photo_set_is_rescored(isolated_db):
    db_core.insert_listings([_seed_one("https://xe.gr/x/photos")])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    _score_all(conn)
    conn.close()
    changed = _seed_one("https://xe.gr/x/photos")
    changed["photo_urls"] = ["x", "y"]
    db_core.insert_listings([changed])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        assert conn.execute("SELECT visual_status_v2 FROM listings").fetchone()[0] is None
    finally:
        conn.close()


def test_model_change_still_selects_carried_rows_for_rescoring(isolated_db):
    db_core.insert_listings([_seed_one("https://xe.gr/x/model")])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    _score_all(conn, model="old-model")
    conn.close()
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        work = db_conductor.get_listings_for_layer1_v2(conn, model_version="qwen3-vl:4b-instruct")
    finally:
        conn.close()
    assert len(work) == 1


def test_corrupt_previous_database_falls_back_to_plain_copy(isolated_db, caplog):
    db_core.insert_listings([_seed_one("https://xe.gr/x/corrupt")])
    db_conductor.UPDATED_DB.write_bytes(b"not a sqlite database")
    caplog.set_level("WARNING")
    assert db_conductor.copy_ingested_to_updated() == 1
    assert "carry-over skipped" in caplog.text


def test_results_scored_before_the_prefilter_existed_are_carried_as_filter_off(isolated_db, caplog):
    """Databases scored before 2026-11-04 have no filter columns; their results are filter-off."""
    db_core.insert_listings([_seed_one("https://xe.gr/x/old")])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        _score_all(conn)
        conn.execute(
            "UPDATE listings SET vision_schema_version = ?, vision_prompt_version = ?",
            (db_conductor.SCHEMA_VERSION, db_conductor.PROMPT_VERSION),
        )
        conn.execute("ALTER TABLE listings DROP COLUMN vision_filter_version")
        conn.execute("ALTER TABLE listings DROP COLUMN vision_filter_mode")
        conn.commit()
    finally:
        conn.close()

    caplog.set_level("INFO")
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        row = conn.execute(
            "SELECT visual_status_v2, vision_filter_version, vision_filter_mode FROM listings"
        ).fetchone()
        work = db_conductor.get_listings_for_layer1_v2(conn, model_version="qwen3-vl:4b-instruct")
    finally:
        conn.close()
    assert row == ("accepted", None, "off")
    assert work == []
    assert "1 of 1 listings keep their previous result" in caplog.text


def test_previous_table_without_layer1_columns_is_reported(isolated_db, caplog):
    db_core.insert_listings([_seed_one("https://xe.gr/x/bare")])
    db_conductor.copy_ingested_to_updated()
    conn = db_conductor.connect_updated()
    try:
        _score_all(conn)
        conn.execute("ALTER TABLE listings DROP COLUMN visual_confidence_v2")
        conn.commit()
    finally:
        conn.close()
    caplog.set_level("WARNING")
    db_conductor.copy_ingested_to_updated()
    assert "carry-over skipped" in caplog.text and "listings" in caplog.text
