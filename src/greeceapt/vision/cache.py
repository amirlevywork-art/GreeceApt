"""Versioned SQLite cache for valid local vision inference results."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from greeceapt.vision.ollama import VisionInference
from greeceapt.vision.types import ImageScore, ImageVerdict


@dataclass(frozen=True)
class CacheKey:
    image_sha256: str
    model_tag: str
    prompt_version: str
    schema_version: str
    resolution: int


class VisionCache:
    """A small explicit-transaction cache keyed by all scoring versions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vision_results (
                image_sha256 TEXT NOT NULL,
                model_tag TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                resolution INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (
                    image_sha256,
                    model_tag,
                    prompt_version,
                    schema_version,
                    resolution
                )
            )
            """
        )
        self._connection.commit()

    @staticmethod
    def _key_values(key: CacheKey) -> tuple[str, str, str, str, int]:
        return (
            key.image_sha256,
            key.model_tag,
            key.prompt_version,
            key.schema_version,
            key.resolution,
        )

    @staticmethod
    def _deserialize(payload: str) -> VisionInference:
        data = json.loads(payload)
        verdict_data = data.get("verdict")
        verdict = ImageVerdict.model_validate(verdict_data) if verdict_data else None
        score_data = data["score"]
        score_verdict_data = score_data.get("verdict")
        score_verdict = (
            ImageVerdict.model_validate(score_verdict_data)
            if score_verdict_data
            else None
        )
        score = ImageScore(
            condition_score=score_data["condition_score"],
            evidence_coverage=score_data["evidence_coverage"],
            effective_confidence=score_data["effective_confidence"],
            status=score_data["status"],
            room_type=score_data["room_type"],
            verdict=score_verdict,
        )
        return VisionInference(
            verdict=verdict,
            score=score,
            attempt_count=data["attempt_count"],
            attempt_latencies_ms=tuple(data["attempt_latencies_ms"]),
            raw_response_sha256=data.get("raw_response_sha256"),
            error_category=data.get("error_category"),
            diagnostic_excerpt=data.get("diagnostic_excerpt"),
            model_tag=data["model_tag"],
            prompt_version=data["prompt_version"],
            schema_version=data["schema_version"],
            attempt_errors=tuple(data.get("attempt_errors", ())),
            attempt_response_sha256=tuple(
                data.get("attempt_response_sha256", ())
            ),
        )

    def get(self, key: CacheKey) -> VisionInference | None:
        row = self._connection.execute(
            """
            SELECT result_json
            FROM vision_results
            WHERE image_sha256 = ?
              AND model_tag = ?
              AND prompt_version = ?
              AND schema_version = ?
              AND resolution = ?
            """,
            self._key_values(key),
        ).fetchone()
        if row is None:
            return None
        try:
            return self._deserialize(row[0])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def put(self, key: CacheKey, result: VisionInference) -> bool:
        """Stage a cacheable result; caller commits once the listing completes."""
        if result.verdict is None or result.score.status not in {
            "accepted",
            "insufficient_evidence",
        }:
            return False
        payload = json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        self._connection.execute(
            """
            INSERT INTO vision_results (
                image_sha256,
                model_tag,
                prompt_version,
                schema_version,
                resolution,
                result_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                image_sha256,
                model_tag,
                prompt_version,
                schema_version,
                resolution
            ) DO UPDATE SET result_json = excluded.result_json
            """,
            (*self._key_values(key), payload),
        )
        return True

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    @contextmanager
    def listing_transaction(self) -> Iterator["VisionCache"]:
        """Commit all cache writes for one completed listing as a unit."""
        try:
            yield self
        except Exception:
            self.rollback()
            raise
        else:
            self.commit()

    def integrity_check(self) -> str:
        row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def close(self) -> None:
        self._connection.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared connection for caches participating in the same transaction."""
        return self._connection

    def __enter__(self) -> "VisionCache":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
