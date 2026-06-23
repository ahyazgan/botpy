# Kripto haber-trade radarı — 24/7 dağıtım imajı (AKTİF proje: news_bot.py).
# Haber her an gelir → sürekli çalışmalı. Bulut/sunucuda 7/24 çalıştırmak için.
FROM python:3.11-slim

# Çalışma zamanı: tamponsuz log + .pyc yok + UTF-8
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    BOTPY_DB=/data/botpy.db

WORKDIR /app

# Önce bağımlılıklar (katman önbelleği) — yalnız çalışma zamanı, dev paketleri değil
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Kalıcı veri (SQLite arşivi + trade_state) için volume; root olmayan kullanıcı
RUN mkdir -p /data && useradd -m -u 10001 botpy && chown -R botpy /app /data
USER botpy
VOLUME ["/data"]

EXPOSE 8000

# Hafif liveness: /healthz her zaman 200 döner (slim imajda curl yok → python)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

# Haber motoru (FastAPI + arka plan thread'leri). Ayarlar env ile:
# ANTHROPIC_API_KEY (puanlama), BINANCE_API_KEY/SECRET (canlı), TELEGRAM_*/DISCORD_*
# (uzak bildirim), API_TOKEN (dışa açılırsa koru).
CMD ["python", "news_bot.py", "--host", "0.0.0.0", "--port", "8000"]
