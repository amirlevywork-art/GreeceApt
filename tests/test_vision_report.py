"""Self-contained image-by-image vision report tests."""

import json
import inspect
from io import BytesIO

from PIL import Image

from greeceapt.evaluation.report import generate_report


def _jpeg(path, color):
    buffer = BytesIO()
    Image.new("RGB", (24, 16), color).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())


def test_report_generation_requires_an_explicit_eligible_comparison_set():
    parameter = inspect.signature(generate_report).parameters["comparison_ids"]

    assert parameter.default is inspect.Parameter.empty


def test_report_embeds_images_evidence_metrics_and_human_disagreements(tmp_path):
    first = tmp_path / "one.jpg"
    second = tmp_path / "two.jpg"
    _jpeg(first, (200, 80, 40))
    _jpeg(second, (40, 80, 200))
    prediction = tmp_path / "predictions.json"
    prediction.write_text(
        json.dumps(
            {
                "metadata": {"split": "tune", "model": "qwen3-vl:4b-instruct"},
                "metrics": {"valid_rate": 1.0, "latency_p95_ms": 2200},
                "predictions": {
                    "one": {
                        "image_path": str(first),
                        "score": 8.4,
                        "room_type": "living<script>alert(1)</script>",
                        "status": "accepted",
                        "evidence_coverage": 0.91,
                        "effective_confidence": 0.88,
                        "latency_ms": 1200,
                        "attempts": 1,
                        "evidence": {
                            "visible_damage": "none",
                            "wall_condition": "fresh",
                            "flooring_condition": "modern",
                        },
                    },
                    "two": {
                        "image_path": str(second),
                        "score": 3.2,
                        "room_type": "bathroom",
                        "status": "accepted",
                        "evidence_coverage": 0.72,
                        "effective_confidence": 0.7,
                        "latency_ms": 2200,
                        "attempts": 2,
                        "evidence": {
                            "visible_damage": "minor",
                            "bathroom_age": "original",
                        },
                    },
                },
                "comparisons": [
                    {"a_id": "one", "b_id": "two", "winner": "b"}
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.html"

    generate_report(
        prediction,
        split="tune",
        output=output,
        comparison_ids={"one", "two"},
    )
    html = output.read_text(encoding="utf-8")

    assert html.count("data:image/jpeg;base64,") == 2
    assert "8.4" in html and "3.2" in html
    assert "visible_damage" in html and "wall_condition" in html
    assert "Confidence" in html and "Status" in html
    assert "1200 ms" in html and "Attempts: 2" in html
    assert "Human comparisons" in html
    assert "Top disagreements" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "living<script>alert(1)</script>" not in html
    assert "filter-status" in html and "filter-room" in html


def test_report_excludes_comparisons_outside_the_eligible_image_set(tmp_path):
    prediction = tmp_path / "predictions.json"
    prediction.write_text(
        json.dumps(
            {
                "metadata": {"split": "tune"},
                "predictions": {
                    "one": {"score": 8.0, "status": "accepted"},
                    "outside": {"score": 2.0, "status": "accepted"},
                },
                "comparisons": [
                    {"a_id": "one", "b_id": "outside", "winner": "b"}
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.html"

    generate_report(
        prediction,
        split="tune",
        output=output,
        comparison_ids={"one"},
    )

    html = output.read_text(encoding="utf-8")
    assert "No scored disagreements in this split." in html
    assert "0W / 0L / 0T" in html
