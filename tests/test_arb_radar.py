"""Arb radar testleri: opp kaydı (arb_bot) + /arb endpoint (bot.py)."""

from __future__ import annotations

import arb_bot as ab


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_opportunity(self, row: dict) -> int:
        self.rows.append(row)
        return len(self.rows)


def _opp(mid: str = "m1") -> ab.ArbOpportunity:
    m = ab.Market(
        id=mid, question="Q?", yes_token_id="y", no_token_id="n",
        yes_bid=0.4, yes_ask=0.45, no_bid=0.4, no_ask=0.45, volume24h=1.0,
    )
    return ab.ArbOpportunity(m, "buy", 10.0, 0.45, 0.45)


def test_opp_to_row_shape():
    row = ab.opp_to_row(_opp("mX"))
    assert row["market_id"] == "mX"
    assert row["direction"] == "buy"
    assert row["profit_pct"] == 10.0
    assert set(row) == {
        "ts", "market_id", "question", "direction",
        "profit_pct", "yes_price", "no_price",
    }


def test_maybe_record_writes_once_within_cooldown(monkeypatch):
    store = _FakeStore()
    guard = ab.ExecutionGuard(cooldown=60.0)
    t = {"v": 1000.0}
    monkeypatch.setattr(guard, "_now", lambda: t["v"])

    assert ab.maybe_record(store, _opp("m1"), guard) is True
    assert ab.maybe_record(store, _opp("m1"), guard) is False  # cooldown
    assert len(store.rows) == 1

    t["v"] += 61.0
    assert ab.maybe_record(store, _opp("m1"), guard) is True  # cooldown geçti
    assert len(store.rows) == 2


def test_maybe_record_none_store_is_noop():
    guard = ab.ExecutionGuard()
    assert ab.maybe_record(None, _opp(), guard) is False


def test_arb_endpoint_lists_recorded(tmp_path, monkeypatch):
    # bot.py'yi izole bir DB ile içe aktar (conftest zaten geçici DB ayarlar)
    import bot
    from fastapi.testclient import TestClient

    # Doğrudan store'a fırsat yaz
    bot.state.store.record_opportunity({
        "ts": "2026-01-01T00:00:00+00:00", "market_id": "mZ", "question": "Q?",
        "direction": "sell", "profit_pct": 7.5, "yes_price": 0.6, "no_price": 0.6,
    })

    client = TestClient(bot.app)
    with client:
        data = client.get("/arb?limit=10").json()

    assert data["count"] >= 1
    assert any(o["market_id"] == "mZ" and o["direction"] == "sell"
               for o in data["opportunities"])
