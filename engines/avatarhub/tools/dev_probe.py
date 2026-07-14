# -*- coding: utf-8 -*-
"""
设备探测子进程（P10 Hub 稳定性收口，2026-07-06）

背景：sounddevice/PortAudio 与 DirectShow(pygrabber/cv2 CAP_DSHOW) 都会把第三方驱动
DLL(DroidCam 虚拟声卡、SplitCam virtualcam_x64.dll 等)加载进调用进程——Windows 事件日志
实锤过 0xc0000409/0xc0000005 原生崩溃直接带走整个 Hub(watchdog 拉回也有 ~20s 盲区，
用户端表现即「试音 Failed to fetch」)。本脚本把这些原生操作隔离成一跑一收的子进程：
崩溃只损失这一次探测，Hub 本体永不因设备栈而死。

协议：stdout 最后一行输出一个 JSON（其余行忽略；C++ 层警告走 stderr 不干扰）。
所有子命令的返回都带 ok 字段；异常统一 {ok:false, detail}。

子命令：
  audio_devices                         枚举音频输入/输出（带 hostapi 后缀，与 RVC 命名一致）
  resolve --name X --kind in|out        设备名 → sounddevice 索引（找不到= null=系统默认）
  mic_rec --secs 3 --device X --wav 1   解析设备名并录音，回电平指标(+可选 wav_b64)
  output_test --secs 1 --device X       播提示音；输出名含 cable 时并行录 CABLE Output 自证回环
  cameras --max 8                       DirectShow 设备名 + cv2 逐索引开测分辨率
  named_cameras                         仅 DirectShow 设备名（不开设备，最快）
  pick_camera_source --adb 0|1 --probe 0|1   最佳视频源决策（device_enum 单一真相）
  probe_live --index N                  指定摄像头索引活帧探测
  annotate_cameras --probe 0|1          带分类(+可选活帧)的摄像头列表
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 保证能 import 仓库根目录的 device_enum（本文件在 tools/ 下）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── 音频 ────────────────────────────────────────────────────────────────


def audio_devices() -> dict:
    """本地枚举音频输入/输出设备。名字带 hostapi 后缀(如 '... (MME)')，
    与前端 .includes('MME')/'CABLE Input' 匹配保持一致。"""
    import sounddevice as sd
    apis = sd.query_hostapis()
    ins, outs, seen_i, seen_o = [], [], set(), set()
    for d in sd.query_devices():
        try:
            api = apis[d["hostapi"]]["name"]
        except Exception:
            api = ""
        nm = f'{d["name"]} ({api})' if api else d["name"]
        if d.get("max_input_channels", 0) > 0 and nm not in seen_i:
            ins.append(nm); seen_i.add(nm)
        if d.get("max_output_channels", 0) > 0 and nm not in seen_o:
            outs.append(nm); seen_o.add(nm)
    return {"ok": True, "inputs": ins, "outputs": outs}


def resolve_audio_device(name: str, kind: str = "in"):
    """RVC 风格设备名('基名 (MME)') → sounddevice 设备索引；空名/找不到返回 None(=系统默认)。
    匹配顺序：全名精确 → 基名精确/前缀(容 MME 31 字符截断)，hostapi 一致者优先，MME 次优。"""
    if not name or name.startswith("LOOPBACK:"):
        return None
    try:
        import sounddevice as sd
        import device_enum
        want_base, want_api = device_enum.parse_audio_name(name)
        apis = sd.query_hostapis()
        cands = []
        for i, d in enumerate(sd.query_devices()):
            ch = d.get("max_input_channels" if kind == "in" else "max_output_channels", 0)
            if ch <= 0:
                continue
            try:
                api = apis[d["hostapi"]]["name"]
            except Exception:
                api = ""
            full = f'{d["name"]} ({api})' if api else d["name"]
            if full == name:
                return i
            b = d["name"]
            if b == want_base or b.startswith(want_base) or want_base.startswith(b):
                rank = 0 if (api and api == want_api) else (1 if api == "MME" else 2)
                cands.append((rank, i))
        if cands:
            cands.sort()
            return cands[0][1]
    except Exception:
        pass
    return None


def mic_rec(secs: float, device_name: str = "", want_wav: bool = False) -> dict:
    """解析设备名并录 secs 秒(16k 单声道)：噪声底(P20)/说话电平(P95)/峰值/SNR (+可选 wav_b64)。"""
    import numpy as np
    import sounddevice as sd
    idx = resolve_audio_device(device_name, "in")
    sr = 16000
    rec = sd.rec(int(secs * sr), samplerate=sr, channels=1, dtype="float32", device=idx)
    sd.wait()
    x = np.asarray(rec).reshape(-1)
    if x.size < sr // 2:
        return {"ok": False, "detail": "采样过短"}
    frame = int(sr * 0.05)
    n = max(1, x.size // frame)
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    res = {"ok": True, "resolved_index": idx, "secs": secs,
           "floor_dbfs": round(float(np.percentile(db, 20)), 1),
           "speech_dbfs": round(float(np.percentile(db, 95)), 1),
           "peak_dbfs": round(float(db.max()), 1),
           "snr_db": round(float(np.percentile(db, 95) - np.percentile(db, 20)), 1)}
    if want_wav:
        import base64
        import io
        import wave
        pcm = np.clip(x * 32767.0, -32768, 32767).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)
        res["wav_b64"] = base64.b64encode(buf.getvalue()).decode()
    return res


def output_test(secs: float, device_name: str = "") -> dict:
    """向选中(或默认)输出播「叮」提示音；输出名含 cable 时并行录 CABLE Output 自证回环。
    sd.play/sd.rec 便捷函数共享全局流(互相顶掉)，回环录音用独立 InputStream。"""
    import time as _time

    import numpy as np
    import sounddevice as sd
    idx = resolve_audio_device(device_name, "out")
    want_probe = "cable" in (device_name or "").lower()
    try:
        info = sd.query_devices(idx if idx is not None else sd.default.device[1], "output")
        sr = int(info.get("default_samplerate") or 44100)
        ch = 2 if int(info.get("max_output_channels") or 1) >= 2 else 1
    except Exception:
        sr, ch = 44100, 1
    t = np.linspace(0.0, secs, int(sr * secs), endpoint=False)
    env = np.exp(-3.0 * t / secs)
    tone = (0.4 * env * (0.6 * np.sin(2 * np.pi * 660.0 * t)
                         + 0.4 * np.sin(2 * np.pi * 880.0 * t))).astype("float32")
    data = np.column_stack([tone, tone]) if ch == 2 else tone
    probe = None
    in_stream = None
    frames = []
    if want_probe:
        probe_idx = None
        try:
            apis = sd.query_hostapis()
            best = None
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) <= 0:
                    continue
                if "cable output" not in (d.get("name") or "").lower():
                    continue
                try:
                    api = apis[d["hostapi"]]["name"]
                except Exception:
                    api = ""
                rank = 0 if api == "MME" else 1
                if best is None or rank < best[0]:
                    best = (rank, i)
            probe_idx = best[1] if best else None
        except Exception:
            probe_idx = None
        probe = {"device_index": probe_idx}
        if probe_idx is not None:
            try:
                def _cb(indata, _n, _t, _status):
                    frames.append(indata.copy())
                in_stream = sd.InputStream(samplerate=16000, channels=1,
                                           dtype="float32", device=probe_idx,
                                           callback=_cb)
                in_stream.start()
            except Exception as pe:
                probe["err"] = str(pe)[:80]
                in_stream = None
    sd.play(data, samplerate=sr, device=idx)
    sd.wait()
    if in_stream is not None:
        _time.sleep(0.3)   # 收尾余量：让回环尾音进缓冲
        try:
            in_stream.stop()
            in_stream.close()
        except Exception:
            pass
        xx = (np.concatenate([f.reshape(-1) for f in frames])
              if frames else np.zeros(1, dtype="float32"))
        pk = float(20.0 * np.log10(max(float(np.abs(xx).max()), 1e-9)))
        probe.update(peak_dbfs=round(pk, 1), heard=bool(pk > -45.0))
    res = {"ok": True, "resolved_index": idx, "secs": secs, "samplerate": sr}
    if probe is not None:
        res["probe"] = probe
    return res


# ── 摄像头（DirectShow：加载第三方 filter DLL 的高危区，务必只在本进程跑）──────


def cameras(max_idx: int = 8) -> dict:
    """DirectShow 设备名 + cv2 逐索引开测分辨率（与 OpenCV CAP_DSHOW 索引一致）。"""
    names = {}
    try:
        import device_enum
        for c in device_enum.list_named_cameras():
            names[c["index"]] = c["name"]
    except Exception:
        pass
    found = []
    import cv2
    try:   # 不存在的索引会刷 DSHOW cap.cpp 警告，静默(保留 ERROR)
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:
        pass
    for i in range(max_idx):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            nm = names.get(i, f"摄像头 {i}")
            try:
                import device_enum as _de
                kind = _de.classify_camera(nm)
            except Exception:
                kind = "other"
            found.append({"index": i, "label": nm, "name": nm,
                          "kind": kind, "resolution": f"{w}x{h}"})
            cap.release()
    return {"ok": True, "cameras": found}


def named_cameras() -> dict:
    """仅 DirectShow 设备名列表（不开设备）。"""
    import device_enum
    return {"ok": True, "cameras": device_enum.list_named_cameras()}


def pick_camera_source(adb: bool, probe: bool) -> dict:
    import device_enum
    r = device_enum.pick_camera_source(adb_has_device=adb, probe=probe)
    r["ok"] = True
    return r


def probe_live(index: int) -> dict:
    import device_enum
    return {"ok": True, "live": bool(device_enum._probe_live(index))}


def annotate_cameras(probe: bool) -> dict:
    import device_enum
    return {"ok": True, "cameras": device_enum.annotate_cameras(probe=probe)}


def main() -> int:
    ap = argparse.ArgumentParser(description="设备探测子进程(音频/DirectShow 原生操作隔离)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audio_devices")
    p = sub.add_parser("resolve")
    p.add_argument("--name", default="")
    p.add_argument("--kind", default="in", choices=["in", "out"])
    p = sub.add_parser("mic_rec")
    p.add_argument("--secs", type=float, default=3.0)
    p.add_argument("--device", default="")
    p.add_argument("--wav", type=int, default=0)
    p = sub.add_parser("output_test")
    p.add_argument("--secs", type=float, default=1.0)
    p.add_argument("--device", default="")
    p = sub.add_parser("cameras")
    p.add_argument("--max", type=int, default=8)
    sub.add_parser("named_cameras")
    p = sub.add_parser("pick_camera_source")
    p.add_argument("--adb", type=int, default=0)
    p.add_argument("--probe", type=int, default=1)
    p = sub.add_parser("probe_live")
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("annotate_cameras")
    p.add_argument("--probe", type=int, default=0)
    a = ap.parse_args()
    try:
        if a.cmd == "audio_devices":
            r = audio_devices()
        elif a.cmd == "resolve":
            r = {"ok": True, "index": resolve_audio_device(a.name, a.kind)}
        elif a.cmd == "mic_rec":
            r = mic_rec(max(0.5, min(10.0, a.secs)), a.device, bool(a.wav))
        elif a.cmd == "output_test":
            r = output_test(max(0.4, min(2.0, a.secs)), a.device)
        elif a.cmd == "cameras":
            r = cameras(max(1, min(16, a.max)))
        elif a.cmd == "named_cameras":
            r = named_cameras()
        elif a.cmd == "pick_camera_source":
            r = pick_camera_source(bool(a.adb), bool(a.probe))
        elif a.cmd == "probe_live":
            r = probe_live(a.index)
        else:
            r = annotate_cameras(bool(a.probe))
    except Exception as e:
        r = {"ok": False, "detail": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(r, ensure_ascii=False), flush=True)
    # 交卷即硬退出(P12)：正常 sys.exit 会走解释器收尾→PortAudio/DirectShow 原生 teardown，
    # 第三方驱动(DroidCam 虚拟声卡等)在这一步有实锤崩溃(2026-07-06 18:39 事件日志
    # libportaudio64bit.dll 0xc0000005——结果已交回,纯善终崩)。父进程只读 stdout 不看
    # 返回码,一次性探测进程无需任何清理——os._exit 跳过全部 finalizer,崩溃面归零,
    # 还省掉每次 WerFault 报告的开销。stdout 已 flush,无数据丢失风险。
    os._exit(0 if r.get("ok") else 1)


if __name__ == "__main__":
    sys.exit(main())
