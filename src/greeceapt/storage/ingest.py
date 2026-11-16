"""Stage 2: ``data/listings.json`` → ``data/ingested_listings.db``."""

import json
import logging
import sys
from pathlib import Path
from typing import Any

from greeceapt.storage import insert_listings, paths

logger = logging.getLogger(__name__)


def load_listings_json_list(path: Path) -> list[dict[str, Any]]:
    """Read ``path`` as UTF-8 JSON; ``FileNotFoundError`` / ``ValueError`` on invalid input."""
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("JSON must contain a list")
    return data


def load_json(path: str | None = None) -> list[dict[str, Any]]:
    if path is None:
        default_path = paths.LISTINGS_JSON
        logger.info("Using default JSON: %s", default_path)
        p = default_path
    else:
        p = Path(path)

    data = load_listings_json_list(p)
    logger.info("Loaded %s listings from %s", len(data), p)
    return data


def main() -> None:
    json_path = sys.argv[1] if len(sys.argv) > 1 else None
    listings = load_json(json_path)
    insert_listings(listings)
    logger.info("Ingestion complete. Database updated.")


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    main()
