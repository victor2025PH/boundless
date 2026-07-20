"""回复逻辑闸门拦截计数——结构化落盘（替代纯日志尾扫口径）。

背景：UI「Telegram → 账号信息」的今日统计原先靠尾扫 logs/app.log 最后
8000 行数 "[回复逻辑] 冷却中/已达上限" 日志行。高流量日 8000 行只覆盖
最近几小时，早间的拦截会漏计。本模块把每次拦截即时累加到
logs/tg_gate_stats.json（{date: {"cooldown": n, "streak": n}}，仅保留
最近 7 天），读数端优先取该文件、缺失才回落日志扫描。

语义与权衡：
- 计数按自然日（本地时区 date.today()）累加，进程重启不清零——比日志
  口径更准（日志轮转/重启即丢）。
- 跨升级首日：升级前的拦截只存在于日志、升级后的进拦截文件，两个口径
  当日会并存。读数端以文件为权威（存在即覆盖），因此首日展示值可能略
  低于"日志+文件"之和，次日起完全一致。不做合并——避免重启后重复计数。
- 写入全程 try/except 静默 + 原子替换（.tmp → os.replace）：统计属旁路
  观测，绝不能因磁盘/权限/并发问题影响回复链路本身。
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

DEFAULT_PATH = Path("logs") / "tg_gate_stats.json"

_KEEP_DAYS = 7


def bump(kind: str, path=None) -> None:
    """当日 kind ∈ {"cooldown","streak"} 计数 +1，原子写盘；任何异常静默。"""
    try:
        p = Path(path) if path is not None else DEFAULT_PATH
        today = datetime.date.today().isoformat()
        data = {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except Exception:
            data = {}  # 不存在/损坏 → 重建
        day = data.get(today)
        if not isinstance(day, dict):
            day = {}
        day[kind] = int(day.get(kind, 0) or 0) + 1
        data[today] = day
        # 只保留最近 7 天的日期键（含今天），防文件无限增长
        cutoff = (datetime.date.today() - datetime.timedelta(days=_KEEP_DAYS - 1)).isoformat()
        data = {k: v for k, v in data.items() if str(k) >= cutoff}
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def today_counts(path=None):
    """返回当日 {"cooldown": n, "streak": n}；文件缺失/无当日条目/解析失败 → None。

    返回 None 表示"结构化数据不可用"，调用方应回落到日志扫描口径。
    """
    try:
        p = Path(path) if path is not None else DEFAULT_PATH
        data = json.loads(p.read_text(encoding="utf-8"))
        day = data.get(datetime.date.today().isoformat())
        if not isinstance(day, dict):
            return None
        return {
            "cooldown": int(day.get("cooldown", 0) or 0),
            "streak": int(day.get("streak", 0) or 0),
        }
    except Exception:
        return None
