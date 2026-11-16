"""
Runs the Spitogatos collector and merges the results into data/listings.json.

The collector runs in its **own subprocess** (``python -m greeceapt.collect.scrape_spitogatos``),
so it owns its event loop. It talks to Spitogatos' public JSON APIs as an ordinary HTTP client:
no browser, no impersonation (decided 2026-11-16). A refusal (403/405/429) stops the run.

XE is no longer collected (its robots.txt forbids the search pages and it blocked this
machine; the collector was removed on 2026-11-16). The XE rows already in
``data/xe_listings.json`` are still merged, so older flats keep their history, but an XE
advertisement is never treated as live (``scoring.availability``).

Merge phase (``greeceapt.collect.util``): the outputs are merged into one JSON with
**Quad-Lock** dedupe (pHash + metadata). Site-side search filters apply on each portal; merge
still moves rows with parseable publication older than ``STALE_DAYS`` to
``stale_listings.json`` (see ``is_listing_stale_by_publication``).
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import httpx

from greeceapt.storage import paths
from greeceapt.collect.floor_resolver import apply_floor_resolution
from greeceapt.storage.neighborhoods import canonical_neighborhood
from greeceapt.storage.util import normalize_listing_url
from greeceapt.collect import dedupe
from greeceapt.collect import opportunity_dedupe
from greeceapt.collect import util as scraper_util
from greeceapt.collect.opportunity_registry import OpportunityRegistry, attach_opportunity_ids
from greeceapt.collect.spitogatos.config import EXIT_BLOCKED

# Aliased from ``paths`` so tests can ``monkeypatch.setattr(run_all, "DATA_DIR", tmp)``.
PROJECT_ROOT = paths.PROJECT_ROOT
DATA_DIR = paths.DATA_DIR
OUTPUT_JSON = paths.LISTINGS_JSON
DELETED_JSON = paths.DELETED_LISTINGS_JSON
STALE_JSON = paths.STALE_LISTINGS_JSON
XE_JSON = paths.XE_LISTINGS_JSON
SPITOGATOS_JSON = paths.SPITOGATOS_LISTINGS_JSON
REVIEW_JSON = paths.DATA_DIR / "review_pairs.json"
OPPORTUNITY_REVIEW_JSON = paths.OPPORTUNITY_REVIEW_JSON
OPPORTUNITY_DECISIONS_JSON = paths.OPPORTUNITY_DECISIONS_JSON
FINGERPRINT_LOG_EVERY = 100   # listings between "Fingerprints: x / y" progress lines
OPPORTUNITY_REGISTRY_DB = paths.OPPORTUNITY_REGISTRY_DB

logger = logging.getLogger(__name__)
DEDUPE_POLICY_VERSION = "phash-v2-conservative"

# source name → (module run as a subprocess, log label)
SCRAPERS = {
    "spitogatos": ("greeceapt.collect.scrape_spitogatos", "Spitogatos"),
}


class ScraperBlocked(RuntimeError):
    """A site refused its collector; the run stops there, before merging or asking for more."""


async def _run_scraper_subprocess(module: str, label: str) -> None:
    """Run a scraper module in a child process so it owns its own event loop."""
    logger.info("Subprocess %s: %s -m %s", label, sys.executable, module)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        cwd=str(PROJECT_ROOT),
    )
    rc = await proc.wait()
    if rc == EXIT_BLOCKED:
        raise ScraperBlocked(f"{label} refused the collector (exit code {rc})")
    if rc != 0:
        raise RuntimeError(f"{label} scraper subprocess exited with code {rc}")
    logger.info("%s subprocess finished OK (exit 0).", label)


async def _merge(*, network_enabled: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
    all_raw: list[dict] = []
    for path, source in [(XE_JSON, "xe"), (SPITOGATOS_JSON, "spitogatos")]:
        if not path.exists():
            logger.warning("Output file missing, skipping: %s", path)
            continue
        with path.open(encoding="utf-8") as f:
            rows = json.load(f)
        for row in rows:
            row.setdefault("source", source)
            # Floors are resolved once, here: after Spitogatos detail enrichment (which
            # supplies the description) and before dedupe compares floors.
            apply_floor_resolution(row)
            canonicalize_listing_neighborhood(row)
        all_raw.extend(rows)
        logger.info("Loaded %s listings from %s", len(rows), path.name)

    url_cache = scraper_util.load_url_hash_cache_from_listings_json(OUTPUT_JSON)
    total_urls = sum(len(r.get("photo_urls") or []) for r in all_raw)
    cached_hits = sum(
        1 for r in all_raw for u in (r.get("photo_urls") or []) if u in url_cache
    )
    logger.info(
        "Computing fingerprints for %s listings — %s/%s image URLs already cached",
        len(all_raw), cached_hits, total_urls,
    )

    sem = asyncio.Semaphore(scraper_util.IMAGE_CONCURRENCY)
    fingerprint_started = time.perf_counter()
    if network_enabled:
        done = 0

        async def _fingerprint(row: dict, client: httpx.AsyncClient) -> dict:
            nonlocal done
            result = await scraper_util.compute_listing_photo_fingerprints(
                row.get("photo_urls") or [], url_cache, sem, client,
            )
            done += 1
            if done % FINGERPRINT_LOG_EVERY == 0 or done == len(all_raw):
                logger.info("Fingerprints: %s / %s listings", done, len(all_raw))
            return result

        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            fingerprint_results = await asyncio.gather(*(_fingerprint(r, client) for r in all_raw))
    else:
        fingerprint_results = [
            {url: url_cache[url] for url in (row.get("photo_urls") or []) if url in url_cache}
            for row in all_raw
        ]
    fingerprint_elapsed = time.perf_counter() - fingerprint_started

    for listing, fingerprints_by_url in zip(all_raw, fingerprint_results):
        listing["photo_fingerprints"] = {
            url: fingerprint.to_json()
            for url, fingerprint in fingerprints_by_url.items()
        }
        listing["photo_url_hashes"] = {
            url: f"{fingerprint.full:016x}"
            for url, fingerprint in fingerprints_by_url.items()
            if fingerprint.full is not None
        }
        listing["image_hashes"] = sorted(set(listing["photo_url_hashes"].values()))
        listing.setdefault("source_urls", [listing.get("url", "")])

    # Persist outside listings.json so wiping the merge output never forces a re-download.
    scraper_util.save_standalone_phash_cache(url_cache)
    logger.info(
        "Fingerprint cache persisted: %s URLs → %s (%.3fs, network_enabled=%s)",
        len(url_cache), paths.PHASH_CACHE_JSON.name, fingerprint_elapsed, network_enabled,
    )

    logger.info(
        "Deduplicating %s listings — blocking (geo %.0fm grid ∪ area/price) + one-to-one "
        "photo matching (D_H<%s) + null-aware metadata gate…",
        len(all_raw), dedupe.GEO_CELL_M, dedupe.PHASH_HAMMING_THRESHOLD,
    )
    t0 = time.monotonic()
    result = dedupe.find_duplicate_groups(all_raw)
    elapsed = time.monotonic() - t0

    deduped: list[dict] = []
    merged_pairs: list[dict] = []
    for group in result.groups:
        survivor = dict(all_raw[group[0]])
        group_member_urls = [all_raw[index].get("url") or all_raw[index].get("id") for index in group]
        authorizing_evidence = [
            dedupe.pair_evidence_record(all_raw[left], all_raw[right], result.pair_evidence[(left, right)])
            for position, left in enumerate(group)
            for right in group[position + 1:]
            if (left, right) in result.pair_evidence
            and result.pair_evidence[(left, right)].confidence == "HIGH"
        ]
        for other_idx in group[1:]:
            absorbed = all_raw[other_idx]
            survivor_snapshot = dict(survivor)
            scraper_util.merge_duplicate_listing_into_kept(survivor, absorbed)
            merged_pairs.append({
                "kept": survivor_snapshot,
                "absorbed": dict(absorbed),
                "dedupe_policy_version": DEDUPE_POLICY_VERSION,
                "group_member_urls": group_member_urls,
                "authorizing_evidence": authorizing_evidence,
            })
            logger.info(
                "Merged %s into %s", absorbed.get("url"), survivor.get("url"),
            )
        deduped.append(survivor)

    logger.info(
        "Deduplication: %s raw → %s unique (%s absorbed) in %.1fs — "
        "%s candidate pairs scored, %s high-confidence, %s boilerplate photos in %s families ignored, "
        "%s bridge components blocked, %s pairs flagged for review; pair scoring %.3fs; "
        "candidates by source=%s",
        len(all_raw), len(deduped), len(merged_pairs), elapsed,
        result.candidate_pairs, result.high_confidence_pairs,
        result.blacklisted_photos, result.blacklist_family_count,
        result.split_components, len(result.review_pairs), result.pair_scoring_seconds,
        result.candidate_pairs_by_source,
    )

    if result.review_pairs:
        with REVIEW_JSON.open("w", encoding="utf-8") as f:
            json.dump(result.review_pairs, f, indent=2, ensure_ascii=False)
        logger.info("Borderline pairs written to %s for inspection.", REVIEW_JSON)
    else:
        REVIEW_JSON.unlink(missing_ok=True)

    # Second pass: collapse advertisements of the same flat (e.g. several agencies) into one
    # investment opportunity before Qwen sees them. Decisions fail closed when malformed.
    decisions = opportunity_dedupe.load_decisions(OPPORTUNITY_DECISIONS_JSON)
    blocked_components = [
        frozenset(normalize_listing_url(url) or str(url) for url in review["component_ids"])
        for review in result.review_pairs
        if review.get("reason_code") == "incomplete_high_clique"
    ]
    collapse = opportunity_dedupe.collapse_opportunities(
        deduped, decisions, blocked_components=blocked_components
    )
    deduped = collapse.listings
    merged_pairs.extend(
        {"opportunity_merge": record, "dedupe_policy_version": record["policy"]}
        for record in collapse.merged
    )
    if collapse.reviews:
        _write_json_atomic(OPPORTUNITY_REVIEW_JSON, collapse.reviews)
    else:
        OPPORTUNITY_REVIEW_JSON.unlink(missing_ok=True)
    logger.info(
        "Opportunity collapse: %s opportunities, %s multi-ad merges, %s pairs pending review → %s",
        len(deduped), len(collapse.merged), len(collapse.reviews), OPPORTUNITY_REVIEW_JSON.name,
    )

    fresh = [r for r in deduped if not scraper_util.is_listing_stale_by_publication(r)]
    stale = [r for r in deduped if scraper_util.is_listing_stale_by_publication(r)]
    logger.info(
        "Date filter: %s fresh (within %s days), %s stale removed",
        len(fresh), scraper_util.STALE_DAYS, len(stale),
    )

    fresh.sort(key=lambda r: (r.get("price_eur") is None, r.get("price_eur") or 0))
    return fresh, stale, merged_pairs


def canonicalize_listing_neighborhood(row: dict) -> None:
    """Map spelling variants to the canonical area name, keeping the original."""
    raw = row.get("neighborhood_raw") or row.get("neighborhood")
    if raw is not None:
        row["neighborhood_raw"] = raw
    row["neighborhood"] = canonical_neighborhood(raw)


def _write_json_atomic(path, payload) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def assign_opportunity_ids(listings: list[dict], *, now: datetime) -> OpportunityRegistry:
    """Attach persistent ``opportunity_id``s; the caller commits after outputs are written."""
    registry = OpportunityRegistry(OPPORTUNITY_REGISTRY_DB)
    attach_opportunity_ids(registry, listings, now=now)
    return registry


async def main() -> None:
    sources = list(SCRAPERS)
    labels = [SCRAPERS[name][1] for name in sources]
    logger.info("=== run_all: collecting from %s ===", " + ".join(labels))
    started_at = datetime.now(timezone.utc)

    results = await asyncio.gather(
        *(_run_scraper_subprocess(*SCRAPERS[name]) for name in sources),
        return_exceptions=True,
    )

    for name, result in zip(labels, results):
        if isinstance(result, Exception):
            logger.error("%s scraper failed: %s", name, result)
        else:
            logger.info("%s scraper finished OK.", name)
    for result in results:
        if isinstance(result, ScraperBlocked):
            # No merge, no photo downloads, no scoring: nothing else is requested after a refusal.
            raise result

    logger.info("Merging results → %s", OUTPUT_JSON)
    listings, stale_listings, merged_pairs = await _merge(network_enabled=True)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    registry = assign_opportunity_ids(listings, now=started_at)
    try:
        with OUTPUT_JSON.open("w", encoding="utf-8") as f:
            json.dump(listings, f, indent=2, ensure_ascii=False)

        with DELETED_JSON.open("w", encoding="utf-8") as f:
            json.dump(merged_pairs, f, indent=2, ensure_ascii=False)

        with STALE_JSON.open("w", encoding="utf-8") as f:
            json.dump(stale_listings, f, indent=2, ensure_ascii=False)
        registry.commit()
    finally:
        registry.close()

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info(
        "Done. %s fresh listings → %s  |  %s stale → %s  |  %s merged pairs → %s  (%.0fs)",
        len(listings), OUTPUT_JSON,
        len(stale_listings), STALE_JSON,
        len(merged_pairs), DELETED_JSON,
        elapsed,
    )


async def merge_only() -> None:
    """Re-run just the merge/dedupe over existing per-site JSON — no network scrape."""
    logger.info("=== run_all: merge-only (no scrape) ===")
    listings, stale_listings, merged_pairs = await _merge(network_enabled=False)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    registry = assign_opportunity_ids(listings, now=datetime.now(timezone.utc))
    try:
        with OUTPUT_JSON.open("w", encoding="utf-8") as f:
            json.dump(listings, f, indent=2, ensure_ascii=False)
        with DELETED_JSON.open("w", encoding="utf-8") as f:
            json.dump(merged_pairs, f, indent=2, ensure_ascii=False)
        with STALE_JSON.open("w", encoding="utf-8") as f:
            json.dump(stale_listings, f, indent=2, ensure_ascii=False)
        registry.commit()
    finally:
        registry.close()
    logger.info(
        "merge-only done. %s fresh, %s stale, %s merged pairs.",
        len(listings), len(stale_listings), len(merged_pairs),
    )


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    if "--merge-only" in sys.argv:
        asyncio.run(merge_only())
    else:
        asyncio.run(main())
