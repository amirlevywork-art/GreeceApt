"""
Layer 2 — Condition Gate.

Removes accepted listings whose condition score is below ``MIN_CONDITION_SCORE``.

Thin sale neighborhoods are **not** removed any more: the ranking is by rental yield from a
static rent benchmark, so a listing in a neighborhood with few sale samples stays eligible.
A thin sale sample only suppresses the secondary market-discount field
(``market_analytics.DEFAULT_MIN_SAMPLE_SIZE``). ``MIN_NEIGHBORHOOD_SIZE`` is kept for
reference by reports only.
"""

from __future__ import annotations

import logging

from greeceapt.storage import db_conductor

MIN_CONDITION_SCORE   = 3.0
MIN_NEIGHBORHOOD_SIZE = 10
LAYER_NAME            = "Layer 2"

logger = logging.getLogger(__name__)


def run() -> None:
    conn = db_conductor.connect_updated()
    try:
        removed_condition = db_conductor.remove_low_condition_listings(
            conn,
            MIN_CONDITION_SCORE,
            score_column="visual_score_v2",
            status_column="visual_status_v2",
            layer_name=LAYER_NAME,
        )
    finally:
        conn.close()

    logger.info(
        "Layer 2 done. condition_purge=%s (score < %.1f). Thin sale neighborhoods are kept "
        "(they only lose the market-discount field).",
        removed_condition, MIN_CONDITION_SCORE,
    )


if __name__ == "__main__":
    run()
