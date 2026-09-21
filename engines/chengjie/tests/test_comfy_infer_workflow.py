# -*- coding: utf-8 -*-
"""comfy_infer.build_workflow 门禁：LoRA 链 / PuLID 姿态参数 / 模型出口路由。

治「头位置表情千篇一律」与「角色 LoRA 部署」两处改动的回归网——保证节点图的
model 走线正确（LoRA 串联 → PuLID → KSampler），PuLID 的 start_at/weight 真的进节点。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

import comfy_infer as ci  # noqa: E402

_DEV = "flux1-dev-fp8.safetensors"
_SCHNELL = "flux1-schnell-fp8.safetensors"


def _wf(**kw):
    base = dict(prompt="x", width=1024, height=1024, steps=20, guidance=3.5,
                seed=1, ckpt=_DEV)
    base.update(kw)
    return ci.build_workflow(**base)


# ── PuLID 姿态参数透传（治千篇一律的核心）────────────────────────────────
def test_pulid_pose_params_flow_into_node():
    wf = _wf(face_ref_name="f.png", face_weight=0.8,
             pulid_start_at=0.12, pulid_end_at=0.9)
    n = wf["24"]["inputs"]
    assert n["weight"] == 0.8
    assert n["start_at"] == 0.12
    assert n["end_at"] == 0.9
    # 无 face_ref → 无 PuLID 节点
    assert "24" not in _wf()


def test_pulid_defaults_backward_compatible():
    # 不传姿态参数 → 保持旧行为（start_at=0.0 全程锁到底）
    wf = _wf(face_ref_name="f.png")
    n = wf["24"]["inputs"]
    assert n["start_at"] == 0.0 and n["end_at"] == 1.0 and n["weight"] == 0.9


# ── LoRA 链路由 ─────────────────────────────────────────────────────────
def test_no_lora_no_face_model_routing():
    wf = _wf(ckpt=_SCHNELL)
    assert "40" not in wf                          # 无 LoRA 节点
    assert wf["3"]["inputs"]["model"] == ["4", 0]  # KSampler 直连 checkpoint


def test_single_lora_routes_through_loader():
    wf = _wf(lora_name="linxy.safetensors", lora_weight=0.9)
    assert wf["40"]["class_type"] == "LoraLoaderModelOnly"
    assert wf["40"]["inputs"]["model"] == ["4", 0]
    assert wf["40"]["inputs"]["lora_name"] == "linxy.safetensors"
    assert wf["40"]["inputs"]["strength_model"] == 0.9
    assert wf["3"]["inputs"]["model"] == ["40", 0]  # KSampler 用经 LoRA 的模型


def test_multi_lora_chained_in_order():
    wf = _wf(lora_name="linxy.safetensors, realism.safetensors", lora_weight=0.8)
    assert wf["40"]["inputs"]["lora_name"] == "linxy.safetensors"
    assert wf["40"]["inputs"]["model"] == ["4", 0]
    assert wf["41"]["inputs"]["lora_name"] == "realism.safetensors"
    assert wf["41"]["inputs"]["model"] == ["40", 0]   # 串联在第一个之后
    assert wf["3"]["inputs"]["model"] == ["41", 0]


def test_lora_before_pulid():
    # 角色 LoRA + 锁脸：PuLID 应注入**经 LoRA 的模型**，KSampler 用 PuLID 输出。
    wf = _wf(lora_name="linxy.safetensors", face_ref_name="f.png")
    assert wf["24"]["inputs"]["model"] == ["40", 0]   # PuLID 吃 LoRA 输出
    assert wf["3"]["inputs"]["model"] == ["24", 0]    # KSampler 吃 PuLID 输出


def test_lora_clip_routes_clip_through_loader():
    # --lora-clip：用全 LoraLoader(model+clip)，文本编码改用经 LoRA 的 clip（含 TE 的 LoRA）。
    wf = _wf(lora_name="a.safetensors,b.safetensors", lora_weight=0.8, lora_clip=True)
    assert wf["40"]["class_type"] == "LoraLoader"
    assert wf["40"]["inputs"]["clip"] == ["4", 1]
    assert wf["40"]["inputs"]["strength_clip"] == 0.8
    assert wf["41"]["inputs"]["clip"] == ["40", 1]     # clip 也串联
    assert wf["6"]["inputs"]["clip"] == ["41", 1]      # 文本编码用最后 LoRA 的 clip
    assert wf["7"]["inputs"]["clip"] == ["41", 1]


def test_lora_model_only_default_leaves_clip():
    # 默认 model-only（角色 LoRA 标准）：clip 不改（仍连 checkpoint）。
    wf = _wf(lora_name="a.safetensors")
    assert wf["40"]["class_type"] == "LoraLoaderModelOnly"
    assert wf["6"]["inputs"]["clip"] == ["4", 1]
    assert wf["7"]["inputs"]["clip"] == ["4", 1]


def test_parse_loras_helper():
    assert ci._parse_loras("", 1.0) == []
    assert ci._parse_loras("a.safetensors", 0.7) == [("a.safetensors", 0.7)]
    assert ci._parse_loras("a, b ,", 0.5) == [("a", 0.5), ("b", 0.5)]


@pytest.mark.parametrize("ckpt,has_guidance", [(_DEV, True), (_SCHNELL, False)])
def test_flux_guidance_only_for_dev(ckpt, has_guidance):
    wf = _wf(ckpt=ckpt)
    assert ("10" in wf) is has_guidance    # schnell 不挂 FluxGuidance


# ── 显存自愈：卸同卡 Ollama 兜底模型（2026-07-14「要照片不给」根因修复）─────
def test_ollama_url_from_comfy_same_host():
    orig = ci.COMFY_URL
    try:
        ci.COMFY_URL = "http://192.168.0.176:8188"
        assert ci._ollama_url_from_comfy() == "http://192.168.0.176:11434"
    finally:
        ci.COMFY_URL = orig


def test_is_vision_model_skips_vlm():
    # VLM 出图后体检要用 → 腾显存时跳过（大头是 30B 聊天模型，不是 5G 的 VLM）
    assert ci._is_vision_model("qwen2.5vl:7b")
    assert ci._is_vision_model("llava:13b")
    assert ci._is_vision_model("minicpm-v:8b")
    # 聊天兜底大模型 → 该卸
    assert not ci._is_vision_model("qwen3:30b-a3b-instruct-2507-q4_K_M")
    assert not ci._is_vision_model("hy-mt2-7b")


def test_free_ollama_unloads_nonvlm_only(monkeypatch):
    """卸载只针对非 VLM 驻留模型，且逐个 keep_alive=0。"""
    posted = []

    class _Resp:
        def __init__(self, body=b""):
            self._b = body
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        url = getattr(req, "full_url", req)
        if isinstance(url, str) and url.endswith("/api/ps"):
            import json as _j
            return _Resp(_j.dumps({"models": [
                {"model": "qwen3:30b-a3b-instruct-2507-q4_K_M"},
                {"model": "qwen2.5vl:7b"},
            ]}).encode())
        # /api/generate 卸载请求：记录 body
        try:
            posted.append(req.data.decode())
        except Exception:
            posted.append("")
        return _Resp(b"{}")

    monkeypatch.setattr(ci.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ci.time, "sleep", lambda *a: None)
    n = ci._free_ollama("http://192.168.0.176:11434")
    assert n == 1                                   # 只卸了 1 个（VLM 被跳过）
    assert len(posted) == 1
    assert "qwen3:30b" in posted[0] and '"keep_alive": 0' in posted[0]


def test_free_ollama_empty_url_noop(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("空 url 不应发任何请求")
    monkeypatch.setattr(ci.urllib.request, "urlopen", boom)
    assert ci._free_ollama("") == 0


def test_ensure_vram_escalates_to_ollama(monkeypatch):
    """ComfyUI 自卸后仍不足 → 触发 Ollama 卸载 → 再查放行。"""
    free_seq = iter([10.0, 11.0, 20.0])  # 初查 / ComfyUI 卸后 / Ollama 卸后
    monkeypatch.setattr(ci, "_vram_free_gb", lambda: next(free_seq))
    monkeypatch.setattr(ci, "_free_comfy", lambda: None)
    called = {}
    monkeypatch.setattr(ci, "_free_ollama", lambda url: called.setdefault("url", url) or 1)
    out = ci.ensure_vram(14.0, ollama_url="http://192.168.0.176:11434")
    assert out == 20.0                              # 卸 Ollama 后腾够放行
    assert called["url"] == "http://192.168.0.176:11434"


def test_ensure_vram_no_ollama_when_comfy_enough(monkeypatch):
    free_seq = iter([10.0, 16.0])                   # ComfyUI 自卸就够 → 不碰 Ollama
    monkeypatch.setattr(ci, "_vram_free_gb", lambda: next(free_seq))
    monkeypatch.setattr(ci, "_comfy_reserved_gb", lambda: 0.1)   # 未常驻=旧路径
    monkeypatch.setattr(ci, "_free_comfy", lambda: None)
    def boom(url):
        raise AssertionError("够了不该卸 Ollama")
    monkeypatch.setattr(ci, "_free_ollama", boom)
    assert ci.ensure_vram(14.0, ollama_url="http://x:11434") == 16.0


# ── 热加载优先（2026-08-28）：模型已在显存里时，min_free_gb 那道「装得下吗」的
#    闸门不适用；旧实现无条件 /free 会把马上要用的那份卸掉再从头搬一遍。
def test_ensure_vram_keeps_resident_model(monkeypatch):
    """已常驻 + 活化显存够 → 一个字节都不腾，直接放行。"""
    monkeypatch.setattr(ci, "_vram_free_gb", lambda: 5.0)     # < min 14
    monkeypatch.setattr(ci, "_comfy_reserved_gb", lambda: 16.0)   # 模型在显存里
    def boom_comfy():
        raise AssertionError("模型已常驻，绝不能 /free 自毁热缓存")
    def boom_ollama(url):
        raise AssertionError("活化显存够，不该动 Ollama")
    monkeypatch.setattr(ci, "_free_comfy", boom_comfy)
    monkeypatch.setattr(ci, "_free_ollama", boom_ollama)
    assert ci.ensure_vram(14.0, ollama_url="http://x:11434") >= 14.0  # 闸门放行


def test_ensure_vram_resident_but_tight_evicts_only_ollama(monkeypatch):
    """已常驻但活化空间不足 → 只卸别人（Ollama 秒级可重载），绝不卸自己。"""
    free_seq = iter([1.0, 6.0])       # 初查 / 卸 Ollama 后
    monkeypatch.setattr(ci, "_vram_free_gb", lambda: next(free_seq))
    monkeypatch.setattr(ci, "_comfy_reserved_gb", lambda: 16.0)
    def boom_comfy():
        raise AssertionError("绝不卸自己")
    monkeypatch.setattr(ci, "_free_comfy", boom_comfy)
    called = {}
    monkeypatch.setattr(ci, "_free_ollama",
                        lambda url: called.setdefault("url", url) or 1)
    assert ci.ensure_vram(14.0, ollama_url="http://x:11434") >= 14.0
    assert called["url"] == "http://x:11434"


def test_ensure_vram_cold_path_unchanged(monkeypatch):
    """未常驻（reserved 极小）→ 完全走旧路径（/free 自己再卸 Ollama）。"""
    free_seq = iter([10.0, 11.0, 20.0])
    monkeypatch.setattr(ci, "_vram_free_gb", lambda: next(free_seq))
    monkeypatch.setattr(ci, "_comfy_reserved_gb", lambda: 0.1)
    seen = []
    monkeypatch.setattr(ci, "_free_comfy", lambda: seen.append("comfy"))
    monkeypatch.setattr(ci, "_free_ollama", lambda url: seen.append("ollama") or 1)
    assert ci.ensure_vram(14.0, ollama_url="http://x:11434") == 20.0
    # 旧路径原样：自卸 → 卸 Ollama → 再让 ComfyUI 整理一次碎片
    assert seen == ["comfy", "ollama", "comfy"]


def test_ensure_vram_enough_free_short_circuits(monkeypatch):
    """空闲本来就够 → 连常驻探针都不查（热路零多余往返）。"""
    monkeypatch.setattr(ci, "_vram_free_gb", lambda: 25.0)
    def boom():
        raise AssertionError("空闲够时不该再探常驻")
    monkeypatch.setattr(ci, "_comfy_reserved_gb", boom)
    assert ci.ensure_vram(14.0, ollama_url="http://x:11434") == 25.0


# ── 闸门随卡容量折算（实施102 阶段3：出图 176(32G) → 104(12G)）
@pytest.mark.parametrize("min_free,total,expected", [
    (14.0, 12.0, 10.2),    # 4070 12G：14 永远达不到 → 85% 卡容量
    (14.0, 31.8, 14.0),    # 5090：原值不动
    (14.0, -1.0, 14.0),    # 查不到总量 → 原值
    (14.0, 0.0, 14.0),
    (5.0, 12.0, 5.0),      # 调用方显式给小闸门 → 尊重
])
def test_effective_min_free_gb_caps_to_card(min_free, total, expected):
    assert ci.effective_min_free_gb(min_free, total) == expected


def test_vram_stats_unknown_when_comfy_down(monkeypatch):
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(ci.urllib.request, "urlopen", boom)
    assert ci._vram_stats_gb() == (-1.0, -1.0)
    assert ci._vram_free_gb() == -1.0


# ── 保温器：判「保得住吗」的纯函数（--lowvram 下保温物理无效，别白烧 GPU）
def test_warmup_detects_flags_that_forbid_residency():
    import importlib
    cw = importlib.import_module("comfy_warmup")
    argv = ["main.py", "--listen", "0.0.0.0", "--port", "8188", "--lowvram"]
    assert cw.blocking_flags(argv) == ["--lowvram"]
    assert cw.blocking_flags(["main.py", "--novram", "--cache-none"]) == [
        "--novram", "--cache-none"]
    # 正常启动（可常驻）→ 空清单，保温才有意义
    assert cw.blocking_flags(["main.py", "--listen", "0.0.0.0"]) == []
    assert cw.blocking_flags([]) == []
