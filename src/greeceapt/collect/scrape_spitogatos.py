"""CLI entry for the Spitogatos scraper (implementation in ``greeceapt.collect.spitogatos``)."""

from __future__ import annotations

import asyncio
import sys

from greeceapt.collect.spitogatos import config
from greeceapt.collect.spitogatos.scrape import BlockedError, run

SEARCH_API = config.SEARCH_API
LISTINGS_JSON = config.LISTINGS_JSON

__all__ = ["LISTINGS_JSON", "SEARCH_API", "run"]


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    try:
        asyncio.run(run())
    except BlockedError as exc:
        # run_all reads this exit code and stops the whole run instead of merging.
        print(f"Spitogatos collector stopped: {exc}", file=sys.stderr)
        sys.exit(config.EXIT_BLOCKED)
