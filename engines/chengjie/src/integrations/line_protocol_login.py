"""LINE 协议模式（web mode 等价）扫码登录 provider（M7）。

LINE 官方**没有可嵌入的完整网页聊天端**，但社区有等价 WhatsApp Baileys 的逆向协议库——
通过 **LINE Chrome 扩展网关**（``line-chrome-gw.line-apps.com``）直连，支持二维码登录 +
收发。本模块用 ``okline``（纯 Python，进程内跑，无需 Node 微服务）实现 LINE 的
「协议多开」登录，功能对齐官方：扫码登录、真实消息 id、通讯录/群、真实昵称头像。

架构（仿 Telegram pyrogram 的进程内 worker，而非 Baileys 的 Node 微服务）：
- 登录：``okline`` 的二维码流是回调驱动的阻塞长轮询（create_session→qr→长轮询扫码→PIN→
  长轮询确认→login_v2）。这里放到**后台线程**里驱动，把 QR/PIN/状态写进共享状态，
  provider 的 ``poll`` 只读状态——契合统一收件箱的「发起→轮询」模型；PIN 走
  ``pin_needed`` 状态 + 独立 ``pin`` 字段回给前端（用户必须在手机上输入它才算登录完成）。
- 成功：落 tokens 到 ``sessions/line/<mid>.json``、落 certificate 到 ``<mid>.cert``
  （下次复登带上它即可免 PIN）、写账号注册表、富集自身昵称/头像。
- 收发：见 ``account_orchestrator.LineProtocolWorker``（okline ``Bot`` 后台线程收消息 →
  protocol_bridge 落库 + 自动回复；``send_text`` 出站）。

落地约束（与 M2/M3 一致的谨慎姿态）：
- ``okline`` 为**非官方逆向库**，违反 LINE ToS、有封号风险、LINE 改网关/WASM 时可能失效；
  部分 E2EE(Letter Sealing) 私聊消息可能读不全。故默认**不启用**，需
  ``config.platform_login.line.protocol_enabled: true`` 且已 ``pip install okline`` 显式开启。
- 缺库 / 未开闸 → 不注册（前端「网页」方式灰显「未启用」），主进程零行为变化。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from typing import Any, Dict, Optional, Tuple

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider

logger = logging.getLogger(__name__)

_DEFAULT_SESSIONS_DIR = os.path.join("sessions", "line")
_registered = False


def is_okline_available() -> bool:
    try:
        import okline  # noqa: F401
        return True
    except Exception:
        return False


def protocol_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    ln = pl.get("line", {}) or {}
    return bool(ln.get("protocol_enabled", False))


# ── Node 运行时解析（okline 的 LTSM 桥要 node 才能算 X-Hmac）────────────────────
#
# okline 不是纯 Python：它起一个**持久 Node 子进程**加载 ltsm.wasm 算 X-Hmac 签名，
# 而网关对每个请求强制校验该签名。没有可用 node ⇒ 扫码失败，且登录后每一次收发也失败。
#
# 为什么钉 `LINE_NODE` 环境变量、而不是给 OkLine 传 node_path：okline 的消费方**不止
# 登录一处** —— `LineProtocolWorker.start` 用 `OkLine.from_tokens_file()` 起收发、
# 存量同步 `_sync_bootstrap_blocking` 与 peer 身份解析又各自另建实例，全都不带 config。
# 只有 env 这一层能一次覆盖全部（okline 自身取值序＝node_path → LINE_NODE → "node"）。
# 走 LineConfig 只能修好登录，之后条条消息仍会失败——那种"能登录但发不出"最难排查。
_NODE_ENV_KEY = "LINE_NODE"
_ELECTRON_AS_NODE_KEY = "ELECTRON_RUN_AS_NODE"
_node_runtime_cache: Optional[str] = None
_node_missing_logged = False


def reset_node_runtime_cache() -> None:
    """清进程内解析缓存（仅测试用）。"""
    global _node_runtime_cache, _node_missing_logged
    _node_runtime_cache = None
    _node_missing_logged = False


def _valid_node_candidate(path: str) -> str:
    """候选可用则回其绝对/可执行路径，否则空串。

    带路径分隔符的按文件存在判断；裸名（如 ``node``）走 PATH 查找。**坏值不致命**：
    调用方会跳过它继续找下一个候选——比把坏路径交给 okline 必败要好。
    """
    p = str(path or "").strip().strip('"')
    if not p:
        return ""
    if os.path.sep in p or (os.altsep and os.altsep in p):
        return p if os.path.isfile(p) else ""
    return shutil.which(p) or ""


def electron_node_candidate() -> str:
    """桌面壳注入的 Electron 可执行路径（配 ``ELECTRON_RUN_AS_NODE=1`` 即 Node 20 运行时）。"""
    return _valid_node_candidate(os.environ.get("AITR_ELECTRON_NODE") or "")


def configured_node_path(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    ln = pl.get("line", {}) or {}
    return str(ln.get("node_path") or "").strip()


def resolve_node_runtime(config: Dict[str, Any]) -> Tuple[str, str]:
    """解析该用哪个 node → ``(path, source)``；``path`` 空＝这台机器跑不了 LINE 协议。

    ``source`` ∈ ``config`` / ``env`` / ``path`` / ``electron`` / ``""``。顺序前者优先：

    1. ``platform_login.line.node_path``（运维显式指定，最高优先）
    2. 已设好的 ``LINE_NODE``（okline 自己的约定，尊重人的意图）
    3. PATH 上的**真 node** —— 正统运行时、行为最可预期，故优先于 Electron
    4. 随包 **Electron**（``AITR_ELECTRON_NODE``）—— 让没装 Node 的客户机也能用的回落，
       与 WhatsApp/Messenger 边车同款做法（见 desktop/backend-launcher.js）
    """
    explicit = _valid_node_candidate(configured_node_path(config))
    if explicit:
        return explicit, "config"
    env_node = _valid_node_candidate(os.environ.get(_NODE_ENV_KEY) or "")
    if env_node:
        return env_node, "env"
    real_node = shutil.which("node")
    if real_node:
        return real_node, "path"
    electron = electron_node_candidate()
    if electron:
        return electron, "electron"
    return "", ""


def ensure_node_runtime(config: Dict[str, Any]) -> str:
    """解析 node 运行时并**按需**钉进 ``os.environ``，返回可用路径（``""``＝没有）。

    只在必须时写 env（最小干预）：
    · PATH 上有真 node → 一个字都不写（okline 默认的 ``"node"`` 本来就对）；
    · 选中 Electron → 写 ``LINE_NODE`` + ``ELECTRON_RUN_AS_NODE``。后者是 Electron
      「变身纯 Node」的开关，**漏了它 Electron 会去开 GUI 窗口而不是跑桥**；okline 的
      ``Popen`` 用 ``env=dict(os.environ)``，所以设在本进程即可传下去；
    · 显式配置 / 修正坏 ``LINE_NODE`` → 写 ``LINE_NODE``。

    **只缓存成功**：命中后不再重复解析（免重复写 env / 刷日志）；失败**不缓存**——
    解析本身只是一次 ``which`` + 几次 ``isfile``，很便宜，而不缓存能让「事后把 node 装进
    一个已在 PATH 里的目录」这种情形被下一次探测直接捡到，不必重开应用。
    """
    global _node_runtime_cache, _node_missing_logged
    if _node_runtime_cache:
        return _node_runtime_cache
    path, source = resolve_node_runtime(config)
    if path and source == "electron":
        os.environ[_NODE_ENV_KEY] = path
        os.environ[_ELECTRON_AS_NODE_KEY] = "1"
        logger.info("[line_protocol] 无系统 Node，改用随包 Electron 作 LINE 的 Node 运行时: %s", path)
    elif path and source == "config":
        os.environ[_NODE_ENV_KEY] = path
    elif path and source == "path":
        # env 里残留一个**坏** LINE_NODE 时 okline 会优先用它而必败 → 用真 node 纠正。
        stale = os.environ.get(_NODE_ENV_KEY)
        if stale and not _valid_node_candidate(stale):
            logger.warning("[line_protocol] LINE_NODE 指向不存在的路径（%s），改用 PATH 上的 node", stale)
            os.environ[_NODE_ENV_KEY] = path
    if not path:
        if not _node_missing_logged:  # 只喊一次，别把每次就绪轮询都刷成 WARNING
            logger.warning("[line_protocol] 找不到可用的 Node 运行时 —— LINE 协议扫码/收发将不可用")
            _node_missing_logged = True
        return ""
    _node_runtime_cache = path
    return path


def is_node_available(config: Dict[str, Any]) -> bool:
    """这台机器能否跑 okline 的 LTSM 桥（就绪诊断用）。"""
    return bool(ensure_node_runtime(config))


def sessions_dir(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    ln = pl.get("line", {}) or {}
    return str(ln.get("sessions_dir") or _DEFAULT_SESSIONS_DIR)


def tokens_path(config: Dict[str, Any], mid: str) -> str:
    return os.path.join(sessions_dir(config), f"{mid}.json")


def cert_path(config: Dict[str, Any], mid: str) -> str:
    return os.path.join(sessions_dir(config), f"{mid}.cert")


def load_certificate(config: Dict[str, Any], mid: str) -> str:
    """读取上次登录留下的 LINE 证书；读不到一律返回空串（调用方据此决定传不传 kwarg）。"""
    if not mid:
        return ""
    try:
        with open(cert_path(config, mid), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def classify_login_error(text: str) -> str:
    """把 okline / 网关抛出的原始异常文本归类为 ``reason_code`` 枚举之一。

    纯函数：原文只进日志，前端拿 code 出本地化文案——既不给用户看英文 thrift 路径，
    也不暴露我们在走逆向协议。判定按「具体 → 泛化」排序，例如 PIN 超时的原文同时含
    ``pin`` 与 ``expired``，必须先于笼统的二维码过期/网络规则命中。
    """
    t = str(text or "").strip().lower()
    if not t:
        return "login_failed"
    if "pin" in t and any(k in t for k in ("timeout", "timed out", "expired", "not verified")):
        return "pin_timeout"
    if "qr code has expired" in t or "code=100" in t or "expired" in t:
        return "qr_expired"
    if any(k in t for k in ("429", "rate limit", "too many", "flood")):
        return "rate_limited"
    if any(k in t for k in ("timeout", "timed out", "connection", "network",
                            "unreachable", "ssl", "dns")):
        return "network"
    return "login_failed"


def _qr_data_uri(qr_url: str) -> str:
    """把 QR 回调 URL 渲染成二维码图片 data URI（前端弹窗直接显示）。"""
    if not qr_url:
        return ""
    try:
        import base64
        import io
        import qrcode
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("[line_protocol] 二维码渲染失败", exc_info=True)
        return ""


def line_picture_url(picture_path: str) -> str:
    """把 LINE ``picturePath`` 拼成可直接渲染的 obs CDN 头像 URL（已是 http 则原样；空→空）。

    纯函数（OBS_BASE 取不到时回落硬编码域名），供 self_profile 与 peer 头像解析共用、便于单测。
    LINE obs 直链是内容寻址的稳定 URL（无 scontent 那种时效 token）→ 可直接落库 ``avatar_url``
    由前端 priority ① 渲染，无需下载落 /static 代理（与 messenger 的会过期直链不同）。
    """
    pic = str(picture_path or "").strip()
    if not pic:
        return ""
    if pic.startswith("http"):
        return pic
    try:
        from okline import OBS_BASE
        return str(OBS_BASE).rstrip("/") + "/" + pic.lstrip("/")
    except Exception:
        return "https://obs.line-scdn.net/" + pic.lstrip("/")


def _self_profile_fields(client: Any) -> tuple[str, str]:
    """best-effort 读取账号自身昵称/头像 URL（供 self_profile 富集）。"""
    try:
        prof = client.get_profile()
        d = prof if isinstance(prof, dict) else getattr(prof, "raw", {}) or {}
        name = str(d.get("displayName") or getattr(prof, "display_name", "") or "")
        pic = str(d.get("picturePath") or getattr(prof, "picture_path", "") or "")
        return name, line_picture_url(pic)
    except Exception:
        logger.debug("[line_protocol] get_profile 失败", exc_info=True)
        return "", ""


def _kick_orchestrator() -> None:
    """登录成功后立刻催一次编排器巡检，让新号的 worker 马上起来。

    不催的话要等下一轮 15s 巡检才 ``start_account``，再叠加前端账号列表 20~45s 轮询
    ——用户侧表现＝「登录成功了但图标要一分钟才变蓝」（2026-07-25 真机反馈）。

    刻意用 ``create_task`` 不 await：``sync()`` 会顺带拉起其它待起 worker（Telegram 的
    ``start`` 要联网握手，秒级），await 会把这次 2.5s 一轮的登录轮询请求拖住。也刻意用
    ``get_orchestrator_if_running``——绝不在这里以空配置误建单例遮蔽 app 的真实实例。
    """
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is None:
            return
        task = asyncio.create_task(orch.sync())
        # 加个回调把异常读掉，否则 task 被 GC 时抛 "exception was never retrieved"
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except Exception:  # noqa: BLE001
        logger.debug("[line_protocol] 催巡检失败（等 15s 兜底）", exc_info=True)


def _discard_certificate(config: Dict[str, Any], account_id: str, used_cert: str) -> None:
    """带证书登录失败 → 删掉它，下一轮走干净的 PIN 流程。

    代价不对称：坏证书留着 = 每次复登都带同一张坏牌，用户怎么刷新都登不上（要运维手工
    删文件才能解），而误删 = 最多多输一次 PIN。所以不做「是不是证书的锅」的启发式判断，
    只要这轮带了证书又失败就丢弃。
    """
    if not used_cert or not account_id:
        return
    try:
        path = cert_path(config, account_id)
        if os.path.exists(path):
            os.remove(path)
            logger.info("[line_protocol] 带证书登录失败，已丢弃证书 account=%s（下轮走 PIN）", account_id)
    except Exception:  # noqa: BLE001
        logger.debug("[line_protocol] 丢弃证书失败", exc_info=True)


def _dump_login_transcript(client: Any, config: Dict[str, Any], tag: str) -> str:
    """登录失败时把 okline 的协议 transcript 落盘，给下次事故留第一现场。

    okline 的 recorder 记了每一次 thrift 往返；只有落盘才能回答「PIN 几点签发的 /
    checkPinCodeVerified 每轮返回什么 / 到底是网关超时还是服务端拒绝」这类问题——
    靠 app.log 里一行 warning 永远说不清（2026-07-25 排障全程都在靠推理补这段盲区）。
    ``redact=True`` 掩掉 token/X-Hmac；只在失败路径调用，正常登录不产生文件。
    """
    try:
        d = os.path.join(sessions_dir(config), "_diag")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"login_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.log")
        client.save_log(path, fmt="text", redact=True)
        return path
    except Exception:  # noqa: BLE001
        return ""


def _mark_failed(state: Dict[str, Any], reason_code: str) -> None:
    """写失败终态：原文不外泄，前端按 ``reason_code`` 出本地化文案。"""
    # 用户主动取消已经写过终态，别让随后 qr_login 抛出的连接错误把它改写成故障
    if str(state.get("reason_code") or "") == "cancelled":
        return
    state["status"] = "failed"
    state["reason_code"] = reason_code
    state["detail"] = ""


def _drive_qr_login(client: Any, state: Dict[str, Any], config: Dict[str, Any],
                    account_id: str = "") -> None:
    """在后台线程里驱动 okline 的二维码登录流，把 QR/PIN/状态写进 ``state``（同步、阻塞）。

    ``account_id`` 为重连场景的既有 mid：能读到它上次的证书就带上，okline 内部
    ``verifyCertificate`` 通过后 ``need_pin=False``，整个 PIN 环节被跳过。

    单测可直接调用本函数（传 fake client），无需起线程。
    """
    def on_qr(url: str) -> None:
        state["qr_url"] = url
        state["qr_image"] = _qr_data_uri(url)
        state["status"] = "pending"

    def on_pin(pin: str) -> None:
        state["pin"] = str(pin or "")
        state["status"] = "pin_needed"
        # PIN 只走独立字段：detail 会被后续阶段的失败文案覆盖，塞这里会在第二段轮询时丢失
        state["detail"] = "请在手机 LINE 上输入验证码"

    try:
        kwargs: Dict[str, Any] = {
            "on_qr": on_qr,
            "on_pin": on_pin,
            # okline 把这份预算在「等扫码」「等 PIN」两阶段各消耗一轮，而用户真人操作
            # （进设置、扫码、手机上输 6 位并勾确认）常超 platform_login.TTL_SEC(180)；
            # TTL 那侧已对 pin_needed 豁免过期，故这里放宽到 240。
            "wait_seconds": 240.0,
        }
        cert = load_certificate(config, account_id)
        if cert:
            kwargs["certificate"] = cert
        try:
            try:
                result = client.qr_login(**kwargs)
            except TypeError as ex:
                # 装的 okline 版本不认 certificate 形参：摘掉重试一次。免 PIN 只是优化，
                # 不能让它把整条登录打死（降级路径 == 老行为，用户多输一次 PIN 而已）。
                if not cert or "certificate" not in str(ex):
                    raise
                logger.warning("[line_protocol] okline 不支持 certificate，回落常规扫码: %s", ex)
                kwargs.pop("certificate", None)
                result = client.qr_login(**kwargs)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[line_protocol] qr_login 失败: %s", ex)
            diag = _dump_login_transcript(client, config, "qrfail")
            if diag:
                logger.warning("[line_protocol] 协议 transcript 已落盘: %s", diag)
            _discard_certificate(config, account_id, cert)
            _mark_failed(state, classify_login_error(str(ex)))
            return

        mid = str(getattr(result, "mid", "") or "")
        if not getattr(result, "success", False) or not mid:
            display_message = str(getattr(result, "display_message", "") or "login failed")
            logger.warning("[line_protocol] qr_login 未成功: %s", display_message)
            diag = _dump_login_transcript(client, config, "notok")
            if diag:
                logger.warning("[line_protocol] 协议 transcript 已落盘: %s", diag)
            _discard_certificate(config, account_id, cert)
            _mark_failed(state, classify_login_error(display_message))
            return

        state["mid"] = mid
        try:
            os.makedirs(sessions_dir(config), exist_ok=True)
            client.save_tokens(tokens_path(config, mid))
        except Exception:  # noqa: BLE001
            logger.debug("[line_protocol] save_tokens 失败", exc_info=True)
        certificate = str(getattr(result, "certificate", "") or "")
        if certificate:
            try:
                os.makedirs(sessions_dir(config), exist_ok=True)
                with open(cert_path(config, mid), "w", encoding="utf-8") as f:
                    f.write(certificate)
            except Exception:  # noqa: BLE001
                logger.debug("[line_protocol] 证书落盘失败（下次仍走 PIN）", exc_info=True)
        name, avatar = _self_profile_fields(client)
        state["name"] = name
        state["avatar_url"] = avatar
        # 清掉 PIN 引导语，否则登录成功后前端还挂着「请输入验证码」
        state["detail"] = ""
        state["status"] = "authorized"
    finally:
        # 成功的客户端会被 LineProtocolWorker 接手继续收发，close() 等于把刚登上的账号
        # 踢下线；只有未成功（失败/取消/异常）时才收连接，否则每次刷新二维码都泄漏一条
        # HTTPS 长连 + WASM bridge + 杀不掉的守护线程。
        if str(state.get("status") or "") != "authorized":
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# QR 登录 HTTP read timeout：必须远大于单轮长轮询窗口 X-LST（interval*1000，实测 30s），
# 否则与网关 hold 同时到期 → requests 竞态抢先抛 ReadTimeout → okline _poll 不认（非 408/410）
# → 整条扫码登录随机暴毙。120s 覆盖 interval 到 ~100s；只作用于一次性登录实例。
# 服务端 createQrCode 实测回 longPollingIntervalSec=150（X-LST=150000），HTTP read
# timeout 必须显著高于它，否则长轮询没等到结果就被本地掐断。上一版取 120 曾与服务端
# 120.3s 的过期响应擦肩而过（transcript #6），故留足缓冲。
_LOGIN_HTTP_TIMEOUT_SEC = 200.0


# ⚠ 别再「优化」掉 okline qr_login 里那次空证书 verifyCertificate。
# 它看着像一次多余的试探（首登证书为空，服务端必回 code=2「验证码不正确」），实则是
# 服务端状态机的必需一步：2026-07-25 曾把它拦下不发，结果紧随其后的 createPinCode
# 直接 code=100「QR code has expired」，连 PIN 都签不出来（transcript 实证，已回滚）。
# 门禁 test_empty_certificate_probe_must_not_be_skipped 钉住这个行为。


def _build_okline(okline_cls: Any) -> Any:
    """构造登录用 OkLine，把 HTTP read timeout 抬到远大于 QR 长轮询窗口（消除竞态）。

    okline 2.7.0 的 ``qr_check_verified`` / ``qr_check_pincode_verified`` 走 X-LST 长轮询
    （网关 hold ``interval*1000`` ms，实测 30~150s 不等），但底层 HTTP read timeout 用
    ``LineConfig.timeout`` 默认 30.0——两者相等即竞态：网络稍慢 requests 先抛
    ``Read timed out``，而它不是 okline ``_poll`` 认的 408/410 翻页信号 → 不重试
    → 整条扫码登录在任意一轮长轮询上随机暴毙（用户根本进不了 PIN，2026-07-25 实录事故）。
    抬高 read timeout 后，长轮询靠网关正常 408/410 翻页续等，扫码确认阶段不再随机死。

    只作用于这个一次性登录实例；登录成功后收发交给 ``LineProtocolWorker`` 的独立实例
    （沿用默认超时，不受影响）。okline 若换构造签名 → 回落无参构造（退回老行为，
    不因这一优化把登录整条打死）。

    只动 timeout：qr_login 的其余步骤（含那次看似多余的空证书 verifyCertificate）都是
    协议必需的，不要在这里加"聪明"的拦截。
    """
    try:
        from okline.transport import LineConfig
        return okline_cls(config=LineConfig(timeout=_LOGIN_HTTP_TIMEOUT_SEC))
    except Exception:  # noqa: BLE001
        logger.debug("[line_protocol] 自定义 LineConfig 失败，回落默认构造", exc_info=True)
        return okline_cls()


def make_provider(config: Dict[str, Any]):

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        if not is_okline_available():
            # 面向用户的话术由前端按 reason_code 本地化；这里不点库名（无谓暴露走的是逆向协议），
            # 真正的安装指引留在 logger 与文档里给运维看。
            logger.warning("[line_protocol] 未安装 okline，LINE 协议登录不可用（pip install okline）")
            return {"instruction": "", "reason_code": "okline_missing"}
        try:
            from okline import OkLine
            client = _build_okline(OkLine)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[line_protocol] 初始化 OkLine 失败: %s", ex)
            return {"instruction": "", "reason_code": "client_init"}

        state: Dict[str, Any] = {
            "status": "pending", "qr_url": "", "qr_image": "", "pin": "",
            "mid": "", "name": "", "avatar_url": "", "detail": "",
            "reason_code": "", "_persisted": False,
        }
        t = threading.Thread(
            target=_drive_qr_login, args=(client, state, config, account_id), daemon=True)
        t.start()
        # 等首个二维码（最多 ~8s）
        deadline = time.time() + 8.0
        while not state["qr_image"] and state["status"] == "pending" and time.time() < deadline:
            time.sleep(0.2)

        async def _poll(session: Any) -> Dict[str, Any]:
            st = str(state.get("status") or "pending")
            mid = str(state.get("mid") or "")
            if st == "authorized" and mid and not state.get("_persisted"):
                state["_persisted"] = True
                try:
                    # merge_meta：防重登录整块覆盖 meta 抹掉 persona_id 等绑定
                    get_account_registry().upsert(
                        "line", mid, mode="protocol", status="online",
                        meta={"tokens_path": tokens_path(config, mid)},
                        merge_meta=True)
                    try:
                        from src.ai.persona_voice import ensure_account_default_persona
                        ensure_account_default_persona(
                            get_account_registry(), "line", mid, config)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    logger.debug("[line_protocol] 注册表写入失败", exc_info=True)
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "line", mid, name=str(state.get("name") or ""),
                        avatar_url=str(state.get("avatar_url") or ""), config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[line_protocol] self_profile 富集失败（忽略）", exc_info=True)
                _kick_orchestrator()
            # pin/reason_code 只在各自状态下回填：PIN 属一次性凭据，登录成功后不该还留在响应里
            return {"status": st, "account_id": mid,
                    "detail": str(state.get("detail") or ""),
                    "pin": (str(state.get("pin") or "") if st == "pin_needed" else ""),
                    "reason_code": (str(state.get("reason_code") or "") if st == "failed" else ""),
                    "qr_image": ("" if st == "authorized" else str(state.get("qr_image") or ""))}

        async def _cancel(session: Any) -> None:
            # 已授权的会话不接受取消：客户端已交给 LineProtocolWorker 收发，close() 等于
            # 把刚上线的号踢下线。出货副驾（cp-accounts.js）就在 authorized 分支里调
            # _stopPoll()→cancelLogin，这里兜住任何「登录成功后又取消」的调用方。
            if str(state.get("status") or "") == "authorized":
                return
            # daemon 线程无法强杀；置 failed 让其 qr_login 超时后自然结束。
            _mark_failed(state, "cancelled")
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        return {
            "qr_image": str(state.get("qr_image") or ""),
            "instruction": "用手机 LINE 扫码：设置 → 我的账户 →「用其他设备登录 / 登录中的设备」出示二维码，"
                           "用主设备 LINE 扫描；如提示 PIN，请在手机上输入弹出的数字。",
            "poll": _poll,
            "cancel": _cancel,
            "state": state,
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 LINE protocol provider（幂等）。

    仅当 ``protocol_enabled: true`` 且已安装 okline 时注册。
    """
    global _registered
    if _registered:
        return True
    if not protocol_enabled(config):
        return False
    if not is_okline_available():
        logger.warning("[line_protocol] protocol_enabled=true 但未安装 okline，跳过注册")
        return False
    # 先把 Node 运行时钉好（登录链与 worker 共用同一 env）。**缺 node 也照样注册**：
    # 不注册会让界面显示笼统的「未启用」，而就绪诊断能给出「缺组件 Node.js 18+ / 去装」
    # 这种可照做的话——把可用性判断交给诊断层，别在这里把原因抹平。
    ensure_node_runtime(config)
    register_login_provider("line", "protocol", make_provider(config))
    _registered = True
    logger.info("[line_protocol] LINE protocol 登录 provider 已注册")
    return True
