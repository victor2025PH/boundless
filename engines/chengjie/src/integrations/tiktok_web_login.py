"""TikTok 个人号网页托管登录 provider（TK-3 ②-A 阶段 1，assistOnly）。

真机（huoke）是 TikTok 个人号私信的主路；本模块是**备用读路**的 Python 桥接：把「账号登录（网页托管）」
的请求转发给独立运行的 Playwright Node 边车（``services/tiktok-web/``），契约与 ``instagram_web_login``
逐一对齐（login/start → poll(status) → 成功落库 mode=web + self_profile 富集）。登录形态＝``hosted``
（服务器隔离浏览器内完成，本窗口做实时预览，**不使用二维码**）。

阶段 1 边车只读：私信进收件箱、智聊起草、坐席看得见；**不代发**（边车 ``/send*`` 恒 501 ``assist_only``）。
人工可发（②-B）须 ① 真机 + ②-A 跑满 72h 自有账号验证后另开；自动发永不。

落地约束：
- 非官方接入（依赖 tiktok.com DOM）有封号风险 → 默认**不启用**，须 ``platform_login.tiktok.web_enabled: true``
  显式开启，且不随桌面默认开；小号 + 一号一代理。
- 本模块**不**把 tiktok 加进 ``platform_login`` 的平台 / 方式静态表（登录弹窗不列它）——那是 ②-B 连同
  ``TikTokWebWorker`` / 能力矩阵行一起做的事；阶段 1 只提供 provider 与就绪探测，供接入页「网页托管」页签用。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch

logger = logging.getLogger(__name__)

PLATFORM = "tiktok"
MODE = "web"
_DEFAULT_BASE_URL = "http://127.0.0.1:8794"
ASSIST_ONLY = True
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    tt = pl.get("tiktok", {}) or {}
    return str(tt.get("web_url") or _DEFAULT_BASE_URL).rstrip("/")


def web_enabled(config: Dict[str, Any]) -> bool:
    """显式配置优先（含 false）；未写过 → False（非官方接入，必须运营显式 opt-in）。"""
    return resolve_login_switch(config, "platform_login.tiktok.web_enabled")


# ── HTTP 薄封装（测试可 monkeypatch） ────────────────────────────────────────

async def _post_json(url: str, payload: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def _get_json(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


def _normalize_status(raw: str) -> str:
    s = str(raw or "").lower()
    if s in ("open", "authorized", "connected", "online", "logged_in"):
        return "authorized"
    if s in ("scanned", "pairing"):
        return "scanned"
    if s in ("expired", "timeout"):
        return "expired"
    if s in ("failed", "error", "logged_out"):
        return "failed"
    return "pending"


async def service_health(config: Dict[str, Any]) -> Dict[str, Any]:
    """边车就绪探测（接入页「网页托管」页签用）：``{reachable, assist_only, accounts}``；不可达如实 False。"""
    base = service_base_url(config)
    try:
        h = await _get_json(f"{base}/health", timeout=4.0)
    except Exception as ex:  # noqa: BLE001
        return {"reachable": False, "assist_only": ASSIST_ONLY, "accounts": 0, "error": str(ex)[:120], "base": base}
    accounts = 0
    try:
        accounts = len(((await _get_json(f"{base}/accounts", timeout=4.0)) or {}).get("accounts") or [])
    except Exception:  # noqa: BLE001
        pass
    return {"reachable": bool(h.get("ok")), "assist_only": bool(h.get("assist_only", ASSIST_ONLY)),
            "accounts": accounts, "base": base}


# ── provider 工厂 + 注册 ─────────────────────────────────────────────────────

def make_provider(config: Dict[str, Any]):
    base = service_base_url(config)

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        proxy = (ctx or {}).get("proxy") or {}
        payload: Dict[str, Any] = {"account_id": account_id or ""}
        if proxy.get("host"):
            payload["proxy_url"] = proxy.get("url") or ""
        try:
            data = await _post_json(f"{base}/login/start", payload)
        except Exception as ex:  # noqa: BLE001
            logger.debug("[tiktok_web] start 调用失败", exc_info=True)
            return {"instruction": f"无法连接 TikTok 网页边车（{ex}）。请确认 tiktok-web 微服务已启动。",
                    "reason_code": "service_down"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[tiktok_web] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    # merge_meta：防重登录整块覆盖 meta 抹掉 persona_id 等绑定。assist_only 落 meta 供闸门 / 徽标读。
                    get_account_registry().upsert(
                        PLATFORM, aid, mode=MODE, status="online",
                        meta={"tiktok_login_id": login_id, "username": str(res.get("username") or ""),
                              "assist_only": ASSIST_ONLY}, merge_meta=True)
                    try:
                        from src.ai.persona_voice import ensure_account_default_persona
                        ensure_account_default_persona(get_account_registry(), PLATFORM, aid, config)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    logger.debug("[tiktok_web] 注册表写入失败", exc_info=True)
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        PLATFORM, aid,
                        name=str(res.get("name") or res.get("username") or ""),
                        avatar_url=str(res.get("avatar_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[tiktok_web] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": aid,
                    "detail": str(res.get("detail") or ""),
                    "reason_code": str(res.get("reason_code") or ""),
                    "hint_code": str(res.get("hint_code") or ""),
                    "qr_image": str(res.get("qr_image") or "")}

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[tiktok_web] cancel 调用失败", exc_info=True)

        # 措辞：网页托管登录没有扫码，绝不出现「扫码」字样；阶段 1 只读要说清。
        return {
            "qr_image": qr_image,
            "instruction": "服务器上已打开 TikTok 官方登录窗口，请在该机器上完成登录（账密 / 2FA / 验证码）。"
                           "完成后本窗口会自动确认——本方式不使用二维码。阶段 1 只读：私信进收件箱、智聊起草，"
                           "不代发；发送走获客真机。",
            "instruction_key": "inbox.connect.instr_tt_web",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base, "assist_only": ASSIST_ONLY},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 TikTok web provider（幂等）。仅当 ``platform_login.tiktok.web_enabled: true``。"""
    global _registered
    if _registered:
        return True
    if not web_enabled(config):
        return False
    register_login_provider(PLATFORM, MODE, make_provider(config))
    _registered = True
    logger.info("[tiktok_web] TikTok web 登录 provider 已注册（assist-only，base=%s）", service_base_url(config))
    return True


__all__ = ["PLATFORM", "MODE", "ASSIST_ONLY", "service_base_url", "web_enabled", "service_health",
           "make_provider", "maybe_register"]
