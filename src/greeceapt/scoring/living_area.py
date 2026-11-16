"""
The living area of a flat whose listed area adds an attic, a mezzanine, a terrace or storage.

Decided 2026-11-16: "30 square meters of ground-floor space and a 24-square-meter loft" is listed as
54 m², and the rent, the renovation and the property tax were all computed on 54 m². When the ad
names a main area and an extra space whose sum is the listed area, the flat is priced on the main
area. Only that exact sum counts (±1.5 m²): ads mention storage rooms, shared gardens and
balconies that are *not* in the listed area far more often than ones that are.
"""

from __future__ import annotations

import re
from typing import Mapping, MutableMapping

from greeceapt.collect.floor_resolver import normalize_text

SUM_TOLERANCE_SQM = 1.5
MIN_LIVING_SQM = 10.0

_AREA = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:-\s*)?"
    r"(?:τ\.?\s?μ\.?|τετραγωνικ\w*|sq\.?\s?m\.?|sqm|m2|m²|square[- ]met\w*)"
)
_EXTRA = re.compile(
    r"loft|mezzanine|attic|παταρι|σοφιτα|terrace|veranda|balcon\w*|βεραντα|ταρατσα|μπαλκον\w*"
    r"|yard|courtyard|garden|αυλη|κηπο\w*|storage|αποθηκ\w*"
)


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def living_area(description: object, listed_sqm: float | None) -> float | None:
    """The main area when the ad splits ``listed_sqm`` into main + extra space; else ``None``."""
    if not listed_sqm:
        return None
    text = normalize_text(description)
    mains, extras = [], []
    for match in _AREA.finditer(text):
        value = _number(match.group(1))
        around = text[max(0, match.start() - 25):match.end() + 30]
        (extras if _EXTRA.search(around) else mains).append(value)
    for extra in extras:
        for main in mains + extras:
            if main is extra or main < MIN_LIVING_SQM or main >= listed_sqm:
                continue
            if abs(main + extra - listed_sqm) <= SUM_TOLERANCE_SQM and extra < listed_sqm:
                return main
    return None


def apply_living_area(listings: list[MutableMapping]) -> int:
    """Price each flat on its living area; keep the listed one in ``area_listed``. Returns the count."""
    changed = 0
    for listing in listings:
        try:
            listed = float(listing.get("area_sqm"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        main = living_area(listing.get("description"), listed)
        if main is not None:
            listing["area_listed"] = listed
            listing["area_sqm"] = main
            changed += 1
    return changed


def area_note(listing: Mapping) -> str | None:
    listed = listing.get("area_listed")
    return None if listed is None else f"listed as {listed:g} m², of which {listing.get('area_sqm'):g} m² is living space"
