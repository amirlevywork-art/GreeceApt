"""Spitogatos sale sweep: search pages (Phase A) → store update → detail fetches (Phase B).

Every run walks the whole search result and brings ``data/spitogatos_listings.json`` in line
with the site:

* a new advertisement is added, then enriched from the detail API;
* a re-seen one gets ``updated_at`` / ``last_seen_at``, its current price (a change is appended
  to its ``price_changes``), the price-reduction fields and a refreshed ``publication_date``;
  its detail is fetched again only when its photo set changed or its first fetch had failed;
* a stored one that a **trusted** sweep did not see gets ``gone_since`` (cleared when it is
  seen again).

A sweep is trusted when it was not refused, reached the last page, saw at least
``MIN_COVERAGE`` of the site's own ``totalResults`` and at least ``MIN_VS_PREVIOUS`` of what the
previous trusted sweep saw. Result pages shift while they are read, so a pass that comes up
short is followed by one more pass and the two are united.

HTTP 403/405/429 means the site refuses the collector. The run then stops asking, saves what
it has, writes ``spitogatos_last_run.json`` and raises ``BlockedError`` (exit code
``EXIT_BLOCKED`` from the CLI). Nothing is retried.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from . import config as cfg
from . import parse as P

logger = logging.getLogger(__name__)

# An ordinary HTTP client that says what it is (decided 2026-11-16): no browser impersonation, no
# borrowed browser identity, no bot-detection tokens. A refusal stops the run (BLOCK_STATUSES).
_USER_AGENT = "GreeceApt/1.0 (personal flat research; runs only when its owner starts it)"

BLOCK_STATUSES = frozenset({403, 405, 429})
EXIT_BLOCKED = cfg.EXIT_BLOCKED
PRICE_TOLERANCE_EUR = 0.5
_THUMBNAIL_MARK = "_300x220"
_IMAGE_ID_RE = re.compile(r"/(\d+)_\d+x\d+\.")


class BlockedError(RuntimeError):
    """Spitogatos refused a request (HTTP 403/405/429): stop asking."""


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "accept": "application/json",
        "accept-language": "en",
        "x-locale": "en",
        "x-mdraw": "1",
        "content-type": "application/json",
        "user-agent": _USER_AGENT,
    })
    return s


def _refused(tag: str, status: int, raise_on_block: bool) -> None:
    logger.error("%s status=%s — Spitogatos refused the request.", tag, status)
    if raise_on_block:
        raise BlockedError(f"{tag}: HTTP {status}")


async def _post_json(
    session: requests.Session, url: str, body: dict, *, tag: str, raise_on_block: bool = False
) -> Any | None:
    """POST JSON; parsed root, or None on a transport / HTTP / JSON failure.

    With ``raise_on_block`` a refusal (403/405/429) raises ``BlockedError`` instead.
    """
    try:
        resp = await asyncio.to_thread(session.post, url, data=json.dumps(body), timeout=30)
    except Exception as exc:
        logger.error("%s: %s", tag, exc)
        return None
    if not resp.ok:
        if resp.status_code in BLOCK_STATUSES:
            _refused(tag, resp.status_code, raise_on_block)
        else:
            logger.warning("%s status=%s", tag, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        logger.error("%s JSON parse error", tag)
        return None


async def _get_json(
    session: requests.Session, url: str, *, tag: str, raise_on_block: bool = False
) -> Any | None:
    """GET; parsed root or None. With ``raise_on_block`` a refusal raises ``BlockedError``."""
    try:
        resp = await asyncio.to_thread(session.get, url, timeout=30)
    except Exception as exc:
        logger.warning("%s: %s", tag, exc)
        return None
    if not resp.ok:
        if resp.status_code in BLOCK_STATUSES:
            _refused(tag, resp.status_code, raise_on_block)
        else:
            logger.warning("%s status=%s", tag, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _props_from_search_root(data: Any) -> list:
    if not isinstance(data, dict):
        return []
    return data.get("data") or data.get("properties") or data.get("results") or []


def listing_url(pid: Any) -> str:
    return f"https://www.spitogatos.gr/en/property/11{pid}"


# ── Phase A: the search sweep ─────────────────────────────────────────────────

@dataclass
class Sweep:
    """Search rows of one run (all passes united), keyed by listing URL."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    reached_end: bool = False
    blocked: bool = False
    total_results: int | None = None
    passes: int = 0
    pages: int = 0


async def _search_page(session: requests.Session, offset: int) -> Any | None:
    """One results page, asked again after a plain failure; ``BlockedError`` on a refusal."""
    payload = {**cfg.SEARCH_PAYLOAD, "offset": offset}
    for attempt in range(1, cfg.PAGE_ATTEMPTS + 1):
        data = await _post_json(
            session, cfg.SEARCH_API, payload, tag=f"Phase A offset={offset}", raise_on_block=True
        )
        if data is not None:
            return data
        if attempt < cfg.PAGE_ATTEMPTS:
            await asyncio.sleep(random.uniform(3.0, 6.0))
    return None


async def _sweep_pass(session: requests.Session, sweep: Sweep) -> None:
    """Read the result pages once, adding their rows to ``sweep``.

    ``reached_end`` is set only by an empty page: a page that keeps failing, a refusal or the
    offset cap all leave the pass incomplete.
    """
    sweep.passes += 1
    sweep.reached_end = False
    for offset in range(0, cfg.MAX_OFFSET, cfg.PAGE_SIZE):
        logger.info("Phase A pass %s — offset=%s  seen=%s  total=%s",
                    sweep.passes, offset, len(sweep.rows), sweep.total_results or "?")
        try:
            data = await _search_page(session, offset)
        except BlockedError as exc:
            logger.error("Phase A stopped: %s", exc)
            sweep.blocked = True
            return
        if data is None:
            logger.error("Phase A: offset=%s failed %s times; the pass is incomplete.", offset, cfg.PAGE_ATTEMPTS)
            return
        sweep.pages += 1
        total = (data.get("pagination") or {}).get("totalResults") if isinstance(data, dict) else None
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            sweep.total_results = total
        props = _props_from_search_root(data)
        if not props:
            logger.info("Phase A — empty page at offset=%s, pass complete.", offset)
            sweep.reached_end = True
            return
        for prop in props:
            if isinstance(prop, dict) and prop.get("id"):
                sweep.rows[listing_url(prop["id"])] = prop
        await asyncio.sleep(random.uniform(1.5, 3.0))
    logger.warning("Phase A: offset cap %s reached before the last page.", cfg.MAX_OFFSET)


async def collect_sweep(session: requests.Session) -> Sweep:
    """Sweep the search; repeat once when a complete pass saw fewer rows than the site reports."""
    sweep = Sweep()
    for _ in range(cfg.MAX_PASSES):
        await _sweep_pass(session, sweep)
        if sweep.blocked or not sweep.reached_end:
            break
        if sweep.total_results is None or len(sweep.rows) >= sweep.total_results:
            break
        logger.info(
            "Phase A: %s of %s results seen after pass %s.", len(sweep.rows), sweep.total_results, sweep.passes
        )
    return sweep


def sweep_trust(sweep: Sweep, *, previous_seen: int | None) -> tuple[bool, str]:
    """Whether a listing missing from this sweep can be taken as gone, and why (not)."""
    seen = len(sweep.rows)
    if sweep.blocked:
        return False, "refused by the site"
    if not sweep.reached_end:
        return False, "did not reach the last page"
    if seen == 0:
        return False, "no results"
    if sweep.total_results is None:
        return False, "the site reported no result count"
    if seen < cfg.MIN_COVERAGE * sweep.total_results:
        return False, f"saw {seen} of {sweep.total_results} results"
    if previous_seen and seen < cfg.MIN_VS_PREVIOUS * previous_seen:
        return False, f"saw {seen} results, the previous sweep saw {previous_seen}"
    return True, "ok"


def previous_sweep_size(store: dict[str, dict[str, Any]]) -> int:
    """Size of the last trusted sweep; before the first one, the stored listings not marked gone."""
    try:
        seen = json.loads(cfg.SWEEP_JSON.read_text(encoding="utf-8")).get("seen")
    except (OSError, ValueError, AttributeError):
        seen = None
    if isinstance(seen, int) and not isinstance(seen, bool) and seen > 0:
        return seen
    return sum(1 for row in store.values() if not row.get("gone_since"))


# ── Bringing the store in line with a sweep ───────────────────────────────────

def _date_part(value: Any) -> str | None:
    dt = P.parse_spitogatos_datetime(value)
    return dt.date().isoformat() if dt is not None else None


def _latest_date(prop: dict[str, Any]) -> str | None:
    dates = [d for d in (_date_part(prop.get(key)) for key in ("modified", "uploaded", "firstPublishDate")) if d]
    return max(dates) if dates else None


def _image_ids(prop: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for value in prop.get("imageIds") or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def _stored_image_ids(row: dict[str, Any]) -> set[int]:
    """Photo ids the stored photos were fetched for (older rows: read off the photo URLs)."""
    stored = row.get("search_image_ids")
    if stored is not None:
        return {int(value) for value in stored}
    return {int(m.group(1)) for url in row.get("photo_urls") or [] if (m := _IMAGE_ID_RE.search(str(url)))}


def _has_thumbnails(row: dict[str, Any]) -> bool:
    """Search thumbnails still in place of detail photos: the detail fetch never succeeded."""
    return any(_THUMBNAIL_MARK in str(url) for url in row.get("photo_urls") or [])


def _price_fields(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "price_reduced": bool(prop.get("priceReduced")),
        "price_pre_reduction": prop.get("pricePreReduction"),
        "price_change_percentage": prop.get("priceChangePercentage"),
    }


def _new_row(prop: dict[str, Any], now_iso: str) -> dict[str, Any]:
    geo = prop.get("geography")
    neighborhood, municipality = P.neighborhood_from_geo(geo if isinstance(geo, dict) else {})
    if isinstance(geo, str) and not neighborhood:
        neighborhood = geo.split("(")[0].strip() or None
    photo_urls = [f"https://m3.spitogatos.gr/{iid}{_THUMBNAIL_MARK}.jpg?v=20130730" for iid in _image_ids(prop)]
    first_published = _date_part(prop.get("firstPublishDate"))
    return {
        "url": listing_url(prop["id"]),
        "title": None,
        "description": None,
        "price_eur": prop.get("price"),
        "price_per_sqm": None,
        **_price_fields(prop),
        "area_sqm": prop.get("sq_meters"),
        "municipality": municipality,
        "neighborhood": neighborhood,
        "address_raw": None,
        "bedrooms": prop.get("rooms"),
        "bathrooms": prop.get("no_of_bathrooms"),
        # floorNumber is an offset code (1 basement, 2 semi-basement, 6 = 1st ...);
        # run_all resolves it together with the description.
        "floor": None,
        "floor_raw_code": prop.get("floorNumber"),
        "year_built": None,
        "renovation_year": None,
        "heating_type": None,
        "energy_class": None,
        "photo_urls": photo_urls,
        "photos_count": len(photo_urls),
        "publication_date": _latest_date(prop),
        "first_publish_date": first_published,
        "earliest_publication_date": first_published,
        "latitude": P.clean_float(prop.get("latitude")),
        "longitude": P.clean_float(prop.get("longitude")),
        "scraped_at": now_iso,
        "updated_at": now_iso,
        "last_seen_at": now_iso,
    }


def _refresh_price(row: dict[str, Any], prop: dict[str, Any], now_iso: str) -> dict[str, Any] | None:
    """Take the advertised price; a real change is appended to the row's ``price_changes``."""
    new, old = P.clean_float(prop.get("price")), P.clean_float(row.get("price_eur"))
    area = P.clean_float(prop.get("sq_meters"))
    area_changed = bool(area and area > 0 and area != P.clean_float(row.get("area_sqm")))
    if area_changed:
        row["area_sqm"] = prop.get("sq_meters")
    change = None
    if new and new > 0 and (old is None or abs(new - old) > PRICE_TOLERANCE_EUR):
        change = {"at": now_iso, "old": row.get("price_eur"), "new": prop.get("price")}
        row.setdefault("price_changes", []).append(change)
        row["price_eur"] = prop.get("price")
    price, stored_area = P.clean_float(row.get("price_eur")), P.clean_float(row.get("area_sqm"))
    if (change or area_changed) and price and stored_area and stored_area > 0:
        row["price_per_sqm"] = round(price / stored_area, 2)
    row.update(_price_fields(prop))
    return change


def _refresh_dates(row: dict[str, Any], prop: dict[str, Any]) -> None:
    """Keep ``publication_date`` at the ad's latest activity so a live ad never ages out."""
    latest = _latest_date(prop)
    if latest and (not row.get("publication_date") or latest > str(row["publication_date"])[:10]):
        row["publication_date"] = latest
    first_published = _date_part(prop.get("firstPublishDate"))
    if first_published:
        row["first_publish_date"] = first_published
        row["earliest_publication_date"] = first_published


@dataclass
class SweepChanges:
    new: list[str] = field(default_factory=list)
    reseen: int = 0
    price_changes: list[dict[str, Any]] = field(default_factory=list)
    photos_changed: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    # url → (property id, search image ids): rows whose detail must be fetched
    needs_detail: dict[str, tuple[str, list[int]]] = field(default_factory=dict)


def apply_sweep(store: dict[str, dict[str, Any]], rows: dict[str, dict[str, Any]], now_iso: str) -> SweepChanges:
    """Update ``store`` in place from the sweep's search rows; no network, no file access."""
    changes = SweepChanges()
    for url, prop in rows.items():
        pid, image_ids = str(prop["id"]), _image_ids(prop)
        row = store.get(url)
        if row is None:
            store[url] = _new_row(prop, now_iso)
            changes.new.append(url)
            changes.needs_detail[url] = (pid, image_ids)
            continue

        changes.reseen += 1
        row["updated_at"] = now_iso
        row["last_seen_at"] = now_iso
        if row.pop("gone_since", None):
            changes.returned.append(url)
        price_change = _refresh_price(row, prop, now_iso)
        if price_change:
            changes.price_changes.append({"url": url, "old": price_change["old"], "new": price_change["new"]})
        _refresh_dates(row, prop)

        if _has_thumbnails(row):
            changes.needs_detail[url] = (pid, image_ids)
        elif image_ids and set(image_ids) != _stored_image_ids(row):
            changes.photos_changed.append(url)
            changes.needs_detail[url] = (pid, image_ids)
        elif row.get("search_image_ids") is None and image_ids:
            row["search_image_ids"] = image_ids
    return changes


def mark_gone(store: dict[str, dict[str, Any]], seen: set[str], now_iso: str) -> list[str]:
    """Stamp ``gone_since`` on stored listings a trusted sweep did not see; returns the new ones."""
    gone = []
    for url, row in store.items():
        if url not in seen and not row.get("gone_since"):
            row["gone_since"] = now_iso
            gone.append(url)
    return gone


# ── Phase B: detail ───────────────────────────────────────────────────────────

async def _enrich_listing(
    session: requests.Session, pid: str, *, stop: asyncio.Event | None = None
) -> dict[str, Any]:
    """Detail fields of one listing ({} when unavailable). A refusal sets ``stop``: requests
    that have not started yet are then skipped."""
    async with cfg.DETAIL_SEM:
        if stop is not None and stop.is_set():
            return {}
        await asyncio.sleep(random.uniform(0.3, 1.0))
        if stop is not None and stop.is_set():
            return {}
        try:
            root = await _get_json(
                session, cfg.DETAIL_API.format(id=pid), tag=f"detail pid={pid}", raise_on_block=stop is not None
            )
        except BlockedError as exc:
            logger.error("Phase B stopped: %s", exc)
            if stop is not None:
                stop.set()
            return {}
        if not isinstance(root, dict):
            return {}
        d = root.get("data") or {}
        if not isinstance(d, dict):
            return {}

        neighborhood, municipality = P.neighborhood_from_geo(d.get("geographiesByLevel"))
        images = d.get("images") or []
        photo_urls = P.photo_urls_from_images(images)
        result: dict[str, Any] = {}

        title = d.get("title") or d.get("name") or d.get("heading")
        if isinstance(title, str) and title.strip():
            result["title"] = title.strip()

        desc = d.get("description")
        if isinstance(desc, str) and desc.strip():
            result["description"] = desc.strip()

        if municipality:
            result["municipality"] = municipality
        if neighborhood:
            result["neighborhood"] = neighborhood

        addr = d.get("streetAddress")
        if isinstance(addr, str) and addr.strip():
            result["address_raw"] = addr.strip()

        ppsm = P.clean_float(d.get("pricePerSqMeters"))
        if ppsm:
            result["price_per_sqm"] = ppsm

        yb = P.clean_year(d.get("year_of_construction"))
        if yb:
            result["year_built"] = yb

        ry = P.clean_year(d.get("renovationYear"))
        if ry:
            result["renovation_year"] = ry

        ec = P.decode_energy_class(d.get("energyClass"))
        if ec:
            result["energy_class"] = ec

        ht = P.decode_heating(d.get("heatingController"), d.get("heatingMedium"))
        if ht:
            result["heating_type"] = ht

        if photo_urls:
            result["photo_urls"] = photo_urls
            result["photos_count"] = len(photo_urls)

        lat = P.clean_float(d.get("latitude"))
        lng = P.clean_float(d.get("longitude"))
        if lat:
            result["latitude"] = lat
        if lng:
            result["longitude"] = lng

        best_dt = None
        for key in ("modified", "uploaded", "firstPublishDate", "updatedAt"):
            dt = P.parse_spitogatos_datetime(d.get(key))
            if dt is not None and (best_dt is None or dt > best_dt):
                best_dt = dt
        if best_dt:
            result["publication_date"] = best_dt.date().isoformat()

        logger.info(
            "Enriched %-12s  year=%s  energy=%s  heat=%s  desc=%s",
            pid,
            result.get("year_built", "—"),
            result.get("energy_class", "—"),
            result.get("heating_type", "—")[:20] if result.get("heating_type") else "—",
            "yes" if result.get("description") else "no",
        )
        return result


def _apply_detail(row: dict[str, Any], detail: dict[str, Any], image_ids: list[int], now_iso: str) -> None:
    detail = dict(detail)
    published = detail.pop("publication_date", None)
    row.update(detail)
    if published and (not row.get("publication_date") or published > str(row["publication_date"])[:10]):
        row["publication_date"] = published
    row["search_image_ids"] = image_ids
    row["detail_fetched_at"] = now_iso
    row["updated_at"] = now_iso


async def _fetch_details(
    store: dict[str, dict[str, Any]], todo: dict[str, tuple[str, list[int]]]
) -> tuple[int, bool]:
    """Fetch and store the detail of every listing in ``todo``; ``(stored, refused)``."""
    stop = asyncio.Event()
    items = list(todo.items())
    done = 0
    for start in range(0, len(items), cfg.ROTATE_EVERY):
        batch = items[start:start + cfg.ROTATE_EVERY]
        batch_session = _make_session()
        logger.info("Phase B batch %s–%s / %s", start + 1, start + len(batch), len(items))
        results = await asyncio.gather(
            *(_enrich_listing(batch_session, pid, stop=stop) for _, (pid, _) in batch),
            return_exceptions=True,
        )
        now_iso = _now_iso()
        for (url, (_, image_ids)), result in zip(batch, results):
            if isinstance(result, Exception):
                logger.warning("Detail error url=%s: %s", url, result)
            elif result:
                _apply_detail(store[url], result, image_ids, now_iso)
                done += 1
        P.atomic_save(store)
        logger.info("Saved — %s / %s details stored.", done, len(items))
        if stop.is_set():
            break
    return done, stop.is_set()


# ── Run records ───────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def write_sweep_marker(started_at: str, *, seen: int, total_results: int | None = None, passes: int = 1) -> None:
    """Record a trusted search sweep: every listing still online was touched after ``started_at``."""
    marker = {
        "started_at": started_at, "completed_at": _now_iso(), "seen": seen,
        "total_results": total_results, "passes": passes,
    }
    _write_json_atomic(cfg.SWEEP_JSON, marker)
    logger.info("Trusted sweep recorded → %s (%s listings seen)", cfg.SWEEP_JSON.name, seen)


def _summary(
    started_at: str, sweep: Sweep, trusted: bool, reason: str, changes: SweepChanges, gone: list[str],
    store: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "finished_at": None,
        "status": "blocked" if sweep.blocked else ("ok" if trusted else "incomplete"),
        "blocked": sweep.blocked,
        "trusted": trusted,
        "trust_reason": reason,
        "passes": sweep.passes,
        "pages": sweep.pages,
        "seen": len(sweep.rows),
        "total_results": sweep.total_results,
        "store_rows": len(store),
        "new": len(changes.new),
        "reseen": changes.reseen,
        "price_changes": len(changes.price_changes),
        "photos_changed": len(changes.photos_changed),
        "returned": len(changes.returned),
        "gone": len(gone),
        "detail_fetches": len(changes.needs_detail),
        "details_stored": 0,
        "changes": {
            "new": changes.new,
            "price_changes": changes.price_changes,
            "photos_changed": changes.photos_changed,
            "returned": changes.returned,
            "gone": gone,
        },
    }


async def run(*, dry_run: bool = False) -> dict[str, Any]:
    """One collector run; returns its summary (also written to ``spitogatos_last_run.json``).

    ``dry_run`` reads the search pages only and reports what a run would change: no detail
    request is made and no file is written.
    """
    session = _make_session()
    logger.info("=== Spitogatos — API-only scraper (no browser needed)%s ===", " — dry run" if dry_run else "")

    store = P.load_listings_url_map(cfg.LISTINGS_JSON)
    if not store and cfg.LISTINGS_JSON.exists() and cfg.LISTINGS_JSON.stat().st_size > 1024:
        raise RuntimeError(f"{cfg.LISTINGS_JSON} could not be read; not replacing it with a fresh sweep.")
    logger.info("Loaded %s existing listings from %s", len(store), cfg.LISTINGS_JSON.name)
    before = previous_sweep_size(store)

    started_at = _now_iso()
    sweep = await collect_sweep(session)
    trusted, reason = sweep_trust(sweep, previous_seen=before)
    now_iso = _now_iso()
    changes = apply_sweep(store, sweep.rows, now_iso)
    gone = mark_gone(store, set(sweep.rows), now_iso) if trusted else []
    summary = _summary(started_at, sweep, trusted, reason, changes, gone, store)
    logger.info(
        "Phase A — %s of %s results in %s pass(es): %s new, %s re-seen, %s price changes, "
        "%s photo sets changed, %s gone, %s back. Sweep %s.",
        len(sweep.rows), sweep.total_results, sweep.passes, len(changes.new), changes.reseen,
        len(changes.price_changes), len(changes.photos_changed), len(gone), len(changes.returned),
        "trusted" if trusted else f"NOT trusted ({reason}): nothing is marked gone",
    )
    if dry_run:
        summary.update(status="dry_run", finished_at=_now_iso())
        return summary

    if sweep.rows:
        P.atomic_save(store)
    if trusted:
        write_sweep_marker(started_at, seen=len(sweep.rows), total_results=sweep.total_results, passes=sweep.passes)

    refused = sweep.blocked
    if not refused and changes.needs_detail:
        logger.info("Phase B — fetching detail for %s listings …", len(changes.needs_detail))
        summary["details_stored"], refused = await _fetch_details(store, changes.needs_detail)
    if refused:
        summary["status"] = "blocked"
    summary["finished_at"] = _now_iso()
    _write_json_atomic(cfg.LAST_RUN_JSON, summary)
    logger.info(
        "Done (%s). %s rows in store, %s of %s details stored → %s",
        summary["status"], len(store), summary["details_stored"], len(changes.needs_detail), cfg.LISTINGS_JSON,
    )
    if refused:
        raise BlockedError("Spitogatos refused the collector; progress so far is saved, nothing was retried.")
    return summary
