"""#195（L-6 C）：Telegram 贴纸按真实容器落盘（.tgs / .webm / .webp）+ 缩略图 + 前端三态。

钧 B990 实锤：动图贴纸（is_animated，Lottie gzip）与视频贴纸（is_video，webm）此前一律按
.webp 落盘 → 工作台 <img> 渲染不了＝破图，识图拿到非位图字节也白失败一轮。钉住：
  - tg_media_meta / sticker_ext 三态扩展名；其它媒体类型不变；
  - download_tg_media 对 .tgs/.webm 额外下载 sticker.thumbs[0] 落 <stem>.thumb.webp（真 WebP）；
  - sticker_thumb_path：识图取「可看的那张图」（静态贴纸自身 / 动图缩略图 / 无缩略图 → None）；
  - media_enrich 对贴纸走缩略图识图、无缩略图跳过；
  - unified_inbox.html 贴纸分支三态 + i18n 键 inbox.media.sticker_animated（zh/en）。
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.integrations import protocol_bridge as pb

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _sticker(**kw):
    base = {"is_animated": False, "is_video": False, "thumbs": [], "emoji": "😂", "file_size": 1234}
    base.update(kw)
    return SimpleNamespace(**base)


def _msg(**media):
    fields = {"photo": None, "voice": None, "audio": None, "video": None, "video_note": None,
              "animation": None, "sticker": None, "document": None, "id": 4242}
    fields.update(media)
    return SimpleNamespace(**fields)


# ── 扩展名三态 ────────────────────────────────────────────────────────

def test_sticker_ext_three_states():
    assert pb.sticker_ext(_sticker(is_animated=True)) == ".tgs"
    assert pb.sticker_ext(_sticker(is_video=True)) == ".webm"
    assert pb.sticker_ext(_sticker()) == ".webp"


def test_tg_media_meta_sticker_variants_and_others_unchanged():
    assert pb.tg_media_meta(_msg(sticker=_sticker(is_animated=True))) == ("sticker", ".tgs")
    assert pb.tg_media_meta(_msg(sticker=_sticker(is_video=True))) == ("sticker", ".webm")
    assert pb.tg_media_meta(_msg(sticker=_sticker())) == ("sticker", ".webp")
    assert pb.tg_media_meta(_msg(photo=object())) == ("image", ".jpg")
    assert pb.tg_media_meta(_msg(voice=object())) == ("voice", ".ogg")
    assert pb.tg_media_meta(_msg(animation=object())) == ("video", ".mp4")
    assert pb.tg_media_meta(_msg()) is None


# ── 缩略图路径约定 ────────────────────────────────────────────────────

def test_sticker_thumb_path_convention(tmp_path):
    (tmp_path / "a.tgs").write_bytes(b"\x1f\x8b")
    (tmp_path / "a.thumb.webp").write_bytes(b"RIFF....WEBP")
    (tmp_path / "b.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    (tmp_path / "c.webp").write_bytes(b"RIFF....WEBP")
    assert pb.sticker_thumb_path(str(tmp_path / "a.tgs")) == str(tmp_path / "a.thumb.webp")
    assert pb.sticker_thumb_path(str(tmp_path / "b.webm")) is None
    assert pb.sticker_thumb_path(str(tmp_path / "c.webp")) == str(tmp_path / "c.webp")
    assert pb.sticker_thumb_path("") is None


# ── download_tg_media：主文件 + 缩略图 ───────────────────────────────

def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (24, 24), (255, 0, 0, 128)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeClient:
    def __init__(self, thumb_bytes: bytes):
        self.calls = []
        self._thumb = thumb_bytes

    async def download_media(self, file_id, in_memory=False, **kw):
        self.calls.append((file_id, in_memory))
        return io.BytesIO(self._thumb)


class _FakeMessage:
    def __init__(self, sticker, mid=77):
        self.photo = self.voice = self.audio = self.video = self.video_note = None
        self.animation = self.document = None
        self.sticker = sticker
        self.id = mid
        self._client = _FakeClient(_png_bytes())
        self.downloaded_to = None

    async def download(self, file_name):
        Path(file_name).write_bytes(b"\x1f\x8b fake-lottie-gzip")
        self.downloaded_to = file_name
        return file_name


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "protocol_media_root", lambda: tmp_path)
    return tmp_path


def test_download_animated_sticker_writes_tgs_and_thumb(media_root):
    stk = _sticker(is_animated=True, thumbs=[SimpleNamespace(file_id="THUMB1")])
    msg = _FakeMessage(stk)
    kind, url = asyncio.run(pb.download_tg_media(msg, "acct9"))
    assert kind == "sticker"
    assert url.endswith("/telegram/acct9_77.tgs")
    main = media_root / "telegram" / "acct9_77.tgs"
    thumb = media_root / "telegram" / "acct9_77.thumb.webp"
    assert main.exists() and thumb.exists()
    assert msg._client.calls == [("THUMB1", True)]
    from PIL import Image
    assert Image.open(thumb).format == "WEBP"          # 统一转成真 WebP，不是改名的 PNG
    # 前端按 media_ref 推导的缩略图 URL 与落盘文件同名
    assert url.replace(".tgs", ".thumb.webp").endswith("/telegram/acct9_77.thumb.webp")
    assert pb.sticker_thumb_path(str(main)) == str(thumb)


def test_download_video_sticker_writes_webm_and_thumb(media_root):
    stk = _sticker(is_video=True, thumbs=[SimpleNamespace(file_id="THUMB2")])
    msg = _FakeMessage(stk, mid=78)
    kind, url = asyncio.run(pb.download_tg_media(msg, "acct9"))
    assert (kind, url.endswith("acct9_78.webm")) == ("sticker", True)
    assert (media_root / "telegram" / "acct9_78.thumb.webp").exists()


def test_download_static_sticker_no_thumb_call(media_root):
    stk = _sticker(thumbs=[SimpleNamespace(file_id="THUMB3")])
    msg = _FakeMessage(stk, mid=79)
    kind, url = asyncio.run(pb.download_tg_media(msg, "acct9"))
    assert (kind, url.endswith("acct9_79.webp")) == ("sticker", True)
    assert msg._client.calls == []                       # 静态贴纸自身就是位图，不多下一份
    assert not (media_root / "telegram" / "acct9_79.thumb.webp").exists()


def test_download_animated_sticker_without_thumbs_still_ok(media_root):
    msg = _FakeMessage(_sticker(is_animated=True, thumbs=[]), mid=80)
    kind, url = asyncio.run(pb.download_tg_media(msg, "acct9"))
    assert kind == "sticker" and url.endswith(".tgs")
    assert pb.sticker_thumb_path(str(media_root / "telegram" / "acct9_80.tgs")) is None


# ── 识图：贴纸用缩略图，无缩略图跳过 ─────────────────────────────────

def test_media_enrich_sticker_uses_thumb_or_skips(tmp_path, monkeypatch):
    from src.inbox import media_enrich as me

    tgs = tmp_path / "s1.tgs"
    tgs.write_bytes(b"\x1f\x8b")
    thumb = tmp_path / "s1.thumb.webp"
    thumb.write_bytes(b"RIFF....WEBP")
    seen = []

    async def fake_describe(path, cfg):
        seen.append(path)
        return "一只挥手的猫"

    monkeypatch.setattr(me, "_describe_image", fake_describe)
    monkeypatch.setattr(me, "_resolve_local_path", lambda ref: str(tmp_path / ref))
    cfg = {"vision": {"enabled": True}}
    text, desc = asyncio.run(me.enrich_inbound_media_text(
        media_type="sticker", media_ref="s1.tgs", caption="", config=cfg, voice_transcriber=None))
    assert seen == [str(thumb)]
    assert desc == "一只挥手的猫" and "[贴纸内容]" in text

    seen.clear()
    (tmp_path / "s2.webm").write_bytes(b"\x1a\x45")
    text2, desc2 = asyncio.run(me.enrich_inbound_media_text(
        media_type="sticker", media_ref="s2.webm", caption="", config=cfg, voice_transcriber=None))
    assert seen == [] and desc2 == "" and text2 == "[贴纸]"


# ── 前端三态 + i18n ─────────────────────────────────────────────────

def test_frontend_sticker_branch_three_states():
    html = (ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    i = html.index("if(mt==='sticker'){")
    branch = html[i:i + 4000]
    assert "/\\.webm$/" in branch and "<video class=\"msg-media-sticker\"" in branch
    assert "autoplay loop muted playsinline" in branch
    assert "/\\.tgs$/" in branch and ".thumb.webp" in branch
    assert "inbox.media.sticker_animated" in branch
    assert 'data-stk-collect="' in branch               # 静态贴纸收藏入包保留
    # 收藏按钮只在静态分支（代码里出现在 tgs/webm 分支之后；注释里的提及不算）
    assert branch.index('data-stk-collect="') > branch.index("/\\.tgs$/")


def test_i18n_sticker_animated_key_zh_en():
    from src.web.i18n_packs import inbox_workspace as p
    assert p.ZH["inbox.media.sticker_animated"] == "动图贴纸"
    assert p.EN["inbox.media.sticker_animated"] == "Animated sticker"
