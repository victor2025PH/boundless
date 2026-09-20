"""入站视频「多图直喂」升级门禁（2026-08-15，算力本地化批次）。

背景：视频理解原形态＝4 帧拼一张宫格 → 单图 VLM——N 帧挤进一张 max_image_dim
的图，每帧只剩 1/N 的像素预算。qwen*-vl 原生支持多图，176 已把 vl 钉成常驻，
多图直喂（每帧独立 video_frame_dim 缩放）是纯精度增益。

核心不变量：
- 多图是**增强不是替代**——开关默认关；开了以后任何失败（后端不支持/帧不足/
  空答/异常）都静默回落宫格路径，视频理解永不因新路径变哑；
- 两条路径共用同一取点函数（_frame_points）——看到的是同一批画面；
- zhipu 云兜底不支持多图 → describe_images_sync 直接 None（绝不误发）。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.video_frames import (  # noqa: E402
    _frame_points, extract_frames_list, ffmpeg_available,
)
from src.vision_client import VisionClient  # noqa: E402
import src.ai.inbound_video as iv  # noqa: E402


# ── 取点逻辑（宫格/多图共用）─────────────────────────────────────

def test_frame_points_single_is_midpoint():
    assert _frame_points(10.0, 1) == [5.0]


def test_frame_points_uniform_within_band():
    pts = _frame_points(100.0, 4)
    assert len(pts) == 4
    assert pts[0] == pytest.approx(8.0) and pts[-1] == pytest.approx(92.0)
    assert all(b > a for a, b in zip(pts, pts[1:]))


def test_frame_points_unknown_duration_fallback():
    assert _frame_points(0.0, 3) == [0.0, 1.0, 2.0]


# ── 抽帧列表（真 ffmpeg，缺则跳过）───────────────────────────────

@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe 不可用")
def test_extract_frames_list_real_video(tmp_path):
    vid = tmp_path / "t.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i",
         "testsrc=duration=2:size=192x108:rate=10", str(vid)],
        capture_output=True, timeout=60,
    )
    assert vid.exists() and vid.stat().st_size > 0
    res = extract_frames_list(str(vid), str(tmp_path / "frames"), frames=3)
    assert res is not None
    paths, dur = res
    assert len(paths) == 3 and dur > 0
    assert all(Path(p).exists() and Path(p).stat().st_size > 0 for p in paths)
    # 时间序命名（f0 < f1 < f2）
    assert [Path(p).name for p in paths] == ["f0.jpg", "f1.jpg", "f2.jpg"]


def test_extract_frames_list_bad_input_soft_none(tmp_path):
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe 不可用")
    assert extract_frames_list(str(tmp_path / "nope.mp4"),
                               str(tmp_path / "o")) is None


# ── describe_images_sync（多图请求形状）──────────────────────────

def _tiny_jpgs(tmp_path, n):
    from PIL import Image
    out = []
    for i in range(n):
        p = tmp_path / f"i{i}.jpg"
        Image.new("RGB", (32, 24), (i * 40 % 255, 10, 10)).save(p, "JPEG")
        out.append(str(p))
    return out


class _FakeOA:
    """捕获 create() 入参并返回固定文案的假 OpenAI 客户端。"""

    def __init__(self, out="三帧显示同一只猫在移动"):
        self.calls = []
        outer = self

        class _Comp:
            def create(self, **kw):
                outer.calls.append(kw)
                msg = SimpleNamespace(content=outer._out)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        self._out = out
        self.chat = SimpleNamespace(completions=_Comp())


def _vc_openai(fake, cfg=None):
    vc = VisionClient(dict(cfg or {"provider": "openai_compatible",
                                   "model": "qwen3-vl:8b-instruct",
                                   "video_frame_dim": 128}))
    vc._backend = "openai"
    vc._oa_endpoints = [("http://e1/v1", fake)]
    return vc


def test_describe_images_sync_payload_shape(tmp_path):
    fake = _FakeOA()
    vc = _vc_openai(fake)
    paths = _tiny_jpgs(tmp_path, 3)
    out = vc.describe_images_sync(paths, prompt="按时间顺序描述")
    assert out == "三帧显示同一只猫在移动"
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    content = kw["messages"][0]["content"]
    imgs = [c for c in content if c.get("type") == "image_url"]
    texts = [c for c in content if c.get("type") == "text"]
    assert len(imgs) == 3 and len(texts) == 1
    assert content[-1]["type"] == "text"  # 文本指令在帧列表之后
    assert all(c["image_url"]["url"].startswith("data:image/jpeg;base64,")
               for c in imgs)
    assert kw["temperature"] == 0  # 抽取任务贪心解码（与单图同纪律）


def test_describe_images_sync_ollama_endpoint_uses_native_api(tmp_path):
    """:11434 端点走原生 /api/chat（options.num_ctx 生效），不碰 /v1 SDK。"""
    fake = _FakeOA()
    vc = _vc_openai(fake)
    vc._oa_endpoints = [("http://192.168.0.176:11434/v1", fake)]
    seen = {}

    def _native(root, raws, prompt):
        seen["root"] = root
        seen["n"] = len(raws)
        seen["raw_ok"] = all(not r.startswith("data:") for r in raws)
        seen["prompt"] = prompt
        return "原生多图描述"

    vc._ollama_native_images_request = _native
    out = vc.describe_images_sync(_tiny_jpgs(tmp_path, 3), prompt="按时间顺序")
    assert out == "原生多图描述"
    assert fake.calls == []  # SDK /v1 分支不该被触发
    assert seen["root"] == "http://192.168.0.176:11434"
    assert seen["n"] == 3 and seen["raw_ok"] and seen["prompt"] == "按时间顺序"


def test_describe_images_sync_native_fail_falls_to_next_endpoint(tmp_path):
    """原生端点失败 → 冷却记账 → 换下一（非 Ollama）/v1 端点成功。"""
    fake = _FakeOA(out="v1 兜底描述")
    vc = _vc_openai(fake)
    vc._oa_endpoints = [("http://192.168.0.176:11434/v1", None),
                        ("http://e2/v1", fake)]

    def _boom(root, raws, prompt):
        raise RuntimeError("native down")

    vc._ollama_native_images_request = _boom
    out = vc.describe_images_sync(_tiny_jpgs(tmp_path, 2))
    assert out == "v1 兜底描述"
    assert len(fake.calls) == 1


def test_describe_images_sync_zhipu_backend_returns_none(tmp_path):
    vc = VisionClient({"provider": "zhipu"})
    vc._backend = "zhipu"
    assert vc.describe_images_sync(_tiny_jpgs(tmp_path, 3)) is None


def test_describe_images_sync_needs_two_valid_frames(tmp_path):
    fake = _FakeOA()
    vc = _vc_openai(fake)
    only_one = _tiny_jpgs(tmp_path, 1) + [str(tmp_path / "missing.jpg")]
    assert vc.describe_images_sync(only_one) is None
    assert fake.calls == []  # 帧不足连请求都不该发


# ── _video_visual_desc 接线（开关/回落语义）──────────────────────

_VCFG = {"provider": "openai_compatible", "base_url": "http://e1/v1",
         "model": "m", "video_frames": 3}


async def _run_visual(vision_cfg, monkeypatch, *, multi_ret, montage_ret):
    calls = {"multi": 0, "montage": 0}

    async def _fake_multi(video_path, loop, cfg, *, frames):
        calls["multi"] += 1
        return multi_ret

    def _fake_montage(video_path, out_path, *, frames=4, **kw):
        calls["montage"] += 1
        Path(out_path).write_bytes(b"\xff\xd8fake")
        return (out_path, 2.0, frames)

    async def _fake_fallback(merged, gv, image_path, prompt=None):
        return montage_ret, "ollama_ok"

    monkeypatch.setattr(iv, "_video_visual_desc_multi", _fake_multi)
    import src.utils.video_frames as vf
    monkeypatch.setattr(vf, "extract_frames_montage", _fake_montage)
    import src.vision_client as vcm
    monkeypatch.setattr(vcm.VisionClient, "describe_image_with_ollama_zhipu_fallback",
                        classmethod(lambda cls, m, g, p, prompt=None:
                                    _fake_fallback(m, g, p, prompt)))
    loop = asyncio.get_running_loop()
    out = await iv._video_visual_desc("/tmp/v.mp4", loop, vision_cfg)
    return out, calls


async def test_multi_flag_off_never_calls_multi(monkeypatch):
    out, calls = await _run_visual(dict(_VCFG), monkeypatch,
                                   multi_ret="不该出现", montage_ret="宫格描述")
    assert out == "宫格描述"
    assert calls["multi"] == 0 and calls["montage"] == 1


async def test_multi_flag_on_uses_multi_first(monkeypatch):
    cfg = dict(_VCFG, video_multi_image=True)
    out, calls = await _run_visual(cfg, monkeypatch,
                                   multi_ret="多图描述", montage_ret="宫格描述")
    assert out == "多图描述"
    assert calls["multi"] == 1 and calls["montage"] == 0


async def test_multi_failure_falls_back_to_montage(monkeypatch):
    cfg = dict(_VCFG, video_multi_image=True)
    out, calls = await _run_visual(cfg, monkeypatch,
                                   multi_ret=None, montage_ret="宫格描述")
    assert out == "宫格描述"
    assert calls["multi"] == 1 and calls["montage"] == 1
