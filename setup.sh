#!/bin/bash
# ============================================================
# Polymarket V3 Bot - VPS Setup Script
# Hostinger Linux VPS (Ubuntu)
# ============================================================
set -e

echo "=========================================="
echo "  Polymarket V3 Bot - VPS Setup"
echo "=========================================="

APP_DIR="/opt/polymarket-bot"

# 1. System updates
echo "[1/5] System update..."
apt update -y && apt upgrade -y

# 2. Install Python 3.11+
echo "[2/5] Installing Python..."
apt install -y python3 python3-pip python3-venv software-properties-common

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1)
echo "  Python: $PYTHON_VERSION"

# 3. Create app directory
echo "[3/5] Setting up app directory..."
mkdir -p $APP_DIR
mkdir -p $APP_DIR/history
mkdir -p $APP_DIR/logs

# 4. Copy project files (assumes files are already in $APP_DIR)
echo "[4/5] Installing dependencies..."
cd $APP_DIR
pip3 install requests --break-system-packages 2>/dev/null || pip3 install requests

# 5. Create systemd services for auto-restart
echo "[5/5] Creating systemd services..."

# Dashboard service
cat > /etc/systemd/system/polymarket-dashboard.service << 'SERVICEEOF'
[Unit]
Description=Polymarket Dashboard Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/polymarket-bot
ExecStart=/usr/bin/python3 -u dashboard_server.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/polymarket-bot/logs/dashboard.log
StandardError=append:/opt/polymarket-bot/logs/dashboard.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICEEOF

# Bot service
cat > /etc/systemd/system/polymarket-bot.service << 'SERVICEEOF'
[Unit]
Description=Polymarket V3 Multi-Config Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/polymarket-bot
ExecStart=/usr/bin/python3 -u multi_config_test_v3.py 5 --resume
Restart=always
RestartSec=10
StandardOutput=append:/opt/polymarket-bot/logs/bot.log
StandardError=append:/opt/polymarket-bot/logs/bot.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
echo ""
echo "=========================================="
echo "  Setup complete!"
echo "  App dir: $APP_DIR"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8877"
echo ""
echo "  Start with: bash run.sh"
echo "  Stop with:  bash stop.sh"
echo "  Logs:       tail -f $APP_DIR/logs/bot.log"
echo "=========================================="
