"""平台注册表——「有哪些平台、叫什么、什么颜色、哪些接法、合规归属、落地了没」的单一事实源。

**为什么要有这个模块**（实施96 P0-3，2026-09-07）：接一个新渠道时，展示名 / 品牌色 /
图标 / 可选接法 / 事实卡 / 能力矩阵 / i18n 各自维护一份平台表，散在约 20 处
（``normalizer.PLATFORM_DISPLAY`` 只有 4 项、``unified_inbox.html`` 的 PC/PI/PN、
``workspace_base.html`` 的 PLAT_COLOR、``platform_icons.js`` 的 COLORS/NAMES、
``product_facts`` 的支持/不支持清单……）。同一个 Telegram 蓝在仓里有三个值。
Zalo/IG 接入付过一次税，QQ/微信客服（实施97）付第二次，抖音/TikTok 不该付第三次。

**本模块的做法是「先立事实源 + 门禁，再逐处迁移」，不是一次性重写**：
- 这里只声明事实（纯数据 + 纯函数，零 IO、零依赖）；
- ``tests/test_platform_registry.py`` 把仍散落的各表与这里**比对**——漂移即红，
  谁改散表谁来这里同步（或反过来）；
- 消费方按批迁移到 ``display_name()`` / ``color()`` / ``modes_table()`` 等访问器；
  前端经 ``scripts/platform_registry_export.py`` 产出的
  ``static/platform_registry.json`` 读同一份数据（产物由门禁钉住与本模块一致）。

**口径约定**（与既有各表对齐，别自创第二套语义）：
- ``name``：语言中立品牌名（工作台 PN 表 / ``PLATFORM_DISPLAY`` 口径，切 UI 语种不混语）；
- ``color``：品牌主色，**唯一**取值（PC / COLORS / PLAT_COLOR 都该等于它）；
- ``modes``/``default_mode``：``platform_login.DEFAULT_PLATFORM_MODES`` 口径；
- ``compliance``：``products/*/product.yaml compliance.visibility`` 口径——
  ``main``＝合规主站可展示，``restricted``＝隔离准入区，``mixed``＝按接法分层
  （官方 API 进主站、个人号进准入区）；
- ``implemented``：有真实 worker / 边车 / webhook 落地（``platform_readiness._IMPLEMENTED_MODES``
  与工作台左栏常驻 ``FIXED_PLATS`` 口径）。**规划中的平台也登记（implemented=False）**，
  这样事实卡的「暂不支持」清单有据可查，翻转时门禁会逼人同步。
- ``driver_state``（QQ 线 A 段，2026-09-10）：``implemented`` 之下再分一层「底层驱动到底接上没」——
  ``real``＝真收发；``mock``＝外壳 / 边车 / UI 全链在、但底层驱动是演示替身（CI 成立、真平台不成立）；
  ``none``＝未落地。缺省按 ``implemented`` 推（True→real / False→none），**只有驱动是替身的平台才显式标
  mock**。消费方：工作台账号卡 / 连接面板「边车已装 · 驱动未接入（演示态）」、事实卡「预览中」清单。
  发明它的原因：qq-personal 边车已进安装包、CI 全绿，但 ``ntq/qqnt-driver.js`` 尚未移植——
  ``implemented=True`` 一个布尔位会让所有下游（含小智）对客说「能用」，这是诚实缺口。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

COMPLIANCE_MAIN = "main"
COMPLIANCE_RESTRICTED = "restricted"
COMPLIANCE_MIXED = "mixed"
_COMPLIANCE_VALUES = (COMPLIANCE_MAIN, COMPLIANCE_RESTRICTED, COMPLIANCE_MIXED)

#: 驱动三态（见模块 docstring ``driver_state``）
DRIVER_REAL = "real"
DRIVER_MOCK = "mock"
DRIVER_NONE = "none"
_DRIVER_STATES = (DRIVER_REAL, DRIVER_MOCK, DRIVER_NONE)

#: 登录/接入方式全集（``platform_login`` 的 mode 词表；``personal_rpa`` 是 leadbus 外部
#: 进程驱动的账号 mode，收件箱可见但本机编排器不接管）。
KNOWN_MODES = ("protocol", "web", "device", "official", "phone", "personal_rpa", "mock")


@dataclass(frozen=True)
class PlatformSpec:
    id: str
    name: str
    name_zh: str
    family: str
    color: str
    modes: Tuple[str, ...]
    default_mode: str
    compliance: str
    implemented: bool
    #: 该平台可信的平台侧消息 id 字段名（``normalizer._PLATFORM_MSG_ID_FIELDS`` 口径）
    msg_id_fields: Tuple[str, ...] = ()
    console_url: str = ""
    #: 账号必须带 ``region``（注册地决定 API 可用性 / 媒体可发性——TikTok）
    region_aware: bool = False
    #: 事实卡（``product_facts``）里的对客叫法；空＝用 ``name``
    facts_label: str = ""
    #: ``platform_icons.js`` 图标键；空＝``id``
    icon: str = ""
    #: 左栏 / 向导排序
    sort: int = 100
    #: 备注（规划状态、路线来源）
    note: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    #: 驱动三态 real/mock/none；空＝按 implemented 推（见 ``driver_state_value()``）
    driver_state: str = ""
    #: 驱动非 real 时的一句原因（对客 / 账号卡 / 事实卡同源）
    driver_note: str = ""

    def facts_name(self) -> str:
        return self.facts_label or self.name

    def icon_key(self) -> str:
        return self.icon or self.id

    def driver_state_value(self) -> str:
        if self.driver_state:
            return self.driver_state
        return DRIVER_REAL if self.implemented else DRIVER_NONE

    def is_preview(self) -> bool:
        """已落地但驱动是演示替身（对客口径「预览，尚不能真收发」）。"""
        return self.implemented and self.driver_state_value() == DRIVER_MOCK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "name_zh": self.name_zh,
            "family": self.family, "color": self.color,
            "modes": list(self.modes), "default_mode": self.default_mode,
            "compliance": self.compliance, "implemented": self.implemented,
            "driver_state": self.driver_state_value(), "driver_note": self.driver_note,
            "msg_id_fields": list(self.msg_id_fields), "console_url": self.console_url,
            "region_aware": self.region_aware, "facts_label": self.facts_name(),
            "icon": self.icon_key(), "sort": self.sort, "note": self.note,
        }


# ── 注册表本体 ─────────────────────────────────────────────────────────────────
# 颜色取 ``platform_icons.js`` 的 COLORS 为基准（那是图标底色，肉眼最先看到的那个）。
_SPECS: Tuple[PlatformSpec, ...] = (
    PlatformSpec(
        id="telegram", name="Telegram", name_zh="Telegram", family="telegram",
        color="#229ED9", modes=("protocol", "device", "phone"), default_mode="protocol",
        compliance=COMPLIANCE_MAIN, implemented=True,
        msg_id_fields=("id", "message_id"), console_url="https://my.telegram.org/",
        sort=10, aliases=("tg",),
    ),
    PlatformSpec(
        id="whatsapp", name="WhatsApp", name_zh="WhatsApp", family="meta",
        color="#25D366", modes=("protocol", "web", "device", "official"),
        default_mode="protocol", compliance=COMPLIANCE_MIXED, implemented=True,
        msg_id_fields=("wamid", "message_id", "msg_id"),
        console_url="https://developers.facebook.com/apps/", sort=20, aliases=("wa",),
    ),
    PlatformSpec(
        id="line", name="LINE", name_zh="LINE", family="line",
        color="#06C755", modes=("protocol", "device", "official"), default_mode="protocol",
        compliance=COMPLIANCE_MIXED, implemented=True,
        msg_id_fields=("message_id", "server_id"),
        console_url="https://developers.line.biz/console/", sort=30, aliases=("line_rpa",),
    ),
    PlatformSpec(
        id="messenger", name="Messenger", name_zh="Messenger", family="meta",
        color="#0084FF", modes=("web", "device", "official"), default_mode="web",
        compliance=COMPLIANCE_MIXED, implemented=True,
        msg_id_fields=("mid", "message_id", "msg_id"),
        console_url="https://developers.facebook.com/apps/", sort=40,
        facts_label="Facebook Messenger",
        aliases=("fb", "facebook", "facebook_messenger"),
    ),
    PlatformSpec(
        id="instagram", name="Instagram", name_zh="Instagram", family="meta",
        color="#E1306C", modes=("official", "web"), default_mode="official",
        compliance=COMPLIANCE_MIXED, implemented=True,
        console_url="https://developers.facebook.com/apps/", sort=50, aliases=("ig",),
    ),
    PlatformSpec(
        id="zalo", name="Zalo", name_zh="Zalo", family="zalo",
        color="#0068FF", modes=("official", "web"), default_mode="official",
        compliance=COMPLIANCE_MIXED, implemented=True,
        console_url="https://oa.zalo.me/", sort=60,
    ),
    PlatformSpec(
        id="web", name="Web", name_zh="官网网页聊天", family="web",
        color="#3B82F6", modes=(), default_mode="",
        compliance=COMPLIANCE_MAIN, implemented=True,
        msg_id_fields=("message_id", "id"), facts_label="官网网页聊天", sort=90,
        aliases=("inbox", "website", "widget", "web_chat"),
    ),
    # ── 实施97（2026-09-07）：QQ 机器人 / 微信客服官方通道 ────────────────────────
    PlatformSpec(
        id="qqbot", name="QQ Bot", name_zh="QQ 机器人", family="tencent",
        color="#1E63FF", modes=("official",), default_mode="official",
        compliance=COMPLIANCE_MAIN, implemented=True,
        facts_label="QQ 机器人", console_url="https://q.qq.com/", sort=70,
        aliases=("qq_bot", "qq-bot", "qqofficial", "qq_official"),
        note="QQ 开放平台官方 API；与个人号协议登录（qq）是两个平台",
    ),
    PlatformSpec(
        id="wechat_kf", name="WeChat Service", name_zh="微信客服", family="tencent",
        color="#07C160", modes=("official",), default_mode="official",
        compliance=COMPLIANCE_MAIN, implemented=True, icon="wechat",
        facts_label="微信客服（企业微信）", console_url="https://work.weixin.qq.com/wework_admin/frame#apps", sort=75,
        aliases=("wxkf", "wecom_kf"),
        note="企业微信官方通道；凭证在企微管理后台「应用管理」取（与 channel_setup 同一入口）；与个人微信（wechat）是两个平台",
    ),
    PlatformSpec(
        id="qq", name="QQ", name_zh="QQ 个人号", family="tencent",
        color="#1EBAFC", modes=("protocol",), default_mode="protocol",
        compliance=COMPLIANCE_RESTRICTED, implemented=True,
        facts_label="QQ 个人号（协议登录）", sort=71, aliases=("tencentqq", "qq_protocol"),
        note="QQ 双轨 P2（2026-09-07 落地）：个人号经 Milky 接口（route-1 自带边车 services/qq-personal，"
             "亦可指向自装协议端），准入区、默认关",
        # QQ 线 A 段（2026-09-10）：边车已进安装包、CI 全绿，但真驱动 ntq/qqnt-driver.js 未移植，
        # QQ_DRIVER=qqnt 加载失败 → 回退 mock。翻 real 的条件：B 段真驱动落地 + C 段测试号 72h 挂机通过。
        driver_state=DRIVER_MOCK,
        driver_note="边车已装 · 驱动未接入（演示态）：扫码与收发均为演示数据，尚不能真收发",
    ),
    # ── 规划中（登记即有据可查；翻 implemented 前门禁逼同步事实卡）──────────────
    PlatformSpec(
        id="wechat", name="WeChat", name_zh="个人微信", family="tencent",
        color="#07C160", modes=("device",), default_mode="device",
        compliance=COMPLIANCE_RESTRICTED, implemented=False,
        facts_label="个人微信", sort=76, aliases=("weixin",),
        note="实施97 线 B：PC 副驾（桌面桥接账号 mode=desktop，非编排器 worker → implemented 保持 False）；接入引导 /workspace/connect/wechat_pc",
    ),
    PlatformSpec(
        id="douyin", name="Douyin", name_zh="抖音", family="bytedance",
        color="#FE2C55", modes=("official", "web", "device"), default_mode="official",
        compliance=COMPLIANCE_MIXED, implemented=False,
        msg_id_fields=("server_message_id", "msg_id"),
        console_url="https://developer.open-douyin.com/", facts_label="抖音", sort=80,
        aliases=("dy", "aweme"),
        note="实施96：official＝企业主体小程序 IM（主站）；web/device＝网页/真机托管（准入区）",
    ),
    PlatformSpec(
        id="tiktok", name="TikTok", name_zh="TikTok", family="bytedance",
        color="#25F4EE", modes=("official", "web", "personal_rpa"), default_mode="official",
        compliance=COMPLIANCE_MIXED, implemented=False, region_aware=True,
        msg_id_fields=("message_id", "msg_id"),
        console_url="https://business-api.tiktok.com/", facts_label="TikTok", sort=85,
        aliases=("tt", "trill", "musically"),
        note="指令 TK-1：official＝Business Messaging / Shop CS API；账号必带 region",
    ),
)

_BY_ID: Dict[str, PlatformSpec] = {s.id: s for s in _SPECS}
_ALIAS: Dict[str, str] = {}
for _s in _SPECS:
    for _a in _s.aliases:
        _ALIAS[_a] = _s.id
del _s, _a


# ── 访问器（全部纯函数）─────────────────────────────────────────────────────────

def normalize_platform_id(raw: Any) -> str:
    """把各处口径不一的平台写法归一到注册表 id（``TG``/``fb``/``line_rpa`` → 正名）。

    未知值原样小写返回（**不**抛、不猜）——调用方按 ``get()`` 为 None 处理。
    """
    p = str(raw or "").strip().lower()
    if not p:
        return ""
    if p in _BY_ID:
        return p
    return _ALIAS.get(p, p)


def get(platform: Any) -> Optional[PlatformSpec]:
    return _BY_ID.get(normalize_platform_id(platform))


def all_platforms(*, implemented_only: bool = False) -> List[PlatformSpec]:
    specs = [s for s in _SPECS if (s.implemented or not implemented_only)]
    return sorted(specs, key=lambda s: (s.sort, s.id))


def implemented_ids() -> Tuple[str, ...]:
    return tuple(s.id for s in all_platforms(implemented_only=True))


def planned_ids() -> Tuple[str, ...]:
    return tuple(s.id for s in all_platforms() if not s.implemented)


def driver_state(platform: Any) -> str:
    """驱动三态 real/mock/none；未登记平台 → ``none``（不猜）。"""
    s = get(platform)
    return s.driver_state_value() if s is not None else DRIVER_NONE


def preview_ids() -> Tuple[str, ...]:
    """已落地但驱动是演示替身的平台（工作台标「演示态」、事实卡列「预览中」）。"""
    return tuple(s.id for s in all_platforms(implemented_only=True) if s.is_preview())


def display_name(platform: Any, fallback: str = "") -> str:
    """语言中立品牌名；未登记 → ``fallback``（缺省 ``platform.title()``，与 normalizer 旧行为同）。"""
    s = get(platform)
    if s is not None:
        return s.name
    raw = str(platform or "")
    return fallback or raw.title()


def color(platform: Any, fallback: str = "#64748B") -> str:
    s = get(platform)
    return s.color if s is not None else fallback


def family_of(platform: Any) -> str:
    s = get(platform)
    return s.family if s is not None else ""


def modes_table(*, implemented_only: bool = True) -> Dict[str, Dict[str, Any]]:
    """``platform_login.DEFAULT_PLATFORM_MODES`` 同形（``{"modes": [...], "default": ...}``）。"""
    return {
        s.id: {"modes": list(s.modes), "default": s.default_mode}
        for s in all_platforms(implemented_only=implemented_only)
    }


def display_table(*, implemented_only: bool = False) -> Dict[str, str]:
    """``normalizer.PLATFORM_DISPLAY`` 同形。"""
    return {s.id: s.name for s in all_platforms(implemented_only=implemented_only)}


def color_table(*, implemented_only: bool = False) -> Dict[str, str]:
    """前端 PC / COLORS / PLAT_COLOR 同形。"""
    return {s.id: s.color for s in all_platforms(implemented_only=implemented_only)}


def facts_supported_labels() -> Tuple[str, ...]:
    """事实卡「支持的渠道」应包含的叫法：已落地 且 合规可见（main/mixed）。"""
    return tuple(
        s.facts_name() for s in all_platforms(implemented_only=True)
        if s.compliance in (COMPLIANCE_MAIN, COMPLIANCE_MIXED)
    )


def facts_unsupported_labels() -> Tuple[str, ...]:
    """事实卡「暂不支持」应包含的叫法：已登记但未落地（规划中）。"""
    return tuple(s.facts_name() for s in all_platforms() if not s.implemented)


def facts_preview_labels() -> Tuple[str, ...]:
    """事实卡「预览中（尚不能真收发）」应包含的叫法：已落地但驱动是演示替身。"""
    return tuple(s.facts_name() for s in all_platforms(implemented_only=True) if s.is_preview())


def export_dict() -> Dict[str, Any]:
    """给前端 / 文档的可序列化快照（稳定排序，便于门禁逐字比对产物）。"""
    return {
        "schema": 1,
        "platforms": [s.as_dict() for s in all_platforms()],
        "aliases": dict(sorted(_ALIAS.items())),
    }


def export_json() -> str:
    return json.dumps(export_dict(), ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def validate() -> List[str]:
    """自检（门禁调用）：id 唯一 / 颜色格式 / mode 词表 / 默认接法属于可选 / 别名不撞正名。"""
    errs: List[str] = []
    seen = set()
    for s in _SPECS:
        if s.id in seen:
            errs.append(f"duplicate id: {s.id}")
        seen.add(s.id)
        if s.id != s.id.lower() or " " in s.id:
            errs.append(f"id must be lower snake: {s.id}")
        c = s.color
        if not (len(c) == 7 and c[0] == "#" and all(ch in "0123456789ABCDEF" for ch in c[1:])):
            errs.append(f"color must be #RRGGBB upper-hex: {s.id} {c}")
        for m in s.modes:
            if m not in KNOWN_MODES:
                errs.append(f"unknown mode {m!r} on {s.id}")
        if s.modes and s.default_mode not in s.modes:
            errs.append(f"default_mode {s.default_mode!r} not in modes for {s.id}")
        if not s.modes and s.default_mode:
            errs.append(f"{s.id}: default_mode set but modes empty")
        if s.compliance not in _COMPLIANCE_VALUES:
            errs.append(f"bad compliance {s.compliance!r} on {s.id}")
        ds = s.driver_state_value()
        if ds not in _DRIVER_STATES:
            errs.append(f"bad driver_state {ds!r} on {s.id}")
        if s.implemented and ds == DRIVER_NONE:
            errs.append(f"{s.id}: implemented but driver_state=none（要么没落地，要么驱动该标 mock/real）")
        if not s.implemented and ds != DRIVER_NONE:
            errs.append(f"{s.id}: not implemented yet driver_state={ds}（未落地的平台不该有驱动）")
        if ds == DRIVER_MOCK and not s.driver_note:
            errs.append(f"{s.id}: driver_state=mock 必须带 driver_note（对客要说得出为什么不能真收发）")
        for a in s.aliases:
            if a in _BY_ID and a != s.id:
                errs.append(f"alias {a!r} of {s.id} collides with a platform id")
    return errs


__all__ = [
    "COMPLIANCE_MAIN", "COMPLIANCE_RESTRICTED", "COMPLIANCE_MIXED", "KNOWN_MODES",
    "DRIVER_REAL", "DRIVER_MOCK", "DRIVER_NONE",
    "PlatformSpec", "normalize_platform_id", "get", "all_platforms", "implemented_ids",
    "planned_ids", "driver_state", "preview_ids", "display_name", "color", "family_of",
    "modes_table", "display_table", "color_table", "facts_supported_labels",
    "facts_unsupported_labels", "facts_preview_labels", "export_dict", "export_json", "validate",
]
