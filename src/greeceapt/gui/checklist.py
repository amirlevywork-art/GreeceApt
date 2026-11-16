"""
"Before you buy", pre-filled from what is already known about a flat.

Each item says what the ad text, the image model's photo verdicts or the rentals nearby show —
``found`` (the evidence answers it, still to confirm with papers), ``warn`` (the evidence points
to a problem) or ``unknown`` (nothing on it: ask). Nothing here is a legal check: the permit, the
approved use and the energy certificate are only settled by the documents.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence

from greeceapt.collect.floor_resolver import normalize_text

BELOW_GROUND = {"basement", "semi_basement"}
COMPARABLE_RADIUS_M = 600.0
COMPARABLE_AREA_SHARE = 0.30        # rentals within ±30 % of the flat's area
MAX_COMPARABLES = 6

# normalized text (casefolded, accent-free, Greek final sigma "σ")
_LEGAL_RE = re.compile(
    r"legali[sz]ed|regulari[sz]ed|building permit|with (?:a )?permit|τακτοποιημεν\w*|νομιμοποιημεν\w*"
    r"|4495|οικοδομικη αδεια|αδεια δομησησ|main use|κυρια χρηση|κυριασ χρησησ"
)
_AUXILIARY_RE = re.compile(r"βοηθητικ\w*|auxiliary (?:space|use)|storage use|used as (?:a )?storage|ωσ αποθηκ\w*|not (?:a )?residen\w*")
_COMMON_RE = re.compile(r"κοινοχρηστ\w*|common (?:charges|expenses|costs|fees)|communal (?:charges|expenses|fees)|building fees")
_AMOUNT_RE = re.compile(r"(?:€\s*(\d{1,3}(?:[.,]\d{1,2})?)|(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|ευρω|euro|eur\b))")
_LOW_COMMON_RE = re.compile(
    r"(?:low|minimal|no|without|χαμηλα|ελαχιστα|χωρισ|μηδενικα)\s+(?:\w+\s+)?(?:common|communal|κοινοχρηστ)")
_DAMP_RE = re.compile(r"υγρασι\w*|humidity|\bdamp\w*|moisture|mou?ld|μουχλ\w*")
# words for light and air only ("double glazed windows" is a feature, not daylight)
_LIGHT_RE = re.compile(r"\bbright\b|sunny|\bairy\b|φωτειν\w*|ηλιολουστ\w*|διαμπερ\w*|ευαερ\w*|through[- ]ventilat\w*|cross[- ]ventilat\w*")
_HEIGHT_RE = re.compile(r"(?:height|υψοσ|υψουσ)\D{0,15}(\d[.,]\d{1,2})\s*(?:m\b|μ\b|meters|μετρ)")


def _snippet(text: str, match: re.Match[str], width: int = 60) -> str:
    start, end = max(0, match.start() - width), min(len(text), match.end() + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _item(key: str, label: str, status: str, note: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "note": note}


# ── rentals nearby ─────────────────────────────────────────────────────────────

_rentals_cache: dict[str, Any] = {"mtime": None, "rows": []}


def _rentals(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    if _rentals_cache["mtime"] != mtime:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT url, rent_eur, area_sqm, floor_class, latitude, longitude, furnished, last_seen_at "
                "FROM listings WHERE latitude IS NOT NULL AND rent_eur IS NOT NULL AND area_sqm IS NOT NULL")]
        _rentals_cache.update(mtime=mtime, rows=rows)
    return _rentals_cache["rows"]


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 110_570.0
    dx = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def comparable_rentals(lat: float | None, lon: float | None, area: float | None, floor_class: str | None,
                       *, rentals_db: Path | None = None) -> list[dict[str, Any]]:
    """Advertised rentals of a similar flat nearby: same side of street level, ±30 % area, ≤600 m."""
    from greeceapt.rent import config as rent_cfg

    if lat is None or lon is None or not area:
        return []
    below = floor_class in BELOW_GROUND
    found = []
    for rental in _rentals(rentals_db or rent_cfg.RENTALS_DB):
        if (rental.get("floor_class") in BELOW_GROUND) != below:
            continue
        if abs(float(rental["area_sqm"]) - area) > COMPARABLE_AREA_SHARE * area:
            continue
        distance = _distance_m(lat, lon, float(rental["latitude"]), float(rental["longitude"]))
        if distance <= COMPARABLE_RADIUS_M:
            found.append({**rental, "distance_m": round(distance)})
    found.sort(key=lambda r: (abs(float(r["area_sqm"]) - area) / area + r["distance_m"] / COMPARABLE_RADIUS_M))
    return found


# ── the checklist ──────────────────────────────────────────────────────────────

def prefill(deal: Mapping[str, Any], listing: Mapping[str, Any], verdicts: Sequence[Mapping[str, Any]],
            *, rentals_db: Path | None = None) -> list[dict[str, Any]]:
    """Items with what is known; ``verdicts`` are the image model's per-photo verdicts."""
    text = normalize_text(" ".join(str(x) for x in (listing.get("title"), listing.get("description")) if x))
    below = deal.get("floor_class") in BELOW_GROUND
    items: list[dict[str, Any]] = []

    # permit, approved use, legalization
    legal = _LEGAL_RE.search(text)
    auxiliary = _AUXILIARY_RE.search(text)
    if auxiliary:
        items.append(_item("permit", "Building permit and approved use", "warn",
                           f"The ad hints it may not be a home on paper: “{_snippet(text, auxiliary)}”. Ask for the permit."))
    elif legal:
        items.append(_item("permit", "Building permit and approved use", "found",
                           f"The ad mentions it: “{_snippet(text, legal)}”. Confirm with the permit and the legalization file."))
    else:
        items.append(_item("permit", "Building permit and approved use", "unknown",
                           "Not mentioned in the ad. Ask for the building permit"
                           + (" and proof the space is approved as a home (κύρια χρήση), or legalized under Law 4495/2017." if below else ".")))

    # energy certificate
    energy = str(listing.get("energy_class") or "").strip().upper()
    if energy == "EXEMPT":
        items.append(_item("energy", "Energy performance certificate (ΠΕΑ)", "found",
                           "The ad says the flat is exempt from an energy certificate. Ask on what grounds."))
    elif energy and energy not in {"N/A", "NA", "NONE", "-", "UNKNOWN"}:
        items.append(_item("energy", "Energy performance certificate (ΠΕΑ)", "found",
                           f"The ad states energy class {energy}, so a certificate should exist. Ask for its number."))
    else:
        items.append(_item("energy", "Energy performance certificate (ΠΕΑ)", "unknown",
                           "No energy class in the ad. A certificate is required to sell: ask whether it exists."))

    # communal charges
    common = _COMMON_RE.search(text)
    if common:
        window = text[max(0, common.start() - 40):common.end() + 60]
        amount = _AMOUNT_RE.search(window)
        low = _LOW_COMMON_RE.search(window)
        if amount:
            value = (amount.group(1) or amount.group(2)).replace(",", ".")
            items.append(_item("common", "Monthly communal charges (κοινόχρηστα)", "found",
                               f"The ad gives about €{value}: “{_snippet(text, common, 50)}”. Ask for the last statements and any building debts."))
        else:
            items.append(_item("common", "Monthly communal charges (κοινόχρηστα)", "found" if low else "unknown",
                               (f"The ad says they are low: “{_snippet(text, common, 50)}”." if low
                                else f"The ad mentions them without an amount: “{_snippet(text, common, 50)}”.")
                               + " Ask for the last statements and any building debts."))
    else:
        items.append(_item("common", "Monthly communal charges (κοινόχρηστα)", "unknown",
                           "Not in the ad. Ask the building manager for the monthly amount and any debts of the flat."))

    # damp — the photos and the text
    damaged = [v for v in verdicts if (v.get("visible_damage") == "major") or (v.get("wall_condition") == "damaged")]
    damp = _DAMP_RE.search(text)
    if damp:
        items.append(_item("damp", "Damp and moisture", "warn", f"The ad mentions it: “{_snippet(text, damp)}”."))
    elif damaged:
        items.append(_item("damp", "Damp and moisture", "warn",
                           f"The image model saw major damage or damaged walls in {len(damaged)} of {len(verdicts)} photos. Look for damp on a visit."))
    elif verdicts:
        items.append(_item("damp", "Damp and moisture", "found" if not below else "unknown",
                           f"No major damage in the {len(verdicts)} photos the image model read"
                           + (" — still check a below-ground flat on a visit, after rain if possible." if below else ".")))

    # light, ventilation, height — mainly for below-ground flats
    if below:
        light = _LIGHT_RE.search(text)
        height = _HEIGHT_RE.search(text)
        notes = []
        if light:
            notes.append(f"the ad says “{_snippet(text, light, 40)}”")
        if height:
            notes.append(f"a height of {height.group(1).replace(',', '.')} m is mentioned")
        items.append(_item("light", "Clear height ≥ 2.50 m, natural light and ventilation", "found" if notes else "unknown",
                           ("; ".join(notes).capitalize() + ". Measure the height on a visit.") if notes
                           else "Nothing in the ad about light, windows or height. Check on a visit (the Building Code asks ≥ 2.50 m)."))

    # renovation — what the photos showed
    if deal.get("renovation_level") and deal.get("renovation_level") != "none":
        kitchen = [v.get("kitchen_age") for v in verdicts if v.get("kitchen_age") not in (None, "unclear", "not_applicable")]
        bathroom = [v.get("bathroom_age") for v in verdicts if v.get("bathroom_age") not in (None, "unclear", "not_applicable")]
        parts = []
        if kitchen:
            parts.append(f"kitchen {max(set(kitchen), key=kitchen.count)}")
        if bathroom:
            parts.append(f"bathroom {max(set(bathroom), key=bathroom.count)}")
        renovation = deal.get("renovation_eur") or 0
        items.append(_item("renovation", "Contractor quote for the renovation", "warn",
                           f"The ranking budgets €{renovation:,.0f} ({deal.get('renovation_level')}), from condition "
                           f"{(deal.get('visual_score') or 0):.1f}/10" + (f"; the photos show {', '.join(parts)}" if parts else "")
                           + ". Get a written quote before an offer."))

    # rent — rentals nearby
    lat, lon = listing.get("latitude"), listing.get("longitude")
    try:
        lat_f, lon_f = float(str(lat)), float(str(lon))
    except (TypeError, ValueError):
        lat_f = lon_f = None
    comps = comparable_rentals(lat_f, lon_f, deal.get("area_sqm"), deal.get("floor_class"), rentals_db=rentals_db)
    rent_lo = deal.get("rent_lo_eur")
    if comps:
        rents = [float(c["rent_eur"]) for c in comps]
        median = statistics.median(rents)
        status = "warn" if rent_lo and median < rent_lo else "found"
        many = len(comps) != 1
        note = (f"{len(comps)} similar {'below-ground ' if below else ''}flat{'s were' if many else ' was'} advertised for rent within "
                f"{COMPARABLE_RADIUS_M:.0f} m: median €{median:,.0f} a month asked (signed rents are lower)"
                + (f"; the ranking counts on €{rent_lo:,.0f}." if rent_lo else "."))
        if status == "warn":
            note += " The rent nearby is below what the ranking assumes — check with a local agent."
        items.append({**_item("rent", "Rent check with rentals nearby", status, note),
                      "comparables": [{k: c.get(k) for k in ("url", "rent_eur", "area_sqm", "floor_class", "distance_m", "furnished")}
                                      for c in comps[:MAX_COMPARABLES]]})
    else:
        items.append(_item("rent", "Rent check with rentals nearby", "unknown",
                           "No similar flat advertised for rent within 600 m. Ask a local agent what it would let for."))

    # floor disagreement
    if "floor_conflict_adjacent" in (deal.get("risk_flags") or []):
        items.append(_item("floor", "Confirm the floor", "warn",
                           "The ads disagree between basement and semi-basement; the ranking takes basement."))
    return items


def verdicts_of(features: Mapping[str, Any] | str | None) -> list[dict[str, Any]]:
    """Per-photo verdicts of the image model (accepted photos only)."""
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except ValueError:
            return []
    out = []
    for score in (features or {}).get("image_scores") or []:
        verdict = score.get("verdict") if isinstance(score, Mapping) else None
        if verdict and score.get("status") == "accepted":
            out.append(verdict)
    return out
