# -*- coding: utf-8 -*-
"""通话实测监控：轮询 /metrics + /review/clips 增量 + tail interp.err.log。
每个事件一行 ASCII 哨兵前缀(便于上层按行监听)，后跟简要中文详情。
A-FIN/B-FIN=我方/对方一句定稿(带端到端毫秒) SLOW=e2e>6s VL-*=声纹 GER-*=二遍复核
ECHO/GATE/SUSPECT=闸口动作 ERR=异常。Ctrl+C 停。
"""
import io, json, os, re, sys, time
import urllib.request

BASE = "http://127.0.0.1:7900"
LOG = r"C:\模仿音色\logs\interp.err.log"

RULES = [
    (re.compile(r"拦截非注册说话人"), "VL-BLOCK"),
    (re.compile(r"声纹自动注册进度"), "VL-STEP"),
    (re.compile(r"声纹锁自动注册完成"), "VL-DONE"),
    (re.compile(r"连拒自愈"), "VL-HEAL"),
    (re.compile(r"字幕纠错"), "GER-FIX"),
    (re.compile(r"存疑段复核=噪声,撤回"), "GER-VETO"),
    (re.compile(r"存疑段复核=旁人声,撤回"), "VL-VETO"),
    (re.compile(r"存疑段复核=真话,晋升"), "GER-REVIVE"),
    (re.compile(r"回声闸"), "ECHO-DROP"),
    (re.compile(r"前置门控"), "GATE-DROP"),
    (re.compile(r"标记制"), "SUSPECT"),
    # 注意:不匹配裸 Traceback——浏览器刷新页面产生的 ProactorBasePipeTransport/
    # ConnectionResetError 噪音栈会刷屏(2026-07-08 实测 4 连误报)。只认业务异常。
    (re.compile(r"流式定稿异常|定稿异常|裁决异常|采集启动失败|播放线程退出|合成失败"), "ERR"),
]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.load(r)


def main():
    f = io.open(LOG, "r", encoding="utf-8", errors="replace")
    f.seek(0, os.SEEK_END)
    m0 = get("/metrics")
    vl0 = (m0.get("voicelock") or {})
    last_rej = vl0.get("rejects", 0)
    # 定稿文本以 /transcript 留档为准(turn 全局递增,按 turn 增量打印)。
    # 旧版按 counts 增量 + 复盘剪辑最后一条取文本:剪辑晚存 → 新句显示旧文本,
    # 看起来像"同一句定稿两次"(2026-07-08 11:51 误判"说两遍"的来源,勿回退)。
    last_turn = {"me": 0, "other": 0}
    try:
        for e in (get("/transcript").get("entries") or []):
            w = e.get("who") or ""
            if w in last_turn:
                last_turn[w] = max(last_turn[w], int(e.get("turn") or 0))
    except Exception:
        pass
    print("MONITOR-READY turn me=%s other=%s" % (last_turn["me"], last_turn["other"]), flush=True)
    while True:
        try:
            m = get("/metrics")
            e2e = (m.get("recent_e2e") or [None])[-1]
            for e in (get("/transcript").get("entries") or []):
                w = e.get("who") or ""
                t = int(e.get("turn") or 0)
                if w not in last_turn or t <= last_turn[w]:
                    continue
                last_turn[w] = t
                txt = (e.get("src") or "")[:60]
                if w == "me":
                    slow = " SLOW" if (e2e or 0) > 6000 else ""
                    print(f"A-FIN{slow}: turn={t} e2e_ms={e2e} text={txt}", flush=True)
                else:
                    print(f"B-FIN: turn={t} text={txt}", flush=True)
            vl = m.get("voicelock") or {}
            if vl.get("rejects", 0) > last_rej:
                last_rej = vl["rejects"]
                print(f"VL-REJ: rejects={last_rej} last_sim={vl.get('last_sim')}", flush=True)
        except Exception as e:
            print("POLL-ERR:", repr(e)[:100], flush=True)
        while True:
            line = f.readline()
            if not line:
                break
            for pat, tag in RULES:
                if pat.search(line):
                    print(f"{tag}: {line.strip()[:150]}", flush=True)
                    break
        time.sleep(3)


if __name__ == "__main__":
    main()
