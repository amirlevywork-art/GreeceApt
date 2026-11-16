"""The rental collector stops asking the moment Spitogatos refuses a request."""

from __future__ import annotations

import asyncio

import pytest

from greeceapt.rent import config as cfg
from greeceapt.rent import scrape
from greeceapt.collect.spitogatos.scrape import BlockedError


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scrape.asyncio, "sleep", no_sleep)


def _page(first_id: int, total: int) -> dict:
    rows = [{"id": first_id + i, "price": 500} for i in range(cfg.PAGE_SIZE)]
    return {"data": rows, "pagination": {"totalResults": total}}


def test_refused_rental_detail_stops_the_ones_still_waiting(monkeypatch):
    asked = []

    async def fake_get(session, url, *, tag, raise_on_block=False):
        asked.append(url)
        assert raise_on_block
        raise BlockedError(tag)

    monkeypatch.setattr(scrape, "_get_json", fake_get)

    async def collect():
        stop, sem = asyncio.Event(), asyncio.Semaphore(1)
        details = await asyncio.gather(*(scrape._fetch_detail(None, sem, str(pid), stop) for pid in (1, 2, 3)))
        return details, stop.is_set()

    details, stopped = asyncio.run(collect())
    assert details == [None, None, None] and stopped
    assert len(asked) == 1                                   # the refusal was not followed by another request


def test_rental_detail_is_returned_when_the_site_answers(monkeypatch):
    async def fake_get(session, url, *, tag, raise_on_block=False):
        return {"data": {"id": 7, "description": "flat"}}

    monkeypatch.setattr(scrape, "_get_json", fake_get)

    async def collect():
        stop = asyncio.Event()
        return await scrape._fetch_detail(None, asyncio.Semaphore(1), "7", stop), stop.is_set()

    assert asyncio.run(collect()) == ({"id": 7, "description": "flat"}, False)


def test_refused_rental_search_page_raises_and_asks_for_no_more_pages(monkeypatch):
    monkeypatch.setattr(cfg, "SEARCH_CONCURRENCY", 1)
    asked = []

    async def fake_post(session, url, body, *, tag, raise_on_block=False):
        asked.append(body["offset"])
        assert raise_on_block
        if body["offset"] == 0:
            return _page(1, total=cfg.PAGE_SIZE * 5)
        raise BlockedError(tag)

    monkeypatch.setattr(scrape, "_post_json", fake_post)

    with pytest.raises(BlockedError):
        asyncio.run(scrape.collect_search_rows(None))
    assert asked == [0, cfg.PAGE_SIZE]                       # four pages were due; one was asked


def test_refused_first_rental_search_page_raises(monkeypatch):
    async def fake_post(session, url, body, *, tag, raise_on_block=False):
        raise BlockedError(tag)

    monkeypatch.setattr(scrape, "_post_json", fake_post)
    with pytest.raises(BlockedError):
        asyncio.run(scrape.collect_search_rows(None))
