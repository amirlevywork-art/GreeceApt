# Local vision scoring: measured status

## Selected production candidate

- Model: `qwen3-vl:4b-instruct` through local Ollama.
- Prompt: `condition-v2-p3`.
- Schema: `condition-v2`.
- Input: aspect-preserving JPEG, maximum edge 512 px.
- One model only; temperature 0, thinking disabled, one retry only for invalid output.
- The unqualified `qwen3-vl:4b` tag resolves to a thinking renderer on this
  machine and is not eligible for this pipeline.
- A controlled 72-image comparison against the exact instruct-only
  `qwen3-vl:8b-instruct` tag found no quality advantage for 8B and clear
  regressions in latency and structured-output reliability (details below).

The model returns observable attributes. Python applies the numeric score, evidence
coverage, confidence, duplicate removal, cache, and room-balanced listing aggregation.
There is no silent 5.0 fallback.

## Final tune measurement (72 images)

| metric | result | preregistered gate | status |
|---|---:|---:|---|
| Valid structured responses | 100% | >= 95% | PASS |
| Retry rate | 0% | < 5% | PASS |
| Five-image, three-run max score stdev | 0.00 | < 0.30 | PASS |
| Numeric coverage | 72 / 72 | diagnostic | PASS |
| Pairwise agreement | 87.5% | holdout CI gate | PROVISIONAL |
| Agreement 95% CI | 78.2%-95.1% | lower bound >= 80% of human consistency | FAIL on tune |
| Spearman correlation | 0.366 | diagnostic | PROVISIONAL |
| Modal score share | 13.9% | < 15% | PASS |
| Score standard deviation | 1.42 | >= 1.2 | PASS |
| Distinct scores | 27 | diagnostic | PASS |
| Median latency | 7.62 s/image | diagnostic | -- |
| p95 latency | 8.51 s/image | <= 3.0 s/image | FAIL |
| End-to-end mean latency | 7.63 s/image | <= 2.5 s/image | FAIL |

These accuracy numbers use only 20 valid interior pairwise judgments and are tune-set measurements,
not an unbiased production estimate.

## Controlled 4B versus 8B comparison

Both models were run on the same 72 tune images with the same `condition-v2-p3`
prompt, `condition-v2` schema, 512 px preparation, deterministic Python scorer,
and the same 20 valid interior human pairwise judgments. The comparison used the instruct-only tags; the
unqualified 8B tag is also a thinking renderer on this machine and was not used.

| metric | `qwen3-vl:4b-instruct` | `qwen3-vl:8b-instruct` | result |
|---|---:|---:|---|
| Pairwise agreement | 87.5% | 88.2% | no meaningful difference |
| Agreement 95% CI | 78.2%-95.1% | 76.7%-97.0% | heavily overlapping |
| Spearman correlation | 0.366 | 0.438 | 8B higher, provisional |
| Valid structured responses | 100% (72/72) | 94.4% (68/72) | 4B passes; 8B fails |
| Retry rate | 0% | 5.6% | 4B passes; 8B fails |
| Median latency | 7.62 s | 14.16 s | 4B is 1.86x faster |
| p95 latency | 8.51 s | 16.66 s | 4B is 1.96x faster |
| End-to-end mean latency | 7.63 s/image | 13.91 s/image | 4B is 1.82x faster |
| Modal score share | 13.9% | 14.7% | both below 15% |
| Score standard deviation | 1.42 | 2.05 | both have useful spread |
| Distinct numeric scores | 27 | 21 | 4B has more granularity |
| Local model size | 3.3 GB | 6.2 GB | 4B is smaller |

The 8B run produced four `invalid_schema` results even after the single allowed
retry. Visual inspection of the largest score disagreements also found cases where
8B labeled dated or unfinished rooms as `major` damage and assigned materially lower
scores than 4B. That observation is diagnostic rather than a substitute for labels;
with only 20 valid comparisons, neither model has a statistically meaningful accuracy
lead. The latency and validity regressions are large and operationally decisive,
however, so 8B is rejected as the current single-model production candidate.

A full visual audit corrected six clearly exterior or view-only images that had been
marked as interior. Three stored comparisons involving those images remain in the
append-only source for provenance but are excluded from label status and evaluation.
The separate Codex audit agreed with the stored human labels on 85.0% of 20 valid
pairs; 4B agreed with that audit on 92.5%, versus 85.3% for 8B on the 17 pairs it could
score. These AI judgments are diagnostic only and are never eligible for promotion.

The matching self-contained visual reports are generated local artifacts in this
workspace (they are intentionally not committed with the code):

- `data/vision_eval/reports/qwen3-vl-4b-instruct-v2-p3-tune.html`
- `data/vision_eval/reports/qwen3-vl-8b-instruct-v2-p3-tune.html`
- `data/vision_eval/reports/codex-visual-audit.md` (diagnostic, not promotion evidence)

## Visual verification and prompt corrections

The self-contained report embeds all 72 images beside the score, evidence attributes,
coverage, confidence, status, model tag, prompt version, and latency:

`data/vision_eval/reports/qwen3-vl-4b-instruct-v2-p3-tune.html`

Manual review of low-confidence and extreme-score cases exposed two real prompt faults:

1. An unfinished renovation received 10.0 because `renovation_signals=clear` was
   interpreted as visible activity instead of completed work.
2. Isolated wall holes were sometimes classified as major damage, while older intact
   tile was classified as modern.

Prompt p3 now defines completed versus unfinished renovation, anchors minor versus
major damage, and separates modern, dated-but-intact, and worn surfaces. Targeted
rechecks changed the false 3.85 room score to 6.0 and two borderline 10.0 scores to
9.0. The final 72-image run uses p3 and a separate cache namespace.

## Speed diagnosis

A profiled uncached request spent about 4.70 seconds evaluating roughly 1,259 prompt
tokens and 3.02 seconds generating 105 output tokens, for about 7.9 seconds total.
Reducing the image edge to 384 or 256 did not materially reduce visual-token count or
latency in this Ollama/Qwen renderer. Concurrency may improve throughput but cannot make
single-image p95 satisfy the 3-second gate, and is therefore not used to hide this
failure.

For capacity planning, uncached serial time is approximately:

`distinct images x 7.63 seconds`

For example, 1,000 listings averaging 15 distinct images would require roughly 31.8
hours for the first pass on this machine. There is no artificial image cap; perceptual
deduplication and the versioned cache make later passes much cheaper.

## Promotion blockers

After the interior-label audit, the current labeling set contains:

- 72 tune images, of which 66 are valid interiors, and 0 holdout images;
- 20 valid interior pairwise comparisons;
- 46 connected components;
- 65 interior images with fewer than three comparisons;
- 0 repeated judgments, below the required 20.

The preregistered human-label promotion gate is therefore still incomplete. On
2026-10-30, the operator nevertheless selected `qwen3-vl:4b-instruct` as the single
default runtime scorer based on its operational validity, speed, and the completed
visual audit. Results remain versioned by model, prompt, and schema; stale or failed
rows are retried and compatible existing V2 rows can be promoted without reinference.
This operational decision does not turn the incomplete holdout into accuracy evidence;
a valid holdout and better-connected repeated labels are still required before making a
stronger measured-accuracy claim.

`qwen3-vl:8b-instruct` is not a promotion candidate after the controlled comparison.
It can remain installed for future prompt research, but the production pipeline uses
only `qwen3-vl:4b-instruct`.

## Historical context

Earlier exploratory benchmarks mixed prompt shapes and, for Qwen, the wrong thinking
variant. They are useful for candidate discovery but are not comparable to the final V2
contract. The authoritative artifacts are the versioned p3 prediction JSON and HTML
report generated by `vision_eval.run_eval` and `vision_eval.report`.
