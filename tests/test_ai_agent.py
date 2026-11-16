"""Tests for greeceapt.photos (no Ollama / network)."""

import sqlite3

from greeceapt.photos.layer_2_aesthetic_filter import (
    MIN_CONDITION_SCORE,
    MIN_NEIGHBORHOOD_SIZE,
)
from greeceapt.storage import db_conductor


def test_layer_2_thresholds():
    assert MIN_CONDITION_SCORE == 3.0
    assert MIN_NEIGHBORHOOD_SIZE == 10


def _v2_policy_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE listings (id INTEGER PRIMARY KEY, visual_score REAL, "
        "visual_score_v2 REAL, visual_status_v2 TEXT, neighborhood TEXT)"
    )
    conn.executemany(
        "INSERT INTO listings VALUES (?, ?, ?, ?, ?)",
        [
            (1, 8.0, 2.9, "accepted", "A"),
            (2, 8.0, 3.0, "accepted", "A"),
            (3, 8.0, None, "insufficient_evidence", "A"),
            (4, 8.0, None, "model_error", "A"),
        ],
    )
    db_conductor.setup_layer0_tables(conn)
    return conn


def test_v2_removes_only_accepted_low_condition_and_quarantines_unknown():
    conn = _v2_policy_db()

    removed = db_conductor.remove_low_condition_listings(
        conn,
        3.0,
        score_column="visual_score_v2",
        status_column="visual_status_v2",
    )

    assert removed == 1
    assert conn.execute("SELECT id FROM removed_listings").fetchall() == [(1,)]
    assert conn.execute("SELECT id FROM listings ORDER BY id").fetchall() == [(2,), (3,), (4,)]
    conn.close()


# ── Layer 1 health guards ─────────────────────────────────────────────────────

import pytest

from greeceapt.photos import ai_conductor
from greeceapt.vision.ollama import VisionConfig
from greeceapt.vision.types import ListingVisionResult


def _result(status: str) -> ListingVisionResult:
    return ListingVisionResult(
        condition_score=7.0 if status == "accepted" else None,
        effective_confidence=0.9 if status == "accepted" else 0.0,
        status=status,
        accepted_count=1 if status == "accepted" else 0,
    )


class _Connection:
    def commit(self):
        return None

    def close(self):
        return None


def _fake_db(n_listings: int, saved: list):
    class FakeDb:
        @staticmethod
        def connect_updated():
            return _Connection()

        @staticmethod
        def promote_current_v2_results(conn, **kwargs):
            return 0

        @staticmethod
        def get_listings_for_layer1_v2(conn, **kwargs):
            return [(str(i), [f"https://images.example/{i}.jpg"]) for i in range(n_listings)]

        @staticmethod
        def save_visual_audit_v2_results(conn, listing_id, result, **kwargs):
            saved.append((listing_id, result.status))

    return FakeDb


def _service(statuses: list[str]):
    class Service:
        scorer = type("Scorer", (), {"config": VisionConfig()})()
        calls = 0

        def score_listing(self, sources):
            status = statuses[self.calls]
            self.calls += 1
            return _result(status)

    return Service()


def test_health_guard_aborts_when_first_20_listings_are_mostly_failures():
    saved: list = []
    statuses = ["model_error"] * 3 + ["insufficient_evidence"] * 3 + ["accepted"] * 34
    with pytest.raises(ai_conductor.Layer1HealthError, match="6/20"):
        ai_conductor._run_layer1_v2_impl(_fake_db(40, saved), service=_service(statuses))
    # Results saved before the abort stay saved.
    assert len(saved) == 20


def test_health_guard_continues_below_the_bad_rate():
    saved: list = []
    statuses = ["model_error"] * 4 + ["accepted"] * 36
    ai_conductor._run_layer1_v2_impl(_fake_db(40, saved), service=_service(statuses))
    assert len(saved) == 40


def test_health_guard_uses_a_rolling_window_after_the_first_sample():
    saved: list = []
    # Healthy start, then the model breaks: 13 of the last 50 (> 25%) fail.
    statuses = ["accepted"] * 60 + ["model_error"] * 20
    with pytest.raises(ai_conductor.Layer1HealthError):
        ai_conductor._run_layer1_v2_impl(_fake_db(80, saved), service=_service(statuses))
    assert 60 < len(saved) < 80


def test_health_guard_ignores_listings_answered_entirely_from_the_cache():
    """A cached verdict says nothing about the live model: known no-evidence flats must not trip it."""
    saved: list = []
    cached_bad = [
        ListingVisionResult(condition_score=None, effective_confidence=0.0,
                            status="insufficient_evidence", rejected_count=4, cache_hit_count=4)
    ] * 6
    fresh = [_result("accepted")] * 34

    class Service:
        scorer = type("Scorer", (), {"config": VisionConfig()})()
        results = iter(cached_bad + fresh)

        def score_listing(self, sources):
            return next(self.results)

    ai_conductor._run_layer1_v2_impl(_fake_db(40, saved), service=Service())
    assert len(saved) == 40


def test_health_guard_still_counts_a_listing_the_model_failed_on_with_some_cache_hits():
    monitor = ai_conductor.Layer1HealthMonitor()
    partly_cached = ListingVisionResult(condition_score=None, effective_confidence=0.0,
                                        status="model_error", rejected_count=4, cache_hit_count=2)
    assert ai_conductor._model_ran(partly_cached)
    no_images = ListingVisionResult(condition_score=None, effective_confidence=0.0, status="model_error")
    assert ai_conductor._model_ran(no_images)
    with pytest.raises(ai_conductor.Layer1HealthError):
        for status in ["model_error"] * 6 + ["accepted"] * 14:
            monitor.record(status)


def test_health_monitor_threshold_is_strictly_greater_than_25_percent():
    monitor = ai_conductor.Layer1HealthMonitor()
    for status in ["model_error"] * 5 + ["accepted"] * 15:
        monitor.record(status)  # 5/20 == 25% is allowed


def test_smoke_check_raises_when_the_model_returns_unparseable_output():
    class BrokenScorer:
        config = VisionConfig()

        def score(self, jpeg_bytes):
            return type("Inference", (), {"error_category": "invalid_json"})()

    with pytest.raises(ai_conductor.Layer1HealthError, match="smoke"):
        ai_conductor.smoke_check_scorer(BrokenScorer())


def test_smoke_check_passes_when_every_probe_parses():
    seen: list[bytes] = []

    class HealthyScorer:
        config = VisionConfig()

        def score(self, jpeg_bytes):
            seen.append(jpeg_bytes)
            return type("Inference", (), {"error_category": None})()

    ai_conductor.smoke_check_scorer(HealthyScorer())
    assert len(seen) == 2
    assert all(blob[:2] == b"\xff\xd8" for blob in seen)  # JPEG bytes


def test_layer2_removes_low_condition_but_never_thin_neighborhoods(monkeypatch):
    from greeceapt.photos import layer_2_aesthetic_filter as layer2

    calls: list[str] = []

    class Conn:
        def close(self):
            return None

    monkeypatch.setattr(layer2.db_conductor, "connect_updated", lambda: Conn())
    monkeypatch.setattr(
        layer2.db_conductor, "remove_low_condition_listings",
        lambda conn, threshold, **kwargs: calls.append(f"condition<{threshold}") or 0,
    )
    layer2.run()
    assert calls == ["condition<3.0"]


def test_guard_violation_is_an_explicit_exclusion():
    from greeceapt.scoring.ranking import rank_deals
    from greeceapt.scoring.yield_model import YieldAssumptions

    row = {"url": "https://g/1", "neighborhood": "Kipseli", "price_eur": 50_000, "area_sqm": 40,
           "floor": 2, "floor_class": "upper", "visual_status_v2": "accepted", "visual_score_v2": 8,
           "duplicate_guard_violation": True}
    ranked, unranked = rank_deals([row], {}, YieldAssumptions())
    assert ranked == [] and unranked[0].estimate.exclusion_reason == "duplicate_guard_violation"


# ── Top-first ordering and interim rankings (speed plan B2) ───────────────────

def test_yield_upper_bound():
    from greeceapt.photos.ai_conductor import yield_upper_bound

    assert yield_upper_bound({"price_eur": 50_000, "area_sqm": 50, "floor_class": "upper", "floor": 2}) \
        == pytest.approx(50 * 1.0 / 50_000)
    assert yield_upper_bound({"price_eur": 50_000, "area_sqm": 50, "floor_class": "basement", "floor": -1}) \
        == pytest.approx(50 * 0.70 / 50_000)
    assert yield_upper_bound({"price_eur": None, "area_sqm": 50}) is None
    assert yield_upper_bound({"price_eur": 50_000, "area_sqm": 50, "floor_class": None}) is None


def test_work_items_round_robin_over_neighborhoods_best_first():
    from greeceapt.photos.ai_conductor import order_work_items

    items = [("a1", ["u"]), ("a2", ["u"]), ("a3", ["u"]), ("b1", ["u"]), ("b2", ["u"]), ("x", ["u"])]
    bounds = {"a1": 0.003, "a2": 0.002, "a3": 0.001, "b1": 0.0005, "b2": 0.0004, "x": None}
    hoods = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "b2": "B", "x": "B"}
    # Each neighborhood's best first, then each second best, …; unknown bounds last.
    assert [item[0] for item in order_work_items(items, bounds, hoods)] == ["a1", "b1", "a2", "b2", "a3", "x"]


def test_interim_publish_runs_every_n_and_failures_do_not_stop_layer1():
    saved: list = []
    published: list[int] = []

    def interim(count):
        published.append(count)
        if count == 2:
            raise RuntimeError("publish failed")

    ai_conductor._run_layer1_v2_impl(
        _fake_db(5, saved), service=_service(["accepted"] * 5), interim_every=2, on_interim=interim
    )
    assert len(saved) == 5
    assert published == [2, 4]
