"""walkforward.split_series ve walk_forward testleri."""

from __future__ import annotations

from walkforward import split_series, walk_forward


def test_split_series_chronological():
    series = {"m1": [{"i": 0}, {"i": 1}, {"i": 2}, {"i": 3}, {"i": 4}]}
    train, test = split_series(series, train_frac=0.6)
    assert train["m1"] == [{"i": 0}, {"i": 1}, {"i": 2}]   # ilk %60
    assert test["m1"] == [{"i": 3}, {"i": 4}]              # son %40


def test_split_series_edge_fractions():
    series = {"m1": [{"i": 0}, {"i": 1}]}
    train, test = split_series(series, train_frac=0.5)
    assert train["m1"] == [{"i": 0}]
    assert test["m1"] == [{"i": 1}]


def _long_series(snaps):
    return {"m1": snaps}


def test_walk_forward_runs_and_reports():
    # Train kısmında sinyal+TP, test kısmında da benzer hareket
    snaps = [
        {"bid": 0.44, "ask": 0.45, "spread": 0.01},  # train: aç
        {"bid": 0.60, "ask": 0.61, "spread": 0.01},  # train: TP kapat
        {"bid": 0.44, "ask": 0.45, "spread": 0.01},  # test: aç
        {"bid": 0.60, "ask": 0.61, "spread": 0.01},  # test: TP kapat
    ]
    grid = {"take_profit_pct": [15.0, 25.0], "stop_loss_pct": [15.0]}
    res = walk_forward(_long_series(snaps), grid, train_frac=0.5, min_trades=1)
    assert res["ok"] is True
    assert "in_sample" in res and "out_of_sample" in res
    assert "verdict" in res
    assert res["params"]["take_profit_pct"] in (15.0, 25.0)


def test_walk_forward_no_trades_in_sample():
    # Geniş spread → hiç sinyal yok → in-sample boş
    snaps = [{"bid": 0.2, "ask": 0.5, "spread": 0.3}] * 4
    grid = {"take_profit_pct": [20.0], "stop_loss_pct": [15.0]}
    res = walk_forward(_long_series(snaps), grid, min_trades=1)
    assert res["ok"] is False


def test_walk_forward_verdict_overfit_when_oos_empty():
    # Train'de işlem var, test kısmında sinyal yok → OOS işlem yok
    snaps = [
        {"bid": 0.44, "ask": 0.45, "spread": 0.01},   # train aç
        {"bid": 0.60, "ask": 0.61, "spread": 0.01},   # train TP
        {"bid": 0.2, "ask": 0.5, "spread": 0.3},      # test: sinyal yok
        {"bid": 0.2, "ask": 0.5, "spread": 0.3},
    ]
    grid = {"take_profit_pct": [20.0], "stop_loss_pct": [15.0]}
    res = walk_forward(_long_series(snaps), grid, train_frac=0.5, min_trades=1)
    assert res["ok"] is True
    assert res["out_of_sample"]["count"] == 0
    assert "işlem yok" in res["verdict"]
