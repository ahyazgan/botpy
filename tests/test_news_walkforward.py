"""news_backtest walk-forward doğrulama testleri (sentetik mumlar, ağ yok)."""

from __future__ import annotations

import news_backtest as nbt


def _sig(i: int, *, entry=100.0, high=130.0, low=99.0, close=125.0,
         direction="bullish") -> dict:
    """Önceden prefetch edilmiş (candles dolu) sentetik sinyal.

    Varsayılan: büyük yukarı hareket → her TP seviyesinde TP vurur, SL vurmaz.
    """
    candles = [
        [0, entry, entry, entry, entry, 0],
        [60_000, entry, high, low, close, 0],
    ]
    return {
        "symbol": f"C{i}USDT", "direction": direction, "time": i * 1000,
        "impact": 8, "title": f"sig{i}", "candles": candles,
    }


def test_walk_forward_consistent_winner():
    signals = [_sig(i) for i in range(10)]  # hepsi kazanan long
    wf = nbt.walk_forward(signals, train_frac=0.7, fee=0.2, usdt=100.0, min_trades=3)
    assert wf["ok"] is True
    assert wf["params"]["tp"] == 10           # en yüksek TP en kârlı (hep TP vuruyor)
    assert wf["in_sample"]["n"] == 7          # 10 * 0.7
    assert wf["out_of_sample"]["n"] == 3
    assert wf["in_sample"]["avg_net_pct"] > 0
    assert wf["out_of_sample"]["avg_net_pct"] > 0
    assert isinstance(wf["verdict"], str)


def test_walk_forward_insufficient_train():
    # 2 sinyal → train int(2*0.7)=1 < min_trades(3) → ok False
    wf = nbt.walk_forward([_sig(0), _sig(1)], train_frac=0.7, min_trades=3)
    assert wf["ok"] is False
    assert wf["params"] is None


def test_walk_forward_sorts_by_time():
    # zaman karışık verilse de kronolojik bölünmeli
    signals = [_sig(i) for i in reversed(range(10))]
    wf = nbt.walk_forward(signals, train_frac=0.7, min_trades=3)
    assert wf["ok"] is True
    assert wf["in_sample"]["n"] + wf["out_of_sample"]["n"] == 10


def test_best_params_respects_min_trades():
    assert nbt._best_params([_sig(0)], fee=0.2, usdt=100.0, min_trades=3) is None
    best = nbt._best_params([_sig(i) for i in range(5)], fee=0.2, usdt=100.0, min_trades=3)
    assert best is not None
    sl, tp, stats = best
    assert tp == 10 and stats["n"] == 5
