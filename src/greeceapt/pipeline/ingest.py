import sys
import json
from pathlib import Path
from typing import Any

from greeceapt.db import create_tables, insert_listings
from greeceapt.utils.helpers import AREA_PREFIXES, extract_area_prefix, strip_area_prefix

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# -----------------------------------------
# Neighborhood canonicalization (fix typos/variants)
# -----------------------------------------

NEIGHBORHOOD_CANONICAL = {
    # ------------------------------------------------------------
    # Cleanups / junk values (map to None so you can drop/flag them)
    # ------------------------------------------------------------
    "130": None,
    "Center": None,

    # ------------------------------------------------------------
    # Kypseli cluster
    # ------------------------------------------------------------
    "Kypseli": "Kypseli",
    "Kipseli": "Kypseli",                  # typo/alt spelling
    "Amerikis Square": "Kypseli",
    "Papadiamantis Square": "Kypseli",
    "Pedion tou Areos": "Kypseli",
    "Agios Nikolaos": "Kypseli",
    "Mpaknana": "Kypseli",

    # ------------------------------------------------------------
    # Agios Panteleimonas / Attiki Square / Victoria / Larissis area
    # ------------------------------------------------------------
    "Agios Panteleimonas": "Agios Panteleimonas",
    "Attica Square": "Agios Panteleimonas",
    "Viktorias Square": "Agios Panteleimonas",
    "Vathis Square": "Agios Panteleimonas",
    "Larissis station": "Agios Panteleimonas",
    "Ipirou": "Agios Panteleimonas",

    # ------------------------------------------------------------
    # Patissia / nearby north-center
    # ------------------------------------------------------------
    "Patisia": "Patisia",
    "Agios Eleftherios": "Patisia",
    "Tris Gefires": "Patisia",
    "Rizoupoli": "Patisia",
    "Prompona": "Patisia",
    "Lambrini": "Patisia",
    "Filadelfeia": "Patisia",

    # ------------------------------------------------------------
    # Central Athens
    # ------------------------------------------------------------
    "Omonia": "Omonia",
    "Metaxourgeio": "Metaxourgeio",
    "Keramikos": "Keramikos",
    "Kolokinthou": "Metaxourgeio",         # treat as Metaxourgeio/Kolonos side
    "Mouseio": "Exarcheia",                # close academic/exarchia-ish market
    "Exarcheia": "Exarcheia",
    "Neapoli": "Exarcheia",
    "Strefi Hill": "Exarcheia",
    "Lycabettus": "Kolonaki",              # market behaves closer to Kolonaki
    "Kolonaki": "Kolonaki",
    "Hilton": "Kolonaki",
    "Ippokratous": "Exarcheia",

    # ------------------------------------------------------------
    # West / NW Athens
    # ------------------------------------------------------------
    "Kolonos": "Kolonos",
    "Skouze Hill": "Kolonos",
    "Akadimia Platonos": "Kolonos",
    "Sepolia": "Kolonos",
    "Egaleo": "Kolonos",
    "Peristeri": "Peristeri",
    "Agioi Anargyroi": "Peristeri",
    "Chalkidona": "Peristeri",
    "Ilion": "Peristeri",

    # ------------------------------------------------------------
    # Ampelokipoi / Gkyzi / Polygono / Ilisia
    # ------------------------------------------------------------
    "Ampelokipoi": "Ampelokipoi",
    "Panormou": "Ampelokipoi",
    "Gyzi": "Ampelokipoi",
    "Polygono": "Ampelokipoi",
    "Ellinoroson": "Ampelokipoi",
    "Ilisia": "Ilisia",
    "Agios Thomas": "Ilisia",

    # ------------------------------------------------------------
    # Zografou / Ymittos
    # ------------------------------------------------------------
    "Zografou": "Zografou",
    "Kaisariani": "Kaisariani",
    "Ymittos": "Zografou",
    "Girokomeio": "Zografou",
    "Agios Loukas": "Zografou",

    # ------------------------------------------------------------
    # Pagkrati / Vyronas / Gouva
    # ------------------------------------------------------------
    "Pagkrati": "Pagkrati",
    "Vyronas": "Vyronas",
    "Gouva": "Vyronas",
    "Agios Artemios": "Vyronas",

    # ------------------------------------------------------------
    # Kallithea / Tavros / Dourgouti / Neos Kosmos
    # ------------------------------------------------------------
    "Kallithea": "Kallithea",
    "Tavros": "Kallithea",
    "Dourgouti": "Kallithea",
    "Neos Kosmos": "Neos Kosmos",
    "Agios Sostis": "Neos Kosmos",
    "Kallirrois": "Neos Kosmos",

    # ------------------------------------------------------------
    # South / Petralona / Koukaki
    # ------------------------------------------------------------
    "Petralona": "Petralona",
    "Koukaki": "Koukaki",

    # ------------------------------------------------------------
    # Northern suburbs / other
    # ------------------------------------------------------------
    "Agia Paraskevi": "Agia Paraskevi",
    "Galatsi": "Galatsi",
    "Agia Eleousa": "Galatsi",
    "Nirvana": "Galatsi",
    "Koliatsou": "Koliatsou",
    "Alepotrypa": "Koliatsou",
    "Erythros": "Koliatsou",
    "Kountouriotika": "Koliatsou",
    "Nosokomeio Paidon": "Koliatsou",
    "Profitis Ilias": "Koliatsou",

    # ------------------------------------------------------------
    # Items I’m not confident about (keep as-is for now)
    # ------------------------------------------------------------
    "Ionia": "Ionia",
    "Smyrni": "Smyrni",
    "Lambrakis Hill": "Lambrakis Hill",
}


def normalize_neighborhood_name(name: str | None) -> str | None:
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    return NEIGHBORHOOD_CANONICAL.get(s, s)


def normalize_neighborhood_fields(item: dict[str, Any]) -> None:
    """
    Enforce your rule:
    - neighborhood: canonical name without Ano/Kato/Nea/Neo
    - area: stores ONLY that prefix (or None)
    - Agia/Agios/Agioi are NOT prefixes, so we do nothing special for them.
    - ALSO: unify spelling variants (Kipseli -> Kypseli)
    """
    nb = item.get("neighborhood")
    if not isinstance(nb, str) or not nb.strip():
        return

    prefix = extract_area_prefix(nb)
    base = strip_area_prefix(nb)

    # Move prefix into item["area"] and strip it from neighborhood
    if prefix:
        item["area"] = item.get("area") or prefix
        item["neighborhood"] = base

    # Apply canonicalization AFTER prefix strip (important)
    item["neighborhood"] = normalize_neighborhood_name(item.get("neighborhood"))

# -----------------------------------------
# Read JSON
# -----------------------------------------

def load_json(path: str | None = None) -> list[dict[str, Any]]:
    if path is None:
        default_path = PROJECT_ROOT / "data" / "listings.json"
        print(f"[INFO] Using default JSON: {default_path}")
        path = str(default_path)

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"[ERROR] JSON file not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("[ERROR] JSON must contain a list")

    print(f"[INFO] Loaded {len(data)} listings from {p}")
    return data

# -----------------------------------------
# MAIN
# -----------------------------------------

def main() -> None:
    json_path = sys.argv[1] if len(sys.argv) > 1 else None
    listings = load_json(json_path)

    for item in listings:
        normalize_neighborhood_fields(item)

    insert_listings(listings)

    print("[INFO] Ingestion complete! Database updated ✔")

if __name__ == "__main__":
    main()
