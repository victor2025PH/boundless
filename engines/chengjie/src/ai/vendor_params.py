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


def thinking_extra_body(base_url: Any, model: Any, *, enabled: bool) -> Dict[str, Any]:
    """按端点给出「思维链开 / 关」的 extra_body（会话级思考开关，2026-09-12）。

    ``enabled=False`` 逐字等于 :func:`thinking_off_extra_body`；``enabled=True`` 对
    vLLM/私网 Qwen3 系显式 ``enable_thinking: true``（``--reasoning-parser qwen3`` 会把思考
    拆进 reasoning_content、正文照常回 content），DeepSeek 官方给 ``thinking.type=enabled``；
    其余端点不加字段（不认识就别乱送）。
    """
    if not enabled:
        return thinking_off_extra_body(base_url, model, reasoning=False)
    base = str(base_url or "").lower()
    model_l = str(model or "").lower()
    if is_deepseek_official(base):
        return {"thinking": {"type": "enabled"}}
    if is_siliconflow(base):
        if any(k in model_l for k in _SF_HYBRID_MARKERS):
            return {"enable_thinking": True}
        return {}
    if (":8001" in base or "vllm" in base or is_private_endpoint(base)
            or model_l.startswith("chatx") or "qwen3" in model_l):
        return {"chat_template_kwargs": {"enable_thinking": True}}
    return {}


# ── 厂商身份（composer「模型」面板 / 开发者页预设共用，2026-09-12）──────────────
# (主机后缀, 厂商键, 展示名, 默认上下文窗口 token)。窗口只用于 UI 按档筛「上下文」
# 档位，取该厂商现役对话模型的**保守**公开值；``ai.models.<n>.max_ctx`` 显式给了永远优先。
_VENDORS: Tuple[Tuple[str, str, str, int], ...] = (
    ("api.deepseek.com", "deepseek", "DeepSeek", 1_000_000),
    ("api.openai.com", "openai", "ChatGPT", 128_000),
    ("generativelanguage.googleapis.com", "gemini", "Gemini", 1_000_000),
    ("api.x.ai", "xai", "Grok", 128_000),
    ("api.anthropic.com", "anthropic", "Claude", 200_000),
    ("api.siliconflow.cn", "siliconflow", "硅基流动", 128_000),
    ("dashscope.aliyuncs.com", "qwen", "通义千问", 128_000),
    ("api.moonshot.cn", "kimi", "Kimi", 128_000),
    ("open.bigmodel.cn", "zhipu", "智谱", 128_000),
    ("openrouter.ai", "openrouter", "OpenRouter", 128_000),
)
DEFAULT_CLOUD_CTX = 128_000
DEFAULT_PRIVATE_CTX = 24_576


def vendor_of(base_url: Any) -> Dict[str, Any]:
    """端点 → ``{key, label, private, default_ctx}``。私网 → ``local``；不认识 → ``other``。"""
    h = host_of(base_url)
    hostname = h.rsplit(":", 1)[0] if ":" in h else h
    if is_private_endpoint(base_url):
        return {"key": "local", "label": "自有算力", "private": True,
                "default_ctx": DEFAULT_PRIVATE_CTX}
    for suffix, key, label, ctx in _VENDORS:
        if hostname == suffix or hostname.endswith("." + suffix):
            return {"key": key, "label": label, "private": False, "default_ctx": ctx}
    return {"key": "other", "label": hostname or "custom", "private": False,
            "default_ctx": DEFAULT_CLOUD_CTX}


def is_ollama_endpoint(base_url: Any) -> bool:
    """Ollama 端点（:11434 / 路径含 ollama）：runner 默认 ``-c 4096``，窗口靠 ``num_ctx`` 随请求下发。"""
    b = str(base_url or "").strip().lower()
    return ":11434" in b or "/ollama" in b


#: Ollama 端点未显式配 ``num_ctx`` 时的保守窗口（历史默认；runner 按此装载）。
DEFAULT_OLLAMA_CTX = 8_192


def resolve_local_num_ctx(fb_cfg: Any, ai_cfg: Any = None) -> Tuple[int, str]:
    """本地对话端点的**输入窗口**口径 → ``(num_ctx, 来源说明)``。

    2026-09-18 实锤：``ai.fallback`` 没配 ``num_ctx`` 时一律落 8192——那是 Ollama 时代的
    假设；现网 173:8001 是 vLLM ``max_model_len=24576``，同一端点在无限制路由按 24576 封顶、
    在 ``ai.primary=local`` 主链却按 8192 裁，人设一个块就把历史裁到 2 条。解析顺序：

    1. ``ai.fallback.num_ctx`` 显式给了 → 原样（运维手调永远优先）；
    2. ``ai.unrestricted.max_ctx`` 显式给了 → 用它（同一台私有模型的窗口事实）；
    3. Ollama 端点 → 8192（runner 要按 num_ctx 装载，不敢默认放大）；
    4. 其余（vLLM / OpenAI 兼容私网）→ :data:`DEFAULT_PRIVATE_CTX`（24576，与 conv_route
       ``endpoint_spec`` 缺省一致）。

    纯函数、绝不抛；脏输入按缺省处理。
    """
    fb = fb_cfg if isinstance(fb_cfg, dict) else {}
    ai = ai_cfg if isinstance(ai_cfg, dict) else {}
    try:
        n = int(fb.get("num_ctx") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n, "ai.fallback.num_ctx"
    unr = ai.get("unrestricted") if isinstance(ai.get("unrestricted"), dict) else {}
    try:
        n = int((unr or {}).get("max_ctx") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n, "ai.unrestricted.max_ctx"
    base = str(fb.get("base_url") or "").strip()
    if is_ollama_endpoint(base):
        return DEFAULT_OLLAMA_CTX, "ollama_default"
    return DEFAULT_PRIVATE_CTX, "private_default"


#: 本地出话预留的地板：回复实测均长 50 字（≈40 token），512 已够；再低会截断长答。
LOCAL_OUTPUT_RESERVE_FLOOR = 512
#: 模板/估算误差余量（与 conv_route.endpoint_prompt_cap 同口径）。
LOCAL_CTX_HEADROOM = 128


def fit_local_budget(prompt_tokens_est: int, num_ctx: int, max_tokens: int,
                     *, floor: int = LOCAL_OUTPUT_RESERVE_FLOOR,
                     headroom: int = LOCAL_CTX_HEADROOM) -> Tuple[int, int]:
    """把「输入预算 / 输出上限」一起装进本地窗口 → ``(prompt_budget, max_tokens_to_send)``。

    旧算法：预算 = num_ctx − max_tokens − 128，然后裁历史。``ai.max_tokens: 4096`` 在
    8192 窗口上只给 prompt 留 3968，人设一个块就超——**先砍输出预留、再裁历史**：
    输出预留 = min(max_tokens, max(floor, num_ctx − prompt_est − headroom))。
    prompt 装得下就不动 max_tokens；装不下先把预留收到 floor，仍装不下才由调用方裁历史。
    返回的 ``max_tokens_to_send`` 必须真的送给端点（vLLM 校验 prompt+max_tokens ≤ 窗口）。
    纯函数、绝不抛。
    """
    try:
        mt = int(max_tokens or 0)
    except (TypeError, ValueError):
        mt = 0
    try:
        ctx = int(num_ctx or 0)
        est = max(0, int(prompt_tokens_est or 0))
    except (TypeError, ValueError):
        return 0, mt
    if ctx <= 0:
        return 0, mt
    fl = max(1, min(int(floor or 1), ctx // 2))
    if mt <= 0:
        mt = fl
    room_for_output = ctx - est - max(0, int(headroom or 0))
    reserve = min(mt, max(fl, room_for_output))
    budget = max(512, ctx - reserve - max(0, int(headroom or 0)))
    return budget, reserve


__all__ = [
    "DEEPSEEK_CURRENT_MODEL", "RETIRED_DEEPSEEK_ALIASES", "DEFAULT_CLOUD_CTX", "DEFAULT_PRIVATE_CTX",
    "DEFAULT_OLLAMA_CTX", "LOCAL_OUTPUT_RESERVE_FLOOR", "LOCAL_CTX_HEADROOM",
    "host_of", "is_deepseek_official", "is_siliconflow", "is_private_endpoint", "is_ollama_endpoint",
    "normalize_model", "thinking_off_extra_body", "thinking_extra_body", "vendor_of",
    "resolve_local_num_ctx", "fit_local_budget",
]
