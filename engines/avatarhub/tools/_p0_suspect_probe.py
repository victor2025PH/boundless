# -*- coding: utf-8 -*-
"""P0-④ 标记制探测：低音量合成音 → 触发 RMS 门 → 灰字(suspect)→ GER 复核晋升/撤回。"""
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
                last = max(last, json.loads(line[5:]).get("id", 0))
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


def speak_quiet(text, volume):
    import win32com.client
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    for v in sp.GetVoices():
        if "Huihui" in v.GetDescription():
            sp.Voice = v
            break
    sp.Volume = volume     # 0~100
    sp.Rate = 0
    sp.Speak(text)


def main():
    vol = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    base = last_event_id()
    ger0 = (requests.get(f"{INTERP}/metrics", timeout=5).json().get("audio_health") or {}).get("ger")
    print("ger before:", json.dumps(ger0, ensure_ascii=False), "| volume:", vol)
    text = "这句话声音很小可能会被门控拦截"
    print("speaking (quiet):", text)
    speak_quiet(text, vol)
    print("waiting 16s ...")
    evs = events_since(base, 16)
    for ev in evs:
        k = {kk: vv for kk, vv in ev.items() if kk in
             ("id", "who", "live", "src", "dst", "zh", "en", "retract", "ger", "suspect", "partial")}
        print("  ev:", json.dumps(k, ensure_ascii=False)[:180])
    ger1 = (requests.get(f"{INTERP}/metrics", timeout=5).json().get("audio_health") or {}).get("ger")
    m = requests.get(f"{INTERP}/metrics", timeout=5).json()
    print("ger after:", json.dumps(ger1, ensure_ascii=False))
    print("drops:", json.dumps((m.get("audio_health") or {}).get("drops"), ensure_ascii=False))


if __name__ == "__main__":
    main()
