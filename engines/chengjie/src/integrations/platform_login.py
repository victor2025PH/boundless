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
HOLD_STATES = frozenset({"password_needed", "pin_needed", "code_needed"})

# hold 只是「豁免常规 TTL」，不是「永不回收」：用户扫到一半关掉页面是常态，而每个滞留
# 会话都挂着一条 okline 长连 + 杀不掉的守护线程，不封顶就是按放弃次数线性泄漏到重启为止。
# 上限取 provider 的 wait_seconds(240s) 之上留足余量——超过它，底层长轮询早已自行超时，
# 会话再留着也不可能走到 authorized。
HOLD_MAX_SEC = 600

SUPPORTED_PLATFORMS = (
    "telegram", "line", "whatsapp", "messenger", "web",
    # 纯官方 API 渠道（mode=official，凭证经 /workspace/setup 接入向导配置）
    "instagram", "zalo",
    # QQ 机器人（QQ 开放平台官方 API，2026-09-07）：与「QQ 协议登录」（platform=qq，
    # 个人号）是两个独立平台——身份空间（openid vs QQ 号）/ 合规归属（主站 vs 准入区）/
    # 图标都不同，刻意不合成一个平台的两种方式。
    "qqbot",
)

# 登录方式（mode）：协议多开 / 网页隔离 / 真机RPA / 官方 API / 手机号验证码
# phone＝服务端协议登录的第二形态（号码+短信码，与 protocol 的扫码并列）；默认关、次要选项。
MODES = ("protocol", "web", "device", "official", "phone")
MODE_LABELS: Dict[str, str] = {
    # 2026-08-11 用户化改名：旧名「协议多开」是机制黑话，坐席视角这就是「扫码登录」
    # （Telegram=官方关联设备 / WhatsApp=Baileys / LINE=okline，都是扫码起号）。
    # 「多开/省资源」的卖点由 desc + caps 徽片承载，标题只回答「我要怎么登录」。
    "protocol": "扫码登录",
    "web": "网页扫码",
    "device": "真机 / 模拟器",
    "official": "官方 API 接入",
    "phone": "手机号 + 验证码",
}
MODE_DESC: Dict[str, str] = {
    # 「（推荐）」从 desc 里拿掉——推荐语义由绿色「推荐」徽标承载，文案里再写一遍是双源
    "protocol": "服务端协议直连，单机可挂大量账号，最省资源",
    "web": "隔离浏览器 + 平台网页二维码，兼容好、更像真人",
    "device": "在真机 / 模拟器上完成官方 App 登录，最稳妥，账号数受设备数限制",
    "official": "平台官方开放接口，最合规稳定；凭证在接入向导里配置，无需扫码",
    "phone": "输入手机号收验证码登录（可能需两步验证密码）；风险高于扫码，建议小号 + 独立代理",
}

# 登录形态（login_kind）：决定前端向导的整套词汇与主视觉，与 mode 正交——
# 同叫 "web" 的方式，在 WhatsApp 是「隔离浏览器里出二维码」（qr），在 Messenger 是
# 「服务器托管的账密 / 2FA 登录」（hosted）。此前整个向导只有「扫码」一套词汇，
# Messenger 用户会对着一个永远不出现的二维码干等（2026-08 修复的根因）。
#   qr     = 本窗口展示二维码，用手机扫（步骤：扫码 → 确认 → 完成）
#   hosted = 登录在服务器的隔离浏览器窗口内完成，本窗口只做实时预览与结果确认
#   device = 登录在真机 / 模拟器上完成，本窗口只等账号上线
#   credentials = 无登录会话：填官方 API 凭证即接入（走 /workspace/setup 接入向导，
#                 不是本弹窗的扫码流程；前端据此把 CTA 换成「去接入向导」）
#   phone_code = 本窗口分步输入：手机号 → 短信/App 验证码 →（可选）两步验证云密码
LOGIN_KINDS = ("qr", "hosted", "device", "credentials", "phone_code")
MODE_LOGIN_KINDS: Dict[str, str] = {
    "protocol": "qr",
    "web": "qr",
    "device": "device",
    "official": "credentials",
    "phone": "phone_code",
}

# hosted 形态的登录会话时长：账密 + 2FA +（可能的）checkpoint 远超扫码节奏，
# 沿用 180s 会在运维输到一半时判过期——前端旧行为还会就地重开会话，把服务器上
# 正在操作的登录窗口直接关掉。
# 2026-08 再放宽 600→1800：实录一次真人手动过 2FA 耗时 ~24min（Messenger 的
# messenger-web 交互登录窗口本就是 30min）。旧 600s 下 _poll 早停 → 负责写 web/online
# 的主链失效，账号卡「未接入」（另有 session-status 兜底，但 TTL 内让主链跑完更干净、
# 且前端弹窗能显示真成功而非假超时）。hosted 会话仅进程内 dict + HTTP 轮询，无长连/线程，
# 30min 存活无资源泄漏之虞（区别于 WA okline 的 HOLD_MAX_SEC 约束）。
HOSTED_TTL_SEC = 1800
# 上面两张表是**后端兜底**文案；前端优先用下面的 i18n 键取本地化文案（英文坐席看到的
# 不该是中文模式名）。键值缺失时前端自动回落 label/desc。
MODE_LABEL_KEYS: Dict[str, str] = {
    "protocol": "inbox.connect.mode_l_protocol",
    "web": "inbox.connect.mode_l_web",
    "device": "inbox.connect.mode_l_device",
    "official": "inbox.connect.mode_l_official",
    "phone": "inbox.connect.mode_l_phone",
}
MODE_DESC_KEYS: Dict[str, str] = {
    "protocol": "inbox.connect.mode_d_protocol",
    "web": "inbox.connect.mode_d_web",
    "device": "inbox.connect.mode_d_device",
    "official": "inbox.connect.mode_d_official",
    "phone": "inbox.connect.mode_d_phone",
}
# 能力标签（前端渲染成 chips → i18n 键 inbox.connect.cap_<name>）：让运营不必读技术描述
# 就能横向比较「能挂几个号 / 要不要买手机 / 要不要运维」。
MODE_CAPS: Dict[str, tuple] = {
    "protocol": ("multi", "light"),
    "web": ("compat", "human"),
    "device": ("antiban", "need_device"),
    "official": ("compliant", "light"),
    "phone": ("human", "light"),
}

# 不可用原因码（结构化，前端按码取本地化解释与处置建议；文本仅作后端兜底）
REASON_NOT_ENABLED = "not_enabled"
REASON_NEEDS_SERVER_SETUP = "needs_server_setup"
# 功能不存在（占位方式，如 whatsapp/web）：与「开关没开」分码——处置是「改用别的
# 方式」，不是翻开关/填凭据。与 platform_readiness.BLOCK_NOT_IMPLEMENTED 同码串。
REASON_NOT_IMPLEMENTED = "not_implemented"
REASON_TEXTS: Dict[str, str] = {
    REASON_NOT_ENABLED: "开发中 / 未启用",
    REASON_NEEDS_SERVER_SETUP: "需服务器端配置后启用",
    REASON_NOT_IMPLEMENTED: "规划中 / 本版本未提供",
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
            "login_kind": "hosted",
        },
        # Messenger 的 device 也不是扫码（FB App 是账密登录），描述走专属键，
        # 别沿用通用 device 文案里的扫码叙事。
        "device": {
            "desc": "在真机 / 模拟器上完成 Facebook App 官方登录，最稳妥，账号数受设备数限制",
            "desc_key": "inbox.connect.mode_d_msg_device",
        },
    },
    # Zalo 个人号扫码（zca-js 边车，模拟 Zalo Web）——与官方 OA 并列的第二路，能力反而
    # 更全（官方 OA 仅文字 + 7 天窗；个人号可发图/语音/贴纸）。随附「稳定运行建议」
    # （小号 + 独立代理；2026-08-10 运营决策：正向建议口径，不再作风险恐吓）。
    # sidecar 未装/未登录时归「需服务器端配置」。
    "zalo": {
        "web": {
            "label": "扫码登录（个人号）",
            "desc": "用你自己的 Zalo 账号扫码登录，可收发文字/图片/语音/贴纸；建议搭配小号与独立代理使用",
            "label_key": "inbox.connect.mode_l_zalo_web",
            "desc_key": "inbox.connect.mode_d_zalo_web",
            "caps": ("human", "compat"),
            "unavailable_reason_code": REASON_NEEDS_SERVER_SETUP,
            "login_kind": "qr",
            # 个人号最佳实践提示（前端在方式卡 + 扫码步各渲一条；单一 i18n 源）。
            # 通用字段：任何 mode override 声明 notice_key 即随 /modes 下发 notice 结构。
            "notice_key": "inbox.connect.notice_unofficial",
            "notice_severity": "info",
        },
    },
    # Instagram 个人号（Playwright 网页托管，login_kind=hosted）——与官方 Graph API 并列
    # 的第二路。IG 网页版无「手机扫码配对」，登录在服务器隔离浏览器内用账密/2FA 完成，
    # 本窗口做实时预览（复用 Messenger hosted 的 LIVE 预览 UI），绝不出现「扫码」字样。
    # 随附「稳定运行建议」（小号 + 独立代理），口径同 Zalo。
    "instagram": {
        "web": {
            "label": "账号登录（个人号）",
            "desc": "在服务器的隔离浏览器里登录你的 Instagram（账密 / 2FA，不用二维码）；建议搭配小号与独立代理使用",
            "label_key": "inbox.connect.mode_l_ig_web",
            "desc_key": "inbox.connect.mode_d_ig_web",
            "caps": ("human", "server"),
            "unavailable_reason_code": REASON_NEEDS_SERVER_SETUP,
            "login_kind": "hosted",
            "notice_key": "inbox.connect.notice_unofficial",
            "notice_severity": "info",
        },
    },
    # Telegram 手机号验证码：与 protocol 扫码并列的第二形态（login_kind=phone_code）。
    # 默认关、桌面种子/升级表可开；风险高于扫码，notice 正向建议小号+代理（同 Zalo 口径）。
    "telegram": {
        "phone": {
            "label": "手机号 + 验证码",
            "desc": "输入手机号收验证码登录（可能需两步验证密码）；无法扫自己屏幕时的备选",
            "label_key": "inbox.connect.mode_l_phone",
            "desc_key": "inbox.connect.mode_d_phone",
            "caps": ("human", "light"),
            "login_kind": "phone_code",
            "notice_key": "inbox.connect.notice_tg_phone",
            "notice_severity": "info",
        },
    },
}


def login_kind(platform: str, mode: str) -> str:
    """(platform, mode) → 登录形态。单一事实源：平台级覆盖优先，其余按 mode 默认表。"""
    ov = PLATFORM_MODE_OVERRIDES.get(str(platform or "").lower(), {}) \
        .get(str(mode or "").lower(), {})
    kind = str(ov.get("login_kind") or "")
    if kind in LOGIN_KINDS:
        return kind
    return MODE_LOGIN_KINDS.get(str(mode or "").lower(), "qr")

# 每平台默认可选方式与默认方式（可被 config.platform_login.<platform> 覆盖）。
# LINE/Messenger/WhatsApp 的 official = 官方 Messaging/Graph/Cloud API 合规通道
# （凭证经接入向导，与扫码/托管登录并列的第二条路）。
DEFAULT_PLATFORM_MODES: Dict[str, Dict[str, Any]] = {
    # telegram 不再列 web 占位（2026-08-11）：protocol 本就是官方「关联设备」扫码
    # （ExportLoginToken，二维码在本窗口显示），对用户与「网页扫码」是同一体验；
    # 挂一张永不可用的重复卡只会引人点进死胡同。桌面种子早已同款摘除（见
    # config.desktop.min.yaml 注释与 test_seed_modes_are_implemented 门禁）。
    "telegram": {"modes": ["protocol", "device"], "default": "protocol"},
    "whatsapp": {"modes": ["protocol", "web", "device", "official"], "default": "protocol"},
    "line": {"modes": ["protocol", "device", "official"], "default": "protocol"},
    "messenger": {"modes": ["web", "device", "official"], "default": "web"},
    "web": {"modes": [], "default": ""},
    # 纯官方 API 渠道：没有「自己账号扫码」形态，凭证经接入向导（/workspace/setup）
    "instagram": {"modes": ["official"], "default": "official"},
    "zalo": {"modes": ["official"], "default": "official"},
    # QQ 机器人：只有官方形态（AppID/AppSecret 经向导）；个人号扫码是另一个平台 qq
    "qqbot": {"modes": ["official"], "default": "official"},
}

# 个人号扫码登录（web/qr 边车）可**增量**补给这些原本纯官方的渠道。刻意不写进上面的
# 静态 DEFAULT_PLATFORM_MODES —— 那份被 test_official_channel_onboarding 钉死为
# {"modes":["official"]}，且改它会立刻改变官方漏斗默认方式（有活跃门禁）。改为在
# list_modes 里按**运行时开关** platform_login.<p>.web_enabled 注入：关＝逐字节旧行为，
# 开＝把个人号扫码作为主路径插到官方前面。值＝该渠道个人号登录用的 mode 名。
_PERSONAL_WEB_LOGIN: Dict[str, str] = {
    "zalo": "web",
    "instagram": "web",  # P2：Playwright 网页托管登录（hosted）
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
        "扫码：在设备 / 投屏端打开 WhatsApp「关联设备」并用手机扫码；"
        "官方 Cloud API：在「接入向导」里填好 Meta 凭证即自动上线。"
    ),
    "messenger": (
        "服务器上已打开 Facebook 官方登录窗口，请在该机器上完成登录（账密 / 2FA）。"
        "完成后本窗口会自动确认——本方式不使用二维码，无需用手机扫描。"
    ),
    "web": "网页客服为服务端原生渠道，无需扫码登录。",
    "instagram": "Instagram 走官方 API 接入：在「接入向导」里填好 Meta 凭证即自动上线，无需扫码。",
    "zalo": "Zalo 走官方 OA API 接入：在「接入向导」里填好 OA 凭证即自动上线，无需扫码。",
    "qqbot": (
        "QQ 机器人走 QQ 开放平台官方接入：在「接入向导」里填好 AppID / AppSecret 即自动上线，"
        "无需扫码；正式环境需在开放平台配置 IP 白名单，联调可先用沙箱。"
    ),
}

# 上表的 i18n 键（英文坐席不该看到中文指引）。仅在指引取自上表（即 provider 没给
# 自己的 instruction）时随会话带出——provider 自带的指引没有对应键，硬配平台默认键
# 会让前端显示与 provider 意图不符的文案。provider 也可在返回 dict 里直接给
# ``instruction_key``（如 Messenger 托管登录）。
PLATFORM_INSTRUCTION_KEYS: Dict[str, str] = {
    "telegram": "inbox.connect.instr_telegram",
    "line": "inbox.connect.instr_line",
    "whatsapp": "inbox.connect.instr_whatsapp",
    "messenger": "inbox.connect.instr_messenger",
    "instagram": "inbox.connect.instr_instagram",
    "zalo": "inbox.connect.instr_zalo",
    "qqbot": "inbox.connect.instr_qqbot",
}


@dataclass
class LoginSession:
    login_id: str
    platform: str
    mode: str = "device"
    account_id: str = ""
    created_at: float = field(default_factory=time.time)
    # pending | scanned | pin_needed | code_needed | password_needed | authorized | expired | failed
    # scanned = 等用户在手机上点确认（我方无信息要传递）；
    # pin_needed = 我方持有 PIN，等用户在手机上输入（LINE 协议登录）
    # code_needed = 已发短信/App 验证码，等用户在本窗口提交（Telegram phone）
    status: str = "pending"
    qr_url: str = ""          # 网页二维码可编码的 URL（如 tg://login?token=...）
    qr_image: str = ""        # data URI（可选，provider 直接给出二维码图片）
    instruction: str = ""
    # instruction 的 i18n 键（可选）：前端优先按键取本地化文案，raw instruction 仅兜底
    instruction_key: str = ""
    # 会话时长覆盖（秒）：0 = 按 TTL_SEC 默认。hosted 形态在 create 时放宽（见 HOSTED_TTL_SEC）
    ttl_sec: float = 0.0
    detail: str = ""
    # failed 的机器可读原因（provider poll 落此，供 status 早退分支继续吐给前端本地化）
    reason_code: str = ""
    # cred_invalid 处置元信息（Telegram 两 provider 下发；其余平台恒缺省）：
    # cred_source=hosted|pool|self；retry_after_sec=换发冷却剩余秒（-1=不适用）。
    # 前端据此倒计时自动重试 / 亮自填修正表单，替代「用户自己判断凭据是谁配的」。
    cred_source: str = ""
    retry_after_sec: int = -1
    # 实时提示码（**非终态**，与 reason_code 同一套词汇）：托管登录里「此刻页面在要什么」
    # （two_factor / checkpoint / password_error）。两个用途：前端出实时指引；会话按 TTL
    # 过期时当归因——那条早退分支不再 poll provider，没有它就只能记一句笼统超时。
    hint_code: str = ""
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
    submit_fn: Optional[Callable[..., Any]] = None   # 两步验证云密码提交（Telegram protocol/phone）
    submit_code_fn: Optional[Callable[..., Any]] = None  # 手机号登录：提交短信/App 验证码
    resend_code_fn: Optional[Callable[..., Any]] = None  # 手机号登录：重发验证码
    # 表单中继（Form-Relay，Messenger 交互登录）：只读探针「登录页此刻该渲染哪一步原生表单」。
    # provider 仅在开启 interactive_login 时提供；其余平台/关闭时为 None（路由据此回 not_supported）。
    relay_step_fn: Optional[Callable[..., Any]] = None
    # 表单中继写入端：把应用侧原生表单字段值（账密 / 2FA 码 / PIN）填回登录页。签名
    # (session, step, values)。同 relay_step_fn 仅交互登录开启时提供。
    relay_submit_fn: Optional[Callable[..., Any]] = None

    def is_expired(self) -> bool:
        age = time.time() - self.created_at
        if self.status in HOLD_STATES:
            return age > HOLD_MAX_SEC
        return age > (self.ttl_sec or TTL_SEC)

    def remaining_sec(self) -> int:
        """距会话过期还剩几秒（与 is_expired 同一套口径；HOLD 态按 HOLD_MAX_SEC 算）。
        供前端画二维码有效期条——只读展示，绝不参与过期判定（判定仍以 is_expired 为准）。"""
        age = time.time() - self.created_at
        cap = HOLD_MAX_SEC if self.status in HOLD_STATES else (self.ttl_sec or TTL_SEC)
        return max(0, int(cap - age))


class LoginManager:
    """内存登录会话表（进程内，单实例）。线程安全。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = threading.Lock()

    def _gc_locked(self) -> None:
        now = time.time()
        dead = [
            k for k, s in self._sessions.items()
            if (now - s.created_at) > (
                HOLD_MAX_SEC if s.status in HOLD_STATES
                else (s.ttl_sec or TTL_SEC) * 2)
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
        instruction_key: str = "",
        ttl_sec: Optional[float] = None,
        label: str = "",
        group: str = "",
        proxy_id: str = "",
        fingerprint_id: str = "",
        provider_state: Any = None,
        poll_fn: Optional[Callable[..., Any]] = None,
        cancel_fn: Optional[Callable[..., Any]] = None,
        submit_fn: Optional[Callable[..., Any]] = None,
        submit_code_fn: Optional[Callable[..., Any]] = None,
        resend_code_fn: Optional[Callable[..., Any]] = None,
        relay_step_fn: Optional[Callable[..., Any]] = None,
        relay_submit_fn: Optional[Callable[..., Any]] = None,
        initial_status: str = "",
        reason_code: str = "",
        detail: str = "",
        cred_source: str = "",
        retry_after_sec: int = -1,
    ) -> LoginSession:
        with self._lock:
            self._gc_locked()
            sid = secrets.token_urlsafe(12)
            # 指引与其 i18n 键必须同源：只有指引取自平台默认表时才配平台默认键，
            # provider 自带指引（无键）时保持 raw 文案，防止前端按键显示错话。
            if not instruction:
                instruction = PLATFORM_INSTRUCTIONS.get(platform, "请完成登录。")
                if not instruction_key:
                    instruction_key = PLATFORM_INSTRUCTION_KEYS.get(platform, "")
            if ttl_sec is None:
                ttl_sec = float(
                    HOSTED_TTL_SEC if login_kind(platform, mode) == "hosted" else 0)
            sess = LoginSession(
                login_id=sid,
                platform=platform,
                mode=mode or "device",
                account_id=account_id or "",
                baseline=set(baseline or set()),
                qr_url=qr_url,
                qr_image=qr_image,
                instruction=instruction,
                instruction_key=instruction_key,
                ttl_sec=float(ttl_sec or 0),
                label=label,
                group=group,
                proxy_id=proxy_id,
                fingerprint_id=fingerprint_id,
                provider_state=provider_state,
                poll_fn=poll_fn,
                cancel_fn=cancel_fn,
                submit_fn=submit_fn,
                submit_code_fn=submit_code_fn,
                resend_code_fn=resend_code_fn,
                relay_step_fn=relay_step_fn,
                relay_submit_fn=relay_submit_fn,
            )
            # phone_code 等开局即进入等待态（已发验证码 / 已失败）：别让前端对着
            # pending 空转一轮 poll 才看见真相。
            _st = str(initial_status or "").strip()
            if _st in ("code_needed", "password_needed", "failed", "authorized"):
                sess.status = _st
            if reason_code:
                sess.reason_code = str(reason_code)
            if detail:
                sess.detail = str(detail)
            if cred_source:
                sess.cred_source = str(cred_source)
                sess.retry_after_sec = int(retry_after_sec)
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
    # 手机号验证码：扫码失败/无法自扫时的备选入口；桌面升级默认开、排在方式列表末位
    "platform_login.telegram.phone_enabled",
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
    protocol/web 需注册了真实 provider 才可用；official 无登录 provider（凭证经
    接入向导落 config），判据＝编排器已注册该平台的官方 worker 工厂（即凭证/开关
    就绪，`register_official_workers` 门控通过）。"""
    mode = str(mode).lower()
    if mode == "device":
        return True
    if mode == "official":
        try:
            # 懒加载防与 account_orchestrator 的函数级反向 import 成环
            from src.integrations.account_orchestrator import get_worker_factory
            return get_worker_factory(platform, "official") is not None
        except Exception:
            return False
    return get_login_provider(platform, mode) is not None


def _mode_implemented(platform: str, mode: str) -> bool:
    """(平台, 方式) 是否有真实实现。单一事实源＝platform_readiness._IMPLEMENTED_MODES
    （懒加载防成环）；device 内置恒真；判定异常按「已实现」处理——宁可回落笼统的
    not_enabled，也不把真功能误标成「规划中」。"""
    mode = str(mode or "").lower()
    if mode == "device":
        return True
    try:
        from src.integrations.platform_readiness import _IMPLEMENTED_MODES
        return (str(platform or "").lower(), mode) in _IMPLEMENTED_MODES
    except Exception:  # noqa: BLE001
        return True


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
    modes = list(cfg.get("modes") or pdef["modes"])
    default = cfg.get("default") or pdef["default"]
    # 个人号扫码登录增量注入（默认关；见 _PERSONAL_WEB_LOGIN 注释）——把 web 插到最前
    # 并设为默认方式，让「用你自己的号扫码」成为主路径，官方 API 退为折叠的合规第二路。
    # 开关经 resolve_login_switch 三态解析（把 platform_cfg 还原成带根路径再问，避免字面
    # 直读绕过升级自愈机制——见 test_no_literal_switch_reads_outside_diagnostics）。
    _pweb = _PERSONAL_WEB_LOGIN.get(platform)
    if _pweb and _pweb not in modes and resolve_login_switch(
        {"platform_login": {platform: cfg}},
        f"platform_login.{platform}.web_enabled",
    ):
        modes = [_pweb, *modes]
        if not cfg.get("default"):
            default = _pweb
    # Telegram 手机号登录：增量挂到清单**末位**（永不抢默认 / 推荐位——扫码仍是主路径）。
    # 开关经三态解析；关＝逐字节旧行为。不写进 DEFAULT_PLATFORM_MODES，避免改默认表
    # 牵动官方漏斗门禁（同 _PERSONAL_WEB_LOGIN 哲学）。
    if (platform == "telegram" and "phone" not in modes
            and resolve_login_switch(
                {"platform_login": {platform: cfg}},
                "platform_login.telegram.phone_enabled",
            )):
        modes = [*modes, "phone"]
    overrides = PLATFORM_MODE_OVERRIDES.get(platform, {})
    out: List[Dict[str, Any]] = []
    for m in modes:
        avail = mode_available(platform, m)
        ov = overrides.get(m, {})
        rc = "" if avail else str(
            ov.get("unavailable_reason_code")
            or (REASON_NOT_IMPLEMENTED if not _mode_implemented(platform, m)
                else REASON_NOT_ENABLED))
        out.append({
            "mode": m,
            "label": str(ov.get("label") or MODE_LABELS.get(m, m)),
            "desc": str(ov.get("desc") or MODE_DESC.get(m, "")),
            "label_key": str(ov.get("label_key") or MODE_LABEL_KEYS.get(m, "")),
            "desc_key": str(ov.get("desc_key") or MODE_DESC_KEYS.get(m, "")),
            "caps": list(ov.get("caps") or MODE_CAPS.get(m, ())),
            # 登录形态：前端整套向导词汇（按钮/步骤/状态/主视觉）按它取值
            "login_kind": login_kind(platform, m),
            "available": avail,
            # recommended = 该平台的**默认方式**（配置语义，与可用性正交，勿改）。
            "recommended": (m == default),
            # preferred = 给 UI 用的「主推」标记：默认方式 **且** 当前真的能用。
            # 二者曾被混为一谈 —— Messenger 的 web 同时是默认方式和不可用，界面便渲染出
            # 一个挂着绿色「推荐」角标却点不动、无任何反馈的选项。可用性是硬前提。
            "preferred": bool(avail and m == default),
            "reason_code": rc,
            "reason": REASON_TEXTS.get(rc, "") if rc else "",
            # 风险/合规告知（override 声明 notice_key 才有；前端按 severity 上色渲横幅）。
            # 通用结构，非 Zalo 专属——IG 个人号（P2）声明同字段即自动获得。
            "notice": ({"key": str(ov["notice_key"]),
                        "severity": str(ov.get("notice_severity") or "warn")}
                       if ov.get("notice_key") else None),
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
