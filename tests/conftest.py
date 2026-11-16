"""Keep scoring-run side effects and the real trained rent model out of the tests."""

import pytest

from greeceapt.storage import paths
from greeceapt.rent import config as rent_config


@pytest.fixture(autouse=True)
def _isolated_data_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "RANK_HISTORY_DB", tmp_path / "rank_history.sqlite3")
    monkeypatch.setattr(paths, "RANK_HISTORY_REPORT", tmp_path / "rank_history_report.md")
    monkeypatch.setattr(paths, "SPITOGATOS_SWEEP_JSON", tmp_path / "spitogatos_sweep.json")
    # Scoring reads the Spitogatos store to see which advertisements are still listed, and the
    # ``update`` command writes a log, a report and backups: none of it may touch data/.
    monkeypatch.setattr(paths, "SPITOGATOS_LISTINGS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(paths, "SPITOGATOS_LAST_RUN_JSON", tmp_path / "spitogatos_last_run.json")
    monkeypatch.setattr(paths, "UPDATE_LOG_JSONL", tmp_path / "update_log.jsonl")
    monkeypatch.setattr(paths, "UPDATE_REPORT_MD", tmp_path / "update_report.md")
    monkeypatch.setattr(paths, "UPDATE_LOCK", tmp_path / "update.lock")
    monkeypatch.setattr(paths, "BACKUPS_DIR", tmp_path / "backups")
    # Many fixtures use XE advertisements; with XE no longer collected they would all be unranked.
    from greeceapt.scoring import availability
    monkeypatch.setattr(availability, "xe_is_collected", lambda: True)
    # A scoring run must not depend on data/rent_model.json: without a model nothing is ranked.
    # Tests that need a ranking pass a stub model in (tests/test_rent_yield.FlatRentModel).
    monkeypatch.setattr(rent_config, "RENT_MODEL_JSON", tmp_path / "rent_model.json")
