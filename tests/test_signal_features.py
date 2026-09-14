"""Sinyal özellik deposu: arşiv, karar-anı bağlamını kaybetmeden saklamalı.

Regresyon zemini: rel_volume/atr_pct arşive yazılmazsa news_backtest'in RVOL
ablasyon kapısı her satırda None görür → kapı kalıcı "veri yetersiz" işaretlenir
→ ablation_search min_rel_volume'ü asla öneremez → /ablation/apply'ın o dalı ölü
kod olur. confirmed'ın 0/1'e düzleştirilmesi de "teyit denenmedi"yi "teyit
başarısız" sayıp 'fiyat teyidi şart' sonucunu yapay güçlendirir.
"""

from __future__ import annotations

import sqlite3

import pytest

import news_backtest as nbt
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "feat.db"))
    yield s
    s.close()


def _sig(sid="s1", **kw):
    base = {
        "id": sid, "source": "treenews", "title": "Binance lists FOO",
        "url": "http://x", "published": None, "fetched_at": "2026-01-01T00:00:00+00:00",
        "coins": ["FOO"], "impact": 9, "direction": "bullish", "reason": "listeleme",
        "scorer": "claude", "symbol": "FOOUSDT", "price_24h_pct": 3.0,
        "price_15m_pct": 1.2, "price_60m_pct": 2.0, "volume_usd": 5e6,
        "confirmed": True, "price_note": "teyitli",
        "rel_volume": 2.4, "atr_pct": 1.1,
        "mismatch": False, "source_count": 3, "confirming_sources": ["rss:a", "rss:b"],
    }
    base.update(kw)
    return base


def test_features_survive_roundtrip(store):
    assert store.add_signal(_sig()) is True
    row = store.list_signals()[0]
    assert row["rel_volume"] == 2.4          # ablasyon RVOL kapısının girdisi
    assert row["atr_pct"] == 1.1
    assert row["price_60m_pct"] == 2.0
    assert row["source_count"] == 3
    assert row["confirming_sources"] == ["rss:a", "rss:b"]
    assert row["mismatch"] is False
    assert row["confirmed"] is True


def test_confirmed_is_three_state(store):
    store.add_signal(_sig("ok", confirmed=True))
    store.add_signal(_sig("fail", confirmed=False))
    store.add_signal(_sig("never", confirmed=False, price_24h_pct=None))  # import/fiyatsız
    by_id = {r["id"]: r for r in store.list_signals()}
    assert by_id["ok"]["confirmed"] is True
    assert by_id["fail"]["confirmed"] is False
    assert by_id["never"]["confirmed"] is None      # DENENMEDİ ≠ başarısız


def test_rvol_ablation_gate_has_data_from_archive(store):
    """Uçtan uca: arşivden gelen satırda RVOL kapısı 'veri var' demeli."""
    store.add_signal(_sig())
    rows = store.list_signals()
    sigs = nbt._signals_from_rows([{**r, "published": "2020-01-01T00:00:00+00:00"} for r in rows])
    assert sigs, "sinyal backtest biçimine süzülmeli"
    gates = {g["gate"]: g for g in nbt._ablation_gates(chase_pct=5.0, rvol_min=1.5, high_impact=8)}
    rvol_gate = next(g for name, g in gates.items() if name.startswith("rvol"))
    assert rvol_gate["has"](sigs[0]) is True      # eskiden hep False'du → kapı ölüydü
    assert rvol_gate["keep"](sigs[0]) is True     # 2.4 >= 1.5

    conf_gate = gates["confirmed"]
    assert conf_gate["has"](sigs[0]) is True


def test_migration_adds_columns_to_old_db(tmp_path):
    """Eski (kolonsuz) DB açıldığında ALTER TABLE ile tamamlanmalı, veri kaybı olmadan."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE news_signals (
        id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL,
        url TEXT, published TEXT, fetched_at TEXT, coins TEXT, impact INTEGER NOT NULL,
        direction TEXT NOT NULL, reason TEXT, scorer TEXT, symbol TEXT,
        price_24h_pct REAL, price_15m_pct REAL, volume_usd REAL, confirmed INTEGER,
        price_note TEXT)""")
    conn.execute("INSERT INTO news_signals (id, ts, source, title, impact, direction) "
                 "VALUES ('eski', '2026-01-01', 'rss', 'eski haber', 7, 'bullish')")
    conn.commit()
    conn.close()

    s = Store(path)
    try:
        cols = {r["name"] for r in
                s._conn.execute("PRAGMA table_info(news_signals)").fetchall()}
        assert {"rel_volume", "atr_pct", "source_count", "confirming_sources"} <= cols
        rows = s.list_signals()
        assert len(rows) == 1 and rows[0]["id"] == "eski"   # eski satır korundu
        assert rows[0]["rel_volume"] is None                 # yeni kolon boş, çökmüyor
        assert s.add_signal(_sig("yeni")) is True            # yazma da çalışıyor
    finally:
        s.close()


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    for _ in range(3):
        s = Store(path)
        s.close()
    s = Store(path)
    try:
        assert s.add_signal(_sig()) is True
    finally:
        s.close()
