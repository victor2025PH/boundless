# -*- coding: utf-8 -*-
"""watermark.py — 授权可见水印的【单一策略 + 渲染】来源（3-E：跨导出路径一致性）。

背景：3-B 已在 vcam_server 广播环打实时水印，覆盖 直播 / OBS / WebRTC / 本地录制 / 快照
（录制与快照都取「已加水印」的 _latest_rgb / _fanout 帧）。但**离线视频导出**
（video_queue → LivePortrait 直接生成 MP4）走的是**另一条不经 vcam 的管线**，天然无水印——
试用/标准档可借此导出无水印成片，破坏 watermark_free 兑现的一致性。

本模块把「是否打 + 打什么字」的**策略集中为唯一真相**：vcam 复用它（判定一致），
导出管线用 apply_to_mp4() 补齐水印（ffmpeg overlay 预渲染 PNG，音轨 -c:a copy 无损）。

策略与 3-B 完全一致：仅当【强制模式 且 授权不含 watermark_free】才打；pro / 未强制 → 不打；
授权不可评估 → 软降级不打（与 license 整体"绝不崩"一致）。文字优先 AVATARHUB_WATERMARK_TEXT，
否则档位标签（试用版/标准版），再否则 DEMO。
"""
from __future__ import annotations

import os
import logging

logger = logging.getLogger("watermark")

_FONT_CACHE: dict = {}


def resolve(force_reload: bool = True) -> tuple[bool, str]:
    """返回 (是否需水印, 水印文字)。所有直播/导出路径共用的唯一判定来源。"""
    try:
        import license as _lic
        if force_reload:
            _lic.load_state(force=True)          # 运行时激活/续费/换档即时生效
        if _lic.enforcing() and not _lic.allowed("watermark_free"):
            text = (os.environ.get("AVATARHUB_WATERMARK_TEXT", "") or "").strip()
            if not text:                         # 默认白牌中性：档位标签（pro 本就不打）
                try:
                    st = _lic.load_state()
                    text = st.EDITION_LABELS.get(st.edition, "") or "DEMO"
                except Exception:
                    text = "DEMO"
            return True, text
    except Exception:
        pass                                     # 授权不可评估 → 不打（软降级）
    return False, ""


def _font(px: int):
    f = _FONT_CACHE.get(px)
    if f is None:
        try:
            from PIL import ImageFont
            for fp in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
                try:
                    f = ImageFont.truetype(fp, px)
                    break
                except Exception:
                    f = None
            if f is None:
                from PIL import ImageFont as _IF
                f = _IF.load_default()
        except Exception:
            f = None
        _FONT_CACHE[px] = f
    return f


def render_rgba(text: str, height: int):
    """右下角半透明水印（暗描边浅底可读 + 半透白字）。返回 (rgb ndarray, alpha float ndarray)。
    与 vcam_server 同款视觉（px=max(16,H//40)），保证直播/导出观感一致。"""
    import numpy as np
    from PIL import Image, ImageDraw
    px = max(16, int(height) // 40)
    font = _font(px)
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = int(d0.textlength(text, font=font)) if font else len(text) * px
    pad = int(px * 0.4)
    pw, ph = tw + 2 * pad, px + 2 * pad
    img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):     # 暗描边
        d.text((pad + dx, pad - 2 + dy), text, font=font, fill=(0, 0, 0, 110))
    d.text((pad, pad - 2), text, font=font, fill=(255, 255, 255, 150))
    arr = np.array(img)
    return arr[:, :, :3], arr[:, :, 3].astype(np.float32) / 255.0


def _ffmpeg_exe():
    """优先 imageio_ffmpeg 自带 ffmpeg（本项目已装，不依赖 PATH）；否则回退 PATH。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        return shutil.which("ffmpeg")


def _probe_size(path) -> tuple[int, int]:
    """(W, H) via cv2；失败 → (0, 0)。"""
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return w, h
    except Exception:
        return 0, 0


def apply_to_mp4(path, *, on: bool = None, text: str = "", force_reload: bool = True) -> dict:
    """按授权策略给已生成的 MP4 补右下角水印（音轨 -c:a copy 无损，就地替换）。
    未强制/pro/无文字/无输入 → 不动原文件；ffmpeg 缺失/失败 → 软降级不打并告警。
    on/text 省略时自动 resolve()（也可由调用方预评估后传入，避免重复强制读授权）。
    返回 {applied: bool, reason?: str, text?: str}。"""
    import numpy as np
    from pathlib import Path
    p = Path(path)
    if on is None:
        on, text = resolve(force_reload=force_reload)
    if not on or not text:
        return {"applied": False, "reason": "not_required"}
    if not p.exists() or p.stat().st_size == 0:
        return {"applied": False, "reason": "no_input"}
    ff = _ffmpeg_exe()
    if not ff:
        logger.warning("水印跳过：未找到 ffmpeg（imageio_ffmpeg / PATH 均无）。")
        return {"applied": False, "reason": "no_ffmpeg"}

    w, h = _probe_size(p)
    if h <= 0:
        h = 720
    import tempfile
    png = None
    try:
        rgb, alpha = render_rgba(text, h)
        from PIL import Image
        a = (np.clip(alpha, 0.0, 1.0) * 255).astype(np.uint8)
        rgba = np.dstack([rgb.astype(np.uint8), a])
        png = Path(tempfile.mkdtemp()) / "wm.png"
        Image.fromarray(rgba, "RGBA").save(str(png))
    except Exception:
        logger.exception("水印 PNG 渲染失败")
        return {"applied": False, "reason": "render_failed"}

    out = p.with_suffix(".wm.mp4")
    # 右下角，边距与直播一致（2% x，3% y）；overlay 尊重 PNG alpha（半透明）
    x = "main_w-overlay_w-trunc(main_w*0.02)"
    y = "main_h-overlay_h-trunc(main_h*0.03)"
    import subprocess
    cmd = [ff, "-y", "-i", str(p), "-i", str(png),
           "-filter_complex", f"overlay={x}:{y}:format=auto",
           "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except Exception as e:
        logger.warning(f"水印 ffmpeg 执行异常：{e}")
        return {"applied": False, "reason": "ffmpeg_error"}
    finally:
        try:
            if png is not None:
                png.unlink()
                png.parent.rmdir()
        except Exception:
            pass

    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        try:
            out.unlink()
        except Exception:
            pass
        logger.warning(f"水印 ffmpeg 返回 {r.returncode}：{r.stderr.decode('utf-8', 'replace')[-300:]}")
        return {"applied": False, "reason": "ffmpeg_failed"}

    try:
        os.replace(str(out), str(p))             # 就地替换（同盘原子）
    except Exception:
        logger.exception("水印替换原文件失败")
        try:
            out.unlink()
        except Exception:
            pass
        return {"applied": False, "reason": "replace_failed"}
    return {"applied": True, "text": text}
