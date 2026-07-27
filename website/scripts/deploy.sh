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

# 绑定地址由 package.json 的 `start: next start -H 127.0.0.1` 决定，本脚本只**校验结果**。
# 背景：`next start` 默认绑 0.0.0.0，应用端口因此对公网直接开放（2026-07-28 实测外部
# 直连 http://<vps>:3000/api/health 返回 200，且 ufw inactive）。那条路径绕过 nginx：
#   ① X-Forwarded-For 完全由客户端写 → 按 IP 的限流（后台/客服台登录爆破、领取刷量）全失效；
#   ② 没有 TLS；③ 反代层的一切策略（跳转、体积限制）统统绕过。
# nginx 的 proxy_pass 指向 127.0.0.1:3000，故绑回环对反代零影响。
#
# 为什么校验而不是在这里设环境变量：**试过，不管用**——`next start`（14.x）只认 `-H`
# 参数，不读 HOSTNAME（env 确实进了进程，监听仍是 *:3000）。所以断言结果比指望机制可靠：
# 机制换了、package.json 被改回去、有人手工重建 pm2 应用，这里都能立刻喊出来。
BIND_EXPECT="${BIND_EXPECT:-127.0.0.1}"
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

# 备份轮转（替代「仅留最近 5 份」）：
#   · 始终保留最新 KEEP_BACKUP_RECENT 份（默认 5）——与旧策略同日回滚深度对齐，覆盖当日连打；
#   · 近 KEEP_BACKUP_DAYS 个日历日各留当日最新 1 份（默认 7）——避免一天打满 N 次后
#     把前几天的可回滚点全部挤掉（此前运维快照里 5 份备份全是同一天）；
#   · 总数硬顶 KEEP_BACKUP_MAX（默认 12），超出时优先丢掉更旧的「日报底」。
prune_backups() {
  local keep_recent="${KEEP_BACKUP_RECENT:-5}"
  local keep_days="${KEEP_BACKUP_DAYS:-7}"
  local max_total="${KEEP_BACKUP_MAX:-12}"
  local -a files=()
  local f base day age today_s day_s
  today_s="$(date +%s)"

  while IFS= read -r f; do
    [ -n "$f" ] && files+=("$f")
  done < <(ls -1t "$PARENT/${NAME}-bak-"*.tar.gz 2>/dev/null || true)
  [ "${#files[@]}" -eq 0 ] && return 0

  declare -A keep=()
  declare -A day_kept=()
  local i=0
  for f in "${files[@]}"; do
    if [ "$i" -lt "$keep_recent" ]; then keep["$f"]=1; fi
    i=$((i + 1))
  done

  for f in "${files[@]}"; do
    base="$(basename "$f")"
    # ${NAME}-bak-YYYYMMDD-HHMMSS.tar.gz
    if [[ "$base" =~ ^${NAME}-bak-([0-9]{8})- ]]; then
      day="${BASH_REMATCH[1]}"
    else
      continue
    fi
    day_s="$(date -d "$day" +%s 2>/dev/null || echo "")"
    [ -n "$day_s" ] || continue
    age=$(( (today_s - day_s) / 86400 ))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$keep_days" ] && [ -z "${day_kept[$day]:-}" ]; then
      day_kept["$day"]=1
      keep["$f"]=1
    fi
  done

  # 硬顶：超出 max_total 时，从最旧文件开始删（但跳过最新 keep_recent，保证快速回滚点）
  local -a kept_sorted=()
  for f in "${files[@]}"; do
    [ -n "${keep[$f]:-}" ] && kept_sorted+=("$f")
  done
  if [ "${#kept_sorted[@]}" -gt "$max_total" ]; then
    local drop=$(( ${#kept_sorted[@]} - max_total ))
    # kept_sorted 按时间新→旧；从尾部丢掉，但保护前 keep_recent
    local idx
    for ((idx=${#kept_sorted[@]}-1; idx>=keep_recent && drop>0; idx--)); do
      unset "keep[${kept_sorted[$idx]}]"
      drop=$((drop - 1))
    done
  fi

  local removed=0
  for f in "${files[@]}"; do
    if [ -z "${keep[$f]:-}" ]; then
      rm -f "$f"
      removed=$((removed + 1))
    fi
  done
  local kept=$(( ${#files[@]} - removed ))
  log "backup prune: kept $kept (recent≤$keep_recent + daily floor ${keep_days}d, max $max_total), removed $removed"
}
prune_backups

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
# 发布物目录（安装包等大文件）常驻服务器、不随源码 tarball 走：本地 deploy.ps1 打包时排除、
# 部署后单独差量上传。这里 exclude + --delete 语义 = 不覆盖也不删除；缺此保护时，
# 任何一次「不含安装包的部署」都会把线上下载文件整目录删掉（2026-07-25 实际发生两次）。
rsync -a --delete \
  --exclude=node_modules --exclude=.next --exclude=.env.local --exclude='*.log' \
  --exclude=public/downloads --exclude=public/releases \
  "$STAGE"/ "$APP_DIR"/
mkdir -p "$APP_DIR/public/downloads" "$APP_DIR/public/releases"

cd "$APP_DIR"

# 品牌 preset 门禁：官网已 vendored 到 vendor/brand，服务器没有 monorepo 的 platform/。
# 缺文件时 next build 会在解析 tailwind.config 时炸掉；这里在 npm ci 之前 fail-fast，
# 避免白跑几分钟安装再回滚。本地发包前请跑 npm run sync:brand（release/deploy 脚本已强制）。
log "3.5/7 assert vendored brand"
if [ ! -f "$APP_DIR/vendor/brand/tailwind-preset.cjs" ] || [ ! -f "$APP_DIR/vendor/brand/tokens.json" ]; then
  fail "vendor/brand/{tailwind-preset.cjs,tokens.json} missing in deploy package"
  fail "  fix: on monorepo machine run (cd website && npm run sync:brand) then re-pack"
  rollback
fi
log "vendor/brand OK"

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

# 绑定面校验：应用端口必须只在回环上监听。非致命（站点照常工作）故不回滚，但要喊出来
# ——它决定了「按 IP 的限流是否有意义」，静默失效过一次就够了。
BIND_ADDRS=$(ss -tlnH "sport = :$PORT" 2>/dev/null | awk '{print $4}' | sed 's/:[0-9]*$//' | sort -u | tr '\n' ' ')
case "$BIND_ADDRS" in
  *"$BIND_EXPECT"*)
    if echo "$BIND_ADDRS" | grep -qE '(\*|0\.0\.0\.0|\[::\])'; then
      log "WARN bind: :$PORT 仍在非回环地址监听 ($BIND_ADDRS) —— 应用端口对公网直接开放，"
      log "WARN bind: 反代会被绕过、按 IP 的限流失效。检查 package.json 的 start 是否为 'next start -H $BIND_EXPECT'"
    else
      log "bind OK ($BIND_ADDRS)"
    fi
    ;;
  "")
    log "WARN bind: 读不到 :$PORT 的监听地址（ss 不可用？），跳过校验" ;;
  *)
    log "WARN bind: :$PORT 监听在 $BIND_ADDRS，期望 $BIND_EXPECT —— 见上一条说明" ;;
esac

# SEO: 部署成功后把可收录 URL 推给 IndexNow（Bing/Naver/Yandex 等）。失败不影响部署。
SETUP_KEY=$(grep -E '^TELEGRAM_SETUP_KEY=' "$APP_DIR/.env.local" 2>/dev/null | sed -E 's/^[^=]+=//; s/^"//; s/"$//' | tr -d '\r')
if [ -n "$SETUP_KEY" ]; then
  IN_RES=$(curl -s -m 20 -X POST -H "x-setup-key: $SETUP_KEY" "http://127.0.0.1:$PORT/api/admin/indexnow" || echo '{"ok":false,"error":"curl_failed"}')
  log "indexnow ping: $IN_RES"
fi

rm -rf "$STAGE" "$TARBALL"
log "DONE @ $TS  (backup kept: $(basename "$BAK"))"
