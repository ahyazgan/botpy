"""Test ortamı: gerçek botpy.db'yi kirletmemek için geçici DB kullan.

bot.py modülü import anında bir Store (DEFAULT_DB_PATH) açar; conftest
pytest tarafından test modüllerinden önce yüklendiği için burada
ayarlanan BOTPY_DB, storage modülü yüklenmeden önce devreye girer.
"""

from __future__ import annotations

import os
import tempfile

_tmp_db = os.path.join(tempfile.mkdtemp(prefix="botpy_test_"), "botpy_test.db")
os.environ["BOTPY_DB"] = _tmp_db
