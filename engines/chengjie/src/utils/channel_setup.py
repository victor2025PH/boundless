"""渠道接入向导规格（P1-1）— 声明式「每渠道需要填什么 + 现状如何」。

把「接入一个渠道要填哪些字段、去哪拿、填了没、对不对」收敛成一份**声明式规格**，
供 Web 向导前端渲染表单、后端按渠道写凭证 overlay、并复用 P0-1 ``check_config``
给出即时校验。零依赖、纯函数，便于单测。

设计：每个渠道 = 若干 :class:`Field`（点分 config 路径 + 标签 + 是否密钥/必填 + 取得指引）。
``channel_status(config)`` 返回每渠道填写/校验现状；``apply_channel_values(overlay,
channel, values)`` 只接受声明字段、按类型强转后写入 overlay dict（防注入任意键）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 占位符判定（与 config_check 同源思路）：空、YOUR_*、<...>、changeme 等视为未填。
_PLACEHOLDER_TOKENS = ("your_", "<", "changeme", "xxxx", "请填写", "填写", "placeholder")


def _is_placeholder(val: Any) -> bool:
    s = str(val if val is not None else "").strip()
    if not s:
        return True
    low = s.lower()
    return any(tok in low for tok in _PLACEHOLDER_TOKENS)


@dataclass
class Field:
    key: str                       # 点分 config 路径，如 "telegram.api_id"
    label: str
    required: bool = True
    secret: bool = False           # 密钥类：状态接口回显时打码
    type: str = "str"             # str | int | bool
    help: str = ""                 # 去哪拿 / 填什么


@dataclass
class Channel:
    id: str
    name: str
    enable_key: str                # 启用开关的 config 路径
    fields: List[Field] = field(default_factory=list)
    login_required: bool = False   # 填完后是否还需扫码/登录（交棒现有登录流程）
    intro: str = ""
    #: 「用自己的账号登录」那条路对应的平台键（扫码 / 协议 / 网页登录，走收件箱接入抽屉）。
    #: 空＝该渠道没有账号形态（网页客服是服务端原生渠道，开启即用）。
    #: 这条路是绝大多数客户真正会走的——官方 API 那条要开发者账号与回调域名，
    #: 向导此前只呈现后者，等于把劝退项摆在首屏、把卖点藏进二级页面。
    login_platform: str = ""
    #: 官方 API 那条路的一句话说明（与 intro 分工：intro 描述整个渠道）。
    api_intro: str = ""
    # 必填字段全部就绪时顺带置 true 的 config 路径（声明式桥接）：修「凭据填了但
    # 登录开关没人开」的断层——如 Telegram 填齐 api_id/api_hash 后若不开
    # platform_login.telegram.protocol_enabled + orchestrator_enabled，接入弹窗的
    # 「协议多开」永远灰着、扫上的号也不会被编排器拉上线，而这两个键没有别的
    # 产品入口会写。语义：只把「从未设置」的键补成 true；显式 false 尊重不动。
    enable_on_ready: List[str] = field(default_factory=list)


CHANNELS: List[Channel] = [
    Channel(
        id="telegram",
        name="Telegram",
        enable_key="telegram.enabled",
        login_required=True,
        login_platform="telegram",
        intro="用你自己的 Telegram 账号登录即可开始收发消息。",
        api_intro="自备 API 凭证（官网未自动派发时才需要，去 my.telegram.org 申请）。",
        enable_on_ready=[
            "platform_login.telegram.protocol_enabled",
            "platform_login.orchestrator_enabled",
        ],
        fields=[
            Field("telegram.api_id", "API ID", type="int",
                  help="https://my.telegram.org → API development tools 获取"),
            Field("telegram.api_hash", "API Hash", secret=True,
                  help="同 my.telegram.org 页面，与 API ID 配对"),
            Field("telegram.phone_number", "手机号", required=False,
                  help="可选；登录账号时也可现填，格式 +8613800000000"),
        ],
    ),
    Channel(
        id="line",
        name="LINE",
        enable_key="line.enabled",
        login_platform="line",
        intro="用你自己的 LINE 账号扫码登录（手机上输 6 位验证码即可）。",
        api_intro="改用 LINE 官方账号（Messaging API）：需要 LINE Developers 控制台与回调域名。",
        fields=[
            Field("line.channel_access_token", "Channel Access Token", secret=True,
                  help="Messaging API 设置页签发的长期 token"),
            Field("line.channel_secret", "Channel Secret", secret=True,
                  help="频道基本设置页的 Channel secret"),
        ],
    ),
    Channel(
        id="whatsapp",
        name="WhatsApp",
        # 没有「whatsapp.enabled」这种顶层键：WhatsApp 在本仓只有两个真实形态——
        # 扫码协议接入（Baileys）与官方 Cloud API。这里挂扫码那条路的开关，
        # 免得为了凑格式发明一个没人读的配置键。
        enable_key="platform_login.whatsapp.protocol_enabled",
        login_platform="whatsapp",
        intro="用你自己的 WhatsApp 账号扫码登录（手机 → 已关联的设备）。",
        # 刻意没有凭证字段：WhatsApp Cloud API 在本仓有 webhook 实现但没有产品级
        # 配置入口，硬塞一个半成品表单只会制造「填了也不通」。扫码是唯一在跑的路。
        fields=[],
    ),
    Channel(
        id="messenger",
        name="Facebook Messenger",
        enable_key="facebook_messenger.enabled",
        login_platform="messenger",
        intro="在服务器的隔离浏览器里登录你的 Facebook 账号（账密 / 2FA，不用二维码）。",
        api_intro="改用 Meta 官方 Page 接入：需要 Meta 开发者后台的 App 与 Webhook 回调。",
        fields=[
            Field("facebook_messenger.page_access_token", "Page Access Token", secret=True,
                  help="绑定主页后签发的 Page token"),
            Field("facebook_messenger.verify_token", "Verify Token",
                  help="自定义任意字符串，需与 Webhook 配置一致"),
            Field("facebook_messenger.app_secret", "App Secret", secret=True, required=False,
                  help="可选；用于校验 Webhook 签名（X-Hub-Signature）"),
        ],
    ),
    Channel(
        id="web",
        name="网页客服 Widget",
        enable_key="web_chat.enabled",
        intro="服务端原生渠道，无需第三方凭证，开启即用。",
        fields=[],
    ),
]

_CHANNEL_BY_ID: Dict[str, Channel] = {c.id: c for c in CHANNELS}


def _dig(config: Dict[str, Any], dotted: str) -> Any:
    cur: Any = config
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _mask(val: Any) -> str:
    """密钥回显打码：保留首尾各 2 位。"""
    s = str(val if val is not None else "")
    if len(s) <= 4:
        return "•" * len(s)
    return s[:2] + "•" * max(3, len(s) - 4) + s[-2:]


def get_channel(channel_id: str) -> Optional[Channel]:
    return _CHANNEL_BY_ID.get(str(channel_id or "").lower())


#: 托管版「凭据由官网自动派发」的渠道 → 需要隐藏的字段（用户不该看见这些黑话）。
#: 判据是配置里的托管标记（由 hosted_gateway.ensure_hosted_telegram 注入），
#: **不是**光看 managed env——没真拿到凭据时必须保留手填路径，否则砸掉唯一出路。
_AUTO_PROVISIONED: Dict[str, Dict[str, Any]] = {
    "telegram": {
        "flag": "telegram._hosted_cred",
        "hide": ("telegram.api_id", "telegram.api_hash"),
        "intro": "凭据已由官网自动配置（无需申请 API ID / Hash）——直接去登录你的账号即可。",
    },
}


def _auto_provisioned(config: Dict[str, Any], channel_id: str) -> Optional[Dict[str, Any]]:
    """该渠道是否已由官网托管派发凭据（真拿到了才算）。"""
    spec = _AUTO_PROVISIONED.get(channel_id)
    if not spec:
        return None
    return spec if bool(_dig(config, str(spec["flag"]))) else None


def channel_status(
    config: Dict[str, Any],
    *,
    accounts_by_platform: Optional[Dict[str, int]] = None,
    login_ready: Optional[Dict[str, bool]] = None,
) -> List[Dict[str, Any]]:
    """每渠道接入现状：两条接入路径各自的状态 + 「能不能收发消息」。

    托管派发（``_AUTO_PROVISIONED``）命中时：隐藏已自动配置的凭据字段并换成人话 intro。

    ``accounts_by_platform``：各平台已接入的账号数（``account_registry``，不含 removed）；
    ``login_ready``：各平台「扫码/协议登录这条路今天通不通」（``platform_readiness``）。
    两者都可省略——纯函数与离线调用方（CLI、测试）照旧只按配置判断。

    **``ready`` 的口径是「这个渠道现在能不能收发消息」**，不是「yaml 填没填」。
    旧口径 ``configured and enabled`` 两头都不准：一台挂着在跑的 Telegram 账号、
    消息正在收发，向导却显示「已就绪 0/4」（登录进来的账号根本不写那些 yaml 键）；
    反过来，填了 api_id/api_hash 但一个号都没登录，旧口径却算它就绪。
    现在：**有已接入账号** → 就绪；或者**官方 API 凭据齐且启用**（LINE/Messenger
    那种填完就能收 webhook 的形态）→ 就绪。``login_required`` 的渠道（Telegram 的
    凭据只是登录的前置、本身不通消息）不吃第二条。
    """
    config = config or {}
    accounts_by_platform = accounts_by_platform or {}
    login_ready = login_ready or {}
    out: List[Dict[str, Any]] = []
    for ch in CHANNELS:
        enabled = bool(_dig(config, ch.enable_key))
        auto = _auto_provisioned(config, ch.id)
        hidden = set(auto["hide"]) if auto else set()
        fields_status = []
        missing: List[str] = []
        for fld in ch.fields:
            if fld.key in hidden:
                continue  # 官网已派发，不向用户暴露这些字段
            raw = _dig(config, fld.key)
            filled = not _is_placeholder(raw)
            if fld.required and not filled:
                missing.append(fld.label)
            disp = ""
            if filled:
                disp = _mask(raw) if fld.secret else str(raw)
            fields_status.append({
                "key": fld.key, "label": fld.label, "required": fld.required,
                "secret": fld.secret, "type": fld.type, "help": fld.help,
                "filled": filled, "display": disp,
            })
        # 托管派发：凭据已就绪 → 视为已配置（不因隐藏了字段而误判「未配置」）
        configured = True if auto else ((not missing) and (bool(ch.fields) or enabled))

        # 路径一：用自己的账号登录（扫码 / 协议 / 服务器托管登录）
        login_path: Optional[Dict[str, Any]] = None
        linked = 0
        if ch.login_platform:
            linked = int(accounts_by_platform.get(ch.login_platform) or 0)
            login_path = {
                "platform": ch.login_platform,
                "accounts": linked,
                # 缺省 True：拿不到诊断信号时不要凭空把入口画成灰的（宁可让用户
                # 点进去看到真实原因，也不要在向导里谎报「不可用」）。
                "available": bool(login_ready.get(ch.login_platform, True)),
                "deeplink": f"/workspace?drawer=1&connect={ch.login_platform}",
            }
        # 路径二：企业官方 API（凭据表单）——只有声明了字段的渠道才有
        api_path: Optional[Dict[str, Any]] = None
        if ch.fields:
            api_path = {
                "intro": ch.api_intro,
                "fields": fields_status,
                "missing": missing,
                "configured": bool(not missing),
                # 凭据本身不通消息、只是登录前置（Telegram）→ 前端别把它讲成「另一条路」
                "is_transport": not ch.login_required,
            }

        if linked > 0:
            ready, ready_by = True, "login"
        elif api_path and api_path["configured"] and enabled and api_path["is_transport"]:
            ready, ready_by = True, "api"
        elif not ch.fields and not ch.login_platform:
            ready, ready_by = bool(enabled), ("native" if enabled else "")
        else:
            ready, ready_by = False, ""

        out.append({
            "id": ch.id, "name": ch.name, "enable_key": ch.enable_key,
            "enabled": enabled,
            "intro": (auto["intro"] if auto else ch.intro),
            "login_required": ch.login_required,
            "fields": fields_status, "missing": missing,
            "configured": configured,
            "auto_provisioned": bool(auto),
            "login_platform": ch.login_platform,
            "linked_accounts": linked,
            "paths": {"login": login_path, "api": api_path},
            "ready": ready,
            "ready_by": ready_by,
        })
    return out


def _coerce(value: Any, typ: str) -> Any:
    if typ == "int":
        return int(str(value).strip())
    if typ == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return str(value)


def _set_dotted(target: Dict[str, Any], dotted: str, value: Any) -> None:
    cur = target
    parts = dotted.split(".")
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _get_dotted(source: Any, dotted: str) -> Any:
    cur = source
    for p in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _required_ready(
    ch: Channel, overlay: Dict[str, Any], base_config: Optional[Dict[str, Any]],
) -> bool:
    """该渠道所有必填字段是否已就绪（overlay 优先，缺的回看运行 config）。"""
    for f in ch.fields:
        if not f.required:
            continue
        val = _get_dotted(overlay, f.key)
        if _is_placeholder(val) and base_config is not None:
            val = _get_dotted(base_config, f.key)
        if _is_placeholder(val):
            return False
    return True


def apply_channel_values(
    overlay: Dict[str, Any], channel: str, values: Dict[str, Any],
    *, base_config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """把 values 中的**已声明字段**写入 overlay（就地），并自动置 enabled=true。

    - 只认 channel 规格里的 field.key（其它键忽略，防注入任意配置）；
    - 空值跳过（不覆盖已有）；类型按 field.type 强转，转换失败即报错；
    - 写入任一字段后顺带把该渠道 enable_key 设为 true；
    - 必填字段全部就绪（overlay 优先，缺的回看 ``base_config``）时，把渠道声明的
      ``enable_on_ready`` 路径补成 true——显式 false 视为管理员手工关闭，不扳回。
    返回 (成功?, 说明)。
    """
    ch = get_channel(channel)
    if ch is None:
        return False, f"未知渠道: {channel}"
    values = values or {}
    by_key = {f.key: f for f in ch.fields}
    # 兼容前端用短键（api_id）或全路径（telegram.api_id）
    short_map = {f.key.split(".")[-1]: f for f in ch.fields}
    wrote = False
    for raw_key, raw_val in values.items():
        fld = by_key.get(raw_key) or short_map.get(raw_key)
        if fld is None:
            continue
        if _is_placeholder(raw_val):
            continue
        try:
            coerced = _coerce(raw_val, fld.type)
        except Exception:
            return False, f"字段 {fld.label} 值无效（应为 {fld.type}）"
        _set_dotted(overlay, fld.key, coerced)
        wrote = True
    # web 等无字段渠道：仅开启
    if wrote or not ch.fields:
        _set_dotted(overlay, ch.enable_key, True)
    # 必填凭据齐全 → 顺带打开声明的配套开关（enable_on_ready 声明式桥接）
    if ch.enable_on_ready and _required_ready(ch, overlay, base_config):
        for dotted in ch.enable_on_ready:
            cur = _get_dotted(overlay, dotted)
            if cur is None and base_config is not None:
                cur = _get_dotted(base_config, dotted)
            if cur is False:  # 显式关过 → 尊重管理员决定
                continue
            _set_dotted(overlay, dotted, True)
    return True, "ok"
