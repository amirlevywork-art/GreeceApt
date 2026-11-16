"""
Ranking snapshots and their check against later delistings.

Every scoring run appends one snapshot of ``method_ranks`` (the net-yield rank of each flat) to
``data/rank_history.sqlite3``. A flat that later leaves Spitogatos was most likely sold or reserved
(or withdrawn), so a method that ranks such flats higher is pointing at the deals the market takes.

A listing counts as **gone** only against a *complete* search sweep (``spitogatos_sweep.json``,
written when the scraper reaches the last results page): none of its Spitogatos URLs was seen
since that sweep started. Flats with no Spitogatos URL, or also listed on XE (whose coverage is
partial), are left out. The report uses the latest snapshot taken at least ``min_days`` before the
sweep, so each flat had time to sell.

    python -m greeceapt.scoring.rank_history            # report (also written to data/)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from greeceapt.storage import paths

logger = logging.getLogger(__name__)

METHODS = {
    "net_yield": "rank_net_yield",
}
MIN_DAYS = 7

TABLE = "rank_snapshots_v2"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    as_of TEXT NOT NULL,
    url TEXT NOT NULL,
    neighborhood TEXT,
    source_urls_json TEXT,
    price_eur REAL,
    net_yield_lo REAL,
    rank_net_yield INTEGER,
    rank_hand_set_yield INTEGER,   -- the old comparison rank; NULL from 2026-11-16
    n_in_neighborhood INTEGER,
    PRIMARY KEY (as_of, url)
)
"""


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Carry snapshots taken before the net-yield rank became primary (2026-11) into the new
    table: their rent-model rank is the same quantity the net-yield rank is now."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'rank_snapshots'").fetchone():
        return
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {TABLE}
        SELECT as_of, url, neighborhood, source_urls_json, price_eur, net_yield_lo,
               rank_model_net_lo, rank_hand_set_yield, n_in_neighborhood
        FROM rank_snapshots WHERE rank_model_net_lo IS NOT NULL
        """
    )


def record_snapshot(final_db: Path, history_db: Path, as_of: date) -> int:
    """Copy this run's ranks into the history (a rerun on the same day replaces that day)."""
    src = sqlite3.connect(f"file:{final_db}?mode=ro", uri=True)
    try:
        rows = src.execute(
            """
            SELECT m.url, m.neighborhood, d.source_urls_json, m.price_eur, m.net_yield_lo,
                   m.rank_net_yield, NULL,
                   COUNT(*) OVER (PARTITION BY m.neighborhood)
            FROM method_ranks m JOIN deals d ON d.url = m.url
            """
        ).fetchall()
    finally:
        src.close()
    conn = sqlite3.connect(history_db)
    try:
        with conn:
            conn.execute(_SCHEMA)
            _migrate_v1(conn)
            conn.execute(f"DELETE FROM {TABLE} WHERE as_of = ?", (as_of.isoformat(),))
            conn.executemany(
                f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(as_of.isoformat(), *row) for row in rows],
            )
    finally:
        conn.close()
    return len(rows)


def gone_status(source_urls: Sequence[str], last_seen: Mapping[str, str | None], sweep_started: str) -> bool | None:
    """True gone / False still listed / None unknowable (no Spitogatos URL, or also on XE)."""
    if any("xe.gr" in url for url in source_urls):
        return None
    spitogatos = [url for url in source_urls if "spitogatos.gr" in url]
    if not spitogatos:
        return None
    seen = [last_seen.get(url) for url in spitogatos]
    return not any(stamp is not None and stamp >= sweep_started for stamp in seen)


def _auc(gone: list[float], kept: list[float]) -> float | None:
    """P(a gone flat sits better (lower percentile) than a listed one); 0.5 = no signal."""
    if not gone or not kept:
        return None
    better = sum((g < k) + 0.5 * (g == k) for g in gone for k in kept)
    return better / (len(gone) * len(kept))


@dataclass(frozen=True)
class MethodCheck:
    method: str
    auc: float | None
    top1_gone: int
    top1_n: int
    top3_gone: int
    top3_n: int
    rest_gone: int
    rest_n: int


@dataclass(frozen=True)
class DelistingReport:
    snapshot_as_of: str
    sweep_started_at: str
    flats: int
    gone: int
    methods: tuple[MethodCheck, ...]


def evaluate(snapshot: Sequence[Mapping[str, Any]], last_seen: Mapping[str, str | None],
             sweep_started: str, snapshot_as_of: str) -> DelistingReport:
    labelled = []
    for row in snapshot:
        status = gone_status(json.loads(row["source_urls_json"] or "[]") or [row["url"]], last_seen, sweep_started)
        if status is not None:
            labelled.append((row, status))
    checks = []
    for method, column in METHODS.items():
        gone_pct, kept_pct = [], []
        counts = {"top1": [0, 0], "top3": [0, 0], "rest": [0, 0]}
        for row, gone in labelled:
            rank, n = row[column], row["n_in_neighborhood"]
            if rank is None:
                continue
            (gone_pct if gone else kept_pct).append((rank - 1) / (n - 1) if n > 1 else 0.0)
            keys = ("top1", "top3") if rank == 1 else ("top3",) if rank <= 3 else ("rest",)
            for key in keys:
                counts[key][0] += gone
                counts[key][1] += 1
        checks.append(MethodCheck(method, _auc(gone_pct, kept_pct), *counts["top1"], *counts["top3"], *counts["rest"]))
    return DelistingReport(snapshot_as_of, sweep_started, len(labelled), sum(g for _, g in labelled), tuple(checks))


def load_last_seen(listings_json: Path) -> dict[str, str | None]:
    data = json.loads(listings_json.read_text(encoding="utf-8"))
    rows = data.values() if isinstance(data, dict) else data
    return {row["url"]: row.get("updated_at") for row in rows if isinstance(row, dict) and row.get("url")}


def delisting_report(history_db: Path | None = None, listings_json: Path | None = None,
                     sweep_json: Path | None = None, *, min_days: int = MIN_DAYS) -> DelistingReport | None:
    """None until there is a complete sweep at least ``min_days`` after some snapshot."""
    history_db = Path(history_db or paths.RANK_HISTORY_DB)
    listings_json = Path(listings_json or paths.SPITOGATOS_LISTINGS_JSON)
    sweep_json = Path(sweep_json or paths.SPITOGATOS_SWEEP_JSON)
    if not (history_db.exists() and listings_json.exists() and sweep_json.exists()):
        return None
    sweep_started = json.loads(sweep_json.read_text(encoding="utf-8"))["started_at"]
    cutoff = (datetime.fromisoformat(sweep_started).date() - timedelta(days=min_days)).isoformat()
    conn = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (TABLE,)).fetchone():
            return None
        found = conn.execute(f"SELECT MAX(as_of) FROM {TABLE} WHERE as_of <= ?", (cutoff,)).fetchone()[0]
        if found is None:
            return None
        snapshot = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE} WHERE as_of = ?", (found,))]
    finally:
        conn.close()
    return evaluate(snapshot, load_last_seen(listings_json), sweep_started, found)


def _rate(gone: int, n: int) -> str:
    return f"{gone}/{n} ({gone / n:.0%})" if n else "–"


def render(report: DelistingReport) -> str:
    lines = [
        "# Rankings vs. later delistings",
        "",
        f"Snapshot {report.snapshot_as_of}; complete sweep started {report.sweep_started_at}. "
        f"{report.gone} of {report.flats} Spitogatos-only flats have since left the site "
        f"({report.gone / report.flats:.1%} base rate)." if report.flats else "No checkable flats.",
        "",
        "AUC = chance a flat that left sat higher in its neighborhood than one still listed "
        "(0.5 = no signal). Small samples: read differences under ~0.05 as noise.",
        "",
        "| method | AUC | gone among #1 | gone in top 3 | gone in the rest |",
        "|---|---|---|---|---|",
    ]
    for check in report.methods:
        auc = f"{check.auc:.3f}" if check.auc is not None else "–"
        lines.append(f"| {check.method} | {auc} | {_rate(check.top1_gone, check.top1_n)} | "
                     f"{_rate(check.top3_gone, check.top3_n)} | {_rate(check.rest_gone, check.rest_n)} |")
    return "\n".join(lines) + "\n"


def log_and_write(report_path: Path | None = None, **kwargs: Any) -> DelistingReport | None:
    report = delisting_report(**kwargs)
    if report is None:
        logger.info("Delisting check: waiting for a complete Spitogatos sweep ≥ %s days after a snapshot.", MIN_DAYS)
        return None
    Path(report_path or paths.RANK_HISTORY_REPORT).write_text(render(report), encoding="utf-8")
    logger.info(
        "Delisting check (snapshot %s): %s of %s flats gone; AUC %s.",
        report.snapshot_as_of, report.gone, report.flats,
        ", ".join(f"{c.method} {c.auc:.2f}" for c in report.methods if c.auc is not None) or "n/a",
    )
    return report


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    result = log_and_write()
    if result is not None:
        print(render(result))
