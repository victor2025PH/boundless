"""Messenger 网页模式（web mode）登录 provider（M5）。

Messenger 没有像 WhatsApp Baileys 那样的干净协议库，但它**有官方网页版 messenger.com**。
本模块是 Python 侧桥接：把统一收件箱「账号管理 → ＋ 新增账号（网页托管）」的登录请求转发给
一个独立运行的 **Playwright Node 微服务**（见 ``services/messenger-web/``），由它用隔离
浏览器加载 messenger.com、完成官方登录、维护连接、DOM 收发，功能对齐官方网页版。

落地约束（与 M2/M3 一致的谨慎姿态）：
- 需先 ``npm install`` 并启动 Node 微服务，且需真号登录联调；故默认**不启用**，需在
  ``config.platform_login.messenger.web_enabled: true`` 显式开启。
- 桥接通过 HTTP 调用微服务；服务不可达时**优雅降级**为错误提示，不影响主进程。
- 网络调用集中在 ``_post_json`` / ``_get_json`` 两个可被测试替换的薄封装里。

契约与 ``whatsapp_baileys_login`` 对齐：login/start → poll(status) → 成功落库 + self_profile 富集。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider, resolve_login_switch

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8791"
_registered = False


def service_base_url(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    mg = pl.get("messenger", {}) or {}
    return str(mg.get("web_url") or _DEFAULT_BASE_URL).rstrip("/")


def web_enabled(config: Dict[str, Any]) -> bool:
    # 三态：显式配置优先（含 false）；未写过时桌面版默认开（见 resolve_login_switch 注释）。
    # 单一事实源——全部消费方（orchestrator / readiness / channel_adapters /
    # protocol_diagnostics / account 路由）都经本函数，故升级安装的 Messenger 扫码
    # 灰卡在此一处收口（104 事故的「另一半」：line/wa 已接、messenger 曾漏接）。
    return resolve_login_switch(config, "platform_login.messenger.web_enabled")


def interactive_login_enabled(config: Dict[str, Any]) -> bool:
    """表单中继（Form-Relay）交互登录开关——把登录搬进程序内（headless 边车 + 应用侧
    原生分步表单：账密 / 2FA 码 / E2EE PIN），不再弹独立浏览器窗口、也不靠只读截图。

    默认**关**（含桌面壳）：headless + 由边车把值填进登录页，改变了 Facebook 的自动化
    判定面，放量前需灰度验证账号不被风控。故刻意**不**进 `_DESKTOP_LOGIN_DEFAULT_ON`——
    `resolve_login_switch` 在未显式配置时对该路径恒回 False，只有显式
    `platform_login.messenger.interactive_login: true` 才开启。关闭时全链回落既有
    headed 窗口 + 截图预览 + 运维指引，逐字节旧行为。"""
    return resolve_login_switch(config, "platform_login.messenger.interactive_login")


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


def http_error_detail(ex: Exception) -> str:
    """HTTP 状态异常 → 附带响应体里的 error/reason_code（Node 边车的真实败因）。

    2026-08-15 173 实锤：/send 的 composer-not-found 500 在 Python 侧只留下
    「Server error '500 Internal Server Error' for url …」——真实原因（渲染超时/
    需要接受/PIN 浮层）在响应体里被 ``raise_for_status`` 丢弃，排查只能上机翻边车。
    duck-typed：任何带 ``.response`` 的异常都尝试提取；提取失败原样返回，绝不抛。
    """
    base = str(ex)
    resp = getattr(ex, "response", None)
    if resp is None:
        return base
    try:
        body = resp.json()
    except Exception:
        return base
    if not isinstance(body, dict):
        return base
    detail = str(body.get("error") or "").strip()
    reason = str(body.get("reason_code") or "").strip()
    extra = detail
    if reason and reason not in detail:
        extra = f"{detail} [{reason}]" if detail else f"[{reason}]"
    return f"{base} — {extra}" if extra else base


def _normalize_status(raw: str) -> str:
    s = str(raw or "").lower()
    if s in ("open", "authorized", "connected", "online"):
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
        # 表单中继开关：开 → 告诉边车走无窗口交互模式（offscreen / new_headless），登录页
        # 由应用侧原生表单驱动；关 → 不带该字段，边车维持既有 headed 弹窗行为（零回归）。
        interactive = interactive_login_enabled(config)
        payload: Dict[str, Any] = {"account_id": account_id or ""}
        if interactive:
            payload["interactive"] = True
        if proxy.get("host"):
            payload["proxy_url"] = proxy.get("url") or ""
        try:
            data = await _post_json(f"{base}/login/start", payload)
        except Exception as ex:  # noqa: BLE001
            logger.debug("[messenger_web] start 调用失败", exc_info=True)
            # 必须带 reason_code：只给 instruction 的话上游拿不到失败信号，会按「已开始登录」
            # 挂起会话，坐席对着转圈干等到 TTL（180s）才等来一句超时——而真相是服务压根没起。
            return {"instruction": f"无法连接 Messenger 网页服务（{ex}）。请确认 messenger-web 微服务已启动。",
                    "reason_code": "service_down"}

        login_id = str(data.get("login_id") or "")
        qr_image = str(data.get("qr_image") or "")

        async def _poll(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/status")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[messenger_web] status 调用失败", exc_info=True)
                return {"status": "pending", "detail": str(ex)}
            st = _normalize_status(res.get("status"))
            aid = str(res.get("account_id") or "")
            if st == "authorized" and aid:
                try:
                    # merge_meta：防重登录整块覆盖 meta 抹掉 persona_id 等绑定
                    get_account_registry().upsert(
                        "messenger", aid, mode="web", status="online",
                        meta={"messenger_login_id": login_id}, merge_meta=True)
                    try:
                        from src.ai.persona_voice import ensure_account_default_persona
                        ensure_account_default_persona(
                            get_account_registry(), "messenger", aid, config)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    logger.debug("[messenger_web] 注册表写入失败", exc_info=True)
                # self_profile 富集：微服务若回传昵称/头像 URL → 富集账号自身身份
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "messenger", aid,
                        name=str(res.get("name") or ""),
                        avatar_url=str(res.get("avatar_url") or res.get("profile_pic_url") or ""),
                        config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[messenger_web] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": aid,
                    "detail": str(res.get("detail") or ""),
                    # 终态失败原因（路由只在非空时落会话，避免被后续空值抹掉）
                    "reason_code": str(res.get("reason_code") or ""),
                    # 实时提示码：微服务旁观登录页判出的「此刻在要什么」
                    # （two_factor / checkpoint / password_error），非终态，可来回变。
                    "hint_code": str(res.get("hint_code") or ""),
                    "qr_image": str(res.get("qr_image") or "")}

        async def _cancel(session: Any) -> None:
            try:
                await _post_json(f"{base}/login/{login_id}/cancel", {})
            except Exception:  # noqa: BLE001
                logger.debug("[messenger_web] cancel 调用失败", exc_info=True)

        # 表单中继只读探针：把边车对登录页的分类（login_form/two_factor/e2ee_pin/checkpoint/…）
        # 翻成「应用此刻该渲染哪一步原生表单」。纯读、失败软回落 wait（绝不阻断登录链）。
        # 仅在 interactive_login 开启时随 provider 暴露（interactive 已在 _provider 顶部算出）；
        # 关闭时为 None，路由回 not_supported，前端走既有 headed / 截图预览旧路径。

        async def _relay_step(session: Any) -> Dict[str, Any]:
            try:
                res = await _get_json(f"{base}/login/{login_id}/relay-step")
            except Exception as ex:  # noqa: BLE001
                logger.debug("[messenger_web] relay-step 调用失败", exc_info=True)
                return {"status": "pending", "step": "wait", "fields": [],
                        "error": False, "escalate": False, "code": "",
                        "qr_image": "", "detail": str(ex)}
            return {
                "status": str(res.get("status") or "pending"),
                "booting": bool(res.get("booting")),
                "step": str(res.get("step") or "wait"),
                "fields": list(res.get("fields") or []),
                "error": bool(res.get("error")),
                "escalate": bool(res.get("escalate")),
                "code": str(res.get("code") or ""),
                "qr_image": str(res.get("qr_image") or ""),
            }

        async def _relay_submit(session: Any, step: str, values: Dict[str, Any]):
            # 把应用侧原生表单字段值填回 headless 登录页（边车 fire-and-forget，结果由 poll
            # / relay_step 观测）。失败一律结构化回落，绝不抛（不阻断登录链）。
            try:
                res = await _post_json(
                    f"{base}/login/{login_id}/relay-submit",
                    {"step": str(step or ""),
                     "values": values if isinstance(values, dict) else {}})
            except Exception as ex:  # noqa: BLE001
                logger.debug("[messenger_web] relay-submit 调用失败", exc_info=True)
                return {"ok": False, "reason_code": "service_down", "detail": str(ex)}
            return {
                "ok": bool(res.get("ok")),
                "status": str(res.get("status") or ""),
                "step": str(res.get("step") or ""),
                "reason_code": str(res.get("reason_code") or ""),
                "missing": list(res.get("missing") or []),
                "submitted": bool(res.get("submitted")),
                "accepted": bool(res.get("accepted")),
                "detail": str(res.get("detail") or ""),
            }

        # 措辞注意：Facebook 网页端没有扫码登录，这里绝不能出现「扫码」字样——
        # 方式选择卡明写「不使用二维码」，等待页再冒出「扫码均可」是自相矛盾（实录事故）。
        # instruction_key 供前端取本地化文案（zh/en 同源），raw instruction 仅作后端兜底。
        # B64 续（2026-08-23 117 实测）：interactive（表单中继）模式的浏览器窗口是
        # **刻意离屏不可见**的，登录发生在应用弹窗内的原生表单——沿用 hosted 的
        # 「服务器上已打开官方登录窗口，请在该机器上完成登录」文案会让用户满桌面找
        # 一扇不存在的窗（实录：老板按文案等窗，判定「登录窗打不开」）。两种模式
        # 必须各说各话。
        if interactive:
            _instr = ("已进入应用内登录：请直接在下方表单输入 Facebook 邮箱和密码"
                      "（需要验证码时也在这里输入），不会弹出浏览器窗口，完成后自动确认。")
            _instr_key = "inbox.connect.hint_inapp_login"
        else:
            _instr = ("服务器上已打开 Facebook 官方登录窗口，请在该机器上完成登录（账密 / 2FA）。"
                      "完成后本窗口会自动确认——本方式不使用二维码，无需用手机扫描。")
            _instr_key = "inbox.connect.hint_server_login"
        return {
            "qr_image": qr_image,
            "instruction": _instr,
            "instruction_key": _instr_key,
            "poll": _poll,
            "cancel": _cancel,
            "state": {"login_id": login_id, "base": base},
            # 表单中继：开启交互登录时暴露 interactive 能力位 + 只读探针 + 写入端；关闭时
            # interactive=False、relay_step/relay_submit=None（前端据此走既有 headed / 截图预览
            # 路径，零行为变化）。
            "interactive": interactive,
            "relay_step": _relay_step if interactive else None,
            "relay_submit": _relay_submit if interactive else None,
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 Messenger web provider（幂等）。

    仅当 ``web_enabled: true`` 时注册（服务可达性在 start 时检测并降级）。
    """
    global _registered
    if _registered:
        return True
    if not web_enabled(config):
        return False
    register_login_provider("messenger", "web", make_provider(config))
    _registered = True
    logger.info("[messenger_web] Messenger web 登录 provider 已注册 (base=%s)",
                service_base_url(config))
    return True
