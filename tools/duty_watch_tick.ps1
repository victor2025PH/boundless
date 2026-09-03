# duty_watch_tick.ps1 -- 值守监控单轮（计划任务 DutyWatchTick 每 2 分钟调用）。
#
# 为什么用计划任务而不是常驻循环：值守会话里起的后台进程会被宿主回收（0903 实测
# PowerShell/python 循环都活不过几分钟），而计划任务由 Windows 拉起，会话断了也照跑。
# 单轮逻辑全在 tools/duty_watch_loop.py（--once）：VPS 新包自动下载解包、群消息与
# mid 跳号、bug_events 全 kind、工单 max(id)，增量写 .ops\duty_watch_loop.log。
# 只读+只下载，绝不发消息。ASCII-only（PS5.1 GBK 陷阱）。
#
# 安装：schtasks /Create /TN DutyWatchTick /SC MINUTE /MO 2 /F ^
#   /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\boundless\tools\duty_watch_tick.ps1"
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$env:PYTHONIOENCODING = "utf-8"
& $py "D:\boundless\tools\duty_watch_loop.py" --once 2>&1 |
    Out-File "D:\chengjie-instances\.ops\duty_watch_tick.out.log" -Encoding utf8
