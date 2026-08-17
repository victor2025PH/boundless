"""合规模式（WP-4 2026-08）：把法规义务做成运营方 opt-in 的开关与导出物。

对应法规（映射表见 docs/智聊合规能力说明_EUAIAct50_SB243_NY1700_2026-08.md）：
- EU AI Act 第 50 条（2026-08-02 生效）：首次交互披露「在与 AI 交互」；
- 加州 SB 243（2026-01-01 生效）：披露 + 危机协议公示 + 年报转介计数；
- 纽约 GBL §1700：AI 陪伴披露义务。

配置组 ``compliance.*``（基线全 false——披露义务属运营方=deployer，产品提供工具，
开关由运营方按属地法规自行开启；见 config.example.yaml 注释）::

    compliance:
      disclosure:
        notice: false            # 系统级披露：新会话首条出站前置披露语（一次性）
        honest_identity: false   # 对话内如实承认：直问「你是 AI 吗」如实回答
        text: ""                 # 可选：披露语整条覆写（{url} 占位；空=内置多语模板）
      crisis_protocol_url: ""    # 运营方《危机干预协议》公示页 URL（进披露语可选）

设计决策（规格 §WP-4）：披露走系统通知层、诚实身份走守卫/prompt 放行层，
**两者独立开关**——多数 B 端客户会开 ① 不开 ②（法规只要求「不误导」，
不要求每句自认）。本包全部纯函数/自包含持久化，任何异常绝不拖垮出站链。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def compliance_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        c = (config or {}).get("compliance")
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _disclosure_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    d = compliance_cfg(config).get("disclosure")
    return d if isinstance(d, dict) else {}


def notice_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """系统级披露开关（``compliance.disclosure.notice``，基线 false）。"""
    try:
        return bool(_disclosure_cfg(config).get("notice"))
    except Exception:
        return False


def honest_identity_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """对话内如实承认开关（``compliance.disclosure.honest_identity``，基线 false）。"""
    try:
        return bool(_disclosure_cfg(config).get("honest_identity"))
    except Exception:
        return False


def notice_text_override(config: Optional[Dict[str, Any]]) -> str:
    try:
        return str(_disclosure_cfg(config).get("text") or "").strip()
    except Exception:
        return ""


def crisis_protocol_url(config: Optional[Dict[str, Any]]) -> str:
    """运营方危机协议公示页 URL（``compliance.crisis_protocol_url``）。"""
    try:
        return str(compliance_cfg(config).get("crisis_protocol_url") or "").strip()
    except Exception:
        return ""


__all__ = [
    "compliance_cfg",
    "notice_enabled",
    "honest_identity_enabled",
    "notice_text_override",
    "crisis_protocol_url",
]
