"""AvatarHub 7852 救援链（Windows 计划任务）活性探测。

2026-08-14 实录：7852 从 13:24 掉线到深夜无自愈——救援链的两个计划任务
``EmotionTTS_Boot`` / ``EmotionTTSWatchdog`` 都处于 **Disabled**（集群进
「代码模式」联动停用后没人恢复），而 ``_check_avatar_voice`` 的告警只说
「服务掉线」，运维读到的隐含语义是「外部看门狗会拉起来」——自愈链本身
已经死了这件事没有任何暴露面。本模块在告警成立时顺带探测救援任务状态，
把「救援链已停用/缺失」显式写进同一条告警。

设计约束：
- 只读探测（``schtasks /Query /TN <name> /XML``），绝不 /Change /Run——
  恢复任务是运维决策（集群代码模式期间停用可能是刻意的）；
- 判读用 XML 里的 ``<Settings><Enabled>false</Enabled>``，**locale 无关**
  （/FO LIST 的 "Status:" 行在中英文系统上是两种词，解析必碎）；
- 非 Windows / schtasks 缺失 / 超时 / XML 坏形一律 ``unknown``——
  宁可少说一行，不把「探测失败」谎报成「任务被停用」。
"""
from __future__ import annotations

import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger("ai_chat_assistant.avatar_voice_rescue")

# 7852 emotion_tts 的救援链：Boot=开机/按需拉起，Watchdog=每 5min 合成级探针
DEFAULT_RESCUE_TASKS = ("EmotionTTS_Boot", "EmotionTTSWatchdog")

# 探测结果桶（probe_rescue_tasks 的值域）
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_MISSING = "missing"    # 任务不存在（被删）＝救援链同样断了
STATE_UNKNOWN = "unknown"    # 探测失败＝信息不足，不进告警


def parse_task_enabled(xml_text: str) -> Optional[bool]:
    """从 schtasks /XML 输出解析任务是否启用（纯函数）。

    只认 ``<Settings>`` 下的 ``<Enabled>``（Trigger 里也有同名标签，语义是
    「该触发器启用与否」，不能混）；``<Settings>`` 无该子节点＝默认启用。
    XML 坏形 / 找不到 Settings → None（信息不足）。
    """
    try:
        root = ET.fromstring(xml_text or "")
    except Exception:
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Settings":
            for child in el:
                if child.tag.rsplit("}", 1)[-1] == "Enabled":
                    return (child.text or "").strip().lower() != "false"
            return True
    return None


def _decode_schtasks_output(raw: bytes) -> str:
    """schtasks /XML 输出按 BOM 判 UTF-16，否则按 UTF-8 宽松解码。

    关键标签全 ASCII，两种解码都不会伤到 ``<Enabled>``；宽松是为了
    任务描述里的本地化文字不至于抛 UnicodeDecodeError。
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def probe_rescue_tasks(
    task_names: Sequence[str] = DEFAULT_RESCUE_TASKS,
    *, timeout_sec: float = 6.0,
) -> Dict[str, str]:
    """逐个查询计划任务的启用态。非 Windows 返回空 dict（天然静默）。"""
    if os.name != "nt":
        return {}
    out: Dict[str, str] = {}
    for name in task_names:
        name = str(name or "").strip()
        if not name:
            continue
        try:
            cp = subprocess.run(
                ["schtasks", "/Query", "/TN", name, "/XML"],
                capture_output=True, timeout=timeout_sec)
        except Exception:
            out[name] = STATE_UNKNOWN
            continue
        if cp.returncode != 0:
            # 查询失败几乎只有「任务不存在」一种（权限问题本进程不会遇到：
            # 服务与任务同机同账户）；任务被删同样意味着救援链断裂。
            out[name] = STATE_MISSING
            continue
        enabled = parse_task_enabled(_decode_schtasks_output(cp.stdout or b""))
        if enabled is None:
            out[name] = STATE_UNKNOWN
        else:
            out[name] = STATE_ENABLED if enabled else STATE_DISABLED
    return out


def broken_rescue_tasks(states: Optional[Dict[str, Any]]) -> List[str]:
    """从探测结果里挑出「救援链确定断了」的任务名（纯函数，排序稳定）。

    只收 disabled/missing；unknown 刻意不进——探测失败不构成断言。
    """
    if not isinstance(states, dict):
        return []
    bad = (STATE_DISABLED, STATE_MISSING)
    return sorted(str(k) for k, v in states.items() if v in bad)


def resolve_rescue_task_names(cfg: Optional[Dict[str, Any]]) -> List[str]:
    """从配置取救援任务清单（``avatar_voice.rescue_tasks``），缺省用默认对。

    显式配空列表＝关闭该探测（部署方没有这两个任务时的逃生门）。
    """
    av = (cfg or {}).get("avatar_voice") if isinstance(cfg, dict) else None
    tasks = (av or {}).get("rescue_tasks") if isinstance(av, dict) else None
    if tasks is None:
        return list(DEFAULT_RESCUE_TASKS)
    if not isinstance(tasks, (list, tuple)):
        return list(DEFAULT_RESCUE_TASKS)
    return [str(t).strip() for t in tasks if str(t or "").strip()]
