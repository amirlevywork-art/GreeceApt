"""Generate a self-contained visual verification report for vision predictions."""

from __future__ import annotations

import argparse
import base64
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _escape(value)


def _image_data_url(path_value: str) -> str:
    path = Path(path_value)
    content = path.read_bytes()
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def _human_summaries(comparisons: list[dict]) -> dict[str, dict[str, int]]:
    summaries: dict[str, dict[str, int]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "ties": 0}
    )
    for comparison in comparisons:
        a_id = str(comparison.get("a_id", ""))
        b_id = str(comparison.get("b_id", ""))
        winner = comparison.get("winner")
        if winner == "=":
            summaries[a_id]["ties"] += 1
            summaries[b_id]["ties"] += 1
        elif winner == "a":
            summaries[a_id]["wins"] += 1
            summaries[b_id]["losses"] += 1
        elif winner == "b":
            summaries[b_id]["wins"] += 1
            summaries[a_id]["losses"] += 1
    return summaries


def _disagreements(predictions: dict[str, dict], comparisons: list[dict]) -> list[dict]:
    disagreements: list[dict] = []
    for comparison in comparisons:
        a_id = str(comparison.get("a_id", ""))
        b_id = str(comparison.get("b_id", ""))
        a_score = predictions.get(a_id, {}).get("score")
        b_score = predictions.get(b_id, {}).get("score")
        if a_score is None or b_score is None:
            continue
        winner = comparison.get("winner")
        disagrees = (
            (winner == "a" and a_score <= b_score)
            or (winner == "b" and b_score <= a_score)
            or (winner == "=" and a_score != b_score)
        )
        if disagrees:
            disagreements.append(
                {
                    "a_id": a_id,
                    "b_id": b_id,
                    "winner": winner,
                    "a_score": a_score,
                    "b_score": b_score,
                    "severity": abs(float(a_score) - float(b_score)),
                }
            )
    return sorted(disagreements, key=lambda item: item["severity"], reverse=True)


def _metric_cards(metrics: dict) -> str:
    return "".join(
        f'<div class="metric"><span>{_escape(name)}</span><strong>{_number(value, 3)}</strong></div>'
        for name, value in sorted(metrics.items())
    )


def _evidence_table(evidence: dict | None) -> str:
    if not evidence:
        return '<p class="muted">No validated evidence</p>'
    rows = "".join(
        f"<tr><th>{_escape(key)}</th><td>{_escape(value)}</td></tr>"
        for key, value in sorted(evidence.items())
    )
    return f"<table>{rows}</table>"


def generate_report(
    prediction: Path,
    *,
    split: str,
    output: Path,
    comparison_ids: set[str],
) -> Path:
    payload = json.loads(Path(prediction).read_text(encoding="utf-8"))
    raw_predictions = payload.get("predictions", {})
    predictions = {
        str(image_id): record
        for image_id, record in raw_predictions.items()
        if not record.get("split") or record.get("split") == split
    }
    comparisons = [
        comparison
        for comparison in payload.get("comparisons", [])
        if str(comparison.get("a_id", "")) in comparison_ids
        and str(comparison.get("b_id", "")) in comparison_ids
    ]
    summaries = _human_summaries(comparisons)
    disagreements = _disagreements(predictions, comparisons)
    disagreement_ids = {
        image_id
        for item in disagreements
        for image_id in (item["a_id"], item["b_id"])
    }

    disagreement_rows = "".join(
        "<li>"
        f"{_escape(item['a_id'])} ({_number(item['a_score'])}) vs "
        f"{_escape(item['b_id'])} ({_number(item['b_score'])}); "
        f"human winner: {_escape(item['winner'])}"
        "</li>"
        for item in disagreements[:25]
    ) or "<li>No scored disagreements in this split.</li>"

    room_options = "".join(
        f'<option value="{_escape(room)}">{_escape(room)}</option>'
        for room in sorted(
            {str(record.get("room_type", "other")) for record in predictions.values()}
        )
    )
    cards: list[str] = []
    for image_id, record in predictions.items():
        try:
            data_url = _image_data_url(str(record["image_path"]))
        except (KeyError, OSError):
            data_url = ""
        score = record.get("score")
        status = str(record.get("status", "unknown"))
        room = str(record.get("room_type", "other"))
        confidence = record.get("effective_confidence", 0.0)
        summary = summaries[image_id]
        has_disagreement = image_id in disagreement_ids
        image_html = (
            f'<img src="{data_url}" alt="Apartment image {_escape(image_id)}">'
            if data_url
            else '<div class="missing">Image unavailable</div>'
        )
        cards.append(
            f"""
            <article class="image-card" data-status="{_escape(status)}"
                     data-room="{_escape(room)}" data-confidence="{_number(confidence, 6)}"
                     data-score="{_number(score, 6)}" data-disagreement="{str(has_disagreement).lower()}">
              <div class="visual">{image_html}</div>
              <div class="details">
                <div class="title-row"><h3>{_escape(image_id)}</h3><span class="score">{_number(score)}</span></div>
                <p><strong>Room:</strong> {_escape(room)} · <strong>Status:</strong> {_escape(status)}</p>
                <p><strong>Coverage:</strong> {_number(record.get('evidence_coverage'), 3)} ·
                   <strong>Confidence:</strong> {_number(confidence, 3)}</p>
                <p><strong>Latency:</strong> {_number(record.get('latency_ms'), 1)} ms ·
                   <span>Attempts: {_escape(record.get('attempts', 0))}</span></p>
                <p><strong>Human comparisons:</strong> {summary['wins']}W / {summary['losses']}L / {summary['ties']}T</p>
                {_evidence_table(record.get('evidence'))}
              </div>
            </article>
            """
        )

    metadata = payload.get("metadata", {})
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vision verification — {_escape(split)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#17221d; --muted:#637069; --paper:#f3f0e8; --card:#fff; --line:#d8d5ca; --accent:#1d6b50; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.45 system-ui,sans-serif; }}
    header, main {{ width:min(1180px,94vw); margin:auto; }} header {{ padding:36px 0 20px; }} h1,h2,h3 {{ margin:.2em 0; }} .subtle,.muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; margin:18px 0; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }} .metric span,.metric strong {{ display:block; }} .metric strong {{ font-size:1.35rem; }}
    .panel {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:16px 0; }}
    .controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; position:sticky; top:0; z-index:3; box-shadow:0 5px 20px #0001; }}
    select,label {{ padding:7px; }} #cards {{ display:grid; gap:16px; padding-bottom:40px; }}
    .image-card {{ display:grid; grid-template-columns:minmax(260px,42%) 1fr; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
    .visual {{ min-height:240px; background:#202622; display:grid; place-items:center; }} .visual img {{ width:100%; height:100%; max-height:430px; object-fit:contain; }}
    .details {{ padding:18px; overflow:auto; }} .title-row {{ display:flex; justify-content:space-between; gap:12px; }} .score {{ color:var(--accent); font-size:2rem; font-weight:750; }}
    table {{ width:100%; border-collapse:collapse; }} th,td {{ border-top:1px solid var(--line); padding:6px; text-align:left; }} th {{ width:44%; font-weight:600; }}
    @media (max-width:720px) {{ .image-card {{ grid-template-columns:1fr; }} .visual {{ min-height:180px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Image-by-image vision verification</h1>
    <p class="subtle">Split: {_escape(split)} · Model: {_escape(metadata.get('model', 'unknown'))} · {len(predictions)} images</p>
    <section class="metrics">{_metric_cards(payload.get('metrics', {}))}</section>
  </header>
  <main>
    <section class="panel"><h2>Top disagreements</h2><ol>{disagreement_rows}</ol></section>
    <section class="panel controls">
      <label>Status <select id="filter-status"><option value="all">All</option><option value="failed">Failed only</option><option value="low">Low confidence</option></select></label>
      <label>Room <select id="filter-room"><option value="all">All rooms</option>{room_options}</select></label>
      <label><input id="filter-disagreement" type="checkbox"> Disagreements only</label>
      <label>Sort <select id="sort"><option value="index">Input order</option><option value="score-asc">Score low → high</option><option value="confidence-asc">Confidence low → high</option></select></label>
    </section>
    <section id="cards">{''.join(cards)}</section>
  </main>
  <script>
    const container = document.getElementById('cards');
    const original = [...container.children];
    function refresh() {{
      const status = document.getElementById('filter-status').value;
      const room = document.getElementById('filter-room').value;
      const disagreement = document.getElementById('filter-disagreement').checked;
      const sort = document.getElementById('sort').value;
      const cards = [...original];
      if (sort === 'score-asc') cards.sort((a,b) => (+a.dataset.score || 999) - (+b.dataset.score || 999));
      if (sort === 'confidence-asc') cards.sort((a,b) => (+a.dataset.confidence) - (+b.dataset.confidence));
      cards.forEach(card => {{
        const failed = !['accepted','insufficient_evidence'].includes(card.dataset.status);
        const low = (+card.dataset.confidence) < 0.6;
        const visible = (room === 'all' || card.dataset.room === room)
          && (!disagreement || card.dataset.disagreement === 'true')
          && (status === 'all' || (status === 'failed' && failed) || (status === 'low' && low));
        card.hidden = !visible; container.appendChild(card);
      }});
    }}
    document.querySelectorAll('.controls input,.controls select').forEach(control => control.addEventListener('change', refresh));
  </script>
</body>
</html>"""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> None:
    from greeceapt.evaluation.golden_set import load_golden

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction", type=Path, required=True)
    parser.add_argument("--split", choices=["tune", "holdout"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    comparison_ids = {
        str(row["id"])
        for row in load_golden()
        if row.get("split") == args.split and row.get("interior", True)
    }
    generated = generate_report(
        args.prediction,
        split=args.split,
        output=args.output,
        comparison_ids=comparison_ids,
    )
    print(generated)


if __name__ == "__main__":
    main()
