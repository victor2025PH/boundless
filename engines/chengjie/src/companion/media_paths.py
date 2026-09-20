# -*- coding: utf-8 -*-
"""人设相册落盘根（单一事实源）+ 存量迁移（#67-① 0830 实锤）。

事故：上传路由用 ``Path(__file__)`` 推 ``src/web/static/persona_albums``——引擎
部署下这是稳定代码根（无害），**打包桌面态却是安装目录**
``resources\\backend\\_internal\\src\\web\\static\\persona_albums``：per-user 安装目录
每次版本更新被整体替换 → 相册随更新清空（skuio 机 28DTZS 实锤：文件真实在、
位置违规）。与 ``licensing.data_paths`` 治的授权文件是同一个病（该模块 docstring
即本病的完整病理），相册这里是「被服务静态资产」桌面态的漏网。

规则（与 data_paths 同序）：``AITR_CONFIG_PATH``/``AITR_DATA_DIR`` 任一在 →
落 **数据根**（``<data>/persona_albums``，升级不丢、备份带得走）；都不在
（裸引擎/CI）→ 维持旧引擎树位置（零行为变化，测试经 conftest 的
``AITR_DATA_DIR``=tmp 自动隔离）。

serving 侧由 admin.py 复用 ``ProtocolMediaStatic`` 把 ``/static/persona_albums``
指到数据根 + 旧树兜底——URL 形态不变，存量引用零 404 窗口。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("ai_chat_assistant.media_paths")

#: 旧位置（引擎树/打包 _internal 静态树）——迁移源 + serving 兜底，勿删。
LEGACY_ALBUM_ROOT = (
    Path(__file__).resolve().parents[1] / "web" / "static" / "persona_albums")


def resolve_album_root() -> Path:
    """当前生效的相册根。绝不抛；判据与 ``licensing.data_paths`` 同一套 env。"""
    try:
        env_path = (os.environ.get("AITR_CONFIG_PATH") or "").strip()
        env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
        if env_path:
            return Path(env_path).expanduser().parent.parent / "persona_albums"
        if env_dir:
            return Path(env_dir).expanduser() / "persona_albums"
    except Exception:  # noqa: BLE001 - 环境变量畸形时退回旧树
        pass
    return LEGACY_ALBUM_ROOT


def migrate_legacy_album_tree(store=None) -> dict:
    """存量迁移（幂等，best-effort）：旧树文件复制到数据根 + DB 路径改写。

    - 只在「数据根 ≠ 旧树」时动作；
    - 复制不搬移（旧树保留＝serving 兜底 + 引擎部署回滚余地）；目标已存在跳过；
    - ``store.rewrite_file_path_prefix`` 把 DB ``file_path`` 前缀改指数据根——
      发送链读 DB 绝对路径，不改写＝复制了也白复制（桌面更新后旧路径蒸发，
      pyrogram 拿到不存在的路径报 ``Failed to decode``，见 #67-③）；
    - 任何单文件失败只记日志不中断（与 protocol_media 启动迁移同哲学）。
    """
    out = {"migrated": 0, "skipped": 0, "db_rows": 0, "active": False}
    try:
        new_root = resolve_album_root()
        legacy = LEGACY_ALBUM_ROOT
        if str(new_root.resolve()) == str(legacy.resolve()):
            return out
        out["active"] = True
        if legacy.is_dir():
            for src in legacy.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(legacy)
                dst = new_root / rel
                try:
                    if dst.exists() and dst.stat().st_size == src.stat().st_size:
                        out["skipped"] += 1
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    out["migrated"] += 1
                except Exception:
                    logger.debug("[media_paths] 迁移单文件失败（跳过）：%s",
                                 src, exc_info=True)
        if store is not None and hasattr(store, "rewrite_file_path_prefix"):
            try:
                out["db_rows"] = int(store.rewrite_file_path_prefix(
                    str(legacy), str(new_root)))
            except Exception:
                logger.debug("[media_paths] DB 路径改写失败（跳过）",
                             exc_info=True)
        if out["migrated"] or out["db_rows"]:
            logger.info(
                "[media_paths] 相册存量迁移：复制 %d（跳过 %d）→ %s；DB 改写 %d 行",
                out["migrated"], out["skipped"], new_root, out["db_rows"])
    except Exception:
        logger.debug("[media_paths] 相册迁移整体跳过", exc_info=True)
    return out


# ── 媒体 magic bytes（#67-② + 媒体产物验证纪律）─────────────────────────────
#
# 实锤：上传管线把损坏文件原样落盘（PIL 开不了→软失败直写），AI 发图时
# pyrogram/接收端解码失败——「上传成功」从头到尾没验过内容。纪律条文：
# 落盘后必验 magic bytes 才许报 OK；尺寸/HTTP 状态码不构成内容验证。

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", 0),          # JPEG
    (b"\x89PNG\r\n\x1a\n", 0),     # PNG
    (b"GIF87a", 0), (b"GIF89a", 0),
)


def sniff_media_bytes(data: bytes, ext: str) -> str:
    """按扩展名族校验 magic bytes。返回 ""=通过，否则一句人话原因。

    图族（jpg/png/gif/webp）与视频族（mp4/mov/m4v/webm）分开判；族内互认
    （.jpg 里装的是合法 PNG 也放行——扩展名错拍不等于内容坏，接收端按内容
    解码）。纯函数绝不抛。
    """
    try:
        b = bytes(data[:16] or b"")
        e = (ext or "").lower().lstrip(".")
        if len(b) < 8:
            return "file too small"
        if e in ("jpg", "jpeg", "png", "webp", "gif"):
            for magic, off in _IMAGE_MAGIC:
                if b[off:off + len(magic)] == magic:
                    return ""
            if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
                return ""
            return "not a decodable image (bad magic bytes)"
        if e in ("mp4", "mov", "m4v"):
            if b[4:8] == b"ftyp":
                return ""
            return "not a decodable video (missing ftyp box)"
        if e == "webm":
            if b[:4] == b"\x1a\x45\xdf\xa3":
                return ""
            return "not a decodable video (bad EBML magic)"
        return ""  # 未知扩展名交上游白名单拦，这里不重复判
    except Exception:
        return ""  # 校验器自身异常绝不拦上传


__all__ = [
    "LEGACY_ALBUM_ROOT", "resolve_album_root", "migrate_legacy_album_tree",
    "sniff_media_bytes",
]
