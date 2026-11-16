"""Cache-aware, no-cap listing vision service tests."""

import hashlib
from dataclasses import replace

import requests

from greeceapt.vision.image_input import InvalidImageError, PreparedImage
from greeceapt.vision.ollama import VisionConfig, VisionInference
from greeceapt.vision.scoring import score_verdict
from greeceapt.vision.service import SourceImage, VisionService, _download
from greeceapt.vision.types import ImageVerdict


def _prepared(content: bytes, *, max_edge: int = 512) -> PreparedImage:
    if content == b"corrupt":
        raise InvalidImageError("bad fixture")
    index = int(content.decode())
    digest = hashlib.sha256(content).hexdigest()
    return PreparedImage(
        jpeg_bytes=content,
        sha256=digest,
        phash=hashlib.sha256(f"phash-{index}".encode()).hexdigest()[:16],
        width=max_edge,
        height=max_edge - 1,
        original_width=1000,
        original_height=998,
    )


def _inference(room_type="living") -> VisionInference:
    verdict = ImageVerdict(
        room_type=room_type,
        is_interior=True,
        visible_damage="none",
        wall_condition="fresh",
        flooring_condition="modern",
        kitchen_age="not_applicable",
        bathroom_age="not_applicable",
        windows_age="modern",
        renovation_signals="clear",
        image_quality="good",
        confidence=0.9,
    )
    return VisionInference(
        verdict=verdict,
        score=score_verdict(verdict),
        attempt_count=1,
        attempt_latencies_ms=(10.0,),
        raw_response_sha256="f" * 64,
        error_category=None,
        diagnostic_excerpt=None,
        model_tag="qwen3-vl:4b-instruct",
    )


class _Scorer:
    def __init__(self, events=None):
        self.config = VisionConfig()
        self.calls = []
        self.events = events if events is not None else []

    def score(self, content):
        self.calls.append(content)
        self.events.append(("score", content))
        return _inference()


class _Cache:
    def __init__(self, values=None, events=None):
        self.values = dict(values or {})
        self.get_calls = []
        self.put_calls = []
        self.commits = 0
        self.events = events if events is not None else []

    def get(self, key):
        self.get_calls.append(key)
        return self.values.get(key.image_sha256)

    def put(self, key, result):
        self.put_calls.append((key, result))
        self.events.append(("put", key.image_sha256))
        self.values[key.image_sha256] = result
        return True

    def commit(self):
        self.commits += 1
        self.events.append(("commit", None))

    def rollback(self):
        self.events.append(("rollback", None))


def _sources(count):
    return [SourceImage(source_url=f"image://{i}", original_index=i) for i in range(count)]


def _downloader(url):
    return url.removeprefix("image://").encode()


def test_download_retries_a_transient_network_failure(monkeypatch):
    attempts = 0

    class Response:
        content = b"jpeg"

        @staticmethod
        def raise_for_status():
            return None

    def get(_url, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise requests.ConnectionError("temporary outage")
        return Response()

    monkeypatch.setattr("greeceapt.vision.service.requests.get", get)
    monkeypatch.setattr("greeceapt.vision.service.time.sleep", lambda _seconds: None)

    assert _download("https://images.example/photo.jpg") == b"jpeg"
    assert attempts == 3


def test_scores_all_30_distinct_images_without_a_cap(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)
    scorer = _Scorer()
    service = VisionService(scorer=scorer, cache=_Cache(), downloader=_downloader)

    result = service.score_listing(_sources(30))

    assert len(scorer.calls) == 30
    assert result.accepted_count == 30


def test_cached_input_bypasses_model(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)
    cached_hash = hashlib.sha256(b"0").hexdigest()
    scorer = _Scorer()
    service = VisionService(
        scorer=scorer,
        cache=_Cache({cached_hash: _inference()}),
        downloader=_downloader,
    )

    result = service.score_listing(_sources(2))

    assert scorer.calls == [b"1"]
    assert result.cache_hit_count == 1


def test_download_completion_does_not_change_original_order(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)
    sources = list(reversed(_sources(6)))
    scorer = _Scorer()
    service = VisionService(scorer=scorer, cache=_Cache(), downloader=_downloader)

    result = service.score_listing(sources)

    assert scorer.calls == [str(i).encode() for i in range(6)]
    assert result.source_urls == tuple(f"image://{i}" for i in range(6))


def test_corrupt_image_does_not_abort_listing(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)

    def downloader(url):
        return b"corrupt" if url == "bad" else b"1"

    scorer = _Scorer()
    service = VisionService(scorer=scorer, cache=_Cache(), downloader=downloader)
    result = service.score_listing(
        [SourceImage("bad", 0), SourceImage("good", 1)]
    )

    assert len(scorer.calls) == 1
    assert result.rejected_count == 1
    assert result.accepted_count == 1
    assert result.status == "accepted"


def test_cache_writes_happen_after_all_scoring_and_commit_once(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)
    events = []
    cache = _Cache(events=events)
    service = VisionService(
        scorer=_Scorer(events=events), cache=cache, downloader=_downloader
    )

    service.score_listing(_sources(3))

    event_names = [name for name, _ in events]
    assert event_names == ["score", "score", "score", "put", "put", "put", "commit"]


def test_perceptual_duplicates_are_inferred_once_and_mapped(monkeypatch):
    def same_visual(content, *, max_edge=512):
        prepared = _prepared(content, max_edge=max_edge)
        if content == b"1":
            return replace(prepared, phash=_prepared(b"0").phash)
        return prepared

    monkeypatch.setattr("greeceapt.vision.service.prepare_image", same_visual)
    scorer = _Scorer()
    service = VisionService(scorer=scorer, cache=_Cache(), downloader=_downloader)

    result = service.score_listing(_sources(3))

    assert scorer.calls == [b"0", b"2"]
    assert result.duplicate_mapping == {1: 0}


def test_result_keeps_reproducible_per_image_provenance(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)
    cached_hash = hashlib.sha256(b"0").hexdigest()
    service = VisionService(
        scorer=_Scorer(),
        cache=_Cache({cached_hash: _inference()}),
        downloader=_downloader,
    )

    result = service.score_listing(_sources(2))

    first, second = result.image_provenance
    assert first["original_index"] == 0
    assert first["source_url"] == "image://0"
    assert first["prepared_sha256"] == cached_hash
    assert first["prepared_width"] == 512
    assert first["cache_hit"] is True
    assert first["raw_response_sha256"] == "f" * 64
    assert first["prompt_version"] == "condition-v2-p3"
    assert first["attempt_latencies_ms"] == [10.0]
    assert first["download_latency_ms"] >= 0.0
    assert first["prepare_latency_ms"] >= 0.0
    assert second["original_index"] == 1
    assert second["cache_hit"] is False


def test_invalid_image_provenance_distinguishes_decode_failure(monkeypatch):
    monkeypatch.setattr("greeceapt.vision.service.prepare_image", _prepared)
    service = VisionService(
        scorer=_Scorer(), cache=_Cache(), downloader=lambda _url: b"corrupt"
    )

    result = service.score_listing([SourceImage("image://bad", 0)])

    assert result.image_provenance[0]["error_category"] == "decode_error"
    assert result.image_provenance[0]["download_prepare_latency_ms"] >= 0.0
