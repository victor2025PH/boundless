# -*- coding: utf-8 -*-
"""每人设「角色 LoRA」管线的**纯逻辑**（真人感根治：把身份焙进权重）。

背景：PuLID 单图零样本锁脸 → 从正面定妆照注入 → 头位置/表情千篇一律（见
``companion_selfie`` 诊断）。角色 LoRA 是工业级正解——用一批**多样**图训练一个
per-persona LoRA，身份学进模型后可用变化 prompt + 随机种子自由出图、几乎丢掉 PuLID。

本模块只放**可单测纯函数**（触发词规范/训练标注拼装/VLM 单人判定/ai-toolkit 配置
生成/数据集分镜脚本），不碰 GPU、不发 HTTP、不做文件 IO——编排在 ``tools/persona_lora_dataset.py``
（出图走 comfy_infer 子进程、打标走 VisionClient 的 HTTP、训练是 176 上的外部 launcher）。

全程 SFW：数据集 prompt 复用 ``build_selfie_prompt(sfw=True)`` 的硬约束。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

# 数据集「分镜」预设：给同一人设覆盖不同景别/角度/表情/光线，让 LoRA 学到的是
# 「这张脸」而非「这个姿势」。与 companion_selfie 的多样性池同源理念，但这里是**训练集
# 覆盖度**导向（刻意穷举镜头语言），且 variety_salt 用 index → 每张确定性不同、可复现。
_DATASET_STYLE = (
    "photorealistic, candid smartphone photo, natural lighting, "
    "natural skin texture with pores, sharp focus, high detail"
)

# ai-toolkit(ostris) 采样用的默认负向（FLUX 走 flowmatch，一般空即可，这里留钩子）。
_DEFAULT_SUBJECT_CLASS = "woman"


def sanitize_trigger(token: Any, default: str = "ohwx") -> str:
    """规范触发词（训练用的稀有 token）：只留字母/数字/下划线、转小写、去首尾下划线。

    触发词应是训练语料里罕见的串（如 ``ohwx``/``p3rs0n``/``linxy``），避免撞常用词
    污染语义。空/非法（清洗后为空或纯数字）回落 ``default``。长度截到 32。
    """
    s = re.sub(r"[^0-9A-Za-z_]", "", str(token or "")).strip("_").lower()
    if not s or s.isdigit():
        return default
    return s[:32]


def build_lora_caption(
    trigger: str,
    description: str,
    *,
    subject_class: str = _DEFAULT_SUBJECT_CLASS,
    max_len: int = 220,
) -> str:
    """拼一条训练标注行：``"<trigger> <subject_class>, <描述>"``（纯函数）。

    - ``trigger``＋``subject_class`` 恒在句首（ai-toolkit/kohya 惯例：触发短语领衔）；
      VLM 描述里若已带 trigger/class 词做去重，避免 "ohwx woman ohwx woman"。
    - 描述做清洗：去换行/引号/markdown、压缩空白、去掉"这张图片显示/the image shows"
      一类元话术（对训练是噪声），按 ``max_len`` 截断到词边界。
    - 描述为空 → 只返回 ``"<trigger> <subject_class>"``（至少有触发短语可训）。
    """
    trig = sanitize_trigger(trigger)
    cls = re.sub(r"[^0-9A-Za-z_ ]", "", str(subject_class or "")).strip() or _DEFAULT_SUBJECT_CLASS
    lead = f"{trig} {cls}".strip()
    desc = _clean_caption_desc(description)
    # 去掉描述开头重复的 trigger/class 词（大小写不敏感，循环剥净"linxy woman, …"这类回声）。
    if desc:
        _lead_re = re.compile(
            r"^(?:%s|%s)[\s,]+" % (re.escape(trig), re.escape(cls)), re.IGNORECASE)
        while True:
            stripped = _lead_re.sub("", desc).strip()
            if stripped == desc:
                break
            desc = stripped
    if not desc:
        return lead
    line = f"{lead}, {desc}"
    if len(line) <= max_len:
        return line
    # 词边界截断（不切半个词）。
    cut = line[:max_len].rsplit(" ", 1)[0].rstrip(",; ")
    return cut or line[:max_len]


_META_PHRASES = (
    "this image shows", "the image shows", "the photo shows", "this photo shows",
    "in this image", "in this photo", "the picture shows", "this picture shows",
    "这张图片显示", "这张照片显示", "图中显示", "图片中", "照片中", "画面中",
    "这是一张", "这张图", "图中是", "图中",
)


def _clean_caption_desc(description: Any) -> str:
    """清洗 VLM 描述：单行化、去引号/markdown、去元话术前缀、压缩空白（纯函数）。"""
    s = str(description or "").strip()
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.strip().strip('"').strip("“”'`").strip()
    s = re.sub(r"[*_#>`]+", " ", s)  # markdown 记号
    low = s.lower()
    for ph in _META_PHRASES:
        if low.startswith(ph):
            s = s[len(ph):].lstrip(" :，,、").strip()
            low = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


# VLM 单人合格判定：明确肯定词 / 明确否定信号（多人/无人/动物/文字水印/卡通）。
_VERDICT_YES = ("yes", "correct", "single", "one person", "是", "对", "单人", "一个人")
_VERDICT_NO = ("no", "none", "multiple", "two ", "three ", "group", "nobody",
               "animal", "cartoon", "anime", "text", "watermark", "logo", "collage",
               "否", "不是", "多人", "两个", "三个", "没有人", "动物", "卡通", "文字", "水印")


def parse_single_person_verdict(vlm_reply: Any) -> bool:
    """解析 VLM「是否恰好一个真人、无动物/文字/水印」的回答 → 合格与否（纯函数）。

    保守口径（宁可漏进也别错收进训练集）：命中任一否定信号 → False；否则命中肯定词 → True；
    都没命中 → False（含糊即弃，训练集质量优先）。
    """
    t = str(vlm_reply or "").strip().lower()
    if not t:
        return False
    if any(n in t for n in _VERDICT_NO):
        return False
    return any(y in t for y in _VERDICT_YES)


def single_person_probe_prompt() -> str:
    """给 VLM 的「数据集自动筛选」判定 prompt（英文，本地 qwen2.5vl 稳定）。"""
    return (
        "Look at this image. Answer strictly with 'yes' or 'no' on the first line: "
        "is there exactly ONE real human person (a photograph, not cartoon/anime/3D "
        "render), with a clearly visible face, and NO other people, NO animals, NO "
        "text/watermark/logo? Then optionally one short reason."
    )


def caption_probe_prompt(subject_class: str = _DEFAULT_SUBJECT_CLASS) -> str:
    """给 VLM 的「训练标注」描述 prompt：产出简洁客观、可训练的英文外观/场景描述。"""
    cls = str(subject_class or _DEFAULT_SUBJECT_CLASS).strip()
    return (
        f"Describe this photo of a {cls} for image model training in ONE concise "
        "English sentence. Cover: framing (close-up/half-body), head pose and gaze "
        "direction, facial expression, outfit, hair, and background/scene. Be "
        "objective and literal. Do NOT mention identity, names, beauty judgments, "
        "or 'the image shows'. No markdown, no quotes."
    )


def dataset_prompt(
    persona: Any,
    *,
    scene_hint: str = "",
    style: str = "",
    default_appearance: str = "",
    index: int = 0,
) -> str:
    """数据集第 ``index`` 张的出图 prompt（纯函数）：复用 ``build_selfie_prompt`` +
    ``variety_salt=index`` → 每张确定性地取不同景别/姿态/表情/视线/写实质感，
    训练集覆盖度最大化且可复现。``style`` 空时用数据集专用写实风格。
    """
    from src.ai.companion_selfie import build_selfie_prompt

    return build_selfie_prompt(
        persona,
        scene_hint=scene_hint,
        style=str(style or "").strip() or _DATASET_STYLE,
        default_appearance=default_appearance,
        variety_salt=int(index),
    )


def build_aitoolkit_config(
    *,
    persona_id: str,
    dataset_dir: str,
    output_dir: str,
    trigger: str,
    base_model: str = "black-forest-labs/FLUX.1-dev",
    steps: int = 2000,
    rank: int = 16,
    resolutions: Optional[List[int]] = None,
    sample_prompts: Optional[List[str]] = None,
    subject_class: str = _DEFAULT_SUBJECT_CLASS,
    quantize: bool = True,
    lr: float = 1e-4,
) -> Dict[str, Any]:
    """生成 ai-toolkit(ostris) 的 FLUX LoRA 训练 config（dict，可 ``yaml.dump``）。

    只训 UNet（``train_text_encoder:false``，FLUX 角色 LoRA 常规）；flowmatch 调度、
    adamw8bit、bf16。``base_model`` 可填本地 FLUX.1-dev 目录（176 上落地路径）。
    ``quantize:true`` 让 24G+ 卡也能训（5090 32G 可关以提速）。采样 prompt 缺省用
    trigger 组两条，训练中途出样便于早停。纯函数：不写盘、不校验路径存在。
    """
    trig = sanitize_trigger(trigger)
    res = [int(x) for x in (resolutions or [768, 1024])]
    name = f"{persona_id}_flux_lora"
    prompts = list(sample_prompts or [
        f"{trig} {subject_class}, close-up selfie, soft natural smile, "
        "cozy room, warm light, photorealistic",
        f"{trig} {subject_class}, half-body, looking away, candid outdoor photo, "
        "daylight, photorealistic",
    ])
    return {
        "job": "extension",
        "config": {
            "name": name,
            "process": [
                {
                    "type": "sd_trainer",
                    "training_folder": str(output_dir),
                    "device": "cuda:0",
                    "trigger_word": trig,
                    "network": {
                        "type": "lora",
                        "linear": int(rank),
                        "linear_alpha": int(rank),
                    },
                    "save": {
                        "dtype": "float16",
                        "save_every": max(1, int(steps) // 8),
                        "max_step_saves_to_keep": 4,
                    },
                    "datasets": [
                        {
                            "folder_path": str(dataset_dir),
                            "caption_ext": "txt",
                            "caption_dropout_rate": 0.05,
                            "shuffle_tokens": False,
                            "cache_latents_to_disk": True,
                            "resolution": res,
                        }
                    ],
                    "train": {
                        "batch_size": 1,
                        "steps": int(steps),
                        "gradient_accumulation_steps": 1,
                        "train_unet": True,
                        "train_text_encoder": False,
                        "gradient_checkpointing": True,
                        "noise_scheduler": "flowmatch",
                        "optimizer": "adamw8bit",
                        "lr": float(lr),
                        "dtype": "bf16",
                    },
                    "model": {
                        "name_or_path": str(base_model),
                        "is_flux": True,
                        "quantize": bool(quantize),
                    },
                    "sample": {
                        "sampler": "flowmatch",
                        "sample_every": max(1, int(steps) // 8),
                        "width": 1024,
                        "height": 1024,
                        "prompts": prompts,
                        "neg": "",
                        "seed": 42,
                        "walk_seed": True,
                        "guidance_scale": 4,
                        "sample_steps": 20,
                    },
                }
            ],
        },
        "meta": {"name": name, "version": "1.0"},
    }


# ── checkpoint 自动选优 + 部署写回（训练交付闭环）─────────────────────────
_VERDICT_RANK = {"ok": 2, "warn": 1, "fail": 0}


def rank_checkpoints(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按身份保真度给候选 checkpoint 排序（best-first，纯函数）。

    ``results``＝``[{"name", "verdict", "summary": {ref_mean, self_consistency,
    no_face_ratio, ...}}, ...]``（summary 出自 ``face_fidelity.summarize_fidelity``）。
    排序键（降序）：verdict(ok>warn>fail) → ref_mean（像不像本人）→ self_consistency
    （是不是同一个人）→ 无脸率越低越好。让训练中途存的多个 step **客观选优**，
    取代"人眼看采样图挑"。
    """
    def _key(r: Dict[str, Any]):
        s = r.get("summary") or {}
        sc = s.get("self_consistency")
        return (
            _VERDICT_RANK.get(str(r.get("verdict") or ""), 0),
            float(s.get("ref_mean") or 0.0),
            float(sc) if sc is not None else 0.0,
            -float(s.get("no_face_ratio") or 0.0),
        )

    return sorted(list(results or []), key=_key, reverse=True)


def pick_best_checkpoint(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """选保真度最高的 checkpoint（纯函数）；空 → None。"""
    ranked = rank_checkpoints(results)
    return ranked[0] if ranked else None


def registry_entry(file: str, trigger: str = "", weight: float = 0.9) -> Dict[str, Any]:
    """规范化一条注册表项（纯函数）：``{file, trigger, weight}``，trigger 经规范化。"""
    try:
        w = float(weight)
    except (TypeError, ValueError):
        w = 0.9
    return {
        "file": str(file or "").strip(),
        "trigger": sanitize_trigger(trigger) if str(trigger or "").strip() else "",
        "weight": w,
    }


def write_lora_registry_entry(
    path: str, pid: str, spec: Dict[str, Any],
) -> Dict[str, Any]:
    """把一条 LoRA spec 写进 JSON 注册表的 ``<pid>`` 项（load→merge→原子替换）。

    机器独占文件（``config/persona_lora.json``），不碰人工带注释的 YAML → 零风险不丢注释。
    与 ``companion_selfie.resolve_persona_lora`` 的中间优先级层同一 schema。返回写入后的全量 dict。
    """
    p = str(path)
    data: Dict[str, Any] = {}
    try:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}
    data[str(pid)] = registry_entry(
        str(spec.get("file") or ""), str(spec.get("trigger") or ""),
        spec.get("weight", 0.9))
    parent = os.path.dirname(os.path.abspath(p)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return data


__all__ = [
    "sanitize_trigger",
    "build_lora_caption",
    "parse_single_person_verdict",
    "single_person_probe_prompt",
    "caption_probe_prompt",
    "dataset_prompt",
    "build_aitoolkit_config",
    "rank_checkpoints",
    "pick_best_checkpoint",
    "registry_entry",
    "write_lora_registry_entry",
]
