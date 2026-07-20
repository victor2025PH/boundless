"""回复逻辑闸门拦截计数结构化落盘（src/client/gate_stats.py）单测。

覆盖：bump 累加与 today_counts 读回、7 天保留清理、损坏文件不抛且可
自愈重建、无文件时 today_counts 返回 None（调用方回落日志扫描口径）。
纯标准库模块，不 import telegram_client，无 pyrogram 依赖。
"""
from __future__ import annotations

import datetime
import json

from src.client.gate_stats import bump, today_counts


def test_bump_and_today_counts(tmp_path):
    p = tmp_path / "gate_stats.json"
    bump("cooldown", path=p)
    bump("cooldown", path=p)
    bump("streak", path=p)
    assert today_counts(path=p) == {"cooldown": 2, "streak": 1}


def test_retention_prunes_old_dates(tmp_path):
    p = tmp_path / "gate_stats.json"
    old_day = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    recent_day = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    p.write_text(json.dumps({
        old_day: {"cooldown": 5, "streak": 2},
        recent_day: {"cooldown": 1, "streak": 0},
    }), encoding="utf-8")
    bump("cooldown", path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert old_day not in data          # 超 7 天 → 被清掉
    assert recent_day in data           # 7 天内 → 保留
    assert data[datetime.date.today().isoformat()]["cooldown"] == 1


def test_corrupt_file_bump_no_raise_and_rebuilds(tmp_path):
    p = tmp_path / "gate_stats.json"
    p.write_text("{not valid json!!", encoding="utf-8")
    assert today_counts(path=p) is None   # 损坏 → None（回落日志扫描）
    bump("streak", path=p)                # 不抛，重建文件
    assert today_counts(path=p) == {"cooldown": 0, "streak": 1}


def test_today_counts_missing_file_returns_none(tmp_path):
    assert today_counts(path=tmp_path / "nope.json") is None


def test_today_counts_no_today_entry_returns_none(tmp_path):
    p = tmp_path / "gate_stats.json"
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    p.write_text(json.dumps({yesterday: {"cooldown": 3, "streak": 1}}), encoding="utf-8")
    assert today_counts(path=p) is None


def test_bump_creates_missing_parent_dir(tmp_path):
    p = tmp_path / "logs" / "sub" / "gate_stats.json"
    bump("cooldown", path=p)
    assert today_counts(path=p) == {"cooldown": 1, "streak": 0}
