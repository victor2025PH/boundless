# -*- coding: utf-8 -*-
"""P3-1/P4 实弹演练：变声「真在跑」时的热切——验证 config→stop→等旧流退→start 全链与双流防线。
非直播态才跑（不打扰真场次）；结束必停转换 + 还原 config.json。
判据：
  · hot_switch 返回 was_running=true 且 started=true
  · 热切后 RVC /start 再打一次应 400 already running（=只有一条流在跑，双流防线成立）
  · 热切耗时应 >0.8s（等待旧拾音流退出的强制间隔真的生效）
P4 追加：
  · 起停走 hub 代理（/rvc/start·stop）→ 转换旗标 _RVC_CONV 生效
  · 转换中 /rvc/devices 走 fresh 合并路径不炸（本地子进程枚举补洞）
  · 热切成功事件真进场次账本（timeline event=device，label 含「热切成功」+来源）
"""
import json
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
RVC = "http://127.0.0.1:6242"
CFG = Path(r"C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\configs\config.json")
FAILS = []


def chk(name, cond, detail=""):
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(detail)[:200]) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    try:
        st = requests.get(HUB + "/realtime/status", timeout=5).json()
        if st.get("running") or st.get("streaming"):
            print("  [ABORT] 正在推流，实弹演练不打扰真场次")
            return 2
    except Exception:
        pass
    cfg_orig = CFG.read_bytes()
    started_by_us = False
    try:
        # 1) 用当前推荐设备把配置立起来（同 hot_switch 的选择链），并启动真实转换
        pick = requests.get(HUB + "/rvc/auto_devices", timeout=15).json()
        cfg = json.loads(cfg_orig.decode("utf-8"))
        cfg["sg_input_device"] = pick["input"]
        cfg["sg_output_device"] = pick["output"]
        cfg.pop("use_jit", None)
        r = requests.post(RVC + "/config", json=cfg, timeout=20)
        chk("预备 /config 200", r.status_code == 200, r.text[:120])
        t0 = time.time()
        # P4：起转换走 hub 代理 → 转换旗标 _RVC_CONV 置位（fresh 合并的开关）
        r = requests.post(HUB + "/rvc/start", timeout=90)   # 首次含模型加载
        started_by_us = ("started" in r.text or r.status_code == 200)
        chk("预备 hub /rvc/start(真转换已跑)", started_by_us, r.text[:120])
        print(f"  [info] 首启耗时 {time.time()-t0:.1f}s")
        time.sleep(2.0)   # 让拾音流稳定跑一会

        # P4：转换中 /rvc/devices 走 fresh 合并路径（本地子进程枚举补洞）——必须不炸、src 仍为 rvc
        dv = requests.get(HUB + "/rvc/devices", timeout=25).json()
        chk("转换中 /rvc/devices ok(fresh 合并路径不炸)", dv.get("ok") is True and dv.get("source") == "rvc",
            f"src={dv.get('source')} in={len(dv.get('input_devices') or [])} fresh_note={dv.get('fresh_note')}")

        # 2) 实弹热切（带来源 → P4 账本留痕）
        t1 = time.time()
        d = requests.post(HUB + "/rvc/hot_switch", json={"src": "drill"}, timeout=60).json()
        dt = time.time() - t1
        print("  [info] hot_switch →", json.dumps({k: d.get(k) for k in
              ("ok", "step", "was_running", "started", "input_label", "output_label", "elapsed_s", "detail")}, ensure_ascii=False))
        chk("was_running=true(识别出在跑)", d.get("was_running") is True)
        chk("started=true(重启成功)", d.get("started") is True, d.get("detail"))
        chk("热切含防双流等待(>0.8s)", dt > 0.8, f"{dt:.2f}s")
        print(f"  [info] 热切全链耗时 {dt:.2f}s")

        # 3) 双流防线：此刻应只有一条流在跑 → 再 start 必 400 already running
        r = requests.post(RVC + "/start", timeout=20)
        chk("热切后再 start=400 already(单流不叠)", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

        # 4) P4 场次账本：热切成功事件真上账（timeline event=device）
        tl = requests.get(HUB + "/realtime/health_timeline?limit=20", timeout=5).json()
        hits = [e for e in (tl.get("timeline") or []) if e.get("event") == "device"
                and "热切成功" in (e.get("label") or "") and "来源=drill" in (e.get("label") or "")]
        chk("账本有「热切成功」事件(来源=drill)", bool(hits), (hits[-1].get("label") if hits else "无"))
    finally:
        try:
            r = requests.post(HUB + "/rvc/stop", timeout=15)   # 走 hub 代理 → 旗标归位
            print(f"  [cleanup] hub /rvc/stop → {r.status_code}")
        except Exception as e:
            print(f"  [cleanup] /stop 异常: {e}")
        CFG.write_bytes(cfg_orig)
        chk("config.json 已还原", CFG.read_bytes() == cfg_orig)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P3-1 实弹演练 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
