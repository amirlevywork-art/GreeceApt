"""Spitogatos rentals: Phase A search pages → Phase B detail per new listing → rentals.db."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

from greeceapt.rent import config as cfg
from greeceapt.rent import store
from greeceapt.rent.records import build_record, listing_url
from greeceapt.collect.spitogatos import config as spito_cfg
from greeceapt.collect.spitogatos.scrape import (
    BlockedError,
    _get_json,
    _make_session,
    _post_json,
    _props_from_search_root,
)

logger = logging.getLogger(__name__)


async def collect_search_rows(session, payload: dict | None = None) -> list[dict[str, Any]]:
    """Every search row for ``payload`` (page 0 reveals the page count; the rest fan out)."""
    payload = payload or cfg.RENT_SEARCH_PAYLOAD
    first = await _post_json(
        session, spito_cfg.SEARCH_API, {**payload, "offset": 0}, tag="rent offset=0", raise_on_block=True
    )
    if not isinstance(first, dict):
        logger.error("Rental search page 0 failed; aborting.")
        return []
    rows = list(_props_from_search_root(first))
    total = int((first.get("pagination") or {}).get("totalResults") or len(rows))
    offsets = list(range(cfg.PAGE_SIZE, min(total, cfg.MAX_OFFSET), cfg.PAGE_SIZE))
    logger.info("Rental search: %s results → %s more pages.", total, len(offsets))
    sem = asyncio.Semaphore(cfg.SEARCH_CONCURRENCY)
    stop = asyncio.Event()  # set by a refusal (HTTP 403/405/429): no page still waiting is asked

    async def fetch(offset: int) -> list:
        async with sem:
            if stop.is_set():
                return []
            await asyncio.sleep(random.uniform(0.3, 1.0))
            if stop.is_set():
                return []
            try:
                data = await _post_json(
                    session, spito_cfg.SEARCH_API, {**payload, "offset": offset}, tag=f"rent offset={offset}",
                    raise_on_block=True,
                )
            except BlockedError:
                stop.set()
                return []
            return list(_props_from_search_root(data))

    pages = await asyncio.gather(*(fetch(o) for o in offsets))
    if stop.is_set():
        raise BlockedError("Spitogatos refused the rental search; nothing was retried.")
    for page in pages:
        rows.extend(page)
    unique: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            unique.setdefault(str(row["id"]), row)
    logger.info("Rental search: %s unique rows.", len(unique))
    return list(unique.values())


async def _fetch_detail(session, sem: asyncio.Semaphore, pid: str, stop: asyncio.Event) -> dict | None:
    """Detail of one rental; a refusal (HTTP 403/405/429) sets ``stop`` so no further one is asked."""
    async with sem:
        if stop.is_set():
            return None
        await asyncio.sleep(random.uniform(0.2, 0.8))
        if stop.is_set():
            return None
        try:
            root = await _get_json(
                session, spito_cfg.DETAIL_API.format(id=pid), tag=f"rent detail {pid}", raise_on_block=True
            )
        except BlockedError:
            stop.set()
            return None
    data = root.get("data") if isinstance(root, dict) else None
    return data if isinstance(data, dict) else None


async def run() -> None:
    conn = store.connect()
    session = _make_session()
    try:
        rows = await collect_search_rows(session)
        known = store.known_urls(conn)
        now = datetime.now(timezone.utc).isoformat()
        new_rows = []
        for row in rows:
            url = listing_url(row["id"])
            if url in known:
                store.touch_seen(conn, url, now, {
                    "rent_eur": row.get("price"),
                    "price_reduced": 1 if row.get("priceReduced") else 0,
                    "price_pre_reduction": row.get("pricePreReduction"),
                })
            else:
                new_rows.append(row)
        conn.commit()
        logger.info("Rentals: %s re-seen, %s new → fetching detail.", len(rows) - len(new_rows), len(new_rows))

        sem = asyncio.Semaphore(cfg.DETAIL_CONCURRENCY)
        stop = asyncio.Event()
        done = failed = 0
        for start in range(0, len(new_rows), cfg.SAVE_EVERY):
            batch = new_rows[start:start + cfg.SAVE_EVERY]
            batch_session = _make_session()
            details = await asyncio.gather(
                *(_fetch_detail(batch_session, sem, str(row["id"]), stop) for row in batch),
                return_exceptions=True,
            )
            records = []
            for row, detail in zip(batch, details):
                if isinstance(detail, Exception) or detail is None:
                    failed += 1
                    continue
                records.append(build_record(row, detail))
            done += store.upsert_records(conn, records)
            logger.info("Rentals: %s / %s stored (%s detail failures).", done, len(new_rows), failed)
            if stop.is_set():
                raise BlockedError("Spitogatos refused the rental collector; nothing was retried.")
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        logger.info("Rentals done: %s rows in %s.", total, cfg.RENTALS_DB)
    finally:
        conn.close()
