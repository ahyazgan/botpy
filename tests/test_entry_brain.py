"""Giriş beyni: maybe_auto_trade beyin kancası + news_bot.entry_brain_decision."""

from __future__ import annotations

import json

import pytest

import trader


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    captured: dict = {}

    def fake_place(symbol, side, usdt=None, source="manual", reason="", news_source="",
                   impact=None, atr_pct=None, sl_mult=1.0, time_stop_min=None, **kwargs):
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
        "brain_self_improve": False,
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


def test_self_improve_vetoes_negative_band(env, monkeypatch):
    """Kendini-iyileştirme: conviction diliminin geçmişi negatifse oto-veto."""
    trader.S.brain_self_improve = True
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"bands": [
        {"band": "0-0.5", "n": 0, "win_rate": None, "avg_pnl": None},
        {"band": "0.5-0.7", "n": 8, "win_rate": 0.3, "avg_pnl": -1.5},  # negatif dilim
        {"band": "0.7-0.85", "n": 0, "win_rate": None, "avg_pnl": None},
        {"band": "0.85-1", "n": 0, "win_rate": None, "avg_pnl": None},
    ]})
    brain = lambda it, d: {"enter": True, "conviction": 0.6, "reason": "x"}  # noqa: E731
    assert trader.maybe_auto_trade(_Item(), brain=brain) is None


def test_self_improve_allows_positive_band(env, monkeypatch):
    trader.S.brain_self_improve = True
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"bands": [
        {"band": "0-0.5", "n": 0, "win_rate": None, "avg_pnl": None},
        {"band": "0.5-0.7", "n": 8, "win_rate": 0.7, "avg_pnl": 3.0},   # pozitif
        {"band": "0.7-0.85", "n": 0, "win_rate": None, "avg_pnl": None},
        {"band": "0.85-1", "n": 0, "win_rate": None, "avg_pnl": None},
    ]})
    brain = lambda it, d: {"enter": True, "conviction": 0.6, "reason": "x"}  # noqa: E731
    assert trader.maybe_auto_trade(_Item(), brain=brain) is not None


# ── trader.orderbook_imbalance (mikroyapı) ────────────────────────────────
def test_orderbook_imbalance_skew(monkeypatch):
    book = {"bids": [["100", "3"], ["99", "1"]], "asks": [["101", "1"], ["102", "1"]]}
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: book)
    out = trader.orderbook_imbalance("FOOUSDT")
    # bid_usd=300+99=399, ask_usd=101+102=203 → skew=(399-203)/602≈0.326
    assert out["skew"] > 0.3 and out["bid_usd"] == 399 and out["ask_usd"] == 203


def test_orderbook_imbalance_none_on_empty(monkeypatch):
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: None)
    assert trader.orderbook_imbalance("FOOUSDT") is None


# ── news_bot.entry_brain_decision (Claude çağrısı mock'lu) ────────────────
import news_bot as nb  # noqa: E402
from news_bot import NewsItem  # noqa: E402


def _news_item():
    return NewsItem(id="a", source="TreeNews", title="Foo hack", url="u", published=None,
                    fetched_at="2026-06-16T19:00:00+00:00", coins=["FOO"], impact=9,
                    direction="bullish", symbol="FOOUSDT", confirmed=True,
                    price_24h_pct=3.0, price_15m_pct=1.2, atr_pct=2.5, volume_usd=5e6)


def _dec(conviction=0.8, **kw):
    base = dict(enter=True, wait_seconds=0, conviction=conviction, direction="bullish",
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
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"samples": 0, "bands": [], "calibrated": None})
    monkeypatch.setattr(trader, "orderbook_imbalance", lambda s, **kw: {"skew": 0.1, "bid_usd": 1, "ask_usd": 1})
    monkeypatch.setattr(nb, "_btc_regime", lambda: {"btc_24s_pct": 1.0, "btc_1s_pct": 0.2, "rejim": "nötr"})
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


def test_entry_brain_backtest_skips_live_inputs(claude):
    """backtest=True → orderbook/BTC-rejimi/küme çağrılmaz (geçmişe kurulamaz)."""
    claude.setattr(trader, "orderbook_imbalance",
                   lambda *a, **k: (_ for _ in ()).throw(AssertionError("orderbook çağrılmamalı")))
    claude.setattr(nb, "_btc_regime", lambda: (_ for _ in ()).throw(AssertionError("rejim çağrılmamalı")))
    cl = _fake_client(_dec(conviction=0.8))
    claude.setattr(nb, "_get_anthropic", lambda: cl)
    out = nb.entry_brain_decision(_news_item(), {"side": "long", "usdt": 100.0}, backtest=True)
    assert out["enter"] is True
    # ctx'te mikroyapı/rejim None gönderildi
    sent = json.loads(cl.calls[0]["messages"][0]["content"])
    assert sent["mikroyapi"] is None and sent["piyasa_rejimi"] is None


# ── Beyin backtest yardımcıları ───────────────────────────────────────────
def test_bt_summary_and_item_from_bt():
    s = nb._bt_summary([2.0, -1.0, 3.0])
    assert s["n"] == 3 and s["avg_net_pct"] == 1.333 and s["win_rate"] == 66.7
    assert nb._bt_summary([])["n"] == 0
    it = nb._item_from_bt({"symbol": "FOOUSDT", "direction": "bullish", "impact": 9,
                           "source": "Binance", "title": "x", "time": 123})
    assert it.symbol == "FOOUSDT" and it.coins == ["FOO"] and it.direction == "bullish"


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


# ── trader.brain_scorecard (kalibrasyon) ──────────────────────────────────
def test_brain_scorecard_buckets_by_conviction(monkeypatch):
    monkeypatch.setattr(trader, "_closed", [
        {"pnl": -1.0, "brain": {"conviction": 0.3}},
        {"pnl": 2.0, "brain": {"conviction": 0.6}},
        {"pnl": 5.0, "brain": {"conviction": 0.9, "escalated": True}},
        {"pnl": 4.0, "brain": {"conviction": 0.9}},
        {"pnl": 1.0, "brain": None},          # beyinsiz → sayılmaz
        {"pnl": 3.0},                          # brain yok → sayılmaz
    ])
    sc = trader.brain_scorecard()
    assert sc["samples"] == 4 and sc["escalated_n"] == 1
    top = next(b for b in sc["bands"] if b["band"] == "0.85-1")
    assert top["n"] == 2 and top["avg_pnl"] == 4.5
    assert sc["calibrated"] is True   # yüksek conviction → yüksek P&L (monoton)


# ── Bekle/izle erteleme (_brain_for_trade + recheck) ─────────────────────
def test_wait_defers_then_resolves(monkeypatch):
    """wait_seconds>0 → erteleme kaydı + enter False; süre dolunca yeniden değerlendirilir."""
    for d in (nb._brain_due, nb._brain_items, nb._brain_tries):
        d.clear()
    item = _news_item()
    seq = iter([
        {"enter": True, "wait_seconds": 60, "conviction": 0.5, "reason": "gelişiyor"},
        {"enter": True, "wait_seconds": 0, "conviction": 0.8, "reason": "net"},
    ])
    monkeypatch.setattr(nb, "entry_brain_decision", lambda it, d: next(seq))

    # 1) İlk bakış: bekle → erteleme kaydı, enter False
    v = nb._brain_for_trade(item, {"side": "long"})
    assert v["enter"] is False and item.id in nb._brain_due

    # 2) Süre geçmiş gibi yap → recheck maybe_auto_trade çağırır → bu kez net girer
    nb._brain_due[item.id] = 0.0
    opened = {"pos": None}
    monkeypatch.setattr(trader, "maybe_auto_trade",
                        lambda it, brain=None, **kw: (opened.update(pos=brain(it, {"side": "long"})) or
                                                      {"side": "long", "symbol": "FOOUSDT", "mode": "paper"}))
    monkeypatch.setattr(nb, "_trade_context", lambda it: {})
    monkeypatch.setattr(nb, "notify_remote", lambda m: None)
    monkeypatch.setattr(nb, "_too_old", lambda it: False)
    nb._recheck_deferred_entries()
    assert item.id not in nb._brain_due   # net karar → erteleme temizlendi


# ── Beyin karar günlüğü (storage + _log_brain_decision + /brain-log) ──────
def test_storage_brain_decision_roundtrip(tmp_path):
    from storage import Store
    st = Store(str(tmp_path / "bd.db"))
    st.add_brain_decision({"news_id": "a", "symbol": "FOOUSDT", "side": "long", "verdict": "veto",
                           "conviction": 0.3, "escalated": True, "reason": "chase",
                           "scores": {"chase_risk": 0.8}, "direction": "bullish"})
    st.add_brain_decision({"news_id": "b", "symbol": "BARUSDT", "verdict": "enter",
                           "conviction": 0.9, "direction": "bearish"})
    alld = st.list_brain_decisions()
    assert len(alld) == 2 and alld[0]["news_id"] == "b"   # en yeni önce
    vetos = st.list_brain_decisions(verdict="veto")
    assert len(vetos) == 1 and vetos[0]["scores"] == {"chase_risk": 0.8} and vetos[0]["escalated"] is True
    st.close()


def test_log_brain_decision_derives_verdict(monkeypatch):
    captured = {}
    monkeypatch.setattr(nb, "get_store", lambda: type("S", (), {"add_brain_decision": staticmethod(lambda d: captured.update(d))})())
    nb._log_brain_decision(_news_item(), "long", {"enter": False, "wait_seconds": 0, "conviction": 0.2, "reason": "x"})
    assert captured["verdict"] == "veto"
    nb._log_brain_decision(_news_item(), "long", {"enter": True, "wait_seconds": 60, "conviction": 0.5})
    assert captured["verdict"] == "wait"   # wait_seconds>0 → bekle
    nb._log_brain_decision(_news_item(), "long", {"enter": True, "wait_seconds": 0, "conviction": 0.8})
    assert captured["verdict"] == "enter"


# ── Küme bağlamı ──────────────────────────────────────────────────────────
def test_cluster_context_counts_recent_same_coin(monkeypatch):
    now_iso = nb._now_iso()
    others = [
        NewsItem(id="b", source="S", title="t", url="u", published=now_iso, fetched_at=now_iso,
                 coins=["FOO"], direction="bullish"),
        NewsItem(id="c", source="S", title="t", url="u", published=now_iso, fetched_at=now_iso,
                 coins=["FOO"], direction="bearish"),
        NewsItem(id="d", source="S", title="t", url="u", published=now_iso, fetched_at=now_iso,
                 coins=["BAR"], direction="bullish"),
    ]
    monkeypatch.setattr(nb, "_news", others)
    out = nb._cluster_context(_news_item())   # item coins=["FOO"], bullish
    assert out["son_haber"] == 2 and out["ayni_yon"] == 1 and out["ters_yon"] == 1
