# -*- coding: utf-8 -*-
"""报障群哨兵（仓库正本，实施81 P0-4；接替 C:\\Users\\Administrator\\tmp_group_sentinel.py）。

tail 智聊实例 app.log，出现报障群新入站消息即打印并退出——值守（173 的
Cursor 会话）把本脚本挂在**阻塞 SSH** 上，进程退出＝事件通知（零轮询）：

    ssh chengjie117 "python D:\\boundless\\deploy\\duty\\group_sentinel.py"

- 水位文件记读取偏移：重挂时从上次位置续读，杜绝「重挂间隙」空窗漏消息
  （B123 的哨兵半边；引擎侧补登记由 bug_intake_backfill 负责）。
- 水位默认落 ``D:\\chengjie-instances\\.ops\\``（仓库外）——正本进 git 后
  运行时状态绝不能把工作树写脏（脏树会撞 restart_instance 闸门）。
- 噪声黑名单：系统内部日志与值守自己的出站不触发。
- 值守缺位时的兜底不在本脚本：见 tools/duty_watchdog.py（计划任务，
  超 30 分钟无人应答告警 @Sousaun）。
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time

DEFAULT_LOG = r"D:\chengjie-instances\zhiliao\data\logs\app.log"
DEFAULT_WATERMARK = r"D:\chengjie-instances\.ops\group_sentinel.pos"
DEFAULT_GROUPS = ("1004345824259", "1004290740529")   # 内测群 / 官方群
NOISE = ("takeover_rearm", "health_watchdog", "intent_tags", "轮询兜底",
         "quota_wall", "send_gate",
         "已发送消息到", "unified_inbox_send_routes", "[send]",
         "bug_intake_backfill", "bug_backfill", "duty_watchdog",
         "gap-probe")   # 0829 值守：账号路由缺口补拉日志带群 id，误报


def out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


def is_hit(line: str, groups) -> bool:
    return any(g in line for g in groups) and not any(n in line for n in NOISE)


def main() -> int:
    ap = argparse.ArgumentParser(description="报障群哨兵（阻塞 tail，命中即退出）")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--watermark", default=DEFAULT_WATERMARK)
    ap.add_argument("--groups", default=",".join(DEFAULT_GROUPS),
                    help="逗号分隔的群 id 片段")
    args = ap.parse_args()
    groups = tuple(g.strip() for g in args.groups.split(",") if g.strip())

    def save_pos(pos: int) -> None:
        try:
            os.makedirs(os.path.dirname(args.watermark), exist_ok=True)
            with open(args.watermark, "w") as w:
                w.write(str(pos))
        except OSError:
            pass

    size = os.path.getsize(args.log)
    start_pos = size
    try:                                   # 水位续读（水位仍有效才用）
        with open(args.watermark) as w:
            p = int(w.read().strip() or 0)
        if 0 <= p <= size:
            start_pos = p
    except (OSError, ValueError):
        pass

    f = io.open(args.log, encoding="utf-8", errors="ignore")
    f.seek(start_pos)
    out("[sentinel-start] %s (from=%d size=%d)"
        % (time.strftime("%m-%d %H:%M:%S"), start_pos, size))

    hit = False
    while True:
        line = f.readline()
        if not line:
            save_pos(f.tell())
            if hit:                        # 命中且已读到尾 → 退出通知
                out("[sentinel-exit] %s" % time.strftime("%m-%d %H:%M:%S"))
                return 0
            time.sleep(1)
            try:                           # 日志轮转：文件变小则重开
                if os.path.getsize(args.log) < f.tell():
                    f.close()
                    f = io.open(args.log, encoding="utf-8", errors="ignore")
                    save_pos(0)
                    out("[sentinel] log rotated, reopened")
            except OSError:
                pass
            continue
        if is_hit(line, groups):
            out("[sentinel-hit] " + line.rstrip("\r\n")[:400])
            hit = True


if __name__ == "__main__":
    raise SystemExit(main())
