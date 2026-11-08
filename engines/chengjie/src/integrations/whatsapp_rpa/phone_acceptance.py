"""多台手机真发验收。只使用号码闭环，不启动 Telegram、关怀或旧 RPA。

adb 一律经 ssh 到远端执行。禁止 kill-server、start-server、connect。
读屏由调用方注入（验收脚本注入 PP-OCR）。发送前用输入框里的实际文字
和要发的中文逐字核对，只有占位提示不算通过。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.integrations.whatsapp_rpa.phone_identity import (
    normalize_e164,
    wa_me_url,
)
from src.integrations.whatsapp_rpa.phone_notify import parse_wa_notifications
from src.integrations.whatsapp_rpa.phone_send import (
    PhoneScreen,
    composer_matches,
    is_screen_chrome,
    screen_is_rate_limited,
)

logger = logging.getLogger("wa_phone_acceptance")

_FORBIDDEN_ADB = frozenset({"kill-server", "start-server", "connect"})
_ALLOWED_ADB = frozenset({"shell", "exec-out", "pull"})
TOKEN_ENV_NAMES = ("WA_DECIDE_TOKEN", "BOUNDLESS_BUS_TOKEN")


def ssh_adb_argv(host: str, serial: str, adb_args: Sequence[str]) -> List[str]:
    """拼出 ``ssh <host> adb -s <serial> ...``。本地不调用 adb。"""
    remote_host = str(host or "").strip()
    device = str(serial or "").strip()
    if not remote_host or not device:
        raise ValueError("adb ssh requires host and serial")
    tokens: List[str] = []
    for arg in adb_args:
        tokens.extend(str(arg).split())
    if not tokens or tokens[0] not in _ALLOWED_ADB:
        raise ValueError("adb command must be shell, exec-out, or pull")
    for token in tokens:
        if token in _FORBIDDEN_ADB:
            raise ValueError(f"forbidden adb command: {token}")
    remote = " ".join(
        ["adb", "-s", shlex.quote(device), *[shlex.quote(str(arg)) for arg in adb_args]]
    )
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", remote_host, remote]


def redact_text(text: str, secrets: Sequence[str]) -> str:
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "***")
    return out


def safe_log_fields(fields: Dict[str, Any], secrets: Sequence[str]) -> Dict[str, Any]:
    """日志和报告里去掉 token、Authorization。"""
    hidden = {"authorization", "token", "bearer", "api_key", "access_token"}
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        if str(key).lower() in hidden:
            continue
        if isinstance(value, str):
            clean[key] = redact_text(value, secrets)
        else:
            clean[key] = value
    return clean


def compare_prefill(expected: str, composer: str) -> Dict[str, Any]:
    """核对预填正文。只有「输入消息」这类占位不算通过。"""
    observed = str(composer or "")
    match = bool(composer_matches(observed, expected))
    placeholder_only = _looks_placeholder(observed)
    return {
        "expected": expected,
        "observed": observed,
        "match": match and not placeholder_only,
        "placeholder_only": placeholder_only,
        "expected_chars": len(expected or ""),
        "observed_chars": len(observed),
    }


def _looks_placeholder(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    from src.integrations.whatsapp_rpa.phone_send import _is_hint_line
    return all(_is_hint_line(line) for line in lines)


def to_device_point(x: float, y: float, src_w: float, src_h: float,
                    device_w: float, device_h: float) -> tuple:
    """把截图或 OCR 坐标换成 adb input tap 用的设备坐标。

    真机上 OCR 框在大约 720×1600 的图里，``wm size`` 往往是 1080×2400。
    直接把 OCR 的 y=1450 拿去点，会点在屏幕中部，打不中底部的发送按钮。
    """
    if src_w <= 0 or src_h <= 0 or device_w <= 0 or device_h <= 0:
        return (int(round(x)), int(round(y)))
    return (
        int(round(float(x) * float(device_w) / float(src_w))),
        int(round(float(y) * float(device_h) / float(src_h))),
    )


def find_green_send_center(png: bytes):
    """在截图像素里找输入框右侧的饱和绿色圆钮，返回圆心。

    顶栏也是绿色，所以只在画面下半、靠右 20% 里找。找不到返回 None。
    """
    if not png or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    try:
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return None
    width, height = image.size
    if width < 8 or height < 8:
        return None
    pixels = image.load()
    x0 = int(width * 0.80)
    y0 = int(height * 0.50)
    total_x = 0
    total_y = 0
    count = 0
    for y in range(y0, height):
        for x in range(x0, width):
            red, green, blue = pixels[x, y][:3]
            if green >= 150 and green >= red + 45 and green >= blue + 25:
                total_x += x
                total_y += y
                count += 1
    if count < 20:
        return None
    return (total_x / count, total_y / count)


def locate_send_tap(png: bytes, device_size: Sequence[float], screen: Optional[PhoneScreen] = None) -> Dict[str, Any]:
    """发送按钮的设备坐标。优先用截图里的绿色圆，否则用正文中心那一行。

    x 约在屏幕宽度的 0.907。y 和输入框正文中心对齐；键盘收起后大约在 0.904 屏高。
    """
    device_w = int(device_size[0]) if device_size else 0
    device_h = int(device_size[1]) if device_size else 0
    shot_w, shot_h = png_size(png)
    ocr_w, ocr_h = shot_w, shot_h
    if screen is not None and len(getattr(screen, "ocr_size", []) or []) >= 2:
        ocr_w, ocr_h = int(screen.ocr_size[0]), int(screen.ocr_size[1])
    green = find_green_send_center(png)
    if green is not None and shot_w and shot_h and device_w and device_h:
        tap = to_device_point(green[0], green[1], shot_w, shot_h, device_w, device_h)
        how = "green"
    else:
        cy = None
        for line in (getattr(screen, "ocr_lines", None) or []):
            box = line.get("box") or []
            if len(box) < 4:
                continue
            text = str(line.get("text") or "")
            if text and text == getattr(screen, "composer", ""):
                cy = (float(box[1]) + float(box[3])) / 2.0
                break
        if cy is None:
            for line in (getattr(screen, "ocr_lines", None) or []):
                box = line.get("box") or []
                if len(box) >= 4 and str(line.get("text") or "").strip():
                    cy = (float(box[1]) + float(box[3])) / 2.0
        if cy is not None and ocr_h and device_h:
            _x, y = to_device_point(0, cy, ocr_w or shot_w or 1, ocr_h, device_w or 1, device_h)
            how = "composer"
        else:
            y = int(round(device_h * 0.904)) if device_h else 0
            how = "fraction"
        x = int(round(device_w * 0.907)) if device_w else 0
        tap = (x, y)
    return {
        "tap": [int(tap[0]), int(tap[1])],
        "device_size": [device_w, device_h],
        "shot_size": [int(shot_w), int(shot_h)],
        "ocr_size": [int(ocr_w or 0), int(ocr_h or 0)],
        "tap_source": how,
    }


def png_size(png: bytes) -> tuple:
    if not png or len(png) < 24 or png[12:16] != b"IHDR":
        return (0, 0)
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return (width, height)


# 键盘弹起时输入栏不在屏幕最底。核心行是最靠下的那行正文，
# 只并入和它垂直连着的行。ceb233 真机：正文 [111,1428,474,1472]，
# 其下「D」[507,1535,544,1572] 是键盘建议条或表情条上的图标。
_COMPOSER_PITCH = 4.0
_LEFT_SLACK = 20.0
_ICON_GUTTER = 0.15


def _box_width(box: Sequence[float]) -> float:
    return max(0.0, float(box[2]) - float(box[0]))


def _box_height(box: Sequence[float]) -> float:
    return max(1.0, float(box[3]) - float(box[1]))


def _box_cy(box: Sequence[float]) -> float:
    return (float(box[1]) + float(box[3])) / 2.0


def _is_body_line(box: Sequence[float]) -> bool:
    """一行字，不是方块图标。真机那个「D」大约 37×37，不能当输入框核心。"""
    width = _box_width(box)
    height = _box_height(box)
    return width >= 80 or width >= height * 2.5


def _composer_cluster(items: Sequence[tuple]) -> List[tuple]:
    """从输入栏最底一行往上，合并左边缘相近、行距还连着的折行。

    左边缘相差超过 20px 的不是同一栏。行距用相邻两行中心的垂直距离，
    不超过 2.2 倍行高才继续往上收。中间断开就停。
    真机三行回复被折成五行，行距大约 50px，旧的 1.5 倍阈值只剩最底两行。
    「D」的左边缘离正文很远，仍然不并进来。
    """
    if not items:
        return []
    bodies = [item for item in items if _is_body_line(item[1])]
    core = max(bodies or list(items), key=lambda item: float(item[1][3]))
    core_left = float(core[1][0])
    column = [
        item for item in items
        if abs(float(item[1][0]) - core_left) <= _LEFT_SLACK
    ]
    column.sort(key=lambda item: _box_cy(item[1]), reverse=True)
    kept = [column[0]]
    for item in column[1:]:
        prev = kept[-1]
        pitch = max(_box_height(prev[1]), _box_height(item[1]))
        dist = abs(_box_cy(item[1]) - _box_cy(prev[1]))
        if dist > pitch * _COMPOSER_PITCH:
            break
        kept.append(item)
    # 同一行贴着正文右侧拆开的字可以留下。左侧差超过 20px 的框（滑动时列表里的「e」）不要。
    for item in items:
        if item in kept:
            continue
        box = item[1]
        if float(box[0]) < core_left - _LEFT_SLACK:
            continue
        for kept_item in list(kept):
            other = kept_item[1]
            overlap = min(float(box[3]), float(other[3])) - max(float(box[1]), float(other[1]))
            gap = _horizontal_gap(box, other)
            if overlap > min(_box_height(box), _box_height(other)) * 0.5 and gap <= _LEFT_SLACK:
                kept.append(item)
                break
    kept.sort(key=lambda item: (float(item[1][1]), float(item[1][0])))
    return kept


def _horizontal_gap(left: Sequence[float], right: Sequence[float]) -> float:
    """两框水平方向的空隙。重叠时是 0。"""
    a0, a1 = float(left[0]), float(left[2])
    b0, b1 = float(right[0]), float(right[2])
    if a1 < b0:
        return b0 - a1
    if b1 < a0:
        return a0 - b1
    return 0.0


def _without_input_icons(items: Sequence[tuple], width: float) -> List[tuple]:
    """从输入栏文字框里去掉两侧图标。

    来由：真机 pair-chat（evidence/pair0608_0203_4/005-a-read2.ocr.json）里，
    要发的是「你好，早上好，在忙吗」，OCR 读成同一句再加一行「D」。
    「D」的框在输入栏右侧，又小，和正文隔开一段，是附件或相机图标，不是句子里的字母。

    同时满足下面三条才丢掉，缺一条就留着：
    - 框中心落在屏幕左侧 15% 或右侧 15%（图标槽，不是打字区）
    - 框宽小于屏幕宽度的 8%，并且不到同一栏里最宽正文的四分之一
    - 和最宽正文框的水平空隙至少有屏幕宽度的 2%（贴着正文的拆字留下）
    只有一个框时不动。不看文字是不是字母。
    """
    if width <= 0 or len(items) < 2:
        return list(items)
    widths = [_box_width(box) for _text, box in items]
    main = max(range(len(widths)), key=lambda index: widths[index])
    main_box = items[main][1]
    main_width = widths[main]
    kept: List[tuple] = []
    for index, item in enumerate(items):
        _text, box = item
        if index == main:
            kept.append(item)
            continue
        center_x = (float(box[0]) + float(box[2])) / 2.0
        in_gutter = center_x < width * _ICON_GUTTER or center_x > width * (1.0 - _ICON_GUTTER)
        narrow = widths[index] < width * 0.08 and widths[index] < main_width * 0.25
        gap = _horizontal_gap(box, main_box)
        if in_gutter and narrow and gap >= width * 0.02:
            continue
        kept.append(item)
    return kept


def _is_own_green(red: int, green: int, blue: int) -> bool:
    """己方气泡底色大约 #D9FDD3，比发送钮淡，绿只略高于红和蓝。"""
    if green < 210 or red < 180 or blue < 170:
        return False
    if green < red or green < blue:
        return False
    if green - red > 70:
        return False
    return green - blue >= 8


def _is_peer_white(red: int, green: int, blue: int) -> bool:
    return min(red, green, blue) >= 245 and max(red, green, blue) - min(red, green, blue) <= 14


def _median_channel(values: Sequence[int]) -> int:
    ordered = sorted(int(v) for v in values)
    if not ordered:
        return 0
    return ordered[len(ordered) // 2]


def _fill_around(png: bytes, box: Sequence[float]) -> str:
    """看气泡框内部、文字间隙里的底色中位数。浅绿是己方，白是对方。"""
    if not png or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    try:
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return ""
    width, height = image.size
    pixels = image.load()
    left, top, right, bottom = (int(float(v)) for v in box[:4])
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width, right))
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height, bottom))
    reds: List[int] = []
    greens: List[int] = []
    blues: List[int] = []
    step_x = max(1, (right - left) // 8)
    step_y = max(1, (bottom - top) // 6)
    for y in range(top + 1, bottom, step_y):
        for x in range(left + 1, right, step_x):
            red, green, blue = pixels[x, y][:3]
            if red + green + blue < 280:
                continue
            reds.append(red)
            greens.append(green)
            blues.append(blue)
    if len(reds) < 4:
        return ""
    red, green, blue = (
        _median_channel(reds), _median_channel(greens), _median_channel(blues),
    )
    if _is_own_green(red, green, blue):
        return "green"
    if _is_peer_white(red, green, blue):
        return "white"
    return ""


def _edge_gap(upper: Sequence[float], lower: Sequence[float]) -> float:
    return float(lower[1]) - float(upper[3])


def _band_fill(png: bytes, upper: Sequence[float], lower: Sequence[float]) -> str:
    """两行之间的底色。气泡色连着是同一条，露出聊天背景就是两条。"""
    gap = _edge_gap(upper, lower)
    if gap <= 2:
        return "touch"
    if not png or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    try:
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return ""
    width, height = image.size
    pixels = image.load()
    top = int(float(upper[3])) + 1
    bottom = int(float(lower[1])) - 1
    if bottom < top:
        return "touch"
    left = max(0, int(min(float(upper[0]), float(lower[0]))) - 4)
    right = min(width - 1, int(max(float(upper[2]), float(lower[2]))) + 4)
    green = white = other = 0
    y = top
    while y <= bottom and y < height:
        x = left
        while x <= right:
            red, g, blue = pixels[x, y][:3]
            if _is_own_green(red, g, blue):
                green += 1
            elif _is_peer_white(red, g, blue):
                white += 1
            else:
                other += 1
            x += 3
        y += 2
    if other >= 6 and other > green + white:
        return "gap"
    if green >= 4 and green >= white:
        return "green"
    if white >= 4 and white > green:
        return "white"
    return ""


def _same_bubble(prev: dict, text: str, box: Sequence[float], side: str, png: bytes) -> bool:
    """时间戳切开。底色间隙切开。没有截图时，只有贴得很近的折行才并。"""
    if abs(float(box[0]) - float(prev["left"])) > _LEFT_SLACK:
        return False
    if side != prev["side"]:
        return False
    gap = _edge_gap(prev["box"], box)
    fill = _band_fill(png, prev["box"], box)
    if fill == "gap":
        return False
    if fill in {"green", "white", "touch"}:
        return True
    height = max(_box_height(prev["box"]), 1.0)
    return gap <= max(8.0, height * 0.55)


def _merge_bubbles(items: Sequence[tuple], width: float, png: bytes) -> tuple:
    """折行收成一条。行间露出聊天背景，或遇到时间戳，就另起一个气泡。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_body, strip_bubble_tail

    incoming: List[str] = []
    outgoing: List[str] = []
    transcript: List[dict] = []
    groups: List[dict] = []

    def close() -> None:
        if not groups:
            return
        group = groups[-1]
        if group.get("closed"):
            return
        group["closed"] = True

    ordered = sorted(items, key=lambda item: (_box_cy(item[1]), float(item[1][0])))
    for text, box in ordered:
        if text == "\x00time":
            close()
            continue
        side = _bubble_side(box, width, png)
        if groups and not groups[-1].get("closed") and _same_bubble(groups[-1], text, box, side, png):
            groups[-1]["lines"].append(text)
            groups[-1]["box"] = box
            groups[-1]["bottom"] = float(box[3])
            continue
        groups.append({
            "side": side, "left": float(box[0]), "box": box,
            "top": float(box[1]), "bottom": float(box[3]), "lines": [text],
        })
    for group in groups:
        body = strip_bubble_tail("".join(group["lines"]))
        if len(composer_body(body)) < 2:
            continue
        row = {
            "side": "out" if group["side"] == "out" else "in",
            "text": body, "top": group["top"], "bottom": group["bottom"],
        }
        transcript.append(row)
        if row["side"] == "out":
            outgoing.append(body)
        else:
            incoming.append(body)
    return incoming, outgoing, transcript


def _bubble_side(box: Sequence[float], width: float, png: bytes) -> str:
    """先看气泡底色，看不清再看左边缘。左边缘小于 0.12W 是对方。"""
    fill = _fill_around(png, box)
    if fill == "green":
        return "out"
    if fill == "white":
        return "in"
    if float(box[0]) < float(width) * 0.12:
        return "in"
    return "out"


_MENU_LABELS = frozenset({
    "编辑", "转发", "删除", "收藏", "贴纸", "查看贴纸", "添加到收藏",
    "查看联系人", "搜索", "静音", "静音通知", "壁纸", "清空对话", "清空聊天",
    "举报", "屏蔽", "更多", "导出聊天", "添加快捷方式",
})


def _key_sized(text: str, box: Sequence[float], width: float) -> bool:
    """一个键帽大小的英文字母。它不是输入框里的草稿。"""
    raw = str(text or "")
    if len(raw) != 1 or not raw.isascii():
        return False
    return _box_width(box) <= max(80.0, float(width) * 0.16)


def _keyboard_zone_top(items: Sequence[tuple], width: float, height: float):
    """下半屏挤满单字符框，或出现 ?123 这类键帽，就是键盘。返回键盘顶边。"""
    from src.integrations.whatsapp_rpa.phone_send import is_keycap

    singles: List[Sequence[float]] = []
    caps: List[Sequence[float]] = []
    for text, box in items:
        if _box_cy(box) < float(height) * 0.40:
            continue
        if is_keycap(text):
            caps.append(box)
            continue
        if len(text) <= 1 and _box_width(box) <= max(56.0, float(width) * 0.14):
            singles.append(box)
    if caps:
        return min(float(box[1]) for box in caps)
    singles.sort(key=_box_cy)
    top = None
    for index, box in enumerate(singles):
        cy = _box_cy(box)
        row = [box]
        for other in singles[index + 1:]:
            if abs(_box_cy(other) - cy) <= 36:
                row.append(other)
        if len(row) >= 2:
            row_top = min(float(item[1]) for item in row)
            top = row_top if top is None else min(top, row_top)
    return top


def _send_button_present(lines: Sequence[Dict[str, Any]], width: float, height: float) -> bool:
    """输入栏右侧的发送钮或麦克风圆。键盘区里的键帽不算。"""
    from src.integrations.whatsapp_rpa.phone_send import is_keycap

    items = []
    for line in lines or []:
        text = str((line or {}).get("text") or "").strip()
        box = (line or {}).get("box") or []
        if len(box) < 4:
            continue
        items.append((text, box))
    zone = _keyboard_zone_top(items, width, height) if width and height else None
    for text, box in items:
        if is_keycap(text):
            continue
        cy = _box_cy(box)
        if zone is not None and cy >= float(zone) - 2:
            continue
        cx = (float(box[0]) + float(box[2])) / 2.0
        if width <= 0 or cx < float(width) * 0.82:
            continue
        if _box_width(box) > max(72.0, float(width) * 0.16):
            continue
        return True
    return False


def _header_noise(text: str) -> bool:
    """在线状态和加密提示不是号码，也不拿去当联系人名。"""
    raw = str(text or "").strip()
    folded = raw.casefold()
    if raw in {"在线", "在線"} or folded in {"online", "typing"}:
        return True
    if folded.startswith("last seen") or raw.startswith("正在输入"):
        return True
    return "端到端加密" in raw or "end-to-end" in folded


def _header_text(parts: Sequence[str]) -> str:
    """有加号号码框时只用那一框。省略号留在原文里，后面的框不拼进来。"""
    from src.integrations.whatsapp_rpa.phone_identity import header_number_candidates

    plus = [text for text in parts if header_number_candidates(text)]
    if plus:
        return plus[0].strip()
    return " ".join(part.strip() for part in parts if part.strip())


def lines_to_screen(lines: Sequence[Dict[str, Any]], width: int, height: int,
                    png: bytes = b"") -> PhoneScreen:
    """按 PP-OCR 的像素框把文字分成顶栏、输入框、对方气泡、己方气泡。

    对方在左侧，己方在右侧，看文字框靠哪一边，不看中心点是否过半。
    输入框只取底部输入栏。时间、日期、勾号和加密提示不进气泡。
    """
    from src.integrations.whatsapp_rpa.phone_send import is_bubble_boundary, is_keycap, is_screen_chrome

    header: List[str] = []
    composer_items: List[tuple] = []
    boundaries: List[tuple] = []
    notes: List[str] = []
    if width <= 0 or height <= 0:
        return PhoneScreen()
    for line in lines or []:
        text = str(line.get("text") or "").strip()
        box = line.get("box") or []
        if not text or len(box) < 4:
            continue
        cy = (float(box[1]) + float(box[3])) / 2.0
        # 只丢掉贴着最顶的时钟和电量。y≈86–121 的 + 号是会话顶栏，不能按 5% 屏高切掉。
        from src.integrations.whatsapp_rpa.phone_identity import header_number_candidates
        status_cut = min(64.0, float(height) * 0.03)
        if cy < status_cut and not header_number_candidates(text):
            continue
        if cy < height * 0.18 or (
            header_number_candidates(text) and cy < height * 0.25
        ):
            if _header_noise(text) or is_screen_chrome(text):
                notes.append(text)
                continue
            header.append(text)
            continue
        if is_bubble_boundary(text):
            boundaries.append(("\x00time", box))
            notes.append(text)
            continue
        if is_screen_chrome(text):
            notes.append(text)
            continue
        if text in _MENU_LABELS:
            notes.append(text)
            continue
        composer_items.append((text, box))
    from src.integrations.whatsapp_rpa.phone_send import composer_is_placeholder as _placeholder
    keyboard_floor = 1200.0 if float(height) <= 1800 else float(height) * 0.72
    kept_items = []
    for text, box in composer_items:
        # 键盘弹出后，贴着左缘、y 超过大约 1200 的短词是键帽。长句和「发消息」留下。
        if (
            not _placeholder(text)
            and float(box[1]) >= keyboard_floor
            and float(box[0]) < 80
            and _box_width(box) < 120
        ):
            notes.append(text)
            continue
        kept_items.append((text, box))
    composer_items = kept_items
    zone = _keyboard_zone_top(composer_items, float(width), float(height))
    if zone is not None:
        above: List[tuple] = []
        for text, box in composer_items:
            if _placeholder(text) or float(box[1]) < float(zone) - 2:
                above.append((text, box))
                continue
            if is_keycap(text) or _key_sized(text, box, float(width)):
                notes.append(text)
                continue
            above.append((text, box))
        composer_items = above
    cluster = _composer_cluster(composer_items)
    kept_composer = _without_input_icons(cluster, float(width))
    composer = [text for text, _box in kept_composer if not is_keycap(text)]
    cluster_ids = {id(item) for item in cluster}
    bubbles = [item for item in composer_items if id(item) not in cluster_ids]
    incoming, outgoing, transcript = _merge_bubbles(
        list(bubbles) + list(boundaries), float(width), png,
    )
    composer_left = float(cluster[0][1][0]) if cluster else 0.0
    # cluster is sorted by top, so the first box is the top line. Use the core left.
    if cluster:
        core = max(cluster, key=lambda item: float(item[1][3]))
        composer_left = float(core[1][0])
    return PhoneScreen(
        header=_header_text(header),
        composer="\n".join(composer),
        outgoing=outgoing,
        incoming=incoming,
        last_incoming=incoming[-1] if incoming else "",
        notes=notes,
        bubbles=transcript,
        ocr_lines=[
            {"text": str(line.get("text") or ""), "box": list(line.get("box") or [])}
            for line in lines or []
            if isinstance(line, dict)
        ],
        composer_left=composer_left,
        shot_size=[int(width), int(height)],
    )


def peer_after_last_own(screen: Any) -> List[str]:
    """只取最后一条己方气泡下面的对方气泡。单字和更短的孤立框丢掉。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_body

    rows = list(getattr(screen, "bubbles", None) or [])
    last_own = None
    for row in rows:
        if str(row.get("side") or "") == "out":
            last_own = float(row.get("bottom") or 0)
    if last_own is None:
        incoming_rows = [
            row for row in rows
            if str(row.get("side") or "") == "in" and len(composer_body(str(row.get("text") or ""))) >= 2
        ]
        if not incoming_rows:
            return []
        return [str(incoming_rows[-1].get("text") or "").strip()]
    fresh: List[str] = []
    for row in rows:
        if str(row.get("side") or "") != "in":
            continue
        if float(row.get("top") or 0) < last_own - 4:
            continue
        text = str(row.get("text") or "").strip()
        if len(composer_body(text)) < 2:
            continue
        fresh.append(text)
    return fresh


def new_left_bubbles(incoming: Sequence[str], baseline: Sequence[str]) -> List[str]:
    """只留这次发送之后新出现的左侧气泡。基线里已有的旧消息不算。"""
    from src.integrations.whatsapp_rpa.phone_send import is_screen_chrome

    pending = [str(item or "").strip() for item in baseline or [] if str(item or "").strip()]
    fresh: List[str] = []
    for raw in incoming or []:
        text = str(raw or "").strip()
        if not text or is_screen_chrome(text):
            continue
        if text in pending:
            pending.remove(text)
            continue
        fresh.append(text)
    return fresh


def make_ppocr_reader(base_url: str, post_json: Optional[Callable[..., Dict[str, Any]]] = None,
                      timeout: float = 20.0) -> Callable[[bytes], PhoneScreen]:
    """``POST {ocr-url}/v1/ocr``，body ``{"image_b64": ...}``。"""
    url = str(base_url or "").rstrip("/") + "/v1/ocr"

    def read(png: bytes) -> PhoneScreen:
        if not png:
            return PhoneScreen()
        import base64
        payload = {"image_b64": base64.b64encode(png).decode("ascii")}
        if post_json is not None:
            data = post_json(url, payload)
        else:
            data = _post_json(url, payload, timeout=timeout, headers=None)
        lines = list((data or {}).get("lines") or [])
        width, height = png_size(png)
        screen = lines_to_screen(lines, width, height, png=png)
        screen.ocr_lines = [
            {"text": str(line.get("text") or ""), "box": list(line.get("box") or [])}
            for line in lines
            if isinstance(line, dict)
        ]
        screen.shot_size = [int(width), int(height)]
        reported_w = (data or {}).get("width") or (data or {}).get("image_width") or width
        reported_h = (data or {}).get("height") or (data or {}).get("image_height") or height
        screen.ocr_size = [int(reported_w or 0), int(reported_h or 0)]
        screen.source = "ocr"
        return screen

    return read


def decide_request_body(*, account_id: str, phone: str, last_inbound: str,
                        persona_id: str = "") -> Dict[str, Any]:
    from src.integrations.whatsapp_rpa.phone_identity import conversation_id
    hint: Dict[str, Any] = {}
    if persona_id:
        hint["persona"] = persona_id
    return {
        "message": {
            "platform": "whatsapp",
            "account": account_id,
            "external_id": normalize_e164(phone),
            "text": last_inbound,
            "session_id": conversation_id(account_id, phone),
            "context_hint": hint,
        }
    }


def make_http_decide(url: str, token: str = "",
                     post_json: Optional[Callable[..., Dict[str, Any]]] = None):
    """向已经在跑的 decide 接口要草稿。token 只放请求头，不进日志。"""

    async def decide(*, account_id: str, phone: str, last_inbound: str, persona_id: str = "",
                     agent_instruction: str = "", self_name: str = "",
                     peer_names: Optional[list] = None):
        del agent_instruction, self_name, peer_names
        body = decide_request_body(
            account_id=account_id, phone=phone, last_inbound=last_inbound,
            persona_id=persona_id,
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        sender = post_json or _post_json
        data = sender(url, body, timeout=60.0, headers=headers)
        logger.info("%s", json.dumps(safe_log_fields(
            {"event": "decide", "url": url, "action": (data or {}).get("action")},
            [token],
        ), ensure_ascii=False))
        reply = str((data or {}).get("text") or (data or {}).get("reply") or "").strip()
        action = str((data or {}).get("action") or "")
        return {"ok": action == "draft" and bool(reply), "reply": reply, "action": action}

    return decide


def _post_json(url: str, body: Dict[str, Any], timeout: float = 20.0,
               headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    import urllib.request
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class DeviceBusy(RuntimeError):
    def __init__(self, serial: str) -> None:
        self.serial = serial
        super().__init__(f"设备 {serial} 正在被另一个验收占用")


class DeviceLock:
    """同一台手机的独占锁。锁文件按序列号放在 data-dir，进程退出或 release 后放开。"""

    def __init__(self, directory: Path, serial: str) -> None:
        self.serial = str(serial or "").strip()
        safe = "".join(ch if ch.isalnum() else "_" for ch in self.serial) or "device"
        self.path = Path(directory) / f".adb-{safe}.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if not self.serial:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os_open_exclusive(self.path)
            except FileExistsError:
                if not lock_is_stale(self.path):
                    raise DeviceBusy(self.serial)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            os.write(fd, f"{os.getpid()}\n".encode())
            self._fd = fd
            return
        raise DeviceBusy(self.serial)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            if self.path.exists():
                self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "DeviceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def os_open_exclusive(path: Path) -> int:
    return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)


def lock_is_stale(path: Path, *, max_age_sec: float = 7200.0) -> bool:
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(text[0]) if text else 0
    except (OSError, ValueError):
        return True
    if not _pid_alive(pid):
        return True
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > max_age_sec


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def lock_serials(directory: Path, serials: Sequence[str]):
    """按序列号排序加锁，避免两台手机交叉抢锁。中途失败时放开已经拿到的锁。"""
    locks = [DeviceLock(directory, serial) for serial in sorted({s for s in serials if str(s).strip()})]
    held: List[DeviceLock] = []
    try:
        for lock in locks:
            lock.acquire()
            held.append(lock)
    except DeviceBusy:
        for lock in held:
            lock.release()
        raise
    return held


class EvidenceLog:
    def __init__(self, directory: Path, secrets: Optional[Sequence[str]] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in (secrets or []) if s]
        self.steps: List[Dict[str, Any]] = []
        self.turns: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self.timings: List[Dict[str, Any]] = []

    def save(self, name: str, png: bytes, screen: PhoneScreen, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        index = len(self.steps) + 1
        stem = f"{index:03d}-{name}"
        png_path = self.dir / f"{stem}.png"
        ocr_path = self.dir / f"{stem}.ocr.json"
        png_path.write_bytes(png or b"")
        ocr = {
            "header": screen.header,
            "composer": screen.composer,
            "outgoing": list(screen.outgoing or []),
            "last_incoming": screen.last_incoming,
            "notes": list(getattr(screen, "notes", []) or []),
            "incoming": list(getattr(screen, "incoming", []) or []),
            "source": str(getattr(screen, "source", "") or ""),
            "ui_dump": str(getattr(screen, "ui_dump", "") or ""),
            "ocr_lines": list(getattr(screen, "ocr_lines", []) or []),
        }
        if extra:
            ocr.update(safe_log_fields(extra, self.secrets))
        ocr_path.write_text(json.dumps(ocr, ensure_ascii=False, indent=2), encoding="utf-8")
        step = {"name": name, "png": str(png_path), "ocr": str(ocr_path), **ocr}
        self.steps.append(step)
        self._write(self._body(partial=True))
        logger.info("%s", json.dumps(safe_log_fields(
            {"event": "evidence", "name": name, "header": screen.header, "composer": screen.composer},
            self.secrets,
        ), ensure_ascii=False))
        return step

    def _body(self, *, partial: bool, **extra: Any) -> Dict[str, Any]:
        body = {
            "partial": partial,
            "steps": self.steps,
            "turns": self.turns,
            "history": self.history,
            "timings": self.timings,
        }
        body.update(extra)
        if "ok" not in body:
            body["ok"] = False
        return body

    def _write(self, report: Dict[str, Any]) -> Dict[str, Any]:
        clean = json.loads(redact_text(json.dumps(report, ensure_ascii=False), self.secrets))
        path = self.dir / "report.json"
        path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        clean["report_path"] = str(path)
        return clean

    def note(self, *, turns: Optional[List[Dict[str, Any]]] = None,
             history: Optional[List[Dict[str, Any]]] = None,
             timings: Optional[List[Dict[str, Any]]] = None) -> None:
        if turns is not None:
            self.turns = list(turns)
        if history is not None:
            self.history = list(history)
        if timings is not None:
            self.timings = list(timings)

    def checkpoint(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """每一步都落盘。轮次、历史台账和耗时分段不会被下一次截图冲掉。"""
        self.note(
            turns=report.get("turns"),
            history=report.get("history"),
            timings=report.get("timings"),
        )
        extra = {
            k: v for k, v in report.items()
            if k not in {"steps", "turns", "history", "timings", "partial"}
        }
        return self._write(self._body(partial=report.get("ok") is not True, **extra))

    def finish(self, report: Dict[str, Any]) -> Dict[str, Any]:
        self.note(
            turns=report.get("turns"),
            history=report.get("history"),
            timings=report.get("timings"),
        )
        extra = {
            k: v for k, v in report.items()
            if k not in {"steps", "turns", "history", "timings", "partial"}
        }
        extra["ok"] = bool(report.get("ok"))
        return self._write(self._body(partial=False, **extra))


class RecordingDevice:
    """把每一次读屏的截图和 OCR 落盘，再交给原来的设备。"""

    def __init__(self, inner: Any, evidence: EvidenceLog, label: str) -> None:
        self.inner = inner
        self.evidence = evidence
        self.label = label
        self._reads = 0

    async def open_url(self, url: str) -> None:
        await self.inner.open_url(url)

    async def screencap(self) -> bytes:
        data = await self.inner.screencap()
        return data or b""

    async def tap_send(self) -> None:
        await self.inner.tap_send()

    async def press_back(self) -> None:
        await self.inner.press_back()

    async def poll_notifications(self):
        poll = getattr(self.inner, "poll_notifications", None)
        if poll is None:
            return []
        return await poll()

    async def read_screen(self, png: bytes) -> PhoneScreen:
        self._reads += 1
        screen = await self.inner.read_screen(png)
        extra = getattr(self.inner, "last_tap", None)
        self.evidence.save(
            f"{self.label}-read{self._reads}", png, screen,
            extra=extra if isinstance(extra, dict) else None,
        )
        return screen

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def subprocess_runner(argv: Sequence[str]) -> tuple:
    import subprocess
    proc = subprocess.run(list(argv), capture_output=True, timeout=40)
    return proc.returncode, proc.stdout or b"", (proc.stderr or b"").decode("utf-8", "replace")


_UI_XML_PATH = "/sdcard/wa_ui.xml"
_TITLE_KEYS = (
    "conversation_contact_name",
    "contact_name",
    "toolbar_title",
    "action_bar_title",
)
_CHEVRON = {"<", "‹", "＜", ">"}


def _xml_payload(raw: bytes) -> bytes:
    if not raw:
        return b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    start = raw.find(b"<")
    return raw[start:] if start >= 0 else b""


def _bounds(value: str):
    import re
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(value or "").strip())
    if not match:
        return None
    return tuple(int(match.group(index)) for index in range(1, 5))


def _is_entry(resource_id: str) -> bool:
    return str(resource_id or "").lower().endswith("/entry")


def _is_title(resource_id: str) -> bool:
    rid = str(resource_id or "").lower()
    if "conversations_row" in rid:
        return False
    return any(key in rid for key in _TITLE_KEYS)


def _is_message(resource_id: str) -> bool:
    return "message_text" in str(resource_id or "").lower()


def screen_from_ui_xml(raw: bytes) -> Optional[PhoneScreen]:
    """从 uiautomator 的 UI 树读精确文字。读不到输入框节点就返回 None，调用方改走 OCR。

    输入框是 ``com.whatsapp:id/entry`` 的 text，不把旁边的图标算进去。
    顶栏用 ``conversation_contact_name`` 这类节点。气泡只用 ``message_text``，
    靠 bounds 分左右。加密提示和单独的「<」不是气泡。
    """
    import xml.etree.ElementTree as ET

    payload = _xml_payload(raw)
    if not payload:
        return None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    width = 0
    titles: List[str] = []
    entry: Optional[str] = None
    entry_bottom = -1
    bubbles: List[tuple] = []
    for node in root.iter():
        bounds = _bounds(node.get("bounds") or "")
        if bounds and bounds[2] > width:
            width = bounds[2]
        rid = node.get("resource-id") or ""
        text = str(node.get("text") or "")
        if _is_entry(rid):
            bottom = bounds[3] if bounds else 0
            if entry is None or bottom >= entry_bottom:
                entry = text
                entry_bottom = bottom
            continue
        shown = text.strip() or str(node.get("content-desc") or "").strip()
        if _is_title(rid) and shown:
            titles.append(shown)
            continue
        if not _is_message(rid) or not shown or not bounds:
            continue
        if shown in _CHEVRON or is_screen_chrome(shown):
            continue
        bubbles.append((bounds[1], bounds, shown))
    if entry is None:
        return None
    if width <= 0:
        width = 1080
    incoming: List[str] = []
    outgoing: List[str] = []
    for _top, bounds, shown in sorted(bubbles, key=lambda item: item[0]):
        left = float(bounds[0])
        right_gap = float(width) - float(bounds[2])
        if right_gap < left:
            outgoing.append(shown)
        elif left < right_gap:
            incoming.append(shown)
    return PhoneScreen(
        header=" ".join(titles),
        composer=entry,
        outgoing=outgoing,
        incoming=incoming,
        last_incoming=incoming[-1] if incoming else "",
        source="uiautomator",
    )


class SshAdbPhoneDevice:
    """通过 ssh 在远端执行 adb。测试注入 runner，不会真的连上主机。

    读屏先 ``uiautomator dump`` 再 ``cat`` UI 树。树里没有输入框时才用 OCR。
    """

    def __init__(self, host: str, serial: str, read_screen_fn: Callable[[bytes], Any],
                 runner: Optional[Callable[[Sequence[str]], tuple]] = None) -> None:
        self.host = host
        self.serial = serial
        self.read_screen_fn = read_screen_fn
        self.runner = runner or subprocess_runner
        self.ui_dump_status = ""
        self.last_tap: Dict[str, Any] = {}
        self._last_screen: Optional[PhoneScreen] = None

    def _argv(self, adb_args: Sequence[str]) -> List[str]:
        return ssh_adb_argv(self.host, self.serial, adb_args)

    async def _run(self, adb_args: Sequence[str]) -> tuple:
        import asyncio
        return await asyncio.to_thread(self.runner, self._argv(adb_args))

    async def open_url(self, url: str) -> None:
        from src.integrations.whatsapp_rpa.phone_identity import view_intent_args
        code, _out, err = await self._run(view_intent_args(url))
        if code != 0:
            raise RuntimeError(redact_text(err or "open_url failed", []))

    async def screencap(self) -> bytes:
        code, out, err = await self._run(["exec-out", "screencap", "-p"])
        if code != 0 or not out:
            raise RuntimeError(redact_text(err or "screencap failed", []))
        return out

    async def press_home(self) -> None:
        """先回安卓桌面，再打开 wa.me，避免从设置页或别的 App 里找返回。"""
        await self._run(["shell", "input", "keyevent", "KEYCODE_HOME"])

    async def dismiss_keyboard(self) -> None:
        """ESC。预填会把键盘顶起来，建议条会挡住输入栏。"""
        await self._run(["shell", "input", "keyevent", "111"])

    async def clear_composer(self) -> None:
        """长按输入框。出现「全选」就点掉再删除，否则把光标移到末尾再连续删除。"""
        await self.dismiss_keyboard()
        width, height = await self._device_size()
        x, y = max(1, int(width * 0.42)), max(1, int(height * 0.91))
        await self._run([
            "shell", "input", "swipe", str(x), str(y), str(x), str(y), "800",
        ])
        point = None
        try:
            screen = await self.read_screen(await self.screencap())
            point = select_all_point(getattr(screen, "ocr_lines", None) or [])
        except Exception:
            point = None
        if point is not None:
            await self.tap_xy(point[0], point[1])
            await self._run(["shell", "input", "keyevent", "67"])
            return
        codes = " ".join(["67"] * 30)
        await self._run(["shell", f"input keyevent 123 {codes}"])

    async def scroll_older(self) -> None:
        """手指向下滑动，露出更早的消息。一次大约一屏。"""
        width, height = await self._device_size()
        if width <= 0 or height <= 0:
            return
        x = max(1, int(width * 0.50))
        y_from = max(1, int(height * 0.32))
        y_to = max(1, int(height * 0.78))
        await self._run(["shell", "input", "swipe", str(x), str(y_from), str(x), str(y_to), "280"])

    async def prefill_text(self, text: str) -> None:
        """当前页只打英文。汉字不走剪贴板，改由 wa.me 预填。"""
        body = str(text or "")
        if not body or not body.isascii():
            return
        await self.clear_composer()
        await self._run(["shell", "input", "text", body.replace(" ", "%s")])

    async def unicode_keyboard_available(self) -> bool:
        """只有装了 ADBKeyBoard 或 clipper 才允许备用输入。系统剪贴板不算。"""
        self._unicode_pkg = ""
        for package in ("com.android.adbkeyboard", "ca.zgrs.clipper"):
            code, out, _err = await self._run(["shell", "pm", "path", package])
            text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
            if code == 0 and "package:" in text:
                self._unicode_pkg = package
                return True
        return False

    async def prefill_unicode(self, text: str) -> None:
        """备用。没探测到键盘或 clipper 时什么都不做。"""
        package = str(getattr(self, "_unicode_pkg", "") or "")
        if not package:
            if not await self.unicode_keyboard_available():
                return
            package = str(self._unicode_pkg or "")
        if package == "ca.zgrs.clipper":
            await self._run(["shell", "am", "broadcast", "-a", "clipper.set", "-e", "text", text])
            await self._run(["shell", "input", "keyevent", "279"])
            return
        import base64
        encoded = base64.b64encode(str(text or "").encode("utf-8")).decode("ascii")
        await self._run([
            "shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", encoded,
        ])

    async def _device_size(self) -> tuple:
        code, out, _err = await self._run(["shell", "wm", "size"])
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        if code != 0:
            return (0, 0)
        return _parse_wm_size(text)

    async def tap_send(self) -> None:
        """收起键盘后，用截图里的绿色圆钮换成设备坐标再点。"""
        await self.dismiss_keyboard()
        device_size = await self._device_size()
        try:
            png = await self.screencap()
        except Exception:
            png = b""
        point = locate_send_tap(png, device_size, self._last_screen)
        self.last_tap = point
        x, y = point["tap"]
        code, _out, err = await self._run(["shell", "input", "tap", str(x), str(y)])
        if code != 0:
            raise RuntimeError(redact_text(err or "tap failed", []))

    async def tap_header(self) -> None:
        code, out, err = await self._run(["shell", "wm", "size"])
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        width, height = _parse_wm_size(text)
        x, y = int(width * 0.32), int(height * 0.09)
        code, _out, err = await self._run(["shell", "input", "tap", str(x), str(y)])
        if code != 0:
            raise RuntimeError(redact_text(err or "header tap failed", []))

    async def tap_xy(self, x: int, y: int) -> None:
        await self._run(["shell", "input", "tap", str(int(x)), str(int(y))])

    async def press_back(self) -> None:
        await self._run(["shell", "input", "keyevent", "KEYCODE_BACK"])

    async def poll_notifications(self) -> List[Dict[str, str]]:
        code, out, _err = await self._run(["shell", "dumpsys", "notification", "--noredact"])
        if code != 0:
            return []
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        return parse_wa_notifications(text)

    async def dump_ui_xml(self) -> bytes:
        """这批手机 dump 会被杀掉。失败只记一次，之后直接走 OCR。"""
        if self.ui_dump_status and self.ui_dump_status != "ok":
            return b""
        await self._run(["shell", "rm", "-f", _UI_XML_PATH])
        code, out, err = await self._run(
            ["shell", "uiautomator", "dump", _UI_XML_PATH],
        )
        blob = out if isinstance(out, (bytes, bytearray)) else str(out or "").encode("utf-8")
        err_text = err if isinstance(err, str) else ""
        text = blob.decode("utf-8", "replace") + err_text
        if code == 137 or "Killed" in text:
            self.ui_dump_status = "killed"
            return b""
        if code != 0:
            self.ui_dump_status = f"exit:{code}"
            return b""
        code, out, err = await self._run(["shell", "cat", _UI_XML_PATH])
        if code != 0 or not out:
            self.ui_dump_status = "empty"
            return b""
        if isinstance(out, str):
            out = out.encode("utf-8")
        if screen_from_ui_xml(out) is None:
            self.ui_dump_status = "no-entry"
            return b""
        self.ui_dump_status = "ok"
        return bytes(out)

    async def scroll_to_latest(self) -> None:
        """先上滑到最新消息，再点右下角的「↓」。读屏前调用，避免停在旧记录。"""
        width, height = await self._device_size()
        if width <= 0 or height <= 0:
            return
        x = max(1, int(width * 0.50))
        y_from = max(1, int(height * 0.70))
        y_to = max(1, int(height * 0.38))
        await self._run(["shell", "input", "swipe", str(x), str(y_from), str(x), str(y_to), "180"])
        await self._run([
            "shell", "input", "tap",
            str(max(1, int(width * 0.91))), str(max(1, int(height * 0.78))),
        ])

    async def read_screen(self, png: bytes) -> PhoneScreen:
        parsed = screen_from_ui_xml(await self.dump_ui_xml())
        if parsed is not None and _dump_has_chat_header(parsed):
            parsed.ui_dump = self.ui_dump_status
            shot_w, shot_h = png_size(png)
            if shot_w and shot_h:
                parsed.shot_size = [int(shot_w), int(shot_h)]
            self._last_screen = parsed
            return parsed
        screen = self.read_screen_fn(png)
        if hasattr(screen, "__await__"):
            screen = await screen
        if not isinstance(screen, PhoneScreen):
            screen = PhoneScreen()
        if not screen.source:
            screen.source = "ocr"
        shot_w, shot_h = png_size(png)
        if shot_w and shot_h and not screen.shot_size:
            screen.shot_size = [int(shot_w), int(shot_h)]
        screen.ui_dump = self.ui_dump_status
        self._last_screen = screen
        return screen


def _parse_wm_size(text: str) -> tuple:
    import re
    match = re.search(r"(\d+)x(\d+)", text or "")
    if not match:
        return (720, 1600)
    return (int(match.group(1)), int(match.group(2)))


async def _seed_already_visible(loop: Any, peer: str, seed: str) -> bool:
    """这句已经在气泡里、输入框也空了。已经在聊天页就不再打开链接。"""
    from src.integrations.whatsapp_rpa.phone_send import send_confirmed
    opened = await enter_chat(loop.device, normalize_e164(peer))
    screen = opened.get("screen") or PhoneScreen()
    if not opened.get("ok"):
        return False
    return send_confirmed(screen, seed)


async def open_peer_chat(device: Any, phone: str) -> str:
    """用 wa.me/{号码} 打开对端，保证不是点到列表第一行。"""
    url = wa_me_url(phone)
    if not url:
        raise ValueError("bad phone")
    await device.open_url(url)
    return url


async def preview_prefill(device: Any, phone: str, text: str, evidence: EvidenceLog) -> Dict[str, Any]:
    """打开预填链接，等到聊天页再核对中文，不点发送。"""
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat

    url = wa_me_url(phone, text)
    await device.open_url(url)
    dismiss = getattr(device, "dismiss_keyboard", None)
    if dismiss is not None:
        await dismiss()
    ready = await wait_for_chat(device, phone, expected=text)
    compared = compare_prefill(text, str(ready.get("composer") or ""))
    confirmed = bool(ready.get("profile_confirmed"))
    if ready.get("ready") and compared["match"]:
        return {
            "status": "dry_run", "reason": "", "tapped": False, "url": url,
            "polls": ready.get("polls"), "profile_confirmed": confirmed, **compared,
        }
    reason = str(ready.get("reason") or "chat_not_ready")
    status = "throttled" if reason == "wa_rate_limit" else "failed"
    return {
        "status": status, "reason": reason, "tapped": False, "url": url,
        "polls": ready.get("polls"), "profile_confirmed": confirmed, **compared,
    }


async def snapshot_incoming(device: Any, phone: str) -> List[str]:
    """记下已经在左侧的旧气泡。已经在聊天页就不再打开链接。"""
    opened = await enter_chat(device, normalize_e164(phone))
    if not opened.get("ok"):
        return []
    screen = opened.get("screen") or PhoneScreen()
    return new_left_bubbles(getattr(screen, "incoming", None) or [], [])


async def read_peer_text(device: Any, peer_phone: str, evidence: EvidenceLog,
                         baseline: Sequence[str] = ()) -> Dict[str, Any]:
    """先看通知。没有就打开 wa.me，只认发送之后新出现的左侧气泡。"""
    notes = []
    poll = getattr(device, "poll_notifications", None)
    if poll is not None:
        notes = await poll() or []
    wanted = normalize_e164(peer_phone)
    noted = ""
    for note in notes:
        candidate = normalize_e164(str(note.get("phone") or "")) or normalize_e164(str(note.get("sender") or ""))
        if candidate == wanted:
            noted = str(note.get("text") or "").strip()
            break
    from src.integrations.whatsapp_rpa.phone_send import composer_body
    import asyncio
    import time

    screen = await _read(device)
    if not in_chat(screen, wanted).get("ok"):
        opened = await enter_chat(device, wanted)
        screen = opened.get("screen") or screen
        if not opened.get("ok") and not (noted and len(composer_body(noted)) >= 2):
            return {
                "ok": False, "reason": "not_in_chat", "text": "",
                "header": str(getattr(screen, "header", "") or ""),
            }
    clock = getattr(device, "clock", None) or time.monotonic
    sleep = getattr(device, "sleep_fn", None) or asyncio.sleep
    deadline = float(clock()) + 20.0
    fresh = new_left_bubbles(peer_after_last_own(screen), baseline)
    unchanged = 0
    previous = None
    while (
        not fresh
        and not noted
        and float(clock()) < deadline
        and unchanged < 4
    ):
        signature = tuple(fresh)
        if signature == previous:
            unchanged += 1
        else:
            unchanged = 0
            previous = signature
        if unchanged >= 4:
            break
        await sleep(1.5)
        screen = await _read(device)
        fresh = new_left_bubbles(peer_after_last_own(screen), baseline)
    if fresh:
        text = "\n".join(fresh)
        source = "screenshot"
    elif noted and len(composer_body(noted)) >= 2:
        text = noted
        source = "notification"
    else:
        text = ""
        source = ""
    header = str(getattr(screen, "header", "") or "")
    composer = str(getattr(screen, "composer", "") or "") if screen is not None else ""
    if text and not _heard_reliable(text):
        screen = await _read(device)
        retried = "\n".join(new_left_bubbles(peer_after_last_own(screen), baseline))
        if _heard_reliable(retried):
            text = retried
            source = "screenshot"
        else:
            return {
                "ok": False, "reason": "low_confidence", "text": "",
                "header": str(getattr(screen, "header", "") or header),
                "composer": str(getattr(screen, "composer", "") or composer),
            }
    if not text:
        return {
            "ok": False, "reason": "empty_text", "text": "",
            "header": header, "composer": composer,
        }
    return {
        "ok": True, "text": text, "header": header,
        "source": source, "composer": composer,
    }


def _heard_reliable(text: str) -> bool:
    """乱码或几乎没有汉字的识别结果不能直接拿去回复。"""
    raw = str(text or "").strip()
    cjk = re.findall(r"[\u4e00-\u9fff]", raw)
    if len(cjk) < 2:
        return False
    if re.search(r"[A-Za-zāáàēéěīíìōóòūúùü]", raw) and len(cjk) <= 4:
        return False
    if re.match(r"^[（(【\[\]）)]", raw) and len(cjk) < 6:
        return False
    return True


def cold_texts(texts: Sequence[str], cap: int) -> List[str]:
    out = [str(item).strip() for item in texts if str(item).strip()]
    if not out:
        out = ["验收冷启动一"]
    while len(out) <= int(cap):
        out.append(f"验收冷启动{len(out) + 1}")
    return out


def report_ok(steps: Sequence[Dict[str, Any]]) -> bool:
    if not steps:
        return False
    bad = {"failed", "header_mismatch", "empty_text"}
    for step in steps:
        status = str(step.get("status") or "")
        if step.get("ok") is False or status in bad:
            return False
    return True


async def run_dry(loop: Any, device: Any, phone: str, text: str, evidence: EvidenceLog) -> Dict[str, Any]:
    wrapped = RecordingDevice(device, evidence, "dry")
    result = await preview_prefill(wrapped, phone, text, evidence)
    result["command"] = "dry-run"
    result["ok"] = result.get("status") == "dry_run" and result.get("tapped") is False and result.get("match") is True
    return evidence.finish(result)


def variant_phone(base: str, index: int) -> str:
    """同一号段上的不同号码。用来验收条数上限，而不是「一个号码只能发一次」。"""
    e164 = normalize_e164(base)
    digits = e164[1:]
    suffix = f"{index % 100:02d}"
    return normalize_e164("+" + digits[:-2] + suffix)


async def run_first_touch_cap(loop: Any, phone: str, texts: Sequence[str], cap: int,
                              evidence: EvidenceLog) -> Dict[str, Any]:
    loop.cfg["hourly_cap"] = int(cap)
    loop.cfg["daily_cap"] = max(int(cap), int(loop.cfg.get("daily_cap") or cap))
    loop.cfg["first_touch_hourly_cap"] = int(cap)
    loop.cfg["first_touch_daily_cap"] = max(int(cap), int(loop.cfg.get("daily_cap") or cap))
    sent = []
    for index, text in enumerate(cold_texts(texts, cap)):
        target = variant_phone(phone, index)
        item = await loop.send_prepared(
            phone=target, text=text, source="first_touch",
            client_msg_id=loop.scoped_msg_id("first_touch", target, text, f"cap-{index}"),
        )
        item["phone"] = target
        sent.append(item)
        if item.get("status") == "capped":
            break
    ok = (
        len(sent) == int(cap) + 1
        and all(row.get("status") == "sent" for row in sent[:-1])
        and sent[-1].get("status") == "capped"
        and sent[-1].get("tapped") is not True
    )
    return evidence.finish({
        "command": "first-touch-cap", "ok": ok, "cap": int(cap), "results": sent,
    })


async def run_stop_test(loop: Any, phone: str, text: str, evidence: EvidenceLog) -> Dict[str, Any]:
    await open_peer_chat(loop.device, phone)
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat
    ready = await wait_for_chat(loop.device, phone, expected="")
    if not ready.get("ready"):
        return evidence.finish({
            "command": "stop-test", "ok": False, "status": ready.get("reason") or "header_mismatch",
            "header": ready.get("header") or "",
        })
    inbound = await loop.handle_inbound(phone=phone, text=text, display_name=phone)
    follow = await loop.send_prepared(
        phone=phone, text="还在吗", source="reply",
        client_msg_id=loop.scoped_msg_id("reply", normalize_e164(phone), "还在吗", "stop-follow"),
    )
    ok = bool(inbound.get("stopped")) and follow.get("status") == "stopped" and follow.get("tapped") is not True
    return evidence.finish({
        "command": "stop-test", "ok": ok, "inbound": inbound, "follow": follow,
    })


async def run_convert_pending(loop: Any, phone: str, text: str, evidence: EvidenceLog,
                              confirm: bool = False) -> Dict[str, Any]:
    loop.cfg["reply_mode"] = "off"
    await open_peer_chat(loop.device, phone)
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat
    ready = await wait_for_chat(loop.device, phone, expected="")
    if not ready.get("ready"):
        return evidence.finish({
            "command": "convert-pending", "ok": False,
            "status": ready.get("reason") or "header_mismatch",
            "header": ready.get("header") or "",
        })
    inbound = await loop.handle_inbound(phone=phone, text=text, display_name=phone)
    deal = None
    if confirm:
        deal = loop.mark_deal(phone, source="manual", note="验收确认", by="acceptance")
    deals = 0
    cid = inbound.get("conversation_id") or ""
    if cid and hasattr(loop.inbox, "count_active_deals"):
        deals = int(loop.inbox.count_active_deals(cid))
    pending = (inbound.get("conversion") or {}).get("status") == "pending_confirm"
    ok = pending and deals == (1 if confirm else 0)
    return evidence.finish({
        "command": "convert-pending", "ok": ok, "inbound": inbound,
        "confirmed": deal, "active_deals": deals,
    })


async def run_rate_limit_check(device: Any, evidence: EvidenceLog) -> Dict[str, Any]:
    wrapped = RecordingDevice(device, evidence, "rate")
    png = await wrapped.screencap()
    screen = await wrapped.read_screen(png)
    limited = screen_is_rate_limited(screen)
    return evidence.finish({
        "command": "rate-limit-check",
        "ok": True,
        "status": "throttled" if limited else "clear",
        "tapped": False,
        "header": screen.header,
        "composer": screen.composer,
    })


def _echoes_peer_intro(ledger: Any, phone: str, draft: str) -> bool:
    """草稿里的「叫我某」和对方刚说的是同一个名字。这是在复述对方，要重写。"""
    import re
    intro = re.compile(r"叫我([\u4e00-\u9fffA-Za-z]{1,8})")
    bodies = ledger.list_inbound_bodies(phone) if hasattr(ledger, "list_inbound_bodies") else []
    names = []
    for body in bodies:
        names.extend(intro.findall(str(body or "")))
    draft_text = str(draft or "")
    return any(name and f"叫我{name}" in draft_text for name in names)


def _bubble_matches_unsent(loop: Any, phone: str, text: str) -> bool:
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key
    from src.integrations.whatsapp_rpa.phone_send import texts_close

    rows = loop.ledger.unsent_rows(phone) if hasattr(loop.ledger, "unsent_rows") else []
    keys = {
        sent_content_key(source, phone, text)
        for source in ("reply", "acceptance")
    }
    for row in rows:
        if str(row.get("content_key") or "") in keys:
            return True
        body = str(row.get("body") or "")
        if body and texts_close(body, text):
            return True
    return False


def absorb_visible_texts(ledger: Any, phone: str, texts: Sequence[str]) -> None:
    """屏幕或对方屏幕上能对上的句子，台账改成 sent，不再发第二次。"""
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key
    from src.integrations.whatsapp_rpa.phone_send import texts_close

    seen = [str(item or "").strip() for item in texts if str(item or "").strip()]
    targets = list(seen)
    for _label, text in _HISTORY:
        if any(texts_close(item, text) for item in seen):
            targets.append(text)
    for text in targets:
        if len(text) < 2:
            continue
        for source in ("reply", "acceptance"):
            ledger.mark_content_sent(sent_content_key(source, phone, text))
        for row in ledger.unsent_rows(phone):
            body = str(row.get("body") or "")
            key = str(row.get("content_key") or "")
            if (body and texts_close(body, text)) or (
                key and key == sent_content_key("reply", phone, text)
            ):
                ledger.mark_content_sent(key or sent_content_key("reply", phone, text))


async def reconcile_visible(loop: Any, peer: str) -> None:
    """屏幕上已经有的己方气泡，把台账里没记成 sent 的改过来，不再补发。"""
    try:
        await open_peer_chat(loop.device, peer)
        screen = await loop.device.read_screen(await loop.device.screencap())
    except Exception:
        return
    phone = normalize_e164(peer)
    bubbles = list(getattr(screen, "outgoing", None) or [])
    bubbles.extend(getattr(screen, "incoming", None) or [])
    absorb_visible_texts(loop.ledger, phone, bubbles)


async def reconcile_pair(loop_a: Any, loop_b: Any, phone_a: str, phone_b: str) -> None:
    """两端屏幕都看。07:35 在对方气泡里也能把发送方台账改成 sent。"""
    found: List[str] = []
    for loop, peer in ((loop_a, phone_b), (loop_b, phone_a)):
        try:
            opened = await enter_chat(loop.device, normalize_e164(peer))
        except Exception:
            continue
        if not opened.get("ok"):
            continue
        screen = opened.get("screen") or PhoneScreen()
        found.extend(getattr(screen, "outgoing", None) or [])
        found.extend(getattr(screen, "incoming", None) or [])
    absorb_visible_texts(loop_a.ledger, normalize_e164(phone_b), found)
    absorb_visible_texts(loop_b.ledger, normalize_e164(phone_a), found)


async def settle_stale_draft(loop: Any, peer: str) -> Dict[str, Any]:
    """没有新入站时，输入框里的旧草稿：和台账待发一致就发出，否则清空。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_body, send_confirmed

    phone = normalize_e164(peer)
    try:
        screen = await loop.device.read_screen(await loop.device.screencap())
    except Exception:
        return {"status": "empty", "tapped": False}
    draft = str(getattr(screen, "composer", "") or "")
    if not composer_body(draft):
        return {"status": "empty", "tapped": False}
    from src.integrations.whatsapp_rpa.phone_loop import claims_peer_identity
    if claims_peer_identity(draft, list(getattr(loop, "peer_names", None) or [])):
        clear = getattr(loop.device, "clear_composer", None)
        if clear is not None:
            await clear()
        if hasattr(loop.ledger, "skip_unsent_like"):
            loop.ledger.skip_unsent_like(phone, draft)
        return {"status": "cleared", "reason": "peer_name", "tapped": False, "text": draft}
    if send_confirmed(screen, draft):
        reconcile = getattr(loop.ledger, "mark_content_sent", None)
        if reconcile is not None:
            from src.integrations.whatsapp_rpa.phone_loop import sent_content_key
            loop.ledger.mark_content_sent(sent_content_key("reply", phone, draft))
        clear = getattr(loop.device, "clear_composer", None)
        if clear is not None:
            await clear()
        return {"status": "sent", "reason": "already_visible", "tapped": False, "text": draft}
    if _echoes_peer_intro(loop.ledger, phone, draft):
        clear = getattr(loop.device, "clear_composer", None)
        if clear is not None:
            await clear()
        if hasattr(loop.ledger, "skip_unsent_like"):
            loop.ledger.skip_unsent_like(phone, draft)
        inbound = ""
        if hasattr(loop.ledger, "latest_unreplied_body"):
            inbound = loop.ledger.latest_unreplied_body(phone)
        return {
            "status": "cleared", "reason": "regenerate", "tapped": False,
            "text": draft, "inbound": inbound,
        }
    if _bubble_matches_unsent(loop, phone, draft):
        sent = await loop.send_prepared(
            phone=phone, text=composer_body(draft) or draft, source="reply",
            client_msg_id=loop.scoped_msg_id("reply", phone, draft, "stale-draft"),
            finish_pending=True,
        )
        return sent
    clear = getattr(loop.device, "clear_composer", None)
    if clear is not None:
        await clear()
    return {"status": "cleared", "tapped": False, "text": draft}


_HISTORY = (
    ("开场", "你好，早上好，在忙吗"),
    ("07:07", "早上好呀，不忙，刚起来没多久。你这问候可够早的。"),
    ("07:08", "早嘛，周日这个点醒的，是自然醒还是被什么事叫起来的呀。"),
    ("07:28", "早嘛，周日能睡到这会儿才起，挺舒服的吧。"),
    ("07:35", "对了，还没问你，平时怎么称呼你比较方便呀？"),
    ("07:54", "早呀，刚醒，还没吃上早饭呢。"),
    ("04开场", "早上好呀，今天天气怎么样"),
)


def ledger_history(*ledgers: Any) -> List[Dict[str, Any]]:
    """按开场和几个时间点列出台账状态。已发出的优先于失败记录。"""
    from src.integrations.whatsapp_rpa.phone_loop import sent_content_key
    from src.integrations.whatsapp_rpa.phone_send import texts_close

    rows: List[Dict[str, Any]] = []
    for ledger in ledgers:
        if hasattr(ledger, "list_sends"):
            rows.extend(ledger.list_sends())
    report: List[Dict[str, Any]] = []
    for label, text in _HISTORY:
        matches = []
        for row in rows:
            body = str(row.get("body") or "")
            phone = str(row.get("phone") or "")
            key = str(row.get("content_key") or "")
            keyed = any(
                key and key == sent_content_key(source, phone, text)
                for source in ("reply", "acceptance")
            )
            if (body and texts_close(body, text)) or keyed:
                matches.append(row)
        status = "absent"
        phone = ""
        if any(str(row.get("status") or "") == "sent" for row in matches):
            status = "sent"
            phone = next(
                str(row.get("phone") or "")
                for row in matches if str(row.get("status") or "") == "sent"
            )
        elif matches:
            status = str(matches[-1].get("status") or "absent")
            phone = str(matches[-1].get("phone") or "")
        report.append({"label": label, "text": text, "status": status, "phone": phone})
    return report


class StepTimer:
    """每一步的毫秒。报告里的 timings 用这个，不再只留一个总数。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self._last = time.monotonic()

    def mark(self, step: str) -> None:
        now = time.monotonic()
        self.rows.append({"step": step, "ms": int((now - self._last) * 1000)})
        self._last = now


def select_all_point(lines: Sequence[Dict[str, Any]]):
    """长按后弹出的「全选」。"""
    for line in lines or []:
        text = str((line or {}).get("text") or "").strip()
        box = (line or {}).get("box") or []
        if text not in {"全选", "Select all", "Select All"} or len(box) < 4:
            continue
        return (
            int((float(box[0]) + float(box[2])) / 2),
            int((float(box[1]) + float(box[3])) / 2),
        )
    return None


def _screen_lines(screen: Any) -> List[str]:
    return [
        str((line or {}).get("text") or "").strip()
        for line in (getattr(screen, "ocr_lines", None) or [])
    ]


def _dump_has_chat_header(screen: Any) -> bool:
    """界面树没有 + 号顶栏时不能拿来判定聊天页，改看这次截图的 OCR。"""
    from src.integrations.whatsapp_rpa.phone_identity import header_number_candidates
    return bool(header_number_candidates(str(getattr(screen, "header", "") or "")))


def _line_box(line: Dict[str, Any]):
    box = (line or {}).get("box") or []
    if len(box) < 4:
        return None
    return [int(float(value)) for value in box[:4]]


def in_chat(screen: Any, phone: str = "") -> Dict[str, Any]:
    """normalize-ui 和 pair-chat 共用。占位「发消息」就是输入栏。

    顶栏 + 号直接在 OCR 框里找，不依赖已经被状态栏过滤掉的 header 字符串。
    """
    from src.integrations.whatsapp_rpa.phone_identity import header_matches, header_number_candidates
    from src.integrations.whatsapp_rpa.phone_send import composer_body, composer_is_placeholder

    header_box = None
    composer_box = None
    header_plus = False
    header_matches_peer = False
    sources = []
    header = str(getattr(screen, "header", "") or "").strip()
    if header:
        sources.append((header, None))
    for line in getattr(screen, "ocr_lines", None) or []:
        text = str((line or {}).get("text") or "").strip()
        box = _line_box(line)
        if text:
            sources.append((text, box))
    for text, box in sources:
        if not header_number_candidates(text):
            continue
        header_plus = True
        matched = header_matches(text, phone=phone)
        if matched:
            header_matches_peer = True
        if box is not None and (header_box is None or matched):
            header_box = box
            if matched:
                break
    if not header_matches_peer and header_matches(header, phone=phone):
        header_matches_peer = True
        header_plus = header_plus or bool(header_number_candidates(header))
    composer = str(getattr(screen, "composer", "") or "")
    placeholder = composer_is_placeholder(composer) or any(
        "发消息" in text or "type a message" in text.casefold()
        for text, _box in sources
    )
    for text, box in sources:
        if box is None:
            continue
        if "发消息" in text or (composer and text == composer.split("\n")[0]):
            composer_box = box
            if "发消息" in text:
                break
    size = getattr(screen, "shot_size", None) or [0, 0]
    width = float(size[0] or 0)
    height = float(size[1] or 0)
    lines = list(getattr(screen, "ocr_lines", None) or [])
    send_button = bool(width) and _send_button_present(lines, width, height or 1)
    input_bar = bool(placeholder or send_button)
    if not lines and (placeholder or composer_body(composer) or composer == ""):
        input_bar = True
    menu = any(text in _MENU_LABELS for text, _box in sources)
    ok = bool(header_matches_peer and input_bar and not menu)
    return {
        "ok": ok,
        "header_plus": header_plus,
        "header_matches_peer": header_matches_peer,
        "input_bar": input_bar,
        "placeholder": bool(placeholder),
        "send_button": send_button,
        "menu": menu,
        "boxes": {"header": header_box, "composer": composer_box},
    }


def has_input_bar(screen: Any) -> bool:
    """占位「发消息」本身就是输入栏。"""
    return bool(in_chat(screen, "").get("input_bar"))


def chat_page_ready(screen: Any, phone: str = "") -> bool:
    """和 normalize-ui、pair-chat 用的是同一个 in_chat。"""
    return bool(in_chat(screen, phone).get("ok"))


def _keyboard_open(screen: Any) -> bool:
    """键盘弹出时，y 大约超过 1200 的短词是键帽，不是输入框。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_is_placeholder

    items = []
    for line in getattr(screen, "ocr_lines", None) or []:
        text = str((line or {}).get("text") or "").strip()
        box = (line or {}).get("box") or []
        if text and len(box) >= 4:
            items.append((text, box))
    size = getattr(screen, "shot_size", None) or [0, 0]
    width = float(size[0] or 0)
    height = float(size[1] or 0)
    if width <= 0 or height <= 0 or not items:
        return False
    floor = 1200.0 if height <= 1800 else height * 0.72
    for text, box in items:
        if composer_is_placeholder(text):
            continue
        cx = (float(box[0]) + float(box[2])) / 2.0
        if cx >= width * 0.82:
            continue
        if float(box[1]) >= floor and _box_width(box) < 160:
            return True
    return _keyboard_zone_top(items, width, height) is not None


def _left_chat(screen: Any, phone: str) -> bool:
    """按返回之后离开了会话，例如掉进「语音输入」设置。"""
    parts = [getattr(screen, "header", ""), getattr(screen, "composer", "")]
    parts.extend(_screen_lines(screen))
    blob = "\n".join(str(part or "") for part in parts)
    if "语音输入" in blob:
        return True
    return not in_chat(screen, phone).get("ok")


def menu_is_open(screen: Any) -> bool:
    """聊天页右上角菜单。这时不能把关闭引用的点击打到菜单上。"""
    return any(text in _MENU_LABELS for text in _screen_lines(screen))


def recognized_popup(screen: Any) -> bool:
    """已经认出的弹层。只允许按一次返回把它关掉，不能用来退出设置或桌面。"""
    texts = _screen_lines(screen)
    if any(text in _MENU_LABELS for text in texts):
        return True
    blob = " ".join([str(getattr(screen, "header", "") or ""), str(getattr(screen, "composer", "") or ""), *texts])
    return any(marker in blob for marker in ("发送给", "正在搜索", "选择联系人"))


def input_bar_top(screen: Any):
    """输入栏文字的上沿。多行被撑到屏幕上半时，取草稿第一行，不取上面的引用。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_is_placeholder

    composer = str(getattr(screen, "composer", "") or "").strip()
    best = None
    for line in getattr(screen, "ocr_lines", None) or []:
        text = str((line or {}).get("text") or "").strip()
        box = (line or {}).get("box") or []
        if len(box) < 4 or not text:
            continue
        first = composer.split("\n")[0].strip()
        same = (
            text == composer
            or "发消息" in text
            or composer_is_placeholder(text)
            or (len(text) >= 4 and first and (
                text == first or first.startswith(text) or text.startswith(first[:8])
            ))
        )
        if same and len(text) >= 1:
            top = float(box[1])
            best = top if best is None else min(best, top)
    return best


def screen_matches_target(screen: Any, target: str) -> bool:
    """整屏文字拼起来做规范化比对。拆成两行的气泡也能对上。"""
    from src.integrations.whatsapp_rpa.phone_send import is_keycap, is_screen_chrome, texts_close

    pieces: List[str] = []
    for line in getattr(screen, "ocr_lines", None) or []:
        text = str((line or {}).get("text") or "").strip()
        if not text or is_keycap(text) or is_screen_chrome(text) or text in _MENU_LABELS:
            continue
        pieces.append(text)
    pieces.extend(str(item or "") for item in (getattr(screen, "outgoing", None) or []))
    pieces.extend(str(item or "") for item in (getattr(screen, "incoming", None) or []))
    pieces = [item for item in pieces if item.strip()]
    if any(texts_close(item, target) for item in pieces):
        return True
    joined = "".join(pieces)
    if texts_close(joined, target):
        return True
    for start in range(len(pieces)):
        window = "".join(pieces[start:start + 4])
        if texts_close(window, target):
            return True
    return False


def failed_history_texts(*ledgers: Any) -> List[str]:
    """台账里 failed / bubble_missing 的原文，加上历史表里仍是这两种状态的句子。"""
    texts: List[str] = []
    seen = set()

    def add(text: str) -> None:
        body = str(text or "").strip()
        if len(body) < 2 or body in seen:
            return
        seen.add(body)
        texts.append(body)

    from src.integrations.whatsapp_rpa.phone_send import texts_close

    sent: List[str] = []
    pending: List[str] = []
    for ledger in ledgers:
        for item in ledger_history(ledger):
            text = str(item.get("text") or "")
            status = str(item.get("status") or "")
            if status == "sent":
                sent.append(text)
            elif status in {"failed", "bubble_missing"}:
                pending.append(text)

    def already_sent(text: str) -> bool:
        return any(texts_close(text, done) for done in sent)

    for text in pending:
        if already_sent(text):
            continue
        add(text)
    return texts


def quote_close_point(lines: Sequence[Dict[str, Any]], width: float, height: float,
                      input_top: Optional[float] = None):
    """引用块右上角的 X。键盘把输入栏顶到中部时，X 跟着输入栏，不在屏幕最底。"""
    if width <= 0 or height <= 0:
        return None
    marks = {"X", "×", "x", "✕", "✖"}
    found = None
    for line in lines or []:
        text = str((line or {}).get("text") or "").strip()
        box = (line or {}).get("box") or []
        if text not in marks or len(box) < 4:
            continue
        cy = (float(box[1]) + float(box[3])) / 2.0
        cx = (float(box[0]) + float(box[2])) / 2.0
        if cy < height * 0.18:
            continue
        if cx < width * 0.55 or float(box[2]) - float(box[0]) > width * 0.08:
            continue
        if input_top is not None and float(input_top) < height * 0.75:
            if not (float(input_top) - 220 <= cy <= float(input_top) + 12):
                continue
        elif cy < height * 0.20 or cy > height * 0.98:
            continue
        found = (int(cx), int(cy))
    if found is not None:
        return found
    if input_top is None or float(input_top) >= height * 0.75:
        return None
    tops: List[float] = []
    for line in lines or []:
        text = str((line or {}).get("text") or "").strip()
        box = (line or {}).get("box") or []
        if not text or text in marks or len(box) < 4 or "发消息" in text:
            continue
        if len(text) > 4 or float(box[1]) < height * 0.18:
            continue
        cy = _box_cy(box)
        if float(input_top) - 220 <= cy <= float(input_top) - 8:
            tops.append(float(box[1]))
    if not tops:
        return None
    # 引用块右上角。OCR 往往只读到左侧的「你」，X 在同一条的右端。
    return int(width * 0.92), int(min(tops) + 12)


async def _press_home(device: Any) -> None:
    home = getattr(device, "press_home", None)
    if home is not None:
        await home()
        return
    run = getattr(device, "_run", None)
    if run is not None:
        await run(["shell", "input", "keyevent", "KEYCODE_HOME"])


async def _read(device: Any) -> Any:
    try:
        return await device.read_screen(await device.screencap())
    except Exception:
        return PhoneScreen()


async def enter_chat(device: Any, phone: str) -> Dict[str, Any]:
    """已经在对方聊天页就停在原地。否则回桌面再用 wa.me 打开。

    返回键最多一次，而且只关已经认出的弹层。不在聊天页才再打开一次。
    """
    screen = await _read(device)
    verdict = in_chat(screen, phone)
    if verdict.get("ok"):
        return {"ok": True, "screen": screen, "reason": "", "in_chat": verdict}
    used_back = False
    for _attempt in range(2):
        try:
            await _press_home(device)
            await open_peer_chat(device, phone)
        except Exception:
            return {"ok": False, "screen": screen, "reason": "not_in_chat"}
        screen = await _read(device)
        verdict = in_chat(screen, phone)
        if verdict.get("ok"):
            return {"ok": True, "screen": screen, "reason": "", "in_chat": verdict}
        if not used_back and recognized_popup(screen):
            used_back = True
            back = getattr(device, "press_back", None)
            if back is not None:
                try:
                    await back()
                except Exception:
                    pass
                screen = await _read(device)
                verdict = in_chat(screen, phone)
                if verdict.get("ok"):
                    return {"ok": True, "screen": screen, "reason": "", "in_chat": verdict}
    return {
        "ok": False, "screen": screen, "reason": "not_in_chat",
        "in_chat": in_chat(screen, phone),
    }


async def scan_history_pages(device: Any, targets: Sequence[str], *, limit: int = 3) -> List[str]:
    """从当前屏往更早的消息找，最多看 limit 屏。找到的句子返回原文。"""
    pending = [str(item) for item in targets if str(item or "").strip()]
    found: List[str] = []
    pages = max(1, int(limit))
    for page in range(pages):
        try:
            screen = await device.read_screen(await device.screencap())
        except Exception:
            break
        hit = [text for text in pending if screen_matches_target(screen, text)]
        found.extend(hit)
        pending = [text for text in pending if text not in hit]
        if not pending or page + 1 >= pages:
            break
        older = getattr(device, "scroll_older", None)
        if older is None:
            break
        try:
            await older()
        except Exception:
            break
    return found


async def prepare_phone(loop: Any, peer: str, timer: Optional[StepTimer] = None) -> Dict[str, Any]:
    """回到聊天页，关掉引用条，草稿对不上才清。不在聊天页就停，不去点。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_body, composer_is_placeholder

    phone = normalize_e164(peer)
    opened = await enter_chat(loop.device, phone)
    screen = opened.get("screen") or PhoneScreen()
    verdict = in_chat(screen, phone)
    if timer is not None:
        timer.mark("open_chat")
    if not opened.get("ok") or not verdict.get("ok"):
        return {
            "cleared": False, "chat": False, "composer": str(getattr(screen, "composer", "") or ""),
            "reason": "not_in_chat", "quote_closed": False, "in_chat": verdict,
        }
    if _keyboard_open(screen):
        back = getattr(loop.device, "press_back", None)
        if back is not None:
            try:
                await back()
                screen = await loop.device.read_screen(await loop.device.screencap())
            except Exception:
                pass
        if timer is not None:
            timer.mark("dismiss_keyboard")
        if _left_chat(screen, phone):
            try:
                await open_peer_chat(loop.device, phone)
                screen = await loop.device.read_screen(await loop.device.screencap())
            except Exception:
                pass
        verdict = in_chat(screen, phone)
        if not verdict.get("ok"):
            return {
                "cleared": False, "chat": False,
                "composer": str(getattr(screen, "composer", "") or ""),
                "reason": "not_in_chat", "quote_closed": False, "in_chat": verdict,
            }
    if menu_is_open(screen):
        dismiss = getattr(loop.device, "dismiss_keyboard", None)
        if dismiss is not None:
            try:
                await dismiss()
                screen = await loop.device.read_screen(await loop.device.screencap())
                verdict = in_chat(screen, phone)
            except Exception:
                pass
    lines = list(getattr(screen, "ocr_lines", None) or [])
    size = getattr(screen, "shot_size", None) or [0, 0]
    width = float(size[0] or 0)
    height = float(size[1] or 0)
    point = None if menu_is_open(screen) else quote_close_point(
        lines, width, height, input_bar_top(screen),
    )
    quote_closed = point is None
    tap = getattr(loop.device, "tap_xy", None)
    if point and tap is not None:
        try:
            await tap(point[0], point[1])
            screen = await loop.device.read_screen(await loop.device.screencap())
        except Exception:
            screen = screen
        if menu_is_open(screen):
            dismiss = getattr(loop.device, "dismiss_keyboard", None)
            if dismiss is not None:
                try:
                    await dismiss()
                    screen = await loop.device.read_screen(await loop.device.screencap())
                except Exception:
                    pass
        if not in_chat(screen, phone).get("ok"):
            return {
                "cleared": False, "chat": False,
                "composer": str(getattr(screen, "composer", "") or ""),
                "reason": "not_in_chat", "quote_closed": False,
                "in_chat": in_chat(screen, phone),
            }
        lines = list(getattr(screen, "ocr_lines", None) or [])
        size = getattr(screen, "shot_size", None) or [width, height]
        quote_closed = quote_close_point(
            lines, float(size[0] or 0), float(size[1] or 0), input_bar_top(screen),
        ) is None
    if timer is not None:
        timer.mark("dismiss_quote")
    from src.integrations.whatsapp_rpa.phone_loop import claims_peer_identity
    names = list(getattr(loop, "peer_names", None) or [])
    draft = str(getattr(screen, "composer", "") or "")
    borrowed = claims_peer_identity(draft, names)
    if composer_body(draft) and (borrowed or not _bubble_matches_unsent(loop, phone, draft)):
        clear = getattr(loop.device, "clear_composer", None)
        if clear is not None:
            try:
                await clear()
            except Exception:
                pass
        try:
            screen = await loop.device.read_screen(await loop.device.screencap())
        except Exception:
            pass
    if timer is not None:
        timer.mark("clear_composer")
    composer = str(getattr(screen, "composer", "") or "")
    chat = chat_page_ready(screen, phone)
    cleared = chat and composer_is_placeholder(composer) and quote_closed
    reason = "" if cleared else ("quote_open" if chat and not quote_closed else "")
    return {
        "cleared": cleared, "chat": chat, "composer": composer,
        "reason": reason, "quote_closed": quote_closed,
        "in_chat": in_chat(screen, phone),
    }


async def reconcile_failed_history(loop_a: Any, loop_b: Any, phone_a: str, phone_b: str,
                                   timer: Optional[StepTimer] = None) -> List[str]:
    """启动时把仍是 failed 或 bubble_missing 的句子在屏幕上找回来，找到就改成 sent。"""
    targets = failed_history_texts(loop_a.ledger, loop_b.ledger)
    found: List[str] = []
    if targets:
        for loop, peer in ((loop_a, phone_b), (loop_b, phone_a)):
            try:
                opened = await enter_chat(loop.device, normalize_e164(peer))
            except Exception:
                continue
            if not opened.get("ok"):
                continue
            try:
                found.extend(await scan_history_pages(loop.device, targets, limit=3))
            except Exception:
                continue
            latest = getattr(loop.device, "scroll_to_latest", None)
            if latest is not None:
                try:
                    await latest()
                except Exception:
                    pass
    if found:
        absorb_visible_texts(loop_a.ledger, normalize_e164(phone_b), found)
        absorb_visible_texts(loop_b.ledger, normalize_e164(phone_a), found)
    if timer is not None:
        timer.mark("search_failed")
    return found


async def run_normalize_ui(loop: Any, phone: str, evidence: EvidenceLog) -> Dict[str, Any]:
    """只回到聊天页、清掉无关草稿、截图。不生成回复，也不点发送。"""
    timer = StepTimer()
    prepared = await prepare_phone(loop, phone, timer)
    try:
        png = await loop.device.screencap()
        screen = await loop.device.read_screen(png)
    except Exception:
        png, screen = b"", PhoneScreen()
    if not isinstance(png, (bytes, bytearray)):
        png = b""
    evidence.save("normalize", bytes(png), screen if isinstance(screen, PhoneScreen) else PhoneScreen())
    timer.mark("screenshot")
    composer = str(getattr(screen, "composer", "") or prepared.get("composer") or "")
    chat = bool(prepared.get("chat")) and chat_page_ready(screen, normalize_e164(phone))
    cleared = bool(prepared.get("cleared")) and chat
    reason = str(prepared.get("reason") or "")
    verdict = prepared.get("in_chat") or in_chat(screen, normalize_e164(phone))
    if not chat:
        reason = "not_in_chat"
    return evidence.finish({
        "command": "normalize-ui",
        "ok": cleared,
        "cleared": cleared,
        "chat": chat,
        "in_chat": verdict,
        "reason": reason,
        "header": str(getattr(screen, "header", "") or ""),
        "composer": composer,
        "tapped": False,
        "timings": timer.rows,
        "turns": [],
        "history": ledger_history(loop.ledger),
    })


async def run_pair_chat(loop_a: Any, loop_b: Any, phone_a: str, phone_b: str,
                        turns: int, seed: str, evidence: EvidenceLog,
                        max_seconds: float = 540) -> Dict[str, Any]:
    """A、B 交替。第 1 句由 A 发给 B，之后每人先收对方的信再回复。

    第一句没发出去就停，不再往下跑。到点写出最终报告，不再被外层杀掉。
    """
    import time
    deadline = time.monotonic() + float(max_seconds or 0) if max_seconds else None
    for loop in (loop_a, loop_b):
        device = getattr(loop, "device", None)
        if device is not None and deadline:
            device.deadline_at = deadline

    def expired() -> bool:
        return bool(deadline) and time.monotonic() >= float(deadline)

    seats = ((loop_a, phone_a, phone_b, "A"), (loop_b, phone_b, phone_a, "B"))
    loops = {"A": loop_a, "B": loop_b}
    results = []
    baselines: Dict[str, List[str]] = {"A": [], "B": []}
    startup = StepTimer()
    await prepare_phone(loop_a, phone_b, startup)
    await prepare_phone(loop_b, phone_a, startup)
    await reconcile_failed_history(loop_a, loop_b, phone_a, phone_b, startup)
    await reconcile_pair(loop_a, loop_b, phone_a, phone_b)
    timings: List[Dict[str, Any]] = []

    def publish(partial_ok: bool = False) -> Dict[str, Any]:
        history = ledger_history(loop_a.ledger, loop_b.ledger)
        return {
            "command": "pair-chat", "ok": partial_ok, "turns": results,
            "history": history, "timings": timings,
        }

    def flush() -> None:
        collected = []
        for row in results:
            timing = {}
            send = row.get("send") if isinstance(row.get("send"), dict) else {}
            if isinstance(send.get("timings"), dict):
                timing = dict(send["timings"])
            collected.append({
                "turn": row.get("turn"), "seat": row.get("seat"),
                "status": row.get("status"), **timing,
            })
        timings[:] = list(startup.rows) + collected
        evidence.checkpoint(publish())

    flush()
    stopped = ""
    for index in range(int(turns)):
        if expired():
            stopped = "max_seconds"
            break
        loop, own, peer, label = seats[index % 2]
        other = "B" if label == "A" else "A"
        if index == 0:
            baselines[other] = await snapshot_incoming(loops[other].device, own)
            if await _seed_already_visible(loop, peer, seed):
                from src.integrations.whatsapp_rpa.phone_loop import sent_content_key
                peer_id = normalize_e164(peer)
                loop.ledger.mark_content_sent(
                    sent_content_key("acceptance", peer_id, seed),
                )
                baselines[other] = [
                    line for line in baselines.get(other, [])
                    if not composer_matches(line, seed)
                ]
                results.append({
                    "turn": index, "seat": label, "status": "sent",
                    "reason": "already_visible", "delivered": True, "tapped": False,
                    "phone": peer_id,
                })
                flush()
                continue
            item = await loop.send_prepared(
                phone=peer, text=seed, source="acceptance",
                client_msg_id=loop.scoped_msg_id("acceptance", normalize_e164(peer), seed, "seed"),
                display_name=peer,
            )
            item["turn"] = index
            item["seat"] = label
            results.append(item)
            flush()
            if item.get("status") != "sent":
                break
            continue
        heard = await read_peer_text(
            loop.device, peer, evidence, baseline=baselines.get(label, []),
        )
        if not heard.get("ok"):
            heard["turn"] = index
            heard["seat"] = label
            heard["status"] = heard.get("reason") or "failed"
            heard["ok"] = False
            results.append(heard)
            if heard.get("reason") == "empty_text":
                settled = await settle_stale_draft(loop, peer)
                if settled.get("status") == "sent":
                    results[-1] = {
                        "turn": index, "seat": label, "status": "sent",
                        "reason": settled.get("reason") or "draft_sent",
                        "tapped": bool(settled.get("tapped")),
                        "delivered": True,
                    }
                elif settled.get("reason") == "regenerate" and settled.get("inbound"):
                    item = await loop.handle_inbound(
                        phone=peer, text=str(settled.get("inbound") or ""), display_name=peer,
                    )
                    send = item.get("send") or {}
                    results[-1] = {
                        "turn": index, "seat": label,
                        "heard": settled.get("inbound") or "",
                        "status": send.get("status") or item.get("reply"),
                        "reason": "regenerate",
                        "send": send, "ok": send.get("status") == "sent",
                        "tapped": bool(send.get("tapped")),
                    }
                else:
                    results[-1]["draft"] = {
                        "status": settled.get("status") or "",
                        "reason": settled.get("reason") or "",
                    }
                flush()
                continue
            flush()
            break
        baselines[label] = list(baselines.get(label, [])) + [str(heard.get("text") or "")]
        baselines[other] = await snapshot_incoming(loops[other].device, own)
        item = await loop.handle_inbound(
            phone=peer, text=heard["text"], display_name=peer,
        )
        send = item.get("send") or {}
        results.append({
            "turn": index, "seat": label, "heard": heard["text"],
            "source": heard.get("source"), "status": send.get("status") or item.get("reply"),
            "send": send, "ok": send.get("status") == "sent",
        })
        flush()
    ok = (
        not stopped
        and len(results) == int(turns)
        and all(row.get("status") == "sent" for row in results)
    )
    final = publish()
    final["ok"] = ok
    if stopped:
        final["reason"] = stopped
    return evidence.finish(final)
