"""
A real neighborhood for flats filed under a catch-all area name.

Spitogatos files an advertisement under "Center" when the agent picks no sub-area. Flats are
compared only within their neighborhood, so the seven "Center" flats of Nov 2026 — from Neos
Kosmos, Agios Sostis and Plateia Vathis — were ranked against each other and against none of
their real neighbours. Their coordinates say where they are: each gets the neighborhood most of
its ``K_NEAREST`` nearest advertisements (rentals and other sale flats with a specific name) are
filed under. The advertised name stays in ``neighborhood_listed`` and the vote in
``neighborhood_note`` ("Inferred from location: 9 of the 15 nearest ads are in Kinosargous").
A flat without coordinates keeps its name.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Iterable, MutableMapping, Sequence

import numpy as np

from greeceapt.storage.neighborhoods import canonical_neighborhood
from greeceapt.rent.features import coords_km

logger = logging.getLogger(__name__)

GENERIC_NEIGHBORHOODS = frozenset({"Center"})
K_NEAREST = 15

Reference = tuple[str, float, float]   # (neighborhood, latitude, longitude)


def _coord(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return None if np.isnan(number) else number


def is_generic(name: object) -> bool:
    return canonical_neighborhood(name) in GENERIC_NEIGHBORHOODS


def rental_reference(path: Path | None = None) -> list[Reference]:
    """Named, located rentals from rentals.db (empty when it does not exist)."""
    from greeceapt.rent import config as rent_cfg

    db = path or rent_cfg.RENTALS_DB
    if not db.exists():
        return []
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT neighborhood, latitude, longitude FROM listings WHERE latitude IS NOT NULL").fetchall()
    out = []
    for name, lat, lon in rows:
        hood = canonical_neighborhood(name)
        if hood and hood not in GENERIC_NEIGHBORHOODS and _coord(lat) is not None and _coord(lon) is not None:
            out.append((hood, float(lat), float(lon)))
    return out


def infer_neighborhoods(candidates: Sequence[MutableMapping], extra_reference: Iterable[Reference] = ()) -> int:
    """Re-file catch-all flats under the majority neighborhood of their nearest advertisements."""
    reference: list[Reference] = list(extra_reference)
    for row in candidates:
        lat, lon, hood = _coord(row.get("latitude")), _coord(row.get("longitude")), row.get("neighborhood")
        if hood and not is_generic(hood) and lat is not None and lon is not None:
            reference.append((str(hood), lat, lon))
    targets = [row for row in candidates if is_generic(row.get("neighborhood"))]
    if not targets or len(reference) < K_NEAREST:
        return 0
    names = [name for name, _, _ in reference]
    xy = coords_km(np.array([r[1] for r in reference]), np.array([r[2] for r in reference]))
    changed = 0
    for row in targets:
        lat, lon = _coord(row.get("latitude")), _coord(row.get("longitude"))
        if lat is None or lon is None:
            continue
        here = coords_km(np.array([lat]), np.array([lon]))[0]
        nearest = np.argsort(((xy - here) ** 2).sum(axis=1))[:K_NEAREST]
        votes = Counter(names[i] for i in nearest)
        (winner, count), = votes.most_common(1)
        row["neighborhood_listed"] = row.get("neighborhood")
        row["neighborhood"] = winner
        row["neighborhood_note"] = (
            f"Listed as “{row['neighborhood_listed']}”; inferred from location: "
            f"{count} of the {K_NEAREST} nearest ads are in {winner}"
        )
        changed += 1
    logger.info("Neighborhoods: %s flats filed under a catch-all name re-filed by location.", changed)
    return changed
