#!/usr/bin/env bash
# Richtet MemeBot auf einem frischen Ubuntu-Server ein (wird von cloud-init aufgerufen).
set -e
cd "$(dirname "$0")/.."
DIR="$(pwd)"
apt-get install -y python3-httpx python3-yaml python3-dotenv python3-pip git >/dev/null
pip3 install --break-system-packages -q "telethon>=1.36" || pip3 install -q "telethon>=1.36" || true
mkdir -p data
cat > /etc/systemd/system/memebot.service <<UNIT
[Unit]
Description=MemeBot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/main.py
Restart=always
RestartSec=5
Environment=TZ=Europe/Berlin
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now memebot
echo "MemeBot läuft. Logs: journalctl -u memebot -f"
