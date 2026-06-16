"""Giriş beyni: maybe_auto_trade beyin kancası + news_bot.entry_brain_decision."""

from __future__ import annotations

import pytest

import trader


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    captured: dict = {}

    def fake_place(symbol, side, usdt=None, source="manual", reason="", news_source="",
                   impact=None, atr_pct=None):
        captured["usdt"] = usdt
        captured["side"] = side
        return {"id": "x", "symbol": symbol, "side": side, "usdt": usdt, "mode": "paper"}

    monkeypatch.setattr(trader, "place_trade", fake_place)
    for k, v in {
        "auto_trade": True, "use_entry_brain": True, "market": "spot",
        "auto_min_impact": 7, "auto_require_confirm": False, "trade_usdt": 100.0,
        "size_by_impact": False, "reduce_after_losses": 0, "cooldown_sec": 0,
        "max_positions": 20, "tier1_skip_confirm_impact": 0, "halt_trade_on_stale": False,
        "max_news_age_sec": 0, "max_same_direction": 0, "suppress_losing_sources": False,
        "skip_already_priced_pct": 0.0, "max_funding_rate_pct": 0.0,
    }.items():
        setattr(trader.S, k, v)
    yield captured
    trader.S.auto_trade = False
    trader.S.use_entry_brain = False


class _Item:
    impact = 9
    direction = "bullish"
    confirmed = True
    symbol = "FOOUSDT"
    source = "TreeNews"
    reason = ""
    price_24h_pct = None


def test_brain_veto_blocks_trade(env):
    brain = lambda it, d: {"enter": False, "conviction": 0.1, "reason": "chase riski"}  # noqa: E731
    assert trader.maybe_auto_trade(_Item(), brain=brain) is None
    assert env == {}   # place_trade hiç çağrılmadı


def test_brain_enter_scales_size_by_conviction(env):
    brain = lambda it, d: {"enter": True, "conviction": 1.0, "reason": "temiz"}  # noqa: E731
    pos = trader.maybe_auto_trade(_Item(), brain=brain)
    assert pos is not None
    assert env["usdt"] == 150.0   # 100 * (0.5 + 1.0) = 1.5x


def test_brain_low_conviction_shrinks_size(env):
    brain = lambda it, d: {"enter": True, "conviction": 0.0, "reason": "zayıf"}  # noqa: E731
    trader.maybe_auto_trade(_Item(), brain=brain)
    assert env["usdt"] == 50.0    # 100 * (0.5 + 0.0) = 0.5x


def test_brain_skipped_when_disabled(env):
    trader.S.use_entry_brain = False
    called = {"hit": False}

    def brain(it, d):
        called["hit"] = True
        return {"enter": False, "conviction": 0.0, "reason": "x"}

    pos = trader.maybe_auto_trade(_Item(), brain=brain)
    assert pos is not None and called["hit"] is False   # beyin çağrılmadı
    assert env["usdt"] == 100.0


def test_brain_skipped_on_tier1_reflex(env):
    """Tier-1 refleks girişte hız için beyin atlanır."""
    trader.S.tier1_skip_confirm_impact = 9
    called = {"hit": False}

    def brain(it, d):
        called["hit"] = True
        return {"enter": False, "conviction": 0.0, "reason": "x"}

    item = _Item()
    item.confirmed = False   # teyitsiz ama Tier-1 refleks → girer, beyin atlanır
    pos = trader.maybe_auto_trade(item, brain=brain)
    assert pos is not None and called["hit"] is False


def test_brain_exception_falls_back_to_mechanical(env):
    def brain(it, d):
        raise RuntimeError("Claude down")

    pos = trader.maybe_auto_trade(_Item(), brain=brain)
    assert pos is not None and env["usdt"] == 100.0   # mekanik karar geçerli


# ── news_bot.entry_brain_decision (Claude çağrısı mock'lu) ────────────────
import news_bot as nb  # noqa: E402
from news_bot import NewsItem  # noqa: E402


def _news_item():
    return NewsItem(id="a", source="TreeNews", title="Foo hack", url="u", published=None,
                    fetched_at="2026-06-16T19:00:00+00:00", coins=["FOO"], impact=9,
                    direction="bullish", symbol="FOOUSDT", confirmed=True,
                    price_24h_pct=3.0, price_15m_pct=1.2, atr_pct=2.5, volume_usd=5e6)


def _fake_client(decision):
    class _Resp:
        parsed_output = decision

    class _Msgs:
        def parse(self, **kw):
            _Msgs.captured = kw
            return _Resp()

    class _Client:
        messages = _Msgs()

    return _Client()


def test_entry_brain_decision_parses(monkeypatch):
    monkeypatch.setattr(nb, "USE_CLAUDE", True)
    monkeypatch.setattr(trader, "source_stats", lambda s: {"count": 12, "avg_pnl": 4.0})
    monkeypatch.setattr(trader, "_open_side_count", lambda s: 1)
    d = nb._EntryDecision(enter=True, conviction=0.8, direction="bullish", reason="temiz hack")
    monkeypatch.setattr(nb, "_get_anthropic", lambda: _fake_client(d))
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0, "news_source": "TreeNews"})
    assert out["enter"] is True and out["conviction"] == 0.8 and out["direction"] == "bullish"


def test_entry_brain_clamps_conviction(monkeypatch):
    monkeypatch.setattr(nb, "USE_CLAUDE", True)
    monkeypatch.setattr(trader, "source_stats", lambda s: {"count": 0, "avg_pnl": 0.0})
    monkeypatch.setattr(trader, "_open_side_count", lambda s: 0)
    d = nb._EntryDecision(enter=True, conviction=1.9, direction="bullish", reason="x")
    monkeypatch.setattr(nb, "_get_anthropic", lambda: _fake_client(d))
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0, "news_source": "T"})
    assert out["conviction"] == 1.0   # [0,1] kıstırıldı


def test_entry_brain_none_without_claude(monkeypatch):
    monkeypatch.setattr(nb, "USE_CLAUDE", False)
    assert nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0}) is None
