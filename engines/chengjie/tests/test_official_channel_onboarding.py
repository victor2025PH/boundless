"""Instagram / Zalo 官方渠道接入闭环门禁（P0，2026-08-05）。

四条不变量（断一条=「接入向导填了也不通」级别的静默故障）：

1. **向导声明 ↔ webhook 真实 config 键逐字对齐**——渠道声明是给运营填表用的，
   键名漂移不会报错，只是填进去的凭证 webhook 永远读不到。
2. **official 账号自动开通的 account_id 口径 == webhook 入站镜像口径**
   （instagram: ``ig_id or "official"``；zalo: ``oa_id or "official"``）——错位则
   入站会话与出站 worker 对不上号，坐席看得见客户消息却发不出去。
3. **官方通道能力位与 send_media 运行时分支一致**——``owns_media`` 按 hasattr 判
   恒 True，Zalo 实际 not_supported、LINE/IG 未配公网 URL 时 no_public_url；
   能力位不收敛=坐席「按钮能点、点了报错」。
4. **platform_login / platform_readiness 的 official 元数据自洽**——modes 表、
   credentials 登录形态、缺凭证诊断（指路接入向导而非 Telegram 专属面板）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.integrations import platform_readiness as PR
from src.integrations.official_api_worker import (
    OFFICIAL_MEDIA_URL_PLATFORMS,
    OFFICIAL_PLATFORMS,
    OfficialApiWorker,
    official_send_caps,
)
from src.integrations.platform_login import (
    DEFAULT_PLATFORM_MODES,
    MODE_CAPS,
    MODE_DESC_KEYS,
    MODE_LABEL_KEYS,
    SUPPORTED_PLATFORMS,
    list_modes,
    login_kind,
    mode_available,
)
from src.utils.channel_setup import CHANNELS, channel_status, get_channel

_ROOT = Path(__file__).resolve().parents[1]


def _webhook_source(name: str) -> str:
    return (_ROOT / "src" / "integrations" / name).read_text(encoding="utf-8")


# ── 1. 向导声明 ↔ webhook config 键对齐 ─────────────────────────────────────

def test_instagram_channel_fields_match_webhook_keys():
    ch = get_channel("instagram")
    assert ch is not None, "instagram 渠道声明缺席（向导不会出现该卡片）"
    assert ch.enable_key == "instagram.enabled"
    src = _webhook_source("instagram_webhook.py")
    for f in ch.fields:
        block, _, key = f.key.partition(".")
        if block == "official_media":
            continue  # IG/LINE 共用的公网媒体 URL，归属由 registry_consistency 门禁钉
        assert block == "instagram", f.key
        assert f'cfg.get("{key}")' in src, (
            f"向导字段 {f.key} 在 instagram_webhook.py 里找不到对应的 cfg.get —— "
            "键名漂移，填了也不通")


def test_zalo_channel_fields_match_webhook_keys():
    ch = get_channel("zalo")
    assert ch is not None, "zalo 渠道声明缺席（向导不会出现该卡片）"
    assert ch.enable_key == "zalo.enabled"
    src = _webhook_source("zalo_webhook.py")
    for f in ch.fields:
        block, _, key = f.key.partition(".")
        assert block == "zalo", f.key
        assert f'cfg.get("{key}")' in src, (
            f"向导字段 {f.key} 在 zalo_webhook.py 里找不到对应的 cfg.get")


def test_official_channels_declare_console_url():
    """console_url＝「打开官方后台」链接的单一事实源（2026-08-10 起）。

    消费方：接入弹窗准备清单（_renderOfficialPrep 优先 API 值）+ 向导官方 API 区
    （setup.ch.console_link）。弹窗侧另有 `_OFFICIAL_CONSOLE` 回落表兜「旧后端还没
    重启」的过渡窗——回落表必须与声明镜像，这里三方一起钉，防「加渠道只改一边」。
    """
    expect_host = {
        "zalo": "oa.zalo.me", "instagram": "developers.facebook.com",
        "messenger": "developers.facebook.com",
        "whatsapp": "developers.facebook.com", "line": "developers.line.biz",
    }
    for cid, host in expect_host.items():
        ch = get_channel(cid)
        assert ch.console_url.startswith("https://") and host in ch.console_url, cid
    # channel_status 逐渠道透传（向导/弹窗都经它消费）
    st = next(c for c in channel_status({}) if c["id"] == "zalo")
    assert st["console_url"] == get_channel("zalo").console_url
    # telegram 的凭证后台（my.telegram.org）只服务向导；web 渠道无此语义保持空
    assert "my.telegram.org" in get_channel("telegram").console_url
    assert get_channel("web").console_url == ""
    # 模板回落表与声明镜像（防两边漂移）
    tpl = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    for cid in expect_host:
        assert f"{cid}:'{get_channel(cid).console_url}'" in tpl, (
            f"_OFFICIAL_CONSOLE 回落表与 channel_setup 声明不一致: {cid}")


def test_official_channels_have_no_login_path_and_are_api_transport():
    """纯官方渠道没有「自己账号扫码」形态；api 路径必须是真通道（is_transport）。"""
    for cid in ("instagram", "zalo"):
        ch = next(c for c in channel_status({}) if c["id"] == cid)
        assert ch["paths"]["login"] is None, cid
        api = ch["paths"]["api"]
        assert api is not None and api["is_transport"] is True, cid
        assert ch["ready"] is False  # 空配置不该误报就绪


def test_official_channel_ready_when_configured_and_enabled():
    cfg = {"instagram": {
        "enabled": True, "ig_id": "17841400000000000",
        "page_access_token": "EAAx", "app_secret": "s", "verify_token": "v",
    }}
    ch = next(c for c in channel_status(cfg) if c["id"] == "instagram")
    assert ch["configured"] is True and ch["ready"] is True
    assert ch["ready_by"] == "api"


# ── 2. official 账号自动开通口径 ────────────────────────────────────────────

def test_webhook_account_id_fallback_is_official():
    """webhook 侧 account_id 口径（``X or "official"``）被本门禁钉住：改了它，
    _provision_official_account 的回落值必须同步改，否则两边对不上。"""
    assert 'ig_account_id = ig_id or "official"' in _webhook_source(
        "instagram_webhook.py")
    assert '"official"' in _webhook_source("zalo_webhook.py")
    for cid, key in (("instagram", "instagram.ig_id"), ("zalo", "zalo.oa_id")):
        ch = get_channel(cid)
        assert ch.official_platform == cid
        assert ch.official_account_id_key == key


def test_provision_official_account_upserts_registry():
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _provision_official_account,
    )

    cfg = {"instagram": {
        "enabled": True, "ig_id": "1784140000",
        "page_access_token": "EAAx", "app_secret": "s", "verify_token": "v",
    }}
    acc = _provision_official_account("instagram", cfg)
    assert acc == "1784140000"
    row = get_account_registry().get("instagram", "1784140000")
    assert row is not None
    assert row.get("mode") == "official"
    assert row.get("status") == "online"


def test_provision_official_account_falls_back_to_official_id():
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _provision_official_account,
    )

    cfg = {"zalo": {"enabled": True, "access_token": "tok"}}
    acc = _provision_official_account("zalo", cfg)
    assert acc == "official", "oa_id 留空必须回落 official（与 webhook 镜像口径一致）"
    assert get_account_registry().get("zalo", "official") is not None


def test_provision_official_account_skips_when_creds_missing():
    from src.web.routes.unified_inbox_setup_routes import (
        _provision_official_account,
    )

    # 必填凭证没齐 → 不开账号（开了也起不来，反而在账号栏挂错误行）
    assert _provision_official_account(
        "instagram", {"instagram": {"ig_id": "1"}}) == ""
    # 非官方渠道 → 无此语义
    assert _provision_official_account("telegram", {"telegram": {}}) == ""


def test_provision_messenger_official_uses_page_id():
    """Messenger 官方第二路（P1）：account_id 口径 = page_id or "official"，
    与 facebook_webhook 入站镜像一致。"""
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _provision_official_account,
    )

    assert 'account_id=(page_id or "official")' in _webhook_source(
        "facebook_webhook.py")
    # app_secret 2026-08-10 起升必填（webhook 对空 app_secret 硬拒收，向导口径同门）
    cfg = {"facebook_messenger": {
        "enabled": True, "page_access_token": "EAAx", "verify_token": "v",
        "app_secret": "s", "page_id": "10099",
    }}
    assert _provision_official_account("messenger", cfg) == "10099"
    row = get_account_registry().get("messenger", "10099")
    assert row is not None and row.get("mode") == "official"


def test_provision_line_official_uses_line_account_id():
    """LINE 官方第二路：account_id 口径 = line.account_id or "official"，
    与 line_webhook 入站镜像一致。"""
    from src.web.routes.unified_inbox_setup_routes import (
        _provision_official_account,
    )

    assert 'cfg.get("account_id") or "official"' in _webhook_source(
        "line_webhook.py")
    cfg = {"line": {"enabled": True, "channel_access_token": "t",
                    "channel_secret": "s"}}
    assert _provision_official_account("line", cfg) == "official"


# ── 3. 能力位与运行时分支一致 ───────────────────────────────────────────────

async def test_zalo_caps_agree_with_runtime_not_supported():
    caps = official_send_caps("zalo", {})
    assert caps["can_media"] is False and caps["can_voice"] is False
    assert caps["reason"] == "zalo_api_no_media"

    w = OfficialApiWorker(
        {"platform": "zalo", "account_id": "official",
         "meta": {"access_token": "x"}}, {})
    out = await w.send_media("zalo:user:1", media_path="x.jpg",
                             media_type="image", caption="")
    assert out["delivered"] is False
    assert out["error_kind"] == "not_supported", (
        "运行时分支与能力位表述不一致 —— 改了 send_media 分支必须同步改 "
        "official_send_caps")


@pytest.mark.parametrize("platform", list(OFFICIAL_MEDIA_URL_PLATFORMS))
def test_url_platforms_caps_follow_public_base_url(platform):
    off = official_send_caps(platform, {})
    assert off["can_media"] is False and off["reason"] == "needs_public_url"
    on = official_send_caps(
        platform, {"official_media": {"public_base_url": "https://x.example"}})
    assert on["can_media"] is True and on["reason"] == ""


async def test_instagram_runtime_no_public_url_agrees_with_caps():
    w = OfficialApiWorker(
        {"platform": "instagram", "account_id": "official",
         "meta": {"page_access_token": "x", "ig_id": "1"}}, {})
    out = await w.send_media("ig:user:1", media_path="x.jpg",
                             media_type="image", caption="")
    assert out["delivered"] is False
    assert out["error_kind"] == "no_public_url"


def test_direct_upload_platforms_report_capable():
    for platform in ("whatsapp", "messenger"):
        caps = official_send_caps(platform, {})
        assert caps["can_media"] is True and caps["reason"] == "", platform


def test_caps_covers_every_official_platform():
    for platform in OFFICIAL_PLATFORMS:
        caps = official_send_caps(platform, {})
        assert set(caps) >= {"can_media", "can_voice", "reason"}, platform


# ── 4. platform_login / readiness 元数据自洽 ────────────────────────────────

def test_platforms_registered_with_official_mode():
    for p in ("instagram", "zalo"):
        assert p in SUPPORTED_PLATFORMS
        assert DEFAULT_PLATFORM_MODES[p] == {
            "modes": ["official"], "default": "official"}
        assert login_kind(p, "official") == "credentials"
    assert "official" in MODE_LABEL_KEYS and "official" in MODE_DESC_KEYS
    assert "official" in MODE_CAPS


def test_line_messenger_whatsapp_expose_official_as_secondary_mode():
    """LINE/Messenger/WhatsApp 的 official 是合规第二路：进 modes 清单但不改默认方式。"""
    for p, default in (("line", "protocol"), ("messenger", "web"),
                       ("whatsapp", "protocol")):
        spec = DEFAULT_PLATFORM_MODES[p]
        assert "official" in spec["modes"], p
        assert spec["default"] == default, f"{p} 默认方式不该被 official 抢走"
        assert login_kind(p, "official") == "credentials"


def test_provision_whatsapp_cloud_uses_phone_number_id():
    """WhatsApp Cloud（P2）：account_id = phone_number_id，与 webhook 镜像一致。"""
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import (
        _provision_official_account,
    )

    ch = get_channel("whatsapp")
    assert ch.official_platform == "whatsapp"
    assert ch.official_account_id_key == "whatsapp_cloud.phone_number_id"
    assert ch.enable_key == "whatsapp_cloud.enabled"
    cfg = {"whatsapp_cloud": {
        "enabled": True, "phone_number_id": "1099887766",
        "access_token": "EAAx", "app_secret": "s", "verify_token": "v",
    }}
    assert _provision_official_account("whatsapp", cfg) == "1099887766"
    row = get_account_registry().get("whatsapp", "1099887766")
    assert row is not None and row.get("mode") == "official"


def test_readiness_line_messenger_whatsapp_official_diagnosis():
    """LINE/Messenger/WhatsApp official 模式的诊断走 channel_setup 声明。"""
    d = PR.diagnose_mode("messenger", "official", {})
    assert d["reason_code"] == PR.BLOCK_OFFICIAL_CREDS
    # app_secret 2026-08-10 起升必填（webhook 空 secret 拒收，向导/诊断同口径）
    ready = PR.diagnose_mode("messenger", "official", {"facebook_messenger": {
        "enabled": True, "page_access_token": "EAAx", "verify_token": "v",
        "app_secret": "s"}})
    hard = [b for b in ready["blockers"] if b["severity"] == PR.SEV_BLOCK]
    assert not hard
    wa = PR.diagnose_mode("whatsapp", "official", {})
    assert wa["reason_code"] == PR.BLOCK_OFFICIAL_CREDS
    wa_ok = PR.diagnose_mode("whatsapp", "official", {"whatsapp_cloud": {
        "enabled": True, "phone_number_id": "1", "access_token": "t",
        "app_secret": "s", "verify_token": "v"}})
    assert not [b for b in wa_ok["blockers"] if b["severity"] == PR.SEV_BLOCK]


def test_list_modes_official_carries_credentials_kind():
    modes = list_modes("instagram")
    assert [m["mode"] for m in modes] == ["official"]
    m = modes[0]
    assert m["login_kind"] == "credentials"
    assert m["label_key"] == "inbox.connect.mode_l_official"


def test_mode_available_official_requires_worker_factory():
    # 没注册工厂的平台恒 False（用杜撰平台名保证与全局注册表状态无关）
    assert mode_available("nosuchplatform", "official") is False
    from src.integrations import account_orchestrator as AO
    key = "gatetestplat:official"
    AO._WORKER_FACTORIES[key] = lambda acc, cfg: object()
    try:
        assert mode_available("gatetestplat", "official") is True
    finally:
        AO._WORKER_FACTORIES.pop(key, None)


def test_readiness_official_creds_missing_points_to_wizard():
    d = PR.diagnose_mode("instagram", "official", {})
    assert d["ready"] is False
    assert d["reason_code"] == PR.BLOCK_OFFICIAL_CREDS
    codes = [b["code"] for b in d["blockers"]]
    assert PR.BLOCK_OFFICIAL_CREDS in codes
    # params 只带纯字段名（散文归 i18n 文案模板管，防英文界面中英夹杂）
    blk = next(b for b in d["blockers"] if b["code"] == PR.BLOCK_OFFICIAL_CREDS)
    field = str((blk.get("params") or {}).get("field") or "")
    assert field, "缺凭证 blocker 必须点名缺哪些字段"
    assert "缺少" not in field, "散文前缀应在 rc_official_creds_missing_why 文案里，不在 params"


def test_readiness_official_ready_when_configured():
    cfg = {"instagram": {
        "enabled": True, "ig_id": "1", "page_access_token": "t",
        "app_secret": "s", "verify_token": "v",
    }}
    d = PR.diagnose_mode("instagram", "official", cfg)
    hard = [b for b in d["blockers"] if b["severity"] == PR.SEV_BLOCK]
    assert not hard and d["ready"] is True


def test_readiness_official_enabled_missing_uses_same_code():
    """凭证齐但 enabled 没开：处置一样是向导（保存自动置 enabled），码不该变。

    差异经结构化参数 ``state=switch_off`` 表达（前端据此选 rc_official_switch_off_*
    文案变体），不再把中文散文塞进 params。"""
    cfg = {"zalo": {"access_token": "tok"}}
    d = PR.diagnose_mode("zalo", "official", cfg)
    assert d["reason_code"] == PR.BLOCK_OFFICIAL_CREDS
    blk = next(b for b in d["blockers"] if b["code"] == PR.BLOCK_OFFICIAL_CREDS)
    assert (blk.get("params") or {}).get("state") == "switch_off"


async def test_media_url_probe_semantics():
    """公网媒体 URL 自检：空=unset、http=直接 fail（平台强制 https）、
    连不上=fail 带原因——只提示不拦截（语义边界见函数 docstring）。"""
    from src.web.routes.unified_inbox_setup_routes import _probe_public_media_url
    assert (await _probe_public_media_url(""))["state"] == "unset"
    out_http = await _probe_public_media_url("http://x.example")
    assert out_http["state"] == "fail" and out_http["detail"] == "must_be_https"
    out = await _probe_public_media_url("https://127.0.0.1:1")
    assert out["state"] == "fail" and out["detail"]


def test_i18n_keys_for_official_mode_exist_bilingually():
    from src.web.web_i18n import get_translations
    needed = (
        "inbox.connect.mode_l_official", "inbox.connect.mode_d_official",
        "inbox.connect.cap_compliant",
        "inbox.connect.rc_official_creds_missing_why",
        "inbox.connect.rc_official_creds_missing_how",
        "inbox.connect.bk_official_creds_missing",
        "inbox.connect.cred_go_wizard",
        "inbox.connect.instr_instagram", "inbox.connect.instr_zalo",
        "err.login.credentials_mode",
    )
    for lang in ("zh", "en"):
        tr = get_translations(lang)
        for key in needed:
            assert key in tr, f"{lang} 缺 i18n 键 {key}"
