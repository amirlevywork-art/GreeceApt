"""Contract tests for the single-model Ollama vision adapter."""

import json
from pathlib import Path

import pytest
import requests

from greeceapt.vision.ollama import OllamaVisionScorer, VisionConfig


def _valid_payload(**overrides) -> dict:
    payload = {
        "room_type": "living",
        "is_interior": True,
        "visible_damage": "none",
        "wall_condition": "fresh",
        "flooring_condition": "modern",
        "kitchen_age": "not_applicable",
        "bathroom_age": "not_applicable",
        "windows_age": "modern",
        "renovation_signals": "clear",
        "image_quality": "good",
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


class _Response:
    def __init__(self, text: str):
        self._text = text

    def raise_for_status(self):
        return None

    def json(self):
        return {"response": self._text}


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)


def test_valid_json_scores_in_one_attempt():
    session = _Session(json.dumps(_valid_payload()))

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.attempt_count == 1
    assert result.score.status == "accepted"
    assert result.verdict is not None
    assert result.error_category is None
    assert len(result.attempt_latencies_ms) == 1


def test_invalid_json_retries_once_with_same_model():
    session = _Session("not json", json.dumps(_valid_payload()))

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.attempt_count == 2
    assert result.score.status == "accepted"
    assert len(session.requests) == 2
    assert session.requests[0][1]["json"]["model"] == "qwen3-vl:4b-instruct"
    assert session.requests[1][1]["json"]["model"] == "qwen3-vl:4b-instruct"
    assert result.attempt_errors == ("invalid_json", None)
    assert result.attempt_response_sha256[0] is not None
    assert result.attempt_response_sha256[1] == result.raw_response_sha256


def test_json_wrapped_in_free_text_is_rejected_as_non_strict_output():
    wrapped = "Here is the result: " + json.dumps(_valid_payload())
    session = _Session(wrapped, wrapped)

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.score.status == "model_error"
    assert result.error_category == "invalid_json"


def test_second_invalid_response_returns_model_error():
    session = _Session("not json", "still not json")

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.attempt_count == 2
    assert result.verdict is None
    assert result.score.status == "model_error"
    assert result.score.condition_score is None
    assert result.error_category == "invalid_json"


def test_low_evidence_valid_json_is_not_retried():
    payload = _valid_payload(
        visible_damage="unclear",
        wall_condition="unclear",
        flooring_condition="unclear",
        windows_age="unclear",
        renovation_signals="unclear",
    )
    session = _Session(json.dumps(payload))

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.attempt_count == 1
    assert result.score.status == "insufficient_evidence"
    assert len(session.requests) == 1


def test_unknown_enum_is_rejected_not_coerced_positive():
    session = _Session(
        json.dumps(_valid_payload(wall_condition="beautiful")),
        json.dumps(_valid_payload(wall_condition="beautiful")),
    )

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.score.status == "model_error"
    assert result.error_category == "invalid_schema"


def test_transport_error_retries_once():
    session = _Session(
        requests.ConnectionError("offline"),
        json.dumps(_valid_payload()),
    )

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.attempt_count == 2
    assert result.score.status == "accepted"


def test_request_disables_thinking_and_temperature():
    session = _Session(json.dumps(_valid_payload()))

    OllamaVisionScorer(session=session).score(b"jpeg")

    body = session.requests[0][1]["json"]
    assert body["think"] is False
    assert body["options"] == {"temperature": 0, "num_predict": 128}
    assert "images" in body and body["images"]
    assert "score" not in body["prompt"].lower()


def test_prompt_treats_unfinished_renovation_as_damage_not_completed_work():
    session = _Session(json.dumps(_valid_payload()))

    OllamaVisionScorer(session=session).score(b"jpeg")

    prompt = session.requests[0][1]["json"]["prompt"].lower()
    assert "unfinished" in prompt
    assert "exposed wiring" in prompt
    assert "never clear" in prompt


def test_prompt_anchors_damage_and_age_categories_to_visible_severity():
    session = _Session(json.dumps(_valid_payload()))

    OllamaVisionScorer(session=session).score(b"jpeg")

    prompt = session.requests[0][1]["json"]["prompt"].lower()
    assert "isolated cosmetic" in prompt
    assert "widespread or structural" in prompt
    assert "older but intact" in prompt


def test_ollama_host_without_scheme_is_normalized():
    config = VisionConfig(endpoint="localhost:11434/")
    session = _Session(json.dumps(_valid_payload()))

    OllamaVisionScorer(config=config, session=session).score(b"jpeg")

    assert session.requests[0][0] == "http://localhost:11434/api/generate"


def test_remote_ollama_endpoint_is_rejected_for_local_only_pipeline():
    with pytest.raises(ValueError, match="local"):
        VisionConfig(endpoint="https://example.com:11434")


@pytest.mark.parametrize(
    "payload",
    [
        _valid_payload(room_type="exterior", is_interior=True),
        _valid_payload(room_type="bedroom", is_interior=False),
        # A stray kitchen/bathroom age outside its room is no longer rejected: it is set to
        # not_applicable (see test_inapplicable_room_age_is_dropped_without_retry).
    ],
)
def test_inconsistent_room_and_applicability_are_rejected(payload):
    session = _Session(json.dumps(payload), json.dumps(payload))

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.score.status == "model_error"
    assert result.error_category == "invalid_schema"


@pytest.mark.live
def test_qwen_v2_scores_fixed_fixture_under_contract():
    golden = json.loads(Path("data/vision_eval/golden.json").read_text())
    image = Path("data/vision_eval/images") / f"{golden[0]['sha256']}.jpg"

    result = OllamaVisionScorer().score(image.read_bytes())

    assert result.verdict is not None
    assert result.score.status in {"accepted", "insufficient_evidence", "invalid_image"}
    assert result.score.condition_score != 5.0 or result.verdict is not None


# ── Lossless recovery of inapplicable room-specific ages (speed plan B3) ──────

@pytest.mark.parametrize(
    ("room", "field"),
    [("living", "kitchen_age"), ("hallway", "bathroom_age"), ("other", "kitchen_age"),
     ("bathroom", "kitchen_age"), ("kitchen", "bathroom_age")],
)
def test_inapplicable_room_age_is_dropped_without_retry(room, field):
    overrides = {"room_type": room, field: "dated"}
    if room == "kitchen":
        overrides["kitchen_age"] = "modern"
    if room == "bathroom":
        overrides["bathroom_age"] = "modern"
    session = _Session(json.dumps(_valid_payload(**overrides)))

    result = OllamaVisionScorer(session=session).score(b"jpeg")

    assert result.attempt_count == 1 and result.error_category is None
    assert getattr(result.verdict, field) == "not_applicable"


def test_dropping_inapplicable_ages_never_changes_a_score():
    from greeceapt.vision.scoring import score_verdict
    from greeceapt.vision.types import ImageVerdict

    clean = ImageVerdict.model_validate(_valid_payload())
    stray = OllamaVisionScorer(session=_Session(json.dumps(_valid_payload(kitchen_age="original")))).score(b"jpeg")
    assert stray.score.condition_score == score_verdict(clean).condition_score


def test_unknown_extra_key_is_dropped_when_all_fields_are_present():
    payload = _valid_payload()
    payload["original"] = "not_applicable"
    result = OllamaVisionScorer(session=_Session(json.dumps(payload))).score(b"jpeg")
    assert result.attempt_count == 1 and result.error_category is None


def test_missing_required_field_still_fails():
    payload = _valid_payload()
    del payload["windows_age"]
    payload["original"] = "not_applicable"
    result = OllamaVisionScorer(session=_Session(json.dumps(payload), json.dumps(payload))).score(b"jpeg")
    assert result.error_category == "invalid_schema"
