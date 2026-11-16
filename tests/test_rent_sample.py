import json

from greeceapt.photos import ai_conductor
from greeceapt.rent import store
from greeceapt.rent.sample import allocate, choose_sample, collapse_duplicates
from greeceapt.rent.vision import rentals_adapter
from greeceapt.vision.ollama import VisionConfig
from greeceapt.vision.types import ListingVisionResult


def _row(i, hood, **over):
    row = {
        "id": i, "area_sqm": 40.0, "rent_eur": 450.0 + i, "photos_count": 5,
        "floor_class": "upper", "floor_conflict": 0, "latitude": 37.97 + i * 0.001,
        "longitude": 23.73, "hood_l4": hood, "in_vision_sample": 0,
    }
    row.update(over)
    return row


def test_allocate_gives_floor_then_proportional_and_hits_size():
    quota = allocate({"a": 100, "b": 10, "c": 3}, 40, 8)
    assert quota["c"] == 3 and quota["b"] >= 8
    assert sum(quota.values()) == 40
    assert quota["a"] > quota["b"]


def test_collapse_duplicates_keeps_one_per_flat():
    a = _row(1, "x", latitude=37.9701, photos_count=3)
    b = _row(2, "x", latitude=37.97012, rent_eur=a["rent_eur"], photos_count=9)
    assert [r["id"] for r in collapse_duplicates([a, b])] == [2]


def test_choose_sample_filters_segment_and_keeps_previous():
    rows = [_row(i, "a") for i in range(1, 50)] + [_row(i, "b") for i in range(50, 60)]
    rows.append(_row(99, "a", area_sqm=120.0))          # outside segment
    rows.append(_row(98, "b", floor_class=None))         # unknown floor
    rows[0]["in_vision_sample"] = 1
    chosen = choose_sample(rows, 20, min_per=8)
    assert len(chosen) == 20
    assert 99 not in chosen and 98 not in chosen
    assert rows[0]["id"] in chosen
    assert sum(1 for s in chosen.values() if s == "b") >= 8


def test_vision_adapter_scores_only_sampled_rentals(tmp_path):
    db = tmp_path / "rentals.db"
    conn = store.connect(db)
    for i in range(3):
        store.upsert_records(conn, [{
            "url": f"u{i}", "rent_eur": 400.0, "area_sqm": 40.0,
            "photo_urls_json": json.dumps([f"https://img/{i}.jpg"]), "scraped_at": "now",
        }])
    conn.execute("UPDATE listings SET in_vision_sample = 1 WHERE url IN ('u0', 'u2')")
    conn.commit()
    conn.close()

    class Service:
        scorer = type("Scorer", (), {"config": VisionConfig()})()
        seen: list = []

        def score_listing(self, sources):
            self.seen.append(sources[0].source_url)
            return ListingVisionResult(condition_score=6.5, effective_confidence=0.9,
                                       status="accepted", accepted_count=1)

    service = Service()
    ai_conductor._run_layer1_v2_impl(rentals_adapter(db), service=service)
    assert sorted(service.seen) == ["https://img/0.jpg", "https://img/2.jpg"]
    conn = store.connect(db)
    scores = {r["url"]: r["visual_score_v2"] for r in store.all_rows(conn)}
    assert scores == {"u0": 6.5, "u1": None, "u2": 6.5}


def test_rebalance_keeps_scored_rows_and_matches_size_mix():
    from greeceapt.rent.sample import band_shares, rebalance, size_band

    def row(i, area, rent=600.0, scored=False):
        return {"id": i, "area_sqm": area, "rent_eur": rent, "photos_count": 5, "floor_class": "upper",
                "floor_conflict": 0, "latitude": 37.98 + i * 1e-3, "longitude": 23.73,
                "in_vision_sample": 1 if scored else 0, "visual_status_v2": "accepted" if scored else None}

    rows = [row(i, 65.0, scored=True) for i in range(10)]                 # scored, all large
    rows += [row(100 + i, 22.0 + (i % 7), rent=350.0 if i < 3 else 500.0) for i in range(30)]
    rows += [row(200 + i, 35.0) for i in range(40)]
    rows += [row(300 + i, 55.0) for i in range(40)]
    shares = band_shares([25.0] * 5 + [35.0] * 4 + [55.0])                  # 50 % / 40 % / 10 %
    chosen = rebalance(rows, 40, shares)
    assert len(chosen) == 40
    assert all(i in chosen for i in range(10))                             # scored work kept
    bands = [size_band(next(r for r in rows if r["id"] == i)["area_sqm"]) for i in chosen]
    # targets 20 / 16 / 4 exceed the 30 free slots (10 kept large rows) → scaled to 15 / 12 / 3
    assert (bands.count(0), bands.count(1), bands.count(3)) == (15, 12, 3)
    assert {100, 101, 102} <= set(chosen)                                   # cheap rentals first
