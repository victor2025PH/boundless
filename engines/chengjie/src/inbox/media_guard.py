"""出站媒体内容守卫（P0 2026-08-17）：魔数嗅探 + 危险扩展名黑名单 + 下载名消毒。

背景：``send-media`` 此前只看扩展名（``save_outbound_media`` →
``media_type_from_ext``），服务端零内容校验——``.exe`` 改名 ``.jpg`` 可直接经
坐席台发给客户；按扩展名归类的 image/voice/video 若内容与类不符，平台 API
（pyrogram ``send_photo`` / Baileys ``image``）也会以更晦涩的方式在发送侧失败。
本模块把「媒体产物验证纪律」（magic bytes 才算内容验证）搬到出站口：
**上传时验，而不是发出后猜**。

纯函数、零 IO——route 接线之外可独立测试。

判定哲学：
- 危险扩展名（可执行/脚本/安装包）**无条件拒**——坐席台不是文件中转站，
  这类文件发给客户只有被滥用一种解释，宁可误拦；
- image/voice/video 三类（会走平台专用发送 API）魔数必须与扩展名同大类，
  否则拒（``magic_mismatch``）——防伪装，也防平台侧晦涩失败；
- document 是杂物袋（txt/csv 等**合法无魔数**格式存在），只拒**内容是
  可执行体**（MZ/ELF/Mach-O）的伪装件，其余放行——精度优先，
  不误伤正常办公文件。
"""

from __future__ import annotations

import os
import re
from typing import Optional

# 可执行/脚本/安装包扩展名：无条件拒绝（大小写不敏感）。
# 刻意含 .js/.sh：客服会话没有给客户发脚本的正当场景，误拦成本≈0。
DANGEROUS_EXTS = frozenset({
    ".exe", ".dll", ".sys", ".com", ".bat", ".cmd", ".scr", ".pif",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1",
    ".msi", ".msp", ".jar", ".apk", ".hta", ".lnk", ".reg", ".cpl",
    ".sh", ".gadget", ".application", ".appref-ms",
})

# 与 protocol_bridge 的 _OUT_* 三表同口径（此处独立复制并有门禁钉住同步，
# 不 import：protocol_bridge 依赖较重，守卫要保持零依赖可测）。
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_AUDIO_EXTS = frozenset({".ogg", ".opus", ".mp3", ".m4a", ".wav", ".amr", ".aac"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".m4v"})


def sniff_media_kind(data: bytes) -> str:
    """按 magic bytes 嗅探内容大类。

    返回：``image`` | ``audio`` | ``video`` | ``mp4``（ISO 容器，音视频两可）|
    ``pdf`` | ``zip`` | ``ole`` | ``executable`` | ``""``（无法识别）。
    只读前 16 字节量级，纯函数。
    """
    if not data:
        return ""
    head = bytes(data[:16])
    # ── 可执行体（安全侧最高优先） ──────────────────────────────
    if head[:2] == b"MZ":
        return "executable"
    if head[:4] == b"\x7fELF":
        return "executable"
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                    b"\xca\xfe\xba\xbe"):
        return "executable"  # Mach-O / universal binary
    # ── 图片 ────────────────────────────────────────────────────
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if head[:3] == b"\xff\xd8\xff":
        return "image"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head[:2] == b"BM":
        return "image"
    # ── 音频 ────────────────────────────────────────────────────
    if head[:4] == b"OggS":
        return "audio"  # ogg/opus
    if head[:3] == b"ID3":
        return "audio"  # mp3 带 ID3 tag
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio"  # mp3 裸帧
    if head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio"  # AAC ADTS
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio"
    if head[:5] == b"#!AMR":
        return "audio"
    # ── 视频/ISO 容器 ───────────────────────────────────────────
    if head[:4] == b"\x1aE\xdf\xa3":
        return "video"  # webm/mkv (EBML)
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand[:3] == b"M4A":
            return "audio"
        return "mp4"  # mp4/mov/m4v…：音视频两可（m4a 也可能挂 mp42 brand）
    # ── 文档 ────────────────────────────────────────────────────
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"  # zip/docx/xlsx/pptx…
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "ole"  # 旧版 doc/xls/ppt
    return ""


def _ext_category(ext: str) -> str:
    """扩展名 → 出站大类（与 protocol_bridge.media_type_from_ext 同口径）。"""
    e = str(ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    if e in _IMAGE_EXTS:
        return "image"
    if e in _AUDIO_EXTS:
        return "voice"
    if e in _VIDEO_EXTS:
        return "video"
    return "document"


# 大类 → 可接受的嗅探结果（"" 不在任何表里：三类媒体不认无魔数内容）
_ACCEPT: dict = {
    "image": {"image"},
    "voice": {"audio", "mp4"},   # m4a 可能挂 mp4-family brand
    "video": {"video", "mp4"},
}


def validate_outbound_media(filename: str, data: bytes) -> dict:
    """出站媒体上传验证。返回 ``{ok, reason, detected, category}``。

    reason（不 ok 时）：``ext_forbidden`` | ``magic_mismatch`` | ``executable_content``。
    空文件/超限由路由在先校验，此处不重复。
    """
    ext = os.path.splitext(str(filename or ""))[1].lower()
    detected = sniff_media_kind(data)
    category = _ext_category(ext)
    if ext in DANGEROUS_EXTS:
        return {"ok": False, "reason": "ext_forbidden",
                "detected": detected, "category": category}
    if detected == "executable":
        # 任何大类都不许发可执行体（含改名成 .jpg/.pdf 的伪装件）
        return {"ok": False, "reason": "executable_content",
                "detected": detected, "category": category}
    if category in _ACCEPT and detected not in _ACCEPT[category]:
        return {"ok": False, "reason": "magic_mismatch",
                "detected": detected, "category": category}
    return {"ok": True, "reason": "", "detected": detected,
            "category": category}


# ── 下载侧 ────────────────────────────────────────────────────────────

_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_download_name(name: str, fallback: str = "download.bin") -> str:
    """把前端建议的下载文件名消毒成安全的 attachment 名。

    只取 basename、剔除路径分隔/控制字符、去首尾点空格；空则回落 fallback。
    """
    base = os.path.basename(str(name or "").strip())
    base = _UNSAFE_NAME_RE.sub("_", base).strip(" .")
    if not base:
        return fallback
    # 防超长文件名（Windows 260 路径预算；255 是常见文件系统单名上限）
    if len(base) > 150:
        root, ext = os.path.splitext(base)
        base = root[:150 - len(ext)] + ext
    return base


def resolve_contained_path(root: str, candidate: str) -> Optional[str]:
    """路径穿越守卫：``candidate`` 规范化后必须落在 ``root`` 之内。

    返回规范化绝对路径；越界/异常返回 None。
    用 realpath（解 symlink/junction——本机 D:\\boundless 是目录联接，
    global_rules 首存曾因 resolve/abspath 口径不一静默失败，同坑不踩二遍）。
    """
    try:
        root_r = os.path.realpath(str(root or ""))
        cand_r = os.path.realpath(str(candidate or ""))
        if not root_r or not cand_r:
            return None
        if os.path.commonpath([root_r, cand_r]) != root_r:
            return None
        return cand_r
    except Exception:
        return None


def resolve_contained_path_any(roots, candidate: str) -> Optional[str]:
    """多白名单根版容纳守卫：落在**任一** root 内即放行（返回规范化路径）。

    协议媒体读取是双根的（``protocol_bridge.protocol_media_roots``：数据根主根
    + 旧引擎树根兜底）；单根守卫会把「解析器在旧根命中的文件」当穿越拒掉——
    症状是 404/400 而文件明明在，比「找不到」更难查。守卫语义本身不放宽：
    每个根仍走同一条 realpath commonpath 检查。
    """
    for r in (roots or ()):
        hit = resolve_contained_path(str(r), candidate)
        if hit:
            return hit
    return None
