# -*- coding: utf-8 -*-
"""手机麦 → 小界语音指挥 接线桥（C1.5 · 2026-08-18 电视台化方案 P1-10）。

出处：《U型舰桥_多目视觉与环幕CAVE_五视角方案_20260813.md》C1.5 挂账
「中继 WebRTC 已有 mic 通道，接到 voice_gateway 的管线要一次接线验证」。

架构（刻意零改 monitor_relay=零同传重启风险，AGENTS 纪律）：
  手机(WebRTC/WS 上麦) → monitor_relay :7878 既有 /mic/pcm 出口（tap 扇出，
  PCM16 单声道 @ X-Sample-Rate 头，20ms 帧，空闲发静音保活）
  → 本桥（纯标准库：3:1 均值抽取重采样 48k→16k，128ms 攒块）
  → hud_server :7913 /api/voice/stream?sid=phonemic（voice_gateway KWS/ASR 同一条链，
    与屏上 ?voice=1 的 AudioWorklet 采集完全同构；verdict=danger 服务端拒收二闸原样生效）。

展演位用法：手机入列上麦后在中枢跑本桥——站 U 中央喊「小界小界」免布线。
LAN 直连一律绕系统代理（ProxyHandler({})，lan_probe_lint 纪律）。

用法：
  python phone_mic_bridge.py                 # 常驻转发（Ctrl+C 停）
  python phone_mic_bridge.py --probe         # 4s 链路活性自证：连通→转发→回读 peek/state
  （--probe 无手机也能过：/mic/pcm 空闲静音帧照样走全链=管道活性；真人声验证需手机上麦）
"""
from __future__ import annotations

import argparse
import array
import json
import sys
import time
import urllib.request

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CHUNK_MS = 128            # 与 hud.html vSend 同粒度（voice_gateway 按块喂）
DST_SR = 16000


def _resample_i16(pcm: bytes, src_sr: int) -> bytes:
    """PCM16 mono src_sr → 16k。48k→16k=3:1 均值抽取（顺带抗混叠）；其他率走线性插值。"""
    if src_sr == DST_SR or not pcm:
        return pcm
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not len(a):
        return b""
    if src_sr == DST_SR * 3:
        n = len(a) // 3
        out = array.array("h", (0 for _ in range(n)))
        for i in range(n):
            j = i * 3
            out[i] = (a[j] + a[j + 1] + a[j + 2]) // 3
        return out.tobytes()
    ratio = src_sr / DST_SR
    n = max(1, int(len(a) / ratio))
    out = array.array("h", (0 for _ in range(n)))
    for i in range(n):
        x = i * ratio
        j = int(x)
        f = x - j
        b = a[j + 1] if j + 1 < len(a) else a[j]
        out[i] = int(a[j] * (1 - f) + b * f)
    return out.tobytes()


def _post(url: str, data: bytes, timeout: float = 6):
    req = urllib.request.Request(url, data=data, method="POST")
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def _get_json(url: str, timeout: float = 6):
    with OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def run(relay: str, hud: str, sid: str, seconds: float, probe: bool) -> int:
    stream_url = f"{relay}/mic/pcm"
    feed_url = f"{hud}/api/voice/stream?sid={sid}"
    try:
        resp = OPENER.open(stream_url, timeout=8)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL 连不上中继 {stream_url}：{e}")
        return 1
    src_sr = int(resp.headers.get("X-Sample-Rate") or 48000)
    print(f"OK 中继麦出口已连（{src_sr}Hz s16le mono）→ {feed_url}")
    chunk_bytes_src = int(src_sr * 2 * CHUNK_MS / 1000)
    buf = b""
    t0 = time.time()
    sent = 0
    peak = 0
    last_note = t0
    try:
        while True:
            if seconds and time.time() - t0 >= seconds:
                break
            data = resp.read(chunk_bytes_src - len(buf) if len(buf) < chunk_bytes_src else 4096)
            if not data:
                print("中继流结束（服务停了？）")
                break
            buf += data
            while len(buf) >= chunk_bytes_src:
                blk, buf = buf[:chunk_bytes_src], buf[chunk_bytes_src:]
                pcm16k = _resample_i16(blk, src_sr)
                a = array.array("h")
                a.frombytes(pcm16k)
                if a:
                    peak = max(peak, max(abs(x) for x in a))
                try:
                    _post(feed_url, pcm16k)
                    sent += 1
                except Exception as e:  # noqa: BLE001
                    print(f"喂送失败（{e}）——1s 后继续")
                    time.sleep(1)
            if not probe and time.time() - last_note > 5:
                last_note = time.time()
                print(f"  转发中：{sent} 块 · 峰值 {peak / 32768:.3f}")
                peak = 0
    except KeyboardInterrupt:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass
    print(f"共转发 {sent} 块（{sent * CHUNK_MS / 1000:.1f}s 音频）")
    if not probe:
        return 0
    # 活性自证：voice_gateway 侧必须真收到（peek 回放耳 + state 会话观测同源）
    try:
        st = _get_json(f"{hud}/api/voice/state")
        pk = _get_json(f"{hud}/api/voice/peek?sid={sid}&s=4")
        ok = sent > 0
        print(f"voice/state 就绪={st.get('ready', st.get('ok'))} · peek({sid})="
              f"{json.dumps(pk, ensure_ascii=False)[:160]}")
        print(("PASS 链路活性：中继→桥→voice_gateway 全通" if ok else "FAIL 没有转发出任何块"))
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        print(f"FAIL 回读失败：{e}")
        return 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="手机麦→小界语音指挥接线桥（零改中继）")
    ap.add_argument("--relay", default="http://127.0.0.1:7878")
    ap.add_argument("--hud", default="http://127.0.0.1:7913")
    ap.add_argument("--sid", default="phonemic")
    ap.add_argument("--seconds", type=float, default=0, help="转发时长（0=直到 Ctrl+C）")
    ap.add_argument("--probe", action="store_true", help="4s 链路活性自证后退出")
    a = ap.parse_args()
    if a.probe and not a.seconds:
        a.seconds = 4
    return run(a.relay.rstrip("/"), a.hud.rstrip("/"), a.sid, a.seconds, a.probe)


if __name__ == "__main__":
    sys.exit(main())
