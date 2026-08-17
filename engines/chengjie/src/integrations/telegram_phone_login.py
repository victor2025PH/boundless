"""Telegram 手机号 + 验证码登录引擎（第 1 层：状态机，DI 客户端可离线单测）。

与 ``telegram_protocol_login.py`` 的**扫码（关联设备 / ExportLoginToken）**并列的第二条
服务端登录路径：``auth.sendCode`` → ``auth.signIn``（→ 两步验证 ``check_password``）。
面向「只有一台手机、无法扫自己屏」的用户，是最直觉的登录入口。

⚠ 风险定位（产品必须显式告知，见 platform_login 的 notice）：号码 + 验证码是 Telegram
**高封号面**的路径——新号 / 共享 api_id 上 ``sendCode`` 易触发号码被限、短信 flood 限流；
与扫码「挂已授权号」相比风险更大。桌面版升级默认开（``phone_enabled`` 进
``_DESKTOP_LOGIN_DEFAULT_ON``），但 UI 仍排在扫码之后、带风险 notice；服务器/
示例配置默认关。强烈建议小号 + 独立代理。

设计（为什么这样切）：
- **DI 客户端**：真实 pyrogram 调用集中在 ``client_factory`` 产出的 client 后面；引擎只按
  返回值/异常推进状态机。于是**无真号也能单测全部迁移**（fake client 抛配置好的异常）。
- **纯分类**：``classify_phone_login_exception`` 先判号码/验证码专属码，再回落
  ``telegram_protocol_login.classify_login_exception``（限流/不可达/凭据坏），单一事实源。
- **绝不自动注册**：``PhoneNumberUnoccupied``（该号无 Telegram 账号）一律判失败并提示去
  App 注册——自动 signUp 是拉黑与合规双重雷区。
- **落库/凭据池/设备指纹**复用既有机制（在下一层 provider 里接线；QR 侧的落库逻辑当前
  在 sibling 文件的闭包内，等其提交稳定后抽公共件，本层先不碰其文件）。
- **状态机成功收尾**（account_id / phone / session_string / self_profile）与 QR 侧同形，
  便于下一层共用同一条落库路径。
"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# 复用扫码 provider 的**纯**助手（单一事实源，绝不重造）：凭据解析 / 通用异常分类 /
# 两步验证判定。这些是 public 函数，import 不触碰其状态机与落库闭包。
from src.integrations.telegram_protocol_login import (
    _is_bad_password,
    _is_password_needed,
    classify_login_exception,
    resolve_credentials,  # noqa: F401  (供下一层 provider 复用，一并再导出)
)

logger = logging.getLogger(__name__)

_DEFAULT_SESSIONS_DIR = "sessions"


# ── 纯分类：号码/验证码专属 reason_code（先专属后回落通用）───────────────────────

def classify_phone_login_exception(ex: Exception) -> str:
    """把手机号登录异常归类为漏斗 ``reason_code``（按「具体 → 泛化」排序）。

    专属桶（前端据此给精准处置，绝不笼统"请重试"）：
    - ``phone_invalid``    号码格式非法（PHONE_NUMBER_INVALID）——改号码，重试无解。
    - ``phone_banned``     号码被封（PHONE_NUMBER_BANNED）——换号，重试/等待都无解。
    - ``phone_unoccupied`` 该号无 Telegram 账号（PHONE_NUMBER_UNOCCUPIED）——去 App 注册；
                           **绝不**自动 signUp。
    - ``code_invalid``     验证码错（PHONE_CODE_INVALID / EMPTY）——可重输。
    - ``code_expired``     验证码过期（PHONE_CODE_EXPIRED）——需重发。
    其余回落 ``classify_login_exception``（rate_limited / tg_unreachable / cred_invalid）。
    """
    name = type(ex).__name__
    text = str(ex or "").upper()
    if name == "PhoneNumberInvalid" or "PHONE_NUMBER_INVALID" in text:
        return "phone_invalid"
    if name == "PhoneNumberBanned" or "PHONE_NUMBER_BANNED" in text:
        return "phone_banned"
    if name == "PhoneNumberUnoccupied" or "PHONE_NUMBER_UNOCCUPIED" in text:
        return "phone_unoccupied"
    if name in ("PhoneCodeInvalid", "PhoneCodeEmpty") or \
            "PHONE_CODE_INVALID" in text or "PHONE_CODE_EMPTY" in text:
        return "code_invalid"
    if name == "PhoneCodeExpired" or "PHONE_CODE_EXPIRED" in text:
        return "code_expired"
    return classify_login_exception(ex)


def _is_unoccupied(ex: Exception) -> bool:
    return (type(ex).__name__ == "PhoneNumberUnoccupied"
            or "PHONE_NUMBER_UNOCCUPIED" in str(ex).upper())


def _is_bad_code(ex: Exception) -> bool:
    """验证码错/空（可重输，不算失败）——过期(EXPIRED)刻意不并入：那要重发不是重输。"""
    name = type(ex).__name__
    if name in ("PhoneCodeInvalid", "PhoneCodeEmpty"):
        return True
    up = str(ex).upper()
    return "PHONE_CODE_INVALID" in up or "PHONE_CODE_EMPTY" in up


# ── 状态机（真实 pyrogram 调用全程 try/except 降级；client 经 DI 便于单测）──────────

class TelegramPhoneLogin:
    """管理一次「号码 + 验证码（+2FA）」登录的生命周期（异步）。

    状态：pending → code_needed → (authorized | password_needed | failed)
          password_needed → (authorized | 停留 password_needed 可重试 | failed)
    """

    def __init__(self, api_id: int, api_hash: str, sessions_dir: str,
                 proxy: Optional[Dict[str, Any]] = None,
                 device_kwargs: Optional[Dict[str, str]] = None,
                 client_factory: Optional[Callable[..., Any]] = None) -> None:
        self.api_id = int(api_id)
        self.api_hash = str(api_hash)
        self.sessions_dir = Path(sessions_dir)
        self.session_name = f"tg_phone_{secrets.token_hex(6)}"
        self.proxy = proxy or None
        self.device_kwargs = dict(device_kwargs or {})
        self._client_factory = client_factory
        self.client: Any = None

        self.status = "pending"   # pending|code_needed|password_needed|authorized|failed|expired
        self.phone = ""
        self.phone_code_hash = ""
        self.account_id = ""
        self.detail = ""
        self.reason_code = ""
        # cred_invalid 处置元信息（telegram_protocol_login.cred_fail_meta 产出）：
        # 凭据来源 + 换发冷却剩余秒，前端据此倒计时/亮修正表单（与 QR 侧同契约）。
        self.cred_source = ""
        self.retry_after_sec = -1
        self.session_string = ""
        self.self_profile: Dict[str, str] = {}

    def result(self) -> Dict[str, Any]:
        out = {
            "status": self.status,
            "account_id": self.account_id,
            "detail": self.detail,
            "reason_code": self.reason_code,
        }
        if self.cred_source:
            out["cred_source"] = self.cred_source
            out["retry_after_sec"] = int(self.retry_after_sec)
        return out

    # ── 客户端构造（DI 优先；否则懒建 pyrogram，绝不在模块顶层 import）──────────
    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                self.session_name, api_id=self.api_id, api_hash=self.api_hash,
                workdir=str(self.sessions_dir), proxy=self.proxy,
                device_kwargs=self.device_kwargs)
        from pyrogram import Client  # noqa: WPS433 (lazy)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        kwargs: Dict[str, Any] = dict(
            api_id=self.api_id, api_hash=self.api_hash,
            workdir=str(self.sessions_dir))
        if self.proxy:
            kwargs["proxy"] = self.proxy
        kwargs.update(self.device_kwargs)
        return Client(self.session_name, **kwargs)

    async def start(self, phone: str) -> Dict[str, Any]:
        """发起：连接 + sendCode。成功 → code_needed（记 phone_code_hash）。"""
        phone = str(phone or "").strip()
        if not phone:
            self.status = "failed"
            self.detail = "手机号为空"
            self.reason_code = "phone_invalid"
            return self.result()
        self.phone = phone
        try:
            self.client = self._build_client()
            await self.client.connect()
            sent = await self.client.send_code(phone)
            self.phone_code_hash = str(getattr(sent, "phone_code_hash", "") or "")
            self.status = "code_needed"
            self.detail = ""
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.reason_code = classify_phone_login_exception(ex)
            self.detail = f"发送验证码失败：{ex}"
            # 与 QR 侧同口径：ERROR 级进公网 beacon（凭据/号码级失败靠用户拍照才发现太晚）
            logger.error("[tg_phone_login] send_code 失败 reason=%s type=%s: %s",
                         self.reason_code or "login_failed", type(ex).__name__, ex,
                         exc_info=True)
            await self._safe_disconnect(remove_session=True)
        return self.result()

    async def submit_code(self, code: str) -> Dict[str, Any]:
        """提交验证码：signIn。→ authorized | password_needed | 停留 code_needed（码错）| failed。"""
        if self.status != "code_needed":
            return self.result()
        code = str(code or "").strip()
        if not code:
            self.detail = "验证码为空"
            return self.result()
        try:
            user = await self.client.sign_in(self.phone, self.phone_code_hash, code)
            await self._finish_with_user(user)
        except Exception as ex:  # noqa: BLE001
            if _is_password_needed(ex):
                self.status = "password_needed"
                self.detail = "该账号已开启两步验证，请输入云密码完成登录"
                logger.info("[tg_phone_login] 验证码通过，等待两步验证云密码")
            elif _is_bad_code(ex):
                # 码错：停留 code_needed 可重输，不算失败、不清 session（phone_code_hash 仍有效）
                self.detail = "验证码错误，请重新输入"
                logger.info("[tg_phone_login] 验证码错误，可重试")
            elif _is_unoccupied(ex):
                # 该号无账号：**绝不**自动注册，判失败并指路 App 注册
                self.status = "failed"
                self.reason_code = "phone_unoccupied"
                self.detail = "该号码尚未注册 Telegram，请先在手机 App 上注册后再登录"
                await self._safe_disconnect(remove_session=True)
            else:
                self.status = "failed"
                self.reason_code = classify_phone_login_exception(ex)
                self.detail = f"登录失败：{ex}"
                logger.error("[tg_phone_login] sign_in 失败 type=%s: %s",
                             type(ex).__name__, ex, exc_info=True)
                await self._safe_disconnect(remove_session=True)
        return self.result()

    async def resend_code(self) -> Dict[str, Any]:
        """重发验证码（码过期/没收到时）：仅 code_needed 有效，刷新 phone_code_hash。"""
        if self.status != "code_needed" or not self.client:
            return self.result()
        try:
            sent = await self.client.send_code(self.phone)
            self.phone_code_hash = str(getattr(sent, "phone_code_hash", "") or "")
            self.detail = "验证码已重发"
        except Exception as ex:  # noqa: BLE001
            self.reason_code = classify_phone_login_exception(ex)
            self.detail = f"重发失败：{ex}"
            logger.info("[tg_phone_login] resend_code 失败 reason=%s", self.reason_code)
        return self.result()

    async def submit_password(self, password: str) -> Dict[str, Any]:
        """两步验证：提交云密码（与 QR 侧同语义）。仅 password_needed 有效；密码错可重试。"""
        if self.status != "password_needed":
            return self.result()
        if not password:
            self.detail = "云密码为空"
            return self.result()
        try:
            user = await self.client.check_password(password)
            await self._finish_with_user(user)
        except Exception as ex:  # noqa: BLE001
            if _is_bad_password(ex):
                self.detail = "云密码错误，请重新输入"
                logger.info("[tg_phone_login] 两步验证密码错误，可重试")
            else:
                self.status = "failed"
                self.detail = f"两步验证失败：{ex}"
                logger.error("[tg_phone_login] check_password 失败 type=%s: %s",
                             type(ex).__name__, ex, exc_info=True)
                await self._safe_disconnect(remove_session=True)
        return self.result()

    async def cancel(self) -> None:
        await self._safe_disconnect(remove_session=(self.status != "authorized"))

    # ── 内部 ──────────────────────────────────────────────────────────────
    async def _finish_with_user(self, user: Any) -> None:
        """统一收尾（与 QR 侧同形，便于下一层共用落库）：抽身份 + 导出 session_string + 落盘。"""
        try:
            self.account_id = str(getattr(user, "id", "") or "")
            self.phone = str(
                getattr(user, "phone_number", "") or getattr(user, "phone", "")
                or self.phone or "")
            try:
                from src.integrations.account_self_profile import extract_self_profile
                self.self_profile = extract_self_profile(user)
            except Exception:  # noqa: BLE001
                self.self_profile = {}
            try:
                await self.client.storage.user_id(user.id)
                await self.client.storage.is_bot(False)
            except Exception:  # noqa: BLE001
                logger.debug("[tg_phone_login] 写 storage user_id 失败（忽略）", exc_info=True)
            self.status = "authorized"
            self.detail = ""
            self.reason_code = ""
            try:
                self.session_string = str(await self.client.export_session_string() or "")
            except Exception:  # noqa: BLE001
                self.session_string = ""
                logger.info("[tg_phone_login] 导出 session_string 失败"
                            "（该号将依赖文件 session 恢复）", exc_info=True)
        except Exception as ex:  # noqa: BLE001
            self.status = "failed"
            self.detail = f"完成登录失败：{ex}"
            logger.error("[tg_phone_login] finish 失败 type=%s: %s",
                         type(ex).__name__, ex, exc_info=True)
        finally:
            # 保留 session 落盘（供编排器后续拉起）
            await self._safe_disconnect(remove_session=False)

    async def _safe_disconnect(self, *, remove_session: bool = False) -> None:
        try:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if remove_session:
                f = self.sessions_dir / f"{self.session_name}.session"
                try:
                    if f.exists():
                        f.unlink()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            logger.debug("[tg_phone_login] disconnect 清理失败", exc_info=True)


# ── 纯助手：号码归一化（大陆桌面用户常省略 +86）──────────────────────────────

# 大陆手机号（11 位、无国家码）——刻意比「1 开头 11 位」更窄：
# NANP 带国家码 ``1XXXXXXXXXX``（如 14155552671）也会 11 位且以 1 开头，
# 若一律补 +86 会把美加号打成错误前缀。号段表跟 MIIT 常见移动号段对齐，
# 宁可不补也不瞎猜（Telegram PHONE_NUMBER_INVALID 仍是最终权威）。
_CN_MOBILE_RE = re.compile(
    r"^1(?:3\d|4[5-9]|5[0-35-9]|6[2567]|7[0-8]|8\d|9[0-35-9])\d{8}$"
)


def normalize_phone(phone: str) -> str:
    """轻量 E.164 归一化：去空白/连字符；``00``→``+``；大陆手机号补 ``+86``。

    不做完整 libphonenumber——误伤成本高、依赖重；Telegram 侧 ``PHONE_NUMBER_INVALID``
    仍是最终权威。宁可不补前缀也不瞎猜非大陆号。
    """
    p = str(phone or "").strip().replace(" ", "").replace("-", "")
    if not p:
        return ""
    if p.startswith("00"):
        p = "+" + p[2:]
    if not p.startswith("+"):
        if p.isdigit() and _CN_MOBILE_RE.match(p):
            p = "+86" + p
        elif p.isdigit():
            p = "+" + p
    return p


def phone_enabled(config: Dict[str, Any]) -> bool:
    """``platform_login.telegram.phone_enabled`` 三态解析（桌面升级默认开）。"""
    from src.integrations.platform_login import resolve_login_switch
    return resolve_login_switch(config, "platform_login.telegram.phone_enabled")


# ── provider 工厂 + 注册（第 2 层：接线扫码侧同款落库/凭据池/指纹）────────────

_registered = False
_latest_config: Dict[str, Any] = {}


def _persist_authorized(
    login: "TelegramPhoneLogin",
    res: Dict[str, Any],
    *,
    cfg: Dict[str, Any],
    api_id: int,
    api_hash: str,
    alloc: Any,
    used_pool_key: str,
    device_seed: str,
    device_fp: Dict[str, str],
) -> None:
    """授权成功 → account_registry（**mode=protocol**：同 pyrogram session，编排器复用）。

    刻意不落 ``mode=phone``——那是登录向导的 UI 形态，不是运行时 worker 形态；
    写成 phone 会让编排器找不到 telegram/protocol 工厂，扫码登录的号反而「登上即掉线」。
    """
    if res.get("status") != "authorized" or not res.get("account_id"):
        return
    try:
        from src.integrations.account_registry import get_account_registry
        from src.integrations import credpool_bridge as _cp
        from src.integrations import device_fingerprint as _fp

        _meta: Dict[str, Any] = {
            "session_name": login.session_name,
            "phone": login.phone,
            "login_via": "phone_code",
        }
        if used_pool_key:
            _meta[_cp.META_KEY] = used_pool_key
            _cred = {"api_id": api_id, "api_hash": api_hash,
                     "tier": str(getattr(alloc, "tier", "") or "free")}
            if getattr(alloc, "proxy", None):
                _cred["proxy"] = dict(alloc.proxy)
            _meta[_cp.META_CRED_KEY] = _cred
            _cp.report(cfg, api_id, True, pool_key=used_pool_key)
        if device_seed:
            _meta[_fp.META_KEY] = device_seed
        if device_fp:
            _meta[_fp.META_FP_KEY] = dict(device_fp)
        if getattr(login, "session_string", ""):
            _meta["session_string"] = login.session_string
        try:
            from src.integrations.account_self_profile import self_profile_enabled
            if self_profile_enabled(cfg) and getattr(login, "self_profile", None):
                _meta.update(login.self_profile)
        except Exception:  # noqa: BLE001
            pass
        _prev = get_account_registry().get("telegram", res["account_id"]) or {}
        get_account_registry().upsert(
            "telegram", res["account_id"], mode="protocol",
            status="online", meta=_meta, merge_meta=True,
        )
        if used_pool_key:
            _old_key = str((_prev.get("meta") or {}).get(_cp.META_KEY) or "")
            if _old_key and _old_key != used_pool_key:
                try:
                    _cp.release(cfg, _old_key)
                except Exception:  # noqa: BLE001
                    pass
        try:
            from src.ai.persona_voice import ensure_account_default_persona
            ensure_account_default_persona(
                get_account_registry(), "telegram", res["account_id"], cfg)
        except Exception:  # noqa: BLE001
            pass
        if (not used_pool_key and getattr(alloc, "source", "") != "credpool"
                and (cfg.get("telegram") or {}).get("_hosted_cred")):
            try:
                _hp = (cfg.get("telegram") or {}).get("_hosted_proxy")
                _cp.remember_cred(
                    {"platform": "telegram",
                     "account_id": res["account_id"], "meta": {}},
                    _cp.Allocation(int(api_id), str(api_hash), "config", "free",
                                   dict(_hp) if isinstance(_hp, dict) else None))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.debug("[tg_phone_login] 注册表写入失败", exc_info=True)


def make_provider(
    config: Dict[str, Any],
    sessions_dir: str = _DEFAULT_SESSIONS_DIR,
    *,
    config_getter: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Callable[..., Any]:
    """构造 telegram:phone provider（签名对齐扫码侧，供 login/start 调用）。"""
    from src.integrations.telegram_protocol_login import (
        _to_pyrogram_proxy,
        is_pyrogram_available,
    )

    def _live(fallback: Dict[str, Any]) -> Dict[str, Any]:
        if config_getter is not None:
            try:
                live = config_getter()
                if isinstance(live, dict) and live:
                    return live
            except Exception:  # noqa: BLE001
                pass
        return fallback

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        cfg = _live(config)
        phone = normalize_phone(str((ctx or {}).get("phone") or ""))
        if not phone:
            return {
                "instruction": "请先填写手机号（含国际区号，如 +86…）。",
                "instruction_key": "inbox.connect.instr_tg_phone",
                "reason_code": "phone_invalid",
            }
        from src.integrations import credpool_bridge as _cp
        pool_key = _cp.new_pool_key() if _cp.credpool_enabled(cfg) else ""
        alloc = await _cp.aresolve_for_account(cfg, pool_key=pool_key)
        if alloc is None:
            return {"instruction": "未配置 Telegram api_id/api_hash，无法发起手机号登录。",
                    "reason_code": "creds_missing"}
        api_id, api_hash = alloc.api_id, alloc.api_hash
        used_pool_key = pool_key if alloc.source == "credpool" else ""
        proxy = (_to_pyrogram_proxy((ctx or {}).get("proxy"))
                 or _to_pyrogram_proxy(alloc.proxy))
        from src.integrations import device_fingerprint as _fp
        device_seed = _fp.new_device_seed() if _fp.enabled(cfg) else ""
        device_fp = _fp.client_kwargs(cfg, device_seed)
        try:
            from src.integrations.credpool_stats import get_credpool_stats
            get_credpool_stats().record_login(bool(device_fp))
        except Exception:  # noqa: BLE001
            pass

        login = TelegramPhoneLogin(
            api_id, api_hash, sessions_dir, proxy=proxy, device_kwargs=device_fp)
        await login.start(phone)

        # 与扫码侧同款：托管废凭据 → 举报换发 → 同轮重试一次（用户无感）
        if (login.status == "failed" and login.reason_code == "cred_invalid"
                and alloc.source != "credpool"):
            try:
                from src.ai import hosted_gateway as _hg
                swapped = _hg.report_invalid_and_refetch(cfg, str(api_id))
            except Exception:  # noqa: BLE001
                swapped = None
            if swapped:
                api_id, api_hash = swapped
                proxy = (_to_pyrogram_proxy((ctx or {}).get("proxy"))
                         or _to_pyrogram_proxy(
                             (cfg.get("telegram") or {}).get("_hosted_proxy")))
                retry = TelegramPhoneLogin(
                    api_id, api_hash, sessions_dir, proxy=proxy,
                    device_kwargs=device_fp)
                await retry.start(phone)
                login = retry

        # 换发也救不回来（或不适用换发）→ 处置元信息随 result() 下发（与 QR 侧同契约）
        if login.status == "failed" and login.reason_code == "cred_invalid":
            from src.integrations.telegram_protocol_login import cred_fail_meta
            _meta = cred_fail_meta(cfg, alloc)
            login.cred_source = str(_meta.get("cred_source") or "")
            login.retry_after_sec = int(_meta.get("retry_after_sec", -1))

        def _release_if_abandoned(reason: str) -> None:
            if not used_pool_key:
                return
            try:
                _cp.release(cfg, used_pool_key)
                logger.info("[tg_phone_login] 登录未完成（%s），已归还池容量", reason)
            except Exception:  # noqa: BLE001
                pass

        async def _poll(session: Any) -> Dict[str, Any]:
            res = login.result()
            _persist_authorized(
                login, res, cfg=cfg, api_id=api_id, api_hash=api_hash,
                alloc=alloc, used_pool_key=used_pool_key,
                device_seed=device_seed, device_fp=device_fp)
            if res.get("status") in ("expired", "failed"):
                _release_if_abandoned(res.get("status") or "terminal")
            return res

        async def _submit_code(session: Any, code: str) -> Dict[str, Any]:
            res = await login.submit_code(code)
            _persist_authorized(
                login, res, cfg=cfg, api_id=api_id, api_hash=api_hash,
                alloc=alloc, used_pool_key=used_pool_key,
                device_seed=device_seed, device_fp=device_fp)
            if res.get("status") == "failed":
                _release_if_abandoned("code_failed")
            return res

        async def _resend_code(session: Any) -> Dict[str, Any]:
            return await login.resend_code()

        async def _submit_password(session: Any, password: str) -> Dict[str, Any]:
            res = await login.submit_password(password)
            _persist_authorized(
                login, res, cfg=cfg, api_id=api_id, api_hash=api_hash,
                alloc=alloc, used_pool_key=used_pool_key,
                device_seed=device_seed, device_fp=device_fp)
            if res.get("status") == "failed":
                _release_if_abandoned("password_failed")
            return res

        async def _cancel(session: Any) -> None:
            await login.cancel()
            if getattr(login, "status", "") != "authorized":
                _release_if_abandoned("cancelled")

        out: Dict[str, Any] = {
            "instruction": "验证码已发到你的 Telegram App / 短信，请输入后继续。",
            "instruction_key": "inbox.connect.instr_tg_phone_code",
            "poll": _poll,
            "submit_code": _submit_code,
            "resend_code": _resend_code,
            "submit_password": _submit_password,
            "cancel": _cancel,
            "state": login,
            "status": login.status,
            "reason_code": login.reason_code,
            "detail": login.detail,
        }
        if login.cred_source:
            out["cred_source"] = login.cred_source
            out["retry_after_sec"] = int(login.retry_after_sec)
        # 开局就失败（号码坏/不可达/凭据废）仍带 poll，便于前端读终态；
        # 但 reason_code 也直接回，让 start 路由可立刻置 failed（无轮询窗口）。
        if login.status == "failed" and used_pool_key:
            _release_if_abandoned("start_failed")
        return out

    # 未装 pyrogram 时仍返回可调用对象，由 start 内路径降级（注册门槛另闸）
    _ = is_pyrogram_available
    return _provider


def maybe_register(
    config: Dict[str, Any], *, sessions_dir: str = _DEFAULT_SESSIONS_DIR
) -> bool:
    """按需注册 Telegram phone provider（幂等）。

    门槛：pyrogram + 凭据/池 + ``phone_enabled``。每次调用刷新 ``_latest_config``。
    """
    global _registered, _latest_config
    from src.integrations.platform_login import register_login_provider
    from src.integrations.telegram_protocol_login import (
        is_pyrogram_available,
        resolve_credentials,
    )

    if isinstance(config, dict) and config:
        _latest_config = config
    if _registered:
        return True
    if not phone_enabled(config):
        return False
    if not is_pyrogram_available():
        return False
    if resolve_credentials(config) is None:
        from src.integrations.credpool_bridge import credpool_enabled
        if not credpool_enabled(config):
            return False
    register_login_provider(
        "telegram", "phone",
        make_provider(config, sessions_dir,
                      config_getter=lambda: _latest_config))
    _registered = True
    logger.info("[tg_phone_login] Telegram phone 登录 provider 已注册")
    return True


__all__ = [
    "TelegramPhoneLogin",
    "classify_phone_login_exception",
    "normalize_phone",
    "phone_enabled",
    "make_provider",
    "maybe_register",
]
