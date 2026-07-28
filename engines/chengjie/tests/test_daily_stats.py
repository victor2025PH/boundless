"""Telegram 今日活动计数结构化落盘（src/client/daily_stats.py）单测。

覆盖：bump 累加/任意新键/当日隔离、today_counts 三态（有数据/无当日/
文件缺失→None，None=调用方回落日志扫描口径的契约）、recent_counts
补 0 与升序、7 天保留清理、损坏文件不抛且可自愈重建、原子写无 .tmp
残留。纯标准库模块，不 import telegram_client，无 pyrogram 依赖。
另含接线静态断言：telegram_client/sender 的 bump 埋点处数须 ≥ 对应
日志行处数（结构化计数与日志尾扫口径同点同频，防漏埋）。
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from src.client.daily_stats import bump, recent_counts, today_counts

_REPO = Path(__file__).resolve().parents[1]


# ── 纯函数面 ─────────────────────────────────────────────────────────

def test_bump_and_today_counts(tmp_path):
    p = tmp_path / "daily_stats.json"
    bump("messages", path=p)
    bump("messages", path=p)
    bump("voice_in", path=p)
    # tts_sent 当日无 bump → 键缺省补 0
    assert today_counts(path=p) == {"messages": 2, "voice_in": 1, "tts_sent": 0}


def test_bump_accepts_arbitrary_key(tmp_path):
    # 与 gate_stats 同：不做枚举硬校验，任意 str 键都累加（today_counts
    # 只回读三个统计名，未知键留在文件里不外泄）。
    p = tmp_path / "daily_stats.json"
    bump("weird_key", path=p)
    bump("weird_key", path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data[datetime.date.today().isoformat()]["weird_key"] == 2
    assert today_counts(path=p) == {"messages": 0, "voice_in": 0, "tts_sent": 0}


def test_bump_isolates_days(tmp_path):
    # 昨日已有计数 → 今日 bump 从 0 起算，互不串味。
    p = tmp_path / "daily_stats.json"
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    p.write_text(json.dumps({yesterday: {"messages": 5}}), encoding="utf-8")
    bump("messages", path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data[yesterday]["messages"] == 5
    assert data[datetime.date.today().isoformat()]["messages"] == 1
    assert today_counts(path=p) == {"messages": 1, "voice_in": 0, "tts_sent": 0}


def test_today_counts_missing_file_returns_none(tmp_path):
    assert today_counts(path=tmp_path / "nope.json") is None


def test_today_counts_no_today_entry_returns_none(tmp_path):
    p = tmp_path / "daily_stats.json"
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    p.write_text(json.dumps({yesterday: {"messages": 3, "voice_in": 1}}),
                 encoding="utf-8")
    assert today_counts(path=p) is None


def test_retention_prunes_old_dates(tmp_path):
    p = tmp_path / "daily_stats.json"
    old_day = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
    recent_day = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    p.write_text(json.dumps({
        old_day: {"messages": 9, "voice_in": 2},
        recent_day: {"messages": 1},
    }), encoding="utf-8")
    bump("messages", path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert old_day not in data          # 超 7 天 → 被清掉
    assert recent_day in data           # 7 天内 → 保留
    assert data[datetime.date.today().isoformat()]["messages"] == 1


def test_corrupt_file_bump_no_raise_and_rebuilds(tmp_path):
    p = tmp_path / "daily_stats.json"
    p.write_text("{not valid json!!", encoding="utf-8")
    assert today_counts(path=p) is None   # 损坏 → None（回落日志扫描）
    bump("voice_in", path=p)              # 不抛，重建文件
    assert today_counts(path=p) == {"messages": 0, "voice_in": 1, "tts_sent": 0}


def test_bump_atomic_write_no_tmp_residue(tmp_path):
    p = tmp_path / "daily_stats.json"
    bump("tts_sent", path=p)
    assert p.exists()
    assert not p.with_suffix(p.suffix + ".tmp").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_recent_counts_fills_missing_days_ascending(tmp_path):
    p = tmp_path / "daily_stats.json"
    today = datetime.date.today()
    d3 = (today - datetime.timedelta(days=3)).isoformat()
    p.write_text(json.dumps({
        today.isoformat(): {"messages": 2, "voice_in": 1, "tts_sent": 1},
        d3: {"messages": 5},                       # 其余键缺失 → 补 0
    }), encoding="utf-8")
    rows = recent_counts(days=7, path=p)
    assert len(rows) == 7
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates)                  # 升序，最后一天=今天
    assert rows[-1] == {"date": today.isoformat(),
                        "messages": 2, "voice_in": 1, "tts_sent": 1}
    assert rows[3] == {"date": d3, "messages": 5, "voice_in": 0, "tts_sent": 0}
    assert rows[0] == {                            # 无数据日全 0
        "date": (today - datetime.timedelta(days=6)).isoformat(),
        "messages": 0, "voice_in": 0, "tts_sent": 0,
    }


def test_recent_counts_missing_file_all_zero(tmp_path):
    rows = recent_counts(days=7, path=tmp_path / "nope.json")
    assert len(rows) == 7
    assert rows[-1]["date"] == datetime.date.today().isoformat()
    assert all(r["messages"] == 0 and r["voice_in"] == 0 and r["tts_sent"] == 0
               for r in rows)


# ── 接线静态断言（埋点与日志行同点同频，防漏埋） ─────────────────────

def _src(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_wiring_telegram_client_bump_sites():
    src = _src("src/client/telegram_client.py")
    assert "from src.client import daily_stats" in src
    assert 'daily_stats.bump("messages")' in src
    assert 'daily_stats.bump("voice_in")' in src
    # 「收到语音消息 [」不含子串「收到消息 [」（读侧尾扫 if/elif 同一判定），
    # 两类日志行处数可独立统计；每处日志行旁都须有对应 bump。
    n_msg_logs = len(re.findall(r"收到消息 \[", src))
    n_voice_logs = len(re.findall(r"收到语音消息 \[", src))
    assert n_msg_logs >= 1 and n_voice_logs >= 1   # 口径基准日志行未漂移
    assert len(re.findall(r'daily_stats\.bump\("messages"\)', src)) >= n_msg_logs
    assert len(re.findall(r'daily_stats\.bump\("voice_in"\)', src)) >= n_voice_logs


def test_wiring_sender_bump_sites():
    src = _src("src/client/sender.py")
    assert "from src.client import daily_stats" in src
    assert 'daily_stats.bump("tts_sent")' in src
    # 发送成功日志点＝单条 "[voice_reply] voice sent" + 分条汇总
    # "[voice_reply] 分条语音已发"（Phase7 分条不打 "voice sent" 行），
    # 每处成功日志点都须有 bump。
    n_single_logs = len(re.findall(r"\[voice_reply\] voice sent", src))
    n_split_logs = len(re.findall(r"分条语音已发", src))
    assert n_single_logs >= 1
    n_bumps = len(re.findall(r'daily_stats\.bump\("tts_sent"\)', src))
    assert n_bumps >= n_single_logs + n_split_logs
