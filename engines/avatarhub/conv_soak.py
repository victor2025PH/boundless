# -*- coding: utf-8 -*-
"""对话并发 soak / 容量回归（受控、live）：
  N 个并发会话循环短问答，压真实 LLM+TTS（关口型/关RAG、max_tokens 小以控时），
  实测每轮 TTFA(首个含音频的 tts_chunk)、整轮耗时、成功率、错误分布；
  同时全程采样 GPU 空闲显存 与 /api/capacity 的 active/waiting，
  以证明「准入闸 K 不被突破 + 排队按设计工作 + 显存不泄漏 + 零崩溃」。
  临时 session_id，用完不留业务状态；仅读端点，绝不改服务配置。

用法：
  python conv_soak.py [时长s=60] [并发数=3]
  python conv_soak.py 30 1     # 单路基线（隔离排队惩罚，看纯渲染 TTFA）
  python conv_soak.py 60 3     # 并发 soak（看准入闸/队列/稳定性）
判读：成功率≥95% 且 active 峰值≤capacity.max 且显存无单调下滑 → PASS。
"""
import sys, time, json, threading, statistics, subprocess
import requests
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

HUB = "http://127.0.0.1:9000"
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 60   # 压测时长(s)
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 3     # 并发会话数
MAX_TOKENS = 48        # 限制回复长度→控时
PROMPTS = ["你好，简单介绍一下你自己", "今天天气怎么样", "讲一句鼓励的话",
           "用一句话说说你的爱好", "周末有什么推荐"]

prof = requests.get(f"{HUB}/profiles", timeout=10).json()["profiles"][0]["name"]
try:
    CAP_MAX = int(requests.get(f"{HUB}/api/capacity", timeout=10).json().get("max", 0))
except Exception:
    CAP_MAX = 0
print(f"角色: {prof} · 并发 {WORKERS} · 时长 {DURATION}s · max_tokens {MAX_TOKENS} · 准入上限 K={CAP_MAX or '不限'}")

results = []          # (ttfa_ms or None, turn_ms, ok:bool, err:str)
res_lock = threading.Lock()
stop_at = time.time() + DURATION
vram_free = []        # 采样的空闲显存(MiB)
cap_active = []       # 采样的 capacity.active（证明准入闸：应 ≤ max）
cap_waiting = []      # 采样的 capacity.waiting（证明排队）


def sample_vram():
    while time.time() < stop_at + 2:
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5)
            vram_free.append(int(out.stdout.strip().splitlines()[0]))
        except Exception:
            pass
        try:
            cap = requests.get(f"{HUB}/api/capacity", timeout=5).json()
            cap_active.append(int(cap.get("active", 0)))
            cap_waiting.append(int(cap.get("waiting", 0)))
        except Exception:
            pass
        time.sleep(2)


def worker(wid):
    sess = f"soak_{wid}_{int(time.time())}"
    i = 0
    while time.time() < stop_at:
        prompt = PROMPTS[i % len(PROMPTS)]; i += 1
        body = {"text": prompt, "session_id": sess, "profile": prof,
                "speak": True, "generate_lipsync": False, "use_rag": False,
                "max_tokens": MAX_TOKENS, "emotion": "neutral"}
        t0 = time.time(); ttfa = None; ok = False; err = ""
        try:
            with requests.post(f"{HUB}/api/converse/stream", json=body,
                               stream=True, timeout=120) as r:
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}"
                else:
                    for line in r.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        ph = ev.get("phase")
                        if ph == "tts_chunk" and ev.get("audio_base64") and ttfa is None:
                            ttfa = int((time.time() - t0) * 1000)
                        if ph in ("done", "cancelled"):
                            ok = True; break
                        if ph == "error":
                            err = (ev.get("message") or "error")[:60]; break
                        if ph == "busy":
                            err = "busy(队列满拒绝)"; break
        except Exception as e:
            err = f"{type(e).__name__}:{str(e)[:50]}"
        with res_lock:
            results.append((ttfa, int((time.time() - t0) * 1000), ok, err))


vt = threading.Thread(target=sample_vram, daemon=True); vt.start()
ths = [threading.Thread(target=worker, args=(w,)) for w in range(WORKERS)]
t_start = time.time()
for t in ths: t.start()
for t in ths: t.join()
elapsed = time.time() - t_start


def pctl(xs, p):
    if not xs: return 0
    xs = sorted(xs); k = (len(xs) - 1) * p / 100
    f = int(k); return round(xs[f] + (xs[min(f+1, len(xs)-1)] - xs[f]) * (k - f))


n = len(results); ok_n = sum(1 for r in results if r[2])
errs = [r[3] for r in results if r[3]]
ttfas = [r[0] for r in results if r[0] is not None]
turns = [r[1] for r in results if r[2]]
print("\n" + "=" * 60)
print(f"  对话并发 soak 结果（{elapsed:.0f}s 实测）")
print("=" * 60)
print(f"  总轮次: {n} · 成功: {ok_n} ({100*ok_n/max(1,n):.0f}%) · 失败: {n-ok_n}")
print(f"  吞吐: {ok_n/elapsed*60:.1f} 轮/分")
print(f"  TTFA(首音) p50/p95/max: {pctl(ttfas,50)}/{pctl(ttfas,95)}/{max(ttfas) if ttfas else 0} ms (n={len(ttfas)})")
print(f"  整轮耗时 p50/p95/max: {pctl(turns,50)}/{pctl(turns,95)}/{max(turns) if turns else 0} ms")
vram_ok = True
if vram_free:
    drift = vram_free[0] - vram_free[-1]    # 首尾空闲差>0 = 显存被吃掉(疑似泄漏)
    vram_ok = drift < 500
    print(f"  显存空闲 最低/最高: {min(vram_free)}/{max(vram_free)} MiB · 首尾漂移 {drift:+d} MiB (采样 {len(vram_free)} 次)")
adm_ok = True
if cap_active:
    peak = max(cap_active)
    adm_ok = (CAP_MAX <= 0) or (peak <= CAP_MAX)
    print(f"  准入闸 active 峰值/均值: {peak}/{statistics.mean(cap_active):.1f} (K={CAP_MAX or '不限'}{' ✓未突破' if adm_ok else ' ✗超限!'})")
    print(f"  队列 waiting 峰值/均值: {max(cap_waiting)}/{statistics.mean(cap_waiting):.1f}")
if errs:
    from collections import Counter
    print("  错误分布:")
    for e, c in Counter(errs).most_common(5):
        print(f"    {c}× {e}")
else:
    print("  错误: 无")
print("=" * 60)
ok_rate = ok_n / max(1, n)
verdict = "PASS" if (n > 0 and ok_rate >= 0.95 and adm_ok and vram_ok) else "CHECK"
if verdict == "PASS":
    print(f"  SOAK PASS: 成功率 {100*ok_rate:.0f}% · 准入闸守住 K≤{CAP_MAX or '∞'} · 显存无泄漏 · 无崩溃")
else:
    why = []
    if ok_rate < 0.95: why.append(f"成功率仅 {100*ok_rate:.0f}%")
    if not adm_ok: why.append("准入闸被突破")
    if not vram_ok: why.append("显存疑似泄漏")
    print(f"  SOAK CHECK: " + "；".join(why or ["见上"]))
sys.exit(0 if verdict == "PASS" else 1)
