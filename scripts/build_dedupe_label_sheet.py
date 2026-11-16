"""Build a local HTML sheet for labeling candidate duplicate pairs (same flat or not).

Positives: pairs with identical price, area (±1 m²) and neighborhood that share ≥ 3 photos
at Hamming ≤ 8 — the November pairs the production dedupe left separate.
Hard negatives: identical price and area (±1 m²) in the same neighborhood but with a different
known floor class or construction year, or no shared photo — likely different units.

Open the HTML, click "same" / "different" / "unsure" for each pair, then "Download labels"
and save the file as ``tests/fixtures/dedupe_same_source_2026-11.json``.

Usage:
    python scripts/build_dedupe_label_sheet.py --input tests/fixtures/november_ranking_input_2026.json \
        --out data/dedupe_label_sheet.html [--positives 40 --negatives 20 --seed 7]
"""

from __future__ import annotations

import argparse
import html
import json
import random
from pathlib import Path

HAMMING = 8


def _fulls(row: dict) -> list[int]:
    values = []
    for value in (row.get("photo_fingerprints") or {}).values():
        if isinstance(value, dict) and value.get("full"):
            full = value["full"]
            values.append(int(full, 16) if isinstance(full, str) else int(full))
    return values


def _matches(a: dict, b: dict) -> int:
    right = _fulls(b)
    return sum(1 for x in _fulls(a) if any((x ^ y).bit_count() <= HAMMING for y in right))


def select_pairs(rows: list[dict], positives: int, negatives: int, seed: int) -> list[dict]:
    from greeceapt.collect.floor_resolver import apply_floor_resolution

    rows = [dict(row) for row in rows]
    for row in rows:
        apply_floor_resolution(row)
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("price_eur") and row.get("area_sqm"):
            groups.setdefault((int(row["price_eur"]), row.get("neighborhood")), []).append(row)
    pos, neg = [], []
    for members in groups.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if abs(float(a["area_sqm"]) - float(b["area_sqm"])) > 1:
                    continue
                shared = _matches(a, b)
                record = {"a": a, "b": b, "shared_photos": shared}
                if shared >= 3:
                    pos.append(record)
                elif (
                    shared == 0
                    or (a.get("floor_class") and b.get("floor_class") and a["floor_class"] != b["floor_class"])
                    or (a.get("year_built") and b.get("year_built") and abs(a["year_built"] - b["year_built"]) > 3)
                ):
                    neg.append(record)
    rng = random.Random(seed)
    chosen = rng.sample(pos, min(positives, len(pos))) + rng.sample(neg, min(negatives, len(neg)))
    rng.shuffle(chosen)
    return chosen


def _side(row: dict) -> str:
    photos = "".join(
        f'<img loading="lazy" src="{html.escape(url)}">' for url in (row.get("photo_urls") or [])[:8]
    )
    desc = html.escape((row.get("description") or "")[:500])
    return (
        f'<div class="side"><a href="{html.escape(row["url"])}" target="_blank">{html.escape(row["url"])}</a>'
        f'<p>€{row.get("price_eur"):,.0f} · {row.get("area_sqm")} m² · floor {row.get("floor_class")} '
        f'({row.get("floor")}) · built {row.get("year_built")} · {row.get("bedrooms")} bd · '
        f'{html.escape(str(row.get("neighborhood")))} · {html.escape(str(row.get("source")))}</p>'
        f'<div class="photos">{photos}</div><p class="desc">{desc}</p></div>'
    )


def render(pairs: list[dict]) -> str:
    items = []
    for index, pair in enumerate(pairs, start=1):
        a, b = pair["a"], pair["b"]
        pid = f"p{index:03d}"
        items.append(
            f'<section data-id="{pid}" data-a="{html.escape(a["url"])}" data-b="{html.escape(b["url"])}">'
            f'<h2>{index}. shared photos (Hamming ≤ {HAMMING}): {pair["shared_photos"]}</h2>'
            f'<div class="pair">{_side(a)}{_side(b)}</div>'
            '<div class="choice">'
            + "".join(
                f'<label><input type="radio" name="{pid}" value="{v}"> {v}</label>'
                for v in ("same", "different", "unsure")
            )
            + "</div></section>"
        )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Duplicate pair labels</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#f6f6f4;color:#222}}
section{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:0 0 18px}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.photos{{display:flex;flex-wrap:wrap;gap:4px}} .photos img{{width:150px;height:110px;object-fit:cover}}
.desc{{font-size:12px;color:#555}} .choice{{margin-top:8px;font-size:16px}} label{{margin-right:18px}}
#bar{{position:sticky;top:0;background:#222;color:#fff;padding:8px;border-radius:6px;margin-bottom:12px}}
button{{font-size:15px;padding:6px 12px}}
</style></head><body>
<div id="bar"><span id="count"></span> <button onclick="download()">Download labels</button></div>
{''.join(items)}
<script>
function labels(){{
  const out={{version:1,hamming:{HAMMING},pairs:[]}};
  document.querySelectorAll('section').forEach(s=>{{
    const c=s.querySelector('input:checked');
    out.pairs.push({{id:s.dataset.id,urls:[s.dataset.a,s.dataset.b],label:c?c.value:null}});
  }});
  return out;
}}
function refresh(){{const l=labels();document.getElementById('count').textContent=
  l.pairs.filter(p=>p.label).length+' / '+l.pairs.length+' labeled';}}
document.addEventListener('change',refresh);refresh();
function download(){{
  const blob=new Blob([JSON.stringify(labels(),null,2)],{{type:'application/json'}});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='dedupe_same_source_2026-11.json';a.click();
}}
</script></body></html>"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--positives", type=int, default=40)
    parser.add_argument("--negatives", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    pairs = select_pairs(rows, args.positives, args.negatives, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(pairs), encoding="utf-8")
    print(f"wrote {len(pairs)} pairs to {args.out}")


if __name__ == "__main__":
    main()
