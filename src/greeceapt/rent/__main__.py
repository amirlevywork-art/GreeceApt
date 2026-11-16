"""CLI: python -m greeceapt.rent {scrape,sample,vision,train,refresh,pairs}."""

from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    from greeceapt.logging_config import configure_root_logging

    parser = argparse.ArgumentParser(prog="python -m greeceapt.rent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scrape", help="Spitogatos rentals → data/rentals.db")
    sample = sub.add_parser("sample", help="mark the stratified vision sample")
    sample.add_argument("--size", type=int, default=None)
    sample.add_argument("--rebalance", action="store_true",
                        help="keep scored rows, redraw the rest to the ranked sale flats' size mix")
    sub.add_parser("vision", help="Qwen V2 condition score on the sample (resumable)")
    sub.add_parser("train", help="fit + spatial CV → data/rent_model.json and report")
    refresh = sub.add_parser("refresh", help="scrape + train if the model is older than RENT_REFRESH_DAYS")
    refresh.add_argument("--force", action="store_true", help="refresh even if the model is recent")
    pairs = sub.add_parser("pairs", help="flats listed for sale and for rent → validation")
    pairs.add_argument("--sale-db", default=None, help="updated_listings.db to match (default: data/)")
    args = parser.parse_args(argv)
    configure_root_logging()

    if args.command == "scrape":
        from greeceapt.rent.scrape import run

        asyncio.run(run())
    elif args.command == "sample":
        from greeceapt.rent import config as cfg
        from greeceapt.rent.sample import band_shares, mark_rebalanced, mark_sample

        if args.rebalance:
            import sqlite3

            from greeceapt.storage.paths import FINAL_DB

            shares = None
            if FINAL_DB.exists():
                conn = sqlite3.connect(f"file:{FINAL_DB}?mode=ro", uri=True)
                try:
                    areas = [a for (a,) in conn.execute(
                        "SELECT area_sqm FROM deals WHERE exclusion_reason IS NULL AND area_sqm IS NOT NULL")]
                finally:
                    conn.close()
                shares = band_shares(areas) if areas else None
            mark_rebalanced(size=args.size or cfg.SAMPLE_SIZE, shares=shares)
        else:
            mark_sample(size=args.size or cfg.SAMPLE_SIZE)
    elif args.command == "vision":
        from greeceapt.rent.vision import run

        run()
    elif args.command == "train":
        from greeceapt.rent.train import run

        run()
    elif args.command == "refresh":
        from greeceapt.rent.refresh import refresh_if_due

        refresh_if_due(force=args.force)
    elif args.command == "pairs":
        from pathlib import Path

        from greeceapt.storage.paths import DATA_DIR, UPDATED_DB
        from greeceapt.rent.pairs import run

        run(Path(args.sale_db) if args.sale_db else UPDATED_DB, DATA_DIR / "rent_sale_pairs.json")


if __name__ == "__main__":
    main()
