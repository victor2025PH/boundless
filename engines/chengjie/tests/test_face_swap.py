"""换脸后处理层（``src/ai/face_swap.py``）门禁：纯函数 + 软失败放行契约。

关键不变量：换脸服务任何故障都不能劣化发图——失败一律返回原图路径（passthrough）。
"""
from __future__ import annotations

import pytest

import src.ai.face_swap as fs


@pytest.fixture(autouse=True)
def _reset():
    fs.reset_metrics()
    yield
    fs.reset_metrics()


def test_resolve_cfg_defaults():
    cfg = fs.resolve_face_swap_cfg({})
    assert cfg["enabled"] is False  # 默认关（增强项需显式开）
    assert cfg["enhance"] == "codeformer"
    c2 = fs.resolve_face_swap_cfg({"face_swap": {"enabled": True, "enhance": "gfpgan"}})
    assert c2["enabled"] is True and c2["enhance"] == "gfpgan"


def test_build_payload_and_parse():
    p = fs.build_swap_payload("SRC", "TGT", enhance="gfpgan")
    assert p == {"source_image": "SRC", "target_image": "TGT", "enhance": "gfpgan"}
    assert fs.parse_swap_result({"result_image": "XX"}) == "XX"
    assert fs.parse_swap_result({"image": "YY"}) == "YY"
    assert fs.parse_swap_result({}) == ""
    assert fs.parse_swap_result("nope") == ""


def test_disabled_is_passthrough(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    ref = tmp_path / "face.png"
    ref.write_bytes(b"\x89PNG")
    out = fs.swap_face_file(str(img), str(ref), {"enabled": False})
    assert out == str(img)  # 关 → 原样返回，不碰网络


def test_missing_input_passthrough(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    # face_ref 不存在 → 原图路径 + passthrough 计数
    out = fs.swap_face_file(str(img), str(tmp_path / "nope.png"), {"enabled": True})
    assert out == str(img)
    assert fs.metrics_snapshot()["passthrough"] == 1


def test_unreachable_service_passthrough(tmp_path, monkeypatch):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGdata")
    ref = tmp_path / "face.png"
    ref.write_bytes(b"\x89PNGface")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(fs, "_post_json", boom)
    out = fs.swap_face_file(str(img), str(ref), {"enabled": True, "base_url": "http://x"})
    assert out == str(img)  # 服务挂 → 软放行原图
    assert fs.metrics_snapshot()["passthrough"] == 1


def test_success_writes_swap_file(tmp_path, monkeypatch):
    import base64
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGdata")
    ref = tmp_path / "face.png"
    ref.write_bytes(b"\x89PNGface")
    payload_seen = {}

    def fake_post(url, payload, timeout, token):
        payload_seen.update(payload)
        return {"result_image": base64.b64encode(b"SWAPPEDBYTES").decode()}

    monkeypatch.setattr(fs, "_post_json", fake_post)
    out = fs.swap_face_file(str(img), str(ref), {"enabled": True})
    assert out == str(tmp_path / "a.swap.png")
    assert (tmp_path / "a.swap.png").read_bytes() == b"SWAPPEDBYTES"
    assert fs.metrics_snapshot()["swapped"] == 1
    # source=face_ref、target=生成图（顺序不能反，反了会把生成脸贴到证件照上）
    import base64 as _b
    assert payload_seen["source_image"] == _b.b64encode(b"\x89PNGface").decode()
    assert payload_seen["target_image"] == _b.b64encode(b"\x89PNGdata").decode()


def test_empty_result_passthrough(tmp_path, monkeypatch):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNGdata")
    ref = tmp_path / "face.png"
    ref.write_bytes(b"\x89PNGface")
    monkeypatch.setattr(fs, "_post_json", lambda *a, **k: {"result_image": ""})
    out = fs.swap_face_file(str(img), str(ref), {"enabled": True})
    assert out == str(img)
    assert fs.metrics_snapshot()["passthrough"] == 1


async def test_maybe_swap_async_disabled_shortcircuit(tmp_path, monkeypatch):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")

    def boom(*a, **k):
        raise AssertionError("should not touch thread when disabled")

    monkeypatch.setattr(fs, "swap_face_file", boom)
    out = await fs.maybe_swap_face(str(img), str(img), {"enabled": False})
    assert out == str(img)
