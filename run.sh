#!/bin/bash
# ============================================================
# Polymarket V3 Bot - Start Script
# Starts dashboard + bot with auto-restart via systemd
# ============================================================

APP_DIR="/opt/polymarket-bot"
LOG_DIR="$APP_DIR/logs"

echo "=========================================="
echo "  Polymarket V3 Bot - Starting..."
echo "=========================================="

# Create log directory
mkdir -p $LOG_DIR

# Rotate logs if they're too big (>10MB)
for logfile in $LOG_DIR/*.log; do
    if [ -f "$logfile" ] && [ $(stat -f%z "$logfile" 2>/dev/null || stat -c%s "$logfile" 2>/dev/null) -gt 10485760 ] 2>/dev/null; then
        mv "$logfile" "${logfile}.old"
        echo "  Rotated: $logfile"
    fi
done

# Start services
echo "[1/2] Starting Dashboard (port 8877)..."
systemctl start polymarket-dashboard
systemctl enable polymarket-dashboard

echo "[2/2] Starting V3 Bot..."
systemctl start polymarket-bot
systemctl enable polymarket-bot

echo ""
sleep 2

# Show status
echo "=========================================="
echo "  STATUS"
echo "=========================================="
systemctl status polymarket-dashboard --no-pager -l | head -5
echo ""
systemctl status polymarket-bot --no-pager -l | head -5

echo ""
IP=$(hostname -I | awk '{print $1}')
echo "=========================================="
echo "  Dashboard: http://$IP:8877"
echo "  Bot logs:  tail -f $LOG_DIR/bot.log"
echo "  Dash logs: tail -f $LOG_DIR/dashboard.log"
echo ""
echo "  Check status: systemctl status polymarket-bot"
echo "  Stop:         bash stop.sh"
echo "=========================================="
