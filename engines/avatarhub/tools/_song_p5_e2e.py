# -*- coding: utf-8 -*-
"""Song-P5 真跑验收：任务持久化+后台对账 / 整曲MV异步 / F5 风格魔改 / 点歌台增强。

覆盖：
  ①能力探测：health.create.capabilities.remix 如实上报
  ②后台对账（核心承诺「浏览器关了歌照出」）：提交纯生成后**绝不轮询状态端点**，
    只看只读看板 /api/song/tasks（数据源=对账快照）——任务自己走完并入历史；
    同时 sqlite 直查 logs/song_tasks.db 写穿行 done=1
  ③整曲 MV 异步：hold 让路下提交不 409 而是 waiting 人话 → 放行 → 整曲出片可下载
  ④F5 魔改（纯）：remix_of=历史成品 → [魔改] 前缀入历史 → 成品可播
  ⑤F5 魔改+换声：svc_swap 叠加 → engine=ace_step+yingmusic_svc + 贴合度评分
  ⑥点歌台：礼物门槛（低于只谢不插队/达标插队）+ 今日点歌榜聚合

用法： python tools/_song_p5_e2e.py
"""
import io
import json
import sqlite3
import sys
import time
import wave
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"
ROOT = Path(__file__).resolve().parents[1]
STORE_DB = ROOT / "logs" / "song_tasks.db"
SONGS_DIR = ROOT / "songs"

PASS = []
FAIL = []


def _req(method, url, *, retries=6, backoff=5.0, **kw):
    """带瞬断重试（Hub 被看门狗拉起需 ~20s，期间连接拒绝不该砸死验收）。"""
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


STYLE = "pop, mandarin, female vocal, warm, acoustic guitar, 90 bpm, uplifting"
LYRICS = """[verse]
夜色慢慢降下来
屏幕还亮着白
一行一行写下来
梦想不打折卖

[chorus]
唱吧唱吧 就现在
这是自己的舞台
"""


def board_row(tid):
    try:
        d = _req("GET", f"{HUB}/api/song/tasks", timeout=15).json()
        for r in d.get("tasks", []):
            if r.get("tid") == tid:
                return r, d
    except Exception:
        pass
    return None, {}


def store_row(kind, tid):
    try:
        conn = sqlite3.connect(str(STORE_DB))
        row = conn.execute("SELECT data, done FROM song_tasks WHERE kind=? AND tid=?",
                           (kind, tid)).fetchone()
        conn.close()
        return row
    except Exception:
        return None


def wait_create(tid, timeout=900):
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
    """按 id 取历史记录（验证前缀/入库）。不走 search——它是 FTS5 MATCH，
    [魔改] 的方括号是其语法字符，拿最近列表按 id 匹配最省心。"""
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
    """验收前置：让路信号若来自「直播」，先判真伪。
    真直播（画面健康 ok/warmup/lag）→ 不打扰，验收直接中止；
    僵尸直播（noface/stalled/svc_down：自愈拉起的空转管线，没人上镜）→ 停掉再验。
    这是 run2 踩的坑：旧 Hub 自愈拉起 realtime_stream，让路协议如实生效，
    整轮 create/MV 全部排队 300s 超时——判据没错，是环境里挂着个空转直播。"""
    try:
        y = _req("GET", f"{HUB}/api/song/yield", timeout=10).json()
    except Exception:
        return True
    if not y.get("yield"):
        return True
    if y.get("source") != "live":
        print(f"  [!] 让路中({y.get('source')}): {y.get('reason')}——等待其自然结束")
        return True
    st = {}
    try:
        st = _req("GET", f"{HUB}/realtime/status", timeout=10).json()
    except Exception:
        pass
    health = (st.get("health") or {}).get("state", "")
    if st.get("video_running") and health in ("noface", "stalled", "svc_down"):
        print(f"  [!] 检测到僵尸直播(health={health}，无真人上镜)——停掉以免验收全程被让路挡死")
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
                    print("  [OK] 让路信号已清，验收继续")
                    return True
            except Exception:
                pass
        print("  [!] 30s 内让路未清")
        return False
    print(f"  [X] 真直播进行中(health={health or '未知'})：不打扰，验收中止。请下播后再跑。")
    return False


def main():
    # ⓪ 环境预检：僵尸直播清场（真直播则不打扰直接停测）
    print("== ⓪ 环境预检 ==")
    if not preflight_clear_zombie_live():
        print("RESULT: SKIP (直播占用)")
        return
    # ① 能力探测（含 remix）
    print("== ① 能力探测(含 remix) ==")
    h = _req("GET", f"{HUB}/api/song/health", timeout=10).json()
    c = h.get("create") or {}
    caps = c.get("capabilities") or {}
    if c.get("online") and caps.get("create"):
        ok("health.create 在线", f"engine={c.get('engine')}")
    else:
        ng("health.create 在线", str(c))
        print("引擎不在线，后续无法验收")
        return
    (ok if caps.get("remix") else ng)("remix 能力如实上报", str(caps))

    _req("POST", f"{HUB}/api/song/yield/hold", params={"on": False}, timeout=10)

    # ② 后台对账：提交后绝不碰状态端点，任务自己完成
    print("== ② 后台对账(不轮询状态端点，任务自走) ==")
    r = _req("POST", f"{HUB}/api/song/create", json={
        "style": STYLE, "lyrics": LYRICS, "duration_s": 30,
        "quality": "turbo", "svc_swap": False, "song_name": "P5E2E对账自走",
    }, timeout=30)
    if r.status_code != 200:
        ng("对账任务提交", f"HTTP {r.status_code}: {r.text[:200]}")
        return
    tid = r.json()["task_id"]
    row = store_row("create", tid)
    (ok if row is not None else ng)("提交即写穿 song_tasks.db", f"tid={tid}")
    # 只读看板轮询（不触发任何推进），等对账循环自己把任务送到 done
    hist_pure = None
    t0 = time.time()
    seen_open = False
    while time.time() - t0 < 300:
        b, full = board_row(tid)
        if b:
            if b.get("status") not in ("done", "error") and full.get("open", 0) >= 1:
                seen_open = True
            if b.get("status") == "done":
                hist_pure = b.get("history_id")
                break
            if b.get("status") == "error":
                break
        time.sleep(5.0)
    (ok if seen_open else ng)("看板对进行中任务可见(open>=1)")
    if hist_pure:
        ok("对账自走完成：无人轮询也入历史", f"hist={hist_pure} 用时{time.time()-t0:.0f}s")
    else:
        b, _ = board_row(tid)
        ng("对账自走完成", str(b)[:300])
    row2 = store_row("create", tid)
    (ok if row2 is not None and row2[1] == 1 else ng)(
        "终态写穿 done=1(重启回载不再重跑)", f"row={row2 and row2[1]}")

    # ③ 整曲 MV 异步（hold 下提交=waiting 而非 409；放行后整曲出片）
    print("== ③ 整曲 MV 异步 ==")
    if hist_pure:
        _req("POST", f"{HUB}/api/song/yield/hold",
             params={"on": True, "reason": "P5 e2e 演练"}, timeout=10)
        r = _req("POST", f"{HUB}/api/song/mv_task", json={
            "history_id": hist_pure, "seconds": 0,
        }, timeout=30)
        if r.status_code == 200:
            mtid = r.json()["task_id"]
            ok("hold 中提交不 409(异步排队语义)", f"tid={mtid}")
            time.sleep(8)
            st = _req("GET", f"{HUB}/api/song/mv_task/{mtid}", timeout=10).json()
            (ok if st.get("status") == "waiting" and "让路" in (st.get("detail") or "")
             else ng)("让路挂起人话可见", f"{st.get('status')}:{st.get('detail', '')[:50]}")
            _req("POST", f"{HUB}/api/song/yield/hold", params={"on": False}, timeout=10)
            t0 = time.time()
            fin = {}
            while time.time() - t0 < 600:
                fin = _req("GET", f"{HUB}/api/song/mv_task/{mtid}", timeout=15).json()
                if fin.get("status") in ("done", "error", "cancelled"):
                    break
                time.sleep(4.0)
            if fin.get("status") == "done" and fin.get("url"):
                secs = fin.get("seconds") or 0
                (ok if 25 <= secs <= 35 else ng)(
                    "整曲语义(30s 歌出 ~30s 片)", f"seconds={secs}")
                v = _req("GET", f"{HUB}{fin['url']}", timeout=120)
                (ok if v.status_code == 200 and len(v.content) > 200_000 else ng)(
                    "成片可下载", f"{len(v.content)//1024}KB 渲染{fin.get('elapsed_ms', 0)/1000:.0f}s")
            else:
                ng("整曲 MV 完成", str(fin)[:300])
        else:
            ng("整曲 MV 提交", f"HTTP {r.status_code}: {r.text[:200]}")
    else:
        ng("整曲 MV(无历史可用)")

    # ④ F5 魔改（纯）：同一首歌换编曲
    print("== ④ F5 风格魔改(纯) ==")
    hist_remix = None
    if hist_pure:
        r = _req("POST", f"{HUB}/api/song/create", json={
            "style": "rock, mandarin, electric guitar, powerful drums, 120 bpm",
            "lyrics": LYRICS, "duration_s": 30, "quality": "turbo",
            "svc_swap": False, "song_name": "P5E2E魔改",
            "remix_of": hist_pure, "remix_strength": 0.5,
        }, timeout=30)
        if r.status_code != 200:
            ng("魔改提交", f"HTTP {r.status_code}: {r.text[:200]}")
        else:
            rtid = r.json()["task_id"]
            st = wait_create(rtid, timeout=600)
            if st.get("status") == "done":
                ok("魔改完成", f"hist={st.get('history_id')} rtf={st.get('rtf')}")
                hist_remix = st.get("history_id")
                rec = find_history_by_id(hist_remix)
                (ok if rec and (rec.get("text") or "").startswith("[魔改]") else ng)(
                    "[魔改] 前缀入历史", (rec or {}).get("text", "")[:50])
                a = _req("GET", f"{HUB}{st['audio_url']}", timeout=60)
                (ok if a.status_code == 200 and len(a.content) > 200_000 else ng)(
                    "魔改成品可播", f"{len(a.content)//1024}KB")
                b, _ = board_row(rtid)
                (ok if b and b.get("kind") == "remix" else ng)(
                    "看板 kind=remix 区分", str(b and b.get("kind")))
            else:
                ng("魔改完成", str(st)[:300])
    else:
        ng("魔改(无参考历史)")

    # ⑤ F5 魔改+换声（双维叠加）
    print("== ⑤ F5 魔改+角色声(双维叠加) ==")
    if hist_pure:
        r = _req("POST", f"{HUB}/api/song/create", json={
            "style": "folk, ballad, mandarin, soft vocal, acoustic guitar, 70 bpm",
            "lyrics": LYRICS, "duration_s": 30, "quality": "turbo",
            "svc_swap": True, "song_name": "P5E2E魔改换声",
            "remix_of": hist_pure, "remix_strength": 0.4,
        }, timeout=30)
        if r.status_code != 200:
            d = ""
            try:
                d = r.json().get("detail", "")
            except Exception:
                pass
            ng("魔改换声提交", f"HTTP {r.status_code} {d[:120]}")
        else:
            wtid = r.json()["task_id"]
            st = wait_create(wtid, timeout=1200)
            if st.get("status") == "done":
                (ok if st.get("engine_used") == "ace_step+yingmusic_svc" else ng)(
                    "引擎标注双引擎", st.get("engine_used", ""))
                (ok if st.get("similarity") else ng)(
                    "换声版有贴合度", str(st.get("similarity")))
                rec = find_history_by_id(st.get("history_id"))
                (ok if rec and (rec.get("text") or "").startswith("[魔改]") else ng)(
                    "[魔改] 前缀透传到换声段", (rec or {}).get("text", "")[:50])
            else:
                ng("魔改换声完成", str(st)[:300])
    else:
        ng("魔改换声(无参考历史)")

    # ⑥ 点歌台：礼物门槛 + 点歌榜
    print("== ⑥ 点歌台礼物门槛+点歌榜 ==")
    st0 = {}
    try:
        st0 = _req("GET", f"{HUB}/api/song/station", timeout=10).json()
    except Exception:
        pass
    test_song = SONGS_DIR / "P5E2E测试曲.wav"
    rid = None
    try:
        # 2 秒静音 wav 入曲库（礼物点歌需要曲库匹配；auto_prepare 关掉避免真备歌）
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 32000)
        test_song.write_bytes(buf.getvalue())
        _req("POST", f"{HUB}/api/song/station/config", json={
            "enabled": True, "auto_prepare": False, "gift_min_value": 50,
        }, timeout=10)
        g1 = _req("POST", f"{HUB}/api/song/station/gift", json={
            "name": "P5土豪甲", "gift": "小心心", "value": 10, "song": "P5E2E测试曲",
        }, timeout=10).json()
        (ok if g1.get("ok") and not g1.get("topped") and "50" in g1.get("message", "")
         else ng)("低于门槛：入队但不插队+人话说明", str(g1)[:120])
        rid = g1.get("id")
        g2 = _req("POST", f"{HUB}/api/song/station/gift", json={
            "name": "P5土豪甲", "gift": "火箭", "value": 100, "song": "P5E2E测试曲",
        }, timeout=10).json()
        (ok if g2.get("ok") and g2.get("topped") else ng)(
            "达标礼物：插队到队首", str(g2)[:120])
        lb = _req("GET", f"{HUB}/api/song/station/leaderboard",
                  params={"days": 1}, timeout=10).json()
        me = next((b for b in lb.get("board", []) if b["name"] == "P5土豪甲"), None)
        (ok if me and me.get("gift_value", 0) >= 110 else ng)(
            "点歌榜聚合(点歌数+礼物价值)", str(me))
    finally:
        # 清理：删点歌 → 还原配置 → 删测试曲
        if rid:
            try:
                _req("POST", f"{HUB}/api/song/station/{rid}/cancel", timeout=10)
                _req("DELETE", f"{HUB}/api/song/station/{rid}", timeout=10)
            except Exception:
                pass
        try:
            _req("POST", f"{HUB}/api/song/station/config", json={
                "enabled": bool(st0.get("enabled", False)),
                "auto_prepare": bool(st0.get("auto_prepare", True)),
                "gift_min_value": float(st0.get("gift_min_value", 0) or 0),
            }, timeout=10)
        except Exception:
            pass
        test_song.unlink(missing_ok=True)

    print(f"\n结果: PASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print("  FAIL -", f)
    print("RESULT:", "OK" if not FAIL else "FAIL")


if __name__ == "__main__":
    main()
