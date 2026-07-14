@echo off
chcp 65001 >nul
REM ============================================================
REM  跨机显存互斥 · B 机示例启动器
REM  用本脚本包裹你的 GPU 程序：运行期间独占 A 机(avatar_hub)整张显卡，
REM  退出后自动归还、A 恢复服务。把下面三处改成你的实际值即可。
REM ============================================================

REM A 机 Hub 地址（avatar_hub 监听 9000）
set "HUB=http://192.168.1.10:9000"

REM A 机的 AVATARHUB_API_TOKEN（A 未设令牌则留空）。也可改为从环境变量读取。
set "TOKEN=PUT_A_AVATARHUB_API_TOKEN_HERE"

REM 你的 GPU 程序需要的空闲显存(MB)，按你的程序实际需求填；留 0 让 A 腾空到 ~90%%
set "NEED_MB=28000"

REM 把 --  后面替换成你要运行的 GPU 程序及参数（示例：python train.py --epochs 10）
python "%~dp0gpu_peer_lease.py" --hub %HUB% --token %TOKEN% --need-mb %NEED_MB% --ttl 30 --wait 1800 -- python your_gpu_program.py

REM 退出码透传：被包裹程序的退出码即本脚本退出码
exit /b %ERRORLEVEL%
