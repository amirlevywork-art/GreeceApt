"""
``final_deals.db``: explainable schema and a locked, atomic, fail-closed publisher.

Publishing never exposes a half-written database and never deletes SQLite sidecar files:

1. take a non-blocking ``flock`` on ``<target>.lock`` (held → ``FinalDatabaseBusyError``);
2. a ``-journal`` file, or a ``-wal`` file next to a non-WAL database, is someone else's
   unfinished work → ``FinalDatabaseSidecarError`` *before* the target is even opened
   (opening could replay or discard it);
3. open the existing target, prove no other connection is active with ``BEGIN EXCLUSIVE``
   (fails → busy); if it is in WAL mode, checkpoint (TRUNCATE) and switch it to DELETE mode;
4. re-check sidecars; write a sibling temp database in DELETE mode, insert with named
   columns, ``PRAGMA integrity_check``, commit, fsync, close;
5. ``os.replace`` onto the target and fsync the directory.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from greeceapt.scoring.ranking import RankedDeal

# 10: net yield after every cost and tax, costs_json, signed rent
# 11: no top-3 / stress-scenario / hand-set comparison columns; gross yield on the conservative rent
# 12: area_listed (the ad's area when it adds an attic, terrace or storage)
FINAL_SCHEMA_VERSION = 12
_BUSY_TIMEOUT_S = 0.5


class FinalDatabaseBusyError(RuntimeError):
    """Another process holds the publish lock or has the final database open."""


class FinalDatabaseSidecarError(RuntimeError):
    """A -wal/-journal file next to the final database must be resolved by a person."""


_DEAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("neighborhood", "TEXT"),
    ("rank_in_neighborhood", "INTEGER"),
    ("needs_check", "INTEGER"),
    ("check_reasons_json", "TEXT NOT NULL"),
    ("opportunity_id", "TEXT NOT NULL"),
    ("url", "TEXT NOT NULL"),
    ("source_urls_json", "TEXT NOT NULL"),
    ("title", "TEXT"),
    ("price_eur", "REAL"),
    ("area_sqm", "REAL"),                   # living area
    ("area_listed", "REAL"),                # the ad's area, when it adds an attic, terrace or storage
    ("floor", "REAL"),
    ("floor_class", "TEXT"),
    ("floor_evidence_json", "TEXT NOT NULL"),
    ("visual_score", "REAL"),
    ("visual_score_after", "REAL"),  # score rent is earned at: own, or the renovation target
    ("cost_before_renovation", "REAL"),  # price + acquisition costs
    ("all_in_cost", "REAL"),
    ("renovation_eur", "REAL"),
    ("renovation_level", "TEXT"),
    ("assumptions_json", "TEXT NOT NULL"),
    ("excluded_assumptions_json", "TEXT NOT NULL"),
    ("confidence", "TEXT NOT NULL"),
    ("evidence_warnings_json", "TEXT NOT NULL"),
    ("risk_flags_json", "TEXT NOT NULL"),
    ("market_discount_pct", "REAL"),
    ("price_cut_pct", "REAL"),
    ("days_on_market", "INTEGER"),
    ("first_published", "TEXT"),            # date of the flat's earliest advertisement
    ("exclusion_reason", "TEXT"),
    # Calibrated rent model: euro rent and yields (rank_in_neighborhood orders by net_yield_lo)
    ("rent_hat_eur", "REAL"),
    ("rent_lo_eur", "REAL"),
    ("gross_yield", "REAL"),
    ("net_yield_lo", "REAL"),
    ("net_yield_lo_before_tax", "REAL"),     # the ranked yield before income tax
    ("costs_json", "TEXT"),                  # the conservative year: rent, every cost, tax, net
    ("rent_basis", "TEXT"),
    ("rent_unavailable_reason", "TEXT"),
    ("neighborhood_note", "TEXT"),              # catch-all area name re-filed by location
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _deal_values(deal: RankedDeal, assumptions: Mapping[str, Any]) -> dict[str, Any]:
    estimate = deal.estimate
    rent = deal.rent_yield
    return {
        "neighborhood": deal.neighborhood,
        "rank_in_neighborhood": deal.rank_in_neighborhood,
        "needs_check": int(deal.needs_check),
        "check_reasons_json": _json(list(deal.check_reasons)),
        "opportunity_id": deal.opportunity_id,
        "url": deal.url,
        "source_urls_json": _json(list(deal.source_urls)),
        "title": deal.title,
        "price_eur": deal.price_eur,
        "area_sqm": deal.area_sqm,
        "area_listed": deal.area_listed,
        "floor": deal.floor,
        "floor_class": deal.floor_class,
        "floor_evidence_json": _json(list(deal.floor_evidence)),
        "visual_score": deal.visual_score,
        "visual_score_after": estimate.condition_score,
        "cost_before_renovation": estimate.acquisition_cost,
        "all_in_cost": estimate.all_in_cost,
        "renovation_eur": estimate.renovation_budget,
        "renovation_level": estimate.renovation_level,
        "assumptions_json": _json(dict(assumptions)),
        "excluded_assumptions_json": _json(list(estimate.excluded_assumptions)),
        "confidence": estimate.confidence,
        "evidence_warnings_json": _json(list(estimate.evidence_warnings)),
        "risk_flags_json": _json(list(estimate.risk_flags)),
        "market_discount_pct": deal.market_discount_pct,
        "price_cut_pct": deal.price_cut_pct,
        "days_on_market": deal.days_on_market,
        "first_published": deal.first_published,
        "exclusion_reason": estimate.exclusion_reason,
        "rent_hat_eur": rent.rent_hat if rent else None,
        "rent_lo_eur": rent.rent_lo if rent else None,
        "gross_yield": rent.gross_yield if rent else None,
        "net_yield_lo": rent.net_yield_lo if rent else None,
        "net_yield_lo_before_tax": rent.net_yield_lo_before_tax if rent else None,
        "costs_json": _json(rent.costs) if rent and rent.costs else None,
        "rent_basis": rent.rent_basis if rent else None,
        "rent_unavailable_reason": rent.unavailable_reason if rent else None,
        "neighborhood_note": deal.neighborhood_note,
    }


def create_final_schema(conn: sqlite3.Connection) -> None:
    columns = ",\n    ".join(f"{name} {kind}" for name, kind in _DEAL_COLUMNS)
    for table in ("deals", "unranked_deals"):
        conn.execute(f"CREATE TABLE {table} (\n    {columns}\n)")
    conn.execute(
        """
        CREATE TABLE neighborhoods (
            name                 TEXT PRIMARY KEY,
            listing_count        INTEGER,
            ranked_count         INTEGER,
            sale_baseline_psqm   REAL,
            tier                 TEXT
        )
        """
    )
    conn.execute("CREATE TABLE ranking_metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
    conn.execute(
        """
        CREATE VIEW method_ranks AS
        SELECT neighborhood, url, title, price_eur, area_sqm, floor_class,
               visual_score, visual_score_after, renovation_level,
               cost_before_renovation, renovation_eur, all_in_cost,
               rent_hat_eur, rent_lo_eur, gross_yield, net_yield_lo, net_yield_lo_before_tax,
               needs_check, rank_in_neighborhood AS rank_net_yield
        FROM deals
        WHERE rank_in_neighborhood IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE VIEW top_needing_check AS
        SELECT * FROM deals WHERE needs_check = 1
        ORDER BY neighborhood, rank_in_neighborhood
        """
    )


def _insert_deals(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]) -> None:
    names = [name for name, _ in _DEAL_COLUMNS]
    placeholders = ", ".join(f":{name}" for name in names)
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})", list(rows)
    )


def _is_wal(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(20)
    return len(header) >= 20 and header[18] == 2 and header[19] == 2


def _check_sidecars(target: Path, *, allow_wal: bool) -> None:
    journal = target.with_name(target.name + "-journal")
    wal = target.with_name(target.name + "-wal")
    if journal.exists():
        raise FinalDatabaseSidecarError(f"{journal} exists; resolve it before publishing (not deleted)")
    if wal.exists() and not allow_wal:
        raise FinalDatabaseSidecarError(f"{wal} exists; resolve it before publishing (not deleted)")


def _quiesce_existing(target: Path) -> None:
    wal_mode = _is_wal(target)
    _check_sidecars(target, allow_wal=wal_mode)
    conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    try:
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise FinalDatabaseBusyError(f"{target} is open in another connection: {exc}") from exc
        conn.execute("ROLLBACK")
        if wal_mode:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    _check_sidecars(target, allow_wal=False)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_final_database_atomic(
    target: Path,
    ranked: Sequence[RankedDeal],
    unranked: Sequence[RankedDeal],
    neighborhoods: Sequence[tuple],
    metadata: Mapping[str, Any],
) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with open(lock_path, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FinalDatabaseBusyError(f"{lock_path} is held by another publisher") from exc
        try:
            if target.exists():
                _quiesce_existing(target)
            else:
                _check_sidecars(target, allow_wal=False)

            tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            tmp.unlink(missing_ok=True)
            try:
                assumptions = metadata.get("assumptions", {})
                conn = sqlite3.connect(tmp)
                try:
                    conn.execute("PRAGMA journal_mode=DELETE")
                    create_final_schema(conn)
                    _insert_deals(conn, "deals", (
                        _deal_values(deal, assumptions) for deal in ranked
                    ))
                    _insert_deals(conn, "unranked_deals", (
                        _deal_values(deal, assumptions) for deal in unranked
                    ))
                    conn.executemany(
                        "INSERT INTO neighborhoods VALUES (?, ?, ?, ?, ?)", list(neighborhoods)
                    )
                    meta = dict(metadata)
                    meta.setdefault("final_schema_version", FINAL_SCHEMA_VERSION)
                    conn.executemany(
                        "INSERT INTO ranking_metadata VALUES (?, ?)",
                        [(key, _json(value)) for key, value in sorted(meta.items())],
                    )
                    conn.commit()
                    status = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if status != "ok":
                        raise RuntimeError(f"integrity_check failed on new final DB: {status}")
                finally:
                    conn.close()
                _fsync(tmp)
                os.replace(tmp, target)
                _fsync(target.parent)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
