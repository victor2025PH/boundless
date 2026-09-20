"""``ai.primary`` 切换审计台账 + 老板锁（``ai.primary_lock``，2026-08-22 事故沉淀）。

背景（为什么需要这两件套）：
- 2026-08-21 05:49 老板指令把客服主链切回 cloud（治理接口执行、当时已验证生效）；
  但 05:49–11:23 之间被翻回 ``local_only``——切换接口彼时**不落任何审计**，翻回方
  至今无法定位（嫌疑=算力中枢执行器的「编程模式结束自动切回 local_only」既定契约）。
- 2026-08-22 01:44–01:46 老板亲测三条消息全部撞 45s 技能超时静默不回，根因正是
  这次未经授权的翻回（本地链冷载/排队 10~65s+）。

两件套语义：
- **审计**：所有让 ``ai.primary`` 生效/改变的路径统一落 JSONL 台账——
  治理接口（保存/被锁拒绝）、AIClient 装载解析（含锁强制）、watchdog 本地保险
  （local* 探测连败自动切 cloud）、``deploy/compute/compute_mode.py`` CLI。
  行式：``{ts, iso, event, via, ...}``，追责不再靠考古。
- **老板锁** ``ai.primary_lock``（``cloud|local|local_only``，空=不锁）：
  - 治理接口拒绝一切与锁不符的切换（``err.setup.ai_primary_locked``）；
  - AIClient 装载时发现配置与锁不符 → **按锁强制生效** + best-effort 回写
    overlay + EventBus 告警（``ai_primary_guard_alert`` kind=lock_enforced）。
    收口点选「生效点」而非各写入口：手改 overlay、外部自动化（SSH 进来改文件）、
    任何绕过接口的途径，最终都必须经过 AIClient 解析才能生效——在这里拦=全途径拦。
  - 锁本身只能通过编辑 overlay 变更（深思熟虑的人工动作）；改锁后下次装载同样留痕。

安全边界（刻意保留）：锁**不**凌驾「声明 local* 但本地端点缺失 → 退回 cloud」的
防变砖护栏——锁到 local_only 而本地端点不存在时，聊天可用性优先于治理语义。

所有函数 best-effort 绝不抛：审计/锁是治理设施，绝不能反过来弄崩聊天主链。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_VALID_MODES = ("cloud", "local", "local_only")
AUDIT_FILENAME = "ai_primary_audit.jsonl"


def resolve_lock(ai_cfg: Any) -> str:
    """``ai.primary_lock`` 归一化读取：合法值返回小写模式名，其余返回 ""（不锁）。

    非法值（拼错/True/数字）按不锁处理并 debug 留痕——锁是收紧动作，
    解析不确定时宁可不收紧，也不能把主链锁到一个不存在的档位。
    """
    try:
        raw = str((ai_cfg or {}).get("primary_lock") or "").strip().lower()
    except Exception:
        return ""
    if raw in _VALID_MODES:
        return raw
    if raw:
        logger.debug("[ai-primary-audit] primary_lock 非法值 %r（按不锁处理）", raw)
    return ""


def audit_path() -> Path:
    """台账落点＝实例数据根 ``logs/ai_primary_audit.jsonl``。

    经 ``licensing.data_paths.config_dir()`` 锚定（AITR_CONFIG_PATH/AITR_DATA_DIR
    感知：生产落实例数据根、pytest 落进程级 tmp——与 instance_restart_status
    同族约定），任何异常回落 CWD 相对 ``logs/``（服务进程 CWD=数据根，语义一致）。
    """
    try:
        from src.licensing.data_paths import config_dir

        return config_dir().parent / "logs" / AUDIT_FILENAME
    except Exception:
        return Path("logs") / AUDIT_FILENAME


def append_event(event: str, **fields: Any) -> bool:
    """追加一行审计（best-effort，绝不抛）。返回是否成功写盘。

    统一字段：``ts``/``iso``/``event``；调用方按需带 ``via``（endpoint /
    ai_client_init / health_watchdog / compute_mode_cli）、``actor``、``ip``、
    ``mode_from``/``mode_to``/``requested``/``lock`` 等。
    """
    try:
        row: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "event": str(event or ""),
        }
        for k, v in fields.items():
            if v is not None:
                row[str(k)] = v
        p = audit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("[ai-primary-audit] %s %s", row["event"],
                    {k: v for k, v in row.items() if k not in ("ts", "iso", "event")})
        return True
    except Exception:
        logger.debug("[ai-primary-audit] 写入失败（已忽略）", exc_info=True)
        return False


def read_tail(limit: int = 50) -> List[Dict[str, Any]]:
    """读取台账最近 N 行（坏行跳过；文件缺失回空表；绝不抛）。"""
    try:
        p = audit_path()
        if not p.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-max(1, int(limit)):]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                continue
        return rows
    except Exception:
        return []


def last_state(limit: int = 200) -> Dict[str, Any]:
    """台账里**上一次已知的生效态**：``{"effective": ..., "lock": ...}``（缺则键为 None）。

    取最近一行带 ``effective`` 的 resolve/lock_enforced 行（装载点写的才是真生效值；
    switch_saved 只是「保存了」）。用于装载点判断「这次解析与上次相比档位/锁变了没」——
    切档通知（2026-09-17）挂在这里而不是切换接口：09-17 的 cloud→local 是直接改 overlay
    完成的，根本没走接口；只有装载点是所有途径的必经之路。
    """
    out: Dict[str, Any] = {"effective": None, "lock": None}
    try:
        for row in reversed(read_tail(limit)):
            if not isinstance(row, dict) or "effective" not in row:
                continue
            if str(row.get("event") or "") not in ("resolve", "lock_enforced"):
                continue
            out["effective"] = row.get("effective")
            out["lock"] = row.get("lock")
            return out
    except Exception:
        pass
    return out


__all__ = ["resolve_lock", "audit_path", "append_event", "read_tail", "last_state",
           "AUDIT_FILENAME"]
