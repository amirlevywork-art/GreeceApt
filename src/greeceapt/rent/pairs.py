"""
Same flat advertised for sale *and* for rent → an observed price/rent pair (ground truth).

1. Metadata candidates: within ``MAX_DISTANCE_KM`` (Spitogatos offsets coordinates), area within
   ``AREA_TOLERANCE``, same floor class (and same storey when both are known upper floors).
2. Photo confirmation: at least ``MIN_SHARED_PHOTOS`` one-to-one photo matches whose full or
   centre pHash lie within ``MAX_HAMMING`` bits — the *near* rule of the opportunity dedupe.
   Agencies stamp their own watermark, which moves the hash: on the 336 candidates of
   Nov 2026 the closest photo pair sat at 0–12 bits for 23 candidates and at ≥ 14 for all the
   rest, so 10 bits keeps the gap on both sides.
3. Flats: one flat is often advertised by several agencies on both sides (one 35 m² flat had
   3 sale × 5 rent ads), so confirmed pairs are grouped into flats and each flat counts once,
   at the median of its asking prices and rents.

``validate`` refits the rent model *without* the paired rentals and compares, per flat, the
predicted rent with the rent actually asked — the central estimate (error, bias) and the
conservative one (share of flats whose asked rent is at least ``rent_lo``; nominally 90%).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx
import numpy as np

from greeceapt.rent.features import coords_km
from greeceapt.rent.model import RentModel
from greeceapt.collect.fingerprints import PhotoFingerprint
from greeceapt.collect.phash_index import PhotoRef

logger = logging.getLogger(__name__)

MAX_DISTANCE_KM = 0.35
AREA_TOLERANCE = 0.03
MAX_HAMMING = 10
MIN_SHARED_PHOTOS = 2
MAX_PHOTOS = 12


@dataclass(frozen=True)
class Pair:
    sale_url: str
    rent_url: str
    price_eur: float
    rent_eur: float
    area_sqm: float
    distance_km: float
    shared_photos: int

    @property
    def gross_yield(self) -> float:
        return 12.0 * self.rent_eur / self.price_eur


def _same_floor(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if a.get("floor_class") is None or a.get("floor_class") != b.get("floor_class"):
        return False
    if a.get("floor_class") == "upper" and a.get("floor") is not None and b.get("floor") is not None:
        return float(a["floor"]) == float(b["floor"])
    return True


def metadata_candidates(sales: Sequence[Mapping[str, Any]], rentals: Sequence[Mapping[str, Any]]
                        ) -> list[tuple[int, int, float]]:
    """(sale index, rental index, distance km) for metadata-compatible pairs."""
    def usable(r):
        return r.get("latitude") is not None and r.get("longitude") is not None and r.get("area_sqm")

    s_idx = [i for i, r in enumerate(sales) if usable(r)]
    r_idx = [j for j, r in enumerate(rentals) if usable(r)]
    if not s_idx or not r_idx:
        return []
    s_xy = coords_km(np.array([float(sales[i]["latitude"]) for i in s_idx]),
                     np.array([float(sales[i]["longitude"]) for i in s_idx]))
    r_xy = coords_km(np.array([float(rentals[j]["latitude"]) for j in r_idx]),
                     np.array([float(rentals[j]["longitude"]) for j in r_idx]))
    out = []
    for a, i in enumerate(s_idx):
        dist = np.sqrt(((r_xy - s_xy[a]) ** 2).sum(axis=1))
        for b in np.flatnonzero(dist <= MAX_DISTANCE_KM):
            j = r_idx[int(b)]
            sa, ra = float(sales[i]["area_sqm"]), float(rentals[j]["area_sqm"])
            if abs(sa - ra) / max(sa, ra) <= AREA_TOLERANCE and _same_floor(sales[i], rentals[j]):
                out.append((i, j, float(dist[b])))
    return out


def shared_photo_count(a: Sequence[PhotoFingerprint], b: Sequence[PhotoFingerprint]) -> int:
    """One-to-one photo matches within ``MAX_HAMMING`` bits on the full or the centre hash."""
    from greeceapt.collect.dedupe import greedy_photo_matching

    refs_a = [PhotoRef(f"a{n}", fp) for n, fp in enumerate(a)]
    refs_b = [PhotoRef(f"b{n}", fp) for n, fp in enumerate(b)]
    return len(greedy_photo_matching(refs_a, refs_b, MAX_HAMMING + 1)) if refs_a and refs_b else 0


@dataclass(frozen=True)
class Flat:
    """Confirmed pairs of one flat: every sale and rent ad linked to it by a pair."""
    sale_urls: tuple[str, ...]
    rent_urls: tuple[str, ...]
    price_eur: float
    rent_eur: float
    area_sqm: float

    @property
    def gross_yield(self) -> float:
        return 12.0 * self.rent_eur / self.price_eur


def group_flats(pairs: Sequence[Pair]) -> list[Flat]:
    """Connected components of the sale ↔ rent pair graph, one ``Flat`` each."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        parent[find("s:" + p.sale_url)] = find("r:" + p.rent_url)
    groups: dict[str, list[Pair]] = {}
    for p in pairs:
        groups.setdefault(find("s:" + p.sale_url), []).append(p)
    flats = []
    for members in groups.values():
        sales = {p.sale_url: p for p in members}
        rents = {p.rent_url: p for p in members}
        flats.append(Flat(
            sale_urls=tuple(sorted(sales)), rent_urls=tuple(sorted(rents)),
            price_eur=float(np.median([p.price_eur for p in sales.values()])),
            rent_eur=float(np.median([p.rent_eur for p in rents.values()])),
            area_sqm=float(np.median([p.area_sqm for p in sales.values()])),
        ))
    return sorted(flats, key=lambda f: f.sale_urls[0])


def _photos(listing: Mapping[str, Any]) -> list[str]:
    urls = listing.get("photo_urls")
    if urls is None and listing.get("photo_urls_json"):
        urls = json.loads(str(listing["photo_urls_json"]))
    return [str(u) for u in (urls or [])][:MAX_PHOTOS]


async def confirm_pairs(sales, rentals, candidates) -> list[Pair]:
    from greeceapt.collect.util import compute_listing_photo_fingerprints, load_standalone_phash_cache

    cache: dict[str, PhotoFingerprint] = load_standalone_phash_cache()  # sale photos are mostly there
    sem = asyncio.Semaphore(8)
    pairs = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, j, dist in candidates:
            fa = await compute_listing_photo_fingerprints(_photos(sales[i]), cache, sem, client)
            fb = await compute_listing_photo_fingerprints(_photos(rentals[j]), cache, sem, client)
            shared = shared_photo_count(list(fa.values()), list(fb.values()))
            if shared >= MIN_SHARED_PHOTOS:
                pairs.append(Pair(
                    sale_url=str(sales[i]["url"]), rent_url=str(rentals[j]["url"]),
                    price_eur=float(sales[i]["price_eur"]), rent_eur=float(rentals[j]["rent_eur"]),
                    area_sqm=float(sales[i]["area_sqm"]), distance_km=dist, shared_photos=shared,
                ))
    return pairs


def validate(pairs: Sequence[Pair], sales_by_url: Mapping[str, Mapping[str, Any]],
             training: Sequence[Mapping[str, Any]], *, alpha: float, k: int,
             trained: RentModel | None) -> dict[str, Any]:
    """Refit without the paired rentals; compare predicted vs actually-asked rent per flat.

    ``fit`` leaves the conservative bounds unset (they come from cross-validation in
    ``select_and_fit``), so the ``trained`` model's bounds are carried over.
    """
    from greeceapt.rent.model import fit

    held_out = {p.rent_url for p in pairs}
    model = fit([r for r in training if r["url"] not in held_out], alpha=alpha, k=k)
    if trained is not None:
        model.lower_log_residual = trained.lower_log_residual
        model.lower_by_group = dict(trained.lower_by_group)
        model.sparse_lower_by_group = dict(trained.sparse_lower_by_group)
        model.sparse_km = trained.sparse_km
    rows = []
    for flat in group_flats(pairs):
        predicted, low = model.predict_rent(dict(sales_by_url[flat.sale_urls[0]]))
        rows.append({**asdict(flat), "predicted_rent": predicted, "predicted_rent_lo": low,
                     "pct_error": (predicted - flat.rent_eur) / flat.rent_eur,
                     "abs_pct_error": abs(predicted - flat.rent_eur) / flat.rent_eur,
                     "rent_at_least_lo": flat.rent_eur >= low,
                     "observed_gross_yield": flat.gross_yield,
                     "predicted_gross_yield": 12 * predicted / flat.price_eur})
    if not rows:
        return {"n": 0, "n_pairs": len(pairs), "flats": []}
    ape = np.array([r["abs_pct_error"] for r in rows])
    obs = np.array([r["observed_gross_yield"] for r in rows])
    pred = np.array([r["predicted_gross_yield"] for r in rows])
    return {"n": len(rows), "n_pairs": len(pairs), "mape": float(ape.mean()),
            "median_ape": float(np.median(ape)),
            "median_pct_error": float(np.median([r["pct_error"] for r in rows])),
            "lower_bound_coverage": float(np.mean([r["rent_at_least_lo"] for r in rows])),
            "yield_spearman": _spearman(obs, pred) if len(rows) >= 5 else None, "flats": rows}


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def write(result: Mapping[str, Any], path: Path) -> None:
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("Pairs: %s pairs, %s flats → %s", result.get("n_pairs"), result.get("n"), path)



def run(sale_db: Path, out_path: Path) -> dict[str, Any]:
    """Match sale listings in ``sale_db`` (updated_listings.db layout) against rentals.db."""
    import sqlite3

    from greeceapt.rent import store
    from greeceapt.rent.train import load, training_rows
    from greeceapt.scoring.ranking import rebuild_listing

    conn = sqlite3.connect(f"file:{sale_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sales = [rebuild_listing(row) for row in conn.execute("SELECT * FROM listings")]
    finally:
        conn.close()
    rconn = store.connect()
    try:
        rentals = store.all_rows(rconn)
    finally:
        rconn.close()
    candidates = metadata_candidates(sales, rentals)
    logger.info("Pairs: %s sale × %s rentals → %s metadata candidates.", len(sales), len(rentals), len(candidates))
    pairs = asyncio.run(confirm_pairs(sales, rentals, candidates))
    model = load()
    training, _ = training_rows(rentals)
    result = validate(pairs, {str(s["url"]): s for s in sales}, training,
                      alpha=model.alpha if model else 3.0, k=model.k if model else 10,
                      trained=model)
    result["metadata_candidates"] = len(candidates)
    write(result, out_path)
    return result
