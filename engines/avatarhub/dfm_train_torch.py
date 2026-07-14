# -*- coding: utf-8 -*-
"""
DFM 每角色模型 · PyTorch 原生训练器（辨识度终极方案 · 阶段3 替代路线）
========================================================================
为什么不用 DeepFaceLab：DFL=TensorFlow，官方 TF 不支持 5090(Blackwell/sm_120)，
必须 WSL2+定制 TF 构建，折腾且训练期独占。本机 facefusion 环境已是
torch 2.12+cu128、sm_120 kernel 实测可用 → 用 PyTorch 从零训一个「per-identity 换脸自编码器」，
**原生跑在 5090、零 TF、零 WSL2**，并导出成与 DeepFaceLive 完全一致的 .dfm ONNX 契约
（in_face:0[N,H,W,3] → out_face_mask/out_celeb_face/out_celeb_face_mask），
直接被 faceswap_api 里已部署验证的 DFMSwap 适配器加载——训练侧换栈、上线侧零改动。

架构（DFL "df" 同思路）：共享 Encoder+Inter，src/dst 两个 Decoder。
  训练：src 脸走 enc→inter→dec_src 重建 src；dst 脸走 enc→inter→dec_dst 重建 dst。
  推理/导出：任意脸(in_face) → enc→inter→dec_src → 该角色脸(out_celeb) + mask。
  即换脸=把目标脸编码后用「角色专属解码器」重建 → 换的是整张脸(骨相/纹理)，非仅 embedding。

用法：
  # 训练（src=角色对齐脸集，dst=多样人脸集；都用 dfm_extract 产出的 aligned/ 或任意 WF 脸夹）
  python dfm_train_torch.py train --char 刘德华 --src <src_aligned> --dst <dst_aligned> \
         --res 224 --batch 8 --iters 100000
  # 导出 .dfm（ONNX，DFMSwap 直接吃）
  python dfm_train_torch.py export --char 刘德华 --out 刘德华.dfm
  # 自检：搭网络→前向→导出→用 onnxruntime 按 DFM 契约验证 I/O
  python dfm_train_torch.py selftest
"""
import os, sys, json, time, argparse, math, random
from pathlib import Path
import numpy as np
import cv2

BASE = Path(r"C:\模仿音色")
WORKSPACE = BASE / "dfm_workspace"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _torch():
    import torch
    return torch


# ────────────────────────── 网络 ──────────────────────────
def _build_modules(res, enc_ch=128, ae_dims=256, dec_ch=128, enc_max_ch=512, bottleneck_max=16):
    import torch
    import torch.nn as nn

    class DownBlock(nn.Module):
        def __init__(self, ci, co):
            super().__init__()
            self.c = nn.Conv2d(ci, co, 5, 2, 2)
            self.a = nn.LeakyReLU(0.1, inplace=True)
        def forward(self, x): return self.a(self.c(x))

    class UpBlock(nn.Module):
        """PixelShuffle 上采样（= DFL depth_to_space），ONNX 友好。"""
        def __init__(self, ci, co):
            super().__init__()
            self.c = nn.Conv2d(ci, co * 4, 3, 1, 1)
            self.a = nn.LeakyReLU(0.1, inplace=True)
            self.ps = nn.PixelShuffle(2)
        def forward(self, x): return self.ps(self.a(self.c(x)))

    class Encoder(nn.Module):
        """自适应下采样：一直降到 out_res≤bottleneck_max，通道翻倍但封顶 enc_max_ch。
        这样 512 也能把 flatten 前的空间尺寸压到 8×8，避免 dense 瓶颈爆到数亿参数。"""
        def __init__(self):
            super().__init__()
            blocks = []
            r, ci, co = res, 3, enc_ch
            while r > bottleneck_max:
                blocks.append(DownBlock(ci, co))
                ci = co; co = min(co * 2, enc_max_ch); r //= 2
            self.body = nn.ModuleList(blocks)
            self.out_res = r
            self.out_ch = ci
        def forward(self, x):
            for b in self.body:
                x = b(x)
            return x

    class Inter(nn.Module):
        def __init__(self, in_res, in_ch):
            super().__init__()
            self.in_res, self.in_ch = in_res, in_ch
            flat = in_res * in_res * in_ch
            self.lat = in_res  # 瓶颈后恢复到 in_res
            self.fc1 = nn.Linear(flat, ae_dims)
            self.fc2 = nn.Linear(ae_dims, in_res * in_res * ae_dims // 4)
            self.up = UpBlock(ae_dims // 4, dec_ch * 4)
            self.out_res = in_res * 2
            self.out_ch = dec_ch * 4
        def forward(self, x):
            b = x.shape[0]
            x = x.reshape(b, -1)
            x = self.fc1(x)
            x = self.fc2(x)
            x = x.reshape(b, ae_dims // 4, self.in_res, self.in_res)
            return self.up(x)

    class Decoder(nn.Module):
        def __init__(self, in_res, in_ch, target_res):
            super().__init__()
            ups = int(round(math.log2(target_res / in_res)))
            chs = in_ch
            blocks = []
            for _ in range(ups):
                blocks.append(UpBlock(chs, max(dec_ch, chs // 2)))
                chs = max(dec_ch, chs // 2)
            self.ups = nn.ModuleList(blocks)
            self.to_bgr = nn.Conv2d(chs, 3, 3, 1, 1)
            self.to_mask = nn.Conv2d(chs, 1, 3, 1, 1)
        def forward(self, x):
            for b in self.ups:
                x = b(x)
            bgr = torch.sigmoid(self.to_bgr(x))
            mask = torch.sigmoid(self.to_mask(x))
            return bgr, mask

    enc = Encoder()
    inter = Inter(enc.out_res, enc.out_ch)
    dec_src = Decoder(inter.out_res, inter.out_ch, res)
    dec_dst = Decoder(inter.out_res, inter.out_ch, res)
    return enc, inter, dec_src, dec_dst


def _load_arcface_torch(dev):
    """把 insightface buffalo_l 的 arcface(w600k_r50.onnx) 转成可微 torch 模块(冻结)。
    用于 identity loss：直接优化"换出脸像不像该角色"——即 dfm_eval 度量的同一把尺子。
    返回 (module, prep_fn) 或 (None,None)（转换失败时静默降级，不阻断训练）。"""
    import os
    try:
        import onnx
        from onnx2torch import convert
    except Exception as e:
        print(f"[id-loss] onnx2torch 不可用({str(e)[:40]})，跳过 identity loss")
        return None, None
    arc_path = os.path.expanduser("~/.insightface/models/buffalo_l/w600k_r50.onnx")
    if not os.path.exists(arc_path):
        print("[id-loss] 未找到 arcface w600k_r50.onnx，跳过")
        return None, None
    import torch
    import torch.nn.functional as F
    proto = onnx.load(arc_path)             # 内存加载，规避 convert 写临时文件的权限问题
    m = convert(proto).eval().to(dev)
    for p in m.parameters():
        p.requires_grad_(False)

    def prep(x):                             # x: NCHW BGR [0,1] → arcface 输入(RGB,112,(v*2-1))
        x = x[:, [2, 1, 0], :, :]
        x = F.interpolate(x, size=(112, 112), mode="bilinear", align_corners=False)
        return x * 2.0 - 1.0
    print("[id-loss] ArcFace(r50) 可微身份损失就绪")
    return m, prep


def _build_discriminator(base=64):
    """PatchGAN 判别器（对抗损失，锐化 dec_src 输出，治"糊"）。输出逐 patch 真伪 logits。"""
    import torch.nn as nn

    def blk(ci, co, s=2):
        return [nn.Conv2d(ci, co, 4, s, 1), nn.InstanceNorm2d(co, affine=True), nn.LeakyReLU(0.2, inplace=True)]

    layers = [nn.Conv2d(3, base, 4, 2, 1), nn.LeakyReLU(0.2, inplace=True)]
    layers += blk(base, base * 2) + blk(base * 2, base * 4) + blk(base * 4, base * 8, s=1)
    layers += [nn.Conv2d(base * 8, 1, 4, 1, 1)]
    return nn.Sequential(*layers)


def _onnx_export(wrap, dummy, out_path):
    """导出 DFM 契约 ONNX。torch 2.x 新 dynamo 导出器需 onnxscript；此处强制走稳定的
    TorchScript 导出器(dynamo=False)，规避额外依赖，产物 opset12、I/O 名对齐 .dfm。"""
    import torch
    kw = dict(input_names=["in_face:0"],
              output_names=["out_face_mask:0", "out_celeb_face:0", "out_celeb_face_mask:0"],
              dynamic_axes={"in_face:0": {0: "N"}, "out_face_mask:0": {0: "N"},
                            "out_celeb_face:0": {0: "N"}, "out_celeb_face_mask:0": {0: "N"}},
              opset_version=12)
    try:
        torch.onnx.export(wrap, dummy, out_path, dynamo=False, **kw)
    except TypeError:
        torch.onnx.export(wrap, dummy, out_path, **kw)


class ExportWrapper:
    """把 enc+inter+dec_src 包成 DFM 契约：输入/输出全 NHWC、BGR[0,1]，输出名带 :0。"""
    def __new__(cls, enc, inter, dec_src):
        import torch, torch.nn as nn

        class _W(nn.Module):
            def __init__(self):
                super().__init__()
                self.enc, self.inter, self.dec = enc, inter, dec_src
            def forward(self, in_face):                 # in_face: NHWC BGR [0,1]
                x = in_face.permute(0, 3, 1, 2)
                bgr, mask = self.dec(self.inter(self.enc(x)))
                celeb = bgr.permute(0, 2, 3, 1)         # NHWC
                m = mask.permute(0, 2, 3, 1)            # NHWC 1ch
                return m, celeb, m                      # out_face_mask, out_celeb_face, out_celeb_face_mask
        return _W()


# ────────────────────────── 数据 ──────────────────────────
def _wf_ellipse_mask(res):
    """WF 对齐脸的软椭圆脸区 mask（脸集无逐图分割时的稳健近似；训练目标）。"""
    m = np.zeros((res, res), np.float32)
    cv2.ellipse(m, (res // 2, int(res * 0.52)), (int(res * 0.34), int(res * 0.46)),
                0, 0, 360, 1.0, -1)
    return cv2.GaussianBlur(m, (0, 0), res * 0.02)


class FaceSet:
    def __init__(self, root, res):
        self.files = [p for p in Path(root).rglob("*") if p.suffix.lower() in IMAGE_EXT]
        self.res = res
        if not self.files:
            raise RuntimeError(f"{root} 无图片")
        self.mask = _wf_ellipse_mask(res)
    def __len__(self): return len(self.files)
    def _read(self, p):
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        if img is None: return None
        if img.shape[0] != self.res:
            img = cv2.resize(img, (self.res, self.res), interpolation=cv2.INTER_AREA)
        return img
    def batch(self, bs, augment=True):
        imgs, masks = [], []
        while len(imgs) < bs:
            p = random.choice(self.files)
            img = self._read(p)
            if img is None: continue
            if augment:
                if random.random() < 0.5:
                    img = cv2.flip(img, 1)
                a = 1.0 + random.uniform(-0.12, 0.12)     # 亮度/对比抖动
                img = np.clip(img.astype(np.float32) * a, 0, 255).astype(np.uint8)
            imgs.append(img.astype(np.float32) / 255.0)
            masks.append(self.mask)
        x = np.stack(imgs).astype(np.float32)             # NHWC BGR
        m = np.stack(masks)[..., None].astype(np.float32)
        return x, m


# ────────────────────────── 损失 ──────────────────────────
def _dssim(t, a, b):
    """1 - SSIM（简化单尺度，窗口 11 高斯），配 L1 用。"""
    import torch, torch.nn.functional as F
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    win = 11; sigma = 1.5
    coords = torch.arange(win, dtype=torch.float32, device=a.device) - win // 2
    g = torch.exp(-(coords ** 2) / (2 * sigma ** 2)); g = (g / g.sum())
    k2 = (g[:, None] * g[None, :]).reshape(1, 1, win, win)
    k2 = k2.expand(a.shape[1], 1, win, win)
    def f(x): return F.conv2d(x, k2, padding=win // 2, groups=x.shape[1])
    mu_a, mu_b = f(a), f(b)
    va, vb = f(a * a) - mu_a ** 2, f(b * b) - mu_b ** 2
    vab = f(a * b) - mu_a * mu_b
    ssim = ((2 * mu_a * mu_b + C1) * (2 * vab + C2)) / ((mu_a ** 2 + mu_b ** 2 + C1) * (va + vb + C2))
    return (1 - ssim).mean()


# ────────────────────────── 训练 ──────────────────────────
def _model_dir(char): return WORKSPACE / char / "torch_model"


def _save_ckpt(char, res, enc, inter, dec_src, dec_dst, it, cfg, disc=None):
    import torch
    d = _model_dir(char); d.mkdir(parents=True, exist_ok=True)
    blob = {"enc": enc.state_dict(), "inter": inter.state_dict(),
            "dec_src": dec_src.state_dict(), "dec_dst": dec_dst.state_dict(),
            "iter": it, "res": res, "cfg": cfg,
            "disc": disc.state_dict() if disc is not None else None}
    torch.save(blob, d / "ckpt.pt")


def _load_ckpt(char):
    import torch
    p = _model_dir(char) / "ckpt.pt"
    if not p.exists(): return None
    return torch.load(p, map_location="cpu")


def cmd_train(a):
    import torch
    torch.backends.cudnn.benchmark = True
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    res = a.res
    enc, inter, dec_src, dec_dst = _build_modules(res)
    ck = _load_ckpt(a.char)
    start = 0
    if ck and ck.get("res") == res and not a.restart:
        try:
            enc.load_state_dict(ck["enc"]); inter.load_state_dict(ck["inter"])
            dec_src.load_state_dict(ck["dec_src"]); dec_dst.load_state_dict(ck["dec_dst"])
            start = ck.get("iter", 0)
            print(f"[续训] 从 iter={start} 继续")
        except Exception as e:
            print(f"[!] 旧检查点与当前架构不兼容({str(e)[:60]})，从零开始训练")
            start = 0
    for m in (enc, inter, dec_src, dec_dst): m.to(dev)
    dst_dir = a.dst or str(WORKSPACE / "_universal_dst")
    if not a.dst and not Path(dst_dir).exists():
        raise RuntimeError(f"未指定 --dst 且通用集不存在：{dst_dir}\n先跑 dfm_workspace/_fetch_universal_dst.py 拉通用多样人脸集")
    src = FaceSet(a.src, res); dst = FaceSet(dst_dir, res)
    print(f"[数据] src={len(src)} dst={len(dst)}({'通用集' if not a.dst else '自定义'})  dev={dev} res={res} batch={a.batch}")
    params = list(enc.parameters()) + list(inter.parameters()) + \
             list(dec_src.parameters()) + list(dec_dst.parameters())
    opt = torch.optim.Adam(params, lr=a.lr, betas=(0.5, 0.999))
    cfg = {"enc_ch": 128, "ae_dims": 256, "dec_ch": 128}

    # 可选 PatchGAN：a.gan_after>0 时到该 iter 起对 dec_src 重建加对抗损失(锐化，治糊)
    disc = opt_d = None
    if a.gan_after >= 0 and a.gan_power > 0:
        disc = _build_discriminator().to(dev)
        if ck and ck.get("disc") is not None and not a.restart:
            try: disc.load_state_dict(ck["disc"])
            except Exception: pass
        opt_d = torch.optim.Adam(disc.parameters(), lr=a.lr, betas=(0.5, 0.999))
        print(f"[GAN] PatchGAN 就绪 gan_after={a.gan_after} gan_power={a.gan_power}")

    # 可选 ArcFace identity loss：直接把 dec_src 重建脸的身份拉向真 src（= 优化辨识度本身）
    arc = arc_prep = None
    if a.id_power > 0:
        arc, arc_prep = _load_arcface_torch(dev)

    def to_t(x): return torch.from_numpy(x).permute(0, 3, 1, 2).contiguous().to(dev)
    bce = torch.nn.functional.binary_cross_entropy_with_logits

    def _oom(e):
        s = str(e).lower()
        return isinstance(e, RuntimeError) and any(
            k in s for k in ("out of memory", "alloc_failed", "memoryallocation", "cudnn_status_alloc"))

    t0 = time.time(); loss_ema = None; oom_hits = 0; oom_streak = 0; last_ok = start
    it = start
    while it < a.iters:
        try:
            sx, sm = src.batch(a.batch); dx, dm = dst.batch(a.batch)
            sx, sm, dx, dm = to_t(sx), to_t(sm), to_t(dx), to_t(dm)
            gan_on = disc is not None and it >= a.gan_after
            opt.zero_grad(set_to_none=True)
            bs, ms = dec_src(inter(enc(sx)))
            bd, md = dec_dst(inter(enc(dx)))
            l_src = _dssim(torch, sx * sm, bs * sm) + (sx * sm - bs * sm).abs().mean() + 0.3 * (ms - sm).abs().mean()
            l_dst = _dssim(torch, dx * dm, bd * dm) + (dx * dm - bd * dm).abs().mean() + 0.3 * (md - dm).abs().mean()
            loss = l_src + l_dst
            if arc is not None:
                e_gen = torch.nn.functional.normalize(arc(arc_prep(bs * sm + (1 - sm) * sx)), dim=1)
                e_tgt = torch.nn.functional.normalize(arc(arc_prep(sx)), dim=1)
                loss = loss + a.id_power * (1 - (e_gen * e_tgt).sum(1)).mean()
            if gan_on:
                for p in disc.parameters(): p.requires_grad_(False)
                g_logit = disc(bs * sm)
                loss = loss + a.gan_power * bce(g_logit, torch.ones_like(g_logit))
            loss.backward(); opt.step()
            if gan_on:
                for p in disc.parameters(): p.requires_grad_(True)
                opt_d.zero_grad(set_to_none=True)
                r_logit = disc((sx * sm).detach())
                f_logit = disc((bs * sm).detach())
                d_loss = 0.5 * (bce(r_logit, torch.ones_like(r_logit)) + bce(f_logit, torch.zeros_like(f_logit)))
                d_loss.backward(); opt_d.step()
            loss_ema = loss.item() if loss_ema is None else 0.98 * loss_ema + 0.02 * loss.item()
            last_ok = it; oom_streak = 0
        except Exception as e:
            if _oom(e):
                # 共享 GPU 被生产服务抢显存 → 让路：存档、清缓存、等一会再重试同一步（不崩、不丢进度）
                oom_hits += 1; oom_streak += 1
                sx = sm = dx = dm = bs = ms = bd = md = loss = None  # 丢弃引用，释放本步显存/计算图
                try: torch.cuda.empty_cache()
                except Exception: pass
                if oom_streak == 1 and it > start:
                    _save_ckpt(a.char, res, enc, inter, dec_src, dec_dst, it, cfg, disc)  # 被抢先落盘，防后续被杀丢进度
                if oom_hits % 5 == 1:
                    print(f"iter {it}  [OOM#{oom_hits}] 显存被抢，等待 30s 后重试（生产服务优先）", flush=True)
                if oom_streak >= 40:
                    print(f"iter {it}  [OOM] 连续 {oom_streak} 次要不到显存(~20分钟)，存档退出，交给守候器等下一窗口", flush=True)
                    return 3
                time.sleep(30)
                continue
            raise
        if it % a.log_every == 0:
            ips = (it - start + 1) / (time.time() - t0)
            tag = " +GAN" if gan_on else ""
            oomtag = f" oom={oom_hits}" if oom_hits else ""
            print(f"iter {it}  loss {loss_ema:.4f}  {ips:.1f} it/s{tag}{oomtag}", flush=True)
        if it > start and it % a.save_every == 0:
            _save_ckpt(a.char, res, enc, inter, dec_src, dec_dst, it, cfg, disc)
            _preview(a.char, res, enc, inter, dec_src, dec_dst, dst, dev, it)
        it += 1
    _save_ckpt(a.char, res, enc, inter, dec_src, dec_dst, a.iters, cfg, disc)
    _preview(a.char, res, enc, inter, dec_src, dec_dst, dst, dev, a.iters)
    print(f"[✓] 训练结束 iter={a.iters}  模型 → {_model_dir(a.char)}")
    return 0


def _preview(char, res, enc, inter, dec_src, dec_dst, dst, dev, it):
    import torch
    enc.eval(); inter.eval(); dec_src.eval()
    with torch.no_grad():
        dx, _ = dst.batch(4, augment=False)
        x = torch.from_numpy(dx).permute(0, 3, 1, 2).contiguous().to(dev)
        swap, _ = dec_src(inter(enc(x)))      # dst 脸 → 角色身份
        swap = swap.permute(0, 2, 3, 1).cpu().numpy()
    rows = []
    for i in range(4):
        a_img = (dx[i] * 255).astype(np.uint8)
        b_img = (np.clip(swap[i], 0, 1) * 255).astype(np.uint8)
        rows.append(np.hstack([a_img, b_img]))
    grid = np.vstack(rows)
    d = _model_dir(char); d.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".jpg", grid)[1].tofile(str(d / f"preview_{it:06d}.jpg"))
    enc.train(); inter.train(); dec_src.train()


# ────────────────────────── 导出 ──────────────────────────
def cmd_export(a):
    import torch
    ck = _load_ckpt(a.char)
    if not ck:
        print(f"[!] 无训练检查点：先 train --char {a.char}", file=sys.stderr); return 2
    res = ck["res"]
    enc, inter, dec_src, _ = _build_modules(res)
    enc.load_state_dict(ck["enc"]); inter.load_state_dict(ck["inter"]); dec_src.load_state_dict(ck["dec_src"])
    enc.eval(); inter.eval(); dec_src.eval()
    wrap = ExportWrapper(enc, inter, dec_src).eval()
    dummy = torch.rand(1, res, res, 3)
    out = Path(a.out) if a.out else (_model_dir(a.char) / f"{a.char}.dfm")
    _onnx_export(wrap, dummy, str(out))
    print(f"[✓] 导出 DFM ONNX → {out}  (iter={ck.get('iter')}, res={res})")
    return 0


# ────────────────────────── 自检 ──────────────────────────
def cmd_selftest(a):
    """搭网络→前向→导出→onnxruntime 按 DFM 契约验证 I/O（不需训练数据）。"""
    import torch, onnxruntime as ort, tempfile
    res = getattr(a, "res", None) or 224
    enc, inter, dec_src, dec_dst = _build_modules(res)
    nparams = sum(p.numel() for m in (enc, inter, dec_src, dec_dst) for p in m.parameters())
    x = torch.rand(2, 3, res, res)
    f = inter(enc(x)); bgr, mask = dec_src(f)
    assert bgr.shape == (2, 3, res, res) and mask.shape == (2, 1, res, res), "解码输出形状错"
    print(f"[selftest] 前向 OK  参数量={nparams/1e6:.1f}M  bgr{tuple(bgr.shape)} mask{tuple(mask.shape)}")
    wrap = ExportWrapper(enc, inter, dec_src).eval()
    tmp = Path(tempfile.gettempdir()) / "_dfm_selftest.dfm"
    _onnx_export(wrap, torch.rand(1, res, res, 3), str(tmp))
    sess = ort.InferenceSession(str(tmp), providers=["CPUExecutionProvider"])
    ins = sess.get_inputs(); outs = sess.get_outputs()
    print(f"[selftest] ONNX in={ins[0].name}{ins[0].shape}  outs={[o.name for o in outs]}")
    assert ins[0].name == "in_face:0", "输入名不符 DFM 契约"
    assert set(o.name for o in outs) == {"out_face_mask:0", "out_celeb_face:0", "out_celeb_face_mask:0"}, "输出名不符契约"
    blob = np.random.rand(1, res, res, 3).astype(np.float32)
    r = sess.run(None, {"in_face:0": blob})
    shapes = [x.shape for x in r]
    print(f"[selftest] 推理 OK 输出形状={shapes}")
    print("[selftest] ✓ 契约与 DeepFaceLive .dfm 一致 → 可被 faceswap_api.DFMSwap 直接加载")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DFM PyTorch 原生训练器（Blackwell 友好，导出 .dfm ONNX 契约）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("train")
    p.add_argument("--char", required=True); p.add_argument("--src", required=True)
    p.add_argument("--dst", default=None, help="目标脸集；缺省用通用集 dfm_workspace/_universal_dst(4000张多样脸)")
    p.add_argument("--res", type=int, default=224); p.add_argument("--batch", type=int, default=8)
    p.add_argument("--iters", type=int, default=100000); p.add_argument("--lr", type=float, default=5e-5)
    p.add_argument("--log-every", type=int, default=50, dest="log_every")
    p.add_argument("--save-every", type=int, default=2000, dest="save_every")
    p.add_argument("--gan-after", type=int, default=-1, dest="gan_after",
                   help=">=0 时从该 iter 起加 PatchGAN 对抗损失(锐化，治糊)；-1 关闭")
    p.add_argument("--gan-power", type=float, default=0.1, dest="gan_power")
    p.add_argument("--id-power", type=float, default=0.0, dest="id_power",
                   help=">0 时加 ArcFace identity loss(直接优化辨识度)；建议 0.1~0.3")
    p.add_argument("--restart", action="store_true")
    e = sub.add_parser("export"); e.add_argument("--char", required=True); e.add_argument("--out", default=None)
    st = sub.add_parser("selftest"); st.add_argument("--res", type=int, default=224)
    args = ap.parse_args()
    if args.cmd == "train": sys.exit(cmd_train(args))
    elif args.cmd == "export": sys.exit(cmd_export(args))
    elif args.cmd == "selftest": sys.exit(cmd_selftest(args))
