"""Local GUI: progress parsing and time left, the job runner, the API guard, duplicate decisions."""

from __future__ import annotations

import json
import sqlite3
import sys
import time

import pytest

from greeceapt.gui import progress as P
from greeceapt.gui.actions import Action
from greeceapt.gui.jobs import JobManager


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ── progress ──────────────────────────────────────────────────────────────────

def test_log_patterns_match_old_and_new_lines():
    new = "2026-11-15 21:22:12 [INFO] Layer 1 V2: id=5565     status=accepted score=8.04 accepted=11 rejected=3 cache_hits=0 (7/173)"
    old = "2026-11-15 21:22:12 [INFO] Layer 1 V2: id=5565     status=accepted score=8.04 accepted=11 rejected=3 cache_hits=0"
    failed = "[ERROR] Layer 1 V2: id=12 all 9 image downloads failed (1/5 consecutive); result left pending for retry. (8/173)"
    m = P.LAYER1_TICK.search(new)
    assert m and m["done"] == "7" and m["total"] == "173"
    assert P.LAYER1_TICK.search(old) and P.LAYER1_TICK.search(old)["done"] is None
    assert P.LAYER1_TICK.search(failed)["done"] == "8"
    assert P.LAYER1_TOTAL.search("Layer 1 V2: 173 listings to process (top-first by yield upper bound).")["total"] == "173"
    a = P.PHASE_A_TICK.search("Phase A pass 1 — offset=60  seen=60  total=1506")
    assert a["done"] == "60" and a["total"] == "1506"
    assert P.PHASE_A_TICK.search("Phase A pass 1 — offset=0  seen=0  total=?")["total"] == "?"
    assert P.PHASE_A_TICK.search("Phase A pass 1 — offset=1380  seen=1380")["done"] == "1380"   # older logs
    assert P.FINGERPRINT_TICK.search("Fingerprints: 300 / 1338 listings")["total"] == "1338"


def test_counted_stage_time_left_follows_the_observed_rate():
    clock = Clock()
    stage = P.Stage("l1", "Image model", tick=P.LAYER1_TICK, total=P.LAYER1_TOTAL, per_item_s=100.0)
    tracker = P.Tracker("x", [stage], now=clock)
    tracker.feed("Layer 1 V2: 10 listings to process")
    snap = tracker.snapshot()
    assert snap.total == 10 and snap.eta_s == pytest.approx(1000)       # prior rate before any item
    for i in range(1, 6):                                                # items actually take 50 s
        clock.t += 50
        tracker.feed(f"Layer 1 V2: id={i} status=accepted score=7 accepted=3 rejected=0 cache_hits=0 ({i}/10)")
    snap = tracker.snapshot()
    assert snap.done == 5
    assert 5 * 50 <= snap.eta_s < 5 * 100                                # moved toward the observed rate
    assert 0.3 < snap.fraction < 0.8


def test_stages_advance_skip_and_finish():
    clock = Clock()
    stages = [P.Stage("a", "A", expected_s=10), P.Stage("b", "B", P.rx("start B"), expected_s=20),
              P.Stage("c", "C", P.rx("start C"), expected_s=30)]
    tracker = P.Tracker("x", stages, now=clock)
    assert tracker.snapshot().eta_s == pytest.approx(60)
    clock.t = 5
    tracker.feed("start C")                       # B never appears: skipped, counts as done
    snap = tracker.snapshot()
    assert snap.stage == "C" and snap.eta_s == pytest.approx(30)
    clock.t = 100                                 # a timed stage over its estimate: bar capped, not done
    assert tracker.snapshot().eta_s == 0 and tracker.snapshot().fraction < 1
    tracker.finish()
    assert tracker.snapshot().fraction == 1.0
    record = tracker.record(True)
    assert record["stages"]["a"]["duration_s"] == pytest.approx(5)
    assert record["stages"]["c"]["duration_s"] == pytest.approx(95)


def test_history_supplies_durations_and_rates(tmp_path):
    history = P.History(tmp_path / "jobs.jsonl")
    for seconds in (40, 60, 50):
        history.append({"action": "x", "ok": True, "stages": {"a": {"duration_s": seconds, "per_item_s": seconds / 10}}})
    history.append({"action": "x", "ok": False, "stages": {"a": {"duration_s": 999}}})   # failed runs ignored
    again = P.History(tmp_path / "jobs.jsonl")
    assert again.duration("x", "a") == 50 and again.rate("x", "a") == 5
    tracker = P.Tracker("x", [P.Stage("a", "A", expected_s=10)], again, now=Clock())
    assert tracker.snapshot().eta_s == pytest.approx(50)


# ── jobs ──────────────────────────────────────────────────────────────────────

def _action(key, code, resources=frozenset({"r"})):  # noqa: B008
    return Action(key, key, "Test", "", lambda p: [[sys.executable, "-c", code]],
                  [P.Stage("one", "One", expected_s=1), P.Stage("two", "Two", P.rx("--- two"), expected_s=1)],
                  frozenset(resources))


def _wait(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_job_runs_logs_progress_and_history(tmp_path):
    action = _action("hello", "print('hello'); print('--- two'); print('done')")
    jobs = JobManager(root=tmp_path, history_path=tmp_path / "h.jsonl", log_dir=tmp_path / "logs",
                      actions={"hello": action})
    job = jobs.submit("hello")
    assert _wait(lambda: job.status == "ok")
    assert list(job.lines) == ["hello", "--- two", "done"]
    assert "done" in jobs.full_log(job.id)
    record = json.loads((tmp_path / "h.jsonl").read_text().splitlines()[-1])
    assert record["ok"] and set(record["stages"]) == {"one", "two"}


def test_jobs_sharing_a_resource_queue_and_stop_works(tmp_path):
    slow = _action("slow", "import time; print('start', flush=True); time.sleep(60)")
    fast = _action("fast", "print('fast')")
    other = _action("other", "print('other')", resources={"elsewhere"})
    jobs = JobManager(root=tmp_path, history_path=tmp_path / "h.jsonl", log_dir=tmp_path / "logs",
                      actions={"slow": slow, "fast": fast, "other": other})
    first = jobs.submit("slow")
    assert _wait(lambda: "start" in first.lines)
    waiting = jobs.submit("fast")
    free = jobs.submit("other")
    assert _wait(lambda: free.status == "ok")             # no shared resource: runs at once
    assert waiting.status == "queued"
    jobs.stop(first.id)
    assert _wait(lambda: first.status == "stopped")
    assert _wait(lambda: waiting.status == "ok")          # starts once the resource is free


def test_failed_and_refused_jobs_are_reported(tmp_path):
    jobs = JobManager(root=tmp_path, history_path=tmp_path / "h.jsonl", log_dir=tmp_path / "logs",
                      actions={"bad": _action("bad", "raise SystemExit(1)"),
                               "blocked": _action("blocked", "raise SystemExit(3)", {"x"})})
    bad, blocked = jobs.submit("bad"), jobs.submit("blocked")
    assert _wait(lambda: bad.status == "failed" and blocked.status == "blocked")
    assert "refused" in blocked.message


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from greeceapt.storage import paths
    from greeceapt.gui.server import create_app

    final = tmp_path / "final_deals.db"
    conn = sqlite3.connect(final)
    cols = ("neighborhood TEXT, rank_in_neighborhood INT, display_rank INT, needs_check INT, opportunity_id TEXT, "
            "url TEXT, source_urls_json TEXT, price_eur REAL, area_sqm REAL, net_yield_lo REAL, exclusion_reason TEXT")
    conn.execute(f"CREATE TABLE deals ({cols})")
    conn.execute(f"CREATE TABLE unranked_deals ({cols})")
    conn.execute("CREATE TABLE ranking_metadata (key TEXT, value_json TEXT)")
    conn.execute("CREATE TABLE neighborhoods (name TEXT, ranked_count INT)")
    conn.execute("INSERT INTO deals VALUES ('Kipseli', 1, 1, 0, 'opp_a', 'https://s/a', '[\"https://s/a\"]', 50000, 40, 0.06, NULL)")
    conn.execute("INSERT INTO unranked_deals VALUES ('Kipseli', NULL, NULL, 0, 'opp_b', 'https://s/b', '[]', 45000, 30, NULL, 'duplicate_review_pending')")
    conn.execute("INSERT INTO ranking_metadata VALUES ('as_of', '\"2026-11-14\"')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(paths, "FINAL_DB", final)
    monkeypatch.setattr(paths, "UPDATED_DB", tmp_path / "missing.db")
    jobs = JobManager(root=tmp_path, history_path=tmp_path / "h.jsonl", log_dir=tmp_path / "logs", actions={})
    return TestClient(create_app(token="tok", manager=jobs, ollama=lambda: {"up": False, "models": []}))


def test_api_serves_flats_and_guards_changes(client):
    flats = client.get("/api/flats").json()
    assert [f["opportunity_id"] for f in flats["flats"]] == ["opp_a", "opp_b"]
    assert flats["flats"][0]["price_psqm"] == 1250 and flats["flats"][0]["source_urls"] == ["https://s/a"]
    assert flats["neighborhoods"]["Kipseli"]["best"] == 0.06
    assert "__TOKEN__" not in client.get("/").text and "tok" in client.get("/").text
    assert client.post("/api/jobs", json={"action": "score_only"}).status_code == 403            # no token
    headers = {"x-greeceapt-token": "tok"}
    assert client.post("/api/jobs", json={"action": "update_check"}, headers=headers).status_code == 409  # network: confirm
    assert client.post("/api/jobs", json={"action": "nope"}, headers=headers).status_code == 404
    assert client.get("/api/flats", headers={"host": "evil.example"}).status_code == 403          # DNS rebinding
    assert client.get("/api/flat/opp_zzz").status_code == 404


# ── duplicate decisions ───────────────────────────────────────────────────────

def test_review_decision_is_saved_and_leaves_the_queue(tmp_path):
    from greeceapt.gui import review
    from greeceapt.collect.opportunity_dedupe import load_decisions

    reviews = [{"pair_key": "k1", "identity": "id1", "urls": ["u1", "u2"], "reason": "r", "fields": {}},
               {"pair_key": "k2", "identity": "id2", "urls": ["u3", "u4"], "reason": "r", "fields": {}}]
    rp, dp, lp = tmp_path / "review.json", tmp_path / "decisions.json", tmp_path / "listings.json"
    rp.write_text(json.dumps(reviews))
    lp.write_text(json.dumps([{"url": "u3", "photo_urls": ["p1"], "description": "d"}]))
    pend = review.pending(review_path=rp, decisions_path=dp, listings_path=lp, held_urls={"u3"})
    assert [p["pair_key"] for p in pend] == ["k2", "k1"]           # the one holding a flat first
    assert pend[0]["sides"][0]["photos"] == ["p1"]
    review.decide("k1", "same", review_path=rp, decisions_path=dp)
    assert load_decisions(dp)["k1"]["decision"] == "same"
    assert [p["pair_key"] for p in review.pending(review_path=rp, decisions_path=dp, listings_path=lp)] == ["k2"]
    with pytest.raises(ValueError):
        review.decide("k2", "maybe", review_path=rp, decisions_path=dp)
    with pytest.raises(KeyError):
        review.decide("k9", "same", review_path=rp, decisions_path=dp)


def test_flat_page_finds_the_ad_under_any_member_url_and_cleans_its_text(tmp_path):
    """The ranking can name an ad a later update dropped; another ad of the flat has the details."""
    from greeceapt.gui import queries

    final, updated = tmp_path / "final.db", tmp_path / "updated.db"
    conn = sqlite3.connect(final)
    conn.execute("CREATE TABLE deals (opportunity_id TEXT, url TEXT, source_urls_json TEXT, price_eur REAL, area_sqm REAL)")
    conn.execute("CREATE TABLE unranked_deals (opportunity_id TEXT, url TEXT, source_urls_json TEXT, price_eur REAL, area_sqm REAL)")
    conn.execute("INSERT INTO deals VALUES ('opp_x', 'https://s/gone', '[\"https://s/gone\", \"https://s/kept\"]', 52000, 52)")
    conn.commit(); conn.close()
    conn = sqlite3.connect(updated)
    conn.execute("CREATE TABLE listings (opportunity_id TEXT, url TEXT, photo_urls_json TEXT, raw_json TEXT, title TEXT)")
    conn.execute("INSERT INTO listings VALUES ('opp_other', 'https://s/kept', '[\"p1\", \"p2\"]', ?, NULL)",
                 (json.dumps({"description": "...SEMI-BASEMENT WITH YARD. <br />52 M2 &amp; bright"}),))
    conn.commit(); conn.close()
    detail = queries.flat_detail("opp_x", final_db=final, updated_db=updated)
    assert [p["url"] for p in detail["photos"]] == ["p1", "p2"]
    assert detail["listing"]["description"] == "...SEMI-BASEMENT WITH YARD.\n52 M2 & bright"


def test_pipeline_jobs_wait_while_an_update_runs_outside_the_window(tmp_path):
    """./update in a terminal holds the ranking database: a re-score must not run on top of it."""
    from greeceapt.gui.jobs import EXTERNAL_WAIT

    outside = ["python -m greeceapt.update"]
    rescore = _action("rescore", "print('ranked')", resources={"final_db"})
    report = _action("report", "print('report')", resources=set())
    jobs = JobManager(root=tmp_path, history_path=tmp_path / "h.jsonl", log_dir=tmp_path / "logs",
                      actions={"rescore": rescore, "report": report}, external=lambda own: list(outside))
    waiting, free = jobs.submit("rescore"), jobs.submit("report")
    assert _wait(lambda: free.status == "ok")             # needs nothing the update holds
    assert waiting.status == "queued" and waiting.message == EXTERNAL_WAIT
    outside.clear()                                       # the outside update ends
    jobs.list()                                           # the window's next poll
    assert _wait(lambda: waiting.status == "ok")
