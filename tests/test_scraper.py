"""Tests for greeceapt.collect (imports and pure helpers; no live scrape)."""

from greeceapt.collect.scrape_spitogatos import SEARCH_API
from greeceapt.collect.util import (
    upsert_listing_in_url_store,
)


def test_spitogatos_search_api_url():
    assert "spitogatos" in SEARCH_API.lower()


def test_upsert_listing_in_url_store_touch_vs_insert():
    store: dict = {}
    u = "https://example.com/a"
    assert upsert_listing_in_url_store(store, u, {"url": u, "price_eur": 1, "scraped_at": "t0"}) == "inserted"
    assert store[u]["price_eur"] == 1
    first_updated = store[u].get("updated_at")
    assert first_updated
    assert upsert_listing_in_url_store(store, u, {"url": u, "price_eur": 99, "scraped_at": "t1"}) == "touched"
    assert store[u]["price_eur"] == 1
    assert store[u]["updated_at"] != first_updated


# ── Duplicate merge: price and area come from the same advertisement ──────────

from greeceapt.collect.util import merge_duplicate_listing_into_kept


def _fresh_listing(url, price, area):
    return {
        "url": url, "source": "spitogatos", "price_eur": price, "area_sqm": area,
        "publication_date": "2099-01-01", "photo_urls": [], "source_urls": [url],
    }


def test_merge_takes_price_and_area_together_from_the_cheaper_ad():
    kept = _fresh_listing("https://a/1", 52_000.0, 50.0)
    merge_duplicate_listing_into_kept(kept, _fresh_listing("https://b/1", 50_000.0, 46.0))
    assert (kept["price_eur"], kept["area_sqm"]) == (50_000.0, 46.0)


def test_merge_records_every_advertised_offer_once():
    kept = _fresh_listing("https://a/1", 52_000.0, 50.0)
    merge_duplicate_listing_into_kept(kept, _fresh_listing("https://b/1", 50_000.0, 46.0))
    merge_duplicate_listing_into_kept(kept, _fresh_listing("https://c/1", 55_000.0, 47.0))
    offers = {(o["url"], o["price_eur"], o["area_sqm"]) for o in kept["advertised_offers"]}
    assert offers == {
        ("https://a/1", 52_000.0, 50.0),
        ("https://b/1", 50_000.0, 46.0),
        ("https://c/1", 55_000.0, 47.0),
    }
