"""
Collapse duplicate advertisements into investment opportunities.

The conservative photo dedupe (``dedupe.py``) stays authoritative for its own merges. This
second, deterministic pass catches what it misses — chiefly the same flat posted by several
agencies — using explicit identity signals:

A pair is the same opportunity automatically only when **all** gates hold:

* same neighborhood;
* area within max(2 m², 3 %) and price within 5 %;
* floor classes equal, one unknown, or the adjacent pair basement / semi-basement
  (resolved to the more pessimistic basement and flagged);
* at least one strong identity signal: ≥ 3 photo matches; ≥ 2 near photo matches (see
  below); normalized description similarity ≥ 0.92 with both descriptions ≥ 80 normalized
  characters; the same source property id; ≥ 1 photo match plus exact price, exact rounded
  area, equal known floor class, construction years within one, and equal bedrooms when both
  are sane; or ≥ 1 near photo match plus exact price and rounded area, equal floor class (or
  one unknown) and no contradicting construction year or bedrooms.

Two agencies advertising one flat stamp their own watermark on the same photographs, which
moves the perceptual hash: on 17 hand-checked pairs (2026-11-13) the copies sat at Hamming
distance 0–12, most at 4–10. A *near* match is a one-to-one photo match at distance ≤ 10.
They also file the flat under overlapping area names ("Gyzi" / "Lofos Finopoulou"), so ads
under different names are compared too when they lie within 500 m and their price, area and
floor already agree. 16 of the 17 checked pairs were the same flat; the 17th matched only on
photos of the building's front.

A strong identity signal whose gates conflict goes to review and never auto-collapses; every
member of an unresolved review pair is marked ``duplicate_review_status="pending"``.
Persisted ``same`` / ``different`` decisions apply only while the pair's identity
fingerprint is unchanged. Grouping is complete-link, so a bridge cannot join unrelated ads.

Photos shared by five or more advertisements are agency boilerplate *unless* every carrier
agrees on neighborhood, price and area — that is one flat posted by several agencies, and
its photos are exactly the evidence we need.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from greeceapt.storage.util import normalize_listing_url
from greeceapt.collect.dedupe import PHASH_HAMMING_THRESHOLD, greedy_photo_matching, haversine_m
from greeceapt.collect.phash_index import PhotoRef, _parse_listing_photos

AREA_ABS_TOLERANCE_SQM = 2.0
AREA_REL_TOLERANCE = 0.03
PRICE_REL_TOLERANCE = 0.05
MIN_PHOTO_MATCHES = 3
NEAR_PHOTO_DISTANCE = 11          # one-to-one matches below this Hamming distance (≤ 10)
MIN_NEAR_PHOTO_MATCHES = 2
NEARBY_M = 500.0                  # ads under different area names are compared within this
DESCRIPTION_SIMILARITY_MIN = 0.92
DESCRIPTION_MIN_CHARS = 80
BOILERPLATE_MIN_LISTINGS = 5
MAX_SANE_BEDROOMS = 10
DECISIONS_VERSION = 1
DECISION_IDENTITY_VERSION = 2   # 2: the ad's own offer, no photo set (2026-11-16)

_ADJACENT_FLOORS = frozenset({"basement", "semi_basement"})
# Opportunity-level fields filled from agreeing members when the basis advertisement lacks them.
_FILL_FIELDS = (
    "title", "description", "address_raw", "bedrooms", "bathrooms", "year_built",
    "renovation_year", "energy_class", "heating_type", "municipality", "latitude", "longitude",
    "condition",
)


@dataclass(frozen=True)
class PairAssessment:
    same: bool
    review_reason: str | None
    signals: tuple[str, ...]
    photo_matches: int
    description_similarity: float | None
    near_photo_matches: int = 0


@dataclass
class OpportunityCollapseResult:
    listings: list[dict]
    merged: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)


# ── Normalization ─────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+|00)\s?30[\s.-]?)?(?:2\d|69)\d(?:[\s.-]?\d){7}(?!\d)")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def normalize_description(value: object) -> str:
    """HTML/entities, NFKC + casefold, accents, URL/e-mail/phone/number tokens, punctuation."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", html.unescape(str(value)))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")
    text = _URL_RE.sub(" url ", text)
    text = _EMAIL_RE.sub(" email ", text)
    text = _PHONE_RE.sub(" phone ", text)
    text = _NUMBER_RE.sub(" 0 ", text)
    text = _PUNCT_RE.sub(" ", text).replace("_", " ")
    return _SPACE_RE.sub(" ", text).strip()


def description_similarity(a: object, b: object) -> float:
    left, right = normalize_description(a), normalize_description(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


# ── Keys and fingerprints ─────────────────────────────────────────────────────

def _url(listing: dict) -> str:
    return normalize_listing_url(listing.get("url")) or str(listing.get("url") or "")


def pair_key(a: dict, b: dict) -> str:
    return "|".join(sorted((_url(a), _url(b))))


def _identity_fields(listing: dict) -> list[Any]:
    """What a decision is about: the advertisement's own price, area and floor, and its area.

    Taken from the ad's own offer, because the same ad is a single listing when ``run_all``
    reviews it and part of a merged flat (other ads' photos and maybe a cheaper price) when
    scoring re-collapses; the photo set was in here and made scoring ignore every decision on a
    merged flat until the next update (fixed 2026-11-16, ``DECISION_IDENTITY_VERSION`` 2).
    """
    from greeceapt.storage.neighborhoods import canonical_neighborhood

    url = _url(listing)
    own = next((offer for offer in _offers(listing)
                if (normalize_listing_url(str(offer.get("url"))) or str(offer.get("url"))) == url), {})
    price = _as_float(own.get("price_eur", listing.get("price_eur")))
    area = _as_float(own.get("area_sqm", listing.get("area_sqm")))
    hood = canonical_neighborhood(listing.get("neighborhood")) or listing.get("neighborhood")
    return [
        url,
        price,
        round(area) if area is not None else None,
        own.get("floor_class") or listing.get("floor_class"),
        str(hood or "").strip().casefold(),
    ]


def identity_fingerprint(a: dict, b: dict) -> str:
    payload = sorted((_identity_fields(a), _identity_fields(b)), key=lambda item: item[0])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def load_decisions(path: Path) -> dict[str, dict]:
    """Load persisted review decisions; malformed content fails closed (raises)."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"opportunity decisions file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != DECISIONS_VERSION:
        raise ValueError(f"opportunity decisions file has an unsupported version: {path}")
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError(f"opportunity decisions file has no decisions object: {path}")
    for key, value in decisions.items():
        if not isinstance(value, dict) or value.get("decision") not in {"same", "different"}:
            raise ValueError(f"invalid decision for {key!r} in {path}")
        if not isinstance(value.get("identity"), str):
            raise ValueError(f"decision for {key!r} lacks an identity fingerprint in {path}")
    return decisions


def merge_decisions(
    existing: Mapping[str, dict],
    answers: Mapping[str, str],
    reviews: Sequence[dict],
    *,
    decided_on: str,
) -> dict:
    """Existing decisions plus same/different answers for pending pairs (unsure is skipped)."""
    identities = {review["pair_key"]: review["identity"] for review in reviews}
    decisions = {key: dict(value) for key, value in existing.items()}
    for key, answer in answers.items():
        if answer not in {"same", "different"} or key not in identities:
            continue
        decisions[key] = {"decision": answer, "identity": identities[key], "decided_on": decided_on}
    return {"version": DECISIONS_VERSION, "decisions": decisions}


def write_decisions(path: Path, payload: Mapping[str, Any]) -> None:
    """Write atomically; the result must load with ``load_decisions`` or nothing is replaced."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        load_decisions(tmp)
    except ValueError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


# ── Pair assessment ───────────────────────────────────────────────────────────

def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _neighborhood(listing: dict) -> str:
    return str(listing.get("neighborhood") or "").strip().casefold()


def _sane_bedrooms(value: Any) -> int | None:
    number = _as_float(value)
    if number is None or number < 0 or number > MAX_SANE_BEDROOMS:
        return None
    return int(number)


def _photos(listing: dict, boilerplate: frozenset[int]) -> tuple[PhotoRef, ...]:
    return tuple(
        photo for photo in _parse_listing_photos(listing)
        if photo.fingerprint.full not in boilerplate
    )


def _area_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(AREA_ABS_TOLERANCE_SQM, AREA_REL_TOLERANCE * max(a, b))


def _coords(listing: dict) -> tuple[float, float] | None:
    lat, lon = _as_float(listing.get("latitude")), _as_float(listing.get("longitude"))
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _nearby(a: dict, b: dict) -> bool:
    ca, cb = _coords(a), _coords(b)
    return ca is not None and cb is not None and haversine_m(*ca, *cb) <= NEARBY_M


def _same_area(a: dict, b: dict) -> bool:
    """Same neighborhood name, or close enough that the names are overlapping labels."""
    return _neighborhood(a) == _neighborhood(b) or _nearby(a, b)


def _floors_compatible(a: dict, b: dict) -> bool:
    fa, fb = a.get("floor_class"), b.get("floor_class")
    return not fa or not fb or fa == fb or {fa, fb} == _ADJACENT_FLOORS


def _gates_hold(a: dict, b: dict) -> bool:
    """Area, price and floor agree (the gates a merge needs anyway)."""
    pa, pb = _as_float(a.get("price_eur")), _as_float(b.get("price_eur"))
    aa, ab = _as_float(a.get("area_sqm")), _as_float(b.get("area_sqm"))
    return (
        pa is not None and pb is not None and aa is not None and ab is not None
        and _area_close(aa, ab)
        and abs(pa - pb) <= PRICE_REL_TOLERANCE * max(pa, pb)
        and _floors_compatible(a, b)
    )


def assess_pair(a: dict, b: dict, photos_a: Sequence[PhotoRef], photos_b: Sequence[PhotoRef]) -> PairAssessment:
    matches = len(greedy_photo_matching(photos_a, photos_b, PHASH_HAMMING_THRESHOLD)) if photos_a and photos_b else 0
    near = len(greedy_photo_matching(photos_a, photos_b, NEAR_PHOTO_DISTANCE)) if photos_a and photos_b else 0
    desc_a, desc_b = normalize_description(a.get("description")), normalize_description(b.get("description"))
    similarity: float | None = None
    if len(desc_a) >= DESCRIPTION_MIN_CHARS and len(desc_b) >= DESCRIPTION_MIN_CHARS:
        matcher = SequenceMatcher(None, desc_a, desc_b, autojunk=False)
        # real_quick_ratio/quick_ratio are exact upper bounds of ratio(): skipping the
        # expensive computation below the threshold cannot change any decision.
        if matcher.real_quick_ratio() >= DESCRIPTION_SIMILARITY_MIN and matcher.quick_ratio() >= DESCRIPTION_SIMILARITY_MIN:
            similarity = matcher.ratio()
        else:
            similarity = matcher.quick_ratio()

    price_a, price_b = _as_float(a.get("price_eur")), _as_float(b.get("price_eur"))
    area_a, area_b = _as_float(a.get("area_sqm")), _as_float(b.get("area_sqm"))
    floor_a, floor_b = a.get("floor_class"), b.get("floor_class")
    year_a, year_b = _as_float(a.get("year_built")), _as_float(b.get("year_built"))
    beds_a, beds_b = _sane_bedrooms(a.get("bedrooms")), _sane_bedrooms(b.get("bedrooms"))

    signals: list[str] = []
    if matches >= MIN_PHOTO_MATCHES:
        signals.append("photos")
    if similarity is not None and similarity >= DESCRIPTION_SIMILARITY_MIN:
        signals.append("description")
    if a.get("source_property_id") and a.get("source_property_id") == b.get("source_property_id"):
        signals.append("source_property_id")
    if (
        matches >= 1
        and price_a is not None and price_a == price_b
        and area_a is not None and area_b is not None and round(area_a) == round(area_b)
        and floor_a is not None and floor_a == floor_b
        and year_a is not None and year_b is not None and abs(year_a - year_b) <= 1
        and (beds_a is None or beds_b is None or beds_a == beds_b)
    ):
        signals.append("photo_plus_exact_metadata")
    exact_price_area = (
        price_a is not None and price_a == price_b
        and area_a is not None and area_b is not None and round(area_a) == round(area_b)
    )
    # Near-photo signals only ever merge: a pair they alone flag whose gates fail stays
    # separate rather than going to review (a pending review hides both flats).
    strong = bool(signals)
    if near >= MIN_NEAR_PHOTO_MATCHES:
        signals.append("near_photos")
    if (
        near >= 1
        and exact_price_area
        and (not floor_a or not floor_b or floor_a == floor_b)
        and (year_a is None or year_b is None or abs(year_a - year_b) <= 1)
        and (beds_a is None or beds_b is None or beds_a == beds_b)
    ):
        signals.append("near_photo_plus_exact_price_area")
    if not signals:
        if (
            matches >= 1
            and exact_price_area
            and {floor_a, floor_b} == _ADJACENT_FLOORS
            and _same_area(a, b)
        ):
            # One photo + exact price/area but basement vs semi-basement: too weak to merge
            # automatically, too strong to ignore — a person decides.
            return PairAssessment(False, "adjacent_floor_single_photo", ("photo_plus_exact_metadata_adjacent_floor",), matches, similarity, near)
        return PairAssessment(False, None, (), matches, similarity, near)

    reason: str | None = None
    if not _same_area(a, b):
        reason = "neighborhood_mismatch"
    elif area_a is None or area_b is None or not _area_close(area_a, area_b):
        reason = "area_gap"
    elif price_a is None or price_b is None or abs(price_a - price_b) > PRICE_REL_TOLERANCE * max(price_a, price_b):
        reason = "price_gap"
    elif floor_a and floor_b and floor_a != floor_b and {floor_a, floor_b} != _ADJACENT_FLOORS:
        reason = "floor_conflict"
    if reason is not None and not strong:
        return PairAssessment(False, None, tuple(signals), matches, similarity, near)
    return PairAssessment(reason is None, reason, tuple(signals), matches, similarity, near)


# ── Boilerplate ───────────────────────────────────────────────────────────────

def _consistent(carriers: Sequence[dict]) -> bool:
    hoods = {_neighborhood(row) for row in carriers}
    prices = [_as_float(row.get("price_eur")) for row in carriers]
    areas = [_as_float(row.get("area_sqm")) for row in carriers]
    if len(hoods) != 1 or any(p is None for p in prices) or any(a is None for a in areas):
        return False
    price_lo, price_hi = min(prices), max(prices)  # type: ignore[type-var]
    area_lo, area_hi = min(areas), max(areas)  # type: ignore[type-var]
    return (
        price_hi - price_lo <= PRICE_REL_TOLERANCE * price_hi
        and _area_close(area_lo, area_hi)
    )


def boilerplate_hashes(listings: Sequence[dict]) -> frozenset[int]:
    """Full hashes on ≥ 5 listings whose carriers do not all describe the same flat."""
    carriers: dict[int, list[int]] = {}
    for index, listing in enumerate(listings):
        for photo in _parse_listing_photos(listing):
            if photo.fingerprint.full is not None:
                carriers.setdefault(photo.fingerprint.full, []).append(index)
    boilerplate: set[int] = set()
    for value, indices in carriers.items():
        unique = sorted(set(indices))
        if len(unique) >= BOILERPLATE_MIN_LISTINGS and not _consistent([listings[i] for i in unique]):
            boilerplate.add(value)
    return frozenset(boilerplate)


# ── Collapse ──────────────────────────────────────────────────────────────────

def _candidate_pairs(listings: Sequence[dict]) -> list[tuple[int, int]]:
    by_hood: dict[str, list[int]] = {}
    for index, listing in enumerate(listings):
        by_hood.setdefault(_neighborhood(listing), []).append(index)
    pairs: list[tuple[int, int]] = []
    # Within a neighborhood every pair is assessed: a gate failure may still be a review case.
    for indices in by_hood.values():
        for position, left in enumerate(indices):
            for right in indices[position + 1:]:
                pairs.append((left, right))
    # Across names only nearby ads whose area, price and floor already agree: overlapping
    # labels for one flat, never a review case on their own.
    located = sorted(
        (coords[0], index) for index, coords in
        ((index, _coords(listing)) for index, listing in enumerate(listings)) if coords is not None
    )
    lat_window = NEARBY_M / 111_000.0
    for position, (lat, left) in enumerate(located):
        for other_lat, right in located[position + 1:]:
            if other_lat - lat > lat_window:
                break
            a, b = listings[left], listings[right]
            if _neighborhood(a) == _neighborhood(b) or not _nearby(a, b) or not _gates_hold(a, b):
                continue
            pairs.append((min(left, right), max(left, right)))
    return pairs


def _complete_link(n: int, edges: dict[tuple[int, int], PairAssessment], strength) -> list[list[int]]:
    clusters: dict[int, set[int]] = {i: {i} for i in range(n)}
    member = list(range(n))
    for (left, right) in sorted(edges, key=strength):
        root_l, root_r = member[left], member[right]
        if root_l == root_r:
            continue
        cross = [tuple(sorted((x, y))) for x in clusters[root_l] for y in clusters[root_r]]
        if all(pair in edges for pair in cross):
            keep, drop = min(root_l, root_r), max(root_l, root_r)
            clusters[keep] |= clusters.pop(drop)
            for item in clusters[keep]:
                member[item] = keep
    return sorted((sorted(group) for group in clusters.values()), key=lambda group: group[0])


def _offers(listing: dict) -> list[dict]:
    existing = listing.get("advertised_offers")
    if existing:
        return [dict(offer) for offer in existing]
    return [{
        "url": listing.get("url"),
        "source": listing.get("source"),
        "price_eur": _as_float(listing.get("price_eur")),
        "area_sqm": _as_float(listing.get("area_sqm")),
        "floor_class": listing.get("floor_class"),
    }]


def _missing(value: object) -> bool:
    """No value: ``None`` or blank text (Spitogatos stores an ad without a description as "")."""
    return value is None or (isinstance(value, str) and not value.strip())


def _merge_group(members: list[dict]) -> dict:
    def basis_key(row: dict) -> tuple:
        price = _as_float(row.get("price_eur"))
        return (price is None, price if price is not None else 0.0, _url(row))

    ordered = sorted(members, key=basis_key)
    basis = dict(ordered[0])  # lowest asking price; its own area stays paired with it

    offers: dict[str, dict] = {}
    source_urls: set[str] = set()
    photos: list[str] = []
    fingerprints: dict = {}
    sources: set[str] = set()
    for row in ordered:
        for offer in _offers(row):
            offers.setdefault(str(offer.get("url")), offer)
        source_urls.update(str(url) for url in (row.get("source_urls") or [row.get("url")]) if url)
        for url in row.get("photo_urls") or []:
            if url not in photos:
                photos.append(url)
        fingerprints.update(row.get("photo_fingerprints") or {})
        sources.update(part.strip() for part in str(row.get("source") or "").split("&") if part.strip())
        for name in _FILL_FIELDS:
            if _missing(basis.get(name)) and not _missing(row.get(name)):
                basis[name] = row[name]

    floor_classes = {row.get("floor_class") for row in ordered if row.get("floor_class")}
    adjacent = any(row.get("floor_conflict_adjacent") for row in ordered)
    if floor_classes == _ADJACENT_FLOORS:
        basis["floor"], basis["floor_class"], adjacent = -1.0, "basement", True
    elif basis.get("floor_class") is None and len(floor_classes) == 1:
        donor = next(row for row in ordered if row.get("floor_class"))
        basis["floor"], basis["floor_class"] = donor.get("floor"), donor.get("floor_class")
    basis["floor_conflict_adjacent"] = adjacent
    # The flat has been on sale since its oldest advertisement, whichever agency's is cheapest.
    published = [
        str(value)[:10] for row in ordered
        for value in (row.get("earliest_publication_date") or row.get("publication_date"),) if value
    ]
    if published:
        basis["earliest_publication_date"] = min(published)
    basis["advertised_offers"] = sorted(offers.values(), key=lambda o: (o.get("price_eur") is None, o.get("price_eur") or 0, str(o.get("url"))))
    basis["source_urls"] = sorted(str(url) for url in source_urls if url)
    basis["photo_urls"] = photos
    basis["photos_count"] = len(photos)
    basis["photo_fingerprints"] = fingerprints
    basis["source"] = " & ".join(sorted(sources)) if sources else basis.get("source")
    return basis


def _member_urls(listing: dict) -> set[str]:
    urls = listing.get("source_urls") or [listing.get("url")]
    return {normalize_listing_url(url) or str(url) for url in urls if url}


def _blocked(a: dict, b: dict, blocked_components: Sequence[frozenset[str]]) -> bool:
    """True when the first pass refused to join these ads (incomplete HIGH clique).

    A merged survivor carries every member's photos, so without this a bridge that the
    conservative pass blocked would be re-joined here.
    """
    left, right = _member_urls(a), _member_urls(b)
    return any(
        (left & component) and (right & component) and not (left & right)
        for component in blocked_components
    )


def collapse_opportunities(
    listings: list[dict],
    decisions: dict[str, dict],
    *,
    blocked_components: Sequence[frozenset[str]] = (),
    is_tracked: Callable[[dict], bool] | None = None,
) -> OpportunityCollapseResult:
    """``is_tracked``: when given, a review pair with an ad nothing follows any more (XE while
    it is not collected, a Spitogatos ad gone from the site) is dropped instead of marking both
    sides pending — it cannot be resolved and would only hide the live flat."""
    boilerplate = boilerplate_hashes(listings)
    photos = [_photos(listing, boilerplate) for listing in listings]

    same_edges: dict[tuple[int, int], PairAssessment] = {}
    review_pairs: list[tuple[int, int, PairAssessment]] = []
    for left, right in _candidate_pairs(listings):
        a, b = listings[left], listings[right]
        key = pair_key(a, b)
        assessment = assess_pair(a, b, photos[left], photos[right])
        decision = decisions.get(key)
        if decision and decision.get("identity") == identity_fingerprint(a, b):
            if decision["decision"] == "same":
                same_edges[(left, right)] = assessment
            continue
        if assessment.same and _blocked(a, b, blocked_components):
            continue  # already in the first pass's bridge review
        if assessment.same:
            same_edges[(left, right)] = assessment
        elif assessment.review_reason is not None:
            if is_tracked is not None and not (is_tracked(a) and is_tracked(b)):
                continue
            review_pairs.append((left, right, assessment))

    def strength(pair: tuple[int, int]) -> tuple:
        item = same_edges[pair]
        return (-item.photo_matches, -item.near_photo_matches, -(item.description_similarity or 0.0), pair_key(listings[pair[0]], listings[pair[1]]))

    groups = _complete_link(len(listings), same_edges, strength)
    group_of = {index: gi for gi, group in enumerate(groups) for index in group}

    pending_groups: set[int] = set()
    reviews: list[dict] = []
    for left, right, assessment in review_pairs:
        if group_of[left] == group_of[right]:
            continue
        pending_groups.update({group_of[left], group_of[right]})
        a, b = listings[left], listings[right]
        reviews.append({
            "pair_key": pair_key(a, b),
            "urls": sorted((_url(a), _url(b))),
            "reason": assessment.review_reason,
            "signals": list(assessment.signals),
            "photo_matches": assessment.photo_matches,
            "near_photo_matches": assessment.near_photo_matches,
            "description_similarity": assessment.description_similarity,
            "identity": identity_fingerprint(a, b),
            "fields": {
                _url(row): {
                    "price_eur": row.get("price_eur"), "area_sqm": row.get("area_sqm"),
                    "floor_class": row.get("floor_class"), "neighborhood": row.get("neighborhood"),
                }
                for row in (a, b)
            },
        })

    output: list[dict] = []
    merged_records: list[dict] = []
    for gi, group in enumerate(groups):
        members = [listings[index] for index in group]
        merged = _merge_group(members) if len(members) > 1 else dict(members[0])
        if len(members) == 1:
            merged.setdefault("advertised_offers", _offers(merged))
            merged.setdefault("source_urls", [merged.get("url")])
            merged.setdefault("floor_conflict_adjacent", bool(merged.get("floor_conflict_adjacent")))
        merged["duplicate_review_status"] = "pending" if gi in pending_groups else "none"
        if len(members) > 1:
            merged_records.append({
                "kept_url": merged.get("url"),
                "member_urls": sorted(_url(row) for row in members),
                "policy": "opportunity-v1",
            })
        output.append(merged)
    reviews.sort(key=lambda record: record["pair_key"])
    return OpportunityCollapseResult(listings=output, merged=merged_records, reviews=reviews)
