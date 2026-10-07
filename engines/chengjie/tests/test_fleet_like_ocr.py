"""Facebook like finds the button on a screenshot. Mock adb, no device.

Text is one signal. A tap needs a high-confidence thumbs-up template or two
agreeing signals (text, template, action bar). The fixed like_button is not a tap.
"""
from __future__ import annotations

import pytest

from src.fleet.like_locate import (
    TEMPLATE_HIGH, TEMPLATE_MED, empty_phrase, extract_blobs, fuse_signals, like_state_changed,
    load_templates, locate_like_row, normalize_ocr_boxes, post_evidence, render_thumb,
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
    assert _swipes(fake.actions()) == []


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
    assert _swipes(fake.actions())
    assert all((a[-2], a[-1]) != _deprecated_tap() for a in _taps(fake.actions()))


def test_like_swipes_zero_scans_once():
    frame = bytes(_logged(bytearray(_frame())))
    status, result, detail, fake = _run(frame, payload=_body("facebook", "like", like_swipes=0))
    assert (status, detail) == (STATUS_FAILED, "empty_feed")
    assert result["swipes"] == 0
    assert _swipes(fake.actions()) == []


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
    assert len(_swipes(fake.actions())) == 1


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
    assert 0 <= result["like_x"] <= 1000 and 0 <= result["like_y"] <= 1000
    tx = str(result["like_x"] * W // 1000)
    ty = str(result["like_y"] * H // 1000)
    assert all((a[-2], a[-1]) != (tx, ty) for a in _taps(fake.actions()))
    clean = sanitize_phone_result(TASK_PHONE_LIKE, {**result, "secret": "nope", "text": "Like"})
    assert clean["like_probe"] is True and clean["like_x"] == result["like_x"]
    assert clean["swipes"] == 0 and "secret" not in clean and "text" not in clean


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
    assert len(_swipes(fake.actions())) == 1


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
