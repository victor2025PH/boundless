# -*- coding: utf-8 -*-
"""活体 HUD 状态矩阵实拍（P2 · 2026-08-07，非门禁）。

用 demo 注入层（?demo=1&scene=…合成数据）拍**确定性**状态图——真数据像素天然抖动
进不了流程，合成数据保证同一场景次次同图可人眼对比。对齐 stream_state_shots 家规：
「太久没拍」要在 doctor 可见（读 manifest 新鲜度），改版后必跑必过目。

场景×5：ok（全绿）/ warn（高水位+告警+ack样例）/ down（节点失联）/
        stream（推流冻结+静默章）/ stale（数据陈旧降饱和）。

产物：C:/模仿音色/logs/hud_shots/<scene>.png + logs/hud_state_shots.json（doctor 读）。
需要：活的 hud_server :7913 + 系统 Edge（headless）。用法：python hud_state_shots.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

AVATARHUB = Path(r"C:\模仿音色")
OUT_DIR = AVATARHUB / "logs" / "hud_shots"
MANIFEST = AVATARHUB / "logs" / "hud_state_shots.json"
BASE = "http://127.0.0.1:7913/hud?machine=zhongshu&demo=1&gesture=0&scene="
# (场景名, scene 参数, 额外 query)：cons=舰桥星座视图（P3 三期，含 demo 卫星两枚其一低电）
# tour_*=展演编排两态（P0 展演骨架 2026-08-08；budget 5s 时横幅正处全亮段，谢幕是常驻态）
SCENES = [("ok", "ok", ""), ("warn", "warn", ""), ("down", "down", ""),
          ("stream", "stream", ""), ("stale", "stale", ""), ("cons", "ok", "&view=cons"),
          ("tour_welcome", "ok", "&tour=welcome&visitor=贵宾示例"),
          ("tour_curtain", "ok", "&tour=curtain"),
          # P1 语音三态（voicestate demo 直达；result 字幕带常显 12s，budget 5s 内必在屏）
          ("voice_listen", "ok", "&voicestate=listen"),
          ("voice_result", "ok", "&voicestate=result"),
          ("voice_deny", "ok", "&voicestate=deny"),
          # P2 隔空指挥两态（opsstate demo 直达；文案无内网 IP=money shot 素材纪律）
          ("ops_armed", "ok", "&opsstate=armed"),
          ("ops_fired", "ok", "&opsstate=fired"),
          # P1 功能加厚（2026-08-12）：三维全息+总览条（down=有离线红曜）/ 节点操作盘展开
          # （脸备离线语境：拉屏+详情+唤醒+切路由四按钮全亮；headless WebGL 走 SwiftShader）
          ("holo", "down", "&view=holo"),
          ("holo_panel", "down", "&view=holo&holopanel=lianbei"),
          # P3 逐层下钻（2026-08-12）：warn 场景中枢 rvc 死=红子星呼吸闪+下钻章
          ("holo_drill", "warn", "&view=holo&holodrill=zhongshu"),
          # P4 展演飞览（2026-08-12）：欢迎幕时间轴回拨 13s=辐条拍中心（准星+变焦+横幅同框；
          # 9s 曾落拍边界=headless 虚拟时间漂 1-3s 会翻到邻机，像素对比不稳）
          ("tour_flyby", "ok", "&view=holo&tour=welcome&visitor=贵宾示例&tourat=13"),
          # N4 模式条三态（2026-08-16 首屏直给改版）：空闲带模式徽章/切换中带 n/N 进度/
          # 姿态漂移带「一键归位」章（cmstate demo 注入层直达，确定性像素）
          ("cm_idle", "ok", "&cmstate=idle"),
          ("cm_switching", "ok", "&cmstate=switching"),
          ("cm_repair", "warn", "&cmstate=repair")]
SIZE = "1920,1080"

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def find_edge() -> Path | None:
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    return None


def shoot(edge: Path, scene: str, out: Path, extra: str = "", scale: int = 1) -> tuple[bool, str]:
    url = BASE + scene + extra
    cmd = [str(edge), "--headless=new", "--disable-gpu", "--hide-scrollbars",
           f"--window-size={SIZE}", "--virtual-time-budget=5000",
           f"--screenshot={out}", url]
    if scale > 1:
        cmd.insert(4, f"--force-device-scale-factor={scale}")   # 营销档：2x=3840×2160 印刷级
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        ok = out.is_file() and out.stat().st_size > 30_000
        return ok, ("" if ok else f"exit={r.returncode} size={out.stat().st_size if out.is_file() else 0}")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# 营销高清包（--marketing）：全息 money shot 四景 2x 出图 → logs/hud_marketing/<日期>/
# （HUD 是单暗主题,无白天档；tour_flyby=飞览节拍钉在辐条聚焦位=准星+变焦入镜）
MARKETING = [("holo", "down", "&view=holo"),
             ("holo_panel", "down", "&view=holo&holopanel=lianbei"),
             ("holo_drill", "warn", "&view=holo&holodrill=zhongshu"),
             ("tour_flyby", "ok", "&view=holo&tour=welcome&visitor=贵宾示例&tourat=13")]


def marketing(edge: Path) -> int:
    out_dir = AVATARHUB / "logs" / "hud_marketing" / time.strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    fails = 0
    for name, sc, extra in MARKETING:
        out = out_dir / f"{name}@2x.png"
        ok, why = shoot(edge, sc, out, extra, scale=2)
        print(f"{'OK  ' if ok else 'FAIL'} {name}@2x -> {out}" + ("" if ok else f" ({why})"))
        fails += 0 if ok else 1
    print(f"== hud_state_shots --marketing: {len(MARKETING) - fails}/{len(MARKETING)} -> {out_dir} ==")
    return 0 if fails == 0 else 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    edge = find_edge()
    if not edge:
        print("Edge 不在标准路径（跳过实拍）")
        return 1
    if "--marketing" in sys.argv:
        return marketing(edge)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shots, fails = [], 0
    for name, sc, extra in SCENES:
        out = OUT_DIR / f"{name}.png"
        ok, why = shoot(edge, sc, out, extra)
        if ok:
            shots.append({"scene": name, "file": str(out), "bytes": out.stat().st_size})
            print(f"OK   {name} -> {out.name} ({out.stat().st_size // 1024}KB)")
        else:
            fails += 1
            print(f"FAIL {name}: {why}")
    MANIFEST.write_text(json.dumps({
        "ts": round(time.time()), "modes": ["demo"], "scenes": [s[0] for s in SCENES],
        "shots": shots, "fails": fails,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"== hud_state_shots: {len(shots)}/{len(SCENES)}，manifest -> {MANIFEST} ==")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
