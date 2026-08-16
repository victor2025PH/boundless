# -*- coding: utf-8 -*-
"""引流闭环「每次真实产出即探针」持久流水（2026-08-13，对标 AvatarHub song_quality/O11）。

背景：引流转化率一直是 0（回复检测 cron 没通电），通电后若哪天转化率悄悄退化，
现有 logs/daily_summary_*.json 只是「当天看一眼」的快照，没有可回归对比的长流水。
本模块在每次**真实业务产出**时追加一行 JSONL 到 data/referral_quality.jsonl：
    reply        识别到一条 referral 回复（转化正信号，逐条）
    sent         成功发出一条 LINE 引流（漏斗分母，逐条）
    reply_batch  一轮回复检测的批次统计（pending/scanned/replied/no_match）
    stale_batch  一轮 SLA 死信标记（stale/dead）

每条都带 canonical_id + platform，让 tools/referral_quality_report.py 能做
**跨平台双键 (canonical_id, platform) 去重**（INTEGRATION_CONTRACT §7.7-3 契约），
而不是像 legacy referral_funnel 那样只按 peer_name 单键去重。

设计原则（照抄 config_audit 的成熟做法）：
    * 追加写、永不覆盖；线程安全（文件锁）
    * 写失败静默，绝不影响主流程（探针不能反噬业务）
    * 超 _MAX_ENTRIES 自动截断最旧
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_MAX_ENTRIES = 20000     # 超出保留最新 (_MAX_ENTRIES - 2000)
_FILE = "referral_quality.jsonl"

VALID_KINDS = {"reply", "sent", "reply_batch", "stale_batch"}


def _path():
    from src.host.device_registry import data_file
    return data_file(_FILE)


def record(kind: str, **fields: Any) -> None:
    """追加一条引流产出记录。kind ∈ VALID_KINDS；fields 任意可 JSON 序列化。

    绝不抛异常（探针失败不许影响引流主流程）。
    """
    if kind not in VALID_KINDS:
        # 记一条 warning 但不落盘坏数据；判定单一真相在调用侧
        logger.debug("[referral_probe] 未知 kind=%s，忽略", kind)
        return
    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
    }
    entry.update(fields)
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _maybe_truncate(path)
    except Exception as e:  # pragma: no cover - fail-silent
        logger.debug("[referral_probe] 写入失败: %s", e)


def _maybe_truncate(path) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_ENTRIES:
            return
        keep = lines[-(_MAX_ENTRIES - 2000):]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except Exception:  # pragma: no cover
        pass


def read_all() -> List[Dict[str, Any]]:
    """读全部记录（供 report / API）。文件不存在返回 []。"""
    try:
        path = _path()
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with _lock:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out
    except Exception:
        return []
