#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# JIP Horizon India — EC2 One-Time Setup Script
# Run on a fresh Ubuntu 22.04+ EC2 instance
# Usage: sudo bash deploy/setup.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="/opt/jip-india"
APP_USER="ubuntu"
REPO_URL="https://github.com/nimishshah1989/theta-india.git"

echo "═══ Step 1: System packages ═══"
apt-get update -qq
apt-get install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y python3.11 python3.11-venv python3.11-dev nginx git curl

echo "═══ Step 2: Application directory ═══"
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

echo "═══ Step 3: Clone repository ═══"
if [ -d "$APP_DIR/.git" ]; then
    echo "Repo already cloned, pulling latest..."
    cd "$APP_DIR"
    sudo -u "$APP_USER" git pull origin main
else
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

echo "═══ Step 4: Python virtual environment ═══"
sudo -u "$APP_USER" python3.11 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip --quiet
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet

echo "═══ Step 5: Environment file ═══"
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo ""
    echo ">>> IMPORTANT: Edit $APP_DIR/.env with your Supabase and Anthropic credentials <<<"
    echo ""
fi

echo "═══ Step 6: Systemd service ═══"
cp "$APP_DIR/deploy/systemd/jip-india.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable jip-india

echo "═══ Step 7: Nginx reverse proxy ═══"
cp "$APP_DIR/deploy/nginx/jip-india.conf" /etc/nginx/sites-available/jip-india
ln -sf /etc/nginx/sites-available/jip-india /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "═══ Step 8: Start application ═══"
systemctl start jip-india
sleep 3

echo "═══ Verification ═══"
if curl -sf http://localhost:8001/health; then
    echo ""
    echo "✅ JIP Horizon India is running on port 8001"
    echo "   Nginx proxying on port 80"
    echo "   Service: sudo systemctl status jip-india"
    echo "   Logs:    sudo journalctl -u jip-india -f"
else
    echo ""
    echo "⚠️  Health check failed. Check:"
    echo "   sudo systemctl status jip-india"
    echo "   sudo journalctl -u jip-india --no-pager -n 50"
    echo "   Make sure .env is configured: $APP_DIR/.env"
fi
