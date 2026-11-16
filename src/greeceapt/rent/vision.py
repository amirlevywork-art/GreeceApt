"""
Qwen V2 condition scores for the rental vision sample.

Runs ``ai_conductor._run_layer1_v2_impl`` — the exact production scoring path, same model tag,
prompt, schema and image cache as the sale listings — against rentals.db via a
small adapter, restricted to rows with ``in_vision_sample = 1``. Resumable: accepted rows for the
current contract are skipped on the next run.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from types import SimpleNamespace

from greeceapt.rent import config as cfg
from greeceapt.rent import store

logger = logging.getLogger(__name__)


def _sample_ids(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT id FROM listings WHERE in_vision_sample = 1")}


def rentals_adapter(db_path=None) -> SimpleNamespace:
    from greeceapt.storage import db_conductor

    path = db_path or cfg.RENTALS_DB

    def connect_updated() -> sqlite3.Connection:
        return sqlite3.connect(path)

    def get_listings_for_layer1_v2(conn, **kwargs):
        sample = _sample_ids(conn)
        items = db_conductor.get_listings_for_layer1_v2(conn, **kwargs)
        chosen = [(listing_id, urls) for listing_id, urls in items if str(listing_id) in sample]
        # seeded random order: if the run is stopped early, the scored part still mirrors the sample
        return sorted(chosen, key=lambda item: hashlib.sha1(f"{cfg.SAMPLE_SEED}:{item[0]}".encode()).hexdigest())

    return SimpleNamespace(
        connect_updated=connect_updated,
        promote_current_v2_results=db_conductor.promote_current_v2_results,
        get_listings_for_layer1_v2=get_listings_for_layer1_v2,
        save_visual_audit_v2_results=db_conductor.save_visual_audit_v2_results,
    )


def run(*, service=None) -> None:
    from greeceapt.photos import ai_conductor
    from greeceapt.logging_config import DEFAULT_FORMAT

    store.connect().close()  # ensure schema
    handler = logging.FileHandler(cfg.RENTALS_DB.with_name("rent_vision.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    logging.getLogger().addHandler(handler)
    try:
        ai_conductor._run_layer1_v2_impl(rentals_adapter(), service=service)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
