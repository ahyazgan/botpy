"""Konsolide değerlendirme: _assess öncelik mantığı + /report endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

import news_bot as nb


def _assess(**kw):
    base = dict(ops_critical=0, ops_warn=0, readiness_verdict="UMUT VERİCİ — ...",
                complexity={"premature": []}, risk_of_ruin=0.0, mc_reliable=True, n_trades=50)
    base.update(kw)
    return nb._assess(**base)


# ── Öncelik sırası (en yüksek blokaj önce) ───────────────────────────────
def test_ops_critical_wins_everything():
    # ops kritik varken her şeyin önünde
    out = _assess(ops_critical=2, readiness_verdict="VERİ YETERSİZ — ...",
                  complexity={"premature": ["Kelly"]}, risk_of_ruin=50.0)
    assert out["status"] == "ops_unsafe"
    assert "GÜVENLİĞİ" in out["verdict"]


def test_gather_data_when_insufficient():
    out = _assess(readiness_verdict="VERİ YETERSİZ — paper'da devam")
    assert out["status"] == "gather_data"
    assert any("paper" in a.lower() for a in out["actions"])
    assert any("import_history" in a for a in out["actions"])


def test_prune_when_premature():
    out = _assess(readiness_verdict="GELİŞİYOR — ...", complexity={"premature": ["Kelly (≥20)"]})
    assert out["status"] == "prune_complexity"
    assert any("Kelly" in a for a in out["actions"])


def test_risk_high_blocks():
    out = _assess(readiness_verdict="GELİŞİYOR — ...", risk_of_ruin=25.0, mc_reliable=True)
    assert out["status"] == "risk_high"
    assert "iflas" in out["verdict"].lower()


def test_risk_ignored_when_unreliable():
    # az örnek (reliable=False) → risk verdikti tetiklenmez
    out = _assess(readiness_verdict="GELİŞİYOR — ...", risk_of_ruin=99.0, mc_reliable=False)
    assert out["status"] == "developing"


def test_edge_weak():
    out = _assess(readiness_verdict="HENÜZ DEĞİL — ayarla")
    assert out["status"] == "edge_weak"
    assert any("ablation" in a or "tuning" in a for a in out["actions"])


def test_ready_when_all_green():
    out = _assess(readiness_verdict="UMUT VERİCİ — ...", ops_critical=0,
                  complexity={"premature": []}, risk_of_ruin=2.0)
    assert out["status"] == "ready"
    assert "CANLI" in out["verdict"]


def test_warn_appended_as_action():
    out = _assess(readiness_verdict="UMUT VERİCİ — ...", ops_warn=3)
    assert any("uyarı" in a for a in out["actions"])


# ── /report endpoint ─────────────────────────────────────────────────────
def test_report_endpoint_shape():
    d = TestClient(nb.app).get("/report").json()
    assert "verdict" in d and "status" in d and "actions" in d
    comp = d["components"]
    assert set(comp) == {"edge", "safety", "complexity", "risk", "performance"}
