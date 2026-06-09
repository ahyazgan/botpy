"""arb_bot.py saf (ağsız, secrets'sız) mantık testleri."""

from __future__ import annotations

import pytest

import arb_bot as ab


# ── extract_token_ids ────────────────────────────────────────────────────
def test_extract_token_ids_gamma_format():
    raw = {
        "clobTokenIds": '["tokYES", "tokNO"]',
        "outcomes": '["Yes", "No"]',
    }
    assert ab.extract_token_ids(raw) == ("tokYES", "tokNO")


def test_extract_token_ids_gamma_reversed_outcomes():
    # outcomes sırası ters → eşleştirme yine de doğru olmalı
    raw = {
        "clobTokenIds": '["tokNO", "tokYES"]',
        "outcomes": '["No", "Yes"]',
    }
    assert ab.extract_token_ids(raw) == ("tokYES", "tokNO")


def test_extract_token_ids_list_not_string():
    raw = {"clobTokenIds": ["a", "b"], "outcomes": ["Yes", "No"]}
    assert ab.extract_token_ids(raw) == ("a", "b")


def test_extract_token_ids_no_outcomes_assumes_yes_no_order():
    raw = {"clobTokenIds": '["a", "b"]'}
    assert ab.extract_token_ids(raw) == ("a", "b")


def test_extract_token_ids_clob_tokens_format():
    raw = {
        "tokens": [
            {"outcome": "Yes", "token_id": "y1"},
            {"outcome": "No", "token_id": "n1"},
        ]
    }
    assert ab.extract_token_ids(raw) == ("y1", "n1")


def test_extract_token_ids_missing_returns_none():
    assert ab.extract_token_ids({}) is None
    assert ab.extract_token_ids({"clobTokenIds": "not-json"}) is None


# ── parse_market ──────────────────────────────────────────────────────────
def test_parse_market_filters_low_volume():
    raw = {
        "clobTokenIds": '["y", "n"]',
        "outcomes": '["Yes", "No"]',
        "volume24hr": ab.MIN_VOLUME_24H - 1,
    }
    assert ab.parse_market(raw) is None


def test_parse_market_derives_no_prices():
    raw = {
        "id": "1",
        "question": "Test?",
        "clobTokenIds": '["y", "n"]',
        "outcomes": '["Yes", "No"]',
        "volume24hr": ab.MIN_VOLUME_24H + 1,
        "bestBid": 0.40,
        "bestAsk": 0.45,
    }
    m = ab.parse_market(raw)
    assert m is not None
    assert m.yes_token_id == "y" and m.no_token_id == "n"
    assert m.yes_bid == pytest.approx(0.40)
    assert m.yes_ask == pytest.approx(0.45)
    # NO_ask = 1 - YES_bid ; NO_bid = 1 - YES_ask
    assert m.no_ask == pytest.approx(0.60)
    assert m.no_bid == pytest.approx(0.55)


def test_parse_market_unparseable_tokens_returns_none():
    raw = {"volume24hr": ab.MIN_VOLUME_24H + 1}
    assert ab.parse_market(raw) is None


# ── detect_arb ────────────────────────────────────────────────────────────
def test_detect_arb_buy():
    # YES_ask + NO_ask = 0.90 < 1 - 0.02 → buy arb, kâr %10
    res = ab.detect_arb(yes_bid=0.4, yes_ask=0.45, no_bid=0.4, no_ask=0.45)
    assert res is not None
    direction, profit, yp, npr = res
    assert direction == "buy"
    assert profit == pytest.approx(10.0)
    assert (yp, npr) == (0.45, 0.45)


def test_detect_arb_sell():
    res = ab.detect_arb(yes_bid=0.6, yes_ask=0.65, no_bid=0.6, no_ask=0.65)
    assert res is not None
    direction, profit, yp, npr = res
    assert direction == "sell"
    assert profit == pytest.approx(20.0)


def test_detect_arb_below_threshold_none():
    # toplam 0.99 → kâr %1 < MIN_PROFIT %2 → arb yok
    assert ab.detect_arb(yes_bid=0.49, yes_ask=0.495, no_bid=0.49, no_ask=0.495) is None


def test_detect_arb_none_inputs():
    assert ab.detect_arb(None, None, None, None) is None


# ── quick_screen (gevşek eşik) ───────────────────────────────────────────
def test_quick_screen_catches_marginal():
    # toplam 0.985 → kâr %1.5: tam eşiği (%2) geçmez ama yarım eşiği (%1) geçer
    m = ab.Market(
        id="1", question="q", yes_token_id="y", no_token_id="n",
        yes_bid=0.49, yes_ask=0.4925, no_bid=0.49, no_ask=0.4925, volume24h=1.0,
    )
    assert ab.quick_screen(m) is True
    assert ab.detect_arb(m.yes_bid, m.yes_ask, m.no_bid, m.no_ask) is None


# ── quantize_price / prepare_arb_orders ──────────────────────────────────
def test_quantize_price_rounds_to_tick():
    assert ab.quantize_price(0.456) == pytest.approx(0.46)
    assert ab.quantize_price(0.454) == pytest.approx(0.45)
    assert ab.quantize_price(0.45) == pytest.approx(0.45)


def _opp(direction: str, yes: float, no: float) -> ab.ArbOpportunity:
    m = ab.Market(
        id="1", question="q", yes_token_id="y", no_token_id="n",
        yes_bid=0.0, yes_ask=0.0, no_bid=0.0, no_ask=0.0, volume24h=1.0,
    )
    return ab.ArbOpportunity(m, direction, 5.0, yes, no)


def test_prepare_arb_orders_valid_buy():
    p = ab.prepare_arb_orders(_opp("buy", 0.45, 0.45))
    assert p is not None
    assert p.yes_price == pytest.approx(0.45)
    assert p.yes_size == pytest.approx(round(ab.MAX_TRADE_USDC / 0.45, 2))


def test_prepare_arb_orders_valid_sell():
    p = ab.prepare_arb_orders(_opp("sell", 0.60, 0.60))
    assert p is not None
    assert p.no_size == pytest.approx(round(ab.MAX_TRADE_USDC / 0.60, 2))


def test_prepare_arb_orders_price_out_of_range():
    # 0.005 → tick'e yuvarlanınca 0.0 → aralık dışı → None
    assert ab.prepare_arb_orders(_opp("buy", 0.005, 0.45)) is None


def test_prepare_arb_orders_quantization_kills_edge():
    # 0.498 + 0.498 → yuvarlanınca 0.50 + 0.50 = 1.00 → kâr yok → None
    assert ab.prepare_arb_orders(_opp("buy", 0.498, 0.498)) is None


def test_prepare_arb_orders_below_min_shares():
    # max_trade küçük → size < MIN_SHARES → None
    assert ab.prepare_arb_orders(_opp("buy", 0.45, 0.45), max_trade=1.0) is None


# ── order_filled ──────────────────────────────────────────────────────────
def test_order_filled_matched():
    assert ab.order_filled({"success": True, "status": "matched"}) is True


def test_order_filled_unmatched():
    assert ab.order_filled({"success": True, "status": "unmatched"}) is False


def test_order_filled_success_false():
    assert ab.order_filled({"success": False, "status": "matched"}) is False


def test_order_filled_exception():
    assert ab.order_filled(RuntimeError("boom")) is False
    assert ab.order_filled(None) is False


def test_order_filled_no_status_field():
    assert ab.order_filled({"success": True}) is True


# ── ExecutionGuard ────────────────────────────────────────────────────────
def test_execution_guard_cooldown(monkeypatch):
    g = ab.ExecutionGuard(cooldown=60.0)
    t = {"v": 1000.0}
    monkeypatch.setattr(g, "_now", lambda: t["v"])

    assert g.can_execute("m1") is True
    g.mark_start("m1")
    assert g.can_execute("m1") is False  # in-flight
    g.mark_done("m1")
    assert g.can_execute("m1") is False  # cooldown içinde

    t["v"] += 61.0
    assert g.can_execute("m1") is True   # cooldown geçti


# ── Budget ────────────────────────────────────────────────────────────────
def test_budget_cap():
    b = ab.Budget(max_total=100.0)
    assert b.can_afford(60.0) is True
    b.charge(60.0)
    assert b.can_afford(60.0) is False
    assert b.can_afford(40.0) is True


# ── load_config ──────────────────────────────────────────────────────────
def test_load_config_missing_raises():
    with pytest.raises(SystemExit):
        ab.load_config(env={})


def test_load_config_dry_run_allows_missing():
    cfg = ab.load_config(env={"ARB_DRY_RUN": "1"})
    assert cfg.dry_run is True
    assert cfg.private_key == ""


def test_load_config_full():
    env = {
        "PRIVATE_KEY": "pk", "FUNDER_ADDRESS": "fa", "POLY_API_KEY": "ak",
        "POLY_SECRET": "sec", "POLY_PASSPHRASE": "pp",
    }
    cfg = ab.load_config(env=env)
    assert cfg.dry_run is False
    assert cfg.private_key == "pk" and cfg.api_passphrase == "pp"
