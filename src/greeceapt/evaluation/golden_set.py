"""
Golden set builder — sample apartment photos to be pairwise-ranked.

``tune`` is built first (used while iterating on prompts). ``holdout`` is created only once
the loop works, then locked: the promotion gate runs against it exactly once and its images
are never opened for failure diagnosis. Both are additive — re-running ``--add`` appends new
ids and never disturbs existing ones, so a set and its labels can grow over time.

Photos are downloaded once at 768px; candidate scorers downscale from there at eval time.

    python -m greeceapt.evaluation.golden_set --add 72 --split tune
    python -m greeceapt.evaluation.golden_set --add 48 --split holdout
    python -m greeceapt.evaluation.golden_set --stats
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import imagehash
import requests
from PIL import Image

from greeceapt.storage import paths
from greeceapt.storage.util import parse_photo_urls_json
from greeceapt.storage.neighborhoods import NEIGHBORHOOD_CANONICAL

logger = logging.getLogger(__name__)

EVAL_DIR = paths.DATA_DIR / "vision_eval"
GOLDEN_JSON = EVAL_DIR / "golden.json"
IMAGES_DIR = EVAL_DIR / "images"
DOWNLOAD_SIZE = 768
JPEG_QUALITY = 90
ROOM_LABELS = {
    "kitchen", "bathroom", "bedroom", "living", "hallway",
    "balcony", "exterior", "floor_plan", "other",
}
QUALITY_LABELS = {"good", "dark", "blurry", "obstructed", "invalid"}


@dataclass(frozen=True)
class GoldenIssue:
    code: str
    message: str
    image_ids: tuple[str, ...] = ()


def validate_golden(
    rows: list[dict],
    *,
    images_dir: Path | None = None,
    max_phash_distance: int = 4,
) -> list[GoldenIssue]:
    """Report reproducibility and split-leakage problems without mutating data."""
    image_root = images_dir or IMAGES_DIR
    issues: list[GoldenIssue] = []
    listing_splits: dict[str, set[str]] = defaultdict(set)
    sha_splits: dict[str, set[str]] = defaultdict(set)
    ids_by_listing: dict[str, list[str]] = defaultdict(list)
    ids_by_sha: dict[str, list[str]] = defaultdict(list)
    hashes: list[tuple[str, str, imagehash.ImageHash]] = []

    for row in rows:
        image_id = str(row.get("id", "<missing-id>"))
        split = row.get("split")
        sha = str(row.get("sha256", ""))
        listing_id = str(row.get("listing_id", ""))
        if split not in {"tune", "holdout"}:
            issues.append(
                GoldenIssue("invalid_split", f"{image_id}: split must be tune or holdout", (image_id,))
            )
        if not isinstance(row.get("interior"), bool):
            issues.append(
                GoldenIssue("invalid_interior", f"{image_id}: interior must be boolean", (image_id,))
            )
        if split == "holdout" and row.get("room_type") not in ROOM_LABELS:
            issues.append(
                GoldenIssue(
                    "missing_room_label",
                    f"{image_id}: holdout requires a valid human room_type label",
                    (image_id,),
                )
            )
        if split == "holdout" and row.get("image_quality") not in QUALITY_LABELS:
            issues.append(
                GoldenIssue(
                    "missing_quality_label",
                    f"{image_id}: holdout requires a valid human image_quality label",
                    (image_id,),
                )
            )
        if split in {"tune", "holdout"}:
            listing_splits[listing_id].add(split)
            sha_splits[sha].add(split)
        ids_by_listing[listing_id].append(image_id)
        ids_by_sha[sha].append(image_id)

        image_path = image_root / f"{sha}.jpg"
        if not image_path.exists():
            issues.append(
                GoldenIssue("missing_image", f"{image_id}: missing {image_path}", (image_id,))
            )
            continue
        try:
            with Image.open(image_path) as image:
                hashes.append((image_id, str(split), imagehash.phash(image.convert("RGB"))))
        except (OSError, ValueError) as exc:
            issues.append(
                GoldenIssue("invalid_image", f"{image_id}: cannot decode image: {exc}", (image_id,))
            )

    for listing_id, splits in listing_splits.items():
        if len(splits) > 1:
            image_ids = tuple(ids_by_listing[listing_id])
            issues.append(
                GoldenIssue(
                    "listing_split_leakage",
                    f"listing {listing_id} appears in tune and holdout",
                    image_ids,
                )
            )
    for sha, splits in sha_splits.items():
        if len(splits) > 1:
            image_ids = tuple(ids_by_sha[sha])
            issues.append(
                GoldenIssue(
                    "sha_split_leakage",
                    f"SHA {sha} appears in tune and holdout",
                    image_ids,
                )
            )
    for index, (a_id, a_split, a_hash) in enumerate(hashes):
        for b_id, b_split, b_hash in hashes[index + 1 :]:
            if a_split == b_split or a_split not in {"tune", "holdout"} or b_split not in {"tune", "holdout"}:
                continue
            distance = a_hash - b_hash
            if distance <= max_phash_distance:
                issues.append(
                    GoldenIssue(
                        "phash_split_leakage",
                        f"{a_id} and {b_id} cross splits at pHash distance {distance}",
                        (a_id, b_id),
                    )
                )
    return issues


def _tier(neighborhood: str | None) -> str:
    """Tier bucket for stratification; unknown hoods share one bucket."""
    return NEIGHBORHOOD_CANONICAL.get((neighborhood or "").strip()) or "Tier_unknown"


def load_golden() -> list[dict]:
    if not GOLDEN_JSON.exists():
        return []
    with GOLDEN_JSON.open(encoding="utf-8") as f:
        return json.load(f)


def _save_golden(rows: list[dict]) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GOLDEN_JSON.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    tmp.replace(GOLDEN_JSON)


def _download(url: str) -> bytes | None:
    try:
        r = requests.get(url.strip(), timeout=25)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((DOWNLOAD_SIZE, DOWNLOAD_SIZE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("download failed %s: %s", url, exc)
        return None


def _candidate_listings(conn: sqlite3.Connection) -> list[dict]:
    """Listings with >=4 photos, carrying the fields we stratify on."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, url, neighborhood, price_eur, photo_urls_json
        FROM listings
        WHERE photo_urls_json IS NOT NULL
          AND price_eur IS NOT NULL
        """
    ).fetchall()
    out = []
    for r in rows:
        urls = parse_photo_urls_json(r["photo_urls_json"])
        if len(urls) >= 4:
            out.append({
                "listing_id": str(r["id"]),
                "url": r["url"],
                "neighborhood": r["neighborhood"],
                "price_eur": float(r["price_eur"]),
                "photo_urls": urls,
            })
    return out


def _price_quartile(price: float, cuts: list[float]) -> int:
    for i, c in enumerate(cuts):
        if price <= c:
            return i
    return len(cuts)


def add_images(n: int, split: str, *, seed: int | None = None) -> int:
    """Sample ``n`` new photos from distinct listings, stratified by tier × price quartile."""
    existing = load_golden()
    used_listings = {r["listing_id"] for r in existing}
    existing_ids = {r["id"] for r in existing}

    with sqlite3.connect(f"file:{paths.INGESTED_DB}?mode=ro", uri=True) as conn:
        candidates = _candidate_listings(conn)
    candidates = [c for c in candidates if c["listing_id"] not in used_listings]
    if not candidates:
        logger.warning("No unused candidate listings left to sample.")
        return 0

    prices = sorted(c["price_eur"] for c in candidates)
    q = len(prices) // 4
    cuts = [prices[q], prices[2 * q], prices[3 * q]] if len(prices) >= 4 else [prices[-1]] * 3

    strata: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for c in candidates:
        strata[(_tier(c["neighborhood"]), _price_quartile(c["price_eur"], cuts))].append(c)

    rng = random.Random(seed)
    for bucket in strata.values():
        rng.shuffle(bucket)

    # round-robin across strata so the sample spans tiers and price bands evenly
    picked: list[dict] = []
    bucket_keys = sorted(strata, key=lambda k: (str(k[0]), k[1]))
    while len(picked) < n and any(strata[k] for k in bucket_keys):
        for k in bucket_keys:
            if strata[k]:
                picked.append(strata[k].pop())
                if len(picked) >= n:
                    break

    added = 0
    new_rows: list[dict] = []
    for listing in picked:
        # deterministic photo choice: 2nd photo is usually a real interior, not a facade
        url = listing["photo_urls"][1] if len(listing["photo_urls"]) > 1 else listing["photo_urls"][0]
        data = _download(url)
        if data is None:
            continue
        sha = hashlib.sha256(data).hexdigest()
        image_id = sha[:16]
        if image_id in existing_ids:
            continue
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        (IMAGES_DIR / f"{sha}.jpg").write_bytes(data)
        new_rows.append({
            "id": image_id,
            "sha256": sha,
            "listing_id": listing["listing_id"],
            "url": listing["url"],
            "photo_url": url,
            "neighborhood": listing["neighborhood"],
            "tier": _tier(listing["neighborhood"]),
            "price_eur": listing["price_eur"],
            "interior": True,          # reviewer flips to False for facades/floor plans
            "split": split,
        })
        existing_ids.add(image_id)
        added += 1

    if new_rows:
        _save_golden(existing + new_rows)
    logger.info("Added %s images to split=%s (requested %s).", added, split, n)
    return added


def print_stats() -> None:
    rows = load_golden()
    by_split: dict[str, int] = defaultdict(int)
    by_tier: dict[str, int] = defaultdict(int)
    non_interior = 0
    for r in rows:
        by_split[r["split"]] += 1
        by_tier[r.get("tier", "?")] += 1
        if not r.get("interior", True):
            non_interior += 1
    print(f"golden set: {len(rows)} images")
    for s, c in sorted(by_split.items()):
        print(f"  split {s:8} {c}")
    print(f"  marked non-interior: {non_interior}")
    for t, c in sorted(by_tier.items()):
        print(f"  {t:26} {c}")
    issues = validate_golden(rows)
    print(f"  validation issues: {len(issues)}")
    for issue in issues:
        print(f"    [{issue.code}] {issue.message}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--add", type=int, default=0, help="how many new images to sample")
    ap.add_argument("--split", choices=["tune", "holdout"], default="tune")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.add:
        add_images(args.add, args.split, seed=args.seed)
    print_stats()


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    main()
