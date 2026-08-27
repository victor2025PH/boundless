"""会话 automation_mode 解析与首条入站 bootstrap（Phase13）。

问题：``InboxStore.get_automation_mode`` 在无显式记录时回落 ``review``，而
``auto_draft`` 用 ``get_automation_mode_if_set`` + 全局 ``automation_mode: auto_ai``——
两条口径不一致 → 新好友 UI 显示「人审」、``inbox_will_autosend`` 不让位 System Z、
与运营「好友消息全自动」方针冲突。

本模块提供单一事实源：
- ``resolve_automation_mode``：显式档位 > 全局 auto_draft.automation_mode
- ``maybe_bootstrap_automation_mode``：首条入站时把全局档位持久化（仅无显式记录时）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.inbox.store import AUTOMATION_MODES, _DEFAULT_AUTOMATION_MODE

# A 线自带「刻意群策略」的平台豁免（pyrogram 客户端：被 @ 触发 / interject 概率 /
# 群冷却 / S5 静默）——豁免平台的群保持旧语义（未显式档位回落全局），群里说不说
# 话由那套群闸决定。
#
# B44（2026-08-22，1.0.46 实录）：**默认豁免清空**。此前 telegram 常驻豁免名单，
# 客户包全自动+「新会话自动沿用」下，用户 AI 把报障群当客户会话**以用户身份
# 代答**（05:59-06:53 五条+语音，秒回复述值守话术，警告后仍接话）——A 线群策略
# （interject 概率等）是内部陪聊部署的业务特性，对客户包是「AI 在任意群以假
# 乱真代言」的安全事故。现豁免走配置 ``inbox.auto_draft.group_autopilot_platforms``
# （列表，默认空＝群一律默认排除自动回复，显式开启才放行）；内部部署（zhiliao）
# 经 overlay 配 ``[telegram]`` 保留既有群策略语义。
_GROUP_SELF_POLICY_PLATFORMS_DEFAULT = frozenset()


def _group_autopilot_platforms(config: Optional[Dict[str, Any]]) -> frozenset:
    """读配置豁免名单；缺省/非法一律回落**空集**（安全默认）。"""
    raw = _auto_draft_cfg(config).get("group_autopilot_platforms")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return _GROUP_SELF_POLICY_PLATFORMS_DEFAULT
    return frozenset(
        str(p).strip().lower() for p in raw if str(p).strip())

# 群判据「弱证据」平台（P1 2026-08-20）：群/私聊之分不来自平台自描述的地址，
# 而来自**网页 DOM 启发式**——messenger 边车看「同线程是否出现 ≥2 个不同发言人」，
# instagram 边车看「线程行是否叠 ≥2 张头像」（services/instagram-web/ig_threads.js）。
# 对比 WA(`@g.us`) / LINE(群 id) / TG(负数 chat_id) / Zalo(边车群注册表) 那些**地址
# 自带群语义**的硬事实，启发式会在平台改版/极端 UI 下误判，而误判方向不对称：
# 把私聊看成群 → 该客户被当群处理，最坏路径是 skip_group_chats 让他**一条草稿都
# 没有**（静默丢客户，无痕迹）；把群看成私聊只是回到旧行为。故弱证据平台不许走
# 「静默」那条路，只许走「可见」那条路（档位仍降 review=AI 不在群里自动说话）。
_GROUP_EVIDENCE_WEAK_PLATFORMS = frozenset({"messenger", "instagram"})


def _auto_draft_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (((config or {}).get("inbox") or {}).get("auto_draft") or {})


def group_autopilot_exempt(
    conversation_id: str, config: Optional[Dict[str, Any]] = None,
) -> bool:
    """该会话所属平台是否豁免「群不回落全自动」守卫（B44 起走配置，默认无豁免）。"""
    plat = str(conversation_id or "").split(":", 1)[0].strip().lower()
    return plat in _group_autopilot_platforms(config)


def group_evidence_is_weak(conversation_id: str) -> bool:
    """该会话的「是不是群」是启发式推断（而非地址自描述）的吗？

    见 ``_GROUP_EVIDENCE_WEAK_PLATFORMS``。纯函数、只看平台前缀——判据强度是
    **边车实现属性**，与具体会话无关；将来某平台换成硬判据（如 IG 拿到
    participants 数）就把它从集合里删掉，全部消费方同步收紧。
    """
    plat = str(conversation_id or "").split(":", 1)[0].strip().lower()
    return plat in _GROUP_EVIDENCE_WEAK_PLATFORMS


def conversation_is_group(store: Any, conversation_id: str) -> bool:
    """会话是否群/频道（store PK 查行 + ``is_group_conversation`` 双判据）。

    读挂/无行 → False（fail-open 保私聊主链：把私聊误判成群会静音 99% 流量，
    把群漏判成私聊只让个别群回到旧行为——方向性选择）。
    """
    if store is None or not conversation_id:
        return False
    if not hasattr(store, "get_conversation"):
        return False
    try:
        row = store.get_conversation(conversation_id)
        if not row:
            return False
        from src.inbox.ingest import is_group_conversation
        return bool(is_group_conversation(row))
    except Exception:
        return False


def global_automation_mode_from_config(config: Optional[Dict[str, Any]]) -> str:
    """读 ``inbox.auto_draft.automation_mode``（缺省 auto_ai）。"""
    mode = str(_auto_draft_cfg(config).get("automation_mode") or "auto_ai").lower()
    return mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE


def bootstrap_enabled_from_config(config: Optional[Dict[str, Any]]) -> bool:
    """是否在新会话首条入站时持久化全局档位。"""
    ad = _auto_draft_cfg(config)
    if "bootstrap_automation_mode" in ad:
        return bool(ad.get("bootstrap_automation_mode"))
    # 默认：全局为 auto_ai 时自动 bootstrap（好友全自动方针）
    return global_automation_mode_from_config(config) == "auto_ai"


def resolve_automation_mode(
    store: Any,
    conversation_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """有效档位：坐席/UI 显式设置 > 全局 auto_draft.automation_mode。

    群守卫（P1 2026-08-20 → B44 2026-08-22 收紧）：**未显式设置**的群/频道不得
    回落全局 ``auto_ai``（生效 ``review``）——「AI 在群里自动说话」必须由坐席经
    confirm_group 409 闸显式确认（显式档位不受影响）。平台豁免走配置
    ``inbox.auto_draft.group_autopilot_platforms``（默认空＝群一律默认排除；
    内部陪聊部署经 overlay 配 [telegram] 保留 A 线群策略语义）。UI 显示、
    A 线协议直发、B 线拟稿、peer_bot_guard 全部经本函数同源继承，绝不各算一套。
    """
    if store is not None and conversation_id:
        explicit = store.get_automation_mode_if_set(conversation_id)
        if explicit is not None:
            return explicit
    mode = global_automation_mode_from_config(config)
    if (mode == "auto_ai"
            and not group_autopilot_exempt(conversation_id, config)
            and conversation_is_group(store, conversation_id)):
        return "review"
    return mode


def maybe_bootstrap_automation_mode(
    store: Any,
    conversation_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """首条入站 bootstrap：无显式档位且开关开 → 持久化全局档位并返回。

    群/频道**绝不代写档位**（任何平台）：群的显式 ``auto_ai`` 只能来自坐席经
    confirm_group 闸的确认——bootstrap 替群写下 auto_ai 会让确认闸形同虚设
    （闸只拦 UI 升档，拦不住已被 bootstrap 写成全自动的群）。返回值与
    ``resolve_automation_mode`` 同口径（非豁免平台的群 → review）。
    """
    if not store or not conversation_id:
        return global_automation_mode_from_config(config)
    explicit = store.get_automation_mode_if_set(conversation_id)
    if explicit is not None:
        return explicit
    mode = global_automation_mode_from_config(config)
    if conversation_is_group(store, conversation_id):
        if mode == "auto_ai" and not group_autopilot_exempt(conversation_id, config):
            return "review"
        return mode
    if bootstrap_enabled_from_config(config) and mode in AUTOMATION_MODES:
        try:
            store.set_automation_mode(conversation_id, mode, source="bootstrap")
        except TypeError:
            # 旧 store / 测试假件无 source 形参 → 按旧签名写（来源留空=未知）
            store.set_automation_mode(conversation_id, mode)
        try:
            from src.inbox.automation_mode_stats import record_bootstrap
            _plat = str(conversation_id or "").split(":", 1)[0]
            record_bootstrap(platform=_plat, conversation_id=conversation_id)
        except Exception:
            pass
    return mode


def group_draft_skip(
    conv: Dict[str, Any],
    store: Any,
    skip_groups: bool,
    *,
    trust_weak_evidence: bool = False,
) -> bool:
    """B 线拟稿是否跳过该群会话（``skip_group_chats`` 语义单源）。

    显式 ``auto_ai`` 的群豁免——坐席经 confirm_group 闸确认过「AI 在这个群自动
    说话」，再按 skip 早退就是「A 线让位（l2 deliver=on）+ B 线跳过」的双让死锁
    （198 事故同构：两边都让、无人拟稿、群永远哑火）。判定异常按旧语义跳过
    （skip 开着时宁静默不误发）。

    **弱证据平台不跳过**（P1 2026-08-20，messenger/instagram 网页启发式）：本函数
    是全链唯一「群 → 一条草稿都不生成」的静默出口，而那两家的群判据是 DOM 猜的。
    猜错 → 真私聊客户永久无人应答且**看板上什么都没有**；不跳过的代价只是真群的
    草稿进人审队列（可见、可拒、可再关）。`trust_weak_evidence=True`
    （``inbox.auto_draft.skip_group_chats_trust_weak_evidence``）恢复旧行为——真群
    噪音压过误判风险时才开。
    """
    if not skip_groups:
        return False
    try:
        from src.inbox.ingest import is_group_conversation
        if not is_group_conversation(conv or {}):
            return False
    except Exception:
        return False
    cid = str((conv or {}).get("conversation_id") or "")
    if not trust_weak_evidence and group_evidence_is_weak(cid):
        return False
    if cid and store is not None:
        try:
            if store.get_automation_mode_if_set(cid) == "auto_ai":
                return False
        except Exception:
            pass
    return True


def allows_direct_autosend(mode: str) -> bool:
    """是否允许直发自动回复（companion A 线 / 主动触达 / protocol 直发）。

    仅 ``auto_ai``（🚀 全自动）放行。``manual`` / ``review`` / ``multi_choice``
    表示坐席接管或「AI 出草稿我审」——直发必须停，改由 System Z 拟稿人审
    （或完全静音）。companion 双轨曾只改收件箱档位、A 线仍直回，本闸是修复点。
    """
    return str(mode or "").strip().lower() == "auto_ai"


def human_gate_skip_reason(mode: str) -> str:
    """非全自动档位的跳过原因码（供 protocol / 观测）。空串=可直发。"""
    m = str(mode or "").strip().lower()
    if m == "auto_ai" or not m:
        return ""
    if m == "manual":
        return "inbox_manual"
    if m in ("review", "multi_choice"):
        return "inbox_human_gate"
    return "inbox_human_gate"


__all__ = [
    "global_automation_mode_from_config",
    "bootstrap_enabled_from_config",
    "resolve_automation_mode",
    "maybe_bootstrap_automation_mode",
    "allows_direct_autosend",
    "human_gate_skip_reason",
    "conversation_is_group",
    "group_autopilot_exempt",
    "group_evidence_is_weak",
    "group_draft_skip",
]
