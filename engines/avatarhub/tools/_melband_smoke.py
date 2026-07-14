#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3/O2 冒烟：Kim Mel-Band RoFormer 权重能否在 accom_separation 栈里加载+分离。
直接调 song_studio_server 的 _load_sep("mel") + run_separate(model_kind="mel")，
用曲库真歌跑 30s 片段，对比 BS 模型输出（人声 RMS/能量占比 sanity）。"""
import io
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import numpy as np  # noqa: E402

import song_studio_server as srv  # noqa: E402

SONG = os.path.join(BASE, "songs")


def _pick_song():
    for f in os.listdir(SONG):
        if f.lower().endswith((".mp3", ".wav", ".flac", ".m4a")):
            return os.path.join(SONG, f)
    raise SystemExit("songs/ 目录没有测试歌曲")


def main():
    cap = srv._cap()
    print("capabilities:", cap)
    assert cap.get("separate_mel"), "mel 权重/配置未就绪"
    p = _pick_song()
    print("测试歌曲:", os.path.basename(p))
    song, _ = srv.load_audio_any(open(p, "rb").read(), 44100, mono=False)
    if song.ndim == 1:
        song = np.stack([song, song])
    song = song[:, : 44100 * 30]                       # 30s 片段，省时
    task = {"cancel": False}

    results = {}
    for kind in ("mel", "bs"):
        t0 = time.time()
        vocals, accomp = srv.run_separate(song, task, num_overlap=2, model_kind=kind)
        el = time.time() - t0
        v_rms = float(np.sqrt(np.mean(vocals ** 2)))
        a_rms = float(np.sqrt(np.mean(accomp ** 2)))
        m_rms = float(np.sqrt(np.mean(song ** 2)))
        results[kind] = (el, v_rms, a_rms)
        print(f"[{kind}] {el:.1f}s vocals_rms={v_rms:.4f} accomp_rms={a_rms:.4f} "
              f"mix_rms={m_rms:.4f} shape={vocals.shape}")
        srv._unload("sep")
        assert vocals.shape == song.shape, "输出形状不对"
        assert 0.005 < v_rms < m_rms * 1.5, f"{kind} 人声能量异常"
        assert a_rms > 0.005, f"{kind} 伴奏能量异常"

    # 两模型输出应相关但不完全相同（不同模型）
    print("PASS: mel 加载/分离/能量 sanity 全过",
          {k: f"{v[0]:.1f}s" for k, v in results.items()})


if __name__ == "__main__":
    main()
