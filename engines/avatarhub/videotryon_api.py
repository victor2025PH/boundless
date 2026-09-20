# -*- coding: utf-8 -*-
"""动态试衣 API（视频虚拟试衣，CatV2TON 512 档）— 阶段15 产品化落地
端口: 8006
用法: POST /video_tryon 提交作业 → GET /job/{id} 轮询进度 → GET /job/{id}/result 取片

设计要点（与静态试衣 8002 的差异）：
  · 6 分钟级任务 → 作业模型（单工位串行 + 进度百分比），不做同步长阻塞。
  · 峰值显存 16.3G（阶段14 实测）→ 提交时显存闸拒单，腾挪由 Hub 编排。
  · 遮罩/姿态用 CatV2TON 官方 AutoMasker（DensePose+SCHP，带时序平滑）——
    阶段14 用 FitDiT 遮罩偏大把裙子织进衣服，官方遮罩与训练分布一致。
  · 模型懒加载（启动秒起），空闲自动整体卸载（默认 10min，用得少不白驻留）。
运行时兼容：tools/_catv2ton_shim.py（pkg_resources/torchvision.io/RoPE 0.29 复刻）。
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
# 必须先于 torch 导入：48 帧预处理会把缓存分配器打碎，解码期要大块连续显存，
# 碎片化直接逼出 WDDM 共享内存回退（首次冒烟实测：free→0、进程被拖死）。
# torch 2.6+ 改名 PYTORCH_ALLOC_CONF（旧名仍认但会警告）——两个都设，确保生效。
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")

import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import app_config

# ── CatV2TON 代码路径必须排最前：它的顶层包名 data/utils 太通用，
#    否则会被本项目根目录同名目录遮蔽（data/ 是营运数据目录）。
sys.path.insert(0, str(app_config.BASE / "tools"))
sys.path.insert(0, r"C:\CatV2TON")
import _catv2ton_shim  # noqa: F401  垫片先行（详见该文件头注）

app = FastAPI(title="Video Try-On API (CatV2TON)")
import service_auth
service_auth.secure(app, name="videotryon")

import vram_gate

BASE_MODEL = Path(os.environ.get("CATV2TON_BASE", r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP"))
FT_MODEL = Path(os.environ.get("CATV2TON_FT", r"D:\models_catv2ton\CatV2TON\512-64K"))
DENSEPOSE_DIR = Path(os.environ.get("CATV2TON_DENSEPOSE", r"D:\models_catv2ton\CatVTON\DensePose"))
SCHP_DIR = Path(os.environ.get("CATV2TON_SCHP", r"D:\models_catv2ton\CatVTON\SCHP"))
CLOTH_DIR = app_config.BASE / "clothes"
OUT_DIR = app_config.BASE / "data" / "video_tryon"
OUT_DIR.mkdir(parents=True, exist_ok=True)

W, H = 384, 512                      # 512 档训练分辨率（3:4）
# 峰值账本（七测分相位实测：encode 5.9 / denoise 3.6 / decode 12.0）：
# 解码期主导。八测再叠「decode 窗口 6→4 潜帧 + 解码期 Transformer 下卡(-3G)」，
# 预期峰值 ~9-12G → 冷单需 物理空闲 ≥ 峰值+预留 ≈ 15G；暖单管线驻留 ~4G
# 记在本进程名下 → 增量 ≈ 11G。动态配额兜底：估少了也只是作业 OOM 报错，
# 不会冻机（六测验证过：配额撞顶 → 干净的 CUDA OOM → 作业转 error）。
MIN_FREE_COLD = float(os.environ.get("VIDEOTRYON_MIN_FREE_GB", "15"))
MIN_FREE_WARM = float(os.environ.get("VIDEOTRYON_MIN_FREE_WARM_GB", "11"))
IDLE_UNLOAD_MIN = float(os.environ.get("VIDEOTRYON_IDLE_UNLOAD_MIN", "10"))
MAX_FRAMES = int(os.environ.get("VIDEOTRYON_MAX_FRAMES", "96"))         # 4s@24fps 封顶
# 防冻机硬顶（阶段15 事故复盘）：峰值一旦越过物理显存，WDDM 不报 OOM 而是
# 溢出到共享内存 → 显示驱动被饿死 → 整机死机重启（实测发生一次）。
# 对策：每单开跑前按「本进程已驻留 + 物理空闲 - 预留」动态设 torch 进程配额，
# 越界直接抛 CUDA OOM（作业报错、可恢复），绝不与桌面/直播抢物理页。
VRAM_RESERVE_GB = float(os.environ.get("VIDEOTRYON_RESERVE_GB", "3"))


def _set_dynamic_vram_cap():
    if not torch.cuda.is_available():
        return
    total = torch.cuda.get_device_properties(0).total_memory / 2**30
    mine = torch.cuda.memory_reserved() / 2**30
    free_phys = vram_gate.free_gb()
    if free_phys == float("inf"):                      # 探测失败 → 静态兜底
        cap = total - VRAM_RESERVE_GB
    else:
        cap = min(total - VRAM_RESERVE_GB, mine + free_phys - VRAM_RESERVE_GB)
    frac = max(0.2, min(1.0, cap / total))
    torch.cuda.set_per_process_memory_fraction(frac, 0)
    print(f"[VideoTryOn] 本单显存硬顶 {total * frac:.1f}G"
          f"（驻留 {mine:.1f}G + 物理空闲 {free_phys:.1f}G - 预留 {VRAM_RESERVE_GB:.0f}G）")

# ── 模型（管线懒加载常驻；AutoMasker 即用即卸——去噪/解码期不让它白占 2G）──
_pipe = None
_load_lock = threading.Lock()
_last_used = time.time()


def _ensure_pipe():
    """V2TONPipeline 加载（~45s）。失败抛异常由作业态兜住。"""
    global _pipe
    with _load_lock:
        if _pipe is not None:
            return
        print("[VideoTryOn] 首次作业，加载 CatV2TON 管线（~45s）...")
        t0 = time.time()
        from modules.pipeline import V2TONPipeline
        _pipe = V2TONPipeline(base_model_path=str(BASE_MODEL),
                              finetuned_model_path=str(FT_MODEL),
                              load_pose=True, torch_dtype=torch.bfloat16,
                              device="cuda")
        sizes = {n: sum(p.numel() * p.element_size() for p in m.parameters()) / 2**30
                 for n, m in (("transformer", _pipe.transformer3d),
                              ("vae", _pipe.vae), ("posenet", _pipe.posenet))}
        print(f"[VideoTryOn] 管线加载完成 {time.time() - t0:.0f}s，驻留 "
              f"{torch.cuda.memory_allocated() / 2**30:.1f}G "
              f"({' '.join(f'{k}={v:.1f}G' for k, v in sizes.items())})")


def _make_masker():
    """AutoMasker 每单现载（~5s，占 6 分钟作业可忽略），预处理完即卸。
    换来去噪/解码期多 ~2G 余量——首次冒烟就是解码期挤爆显存拖死整机。"""
    from modules.cloth_masker import AutoMasker
    return AutoMasker(densepose_ckpt=str(DENSEPOSE_DIR),
                      schp_ckpt=str(SCHP_DIR), device="cuda")


def _free_cuda():
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


def _do_unload():
    global _pipe
    was = _pipe is not None
    _pipe = None
    _free_cuda()
    return {"ok": True, "was_loaded": was, "free_gb": round(vram_gate.free_gb(), 1)}


def _idle_unload_loop():
    """空闲整体卸载：本服务用得少（出片工坊按钮级），驻留 ~5G 不值得白占。
    与发型的「显存被挤才卸」不同——这里无条件到点即还，冷启动 45s 可接受。"""
    if IDLE_UNLOAD_MIN <= 0:
        print("[VideoTryOn] 空闲自动卸载已停用")
        return
    while True:
        time.sleep(60)
        try:
            if _pipe is not None and not _JOB["busy"] \
                    and (time.time() - _last_used) / 60 >= IDLE_UNLOAD_MIN:
                r = _do_unload()
                print(f"[VideoTryOn] 空闲 {IDLE_UNLOAD_MIN:.0f}min → 自动卸载 "
                      f"(free→{r['free_gb']}G)")
        except Exception as e:
            print(f"[VideoTryOn] 自动卸载线程异常(继续): {e}")


threading.Thread(target=_idle_unload_loop, daemon=True).start()

# ── 作业状态（单工位）─────────────────────────────────────────────────────
_JOB = {"busy": False}
_jobs: dict = {}                      # id -> {state, progress, detail, ...}
_JOBS_KEEP = 20


def _job_update(jid: str, **kw):
    j = _jobs.get(jid)
    if j:
        j.update(kw)


def _n_slices(latent_frames: int, sf: int = 6, pf: int = 2) -> int:
    """复刻 video_try_on 的分片循环，算准去噪分片数（进度条用）。"""
    start, end = 0, min(sf, latent_frames)
    n = 0
    while end <= latent_frames:
        n += 1
        if end == latent_frames:
            break
        start, end = start + sf - pf, end + sf - pf
        if end > latent_frames and start < latent_frames:
            end, start = latent_frames, latent_frames - sf
    return max(n, 1)


def _read_video_34(path: str, max_frames: int):
    """读视频 → 中裁 3:4 → 缩放 384x512。返回 (frames RGB uint8 list, fps)。"""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frames = []
    while len(frames) < max_frames:
        ok, f = cap.read()
        if not ok:
            break
        h, w = f.shape[:2]
        tw = int(h * 3 / 4)
        if w > tw:                                    # 过宽 → 横向中裁
            x0 = (w - tw) // 2
            f = f[:, x0:x0 + tw]
        elif w < tw:                                  # 过窄 → 纵向中裁
            th = int(w * 4 / 3)
            y0 = max((h - th) // 2, 0)
            f = f[y0:y0 + th]
        frames.append(cv2.cvtColor(cv2.resize(f, (W, H)), cv2.COLOR_BGR2RGB))
    cap.release()
    return frames, fps


def _run_job(jid: str, req: dict):
    global _last_used
    try:
        _set_dynamic_vram_cap()                 # 防冻机：越物理界抛 OOM 不拖死整机
        _job_update(jid, state="preprocess", progress=2, detail="解析输入")
        # ① 人物视频
        vp = req.get("person_video_path") or ""
        if req.get("person_video_b64"):
            vp = str(OUT_DIR / jid / "person_in.mp4")
            Path(vp).parent.mkdir(parents=True, exist_ok=True)
            Path(vp).write_bytes(base64.b64decode(req["person_video_b64"]))
        if not vp or not Path(vp).exists():
            raise RuntimeError(f"人物视频不存在: {vp}")
        frames, fps = _read_video_34(vp, int(req.get("max_frames") or MAX_FRAMES))
        if len(frames) < 8:
            raise RuntimeError(f"人物视频过短（{len(frames)} 帧 < 8）")
        # ② 服装图
        if req.get("cloth_image"):
            b64 = req["cloth_image"]
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            arr = np.frombuffer(base64.b64decode(b64), np.uint8)
            cloth_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        else:
            name = req.get("cloth_name") or ""
            hits = [p for ext in ("jpg", "jpeg", "png", "webp")
                    for p in CLOTH_DIR.glob(f"{name}.{ext}")]
            if not hits:
                raise RuntimeError(f"服装不存在: {name}")
            cloth_bgr = cv2.imdecode(np.fromfile(str(hits[0]), np.uint8), cv2.IMREAD_COLOR)
        cloth = cv2.cvtColor(cv2.resize(cloth_bgr, (W, H)), cv2.COLOR_BGR2RGB)
        mask_type = {"upper": "upper", "lower": "lower", "full": "overall",
                     "dresses": "overall", "overall": "overall"}.get(
                        (req.get("cloth_type") or "upper").lower(), "upper")

        _ensure_pipe()
        _last_used = time.time()

        # ③ 官方 AutoMasker：逐帧遮罩+DensePose（自带手脸保护），带进度。
        #    即用即卸：预处理完立刻释放，去噪/解码期不留一兵一卒在显存里。
        from modules.cloth_masker import smooth_video_mask
        from data.utils import densepose_to_rgb
        _job_update(jid, progress=4, detail="加载遮罩模型（~5s）")
        masker = _make_masker()
        masks, poses = [], []
        t0 = time.time()
        try:
            for i, fr in enumerate(frames):
                from PIL import Image as _Img
                r = masker(_Img.fromarray(fr), mask_type=mask_type)
                masks.append(np.array(r["mask"]))
                poses.append(np.array(densepose_to_rgb(r["densepose"],
                                                       colormap=cv2.COLORMAP_VIRIDIS)))
                _job_update(jid, progress=5 + int(30 * (i + 1) / len(frames)),
                            detail=f"遮罩+姿态 {i + 1}/{len(frames)}")
        finally:
            del masker
            _free_cuda()
        mask_ms = (time.time() - t0) * 1000 / len(frames)

        # 时序平滑（官方后处理，防遮罩边缘闪烁）。
        # 契约陷阱（四次冒烟实锤，衣区纯白爆掉的根因）：smooth_video_mask 进出都是
        # 0-255 字节张量，不是 0-1——直接当 0-1 喂 VAE/重绘融合等于把遮罩放大 255 倍。
        m = torch.from_numpy(np.stack(masks)).unsqueeze(1).repeat(1, 3, 1, 1)
        m = (smooth_video_mask(m.permute(1, 0, 2, 3)).float() / 255).cpu()  # C,T,H,W → 0/1
        _job_update(jid, progress=37, detail="张量组装")

        # ④ 组装 B,C,T,H,W
        person_t = torch.from_numpy(np.stack(frames)).permute(3, 0, 1, 2)[None].float() / 255 * 2 - 1
        mask_t = m[None].float()
        pose_t = torch.from_numpy(np.stack(poses)).permute(3, 0, 1, 2)[None].float() / 255 * 2 - 1
        cloth_t = (torch.from_numpy(cloth).permute(2, 0, 1)[None, :, None].float() / 255 * 2 - 1)
        if person_t.size(2) % 4:                                     # MagViT 时间压缩 4 对齐
            pad = 4 - person_t.size(2) % 4
            person_t = torch.cat([person_t, person_t[:, :, -1:].repeat(1, 1, pad, 1, 1)], 2)
            mask_t = torch.cat([mask_t, mask_t[:, :, -1:].repeat(1, 1, pad, 1, 1)], 2)
            pose_t = torch.cat([pose_t, pose_t[:, :, -1:].repeat(1, 1, pad, 1, 1)], 2)

        # ⑤ 去噪（按分片报进度：包一层 denoising 计数）
        steps = int(req.get("steps") or 15)
        total_slices = _n_slices(person_t.size(2) // 4)
        done = {"n": 0}
        orig_denoising = _pipe.denoising

        phase_peaks: dict = {}

        def _mark(phase: str):
            """分相位显存峰值账本（还债式排查：16G 峰值到底在哪个相位）。"""
            phase_peaks[phase] = round(torch.cuda.max_memory_allocated() / 2**30, 1)
            torch.cuda.reset_peak_memory_stats()

        def _denoising_counted(*a, **kw):
            if done["n"] == 0 and "encode" not in phase_peaks:
                _mark("encode")                # 首片开跑=编码相位结束
                # 上一单解码期把 Transformer 下到 CPU（编码期它也是死重，顺带
                # 白赚余量）；去噪开跑前搬回，PCIe4 x16 实测 ~1s。
                if next(_pipe.transformer3d.parameters()).device.type != "cuda":
                    _pipe.transformer3d.to("cuda")
            out = orig_denoising(*a, **kw)
            done["n"] += 1
            # 每片结束还一次碎片：首测 OOM 实锤——去噪期攒下 3.3G「保留未分配」，
            # 解码开局要 1.4G 整块直接撞进程配额。empty_cache 毫秒级，白赚 3G 余量。
            torch.cuda.empty_cache()
            if done["n"] >= total_slices:      # 之后还有 VAE 解码（管线内部，分钟级）
                _mark("denoise")
                _job_update(jid, state="decode", progress=88,
                            detail="VAE 解码出片（约 1-3 分钟）")
            else:
                _job_update(jid, progress=45 + int(43 * done["n"] / total_slices),
                            detail=f"去噪分片 {done['n']}/{total_slices}")
            return out

        def _vae_decode_raw(z):
            """裸 VAE 解码（空间平铺由 vae 自带 use_tiling_decoder 处理）→ CPU float。"""
            z = (z / _pipe.vae.config.scaling_factor).to(_pipe.device, _pipe.weight_dtype)
            v = _pipe.vae.decode(z).sample
            return v.clamp(-1, 1).cpu().float()

        # 编码同病同治（五测 peak 仍 16G 的来源）：_slice_vae 名为 slice 实为
        # 整段编码（FIXME: Not use mini_batch 是上游自己留的注释），48+4 帧的
        # 编码器激活 ~14G 与解码对称。时间维 16 像素帧/窗分块，窗间 4 帧重叠
        # 只取后窗新 latent（MagViT 因果卷积，头部 1 潜帧受冷启动影响最深，
        # 重叠丢弃即可）；单图（服装/条件掩码 T=1）原样直过。
        orig_slice_vae = _pipe._slice_vae

        def _slice_vae_chunked(px, chunk=16, ov=8):
            t = px.shape[2] if px.ndim == 5 else 1
            if px.ndim != 5 or t <= chunk + ov or px.size(1) == 4:
                return orig_slice_vae(px)
            outs, start = [], 0
            while start < t:
                s0 = max(start - ov, 0) if start else 0
                end = min(start + chunk, t)
                z = orig_slice_vae(px[:, :, s0:end])
                torch.cuda.empty_cache()
                outs.append(z[:, :, (start - s0) // 4:])
                start = end
            return torch.cat(outs, dim=2)

        _pipe._slice_vae = _slice_vae_chunked

        def _decode_with_headroom(latents, chunk=2, ov=2):
            """时间维分块+重叠融合解码，替代管线原 decode_latents。
            账本（七测分相位实锤）：全链峰值在解码期，VAE 解码激活 ~2-3G/潜帧，
            原实现整段 13 潜帧一把过 ≈ 14.5G 激活 = 首测整机冻结的元凶；还追加
            smooth_output 整段再编解码（峰值×2）——重绘阶段遮罩外会还原源片，
            二次平滑纯属浪费，直接跳过。
            窗口策略：chunk=2 + ov=2 → 单窗 ≤4 潜帧（激活 ~9-12G）；重叠固定
            2 潜帧(=8 像素帧)线性融合，与五测 L1 0.58 的接缝质量同宽——六测
            用 ov=1 省显存，L1 恶化到 2.66 肉眼可见接缝，否决。
            叠加「解码期卸 Transformer」：去噪结束后它就是死重（bf16 权重 ~3G
            白占），挪去 CPU 内存，下一单去噪前再搬回（PCIe 秒级，见
            _denoising_counted）。"""
            _pipe.transformer3d.to("cpu")      # 解码期死重下卡（本相位只用 VAE）
            torch.cuda.empty_cache()           # 解码前清缓存，凑整块
            # 八测 OOM 复盘：配额是开单时按「当时物理空闲」定死的快照，途中腾挪
            # 到位后物理反而更宽裕（16.1G→17.9G），旧配额把自己勒死在 13G。
            # 解码是峰值相位 → 开跑前按最新物理空闲重定一次配额（3G 预留不变，
            # 防冻机语义不破：配额永远 ≤ 驻留+物理空闲-预留）。
            _set_dynamic_vram_cap()
            t = latents.shape[2]
            if t <= chunk + ov:                # 短片一把梭（峰值本来就低）
                res = _vae_decode_raw(latents)
                _mark("decode")
                return res
            out = None
            start = 0
            while start < t:
                s0 = max(start - ov, 0) if start else 0
                end = min(start + chunk, t)
                seg = _vae_decode_raw(latents[:, :, s0:end])
                torch.cuda.empty_cache()
                if out is None:
                    out = seg
                else:
                    n_ov = (start - s0) * 4    # 像素域重叠帧数（时间压缩 4x）
                    w = torch.linspace(0.0, 1.0, n_ov).view(1, 1, n_ov, 1, 1)
                    out[:, :, -n_ov:] = out[:, :, -n_ov:] * (1 - w) + seg[:, :, :n_ov] * w
                    out = torch.cat([out, seg[:, :, n_ov:]], dim=2)
                start = end
            _mark("decode")
            return out

        # CFG 顺序前向（三次冒烟后的釜底抽薪）：原 step() 把 uncond/cond 两支拼成
        # batch2 一次过 Transformer，激活峰值×2。顺序两次前向数值恒等、时间×2
        # （去噪 21s→42s，占 3 分钟作业可忽略），共卡直播机显存比时间金贵。
        orig_step = _pipe.step
        seq_steps = {"n": 0}

        def _step_seq_cfg(latents, t, inpaint_latents, pose_latents=None,
                          guidance_scale=1.0, guidance_rescale=0.0,
                          dit_added_args=None, extra_step_kwargs=None):
            seq_steps["n"] += 1
            if guidance_scale <= 1.0 or guidance_rescale > 0:
                return orig_step(latents, t, inpaint_latents, pose_latents,
                                 guidance_scale, guidance_rescale,
                                 dit_added_args, extra_step_kwargs)
            from copy import deepcopy
            p = _pipe
            bsz = latents.shape[0]
            ts = torch.tensor([t] * bsz, device=latents.device, dtype=p.weight_dtype)
            args = deepcopy(dit_added_args)
            pose_embeds = None
            if pose_latents is not None:       # 原实现同一 pose_embeds 复制两份 → 两支共用
                pose_embeds = p.posenet(pose_latents, ts, return_dict=False, **args)
            x = p.noise_scheduler.scale_model_input(latents, t)
            inp_u, inp_c = inpaint_latents.chunk(2)      # denoising 里 cat 顺序：uncond 前
            preds = []
            for inp in (inp_u, inp_c):
                n = p.transformer3d(x, ts, pose_emb=pose_embeds,
                                    inpaint_latents=inp, return_dict=False, **args)[0]
                preds.append(n.chunk(2, dim=1)[0])
            noise_pred = preds[0] + guidance_scale * (preds[1] - preds[0])
            return p.noise_scheduler.step(noise_pred, t, latents,
                                          **extra_step_kwargs, return_dict=False)[0]

        _pipe.denoising = _denoising_counted
        _pipe.decode_latents = _decode_with_headroom
        _pipe.step = _step_seq_cfg
        _job_update(jid, state="denoise", progress=40,
                    detail=f"VAE 编码底片 + 去噪 0/{total_slices}")
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        t0 = time.time()
        try:
            with torch.no_grad():
                out = _pipe.video_try_on(
                    source_video=person_t, mask_video=mask_t, condition_image=cloth_t,
                    pose_video=pose_t, num_inference_steps=steps,
                    guidance_scale=float(req.get("guidance") or 2.5),
                    slice_frames=24, pre_frames=8,
                    generator=torch.Generator(device="cuda").manual_seed(
                        int(req.get("seed") or 42)),
                    use_adacn=True)
        finally:
            # 实例属性补丁必须全部清除：残留会让下一单在补丁上再包补丁（双重包裹）
            for attr in ("denoising", "decode_latents", "step", "_slice_vae"):
                try:
                    delattr(_pipe, attr)
                except AttributeError:
                    pass
        infer_s = time.time() - t0
        peak_gb = max([torch.cuda.max_memory_allocated() / 2**30]
                      + list(phase_peaks.values()))       # 分相位后取全程最大
        torch.cuda.empty_cache()

        # ⑥ 重绘（遮罩外还原源片）+ 落盘
        _job_update(jid, state="decode", progress=96, detail="重绘+编码")
        res = out.permute(0, 4, 1, 2, 3).float().cpu()               # B,C,T,H,W
        soft = torch.nn.functional.avg_pool2d(
            mask_t.squeeze(0).permute(1, 0, 2, 3), 11, stride=1, padding=5)
        soft = soft.permute(1, 0, 2, 3).unsqueeze(0)
        final = person_t * (1 - soft) + res * soft
        final = ((final.squeeze(0).permute(1, 2, 3, 0) * 0.5 + 0.5).clamp(0, 1) * 255).byte().numpy()

        jdir = OUT_DIR / jid
        jdir.mkdir(parents=True, exist_ok=True)
        vw = cv2.VideoWriter(str(jdir / "result.mp4"),
                             cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
        for f in final[:len(frames)]:
            vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
        vw.release()
        mid = final[len(frames) // 2]
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(mid, cv2.COLOR_RGB2BGR),
                               [cv2.IMWRITE_JPEG_QUALITY, 90])
        (jdir / "preview.jpg").write_bytes(buf.tobytes())
        meta = {"frames": len(frames), "fps": round(fps, 2), "steps": steps,
                "mask_type": mask_type, "mask_ms_per_frame": round(mask_ms),
                "infer_s": round(infer_s), "peak_gb": round(peak_gb, 1),
                "phase_peaks_gb": phase_peaks, "seq_cfg_steps": seq_steps["n"],
                "s_per_frame": round(infer_s / len(frames), 2)}
        (jdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        _job_update(jid, state="done", progress=100, detail="完成",
                    result_path=str(jdir / "result.mp4"),
                    preview_path=str(jdir / "preview.jpg"), meta=meta)
        print(f"[VideoTryOn] 作业 {jid} 完成: {meta}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _job_update(jid, state="error", detail=str(e)[:300])
        print(f"[VideoTryOn] 作业 {jid} 失败: {e}")
    finally:
        globals()["_last_used"] = time.time()
        _JOB["busy"] = False


# ── 路由 ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _pipe is not None,
            "busy": _JOB["busy"], "free_gb": round(vram_gate.free_gb(), 1)}


@app.post("/video_tryon")
def submit(req: dict):
    """提交作业。req: person_video_path|person_video_b64, cloth_name|cloth_image,
    cloth_type=upper|lower|full, max_frames?, steps?, guidance?, seed?
    立即返回 {job_id}；单工位，忙时 409；显存不足 503（人话提示，由 Hub 腾挪重试）。"""
    if _JOB["busy"]:
        raise HTTPException(409, "动态试衣工位忙（单工位串行）——稍后再试")
    need = MIN_FREE_WARM if _pipe is not None else MIN_FREE_COLD
    free = vram_gate.free_gb()
    if free < need:
        raise HTTPException(503, {"error": "vram", "free_gb": round(free, 1),
                                  "need_gb": need,
                                  "msg": f"显存不足（空闲 {free:.1f}G < 需 {need}G）——"
                                         f"请先卸载发型/试衣模型再试"})
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"id": jid, "state": "queued", "progress": 0, "detail": "排队中",
                  "created": time.time()}
    while len(_jobs) > _JOBS_KEEP:                     # 只留最近 N 单
        oldest = min(_jobs, key=lambda k: _jobs[k]["created"])
        if oldest == jid:
            break
        _jobs.pop(oldest, None)
    _JOB["busy"] = True
    threading.Thread(target=_run_job, args=(jid, req), daemon=True).start()
    return {"job_id": jid}


@app.get("/job/{jid}")
def job_status(jid: str):
    j = _jobs.get(jid)
    if not j:
        raise HTTPException(404, "作业不存在（可能已被轮换清理）")
    return j


@app.get("/job/{jid}/result")
def job_result(jid: str):
    j = _jobs.get(jid)
    if not j or j.get("state") != "done":
        raise HTTPException(404, "作业未完成")
    return FileResponse(j["result_path"], media_type="video/mp4")


@app.get("/job/{jid}/preview")
def job_preview(jid: str):
    j = _jobs.get(jid)
    if not j or not j.get("preview_path"):
        raise HTTPException(404, "预览不存在")
    return FileResponse(j["preview_path"], media_type="image/jpeg")


@app.post("/unload")
def unload_endpoint():
    if _JOB["busy"]:
        raise HTTPException(409, "作业进行中，不可卸载")
    return _do_unload()


if __name__ == "__main__":
    _port = int(os.environ.get("VIDEOTRYON_PORT") or app_config.port("videotryon") or 8006)
    print(f"[VideoTryOn] 启动于 :{_port}（模型懒加载，首单 ~45s 冷启动）")
    uvicorn.run(app, host="0.0.0.0", port=_port, log_level="warning")
