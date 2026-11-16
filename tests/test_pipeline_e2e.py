"""
End-to-end pipeline test: both portal JSONs → merge → ingest → AI → scoring.

Layer 1 is stubbed (no Ollama); merge pHash step avoids HTTP. Layer 2’s
``MIN_NEIGHBORHOOD_SIZE`` is lowered so two listings in one hood are not purged
before scoring. Layer 3 baseline computation is stubbed (real baselines require
min 10 listings per neighborhood); scoring then runs and writes ``final_deals.db``.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from greeceapt.photos import ai_conductor
from greeceapt.photos import layer_2_aesthetic_filter as layer2
from greeceapt.photos import layer_0_cleaner as layer0
from greeceapt.storage import core as db_core
from greeceapt.storage import db_conductor
from greeceapt.storage import insert_listings
from greeceapt.storage import ingest
from greeceapt.collect import run_all
from greeceapt.collect import util as scraper_util
from greeceapt.collect.fingerprints import fingerprint_image
from greeceapt.scoring import market_analytics
from greeceapt.scoring import scoring_conductor
from greeceapt.vision.cache import VisionCache
from greeceapt.vision.ollama import PROMPT_VERSION, VisionConfig, VisionInference
from greeceapt.vision.scoring import aggregate_listing, score_verdict
from greeceapt.vision.service import SourceImage, VisionService
from greeceapt.vision.types import ImageVerdict, ListingVisionResult


def _stub_neighborhood_baselines(*_args, **_kwargs):
    """Match ``compute_neighborhood_baselines`` return shape; Pagkrati anchor for e2e rows."""
    return ({"Pagkrati": 4500.0}, 0, 10)


def _listing(url: str, source: str, price_eur: int, *, area_sqm: float = 55.0) -> dict:
    """Minimal valid row for merge + Layer 0 (neighborhood, fresh date, ≥3 photo URLs)."""
    today = date.today().isoformat()
    suffix = url.rsplit("/", 1)[-1]
    return {
        "url": url,
        "title": f"e2e {source}",
        "price_eur": price_eur,
        "area_sqm": area_sqm,
        "floor": 2,
        "year_built": 2015,
        "energy_class": "B",
        "neighborhood": "Pagkrati",
        "municipality": "Athens",
        "latitude": 37.968,
        "longitude": 23.744,
        "publication_date": today,
        "scraped_at": f"{today}T12:00:00+00:00",
        "photos_count": 3,
        "photo_urls": [
            f"https://example.invalid/e2e/{suffix}/1.jpg",
            f"https://example.invalid/e2e/{suffix}/2.jpg",
            f"https://example.invalid/e2e/{suffix}/3.jpg",
        ],
        "source": source,
    }


@pytest.fixture
def pipeline_tmp(monkeypatch, tmp_path: Path):
    """Route DBs, merge I/O, and Layer 1 logs to tmp_path."""
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_core, "DB_PATH", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db_conductor, "INGESTED_DB", tmp_path / "ingested_listings.db")
    monkeypatch.setattr(db_conductor, "UPDATED_DB", tmp_path / "updated_listings.db")
    monkeypatch.setattr(db_conductor, "FINAL_DB", tmp_path / "final_deals.db")

    monkeypatch.setattr(run_all, "DATA_DIR", tmp_path)
    monkeypatch.setattr(run_all, "OUTPUT_JSON", tmp_path / "listings.json")
    monkeypatch.setattr(run_all, "DELETED_JSON", tmp_path / "deleted_listings.json")
    monkeypatch.setattr(run_all, "STALE_JSON", tmp_path / "stale_listings.json")
    monkeypatch.setattr(run_all, "XE_JSON", tmp_path / "xe_listings.json")
    monkeypatch.setattr(run_all, "SPITOGATOS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(scraper_util.paths, "PHASH_CACHE_JSON", tmp_path / "phash_cache.json")

    monkeypatch.setattr(ai_conductor, "DATA_DIR", tmp_path)

    monkeypatch.setattr(run_all, "OPPORTUNITY_REVIEW_JSON", tmp_path / "opportunity_review.json")
    monkeypatch.setattr(run_all, "OPPORTUNITY_DECISIONS_JSON", tmp_path / "opportunity_decisions.json")
    monkeypatch.setattr(run_all, "OPPORTUNITY_REGISTRY_DB", tmp_path / "opportunity_registry.sqlite3")
    monkeypatch.setattr(scoring_conductor, "OPPORTUNITY_DECISIONS_JSON", tmp_path / "opportunity_decisions.json")
    monkeypatch.setattr(scoring_conductor, "OPPORTUNITY_REGISTRY_DB", tmp_path / "opportunity_registry.sqlite3")
    monkeypatch.setattr(scoring_conductor, "load_rent_model", _StubRentModel)
    yield tmp_path


class _StubRentModel:
    """The rent model's interface with a flat €12/m²: the ranking needs a rent to rank anything."""
    meta: dict = {}
    alpha, k, metrics, lower_log_residual = 1.0, 10, {}, -0.1

    def predict_rent(self, listing, *, visual_override=None):
        rent = 12.0 * float(listing["area_sqm"])
        return rent, rent * 0.9


async def _fake_compute_listing_photo_fingerprints(
    photo_urls: list[str],
    url_cache: dict,
    sem,
    client,
) -> dict:
    """Deterministic v2 fingerprints without HTTP (merge dedupe stays per-listing)."""
    out = {}
    for u in photo_urls:
        if u in url_cache:
            out[u] = url_cache[u]
            continue
        rgb = (abs(hash(u)) % 220 + 20, (abs(hash(u)) >> 8) % 220, (abs(hash(u)) >> 16) % 220)
        im = Image.new("RGB", (32, 32), color=rgb)
        value = fingerprint_image(im)
        out[u] = value
        url_cache[u] = value
    return out


def _stub_run_layer1(*, service=None, **_kwargs) -> None:
    """Skip Ollama; assign a promoted passing V2 score to every Layer 1 work item."""
    conn = db_conductor.connect_updated()
    try:
        for lid, _urls in db_conductor.get_listings_for_layer1_v2(conn):
            db_conductor.save_visual_audit_v2_results(
                conn,
                lid,
                ListingVisionResult(
                    condition_score=8.0,
                    effective_confidence=0.9,
                    status="accepted",
                    accepted_count=3,
                ),
                promote=True,
            )
    finally:
        conn.close()


def test_merge_ingest_ai_scoring_full_pipeline(monkeypatch, pipeline_tmp: Path):
    monkeypatch.setattr(
        scraper_util,
        "compute_listing_photo_fingerprints",
        _fake_compute_listing_photo_fingerprints,
    )
    monkeypatch.setattr(ai_conductor, "run_layer1", _stub_run_layer1)
    monkeypatch.setattr(market_analytics, "compute_neighborhood_baselines", _stub_neighborhood_baselines)
    # Production MIN_NEIGHBORHOOD_SIZE=10 would purge tiny test hoods before scoring.
    monkeypatch.setattr(layer2, "MIN_NEIGHBORHOOD_SIZE", 1)

    xe_listing = _listing("https://www.xe.gr/property/e2e-xe-1", "xe", 195_000, area_sqm=55.0)
    # Distinct area vs XE so Quad-Lock does not merge if pHashes collide (same metadata + similar images).
    spit_listing = _listing(
        "https://www.spitogatos.gr/en/property/e2e-spit-1", "spitogatos", 210_000, area_sqm=72.0
    )

    pipeline_tmp.mkdir(parents=True, exist_ok=True)
    (pipeline_tmp / "xe_listings.json").write_text(
        json.dumps([xe_listing], ensure_ascii=False),
        encoding="utf-8",
    )
    (pipeline_tmp / "spitogatos_listings.json").write_text(
        json.dumps([spit_listing], ensure_ascii=False),
        encoding="utf-8",
    )

    fresh, stale, merged = asyncio.run(run_all._merge())
    assert len(fresh) == 2
    assert not stale

    with run_all.OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(fresh, f, indent=2, ensure_ascii=False)
    with run_all.DELETED_JSON.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    with run_all.STALE_JSON.open("w", encoding="utf-8") as f:
        json.dump(stale, f, indent=2, ensure_ascii=False)

    listings = ingest.load_json(str(run_all.OUTPUT_JSON))
    assert {row["source"] for row in listings} == {"xe", "spitogatos"}

    db_core.create_tables()
    insert_listings(listings)

    ing = sqlite3.connect(pipeline_tmp / "ingested_listings.db")
    try:
        assert ing.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
    finally:
        ing.close()

    ai_conductor.run()

    upd = sqlite3.connect(pipeline_tmp / "updated_listings.db")
    try:
        n = upd.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        assert n == 2
        scored = upd.execute(
            "SELECT COUNT(*) FROM listings "
            "WHERE visual_score_v2 IS NOT NULL AND visual_status_v2 = 'accepted'"
        ).fetchone()[0]
        assert scored == 2
    finally:
        upd.close()

    scoring_conductor.run()

    assert db_conductor.FINAL_DB.exists()
    fin = sqlite3.connect(pipeline_tmp / "final_deals.db")
    try:
        deal_n = fin.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        hood_n = fin.execute("SELECT COUNT(*) FROM neighborhoods").fetchone()[0]
        assert deal_n == 2
        assert hood_n >= 1
        urls = {row[0] for row in fin.execute("SELECT url FROM deals")}
        meta = dict(fin.execute("SELECT key, value_json FROM ranking_metadata").fetchall())
        assert json.loads(meta["vision_reliability"])["distribution"]["n"] == 2
        assert "https://www.xe.gr/property/e2e-xe-1" in urls
        assert "https://www.spitogatos.gr/en/property/e2e-spit-1" in urls
    finally:
        fin.close()


def test_v2_failed_listing_records_model_error_and_continues():
    saved = []

    class Connection:
        def commit(self):
            return None

        def close(self):
            return None

    class FakeDb:
        @staticmethod
        def connect_updated():
            return Connection()

        @staticmethod
        def promote_current_v2_results(conn, **kwargs):
            return 0

        @staticmethod
        def get_listings_for_layer1_v2(conn, **kwargs):
            return [("1", ["img://bad"]), ("2", ["img://good"])]

        @staticmethod
        def save_visual_audit_v2_results(conn, listing_id, result, **kwargs):
            saved.append((listing_id, result.status, kwargs["promote"]))

    accepted = aggregate_listing([_vision_inference().score])

    class Service:
        calls = 0
        scorer = type("Scorer", (), {"config": VisionConfig()})()

        def score_listing(self, sources):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("fixture failure")
            return accepted

    ai_conductor._run_layer1_v2_impl(FakeDb, service=Service())

    assert saved == [("1", "model_error", True), ("2", "accepted", True)]


def test_v2_download_outage_is_not_persisted_as_a_visual_result():
    saved = []

    class Connection:
        def commit(self):
            return None

        def close(self):
            return None

    class FakeDb:
        @staticmethod
        def connect_updated():
            return Connection()

        @staticmethod
        def promote_current_v2_results(conn, **kwargs):
            return 0

        @staticmethod
        def get_listings_for_layer1_v2(conn, **kwargs):
            return [(str(i), [f"https://images.example/{i}.jpg"]) for i in range(4)]

        @staticmethod
        def save_visual_audit_v2_results(conn, listing_id, result, **kwargs):
            saved.append((listing_id, result.status))

    download_failure = ListingVisionResult(
        condition_score=None,
        effective_confidence=0.0,
        status="insufficient_evidence",
        rejected_count=1,
        image_provenance=(
            {
                "source_url": "https://images.example/photo.jpg",
                "status": "invalid_image",
                "error_category": "download_error",
            },
        ),
    )

    class Service:
        scorer = type("Scorer", (), {"config": VisionConfig()})()

        def score_listing(self, sources):
            return download_failure

    with pytest.raises(RuntimeError, match="download outage"):
        ai_conductor._run_layer1_v2_impl(FakeDb, service=Service())

    assert saved == []


def test_v2_checkpoints_completed_listing_before_a_later_download_outage():
    saved = []

    class Connection:
        def commit(self):
            return None

        def close(self):
            return None

    class FakeDb:
        @staticmethod
        def connect_updated():
            return Connection()

        @staticmethod
        def promote_current_v2_results(conn, **kwargs):
            return 0

        @staticmethod
        def get_listings_for_layer1_v2(conn, **kwargs):
            return [(str(i), [f"https://images.example/{i}.jpg"]) for i in range(4)]

        @staticmethod
        def save_visual_audit_v2_results(conn, listing_id, result, **kwargs):
            saved.append((listing_id, result.status))

    accepted = ListingVisionResult(
        condition_score=8.0,
        effective_confidence=0.9,
        status="accepted",
        accepted_count=1,
    )
    download_failure = ListingVisionResult(
        condition_score=None,
        effective_confidence=0.0,
        status="insufficient_evidence",
        rejected_count=1,
        image_provenance=(
            {
                "source_url": "https://images.example/photo.jpg",
                "status": "invalid_image",
                "error_category": "download_error",
            },
        ),
    )

    class Service:
        scorer = type("Scorer", (), {"config": VisionConfig()})()
        calls = 0

        def score_listing(self, sources):
            self.calls += 1
            return accepted if self.calls == 1 else download_failure

    with pytest.raises(RuntimeError, match="download outage"):
        ai_conductor._run_layer1_v2_impl(FakeDb, service=Service())

    assert saved == [("0", "accepted")]


def test_layer1_uses_one_scoring_contract_for_selection_and_persistence():
    observed = {}

    class Connection:
        def close(self):
            return None

    class FakeDb:
        @staticmethod
        def connect_updated():
            return Connection()

        @staticmethod
        def promote_current_v2_results(conn, **kwargs):
            observed["promote"] = kwargs
            return 0

        @staticmethod
        def get_listings_for_layer1_v2(conn, **kwargs):
            observed["select"] = kwargs
            return [("1", ["img://one"])]

        @staticmethod
        def save_visual_audit_v2_results(conn, listing_id, result, **kwargs):
            observed["save"] = kwargs

    result = ListingVisionResult(
        condition_score=8.0,
        effective_confidence=0.9,
        status="accepted",
        accepted_count=1,
    )

    class Service:
        scorer = type("Scorer", (), {"config": VisionConfig()})()

        def score_listing(self, sources):
            return result

    ai_conductor._run_layer1_v2_impl(FakeDb, service=Service())

    for operation in ("promote", "select", "save"):
        assert observed[operation]["prompt_version"] == PROMPT_VERSION
        assert observed[operation]["model_version"] == VisionConfig().model
        assert "filter_mode" not in observed[operation]


def _fixture_jpeg(seed: int) -> bytes:
    rng = random.Random(seed)
    image = Image.frombytes("RGB", (64, 64), rng.randbytes(64 * 64 * 3))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _vision_inference(*, room="living", unknown=False) -> VisionInference:
    verdict = ImageVerdict(
        room_type=room,
        is_interior=True,
        visible_damage="unclear" if unknown else ("major" if room == "bathroom" else "none"),
        wall_condition="unclear" if unknown else ("worn" if room == "bathroom" else "fresh"),
        flooring_condition="unclear" if unknown else ("worn" if room == "bathroom" else "modern"),
        kitchen_age="not_applicable",
        bathroom_age="unclear" if unknown else ("original" if room == "bathroom" else "not_applicable"),
        windows_age="unclear" if unknown else ("dated" if room == "bathroom" else "modern"),
        renovation_signals="unclear" if unknown else ("none" if room == "bathroom" else "clear"),
        image_quality="good",
        confidence=0.9,
    )
    score = score_verdict(verdict)
    return VisionInference(
        verdict=verdict,
        score=score,
        attempt_count=1,
        attempt_latencies_ms=(5.0,),
        raw_response_sha256=str(room) * 4,
        error_category=None,
        diagnostic_excerpt=None,
        model_tag="qwen3-vl:4b-instruct",
    )


def test_qwen_cache_is_reused_by_a_new_service(tmp_path):
    payload = _fixture_jpeg(42)

    class Scorer:
        config = VisionConfig()

        def __init__(self):
            self.calls = []

        def score(self, content):
            self.calls.append(content)
            return _vision_inference()

    cache = VisionCache(tmp_path / "vision-cache.sqlite3")
    first_scorer = Scorer()
    VisionService(
        scorer=first_scorer,
        cache=cache,
        downloader=lambda _url: payload,
    ).score_listing([SourceImage("img://same", 0)])
    second_scorer = Scorer()
    result = VisionService(
        scorer=second_scorer,
        cache=cache,
        downloader=lambda _url: payload,
    ).score_listing([SourceImage("img://same", 0)])
    cache.close()

    assert len(first_scorer.calls) == 1
    assert second_scorer.calls == []
    assert result.cache_hit_count == 1


def test_v2_fake_model_pipeline_dedupes_aggregates_and_quarantines_unknown(
    monkeypatch, pipeline_tmp: Path
):
    accepted = _listing("https://xe.gr/v2/accepted", "xe", 190_000)
    unknown = _listing("https://xe.gr/v2/unknown", "xe", 180_000)
    accepted["photo_urls"] = ["img://a1", "img://a1-copy", "img://a2", "img://a3"]
    unknown["photo_urls"] = ["img://b1", "img://b2", "img://b3"]
    db_core.insert_listings([accepted, unknown])
    layer0.run()

    payloads = {
        "img://a1": _fixture_jpeg(1),
        "img://a1-copy": _fixture_jpeg(1),
        "img://a2": _fixture_jpeg(2),
        "img://a3": _fixture_jpeg(6),
        "img://b1": _fixture_jpeg(3),
        "img://b2": _fixture_jpeg(4),
        "img://b3": _fixture_jpeg(5),
    }
    outputs = [
        _vision_inference(room="living"),
        _vision_inference(room="bathroom"),
        _vision_inference(room="bedroom"),
        _vision_inference(unknown=True),
        _vision_inference(unknown=True),
        _vision_inference(unknown=True),
    ]

    class FakeScorer:
        config = VisionConfig()

        def __init__(self):
            self.calls = []

        def score(self, content):
            self.calls.append(content)
            return outputs[len(self.calls) - 1]

    scorer = FakeScorer()
    cache = VisionCache(pipeline_tmp / "vision-cache.sqlite3")
    service = VisionService(
        scorer=scorer,
        cache=cache,
        downloader=payloads.__getitem__,
        download_workers=3,
    )
    monkeypatch.setenv("VISION_SCORER_VERSION", "v1")
    ai_conductor.run_layer1(service=service)
    cache.close()

    expected = aggregate_listing([outputs[0].score, outputs[1].score, outputs[2].score])
    conn = db_conductor.connect_updated()
    rows = conn.execute(
        "SELECT url, visual_score_v2, visual_status_v2 FROM listings ORDER BY url"
    ).fetchall()
    conn.close()
    assert len(scorer.calls) == 6  # the duplicate photo is scored once
    assert rows[0][1] == pytest.approx(expected.condition_score)
    assert rows[0][2] == "accepted"
    assert rows[1][1] is None
    assert rows[1][2] == "insufficient_evidence"

    monkeypatch.setattr(layer2, "MIN_NEIGHBORHOOD_SIZE", 1)
    layer2.run()
    monkeypatch.setattr(market_analytics, "compute_neighborhood_baselines", _stub_neighborhood_baselines)
    scoring_conductor.run()

    conn = db_conductor.connect_updated()
    assert conn.execute(
        "SELECT COUNT(*) FROM listings WHERE visual_status_v2 = 'insufficient_evidence'"
    ).fetchone()[0] == 1
    conn.close()
    fin = sqlite3.connect(db_conductor.FINAL_DB)
    urls = {row[0] for row in fin.execute("SELECT url FROM deals")}
    fin.close()
    assert urls == {"https://xe.gr/v2/accepted"}
