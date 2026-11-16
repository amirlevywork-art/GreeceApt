"""
Progress and time-left for a running action, read from its log lines.

An action is a sequence of ``Stage``s. A stage starts when its ``start`` pattern appears (the
first stage at once) and ends when a later stage starts or the job ends; stages that never
appear (no ad details to fetch, say) simply count as done. Inside a stage:

* a *counted* stage reads ``done`` (and ``total`` when the line has it) from its ``tick``
  pattern, or adds one per tick when it has no number. Time left = items left × seconds per
  item, the latter an exponentially weighted average of the observed rate, starting from the
  rate learned in earlier runs (``History``) or the stage default;
* an *uncounted* stage is timed: expected duration from earlier runs, the bar stops at 95 %
  until the stage ends.

Stages not yet started add their expected duration. Everything is measured on the GUI's own
clock (line arrival time), so log timestamps never need parsing.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

EWMA_WEIGHT = 0.2            # weight of the newest observed per-item rate (floor)
TIMED_CAP = 0.95             # an uncounted stage never shows more than this before it ends


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    start: re.Pattern | None = None           # None: starts with the job
    expected_s: float = 30.0                  # default duration (uncounted) when no history
    tick: re.Pattern | None = None            # counted: groups "done" (and optional "total")
    total: re.Pattern | None = None           # a line announcing the total (group "total")
    per_item_s: float | None = None           # default seconds per item (counted)
    default_total: Callable[[], int | None] | None = None   # e.g. last sweep's result count


@dataclass
class _StageRun:
    started_at: float
    done: int = 0
    total: int | None = None
    rate: float | None = None                 # seconds per item (EWMA)
    observed: int = 0
    last_tick: tuple[float, int] | None = None
    ended_at: float | None = None


@dataclass
class Snapshot:
    stage: str | None
    stage_index: int
    stages: int
    done: int | None
    total: int | None
    fraction: float
    elapsed_s: float
    eta_s: float | None

    def to_json(self) -> dict[str, Any]:
        return self.__dict__.copy()


class History:
    """Per-action stage durations and per-item rates of earlier successful runs (JSON lines)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.records: list[dict[str, Any]] = []
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self.records.append(json.loads(line))
                except ValueError:
                    continue

    def _values(self, action: str, stage: str, field_name: str) -> list[float]:
        out = []
        for record in self.records[-50:]:
            if record.get("action") == action and record.get("ok"):
                value = (record.get("stages") or {}).get(stage, {}).get(field_name)
                if isinstance(value, (int, float)) and value > 0:
                    out.append(float(value))
        return out[-5:]

    def duration(self, action: str, stage: str) -> float | None:
        values = self._values(action, stage, "duration_s")
        return statistics.median(values) if values else None

    def rate(self, action: str, stage: str) -> float | None:
        values = self._values(action, stage, "per_item_s")
        return statistics.median(values) if values else None

    def append(self, record: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.records.append(dict(record))


class Tracker:
    def __init__(self, action: str, stages: Sequence[Stage], history: History | None = None,
                 *, now: Callable[[], float] = time.monotonic):
        self.action = action
        self.stages = list(stages)
        self.history = history or History(None)
        self.now = now
        self.started_at = now()
        self.current = 0
        self.runs: dict[int, _StageRun] = {0: self._begin(0, self.started_at)}
        self.finished_at: float | None = None

    # ── input ────────────────────────────────────────────────────────────────
    def _begin(self, index: int, at: float) -> _StageRun:
        stage = self.stages[index]
        run = _StageRun(started_at=at)
        run.rate = self.history.rate(self.action, stage.key) or stage.per_item_s
        if stage.default_total is not None:
            try:
                run.total = stage.default_total()
            except Exception:
                run.total = None
        return run

    def feed(self, line: str) -> None:
        at = self.now()
        for index in range(self.current + 1, len(self.stages)):
            pattern = self.stages[index].start
            if pattern is not None and pattern.search(line):
                self.runs[self.current].ended_at = at
                self.current = index
                self.runs[index] = self._begin(index, at)
                break
        stage, run = self.stages[self.current], self.runs[self.current]
        if stage.total is not None:
            match = stage.total.search(line)
            if match:
                run.total = int(match.group("total"))
        if stage.tick is not None:
            match = stage.tick.search(line)
            if match:
                groups = match.groupdict()
                done = int(groups["done"]) if groups.get("done") else run.done + 1
                if groups.get("total") and groups["total"].isdigit():
                    run.total = int(groups["total"])
                self._observe(run, at, done)

    def _observe(self, run: _StageRun, at: float, done: int) -> None:
        if run.last_tick is None:
            previous_at, previous_done = run.started_at, 0
        else:
            previous_at, previous_done = run.last_tick
        if done > previous_done:
            observed = (at - previous_at) / (done - previous_done)
            run.observed += done - previous_done
            if run.rate is None:
                run.rate = observed
            else:
                # the first observations move a prior rate halfway, later ones by EWMA_WEIGHT
                weight = max(EWMA_WEIGHT, 1 / (run.observed + 1))
                run.rate = (1 - weight) * run.rate + weight * observed
        run.done = max(run.done, done)
        run.last_tick = (at, run.done)

    def finish(self) -> None:
        at = self.now()
        self.runs[self.current].ended_at = at
        self.finished_at = at

    # ── output ───────────────────────────────────────────────────────────────
    def _expected(self, index: int) -> float:
        stage = self.stages[index]
        known = self.history.duration(self.action, stage.key)
        return known if known is not None else stage.expected_s

    def _stage_left(self, index: int, at: float) -> tuple[float, float]:
        """(seconds left, fraction done) of a started stage."""
        stage, run = self.stages[index], self.runs[index]
        elapsed = at - run.started_at
        if stage.tick is not None and run.total and run.rate is not None:
            left_items = max(run.total - run.done, 0)
            fraction = min(run.done / run.total, 1.0)
            return left_items * run.rate, fraction
        expected = self._expected(index)
        return max(expected - elapsed, 0.0), min(elapsed / expected, TIMED_CAP) if expected else TIMED_CAP

    def snapshot(self) -> Snapshot:
        at = self.finished_at if self.finished_at is not None else self.now()
        elapsed = at - self.started_at
        if self.finished_at is not None:
            return Snapshot(None, len(self.stages), len(self.stages), None, None, 1.0, elapsed, 0.0)
        left_now, _ = self._stage_left(self.current, at)
        future = sum(self._expected(i) for i in range(self.current + 1, len(self.stages)))
        done_so_far = sum(
            (run.ended_at or at) - run.started_at for i, run in self.runs.items() if i < self.current
        ) + (at - self.runs[self.current].started_at)   # skipped stages never ran and take no time
        total_time = done_so_far + left_now + future
        fraction = done_so_far / total_time if total_time > 0 else 0.0
        stage, run = self.stages[self.current], self.runs[self.current]
        return Snapshot(
            stage=stage.label, stage_index=self.current, stages=len(self.stages),
            done=run.done if stage.tick is not None else None,
            total=run.total if stage.tick is not None else None,
            fraction=min(max(fraction, 0.0), 0.99),   # 100 % only once the job has ended
            elapsed_s=elapsed, eta_s=left_now + future,
        )

    def record(self, ok: bool) -> dict[str, Any]:
        """What ``History`` keeps of this run."""
        stages: dict[str, dict[str, float]] = {}
        for index, run in self.runs.items():
            if run.ended_at is None:
                continue
            entry = {"duration_s": run.ended_at - run.started_at}
            if run.done and run.observed:
                entry["per_item_s"] = (run.ended_at - run.started_at) / run.done
            stages[self.stages[index].key] = entry
        return {"action": self.action, "ok": ok, "stages": stages}


# ── Patterns shared by the actions ─────────────────────────────────────────────

def rx(pattern: str) -> re.Pattern:
    return re.compile(pattern)


PHASE_A_TICK = rx(r"Phase A pass \d+ — offset=\d+\s+seen=(?P<done>\d+)(?:\s+total=(?P<total>\d+|\?))?")
PHASE_B_START = rx(r"Phase B — fetching detail for (?P<total>\d+) listings")
PHASE_B_TICK = rx(r"Saved — (?P<done>\d+) / (?P<total>\d+) details stored")
FINGERPRINT_START = rx(r"Computing fingerprints for (?P<total>\d+) listings")
FINGERPRINT_TICK = rx(r"Fingerprints: (?P<done>\d+) / (?P<total>\d+) listings")
LAYER1_TOTAL = rx(r"Layer 1 V2: (?P<total>\d+) listings to process")
LAYER1_TICK = rx(r"Layer 1 V2: id=.*(?:cache_hits=\S+|left pending for retry\.)(?: \((?P<done>\d+)/(?P<total>\d+)\))?")
RENTALS_DETAIL_START = rx(r"Rentals: \d+ re-seen, (?P<total>\d+) new")
RENTALS_TICK = rx(r"Rentals: (?P<done>\d+) / (?P<total>\d+) stored")
