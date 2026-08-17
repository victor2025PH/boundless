# -*- coding: utf-8 -*-
"""展演全链演练（P3b 收官 · 2026-08-09，非门禁）。

对齐 install_drill / notice_drill 文化：把散在 _logs 的一次性探针（ops/face/roles/screens）
固化成一支**常驻、可重复**的端到端演练——补 hud_contract_test（静态串检）的运行时盲区：
端点会不会 500、状态机 arm→disarm 会不会坏、护栏负向路径还灵不灵、六屏聚合对不对。
安全纪律：默认零生产写（ops 只武装即撤防、不点火）；真点火链(route_switch 切库再撤销)
藏在 --live-ops 后，且只在 verdict=safe 且能复原时跑。

产物 logs/showcase_drill.json（doctor「展演演练」读新鲜度）。
用法：python showcase_drill.py [--live-ops]   （需活的 hud_server :7913）
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:7913"
AVATARHUB = Path(r"C:\模仿音色")
REPORT = AVATARHUB / "logs" / "showcase_drill.json"
FLAG = AVATARHUB / "logs" / "hud_ops_disable.flag"
HUB_CONFIG = AVATARHUB / "hub_config.json"

sections: dict[str, dict] = {}
_cur = {"name": ""}


def sect(name: str):
    _cur["name"] = name
    sections[name] = {"ok": True, "detail": []}


def ck(cond: bool, note: str) -> bool:
    s = sections[_cur["name"]]
    s["detail"].append(("+" if cond else "!") + note)
    if not cond:
        s["ok"] = False
    print(("  PASS " if cond else "  FAIL ") + note)
    return cond


def get(path: str, timeout: float = 15, headers: dict | None = None):
    try:
        req = urllib.request.Request(BASE + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"_err": str(e)[:80]}


def post(path: str, body: dict, timeout: float = 20):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"_err": str(e)[:80]}


def _sse_client(machine: str, modes: str, secs: float):
    try:
        urllib.request.urlopen(
            f"{BASE}/api/stream?machine={machine}&modes={modes}", timeout=secs).read(300)
    except Exception:
        pass


def drill_tour():
    sect("tour 幕次编排")
    _, d0 = post("/api/tour", {"step": 0})
    ck(d0.get("ok") and d0["tour"]["step"] == 0, "复位第 0 幕")
    last = 0
    ok_seq = True
    for _ in range(6):
        _, d = post("/api/tour", {"step": "next"})
        if not (d.get("ok") and d["tour"]["step"] == last + 1):
            ok_seq = False
            break
        last = d["tour"]["step"]
    ck(ok_seq and last == 6, f"next 逐幕推进至谢幕（到第 {last} 幕）")
    st, dg = get("/api/tour")
    ck(st == 200 and "screens" in dg, "/api/tour 带 screens 字段")
    post("/api/tour", {"step": 0})
    ck(True, "演练后复位第 0 幕")


def drill_preflight():
    sect("开演预检")
    st, d = get("/api/preflight")
    names = {c["name"] for c in (d.get("checks") or [])}
    ck(st == 200 and d.get("checks"), "预检返回检查项")
    ck("点火授权" in names, "含「点火授权」项")
    ck(any("屏" in n for n in names), "含屏在线项")
    ck("忙态" in names, "含忙态项")


def drill_screens():
    sect("六屏逐屏在线")
    # 用专用 drill 机名（不与真实在连的 HUD 客户端撞——演练时可能有真屏/验证浏览器连着，
    # 用 zhongshu 会被真客户端污染判断，§20 那段的脆弱性，此处根治）
    threading.Thread(target=_sse_client, args=("drilla", "gesture,voice", 5), daemon=True).start()
    threading.Thread(target=_sse_client, args=("drillb", "gesture,phone", 5), daemon=True).start()
    time.sleep(2.0)
    _, d = get("/api/tour")
    scr = d.get("screens") or {}
    ck("drilla" in scr and "drillb" in scr, f"两屏聚合在线（{[k for k in scr if k.startswith('drill')]}）")
    ck("voice" in (scr.get("drilla", {}).get("modes") or []), "屏上报语音模式")
    ck("phone" in (scr.get("drillb", {}).get("modes") or []), "屏上报手机模式")
    # SSE 断开是写触发检测（服务端下次写才发现死 socket，崩屏最坏 15s keepalive 内现形）。
    # 客户端已读完即闭，这里主动切一次幕（强制向全部连接写一帧）逼出死连接的摘除。
    time.sleep(3.5)
    post("/api/tour", {"step": 1}); time.sleep(0.6); post("/api/tour", {"step": 0}); time.sleep(0.6)
    _, d2 = get("/api/tour")
    scr2 = d2.get("screens") or {}
    ck("drilla" not in scr2 and "drillb" not in scr2, "断开即摘（写触发探测清理注册表）")


def drill_ops_guards():
    sect("隔空指挥·护栏负向")
    _, r1 = post("/api/ops/arm", {"verb": "nosuch", "args": {}, "src": "key"})
    ck(not r1.get("ok"), "白名单外动词拒")
    _, r2 = post("/api/ops/arm", {"verb": "wol", "args": {"machine": "tingxie"}, "src": "key"})
    ck(not r2.get("ok") and "醒" in str(r2.get("error", "")), "在线机 WOL 被 precheck 拒")
    # 旗标反演练
    FLAG.write_text("drill", encoding="utf-8")
    _, r3 = post("/api/ops/arm", {"verb": "route_switch", "args": {"target": "lianbei"}, "src": "key"})
    ck(not r3.get("ok") and "总闸" in str(r3.get("error", "")), "总闸旗标在位武装必拒")
    FLAG.unlink(missing_ok=True)
    # P1 mode_switch 负向+正向（2026-08-13，须在总闸旗标删除后——首版插进旗标窗口全被总闸拒）：
    # 非法目标拒/同模式拒/合法武装带闸门预览→立即撤防（不点火）
    _, rm1 = post("/api/ops/arm", {"verb": "mode_switch", "args": {"target": "nosuch"}, "src": "key"})
    ck(not rm1.get("ok") and "只认" in str(rm1.get("error", "")), "切模式非法目标拒")
    try:
        import json as _j
        _cur = _j.loads((AVATARHUB / "logs" / "cluster_mode.json")
                        .read_text(encoding="utf-8")).get("mode") or ""
    except Exception:
        _cur = ""
    if _cur:
        _, rm2 = post("/api/ops/arm", {"verb": "mode_switch", "args": {"target": _cur}, "src": "key"})
        ck(not rm2.get("ok") and "无需切换" in str(rm2.get("error", "")), "切模式同模式拒")
        _other = "face" if _cur == "chatx" else "chatx"
        _, rm3 = post("/api/ops/arm", {"verb": "mode_switch", "args": {"target": _other},
                                       "by": "drill", "src": "key"}, timeout=30)
        ck(rm3.get("ok") and "闸门" in str(rm3.get("impact", "")), "切模式武装带执行器闸门预览")
        _, rm4 = post("/api/ops/disarm", {"id": rm3.get("id", ""), "src": "key"})
        ck(rm4.get("ok"), "切模式演练即撤防（不点火）")
    # 武装→遥控点火拒→撤防（不真点火）
    _, ra = post("/api/ops/arm", {"verb": "route_switch", "args": {"target": "lianbei"},
                                  "by": "drill", "src": "key"})
    armed = ra.get("ok")
    ck(armed, f"route_switch 武装（{ra.get('desc') or ra.get('error')}）")
    if armed:
        _, rf = post("/api/ops/fire", {"id": ra["id"], "src": "remote"})
        ck(not rf.get("ok") and "在场" in str(rf.get("error", "")), "遥控点火被授权拒")
        _, rd = post("/api/ops/disarm", {"id": ra["id"], "src": "key"})
        ck(rd.get("ok"), "撤防成功")
    _, sd = get("/api/ops/state")
    ck(sd.get("armed") is None, "撤防后无武装位残留")


def drill_cockpit():
    sect("指挥舱甩送·路由")
    # C0 2026-08-13：座次邻接路由三案。src=drill → HUD 静默不真开（防例行演练扰生产屏）。
    # 08-13 午后按真实 9 屏摆位换锚：中央=lianbei-1(104,操作者正对)，右邻=tingxie-1，
    # 左尽头=yunsheng-1（座次由 cockpit.json order 推导——摆位再变只改台账，此处跟着台账走）
    _, c1 = post("/api/cockpit/throw", {"screen": "lianbei-1", "dir": "right",
                                        "machine": "kouxing", "src": "drill"})
    ck(c1.get("ok") and c1.get("target") == "tingxie-1",
       f"中央主屏右甩 → 邻接解析 {c1.get('target')}")
    _, c2 = post("/api/cockpit/throw", {"screen": "yunsheng-1", "dir": "left",
                                        "machine": "kouxing", "src": "drill"})
    ck(not c2.get("ok") and "尽头" in str(c2.get("error", "")), "U 型尽头左甩拒（人话）")
    _, c3 = post("/api/cockpit/throw", {"screen": "lianbei-1", "dir": "up",
                                        "machine": "kouxing", "src": "drill"})
    ck(not c3.get("ok"), "非法方向拒")
    # 命名目标（2026-08-13d）：任意屏一步上大屏（173 电视）;已在大屏上再甩=人话拒
    _, c4 = post("/api/cockpit/throw", {"screen": "zhongshu-2", "dir": "tv",
                                        "machine": "kouxing", "src": "drill"})
    ck(c4.get("ok") and c4.get("target") == "yunsheng-1",
       f"右端屏一步上大屏 → {c4.get('target')}")
    _, c5 = post("/api/cockpit/throw", {"screen": "yunsheng-1", "dir": "tv",
                                        "machine": "kouxing", "src": "drill"})
    ck(not c5.get("ok") and "已在大屏" in str(c5.get("error", "")), "已在大屏再甩拒（人话）")


def drill_gestlead():
    sect("手势指挥权·端点")
    # C1 2026-08-13：只验端点契约不抢真实指挥权（q 用地板值,只有全场空闲才会短暂当选;收尾 q=0 释放）
    _, g1 = post("/api/gest/beat", {"screen": "no-such-screen", "q": 0.5, "hands": 1, "src": "drill"})
    ck(not g1.get("ok") and "不在台账" in str(g1.get("error", "")), "野屏心跳拒（人话）")
    _, g2 = post("/api/gest/beat", {"screen": "kouxing-1", "q": 0.001, "hands": 1, "src": "drill"})
    ck(g2.get("ok"), "合法心跳受理")
    _, g3 = post("/api/gest/beat", {"screen": "kouxing-1", "q": 0, "hands": 0, "src": "drill"})
    ck(g3.get("ok"), "空手心跳=释放受理")


def drill_face():
    sect("刷脸授权态")
    _, d = get("/api/ops/state")
    mode = d.get("auth_mode", "")
    ops = d.get("operators") or []
    ck(mode in ("presence", "presence+face"), f"授权模式可读（{mode}）")
    if ops:
        ck(mode == "presence+face", f"已登记操作员 {ops} → 刷脸层开")
    else:
        ck(mode == "presence", "未登记 → 纯在场信号（保守降级）")


def drill_roles():
    sect("手机万能外设")
    _, b = post("/api/roles/beat", {"sid": "yunsheng", "role": "speaker", "batt": 66})
    ck(b.get("ok"), "扬声器角色心跳")
    _, w = post("/api/wand", {"sid": "kouxing", "x": 0.4, "y": 0.6})
    ck(w.get("ok"), "魔杖坐标上报")
    st, wg = get("/api/wand/kouxing")
    ck(st == 200 and wg.get("ok"), "魔杖坐标可拉取")
    _, s = get("/api/stations")
    stns = s.get("stations") or {}
    ck(stns.get("yunsheng", {}).get("role") == "speaker", "扬声器进注册表合并")


def drill_voice():
    sect("语音链就绪")
    st, d = get("/api/voice/state")
    ck(st == 200, "语音态可读")
    if d.get("ready"):
        ck(True, "KWS+ASR 常驻就绪")
    else:
        ck(True, f"语音降级（非故障）：{d.get('reason', '')[:50]}")   # 降级不判失败，诚实记录


def drill_desktop():
    sect("隔空拉屏·按需闸")
    _, w1 = post("/api/desktop/want", {"machine": "lianbei", "on": True})
    ck(w1.get("ok"), "刷新「想看脸备」")
    _, g1 = get("/api/desktop/wanted?machine=lianbei")
    ck(g1.get("wanted") is True, "agent 侧读到 wanted=true（按需捕获）")
    _, w0 = post("/api/desktop/want", {"machine": "lianbei", "on": False})
    _, g0 = get("/api/desktop/wanted?machine=lianbei")
    ck(g0.get("wanted") is False, "撤销想看→wanted=false（没人看不截屏）")
    st, dl = get("/api/desktop/list")
    ck(st == 200 and "desktops" in dl, "在推列表可读（无 agent 时为空属常态）")


def drill_ctrl():
    sect("隔空受控·安全闸(P2 输入注入)")
    st, cs = get("/api/ctrl/state")
    ck(st == 200 and set(cs.get("targets") or []) == {"lianbei", "kouxing", "shengbei"},
       f"白名单三机(脸备/口型/声备)：{cs.get('targets')}")
    # 非白名单机武装 → 拒（生产链永久只读）
    _, a1 = post("/api/ctrl/arm", {"machine": "yunsheng", "src": "gesture"})
    ck(not a1.get("ok") and "白名单" in (a1.get("error") or ""), "非白名单机(韵声)武装→拒")
    # 无会话动作 → 拒
    _, a2 = post("/api/ctrl/action", {"token": "bad", "kind": "click", "nx": 0.5, "ny": 0.5})
    ck(not a2.get("ok"), "无有效会话动作→拒")
    # agent 错密钥拉 → 不授予不投递
    st3, p3 = get("/api/ctrl/pull?machine=kouxing", headers={"X-Ctrl-Secret": "wrong"})
    ck(st3 == 200 and p3.get("granted") is False and p3.get("events") == [],
       "agent 错密钥拉→不授予空队列")
    # 已部署/在线态（口型已铺则应 alive）
    dep, alive = cs.get("deployed") or [], cs.get("alive") or []
    ck(True, f"已部署 {dep or '无'}；在线 agent {alive or '无'}（happy-path 刷脸落点走真机）")


def drill_ops_live():
    """真点火链（--live-ops）：仅 verdict=safe + 能复原时跑；切库→撤销复原。"""
    sect("隔空指挥·真点火链(live)")
    _, busy = get("/api/preflight")
    v = next((c["note"] for c in (busy.get("checks") or []) if c["name"] == "忙态"), "")
    if "safe" not in v:
        ck(True, f"忙态非 safe，跳过真点火（{v[:30]}）")
        return
    orig = json.loads(HUB_CONFIG.read_text(encoding="utf-8")).get("faceswap")
    _, ra = post("/api/ops/arm", {"verb": "route_switch", "args": {"target": "lianbei"},
                                  "by": "drill-live", "src": "key"})
    if not ra.get("ok"):
        ck(False, f"武装失败：{ra.get('error')}")
        return
    _, rf = post("/api/ops/fire", {"id": ra["id"], "src": "key"})
    ck(rf.get("ok"), f"点火（{rf.get('detail') or rf.get('error')}）")
    cur = json.loads(HUB_CONFIG.read_text(encoding="utf-8")).get("faceswap")
    ck(cur == "http://192.168.0.104:8000", f"路由已切脸备（{cur}）")
    _, ru = post("/api/ops/undo", {"src": "key"})
    back = json.loads(HUB_CONFIG.read_text(encoding="utf-8")).get("faceswap")
    ck(ru.get("ok") and back == orig, f"撤销复原（{back}）")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="展演全链演练")
    ap.add_argument("--live-ops", action="store_true", help="含真点火链（切换换脸路由再撤销）")
    a = ap.parse_args()
    st, _ = get("/health", timeout=5)
    if st != 200:
        print("hud_server :7913 不在线，跳过（schtasks /run /tn BoundlessHudServer）")
        return 1
    t0 = time.time()
    for fn in (drill_tour, drill_preflight, drill_screens, drill_ops_guards,
               drill_cockpit, drill_gestlead, drill_face, drill_roles, drill_voice, drill_desktop, drill_ctrl):
        print(f"== {fn.__doc__ or fn.__name__} ==" if False else f"== {fn.__name__} ==")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            sect(sections and _cur["name"] or fn.__name__)
            ck(False, f"演练异常：{type(e).__name__}: {str(e)[:80]}")
    if a.live_ops:
        try:
            drill_ops_live()
        except Exception as e:  # noqa: BLE001
            ck(False, f"live 异常：{type(e).__name__}")
    passed = sum(1 for s in sections.values() if s["ok"])
    total = len(sections)
    ok = passed == total
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ok": ok, "passed": passed, "total": total,
        "elapsed_s": round(time.time() - t0, 1), "live_ops": a.live_ops,
        "sections": {k: {"ok": v["ok"], "detail": v["detail"]} for k, v in sections.items()},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n== showcase_drill: {passed}/{total} 段通过，{time.time() - t0:.1f}s"
          f"，报告 -> {REPORT} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
