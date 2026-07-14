# -*- coding: utf-8 -*-
"""效果演示自动录制驱动:Playwright 全屏(kiosk)打开 Hub,渲染可见假光标逐步操作,
Bandicam 同步录屏(含系统声音)。每个 --scene 对应 /order 页一个演示位。

用法(facefusion 环境 python):
  python demo_record/driver.py --scene voice [--no-record]

产物: demo_record/out/<scene>_take<N>_raw.mp4 + 同名 _beats.json(节拍时间点,给后期剪辑用)
"""
import argparse
import ctypes
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
HUB = "http://127.0.0.1:9000/ui"

from playwright.sync_api import sync_playwright  # noqa: E402

sys.path.insert(0, HERE)
from recorder import Bandicam  # noqa: E402

# 页面内假光标(白色箭头 + 点击涟漪)。真实光标已停到角落且 Bandicam 不录它。
CURSOR_JS = r"""
(() => {
  if (window.__demoCursor) return;
  const c = document.createElement('div');
  c.id = '__demo_cursor';
  c.style.cssText = 'position:fixed;left:-40px;top:-40px;z-index:2147483647;pointer-events:none;width:26px;height:26px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.6));';
  c.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M5 3 L5 19 L9.5 15.4 L12.2 21 L14.6 19.9 L11.9 14.4 L17.5 14.2 Z" fill="#fff" stroke="#222" stroke-width="1.3"/></svg>';
  document.documentElement.appendChild(c);
  window.__demoCursor = c;
  const st = document.createElement('style');
  st.textContent = '@keyframes __demoRip{from{transform:scale(.35);opacity:.95}to{transform:scale(1.7);opacity:0}}';
  document.head.appendChild(st);
  window.__demoMove = (x, y) => { c.style.left = (x - 3) + 'px'; c.style.top = (y - 2) + 'px'; };
  window.__demoRipple = (x, y) => {
    const r = document.createElement('div');
    r.style.cssText = 'position:fixed;left:' + (x - 17) + 'px;top:' + (y - 17) + 'px;width:34px;height:34px;border-radius:50%;border:3px solid #22d3ee;z-index:2147483646;pointer-events:none;animation:__demoRip .45s ease-out forwards;';
    document.documentElement.appendChild(r);
    setTimeout(() => r.remove(), 520);
  };
})();
"""

# 演示前静默各种新手引导/恢复上次页签,并进入演示模式(隐藏运维噪音)
INIT_LS = """
try {
  localStorage.setItem('avatarhub_seen_tour', '1');
  localStorage.setItem('ah_onboard_v1', '1');
  localStorage.setItem('hub_stream_guide_done', '1');
  localStorage.setItem('hub_demo', '1');
  localStorage.setItem('ah_speak_mode_v1', 'standard');
  localStorage.setItem('hub_tab', 'profiles');
  localStorage.setItem('hub_sidebar_collapsed', '0');
} catch (e) {}
"""


class Human:
    """带缓动的假光标 + CDP 鼠标键盘。所有坐标 = CSS 像素(kiosk 下与屏幕像素一致)。"""

    def __init__(self, page):
        self.page = page
        self.x, self.y = 960.0, 620.0

    def inject(self):
        self.page.evaluate(CURSOR_JS)
        self.page.evaluate("window.__demoMove(%f,%f)" % (self.x, self.y))

    def _ease(self, t):
        return t * t * (3 - 2 * t)  # smoothstep

    def move_to(self, tx, ty, dur=0.55):
        steps = max(8, int(dur * 60))
        sx, sy = self.x, self.y
        for i in range(1, steps + 1):
            k = self._ease(i / steps)
            nx, ny = sx + (tx - sx) * k, sy + (ty - sy) * k
            self.page.evaluate("window.__demoMove(%f,%f)" % (nx, ny))
            self.page.mouse.move(nx, ny)
            time.sleep(dur / steps)
        self.x, self.y = float(tx), float(ty)

    def click(self, locator, settle=0.5, dur=0.55):
        locator.scroll_into_view_if_needed()
        time.sleep(0.25)
        box = locator.bounding_box()
        if not box:
            raise RuntimeError("元素不可见: %s" % locator)
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.move_to(cx, cy, dur)
        time.sleep(0.15)
        self.page.evaluate("window.__demoRipple(%f,%f)" % (cx, cy))
        self.page.mouse.down()
        time.sleep(0.06)
        self.page.mouse.up()
        time.sleep(settle)

    def type(self, text, cps=14):
        self.page.keyboard.type(text, delay=1000.0 / cps)


class Scene:
    def __init__(self, page, human, log):
        self.page = page
        self.h = human
        self.t0 = time.time()
        self.beats = []
        self.log = log

    def beat(self, name):
        t = time.time() - self.t0
        self.beats.append({"t": round(t, 2), "name": name})
        self.log("[%6.1fs] %s" % (t, name))

    def pause(self, s):
        time.sleep(s)

    # ── 通用动作 ──────────────────────────────────────────
    _TAB_ID = {"角色库": "profiles", "克隆": "clone", "语音": "voice", "唱歌": "sing",
               "批量": "batch", "看板": "dashboard", "开播": "stream", "同传": "interp",
               "历史": "history", "交付体检": "selfcheck", "日志": "logs", "设置": "settings"}

    def nav(self, label):
        """点桌面侧栏页签(aria-label=页签名),并断言 Alpine tab 已切换;未切成则 JS 兜底。"""
        btn = self.page.locator('aside button.sb-tab[aria-label="%s"]' % label)
        self.h.click(btn, settle=0.9)
        want = self._TAB_ID.get(label, "")
        if not want:
            return
        for _ in range(3):
            cur = self.page.evaluate(
                "(()=>{try{return Alpine.$data(document.querySelector('[x-data]')).tab}catch(e){return ''}})()")
            if cur == want:
                return
            self.log("  [nav] tab=%r != %r,JS 兜底 goTab" % (cur, want))
            self.page.evaluate(
                "(id)=>{try{Alpine.$data(document.querySelector('[x-data]')).goTab(id)}catch(e){}}", want)
            self.page.wait_for_timeout(600)

    def wait_audio_change(self, prev_src, timeout=120):
        self.page.wait_for_function(
            """(prev) => { const a = document.querySelector('audio[x-ref="player"]');
                 return a && a.src && a.src !== prev; }""",
            arg=prev_src or "", timeout=timeout * 1000)
        return self.page.evaluate(
            "document.querySelector('audio[x-ref=\\'player\\']').src")

    def play_and_wait(self, max_s=60):
        """确保结果音频从头播完(录屏收声)。"""
        self.page.evaluate(
            """() => { const a = document.querySelector('audio[x-ref="player"]');
                 if (a) { a.currentTime = 0; a.play().catch(() => {}); } }""")
        self.page.wait_for_function(
            """() => { const a = document.querySelector('audio[x-ref="player"]');
                 return a && a.ended; }""", timeout=max_s * 1000)

    def speak_button(self):
        return self.page.locator("button:visible", has_text="开口说话").first

    def vis(self, text):
        return self.page.locator("button:visible", has_text=text).first

    def wait_speak_idle(self, timeout=180):
        """合成按钮回到可点状态(speakLoading 结束)。"""
        self.page.wait_for_function(
            """() => { const b = [...document.querySelectorAll('button')]
                 .find(x => x.innerText.includes('开口说话'));
                 return b && !b.disabled; }""", timeout=timeout * 1000)


# ══════════════════════════════════════════════════════════
# scene: voice ── 声音克隆·情感 TTS(全自动,无需真人)
# ══════════════════════════════════════════════════════════
def scene_voice(sc: Scene):
    page, h = sc.page, sc.h
    T_CN = "大家好,欢迎来到无界数字人!现在听到的这个声音,是用三十秒样本克隆出来的。"
    T_EN = "Hello everyone. This is still the same cloned voice, now speaking English."

    sc.beat("开场:角色库")
    sc.pause(1.2)
    # 扫一眼角色卡(视觉引导),不点激活(免得触发形象预计算)
    cards = page.locator('div.grid > div', has_text="已启用")
    if cards.count():
        box = cards.first.bounding_box()
        if box:
            h.move_to(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, 0.7)
    sc.pause(1.0)

    sc.beat("切到语音页")
    sc.nav("语音")
    sc.pause(0.8)

    sc.beat("输入台词")
    ta = page.locator('textarea[x-ref="speakTa"]')
    h.click(ta, settle=0.3)
    h.type(T_CN, cps=16)
    sc.pause(0.8)

    sc.beat("选情感:开心")
    h.click(sc.vis("😊 开心"))
    sc.pause(0.5)

    sc.beat("合成①(开心)")
    prev = page.evaluate(
        "(() => { const a = document.querySelector('audio[x-ref=\\'player\\']'); return a ? a.src : ''; })()")
    h.click(sc.speak_button(), settle=0.2)
    src = sc.wait_audio_change(prev)
    sc.wait_speak_idle()
    sc.beat("播放①")
    sc.play_and_wait()
    sc.pause(0.8)

    sc.beat("展开更多情感 → 悲伤")
    h.click(sc.vis("更多 ▾"), settle=0.4)
    h.click(sc.vis("😢 悲伤"))
    sc.pause(0.5)

    sc.beat("合成②(同一句 · 悲伤)")
    h.click(sc.speak_button(), settle=0.2)
    src = sc.wait_audio_change(src)
    sc.wait_speak_idle()
    sc.beat("播放②")
    sc.play_and_wait()
    sc.pause(0.8)

    sc.beat("换英文台词(多语种)")
    h.click(ta, settle=0.3)
    page.keyboard.press("Control+a")
    time.sleep(0.15)
    h.type(T_EN, cps=18)
    sc.pause(0.4)
    lang = page.locator('select[x-model="speakLang"]')
    box = lang.bounding_box()
    if box:  # 光标移过去 + 涟漪示意,但不真点开原生下拉(kiosk 下 OS 弹层不可控)
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        h.move_to(cx, cy, 0.5)
        page.evaluate("window.__demoRipple(%f,%f)" % (cx, cy))
    lang.select_option("en")
    sc.pause(0.4)
    # 注意:neutral 在标准模式走 XTTS(本机离线),必须选带情感的档位走情感引擎(CosyVoice 多语种)
    h.click(sc.vis("🌸 温柔"))
    sc.pause(0.4)

    sc.beat("合成③(英文)")
    h.click(sc.speak_button(), settle=0.2)
    src = sc.wait_audio_change(src)
    sc.wait_speak_idle()
    sc.beat("播放③")
    sc.play_and_wait()
    sc.pause(1.5)
    sc.beat("收尾")


# ══════════════════════════════════════════════════════════
# scene: interp ── 克隆音实时同传(全自动:TTS中文注入虚拟声卡→真实同传链路→克隆英文出扬声器)
# 前置:场景启动前由 main() 调 _interp_session_up() 起好会话(录制画面里状态=运行中)
# ══════════════════════════════════════════════════════════
import json as _json
import threading
import urllib.request as _rq
import wave as _wave

INTERP = "http://127.0.0.1:7900"


def _post(url, payload=None, timeout=30):
    data = _json.dumps(payload or {}).encode()
    req = _rq.Request(url, data=data, headers={"Content-Type": "application/json"})
    return _json.load(_rq.urlopen(req, timeout=timeout))


def _dev_by_name(sub, output, hostapi=0):
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_output_channels"] if output else d["max_input_channels"]
        if d["hostapi"] == hostapi and ch > 0 and sub.lower() in d["name"].lower():
            return i
    raise RuntimeError("找不到设备: %s" % sub)


def _default_out_index():
    import sounddevice as sd
    base = sd.query_devices(kind="output")["name"].strip()
    return _dev_by_name(base.split(" (")[0], True)


def _play_wav_multi(path, dev_indices):
    """同一段 wav 同时播到多个输出设备(注入虚拟声卡 + 扬声器可闻,供录屏收声)。"""
    import numpy as np
    import sounddevice as sd
    with _wave.open(path, "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    threads = []
    for idx in dev_indices:
        def _p(i=idx):
            try:
                st = sd.OutputStream(samplerate=sr, channels=1, device=i, dtype="float32")
                st.start(); st.write(pcm.reshape(-1, 1)); st.stop(); st.close()
            except Exception as e:
                print("  [inject] dev%s 播放失败: %s" % (i, e), flush=True)
        t = threading.Thread(target=_p, daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join()
    return len(pcm) / sr


def _interp_turn_count():
    try:
        d = _json.load(_rq.urlopen(INTERP + "/transcript.json", timeout=5))
        rows = d if isinstance(d, list) else (d.get("turns") or d.get("rows") or [])
        return len(rows)
    except Exception:
        return -1


def interp_session_up(profile="磁性港风"):
    """起同传会话:麦=CABLE Output(收我们注入的中文),克隆音出口=默认扬声器(录屏可收声)。"""
    st = _json.load(_rq.urlopen(INTERP + "/health", timeout=5))
    if st.get("running"):
        _post(INTERP + "/stop")
        time.sleep(1.5)
    # 关键:清掉历史会话遗留的声纹锁——否则注入的克隆音相似度不达标会被当"非注册说话人"全程拦截
    try:
        _post(INTERP + "/voicelock/reset")
        print("voicelock reset", flush=True)
    except Exception as e:
        print("voicelock reset skip:", e, flush=True)
    mic = _dev_by_name("CABLE Output", False)     # 我方麦=注入口读端(听我们播进去的中文)
    spk = _default_out_index()                    # 克隆英文出扬声器(录屏可收声;不回灌 mic→无自激)
    # 方向B(对方声)故意指向静默虚拟麦(iVCam 未连=数字静音):本演示只走"我说中文→英文克隆"单向,
    # 若指向立体声混音会把扬声器上的中文源+英文克隆再转录一遍→污染字幕。cap_b 静默/报错都无害。
    try:
        silent = _dev_by_name("iVCam", False)
    except RuntimeError:
        silent = _dev_by_name("DroidCam", False)
    r = _post(INTERP + "/start", {
        "mic_index": mic, "cable_index": spk,
        "loopback_index": silent, "loopback_is_output": False,
        "profile": profile, "mode": "local", "live_mode": False})
    print("interp /start ->", r, flush=True)
    if not r.get("ok"):
        raise RuntimeError("同传会话启动失败: %s" % r)
    time.sleep(2.0)
    return {"mic": mic, "spk": spk, "silent": silent}


def scene_interp(sc: Scene):
    page, h = sc.page, sc.h
    cable_in = _dev_by_name("CABLE Input", True)
    spk = _default_out_index()
    src_dir = os.path.join(HERE, "interp_src")
    lines = sorted(f for f in os.listdir(src_dir) if f.endswith(".wav"))

    sc.beat("切到同传页")
    sc.nav("同传")
    sc.pause(3.0)   # iframe 加载 + 观测行出现

    # 光标指一下「同传运行中」观测行
    obs = page.locator("text=同传运行中").first
    try:
        box = obs.bounding_box()
        if box:
            h.move_to(box["x"] + box["width"] / 2, box["y"] + box["height"] + 8, 0.6)
    except Exception:
        pass
    sc.pause(1.0)

    for i, fn in enumerate(lines, 1):
        sc.beat("注入中文第%d句" % i)
        before = _interp_turn_count()
        dur = _play_wav_multi(os.path.join(src_dir, fn), [cable_in, spk])
        sc.beat("第%d句播完(%.1fs),等译文+配音" % (i, dur))
        # 等新转写行出现(字幕上屏),最多 25s
        t0 = time.time()
        while time.time() - t0 < 25:
            n = _interp_turn_count()
            if before >= 0 and n > before:
                break
            time.sleep(0.8)
        sc.pause(8.0)   # 克隆英文配音在扬声器播出(录屏收声)
    sc.pause(2.5)
    sc.beat("收尾")


SCENES = {"voice": scene_voice, "interp": scene_interp}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True, choices=sorted(SCENES))
    ap.add_argument("--no-record", action="store_true", help="只走流程不录屏(调试选择器)")
    ap.add_argument("--take", type=int, default=0, help="覆盖自动 take 序号")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    take = args.take or (1 + len([f for f in os.listdir(OUT_DIR)
                                  if f.startswith(args.scene + "_take")]))
    tag = "%s_take%d" % (args.scene, take)

    def log(msg):
        print(msg, flush=True)

    # 真实光标停到右下角(不参与演示,Bandicam 也不录它)
    ctypes.windll.user32.SetCursorPos(1915, 1040)

    if args.scene == "interp":   # 录制开始前先把同传会话拉起来(画面里直接是运行态)
        interp_session_up()

    rec = Bandicam() if not args.no_record else None
    if rec:
        rec.ensure_running()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(HERE, ".pwprofile"),
            headless=False, no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--kiosk", "--window-position=0,0", "--window-size=1920,1080",
                  "--autoplay-policy=no-user-gesture-required",
                  "--no-first-run", "--disable-infobars",
                  "--hide-crash-restore-bubble", "--disable-session-crashed-bubble"])
        ctx.add_init_script(INIT_LS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HUB, wait_until="domcontentloaded")
        page.wait_for_selector("aside button.sb-tab", timeout=30000)
        vw = page.evaluate("[window.innerWidth, window.innerHeight]")
        log("视口: %s" % vw)

        h = Human(page)
        h.inject()
        sc = Scene(page, h, log)
        time.sleep(1.0)

        if rec:
            log("▶ Bandicam 开始录制")
            rec.start()
        t_rec = time.time()
        err = None
        try:
            SCENES[args.scene](sc)
        except Exception as e:  # 失败也要停录,保留现场
            err = e
            try:
                page.screenshot(path=os.path.join(OUT_DIR, tag + "_error.png"))
            except Exception:
                pass
        raw = None
        if rec:
            raw = rec.stop()
            log("■ 停止录制: %s" % raw)
        ctx.close()

    if err:
        raise err

    result = {"scene": args.scene, "take": take, "raw": raw,
              "rec_started_at": t_rec, "beats": sc.beats}
    beats_path = os.path.join(OUT_DIR, tag + "_beats.json")
    with open(beats_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    if raw and os.path.isfile(raw):
        dst = os.path.join(OUT_DIR, tag + "_raw.mp4")
        if os.path.abspath(raw) != os.path.abspath(dst):
            os.replace(raw, dst)
        log("原片: %s" % dst)
    log("节拍: %s" % beats_path)


if __name__ == "__main__":
    main()
