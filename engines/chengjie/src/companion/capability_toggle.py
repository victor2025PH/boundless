"""陪伴能力「分阶段开启」开闸护栏（纯函数）。

看板的「看→校→**开**」闭环里"开"那一步：把开/关意图喂进来，由本模块**权威**判定
是否放行，路由层只负责写 config overlay + 审计。护栏全在服务端，前端二次确认只是体验。

核心约束（开启方向）：
1. 未知能力 / 无对应开关档 → 拒。
2. 父总开关未开 → 拒（如开 proactive 前必须 companion.enabled）。
3. ⚠ 全自动真发主开关（critical）双重 opt-in：worker 必开 + 至少 1 个 auto_ai 会话，
   否则拒；send-gate 未开不拒但回 warn（裸奔强烈不建议）。
关闭方向一律放行（关真发永远安全；关安全闸危险但由前端二次确认兜底，并回 warn）。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .capability_status import CAPABILITIES, _dig
from .delivery_calibration import delivery_calibration

CAP_BY_KEY: Dict[str, Any] = {c.key: c for c in CAPABILITIES}
_VALID_FIELDS = ("enabled", "dry_run")


def _global_auto_ai(config: Any) -> bool:
    """全局默认档位是否全自动（单一事实源在 ``src.inbox.automation_mode``——
    含缺省 auto_ai 语义与档位合法性校验；导入失败按保守 False＝维持旧判据）。"""
    try:
        from src.inbox.automation_mode import global_automation_mode_from_config
        return global_automation_mode_from_config(config) == "auto_ai"
    except Exception:
        return False


def resolve_flag_path(cap: Any, field: str) -> str:
    """字段→config 路径：enabled 用 flag_path，dry_run 用 dry_run_path。"""
    return cap.dry_run_path if field == "dry_run" else cap.flag_path


# 不在能力注册表里的父总开关 → 人话名（注册表反查优先，这里兜底）。
# 当前注册表全部 parent_path 只有 companion.enabled 一个表外值（rg parent_path= 可复核）。
_PARENT_NAMES = {
    "companion.enabled": "AI 陪伴总开关",
    "inbox.l2_autosend.enabled": "AI 自动拟稿引擎",
}


def _parent_label(parent_path: str) -> str:
    """父总开关的人话名：按注册表反查同 flag_path 能力的 label → 静态表兜底 →
    实在没有才回落原路径（宁可路径也不空说）。

    B37-3（2026-08-22）：护栏拒因此前直接亮 config 路径（`请先开父总开关
    companion.enabled`）——客户包用户不知道那是什么、也没有地方能按路径操作，
    违反人话铁律。反查让拒因说「先打开「AI 陪伴总开关」」这类可行动的话。"""
    for c in CAPABILITIES:
        if getattr(c, "flag_path", "") == parent_path:
            lbl = str(getattr(c, "label", "") or "")
            if lbl:
                return f"「{lbl}」"
    named = _PARENT_NAMES.get(str(parent_path or ""))
    return f"「{named}」" if named else parent_path


def check_toggle(
    config: Any,
    modes: Optional[Mapping[str, str]],
    key: str,
    field: str = "enabled",
    value: bool = True,
) -> Dict[str, Any]:
    """判定单次开关是否放行（纯函数）。

    返回 ``{allowed, reason, flag_path, warn}``：allowed=False 时 reason 为拒因；
    allowed=True 且 warn=True 时 reason 为风险提示（放行但应让运营知情）。
    """
    cap = CAP_BY_KEY.get(key)
    if cap is None:
        return {"allowed": False, "reason": f"未知能力: {key}", "flag_path": "", "warn": False}
    if field not in _VALID_FIELDS:
        return {"allowed": False, "reason": f"未知字段: {field}", "flag_path": "", "warn": False}
    path = resolve_flag_path(cap, field)
    if not path:
        return {"allowed": False, "reason": "该能力无此开关档", "flag_path": "", "warn": False}
    value = bool(value)

    # 关闭方向：永远放行；关安全防护回 warn 让运营知情
    if not value:
        if cap.kind == "safeguard" and field == "enabled":
            return {"allowed": True, "warn": True, "flag_path": path,
                    "reason": f"关闭安全防护「{cap.label}」会移除护栏，请确认风险"}
        return {"allowed": True, "warn": False, "flag_path": path, "reason": ""}

    # 开启方向：前置校验
    # B37-3（2026-08-22）：拒因/警示是客户包 verbatim 直显的文案——不再亮
    # config 路径 / auto_ai 等内部代号，改说用户能听懂、能行动的话。
    if cap.parent_path and not _dig(config, cap.parent_path, cap.parent_default):
        return {"allowed": False, "warn": False, "flag_path": path,
                "reason": f"需要先打开上级功能 {_parent_label(cap.parent_path)}，再开这一项"}

    if cap.critical and field == "enabled":
        cal = delivery_calibration(config, modes)
        sw = cal["switches"]
        blockers = []
        if not sw["worker"]:
            blockers.append("需先打开「AI 自动拟稿引擎」——引擎不开，AI 写好的回复"
                            "无人投递（设置页「AI 接管」选「值守中」可一键开齐）")
        # 「至少 1 个 auto_ai 会话」只在全局默认档**不是**全自动时才成立——
        # 全局档=auto_ai（+bootstrap 默认随行）时，新会话首条入站即落全自动，
        # 「不会对任何人真发」的前提不存在。旧判据对新装机（零会话）是先有鸡
        # 还是先有蛋的死锁（impl49 B37：两位内测用户 100% 卡死在向导第 4 步）。
        if cal["automation_modes"]["auto_ai"] <= 0 and not _global_auto_ai(config):
            blockers.append("需至少把 1 条会话切到「🚀 全自动」，或把默认档位设为"
                            "全自动——否则 AI 不会对任何人真发")
        if blockers:
            return {"allowed": False, "warn": False, "flag_path": path,
                    "reason": "；".join(blockers)}
        if not sw["send_gate"]:
            return {"allowed": True, "warn": True, "flag_path": path,
                    "reason": "真发将开启，但「出站安全闸」（内容与频率防护）还没开"
                              "——强烈建议同时开启"}

    if cap.key == "realtime_voice" and field == "enabled":
        from src.companion.realtime_voice_readiness import realtime_voice_host_configured, _rtv_cfg
        if not realtime_voice_host_configured(config):
            return {"allowed": False, "warn": False, "flag_path": path,
                    "reason": "请先配置 realtime_voice.base_url（MiniCPM-o 语音主机）"}
        if not str(_rtv_cfg(config).get("access_token") or "").strip():
            return {"allowed": True, "warn": True, "flag_path": path,
                    "reason": "未配置 access_token → 试拨/引擎 API 无口令保护（公网暴露前务必设置）"}

    return {"allowed": True, "warn": False, "flag_path": path, "reason": ""}


__all__ = ["CAP_BY_KEY", "resolve_flag_path", "check_toggle"]
