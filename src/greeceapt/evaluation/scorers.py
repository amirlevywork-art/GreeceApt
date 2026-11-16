"""
Candidate image scorers for the bake-off. A candidate = model × prompt strategy × resolution.

The central fix lives here. The old pipeline asked an LLM for a free-floating 1-10 number,
which snaps to a few training-frequent anchors (46% of images got exactly 5.0). P3 instead
asks for ~7 concrete, pixel-grounded observations and combines them by fixed weights, so the
score is continuous and reaches thousands of distinct values.

Physical condition and image usability are kept SEPARATE outputs: an empty flat can be freshly
renovated and a cluttered one immaculate, so clutter/darkness must not move the condition
score — they lower *confidence* instead. And ``unclear`` on an attribute EXCLUDES it from the
mean rather than scoring it 0.5, so a dark photo is not silently rated "average condition".
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import requests
from pydantic import BaseModel, Field, ValidationError

from greeceapt.vision.image_input import InvalidImageError, prepare_image
from greeceapt.vision.ollama import (
    PROMPT_VERSION,
    OllamaVisionScorer,
    VisionConfig,
)
from greeceapt.vision.types import SCHEMA_VERSION

logger = logging.getLogger(__name__)

OLLAMA = "http://localhost:11434"

Room = Literal["kitchen", "bathroom", "bedroom", "living", "hallway",
               "balcony", "exterior", "floor_plan", "other"]


class ImageVerdict(BaseModel):
    # physical condition only
    visible_damage: Literal["none", "minor", "major", "unclear"] = "unclear"
    wall_condition: Literal["fresh", "worn", "peeling_cracked", "unclear"] = "unclear"
    flooring_condition: Literal["modern", "dated_ok", "worn", "unclear"] = "unclear"
    kitchen_age: Literal["modern", "dated", "original", "na", "unclear"] = "na"
    bathroom_age: Literal["modern", "dated", "original", "na", "unclear"] = "na"
    windows_age: Literal["modern", "dated", "unclear"] = "unclear"
    renovation_signals: Literal["clear", "partial", "none", "unclear"] = "unclear"
    # image usability — NOT condition
    room: Room = "other"
    interior: bool = True
    clutter: Literal["empty", "normal", "heavy", "unclear"] = "unclear"
    image_quality: Literal["good", "dark", "blurry", "obstructed"] = "good"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # score on the "physical condition" attributes only; unclear ones drop out
    def condition_score(self) -> float:
        levels = {
            "visible_damage": {"none": 10, "minor": 6, "major": 2},
            "wall_condition": {"fresh": 10, "worn": 6, "peeling_cracked": 2},
            "flooring_condition": {"modern": 10, "dated_ok": 6, "worn": 3},
            "kitchen_age": {"modern": 10, "dated": 6, "original": 3},
            "bathroom_age": {"modern": 10, "dated": 6, "original": 3},
            "windows_age": {"modern": 9, "dated": 5},
            "renovation_signals": {"clear": 10, "partial": 6, "none": 4},
        }
        weights = {
            "visible_damage": 1.4, "wall_condition": 1.2, "flooring_condition": 1.0,
            "kitchen_age": 1.3, "bathroom_age": 1.3, "windows_age": 0.8,
            "renovation_signals": 1.2,
        }
        num = den = 0.0
        for attr, scale in levels.items():
            val = getattr(self, attr)
            if val in ("unclear", "na"):     # excluded, not scored 0.5
                continue
            num += weights[attr] * scale[val]
            den += weights[attr]
        if den == 0:
            return 5.0
        return max(1.0, min(10.0, num / den))


# Map the free text small VLMs actually emit onto the schema's enum values. qwen3-vl says
# "recent"/"not applicable"/"good" rather than "modern"/"na"/"fresh", and Ollama's
# schema-constrained decoding returns empty for this model, so normalise in Python instead.
_ENUM_SYNONYMS: dict[str, dict[str, str]] = {
    "visible_damage": {"no": "none", "nothing": "none", "slight": "minor", "some": "minor",
                       "significant": "major", "severe": "major", "heavy": "major"},
    "wall_condition": {"good": "fresh", "clean": "fresh", "painted": "fresh", "ok": "worn",
                       "fair": "worn", "cracked": "peeling_cracked", "peeling": "peeling_cracked",
                       "damaged": "peeling_cracked"},
    "flooring_condition": {"good": "modern", "new": "modern", "tiled": "dated_ok", "ok": "dated_ok",
                           "fair": "dated_ok", "old": "worn", "damaged": "worn"},
    "kitchen_age": {"new": "modern", "recent": "modern", "renovated": "modern", "old": "original",
                    "not applicable": "na", "n/a": "na", "none": "na", "not_applicable": "na"},
    "bathroom_age": {"new": "modern", "recent": "modern", "renovated": "modern", "old": "original",
                     "not applicable": "na", "n/a": "na", "none": "na", "not_applicable": "na"},
    "windows_age": {"new": "modern", "recent": "modern", "old": "dated", "aluminum": "dated",
                    "wooden": "dated", "pvc": "modern"},
    "renovation_signals": {"yes": "clear", "recent": "clear", "some": "partial", "no": "none"},
    "clutter": {"cluttered": "heavy", "messy": "heavy", "tidy": "normal", "clean": "normal",
                "vacant": "empty", "unfurnished": "empty"},
    "image_quality": {"clear": "good", "sharp": "good", "bright": "good", "low": "blurry",
                      "poor": "blurry", "dim": "dark"},
}
_ALLOWED = {f: set(getattr(ImageVerdict.model_fields[f].annotation, "__args__", ()))
            for f in ImageVerdict.model_fields if f not in ("interior", "confidence", "room")}
_ALLOWED["room"] = set(ImageVerdict.model_fields["room"].annotation.__args__)


def _normalize_verdict(obj: dict) -> dict:
    """Coerce a loosely-worded verdict dict into valid ImageVerdict enum values."""
    out: dict = {}
    for key, allowed in _ALLOWED.items():
        raw = obj.get(key)
        if raw is None:
            continue
        val = str(raw).strip().lower().replace(" ", "_") if key != "room" else str(raw).strip().lower()
        val_sp = str(raw).strip().lower()
        if val in allowed:
            out[key] = val
        elif val_sp in _ENUM_SYNONYMS.get(key, {}):
            out[key] = _ENUM_SYNONYMS[key][val_sp]
        elif val in _ENUM_SYNONYMS.get(key, {}):
            out[key] = _ENUM_SYNONYMS[key][val]
        # else: leave unset, pydantic default (usually "unclear") applies
    inter = obj.get("interior")
    if isinstance(inter, bool):
        out["interior"] = inter
    elif isinstance(inter, str):
        out["interior"] = inter.strip().lower() in ("true", "yes", "1")
    conf = obj.get("confidence")
    try:
        c = float(conf)
        out["confidence"] = max(0.0, min(1.0, c if c <= 1.0 else c / 100.0))
    except (TypeError, ValueError):
        if isinstance(conf, str) and conf.strip().lower() in ("high", "very high"):
            out["confidence"] = 0.9
        elif isinstance(conf, str) and conf.strip().lower() in ("low",):
            out["confidence"] = 0.3
    return out


@dataclass(frozen=True)
class ScorerConfig:
    model: str = "qwen3-vl:4b-instruct"
    strategy: str = "V2"  # V2 production contract; P0-P4 remain historical baselines
    resolution: int = 512

    @property
    def name(self) -> str:
        return f"{self.model}__{self.strategy}__{self.resolution}"

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION if self.strategy == "V2" else self.strategy

    @property
    def schema_version(self) -> str:
        return SCHEMA_VERSION if self.strategy == "V2" else "legacy"


_P3_PROMPT = """Inspect this apartment photo. Return a compact JSON object with exactly these keys:
visible_damage: none, minor, major, or unclear
wall_condition: fresh, worn, peeling_cracked, or unclear
flooring_condition: modern, dated_ok, worn, or unclear
kitchen_age: modern, dated, original, na, or unclear (na if not a kitchen)
bathroom_age: modern, dated, original, na, or unclear (na if not a bathroom)
renovation_signals: clear, partial, none, or unclear
room: kitchen, bathroom, bedroom, living, hallway, balcony, exterior, floor_plan, or other
Judge physical condition only; ignore furniture and tidiness. Use unclear when you cannot tell.
Reply with only the JSON object."""

_P2_CLASSES = {
    "derelict": 1.5, "severely_dated": 3.0, "dated": 4.5, "liveable_aged": 5.5,
    "maintained": 6.5, "recently_updated": 8.0, "fully_renovated": 9.5,
}
_P2_PROMPT = (
    "Classify this apartment room's physical condition as exactly one of: "
    + ", ".join(_P2_CLASSES) + ".\nReply with only the class name."
)

_P1_PROMPT = (
    "Rate this apartment room's physical condition from 1.0 to 10.0 and identify the room. "
    'Return JSON: {"score": <float>, "room": "<kitchen|bathroom|bedroom|living|hallway|'
    'balcony|exterior|floor_plan|other>"}. Reply with only the JSON.'
)


def _b64(path: str, resolution: int) -> str:
    import io
    from PIL import Image
    img = Image.open(path).convert("RGB")
    if max(img.size) > resolution:
        img.thumbnail((resolution, resolution), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def _generate(model: str, prompt: str, img_b64: str, *, fmt: str | None = None,
              num_predict: int = 200, timeout: float = 60.0) -> str:
    body = {"model": model, "prompt": prompt, "images": [img_b64], "stream": False,
            # qwen3-* default to a "thinking" pass that eats the whole token budget before
            # any answer is emitted (=> empty response). Disable it; models that don't think
            # ignore the flag. This was why the first P3 run scored 0 images.
            "think": False,
            "options": {"temperature": 0, "num_predict": num_predict}}
    if fmt:
        body["format"] = fmt
    r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json().get("response", "").strip()


def _extract_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None


def score_image_detailed(
    path: str,
    cfg: ScorerConfig,
    *,
    scorer: OllamaVisionScorer | None = None,
) -> dict:
    """Return one serializable prediction using the production V2 implementation."""
    if cfg.strategy != "V2":
        score, verdict = score_image(path, cfg)
        return {
            "score": score,
            "status": "accepted" if score is not None else "model_error",
            "room_type": verdict.room if verdict is not None else "other",
            "evidence_coverage": None,
            "effective_confidence": verdict.confidence if verdict is not None else 0.0,
            "latency_ms": 0.0,
            "attempts": 1,
            "evidence": verdict.model_dump() if verdict is not None else None,
            "model_tag": cfg.model,
            "prompt_version": cfg.prompt_version,
            "schema_version": cfg.schema_version,
            "resolution": cfg.resolution,
        }

    try:
        prepared = prepare_image(Path(path).read_bytes(), max_edge=cfg.resolution)
    except (OSError, InvalidImageError, ValueError) as exc:
        logger.warning("image load failed %s: %s", path, exc)
        return {
            "score": None,
            "status": "invalid_image",
            "room_type": "other",
            "evidence_coverage": 0.0,
            "effective_confidence": 0.0,
            "latency_ms": 0.0,
            "attempts": 0,
            "evidence": None,
            "error_category": "invalid_image",
            "model_tag": cfg.model,
            "prompt_version": cfg.prompt_version,
            "schema_version": cfg.schema_version,
            "resolution": cfg.resolution,
        }

    production_scorer = scorer or OllamaVisionScorer(
        VisionConfig(model=cfg.model, resolution=cfg.resolution)
    )
    inference = production_scorer.score(prepared.jpeg_bytes)
    verdict = inference.verdict
    return {
        "score": inference.score.condition_score,
        "status": inference.score.status,
        "room_type": inference.score.room_type,
        "evidence_coverage": inference.score.evidence_coverage,
        "effective_confidence": inference.score.effective_confidence,
        "latency_ms": sum(inference.attempt_latencies_ms),
        "attempts": inference.attempt_count,
        "evidence": verdict.model_dump() if verdict is not None else None,
        "raw_response_sha256": inference.raw_response_sha256,
        "error_category": inference.error_category,
        "diagnostic_excerpt": inference.diagnostic_excerpt,
        "model_tag": inference.model_tag,
        "prompt_version": inference.prompt_version,
        "schema_version": inference.schema_version,
        "resolution": cfg.resolution,
        "prepared_sha256": prepared.sha256,
        "prepared_width": prepared.width,
        "prepared_height": prepared.height,
    }


def score_image(path: str, cfg: ScorerConfig) -> tuple[float | None, ImageVerdict | None]:
    """Score one image. Returns (condition_score_1_10, verdict) or (None, None) on failure."""
    if cfg.strategy == "V2":
        prediction = score_image_detailed(path, cfg)
        return prediction["score"], None
    try:
        img = _b64(path, cfg.resolution)
    except Exception as exc:
        logger.warning("image load failed %s: %s", path, exc)
        return None, None

    try:
        if cfg.strategy in ("P3", "P4"):
            prompt = _P3_PROMPT
            if cfg.strategy == "P4":
                prompt += ("\n(Anchor: a gutted flat with cracked walls and original fittings "
                           "scores ~3; a fully re-fitted modern kitchen and bathroom ~8.)")
            # No schema-format: qwen3-vl returns empty under Ollama structured decoding.
            # Free JSON + generous budget (the model is verbose) + Python normalisation instead.
            raw = _generate(cfg.model, prompt, img, num_predict=220)
            obj = _extract_json(raw)
            if obj is None:
                return None, None
            try:
                verdict = ImageVerdict.model_validate(_normalize_verdict(obj))
            except ValidationError:
                return None, None
            return verdict.condition_score(), verdict

        if cfg.strategy == "P2":
            raw = _generate(cfg.model, _P2_PROMPT, img, num_predict=12).lower()
            for cls, val in _P2_CLASSES.items():
                if cls in raw:
                    return val, ImageVerdict(confidence=0.6)
            return None, None

        if cfg.strategy == "P1":
            raw = _generate(cfg.model, _P1_PROMPT, img, fmt="json")
            obj = _extract_json(raw) or {}
            try:
                score = max(1.0, min(10.0, float(obj.get("score"))))
            except (TypeError, ValueError):
                return None, None
            room = obj.get("room") if obj.get("room") in ImageVerdict.model_fields["room"].annotation.__args__ else "other"
            return score, ImageVerdict(room=room, confidence=0.6)

        if cfg.strategy == "P0":
            # incumbent 2-stage: caption then llama3 scores the caption
            desc = _generate(cfg.model, "Describe the condition of this room in one sentence.",
                             img, num_predict=80)
            if not desc:
                return None, None
            sc = _score_caption(desc)
            return sc, ImageVerdict(confidence=0.4)

    except requests.RequestException as exc:
        logger.warning("ollama error (%s): %s", cfg.name, exc)
        return None, None

    return None, None


def _score_caption(desc: str) -> float | None:
    prompt = (f'An apartment photo was described as: "{desc}"\n'
              'Return JSON {"score": <float 1.0-10.0>} rating its condition. Only JSON.')
    try:
        # llama3 is text-only; call without an image
        r = requests.post(f"{OLLAMA}/api/generate",
                          json={"model": "llama3", "prompt": prompt, "stream": False,
                                "format": "json", "options": {"temperature": 0.1, "num_predict": 40}},
                          timeout=60)
        obj = _extract_json(r.json().get("response", "")) or {}
        return max(1.0, min(10.0, float(obj.get("score"))))
    except Exception:
        return None
