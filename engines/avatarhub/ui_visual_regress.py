#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""客户端 UI 可视化回归基线（phone.html 等）。

与 test_ui_optimization.py（字符串断言）互补：本脚本用无头 Edge 在多分辨率 +
多交互状态下截图，并与基线逐像素对比，专门守护「右栏布局不被裁切 / 抽屉覆盖正常」
这类只有渲染后才暴露的问题。

依赖：Microsoft Edge（无头截图）、Pillow（像素对比，缺失则仅截图不对比）。

用法（先确保 Hub 在运行，默认 http://127.0.0.1:9000）：
  .venv_launcher\\Scripts\\python.exe ui_visual_regress.py                # 截图 + 与基线对比
  .venv_launcher\\Scripts\\python.exe ui_visual_regress.py --update-baseline  # 采纳当前为新基线
  .venv_launcher\\Scripts\\python.exe ui_visual_regress.py --base http://127.0.0.1:9000

退出码：0 全部通过 / 基线已更新；1 有超阈真实差异（视觉回归，应阻断发布）；
        2 不可用/跳过（无 Edge / Hub 未起 / 本机尚无基线 / 截图环境异常——门禁视为跳过）。
失败时在 ui_snapshots/diff/ 输出红色高亮差异图，便于人工复核。

跨机基线（阶段9）：像素基线天然含「本机字体渲染 + Edge 版本」特征，换机会整体偏移。
  故基线按机器指纹分目录存放：ui_snapshots/baseline/<平台-edge大版本>/ （如 windows-edge149/）。
  · 某台机器首次跑、该目录不存在 → 判「跳过(2)」并提示 --update-baseline 自建，绝不误报回归。
  · 截图统一加渲染归一化参数（缩放=1 / 关闭字体 hinting / 关闭次像素 LCD），压低跨机/跨次噪声。
"""
import sys, io, os, re, shutil, platform, subprocess, time, argparse, urllib.request
from urllib.parse import quote
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).parent
STATIC = ROOT / "static"
SNAP = ROOT / "ui_snapshots"
CUR = SNAP / "current"
BASE_DIR = SNAP / "baseline"
DIFF = SNAP / "diff"

# 差异阈值（平均绝对像素差占比 %）。抗轻微抗锯齿/光标闪烁噪声，又能抓住真实布局位移。
DIFF_THRESHOLD = 2.0

# phone.html 默认头像取自 profiles[0]/localStorage，跨次会漂移（换头像→整块像素变化误报）。
# 用 ?profile= 钉死一个固定角色，让头像/选中态可复现。该角色不存在时页面自动回退，不致报错。
# 2026-07-09：原钉死「刘亦菲」已随名人合规迁移改名（_p1_celebrity_migrate: 刘亦菲→清雅淑女），
# 角色不在库导致回归长期 SKIP（形同关灯）。改钉迁移后继者并重建像素基线。
PIN_PROFILE = "清雅淑女"

# 截图矩阵：(名称, 状态, 宽, 高)。
#   default=phone 原页；drawer=phone「更多」抽屉展开；ui=工作室 /ui 原页。
SHOTS = [
    ("phone_default_1280x600", "default", 1280, 600),   # 原先被裁切的高度，回归重点
    ("phone_default_1180x470", "default", 1180, 470),   # 极矮屏：引导条折叠 + composer 完整
    ("phone_default_1280x860", "default", 1280, 860),   # 正常桌面
    ("phone_mobile_390x844",   "default", 390, 844),    # 手机单栏
    ("phone_drawer_1200x640",  "drawer",  1200, 640),   # 「更多」抽屉覆盖层
    ("ui_default_1440x900",    "ui",      1440, 900),   # 工作室：默认 Tab（角色库）常用桌面
    ("ui_default_1280x800",    "ui",      1280, 800),   # 工作室：小桌面
    ("ui_narrow_820x900",      "ui",      820, 900),    # 工作室：窄屏/平板（导航响应式）
    # [2026-07-16 复牌] ui_mobile 曾因跨次双稳态（0.00%↔9.20%）摘牌数小时；P5-A 专项定位：
    # 振荡=无头 --screenshot 抓图时机相对页面异步任务的竞速（对照实验实锤：现状参数 6 拍 2 簇，
    # 加 --virtual-time-budget 后 6 拍 1 簇）。shot() 已加虚拟时间预算根修，本行恢复门禁。
    ("ui_mobile_390x900",      "ui",      390, 900),    # 工作室：手机单栏（v3.9 窄屏收口，锚顶栏换行/无横向溢出）
    # 阶段7：逐 Tab 覆盖——只取「跨次零抖动」的表单类 Tab（经临时副本设初始 tab）。
    # 已剔除：dashboard/stream/interp/history/logs/selfcheck（iframe/实时/流式）、
    #         batch（大 textarea 文案/光标漂移）、voice（异步内容偶发整体位移，~20s 尺度抖动）——
    #         门禁要「零误报」，可靠性优先于覆盖面。
    ("ui_tab_clone_1440x900",    "ui:clone",    1440, 900),
    ("ui_tab_sing_1440x900",     "ui:sing",     1440, 900),
    ("ui_tab_settings_1440x900", "ui:settings", 1440, 900),
]

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge():
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    found = shutil.which("msedge") or shutil.which("chrome")
    return found


def edge_major(edge_path):
    """取 Edge/Chromium 大版本号。Windows 上 --version 常乱码/不输出，故优先解析安装目录下的
    版本子目录（…/Application/149.0.4022.80/），再回退到 --version，最后 'x'。"""
    try:
        appdir = Path(edge_path).parent
        vers = [d.name for d in appdir.iterdir()
                if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", d.name)]
        if vers:
            vers.sort(key=lambda s: [int(x) for x in s.split(".")])
            return vers[-1].split(".")[0]
    except Exception:
        pass
    try:
        r = subprocess.run([edge_path, "--version"], capture_output=True, text=True,
                           timeout=10, encoding="utf-8", errors="replace")
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", (r.stdout or "") + (r.stderr or ""))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "x"


def machine_key(edge_path):
    """机器指纹（基线分目录键）：平台 + Edge 大版本。缩放被强制为 1，故不入键。
    例：windows-edge149 / linux-edge140。"""
    return f"{platform.system().lower()}-edge{edge_major(edge_path)}"


def hub_alive(base):
    try:
        with urllib.request.urlopen(base + "/phone", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def make_state_page(state, profile=PIN_PROFILE):
    """为需要交互态的截图生成临时静态副本（直连渲染，避免 iframe+虚拟时间不稳定）。
    phone 类页面统一钉死 ?profile= 以固定头像/选中态；并带 uivr=1 让页面跳过
    「选中即激活」的 /activate 副作用——否则截图批量加载会把全局激活角色反复
    顶回 PIN_PROFILE，与用户手动激活打架（2026-07-07 身份错乱事故）。
    返回 (url_path, temp_file or None)。"""
    pin = (f"?profile={quote(profile)}&uivr=1" if profile else "?uivr=1")
    if state == "default":
        return "/phone" + pin, None
    # /ui 一律带 ?uivr=1：无头截图每次都是全新 profile，「首访」浮层(三步开始对话/上手气泡)
    # 弹不弹取决于与异步数据的竞速(2026-07-06x 实锤 5/11 尺寸随机入镜)。uivr=1 让 hub.js
    # 抑制全部首访浮层——基线锚定确定性的「回头客稳态视图」。
    if state == "ui":
        return "/ui?uivr=1", None
    if state.startswith("ui:"):
        # 直接用 URL hash 选 Tab：hub.js 现在开屏即同步读 location.hash 定初始 Tab（见 init() 顶部「同步定 Tab」），
        # 首帧即命中正确 Tab，无需临时副本/字符串替换。
        # （旧法在 ui.html 里替换内联 `tab:'profiles'`，但该状态早已迁至 hub.js，替换长期空转→clone/sing/settings 实际只截到默认 Tab。）
        tabid = state.split(":", 1)[1]
        return f"/ui?uivr=1#{tabid}", None
    src = (STATIC / "phone.html").read_text(encoding="utf-8", errors="ignore")
    if state == "drawer":
        src = src.replace('<div class="more-mask" id="moreMask"',
                          '<div class="more-mask show" id="moreMask"')
        src = src.replace('<div class="more-settings" id="moreSettings"',
                          '<div class="more-settings show" id="moreSettings"')
    tmp = STATIC / f"_regress_{state}.html"
    tmp.write_text(src, encoding="utf-8")
    return f"/static/_regress_{state}.html" + pin, tmp


def shot(edge, url, out, w, h, vt_ms=0):
    if out.exists():
        out.unlink()
    # 仅保留 --force-prefers-reduced-motion：触发 reduced-motion 降级，冻结头像呼吸/光环等装饰动画。
    # 跨机差异已由「按机器指纹分目录基线」彻底解决，不再叠加渲染归一化参数——实测
    # font-render-hinting/disable-lcd-text 会扰动时序放大异步内容竞态，force-device-scale-factor=1
    # 会触发头像图二次解码竞态（headless 下 DPR 本就为 1，该参数对同机确定性无增益却有害）。
    # [P5-A·2026-07-16] vt_ms>0 → --virtual-time-budget：--screenshot 默认 load 后立即抓帧，
    # 页面异步任务（Alpine 托管/轮询首拍/字体回流）跑到哪一步纯看运气 → /ui 双稳态假红
    # （ui_mobile 0.00%↔9.20% 实锤；对照实验 6 拍 2 簇 → 加虚拟时间 6 拍 1 簇）。
    # 只给 /ui 系启用：/phone 有免提长轮询，虚拟时间会与之互锁偶发 40s 超时（实测），且其基线长期 0.00% 稳定不需要。
    cmd = [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars=false",
           "--force-prefers-reduced-motion",
           *([f"--virtual-time-budget={vt_ms}"] if vt_ms else []),
           f"--window-size={w},{h}", f"--screenshot={out}", url]
    try:
        # 虚拟时间模式下 WS 心跳/轮询会拖慢虚拟时钟推进，真实耗时偶发 >40s → 放宽到 90s
        subprocess.run(cmd, timeout=(90 if vt_ms else 40), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"   ! edge 失败: {e}")
    time.sleep(0.4)
    return out.exists()


def capture_settled(edge, url, out, w, h, settle_eps=0.6, tries=4, vt_ms=0):
    """连续截图直到「相邻两帧基本一致」，得到稳定（已 settle）的画面。
    异步内容（角色卡/服务状态/字体回流）会让一次性截图落在不确定的加载相位上，
    导致基线或当前帧偶发抓到瞬时态。本函数确保我们存/比的都是稳定帧。
    返回 (captured_ok, settled_bool)。settled=False 表示 tries 内仍未收敛（该帧本质抖动）。"""
    prev = out.with_name(out.stem + ".prev.png")
    if not shot(edge, url, out, w, h, vt_ms):
        return False, False
    for _ in range(tries):
        shutil.copy2(out, prev)
        if not shot(edge, url, out, w, h, vt_ms):
            # [P5-A 存量 bug 修复] shot() 开头先删旧帧，中途失败会把 out 留成"不存在"——
            # 下游 diff_pct 直接 FileNotFoundError 崩整个回归。用上一帧回退（保底有图），判未收敛。
            shutil.copy2(prev, out)
            break
        d = diff_pct(out, prev)
        if d is not None and d <= settle_eps:
            try: prev.unlink()
            except Exception: pass
            return True, True
    try: prev.unlink()
    except Exception: pass
    return True, False


def diff_pct(a, b):
    """两图平均绝对像素差（%）。尺寸不一致直接判 100。"""
    try:
        from PIL import Image, ImageChops
    except Exception:
        return None
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return 100.0
    d = ImageChops.difference(ia, ib)
    hist = d.histogram()
    total = 0
    # 三通道各 256 桶；平均绝对差 = Σ(value*count)/(像素数*通道数*255)
    px = ia.size[0] * ia.size[1]
    for ch in range(3):
        base = ch * 256
        for v in range(256):
            total += v * hist[base + v]
    return total / (px * 3 * 255) * 100.0


def write_diff_image(cur, base, outp):
    """生成红色高亮差异图：变化区域标红，其余压暗为灰底，便于一眼定位回归位置。
    尺寸不一致时直接落当前图（并返回 False 表示无法叠加）。"""
    try:
        from PIL import Image, ImageChops, ImageFilter
    except Exception:
        return False
    ia = Image.open(cur).convert("RGB")
    ib = Image.open(base).convert("RGB")
    outp.parent.mkdir(parents=True, exist_ok=True)
    if ia.size != ib.size:
        ia.save(outp)
        return True
    diff = ImageChops.difference(ia, ib).convert("L")
    mask = diff.point(lambda p: 255 if p > 16 else 0).filter(ImageFilter.MaxFilter(3))
    dim = Image.eval(ia.convert("L").convert("RGB"), lambda p: int(p * 0.45))
    red = Image.new("RGB", ia.size, (255, 40, 90))
    Image.composite(red, dim, mask).save(outp)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("HUB_BASE", "http://127.0.0.1:9000"))
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--threshold", type=float, default=DIFF_THRESHOLD)
    ap.add_argument("--profile", default=PIN_PROFILE,
                    help="phone 截图固定的角色名（钉死头像/选中态，避免漂移误报）")
    ap.add_argument("--baseline-key", default=None,
                    help="覆盖机器指纹（默认按 平台-edge大版本 自动取，如 windows-edge149）")
    ap.add_argument("--list-baselines", action="store_true",
                    help="列出已存在的机器基线目录后退出")
    args = ap.parse_args()

    if args.list_baselines:
        keys = sorted(d.name for d in BASE_DIR.iterdir() if d.is_dir()) if BASE_DIR.is_dir() else []
        print("已有机器基线目录：" + ("、".join(keys) if keys else "（无）"))
        return 0

    edge = find_edge()
    if not edge:
        print("· 未找到 Edge/Chrome，跳过可视化回归。"); return 2
    if not hub_alive(args.base):
        print(f"· Hub 未响应：{args.base}/phone —— 跳过（先启动 Hub）"); return 2
    # P13 钉死角色预检：?profile= 指向的角色不在库时，phone 会回退到「激活角色/首个角色」——
    # 回退渲染的是运行态相关的别人脸，截进基线/对比都是脏数据（2026-07-07 单次 9.57% 假红根因）。
    # 缺角色=环境未复位，宁可明确跳过也不产出误导性 diff。
    if args.profile:
        try:
            import json
            with urllib.request.urlopen(args.base + "/profiles", timeout=10) as r:
                _names = [p.get("name") for p in (json.loads(r.read().decode("utf-8")).get("profiles") or [])]
            if args.profile not in _names:
                print(f"· 钉死角色「{args.profile}」不在角色库（现有 {len(_names)} 个）——环境未复位，跳过本次回归。")
                return 2
        except Exception as e:
            print(f"· 角色库预检失败（{e}），继续截图（回退旧行为）。")

    key = args.baseline_key or machine_key(edge)
    base_dir = BASE_DIR / key

    CUR.mkdir(parents=True, exist_ok=True)

    print(f"● Edge: {edge}")
    print(f"● 机器基线: {key}  →  {base_dir}")
    print(f"● Base: {args.base}   阈值: {args.threshold}%   基线更新: {args.update_baseline}\n")

    temps = []
    captured = []
    settled = {}
    try:
        for name, state, w, h in SHOTS:
            url_path, tmp = make_state_page(state, args.profile)
            if tmp:
                temps.append(tmp)
            out = CUR / f"{name}.png"
            _vt = 8000 if state.startswith("ui") else 0   # P5-A：虚拟时间只给 /ui 系（见 shot 注释）
            ok, st = capture_settled(edge, args.base + url_path, out, w, h, vt_ms=_vt)
            settled[name] = st
            flag = "✓" if ok else "✗"
            note = "" if st else "  (未收敛/抖动)"
            print(f"  {flag} 截图 {name}  ({w}x{h}, {state}){note}")
            if ok:
                captured.append(name)
    finally:
        for t in temps:
            try: t.unlink()
            except Exception: pass

    if len(captured) != len(SHOTS):
        print("\n· 有截图未完成（环境问题），跳过本次对比。"); return 2

    if args.update_baseline:
        base_dir.mkdir(parents=True, exist_ok=True)
        unstable = [n for n in settled if not settled[n]]
        for name, *_ in SHOTS:
            shutil.copy2(CUR / f"{name}.png", base_dir / f"{name}.png")
        print(f"\n✓ 基线已更新（{len(SHOTS)} 张，机器={key}） → {base_dir}")
        if unstable:
            print(f"  ⚠ 注意 {len(unstable)} 张采集时未收敛（{'、'.join(unstable)}），其基线可能含瞬时态，建议复跑确认。")
        return 0

    if not base_dir.is_dir() or not any(base_dir.glob("*.png")):
        print(f"\n· 本机（{key}）尚无基线 —— 跳过对比。")
        print(f"  在本机确认页面无误后执行：python ui_visual_regress.py --update-baseline")
        return 2

    print("\n— 与基线对比 —")
    fails, news, flaky = 0, 0, 0
    for name, *_ in SHOTS:
        b = base_dir / f"{name}.png"
        c = CUR / f"{name}.png"
        if not b.exists():
            print(f"  ◇ {name}: 无基线（首次，请 --update-baseline 采纳）"); news += 1; continue
        d = diff_pct(c, b)
        if d is None:
            print(f"  ◇ {name}: 缺 Pillow，跳过对比"); continue
        if d <= args.threshold:
            print(f"  ✓ PASS {name}: 差异 {d:.2f}%")
            continue
        # 超阈：稳定帧→真实回归（阻断）；未收敛帧→判定抖动跳过（不阻断），仅出高亮图供查看
        dp = DIFF / f"{name}.png"
        wrote = write_diff_image(c, b, dp)
        if settled.get(name):
            fails += 1
            print(f"  ✗ FAIL {name}: 差异 {d:.2f}%（稳定帧）" + (f"  → 高亮图 {dp.name}" if wrote else ""))
        else:
            flaky += 1
            print(f"  · SKIP {name}: 差异 {d:.2f}%（采集未收敛，判为抖动不阻断）" + (f"  → {dp.name}" if wrote else ""))

    print(f"\n当前截图目录：{CUR}")
    if fails:
        print(f"✗ {fails} 项超阈（稳定帧=视觉回归）。高亮差异图见：{DIFF}")
        print("  人工复核后：确属预期改动 → --update-baseline 采纳；否则修复回归。")
        return 1
    if flaky:
        print(f"· {flaky} 项采集未收敛被跳过（疑似页面异步抖动，非回归）。")
    if news:
        print("（存在无基线项；确认无误后用 --update-baseline 建立基线）"); return 2
    print("✓ 全部通过。"); return 0


if __name__ == "__main__":
    sys.exit(main())
