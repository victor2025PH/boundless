# -*- coding: utf-8 -*-
"""角色 LoRA 管线纯函数门禁（trigger 规范 / 训练标注 / VLM 判定 / ai-toolkit 配置）。"""

from __future__ import annotations

from src.ai import persona_lora as pl


# ── 触发词规范 ──────────────────────────────────────────────────────────
def test_sanitize_trigger():
    assert pl.sanitize_trigger("Lin-Xy 99!") == "linxy99"     # 去符号/空格、小写
    assert pl.sanitize_trigger("") == "ohwx"                    # 空→默认
    assert pl.sanitize_trigger("123") == "ohwx"                 # 纯数字→默认（无区分度）
    assert pl.sanitize_trigger("__ohwx__") == "ohwx"            # 去首尾下划线
    assert pl.sanitize_trigger("x" * 50) == "x" * 32            # 截长
    assert pl.sanitize_trigger(None, default="p3rs") == "p3rs"


# ── 训练标注拼装 ────────────────────────────────────────────────────────
def test_build_lora_caption_basic_and_meta_strip():
    out = pl.build_lora_caption("ohwx", "This image shows a woman by a window, smiling.")
    assert out.startswith("ohwx woman, ")
    assert "image shows" not in out.lower()          # 元话术剥离
    assert "window" in out


def test_build_lora_caption_dedupes_leading_tokens():
    # VLM 回声了 trigger + class → 前导重复应剥净，不出现 "linxy woman, woman,"
    out = pl.build_lora_caption("linxy", "linxy woman, close-up, soft smile")
    assert out == "linxy woman, close-up, soft smile"
    assert out.count("woman") == 1


def test_build_lora_caption_empty_desc_and_truncation():
    assert pl.build_lora_caption("ohwx", "") == "ohwx woman"          # 空描述→仅触发短语
    long = "a " + "very " * 200 + "long description"
    out = pl.build_lora_caption("ohwx", long, max_len=60)
    assert len(out) <= 60 and out.startswith("ohwx woman,")


def test_build_lora_caption_subject_class():
    out = pl.build_lora_caption("ohwx", "standing outdoors", subject_class="man")
    assert out.startswith("ohwx man, ")


# ── VLM 单人判定（数据集自动筛）───────────────────────────────────────────
def test_parse_single_person_verdict():
    assert pl.parse_single_person_verdict("Yes, exactly one person with a visible face.")
    assert pl.parse_single_person_verdict("是，单人") is True
    assert not pl.parse_single_person_verdict("No, two people in the frame")
    assert not pl.parse_single_person_verdict("There is a dog / animal")
    assert not pl.parse_single_person_verdict("It's a cartoon anime style")
    assert not pl.parse_single_person_verdict("has a watermark / text")
    assert not pl.parse_single_person_verdict("maybe")     # 含糊即弃
    assert not pl.parse_single_person_verdict("")


def test_probe_prompts_nonempty():
    assert "yes" in pl.single_person_probe_prompt().lower()
    assert "training" in pl.caption_probe_prompt().lower()
    assert "man" in pl.caption_probe_prompt("man")


# ── 数据集 prompt（多样性 + SFW）──────────────────────────────────────────
def test_dataset_prompt_variety_and_sfw():
    p0 = pl.dataset_prompt("a woman", scene_hint="cafe", index=0)
    p1 = pl.dataset_prompt("a woman", scene_hint="cafe", index=1)
    assert p0 != p1                                 # 不同 index → 不同姿态/表情
    assert "safe-for-work" in p0                    # SFW 硬约束仍在
    assert "solo, one person" in p0                 # 单人锚
    # 默认写实风格（未给 style 时）
    assert "photorealistic" in p0


# ── ai-toolkit 配置生成 ──────────────────────────────────────────────────
def test_build_aitoolkit_config_structure():
    c = pl.build_aitoolkit_config(
        persona_id="lin_xiaoyu", dataset_dir="datasets/lora/lin_xiaoyu",
        output_dir="out", trigger="Lin Xy!", steps=1500, rank=32)
    assert c["job"] == "extension"
    proc = c["config"]["process"][0]
    assert proc["type"] == "sd_trainer"
    assert proc["trigger_word"] == "linxy"          # 触发词经规范
    assert proc["network"]["linear"] == 32
    assert proc["train"]["steps"] == 1500
    assert proc["train"]["train_text_encoder"] is False   # FLUX 角色 LoRA 只训 UNet
    assert proc["model"]["is_flux"] is True
    ds = proc["datasets"][0]
    assert ds["folder_path"] == "datasets/lora/lin_xiaoyu"
    assert ds["caption_ext"] == "txt"
    # 采样 prompt 带 trigger（训练中途出样可早停）
    assert any("linxy" in s for s in proc["sample"]["prompts"])
    assert c["config"]["name"] == "lin_xiaoyu_flux_lora"


# ── checkpoint 选优 + 注册表写回 ─────────────────────────────────────────
def test_rank_and_pick_checkpoints():
    results = [
        {"name": "a", "verdict": "ok", "summary": {"ref_mean": 0.45, "self_consistency": 0.8, "no_face_ratio": 0.0}},
        {"name": "b", "verdict": "ok", "summary": {"ref_mean": 0.52, "self_consistency": 0.7, "no_face_ratio": 0.0}},
        {"name": "c", "verdict": "warn", "summary": {"ref_mean": 0.60, "self_consistency": 0.9, "no_face_ratio": 0.0}},
        {"name": "d", "verdict": "fail", "summary": {"ref_mean": 0.70, "self_consistency": 0.9, "no_face_ratio": 0.5}},
    ]
    ranked = pl.rank_checkpoints(results)
    # verdict 优先于 ref_mean：ok 组(b>a) 在 warn(c) 前，fail(d) 垫底（哪怕 ref_mean 最高）
    assert [r["name"] for r in ranked] == ["b", "a", "c", "d"]
    assert pl.pick_best_checkpoint(results)["name"] == "b"
    assert pl.pick_best_checkpoint([]) is None
    # 同 verdict 同 ref_mean → 自一致性打破平手
    tie = [
        {"name": "x", "verdict": "ok", "summary": {"ref_mean": 0.5, "self_consistency": 0.6, "no_face_ratio": 0.0}},
        {"name": "y", "verdict": "ok", "summary": {"ref_mean": 0.5, "self_consistency": 0.9, "no_face_ratio": 0.0}},
    ]
    assert pl.pick_best_checkpoint(tie)["name"] == "y"


def test_registry_entry_normalizes():
    e = pl.registry_entry("a.safetensors", "Lin Xy!", 0.8)
    assert e == {"file": "a.safetensors", "trigger": "linxy", "weight": 0.8}
    assert pl.registry_entry("a.safetensors")["trigger"] == ""        # 无 trigger→空
    assert pl.registry_entry("a.safetensors", weight="bad")["weight"] == 0.9  # 非法权重兜底


def test_write_lora_registry_entry_merge_atomic(tmp_path):
    p = tmp_path / "persona_lora.json"
    d = pl.write_lora_registry_entry(
        str(p), "lin_xiaoyu", {"file": "a.safetensors", "trigger": "linxy", "weight": 0.9})
    assert d["lin_xiaoyu"] == {"file": "a.safetensors", "trigger": "linxy", "weight": 0.9}
    # 再写另一个 pid → 合并保留旧项
    pl.write_lora_registry_entry(str(p), "chen", {"file": "b.safetensors"})
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data) == {"lin_xiaoyu", "chen"}
    assert data["chen"]["weight"] == 0.9 and data["chen"]["trigger"] == ""
    # 覆写同一 pid
    pl.write_lora_registry_entry(str(p), "lin_xiaoyu", {"file": "c.safetensors", "weight": 1.0})
    data2 = json.loads(p.read_text(encoding="utf-8"))
    assert data2["lin_xiaoyu"]["file"] == "c.safetensors"
