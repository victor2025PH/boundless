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
    #: 纯官方 API 渠道（Instagram / Zalo 这类没有「自己账号扫码」形态的）：凭证保存
    #: 成功后要自动在账号注册表开一行 ``mode=official`` 的账号，编排器才会把官方
    #: 出站 worker 拉上线、收件箱账号栏才会出现该平台分组。值＝注册表 platform 名。
    #: 空＝无此语义（扫码类渠道的账号由登录流程落库，不走这里）。
    official_platform: str = ""
    #: official 账号 id 取哪个 config 键（如 ``instagram.ig_id``）。取值为空时回落
    #: ``"official"``——**必须与对应 webhook 入站镜像的 account_id 口径一致**
    #: （instagram_webhook: ``ig_id or "official"``；zalo_webhook: ``oa_id or
    #: "official"``），否则入站会话与出站 worker 的账号对不上，坐席能看见却回不了。
    official_account_id_key: str = ""
    #: 该渠道凭证的官方后台入口（如 Zalo OA 后台 / Meta 开发者控制台）。
    #: 单一事实源：接入弹窗「准备清单」的直达链接与向导官方 API 区的入口链接都读它
    #: （2026-08-10 前弹窗侧是 JS 硬编码表 `_OFFICIAL_CONSOLE`，现降级为旧后端回落）。
    #: 空＝该渠道无官方后台语义，前端不渲染链接行。
    console_url: str = ""
    #: 「个人号登录」路径的**门控开关**（config 点分路径）。非空时，仅当该键为真才对外
    #: 呈现 login 路径——用于给纯官方渠道（Instagram/Zalo）**增量、可回退**地补一条个人号
    #: 扫码登录路径：未开时整条渠道现状与官方 API 独存时**逐字节一致**（不动
    #: DEFAULT_PLATFORM_MODES 静态声明、不改既有官方漏斗与其浏览器门禁），开了才点亮。
    #: 空＝该 login 路径无条件呈现（Telegram/LINE/WhatsApp/Messenger 的既有语义，不受影响）。
    login_gated_by: str = ""
    #: 个人号登录路径随附的「稳定运行建议」i18n 键（2026-08-10 起为正向最佳实践口径，
    #: 不再作风险恐吓）。非空时随 login_path 下发，向导在个人号主卡渲一条建议提示。
    #: 单一事实源＝与 /modes 的 notice 同键，保证「弹窗」与「向导」两处话术一致。
    #: 空＝不渲染（官方渠道等无需建议的形态）。
    login_notice_key: str = ""


CHANNELS: List[Channel] = [
    Channel(
        id="telegram",
        name="Telegram",
        enable_key="telegram.enabled",
        login_required=True,
        login_platform="telegram",
        intro="用你自己的 Telegram 账号登录即可开始收发消息。",
        api_intro="自备 API 凭证（官网未自动派发时才需要，去 my.telegram.org 申请）。",
        console_url="https://my.telegram.org/",
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
        official_platform="line",
        official_account_id_key="line.account_id",
        console_url="https://developers.line.biz/console/",
        # 官方凭证齐 → 打开编排器，否则 mode=official 行落了库也不会被拉上线
        enable_on_ready=["platform_login.orchestrator_enabled"],
        intro="用你自己的 LINE 账号扫码登录（手机上输 6 位验证码即可）。",
        api_intro=(
            "改用 LINE 官方账号（Messaging API）：需要 LINE Developers 控制台与回调域名；"
            "官方通道发图/语音须另配公网媒体 URL（下方字段，IG/LINE 共用）。"
        ),
        fields=[
            Field("line.channel_access_token", "Channel Access Token", secret=True,
                  help="Messaging API 设置页签发的长期 token"),
            Field("line.channel_secret", "Channel Secret", secret=True,
                  help="频道基本设置页的 Channel secret"),
            Field("line.account_id", "官方账号 ID", required=False,
                  help="可选；官方账号标识（留空按 official 记账）"),
            Field("official_media.public_base_url", "公网媒体 URL（IG/LINE 共用）",
                  required=False,
                  help="官方通道发图/语音用：本服务的公网 https 地址（如经隧道/反代暴露的 "
                       "https://bot.example.com）。留空则官方通道仅能发文字。"),
        ],
    ),
    Channel(
        id="whatsapp",
        name="WhatsApp",
        # 双路径：扫码（Baileys / protocol）+ 官方 Cloud API（mode=official）。
        # enable_key 挂 Cloud 开关——与 LINE/Messenger 同构（官方凭证保存即置 true）；
        # 扫码就绪不靠这个键，靠注册表已接入账号数（channel_status ready_by=login）。
        enable_key="whatsapp_cloud.enabled",
        login_platform="whatsapp",
        official_platform="whatsapp",
        official_account_id_key="whatsapp_cloud.phone_number_id",
        # Cloud API 的 Phone Number ID / token / App Secret 都在 Meta 开发者应用面板
        console_url="https://developers.facebook.com/apps/",
        enable_on_ready=["platform_login.orchestrator_enabled"],
        intro="用你自己的 WhatsApp 账号扫码登录（手机 → 已关联的设备）。",
        api_intro=(
            "改用 Meta WhatsApp Cloud API（合规 Business 通道）：需要 Meta 开发者后台的 "
            "WhatsApp Business App，把 Webhook 指到 https://<你的域名>/wa/webhook 并订阅 messages。"
            "出站媒体走 media_id 直传，无需另配公网媒体 URL。"
        ),
        fields=[
            Field("whatsapp_cloud.phone_number_id", "Phone Number ID",
                  help="Cloud API 的 Phone Number ID（不是手机号；Meta 后台 WhatsApp → API 设置）"),
            Field("whatsapp_cloud.access_token", "Access Token", secret=True,
                  help="永久/系统用户 access token（whatsapp_business_messaging 权限）"),
            Field("whatsapp_cloud.app_secret", "App Secret", secret=True,
                  help="Meta App 的 App Secret，用于 Webhook 签名校验（X-Hub-Signature-256）"),
            Field("whatsapp_cloud.verify_token", "Verify Token",
                  help="自定义任意字符串，需与 Meta 后台 Webhook 配置里的一致"),
        ],
    ),
    Channel(
        id="messenger",
        name="Facebook Messenger",
        enable_key="facebook_messenger.enabled",
        login_platform="messenger",
        official_platform="messenger",
        official_account_id_key="facebook_messenger.page_id",
        console_url="https://developers.facebook.com/apps/",
        enable_on_ready=["platform_login.orchestrator_enabled"],
        intro="在服务器的隔离浏览器里登录你的 Facebook 账号（账密 / 2FA，不用二维码）。",
        api_intro=(
            "改用 Meta 官方 Page 接入（官方合规通道）：需要 Meta 开发者后台的 App，"
            "把 Webhook 指到 https://<你的域名>/fb/webhook 并订阅 messages。"
        ),
        fields=[
            Field("facebook_messenger.page_access_token", "Page Access Token", secret=True,
                  help="绑定主页后签发的 Page token"),
            Field("facebook_messenger.verify_token", "Verify Token",
                  help="自定义任意字符串，需与 Webhook 配置一致"),
            # 2026-08-10 升必填：webhook 入站对空 app_secret 硬拒（无签名校验的公网
            # 回调不该放行），「向导说可选、入站却拒收」的口径分裂修复为同一门。
            Field("facebook_messenger.app_secret", "App Secret", secret=True,
                  help="Meta App 的 App Secret，用于 Webhook 签名校验"
                       "（X-Hub-Signature-256）；不填则入站消息不放行"),
            Field("facebook_messenger.page_id", "Page ID", required=False,
                  help="可选；主页 id——多 Page 同 App 时用于过滤事件（留空按 official 记账）"),
        ],
    ),
    Channel(
        id="instagram",
        name="Instagram",
        enable_key="instagram.enabled",
        official_platform="instagram",
        official_account_id_key="instagram.ig_id",
        console_url="https://developers.facebook.com/apps/",
        enable_on_ready=["platform_login.orchestrator_enabled"],
        # 个人号网页托管登录路径（Playwright 边车，mode=web/hosted）——门控于开关，未开则
        # 整条渠道与官方 Graph 独存时一致。开了才在向导呈现「用你自己的 IG 账号登录」主路径。
        login_platform="instagram",
        login_gated_by="platform_login.instagram.web_enabled",
        login_notice_key="inbox.connect.notice_unofficial",
        intro="接入 Instagram 专业账号私信（Meta 官方 Graph API）：填好凭证即可收发 DM。",
        api_intro=(
            "需要 Meta 开发者后台的 App：把 Webhook 回调指到 https://<你的域名>/ig/webhook "
            "并订阅 messages；发图/发语音须填「公网媒体 URL」（IG/LINE 共用）。"
        ),
        fields=[
            Field("instagram.ig_id", "IG 专业账号 ID",
                  help="Graph API 里的 IG 专业账号 id（17 位数字，Meta 后台可查）"),
            Field("instagram.page_access_token", "Page Access Token", secret=True,
                  help="关联 Facebook Page 的长期 token（需 instagram_manage_messages 权限）"),
            Field("instagram.app_secret", "App Secret", secret=True,
                  help="Meta App 的 App Secret，用于 Webhook 签名校验（X-Hub-Signature-256）"),
            Field("instagram.verify_token", "Verify Token",
                  help="自定义任意字符串，需与 Meta 后台 Webhook 配置里的一致"),
            Field("official_media.public_base_url", "公网媒体 URL（IG/LINE 共用）",
                  required=False,
                  help="官方通道发图/语音用：本服务的公网 https 地址（如经隧道/反代暴露的 "
                       "https://bot.example.com）。留空则官方通道仅能发文字。"),
        ],
    ),
    Channel(
        id="qq",
        name="QQ",
        # 启用开关 = 协议登录开关本身：填好协议端地址即视为 opt-in（非官方接入，默认关）
        enable_key="platform_login.qq.protocol_enabled",
        login_required=True,
        login_platform="qq",
        # 这是「用你自己的 QQ 号」那条路（准入区，非官方接入）；官方机器人是另一张卡 qqbot
        login_notice_key="inbox.connect.notice_unofficial",
        enable_on_ready=["platform_login.orchestrator_enabled"],
        intro=(
            "用你自己的 QQ 号收发消息：先在电脑/服务器上装一个 QQ 协议端（NapCat / LLOneBot / "
            "Lagrange，任选其一，均支持 Milky 协议），在它的界面里扫码登录 QQ，再把它的 Milky "
            "服务地址填到这里。与「QQ 机器人」（官方开放平台）是两个独立渠道。"
        ),
        api_intro=(
            "协议端不随本软件分发（许可证原因），请按各项目文档安装并开启 Milky 服务；"
            "建议使用小号 + 固定 IP。填好地址后到工作台「账号 → 新增 QQ」确认接入。"
        ),
        fields=[
            Field("platform_login.qq.milky_url", "协议端 Milky 地址",
                  help="协议端 Milky 服务的 http 地址，如 http://127.0.0.1:3000（NapCat/LLOneBot/Lagrange 均在其设置里可查）"),
            Field("platform_login.qq.milky_token", "协议端 Token", secret=True, required=False,
                  help="协议端 Milky 服务设置的 access_token（强烈建议设置；留空=协议端未设 token）"),
        ],
    ),
    Channel(
        id="qqbot",
        name="QQ 机器人",
        enable_key="qqbot.enabled",
        official_platform="qqbot",
        # account_id 口径 = qqbot.app_id（qq_official.register_qqbot_routes 入站镜像同源）
        official_account_id_key="qqbot.app_id",
        console_url="https://q.qq.com/",
        enable_on_ready=["platform_login.orchestrator_enabled"],
        intro=(
            "接入 QQ 开放平台机器人（官方 API）：填好 AppID / AppSecret 即可收发单聊与群 @ 消息。"
            "与「QQ 协议登录」（用你自己的 QQ 号）是两个独立渠道。"
        ),
        api_intro=(
            "去 q.qq.com 创建机器人拿 AppID / AppSecret；默认 WebSocket 直连（免公网），"
            "正式环境须在开放平台填 IP 白名单，联调可先勾「沙箱」。"
            "注意：官方机器人只能被动回复（单聊每条来话 60 分钟内最多 4 条、群 5 分钟 5 条），"
            "不支持主动消息；发图/语音属下一批次，界面会自动置灰。"
        ),
        fields=[
            Field("qqbot.app_id", "AppID",
                  help="QQ 开放平台 → 机器人 → 开发设置 里的 AppID（机器人 ID）"),
            Field("qqbot.app_secret", "AppSecret", secret=True,
                  help="同页 AppSecret；用于换取 access_token 与 Webhook 验签"),
            Field("qqbot.sandbox", "沙箱环境", required=False, type="bool",
                  help="true=沙箱（只收沙箱配置里的群/单聊事件，提审前联调用）；上线后改 false"),
            Field("qqbot.connect_mode", "连接方式", required=False,
                  help="websocket（默认，单机免公网，需 IP 白名单）或 webhook（需公网 HTTPS 回调）"),
            Field("qqbot.webhook_path", "Webhook 路径", required=False,
                  help="仅 webhook 方式用；默认 /qqbot/webhook，开放平台回调地址填 https://<域名>/qqbot/webhook"),
        ],
    ),
    Channel(
        id="zalo",
        name="Zalo",
        enable_key="zalo.enabled",
        official_platform="zalo",
        official_account_id_key="zalo.oa_id",
        console_url="https://oa.zalo.me/",
        enable_on_ready=["platform_login.orchestrator_enabled"],
        # 个人号扫码登录路径（zca-js 边车，mode=web/qr）——门控于开关，未开则整条渠道
        # 与官方 OA 独存时完全一致。开了才在向导呈现「用你自己的 Zalo 账号扫码」主路径。
        login_platform="zalo",
        login_gated_by="platform_login.zalo.web_enabled",
        login_notice_key="inbox.connect.notice_unofficial",
        intro="接入 Zalo 官方账号（OA API）：越南市场主流渠道，填好凭证即可收发文字消息。",
        api_intro=(
            "需要 Zalo OA 后台：把 Webhook 指到 https://<你的域名>/zalo/webhook。"
            "注意：Zalo OA API 暂不支持发送图片/语音（官方能力限制，界面会自动置灰）。"
        ),
        fields=[
            Field("zalo.access_token", "OA Access Token", secret=True,
                  help="Zalo OA 后台签发的 access token"),
            Field("zalo.oa_secret", "OA Secret", secret=True, required=False,
                  help="可选；用于 Webhook 签名校验（X-ZEvent-Signature）"),
            Field("zalo.oa_id", "OA 账号 ID", required=False,
                  help="可选；官方账号 id（留空按 official 记账）"),
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
        # login_gated_by 非空时，仅当门控键为真才呈现——纯官方渠道（Zalo/IG）补个人号
        # 路径的可回退闸门：未开＝旧行为（官方独存），开了＝点亮个人号主路径。
        login_path: Optional[Dict[str, Any]] = None
        linked = 0
        if ch.login_platform and (
            not ch.login_gated_by or bool(_dig(config, ch.login_gated_by))
        ):
            linked = int(accounts_by_platform.get(ch.login_platform) or 0)
            login_path = {
                "platform": ch.login_platform,
                "accounts": linked,
                # 缺省 True：拿不到诊断信号时不要凭空把入口画成灰的（宁可让用户
                # 点进去看到真实原因，也不要在向导里谎报「不可用」）。
                "available": bool(login_ready.get(ch.login_platform, True)),
                "deeplink": f"/workspace?drawer=1&connect={ch.login_platform}",
                # 个人号「稳定运行建议」键（与 /modes 的 notice 同键，两处话术一致）；空则前端不渲染。
                "notice_key": ch.login_notice_key,
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
            "console_url": ch.console_url,
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
