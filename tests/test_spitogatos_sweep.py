"""The Spitogatos sweep: completeness, refusals, trust, and what it changes in the store."""

from __future__ import annotations

import asyncio
import json

import pytest

from greeceapt.collect.spitogatos import config as cfg
from greeceapt.collect.spitogatos import scrape

NOW = "2026-11-12T10:00:00+00:00"
REFUSED = "refused"


def _url(pid) -> str:
    return scrape.listing_url(pid)


def _prop(pid, *, price=50000, area=40, images=(1, 2), **over) -> dict:
    row = {
        "id": pid, "price": price, "sq_meters": area, "imageIds": list(images), "floorNumber": 6,
        "rooms": 1, "no_of_bathrooms": 1, "geography": "Kypseli (Athens)", "latitude": 37.99,
        "longitude": 23.74, "modified": "2026-11-10 09:00:00", "uploaded": "2026-08-01 09:00:00",
        "firstPublishDate": "2026-05-20 09:00:00", "priceReduced": False, "pricePreReduction": None,
        "priceChangePercentage": None,
    }
    row.update(over)
    return row


def _page(props, total=None) -> dict:
    props = [p if isinstance(p, dict) else _prop(p) for p in props]
    return {"data": props, "pagination": {"totalResults": total}}


def _stored(pid, *, price=50000, area=40, images=(1, 2), **over) -> dict:
    row = {
        "url": _url(pid), "price_eur": price, "area_sqm": area, "price_per_sqm": round(price / area, 2),
        "floor": 6.0, "publication_date": "2026-10-01", "updated_at": "2026-11-02T08:00:00+00:00",
        "photo_urls": [f"https://m3.spitogatos.gr/{i}_900x675.jpg" for i in images],
    }
    row.update(over)
    return row


class Site:
    """Scripted search API: ``offset → answer`` or a list of answers used one per request."""

    def __init__(self, pages: dict):
        self.pages = {offset: list(a) if isinstance(a, list) else [a] for offset, a in pages.items()}
        self.calls: list[int] = []
        self.detail_calls: list[str] = []
        self.details: dict[str, object] = {}

    async def post(self, session, url, payload, *, tag, raise_on_block=False):
        offset = payload["offset"]
        self.calls.append(offset)
        answers = self.pages.get(offset, [None])
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if answer == REFUSED:
            if raise_on_block:
                raise scrape.BlockedError(tag)
            return None
        return answer

    async def get(self, session, url, *, tag, raise_on_block=False):
        pid = url.rsplit("/", 1)[-1]
        self.detail_calls.append(pid)
        answer = self.details.get(pid)
        if answer == REFUSED:
            if raise_on_block:
                raise scrape.BlockedError(tag)
            return None
        return answer


@pytest.fixture
def site(monkeypatch, tmp_path):
    async def no_sleep(_):
        return None

    fake = Site({})
    monkeypatch.setattr(scrape, "_post_json", fake.post)
    monkeypatch.setattr(scrape, "_get_json", fake.get)
    monkeypatch.setattr(scrape, "_make_session", lambda: None)
    monkeypatch.setattr(scrape.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(cfg, "LISTINGS_JSON", tmp_path / "spitogatos_listings.json")
    monkeypatch.setattr(cfg, "SWEEP_JSON", tmp_path / "spitogatos_sweep.json")
    monkeypatch.setattr(cfg, "LAST_RUN_JSON", tmp_path / "spitogatos_last_run.json")
    return fake


def _script(site: Site, pages: dict) -> Site:
    site.pages = {offset: list(a) if isinstance(a, list) else [a] for offset, a in pages.items()}
    return site


def _collect(site: Site, pages: dict) -> scrape.Sweep:
    _script(site, pages)
    return asyncio.run(scrape.collect_sweep(scrape._make_session()))


def _save_store(rows: list[dict]) -> None:
    cfg.LISTINGS_JSON.write_text(json.dumps(rows), encoding="utf-8")


def _load_store() -> dict[str, dict]:
    return {row["url"]: row for row in json.loads(cfg.LISTINGS_JSON.read_text(encoding="utf-8"))}


# ── Phase A ───────────────────────────────────────────────────────────────────

def test_pass_that_reaches_an_empty_page_is_complete(site):
    sweep = _collect(site, {0: _page([1, 2], total=2), 30: _page([], total=2)})
    assert sweep.reached_end and not sweep.blocked
    assert sorted(sweep.rows) == [_url(1), _url(2)] and sweep.total_results == 2
    assert sweep.passes == 1 and site.calls == [0, 30]
    assert scrape.sweep_trust(sweep, previous_seen=2) == (True, "ok")


def test_failed_page_is_asked_again_and_never_skipped(site):
    sweep = _collect(site, {0: [None, _page([1], total=1)], 30: _page([], total=1)})
    assert sweep.reached_end and site.calls == [0, 0, 30]

    site.calls.clear()
    sweep = _collect(site, {0: _page([1], total=40), 30: None, 60: _page([])})
    assert not sweep.reached_end
    assert site.calls == [0] + [30] * cfg.PAGE_ATTEMPTS          # offset 60 is never asked
    assert scrape.sweep_trust(sweep, previous_seen=1)[0] is False


def test_refusal_stops_the_sweep_at_once(site):
    sweep = _collect(site, {0: _page([1], total=40), 30: REFUSED, 60: _page([])})
    assert sweep.blocked and not sweep.reached_end
    assert site.calls == [0, 30]                                  # no retry, no next page, no 2nd pass
    assert scrape.sweep_trust(sweep, previous_seen=1) == (False, "refused by the site")


def test_offset_cap_leaves_the_pass_incomplete(site, monkeypatch):
    monkeypatch.setattr(cfg, "MAX_OFFSET", 90)
    sweep = _collect(site, {offset: _page([offset + 1], total=3) for offset in (0, 30, 60)})
    assert not sweep.reached_end and len(sweep.rows) == 3


def test_short_pass_is_followed_by_one_more_and_the_two_are_united(site):
    sweep = _collect(site, {
        0: [_page([1, 2], total=3), _page([2, 3], total=3)],
        30: _page([], total=3),
    })
    assert sweep.passes == 2 and sorted(sweep.rows) == [_url(1), _url(2), _url(3)]
    assert scrape.sweep_trust(sweep, previous_seen=3)[0] is True

    site.calls.clear()
    sweep = _collect(site, {0: _page([1, 2], total=10), 30: _page([], total=10)})
    assert sweep.passes == cfg.MAX_PASSES and site.calls == [0, 30] * cfg.MAX_PASSES
    assert scrape.sweep_trust(sweep, previous_seen=2) == (False, "saw 2 of 10 results")


def test_trust_needs_the_sites_count_and_a_size_like_the_previous_sweep():
    rows = {_url(i): _prop(i) for i in range(100)}
    ok = scrape.Sweep(rows=rows, reached_end=True, total_results=101)
    assert scrape.sweep_trust(ok, previous_seen=120)[0] is True     # 99% of the site, 83% of last time
    assert scrape.sweep_trust(ok, previous_seen=None)[0] is True
    unknown = scrape.Sweep(rows=rows, reached_end=True, total_results=None)
    assert scrape.sweep_trust(unknown, previous_seen=100) == (False, "the site reported no result count")
    assert scrape.sweep_trust(ok, previous_seen=200)[0] is False     # half of what was seen last time
    assert scrape.sweep_trust(scrape.Sweep(reached_end=True, total_results=5), previous_seen=5)[0] is False


def test_previous_sweep_size_comes_from_the_marker_else_from_the_store(site):
    store = {_url(1): _stored(1), _url(2): _stored(2, gone_since=NOW)}
    assert scrape.previous_sweep_size(store) == 1
    scrape.write_sweep_marker(NOW, seen=42, total_results=43, passes=2)
    assert scrape.previous_sweep_size(store) == 42


# ── Store update ──────────────────────────────────────────────────────────────

def test_new_advertisement_becomes_a_row_that_needs_its_detail():
    store: dict = {}
    changes = scrape.apply_sweep(store, {_url(7): _prop(7, price=61000, area=45, images=(5, 6))}, NOW)
    row = store[_url(7)]
    assert changes.new == [_url(7)] and changes.needs_detail == {_url(7): ("7", [5, 6])}
    assert row["price_eur"] == 61000 and row["area_sqm"] == 45
    assert row["floor"] is None and row["floor_raw_code"] == 6
    assert row["neighborhood"] == "Kypseli"
    assert row["photo_urls"] == [
        "https://m3.spitogatos.gr/5_300x220.jpg?v=20130730", "https://m3.spitogatos.gr/6_300x220.jpg?v=20130730",
    ]
    assert row["publication_date"] == "2026-11-10"
    assert row["first_publish_date"] == row["earliest_publication_date"] == "2026-05-20"
    assert row["scraped_at"] == row["updated_at"] == row["last_seen_at"] == NOW


def test_reseen_advertisement_takes_the_new_price_and_keeps_its_history():
    store = {_url(1): _stored(1, price=50000, area=40)}
    prop = _prop(1, price=47000, priceReduced=True, pricePreReduction=50000, priceChangePercentage=-6)
    changes = scrape.apply_sweep(store, {_url(1): prop}, NOW)
    row = store[_url(1)]
    assert changes.reseen == 1 and not changes.new and not changes.needs_detail
    assert changes.price_changes == [{"url": _url(1), "old": 50000, "new": 47000}]
    assert row["price_eur"] == 47000 and row["price_per_sqm"] == 1175.0
    assert row["price_changes"] == [{"at": NOW, "old": 50000, "new": 47000}]
    assert row["price_reduced"] is True and row["price_pre_reduction"] == 50000
    assert row["updated_at"] == row["last_seen_at"] == NOW
    assert row["floor"] == 6.0 and "floor_raw_code" not in row      # older rows keep their floor code
    assert row["publication_date"] == "2026-11-10"                  # raised to the latest activity
    assert row["earliest_publication_date"] == "2026-05-20"         # days on market keep counting
    assert row["search_image_ids"] == [1, 2]

    again = scrape.apply_sweep(store, {_url(1): prop}, "2026-11-13T10:00:00+00:00")
    assert not again.price_changes and len(row["price_changes"]) == 1


def test_publication_date_is_never_lowered_and_an_unchanged_price_is_left_alone():
    store = {_url(1): _stored(1, publication_date="2026-11-11", price_per_sqm=1249.0)}
    changes = scrape.apply_sweep(store, {_url(1): _prop(1)}, NOW)
    assert store[_url(1)]["publication_date"] == "2026-11-11"
    assert not changes.price_changes and "price_changes" not in store[_url(1)]
    assert store[_url(1)]["price_per_sqm"] == 1249.0


def test_changed_photo_set_or_leftover_thumbnails_ask_for_the_detail_again():
    store = {
        _url(1): _stored(1, images=(1, 2)),
        _url(2): _stored(2, photo_urls=["https://m3.spitogatos.gr/9_300x220.jpg?v=20130730"]),
        _url(3): _stored(3, images=(1, 2), search_image_ids=[7, 8]),
    }
    rows = {_url(1): _prop(1, images=(1, 2, 3)), _url(2): _prop(2, images=(9,)), _url(3): _prop(3, images=(8, 7))}
    changes = scrape.apply_sweep(store, rows, NOW)
    assert changes.photos_changed == [_url(1)]
    assert changes.needs_detail == {_url(1): ("1", [1, 2, 3]), _url(2): ("2", [9])}


def test_gone_is_stamped_once_and_cleared_when_the_advertisement_returns():
    store = {_url(1): _stored(1), _url(2): _stored(2)}
    assert scrape.mark_gone(store, {_url(1)}, NOW) == [_url(2)]
    assert store[_url(2)]["gone_since"] == NOW and "gone_since" not in store[_url(1)]
    assert scrape.mark_gone(store, {_url(1)}, "2026-11-13T10:00:00+00:00") == []
    assert store[_url(2)]["gone_since"] == NOW

    changes = scrape.apply_sweep(store, {_url(2): _prop(2)}, "2026-11-14T10:00:00+00:00")
    assert changes.returned == [_url(2)] and "gone_since" not in store[_url(2)]


def test_detail_never_lowers_the_publication_date():
    row = _stored(1, publication_date="2026-11-10")
    scrape._apply_detail(row, {"title": "Flat", "publication_date": "2026-09-01"}, [4, 5], NOW)
    assert row["title"] == "Flat" and row["publication_date"] == "2026-11-10"
    assert row["search_image_ids"] == [4, 5] and row["detail_fetched_at"] == NOW


def test_marker_is_written_atomically(site):
    scrape.write_sweep_marker("2026-11-09T10:00:00+00:00", seen=42, total_results=43, passes=2)
    marker = json.loads(cfg.SWEEP_JSON.read_text())
    assert marker["started_at"] == "2026-11-09T10:00:00+00:00" and marker["seen"] == 42
    assert marker["total_results"] == 43 and marker["passes"] == 2
    assert not cfg.SWEEP_JSON.with_name(cfg.SWEEP_JSON.name + ".tmp").exists()


# ── Whole runs ────────────────────────────────────────────────────────────────

def _detail(*image_ids) -> dict:
    return {"data": {
        "title": "Apartment", "description": "Bright flat",
        "images": [{"large": f"https://m3.spitogatos.gr/{i}_900x675.jpg"} for i in image_ids],
        "modified": "2026-11-11 08:00:00",
    }}


def test_run_adds_updates_and_marks_gone(site):
    _save_store([_stored(1, price=50000), _stored(2)])
    _script(site, {0: _page([_prop(1, price=48000), _prop(3, images=(7, 8))], total=2), 30: _page([], total=2)})
    site.details = {"3": _detail(7, 8)}

    summary = asyncio.run(scrape.run())

    store = _load_store()
    assert summary["status"] == "ok" and summary["trusted"] is True
    assert (summary["new"], summary["reseen"], summary["price_changes"], summary["gone"]) == (1, 1, 1, 1)
    assert summary["changes"]["gone"] == [_url(2)] and summary["changes"]["new"] == [_url(3)]
    assert store[_url(1)]["price_eur"] == 48000
    assert store[_url(2)]["gone_since"]
    assert store[_url(3)]["title"] == "Apartment" and store[_url(3)]["search_image_ids"] == [7, 8]
    assert store[_url(3)]["photo_urls"] == [f"https://m3.spitogatos.gr/{i}_900x675.jpg" for i in (7, 8)]
    assert site.detail_calls == ["3"] and summary["details_stored"] == 1

    marker = json.loads(cfg.SWEEP_JSON.read_text())
    assert marker["seen"] == 2 and marker["started_at"] == summary["started_at"]
    assert store[_url(1)]["updated_at"] >= marker["started_at"]     # re-seen rows stay "still listed"
    assert json.loads(cfg.LAST_RUN_JSON.read_text())["status"] == "ok"


def test_dry_run_reports_the_changes_and_writes_nothing(site):
    _save_store([_stored(1, price=50000), _stored(2)])
    before = cfg.LISTINGS_JSON.read_text()
    _script(site, {0: _page([_prop(1, price=48000), _prop(3)], total=2), 30: _page([], total=2)})

    summary = asyncio.run(scrape.run(dry_run=True))

    assert summary["status"] == "dry_run" and summary["trusted"] is True
    assert (summary["new"], summary["price_changes"], summary["gone"], summary["detail_fetches"]) == (1, 1, 1, 1)
    assert cfg.LISTINGS_JSON.read_text() == before
    assert not cfg.SWEEP_JSON.exists() and not cfg.LAST_RUN_JSON.exists()
    assert site.detail_calls == []


def test_incomplete_sweep_marks_nothing_gone_but_still_stores_what_it_saw(site):
    _save_store([_stored(1), _stored(2)])
    _script(site, {0: _page([_prop(1, price=48000), _prop(3, images=(7,))], total=40), 30: None})
    site.details = {"3": _detail(7)}

    summary = asyncio.run(scrape.run())

    store = _load_store()
    assert summary["status"] == "incomplete" and summary["trusted"] is False and summary["gone"] == 0
    assert "gone_since" not in store[_url(2)]
    assert store[_url(1)]["price_eur"] == 48000 and store[_url(3)]["title"] == "Apartment"
    assert not cfg.SWEEP_JSON.exists()


def test_refusal_during_the_search_saves_progress_and_asks_for_nothing_more(site):
    _save_store([_stored(1), _stored(2)])
    _script(site, {0: _page([_prop(1, price=48000), _prop(3)], total=40), 30: REFUSED})

    with pytest.raises(scrape.BlockedError):
        asyncio.run(scrape.run())

    store = _load_store()
    assert site.calls == [0, 30] and site.detail_calls == []
    assert store[_url(1)]["price_eur"] == 48000 and _url(3) in store
    assert "gone_since" not in store[_url(2)] and not cfg.SWEEP_JSON.exists()
    last_run = json.loads(cfg.LAST_RUN_JSON.read_text())
    assert last_run["status"] == "blocked" and last_run["trusted"] is False


def test_refusal_during_detail_fetches_stops_the_rest(site):
    _save_store([])
    _script(site, {0: _page([3, 4, 5], total=3), 30: _page([], total=3)})
    site.details = {"3": _detail(1), "4": REFUSED, "5": _detail(1)}

    with pytest.raises(scrape.BlockedError):
        asyncio.run(scrape.run())

    store = _load_store()
    assert site.detail_calls == ["3", "4"]                          # 5 is never asked
    assert store[_url(3)]["title"] == "Apartment" and store[_url(5)]["title"] is None
    assert json.loads(cfg.LAST_RUN_JSON.read_text())["status"] == "blocked"
    assert cfg.SWEEP_JSON.exists()                                  # the search sweep itself was whole


def test_unreadable_store_is_not_replaced_by_a_fresh_sweep(site):
    cfg.LISTINGS_JSON.write_text("{" + "x" * 2000, encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be read"):
        asyncio.run(scrape.run())
    assert site.calls == []
