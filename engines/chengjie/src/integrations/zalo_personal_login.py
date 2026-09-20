"""Zalo 个人号扫码登录 provider（P1：给纯官方的 Zalo 补一条个人号路径）。

Zalo 官方 OA API 门槛高（要 OA 官方账号）且能力受限（**仅文字 + 7 天互动窗**）——把
绝大多数东南亚个人卖家 / 小 B 挡在门外。社区主流的「个人号」方案是 **zca-js（Node.js）**：
模拟 Zalo Web 会话（``loginQR()`` 扫码），能收发文字/图片/语音/贴纸，能力反而比官方 OA 全。

本模块是 Python 侧桥接：把统一收件箱「账号管理 → ＋ 扫码新增（个人号）」的登录请求转发给
一个独立运行的 **zca-js Node 微服务**（见 ``services/zalo-personal/``），由它生成扫码二维码、
维护 Zalo Web 连接、回报账号上线、并把入站消息 push 回 ``/api/internal/protocol/ingest``。

契约与 ``whatsapp_baileys_login`` / ``messenger_web_login`` **逐一对齐**（login/start →
poll(status) → 成功落库 + self_profile 富集），故 Python 桥接近乎同构、便于单测。

落地约束（与 M2/M3/M5 一致的谨慎姿态）：
- zca-js 是**非官方**接入，有账号被限制 / 封禁风险 → 默认**不启用**，需在
  ``config.platform_login.zalo.web_enabled: true`` 显式开启，且强烈建议**用小号 + 一号一代理**。
- 需先 ``npm install`` 并启动 Node 微服务，且需真号扫码联调。
- 桥接通过 HTTP 调用微服务；服务不可达时**优雅降级**为错误提示（带 reason_code），不影响主进程。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8792"
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    zl = pl.get("zalo", {}) or {}
    return str(zl.get("zca_url") or _DEFAULT_BASE_URL).rstrip("/")


def web_enabled(config: Dict[str, Any]) -> bool:
    # 显式配置优先（含 false）；未写过 → False（**不**随桌面默认开：非官方接入有封号
    # 风险，故不进 _DESKTOP_LOGIN_DEFAULT_ON，必须运营显式 opt-in）。单一事实源——
    # 全部消费方（orchestrator / readiness / channel_status 门控 / provider 注册）都经本函数。
    return resolve_login_switch(config, "platform_login.zalo.web_enabled")


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
    if s in ("scanned", "pairing", "qr_scanned"):
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
            # 透传给 Node 微服务（zca-js 侧按一号一代理应用）
            payload["proxy_url"] = proxy.get("url") or ""
        try:
            data = await _post_json(f"{base}/login/start", payload)
        except Exception as ex:  # noqa: BLE001
            logger.debug("[zalo_personal] start 调用失败", exc_info=True)
            # 必须带 reason_code：只给 instruction 上游拿不到失败信号，会挂到 TTL 耗尽。
            return {"instruction": f"无法连接 Zalo 个人号服务（{ex}）。请确认 zalo-personal 微服务已启动。",
                    "reason_code": "service_down"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")
        qr_url = str(data.get("qr_url") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[zalo_personal] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    # merge_meta：只登记 zca_login_id，绝不整块覆盖 meta（对齐 baileys 的
                    # 2026-07-23 教训：重登录抹掉 persona_id 绑定 → 错人设 / 错语言音色）。
                    get_account_registry().upsert(
                        "zalo", aid, mode="web", status="online",
                        meta={"zca_login_id": login_id}, merge_meta=True)
                    try:
                        from src.ai.persona_voice import ensure_account_default_persona
                        ensure_account_default_persona(
                            get_account_registry(), "zalo", aid, config)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    logger.debug("[zalo_personal] 注册表写入失败", exc_info=True)
                # 身份化：Node 若回传 display_name/avatar_url → 富集自身昵称/头像
                # （前向兼容：字段缺失则 enrich 内部 no-op）。
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "zalo", aid,
                        name=str(res.get("display_name") or res.get("name") or ""),
                        avatar_url=str(res.get("avatar_url") or res.get("profile_pic_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[zalo_personal] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": aid,
                    "detail": str(res.get("detail") or ""),
                    "qr_image": str(res.get("qr_image") or "")}

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[zalo_personal] cancel 调用失败", exc_info=True)

        return {
            "qr_image": qr_image,
            "qr_url": qr_url,
            "instruction": "用手机 Zalo：右上角 ＋ → 扫码（QR），扫描本窗口二维码。登录成功后本窗口会自动确认。",
            # i18n 键随会话下发：英文坐席按键取本地化指引，raw instruction 仅兜底
            "instruction_key": "inbox.connect.instr_zalo_web",
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base},
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 Zalo 个人号 web provider（幂等）。

    仅当 ``platform_login.zalo.web_enabled: true`` 时注册（服务可达性在 start 时检测并降级）。
    """
    global _registered
    if _registered:
        return True
    if not web_enabled(config):
        return False
    register_login_provider("zalo", "web", make_provider(config))
    _registered = True
    logger.info("[zalo_personal] Zalo 个人号 web 登录 provider 已注册 (base=%s)",
                service_base_url(config))
    return True
