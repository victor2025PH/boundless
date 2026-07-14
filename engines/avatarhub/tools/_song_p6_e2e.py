# -*- coding: utf-8 -*-
"""Song-P6 真跑验收：空转直播治理 / 唱腔 LoRA(音乐人格) / 批量魔改(专辑化) / 点歌台全自动闭环。

覆盖：
  ①治理配置与策略：/api/heal/config 暴露 idle_live_govern+阈值、运行时可切可持久化；
    /api/heal/selftest 全绿（内含 noface超阈下播/独立于autoheal/回合去重 场景矩阵）
  ②唱腔 LoRA：health.create.capabilities.loras 如实上报就绪名单；
    lora=中文RAP 真跑一首 → 成品元数据带 lora、历史文本带（RAP腔）、音频可播；
    未装的 lora 名 → 400 人话拒
  ③批量魔改：一首参考 × 2 风格一键排产 → 双任务各自入历史且名字不同（不被
    24h 去重互吞）；>6 风格 → 400 上限人话
  ④点歌台全自动闭环：auto_play 开 + 队列曲目备好 → 无人点播自动上麦(冷启动
    第一首，P6 新补) → 播完自动 done。vcam 不在线则如实 SKIP。

用法： python tools/_song_p6_e2e.py
"""
import io
import json
import math
import sqlite3
import struct
import sys
import time
import wave
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parents[1]
SONGS_DIR = ROOT / "songs"
RAP_LORA = "ACE-Step-v1-chinese-rap-LoRA"

PASS = []
FAIL = []
SKIP = []


def _req(method, url, *, retries=6, backoff=5.0, **kw):
    last = None
    for i in range(retries):
        try:
            return requests.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            if i < retries - 1:
                print(f"    ..连接失败({type(e).__name__})，{backoff:.0f}s 后重试 {i+1}/{retries-1}")
                time.sleep(backoff)
    raise last


def ok(label, extra=""):
    PASS.append(label)
    print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))


def ng(label, extra=""):
    FAIL.append(label)
    print(f"  [FAIL] {label}" + (f"  {extra}" if extra else ""))


def skip(label, why=""):
    SKIP.append(label)
    print(f"  [SKIP] {label}" + (f"  {why}" if why else ""))


RAP_LYRICS = """[verse]
键盘敲出节奏感
代码写到天亮才算完
别问我累不累
梦想它自己会发光

[chorus]
就是干 就是拼
这条路我自己定
"""


def wait_create(tid, timeout=1800):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        r = _req("GET", f"{HUB}/api/song/create/{tid}", timeout=30)
        if r.status_code != 200:
            return {"status": "http_error", "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
        last = r.json()
        if last.get("status") in ("done", "error", "cancelled"):
            return last
        time.sleep(2.0)
    last["_timeout"] = True
    return last


def find_history_by_id(hist_id, timeout=30):
    if not hist_id:
        return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = _req("GET", f"{HUB}/api/history",
                     params={"limit": 30}, timeout=15).json()
            for rec in d.get("records", []):
                if rec.get("id") == hist_id:
                    return rec
        except Exception:
            pass
        time.sleep(3.0)
    return None


def preflight_clear_zombie_live():
    """P5 同款+P6 再进化：只有「真直播」才中止验收（不打扰底线）；僵尸直播停掉再验。
    converse/vram/hold 源（run1/run3 实锤：另一个自动化会话循环跑在线门禁，每轮
    3~4 分钟对话活跃、周期约 10 分钟——等清场会永远等不到）**不再阻塞开跑**：
    让路协议本就是排队不是拒绝，任务提交后自动熬过让路窗口，
    各段等待上限已放大到能跨过 2~3 轮门禁（wait_create 1800s / station 900s）。"""
    try:
        y = _req("GET", f"{HUB}/api/song/yield", timeout=10).json()
    except Exception:
        return True
    if not y.get("yield"):
        return True
    if y.get("source") in ("converse", "vram", "hold"):
        print(f"  [!] 让路中({y.get('source')}): {y.get('reason')}"
              "——照常开跑，任务会排队等让路解除（断言只看最终完成）")
        return True
    st = {}
    try:
        st = _req("GET", f"{HUB}/realtime/status", timeout=10).json()
    except Exception:
        pass
    health = (st.get("health") or {}).get("state", "")
    if st.get("video_running") and health in ("noface", "stalled", "svc_down"):
        print(f"  [!] 检测到僵尸直播(health={health})——停掉以免验收被让路挡死")
        try:
            _req("POST", f"{HUB}/realtime/stop", timeout=15)
        except Exception as e:
            print(f"  [!] 停僵尸直播失败: {e}")
            return False
        t0 = time.time()
        while time.time() - t0 < 30:
            time.sleep(3.0)
            try:
                y2 = _req("GET", f"{HUB}/api/song/yield", timeout=10).json()
                if not y2.get("yield") or y2.get("source") != "live":
                    return True
            except Exception:
                pass
        return False
    print(f"  [X] 真直播进行中(health={health or '未知'})：不打扰，验收中止。")
    return False


def make_tone_wav(path: Path, seconds=8.0, sr=22050):
    """带旋律起伏的合成音（比纯静音对分离/F0 友好）：A3/E4 交替 + 音量包络。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(seconds * sr)
        frames = bytearray()
        for i in range(n):
            t = i / sr
            f = 220.0 if int(t * 2) % 2 == 0 else 330.0
            env = 0.35 * (0.6 + 0.4 * math.sin(2 * math.pi * 0.5 * t))
            v = int(32767 * env * math.sin(2 * math.pi * f * t))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    path.write_bytes(buf.getvalue())


def main():
    print("== ⓪ 环境预检 ==")
    if not preflight_clear_zombie_live():
        print("RESULT: SKIP (直播占用)")
        return

    # ① 空转直播治理：配置暴露 / 运行时切换持久化 / 策略演练全绿
    print("== ① 空转直播治理 ==")
    cfg = _req("GET", f"{HUB}/api/heal/config", timeout=10).json()
    (ok if isinstance(cfg.get("idle_live_govern"), bool) else ng)(
        "治理开关暴露", str(cfg.get("idle_live_govern")))
    g = cfg.get("idle_live_guard") or {}
    (ok if g.get("noface_min") and g.get("stalled_min") else ng)(
        "分钟级阈值暴露", json.dumps(g))
    orig = bool(cfg.get("idle_live_govern"))
    try:
        r1 = _req("POST", f"{HUB}/api/heal/config",
                  json={"idle_live_govern": not orig}, timeout=10).json()
        (ok if r1.get("idle_live_govern") == (not orig) else ng)(
            "运行时可切", f"{orig}→{r1.get('idle_live_govern')}")
        hc = json.loads((ROOT / "data" / "heal_config.json").read_text(encoding="utf-8"))
        (ok if hc.get("idle_live_govern") == (not orig) else ng)(
            "切换随 heal_config.json 持久化", str(hc.get("idle_live_govern")))
    finally:
        _req("POST", f"{HUB}/api/heal/config", json={"idle_live_govern": orig}, timeout=10)
    st = _req("GET", f"{HUB}/api/heal/selftest", timeout=20).json()
    bad = [c["name"] for c in st.get("checks", []) if not c.get("ok")]
    (ok if st.get("ok") and not bad else ng)(
        f"自愈演练全绿(含治理场景) {st.get('pass')}/{st.get('total')}", ",".join(bad)[:120])
    names = [c["name"] for c in st.get("checks", [])]
    (ok if "僵尸noface超阈·自动下播" in names and "治理独立于自愈开关" in names else ng)(
        "治理场景确实在演练矩阵里")

    # ② 唱腔 LoRA
    print("== ② 唱腔 LoRA(音乐人格) ==")
    h = _req("GET", f"{HUB}/api/song/health", timeout=15).json()
    c = h.get("create") or {}
    caps = c.get("capabilities") or {}
    if not (c.get("online") and caps.get("create")):
        ng("原创歌引擎在线", str(c)[:200])
        print("引擎不在线，后续无法验收")
        return
    loras = caps.get("loras") or []
    (ok if RAP_LORA in loras else ng)("loras 能力如实上报", str(loras))
    _req("POST", f"{HUB}/api/song/yield/hold", params={"on": False}, timeout=10)

    r = _req("POST", f"{HUB}/api/song/create", json={
        "style": "hip hop, rap, mandarin, male vocal, heavy bass, 95 bpm",
        "lyrics": RAP_LYRICS, "duration_s": 30, "quality": "turbo",
        "svc_swap": False, "song_name": "P6E2E说唱人格",
        "lora": "不存在的LoRA名",
    }, timeout=30)
    d = {}
    try:
        d = r.json()
    except Exception:
        pass
    (ok if r.status_code == 400 and "未安装" in str(d.get("detail", "")) else ng)(
        "未装 LoRA 人话拒(400)", f"HTTP {r.status_code} {str(d.get('detail', ''))[:80]}")

    hist_rap = None
    r = _req("POST", f"{HUB}/api/song/create", json={
        "style": "hip hop, rap, mandarin, male vocal, heavy bass, 95 bpm",
        "lyrics": RAP_LYRICS, "duration_s": 30, "quality": "turbo",
        "svc_swap": False, "song_name": "P6E2E说唱人格",
        "lora": RAP_LORA, "lora_weight": 1.0,
    }, timeout=30)
    if r.status_code != 200:
        ng("LoRA 创作提交", f"HTTP {r.status_code}: {r.text[:200]}")
    else:
        tid = r.json()["task_id"]
        st = wait_create(tid, timeout=900)
        if st.get("status") == "done":
            ok("LoRA 创作完成", f"rtf={st.get('rtf')} 用时含加载")
            (ok if st.get("lora") == RAP_LORA else ng)(
                "成品元数据带 lora 溯源", str(st.get("lora")))
            hist_rap = st.get("history_id")
            rec = find_history_by_id(hist_rap)
            txt = (rec or {}).get("text", "")
            (ok if "（RAP腔）" in txt else ng)("历史文本带（RAP腔）标", txt[:60])
            a = _req("GET", f"{HUB}{st['audio_url']}", timeout=60)
            (ok if a.status_code == 200 and len(a.content) > 200_000 else ng)(
                "LoRA 成品可播", f"{len(a.content)//1024}KB")
        else:
            ng("LoRA 创作完成", str(st)[:300])

    # ③ 批量魔改（专辑化）
    print("== ③ 批量魔改(一歌×N风格) ==")
    if hist_rap:
        r = _req("POST", f"{HUB}/api/song/remix_batch", json={
            "history_id": hist_rap,
            "styles": [{"style": "edm, dance, electronic, energetic, synth, 128 bpm",
                        "label": "电子舞曲"},
                       {"style": "folk, ballad, mandarin, soft vocal, acoustic guitar, 70 bpm",
                        "label": "抒情民谣"}],
            "remix_strength": 0.5, "quality": "turbo",
        }, timeout=30)
        if r.status_code != 200:
            ng("批量魔改提交", f"HTTP {r.status_code}: {r.text[:200]}")
        else:
            d = r.json()
            tasks = d.get("tasks") or []
            (ok if len(tasks) == 2 and not d.get("errors") else ng)(
                "两风格全部受理", json.dumps([t['label'] for t in tasks], ensure_ascii=False))
            names = {t["song_name"] for t in tasks}
            (ok if len(names) == 2 else ng)("成品名互不相同(防历史去重互吞)",
                                            json.dumps(list(names), ensure_ascii=False))
            hist_ids = []
            for t in tasks:
                st = wait_create(t["task_id"], timeout=900)
                if st.get("status") == "done" and st.get("history_id"):
                    hist_ids.append(st["history_id"])
                else:
                    ng(f"批量任务[{t['label']}]完成", str(st)[:200])
            if len(hist_ids) == 2:
                ok("两版全部出品入历史", str(hist_ids))
                recs = [find_history_by_id(h) for h in hist_ids]
                txts = [(r0 or {}).get("text", "") for r0 in recs]
                (ok if all(t0.startswith("[魔改]") for t0 in txts) and txts[0] != txts[1]
                 else ng)("历史两条各自留名([魔改]+不同后缀)",
                          " | ".join(t0[:40] for t0 in txts))
        # 上限护栏
        r = _req("POST", f"{HUB}/api/song/remix_batch", json={
            "history_id": hist_rap,
            "styles": [{"style": f"style{i}", "label": f"L{i}"} for i in range(7)],
        }, timeout=15)
        (ok if r.status_code == 400 and "最多 6" in r.text else ng)(
            ">6 风格上限人话拒", f"HTTP {r.status_code}")
    else:
        ng("批量魔改(无参考历史)")

    # ④ 点歌台全自动闭环（就绪自动第一首 → 播完自动 done）
    print("== ④ 点歌台全自动闭环 ==")
    vcam_up = False
    try:
        vcam_up = bool(_req("GET", "http://127.0.0.1:7870/health",
                            retries=1, timeout=4).json().get("ok"))
    except Exception:
        pass
    if not vcam_up:
        skip("点歌台自动上麦(vcam 不在线)", "广播中枢没起，装了 SplitCam/OBS 后重验")
    else:
        st0 = {}
        try:
            st0 = _req("GET", f"{HUB}/api/song/station", timeout=10).json()
        except Exception:
            pass
        # 优先用曲库真歌（P2 验收同款）：真人声对分离/SVC 管线才是真验；
        # 曲库空了才退合成音（可能因无人声被引擎如实拒，属环境缺料非功能缺陷）。
        real = SONGS_DIR / "圣诞快乐歌.mp3"
        synth = SONGS_DIR / "P6E2E自动电台.wav"
        test_song = real if real.exists() else synth
        rid = None
        try:
            if test_song is synth:
                make_tone_wav(test_song, seconds=8.0)
            # 清残留：此前验收中断留下的同名条目会触发防重复点歌拒（run4 实锤 #9 ready 挡路）
            for q in (st0.get("queue") or []):
                if q.get("file") == test_song.name and q.get("status") != "playing":
                    try:
                        _req("DELETE", f"{HUB}/api/song/station/{q['id']}", timeout=10)
                        print(f"  [i] 清掉残留点歌 #{q['id']}({q.get('status')})")
                    except Exception:
                        pass
            _req("POST", f"{HUB}/api/song/station/config", json={
                "enabled": True, "auto_prepare": True, "auto_play": True,
                "announce": False,
            }, timeout=10)
            rq = _req("POST", f"{HUB}/api/song/station/request", json={
                "file": test_song.name, "requester": "P6E2E自动化",
            }, timeout=15).json()
            rid = rq.get("id")
            (ok if rid else ng)("点歌入队", str(rq)[:120])
            # 全程无人点播：等它自己走完 queued→preparing→ready→playing→done
            saw_playing = False
            final_state = ""
            t0 = time.time()
            while time.time() - t0 < 900:   # 备歌走翻唱管线，中途可能被在线门禁的对话测试让路挂起

                s = _req("GET", f"{HUB}/api/song/station", timeout=15).json()
                me = next((q for q in s.get("queue", []) if q.get("id") == rid), None)
                if me:
                    if me.get("status") == "playing":
                        saw_playing = True
                    if me.get("status") in ("done", "failed"):
                        final_state = me.get("status")
                        break
                time.sleep(4.0)
            (ok if saw_playing else ng)("就绪后无人点播自动上麦(P6 冷启动第一首)")
            (ok if final_state == "done" else ng)(
                "播完自动收尾 done", final_state or "超时")
        finally:
            if rid:
                try:
                    _req("DELETE", f"{HUB}/api/song/station/{rid}", timeout=10)
                except Exception:
                    pass
            try:
                _req("POST", f"{HUB}/api/song/station/config", json={
                    "enabled": bool(st0.get("enabled", False)),
                    "auto_prepare": bool(st0.get("auto_prepare", True)),
                    "auto_play": bool(st0.get("auto_play", False)),
                    "announce": bool(st0.get("announce", False)),
                }, timeout=10)
            except Exception:
                pass
            if test_song is synth:
                test_song.unlink(missing_ok=True)

    print(f"\n结果: PASS {len(PASS)} / FAIL {len(FAIL)} / SKIP {len(SKIP)}")
    for f in FAIL:
        print("  FAIL -", f)
    print("RESULT:", "OK" if not FAIL else "FAIL")


if __name__ == "__main__":
    main()
