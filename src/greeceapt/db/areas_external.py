"""
Utility functions for the areas_external table in listings.db.
This table holds neighborhood scores and is used for ad-hoc/manual work.
It is NOT part of the main pipeline (create_updated_db manages its own copy).
"""

from greeceapt.db.core import get_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS areas_external (
    neighborhood TEXT PRIMARY KEY,
    score REAL
);
"""


def reset_areas_external_table(drop: bool = True) -> None:
    """Recreate areas_external. drop=True deletes existing data."""
    conn = get_connection()
    if drop:
        conn.executescript("DROP TABLE IF EXISTS areas_external;")
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    print("[INFO] areas_external table ready.")


def populate_neighborhoods_from_listings() -> None:
    """Insert DISTINCT neighborhoods from listings into areas_external."""
    conn = get_connection()
    cur = conn.cursor()
    conn.executescript(_SCHEMA)
    cur.execute("""
        SELECT DISTINCT TRIM(neighborhood)
        FROM listings
        WHERE neighborhood IS NOT NULL AND TRIM(neighborhood) != '';
    """)
    neighborhoods = [r[0] for r in cur.fetchall() if r[0]]
    for n in neighborhoods:
        conn.execute(
            "INSERT INTO areas_external (neighborhood, score) VALUES (?, NULL)"
            " ON CONFLICT(neighborhood) DO NOTHING;",
            (n,),
        )
    conn.commit()
    conn.close()
    print(f"[INFO] Populated {len(neighborhoods)} neighborhoods into areas_external.")


def set_neighborhood_score(neighborhood: str, score: float | None) -> None:
    conn = get_connection()
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO areas_external (neighborhood, score) VALUES (?, ?)"
        " ON CONFLICT(neighborhood) DO UPDATE SET score = excluded.score;",
        (neighborhood, score),
    )
    conn.commit()
    conn.close()
    print(f"[INFO] Score set: {neighborhood} -> {score}")


def bulk_set_scores(scores: dict[str, float]) -> None:
    conn = get_connection()
    conn.executescript(_SCHEMA)
    for neighborhood, score in scores.items():
        conn.execute(
            "INSERT INTO areas_external (neighborhood, score) VALUES (?, ?)"
            " ON CONFLICT(neighborhood) DO UPDATE SET score = excluded.score;",
            (neighborhood, score),
        )
    conn.commit()
    conn.close()
    print(f"[INFO] Bulk scores updated for {len(scores)} neighborhoods.")


def get_all_neighborhood_scores() -> list[tuple[str, float | None]]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT neighborhood, score FROM areas_external ORDER BY neighborhood;")
    rows = cur.fetchall()
    conn.close()
    return rows
