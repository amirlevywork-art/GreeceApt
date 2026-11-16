"""
ai_conductor.py — Manages the AI filter pipeline.

Full flow:
  Layer 0  Metadata Filter         (SQL only — neighborhood, age, photo count)
  Layer 1  Qwen Visual Audit       (Ollama, structured evidence per image, deterministic 1–10 score)
  Layer 2  Condition Gate          (removes accepted low-condition listings)

Listings that fail Layer 0 are moved to removed_listings before any inference.

``AI_DOWNLOAD_WORKERS`` controls parallel image downloads per listing (default 8).
The single production scorer is the local Qwen V2 service.
"""

from __future__ import annotations

import logging
import os
from collections import Counter, deque
from io import BytesIO

from greeceapt.storage import paths as app_paths
from greeceapt.logging_config import DEFAULT_FORMAT
from greeceapt.vision.types import ListingVisionResult

logger = logging.getLogger(__name__)

_LOG_FORMAT = logging.Formatter(DEFAULT_FORMAT)

# Log directory for Layer 1 file handler; tests monkeypatch ``ai_conductor.DATA_DIR``.
DATA_DIR = app_paths.DATA_DIR

DOWNLOAD_WORKERS = max(1, int(os.environ.get("AI_DOWNLOAD_WORKERS", "8")))
DOWNLOAD_OUTAGE_THRESHOLD = max(
    1, int(os.environ.get("VISION_DOWNLOAD_OUTAGE_THRESHOLD", "3"))
)


# Health guards. A run on 2026-11-02 spent 9.7 h producing 762/1,176 unusable results
# because nothing stopped it; these thresholds abort such a run within minutes.
HEALTH_MIN_SAMPLE = 20
HEALTH_WINDOW = 50
HEALTH_MAX_BAD_RATE = 0.25
HEALTH_BAD_STATUSES = frozenset({"insufficient_evidence", "model_error"})
SMOKE_PROBE_COUNT = 2


class Layer1HealthError(RuntimeError):
    """Layer 1 stopped because the model is unreachable or producing unusable output."""


class Layer1HealthMonitor:
    """Rolling bad-result rate over the most recent listings."""

    def __init__(
        self,
        *,
        min_sample: int = HEALTH_MIN_SAMPLE,
        window: int = HEALTH_WINDOW,
        max_bad_rate: float = HEALTH_MAX_BAD_RATE,
    ) -> None:
        self.min_sample = min_sample
        self.max_bad_rate = max_bad_rate
        self._statuses: deque[str] = deque(maxlen=window)
        self._errors: Counter[str] = Counter()

    def record(self, status: str, attempt_errors: tuple[str, ...] = ()) -> None:
        self._statuses.append(status)
        self._errors.update(error for error in attempt_errors if error)
        sample = len(self._statuses)
        if sample < self.min_sample:
            return
        bad = sum(1 for item in self._statuses if item in HEALTH_BAD_STATUSES)
        if bad / sample > self.max_bad_rate:
            common = ", ".join(f"{name}={count}" for name, count in self._errors.most_common(3))
            raise Layer1HealthError(
                f"Layer 1 health check failed: {bad}/{sample} recent listings were "
                f"insufficient_evidence or model_error (limit {self.max_bad_rate:.0%}); "
                f"most common attempt errors: {common or 'none recorded'}. "
                "Check that Ollama is running and the model/prompt still parse."
            )


def _model_ran(result: ListingVisionResult) -> bool:
    """False when every image verdict came from the cache: such a result says nothing about the
    live model (known no-evidence flats are retried every run and answer from the cache)."""
    images = result.accepted_count + result.rejected_count
    return not (result.cache_hit_count > 0 and result.cache_hit_count >= images)


def _attempt_errors(result: ListingVisionResult) -> tuple[str, ...]:
    errors: list[str] = []
    for image in result.image_provenance or ():
        errors.extend(error for error in image.get("attempt_errors") or () if error)
    return tuple(errors)


def _smoke_probe_images() -> list[bytes]:
    """Synthetic JPEGs: they test that the model answers in a parseable form, not its judgement."""
    from PIL import Image, ImageDraw

    probes: list[bytes] = []
    for index in range(SMOKE_PROBE_COUNT):
        image = Image.new("RGB", (512, 384), (235 - 40 * index, 228, 214))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 280, 511, 383), fill=(150, 110, 70))  # floor
        draw.rectangle((60 + 120 * index, 90, 220 + 120 * index, 230), fill=(170, 200, 230))  # window
        output = BytesIO()
        image.save(output, format="JPEG", quality=90)
        probes.append(output.getvalue())
    return probes


def smoke_check_scorer(scorer) -> None:
    """Fail before any listing is processed if the model cannot return a parseable verdict."""
    for index, probe in enumerate(_smoke_probe_images(), start=1):
        inference = scorer.score(probe)
        error = getattr(inference, "error_category", None)
        if error is not None:
            raise Layer1HealthError(
                f"Layer 1 smoke check failed on probe {index}/{SMOKE_PROBE_COUNT}: "
                f"model returned {error}. Check that Ollama is running and serving "
                f"{scorer.config.model}."
            )
    logger.info("Layer 1 smoke check passed (%s probes).", SMOKE_PROBE_COUNT)


# ── Top-first ordering ────────────────────────────────────────────────────────

def yield_upper_bound(row) -> float | None:
    """Within-neighborhood relative-yield upper bound with condition factor 1.0.

    area × floor factor / price; Qwen can only lower the condition factor.
    """
    from greeceapt.scoring.yield_model import floor_factor

    def number(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    price, area = number(row.get("price_eur")), number(row.get("area_sqm"))
    factor = floor_factor(row.get("floor_class"), number(row.get("floor")))
    if not price or price <= 0 or not area or area <= 0 or factor is None:
        return None
    return area * factor / price


def order_work_items(work_items, bounds: dict, neighborhoods: dict | None = None) -> list:
    """Round-robin over neighborhoods: every neighborhood's best candidate first, then its
    second best, … so each neighborhood's top 3 settles early. Unknown bounds go last."""
    neighborhoods = neighborhoods or {}
    by_hood: dict[str, list] = {}
    for item in work_items:
        by_hood.setdefault(str(neighborhoods.get(item[0]) or ""), []).append(item)
    position: dict[str, int] = {}
    for items in by_hood.values():
        items.sort(key=lambda item: (bounds.get(item[0]) is None, -(bounds.get(item[0]) or 0.0), str(item[0])))
        for rank, item in enumerate(items):
            position[item[0]] = rank
    return sorted(
        work_items,
        key=lambda item: (
            bounds.get(item[0]) is None,
            position[item[0]],
            -(bounds.get(item[0]) or 0.0),
            str(item[0]),
        ),
    )


def _vision_cache_path():
    from pathlib import Path

    configured = os.environ.get("VISION_CACHE_DB")
    return Path(configured) if configured else DATA_DIR / "vision_cache.sqlite3"


# ── Layer 1 orchestrator ──────────────────────────────────────────────────────

def run_layer1(
    *,
    service=None,
    interim_every: int = 0,
    on_interim=None,
) -> None:
    """Score all pending listings with Qwen V2 and promote accepted results."""
    from greeceapt.storage import db_conductor

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _log_path = DATA_DIR / "layer1.log"
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(_LOG_FORMAT)
    logging.getLogger().addHandler(_fh)

    try:
        _run_layer1_v2_impl(
            db_conductor, service=service, interim_every=interim_every, on_interim=on_interim
        )
    finally:
        logging.getLogger().removeHandler(_fh)
        _fh.close()

def _failed_listing_result() -> ListingVisionResult:
    return ListingVisionResult(condition_score=None, effective_confidence=0.0, status="model_error")


def _is_total_download_failure(result: ListingVisionResult) -> bool:
    provenance = result.image_provenance
    return bool(provenance) and all(
        image.get("error_category") == "download_error" for image in provenance
    )


def _run_layer1_v2_impl(db_conductor, *, service=None, interim_every: int = 0, on_interim=None) -> None:
    """Run the shared V2 service once per listing, without nested model concurrency."""
    from greeceapt.vision.cache import VisionCache
    from greeceapt.vision.ollama import PROMPT_VERSION, OllamaVisionScorer
    from greeceapt.vision.types import SCHEMA_VERSION
    from greeceapt.vision.service import SourceImage, VisionService

    owns_service = service is None
    cache = None
    if service is None:
        cache = VisionCache(_vision_cache_path())
        service = VisionService(scorer=OllamaVisionScorer(), cache=cache, download_workers=DOWNLOAD_WORKERS)

    if owns_service:
        smoke_check_scorer(service.scorer)

    model_version = service.scorer.config.model
    conn = db_conductor.connect_updated()
    try:
        promoted = db_conductor.promote_current_v2_results(
            conn,
            schema_version=SCHEMA_VERSION,
            model_version=model_version,
            prompt_version=PROMPT_VERSION,
        )
        logger.info("Layer 1 V2: promoted %s current compatible rows.", promoted)
        work_items = db_conductor.get_listings_for_layer1_v2(
            conn,
            schema_version=SCHEMA_VERSION,
            model_version=model_version,
            prompt_version=PROMPT_VERSION,
        )
        priorities = (
            db_conductor.get_layer1_priority_rows(conn)
            if hasattr(db_conductor, "get_layer1_priority_rows") else {}
        )
    finally:
        conn.close()
    if priorities:
        bounds = {listing_id: yield_upper_bound(row) for listing_id, row in priorities.items()}
        hoods = {listing_id: row.get("neighborhood") for listing_id, row in priorities.items()}
        work_items = order_work_items(work_items, bounds, hoods)
    logger.info("Layer 1 V2: %s listings to process (top-first by yield upper bound).", len(work_items))

    consecutive_download_failures = 0
    health = Layer1HealthMonitor()
    scored = 0
    for position, (listing_id, urls) in enumerate(work_items, start=1):
        sources = [
            SourceImage(source_url=url, original_index=index)
            for index, url in enumerate(urls)
        ]
        try:
            result = service.score_listing(sources)
        except Exception as exc:
            logger.exception("Layer 1 V2: id=%s worker failed — %s", listing_id, exc)
            result = _failed_listing_result()
        if _is_total_download_failure(result):
            consecutive_download_failures += 1
            logger.error(
                "Layer 1 V2: id=%s all %s image downloads failed (%s/%s consecutive); "
                "result left pending for retry. (%s/%s)",
                listing_id,
                len(result.image_provenance),
                consecutive_download_failures,
                DOWNLOAD_OUTAGE_THRESHOLD,
                position,
                len(work_items),
            )
            if consecutive_download_failures >= DOWNLOAD_OUTAGE_THRESHOLD:
                raise RuntimeError(
                    "Layer 1 download outage: consecutive listings lost every image"
                )
            continue
        consecutive_download_failures = 0
        conn = db_conductor.connect_updated()
        try:
            db_conductor.save_visual_audit_v2_results(
                conn,
                listing_id,
                result,
                promote=True,
                schema_version=SCHEMA_VERSION,
                model_version=model_version,
                prompt_version=PROMPT_VERSION,
            )
        finally:
            conn.close()
        logger.info(
            "Layer 1 V2: id=%-8s status=%s score=%s accepted=%s rejected=%s cache_hits=%s (%s/%s)",
            listing_id,
            result.status,
            f"{result.condition_score:.2f}" if result.condition_score is not None else "None",
            result.accepted_count,
            result.rejected_count,
            result.cache_hit_count,
            position,
            len(work_items),
        )
        if _model_ran(result):
            health.record(result.status, _attempt_errors(result))
        scored += 1
        if interim_every > 0 and on_interim is not None and scored % interim_every == 0:
            try:
                on_interim(scored)
            except Exception as exc:  # an interim ranking must never stop scoring
                logger.warning("Interim ranking after %s listings failed: %s", scored, exc)

    if owns_service:
        if cache is not None:
            cache.close()

# ── Top-level pipeline runner ─────────────────────────────────────────────────

def run(*, interim_every: int = 0, on_interim=None) -> None:
    logger.info("=== AI Conductor: Layer 0 — Metadata Filter ===")
    from greeceapt.photos.layer_0_cleaner import run as layer0
    layer0()

    logger.info("=== AI Conductor: Layer 1 — Qwen Visual Condition Audit ===")
    run_layer1(interim_every=interim_every, on_interim=on_interim)

    logger.info("=== AI Conductor: Layer 2 — Condition Quality Gate ===")
    from greeceapt.photos.layer_2_aesthetic_filter import run as layer2
    layer2()

    logger.info("AI pipeline complete.")


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    run()
