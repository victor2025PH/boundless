# verify_vllm.ps1 - P3 acceptance probe for 173 vLLM (ASCII output only)
# Usage: powershell -ExecutionPolicy Bypass -File deploy\compute\verify_vllm.ps1 [-BaseUrl http://192.168.0.173:8001] [-MinTokS 25]
param(
    [string]$BaseUrl = "http://192.168.0.173:8001",
    [string]$Model = "chatx",
    [double]$MinTokS = 25.0
)
$ErrorActionPreference = "Stop"
$fail = 0

# 1) /v1/models
try {
    $m = Invoke-RestMethod -Uri "$BaseUrl/v1/models" -TimeoutSec 8
    $ids = @($m.data | ForEach-Object { $_.id })
    if ($ids -contains $Model) { "PASS models: $($ids -join ',')" }
    else { "FAIL models: expected '$Model' got '$($ids -join ',')'"; $fail = 1 }
} catch { "FAIL models: $($_.Exception.Message)"; $fail = 1 }

# 2) chat completion + tok/s
if ($fail -eq 0) {
    # untimed warmup first: vLLM JIT-compiles kernels on first long generation after
    # restart (7 tok/s cold vs 56-60 warm measured 2026-08-13); we accept WARM speed.
    try {
        $wb = @{ model = $Model; max_tokens = 300; temperature = 0.7
                 messages = @(@{ role = "user"; content = "warmup: write a 200-word story in Chinese." }) } | ConvertTo-Json -Depth 5 -Compress
        Invoke-RestMethod -Uri "$BaseUrl/v1/chat/completions" -Method POST -ContentType "application/json" -Body $wb -TimeoutSec 120 | Out-Null
        "INFO warmup completion done"
    } catch { "INFO warmup failed (continuing): $($_.Exception.Message)" }
    # long-form prompt: short completions underestimate tok/s (fixed overhead dominates)
    $body = @{
        model = $Model
        messages = @(@{ role = "user"; content = "Write a vivid 200-word story in Chinese about a rainy night in a small town. Do not stop early." })
        max_tokens = 300
        temperature = 0.7
    } | ConvertTo-Json -Depth 5 -Compress
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-RestMethod -Uri "$BaseUrl/v1/chat/completions" -Method POST -ContentType "application/json" -Body $body -TimeoutSec 90
        $sw.Stop()
        $ct = [int]$r.usage.completion_tokens
        $secs = [math]::Max($sw.Elapsed.TotalSeconds, 0.001)
        $toks = [math]::Round($ct / $secs, 1)
        $preview = ($r.choices[0].message.content -replace "\s+", " ")
        if ($preview.Length -gt 60) { $preview = $preview.Substring(0, 60) + "..." }
        "PASS chat: $ct tokens in $([math]::Round($secs,1))s = $toks tok/s | $preview"
        if ($toks -lt $MinTokS) { "FAIL speed: $toks tok/s < floor $MinTokS (Blackwell should do ~40+ eager)"; $fail = 1 }
        else { "PASS speed: $toks tok/s >= $MinTokS" }
    } catch { "FAIL chat: $($_.Exception.Message)"; $fail = 1 }
}

if ($fail -eq 0) { "VERIFY_VLLM_OK" } else { "VERIFY_VLLM_FAIL" }
exit $fail
