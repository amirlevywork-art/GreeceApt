"""``./update`` — refresh the ranking by hand, and say when that was last done.

    ./update           collect what changed on Spitogatos, score it, re-rank, report the changes
    ./update status    date and time of the last update and what it changed (asks the site nothing)
    ./update check     read the search pages only and say what an update would change (writes nothing)

There is no scheduler: an update runs when this command is run. Each run

1. takes a lock (a second ``./update`` started meanwhile exits at once);
2. copies the files it is about to change to ``data/backups/<time>/`` (the last 5 are kept);
3. runs the full pipeline (``greeceapt.main.run_full_pipeline``);
4. compares the ranking before and after and writes ``data/update_report.md``;
5. appends one line to ``data/update_log.jsonl`` — where "last update: <date> <time>" comes from.

If Spitogatos refuses the collector (HTTP 403/405/429) the run stops there: nothing else is
requested, the ranking is left as it was, and the refusal is recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import re
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from greeceapt.storage import paths

logger = logging.getLogger(__name__)

KEEP_BACKUPS = 5
# Layer 1 (local Qwen) took about 100 s per flat on this machine (data/layer1.log).
SECONDS_PER_VISION_FLAT = 100
PRICE_TOLERANCE_EUR = 0.5
REPORT_LIST_LIMIT = 50
NO_LONGER_LISTED = "no_longer_listed"

EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_BLOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130

_BACKUP_NAME = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
_SQLITE_SUFFIXES = {".db", ".sqlite3"}
_SNAPSHOT_COLUMNS = (
    "opportunity_id", "neighborhood", "rank_in_neighborhood", "url", "title",
    "price_eur", "area_sqm", "net_yield_lo", "days_on_market", "exclusion_reason",
)
_SWEEP_KEYS = (
    "status", "trusted", "trust_reason", "passes", "seen", "total_results", "new", "price_changes",
    "photos_changed", "gone", "returned", "detail_fetches", "details_stored",
)

Flat = dict[str, Any]
Pipeline = Callable[[dict[str, float]], None]


class UpdateRunning(RuntimeError):
    """Another ``./update`` holds the lock."""


# ── Lock ─────────────────────────────────────────────────────────────────────

@contextmanager
def update_lock() -> Iterator[None]:
    """Hold ``data/update.lock`` for the run; raises ``UpdateRunning`` when it is already held."""
    paths.UPDATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with paths.UPDATE_LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateRunning("Another update is already running; this one did nothing.") from exc
        yield  # closing the file releases the lock


# ── Times ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_time(value: object) -> str:
    """``2026-11-12 18:40`` in this machine's time zone; ``unknown`` for an unreadable value."""
    moment = value if isinstance(value, datetime) else _parse_time(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else "unknown"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def age(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{_plural(round(seconds / 60), 'minute')} ago"
    if seconds < 36 * 3600:
        return f"{_plural(round(seconds / 3600), 'hour')} ago"
    return f"{_plural(round(seconds / 86400), 'day')} ago"


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _stamp(value: object, now: datetime) -> str:
    moment = _parse_time(value)
    return f"{local_time(moment)} ({age(moment, now)})" if moment else "unknown"


# ── Log ──────────────────────────────────────────────────────────────────────

def read_log() -> list[dict[str, Any]]:
    try:
        lines = paths.UPDATE_LOG_JSONL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("started_at"):
            entries.append(entry)
    return entries


def append_log(entry: Mapping[str, Any]) -> None:
    paths.UPDATE_LOG_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with paths.UPDATE_LOG_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def last_update(entries: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    """The latest run that finished the whole pipeline."""
    entries = read_log() if entries is None else entries
    return next((dict(entry) for entry in reversed(entries) if entry.get("ok")), None)


# ── Backups ──────────────────────────────────────────────────────────────────

def backup_sources() -> list[Path]:
    from greeceapt.rent import config as rent_config

    return [
        paths.SPITOGATOS_LISTINGS_JSON, paths.INGESTED_DB, paths.UPDATED_DB, paths.FINAL_DB,
        paths.RANK_HISTORY_DB, paths.OPPORTUNITY_REGISTRY_DB, rent_config.RENT_MODEL_JSON,
    ]


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _copy_sqlite(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's backup API (a plain file copy could catch a write)."""
    src = _read_only(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def make_backup(now: datetime) -> tuple[Path, int]:
    """Copy the files an update changes; returns the folder and how many files it holds."""
    name = now.astimezone().strftime("%Y%m%d-%H%M%S")
    folder, attempt = paths.BACKUPS_DIR / name, 1
    while folder.exists():  # a second backup within the same second
        attempt += 1
        folder = paths.BACKUPS_DIR / f"{name}-{attempt}"
    folder.mkdir(parents=True)
    copied = 0
    for source in backup_sources():
        if not source.exists():
            continue
        if source.suffix in _SQLITE_SUFFIXES:
            _copy_sqlite(source, folder / source.name)
        else:
            shutil.copy2(source, folder / source.name)
        copied += 1
    prune_backups()
    return folder, copied


def prune_backups(keep: int = KEEP_BACKUPS) -> list[Path]:
    """Delete all but the newest ``keep`` backup folders; anything not named like one is left alone."""
    try:
        folders = sorted(p for p in paths.BACKUPS_DIR.iterdir() if p.is_dir() and _BACKUP_NAME.match(p.name))
    except OSError:
        return []
    removed = folders[:-keep] if keep > 0 else folders
    for folder in removed:
        shutil.rmtree(folder)
    return removed


# ── Ranking before / after ───────────────────────────────────────────────────

def snapshot_ranking(path: Path | None = None) -> dict[str, Flat]:
    """Every flat in ``final_deals.db`` by opportunity ID; empty when there is no readable ranking."""
    path = path or paths.FINAL_DB
    if not path.exists():
        return {}
    columns = ", ".join(_SNAPSHOT_COLUMNS)
    flats: dict[str, Flat] = {}
    try:
        conn = _read_only(path)
        try:
            for table, ranked in (("deals", True), ("unranked_deals", False)):
                for row in conn.execute(f"SELECT {columns} FROM {table}"):
                    flats[row[0]] = {**dict(zip(_SNAPSHOT_COLUMNS, row)), "ranked": ranked}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Could not read the ranking in %s: %s", path.name, exc)
        return {}
    return flats


def ranking_built_at(path: Path | None = None) -> str | None:
    path = path or paths.FINAL_DB
    if not path.exists():
        return None
    try:
        conn = _read_only(path)
        try:
            row = conn.execute("SELECT value_json FROM ranking_metadata WHERE key = 'created_at'").fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def _price_differs(old: object, new: object) -> bool:
    return isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(new - old) > PRICE_TOLERANCE_EUR


def _by_place(flat: Mapping) -> tuple:
    return (str(flat.get("neighborhood") or ""), flat.get("rank_in_neighborhood") or 10**6, str(flat.get("url")))


def diff_rankings(before: Mapping[str, Flat], after: Mapping[str, Flat]) -> dict[str, list[Flat]]:
    """What an update changed, by opportunity ID.

    ``price_changes``: ranked flats whose price moved. ``new_ranked``: ranked flats the ranking had never held.
    ``no_longer_listed``: ranked before, now without a live advertisement. ``left_ranking``:
    ranked before, now unranked for any other reason.
    """
    changes: dict[str, list[Flat]] = {
        key: [] for key in
        ("price_changes", "new_ranked", "no_longer_listed", "left_ranking")
    }
    for oid, now in after.items():
        was = before.get(oid)
        if was is None:
            if now.get("ranked"):
                changes["new_ranked"].append(dict(now))
        elif now.get("ranked") and _price_differs(was.get("price_eur"), now.get("price_eur")):
            changes["price_changes"].append({**now, "old_price_eur": was["price_eur"]})
    for oid, was in before.items():
        now = after.get(oid)
        still_ranked = bool(now and now.get("ranked"))
        if was.get("ranked") and not still_ranked:
            reason = (now.get("exclusion_reason") if now else None) or "not_in_the_data"
            key = "no_longer_listed" if reason == NO_LONGER_LISTED else "left_ranking"
            changes[key].append({**was, "reason": reason})
    for key in ("no_longer_listed", "left_ranking"):
        changes[key].sort(key=_by_place)
    changes["price_changes"].sort(key=lambda f: (f["price_eur"] / f["old_price_eur"] if f["old_price_eur"] else 1.0, f["url"]))
    changes["new_ranked"].sort(key=lambda f: (-(f.get("net_yield_lo") or 0.0), str(f.get("url"))))
    return changes


# ── Report ───────────────────────────────────────────────────────────────────

def _eur(value: object) -> str:
    return f"€{value:,.0f}" if isinstance(value, (int, float)) else "price unknown"


def _pct(value: object) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "n/a"


def _where(flat: Mapping) -> str:
    rank = flat.get("rank_in_neighborhood")
    return f"{flat.get('neighborhood') or 'Unknown'}" + (f" #{rank}" if rank is not None else "")


def _basics(flat: Mapping) -> str:
    area = flat.get("area_sqm")
    parts = [_eur(flat.get("price_eur"))]
    if isinstance(area, (int, float)):
        parts.append(f"{area:.0f} m²")
    if flat.get("net_yield_lo") is not None:
        parts.append(f"net yield {_pct(flat.get('net_yield_lo'))}")
    return ", ".join(parts)


def _reason_text(reason: object) -> str:
    return str(reason or "unknown").replace("_", " ")


def _section(title: str, flats: Sequence[Flat], line: Callable[[Flat], str]) -> list[str]:
    out = [f"## {title} ({len(flats)})", ""]
    if not flats:
        return out + ["None.", ""]
    out += [f"- {line(flat)}" for flat in flats[:REPORT_LIST_LIMIT]]
    if len(flats) > REPORT_LIST_LIMIT:
        out.append(f"- … and {len(flats) - REPORT_LIST_LIMIT} more")
    return out + [""]


def _price_line(flat: Flat) -> str:
    old, new = flat["old_price_eur"], flat["price_eur"]
    change = f" ({(new - old) / old * 100:+.1f}%)" if old else ""
    return f"{_where(flat)} — {_eur(old)} → {_eur(new)}{change} — {flat.get('url')}"


def _gone_line(flat: Flat) -> str:
    days = flat.get("days_on_market")
    listed = f", {days} days on the market" if days is not None else ""
    return f"{_where(flat)} — last price {_eur(flat.get('price_eur'))}{listed} — {flat.get('url')}"


def sweep_line(sweep: Mapping[str, Any] | None) -> str:
    if not sweep:
        return "Spitogatos: the collector left no summary for this run."
    total = sweep.get("total_results")
    seen = f"saw {sweep.get('seen', 0):,} of {total:,} results" if isinstance(total, int) else f"saw {sweep.get('seen', 0):,} results"
    trust = "trusted" if sweep.get("trusted") else f"NOT trusted ({sweep.get('trust_reason')}): no flat was marked gone"
    return (
        f"Spitogatos: {seen} ({trust}) — {sweep.get('new', 0)} new, {sweep.get('price_changes', 0)} price changes, "
        f"{sweep.get('photos_changed', 0)} photo sets changed, {sweep.get('gone', 0)} gone, "
        f"{sweep.get('returned', 0)} back"
    )


def changes_line(counts: Mapping[str, Any]) -> str:
    return (
        f"Ranking: {counts.get('ranked_after', 0)} ranked flats (was {counts.get('ranked_before', 0)}); "
        f"{counts.get('price_changes', 0)} price changes; {counts.get('new_ranked', 0)} new to the ranking; "
        f"{counts.get('no_longer_listed', 0)} no longer listed; "
        f"{counts.get('left_ranking', 0)} left the ranking for another reason"
    )


def outcome_text(entry: Mapping[str, Any]) -> str:
    status = entry.get("status")
    if status == "ok":
        return "ok"
    if status == "incomplete":
        return "done, but the Spitogatos sweep was not trusted, so no flat was marked gone"
    if status == "blocked":
        return f"blocked — {entry.get('error')}. Nothing else was requested; the ranking was left as it was"
    if status == "interrupted":
        return "stopped by hand before it finished"
    return f"failed — {entry.get('error')}"


def render_report(
    entry: Mapping[str, Any], previous: Mapping[str, Any] | None, changes: Mapping[str, Sequence[Flat]],
    backup: Path | None,
) -> str:
    started, finished = _parse_time(entry.get("started_at")), _parse_time(entry.get("finished_at"))
    counts = entry.get("counts") or {}
    lines = [f"# Update report — {local_time(finished)}", ""]
    if previous and _parse_time(previous.get("finished_at")):
        lines.append(f"- Previous update: {local_time(previous.get('finished_at'))}")
    else:
        lines.append("- Previous update: none recorded (first run of `./update`)")
    if started and finished:
        lines.append(f"- Started {local_time(started)}, took {duration((finished - started).total_seconds())}")
    lines.append(f"- Outcome: {outcome_text(entry)}")
    lines.append(f"- {sweep_line(counts.get('sweep'))}")
    lines.append(f"- {changes_line(counts)}")
    if backup is not None:
        lines.append(f"- Backup of the previous state: `{_shown(backup)}`")
    lines.append("")
    lines += _section("Price changes", changes["price_changes"], _price_line)
    lines += _section("New to the ranking (new ads, or flats a rule change now admits)", changes["new_ranked"], lambda f: f"{_where(f)} — {_basics(f)} — {f.get('url')}")
    lines += _section("No longer listed (sold, withdrawn or out of the searched range)",
                      changes["no_longer_listed"], _gone_line)
    lines += _section("Left the ranking for another reason", changes["left_ranking"],
                      lambda f: f"{_where(f)} — {_eur(f.get('price_eur'))} — {_reason_text(f.get('reason'))} — {f.get('url')}")
    return "\n".join(lines)


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(paths.PROJECT_ROOT))
    except ValueError:
        return str(path)


# ── Commands ─────────────────────────────────────────────────────────────────

def read_sweep_summary(since: datetime | None = None) -> dict[str, Any] | None:
    """The collector's last-run summary; ``None`` when missing or older than ``since``."""
    try:
        summary = json.loads(paths.SPITOGATOS_LAST_RUN_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(summary, dict):
        return None
    finished = _parse_time(summary.get("finished_at"))
    if since is not None and (finished is None or finished < since):
        return None
    return summary


def format_status(now: datetime | None = None) -> str:
    """When the ranking was last updated and what that changed."""
    now = now or _now()
    entries = read_log()
    done = last_update(entries)
    lines = []
    if done is None:
        lines.append("No update has been run with ./update yet.")
        built = ranking_built_at()
        if built:
            lines.append(f"Ranking last built: {_stamp(built, now)}")
        sweep = read_sweep_summary()
        if sweep:
            lines.append(f"Spitogatos last collected: {_stamp(sweep.get('finished_at'), now)}")
        elif paths.SPITOGATOS_LISTINGS_JSON.exists():
            collected = datetime.fromtimestamp(paths.SPITOGATOS_LISTINGS_JSON.stat().st_mtime, timezone.utc)
            lines.append(f"Spitogatos last collected: {_stamp(collected.isoformat(), now)}")
    else:
        started, finished = _parse_time(done.get("started_at")), _parse_time(done.get("finished_at"))
        took = f", took {duration((finished - started).total_seconds())}" if started and finished else ""
        counts = done.get("counts") or {}
        lines.append(f"Last update: {_stamp(done.get('finished_at'), now)} — {outcome_text(done)}{took}")
        lines.append(f"  {sweep_line(counts.get('sweep'))}")
        lines.append(f"  {changes_line(counts)}")
        if paths.UPDATE_REPORT_MD.exists():
            lines.append(f"  Report: {_shown(paths.UPDATE_REPORT_MD)}")
    if entries and not entries[-1].get("ok"):
        attempt = entries[-1]
        lines.append(f"Last attempt: {_stamp(attempt.get('finished_at') or attempt.get('started_at'), now)} — "
                     f"{outcome_text(attempt)}")
    return "\n".join(lines)


def _run_pipeline(renovation_budgets: dict[str, float]) -> None:
    from greeceapt.main import run_full_pipeline

    run_full_pipeline(as_of=date.today(), renovation_budgets=renovation_budgets, interim_every=200)


def _is_refusal(exc: BaseException) -> bool:
    from greeceapt.collect.run_all import ScraperBlocked
    from greeceapt.collect.spitogatos.scrape import BlockedError

    return isinstance(exc, (ScraperBlocked, BlockedError))


def run_update(*, renovation_budgets: dict[str, float] | None = None, pipeline: Pipeline = _run_pipeline) -> int:
    """One update; returns the process exit code."""
    started = _now()
    try:
        with update_lock():
            print(format_status(started), flush=True)
            previous = last_update()
            entry: dict[str, Any] = {
                "started_at": started.isoformat(), "finished_at": None, "ok": False, "status": "failed",
                "error": None, "counts": {},
            }
            code = EXIT_FAILED
            try:
                backup, copied = make_backup(started)
            except (OSError, sqlite3.Error) as exc:
                # Without a copy of the current state nothing is collected or changed.
                entry.update(error=f"backup failed ({type(exc).__name__}: {exc})", finished_at=_now().isoformat())
                append_log(entry)
                print(f"Update not started: {entry['error']}. Nothing was requested or changed.", file=sys.stderr)
                return code
            print(f"Backup: {_shown(backup)} ({_plural(copied, 'file')})", flush=True)
            before = snapshot_ranking()

            try:
                pipeline(dict(renovation_budgets or {}))
            except KeyboardInterrupt:
                entry.update(status="interrupted", error="stopped by hand")
                code = EXIT_INTERRUPTED
            except Exception as exc:
                if _is_refusal(exc):
                    entry.update(status="blocked", error=str(exc))
                    code = EXIT_BLOCKED
                    logger.error("Update stopped: %s. Nothing else was requested.", exc)
                else:
                    entry.update(error=f"{type(exc).__name__}: {exc}")
                    logger.exception("Update failed.")
            else:
                entry["ok"], code = True, EXIT_OK

            sweep = read_sweep_summary(since=started)
            if entry["ok"]:
                entry["status"] = "ok" if (sweep is None or sweep.get("trusted")) else "incomplete"
            after = snapshot_ranking()
            changes = diff_rankings(before, after)
            entry["counts"] = {
                "ranked_before": sum(1 for flat in before.values() if flat["ranked"]),
                "ranked_after": sum(1 for flat in after.values() if flat["ranked"]),
                **{key: len(flats) for key, flats in changes.items()},
                "sweep": {key: sweep.get(key) for key in _SWEEP_KEYS} if sweep else None,
            }
            entry["finished_at"] = _now().isoformat()
            paths.UPDATE_REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
            paths.UPDATE_REPORT_MD.write_text(render_report(entry, previous, changes, backup), encoding="utf-8")
            append_log(entry)

            took = duration((_parse_time(entry["finished_at"]) - started).total_seconds())  # type: ignore[operator]
            print(f"\nUpdate finished {local_time(entry['finished_at'])} (took {took}): {outcome_text(entry)}")
            print(f"  {sweep_line(entry['counts']['sweep'])}")
            print(f"  {changes_line(entry['counts'])}")
            print(f"  Report: {_shown(paths.UPDATE_REPORT_MD)}", flush=True)
            return code
    except UpdateRunning as exc:
        print(exc, file=sys.stderr)
        return EXIT_BUSY


def run_check() -> int:
    """Dry run of the search sweep: what an update would change. Writes nothing."""
    from greeceapt.collect.spitogatos import scrape

    try:
        with update_lock():
            summary = asyncio.run(scrape.run(dry_run=True))
    except UpdateRunning as exc:
        print(exc, file=sys.stderr)
        return EXIT_BUSY
    except scrape.BlockedError as exc:
        print(f"Spitogatos refused the request: {exc}. Stopped at once; nothing was retried.", file=sys.stderr)
        return EXIT_BLOCKED

    if summary.get("blocked"):
        print("Spitogatos refused the request. Stopped at once; nothing was retried or written.", file=sys.stderr)
        return EXIT_BLOCKED
    total = summary.get("total_results")
    trusted = bool(summary.get("trusted"))
    print(f"\nSpitogatos lists {total:,} results now." if isinstance(total, int) else "\nSpitogatos gave no result count.")
    print(f"The sweep saw {summary.get('seen', 0):,} of them in {_plural(summary.get('passes', 1), 'pass')}: "
          + ("trusted." if trusted else f"NOT trusted ({summary.get('trust_reason')})."))
    print(f"Against the {summary.get('store_rows', 0) - summary.get('new', 0):,} stored advertisements:")
    print(f"  new             {summary.get('new', 0):>5}")
    print(f"  price changed   {summary.get('price_changes', 0):>5}")
    print(f"  photos changed  {summary.get('photos_changed', 0):>5}")
    print(f"  back            {summary.get('returned', 0):>5}")
    print(f"  gone            {summary.get('gone', 0):>5}" + ("" if trusted else "   (not counted: the sweep is not trusted)"))
    fetches = summary.get("detail_fetches", 0)
    vision = summary.get("new", 0) + summary.get("photos_changed", 0)
    print(f"An update now would fetch the detail of {_plural(fetches, 'advertisement')} and send at most "
          f"{_plural(vision, 'flat')} to the local vision model: up to about "
          f"{duration(vision * SECONDS_PER_VISION_FLAT)} at ~{SECONDS_PER_VISION_FLAT} s per flat.")
    print("Nothing was written.", flush=True)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="update",
        description="Refresh the ranking: collect what changed on Spitogatos, score it, re-rank, report.",
    )
    parser.add_argument(
        "command", nargs="?", default="run", choices=("run", "status", "check"),
        help="run (default): update now. status: when the last update ran. "
             "check: what an update would change, without changing anything.",
    )
    parser.add_argument(
        "--renovation-budgets", type=Path, default=None,
        help="JSON object keyed by opportunity ID or listing URL with non-negative euro amounts.",
    )
    args = parser.parse_args(argv)

    if args.command == "status":
        print(format_status())
        return EXIT_OK

    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    if args.command == "check":
        return run_check()

    budgets: dict[str, float] = {}
    if args.renovation_budgets is not None:
        from greeceapt.main import load_renovation_budgets

        try:
            budgets = load_renovation_budgets(args.renovation_budgets)
        except ValueError as exc:
            parser.error(str(exc))
    return run_update(renovation_budgets=budgets)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
