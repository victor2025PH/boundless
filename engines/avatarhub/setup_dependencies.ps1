# ============================================================
#  系统级依赖一键安装脚本
#  安装顺序: Git -> Miniconda -> FFmpeg -> VS Build Tools
#  运行方式: 以管理员身份运行 PowerShell，执行此脚本
# ============================================================

$ErrorActionPreference = "Stop"
$DownloadDir = "$env:USERPROFILE\Downloads\ai_setup"
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

function Download-File($url, $dest) {
    if (Test-Path $dest) {
        Write-Host "[跳过] 文件已存在: $dest" -ForegroundColor Yellow
    } else {
        Write-Host "[下载] $url" -ForegroundColor Cyan
        $wc = New-Object System.Net.WebClient
        $wc.DownloadFile($url, $dest)
        Write-Host "[完成] 已保存到: $dest" -ForegroundColor Green
    }
}

# ── 1. Git for Windows ──────────────────────────────────────────
Write-Host "`n[1/4] 安装 Git for Windows..." -ForegroundColor Magenta
$gitInstaller = "$DownloadDir\git_installer.exe"
Download-File "https://github.com/git-for-windows/git/releases/download/v2.45.2.windows.1/Git-2.45.2-64-bit.exe" $gitInstaller
Start-Process -FilePath $gitInstaller -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS=`"icons,ext\reg\shellhere,assoc,assoc_sh`"" -Wait
Write-Host "[完成] Git 安装完毕" -ForegroundColor Green

# ── 2. Miniconda ────────────────────────────────────────────────
Write-Host "`n[2/4] 安装 Miniconda3..." -ForegroundColor Magenta
$condaInstaller = "$DownloadDir\miniconda_installer.exe"
Download-File "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe" $condaInstaller
Start-Process -FilePath $condaInstaller -ArgumentList "/InstallationType=JustMe /RegisterPython=0 /S /D=$env:USERPROFILE\Miniconda3" -Wait
Write-Host "[完成] Miniconda 安装完毕" -ForegroundColor Green

# 将 conda 加入当前会话 PATH
$condaPath = "$env:USERPROFILE\Miniconda3"
$env:PATH = "$condaPath;$condaPath\Scripts;$condaPath\Library\bin;$env:PATH"

# ── 3. FFmpeg ───────────────────────────────────────────────────
Write-Host "`n[3/4] 安装 FFmpeg..." -ForegroundColor Magenta
$ffmpegZip = "$DownloadDir\ffmpeg.zip"
Download-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $ffmpegZip
Write-Host "[解压] FFmpeg 到 C:\ffmpeg ..." -ForegroundColor Cyan
Expand-Archive -Path $ffmpegZip -DestinationPath "C:\" -Force
# 找到解压后的文件夹名（包含版本号）
$ffmpegFolder = Get-ChildItem "C:\" -Directory | Where-Object { $_.Name -like "ffmpeg-*" } | Select-Object -First 1
if ($ffmpegFolder) {
    Rename-Item -Path $ffmpegFolder.FullName -NewName "ffmpeg" -Force -ErrorAction SilentlyContinue
}
# 写入系统 PATH
$machinePath = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
if ($machinePath -notlike "*C:\ffmpeg\bin*") {
    [System.Environment]::SetEnvironmentVariable("PATH", "C:\ffmpeg\bin;" + $machinePath, "Machine")
    Write-Host "[完成] FFmpeg 已加入系统 PATH" -ForegroundColor Green
} else {
    Write-Host "[跳过] FFmpeg 路径已在 PATH 中" -ForegroundColor Yellow
}

# ── 4. Visual Studio Build Tools 2022 ──────────────────────────
Write-Host "`n[4/4] 安装 Visual Studio Build Tools 2022 (C++ 桌面开发)..." -ForegroundColor Magenta
Write-Host "      这一步耗时较长（约 5-15 分钟），请耐心等待..." -ForegroundColor Yellow
$vsInstaller = "$DownloadDir\vs_buildtools.exe"
Download-File "https://aka.ms/vs/17/release/vs_buildtools.exe" $vsInstaller
Start-Process -FilePath $vsInstaller -ArgumentList "--quiet --wait --norestart --nocache --installPath `"C:\BuildTools`" --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended" -Wait
Write-Host "[完成] VS Build Tools 安装完毕" -ForegroundColor Green

# ── 验证 ────────────────────────────────────────────────────────
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host " 安装验证" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$env:PATH = "$env:USERPROFILE\Miniconda3;$env:USERPROFILE\Miniconda3\Scripts;C:\ffmpeg\bin;C:\Program Files\Git\bin;$env:PATH"

try { $gitVer = git --version; Write-Host "[OK] $gitVer" -ForegroundColor Green } catch { Write-Host "[FAIL] Git 未检测到，请重启 PowerShell 后再验证" -ForegroundColor Red }
try { $condaVer = conda --version; Write-Host "[OK] $condaVer" -ForegroundColor Green } catch { Write-Host "[FAIL] Conda 未检测到，请重启 PowerShell 后再验证" -ForegroundColor Red }
try { $ffVer = ffmpeg -version 2>&1 | Select-Object -First 1; Write-Host "[OK] $ffVer" -ForegroundColor Green } catch { Write-Host "[FAIL] FFmpeg 未检测到" -ForegroundColor Red }

Write-Host "`n[提示] 所有安装完成后请关闭并重新打开终端，使 PATH 生效。" -ForegroundColor Yellow
Write-Host "[下一步] 重启终端后，运行 install_facefusion.bat 和 install_rvc.bat" -ForegroundColor Yellow
Write-Host "============================================================`n" -ForegroundColor Cyan
