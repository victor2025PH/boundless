# -*- coding: utf-8 -*-
"""P0 GER 纠错闭环端到端验证(无需真人说话)：
用 Windows SAPI 合成两段中文语音 → 直接调用运行中解释器的内部管线?——不行(跨进程)。
改为黑盒法:合成语音 → 经 STT /transcribe_b64 确认音频可识别 → 把音频喂给解释器的
虚拟麦?太重。此处做次优但真实的链路验证:
  1) /transcribe_b64(turbo) 带热词 initial_prompt 的识别质量(P0-②③)
  2) 解释器 /events 流:观察 GER 事件字段兼容(等真人实测)
  3) GER 观测计数基线
用法: python tools/_p0_ger_e2e.py
"""
import sys, io, json, time, base64, wave
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

INTERP = "http://127.0.0.1:7900"
STT = "http://192.168.0.140:7854"


def sapi_tts(text: str, out_path: str):
    """Windows SAPI 合成中文 wav(22k/16bit)。"""
    import win32com.client
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    st = win32com.client.Dispatch("SAPI.SpFileStream")
    st.Format.Type = 22          # SAFT22kHz16BitMono
    st.Open(out_path, 3)
    sp.AudioOutputStream = st
    for v in sp.GetVoices():
        if "Huihui" in v.GetDescription():
            sp.Voice = v
            break
    sp.Speak(text)
    st.Close()


def transcribe(path: str, prompt: str = ""):
    b = base64.b64encode(open(path, "rb").read()).decode()
    payload = {"audio_base64": b, "language": "zh", "task": "transcribe"}
    if prompt:
        payload["initial_prompt"] = prompt
    t = time.time()
    r = requests.post(f"{STT}/transcribe_b64", json=payload, timeout=60)
    j = r.json()
    return j.get("text", ""), time.time() - t, j


def main():
    import tempfile, os
    tests = [
        "我们用通译软件进行实时翻译",
        "数字人换脸加上声音克隆",
        "今天下午三点开会讨论方案",
    ]
    prompt = "以下是普通话内容，可能出现这些词语：通译、数字人、换脸、声音克隆。"
    print("== P0-②③ turbo+热词识别验证 ==")
    for t in tests:
        p = os.path.join(tempfile.gettempdir(), "_ger_test.wav")
        sapi_tts(t, p)
        txt0, el0, _ = transcribe(p)
        txt1, el1, _ = transcribe(p, prompt)
        print(f"  原文: {t}")
        print(f"  无热词: {txt0}  ({el0:.2f}s)")
        print(f"  带热词: {txt1}  ({el1:.2f}s)")
    print()
    print("== 解释器 GER 观测 ==")
    m = requests.get(f"{INTERP}/metrics", timeout=5).json()
    ah = m.get("audio_health") or {}
    print("  ger:", json.dumps(ah.get("ger"), ensure_ascii=False))
    print("  drops:", json.dumps(ah.get("drops"), ensure_ascii=False))
    print("  running:", m.get("running"), "stream:", m.get("stream_on"))


if __name__ == "__main__":
    main()
