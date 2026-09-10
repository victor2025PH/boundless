"""厂商参数口径（单一事实源，2026-09-11 DeepSeek V4.1-Flash 切链配套）。

两件事，AIClient 主链 / key_pool / models 路由 / 助手直连 / 探活 ping 都从这里取，
不再各写一份「deepseek-v4 才关思考」的判断：

1. ``thinking_off_extra_body(base_url, model)`` —— 按**端点主机**决定「关思维链」字段：
   - api.deepseek.com：``{"thinking": {"type": "disabled"}}``（官方强校验，布尔值 422）。
     V4.1-Flash 起官方唯一对话模型 ``deepseek-flash`` **默认开思考**，且思考与正文共享
     max_tokens → 不关就是 2026-08-17「空响应」事故重演；按主机关而不按模型名关，
     是因为退役别名（deepseek-chat / deepseek-v4-flash）在过渡期照样被路由到 V4.1，
     按名字判会漏。
   - api.siliconflow.cn：``{"enable_thinking": False}``（混合推理档：DeepSeek-V3.1+/V4、
     Qwen3、GLM-4.5+）。不带它 V4-Flash 的 ``</think>`` 会直接混进 content
     （2026-08-23 实测）；非混合档不下发（硅基对未知字段不保证宽容）。
   - vLLM / 私网 Qwen3 系：``{"chat_template_kwargs": {"enable_thinking": False}}``。
   - ``reasoning=True`` → ``{}``（显式要思维链时一律不干预）。

2. ``normalize_model(base_url, model)`` —— DeepSeek 官方**退役别名归一**到 ``deepseek-flash``：
   deepseek-chat（07-24 退役）、deepseek-v4-flash / -vision-exp（09-10 退役）。存量配置
   （客户机 overlay、示例、池条目）不必手改，进程装载时归一并打 INFO 日志；其他主机
   （硅基 ``deepseek-ai/DeepSeek-V4-Flash``、LAN chatx）原样不动。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

DEEPSEEK_CURRENT_MODEL = "deepseek-flash"

# 官方退役别名 → 现役。deepseek-reasoner / deepseek-v4-pro 刻意不映射：前者语义是
# 「要思维链」，后者 09-14 起官方自己路由到 V4.1；两者都不在我们的机队里。
RETIRED_DEEPSEEK_ALIASES: Dict[str, str] = {
    "deepseek-chat": DEEPSEEK_CURRENT_MODEL,
    "deepseek-v4-flash": DEEPSEEK_CURRENT_MODEL,
    "deepseek-v4-flash-vision-exp": DEEPSEEK_CURRENT_MODEL,
}

# 硅基上受 enable_thinking 控制的混合推理档（模型名小写子串匹配）
_SF_HYBRID_MARKERS = (
    "deepseek-v3.1", "deepseek-v3.2", "deepseek-v4", "qwen3", "glm-4.5", "glm-4.6", "glm-5",
)

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.|10\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|192\.168\.|"
    r"172\.(1[6-9]|2\d|3[01])\.)")


def host_of(base_url: Any) -> str:
    b = str(base_url or "").strip().lower()
    if "://" in b:
        b = b.split("://", 1)[1]
    return b.split("/", 1)[0]


def is_deepseek_official(base_url: Any) -> bool:
    return host_of(base_url).endswith("api.deepseek.com")


def is_siliconflow(base_url: Any) -> bool:
    return "siliconflow" in host_of(base_url)


def is_private_endpoint(base_url: Any) -> bool:
    h = host_of(base_url)
    hostname = h.rsplit(":", 1)[0] if ":" in h else h
    return bool(_PRIVATE_HOST_RE.match(hostname))


def normalize_model(base_url: Any, model: Any) -> Tuple[str, Optional[str]]:
    """返回 (归一后模型名, 说明)；无需归一时说明为 None。仅对 DeepSeek 官方主机生效。"""
    m = str(model or "").strip()
    if not m or not is_deepseek_official(base_url):
        return m, None
    target = RETIRED_DEEPSEEK_ALIASES.get(m.lower())
    if not target or target == m:
        return m, None
    return target, f"DeepSeek 退役模型名 {m} → {target}（官方已下线该别名，自动归一）"


def thinking_off_extra_body(base_url: Any, model: Any, *,
                            reasoning: bool = False) -> Dict[str, Any]:
    """按端点主机给出「关思维链」的 extra_body；不需要/不认识的端点返回 ``{}``。"""
    if reasoning:
        return {}
    base = str(base_url or "").lower()
    model_l = str(model or "").lower()
    if is_deepseek_official(base):
        return {"thinking": {"type": "disabled"}}
    if is_siliconflow(base):
        if any(k in model_l for k in _SF_HYBRID_MARKERS):
            return {"enable_thinking": False}
        return {}
    # vLLM 上的 Qwen3 系（LAN 落点 173:8001 chatx=Qwen3.6-27B）默认开 thinking：
    # 正文全进 reasoning、content 恒 null。键名与 translation_engines /
    # voice_colloquial_llm / ai_client 兜底档同源。
    if (":8001" in base or "vllm" in base or is_private_endpoint(base)
            or model_l.startswith("chatx") or "qwen3" in model_l):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


__all__ = [
    "DEEPSEEK_CURRENT_MODEL", "RETIRED_DEEPSEEK_ALIASES",
    "host_of", "is_deepseek_official", "is_siliconflow", "is_private_endpoint",
    "normalize_model", "thinking_off_extra_body",
]
