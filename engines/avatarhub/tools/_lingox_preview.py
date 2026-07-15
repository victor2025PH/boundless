# -*- coding: utf-8 -*-
"""同传页(_PAGE)离线预览截图：不起真服务、不占 7900，抽出页面配 mock 接口渲染。

用途：改版语向控件后，肉眼验证多宽度布局与「待生效」琥珀键交互态。
  python tools/_lingox_preview.py
产出：ui_snapshots/preview_lingox/*.png

mock 端点只喂 boot() 所需的最小 JSON；?mockpend=1 时注入脚本模拟
「运行中把对方语言 日语→英语」——走真实 onchange 代码路径点亮「生效」键。
"""
import ast
import io
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ui_snapshots" / "preview_lingox"
PORT = 7911

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

MOCK = {
    "/config/langs": {
        "langs": [{"code": c, "name": n} for c, n in
                  [("zh", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
                   ("ru", "俄语"), ("yue", "粤语"), ("es", "西班牙语"), ("fr", "法语")]],
        "src": "zh", "dst": "ja", "glossary_count": 3, "glossary_on": True,
        "transcript_count": 0, "transcript_on": True, "warm": False, "stream_weak": [],
    },
    "/config/tts": {"engine": "fish"},
    "/devices": {
        "inputs": [{"index": 4, "name": "麦克风(PD100X Podcast Microphone)", "hostapi": "MME"},
                   {"index": 14, "name": "立体声混音(Realtek High Definition)", "hostapi": "MME"}],
        "outputs": [{"index": 23, "name": "CABLE Input(VB-Audio Virtual C)", "hostapi": "MME"}],
        "defaults": {"mic": 4, "cable": 23, "loopback": 14}, "stereo_mix": 14,
    },
    "/hub_profiles": {"profiles": [
        {"name": "林小玲", "active": True, "use_n": 6, "has_voice": True, "vp_fav": True},
        {"name": "清雅淑女", "use_n": 2, "has_voice": True, "voicepack_spk": "s1"},
        {"name": "温柔大叔", "use_n": 0, "has_voice": True},
    ]},
    "/metrics": {"running": False, "live_mode": False},
    "/monitor_status": {"reachable": False},
    "/audio_profile": {"ok": True, "active": "pc",
                       "profiles": {"pc": {"label": "电脑直连"}, "phone": {"label": "手机随身"},
                                    "live": {"label": "直播模式"}},
                       "resolved": {"mic": "麦克风(PD100X)", "listen": "扬声器", "dub": "CABLE Input"},
                       "half_duplex_now": False},
    "/tts/engines_health": {"ok": True, "engines": []},
    "/session/last": {"ok": True, "running": False,
                      "summary": {"profile": "林小玲", "counts": {"a": 23, "b": 18},
                                  "e2e_ms": 1450, "live_mode": True, "ttfv_ms": 620,
                                  "seg_gap_ms": 800, "dropped": 0}},
}

PEND_SNIPPET = """<script>
setTimeout(()=>{ running=true; syncMainBtn(); syncCtx();
  const d=document.querySelector('#ldst'); if(d){ d.value='en'; d.onchange&&d.onchange(); } },1500);
</script>"""


def extract_page() -> str:
    tree = ast.parse(io.open(ROOT / "live_interpreter.py", encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PAGE" and isinstance(node.value, ast.Constant):
                    return node.value.value.replace("__HUB_BASE__", "")
    raise SystemExit("未找到 _PAGE")


class H(BaseHTTPRequestHandler):
    page = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            html = self.page
            if "mockpend=1" in query:
                html = html.replace("</body>", PEND_SNIPPET + "</body>")
            self._send(200, html.encode("utf-8"), "text/html")
            return
        if path in MOCK:
            import json
            self._send(200, json.dumps(MOCK[path], ensure_ascii=False).encode("utf-8"))
            return
        self._send(404, b"{}")

    def do_POST(self):
        self._send(200, b'{"ok": true, "src": "zh", "dst": "en"}')


def find_edge():
    import shutil
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def shot(edge, url, out: Path, w, h):
    if out.exists():
        out.unlink()
    cmd = [edge, "--headless=new", "--disable-gpu", "--force-prefers-reduced-motion",
           "--virtual-time-budget=6000", f"--window-size={w},{h}", f"--screenshot={out}", url]
    subprocess.run(cmd, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out.exists()


def main():
    edge = find_edge()
    if not edge:
        print("✗ 未找到 Edge，跳过截图")
        return 2
    H.page = extract_page()
    OUT.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{PORT}/"
    shots = [
        ("standalone_1600x900", base + "?embed=0", 1600, 900),   # 独立窗口:品牌块保留
        ("embed_1600x900", base + "?embed=1", 1600, 900),        # Hub iframe:品牌块隐藏
        ("embed_1280x800", base + "?embed=1", 1280, 800),
        ("embed_1000x760", base + "?embed=1", 1000, 760),        # Hub iframe 常见宽度
        ("mobile_390x844", base + "?embed=0", 390, 844),         # 手机
        ("pend_embed_1000x760", base + "?embed=1&mockpend=1", 1000, 760),  # 运行中改语向→琥珀「生效」
    ]
    fails = 0
    for name, url, w, h in shots:
        ok = shot(edge, url, OUT / f"{name}.png", w, h)
        print(("✓ " if ok else "✗ ") + f"{name}.png")
        fails += (0 if ok else 1)
    srv.shutdown()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
