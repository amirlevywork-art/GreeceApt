"""Versioned SQLite cache contract tests."""

from dataclasses import replace

from greeceapt.vision.cache import CacheKey, VisionCache
from greeceapt.vision.ollama import VisionInference
from greeceapt.vision.scoring import score_verdict
from greeceapt.vision.types import ImageScore, ImageVerdict


def _inference(*, confidence: float = 0.8) -> VisionInference:
    verdict = ImageVerdict(
        room_type="living",
        is_interior=True,
        visible_damage="none",
        wall_condition="fresh",
        flooring_condition="modern",
        kitchen_age="not_applicable",
        bathroom_age="not_applicable",
        windows_age="modern",
        renovation_signals="clear",
        image_quality="good",
        confidence=confidence,
    )
    return VisionInference(
        verdict=verdict,
        score=score_verdict(verdict),
        attempt_count=1,
        attempt_latencies_ms=(123.4,),
        raw_response_sha256="a" * 64,
        error_category=None,
        diagnostic_excerpt=None,
        model_tag="qwen3-vl:4b-instruct",
    )


def _key() -> CacheKey:
    return CacheKey(
        image_sha256="b" * 64,
        model_tag="qwen3-vl:4b-instruct",
        prompt_version="condition-v2-p1",
        schema_version="condition-v2",
        resolution=512,
    )


def test_round_trip_fidelity_and_exact_key_hit(tmp_path):
    path = tmp_path / "vision.sqlite3"
    expected = _inference()
    with VisionCache(path) as cache:
        cache.put(_key(), expected)
        cache.commit()

    with VisionCache(path) as reopened:
        actual = reopened.get(_key())

    assert actual == expected


def test_each_version_field_change_is_a_cache_miss(tmp_path):
    cache = VisionCache(tmp_path / "vision.sqlite3")
    key = _key()
    cache.put(key, _inference())

    assert cache.get(replace(key, image_sha256="c" * 64)) is None
    assert cache.get(replace(key, model_tag="other-model")) is None
    assert cache.get(replace(key, prompt_version="condition-v2-p2")) is None
    assert cache.get(replace(key, schema_version="condition-v3")) is None
    assert cache.get(replace(key, resolution=768)) is None
    cache.close()


def test_invalid_and_model_error_results_are_not_cached(tmp_path):
    cache = VisionCache(tmp_path / "vision.sqlite3")
    accepted = _inference()
    invalid = replace(
        accepted,
        score=replace(accepted.score, condition_score=None, status="invalid_image"),
    )
    model_error = replace(
        accepted,
        verdict=None,
        score=ImageScore(
            condition_score=None,
            evidence_coverage=0.0,
            effective_confidence=0.0,
            status="model_error",
            room_type="other",
            verdict=None,
        ),
        error_category="invalid_json",
    )

    assert cache.put(_key(), invalid) is False
    assert cache.put(_key(), model_error) is False
    assert cache.get(_key()) is None
    cache.close()


def test_atomic_upsert_leaves_reopenable_database(tmp_path):
    path = tmp_path / "vision.sqlite3"
    key = _key()
    cache = VisionCache(path)
    cache.put(key, _inference(confidence=0.7))
    cache.commit()
    cache.put(key, _inference(confidence=0.95))
    cache.commit()
    cache.close()

    with VisionCache(path) as reopened:
        result = reopened.get(key)
        integrity = reopened.integrity_check()

    assert result is not None
    assert result.verdict is not None
    assert result.verdict.confidence == 0.95
    assert integrity == "ok"
