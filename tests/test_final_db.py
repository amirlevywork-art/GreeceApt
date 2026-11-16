"""Explainable final schema and locked, atomic, fail-closed publishing."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import date

import pytest

from greeceapt.storage import final_db
from greeceapt.storage.final_db import (
    FinalDatabaseBusyError,
    FinalDatabaseSidecarError,
    write_final_database_atomic,
)
from greeceapt.scoring.ranking import rank_deals
from greeceapt.scoring.yield_model import YieldAssumptions
from tests.test_rent_yield import FlatRentModel

TEXT = (
    "Renovated one bedroom apartment close to the metro with new kitchen, new bathroom, "
    "security door and aluminium frames."
)


def _row(url, **over):
    row = {
        "url": url, "opportunity_id": f"opp_{url[-1]}", "neighborhood": "Ampelokipoi",
        "price_eur": 50_000.0, "area_sqm": 46.0, "floor": -1.0, "floor_class": "basement",
        "floor_evidence": [{"source": "description", "raw_value": "floor: basement",
                            "parsed_value": -1.0, "floor_classes": ["basement"]}],
        "visual_status_v2": "accepted", "visual_score_v2": 9.0, "description": TEXT,
        "bedrooms": 1, "bathrooms": 1, "source_urls": [url, url + "-b"],
        "latitude": 37.99, "longitude": 23.76,
    }
    row.update(over)
    return row


@pytest.fixture
def results():
    rows = [_row("https://d/1"), _row("https://d/2", floor_class=None, floor=None)]
    ranked, unranked = rank_deals(rows, {"Ampelokipoi": 1883.3}, YieldAssumptions(), rent_model=FlatRentModel())
    metadata = {"as_of": "2026-11-02", "assumptions": YieldAssumptions().to_json()}
    hoods = [("Ampelokipoi", 2, 1, 1883.3, "Tier_2_Growth_Hub")]
    return ranked, unranked, hoods, metadata


def _publish(target, results):
    ranked, unranked, hoods, metadata = results
    write_final_database_atomic(target, ranked, unranked, hoods, metadata)


def test_schema_persists_every_explanation_field(tmp_path, results):
    target = tmp_path / "final_deals.db"
    _publish(target, results)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        deal = conn.execute("SELECT * FROM deals").fetchone()
        views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
        unranked = conn.execute("SELECT * FROM unranked_deals").fetchone()
        meta = dict(conn.execute("SELECT key, value_json FROM ranking_metadata").fetchall())
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert (deal["neighborhood"], deal["rank_in_neighborhood"]) == ("Ampelokipoi", 1)
    assert deal["opportunity_id"] == "opp_1"
    assert json.loads(deal["source_urls_json"]) == ["https://d/1", "https://d/1-b"]
    assert deal["floor_class"] == "basement" and json.loads(deal["floor_evidence_json"])[0]["source"] == "description"
    assert deal["all_in_cost"] == pytest.approx(54_000.0) and deal["net_yield_lo"] > 0
    assert deal["renovation_level"] == "none" and deal["renovation_eur"] == 0.0
    gone = {"relative_yield", "rent_units", "floor_factor", "condition_factor", "in_top3", "display_rank",
            "checklist_json", "worst_rank_in_neighborhood", "stable_top3", "hand_rank_in_neighborhood"}
    assert not gone & set(deal.keys())
    assert "market_rent_level" in json.loads(deal["excluded_assumptions_json"])
    assert deal["confidence"] == "high" and "basement" in json.loads(deal["risk_flags_json"])
    assert deal["market_discount_pct"] == pytest.approx(16.31, abs=0.01)
    assert "legacy_composite_score" not in deal.keys()
    assert views == {"method_ranks", "top_needing_check"}
    assert unranked["exclusion_reason"] == "floor_unknown" and unranked["rank_in_neighborhood"] is None
    assert json.loads(meta["as_of"]) == "2026-11-02"
    assert json.loads(meta["final_schema_version"]) == final_db.FINAL_SCHEMA_VERSION
    assert mode == "delete" and integrity == "ok"


def _existing(target):
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE deals (url TEXT)")
    conn.execute("INSERT INTO deals VALUES ('old')")
    conn.commit()
    conn.close()
    return target.read_bytes()


def test_writer_failure_leaves_old_database_and_no_temp_file(tmp_path, results, monkeypatch):
    target = tmp_path / "final_deals.db"
    before = _existing(target)

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(final_db, "_insert_deals", boom)
    with pytest.raises(RuntimeError, match="injected"):
        _publish(target, results)
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".final_deals")] == []


def test_open_reader_makes_publish_busy_and_keeps_old_database(tmp_path, results):
    target = tmp_path / "final_deals.db"
    before = _existing(target)
    reader = sqlite3.connect(target)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM deals").fetchall()
    try:
        with pytest.raises(FinalDatabaseBusyError):
            _publish(target, results)
    finally:
        reader.rollback()
        reader.close()
    assert target.read_bytes() == before


def test_held_lock_file_makes_publish_busy(tmp_path, results):
    target = tmp_path / "final_deals.db"
    lock = open(tmp_path / "final_deals.db.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(FinalDatabaseBusyError):
            _publish(target, results)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    assert not target.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_stray_sidecar_fails_closed_and_is_never_deleted(tmp_path, results, suffix):
    target = tmp_path / "final_deals.db"
    before = _existing(target)
    sidecar = tmp_path / f"final_deals.db{suffix}"
    sidecar.write_bytes(b"not ours")
    with pytest.raises(FinalDatabaseSidecarError):
        _publish(target, results)
    assert sidecar.read_bytes() == b"not ours"
    assert target.read_bytes() == before


def test_wal_mode_target_is_checkpointed_then_replaced(tmp_path, results):
    target = tmp_path / "final_deals.db"
    conn = sqlite3.connect(target)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE deals (url TEXT)")
    conn.commit()
    conn.close()
    _publish(target, results)
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert not (tmp_path / "final_deals.db-wal").exists()


def test_publish_to_new_target(tmp_path, results):
    target = tmp_path / "final_deals.db"
    _publish(target, results)
    assert target.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".final_deals")]
    assert leftovers == []


def test_verification_fields_and_views(tmp_path, results):
    from dataclasses import replace as _replace

    ranked, unranked, hoods, metadata = results
    flagged = [_replace(ranked[0], needs_check=True, check_reasons=("price_outlier",))]
    target = tmp_path / "final_deals.db"
    write_final_database_atomic(target, flagged, unranked, hoods, metadata)
    conn = sqlite3.connect(target)
    try:
        reasons = conn.execute("SELECT check_reasons_json FROM top_needing_check").fetchone()[0]
    finally:
        conn.close()
    assert json.loads(reasons) == ["price_outlier"]
