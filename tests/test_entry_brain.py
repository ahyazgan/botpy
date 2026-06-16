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
                   impact=None, atr_pct=None, sl_mult=1.0, time_stop_min=None):
        captured["usdt"] = usdt
        captured["side"] = side
        captured["sl_mult"] = sl_mult
        captured["time_stop_min"] = time_stop_min
        return {"id": "x", "symbol": symbol, "side": side, "usdt": usdt, "mode": "paper"}

    monkeypatch.setattr(trader, "place_trade", fake_place)
    for k, v in {
        "auto_trade": True, "use_entry_brain": True, "market": "spot",
        "auto_min_impact": 7, "auto_require_confirm": False, "trade_usdt": 100.0,
        "size_by_impact": False, "reduce_after_losses": 0, "cooldown_sec": 0,
        "max_positions": 20, "tier1_skip_confirm_impact": 0, "halt_trade_on_stale": False,
        "max_news_age_sec": 0, "max_same_direction": 0, "suppress_losing_sources": False,
        "skip_already_priced_pct": 0.0, "max_funding_rate_pct": 0.0, "brain_escalate": False,
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


def test_brain_exit_suggestions_applied(env):
    """sl_tightness → sl_mult, hold_minutes → time_stop_min place_trade'e geçer."""
    brain = lambda it, d: {"enter": True, "conviction": 0.5, "sl_tightness": "tight",  # noqa: E731
                           "hold_minutes": 30, "reason": "oynak"}
    pos = trader.maybe_auto_trade(_Item(), brain=brain)
    assert pos is not None
    assert env["sl_mult"] == 0.6 and env["time_stop_min"] == 30


def test_brain_verdict_stored_on_position(env):
    v = {"enter": True, "conviction": 0.7, "sl_tightness": "normal", "hold_minutes": 0,
         "reason": "ok", "scores": {"chase_risk": 0.2}}
    pos = trader.maybe_auto_trade(_Item(), brain=lambda it, d: v)
    assert pos["brain"]["conviction"] == 0.7 and "scores" in pos["brain"]


# ── news_bot.entry_brain_decision (Claude çağrısı mock'lu) ────────────────
import news_bot as nb  # noqa: E402
from news_bot import NewsItem  # noqa: E402


def _news_item():
    return NewsItem(id="a", source="TreeNews", title="Foo hack", url="u", published=None,
                    fetched_at="2026-06-16T19:00:00+00:00", coins=["FOO"], impact=9,
                    direction="bullish", symbol="FOOUSDT", confirmed=True,
                    price_24h_pct=3.0, price_15m_pct=1.2, atr_pct=2.5, volume_usd=5e6)


def _dec(conviction=0.8, **kw):
    base = dict(enter=True, conviction=conviction, direction="bullish",
                chase_risk=0.2, fade_risk=0.2, liquidity=0.8, source_quality=0.7,
                correlation_risk=0.1, sl_tightness="normal", hold_minutes=30, reason="temiz")
    base.update(kw)
    return nb._EntryDecision(**base)


def _fake_client(*decisions):
    """parse() çağrıldıkça sırayla decision döndürür (eskalasyon için 2 çağrı)."""
    seq = list(decisions)
    calls: list[dict] = []

    class _Resp:
        def __init__(self, d):
            self.parsed_output = d

    class _Msgs:
        def parse(self, **kw):
            calls.append(kw)
            return _Resp(seq[min(len(calls) - 1, len(seq) - 1)])

    class _Client:
        messages = _Msgs()

    _Client.calls = calls
    return _Client()


@pytest.fixture()
def claude(monkeypatch):
    monkeypatch.setattr(nb, "USE_CLAUDE", True)
    monkeypatch.setattr(trader, "source_stats", lambda s: {"count": 12, "avg_pnl": 4.0})
    monkeypatch.setattr(trader, "_open_side_count", lambda s: 1)
    monkeypatch.setattr(trader, "precedent_stats", lambda **kw: {"n": 3, "win_rate": 0.67,
                                                                 "avg_pnl": 2.0, "recent_pnls": [4, 6, -2]})
    monkeypatch.setattr(trader.S, "brain_escalate", False)
    yield monkeypatch


def test_entry_brain_full_output(claude):
    cl = _fake_client(_dec(conviction=0.8))
    claude.setattr(nb, "_get_anthropic", lambda: cl)
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0, "news_source": "TreeNews"})
    assert out["enter"] is True and out["conviction"] == 0.8
    assert out["sl_tightness"] == "normal" and out["hold_minutes"] == 30
    assert "chase_risk" in out["scores"] and out["escalated"] is False


def test_entry_brain_clamps_conviction(claude):
    cl = _fake_client(_dec(conviction=1.9))
    claude.setattr(nb, "_get_anthropic", lambda: cl)
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0, "news_source": "T"})
    assert out["conviction"] == 1.0   # [0,1] kıstırıldı


def test_entry_brain_escalates_in_band(claude):
    """Kararsız bantta (0.5) ikinci modele gider; nihai karar onun."""
    claude.setattr(trader.S, "brain_escalate", True)
    cl = _fake_client(_dec(conviction=0.5), _dec(conviction=0.85, reason="derin bakış"))
    claude.setattr(nb, "_get_anthropic", lambda: cl)
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0, "news_source": "T"})
    assert out["escalated"] is True and out["conviction"] == 0.85
    assert len(cl.calls) == 2 and cl.calls[1]["model"] == nb.ENTRY_BRAIN_ESCALATE_MODEL


def test_entry_brain_no_escalate_outside_band(claude):
    claude.setattr(trader.S, "brain_escalate", True)
    cl = _fake_client(_dec(conviction=0.9))   # bant dışı → tek çağrı
    claude.setattr(nb, "_get_anthropic", lambda: cl)
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0, "news_source": "T"})
    assert out["escalated"] is False and len(cl.calls) == 1


def test_entry_brain_none_without_claude(monkeypatch):
    monkeypatch.setattr(nb, "USE_CLAUDE", False)
    assert nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0}) is None


# ── trader.precedent_stats ────────────────────────────────────────────────
def test_precedent_stats_filters_and_summarizes(monkeypatch):
    monkeypatch.setattr(trader, "_closed", [
        {"news_source": "Binance", "symbol": "FOOUSDT", "side": "long", "pnl": 4.0},
        {"news_source": "Binance", "symbol": "FOOUSDT", "side": "long", "pnl": -2.0},
        {"news_source": "Twitter", "symbol": "BARUSDT", "side": "short", "pnl": 1.0},
    ])
    out = trader.precedent_stats(news_source="Binance")
    assert out["n"] == 2 and out["win_rate"] == 0.5 and out["avg_pnl"] == 1.0
    narrow = trader.precedent_stats(symbol="FOOUSDT", side="long")
    assert narrow["n"] == 2 and narrow["recent_pnls"] == [4.0, -2.0]


def test_precedent_stats_empty(monkeypatch):
    monkeypatch.setattr(trader, "_closed", [])
    out = trader.precedent_stats(news_source="X")
    assert out["n"] == 0 and out["win_rate"] is None and out["recent_pnls"] == []
