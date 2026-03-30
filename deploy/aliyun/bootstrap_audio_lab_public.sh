#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/audio-lab-public-site"
SERVICE_NAME="audio-lab-public.service"
NGINX_CONF_NAME="audio-lab-public.nginx.conf"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This bootstrap script currently expects an Ubuntu/Debian server with apt-get."
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 nginx rsync

sudo mkdir -p "${APP_DIR}"
sudo rsync -av --delete ./ "${APP_DIR}/" \
  --exclude ".git" \
  --exclude ".github"

sudo cp "${APP_DIR}/deploy/aliyun/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
sudo cp "${APP_DIR}/deploy/aliyun/${NGINX_CONF_NAME}" "/etc/nginx/sites-available/audio-lab-public"
sudo ln -sf /etc/nginx/sites-available/audio-lab-public /etc/nginx/sites-enabled/audio-lab-public
sudo rm -f /etc/nginx/sites-enabled/default

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sudo nginx -t
sudo systemctl restart nginx

echo
echo "Audio Lab Public Site is now serving through nginx."
echo "Open: http://$(curl -s ifconfig.me || hostname -I | awk '{print $1}')/"
