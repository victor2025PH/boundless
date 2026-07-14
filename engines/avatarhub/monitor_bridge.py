# -*- coding: utf-8 -*-
"""
监听桥 monitor_bridge.py —— 把"发给直播软件的克隆语音"实时送进蓝牙耳机做监听。

背景：live_interpreter 把克隆英文写入 CABLE Input（→直播软件当麦克风用 CABLE Output）。
      蓝牙耳机(EDIFIER)只在 WASAPI 层可用，PortAudio/sounddevice 打不开(报 -9999)，
      所以这里用 soundcard(WASAPI)：采集 CABLE Output → 播放到耳机，实现"监听自动到耳机"。

链路：CABLE Output (VB-Audio Virtual Cable)  →  耳机 (EDIFIER EvoBuds Pro)
      采集线程持续读 → 环形队列 → 播放线程持续写，两端时钟解耦，避免卡顿/断续。
      按设备名子串匹配；采集设备找不到就重试，播放设备找不到就退回系统默认扬声器；断线自动重连。

环境变量(可选)：
  MON_SRC   采集设备名子串   默认 "CABLE Output"
  MON_DST   播放设备名子串   默认 "EDIFIER"（留空=系统默认扬声器）
  MON_SR    采样率           默认 48000
  MON_BLOCK 块大小(帧)       默认 1024（越小延迟越低，越大越稳）
  MON_GAIN  监听增益         默认 1.0
  MON_QUEUE 缓冲块数上限     默认 8（越大越稳、延迟越高）
"""
import os
import time
import queue
import threading
import warnings
import numpy as np
import soundcard as sc

try:
    from soundcard import SoundcardRuntimeWarning
    warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
except Exception:
    warnings.filterwarnings("ignore", message=".*discontinuity.*")

SRC = os.environ.get("MON_SRC", "CABLE Output")
DST = os.environ.get("MON_DST", "EDIFIER")
SR = int(os.environ.get("MON_SR", "48000"))
BLK = int(os.environ.get("MON_BLOCK", "1024"))
GAIN = float(os.environ.get("MON_GAIN", "1.0"))
QMAX = int(os.environ.get("MON_QUEUE", "8"))


def _find_mic(sub: str):
    subl = (sub or "").lower()
    for m in sc.all_microphones(include_loopback=True):
        if subl in m.name.lower():
            return m
    return None


def _find_spk(sub: str):
    """DST 非空时严格按名匹配（找不到返回 None，交由上层等待重试，绝不误退到默认设备）；
    DST 为空时才用系统默认扬声器。"""
    subl = (sub or "").lower()
    if not subl:
        return sc.default_speaker()
    for s in sc.all_speakers():
        if subl in s.name.lower():
            return s
    return None


def _producer(mic, q: "queue.Queue", stop_ev: threading.Event):
    try:
        with mic.recorder(samplerate=SR, channels=1, blocksize=BLK) as rec:
            while not stop_ev.is_set():
                data = rec.record(numframes=BLK)
                try:
                    q.put_nowait(data)
                except queue.Full:
                    try:          # 丢最旧一块，保持低延迟
                        q.get_nowait()
                        q.put_nowait(data)
                    except queue.Empty:
                        pass
    except Exception as e:
        print(f"[monitor] 采集线程中断：{e}", flush=True)
    finally:
        stop_ev.set()


def main():
    print(f"[monitor] 启动：采集~='{SRC}'  播放~='{DST or '默认扬声器'}'  sr={SR} block={BLK} gain={GAIN} q={QMAX}", flush=True)
    logged = 0.0
    while True:
        try:
            mic = _find_mic(SRC)
            if mic is None:
                print(f"[monitor] 未找到采集设备(含 '{SRC}')，2s 后重试…", flush=True)
                time.sleep(2)
                continue
            spk = _find_spk(DST)
            if spk is None:
                print(f"[monitor] 等待耳机(含 '{DST}')连接…（请戴上/唤醒耳机；连上后自动开始）", flush=True)
                time.sleep(2)
                continue
            print(f"[monitor] 采集 = '{mic.name}'  →  播放 = '{spk.name}'", flush=True)

            q: "queue.Queue" = queue.Queue(maxsize=QMAX)
            stop_ev = threading.Event()
            t = threading.Thread(target=_producer, args=(mic, q, stop_ev), daemon=True)
            t.start()

            silence = np.zeros((BLK, 2), dtype=np.float32)
            with spk.player(samplerate=SR, channels=2, blocksize=BLK) as ply:
                print("[monitor] 监听进行中（耳机可听到克隆输出）", flush=True)
                while not stop_ev.is_set():
                    try:
                        data = q.get(timeout=0.2)
                        rms = float(np.sqrt(np.mean(np.square(data))))
                        if GAIN != 1.0:
                            data = data * GAIN
                        if data.ndim == 2 and data.shape[1] == 1:
                            data = np.repeat(data, 2, axis=1)
                        ply.play(data)
                    except queue.Empty:
                        ply.play(silence)   # 空闲时持续送静音，防止蓝牙耳机休眠掉线
                        rms = 0.0
                    now = time.time()
                    if now - logged >= 3.0:
                        logged = now
                        print(f"[monitor] rms={rms:.4f} q={q.qsize()}", flush=True)

            stop_ev.set()
            t.join(timeout=1.0)
        except KeyboardInterrupt:
            print("[monitor] 已退出", flush=True)
            return
        except Exception as e:
            print(f"[monitor] 中断：{e} —— 1.5s 后重连", flush=True)
            time.sleep(1.5)


if __name__ == "__main__":
    main()
