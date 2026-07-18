# preflight_instance.ps1 — 通译 LingoX 实例拉起前预检（只读探测；唯一写动作=可写性探针，用完即删）
# 用法: powershell -ExecutionPolicy Bypass -File .\preflight_instance.ps1 [-DataDir <数据根>] [-Ports 18899,18887]
# 检查项：
#   1) python 可用（版本打印）
#   2) engines\chengjie\requirements.txt 逐包 import 探测（缺哪个列哪个）
#   3) 引擎全链导入烟测（import main —— 真正的"能不能起"硬闸；软依赖缺失只记 WARN）
#   4) 实例端口空闲（默认通译 18899 主位 + 18887 备用位）
#   5) AITR_DATA_DIR 目标可写（写探针文件后删除；目录不存在则临时建再删，不留痕）
# 输出 PASS/WARN/FAIL 清单；退出码 0=可拉起（允许 WARN） 1=有 FAIL。

[CmdletBinding()]
param(
    [string]$DataDir   = '',                 # 空 = 默认 tongyi\data；试点传临时目录
    # 声明为 string[] 再自行拆分：powershell -File 传 "-Ports 18799,18787" 时整串按
    # 千分位数字解析（[int[]] 会得到单元素 1879918787），string[] + 手工 split 两种
    # 调用形态（-File 逗号串 / -Command 真数组）都正确。
    [string[]]$Ports   = @('18899', '18887'),
    [string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# 端口参数归一化（兼容 "18899,18887" 单串与 18899,18887 数组两种传法）
$PortList = @()
foreach ($p in $Ports) {
    foreach ($tok in ("$p" -split '[,\s]+')) {
        if ($tok) { $PortList += [int]$tok }
    }
}
$Ports = $PortList

$RepoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EngineDir = Join-Path $RepoRoot 'engines\chengjie'
if (-not $DataDir) { $DataDir = Join-Path $PSScriptRoot 'tongyi\data' }

$checks = New-Object System.Collections.ArrayList
function Add-Check([string]$item, [string]$status, [string]$detail) {
    [void]$checks.Add([pscustomobject]@{ item = $item; status = $status; detail = $detail })
    $color = switch ($status) { 'PASS' {'Green'} 'WARN' {'Yellow'} 'FAIL' {'Red'} default {'Gray'} }
    Write-Host ("  [{0,-4}] {1,-22} {2}" -f $status, $item, $detail) -ForegroundColor $color
}

Write-Host "=============== preflight — 通译 LingoX 实例预检 ==============="
Write-Host "  引擎: $EngineDir"
Write-Host "  数据根(AITR_DATA_DIR 目标): $DataDir"
Write-Host "  端口: $($Ports -join ', ')"
Write-Host '----------------------------------------------------------------'

# ── 1) python 可用 ────────────────────────────────────────────────────────
$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) {
    Add-Check 'python' 'FAIL' "PATH 里找不到 $PythonExe（stack.json runtime=python 同一约定）"
} else {
    $ver = (& $PythonExe --version 2>&1 | Out-String).Trim()
    Add-Check 'python' 'PASS' "$ver @ $($py.Source)"
}

# ── 2) requirements.txt 逐包 import 探测（单 python 进程内逐个 import，含 C 扩展/重包）──
$reqFile = Join-Path $EngineDir 'requirements.txt'
$depMiss = @()
if (-not (Test-Path $reqFile)) {
    Add-Check 'requirements.txt' 'FAIL' "文件不存在: $reqFile"
} elseif ($py) {
    $probePy = @'
import importlib, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
req = sys.argv[1]
# pip 包名 -> import 模块名（其余按 - 转 _ 规则）
MAP = {
    "pyyaml": "yaml", "python-multipart": "multipart", "python-docx": "docx",
    "pdfminer.six": "pdfminer", "faster-whisper": "faster_whisper",
    "openai-whisper": "whisper", "google-genai": "google.genai", "pillow": "PIL",
}
names, seen = [], set()
for raw in open(req, encoding="utf-8", errors="replace"):
    s = raw.strip()
    if not s or s.startswith("#"):
        continue
    for sep in (">=", "==", "<=", "~=", "!=", ">", "<", "[", ";", " "):
        i = s.find(sep)
        if i > 0:
            s = s[:i]
    s = s.strip()
    if s and s.lower() not in seen:
        seen.add(s.lower()); names.append(s)
miss = 0
for pkg in names:
    mod = MAP.get(pkg.lower(), pkg.replace("-", "_"))
    try:
        importlib.import_module(mod)
        print("DEP_OK %s" % pkg, flush=True)
    except BaseException as e:
        miss += 1
        print("DEP_MISS %s (import %s) :: %s: %s" % (pkg, mod, type(e).__name__, str(e)[:100]), flush=True)
print("DEP_SUMMARY total=%d miss=%d" % (len(names), miss), flush=True)
'@
    $probePath = Join-Path $env:TEMP ("preflight_dep_probe_{0}.py" -f $PID)
    [IO.File]::WriteAllText($probePath, $probePy, [Text.UTF8Encoding]::new($false))
    Write-Host "  ... 依赖探测中（含 whisper/torch 级重包导入，约 1 分钟）" -ForegroundColor DarkGray
    $depOk = 0
    try {
        & $PythonExe -u $probePath $reqFile 2>&1 | ForEach-Object {
            $line = "$_"
            if     ($line -like 'DEP_OK *')   { $depOk++ }
            elseif ($line -like 'DEP_MISS *') { $depMiss += $line.Substring(9) }
        }
    } finally {
        Remove-Item $probePath -Force -ErrorAction SilentlyContinue
    }
    if ($depMiss.Count -eq 0) {
        Add-Check 'requirements 依赖' 'PASS' "requirements.txt 全部 $depOk 个包可导入"
    } else {
        # 缺包先记 WARN；是否致命由下面的全链烟测（import main）判定——
        # 引擎对大量可选依赖有 try/except 软防护（见 requirements.txt 各条注释）
        foreach ($m in $depMiss) { Add-Check '依赖缺失' 'WARN' $m }
    }
}

# ── 3) 引擎全链导入烟测（import main = 启动硬闸）───────────────────────────
if ($py) {
    $smokeDir = Join-Path $env:TEMP ("preflight_smoke_{0}" -f $PID)
    New-Item -ItemType Directory -Force -Path $smokeDir | Out-Null
    Push-Location $smokeDir
    try {
        $env:PYTHONIOENCODING = 'utf-8'
        $smoke = (& $PythonExe -u -c "import sys; sys.path.insert(0, r'$EngineDir'); import main; print('MAIN_IMPORT_OK')" 2>&1 | Out-String)
    } finally {
        Pop-Location
        Remove-Item $smokeDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($smoke -match 'MAIN_IMPORT_OK') {
        $note = '引擎 main.py 全依赖链可导入'
        if ($depMiss.Count) { $note += "（上列缺包均为软依赖，引擎有 try/except 防护，对应功能降级）" }
        Add-Check '全链导入烟测' 'PASS' $note
    } else {
        $tail = (($smoke -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 3) -join ' | ')
        Add-Check '全链导入烟测' 'FAIL' "import main 失败: $tail"
    }
}

# ── 4) 端口空闲 ──────────────────────────────────────────────────────────
foreach ($port in $Ports) {
    $own = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if (-not $own.Count) {
        Add-Check "端口 $port" 'PASS' '空闲'
    } else {
        $pids = @($own | Select-Object -ExpandProperty OwningProcess -Unique)
        $who = foreach ($holderPid in $pids) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
            if ($p) { "$holderPid($($p.Name))" } else { "$holderPid" }
        }
        Add-Check "端口 $port" 'FAIL' "已被占用 PID=$($who -join ', ')"
    }
}

# ── 5) AITR_DATA_DIR 目标可写（探针写后即删，目录不存在则临时建再删）────────
$dirExisted = Test-Path $DataDir
try {
    if (-not $dirExisted) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
    $probeFile = Join-Path $DataDir (".preflight_write_probe_{0}" -f $PID)
    Set-Content -Path $probeFile -Value 'probe' -Encoding ASCII
    Remove-Item $probeFile -Force
    $note = if ($dirExisted) { '目录已存在且可写' } else { '目录可创建且可写（探针目录已删，不留痕）' }
    Add-Check '数据根可写' 'PASS' "$DataDir — $note"
} catch {
    Add-Check '数据根可写' 'FAIL' "$DataDir 写入失败: $($_.Exception.Message)"
} finally {
    if (-not $dirExisted -and (Test-Path $DataDir)) {
        Remove-Item $DataDir -Force -ErrorAction SilentlyContinue   # 仅删本脚本刚建的空目录
    }
}

# ── 汇总 ─────────────────────────────────────────────────────────────────
Write-Host '----------------------------------------------------------------'
$nFail = @($checks | Where-Object status -eq 'FAIL').Count
$nWarn = @($checks | Where-Object status -eq 'WARN').Count
if ($nFail -eq 0) {
    Write-Host "  PREFLIGHT: PASS（$($checks.Count) 项检查，$nWarn 个警告）— 可按 README §3.1 初始化并拉起" -ForegroundColor Green
    exit 0
} else {
    Write-Host "  PREFLIGHT: FAIL（$nFail 项失败 / $nWarn 个警告）— 先处理上列 FAIL 项" -ForegroundColor Red
    exit 1
}
