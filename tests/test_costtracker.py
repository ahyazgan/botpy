"""costtracker.CostTracker + /cost endpoint + usage kaydı."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import costtracker as ct
import news_bot as nb
from costtracker import CostTracker

PRICING = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0)}


# ── CostTracker ──────────────────────────────────────────────────────────
def test_record_and_summary():
    t = CostTracker()
    t.record("scoring", "claude-haiku-4-5", 1000, 200)
    t.record("scoring", "claude-haiku-4-5", 2000, 100)
    s = t.summary(PRICING)
    assert s["totals"]["calls"] == 2
    assert s["totals"]["input_tokens"] == 3000
    assert s["totals"]["output_tokens"] == 300
    # maliyet: 3000/1e6*1 + 300/1e6*5 = 0.003 + 0.0015 = 0.0045
    assert s["totals"]["est_cost_usd"] == pytest.approx(0.0045)


def test_by_category_and_model():
    t = CostTracker()
    t.record("scoring", "claude-haiku-4-5", 1000, 100)
    t.record("entry_brain", "claude-sonnet-4-6", 600, 60)
    s = t.summary(PRICING)
    assert set(s["by_category"]) == {"scoring", "entry_brain"}
    assert len(s["by_key"]) == 2
    brain = next(k for k in s["by_key"] if k["category"] == "entry_brain")
    assert brain["model"] == "claude-sonnet-4-6"
    # 600/1e6*3 + 60/1e6*15 = 0.0018 + 0.0009 = 0.0027
    assert brain["est_cost_usd"] == pytest.approx(0.0027)


def test_unknown_model_zero_cost():
    t = CostTracker()
    t.record("scoring", "mystery-model", 1_000_000, 1_000_000)
    s = t.summary(PRICING)
    assert s["totals"]["calls"] == 1
    assert s["totals"]["est_cost_usd"] == 0.0   # fiyat yok → 0, token yine sayılır


def test_negative_tokens_clamped():
    t = CostTracker()
    t.record("scoring", "claude-haiku-4-5", -5, None)  # type: ignore[arg-type]
    s = t.summary(PRICING)
    assert s["totals"]["input_tokens"] == 0
    assert s["totals"]["output_tokens"] == 0


def test_reset():
    t = CostTracker()
    t.record("scoring", "claude-haiku-4-5", 100, 100)
    t.reset()
    assert t.summary(PRICING)["totals"]["calls"] == 0


# ── _record_claude_usage (yanıt usage çıkarımı) ──────────────────────────
class _Usage:
    input_tokens = 1200
    output_tokens = 300


class _Resp:
    usage = _Usage()


def test_record_claude_usage_from_response(monkeypatch):
    ct.reset()
    nb._record_claude_usage("scoring", "claude-haiku-4-5", _Resp())
    s = ct.summary(nb.CLAUDE_PRICING)
    assert s["totals"]["calls"] == 1
    assert s["totals"]["input_tokens"] == 1200
    ct.reset()


def test_record_claude_usage_no_usage_safe():
    ct.reset()
    nb._record_claude_usage("scoring", "claude-haiku-4-5", object())  # usage yok → sessiz
    assert ct.summary()["totals"]["calls"] == 0


# ── /cost endpoint ───────────────────────────────────────────────────────
def test_cost_endpoint():
    ct.reset()
    ct.record("entry_brain", "claude-haiku-4-5", 1000, 500)
    d = TestClient(nb.app).get("/cost").json()
    assert d["totals"]["calls"] == 1
    assert "projected_daily_usd" in d
    assert "entry_brain" in d["by_category"]
    ct.reset()


def test_metrics_includes_claude_cost():
    ct.reset()
    ct.record("scoring", "claude-haiku-4-5", 1000, 100)
    text = TestClient(nb.app).get("/metrics").text
    assert "botpy_claude_calls_total" in text
    assert "botpy_claude_cost_usd_total" in text
    ct.reset()
