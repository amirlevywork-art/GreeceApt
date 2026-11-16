"""
Stratified vision sample of rentals.

Only the buy segment (area / rent band in ``config``) with ≥3 photos and a known floor is
eligible. Agency duplicates of one flat are collapsed first so the sample does not spend its
budget scoring the same photos twice. Strata are level-4 neighborhoods; every stratum gets
``min(n, SAMPLE_MIN_PER_HOOD)`` and the rest is allocated proportionally. Already-sampled rows
stay sampled, so the sample can be grown later without discarding scored work.
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from typing import Any, Iterable

from greeceapt.rent import config as cfg
from greeceapt.rent import store

logger = logging.getLogger(__name__)


def duplicate_key(row: dict[str, Any]) -> tuple:
    """Same flat advertised by several agencies: same place (~100 m), size, rent and floor."""
    lat, lon = row.get("latitude"), row.get("longitude")
    return (
        round(float(lat), 3) if lat is not None else None,
        round(float(lon), 3) if lon is not None else None,
        round(float(row.get("area_sqm") or 0)),
        round(float(row.get("rent_eur") or 0) / 10),
        row.get("floor_class"),
    )


def collapse_duplicates(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per ``duplicate_key`` (the one with the most photos, then lowest id)."""
    best: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = duplicate_key(row)
        current = best.get(key)
        if current is None or (row.get("photos_count") or 0, -row["id"]) > (
            current.get("photos_count") or 0, -current["id"]
        ):
            best[key] = row
    return sorted(best.values(), key=lambda r: r["id"])


def eligible(row: dict[str, Any]) -> bool:
    area, rent = row.get("area_sqm"), row.get("rent_eur")
    return (
        area is not None and rent is not None
        and cfg.SEGMENT_AREA[0] <= area <= cfg.SEGMENT_AREA[1]
        and cfg.SEGMENT_RENT[0] <= rent <= cfg.SEGMENT_RENT[1]
        and (row.get("photos_count") or 0) >= 3
        and row.get("floor_class") is not None
        and not row.get("floor_conflict")
        and row.get("latitude") is not None
    )


def allocate(counts: dict[str, int], size: int, min_per: int) -> dict[str, int]:
    """Per-stratum quota: a floor of ``min_per`` (capped at n), the remainder proportional."""
    total = sum(counts.values())
    if size >= total:
        return dict(counts)
    quota = {h: min(n, min_per) for h, n in counts.items()}
    remaining = size - sum(quota.values())
    if remaining <= 0:
        return quota
    spare = {h: n - quota[h] for h, n in counts.items() if n > quota[h]}
    spare_total = sum(spare.values())
    exact = {h: remaining * s / spare_total for h, s in spare.items()}
    for h, value in exact.items():
        quota[h] += int(value)
    leftover = size - sum(quota.values())
    for h in sorted(exact, key=lambda k: exact[k] - int(exact[k]), reverse=True)[:leftover]:
        quota[h] += 1
    return quota


def choose_sample(rows: list[dict[str, Any]], size: int, *, seed: int = cfg.SAMPLE_SEED,
                  min_per: int = cfg.SAMPLE_MIN_PER_HOOD) -> dict[int, str]:
    """``{row id: stratum}`` for the sample; previously sampled rows are always kept."""
    pool = collapse_duplicates(r for r in rows if eligible(r))
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        by_stratum[str(row.get("hood_l4") or row.get("neighborhood") or "unknown")].append(row)
    quota = allocate({h: len(v) for h, v in by_stratum.items()}, size, min_per)
    rng = random.Random(seed)
    chosen: dict[int, str] = {}
    for stratum in sorted(by_stratum):
        members = by_stratum[stratum]
        kept = [r for r in members if r.get("in_vision_sample")]
        rest = [r for r in members if not r.get("in_vision_sample")]
        rng.shuffle(rest)
        for row in kept + rest[: max(0, quota[stratum] - len(kept))]:
            chosen[row["id"]] = stratum
    return chosen


def mark_sample(size: int = cfg.SAMPLE_SIZE) -> dict[int, str]:
    conn = store.connect()
    try:
        chosen = choose_sample(store.all_rows(conn), size)
        conn.executemany(
            "UPDATE listings SET in_vision_sample = 1, sample_stratum = ? WHERE id = ?",
            [(stratum, row_id) for row_id, stratum in chosen.items()],
        )
        conn.commit()
        strata = defaultdict(int)
        for stratum in chosen.values():
            strata[stratum] += 1
        logger.info("Vision sample: %s rentals across %s neighborhoods.", len(chosen), len(strata))
        return chosen
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------
# Rebalancing toward the buy segment
# ---------------------------------------------------------------------------------------------

def size_band(area: float | None) -> int | None:
    if area is None:
        return None
    for i, (lo, hi) in enumerate(cfg.SIZE_BANDS):
        if lo <= area < hi or (i == len(cfg.SIZE_BANDS) - 1 and area == hi):
            return i
    return None


def band_label(index: int) -> str:
    lo, hi = cfg.SIZE_BANDS[index]
    return f"area_{lo:g}-{hi:g}"


def band_shares(areas: Iterable[float]) -> tuple[float, ...]:
    """Share of each size band among ``areas`` (sale flats); out-of-band areas clip to the ends."""
    counts = [0] * len(cfg.SIZE_BANDS)
    for area in areas:
        band = size_band(area)
        if band is None:
            band = 0 if area < cfg.SIZE_BANDS[0][0] else len(cfg.SIZE_BANDS) - 1
        counts[band] += 1
    total = sum(counts)
    return tuple(c / total for c in counts) if total else cfg.DEFAULT_BAND_SHARES


def rebalance(rows: list[dict[str, Any]], size: int, shares: tuple[float, ...], *,
              seed: int = cfg.SAMPLE_SEED) -> dict[int, str]:
    """``{row id: band label}``: scored rows kept, the rest redrawn to match ``shares``.

    Rows already carrying a vision result stay. Per band the quota is ``size × share`` minus the
    kept rows in it (scaled to the free slots when kept rows overshoot their band); cheap rentals (< ``CHEAP_RENT``) are taken first, the rest at random. A band
    that runs out passes its leftover to the smaller bands first, then the larger ones.
    """
    kept = [r for r in rows if r.get("in_vision_sample") and r.get("visual_status_v2") is not None]
    chosen: dict[int, str] = {}
    taken_keys = set()
    for row in kept:
        band = size_band(row.get("area_sqm"))
        chosen[row["id"]] = band_label(band) if band is not None else "kept"
        taken_keys.add(duplicate_key(row))

    rng = random.Random(seed)
    candidates: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in collapse_duplicates(r for r in rows if eligible(r)):
        band = size_band(row.get("area_sqm"))
        if row["id"] in chosen or duplicate_key(row) in taken_keys or band is None:
            continue
        candidates[band].append(row)
    for band, members in candidates.items():
        rng.shuffle(members)
        members.sort(key=lambda r: (r.get("rent_eur") or 0) >= cfg.CHEAP_RENT)  # stable: cheap first

    have = defaultdict(int)
    for row in kept:
        band = size_band(row.get("area_sqm"))
        if band is not None:
            have[band] += 1
    deficit = {band: max(0, round(size * share) - have[band]) for band, share in enumerate(shares)}
    quota = allocate(deficit, max(0, size - len(chosen)), 0)  # scaled down if kept rows overshoot
    for band in range(len(shares)):
        want = quota[band]
        take = candidates[band][:want]
        candidates[band] = candidates[band][want:]
        for row in take:
            chosen[row["id"]] = band_label(band)
    leftover = size - len(chosen)
    for band in range(len(shares)):  # smaller bands first
        if leftover <= 0:
            break
        take = candidates[band][:leftover]
        leftover -= len(take)
        for row in take:
            chosen[row["id"]] = band_label(band)
    return chosen


def mark_rebalanced(size: int = cfg.SAMPLE_SIZE, shares: tuple[float, ...] | None = None) -> dict[int, str]:
    """Replace the unscored part of the sample with a size-matched redraw (scored rows stay)."""
    conn = store.connect()
    try:
        chosen = rebalance(store.all_rows(conn), size, shares or cfg.DEFAULT_BAND_SHARES)
        conn.execute("UPDATE listings SET in_vision_sample = 0, sample_stratum = NULL "
                     "WHERE in_vision_sample = 1 AND visual_status_v2 IS NULL")
        conn.executemany(
            "UPDATE listings SET in_vision_sample = 1, sample_stratum = ? WHERE id = ?",
            [(label, row_id) for row_id, label in chosen.items()],
        )
        conn.commit()
        by_label = defaultdict(int)
        for label in chosen.values():
            by_label[label] += 1
        logger.info("Rebalanced vision sample: %s rentals %s.", len(chosen), dict(sorted(by_label.items())))
        return chosen
    finally:
        conn.close()
