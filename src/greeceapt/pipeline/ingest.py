import sys
import json
from pathlib import Path
from typing import Any

from greeceapt.db import insert_listings
from greeceapt.utils.helpers import extract_area_prefix, strip_area_prefix

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# -----------------------------------------
# Neighborhood canonicalization (fix typos/variants)
# -----------------------------------------

NEIGHBORHOOD_CANONICAL = {
    # ------------------------------------------------------------
    # CLUSTER 1: Kypseli-Patisia (High Yield & Metro 4 Growth)
    # אשכול התשואה הגבוהה ביותר - שוק מאוחד סביב קו המטרו החדש
    # ------------------------------------------------------------
    "Kypseli": "Kypseli",
    "Kipseli": "Kypseli",
    "Amerikis Square": "Kypseli",
    "Papadiamantis Square": "Kypseli",
    "Pedion tou Areos": "Kypseli",
    "Agios Nikolaos": "Kypseli",
    "Mpaknana": "Kypseli",
    "Koliatsou": "Kypseli",
    "Alepotrypa": "Kypseli",
    "Ano Kypseli": "Kypseli",
    "Nirvana": "Kypseli",
    "Nirvanav": "Kypseli",
    "Patisia": "Kypseli",
    "Kato Patisia": "Kypseli",
    "Ano Patisia": "Kypseli",
    "Agios Eleftherios": "Kypseli",
    "Tris Gefires": "Kypseli",
    "Rizoupoli": "Kypseli",
    "Prompona": "Kypseli",
    "Lambrini": "Kypseli",
    "Filadelfeia": "Kypseli",
    "Galatsi": "Kypseli",              # גלאצי נסחרת בערכים דומים לצפון קיפסלי ב-2026
    "Agia Eleousa": "Kypseli",

    # ------------------------------------------------------------
    # CLUSTER 2: Central Transit Hub (Agios Panteleimonas - Deep Value)
    # האזורים הזולים ביותר במרכז עם תשואה פוטנציאלית גבוהה
    # ------------------------------------------------------------
    "Agios Panteleimonas": "Agios Panteleimonas",
    "Attica Square": "Agios Panteleimonas",
    "Attiki": "Agios Panteleimonas",
    "Viktorias Square": "Agios Panteleimonas",
    "Vathis Square": "Agios Panteleimonas",
    "Larissis station": "Agios Panteleimonas",
    "Stathmos Larissis": "Agios Panteleimonas",
    "Ipirou": "Agios Panteleimonas",
    "Omonia": "Agios Panteleimonas",

    # ------------------------------------------------------------
    # CLUSTER 3: University Belt (Zografou/Ampelokipoi - Safe LTR)
    # שוק הסטודנטים והסגל הרפואי - ביקוש קשיח ויציב מאוד
    # ------------------------------------------------------------
    "Zografou": "Zografou",
    "Ilisia": "Zografou",
    "Kaisariani": "Zografou",
    "Ymittos": "Zografou",
    "Girokomeio": "Zografou",
    "Agios Loukas": "Zografou",
    "Agios Thomas": "Zografou",
    "Erythros": "Zografou",
    "Kountouriotika": "Zografou",
    "Nosokomeio Paidon": "Zografou",
    "Ampelokipoi": "Zografou",
    "Panormou": "Zografou",
    "Gyzi": "Zografou",
    "Gkyzi": "Zografou",
    "Polygono": "Zografou",
    "Ellinoroson": "Zografou",

    # ------------------------------------------------------------
    # CLUSTER 4: Southern Premium (Neos Kosmos/Kallithea - Appreciation)
    # שכונות בביקוש גבוה בשל קרבה למרכז ולקוקאקי היקרה
    # ------------------------------------------------------------
    "Neos Kosmos": "Neos Kosmos",
    "Agios Sostis": "Neos Kosmos",
    "Kallirrois": "Neos Kosmos",
    "Dourgouti": "Neos Kosmos",
    "Kallithea": "Neos Kosmos",
    "Tavros": "Neos Kosmos",
    "Koukaki": "Neos Kosmos",
    "Petralona": "Neos Kosmos",

    # ------------------------------------------------------------
    # CLUSTER 5: Blue Chip (Pagkrati - High Liquidity)
    # נזילות מקסימלית - דירות שנמכרות במהירות שיא
    # ------------------------------------------------------------
    "Pagkrati": "Pagkrati",
    "Vyronas": "Pagkrati",
    "Gouva": "Pagkrati",
    "Agios Artemios": "Pagkrati",
    "Mets": "Pagkrati",
    "Profitis Ilias": "Pagkrati",

    # ------------------------------------------------------------
    # CLUSTER 6: Growth West (Kolonos/Peristeri - Value)
    # פוטנציאל עליית ערך גבוה בשל מחירי כניסה נמוכים
    # ------------------------------------------------------------
    "Kolonos": "Kolonos",
    "Skouze Hill": "Kolonos",
    "Akadimia Platonos": "Kolonos",
    "Sepolia": "Kolonos",
    "Egaleo": "Kolonos",
    "Kolokinthou": "Kolonos",
    "Peristeri": "Kolonos",
    "Chalkidona": "Kolonos",
    "Ilion": "Kolonos",
    "Agioi Anargyroi": "Kolonos",

    # ------------------------------------------------------------
    # CLUSTER 7: Gentrification (Exarcheia - Hip & Central)
    # אזור הנוודים הדיגיטליים והצעירים - עליית מחירי שכירות מואצת
    # ------------------------------------------------------------
    "Exarcheia": "Exarcheia",
    "Mouseio": "Exarcheia",
    "Neapoli": "Exarcheia",
    "Strefi Hill": "Exarcheia",
    "Ippokratous": "Exarcheia",
    "Metaxourgeio": "Exarcheia",
    "Keramikos": "Exarcheia",

    # ------------------------------------------------------------
    # CLUSTER 8: Elite (Kolonaki - Low Yield, High Prestige)
    # אזורי יוקרה - תשואה נמוכה אך נכסי "Safe Haven"
    # ------------------------------------------------------------
    "Kolonaki": "Kolonaki",
    "Lycabettus": "Kolonaki",
    "Hilton": "Kolonaki",

    # --- Standalone / Outer ---
    "Agia Paraskevi": "Agia Paraskevi",
    "Ionia": "Ionia",
    "Smyrni": "Smyrni",

    # --- Cleanup ---
    "130": None,
    "Center": None,
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
