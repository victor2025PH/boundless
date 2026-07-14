# -*- coding: utf-8 -*-
"""triple_channel_soak.py — 三通道叠加无人值守实测（2026-07-06 Phase 12 收官附加）

背景：画质线 P2-P6 把「脸区裁剪通道 / 超清1080P / 口播极致(CodeFormer) / 虚拟背景」各自标定过，
但**叠加工况**只有人工实机观察一条路（路线图 2026-07-06j 下一阶段第 1 项）。本脚本把其中
可自动化的 80% 先跑掉——真人场次只剩「看观感」（贴缝/美颜化/字幕遮挡），不用再盯指标。

三阶段（渐进叠加，每阶段 ~90s，全程生产同款路由 synth_cam→realtime→hub→.104）：
  P1  裁剪通道 + 1080P 画布 + hd(GFPGAN)      —— 复核 P3 批次的 323ms 口径
  P2  P1 + 虚拟背景(blur)                      —— 叠加后延迟增量应 ≈ bg ~5ms(CPU) 而非放大
  P3  裁剪 + 1080P + 口播极致(CodeFormer·5fps) + 背景 —— 三通道全开的极限工况

同步观察 D-1→E 体检卡点：空闲时与直播中各测一次 quick 体检，
红灯而链路实际健康 = 误拦(false_block)，写进结论供人工复核。

用法:  python tools/triple_channel_soak.py [--phase-s 90] [--skip-p3] [--force]
产物:  logs/triple_channel_soak_<日期>.json + 控制台时间线
注:    - soak 用隔离端口(rt=8081/cam=8088)，不碰生产 8080/8087;
       - 生产直播(8080)在跑时默认拒绝启动(双路都打 .104,互相拖慢数据双废),--force 可强跑;
       - 与 vcam_server 并存无冲突(rt 抢不到 OBS 自动降级为统计/MJPEG-only)。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests

PY = sys.executable
HUB = "http://127.0.0.1:9000"
# 隔离端口：8080/8087 是生产直播占用位。2026-07-06 首跑教训——soak 与在播实例同绑 8080,
# 采样/控制全打到对方实例(bg 开进直播、体检读错对象)，P1/P2 数据作废。
RT = "http://127.0.0.1:8081"
CAM = "http://127.0.0.1:8088"
PROD_RT = "http://127.0.0.1:8080"


def wait_http(url: str, timeout_s: float, desc: str) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                print(f"  {desc} 就绪 ({time.time() - t0:.0f}s)", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    print(f"  !! {desc} {timeout_s}s 未就绪", flush=True)
    return False


def checkup_quick(tag: str) -> dict:
    """D-1 quick 体检快照(不录音,<1s)。探测失败不阻断 soak(与开播卡点同一原则)。"""
    try:
        j = requests.get(f"{HUB}/api/device/checkup", params={"quick": 1}, timeout=8).json()
        bad = [i.get("key") for i in (j.get("items") or [])
               if i.get("measured") and i.get("level") == "bad"]
        rec = {"tag": tag, "ok": j.get("ok"), "score": j.get("score"),
               "grade": j.get("grade"), "bad_items": bad}
    except Exception as e:
        rec = {"tag": tag, "ok": False, "err": str(e)[:80]}
    print(f"  [体检@{tag}] {rec}", flush=True)
    return rec


def rt_start(procs: list, extra_args: list, extra_env: dict, log_name: str) -> bool:
    import os
    env = dict(os.environ)
    env.update({"SWAP_AUTO_QUALITY": "1", "SWAP_AUTO_DOWN_MS": "650",
                "SWAP_AUTO_UP_MS": "475", "SWAP_STATS": "0",   # 不污染趋势库
                "PYTHONIOENCODING": "utf-8"})
    env.update(extra_env)
    p = subprocess.Popen(
        [PY, str(BASE / "realtime_stream.py"),
         "--source", f"{CAM}/stream", "--width", "1920", "--height", "1080",
         "--swap-preset", "hd", "--no-preview",
         "--mjpeg-port", RT.rsplit(":", 1)[1]] + extra_args,
        cwd=str(BASE), env=env,
        stdout=open(BASE / "logs" / log_name, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT)
    procs.append(p)
    if not wait_http(f"{RT}/swap/status", 60, "realtime_stream(1080p)"):
        return False
    # 等换脸真正出帧(ok 计数动起来)，最多 60s——冷启含检测器/引擎首帧
    t0 = time.time()
    base_ok = -1
    while time.time() - t0 < 60:
        try:
            st = requests.get(f"{RT}/swap/status", timeout=3).json()
            ok_n = (st.get("stats") or {}).get("ok", 0)
            if base_ok < 0:
                base_ok = ok_n
            elif ok_n > base_ok:
                print(f"  换脸帧开始流动 (ok={ok_n}, {time.time() - t0:.0f}s)", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    print("  !! 60s 内换脸无成功帧", flush=True)
    return False


def rt_stop(procs: list):
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(2)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    procs.clear()


def observe_phase(name: str, seconds: float) -> dict:
    """2s 采样 /swap/status：延迟/档位/裁剪命中/背景耗时；中点抓一次 /swap/quality。"""
    t_end = time.time() + seconds
    samples, quality = [], None
    c0 = None
    last_eff = None
    while time.time() < t_end:
        try:
            st = requests.get(f"{RT}/swap/status", timeout=3).json()
            crop = st.get("crop") or {}
            stats = st.get("stats") or {}
            auto = st.get("auto") or {}
            bg = st.get("bg") or {}
            if c0 is None:
                c0 = {"hits": crop.get("hits", 0), "miss": crop.get("miss", 0),
                      "ok": stats.get("ok", 0), "fail": stats.get("fail", 0),
                      "t": time.time()}
            rec = {"t": round(time.time(), 1), "lat": stats.get("latency_ms"),
                   "eff": st.get("preset"), "reason": (auto.get("reason") or "")[:60],
                   "crop_active": crop.get("active"), "bg_ms": bg.get("ms"),
                   "fps": stats.get("fps")}
            samples.append(rec)
            if rec["eff"] != last_eff:
                last_eff = rec["eff"]
                print(f"  [{name}] {time.strftime('%H:%M:%S')} 档={last_eff} "
                      f"lat={rec['lat']}ms crop={rec['crop_active']} bg={rec['bg_ms']}ms",
                      flush=True)
            if quality is None and time.time() > t_end - seconds / 2:
                try:
                    quality = requests.get(f"{RT}/swap/quality", timeout=5).json()
                except Exception:
                    pass
        except Exception as e:
            samples.append({"t": round(time.time(), 1), "err": str(e)[:60]})
        time.sleep(2)

    # 汇总
    try:
        st = requests.get(f"{RT}/swap/status", timeout=3).json()
        crop = st.get("crop") or {}
        stats = st.get("stats") or {}
        dt = max(1e-6, time.time() - c0["t"])
        d_hits = crop.get("hits", 0) - c0["hits"]
        d_miss = crop.get("miss", 0) - c0["miss"]
        d_ok = stats.get("ok", 0) - c0["ok"]
        d_fail = stats.get("fail", 0) - c0["fail"]
    except Exception:
        d_hits = d_miss = d_ok = d_fail = 0
        dt = 1.0
    lats = [s["lat"] for s in samples if isinstance(s.get("lat"), (int, float)) and s["lat"] > 0]
    lats.sort()
    effs = [s.get("eff") for s in samples if s.get("eff")]
    summary = {
        "samples": len(samples),
        "lat_mean": round(sum(lats) / len(lats), 1) if lats else None,
        "lat_p95": round(lats[int(0.95 * (len(lats) - 1))], 1) if lats else None,
        "swap_ok_rate": round(d_ok / dt, 2), "swap_fail": d_fail,
        "crop_hit_ratio": round(d_hits / (d_hits + d_miss), 3) if (d_hits + d_miss) else None,
        "end_preset": effs[-1] if effs else None,
        "downshifted": sorted({e for e in effs if e not in ("hd",)}),
        "quality": {k: (quality or {}).get(k) for k in
                    ("ok", "retention", "mouth_retention", "sharp_swapped", "sharp_raw",
                     "brightness", "face_w", "crop_active", "advice")}
                   if quality else None,
    }
    print(f"  [{name}] 汇总: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return {"name": name, "summary": summary, "timeline": samples}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase-s", type=int, default=90, help="每阶段观察时长(秒)")
    ap.add_argument("--skip-p3", action="store_true", help="跳过口播极致阶段(省时)")
    ap.add_argument("--force", action="store_true", help="生产直播在跑也强行 soak(数据会互相污染,慎用)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 在播预检：生产 8080 活着(且真在换脸)时双路会同抢 .104，两边数据双废
    if not args.force:
        try:
            st = requests.get(f"{PROD_RT}/swap/status", timeout=2).json()
            print(f"!! 生产 realtime_stream(8080) 正在运行(档={st.get('preset')})。"
                  f"soak 与直播同抢换脸引擎会互相拖慢——请停播后再跑，或 --force 强行。", flush=True)
            sys.exit(3)
        except requests.RequestException:
            pass   # 8080 无人监听 = 未开播,正常继续

    (BASE / "logs").mkdir(exist_ok=True)
    cam_proc, rt_procs = [], []
    phases, checkups = [], []
    verdict = {}
    try:
        checkups.append(checkup_quick("pre-idle"))

        print(f"[1/6] 拉起合成摄像头 1080p({CAM.rsplit(':', 1)[1]})…", flush=True)
        cam_proc.append(subprocess.Popen(
            [PY, str(BASE / "tools" / "synth_cam.py"),
             "--width", "1920", "--height", "1080", "--fps", "25",
             "--port", CAM.rsplit(":", 1)[1]],
            cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not wait_http(f"{CAM}/health", 20, "synth_cam"):
            raise SystemExit(1)

        print("[2/6] 拉起 realtime_stream 1080p·hd·裁剪默认开(生产路由 hub→.104)…", flush=True)
        if not rt_start(rt_procs, [], {}, "rt_soak_p1.log"):
            raise SystemExit(1)

        print(f"[3/6] P1 裁剪+1080P+GFPGAN {args.phase_s}s…", flush=True)
        phases.append(observe_phase("P1-crop+1080p+hd", args.phase_s))

        print("[4/6] 开虚拟背景(blur) → P2 叠加观察…", flush=True)
        try:
            r = requests.get(f"{RT}/bg/set", params={"mode": "blur"}, timeout=5).json()
            print(f"  bg/set → {r}", flush=True)
        except Exception as e:
            print(f"  !! bg/set 失败: {e}", flush=True)
        checkups.append(checkup_quick("during-stream"))
        phases.append(observe_phase("P2-+bg_blur", args.phase_s))

        if not args.skip_p3:
            print("[5/6] 重启 rt 为口播极致(CodeFormer·5fps·out_q88) + 背景 → P3…", flush=True)
            rt_stop(rt_procs)
            if rt_start(rt_procs, ["--face-enhance", "codeformer", "--swap-fps", "5"],
                        {"OUT_JPEG_QUALITY": "88"}, "rt_soak_p3.log"):
                try:
                    requests.get(f"{RT}/bg/set", params={"mode": "blur"}, timeout=5)
                except Exception:
                    pass
                phases.append(observe_phase("P3-vocal+bg(三通道)", args.phase_s))
            else:
                phases.append({"name": "P3-vocal+bg(三通道)", "summary": {"error": "启动失败"}})

        # ── 裁决 ──
        def _s(i):
            return (phases[i]["summary"] if i < len(phases) else {}) or {}
        p1, p2 = _s(0), _s(1)
        v = {
            "p1_latency_ok": bool(p1.get("lat_mean")) and p1["lat_mean"] < 650,
            "p1_crop_effective": (p1.get("crop_hit_ratio") or 0) >= 0.9,
            "p1_held_hd": p1.get("end_preset") == "hd" and not p1.get("downshifted"),
            "p2_bg_overhead_ok": bool(p1.get("lat_mean") and p2.get("lat_mean"))
                                 and (p2["lat_mean"] - p1["lat_mean"]) < 150,
            "p2_held_hd": p2.get("end_preset") == "hd",
        }
        if not args.skip_p3 and len(phases) >= 3:
            p3 = _s(2)
            v["p3_rate_ok"] = (p3.get("swap_ok_rate") or 0) >= 3.5   # 5fps 目标,3 并发在飞
            v["p3_latency_ok"] = bool(p3.get("lat_mean")) and p3["lat_mean"] < 650
        # 体检卡点误拦：链路健康(帧在流动)时直播中体检不应红灯
        during = next((c for c in checkups if c["tag"] == "during-stream"), {})
        v["checkup_false_block"] = (during.get("grade") == "red"
                                    and (p1.get("swap_ok_rate") or 0) > 3)
        v["pass"] = all(bool(v[k]) for k in v if k != "checkup_false_block") \
                    and not v["checkup_false_block"]
        verdict = v
    finally:
        rt_stop(rt_procs)
        rt_stop(cam_proc)

    out = {"ts": datetime.now().isoformat(timespec="seconds"),
           "phase_s": args.phase_s, "checkups": checkups,
           "verdict": verdict,
           "phases": [{"name": p["name"], "summary": p["summary"]} for p in phases],
           "timelines": {p["name"]: p.get("timeline", []) for p in phases},
           "human_checklist": [
               "贴缝：裁剪框边缘在纯色背景/大幅转头时是否可见",
               "CodeFormer 观感：是否过度美颜化/身份漂移(口播近景)",
               "虚拟背景：发丝边缘闪烁/背景残影",
               "字幕叠加(若开)与背景替换是否互相遮挡",
           ]}
    day = datetime.now().strftime("%Y%m%d")
    p = BASE / "logs" / f"triple_channel_soak_{day}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论: {json.dumps(verdict, ensure_ascii=False)}\n已写 {p}", flush=True)
    sys.exit(0 if verdict.get("pass") else 1)


if __name__ == "__main__":
    main()
