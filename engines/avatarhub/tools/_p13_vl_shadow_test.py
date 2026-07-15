# -*- coding: utf-8 -*-
"""影子模式状态机隔离测试：从 live_interpreter.py 抽取 _VoiceLock 类,回放 2026-07-15 哑播事故。
不导入大模块(重依赖),用桩替换 ST/logger/常量。"""
import os, sys, time, types, tempfile, logging, threading
from collections import deque
import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "live_interpreter.py")
with open(SRC, "r", encoding="utf-8") as f:
    lines = f.readlines()
beg = next(i for i, l in enumerate(lines) if l.startswith("class _VoiceLock"))
end = next(i for i, l in enumerate(lines) if l.startswith("_voicelock = _VoiceLock()"))
cls_src = "".join(lines[beg:end])

EVENTS = []
ST = types.SimpleNamespace(live_mode=False, push_event=lambda d: EVENTS.append(d.get("warn", "")))
logging.basicConfig(level=logging.INFO, format="%(message)s")
tmpdir = tempfile.mkdtemp()
G = {
    "os": os, "time": time, "np": np, "deque": deque, "threading": threading,
    "logger": logging.getLogger("vl"), "ST": ST,
    "VOICELOCK_ENABLE": True, "VOICELOCK_THR": 0.4, "VOICELOCK_AUTOENROLL": True,
    "VOICELOCK_SUSPECT_N": 5, "VOICELOCK_SHADOW": True, "VOICELOCK_SHADOW_LIVE": False,
    "VOICELOCK_SHADOW_N": 5, "VOICELOCK_SHADOW_WIN_S": 120.0, "VOICELOCK_BASE_AGE_H": 72.0,
    "VOICELOCK_MODEL": "stub.onnx", "_VL_PERSIST": os.path.join(tmpdir, "vl.npz"),
    "REF_DIR": tmpdir,
}
exec(cls_src, G)
_VoiceLock = G["_VoiceLock"]

# ── 声纹向量桩：wav[0] 编码说话人 id ────────────────────────────────
D = 8
e0 = np.zeros(D, np.float32); e0[0] = 1.0                     # 旧底座(注册时的声音/链路)
e1 = np.zeros(D, np.float32); e1[1] = 1.0
owner = (0.2 * e0 + np.sqrt(1 - 0.04) * e1).astype(np.float32)  # 今天的机主: 与旧底座 sim=0.2
owner /= np.linalg.norm(owner)
tv = np.zeros(D, np.float32); tv[2] = 1.0                     # 电视/旁人: 与谁都不像
VOICES = {1.0: owner, 2.0: tv}

def wav(spk_id, sec=2.0):
    w = np.zeros(int(16000 * sec), np.float32); w[0] = spk_id
    return w

def make_vl():
    vl = _VoiceLock()
    vl.available = True
    vl.embed = lambda w: VOICES[float(w[0])].copy()
    vl.centroid = e0.copy(); vl.n_enrolled = 3                # 装载"失真旧底座"
    return vl

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# ── 场景1：事故回放(通话模式,长寿进程 accepts>0) ─────────────────────
vl = make_vl()
vl.accepts = 25                                              # 历史放行(3N=15 门槛几乎够不着的元凶)
vl.on_session_start("PD100X")
rejected = sum(0 if vl.check(wav(1.0))[0] else 1 for _ in range(5))
check(f"场景1: 前4句被拦第5句触发(实拦{rejected}句<=4)", rejected <= 4)
check("场景1: 证据窗自洽→当场重建底座,未滞留影子", not vl.shadow_on and vl.n_enrolled >= 3)
ok, sim = vl.check(wav(1.0))
check(f"场景1: 重建后本人正常放行(sim={sim:.2f})", ok and sim > 0.9)
ok_tv, sim_tv = vl.check(wav(2.0))
check(f"场景1: 重建后旁人声仍被拦(sim={sim_tv:.2f})", not ok_tv)
check("场景1: 有恢复事件推送", any("恢复正常拦截" in e for e in EVENTS))

# ── 场景2：直播无人值守——不自动放行,只告警一次 ────────────────────────
EVENTS.clear()
vl2 = make_vl(); vl2.accepts = 25
vl2.on_session_start("PD100X")
ST.live_mode = True
res2 = [vl2.check(wav(2.0))[0] for _ in range(8)]            # 电视声连讲8句
check("场景2: 直播模式全部仍被拦(不放行)", not any(res2))
check("场景2: 底座未被旁人声顶掉", bool(np.allclose(vl2.centroid, e0)))
check("场景2: 单次醒目告警", sum(1 for e in EVENTS if "疑似失真" in e) == 1)
ST.live_mode = False

# ── 场景3：通话模式混源(电视+机主交替)——一致性门挡住,后续机主真话重建 ──
EVENTS.clear()
vl3 = make_vl(); vl3.accepts = 25
vl3.on_session_start("PD100X")
seq = [2.0, 1.0, 2.0, 1.0, 2.0]                              # 交替混源,自洽不成立
[vl3.check(wav(s))[0] for s in seq]
check("场景3: 混源触发后进影子(放行保出声)而非误重建", vl3.shadow_on)
before = vl3.centroid.copy()
[vl3.check(wav(1.0)) for _ in range(4)]                      # 机主连说4句
check("场景3: 影子期机主自洽真话重建底座并退出影子", not vl3.shadow_on and not np.allclose(vl3.centroid, before))
ok3, _ = vl3.check(wav(2.0))
check("场景3: 重建后电视声再被拦", not ok3)

# ── 场景4：会话内曾真实放行→影子永不触发(底座没坏) ────────────────────
vl4 = make_vl(); vl4.accepts = 25
vl4.on_session_start("PD100X")
vl4.centroid = owner.copy()                                  # 底座是好的
vl4.check(wav(1.0))                                          # 机主一句真实放行
res4 = [vl4.check(wav(2.0))[0] for _ in range(10)]           # 旁人狂讲10句
check("场景4: 有真实放行时旁人连讲不触发影子", not vl4.shadow_on and not any(res4))

# ── 场景6：「是我」放行学习——反复确认后底座向本人收敛 ──────────────────
vl6 = make_vl(); vl6.accepts = 25
vl6.on_session_start("PD100X")
sims6 = []
for _ in range(6):
    ok6, s6 = vl6.check(wav(1.0))
    sims6.append(s6)
    if not ok6:
        vl6.affirm_learn(wav(1.0))                           # 用户每次点「是我」
check(f"场景6: 放行学习后相似度单调上升({sims6[0]:.2f}→{sims6[-1]:.2f})", sims6[-1] > sims6[0] + 0.2)
check("场景6: 最终本人直接过门", vl6.check(wav(1.0))[0])
check("场景6: 学习后旁人声仍被拦", not vl6.check(wav(2.0))[0])

# ── 场景5：换麦清证据窗 + 底座健康度提示 ──────────────────────────────
EVENTS.clear()
vl5 = make_vl(); vl5.accepts = 25
vl5.on_session_start("PD100X")
[vl5.check(wav(1.0)) for _ in range(3)]                      # 攒3句证据
vl5.on_session_start("USB 会议麦")                            # 换麦重启会话
check("场景5: 换麦后证据窗清零", len(vl5._rej_win) == 0)
vl5.base_meta = {"ts": time.time() - 100 * 3600, "mic": "PD100X"}
vl5.on_session_start("USB 会议麦")
check("场景5: 底座过老+换麦有健康提示", any("健康提示" in e for e in EVENTS))

print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
