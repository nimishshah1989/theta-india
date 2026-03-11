#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# JIP Horizon India — EC2 One-Time Setup Script
# For Amazon Linux 2023 (t3.micro, ap-south-1)
# Usage: sudo bash deploy/setup.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="/opt/jip-india"
APP_USER="ec2-user"
REPO_URL="https://github.com/nimishshah1989/theta-india.git"

echo "═══ Step 1: System packages (Amazon Linux 2023) ═══"
dnf update -y -q
dnf install -y python3.11 python3.11-pip python3.11-devel nginx git curl

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
# Amazon Linux 2023 uses /etc/nginx/conf.d/ (no sites-available/sites-enabled)
cp "$APP_DIR/deploy/nginx/jip-india.conf" /etc/nginx/conf.d/jip-india.conf
# Disable default server block if it exists
if [ -f /etc/nginx/conf.d/default.conf ]; then
    mv /etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf.disabled
fi
nginx -t && systemctl enable nginx && systemctl restart nginx

echo "═══ Step 8: Start application ═══"
systemctl start jip-india
sleep 3

echo "═══ Verification ═══"
if curl -sf http://localhost:8001/health; then
    echo ""
    echo "✅ JIP Horizon India is running on port 8001"
    echo "   Nginx proxying on port 80"
    echo "   Public: http://13.206.50.251:8001/health"
    echo "   Service: sudo systemctl status jip-india"
    echo "   Logs:    sudo journalctl -u jip-india -f"
else
    echo ""
    echo "⚠️  Health check failed. Check:"
    echo "   sudo systemctl status jip-india"
    echo "   sudo journalctl -u jip-india --no-pager -n 50"
    echo "   Make sure .env is configured: $APP_DIR/.env"
fi
