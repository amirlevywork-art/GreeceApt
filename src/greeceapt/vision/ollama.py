"""Single-model Ollama adapter for structured apartment-condition evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from pydantic import ValidationError

from greeceapt.vision.scoring import score_verdict
from greeceapt.vision.types import SCHEMA_VERSION, ImageScore, ImageVerdict

PROMPT_VERSION = "condition-v2-p3"

EVIDENCE_PROMPT = """Inspect this apartment photo using visible evidence only.
Return one compact JSON object with exactly these keys and allowed values:
room_type: kitchen|bathroom|bedroom|living|hallway|balcony|exterior|floor_plan|other
is_interior: true|false
visible_damage: none|minor|major|unclear
wall_condition: fresh|worn|peeling_cracked|unclear
flooring_condition: modern|dated_ok|worn|unclear
kitchen_age: modern|dated|original|not_applicable|unclear
bathroom_age: modern|dated|original|not_applicable|unclear
windows_age: modern|dated|unclear
renovation_signals: clear|partial|none|unclear
image_quality: good|dark|blurry|obstructed|invalid
confidence: number from 0 to 1
Judge physical condition only. Ignore furniture, tidiness, and style.
For visible_damage, minor means isolated cosmetic holes, chips, or scuffs; major
means widespread or structural damage, major water damage, or unfinished systems.
For walls, fresh means evenly finished; worn includes stains, scuffs, or isolated
holes; peeling_cracked requires visible peeling or substantial cracking.
For floors and room age, modern means visibly recent; dated means older but intact;
worn means visibly damaged or degraded.
Treat unfinished work, exposed wiring, incomplete paint or plaster, missing fixtures,
and construction debris as visible damage and partial renovation, never clear
completed renovation. Use unclear when the photo does not support a conclusion.
JSON only."""

RETRY_PROMPT = EVIDENCE_PROMPT + "\nYour previous output was invalid. Return only the complete JSON object."


def _normalize_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    if not endpoint.lower().startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    return endpoint


@dataclass(frozen=True)
class VisionConfig:
    model: str = "qwen3-vl:4b-instruct"
    endpoint: str = ""
    resolution: int = 512
    timeout_s: float = 60.0
    num_predict: int = 128
    max_retries: int = 1

    def __post_init__(self) -> None:
        raw_endpoint = self.endpoint or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        endpoint = _normalize_endpoint(raw_endpoint)
        hostname = (urlparse(endpoint).hostname or "").lower()
        if hostname not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            raise ValueError("vision inference must use a local Ollama endpoint")
        object.__setattr__(self, "endpoint", endpoint)


@dataclass(frozen=True)
class VisionInference:
    verdict: ImageVerdict | None
    score: ImageScore
    attempt_count: int
    attempt_latencies_ms: tuple[float, ...]
    raw_response_sha256: str | None
    error_category: str | None
    diagnostic_excerpt: str | None
    model_tag: str
    prompt_version: str = PROMPT_VERSION
    schema_version: str = SCHEMA_VERSION
    attempt_errors: tuple[str | None, ...] = ()
    attempt_response_sha256: tuple[str | None, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.verdict is not None:
            data["verdict"] = self.verdict.model_dump()
        if self.score.verdict is not None:
            data["score"]["verdict"] = self.score.verdict.model_dump()
        data["attempt_latencies_ms"] = list(self.attempt_latencies_ms)
        return data


def _model_error_score() -> ImageScore:
    return ImageScore(
        condition_score=None,
        evidence_coverage=0.0,
        effective_confidence=0.0,
        status="model_error",
        room_type="other",
        verdict=None,
    )


def _extract_object(raw: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_object(obj: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(obj)
    if "room" in normalized and "room_type" not in normalized:
        normalized["room_type"] = normalized.pop("room")
    if "interior" in normalized and "is_interior" not in normalized:
        normalized["is_interior"] = normalized.pop("interior")
    for field in ("kitchen_age", "bathroom_age"):
        if normalized.get(field) in {"na", "n/a", "not applicable"}:
            normalized[field] = "not_applicable"
    room = normalized.get("room_type")
    if room == "living room":
        normalized["room_type"] = "living"
        room = "living"
    # Room-specific ages only count in their own room (vision/scoring._applicable_attributes),
    # so a stray value elsewhere (an open-plan kitchen in a living room, a bathroom age on a
    # hallway photo) is set to not_applicable instead of rejecting the whole photo. Lossless:
    # the score never reads these fields outside their room.
    if room != "kitchen" and normalized.get("kitchen_age") not in (None, "not_applicable", "unclear"):
        normalized["kitchen_age"] = "not_applicable"
    if room != "bathroom" and normalized.get("bathroom_age") not in (None, "not_applicable", "unclear"):
        normalized["bathroom_age"] = "not_applicable"
    # Unknown extra keys are dropped; a missing required field still fails validation.
    from greeceapt.vision.types import ImageVerdict

    return {key: value for key, value in normalized.items() if key in ImageVerdict.model_fields}


class OllamaVisionScorer:
    def __init__(
        self,
        config: VisionConfig | None = None,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or VisionConfig()
        self.session = session or requests.Session()

    def score(self, prepared_jpeg: bytes) -> VisionInference:
        image_b64 = base64.b64encode(prepared_jpeg).decode("ascii")
        latencies: list[float] = []
        attempt_errors: list[str | None] = []
        attempt_hashes: list[str | None] = []
        raw = ""
        last_error: str | None = None

        for attempt in range(self.config.max_retries + 1):
            prompt = EVIDENCE_PROMPT if attempt == 0 else RETRY_PROMPT
            started = time.perf_counter()
            try:
                response = self.session.post(
                    f"{self.config.endpoint}/api/generate",
                    json={
                        "model": self.config.model,
                        "prompt": prompt,
                        "images": [image_b64],
                        "stream": False,
                        "think": False,
                        "options": {
                            "temperature": 0,
                            "num_predict": self.config.num_predict,
                        },
                    },
                    timeout=self.config.timeout_s,
                )
                response.raise_for_status()
                raw = str(response.json().get("response", "")).strip()
                response_hash = hashlib.sha256(raw.encode()).hexdigest() if raw else None
                obj = _extract_object(raw)
                if obj is None:
                    last_error = "invalid_json"
                    attempt_errors.append(last_error)
                    attempt_hashes.append(response_hash)
                    continue
                try:
                    verdict = ImageVerdict.model_validate(_normalize_object(obj))
                except ValidationError:
                    last_error = "invalid_schema"
                    attempt_errors.append(last_error)
                    attempt_hashes.append(response_hash)
                    continue
                scored = score_verdict(verdict)
                elapsed_ms = (time.perf_counter() - started) * 1000
                return VisionInference(
                    verdict=verdict,
                    score=scored,
                    attempt_count=attempt + 1,
                    attempt_latencies_ms=tuple(latencies + [elapsed_ms]),
                    raw_response_sha256=response_hash,
                    error_category=None,
                    diagnostic_excerpt=None,
                    model_tag=self.config.model,
                    attempt_errors=tuple(attempt_errors + [None]),
                    attempt_response_sha256=tuple(attempt_hashes + [response_hash]),
                )
            except (requests.RequestException, ValueError, KeyError, TypeError):
                last_error = "transport_error"
                attempt_errors.append(last_error)
                attempt_hashes.append(None)
            finally:
                if len(latencies) == attempt:
                    latencies.append((time.perf_counter() - started) * 1000)

        return VisionInference(
            verdict=None,
            score=_model_error_score(),
            attempt_count=len(latencies),
            attempt_latencies_ms=tuple(latencies),
            raw_response_sha256=hashlib.sha256(raw.encode()).hexdigest() if raw else None,
            error_category=last_error,
            diagnostic_excerpt=raw[:240] if raw else None,
            model_tag=self.config.model,
            attempt_errors=tuple(attempt_errors),
            attempt_response_sha256=tuple(attempt_hashes),
        )
