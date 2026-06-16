#!/bin/bash
# Claude Code on the web — SessionStart hook.
# Kripto haber-trade radarı bağımlılıklarını kurar ki testler/linter/uygulama çalışsın.
# Senkron çalışır: oturum başlamadan önce bağımlılıklar hazır olur.
set -euo pipefail

# Yalnız uzak (Claude Code on the web) ortamında çalış; yerelde dokunma.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# feedparser'ın sgmllib3k bağımlılığı Debian setuptools'ta install_layout
# hatası verir; stdlib distutils ile düzgün build olur.
export SETUPTOOLS_USE_DISTUTILS=stdlib

# Python: CI ile birebir (requirements-dev.txt = runtime + pytest/ruff/mypy)
pip install -r requirements-dev.txt

# Dashboard (React) — install (ci değil; konteyner cache'inden faydalanır)
if [ -d dashboard ]; then
  (cd dashboard && npm install)
fi

echo "Bağımlılıklar hazır (Python + dashboard)."
