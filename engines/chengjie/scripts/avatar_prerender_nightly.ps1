# avatar_prerender_nightly.ps1 — 夜间批量预渲染所有 avatar_clone 人设的固定台词
# 计划任务：AvatarPrerenderNightly（每日 04:30，低峰跑 7858 批量，RTF≈2.8 不占白天 GPU）。
# 台词库：config/prerender_lines/（_common.txt 共用 + <persona>.txt 专属）。
# 幂等：已渲染台词自动跳过；人设换参考音后需手动 --force 重渲一次。
# 日志：logs/prerender/nightly_<ts>.log（保留最近 14 份）。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 日志编码统一 UTF-8：PowerShell 5 的 *>> 会按 UTF-16 混写中文输出成乱码
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = Join-Path $root "logs\prerender"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "nightly_$ts.log"

"[nightly] start $ts root=$root" | Out-File $log -Encoding utf8

# 渲染（CLI 自带 7858 就绪等待 + 批级自愈重试）
python -m scripts.avatar_prerender --all-personas 2>&1 | Out-File $log -Append -Encoding utf8
$rc = $LASTEXITCODE
"[nightly] prerender exit=$rc" | Out-File $log -Append -Encoding utf8

# 顺手对 7852 预热一轮 register_spk（夜间服务若重启过，speaker 缓存已丢；
# 便宜且幂等，白天首句延迟更稳）
python -c "import sys; sys.path.insert(0,'.'); import yaml; from pathlib import Path; d=yaml.safe_load(Path('config/config.yaml').read_text(encoding='utf-8')) or {}; o=yaml.safe_load(Path('config/config.local.yaml').read_text(encoding='utf-8')) or {}; f=lambda a,b:[a.__setitem__(k,(f(a[k],v) or v) if isinstance(v,dict) and isinstance(a.get(k),dict) else v) for k,v in b.items()] and a or a; f(d,o); from src.ai.avatar_voice import warmup_personas; print('warmed:', warmup_personas(d))" 2>&1 | Out-File $log -Append -Encoding utf8
"[nightly] warmup exit=$LASTEXITCODE" | Out-File $log -Append -Encoding utf8

# 音色相似度周期抽检（campplus CPU 声纹比对；灾难级漂移=exit 1 写进日志）
python -m scripts.voice_similarity_probe 2>&1 | Out-File $log -Append -Encoding utf8
"[nightly] similarity probe exit=$LASTEXITCODE" | Out-File $log -Append -Encoding utf8

# 参考音质量审计（纯 CPU，产物 logs/reference_audio_audit.json → avatar-status 看板）
python -m scripts.reference_audio_audit 2>&1 | Out-File $log -Append -Encoding utf8
"[nightly] reference audit exit=$LASTEXITCODE" | Out-File $log -Append -Encoding utf8

# 清理 14 份以前的旧日志
Get-ChildItem $logDir -Filter "nightly_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

"[nightly] done" | Out-File $log -Append -Encoding utf8
exit $rc
