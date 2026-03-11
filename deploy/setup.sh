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
APP_PORT=8002

echo "═══ Step 1: System packages (Amazon Linux 2023) ═══"
dnf update -y -q
dnf install -y python3.11 python3.11-pip python3.11-devel git curl

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

# NOTE: Nginx skipped — port 80 is used by existing Docker containers (fie2/Market Pulse).
# JIP India runs directly on port $APP_PORT via uvicorn.
# To add nginx later when port 80 is available:
#   cp "$APP_DIR/deploy/nginx/jip-india.conf" /etc/nginx/conf.d/
#   systemctl enable nginx && systemctl restart nginx

echo "═══ Step 7: Start application ═══"
systemctl start jip-india
sleep 3

echo "═══ Verification ═══"
if curl -sf "http://localhost:$APP_PORT/health"; then
    echo ""
    echo "JIP Horizon India is running on port $APP_PORT"
    echo "   Public: http://13.206.50.251:$APP_PORT/health"
    echo "   Service: sudo systemctl status jip-india"
    echo "   Logs:    sudo journalctl -u jip-india -f"
else
    echo ""
    echo "Health check failed. Check:"
    echo "   sudo systemctl status jip-india"
    echo "   sudo journalctl -u jip-india --no-pager -n 50"
    echo "   Make sure .env is configured: $APP_DIR/.env"
fi
