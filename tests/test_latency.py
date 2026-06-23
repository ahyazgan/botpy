"""latency.LatencyTracker + percentile/özet + flatten testleri."""

from __future__ import annotations

import pytest

from latency import LatencyTracker, _percentile, _stats, flatten_metrics


# ── _percentile ──────────────────────────────────────────────────────────
def test_percentile_empty():
    assert _percentile([], 0.5) == 0.0


def test_percentile_single():
    assert _percentile([42.0], 0.95) == 42.0


def test_percentile_median_and_p95():
    vals = [float(i) for i in range(1, 101)]  # 1..100
    assert _percentile(vals, 0.50) == pytest.approx(50.5)
    assert _percentile(vals, 0.95) == pytest.approx(95.05)
    assert _percentile(vals, 1.0) == pytest.approx(100.0)


# ── _stats ───────────────────────────────────────────────────────────────
def test_stats_summary_keys():
    s = _stats([10.0, 20.0, 30.0])
    assert s["count"] == 3
    assert s["avg_ms"] == pytest.approx(20.0)
    assert s["p50_ms"] == pytest.approx(20.0)
    assert s["max_ms"] == pytest.approx(30.0)
    assert s["last_ms"] == pytest.approx(30.0)   # son eklenen (sıralanmamış)


# ── LatencyTracker ───────────────────────────────────────────────────────
def test_record_and_summary():
    t = LatencyTracker()
    for v in (100.0, 200.0, 300.0):
        t.record("pipeline", v)
    s = t.summary()
    assert set(s) == {"pipeline"}
    assert s["pipeline"]["count"] == 3
    assert s["pipeline"]["max_ms"] == pytest.approx(300.0)


def test_negative_and_none_ignored():
    t = LatencyTracker()
    t.record("ingest", -5.0)   # saat kayması → atla
    t.record("ingest", None)   # bilinmiyor → atla
    t.record("ingest", 12.0)
    s = t.summary()
    assert s["ingest"]["count"] == 1


def test_rolling_window_caps_samples():
    t = LatencyTracker(maxlen=3)
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        t.record("order", v)
    s = t.summary()
    assert s["order"]["count"] == 3          # yalnız son 3
    assert s["order"]["last_ms"] == pytest.approx(5.0)


def test_empty_stage_omitted():
    t = LatencyTracker()
    assert t.summary() == {}


def test_reset_clears():
    t = LatencyTracker()
    t.record("score", 50.0)
    t.reset()
    assert t.summary() == {}


# ── flatten_metrics ──────────────────────────────────────────────────────
def test_flatten_metrics_shape():
    t = LatencyTracker()
    for v in (10.0, 20.0, 30.0):
        t.record("pipeline", v)
    flat = flatten_metrics(t.summary())
    assert "botpy_latency_pipeline_p50_ms" in flat
    assert "botpy_latency_pipeline_p95_ms" in flat
    assert "botpy_latency_pipeline_max_ms" in flat
    assert "botpy_latency_pipeline_count" in flat
    assert flat["botpy_latency_pipeline_count"] == 3
    # avg/last düz metriğe dahil değil (kardinaliteyi düşük tut)
    assert "botpy_latency_pipeline_avg_ms" not in flat


# ── /latency + /metrics entegrasyonu ─────────────────────────────────────
def test_latency_endpoint_and_metrics():
    from fastapi.testclient import TestClient

    import latency as lat
    import news_bot as nb

    lat.reset()
    lat.record("pipeline", 250.0)
    lat.record("ingest", 80.0)
    client = TestClient(nb.app)

    rep = client.get("/latency").json()
    assert "pipeline" in rep["stages"]
    assert rep["stages"]["pipeline"]["count"] == 1

    text = client.get("/metrics").text
    assert "botpy_latency_pipeline_p50_ms" in text
    assert "botpy_latency_ingest_count" in text
    lat.reset()

