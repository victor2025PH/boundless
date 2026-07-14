# -*- coding: utf-8 -*-
"""P9-3/P10 交互级前端冒烟：真点开播页按钮，断言埋点与后端调用真的发出。

覆盖面：A/B 建议条（采纳/婉拒）· C/D/E 自救卡（再试/重启/真相收敛/成功 toast）·
F/G 零 pageerror + x-for key 撞车哨兵 · H 试音（请求→状态→持久化→就绪度转绿）·
I 试听（直播声卡回环同链路）· J 设备缺席自动热切开关（勾选→heal POST 双向）。

与 _fe_smoke（零 pageerror）互补：门禁静态断言只保证「接线存在」，这里保证「点了真会响」——
@click 表达式打错字/状态逻辑写反，这层才能兜住。

隔离设计：页面从真 hub 加载（/ui），但所有**写路径**全部网络层拦截并 mock——
  POST /api/metrics/devflow（埋点）/ POST /api/heal/config（开关）/ POST /rvc/hot_switch / POST /rvc/start
  GET  /api/audio/mic_test（真录 3 秒麦）/ GET /api/audio/output_test（真放提示音）——后两个是读语义的
  真硬件动作，无头环境没有麦/回环可用，且门禁不该指望房间安静，所以同样 mock。
真实 devflow 账本、heal_config、RVC、音频硬件全程零触碰；GET /api/metrics/devflow 也 mock（控制建议条初始不出现）。
状态注入走 Alpine.$data（与真实用户路径的差异仅在「谁把状态摆上去」，点击链路 100% 真实）。
"""
import json
import os
import sys
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from playwright.sync_api import sync_playwright

HUB = os.environ.get("ACCEPT_HUB", "http://127.0.0.1:9000")
FAILS = []
TOTAL = [0]


def chk(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(detail)[:160]) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    rec = {"devflow": [], "heal": [], "hot": [], "start": [], "mic": [], "out": []}
    errs = []

    def route_devflow(route):
        if route.request.method == "POST":
            try:
                rec["devflow"].append(route.request.post_data_json or {})
            except Exception:
                rec["devflow"].append({})
            route.fulfill(json=({"ok": True}))
        else:   # GET：建议条初始不出现（测试自己注入），advice 读数给空
            route.fulfill(json={"ok": True, "funnel": {}, "advice": {},
                                "auto_advice": {"suggest": False, "reason": "样本不足"}})

    def route_heal(route):
        if route.request.method == "POST":
            body = {}
            try:
                body = route.request.post_data_json or {}
            except Exception:
                pass
            rec["heal"].append(body)
            route.fulfill(json={"ok": True, "autoheal": False, "alerts": False,
                                "dev_autoswitch": bool(body.get("dev_autoswitch"))})
        else:
            route.fulfill(json={"ok": True, "autoheal": False, "alerts": False, "dev_autoswitch": False})

    def route_hot(route):
        try:
            rec["hot"].append(route.request.post_data_json or {})
        except Exception:
            rec["hot"].append({})
        route.fulfill(json={"ok": True, "was_running": True, "started": True,
                            "input": "麦X (MME)", "input_label": "麦X",
                            "output": "CABLE Input (MME)", "output_label": "直播声卡",
                            "elapsed_s": 1.0})

    def route_start(route):
        rec["start"].append(True)
        route.fulfill(json={"message": "Audio conversion started"})

    def route_mic(route):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
        rec["mic"].append({"device": (q.get("device") or [""])[0], "secs": (q.get("secs") or [""])[0]})
        route.fulfill(json={"ok": True, "level": "good", "verdict": "收音正常（峰值 -18dB · 底噪 -62dB）",
                            "peak_dbfs": -18, "noise_dbfs": -62, "wav_b64": ""})

    def route_out(route):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
        rec["out"].append({"device": (q.get("device") or [""])[0]})
        route.fulfill(json={"ok": True, "device": (q.get("device") or [""])[0],
                            "probe": {"heard": True, "peak_dbfs": -12.5}})

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context()
        # 引导/导览全部预熄灯：本测试只关心设备条区交互
        ctx.add_init_script("""try{
          localStorage.setItem('hub_stream_guide_done','1');
          localStorage.setItem('avatarhub_seen_tour','1');
          localStorage.setItem('ah_onboard_v1','1');
          localStorage.setItem('hub_seen_staticmouth_hint','1');
          localStorage.setItem('hub_stream_simple','1');
          localStorage.removeItem('hub_autosw_hint_done');
          localStorage.removeItem('hub_mic_test');
          localStorage.removeItem('hub_out_test');
        }catch(_){}""")
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(getattr(e, "stack", None) or str(e)))
        # P9 实战教训：设备名被 PortAudio 截断后可能重名 → x-for :key 撞车 → Alpine 崩渲染。
        # 双探测：console 的 Alpine 警告 + 数据层去重断言（见 G 组），任一命中都判 FAIL。
        keydups = []
        pg.on("console", lambda m: keydups.append(m.text[:120])
              if ("Duplicate key on x-for" in m.text or 'x-for ":key" is undefined' in m.text) else None)
        pg.route("**/api/metrics/devflow", route_devflow)
        pg.route("**/api/heal/config", route_heal)
        pg.route("**/rvc/hot_switch", route_hot)
        pg.route("**/rvc/start", route_start)
        pg.route("**/api/audio/mic_test*", route_mic)
        pg.route("**/api/audio/output_test*", route_out)

        pg.set_default_timeout(8000)   # 快败：卡 30s 说明接线已断，没必要陪跑
        pg.goto(HUB + "/ui", wait_until="domcontentloaded", timeout=25000)
        pg.wait_for_function("() => window.Alpine && document.querySelector('[x-data]')", timeout=15000)
        pg.wait_for_timeout(1200)

        def ev(js):
            return pg.evaluate("() => { const d=Alpine.$data(document.querySelector('[x-data]')); return (%s); }" % js)

        ev("d.tab='stream', d.streamSimple=true, 1")
        # 基线确定化：先手动喂一轮空基线（真轮询何时到无所谓，后续注入都算「新事件」）
        ev("d._autoSwBaselined || d._autoSwNotice(null,false), 1")
        pg.wait_for_timeout(300)

        # ── A 建议条 · 开启路径 ──────────────────────────────────
        ev("d.autoSwAdvice={suggest:true, reason:'测试:你已手动热切 3 次、成功率 100%'}, 1")
        strip = pg.locator("button:visible", has_text="开启自动热切").first
        chk("A1 建议条可见(开启按钮)", strip.count() > 0 and strip.is_visible())
        strip.click()
        pg.wait_for_timeout(500)
        chk("A2 采纳埋点 advice_enable 已发出", any(r.get("ev") == "advice_enable" for r in rec["devflow"]),
            rec["devflow"])
        chk("A3 heal POST dev_autoswitch=true", any(h.get("dev_autoswitch") is True for h in rec["heal"]),
            rec["heal"])
        a4 = ev("d.autoSwAdvice===null && d.autoSwHintDone===true && localStorage.getItem('hub_autosw_hint_done')==='1'")
        chk("A4 建议条退场+永久标记", bool(a4))
        a5 = ev("d.toasts.map(t=>t.msg).join('|')")
        chk("A5 开启成功 toast", "已开启设备缺席自动热切" in (a5 or ""), a5)

        # ── B 建议条 · 婉拒路径 ──────────────────────────────────
        ev("d.toasts=[], localStorage.removeItem('hub_autosw_hint_done'), d.autoSwHintDone=false,"
           " d.devAutoSwOn=false, d.autoSwAdvice={suggest:true, reason:'测试2'}, 1")
        pg.wait_for_timeout(200)
        pg.locator("button:visible", has_text="不再提示").first.click()
        pg.wait_for_timeout(400)
        chk("B1 婉拒埋点 advice_dismiss 已发出", any(r.get("ev") == "advice_dismiss" for r in rec["devflow"]))
        chk("B2 婉拒不碰开关(heal POST 仍只 1 次)", len(rec["heal"]) == 1, rec["heal"])
        chk("B3 建议条退场", bool(ev("d.autoSwAdvice===null")))

        # ── C 自救卡 · 失败进场 + 再试热切 ───────────────────────
        ev("d._autoSwNotice({ts:9001, ok:false, detail:'测试:PortAudio 打不开', n:1}, false), 1")
        pg.wait_for_timeout(200)
        chk("C1 失败事件挂自救卡", bool(ev("!!d.autoSwFail")))
        c1t = ev("d.toasts.map(t=>t.msg).join('|')")
        chk("C2 失败 toast(新事件才弹)", "自动热切失败" in (c1t or ""), c1t)
        chk("C3 自救卡曝光埋点 expose:rescue", any(r.get("ev") == "expose" and r.get("src") == "rescue"
                                                   for r in rec["devflow"]))
        ev("d.toasts=[], 1")   # toast 悬浮层会挡按钮点击，清掉再点（不 force-click，保住真实命中检测）
        pg.wait_for_timeout(150)
        pg.locator("button:visible", has_text="再试一次热切").first.click()
        pg.wait_for_timeout(600)
        chk("C4 热切请求 src=rescue", any(h.get("src") == "rescue" for h in rec["hot"]), rec["hot"])
        chk("C5 漏斗 click+ok:rescue", any(r.get("ev") == "click" and r.get("src") == "rescue" for r in rec["devflow"])
            and any(r.get("ev") == "ok" and r.get("src") == "rescue" for r in rec["devflow"]))
        chk("C6 切换成功→自救卡退场", bool(ev("d.autoSwFail===null")))

        # ── D 自救卡 · 重启变声 + 按真相收敛 ─────────────────────
        ev("d._autoSwNotice({ts:9002, ok:false, detail:'测试:二连击', n:2}, false), 1")
        pg.wait_for_timeout(200)
        ev("d.toasts=[], 1")
        pg.wait_for_timeout(150)
        pg.locator("button:visible", has_text="直接重启变声").first.click()
        pg.wait_for_timeout(500)
        chk("D1 重启变声请求已发出", len(rec["start"]) >= 1)
        chk("D2 重启后卡不乐观退场(等真相)", bool(ev("!!d.autoSwFail")))
        ev("d._autoSwNotice(null, true), 1")   # 顺风车看到转换在跑 → 危机解除
        chk("D3 rvc_conv=true → 卡自动退场", bool(ev("d.autoSwFail===null")))

        # ── E 成功事件 toast（P7-1 即时感知）────────────────────
        ev("d._autoSwNotice({ts:9003, ok:true, to:'麦Y', out:'声卡Z', n:2}, true), 1")
        e1 = ev("d.toasts.map(t=>t.msg).join('|')")
        chk("E1 自动热切成功 toast", "已自动热切到" in (e1 or "") and "麦Y" in (e1 or ""), e1)

        # ── H 试音链路：请求→结论→持久化→就绪度转绿（P2-2 全链）──
        # 就绪度里的试音行只在「枚举到麦克风」后才存在；真实枚举走 RVC/本地兜底，可能比 A~E 组慢——先等到位
        dev_ready = True
        try:
            pg.wait_for_function(
                "() => Alpine.$data(document.querySelector('[x-data]')).rvcInputDevices.length>0",
                timeout=20000)
        except Exception:
            dev_ready = False
        chk("H0 设备枚举回填(试音行存在前提)", dev_ready, ev("d.rvcInputDevices.length"))
        h0 = ev("(d.preflight().items.find(i=>i.key==='mictest')||{}).status")
        chk("H1 试音前就绪度=warn(还没试过)", h0 == "warn", h0)
        mic_dev = ev("d.audioInput||d.rvc.inputDevice||''")
        ev("d.toasts=[], 1")
        pg.wait_for_timeout(150)
        pg.locator("button:visible", has_text="试音").first.click()
        pg.wait_for_timeout(500)
        chk("H2 试音请求真发出(secs=3+当前设备)", len(rec["mic"]) >= 1 and rec["mic"][0]["secs"] == "3"
            and rec["mic"][0]["device"] == mic_dev, rec["mic"])
        chk("H3 结论落状态(good)", bool(ev("d.micTest.res && d.micTest.res.ok && d.micTest.res.level==='good'")))
        h4 = ev("(function(){ try{ const r=JSON.parse(localStorage.getItem('hub_mic_test')||'null');"
                " return r && r.level==='good' && r.device===(d.audioInput||d.rvc.inputDevice||'')"
                " && d.micTestJudge().state==='good'; }catch(_){ return false; } })()")
        chk("H4 结论持久化+7天裁决=good", bool(h4))
        h5 = ev("(d.preflight().items.find(i=>i.key==='mictest')||{}).status")
        chk("H5 就绪度试音项转绿", h5 == "ok", h5)
        # 失败路径不污染已存结论：设备被占用等失败只进当次条子，7 天 good 裁决与就绪度绿灯保持
        pg.unroute("**/api/audio/mic_test*")
        pg.route("**/api/audio/mic_test*",
                 lambda r: r.fulfill(json={"ok": False, "detail": "设备被占用(测试注入)"}))
        ev("d.toasts=[], 1")
        pg.wait_for_timeout(150)
        pg.locator("button:visible", has_text="试音").first.click()
        pg.wait_for_timeout(500)
        chk("H6 失败结论落当次状态", bool(ev("d.micTest.res && d.micTest.res.ok===false")))
        h7 = ev("d.micTestJudge().state==='good' &&"
                " (d.preflight().items.find(i=>i.key==='mictest')||{}).status==='ok'")
        chk("H7 失败不clobber已存good裁决(就绪度仍绿)", bool(h7))

        # ── I 试听链路：直播声卡回环 heard → 就绪度转绿（P3-2 全链）──
        out_dev = ev("d.audioOutput||d.rvc.outputDevice||''")
        ev("d.toasts=[], 1")
        pg.wait_for_timeout(150)
        pg.locator("button:visible", has_text="试听").first.click()
        pg.wait_for_timeout(500)
        chk("I1 试听请求真发出(当前输出)", len(rec["out"]) >= 1 and rec["out"][0]["device"] == out_dev, rec["out"])
        chk("I2 回环结论 heard=true", bool(ev("d.outTestGood() && d.outTest.res.probe && d.outTest.res.probe.heard")))
        i3 = ev("d.outTestText()")
        chk("I3 结论人话(观众能听到这一路)", "直播声卡收到了提示音" in (i3 or ""), i3)
        # 就绪度验证的是「直播声卡」那一路：CTA 同款调用 outTestRun(_cableOutDev())
        cable = ev("d._cableOutDev()")
        chk("I4 本机存在直播声卡(CABLE)", bool(cable), cable)
        ev("d.outTestRun(d._cableOutDev()), 1")
        pg.wait_for_timeout(400)
        i5 = ev("d.outTestJudge().state")
        chk("I5 回环裁决=good(同一路+7天内)", i5 == "good", i5)
        i6 = ev("(d.preflight().items.find(i=>i.key==='outtest')||{}).status")
        chk("I6 就绪度试听项转绿", i6 == "ok", i6)

        # ── J 设备缺席自动热切开关：勾选↔heal POST 双向（P6-1 开关链）──
        ev("d.setStreamMode(true), (d.panelOpen.monitor||d.togglePanel('monitor')), d.devAutoSwOn=false, d.toasts=[], 1")
        pg.wait_for_timeout(300)
        heal_n = len(rec["heal"])
        sw = pg.locator("label:visible", has_text="设备缺席自动热切").first
        chk("J1 专家区开关可见", sw.count() > 0 and sw.is_visible())
        sw.click()
        pg.wait_for_timeout(400)
        chk("J2 勾选→heal POST dev_autoswitch=true", len(rec["heal"]) == heal_n + 1
            and rec["heal"][-1].get("dev_autoswitch") is True, rec["heal"][heal_n:])
        chk("J3 开关态回读(服务端echo)", bool(ev("d.devAutoSwOn===true")))
        j4 = ev("d.toasts.map(t=>t.msg).join('|')")
        chk("J4 开启 toast(带护栏参数)", "已开启设备缺席自动热切" in (j4 or ""), j4)
        ev("d.toasts=[], 1")
        pg.wait_for_timeout(150)
        sw.click()
        pg.wait_for_timeout(400)
        chk("J5 再点→heal POST dev_autoswitch=false", len(rec["heal"]) == heal_n + 2
            and rec["heal"][-1].get("dev_autoswitch") is False, rec["heal"][heal_n:])
        chk("J6 关闭态回读+toast", bool(ev("d.devAutoSwOn===false"))
            and "已关闭设备缺席自动热切" in (ev("d.toasts.map(t=>t.msg).join('|')") or ""))

        chk("F 全程零 pageerror", not errs, "%d 条" % len(errs))
        for e in errs[:2]:
            print("    --- pageerror ---")
            print("    " + "\n    ".join(str(e).splitlines()[:12]))
        chk("G1 无 x-for 重复/无效 key 警告", not keydups, keydups[:2])
        g2 = ev("(function(){ const dup=a=>new Set(a).size!==a.length;"
                " return !dup(d.rvcInputDevices)&&!dup(d.rvcOutputDevices); })()")
        chk("G2 设备下拉数据无重名(后端+前端双去重生效)", bool(g2))
        pg.close()
        b.close()

    print()
    if FAILS:
        print("== 前端交互冒烟 %d/%d FAIL ==" % (TOTAL[0] - len(FAILS), TOTAL[0]))
        for f in FAILS:
            print(" -", f)
        return 1
    print("== 前端交互冒烟(自救卡/建议条/试音/试听/热切开关) %d/%d 全部通过 ==" % (TOTAL[0], TOTAL[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
