# -*- coding: utf-8 -*-
"""ComfyUI 文生图客户端 —— 供 companion.selfie.provider.backend=command 调用。

用法（SelfieProvider command_args 会填 {prompt} {out}）：
    python tools/comfy_infer.py --prompt "a slice of matcha cake" --out out.png --url http://192.168.0.176:8188

锁脸（PuLID-Flux，人物自拍必须带，保证每张都是同一个"林小雨"）：
    python tools/comfy_infer.py --prompt "..." --out out.png \
        --face-ref assets/persona_media/lin_xiaoyu/face_ref.png

流程：构造 FLUX(fp8 all-in-one) API 工作流（--face-ref 时插入 PuLID 锁脸链：
基准脸经 /upload/image 上传 → LoadImage → ApplyPulidFlux 注入 FLUX）→
POST /prompt → 轮询 /history → 经 /view 取图落 --out。全程软失败：任何异常 →
退出码 !=0 + stderr 说明，调用方（image_autosend/SelfieProvider）据此回落
文本/相册，绝不把半成品当成功。

首次带 --face-ref 运行会在 5090 上自动下载 antelopev2(~360MB, GitHub) +
EVA02-CLIP(~850MB, HuggingFace)，冷启动给大 --timeout（如 1800）。

模型路由（2026-07-14 商用合规）：FLUX.1-dev 非商用许可 → 无脸图（物体/风景，
--face-ref 空）默认走 --ckpt-noface（FLUX.1-schnell fp8，Apache 2.0 可商用、
4 步蒸馏更快）；锁脸自拍走 --ckpt（PuLID 在 dev 上训练，效果最稳）。
steps/guidance 按模型自适应：schnell=4 步无 FluxGuidance；dev=20 步 guidance 3.5
（--steps/--guidance 显式给了则尊重调用方）。

环境变量：
    COMFY_URL         ComfyUI 基址（默认 http://127.0.0.1:8188；生产直连 176:8188）
    COMFY_CKPT        锁脸/默认 checkpoint（默认 flux1-dev-fp8.safetensors）
    COMFY_CKPT_NOFACE 无脸图 checkpoint（默认 flux1-schnell-fp8.safetensors；
                      置空串=不路由，全部用 COMFY_CKPT）
    COMFY_FACE_REF    基准脸路径（--face-ref 的默认值；配置层给 selfie 场景统一注入）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
CKPT = os.environ.get("COMFY_CKPT", "flux1-dev-fp8.safetensors")
CKPT_NOFACE = os.environ.get("COMFY_CKPT_NOFACE", "flux1-schnell-fp8.safetensors")
PULID_FILE = os.environ.get("COMFY_PULID", "pulid_flux_v0.9.1.safetensors")

# PuLID 锁脸参数默认值（可被 CLI/env 覆盖）。face_weight 越大越像基准脸；
# start_at 是**姿态/表情僵化的关键**：0.0=从去噪第 0 步就注入人脸 → 构图/头部
# 朝向被基准正面照钉死（"每张头位置表情都一样"的主因）。start_at 抬到 ~0.1-0.2
# 让构图/姿态先在早期步成形、PuLID 只在中后段锁身份 → 同一个人但姿态表情自由。
# 保守默认保持旧行为（0.0/1.0/0.9），线上经 command_args/env opt-in 调优。
def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


PULID_WEIGHT = _env_float("COMFY_FACE_WEIGHT", 0.9)
PULID_START_AT = _env_float("COMFY_PULID_START_AT", 0.0)
PULID_END_AT = _env_float("COMFY_PULID_END_AT", 1.0)

# 角色 LoRA（真人感根治：per-persona 训练后身份"焙"进模型，可几乎丢掉 PuLID）/
# 写实 LoRA（修蜡感皮肤）。默认空=不挂。COMFY_LORA 可给逗号分隔多个（各自 weight
# 用 COMFY_LORA_WEIGHT 的对应项，缺省沿用末项/1.0），串联叠加。
LORA_NAME = os.environ.get("COMFY_LORA", "").strip()
LORA_WEIGHT = _env_float("COMFY_LORA_WEIGHT", 1.0)
# 默认 LoraLoaderModelOnly（只改 UNet，FLUX 角色 LoRA 标准，最省最稳）；含文本编码器
# (TE/CLIP) 训练的写实/风格 LoRA 需 model+clip → 置 COMFY_LORA_CLIP=1 用全 LoraLoader。
LORA_CLIP = os.environ.get("COMFY_LORA_CLIP", "").strip() not in ("", "0", "false", "False")


def is_schnell(ckpt: str) -> bool:
    """按文件名识别 guidance-distilled 快速模型（schnell/turbo 系）。"""
    low = str(ckpt or "").lower()
    return "schnell" in low or "turbo" in low


# ── 多引擎（2026-08-21，工具箱「AI 生成图片」按局域网算力分配）────────────────
# 默认 engine=flux＝现役 FLUX(fp8)+PuLID 锁脸链，**零行为变更**（autosend 自动链的
# command_args 不带 --engine → 恒走 flux）。qwen_edit / z_image 是坐席手动出图的可选
# 引擎，需 ComfyUI 侧先装对应模型（未装＝提交 400 → 调用方回落 flux/相册/文字）。
#
# GPU 分配（智聊模式实测：176=5090 现役 ComfyUI+FLUX 稳态仅剩 ~5.7G；173=5090 有更多
# 名义余量但常驻 vLLM-27B+IndexTTS）：
#   - 默认把 qwen_edit/z_image 也指向 **176:8188 现役 ComfyUI**（与 FLUX 模型热切换，
#     同为出图用途不并存；本文件的 ensure_vram 会先卸同卡 Ollama 非 VLM 模型腾显存）。
#     手动出图是低频动作，冷切换成本可接受。
#   - 用量起来或切换抖动明显 → 把 --url 指向 173:8188 独立 ComfyUI（需在 173 泊车
#     vLLM/IndexTTS 后装 Qwen 模型）。切换只改 config 的 command_args --url，无需改码。
#
# 节点图不可测风险的兜底：Qwen-Image-Edit-2511 的 ComfyUI 原生工作流节点名随版本演进，
# 本文件内置的是「最佳努力」图；**推荐运维用 --workflow-template 指向自己 ComfyUI 里
# 导出的 API 格式工作流 JSON**（占位符见 _apply_workflow_template），保证与在装版本逐字节
# 对齐——那份是「一定跑得通」的真相，内置图只是便利默认。
_QWEN_EDIT_UNET = os.environ.get("COMFY_QWEN_UNET", "qwen_image_edit_2511_fp8_e4m3fn.safetensors")
_QWEN_EDIT_CLIP = os.environ.get("COMFY_QWEN_CLIP", "qwen_2.5_vl_7b_fp8_scaled.safetensors")
_QWEN_EDIT_VAE = os.environ.get("COMFY_QWEN_VAE", "qwen_image_vae.safetensors")
_ZIMAGE_UNET = os.environ.get("COMFY_ZIMAGE_UNET", "z_image_turbo_bf16.safetensors")
_ZIMAGE_CLIP = os.environ.get("COMFY_ZIMAGE_CLIP", "qwen_2.5_vl_7b_fp8_scaled.safetensors")
_ZIMAGE_VAE = os.environ.get("COMFY_ZIMAGE_VAE", "z_image_vae.safetensors")


def _apply_workflow_template(raw: str, *, prompt: str, seed: int, width: int,
                             height: int, steps: int, ref_image_name: str,
                             out_prefix: str = "aitr_gen") -> dict:
    """把运维导出的 ComfyUI API 格式工作流 JSON 里的占位符替换成本次参数。

    支持占位符（字符串内子串替换，数字占位放在字符串里由 ComfyUI 自转型；模板作者
    也可直接写数字并让本函数只替换文本占位）：
        %PROMPT% %SEED% %WIDTH% %HEIGHT% %STEPS% %REF_IMAGE% %OUT_PREFIX%
    这是「引擎无关」的稳妥路径：模板即运维在装 ComfyUI 里跑通过的真实工作流。
    """
    def _sub(s: str) -> str:
        return (s.replace("%PROMPT%", prompt)
                 .replace("%REF_IMAGE%", ref_image_name)
                 .replace("%OUT_PREFIX%", out_prefix)
                 .replace("%SEED%", str(seed))
                 .replace("%WIDTH%", str(width))
                 .replace("%HEIGHT%", str(height))
                 .replace("%STEPS%", str(steps)))
    wf = json.loads(_sub(raw))

    def _walk(node):
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return _sub(node)
        return node
    return _walk(wf)


def build_qwen_edit_workflow(prompt: str, *, width: int, height: int, steps: int,
                             seed: int, ref_image_name: str = "",
                             unet: str = "", clip: str = "", vae: str = "") -> dict:
    """Qwen-Image-Edit-2511 的 ComfyUI 原生工作流（最佳努力，可被 --workflow-template 覆写）。

    人物一致性靠**参考图直出**（提示词只描述场景/衣着，长相取自参考图）——治现役
    PuLID「头位表情千篇一律」。ref_image_name 空＝退化成纯文生图（Qwen-Image 基座）。
    节点名以 2026 ComfyUI 原生 Qwen-Image-Edit 支持为准；不同版本可能命名有别，届时用
    模板覆写。steps=0 → 20（非蒸馏基座）。
    """
    _unet = unet or _QWEN_EDIT_UNET
    _clip = clip or _QWEN_EDIT_CLIP
    _vae = vae or _QWEN_EDIT_VAE
    _steps = steps if steps > 0 else 20
    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": _unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _clip, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _vae}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["2", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "aitr_gen"}},
    }
    if ref_image_name:
        # 参考图编辑链：TextEncodeQwenImageEditPlus 吃 clip+vae+参考图+prompt，
        # 同时产出正向 conditioning 与参考 latent（2511 支持多参考，这里单参考）。
        wf["20"] = {"class_type": "LoadImage", "inputs": {"image": ref_image_name}}
        wf["5"] = {"class_type": "TextEncodeQwenImageEditPlus",
                   "inputs": {"prompt": prompt, "clip": ["2", 0], "vae": ["3", 0],
                              "image1": ["20", 0]}}
        wf["10"] = {"class_type": "EmptySD3LatentImage",
                    "inputs": {"width": width, "height": height, "batch_size": 1}}
        wf["6"] = {"class_type": "KSampler",
                   "inputs": {"model": ["1", 0], "positive": ["5", 0],
                              "negative": ["7", 0], "latent_image": ["10", 0],
                              "seed": seed, "steps": _steps, "cfg": 2.5,
                              "sampler_name": "euler", "scheduler": "simple",
                              "denoise": 1.0}}
    else:
        wf["5"] = {"class_type": "CLIPTextEncode",
                   "inputs": {"text": prompt, "clip": ["2", 0]}}
        wf["10"] = {"class_type": "EmptySD3LatentImage",
                    "inputs": {"width": width, "height": height, "batch_size": 1}}
        wf["6"] = {"class_type": "KSampler",
                   "inputs": {"model": ["1", 0], "positive": ["5", 0],
                              "negative": ["7", 0], "latent_image": ["10", 0],
                              "seed": seed, "steps": _steps, "cfg": 2.5,
                              "sampler_name": "euler", "scheduler": "simple",
                              "denoise": 1.0}}
    return wf


def build_zimage_workflow(prompt: str, *, width: int, height: int, steps: int,
                          seed: int, unet: str = "", clip: str = "",
                          vae: str = "") -> dict:
    """Z-Image-Turbo（6B, Apache 2.0, 8 步蒸馏）的 ComfyUI 工作流（最佳努力）。

    定位＝高频泛用图的速度档（拟真口碑最好）；不锁脸（纯文生图）。steps=0 → 8（蒸馏）。
    """
    _unet = unet or _ZIMAGE_UNET
    _clip = clip or _ZIMAGE_CLIP
    _vae = vae or _ZIMAGE_VAE
    _steps = steps if steps > 0 else 8
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": _unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _clip, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _vae}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["2", 0]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["2", 0]}},
        "10": {"class_type": "EmptySD3LatentImage",
               "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["5", 0],
                         "negative": ["7", 0], "latent_image": ["10", 0],
                         "seed": seed, "steps": _steps, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "aitr_gen"}},
    }


def _log(m: str) -> None:
    print("[comfy_infer] " + m, file=sys.stderr, flush=True)


# ── 本机出图互斥锁（跨进程，串行化出图，防显存峰值叠加）──────────────────────
import tempfile

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "comfy_infer.lock")


def _acquire_lock(wait_sec: float):
    """独占创建锁文件；被占则轮询等待。返回文件句柄或 None(超时)。陈旧锁(>10min)自动接管。"""
    deadline = time.time() + max(0.0, wait_sec)
    while True:
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            return fd
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(_LOCK_PATH)
                if age > 600:  # 陈旧锁（持锁进程可能已崩）→ 清掉重抢
                    os.remove(_LOCK_PATH)
                    continue
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(1.0)


def _release_lock(fd) -> None:
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.remove(_LOCK_PATH)
    except Exception:
        pass


def _upload_image(path: str) -> str:
    """把基准脸传到 ComfyUI 的 input 目录（POST /upload/image, multipart）。

    服务器端文件名用内容 md5 定名 + overwrite，幂等：同一张脸重复调用不会堆文件。
    返回服务器端文件名，供 LoadImage 节点引用。
    """
    import hashlib

    with open(path, "rb") as f:
        blob = f.read()
    ext = os.path.splitext(path)[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    name = "faceref_" + hashlib.md5(blob).hexdigest()[:16] + ext

    boundary = "----comfyinfer" + uuid.uuid4().hex
    parts = []
    parts.append(("--%s\r\n"
                  "Content-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode())
    parts.append(blob)
    parts.append(("\r\n--%s\r\n"
                  "Content-Disposition: form-data; name=\"overwrite\"\r\n\r\n"
                  "true\r\n--%s--\r\n" % (boundary, boundary)).encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        COMFY_URL + "/upload/image", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read())
    return j.get("name") or name


def _parse_loras(lora_name: str, lora_weight: float) -> list:
    """把 ``lora_name``（逗号分隔多个）解析成 ``[(name, weight), ...]``。

    weight 统一用 ``lora_weight``（多 LoRA 常用同权重；要各异可分别用不同 env 调用）。
    空名/空串被剔除。
    """
    names = [n.strip() for n in str(lora_name or "").split(",") if n.strip()]
    return [(n, float(lora_weight)) for n in names]


def build_workflow(prompt: str, *, width: int, height: int, steps: int,
                   guidance: float, seed: int, ckpt: str = "",
                   face_ref_name: str = "", face_weight: float = 0.9,
                   pulid_start_at: float = 0.0, pulid_end_at: float = 1.0,
                   lora_name: str = "", lora_weight: float = 1.0,
                   lora_clip: bool = False) -> dict:
    """FLUX(fp8 all-in-one) 的 ComfyUI API 工作流（节点图）。

    ``ckpt``＝本次用的 checkpoint（空=模块默认 CKPT）。schnell 系（guidance
    蒸馏）不挂 FluxGuidance 节点——它的 guidance 已蒸进权重，挂了也无效。
    face_ref_name 非空时插入 PuLID-Flux 锁脸链（ComfyUI_PuLID_Flux_ll）：
    LoadImage(基准脸) + PulidFluxModel/InsightFace/EvaClip 三个 loader →
    ApplyPulidFlux 把脸部特征注入 FLUX 模型 → KSampler 用注入后的 model。

    ``pulid_start_at``/``pulid_end_at``＝PuLID 生效的去噪进度区间 [0,1]。
    start_at>0（如 0.12）让构图/头部姿态先在早期步自由成形、PuLID 只在中后段
    锁身份——同一张脸但姿态表情不再被基准正面照钉死（治"千篇一律"）。

    ``lora_name``（逗号分隔可多个）＝挂在基座模型上的 LoRA：训好的**角色 LoRA**
    把身份焙进权重（可把 face_weight 调很低甚至不给 face_ref），**写实 LoRA**修
    蜡感皮肤。串联顺序＝声明顺序，都接在 PuLID **之前**（PuLID 注入 LoRA 后的模型）。
    ``lora_clip=False``＝LoraLoaderModelOnly（只改 UNet，角色 LoRA 标准、最稳）；
    ``True``＝全 LoraLoader（model+clip），含文本编码器训练的写实/风格 LoRA 需要，
    文本编码节点改用经 LoRA 的 clip。
    """
    _ckpt = ckpt or CKPT
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": _ckpt}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0],
                         "seed": seed, "steps": steps, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "aitr_gen"}},
    }
    # 基座模型出口：默认 checkpoint 的 model；挂 LoRA 则串联，出口改到最后一个 LoRA
    # 节点（PuLID / KSampler 都用这个"经 LoRA 的模型"）。lora_clip 时同步串 clip 分量。
    model_src = ["4", 0]
    clip_src = ["4", 1]
    loras = _parse_loras(lora_name, lora_weight)
    _nid = 40
    for _nm, _w in loras:
        if lora_clip:
            wf[str(_nid)] = {"class_type": "LoraLoader",
                             "inputs": {"model": model_src, "clip": clip_src,
                                        "lora_name": _nm, "strength_model": _w,
                                        "strength_clip": _w}}
            clip_src = [str(_nid), 1]
        else:
            wf[str(_nid)] = {"class_type": "LoraLoaderModelOnly",
                             "inputs": {"model": model_src, "lora_name": _nm,
                                        "strength_model": _w}}
        model_src = [str(_nid), 0]
        _nid += 1
    wf["3"]["inputs"]["model"] = model_src
    if lora_clip and loras:
        # 文本编码用经 LoRA 的 clip（含 TE 的 LoRA 才有意义；ModelOnly 分支不改 clip）。
        wf["6"]["inputs"]["clip"] = clip_src
        wf["7"]["inputs"]["clip"] = clip_src
    if not is_schnell(_ckpt):
        wf["10"] = {"class_type": "FluxGuidance",
                    "inputs": {"guidance": guidance, "conditioning": ["6", 0]}}
        wf["3"]["inputs"]["positive"] = ["10", 0]
    if face_ref_name:
        wf.update({
            "20": {"class_type": "LoadImage",
                   "inputs": {"image": face_ref_name}},
            "21": {"class_type": "PulidFluxModelLoader",
                   "inputs": {"pulid_file": PULID_FILE}},
            "22": {"class_type": "PulidFluxInsightFaceLoader",
                   "inputs": {"provider": "CUDA"}},
            "23": {"class_type": "PulidFluxEvaClipLoader", "inputs": {}},
            "24": {"class_type": "ApplyPulidFlux",
                   "inputs": {"model": model_src, "pulid_flux": ["21", 0],
                              "eva_clip": ["23", 0], "face_analysis": ["22", 0],
                              "image": ["20", 0], "weight": face_weight,
                              "start_at": pulid_start_at, "end_at": pulid_end_at}},
        })
        wf["3"]["inputs"]["model"] = ["24", 0]
    return wf


_CKPT_CACHE: list = []


def available_ckpts() -> list:
    """查服务端 CheckpointLoaderSimple 可选的 ckpt 文件名列表（进程内缓存）。查不到返回 []。"""
    global _CKPT_CACHE
    if _CKPT_CACHE:
        return _CKPT_CACHE
    try:
        with urllib.request.urlopen(COMFY_URL + "/object_info/CheckpointLoaderSimple",
                                    timeout=10) as r:
            info = json.loads(r.read())
        node = info.get("CheckpointLoaderSimple", {})
        lst = (node.get("input", {}).get("required", {}).get("ckpt_name") or [[]])[0]
        _CKPT_CACHE = [str(x) for x in lst] if isinstance(lst, list) else []
    except Exception:
        _CKPT_CACHE = []
    return _CKPT_CACHE


def resolve_ckpt(preferred: str, fallback: str = "") -> str:
    """选定 checkpoint：``preferred`` 在服务端可用则用它；否则回落 ``fallback`` →
    服务端第一个可用 → ``preferred`` 原样（探测失败时不拦，交由提交报错）。

    治 2026-07-14 现网事故：comfy_infer 无脸图默认 schnell(可商用)，但 .176 只装了
    dev → 无 face_ref 的生成全 400。有 schnell 就用（尊重商用合规），没有就回落 dev，
    不再硬挂。
    """
    avail = available_ckpts()
    if not avail:
        return preferred
    if preferred in avail:
        return preferred
    if fallback and fallback in avail:
        _log("checkpoint '%s' 不可用 → 回落 '%s'" % (preferred, fallback))
        return fallback
    _log("checkpoint '%s' 不可用 → 回落服务端首个 '%s'" % (preferred, avail[0]))
    return avail[0]


def _vram_free_gb() -> float:
    """查 ComfyUI 所在卡的当前空闲显存(GB)；查不到返回 -1（视为未知、不拦）。"""
    try:
        with urllib.request.urlopen(COMFY_URL + "/system_stats", timeout=10) as r:
            j = json.loads(r.read())
        dev = (j.get("devices") or [{}])[0]
        return float(dev.get("vram_free", 0)) / (1024 ** 3)
    except Exception:
        return -1.0


def _comfy_reserved_gb() -> float:
    """ComfyUI 自己在显存里占了多少(GB)——「大模型是否已常驻」的代理指标。

    ``torch_vram_total`` = ComfyUI 进程 torch 侧 reserved。裸服务约 0.1G；
    加载完 flux 全家桶后是十几 G。查不到返回 -1（未知）。
    """
    try:
        with urllib.request.urlopen(COMFY_URL + "/system_stats", timeout=10) as r:
            j = json.loads(r.read())
        dev = (j.get("devices") or [{}])[0]
        return float(dev.get("torch_vram_total", 0)) / (1024 ** 3)
    except Exception:
        return -1.0


# 「模型已常驻」判据与活化显存下限（env 可调）。
# 8G：裸服务 0.1G、加载完 flux 十几 G，8 落在两者之间且远离两端。
WARM_RESERVED_GB = float(os.environ.get("COMFY_WARM_RESERVED_GB", "8") or 8)
# 3G：1024x1024 flux 单张出图的活化显存约 2-4G（模型本身已在显存里，不再需要
# min_free_gb 那么大的**加载**空间）。
WARM_FREE_GB = float(os.environ.get("COMFY_WARM_FREE_GB", "3") or 3)


def _free_comfy() -> None:
    """让 ComfyUI 卸载已加载模型 + 释放缓存显存（把地方让给换脸栈/给本次冷加载腾空间）。"""
    try:
        data = json.dumps({"unload_models": True, "free_memory": True}).encode()
        req = urllib.request.Request(COMFY_URL + "/free", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        time.sleep(2.0)
    except Exception:
        pass


def _ollama_url_from_comfy() -> str:
    """从 ``COMFY_URL`` 推同主机的 Ollama 端点（默认 11434）。

    ComfyUI 与聊天兜底 LLM（qwen3:30b）常驻同一块 5090 → 出图腾显存要能卸得动
    **别的进程**占的显存，而 ComfyUI 自己的 /free 只能卸自己的（README 记录的
    「显存互挤」事故根因）。同主机 Ollama 端口约定 11434。
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(COMFY_URL).hostname or "127.0.0.1"
    except Exception:
        host = "127.0.0.1"
    return "http://%s:11434" % host


def _is_vision_model(name: str) -> bool:
    """VLM（qwen2.5vl/llava/minicpm-v…）判定：出图后的 image_gate 体检要用，
    腾显存时**跳过它**（它只 ~5G，大头是 30B 聊天模型；卸了体检要绕道 140）。"""
    low = str(name or "").lower()
    return any(t in low for t in ("vl", "vision", "llava", "minicpm-v", "-v:", "clip"))


def _free_ollama(ollama_url: str) -> int:
    """卸载 Ollama 驻留的**非 VLM** 模型（keep_alive=0）腾显存，返回尝试卸载的模型数。

    元凶＝云端 LLM 抖动时被拉进 5090 的本地兜底 qwen3:30b（16G, keep_alive 30m）。
    查 ``/api/ps`` 拿当前加载模型逐个 keep_alive=0 卸（不硬编模型名，兼容改名/多模型）；
    保留 VLM（体检用）。全程软失败——腾不动就腾不动，出图自身失败会回落文字。
    keep_alive=0 只让模型在本请求后过期，不中断正在进行的对话生成。
    """
    base = str(ollama_url or "").rstrip("/")
    if not base:
        return 0
    try:
        with urllib.request.urlopen(base + "/api/ps", timeout=8) as r:
            models = (json.loads(r.read()) or {}).get("models") or []
    except Exception:
        return 0
    n = 0
    for m in models:
        name = str(m.get("model") or m.get("name") or "")
        if not name or _is_vision_model(name):
            continue
        try:
            data = json.dumps({"model": name, "keep_alive": 0}).encode()
            req = urllib.request.Request(base + "/api/generate", data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            _log("已请求卸载 Ollama 模型 %s（keep_alive=0）" % name)
            n += 1
        except Exception:
            pass
    if n:
        time.sleep(2.5)  # 给 Ollama 释放显存的时间
    return n


def ensure_vram(min_free_gb: float, ollama_url: str = "") -> float:
    """显存闸门：空闲不足 min_free_gb 时先让 ComfyUI /free 腾一次；仍不足且给了
    ``ollama_url`` → 再卸同主机 Ollama 驻留的非 VLM 模型（腾聊天兜底 30B 占的显存），
    然后再查。

    返回最终 vram_free（GB）。调用方据此决定出图或回落。-1=查不到(放行)。

    ⚠ **热加载优先**（2026-08-28 老板点名「要让模型一直热加载，不能等到用的时候
    再加载」时定位到的自毁循环）：``min_free_gb`` 描述的是「把模型**装进**显存需要
    多大空位」，模型**已经在**显存里时它就不适用了——而旧实现无条件先 ``/free``，
    等于把马上要用的那份卸掉再从磁盘重载（实测冷加载 40-60s，热出图几秒）。同卡
    Ollama 的翻译/嵌入模型会周期性把空闲显存压回闸门线以下，于是每次出图都重演
    一遍：**热缓存的头号杀手是我们自己**。故先判常驻：已常驻且活化显存够 → 直接
    放行，一个字节都不腾；活化不够也只卸**别人**（Ollama，秒级可重载），绝不卸自己。
    """
    free = _vram_free_gb()
    if free < 0:
        return free  # 查不到 → 不拦（交给出图本身，失败会回落）
    if free < min_free_gb:
        reserved = _comfy_reserved_gb()
        if reserved >= WARM_RESERVED_GB:
            if free >= WARM_FREE_GB:
                _log("模型已驻留显存 %.1fG、空闲 %.1fG 够出图 → 跳过腾挪直接用(热)"
                     % (reserved, free))
                return max(free, min_free_gb)   # 闸门放行：无需再腾
            _log("模型已驻留 %.1fG 但空闲仅 %.1fG，只卸 Ollama 腾活化空间(不卸自己)"
                 % (reserved, free))
            if ollama_url and _free_ollama(ollama_url):
                free = _vram_free_gb()
                _log("卸 Ollama 后 free=%.1fG" % free)
            return max(free, min_free_gb) if free >= WARM_FREE_GB else free
    if free < min_free_gb:
        _log("显存不足 free=%.1fG < %.1fG，请求 ComfyUI 卸载腾显存…" % (free, min_free_gb))
        _free_comfy()
        free = _vram_free_gb()
        _log("ComfyUI 腾后 free=%.1fG" % free)
        # 仍不足 → 卸 Ollama 驻留模型（元凶：云抖动被拉进同卡的 qwen3:30b）再腾一次。
        if 0 <= free < min_free_gb and ollama_url:
            _log("仍不足，尝试卸载 Ollama 驻留模型腾显存 @ %s" % ollama_url)
            if _free_ollama(ollama_url):
                _free_comfy()  # Ollama 释放后再让 ComfyUI 整理一次碎片
                free = _vram_free_gb()
                _log("卸 Ollama 后 free=%.1fG" % free)
    return free


def classify_submit_rejection(body: str) -> str:
    """把 ``/prompt`` 被拒（400 node_errors）归类：**模型文件缺失** vs 其它校验失败。

    ComfyUI 校验器对「loader 的文件名不在服务端可选列表」输出
    ``Value not in list: ckpt_name: 'xx' not in [...]``——2026-08-22 实锤事故形态是
    ``not in []``（模型目录被整树清空，列表为空）。这类错误坐席自己救不了，
    必须与「参数非法」区分开，前端才能给对的建议（联系运维 vs 换描述重试）。
    """
    low = str(body or "").lower()
    if "not in list" in low or "not in []" in low or "value not in" in low:
        return "model_missing"
    return "submit_rejected"


def classify_failure(exc: BaseException) -> str:
    """终局异常 → 机器可读错误码（``ERR_CODE=`` 行的单一事实源）。"""
    if isinstance(exc, TimeoutError):
        return "gen_timeout"
    msg = str(exc)
    if isinstance(exc, RuntimeError) and "提交被拒" in msg:
        return classify_submit_rejection(msg)
    low = msg.lower()
    if isinstance(exc, urllib.error.URLError) or "urlopen error" in low \
            or "connection refused" in low or "10061" in low:
        return "server_unreachable"
    return "exec_error"


def _post_prompt(workflow: dict, client_id: str) -> str:
    data = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(COMFY_URL + "/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["prompt_id"]
    except urllib.error.HTTPError as e:
        # 400 多为 node 校验失败（缺模型/参数非法）——把服务端 node_errors 透出，
        # 否则调用方只见 "HTTP Error 400" 无从排障（2026-07-14 实测踩坑）。
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise RuntimeError("prompt 提交被拒 HTTP %s: %s" % (e.code, body[:800]))


def _wait_history(prompt_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    COMFY_URL + "/history/" + prompt_id, timeout=15) as r:
                hist = json.loads(r.read())
            if prompt_id in hist:
                return hist[prompt_id]
        except Exception:
            pass
        time.sleep(1.5)
    raise TimeoutError("等待出图超时 %.0fs" % timeout)


def _download_first_image(entry: dict, out_path: str) -> bool:
    outputs = entry.get("outputs") or {}
    for _node, data in outputs.items():
        for img in (data.get("images") or []):
            params = urllib.parse.urlencode({
                "filename": img.get("filename", ""),
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output")})
            with urllib.request.urlopen(
                    COMFY_URL + "/view?" + params, timeout=60) as r:
                blob = r.read()
            if blob:
                os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".",
                            exist_ok=True)
                with open(out_path, "wb") as f:
                    f.write(blob)
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="", help="ComfyUI 基址；覆盖 COMFY_URL 环境变量（跨机调用用）")
    ap.add_argument("--engine", default="flux", choices=["flux", "qwen_edit", "z_image"],
                    help="出图引擎（默认 flux=现役 FLUX+PuLID，零行为变更）；"
                         "qwen_edit=Qwen-Image-Edit-2511 参考图一致性；z_image=Z-Image-Turbo 速度档。"
                         "非 flux 引擎需 ComfyUI 侧先装模型（未装=提交 400，调用方回落）")
    ap.add_argument("--workflow-template", default="",
                    help="ComfyUI API 格式工作流 JSON 路径（引擎无关的稳妥覆写）：占位符 "
                         "%PROMPT%/%SEED%/%WIDTH%/%HEIGHT%/%STEPS%/%REF_IMAGE%/%OUT_PREFIX%。"
                         "给了就用它、忽略内置引擎图——运维在装 ComfyUI 里导出的真实工作流最可靠")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=0,
                    help="采样步数；0=按模型自适应(schnell=4, dev=20)")
    ap.add_argument("--guidance", type=float, default=3.5)
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--ckpt", default=CKPT,
                    help="锁脸/默认 checkpoint（默认 env COMFY_CKPT 或 flux1-dev-fp8）")
    ap.add_argument("--ckpt-noface", default=CKPT_NOFACE,
                    help="无脸图(--face-ref 空)用的可商用 checkpoint"
                         "（默认 flux1-schnell-fp8；空串=不路由全走 --ckpt）")
    ap.add_argument("--face-ref", default=os.environ.get("COMFY_FACE_REF", ""),
                    help="基准脸图片路径；给了就走 PuLID-Flux 锁脸(人物自拍必带，保证同一张脸)")
    ap.add_argument("--face-weight", type=float, default=PULID_WEIGHT,
                    help="锁脸强度 0~1.2；越大越像基准脸，太大姿势/表情会僵(env COMFY_FACE_WEIGHT)")
    ap.add_argument("--pulid-start-at", type=float, default=PULID_START_AT,
                    help="PuLID 生效起点(去噪进度 0~1)；>0(如 0.12)让姿态/表情先成形再锁脸，"
                         "治头位置表情千篇一律(env COMFY_PULID_START_AT)")
    ap.add_argument("--pulid-end-at", type=float, default=PULID_END_AT,
                    help="PuLID 生效终点(去噪进度 0~1；默认 1.0=全程锁到底，env COMFY_PULID_END_AT)")
    ap.add_argument("--lora", default=LORA_NAME,
                    help="角色/写实 LoRA 文件名(ComfyUI models/loras 下)；逗号分隔可多个串联"
                         "(env COMFY_LORA)。角色 LoRA 训好后可把 --face-weight 调低甚至去掉 --face-ref")
    ap.add_argument("--lora-weight", type=float, default=LORA_WEIGHT,
                    help="LoRA 强度(默认 1.0；角色 LoRA 常 0.8~1.0，写实 LoRA 0.3~0.6，env COMFY_LORA_WEIGHT)")
    ap.add_argument("--lora-clip", action="store_true", default=LORA_CLIP,
                    help="用全 LoraLoader(model+clip) 而非仅 UNet；含文本编码器训练的写实/风格 "
                         "LoRA 需要(env COMFY_LORA_CLIP=1)")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--min-free-gb", type=float, default=14.0,
                    help="出图前要求的最小空闲显存(GB)；不足先让 ComfyUI 卸载，仍不足则放弃回落")
    ap.add_argument("--ollama-url", default=os.environ.get("COMFY_OLLAMA_URL", "__auto__"),
                    help="显存不足且 ComfyUI 自卸后仍不够时，卸此 Ollama 端点的非 VLM 驻留模型"
                         "腾显存（元凶=同卡的兜底 qwen3:30b）。默认 __auto__=同 --url 主机的 11434；"
                         "空串=关闭该自愈(env COMFY_OLLAMA_URL)")
    ap.add_argument("--lock-wait", type=float, default=90.0,
                    help="本机出图互斥锁最长等待秒(串行化并发出图，防峰值叠加 OOM)")
    ap.add_argument("--free-after", action="store_true",
                    help="出图后让 ComfyUI 卸载模型归还显存给换脸栈(换脸吃紧时开；代价=下次出图冷加载慢)")
    args = ap.parse_args()

    if args.url:
        global COMFY_URL
        COMFY_URL = args.url.rstrip("/")

    # ① 本机互斥锁：同一时刻只放一个出图，防两个请求峰值叠加把 5090 撑爆。
    # 失败路径统一补一行 ERR_CODE=<code>（stderr 末尾，机器可读）——调用方
    # （SelfieProvider 摘录尾部 → image_gen 路由分类 → 前端人话+建议）据此
    # 确定性归因，不再靠正则猜中文日志（2026-08-22 错误面收口）。
    lock = _acquire_lock(args.lock_wait)
    if lock is None:
        _log("获取出图锁超时，放弃(回落)")
        _log("ERR_CODE=lock_busy")
        return 4
    try:
        # ② 显存闸门：不足先让 ComfyUI /free 腾；仍不足卸同卡 Ollama 兜底模型再腾；
        #    最终仍不足 → 退出码3，SelfieProvider 回落相册/文字。
        _ollama = args.ollama_url
        if _ollama == "__auto__":
            _ollama = _ollama_url_from_comfy()
        free = ensure_vram(args.min_free_gb, ollama_url=_ollama)
        if 0 <= free < args.min_free_gb:
            _log("显存仍不足 free=%.1fG < %.1fG，放弃出图(回落，不 OOM 换脸栈)" % (free, args.min_free_gb))
            _log("ERR_CODE=vram_insufficient")
            return 3

        seed = args.seed if args.seed >= 0 else uuid.uuid4().int % (2**31)
        face_ref_name = ""
        if args.face_ref:
            if not os.path.isfile(args.face_ref):
                _log("基准脸不存在: %s" % args.face_ref)
                _log("ERR_CODE=face_ref_missing")
                return 2
            try:
                face_ref_name = _upload_image(args.face_ref)
                _log("face_ref 已上传: %s" % face_ref_name)
            except Exception as e:
                _log("基准脸上传失败: %s" % e)
                _log("ERR_CODE=face_ref_upload_failed")
                return 2
        # ── 工作流构造：模板覆写 > 引擎分支 > 现役 FLUX（默认，零行为变更）──────────
        _engine = str(getattr(args, "engine", "flux") or "flux").lower()
        _tpl_path = str(getattr(args, "workflow_template", "") or "").strip()
        if _tpl_path:
            # 引擎无关的稳妥路径：运维在装 ComfyUI 导出的真实工作流（逐字节对齐）。
            if not os.path.isfile(_tpl_path):
                _log("工作流模板不存在: %s" % _tpl_path)
                _log("ERR_CODE=template_error")
                return 2
            try:
                with open(_tpl_path, "r", encoding="utf-8") as _tf:
                    _raw = _tf.read()
                _steps_tpl = args.steps if args.steps > 0 else 0
                wf = _apply_workflow_template(
                    _raw, prompt=args.prompt, seed=seed, width=args.width,
                    height=args.height, steps=_steps_tpl, ref_image_name=face_ref_name)
                _log("工作流走模板 %s engine=%s ref=%s"
                     % (os.path.basename(_tpl_path), _engine, face_ref_name or "-"))
            except Exception as e:
                _log("模板解析失败: %s" % e)
                _log("ERR_CODE=template_error")
                return 2
        elif _engine == "qwen_edit":
            steps = args.steps
            wf = build_qwen_edit_workflow(
                args.prompt, width=args.width, height=args.height, steps=steps,
                seed=seed, ref_image_name=face_ref_name)
            _log("engine=qwen_edit ref=%s（参考图一致性；需 ComfyUI 装 Qwen-Image-Edit-2511）"
                 % (face_ref_name or "-"))
        elif _engine == "z_image":
            steps = args.steps
            wf = build_zimage_workflow(
                args.prompt, width=args.width, height=args.height, steps=steps, seed=seed)
            _log("engine=z_image（速度档；需 ComfyUI 装 Z-Image-Turbo）")
        else:
            # 现役 FLUX+PuLID（默认）。模型路由（商用合规）：无脸图 → schnell(Apache 2.0)；
            # 锁脸 → dev(PuLID 训练基座)。按服务端可用列表收敛（治「schnell 未装 → 400」）。
            _want = args.ckpt if (face_ref_name or not args.ckpt_noface) else args.ckpt_noface
            ckpt = resolve_ckpt(_want, fallback=args.ckpt)
            steps = args.steps if args.steps > 0 else (4 if is_schnell(ckpt) else 20)
            wf = build_workflow(args.prompt, width=args.width, height=args.height,
                                steps=steps, guidance=args.guidance, seed=seed,
                                ckpt=ckpt, face_ref_name=face_ref_name,
                                face_weight=args.face_weight,
                                pulid_start_at=args.pulid_start_at,
                                pulid_end_at=args.pulid_end_at,
                                lora_name=args.lora, lora_weight=args.lora_weight,
                                lora_clip=args.lora_clip)
        t0 = time.time()
        try:
            cid = uuid.uuid4().hex
            pid = _post_prompt(wf, cid)
            _pulid = ("" if not face_ref_name else
                      " pulid(w=%.2f,%.2f-%.2f)" % (args.face_weight,
                                                    args.pulid_start_at,
                                                    args.pulid_end_at))
            _lora = (" lora=%s@%.2f" % (args.lora, args.lora_weight)
                     if args.lora else "")
            # ckpt/steps 只在 flux 分支定义；非 flux 引擎按引擎名记（模板/qwen/z_image）。
            _ckpt_tag = locals().get("ckpt", _engine)
            _steps_tag = locals().get("steps", args.steps)
            _log("submitted prompt_id=%s seed=%d free=%.1fG face=%s engine=%s model=%s steps=%s%s%s"
                 % (pid, seed, free, face_ref_name or "-", _engine, _ckpt_tag,
                    _steps_tag, _pulid, _lora))
            entry = _wait_history(pid, args.timeout)
            if not _download_first_image(entry, args.out):
                st = json.dumps(entry.get("status") or {}, ensure_ascii=False)
                _log("无输出图 status=%s" % st[:2000])
                _log("ERR_CODE=no_output")
                return 3
        except Exception as e:
            _log("失败: %s" % e)
            _log("ERR_CODE=%s" % classify_failure(e))
            return 2
        _log("OK %s (%.1fs)" % (args.out, time.time() - t0))
        if args.free_after:
            _free_comfy()
            _log("已归还显存给换脸栈")
        return 0
    finally:
        _release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
