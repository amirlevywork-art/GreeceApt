"""Opportunity-level duplicate collapse (one investment opportunity per physical flat)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from greeceapt.collect.fingerprints import PhotoFingerprint
from greeceapt.collect.opportunity_dedupe import (
    DESCRIPTION_MIN_CHARS,
    DESCRIPTION_SIMILARITY_MIN,
    collapse_opportunities,
    description_similarity,
    identity_fingerprint,
    load_decisions,
    normalize_description,
    pair_key,
)

FIXTURE = Path(__file__).parent / "fixtures" / "november_ranking_input_2026.json"
AMPELOKIPOI_URLS = {
    "https://www.spitogatos.gr/en/property/1121148326",
    "https://www.spitogatos.gr/en/property/1121175748",
    "https://www.spitogatos.gr/en/property/1121149817",
    "https://www.spitogatos.gr/en/property/1121156044",
}


def _fp(label: str) -> dict:
    full = int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)
    center = int(hashlib.sha256(f"c:{label}".encode()).hexdigest()[:16], 16)
    return PhotoFingerprint("phash-v2", full, center).to_json()


def _listing(url: str, photos: list[str], **over) -> dict:
    row = {
        "url": url,
        "source": "spitogatos",
        "neighborhood": "Ampelokipoi",
        "price_eur": 50_000.0,
        "area_sqm": 46.0,
        "floor": -1.0,
        "floor_class": "basement",
        "year_built": 1976,
        "bedrooms": 1,
        "description": None,
        "photo_urls": [f"https://img/{p}.jpg" for p in photos],
        "photo_fingerprints": {f"https://img/{p}.jpg": _fp(p) for p in photos},
        "source_urls": [url],
    }
    row.update(over)
    return row


# ── Normalization ─────────────────────────────────────────────────────────────

def test_normalize_description_contract():
    text = (
        "<p>Διαμέρισμα &amp; ΑΠΟΘΗΚΗ, 46 τ.μ.!</p> Call 6912345678 or info@agency.gr "
        "https://agency.gr/x"
    )
    assert normalize_description(text) == (
        "διαμερισμα αποθηκη 0 τ μ call phone or email url"
    )


def test_description_similarity_uses_full_sequence_matcher_boundary():
    base = "bright renovated apartment close to the metro station with balcony and storage " * 2
    assert description_similarity(base, base) == 1.0
    assert DESCRIPTION_SIMILARITY_MIN == 0.92
    assert DESCRIPTION_MIN_CHARS == 80


def test_short_descriptions_cannot_authorize_description_only_merge():
    a = _listing("https://a/1", ["a1"], description="Nice flat near metro.")
    b = _listing("https://b/1", ["b1"], description="Nice flat near metro.")
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2


def test_long_identical_descriptions_merge_without_photos():
    text = "Fully renovated one bedroom apartment in Ampelokipoi, close to Panormou metro, " \
        "with new kitchen, new bathroom, security door and aluminium frames."
    a = _listing("https://a/1", ["a1"], description=text)
    b = _listing("https://b/1", ["b1"], description=text + " Call 6912345678.")
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 1


def test_a_blank_description_is_filled_from_another_ad_of_the_flat():
    """Nov 2026: 171 of 502 ranked flats showed no description; for 74 another ad had one (the
    cheapest ad's "" did not count as missing)."""
    text = "Bright semi-basement studio, currently rented for €400 per month, close to the metro."
    cheap = _listing("https://a/1", ["p1", "p2", "p3"], price_eur=50_000.0, description="", title="  ")
    other = _listing("https://b/1", ["p1", "p2", "p3"], price_eur=52_000.0, description=text, title="Studio")
    (flat,) = collapse_opportunities([cheap, other], {}).listings
    assert flat["price_eur"] == 50_000.0                      # still priced on the cheapest ad
    assert flat["description"] == text and flat["title"] == "Studio"


def test_a_decision_still_applies_when_one_side_is_already_a_merged_flat():
    """Nov 2026: decisions on a pair whose side was a merged flat (its photos the union of every
    ad's, its price the cheapest ad's) were ignored at re-score until the next update."""
    a = _listing("https://a/1", ["p1", "p2", "p3"], price_eur=60_000.0, area_sqm=17.0)
    b = _listing("https://b/1", ["q1", "q2", "q3"], price_eur=60_000.0, area_sqm=20.0)
    merged = dict(a, photo_urls=["p1", "p2", "p3", "r1", "r2"],
                  photo_fingerprints={**a["photo_fingerprints"], "r1": _fp("r1"), "r2": _fp("r2")},
                  price_eur=58_000.0, source_urls=["https://a/1", "https://c/1"],
                  advertised_offers=[{"url": "https://a/1", "price_eur": 60_000.0, "area_sqm": 17.0, "floor_class": a["floor_class"]},
                                     {"url": "https://c/1", "price_eur": 58_000.0, "area_sqm": 17.0, "floor_class": a["floor_class"]}])
    assert identity_fingerprint(a, b) == identity_fingerprint(merged, b)
    assert identity_fingerprint(a, b) != identity_fingerprint(dict(a, price_eur=55_000.0), b)   # a real change


# ── Pair gates ────────────────────────────────────────────────────────────────

def test_three_photo_matches_merge_and_keep_every_offer():
    a = _listing("https://a/1", ["p1", "p2", "p3"], price_eur=52_000.0, area_sqm=47.0)
    b = _listing("https://b/1", ["p1", "p2", "p3", "p4"], price_eur=50_000.0, area_sqm=46.0)
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 1
    merged = result.listings[0]
    assert (merged["price_eur"], merged["area_sqm"]) == (50_000.0, 46.0)  # same advertisement
    assert sorted(merged["source_urls"]) == ["https://a/1", "https://b/1"]
    assert sorted(o["price_eur"] for o in merged["advertised_offers"]) == [50_000.0, 52_000.0]
    assert len(merged["photo_urls"]) == 4


def test_one_photo_plus_exact_identity_metadata_merges():
    a = _listing("https://a/1", ["p1", "a2", "a3"])
    b = _listing("https://b/1", ["p1", "b2", "b3"], year_built=1975)
    assert len(collapse_opportunities([a, b], {}).listings) == 1


def test_one_photo_with_different_year_does_not_merge():
    a = _listing("https://a/1", ["p1", "a2", "a3"])
    b = _listing("https://b/1", ["p1", "b2", "b3"], year_built=1990)
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2
    assert result.reviews == []


def test_hard_negative_same_price_and_area_without_identity_signal_stays_separate():
    a = _listing("https://a/1", ["a1", "a2", "a3"])
    b = _listing("https://b/1", ["b1", "b2", "b3"], floor=2.0, floor_class="upper", year_built=2001)
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2
    assert result.reviews == []


def test_adjacent_basement_semi_basement_collapses_to_basement_with_flag():
    a = _listing("https://a/1", ["p1", "p2", "p3"])
    b = _listing("https://b/1", ["p1", "p2", "p3"], source="xe", floor=-0.5, floor_class="semi_basement")
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 1
    merged = result.listings[0]
    assert (merged["floor"], merged["floor_class"]) == (-1.0, "basement")
    assert merged["floor_conflict_adjacent"] is True


def test_strong_identity_with_conflicting_floor_goes_to_review():
    a = _listing("https://a/1", ["p1", "p2", "p3"])
    b = _listing("https://b/1", ["p1", "p2", "p3"], floor=2.0, floor_class="upper")
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2
    assert len(result.reviews) == 1
    assert {row["duplicate_review_status"] for row in result.listings} == {"pending"}


def test_strong_identity_with_price_gap_goes_to_review():
    a = _listing("https://a/1", ["p1", "p2", "p3"])
    b = _listing("https://b/1", ["p1", "p2", "p3"], price_eur=60_000.0)
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2
    assert result.reviews[0]["reason"] == "price_gap"


def test_persisted_decisions_apply_only_while_identity_is_unchanged():
    a = _listing("https://a/1", ["p1", "p2", "p3"])
    b = _listing("https://b/1", ["p1", "p2", "p3"], floor=2.0, floor_class="upper")
    key = pair_key(a, b)
    same = {key: {"decision": "same", "identity": identity_fingerprint(a, b)}}
    assert len(collapse_opportunities([a, b], same).listings) == 1

    different = {key: {"decision": "different", "identity": identity_fingerprint(a, b)}}
    result = collapse_opportunities([a, b], different)
    assert len(result.listings) == 2 and result.reviews == []
    assert {row.get("duplicate_review_status") for row in result.listings} == {"none"}

    b_changed = dict(b, price_eur=49_000.0)
    stale = collapse_opportunities([a, b_changed], same)
    assert stale.reviews and len(stale.listings) == 2


def test_complete_link_blocks_bridge_merges():
    a = _listing("https://a/1", ["x1", "x2", "x3"])
    bridge = _listing("https://b/1", ["x1", "x2", "x3", "y1", "y2", "y3"])
    c = _listing("https://c/1", ["y1", "y2", "y3"])
    result = collapse_opportunities([a, bridge, c], {})
    assert len(result.listings) == 2


def test_two_merged_groups_without_shared_url_collapse():
    a = _listing("https://a/1", ["p1", "p2", "p3"], source_urls=["https://a/1", "https://a/2"])
    b = _listing("https://b/1", ["p1", "p2", "p3"], source_urls=["https://b/1", "https://b/2"])
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 1
    assert len(result.listings[0]["source_urls"]) == 4


def test_photos_shared_by_many_consistent_ads_are_still_evidence():
    """Five agencies posting the same flat share its photos; that is not boilerplate."""
    rows = [_listing(f"https://agency{i}/1", ["p1", "p2", "p3"]) for i in range(5)]
    assert len(collapse_opportunities(rows, {}).listings) == 1


def test_photos_shared_by_inconsistent_ads_are_boilerplate():
    rows = [
        _listing(f"https://agency{i}/1", ["logo", f"u{i}a", f"u{i}b"], price_eur=50_000.0 + 9_000 * i,
                 area_sqm=40.0 + 7 * i)
        for i in range(5)
    ]
    assert len(collapse_opportunities(rows, {}).listings) == 5


# ── Decisions file ────────────────────────────────────────────────────────────

def test_load_decisions_fails_closed_on_malformed_json(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_decisions(path)
    path.write_text(json.dumps({"version": 1, "decisions": {"a|b": {"decision": "maybe"}}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_decisions(path)
    assert load_decisions(tmp_path / "missing.json") == {}


# ── November cluster ──────────────────────────────────────────────────────────

def test_single_photo_with_adjacent_floor_goes_to_review():
    a = _listing("https://a/1", ["p1", "a2", "a3"])
    b = _listing("https://b/1", ["p1", "b2", "b3"], source="xe", floor=-0.5, floor_class="semi_basement")
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2
    assert result.reviews[0]["reason"] == "adjacent_floor_single_photo"
    assert {row["duplicate_review_status"] for row in result.listings} == {"pending"}


def test_november_ampelokipoi_spitogatos_ads_are_one_opportunity_and_the_xe_copy_is_reviewed():
    """Four agencies' watermarked copies share 3-7 photos only at Hamming <= 10. The XE ad
    (semi-basement, one near photo with two of them) is too weak to merge and goes to review."""
    from greeceapt.collect.floor_resolver import apply_floor_resolution

    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cluster = [
        dict(row) for row in rows
        if row["url"] in AMPELOKIPOI_URLS or "c2a554d1-8760-414b-8314-13c87d275556" in row["url"]
    ]
    assert len(cluster) == 5
    for row in cluster:
        apply_floor_resolution(row)
    result = collapse_opportunities(cluster, {})
    assert len(result.listings) == 2
    spitogatos = next(row for row in result.listings if AMPELOKIPOI_URLS <= set(row["source_urls"]))
    assert spitogatos["floor_class"] == "basement"
    assert {review["reason"] for review in result.reviews} == {"adjacent_floor_single_photo"}


def test_first_pass_bridge_block_is_respected():
    survivor = _listing("https://a/1", ["x1", "x2", "x3", "y1", "y2", "y3"],
                        source_urls=["https://a/1", "https://b/1"])
    c = _listing("https://c/1", ["y1", "y2", "y3"])
    blocked = [frozenset({"https://a/1", "https://b/1", "https://c/1"})]
    assert len(collapse_opportunities([survivor, c], {}, blocked_components=blocked).listings) == 2
    assert len(collapse_opportunities([survivor, c], {}).listings) == 1


# ── Same flat from two agencies: watermarks and overlapping neighborhood names ─

KIPSELI = (37.9990, 23.7400)


def _near_fp(label: str, flipped: int) -> dict:
    """The fingerprint of ``label`` with ``flipped`` bits changed, as a watermark does."""
    base = PhotoFingerprint.from_json(_fp(label))
    mask = (1 << flipped) - 1
    return PhotoFingerprint("phash-v2", base.full ^ mask, base.center ^ mask).to_json()


def _watermarked(url: str, photos: list[str], flipped: int, **over) -> dict:
    row = _listing(url, [f"{url}-{p}" for p in photos], **over)
    row["photo_fingerprints"] = {
        photo_url: _near_fp(p, flipped) for photo_url, p in zip(row["photo_urls"], photos)
    }
    return row


def _at(lat_offset_m: float = 0.0) -> dict:
    return {"latitude": KIPSELI[0] + lat_offset_m / 111_320, "longitude": KIPSELI[1]}


def test_watermarked_copy_under_another_neighborhood_name_is_one_opportunity():
    a = _listing("https://a/1", ["p1", "p2", "a3"], neighborhood="Gyzi", **_at())
    b = _watermarked("https://b/1", ["p1", "p2", "b3"], 8, neighborhood="Lofos Finopoulou", **_at(120))
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 1 and result.reviews == []
    assert sorted(result.listings[0]["source_urls"]) == ["https://a/1", "https://b/1"]


def test_ads_in_different_neighborhoods_far_apart_are_not_compared():
    a = _listing("https://a/1", ["p1", "p2", "p3"], neighborhood="Gyzi", **_at())
    b = _listing("https://b/1", ["p1", "p2", "p3"], neighborhood="Pagkrati", **_at(2_000))
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2 and result.reviews == []


def test_nearby_ads_under_another_name_with_a_different_floor_stay_separate():
    a = _listing("https://a/1", ["p1", "p2", "p3"], neighborhood="Gyzi", **_at())
    b = _listing("https://b/1", ["p1", "p2", "p3"], neighborhood="Lofos Finopoulou",
                 floor=3.0, floor_class="upper", **_at(50))
    result = collapse_opportunities([a, b], {})
    assert len(result.listings) == 2 and result.reviews == []


def test_two_watermarked_photos_merge_but_photos_beyond_the_relaxed_distance_do_not():
    a = _listing("https://a/1", ["p1", "p2", "a3"])
    close = _watermarked("https://b/1", ["p1", "p2", "b3"], 10, year_built=None)
    far = _watermarked("https://c/1", ["p1", "p2", "c3"], 12, year_built=None)
    assert len(collapse_opportunities([a, close], {}).listings) == 1
    assert len(collapse_opportunities([a, far], {}).listings) == 2


def test_one_watermarked_photo_with_the_same_price_and_area_merges():
    a = _listing("https://a/1", ["p1", "a2", "a3"], year_built=None)
    b = _watermarked("https://b/1", ["p1", "b2", "b3"], 6, year_built=None)
    assert len(collapse_opportunities([a, b], {}).listings) == 1


def test_one_watermarked_photo_needs_the_exact_price_and_area():
    a = _listing("https://a/1", ["p1", "a2", "a3"], year_built=None)
    other_price = _watermarked("https://b/1", ["p1", "b2", "b3"], 6, year_built=None, price_eur=51_000.0)
    other_area = _watermarked("https://c/1", ["p1", "c2", "c3"], 6, year_built=None, area_sqm=48.0)
    for b in (other_price, other_area):
        result = collapse_opportunities([a, b], {})
        assert len(result.listings) == 2 and result.reviews == []


def test_near_photos_alone_never_send_a_pair_to_review():
    """A pending review hides both flats, so the weaker near-photo evidence only merges."""
    a = _listing("https://a/1", ["p1", "p2", "a3"], year_built=None)
    price_gap = _watermarked("https://b/1", ["p1", "p2", "b3"], 6, year_built=None, price_eur=60_000.0)
    adjacent = _watermarked("https://c/1", ["p1", "c2", "c3"], 6, year_built=None,
                            floor=-0.5, floor_class="semi_basement")
    for b in (price_gap, adjacent):
        result = collapse_opportunities([a, b], {})
        assert len(result.listings) == 2 and result.reviews == []
        assert {row["duplicate_review_status"] for row in result.listings} == {"none"}


def test_review_against_an_untracked_ad_does_not_hold_back_the_other_flat():
    """XE is not collected: a review pair with an XE-only ad cannot be resolved and must not hide
    the live Spitogatos flat."""
    a = _listing("https://www.spitogatos.gr/en/property/1", ["p1", "a2", "a3"])
    xe = _listing("https://www.xe.gr/property/d/1", ["p1", "b2", "b3"], source="xe",
                  floor=-0.5, floor_class="semi_basement")
    reviewed = collapse_opportunities([a, xe], {})
    assert {row["duplicate_review_status"] for row in reviewed.listings} == {"pending"}

    def tracked(listing):
        return "xe.gr" not in listing["url"]

    result = collapse_opportunities([a, xe], {}, is_tracked=tracked)
    assert len(result.listings) == 2 and result.reviews == []
    assert {row["duplicate_review_status"] for row in result.listings} == {"none"}


def test_merged_opportunity_dates_from_its_oldest_advertisement():
    """Time on the market is the flat's, not the cheapest agency's newest ad."""
    cheap_new = _listing("https://a/1", ["p1", "p2", "p3"], price_eur=49_000.0,
                         publication_date="2026-11-01", earliest_publication_date="2026-11-01")
    old = _listing("https://b/1", ["p1", "p2", "p3"], publication_date="2026-10-20",
                   earliest_publication_date="2025-03-04")
    merged = collapse_opportunities([cheap_new, old], {}).listings
    assert len(merged) == 1 and merged[0]["url"] == "https://a/1"
    assert merged[0]["earliest_publication_date"] == "2025-03-04"
