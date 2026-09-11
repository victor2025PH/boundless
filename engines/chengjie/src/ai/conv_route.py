# -*- coding: utf-8 -*-
"""会话级模型路由 + 「无限制」模式（2026-09-12，composer 模型选择器后端单一事实源）。

背景
====
坐席在 composer 底栏（键盘图标左侧）像 Cursor 一样按**会话**挑模型：
``标准``（实例主链：云端 + key_pool + LAN 兜底，规则全开）或 ``无限制``（局域网私有
模型——现网 173:8001 ``chatx``＝Qwen3.6-27B-abliterated，去审查权重——**只走该端点、
不回落云端**，且本会话的人设规则 / 出站改写守卫 / 风控分级 / 手发 409 护栏全部让路）。
再带三个 Cursor 式旋钮：上下文深度档（复用 :mod:`src.ai.context_depth` 四档，按端点
硬上限封顶）、力度（低/中/高 —— **答复长度 / 温度的近似**，不是推理力度，UI 明示）、
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
#: ``ultra``（900k）对 LAN 64k 端点无意义，UI 不出，但键合法（云端标准档可用）。
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

#: 端点硬上限（token）→ 允许的最高深度档。chatx ``--max-model-len 65536``。
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

@dataclass(frozen=True)
class Route:
    profile: str = PROFILE_STANDARD
    depth: str = DEPTH_FOLLOW
    effort: str = EFFORT_FOLLOW
    thinking: bool = False
    bypass_safety: bool = False
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
                and not self.thinking and not self.bypass_safety)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile, "depth": self.depth, "effort": self.effort,
            "thinking": bool(self.thinking), "bypass_safety": bool(self.bypass_safety),
            "unrestricted": self.unrestricted,
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


def normalize(payload: Mapping[str, Any], *, base: Optional[Route] = None) -> Route:
    """宽松归一：非法值回落到 base（或默认）。``profile`` 切到无限制且未显式给旋钮
    → 灌 :data:`UNRESTRICTED_DEFAULTS`；切回标准 → 旋钮清空（跟随全局）。"""
    r = base or Route.standard()
    prof = str(payload.get("profile", r.profile) or PROFILE_STANDARD).strip().lower()
    if prof not in PROFILES:
        prof = r.profile
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
    if not r.unrestricted:
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
        max_ctx = int(spec.get("max_ctx") or _section(root).get("max_ctx") or 65_536)
    except (TypeError, ValueError):
        max_ctx = 65_536
    return {
        "base_url": base, "model": model, "source": source, "host": host,
        "max_ctx": max_ctx,
        "supports_thinking": bool(spec.get("supports_thinking", True)),
    }


def depth_cap(max_ctx: int) -> str:
    """端点上下文硬上限 → 允许的最高深度档键。"""
    best = "standard"
    for k in _DEPTH_ORDER:
        if int(max_ctx or 0) >= _DEPTH_MIN_CTX[k]:
            best = k
    return best


def allowed_depths(config: Any = None, *, unrestricted: bool) -> list:
    """UI 可选深度档（含空串＝跟随全局）。无限制档按端点上限封顶。"""
    if not unrestricted:
        return list(DEPTH_CHOICES)
    spec = endpoint_spec(config)
    cap = depth_cap(int(spec.get("max_ctx") or 65_536)) if spec else "max"
    out = [DEPTH_FOLLOW]
    for k in _DEPTH_ORDER:
        out.append(k)
        if k == cap:
            break
    return out


def effective_depth(route: Route, config: Any = None) -> str:
    """路由深度档按端点封顶后的实际档；空串＝跟随全局。"""
    if not route.depth:
        return DEPTH_FOLLOW
    if not route.unrestricted:
        return route.depth
    allowed = allowed_depths(config, unrestricted=True)
    if route.depth in allowed:
        return route.depth
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
        now: Optional[float] = None) -> Optional[Route]:
    """按 patch 归一后落库；结果为默认路由 → 删键。返回生效路由；store 缺席 → None。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return None
    prev = get(store, cid)
    ts = float(now if now is not None else time.time())
    nxt = replace(normalize(patch, base=prev), updated_by=str(by or "agent"), updated_at=ts)
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
        logger.info("[conv_route] conv=%s profile=%s->%s depth=%s effort=%s thinking=%d "
                    "bypass_safety=%d->%d by=%s",
                    cid, prev.profile, nxt.profile, nxt.depth or "follow",
                    nxt.effort or "follow", int(nxt.thinking),
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
    store 缺省取进程默认；任何异常 → 标准档（只清键）。"""
    try:
        st = store if store is not None else default_store()
        r = get(st, cid) if cid else Route.standard()
        if r.unrestricted and not r.bypass_safety and global_bypass_safety(config):
            r = replace(r, bypass_safety=True)
        r.apply_context(user_context)
        return r
    except Exception:
        try:
            Route.standard().apply_context(user_context)
        except Exception:
            pass
        return Route.standard()


# ─────────────────────────────────────────────────────────────────────────────
# 守卫链读点 + 观测
# ─────────────────────────────────────────────────────────────────────────────

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {"skipped_total": 0, "by_layer": {}, "offline_holds": 0,
                          "safety_bypassed": 0}


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
        }
    routes = all_routes(store) if store is not None else {}
    snap["unrestricted_convs"] = sum(1 for r in routes.values() if r.unrestricted)
    snap["bypass_safety_convs"] = sum(1 for r in routes.values() if r.bypass_safety)
    snap["global_bypass_safety"] = global_bypass_safety()
    return snap


def reset_for_tests() -> None:
    with _LOCK:
        _STATS["skipped_total"] = 0
        _STATS["safety_bypassed"] = 0
        _STATS["offline_holds"] = 0
        _STATS["by_layer"].clear()
    with _HEALTH_LOCK:
        _HEALTH.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 端点探活（1-token chat ping；GET /v1/models 在 173 网关会 RST）
# ─────────────────────────────────────────────────────────────────────────────

_HEALTH_LOCK = threading.Lock()
_HEALTH: Dict[str, Dict[str, Any]] = {}
HEALTH_TTL_SEC = 60.0


async def probe_endpoint(config: Any = None, *, force: bool = False,
                         timeout: float = 6.0) -> Dict[str, Any]:
    """无限制端点在线态 ``{configured, online, model, host, source, latency_ms, checked_at, error}``。
    60s TTL 缓存；``force`` 跳缓存。绝不抛。"""
    spec = endpoint_spec(config)
    if not spec:
        return {"configured": False, "online": False, "error": "no_endpoint"}
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
    }
    api_key = _api_key_for(config, spec)
    try:
        import httpx
        body = {
            "model": spec["model"], "max_tokens": 1, "temperature": 0,
            "messages": [{"role": "user", "content": "ping"}],
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {"Authorization": f"Bearer {api_key or 'vllm'}"}
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.post(spec["base_url"] + "/chat/completions", json=body,
                                  headers=headers)
        out["latency_ms"] = int((time.monotonic() - t0) * 1000)
        if resp.status_code < 500:
            out["online"] = True
        else:
            out["error"] = f"http_{resp.status_code}"
    except Exception as e:  # 连接拒绝 / 超时 / DNS
        out["error"] = type(e).__name__.lower()[:40]
    with _HEALTH_LOCK:
        _HEALTH[ck] = dict(out)
    return out


def _api_key_for(config: Any, spec: Mapping[str, Any]) -> str:
    try:
        ai = _root(config).get("ai") or {}
        if str(spec.get("source") or "").startswith("ai.models."):
            name = str(spec["source"]).split(".", 2)[-1]
            return str(((ai.get("models") or {}).get(name) or {}).get("api_key") or "")
        return str(((ai.get("fallback") or {}).get("api_key")) or "")
    except Exception:
        return ""


def note_offline(cid: str) -> None:
    """ai_client 严格路由失败时打点（观测 + UI 上一次离线时刻）。"""
    record_offline_hold()
    logger.warning("[conv_route] unrestricted endpoint offline → 本轮不回落云端 conv=%s", cid or "-")


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
    return {
        "route": r.as_dict(),
        "effective_depth": effective_depth(r, config),
        "endpoint": {k: v for k, v in spec.items() if k != "api_key"},
        "choices": {
            "profiles": list(PROFILES),
            "depths": allowed_depths(config, unrestricted=r.unrestricted),
            "efforts": list(EFFORT_CHOICES),
            "effort_params": {k: dict(v) for k, v in EFFORT_PARAMS.items()},
        },
        "enabled": enabled(config),
        "allowed": feature_allowed(config),
        "global_bypass_safety": global_bypass_safety(config),
    }


__all__ = [
    "KEY_PREFIX", "PROFILES", "PROFILE_STANDARD", "PROFILE_UNRESTRICTED",
    "DEPTH_CHOICES", "EFFORT_CHOICES", "EFFORT_PARAMS", "UNRESTRICTED_DEFAULTS",
    "FEATURE_NAME", "QUALITY_LAYERS", "SAFETY_LAYERS",
    "Route", "normalize", "enabled", "profile_name", "global_bypass_safety",
    "endpoint_spec", "depth_cap", "allowed_depths", "effective_depth", "feature_allowed",
    "conv_id", "get", "set", "clear", "all_routes", "is_unrestricted",
    "bypass_safety_active", "resolve_for", "default_store", "conv_id_from_context", "attach",
    "skip_guard", "skip_for_conv", "record_offline_hold", "stats_snapshot", "reset_for_tests",
    "probe_endpoint", "note_offline", "depth_scope", "describe",
    "active_route", "active_unrestricted", "generation_scope",
]
