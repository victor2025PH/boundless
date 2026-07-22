"""日志巡检聚合器：把 app.log 按 (级别, logger, 归一化模板) 折叠成 Top-N，
每组给出 计数 / 首次 / 末次 / 今日计数，一条命令替代手工 Select-String。

服务「一眼看清此刻哪类问题在烧」的巡检流程——把动辄上千行的 WARNING/ERROR
先归一化（把 hex/UUID/IP/数字/请求号等易变部分抹成占位符）再分组计数，
噪声（网络超时刷屏）和真 bug（同一模板反复出现）自然分层，配合首末时间戳
即可判断「历史残留 vs 当前活跃」。

用法：
    python -m scripts.log_triage [--file <路径>] [--level WARNING,ERROR]
        [--top 25] [--today] [--since "2026-07-22 22:00"] [--grep <正则>]
        [--width 120] [--json]

默认 --file 取环境变量 AITR_TRIAGE_LOG，否则回退到常见实例日志路径。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ── 行解析：[YYYY-MM-DD HH:MM:SS] [LEVEL] logger: message ───────────────
_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*"
    r"\[(?P<level>[A-Z]+)\]\s*"
    r"(?P<logger>[^:]+):\s*"
    r"(?P<msg>.*)$"
)

# 归一化替换（顺序敏感：IP → hex → 纯数字）
_IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{6,}\b")
_NUM_RE = re.compile(r"\d+")


@dataclass
class Record:
    ts: str
    level: str
    logger: str
    msg: str


@dataclass
class Group:
    level: str
    logger: str
    template: str
    count: int = 0
    first_ts: str = ""
    last_ts: str = ""
    today_count: int = 0
    sample: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "today": self.today_count,
            "level": self.level,
            "logger": self.logger,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "template": self.template,
            "sample": self.sample,
        }


def normalize(msg: str, width: int = 120) -> str:
    """把一条消息抹成稳定模板：IP/hex/数字 → 占位符，压缩空白并截断。"""
    s = _IP_RE.sub("<ip>", msg)
    s = _HEX_RE.sub("<hex>", s)
    s = _NUM_RE.sub("<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    if width and len(s) > width:
        s = s[:width] + "…"
    return s


def parse_lines(lines: Iterable[str]) -> List[Record]:
    """只收 `[ts] [LEVEL] logger: msg` 结构行；续行（traceback 等）忽略。"""
    out: List[Record] = []
    for line in lines:
        m = _LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        out.append(Record(m["ts"], m["level"], m["logger"].strip(), m["msg"]))
    return out


def _latest_date(records: List[Record]) -> str:
    d = ""
    for r in records:
        day = r.ts[:10]
        if day > d:
            d = day
    return d


def triage(
    records: List[Record],
    levels: Optional[Iterable[str]] = None,
    since: Optional[str] = None,
    today_only: bool = False,
    grep: Optional[str] = None,
    width: int = 120,
    today: Optional[str] = None,
) -> List[Group]:
    """分组聚合。levels/since/grep 为过滤；today 缺省取日志内最新日期。"""
    level_set = {x.strip().upper() for x in levels} if levels else None
    grep_re = re.compile(grep) if grep else None
    today_str = today or _latest_date(records)

    groups: Dict[tuple, Group] = {}
    for r in records:
        if level_set and r.level not in level_set:
            continue
        if since and r.ts < since:
            continue
        if today_only and r.ts[:10] != today_str:
            continue
        if grep_re and not grep_re.search(r.msg):
            continue
        tmpl = normalize(r.msg, width=width)
        key = (r.level, r.logger, tmpl)
        g = groups.get(key)
        if g is None:
            g = Group(level=r.level, logger=r.logger, template=tmpl,
                      first_ts=r.ts, sample=r.msg[:200])
            groups[key] = g
        g.count += 1
        if r.ts > g.last_ts:
            g.last_ts = r.ts
        if r.ts < g.first_ts:
            g.first_ts = r.ts
        if r.ts[:10] == today_str:
            g.today_count += 1
    return sorted(groups.values(), key=lambda x: x.count, reverse=True)


def _default_log_path() -> Optional[Path]:
    env = os.environ.get("AITR_TRIAGE_LOG")
    if env:
        return Path(env)
    for cand in (
        Path(r"D:\chengjie-instances\zhiliao\data\logs\app.log"),
        Path("logs/app.log"),
        Path("app.log"),
    ):
        if cand.exists():
            return cand
    return None


def _render_table(groups: List[Group], top: int) -> str:
    rows = groups[:top]
    if not rows:
        return "(无匹配记录)"
    lines = [f"{'CNT':>6} {'今日':>5}  {'LEVEL':<8} {'LOGGER':<32} 首次…末次 / 模板"]
    lines.append("-" * 110)
    for g in rows:
        span = f"{g.first_ts[5:]} … {g.last_ts[5:]}"
        lines.append(
            f"{g.count:>6} {g.today_count:>5}  {g.level:<8} {g.logger[:32]:<32} {span}"
        )
        lines.append(f"        └ {g.template}")
    total = sum(g.count for g in groups)
    lines.append("-" * 110)
    lines.append(f"分组 {len(groups)} 组，命中 {total} 行（显示前 {len(rows)}）")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="日志巡检聚合器")
    ap.add_argument("--file", default=None, help="日志路径（默认自动探测）")
    ap.add_argument("--level", default="WARNING,ERROR",
                    help="逗号分隔的级别过滤；空串=全部")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--today", action="store_true", help="只看日志内最新日期")
    ap.add_argument("--since", default=None, help='起始时间，如 "2026-07-22 22:00"')
    ap.add_argument("--grep", default=None, help="消息正则过滤")
    ap.add_argument("--width", type=int, default=120, help="模板截断宽度")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.file) if args.file else _default_log_path()
    if not path or not path.exists():
        print(f"日志文件不存在: {path}", file=sys.stderr)
        return 2

    with open(path, encoding="utf-8", errors="replace") as f:
        records = parse_lines(f)

    levels = [x for x in args.level.split(",") if x.strip()] if args.level else None
    groups = triage(
        records, levels=levels, since=args.since,
        today_only=args.today, grep=args.grep, width=args.width,
    )

    if args.json:
        print(json.dumps([g.as_dict() for g in groups[: args.top]],
                         ensure_ascii=False, indent=2))
    else:
        print(f"# {path}  （共 {len(records)} 条结构化记录）")
        print(_render_table(groups, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
