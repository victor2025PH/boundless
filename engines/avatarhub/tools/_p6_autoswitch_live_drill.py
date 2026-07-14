# -*- coding: utf-8 -*-
"""P6-1 实弹演练：变声「真在跑」时模拟设备被拔 → 服务端守护自动热切（无人值守，全程不碰浏览器）。

模拟手法：往 RVC 落盘的 configs/config.json 写一个不存在的输入设备名（幽灵麦）。
守护把它当「运行设备」，对照本地即时枚举判缺席；持续超过确认窗(25s)后自动走
与手动 CTA 同一条 rvc_hot_switch 链切回真实推荐设备并重启拾音流。
判据：
  · timeline 出现 event=device 且 label 含「热切成功」「来源=auto」
  · 漏斗真进账：src_total 含 ok:auto
  · config.json 的输入设备被改回真实设备（≠幽灵名）
  · 热切后 RVC /start 再打一次应 400 already（单流不叠，双流防线仍成立）
非直播态才跑；结束必停转换 + 还原 config.json / heal_config / devflow。
"""
import json
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
RVC = "http://127.0.0.1:6242"
CFG = Path(r"C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\configs\config.json")
FLOW = Path(r"C:\模仿音色\data\devflow_stats.json")
HEAL = Path(r"C:\模仿音色\data\heal_config.json")
GHOST = "P6幽灵麦克风 (MME)"
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
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    heal_orig = HEAL.read_bytes() if HEAL.exists() else None
    autosw_orig = bool(requests.get(HUB + "/api/heal/config", timeout=5).json().get("dev_autoswitch"))
    try:
        # 0) 打开自动热切开关（运行时生效，结束还原）
        r = requests.post(HUB + "/api/heal/config", json={"dev_autoswitch": True}, timeout=5).json()
        chk("预备 自动热切开关已开", r.get("dev_autoswitch") is True)

        # 1) 真实设备立配置 + 起真转换（走 hub 代理 → _RVC_CONV 旗标置位，守护才会盯枚举）
        pick = requests.get(HUB + "/rvc/auto_devices", timeout=15).json()
        cfg = json.loads(cfg_orig.decode("utf-8"))
        cfg["sg_input_device"] = pick["input"]
        cfg["sg_output_device"] = pick["output"]
        cfg.pop("use_jit", None)
        r = requests.post(RVC + "/config", json=cfg, timeout=20)
        chk("预备 /config 200", r.status_code == 200, r.text[:120])
        t0 = time.time()
        r = requests.post(HUB + "/rvc/start", timeout=90)
        chk("预备 hub /rvc/start(真转换已跑)", r.status_code == 200, r.text[:120])
        print(f"  [info] 首启耗时 {time.time()-t0:.1f}s")
        time.sleep(2.0)

        # 2) 模拟拔麦：把落盘配置的输入设备改成幽灵名（转换仍在真设备上跑，仅骗过守护的缺席判定）
        ghost_cfg = json.loads(CFG.read_text(encoding="utf-8"))
        ghost_cfg["sg_input_device"] = GHOST
        CFG.write_text(json.dumps(ghost_cfg, ensure_ascii=False, indent=4), encoding="utf-8")
        print(f"  [info] 已注入幽灵设备名 → 等守护确认缺席(25s)+开火(tick 10s)…")

        # 3) 等自动热切发生（最多 ~100s）：观测 timeline 出现 来源=auto 的 device 事件
        t_inject = time.time()
        auto_ev = None
        while time.time() - t_inject < 100:
            time.sleep(5)
            tl = requests.get(HUB + "/realtime/health_timeline?limit=30", timeout=5).json()
            evs = [e for e in (tl.get("timeline") or [])
                   if e.get("event") == "device" and "来源=auto" in (e.get("label") or "")
                   and float(e.get("ts") or 0) >= t_inject - 2]
            if evs:
                auto_ev = evs[-1]
                break
        chk("守护自动热切已触发(timeline 来源=auto)", auto_ev is not None,
            (auto_ev or {}).get("label") or "超时未触发")
        if auto_ev:
            chk("账本记「热切成功」", "热切成功" in (auto_ev.get("label") or ""), auto_ev.get("label"))
            dt_fire = float(auto_ev.get("ts") or 0) - t_inject
            chk("触发时机在确认窗之后(≥25s)", dt_fire >= 24, f"{dt_fire:.0f}s")
            print(f"  [info] 注入→开火 {dt_fire:.0f}s（确认窗25s+tick≤10s+枚举缓存≤20s）")

        # 4) 配置被改回真实设备（≠幽灵名），转换重启成功（再 start 必 400=单流）
        cur = json.loads(CFG.read_text(encoding="utf-8"))
        chk("config 输入已切回真实设备", cur.get("sg_input_device") != GHOST, cur.get("sg_input_device"))
        try:
            r = requests.post(RVC + "/start", timeout=20)
            chk("热切后再 start=400 already(单流不叠)", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
        except Exception as e:   # RVC 进程若猝死，这里如实记 NG 而不是让演练脚本带着未还原状态崩掉
            chk("热切后再 start=400 already(单流不叠)", False, f"RVC 不可达(进程可能猝死): {e}")

        # 4b) P7-1 即时感知顺风车：/realtime/status 应带上这次自动热切（前端 4s 轮询到即弹 toast）
        stt = requests.get(HUB + "/realtime/status", timeout=5).json()
        asw = stt.get("dev_autoswitch_last") or {}
        chk("status 顺风车带自动热切(P7-1)", bool(asw.get("ts")) and asw.get("ok") is True
            and float(asw.get("ts") or 0) >= t_inject, {k: asw.get(k) for k in ("ok", "to", "n")})
        chk("顺风车含目标设备人话名", bool(asw.get("to")), asw.get("to"))
        # 4c) P8-1 自救卡退场判据顺风车：热切成功后转换在跑 → rvc_conv=True
        chk("status 顺风车 rvc_conv=True(P8-1)", stt.get("rvc_conv") is True, stt.get("rvc_conv"))

        # 5) 漏斗同一本账：auto 来源的 expose/click/ok 都进账
        d = requests.get(HUB + "/api/metrics/devflow", timeout=5).json()
        stot = d.get("src_total") or {}
        chk("漏斗进账 ok:auto", stot.get("ok:auto", 0) >= 1,
            {k: v for k, v in stot.items() if k.endswith(":auto")})
    finally:
        try:
            r = requests.post(HUB + "/rvc/stop", timeout=15)
            print(f"  [cleanup] hub /rvc/stop → {r.status_code}")
        except Exception as e:
            print(f"  [cleanup] /stop 异常: {e}")
        CFG.write_bytes(cfg_orig)
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        if heal_orig is None:
            HEAL.unlink(missing_ok=True)
        else:
            HEAL.write_bytes(heal_orig)
        try:
            requests.post(HUB + "/api/heal/config", json={"dev_autoswitch": autosw_orig}, timeout=5)
            print(f"  [cleanup] 自动热切开关还原为 {autosw_orig}")
        except Exception:
            pass
        chk("config.json 已还原", CFG.read_bytes() == cfg_orig)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P6-1 实弹演练 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
