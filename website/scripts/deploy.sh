#!/usr/bin/env bash
# 华灵网站 · 服务器侧原子部署脚本
# 从上传的 tarball 部署到 pm2 站点：自动备份 -> 同步源码(保留 .env.local/node_modules/.next)
#   -> npm ci -> build -> pm2 restart -> 健康检查；任一步失败自动回滚到最近备份。
#
# 用法:   bash deploy.sh [/path/to/website-deploy.tar.gz]
# 覆盖项: APP_DIR(默认 /home/ubuntu/yuntech) PM2_NAME(默认 yuntech) PORT(默认 3000)
set -euo pipefail

# 并发锁：此前两次失败部署触发回滚，回滚里的重装/重建长尾（npm ci + next build 可达数分钟）
# 与随后发起的新一轮部署产生过竞态，多个 build 同时跑会互相踩 .next 产物、损坏线上站点。
# 用 fd 9 + flock 独占非阻塞锁把部署串行化：拿不到锁说明另一个部署（或其回滚）仍在进行，
# 直接报错退出（exit 1），由调用方稍后重试，绝不并行往下走。
exec 9>/tmp/yuntech-deploy.lock
flock -xn 9 || { echo "[deploy ERROR] another deploy holds /tmp/yuntech-deploy.lock — aborting" >&2; exit 1; }

APP_DIR="${APP_DIR:-/home/ubuntu/yuntech}"
PM2_NAME="${PM2_NAME:-yuntech}"
PORT="${PORT:-3000}"
TARBALL="${1:-/home/ubuntu/website-deploy.tar.gz}"

PARENT="$(dirname "$APP_DIR")"
NAME="$(basename "$APP_DIR")"
TS="$(date +%Y%m%d-%H%M%S)"
BAK="$PARENT/${NAME}-bak-${TS}.tar.gz"
STAGE="$PARENT/${NAME}-stage"

log()  { echo "[deploy $(date +%H:%M:%S)] $*"; }
fail() { echo "[deploy ERROR] $*" >&2; }

[ -f "$TARBALL" ] || { fail "tarball not found: $TARBALL"; exit 1; }
[ -d "$APP_DIR" ] || { fail "app dir not found: $APP_DIR"; exit 1; }

log "1/7 backup current -> $(basename "$BAK")"
tar -czf "$BAK" -C "$APP_DIR" --exclude=node_modules --exclude=.next .
# 仅保留最近 5 份备份
ls -1t "$PARENT/${NAME}-bak-"*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f

rollback() {
  fail "deploy failed — rolling back from $(basename "$BAK")"
  rm -rf "$STAGE"
  tar -xzf "$BAK" -C "$APP_DIR" || { fail "restore extract failed"; exit 1; }
  # 备份 tar 不含 node_modules；若本次部署改过依赖（package/lock）再回滚，旧代码必须配旧 lock
  # 对应的依赖树，否则 build 会因依赖错配失败。LIBC=glibc 已在主流程全局 export，对 rollback 同样生效。
  npm ci --no-audit --no-fund >/dev/null 2>&1 || true
  ( cd "$APP_DIR" && npm run build >/dev/null 2>&1 && pm2 restart "$PM2_NAME" --update-env >/dev/null 2>&1 ) \
    || fail "rollback rebuild/restart had issues — inspect manually"
  fail "rollback attempted; site restored to pre-deploy state"
  exit 1
}

log "2/7 extract stage"
rm -rf "$STAGE" && mkdir -p "$STAGE"
tar -xzf "$TARBALL" -C "$STAGE"

log "3/7 sync into place (keep .env.local/node_modules/.next, prune stale)"
rsync -a --delete \
  --exclude=node_modules --exclude=.next --exclude=.env.local --exclude='*.log' \
  "$STAGE"/ "$APP_DIR"/

cd "$APP_DIR"
log "4/7 npm ci"
# LIBC=glibc：本机 prebuild-install 探测不到 libc（日志见 libc= 空），会放弃预编译二进制
# 转而源码编译 better-sqlite3，在 1C 小鸡上必失败；显式声明后直接下载官方 glibc 预编译包。
export LIBC=glibc
npm ci --no-audit --no-fund || rollback
log "5/7 next build"
npm run build || rollback
log "6/7 pm2 restart ($PM2_NAME)"
pm2 restart "$PM2_NAME" --update-env || rollback
pm2 save >/dev/null 2>&1 || true

log "7/7 health check (:$PORT)"
sleep 4
if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null; then
  log "health OK"
else
  rollback
fi

# SEO: 部署成功后把可收录 URL 推给 IndexNow（Bing/Naver/Yandex 等）。失败不影响部署。
SETUP_KEY=$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')
if [ -n "$SETUP_KEY" ]; then
  IN_RES=$(curl -s -m 20 -X POST -H "x-setup-key: $SETUP_KEY" "http://127.0.0.1:$PORT/api/admin/indexnow" || echo '{"ok":false,"error":"curl_failed"}')
  log "indexnow ping: $IN_RES"
fi

rm -rf "$STAGE" "$TARBALL"
log "DONE @ $TS  (backup kept: $(basename "$BAK"))"
