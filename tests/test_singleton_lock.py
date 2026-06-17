"""Tek-instance kilidi: çift bot çalışmasını önler."""

from __future__ import annotations

import os

import news_bot as nb


def test_singleton_lock_acquires(tmp_path, monkeypatch):
    lock = str(tmp_path / "bot.lock")
    monkeypatch.setenv("BOTPY_LOCK", lock)
    monkeypatch.setattr(nb, "_instance_lock", None)
    assert nb._acquire_singleton_lock() is True
    assert nb._instance_lock is not None
    assert os.path.exists(lock)
    # pid dosyaya yazıldı. Windows'ta kilitli dosya başka handle ile AÇILAMAZ
    # (msvcrt özel kilit) — orada kilit alımı + dosya varlığı yeterli kanıt.
    if os.name != "nt":
        with open(lock) as f:
            assert str(os.getpid()) in f.read()
    nb._instance_lock.close()   # temizlik


def test_singleton_lock_blocks_second_holder(tmp_path, monkeypatch):
    """Kilit başka bir fd tarafından tutulurken ikinci alım başarısız olmalı."""
    lock = str(tmp_path / "bot2.lock")
    # Birinci tutucuyu elle kur (gerçek OS kilidi)
    f1 = open(lock, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f1.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f1.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        f1.close()
        import pytest
        pytest.skip("OS kilidi bu ortamda yok")
    monkeypatch.setenv("BOTPY_LOCK", lock)
    monkeypatch.setattr(nb, "_instance_lock", None)
    try:
        assert nb._acquire_singleton_lock() is False   # ikinci alım engellendi
    finally:
        f1.close()
