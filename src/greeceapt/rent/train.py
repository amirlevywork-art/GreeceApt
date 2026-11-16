"""Fit the rent model on rentals.db → data/rent_model.json + data/rent_model_report.md."""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from greeceapt.rent import config as cfg
from greeceapt.rent import store
from greeceapt.rent.features import LABELS
from greeceapt.rent.model import LOCATION, RentModel, select_and_fit
from greeceapt.rent.sample import collapse_duplicates

logger = logging.getLogger(__name__)


def _seen(row: Mapping[str, Any]) -> datetime | None:
    stamp = row.get("last_seen_at") or row.get("scraped_at")
    try:
        seen = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)


def training_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], Counter]:
    """Usable, duplicate-collapsed rentals plus a count of why the others were dropped.

    Only rentals seen within ``TRAIN_WINDOW_DAYS`` of the newest sweep count, so the model
    follows the current market as rentals.db accumulates monthly refreshes.
    """
    dropped: Counter = Counter()
    keep = []
    stamps = [s for s in (_seen(r) for r in rows) if s is not None]
    cutoff = max(stamps) - timedelta(days=cfg.TRAIN_WINDOW_DAYS) if stamps else None
    for row in rows:
        rent, area = row.get("rent_eur"), row.get("area_sqm")
        seen = _seen(row)
        if cutoff is not None and seen is not None and seen < cutoff:
            dropped["not_seen_recently"] += 1
        elif not rent or not area or area <= 0:
            dropped["missing_rent_or_area"] += 1
        elif not cfg.TRAIN_RENT_PSQM[0] <= rent / area <= cfg.TRAIN_RENT_PSQM[1]:
            dropped["implausible_eur_per_sqm"] += 1
        elif row.get("latitude") is None or row.get("longitude") is None:
            dropped["no_coordinates"] += 1
        elif row.get("floor_class") is None or row.get("floor_conflict"):
            dropped["floor_unknown_or_conflict"] += 1
        else:
            keep.append(dict(row))
    collapsed = collapse_duplicates(keep)
    dropped["agency_duplicate"] = len(keep) - len(collapsed)
    return collapsed, dropped


def _vision_contract(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    accepted = [r for r in rows if r.get("visual_status_v2") == "accepted"]
    versions = Counter(
        (r.get("vision_model_version"), r.get("vision_prompt_version"), r.get("vision_schema_version"))
        for r in accepted
    )
    top = versions.most_common(1)[0][0] if versions else (None, None, None)
    return {"n_visual": len(accepted), "model": top[0], "prompt": top[1], "schema": top[2]}


def run(db_path: Path | None = None, out_path: Path | None = None, report_path: Path | None = None,
        history_path: Path | None = None) -> RentModel:
    conn = store.connect(db_path)
    try:
        raw = store.all_rows(conn)
    finally:
        conn.close()
    rows, dropped = training_rows(raw)
    logger.info("Rent model: %s training rentals (%s raw; dropped %s).", len(rows), len(raw), dict(dropped))
    model = select_and_fit(rows)
    model.meta = {
        **model.meta,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_raw": len(raw),
        "n_train": len(rows),
        "dropped": dict(dropped),
        "vision": _vision_contract(rows),
        "source": "spitogatos rentals, Athens-Center, last 6 months",
    }
    out = Path(out_path or cfg.RENT_MODEL_JSON)
    out.write_text(json.dumps(model.to_json()), encoding="utf-8")
    report = Path(report_path or cfg.RENT_MODEL_REPORT)
    report.write_text(render_report(model), encoding="utf-8")
    append_history(model, rows, Path(history_path or cfg.RENT_MODEL_HISTORY))
    cv = model.metrics["random_cv"]
    logger.info("Rent model saved → %s (CV MAPE %.1f%%, R² %.2f, alpha=%s, k=%s); report → %s",
                out, cv["mape"] * 100, cv["r2_log"], model.alpha, model.k, report)
    return model


def append_history(model: RentModel, rows: Sequence[Mapping[str, Any]], path: Path) -> None:
    """One JSON line per training run, so market and coefficient drift can be followed."""
    psqm = sorted(r["rent_eur"] / r["area_sqm"] for r in rows)
    entry = {
        "trained_at": model.meta.get("trained_at"),
        "n_train": len(rows),
        "median_rent_eur_per_sqm": psqm[len(psqm) // 2] if psqm else None,
        "random_cv_mape": model.metrics.get("random_cv", {}).get("mape"),
        "alpha": model.alpha,
        "k": model.k,
        "coefficients": {c["feature"]: {"coef": c["coef"], "ci90": c.get("ci90"), "pct_effect": c.get("pct_effect")}
                         for c in model.coefficients},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def model_age_days(path: Path | None = None, *, now: datetime | None = None) -> float | None:
    """Days since the saved model was trained (None: no model or no timestamp)."""
    p = Path(path or cfg.RENT_MODEL_JSON)
    if not p.exists():
        return None
    try:
        stamp = json.loads(p.read_text(encoding="utf-8")).get("meta", {}).get("trained_at")
        trained = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    trained = trained if trained.tzinfo else trained.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - trained).total_seconds() / 86400


def load(path: Path | None = None) -> RentModel | None:
    p = Path(path or cfg.RENT_MODEL_JSON)
    if not p.exists():
        return None
    return RentModel.from_json(json.loads(p.read_text(encoding="utf-8")))


# ── Report ────────────────────────────────────────────────────────────────────

def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def render_report(model: RentModel) -> str:
    m = model.metrics
    lines = [
        "# Rent model report",
        "",
        f"Trained {model.meta.get('trained_at')} on {model.meta.get('n_train')} rentals "
        f"({model.meta.get('n_raw')} scraped; dropped {model.meta.get('dropped')}). "
        f"Visual scores: {model.meta.get('vision', {}).get('n_visual')} rentals.",
        f"Selected ridge alpha={model.alpha}, k={model.k} nearest rentals.",
        *([f"Constraints: {'; '.join(model.meta['constraints'])}."] if model.meta.get("constraints") else []),
        "",
        "## Accuracy (out-of-fold)",
        "",
        "| model | MAPE | median APE | within ±10% | R² (log) | n |",
        "|---|---|---|---|---|---|",
    ]
    for key, label in (
        ("random_cv", "**this model** (random 5-fold)"),
        ("neighborhood_holdout_cv", "this model, whole neighborhoods held out"),
        ("no_location_term", "without the location term"),
        ("baseline_hood_median_psqm", "baseline: neighborhood median €/m² × area"),
    ):
        x = m[key]
        lines.append(f"| {label} | {x['mape']*100:.1f}% | {x['median_ape']*100:.1f}% | "
                     f"{x['within_10pct']*100:.0f}% | {x['r2_log']:.2f} | {x['n']} |")
    lines += [
        "",
        f"Conservative rent = central × {math.exp(model.lower_log_residual):.3f} "
        f"(10th percentile of out-of-fold errors); observed coverage "
        f"{m['random_cv_lower_bound_coverage']*100:.1f}%.",
        "",
        "Per group (used when the group has enough rentals; otherwise the overall bound):",
        "",
        "| group | rentals | conservative × | coverage | no rentals nearby × |",
        "|---|---|---|---|---|",
        *[
            f"| {g} | {c['n']} | "
            + (f"{math.exp(model.lower_by_group[g]):.3f} | {c['coverage']*100:.1f}% | "
               if g in model.lower_by_group else "overall | – | ")
            + (f"{math.exp(model.sparse_lower_by_group[g]):.3f} |" if g in model.sparse_lower_by_group else "– |")
            for g, c in m.get("group_lower_bound_coverage", {}).items()
        ],
        "",
        f"No rentals nearby: the 10th-nearest rental farther than {model.sparse_km or 0:.2f} km "
        "(99th percentile of the rentals' own); such a flat takes the lower of its group's bound "
        "and the bound with whole neighborhoods held out.",
        "",
        "## Learned effects on monthly rent",
        "",
        "| feature | effect | 90% CI | share of rentals |",
        "|---|---|---|---|",
    ]
    for c in model.coefficients:
        name = c["feature"]
        lo, hi = c["ci90"]
        if name == "log_area":
            effect, ci = f"elasticity {c['coef']:.2f}", f"{lo:.2f} – {hi:.2f}"
        elif name == LOCATION:
            effect, ci = f"×{c['coef']:.2f} of local level", f"{lo:.2f} – {hi:.2f}"
        else:
            effect, ci = _pct(math.expm1(c["coef"])), f"{_pct(math.expm1(lo))} – {_pct(math.expm1(hi))}"
        label = LABELS.get(name, name)
        lines.append(f"| {label} | {effect} | {ci} | {c['share_nonzero']*100:.0f}% |")

    lines += [
        "",
        "Grid (rmse_log, alpha, k), best first: "
        + ", ".join(f"({g['rmse_log']:.4f}, {g['alpha']}, {g['k']})" for g in m["grid"][:6]),
        "",
    ]
    return "\n".join(lines)
