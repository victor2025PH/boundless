"""平台清单一致性门禁（P1，2026-08-05）——治「五份平行清单各自漂移」。

背景：加一个平台要改 N 处散落的平台元组（前端 rail、登录、记忆键解析、pacing、
官方 worker），漏一处不是报错而是**静默功能残缺**（Instagram 曾经就是：官方出站
worker 写好了、接入 UI 清单里却没有它，半成品状态存活了一个月）。

本门禁不强行把各清单合并成一个 import（各处语义确有差异：episodic 刻意不含 web、
pacing 只管有拟人节奏的四端），而是把**清单之间的关系**钉死——谁是谁的子集、
差集必须恰好是登记过的刻意例外。新平台落地时漏改哪份清单，这里立刻点名。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.integrations.official_api_worker import OFFICIAL_PLATFORMS
from src.integrations.platform_login import (
    DEFAULT_PLATFORM_MODES,
    SUPPORTED_PLATFORMS,
)
from src.utils.channel_setup import CHANNELS

_ROOT = Path(__file__).resolve().parents[1]
_INBOX_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"

SUPPORTED = set(SUPPORTED_PLATFORMS)


def test_supported_platforms_is_reference_superset():
    """SUPPORTED_PLATFORMS 是全系统的参照系：模式表必须与之一一对应。"""
    assert set(DEFAULT_PLATFORM_MODES) == SUPPORTED, (
        "DEFAULT_PLATFORM_MODES 与 SUPPORTED_PLATFORMS 不同步")


def test_account_scope_migration_covers_all_platforms():
    """记忆归属迁移的平台前缀集 == 参照系（含 web；漏平台=该平台会话被归成畸形键）。"""
    from src.utils.account_scope_migration import KNOWN_PLATFORMS
    assert set(KNOWN_PLATFORMS) == SUPPORTED, (
        f"差集 {set(KNOWN_PLATFORMS) ^ SUPPORTED} —— 新平台落地漏改了这份清单")


def test_episodic_display_covers_all_but_web():
    """身份展示解析集 == 参照系 - {web}（web 访客是会话级匿名 id，无跨账号身份语义，
    刻意排除；除此以外的任何差集都是漏改）。"""
    from src.utils.episodic_identity_display import KNOWN_PLATFORMS
    assert set(KNOWN_PLATFORMS) == SUPPORTED - {"web"}, (
        f"差集 {set(KNOWN_PLATFORMS) ^ (SUPPORTED - {'web'})}")


def test_reply_pacing_platforms_are_supported_subset():
    """拟人节奏平台表 ⊆ 参照系（它只管有坐席手发节奏的端，可以少、不能有陌生名）。"""
    from src.inbox.reply_pacing_settings import PLATFORMS
    assert set(PLATFORMS) <= SUPPORTED


def test_channel_official_platforms_are_registered():
    """渠道向导声明的 official_platform 必须是官方 worker 真支持的平台。"""
    for ch in CHANNELS:
        if ch.official_platform:
            assert ch.official_platform in OFFICIAL_PLATFORMS, ch.id
            assert ch.official_platform in SUPPORTED, ch.id


def test_frontend_fixed_plats_match_login_channels():
    """左栏常驻分组（FIXED_PLATS）== 登录形态渠道 ∪ 纯官方 API 渠道（渠道注册表推导）。

    旧不变量只含登录形态 4 渠道，official-only（IG/Zalo）「刻意不常驻、免得占空分组」
    ——2026-08-07 运营决策显式推翻：后端完工的平台必须在面板可见（「怎么看不到新
    软件 logo」正是本条的由来），未接入以暗态+接入引导常驻，原「空分组」顾虑已由
    账号抽屉的富引导空态（品牌渐变+能力 chips+接入 CTA）消解。
    与 tests/test_inbox_platform_rail.py 互补：那边钉 FIXED_PLATS ==
    platform_readiness._IMPLEMENTED_MODES（就绪度注册表），这边钉渠道向导注册表
    ——两个事实源经由同一个前端清单传递一致，任一注册表漂移都会被点名。
    """
    src = _INBOX_TPL.read_text(encoding="utf-8")
    m = re.search(r"const FIXED_PLATS *= *\[([^\]]*)\]", src)
    assert m, "unified_inbox.html 里找不到 FIXED_PLATS 定义"
    fixed = set(re.findall(r"'([a-z]+)'", m.group(1)))
    login_platforms = {ch.login_platform for ch in CHANNELS if ch.login_platform}
    official_only = {ch.official_platform for ch in CHANNELS if ch.official_platform}
    assert fixed == (login_platforms | official_only), (
        f"FIXED_PLATS {fixed} != 登录形态 {login_platforms} ∪ 纯官方 {official_only}")


def test_frontend_connect_deeplink_plats_are_supported():
    """深链接入白名单 ⊆ 参照系（陌生名=deeplink 永远打不开还查不出为什么）。"""
    src = _INBOX_TPL.read_text(encoding="utf-8")
    m = re.search(r"_CONNECT_PLATS *= *new Set\(\[([^\]]*)\]", src)
    assert m, "unified_inbox.html 里找不到 _CONNECT_PLATS 定义"
    plats = set(re.findall(r"'([a-z]+)'", m.group(1)))
    assert plats <= SUPPORTED


def test_shared_media_url_field_matches_worker_read_path():
    """向导里的「公网媒体 URL」共享字段（IG/LINE 卡各摆一份）必须写官方 worker
    真正读的那个 config 路径——键名漂移=填了也不通。"""
    declared = {
        f.key for ch in CHANNELS for f in ch.fields
        if f.key.startswith("official_media.")
    }
    assert declared == {"official_media.public_base_url"}, declared
    worker_src = (_ROOT / "src" / "integrations" / "official_api_worker.py"
                  ).read_text(encoding="utf-8")
    assert '"official_media"' in worker_src
    assert '"public_base_url"' in worker_src
    # 两张卡（instagram / line）都得摆：只摆一张，走另一条路的运营看不见它
    holders = {ch.id for ch in CHANNELS
               if any(f.key == "official_media.public_base_url" for f in ch.fields)}
    assert holders == {"instagram", "line"}, holders
