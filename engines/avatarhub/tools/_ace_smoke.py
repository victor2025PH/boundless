# -*- coding: utf-8 -*-
"""ACE-Step 离线冒烟：直接实例化 pipeline，在 5090 上真跑一段 30s 原创歌生成。

用法（需先释放显存，权重已在 models/ace_step/ACE-Step-v1-3.5B）：
    set PYTHONPATH=c:\\模仿音色\\ACE-Step
    ymsvc python tools/_ace_smoke.py [--steps 27] [--dur 30] [--offload]

验收点：出 wav、时长匹配、能量 sanity、打印各阶段耗时与显存峰值。
"""
import argparse
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ACE-Step"))

CKPT = os.path.join(ROOT, "models", "ace_step", "ACE-Step-v1-3.5B")
OUT = os.path.join(ROOT, "songs", "_ace_smoke_out.wav")

PROMPT = "pop, mandarin, female vocal, warm, acoustic guitar, 90 bpm, uplifting"
LYRICS = """[verse]
晚风吹过屋顶
星星刚刚点亮
你哼着一段旋律
把今天轻轻收藏

[chorus]
就唱吧 不用管唱得像不像
心跳就是节拍 快乐就是原创
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=27)
    ap.add_argument("--dur", type=float, default=30.0)
    ap.add_argument("--offload", action="store_true")
    args = ap.parse_args()

    import torch
    from acestep.pipeline_ace_step import ACEStepPipeline

    free0, total = torch.cuda.mem_get_info()
    print(f"[smoke] VRAM free {free0/1e9:.1f}G / {total/1e9:.1f}G | offload={args.offload}")

    t0 = time.time()
    pipe = ACEStepPipeline(checkpoint_dir=CKPT, dtype="bfloat16",
                           cpu_offload=args.offload, torch_compile=False)
    pipe.load_checkpoint(CKPT)
    t_load = time.time() - t0
    torch.cuda.reset_peak_memory_stats()
    print(f"[smoke] 模型加载 {t_load:.1f}s")

    t1 = time.time()
    out = pipe(
        format="wav",
        audio_duration=args.dur,
        prompt=PROMPT,
        lyrics=LYRICS,
        infer_step=args.steps,
        guidance_scale=15.0,
        scheduler_type="euler",
        cfg_type="apg",
        omega_scale=10.0,
        manual_seeds=[42],
        save_path=OUT,
    )
    t_gen = time.time() - t1
    peak = torch.cuda.max_memory_allocated() / 1e9
    print(f"[smoke] 生成 {t_gen:.1f}s | 显存峰值 {peak:.1f}G | out={out[0]}")

    import soundfile as sf
    wav, sr = sf.read(OUT)
    dur = len(wav) / sr
    rms = float((wav ** 2).mean() ** 0.5)
    print(f"[smoke] wav {dur:.1f}s @ {sr}Hz rms={rms:.4f}")
    assert dur > args.dur * 0.8, "时长异常"
    assert rms > 0.01, "音频近乎无声"
    print("RESULT: OK")


if __name__ == "__main__":
    main()
