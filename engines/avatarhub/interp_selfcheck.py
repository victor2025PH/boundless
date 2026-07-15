# -*- coding: utf-8 -*-
"""
实时同传 真机联调自检
─────────────────────────────────────────────────────────────────────────
1) 服务体检：STT(含本地MT) / Fish克隆TTS / Hub角色
2) 设备体检：我的麦 / 通话App虚拟麦(CABLE Output) / 抓对方声(立体声混音)
3) 延迟实测：用角色参考音切一段当"短句" → ASR(中)+NMT(英)+首块合成 = 对方听到首音(TTFA)
4) 直播链路(可选)：lipsync+vcam 健康 / 人脸预热 / 口型流式首段(TTFV) / 真人待机

运行(facefusion 环境)：python interp_selfcheck.py
  python interp_selfcheck.py --live   # 含直播链路探测
"""
import sys, time, base64, struct, argparse, requests
try:                                              # Windows 控制台默认 GBK,无法输出 ✓/⚠ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from urllib.parse import quote
import numpy as np
import sounddevice as sd
import live_interpreter as L

STT = L.STT_URL
HUB = L.HUB_URL
FISH = L.FISH_URL
LS = L.LIPSYNC_URL
VC = L.VCAM_URL
OK, BAD = "  [OK] ", "  [!!] "


def hr(t): print("\n" + "=" * 60 + f"\n {t}\n" + "=" * 60)


def check_services():
    hr("1) 服务体检")
    ok = True
    try:
        h = requests.get(f"{STT}/health", timeout=5).json()
        mt = h.get("mt_loaded", [])
        print((OK if h.get("loaded") else BAD) + f"STT whisper={h.get('model')} loaded={h.get('loaded')}")
        print((OK if len(mt) >= 2 else BAD) + f"本地MT 方向={mt}")
        ok &= h.get("loaded") and len(mt) >= 2
    except Exception as e:
        print(BAD + f"STT 不可达: {e}"); ok = False
    try:
        requests.get(f"{FISH}/health", timeout=5); print(OK + f"Fish 克隆TTS 在线")
    except Exception as e:
        print(BAD + f"Fish 不可达: {e}"); ok = False
    try:
        j = requests.get(f"{HUB}/profiles", timeout=5).json()
        print(OK + f"Hub 活动角色={j.get('active')!r}")
    except Exception as e:
        print(BAD + f"Hub 不可达: {e}"); ok = False
    return ok


def check_devices():
    hr("2) 设备体检")
    try:
        d = requests.get(f"http://127.0.0.1:7900/devices", timeout=8).json()
        defs = d["defaults"]
        print((OK if defs.get("mic") is not None else BAD) + f"我的麦 index={defs.get('mic')}")
        print((OK if defs.get("cable") is not None else BAD) +
              f"通话App虚拟麦(CABLE Input)输出 index={defs.get('cable')}  ← 配音播到这里")
        print((OK if defs.get("loopback") is not None else BAD) +
              f"抓对方声(立体声混音) index={defs.get('loopback')}")
        return defs
    except Exception as e:
        print(BAD + f"interp /devices 不可达(先启动 live_interpreter): {e}")
        return {}


def _stream_first_block(en):
    """实测流式首块:返回 (首块到达ms, 总耗时ms, 首块float32单声道, sr)。"""
    payload = {"text": en, "language": "en", "temperature": 0.7, "top_p": 0.7,
               "repetition_penalty": 1.2, "seed": 42,
               "reference_audio_b64": L.ST.voice_b64, "reference_text": L.ST.ref_text}
    t0 = time.time(); first_ms = None; first_pcm = None
    with requests.post(f"{FISH}/v1/tts/clone/stream", json=payload, stream=True, timeout=120) as r:
        sr = int(r.headers.get("X-Sample-Rate", "44100")); buf = b""
        for raw in r.iter_content(chunk_size=4096):
            buf += raw
            while len(buf) >= 4:
                ln = struct.unpack("<I", buf[:4])[0]
                if ln == 0:
                    buf = b""; break
                if len(buf) < 4 + ln:
                    break
                pcm = buf[4:4+ln]; buf = buf[4+ln:]
                if len(pcm) < 2205:
                    continue
                if first_ms is None:
                    first_ms = (time.time() - t0) * 1000
                    first_pcm = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return first_ms, (time.time() - t0) * 1000, first_pcm, sr


def check_latency(cable_index):
    hr("3) 端到端延迟实测(对方听到首音 TTFA · 流式管路)")
    name = requests.get(f"{HUB}/profiles", timeout=5).json().get("active", "")
    L.ST.voice_b64, L.ST.ref_text = L._fetch_voice_ref(name)
    L._prewarm_ref(L.ST.voice_b64, L.ST.ref_text)
    data, sr = L._wav_bytes_to_f32(base64.b64decode(L.ST.voice_b64))
    clip = data[:int(sr * 2.5)]
    mono16k = L._resample(clip, sr, L.SR)

    t = time.time(); zh = L._stt(mono16k, "zh", task="transcribe"); t_asr = (time.time() - t) * 1000
    t = time.time(); en = L._translate_nmt(zh, "zh", "en"); t_nmt = (time.time() - t) * 1000
    en = en or "One step at a time."
    first_ms, total_ms, first_pcm, fsr = _stream_first_block(en)

    print(f"  ASR(中)   {t_asr:5.0f}ms : {zh[:34]}")
    print(f"  NMT(英)   {t_nmt:5.0f}ms : {en[:34]}")
    print(f"  流式首块   {first_ms:5.0f}ms (整句流总耗时 {total_ms:.0f}ms)")
    print(f"  ── 对方听到首音(TTFA) ≈ {t_asr + t_nmt + (first_ms or 0):.0f}ms ──")

    if cable_index is not None and first_pcm is not None:
        try:
            L.ST.play_sr, L.ST.play_ch = L._dev_out_params(cable_index)
            d = L._resample(first_pcm, fsr, L.ST.play_sr) if fsr != L.ST.play_sr else first_pcm
            if L.ST.play_ch >= 2:
                d = np.column_stack([d] * L.ST.play_ch)
            sd.play(np.ascontiguousarray(d, np.float32), L.ST.play_sr, device=cable_index, blocking=True)
            print(OK + f"已把流式首块播到 CABLE index={cable_index} → 输出管路通")
        except Exception as e:
            print(BAD + f"播到 CABLE 失败: {e}")


def check_live_chain():
    """直播链路：lipsync/vcam 健康 → 人脸预热 → 流式口型首段 TTFV。"""
    hr("4) 直播链路体检")
    ok = True
    for label, url in (("Lipsync", f"{LS}/health"), ("VCam", f"{VC}/health")):
        try:
            requests.get(url, timeout=5).raise_for_status()
            print(OK + f"{label} 在线")
        except Exception as e:
            print(BAD + f"{label} 不可达: {e}"); ok = False
    if not ok:
        return False

    name = requests.get(f"{HUB}/profiles", timeout=5).json().get("active", "")
    pj = requests.get(f"{HUB}/profiles/{quote(name, safe='')}", params={"include_face": "true"}, timeout=10).json()
    face_b64 = pj.get("face_b64", "")
    idle = (pj.get("idle_video") or "").strip()
    print((OK if face_b64 else BAD) + f"角色 {name!r} 人脸={'有' if face_b64 else '无'}")
    print((OK if idle else "  [--] ") + f"真人待机视频={'有 '+idle if idle else '无(回退伪活体循环)'}")
    if not face_b64:
        return False

    face_id = f"selfcheck_{name}"
    face_bytes = L._b64bytes(face_b64)
    t0 = time.time()
    r = requests.post(f"{LS}/lipsync/precompute_face",
                      files={"face": ("f.jpg", face_bytes, "image/jpeg")},
                      data={"face_id": face_id}, timeout=120)
    pre_ms = (time.time() - t0) * 1000
    print((OK if r.ok else BAD) + f"人脸预热 face_id={face_id} ({pre_ms:.0f}ms)")

    L.ST.voice_b64, L.ST.ref_text = L._fetch_voice_ref(name)
    L._prewarm_ref(L.ST.voice_b64, L.ST.ref_text)
    en = "Hello, this is a live stream probe."
    payload = {"text": en, "language": "en", "return_base64": True, "seed": 42,
               "reference_audio_b64": L.ST.voice_b64, "reference_text": L.ST.ref_text}
    wav = base64.b64decode(requests.post(f"{FISH}/v1/tts/clone", json=payload, timeout=60).json()["audio_base64"])

    requests.post(f"{VC}/clear", timeout=3)
    time.sleep(0.2)
    t1 = time.time()
    r = requests.post(f"{LS}/lipsync/generate_stream",
                      files={"audio": ("a.wav", wav, "audio/wav")},
                      data={"face_id": face_id, "fps": "25",
                            "first_seg_frames": str(L.FIRST_SEG_FRAMES),
                            "seg_frames": "25", "vcam_url": VC}, timeout=180)
    lip_ms = (time.time() - t1) * 1000
    if not r.ok:
        print(BAD + f"口型流式生成失败 {r.status_code}"); return False
    j = r.json()
    ttfv = j.get("ttfv_ms")
    seg_gap = j.get("seg_gap_ms")
    seg_n = j.get("seg_count", 0)
    print(OK + f"口型流式 {lip_ms:.0f}ms · {seg_n} 段")
    print(f"  首段入队(TTFV)  {ttfv if ttfv is not None else 'NA'}ms  ← 直播首帧关键指标")
    print(f"  段间推送间隔    {seg_gap if seg_gap is not None else 'NA'}ms  (理想 ≤1000ms@25fps)")
    if seg_gap and seg_gap > 1200:
        print(BAD + "段间隔偏大，口型可能追不上实时语速")
    else:
        print(OK + "段间隔正常")
    try:
        st = requests.get(f"{VC}/status", timeout=3).json()
        print(OK + f"vcam playing={st.get('playing')} queued={st.get('queued')}")
    except Exception as e:
        print(BAD + f"vcam status 失败: {e}")
    requests.post(f"{VC}/clear", timeout=3)
    return True


_SOAK_LINES = [
    "Hello, thanks for joining the live stream today.",
    "Let me walk you through the main features step by step.",
    "This part is really important, so please pay attention.",
    "If you have any questions, drop them in the chat.",
    "We support real time translation with cloned voice.",
    "The digital human keeps lip sync aligned with the audio.",
    "Now let's move on to the next topic.",
    "Thanks everyone, that's all for this section.",
    "Today I want to explain in detail how our real time translation pipeline keeps the cloned voice and the lip motion perfectly aligned even under heavy load.",
    "If you look closely at the screen, you will notice that the mouth movement follows every single word of the translated speech without any visible delay at all.",
    "Before we wrap up this segment, let me quickly summarize the three key advantages, namely low latency, natural voice cloning, and rock solid lip synchronization.",
]


def _gpu_snapshot():
    """抓取 GPU 占用快照(显存/利用率/计算进程数),用于解释压测延迟是否受争用影响。"""
    try:
        import subprocess
        q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8)
        mem_used, mem_total, util = [x.strip() for x in q.stdout.strip().splitlines()[0].split(",")]
        a = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=8)
        napps = len([l for l in a.stdout.strip().splitlines() if l.strip()])
        return {"mem_used_mib": int(mem_used), "mem_total_mib": int(mem_total),
                "util_pct": int(util), "compute_apps": napps}
    except Exception as e:
        return {"error": str(e)}


def _pct(xs, q):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def check_soak(n: int, inject_fail: bool = False):
    """连续压测:对真链路(Fish→lipsync→vcam)连发 N 句,统计 TTFV/段间隔/整句耗时分布,
    并据段间隔判定实时达标占比 → 评估持续直播的稳定性。写 JSON 报告。
    inject_fail=True:在中段把 lipsync 指向坏端口 2 句,验证失败被捕获且恢复后回到实时。"""
    hr(f"6) 连续压测 soak · {n} 句(真链路 Fish→lipsync→vcam)" + ("  [含故障注入]" if inject_fail else ""))
    for label, url in (("Lipsync", f"{LS}/health"), ("VCam", f"{VC}/health"), ("Fish", f"{FISH}/health")):
        try:
            requests.get(url, timeout=5).raise_for_status(); print(OK + f"{label} 在线")
        except Exception as e:
            print(BAD + f"{label} 不可达,压测中止: {e}"); return None
    name = requests.get(f"{HUB}/profiles", timeout=5).json().get("active", "")
    pj = requests.get(f"{HUB}/profiles/{quote(name, safe='')}", params={"include_face": "true"}, timeout=10).json()
    face_b64 = pj.get("face_b64", "")
    if not face_b64:
        print(BAD + f"活动角色 {name!r} 无人脸,压测中止"); return None
    L.ST.voice_b64, L.ST.ref_text = L._fetch_voice_ref(name)
    L.ST.live_mode = True
    L.ST.face_id = f"soak_{name}"
    if not L._precompute_face_call(L._b64bytes(face_b64), L.ST.face_id):
        print(BAD + "人脸预计算失败,压测中止"); return None
    L._prewarm_ref(L.ST.voice_b64, L.ST.ref_text)
    L._warmup_lipsync(L.ST.face_id)                 # 先热启动,避免首句编译尖峰污染分布
    L._push_subtitle = lambda *a, **k: None         # 压测不依赖 vcam 字幕

    gpu0 = _gpu_snapshot()                           # 压测前 GPU 占用快照(解释延迟是否受争用影响)
    print(OK + f"GPU 起始: 显存 {gpu0.get('mem_used_mib')}/{gpu0.get('mem_total_mib')}MiB · "
               f"利用率 {gpu0.get('util_pct')}% · 计算进程 {gpu0.get('compute_apps')}")
    samples = []
    _real_drive = L._drive_avatar
    fail_lo, fail_hi = (n // 3, n // 3 + 2) if inject_fail else (-1, -1)   # 注入窗口 [lo,hi)
    t_all = time.time()
    for i in range(n):
        injected = inject_fail and fail_lo <= i < fail_hi
        if inject_fail:
            if i == fail_lo:
                def _boom(*a, **k):                      # 模拟 lipsync 不可达(确定性,免网络超时)
                    raise ConnectionError("injected lipsync failure")
                L._drive_avatar = _boom
                print(BAD + f"[注入] 段 {fail_lo+1}~{fail_hi} 故障:lipsync 驱动抛连接异常")
            elif i == fail_hi:
                L._drive_avatar = _real_drive
                print(OK + "[恢复] lipsync 驱动已还原")
        en = _SOAK_LINES[i % len(_SOAK_LINES)]
        try:
            av = L._drive_avatar(en) or {}
        except Exception:
            av = {"ok": False}                           # 驱动异常 → 计为失败(生产中由 _process_a 兜底降级)
        ok = av.get("ok")
        av_off = av_drift = preroll = None
        if ok:                                           # 读 vcam 实测音画偏移/全程漂移/自适应 pre-roll
            try:
                _st = requests.get(f"{VC}/status", timeout=2).json()
                av_off = _st.get("av_offset_ms"); av_drift = _st.get("av_drift_ms")
                preroll = _st.get("preroll_ms")
            except Exception:
                pass
        samples.append({"i": i, "ok": bool(ok), "injected": injected, "ttfv": av.get("ttfv_ms"),
                        "seg_gap": av.get("seg_gap_ms"), "avatar_ms": av.get("avatar_ms"),
                        "synth_ms": av.get("synth_ms"), "av_offset_ms": av_off, "av_drift_ms": av_drift,
                        "preroll_ms": preroll, "chunks": av.get("chunks")})
        gap = av.get("seg_gap_ms")
        if injected:
            flag = OK if not ok else BAD                 # 注入期:预期失败才算对
            print(flag + f"[{i+1}/{n}] 注入期 ok={ok}(预期 False)")
        else:
            flag = OK if (ok and (gap is None or gap <= 1250)) else BAD
            print(flag + f"[{i+1}/{n}] ttfv={av.get('ttfv_ms')}ms gap={gap}ms 整句={av.get('avatar_ms')}ms")
    L._drive_avatar = _real_drive
    requests.post(f"{VC}/clear", timeout=3)

    normal = [s for s in samples if not s["injected"]]   # 注入期为预期失败,排除出分布
    oks = [s for s in normal if s["ok"]]
    rt = [s for s in oks if (s["seg_gap"] is None or s["seg_gap"] <= 1250)]
    dist = {}
    for k in ("ttfv", "seg_gap", "avatar_ms", "synth_ms", "av_offset_ms", "av_drift_ms"):
        vals = [s.get(k) for s in oks]
        dist[k] = {"median": _pct(vals, 0.5), "p90": _pct(vals, 0.9),
                   "max": max([v for v in vals if v is not None], default=None)}
    rtt_ratio = round(len(rt) / len(oks), 3) if oks else 0.0
    gpu1 = _gpu_snapshot()
    rep = {"profile": name, "n": n, "normal": len(normal), "ok": len(oks), "fail": len(normal) - len(oks),
           "realtime_ratio": rtt_ratio, "total_s": round(time.time() - t_all, 1), "dist": dist,
           "gpu_before": gpu0, "gpu_after": gpu1, "samples": samples, "inject_fail": inject_fail}
    inj_ok = None
    if inject_fail:
        injected = [s for s in samples if s["injected"]]
        after = [s for s in normal if s["i"] >= fail_hi]
        inj_failed = all(not s["ok"] for s in injected) and len(injected) > 0
        recovered = bool(after) and all(s["ok"] for s in after)
        inj_ok = inj_failed and recovered
        rep["inject"] = {"window": [fail_lo, fail_hi], "all_failed_in_window": inj_failed,
                         "recovered_after": recovered, "ok": inj_ok}
    hr("压测分布")
    for k in ("ttfv", "seg_gap", "avatar_ms", "synth_ms", "av_offset_ms", "av_drift_ms"):
        d = dist[k]; print(f"  {k:12s} 中位 {d['median']}  p90 {d['p90']}  max {d['max']}")
    print(f"  正常句成功 {len(oks)}/{len(normal)} · 实时达标率 {rtt_ratio*100:.0f}% · 总耗时 {rep['total_s']}s")
    healthy = rtt_ratio >= 0.9 and len(oks) == len(normal)
    print((OK if healthy else BAD) + ("持续直播稳定" if healthy else "存在偏慢/失败句,见上"))
    if inject_fail:
        print((OK if inj_ok else BAD) + f"故障注入闭环:注入期全失败={rep['inject']['all_failed_in_window']} "
              f"恢复后全成功={rep['inject']['recovered_after']}")
    if not healthy and (gpu1.get("compute_apps", 0) > 4 or gpu1.get("util_pct", 0) > 60):
        print(BAD + f"GPU 争用嫌疑:计算进程 {gpu1.get('compute_apps')} 个/利用率 {gpu1.get('util_pct')}%；"
                    f"隔离独占显卡后重测(实测独占仅 ~0.4x 段间隔)")
    return rep


def check_rehearsal():
    """直播自愈/切换 场景演练(离线逻辑回归):不依赖 GPU/设备,仅 stub 网络出口,
    确定性验证 降级→回待机→恢复 / 角色边界切换 / 预案池命中 / 多路输出帧源一致。
    返回 (scenarios:list[(name,ok)], 是否全过)。"""
    hr("5) 直播自愈/切换 场景演练(离线逻辑回归)")
    ST = L.ST
    calls = {"notice": [], "return_idle": 0, "build": []}
    L._set_vcam_notice = lambda t: calls["notice"].append(t)               # stub 网络出口
    def _ri(): calls["return_idle"] += 1
    L._vcam_return_idle = _ri
    # 重置会话态(本进程独立 ST 实例)
    ST.live_mode = True; ST.running = True
    ST.switch_count = 0; ST.degrade_count = 0; ST.degrade_ms = 0
    ST._degrade_since = 0.0; ST.live_degraded = False; ST._avatar_fail = 0
    ST.preset_cache = {}; ST.pending_switch = None; ST._post_switch_probe = False
    ST.profile = "角色A"; ST.voice_b64 = "A"; ST.ref_text = "ra"; ST.face_id = "interp_角色A"
    res = []

    # S1 连续失败→降级入场(置角标 + 立即回待机帧)
    changed = ST.set_degraded(True)
    L._enter_degrade("● 配音模式（口型恢复中）", "⚠ 演练降级")
    s1 = bool(changed and ST.live_degraded and ST.degrade_count == 1
              and calls["return_idle"] >= 1 and any("配音模式" in n for n in calls["notice"]))
    res.append(("S1 失败→降级·回待机·角标", s1))

    # S2 服务恢复→清角标 + 降级时长入账(非重复计数)
    time.sleep(0.2)
    ST._avatar_fail = 0
    rec = ST.set_degraded(False)
    if rec:
        L._exit_degrade()
    s2 = bool(rec and (not ST.live_degraded) and ST.degrade_ms > 0
              and calls["notice"] and calls["notice"][-1] == "")
    res.append(("S2 恢复→清角标·时长入账", s2))

    # S3 角色切换在句子边界原子生效(换脸→置 A/V 回归探针)
    ST.pending_switch = {"profile": "角色B", "voice_b64": "B64", "ref_text": "rb",
                         "face_id": "interp_角色B", "idle_video": "", "face_bytes": b""}
    L._apply_pending_switch()
    s3 = bool(ST.profile == "角色B" and ST.voice_b64 == "B64" and ST.switch_count == 1
              and ST.pending_switch is None and ST._post_switch_probe)
    res.append(("S3 角色边界原子切换+探针", s3))

    # S4 预案池命中(指纹一致)→免重建;指纹不一致→失效重建。stub 签名与 _build_switch。
    L._profile_sig = lambda nm: "SIG-C" if nm == "角色C" else "SIG-NEW"
    L._build_switch = lambda nm, warn=True: (calls["build"].append(nm),
                                             {"profile": nm, "voice_b64": "x", "ref_text": "x",
                                              "face_id": f"interp_{nm}", "idle_video": "", "face_bytes": b"",
                                              "sig": "SIG-NEW", "fp": ""})[1]
    ST.preset_cache = {"角色C": {"profile": "角色C", "voice_b64": "C64", "ref_text": "rc",
                                 "face_id": "interp_角色C", "idle_video": "", "face_bytes": b"",
                                 "sig": "SIG-C", "fp": "fpc"}}
    ST.switching = True
    L._prepare_switch("角色C")                 # 指纹一致 → 命中,不应调用 _build_switch
    hit = bool(ST.pending_switch and ST.pending_switch["profile"] == "角色C" and len(calls["build"]) == 0)
    # 内容已变(签名不符)→ 应失效重建
    ST.pending_switch = None
    ST.preset_cache["角色C"]["sig"] = "SIG-OLD"   # 模拟 Hub 内容变更
    ST.switching = True
    L._prepare_switch("角色C")                 # 签名不符 → 失效重建,调用 _build_switch 一次
    invalidate = bool(len(calls["build"]) == 1 and "角色C" not in ST.preset_cache)
    s4 = hit and invalidate
    res.append(("S4 预案命中免重建·失效重建", s4))

    # S5 多路输出一致:字幕/角标先叠加,再 fanout(RTMP/录制)+ cam.send(OBS)+ WebRTC 共享同帧
    import inspect, vcam_server as V
    src = inspect.getsource(V._camera_loop)
    i_sub, i_fan, i_send = src.find("_apply_subtitle"), src.find("_fanout_push"), src.find("cam.send")
    s5 = bool(i_sub > 0 and i_sub < i_fan and i_sub < i_send)
    res.append(("S5 叠加先于多路推送(OBS/RTMP/WebRTC一致)", s5))

    ST.running = False
    for name, ok in res:
        print((OK if ok else BAD) + name)
    return res, all(ok for _, ok in res)


def check_stt_realtime():
    """6) STT 实时闭环 就绪/接线核验（无需 GPU）：
       - Hub 是否注册了 nemotron_stt 引擎（3-C 接线证明，Hub 在线即可验）
       - nemo_stt 流式服务 /health 是否就绪（未就绪不判失败，仅标记「待就绪」，因其为可选灰度引擎）
       返回 (info:dict, hard_fail:bool)。hard_fail 仅在「Hub 在线但未注册 nemotron_stt」时为真（真实接线回归）。"""
    import os as _os
    hr("6) STT 实时闭环 就绪核验")
    nemo = _os.environ.get("NEMO_STT_BASE_URL", "http://127.0.0.1:7857")
    info = {"hub_up": False, "nemotron_registered": None,
            "nemo_health": False, "nemo_loaded": None}
    hard_fail = False
    try:
        d = requests.get(f"{HUB}/api/converse/backends", timeout=5).json()
        info["hub_up"] = True
        names = [b.get("name") for b in d.get("stt", [])]
        reg = "nemotron_stt" in names
        info["nemotron_registered"] = reg
        print((OK if reg else BAD) + f"Hub STT 引擎={names}（nemotron_stt {'已注册' if reg else '缺失'}）")
        if not reg:
            hard_fail = True       # Hub 在线却没注册 → 3-C 接线回归
    except Exception as e:
        print(BAD + f"Hub /api/converse/backends 不可达（Hub 未启动?）: {e}")
    try:
        h = requests.get(f"{nemo}/health", timeout=5).json()
        info["nemo_health"] = True
        info["nemo_loaded"] = bool(h.get("loaded"))
        print((OK if h.get("loaded") else BAD) +
              f"nemo_stt {nemo} loaded={h.get('loaded')} model={h.get('model')}")
    except Exception as e:
        print("  [..] " + f"nemo_stt 未就绪（{nemo}）——可选灰度引擎，就绪后可跑闭环压测: {e}")
    print("  就绪后一键闭环压测：python interp_selfcheck.py --stt-bench 8")
    return info, hard_fail


def _fmt(x):
    return "n/a" if x is None else str(x)

def _fmtpct(x):
    return "n/a" if x is None else f"{round(x*100)}%"

def _eval_sla(pt, sla):
    """对单档并发结果判 SLA。sla={first_p95, final_p95, ok_rate}。
    返回 (ok:bool, why:list[str])。延迟为 n/a（如合成音无文本）则该项「跳过」不判失败，仅在 why 标注。"""
    why = []
    if not pt.get("ok"):
        return False, ["流未全部完成"]
    okr = pt.get("ok_rate")
    if okr is not None and okr < sla["ok_rate"]:
        why.append(f"成功率 {round(okr*100)}%<{round(sla['ok_rate']*100)}%")
    fp95 = pt.get("first_partial_p95")
    if fp95 is None:
        why.append("首partial n/a(跳过,需 --wav)")
    elif fp95 > sla["first_p95"]:
        why.append(f"首partial p95 {fp95}>{sla['first_p95']}ms")
    fn95 = pt.get("final_p95")
    if fn95 is None:
        why.append("final n/a(跳过)")
    elif fn95 > sla["final_p95"]:
        why.append(f"final p95 {fn95}>{sla['final_p95']}ms")
    hard = [w for w in why if "跳过" not in w and "n/a" not in w]
    return (len(hard) == 0), why

def _latest_json(pattern):
    import glob, os as _os, json as _json
    files = sorted(glob.glob(_os.path.join("logs", pattern)))
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def run_stt_bench(ladder=(1, 4, 8, 16), utter_sec=4.0, with_soak=0, sla=None):
    """STT 实时闭环一键压测（需 Hub/nemo_stt 就绪、GPU）：barge-in 验收 + 多路并发「阶梯」压测
    （逐档 1/4/8/16…）+ 可选并入对话并发 soak，聚合成并发/延迟曲线，写 logs/stt_closedloop_report_*.json。
    子进程隔离运行（避免脚本顶层副作用 / 环境耦合）。
    sla=None 时仅判「连通/完成」；给定 {first_p95,final_p95,ok_rate,target_c} 时按 SLA 阈值判达标。"""
    import subprocess, os as _os, json as _json, datetime
    ladder = [int(c) for c in ladder if int(c) > 0] or [8]
    hr(f"STT 实时闭环压测（barge-in 验收 + 并发阶梯 {','.join(map(str, ladder))}"
       + (f" + conv_soak {with_soak}" if with_soak else "")
       + (f" · SLA 首p95≤{sla['first_p95']}/final p95≤{sla['final_p95']}/成功率≥{round(sla['ok_rate']*100)}%@c={sla.get('target_c') or max(ladder)}" if sla else "")
       + "）")
    agg = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "ladder": ladder, "utter_sec": utter_sec, "curve": []}

    # 1) barge-in 闭环验收（仅 urllib 打 Hub，任意 env 可跑）
    try:
        r = subprocess.run([sys.executable, "_bargein_verify.py"],
                           timeout=300, capture_output=True, text=True)
        out = r.stdout or ""
        bok = (r.returncode == 0) and ("PASS" in out)
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        print((OK if bok else BAD) + f"barge-in 验收：{'PASS' if bok else 'CHECK'}  | {tail}")
        agg["bargein"] = {"ok": bok, "rc": r.returncode}
    except Exception as e:
        print(BAD + f"barge-in 验收异常: {e}"); agg["bargein"] = {"ok": False, "err": str(e)}

    # 2) 并发阶梯压测：逐档跑 _stt_concurrency，抽取每档曲线点（需 websockets+numpy，本 env 已具备）
    all_rungs_ok = True
    for c in ladder:
        try:
            r = subprocess.run([sys.executable, "_stt_concurrency.py", "-c", str(c),
                                "--utter-sec", str(utter_sec)],
                               timeout=600, capture_output=True, text=True)
            cok = (r.returncode == 0)
            rep = _latest_json("stt_concurrency_report_*.json") if cok else None
            fp = (rep or {}).get("first_partial_ms", {})
            fn = (rep or {}).get("final_ms", {})
            gpu = (rep or {}).get("gpu_free_mib", {})
            pt = {"c": c, "ok": cok, "ok_rate": (rep or {}).get("ok_rate"),
                  "first_partial_p50": fp.get("p50"), "first_partial_p95": fp.get("p95"),
                  "final_p50": fn.get("p50"), "final_p95": fn.get("p95"),
                  "gpu_free_min": gpu.get("min"), "gpu_drift": gpu.get("drift")}
            if sla:
                pt["sla_ok"], pt["sla_why"] = _eval_sla(pt, sla)
            agg["curve"].append(pt)
            _sla_tag = (f" · SLA {'达标' if pt['sla_ok'] else '未达标'}"
                        + (f"({';'.join(pt['sla_why'])})" if pt.get("sla_why") else "")) if sla else ""
            print((OK if cok else BAD)
                  + f"并发 c={c:<3}：{'PASS' if cok else 'CHECK'} "
                  + f"成功率={_fmtpct(pt['ok_rate'])} "
                  + f"首partial p50/p95={_fmt(fp.get('p50'))}/{_fmt(fp.get('p95'))}ms "
                  + f"final p50/p95={_fmt(fn.get('p50'))}/{_fmt(fn.get('p95'))}ms" + _sla_tag)
            all_rungs_ok = all_rungs_ok and cok
        except Exception as e:
            print(BAD + f"并发 c={c} 异常: {e}")
            agg["curve"].append({"c": c, "ok": False, "err": str(e)})
            all_rungs_ok = False
    agg["concurrency_ok"] = all_rungs_ok

    # 3) 可选：对话并发 soak（Hub /api/converse 整链路），并入闭环报告
    if with_soak:
        try:
            r = subprocess.run([sys.executable, "conv_soak.py", str(with_soak)],
                               timeout=1200, capture_output=True, text=True)
            sok = (r.returncode == 0)
            print((OK if sok else BAD) + f"对话并发 soak {with_soak}：{'PASS' if sok else 'CHECK'}")
            agg["conv_soak"] = {"ok": sok, "rc": r.returncode,
                                "report": _latest_json("soak_report_*.json")}
        except Exception as e:
            print(BAD + f"conv_soak 异常: {e}"); agg["conv_soak"] = {"ok": False, "err": str(e)}

    # 曲线小结表
    if any(p.get("ok") for p in agg["curve"]):
        hr("并发/延迟曲线")
        print("  并发  成功率  首partial(p50/p95)  final(p50/p95)  GPU空闲min(MiB)")
        for p in agg["curve"]:
            print(f"  {p['c']:<4}  {_fmtpct(p.get('ok_rate')):<6}  "
                  f"{_fmt(p.get('first_partial_p50'))}/{_fmt(p.get('first_partial_p95'))}".ljust(34)
                  + f"{_fmt(p.get('final_p50'))}/{_fmt(p.get('final_p95'))}".ljust(16)
                  + f"{_fmt(p.get('gpu_free_min'))}")

    soak_ok = agg.get("conv_soak", {}).get("ok", True) if with_soak else True

    # SLA 达标判定：取目标并发档（默认阶梯最大档）所在档位是否 SLA 达标
    sla_ok = True
    if sla:
        target_c = sla.get("target_c") or max(ladder)
        cand = [p for p in agg["curve"] if p["c"] <= target_c and p.get("ok")]
        tgt = max(cand, key=lambda p: p["c"]) if cand else None
        if tgt is None:
            sla_ok = False; sla_summary = f"无可达标档(≤c={target_c} 均未完成)"
        else:
            sla_ok = bool(tgt.get("sla_ok"))
            sla_summary = (f"c={tgt['c']} {'达标' if sla_ok else '未达标'}"
                           + (f"：{';'.join(tgt.get('sla_why') or [])}" if tgt.get("sla_why") else ""))
        agg["sla"] = {"thresholds": sla, "target_c": target_c,
                      "evaluated_c": (tgt or {}).get("c"), "ok": sla_ok, "summary": sla_summary}
        print((OK if sla_ok else BAD) + f"SLA 判定：{sla_summary}")

    verdict = "PASS" if (agg.get("bargein", {}).get("ok") and all_rungs_ok and soak_ok and sla_ok) else "CHECK"
    agg["verdict"] = verdict
    _os.makedirs("logs", exist_ok=True)
    path = _os.path.join("logs", f"stt_closedloop_report_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(agg, f, ensure_ascii=False, indent=2)
    hr("STT 闭环压测结论")
    print("  STT CLOSED-LOOP PASS ✓" if verdict == "PASS" else "  STT CLOSED-LOOP CHECK ⚠ 见上方未过项")
    print(f"  报告已写入 {path}")
    return verdict == "PASS"


def backfill_sla(report="", factor=1.3, apply=False):
    """12-C:从 STT 实时闭环报告(logs/stt_closedloop_report_*.json)的实测曲线反推 SLA 达标线。
    取所有「完成」档位的 first/final p95 最大值 ×factor 作建议阈值,目标并发取最高完整通过档。
    apply=False 仅打印建议(干跑);apply=True 写入 config.json[stt_sla]。返回 0/非0。"""
    import json as _json, glob, math, os as _os
    path = report
    if not path:
        cands = sorted(glob.glob(_os.path.join("logs", "stt_closedloop_report_*.json")))
        if not cands:
            print(BAD + "未找到 logs/stt_closedloop_report_*.json(先跑 --stt-bench 生成曲线)"); return 2
        path = cands[-1]
    try:
        agg = _json.loads(open(path, encoding="utf-8").read())
    except Exception as e:
        print(BAD + f"读取报告失败 {path}: {e}"); return 2
    curve = agg.get("curve") or []
    okr = [p for p in curve if p.get("ok")]
    if not okr:
        print(BAD + f"报告无「完成」档位,无法反推(见 {path})"); return 2

    def _mx(key):
        vals = [p[key] for p in okr if p.get(key) is not None]
        return max(vals) if vals else None
    f95, n95 = _mx("first_partial_p95"), _mx("final_p95")
    rate_min = min([p["ok_rate"] for p in okr if p.get("ok_rate") is not None], default=None)
    target_c = max(p["c"] for p in okr)

    import app_config
    cur = app_config.stt_sla()
    rec = dict(cur)
    if f95 is not None: rec["first_p95"] = int(math.ceil(f95 * factor))
    if n95 is not None: rec["final_p95"] = int(math.ceil(n95 * factor))
    rec["target_c"] = target_c
    # 成功率建议:不高于实测最低,且不低于 0.9（留余量但别把自己卡死）
    if rate_min is not None:
        rec["ok_rate"] = round(max(0.90, min(cur.get("ok_rate", 0.95), rate_min)), 4)

    hr("SLA 阈值回填建议（实测曲线 ×%.2f）" % factor)
    print(f"  源报告：{path}")
    print(f"  完整通过档：c={target_c}（实测 first p95={_fmt(f95)}ms / final p95={_fmt(n95)}ms / 最低成功率={_fmtpct(rate_min)}）")
    print(f"  当前阈值：首{cur['first_p95']} / final{cur['final_p95']} / 成功率≥{cur['ok_rate']} / 并发{cur['target_c']}")
    print(f"  建议阈值：首{rec['first_p95']} / final{rec['final_p95']} / 成功率≥{rec['ok_rate']} / 并发{rec['target_c']}")
    if apply:
        app_config.update_config({"stt_sla": rec})
        print(OK + "已写入 config.json[stt_sla]（环境变量 AVATARHUB_SLA_* 仍优先）")
    else:
        print("  （干跑：加 --sla-backfill-apply 写入 config.json）")
    return 0


def _parse_ladder(s):
    """'8' / '1,4,8,16'（含全角逗号）→ [int,...]；非法返回 None。"""
    try:
        return [int(x) for x in str(s).replace("，", ",").split(",") if x.strip()] or None
    except ValueError:
        return None


def _build_sla_from_args(args):
    """按 CLI > 环境变量 > config.json > 默认 组装 SLA 阈值；--no-sla 返回 None。"""
    if getattr(args, "no_sla", False):
        return None
    import app_config
    sla = app_config.stt_sla()
    if args.sla_first_p95 is not None: sla["first_p95"] = args.sla_first_p95
    if args.sla_final_p95 is not None: sla["final_p95"] = args.sla_final_p95
    if args.sla_ok_rate is not None: sla["ok_rate"] = args.sla_ok_rate
    if args.sla_concurrency is not None: sla["target_c"] = args.sla_concurrency
    return sla


def main():
    ap = argparse.ArgumentParser(description="实时同传真机联调自检")
    ap.add_argument("--live", action="store_true", help="含直播链路(lipsync/vcam/TTFV)探测")
    ap.add_argument("--rehearsal", action="store_true",
                    help="真机彩排:服务+直播链路探测 + 自愈/切换场景演练 + 写 JSON 报告")
    ap.add_argument("--soak", type=int, metavar="N", default=0,
                    help="连续压测 N 句(真链路),统计延迟分布+实时达标率,写 JSON 报告")
    ap.add_argument("--inject-fail", action="store_true",
                    help="配合 --soak:中段注入 lipsync 故障 2 句,验证失败被捕获且恢复后回到实时")
    ap.add_argument("--stt-bench", metavar="LADDER", default="",
                    help="STT 实时闭环一键压测:barge-in 验收 + 并发阶梯压测(如 8 或 1,4,8,16),聚合写曲线报告(需服务/GPU 就绪)")
    ap.add_argument("--with-soak", type=int, metavar="N", default=0,
                    help="配合 --stt-bench:并入对话并发 soak N 句(Hub /api/converse 整链路)")
    ap.add_argument("--bench-utter", type=float, metavar="SEC", default=4.0,
                    help="配合 --stt-bench:每路合成音时长秒数(默认 4.0)")
    ap.add_argument("--no-sla", action="store_true",
                    help="配合 --stt-bench:关闭 SLA 阈值判定,只看连通/完成")
    ap.add_argument("--sla-first-p95", type=int, metavar="MS", default=None,
                    help="SLA:首 partial p95 上限(ms;默认读 config.json[stt_sla],缺省 700)")
    ap.add_argument("--sla-final-p95", type=int, metavar="MS", default=None,
                    help="SLA:eou→final p95 上限(ms;默认读配置,缺省 1500)")
    ap.add_argument("--sla-ok-rate", type=float, metavar="R", default=None,
                    help="SLA:每档最低成功率(0~1;默认读配置,缺省 0.95)")
    ap.add_argument("--sla-concurrency", type=int, metavar="N", default=None,
                    help="SLA:需达标的目标并发档(默认读配置,缺省=阶梯最大档)")
    ap.add_argument("--ci", action="store_true",
                    help="门禁模式:总结论非 PASS 即以非零码退出(可挂 CI/验收)。配合 --rehearsal/--stt-bench")
    ap.add_argument("--sla-backfill", nargs="?", const="", default=None, metavar="REPORT",
                    help="从闭环报告反推 SLA 达标线(p95×系数);可给报告路径,默认取 logs/ 最新。干跑打印建议")
    ap.add_argument("--sla-backfill-apply", action="store_true",
                    help="配合 --sla-backfill:把建议阈值写入 config.json[stt_sla]")
    ap.add_argument("--sla-factor", type=float, metavar="K", default=1.3,
                    help="配合 --sla-backfill:实测 p95 的放大系数(默认 1.3)")
    args = ap.parse_args()

    if args.sla_backfill is not None:
        sys.exit(backfill_sla(args.sla_backfill, factor=args.sla_factor, apply=args.sla_backfill_apply))

    # --stt-bench 单独运行；若同时指定 --rehearsal,则由彩排统一编排并纳入总判定。
    if args.stt_bench and not args.rehearsal:
        ladder = _parse_ladder(args.stt_bench)
        if ladder is None:
            print(BAD + f"--stt-bench 取值无效: {args.stt_bench!r}（用如 8 或 1,4,8,16）"); sys.exit(2)
        sla = _build_sla_from_args(args)
        ok = run_stt_bench(ladder=ladder, utter_sec=args.bench_utter,
                           with_soak=args.with_soak, sla=sla)
        sys.exit(0 if ok else 1)

    if args.soak:
        import json as _json, datetime, os
        rep = check_soak(args.soak, inject_fail=args.inject_fail)
        if rep is not None:
            os.makedirs("logs", exist_ok=True)
            path = os.path.join("logs", f"soak_report_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
            rep["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(rep, f, ensure_ascii=False, indent=2)
            print(f"  报告已写入 {path}")
        return

    if args.rehearsal:
        import json as _json, datetime, os
        report = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
        svc = check_services()
        defs = check_devices()
        live_ok = None
        if svc:
            try:
                live_ok = check_live_chain()
            except Exception as e:
                print(BAD + f"直播链路探测异常: {e}"); live_ok = False
        scenarios, drill_ok = check_rehearsal()
        stt_info, stt_fail = check_stt_realtime()
        report.update({"services_ok": bool(svc), "live_chain_ok": live_ok,
                       "scenarios": {n: bool(o) for n, o in scenarios}, "drill_ok": drill_ok,
                       "stt_realtime": stt_info})
        # 可选:同时跑 STT 实时闭环压测(barge-in + 并发阶梯 + SLA),并入彩排总判定。
        bench_ok = True
        if args.stt_bench:
            ladder = _parse_ladder(args.stt_bench)
            if ladder is None:
                print(BAD + f"--stt-bench 取值无效: {args.stt_bench!r}（用如 8 或 1,4,8,16）"); sys.exit(2)
            bench_ok = run_stt_bench(ladder=ladder, utter_sec=args.bench_utter,
                                     with_soak=args.with_soak, sla=_build_sla_from_args(args))
            report["stt_closedloop_ok"] = bool(bench_ok)
        verdict = "PASS" if (svc and live_ok is not False and drill_ok
                             and not stt_fail and bench_ok) else "CHECK"
        report["verdict"] = verdict
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", f"rehearsal_report_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(report, f, ensure_ascii=False, indent=2)
        hr("彩排结论")
        _stt_reg = stt_info.get("nemotron_registered")
        _stt_txt = ("接线OK" if _stt_reg else ("接线缺失!" if _stt_reg is False else "Hub未起"))
        _stt_rdy = "已就绪" if stt_info.get("nemo_loaded") else "待就绪"
        _bench_txt = (f" · STT压测={'达标' if bench_ok else '未达标'}") if args.stt_bench else ""
        print(f"  服务={'OK' if svc else 'FAIL'} · 直播链路={live_ok} · 场景演练={'全过' if drill_ok else '有未过'}"
              f" · STT闭环={_stt_txt}/{_stt_rdy}{_bench_txt}")
        print(("  REHEARSAL PASS ✓" if verdict == "PASS" else "  REHEARSAL CHECK ⚠ 见上方未过项"))
        print(f"  报告已写入 {path}")
        if args.ci and verdict != "PASS":
            print(BAD + "门禁(--ci):总结论非 PASS,以退出码 1 失败")
            sys.exit(1)
        return

    svc = check_services()
    defs = check_devices()
    if svc:
        check_latency(defs.get("cable"))
    if args.live and svc:
        check_live_chain()
    hr("通话App / 直播 设置清单")
    print("""  ① 安装 VB-CABLE(已装可跳过)。
  ② 通话模式：通话App 麦克风 → CABLE Output；扬声器 → 真实耳机。
  ③ 直播模式：启动器点「直播同传」→ OBS 摄像头选 OBS Virtual Camera。
  ④ Windows 录制 → 启用「立体声混音」(抓对方声做字幕)。
  ⑤ 打开 http://127.0.0.1:7900/ 或 Hub「同传」Tab → 选角色 → 开始。
  ⑥ 直播体检：python interp_selfcheck.py --live
  ⑦ 真机彩排(含自愈/切换场景演练+报告)：python interp_selfcheck.py --rehearsal
  ⑧ 连续压测(N 句延迟分布+实时达标率+报告)：python interp_selfcheck.py --soak 20
  ⑨ STT 实时闭环压测(barge-in+并发阶梯+曲线+SLA 判定)：
     python interp_selfcheck.py --stt-bench 1,4,8,16 [--with-soak 20] [--sla-final-p95 1500 --sla-concurrency 8 | --no-sla]
  ⑩ 验收门禁(未达标即非零退出,可挂 CI)：python interp_selfcheck.py --rehearsal --stt-bench 1,4,8 --ci
  ⑪ 实测回填 SLA 达标线(p95×系数,写 config.json)：python interp_selfcheck.py --sla-backfill --sla-backfill-apply""")


if __name__ == "__main__":
    main()
