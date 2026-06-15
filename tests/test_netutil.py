"""netutil.get_json — retry + backoff davranışı (ağsız)."""

from __future__ import annotations

import requests

from netutil import get_json


class _Resp:
    def __init__(self, status, payload=None, bad_json=False, headers=None):
        self.status_code = status
        self._payload = payload
        self._bad = bad_json
        self.headers = headers or {}

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


class _Session:
    """Ardışık yanıt/exception senaryosu oynatan sahte oturum."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _no_sleep(_):
    pass


def test_success_first_try():
    s = _Session([_Resp(200, {"price": "1"})])
    assert get_json("u", session=s, sleep=_no_sleep) == {"price": "1"}
    assert s.calls == 1


def test_retries_on_connection_error_then_succeeds():
    s = _Session([requests.ConnectionError("x"), _Resp(200, {"ok": 1})])
    slept = []
    assert get_json("u", session=s, retries=3, sleep=slept.append) == {"ok": 1}
    assert s.calls == 2 and len(slept) == 1     # bir kez bekledi


def test_retries_on_5xx():
    s = _Session([_Resp(503), _Resp(500), _Resp(200, {"ok": 1})])
    assert get_json("u", session=s, retries=3, sleep=_no_sleep) == {"ok": 1}
    assert s.calls == 3


def test_no_retry_on_4xx():
    s = _Session([_Resp(404), _Resp(200, {"ok": 1})])
    assert get_json("u", session=s, retries=3, sleep=_no_sleep) is None
    assert s.calls == 1                          # istemci hatası → tek deneme


def test_exhausts_retries_returns_none():
    s = _Session([requests.Timeout("t")] * 3)
    slept = []
    assert get_json("u", session=s, retries=3, sleep=slept.append) is None
    assert s.calls == 3 and len(slept) == 2      # son denemede beklemez


def test_backoff_is_exponential():
    s = _Session([_Resp(500)] * 3)
    slept = []
    get_json("u", session=s, retries=3, backoff=0.5, sleep=slept.append)
    assert slept == [0.5, 1.0]                   # 0.5*2^0, 0.5*2^1


def test_bad_json_returns_none():
    s = _Session([_Resp(200, bad_json=True)])
    assert get_json("u", session=s, sleep=_no_sleep) is None


def test_retries_on_429_rate_limit():
    s = _Session([_Resp(429), _Resp(200, {"ok": 1})])
    assert get_json("u", session=s, retries=3, sleep=_no_sleep) == {"ok": 1}
    assert s.calls == 2                              # 429 → yeniden denendi


def test_retries_on_418_ip_ban():
    s = _Session([_Resp(418), _Resp(418), _Resp(200, {"ok": 1})])
    assert get_json("u", session=s, retries=3, sleep=_no_sleep) == {"ok": 1}
    assert s.calls == 3


def test_404_still_no_retry():
    s = _Session([_Resp(404), _Resp(200, {"ok": 1})])
    assert get_json("u", session=s, retries=3, sleep=_no_sleep) is None
    assert s.calls == 1                              # diğer 4xx → tek deneme


# ── Retry-After başlığı ──────────────────────────────────────────────────
def test_retry_after_parse():
    from netutil import _retry_after_seconds

    class R:
        def __init__(self, h):
            self.headers = h
    assert _retry_after_seconds(R({"Retry-After": "5"})) == 5.0
    assert _retry_after_seconds(R({})) is None
    assert _retry_after_seconds(R({"Retry-After": "abc"})) is None
    assert _retry_after_seconds(R({"Retry-After": "-3"})) is None


def test_honors_retry_after_over_backoff():
    s = _Session([_Resp(429, headers={"Retry-After": "5"}), _Resp(200, {"ok": 1})])
    slept = []
    assert get_json("u", session=s, retries=3, backoff=0.4, sleep=slept.append) == {"ok": 1}
    assert slept == [5.0]                    # backoff (0.4) yerine Retry-After (5)


def test_retry_after_capped():
    s = _Session([_Resp(429, headers={"Retry-After": "999"}), _Resp(200, {"ok": 1})])
    slept = []
    get_json("u", session=s, retries=3, sleep=slept.append)
    assert slept == [120.0]                  # RETRY_AFTER_MAX ile sınırlı


def test_stats_count_rate_limit_and_retries(monkeypatch):
    import netutil
    monkeypatch.setitem(netutil._stats, "rate_limited", 0)
    monkeypatch.setitem(netutil._stats, "retries", 0)
    s = _Session([_Resp(429), _Resp(503), _Resp(200, {"ok": 1})])
    get_json("u", session=s, retries=5, sleep=_no_sleep)
    st = netutil.get_stats()
    assert st["rate_limited"] == 1     # yalnızca 429
    assert st["retries"] == 2          # 429 ve 503 sonrası birer bekleme
