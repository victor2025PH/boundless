# -*- coding: utf-8 -*-
"""下载 FLUX.1-schnell fp8 到 ComfyUI checkpoints（在 gpu176 上跑，stdlib）。

Apache 2.0 许可 → 商用合规路由的无脸/快速出图模型。
镜像/HF 对**单连接**限速（实测 22MB/s 起步后掉到 0.4MB/s）→ 多段 Range 并行
（N 线程各下一段，各段独立断点续传）。预分配整文件 + 段内 offset 写入 +
.meta 记录各段进度；完成后校验总长原子改名。中断重跑只补缺口。
"""
import json
import os
import sys
import threading
import time
import urllib.request

PATH = "Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors"
MIRRORS = ("https://hf-mirror.com/" + PATH,
           "https://huggingface.co/" + PATH)
DST = r"D:\ComfyUI\models\checkpoints\flux1-schnell-fp8.safetensors"
EXPECT = 17236328572
N_SEG = 4  # 并发降到 4：8 并发+频繁重连疑似触发镜像风控（全连接被 hold 零进展）
# 64KB 小读块：镜像限速是"滴字节"（每次 recv 都有几 KB，socket 不超时）——
# 大块 read() 内部凑满前不返回，滑窗检测永远执行不到 → 之前三轮全程僵死的根因。
CHUNK = 64 << 10
GLOBAL_STALL_SEC = 75  # 全局看门狗：总增量近零持续这么久 → exit 3 交外层重启进程


def _load_meta(meta_path):
    """段表以 meta 文件为准（段数可与当前 N_SEG 不同——改并发数不丢已下进度）。"""
    try:
        with open(meta_path, encoding="utf-8") as f:
            m = json.load(f)
        if (m.get("size") == EXPECT and m.get("done")
                and len(m["done"]) == len(m.get("starts", []))
                == len(m.get("ends", []))):
            return m
    except Exception:
        pass
    seg = EXPECT // N_SEG
    return {"size": EXPECT,
            "starts": [i * seg for i in range(N_SEG)],
            "ends": [(i + 1) * seg if i < N_SEG - 1 else EXPECT
                     for i in range(N_SEG)],
            "done": [0] * N_SEG}


def _save_meta(meta_path, meta, lock):
    with lock:
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, meta_path)


PER_CONN_BYTES = 8 << 20   # 每连接只请求 8MB：全程只吃"起步高速额度"，掐速前已断开
MAX_CONN_SEC = 25          # 单连接硬时限（8MB 正常 <1s；滴流下到点强弃）


def _seg_worker(idx, part, meta, meta_path, lock, stop_evt):
    """段下载线程：断点续传本段。反限速＝超短连接轮转（每连接 8MB 即断）。"""
    round_ = idx  # 各线程从不同镜像起步，分散压力
    while not stop_evt.is_set():
        with lock:
            pos = meta["starts"][idx] + meta["done"][idx]
            end = meta["ends"][idx]
        if pos >= end:
            return
        stop_at = min(pos + PER_CONN_BYTES, end)
        url = MIRRORS[round_ % len(MIRRORS)]
        round_ += 1
        req = urllib.request.Request(
            url, headers={"Range": "bytes=%d-%d" % (pos, stop_at - 1)})
        try:
            with urllib.request.urlopen(req, timeout=15) as r, \
                    open(part, "r+b") as f:
                f.seek(pos)
                t0 = time.time()
                while not stop_evt.is_set():
                    if time.time() - t0 > MAX_CONN_SEC:
                        break  # 滴流僵死保险：强弃本连接（进度已计入 meta）
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    with lock:
                        meta["done"][idx] += len(chunk)
                        if meta["starts"][idx] + meta["done"][idx] >= end:
                            return
        except Exception:
            time.sleep(1.0)


def main() -> int:
    if os.path.exists(DST) and os.path.getsize(DST) == EXPECT:
        print("already complete", flush=True)
        return 0
    part, meta_path = DST + ".part", DST + ".meta"
    lock = threading.Lock()
    meta = _load_meta(meta_path)
    n_seg = len(meta["done"])
    # 预分配（首次）：sparse 文件占位，各段 offset 写入互不干扰
    if not os.path.exists(part) or os.path.getsize(part) != EXPECT:
        with open(part, "wb") as f:
            f.truncate(EXPECT)
        meta["done"] = [0] * n_seg
    stop_evt = threading.Event()
    ths = [threading.Thread(target=_seg_worker, daemon=True,
                            args=(i, part, meta, meta_path, lock, stop_evt))
           for i in range(n_seg)]
    t0 = time.time()
    for t in ths:
        t.start()
    last = 0
    stalled_since = None
    try:
        while any(t.is_alive() for t in ths):
            time.sleep(15)
            with lock:
                done = sum(meta["done"])
            _save_meta(meta_path, meta, lock)
            sp = (done - last) / 15.0 / (1 << 20)
            last = done
            print("%5.1f%%  %.2fGB  %.1fMB/s" % (
                done * 100.0 / EXPECT, done / (1 << 30), sp), flush=True)
            # 全局看门狗：所有段合计近零推进持续 GLOBAL_STALL_SEC → 自杀退 3，
            # 外层 wrapper 循环重启本进程（全新 socket 池，最硬的自愈）。
            if sp < 0.05:
                stalled_since = stalled_since or time.time()
                if time.time() - stalled_since > GLOBAL_STALL_SEC:
                    print("GLOBAL STALL, exiting for restart", flush=True)
                    stop_evt.set()
                    _save_meta(meta_path, meta, lock)
                    return 3
            else:
                stalled_since = None
    finally:
        stop_evt.set()
        _save_meta(meta_path, meta, lock)
    with lock:
        total = sum(meta["done"])
    if total < EXPECT:
        print("INCOMPLETE %d/%d (rerun to resume)" % (total, EXPECT),
              file=sys.stderr)
        return 2
    os.replace(part, DST)
    try:
        os.remove(meta_path)
    except OSError:
        pass
    print("OK %s (%.0fs)" % (DST, time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
