"""Tarihsel haber içe-aktarıcı: normalize + parse_ts + scoring + arşivleme."""

from __future__ import annotations

import pytest

from import_history import (
    _symbol_from_coins,
    build_signal,
    import_rows,
    normalize_row,
    parse_ts,
)
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "imp.db"))
    yield s
    s.close()


# ── parse_ts ─────────────────────────────────────────────────────────────
def test_parse_iso():
    assert parse_ts("2026-06-01T12:00:00+00:00").startswith("2026-06-01T12:00")


def test_parse_epoch_seconds():
    ts = parse_ts(1780000000)
    assert ts is not None and ts.startswith("2026-")


def test_parse_epoch_millis():
    ts = parse_ts(1780000000000)
    assert ts is not None and ts.startswith("2026-")


def test_parse_garbage_none():
    assert parse_ts("not a date") is None
    assert parse_ts("") is None


# ── normalize_row (esnek sütunlar) ───────────────────────────────────────
def test_normalize_flexible_columns():
    n = normalize_row({"headline": "SEC approves ETF", "date": "2026-06-01T00:00:00Z", "ticker": "BTC"})
    assert n is not None
    assert n["title"] == "SEC approves ETF"
    assert n["coin"] == "BTC"


def test_normalize_missing_returns_none():
    assert normalize_row({"foo": "bar"}) is None           # başlık/zaman yok
    assert normalize_row({"title": "x"}) is None            # zaman yok


# ── _symbol_from_coins ───────────────────────────────────────────────────
def test_symbol_skips_stablecoin():
    assert _symbol_from_coins(["USDT", "SOL"]) == "SOLUSDT"
    assert _symbol_from_coins([]) is None


# ── build_signal: kendi kurallarımızla puanlama ──────────────────────────
def test_build_scores_with_our_rules():
    n = normalize_row({"title": "Major exchange hacked, ETH drained",
                       "time": "2026-06-01T00:00:00Z"})
    item = build_signal(n)
    assert item.scorer == "rule"
    assert item.impact >= 8            # hack = güçlü
    assert item.direction == "bearish"
    assert item.symbol == "ETHUSDT"    # başlıktan ETH


def test_build_uses_provided_coin_hint():
    n = normalize_row({"title": "Big partnership announced", "time": "2026-06-01T00:00:00Z", "coin": "ARB"})
    item = build_signal(n)
    assert item.symbol == "ARBUSDT"


# ── import_rows: arşivleme + sayımlar ────────────────────────────────────
def test_import_archives_tradeable(store):
    rows = [
        {"title": "SEC approves spot BTC ETF", "time": "2026-06-01T00:00:00Z"},   # bullish, BTC
        {"title": "Exchange suffers ETH hack", "time": "2026-06-02T00:00:00Z"},   # bearish, ETH
    ]
    res = import_rows(rows, store=store)
    assert res["imported"] == 2
    assert store.signal_span()["count"] == 2


def test_import_skips_neutral_and_no_symbol(store):
    rows = [
        {"title": "Fed holds interest rate steady", "time": "2026-06-01T00:00:00Z"},  # neutral makro
        {"title": "Some vague headline with no coin", "time": "2026-06-02T00:00:00Z"},  # symbol yok
        {"foo": "bar"},  # başlık/zaman yok
    ]
    res = import_rows(rows, store=store)
    assert res["imported"] == 0
    sk = res["skipped"]
    assert sk["missing_title_or_time"] == 1
    assert sk["neutral_direction"] + sk["no_tradeable_symbol"] == 2


def test_import_dedupes(store):
    rows = [{"title": "BTC ETF approved", "time": "2026-06-01T00:00:00Z"}]
    import_rows(rows, store=store)
    res2 = import_rows(rows, store=store)   # aynı id → dupe
    assert res2["imported"] == 0
    assert res2["skipped"]["duplicate"] == 1


def test_min_impact_filter(store):
    rows = [{"title": "Minor mainnet upgrade for SOL", "time": "2026-06-01T00:00:00Z"}]  # impact ~6
    res = import_rows(rows, store=store, min_impact=9)
    assert res["imported"] == 0
    assert res["skipped"]["below_min_impact"] == 1
