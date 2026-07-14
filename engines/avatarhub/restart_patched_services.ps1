# 重启本次内存优化中已打补丁的 4 个服务（latentsync / lipsync / emotion_tts / enhance）。
# 用 PowerShell 启动以正确处理中文路径(C:\模仿音色)。vcam / avatar_hub 不动。
# 用法：右键“使用 PowerShell 运行”，或：  powershell -ExecutionPolicy Bypass -File restart_patched_services.ps1
$ErrorActionPreference = "SilentlyContinue"
$base = $PSScriptRoot   # 项目根随脚本位置推导(2026-07-11 项目已迁 D:\projects,勿再写死盘符)

Write-Host "[1/3] 结束旧进程(按命令行精确匹配)..."
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'latentsync_server\.py|lipsync_server\.py|emotion_tts_server\.py|enhance_server\.py' } |
    ForEach-Object { Write-Host ("  kill PID=" + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 3

Write-Host "[2/3] 重新启动(各自 conda 环境, 最小化窗口)..."
$svcs = @(
  @{ t = "LatentSync"; py = "C:\Users\user\Miniconda3\envs\latentsync\python.exe";  s = "$base\latentsync_server.py" },
  @{ t = "LipSync";    py = "C:\Users\user\Miniconda3\envs\musethepeak\python.exe"; s = "$base\lipsync_server.py" },
  @{ t = "Enhance";    py = "C:\Users\user\Miniconda3\envs\facefusion\python.exe";  s = "$base\enhance_server.py" },
  @{ t = "EmotionTTS"; py = "C:\Users\user\Miniconda3\envs\cosyvoice\python.exe";   s = "$base\emotion_tts_server.py" }
)
foreach ($v in $svcs) {
    $inner = "title $($v.t) & chcp 65001 >nul & `"$($v.py)`" `"$($v.s)`""
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $inner -WorkingDirectory $base -WindowStyle Minimized
    Write-Host "  已启动: $($v.t)"
    Start-Sleep -Seconds 2
}

Write-Host "[3/3] 已重新拉起。约 60-120 秒后模型加载完成。健康检查端口："
Write-Host "  LatentSync :8091  LipSync :8090  Enhance :8092  EmotionTTS :7852"
