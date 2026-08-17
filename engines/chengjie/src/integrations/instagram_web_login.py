"""Instagram 个人号网页托管登录 provider（P2：给纯官方的 IG 补一条个人号路径）。

Instagram 官方 Graph API 门槛高（要 Meta 开发者 App + IG 专业账号 + 关联 Facebook Page +
一堆令牌 + 公网回调 + 权限审核），把绝大多数个人卖家挡在门外。个人号这条路：用 Playwright
驱动一个隔离持久化 Chromium 加载 **instagram.com**，在服务器窗口内完成官方登录（账密 / 2FA），
再用 DOM/网络层收发私信（DM），功能对齐官方网页版。

本模块是 Python 侧桥接：把统一收件箱「账号管理 → ＋ 新增账号（网页托管）」的登录请求转发给
一个独立运行的 **Playwright Node 微服务**（见 ``services/instagram-web/``），契约与
``messenger_web_login`` **逐一对齐**（login/start → poll(status) → 成功落库 + self_profile 富集）。
登录形态＝``hosted``（服务器隔离浏览器内完成，本窗口做实时预览，**不使用二维码**）。

落地约束（与 M5 Messenger web 一致的谨慎姿态）：
- IG 网页自动化是**非官方**接入（依赖 instagram.com DOM，平台改版可能需微调选择器），有账号
  被限制 / 封禁风险 → 默认**不启用**，需在 ``config.platform_login.instagram.web_enabled: true``
  显式开启，且强烈建议**用小号 + 一号一代理**。
- 桥接通过 HTTP 调用微服务；服务不可达时**优雅降级**为错误提示（带 reason_code），不影响主进程。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8793"
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    ig = pl.get("instagram", {}) or {}
    return str(ig.get("web_url") or _DEFAULT_BASE_URL).rstrip("/")


def web_enabled(config: Dict[str, Any]) -> bool:
    # 显式配置优先（含 false）；未写过 → False（**不**随桌面默认开：非官方接入有封号
    # 风险，故不进 _DESKTOP_LOGIN_DEFAULT_ON，必须运营显式 opt-in）。单一事实源。
    return resolve_login_switch(config, "platform_login.instagram.web_enabled")


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
            logger.debug("[instagram_web] start 调用失败", exc_info=True)
            # 必须带 reason_code：只给 instruction 上游拿不到失败信号，会挂到 TTL 耗尽。
            return {"instruction": f"无法连接 Instagram 网页服务（{ex}）。请确认 instagram-web 微服务已启动。",
                    "reason_code": "service_down"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[instagram_web] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    # merge_meta：防重登录整块覆盖 meta 抹掉 persona_id 等绑定（对齐 messenger/wa）。
                    get_account_registry().upsert(
                        "instagram", aid, mode="web", status="online",
                        meta={"instagram_login_id": login_id}, merge_meta=True)
                    try:
                        from src.ai.persona_voice import ensure_account_default_persona
                        ensure_account_default_persona(
                            get_account_registry(), "instagram", aid, config)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    logger.debug("[instagram_web] 注册表写入失败", exc_info=True)
                # self_profile 富集：微服务若回传昵称/头像 URL → 富集账号自身身份
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "instagram", aid,
                        name=str(res.get("name") or res.get("username") or ""),
                        avatar_url=str(res.get("avatar_url") or res.get("profile_pic_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[instagram_web] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": aid,
                    "detail": str(res.get("detail") or ""),
                    "reason_code": str(res.get("reason_code") or ""),
                    # 实时提示码：微服务旁观登录页判出的「此刻在要什么」（two_factor/checkpoint/…）
                    "hint_code": str(res.get("hint_code") or ""),
                    "qr_image": str(res.get("qr_image") or "")}

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[instagram_web] cancel 调用失败", exc_info=True)

        # 措辞注意：IG 网页个人登录没有扫码，绝不出现「扫码」字样（与 Messenger hosted 同）。
        return {
            "qr_image": qr_image,
            "instruction": "服务器上已打开 Instagram 官方登录窗口，请在该机器上完成登录（账密 / 2FA）。"
                           "完成后本窗口会自动确认——本方式不使用二维码，无需用手机扫描。",
            "instruction_key": "inbox.connect.instr_ig_web",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 Instagram web provider（幂等）。

    仅当 ``platform_login.instagram.web_enabled: true`` 时注册（服务可达性在 start 时检测并降级）。
    """
    global _registered
    if _registered:
        return True
    if not web_enabled(config):
        return False
    register_login_provider("instagram", "web", make_provider(config))
    _registered = True
    logger.info("[instagram_web] Instagram web 登录 provider 已注册 (base=%s)",
                service_base_url(config))
    return True
