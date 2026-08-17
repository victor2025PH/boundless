"""平台扫码登录会话管理（P3）。

统一收件箱「账号管理 → ＋ 扫码新增 / 重连」对应的后端。本模块**不直接驱动**各
平台底层登录（LINE/WhatsApp/Messenger 为手机设备 RPA，登录二维码显示在被控设备/
投屏端；Telegram 走 pyrogram 多账号），而是提供一个**安全、可插拔**的登录会话层：

- ``LoginManager``：内存登录会话（带 TTL），记录发起时该平台「已在线账号」基线。
- 轮询时通过适配器的实时状态对比基线，**检测到新账号上线即判定 authorized**——
  即无论扫码在设备端还是网页端完成，账号真正连上后前端弹窗会自动转「登录成功」。
- ``register_login_provider``：预留真实 per-platform QR provider 的注册点（如未来为
  Telegram 接入 pyrogram ExportLoginToken 网页二维码），核心轮询逻辑无需改动。
- ``HOLD_STATES``（等云密码 / 等用户输 PIN）豁免常规 TTL：此时连接已建立，用户正在
  另一端输入，判过期会把整轮流程作废；但仍受 ``HOLD_MAX_SEC`` 封顶，否则用户扫到一半
  关页面就会永久滞留一条会话（连带底层长连与守护线程）直到进程重启。

设计原则：只读各服务的 ``status()``，**绝不触碰正在运行的客户端**，零副作用。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

# 登录会话有效期（秒）：二维码/等待窗口超过即过期，前端可「刷新」重开
TTL_SEC = 180

# 这些状态下连接已建立、正等用户在另一端完成输入（云密码 / LINE 的 6 位 PIN），
# 判过期会误清会话导致前功尽弃——用户读码、切 App、手输的耗时经常超过 TTL_SEC。
HOLD_STATES = frozenset({"password_needed", "pin_needed"})

# hold 只是「豁免常规 TTL」，不是「永不回收」：用户扫到一半关掉页面是常态，而每个滞留
# 会话都挂着一条 okline 长连 + 杀不掉的守护线程，不封顶就是按放弃次数线性泄漏到重启为止。
# 上限取 provider 的 wait_seconds(240s) 之上留足余量——超过它，底层长轮询早已自行超时，
# 会话再留着也不可能走到 authorized。
HOLD_MAX_SEC = 600

SUPPORTED_PLATFORMS = ("telegram", "line", "whatsapp", "messenger", "discord", "web")

# 登录方式（mode）：协议多开 / 网页隔离 / 真机RPA
MODES = ("protocol", "web", "device")
MODE_LABELS: Dict[str, str] = {
    "protocol": "协议多开",
    "web": "网页扫码",
    "device": "真机 / 模拟器",
}
MODE_DESC: Dict[str, str] = {
    "protocol": "服务端协议直连，单机可挂大量账号，最省资源（推荐）",
    "web": "隔离浏览器 + 平台网页二维码，兼容好、更像真人",
    "device": "真机 / 模拟器扫码，最难封号，账号数受设备数限制",
}
# 上面两张表是**后端兜底**文案；前端优先用下面的 i18n 键取本地化文案（英文坐席看到的
# 不该是中文模式名）。键值缺失时前端自动回落 label/desc。
MODE_LABEL_KEYS: Dict[str, str] = {
    "protocol": "inbox.connect.mode_l_protocol",
    "web": "inbox.connect.mode_l_web",
    "device": "inbox.connect.mode_l_device",
}
MODE_DESC_KEYS: Dict[str, str] = {
    "protocol": "inbox.connect.mode_d_protocol",
    "web": "inbox.connect.mode_d_web",
    "device": "inbox.connect.mode_d_device",
}
# 能力标签（前端渲染成 chips → i18n 键 inbox.connect.cap_<name>）：让运营不必读技术描述
# 就能横向比较「能挂几个号 / 要不要买手机 / 要不要运维」。
MODE_CAPS: Dict[str, tuple] = {
    "protocol": ("multi", "light"),
    "web": ("compat", "human"),
    "device": ("antiban", "need_device"),
}

# 不可用原因码（结构化，前端按码取本地化解释与处置建议；文本仅作后端兜底）
REASON_NOT_ENABLED = "not_enabled"
REASON_NEEDS_SERVER_SETUP = "needs_server_setup"
REASON_TEXTS: Dict[str, str] = {
    REASON_NOT_ENABLED: "开发中 / 未启用",
    REASON_NEEDS_SERVER_SETUP: "需服务器端配置后启用",
}

# 平台级覆盖：同一个 mode 名在不同平台可能是**完全不同的东西**。典型如 Messenger 的 web——
# Facebook 网页端并没有 WhatsApp 那种「手机扫码配对设备」，其二维码只用于加好友；登录必须在
# 服务器的隔离浏览器里用账密 / 2FA 完成。沿用通用的「网页扫码」文案，坐席只会对着弹窗干等一个
# 永远不会出现的码（本条即该故障的修复点）。
PLATFORM_MODE_OVERRIDES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "messenger": {
        "web": {
            "label": "服务器托管登录",
            "desc": "在服务器的隔离浏览器内完成 Facebook 官方登录（账密 / 2FA），不使用二维码",
            "label_key": "inbox.connect.mode_l_msg_web",
            "desc_key": "inbox.connect.mode_d_msg_web",
            "caps": ("server", "human", "need_ops"),
            "unavailable_reason_code": REASON_NEEDS_SERVER_SETUP,
        },
    },
    # Discord 复用 protocol 这条 mode 通道（编排器/前端无需认识新 mode），但语义完全
    # 不同：不是「协议多开」而是**官方 Bot 应用**——填 Token 而非扫码，且能力上限由
    # 平台规则封顶（不能主动私聊陌生人）。文案必须改写，否则运营会按「协议号」的心智
    # 去用，第一件事就是主动群发，然后收获一屏 403。
    "discord": {
        "protocol": {
            "label": "官方 Bot 接入",
            "desc": "填入 Discord Bot Token 接入（需在开发者后台开启 MESSAGE CONTENT "
                    "INTENT）。Bot 只能被动接待：对方需与 Bot 有共同服务器或先主动私信。",
            "caps": ("official", "light"),
            "credential": {
                "field": "bot_token",
                "label": "Bot Token",
                "label_key": "inbox.connect.cred_l_bot_token",
                "help": "Discord 开发者后台 → 你的应用 → Bot → Reset Token 复制整串；"
                        "同页需开启 MESSAGE CONTENT INTENT，否则 Bot 收不到消息正文。",
                "help_key": "inbox.connect.cred_h_discord",
                "secret": True,
                "optional_if_configured": True,
            },
        },
    },
}

# ``credential`` 的语义（前端按它通用渲染，勿写成 if platform==='discord'）：
#
# 该 mode 不是「把一个人的账号连进来」，而是**服务端 API 凭据接入**。三条推论对前端
# 是硬约束，都由这一个字段推出，不再另设开关：
#
# 1. **没有二维码**——接入是一次同步校验（打平台 API 验 token），没有「等手机扫」的
#    等待期。前端必须隐藏码位与「刷新二维码」，否则坐席对着一个永不出现的码干等
#    （Messenger 那次的同类事故，见上面 PLATFORM_MODE_OVERRIDES 的注释）。
# 2. **没有防关联配置**——代理/指纹是给「一号一 IP、伪装成真人设备」用的；Bot 走官方
#    API、身份就是明牌，摆出这两栏等于让运营配一堆**代码根本不读**的东西。
# 3. **凭据即身份**——``field`` 是随 ``login/start`` 上送的请求体键名（后端白名单接收），
#    ``secret`` 决定输入框类型与「绝不回显」；``optional_if_configured`` 表示配置里已有
#    同名凭据时可留空沿用（前端据此决定要不要拦「必填」）。
#
# 反过来说：**扫码/账密类 mode 一律不要声明 credential**，否则会被前端当成 API 凭据接入
# 而把二维码藏掉。
CREDENTIAL_MODE_HINT = "credential"

# 每平台默认可选方式与默认方式（可被 config.platform_login.<platform> 覆盖）
DEFAULT_PLATFORM_MODES: Dict[str, Dict[str, Any]] = {
    "telegram": {"modes": ["protocol", "web", "device"], "default": "protocol"},
    "whatsapp": {"modes": ["protocol", "web", "device"], "default": "protocol"},
    "line": {"modes": ["protocol", "device"], "default": "protocol"},
    "messenger": {"modes": ["web", "device"], "default": "web"},
    "discord": {"modes": ["protocol"], "default": "protocol"},
    "web": {"modes": [], "default": ""},
}

# 各平台扫码指引（设备/投屏端为主，网页端 provider 可覆盖）
PLATFORM_INSTRUCTIONS: Dict[str, str] = {
    "telegram": (
        "在 Telegram 手机端：设置 → 设备 → 关联桌面设备，扫描二维码；"
        "或由管理员在设备端完成新账号登录。账号连上后本窗口会自动确认。"
    ),
    "line": (
        "在已连接的设备 / 投屏端打开 LINE 登录页并用手机扫码；"
        "登录成功后本窗口会自动确认。"
    ),
    "whatsapp": (
        "在设备 / 投屏端打开 WhatsApp「关联设备」并用手机扫码；"
        "成功后本窗口会自动确认。"
    ),
    "messenger": (
        "在设备 / 投屏端完成 Facebook 账号登录授权；成功后本窗口会自动确认。"
    ),
    "discord": (
        "Discord 无二维码登录：在开发者后台（discord.com/developers）建应用 → Bot → "
        "开启 MESSAGE CONTENT INTENT → Reset Token 复制，填入配置 "
        "platform_login.discord.bot_token，再点接入即可校验上线。"
    ),
    "web": "网页客服为服务端原生渠道，无需扫码登录。",
}


@dataclass
class LoginSession:
    login_id: str
    platform: str
    mode: str = "device"
    account_id: str = ""
    created_at: float = field(default_factory=time.time)
    # pending | scanned | pin_needed | password_needed | authorized | expired | failed
    # scanned = 等用户在手机上点确认（我方无信息要传递）；
    # pin_needed = 我方持有 PIN，等用户在手机上输入（LINE 协议登录）
    status: str = "pending"
    qr_url: str = ""          # 网页二维码可编码的 URL（如 tg://login?token=...）
    qr_image: str = ""        # data URI（可选，provider 直接给出二维码图片）
    instruction: str = ""
    detail: str = ""
    # failed 的机器可读原因（provider poll 落此，供 status 早退分支继续吐给前端本地化）
    reason_code: str = ""
    # 已上报过的漏斗分段：轮询每 2.5s 一次，不去重会把一次登录记成上百次
    funnel_marks: Set[str] = field(default_factory=set)
    baseline: Set[str] = field(default_factory=set)
    # M4：账号配置（防关联）—— 登录成功后随 account_id 一起落库
    label: str = ""
    group: str = ""
    proxy_id: str = ""
    fingerprint_id: str = ""
    # provider 事件驱动钩子（protocol/web provider 用；device 走基线轮询，留空）
    provider_state: Any = None
    poll_fn: Optional[Callable[..., Any]] = None
    cancel_fn: Optional[Callable[..., Any]] = None
    submit_fn: Optional[Callable[..., Any]] = None   # 两步验证云密码提交（Telegram protocol）

    def is_expired(self) -> bool:
        age = time.time() - self.created_at
        if self.status in HOLD_STATES:
            return age > HOLD_MAX_SEC
        return age > TTL_SEC


class LoginManager:
    """内存登录会话表（进程内，单实例）。线程安全。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = threading.Lock()

    def _gc_locked(self) -> None:
        now = time.time()
        dead = [
            k for k, s in self._sessions.items()
            if (now - s.created_at) > (HOLD_MAX_SEC if s.status in HOLD_STATES else TTL_SEC * 2)
        ]
        for k in dead:
            self._sessions.pop(k, None)

    def create(
        self,
        platform: str,
        account_id: str,
        baseline: Set[str],
        *,
        mode: str = "device",
        qr_url: str = "",
        qr_image: str = "",
        instruction: str = "",
        label: str = "",
        group: str = "",
        proxy_id: str = "",
        fingerprint_id: str = "",
        provider_state: Any = None,
        poll_fn: Optional[Callable[..., Any]] = None,
        cancel_fn: Optional[Callable[..., Any]] = None,
        submit_fn: Optional[Callable[..., Any]] = None,
    ) -> LoginSession:
        with self._lock:
            self._gc_locked()
            sid = secrets.token_urlsafe(12)
            sess = LoginSession(
                login_id=sid,
                platform=platform,
                mode=mode or "device",
                account_id=account_id or "",
                baseline=set(baseline or set()),
                qr_url=qr_url,
                qr_image=qr_image,
                instruction=instruction
                or PLATFORM_INSTRUCTIONS.get(platform, "请完成扫码登录。"),
                label=label,
                group=group,
                proxy_id=proxy_id,
                fingerprint_id=fingerprint_id,
                provider_state=provider_state,
                poll_fn=poll_fn,
                cancel_fn=cancel_fn,
                submit_fn=submit_fn,
            )
            self._sessions[sid] = sess
            return sess

    def get(self, login_id: str) -> Optional[LoginSession]:
        with self._lock:
            return self._sessions.get(login_id)

    def cancel(self, login_id: str) -> None:
        with self._lock:
            self._sessions.pop(login_id, None)


_manager: Optional[LoginManager] = None


def get_login_manager() -> LoginManager:
    global _manager
    if _manager is None:
        _manager = LoginManager()
    return _manager


def online_account_keys(
    status_map: Dict[str, Dict[str, Any]], platform: str
) -> Set[str]:
    """从 ``status_via_adapters`` 结果中取某平台「在线」账号的唯一键集合。"""
    keys: Set[str] = set()
    for k, v in (status_map or {}).items():
        if not isinstance(v, dict):
            continue
        if (v.get("platform") or "") != platform:
            continue
        if v.get("running"):
            keys.add(str(v.get("account_id") or k))
    return keys


# ── per-(platform, mode) QR provider 注册点 ──────────────────────────────────
# provider(request, platform, mode, account_id) -> dict|None
#   返回 {"qr_url"?, "qr_image"?, "instruction"?, "account_id"?} 覆盖默认指引；
#   返回 None 表示沿用「设备端扫码 + 状态轮询」。
#   key = f"{platform}:{mode}"。M2/M3 起为 telegram:protocol / whatsapp:protocol 注册真实 provider。
_PROVIDERS: Dict[str, Callable[..., Optional[Dict[str, Any]]]] = {}


def _pkey(platform: str, mode: str) -> str:
    return f"{str(platform).lower()}:{str(mode).lower()}"


def register_login_provider(
    platform: str, mode: str, provider: Callable[..., Optional[Dict[str, Any]]]
) -> None:
    _PROVIDERS[_pkey(platform, mode)] = provider


def get_login_provider(
    platform: str, mode: str = "device",
) -> Optional[Callable[..., Optional[Dict[str, Any]]]]:
    return _PROVIDERS.get(_pkey(platform, mode))


# ── 接入开关三态解析（P0：产品默认随程序版本走，不随只播一次的种子） ──────────
#
# 背景（104 事故）：LINE/WhatsApp/Messenger 的扫码开关写在 config.desktop.min.yaml
# 种子里，而 ConfigManager._ensure_seeded 只在配置文件不存在时播种一次。于是**升级
# 安装**（旧 config、缺后加的开关）永远拿不到新平台的默认开启——程序里 okline/边车/
# Electron-Node 都随包到位了，开关却停在旧配置，接入弹窗里对应方式恒灰「未启用」，
# 且除 Telegram 外界面上没有别的入口能打开。与 licensing.trial 同类（见
# local_trial.configure_local_trial 的同款修法）。
#
# 修法：把「产品该点亮哪些接入方式」这个**产品决策**从种子回归代码默认——桌面模式下、
# 且配置**完全没写过**该键时按下表默认开；配置一旦写了就完全以配置为准（含显式 false，
# 尊重管理员/运营的关闭意愿）。服务器部署无 AITR_DESKTOP_MODE → 一律保持原「默认关」
# 语义，零行为变化。用户资产（key/账号/overlay）仍只由配置承载，本机制只管产品开关。
#
# 表内每一项都必须在 config.desktop.min.yaml 有对应的「随包交付」承诺，且由
# tests/test_platform_login_defaults.py 双向钉住（种子开的必须在表内、表内的种子必须开），
# 防「种子加了新平台开关但忘了配代码默认」这条复发路径（正是 104 的成因类）。
_DESKTOP_LOGIN_DEFAULT_ON = frozenset({
    "platform_login.telegram.protocol_enabled",
    "platform_login.line.protocol_enabled",
    "platform_login.whatsapp.protocol_enabled",
    "platform_login.messenger.web_enabled",
    "platform_login.orchestrator_enabled",
})

_UNSET = object()


def _desktop_mode() -> bool:
    """是否桌面壳部署。与 ``local_trial.configure_local_trial`` 同口径——桌面壳
    launcher 经 ``AITR_DESKTOP_MODE=1`` 注入（见 desktop/backend-launcher.js）。"""
    return str(os.environ.get("AITR_DESKTOP_MODE") or "") == "1"


def _dotted_get(config: Dict[str, Any], path: str) -> Any:
    cur: Any = config if isinstance(config, dict) else {}
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _UNSET
        cur = cur[part]
    return cur


def resolve_login_switch(
    config: Dict[str, Any], path: str, *, desktop: Optional[bool] = None
) -> bool:
    """接入开关三态解析（单一事实源）。

    - 配置**显式写过**（含 ``false``）→ 完全以配置为准；
    - **从未写过** → 桌面模式且命中 ``_DESKTOP_LOGIN_DEFAULT_ON`` 表则 True，否则 False。

    ``path`` 为从 config 根算起的点分路径（如 ``platform_login.line.protocol_enabled``）。
    ``desktop`` 可注入以便单测；缺省读环境（``AITR_DESKTOP_MODE``）。
    """
    val = _dotted_get(config, path)
    if val is not _UNSET:
        return bool(val)
    if desktop is None:
        desktop = _desktop_mode()
    return bool(desktop and path in _DESKTOP_LOGIN_DEFAULT_ON)


def mode_available(platform: str, mode: str) -> bool:
    """该平台该方式是否可用。device 为内置（设备端扫码+状态轮询）始终可用；
    protocol/web 需注册了真实 provider 才可用。"""
    mode = str(mode).lower()
    if mode == "device":
        return True
    return get_login_provider(platform, mode) is not None


def list_modes(
    platform: str, platform_cfg: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """返回该平台的登录方式清单（供前端「选择登录方式」渲染）。

    ``platform_cfg`` 为 config.platform_login.<platform>（可选，覆盖默认 modes/default）。
    """
    platform = str(platform or "").lower()
    pdef = DEFAULT_PLATFORM_MODES.get(
        platform, {"modes": ["device"], "default": "device"}
    )
    cfg = platform_cfg or {}
    modes = cfg.get("modes") or pdef["modes"]
    default = cfg.get("default") or pdef["default"]
    overrides = PLATFORM_MODE_OVERRIDES.get(platform, {})
    out: List[Dict[str, Any]] = []
    for m in modes:
        avail = mode_available(platform, m)
        ov = overrides.get(m, {})
        rc = "" if avail else str(
            ov.get("unavailable_reason_code") or REASON_NOT_ENABLED)
        out.append({
            "mode": m,
            "label": str(ov.get("label") or MODE_LABELS.get(m, m)),
            "desc": str(ov.get("desc") or MODE_DESC.get(m, "")),
            "label_key": str(ov.get("label_key") or MODE_LABEL_KEYS.get(m, "")),
            "desc_key": str(ov.get("desc_key") or MODE_DESC_KEYS.get(m, "")),
            "caps": list(ov.get("caps") or MODE_CAPS.get(m, ())),
            "available": avail,
            # recommended = 该平台的**默认方式**（配置语义，与可用性正交，勿改）。
            "recommended": (m == default),
            # preferred = 给 UI 用的「主推」标记：默认方式 **且** 当前真的能用。
            # 二者曾被混为一谈 —— Messenger 的 web 同时是默认方式和不可用，界面便渲染出
            # 一个挂着绿色「推荐」角标却点不动、无任何反馈的选项。可用性是硬前提。
            "preferred": bool(avail and m == default),
            "reason_code": rc,
            "reason": REASON_TEXTS.get(rc, "") if rc else "",
            # 凭据式接入契约（见 CREDENTIAL_MODE_HINT 处的长注释）。非凭据式恒为 {}，
            # 前端据「空/非空」二分渲染：空=扫码流，非空=表单流（无码、无防关联配置）。
            "credential": dict(ov.get("credential") or {}),
        })
    return out


def first_available_mode(modes: List[Dict[str, Any]]) -> str:
    """从 list_modes 结果里挑一个能真正走通的方式：优先主推，其次任一可用。

    调用方原本取「第一个 recommended」，而默认方式可能恰好不可用（Messenger 即如此），
    于是回落到 modes[0] —— 那还是同一个不可用项，用户拿到的是一句无从下手的报错。
    """
    for m in modes:
        if m.get("preferred"):
            return str(m.get("mode") or "")
    for m in modes:
        if m.get("available"):
            return str(m.get("mode") or "")
    return str(modes[0].get("mode") or "") if modes else "device"
