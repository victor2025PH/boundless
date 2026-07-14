#!/usr/bin/env bash
# 新 VPS(Ubuntu 24.04, 1C1G) 一键初始化 + 部署华灵官网
# 以 root 运行(sudo)。前置：/home/ubuntu/website-deploy.tar.gz 和 /home/ubuntu/prod.env.local 已上传。
set -euo pipefail
log() { echo "[provision $(date +%H:%M:%S)] $*"; }

APP_USER=ubuntu
APP_DIR=/home/$APP_USER/yuntech
PM2_NAME=yuntech
UPLOAD_DIR=/home/$APP_USER

# ── 1. swap（1G 内存机器必须，否则 next build OOM）─────────────
if ! swapon --show | grep -q swap; then
  log "1/8 create 3G swapfile"
  fallocate -l 3G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=3072
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  log "1/8 swap exists, skip"
fi

# ── 2. 基础软件 ────────────────────────────────────────────────
log "2/8 apt install base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx rsync curl ca-certificates gnupg certbot python3-certbot-nginx > /dev/null

# ── 3. Node 20 (NodeSource) ───────────────────────────────────
if ! command -v node >/dev/null || [[ "$(node -v)" != v2* ]]; then
  log "3/8 install Node 20"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null
  apt-get install -y -qq nodejs > /dev/null
fi
log "node $(node -v) npm $(npm -v)"
npm i -g pm2 --silent

# ── 4. ubuntu 用户已存在且已装 key，跳过 ──────────────────────
log "4/8 user $APP_USER ready"

# ── 5. 部署应用 ────────────────────────────────────────────────
log "5/8 extract app to $APP_DIR"
mkdir -p "$APP_DIR"
tar -xzf "$UPLOAD_DIR/website-deploy.tar.gz" -C "$APP_DIR"
cp "$UPLOAD_DIR/prod.env.local" "$APP_DIR/.env.local"
chmod 600 "$APP_DIR/.env.local"
mkdir -p /home/$APP_USER/hualing-leads
chown -R $APP_USER:$APP_USER /home/$APP_USER/yuntech /home/$APP_USER/hualing-leads

log "6/8 npm ci + next build (1C1G, 可能需要 5-15 分钟)"
cd "$APP_DIR"
sudo -u $APP_USER bash -c "cd $APP_DIR && npm ci --no-audit --no-fund --silent"
sudo -u $APP_USER bash -c "cd $APP_DIR && NODE_OPTIONS=--max-old-space-size=768 npm run build"

# ── 7. pm2 ────────────────────────────────────────────────────
log "7/8 pm2 start + startup"
sudo -u $APP_USER bash -c "cd $APP_DIR && pm2 delete $PM2_NAME 2>/dev/null; cd $APP_DIR && pm2 start npm --name $PM2_NAME -- start && pm2 save"
env PATH=$PATH:/usr/bin pm2 startup systemd -u $APP_USER --hp /home/$APP_USER > /dev/null
sudo -u $APP_USER pm2 save

# ── 8. nginx ──────────────────────────────────────────────────
log "8/8 nginx site config"
cat > /etc/nginx/sites-available/yuntech <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name usdt2026.cc www.usdt2026.cc _;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        client_max_body_size 20m;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/yuntech /etc/nginx/sites-enabled/yuntech
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

sleep 3
if curl -sf http://127.0.0.1:3000/api/health > /dev/null; then
  log "HEALTH OK (app :3000)"
else
  log "WARN: app health check failed - inspect pm2 logs"
  sudo -u $APP_USER pm2 logs $PM2_NAME --lines 20 --nostream || true
fi
curl -s -o /dev/null -w "nginx:80 -> %{http_code}\n" -H "Host: usdt2026.cc" http://127.0.0.1/
log "PROVISION DONE"
