"""
Pairwise labelling CLI — you rank apartment photos by condition; the file grows over time.

    python -m greeceapt.evaluation.label          # rank until you press q
    python -m greeceapt.evaluation.label --stats  # progress only, no labelling

Design promises:
  * Append-only JSONL (``comparisons.jsonl``). It is never rewritten or truncated; every
    judgement is flushed the instant you make it, so quitting any way — q, Ctrl-C, closing
    the terminal — loses nothing. The next run continues from exactly where you stopped.
  * No fixed quota. Rank as much or as little as you like; θ movement in --stats tells you
    when more labelling has stopped helping.
  * Three-phase pair selection so the comparison graph stays a single connected component
    (θ across two disconnected clusters would not be comparable):
        1. connectivity — link every image into one graph (random spanning tree)
        2. coverage     — bring every image to >= MIN_COVERAGE comparisons
        3. uncertainty  — compare the pairs whose current θ are closest (most informative)
  * ~8% deliberate repeats (same pair re-shown later, sides flipped) measure YOUR
    self-consistency, which is the ceiling any model is graded against.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
from base64 import b64encode
from pathlib import Path

from greeceapt.storage import paths
from greeceapt.evaluation.bradley_terry import Comparison, connected_components, fit_bradley_terry
from greeceapt.evaluation.golden_set import IMAGES_DIR, load_golden
from greeceapt.evaluation import metrics

EVAL_DIR = paths.DATA_DIR / "vision_eval"
COMPARISONS_JSONL = EVAL_DIR / "comparisons.jsonl"
MIN_COVERAGE = 3
REPEAT_RATE = 0.08
MIN_REPEAT_JUDGMENTS = 20


# ── Persistence (append-only) ─────────────────────────────────────────────────

def load_comparisons() -> list[Comparison]:
    if not COMPARISONS_JSONL.exists():
        return []
    out: list[Comparison] = []
    with COMPARISONS_JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(Comparison(d["a_id"], d["b_id"], d["winner"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def _append(rec: dict) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with COMPARISONS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


# ── Pair selection ─────────────────────────────────────────────────────────────

def _judged_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def comparisons_for_ids(
    comparisons: list[Comparison], ids: list[str] | set[str]
) -> list[Comparison]:
    """Keep only judgments whose two images belong to the selected set."""
    allowed = set(ids)
    return [
        comparison
        for comparison in comparisons
        if comparison.a_id in allowed and comparison.b_id in allowed
    ]


def _spanning_tree_edges(ids: list[str], judged: set[tuple[str, str]], rng: random.Random) -> list[tuple[str, str]]:
    """Edges needed to link all images into one component, respecting existing edges."""
    parent: dict[str, str] = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for a, b in judged:
        if a in parent and b in parent:
            union(a, b)

    shuffled = ids[:]
    rng.shuffle(shuffled)
    needed: list[tuple[str, str]] = []
    for i in range(1, len(shuffled)):
        a, b = shuffled[i - 1], shuffled[i]
        if find(a) != find(b):
            union(a, b)
            needed.append(_judged_key(a, b))
    return needed


def choose_pair(
    ids: list[str],
    judged: set[tuple[str, str]],
    counts: dict[str, int],
    theta: dict[str, float],
    rng: random.Random,
) -> tuple[str, str, str]:
    """Return (a_id, b_id, phase). Phase is 'connect' | 'cover' | 'uncertain' | 'repeat'."""
    # Phase 1: connectivity
    for edge in _spanning_tree_edges(ids, judged, rng):
        return edge[0], edge[1], "connect"

    # Occasional deliberate repeat of an already-judged pair (consistency check)
    if judged and rng.random() < REPEAT_RATE:
        a, b = rng.choice(list(judged))
        return (b, a, "repeat") if rng.random() < 0.5 else (a, b, "repeat")

    # Phase 2: coverage — least-compared image, paired with another low-coverage image
    under = [i for i in ids if counts.get(i, 0) < MIN_COVERAGE]
    if under:
        under.sort(key=lambda i: counts.get(i, 0))
        a = under[0]
        for b in sorted(ids, key=lambda i: counts.get(i, 0)):
            if b != a and _judged_key(a, b) not in judged:
                return a, b, "cover"

    # Phase 3: uncertainty — closest θ pair not yet judged
    if theta:
        ordered = sorted(ids, key=lambda i: theta.get(i, 0.0))
        best: tuple[float, str, str] | None = None
        for i in range(len(ordered) - 1):
            a, b = ordered[i], ordered[i + 1]
            if _judged_key(a, b) in judged:
                continue
            gap = abs(theta.get(a, 0.0) - theta.get(b, 0.0))
            if best is None or gap < best[0]:
                best = (gap, a, b)
        if best is not None:
            return best[1], best[2], "uncertain"

    # Fallback: any unjudged pair
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _judged_key(ids[i], ids[j]) not in judged:
                return ids[i], ids[j], "uncertain"
    # everything judged: random repeat
    a, b = rng.sample(ids, 2)
    return a, b, "repeat"


# ── Display ────────────────────────────────────────────────────────────────────

def _img_data_uri(sha: str) -> str:
    data = (IMAGES_DIR / f"{sha}.jpg").read_bytes()
    return "data:image/jpeg;base64," + b64encode(data).decode()


def _show(a_sha: str, b_sha: str) -> Path:
    html = f"""<!doctype html><meta charset=utf-8>
<title>A vs B</title>
<style>body{{margin:0;background:#111;font-family:sans-serif;color:#eee}}
.row{{display:flex}}.col{{flex:1;text-align:center;padding:8px}}
img{{max-width:100%;max-height:88vh}}h2{{margin:6px}}</style>
<div class=row>
  <div class=col><h2>A</h2><img src="{_img_data_uri(a_sha)}"></div>
  <div class=col><h2>B</h2><img src="{_img_data_uri(b_sha)}"></div>
</div>"""
    f = Path(tempfile.gettempdir()) / "greeceapt_label_pair.html"
    f.write_text(html, encoding="utf-8")
    if sys.platform == "darwin":
        subprocess.run(["open", str(f)], check=False)
    elif sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", str(f)], check=False)
    return f


# ── Stats ──────────────────────────────────────────────────────────────────────

def _stats(comparisons: list[Comparison], ids: list[str], prev_theta: dict[str, float] | None = None):
    counts: dict[str, int] = {i: 0 for i in ids}
    judged: set[tuple[str, str]] = set()
    for c in comparisons:
        counts[c.a_id] = counts.get(c.a_id, 0) + 1
        counts[c.b_id] = counts.get(c.b_id, 0) + 1
        judged.add(_judged_key(c.a_id, c.b_id))
    theta = fit_bradley_terry(comparisons) if comparisons else {}
    comps_graph = connected_components(comparisons)
    compared_ids = {item for component in comps_graph for item in component}
    isolated_count = sum(1 for image_id in ids if image_id not in compared_ids)
    n_components = len(comps_graph) + isolated_count
    under = sum(1 for i in ids if counts.get(i, 0) < MIN_COVERAGE)
    consistency, n_rep = metrics.self_consistency(comparisons)
    drift = None
    if prev_theta and theta:
        common = set(prev_theta) & set(theta)
        if common:
            drift = sum(abs(theta[i] - prev_theta[i]) for i in common) / len(common)
    return {
        "comparisons": len(comparisons),
        "images": len(ids),
        "under_min_coverage": under,
        "n_components": n_components,
        "self_consistency": consistency,
        "consistency_pairs": n_rep,
        "theta_drift": drift,
        "theta": theta,
    }


def label_status(comparisons: list[Comparison], ids: list[str]) -> dict:
    """Return explicit human-label completion gates used by promotion checks."""
    status = _stats(comparisons_for_ids(comparisons, ids), ids)
    status.update(
        {
            "connected_complete": status["n_components"] == 1,
            "coverage_complete": status["under_min_coverage"] == 0,
            "repeats_complete": status["consistency_pairs"] >= MIN_REPEAT_JUDGMENTS,
        }
    )
    status["ready"] = all(
        status[key]
        for key in ("connected_complete", "coverage_complete", "repeats_complete")
    )
    return status


def print_stats() -> None:
    golden = load_golden()
    ids = [r["id"] for r in golden if r.get("interior", True)]
    st = label_status(load_comparisons(), ids)
    print(f"comparisons:          {st['comparisons']}")
    print(f"images (interior):    {st['images']}")
    print(f"under {MIN_COVERAGE} comparisons:   {st['under_min_coverage']}")
    print(f"connected components: {st['n_components']}  (must reach 1 for comparable θ)")
    print(f"self-consistency:     {st['self_consistency']:.3f}  (from {st['consistency_pairs']} repeat pairs)")
    print(f"connected graph:      {'COMPLETE' if st['connected_complete'] else 'INCOMPLETE'}")
    print(f"3 comparisons/image:  {'COMPLETE' if st['coverage_complete'] else 'INCOMPLETE'}")
    print(f"20 repeat judgments:  {'COMPLETE' if st['repeats_complete'] else 'INCOMPLETE'}")
    print(f"promotion labels:     {'READY' if st['ready'] else 'BLOCKED'}")
    print("  → this is the ceiling any model is graded against.")


# ── Interactive loop ───────────────────────────────────────────────────────────

def run_labelling() -> None:
    golden = load_golden()
    interior = [r for r in golden if r.get("interior", True)]
    sha_by_id = {r["id"]: r["sha256"] for r in interior}
    ids = [r["id"] for r in interior]
    if len(ids) < 2:
        print("Need at least 2 interior images in the golden set. Run golden_set --add first.")
        return

    comparisons = comparisons_for_ids(load_comparisons(), ids)
    rng = random.Random()
    session = time.strftime("%Y%m%dT%H%M%S")

    print(f"Loaded {len(comparisons)} existing comparisons over {len(ids)} images.")
    print("Keys:  [a] A better   [b] B better   [=] equal   [s] skip   [q] save+quit\n")

    labelled_this_run = 0
    while True:
        counts: dict[str, int] = {i: 0 for i in ids}
        judged: set[tuple[str, str]] = set()
        for c in comparisons:
            counts[c.a_id] = counts.get(c.a_id, 0) + 1
            counts[c.b_id] = counts.get(c.b_id, 0) + 1
            judged.add(_judged_key(c.a_id, c.b_id))
        theta = fit_bradley_terry(comparisons) if comparisons else {}

        a_id, b_id, phase = choose_pair(ids, judged, counts, theta, rng)
        _show(sha_by_id[a_id], sha_by_id[b_id])

        try:
            ans = input(f"[{phase}] A vs B — which apartment is in better condition? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nSaved. Bye.")
            break

        if ans in ("q", "quit"):
            print("Saved. Bye.")
            break
        if ans in ("s", "skip", ""):
            continue
        if ans not in ("a", "b", "="):
            print("  (use a / b / = / s / q)")
            continue

        rec = {"a_id": a_id, "b_id": b_id, "winner": ans, "phase": phase,
               "ts": time.time(), "session": session}
        _append(rec)
        comparisons.append(Comparison(a_id, b_id, ans))
        labelled_this_run += 1

    st = _stats(comparisons, ids)
    print(f"\nThis run: {labelled_this_run} new. Total {st['comparisons']} over {st['images']} images. "
          f"Components: {st['n_components']} (want 1). Self-consistency: {st['self_consistency']:.3f}.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stats", action="store_true", help="show progress and exit")
    args = ap.parse_args(argv)
    if args.stats:
        print_stats()
    else:
        run_labelling()


if __name__ == "__main__":
    main()
