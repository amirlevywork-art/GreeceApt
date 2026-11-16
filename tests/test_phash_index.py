"""Five-band pHash indexing and similarity-aware boilerplate tests."""

from __future__ import annotations

import hashlib
import random

from greeceapt.collect.fingerprints import PhotoFingerprint
from greeceapt.collect.phash_index import (
    HashRef,
    HammingBandIndex,
    analyze_photo_index,
)


def _ref(listing: int, key: str, value: int, variant: str = "full") -> HashRef:
    return HashRef(listing, key, variant, value)


def _listing(index: int, photos: dict[str, tuple[int, int | None]]) -> dict:
    return {
        "id": f"listing-{index}",
        "photo_fingerprints": {
            key: PhotoFingerprint("phash-v2", full, center).to_json()
            for key, (full, center) in photos.items()
        },
    }


def _far(index: int) -> int:
    return int(hashlib.sha256(str(index).encode()).hexdigest()[:16], 16)


def test_band_index_confirms_distances_zero_through_four_and_excludes_five():
    index = HammingBandIndex()
    base = _ref(0, "base", 0)
    index.add(base)

    for distance in range(5):
        query = _ref(distance + 1, f"d{distance}", (1 << distance) - 1)
        assert base in index.neighbors(query)

    assert base not in index.neighbors(_ref(9, "d5", (1 << 5) - 1))


def test_band_index_keeps_full_and_center_variants_separate():
    index = HammingBandIndex()
    index.add(_ref(0, "photo", 123, "full"))

    assert index.neighbors(_ref(1, "photo", 123, "center")) == ()


def test_band_index_recall_spans_every_unchanged_band_and_is_order_independent():
    rng = random.Random(20261031)
    base_value = rng.getrandbits(64)
    refs = [_ref(0, "base", base_value)]
    for omitted_band, (start, width) in enumerate(HammingBandIndex.band_layout()):
        positions = [bit for bit in range(64) if not start <= bit < start + width]
        mask = sum(1 << bit for bit in rng.sample(positions, 4))
        refs.append(_ref(omitted_band + 1, f"changed-{omitted_band}", base_value ^ mask))

    def pairs(order):
        index = HammingBandIndex()
        for ref in order:
            index.add(ref)
        return {
            tuple(sorted(((ref.listing_index, ref.photo_key), (other.listing_index, other.photo_key))))
            for ref in refs
            for other in index.neighbors(ref)
            if ref != other
        }

    assert pairs(refs) == pairs(list(reversed(refs)))
    assert all(refs[0] in HammingBandIndex.from_refs(refs).neighbors(ref) for ref in refs[1:])


def test_near_hash_family_in_five_distinct_listings_is_blacklisted():
    listings = [_listing(i, {f"p{i}": (value, _far(i))})
                for i, value in enumerate((0, 1, 2, 4, 8))]

    analysis = analyze_photo_index(listings)

    assert len(analysis.blacklisted_refs) == 5
    assert analysis.blacklist_family_count == 1
    assert all(not photos for photos in analysis.usable_photos_by_listing)
    assert analysis.photo_candidate_pairs == frozenset()


def test_four_listing_family_is_not_blacklisted():
    listings = [_listing(i, {f"p{i}": (value, _far(i))})
                for i, value in enumerate((0, 1, 2, 4))]

    analysis = analyze_photo_index(listings)

    assert analysis.blacklisted_refs == frozenset()
    assert len(analysis.photo_candidate_pairs) == 6


def test_center_family_alone_invalidates_the_whole_photo():
    listings = [_listing(i, {f"p{i}": (_far(i), value)})
                for i, value in enumerate((0, 1, 2, 4, 8))]

    analysis = analyze_photo_index(listings)

    assert len(analysis.blacklisted_refs) == 5
    assert all(not photos for photos in analysis.usable_photos_by_listing)


def test_duplicate_photos_in_one_listing_count_once_toward_threshold():
    listings = [
        _listing(0, {"a": (0, 100), "b": (0, 100)}),
        _listing(1, {"a": (1, 200)}),
        _listing(2, {"a": (2, 300)}),
        _listing(3, {"a": (4, 400)}),
    ]

    analysis = analyze_photo_index(listings)

    assert analysis.blacklisted_refs == frozenset()


# ── Cluster-aware boilerplate (plot-source-vision-dedupe D3) ──────────────────

def _carrier(index: int, *, price: float, area: float, hood: str = "Ampelokipoi", shared: str = "same") -> dict:
    import hashlib as _hashlib

    from greeceapt.collect.fingerprints import PhotoFingerprint as _FP

    def fp(label):
        full = int(_hashlib.sha256(label.encode()).hexdigest()[:16], 16)
        return _FP("phash-v2", full, int(_hashlib.sha256(("c" + label).encode()).hexdigest()[:16], 16)).to_json()

    return {
        "url": f"https://agency{index}/1", "price_eur": price, "area_sqm": area, "neighborhood": hood,
        "photo_fingerprints": {f"{shared}.jpg": fp(shared), f"own{index}.jpg": fp(f"own{index}")},
    }


def test_photo_shared_by_five_consistent_ads_is_not_boilerplate():
    from greeceapt.collect.phash_index import analyze_photo_index

    listings = [_carrier(i, price=50_000.0, area=46.0) for i in range(5)]
    analysis = analyze_photo_index(listings)
    assert analysis.blacklist_family_count == 0
    assert all(len(photos) == 2 for photos in analysis.usable_photos_by_listing)


def test_photo_shared_by_five_different_flats_stays_boilerplate():
    from greeceapt.collect.phash_index import analyze_photo_index

    listings = [_carrier(i, price=50_000.0 + 9_000 * i, area=40.0 + 7 * i, shared="logo") for i in range(5)]
    analysis = analyze_photo_index(listings)
    assert {key for _, key in analysis.blacklisted_refs} == {"logo.jpg"}  # full + center families
    assert all(len(photos) == 1 for photos in analysis.usable_photos_by_listing)


def test_mixed_carriers_stay_boilerplate():
    from greeceapt.collect.phash_index import analyze_photo_index

    listings = [_carrier(i, price=50_000.0, area=46.0) for i in range(4)]
    listings.append(_carrier(4, price=90_000.0, area=80.0))
    assert {key for _, key in analyze_photo_index(listings).blacklisted_refs} == {"same.jpg"}
