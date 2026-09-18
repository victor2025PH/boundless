# -*- coding: utf-8 -*-
"""会话级模型路由 + 「无限制」模式（2026-09-12，composer 模型选择器后端单一事实源）。

背景
====
坐席在 composer 底栏（键盘图标左侧）像 Cursor 一样按**会话**挑模型：
``标准``（实例主链：云端 + key_pool + LAN 兜底，规则全开）或 ``无限制``（局域网私有
模型——现网 173:8001 ``chatx``＝Qwen3.8-27B-abliterated，去审查权重——**只走该端点、
不回落云端**，且本会话的人设规则 / 出站改写守卫 / 风控分级 / 手发 409 护栏全部让路）。
再带三个 Cursor 式旋钮：上下文深度档（复用 :mod:`src.ai.context_depth` 四档：
云端 12k / 32k / 128k / 900k；本机无限制按端点窗口重画，满窗＝现役 ``max_model_len``）、
力度（低/中/高 —— **答复长度 / 温度的近似**，不是推理力度，UI 明示）、
思考开关（vLLM ``chat_template_kwargs.enable_thinking``）。

存储
====
复用 ``InboxStore`` 通用 KV ``app_settings``（键 ``conv_model_route:<cid>``，值 JSON），
与 :mod:`src.inbox.risk_hold` / ``ai_fail_marker`` 同一先例——**不动 store.py / 不建表**。
缺席＝:meth:`Route.standard`（逐字节旧行为）。

两级安全刹车（老板拍板 2026-09-12）
================================
``unrestricted`` 只短路**质量 / 人设 / 业务**层。五条「账号与人身保险丝」
（Kill-Switch / ban_signal、账号冷启动 send_gate、stop_contact 冻结、危机自伤兜底、
授权 / token 硬拒）默认仍生效；要连它们也关，有两把钥匙、任一为真即全关：

- 会话级 ``bypass_safety``（composer 弹层「安全刹车：全关」行，逐会话、有审计）；
- 全局 YAML ``ai.unrestricted.bypass_safety_brakes: true``（默认 false）。

消费方统一问 :func:`skip_guard` / :func:`bypass_safety_active`，不要各自读 KV。

授权
====
``licensing.feature_gate`` 登记 ``unrestricted_model``（flagship）。闸门总开关默认关＝
全放行（launch day 不会把坐席锁在外面）；开闸后非旗舰档 POST 开无限制 → 403，GET 里
``allowed=false`` 让 UI 灰显 + 锁标。
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "conv_model_route:"

PROFILE_STANDARD = "standard"
PROFILE_UNRESTRICTED = "unrestricted"
PROFILES = (PROFILE_STANDARD, PROFILE_UNRESTRICTED)

#: 上下文档位：空串＝跟随全局 ``ai.context_depth``；其余复用 context_depth 四档键。
#: 无限制 UI 按端点窗口筛档；``max`` 在本机上表示满窗（发送时封顶），云端仍是 128k。
DEPTH_FOLLOW = ""
DEPTH_CHOICES = (DEPTH_FOLLOW, "standard", "deep", "max", "ultra")

#: 力度三档（≈ 答复长度 / 温度；空串＝跟随策略）。
EFFORT_FOLLOW = ""
EFFORT_CHOICES = (EFFORT_FOLLOW, "low", "medium", "high")
EFFORT_PARAMS: Dict[str, Dict[str, Any]] = {
    "low": {"max_tokens": 512, "temperature": 0.6},
    "medium": {"max_tokens": 1024, "temperature": 0.7},
    "high": {"max_tokens": 2048, "temperature": 0.8},
}

#: 切到无限制时的默认旋钮（坐席不必四下点完才能用）。
UNRESTRICTED_DEFAULTS = {"depth": "max", "effort": "high", "thinking": False}

FEATURE_NAME = "unrestricted_model"

#: 端点硬上限（token）→ 允许的最高*云端*深度档。本机 24k 吃不下 32k+，
#: allowed_depths 会额外放出 ``max`` 当满窗键（发送路径再封顶）。
_DEPTH_ORDER = ("standard", "deep", "max", "ultra")
_DEPTH_MIN_CTX = {"standard": 0, "deep": 32_000, "max": 64_000, "ultra": 256_000}

# ── 质量层清单（skip_guard 只认这些名字；未登记的层名一律**不**跳过＝安全默认）──
QUALITY_LAYERS = frozenset({
    # 生成前 prompt 规则
    "global_rules", "spoken_style", "language_rule", "temper_inject", "media_capability_hint",
    # 生成后改写
    "persona_guard", "outbound_text_guard", "photo_capability_sanitize", "fabrication_guard",
    "status_fabrication_guard", "world_clock_guard", "goal_link_guard", "temper_output_guard",
    "media_promise_guard", "song_claim_guard", "spoken_style_cleanup", "draft_humanize",
    "commitment_guard", "adult_grader", "risk_grader",
    # 分级 / 发送时业务护栏
    "risk_level", "risk_hold", "needs_human_tag", "skill_cooldown", "lang_mismatch",
    "outbound_dup", "stale_approve",
})
#: 安全刹车层：只有 bypass_safety 才跳。
SAFETY_LAYERS = frozenset({
    "crisis_safety_net", "hard_stop", "kill_switch", "ban_signal", "send_gate_warmup",
    "license_quota",
})


# ─────────────────────────────────────────────────────────────────────────────
# Route
# ─────────────────────────────────────────────────────────────────────────────

#: ``Route.model``（2026-09-12 拆面板）：标准模式下本会话「用谁答」＝ ``ai.models`` 档名；
#: 空串＝跟随实例主链。无限制模式的端点由模式自己决定，此字段恒空。
MODEL_FOLLOW = ""
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")

#: 坐席可见的 ChatX 产品名（不再带参数规模「27B」）。前端 i18n ``inbox.mp.vendor_local``
#: 会覆盖展示；本常量是目录 / 端点事实 / 无 i18n 消费方的兜底。
CHATX_DISPLAY_LABEL = "ChatX聊天模型"
#: 局域网 ChatX 的路径标签：不暴露 RFC1918，也不再用客户词「办公室」。
PUBLIC_HOST_LAN_CHATX = "局域网直连"


@dataclass(frozen=True)
class Route:
    profile: str = PROFILE_STANDARD
    depth: str = DEPTH_FOLLOW
    effort: str = EFFORT_FOLLOW
    thinking: bool = False
    bypass_safety: bool = False
    model: str = MODEL_FOLLOW
    updated_by: str = ""
    updated_at: float = 0.0

    @classmethod
    def standard(cls) -> "Route":
        return cls()

    @property
    def unrestricted(self) -> bool:
        return self.profile == PROFILE_UNRESTRICTED

    @property
    def is_default(self) -> bool:
        return (self.profile == PROFILE_STANDARD and not self.depth and not self.effort
                and not self.thinking and not self.bypass_safety and not self.model)

    @property
    def route_key(self) -> str:
        """ai_client ``_route`` 值：无限制 → 无限制档；标准 + 选了模型 → 该档；否则空。"""
        if self.unrestricted:
            return "profile:" + profile_name()
        if self.model:
            return "profile:" + self.model
        return ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile, "depth": self.depth, "effort": self.effort,
            "thinking": bool(self.thinking), "bypass_safety": bool(self.bypass_safety),
            "model": self.model, "unrestricted": self.unrestricted,
            "updated_by": self.updated_by, "updated_at": self.updated_at,
        }

    def strategy_overrides(self) -> Dict[str, Any]:
        """力度档 → ``strategy_overrides``（只在显式选了档位时给；空档不动策略值）。"""
        p = EFFORT_PARAMS.get(self.effort)
        return dict(p) if p else {}

    def apply_context(self, ctx: Dict[str, Any]) -> None:
        """把路由写进 ``user_context``（ai_client / skill_manager 守卫链消费的键）。

        标准档只清键（防上一轮残留），不加任何东西＝逐字节旧行为。
        """
        for k in ("_route", "_route_strict", "_thinking", "_unrestricted",
                  "_unrestricted_bypass_safety", "_conv_route"):
            ctx.pop(k, None)
        if self.is_default:
            return
        ctx["_conv_route"] = self.as_dict()
        if self.thinking:
            ctx["_thinking"] = True
        if self.unrestricted:
            ctx["_route"] = "profile:" + profile_name()
            ctx["_route_strict"] = True
            ctx["_unrestricted"] = True
            if self.bypass_safety:
                ctx["_unrestricted_bypass_safety"] = True
        elif self.model:
            # 标准模式点名厂商模型：只换「主尝试」的端点，**不**严格——该档离线/失败
            # 照走 备用池 → 本地兜底 → canned 降级链（规则全开，与主链同一条命）。
            ctx["_route"] = "profile:" + self.model


def normalize(payload: Mapping[str, Any], *, base: Optional[Route] = None,
              config: Any = None) -> Route:
    """宽松归一：非法值回落到 base（或默认）。``profile`` 切到无限制且未显式给旋钮
    → 灌 :data:`UNRESTRICTED_DEFAULTS`；切回标准 → 旋钮清空（跟随全局）。

    ``model`` 若是无限制档名（默认 ``unrestricted``，或 ``ai.unrestricted.profile``）
    且没显式给 ``profile`` → 视为切到无限制（ChatX 目录行＝无限制，不再当「规则全开
    的自有算力档」）。无限制会话里点选云厂商且没显式给 profile → 改回标准。"""
    r = base or Route.standard()
    prof = str(payload.get("profile", r.profile) or PROFILE_STANDARD).strip().lower()
    if prof not in PROFILES:
        prof = r.profile
    # 模型字段：非法名回落 base；「点了 ChatX 目录行」→ 开无限制；
    # 「无限制会话里点选了云厂商」且没显式给 profile → 改回标准（两者天然互斥）。
    model_in = "model" in payload
    model_v = r.model
    if model_in:
        mv = str(payload.get("model") or "").strip()
        if not mv:
            model_v = MODEL_FOLLOW
        elif _MODEL_NAME_RE.match(mv):
            model_v = mv
        if model_opens_unrestricted(model_v, config):
            if "profile" not in payload:
                prof = PROFILE_UNRESTRICTED
            model_v = MODEL_FOLLOW
        elif model_v and "profile" not in payload and r.unrestricted:
            prof = PROFILE_STANDARD
    switched_to_unr = prof == PROFILE_UNRESTRICTED and r.profile != PROFILE_UNRESTRICTED
    switched_to_std = prof == PROFILE_STANDARD and r.profile != PROFILE_STANDARD
    if switched_to_unr:
        r = replace(r, profile=prof, depth=UNRESTRICTED_DEFAULTS["depth"],
                    effort=UNRESTRICTED_DEFAULTS["effort"],
                    thinking=bool(UNRESTRICTED_DEFAULTS["thinking"]))
    elif switched_to_std:
        r = replace(r, profile=prof, depth=DEPTH_FOLLOW, effort=EFFORT_FOLLOW,
                    thinking=False, bypass_safety=False)
    else:
        r = replace(r, profile=prof)
    if "depth" in payload:
        d = str(payload.get("depth") or "").strip().lower()
        if d in DEPTH_CHOICES:
            r = replace(r, depth=d)
    if "effort" in payload:
        e = str(payload.get("effort") or "").strip().lower()
        if e in EFFORT_CHOICES:
            r = replace(r, effort=e)
    if "thinking" in payload:
        r = replace(r, thinking=_truthy(payload.get("thinking")))
    if "bypass_safety" in payload:
        r = replace(r, bypass_safety=_truthy(payload.get("bypass_safety")))
    if model_in:
        r = replace(r, model=model_v)
    if r.unrestricted:
        # 无限制的端点由模式自己定（ai.unrestricted.profile / ai.fallback），模型字段恒空
        r = replace(r, model=MODEL_FOLLOW)
    else:
        # 安全刹车全关只对无限制会话有意义；标准档不许挂这把钥匙
        r = replace(r, bypass_safety=False)
    return r


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────

def _root(config: Any) -> Dict[str, Any]:
    if config is None:
        try:
            from src.compliance.runtime import runtime_config
            return runtime_config() or {}
        except Exception:
            return {}
    if isinstance(config, Mapping):
        return dict(config)
    inner = getattr(config, "config", None)
    return dict(inner) if isinstance(inner, Mapping) else {}


def _section(config: Any) -> Dict[str, Any]:
    ai = _root(config).get("ai") or {}
    sec = ai.get("unrestricted") if isinstance(ai, Mapping) else None
    return dict(sec) if isinstance(sec, Mapping) else {}


def enabled(config: Any = None) -> bool:
    """``ai.unrestricted.enabled``（默认 True：入口可见；无会话选它＝零行为变化）。"""
    try:
        return bool(_section(config).get("enabled", True))
    except Exception:
        return True


def profile_name(config: Any = None) -> str:
    """无限制档指向的 ``ai.models`` 档名（默认 ``unrestricted``）。"""
    try:
        return str(_section(config).get("profile") or PROFILE_UNRESTRICTED).strip() or PROFILE_UNRESTRICTED
    except Exception:
        return PROFILE_UNRESTRICTED


def model_opens_unrestricted(name: str, config: Any = None) -> bool:
    """该 ``ai.models`` 档名是否就是无限制入口（ChatX 目录行）。

    认两件事：字面量 ``unrestricted``，或配置里 ``ai.unrestricted.profile`` 指向的档。
    其它也叫 chatx 的局域网档（例如预设 ``lan_chatx``）不自动开无限制。"""
    n = str(name or "").strip()
    if not n:
        return False
    if n.lower() == PROFILE_UNRESTRICTED:
        return True
    try:
        return n == profile_name(config)
    except Exception:
        return False


def global_bypass_safety(config: Any = None) -> bool:
    """全局钥匙 ``ai.unrestricted.bypass_safety_brakes``（默认 false）。"""
    try:
        return bool(_section(config).get("bypass_safety_brakes", False))
    except Exception:
        return False


def endpoint_spec(config: Any = None) -> Dict[str, Any]:
    """无限制档的端点事实：``ai.models.<profile>`` 显式档优先；缺席时回落 ``ai.fallback``
    （LAN 兜底端点＝局域网私有模型，现网即 173:8001/chatx）。两者皆无 → ``{}``＝不可用。

    返回 ``{base_url, model, source, max_ctx, supports_thinking, host}``，api_key 不出。
    """
    root = _root(config)
    ai = root.get("ai") or {}
    name = profile_name(root)
    models = ai.get("models") if isinstance(ai, Mapping) else None
    spec: Dict[str, Any] = {}
    source = ""
    if isinstance(models, Mapping) and isinstance(models.get(name), Mapping):
        spec = dict(models[name])
        source = f"ai.models.{name}"
    else:
        fb = ai.get("fallback") if isinstance(ai, Mapping) else None
        if isinstance(fb, Mapping) and fb.get("base_url") and fb.get("enabled", True):
            spec = {"base_url": fb.get("base_url"), "model": fb.get("model"),
                    "max_ctx": fb.get("num_ctx")}
            source = "ai.fallback"
    base = str(spec.get("base_url") or "").strip().rstrip("/")
    model = str(spec.get("model") or "").strip()
    if not base or not model:
        return {}
    host = base.split("://", 1)[-1].split("/", 1)[0]
    try:
        max_ctx = int(spec.get("max_ctx") or _section(root).get("max_ctx") or 24_576)
    except (TypeError, ValueError):
        max_ctx = 24_576
    via = _path_via(base)
    chatx = model.lower() == "chatx"
    return {
        "base_url": base, "model": model, "source": source, "host": host,
        "max_ctx": max_ctx,
        "supports_thinking": bool(spec.get("supports_thinking", True)),
        "via": via,
        "public_host": public_host_for(base, model),
        "private": via == "lan",
        "label": CHATX_DISPLAY_LABEL if chatx else "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 模型目录（composer「模型」面板：标准模式下本会话「用谁答」）
# ─────────────────────────────────────────────────────────────────────────────

def _vendor(base_url: str) -> Dict[str, Any]:
    try:
        from src.ai.vendor_params import vendor_of
        return vendor_of(base_url)
    except Exception:
        return {"key": "other", "label": "", "private": False, "default_ctx": 128_000}


def _path_via(base: str) -> str:
    """流量怎么走：lan＝RFC1918 直连；hosted＝官网 /api/ai/v1 中继；cloud＝公网厂商。"""
    b = str(base or "").lower()
    if "/api/ai/v1" in b or "bd2026.cc" in b:
        return "hosted"
    try:
        from src.ai.vendor_params import is_private_endpoint
        if is_private_endpoint(base):
            return "lan"
    except Exception:
        pass
    return "cloud"


def public_host_for(host_or_base: str, model: str = "") -> str:
    """给坐席看的路径：不暴露 RFC1918。host 或完整 base_url 都能吃。"""
    raw = str(host_or_base or "").strip()
    if not raw:
        return ""
    base = raw if "://" in raw else f"http://{raw}"
    via = _path_via(base)
    chatx = str(model or "").strip().lower() == "chatx"
    if via == "lan":
        return PUBLIC_HOST_LAN_CHATX if chatx else "自有算力"
    if via == "hosted":
        return "经官网"
    return raw.split("://", 1)[-1].split("/", 1)[0]


def _spec_row(name: str, spec: Mapping[str, Any], *, source: str, label: str = "") -> Dict[str, Any]:
    base = str(spec.get("base_url") or "").strip().rstrip("/")
    model = str(spec.get("model") or "").strip()
    if not base or not model:
        return {}
    v = _vendor(base)
    try:
        max_ctx = int(spec.get("max_ctx") or spec.get("num_ctx") or 0)
    except (TypeError, ValueError):
        max_ctx = 0
    if max_ctx <= 0:
        max_ctx = int(v.get("default_ctx") or 128_000)
    host = base.split("://", 1)[-1].split("/", 1)[0]
    via = _path_via(base)
    chatx = model.lower() == "chatx"
    shown = label or str(spec.get("label") or "").strip()
    if chatx:
        shown = shown or CHATX_DISPLAY_LABEL
    elif via == "lan":
        shown = shown or str(v.get("label") or "") or "自有算力"
    else:
        shown = shown or str(v.get("label") or "") or host
    public_host = public_host_for(base, model)
    return {
        "name": name, "label": shown,
        "vendor": "local" if (chatx or via == "lan") else (v.get("key") or "other"),
        "model": model, "host": host, "base_url": base,
        "private": via == "lan", "via": via, "public_host": public_host,
        "max_ctx": max_ctx, "source": source,
        "cost_hint": str(spec.get("cost_hint") or "").strip()[:24],
        "supports_thinking": bool(spec.get("supports_thinking", True)),
    }


def main_chain_spec(config: Any = None) -> Dict[str, Any]:
    """实例主链（``ai.base_url`` / ``ai.model``）作为目录首项；缺配置 → ``{}``。"""
    ai = _root(config).get("ai") or {}
    if not isinstance(ai, Mapping):
        return {}
    row = _spec_row(MODEL_FOLLOW, {"base_url": ai.get("base_url"), "model": ai.get("model"),
                                   "max_ctx": ai.get("max_ctx")}, source="ai")
    return row


def model_catalog(config: Any = None) -> list:
    """可点名的模型档：主链（name=""）+ ``ai.models`` 每档（跳过 ``_`` 前缀内部档）。

    无限制档名（``ai.unrestricted.profile``）若在 ``ai.models`` 里显式配了，也列出，
    并打 ``opens_unrestricted=true``——点它＝切到无限制（ChatX 直答、规则让路），
    不再当「标准模式下的自有算力档」。``ai.models`` 没配而 ``ai.fallback`` 有端点时
    同样列出（``source=ai.fallback``）。前端按 ``via``（lan/hosted/cloud）画数据
    路径，不把 RFC1918 地址亮给坐席。
    """
    root = _root(config)
    ai = root.get("ai") or {}
    out: list = []
    main = main_chain_spec(root)
    if main:
        out.append(main)
    models = ai.get("models") if isinstance(ai, Mapping) else None
    seen: Dict[str, bool] = {}   # 模块里 set() 是存储函数名，别用内置 set
    if isinstance(models, Mapping):
        for name, spec in models.items():
            n = str(name or "").strip()
            if not n or n.startswith("_") or not _MODEL_NAME_RE.match(n) or not isinstance(spec, Mapping):
                continue
            row = _spec_row(n, spec, source=f"ai.models.{n}")
            if row:
                out.append(row)
                seen[n] = True
    unr = profile_name(root)
    if unr not in seen:
        fb = ai.get("fallback") if isinstance(ai, Mapping) else None
        if isinstance(fb, Mapping) and fb.get("base_url") and fb.get("enabled", True):
            row = _spec_row(unr, {"base_url": fb.get("base_url"), "model": fb.get("model"),
                                  "max_ctx": fb.get("num_ctx") or _section(root).get("max_ctx")},
                            source="ai.fallback")
            if row:
                out.append(row)
    for row in out:
        row["opens_unrestricted"] = bool(row.get("name") and row["name"] == unr)
    return out


def model_spec(config: Any, name: str) -> Dict[str, Any]:
    """目录里按档名取一行；``""`` ＝主链；没有 → ``{}``。"""
    n = str(name or "").strip()
    for row in model_catalog(config):
        if row.get("name") == n:
            return dict(row)
    return {}


def active_endpoint(route: Route, config: Any = None) -> Dict[str, Any]:
    """本会话实际「用谁答」的端点事实：无限制 → 无限制端点；标准+模型 → 该档；否则主链。"""
    if route.unrestricted:
        spec = endpoint_spec(config)
        if spec:
            spec = dict(spec)
            spec.setdefault("name", profile_name(config))
            spec.setdefault("private", True)
        return spec
    if route.model:
        row = model_spec(config, route.model)
        if row:
            return row
    return main_chain_spec(config)


#: 无限制发送时从 ``max_ctx`` 里给补全留的空位（高档力度 2048 + 模板余量）。
_ENDPOINT_OUTPUT_RESERVE = 2048
_ENDPOINT_HEADROOM = 128


def endpoint_prompt_cap(config: Any = None, *, max_tokens: Optional[int] = None) -> int:
    """无限制端点的**输入**预算上限。无端点 → 0（不封顶）。

    ``max_ctx - 补全预留 - 128``，避免 prompt+output 超过 vLLM ``max_model_len`` 直接 400。
    补全预留：显式 ``max_tokens`` > 当前路由力度 > 高档 2048。
    """
    spec = endpoint_spec(config)
    try:
        max_ctx = int(spec.get("max_ctx") or 0)
    except (TypeError, ValueError):
        max_ctx = 0
    if max_ctx <= 0:
        return 0
    mt = int(max_tokens or 0)
    if mt <= 0:
        r = active_route()
        if r is not None:
            mt = int((EFFORT_PARAMS.get(r.effort) or {}).get("max_tokens") or 0)
        if mt <= 0:
            mt = _ENDPOINT_OUTPUT_RESERVE
    return max(1024, max_ctx - mt - _ENDPOINT_HEADROOM)


def depth_cap(max_ctx: int) -> str:
    """端点上下文硬上限 → 允许的最高深度档键。"""
    best = "standard"
    for k in _DEPTH_ORDER:
        if int(max_ctx or 0) >= _DEPTH_MIN_CTX[k]:
            best = k
    return best


def _depths_fitting(max_ctx: int, *, fill_key: bool) -> list:
    out = [DEPTH_FOLLOW, "standard"]
    try:
        from src.ai.context_depth import TIERS
    except Exception:
        TIERS = {}
    for k in ("deep", "max", "ultra"):
        d = TIERS.get(k) if isinstance(TIERS, dict) else None
        budget = getattr(d, "prompt_budget_tokens", None) if d is not None else None
        if budget and int(budget) <= max_ctx:
            out.append(k)
    if fill_key and "max" not in out and max_ctx > 12_000:
        out.append("max")
    return out


def allowed_depths(config: Any = None, *, unrestricted: bool = False, model: str = "") -> list:
    """UI 可选深度档（含空串＝跟随全局）。

    云端主链：四档全放。本机无限制：只放预算 ≤ 端点窗口的档，并保证有一档
    ``max``＝满窗（发送时 :func:`endpoint_prompt_cap` 封顶到 ``max_ctx``）。
    标准模式点名了模型档：按该档窗口筛（``ai.models.<n>.max_ctx`` / 厂商缺省），不造满窗键。
    """
    if unrestricted:
        spec = endpoint_spec(config)
        try:
            max_ctx = int((spec or {}).get("max_ctx") or 0)
        except (TypeError, ValueError):
            max_ctx = 0
        if max_ctx <= 0:
            max_ctx = 24_576
        return _depths_fitting(max_ctx, fill_key=True)
    if model:
        row = model_spec(config, model)
        if row:
            return _depths_fitting(int(row.get("max_ctx") or 0), fill_key=False)
    return list(DEPTH_CHOICES)


def effective_depth(route: Route, config: Any = None) -> str:
    """路由深度档按端点筛过后的实际档；空串＝跟随全局。"""
    if not route.depth:
        return DEPTH_FOLLOW
    d = str(route.depth).strip().lower()
    allowed = allowed_depths(config, unrestricted=route.unrestricted, model=route.model)
    if d in allowed:
        return d
    return allowed[-1] if len(allowed) > 1 else DEPTH_FOLLOW


# ─────────────────────────────────────────────────────────────────────────────
# 授权闸门
# ─────────────────────────────────────────────────────────────────────────────

def feature_allowed(config: Any = None) -> bool:
    """``licensing.feature_gate`` 是否放行 ``unrestricted_model``（闸关 / 异常 → True）。"""
    try:
        from src.licensing.feature_gate import feature_enabled
        return bool(feature_enabled(FEATURE_NAME, _root(config) or None))
    except Exception:
        return True


VENDOR_FEATURE_NAME = "multi_vendor_model"


def vendor_allowed(config: Any = None) -> bool:
    """``licensing.feature_gate`` 是否放行 ``multi_vendor_model``（按会话点名厂商档；主链恒可选）。"""
    try:
        from src.licensing.feature_gate import feature_enabled
        return bool(feature_enabled(VENDOR_FEATURE_NAME, _root(config) or None))
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# 存储（app_settings KV）
# ─────────────────────────────────────────────────────────────────────────────

def conv_id(platform: str, account_id: str, chat_key: str) -> str:
    p = str(platform or "").strip().lower()
    a = str(account_id or "default").strip() or "default"
    c = str(chat_key or "").strip()
    if not p or not c:
        return ""
    return f"{p}:{a}:{c}"


def _key(cid: str) -> str:
    return KEY_PREFIX + str(cid or "").strip()


def _parse(raw: Any) -> Optional[Route]:
    try:
        got = json.loads(str(raw or "") or "{}")
    except Exception:
        return None
    if not isinstance(got, dict) or not got.get("profile"):
        return None
    r = normalize(got)
    try:
        r = replace(r, updated_by=str(got.get("updated_by") or ""),
                    updated_at=float(got.get("updated_at") or 0.0))
    except Exception:
        pass
    return r


def get(store: Any, cid: str) -> Route:
    """会话路由；无 / 脏 / store 缺席 → :meth:`Route.standard`。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return Route.standard()
    try:
        r = _parse(store.get_app_setting(_key(cid), ""))
        return r or Route.standard()
    except Exception:
        return Route.standard()


def set(store: Any, cid: str, patch: Mapping[str, Any], *, by: str = "agent",   # noqa: A001
        now: Optional[float] = None, config: Any = None) -> Optional[Route]:
    """按 patch 归一后落库；结果为默认路由 → 删键。返回生效路由；store 缺席 → None。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return None
    prev = get(store, cid)
    ts = float(now if now is not None else time.time())
    nxt = replace(normalize(patch, base=prev, config=config),
                  updated_by=str(by or "agent"), updated_at=ts)
    try:
        if nxt.is_default:
            store.set_app_setting(_key(cid), "", updated_by="conv_route")
        else:
            store.set_app_setting(_key(cid), json.dumps(nxt.as_dict(), ensure_ascii=False),
                                  updated_by="conv_route")
    except Exception:
        logger.debug("[conv_route] 写入失败 conv=%s", cid, exc_info=True)
        return None
    if prev.as_dict() != nxt.as_dict():
        # 审计：谁在何时给哪个会话开 / 关了无限制、动了哪把安全钥匙（合规与市场都要这条账）
        logger.info("[conv_route] conv=%s profile=%s->%s model=%s->%s depth=%s effort=%s "
                    "thinking=%d bypass_safety=%d->%d by=%s",
                    cid, prev.profile, nxt.profile, prev.model or "main", nxt.model or "main",
                    nxt.depth or "follow", nxt.effort or "follow", int(nxt.thinking),
                    int(prev.bypass_safety), int(nxt.bypass_safety), nxt.updated_by)
    return nxt


def clear(store: Any, cid: str, *, by: str = "agent") -> bool:
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return False
    prev = get(store, cid)
    try:
        store.set_app_setting(_key(cid), "", updated_by="conv_route")
    except Exception:
        return False
    if not prev.is_default:
        logger.info("[conv_route] conv=%s cleared (was %s) by=%s", cid, prev.profile, by)
    return True


def all_routes(store: Any) -> Dict[str, Route]:
    """全部非默认路由 ``{cid: Route}``（观测 / 会话列表角标一次取全）。"""
    out: Dict[str, Route] = {}
    if store is None or not hasattr(store, "list_app_settings"):
        return out
    try:
        for row in store.list_app_settings(KEY_PREFIX) or []:
            k = str(row.get("key") or "")
            if not k.startswith(KEY_PREFIX):
                continue
            r = _parse(row.get("value"))
            if r and not r.is_default:
                out[k[len(KEY_PREFIX):]] = r
    except Exception:
        logger.debug("[conv_route] 列举失败", exc_info=True)
    return out


def is_unrestricted(store: Any, cid: str) -> bool:
    return get(store, cid).unrestricted


def bypass_safety_active(store: Any, cid: str, config: Any = None) -> bool:
    """安全刹车是否全关：会话钥匙 OR 全局钥匙（且会话确为无限制）。"""
    r = get(store, cid)
    if not r.unrestricted:
        return False
    return bool(r.bypass_safety or global_bypass_safety(config))


def resolve_for(store: Any, platform: str, account_id: str, chat_key: str) -> Route:
    return get(store, conv_id(platform, account_id, chat_key))


def default_store() -> Any:
    """进程内默认 inbox store（A 线 / B 线没有 request 时用）；拿不到 → None。"""
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def conv_id_from_context(context: Mapping[str, Any], user_id: Any = "") -> str:
    """A 线：``context`` / ``user_id`` → 三段式会话 id（协议线 user_id 本身即三段式）。"""
    try:
        cid = str((context or {}).get("conversation_id") or "").strip()
        if cid.count(":") >= 2:
            return cid
        uid = str(user_id or "").strip()
        if uid.count(":") >= 2:
            return uid
        plat = str((context or {}).get("platform") or "").strip().lower()
        acct = str((context or {}).get("account_id") or "default").strip() or "default"
        key = str((context or {}).get("chat_id") or uid or "").strip()
        return conv_id(plat, acct, key) if plat and key else ""
    except Exception:
        return ""


def attach(user_context: Dict[str, Any], cid: str, *, store: Any = None,
           config: Any = None) -> Route:
    """读会话路由并写进 ``user_context``；顺带把全局钥匙折进 ``_unrestricted_bypass_safety``。
    store 缺省取进程默认；任何异常 → 标准档（只清键）。

    R87 #330（2026-09-17）：无限制端点**刚刚**被判离线（:data:`OFFLINE_FALLBACK_TTL_SEC` 窗内）
    且 :func:`offline_fallback_enabled` → 本轮直接按**标准档**装配（规则全开、走主链），
    ``user_context["_route_fallback"]`` 带原因给状态带 / 日志；不再让每条入站都先撞一次
    离线端点才发现没人回。窗过后再试一次真端点；成功即 :func:`record_reply` 清窗。
    """
    try:
        user_context.pop("_route_fallback", None)
        st = store if store is not None else default_store()
        r = get(st, cid) if cid else Route.standard()
        if r.unrestricted and not r.bypass_safety and global_bypass_safety(config):
            r = replace(r, bypass_safety=True)
        if r.unrestricted and offline_fallback_enabled(config):
            last = recent_offline(config)
            if last:
                Route.standard().apply_context(user_context)
                user_context["_route_fallback"] = {
                    "from": PROFILE_UNRESTRICTED, "reason": str(last.get("reason") or "offline"),
                    "since": float(last.get("ts") or 0.0), "mode": "preemptive",
                }
                logger.info("[conv_route] fallback=standard mode=preemptive conv=%s reason=%s "
                            "offline_age=%.0fs", cid or "-", last.get("reason") or "offline",
                            max(0.0, time.time() - float(last.get("ts") or 0.0)))
                return Route.standard()
        r.apply_context(user_context)
        return r
    except Exception:
        try:
            Route.standard().apply_context(user_context)
        except Exception:
            pass
        return Route.standard()


# ─────────────────────────────────────────────────────────────────────────────
# 离线回退（R87 #330）：端点级「最近离线」窗 + 会话级 note（状态带 / 诊断读）
# ─────────────────────────────────────────────────────────────────────────────

#: 端点被判离线后多久内所有无限制会话直接按标准档装配（不再逐条撞端点）
OFFLINE_FALLBACK_TTL_SEC = 300.0
OFFLINE_NOTE_PREFIX = "route_offline:"
#: 会话 note 超过这个时长不再显示（端点通常小时级恢复；陈旧不删只是不显）
OFFLINE_NOTE_TTL_SEC = 24 * 3600

_OFFLINE_LOCK = threading.Lock()
_LAST_OFFLINE: Dict[str, Dict[str, Any]] = {}


def offline_fallback_enabled(config: Any = None) -> bool:
    """``ai.unrestricted.offline_fallback``（默认 True）：本机私有端点离线时是否按标准档
    （规则全开、走实例主链）代答并在会话头亮黄条。False＝旧行为：不回落、本轮不回复。"""
    try:
        return bool(_section(config).get("offline_fallback", True))
    except Exception:
        return True


def _offline_key(config: Any = None) -> str:
    spec = endpoint_spec(config)
    if not spec:
        return "no_endpoint"
    return str(spec.get("base_url") or "") + "|" + str(spec.get("model") or "")


def recent_offline(config: Any = None, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """无限制端点在 :data:`OFFLINE_FALLBACK_TTL_SEC` 内被判过离线 → ``{ts, reason}``；否则 None。"""
    try:
        with _OFFLINE_LOCK:
            rec = _LAST_OFFLINE.get(_offline_key(config))
        if not rec:
            return None
        ts = float(rec.get("ts") or 0.0)
        if (float(now) if now is not None else time.time()) - ts > OFFLINE_FALLBACK_TTL_SEC:
            return None
        return dict(rec)
    except Exception:
        return None


def clear_offline(config: Any = None) -> None:
    """端点又出话了（无限制真答成功）→ 关窗，下一轮回到真端点。
    ``config=None``（record_reply 回调没有配置句柄）→ 清全部：无限制端点全实例只有一个。"""
    try:
        with _OFFLINE_LOCK:
            if config is None:
                _LAST_OFFLINE.clear()
            else:
                _LAST_OFFLINE.pop(_offline_key(config), None)
    except Exception:
        pass


def fallback_active(config: Any = None) -> bool:
    """守卫链的无 ``user_context`` 读点用：回退开着且端点仍在离线窗 → 本会话按标准档判（规则全开）。"""
    return offline_fallback_enabled(config) and recent_offline(config) is not None


def _note_key(cid: str) -> str:
    return OFFLINE_NOTE_PREFIX + str(cid or "").strip()


def mark_offline_note(cid: str, *, reason: str = "", fallback: bool = False,
                      store: Any = None, ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """会话级 note ``route_offline:<cid>={ts, first_ts, n, reason, fallback}``（app_settings KV，
    与 xlate_hold_marker 同款）。``fallback=True``＝这轮已按标准档代答。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid:
        return None
    st = store if store is not None else default_store()
    if st is None or not hasattr(st, "set_app_setting"):
        return None
    now = float(ts or time.time())
    prev = offline_note(cid, store=st, now=now, ttl_sec=6 * 3600) or {}
    rec = {"ts": now, "first_ts": float(prev.get("first_ts") or now),
           "n": int(prev.get("n") or 0) + 1, "reason": str(reason or "offline")[:60],
           "fallback": bool(fallback)}
    try:
        st.set_app_setting(_note_key(cid), json.dumps(rec, ensure_ascii=False),
                           updated_by="conv_route")
        return rec
    except Exception:
        logger.debug("[conv_route] offline note 写入失败（忽略）", exc_info=True)
        return None


def clear_offline_note(cid: str, *, store: Any = None) -> bool:
    cid = str(cid or "").strip()
    st = store if store is not None else default_store()
    if not cid or st is None or not hasattr(st, "set_app_setting") or not hasattr(st, "get_app_setting"):
        return False
    try:
        if not str(st.get_app_setting(_note_key(cid), "") or ""):
            return False
        st.set_app_setting(_note_key(cid), "")
        return True
    except Exception:
        return False


def offline_note(cid: str, *, store: Any = None, now: Optional[float] = None,
                 ttl_sec: float = OFFLINE_NOTE_TTL_SEC) -> Optional[Dict[str, Any]]:
    """读会话 note；无 / 脏 / 超 ttl → None。"""
    cid = str(cid or "").strip()
    st = store if store is not None else default_store()
    if not cid or st is None or not hasattr(st, "get_app_setting"):
        return None
    try:
        rec = json.loads(str(st.get_app_setting(_note_key(cid), "") or "") or "{}")
    except Exception:
        return None
    if not isinstance(rec, dict) or not rec.get("ts"):
        return None
    try:
        age = (float(now) if now is not None else time.time()) - float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ttl_sec and age > float(ttl_sec):
        return None
    rec["age_sec"] = round(max(0.0, age), 0)
    return rec


def begin_fallback(user_context: Dict[str, Any], cid: str, *, reason: str = "",
                   config: Any = None, store: Any = None) -> Optional[Dict[str, Any]]:
    """无限制端点这一轮真撞离线了（ai_client 置 ``_route_offline``）→ 把 ``user_context`` 改成
    标准档装配并返回回退信息；回退关着 → None（调用方按旧行为不回复）。绝不抛。"""
    try:
        if not offline_fallback_enabled(config):
            return None
        Route.standard().apply_context(user_context)
        user_context.pop("_route_offline", None)
        info = {"from": PROFILE_UNRESTRICTED, "reason": str(reason or "offline"),
                "since": time.time(), "mode": "reactive"}
        user_context["_route_fallback"] = info
        mark_offline_note(cid, reason=reason, fallback=True, store=store)
        logger.info("[conv_route] fallback=standard mode=reactive conv=%s reason=%s",
                    cid or "-", reason or "offline")
        return info
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 守卫链读点 + 观测
# ─────────────────────────────────────────────────────────────────────────────

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {"skipped_total": 0, "by_layer": {}, "offline_holds": 0,
                          "safety_bypassed": 0, "by_model": {}}


def record_reply(route: Optional[Mapping[str, Any]], *, ok: bool = True,
                 latency_ms: int = 0) -> None:
    """一次主链调用按「模型档」分桶计数（prompt_trace.record 回调；进程内、绝不抛）。

    桶键：无限制 → ``unrestricted``；点名厂商档 → 档名；否则 ``main``（主链）。
    stats 面板据此回答「今天哪几个会话在花哪家的钱 / 走了几条本机」。"""
    try:
        r = route or {}
        if r.get("unrestricted") or r.get("profile") == PROFILE_UNRESTRICTED:
            key = PROFILE_UNRESTRICTED
            if ok:
                clear_offline()   # R87 #330：真端点又出话了 → 关离线窗
        else:
            key = str(r.get("model") or "").strip() or "main"
        with _LOCK:
            bm = _STATS["by_model"]
            b = bm.get(key) or {"calls": 0, "ok": 0, "fail": 0, "latency_ms_total": 0}
            b["calls"] += 1
            b["ok" if ok else "fail"] += 1
            b["latency_ms_total"] += max(0, int(latency_ms or 0))
            bm[key] = b
    except Exception:
        pass


def _bump(layer: str, safety: bool) -> None:
    with _LOCK:
        _STATS["skipped_total"] = int(_STATS["skipped_total"]) + 1
        bl = _STATS["by_layer"]
        bl[layer] = int(bl.get(layer, 0)) + 1
        if safety:
            _STATS["safety_bypassed"] = int(_STATS["safety_bypassed"]) + 1


def skip_guard(user_context: Optional[Mapping[str, Any]], layer: str) -> bool:
    """本会话是否跳过某一层。质量层：``_unrestricted`` 即跳；安全层：还要 ``_unrestricted_bypass_safety``；
    未登记的层名恒 False（新守卫忘了登记＝照常拦，安全默认）。绝不抛。"""
    try:
        ctx = user_context or {}
        if not ctx.get("_unrestricted"):
            return False
        lyr = str(layer or "")
        if lyr in QUALITY_LAYERS:
            _bump(lyr, False)
            return True
        if lyr in SAFETY_LAYERS and ctx.get("_unrestricted_bypass_safety"):
            _bump(lyr, True)
            return True
        return False
    except Exception:
        return False


def skip_for_conv(store: Any, cid: str, layer: str, config: Any = None) -> bool:
    """无 ``user_context`` 的消费点（drafts / worker / send routes）用：按会话 id 判。"""
    try:
        r = get(store, cid)
        if not r.unrestricted:
            return False
        if fallback_active(config):
            # R87 #330：端点离线窗内本会话按标准档代答——守卫链同样全开，不许「规则让路
            # 但答的是云端」这种半吊子状态
            return False
        ctx = {"_unrestricted": True}
        if r.bypass_safety or global_bypass_safety(config):
            ctx["_unrestricted_bypass_safety"] = True
        return skip_guard(ctx, layer)
    except Exception:
        return False


def record_offline_hold() -> None:
    with _LOCK:
        _STATS["offline_holds"] = int(_STATS["offline_holds"]) + 1


def stats_snapshot(store: Any = None) -> Dict[str, Any]:
    with _LOCK:
        snap = {
            "skipped_total": int(_STATS["skipped_total"]),
            "safety_bypassed": int(_STATS["safety_bypassed"]),
            "offline_holds": int(_STATS["offline_holds"]),
            "by_layer": dict(_STATS["by_layer"]),
            "by_model": {k: dict(v) for k, v in _STATS["by_model"].items()},
        }
    for b in snap["by_model"].values():
        b["avg_latency_ms"] = int(b["latency_ms_total"] / b["calls"]) if b.get("calls") else 0
    routes = all_routes(store) if store is not None else {}
    snap["unrestricted_convs"] = sum(1 for r in routes.values() if r.unrestricted)
    snap["bypass_safety_convs"] = sum(1 for r in routes.values() if r.bypass_safety)
    # 每个厂商档当前被几个会话点名（谁在花哪家的钱）
    vm: Dict[str, int] = {}
    for r in routes.values():
        if r.model and not r.unrestricted:
            vm[r.model] = vm.get(r.model, 0) + 1
    snap["model_convs"] = vm
    snap["global_bypass_safety"] = global_bypass_safety()
    return snap


def reset_for_tests() -> None:
    with _LOCK:
        _STATS["skipped_total"] = 0
        _STATS["safety_bypassed"] = 0
        _STATS["offline_holds"] = 0
        _STATS["by_layer"].clear()
        _STATS["by_model"].clear()
    with _HEALTH_LOCK:
        _HEALTH.clear()
    with _OFFLINE_LOCK:
        _LAST_OFFLINE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 端点探活（1-token chat ping；GET /v1/models 在 173 网关会 RST）
# ─────────────────────────────────────────────────────────────────────────────

_HEALTH_LOCK = threading.Lock()
_HEALTH: Dict[str, Dict[str, Any]] = {}
HEALTH_TTL_SEC = 60.0


async def probe_endpoint(config: Any = None, *, force: bool = False,
                         timeout: float = 6.0, profile: Optional[str] = None) -> Dict[str, Any]:
    """端点在线态 ``{configured, online, model, host, source, latency_ms, checked_at, error}``。

    ``profile=None`` → 无限制端点（历史语义）；``""`` → 实例主链；档名 → 该模型档。
    60s TTL 缓存（按 base|model 分桶）；``force`` 跳缓存。绝不抛。
    """
    spec = endpoint_spec(config) if profile is None else model_spec(config, profile)
    if not spec:
        return {"configured": False, "online": False, "error": "no_endpoint",
                "profile": profile if profile is not None else profile_name(config)}
    ck = spec["base_url"] + "|" + spec["model"]
    now = time.time()
    with _HEALTH_LOCK:
        cached = _HEALTH.get(ck)
    if cached and not force and now - float(cached.get("checked_at") or 0) < HEALTH_TTL_SEC:
        return dict(cached)
    out: Dict[str, Any] = {
        "configured": True, "online": False, "model": spec["model"], "host": spec["host"],
        "source": spec["source"], "max_ctx": spec["max_ctx"], "latency_ms": 0,
        "checked_at": now, "error": "",
        "profile": profile if profile is not None else profile_name(config),
    }
    api_key = _api_key_for(config, spec)
    headers = {"Authorization": f"Bearer {api_key or 'vllm'}"}
    try:
        import httpx
        private = True
        try:
            from src.ai.vendor_params import vendor_of as _vof
            private = bool(_vof(spec["base_url"]).get("private"))
        except Exception:
            private = True
        async with httpx.AsyncClient(timeout=timeout) as cli:
            listed: Optional[bool] = None
            sc = -1
            if not private:
                # 云厂商：只读 GET /models 探活（零 token、零账单；401/403 同样在这里暴露）。
                # 404/405＝该厂商不支持列表 → 回落 1-token ping。
                out["probe"] = "models"
                t0 = time.monotonic()
                resp = await cli.get(spec["base_url"] + "/models", headers=headers)
                out["latency_ms"] = int((time.monotonic() - t0) * 1000)
                sc = int(resp.status_code)
                if sc == 200:
                    listed = _model_listed(resp, spec["model"])
                elif sc in (404, 405):
                    sc = -1  # 回落 ping
            if sc == -1:
                # 本机私有（173 vLLM）/ 不支持列表的厂商：1-token ping，顺带量真实首字延迟
                out["probe"] = "chat"
                body: Dict[str, Any] = {
                    "model": spec["model"], "max_tokens": 1, "temperature": 0,
                    "messages": [{"role": "user", "content": "ping"}],
                }
                # 关思维链字段按厂商给（云厂商对未知顶层字段会 400，不能一律送 chat_template_kwargs）
                try:
                    from src.ai.vendor_params import thinking_off_extra_body as _toff
                    body.update(_toff(spec["base_url"], spec["model"]) or {})
                except Exception:
                    body["chat_template_kwargs"] = {"enable_thinking": False}
                t0 = time.monotonic()
                resp = await cli.post(spec["base_url"] + "/chat/completions", json=body,
                                      headers=headers)
                out["latency_ms"] = int((time.monotonic() - t0) * 1000)
                sc = int(resp.status_code)
        if sc < 500 and sc not in (401, 403, 404, 429):
            out["online"] = True
            if listed is False:
                # 列表里没这个模型名：能连、密钥对，但 chat 时大概率 404 —— 在线但给告警
                out["warn"] = "model_not_listed"
        else:
            # 401/403＝密钥不对、404＝模型名不存在、429＝额度/限流：能连上但不能用，如实报离线
            out["error"] = f"http_{sc}"
    except Exception as e:  # 连接拒绝 / 超时 / DNS
        out["error"] = type(e).__name__.lower()[:40]
    with _HEALTH_LOCK:
        _HEALTH[ck] = dict(out)
    return out


def _model_listed(resp: Any, model: str) -> Optional[bool]:
    """``GET /models`` 响应里有没有该模型名（``id`` 等于 / 以 ``/<model>`` 结尾，Gemini 兼容层
    是 ``models/gemini-…``）。列表为空 / 解析失败 → None（不据此判定）。"""
    try:
        data = resp.json()
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            return None
        ids = {str((r or {}).get("id") or "") for r in rows if isinstance(r, dict)}
        ids.discard("")
        if not ids:
            return None
        m = str(model or "")
        return any(i == m or i.endswith("/" + m) for i in ids)
    except Exception:
        return None


async def probe_catalog(config: Any = None, *, force: bool = False,
                        timeout: float = 6.0) -> Dict[str, Dict[str, Any]]:
    """目录全档并发探活 ``{name: health}``（面板打开一次请求画全所有在线点）。绝不抛。"""
    import asyncio
    names = [str(r.get("name") or "") for r in model_catalog(config)]
    if not names:
        return {}
    results = await asyncio.gather(
        *(probe_endpoint(config, force=force, timeout=timeout, profile=n) for n in names),
        return_exceptions=True)
    out: Dict[str, Dict[str, Any]] = {}
    for n, h in zip(names, results):
        out[n] = h if isinstance(h, dict) else {"configured": True, "online": False,
                                                "error": "probe_failed", "profile": n}
    return out


def _api_key_for(config: Any, spec: Mapping[str, Any]) -> str:
    try:
        ai = _root(config).get("ai") or {}
        src = str(spec.get("source") or "")
        if src.startswith("ai.models."):
            name = src.split(".", 2)[-1]
            return (str(((ai.get("models") or {}).get(name) or {}).get("api_key") or "")
                    or str(ai.get("api_key") or ""))
        if src == "ai":
            return str(ai.get("api_key") or "")
        return str(((ai.get("fallback") or {}).get("api_key")) or "")
    except Exception:
        return ""


def note_offline(cid: str, reason: str = "", config: Any = None) -> None:
    """ai_client 严格路由失败时打点（观测 + 端点级离线窗 + 会话 note）。

    R87 #330：除计数外还开 :data:`OFFLINE_FALLBACK_TTL_SEC` 窗——窗内 :func:`attach` 对所有
    无限制会话直接按标准档装配；会话 note 让状态带能说「本机模型离线 · {hhmm}」。
    """
    record_offline_hold()
    try:
        with _OFFLINE_LOCK:
            _LAST_OFFLINE[_offline_key(config)] = {"ts": time.time(),
                                                   "reason": str(reason or "offline")[:60]}
    except Exception:
        pass
    mark_offline_note(cid, reason=reason, fallback=False)
    logger.warning("[conv_route] unrestricted endpoint offline → 本轮不回落云端 conv=%s reason=%s "
                   "fallback=%d", cid or "-", reason or "-", int(offline_fallback_enabled(config)))


# ─────────────────────────────────────────────────────────────────────────────
# 上下文深度 per-call 作用域（配合 context_depth.override_scope）
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def depth_scope(route: Route, config: Any = None) -> Iterator[None]:
    """把路由深度档套进 ``context_depth`` 的 contextvar；空档 / 标准路由＝no-op。"""
    tier = effective_depth(route, config)
    if not tier:
        yield
        return
    try:
        from src.ai.context_depth import override_scope
    except Exception:
        yield
        return
    with override_scope(tier, skip_economy=route.unrestricted):
        yield


# 生成作用域：prompt 装配口（persona_manager 规则块 / spoken_style / 媒体能力提示…）没有
# user_context 可读，用 contextvar 问「本次生成是不是无限制会话」。async 同任务内穿透 await。
_ACTIVE: contextvars.ContextVar[Optional[Route]] = contextvars.ContextVar(
    "conv_route_active", default=None)


def active_route() -> Optional[Route]:
    return _ACTIVE.get()


def active_unrestricted() -> bool:
    r = _ACTIVE.get()
    return bool(r and r.unrestricted)


@contextmanager
def generation_scope(route: Optional[Route], config: Any = None) -> Iterator[None]:
    """一次拟稿 / 应答生成的作用域：登记活动路由 + 套深度档。标准默认路由＝只套 no-op。"""
    r = route or Route.standard()
    token = _ACTIVE.set(r if not r.is_default else None)
    try:
        with depth_scope(r, config):
            yield
    finally:
        _ACTIVE.reset(token)


def describe(store: Any, cid: str, config: Any = None) -> Dict[str, Any]:
    """给 API / UI 的完整视图：路由 + 端点 + 可选项 + 授权。"""
    r = get(store, cid)
    spec = endpoint_spec(config)
    catalog = model_catalog(config)
    names = {str(row.get("name") or "") for row in catalog}
    return {
        "route": r.as_dict(),
        "effective_depth": effective_depth(r, config),
        # endpoint＝无限制端点事实（历史键，模式面板用）；active_endpoint＝本会话实际用谁答
        "endpoint": {k: v for k, v in spec.items() if k != "api_key"},
        "active_endpoint": {k: v for k, v in active_endpoint(r, config).items() if k != "api_key"},
        # 选了的模型档已从 ai.models 删掉 → 运行时静默回主链；UI 要提示而不是装没事
        "model_missing": bool(r.model and r.model not in names),
        "choices": {
            "profiles": list(PROFILES),
            "models": catalog,
            "depths": allowed_depths(config, unrestricted=r.unrestricted, model=r.model),
            "efforts": list(EFFORT_CHOICES),
            "effort_params": {k: dict(v) for k, v in EFFORT_PARAMS.items()},
        },
        "enabled": enabled(config),
        "allowed": feature_allowed(config),
        "vendor_allowed": vendor_allowed(config),
        "global_bypass_safety": global_bypass_safety(config),
    }


__all__ = [
    "KEY_PREFIX", "PROFILES", "PROFILE_STANDARD", "PROFILE_UNRESTRICTED",
    "CHATX_DISPLAY_LABEL", "PUBLIC_HOST_LAN_CHATX",
    "DEPTH_CHOICES", "EFFORT_CHOICES", "EFFORT_PARAMS", "UNRESTRICTED_DEFAULTS", "MODEL_FOLLOW",
    "model_opens_unrestricted",
    "FEATURE_NAME", "QUALITY_LAYERS", "SAFETY_LAYERS",
    "Route", "normalize", "enabled", "profile_name", "global_bypass_safety",
    "endpoint_spec", "main_chain_spec", "model_catalog", "model_spec", "active_endpoint",
    "probe_catalog",
    "endpoint_prompt_cap", "depth_cap", "allowed_depths", "effective_depth", "feature_allowed",
    "VENDOR_FEATURE_NAME", "vendor_allowed", "record_reply",
    "conv_id", "get", "set", "clear", "all_routes", "is_unrestricted",
    "bypass_safety_active", "resolve_for", "default_store", "conv_id_from_context", "attach",
    "skip_guard", "skip_for_conv", "record_offline_hold", "stats_snapshot", "reset_for_tests",
    "probe_endpoint", "note_offline", "depth_scope", "describe",
    "OFFLINE_FALLBACK_TTL_SEC", "OFFLINE_NOTE_PREFIX", "offline_fallback_enabled",
    "recent_offline", "clear_offline", "fallback_active", "mark_offline_note",
    "clear_offline_note", "offline_note", "begin_fallback",
    "active_route", "active_unrestricted", "generation_scope",
]
