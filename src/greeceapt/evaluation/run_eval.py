"""
Bake-off runner — score the golden set with a candidate, grade it against your labels.

    python -m greeceapt.evaluation.run_eval --model qwen3-vl:4b-instruct --strategy V2
    python -m greeceapt.evaluation.run_eval --all --split tune
    python -m greeceapt.evaluation.run_eval --model qwen3-vl:4b-instruct --strategy V2 --split holdout --promote

``--split holdout`` is refused without ``--promote`` so prompt iteration cannot quietly
overfit to the held-out images. Every leaderboard row records ``n_comparisons`` because the
label set grows over time and rows are only comparable when fit on the same labels.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from statistics import median, pstdev

from greeceapt.storage import paths
from greeceapt.evaluation.bradley_terry import Comparison, fit_bradley_terry
from greeceapt.evaluation.golden_set import IMAGES_DIR, load_golden, validate_golden
from greeceapt.evaluation import metrics
from greeceapt.evaluation.label import (
    comparisons_for_ids,
    label_status,
    load_comparisons,
)
from greeceapt.evaluation.scorers import ScorerConfig, score_image_detailed

EVAL_DIR = paths.DATA_DIR / "vision_eval"
PRED_DIR = EVAL_DIR / "predictions"
LEADERBOARD = EVAL_DIR / "leaderboard.md"

DEFAULT_STRATEGIES = ["V2", "P0", "P1", "P2", "P3", "P4"]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")


def prediction_path(
    cfg: ScorerConfig,
    split: str,
    *,
    directory: Path = PRED_DIR,
    limit: int | None = None,
    repeat: int = 1,
) -> Path:
    contract = "__".join(
        [
            _slug(cfg.model),
            _slug(cfg.prompt_version),
            _slug(cfg.schema_version),
            str(cfg.resolution),
            _slug(split),
        ]
    )
    if limit is not None:
        contract += f"__limit-{limit}"
    if repeat > 1:
        contract += f"__repeat-{repeat}"
    return directory / f"{contract}.json"


def holdout_lock_path(
    cfg: ScorerConfig,
    *,
    directory: Path = PRED_DIR,
) -> Path:
    candidate = prediction_path(cfg, "holdout", directory=directory)
    return candidate.with_suffix(".lock")


def guard_holdout_run(*, split: str, limit: int | None, lock_path: Path) -> None:
    """Atomically claim a repeat-independent candidate holdout identity."""
    if split != "holdout":
        return
    if limit is not None:
        raise SystemExit("--limit is forbidden on the locked holdout")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_prefix = lock_path.name.removesuffix(".lock")
    if any(lock_path.parent.glob(f"{artifact_prefix}*.json")):
        raise SystemExit(
            f"holdout candidate was already evaluated under {artifact_prefix}; "
            "version the model, prompt, or schema before another run"
        )
    try:
        with lock_path.open("x", encoding="utf-8") as lock:
            lock.write("claimed\n")
    except FileExistsError:
        raise SystemExit(
            f"holdout candidate was already evaluated: {lock_path}; version the "
            "model, prompt, or schema before another release-candidate run"
        )


def _score_split(
    cfg: ScorerConfig,
    split: str,
    *,
    limit: int | None = None,
    repeat: int = 1,
) -> tuple[dict[str, float], dict[str, dict]]:
    """Return numeric scores and full serializable image predictions."""
    golden = [r for r in load_golden() if r["split"] == split]
    if limit is not None:
        golden = golden[:limit]
    scores: dict[str, float] = {}
    predictions: dict[str, dict] = {}
    for r in golden:
        image_path = IMAGES_DIR / f"{r['sha256']}.jpg"
        runs = [score_image_detailed(str(image_path), cfg) for _ in range(repeat)]
        run_scores = [run["score"] for run in runs if run["score"] is not None]
        prediction = dict(runs[0])
        if run_scores:
            prediction["score"] = median(run_scores)
        prediction["latency_ms"] = median(run["latency_ms"] for run in runs)
        prediction["attempts"] = max(run["attempts"] for run in runs)
        prediction["repeat_score_stdev"] = (
            pstdev(run_scores) if len(run_scores) > 1 else 0.0
        )
        prediction["runs"] = runs
        prediction.update(
            {
                "image_id": r["id"],
                "image_path": str(image_path),
                "listing_id": str(r["listing_id"]),
                "split": split,
            }
        )
        predictions[r["id"]] = prediction
        if prediction["score"] is not None:
            scores[r["id"]] = prediction["score"]
    return scores, predictions


def _critical_slice_metrics(
    predictions: dict[str, dict],
    golden: list[dict],
    comparisons: list[Comparison],
    scores: dict[str, float],
) -> dict[str, dict[str, float | int]]:
    """Report preregistered slices; only n>=20 can enforce promotion."""
    members: dict[str, set[str]] = {
        name: set()
        for name in (
            "kitchen",
            "bathroom",
            "living",
            "bedroom",
            "good_quality",
            "degraded_quality",
            "xe",
            "spitogatos",
        )
    }
    golden_by_id = {str(row["id"]): row for row in golden}
    for image_id, prediction in predictions.items():
        golden_row = golden_by_id.get(image_id, {})
        room = str(golden_row.get("room_type", ""))
        if room in {"kitchen", "bathroom", "living", "bedroom"}:
            members[room].add(image_id)
        quality = str(golden_row.get("image_quality", ""))
        if quality == "good":
            members["good_quality"].add(image_id)
        elif quality:
            members["degraded_quality"].add(image_id)
        source = " ".join(
            str(golden_by_id.get(image_id, {}).get(key, ""))
            for key in ("url", "photo_url")
        ).lower()
        if "xe.gr" in source:
            members["xe"].add(image_id)
        if "spitogatos" in source:
            members["spitogatos"].add(image_id)

    result: dict[str, dict[str, float | int]] = {}
    for name, ids in members.items():
        slice_comparisons = [
            comparison
            for comparison in comparisons
            if comparison.a_id in ids and comparison.b_id in ids
        ]
        agreement, _, _ = metrics.pairwise_agreement(
            scores, slice_comparisons, bootstrap=0
        )
        result[name] = {
            "n_comparisons": len(slice_comparisons),
            "agreement": round(agreement, 3),
        }
    return result


def evaluate(
    cfg: ScorerConfig,
    split: str,
    *,
    limit: int | None = None,
    repeat: int = 1,
) -> dict:
    comparisons = [c for c in load_comparisons()]
    selected_golden = [r for r in load_golden() if r["split"] == split]
    if limit is not None:
        selected_golden = selected_golden[:limit]
    interior_ids = {
        r["id"] for r in selected_golden if r.get("interior", True)
    }
    split_comps = comparisons_for_ids(comparisons, interior_ids)

    scores, predictions = _score_split(cfg, split, limit=limit, repeat=repeat)
    golden = selected_golden
    cluster_by_id = {r["id"]: str(r["listing_id"]) for r in golden}

    agr, lo, hi = metrics.pairwise_agreement_clustered(
        scores,
        split_comps,
        cluster_by_id=cluster_by_id,
    )
    theta = fit_bradley_terry(split_comps) if split_comps else {}
    common = [i for i in scores if i in theta]
    sp = metrics.spearman([scores[i] for i in common], [theta[i] for i in common]) if len(common) >= 2 else 0.0
    coll = metrics.collapse_stats(list(scores.values()))
    consistency, _ = metrics.self_consistency(split_comps)
    individual_runs = [
        run
        for prediction in predictions.values()
        for run in prediction.get("runs", [prediction])
    ]
    operational = metrics.prediction_stats(individual_runs)
    repeat_stdevs = [
        float(prediction.get("repeat_score_stdev", 0.0))
        for prediction in predictions.values()
    ]
    operational["max_repeat_stdev"] = max(repeat_stdevs, default=0.0)
    critical_slices = _critical_slice_metrics(
        predictions, golden, split_comps, scores
    )
    interior_matches = [
        bool((predictions[row["id"]].get("evidence") or {}).get("is_interior"))
        == bool(row.get("interior", True))
        for row in golden
        if predictions[row["id"]].get("evidence") is not None
    ]

    PRED_DIR.mkdir(parents=True, exist_ok=True)
    comparable = [
        {"a_id": c.a_id, "b_id": c.b_id, "winner": c.winner}
        for c in split_comps
    ]
    artifact = prediction_path(cfg, split, limit=limit, repeat=repeat)
    artifact.write_text(
        json.dumps(
            {
                "metadata": {
                    "model": cfg.model,
                    "strategy": cfg.strategy,
                    "prompt_version": cfg.prompt_version,
                    "schema_version": cfg.schema_version,
                    "resolution": cfg.resolution,
                    "split": split,
                    "limit": limit,
                    "repeat": repeat,
                },
                "metrics": operational,
                "predictions": predictions,
                "comparisons": comparable,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if not scores:
        return {
            "name": cfg.name,
            "split": split,
            "error": "no images scored",
            "prediction_path": str(artifact),
            **operational,
        }

    return {
        "name": cfg.name, "split": split,
        "agreement": round(agr, 3), "agreement_lo": round(lo, 3), "agreement_hi": round(hi, 3),
        "spearman": round(sp, 3),
        "modal_share": round(coll["modal_share"], 3), "n_distinct": int(coll["n_distinct"]),
        "stdev": round(coll["stdev"], 2),
        "sec_per_img": round(
            sum(item["latency_ms"] for item in predictions.values())
            / max(1, len(predictions))
            / 1000,
            2,
        ),
        "latency_p50_ms": round(operational["latency_p50_ms"], 1),
        "latency_p95_ms": round(operational["latency_p95_ms"], 1),
        "latency_mean_ms": round(operational["latency_mean_ms"], 1),
        "valid_rate": round(operational["valid_rate"], 3),
        "null_rate": round(operational["null_rate"], 3),
        "retry_rate": round(operational["retry_rate"], 3),
        "max_repeat_stdev": round(operational["max_repeat_stdev"], 3),
        "n_comparisons": len(split_comps),
        "self_consistency": round(consistency, 3),
        "n_scored": len(scores),
        "n_images": len(predictions),
        "n_listings": len({str(row["listing_id"]) for row in golden}),
        "repeat": repeat,
        "interior_accuracy": round(
            sum(interior_matches) / len(interior_matches), 3
        ) if interior_matches else 0.0,
        "critical_slices": critical_slices,
        "prediction_path": str(artifact),
    }


def check_promotion_gates(row: dict, labels: dict) -> dict[str, bool]:
    """Evaluate only preregistered aggregate gates; sparse slices are diagnostic."""
    consistency = float(row.get("self_consistency", 0.0))
    return {
        "holdout_size": int(row.get("n_images", 0)) >= 48,
        "holdout_listings": int(row.get("n_listings", 0)) >= 48,
        "agreement_ci": float(row.get("agreement_lo", 0.0)) >= 0.8 * consistency,
        "score_spread": (
            float(row.get("modal_share", 1.0)) < 0.15
            and float(row.get("stdev", 0.0)) >= 1.2
        ),
        "latency_p95": float(row.get("latency_p95_ms", float("inf"))) <= 3000.0,
        "latency_mean": float(row.get("latency_mean_ms", float("inf"))) <= 2500.0,
        "valid_rate": float(row.get("valid_rate", 0.0)) >= 0.95,
        "retry_rate": float(row.get("retry_rate", 1.0)) < 0.05,
        "repeat_stability": (
            int(row.get("repeat", 0)) >= 3
            and float(row.get("max_repeat_stdev", float("inf"))) < 0.30
        ),
        "critical_slices": all(
            int(slice_row.get("n_comparisons", 0)) < 20
            or float(slice_row.get("agreement", 0.0))
            >= float(row.get("agreement", 0.0)) - 0.10
            for slice_row in row.get("critical_slices", {}).values()
        ),
        "human_connected": bool(labels.get("connected_complete")),
        "human_coverage": bool(labels.get("coverage_complete")),
        "human_repeats": bool(labels.get("repeats_complete"))
        and int(labels.get("consistency_pairs", 0)) >= 20,
    }


def _append_leaderboard(row: dict) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    if not LEADERBOARD.exists():
        LEADERBOARD.write_text(
            "# Vision bake-off leaderboard\n\n"
            "Gate (on locked holdout): agreement CI-low >= 0.80×self-consistency, "
            "modal_share < 0.15, stdev >= 1.2, p95 <= 3 s/img.\n\n"
            "| candidate | split | agreement [95% CI] | spearman | modal | n_dist | stdev | "
            "s/img | n_comp | self-cons |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
        )
    if "error" in row:
        line = f"| {row['name']} | — | ERROR: {row['error']} |\n"
    else:
        line = (f"| {row['name']} | {row['split']} | "
                f"{row['agreement']} [{row['agreement_lo']}–{row['agreement_hi']}] | "
                f"{row['spearman']} | {row['modal_share']} | {row['n_distinct']} | "
                f"{row['stdev']} | {row['sec_per_img']} | {row['n_comparisons']} | "
                f"{row['self_consistency']} |\n")
    LEADERBOARD.write_text(LEADERBOARD.read_text() + line)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen3-vl:4b-instruct")
    ap.add_argument("--strategy", default="V2")
    ap.add_argument("--resolution", type=int, default=512)
    ap.add_argument("--all", action="store_true", help="run every strategy for --model")
    ap.add_argument("--split", choices=["tune", "holdout"], default="tune")
    ap.add_argument("--promote", action="store_true", help="required to touch the holdout split")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--check-gates", action="store_true")
    args = ap.parse_args(argv)

    if args.split == "holdout" and not args.promote:
        raise SystemExit(
            "Refusing to evaluate on holdout without --promote. The holdout is locked so "
            "prompt iteration cannot overfit to it; run it once, only when promoting a winner."
        )
    if args.split == "holdout" and not args.check_gates:
        raise SystemExit("holdout access requires --check-gates")
    if args.split == "holdout" and args.all:
        raise SystemExit("holdout accepts one release candidate, not --all")
    if args.check_gates and args.split != "holdout":
        raise SystemExit("--check-gates is promotion-only and requires --split holdout")
    if args.check_gates and args.repeat < 3:
        raise SystemExit("--check-gates requires --repeat 3 or greater")

    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.repeat <= 0:
        raise SystemExit("--repeat must be positive")

    golden_rows = load_golden()
    issues = validate_golden(golden_rows)
    if issues:
        print("Golden-set validation failed:")
        for issue in issues:
            print(f"  [{issue.code}] {issue.message}")
        raise SystemExit(
            "Fix golden.json/image issues deliberately before evaluation; no files were changed."
        )
    if args.split == "holdout":
        holdout_rows = [row for row in golden_rows if row["split"] == "holdout"]
        if len(holdout_rows) < 48 or len(
            {str(row["listing_id"]) for row in holdout_rows}
        ) < 48:
            raise SystemExit(
                "holdout requires at least 48 images from 48 distinct listings"
            )
        holdout_ids = [
            row["id"] for row in holdout_rows if row.get("interior", True)
        ]
        holdout_comparisons = [
            comparison
            for comparison in load_comparisons()
            if comparison.a_id in holdout_ids and comparison.b_id in holdout_ids
        ]
        if not label_status(holdout_comparisons, holdout_ids)["ready"]:
            raise SystemExit(
                "holdout human labels must be connected, covered, and repeated before access"
            )

    strategies = DEFAULT_STRATEGIES if args.all else [args.strategy]
    gate_failure = False
    for strat in strategies:
        cfg = ScorerConfig(model=args.model, strategy=strat, resolution=args.resolution)
        guard_holdout_run(
            split=args.split,
            limit=args.limit,
            lock_path=holdout_lock_path(cfg),
        )
        print(f"→ evaluating {cfg.name} on split={args.split} …")
        row = evaluate(
            cfg,
            args.split,
            limit=args.limit,
            repeat=args.repeat,
        )
        _append_leaderboard(row)
        print("   " + json.dumps({k: v for k, v in row.items() if k != "name"}))
        if args.check_gates:
            ids = [r["id"] for r in golden_rows if r.get("interior", True) and r["split"] == args.split]
            comparisons = [
                comparison
                for comparison in load_comparisons()
                if comparison.a_id in ids and comparison.b_id in ids
            ]
            gates = check_promotion_gates(row, label_status(comparisons, ids))
            for name, passed in gates.items():
                print(f"   gate {name:20} {'PASS' if passed else 'FAIL'}")
            gate_failure = gate_failure or not all(gates.values())
    print(f"\nLeaderboard: {LEADERBOARD}")
    if gate_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging
    configure_root_logging()
    main()
