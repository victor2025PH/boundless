#!/usr/bin/env bash
# deploy_controller.sh -- 在 VPS（bd2026.cc）上安装 / 升级智控主控（fleet_control 独立实例）。幂等，可重复跑。
#
#   sudo bash deploy_controller.sh /path/to/chengjie-src.tar.gz      # 首装 / 升级（tarball = engines/chengjie 目录打包）
#   sudo bash deploy_controller.sh --check                            # 只体检：服务 / 端口 / 反代 / 配置
#   sudo bash deploy_controller.sh --rollback                         # 回滚到上一版 app（app.prev）
#   sudo bash deploy_controller.sh --console-check                    # 只体检控制台页（主令牌 GET，只读）
#
# 布局：/opt/chatx-fleet/{app,app.prev,venv}  /etc/chatx-fleet/{config.yaml,env}（只读）
#       /var/lib/chatx-fleet/（fleet.db 与其余运行时库；AITR_DATA_DIR）
# 步骤：建用户 → 解包到 app.new → venv + pip（requirements-ci.txt：主控不需要 TG/WA 运行时依赖） → 首次生成 config
#      （随机 auth_token / secret_key，只打印一次） → --check 体检 → 原子切换 app → systemd → nginx snippet → 健康检查；
#      健康检查失败自动回滚。**不改现有官网 nginx server 块，只 include 一个 snippet**（需人工加一行，脚本只提示）。
#      健康检查之后再跑控制台体检（console_check.py：登录态拉 /fleet/console，断言写操作路径没被改坏 /
#      前缀没补两遍 / 公网 /fleet/login 与静态可达），不过同样自动回滚。CONSOLE_CHECK=0 可跳过（只限救火）。
#      前缀模式：公网只反代 /fleet/ 时 config 必须有 web_admin.public_prefix: "/fleet/ui"（见 src/web/public_prefix.py）；
#      现网 app 还是手补版（含 FleetUiPrefixMiddleware）而 config 没这项时，脚本在动任何东西之前拒绝继续。
#
# 与官网 deploy.sh（pm2 / yuntech）互不相干：不同用户、不同目录、不同端口（18798 仅回环）。
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/chatx-fleet}"
CONF_DIR="${CONF_DIR:-/etc/chatx-fleet}"
DATA_DIR="${DATA_DIR:-/var/lib/chatx-fleet}"
SVC_USER="${SVC_USER:-chatx-fleet}"
SVC_NAME="chatx-fleet"
PORT="${PORT:-18798}"
PUBLIC_URL="${PUBLIC_URL:-https://bd2026.cc/fleet}"
PYTHON="${PYTHON:-python3}"
CONSOLE_CHECK="${CONSOLE_CHECK:-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo "[fleet-deploy $(date +%H:%M:%S)] $*"; }
fail() { echo "[fleet-deploy ERROR] $*" >&2; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || fail "run as root (sudo)"; }

check() {
  local rc=0
  systemctl is-active --quiet "$SVC_NAME" && log "service: active" || { log "service: NOT active"; rc=1; }
  if curl -fsS -m 5 "http://127.0.0.1:${PORT}/fleet/" -o /dev/null; then log "local /fleet/: 200"; else log "local /fleet/: FAIL"; rc=1; fi
  if curl -fsS -m 8 "${PUBLIC_URL}/" -o /dev/null; then log "public ${PUBLIC_URL}/: 200"; else log "public ${PUBLIC_URL}/: FAIL (nginx snippet included? cert?)"; rc=1; fi
  # GET overview is the auth wall. Do not POST /api/fleet/enroll: a fake code counts as a failure against this VPS IP.
  local code
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "${PUBLIC_URL}/api/fleet/overview" || true)
  [ "$code" = "401" ] || [ "$code" = "403" ] && log "public api probe: HTTP $code (ok, auth wall)" || { log "public api probe: HTTP $code (expected 401)"; rc=1; }
  grep -q 'CHANGE_ME' "$CONF_DIR/config.yaml" 2>/dev/null && { log "config still has CHANGE_ME"; rc=1; }
  [ -f "$DATA_DIR/fleet.db" ] && log "db: $DATA_DIR/fleet.db ($(du -h "$DATA_DIR/fleet.db" | cut -f1))" || log "db: not created yet (first enroll creates it)"
  return $rc
}

# 控制台体检（只读：主令牌 GET /fleet/console + 公网 /fleet/login、前缀静态）。令牌由脚本自己从 config 读，不进命令行。
console_check() {
  local py="$APP_ROOT/venv/bin/python" chk="$HERE/console_check.py"
  [ -x "$py" ] || py="$PYTHON"
  [ -f "$chk" ] || chk="$APP_ROOT/app/deploy/fleet/console_check.py"
  [ -f "$chk" ] || { log "console check: console_check.py not found"; return 1; }
  "$py" "$chk" --config "$CONF_DIR/config.yaml" --env-file "$CONF_DIR/env" \
      --base "http://127.0.0.1:${PORT}" --public-url "$PUBLIC_URL"
}

# 现网 app 是 VPS 手补版（/fleet/ui 前缀中间件）而 config 没开 public_prefix → 新包会把 /fleet/ui 整个打掉。
preflight_public_prefix() {
  local live="$APP_ROOT/app/src/web/admin.py"
  [ -f "$live" ] && grep -q 'FleetUiPrefixMiddleware' "$live" || return 0
  [ -x "$APP_ROOT/venv/bin/python" ] || return 0
  if "$APP_ROOT/venv/bin/python" - "$CONF_DIR/config.yaml" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
sys.exit(0 if str((c.get("web_admin") or {}).get("public_prefix") or "").strip() else 1)
PY
  then
    log "preflight: live app is the hand-patched /fleet/ui build; config has web_admin.public_prefix (ok)"
  else
    fail "live app serves /fleet/ui via hand patches but $CONF_DIR/config.yaml has no web_admin.public_prefix. Add   web_admin: public_prefix: \"/fleet/ui\"   first (new code reads it), then re-run."
  fi
}

rollback() {
  need_root
  [ -d "$APP_ROOT/app.prev" ] || fail "no app.prev to roll back to"
  log "rollback: app <-> app.prev"
  mv "$APP_ROOT/app" "$APP_ROOT/app.rollback.$$" && mv "$APP_ROOT/app.prev" "$APP_ROOT/app" && mv "$APP_ROOT/app.rollback.$$" "$APP_ROOT/app.prev"
  systemctl restart "$SVC_NAME"
  sleep 3
  check
}

case "${1:-}" in
  --check) check; exit $? ;;
  --rollback) rollback; exit $? ;;
  --console-check) console_check; exit $? ;;
  "") fail "usage: deploy_controller.sh <chengjie-src.tar.gz> | --check | --rollback | --console-check" ;;
esac

need_root
TARBALL="$1"
[ -f "$TARBALL" ] || fail "tarball not found: $TARBALL"
command -v "$PYTHON" >/dev/null || fail "$PYTHON not found"
preflight_public_prefix

log "1/8 user + dirs"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$APP_ROOT" --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$APP_ROOT" "$CONF_DIR" "$DATA_DIR"
# Config dir stays root:group 750 (readable, not writable). The service writes the db under DATA_DIR.
chown root:"$SVC_USER" "$CONF_DIR"
chmod 750 "$CONF_DIR"
chown "$SVC_USER:$SVC_USER" "$DATA_DIR"
chmod 770 "$DATA_DIR"

log "2/8 unpack -> app.new"
rm -rf "$APP_ROOT/app.new"; mkdir -p "$APP_ROOT/app.new"
tar -xzf "$TARBALL" -C "$APP_ROOT/app.new" --strip-components="${STRIP_COMPONENTS:-0}"
[ -f "$APP_ROOT/app.new/main.py" ] || fail "tarball has no main.py at top level (set STRIP_COMPONENTS=1 if it is nested)"
mkdir -p "$APP_ROOT/app.new/logs" "$APP_ROOT/app.new/config"
# Windows 打的 tarball 带 0777/0666：解包后收掉 group/other 写位（属主是服务用户，自己的 logs/config 照写）
chmod -R go-w "$APP_ROOT/app.new"
chown -R "$SVC_USER:$SVC_USER" "$APP_ROOT/app.new"

log "3/8 venv + pip"
[ -d "$APP_ROOT/venv" ] || "$PYTHON" -m venv "$APP_ROOT/venv"
REQ="$APP_ROOT/app.new/requirements-ci.txt"; [ -f "$REQ" ] || REQ="$APP_ROOT/app.new/requirements.txt"
"$APP_ROOT/venv/bin/pip" install -q --upgrade pip
"$APP_ROOT/venv/bin/pip" install -q -r "$REQ"

# Move a runtime file (and sqlite -wal/-shm) out of the read-only config dir.
# Never touches config.yaml / env. If the destination already exists, leave both.
relocate_runtime_file() {
  local name="$1"
  local src="$CONF_DIR/$name"
  local dst="$DATA_DIR/$name"
  if [ -f "$src" ] && [ ! -e "$dst" ]; then
    mv "$src" "$dst"
    chown "$SVC_USER:$SVC_USER" "$dst"
    log "moved runtime state $name -> $DATA_DIR"
    local ext
    for ext in -wal -shm; do
      if [ -f "${src}${ext}" ] && [ ! -e "${dst}${ext}" ]; then
        mv "${src}${ext}" "${dst}${ext}"
        chown "$SVC_USER:$SVC_USER" "${dst}${ext}"
      fi
    done
  elif [ -f "$src" ] && [ -e "$dst" ]; then
    log "leave $name in both $CONF_DIR and $DATA_DIR (dest already exists)"
  fi
}

relocate_runtime_state() {
  local name
  for name in \
    web_users.db cost_ledger.db audit.db knowledge_base.db \
    runtime_flags.db inbox.db translation_memory.db \
    license_quota.db token_ledger.db fleet_control.db \
    agent_char_usage.db bot.db strategy_events.db \
    persona_media.db group_members.db quality_trend.db \
    license.key trial_claim.json audit_log.jsonl
  do
    relocate_runtime_file "$name"
  done
}

log "4/8 config"
if [ ! -f "$CONF_DIR/config.yaml" ]; then
  TOKEN="$(openssl rand -hex 24)"; SECRET="$(openssl rand -hex 32)"
  (cd "$APP_ROOT/app.new" && "$APP_ROOT/venv/bin/python" main.py --init fleet_control --config "$CONF_DIR/config.yaml" \
      --set "web_admin.auth_token=$TOKEN" --set "web_admin.secret_key=$SECRET" \
      --set "fleet_control.db_path=$DATA_DIR/fleet.db" --set "fleet_control.public_url=$PUBLIC_URL" </dev/null)
  # 端口 / host 沿用 preset（127.0.0.1:18798）；--set 写出的是字符串，不用它改数字项
  chown root:"$SVC_USER" "$CONF_DIR/config.yaml"; chmod 640 "$CONF_DIR/config.yaml"
  echo
  echo "=================================================================="
  echo " 主控 web_admin.auth_token（操作端 CHATX_FLEET_ADMIN_TOKEN，只显示这一次，请存密码库）："
  echo "   $TOKEN"
  echo "=================================================================="
  echo
else
  log "config exists, keeping $CONF_DIR/config.yaml"
fi
(cd "$APP_ROOT/app.new" && sudo -u "$SVC_USER" \
    AITR_CONFIG_PATH="$CONF_DIR/config.yaml" AITR_DATA_DIR="$DATA_DIR" \
    "$APP_ROOT/venv/bin/python" main.py --check --config "$CONF_DIR/config.yaml") \
  || fail "config --check failed"
# One-time: older builds wrote sqlite next to config.yaml. strict makes /etc read-only.
relocate_runtime_state

log "5/8 switch app (atomic)"
[ -d "$APP_ROOT/app" ] && { rm -rf "$APP_ROOT/app.prev"; mv "$APP_ROOT/app" "$APP_ROOT/app.prev"; }
mv "$APP_ROOT/app.new" "$APP_ROOT/app"

log "6/8 systemd"
install -m 644 "$HERE/chatx-fleet.service" /etc/systemd/system/"$SVC_NAME".service
systemctl daemon-reload
systemctl enable "$SVC_NAME" >/dev/null
systemctl restart "$SVC_NAME"

log "7/8 nginx snippet"
mkdir -p /etc/nginx/snippets
awk '/^# ───────── B\)/{exit} {print}' "$HERE/nginx-fleet.conf" | grep -v '^#' > /etc/nginx/snippets/chatx-fleet-main.conf
if grep -rq 'snippets/chatx-fleet-main.conf' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null; then
  nginx -t && systemctl reload nginx && log "nginx reloaded"
else
  log "NOTE: add   include /etc/nginx/snippets/chatx-fleet-main.conf;   inside the bd2026.cc server{} block, then: nginx -t && systemctl reload nginx"
fi

log "8/8 health"
sleep 3
for i in 1 2 3 4 5; do
  curl -fsS -m 5 "http://127.0.0.1:${PORT}/fleet/" -o /dev/null && { log "OK: controller up on 127.0.0.1:${PORT}"; break; }
  [ "$i" = 5 ] && { log "health failed, rolling back"; journalctl -u "$SVC_NAME" -n 30 --no-pager || true; rollback || true; exit 1; }
  sleep 3
done
check || log "some public checks failed (nginx / cert step pending)"
if [ "$CONSOLE_CHECK" = "0" ]; then
  log "console check SKIPPED (CONSOLE_CHECK=0)"
else
  ok=""
  for i in 1 2 3; do
    console_check && { ok=1; break; }
    sleep 3
  done
  [ -n "$ok" ] || { log "console check failed, rolling back"; rollback || true; exit 1; }
fi
log "done. logs: journalctl -u $SVC_NAME -f"
