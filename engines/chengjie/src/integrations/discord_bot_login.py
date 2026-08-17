"""Discord Bot 接入：开关解析 + Token 校验 + 登录 provider。

与其他平台的一个**本质差异**，先说清楚免得误用：

Discord 没有二维码登录，也没有「用户号协议登录」这条合法路径（self-bot 明令违反
ToS 且封号）。本仓的 Discord 接入 = **官方 Bot 应用**：运营在 Discord 开发者后台建
一个 Bot、开 ``MESSAGE CONTENT INTENT``、把 Bot Token 填进来。所以这里的「登录」
其实是**凭据校验 + 落注册表**（打 ``GET /users/@me`` 确认 token 有效并取回 bot 身份），
一次往返即 authorized，没有扫码等待期。

Token 来源两条（后者优先，为将来多 Bot 的前端表单预留）：

1. 配置 ``platform_login.discord.bot_token``（当前 MVP 的主路径，无需改前端）；
2. 登录会话 ``ctx["bot_token"]``（前端表单填的，一号一 token）。

**安全**：token 是 Bot 的完整身份凭据（等价密码）。全模块只在 ``Authorization`` 头里
用它，日志一律走 ``mask_token``；落注册表时存在 ``meta.bot_token``（与 telegram
session_string / baileys login_id 同级别的账号私密态，随实例数据走）。

**能力硬边界**（不是 bug，是 Discord 平台规则，见 platform_capabilities.HARD_LIMITS）：
Bot **不能主动私聊陌生人**——只有对方与 Bot 有共同服务器、且其隐私设置允许服务器成员
私信时，Bot 才能开 DM；否则 API 返回 403 ``Cannot send messages to this user``。
这条限制在 worker 里被翻译成 ``delivered: False``（而非抛异常），并由主动触达闸门
在**规划阶段**就把 discord 排除，避免每个 tick 撞一轮 403。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch
from src.utils import external_api_lifecycle as _lifecycle

logger = logging.getLogger(__name__)

#: Discord REST 版本的单一事实源（别在别处拼 ``discord.com/api/vN``，有门禁扫）。
#: Discord 与 Meta/Shopify 不同：它**不公布**版本死期，v10 自 2022 年起一直是当前版，
#: 弃用是发公告而非按表到期。故登记表里它的 eol 是 None——「没有死期」是事实陈述，
#: 不是「忘了填」，这个区分由 ``unpublished``/``unknown`` 两档挡着。
API_VERSION = "v10"
API_BASE = f"https://discord.com/api/{API_VERSION}"


def pins():
    """向通用生命周期登记表申报 Discord 侧钉住的版本。"""
    return [
        _lifecycle.ApiPin(
            key="discord",
            label="Discord REST API",
            version=API_VERSION,
            eol=None,
            eol_unpublished=True,  # 官方确实不公布死期，不是我们查不到
            failure_mode=_lifecycle.FAIL_UNKNOWN,
            owner="src/integrations/discord_bot_login.py",
            doc_url="https://discord.com/developers/docs/reference#api-versioning",
            remediation="Discord 弃用版本走公告；升级前读 changelog 再改 API_VERSION。",
        )
    ]

# 入站消息在服务器频道里的处理档位（私聊永远收）：
#   mention = 只收 @ 到本 Bot 的（默认，防大服务器把收件箱冲垮）
#   all     = 频道里所有人说话都收（小客服服务器可用，慎开）
#   off     = 完全不收服务器频道，只做私聊客服
GUILD_MODES = ("mention", "all", "off")

_registered = False


# ── 配置读取（worker 与 provider 共用的单一事实源） ──────────────────────────

def discord_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    pl = (config or {}).get("platform_login", {}) or {}
    return pl.get("discord", {}) or {}


def bot_enabled(config: Dict[str, Any]) -> bool:
    """接入开关（三态：显式配置优先，未写过时按 resolve_login_switch 的桌面默认）。

    刻意**不**进 ``_DESKTOP_LOGIN_DEFAULT_ON``：Discord 需要运营自己去开发者后台建
    Bot、开 privileged intent、填 token，没有「随包即用」的可能，桌面版默认开只会
    在接入弹窗里点亮一个必然失败的入口。新子系统默认关，符合本仓 feature flag 约定。
    """
    return resolve_login_switch(config, "platform_login.discord.bot_enabled")


def configured_token(config: Dict[str, Any]) -> str:
    return str(discord_cfg(config).get("bot_token") or "").strip()


def guild_mode(config: Dict[str, Any]) -> str:
    m = str(discord_cfg(config).get("guild_mode") or "mention").strip().lower()
    return m if m in GUILD_MODES else "mention"


def is_discord_available() -> bool:
    """``discord.py`` 是否已安装（未装则整条 Discord 链优雅缺席，不崩启动）。"""
    try:
        import importlib.util
        return importlib.util.find_spec("discord") is not None
    except Exception:  # noqa: BLE001
        return False


def mask_token(token: str) -> str:
    """``MTIzNDU2…abcd`` → ``MTIzN…abcd``（日志/接口回显专用，绝不出全串）。"""
    t = str(token or "")
    if len(t) <= 12:
        return "***" if t else ""
    return f"{t[:5]}…{t[-4:]}"


# ── Token 校验（HTTP 薄封装，测试可 monkeypatch） ───────────────────────────

async def _get_json(url: str, headers: Dict[str, str], timeout: float = 15.0) -> Tuple[int, Dict[str, Any]]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=headers)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        return r.status_code, (body if isinstance(body, dict) else {})


async def validate_token(token: str) -> Dict[str, Any]:
    """打 ``GET /users/@me`` 验 Bot Token。

    返回 ``{ok, account_id, name, avatar_url, reason}``。``reason`` 是**机器可读**的
    分类（``invalid_token`` / ``network`` / ``http_<code>``），供上游决定文案与是否重试
    ——只回一句人话会让调用方无从判断该重试还是该让用户换 token。
    """
    tok = str(token or "").strip()
    if not tok:
        return {"ok": False, "reason": "empty_token"}
    try:
        code, body = await _get_json(
            f"{API_BASE}/users/@me", {"Authorization": f"Bot {tok}"})
    except Exception as ex:  # noqa: BLE001
        logger.debug("[discord] token 校验网络失败", exc_info=True)
        return {"ok": False, "reason": "network", "detail": str(ex)}
    if code == 401:
        return {"ok": False, "reason": "invalid_token"}
    if code != 200:
        return {"ok": False, "reason": f"http_{code}",
                "detail": str(body.get("message") or "")}
    uid = str(body.get("id") or "")
    if not uid:
        return {"ok": False, "reason": "no_id"}
    name = str(body.get("global_name") or body.get("username") or uid)
    avatar = str(body.get("avatar") or "")
    return {
        "ok": True, "account_id": uid, "name": name,
        "avatar_url": (f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png"
                       if avatar else ""),
        "discriminator": str(body.get("discriminator") or ""),
    }


# ── provider 工厂 + 注册 ─────────────────────────────────────────────────────

def make_provider(config: Dict[str, Any]):
    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        # 前端表单 token 优先于配置 token（多 Bot 场景一号一 token）
        token = str((ctx or {}).get("bot_token") or "").strip() or configured_token(config)
        if not token:
            return {
                "instruction": "请先在配置 platform_login.discord.bot_token 填入 Bot Token"
                               "（Discord 开发者后台 → Bot → Reset Token），并开启"
                               " MESSAGE CONTENT INTENT。",
                "reason_code": "token_required",
            }
        if not is_discord_available():
            return {
                "instruction": "服务端缺少 discord.py 依赖，无法接入 Discord Bot。"
                               "请安装：pip install -U discord.py",
                "reason_code": "dependency_missing",
            }

        res = await validate_token(token)
        if not res.get("ok"):
            reason = str(res.get("reason") or "")
            msg = {
                "invalid_token": "Bot Token 无效或已被重置，请到开发者后台重新生成。",
                "network": "无法连接 Discord API，请检查网络/代理。",
                "empty_token": "Bot Token 为空。",
            }.get(reason, f"Discord 校验失败（{reason}）。")
            logger.info("[discord] token 校验失败 reason=%s token=%s",
                        reason, mask_token(token))
            return {"instruction": msg, "reason_code": reason or "verify_failed"}

        aid = str(res.get("account_id") or "")
        try:
            # merge_meta：只补 bot_token / bot_name，绝不整块覆盖——重登录抹掉
            # persona_id 绑定是本仓踩过的实锤事故（见 whatsapp_baileys_login 注释）。
            get_account_registry().upsert(
                "discord", aid, mode="protocol", status="online",
                meta={"bot_token": token, "bot_name": res.get("name") or ""},
                merge_meta=True)
        except Exception as ex:  # noqa: BLE001
            # 落库失败**必须如实失败**：token 就是这条链路的全部产物，没存下来
            # 编排器起 worker 时取不到 token，而 UI 早已显示「已连接」——
            # 表现为「明明连上了却一条消息都收不到」，且没有任何线索指向登录环节。
            logger.warning("[discord] 注册表写入失败，判定接入失败 id=%s", aid,
                           exc_info=True)
            return {
                "instruction": f"Token 校验通过，但账号写入失败（{ex}）。"
                               "请检查实例数据目录是否可写后重试。",
                "reason_code": "registry_write_failed",
            }
        try:
            from src.ai.persona_voice import ensure_account_default_persona
            ensure_account_default_persona(
                get_account_registry(), "discord", aid, config)
        except Exception:  # noqa: BLE001
            # 人设兜底属锦上添花，失败不该拦住已经存好 token 的接入
            logger.debug("[discord] 默认人设绑定失败（忽略）", exc_info=True)

        try:
            from src.integrations.account_self_profile import enrich_from_fields
            await enrich_from_fields(
                "discord", aid, name=str(res.get("name") or ""),
                avatar_url=str(res.get("avatar_url") or ""), config=config)
        except Exception:  # noqa: BLE001
            logger.debug("[discord] self_profile 富集失败（忽略）", exc_info=True)

        logger.info("[discord] Bot 接入成功 name=%s id=%s", res.get("name"), aid)

        async def _poll(session: Any) -> Dict[str, Any]:
            # 校验已在 start 阶段同步完成 → 首次轮询即 authorized（无扫码等待期）。
            return {"status": "authorized", "account_id": aid,
                    "detail": str(res.get("name") or "")}

        async def _cancel(session: Any) -> None:
            return None

        return {
            "qr_image": "", "qr_url": "",
            "instruction": f"Bot「{res.get('name')}」已连接。注意：Bot 只能被动接待"
                           "——对方需与 Bot 有共同服务器或先私信 Bot，才能建立会话。",
            "poll": _poll, "cancel": _cancel,
            "state": {"account_id": aid, "token": mask_token(token)},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 Discord bot provider（幂等）。"""
    global _registered
    if _registered:
        return True
    if not bot_enabled(config):
        return False
    register_login_provider("discord", "protocol", make_provider(config))
    _registered = True
    logger.info("[discord] Discord Bot 登录 provider 已注册")
    return True


def _reset_for_tests() -> None:
    global _registered
    _registered = False
