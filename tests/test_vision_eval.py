"""Tests for the vision evaluation harness (Bradley-Terry, metrics)."""

import math
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from greeceapt.evaluation.bradley_terry import (
    Comparison,
    connected_components,
    fit_bradley_terry,
)
from greeceapt.evaluation import metrics
from greeceapt.vision.ollama import VisionInference
from greeceapt.vision.scoring import score_verdict
from greeceapt.vision.types import ImageVerdict


def _ladder(items, repeats=3):
    """Every adjacent pair compared, lower index always 'loses' → known total order."""
    comps = []
    for _ in range(repeats):
        for i in range(len(items) - 1):
            # items[i+1] beats items[i]
            comps.append(Comparison(items[i], items[i + 1], "b"))
    return comps


# ── Bradley-Terry ─────────────────────────────────────────────────────────────

def test_bt_recovers_monotone_order():
    items = ["a", "b", "c", "d", "e"]
    theta = fit_bradley_terry(_ladder(items))
    ordered = sorted(items, key=lambda i: theta[i])
    assert ordered == items, f"expected ascending strength, got {ordered}"


def test_bt_empty_is_empty():
    assert fit_bradley_terry([]) == {}


def test_bt_item_that_never_wins_is_finite_and_lowest():
    # 'loser' loses every game; regularisation must keep theta finite.
    comps = [Comparison("loser", x, "b") for x in ("a", "b", "c") for _ in range(3)]
    theta = fit_bradley_terry(comps)
    assert math.isfinite(theta["loser"])
    assert theta["loser"] == min(theta.values())


def test_bt_ties_sit_between():
    comps = [Comparison("a", "b", "=")] * 5
    theta = fit_bradley_terry(comps)
    assert abs(theta["a"] - theta["b"]) < 1e-6


def test_bt_theta_mean_centred():
    theta = fit_bradley_terry(_ladder(["a", "b", "c"]))
    assert abs(sum(theta.values())) < 1e-6


# ── Connected components ──────────────────────────────────────────────────────

def test_single_component_when_connected():
    comps = _ladder(["a", "b", "c", "d"])
    assert len(connected_components(comps)) == 1


def test_two_disconnected_clusters_detected():
    comps = [Comparison("a", "b", "a"), Comparison("c", "d", "a")]
    comp = connected_components(comps)
    assert len(comp) == 2


def test_no_comparisons_no_components():
    assert connected_components([]) == []


# ── Pairwise agreement ────────────────────────────────────────────────────────

def test_perfect_predictor_scores_one():
    comps = _ladder(["a", "b", "c", "d"])
    pred = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}  # matches "later index wins"
    point, lo, hi = metrics.pairwise_agreement(pred, comps, bootstrap=500)
    assert point == 1.0
    assert lo <= 1.0 <= hi + 1e-9


def test_reversed_predictor_scores_zero():
    comps = _ladder(["a", "b", "c", "d"])
    pred = {"a": 4.0, "b": 3.0, "c": 2.0, "d": 1.0}
    point, _, _ = metrics.pairwise_agreement(pred, comps, bootstrap=0)
    assert point == 0.0


def test_constant_predictor_scores_half():
    comps = _ladder(["a", "b", "c"])
    pred = {"a": 5.0, "b": 5.0, "c": 5.0}
    point, _, _ = metrics.pairwise_agreement(pred, comps, bootstrap=0)
    assert point == 0.5


def test_agreement_ci_brackets_point():
    comps = _ladder(["a", "b", "c", "d", "e"], repeats=5)
    pred = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 4.0}  # one tie -> imperfect
    point, lo, hi = metrics.pairwise_agreement(pred, comps, bootstrap=1000)
    assert lo <= point <= hi


def test_clustered_bootstrap_samples_listing_clusters_not_rows():
    comps = [
        Comparison("a1", "b1", "a"),
        Comparison("a2", "b2", "b"),
    ]
    pred = {"a1": 9.0, "b1": 2.0, "a2": 9.0, "b2": 2.0}
    clusters = {"a1": "listing-a", "b1": "listing-b", "a2": "listing-c", "b2": "listing-d"}

    base = metrics.pairwise_agreement_clustered(
        pred, comps, cluster_by_id=clusters, bootstrap=200, seed=4
    )
    duplicated = metrics.pairwise_agreement_clustered(
        pred, comps * 20, cluster_by_id=clusters, bootstrap=200, seed=4
    )

    assert duplicated == base


# ── Self-consistency ──────────────────────────────────────────────────────────

def test_self_consistency_all_agree():
    comps = [Comparison("a", "b", "a"), Comparison("a", "b", "a")]
    cons, n = metrics.self_consistency(comps)
    assert cons == 1.0 and n == 1


def test_self_consistency_flip_is_zero():
    # first: a beat b.  second: same pair, b beat a  -> a genuine contradiction.
    comps = [Comparison("a", "b", "a"), Comparison("a", "b", "b")]
    cons, n = metrics.self_consistency(comps)
    assert n == 1 and cons == 0.0


def test_self_consistency_no_repeats_returns_one():
    cons, n = metrics.self_consistency([Comparison("a", "b", "a")])
    assert n == 0 and cons == 1.0


# ── Spearman ──────────────────────────────────────────────────────────────────

def test_spearman_perfect():
    assert abs(metrics.spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9


def test_spearman_reversed():
    assert abs(metrics.spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-9


# ── Collapse stats ────────────────────────────────────────────────────────────

def test_collapse_constant_is_modal_one():
    st = metrics.collapse_stats([5.0] * 10)
    assert st["modal_share"] == 1.0
    assert st["n_distinct"] == 1.0
    assert st["stdev"] == 0.0


def test_collapse_spread_has_low_modal_share():
    st = metrics.collapse_stats([float(i) for i in range(10)])
    assert st["modal_share"] == 0.1
    assert st["n_distinct"] == 10.0
    assert st["stdev"] > 0


def test_collapse_matches_old_layer1_shape():
    # 46% on 5.0 like the real broken output
    scores = [5.0] * 46 + [5.3] * 22 + [7.0] * 15 + [4.5] * 17
    st = metrics.collapse_stats(scores)
    assert abs(st["modal_share"] - 0.46) < 0.01
    assert st["n_distinct"] == 4.0


def test_prediction_stats_use_individual_timings_and_rates():
    predictions = [
        {"score": 8.0, "status": "accepted", "latency_ms": 1000, "attempts": 1},
        {"score": None, "status": "model_error", "latency_ms": 2000, "attempts": 2},
        {"score": None, "status": "insufficient_evidence", "latency_ms": 3000, "attempts": 1},
        {"score": 6.0, "status": "accepted", "latency_ms": 4000, "attempts": 1},
    ]

    stats = metrics.prediction_stats(predictions)

    assert stats["latency_p50_ms"] == 2500
    assert stats["latency_p95_ms"] == 3850
    assert stats["latency_mean_ms"] == 2500
    assert stats["valid_rate"] == 0.75
    assert stats["null_rate"] == 0.5
    assert stats["retry_rate"] == 0.25


def _shared_inference() -> VisionInference:
    verdict = ImageVerdict(
        room_type="living",
        is_interior=True,
        visible_damage="none",
        wall_condition="fresh",
        flooring_condition="modern",
        kitchen_age="not_applicable",
        bathroom_age="not_applicable",
        windows_age="modern",
        renovation_signals="clear",
        image_quality="good",
        confidence=0.9,
    )
    return VisionInference(
        verdict=verdict,
        score=score_verdict(verdict),
        attempt_count=1,
        attempt_latencies_ms=(123.0,),
        raw_response_sha256="a" * 64,
        error_category=None,
        diagnostic_excerpt=None,
        model_tag="qwen3-vl:4b-instruct",
    )


def test_v2_delegates_to_shared_ollama_scorer_and_returns_provenance(tmp_path):
    from greeceapt.evaluation.scorers import ScorerConfig, score_image_detailed

    image_path = tmp_path / "room.jpg"
    buffer = BytesIO()
    Image.new("RGB", (30, 20), (120, 90, 60)).save(buffer, format="JPEG")
    image_path.write_bytes(buffer.getvalue())

    class FakeScorer:
        def __init__(self):
            self.calls = []

        def score(self, content):
            self.calls.append(content)
            return _shared_inference()

    scorer = FakeScorer()
    prediction = score_image_detailed(
        str(image_path), ScorerConfig(), scorer=scorer
    )

    assert len(scorer.calls) == 1
    assert prediction["score"] == 9.86
    assert prediction["status"] == "accepted"
    assert prediction["evidence_coverage"] == 1.0
    assert prediction["effective_confidence"] > 0.9
    assert prediction["latency_ms"] == 123.0
    assert prediction["attempts"] == 1
    assert prediction["evidence"]["room_type"] == "living"


def test_versioned_prediction_filename_contains_all_contract_versions(tmp_path):
    from greeceapt.evaluation.run_eval import prediction_path
    from greeceapt.evaluation.scorers import ScorerConfig

    path = prediction_path(ScorerConfig(), "tune", directory=tmp_path)

    assert "qwen3-vl-4b-instruct" in path.name
    assert "condition-v2-p3" in path.name
    assert "condition-v2" in path.name
    assert "512" in path.name
    assert "tune" in path.name


def _write_eval_image(path: Path, *, quality=90, color=(120, 90, 60)):
    image = Image.new("RGB", (48, 32), color)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    path.write_bytes(buffer.getvalue())


def test_validate_golden_detects_split_and_duplicate_leakage(tmp_path):
    from greeceapt.evaluation.golden_set import validate_golden

    _write_eval_image(tmp_path / "a.jpg", quality=95)
    _write_eval_image(tmp_path / "b.jpg", quality=65)
    rows = [
        {"id": "a", "sha256": "a", "listing_id": "listing-1", "split": "tune", "interior": True},
        {"id": "b", "sha256": "b", "listing_id": "listing-1", "split": "holdout", "interior": True},
        {"id": "c", "sha256": "a", "listing_id": "listing-2", "split": "holdout", "interior": True},
    ]

    issues = validate_golden(rows, images_dir=tmp_path)
    codes = {issue.code for issue in issues}

    assert "listing_split_leakage" in codes
    assert "sha_split_leakage" in codes
    assert "phash_split_leakage" in codes


def test_validate_golden_reports_missing_file_and_invalid_metadata(tmp_path):
    from greeceapt.evaluation.golden_set import validate_golden

    rows = [
        {"id": "bad", "sha256": "missing", "listing_id": "1", "split": "dev", "interior": "yes"}
    ]

    issues = validate_golden(rows, images_dir=tmp_path)
    codes = {issue.code for issue in issues}

    assert codes == {"missing_image", "invalid_split", "invalid_interior"}


def test_holdout_requires_human_room_and_quality_labels(tmp_path):
    from greeceapt.evaluation.golden_set import validate_golden

    _write_eval_image(tmp_path / "a.jpg")
    rows = [
        {
            "id": "a",
            "sha256": "a",
            "listing_id": "listing-1",
            "split": "holdout",
            "interior": True,
        }
    ]

    codes = {issue.code for issue in validate_golden(rows, images_dir=tmp_path)}

    assert "missing_room_label" in codes
    assert "missing_quality_label" in codes


def test_holdout_still_requires_explicit_promote_flag():
    from greeceapt.evaluation.run_eval import main

    with pytest.raises(SystemExit, match="holdout"):
        main(["--split", "holdout", "--strategy", "V2"])


def _passing_gate_row():
    return {
        "n_images": 48,
        "n_listings": 48,
        "repeat": 3,
        "agreement_lo": 0.85,
        "agreement": 0.9,
        "self_consistency": 1.0,
        "modal_share": 0.1,
        "stdev": 1.3,
        "latency_p95_ms": 2500,
        "latency_mean_ms": 2400,
        "valid_rate": 0.98,
        "retry_rate": 0.02,
        "max_repeat_stdev": 0.1,
    }


def _complete_labels():
    return {
        "connected_complete": True,
        "coverage_complete": True,
        "repeats_complete": True,
        "consistency_pairs": 20,
    }


def test_promotion_latency_gate_uses_p95_not_fast_mean():
    from greeceapt.evaluation.run_eval import check_promotion_gates

    row = {**_passing_gate_row(), "sec_per_img": 1.0, "latency_p95_ms": 4000}
    gates = check_promotion_gates(row, _complete_labels())

    assert gates["latency_p95"] is False


def test_promotion_cannot_pass_without_twenty_repeat_judgments():
    from greeceapt.evaluation.run_eval import check_promotion_gates

    labels = {**_complete_labels(), "repeats_complete": False, "consistency_pairs": 19}
    gates = check_promotion_gates(_passing_gate_row(), labels)

    assert gates["human_repeats"] is False
    assert not all(gates.values())


def test_sparse_room_slices_do_not_create_independent_failure_gates():
    from greeceapt.evaluation.run_eval import check_promotion_gates

    row = {**_passing_gate_row(), "critical_slices": {"bathroom": {"n_comparisons": 1, "agreement": 0.0}}}
    gates = check_promotion_gates(row, _complete_labels())

    assert all(gates.values())


def test_well_sampled_critical_slice_can_fail_promotion():
    from greeceapt.evaluation.run_eval import check_promotion_gates

    row = {
        **_passing_gate_row(),
        "critical_slices": {
            "bathroom": {"n_comparisons": 20, "agreement": 0.79}
        },
    }

    assert check_promotion_gates(row, _complete_labels())["critical_slices"] is False


@pytest.mark.parametrize(
    ("override", "gate"),
    [
        ({"latency_mean_ms": 2501}, "latency_mean"),
        ({"n_listings": 47}, "holdout_listings"),
        ({"repeat": 1, "max_repeat_stdev": 0.0}, "repeat_stability"),
    ],
)
def test_promotion_requires_mean_latency_unique_listings_and_real_repeats(override, gate):
    from greeceapt.evaluation.run_eval import check_promotion_gates

    gates = check_promotion_gates({**_passing_gate_row(), **override}, _complete_labels())

    assert gates[gate] is False
    assert not all(gates.values())


def test_check_gates_requires_locked_holdout_and_three_repeats():
    from greeceapt.evaluation.run_eval import main

    with pytest.raises(SystemExit, match="holdout"):
        main(["--check-gates", "--repeat", "3"])
    with pytest.raises(SystemExit, match="repeat"):
        main(["--check-gates", "--split", "holdout", "--promote"])


def test_holdout_rejects_partial_or_repeated_candidate_run(tmp_path):
    from greeceapt.evaluation.run_eval import guard_holdout_run

    lock = tmp_path / "candidate.holdout.lock"
    with pytest.raises(SystemExit, match="limit"):
        guard_holdout_run(split="holdout", limit=5, lock_path=lock)

    old_artifact = tmp_path / "candidate.holdout__repeat-1.json"
    old_artifact.write_text("legacy run")
    with pytest.raises(SystemExit, match="already"):
        guard_holdout_run(split="holdout", limit=None, lock_path=lock)
    old_artifact.unlink()

    guard_holdout_run(split="holdout", limit=None, lock_path=lock)
    with pytest.raises(SystemExit, match="already"):
        guard_holdout_run(split="holdout", limit=None, lock_path=lock)


def test_any_holdout_access_requires_full_gate_protocol():
    from greeceapt.evaluation.run_eval import main

    with pytest.raises(SystemExit, match="check-gates"):
        main(["--split", "holdout", "--promote", "--repeat", "3"])


def test_repeat_count_is_number_of_extra_judgments_not_combinations():
    comparisons = [Comparison("a", "b", "a") for _ in range(7)]

    consistency, repeats = metrics.self_consistency(comparisons)

    assert consistency == 1.0
    assert repeats == 6


def test_label_status_names_all_three_human_completion_requirements():
    from greeceapt.evaluation.label import label_status

    comparisons = [Comparison("a", "b", "a")]
    status = label_status(comparisons, ["a", "b", "c"])

    assert status["connected_complete"] is False
    assert status["coverage_complete"] is False
    assert status["repeats_complete"] is False
    assert status["ready"] is False


def test_label_status_ignores_comparisons_outside_the_selected_images():
    from greeceapt.evaluation.label import label_status

    comparisons = [
        Comparison("a", "b", "a"),
        Comparison("outside-a", "outside-b", "outside-a"),
        Comparison("a", "outside-a", "a"),
    ]

    status = label_status(comparisons, ["a", "b"])

    assert status["comparisons"] == 1
    assert status["n_components"] == 1
    assert set(status["theta"]) == {"a", "b"}


# ── Labelling pair selection ──────────────────────────────────────────────────

def test_pair_selection_connects_graph_first():
    from greeceapt.evaluation import label
    import random as _r
    ids = ["a", "b", "c", "d"]
    a, b, phase = label.choose_pair(ids, set(), {i: 0 for i in ids}, {}, _r.Random(0))
    assert phase == "connect"
    assert a in ids and b in ids and a != b


def test_pair_selection_covers_before_uncertainty():
    from greeceapt.evaluation import label
    import random as _r
    ids = ["a", "b", "c", "d"]
    # graph already connected via a chain, but c,d still under-covered
    judged = {("a", "b"), ("b", "c"), ("c", "d")}
    counts = {"a": 5, "b": 5, "c": 1, "d": 1}
    a, b, phase = label.choose_pair(ids, judged, counts, {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}, _r.Random(0))
    assert phase in ("cover", "repeat")
    if phase == "cover":
        assert "c" in (a, b) or "d" in (a, b)


def test_pair_selection_uncertainty_picks_close_theta():
    from greeceapt.evaluation import label
    import random as _r
    ids = ["a", "b", "c"]
    judged = {("a", "b"), ("b", "c"), ("a", "c")}  # fully connected & covered
    counts = {"a": 9, "b": 9, "c": 9}
    # make it choose among unjudged — but all judged, so it falls to repeat
    a, b, phase = label.choose_pair(ids, judged, counts, {"a": 0.0, "b": 0.01, "c": 5.0}, _r.Random(0))
    assert phase in ("uncertain", "repeat")
