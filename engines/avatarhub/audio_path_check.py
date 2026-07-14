# -*- coding: utf-8 -*-
"""
声卡链路自检(一键通话模式的子进程探针)。独立进程跑的原因：
1) 干净的 PortAudio 状态——默认设备刚被切换过时,宿主进程的设备快照可能已失效;
2) sounddevice 在中文 Windows 上会把 GBK 错误文本按 utf-8 解码,异常被掩盖,隔离在子进程里不拖垮服务。

用法:
    python audio_path_check.py cable <play_out_index> [device_name] [hostapi]  # CABLE 通路:放测试音→录 CABLE Output,量峰值
    python audio_path_check.py mic <mic_index> [device_name] [hostapi]         # 麦克风:开流录 0.4s,报底噪
    python audio_path_check.py cablewav <play_out_index> <wav_path> [device_name] [hostapi]  # 端到端试音:把真实合成音双工推入 CABLE Input→录 CABLE Output,量峰值
    python audio_path_check.py reccable <seconds> [_] [hostapi]                # 仅回录 CABLE Output N 秒(运行中经播放队列推音时用),报峰值
device_name 非空时在本进程自己的设备快照里按名重解析(父子进程各自初始化 PortAudio,
索引可能对不上——实测父进程的 BRIO 索引在子进程里是 Voicemeeter B3 静音口)。
hostapi 非空时只在该宿主API里解析——父进程按名换用 WASAPI/DS 备选实例时,
子进程不得再漂回 MME(实测 MME 实例会整机"坏死"读数字静音,WASAPI 同名实例却正常)。
输出: 单行 JSON
"""
import sys, json, time, threading

import numpy as np
import sounddevice as sd


def resolve_by_name(name_sub: str, want_output: bool, fallback_idx: int, hostapi: str = "") -> int:
    """按名在本进程快照重解析设备。MME>WASAPI>DS,排除 WDM-KS;找不到回退传入索引。
    MME 名截断到 31 字符 → 用短边前缀互相匹配。hostapi 非空=只认该宿主API。"""
    if not name_sub:
        return fallback_idx
    pref = {"MME": 0, "Windows WASAPI": 1, "Windows DirectSound": 2}
    ns = (name_sub or "").strip().lower()[:28]
    cands = []
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_output_channels"] if want_output else d["max_input_channels"]
        if ch <= 0:
            continue
        try:
            ha = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            continue
        if ha not in pref or (hostapi and ha != hostapi):
            continue
        nm = (d["name"] or "").strip().lower()[:28]
        if nm and (nm.startswith(ns) or ns.startswith(nm)):
            cands.append((pref[ha], i))
    cands.sort()
    return cands[0][1] if cands else fallback_idx


def _cable_output_rec_idx() -> int:
    """找 CABLE Output 的录音端索引(微信/TG 的收音口)。MME>DS>WASAPI;找不到回 -1。"""
    pref = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "cable output" in d["name"].lower():
            ha = sd.query_hostapis(d["hostapi"])["name"]
            if ha in pref:
                cands.append((pref[ha], i))
    cands.sort()
    return cands[0][1] if cands else -1


def _duplex_peak(buf: np.ndarray, sr: int, rec_idx: int, play_idx: int) -> float:
    """单条双工流:把 buf 放进 play_idx、同时录 rec_idx,返回录到的峰值 dBFS。
    P3 实测坑:旧法"先开录音流,再开播放流"必假阴——VB-Cable 在新 render 客户端接入瞬间
    重锁时钟,已开的 MME 录音流被饿死(录到硬零)。playrec 同一时钟同时收发才稳。线程法降级兜底。"""
    x = None
    try:
        x = sd.playrec(buf, sr, device=(rec_idx, play_idx), channels=1, dtype="float32")
        sd.wait()
    except Exception:
        got = []

        def _rec():
            r = sd.rec(len(buf) + int(sr * 0.3), samplerate=sr, channels=1, device=rec_idx, dtype="float32")
            sd.wait(); got.append(r)

        th = threading.Thread(target=_rec); th.start()
        time.sleep(0.25)
        sd.play(buf.reshape(-1), sr, device=play_idx); sd.wait()
        th.join(timeout=max(4, int(len(buf) / sr) + 3))
        x = got[0] if got else None
    # 首块偶见 inf/NaN(设备刚被切默认后的瞬态)——非有限样本一律清零,防伪高峰值
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0) if x is not None else np.zeros(1, np.float32)
    x = np.clip(x, -1.0, 1.0)
    return 20.0 * float(np.log10(float(np.abs(x).max()) + 1e-9))


def check_cable(play_idx: int) -> dict:
    rec_idx = _cable_output_rec_idx()
    if rec_idx < 0:
        return {"ok": False, "detail": "未找到 CABLE Output 录音端"}
    sr = int(sd.query_devices(rec_idx).get("default_samplerate") or 44100)
    tone = (0.4 * np.sin(2 * np.pi * 440 * np.arange(int(sr * 0.5)) / sr)).astype(np.float32)
    buf = np.vstack([tone.reshape(-1, 1), np.zeros((int(sr * 0.4), 1), np.float32)])
    peak_db = _duplex_peak(buf, sr, rec_idx, play_idx)
    return {"ok": peak_db > -30.0, "peak_dbfs": round(peak_db, 1),
            "detail": f"CABLE通路 peak={peak_db:.1f}dBFS ({'通畅' if peak_db > -30 else '不通'})"}


def _load_wav_mono(path: str):
    """WAV → (float32 单声道, sr)。只用标准库 wave,子进程无需额外依赖。"""
    import wave
    with wave.open(path, "rb") as w:
        ch, sw, wsr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        data = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        a = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, wsr


def check_cable_wav(play_idx: int, wav_path: str) -> dict:
    """端到端试音:把"真实克隆合成音"双工推入 CABLE Input、同时录 CABLE Output,量峰值。
    比 440Hz 测试音更强——验的是真实配音波形能否原样到达对方收音口。"""
    rec_idx = _cable_output_rec_idx()
    if rec_idx < 0:
        return {"ok": False, "detail": "未找到 CABLE Output 录音端"}
    try:
        x, wsr = _load_wav_mono(wav_path)
    except Exception as e:
        return {"ok": False, "detail": f"读取合成音失败: {str(e)[:120]}"}
    if x.size == 0:
        return {"ok": False, "detail": "合成音为空(TTS 未产出)"}
    sr = int(sd.query_devices(rec_idx).get("default_samplerate") or wsr or 44100)
    if wsr != sr:                                   # 线性重采样到录音端采样率(子进程无 scipy)
        n = max(1, int(round(len(x) * sr / wsr)))
        x = np.interp(np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x).astype(np.float32)
    x = np.clip(np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0), -1.0, 1.0)
    buf = np.vstack([x.reshape(-1, 1), np.zeros((int(sr * 0.4), 1), np.float32)])
    peak_db = _duplex_peak(buf, sr, rec_idx, play_idx)
    return {"ok": peak_db > -30.0, "peak_dbfs": round(peak_db, 1),
            "detail": f"配音到 CABLE peak={peak_db:.1f}dBFS ({'对方能听到' if peak_db > -30 else '几乎无声'})"}


def check_record_cable(seconds: float) -> dict:
    """仅回录 CABLE Output N 秒并量峰值(运行中经播放队列推音时用,不与播放线程抢输出流)。"""
    rec_idx = _cable_output_rec_idx()
    if rec_idx < 0:
        return {"ok": False, "detail": "未找到 CABLE Output 录音端"}
    sr = int(sd.query_devices(rec_idx).get("default_samplerate") or 44100)
    dur = max(0.5, min(8.0, seconds))
    try:
        r = sd.rec(int(sr * dur), samplerate=sr, channels=1, device=rec_idx, dtype="float32")
        sd.wait()
    except Exception as e:
        return {"ok": False, "detail": f"回录 CABLE 失败: {str(e)[:120]}"}
    x = np.clip(np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0), -1.0, 1.0)
    peak_db = 20.0 * float(np.log10(float(np.abs(x).max()) + 1e-9))
    return {"ok": peak_db > -40.0, "peak_dbfs": round(peak_db, 1),
            "detail": f"CABLE 回录 peak={peak_db:.1f}dBFS"}


def check_mic(mic_idx: int) -> dict:
    dev = sd.query_devices(mic_idx)
    sr = int(dev.get("default_samplerate") or 48000)
    ch = min(2, max(1, int(dev.get("max_input_channels") or 1)))   # WASAPI 实例常为 2ch,强开 1ch 会报错
    # 静音复测：默认设备刚切换/引擎重建的瞬间,首开流常读全零(实测同一麦 0.4s 探针 -96.7,
    # 几秒后 -64.6 正常)。读到数字静音不立刻定罪,间隔重试并拉长录音窗,三次全零才报故障。
    rms_db = -999.0
    for attempt in range(3):
        dur = 0.4 if attempt == 0 else 0.8
        r = sd.rec(int(sr * dur), samplerate=sr, channels=ch, device=mic_idx, dtype="float32")
        sd.wait()
        x = np.clip(np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0), -1.0, 1.0)
        rms_db = 20.0 * float(np.log10(float(np.sqrt((x ** 2).mean())) + 1e-9))
        # 数字静音检测：真麦克风经 Windows 增益后底噪总在 -80dBFS 以上;≤-85 基本是"开错设备"
        # (虚拟声卡静音口/被独占/物理静音)。联测实测踩坑:索引漂移开到 CABLE Output,-96.7 仍报"正常"。
        if rms_db > -85.0:
            return {"ok": True, "noise_dbfs": round(rms_db, 1),
                    "detail": f"麦克风正常 (底噪 {rms_db:.1f}dBFS)" + (f"，第{attempt+1}次复测通过" if attempt else "")}
        time.sleep(0.6)
    return {"ok": False, "noise_dbfs": round(rms_db, 1),
            "detail": f"采到的是数字静音({rms_db:.1f}dBFS,三次复测)——疑似开错设备(索引漂移)/麦被占用或静音,"
                      f"当前开的是 '{dev.get('name', '?')}'"}


def main():
    kind = sys.argv[1]
    try:
        if kind == "cablewav":
            # cablewav <play_idx> <wav_path> [name] [hostapi]
            idx = int(sys.argv[2]); wav_path = sys.argv[3]
            name = sys.argv[4] if len(sys.argv) > 4 else ""
            hostapi = sys.argv[5] if len(sys.argv) > 5 else ""
            idx = resolve_by_name(name, want_output=True, fallback_idx=idx, hostapi=hostapi)
            out = check_cable_wav(idx, wav_path)
        elif kind == "reccable":
            # reccable <seconds> ...  (录音端在函数内自解析,无需索引)
            out = check_record_cable(float(sys.argv[2]))
        else:
            idx = int(sys.argv[2])
            name = sys.argv[3] if len(sys.argv) > 3 else ""
            hostapi = sys.argv[4] if len(sys.argv) > 4 else ""
            idx = resolve_by_name(name, want_output=(kind == "cable"), fallback_idx=idx, hostapi=hostapi)
            out = check_cable(idx) if kind == "cable" else check_mic(idx)
    except Exception as e:
        out = {"ok": False, "detail": f"{kind} 自检异常: {str(e)[:140]}"}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
