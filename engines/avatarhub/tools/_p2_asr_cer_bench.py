# -*- coding: utf-8 -*-
"""P2 ASR 真值基准：SAPI 合成已知文本 → 分别喂 Nemotron(流式WS,生产同参) 与
Whisper-turbo(/transcribe_b64,带/不带热词) → 计算字符错误率 CER。
作用：给"识别引擎换代"一把客观尺子——今晚先量出 nemotron vs turbo 的差距基线，
将来 Qwen3-ASR 部署后跑同一脚本即知赢没赢(赢流式、逼近离线才值得切)。
注意：真值来自 SAPI 发音,量的是"TTS发音+ASR"整链,作引擎间【相对】比较完全成立。
用法: python tools/_p2_asr_cer_bench.py [--ws ws://IP:7857] [--stt http://IP:7854]
结果: stdout + 追加 logs/optimize_20260707/asr_cer_baseline.jsonl
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "logs" / "optimize_20260707" / "asr_cer_baseline.jsonl"

# 真值语料：术语(在词表,turbo 有热词加持=生产口径) / 数字 / 口语 / 长句
SENTS = [
    "我们现在用通译软件进行实时翻译测试",
    "这款产品现在下单立减五十",
    "货物将在三个工作日内送达",
    "数字人换脸加上声音克隆",
    "今天下午三点开会讨论方案",
    "麻烦把订单的尾款今天之内结一下",
    "你能听清楚我说话吗",
    "我们这套系统延迟能做到一秒出头",
    "直播间的朋友们记得点关注",
    "下一批船期大概在月底",
    "先试用七天满意再付款",
    "把摄像头往左边挪一点",
]
HOTWORDS = "以下是普通话内容，可能出现这些词语：通译、数字人、换脸、声音克隆、直播间。"


def sapi_tts(text: str, out_path: str):
    import win32com.client
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    st = win32com.client.Dispatch("SAPI.SpFileStream")
    st.Format.Type = 22          # 22kHz 16bit mono
    st.Open(out_path, 3)
    sp.AudioOutputStream = st
    for v in sp.GetVoices():
        if "Huihui" in v.GetDescription():
            sp.Voice = v
            break
    sp.Speak(text)
    st.Close()


def load_wav_16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    if sr != 16000:
        t_old = np.linspace(0.0, 1.0, len(pcm), endpoint=False)
        t_new = np.linspace(0.0, 1.0, int(len(pcm) * 16000 / sr), endpoint=False)
        pcm = np.interp(t_new, t_old, pcm).astype(np.float32)
    return pcm


_DIGS = "零一二三四五六七八九"


def _n2h(n: int) -> str:
    """整数→中文读法(≤亿)。CER 计分前把 '50'/'3點' 归一成 '五十'/'三点'，公平比对两引擎。"""
    if n == 0:
        return "零"
    if n >= 10000:
        hi, lo = divmod(n, 10000)
        return _n2h(hi) + "万" + ("" if not lo else ("零" if lo < 1000 else "") + _n2h(lo))
    units = ["", "十", "百", "千"]
    s, out, zero_pending = str(n), "", False
    for i, ch in enumerate(s):
        d = int(ch)
        if d == 0:
            zero_pending = bool(out)
            continue
        if zero_pending:
            out += "零"; zero_pending = False
        out += _DIGS[d] + units[len(s) - 1 - i]
    return out[1:] if out.startswith("一十") else out


def norm(s: str) -> str:
    """计分归一：繁→简(zhconv) + 数字→汉字 + 只留字母/CJK 小写。两引擎同一把尺。"""
    s = s or ""
    try:
        from zhconv import convert
        s = convert(s, "zh-cn")
    except Exception:
        pass
    import re
    s = re.sub(r"\d+", lambda m: _n2h(int(m.group())) if len(m.group()) <= 8 else m.group(), s)
    return "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff").lower()


def cer(ref: str, hyp: str) -> float:
    """字符错误率 = 编辑距离/参考长度(归一化后)。"""
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def stt_turbo(stt_url: str, wav_path: str, prompt: str = "") -> str:
    b = base64.b64encode(open(wav_path, "rb").read()).decode()
    payload = {"audio_base64": b, "language": "zh", "task": "transcribe"}
    if prompt:
        payload["initial_prompt"] = prompt
    r = requests.post(f"{stt_url}/transcribe_b64", json=payload, timeout=60)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


async def stt_nemo(ws_url: str, pcm16k: np.ndarray) -> str:
    """生产同参喂 nemotron：100ms 块 + 尾部 0.7s 静音 + eou，收全部 final 拼接。"""
    import websockets
    uri = f"{ws_url}/ws/transcribe?language=zh&auto_eou=1&sil_ms=500&min_voice_ms=300"
    audio = np.concatenate([pcm16k, np.zeros(int(16000 * 0.7), np.float32)])
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    finals = []
    async with websockets.connect(uri, max_size=None, ping_interval=20) as ws:
        step = 3200  # 100ms @16k PCM16
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step])
            await asyncio.sleep(0.02)      # 轻微节流,近似实时流(全速灌会触发服务端缓冲策略)
        await ws.send(json.dumps({"event": "eou"}))
        quiet = 0.0
        while quiet < 2.0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                quiet += 0.5
                continue
            except Exception:
                break
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("final"):
                finals.append(m["final"].strip())
                quiet = 0.0
    return "".join(finals)


def degrade(pcm: np.ndarray, cond: str) -> np.ndarray:
    """P3 劣化条件：noise=叠加白噪声(SNR≈10dB) quiet=整体压 -24dB(轻声说话)
    noisy_quiet=两者叠加(最恶劣)。clean=原样。真实通话的劣化以这三类为主。"""
    x = pcm.copy()
    if cond in ("quiet", "noisy_quiet"):
        x = x * (10.0 ** (-24.0 / 20.0))
    if cond in ("noise", "noisy_quiet"):
        sig = float(np.sqrt(np.mean(x ** 2)) + 1e-9)
        n = np.random.default_rng(7).standard_normal(len(x)).astype(np.float32)
        n *= sig / (10.0 ** (10.0 / 20.0)) / float(np.sqrt(np.mean(n ** 2)) + 1e-9)
        x = x + n
    return np.clip(x, -1.0, 1.0)


def save_wav_16k(pcm: np.ndarray, path: str):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=os.environ.get("NEMO_WS_URL", "ws://192.168.0.140:7857"))
    ap.add_argument("--stt", default=os.environ.get("STT_URL", "http://192.168.0.140:7854"))
    ap.add_argument("--tag", default="", help="引擎标注(如 qwen3-asr 换代后复测用)")
    ap.add_argument("--conds", default="clean", help="逗号分隔: clean,noise,quiet,noisy_quiet")
    ap.add_argument("--hot", action="store_true", help="turbo 加测热词版(仅 clean 有意义)")
    args = ap.parse_args()
    conds = [c.strip() for c in args.conds.split(",") if c.strip()]

    tmp = os.path.join(tempfile.gettempdir(), "_cer_bench.wav")
    tmp2 = os.path.join(tempfile.gettempdir(), "_cer_bench_deg.wav")
    all_res = []
    for cond in conds:
        rows = []
        sums = {"nemo": [], "turbo": [], "turbo_hot": []}
        print(f"\n== ASR CER 基准 [{cond}] ({len(SENTS)} 句) ==  ws={args.ws}")
        for text in SENTS:
            sapi_tts(text, tmp)
            pcm = degrade(load_wav_16k(tmp), cond)
            save_wav_16k(pcm, tmp2)
            try:
                nemo = asyncio.run(stt_nemo(args.ws, pcm))
            except Exception as e:
                nemo = f"<ERR {e}>"
            try:
                tb = stt_turbo(args.stt, tmp2)
                th = stt_turbo(args.stt, tmp2, HOTWORDS) if args.hot else ""
            except Exception as e:
                tb = th = f"<ERR {e}>"
            row = {"ref": text,
                   "nemo": nemo, "cer_nemo": round(cer(text, nemo), 3),
                   "turbo": tb, "cer_turbo": round(cer(text, tb), 3)}
            if args.hot:
                row["turbo_hot"] = th; row["cer_turbo_hot"] = round(cer(text, th), 3)
            rows.append(row)
            for k, ck in (("nemo", "cer_nemo"), ("turbo", "cer_turbo"), ("turbo_hot", "cer_turbo_hot")):
                if k in row and not str(row[k]).startswith("<ERR"):
                    sums[k].append(row[ck])
            print(f"  参考: {text}")
            print(f"    nemo   {row['cer_nemo']:.0%}  {nemo}")
            print(f"    turbo  {row['cer_turbo']:.0%}  {tb}")
            if args.hot:
                print(f"    turbo+热词 {row['cer_turbo_hot']:.0%}  {th}")
        avg = {k: (round(float(np.mean(v)), 4) if v else None) for k, v in sums.items()}
        print(f"== [{cond}] 平均 CER ==  nemotron {avg['nemo']:.1%} · turbo {avg['turbo']:.1%}"
              + (f" · turbo+热词 {avg['turbo_hot']:.1%}" if avg.get("turbo_hot") is not None else ""))
        res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": args.tag or "baseline",
               "cond": cond, "ws": args.ws, "n": len(SENTS), "avg_cer": avg, "rows": rows}
        all_res.append(res)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        for res in all_res:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
    print(f"\n结果已追加 {OUT}")


if __name__ == "__main__":
    main()
