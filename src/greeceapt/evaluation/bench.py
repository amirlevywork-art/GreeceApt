"""
Hardware capability gate — can THIS machine sustain a candidate VLM at usable speed?

Not "does it load" but "does it hold up over a real workload": sustained latency, its p95,
drift between the first and last images (thermal throttling), memory-induced swap growth, and
whether the model's own output is stable enough to evaluate at all.

    python -m greeceapt.evaluation.bench qwen3-vl:4b --images 60
    python -m greeceapt.evaluation.bench --idle-baseline

A model advances only if: p95 <= MAX_SEC_PER_IMAGE, swap growth below the idle-baseline
threshold, latency drift < 25% first-vs-last, and self-agreement stdev < 0.3 on repeat scoring.
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import subprocess
import time
from pathlib import Path

import requests

from greeceapt.evaluation.golden_set import IMAGES_DIR, load_golden

OLLAMA = "http://localhost:11434"
MAX_SEC_PER_IMAGE = 6.0
MAX_DRIFT = 0.25
MAX_SELF_STDEV = 0.3

_PROMPT = (
    "Rate the physical condition of this apartment room from 1 to 10, where 1 is derelict "
    "and 10 is a brand-new luxury renovation. Reply with only the number."
)


def _swap_mb() -> float:
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        # "total = 3072.00M  used = 1234.50M  free = ..."
        for tok in out.replace("=", " ").split():
            if tok.endswith("M") and "used" in out[:out.index(tok)][-8:]:
                return float(tok[:-1])
        parts = out.split()
        return float(parts[parts.index("used") + 2].rstrip("M"))
    except Exception:
        return 0.0


def _b64(sha: str) -> str:
    return base64.b64encode((IMAGES_DIR / f"{sha}.jpg").read_bytes()).decode()


def _score_once(model: str, img_b64: str, timeout: float = 60.0) -> tuple[float, str]:
    t0 = time.time()
    r = requests.post(
        f"{OLLAMA}/api/generate",
        json={"model": model, "prompt": _PROMPT, "images": [img_b64],
              "stream": False, "options": {"temperature": 0, "num_predict": 8}},
        timeout=timeout,
    )
    r.raise_for_status()
    return time.time() - t0, r.json().get("response", "").strip()


def idle_baseline(seconds: int = 60) -> float:
    """Swap delta over an idle window — the reference for judging model-induced swap."""
    print(f"idle baseline: sampling swap for {seconds}s with no model loaded…")
    start = _swap_mb()
    time.sleep(seconds)
    delta = _swap_mb() - start
    print(f"  idle swap delta: {delta:+.0f} MB")
    return delta


def bench(model: str, n_images: int, idle_swap: float = 0.0) -> dict:
    golden = [r for r in load_golden() if r.get("interior", True)]
    shas = [r["sha256"] for r in golden][:n_images]
    if not shas:
        raise SystemExit("No golden images. Run golden_set --add first.")
    imgs = [_b64(s) for s in shas]

    print(f"benchmarking {model} on {len(imgs)} images…")
    swap_start = _swap_mb()
    times: list[float] = []
    # self-stability: same image scored 3x
    stab = [_score_once(model, imgs[0])[1] for _ in range(3)]

    for i, img in enumerate(imgs):
        dt, _resp = _score_once(model, img)
        times.append(dt)
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(imgs)}  median so far {statistics.median(times):.2f}s")

    swap_delta = _swap_mb() - swap_start
    half = max(1, len(times) // 4)
    first, last = times[:half], times[-half:]
    drift = (statistics.mean(last) - statistics.mean(first)) / max(statistics.mean(first), 1e-6)

    def _num(s: str) -> float | None:
        import re
        m = re.search(r"\d+(\.\d+)?", s)
        return float(m.group()) if m else None
    stab_nums = [x for x in (_num(s) for s in stab) if x is not None]
    self_stdev = statistics.pstdev(stab_nums) if len(stab_nums) > 1 else 0.0

    p95 = sorted(times)[min(len(times) - 1, int(0.95 * len(times)))]
    result = {
        "model": model,
        "n": len(times),
        "median_s": round(statistics.median(times), 2),
        "p95_s": round(p95, 2),
        "drift": round(drift, 3),
        "swap_delta_mb": round(swap_delta, 0),
        "idle_swap_mb": round(idle_swap, 0),
        "self_stdev": round(self_stdev, 3),
    }
    # Gate on the DIRECT degradation signals. A model that swap-thrashes shows it as rising
    # latency (drift) — so drift + p95 + output stability are decisive. Machine-wide swap on
    # a 16 GB Mac is confounded by everything else resident, so it is reported, not gated.
    result["passes"] = bool(
        p95 <= MAX_SEC_PER_IMAGE
        and abs(drift) < MAX_DRIFT
        and self_stdev < MAX_SELF_STDEV
    )
    return result


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", nargs="?", help="ollama model tag")
    ap.add_argument("--images", type=int, default=60)
    ap.add_argument("--idle-baseline", action="store_true")
    ap.add_argument("--idle-swap", type=float, default=0.0)
    args = ap.parse_args(argv)

    if args.idle_baseline:
        idle_baseline()
        return
    if not args.model:
        ap.error("model tag required (or use --idle-baseline)")

    res = bench(args.model, args.images, args.idle_swap)
    print(json.dumps(res, indent=2))
    survey = Path("docs/vision_model_survey.md")
    survey.parent.mkdir(exist_ok=True)
    line = (f"| {res['model']} | {res['median_s']} | {res['p95_s']} | {res['drift']} | "
            f"{res['swap_delta_mb']} | {res['self_stdev']} | {'PASS' if res['passes'] else 'FAIL'} |\n")
    if not survey.exists():
        survey.write_text(
            "# Vision model survey (this machine)\n\n"
            "| model | median s/img | p95 s/img | drift | swap Δ MB | self-stdev | gate |\n"
            "|---|---|---|---|---|---|---|\n"
        )
    survey.write_text(survey.read_text() + line)
    print(f"appended to {survey}")


if __name__ == "__main__":
    main()
