"""Strict data contracts shared by production and vision evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "condition-v2"

RoomType = Literal[
    "kitchen",
    "bathroom",
    "bedroom",
    "living",
    "hallway",
    "balcony",
    "exterior",
    "floor_plan",
    "other",
]
ImageStatus = Literal[
    "accepted",
    "insufficient_evidence",
    "invalid_image",
    "model_error",
]


class ImageVerdict(BaseModel):
    """Observable image evidence; the model is not allowed to provide a score."""

    model_config = ConfigDict(extra="forbid")

    room_type: RoomType
    is_interior: bool
    visible_damage: Literal["none", "minor", "major", "unclear"]
    wall_condition: Literal["fresh", "worn", "peeling_cracked", "unclear"]
    flooring_condition: Literal["modern", "dated_ok", "worn", "unclear"]
    kitchen_age: Literal["modern", "dated", "original", "not_applicable", "unclear"]
    bathroom_age: Literal["modern", "dated", "original", "not_applicable", "unclear"]
    windows_age: Literal["modern", "dated", "unclear"]
    renovation_signals: Literal["clear", "partial", "none", "unclear"]
    image_quality: Literal["good", "dark", "blurry", "obstructed", "invalid"]
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_room_consistency(self) -> "ImageVerdict":
        if self.room_type in {"exterior", "floor_plan"} and self.is_interior:
            raise ValueError(f"{self.room_type} cannot be marked as interior")
        if self.room_type in {
            "kitchen", "bathroom", "bedroom", "living", "hallway"
        } and not self.is_interior:
            raise ValueError(f"{self.room_type} must be marked as interior")
        if self.room_type == "kitchen":
            if self.kitchen_age == "not_applicable":
                raise ValueError("kitchen_age must be applicable in a kitchen")
            if self.bathroom_age not in {"not_applicable", "unclear"}:
                raise ValueError("bathroom_age is not applicable outside a bathroom")
        elif self.room_type == "bathroom":
            if self.bathroom_age == "not_applicable":
                raise ValueError("bathroom_age must be applicable in a bathroom")
            if self.kitchen_age not in {"not_applicable", "unclear"}:
                raise ValueError("kitchen_age is not applicable outside a kitchen")
        elif (
            self.kitchen_age not in {"not_applicable", "unclear"}
            or self.bathroom_age not in {"not_applicable", "unclear"}
        ):
            raise ValueError("room-specific age fields must be not_applicable")
        return self


@dataclass(frozen=True)
class ImageScore:
    condition_score: float | None
    evidence_coverage: float
    effective_confidence: float
    status: ImageStatus
    room_type: RoomType
    verdict: ImageVerdict | None


@dataclass(frozen=True)
class ListingVisionResult:
    """Room-balanced listing aggregate plus bounded processing metadata."""

    condition_score: float | None
    effective_confidence: float
    status: ImageStatus
    image_scores: tuple[ImageScore, ...] = ()
    room_scores: dict[str, float] = field(default_factory=dict)
    accepted_count: int = 0
    rejected_count: int = 0
    retried_count: int = 0
    cache_hit_count: int = 0
    duplicate_mapping: dict[int, int] = field(default_factory=dict)
    source_urls: tuple[str, ...] = ()
    image_provenance: tuple[dict[str, Any], ...] = ()
