"""Cache-aware orchestration for scoring every distinct listing image."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Callable, Sequence

import requests

from greeceapt.vision.cache import CacheKey, VisionCache
from greeceapt.vision.image_input import (
    InvalidImageError,
    PreparedImage,
    dedupe_prepared,
    prepare_image,
)
from greeceapt.vision.ollama import (
    PROMPT_VERSION,
    OllamaVisionScorer,
    VisionInference,
)
from greeceapt.vision.scoring import aggregate_listing
from greeceapt.vision.types import SCHEMA_VERSION, ImageScore, ListingVisionResult


@dataclass(frozen=True)
class SourceImage:
    source_url: str
    original_index: int


def _download(url: str) -> bytes:
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(0.5 * (2**attempt))
    raise AssertionError("unreachable")


def _invalid_score() -> ImageScore:
    return ImageScore(
        condition_score=None,
        evidence_coverage=0.0,
        effective_confidence=0.0,
        status="invalid_image",
        room_type="other",
        verdict=None,
    )


def _unexpected_model_error(model_tag: str) -> VisionInference:
    return VisionInference(
        verdict=None,
        score=ImageScore(
            condition_score=None,
            evidence_coverage=0.0,
            effective_confidence=0.0,
            status="model_error",
            room_type="other",
            verdict=None,
        ),
        attempt_count=1,
        attempt_latencies_ms=(),
        raw_response_sha256=None,
        error_category="unexpected_error",
        diagnostic_excerpt=None,
        model_tag=model_tag,
    )


class VisionService:
    def __init__(
        self,
        *,
        scorer: OllamaVisionScorer,
        cache: VisionCache,
        downloader: Callable[[str], bytes] = _download,
        download_workers: int = 8,
        inference_workers: int | None = None,
    ) -> None:
        self.scorer = scorer
        self.cache = cache
        self.downloader = downloader
        self.download_workers = max(1, download_workers)
        configured_workers = inference_workers
        if configured_workers is None:
            try:
                configured_workers = int(os.environ.get("VISION_INFERENCE_WORKERS", "1"))
            except ValueError:
                configured_workers = 1
        self.inference_workers = max(1, configured_workers)

    def _download_and_prepare(
        self, source: SourceImage
    ) -> tuple[PreparedImage, float, float]:
        download_started = time.perf_counter()
        content = self.downloader(source.source_url)
        download_ms = (time.perf_counter() - download_started) * 1000
        prepare_started = time.perf_counter()
        prepared = prepare_image(content, max_edge=self.scorer.config.resolution)
        prepare_ms = (time.perf_counter() - prepare_started) * 1000
        return prepared, download_ms, prepare_ms

    def _safe_score(self, prepared: PreparedImage) -> VisionInference:
        try:
            return self.scorer.score(prepared.jpeg_bytes)
        except Exception:
            return _unexpected_model_error(self.scorer.config.model)

    def _cache_key(self, prepared: PreparedImage) -> CacheKey:
        return CacheKey(
            image_sha256=prepared.sha256,
            model_tag=self.scorer.config.model,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            resolution=self.scorer.config.resolution,
        )

    def score_listing(self, images: Sequence[SourceImage]) -> ListingVisionResult:
        ordered = sorted(images, key=lambda image: image.original_index)
        if len({image.original_index for image in ordered}) != len(ordered):
            raise ValueError("source image original_index values must be unique")

        prepared_by_index: dict[int, PreparedImage] = {}
        stage_timings_by_index: dict[int, tuple[float, float]] = {}
        invalid_by_index: dict[int, ImageScore] = {}
        failure_by_index: dict[int, tuple[str, float]] = {}
        with ThreadPoolExecutor(max_workers=self.download_workers) as executor:
            futures = {
                executor.submit(self._download_and_prepare, source): source
                for source in ordered
            }
            submitted_at = {future: time.perf_counter() for future in futures}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    prepared, download_ms, prepare_ms = future.result()
                    prepared_by_index[source.original_index] = prepared
                    stage_timings_by_index[source.original_index] = (
                        download_ms,
                        prepare_ms,
                    )
                except requests.RequestException:
                    invalid_by_index[source.original_index] = _invalid_score()
                    failure_by_index[source.original_index] = (
                        "download_error",
                        (time.perf_counter() - submitted_at[future]) * 1000,
                    )
                except InvalidImageError:
                    invalid_by_index[source.original_index] = _invalid_score()
                    failure_by_index[source.original_index] = (
                        "decode_error",
                        (time.perf_counter() - submitted_at[future]) * 1000,
                    )
                except (OSError, ValueError):
                    invalid_by_index[source.original_index] = _invalid_score()
                    failure_by_index[source.original_index] = (
                        "prepare_error",
                        (time.perf_counter() - submitted_at[future]) * 1000,
                    )

        valid_sources = [
            source for source in ordered if source.original_index in prepared_by_index
        ]
        prepared_in_order = [
            prepared_by_index[source.original_index] for source in valid_sources
        ]
        unique_prepared, duplicate_positions = dedupe_prepared(prepared_in_order)
        duplicate_mapping = {
            valid_sources[duplicate_position].original_index:
            valid_sources[first_position].original_index
            for duplicate_position, first_position in duplicate_positions.items()
        }
        duplicate_original_indices = set(duplicate_mapping)
        unique_pairs = [
            (source, prepared)
            for source, prepared in zip(valid_sources, prepared_in_order, strict=True)
            if source.original_index not in duplicate_original_indices
        ]
        assert [prepared for _, prepared in unique_pairs] == unique_prepared

        inference_by_index: dict[int, VisionInference] = {}
        uncached_pairs: list[tuple[SourceImage, PreparedImage]] = []
        cache_hit_count = 0
        cache_hit_indices: set[int] = set()
        for source, prepared in unique_pairs:
            cached = self.cache.get(self._cache_key(prepared))
            if cached is None:
                uncached_pairs.append((source, prepared))
            else:
                inference_by_index[source.original_index] = cached
                cache_hit_count += 1
                cache_hit_indices.add(source.original_index)

        if uncached_pairs:
            with ThreadPoolExecutor(max_workers=self.inference_workers) as executor:
                new_results = list(
                    executor.map(
                        self._safe_score,
                        [prepared for _, prepared in uncached_pairs],
                    )
                )
            for (source, _), inference in zip(
                uncached_pairs, new_results, strict=True
            ):
                inference_by_index[source.original_index] = inference

            try:
                for (_, prepared), inference in zip(
                    uncached_pairs, new_results, strict=True
                ):
                    self.cache.put(self._cache_key(prepared), inference)
                self.cache.commit()
            except Exception:
                self.cache.rollback()
                raise

        scored_by_index = {
            index: inference.score for index, inference in inference_by_index.items()
        }
        scored_by_index.update(invalid_by_index)
        distinct_scores = tuple(
            scored_by_index[index]
            for index in sorted(scored_by_index)
        )
        aggregate = aggregate_listing(distinct_scores)
        retried_count = sum(
            inference.attempt_count > 1 for inference in inference_by_index.values()
        )
        provenance: list[dict[str, object]] = []
        for source in ordered:
            index = source.original_index
            record: dict[str, object] = {
                "original_index": index,
                "source_url": source.source_url,
            }
            prepared = prepared_by_index.get(index)
            if prepared is None:
                error_category, elapsed_ms = failure_by_index.get(
                    index, ("download_or_prepare_error", 0.0)
                )
                record.update(
                    {
                        "status": "invalid_image",
                        "error_category": error_category,
                        "download_prepare_latency_ms": elapsed_ms,
                    }
                )
                provenance.append(record)
                continue
            download_ms, prepare_ms = stage_timings_by_index[index]
            record.update(
                {
                    "prepared_sha256": prepared.sha256,
                    "prepared_phash": prepared.phash,
                    "prepared_width": prepared.width,
                    "prepared_height": prepared.height,
                    "original_width": prepared.original_width,
                    "original_height": prepared.original_height,
                    "resolution": self.scorer.config.resolution,
                    "download_latency_ms": download_ms,
                    "prepare_latency_ms": prepare_ms,
                }
            )
            if index in duplicate_mapping:
                record.update(
                    {
                        "status": "duplicate",
                        "duplicate_of": duplicate_mapping[index],
                        "cache_hit": False,
                    }
                )
                provenance.append(record)
                continue
            inference = inference_by_index[index]
            record.update(
                {
                    "status": inference.score.status,
                    "cache_hit": index in cache_hit_indices,
                    "model_tag": inference.model_tag,
                    "prompt_version": inference.prompt_version,
                    "schema_version": inference.schema_version,
                    "attempt_count": inference.attempt_count,
                    "attempt_latencies_ms": list(inference.attempt_latencies_ms),
                    "attempt_errors": list(inference.attempt_errors),
                    "attempt_response_sha256": list(
                        inference.attempt_response_sha256
                    ),
                    "raw_response_sha256": inference.raw_response_sha256,
                    "error_category": inference.error_category,
                    "diagnostic_excerpt": inference.diagnostic_excerpt,
                }
            )
            provenance.append(record)
        return replace(
            aggregate,
            retried_count=retried_count,
            cache_hit_count=cache_hit_count,
            duplicate_mapping=duplicate_mapping,
            source_urls=tuple(source.source_url for source in ordered),
            image_provenance=tuple(provenance),
        )
