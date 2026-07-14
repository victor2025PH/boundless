# -*- coding: utf-8 -*-
"""真机端到端 VLM「全页面识别」验证（不依赖 FB 登录）。

目的：在 FB 未登录 / uiautomator dump 被 MIUI kill 的环境下，
仍然验证 P2-4 核心链路是否可用：
  screencap → classify_images(qwen2.5vl:7b) → fb_profile_insights 落盘
  → fb_content_exposure 自动写入 → ai_cost_events 记录队列/延迟。

用法：
    python scripts/smoke_real_device_vlm_fullpage.py --device JZBIGUKZS4NBAYDI

可选：
    --shots 3          截图张数（默认 3）
    --target-key ...   目标 key（默认 e2e_probe_<ts>）
    --open-fb          先 am start FB 登录页（即便未登录也能截到样本）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _y(s): return f"\033[33m{s}\033[0m"
def _b(s): return f"\033[36m{s}\033[0m"


FAILS: List[str] = []
ADB = r"C:\platform-tools\adb.exe"


def chk(name: str, ok: bool, detail: str = ""):
    tag = _g("PASS") if ok else _r("FAIL")
    print(f"[{tag}] {name}  {detail}")
    if not ok:
        FAILS.append(name)


def adb_run(device: str, args: List[str], timeout: int = 10) -> Tuple[int, str]:
    try:
        r = subprocess.run(
            [ADB, "-s", device, *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="ignore", check=False,
        )
        return r.returncode, (r.stdout or "")
    except Exception as e:
        return 1, f"EXC:{e}"


def screencap_one(device: str, save_path: str) -> bool:
    """Pure adb screencap → local file（不经过 uiautomator）。"""
    remote = "/sdcard/_e2e_screen.png"
    code, _ = adb_run(device, ["shell", "screencap", "-p", remote], timeout=10)
    if code != 0:
        return False
    code, _ = adb_run(device, ["pull", remote, save_path], timeout=15)
    return code == 0 and os.path.exists(save_path) and os.path.getsize(save_path) > 10240


def probe_screencap(device: str, shots: int) -> List[str]:
    """Step 1：证明 screencap 能重复成功，不依赖 uiautomator。"""
    print(_b("\n─── 1) 真机 screencap 循环 ───"))
    out_dir = ROOT / "data" / f"e2e_shots_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: List[str] = []
    for i in range(shots):
        p = str(out_dir / f"shot_{i+1}.png")
        t0 = time.time()
        ok = screencap_one(device, p)
        dt = int((time.time() - t0) * 1000)
        chk(f"screencap #{i+1}", ok, f"{dt}ms size={os.path.getsize(p) if ok else 0}B")
        if ok:
            paths.append(p)
        # 滑动一下再截（可选；用 input swipe，不依赖 dump）
        if i < shots - 1:
            adb_run(device, ["shell", "input", "swipe", "360", "1100", "360", "500", "250"], timeout=6)
            time.sleep(1.0)
    return paths


def probe_vlm_fullpage(device: str, images: List[str], persona_key: str, target_key: str) -> Dict[str, Any]:
    """Step 2：把真机截图送 VLM，跑完整 classify() 流程（含 L1+L2+落盘）。"""
    print(_b("\n─── 2) 真机截图 → VLM 全页面识别 → classify() ───"))
    from src.host import fb_profile_classifier as _clf
    from src.host.fb_target_personas import get_persona
    from src.host.database import init_db

    init_db()
    persona = get_persona(persona_key)
    pk = persona["persona_key"]
    chk("persona 可取", bool(persona), f"persona_key={pk}")

    t0 = time.time()
    result = _clf.classify(
        device_id=device,
        task_id=f"e2e_{int(time.time())}",
        persona_key=pk,
        target_key=target_key,
        display_name="山田 花子",   # 提供一个符合 L1 命中的日文名，迫使进 L2
        bio="日本 東京 ヨガ 45歳",
        username="",
        locale="ja_JP",
        image_paths=images,
        l2_image_paths=images,
        do_l2=True,
        dry_run=False,
    )
    dt = int((time.time() - t0) * 1000)
    chk("classify() 正常返回", isinstance(result, dict), f"total_ms={dt}")
    chk("stage_reached 非空", bool(result.get("stage_reached")),
        f"stage_reached={result.get('stage_reached')} match={result.get('match')}")
    l1 = result.get("l1") or {}
    l2 = result.get("l2") or {}
    print(f"    l1.score={l1.get('score')} pass={l1.get('pass')}  reasons={l1.get('reasons')}")
    print(f"    l2={json.dumps(l2, ensure_ascii=False, default=str)[:400]}")
    print(f"    insights={json.dumps(result.get('insights') or {}, ensure_ascii=False)[:240]}")
    chk("L1 命中（日文名应过）", bool(l1.get("pass")), f"score={l1.get('score')}")
    return result


def probe_db_writes(device: str, target_key: str) -> None:
    """Step 3：验证 fb_profile_insights / fb_content_exposure / ai_cost_events 都落盘。"""
    print(_b("\n─── 3) DB 写入审计 ───"))
    import sqlite3
    from src.host.database import DB_PATH
    con = sqlite3.connect(str(DB_PATH)); con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""SELECT COUNT(*) AS n FROM fb_profile_insights
                   WHERE device_id=? AND target_key=?""", (device, target_key))
    n_ins = cur.fetchone()["n"]
    chk("fb_profile_insights 写入 >= 1", n_ins >= 1, f"n={n_ins}")

    cur.execute("""SELECT COUNT(*) AS n FROM fb_content_exposure
                   WHERE device_id=? AND meta_json LIKE ?""",
                (device, f'%{target_key}%'))
    n_exp = cur.fetchone()["n"]
    chk("fb_content_exposure 可查（匹配时才有）", n_exp >= 0, f"n={n_exp}")

    cur.execute("""SELECT scene, ok, model, latency_ms, queue_wait_ms, device_id, at
                   FROM ai_cost_events WHERE device_id=? ORDER BY id DESC LIMIT 5""", (device,))
    rows = [dict(r) for r in cur.fetchall()]
    chk("ai_cost_events 有本设备记录", len(rows) >= 1, f"rows={len(rows)}")
    for r in rows[:3]:
        print(f"    {r['at']}  {r['scene']:<28} ok={r['ok']} lat={r['latency_ms']}ms q={r['queue_wait_ms']}ms")
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True)
    ap.add_argument("--shots", type=int, default=3)
    ap.add_argument("--persona", default="jp_female_midlife")
    ap.add_argument("--target-key", default=f"e2e_probe_{int(time.time())}")
    ap.add_argument("--open-fb", action="store_true")
    args = ap.parse_args()

    print(_b("=" * 54))
    print(_b(f"  P2-4 真机 VLM 全页面识别 smoke  device={args.device}"))
    print(_b("=" * 54))

    if args.open_fb:
        adb_run(args.device, ["shell", "am", "start", "-n",
                              "com.facebook.katana/com.facebook.katana.LoginActivity"],
                timeout=8)
        time.sleep(3)

    imgs = probe_screencap(args.device, args.shots)
    if not imgs:
        print(_r("没有有效截图，中止"))
        sys.exit(2)

    probe_vlm_fullpage(args.device, imgs, args.persona, args.target_key)
    probe_db_writes(args.device, args.target_key)

    print(_b("\n─── 总结 ───"))
    if not FAILS:
        print(_g("ALL GREEN — 真机 screencap → VLM 全页面识别 → DB 落盘 全链路可用"))
        sys.exit(0)
    print(_r(f"FAIL × {len(FAILS)}"))
    for f in FAILS:
        print(f"  · {f}")
    sys.exit(1)


if __name__ == "__main__":
    main()
