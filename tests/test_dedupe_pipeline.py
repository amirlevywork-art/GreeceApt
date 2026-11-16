"""Integration coverage for the default v2 scraper merge pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from greeceapt.collect import run_all
from greeceapt.collect import util as scraper_util
from greeceapt.collect.fingerprints import PhotoFingerprint


def _hash(label: str) -> int:
    return int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)


def _listing(name: str, photos: list[str], **overrides) -> dict:
    row = {
        "url": name,
        "source": "fixture",
        "photo_urls": [f"photo:{label}" for label in photos],
        "photos_count": len(photos),
        "latitude": 37.9838,
        "longitude": 23.7275,
        "area_sqm": 50.0,
        "price_eur": 100_000.0,
        "year_built": 1980,
        "bedrooms": 2,
        "publication_date": "2026-10-20",
    }
    row.update(overrides)
    return row


@pytest.fixture
def merge_paths(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(run_all, "DATA_DIR", tmp_path)
    monkeypatch.setattr(run_all, "OUTPUT_JSON", tmp_path / "listings.json")
    monkeypatch.setattr(run_all, "DELETED_JSON", tmp_path / "deleted_listings.json")
    monkeypatch.setattr(run_all, "STALE_JSON", tmp_path / "stale_listings.json")
    monkeypatch.setattr(run_all, "XE_JSON", tmp_path / "xe_listings.json")
    monkeypatch.setattr(run_all, "SPITOGATOS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(run_all, "REVIEW_JSON", tmp_path / "review_pairs.json")
    monkeypatch.setattr(run_all, "OPPORTUNITY_REVIEW_JSON", tmp_path / "opportunity_review.json")
    monkeypatch.setattr(run_all, "OPPORTUNITY_DECISIONS_JSON", tmp_path / "opportunity_decisions.json")
    monkeypatch.setattr(run_all, "OPPORTUNITY_REGISTRY_DB", tmp_path / "opportunity_registry.sqlite3")
    monkeypatch.setattr(scraper_util.paths, "PHASH_CACHE_JSON", tmp_path / "phash_cache.json")
    return tmp_path


def _install_fake_fingerprints(monkeypatch) -> None:
    async def fake(photo_urls, url_cache, sem, client):
        result = {}
        for url in photo_urls:
            label = url.removeprefix("photo:")
            value = PhotoFingerprint("phash-v2", _hash(label), _hash(f"center:{label}"))
            result[url] = value
            url_cache[url] = value
        return result

    monkeypatch.setattr(scraper_util, "compute_listing_photo_fingerprints", fake)


def test_v2_merge_writes_fingerprints_compatibility_fields_and_conservative_groups(
    monkeypatch, merge_paths, caplog
):
    caplog.set_level("INFO")
    _install_fake_fingerprints(monkeypatch)
    rows = [
        _listing("high-a", ["h1", "h2", "h3"]),
        _listing("high-b", ["h1", "h2", "h3"]),
        _listing("weak-a", ["w1", "w2"]),
        _listing("weak-b", ["w1", "w2"]),
        _listing("price-a", ["p1", "p2", "p3"], price_eur=100_000.0),
        _listing("price-b", ["p1", "p2", "p3"], price_eur=140_000.0),
        _listing("area-a", ["a1", "a2", "a3"], area_sqm=50.0),
        _listing("area-b", ["a1", "a2", "a3"], area_sqm=80.0),
        _listing("bridge-a", ["ab1", "ab2", "ab3"]),
        _listing("bridge-b", ["ab1", "ab2", "ab3", "bc1", "bc2", "bc3"]),
        _listing("bridge-c", ["bc1", "bc2", "bc3"]),
    ]
    (run_all.XE_JSON).write_text(json.dumps(rows), encoding="utf-8")
    (run_all.SPITOGATOS_JSON).write_text("[]", encoding="utf-8")

    fresh, stale, merged = asyncio.run(run_all._merge())

    assert stale == []
    first_pass = [record for record in merged if "opportunity_merge" not in record]
    assert {record["absorbed"]["url"] for record in first_pass} == {"high-b", "bridge-b"}
    # The conservative pass only reviews the two-photo pair; the opportunity pass joins it
    # (two matching photos, same place, price, area and floor).
    assert [record["opportunity_merge"]["member_urls"] for record in merged
            if "opportunity_merge" in record] == [["weak-a", "weak-b"]]
    assert len(fresh) == len(rows) - 3
    output_rows = fresh + [record["absorbed"] for record in first_pass]
    for listing in output_rows:
        assert listing["photo_fingerprints"]
        assert all(value["version"] == "phash-v2" for value in listing["photo_fingerprints"].values())
        assert all(isinstance(value, str) for value in listing["photo_url_hashes"].values())
        assert all(isinstance(value, str) for value in listing["image_hashes"])
    cache = json.loads((merge_paths / "phash_cache.json").read_text(encoding="utf-8"))
    assert cache and all(value["version"] == "phash-v2" for value in cache.values())
    assert "candidates by source" in caplog.text


def test_review_and_merge_audit_include_complete_authorizing_evidence(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    rows = [
        _listing("high-a", ["h1", "h2", "h3"]),
        _listing("high-b", ["h1", "h2", "h3"]),
        _listing("weak-a", ["w1", "w2"]),
        _listing("weak-b", ["w1", "w2"]),
    ]
    run_all.XE_JSON.write_text(json.dumps(rows), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text("[]", encoding="utf-8")

    _fresh, _stale, merged = asyncio.run(run_all._merge())

    reviews = json.loads(run_all.REVIEW_JSON.read_text(encoding="utf-8"))
    weak = next(review for review in reviews if review.get("reason_code") == "insufficient_photo_count")
    required = {
        "a_url", "b_url", "candidate_sources", "full_matches", "center_matches",
        "matches", "photo_match_frac", "min_hamming", "geo_distance_m", "price_rel_delta",
        "agreeing_fields", "conflicting_fields", "reason_code", "confidence",
    }
    assert required <= weak.keys()
    assert len(weak["candidate_sources"]) == len(set(weak["candidate_sources"]))
    assert all({"a_photo_key", "b_photo_key", "distance", "variant"} <= match.keys()
               for match in weak["matches"])

    first_pass = [record for record in merged if "opportunity_merge" not in record]
    assert len(first_pass) == 1
    audit = first_pass[0]
    assert audit["dedupe_policy_version"] == "phash-v2-conservative"
    assert audit["group_member_urls"] == ["high-a", "high-b"]
    assert len(audit["authorizing_evidence"]) == 1
    assert required <= audit["authorizing_evidence"][0].keys()


def test_zero_review_run_removes_stale_review_file(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    run_all.XE_JSON.write_text(json.dumps([_listing("one", ["x1", "x2", "x3"])]), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text("[]", encoding="utf-8")
    run_all.REVIEW_JSON.write_text('[{"stale": true}]', encoding="utf-8")

    asyncio.run(run_all._merge())

    assert not run_all.REVIEW_JSON.exists()


def test_network_disabled_merge_uses_legacy_full_hashes_without_download(monkeypatch, merge_paths):
    async def unexpected_download(*args, **kwargs):
        raise AssertionError("network-disabled merge attempted fingerprint download")

    monkeypatch.setattr(scraper_util, "compute_listing_photo_fingerprints", unexpected_download)
    shared = ["l1", "l2", "l3"]
    rows = [_listing("legacy-a", shared), _listing("legacy-b", shared)]
    run_all.XE_JSON.write_text(json.dumps(rows), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text("[]", encoding="utf-8")
    scraper_util.save_standalone_phash_cache(
        {f"photo:{label}": f"{_hash(label):016x}" for label in shared},
        merge_paths / "phash_cache.json",
    )

    fresh, _stale, merged = asyncio.run(run_all._merge(network_enabled=False))

    assert len(fresh) == 1
    assert len(merged) == 1
    assert all(
        value["version"] == "phash-v1"
        for value in fresh[0]["photo_fingerprints"].values()
    )


def test_survivor_merge_keeps_date_range_and_longer_photo_set():
    kept = {
        "url": "a", "source": "xe", "price_eur": 50_000,
        "publication_date": "2026-03-01", "photo_urls": ["1", "2", "3"],
        "photos_count": 99,
    }
    absorbed = {
        "url": "b", "source": "spitogatos", "price_eur": 60_000,
        "publication_date": "2026-05-20", "photo_urls": ["1"],
    }

    scraper_util.merge_duplicate_listing_into_kept(kept, absorbed)

    assert kept["earliest_publication_date"] == "2026-03-01"
    assert kept["latest_publication_date"] == "2026-05-20"
    assert kept["publication_date"] == "2026-05-20"
    assert kept["photo_urls"] == ["1", "2", "3"]
    assert kept["photos_count"] == 3


def test_normal_main_enables_network_and_merge_only_disables_it(monkeypatch, merge_paths):
    calls = []

    async def fake_subprocess(*args, **kwargs):
        return None

    async def fake_merge(*, network_enabled=True):
        calls.append(network_enabled)
        return [], [], []

    monkeypatch.setattr(run_all, "_run_scraper_subprocess", fake_subprocess)
    monkeypatch.setattr(run_all, "_merge", fake_merge)

    asyncio.run(run_all.main())
    asyncio.run(run_all.merge_only())

    assert calls == [True, False]


def test_only_spitogatos_is_collected(monkeypatch, merge_paths):
    launched = []

    async def fake_subprocess(module, label):
        launched.append(label)

    async def fake_merge(*, network_enabled=True):
        return [], [], []

    monkeypatch.setattr(run_all, "_run_scraper_subprocess", fake_subprocess)
    monkeypatch.setattr(run_all, "_merge", fake_merge)

    asyncio.run(run_all.main())
    assert launched == ["Spitogatos"]       # XE is no longer collected


def test_refused_collector_stops_the_run_before_merging(monkeypatch, merge_paths):
    merged = []

    async def refused_subprocess(module, label):
        raise run_all.ScraperBlocked(f"{label} refused the collector")

    async def fake_merge(*, network_enabled=True):
        merged.append(network_enabled)
        return [], [], []

    monkeypatch.setattr(run_all, "_run_scraper_subprocess", refused_subprocess)
    monkeypatch.setattr(run_all, "_merge", fake_merge)

    with pytest.raises(run_all.ScraperBlocked):
        asyncio.run(run_all.main())
    assert merged == [] and not run_all.OUTPUT_JSON.exists()


def test_collector_exit_code_for_a_refusal_becomes_scraper_blocked(monkeypatch):
    codes = iter([run_all.EXIT_BLOCKED, 1, 0])

    class Proc:
        async def wait(self):
            return next(codes)

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(run_all.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(run_all.ScraperBlocked, match="Spitogatos refused"):
        asyncio.run(run_all._run_scraper_subprocess("module", "Spitogatos"))
    with pytest.raises(RuntimeError, match="exited with code 1") as failed:
        asyncio.run(run_all._run_scraper_subprocess("module", "Spitogatos"))
    assert not isinstance(failed.value, run_all.ScraperBlocked)
    asyncio.run(run_all._run_scraper_subprocess("module", "Spitogatos"))


def test_merge_resolves_floors_before_dedupe(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    spitogatos = _listing(
        "spito-basement",
        ["s1", "s2", "s3"],
        source="spitogatos",
        floor_raw_code=1,
        floor=None,
        description="Studio For Sale, 46 sq.m., Floor: Basement, 1 Level(s)",
    )
    legacy_spitogatos = _listing(
        "spito-legacy",
        ["l1", "l2", "l3"],
        source="spitogatos",
        floor=6.0,  # older scrape files stored the raw code in `floor`
        description=None,
    )
    xe = _listing(
        "xe-semi",
        ["x1", "x2", "x3"],
        source="xe",
        floor=-0.5,
        floor_level_text="Semi-basement",
        description="ΑΜΠΕΛΟΚΗΠΟΙ ΔΙΑΜΕΡΙΣΜΑ 46 ΤΜ,ΗΜΙΥΠΟΓΕΙΟ",
    )
    run_all.XE_JSON.write_text(json.dumps([xe]), encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text(json.dumps([spitogatos, legacy_spitogatos]), encoding="utf-8")

    fresh, _stale, _merged = asyncio.run(run_all._merge(network_enabled=False))
    by_url = {row["url"]: row for row in fresh}

    assert (by_url["spito-basement"]["floor"], by_url["spito-basement"]["floor_class"]) == (-1.0, "basement")
    assert (by_url["spito-legacy"]["floor"], by_url["spito-legacy"]["floor_class"]) == (1.0, "upper")
    assert (by_url["xe-semi"]["floor"], by_url["xe-semi"]["floor_class"]) == (-0.5, "semi_basement")
    for row in fresh:
        assert row["floor_conflict"] is False
        assert row["floor_evidence"], row["url"]
        json.dumps(row["floor_evidence"])


def test_merge_collapses_opportunities_and_assigns_persistent_ids(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    text = (
        "Fully renovated one bedroom apartment in Ampelokipoi, close to Panormou metro, "
        "with new kitchen, new bathroom, security door and aluminium frames."
    )
    rows = [
        _listing("https://agency-a/1", ["a1", "a2", "a3"], description=text, neighborhood="Ampelokipoi"),
        _listing("https://agency-b/1", ["b1", "b2", "b3"], description=text, neighborhood="Ampelokipoi",
                 price_eur=99_000.0),
        _listing("https://other/1", ["o1", "o2", "o3"], neighborhood="Kipseli"),
    ]
    run_all.XE_JSON.write_text("[]", encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text(json.dumps(rows), encoding="utf-8")

    fresh, _stale, merged = asyncio.run(run_all._merge(network_enabled=False))
    assert len(fresh) == 2
    opportunity = next(row for row in fresh if row["neighborhood"] == "Ampelokipoi")
    assert opportunity["price_eur"] == 99_000.0
    assert sorted(opportunity["source_urls"]) == ["https://agency-a/1", "https://agency-b/1"]
    assert any("opportunity_merge" in record for record in merged)
    assert not run_all.OPPORTUNITY_REVIEW_JSON.exists()

    registry = run_all.assign_opportunity_ids(fresh, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    registry.commit()
    registry.close()
    assert all(row["opportunity_id"].startswith("opp_") for row in fresh)
    assert len({row["opportunity_id"] for row in fresh}) == 2


def test_merge_writes_review_file_and_fails_closed_on_bad_decisions(monkeypatch, merge_paths):
    _install_fake_fingerprints(monkeypatch)
    rows = [
        _listing("https://agency-a/1", ["p1", "p2", "p3"], neighborhood="Ampelokipoi"),
        # 40% apart: above the first pass's 25% tolerance, so only the second pass sees it.
        _listing("https://agency-b/1", ["p1", "p2", "p3"], neighborhood="Ampelokipoi", price_eur=140_000.0),
    ]
    run_all.XE_JSON.write_text("[]", encoding="utf-8")
    run_all.SPITOGATOS_JSON.write_text(json.dumps(rows), encoding="utf-8")
    fresh, _stale, _merged = asyncio.run(run_all._merge())
    review = json.loads(run_all.OPPORTUNITY_REVIEW_JSON.read_text(encoding="utf-8"))
    assert review and review[0]["reason"] == "price_gap"
    assert {row["duplicate_review_status"] for row in fresh} == {"pending"}

    run_all.OPPORTUNITY_DECISIONS_JSON.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        asyncio.run(run_all._merge())
