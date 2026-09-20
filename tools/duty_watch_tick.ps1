# duty_watch_tick.ps1 -- 值守监控单轮（计划任务 DutyWatchTick 每 2 分钟调用）。
#
# 为什么用计划任务而不是常驻循环：值守会话里起的后台进程会被宿主回收（0903 实测
# PowerShell/python 循环都活不过几分钟），而计划任务由 Windows 拉起，会话断了也照跑。
# 单轮逻辑全在 tools/duty_watch_loop.py（--once）：VPS 新包自动下载解包、群消息与
# mid 跳号、bug_events 全 kind、工单 max(id)，增量写 .ops\duty_watch_loop.log。
# watch_loop 只读+只下载，绝不发消息；本 tick 唯一的发送面是第二步 duty_channel_reminder（见下）。
# ASCII-only（PS5.1 GBK 陷阱）。
#
# 安装（2026-09-04 起必须经 run_hidden.vbs）：交互式分钟任务直指 powershell.exe，
# 控制台窗口在 -WindowStyle Hidden 生效前就已创建，本任务又没带该参数 → 每 2 分钟
# 在桌面上真开一个蓝窗、停留约 40 秒（python 跑完才关）。用 wscript(GUI 宿主) 拉起
# 才是零闪烁；117 只剩 32 位 wscript，故 SysWOW64 + Sysnative 配对，见该 vbs 头部。
#   Execute  : C:\Windows\SysWOW64\wscript.exe
#   Arguments: //B //Nologo "D:\workspace\boundless\deploy\instances\run_hidden.vbs"
#              C:\Windows\Sysnative\WindowsPowerShell\v1.0\powershell.exe
#              -NoProfile -ExecutionPolicy Bypass -File D:\boundless\tools\duty_watch_tick.ps1
# 触发器：SC MINUTE /MO 2，Interactive/Administrator。旧任务 XML 备份在
# D:\chengjie-instances\.ops\task_backups_20260904\
#
# 2026-09-06 boss rule: testers must submit bugs via Cursor field-agent report, not the group.
# Second step below (tools/duty_channel_reminder.py) is the ONLY sender on this tick:
# it replies once per reporter per 6h when the bot sees a group bug submission, and
# posts a "received <code> -> ticket #N" receipt for newly downloaded reports. Runs after
# the loop so the loop's freshly downloaded codes are in its state. Own log: .ops\duty_channel_reminder.log
# L-7 A (2026-09-06 11:xx): before each receipt the reminder calls tools/duty_auto_ticket.py
# (attach by #N / linked code / same-reporter-30min topic, else create a ticket); it writes
# tmp_diag\<code>\ticket.txt + duty_evidence.jsonl + bug_events and returns the ticket number
# for the receipt text. Own log: .ops\duty_auto_ticket.log. It never sends messages itself.
$ErrorActionPreference = "SilentlyContinue"
$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$env:PYTHONIOENCODING = "utf-8"
& $py "D:\boundless\tools\duty_watch_loop.py" --once 2>&1 |
    Out-File "D:\chengjie-instances\.ops\duty_watch_tick.out.log" -Encoding utf8
& $py "D:\boundless\tools\duty_channel_reminder.py" 2>&1 |
    Out-File "D:\chengjie-instances\.ops\duty_channel_reminder.out.log" -Encoding utf8
