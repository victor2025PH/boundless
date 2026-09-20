# -*- coding: utf-8 -*-
"""「搁置并停止自动回复」——搁置期把会话档位按下为 ``manual``，取消时**还原原档**。

背景（P1-12，2026-08-20 内测反馈）：坐席点「搁置」只是把会话从待接管/超时队列
临时移出，**AI 照常自动回复客户**。测试者的真实诉求是「这个会话我先不管，也别
让 AI 去搭话」——旧行为下他们只能手动把档位拨到「手动」，事后又忘了拨回来，
于是会话被钉死在 manual（与 .198/.104「接管即静音」同型事故）。

实现刻意**复用 takeover_rearm 的 source 编码术**而非加库表字段：

- 写：``set_automation_mode(cid, "manual", source="snooze_hold_from:<原档>")``
  ——原档编码进 source 本身，一次读 meta 即拿到「现在什么档 + 该还原成什么」，
  零 schema 迁移、零额外查询（``automation_mode_log`` 的 prev_mode 是审计流水，
  按时间倒查「哪条是搁置那次写的」既慢又脆）。
- 还原：只认自己写的 source。坐席在下拉框显式改过档（source=human）、守卫降档
  （guard:*）、接管（takeover*）——一律**不还原**，因为那是更新的、别人的意图。

与 ``takeover_rearm`` 的关键差异（别顺手加自动接回）：接管静音是「坐席正在忙，
忙完 AI 该接回」故有超时 sweep；搁置静音是**坐席显式说「别管它」**，超时自动
放 AI 回去等于推翻用户的决定。故本模块**只有显式释放**（取消搁置 / 坐席自己改
档位），代价是「静音会一直静下去」——用 ``snooze_hold_state`` 把它在回复区做成
常驻 pill + 一键恢复，让「一直静音」是看得见的选择而不是看不见的事故。

语义边界（与搁置本身正交，勿混）：
- 搁置到点/客户来消息 → 会话**重浮**（ingest 侧 ``clear_snooze``），但**静音
  保持**——客户来消息恰恰是最不该让 AI 抢答的时刻，坐席自己看到再决定。
- 取消搁置（坐席显式点「取消搁置」）→ 释放静音，还原原档。

失败方向：任何一步异常都不得阻断搁置主流程（调用方 best-effort 包裹）；还原
拿不到原档 → 回落全局默认档（与 ``rearm_restore_mode`` 同口径）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.inbox.store import AUTOMATION_MODES

logger = logging.getLogger(__name__)

# source 词汇（与 human/bootstrap/guard:*/sweep/bulk/takeover* 并列）
SNOOZE_HOLD_SOURCE = "snooze_hold"
_SNOOZE_HOLD_FROM_PREFIX = "snooze_hold_from:"
SNOOZE_RELEASE_SOURCE = "snooze_release"


def is_snooze_hold_source(source: Any) -> bool:
    """该 source 是否为「搁置静音」写入（还原只认它）。"""
    s = str(source or "")
    return s == SNOOZE_HOLD_SOURCE or s.startswith(_SNOOZE_HOLD_FROM_PREFIX)


def snooze_hold_prev_mode(source: Any) -> str:
    """从搁置静音 source 解出静音前档位；无记录/不合法 → 空串。"""
    s = str(source or "")
    if s.startswith(_SNOOZE_HOLD_FROM_PREFIX):
        prev = s[len(_SNOOZE_HOLD_FROM_PREFIX):].strip().lower()
        if prev in AUTOMATION_MODES:
            return prev
    return ""


def _read_meta(store: Any, cid: str) -> Dict[str, Any]:
    if store is None or not hasattr(store, "get_automation_mode_meta"):
        return {}
    try:
        return store.get_automation_mode_meta(cid) or {}
    except Exception:
        return {}


def _resolve_prev_mode(store: Any, cid: str, meta: Dict[str, Any]) -> str:
    """静音前的「真实原档」：接管态要穿透到接管前档位，否则原档永远丢成 manual。"""
    from src.inbox.takeover_rearm import is_takeover_source, takeover_prev_mode

    src = meta.get("source")
    if is_takeover_source(src):
        # 坐席先接管（已 manual）再搁置：当前 mode 是 manual，真正的原档编码在
        # takeover_from: 里——不穿透就会把「本来是全自动」丢成「无记录」。
        return takeover_prev_mode(src)
    prev = str(meta.get("mode") or "").strip().lower()
    if not prev and hasattr(store, "get_automation_mode_if_set"):
        try:
            prev = str(store.get_automation_mode_if_set(cid) or "").strip().lower()
        except Exception:
            prev = ""
    if prev in AUTOMATION_MODES and prev != "manual":
        return prev
    return ""


def apply_snooze_hold(store: Any, conversation_id: str) -> str:
    """搁置时按下静音：会话切 ``manual`` 并打搁置标。返回写入的 source（失败空串）。

    - 首次静音：原档编码进 source（``snooze_hold_from:auto_ai``）；无显式原档
      写裸 ``snooze_hold``（还原时回全局默认）。
    - 已处于搁置静音：**原样重写**（source 不变 → 保住最初记录的原档；重复搁置
      /改期不会把原档洗成 manual）。
    - 旧 store / 测试假件无 source 形参 → 退回旧签名（诚实降级，返回空串）。
    """
    cid = str(conversation_id or "").strip()
    if not cid or store is None:
        return ""
    try:
        meta = _read_meta(store, cid)
        if is_snooze_hold_source(meta.get("source")):
            source = str(meta.get("source"))
        else:
            prev = _resolve_prev_mode(store, cid, meta)
            source = (f"{_SNOOZE_HOLD_FROM_PREFIX}{prev}" if prev
                      else SNOOZE_HOLD_SOURCE)
    except Exception:
        source = SNOOZE_HOLD_SOURCE
    try:
        store.set_automation_mode(cid, "manual", source=source)
    except TypeError:
        try:
            store.set_automation_mode(cid, "manual")
        except Exception:
            logger.debug("[snooze_hold] 静音写入失败 cid=%s", cid, exc_info=True)
        return ""
    except Exception:
        logger.debug("[snooze_hold] 静音写入失败 cid=%s", cid, exc_info=True)
        return ""
    logger.info("[snooze_hold] 搁置静音 cid=%s source=%s", cid, source)
    return source


def restore_mode_for(source: Any, config: Optional[Dict[str, Any]]) -> str:
    """还原目标档：静音前原档 > 全局默认档。``manual``/不合法＝无事可做返空串。"""
    prev = snooze_hold_prev_mode(source)
    if not prev:
        try:
            from src.inbox.automation_mode import global_automation_mode_from_config
            prev = global_automation_mode_from_config(config)
        except Exception:
            prev = ""
    if prev not in AUTOMATION_MODES or prev == "manual":
        return ""
    return prev


def release_snooze_hold(
    store: Any,
    conversation_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """取消搁置时释放静音，还原原档。返回还原到的档位（无事可做＝空串）。

    幂等且保守：只在「当前 mode=manual 且 source 是本模块写的」时动手——坐席
    期间自己改过档位 / 被守卫降档 / 转成接管态，一律不动（更新的意图优先）。
    """
    cid = str(conversation_id or "").strip()
    if not cid or store is None:
        return ""
    meta = _read_meta(store, cid)
    if not meta or not is_snooze_hold_source(meta.get("source")):
        return ""
    if str(meta.get("mode") or "").strip().lower() != "manual":
        return ""
    target = restore_mode_for(meta.get("source"), config)
    if not target:
        return ""
    try:
        store.set_automation_mode(cid, target, source=SNOOZE_RELEASE_SOURCE)
    except TypeError:
        try:
            store.set_automation_mode(cid, target)
        except Exception:
            return ""
    except Exception:
        logger.debug("[snooze_hold] 还原失败 cid=%s", cid, exc_info=True)
        return ""
    logger.info("[snooze_hold] 取消搁置，AI 档位还原 cid=%s → %s", cid, target)
    return target


def snooze_hold_state(
    meta: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """GET /automation 的搁置静音快照（回复区常驻 pill + 一键恢复的数据源）。

    仅当「当前 manual 且 source 是搁置静音」才返回 dict；其余 None（前端不渲染）。
    这是「静音没有自动接回」的配套可见化——没有它，搁置到点后静音就是隐形的。
    """
    if not meta or str(meta.get("mode") or "") != "manual":
        return None
    if not is_snooze_hold_source(meta.get("source")):
        return None
    return {
        "held_at": float(meta.get("updated_at") or 0.0),
        "restore_mode": restore_mode_for(meta.get("source"), config),
    }


__all__ = [
    "SNOOZE_HOLD_SOURCE",
    "SNOOZE_RELEASE_SOURCE",
    "is_snooze_hold_source",
    "snooze_hold_prev_mode",
    "apply_snooze_hold",
    "restore_mode_for",
    "release_snooze_hold",
    "snooze_hold_state",
]
