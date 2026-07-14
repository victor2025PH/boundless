# -*- coding: utf-8 -*-
"""单独重跑自测阶段3(长跑稳定性)。素材复用 logs/_selftest_wavs。"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _selftest_interp as st

wav_dir = os.path.join(st.BASE, "logs", "_selftest_wavs")
wavs = [st.load_wav16k(os.path.join(wav_dir, f"zh_{i}.wav"))
        for i in range(len(st.ZH_SENTENCES))]
try:
    st.phase3_longrun(wavs, minutes=float(os.environ.get("SELFTEST_LONGRUN_MIN", "8")))
finally:
    st.api("POST", "/voicelock/reset")
    st.api("POST", "/stop")
    out = os.path.join(st.BASE, "logs", "selftest_phase3_rerun.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(st.REPORT, f, ensure_ascii=False, indent=2)
    st.log(f"阶段3重跑报告: {out}")
