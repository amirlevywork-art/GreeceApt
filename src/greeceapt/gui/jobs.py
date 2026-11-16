"""
Run GUI actions as subprocesses: a queue, live log lines, progress, Stop.

A job waits until none of its resources is held by a running job (first come, first served
among jobs that compete for a resource). Stop sends SIGINT to the job's process group — the
pipeline records "stopped by hand" — then SIGTERM after ``STOP_GRACE_S``, SIGKILL after as
long again. Every finished job is appended to ``data/gui_jobs.jsonl``, which also teaches the
progress bars how long each stage takes; its full log is kept in ``data/gui_logs/``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from greeceapt.storage import paths
from greeceapt.gui.actions import ACTIONS, Action
from greeceapt.gui.progress import History, Tracker

STOP_GRACE_S = 20.0
POLL_S = 0.2
# A pipeline started outside this window (./update in a terminal, another GUI) holds these.
PIPELINE_RESOURCES = frozenset({"network", "update", "final_db", "rent_model", "rentals", "ollama"})
EXTERNAL_WAIT = "Waiting for an update that is running outside this window"


def pipeline_processes(exclude: set[int] = frozenset()) -> list[str]:
    """Command lines of running ``greeceapt.update`` / ``greeceapt.main`` processes not in ``exclude``.

    Read from the process list, never by touching ``data/update.lock`` (probing the lock could make
    an update starting at that moment exit as "already running").
    """
    try:
        out = subprocess.run(["pgrep", "-fl", r"greeceapt\.(update|main)"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) in exclude or int(pid) == os.getpid():
            continue
        if "greeceapt.update status" in command or "pgrep" in command:
            continue
        found.append(command)
    return found
TAIL_LINES = 400
KEEP_JOBS = 30
EXIT_TEXT = {2: "another update is running", 3: "Spitogatos refused the request — stopped at once",
             130: "stopped"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    action: Action
    params: dict[str, Any]
    tracker: Tracker | None = None
    status: str = "queued"            # queued | running | ok | failed | stopped | blocked
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    message: str | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    log_path: Path | None = None
    process: subprocess.Popen | None = None
    stop_requested: bool = False

    def to_json(self, *, tail: int = 20) -> dict[str, Any]:
        snapshot = self.tracker.snapshot().to_json() if self.tracker else None
        return {
            "id": self.id, "action": self.action.key, "label": self.action.label, "params": self.params,
            "status": self.status, "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "exit_code": self.exit_code, "message": self.message,
            "progress": snapshot, "tail": list(self.lines)[-tail:],
        }


class JobManager:
    def __init__(self, *, root: Path | None = None, history_path: Path | None = None,
                 log_dir: Path | None = None, actions: Mapping[str, Action] | None = None,
                 env: Mapping[str, str] | None = None, now: Callable[[], float] = time.monotonic,
                 external: Callable[[set[int]], list[str]] | None = pipeline_processes):
        self.root = root or paths.PROJECT_ROOT
        self.history = History(history_path if history_path is not None else paths.DATA_DIR / "gui_jobs.jsonl")
        self.log_dir = log_dir or paths.DATA_DIR / "gui_logs"
        self.actions = dict(actions or ACTIONS)
        self.env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
        self.now = now
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.external = external

    # ── public ───────────────────────────────────────────────────────────────
    def submit(self, key: str, params: Mapping[str, Any] | None = None) -> Job:
        action = self.actions[key]
        job = Job(id=uuid.uuid4().hex[:10], action=action, params=dict(params or {}))
        with self.lock:
            self.jobs[job.id] = job
            self._prune()
            self._schedule()
        return job

    def stop(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs[job_id]
            job.stop_requested = True
            if job.status == "queued":
                job.status, job.finished_at, job.message = "stopped", _now_iso(), "removed from the queue"
                self._schedule()
                return job
            process = job.process
        if process is not None and process.poll() is None:
            threading.Thread(target=self._terminate, args=(process,), daemon=True).start()
        return job

    def list(self) -> list[Job]:
        with self.lock:
            self._schedule()   # polled by the window: queued jobs start once an outside update ends
            return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    def external_pipelines(self) -> list[str]:
        """Pipeline processes running that this window did not start."""
        if self.external is None:
            return []
        own = {j.process.pid for j in self.jobs.values() if j.status == "running" and j.process is not None}
        return self.external(own)

    def get(self, job_id: str) -> Job:
        return self.jobs[job_id]

    def running(self) -> list[Job]:
        with self.lock:
            return [j for j in self.jobs.values() if j.status in {"running", "queued"}]

    def full_log(self, job_id: str) -> str:
        job = self.jobs[job_id]
        if job.log_path and job.log_path.exists():
            return job.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(job.lines)

    # ── scheduling ───────────────────────────────────────────────────────────
    def _held(self) -> set[str]:
        return {r for j in self.jobs.values() if j.status == "running" for r in j.action.resources}

    def _schedule(self) -> None:
        held = self._held()
        waiting = sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: j.created_at)
        if not waiting:
            return
        outside = bool(self.external_pipelines()) if any(j.action.resources & PIPELINE_RESOURCES for j in waiting) else False
        claimed: set[str] = set()
        for job in waiting:
            needs = set(job.action.resources)
            if outside and needs & PIPELINE_RESOURCES:
                job.message = EXTERNAL_WAIT
                continue
            if needs & (held | claimed):
                claimed |= needs          # an earlier job keeps its place before later ones
                continue
            held |= needs
            job.message = None
            self._start(job)

    def _prune(self) -> None:
        done = sorted((j for j in self.jobs.values() if j.status not in {"running", "queued"}),
                      key=lambda j: j.created_at)
        for job in done[:max(0, len(done) - KEEP_JOBS)]:
            self.jobs.pop(job.id, None)

    def _start(self, job: Job) -> None:
        job.status, job.started_at = "running", _now_iso()
        job.tracker = Tracker(job.action.key, job.action.stages, self.history, now=self.now)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        job.log_path = self.log_dir / f"{job.started_at[:19].replace(':', '')}-{job.action.key}-{job.id}.log"
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    # ── running ──────────────────────────────────────────────────────────────
    def _run(self, job: Job) -> None:
        code = 0
        try:
            for argv in job.action.build(job.params):
                if job.stop_requested:
                    code = 130
                    break
                code = self._run_one(job, argv)
                if code != 0:
                    break
        except Exception as exc:  # the job fails, the GUI stays up
            code = code or 1
            job.lines.append(f"GUI: {type(exc).__name__}: {exc}")
        with self.lock:
            if job.tracker:
                job.tracker.finish()
            job.exit_code = code
            job.finished_at = _now_iso()
            if job.stop_requested:
                job.status, job.message = "stopped", "stopped by hand"
            elif code == 0:
                job.status = "ok"
            elif code == 3:
                job.status, job.message = "blocked", EXIT_TEXT[3]
            else:
                job.status, job.message = "failed", EXIT_TEXT.get(code, f"exit code {code}")
            if job.tracker:
                record = job.tracker.record(job.status == "ok")
                self.history.append({**record, "at": job.finished_at, "exit_code": code, "params": job.params})
            self._schedule()

    def _run_one(self, job: Job, argv: list[str]) -> int:
        """Run with output going straight to the job's log file, which is tailed here.

        The child never writes to a pipe of the GUI, so closing the GUI cannot break a running
        update (it goes on in its own session; its log stays in ``data/gui_logs/``).
        """
        assert job.log_path is not None
        with job.log_path.open("ab") as out, job.log_path.open("rb") as tail:
            tail.seek(0, os.SEEK_END)
            process = subprocess.Popen(argv, cwd=self.root, env=self.env, stdout=out,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
            job.process = process
            pending = b""
            while True:
                chunk = tail.read()
                if chunk:
                    pending += chunk
                    *complete, pending = pending.split(b"\n")
                    for raw in complete:
                        self._line(job, raw.decode("utf-8", errors="replace").rstrip("\r"))
                elif process.poll() is not None:
                    break
                else:
                    time.sleep(POLL_S)
            if pending:
                self._line(job, pending.decode("utf-8", errors="replace"))
            return process.returncode

    def _line(self, job: Job, line: str) -> None:
        with self.lock:
            job.lines.append(line)
            if job.tracker:
                job.tracker.feed(line)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                process.wait(timeout=STOP_GRACE_S)
                return
            except subprocess.TimeoutExpired:
                continue
