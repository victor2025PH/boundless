# deploy_relay.ps1 —— 把 ChatX 官网中继部署/更新到 VPS（relay.bd2026.cc）。幂等，可重复跑。
# 用法（任意目录）：
#   powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1            # 全流程：上传 → venv → systemd → nginx → certbot → 体检
#   powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1 -CodeOnly  # 只更新代码并重启服务
#   powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1 -Status    # 只读核对：systemd 状态 + 本机/公网 healthz（不上传不重启）
# 前置：DNS *.bd2026.cc → VPS（已有泛解析）；VPS ubuntu 免密 sudo；本机 ~/.ssh/hualing_deploy（与官网部署同一把钥匙）。
# 不碰：官网 yuntech 服务、prod-katie.conf、其它站点；只新增 /home/ubuntu/relay 与 nginx 站点 relay.bd2026.cc。
[CmdletBinding()]
param(
    [string]$VpsHost = $(if ($env:VPS_HOST) { $env:VPS_HOST } else { '165.154.233.121' }),
    [string]$User    = $(if ($env:VPS_USER) { $env:VPS_USER } else { 'ubuntu' }),
    [string]$KeyFile = $(if ($env:VPS_KEY) { $env:VPS_KEY } else { Join-Path $HOME '.ssh/hualing_deploy' }),
    [string]$Fqdn    = 'relay.bd2026.cc',
    [string]$RegisterKey = '',        # 可选：限制谁能注册设备（写进 relay.env 的 RELAY_REGISTER_KEY；空＝首次即信任）
    [string]$VerifyFile = '',         # 可选：企微后台下载的 WW_verify_xxx.txt，上传到中继根目录供「可信域名」归属验证
    [switch]$CodeOnly,
    [switch]$Status                   # 只读核对（运维闭环用）：systemctl is-active/status、本机 healthz、公网 healthz；任何一项不健康 exit 1
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$here = $PSScriptRoot
$target = "$User@$VpsHost"
$sshOpts = @('-i', $KeyFile, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=20')

function Invoke-Remote([string]$script) {
    $script = $script -replace "`r`n", "`n"   # here-string 在 Windows 上带 CRLF，bash 会把 \r 当参数
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
    & ssh @sshOpts $target "echo $b64 | base64 -d | bash -s"
    if ($LASTEXITCODE -ne 0) { throw "remote step failed (exit $LASTEXITCODE)" }
}

if ($Status) {
    # 只读：不 scp、不 restart、不动 nginx/证书。prod_edge_watchdog 报 relay DOWN 时人工核对用。
    Write-Host "[relay] -Status 只读核对 ${target} / https://$Fqdn"
    $bad = 0
    try {
        Invoke-Remote @'
set +e
echo "--- systemctl is-active chatx-relay"; systemctl is-active chatx-relay
echo "--- systemctl status (tail)"; systemctl status --no-pager -n 8 chatx-relay 2>&1 | sed 's/^/    /'
echo "--- listener 127.0.0.1:18790"; ss -ltn 2>/dev/null | grep -E ':18790\b' || echo "    (no listener on 18790)"
echo "--- local healthz"; curl -fsS -m 8 http://127.0.0.1:18790/healthz; echo
echo "--- state file"; ls -l /home/ubuntu/relay/state/devices.json 2>/dev/null || echo "    (no devices.json yet)"
echo "--- nginx vhost"; ls -l /etc/nginx/sites-enabled/ 2>/dev/null | grep -F relay || echo "    (no relay vhost enabled?)"
systemctl is-active --quiet chatx-relay
'@
    } catch {
        Write-Host "[relay] 远端核对失败或 chatx-relay 非 active：$($_.Exception.Message)" -ForegroundColor Red
        $bad = 1
    }
    Write-Host "[relay] 公网 https://$Fqdn/healthz"
    try {
        $h = Invoke-RestMethod -Uri "https://$Fqdn/healthz" -TimeoutSec 20
        $line = "[relay] public OK  devices_online={0} devices_known={1} devices_stale={2} pruned_total={3} stale_sec={4}" -f `
            $h.devices_online, $h.devices_known, $h.devices_stale, $h.devices_pruned_total, $h.stale_sec
        if ($h.ok -eq $true) { Write-Host $line -ForegroundColor Green } else { Write-Host ($line + "  (ok!=true)") -ForegroundColor Red; $bad = 1 }
    } catch {
        Write-Host "[relay] 公网 healthz 失败：$($_.Exception.Message)" -ForegroundColor Red
        $bad = 1
    }
    exit $bad
}

Write-Host "[relay] ① 上传代码 → ${target}:/home/ubuntu/relay"
Invoke-Remote "mkdir -p /home/ubuntu/relay/state /home/ubuntu/relay/verify"
& scp @sshOpts (Join-Path $here 'app.py') (Join-Path $here 'requirements.txt') (Join-Path $here 'chatx-relay.service') (Join-Path $here 'nginx-relay.conf.tmpl') "${target}:/home/ubuntu/relay/"
if ($LASTEXITCODE -ne 0) { throw 'scp failed' }

Write-Host "[relay] ② venv + 依赖"
Invoke-Remote @'
set -e
cd /home/ubuntu/relay
if ! python3 -c "import venv" 2>/dev/null || ! dpkg -s python3-venv >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null
fi
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip >/dev/null
./venv/bin/pip install -q -r requirements.txt >/dev/null
./venv/bin/python -c "import fastapi, uvicorn, websockets; print('deps ok', fastapi.__version__, uvicorn.__version__)"
'@

if ($RegisterKey) {
    Write-Host "[relay] ②b 写 relay.env（RELAY_REGISTER_KEY）"
    Invoke-Remote "umask 077; printf 'RELAY_REGISTER_KEY=%s\n' '$RegisterKey' > /home/ubuntu/relay/relay.env"
}
if ($VerifyFile) {
    if (-not (Test-Path $VerifyFile)) { throw "VerifyFile not found: $VerifyFile" }
    $vn = Split-Path -Leaf $VerifyFile
    if ($vn -notmatch '^(WW|MP)_verify_[A-Za-z0-9]{4,64}\.txt$') { throw "VerifyFile 名字须形如 WW_verify_xxx.txt：$vn" }
    Write-Host "[relay] ②c 上传域名归属验证文件 $vn → https://$Fqdn/$vn"
    & scp @sshOpts $VerifyFile "${target}:/home/ubuntu/relay/verify/$vn"
    if ($LASTEXITCODE -ne 0) { throw 'scp verify file failed' }
}

Write-Host "[relay] ③ systemd chatx-relay"
Invoke-Remote @'
set -e
sudo cp /home/ubuntu/relay/chatx-relay.service /etc/systemd/system/chatx-relay.service
sudo systemctl daemon-reload
sudo systemctl enable chatx-relay >/dev/null 2>&1 || true
sudo systemctl restart chatx-relay
sleep 1.5
systemctl is-active chatx-relay
curl -fsS http://127.0.0.1:18790/healthz
echo
'@

if (-not $CodeOnly) {
    Write-Host "[relay] ④ 证书（certbot webroot）+ nginx 站点 $Fqdn"
    Invoke-Remote @"
set -e
FQDN='$Fqdn'
if [ ! -f "/etc/letsencrypt/live/`$FQDN/fullchain.pem" ]; then
  sudo certbot certonly --webroot -w /var/www/html -d "`$FQDN" --non-interactive --agree-tos --register-unsafely-without-email --quiet
fi
sed "s/__FQDN__/`$FQDN/g" /home/ubuntu/relay/nginx-relay.conf.tmpl | sudo tee "/etc/nginx/sites-available/`$FQDN" >/dev/null
sudo ln -sf "/etc/nginx/sites-available/`$FQDN" "/etc/nginx/sites-enabled/`$FQDN"
sudo nginx -t
sudo systemctl reload nginx
"@
}

Write-Host "[relay] ⑤ 公网体检 https://$Fqdn/healthz"
try {
    $h = Invoke-RestMethod -Uri "https://$Fqdn/healthz" -TimeoutSec 20
    Write-Host ("[relay] OK  devices_online={0}" -f $h.devices_online) -ForegroundColor Green
} catch {
    Write-Host "[relay] 公网体检失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
