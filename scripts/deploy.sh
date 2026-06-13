#!/usr/bin/env bash
# deploy.sh — One-shot setup script for Oracle Ubuntu server
# Run as: bash scripts/deploy.sh

set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="crypto-signal-bot"
PYTHON="python3"

echo "═══════════════════════════════════════════"
echo "  Crypto Signal Bot — Deploy Script"
echo "═══════════════════════════════════════════"
echo "Bot directory: $BOT_DIR"

# 1. System deps
echo ""
echo "[1/6] Installing system dependencies..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-pip python3-venv git curl

# 2. Python venv
echo ""
echo "[2/6] Creating Python virtual environment..."
cd "$BOT_DIR"
$PYTHON -m venv venv
source venv/bin/activate

# 3. Pip deps
echo ""
echo "[3/6] Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. .env file
echo ""
echo "[4/6] Setting up configuration..."
if [ ! -f "$BOT_DIR/.env" ]; then
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    echo ""
    echo "  ⚠️  .env file created from template."
    echo "  Please edit $BOT_DIR/.env with your API keys and channel ID."
    echo "  Then re-run this script."
    exit 0
fi

# 5. Logs directory
mkdir -p "$BOT_DIR/logs"

# 6. Systemd service
echo ""
echo "[5/6] Installing systemd service..."
CURRENT_USER=$(whoami)
# Replace placeholder user in service file
sed "s/User=ubuntu/User=$CURRENT_USER/g; s|/home/ubuntu|$HOME|g" \
    "$BOT_DIR/scripts/crypto-signal-bot.service" \
    | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "[6/6] Done!"
echo ""
echo "  ✅ Service installed and started."
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status $SERVICE_NAME"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo "    tail -f $BOT_DIR/logs/bot.log"
echo ""
