"""Persistent opportunity identities."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from greeceapt.collect.opportunity_registry import OpportunityRegistry

T0 = datetime(2026, 11, 2, tzinfo=timezone.utc)


def _expected_id(url: str) -> str:
    return "opp_" + hashlib.sha1(url.encode()).hexdigest()[:16]


def test_new_group_gets_an_id_from_its_first_url(tmp_path):
    registry = OpportunityRegistry(tmp_path / "reg.sqlite3")
    ids = registry.assign_ids([["https://b/1", "https://a/1"]], now=T0)
    registry.commit()
    assert ids == {"https://a/1": _expected_id("https://a/1"), "https://b/1": _expected_id("https://a/1")}


def test_adding_a_member_with_a_smaller_url_keeps_the_id(tmp_path):
    path = tmp_path / "reg.sqlite3"
    first = OpportunityRegistry(path)
    original = first.assign_ids([["https://m/1"]], now=T0)["https://m/1"]
    first.commit()
    first.close()

    second = OpportunityRegistry(path)
    ids = second.assign_ids([["https://a/0", "https://m/1"]], now=T0 + timedelta(days=7))
    second.commit()
    assert ids["https://a/0"] == original
    assert ids["https://m/1"] == original


def test_merging_two_known_ids_keeps_the_oldest_and_records_an_alias(tmp_path):
    path = tmp_path / "reg.sqlite3"
    registry = OpportunityRegistry(path)
    old = registry.assign_ids([["https://x/1"]], now=T0)["https://x/1"]
    registry.commit()
    young = registry.assign_ids([["https://y/1"]], now=T0 + timedelta(days=1))["https://y/1"]
    registry.commit()

    ids = registry.assign_ids([["https://x/1", "https://y/1"]], now=T0 + timedelta(days=2))
    registry.commit()
    assert set(ids.values()) == {old}
    assert registry.resolve(young) == old
    assert registry.resolve("https://y/1") == old


def test_splitting_a_group_gives_the_split_part_a_new_id_and_alias(tmp_path):
    registry = OpportunityRegistry(tmp_path / "reg.sqlite3")
    original = registry.assign_ids([["https://a/1", "https://b/1"]], now=T0)["https://a/1"]
    registry.commit()

    ids = registry.assign_ids([["https://a/1"], ["https://b/1"]], now=T0 + timedelta(days=1))
    registry.commit()
    assert ids["https://a/1"] == original
    assert ids["https://b/1"] != original
    conn = sqlite3.connect(tmp_path / "reg.sqlite3")
    try:
        reasons = {row[0] for row in conn.execute("SELECT reason FROM opportunity_aliases")}
    finally:
        conn.close()
    assert "split" in reasons


def test_a_split_part_keeps_its_new_id_on_later_runs(tmp_path):
    """Nov 2026: a split-off flat got a fresh ID on every re-score (the split alias sent it back)."""
    registry = OpportunityRegistry(tmp_path / "reg.sqlite3")
    registry.assign_ids([["https://a/1", "https://b/1"]], now=T0)
    registry.commit()
    seen = []
    for day in range(1, 4):
        ids = registry.assign_ids([["https://a/1"], ["https://b/1"]], now=T0 + timedelta(days=day))
        registry.commit()
        seen.append((ids["https://a/1"], ids["https://b/1"]))
    assert seen[0] == seen[1] == seen[2] and seen[0][0] != seen[0][1]


def test_nothing_is_written_before_commit(tmp_path):
    path = tmp_path / "reg.sqlite3"
    registry = OpportunityRegistry(path)
    registry.assign_ids([["https://a/1"]], now=T0)
    registry.rollback()
    registry.close()
    reopened = OpportunityRegistry(path)
    assert reopened.resolve("https://a/1") is None


def test_fresh_registries_are_deterministic(tmp_path):
    groups = [["https://c/1", "https://a/1"], ["https://b/1"]]
    first = OpportunityRegistry(tmp_path / "one.sqlite3").assign_ids(groups, now=T0)
    second = OpportunityRegistry(tmp_path / "two.sqlite3").assign_ids(groups, now=T0)
    assert first == second
