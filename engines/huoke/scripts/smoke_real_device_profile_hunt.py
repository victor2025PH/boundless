# -*- coding: utf-8 -*-
"""P2-4 Sprint C-2 真机 smoke：分层探测 profile_hunt 全链路。

三层模式（互不依赖，从安全到最真实）:
    --mode probe   默认，只探测: adb设备 / Ollama就绪 / DB可写 / 配置完整
    --mode vlm     探测 + 对一张样本图跑一次 qwen2.5vl:7b 推理, 校验 JSON
    --mode full    真机跑单人 profile_hunt(需提前登录好 Facebook + 连 VPN)

用法::
    python scripts/smoke_real_device_profile_hunt.py --mode probe
    python scripts/smoke_real_device_profile_hunt.py --mode vlm
    python scripts/smoke_real_device_profile_hunt.py --mode full \
        --device 4HDEWKFMJVUCTO6T --name "山田花子" --action none
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _g(s): return f"\033[32m{s}\033[0m"
def _r(s): return f"\033[31m{s}\033[0m"
def _y(s): return f"\033[33m{s}\033[0m"
def _b(s): return f"\033[36m{s}\033[0m"


fails: List[str] = []


def chk(name: str, ok: bool, detail: str = ""):
    tag = _g("PASS") if ok else _r("FAIL")
    print(f"[{tag}] {name}  {detail}")
    if not ok:
        fails.append(name)


# ─────────────────────────────────────────────────────────────
def probe_adb() -> List[str]:
    print(_b("\n─── 1) ADB 设备探测 ───"))
    try:
        out = subprocess.run(
            [r"C:\platform-tools\adb.exe", "devices"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except FileNotFoundError:
        chk("adb.exe 存在", False, "请安装 platform-tools 到 C:\\platform-tools")
        return []
    lines = [l.strip() for l in out.stdout.splitlines() if "\t" in l]
    devs = [l.split("\t")[0] for l in lines if l.endswith("device")]
    chk("adb devices 至少 1 台在线", len(devs) >= 1, f"devices={devs}")
    for d in devs:
        print(f"    · {d}")
    return devs


def probe_ollama() -> bool:
    print(_b("\n─── 2) Ollama 健康 ───"))
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        chk("Ollama /api/tags 可达", False, f"err={e}")
        return False
    chk("Ollama /api/tags 可达", True)
    models = [m.get("name") for m in (data.get("models") or [])]
    has_vlm = any("qwen2.5vl" in (m or "") for m in models)
    chk("qwen2.5vl:7b 已拉取", has_vlm, f"models={models[:5]}")
    return has_vlm


def probe_db() -> bool:
    print(_b("\n─── 3) DB 迁移检查 ───"))
    try:
        from src.host.database import init_db, get_conn
    except Exception as e:
        chk("加载 database 模块", False, str(e))
        return False
    try:
        init_db()
    except Exception as e:
        chk("init_db() 成功", False, str(e))
        return False
    chk("init_db() 成功", True)
    need_tables = ["fb_target_personas", "fb_profile_insights", "fb_content_exposure", "ai_cost_events"]
    try:
        with get_conn() as conn:
            rows = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception as e:
        chk("读表列表", False, str(e))
        return False
    for t in need_tables:
        chk(f"表 {t} 存在", t in rows)

    # C-1 新加的列
    try:
        with get_conn() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_cost_events)").fetchall()]
        chk("ai_cost_events.queue_wait_ms 列存在", "queue_wait_ms" in cols, f"cols={cols[-5:]}")
        chk("ai_cost_events.device_id 列存在", "device_id" in cols)
    except Exception as e:
        chk("PRAGMA table_info", False, str(e))
    return True


def probe_config() -> bool:
    print(_b("\n─── 4) persona 配置 ───"))
    try:
        from src.host.fb_target_personas import (
            list_personas, get_persona, get_quotas, get_vlm_config, get_risk_guard,
        )
    except Exception as e:
        chk("加载 persona 模块", False, str(e))
        return False
    p = list_personas()
    chk("list_personas() 非空", len(p) >= 1, f"count={len(p)}")
    if p:
        d = get_persona(p[0].get("persona_key"))
        chk("default persona 可取", bool(d))
        chk("persona.l1.rules 存在", len((d.get("l1") or {}).get("rules") or []) >= 3,
            f"rules={len((d.get('l1') or {}).get('rules') or [])}")
    q = get_quotas() or {}
    chk("quotas.l2_per_device_per_day", q.get("l2_per_device_per_day"), f"={q.get('l2_per_device_per_day')}")
    chk("quotas.l2_per_device_per_hour (C-1 新增)", q.get("l2_per_device_per_hour"),
        f"={q.get('l2_per_device_per_hour')}")
    v = get_vlm_config() or {}
    chk("vlm.model = qwen2.5vl:7b", v.get("model") == "qwen2.5vl:7b", f"={v.get('model')}")
    chk("vlm.num_ctx = 4096（防 OOM）", int(v.get("num_ctx") or 0) <= 8192,
        f"={v.get('num_ctx')}")
    g = get_risk_guard() or {}
    chk("risk_guard.pause_l2_after_risk_hours", "pause_l2_after_risk_hours" in g,
        f"={g.get('pause_l2_after_risk_hours')}h")
    return True


def probe_vlm_inference():
    print(_b("\n─── 5) VLM 单次推理（不带图，纯文本触发模型加载） ───"))
    from src.host import ollama_vlm
    t0 = time.time()
    hc = ollama_vlm.check_health()
    chk("ollama_vlm.check_health() online + model_available",
        hc.get("online") and hc.get("model_available"),
        f"online={hc.get('online')} model={hc.get('model')} avail={hc.get('model_available')} "
        f"latency={int((time.time()-t0)*1000)}ms")

    # 纯文本调一次 generate 让 qwen2.5vl 加载进显存（首次会慢）
    prompt = "Reply with exactly this JSON only: {\"ping\":\"pong\"}"
    t1 = time.time()
    raw, meta = ollama_vlm.generate(
        prompt=prompt, scene="smoke_c2_probe", task_id="c2-probe", device_id="probe",
    )
    dt = int((time.time() - t1) * 1000)
    chk("VLM generate ok (文本, 冷/热启动均计入)", meta.get("ok"),
        f"total_ms={meta.get('total_ms') or dt} latency={meta.get('latency_ms')} queue={meta.get('queue_wait_ms')}")
    chk("VLM 返回含 'pong'", "pong" in (raw or "").lower(),
        f"raw={repr((raw or '')[:80])}")

    # 第二次（应该已预热，快得多）
    t2 = time.time()
    raw2, meta2 = ollama_vlm.generate(
        prompt=prompt, scene="smoke_c2_warm", task_id="c2-probe", device_id="probe",
    )
    dt2 = int((time.time() - t2) * 1000)
    chk("VLM generate 第二次（预热后）", meta2.get("ok"),
        f"total_ms={meta2.get('total_ms') or dt2}")
    # 并发指标
    st = ollama_vlm.get_concurrency_stats()
    chk("VLM concurrency_stats 累加", st["total_calls"] >= 2, f"{st}")


def probe_vlm_image():
    """用一张人脸占位图做最小 classify，看 JSON 能不能回。"""
    print(_b("\n─── 6) VLM 图像推理（持久合成小图） ───"))
    from PIL import Image, ImageDraw
    from src.host import ollama_vlm

    tmp = ROOT / "data" / f"c2_smoke_{int(time.time())}.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (320, 320), (255, 240, 220))
    d = ImageDraw.Draw(im)
    d.rectangle((80, 80, 240, 240), outline=(80, 80, 80), width=3)
    d.text((96, 140), "Yamada\nJapan", fill=(30, 30, 30))
    im.save(tmp, "JPEG", quality=85)

    prompt = ("画像に写っている情報について、必ず次の JSON のみを返してください"
              "（他の文章不要）：{\"has_text\": true/false, \"language\": \"...\"}")
    t0 = time.time()
    insights, meta = ollama_vlm.classify_images(
        prompt=prompt, image_paths=[str(tmp)],
        scene="smoke_c2_image", task_id="c2-probe", device_id="probe",
    )
    dt = int((time.time() - t0) * 1000)
    chk("classify_images ok", meta.get("ok"),
        f"total_ms={meta.get('total_ms') or dt} queue={meta.get('queue_wait_ms')}")
    chk("返回可解析为 JSON dict", isinstance(insights, dict) and len(insights) > 0,
        f"insights={insights}")
    try:
        tmp.unlink()
    except Exception:
        pass


def probe_real_hunt(device_id: str, name: str, action: str, persona_key: str):
    """真机跑一个人的 profile_hunt（要求 FB 已登录 + VPN 在线）。"""
    print(_b(f"\n─── 7) 真机 profile_hunt (device={device_id} name='{name}' action={action}) ───"))
    # 环境确认
    try:
        out = subprocess.run(
            [r"C:\platform-tools\adb.exe", "-s", device_id, "shell", "dumpsys", "activity", "top"],
            capture_output=True, text=True, timeout=8, check=False,
            encoding="utf-8", errors="ignore",
        )
    except Exception as e:
        chk("adb shell 可达", False, str(e))
        return
    stdout = out.stdout or ""
    ok_adb = bool(stdout)
    chk("adb -s device 可通信", ok_adb)
    in_fb = "com.facebook.katana" in stdout
    if not in_fb:
        print(_y(f"    ⚠ 当前前台不是 Facebook，脚本将尝试启动 FB。若失败请手动打开 FB 并登录。"))

    from src.host.executor import _execute_facebook
    from src.device_control.device_manager import get_device_manager
    cfg_path = str(ROOT / "config" / "devices.yaml")
    mgr = get_device_manager(cfg_path)
    try:
        mgr.discover_devices(force=True)
    except Exception as _e:
        print(_y(f"    ⚠ discover_devices 异常: {_e}"))
    info = mgr.get_device_info(device_id)
    chk("DeviceManager 已注册目标设备", info is not None,
        f"info={'' if info is None else info.display_name}")
    if info is None:
        print(_r(f"    [ABORT] 设备 {device_id} 未被 DeviceManager 识别，跳过 profile_hunt"))
        return
    t0 = time.time()
    try:
        ok, msg, result = _execute_facebook(
            mgr, device_id, "facebook_profile_hunt",
            {
                "candidates": [name],
                "persona_key": persona_key,
                "action_on_match": action,
                "max_targets": 1,
                "inter_target_min_sec": 5,
                "inter_target_max_sec": 8,
                "shot_count": 3,
                "note": "",
            },
        )
    except Exception as e:
        import traceback as _tb
        ok, msg, result = False, f"EXC:{type(e).__name__}:{e}", None
        print(_r(f"    异常堆栈:\n{_tb.format_exc()}"))
    dt = int(time.time() - t0)
    chk("_execute_facebook 返回（不崩）", ok is not None,
        f"ok={ok} elapsed={dt}s msg={msg}")
    try:
        print(f"    raw_result = {json.dumps(result, ensure_ascii=False, default=str)[:800]}")
    except Exception:
        print(f"    raw_result(repr) = {repr(result)[:800]}")
    stats = (result or {}).get("stats") if isinstance(result, dict) else None
    if isinstance(stats, dict):
        chk("stats.card_type == fb_profile_hunt",
            stats.get("card_type") == "fb_profile_hunt",
            f"card_type={stats.get('card_type')}")
        chk("processed >= 1", int(stats.get("processed") or 0) >= 1,
            f"processed={stats.get('processed')}")
        sk = stats.get("skipped") or {}
        print(f"    skipped={sk}")
        print(f"    results={(stats.get('results') or [])[:3]}")


# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "vlm", "full"], default="probe")
    ap.add_argument("--device", default="", help="真机 serial（full 模式必填）")
    ap.add_argument("--name", default="山田花子", help="候选昵称（full 模式）")
    ap.add_argument("--action", default="none",
                    choices=["none", "follow", "add_friend"])
    ap.add_argument("--persona", default="jp_female_midlife")
    args = ap.parse_args()

    print(_b("======================================================"))
    print(_b(f"  P2-4 C-2 真机 smoke  mode={args.mode}"))
    print(_b("======================================================"))

    devs = probe_adb()
    if not probe_ollama():
        print(_r("\nOllama 未就绪，停止。"))
        return 1
    if not probe_db():
        print(_r("\nDB 不健康，停止。"))
        return 1
    if not probe_config():
        print(_r("\nconfig 异常，停止。"))
        return 1

    if args.mode in ("vlm", "full"):
        probe_vlm_inference()
        probe_vlm_image()

    if args.mode == "full":
        if not args.device:
            if not devs:
                print(_r("\n无在线设备，无法跑 full。"))
                return 1
            args.device = devs[0]
            print(_y(f"\n[自动选用] --device {args.device}"))
        if args.device not in devs:
            print(_r(f"\n[ERR] --device {args.device} 不在线（devices={devs}）"))
            return 1
        probe_real_hunt(args.device, args.name, args.action, args.persona)

    print(_b("\n─── 总结 ───"))
    if fails:
        print(_r(f"FAIL × {len(fails)}"))
        for f in fails:
            print(_r(f"  · {f}"))
        return 2
    print(_g(f"ALL GREEN ({args.mode} mode)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
