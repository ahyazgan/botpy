"""Test ortamı: gerçek botpy.db ve trade_state.json'ı kirletmemek için geçici dosyalar.

bot.py modülü import anında bir Store (DEFAULT_DB_PATH) açar; conftest pytest
tarafından test modüllerinden önce yüklendiği için burada ayarlanan BOTPY_DB,
storage modülü yüklenmeden önce devreye girer. Aynı şekilde trader modülü import
anında STATE_FILE'ı okur ve load_state çağırır; BOTPY_STATE'i burada temp'e
yönlendirmek testlerin gerçek trade_state.json'ı okuyup/yazmasını engeller.
"""

from __future__ import annotations

import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="botpy_test_")
os.environ["BOTPY_DB"] = os.path.join(_tmp_dir, "botpy_test.db")
os.environ["BOTPY_STATE"] = os.path.join(_tmp_dir, "trade_state_test.json")
