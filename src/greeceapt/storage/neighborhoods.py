"""
Canonical neighborhood names.

XE and Spitogatos spell the same area differently (Kypseli / Kipseli, Amerikis Square /
Plateia Amerikis, …). Canonical keys follow the Spitogatos/Indomio area taxonomy, which is
also the unit flats are ranked within. The mapping is an explicit, reviewed alias table — never
fuzzy matching; names without an alias pass through unchanged.

CLI:  python -m greeceapt.storage.neighborhoods --report PATH_TO_DB
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("neighborhood_aliases_v1.json")
_SPACE_RE = re.compile(r"\s+")
_HYPHEN_RE = re.compile(r"\s*-\s*")


def _key(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")
    return _SPACE_RE.sub(" ", _HYPHEN_RE.sub(" - ", text)).strip()


def load_aliases(path: Path = DEFAULT_PATH) -> dict[str, str]:
    """``{raw name: canonical name}``; rejects chains so the mapping stays idempotent."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("aliases"), dict):
        raise ValueError(f"unsupported neighborhood alias file: {path}")
    aliases = {str(k): str(v) for k, v in payload["aliases"].items()}
    keys = {_key(alias) for alias in aliases}
    for alias, target in aliases.items():
        if _key(target) in keys and _key(target) != _key(alias):
            raise ValueError(f"alias chain: {alias!r} -> {target!r} is itself an alias")
    return aliases


@lru_cache(maxsize=1)
def _lookup() -> dict[str, str]:
    return {_key(alias): target for alias, target in load_aliases().items()}


def canonical_neighborhood(name: object) -> str | None:
    if name is None:
        return None
    text = _SPACE_RE.sub(" ", str(name)).strip()
    if not text:
        return None
    return _lookup().get(_key(text), text)


# ── Neighborhood tiers ────────────────────────────────────────────────────────
# The hand-made location tier of each central-Athens neighborhood, keyed by canonical name.
# The vision golden set stratifies on it; the GUI's neighborhood table shows it.

_TIER_MAP_AS_WRITTEN: dict[str, str | None] = {
    # --- Tier 5: Elite & Maximum Safety ---
    "Plaka":             "Tier_5_Elite_BlueChip",  # tourist core, peak safety/demand
    "Kolonaki":          "Tier_5_Elite_BlueChip",
    "Lycabettus":        "Tier_5_Elite_BlueChip",
    "Koukaki":           "Tier_5_Elite_BlueChip",
    "Kynosargous":       "Tier_5_Elite_BlueChip",  # next to Koukaki, strong demand
    "Mets":              "Tier_5_Elite_BlueChip",
    "First Cemetery":    "Tier_5_Elite_BlueChip",  # Mets-adjacent, premium & quiet
    "Hilton":            "Tier_5_Elite_BlueChip",

    # --- Tier 4: Hip & High Demand ---
    "Pagkrati":          "Tier_4_Hip_Urban",
    "Varnava":           "Tier_4_Hip_Urban",  # Pagkrati core, high demand
    "Ano Petralona":     "Tier_4_Hip_Urban",  # desirable pocket
    "Kato Petralona":    "Tier_4_Hip_Urban",
    "Neos Kosmos":       "Tier_4_Hip_Urban",
    "Agios Ioannis":     "Tier_4_Hip_Urban",  # metro-adjacent, solid demand
    "Exarcheia":         "Tier_4_Hip_Urban",  # strong student demand + transit
    "Metaxourgeio":      "Tier_4_Hip_Urban",
    "Keramikos":         "Tier_4_Hip_Urban",
    "Gouva":             "Tier_4_Hip_Urban",
    "Lambrakis Hill":    "Tier_4_Hip_Urban",  # Neos Kosmos / Pagkrati views
    "Mouseio":           "Tier_4_Hip_Urban",

    # --- Tier 3: Prime & Safe Residential ---
    "Dafni":             "Tier_3_Prime_Residential",  # family-friendly, metro
    "Kallithea":         "Tier_3_Prime_Residential",
    "Zografou":          "Tier_3_Prime_Residential",
    "Ilisia":            "Tier_3_Prime_Residential",
    "Ampelokipoi":       "Tier_3_Prime_Residential",
    "Nea Chalkidona":    "Tier_3_Prime_Residential",  # quiet residential pocket
    "Gyzi":              "Tier_3_Prime_Residential",
    "Vyronas":           "Tier_3_Prime_Residential",
    "Agia Paraskevi":    "Tier_3_Prime_Residential",
    "Nea Smyrni":        "Tier_3_Prime_Residential",
    "Kaisariani":        "Tier_3_Prime_Residential",

    # --- Tier 2: Developing / Growth ---
    "Kypseli":           "Tier_2_Growth_Hub",
    "Ano Kypseli":       "Tier_2_Growth_Hub",
    "Nea Kypseli":       "Tier_2_Growth_Hub",
    "Amerikis Square":   "Tier_2_Growth_Hub",
    "Pedion tou Areos":  "Tier_2_Growth_Hub",
    "Peristeri":         "Tier_2_Growth_Hub",
    "Galatsi":           "Tier_2_Growth_Hub",
    "Nirvana":           "Tier_2_Growth_Hub",
    "Ano Patisia":       "Tier_2_Growth_Hub",

    # --- Tier 1: High Risk / Budget ---
    "Omonia":            "Tier_1_Budget_Central",
    "Victoria Square":   "Tier_1_Budget_Central",
    "Agios Panteleimonas":"Tier_1_Budget_Central",
    "Attica Square":     "Tier_1_Budget_Central",
    "Larissis station":  "Tier_1_Budget_Central",
    "Stathmos Larissis": "Tier_1_Budget_Central",
    "Kolonos":           "Tier_1_Budget_Central",
    "Akadimia Platonos": "Tier_1_Budget_Central",
    "Kato Patisia":      "Tier_1_Budget_Central",
    "Agios Nikolaos":    "Tier_1_Budget_Central",
    "Ipirou":            "Tier_1_Budget_Central",

    # ── Spitogatos / alternate transliteration variants ──────────────────────
    # Kipseli / Kypseli variants (Greek Κυψέλη)
    "Kipseli":                   "Tier_2_Growth_Hub",
    "Ano Kipseli":               "Tier_2_Growth_Hub",
    "Nea Kipseli":               "Tier_2_Growth_Hub",
    "Plateia Kipselis":          "Tier_2_Growth_Hub",
    "Fokionos Negri":            "Tier_2_Growth_Hub",  # pedestrian street in Kypseli
    "Gkrava":                    "Tier_2_Growth_Hub",  # sub-area of Kypseli

    # Victoria / Viktorias Square area
    "Viktorias Square":          "Tier_1_Budget_Central",
    "Plateia Attikis":           "Tier_1_Budget_Central",
    "Attiki":                    "Tier_1_Budget_Central",
    "Plateia Vathis":            "Tier_1_Budget_Central",  # Vathis Square, central
    "Agiou Meletiou - Victorias Square - Marni": "Tier_1_Budget_Central",
    "Evelpidon - Dikastiria":    "Tier_1_Budget_Central",  # court district near Omonia
    "Leoforos Patision - Leoforos Acharnon": "Tier_1_Budget_Central",
    "Sepolia":                   "Tier_1_Budget_Central",
    "Thimarakia":                "Tier_1_Budget_Central",  # near Ano Patisia
    "Patisia":                   "Tier_1_Budget_Central",
    "Stathmos Larisis":          "Tier_1_Budget_Central",  # variant of Larissis station
    "akadimia Platonos":         "Tier_1_Budget_Central",  # lowercase from Spitogatos

    # Amerikis / Plateia Amerikis
    "Plateia Amerikis":          "Tier_2_Growth_Hub",
    "Plateia Koliatsou":         "Tier_2_Growth_Hub",
    "Koliatsou":                 "Tier_2_Growth_Hub",
    "Kipriadou - Ano Patisia":   "Tier_2_Growth_Hub",

    # Gyzi / Gizi variants
    "Gizi - Pedion Areos":       "Tier_2_Growth_Hub",
    "Plateia Gkizi":             "Tier_3_Prime_Residential",
    "Gkyzi - Arios Pagos":       "Tier_3_Prime_Residential",

    # Ilisia area
    "Kato Ilisia":               "Tier_3_Prime_Residential",
    "Panormou":                  "Tier_3_Prime_Residential",  # Ampelokipoi sub-area

    # Exarcheia variants
    "Exarchia - Neapoli":        "Tier_4_Hip_Urban",
    "Neapoli Exarcheion":        "Tier_4_Hip_Urban",
    "Pagkrati Center":           "Tier_4_Hip_Urban",
    "Agios Eleftherios":         "Tier_4_Hip_Urban",  # near Pagkrati

    # Koukaki compound name
    "Koukaki - Pediki Chara":    "Tier_5_Elite_BlueChip",

    # Exceptions
    "130":               None,
}


def _canonical_tier_map(raw: dict[str, str | None]) -> dict[str, str | None]:
    """Key the tier map by canonical neighborhood names; spelling variants must agree."""
    merged: dict[str, str | None] = {}
    for name, tier in raw.items():
        key = canonical_neighborhood(name) or name
        if key in merged and merged[key] is not None and tier is not None and merged[key] != tier:
            raise ValueError(f"tier conflict for {key!r}: {merged[key]} vs {tier} (from {name!r})")
        if merged.get(key) is None:
            merged[key] = tier
    return merged


NEIGHBORHOOD_CANONICAL: dict[str, str | None] = _canonical_tier_map(_TIER_MAP_AS_WRITTEN)


def report(db_path: Path) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        counts = Counter(name for (name,) in conn.execute("SELECT neighborhood FROM listings") if name)
    finally:
        conn.close()
    lines = [f"{len(counts)} raw names → {len({canonical_neighborhood(n) for n in counts})} canonical"]
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        canonical = canonical_neighborhood(name)
        lines.append(f"  {count:4d}  {name}" + (f"  →  {canonical}" if canonical != name else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Neighborhood alias coverage report.")
    parser.add_argument("--report", type=Path, required=True)
    print(report(parser.parse_args(argv).report))


if __name__ == "__main__":
    main()
