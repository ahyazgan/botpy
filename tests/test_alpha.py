"""Alpha analizi: haber kategorisi sınıflama + kategori/kaynak kırılımı."""

from __future__ import annotations

import pytest

from news_backtest import _categorize, alpha_analysis


# ── _categorize ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,expected", [
    ("Binance exchange suffers major hack, $200M drained", "hack"),
    ("SEC approves spot Bitcoin ETF", "etf"),
    ("Coinbase will list new token XYZ", "listing"),
    ("Project announces partnership with Visa", "partnership"),
    ("Binance to delist 5 trading pairs", "delisting"),
    ("BlackRock files for institutional product", "institutional"),
    ("SOL price surges to all-time high", "rally"),
    ("Market plunges as liquidations spike", "crash"),
    ("Fed signals interest rate decision", "macro"),
    ("Some random neutral headline about nothing", "other"),
])
def test_categorize(title, expected):
    assert _categorize(title) == expected


def test_categorize_priority_hack_over_rally():
    # Hem hack hem pump geçse hack (daha spesifik/güçlü) kazanır
    assert _categorize("Token pumps after hack rumor denied") == "hack"


# ── alpha_analysis ───────────────────────────────────────────────────────
def _res(net, title, *, direction="bullish", source="X", move=None):
    candles = None
    if move is not None:
        # entry=candles[0][1]=100, last=candles[-1][4]; _directional_move ≥2 mum ister
        last = 100 * (1 + (move if direction == "bullish" else -move) / 100)
        candles = [[0, 100.0, 100.0, 100.0, 100.0], [0, 100.0, last, last, last]]
    r = {"net_pct": net, "outcome": "tp" if net > 0 else "sl", "title": title,
         "direction": direction, "source": source}
    if candles:
        r["candles"] = candles
    return r


def test_groups_by_category():
    res = [_res(4.0, "huge hack drains funds", move=5.0) for _ in range(4)]
    res += [_res(-2.0, "minor partnership news", move=-1.0) for _ in range(4)]
    out = alpha_analysis(res, min_n=3)
    assert "hack" in out["by_category"]
    assert "partnership" in out["by_category"]
    assert out["by_category"]["hack"]["avg_net_pct"] > 0
    assert out["by_category"]["partnership"]["avg_net_pct"] < 0


def test_best_worst_category():
    res = [_res(5.0, "etf approved", move=6.0) for _ in range(4)]
    res += [_res(-3.0, "lawsuit filed against exchange", move=-4.0) for _ in range(4)]
    out = alpha_analysis(res, min_n=3)
    assert out["best"] == "etf"
    assert out["worst"] == "legal"


def test_thin_flag():
    res = [_res(3.0, "etf approved", move=2.0) for _ in range(2)]   # < min_n
    out = alpha_analysis(res, min_n=3)
    assert out["by_category"]["etf"]["thin"] is True
    # thin gruplar best/worst sıralamasına girmez
    assert out["best"] is None


def test_directional_stats():
    # 3 isabet (move>0), 1 kaçış → hit_rate %75
    res = [_res(1.0, "etf news", move=2.0) for _ in range(3)]
    res += [_res(-1.0, "etf news", direction="bullish", move=-2.0)]
    out = alpha_analysis(res, min_n=3)
    etf = out["by_category"]["etf"]
    assert etf["hit_rate"] == pytest.approx(75.0)
    assert etf["n"] == 4


def test_by_source_present():
    res = [_res(2.0, "etf news", source="TreeNews", move=1.0) for _ in range(3)]
    out = alpha_analysis(res, min_n=1)
    assert "TreeNews" in out["by_source"]
