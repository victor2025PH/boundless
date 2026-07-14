# -*- coding: utf-8 -*-
"""
DFM 每角色模型训练编排（辨识度终极方案 · 阶段3）
====================================================
把「对齐脸集(dfm_extract 产出) + RTM 预训练 + 快训 recipe」串成一条可复现的命令流，
并在开跑前做**前置体检**（GPU/环境/DFL/素材/预训练/磁盘），避免开跑数小时才发现缺件。

⚠ 关键事实（本阶段循证得出，务必先读）：
  DeepFaceLab 用 TensorFlow，而 RTX 5090(Blackwell/sm_120) 官方 TF 不支持。
  Windows 原生跑不起来，训练环境有三条路（本脚本 --check 会自动判别当前处于哪条路）：
    路径C(本机 PyTorch 原生 · 推荐)：dfm_train_torch.py，用本机 facefusion 环境的
                      torch 2.12+cu128（sm_120 kernel 实测可用）从零训每角色换脸自编码器，
                      **零 TF、零 WSL2，原生跑 5090**，导出同款 .dfm ONNX 契约。首选。
    路径A(本机 WSL2)：WSL2+Ubuntu + volnas10/DeepFaceLab-RTX5000 fork(自带定制 TF)。
                      一次性搭建，之后本机 5090 训练；训练期独占本机 GPU。用 DFL 成熟质量兜底。
    路径B(云)       ：租非 Blackwell 卡或用 pytorch/DFL 容器训练，产出 .dfm 回传。
                      不占本机、不折腾 TF，按时长付费。
  三条路产出的 .dfm 都经 faceswap_api 的 DFMSwap 适配器即插即用（阶段1已验证）。

RTM「快训」recipe（iperov 官方 FAQ「1天出片」法，本脚本据此生成命令/配置）：
  1. data_dst/aligned ← RTM WF Faceset V2（公开多样人脸，当"任意脸"训练目标）
  2. data_src/aligned ← 本角色对齐脸集（dfm_extract 产出）
  3. model/ ← RTT model 224 V2 预训练（省掉从零学脸的数天）
  4. 训练 +25k 迭代 → 删 inter_AB.npy → +30k → random_warp off + GAN0.1/patch28/gan_dims32 → 封顶
  5. export SAEHD as dfm（quantized）→ <角色>.dfm

用法：
  python dfm_train.py --char 刘德华 --check            # 只体检环境与素材，不训练
  python dfm_train.py --char 刘德华 --emit-plan         # 打印/落盘该角色完整命令流(路径A)
  python dfm_train.py --char 刘德华 --deploy <x.dfm>    # 训好后：部署 .dfm 到 .104 生产
"""
import os, sys, json, argparse, shutil, subprocess
from pathlib import Path

BASE = Path(r"C:\模仿音色")
WORKSPACE = BASE / "dfm_workspace"

# RTM 快训所需公开资产（已核实存在于 HF: dimanchkek/Deepfacelive-DFM-Models）
_HF_REPO = "dimanchkek/Deepfacelive-DFM-Models"
_HF_BASE = f"https://huggingface.co/datasets/{_HF_REPO}/resolve/main"
RTM_ASSETS = {
    "rtt_model_224_v2": {
        "desc": "RTT model 224 V2 预训练（放 model/，省掉从零学脸的数天）",
        "hf_path": "Pretrained/RTT model 224 V2.zip",
    },
    "rtm_dst_faceset": {
        "desc": "MiniRTM DST Faceset（放 data_dst/aligned，作'任意脸'目标集）",
        "hf_path": "Facesets/Latest_MiniRTM_DST_Faceset/Latest_MiniRTM_DST_Faceset_by_Druuzil.pak",
    },
}


def cmd_fetch_rtm(args):
    """把 RTM 快训公开资产拉到 dfm_workspace/_rtm_assets（一次性，供后续所有角色复用）。"""
    import urllib.parse
    out = WORKSPACE / "_rtm_assets"
    out.mkdir(parents=True, exist_ok=True)
    for k, v in RTM_ASSETS.items():
        url = f"{_HF_BASE}/{urllib.parse.quote(v['hf_path'])}"
        dst = out / Path(v["hf_path"]).name
        if dst.exists() and dst.stat().st_size > 0:
            print(f"[skip] {dst.name} 已存在"); continue
        print(f"[get] {v['desc']}\n      {url}")
        r = subprocess.run(["curl.exe", "-L", "-C", "-", "-o", str(dst), url])
        if r.returncode != 0:
            print(f"[!] 下载失败 {k}（可手动从 HF 页面下）")
    print(f"[✓] RTM 资产目录：{out}")
    return 0


def _probe_gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or None
    except Exception:
        return None


def _probe_torch_sm120():
    """判断本机 torch 是否能在当前 GPU(sm_120) 跑 kernel → 决定路径C(PyTorch 原生)是否可用。"""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        cap = torch.cuda.get_device_capability(0)
        x = torch.randn(32, 32, device="cuda")
        _ = (x @ x).sum().item()  # 触发 kernel，sm 不匹配会在此抛错
        return f"torch {torch.__version__} cu{torch.version.cuda} sm_{cap[0]}{cap[1]} kernel OK"
    except Exception as e:
        return f"FAIL: {str(e)[:80]}"


def _probe_wsl():
    # wsl.exe 输出是 UTF-16-LE，用 bytes 捕获后手工解码，避免 text=True 的 utf-8 解码崩线程
    try:
        out = subprocess.run(["wsl", "-l", "-v"], capture_output=True, timeout=15)
        raw = (out.stdout or b"") + (out.stderr or b"")
        txt = raw.decode("utf-16-le", errors="ignore").replace("\x00", "")
        if "--install" in txt or "NAME" not in txt:
            return None
        return txt.strip()
    except Exception:
        return None


def _find_dfl():
    """找本机/WSL 的 DeepFaceLab 安装（scan 常见位置 + 环境变量 DFL_ROOT）。"""
    cands = []
    env = os.environ.get("DFL_ROOT")
    if env:
        cands.append(Path(env))
    for base in [BASE, BASE.parent, Path("C:/"), Path("D:/")]:
        try:
            for p in base.glob("DeepFaceLab*"):
                cands.append(p)
        except Exception:
            pass
    for c in cands:
        if c and (c / "main.py").exists():
            return c
    return None


def cmd_check(args):
    print("=" * 60)
    print(f" DFM 训练前置体检 · 角色={args.char}")
    print("=" * 60)
    ok = True

    gpu = _probe_gpu()
    print(f"[GPU] {gpu or '未检出'}")
    is_blackwell = bool(gpu and ("5090" in gpu or "5080" in gpu or "50" in gpu.split(',')[0]))
    if is_blackwell:
        print("      → Blackwell 架构：DeepFaceLab 的 TF 需 WSL2+定制构建(路径A) 或 云(路径B)")

    # 路径C：本机 PyTorch 原生（推荐）
    tsm = _probe_torch_sm120()
    print(f"[PyTorch原生·路径C] {tsm or '本机 torch 无 CUDA'}")
    if tsm and "OK" in tsm:
        udst = WORKSPACE / "_universal_dst"
        n_udst = len(list(udst.glob("*.jpg"))) if udst.exists() else 0
        dst_hint = "(缺省用通用集)" if n_udst else "(先跑 dfm_workspace/_fetch_universal_dst.py 拉通用集)"
        print(f"      → 推荐(512+GAN+身份损失)：")
        print(f"        python dfm_train_torch.py train --char {args.char} --src <角色对齐脸集> \\")
        print(f"          --res 512 --iters 100000 --gan-after 60000 --id-power 0.25   # --dst {dst_hint}")
        print(f"        python dfm_eval.py --model {args.char}.dfm --dst <独立目标脸> --ref <角色参考照>  # margin>0.15 上线")
    print(f"[通用dst] {'在：'+str(len(list((WORKSPACE/'_universal_dst').glob('*.jpg'))))+' 张' if (WORKSPACE/'_universal_dst').exists() else '缺（可选，缺省训练用）'}")

    wsl = _probe_wsl()
    print(f"[WSL2] {'已装：'+wsl.splitlines()[-1].strip() if wsl else '未安装'}")
    if is_blackwell and not wsl:
        print("      → 路径A 需要先 `wsl --install`（管理员，装 Ubuntu 24.04）")

    torch_ok = bool(tsm and "OK" in tsm)
    dfl = _find_dfl()
    print(f"[DeepFaceLab] {dfl or '未找到（路径A/B 才需要；路径C 用 PyTorch 无需 DFL）'}")
    if not dfl and not torch_ok:
        # 只有当路径C(torch) 也不可用时，缺 DFL 才算阻断
        ok = False

    # 素材：对齐脸集
    aligned = WORKSPACE / args.char / "aligned"
    n_aligned = len(list(aligned.glob("*.jpg"))) if aligned.exists() else 0
    print(f"[素材] {args.char} 对齐脸集: {n_aligned} 张 @ {aligned}")
    if n_aligned < args.min_faces:
        print(f"      → 不足 {args.min_faces}：先跑 dfm_material.py + dfm_extract.py（访谈/多角度视频最佳）")
        ok = False

    # RTM 资产
    rtm_dir = WORKSPACE / "_rtm_assets"
    for k, v in RTM_ASSETS.items():
        present = (rtm_dir / Path(v["hf_path"]).name).exists() if rtm_dir.exists() else False
        print(f"[RTM] {Path(v['hf_path']).name}: {'在' if present else '缺'} — {v['desc']}")
        if not present:
            print(f"      拉取: python dfm_train.py --char {args.char} --fetch-rtm  (HF: {_HF_REPO})")

    # 磁盘
    try:
        free_gb = shutil.disk_usage(BASE).free / 1e9
        print(f"[磁盘] 可用 {free_gb:.0f} GB {'(充足)' if free_gb > 30 else '(建议≥30G)'}")
    except Exception:
        pass

    print("-" * 60)
    if ok and torch_ok and n_aligned >= args.min_faces:
        print("结论：✓ 就绪，可直接路径C(本机PyTorch)开训")
    elif ok:
        print(f"结论：✓ 环境就绪（路径C{'可用' if torch_ok else '不可用'}）；素材待补齐至 {args.min_faces} 张")
    else:
        print("结论：✗ 有缺件，见上方 →")
    return 0 if ok else 1


def cmd_emit_plan(args):
    """生成路径A(WSL2 DFL-RTX5000)的完整命令流；写到 workspace/<char>/train_plan.sh。"""
    char = args.char
    ws = f"~/dfl_workspace/{char}"
    aligned_win = (WORKSPACE / char / "aligned").as_posix()
    lines = [
        "#!/bin/bash",
        f"# DFM 训练命令流（路径A：WSL2 + DeepFaceLab-RTX5000）· 角色 {char}",
        "set -e",
        'DFL=~/DeepFaceLab-RTX5000',
        f'WS={ws}',
        'mkdir -p $WS/{data_src/aligned,data_dst/aligned,model}',
        "",
        "# 1) 导入本角色对齐脸集（从 Windows 侧拷入 WSL）",
        f'cp -r "/mnt/c/模仿音色/dfm_workspace/{char}/aligned/." $WS/data_src/aligned/',
        "# 2) 导入 RTM WF Faceset V2 → data_dst/aligned（作'任意脸'目标）",
        '#    解压 RTM WF Faceset V2 到 $WS/data_dst/aligned/',
        "# 3) 导入 RTT model 224 V2 预训练 → model/",
        '#    解压 RTT model 224 V2 到 $WS/model/',
        "",
        "# 4) 快训（官方 1天recipe）——首轮 25k",
        'python $DFL/main.py train --training-data-src-dir $WS/data_src/aligned \\',
        '  --training-data-dst-dir $WS/data_dst/aligned --model-dir $WS/model --model SAEHD',
        '#   跑到 ~25k 迭代后停；删 inter_AB.npy：',
        'rm -f $WS/model/*_inter_AB.npy',
        '#   继续 +30k；然后在交互里设 random_warp=n, gan_power=0.1, gan_patch_size=28, gan_dims=32，再 +数万',
        "",
        "# 5) 导出 .dfm（quantized）",
        f'python $DFL/main.py exportdfm --model-dir $WS/model --model SAEHD',
        f'#   产物：$WS/model/{char}.dfm  → 回拷 Windows：',
        f'cp $WS/model/*.dfm "/mnt/c/模仿音色/dfm_workspace/{char}/"',
        "",
        f"# 6) 部署到生产：python dfm_train.py --char {char} --deploy dfm_workspace/{char}/{char}.dfm",
    ]
    out = WORKSPACE / char / "train_plan.sh"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print("\n".join(lines))
    print(f"\n[✓] 命令流已落盘 → {out}")
    print(f"    src 对齐脸集(Windows 侧): {aligned_win}")
    return 0


def cmd_deploy(args):
    """把训好的 .dfm 部署到 .104 生产并热切到该模型（复用现有 scp/ssh 通路）。"""
    dfm = Path(args.deploy)
    if not dfm.exists():
        print(f"[!] 找不到 {dfm}", file=sys.stderr); return 2
    remote = f"/{dfm.name}"
    print(f"[1/3] scp {dfm.name} → .104 …")
    r = subprocess.run(["scp", "-o", "ConnectTimeout=15", str(dfm), f"Administrator@192.168.0.104:{remote}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("scp 失败：", r.stderr[-400:]); return 1
    print(f"[2/3] 已上传到 .104 C:{remote}")
    print("[3/3] 启用：在 .104 设 FACESWAP_MODEL=C:\\%s 重启 faceswap（或经 Hub 按角色下发）" % dfm.name)
    print("      —— 生产热切建议走 Hub 的按角色引擎路由，避免打断在线会话。")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DFM 每角色模型训练编排")
    ap.add_argument("--char", required=True)
    ap.add_argument("--check", action="store_true", help="前置体检")
    ap.add_argument("--emit-plan", action="store_true", dest="emit_plan", help="生成训练命令流(路径A)")
    ap.add_argument("--fetch-rtm", action="store_true", dest="fetch_rtm", help="下载 RTM 快训公开资产")
    ap.add_argument("--deploy", default=None, help="部署训好的 .dfm 到 .104")
    ap.add_argument("--min-faces", type=int, default=1500, dest="min_faces")
    a = ap.parse_args()
    if a.deploy:
        sys.exit(cmd_deploy(a))
    elif a.fetch_rtm:
        sys.exit(cmd_fetch_rtm(a))
    elif a.emit_plan:
        sys.exit(cmd_emit_plan(a))
    else:
        sys.exit(cmd_check(a))
