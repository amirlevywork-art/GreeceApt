"""Monthly rent-model refresh: re-scrape Spitogatos rentals, then retrain.

The net-yield ranking stands on the rent model, so it should follow the market. The full
pipeline calls ``refresh_if_due`` before scoring: when the saved model is older than
``REFRESH_AFTER_DAYS`` (or missing) it re-runs the rental sweep (new ads get a detail fetch,
re-seen ones only a ``last_seen_at``/price update) and retrains on rentals seen in the last
``TRAIN_WINDOW_DAYS``. New rentals have no Qwen score until ``python -m greeceapt.rent vision``
runs; the model treats them as "visual missing". A failure keeps the previous model.

    python -m greeceapt.rent refresh [--force]
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

from greeceapt.rent import config as cfg

logger = logging.getLogger(__name__)


def is_due(model_path: Path | None = None, *, max_age_days: float | None = None) -> bool:
    from greeceapt.rent.train import model_age_days

    age = model_age_days(model_path)
    limit = cfg.REFRESH_AFTER_DAYS if max_age_days is None else max_age_days
    return age is None or age >= limit


def _scrape_rentals() -> None:
    from greeceapt.rent.scrape import run

    asyncio.run(run())


def _train_model() -> None:
    from greeceapt.rent.train import run

    run()


def refresh(*, scrape: Callable[[], object] = _scrape_rentals, train: Callable[[], object] = _train_model) -> bool:
    """Scrape then train; True when a new model was saved. Errors are logged, never raised."""
    try:
        scrape()
    except Exception as exc:  # a blocked or failed sweep must not stop the pipeline
        logger.warning("Rent refresh: rental sweep failed (%s); keeping the previous model.", exc)
        return False
    try:
        train()
    except Exception as exc:
        logger.warning("Rent refresh: training failed (%s); keeping the previous model.", exc)
        return False
    return True


def refresh_if_due(*, force: bool = False, **kwargs) -> bool:
    if not force and not is_due():
        logger.info("Rent model is younger than %s days; no refresh.", cfg.REFRESH_AFTER_DAYS)
        return False
    logger.info("Rent model refresh: re-scraping rentals and retraining.")
    return refresh(**kwargs)
