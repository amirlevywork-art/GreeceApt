"""
The GUI's buttons: a closed list of commands, never arbitrary input.

Each action names the resources it holds while running; a job waits in the queue until none of
its resources is held by a running job. ``network`` actions ask for confirmation first and say
what will be requested; the collectors keep their own stop-at-once handling of refusals.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from greeceapt.storage import paths
from greeceapt.gui.progress import (
    FINGERPRINT_START, FINGERPRINT_TICK, LAYER1_TICK, LAYER1_TOTAL, PHASE_A_TICK, PHASE_B_START,
    PHASE_B_TICK, RENTALS_DETAIL_START, RENTALS_TICK, Stage, rx,
)

ALL = frozenset({"network", "update", "final_db", "rent_model", "rentals", "ollama"})


def last_sweep_total() -> int | None:
    """Result count of the last Spitogatos sweep: the expected total of the next one."""
    for path in (paths.DATA_DIR / "spitogatos_sweep.json", paths.DATA_DIR / "spitogatos_last_run.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("total_results")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, int) and value > 0:
            return value
    return None


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    group: str
    description: str
    build: Callable[[Mapping[str, Any]], list[list[str]]]   # params → argv lists, run in order
    stages: Sequence[Stage]
    resources: frozenset[str]
    network: str | None = None        # what is requested on the network (confirmation text)
    params: Sequence[dict[str, Any]] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "group": self.group,
                "description": self.description, "network": self.network,
                "params": list(self.params), "resources": sorted(self.resources)}


def _py(*args: str) -> list[str]:
    return [sys.executable, "-m", *args]


def _vision_commands(params: Mapping[str, Any]) -> list[list[str]]:
    size = params.get("size")
    commands = []
    if size:
        commands.append(_py("greeceapt.rent", "sample", "--rebalance", "--size", str(int(size))))
    commands.append(_py("greeceapt.rent", "vision"))
    return commands


def current_sample_size() -> int | None:
    import sqlite3
    from contextlib import closing

    from greeceapt.rent import config as rent_cfg

    if not rent_cfg.RENTALS_DB.exists():
        return None
    with closing(sqlite3.connect(f"file:{rent_cfg.RENTALS_DB}?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT COUNT(*) FROM listings WHERE in_vision_sample = 1").fetchone()[0]


SWEEP = Stage("sweep", "Reading Spitogatos search pages", rx(r"--- Stage 1"), expected_s=150,
              tick=PHASE_A_TICK, per_item_s=0.1, default_total=last_sweep_total)

ACTIONS: dict[str, Action] = {a.key: a for a in (
    Action(
        "update_check", "Check for changes", "Update",
        "Reads the search result pages only and says what an update would change. Writes nothing.",
        lambda p: [_py("greeceapt.update", "check")],
        [Stage("sweep", "Reading Spitogatos search pages", expected_s=150, tick=PHASE_A_TICK,
               per_item_s=0.1, default_total=last_sweep_total)],
        frozenset({"network", "update"}),
        network="Spitogatos search result pages (about 50 requests, 30 results each). "
                "Stops at once if Spitogatos refuses.",
    ),
    Action(
        "update", "Full update", "Update",
        "Backup, collect what changed on Spitogatos, score new photos with the local image model, "
        "re-rank and write the change report. Hours when many flats are new.",
        lambda p: [_py("greeceapt.update")],
        [
            Stage("backup", "Backing up", expected_s=20),
            SWEEP,
            Stage("details", "Fetching ad details", PHASE_B_START, total=PHASE_B_START,
                  tick=PHASE_B_TICK, per_item_s=1.0, expected_s=60),
            Stage("fingerprints", "Photo fingerprints", FINGERPRINT_START, total=FINGERPRINT_START,
                  tick=FINGERPRINT_TICK, per_item_s=0.05, expected_s=60),
            Stage("merge", "Merging and removing duplicates", rx(r"Fingerprint cache persisted"), expected_s=30),
            Stage("ingest", "Loading into the database", rx(r"--- Stage 2"), expected_s=15),
            Stage("layer0", "Filtering (Layer 0)", rx(r"--- Stage 3: AI"), expected_s=30),
            Stage("layer1", "Image model (Layer 1)", LAYER1_TOTAL, total=LAYER1_TOTAL, tick=LAYER1_TICK,
                  per_item_s=100.0, expected_s=1800),
            Stage("rent", "Rent model refresh (monthly)", rx(r"--- Stage 3b"), expected_s=10),
            Stage("score", "Ranking", rx(r"--- Stage 4"), expected_s=60),
        ],
        ALL,
        network="Spitogatos search pages and the detail of new or changed ads, plus their photos. "
                "Stops at once if Spitogatos refuses; the ranking is then left as it was.",
    ),
    Action(
        "score_only", "Re-score", "Update",
        "Re-rank from the data already collected (after a rent model change or a duplicate decision).",
        lambda p: [_py("greeceapt.main", "--score-only")],
        [Stage("score", "Ranking", expected_s=45)],
        frozenset({"final_db"}),
    ),
    Action(
        "rent_train", "Retrain rent model", "Rent model",
        "Fit the rent model on the stored rentals (with their image scores) and write its report.",
        lambda p: [_py("greeceapt.rent", "train")],
        [Stage("train", "Training", expected_s=75)],
        frozenset({"rent_model"}),
    ),
    Action(
        "rent_pairs", "Validate on sale/rent pairs", "Rent model",
        "Find flats advertised for sale and for rent at once and compare the model's rent with the asked rent.",
        lambda p: [_py("greeceapt.rent", "pairs")],
        [Stage("pairs", "Matching photos and comparing", expected_s=40)],
        frozenset({"network"}),
        network="Photos of the candidate ads from the Spitogatos image server (most are cached).",
    ),
    Action(
        "rent_vision", "Score rentals with the image model", "Rent model",
        "Score the rentals in the image sample with the local model (about 2.5 min each). "
        "Can be stopped and resumed; enlarge the sample to score more.",
        _vision_commands,
        [Stage("sample", "Drawing the sample", expected_s=5),
         Stage("vision", "Image model on rentals", LAYER1_TOTAL, total=LAYER1_TOTAL, tick=LAYER1_TICK,
               per_item_s=150.0, expected_s=600)],
        frozenset({"ollama", "rentals"}),
        network="Photos of the sampled rentals from the Spitogatos image server.",
        params=({"name": "size", "label": "Sample size (blank: keep)", "type": "int",
                 "default": current_sample_size},),
    ),
    Action(
        "rent_refresh", "Refresh rentals", "Rent model",
        "Re-collect the Athens-Center rentals and retrain the rent model (normally monthly, inside the full update).",
        lambda p: [_py("greeceapt.rent", "refresh", "--force")],
        [Stage("search", "Rental search pages", expected_s=120),
         Stage("details", "Rental details", RENTALS_DETAIL_START, total=RENTALS_DETAIL_START,
               tick=RENTALS_TICK, per_item_s=0.5, expected_s=300),
         Stage("train", "Training", rx(r"Rent model: \d+ training rentals"), expected_s=75)],
        frozenset({"network", "rentals", "rent_model"}),
        network="Spitogatos rental search pages (about 6,000 results) and the detail of new rentals. "
                "Stops at once if Spitogatos refuses.",
    ),
    Action(
        "delisting", "Delisting report", "Reports",
        "Which ranked flats left the site since an earlier ranking (likely sold).",
        lambda p: [_py("greeceapt.scoring.rank_history")],
        [Stage("report", "Comparing snapshots", expected_s=5)],
        frozenset(),
    ),
)}

def resolve_params(action: Action) -> list[dict[str, Any]]:
    """Parameter descriptions with their current defaults filled in."""
    out = []
    for param in action.params:
        default = param.get("default")
        out.append({**param, "default": default() if callable(default) else default})
    return out


def validate_params(action: Action, params: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for spec in action.params:
        value = params.get(spec["name"])
        if value in (None, ""):
            continue
        if spec.get("type") == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{spec['label']}: a whole number is needed") from None
            if number <= 0 or number > 20_000:
                raise ValueError(f"{spec['label']}: between 1 and 20,000")
            clean[spec["name"]] = number
    return clean
