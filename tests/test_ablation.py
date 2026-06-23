"""news_backtest.ablation — mekanik gate katkı ölçümü (saf)."""

from __future__ import annotations

from news_backtest import _directional_24h, ablation


def _res(net, *, impact=8, confirmed=None, rvol=None, p24=None, direction="bullish"):
    """Simüle edilmiş bir sonuç dict'i (ablation'ın beklediği biçim)."""
    return {
        "net_pct": net, "outcome": "tp" if net > 0 else "sl",
        "impact": impact, "direction": direction, "source": "X",
        "confirmed": confirmed, "rel_volume": rvol, "price_24h_pct": p24,
    }


# ── _directional_24h ─────────────────────────────────────────────────────
def test_directional_24h_none_when_missing():
    assert _directional_24h({"direction": "bullish"}) is None


def test_directional_24h_sign_by_direction():
    assert _directional_24h({"direction": "bullish", "price_24h_pct": 8.0}) == 8.0
    assert _directional_24h({"direction": "bearish", "price_24h_pct": 8.0}) == -8.0


# ── ablation: yetersiz veri ──────────────────────────────────────────────
def test_insufficient_data_marks_gate():
    res = [_res(1.0) for _ in range(3)]   # < min_subset (5)
    out = ablation(res)
    impact_gate = next(g for g in out["gates"] if g["gate"].startswith("impact"))
    assert impact_gate["status"] == "yetersiz-veri"


# ── ablation: confirmed gate kaybedeni eliyor ────────────────────────────
def test_confirmed_gate_pays_when_unconfirmed_loses():
    # Teyitli=kazanç, teyitsiz=zarar → gate işe yaramalı
    res = [_res(4.0, confirmed=True) for _ in range(6)]
    res += [_res(-3.0, confirmed=False) for _ in range(6)]
    out = ablation(res)
    g = next(x for x in out["gates"] if x["gate"] == "confirmed")
    assert g["status"] == "işe-yarar"
    assert g["removed"]["avg_net_pct"] < 0    # bloklananlar kaybeden
    assert g["kept"]["avg_net_pct"] > 0
    assert g["delta_avg_pct"] > 0


def test_confirmed_gate_neutral_when_removed_winners():
    # Teyitsizler de kazanıyorsa gate kazananı eliyor = işe yaramaz
    res = [_res(2.0, confirmed=True) for _ in range(6)]
    res += [_res(5.0, confirmed=False) for _ in range(6)]
    out = ablation(res)
    g = next(x for x in out["gates"] if x["gate"] == "confirmed")
    assert g["status"] != "işe-yarar"
    assert g["removed"]["avg_net_pct"] > 0     # bloklananlar kazanıyordu


# ── ablation: rvol gate alt-küme bölünmesi ───────────────────────────────
def test_rvol_gate_splits_on_threshold():
    res = [_res(3.0, rvol=2.5) for _ in range(6)]
    res += [_res(-2.0, rvol=0.5) for _ in range(6)]
    out = ablation(res, rvol_min=1.5)
    g = next(x for x in out["gates"] if x["gate"].startswith("rvol"))
    assert g["status"] == "işe-yarar"


def test_baseline_present():
    res = [_res(1.0, confirmed=True) for _ in range(6)]
    out = ablation(res)
    assert out["baseline"]["n"] == 6
    assert "base_avg_net_pct" in out
