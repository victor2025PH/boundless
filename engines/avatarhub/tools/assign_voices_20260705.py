# -*- coding: utf-8 -*-
"""2026-07-05 角色补声：无克隆音角色统一借用刘德华/杰森斯坦森参考音，刘亦菲用官方 AI 女声。

映射（用户指定）：
  古天乐 / 张一健 / 葛优 / 阿龙2  → 刘德华参考音（中文男声）
  皮特                          → 杰森斯坦森参考音（英文男声）
  刘亦菲                        → CosyVoice 官方演示女声（阿里官方 AI 音源，非真人克隆）

做法：PATCH /profiles/{name} 写 voice_b64 + tts_engine=fish_speech + fish_tts_params
（reference_text 必须与参考音逐字对应，克隆质量才有保障；seed 固定降延迟抖动）。
PATCH 会自动触发 voice preview 重建（P4-D）。
"""
import base64
import io
import json
import sys

import requests
import soundfile as sf
import numpy as np

HUB = "http://127.0.0.1:9000"

FISH_PARAMS_BASE = {"temperature": 0.7, "top_p": 0.7, "repetition_penalty": 1.2,
                    "chunk_length": 200, "seed": 123}


def donor_voice(name: str) -> tuple[str, str]:
    """取捐赠角色的 voice_b64 与 reference_text。"""
    p = requests.get(f"{HUB}/profiles/{name}", params={"include_face": "true"}, timeout=15).json()
    b64 = p.get("voice_b64", "")
    ref_text = (p.get("fish_tts_params") or {}).get("reference_text", "")
    assert b64, f"{name} 无参考音"
    return b64, ref_text


def official_female_voice() -> tuple[str, str]:
    """CosyVoice 官方资产女声 → 转 16bit PCM mono WAV（float32 原格式部分解码器不认）。"""
    data, sr = sf.read(r"CosyVoice/asset/zero_shot_prompt.wav", dtype="float32", always_2d=False)
    if getattr(data, "ndim", 1) > 1:
        data = data.mean(axis=1)
    buf = io.BytesIO()
    sf.write(buf, np.asarray(data), sr, format="WAV", subtype="PCM_16")
    # CosyVoice 官方 demo 参考文本（与音频逐字对应）
    return base64.b64encode(buf.getvalue()).decode(), "希望你以后能够做的比我还好呦。"


def main():
    andy_b64, andy_text = donor_voice("刘德华")
    jason_b64, jason_text = donor_voice("杰森斯坦森")
    female_b64, female_text = official_female_voice()
    print(f"捐赠音就绪: 刘德华({len(andy_b64)}b64) 杰森({len(jason_b64)}b64) 官方女声({len(female_b64)}b64)")

    plan = [
        ("古天乐",  andy_b64,  andy_text),
        ("张一健",  andy_b64,  andy_text),
        ("葛优",    andy_b64,  andy_text),
        ("阿龙2",   andy_b64,  andy_text),
        ("皮特",    jason_b64, jason_text),
        ("刘亦菲",  female_b64, female_text),
    ]
    failed = []
    for name, b64, ref_text in plan:
        body = {
            "voice_b64": b64,
            "tts_engine": "fish_speech",
            "fish_tts_params": dict(FISH_PARAMS_BASE, reference_text=ref_text),
        }
        r = requests.patch(f"{HUB}/profiles/{name}", json=body, timeout=30)
        ok = r.status_code == 200 and r.json().get("ok")
        print(f"  {name}: {'OK' if ok else 'FAIL ' + r.text[:100]}")
        if not ok:
            failed.append(name)

    # 验证
    d = requests.get(f"{HUB}/profiles", timeout=10).json()
    novoice = [p["name"] for p in d["profiles"] if not p.get("has_voice")]
    print(f"\n复查: 仍无声角色 = {novoice or '无'} | active = {d.get('active')}")
    sys.exit(1 if (failed or novoice) else 0)


if __name__ == "__main__":
    main()
