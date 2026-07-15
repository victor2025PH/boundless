# -*- coding: utf-8 -*-
"""
实时同传全链路自检：一屏看清各微服务健康 + 关键阶段延迟(NMT/STT/TTS)，并高亮红旗
(如换脸静默掉到 CPU、STT/翻译引擎异常)。轻量·只读探测(不改任何运行状态)。

与 acceptance.py 的重型回归套件互补：acceptance 做深度端到端回归；本工具回答“此刻链路是否健康且够快”，
适合开播前/排障时秒级体检。健康扫描并发进行(快)；延迟探测按需、失败不影响其余项。

用法：
  python selfcheck_pipeline.py             # 健康扫描 + 关键延迟探测
  python selfcheck_pipeline.py --no-latency  # 只做健康扫描(最快)
  python selfcheck_pipeline.py --json        # 机器可读
"""
import argparse
import base64
import concurrent.futures as _cf
import json
import os
import sys
import time

import requests

import app_config

# 延迟阈值(ms)：仅用于给出“慢”提示，非硬性失败
_SLOW = {"nmt": 800, "stt": 2500, "tts_clone": 2500, "tts_ttfa": 1500}


def _get_json(url, timeout=3):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _summarize(key: str, j: dict) -> str:
    """各服务 /health 里最值得一眼看到的关键信息。"""
    if key == "faceswap":
        return (f"backend={j.get('execution_backend')} "
                f"active={j.get('swap_providers_active')} model={j.get('swap_model')}")
    if key == "stt":
        nllb = (j.get("nllb") or {}).get("loaded")
        return f"asr_backend={j.get('backend')} mt_engine={j.get('translate_engine')} nllb_loaded={nllb}"
    if key == "interpreter":
        return "ok"
    return "ok"


def _flags(key: str, j: dict) -> list:
    """从 /health 提取红旗(需要人关注的异常)。"""
    f = []
    if key == "faceswap" and j.get("swap_cpu_only"):
        f.append("换脸实际在 CPU(实时不可用)→ 排查 GPU EP/TensorRT 运行库")
    if key == "stt" and j.get("loaded") is False:
        f.append("Whisper 未加载(流式模式下可能正常；分段模式首句会慢)")
    return f


def _scan_one(key: str, svc: dict) -> dict:
    url = app_config.svc_url(key)
    row = {"key": key, "label": svc.get("label", key), "url": url,
           "core": bool(svc.get("core", False))}
    t = time.time()
    try:
        j = _get_json(url + svc.get("health", "/health"), timeout=3)
        row.update(up=True, ms=int((time.time() - t) * 1000),
                   info=_summarize(key, j), flags=_flags(key, j))
    except Exception as e:
        row.update(up=False, err=type(e).__name__)
    return row


def scan_health() -> list:
    """并发扫描所有 app_config.SERVICES 的 /health(短超时)。"""
    svcs = list(app_config.SERVICES.items())
    rows = []
    with _cf.ThreadPoolExecutor(max_workers=min(12, len(svcs))) as ex:
        futs = {ex.submit(_scan_one, k, v): k for k, v in svcs}
        for fu in _cf.as_completed(futs):
            rows.append(fu.result())
    order = {k: i for i, k in enumerate(app_config.SERVICES)}
    rows.sort(key=lambda r: order.get(r["key"], 999))
    return rows


def probe_nmt(stt_url: str) -> dict:
    t = time.time()
    r = requests.post(stt_url + "/translate",
                      json={"text": "你好，这是一次链路自检。", "src": "zh", "dest": "en"}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return {"ms": int((time.time() - t) * 1000), "server_ms": j.get("elapsed_ms"),
            "out": (j.get("text") or "")[:60]}


def probe_stt(stt_url: str) -> dict:
    import bench_tts
    b64 = base64.b64encode(bench_tts._gen_sine_wav(2.0)).decode()
    t = time.time()
    r = requests.post(stt_url + "/transcribe_b64",
                      json={"audio_base64": b64, "language": "zh", "task": "transcribe"}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return {"ms": int((time.time() - t) * 1000), "server_s": j.get("elapsed_s"),
            "out": (j.get("text") or "")[:40]}


def probe_tts(engine: str) -> dict:
    import bench_tts
    url = bench_tts._resolve_url(engine)
    if not bench_tts._healthy(url):
        return {"engine": engine, "up": False}
    ref_b64, ref_text = bench_tts._load_ref("")
    text = "Hello, this is a pipeline self-check."
    res = {"engine": engine, "up": True, "url": url}
    try:
        bench_tts._clone_once(url, text, ref_b64, ref_text)                 # 预热(不计)
        res["clone_ms"] = int(bench_tts._clone_once(url, text, ref_b64, ref_text))
    except Exception as e:
        res["clone_err"] = str(e)[:80]
    if engine in ("fish", "qwen3"):
        try:
            ttfa, _ = bench_tts._clone_stream_once(url, text, ref_b64, ref_text)
            res["stream_ttfa_ms"] = int(ttfa) if ttfa else None
        except Exception as e:
            res["stream_err"] = str(e)[:80]
    return res


def _static_core_set() -> set:
    """静态兜底核心集：SERVICES 里 core=True 的服务（Hub 不可达/无 broadcast 时用）。"""
    return {k for k, v in app_config.SERVICES.items() if v.get("core")}


def _effective_core(timeout: float = 2.5) -> tuple:
    """当前"核心服务"的单一真源：优先 Hub /health 的 broadcast.core（开播模式感知——
    真人换脸模式含 faceswap、不含 lipsync/vcam）；Hub 不可达/无 broadcast 时回落静态
    SERVICES.core（多机/离线不强依赖 Hub）。返回 (core_set, source)。只读、失败静默。"""
    try:
        j = _get_json(app_config.svc_url("hub") + "/health", timeout=timeout)
        bc = j.get("broadcast") or {}
        if bc.get("core"):
            return set(bc["core"]), "broadcast:" + str(bc.get("mode") or "?")
    except Exception:
        pass
    return _static_core_set(), "static"


def run(do_latency: bool) -> dict:
    health = scan_health()
    core_set, core_src = _effective_core()        # 核心判定统一到模式感知真源（回落静态）
    for r in health:
        r["core"] = r["key"] in core_set          # 覆盖 _scan_one 的静态默认，消除误报/漏报
    up_keys = {r["key"] for r in health if r.get("up")}
    result = {"health": health, "latency": {}, "flags": [], "core_source": core_src}
    for r in health:
        for fl in r.get("flags", []):
            result["flags"].append(f"{r['key']}: {fl}")
        if r.get("core") and not r.get("up"):
            result["flags"].append(f"{r['key']}: 核心服务未就绪")

    # S7: 换脸容灾态红旗——画面正由本机副本接管时，看板必须一眼看到（主引擎回来会自动回切，
    #   但接管期>几分钟通常意味着 .104 看门狗没把引擎拉起来，需人工介入）。只读 Hub，失败静默。
    try:
        fo = _get_json(app_config.svc_url("hub") + "/api/dfm/current", timeout=9).get("failover") or {}
        if fo.get("on_replica"):
            result["flags"].append("faceswap: 容灾副本接管中(主引擎失联·画质降级无锐化)——超过几分钟未自动回切需查 .104 看门狗")
        result["faceswap_failover"] = fo
    except Exception:
        pass

    # S8-3: STT 单点容灾态红旗——同传转写正由备用端点接管时看板一眼可见（主恢复自动切回，
    #   但持续接管通常意味着主 STT 节点(.140)没被 mem_watchdog 拉起来，需人工介入）。只读，失败静默。
    try:
        sf = _get_json(app_config.svc_url("interpreter") + "/metrics", timeout=6).get("stt_failover") or {}
        if sf.get("on_fallback"):
            result["flags"].append(f"STT: 备用端点接管中(主转写节点失联{sf.get('engaged_s','?')}s)——持续未切回需查主 STT 节点")
        result["stt_failover"] = sf
    except Exception:
        pass

    # S9-1: 单点保护全景——裸单点(远端+单副本+无降级)是最高优先加固对象，看板/巡检一眼可见。
    try:
        _sec = _get_json(app_config.svc_url("hub") + "/api/ops/snapshot", timeout=8).get("security") or {}
        spm = _sec.get("single_point_map") or {}
        bare = [d.get("label") for d in spm.get("deps", []) if d.get("grade") == "bare"]
        if bare:
            result["flags"].append(f"单点保护: {len(bare)} 个核心依赖裸单点无兜底（{'、'.join(bare)}）——建议配副本/备用端点")
        result["single_point_map"] = spm

        # S9-2: 自训素材就绪度——料不足(starved)会烧废卡，闭环已拦；这里让巡检也看得见。
        tm = _sec.get("train_material") or {}
        starved = [k for k, c in (tm.get("chars") or {}).items() if c.get("grade") == "starved"]
        if starved:
            result["flags"].append(f"自训素材: {len(starved)} 个角色对齐脸<{tm.get('floor',300)}张练必废(已拦)（{'、'.join(starved)}）——需扩充素材")
        result["train_material"] = tm
    except Exception:
        pass

    if do_latency and "stt" in up_keys:
        try:
            result["latency"]["nmt"] = probe_nmt(app_config.svc_url("stt"))
        except Exception as e:
            result["latency"]["nmt"] = {"err": str(e)[:80]}
        try:
            result["latency"]["stt"] = probe_stt(app_config.svc_url("stt"))
        except Exception as e:
            result["latency"]["stt"] = {"err": str(e)[:80]}
    if do_latency:
        tts = {}
        for eng, key in (("fish", "fish_tts"), ("qwen3", "qwen3_tts"), ("cosyvoice", "emotion_tts")):
            if key in up_keys:
                try:
                    tts[eng] = probe_tts(eng)
                except Exception as e:
                    tts[eng] = {"engine": eng, "err": str(e)[:80]}
        if tts:
            result["latency"]["tts"] = tts

    # 慢提示
    lat = result["latency"]
    if isinstance(lat.get("nmt"), dict) and isinstance(lat["nmt"].get("ms"), int) and lat["nmt"]["ms"] > _SLOW["nmt"]:
        result["flags"].append(f"NMT 偏慢 {lat['nmt']['ms']}ms(>{_SLOW['nmt']})")
    if isinstance(lat.get("stt"), dict) and isinstance(lat["stt"].get("ms"), int) and lat["stt"]["ms"] > _SLOW["stt"]:
        result["flags"].append(f"STT 偏慢 {lat['stt']['ms']}ms(>{_SLOW['stt']})")
    for eng, d in (lat.get("tts") or {}).items():
        if isinstance(d.get("clone_ms"), int) and d["clone_ms"] > _SLOW["tts_clone"]:
            result["flags"].append(f"TTS[{eng}] clone 偏慢 {d['clone_ms']}ms")
        if isinstance(d.get("stream_ttfa_ms"), int) and d["stream_ttfa_ms"] > _SLOW["tts_ttfa"]:
            result["flags"].append(f"TTS[{eng}] 首包偏慢 {d['stream_ttfa_ms']}ms")

    result["ok"] = len(result["flags"]) == 0
    return result


# ── 历史留痕（CLI/计划任务与 /ops 看板共用同一份，趋势覆盖所有触发方式）────────
_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "selfcheck_history.jsonl")
_HISTORY_MAX = 300


def _compact(res: dict) -> dict:
    """一次自检的紧凑留档（供趋势/看板；不含完整健康表以控体积）。"""
    lat = res.get("latency") or {}

    def _ms(x):
        return x.get("ms") if isinstance(x, dict) else None
    fish = (lat.get("tts") or {}).get("fish") or {}
    return {"ts": res.get("ts") or time.time(), "ok": bool(res.get("ok")),
            "up": sum(1 for r in res.get("health", []) if r.get("up")),
            "total": len(res.get("health", [])),
            "nflags": len(res.get("flags", [])),
            "flags": (res.get("flags") or [])[:6],
            "nmt_ms": _ms(lat.get("nmt")), "stt_ms": _ms(lat.get("stt")),
            "tts_fish_clone": fish.get("clone_ms"), "tts_fish_ttfa": fish.get("stream_ttfa_ms")}


def append_history(res: dict) -> None:
    """把一次自检结果紧凑追加到 logs/selfcheck_history.jsonl（滚动上限，失败静默）。"""
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        lines = []
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(_compact(res), ensure_ascii=False))
        lines = lines[-_HISTORY_MAX:]
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def read_history(limit: int = 40) -> list:
    """读近 limit 条自检历史（供 /ops 看板画趋势）。"""
    out = []
    if os.path.exists(_HISTORY_FILE):
        try:
            with open(_HISTORY_FILE, encoding="utf-8") as f:
                tail = f.read().splitlines()[-max(1, min(int(limit), _HISTORY_MAX)):]
            for ln in tail:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        pass
        except Exception:
            pass
    return out


def _alert_checks(res: dict) -> list:
    """从自检结果派生确定性告警项：(key, fired, title, detail, level)。fired=True 需告警、False 表示恢复。"""
    checks = []
    for r in res["health"]:
        if r.get("core"):
            checks.append((f"svc_down:{r['key']}", not r.get("up"),
                           f"核心服务离线: {r['key']} ({r.get('label', '')})",
                           r.get("err", "") or r.get("url", ""), "error"))
    fs = next((r for r in res["health"] if r["key"] == "faceswap"), None)
    if fs and fs.get("up"):
        cpu = any("CPU" in fl for fl in fs.get("flags", []))
        checks.append(("faceswap_cpu", cpu, "换脸掉到 CPU(实时不可用)",
                       "; ".join(fs.get("flags", [])) or "swap_cpu_only", "error"))
    lat = res.get("latency") or {}
    if isinstance(lat.get("nmt"), dict) and isinstance(lat["nmt"].get("ms"), int):
        checks.append(("slow:nmt", lat["nmt"]["ms"] > _SLOW["nmt"], "NMT 偏慢",
                       f"{lat['nmt']['ms']}ms(>{_SLOW['nmt']})", "warning"))
    if isinstance(lat.get("stt"), dict) and isinstance(lat["stt"].get("ms"), int):
        checks.append(("slow:stt", lat["stt"]["ms"] > _SLOW["stt"], "STT 偏慢",
                       f"{lat['stt']['ms']}ms(>{_SLOW['stt']})", "warning"))
    for eng, d in (lat.get("tts") or {}).items():
        if isinstance(d.get("clone_ms"), int):
            checks.append((f"slow:tts_clone:{eng}", d["clone_ms"] > _SLOW["tts_clone"],
                           f"TTS[{eng}] clone 偏慢", f"{d['clone_ms']}ms(>{_SLOW['tts_clone']})", "warning"))
    return checks


def sync_alerts(res: dict) -> dict:
    """把确定性告警项同步到 alerts.py：fired→raise_alert、否则 clear_alert(恢复)。key 以 selfcheck: 前缀，稳定去抖。"""
    try:
        import alerts
    except Exception as e:
        return {"error": f"alerts 不可用: {e}"}
    fired, cleared = [], []
    for key, is_fired, title, detail, level in _alert_checks(res):
        k = f"selfcheck:{key}"
        if is_fired:
            alerts.raise_alert(k, title, detail=detail, level=level, source="selfcheck")
            fired.append(k)
        else:
            alerts.clear_alert(k, note="selfcheck 恢复")
            cleared.append(k)
    return {"fired": fired, "cleared": cleared}


def _print_human(res: dict):
    print("=" * 72)
    print("实时同传全链路自检")
    print("=" * 72)
    up = sum(1 for r in res["health"] if r.get("up"))
    print(f"服务健康：{up}/{len(res['health'])} 在线")
    _csrc = res.get("core_source")
    if _csrc:
        print(f"核心判定：{_csrc}（broadcast=开播模式感知，static=Hub不可达回落）")
    print()
    for r in res["health"]:
        mark = "[OK]  " if r.get("up") else "[DOWN]"
        core = "★" if r.get("core") else " "
        line = f"{mark}{core} {r['key']:<12} {r['label']}"
        if r.get("up"):
            line += f"  ({r['ms']}ms)  {r.get('info', '')}"
        else:
            line += f"  ✗ {r.get('err', '')}  {r['url']}"
        print(line)
        for fl in r.get("flags", []):
            print(f"        ⚠ {fl}")

    lat = res.get("latency") or {}
    if lat:
        print("\n关键阶段延迟：")
        if isinstance(lat.get("nmt"), dict):
            d = lat["nmt"]
            print(f"  NMT(翻译)     {d.get('ms', '?')}ms (server {d.get('server_ms', '?')}ms)  译:“{d.get('out', '')}”"
                  if "err" not in d else f"  NMT(翻译)     失败 {d['err']}")
        if isinstance(lat.get("stt"), dict):
            d = lat["stt"]
            print(f"  STT(转写)     {d.get('ms', '?')}ms (server {d.get('server_s', '?')}s)"
                  if "err" not in d else f"  STT(转写)     失败 {d['err']}")
        for eng, d in (lat.get("tts") or {}).items():
            if not d.get("up", True):
                continue
            seg = f"  TTS[{eng}]"
            if "clone_ms" in d:
                seg += f"   clone {d['clone_ms']}ms"
            if d.get("stream_ttfa_ms") is not None:
                seg += f"  首包 {d['stream_ttfa_ms']}ms"
            if d.get("clone_err") or d.get("stream_err"):
                seg += f"   失败 {d.get('clone_err') or d.get('stream_err')}"
            print(seg)

    print("\n" + ("[通过] 未发现红旗。" if res["ok"] else f"[注意] {len(res['flags'])} 项需关注："))
    for fl in res["flags"]:
        print(f"  ⚠ {fl}")
    # 机器可解析摘要行(供 acceptance.py 等据 '== ... PASS/FAIL ==' 判定)
    print(f"== 全链路自检 {'PASS' if res['ok'] else 'FAIL'} ({up}/{len(res['health'])}在线, {len(res['flags'])}红旗) ==")


def main() -> int:
    ap = argparse.ArgumentParser(description="实时同传全链路自检")
    ap.add_argument("--no-latency", action="store_true", help="只做健康扫描(不测延迟)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--alert", action="store_true",
                    help="把红旗(换脸掉CPU/核心离线/阶段偏慢)同步到 alerts.py 自动告警(含恢复通知)；适合周期性运行")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC",
                    help="常态巡检：每 SEC 秒循环一次(配合 --alert 即持续自动告警)；0=单次(默认)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.watch and args.watch > 0:      # 常态巡检模式：紧凑单行/轮，Ctrl+C 退出
        import datetime
        print(f"[巡检] 每 {args.watch}s 一轮，Ctrl+C 退出。告警={'开' if args.alert else '关'} "
              f"延迟探测={'关' if args.no_latency else '开'}", flush=True)
        try:
            while True:
                res = run(do_latency=not args.no_latency)
                if args.alert:
                    res["alert_sync"] = sync_alerts(res)
                append_history(res)
                up = sum(1 for r in res["health"] if r.get("up"))
                a = res.get("alert_sync") or {}
                asum = f"  告警+{len(a.get('fired', []))}/-{len(a.get('cleared', []))}" if args.alert else ""
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                tail = ("：" + "；".join(res["flags"][:3])) if res["flags"] else ""
                print(f"[{ts}] {up}/{len(res['health'])}在线 红旗{len(res['flags'])}{asum}{tail}", flush=True)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n[巡检] 已停止。")
            return 0

    res = run(do_latency=not args.no_latency)
    if args.alert:
        res["alert_sync"] = sync_alerts(res)
    append_history(res)
    if args.json:
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_human(res)
        if args.alert and isinstance(res.get("alert_sync"), dict):
            a = res["alert_sync"]
            if a.get("error"):
                print(f"\n[告警] 同步失败：{a['error']}")
            else:
                print(f"\n[告警] 已触发 {len(a.get('fired', []))} 项、恢复 {len(a.get('cleared', []))} 项 → logs/alerts.jsonl")
    return 0 if res["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
