"""
Scoring metrics for the vision bake-off — pure Python.

The primary metric is pairwise agreement (does the model order a pair the way you did),
reported with a bootstrap confidence interval so two candidates aren't declared different
on noise. Also: Spearman rank correlation vs Bradley-Terry theta, and collapse stats that
guard against a scorer that just emits the same number for everything.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from statistics import pstdev
from typing import Mapping

from greeceapt.evaluation.bradley_terry import Comparison


def _agreement_hits(pred: dict[str, float], comparisons: Sequence[Comparison]) -> list[float]:
    """Per-comparison agreement score (1 agree, 0 disagree, 0.5 tie/undecidable)."""
    hits: list[float] = []
    for c in comparisons:
        if c.a_id not in pred or c.b_id not in pred:
            continue
        pa, pb = pred[c.a_id], pred[c.b_id]
        if c.winner == "=":
            hits.append(1.0 if pa == pb else 0.5)
        else:
            better = c.a_id if c.winner == "a" else c.b_id
            worse = c.b_id if c.winner == "a" else c.a_id
            if pred[better] > pred[worse]:
                hits.append(1.0)
            elif pred[better] == pred[worse]:
                hits.append(0.5)
            else:
                hits.append(0.0)
    return hits


def pairwise_agreement(
    pred: dict[str, float],
    comparisons: Sequence[Comparison],
    *,
    bootstrap: int = 2000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """
    Fraction of judged pairs the model orders the same way, with a 95% bootstrap CI.

    Returns ``(point, ci_lo, ci_hi)``. The CI matters: with a few hundred comparisons,
    0.78 vs 0.81 is not a real difference, and the promotion gate uses the CI *lower bound*
    so a candidate is never promoted on noise.
    """
    hits = _agreement_hits(pred, comparisons)
    if not hits:
        return 0.0, 0.0, 0.0
    point = sum(hits) / len(hits)
    if bootstrap <= 0 or len(hits) == 1:
        return point, point, point

    rng = random.Random(seed)
    means: list[float] = []
    n = len(hits)
    for _ in range(bootstrap):
        s = 0.0
        for _ in range(n):
            s += hits[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * bootstrap)]
    hi = means[min(bootstrap - 1, int(0.975 * bootstrap))]
    return point, lo, hi


def pairwise_agreement_clustered(
    pred: dict[str, float],
    comparisons: Sequence[Comparison],
    *,
    cluster_by_id: Mapping[str, str],
    bootstrap: int = 2000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Agreement CI from resampling listing clusters instead of comparison rows."""
    rows: list[tuple[float, str, str]] = []
    for comparison in comparisons:
        hits = _agreement_hits(pred, [comparison])
        if not hits:
            continue
        cluster_a = cluster_by_id.get(comparison.a_id)
        cluster_b = cluster_by_id.get(comparison.b_id)
        if cluster_a is None or cluster_b is None:
            continue
        rows.append((hits[0], cluster_a, cluster_b))
    if not rows:
        return 0.0, 0.0, 0.0

    point = sum(hit for hit, _, _ in rows) / len(rows)
    clusters = sorted({cluster for _, a, b in rows for cluster in (a, b)})
    if bootstrap <= 0 or len(clusters) == 1:
        return point, point, point

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(bootstrap):
        counts = Counter(clusters[rng.randrange(len(clusters))] for _ in clusters)
        weighted_hits = 0.0
        total_weight = 0.0
        for hit, cluster_a, cluster_b in rows:
            weight = float(counts[cluster_a])
            if cluster_b != cluster_a:
                weight += counts[cluster_b]
            if weight:
                weighted_hits += hit * weight
                total_weight += weight
        if total_weight:
            means.append(weighted_hits / total_weight)
    if not means:
        return point, point, point
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return point, lo, hi


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return round(ordered[lower] + fraction * (ordered[upper] - ordered[lower]), 10)


def prediction_stats(predictions: Sequence[dict]) -> dict[str, float]:
    """Operational reliability and latency metrics from individual predictions."""
    if not predictions:
        return {
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "latency_mean_ms": 0.0,
            "valid_rate": 0.0,
            "null_rate": 1.0,
            "retry_rate": 0.0,
        }
    latencies = [float(item.get("latency_ms", 0.0)) for item in predictions]
    valid = sum(item.get("status") != "model_error" for item in predictions)
    nulls = sum(item.get("score") is None for item in predictions)
    retries = sum(int(item.get("attempts", 1)) > 1 for item in predictions)
    count = len(predictions)
    return {
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "latency_mean_ms": sum(latencies) / count,
        "valid_rate": valid / count,
        "null_rate": nulls / count,
        "retry_rate": retries / count,
    }


def self_consistency(comparisons: Sequence[Comparison]) -> tuple[float, int]:
    """
    Human self-agreement from repeated pairs (same unordered pair judged more than once).

    This is the realistic ceiling — a model cannot agree with you more than you agree with
    yourself. Returns ``(consistency, n_repeat_pairs)``; consistency is NaN-safe (returns
    1.0 when there are no repeats, with n=0 so callers can tell).
    """
    from collections import defaultdict

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for c in comparisons:
        key = tuple(sorted((c.a_id, c.b_id)))
        # normalise the winner to the sorted-first item's perspective
        if c.winner == "=":
            verdict = "="
        else:
            winner_id = c.a_id if c.winner == "a" else c.b_id
            verdict = "first" if winner_id == key[0] else "second"
        groups[key].append(verdict)

    agree = 0.0
    total = 0
    for verdicts in groups.values():
        if len(verdicts) < 2:
            continue
        baseline = verdicts[0]
        for repeated in verdicts[1:]:
            total += 1
            if baseline == repeated:
                agree += 1.0
            elif baseline == "=" or repeated == "=":
                agree += 0.5
    if total == 0:
        return 1.0, 0
    return agree / total, total


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation with average ranks for ties."""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((r - mx) ** 2 for r in rx)
    vy = sum((r - my) ** 2 for r in ry)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx ** 0.5 * vy ** 0.5)


def _avg_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def collapse_stats(scores: Sequence[float]) -> dict[str, float]:
    """
    Anti-quantisation guard. The old scorer put 46% of images on exactly 5.0.

    Returns modal_share (fraction on the single most common rounded value), n_distinct
    (distinct values at 1-decimal resolution) and stdev.
    """
    if not scores:
        return {"modal_share": 1.0, "n_distinct": 0.0, "stdev": 0.0}
    from collections import Counter

    rounded = [round(s, 1) for s in scores]
    counts = Counter(rounded)
    modal_share = max(counts.values()) / len(rounded)
    return {
        "modal_share": modal_share,
        "n_distinct": float(len(counts)),
        "stdev": pstdev(scores) if len(scores) > 1 else 0.0,
    }
