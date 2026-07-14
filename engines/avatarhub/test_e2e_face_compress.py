# -*- coding: utf-8 -*-
"""e2e 门禁：人脸照片客户端压缩（浏览器内 canvas 长边 1280 + JPEG 重编码）

依赖本机 hub（http://127.0.0.1:9000）、playwright、PIL；任一不可用则整体 SKIP（exit 0）。

覆盖：
  1. 超限大图（b64 > 后端 7M 字符限）→ 压到限内、长边精确 1280、输出 JPEG
  2. 小尺寸 JPEG（≤1280 且 <400KB）→ 字节级原样保留（不二次有损）
  3. 带透明 PNG → 白底 JPEG（防转档变黑）
  4. 压缩图走完整「照片建角色（稍后绑定声音）」→ 后端 has_face=true → 清理
"""
import base64, json, os, random, sys, urllib.request, urllib.parse

HUB = "http://127.0.0.1:9000"
PNAME = "_e2e压图测试"
PENC = urllib.parse.quote(PNAME)
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


def main():
    if not hub_alive():
        print("[SKIP] hub 未运行（127.0.0.1:9000），跳过压图 e2e")
        return 0
    try:
        from playwright.sync_api import sync_playwright
        from PIL import Image, ImageDraw
    except ImportError as e:
        print(f"[SKIP] 缺依赖（{e.name}），跳过压图 e2e")
        return 0

    tmp = os.environ.get("TEMP", ".")
    # ① 大 JPEG：4000x3000 随机色块（难压缩 → 文件大）
    big = Image.new("RGB", (4000, 3000))
    dr = ImageDraw.Draw(big)
    random.seed(7)
    for _ in range(30000):
        x, y = random.randint(0, 3999), random.randint(0, 2999)
        dr.rectangle([x, y, x+18, y+18],
                     fill=(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    big_path = os.path.join(tmp, "_e2e_big.jpg")
    big.save(big_path, "JPEG", quality=97)
    big_b64_len = (os.path.getsize(big_path) * 4 + 2) // 3
    check("准备:大图确实超后端限", big_b64_len > 7_000_000, extra=f"b64≈{big_b64_len/1e6:.1f}M字符")

    # ② 小 JPEG
    small = Image.new("RGB", (800, 600), (90, 140, 200))
    ImageDraw.Draw(small).ellipse([200, 100, 600, 500], fill=(230, 200, 170))
    small_path = os.path.join(tmp, "_e2e_small.jpg")
    small.save(small_path, "JPEG", quality=85)
    small_b64 = base64.b64encode(open(small_path, "rb").read()).decode()

    # ③ 透明 PNG
    png = Image.new("RGBA", (2000, 2000), (0, 0, 0, 0))
    ImageDraw.Draw(png).ellipse([400, 400, 1600, 1600], fill=(200, 60, 60, 255))
    png_path = os.path.join(tmp, "_e2e_alpha.png")
    png.save(png_path, "PNG")

    api("DELETE", f"/profiles/{PENC}")

    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(HUB + "/ui", wait_until="domcontentloaded")
        pg.wait_for_timeout(2500)

        def D(expr):
            return pg.evaluate("() => { const d=Alpine.$data(document.body); return " + expr + "; }")

        def quiet():
            pg.evaluate("() => { const d=Alpine.$data(document.body); d.onboardShow=false; d.showTour=false; }")
            pg.wait_for_timeout(150)

        def face_inputs():
            return pg.locator("div[x-show*=\"createHubMode==='photo'\"] input[type=file][accept='image/*']")

        def wait_face(prev_len=-1):
            for _ in range(40):
                ln = D("(d.newP.faceB64||'').length")
                if ln and ln != prev_len:
                    return ln
                pg.wait_for_timeout(250)
            return D("(d.newP.faceB64||'').length")

        quiet()
        pg.evaluate("() => { const d=Alpine.$data(document.body); d.openCreateHub(); d.pickCreate('photo'); }")
        pg.wait_for_timeout(500); quiet()
        check("表单:人脸输入存在", face_inputs().count() >= 1)

        # ── ① 大图压缩 ──
        face_inputs().first.set_input_files(big_path)
        ln = wait_face()
        head = D("(d.newP.facePreview||'').slice(0,30)")
        check("大图:压到后端限内", 10000 < ln < 1_500_000, extra=f"{big_b64_len/1e6:.1f}M→{ln/1e6:.2f}M字符")
        check("大图:输出 JPEG", head.startswith("data:image/jpeg"), extra=head)
        dims = pg.evaluate("""() => new Promise(res => {
            const d=Alpine.$data(document.body);
            const im=new Image(); im.onload=()=>res([im.naturalWidth,im.naturalHeight]);
            im.src=d.newP.facePreview; })""")
        check("大图:长边=1280", max(dims) == 1280, extra=str(dims))

        # ── ② 小图原样保留 ──
        face_inputs().first.set_input_files(small_path)
        ln2 = wait_face(prev_len=ln)
        same = pg.evaluate("(b) => Alpine.$data(document.body).newP.faceB64===b", small_b64)
        check("小图:原样保留不重编码", same, extra=f"len={ln2}")

        # ── ③ 透明 PNG → JPEG ──
        face_inputs().first.set_input_files(png_path)
        ln3 = wait_face(prev_len=ln2)
        head3 = D("(d.newP.facePreview||'').slice(0,30)")
        check("透明PNG:转 JPEG", head3.startswith("data:image/jpeg"), extra=f"len={ln3}")

        # ── ④ 完整创建（稍后绑定声音）──
        pg.evaluate(f"""() => {{ const d=Alpine.$data(document.body);
            d.newP.name='{PNAME}'; d.setVoiceMode('none'); }}""")
        pg.wait_for_timeout(300)
        check("创建:无拦截原因", D("d.newPBlockReason") == "", extra=str(D("d.newPBlockReason")))
        pg.evaluate("() => Alpine.$data(document.body).createProfile()")
        for _ in range(40):
            if D("d.newPDone!==''"):
                break
            pg.wait_for_timeout(500)
        check("创建:成功态出现", D("d.newPDone") == PNAME, extra=str(D("d.createMsg")))
        b.close()

    st, d = api("GET", f"/profiles/{PENC}?include_face=true")
    check("后端:has_face=true", bool(d.get("has_face")))
    fb = d.get("face_b64") or ""
    check("后端:face_b64 在限内", 0 < len(fb) < 7_000_000, extra=f"len={len(fb)}")

    check("清理:删除临时角色", api("DELETE", f"/profiles/{PENC}")[0] == 200)
    for f in ("_e2e_big.jpg", "_e2e_small.jpg", "_e2e_alpha.png"):
        try:
            os.remove(os.path.join(tmp, f))
        except OSError:
            pass

    ignored = [e for e in errors if "ERR_INVALID_URL" not in e and "data:image/jpeg;base64,undefined" not in e]
    check("控制台:无新增 JS 错误", len(ignored) == 0, extra="; ".join(ignored[:3]))

    print(f"\n===== 压图 e2e: PASS {len(PASS)}  FAIL {len(FAIL)} =====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
