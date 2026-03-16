#!/bin/bash
# ============================================================
# Polymarket V3 Bot - Stop Script
# Stops all bot processes cleanly
# ============================================================

echo "=========================================="
echo "  Polymarket V3 Bot - Stopping..."
echo "=========================================="

# Stop systemd services
echo "[1/2] Stopping Bot..."
systemctl stop polymarket-bot 2>/dev/null && echo "  Bot stopped." || echo "  Bot was not running."

echo "[2/2] Stopping Dashboard..."
systemctl stop polymarket-dashboard 2>/dev/null && echo "  Dashboard stopped." || echo "  Dashboard was not running."

# Kill any orphan processes
echo ""
echo "Cleaning up orphan processes..."
pkill -f "multi_config_test_v3.py" 2>/dev/null && echo "  Killed orphan bot process." || true
pkill -f "dashboard_server.py" 2>/dev/null && echo "  Killed orphan dashboard process." || true

echo ""
echo "=========================================="
echo "  All processes stopped."
echo "  Results saved in: /opt/polymarket-bot/v3_results.json"
echo "  Logs in: /opt/polymarket-bot/logs/"
echo "=========================================="
