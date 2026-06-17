"""Çoklu-oylama: N bağımsız beyin çağrısı → çoğunluk enter + medyan conviction.

Gürültü azaltma: tek aykırı oy enter'ı veya conviction'ı domine edemez. n=1'de eski
tek-çağrı davranışı korunur (oy yok).
"""

from __future__ import annotations

from types import SimpleNamespace

import news_bot as nb


def _decision(enter, conviction, sl="normal", hold=0):
    return SimpleNamespace(
        enter=enter, conviction=conviction, wait_seconds=0, direction="bullish",
        sl_tightness=sl, hold_minutes=hold, reason="x",
        chase_risk=0.1, fade_risk=0.1, liquidity=0.8, source_quality=0.7,
        correlation_risk=0.1)


def _client_returning(seq):
    """_brain_call her çağrıda seq'ten sıradaki kararı döndürsün."""
    calls = {"i": 0}

    def fake_brain_call(client, model, ctx):
        d = seq[calls["i"] % len(seq)]
        calls["i"] += 1
        return d

    return fake_brain_call


# ── _median ─────────────────────────────────────────────────────────────────
def test_median_odd():
    assert nb._median([0.3, 0.9, 0.5]) == 0.5


def test_median_even():
    assert nb._median([0.2, 0.4, 0.6, 0.8]) == 0.5


def test_median_empty():
    assert nb._median([]) == 0.0


# ── _brain_vote ──────────────────────────────────────────────────────────────
def test_vote_n1_no_voting(monkeypatch):
    monkeypatch.setattr(nb, "_brain_call", _client_returning([_decision(True, 0.7)]))
    r, vote = nb._brain_vote(None, "m", {}, 1)
    assert vote is None          # n=1 → oylama yok
    assert r.enter is True


def test_vote_majority_enter(monkeypatch):
    # 2 enter, 1 veto → çoğunluk enter
    seq = [_decision(True, 0.7), _decision(True, 0.6), _decision(False, 0.3)]
    monkeypatch.setattr(nb, "_brain_call", _client_returning(seq))
    r, vote = nb._brain_vote(None, "m", {}, 3)
    assert r.enter is True
    assert vote["n"] == 3
    assert vote["enter_ratio"] == round(2 / 3, 2)
    assert r.conviction == 0.6   # medyan(0.7,0.6,0.3)


def test_vote_majority_veto(monkeypatch):
    # 1 enter, 2 veto → çoğunluk veto (tek iyimser oy domine edemez)
    seq = [_decision(True, 0.9), _decision(False, 0.2), _decision(False, 0.3)]
    monkeypatch.setattr(nb, "_brain_call", _client_returning(seq))
    r, vote = nb._brain_vote(None, "m", {}, 3)
    assert r.enter is False
    assert vote["enter_ratio"] == round(1 / 3, 2)


def test_vote_agreement(monkeypatch):
    seq = [_decision(True, 0.8), _decision(True, 0.7), _decision(True, 0.75)]
    monkeypatch.setattr(nb, "_brain_call", _client_returning(seq))
    _, vote = nb._brain_vote(None, "m", {}, 3)
    assert vote["agreement"] == 1.0   # tam oybirliği


def test_vote_skips_failures(monkeypatch):
    calls = {"i": 0}

    def flaky(client, model, ctx):
        calls["i"] += 1
        if calls["i"] == 2:
            raise RuntimeError("API hata")
        return _decision(True, 0.6)

    monkeypatch.setattr(nb, "_brain_call", flaky)
    r, vote = nb._brain_vote(None, "m", {}, 3)
    assert vote["n"] == 2   # 1 çağrı atlandı, 2 sağlam oy
    assert r.enter is True


def test_vote_all_fail_raises(monkeypatch):
    def always_fail(client, model, ctx):
        raise RuntimeError("hep patla")

    monkeypatch.setattr(nb, "_brain_call", always_fail)
    try:
        nb._brain_vote(None, "m", {}, 3)
        assert False, "istisna bekleniyordu"
    except RuntimeError:
        pass


def test_vote_representative_keeps_exit_fields(monkeypatch):
    # medyan conviction'a en yakın oyun sl_tightness/hold'u korunur
    seq = [_decision(True, 0.9, sl="wide", hold=60),
           _decision(True, 0.5, sl="tight", hold=30),
           _decision(True, 0.1, sl="normal", hold=10)]
    monkeypatch.setattr(nb, "_brain_call", _client_returning(seq))
    r, _ = nb._brain_vote(None, "m", {}, 3)
    assert r.conviction == 0.5
    assert r.sl_tightness == "tight"  # medyan(0.5) oyunun çıkış alanları
    assert r.hold_minutes == 30
