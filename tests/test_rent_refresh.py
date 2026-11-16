"""Monthly rent-model refresh, the training window and the coefficient history."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from greeceapt.rent import config as cfg
from greeceapt.rent import refresh
from greeceapt.rent.train import append_history, model_age_days, training_rows

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def _rental(url: str, seen: datetime, **over) -> dict:
    row = {
        "id": len(url), "url": url, "rent_eur": 500.0, "area_sqm": 40.0, "latitude": 37.99,
        "longitude": 23.74, "floor_class": "upper", "floor": 2.0, "floor_conflict": 0,
        "last_seen_at": seen.isoformat(),
    }
    row.update(over)
    return row


def test_training_keeps_only_rentals_seen_in_the_window_of_the_newest_sweep():
    rows = [
        _rental("fresh", NOW, latitude=37.98),
        _rental("recentish", NOW - timedelta(days=cfg.TRAIN_WINDOW_DAYS - 1), latitude=37.99),
        _rental("gone-long-ago", NOW - timedelta(days=cfg.TRAIN_WINDOW_DAYS + 1), latitude=38.00),
    ]
    kept, dropped = training_rows(rows)
    assert sorted(r["url"] for r in kept) == ["fresh", "recentish"]
    assert dropped["not_seen_recently"] == 1


def test_history_gets_one_line_per_training_run(tmp_path):
    model = SimpleNamespace(
        meta={"trained_at": NOW.isoformat()}, metrics={"random_cv": {"mape": 0.13}}, alpha=10.0, k=40,
        coefficients=[{"feature": "floor_basement", "coef": -0.13, "ci90": [-0.19, -0.07], "pct_effect": -12.6}],
    )
    path = tmp_path / "history.jsonl"
    rows = [_rental("a", NOW, rent_eur=400.0), _rental("b", NOW, rent_eur=600.0), _rental("c", NOW)]
    append_history(model, rows, path)
    append_history(model, rows, path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["n_train"] == 3 and lines[0]["median_rent_eur_per_sqm"] == 12.5
    assert lines[0]["coefficients"]["floor_basement"]["pct_effect"] == -12.6


def test_model_age_and_due(tmp_path):
    path = tmp_path / "rent_model.json"
    assert model_age_days(path) is None and refresh.is_due(path)
    trained = datetime.now(timezone.utc) - timedelta(days=10)
    path.write_text(json.dumps({"meta": {"trained_at": trained.isoformat()}}))
    assert model_age_days(path, now=trained + timedelta(days=10)) == 10
    assert not refresh.is_due(path, max_age_days=30)
    assert refresh.is_due(path, max_age_days=5)


def test_refresh_trains_only_after_a_successful_sweep():
    calls = []

    def boom():
        raise RuntimeError("blocked")

    assert refresh.refresh(scrape=boom, train=lambda: calls.append("train")) is False
    assert calls == []
    assert refresh.refresh(scrape=lambda: calls.append("scrape"), train=lambda: calls.append("train")) is True
    assert calls == ["scrape", "train"]

    def bad_train():
        raise ValueError("no rows")

    assert refresh.refresh(scrape=lambda: None, train=bad_train) is False


def test_refresh_if_due_skips_a_recent_model(monkeypatch):
    calls = []
    monkeypatch.setattr(refresh, "is_due", lambda *_args, **_kwargs: False)
    assert refresh.refresh_if_due(scrape=lambda: calls.append("scrape"), train=lambda: calls.append("t")) is False
    assert calls == []
    assert refresh.refresh_if_due(force=True, scrape=lambda: calls.append("scrape"), train=lambda: None) is True
    assert calls == ["scrape"]
