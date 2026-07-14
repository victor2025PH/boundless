"""
mf_vcam_companion.py — 把 realtime_stream 的换脸画面喂进 BestCam MF 虚拟摄像头
================================================================================
链路:
  realtime_stream (换脸) --/swapped MJPEG--> 本脚本(解码→垫黑边到1920x1080→NV12)
  --Global\\BestCam_SharedMem--> BestCamSource.dll (MF媒体源, Session 0)
  --MFCreateVirtualCamera--> "BestCam Virtual Webcam" (Image 类, Telegram 能选到)

要点:
  * 本脚本只是 OpenFileMapping 打开由 DLL 创建的共享内存, 不需要管理员权限
    (需要管理员的是 BestCamHost.exe)。
  * DLL 固定按 1920x1080 NV12 对外发布, 所以这里必须输出 1920x1080。
    竖屏(720x1280)按比例居中, 两侧留黑边, 不拉伸变形。
  * 写帧顺序: 先写 NV12 数据, 最后写 frameIndex, 避免 DLL 读到半张帧。

依赖: numpy, opencv-python (realtime_stream 已在用, 环境里就有)
用法:
  python mf_vcam_companion.py                       # 默认读 http://127.0.0.1:8080/swapped
  python mf_vcam_companion.py --url http://127.0.0.1:8080/swapped
  python mf_vcam_companion.py --port 8080 --path /swapped
"""

import argparse
import ctypes
import struct
import sys
import time
import urllib.request

import cv2
import numpy as np

# ── 共享内存协议 (必须与 FrameServer.h / BestCamHost 一致) ──────────────────
SHARED_MEM_NAME = "Global\\BestCam_SharedMem"

TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
FRAME_SIZE = TARGET_WIDTH * TARGET_HEIGHT * 3 // 2   # NV12
HEADER_SIZE = 24                                     # 4*uint32 + uint64
TOTAL_SIZE = HEADER_SIZE + FRAME_SIZE

FILE_MAP_WRITE = 0x0002
FILE_MAP_READ = 0x0004

# 静态头: width, height, stride, frameSize (与 DLL 发布的媒体类型一致)
_HEADER_STATIC = struct.pack("<4I", TARGET_WIDTH, TARGET_HEIGHT, TARGET_WIDTH, FRAME_SIZE)


def open_shared_memory(wait_secs: int = 60):
    """打开由 BestCamHost/BestCamSource.dll 创建的全局共享内存。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = ctypes.c_void_p
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]

    h_map = None
    for i in range(wait_secs):
        h_map = k32.OpenFileMappingW(FILE_MAP_WRITE | FILE_MAP_READ, False, SHARED_MEM_NAME)
        if h_map:
            break
        if i == 0:
            print("[companion] 等待 BestCamHost.exe 创建共享内存... (先以管理员运行 BestCamHost.exe)")
        time.sleep(1)

    if not h_map:
        print("[companion] 找不到共享内存, BestCamHost.exe 没在运行? 退出。")
        return None, None, None

    ptr = k32.MapViewOfFile(h_map, FILE_MAP_WRITE | FILE_MAP_READ, 0, 0, TOTAL_SIZE)
    if not ptr:
        print(f"[companion] MapViewOfFile 失败, err={ctypes.get_last_error()}")
        k32.CloseHandle(h_map)
        return None, None, None

    # 写一次静态头
    ctypes.memmove(ptr, _HEADER_STATIC, len(_HEADER_STATIC))
    print("[companion] 共享内存已连接。")
    return k32, h_map, ptr


def fit_canvas(frame, tw=TARGET_WIDTH, th=TARGET_HEIGHT):
    """按比例缩放并居中垫黑边到 tw x th, 不变形。"""
    h, w = frame.shape[:2]
    if w == 0 or h == 0:
        return np.zeros((th, tw, 3), dtype=np.uint8)
    scale = min(tw / w, th / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((th, tw, 3), dtype=np.uint8)
    x = (tw - nw) // 2
    y = (th - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas


def bgr_to_nv12(bgr, out):
    """BGR(1920x1080) -> NV12, 写入预分配的一维 out (uint8, FRAME_SIZE)。"""
    h, w = bgr.shape[:2]
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)   # (h*3/2, w)
    uv_off = w * h
    out[:uv_off] = yuv[:h].ravel()                    # Y 平面
    u = yuv[h:h + h // 4].ravel()
    v = yuv[h + h // 4:h + h // 2].ravel()
    out[uv_off::2] = u                                # UV 交织
    out[uv_off + 1::2] = v


def standby_frame(text="等待换脸画面 (realtime_stream / 手机推流)..."):
    """占位帧, 避免 Telegram 预览一片黑。"""
    img = np.full((TARGET_HEIGHT, TARGET_WIDTH, 3), 24, dtype=np.uint8)
    cv2.putText(img, "BestCam Virtual Webcam", (60, 480),
                cv2.FONT_HERSHEY_SIMPLEX, 1.6, (200, 200, 200), 3)
    cv2.putText(img, "waiting for face-swap stream...", (60, 560),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (120, 120, 120), 2)
    return img


def iter_mjpeg(url, timeout=10):
    """从 multipart/x-mixed-replace MJPEG 流里逐帧提取 JPEG 字节。
    直接扫 SOI(FFD8)/EOI(FFD9), 不依赖 boundary, 兼容各种服务端。"""
    req = urllib.request.Request(url, headers={"User-Agent": "bestcam-companion"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    buf = b""
    try:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                soi = buf.find(b"\xff\xd8")
                if soi < 0:
                    if len(buf) > 4:
                        buf = buf[-2:]
                    break
                eoi = buf.find(b"\xff\xd9", soi + 2)
                if eoi < 0:
                    if soi > 0:
                        buf = buf[soi:]
                    break
                yield buf[soi:eoi + 2]
                buf = buf[eoi + 2:]
    finally:
        try:
            resp.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="喂 realtime_stream 换脸画面到 BestCam MF 虚拟摄像头")
    ap.add_argument("--url", default=None, help="MJPEG 源地址 (覆盖 --host/--port/--path)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--path", default="/swapped")
    ap.add_argument("--fps", type=float, default=30.0, help="最高推送帧率")
    args = ap.parse_args()

    url = args.url or f"http://{args.host}:{args.port}{args.path}"
    print(f"[companion] MJPEG 源: {url}")

    k32, h_map, ptr = open_shared_memory()
    if not ptr:
        sys.exit(1)

    nv12 = np.empty(FRAME_SIZE, dtype=np.uint8)
    frame_index = 0
    min_interval = 1.0 / args.fps if args.fps > 0 else 0.0

    def push(bgr_canvas):
        nonlocal frame_index
        bgr_to_nv12(bgr_canvas, nv12)
        ctypes.memmove(ptr + HEADER_SIZE, nv12.ctypes.data, FRAME_SIZE)   # 先写数据
        frame_index += 1
        ctypes.memmove(ptr + 16, struct.pack("<Q", frame_index), 8)       # 再写 frameIndex
        ctypes.memmove(ptr, _HEADER_STATIC, len(_HEADER_STATIC))

    # 起步先推占位帧
    push(standby_frame())

    last_push = 0.0
    n_ok = 0
    t_report = time.time()

    try:
        while True:
            try:
                for jpg in iter_mjpeg(url):
                    now = time.time()
                    if now - last_push < min_interval:
                        continue
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    push(fit_canvas(frame))
                    last_push = now
                    n_ok += 1
                    if now - t_report >= 5.0:
                        print(f"[companion] 已推送 {n_ok} 帧, frameIndex={frame_index}")
                        t_report = now
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[companion] 源断开/未就绪 ({e}); 2s 后重连, 期间保持占位帧...")
                push(standby_frame("realtime_stream 未连接, 重连中..."))
                time.sleep(2)
    except KeyboardInterrupt:
        print("\n[companion] 停止。")
    finally:
        try:
            k32.UnmapViewOfFile(ctypes.c_void_p(ptr))
            k32.CloseHandle(h_map)
        except Exception:
            pass


if __name__ == "__main__":
    main()
