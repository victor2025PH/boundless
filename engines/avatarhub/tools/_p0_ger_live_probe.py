# -*- coding: utf-8 -*-
"""P0 GER 实链路探测：向系统默认输出播中文合成音 → 对方声环回通道(方向B)
完整走 流式ASR→门控→定稿→GER复核。之后拉取 /events 与 ger 计数验证闭环。"""
import sys, io, json, time
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
INTERP = "http://127.0.0.1:7900"


def last_event_id():
    r = requests.get(f"{INTERP}/events?since=0", stream=True, timeout=4)
    last = 0
    t0 = time.time()
    try:
        for line in r.iter_lines(decode_unicode=True):
            if time.time() - t0 > 2.5:
                break
            if line and line.startswith("data:"):
                ev = json.loads(line[5:])
                last = max(last, ev.get("id", 0))
    except Exception:
        pass
    finally:
        r.close()
    return last


def events_since(since, wait_s):
    out = []
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{INTERP}/events?since={since}", stream=True, timeout=6)
            for line in r.iter_lines(decode_unicode=True):
                if time.time() > deadline:
                    break
                if line and line.startswith("data:"):
                    ev = json.loads(line[5:])
                    since = max(since, ev.get("id", 0))
                    out.append(ev)
            r.close()
        except Exception:
            time.sleep(0.5)
    return out


def speak(text):
    import win32com.client
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    for v in sp.GetVoices():
        if "Huihui" in v.GetDescription():
            sp.Voice = v
            break
    sp.Rate = 0
    sp.Speak(text)


def main():
    base = last_event_id()
    print("base event id:", base)
    ger0 = (requests.get(f"{INTERP}/metrics", timeout=5).json().get("audio_health") or {}).get("ger")
    print("ger before:", json.dumps(ger0, ensure_ascii=False))
    text = "我们现在用通译软件进行实时翻译测试"
    print("speaking:", text)
    speak(text)
    print("waiting 14s for final + GER review...")
    evs = events_since(base, 14)
    for ev in evs:
        k = {kk: vv for kk, vv in ev.items() if kk in
             ("id", "who", "live", "src", "dst", "zh", "en", "retract", "ger", "suspect", "partial", "warn")}
        print("  ev:", json.dumps(k, ensure_ascii=False)[:180])
    ger1 = (requests.get(f"{INTERP}/metrics", timeout=5).json().get("audio_health") or {}).get("ger")
    print("ger after:", json.dumps(ger1, ensure_ascii=False))


if __name__ == "__main__":
    main()
