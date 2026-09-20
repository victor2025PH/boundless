"""autoreply_factors — 「影响全自动回复的因素」注册表 + 聚合判定（实施56，2026-08-22）。

背景（老板拍板「集中一处、一键开关，别再让小问题掐断全自动」）：外网内测机一周内
踩过 短消息门槛 / 识图关闭 / 转写缺失 / 额度触顶 四类「全自动看起来坏了」，每一类的
开关与修复入口散在不同页面（首启向导 / 能力看板 / 设置页高级折叠 / 收件箱横幅），
用户找不到、客服要陪查。本模块把「哪些因素会让全自动不全」收成一张**注册表**：

- **判定纯函数化**：所有运行态输入（会话档位 / 额度台账行 / 托管额度 / 会话健康 /
  近窗拦截）由路由层注入，本模块零 IO、零网络——可直接单测；config 判定复用各域
  权威单点（standby_mode / media_capability / peer_bot_guard），绝不另算一套语义。
- **每个因素自带 fix 描述符**（kind=preset|settings|feature|anchor|link），前端据此
  渲染「一键修复」——全部复用既有写入口（媒体预设 / reply-settings 白名单 POST /
  功能总览 toggle / 守卫卡锚点 / 会员页），本模块与配套路由**零新增写端点**。
- **防再散门禁**：`delivery_block._KNOWN_DOMAINS` 的每个域必须在
  :data:`DOMAIN_FACTOR` 有归属（tests/test_autoreply_factors.py 钉住）——以后新增
  拦截域时，注册表红灯先响，逼着同步接进总控台，而不是又长出一个孤儿散点。

state 语义（前端配色按此映射，色点必配文字防色盲不可辨）：
    ok   绿：不拖累全自动；
    warn 黄：能跑但有折损/边界（短消息不回、接近限额、余量偏低、近窗有拦截）；
    bad  红：正在或将要掐断某类消息的全自动（能力关着/后端未接/触顶/额度用尽/掉线）；
    na   灰：与本部署无关（非托管无额度概念 / 无外部会话监测），UI 直接隐藏该行。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "FACTOR_KEYS", "DOMAIN_FACTOR", "collect_autoreply_health",
]

#: 注册表顺序即 UI 展示顺序（chat 排第一供 API 消费方用，设置页卡内隐藏——
#: 主控三档单选就在因素清单正上方，重复渲染=两处真相）。
FACTOR_KEYS = (
    "chat",        # 发送链姿态（standby 三档 + 近窗聊天生成拦截）
    "vision",      # 图片消息能否看懂（vision.enabled × 后端就绪 × 近窗拦截）
    "asr",         # 语音消息能否听懂（voice_recognition 同构）
    "voice_out",   # 语音回复出站（仅近窗有拦截才现身）
    "short_msg",   # 短消息门槛（min_text_len>0 = 「好/嗯」不回）
    "translate",   # 出站自动翻译（外语客户）
    "budget",      # 每日回复额度守卫（触顶=对该会话停发）
    "quota",       # 托管算力额度（试用/套餐字符数）
    "session",     # 外部平台会话在线（messenger-web / whatsapp Node worker）
)

#: delivery_block 拦截域 → 因素归属（防再散门禁的核心映射）。
DOMAIN_FACTOR = {
    "chat": "chat",
    "vision": "vision",
    "asr": "asr",
    "voice": "voice_out",
    "translate": "translate",
}

#: 会话健康表里「不算故障」的状态（与坐席离线横幅同口径：logged_out 是运营
#: 主动登出/种子态，不是掉线）。
_SESSION_IGNORED_STATUSES = ("logged_out",)


def _dig(cfg: Any, dotted: str, default: Any = None) -> Any:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def _blocks_count(blocks: Optional[Dict[str, Any]], domain: str) -> int:
    """近窗（seat_banner 已按 30min 窗过滤）该域拦截数；无数据=0。"""
    try:
        return int(((blocks or {}).get("by_domain") or {}).get(domain) or 0)
    except (TypeError, ValueError):
        return 0


def _f(key: str, state: str, code: str, *, fix: Optional[Dict[str, Any]] = None,
       **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"key": key, "state": state, "code": code}
    if fix:
        out["fix"] = fix
    out.update(extra)
    return out


_FIX_MEDIA = {"kind": "preset", "name": "understand_all"}
_FIX_MINLEN = {"kind": "settings",
               "changes": {"inbox.auto_draft.min_text_len": 0}}
_FIX_XLATE = {"kind": "feature",
              "key": "inbox.l2_autosend.translate.enabled"}
_FIX_BUDGET = {"kind": "anchor", "href": "#rps-sec-guard"}
_FIX_QUOTA = {"kind": "link", "href": "/membership"}


def _chat_factor(config: dict, blocks: Optional[dict]) -> Dict[str, Any]:
    """发送链姿态：权威判定复用 standby_mode（勿在此重推 worker/deliver 组合）。"""
    try:
        from src.companion.standby_mode import infer_standby_mode
        mode = infer_standby_mode(config)
    except Exception:
        mode = "custom"
    n = _blocks_count(blocks, "chat")
    if mode == "watching":
        if n:
            return _f("chat", "warn", "blocks", n=n)
        return _f("chat", "ok", "watching")
    if mode == "off":
        return _f("chat", "bad", "off")
    # suggest（刻意的人审档）与 custom（自相矛盾配置）都黄灯，code 区分
    return _f("chat", "warn", mode, n=n or None)


def _media_factor(key: str, enabled: Any, ready: bool,
                  blocks_n: int) -> Dict[str, Any]:
    if not bool(enabled):
        return _f(key, "bad", "disabled", fix=_FIX_MEDIA, n=blocks_n or None)
    if not ready:
        return _f(key, "bad", "needs_backend", fix=_FIX_MEDIA,
                  n=blocks_n or None)
    if blocks_n:
        return _f(key, "warn", "blocks", n=blocks_n)
    return _f(key, "ok", "ok")


def _vision_factor(config: dict, blocks: Optional[dict]) -> Dict[str, Any]:
    try:
        from src.companion.media_capability import vision_backend_ready
        ready = bool(vision_backend_ready(config))
    except Exception:
        ready = False
    return _media_factor("vision", _dig(config, "vision.enabled", False),
                         ready, _blocks_count(blocks, "vision"))


def _asr_factor(config: dict, blocks: Optional[dict]) -> Dict[str, Any]:
    try:
        from src.companion.media_capability import asr_backend_ready
        ready = bool(asr_backend_ready(config))
    except Exception:
        ready = False
    return _media_factor("asr", _dig(config, "voice_recognition.enabled", False),
                         ready, _blocks_count(blocks, "asr"))


def _voice_out_factor(blocks: Optional[dict]) -> Dict[str, Any]:
    """语音回复出站：常态不占版面（na），近窗有 TTS 拦截才现身提示。"""
    n = _blocks_count(blocks, "voice")
    if n:
        return _f("voice_out", "warn", "blocks", n=n)
    return _f("voice_out", "na", "quiet")


def _short_msg_factor(config: dict) -> Dict[str, Any]:
    try:
        v = int(_dig(config, "inbox.auto_draft.min_text_len", 0) or 0)
    except (TypeError, ValueError):
        v = 0
    if v > 0:
        return _f("short_msg", "warn", "min_len", n=v, fix=_FIX_MINLEN)
    return _f("short_msg", "ok", "any_len")


def _translate_factor(config: dict, blocks: Optional[dict]) -> Dict[str, Any]:
    n = _blocks_count(blocks, "translate")
    if not bool(_dig(config, "inbox.l2_autosend.translate.enabled", False)):
        return _f("translate", "warn", "off", fix=_FIX_XLATE)
    if n:
        return _f("translate", "warn", "blocks", n=n)
    return _f("translate", "ok", "on")


def _budget_factor(config: dict,
                   budget_rows: Optional[List[dict]]) -> Dict[str, Any]:
    """额度守卫：语义单点复用 peer_bot_guard.budget_flags（横幅/设置页同源）。"""
    try:
        from src.inbox.peer_bot_guard import budget_flags, parse_cfg
        guard = parse_cfg(config)
    except Exception:
        return _f("budget", "na", "unavailable")
    if not (bool(guard.get("enabled"))
            and int(guard.get("daily_reply_budget") or 0) > 0):
        return _f("budget", "ok", "unlimited")
    limit = int(guard.get("daily_reply_budget") or 0)
    if budget_rows is None:
        # store 缺席（旧后端/未启持久化）：只报「守卫开着、限额 N」，不猜触顶数
        return _f("budget", "ok", "on", limit=limit, fix=_FIX_BUDGET)
    exhausted = near = 0
    for r in budget_rows:
        try:
            flags = budget_flags(r.get("used"), r.get("relieved"), guard)
        except Exception:
            continue
        if flags.get("exhausted"):
            exhausted += 1
        elif flags.get("near"):
            near += 1
    if exhausted:
        return _f("budget", "bad", "exhausted", n=exhausted, limit=limit,
                  fix=_FIX_BUDGET)
    if near:
        return _f("budget", "warn", "near", n=near, limit=limit,
                  fix=_FIX_BUDGET)
    return _f("budget", "ok", "headroom", limit=limit)


def _quota_factor(quota: Optional[dict]) -> Dict[str, Any]:
    q = quota or {}
    if not q.get("enabled"):
        return _f("quota", "na", "self_hosted")
    if q.get("error"):
        return _f("quota", "warn", "unknown")
    if q.get("exhausted"):
        return _f("quota", "bad", "out", fix=_FIX_QUOTA)
    budget = int(q.get("budget") or 0)
    if budget > 0:
        pct = max(0, min(100, round(100 * int(q.get("remaining") or 0) / budget)))
        if pct < 10:
            return _f("quota", "warn", "low", pct=pct, fix=_FIX_QUOTA)
        return _f("quota", "ok", "ok", pct=pct)
    return _f("quota", "ok", "ok")


def _session_factor(sessions: Optional[dict]) -> Dict[str, Any]:
    """外部 worker 会话（messenger-web/whatsapp Node push）。

    登记表整体缺席（纯 Telegram 协议部署天然如此）→ na 而非谎报「全部在线」
    ——Telegram 账号自身的在线态不经这张表，报绿是越权担保。
    """
    if not isinstance(sessions, dict) or not sessions.get("known"):
        return _f("session", "na", "untracked")
    down: List[str] = []
    for key, sess in (sessions.get("unhealthy") or {}).items():
        st = str((sess or {}).get("status") or "")
        if st in _SESSION_IGNORED_STATUSES:
            continue
        down.append(str(key))
    if down:
        plat = down[0].partition(":")[0] or "telegram"
        return _f("session", "bad", "down", n=len(down),
                  fix={"kind": "link", "href": f"/workspace/channels/{plat}"})
    return _f("session", "ok", "ok")


def collect_autoreply_health(
    config: Any,
    *,
    budget_rows: Optional[List[dict]] = None,
    quota: Optional[dict] = None,
    sessions: Optional[dict] = None,
    blocks: Optional[dict] = None,
) -> Dict[str, Any]:
    """聚合「影响全自动回复的因素」清单（纯函数，输入全注入）。

    - ``budget_rows``：``store.list_reply_budget_today`` 原始行（None=store 缺席）；
    - ``quota``：``hosted_gateway.quota_probe`` 结果（None=未取）；
    - ``sessions``：``{"known": bool, "unhealthy": {key: sess}}``（None=未取）；
    - ``blocks``：``delivery_block.seat_banner()`` 快照（None=未取）。
    """
    cfg = config if isinstance(config, dict) else {}
    factors = [
        _chat_factor(cfg, blocks),
        _vision_factor(cfg, blocks),
        _asr_factor(cfg, blocks),
        _voice_out_factor(blocks),
        _short_msg_factor(cfg),
        _translate_factor(cfg, blocks),
        _budget_factor(cfg, budget_rows),
        _quota_factor(quota),
        _session_factor(sessions),
    ]
    by_state: Dict[str, int] = {}
    for f in factors:
        by_state[f["state"]] = by_state.get(f["state"], 0) + 1
    chat = factors[0]
    return {
        "factors": factors,
        "summary": {
            "by_state": by_state,
            # 总裁决：发送链 off=paused；任一 bad=attn；任一 warn=degraded；全 ok=full
            "verdict": (
                "paused" if chat["code"] == "off"
                else "attn" if by_state.get("bad")
                else "degraded" if by_state.get("warn")
                else "full"
            ),
        },
        "blocks": {
            "active": bool((blocks or {}).get("active")),
            "by_domain": dict((blocks or {}).get("by_domain") or {}),
        },
    }
