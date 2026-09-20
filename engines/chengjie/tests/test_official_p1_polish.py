"""P1 收口门禁：官方渠道编排器桥接 + Messenger media_ref + 坐席 caps_reason 文案。

承接并发线已落地的向导/探针/清单一致性；本文件钉住本轮「二次优化」不变量，
避免再出现「后端有 reason、前端仍显示笼统不支持」这类半成品缝。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.integrations.shared.official_inbound import meta_attachment_url
from src.utils.channel_setup import CHANNELS

_ROOT = Path(__file__).resolve().parents[1]
_INBOX = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def test_official_channels_bridge_orchestrator_on_ready():
    """凭证齐的官方渠道必须声明打开编排器——否则 provision 出的 official 行永不上线。"""
    need = {"line", "messenger", "instagram", "zalo", "whatsapp"}
    got = {ch.id for ch in CHANNELS
           if "platform_login.orchestrator_enabled" in (ch.enable_on_ready or [])}
    assert need <= got, f"缺编排器桥接的官方渠道: {need - got}"


def test_meta_attachment_url_extracts_cdn():
    assert meta_attachment_url([
        {"type": "image", "payload": {"url": "https://cdn/a.jpg"}},
    ]) == "https://cdn/a.jpg"
    assert meta_attachment_url([
        {"type": "video", "payload": {"thumbnail": "https://cdn/t.jpg"}},
    ]) == "https://cdn/t.jpg"
    assert meta_attachment_url(None) == ""
    assert meta_attachment_url([]) == ""
    assert meta_attachment_url([{"payload": {}}]) == ""


def test_facebook_webhook_passes_media_ref():
    src = (_ROOT / "src" / "integrations" / "facebook_webhook.py").read_text(
        encoding="utf-8")
    assert "meta_attachment_url" in src
    assert "media_ref=" in src


def test_inbox_caps_reason_titles_wired():
    """前端消费 caps_reason → 对应 i18n 键双语都在；模板挂了 _capsDenyTitle。"""
    tpl = _INBOX.read_text(encoding="utf-8")
    assert "function _capsDenyTitle" in tpl
    assert "caps_reason" in tpl
    assert "inbox.caps.zalo_api_no_media" in tpl
    assert "inbox.caps.needs_public_url" in tpl
    i18n = _I18N.read_text(encoding="utf-8")
    for key in ("inbox.caps.zalo_api_no_media", "inbox.caps.needs_public_url"):
        assert i18n.count(f'"{key}"') >= 2, f"缺双语键: {key}"


def test_connect_deeplink_includes_official_only_platforms():
    """深链接 ?connect=instagram|zalo 必须能打开接入弹窗（再跳向导）。"""
    src = _INBOX.read_text(encoding="utf-8")
    m = re.search(r"_CONNECT_PLATS *= *new Set\(\[([^\]]*)\]", src)
    assert m, "_CONNECT_PLATS missing"
    plats = set(re.findall(r"'([a-z]+)'", m.group(1)))
    assert {"instagram", "zalo"} <= plats


def test_probe_official_channels_tool_importable():
    """验收探针可加载（不触网、不写库；tools/ 非包，走 file location）。"""
    import importlib.util
    p = _ROOT / "tools" / "probe_official_channels.py"
    spec = importlib.util.spec_from_file_location("_probe_official_channels", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    assert callable(mod.run)
    assert callable(mod._check_platform)
    # PS5.1 GBK 控制台：输出不得含勾叉符号
    src = p.read_text(encoding="utf-8")
    for bad in ("\u2713", "\u2717", "\u2714", "\u2718", "\u00b7"):
        assert bad not in src, "输出符号 %r 在 GBK 控制台会炸" % bad
