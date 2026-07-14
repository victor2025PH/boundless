# -*- coding: utf-8 -*-
"""同传端到端自测装置(无人值守)：
  阶段1 注入式——本地起 PCM 假麦服务(模拟手机麦直连)，把已知中文句注入通译，
        验证「识别文本≈发出文本 → 翻译非空 → 配音已合成」。隔离声学，专测管线。
  阶段2 声学式——SAPI 经音箱发声、BRIO 实收，验证真实麦克风链路(含 RNNoise/门控/半双工)。
  阶段3 长跑——按节奏持续注入 N 轮，验证"不会收录一句后停止"(问题2回归)。
输出: logs/selftest_report.json + stdout 逐步日志。
"""
import base64, difflib, io, json, os, queue, socket, subprocess, sys, threading, time, wave

import numpy as np
import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERP = "http://127.0.0.1:7900"
PCM_PORT = 7899
SR = 16000

ZH_SENTENCES = [
    "今天天气非常好，我们出去走一走吧。",
    "请问这个东西多少钱，能不能便宜一点。",
    "我们明天上午九点在公司门口集合。",
    "这个项目的进度比我们预想的要快很多。",
]
EN_SENTENCE = "How much does it cost in total?"

LOG = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


def norm(t):
    return "".join(c.lower() for c in (t or "") if c.isalnum())


def sim(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ── 语音素材：SAPI 合成 16k 单声道 WAV ────────────────────────────────
def sapi_wav(text, path, lang="zh"):
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{path}', $fmt)
$s.Rate = -1
$s.Speak('{text}')
$s.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=60)
    return os.path.exists(path)


def load_wav16k(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            pcm = pcm.reshape(-1, w.getnchannels()).mean(axis=1)
    if sr != SR:
        n = int(len(pcm) * SR / sr)
        pcm = np.interp(np.linspace(0, len(pcm), n, False), np.arange(len(pcm)), pcm).astype(np.float32)
    return pcm


# ── 阶段1：PCM 假麦服务(模拟中继 /mic/pcm) ────────────────────────────
class FakeMic(threading.Thread):
    """GET /pcm → 无限 int16 PCM 流：默认静音，inject() 的音频按实时节奏播出。"""
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.q = queue.Queue()
        self._srv = None

    def inject(self, f32):
        self.q.put(np.asarray(f32, np.float32))

    def run(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        fm = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/pcm":
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("X-Sample-Rate", str(SR))
                self.end_headers()
                chunk = int(SR * 0.1)                       # 100ms
                pending = np.zeros(0, np.float32)
                try:
                    while True:
                        if pending.size < chunk:
                            try:
                                pending = np.concatenate([pending, fm.q.get_nowait()])
                            except queue.Empty:
                                pass
                        if pending.size >= chunk:
                            out, pending = pending[:chunk], pending[chunk:]
                        else:
                            out = np.zeros(chunk, np.float32)
                        pcm = (np.clip(out, -1, 1) * 32767).astype("<i2").tobytes()
                        self.wfile.write(pcm)
                        time.sleep(0.1)                     # 实时节奏
                except Exception:
                    return

        class Srv(ThreadingHTTPServer):
            allow_reuse_address = True                     # 上一实例 TIME_WAIT 不阻塞重绑

        try:
            self._srv = Srv(("127.0.0.1", self.port), H)
        except Exception as e:
            log(f"✗ 假麦服务绑定 {self.port} 失败: {e}")
            return
        self.ready = True
        self._srv.serve_forever()

    def wait_ready(self, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if getattr(self, "ready", False):
                try:                                        # 真连一把,确认可服务
                    s = socket.create_connection(("127.0.0.1", self.port), 2)
                    s.close()
                    return True
                except Exception:
                    pass
            time.sleep(0.2)
        return False

    def shutdown(self):
        try:
            self._srv.shutdown()
            self._srv.server_close()                       # 关监听 socket,释放端口给下个实例
        except Exception:
            pass


# ── 事件收集(SSE /events) ─────────────────────────────────────────────
class EventTap(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.events = []
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        last = 0
        while not self._stop.is_set():
            try:
                with requests.get(f"{INTERP}/events", params={"since": last},
                                  stream=True, timeout=(5, 30)) as r:
                    for raw in r.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if not raw or not raw.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(raw[5:].strip())
                        except Exception:
                            continue
                        last = max(last, ev.get("id") or 0)
                        with self.lock:
                            self.events.append(ev)
            except Exception:
                time.sleep(1)

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def stop(self):
        self._stop.set()


def accepted_rows(events, who):
    """按 turn 聚合非撤回的定稿文本(zh/en 合并子句)。"""
    turns = {}
    for e in events:
        if e.get("who") != who or e.get("turn") is None:
            continue
        t = turns.setdefault(e["turn"], {"zh": {}, "en": {}, "retracted": False})
        if e.get("retract"):
            t["retracted"] = True
        if e.get("uid") is not None:
            if e.get("zh"):
                t["zh"][e["uid"]] = e["zh"]
            if e.get("en"):
                t["en"][e["uid"]] = e["en"]
        if e.get("finalize"):
            if e.get("zh"):
                t["zh"] = {0: e["zh"]}
            if e.get("en"):
                t["en"] = {0: e["en"]}
    out = []
    for tid, t in turns.items():
        if t["retracted"] and not t["zh"]:
            continue
        zh = " ".join(t["zh"][k] for k in sorted(t["zh"]))
        en = " ".join(t["en"][k] for k in sorted(t["en"]))
        if zh or en:
            out.append({"turn": tid, "zh": zh, "en": en})
    return out


def metrics():
    return requests.get(f"{INTERP}/metrics", timeout=8).json()


def api(method, path, **kw):
    r = requests.request(method, INTERP + path, timeout=kw.pop("timeout", 15), **kw)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "http": r.status_code}


def wait_final(tap, who, prev_n, timeout=25):
    """等到 who 侧新增一条含译文的定稿行，返回行列表。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rows = [r for r in accepted_rows(tap.snapshot(), who) if r["en"] or r["zh"]]
        if len(rows) > prev_n:
            time.sleep(1.2)      # 等子句补齐
            return accepted_rows(tap.snapshot(), who)
        time.sleep(0.5)
    return accepted_rows(tap.snapshot(), who)


def score_phase(name, spoken, rows):
    """spoken=[(zh句)],rows=accepted me 行 → 相似度评分。"""
    detail, hit = [], 0
    for s in spoken:
        best, best_r = None, 0.0
        for r in rows:
            v = sim(s, r["zh"])
            if v > best_r:
                best_r, best = v, r
        ok = best_r >= 0.55 and bool(best and best["en"])
        hit += 1 if ok else 0
        detail.append({"spoken": s, "matched": (best or {}).get("zh", ""),
                       "asr_sim": round(best_r, 3), "en": (best or {}).get("en", ""), "ok": ok})
    res = {"phase": name, "total": len(spoken), "pass": hit, "detail": detail}
    log(f"◆ {name}: {hit}/{len(spoken)} 句通过")
    for d in detail:
        log(f"   {'✓' if d['ok'] else '✗'} 说:{d['spoken'][:18]} → 识:{d['matched'][:18]} (sim={d['asr_sim']}) 译:{d['en'][:30]}")
    return res


REPORT = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": []}


def phase1_injection(wavs):
    log("===== 阶段1 注入式管线测试(假麦直连,无声学干扰) =====")
    fm = FakeMic(PCM_PORT)
    fm.start()
    if not fm.wait_ready():
        log("✗ 假麦服务未就绪,阶段1中止")
        REPORT["phases"].append({"phase": "阶段1·注入式", "ok": False, "error": "fake mic not ready"})
        return None
    api("POST", "/voicelock/reset")
    api("POST", "/monitor", json={"on": False})    # 注入阶段不需要耳返外放(夜间安静+少一路变量)
    d = api("GET", "/devices")
    cable = (d.get("defaults") or {}).get("cable")
    loop = (d.get("defaults") or {}).get("loopback")
    st = api("POST", "/start", json={
        "mic_index": 0, "cable_index": cable if cable is not None else 22,
        "loopback_index": loop if loop is not None else -1,
        "loopback_is_output": False, "profile": "", "mode": "local",
        "live_mode": False, "stream": True,
        "mic_net_url": f"http://127.0.0.1:{PCM_PORT}/pcm"})
    log(f"start: {json.dumps(st, ensure_ascii=False)[:120]}")
    tap = EventTap(); tap.start()
    time.sleep(4)
    m0 = metrics()
    rows = []
    for i, (txt, pcm) in enumerate(zip(ZH_SENTENCES, wavs)):
        prev = len(accepted_rows(tap.snapshot(), "me"))
        log(f"注入第 {i+1} 句({len(pcm)/SR:.1f}s): {txt}")
        fm.inject(pcm)
        rows = wait_final(tap, "me", prev, timeout=max(20, len(pcm) / SR + 15))
        time.sleep(2.0)
    m1 = metrics()
    res = score_phase("阶段1·注入式", ZH_SENTENCES, rows)
    res["metrics"] = {"fin": (m1.get("stream") or {}).get("fin"),
                      "counts": m1.get("counts"), "synth_n": (m1.get("synth_ms") or {}).get("n"),
                      "drops": (m1.get("audio_health") or {}).get("drops")}
    REPORT["phases"].append(res)
    api("POST", "/stop")
    tap.stop(); fm.shutdown()
    time.sleep(2)
    return res


def _speak_via_speaker(text, rate=-1):
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = {rate}
$s.Volume = 100
$s.Speak('{text}')
$s.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=90)


def phase2_acoustic():
    log("===== 阶段2 声学链路测试(SAPI→音箱→BRIO 实收) =====")
    api("POST", "/voicelock/reset")
    st = api("POST", "/call_mode/start", json={}, timeout=120)
    oks = [s.get("ok") for s in (st.get("steps") or [])]
    log(f"call_mode/start: ok={st.get('ok')} steps={sum(1 for o in oks if o)}/{len(oks)}")
    if not st.get("ok"):
        for s in (st.get("steps") or []):
            if not s.get("ok"):
                log(f"   ✗ {s.get('name')}: {str(s.get('detail'))[:100]}")
    tap = EventTap(); tap.start()
    time.sleep(4)

    # 2a) 耳返关：纯声学识别准确率(配音只进 CABLE,不进音箱,无串扰变量)
    api("POST", "/monitor", json={"on": False})
    rows = []
    for i, txt in enumerate(ZH_SENTENCES):
        prev = len(accepted_rows(tap.snapshot(), "me"))
        log(f"[2a·耳返关] 音箱发声第 {i+1} 句: {txt}")
        _speak_via_speaker(txt)
        rows = wait_final(tap, "me", prev, timeout=30)
        time.sleep(3)
    res = score_phase("阶段2a·声学(耳返关)", ZH_SENTENCES, rows)
    m = metrics()
    res["metrics"] = {"fin": (m.get("stream") or {}).get("fin"), "counts": m.get("counts"),
                      "drops": (m.get("audio_health") or {}).get("drops"),
                      "voicelock": m.get("voicelock")}
    REPORT["phases"].append(res)

    # 2b) 耳返开(音箱外放)：半双工应把外放期间的麦静音——外放英文不得变成新识别行(回声自激回归)
    api("POST", "/monitor", json={"on": True})
    sub = ZH_SENTENCES[:2]
    rows_b = []
    for i, txt in enumerate(sub):
        prev = len(accepted_rows(tap.snapshot(), "me"))
        log(f"[2b·耳返开] 音箱发声第 {i+1} 句: {txt}")
        _speak_via_speaker(txt)
        rows_b = wait_final(tap, "me", prev, timeout=30)
        time.sleep(12)              # 留足耳返英文外放完 + 半双工尾闸恢复
    res_b = score_phase("阶段2b·声学(耳返开·半双工)", sub, rows_b)
    m = metrics()
    # 回声自激检查：任何 me 行既不像发出的中文句、又像英文配音回录 → 泄漏
    dubbed = [d["en"] for d in res["detail"] + res_b["detail"] if d["en"]]
    leaks = [r for r in rows_b
             if r["zh"] and all(sim(r["zh"], s) < 0.4 for s in ZH_SENTENCES)
             and (max((sim(r["zh"], e) for e in dubbed), default=0) > 0.6
                  or all(ord(c) < 128 for c in norm(r["zh"])[:12] or "x"))]
    res_b["echo_leak"] = len(leaks)
    res_b["metrics"] = {"drops": (m.get("audio_health") or {}).get("drops")}
    log(f"[2b] 回声泄漏行: {len(leaks)} (应为 0)")
    REPORT["phases"].append(res_b)

    api("POST", "/monitor", json={"on": False})
    api("POST", "/stop")
    tap.stop()
    time.sleep(2)
    return res


def phase3_longrun(wavs, minutes=8.0):
    log(f"===== 阶段3 长跑稳定性(注入式,{minutes:.0f} 分钟,验证不会中途停摆) =====")
    fm = FakeMic(PCM_PORT - 2)                      # 换端口(7897):避开上一实例残留,也别撞通译 7900
    fm.start()
    if not fm.wait_ready():
        log("✗ 假麦服务未就绪,阶段3中止")
        REPORT["phases"].append({"phase": "阶段3·长跑", "ok": False, "error": "fake mic not ready"})
        return
    api("POST", "/voicelock/reset")
    d = api("GET", "/devices")
    cable = (d.get("defaults") or {}).get("cable")
    loop = (d.get("defaults") or {}).get("loopback")
    api("POST", "/start", json={
        "mic_index": 0, "cable_index": cable if cable is not None else 22,
        "loopback_index": loop if loop is not None else -1,
        "loopback_is_output": False, "profile": "", "mode": "local",
        "live_mode": False, "stream": True,
        "mic_net_url": f"http://127.0.0.1:{fm.port}/pcm"})
    tap = EventTap(); tap.start()
    time.sleep(4)
    t_end = time.time() + minutes * 60
    cyc, stall, last_fin = 0, 0, 0
    fins = []
    while time.time() < t_end:
        pcm = wavs[cyc % len(wavs)]
        fm.inject(pcm)
        cyc += 1
        time.sleep(len(pcm) / SR + 7.5)          # 句长+静音间隔
        m = metrics()
        fin = (m.get("stream") or {}).get("fin") or 0
        fins.append(fin)
        alive = m.get("running")
        if fin <= last_fin:
            stall += 1
            log(f"⚠ 第 {cyc} 轮未见新定稿(fin 停在 {fin})")
            if stall == 1:
                try:
                    s = api("GET", "/status")
                    log(f"   诊断: cap_a_err={s.get('cap_a_err')} cap_b_err={s.get('cap_b_err')}")
                except Exception:
                    pass
        last_fin = fin
        if not alive:
            log("✗ 会话中途死亡!")
            break
        if cyc % 5 == 0:
            log(f"  …第 {cyc} 轮, fin={fin}, counts={json.dumps(m.get('counts'))}")
    m = metrics()
    res = {"phase": "阶段3·长跑", "cycles": cyc, "stalls": stall,
           "final_fin": last_fin, "running_at_end": m.get("running"),
           "counts": m.get("counts"),
           "ok": (stall <= max(1, cyc // 10)) and m.get("running") is True}
    log(f"◆ 阶段3: {cyc} 轮注入, 停顿轮 {stall}, 结束时会话存活={m.get('running')}")
    REPORT["phases"].append(res)
    api("POST", "/stop")
    tap.stop(); fm.shutdown()
    return res


def main():
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    wav_dir = os.path.join(BASE, "logs", "_selftest_wavs")
    os.makedirs(wav_dir, exist_ok=True)
    wavs = []
    for i, s in enumerate(ZH_SENTENCES):
        p = os.path.join(wav_dir, f"zh_{i}.wav")
        if not os.path.exists(p):
            log(f"合成测试语音 {i+1}/{len(ZH_SENTENCES)}: {s}")
            if not sapi_wav(s, p):
                log(f"✗ SAPI 合成失败: {s}")
                sys.exit(2)
        wavs.append(load_wav16k(p))

    try:
        phase1_injection(wavs)
        phase2_acoustic()
        phase3_longrun(wavs, minutes=float(os.environ.get("SELFTEST_LONGRUN_MIN", "8")))
    finally:
        # 善后：清声纹(下次用户开口重新注册本人)，会话确保停止
        api("POST", "/voicelock/reset")
        api("POST", "/stop")
        REPORT["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        REPORT["log"] = LOG
        out = os.path.join(BASE, "logs", "selftest_report.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(REPORT, f, ensure_ascii=False, indent=2)
        log(f"报告已写入 {out}")


if __name__ == "__main__":
    main()
