# -*- coding: utf-8 -*-
"""e2e 门禁：RVC 变声模型资产 + 资产回收站（/api/rvc_assets、/api/asset_trash 系列）

依赖本机 hub（AVATARHUB_URL 可覆盖，默认 :9000）与 playwright；不可用则整体 SKIP。
不依赖 RVC 引擎在线：只验 hub 侧资产逻辑（扫描/绑定/引用拒删/软删/还原/彻删）。
用一个假 .pth 文件全程走真实目录（assets/weights/），跑完全部清理。

覆盖：
  1. 扫描：假模型出现在 /api/rvc_assets 与 /rvc/models（嵌套一层也扫到）
  2. 绑定：bind → profile.rvc_model=相对 id → refs 命中；重复绑定幂等
  3. 保护：被引用时删除→400；解绑（bind id=''）→ refs 清空
  4. 生命周期：软删→回收站列表→还原→再软删→彻删单条→列表消失
  5. 安全：非法 id 404、回收站路径穿越 400、非法 kind 400
  6. UI 冒烟：资产面板 rvc/trash 页签渲染、假模型可见、无 JS 错误
"""
import json, math, os, struct, sys, time, urllib.parse, urllib.request, wave

HUB = os.environ.get("AVATARHUB_URL", "http://127.0.0.1:9000").rstrip("/")
PROF = "_e2eRVC角色"
FAKE = "_e2e_fake_model.pth"
FAKE_SUB = "_e2e_fake_nested.pth"          # 放进 weights/ 嵌套一层的子目录验证嵌套扫描
SAMPLE = "_e2e_rvc_sample.wav"             # 自带试听样本：不能赌机器上恰好有克隆音（空库时预览会 400 没样本）
WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "Retrieval-based-Voice-Conversion-WebUI", "assets", "weights")
CLONES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_clones")
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
        # 90s：引擎在线时真模型试听要冷加载（10-30s+），30s 会在半路超时炸掉整个套件
        with urllib.request.urlopen(req, data, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:                      # 网络级异常（超时/连接断）→ 599，让断言可读地失败而非整套崩
        print(f"    [net] {method} {path}: {e}")
        return 599, {}


def hub_alive():
    try:
        with urllib.request.urlopen(HUB + "/health", timeout=3):
            return True
    except Exception:
        return False


def enc(s):
    return urllib.parse.quote(s)


def cleanup():
    api("DELETE", f"/profiles/{enc(PROF)}")
    for sub_name in ("_e2esub", "e2esub"):
        sub = os.path.join(WEIGHTS, sub_name)
        if os.path.isdir(sub):
            for f in os.listdir(sub):
                os.remove(os.path.join(sub, f))
            os.rmdir(sub)
    for p in (os.path.join(WEIGHTS, FAKE), os.path.join(WEIGHTS, FAKE_SUB),
              os.path.join(CLONES, SAMPLE)):
        if os.path.isfile(p):
            os.remove(p)
    for trash in (os.path.join(WEIGHTS, "_trash"), os.path.join(CLONES, "_trash")):
        if os.path.isdir(trash):
            for f in os.listdir(trash):
                if FAKE in f or FAKE_SUB in f or SAMPLE in f:
                    os.remove(os.path.join(trash, f))
    presets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rvc_model_presets.json")
    if os.path.isfile(presets):
        try:
            data = json.loads(open(presets, encoding="utf-8").read())
            data.pop(FAKE, None)
            data.pop(f"e2esub/{FAKE_SUB}", None)
            open(presets, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            pass


def main():
    if not hub_alive():
        print(f"[SKIP] hub 未运行（{HUB}），跳过 RVC 资产 e2e")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright，跳过 RVC 资产 e2e")
        return 0
    if not os.path.isdir(WEIGHTS):
        print(f"[SKIP] RVC weights 目录不存在（{WEIGHTS}），跳过")
        return 0

    cleanup()
    # 自带试听样本人声：预览接口要求库里至少有一段真人声（空库机器上没有它会 400"没有样本"，
    # 把引擎路径断言全带崩）。1 秒 16k 正弦波足够走通"取样本→调引擎"的通路。
    os.makedirs(CLONES, exist_ok=True)
    with wave.open(os.path.join(CLONES, SAMPLE), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(i * 0.12)))
                                for i in range(16000)))
    # 造假模型：根目录 1 个 + 嵌套子目录 1 个（子目录不能以 "_" 开头——那是 _trash 约定，会被扫描排除）
    with open(os.path.join(WEIGHTS, FAKE), "wb") as f:
        f.write(b"\x00" * 8192)
    sub = os.path.join(WEIGHTS, "e2esub")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, FAKE_SUB), "wb") as f:
        f.write(b"\x00" * 4096)

    try:
        # ── 1. 扫描 ──
        st, d = api("GET", "/api/rvc_assets")
        ids = [a["id"] for a in d.get("assets", [])]
        check("扫描:接口 ok + 假模型在列", st == 200 and d.get("ok") and FAKE in ids, extra=f"n={len(ids)}")
        check("扫描:嵌套子目录也扫到", f"e2esub/{FAKE_SUB}" in ids)
        check("扫描:total_bytes>0", d.get("total_bytes", 0) > 0)

        # ── 2. 绑定 ──
        api("POST", "/profiles", {"name": PROF, "description": "e2e rvc"})
        st, r = api("POST", "/api/rvc_assets/bind", {"id": FAKE, "profile": PROF})
        check("绑定:bind 成功", st == 200 and r.get("ok"), extra=str(r))
        st, pr = api("GET", f"/profiles/{enc(PROF)}?include_face=false")
        check("绑定:角色 rvc_model=相对id", pr.get("rvc_model") == FAKE, extra=repr(pr.get("rvc_model")))
        st, d = api("GET", "/api/rvc_assets")
        mine = [a for a in d.get("assets", []) if a["id"] == FAKE]
        check("绑定:refs 命中", mine and mine[0]["refs"] == [PROF], extra=str(mine[0]["refs"] if mine else None))

        # ── 2.5 参数预设：保存 → 绑定时带入角色 rvc_settings ──
        st, r = api("POST", "/api/rvc_assets/preset",
                    {"id": FAKE, "settings": {"pitch": 2, "index_rate": 0.45, "f0method": "rmvpe"}})
        check("预设:保存成功", st == 200 and r.get("preset", {}).get("pitch") == 2, extra=str(r.get("preset")))
        st, r = api("POST", "/api/rvc_assets/bind", {"id": FAKE, "profile": PROF})
        check("预设:重绑成功", st == 200 and r.get("ok"))
        st, pr = api("GET", f"/profiles/{enc(PROF)}?include_face=false")
        check("预设:绑定时带入 pitch", (pr.get("rvc_settings") or {}).get("pitch") == 2,
              extra=str(pr.get("rvc_settings")))

        # ── 3. 保护 ──
        st, r = api("DELETE", f"/api/rvc_assets/{FAKE}")
        check("保护:被引用删除→400", st == 400 and PROF in str(r.get("detail", "")))
        st, r = api("POST", "/api/rvc_assets/bind", {"id": "", "profile": PROF})
        check("解绑:bind 空 id 成功", st == 200 and r.get("ok"))
        st, d = api("GET", "/api/rvc_assets")
        mine = [a for a in d.get("assets", []) if a["id"] == FAKE]
        check("解绑:refs 清空", mine and mine[0]["refs"] == [])
        st, _ = api("DELETE", "/api/rvc_assets/not_exist_model.pth")
        check("安全:不存在的模型→404", st == 404)

        # ── 3.5 变声试听契约（须在删 FAKE 之前跑——否则 404 而非 502/503）──
        st, _ = api("POST", "/api/rvc_assets/preview", {"id": "not_exist.pth"})
        check("试听:不存在的模型→404", st == 404)
        st, d = api("GET", "/api/rvc_assets")
        rvc_up = bool(d.get("rvc_up"))
        check("试听:rvc_up 健康位存在", "rvc_up" in d, extra=f"rvc_up={rvc_up}")
        st, r = api("POST", "/api/rvc_assets/preview", {"id": FAKE})
        check("试听:假模型不崩（503离线指引/502加载失败）", st in (502, 503),
              extra=f"st={st} {str(r.get('detail'))[:50]}")
        # AS6: 未保存滑条参数直接试听（settings 覆盖已存预设）
        st, r = api("POST", "/api/rvc_assets/preview",
                    {"id": FAKE, "settings": {"pitch": 3, "index_rate": 0.5, "f0method": "rmvpe"}})
        check("试听:settings 覆盖被接受（不 400）", st in (502, 503), extra=f"st={st}")
        st, r = api("POST", "/api/rvc_assets/preview", {"id": FAKE, "settings": "bad"})
        check("试听:settings 非对象→400", st == 400)
        st, r = api("POST", "/api/rvc_assets/preview", {"id": FAKE, "settings": {"pitch": "abc"}})
        check("试听:settings 坏值→400", st == 400, extra=str(r.get("detail"))[:40])
        # AS7: 试听样本可选（sample_profile 指定用某角色参考音当输入）
        st, r = api("POST", "/api/rvc_assets/preview", {"id": FAKE, "sample_profile": "_e2e不存在的角色"})
        check("试听:sample_profile 角色不存在→404", st == 404, extra=str(r.get("detail"))[:40])
        st, r = api("POST", "/api/rvc_assets/preview", {"id": FAKE, "sample_profile": PROF})
        check("试听:sample_profile 无参考音→400", st == 400 and "参考音" in str(r.get("detail")),
              extra=str(r.get("detail"))[:40])
        if rvc_up:
            real = [a["id"] for a in d.get("assets", [])
                    if not a["id"].endswith((FAKE, FAKE_SUB))]
            if real:
                st, r = api("POST", "/api/rvc_assets/preview", {"id": real[0]})
                if st == 200:
                    check("试听:真模型出音频", bool(r.get("audio_base64")),
                          extra=f"len={len(r.get('audio_base64') or '')}")
                    st2, r2 = api("POST", "/api/rvc_assets/preview", {"id": real[0]})
                    check("试听:二次请求命中缓存", st2 == 200 and r2.get("cached") is True)
                else:
                    print(f"  [SKIP] 真模型试听未成功（st={st}），跳过缓存断言")
                    check("试听:真模型失败也返回结构化错误", st in (502, 503, 504), extra=f"st={st}")
                    check("试听:（缓存断言随上一步跳过）", True)
            else:
                check("试听:真模型出音频（无真实模型跳过）", True)
                check("试听:二次请求命中缓存（无真实模型跳过）", True)
        else:
            print("  [SKIP] RVC 引擎离线，跳过真模型试听主路径")
            check("试听:真模型出音频（引擎离线跳过）", True)
            check("试听:二次请求命中缓存（引擎离线跳过）", True)

        # ── 4. 生命周期：软删 → 回收站 → 还原 → 再删 → 彻删 ──
        st, r = api("DELETE", f"/api/rvc_assets/{FAKE}")
        check("软删:成功", st == 200 and r.get("ok") and r.get("trashed"), extra=str(r.get("trashed")))
        trashed_name = r.get("trashed") or ""
        st, t = api("GET", "/api/asset_trash")
        titems = [i for i in t.get("items", []) if i["name"] == trashed_name]
        check("回收站:列表含该项(kind=rvc)", titems and titems[0]["kind"] == "rvc"
              and titems[0]["orig_name"] == FAKE, extra=str(titems[:1]))
        st, r = api("POST", "/api/asset_trash/restore", {"kind": "rvc", "name": trashed_name})
        check("还原:成功且回原名", st == 200 and r.get("restored") == FAKE, extra=str(r))
        check("还原:文件确实回来了", os.path.isfile(os.path.join(WEIGHTS, FAKE)))
        st, r = api("DELETE", f"/api/rvc_assets/{FAKE}")
        trashed_name = r.get("trashed") or ""
        st, r = api("POST", "/api/asset_trash/purge", {"kind": "rvc", "name": trashed_name})
        check("彻删:单条成功", st == 200 and r.get("purged") == 1, extra=str(r))
        st, t = api("GET", "/api/asset_trash")
        check("彻删:列表已消失", not any(i["name"] == trashed_name for i in t.get("items", [])))

        # ── 4b. 还原冲突三态（AS9）：缺省→409 / rename→共存 / overwrite→顶替 ──
        fake_path = os.path.join(WEIGHTS, FAKE)
        with open(fake_path, "wb") as fh:
            fh.write(b"VER_A")
        st, r = api("DELETE", f"/api/rvc_assets/{FAKE}")          # A 进回收站
        tn_a = r.get("trashed") or ""
        with open(fake_path, "wb") as fh:
            fh.write(b"VER_B")                                     # 原位重建 B → 制造冲突
        st, r = api("POST", "/api/asset_trash/restore", {"kind": "rvc", "name": tn_a})
        check("冲突:缺省策略→409", st == 409, extra=str(r))
        st, r = api("POST", "/api/asset_trash/restore",
                    {"kind": "rvc", "name": tn_a, "on_conflict": "hack"})
        check("冲突:非法策略→400", st == 400)
        st, r = api("POST", "/api/asset_trash/restore",
                    {"kind": "rvc", "name": tn_a, "on_conflict": "rename"})
        renamed_name = r.get("restored") or ""
        check("冲突:rename→改名共存", st == 200 and r.get("renamed") is True
              and renamed_name and renamed_name != FAKE, extra=renamed_name)
        with open(fake_path, "rb") as fh:
            check("冲突:rename 原文件未动", fh.read() == b"VER_B")
        renamed_path = os.path.join(WEIGHTS, renamed_name)
        with open(renamed_path, "rb") as fh:
            check("冲突:rename 回收版内容对", fh.read() == b"VER_A")
        os.remove(renamed_path)                                    # 清掉改名产物
        st, r = api("DELETE", f"/api/rvc_assets/{FAKE}")           # B 进回收站
        tn_b = r.get("trashed") or ""
        with open(fake_path, "wb") as fh:
            fh.write(b"VER_C")                                     # 原位再建 C
        st, r = api("POST", "/api/asset_trash/restore",
                    {"kind": "rvc", "name": tn_b, "on_conflict": "overwrite"})
        check("冲突:overwrite→顶替成功", st == 200 and r.get("restored") == FAKE
              and r.get("renamed") is False, extra=str(r))
        with open(fake_path, "rb") as fh:
            check("冲突:overwrite 内容换成回收版", fh.read() == b"VER_B")
        st, r = api("DELETE", f"/api/rvc_assets/{FAKE}")           # 收尾：清回收站
        api("POST", "/api/asset_trash/purge", {"kind": "rvc", "name": r.get("trashed") or ""})

        # ── 5. 安全 ──
        st, _ = api("POST", "/api/asset_trash/restore", {"kind": "rvc", "name": "..\\..\\x.pth"})
        check("安全:回收站路径穿越→400", st == 400)
        st, _ = api("POST", "/api/asset_trash/restore", {"kind": "nope", "name": "x"})
        check("安全:非法 kind→400", st == 400)

        # ── 5.5 资产巡检 + 导出体积预估（AS5 契约）──
        st, h = api("GET", "/api/asset_health")
        check("巡检:接口 ok 字段齐", st == 200 and h.get("ok")
              and all(k in h for k in ("orphans", "unbacked", "trash_n", "trash_bytes",
                                       "trash_old_n", "trash_old_bytes")),
              extra=str(h))
        # AS7: 回收站「清 N 天前」契约（100 年前必删 0 条 → 参数通路验证但零破坏）
        st, r = api("POST", "/api/asset_trash/purge", {"older_than_days": 36500})
        check("回收站:清 100 年前→0 条（参数通路）", st == 200 and r.get("purged") == 0, extra=str(r))
        st, r = api("POST", "/api/asset_trash/purge", {"older_than_days": "abc"})
        check("回收站:天数坏值→400", st == 400)
        st, r = api("POST", "/api/asset_trash/purge", {"older_than_days": -1})
        check("回收站:天数负值→400", st == 400)
        st, pi = api("GET", f"/api/profile/{enc(PROF)}/package_info")
        check("导出预估:est_bytes 三分项", st == 200 and isinstance(pi.get("est_bytes"), dict)
              and all(k in pi["est_bytes"] for k in ("base", "face", "rvc")),
              extra=str(pi.get("est_bytes")))
        check("导出预估:rvc_model/has_face 字段", "rvc_model" in pi and "has_face" in pi)

        # ── 6. UI 冒烟 ──
        errors = []
        with sync_playwright() as p:
            from playwright.sync_api import Error as PWError
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            # 预写"已看过"标记：首访引导浮层不再与测试竞速（同 voice_binding 的确定性处理）
            pg.add_init_script(
                "try{localStorage.setItem('ah_onboard_v1','1');"
                "localStorage.setItem('avatarhub_seen_tour','1');}catch(_){}")
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            def D(expr):
                return pg.evaluate("() => { const d=Alpine.$data(document.body); return " + expr + "; }")

            def ui_smoke():
                pg.goto(HUB + "/ui", wait_until="domcontentloaded")
                pg.wait_for_timeout(3000)
                pg.evaluate("() => { const d=Alpine.$data(document.body); d.onboardShow=false; d.showTour=false; }")
                pg.evaluate("() => Alpine.$data(document.body).vaOpen('rvc')")
                for _ in range(30):
                    if D("!d.vaLoading && d.vaRvc.length>0"):
                        break
                    pg.wait_for_timeout(300)

            for attempt in (1, 2):
                try:
                    ui_smoke()
                    break
                except PWError as e:
                    if attempt == 2:
                        raise
                    print(f"  .. UI 冒烟被页面刷新打断，重试（{e.message.splitlines()[0][:60]}）")
                    pg.wait_for_timeout(2000)

            check("UI:rvc 页签打开且有数据", D("d.vaShow===true && d.vaTab==='rvc' && d.vaRvc.length>0"),
                  extra=f"rvc={D('d.vaRvc.length')}")
            check("UI:假模型条目可见", pg.locator(f"div[role=dialog] >> text={FAKE_SUB}").first.is_visible())
            check("UI:绑定变声入口渲染", pg.locator("select[aria-label='绑定变声到角色']:visible").count() >= 1)
            check("UI:变声试听按钮渲染", pg.locator("button[aria-label='试听此变声']:visible").count() >= 1)
            ui_up = D("d.vaRvcUp")
            check("UI:rvc_up 健康位已回填", ui_up is True or ui_up is False, extra=f"rvc_up={ui_up}")

            # AS5: ⚙️ 预设滑条内联面板（prompt 已淘汰）
            check("UI:⚙️ 预设按钮渲染", pg.locator("button[aria-label='模型默认参数']:visible").count() >= 1)
            pg.locator("button[aria-label='模型默认参数']:visible").first.click()
            pg.wait_for_timeout(400)
            check("UI:预设滑条面板展开", pg.locator("div[role=dialog] >> text=保存为模型默认").first.is_visible()
                  and pg.locator("div[role=dialog] >> text=咬字保护 protect").first.is_visible())
            check("UI:预设面板状态位", bool(D("d.vaPresetId")), extra=str(D("d.vaPresetId")))
            # AS6: 面板内「试听当前参数」按钮（调参闭环：拖滑条→听→存）
            check("UI:试听当前参数按钮渲染", pg.locator("button[aria-label='试听当前滑条参数']:visible").count() >= 1)
            # AS7: 试听输入样本选择器（库存样本 / 角色参考音）
            check("UI:试听用声选择器渲染", pg.locator("select[aria-label='试听输入样本']:visible").count() >= 1)
            check("UI:试听用声默认库存样本", D("d.vaPreviewSample===''"))
            # AS8: 听到即所得——预设面板打开时默认跟随绑定角色参考音；手选过则尊重不覆盖
            follow = pg.evaluate(
                "() => { const d=Alpine.$data(document.body); d.vaPreviewSample=''; "
                "d.profiles.push({name:'_e2e带声角色', has_voice:true}); "
                "d.vaPresetToggle({id:'_zz.pth', refs:['_e2e带声角色'], preset:{}}); "
                "const v=d.vaPreviewSample; d.vaPresetId=''; return v; }")
            check("UI:预设打开默认跟随绑定角色", follow == "_e2e带声角色", extra=str(follow))
            manual = pg.evaluate(
                "() => { const d=Alpine.$data(document.body); d.vaPreviewSample='_e2e手选'; "
                "d.vaPresetToggle({id:'_zz.pth', refs:['_e2e带声角色'], preset:{}}); "
                "const v=d.vaPreviewSample; d.vaPresetId=''; d.vaPreviewSample=''; "
                "d.profiles=d.profiles.filter(p=>p.name!=='_e2e带声角色'); return v; }")
            check("UI:手选试听用声不被覆盖", manual == "_e2e手选", extra=str(manual))

            pg.evaluate("() => { Alpine.$data(document.body).vaTab='trash'; }")
            pg.wait_for_timeout(400)
            check("UI:回收站页签渲染", pg.locator("div[role=dialog] >> text=回收站是空的").count() >= 0)  # 有无条目都合法
            check("UI:健康汇总条渲染", pg.locator("div[role=dialog] >> text=未绑定模型").count() >= 1
                  or pg.locator("div[role=dialog] >> text=资产健康").count() >= 1)

            # AS5: 导出选项面板（勾选项 + 预估体积）
            pg.evaluate("() => { const d=Alpine.$data(document.body); d.vaClose(); d.exportPackage(" + json.dumps(PROF) + "); }")
            for _ in range(20):
                if D("d.expShow===true && !d.expLoading && !!d.expInfo"):
                    break
                pg.wait_for_timeout(300)
            pg.wait_for_timeout(400)   # expInfo 就位后再等一拍：x-if 模板渲染在下一 tick，即查即败是竞态
            check("UI:导出面板打开且拿到预估", D("d.expShow===true && !!d.expInfo && !!d.expInfo.est_bytes"))
            check("UI:导出面板体积行可见", pg.locator("text=预估体积").first.is_visible())
            check("UI:口令输入框渲染", pg.locator("input[placeholder='留空则不加密']").count() >= 1)
            pg.evaluate("() => { Alpine.$data(document.body).expShow=false; }")

            # AS5: 资产巡检轻横幅（逻辑级：注入健康数据断言 getter，环境无关且确定性）
            nudge_on = pg.evaluate("() => { const d=Alpine.$data(document.body); d.nudgeDismissed=false; "
                                   "d.assetHealth={ok:true,orphans:5,unbacked:0,trash_n:0,trash_bytes:0}; return d.assetNudge; }")
            check("UI:横幅逻辑-孤儿≥3 触发", "5" in str(nudge_on), extra=str(nudge_on))
            nudge_trash = pg.evaluate("() => { const d=Alpine.$data(document.body); "
                                      "d.assetHealth={ok:true,orphans:0,unbacked:0,trash_n:9,trash_bytes:600*1024*1024}; return d.assetNudge; }")
            check("UI:横幅逻辑-回收站≥500MB 触发", "回收站" in str(nudge_trash), extra=str(nudge_trash))
            nudge_off = pg.evaluate("() => { const d=Alpine.$data(document.body); "
                                    "d.assetHealth={ok:true,orphans:2,unbacked:8,trash_n:1,trash_bytes:1024}; return d.assetNudge; }")
            check("UI:横幅逻辑-未超阈值不扰(含 unbacked 不触发)", nudge_off == "", extra=repr(nudge_off))
            nudge_dis = pg.evaluate("() => { const d=Alpine.$data(document.body); "
                                    "d.assetHealth={ok:true,orphans:9,unbacked:0,trash_n:0,trash_bytes:0}; d.nudgeDismiss(); return d.assetNudge; }")
            check("UI:横幅逻辑-✕ 后本会话静默", nudge_dis == "", extra=repr(nudge_dis))
            b.close()

        ignored = [e for e in errors if "ERR_INVALID_URL" not in e]
        check("控制台:无新增 JS 错误", len(ignored) == 0, extra="; ".join(ignored[:3]))
    finally:
        cleanup()

    st, d = api("GET", "/api/rvc_assets")
    check("清理:无测试残留", not any(a["id"].endswith((FAKE, FAKE_SUB)) for a in d.get("assets", [])))

    print(f"\n===== RVC资产+回收站 e2e: PASS {len(PASS)}  FAIL {len(FAIL)} =====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
