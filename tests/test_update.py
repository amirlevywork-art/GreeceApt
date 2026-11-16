"""The manual ``./update`` command: lock, backups, before/after report, last-update date and time."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from greeceapt import update
from greeceapt.storage import paths
from greeceapt.collect import run_all
from greeceapt.collect.spitogatos import scrape

URL = "https://www.spitogatos.gr/en/property/{}"


@pytest.fixture(autouse=True)
def _data_files(tmp_path, monkeypatch):
    for name, filename in (
        ("FINAL_DB", "final_deals.db"), ("INGESTED_DB", "ingested_listings.db"),
        ("UPDATED_DB", "updated_listings.db"), ("OPPORTUNITY_REGISTRY_DB", "opportunity_registry.sqlite3"),
    ):
        monkeypatch.setattr(paths, name, tmp_path / filename)
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)


@pytest.fixture
def athens_time(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Athens")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _flat(oid, *, hood="Kypseli", rank=1, price=50_000.0, ranked=True, reason=None, **over) -> dict:
    return {
        "opportunity_id": oid, "neighborhood": hood,
        "rank_in_neighborhood": rank if ranked else None, "url": URL.format(oid), "title": f"Flat {oid}",
        "price_eur": price, "area_sqm": 40.0, "net_yield_lo": 0.06 if ranked else None, "days_on_market": 30,
        "exclusion_reason": reason, "ranked": ranked, **over,
    }


def _write_ranking(flats, *, created_at="2026-11-12T14:57:00+00:00") -> None:
    paths.FINAL_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(paths.FINAL_DB)
    columns = update._SNAPSHOT_COLUMNS
    for table in ("deals", "unranked_deals"):
        conn.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
    for flat in flats:
        conn.execute(
            f"INSERT INTO {'deals' if flat['ranked'] else 'unranked_deals'} VALUES ({', '.join('?' * len(columns))})",
            [flat[column] for column in columns],
        )
    conn.execute("CREATE TABLE ranking_metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
    conn.execute("INSERT INTO ranking_metadata VALUES ('created_at', ?)", (json.dumps(created_at),))
    conn.commit()
    conn.close()


def _sweep_summary(**over) -> dict:
    summary = {
        "started_at": update._now().isoformat(), "finished_at": (update._now() + timedelta(seconds=1)).isoformat(),
        "status": "ok", "blocked": False, "trusted": True, "trust_reason": "ok", "passes": 1, "seen": 1500,
        "total_results": 1507, "store_rows": 1510, "new": 12, "price_changes": 9, "photos_changed": 5,
        "returned": 1, "gone": 4, "detail_fetches": 17, "details_stored": 17,
    }
    summary.update(over)
    return summary


BEFORE = [
    _flat("a", rank=1), _flat("b", rank=2, price=60_000.0), _flat("c", rank=3),
    _flat("d", rank=4), _flat("e", hood="Patisia", rank=1),
    _flat("f", ranked=False, reason="floor_unknown"),
]
AFTER = [
    _flat("a", rank=1), _flat("b", rank=4, price=57_000.0),                      # price cut
    _flat("c", ranked=False, reason="no_longer_listed"),                          # gone from the site
    _flat("d", rank=2), _flat("n", rank=3, price=45_000.0),                       # d moved up, n is new
    _flat("e", hood="Patisia", ranked=False, reason="xe_only_untracked"),
    _flat("f", ranked=False, reason="floor_unknown"),
]


def _by_id(flats) -> dict:
    return {flat["opportunity_id"]: flat for flat in flats}


def _ids(flats) -> list[str]:
    return [flat["opportunity_id"] for flat in flats]


def test_second_update_is_refused_while_one_is_running(capsys):
    ran = []
    with update.update_lock():
        code = update.run_update(pipeline=lambda budgets: ran.append(budgets))
    assert code == update.EXIT_BUSY and ran == []
    assert "already running" in capsys.readouterr().err
    assert not paths.UPDATE_LOG_JSONL.exists() and not paths.BACKUPS_DIR.exists()

    with update.update_lock():                                 # the lock is free again afterwards
        pass


def test_backup_copies_the_files_and_keeps_the_last_five():
    _write_ranking(BEFORE)
    paths.SPITOGATOS_LISTINGS_JSON.write_text('[{"url": "x"}]', encoding="utf-8")
    start = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
    keep_me = paths.BACKUPS_DIR / "notes"
    keep_me.mkdir(parents=True)

    folders = [update.make_backup(start + timedelta(hours=hour))[0] for hour in range(7)]

    folder, copied = folders[-1], len(list(folders[-1].iterdir()))
    assert copied == 2 and (folder / "spitogatos_listings.json").read_text(encoding="utf-8") == '[{"url": "x"}]'
    assert _ids(update.snapshot_ranking(folder / "final_deals.db").values()) == _ids(BEFORE)
    kept = sorted(p.name for p in paths.BACKUPS_DIR.iterdir())
    assert kept == sorted([f.name for f in folders[-5:]] + ["notes"])   # only backup folders are pruned


def test_backup_that_cannot_be_taken_stops_the_update(capsys):
    paths.FINAL_DB.write_text("this is not a database, " * 100, encoding="utf-8")
    ran = []
    assert update.run_update(pipeline=lambda budgets: ran.append(budgets)) == update.EXIT_FAILED
    assert ran == [] and "Nothing was requested or changed" in capsys.readouterr().err
    (entry,) = update.read_log()
    assert entry["status"] == "failed" and entry["error"].startswith("backup failed")
    assert not paths.UPDATE_REPORT_MD.exists()


def test_diff_names_what_changed_price_appeared_and_went():
    changes = update.diff_rankings(_by_id(BEFORE), _by_id(AFTER))

    assert set(changes) == {"price_changes", "new_ranked", "no_longer_listed", "left_ranking"}
    assert [(f["opportunity_id"], f["old_price_eur"], f["price_eur"]) for f in changes["price_changes"]] == [
        ("b", 60_000.0, 57_000.0)
    ]
    assert _ids(changes["new_ranked"]) == ["n"]
    assert _ids(changes["no_longer_listed"]) == ["c"]
    assert [(f["opportunity_id"], f["reason"]) for f in changes["left_ranking"]] == [("e", "xe_only_untracked")]


def test_diff_of_an_unchanged_ranking_is_empty():
    changes = update.diff_rankings(_by_id(BEFORE), _by_id(BEFORE))
    assert all(flats == [] for flats in changes.values())


def test_update_writes_the_report_the_log_and_the_last_update_time(athens_time, capsys):
    _write_ranking(BEFORE)
    budgets_seen = []

    def pipeline(budgets):
        budgets_seen.append(budgets)
        paths.SPITOGATOS_LAST_RUN_JSON.write_text(json.dumps(_sweep_summary()), encoding="utf-8")
        _write_ranking(AFTER)

    code = update.run_update(renovation_budgets={"opp_1": 5000.0}, pipeline=pipeline)

    assert code == update.EXIT_OK and budgets_seen == [{"opp_1": 5000.0}]
    (entry,) = update.read_log()
    assert entry["ok"] and entry["status"] == "ok" and entry["error"] is None
    assert entry["counts"]["ranked_before"] == 5 and entry["counts"]["ranked_after"] == 4
    assert {k: entry["counts"][k] for k in ("price_changes", "new_ranked", "no_longer_listed", "left_ranking")} == {
        "price_changes": 1, "new_ranked": 1, "no_longer_listed": 1, "left_ranking": 1,
    }
    assert "entered_top3" not in entry["counts"]
    assert entry["counts"]["sweep"]["seen"] == 1500 and entry["counts"]["sweep"]["trusted"] is True

    report = paths.UPDATE_REPORT_MD.read_text(encoding="utf-8")
    assert "Previous update: none recorded" in report and "Outcome: ok" in report
    assert "Kypseli #3 — €45,000, 40 m², net yield 6.0% —" in report
    assert "top 3" not in report.lower()
    assert "Kypseli #4 — €60,000 → €57,000 (-5.0%)" in report
    assert "last price €50,000, 30 days on the market" in report
    assert "xe only untracked" in report
    assert len(list(paths.BACKUPS_DIR.iterdir())) == 1

    finished = update.local_time(entry["finished_at"])
    assert finished == datetime.fromisoformat(entry["finished_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    out = capsys.readouterr().out
    assert "No update has been run with ./update yet." in out          # printed before this first run
    assert f"Update finished {finished}" in out
    status = update.format_status()
    assert status.startswith(f"Last update: {finished} (just now) — ok")
    assert "saw 1,500 of 1,507 results (trusted)" in status and "1 price changes" in status
    assert "Report: update_report.md" in status


def test_status_shows_the_local_date_and_time_and_the_age(athens_time):
    update.append_log({
        "started_at": "2026-11-12T17:05:00+00:00", "finished_at": "2026-11-12T18:17:00+00:00", "ok": True,
        "status": "incomplete", "error": None,
        "counts": {"ranked_before": 700, "ranked_after": 705, "sweep": _sweep_summary(trusted=False, gone=0,
                                                                                    trust_reason="saw 1300 of 1507 results")},
    })
    update.append_log({
        "started_at": "2026-11-13T07:00:00+00:00", "finished_at": "2026-11-13T07:00:20+00:00", "ok": False,
        "status": "blocked", "error": "Spitogatos refused the collector (exit code 3)", "counts": {},
    })
    paths.UPDATE_LOG_JSONL.open("a", encoding="utf-8").write("not json\n")

    status = update.format_status(datetime(2026, 11, 13, 7, 30, tzinfo=timezone.utc)).splitlines()

    assert status[0] == ("Last update: 2026-11-12 20:17 (13 hours ago) — done, but the Spitogatos sweep was "
                         "not trusted, so no flat was marked gone, took 1 h 12 min")
    assert "NOT trusted (saw 1300 of 1507 results)" in status[1]
    assert status[-1].startswith("Last attempt: 2026-11-13 09:00 (30 minutes ago) — blocked — Spitogatos refused")


def test_status_before_any_update_falls_back_to_the_ranking_and_the_store(athens_time):
    assert update.format_status() == "No update has been run with ./update yet."

    _write_ranking(BEFORE, created_at="2026-11-12T14:57:32+00:00")
    paths.SPITOGATOS_LAST_RUN_JSON.write_text(
        json.dumps(_sweep_summary(finished_at="2026-11-02T00:28:00+00:00")), encoding="utf-8"
    )
    status = update.format_status(datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)).splitlines()
    assert status == [
        "No update has been run with ./update yet.",
        "Ranking last built: 2026-11-12 16:57 (2 hours ago)",
        "Spitogatos last collected: 2026-11-02 02:28 (11 days ago)",
    ]


def test_refused_collector_is_recorded_and_the_ranking_is_left_alone(capsys):
    _write_ranking(BEFORE)

    def pipeline(budgets):
        raise run_all.ScraperBlocked("Spitogatos refused the collector (exit code 3)")

    code = update.run_update(pipeline=pipeline)

    assert code == update.EXIT_BLOCKED
    (entry,) = update.read_log()
    assert (entry["ok"], entry["status"]) == (False, "blocked") and "refused" in entry["error"]
    assert entry["counts"]["sweep"] is None and entry["counts"]["new_ranked"] == 0
    assert _ids(update.snapshot_ranking().values()) == _ids(BEFORE)
    assert "Nothing else was requested" in paths.UPDATE_REPORT_MD.read_text(encoding="utf-8")
    assert update.last_update() is None
    assert "Last attempt:" in update.format_status() and "blocked" in capsys.readouterr().out


def test_failed_and_interrupted_runs_do_not_count_as_the_last_update():
    def broken(budgets):
        raise RuntimeError("Check that Ollama is running")

    def stopped(budgets):
        raise KeyboardInterrupt

    assert update.run_update(pipeline=broken) == update.EXIT_FAILED
    assert update.run_update(pipeline=stopped) == update.EXIT_INTERRUPTED
    failed, interrupted = update.read_log()
    assert failed["status"] == "failed" and failed["error"] == "RuntimeError: Check that Ollama is running"
    assert interrupted["status"] == "interrupted" and update.last_update() is None


def test_old_collector_summary_is_not_reported_for_a_run_that_wrote_none():
    paths.SPITOGATOS_LAST_RUN_JSON.write_text(
        json.dumps(_sweep_summary(finished_at="2026-11-01T10:00:00+00:00")), encoding="utf-8"
    )
    assert update.run_update(pipeline=lambda budgets: None) == update.EXIT_OK
    (entry,) = update.read_log()
    assert entry["status"] == "ok" and entry["counts"]["sweep"] is None


def test_check_is_a_dry_run_that_writes_nothing(monkeypatch, capsys):
    calls = []

    async def fake_run(*, dry_run=False):
        calls.append(dry_run)
        return _sweep_summary(status="dry_run", finished_at=None)

    monkeypatch.setattr(scrape, "run", fake_run)

    assert update.main(["check"]) == update.EXIT_OK
    assert calls == [True]
    out = capsys.readouterr().out
    assert "Spitogatos lists 1,507 results now." in out and "saw 1,500 of them in 1 pass: trusted." in out
    assert "new                12" in out and "gone                4" in out
    assert "at most 17 flats" in out and "up to about 28 min" in out and "Nothing was written." in out
    assert not paths.UPDATE_LOG_JSONL.exists() and not paths.UPDATE_REPORT_MD.exists()
    assert not paths.BACKUPS_DIR.exists()


def test_check_reports_a_refusal_with_the_refusal_exit_code(monkeypatch, capsys):
    async def refused(*, dry_run=False):
        return _sweep_summary(status="dry_run", blocked=True, trusted=False, trust_reason="refused by the site")

    monkeypatch.setattr(scrape, "run", refused)
    assert update.main(["check"]) == update.EXIT_BLOCKED
    assert "refused" in capsys.readouterr().err


def test_command_line_routes_status_and_run(monkeypatch, capsys, tmp_path):
    assert update.main(["status"]) == update.EXIT_OK
    assert "No update has been run" in capsys.readouterr().out

    seen = {}
    monkeypatch.setattr(update, "run_update", lambda *, renovation_budgets: seen.update(renovation_budgets) or 0)
    budgets = tmp_path / "budgets.json"
    budgets.write_text('{"opp_9": 4000}', encoding="utf-8")
    assert update.main(["--renovation-budgets", str(budgets)]) == 0 and seen == {"opp_9": 4000.0}

    budgets.write_text('{"opp_9": -1}', encoding="utf-8")
    with pytest.raises(SystemExit):
        update.main(["run", "--renovation-budgets", str(budgets)])
