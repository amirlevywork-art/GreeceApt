"""Tests for greeceapt.storage.ingest."""

import json

import pytest

from greeceapt.storage import ingest


def test_load_json_list(tmp_path):
    path = tmp_path / "listings.json"
    path.write_text(json.dumps([{"url": "https://a/1", "price_eur": 1}]), encoding="utf-8")
    rows = ingest.load_json(str(path))
    assert len(rows) == 1
    assert rows[0]["url"] == "https://a/1"


def test_load_json_rejects_non_list(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        ingest.load_json(str(path))


def test_load_json_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest.load_json(str(tmp_path / "nope.json"))


# ── CLI: --as-of and --renovation-budgets ─────────────────────────────────────

from datetime import date

from greeceapt import main as cli


@pytest.fixture
def captured(monkeypatch):
    calls: dict = {}

    def fake_full(*, as_of, renovation_budgets, interim_every=200):
        calls["full"] = (as_of, renovation_budgets)
        calls["interim_every"] = interim_every

    def fake_score(*, as_of, renovation_budgets):
        calls["score"] = (as_of, renovation_budgets)

    monkeypatch.setattr(cli, "run_full_pipeline", fake_full)
    monkeypatch.setattr(cli, "run_score_only", fake_score)
    return calls


def test_explicit_as_of_reaches_full_and_score_only(captured):
    cli.main(["--as-of", "2026-11-02"])
    cli.main(["--score-only", "--as-of", "2026-11-02"])
    assert captured["full"][0] == date(2026, 11, 2)
    assert captured["score"][0] == date(2026, 11, 2)


def test_omitted_as_of_is_captured_once_at_start(captured, monkeypatch):
    class FrozenDate(date):
        calls = 0

        @classmethod
        def today(cls):
            cls.calls += 1
            return cls(2026, 11, 5)

    monkeypatch.setattr(cli, "date", FrozenDate)
    cli.main(["--score-only"])
    assert captured["score"][0] == date(2026, 11, 5)
    assert FrozenDate.calls == 1


def test_invalid_date_fails_before_any_stage(captured):
    with pytest.raises(SystemExit):
        cli.main(["--as-of", "2026-13-40"])
    assert captured == {}


@pytest.mark.parametrize(
    "payload",
    ['{"opp_1": -5}', '{"opp_1": "abc"}', '[1, 2]', '{"opp_1": NaN}', '{"opp_1": Infinity}', "{broken"],
)
def test_malformed_or_negative_budgets_fail_before_any_stage(captured, tmp_path, payload):
    path = tmp_path / "budgets.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["--score-only", "--renovation-budgets", str(path)])
    assert captured == {}


def test_valid_budgets_are_passed_as_floats(captured, tmp_path):
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps({"opp_1": 10000, "https://www.xe.gr/x/1": 2500.5}), encoding="utf-8")
    cli.main(["--score-only", "--as-of", "2026-11-02", "--renovation-budgets", str(path)])
    assert captured["score"][1] == {"opp_1": 10000.0, "https://www.xe.gr/x/1": 2500.5}


def test_conflicting_budgets_for_one_opportunity_are_rejected():
    from greeceapt.scoring.scoring_conductor import RenovationBudgetError, resolve_renovation_budgets

    candidates = [{"opportunity_id": "opp_1", "url": "https://a/1", "source_urls": ["https://a/1", "https://b/1"]}]
    with pytest.raises(RenovationBudgetError, match="conflicting"):
        resolve_renovation_budgets({"https://a/1": 1000, "https://b/1": 2000}, candidates, None)
    with pytest.raises(RenovationBudgetError, match="matches no"):
        resolve_renovation_budgets({"https://zzz/1": 1000}, candidates, None)
    assert resolve_renovation_budgets({"https://b/1": 1000, "opp_1": 1000}, candidates, None) == {"opp_1": 1000.0}


def test_interim_every_is_passed_and_validated(captured):
    cli.main(["--interim-every", "50"])
    assert captured["interim_every"] == 50
    with pytest.raises(SystemExit):
        cli.main(["--interim-every", "-1"])

def test_load_listings_json_list_ok(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps([{"a": 1}]), encoding="utf-8")
    assert ingest.load_listings_json_list(p) == [{"a": 1}]


def test_load_listings_json_list_not_list(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError):
        ingest.load_listings_json_list(p)
