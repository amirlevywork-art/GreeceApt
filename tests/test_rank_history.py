"""Ranking snapshots and the delisting check."""

from __future__ import annotations

import json
from datetime import date

import pytest

from greeceapt.storage.final_db import write_final_database_atomic
from greeceapt.scoring import rank_history
from greeceapt.scoring.ranking import rank_deals
from greeceapt.scoring.yield_model import YieldAssumptions
from tests.test_rent_yield import FlatRentModel, _row

SP = "https://www.spitogatos.gr/en/property/11"


def test_gone_needs_a_spitogatos_url_unseen_since_the_sweep():
    seen = {SP + "1": "2026-12-01T10:00:00+00:00", SP + "2": "2026-11-01T10:00:00+00:00"}
    sweep = "2026-12-01T09:00:00+00:00"
    assert rank_history.gone_status([SP + "1"], seen, sweep) is False
    assert rank_history.gone_status([SP + "2"], seen, sweep) is True
    assert rank_history.gone_status([SP + "2", SP + "1"], seen, sweep) is False  # one copy still listed
    assert rank_history.gone_status([SP + "2", "https://www.xe.gr/property/d/x"], seen, sweep) is None
    assert rank_history.gone_status(["https://www.xe.gr/property/d/x"], seen, sweep) is None


def _publish(tmp_path):
    rows = [
        _row(SP + "1", price=40_000.0), _row(SP + "2", price=55_000.0),
        _row(SP + "3", price=69_000.0), _row(SP + "4", price=45_000.0, hood="Pagkrati"),
    ]
    ranked, unranked = rank_deals(rows, {}, YieldAssumptions(), rent_model=FlatRentModel())
    final = tmp_path / "final_deals.db"
    write_final_database_atomic(final, ranked, unranked, [], {"as_of": "2026-11-09"})
    return final


def test_snapshot_then_report_against_a_later_complete_sweep(tmp_path):
    final, history = _publish(tmp_path), tmp_path / "history.sqlite3"
    assert rank_history.record_snapshot(final, history, date(2026, 11, 9)) == 4
    assert rank_history.record_snapshot(final, history, date(2026, 11, 9)) == 4  # same day replaces

    listings = tmp_path / "spitogatos_listings.json"
    sweep = tmp_path / "spitogatos_sweep.json"
    # The cheapest Kypseli flat (#1 everywhere) left; the others were seen in the sweep.
    listings.write_text(json.dumps([
        {"url": SP + "1", "updated_at": "2026-11-10T08:00:00+00:00"},
        {"url": SP + "2", "updated_at": "2026-12-01T10:00:00+00:00"},
        {"url": SP + "3", "updated_at": "2026-12-01T10:00:00+00:00"},
        {"url": SP + "4", "updated_at": "2026-12-01T10:00:00+00:00"},
    ]))
    sweep.write_text(json.dumps({"started_at": "2026-12-01T09:00:00+00:00"}))
    report = rank_history.delisting_report(history, listings, sweep)
    assert report is not None and (report.snapshot_as_of, report.flats, report.gone) == ("2026-11-09", 4, 1)
    by_method = {check.method: check for check in report.methods}
    assert set(by_method) == {"net_yield"}
    net = by_method["net_yield"]
    assert (net.top1_gone, net.top1_n) == (1, 2) and net.auc == pytest.approx(5 / 6)
    assert "| net_yield |" in rank_history.render(report)

    # A sweep too soon after the only snapshot gives no report yet.
    sweep.write_text(json.dumps({"started_at": "2026-11-12T09:00:00+00:00"}))
    assert rank_history.delisting_report(history, listings, sweep) is None


def test_snapshots_from_before_the_switch_are_carried_over(tmp_path):
    import sqlite3

    history = tmp_path / "history.sqlite3"
    conn = sqlite3.connect(history)
    conn.execute(
        "CREATE TABLE rank_snapshots (as_of TEXT, url TEXT, neighborhood TEXT, source_urls_json TEXT, "
        "price_eur REAL, net_yield_lo REAL, combined_score REAL, rank_hand_set_yield INTEGER, "
        "rank_model_net_lo INTEGER, rank_legacy_score INTEGER, rank_combined INTEGER, "
        "n_in_neighborhood INTEGER, PRIMARY KEY (as_of, url))")
    conn.execute("INSERT INTO rank_snapshots VALUES ('2026-11-09', 'u', 'K', '[]', 1, 0.05, 50, 2, 1, 3, 1, 4)")
    conn.commit()
    conn.close()
    final = _publish(tmp_path)
    rank_history.record_snapshot(final, history, date(2026, 11, 12))
    conn = sqlite3.connect(history)
    old = conn.execute(f"SELECT rank_net_yield, rank_hand_set_yield FROM {rank_history.TABLE} "
                       "WHERE as_of = '2026-11-09'").fetchall()
    days = [r[0] for r in conn.execute(f"SELECT DISTINCT as_of FROM {rank_history.TABLE} ORDER BY 1")]
    conn.close()
    assert old == [(1, 2)] and days == ["2026-11-09", "2026-11-12"]
