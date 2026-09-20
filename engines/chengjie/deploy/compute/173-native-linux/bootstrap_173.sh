#!/usr/bin/env bash
# =============================================================================
# 173 (yunsheng, RTX 5090 32G) 原生 Ubuntu 24.04 → vLLM 生产节点 bootstrap
#
# 用法（装完系统后，以安装期创建的 admin 用户登录）：
#   sudo bash bootstrap_173.sh                 # 全流程
#   sudo bash bootstrap_173.sh --skip-driver   # 驱动已装好时跳过
#   sudo ENFORCE_EAGER=1 bash bootstrap_173.sh # 原生图捕获仍出问题时的保底档
#
# 幂等：任何一步失败修复后重跑即可，已完成的步骤会自动跳过。
# 若驱动步骤要求重启：重启后重跑本脚本，会从模型步骤继续。
#
# 上游实证（WSL 轮，勿重复踩）：
#   - vLLM 0.27.0 + 5090 (sm_120) 出话没问题；WSL 里必须 enforce-eager，
#     原生 Linux 预期图捕获可用（先不加，出问题再 ENFORCE_EAGER=1 重跑）。
#   - WSL 的 VLLM_WSL2_ENABLE_PIN_MEMORY / vmIdleTimeout / socket handoff
#     补丁在原生 Linux 全部不需要。
# =============================================================================
set -euo pipefail

MODEL_NAME="${MODEL_NAME:-qwen25-abl-awq}"
MODEL_DIR="/opt/models"
VENV="/opt/vllm/venv"
PORT="${PORT:-8000}"
VLLM_VERSION="${VLLM_VERSION:-0.27.0}"   # WSL 实测版本；升级先过 verify_vllm.ps1
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"  # 应用侧 num_ctx=8192 + 出话 2048，留一倍余量
GPU_UTIL="${GPU_UTIL:-0.90}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
SKIP_DRIVER=0
[ "${1:-}" = "--skip-driver" ] && SKIP_DRIVER=1

# 117 控制机公钥（~/.ssh/cluster_controller，SSH 别名 yunsheng/llm173 沿用不变）
CONTROLLER_PUB="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICwPFIevCk2i2Fe5peSNagsSkxTn6rFaaKao1LqjYMKn 5090-controller"

log() { echo -e "\n=== [$(date +%H:%M:%S)] $* ==="; }

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }

# ---------------------------------------------------------------- [1/7] 基础包
log "[1/7] apt 基础包"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip rsync curl jq htop ubuntu-drivers-common ntfs-3g

# ------------------------------------------------------- [2/7] SSH 控制机公钥
log "[2/7] 注入 117 控制机公钥（保 SSH 别名连续性）"
ADMIN_USER="${SUDO_USER:-admin}"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "$ADMIN_HOME/.ssh"
touch "$ADMIN_HOME/.ssh/authorized_keys"
grep -qF "$CONTROLLER_PUB" "$ADMIN_HOME/.ssh/authorized_keys" || \
  echo "$CONTROLLER_PUB" >> "$ADMIN_HOME/.ssh/authorized_keys"
chown "$ADMIN_USER:$ADMIN_USER" "$ADMIN_HOME/.ssh/authorized_keys"
chmod 600 "$ADMIN_HOME/.ssh/authorized_keys"

# ---------------------------------------------------------------- [3/7] 驱动
if [ "$SKIP_DRIVER" -eq 0 ] && ! command -v nvidia-smi >/dev/null 2>&1; then
  log "[3/7] NVIDIA 驱动（Blackwell/sm_120 需 open 内核模块系）"
  apt-get install -y nvidia-driver-580-open || {
    log "归档没有 580-open，回落 ubuntu-drivers 自动选型"
    ubuntu-drivers install --gpgpu || ubuntu-drivers install
  }
  if ! nvidia-smi >/dev/null 2>&1; then
    log "驱动已装但内核模块未加载 → 需要重启。重启后重跑本脚本（会跳过本步）。"
    echo "  sudo reboot && sudo bash bootstrap_173.sh"
    exit 0
  fi
else
  log "[3/7] 驱动已就绪，跳过"
fi
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

# ------------------------------------------------- [4/7] 从旧 Windows D 盘取模型
log "[4/7] 模型落位 $MODEL_DIR/$MODEL_NAME"
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/$MODEL_NAME/config.json" ]; then
  SRC=""
  # 自动发现：扫所有 NTFS 分区找 models/qwen25-abl-awq（撤离脚本落的位置 D:\models\）
  MNT=/mnt/windata
  mkdir -p "$MNT"
  while read -r dev fstype; do
    [ "$fstype" = "ntfs" ] || continue
    mountpoint -q "$MNT" && umount "$MNT"
    mount -o ro "/dev/$dev" "$MNT" 2>/dev/null || continue
    if [ -f "$MNT/models/$MODEL_NAME/config.json" ]; then SRC="$MNT/models/$MODEL_NAME"; break; fi
    umount "$MNT"
  done < <(lsblk -rno NAME,FSTYPE)
  if [ -z "$SRC" ]; then
    echo "!! 没在任何 NTFS 分区找到 models/$MODEL_NAME（撤离目录）。lsblk 如下，人工挂载后重跑："
    lsblk -f
    exit 1
  fi
  log "从 $SRC rsync（19G，NVMe 间约 1-3 分钟）"
  rsync -a --info=progress2 "$SRC/" "$MODEL_DIR/$MODEL_NAME/"
  # 顺带把 A/B 备用模型也带上（有则拷，17G）
  if [ -d "$(dirname "$SRC")/qwen3awq" ] && [ ! -d "$MODEL_DIR/qwen3awq" ]; then
    rsync -a --info=progress2 "$(dirname "$SRC")/qwen3awq/" "$MODEL_DIR/qwen3awq/" || true
  fi
  mountpoint -q "$MNT" && umount "$MNT"
else
  log "模型已在位，跳过拷贝"
fi

# ---------------------------------------------------------------- [5/7] vLLM
log "[5/7] venv + vLLM $VLLM_VERSION（torch 轮子自带 CUDA，无需装 toolkit）"
mkdir -p "$(dirname "$VENV")"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -U pip
"$VENV/bin/python" -c "import vllm" 2>/dev/null || \
  "$VENV/bin/pip" install "vllm==$VLLM_VERSION"
"$VENV/bin/python" -c "import vllm, torch; print('vllm', vllm.__version__, '| torch', torch.__version__, '| cuda avail', torch.cuda.is_available())"

# ------------------------------------------------------------- [6/7] systemd
log "[6/7] systemd 服务（开机自启 + 崩溃自拉）"
EAGER_FLAG=""
[ "$ENFORCE_EAGER" = "1" ] && EAGER_FLAG=" --enforce-eager"
cat > /etc/systemd/system/vllm.service <<EOF
[Unit]
Description=vLLM OpenAI server ($MODEL_NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=VLLM_NO_USAGE_STATS=1
ExecStart=$VENV/bin/python -m vllm.entrypoints.openai.api_server \\
  --model $MODEL_DIR/$MODEL_NAME \\
  --served-model-name $MODEL_NAME \\
  --host 0.0.0.0 --port $PORT \\
  --max-model-len $MAX_MODEL_LEN \\
  --gpu-memory-utilization $GPU_UTIL$EAGER_FLAG
Restart=always
RestartSec=5
# 模型冷载窗口内别让 systemd 误杀
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# 2026-08-15 启停权移交：unit 保持 disabled、只 start 不 enable——开机拉起归中枢(176)
# 模式执行器 AvatarHubVllmKeepwarm（回执 D:\chengjie-instances\.ops\REPLY_from_zhongshu_20260815.md）。
# 重装后若中枢执行器尚未接管本机，先发 ASK 联络单再议，勿擅自 enable。
systemctl disable vllm >/dev/null 2>&1 || true
systemctl restart vllm

# ---------------------------------------------------------------- [7/7] 验收
log "[7/7] 等服务就绪（冷载约 1-3 分钟）"
for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then break; fi
  [ "$i" -eq 90 ] && { echo "!! 15 分钟未就绪：journalctl -u vllm -n 100"; exit 1; }
  sleep 10
done
curl -fsS "http://127.0.0.1:$PORT/v1/models" | jq -r '.data[].id'
log "冒烟对话"
T0=$(date +%s.%N)
RESP=$(curl -fsS "http://127.0.0.1:$PORT/v1/chat/completions" -H 'Content-Type: application/json' -d "{
  \"model\": \"$MODEL_NAME\",
  \"messages\": [{\"role\":\"user\",\"content\":\"用两句地道的中文口语夸一下今晚的月色\"}],
  \"max_tokens\": 120, \"temperature\": 0.7}")
T1=$(date +%s.%N)
echo "$RESP" | jq -r '.choices[0].message.content'
TOK=$(echo "$RESP" | jq -r '.usage.completion_tokens')
echo "completion_tokens=$TOK, wall=$(echo "$T1 $T0" | awk '{printf "%.1f", $1-$2}')s, tok/s=$(echo "$TOK $T1 $T0" | awk '{printf "%.1f", $1/($2-$3)}')"
log "全部完成。下一步：在 117 上跑 deploy\\compute\\173-native-linux\\verify_vllm.ps1 做 LAN 侧验收"
