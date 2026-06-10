#!/bin/bash
# ─────────────────────────────────────────────────────────
# EC2 kurulum scripti — Amazon Linux 2023 / Ubuntu 22.04
# Kullanım: bash setup_ec2.sh
# ─────────────────────────────────────────────────────────
set -e

REPO_URL="https://github.com/ahyazgan/botpy.git"   # kendi repo URL'nizi girin
BOT_DIR="$HOME/botpy"
SERVICE_NAME="arb-bot"

echo "=== 1. Sistem güncellemesi ==="
if command -v apt-get &>/dev/null; then
    sudo apt-get update -y && sudo apt-get install -y git python3 python3-pip python3-venv
elif command -v dnf &>/dev/null; then
    sudo dnf update -y && sudo dnf install -y git python3 python3-pip
fi

echo "=== 2. Repo klonla ==="
if [ -d "$BOT_DIR" ]; then
    cd "$BOT_DIR" && git pull
else
    git clone "$REPO_URL" "$BOT_DIR"
    cd "$BOT_DIR"
fi

echo "=== 3. Python sanal ortam + bağımlılıklar ==="
python3 -m venv "$BOT_DIR/venv"
source "$BOT_DIR/venv/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "=== 4. .env dosyası ==="
if [ ! -f "$BOT_DIR/.env" ]; then
    cat > "$BOT_DIR/.env" <<'ENV'
# Polymarket CLOB kimlik bilgileri
POLY_API_KEY=
POLY_API_SECRET=
POLY_PASSPHRASE=
PRIVATE_KEY=
CHAIN_ID=137

# Bot ayarları
MIN_PROFIT=0.015
MAX_TRADE_USDC=20.0
MIN_VOLUME_24H=10000
SCAN_INTERVAL=10
PAPER_MODE=true
ENV
    echo ">>> .env oluşturuldu: $BOT_DIR/.env"
    echo ">>> Lütfen bilgileri doldurun: nano $BOT_DIR/.env"
fi

echo "=== 5. systemd servisi ==="
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<SERVICE
[Unit]
Description=Polymarket Arb Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python arb_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Kurulum tamamlandı!"
echo ""
echo " Sonraki adımlar:"
echo " 1. .env bilgilerini doldur:  nano $BOT_DIR/.env"
echo " 2. Botu başlat:              sudo systemctl start $SERVICE_NAME"
echo " 3. Logları izle:             sudo journalctl -u $SERVICE_NAME -f"
echo " 4. Durumu kontrol et:        sudo systemctl status $SERVICE_NAME"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
