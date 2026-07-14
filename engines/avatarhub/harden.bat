@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo ============================================================
echo   AvatarHub 服务面加固 — 一键令牌生成 / 多机下发指引
echo ============================================================
echo.

rem Resolve local conda python (prefer facefusion env, fall back to PATH python)
set "PYEXE=python"
for %%P in (
  "%USERPROFILE%\Miniconda3\envs\facefusion\python.exe"
  "%USERPROFILE%\Anaconda3\envs\facefusion\python.exe"
  "C:\ProgramData\Miniconda3\envs\facefusion\python.exe"
) do (
  if exist "%%~P" set "PYEXE=%%~P"
)

if not exist "secrets" mkdir "secrets"

if not exist "secrets\service_token.txt" (
  "%PYEXE%" -c "import secrets,pathlib;pathlib.Path('secrets/service_token.txt').write_text(secrets.token_urlsafe(32),encoding='ascii')"
  if errorlevel 1 (
    echo [错误] 令牌生成失败：请确认 Python 可用。
    pause & exit /b 1
  )
  echo [harden] 已生成新令牌 secrets\service_token.txt
) else (
  echo [harden] 已存在 secrets\service_token.txt（复用，不覆盖）
)

set /p TOK=<secrets\service_token.txt

rem 管理面令牌（锁 hub :9000 控制台；hub 优先 env AVATARHUB_API_TOKEN，其次回退读 secrets\api_token.txt）
if not exist "secrets\api_token.txt" (
  "%PYEXE%" -c "import secrets,pathlib;pathlib.Path('secrets/api_token.txt').write_text(secrets.token_urlsafe(24),encoding='ascii')"
  if errorlevel 1 (
    echo [错误] 管理令牌生成失败：请确认 Python 可用。
    pause & exit /b 1
  )
  echo [harden] 已生成新管理令牌 secrets\api_token.txt（hub 重启即自动启用；回环本机不受影响）
) else (
  echo [harden] 已存在 secrets\api_token.txt（复用，不覆盖）
)
set /p APITOK=<secrets\api_token.txt

rem Detect local LAN IP (reference for the "service host allows hub IP" option)
for /f "delims=" %%I in ('"%PYEXE%" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect((\"8.8.8.8\",80));print(s.getsockname()[0]);s.close()"') do set "MYIP=%%I"

echo.
echo ===================== 令牌 =====================
echo   AVATARHUB_SERVICE_TOKEN = !TOK!
echo   本机局域网 IP            = !MYIP!
echo ================================================
echo.
echo 【方案A·共享令牌（推荐，全机统一）】
echo   1) 把上面这串 token 复制到【每一台机器】的 secrets.bat：
echo        set AVATARHUB_SERVICE_TOKEN=!TOK!
echo      （或把 secrets\service_token.txt 拷到每台机器的同一相对路径）
echo   2) 重启各机的服务（含 hub）。hub 调用各服务会自动带令牌；
echo      其它机器无令牌直连将被拒（回环本机不受影响）。
echo.
echo 【方案B·IP 白名单（多机最省心，hub 侧零改动）】
echo   在【每台"服务机"】的 secrets.bat 放行 hub 的局域网 IP：
echo        set AVATARHUB_SERVICE_ALLOW_IPS=^<hub机器的局域网IP^>
echo   （本机即 hub 时，hub 调本机服务走回环，天然放行）
echo.
echo 【方案C·管理面令牌（锁 :9000 控制台，防同网他人改配置/增删角色/操纵服务）】
echo   本次已生成管理令牌（存 secrets\api_token.txt，hub 重启即自动启用）：
echo        AVATARHUB_API_TOKEN = !APITOK!
echo   * 本机(回环)照常免令牌；其它机器打开 /ops 或 /ui 顶部会弹输入条，输入上面令牌即解锁
echo     （浏览器记住，同源各页通用）。也可在各机 secrets.bat 里 set AVATARHUB_API_TOKEN=... 固化。
echo   * 更强隔离：secrets.bat 里 set AVATARHUB_BIND=127.0.0.1 =^> hub 只绑本机，对外经反代/SSH 隧道。
echo   * 自查：浏览器开 http://hub:9000/api/security/posture 或看 /ops「安全体检」卡片。
echo.
echo 【验证】跨机用浏览器/curl 直连某服务(如 http://本机IP:7855/v1/...) 应 401；
echo        带头 X-AH-Svc: 上面的令牌 应 200；/health 始终放行。
echo.
echo 说明：未配置 token 也未配置 allowlist 时，服务面不拦截（保持现状）。
echo       secrets\ 已在 .gitignore，令牌不会入库。
echo.
echo ============================================================
echo 【一键自动下发（推荐）】上面的手动步骤已封装为脚本：
echo   powershell -ExecutionPolicy Bypass -File harden_remote.ps1
echo     -Mode deploy    分发service_auth+注入鉴权+写令牌/白名单+固化env+重启+验证
echo     -Mode verify    只复验(无令牌应401 / hub与带令牌应放行)
echo     -Mode rollback  应急回滚(还原 .bak_auth + 清 env)
echo   服务拓扑写在脚本顶部 $MAP，新增机器在此追加一行即可。
echo ============================================================
echo.
pause
endlocal
