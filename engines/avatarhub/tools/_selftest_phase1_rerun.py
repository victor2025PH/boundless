# -*- coding: utf-8 -*-
"""单独重跑自测阶段1(注入式管线,无声学变量)。素材复用 logs/_selftest_wavs。"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _selftest_interp as st

wav_dir = os.path.join(st.BASE, "logs", "_selftest_wavs")
wavs = [st.load_wav16k(os.path.join(wav_dir, f"zh_{i}.wav"))
        for i in range(len(st.ZH_SENTENCES))]
try:
    st.phase1_injection(wavs)
finally:
    st.api("POST", "/voicelock/reset")
    st.api("POST", "/stop")
    out = os.path.join(st.BASE, "logs", "selftest_phase1_rerun.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(st.REPORT, f, ensure_ascii=False, indent=2)
    st.log(f"阶段1重跑报告: {out}")
