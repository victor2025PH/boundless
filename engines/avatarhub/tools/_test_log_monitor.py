# -*- coding: utf-8 -*-
"""联测日志哨兵：同时尾随各服务日志,只打印值得人看的行。
[MON-ALERT] 前缀 = 疑似故障(错误/异常/失败/500) → 触发外部通知
[MON-EVENT] 前缀 = 关键业务事件(会话结束/弱语种回退/告警/预热完成) → 仅记录"""
import os, time, io, sys

BASE = r"c:\模仿音色"
FILES = {
    "interp": os.path.join(BASE, "logs", "interp.err.log"),
    "fish":   os.path.join(BASE, "logs", "fish_local.log"),
    "nemo":   os.path.join(BASE, "logs", "nemo_stt.err.log"),
    "relay":  os.path.join(BASE, "logs", "sup_monitor.log"),
    "alerts": os.path.join(BASE, "logs", "alerts.jsonl"),
}
BAD = ("error", "exception", "traceback", "failed", "失败", "异常", "拒绝", "refused",
       "http/1.1\" 500", "http/1.1\" 502", "critical", "unavailable", "熔断")
BAD_IGNORE = ("no error", "0 failed", "error=none", "error=0", "err.log",
              # WebRTC ICE 例行噪音:链路本地地址(169.254=虚拟网卡)绑定失败、STUN 候选对失败,
              # 均不影响 LAN 直连(host candidate 兜底),联测实测为无害刷屏
              "could not bind to 169.254", "candidatepair", "state.failed",
              # fish-speech 模型加载成功的固定话术含 "error" 字样(实为 PyTorch
              # load_state_dict 的 <All keys matched successfully>),纯成功信号
              "all keys matched successfully")
EVT = ("会话观测日志已写入", "弱语种", "质量", "告警", "劣化", "语向切换", "预热完成",
       "preload", "voicelock", "声纹", "已回退", "discontinuity", "断点", "通话模式")

pos = {}
for tag, p in FILES.items():
    try:
        pos[tag] = os.path.getsize(p)
    except OSError:
        pos[tag] = 0

print(f"[MON] 哨兵启动,盯 {len(FILES)} 个日志: {', '.join(FILES)}", flush=True)
while True:
    for tag, p in FILES.items():
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if sz < pos.get(tag, 0):          # 轮转/清空
            pos[tag] = 0
        if sz == pos.get(tag, 0):
            continue
        try:
            with io.open(p, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos.get(tag, 0))
                chunk = f.read(sz - pos.get(tag, 0))
            pos[tag] = sz
        except OSError:
            continue
        for ln in chunk.splitlines():
            s = ln.strip()
            if not s:
                continue
            # alerts.jsonl 是结构化流水,直接按 level/event 分级,不做关键词猜——
            # info 级周报正文里的"失败N次"统计字眼曾被误标成 MON-ALERT(2026-07-06)
            if tag == "alerts" and s.startswith("{"):
                try:
                    import json as _j
                    d = _j.loads(s)
                    is_bad = (d.get("level") in ("error", "critical")
                              and d.get("event") in ("raise", "notify"))
                    print(f"[MON-{'ALERT' if is_bad else 'EVENT'}][{tag}] {s[:400]}", flush=True)
                    continue
                except Exception:
                    pass          # 解析不了再落回关键词路径
            low = s.lower()
            if any(b in low for b in BAD) and not any(g in low for g in BAD_IGNORE):
                print(f"[MON-ALERT][{tag}] {s[:400]}", flush=True)
            elif any(e in s for e in EVT):
                print(f"[MON-EVENT][{tag}] {s[:300]}", flush=True)
    time.sleep(2)
