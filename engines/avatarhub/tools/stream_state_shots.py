# -*- coding: utf-8 -*-
"""[S8 工程底座] 开播页相位状态截图工具（非门禁·人工 QA 辅助）。

为什么单独一个工具而不进 ui_visual_regress 门禁：
  starting/ended/guide 等相位截图依赖「临时补丁副本」伪造前端状态，但页面其余部分
  （就绪度环形 4/6、设备清单、角色头像）仍取自实时服务数据——设备插拔/角色增删都会
  让像素差超阈值，进门禁必然频繁误报。故定位为"一条命令产出全部相位截图供人工过目"，
  与门禁互补：门禁守默认态，本工具守相位态。

用法:
  python tools/stream_state_shots.py                       # 全部状态 → %TEMP%/stream_states
  python tools/stream_state_shots.py --states starting,ended --out D:/shots
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
sys.path.insert(0, str(ROOT))
import ui_visual_regress as vr  # noqa: E402  复用 find_edge/capture_settled（settle 截图法，防抓到加载瞬时态）


def say(msg):
    # vr 导入时可能重包/关闭 sys.stdout；用原始句柄稳妥输出
    sys.__stdout__.write(msg + "\n")
    sys.__stdout__.flush()


# 各相位状态的 hub.js 补丁（锚点必须与源码逐字一致；失配时报错退出，避免"默默截了个默认态"）。
# 锚点选取的都是带注释/多字段的整行片段，避免误伤其他同名字符串。
STATES = {
    # 默认态：不打补丁，直连 /ui#stream（与门禁的 ui_tab 系列同源，此处方便一并对照）
    "default": [],
    # S6 新手三步引导：无视 localStorage 强制显示（步骤打勾仍取真实数据，诚实呈现）
    "guide": [
        ("streamGuideDone: (function(){ try{ return localStorage.getItem('hub_stream_guide_done')==='1'; }catch(_){ return true; } })(),",
         "streamGuideDone: false,"),
        # 同时停用「老操作者免打扰」init 钩子，避免截图环境残留 hub_broadcast_mode 时引导被立即退场
        ("try { if(!this.streamGuideDone && localStorage.getItem('hub_broadcast_mode')) this.guideDismiss(); } catch(_){}",
         "/* [shot] 老操作者免打扰钩子已停用 */"),
    ],
    # ready 相位：钉死已选「真人换脸」（headless 无 localStorage，默认永远停在 setup）。
    # 顺带覆盖 S7 行动条（手机终端在线时）与 CTA 模式文案。
    "ready": [
        ("broadcastMode: (function(){ try{ return localStorage.getItem('hub_broadcast_mode')||''; }catch(_){ return ''; } })(),",
         "broadcastMode: 'real_faceswap',"),
    ],
    # S5a 启动仪式：伪造"点击开播 2.5 秒后"的 starting 相位（里程碑面板 + 耗时）
    "starting": [
        ("streaming:false, streamSteps:[], mjpegOn:false, streamNonce:Date.now(),",
         "streaming:true, streamSteps:[], mjpegOn:false, streamNonce:Date.now(),"),
        ("startingSince: 0,", "startingSince: Date.now()-2500,"),
    ],
    # S5b 停播成绩单：伪造一场 12 分半、峰值 24fps、稳定度 96%、用过变声的场次
    "ended": [
        ("lastSession: null,",
         "lastSession: {durSec:754, peakFps:24, usedRvc:true, stabilityPct:96, endedTs:Date.now()},"),
    ],
    # P7 大字模式过目图：?lt=1 强制 largeText（headless 无 localStorage），验证 +2px 后关键行不折行。
    # 纯 URL 参数、零补丁副本——hub.js 的 largeText 初始化原生识别 lt=1。
    "large": [],
    # P6 强制模式演练（UI 半场）：钉死「trial+强制」授权快照 → 专家模式下 超清1080P/口播极致
    # 预设章显示 🔒+置灰。不触碰真实 license 状态（服务端半场由 _license_test.py 离线覆盖）。
    "locked": [
        ("streamSimple: (function(){ try{ return localStorage.getItem('hub_stream_simple')!=='0'; }catch(_){ return true; } })(),",
         "streamSimple: false,"),
        ("lic:null,",
         "lic:{effective:{enforced:true, preset_ultra:false, preset_vocal:false}},"),
        # 停用 init 补读+licChip 广播订阅：真实授权(评估模式)会覆盖伪造快照 → 锁定态消失
        ("try{ this.lic=window.__bdLic||null; window.addEventListener('bd-lic', e=>{ this.lic=(e&&e.detail)||null; }); }catch(_){}",
         "/* [shot] 授权订阅已停用，钉死伪造 trial+强制 快照 */"),
    ],
}


def build_temp_pages(state):
    """生成 (临时 hub.js, 临时 ui.html)，返回 (url_path, [temp_files])。default/large 态无需副本。
    统一带 ?uivr=1：无头截图=全新浏览器 profile，「三步开始对话/上手引导」等首访浮层
    会随异步竞速随机糊在相位画面上（2026-07-07 实锤 ended 相位被完全遮挡）——
    本工具要的恰是相位本身，与门禁同用 uivr=1 抑制首访层。"""
    if not STATES[state]:
        extra = "&lt=1" if state == "large" else ""
        return f"/ui?uivr=1{extra}#stream", []
    src = (STATIC / "hub.js").read_text(encoding="utf-8", errors="ignore")
    for anchor, repl in STATES[state]:
        if anchor not in src:
            raise SystemExit(f"[{state}] 锚点失配（hub.js 源码已变，请更新 STATES）: {anchor[:60]}...")
        src = src.replace(anchor, repl, 1)
    js_tmp = STATIC / f"_ss_hub_{state}.js"
    js_tmp.write_text(src, encoding="utf-8")

    html = (STATIC / "ui.html").read_text(encoding="utf-8", errors="ignore")
    # hub.js 引用带缓存版本参数(?v=...)，用正则匹配任意版本
    import re as _re
    m = _re.search(r'<script src="/static/hub\.js[^"]*"></script>', html)
    if not m:
        raise SystemExit("ui.html 中未找到 hub.js 引用标签，请更新本工具")
    html = html.replace(m.group(0), f'<script src="/static/_ss_hub_{state}.js"></script>', 1)
    html_tmp = STATIC / f"_ss_ui_{state}.html"
    html_tmp.write_text(html, encoding="utf-8")
    return f"/static/_ss_ui_{state}.html?uivr=1#stream", [js_tmp, html_tmp]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "stream_states"),
                    help="输出目录（默认 %%TEMP%%/stream_states；避免非 ASCII 工作区路径导致 Edge 存图失败）")
    ap.add_argument("--states", default=",".join(STATES), help="逗号分隔: " + ",".join(STATES))
    ap.add_argument("--size", default="1440x1350")
    ap.add_argument("--keep", action="store_true", help="保留临时补丁副本（调试用）")
    args = ap.parse_args()

    edge = vr.find_edge()
    if not edge:
        say("未找到 Edge，无法截图"); return 2
    if not vr.hub_alive(args.base):
        say(f"服务不在线: {args.base}（先启动 avatar_hub）"); return 2
    w, h = (int(x) for x in args.size.lower().split("x"))
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)

    fails = 0
    for state in [s.strip() for s in args.states.split(",") if s.strip()]:
        if state not in STATES:
            say(f"跳过未知状态: {state}"); continue
        url_path, temps = build_temp_pages(state)
        out = outdir / f"stream_{state}_{w}x{h}.png"
        try:
            ok, settled = vr.capture_settled(edge, args.base + url_path, out, w, h)
            say(f"[{state}] captured={ok} settled={settled} -> {out}")
            if not ok:
                fails += 1
        finally:
            if not args.keep:
                time.sleep(0.2)
                for t in temps:
                    try: t.unlink()
                    except Exception: pass
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
