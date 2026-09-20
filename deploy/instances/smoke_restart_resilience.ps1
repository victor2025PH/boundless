# smoke_restart_resilience.ps1 — read-only check that Phase3–7 anti-flap surface is alive.
# Does NOT restart anything.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\smoke_restart_resilience.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_restart_cooldown.ps1')

Write-Host '=== chengjie restart-resilience smoke (read-only) ===' -ForegroundColor Cyan

$snapPath = 'D:\chengjie-instances\.ops\last_status.json'
# Phase7: status always writes UTF-8 snapshot (human or -Json)
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'status_instances.ps1') `
    -SnapshotPath $snapPath | Out-Host

if (-not (Test-Path -LiteralPath $snapPath)) {
    Write-Host 'FAIL: snapshot missing after status_instances' -ForegroundColor Red
    exit 2
}

$raw = Get-Content -LiteralPath $snapPath -Raw -Encoding UTF8
$obj = $null
try { $obj = $raw | ConvertFrom-Json } catch {
    Write-Host 'FAIL: snapshot JSON unreadable' -ForegroundColor Red
    exit 2
}

# Phase8: note_en (ASCII) required; zh note must keep real CJK when present (not health-only false pass)
$noteEnOk = $false
$cjkOk = $false
foreach ($inst in @($obj.instances)) {
    $noteEn = [string]$inst.note_en
    $note = [string]$inst.note
    if ($noteEn -match 'HTTP|warming|unresponsive|not running|alive|seat-ready|foreign') {
        $noteEnOk = $true
    }
    if ($note -match '[\u4e00-\u9fff]') { $cjkOk = $true }
}
if (-not $noteEnOk) {
    Write-Host 'FAIL: snapshot missing note_en (Phase8 encoding-proof field)' -ForegroundColor Red
    Write-Host ("  sample: note={0} note_en={1}" -f $(@($obj.instances)[0].note), $(@($obj.instances)[0].note_en)) -ForegroundColor DarkGray
    exit 2
}
Write-Host '  snapshot note_en: OK' -ForegroundColor Green
if ($cjkOk) {
    Write-Host '  snapshot UTF-8 CJK notes: OK' -ForegroundColor Green
} else {
    # GO path usually has CJK; warming/ASCII-only notes are fine
    Write-Host '  snapshot CJK notes: skipped (ASCII-only notes this run)' -ForegroundColor DarkGray
}

# Python round-trip: note_en must be pure ASCII (ops SSOT)
try {
    $py = @'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
d=json.loads(p.read_text(encoding="utf-8"))
rows=d.get("instances") or []
assert rows, "no instances"
for r in rows:
    en=str(r.get("note_en") or "")
    assert en, "empty note_en"
    assert all(ord(c) < 128 for c in en), repr(en)
print("py_ok", len(rows))
'@
    $pyFile = Join-Path $env:TEMP 'chengjie_smoke_note_en.py'
    Set-Content -LiteralPath $pyFile -Value $py -Encoding ASCII
    $pyOut = & python $pyFile $snapPath 2>&1
    if ($LASTEXITCODE -ne 0) { throw "python check failed: $pyOut" }
    Write-Host ("  python note_en round-trip: OK ({0})" -f $pyOut) -ForegroundColor Green
} catch {
    Write-Host ("FAIL: python note_en check: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 2
}

$bad = 0
foreach ($inst in $obj.instances) {
    $phase = [string]$inst.http_phase
    $cd = Test-RestartCooldownActive -Instance $inst.id
    $flap = Test-RestartFlap -Instance $inst.id
    Write-Host ("  {0}: phase={1} login_ready={2} cooldown={3} flap={4}({5})" -f `
        $inst.id, $phase, $inst.login_ready, $cd.active, $flap.flapping, $flap.count)
    if ($phase -eq 'unresponsive') { $bad = 1 }
    if ($flap.flapping) { $bad = 1 }
}

Write-Host ("  status snapshot: {0}" -f $snapPath) -ForegroundColor DarkGray

# Lightweight contract: adaptive poll + coalesce markers present in inbox template
$inbox = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'engines\chengjie\src\web\templates\unified_inbox.html'
if (Test-Path $inbox) {
    $src = Get-Content -LiteralPath $inbox -Raw -Encoding UTF8
    foreach ($marker in @('_pollIntervalMs', '_coalesceAsync', '_threadInflight')) {
        if ($src -notlike "*$marker*") {
            Write-Host ("FAIL: inbox missing {0}" -f $marker) -ForegroundColor Red
            exit 2
        }
    }
    Write-Host '  inbox poll/coalesce markers: OK' -ForegroundColor Green
}

if ($bad -eq 0) {
    Write-Host 'SMOKE OK' -ForegroundColor Green
    exit 0
}
Write-Host 'SMOKE WARN - see unresponsive/flap above' -ForegroundColor Yellow
exit 1
