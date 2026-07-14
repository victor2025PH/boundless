# -*- coding: utf-8 -*-
"""试衣(VTON)候选模型可装性探针 —— 在**本机真实环境**里回答"该上哪个模型"。

背景（2026-07-08 试衣质量专项）：
  当前 8002 实际跑的是 inpaint_fallback（SD1.5 inpaint + 文本提示），**服装参考图
  根本没进管线**——试衣结果与所选服装无关，等于假试衣。任何真 VTON 模型都是质变。

候选与其在 Windows/本机的关键约束：
  FitDiT   : SD3-DiT 架构。预处理 = DWPose(onnx) + humanparsing(onnx)，无 detectron2
             依赖 → Windows 友好。权重 ~16GB(bf16)。diffusers>=0.31 需 SD3 支持。
             官方仓自带 preprocess/ 与推理脚本，支持 offload(<6G 显存)。
  CatVTON  : 轻量(权重~6GB 含 SD1.5-inpaint 底模)。但 AutoMasker 依赖
             DensePose(detectron2) —— Windows 无轮子需 VS 编译，风险高；
             mask 自备可绕过（需要自研 cloth-agnostic mask，工程量中）。
  OOTDiffusion: SD1.5 双 UNet。预处理 humanparsing(onnx)+openpose。权重 ~8GB。
             质量次于 FitDiT/CatVTON（2024 上半年水平）。
  IDM-VTON : 效果好但重(权重 ~20GB + densepose 预处理)，且官方管线类需自行适配
             （项目里 idm_vton_pipeline.py 从未存在过——当年就没装成）。

本脚本产出 logs/tryon_probe_report.json + 控制台矩阵，包含：
  python/torch/cuda/diffusers/transformers/onnxruntime 版本、显卡与显存、磁盘余量、
  HF 网络连通性（HF_ENDPOINT 生效值）、每个候选的 verdict。
"""
import sys, io, json, shutil, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(r"c:\模仿音色")
REPORT = BASE / "logs" / "tryon_probe_report.json"


def probe_env() -> dict:
    import platform
    out = {"python": platform.python_version()}
    try:
        import torch
        out["torch"] = torch.__version__
        out["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            out["gpu"] = torch.cuda.get_device_name(0)
            free_b, total_b = torch.cuda.mem_get_info()
            out["vram_free_gb"] = round(free_b / 1024**3, 1)
            out["vram_total_gb"] = round(total_b / 1024**3, 1)
            cap = torch.cuda.get_device_capability(0)
            out["sm"] = f"sm_{cap[0]}{cap[1]}"
            out["bf16_ok"] = torch.cuda.is_bf16_supported()
    except Exception as e:
        out["torch_error"] = str(e)[:200]
    for mod in ("diffusers", "transformers", "onnxruntime", "accelerate",
                "huggingface_hub", "safetensors", "xformers"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "?")
        except ImportError:
            out[mod] = None
    try:
        import onnxruntime as ort
        out["ort_providers"] = ort.get_available_providers()
    except Exception:
        pass
    return out


def probe_detectron2() -> dict:
    try:
        import detectron2  # noqa
        return {"installed": True, "version": detectron2.__version__}
    except ImportError:
        return {"installed": False,
                "note": "Windows 无官方轮子，需 VS BuildTools 源码编译（CatVTON AutoMasker 阻断项）"}


def probe_disk() -> dict:
    du = shutil.disk_usage("C:\\")
    return {"free_gb": round(du.free / 1024**3, 1), "total_gb": round(du.total / 1024**3, 1)}


def probe_hf() -> dict:
    import os
    out = {"HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "(默认 huggingface.co)")}
    try:
        from huggingface_hub import HfApi
        t0 = time.time()
        info = HfApi().model_info("BoyuanJiang/FitDiT", timeout=15)
        out["fitdit_repo_reachable"] = True
        out["fitdit_files"] = len(info.siblings or [])
        out["latency_ms"] = int((time.time() - t0) * 1000)
        try:  # 估算仓库体积（siblings size 需要 files_metadata）
            info2 = HfApi().model_info("BoyuanJiang/FitDiT", files_metadata=True, timeout=30)
            total = sum(s.size or 0 for s in (info2.siblings or []))
            out["fitdit_repo_gb"] = round(total / 1024**3, 1)
        except Exception:
            pass
    except Exception as e:
        out["fitdit_repo_reachable"] = False
        out["error"] = str(e)[:200]
    return out


def verdicts(env: dict, det2: dict, disk: dict, hf: dict) -> dict:
    diff_ver = env.get("diffusers") or "0"
    diff_maj = tuple(int(x) for x in diff_ver.split(".")[:2]) if env.get("diffusers") else (0, 0)
    ort_ok = bool(env.get("onnxruntime"))
    v = {}

    blockers = []
    if diff_maj < (0, 31):
        blockers.append(f"diffusers {diff_ver} < 0.31（SD3 支持不足，需升级——同环境风险：facefusion 主env 共用）")
    if not ort_ok:
        blockers.append("缺 onnxruntime（DWPose/humanparsing 预处理需要）")
    if disk["free_gb"] < 40:
        blockers.append(f"磁盘余量 {disk['free_gb']}GB < 40GB")
    if not hf.get("fitdit_repo_reachable"):
        blockers.append("HF 仓不可达（需配 HF_ENDPOINT 镜像或代理）")
    v["FitDiT"] = {"verdict": "GO" if not blockers else "BLOCKED", "blockers": blockers,
                   "weights_gb": hf.get("fitdit_repo_gb", "~16"),
                   "note": "首选：质量最好梯队 + 无 detectron2 依赖 + offload<6G"}

    blockers = []
    if not det2["installed"]:
        blockers.append("detectron2 未装（AutoMasker/DensePose 阻断；绕行=自研 mask，工程量中）")
    v["CatVTON"] = {"verdict": "GO" if not blockers else "BLOCKED", "blockers": blockers,
                    "weights_gb": 6, "note": "轻量备选；mask-based 模式可绕 detectron2 但要自研 agnostic mask"}

    blockers = []
    if not ort_ok:
        blockers.append("缺 onnxruntime")
    v["OOTDiffusion"] = {"verdict": "GO" if not blockers else "BLOCKED", "blockers": blockers,
                         "weights_gb": 8, "note": "次选：可装性好但质量低于 FitDiT 一档"}

    v["IDM-VTON"] = {"verdict": "SKIP", "blockers": ["densepose 预处理 + 官方管线需大改（历史上从未在本机装成）"],
                     "weights_gb": 20, "note": "历史遗留选项，投入产出差"}
    return v


def main():
    print("=" * 72)
    print("试衣候选模型可装性探针")
    print("=" * 72)
    env = probe_env()
    det2 = probe_detectron2()
    disk = probe_disk()
    hf = probe_hf()
    v = verdicts(env, det2, disk, hf)

    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "env": env,
              "detectron2": det2, "disk": disk, "hf": hf, "verdicts": v}
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n[env] python={env.get('python')} torch={env.get('torch')} "
          f"diffusers={env.get('diffusers')} transformers={env.get('transformers')} "
          f"onnxruntime={env.get('onnxruntime')}")
    print(f"[gpu] {env.get('gpu')} {env.get('sm')} free={env.get('vram_free_gb')}G/"
          f"{env.get('vram_total_gb')}G bf16={env.get('bf16_ok')}")
    print(f"[disk] free={disk['free_gb']}G  [detectron2] {det2['installed']}")
    print(f"[hf] endpoint={hf.get('HF_ENDPOINT')} fitdit_reachable={hf.get('fitdit_repo_reachable')} "
          f"repo_size={hf.get('fitdit_repo_gb', '?')}G")
    print("\n──选型矩阵──")
    for name, d in v.items():
        print(f"  {name:14s} {d['verdict']:8s} weights~{d['weights_gb']}G  {d['note']}")
        for b in d["blockers"]:
            print(f"      ✗ {b}")
    print(f"\n[report] -> {REPORT}")


if __name__ == "__main__":
    main()
