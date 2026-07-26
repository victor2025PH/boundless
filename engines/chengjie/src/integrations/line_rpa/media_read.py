"""P2.4：LINE 图片消息识别地基——从 uiautomator XML 定位「最新一条对方图片气泡」+ 截屏裁剪。

背景：`ui_hierarchy` 只提取带 text 的节点，图片气泡（ImageView，无 text）在 XML
读取路径下完全不可见——对方发图要么落 `no_peer_text`、要么重读屏上旧文本被去重
吞掉，AI 对图片零感知。本模块提供纯函数地基：

- `find_latest_peer_image`：几何启发式定位左侧（对方）图片气泡，且仅当它比
  屏上最新一条消息文本（对方或己方气泡）更靠下（＝最新一条消息就是这张图）
  时才返回——旧图不重复触发。
- `crop_png_region`：按气泡 bounds 从整屏截图裁出图片位图（供共享识别层
  `src/inbox/media_enrich.enrich_inbound_media_text` 喂 Vision）。
- `image_sha256`：裁剪位图指纹（同图去重，防 vision 描述不稳定导致重复回复）。

全部纯函数、不做 adb / 网络调用；解析失败一律返回 None/空，绝不抛异常阻断主链。
"""

from __future__ import annotations

import hashlib
import io
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Tuple

from src.integrations.line_rpa.ui_hierarchy import _parse_bounds, _should_skip_text

logger = logging.getLogger(__name__)

# resource-id 命中即排除（头像/图标/输入区/贴图商店等非消息图片；几何过滤为主力，
# 此表为辅助保险——LINE 版本间 rid 不稳定，勿依赖白名单式精确匹配）
_EXCLUDE_RID_SUBSTR = (
    "avatar", "profile", "icon", "btn", "button", "keyboard", "emoji",
    "input", "send", "toolbar", "tab", "navigation", "status_bar", "title",
    "fab", "appbar", "background", "wallpaper", "header", "banner", "ad_",
    "sticker_shop", "voice",
)


@dataclass
class PeerImage:
    """候选图片气泡：bounds + 调试信息。"""
    left: int
    top: int
    right: int
    bottom: int
    rid: str = ""

    @property
    def bounds(self) -> Tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2.0


def _screen_size(root: ET.Element) -> Tuple[int, int]:
    """从全部节点 bounds 推屏幕宽高（取 max right / max bottom）。"""
    w, h = 0, 0
    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        w = max(w, bb[2])
        h = max(h, bb[3])
    return max(1, w), max(1, h)


def find_latest_peer_image(
    xml_bytes: bytes,
    *,
    text_left_ratio: float = 0.42,
    min_width_ratio: float = 0.16,
    min_height_ratio: float = 0.09,
    left_edge_max_ratio: float = 0.32,
    cx_max_ratio: float = 0.62,
    top_min_ratio: float = 0.04,
    bottom_max_ratio: float = 0.93,
) -> Tuple[Optional[PeerImage], str]:
    """定位「最新一条对方消息是图片」的气泡；不是最新/无图 → (None, reason)。

    启发式（全部相对屏幕尺寸，跨分辨率稳定）：
      1. 候选 = class 含 ImageView、无 text、rid 不在排除表、
         尺寸 ≥ (min_width_ratio*W, min_height_ratio*H)（滤头像/小图标）、
         左缘 ≤ left_edge_max_ratio*W 且中心 ≤ cx_max_ratio*W（对方在左；己方图右对齐不命中）、
         纵向落在 (top_min_ratio*H, bottom_max_ratio*H) 内（滤顶栏/输入栏）、
         非整屏背景（宽 ≥0.9W 且高 ≥0.55H 排除）。
      2. 取 bottom 最大（最靠下）的候选。
      3. 计算屏上最新一条**消息文本** bottom（对方左侧 + 己方右侧气泡都算；
         时间戳/UI 文案经 `_should_skip_text` 过滤，底部输入区之下不算）——
         图片必须比它更靠下才算「最新一条消息是图」，否则视为旧图不触发。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return None, f"xml_parse_error:{e}"

    w, h = _screen_size(root)
    min_w = max(48.0, w * min_width_ratio)
    min_h = max(48.0, h * min_height_ratio)
    top_min = h * top_min_ratio
    bottom_max = h * bottom_max_ratio

    best: Optional[PeerImage] = None
    latest_text_bottom = -1

    for el in root.iter():
        bb = _parse_bounds(el.get("bounds") or "")
        if not bb:
            continue
        l, t, r, b = bb
        text = (el.get("text") or "").strip()
        rid = (el.get("resource-id") or "").strip()
        cls = (el.get("class") or "")

        # —— 消息文本时间线（对方 + 己方气泡都算「最新消息」候选） ——
        if text and not _should_skip_text(text) and b <= bottom_max and t >= top_min:
            latest_text_bottom = max(latest_text_bottom, b)
            continue

        # —— 图片候选 ——
        if "ImageView" not in cls:
            continue
        if text:
            continue
        rid_low = rid.lower()
        if any(x in rid_low for x in _EXCLUDE_RID_SUBSTR):
            continue
        bw, bh = r - l, b - t
        if bw < min_w or bh < min_h:
            continue
        if bw >= w * 0.9 and bh >= h * 0.55:
            continue  # 整屏背景/壁纸
        if t < top_min or b > bottom_max:
            continue
        cx = (l + r) / 2.0
        if l > w * left_edge_max_ratio or cx > w * cx_max_ratio:
            continue  # 右侧（己方）或非气泡布局
        if best is None or b > best.bottom:
            best = PeerImage(left=l, top=t, right=r, bottom=b, rid=rid)

    if best is None:
        return None, "no_image_candidates"
    if latest_text_bottom > best.bottom:
        return None, (
            f"text_below_image:text_bottom={latest_text_bottom}"
            f":img_bottom={best.bottom}"
        )
    _ = text_left_ratio  # 保留形参：与 pick_last_peer_text 口径对齐的调优位
    return best, (
        f"image_latest bottom={best.bottom} size={best.width}x{best.height}"
        f" rid={best.rid[:48]}"
    )


def crop_png_region(
    png_bytes: bytes,
    bounds: Tuple[int, int, int, int],
    *,
    margin: int = 8,
    min_size_px: int = 24,
) -> Optional[bytes]:
    """从整屏 PNG 按 bounds（+margin，越界自动收敛）裁出区域；失败返回 None。"""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        w, h = im.size
        l, t, r, b = bounds
        l = max(0, int(l) - margin)
        t = max(0, int(t) - margin)
        r = min(w, int(r) + margin)
        b = min(h, int(b) + margin)
        if (r - l) < min_size_px or (b - t) < min_size_px:
            return None
        crop = im.crop((l, t, r, b))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.debug("crop_png_region 失败: %s", e)
        return None


def image_sha256(png_bytes: bytes) -> str:
    return hashlib.sha256(png_bytes or b"").hexdigest()


__all__ = [
    "PeerImage",
    "crop_png_region",
    "find_latest_peer_image",
    "image_sha256",
]
