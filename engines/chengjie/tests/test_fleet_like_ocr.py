"""Facebook like finds the button on a screenshot. Mock adb, no device.

Text is one signal. A tap needs a high-confidence thumbs-up template or two
agreeing signals (text, template, action bar). The fixed like_button is not a tap.
"""
from __future__ import annotations

import json
import struct

import pytest

from src.fleet.like_locate import (
    TEMPLATE_HIGH, TEMPLATE_MED, empty_phrase, extract_blobs, fuse_signals, like_state_changed,
    label_hits, load_templates, locate_like_row, normalize_ocr_boxes, post_evidence, position_hits, region_hits,
    render_thumb, render_thumb_icon,
    structure_hits, template_hits, templates_available, text_hits, write_default_templates,
)
from src.fleet.phone_flow_rules import validate_flow_payload
from src.fleet.phone_flows import PhoneFlows, bundled_ui_map
from src.fleet.phone_rules import PhoneOpError, sanitize_phone_result
from src.fleet.protocol import STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, TASK_PHONE_LIKE
from tests.test_fleet_phone_flow_robust import _paint, _px
from tests.test_fleet_phone_flows import W, H, FakeAdb, _body, _frame, _ops

_LOGGED = (24, 119, 242)
_WALL = (66, 103, 178)
_BLUE = (24, 119, 242)


def _box(text, x0, y0, x1, y1, score=0.95):
    return {"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "score": score}


def _hit(source, x, y, *, score=1.0, full=True, label=""):
    return {"source": source, "x_pm": x, "y_pm": y, "score": score, "label": label or source,
            "full": full, "x": x, "y": y}


def _blit(raw: bytearray, rgb: bytes, tw: int, th: int, x: int, y: int) -> None:
    for yy in range(th):
        for xx in range(tw):
            px, py = x + xx, y + yy
            if not (0 <= px < W and 0 <= py < H):
                continue
            off = 12 + (py * W + px) * 4
            i = (yy * tw + xx) * 3
            raw[off:off + 3] = rgb[i:i + 3]
            raw[off + 3] = 255


def _square(raw: bytearray, x: int, y: int, side: int, rgb=(240, 240, 240)) -> None:
    for yy in range(side):
        for xx in range(side):
            px, py = x + xx, y + yy
            if not (0 <= px < W and 0 <= py < H):
                continue
            off = 12 + (py * W + px) * 4
            raw[off:off + 3] = bytes(rgb)
            raw[off + 3] = 255


def _logged(raw: bytearray) -> bytearray:
    x, y = _px("feed_tab")
    off = 12 + (y * W + x) * 4
    raw[off:off + 3] = bytes(_LOGGED)
    return raw


def _light():
    pack = {item["name"]: item for item in load_templates()}
    tmpl = pack["like_light_s"]
    return tmpl, render_thumb(tmpl["w"], tmpl["h"], dark=False)


def _with_thumb(x=28, y=250):
    tmpl, rgb = _light()
    raw = _logged(bytearray(_frame()))
    _blit(raw, rgb, tmpl["w"], tmpl["h"], x, y)
    return bytes(raw), tmpl, x, y


class SeqAdb(FakeAdb):
    def __init__(self, frames):
        super().__init__(frame=frames[0])
        self.frames = list(frames)
        self.n = 0

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 4 and args[2:] == ("exec-out", "screencap"):
            self.frame = self.frames[min(self.n, len(self.frames) - 1)]
            self.n += 1
        return FakeAdb.__call__(self, cmd, **kw)


def _taps(actions):
    return [a for a in actions if len(a) > 4 and a[4] == "tap"]


def _swipes(actions):
    return [a for a in actions if len(a) > 4 and a[4] == "swipe"]


def _feed_dirs(actions):
    """``top`` scrolls toward the start of the feed. ``feed`` scrolls down the feed."""
    dirs = []
    for action in _swipes(actions):
        y1, y2 = int(action[6]), int(action[8])
        dirs.append("top" if y2 > y1 else "feed")
    return dirs


def _fill(raw, x, y, w, h, color):
    for yy in range(h):
        for xx in range(w):
            px, py = x + xx, y + yy
            if not (0 <= px < W and 0 <= py < H):
                continue
            off = 12 + (py * W + px) * 4
            raw[off:off + 3] = bytes(color)
            raw[off + 3] = 255


def _light_page():
    raw = bytearray(_frame())
    _fill(raw, 0, 0, W, H, (246, 246, 248))
    return _logged(raw)


def _circle(raw, x, y, d, ink=(55, 58, 64)):
    thick = max(2, d // 8)
    for yy in range(d):
        for xx in range(d):
            dx, dy = xx - (d - 1) / 2.0, yy - (d - 1) / 2.0
            radius = (dx * dx + dy * dy) ** 0.5
            if (d * 0.5 - thick) <= radius <= d * 0.48:
                px, py = x + xx, y + yy
                if 0 <= px < W and 0 <= py < H:
                    off = 12 + (py * W + px) * 4
                    raw[off:off + 3] = bytes(ink)


def _arrow(raw, x, y, w, h, ink=(55, 58, 64)):
    thick = max(2, int(h * 0.12))
    for yy in range(h):
        for xx in range(w):
            shaft = abs(yy - (h - 1) / 2.0) <= thick and xx < int(w * 0.62)
            head = xx >= int(w * 0.40) and abs(yy - (h - 1) / 2.0) <= (w - 1 - xx) * 0.95
            if shaft or head:
                px, py = x + xx, y + yy
                if 0 <= px < W and 0 <= py < H:
                    off = 12 + (py * W + px) * 4
                    raw[off:off + 3] = bytes(ink)


def _icon_only_bar(*, thumb=True):
    """Light feed, outline Like, comment bubble, share arrow, low-contrast composer. No words."""
    raw = _light_page()
    if thumb:
        rgb = render_thumb_icon(18, 22, dark=False, filled=False)
        _blit(raw, rgb, 18, 22, 22, 248)
    else:
        _fill(raw, 22, 248, 18, 18, (55, 58, 64))
    _circle(raw, 74, 248, 22)
    _arrow(raw, 126, 248, 20, 22)
    _fill(raw, 12, 292, 156, 16, (255, 255, 255))
    return bytes(raw)


def _deprecated_tap():
    ax, ay = bundled_ui_map()["apps"]["facebook"]["anchors"]["like_button"]
    return str(ax * W // 1000), str(ay * H // 1000)


# ── locator ───────────────────────────────────────────────────────────────────
def test_templates_cover_light_dark_and_three_scales():
    assert templates_available()
    names = {item["name"] for item in load_templates()}
    for theme in ("light", "dark"):
        for scale in ("s", "m", "l"):
            assert f"like_{theme}_{scale}" in names
        for style in ("outline", "filled"):
            for scale in ("s", "m", "l", "xl", "xxl", "xxxl"):
                assert f"like_{theme}_{style}_{scale}" in names


def test_text_row_languages_and_exact_tokens():
    def row(like, comment, share, y=400):
        return text_hits([
            _box(like, 40, y, 90, y + 20),
            _box(comment, 140, y, 220, y + 20),
            _box(share, 260, y, 330, y + 20),
        ], 1000, 1000)

    assert row("Like", "Comment", "Share")[0]["label"] == "like"
    assert row("Gusto", "Komento", "Ibahagi")[0]["label"] == "gusto"
    assert row("I-like", "Comment", "Share")[0]["label"] == "i-like"
    assert row("I like", "Comment", "Share")[0]["label"] == "i like"
    assert row("赞", "评论", "分享")[0]["label"] == "赞"
    assert row("Liked", "Comment", "Share") == []
    assert row("Nagustuhan", "Komento", "Ibahagi") == []
    assert row("已赞", "评论", "分享") == []
    assert text_hits([_box("Like", 40, 400, 90, 420)], 1000, 1000) == []
    assert text_hits([
        _box("Likely", 40, 400, 90, 420),
        _box("Comment", 140, 400, 220, 420),
        _box("Share", 260, 400, 330, 420),
    ], 1000, 1000) == []


def test_no_complete_row_is_not_a_target():
    boxes = [_box("Like", 40, 400, 90, 420), _box("Comment", 140, 400, 220, 420)]
    assert text_hits(boxes, 1000, 1000) == []
    assert fuse_signals(text_hits(boxes, 1000, 1000), [], []) is None


def test_text_alone_does_not_tap_two_signals_do():
    boxes = [
        _box("Gusto", 80, 700, 140, 724),
        _box("Komento", 200, 702, 280, 726),
        _box("Ibahagi", 360, 698, 450, 722),
    ]
    text = text_hits(boxes, 1000, 1000)
    assert text and fuse_signals(text, [], []) is None
    structure = [_hit("structure", text[0]["x_pm"], text[0]["y_pm"])]
    chosen = fuse_signals(text, [], structure)
    assert chosen is not None
    assert chosen["signals"] == "structure+text"
    assert chosen["y"] == text[0]["y_pm"]


def test_multiple_rows_pick_the_topmost_full_row_and_skip_a_clipped_one():
    low = [_hit("text", 120, 640, label="like"), _hit("structure", 110, 650)]
    high = [_hit("text", 120, 280, label="赞"), _hit("structure", 118, 290)]
    clipped = [_hit("text", 120, 8, full=False), _hit("structure", 120, 8, full=False)]
    top = fuse_signals(high[:1] + low[:1] + clipped[:1], [], high[1:] + low[1:] + clipped[1:])
    assert top is not None and top["y"] == 290
    only_low = fuse_signals(low[:1] + clipped[:1], [], low[1:] + clipped[1:])
    assert only_low is not None and only_low["y"] == 650


def _hierarchy(nodes):
    parts = []
    for node in nodes:
        if len(node) == 4:
            text, desc, rid, bounds = node
            cls = "android.widget.ImageView"
        else:
            text, desc, rid, bounds, cls = node
        parts.append(
            f'<node text="{text}" content-desc="{desc}" resource-id="{rid}" '
            f'class="{cls}" bounds="{bounds}" />'
        )
    body = "".join(parts)
    return f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy>{body}</hierarchy>\nUI hierchary dumped to: /dev/tty\n"


def test_hierarchy_label_matches_huoke_needles_and_react():
    width, height = 1000, 2000
    like = label_hits(_hierarchy([
        ("", "Like", "", "[40,700][100,760]"),
        ("", "赞", "", "[40,900][100,960]"),
        ("", "React", "", "[40,1100][100,1160]"),
        ("いいね", "", "", "[40,1300][120,1360]"),
        ("", "", "com.facebook.katana:id/like_button", "[40,1500][100,1560]"),
    ]), width, height)
    assert [h["label"] for h in like] == ["like", "赞", "react", "いいね", "like"]
    assert label_hits(_hierarchy([
        ("", "Liked", "", "[40,700][100,760]"),
        ("", "Unlike", "", "[40,800][100,860]"),
        ("92 reactions", "", "", "[40,900][160,960]"),
        ("", "Comment", "", "[40,1000][120,1060]"),
        ("", "Share", "", "[40,1100][120,1160]"),
        ("", "Like", "", "[1,700][30,740]"),
    ]), width, height) == []
    assert label_hits("not xml", width, height) == []
    assert label_hits("<hierarchy><node content-desc='Like' bounds='nope' /></hierarchy>", width, height) == []


def test_hierarchy_label_needs_a_template():
    label = [_hit("label", 200, 500, label="react")]
    medium = [_hit("template", 210, 510, score=TEMPLATE_MED)]
    assert fuse_signals([], [], [], [], label) is None
    assert fuse_signals([], [], [_hit("structure", 200, 500)], [], label) is None
    assert fuse_signals([], [], [], [_hit("shape", 200, 500)], label) is None
    agreed = fuse_signals([], medium, [], [], label)
    assert agreed is not None
    assert agreed["signals"] == "label+template"
    assert agreed["label"] == "react"


def test_phone_width_like_button_keeps_label_variants_and_resource_id():
    # 360px is a normal xxhdpi action button and used to be dropped at 300.
    wide = label_hits(_hierarchy([("", "React", "", "[40,800][400,900]")]), 1080, 2400)
    assert [h["label"] for h in wide] == ["react"]
    assert wide[0]["by"] == "content-desc"
    # A half-screen banner is still not a button.
    assert label_hits(_hierarchy([("", "React", "", "[10,800][510,900]")]), 1000, 2000) == []
    assert label_hits(_hierarchy([
        ("", "", "com.facebook.katana:id/feed_story_header", "[40,800][140,880]"),
    ]), 1080, 2400) == []
    named = label_hits(_hierarchy([
        ("", "", "com.facebook.katana:id/feed_story_ufi_like_button", "[40,800][140,880]"),
        ("", "Gustuhin", "", "[40,1000][160,1080]"),
        ("", "点赞", "", "[40,1200][140,1280]"),
        ("", "讚", "", "[40,1400][140,1480]"),
    ]), 1080, 2400)
    assert [h["label"] for h in named] == ["like", "gustuhin", "点赞", "讚"]
    assert named[0]["by"] == "resource-id"
    assert named[1]["by"] == "content-desc"


def test_position_needs_a_screenshot_signal():
    position = [_hit("position", 200, 500, label="icon")]
    label = [_hit("label", 200, 500, label="react")]
    assert fuse_signals([], [], [], [], [], position) is None
    assert fuse_signals([], [], [], [], label, position) is None
    agreed = fuse_signals([], [], [_hit("structure", 200, 500)], [], label, position)
    assert agreed is not None
    assert "position" in agreed["signals"] and "structure" in agreed["signals"]
    assert "label" not in agreed["signals"]


def test_region_inside_the_action_bar_agrees_only_with_a_screenshot():
    xml = (
        "<hierarchy>"
        '<node package="com.facebook.katana" class="android.view.View" clickable="true" '
        'content-desc="Like" bounds="[48,770][60,800]" />'
        '<node class="android.view.View" clickable="true" content-desc="Comment" bounds="[240,770][252,800]" />'
        '<node class="android.view.View" clickable="true" content-desc="Share" bounds="[430,770][442,800]" />'
        "</hierarchy>"
    )
    hits = region_hits(xml, "[40,760][520,820]", 1000, 1000)
    assert len(hits) == 1 and hits[0]["source"] == "region"
    assert label_hits(xml, 1000, 1000) == []
    assert fuse_signals([], [], [], [], [], [], hits) is None
    label = [_hit("label", hits[0]["x_pm"], hits[0]["y_pm"], label="like")]
    assert fuse_signals([], [], [], [], label, [], hits) is None
    position = [_hit("position", hits[0]["x_pm"], hits[0]["y_pm"])]
    assert fuse_signals([], [], [], [], [], position, hits) is None
    agreed = fuse_signals([], [], [_hit("structure", hits[0]["x_pm"], hits[0]["y_pm"])], [], [], [], hits)
    assert agreed is not None and agreed["signals"] == "region+structure"


def test_hierarchy_census_does_not_decide_a_tap():
    xml = (
        '<hierarchy><node package="com.facebook.katana" class="android.view.View" '
        'clickable="true" bounds="[10,10][18,40]" />'
        '<node class="android.widget.TextView" clickable="false" bounds="[20,10][28,40]" />'
        "</hierarchy>"
    )
    loc = locate_like_row(_frame(), [], xml)
    assert loc["target"] is None
    census = loc["diag"]["hierarchy"]
    assert census["node_count"] == 2
    assert census["button_count"] == 0
    assert census["clickable_count"] == 1
    assert census["row_counts"] == []
    assert census["top_package"] == "com.facebook.katana"
    assert census["classes"][0] == "android.view.View"
    assert loc["diag"]["region_matched"] is False
    kept = sanitize_phone_result(TASK_PHONE_LIKE, {"like_probe": True, "like_diag": loc["diag"]})
    assert kept["like_diag"]["hierarchy"]["top_package"] == "com.facebook.katana"
    assert kept["like_diag"]["hierarchy"]["node_count"] == 2
    dirty = sanitize_phone_result(TASK_PHONE_LIKE, {
        "like_probe": True,
        "like_diag": {
            **loc["diag"],
            "secret": "nope",
            "hierarchy": {**census, "top_package": "com.facebook.katana/evil", "classes": ["not a class"]},
        },
    })
    assert dirty["like_diag"]["hierarchy"]["top_package"] == ""
    assert dirty["like_diag"]["hierarchy"]["classes"] == []
    assert "secret" not in json.dumps(dirty["like_diag"])


def test_painted_thumb_agrees_with_its_accessibility_label():
    raw, _tmpl, x, y = _with_thumb()
    # The painted block is 16px. Pad the node so it meets the 20px button box
    # and the center stays on the glyph.
    xml = _hierarchy([("", "React", "", f"[{x - 4},{y - 2}][{x + 20},{y + 24}]")])
    loc = locate_like_row(raw, [], xml)
    assert loc["target"] is not None
    assert "label" in loc["target"]["signals"] and "template" in loc["target"]["signals"]
    assert loc["target"]["label"] == "react"


def test_shape_and_template_do_not_confirm_each_other():
    medium = [_hit("template", 200, 500, score=TEMPLATE_MED)]
    shape = [_hit("shape", 200, 500)]
    assert fuse_signals([], medium, [], shape) is None
    assert fuse_signals([], [], [], shape) is None
    agreed = fuse_signals([], medium, [_hit("structure", 210, 510)], shape)
    assert agreed is not None
    assert "structure" in agreed["signals"]
    assert "template" in agreed["signals"] or "shape" in agreed["signals"]


def test_high_template_taps_medium_template_needs_a_second_signal():
    alone = fuse_signals([], [_hit("template", 200, 500, score=TEMPLATE_HIGH)], [])
    assert alone is not None and alone["signals"] == "template"
    medium = [_hit("template", 200, 500, score=TEMPLATE_MED)]
    assert fuse_signals([], medium, []) is None
    agreed = fuse_signals([], medium, [_hit("structure", 210, 510)])
    assert agreed is not None and "template" in agreed["signals"] and "structure" in agreed["signals"]


def test_field_count_row_and_leftmost_slot():
    # Wallpaper 09: counts at y≈736, icons below, no Like word. 1000px == permille.
    boxes = [
        _box("92k", 80, 726, 130, 746),
        _box("1.5k", 300, 726, 360, 746),
    ]
    blobs = [
        {"x0": 85, "y0": 782, "x1": 121, "y1": 818},
        {"x0": 293, "y0": 782, "x1": 349, "y1": 818},
        {"x0": 520, "y0": 782, "x1": 576, "y1": 818},
    ]
    hits = structure_hits(blobs, boxes, 1000, 1000)
    assert len(hits) == 1
    assert hits[0]["x_pm"] == 103 and hits[0]["y_pm"] == 800
    # Same bar after a swipe, 68 permille higher, still the leftmost slot.
    moved = [{**b, "y0": b["y0"] - 68, "y1": b["y1"] - 68} for b in blobs]
    moved_boxes = [{**b, "y0": b["y0"] - 68, "y1": b["y1"] - 68} for b in boxes]
    again = structure_hits(moved, moved_boxes, 1000, 1000)
    assert again[0]["y_pm"] == 732
    both = fuse_signals([], [
        _hit("template", hits[0]["x_pm"], hits[0]["y_pm"], score=0.7),
        _hit("template", again[0]["x_pm"], again[0]["y_pm"], score=0.9),
    ], hits + again)
    assert both["y"] == again[0]["y_pm"]
    assert post_evidence(boxes, blobs, 1000, 1000) is True
    assert empty_phrase([_box("Something went wrong", 200, 400, 700, 440)]) is True
    assert empty_phrase([_box("Stories couldn't load", 180, 460, 720, 500)]) is True


def test_rapidocr_tuple_shape_is_accepted():
    raw = ([
        [[[10, 10], [40, 10], [40, 24], [10, 24]], "Like", 0.91],
        [[[10, 10], [20, 12], [18, 20], [8, 18]], "noise", 0.2],
    ], 0.01)
    boxes = normalize_ocr_boxes(raw)
    assert len(boxes) == 1 and boxes[0]["text"] == "like"
    assert boxes[0]["x0"] == 10 and boxes[0]["y1"] == 24


def test_painted_thumb_is_a_high_confidence_template(tmp_path):
    write_default_templates()
    raw, tmpl, x, y = _with_thumb()
    blobs = extract_blobs(raw)
    hits = template_hits(raw, blobs)
    assert hits, blobs
    assert hits[0]["score"] >= TEMPLATE_HIGH
    loc = locate_like_row(raw, [])
    assert loc["target"] is not None
    assert loc["target"]["signals"] == "template"
    cx = x + tmpl["w"] / 2.0
    cy = y + tmpl["h"] / 2.0
    assert abs(loc["target"]["x"] - round(cx * 1000 / W)) <= 30
    assert abs(loc["target"]["y"] - round(cy * 1000 / H)) <= 30


def test_plain_icons_do_not_pass_the_template_bar():
    raw = _logged(bytearray(_frame()))
    for x in (28, 70, 112):
        _square(raw, x, 250, 16)
    blobs = extract_blobs(bytes(raw))
    hits = template_hits(bytes(raw), blobs)
    assert all(h["score"] < TEMPLATE_HIGH for h in hits)
    boxes = [
        _box("92k", 30, 220, 60, 236),
        _box("1.5k", 78, 220, 120, 236),
    ]
    loc = locate_like_row(bytes(raw), boxes)
    assert loc["post"] is True
    assert loc["target"] is None


def test_icon_only_bar_locates_like_without_ocr_text():
    raw = _icon_only_bar()
    loc = locate_like_row(raw, [])
    assert loc["empty_phrase"] is False
    assert loc["post"] is True
    assert loc["target"] is not None
    assert "structure" in loc["target"]["signals"]
    assert "template" in loc["target"]["signals"] or "shape" in loc["target"]["signals"]
    # Leftmost slot, not the comment bubble or the share arrow.
    assert loc["target"]["x"] < 300


def test_icon_only_squares_are_a_post_but_not_a_tap():
    raw = _icon_only_bar(thumb=False)
    loc = locate_like_row(raw, [])
    assert loc["post"] is True
    assert loc["target"] is None
    assert loc["diag"]["template_matched"] is False
    assert loc["diag"]["uiautomator"]["dump"] == "empty"
    assert loc["diag"]["uiautomator"]["like_found"] is False
    assert isinstance(loc["diag"]["template_score"], float)


def _action_bar_xml():
    return _hierarchy([
        ("", "", "com.facebook.katana:id/feed_story_ufi", "[16,240][48,272]"),
        ("", "Comment", "", "[64,240][100,272]"),
        ("", "Share", "", "[116,240][152,272]"),
    ])


def test_unlabeled_first_button_agrees_with_the_pixel_bar():
    loc = locate_like_row(_icon_only_bar(thumb=False), [], _action_bar_xml())
    assert loc["target"] is not None
    assert "position" in loc["target"]["signals"]
    assert "structure" in loc["target"]["signals"]
    assert loc["target"]["x"] < 300
    assert loc["diag"]["position_matched"] is True
    assert loc["diag"]["uiautomator"]["like_found"] is False
    assert "Comment" in loc["diag"]["action_bar"]["content_descs"]
    assert "Share" in loc["diag"]["action_bar"]["content_descs"]
    assert any("feed_story_ufi" in item for item in loc["diag"]["action_bar"]["resource_ids"])
    node = loc["diag"]["nodes"][0]
    assert set(node) == {"bounds", "class", "content_desc", "resource_id", "text"}


def test_plain_buttons_and_a_reaction_row_are_not_targets():
    unlabeled = _hierarchy([
        ("", "", "", "[16,240][48,272]"),
        ("", "", "", "[64,240][100,272]"),
        ("", "", "", "[116,240][152,272]"),
    ])
    reactions = _hierarchy([
        ("", "Love", "", "[16,240][48,272]"),
        ("", "Haha", "", "[64,240][100,272]"),
        ("", "Wow", "", "[116,240][152,272]"),
    ])
    liked = _hierarchy([
        ("", "Liked", "", "[16,240][48,272]"),
        ("", "Comment", "", "[64,240][100,272]"),
        ("", "Share", "", "[116,240][152,272]"),
    ])
    frame = _icon_only_bar(thumb=False)
    assert locate_like_row(frame, [], unlabeled)["target"] is None
    assert locate_like_row(frame, [], reactions)["target"] is None
    assert locate_like_row(frame, [], liked)["target"] is None


def test_empty_light_page_is_not_a_post():
    raw = bytes(_light_page())
    loc = locate_like_row(raw, [])
    assert loc["post"] is False
    assert loc["target"] is None


def test_phone_width_icon_bar_still_locates_like():
    # 540px is past the blob downsample step. The outline has to survive that.
    width, height = 540, 1200
    raw = bytearray(struct.pack("<III", width, height, 1) + bytes((246, 246, 248, 255)) * (width * height))
    rgb = render_thumb_icon(56, 68, dark=False, filled=False)

    def blit(x, y, glyph, gw, gh):
        for yy in range(gh):
            for xx in range(gw):
                off = 12 + ((y + yy) * width + (x + xx)) * 4
                i = (yy * gw + xx) * 3
                raw[off:off + 3] = glyph[i:i + 3]

    blit(40, 700, rgb, 56, 68)
    ink = bytes((55, 58, 64))
    for yy in range(64):
        for xx in range(64):
            dx, dy = xx - 31.5, yy - 31.5
            radius = (dx * dx + dy * dy) ** 0.5
            if 24 <= radius <= 31:
                off = 12 + ((704 + yy) * width + (200 + xx)) * 4
                raw[off:off + 3] = ink
    for yy in range(64):
        for xx in range(56):
            shaft = abs(yy - 31.5) <= 8 and xx < 34
            head = xx >= 22 and abs(yy - 31.5) <= (55 - xx) * 0.95
            if shaft or head:
                off = 12 + ((706 + yy) * width + (360 + xx)) * 4
                raw[off:off + 3] = ink
    for yy in range(36):
        for xx in range(480):
            off = 12 + ((820 + yy) * width + (30 + xx)) * 4
            raw[off:off + 3] = bytes((255, 255, 255))
    loc = locate_like_row(bytes(raw), [])
    assert loc["post"] is True
    assert loc["target"] is not None
    assert "structure" in loc["target"]["signals"]
    assert loc["target"]["x"] < 250


def test_thumb_plus_neighbor_icons_agrees():
    tmpl, rgb = _light()
    raw = _logged(bytearray(_frame()))
    _blit(raw, rgb, tmpl["w"], tmpl["h"], 28, 250)
    _square(raw, 70, 250, tmpl["w"])
    _square(raw, 112, 250, tmpl["w"])
    boxes = [
        _box("913k", 24, 214, 70, 232),
        _box("1.3k", 74, 214, 120, 232),
        _box("330", 116, 214, 150, 232),
    ]
    loc = locate_like_row(bytes(raw), boxes)
    assert loc["target"] is not None
    assert "template" in loc["target"]["signals"]
    assert loc["target"]["x"] < 400


def test_verify_accepts_blue_or_liked_label_and_ignores_unrelated_change():
    before = _frame()
    after = bytearray(_frame())
    after[12 + 5 * W * 4] = 90
    assert like_state_changed(before, bytes(after), 40, 40) is False
    x, y = 40, 80
    off = 12 + (y * W + x) * 4
    after[off:off + 3] = bytes(_BLUE)
    assert like_state_changed(before, bytes(after), x, y) is True
    relabel = like_state_changed(
        before, before, 40, 80,
        [_box("Like", 20, 70, 60, 90)],
        [_box("Liked", 20, 70, 70, 90)],
    )
    assert relabel is True


# ── flow ──────────────────────────────────────────────────────────────────────
def _run(frame, *, payload=None, ocr=None, frames=None):
    adb = SeqAdb(frames) if frames is not None else FakeAdb(frame=frame)
    ops, fake, _slept = _ops(adb=adb)
    reader = (lambda _raw: []) if ocr is None else ocr
    flows = PhoneFlows(enabled=True, ocr=reader)
    body = payload if payload is not None else _body("facebook", "like", like_swipes=0)
    status, result, detail = flows.execute(TASK_PHONE_LIKE, body, {"serial": "S1"}, ops=ops)
    return status, result, detail, fake


def test_logged_out_frame_does_not_tap_like():
    status, result, detail, fake = _run(_frame(), payload=_body("facebook", "like"))
    assert (status, detail) == (STATUS_FAILED, "app_not_ready")
    assert result["stderr"] == "probe:logged_in"
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(fake.actions()))
    # The blue pixel never appears, so the open poll scrolls up twice to
    # reveal a hidden tab bar, then gives up. It does not scroll the feed.
    assert _feed_dirs(fake.actions()) == ["top", "top"]
    assert sum(1 for a in fake.actions() if a[2:] == ("exec-out", "screencap")) > 4


def test_facebook_already_open_does_not_go_home():
    from types import SimpleNamespace

    class _Front(FakeAdb):
        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 6 and args[2:6] == ("shell", "dumpsys", "activity", "activities"):
                self.calls.append(args)
                text = b"mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.FbMainTabActivity t1}\n"
                return SimpleNamespace(returncode=0, stdout=text, stderr=b"")
            return FakeAdb.__call__(self, cmd, **kw)

    adb = _Front(frame=bytes(_logged(bytearray(_frame()))))
    ops, fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert detail != "not_logged_in"
    assert status == STATUS_FAILED
    assert not any(a[2:6] == ("shell", "input", "keyevent", "3") for a in fake.actions())
    assert not any(len(a) > 4 and a[3] == "am" and a[4] == "start" for a in fake.actions())


def test_wrong_app_and_store_are_not_reported_as_logged_out():
    from types import SimpleNamespace

    from src.fleet.phone_flows import classify_facebook_open

    assert classify_facebook_open("com.facebook.katana") == ""
    assert classify_facebook_open("") == ""
    assert classify_facebook_open("org.telegram.messenger") == "wrong_app_launched:org.telegram.messenger"
    assert classify_facebook_open("com.android.vending") == "fb_not_installed_or_store_redirect"
    assert classify_facebook_open("com.miui.home") == "wrong_app_launched:com.miui.home"

    class _Stuck(FakeAdb):
        def __init__(self, package):
            super().__init__(frame=_frame())
            self.package = package

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 6 and args[2:6] == ("shell", "dumpsys", "activity", "activities"):
                self.calls.append(args)
                text = f"mResumedActivity: ActivityRecord{{abc u0 {self.package}/.Main t1}}\n".encode()
                return SimpleNamespace(returncode=0, stdout=text, stderr=b"")
            if len(args) >= 5 and args[2:5] == ("shell", "pm", "path"):
                self.calls.append(args)
                name = args[5] if len(args) > 5 else ""
                if name in ("com.facebook.katana", "com.facebook.lite"):
                    body = f"package:/data/app/~~x==/{name}-y==/base.apk\n".encode()
                    return SimpleNamespace(returncode=0, stdout=body, stderr=b"")
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            return FakeAdb.__call__(self, cmd, **kw)

    for package, code in (
        ("org.telegram.messenger", "wrong_app_launched:org.telegram.messenger"),
        ("com.android.vending", "fb_not_installed_or_store_redirect"),
        ("com.android.launcher3", "wrong_app_launched:com.android.launcher3"),
    ):
        adb = _Stuck(package)
        ops, fake, _slept = _ops(adb=adb)
        flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
        status, result, detail = flows.execute(
            TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
        assert (status, detail) == (STATUS_FAILED, code), (package, detail)
        assert detail != "not_logged_in"
        assert result["foreground"]["package"] == package
        assert "S1" not in json.dumps(result["foreground"])
        # am start left the old app in front, so the 0.3.25 Home+icon path runs too.
        assert any(a[2:6] == ("shell", "input", "keyevent", "3") for a in fake.actions())
        assert sum(_slept) >= 8.0


def test_missing_facebook_package_is_not_adb_exit():
    from types import SimpleNamespace

    class _Missing(FakeAdb):
        def __init__(self):
            super().__init__(frame=_frame())
            self.started = False

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 5 and args[2:5] == ("shell", "pm", "path"):
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if len(args) >= 5 and args[3:5] == ("am", "start"):
                self.started = True
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Error: Activity not started")
            return FakeAdb.__call__(self, cmd, **kw)

    adb = _Missing()
    ops, fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "fb_not_installed_or_store_redirect")
    assert adb.started is False
    assert not any(len(a) > 4 and a[3] == "am" and a[4] == "start" for a in fake.actions())
    assert not any(a[2:6] == ("shell", "input", "keyevent", "3") for a in fake.actions())


def test_pm_path_real_format_recognizes_katana_lite_and_rejects_errors():
    """Devices print ``package:/data/app/.../base.apk``, not ``package:<name>``."""
    from types import SimpleNamespace

    from src.fleet.phone_ops import pm_path_installed

    katana = "package:/data/app/~~x==/com.facebook.katana-y==/base.apk\n"
    lite = "package:/data/app/com.facebook.lite-1/base.apk\r\n"
    net_health = "package:/data/app/com.facebook.katana-1/base.apk\n"
    for sample, package in ((katana, "com.facebook.katana"), (lite, "com.facebook.lite"), (net_health, "com.facebook.katana")):
        assert ("package:" + package) not in sample
        assert pm_path_installed(sample, 0)
        assert not pm_path_installed(sample, 1)
    assert not pm_path_installed("", 0)
    assert not pm_path_installed("package:", 0)
    assert not pm_path_installed("package:\n", 0)
    assert not pm_path_installed("Error: package not found\n", 0)

    class _Pm(FakeAdb):
        def __init__(self, answers):
            super().__init__(frame=_frame())
            self.answers = answers

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 6 and args[2:5] == ("shell", "pm", "path"):
                self.calls.append(args)
                text, rc = self.answers.get(args[5], ("", 1))
                return SimpleNamespace(returncode=rc, stdout=text.encode(), stderr=b"")
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, _slept = _ops(adb=_Pm({"com.facebook.katana": (katana, 0)}))
    assert ops.facebook_package("S1") == "com.facebook.katana"
    assert not any(len(a) > 4 and a[3] == "am" for a in fake.actions())

    ops, _fake, _slept = _ops(adb=_Pm({"com.facebook.lite": (lite, 0)}))
    assert ops.facebook_package("S1") == "com.facebook.lite"

    ops, fake, _slept = _ops(adb=_Pm({}))
    assert ops.facebook_package("S1") == ""
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "fb_not_installed_or_store_redirect")
    assert not any(len(a) > 4 and a[3] == "am" and a[4] == "start" for a in fake.actions())

    ops, fake, _slept = _ops(adb=_Pm({
        "com.facebook.katana": (katana, 1),
        "com.facebook.lite": (lite, 2),
    }))
    assert ops.facebook_package("S1") == ""
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "fb_not_installed_or_store_redirect")
    assert not any(a[2:6] == ("shell", "input", "keyevent", "3") for a in fake.actions())
    assert not any(len(a) > 4 and a[3] == "am" and a[4] == "start" for a in fake.actions())


def test_facebook_that_appears_during_the_poll_does_not_go_home():
    from types import SimpleNamespace

    class _Slow(FakeAdb):
        def __init__(self):
            super().__init__(frame=_frame())
            self.started = False

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 6 and args[2:6] == ("shell", "dumpsys", "activity", "activities"):
                self.calls.append(args)
                pkg = "com.facebook.katana" if self.started else "org.telegram.messenger"
                text = f"mResumedActivity: ActivityRecord{{abc u0 {pkg}/.Main t1}}\n".encode()
                return SimpleNamespace(returncode=0, stdout=text, stderr=b"")
            if len(args) >= 5 and args[3:5] == ("am", "start"):
                self.started = True
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, _slept = _ops(adb=_Slow())
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "app_not_ready")
    assert any(len(a) > 4 and a[3] == "am" and a[4] == "start" for a in fake.actions())
    assert not any(a[2:6] == ("shell", "input", "keyevent", "3") for a in fake.actions())


def test_dark_lock_screen_is_woken_before_launch():
    from types import SimpleNamespace

    from src.fleet.phone_ops import parse_screen_lock

    assert parse_screen_lock("mAwake=false\nmShowingLockscreen=true\n") == {"awake": False, "locked": True}
    assert parse_screen_lock("mScreenOnFully=true\nisStatusBarKeyguard=false\n") == {"awake": True, "locked": False}
    assert parse_screen_lock("") == {"awake": None, "locked": None}

    class _Dark(FakeAdb):
        def __init__(self):
            super().__init__(frame=_frame())
            self.woken = False
            self.started = False

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 5 and args[2:5] == ("shell", "dumpsys", "window"):
                self.calls.append(args)
                if self.woken:
                    text = b"mAwake=true\nmShowingLockscreen=true\n"
                else:
                    text = b"mAwake=false\nmShowingLockscreen=true\n"
                return SimpleNamespace(returncode=0, stdout=text, stderr=b"")
            if args[2:6] == ("shell", "input", "keyevent", "224"):
                self.woken = True
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if len(args) >= 6 and args[2:6] == ("shell", "dumpsys", "activity", "activities"):
                self.calls.append(args)
                pkg = "com.facebook.katana" if self.started else "com.miui.home"
                text = f"mResumedActivity: ActivityRecord{{abc u0 {pkg}/.Main t1}}\n".encode()
                return SimpleNamespace(returncode=0, stdout=text, stderr=b"")
            if len(args) >= 5 and args[3:5] == ("am", "start"):
                self.started = True
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, _slept = _ops(adb=_Dark())
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "app_not_ready")
    actions = fake.actions()
    wake = next(i for i, a in enumerate(actions) if a[2:6] == ("shell", "input", "keyevent", "224"))
    start = next(i for i, a in enumerate(actions) if len(a) > 4 and a[3] == "am" and a[4] == "start")
    swipes = [i for i, a in enumerate(actions) if len(a) > 4 and a[4] == "swipe"]
    assert swipes and swipes[0] < start and wake < start
    assert not any(a[2:6] == ("shell", "input", "keyevent", "26") for a in actions)
    assert not any(a[2:6] == ("shell", "input", "keyevent", "3") for a in actions)


def test_home_icon_fallback_reaches_facebook():
    from types import SimpleNamespace

    class _Icon(FakeAdb):
        def __init__(self):
            super().__init__(frame=_frame())
            self.homed = False

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if args[2:6] == ("shell", "input", "keyevent", "3"):
                self.homed = True
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            if len(args) >= 6 and args[2:6] == ("shell", "dumpsys", "activity", "activities"):
                self.calls.append(args)
                pkg = "com.facebook.katana" if self.homed else "org.telegram.messenger"
                text = f"mResumedActivity: ActivityRecord{{abc u0 {pkg}/.Main t1}}\n".encode()
                return SimpleNamespace(returncode=0, stdout=text, stderr=b"")
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, slept = _ops(adb=_Icon())
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "app_not_ready")
    assert any(a[2:6] == ("shell", "input", "keyevent", "3") for a in fake.actions())
    icon = bundled_ui_map()["apps"]["facebook"]["anchors"]["app_icon"]
    tap = (str(icon[0] * W // 1000), str(icon[1] * H // 1000))
    assert any(a[-2:] == tap for a in fake.actions() if len(a) > 4 and a[4] == "tap")
    assert sum(slept) >= 8.0


def test_landscape_locks_portrait_or_reports_the_orientation():
    wide = _frame(400, 180)
    tall = _frame(180, 400)

    class _Wide(FakeAdb):
        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            self.frame = wide
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, _slept = _ops(adb=_Wide(frame=wide))
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "landscape_orientation")
    assert result["device_width"] > result["device_height"]
    puts = [a for a in fake.actions() if len(a) > 6 and a[3:6] == ("settings", "put", "system")]
    assert ("user_rotation", "0") == (puts[-1][-2], puts[-1][-1])
    assert any(a[-2:] == ("accelerometer_rotation", "0") for a in puts)

    class _Recover(FakeAdb):
        def __init__(self):
            super().__init__(frame=wide)
            self.shots = 0

        def __call__(self, cmd, **kw):
            args = tuple(cmd[1:])
            if len(args) >= 4 and args[2:] == ("exec-out", "screencap"):
                self.shots += 1
                self.frame = tall if self.shots >= 2 else wide
            return FakeAdb.__call__(self, cmd, **kw)

    ops, fake, _slept = _ops(adb=_Recover())
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0), {"serial": "S1"}, ops=ops)
    assert detail != "landscape_orientation"
    assert detail == "app_not_ready"
    assert status == STATUS_FAILED


def test_login_wall_stops_before_the_feed():
    wall = _paint(3, [(*_px("login_mark"), _WALL)])
    status, result, detail, fake = _run(wall, payload=_body("facebook", "like", like_swipes=0))
    assert (status, detail) == (STATUS_FAILED, "not_logged_in")
    assert result["stderr"] == "probe:login_wall"
    feed = tuple(str(v) for v in _px("feed_tab"))
    assert all((a[-2], a[-1]) != feed for a in _taps(fake.actions()))


def test_empty_feed_after_the_default_swipe_budget():
    frame = bytes(_logged(bytearray(_frame())))
    status, result, detail, fake = _run(frame, ocr=lambda _r: [
        _box("Something went wrong", 20, 160, 150, 180),
        _box("Stories couldn't load", 20, 190, 160, 210),
    ], payload=_body("facebook", "like"))
    assert (status, detail) == (STATUS_FAILED, "empty_feed")
    assert result["swipes"] == 3
    assert "like_x" not in result
    assert _feed_dirs(fake.actions()) == ["top", "top", "feed", "feed", "feed"]
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(fake.actions()))


def test_like_swipes_zero_scans_once():
    frame = bytes(_logged(bytearray(_frame())))
    status, result, detail, fake = _run(frame, payload=_body("facebook", "like", like_swipes=0))
    assert (status, detail) == (STATUS_FAILED, "empty_feed")
    assert result["swipes"] == 0
    # Search budget is zero. Opening still returns the feed to the top.
    assert _feed_dirs(fake.actions()) == ["top", "top"]


def test_icons_without_a_confident_like_are_not_empty_and_not_tapped():
    raw = _logged(bytearray(_frame()))
    for x in (28, 70, 112):
        _square(raw, x, 250, 16)
    boxes = [_box("92k", 30, 220, 60, 236), _box("1.5k", 78, 220, 120, 236)]
    status, result, detail, fake = _run(bytes(raw), ocr=lambda _r: boxes,
                                        payload=_body("facebook", "like", like_swipes=0))
    assert (status, detail) == (STATUS_FAILED, "like_row_not_found")
    assert result["swipes"] == 0
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(fake.actions()))


def test_text_only_row_is_not_tapped():
    boxes = [
        _box("Like", 30, 250, 70, 270),
        _box("Comment", 80, 250, 130, 270),
        _box("Share", 140, 250, 180, 270),
    ]
    frame = bytes(_logged(bytearray(_frame())))
    status, result, detail, fake = _run(frame, ocr=lambda _r: boxes,
                                        payload=_body("facebook", "like", like_swipes=1))
    assert (status, detail) == (STATUS_FAILED, "like_row_not_found")
    assert result["swipes"] == 1
    assert _feed_dirs(fake.actions()) == ["top", "top", "feed"]


def test_unverified_like_is_not_tapped_twice():
    frame, _tmpl, _x, _y = _with_thumb()
    status, result, detail, fake = _run(frame, payload=_body("facebook", "like", like_swipes=0))
    assert (status, detail) == (STATUS_FAILED, "not_verified")
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert sum(1 for a in _taps(fake.actions()) if (a[-2], a[-1]) == (tx, ty)) == 1
    assert "template" in result["like_signals"]


def test_verified_like_taps_once_and_stops():
    frame, tmpl, x, y = _with_thumb()
    blue = bytearray(frame)
    _square(blue, x, y, max(tmpl["w"], tmpl["h"]), _BLUE)
    status, result, detail, fake = _run(frame, frames=[frame, frame, frame, bytes(blue)],
                                        payload=_body("facebook", "like", like_swipes=2))
    assert (status, detail) == (STATUS_DONE, "ok"), detail
    assert result["swipes"] == 0
    assert "like_diag" not in result
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert sum(1 for a in _taps(fake.actions()) if (a[-2], a[-1]) == (tx, ty)) == 1


def test_like_probe_does_not_tap_the_row():
    frame, _tmpl, _x, _y = _with_thumb()
    status, result, detail, fake = _run(
        frame, payload=_body("facebook", "like", like_swipes=2, like_probe=True))
    assert (status, detail) == (STATUS_DONE, "like_probe")
    assert result["like_probe"] is True
    assert result["swipes"] == 0
    assert result["elapsed_ms"] >= 3000
    assert 0 <= result["like_x"] <= 1000 and 0 <= result["like_y"] <= 1000
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert all((a[-2], a[-1]) != (tx, ty) for a in _taps(fake.actions()))
    clean = sanitize_phone_result(TASK_PHONE_LIKE, {**result, "secret": "nope", "text": "Like"})
    assert clean["like_probe"] is True and clean["like_x"] == result["like_x"]
    assert clean["swipes"] == 0 and "secret" not in clean and "text" not in clean
    assert clean["like_diag"]["template_matched"] is True
    assert clean["like_diag"]["uiautomator"]["dump"] == "empty"
    assert "png" not in json.dumps(clean["like_diag"])


def test_finds_the_row_on_a_later_swipe():
    empty = bytes(_logged(bytearray(_frame())))
    found, _tmpl, _x, _y = _with_thumb()
    # size, login, miss, swipe, hit, then the post-tap shot with a blue icon.
    painted = bytearray(found)
    _square(painted, 28, 250, 24, _BLUE)
    frames = [empty, empty, empty, found, bytes(painted)]
    status, result, detail, fake = _run(
        empty, frames=frames, payload=_body("facebook", "like", like_swipes=3))
    assert (status, detail) == (STATUS_DONE, "ok"), detail
    assert result["swipes"] == 1
    assert _feed_dirs(fake.actions()) == ["top", "top", "feed"]


_HIERARCHY_REMOTE = "/sdcard/chatx_like_hierarchy.xml"


class _HierarchyAdb(FakeAdb):
    """Serves one hierarchy. The file dump is pulled; stdout is the fallback."""

    def __init__(self, frame, xml):
        super().__init__(frame=frame)
        self.xml = xml.encode("utf-8") if isinstance(xml, str) else xml

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 5 and args[2:5] == ("shell", "uiautomator", "dump"):
            self.calls.append(args)
            from types import SimpleNamespace
            if args[-1] == "/dev/tty":
                return SimpleNamespace(returncode=0, stdout=self.xml, stderr=b"")
            return SimpleNamespace(
                returncode=0,
                stdout=b"UI hierchary dumped to: /sdcard/chatx_like_hierarchy.xml\n",
                stderr=b"",
            )
        if len(args) >= 5 and args[2] == "pull" and args[3] == _HIERARCHY_REMOTE:
            self.calls.append(args)
            from pathlib import Path
            from types import SimpleNamespace
            Path(args[4]).write_bytes(self.xml)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return FakeAdb.__call__(self, cmd, **kw)


class _RevealAdb(FakeAdb):
    """No blue Home pixel until two toward-top swipes, then the feed frame."""

    def __init__(self, feed):
        super().__init__(frame=_frame())
        self.feed = feed
        self.top = 0
        self.shots = 0

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 9 and args[4] == "swipe" and int(args[8]) > int(args[6]):
            self.top += 1
        if len(args) >= 4 and args[2:] == ("exec-out", "screencap"):
            self.shots += 1
            self.frame = self.feed if self.top >= 2 else _frame()
        return FakeAdb.__call__(self, cmd, **kw)


class _LateFeedAdb(FakeAdb):
    """The blue Home pixel shows up on the third screenshot, not the first."""

    def __init__(self, feed):
        super().__init__(frame=_frame())
        self.feed = feed
        self.shots = 0

    def __call__(self, cmd, **kw):
        args = tuple(cmd[1:])
        if len(args) >= 4 and args[2:] == ("exec-out", "screencap"):
            self.shots += 1
            self.frame = self.feed if self.shots >= 3 else _frame()
        return FakeAdb.__call__(self, cmd, **kw)


def test_hidden_tab_bar_is_revealed_before_the_like_search():
    feed, _tmpl, _x, _y = _with_thumb()
    adb = _RevealAdb(feed)
    ops, _fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0, like_probe=True),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "like_probe"), detail
    assert adb.top >= 2 and adb.shots >= 3
    assert "like_x" in result
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert all((a[-2], a[-1]) != (tx, ty) for a in _taps(adb.actions()))


def test_feed_blue_on_a_later_poll_is_not_app_not_ready():
    feed, _tmpl, _x, _y = _with_thumb()
    adb = _LateFeedAdb(feed)
    ops, _fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0, like_probe=True),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "like_probe"), detail
    assert adb.shots >= 3
    assert "like_x" in result


def test_like_probe_pairs_the_dump_label_with_the_template():
    frame, _tmpl, x, y = _with_thumb()
    xml = _hierarchy([("", "React", "", f"[{x - 4},{y - 2}][{x + 20},{y + 24}]")])
    adb = _HierarchyAdb(frame, xml)
    ops, _fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0, like_probe=True),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "like_probe"), detail
    assert "label" in result["like_signals"] and "template" in result["like_signals"]
    assert result["like_label"] == "react"
    assert any(
        a[2:5] == ("shell", "uiautomator", "dump") and a[-1] == _HIERARCHY_REMOTE and "--compressed" not in a
        for a in adb.actions()
    )
    assert any(len(a) >= 4 and a[2] == "pull" and a[3] == _HIERARCHY_REMOTE for a in adb.actions())
    diag = result["like_diag"]
    assert diag["uiautomator"]["dump"] == "ok"
    assert diag["uiautomator"]["attempts"] == 1
    assert diag["uiautomator"]["via"] == "file"
    assert diag["uiautomator"]["compressed"] is False
    assert diag["uiautomator"]["error"] == ""
    assert diag["uiautomator"]["like_found"] is True
    assert diag["uiautomator"]["match"]["label"] == "react"
    assert diag["uiautomator"]["match"]["by"] == "content-desc"
    assert diag["uiautomator"]["match"]["content_desc"] == "React"
    assert diag["template_matched"] is True
    assert diag["template_score"] >= TEMPLATE_MED
    assert isinstance(diag["structure_matched"], bool)
    assert isinstance(diag["shape_matched"], bool)
    assert 0.0 <= diag["shape_score"] <= 1.0
    if diag["shape_matched"]:
        assert diag["shape_score"] == 1.0
    else:
        assert diag["shape_score"] < 1.0
    if diag["structure_matched"]:
        assert diag["structure_bounds"].startswith("[")
    else:
        assert diag["structure_bounds"] == ""
    assert diag["nodes"] and set(diag["nodes"][0]) == {
        "bounds", "class", "content_desc", "resource_id", "text",
    }
    assert "S1" not in json.dumps(diag)


def test_like_node_inside_the_bar_taps_once_and_still_needs_verification():
    frame = _icon_only_bar(thumb=False)
    xml = _hierarchy([("", "Like", "", "[16,242][46,272]")])
    adb = _HierarchyAdb(frame, xml)
    ops, _fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "not_verified"), detail
    assert "region" in result["like_signals"] and "structure" in result["like_signals"]
    assert "like_diag" not in result
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert sum(1 for a in _taps(adb.actions()) if (a[-2], a[-1]) == (tx, ty)) == 1
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(adb.actions()))


def test_like_probe_names_a_region_hit_without_tapping():
    frame = _icon_only_bar(thumb=False)
    xml = _hierarchy([("", "Like", "", "[16,242][46,272]")])
    adb = _HierarchyAdb(frame, xml)
    ops, _fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0, like_probe=True),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "like_probe"), detail
    assert "region" in result["like_signals"] and "structure" in result["like_signals"]
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert sum(1 for a in _taps(adb.actions()) if (a[-2], a[-1]) == (tx, ty)) == 0
    diag = result["like_diag"]
    assert diag["region_matched"] is True
    assert diag["hierarchy"]["node_count"] >= 1
    assert diag["hierarchy"]["button_count"] >= 1
    assert diag["uiautomator"]["like_found"] is True
    assert diag["uiautomator"]["match"]["by"] == "content-desc"
    assert diag["uiautomator"]["match"]["label"] == "like"
    assert diag["template_matched"] is False
    assert diag["template_score"] < TEMPLATE_HIGH
    assert isinstance(diag["structure_matched"], bool)
    assert diag["shape_matched"] is False
    assert diag["shape_score"] < 1.0
    if diag["structure_matched"]:
        assert diag["structure_bounds"].startswith("[")
    assert diag["nodes"][0]["bounds"] == "[16,242][46,272]"
    assert diag["nodes"][0]["content_desc"] == "Like"
    serial = "TESTSERIAL01"
    dirty = {
        "like_probe": True, "serial": serial,
        "stderr": f"error: device '{serial}' not found",
        "like_diag": {
            **diag,
            "png_b64": "iVBORw0KGgoAAA",
            "nodes": [{**diag["nodes"][0], "content_desc": f"Like {serial}", "png_b64": "iVBORw0KGgoAAA"}],
        },
    }
    clean = sanitize_phone_result(TASK_PHONE_LIKE, dirty, wallpaper="07")
    blob = json.dumps(clean["like_diag"]) + clean["stderr"]
    assert serial not in blob and "iVBORw0KGgo" not in blob and "png" not in blob
    assert "07" in clean["stderr"] and "07" in clean["like_diag"]["nodes"][0]["content_desc"]
    assert clean["serial"] == serial
    offline = sanitize_phone_result(TASK_PHONE_LIKE, {"serial": "S1", "stderr": "error: device offline"})
    assert offline["stderr"] == "error: device offline"


def test_comment_share_row_still_needs_post_tap_verification():
    frame = _icon_only_bar(thumb=False)
    adb = _HierarchyAdb(frame, _action_bar_xml())
    ops, _fake, _slept = _ops(adb=adb)
    flows = PhoneFlows(enabled=True, ocr=lambda _raw: [])
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_swipes=0),
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_FAILED, "not_verified"), detail
    assert "position" in result["like_signals"] and "structure" in result["like_signals"]
    assert "like_diag" not in result
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert sum(1 for a in _taps(adb.actions()) if (a[-2], a[-1]) == (tx, ty)) == 1
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(adb.actions()))


def test_like_probe_finds_an_icon_only_bar_without_ocr():
    frame = _icon_only_bar()
    status, result, detail, fake = _run(
        frame, ocr=lambda _raw: [],
        payload=_body("facebook", "like", like_swipes=0, like_probe=True))
    assert (status, detail) == (STATUS_DONE, "like_probe"), detail
    assert "structure" in result["like_signals"]
    assert "template" in result["like_signals"] or "shape" in result["like_signals"]
    assert result["like_x"] < 300
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert all((a[-2], a[-1]) != (tx, ty) for a in _taps(fake.actions()))


def test_icon_only_squares_stay_like_row_not_found():
    frame = _icon_only_bar(thumb=False)
    status, result, detail, fake = _run(
        frame, ocr=lambda _raw: [],
        payload=_body("facebook", "like", like_swipes=0))
    assert (status, detail) == (STATUS_FAILED, "like_row_not_found"), detail
    assert "like_x" not in result
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(fake.actions()))


def test_rapidocr_import_is_lazy():
    import ast
    from pathlib import Path

    path = Path(locate_like_row.__code__.co_filename)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {"rapidocr_onnxruntime", "cv2", "onnxruntime", "rapidocr"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = {node.module.split(".")[0]}
        else:
            continue
        assert names.isdisjoint(banned)


def test_dry_run_keeps_the_deprecated_coordinate_and_never_touches_adb():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True, ocr=False)
    status, result, detail = flows.execute(
        TASK_PHONE_LIKE, {**_body("facebook", "like"), "dry_run": True, "like_probe": True},
        {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_DONE, "dry_run")
    assert fake.calls == []
    assert result["like_button_deprecated"] is True
    assert {"op": "tap", "x": 900, "y": 560} in result["plan"]
    assert all(set(step) <= {"op", "x", "y", "x1", "y1", "x2", "y2", "duration_ms", "key", "text", "chars", "lo_ms", "hi_ms"}
               for step in result["plan"])
    clean = sanitize_phone_result(TASK_PHONE_LIKE, result)
    assert clean["like_button_deprecated"] is True
    assert clean["dry_run"] is True


def test_ocr_unavailable_refuses_before_adb(monkeypatch):
    import src.fleet.like_locate as loc

    monkeypatch.setattr(loc, "templates_available", lambda folder=None: False)
    monkeypatch.setattr(loc, "rapidocr_available", lambda: False)
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, _result, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like"), {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "ocr_unavailable")
    assert fake.calls == []


def test_protected_phone_still_never_runs_adb():
    ops, fake, _slept = _ops()
    flows = PhoneFlows(enabled=True)
    status, _res, detail = flows.execute(
        TASK_PHONE_LIKE, _body("facebook", "like", like_probe=True),
        {"serial": "3B1F4KE5MS140P4X"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    assert fake.calls == []


@pytest.mark.parametrize("payload,code", [
    ({"app": "facebook", "like_probe": "yes"}, "bad_like_probe"),
    ({"app": "tiktok", "like_probe": True}, "bad_like_probe"),
    ({"app": "instagram", "like_swipes": 1}, "bad_like_swipes"),
    ({"app": "facebook", "like_swipes": 9}, "bad_like_swipes"),
    ({"app": "facebook", "like_swipes": True}, "bad_like_swipes"),
])
def test_like_probe_and_swipe_flags(payload, code):
    with pytest.raises(PhoneOpError) as ei:
        validate_flow_payload(TASK_PHONE_LIKE, payload)
    assert ei.value.code == code


def test_like_probe_false_is_omitted_and_scrolls_are_not_the_scan_budget():
    out = validate_flow_payload(TASK_PHONE_LIKE, {"app": "facebook", "like_probe": False, "scrolls": 0})
    assert out == {"app": "facebook", "scrolls": 0}
    kept = validate_flow_payload(TASK_PHONE_LIKE, {"app": "facebook", "like_swipes": 2.0, "like_probe": True})
    assert kept["like_swipes"] == 2 and kept["like_probe"] is True


def test_build_bundles_templates_without_requiring_rapidocr():
    import importlib.util
    import os
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fleet_agent" / "build_agent.py"
    spec = importlib.util.spec_from_file_location("chatx_build_agent_like", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src_s, dest = mod.like_templates_add_data().rsplit(os.pathsep, 1)
    assert dest == "src/fleet/like_templates"
    assert Path(src_s).is_dir() and any(Path(src_s).glob("like_*.png"))
    text = path.read_text(encoding="utf-8")
    assert "ocr_collect_flags" in text and "rapidocr_onnxruntime" in text


def _wp_bar(header=True, like=False, comment=True, share=True):
    """Synthetic feed: optional post-header row, then Comment/Share (wp11 has no Like)."""
    nodes = []
    if header:
        nodes.append(("", "Alice", "", "[40,180][220,260]"))
        nodes.append(("", "2h", "", "[240,180][360,260]"))
        nodes.append(("", "Follow", "", "[400,180][560,260]"))
    if like:
        nodes.append(("", "Like", "", "[240,900][360,980]"))
    if comment:
        nodes.append(("", "Comment", "", "[480,900][600,980]"))
    if share:
        nodes.append(("", "Share", "", "[720,900][840,980]"))
    return _hierarchy(nodes)


def test_wp11_infers_like_left_of_comment_and_needs_a_screenshot():
    xml = _wp_bar()
    hits = position_hits(xml, 1080, 1920)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["source"] == "position" and hit["inferred"] is True
    assert hit["row_y0"] <= hit["y"] <= hit["row_y1"]
    assert 900 <= hit["y"] <= 980
    assert hit["x"] < 540
    assert hit["y"] > 260
    assert fuse_signals([], [], [], [], [], [hit]) is None
    header = _hit("structure", hit["x_pm"], 115)
    assert fuse_signals([], [], [header], [], [], [hit]) is None
    near = {
        "source": "structure", "x_pm": hit["x_pm"], "y_pm": hit["y_pm"], "score": 1.0,
        "label": "structure", "full": True, "x": hit["x"], "y": hit["y"],
    }
    agreed = fuse_signals([], [], [near], [], [], [hit])
    assert agreed is not None
    assert "position" in agreed["signals"] and "structure" in agreed["signals"]
    assert abs(agreed["y"] - hit["y_pm"]) <= 2
    assert agreed["y"] > 400
    # wp11: a screenshot landing ~600px off the inferred slot must not count,
    # even when its permille matches the slot. An in-window hit whose permille
    # would miss still makes two signals, and it stays on the action-bar row.
    far = {
        "source": "structure", "x_pm": hit["x_pm"], "y_pm": hit["y_pm"], "score": 1.0,
        "label": "structure", "full": True, "x": hit["x"] + 600, "y": hit["y"],
    }
    assert fuse_signals([], [], [far], [], [], [hit]) is None
    slot = dict(hit)
    slot["row_y0"] = int(hit["y"])
    slot["row_y1"] = int(hit["y"]) + 20
    off_row = {
        "source": "template", "x_pm": hit["x_pm"] + 400, "y_pm": hit["y_pm"] + 400,
        "score": 0.5, "label": "template", "full": True,
        "x": int(slot["x"]), "y": int(slot["y"]) - 5,
    }
    assert fuse_signals([], [off_row], [], [], [], [slot]) is None
    inside = {
        "source": "template", "x_pm": hit["x_pm"] + 400, "y_pm": hit["y_pm"] + 400,
        "score": 0.5, "label": "template", "full": True,
        "x": hit["x"] + 30, "y": hit["y"],
    }
    paired = fuse_signals([], [inside], [], [], [], [hit])
    assert paired is not None
    assert paired["signals"].count("+") == 1
    assert "position" in paired["signals"] and "template" in paired["signals"]
    kept = sanitize_phone_result(TASK_PHONE_LIKE, {
        "like_probe": True,
        "like_diag": {
            "position_inferred": True, "position_matched": True, "serial": "3B1FABCDEF",
            "screenshot_outside": True, "screenshot_window_px": 40,
        },
    })
    blob = json.dumps(kept["like_diag"])
    assert kept["like_diag"]["position_inferred"] is True
    assert kept["like_diag"]["screenshot_outside"] is True
    assert kept["like_diag"]["screenshot_window_px"] == 40
    assert "3B1FABCDEF" not in blob and "serial" not in kept["like_diag"]


def test_wp02_header_row_is_not_the_like_slot():
    xml = _wp_bar()
    hits = position_hits(xml, 1080, 1920)
    assert hits and all(h["y"] >= 900 for h in hits)
    assert all(h["row_y0"] >= 900 for h in hits)
    lone_header = position_hits(_hierarchy([
        ("", "Alice", "", "[40,180][220,260]"),
        ("", "Follow", "", "[240,180][400,260]"),
        ("", "Menu", "", "[420,180][540,260]"),
    ]), 1080, 1920)
    assert lone_header == []


def test_wp03_sparse_hierarchy_does_not_invent_a_like():
    nodes = [("", "", "", f"[{20 + i * 40},100][{50 + i * 40},140]") for i in range(6)]
    assert position_hits(_hierarchy(nodes), 1080, 1920) == []
    assert position_hits(_wp_bar(comment=False, share=False), 1080, 1920) == []
