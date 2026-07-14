# -*- coding: utf-8 -*-
"""e2e 门禁：角色声音绑定全链路（编辑抽屉换声 / 解绑 / 现场录音管线 / 克隆向导冒烟）

依赖本机 hub（http://127.0.0.1:9000）与 playwright；任一不可用则整体 SKIP（exit 0），
不阻塞离线门禁。建临时角色跑完即删，不动真实数据。

覆盖：
  1. 抽屉「现录/上传新声音」：上传 → 双质检 → 漏授权保存被拦 → 授权保存 → 后端 voice_b64 落库
  2. 「收起=放弃」语义：面板收起后已录内容清空
  3. 解绑：克隆参考音在下拉框如实显示「当前：专属参考音」；选「不绑定」保存 → 确认弹窗 → has_voice=false
  4. PATCH clear_voice 纯 API 契约（不带其他字段也能解绑）
  5. 现场录音管线（Chromium 假麦克风）：录音中电平>0、停止后走完转码+质检（2 秒短音频必报「过短」）
  6. 克隆向导冒烟：Tab 可达、录音入口在
"""
import base64, io, json, math, os, struct, sys, urllib.request, urllib.parse, wave

HUB = "http://127.0.0.1:9000"
PNAME = "_e2e换声测试"
PENC = urllib.parse.quote(PNAME)
PNAME2 = "_e2e解绑API"
PENC2 = urllib.parse.quote(PNAME2)
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))


def api(method, path, body=None):
    req = urllib.request.Request(HUB + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def hub_alive():
    try:
        with urllib.request.urlopen(HUB + "/health", timeout=3):
            return True
    except Exception:
        return False


def make_wav(path, sec=10.0, sr=16000):
    """8.5s 有声段 + 尾部近静音：分帧 SNR 估算(安静帧≈底噪)必然高分；带包络防削幅。"""
    n = int(sec * sr)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            t = i / sr
            if t < sec * 0.85:
                env = 0.55 + 0.35 * math.sin(2 * math.pi * 1.3 * t)
                v = env * 0.5 * (math.sin(2*math.pi*220*t) + 0.4*math.sin(2*math.pi*440*t) + 0.2*math.sin(2*math.pi*880*t))
            else:
                v = 0.001 * math.sin(2*math.pi*100*t)
            frames += struct.pack("<h", int(max(-1, min(1, v)) * 30000))
        wf.writeframes(bytes(frames))


def main():
    if not hub_alive():
        print("[SKIP] hub 未运行（127.0.0.1:9000），跳过声音绑定 e2e")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright，跳过声音绑定 e2e")
        return 0

    wav_path = os.path.join(os.environ.get("TEMP", "."), "_e2e_voice.wav")
    make_wav(wav_path)

    # ── 准备：临时无声角色 ──
    api("DELETE", f"/profiles/{PENC}")
    st, d = api("POST", "/profiles", {"name": PNAME, "description": "e2e 换声临时角色"})
    check("准备:创建无声角色", st == 200)
    st, d = api("GET", f"/profiles/{PENC}?include_face=false")
    check("准备:初始 has_voice=false", not d.get("has_voice"))

    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        ctx = b.new_context(permissions=["microphone"])
        # 首访引导（onboard/tour）在 init 异步尾部才弹，固定 sleep+单次 quiet() 有竞态（hub 冷启时实锤挡点击）
        # → 预写"已看过"标记，从源头让浮层永不触发，测试全程确定性
        ctx.add_init_script(
            "try{localStorage.setItem('ah_onboard_v1','1');"
            "localStorage.setItem('avatarhub_seen_tour','1');}catch(_){}")
        pg = ctx.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("dialog", lambda dlg: dlg.accept())   # 解绑确认弹窗 → 接受
        pg.goto(HUB + "/ui", wait_until="domcontentloaded")
        pg.wait_for_timeout(2500)

        def D(expr):
            return pg.evaluate("() => { const d=Alpine.$data(document.body); return " + expr + "; }")

        def quiet():
            pg.evaluate("() => { const d=Alpine.$data(document.body); d.onboardShow=false; d.showTour=false; }")
            pg.wait_for_timeout(150)

        def wait_quality(domain):
            for _ in range(60):
                if D(f"!!d.{domain}.quality && !d.{domain}.checking"):
                    return True
                pg.wait_for_timeout(500)
            return False

        quiet()

        # ══ 1. 抽屉换声 ══
        pg.evaluate(f"""() => {{ const d=Alpine.$data(document.body);
            d.openEdit(d.profiles.find(x=>x.name==='{PNAME}'),'edit'); }}""")
        pg.wait_for_timeout(600); quiet()
        toggle = pg.locator("button:has-text('现录 / 上传一段新声音替换')")
        check("抽屉:换声入口可见", toggle.count() >= 1 and toggle.first.is_visible())
        toggle.first.click(); pg.wait_for_timeout(300)
        pg.locator("input[type=file][accept='audio/*']").last.set_input_files(wav_path)
        check("抽屉:质检返回", wait_quality("editVoice"))
        check("抽屉:质检通过", D("d.editVoice.quality.ok===true"), extra=str(D("d.editVoice.quality")))

        pg.locator("button:has-text('保存修改')").first.click(); pg.wait_for_timeout(400)
        check("抽屉:漏授权保存被拦", "合法使用权" in (D("d.editMsg") or ""), extra=D("d.editMsg"))

        # ══ 2. 收起=放弃 ══
        pg.locator("button:has-text('取消换声')").first.click(); pg.wait_for_timeout(200)
        check("收起:已录内容清空", D("d.editVoice.open===false && d.editVoice.audioB64===''"))

        # 重传 → 授权 → 保存
        toggle.first.click(); pg.wait_for_timeout(200)
        pg.locator("input[type=file][accept='audio/*']").last.set_input_files(wav_path)
        wait_quality("editVoice")
        pg.evaluate("() => { Alpine.$data(document.body).editVoice.agreed=true; }")
        pg.wait_for_timeout(200)
        pg.locator("button:has-text('保存修改')").first.click()
        for _ in range(60):
            if (D("d.editMsg") or "").startswith(("✅", "❌")):
                break
            pg.wait_for_timeout(500)
        check("抽屉:保存成功提示换声", "声音已替换" in (D("d.editMsg") or ""), extra=D("d.editMsg"))
        pg.wait_for_timeout(2000)   # 等抽屉自动关闭 + 角色列表刷新

        st, d = api("GET", f"/profiles/{PENC}?include_face=true")
        check("后端:换声后 voice_b64 落库", len(d.get("voice_b64") or "") > 10000,
              extra=f"len={len(d.get('voice_b64') or '')}")

        # ══ 3. 解绑（UI 全流程）══
        pg.evaluate(f"""() => {{ const d=Alpine.$data(document.body);
            d.openEdit(d.profiles.find(x=>x.name==='{PNAME}'),'edit'); }}""")
        pg.wait_for_timeout(600); quiet()
        check("解绑:下拉如实显示当前参考音", D("d.editP.voice_name==='__current__'"))
        pg.evaluate("() => { Alpine.$data(document.body).editP.voice_name=''; }")
        pg.wait_for_timeout(300)
        check("解绑:预告提示可见", pg.locator("text=将解绑该角色的声音").first.is_visible())
        pg.locator("button:has-text('保存修改')").first.click()   # confirm 由 dialog 处理器接受
        for _ in range(60):
            if (D("d.editMsg") or "").startswith(("✅", "❌")):
                break
            pg.wait_for_timeout(500)
        check("解绑:保存成功提示解绑", "声音已解绑" in (D("d.editMsg") or ""), extra=D("d.editMsg"))
        pg.wait_for_timeout(1000)
        st, d = api("GET", f"/profiles/{PENC}?include_face=true")
        check("后端:解绑后 has_voice=false", not d.get("has_voice"),
              extra=f"voice_b64_len={len(d.get('voice_b64') or '')}")

        # ══ 5. 现场录音管线（假麦克风）══
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.editShow=false; d.openCreateHub(); d.pickCreate('photo'); }")
        pg.wait_for_timeout(500); quiet()
        pg.evaluate("() => { Alpine.$data(document.body).setVoiceMode('new'); }")
        pg.wait_for_timeout(300)
        rec_btn = pg.locator("button:has-text('现场录音'):visible")
        check("录音:入口可见", rec_btn.count() >= 1)
        rec_btn.first.click()
        pg.wait_for_timeout(800)
        check("录音:状态进行中", D("d.recording===true && d._recTarget==='newP'"))
        max_lvl = 0
        for _ in range(18):   # 采样 ~2.7s 取峰值（Chromium 假麦克风是间歇脉冲音，电平常年偏低）
            max_lvl = max(max_lvl, D("d.recLevel") or 0)
            pg.wait_for_timeout(150)
        check("录音:电平有读数", max_lvl > 2, extra=f"peak={max_lvl}")
        check("录音:电平条渲染", pg.locator("div[role=meter]:visible").count() >= 1)
        pg.locator("button:has-text('停止录音'):visible").first.click()
        check("录音:质检返回（短音频）", wait_quality("newVoice"))
        reason = D("d.newVoice.quality && d.newVoice.quality.reason") or ""
        check("录音:2秒短音频判「过短」", D("d.newVoice.quality.ok===false") and "过短" in reason, extra=reason)
        check("录音:停止后电平复位", D("d.recLevel===0 && d.recording===false"))

        # ══ 6. 克隆向导冒烟 ══
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.createHubShow=false; d.pickCreate('voice'); }")
        pg.wait_for_timeout(600); quiet()
        check("克隆向导:Tab 可达且录音入口在",
              D("d.tab==='clone'") and pg.locator("button:has-text('现场录音'):visible").count() >= 1)
        b.close()

    # ══ 4. PATCH clear_voice 纯 API 契约 ══
    api("DELETE", f"/profiles/{PENC2}")
    api("POST", "/profiles", {"name": PNAME2, "voice_name": "dummy.wav"})
    st, d = api("GET", f"/profiles/{PENC2}?include_face=false")
    check("API:预置 has_voice=true", bool(d.get("has_voice")))
    st, d = api("PATCH", f"/profiles/{PENC2}", {"clear_voice": True})
    check("API:clear_voice 请求成功", st == 200 and d.get("ok"))
    st, d = api("GET", f"/profiles/{PENC2}?include_face=false")
    check("API:纯 clear_voice 解绑生效", not d.get("has_voice"))

    # ── 清理 ──
    check("清理:删除临时角色", api("DELETE", f"/profiles/{PENC}")[0] == 200
                              and api("DELETE", f"/profiles/{PENC2}")[0] == 200)

    ignored = [e for e in errors if "ERR_INVALID_URL" not in e and "data:image/jpeg;base64,undefined" not in e]
    check("控制台:无新增 JS 错误", len(ignored) == 0, extra="; ".join(ignored[:3]))

    print(f"\n===== 声音绑定 e2e: PASS {len(PASS)}  FAIL {len(FAIL)} =====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
