"""P2.4：LINE 图片消息接入共享识别层（line_rpa.media_enrich）。

三平台对齐背景：Telegram A 线入站图片经 VisionClient 出 `[图片内容] desc` 参与
回复；WhatsApp 在拟稿链经 `src/inbox/media_enrich` 补描述；LINE 的 XML 读取层
只认文本节点——对方发图此前完全不可见。本测试锁定：

1. 纯函数地基 `media_read.find_latest_peer_image` / `crop_png_region` 的几何语义
   （检出最新对方图片气泡；头像/己方图/整屏背景/旧图不误报）；
2. runner 注入语义：图为最新一条 → `[图片内容] {desc}` 作为消息文本进
   process_message（Telegram 口径）；「文字+图」连发拼接；inbox/contacts 镜像
   记 `[图片] {desc}` 短占位；
3. 开关默认关（新子系统铁律）；
4. 软失败回落：截屏失败 / vision 无描述 → 跳过本会话（＝旧行为不回复）绝不阻塞；
5. 同图去重：裁剪位图 sha 相同 → 不重复调 vision、不重复回复；发送成功后 sha
   随 per-chat 状态持久化。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from src.integrations.line_rpa import media_read
from src.integrations.line_rpa.runner import LineRpaRunner

# ── XML fixture 工具 ──────────────────────────────────────────────────


def _n(cls: str, bounds: str, text: str = "", rid: str = "") -> str:
    return (
        f'<node class="{cls}" text="{text}" resource-id="{rid}" '
        f'content-desc="" bounds="{bounds}" />'
    )


def _xml(*nodes: str) -> bytes:
    inner = "".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hierarchy rotation="0">'
        f'<node class="android.widget.FrameLayout" text="" resource-id="" '
        f'content-desc="" bounds="[0,0][1080,1920]">{inner}</node>'
        "</hierarchy>"
    ).encode("utf-8")


# 常用节点（1080x1920 屏）
_PEER_TEXT = _n("android.widget.TextView", "[80,900][500,1000]", text="在吗")
_AVATAR = _n("android.widget.ImageView", "[24,1100][120,1196]",
             rid="jp.naver.line.android:id/avatar")
_PEER_IMAGE = _n("android.widget.ImageView", "[96,1100][700,1650]",
                 rid="jp.naver.line.android:id/message_image")
_IMG_BOUNDS = (96, 1100, 700, 1650)


def _screen_png() -> bytes:
    """确定性整屏 PNG（1080x1920，图片气泡区域画一块红），供裁剪。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (1080, 1920), (240, 240, 240))
    ImageDraw.Draw(im).rectangle(_IMG_BOUNDS, fill=(200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _expected_sha() -> str:
    crop = media_read.crop_png_region(_screen_png(), _IMG_BOUNDS, margin=8)
    assert crop
    return media_read.image_sha256(crop)


# ── 纯函数：find_latest_peer_image ───────────────────────────────────


def test_detects_image_when_latest() -> None:
    cand, dbg = media_read.find_latest_peer_image(
        _xml(_PEER_TEXT, _AVATAR, _PEER_IMAGE)
    )
    assert cand is not None, dbg
    assert cand.bounds == _IMG_BOUNDS
    assert "image_latest" in dbg


def test_no_trigger_when_text_is_newer() -> None:
    """图片上方旧图、下方有更新的文字 → 文字才是最新消息，不触发。"""
    newer_text = _n("android.widget.TextView", "[80,1700][400,1780]", text="这张好看吗")
    cand, dbg = media_read.find_latest_peer_image(
        _xml(_PEER_IMAGE, newer_text)
    )
    assert cand is None
    assert "text_below_image" in dbg


def test_no_trigger_when_self_text_is_newer() -> None:
    """己方回复在图之下（这图早就处理过）→ 不触发。"""
    self_text = _n("android.widget.TextView", "[600,1700][1000,1780]", text="收到啦")
    cand, dbg = media_read.find_latest_peer_image(
        _xml(_PEER_IMAGE, self_text)
    )
    assert cand is None
    assert "text_below_image" in dbg


def test_avatar_not_detected() -> None:
    cand, dbg = media_read.find_latest_peer_image(_xml(_PEER_TEXT, _AVATAR))
    assert cand is None
    assert dbg == "no_image_candidates"


def test_self_side_image_not_detected() -> None:
    self_img = _n("android.widget.ImageView", "[500,1100][1050,1650]",
                  rid="jp.naver.line.android:id/message_image")
    cand, _ = media_read.find_latest_peer_image(_xml(self_img))
    assert cand is None


def test_fullscreen_background_not_detected() -> None:
    bg = _n("android.widget.ImageView", "[0,0][1080,1920]", rid="")
    cand, _ = media_read.find_latest_peer_image(_xml(bg))
    assert cand is None


def test_excluded_rid_not_detected() -> None:
    big_avatar = _n("android.widget.ImageView", "[96,1100][700,1650]",
                    rid="jp.naver.line.android:id/profile_image")
    cand, _ = media_read.find_latest_peer_image(_xml(big_avatar))
    assert cand is None


def test_timestamp_text_does_not_block_image() -> None:
    """图片下方的时间戳（纯时间文本）不算「更新的消息」。"""
    ts = _n("android.widget.TextView", "[80,1660][200,1700]", text="12:30")
    cand, dbg = media_read.find_latest_peer_image(_xml(_PEER_IMAGE, ts))
    assert cand is not None, dbg


def test_crop_png_region_deterministic_and_clamped() -> None:
    png = _screen_png()
    c1 = media_read.crop_png_region(png, _IMG_BOUNDS, margin=8)
    c2 = media_read.crop_png_region(png, _IMG_BOUNDS, margin=8)
    assert c1 and c2 and media_read.image_sha256(c1) == media_read.image_sha256(c2)
    from PIL import Image

    im = Image.open(io.BytesIO(c1))
    # 8px margin 双侧外扩
    assert im.size == (700 - 96 + 16, 1650 - 1100 + 16)
    # 越界 bounds 自动收敛，不炸
    edge = media_read.crop_png_region(png, (1000, 1800, 1300, 2100), margin=8)
    assert edge is not None
    # 过小区域 → None
    assert media_read.crop_png_region(png, (10, 10, 20, 20), margin=0) is None


# ── runner 集成 ──────────────────────────────────────────────────────


class _SkillStub:
    def __init__(self) -> None:
        self.calls: list = []

    async def process_message(self, text: str, user_id: str = "", context: Any = None):
        self.calls.append({"text": text, "user_id": user_id, "context": context})
        return "好可爱！"


class _FakeStore:
    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self.state = dict(state or {})
        self.updates: list = []

    def get_chat_state(self, chat_key: str) -> Dict[str, Any]:
        return dict(self.state)

    def update_chat_state(self, chat_key: str, **kw: Any) -> None:
        self.updates.append((chat_key, kw))

    def insert_pending(self, **kw: Any) -> int:
        return 1


class _HooksStub:
    def __init__(self) -> None:
        self.inbound: list = []

    def on_line_first_text(self, **kw: Any) -> None:
        self.inbound.append(kw)

    def on_message(self, **kw: Any) -> None:
        pass


def _runner(
    cfg: Dict[str, Any],
    *,
    store: Any = None,
    skill: Any = None,
) -> LineRpaRunner:
    cm = SimpleNamespace(
        config_path=str(Path("config/config.yaml")),
        config={"vision": {"enabled": True}},
    )
    r = LineRpaRunner(
        config_manager=cm,
        skill_manager=skill or _SkillStub(),
        line_rpa_cfg=cfg,
        state_store=store,
    )
    r._serial = "FAKESERIAL"
    return r


def _install_screenshot(r: LineRpaRunner, png: Optional[bytes], calls: list) -> None:
    async def fake_shot() -> Optional[bytes]:
        calls.append(1)
        return png

    r._capture_screen_for_media = fake_shot  # type: ignore[method-assign]


def _install_enrich(monkeypatch, desc: str, calls: list) -> None:
    async def fake_enrich(*, media_type: str, media_ref: str, caption: str = "",
                          config: Any = None, voice_transcriber: Any = None):
        calls.append({
            "media_type": media_type,
            "media_ref": media_ref,
            "ref_exists": Path(media_ref).is_file(),
            "is_png": Path(media_ref).read_bytes()[:4] == b"\x89PNG"
            if Path(media_ref).is_file() else False,
        })
        if not desc:
            return "[图片]", ""
        return f"[图片内容] {desc}", desc

    monkeypatch.setattr(
        "src.inbox.media_enrich.enrich_inbound_media_text", fake_enrich
    )


async def test_switch_default_off_no_capture() -> None:
    """开关缺省=关：不截屏、不识别，图片消息保持旧行为（no_peer_text）。"""
    skill = _SkillStub()
    r = _runner({}, store=_FakeStore(), skill=skill)
    shots: list = []
    _install_screenshot(r, _screen_png(), shots)
    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_AVATAR, _PEER_IMAGE), fallback_chat_key="c1",
    )
    assert out["step"] == "no_peer_text"
    assert shots == [] and skill.calls == []


async def test_service_defaults_media_enrich_off() -> None:
    from src.integrations.line_rpa.service import LineRpaService

    d = LineRpaService._defaults(None)  # type: ignore[arg-type]
    assert d["media_enrich"]["enabled"] is False


async def test_enriched_image_injected_as_message(monkeypatch) -> None:
    """纯图消息：[图片内容] desc 作为消息文本进 process_message（Telegram 口径）；
    contacts/inbox 镜像记 [图片] desc 短占位；临时文件用后即删。"""
    skill = _SkillStub()
    store = _FakeStore()
    r = _runner({"media_enrich": {"enabled": True}}, store=store, skill=skill)
    hooks = _HooksStub()
    r.set_contact_hooks(hooks)
    shots: list = []
    _install_screenshot(r, _screen_png(), shots)
    enrich_calls: list = []
    _install_enrich(monkeypatch, "一只橘猫趴在沙发上", enrich_calls)

    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_AVATAR, _PEER_IMAGE), fallback_chat_key="c1",
    )
    assert out["step"] == "dry_run_done", out
    assert len(skill.calls) == 1
    assert skill.calls[0]["text"] == "[图片内容] 一只橘猫趴在沙发上"
    assert "image_enrich" in str(out.get("peer_debug"))
    # 共享层收到 image 类型 + 本地 PNG 临时文件；用后即删
    assert enrich_calls and enrich_calls[0]["media_type"] == "image"
    assert enrich_calls[0]["ref_exists"] and enrich_calls[0]["is_png"]
    assert not Path(enrich_calls[0]["media_ref"]).exists()
    # inbox/contacts 镜像短占位
    assert hooks.inbound and hooks.inbound[0]["text"] == "[图片] 一只橘猫趴在沙发上"


async def test_text_plus_image_combo_prepends_unreplied_text(monkeypatch) -> None:
    """「文字+图」连发：未回过的文字气泡拼在图描述前。"""
    skill = _SkillStub()
    r = _runner({"media_enrich": {"enabled": True}}, store=_FakeStore(), skill=skill)
    _install_screenshot(r, _screen_png(), [])
    _install_enrich(monkeypatch, "一只橘猫趴在沙发上", [])

    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_PEER_TEXT, _AVATAR, _PEER_IMAGE),
        fallback_chat_key="c1",
    )
    assert out["step"] == "dry_run_done", out
    assert skill.calls[0]["text"] == "在吗\n[图片内容] 一只橘猫趴在沙发上"


async def test_replied_text_not_prepended(monkeypatch) -> None:
    """屏上文字气泡已回过（== last_peer_text）→ 不重复拼进图片轮。"""
    skill = _SkillStub()
    store = _FakeStore({"last_peer_text": "在吗"})
    r = _runner({"media_enrich": {"enabled": True}}, store=store, skill=skill)
    _install_screenshot(r, _screen_png(), [])
    _install_enrich(monkeypatch, "一只橘猫趴在沙发上", [])

    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_PEER_TEXT, _AVATAR, _PEER_IMAGE),
        fallback_chat_key="c1",
    )
    assert out["step"] == "dry_run_done", out
    assert skill.calls[0]["text"] == "[图片内容] 一只橘猫趴在沙发上"


async def test_soft_fail_no_desc_falls_back(monkeypatch) -> None:
    """vision 关/挂/识别为空 → 跳过本会话（旧行为：不回复），不阻塞。"""
    skill = _SkillStub()
    r = _runner({"media_enrich": {"enabled": True}}, store=_FakeStore(), skill=skill)
    _install_screenshot(r, _screen_png(), [])
    _install_enrich(monkeypatch, "", [])  # 共享层软失败：desc 空

    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_AVATAR, _PEER_IMAGE), fallback_chat_key="c1",
    )
    assert out["step"] == "image_enrich_skipped"
    assert out["ok"] is True and skill.calls == []


async def test_soft_fail_screenshot_falls_back(monkeypatch) -> None:
    skill = _SkillStub()
    r = _runner({"media_enrich": {"enabled": True}}, store=_FakeStore(), skill=skill)
    _install_screenshot(r, None, [])  # 截屏失败
    enrich_calls: list = []
    _install_enrich(monkeypatch, "不该被调用", enrich_calls)

    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_AVATAR, _PEER_IMAGE), fallback_chat_key="c1",
    )
    assert out["step"] == "image_enrich_skipped"
    assert out["ok"] is True and skill.calls == [] and enrich_calls == []


async def test_same_image_sha_dedup_skips_vision(monkeypatch) -> None:
    """同一张图（裁剪位图 sha 相同）→ 不再调 vision、不重复回复。"""
    skill = _SkillStub()
    store = _FakeStore({"last_screen_sha256": _expected_sha()})
    r = _runner({"media_enrich": {"enabled": True}}, store=store, skill=skill)
    _install_screenshot(r, _screen_png(), [])
    enrich_calls: list = []
    _install_enrich(monkeypatch, "不该被调用", enrich_calls)

    out = await r._process_chat_room(
        dry_run=True, force_reply=False,
        xml_in_room=_xml(_AVATAR, _PEER_IMAGE), fallback_chat_key="c1",
    )
    assert out["step"] == "image_duplicate_skipped"
    assert out["ok"] is True and enrich_calls == [] and skill.calls == []


async def test_sent_persists_image_sha(monkeypatch) -> None:
    """发送成功 → last_screen_sha256 随 per-chat 状态落库（下一轮同图去重）。"""
    skill = _SkillStub()
    store = _FakeStore()
    r = _runner({"media_enrich": {"enabled": True}}, store=store, skill=skill)
    _install_screenshot(r, _screen_png(), [])
    _install_enrich(monkeypatch, "一只橘猫趴在沙发上", [])

    async def fake_send(xml: Any, text: str) -> Dict[str, Any]:
        return {"ok": True, "parts": []}

    r._pace_and_send = fake_send  # type: ignore[method-assign]

    out = await r._process_chat_room(
        dry_run=False, force_reply=False,
        xml_in_room=_xml(_AVATAR, _PEER_IMAGE), fallback_chat_key="c1",
    )
    assert out["step"] == "sent", out
    sha_updates = [kw for _, kw in store.updates if kw.get("last_screen_sha256")]
    assert sha_updates and sha_updates[-1]["last_screen_sha256"] == _expected_sha()
    assert sha_updates[-1]["last_peer_text"] == "[图片内容] 一只橘猫趴在沙发上"


async def test_legacy_path_injects_and_persists(monkeypatch, tmp_path) -> None:
    """老单会话路径（无 navigation）：同口径注入 + dry-run 持久化裁剪指纹；
    顺带锁定 reply_lang 落入 result（修 `out` 未定义的 NameError 崩溃）。"""
    skill = _SkillStub()
    state_file = tmp_path / "line_state.json"
    r = _runner(
        {
            "media_enrich": {"enabled": True},
            "state_file": str(state_file),
        },
        store=None,
        skill=skill,
    )
    r._resolve_serial = lambda: "FAKESERIAL"  # type: ignore[method-assign]
    r._dump_ui_xml = lambda: (_xml(_PEER_TEXT, _AVATAR, _PEER_IMAGE), "ok:test")  # type: ignore[method-assign]
    _install_screenshot(r, _screen_png(), [])
    _install_enrich(monkeypatch, "一只橘猫趴在沙发上", [])

    out = await r.run_once(dry_run=True)
    assert out["step"] == "dry_run_done", out
    assert skill.calls[0]["text"] == "在吗\n[图片内容] 一只橘猫趴在沙发上"
    assert out.get("reply_lang")  # 老路径 NameError 修复的回归钉
    st = json.loads(state_file.read_text(encoding="utf-8"))
    assert st.get("last_screen_crop_sha256") == _expected_sha()
