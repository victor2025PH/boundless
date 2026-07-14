# -*- coding: utf-8 -*-
"""FitDiT 试衣后端包装（2026-07-08 试衣质量专项）。

选型依据（tools/tryon_candidate_probe.py + 实测）：
  · FitDiT = SD3-DiT 双塔(garm/vton)，VITON-HD/CVDD 质量第一梯队（与 Leffa 并列，
    IEEE 2025 评测），预处理全 onnx(DWPose+humanparsing)——无 detectron2，Windows 可装；
  · CatVTON 轻但 AutoMasker 依赖 detectron2（Windows 编译阻断）；OOTD 质量低一档；
  · 权重 8.1GB 已落 C:\\models\\FitDiT（含 dwpose/humanparsing onnx，自包含）；
  · 宿主 env=fitdit（克隆自 musethepeak：diffusers 0.38 + torch 2.11+cu128 + ort-gpu），
    与直播链 musethepeak(lipsync) 进程隔离，未来 pip 调整互不影响。

显存策略：默认 FITDIT_OFFLOAD=1 (model_cpu_offload，峰值 <6G ——直播中 5090 剩
  ~13G 也够)；空闲机器可 FITDIT_OFFLOAD=0 全驻留(~14G, 4.5s/张@1024)。
授权注意：FitDiT 权重 CC BY-NC-SA（非商用）。商用需走腾讯云版本。

对外只暴露 FitDiTWrapper：
    wrapper = FitDiTWrapper(model_root, device="cuda:0")
    result_pil = wrapper.tryon(person_pil, cloth_pil, cloth_type="upper")
"""
import math
import os
import random
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image

FITDIT_CODE_DIR = os.environ.get("FITDIT_CODE_DIR", r"C:\FitDiT")

_CATEGORY = {"upper": "Upper-body", "lower": "Lower-body", "full": "Dresses",
             "dress": "Dresses", "dresses": "Dresses",
             "upper-body": "Upper-body", "lower-body": "Lower-body"}


def _pad_and_resize(im: Image.Image, new_width: int, new_height: int,
                    pad_color=(255, 255, 255), mode=Image.LANCZOS):
    old_width, old_height = im.size
    ratio_w = new_width / old_width
    ratio_h = new_height / old_height
    if ratio_w < ratio_h:
        new_size = (new_width, round(old_height * ratio_w))
    else:
        new_size = (round(old_width * ratio_h), new_height)
    im_resized = im.resize(new_size, mode)
    pad_w = math.ceil((new_width - im_resized.width) / 2)
    pad_h = math.ceil((new_height - im_resized.height) / 2)
    new_im = Image.new("RGB", (new_width, new_height), pad_color)
    new_im.paste(im_resized, (pad_w, pad_h))
    return new_im, pad_w, pad_h


def _unpad_and_resize(padded_im: Image.Image, pad_w: int, pad_h: int,
                      original_width: int, original_height: int):
    width, height = padded_im.size
    cropped = padded_im.crop((pad_w, pad_h, width - pad_w, height - pad_h))
    return cropped.resize((original_width, original_height), Image.LANCZOS)


def _resize_image(img: Image.Image, target_size: int = 768):
    width, height = img.size
    scale = target_size / min(width, height)
    return img.resize((int(round(width * scale)), int(round(height * scale))), Image.LANCZOS)


class FitDiTWrapper:
    """封装官方 FitDiTGenerator 的两步(mask→tryon)为一步调用，输入输出均为 PIL。"""

    def __init__(self, model_root: str, device: str = "cuda:0",
                 offload: bool | None = None, fp16: bool | None = None):
        sys.path.insert(0, FITDIT_CODE_DIR)
        from preprocess.humanparsing.run_parsing import Parsing
        from preprocess.dwpose import DWposeDetector
        from transformers import CLIPVisionModelWithProjection
        from src.pose_guider import PoseGuider
        from src.utils_mask import get_mask_location
        from src.pipeline_stable_diffusion_3_tryon import StableDiffusion3TryOnPipeline
        from src.transformer_sd3_garm import SD3Transformer2DModel as GarmT
        from src.transformer_sd3_vton import SD3Transformer2DModel as VtonT

        self._get_mask_location = get_mask_location
        if offload is None:
            offload = os.environ.get("FITDIT_OFFLOAD", "1") == "1"
        if fp16 is None:  # 5090 bf16 原生支持；fp16 仅为老卡兜底
            fp16 = os.environ.get("FITDIT_FP16", "0") == "1"
        dtype = torch.float16 if fp16 else torch.bfloat16

        garm = GarmT.from_pretrained(os.path.join(model_root, "transformer_garm"), torch_dtype=dtype)
        vton = VtonT.from_pretrained(os.path.join(model_root, "transformer_vton"), torch_dtype=dtype)
        pose_guider = PoseGuider(conditioning_embedding_channels=1536, conditioning_channels=3,
                                 block_out_channels=(32, 64, 256, 512))
        pose_guider.load_state_dict(torch.load(
            os.path.join(model_root, "pose_guider", "diffusion_pytorch_model.bin"),
            map_location="cpu", weights_only=True))
        enc_l = CLIPVisionModelWithProjection.from_pretrained(
            os.environ.get("FITDIT_CLIP_L", "openai/clip-vit-large-patch14"), torch_dtype=dtype)
        enc_g = CLIPVisionModelWithProjection.from_pretrained(
            os.environ.get("FITDIT_CLIP_G", "laion/CLIP-ViT-bigG-14-laion2B-39B-b160k"),
            torch_dtype=dtype)
        pose_guider.to(device=device, dtype=dtype)
        enc_l.to(device); enc_g.to(device)
        self.pipeline = StableDiffusion3TryOnPipeline.from_pretrained(
            model_root, torch_dtype=dtype, transformer_garm=garm, transformer_vton=vton,
            pose_guider=pose_guider, image_encoder_large=enc_l, image_encoder_bigG=enc_g)
        if offload:
            self.pipeline.enable_model_cpu_offload(device=device)
            prep_dev = "cpu"
        else:
            self.pipeline.to(device)
            prep_dev = device
        # 预处理器 onnx：offload 时放 CPU（省显存），常驻时走 CUDA EP
        self.dwprocessor = DWposeDetector(model_root=model_root, device=prep_dev)
        self.parsing_model = Parsing(model_root=model_root, device=prep_dev)
        self.offload = offload

    # ── 两步合一 ────────────────────────────────────────────────────
    @torch.inference_mode()
    def _make_mask(self, vton_img: Image.Image, category: str):
        vton_det = _resize_image(vton_img)
        pose_image, keypoints, _, candidate = self.dwprocessor(np.array(vton_det)[:, :, ::-1])
        candidate[candidate < 0] = 0
        candidate = candidate[0]
        candidate[:, 0] *= vton_det.width
        candidate[:, 1] *= vton_det.height
        pose_pil = Image.fromarray(pose_image[:, :, ::-1])
        model_parse, _ = self.parsing_model(vton_det)
        mask, _ = self._get_mask_location(category, model_parse, candidate,
                                          model_parse.width, model_parse.height, 0, 0, 0, 0)
        return mask.resize(vton_img.size).convert("L"), pose_pil

    @torch.inference_mode()
    def tryon(self, person: Image.Image, cloth: Image.Image, cloth_type: str = "upper",
              steps: int = 0, guidance: float = 0.0, seed: int = -1,
              resolution: str = "") -> Image.Image:
        category = _CATEGORY.get((cloth_type or "upper").lower(), "Upper-body")
        steps = steps or int(os.environ.get("FITDIT_STEPS", "20"))
        guidance = guidance or float(os.environ.get("FITDIT_GUIDANCE", "2.0"))
        resolution = resolution or os.environ.get("FITDIT_RESOLUTION", "768x1024")
        if resolution not in ("768x1024", "1152x1536", "1536x2048"):
            resolution = "768x1024"
        new_w, new_h = (int(x) for x in resolution.split("x"))

        person = person.convert("RGB")
        cloth = cloth.convert("RGB")
        mask, pose_pil = self._make_mask(person, category)

        orig_size = person.size
        cloth_p, _, _ = _pad_and_resize(cloth, new_w, new_h)
        person_p, pad_w, pad_h = _pad_and_resize(person, new_w, new_h)
        mask_p, _, _ = _pad_and_resize(mask, new_w, new_h, pad_color=(0, 0, 0))
        mask_p = mask_p.convert("L")
        pose_p, _, _ = _pad_and_resize(pose_pil, new_w, new_h, pad_color=(0, 0, 0))
        if seed == -1:
            seed = random.randint(0, 2147483647)

        res = self.pipeline(
            height=new_h, width=new_w,
            guidance_scale=guidance, num_inference_steps=steps,
            generator=torch.Generator("cpu").manual_seed(seed),
            cloth_image=cloth_p, model_image=person_p,
            mask=mask_p, pose_image=pose_p,
            num_images_per_prompt=1).images
        return _unpad_and_resize(res[0], pad_w, pad_h, orig_size[0], orig_size[1])
