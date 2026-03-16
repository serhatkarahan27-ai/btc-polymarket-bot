#!/bin/bash
# ============================================================
# Polymarket V3 Bot - Deploy Script (run on VPS)
# Downloads files from local machine and sets up everything
# ============================================================
set -e

APP_DIR="/opt/polymarket-bot"

echo "=========================================="
echo "  Polymarket V3 Bot - Deploy"
echo "=========================================="

# Create directory structure
mkdir -p $APP_DIR/history
mkdir -p $APP_DIR/logs

# Files should already be uploaded via scp
cd $APP_DIR

# Make scripts executable
chmod +x setup.sh run.sh stop.sh deploy.sh 2>/dev/null || true

# Run setup
echo "Running setup..."
bash setup.sh

echo ""
echo "=========================================="
echo "  Deploy complete!"
echo "  Run: cd $APP_DIR && bash run.sh"
echo "=========================================="
