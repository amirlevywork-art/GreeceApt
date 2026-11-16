"""
Per-run reliability report for the accepted Qwen condition score.

* duplicate_agreement — likely duplicates (same price, rounded area and construction year,
  different ads/photo sets): how far apart their scores are, and how often their condition
  factors differ by more than 5 % (what actually moves a flat in the ranking);
* validity — scores of flats whose publisher says "renovated" vs "needs renovation";
* distribution — the overall score distribution.

Stored with every ranking (``ranking_metadata.vision_reliability``) so a model or prompt change
that degrades the score is visible. CLI: ``python -m greeceapt.vision.reliability --db PATH``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from greeceapt.scoring.condition_text import publisher_condition
from greeceapt.scoring.yield_model import condition_factor


def _accepted(rows: Iterable[Mapping]) -> list[Mapping]:
    return [
        row for row in rows
        if row.get("visual_status_v2") == "accepted" and row.get("visual_score_v2") is not None
    ]


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def reliability_report(rows: Iterable[Mapping]) -> dict:
    accepted = _accepted(rows)
    scores = [float(row["visual_score_v2"]) for row in accepted]

    groups: dict[tuple, list[float]] = defaultdict(list)
    for row in accepted:
        if row.get("price_eur") and row.get("area_sqm") and row.get("year_built"):
            groups[(int(float(row["price_eur"])), round(float(row["area_sqm"])), row["year_built"])].append(
                float(row["visual_score_v2"])
            )
    deltas: list[float] = []
    factor_jumps = 0
    for members in groups.values():
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                deltas.append(abs(left - right))
                fl, fr = condition_factor(max(3.0, left)), condition_factor(max(3.0, right))
                if fl and fr and abs(fl - fr) / max(fl, fr) > 0.05:
                    factor_jumps += 1

    validity = {}
    for label in ("renovated", "needs_renovation"):
        values = [float(row["visual_score_v2"]) for row in accepted if publisher_condition(row) == label]
        validity[label] = {
            "n": len(values),
            "median": _round(statistics.median(values)) if values else None,
            "share_ge_8": _round(sum(v >= 8 for v in values) / len(values)) if values else None,
            "share_lt_6": _round(sum(v < 6 for v in values) / len(values)) if values else None,
        }

    return {
        "duplicate_agreement": {
            "pairs": len(deltas),
            "median_abs_delta": _round(statistics.median(deltas)) if deltas else None,
            "p90_abs_delta": _round(_quantile(deltas, 0.9)) if deltas else None,
            "share_delta_above_1": _round(sum(d > 1 for d in deltas) / len(deltas)) if deltas else None,
            "share_factor_differs_over_5pct": _round(factor_jumps / len(deltas)) if deltas else None,
        },
        "validity": validity,
        "distribution": {
            "n": len(scores),
            "median": _round(statistics.median(scores)) if scores else None,
            "p10": _round(_quantile(scores, 0.1)) if scores else None,
            "p90": _round(_quantile(scores, 0.9)) if scores else None,
            "share_ge_8": _round(sum(s >= 8 for s in scores) / len(scores)) if scores else None,
            "share_lt_4": _round(sum(s < 4 for s in scores) / len(scores)) if scores else None,
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Qwen condition-score reliability report.")
    parser.add_argument("--db", type=Path, required=True, help="updated_listings.db")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        for row in conn.execute("SELECT * FROM listings"):
            data = dict(row)
            raw = json.loads(data.get("raw_json") or "{}")
            data.setdefault("description", raw.get("description"))
            rows.append(data)
    finally:
        conn.close()
    report = json.dumps(reliability_report(rows), indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
