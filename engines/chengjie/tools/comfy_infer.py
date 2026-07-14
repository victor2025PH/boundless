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
    """
    free = _vram_free_gb()
    if free < 0:
        return free  # 查不到 → 不拦（交给出图本身，失败会回落）
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
    lock = _acquire_lock(args.lock_wait)
    if lock is None:
        _log("获取出图锁超时，放弃(回落)")
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
            return 3

        seed = args.seed if args.seed >= 0 else uuid.uuid4().int % (2**31)
        face_ref_name = ""
        if args.face_ref:
            if not os.path.isfile(args.face_ref):
                _log("基准脸不存在: %s" % args.face_ref)
                return 2
            try:
                face_ref_name = _upload_image(args.face_ref)
                _log("face_ref 已上传: %s" % face_ref_name)
            except Exception as e:
                _log("基准脸上传失败: %s" % e)
                return 2
        # 模型路由（商用合规）：无脸图 → schnell(Apache 2.0)；锁脸 → dev(PuLID 训练基座)。
        # 但按服务端实际可用列表收敛：期望模型没装则回落已装的（治「schnell 未装 → 400」）。
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
            _log("submitted prompt_id=%s seed=%d free=%.1fG face=%s ckpt=%s steps=%d%s%s"
                 % (pid, seed, free, face_ref_name or "-", ckpt, steps, _pulid, _lora))
            entry = _wait_history(pid, args.timeout)
            if not _download_first_image(entry, args.out):
                st = json.dumps(entry.get("status") or {}, ensure_ascii=False)
                _log("无输出图 status=%s" % st[:2000])
                return 3
        except Exception as e:
            _log("失败: %s" % e)
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
