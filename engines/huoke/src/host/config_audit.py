# -*- coding: utf-8 -*-
"""
2026-05-13: 配置变更审计日志。

各热重载函数在检测到 mtime 变化并完成重载后调用 record()，
将变更追加写入 data/config_audit.jsonl（每行一条 JSON）。

设计原则:
- 追加写入，永不覆盖，完整保留变更历史
- 线程安全（文件锁）
- 写入失败静默处理，不影响主流程
- 每文件只保留最新 1000 条，超出时自动截断
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_MAX_ENTRIES = 1000   # 每文件最多保留条数，超出时截断最旧 200 条

# 2026-05-13: 内存缓存—每个 file:section 的上一次写入值
# 用于计算 changed_keys，无需每次扫描文件
_last_value: Dict[str, dict] = {}

def record(filename: str, section: str, new_value: Dict[str, Any]) -> None:
    """追加一条配置变更记录到 data/config_audit.jsonl。

    自动计算 changed_keys：与同一 file+section 的上次写入内容对比，
    第一次写入时 changed_keys = 所有 key（基准建立）。

    Args:
        filename:  配置文件名，如 "task_execution_policy.yaml"
        section:   变更的配置节名，如 "scheduler"
        new_value: 变更后的该节内容（dict）
    """
    cache_key = f"{filename}:{section}"
    prev = _last_value.get(cache_key)
    if prev is not None:
        # 计算变更的 key 列表（包含新增和删除的 key）
        all_keys = set(new_value.keys()) | set(prev.keys())
        changed_keys = sorted(k for k in all_keys if new_value.get(k) != prev.get(k))
    else:
        changed_keys = sorted(new_value.keys())  # 首次建立基准

    # 更新内存缓存（锁外，次序不影响正确性）
    _last_value[cache_key] = new_value

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "file": filename,
        "section": section,
        "changed_keys": changed_keys,
        "new_value": new_value,
    }
    try:
        from src.host.device_registry import data_file
        path = data_file("config_audit.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            # 追加写入
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 超出容量时截断最旧条目（保留最新 800 条）
            _maybe_truncate(path)
    except Exception as e:
        logger.debug("[config_audit] 写入失败: %s", e)


def _maybe_truncate(path) -> None:
    """内部：超出 _MAX_ENTRIES 时截断文件，保留最新 (_MAX_ENTRIES - 200) 条。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_ENTRIES:
            return
        keep = lines[-(  _MAX_ENTRIES - 200):]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
        logger.debug("[config_audit] 已截断至 %d 条", len(keep))
    except Exception:
        pass


def read_recent(limit: int = 100) -> list:
    """读取最近 N 条配置变更记录（供 API 端点使用）。"""
    try:
        from src.host.device_registry import data_file
        path = data_file("config_audit.jsonl")
        if not path.exists():
            return []
        with _lock:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        limit = min(limit, 500)
        result = []
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except Exception:
                    pass
        return result
    except Exception:
        return []
