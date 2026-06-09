"""execute_arb bacak-riski (leg-risk) ve bütçe davranışı testleri."""

from __future__ import annotations

import asyncio

import pytest

import arb_bot as ab


def _make_opp() -> ab.ArbOpportunity:
    m = ab.Market(
        id="m1", question="Test?", yes_token_id="YT", no_token_id="NT",
        yes_bid=0.4, yes_ask=0.45, no_bid=0.4, no_ask=0.45, volume24h=1.0,
    )
    return ab.ArbOpportunity(m, "buy", 10.0, 0.45, 0.45)


class _Recorder:
    """ab._place_order_sync yerine geçen sahte emir göndericisi."""

    def __init__(self, responses: dict[tuple[str, str], object]):
        # (token_id, side) -> response (dict) ya da Exception
        self.responses = responses
        self.calls: list[tuple[str, str, float, float]] = []

    def __call__(self, client, token_id, side, price, size):
        self.calls.append((token_id, side, price, size))
        res = self.responses.get((token_id, side), {"success": True, "status": "matched"})
        if isinstance(res, Exception):
            raise res
        return res


@pytest.mark.asyncio
async def test_both_legs_fill_no_unwind(monkeypatch):
    rec = _Recorder({
        ("YT", "BUY"): {"success": True, "status": "matched"},
        ("NT", "BUY"): {"success": True, "status": "matched"},
    })
    monkeypatch.setattr(ab, "_place_order_sync", rec)
    budget = ab.Budget(1000.0)

    await ab.execute_arb(
        client=None, opp=_make_opp(), loop=asyncio.get_event_loop(),
        budget=budget, dry_run=False,
    )

    # Sadece 2 emir (iki bacak), unwind yok
    assert len(rec.calls) == 2
    assert budget.spent == pytest.approx(ab.MAX_TRADE_USDC * 2)


@pytest.mark.asyncio
async def test_one_leg_fills_triggers_unwind(monkeypatch):
    # YES dolar (matched), NO iptal (unmatched) → YES bacağı SELL ile kapatılmalı
    rec = _Recorder({
        ("YT", "BUY"): {"success": True, "status": "matched"},
        ("NT", "BUY"): {"success": True, "status": "unmatched"},
        ("YT", "SELL"): {"success": True, "status": "matched"},  # unwind doluyor
    })
    monkeypatch.setattr(ab, "_place_order_sync", rec)

    await ab.execute_arb(
        client=None, opp=_make_opp(), loop=asyncio.get_event_loop(),
        budget=ab.Budget(1000.0), dry_run=False,
    )

    # 2 ilk emir + 1 unwind = 3 çağrı
    assert len(rec.calls) == 3
    unwind_call = rec.calls[2]
    assert unwind_call[0] == "YT" and unwind_call[1] == "SELL"


@pytest.mark.asyncio
async def test_dry_run_sends_no_orders(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("dry_run'da emir gönderilmemeli")

    monkeypatch.setattr(ab, "_place_order_sync", _boom)
    budget = ab.Budget(1000.0)

    await ab.execute_arb(
        client=None, opp=_make_opp(), loop=asyncio.get_event_loop(),
        budget=budget, dry_run=True,
    )
    # Emir yok ama bütçe yine de rezerve edilir (simülasyon muhasebesi)
    assert budget.spent == pytest.approx(ab.MAX_TRADE_USDC * 2)


@pytest.mark.asyncio
async def test_budget_cap_blocks_execution(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("bütçe dolu, emir gönderilmemeli")

    monkeypatch.setattr(ab, "_place_order_sync", _boom)
    budget = ab.Budget(max_total=10.0)  # 2*50 = 100 > 10 → engellenir

    await ab.execute_arb(
        client=None, opp=_make_opp(), loop=asyncio.get_event_loop(),
        budget=budget, dry_run=False,
    )
    assert budget.spent == 0.0
