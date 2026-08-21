# -*- coding: utf-8 -*-
"""集群实况活体 HUD 契约断言（P1 · 2026-08-07）。

守什么（出处《集群实况板_五视角优化美化方案_20260807.md》§15.6/§九/§十）：
  A. hud.html 结构件齐全（面板/六机行/判定/事件/时钟/三枚状态章/关闭钮/页脚身份）。
  B. 性能总督四闸在位：推流冻结(streaming→frozen) / 页面隐藏暂停(visibilitychange+document.hidden) /
     reduced-motion 静帧 / 帧预算降密度(FRAME_BUDGET)；夜间降亮(night+brightness)。
  C. 离线纪律：hud.html 零外链（禁 http:// 与 https:// 字面量——wasm/字体/图标全走同源相对路径）。
  D. 令牌同源：render_cluster_board.py 的 INK_*/NEON_*/COLORS 常量逐值必须出现在 hud.html
     （壁纸板与活体 HUD 一个色板，改色只许改渲染器再同步，防两张皮漂移）。
  E. 水位阈值同源：hud_server.VRAM_BADGE_MIN/VRAM_SAT_MIN == render_cluster_board 同名值。
  F. hud_server 端点面：/api/stream(text/event-stream)/health/icon + 心跳文件 + import 小界（判据同源锚）。
  G. P0 回归锚：xiaojie /board/ 路由与 BOARD_STALE_S 仍在（防后续改动拆了六机 PNG 广播）。
  H. 三个 py 全部可编译。
  I. P2 手势层（2026-08-07 二期）：vendor 资产在位（wasm×2/bundle/模型）、worker 只引本地
     vendor、六词汇阈值常量（CONF 0.7/COOLDOWN 300/DWELL_ACK 1200）、虚拟机位钉扎排除
     （virtual|obs|vcam…）、隐私声明（不落盘不出机）、注入测试钩（_injectGesture）、
     demo 模式（demoCtx/scene）、ack 端点（hud_server /api/alerts/ack + import alerts 同源锚）、
     alerts.ack_alert 存在且 raise_alert 持续期保留 ack、xiaojie 事件带 key/ack。
  N. P0 展演骨架（未来感机房展演方案 · 2026-08-08）：tour 引擎（Condition 即时唤醒 +
     服务端时钟对时 + 幕次表两侧同源）、遥控页（零外链/PIN/预检）、编排层（波纹/横幅/
     谢幕 + 推流硬闸清场）、埋点信标（demo 不落账 + 截尾闸）、周报工具。

用法：python hud_contract_test.py   （0=全绿；非 0=有红，逐条打印）
"""
from __future__ import annotations

import json
import py_compile
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                      # D:\boundless
HUD_HTML = HERE / "hud.html"
HUD_SRV = HERE / "hud_server.py"
BOARD_PY = ROOT / "tools" / "render_cluster_board.py"
XIAOJIE = ROOT / "tools" / "xiaojie" / "xiaojie_server.py"
ALERTS = Path(r"C:\模仿音色") / "alerts.py"
MONITOR = Path(r"C:\模仿音色") / "monitor_relay.py"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name + (("：" + detail) if (detail and not ok) else "")))


def rgb_hex(r: str, g: str, b: str) -> str:
    return f"#{int(r):02X}{int(g):02X}{int(b):02X}"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    html = HUD_HTML.read_text(encoding="utf-8", errors="replace")
    srv = HUD_SRV.read_text(encoding="utf-8", errors="replace")
    board = BOARD_PY.read_text(encoding="utf-8", errors="replace")
    xj = XIAOJIE.read_text(encoding="utf-8", errors="replace")
    html_up = html.upper()

    # A. 结构件
    for el in ["id=\"rain\"", "id=\"panel\"", "id=\"rows\"", "id=\"verdict\"", "id=\"vreason\"",
               "id=\"events\"", "id=\"clock\"", "id=\"chip-stale\"", "id=\"chip-frozen\"",
               "id=\"chip-conn\"", "id=\"closebtn\"", "id=\"foot-id\"", "id=\"foot-meta\"",
               "id=\"chip-cam\"", "id=\"chip-demo\"", "id=\"gcur\"", "id=\"hint\"",
               "id=\"detail\"", "id=\"dcard\""]:
        check(f"A 结构件 {el}", el in html)

    # B. 性能总督
    for mark, why in [("visibilitychange", "隐藏暂停监听"), ("document.hidden", "隐藏判定"),
                      ("prefers-reduced-motion", "动效弱化尊重"), ("FRAME_BUDGET", "帧预算降密度"),
                      ("streaming", "推流冻结分支"), ("\"frozen\"", "冻结模式"),
                      ("night", "夜间模式类"),
                      ("nightshade", "夜间降亮遮罩层（body filter 会错位 fixed 层，2026-08-08 实锤禁用）")]:
        check(f"B 总督 {why}", mark in html)

    # C. 离线纪律：零外链
    ext = re.findall(r"https?://", html)
    check("C 离线零外链", not ext, f"发现 {len(ext)} 处外链字面量")

    # D. 令牌同源（渲染器 → HUD）
    named = re.findall(r"^(INK_\d+|NEON_[A-Z]+)\s*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", board, re.M)
    check("D 渲染器常量可解析", len(named) >= 6, f"只解析到 {len(named)} 个")
    for name, r, g, b in named:
        if name == "NEON_BLUE":     # HUD 未用蓝（板上也只用于渐变备选），不强制
            continue
        hx = rgb_hex(r, g, b)
        check(f"D 令牌 {name}={hx}", hx in html_up)
    mcolors = re.search(r"COLORS\s*=\s*\{[^}]+\}", board)
    trip = re.findall(r"\((\d+),\s*(\d+),\s*(\d+)\)", mcolors.group(0)) if mcolors else []
    check("D 状态色可解析", len(trip) == 4, f"解析到 {len(trip)} 个")
    for r, g, b in trip:
        hx = rgb_hex(r, g, b)
        check(f"D 状态色 {hx}", hx in html_up)

    # E. 水位阈值同源
    mb = re.search(r"VRAM_BADGE_FRAC,\s*VRAM_BADGE_MIN\s*=\s*[\d.]+,\s*(\d+)", board)
    ms = re.search(r"VRAM_SAT_FRAC,\s*VRAM_SAT_MIN\s*=\s*[\d.]+,\s*(\d+)", board)
    sb = re.search(r"^VRAM_BADGE_MIN\s*=\s*(\d+)", srv, re.M)
    ss = re.search(r"^VRAM_SAT_MIN\s*=\s*(\d+)", srv, re.M)
    check("E 高水位分钟阈值同源", bool(mb and sb) and mb.group(1) == sb.group(1),
          f"渲染器 {mb and mb.group(1)} vs hud_server {sb and sb.group(1)}")
    check("E 饱和分钟阈值同源", bool(ms and ss) and ms.group(1) == ss.group(1),
          f"渲染器 {ms and ms.group(1)} vs hud_server {ss and ss.group(1)}")

    # F. hud_server 端点面与同源锚
    for mark, why in [("/api/stream", "SSE 端点"), ("text/event-stream", "SSE 头"),
                      ("/health", "健康端点"), ("/icon/", "图标端点"),
                      ("hud_server_state.json", "doctor 心跳文件"),
                      ("import xiaojie_server", "判据同源 import 锚")]:
        check(f"F 服务 {why}", mark in srv)

    # G. P0 回归锚（六机 PNG 广播不许被拆）
    check("G xiaojie /board 路由在位", "/board/" in xj)
    check("G xiaojie 陈旧闸在位", "BOARD_STALE_S" in xj)

    # H. 可编译
    for p in [HUD_SRV, BOARD_PY, XIAOJIE, ALERTS]:
        try:
            py_compile.compile(str(p), doraise=True)
            check(f"H 编译 {p.name}", True)
        except Exception as e:  # noqa: BLE001
            check(f"H 编译 {p.name}", False, str(e)[:120])

    # I. P2 手势层
    for rel in ["tasks-vision/vision_bundle.mjs", "tasks-vision/wasm/vision_wasm_internal.wasm",
                "tasks-vision/wasm/vision_wasm_nosimd_internal.wasm", "models/gesture_recognizer.task"]:
        p = HERE / "vendor" / rel
        check(f"I vendor {rel}", p.is_file() and p.stat().st_size > 1024)
    for mark, why in [("CONF:0.7", "置信度阈值 0.7"), ("COOLDOWN:300", "冷却 300ms"),
                      ("DWELL_ACK:1200", "ack dwell 1.2s"), ("DWELL_WAKE:1000", "唤醒 dwell 1s"),
                      ("virtual|obs|vcam", "虚拟/生产机位钉扎排除"),
                      ("splitcam", "splitcam 排除（真机抓获的漏网）"),
                      ("不落盘不出机", "隐私声明"), ("_injectGesture", "无摄像头注入测试钩"),
                      ("demoCtx", "demo 合成数据"), ("\"scene\"", "demo 场景参数"),
                      ("/api/alerts/ack", "ack 请求路径"),
                      ("/vendor/tasks-vision/vision_bundle.mjs", "主线程动态 import（绝对路径）"),
                      ("recognizeForVideo", "视频推理调用"),
                      ("/vendor/models/gesture_recognizer.task", "模型绝对路径")]:
        check(f"I 手势/demo {why}", mark in html)
    for mark, why in [("/vendor/", "vendor 静态路由"), ("application/wasm", "wasm MIME"),
                      ("/api/alerts/ack", "ack 端点"), ("import alerts", "判据同源 import 锚")]:
        check(f"I 服务 {why}", mark in srv)
    atxt = ALERTS.read_text(encoding="utf-8", errors="replace")
    check("I alerts.ack_alert 在位", "def ack_alert" in atxt)
    check("I raise_alert 持续期保留 ack", "\"ack\", \"ack_ts\", \"ack_str\", \"ack_by\"" in atxt)
    check("I xiaojie 事件带 key/ack", "\"key\": k" in xj and "\"ack\": bool" in xj)

    # J. 手机手势站（手机集群方案·阶段一，2026-08-07）
    mon = MONITOR.read_text(encoding="utf-8", errors="replace")
    for mark, why in [("/station/offer", "站位 offer 路由"), ("/station/beat", "电量心跳路由"),
                      ("_stations_snapshot", "注册表快照"), ("phones_live.json", "台账落盘"),
                      ("STATION_MAX", "阶段一 4 路硬闸"), ("_STATION_ID_RE", "站位名白名单")]:
        check(f"J 中继 {why}", mark in mon)
    check("J 中继 换脸单机位零触碰", "_cam_frame: np.ndarray | None = None" in mon
          and "@app.post(\"/webrtc/cam/offer\")" in mon)
    # 阶段二首件（导播墙）：空位即入口 + 导播踢出 + /show 第③码卡
    for mark, why in [("/wall", "导播墙路由"), ("/station/kick", "导播踢出"),
                      ("空位 · 扫码入列", "空位即入口"), ("当手势站 · 集群机位", "/show 第③码卡"),
                      ("isSecureContext", "安全上下文判据（localhost 可调试）"),
                      ("auto=1", "常驻位自动续推")]:
        check(f"J 导播墙 {why}", mark in mon)
    for mark, why in [("\"gsrc\"", "手势源参数"), ("/station_feed/", "同源代理路径"),
                      ("startPhoneSource", "手机源启动"), ("手机机位", "手机机位章文案"),
                      ("geomGesture", "几何手势判定（真机视角实证：内置分类仰拍全 None）"),
                      ("GRACE:400", "dwell 抖动宽限"),
                      ("pcanvas.width=phoneImg.naturalWidth", "竖屏画布跟随不变形")]:
        check(f"J HUD {why}", mark in html)
    check("J 服务 station_feed 代理", "/station_feed/" in srv)

    # K. P3 切片（舰桥星座/三视图/绑定可视化/板 ack 补显，2026-08-07 三期）
    for mark, why in [("id=\"cons\"", "星座画布"), ("cycleView", "三视图循环"),
                      ("drawCons", "星座绘制"), ("CONS_ANG", "五边形拓扑角（与壁纸星座同源）"),
                      ("粉点=手机手势站", "卫星图例"), ("/api/stations", "卫星数据源"),
                      ("站位「", "绑定关系明示（销 UX 债）"), ("chip-cam.wait", "等待入列琥珀态"),
                      ("PARAMS.get(\"view\")", "view 直达参数（实拍/营销）"),
                      ("ArrowRight", "键盘平权切视图")]:
        check(f"K P3 {why}", mark in html)
    check("K 服务 stations 透传", "/api/stations" in srv)
    check("K 板 ack 补显", "for hhmm, title, col, acked in events" in board)

    # L. 秒级遥测（P3 遥测切片，2026-08-07 四期）
    agent = HERE / "telemetry_agent.ps1"
    check("L agent 文件在位", agent.is_file())
    atext = agent.read_text(encoding="utf-8", errors="replace") if agent.is_file() else ""
    check("L agent 纯 ASCII", agent.is_file() and all(ord(ch) < 128 for ch in atext))
    check("L agent 单进程 loop 模式", "-l 3" in atext and "telemetry.pid" in atext)
    sentinel = (ROOT / "tools" / "hud" / "sentinel.ps1").read_text(encoding="utf-8", errors="replace")
    check("L 哨兵 v5 看门狗", "telemetry_agent.ps1" in sentinel and "telemetry.pid" in sentinel)
    deploy = (ROOT / "tools" / "deploy_hud.ps1").read_text(encoding="utf-8", errors="replace")
    check("L 部署随包 agent", "telemetry_agent.ps1" in deploy)
    for mark, why in [("/api/telemetry", "接收端点"), ("TELEM_FRESH_S", "新鲜窗常量"),
                      ("telem_fresh", "覆盖面计数"), ("\"telem\": telem_ages", "心跳带遥测龄")]:
        check(f"L 服务 {why}", mark in srv)
    check("L HUD 遥测脚注", "遥测" in html and "秒级" in html)

    # M. 遥测调制与营销包（P3 四期收官，2026-08-08）
    check("M 星座脉冲逐机相位（随 util 调速）", "consPhases" in html)
    check("M 显存弧平滑", "consSmooth" in html)
    check("M 星座温度标签", "°C" in html)
    for tool in ["hud_shot_pack.py", "hud_state_shots.py"]:
        p = HERE / tool
        okc = p.is_file()
        if okc:
            try:
                py_compile.compile(str(p), doraise=True)
            except Exception:
                okc = False
        check(f"M 工具可编译 {tool}", okc)
    pack = (HERE / "hud_shot_pack.py").read_text(encoding="utf-8", errors="replace")
    check("M 营销包排除导播墙（IP 纪律）", "/wall" in pack and "不入包" in pack)

    # N. P0 展演骨架（2026-08-08）：tour 引擎 / 遥控页 / 编排层 / 埋点 / 周报
    tour_p = HERE / "tour.html"
    check("N tour.html 在位", tour_p.is_file())
    tour = tour_p.read_text(encoding="utf-8", errors="replace") if tour_p.is_file() else ""
    check("N 遥控页零外链", tour != "" and not re.findall(r"https?://", tour))
    for mark, why in [("/api/tour", "切幕接口"), ("/api/preflight", "预检接口"),
                      ("pin", "PIN 支持"), ("第${x.n}幕", "幕次卡"),
                      ("prev", "上一幕"), ("next", "下一幕"),
                      ("T0", "只有呈现动词的声明")]:
        check(f"N 遥控页 {why}", mark in tour)
    for mark, why in [("TOUR_STEPS", "幕次表"), ("def set_tour", "切幕函数"),
                      ("_tour_cond", "Condition 即时唤醒（波纹节拍防相位差）"),
                      ("notify_all", "SSE 唤醒广播"), ("srv_epoch", "服务端时钟（编排对时）"),
                      ("TOUR_HTML", "遥控页路由文件"), ("/api/preflight", "预检端点"),
                      ("HUD_TOUR_PIN", "PIN 环境变量"), ("/api/stat", "埋点端点"),
                      ("hud_usage.jsonl", "周报数据文件"), ("USAGE_MAX_BYTES", "埋点截尾闸")]:
        check(f"N 服务 {why}", mark in srv)
    check("N srv_epoch 剔出变更键（防 SSE 空转）", '"ts", "age_s", "srv_epoch"' in srv)
    for mark, why in [("id=\"ripple\"", "波纹层"), ("id=\"banner\"", "欢迎横幅"),
                      ("id=\"curtain\"", "谢幕暗场"), ("id=\"chip-tour\"", "幕次章"),
                      ("TOUR_NAMES", "幕次名同源"), ("playWelcome", "开场编排"),
                      ("srvSkew", "钟差校正"), ("selfIdx()*0.45", "0.45s/屏 接力节拍"),
                      ("_injectTour", "展演注入钩（实拍/验证）"),
                      ("function stat(", "埋点信标"), ("if(DEMO) return", "demo 不落账"),
                      ("sendBeacon", "信标通道"), ("handleTour", "幕次处理")]:
        check(f"N HUD {why}", mark in html)
    check("N 编排推流硬闸", html.count('state.mode==="frozen"') >= 3
          and '["ripple","banner","curtain"]' in html)
    check("N demo 展演直达参数", 'PARAMS.get("tour")' in html)
    shots_txt = (HERE / "hud_state_shots.py").read_text(encoding="utf-8", errors="replace")
    check("N 实拍矩阵含展演两态", "tour_welcome" in shots_txt and "tour_curtain" in shots_txt)
    m_srv = re.search(r"TOUR_STEPS\s*=\s*\[([^\]]+)\]", srv)
    m_hud = re.search(r"TOUR_NAMES=\[([^\]]+)\]", html)
    same = bool(m_srv and m_hud) and (
        [s.strip().strip('"') for s in m_srv.group(1).split(",")]
        == [s.strip().strip('"') for s in m_hud.group(1).split(",")])
    check("N 幕次表两侧同源", same,
          f"srv={m_srv and m_srv.group(1)} hud={m_hud and m_hud.group(1)}")
    rep_p = HERE / "hud_usage_report.py"
    okr = rep_p.is_file()
    if okr:
        try:
            py_compile.compile(str(rep_p), doraise=True)
        except Exception:
            okr = False
    check("N 周报工具可编译 hud_usage_report.py", okr)
    check("N 周报挂号观察名单", okr and "WATCHED" in
          rep_p.read_text(encoding="utf-8", errors="replace"))

    # O. P1 语音指挥（「小界」唤醒 · 2026-08-08）：vendor 资产 / 网关 / 采集端 / 双闸 / 周报
    vdir = HERE / "vendor" / "voice"
    kwsd = vdir / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    asrd = vdir / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    for rel, why in [(kwsd / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", "KWS 编码器"),
                     (kwsd / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx", "KWS 解码器"),
                     (kwsd / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx", "KWS joiner"),
                     (kwsd / "tokens.txt", "KWS tokens"),
                     (asrd / "model.int8.onnx", "SenseVoice int8"),
                     (asrd / "tokens.txt", "ASR tokens")]:
        check(f"O vendor {why}", rel.is_file() and rel.stat().st_size > 1024)
    check("O fp32 冗余模型已清（937MB 省盘纪律）", not (asrd / "model.onnx").exists())
    kwf = vdir / "keywords_xiaojie.txt"
    kwt = kwf.read_text(encoding="utf-8", errors="replace") if kwf.is_file() else ""
    check("O 唤醒词=小界（ppinyin 生成）", "x iǎo j iè" in kwt and "@小界" in kwt)
    vgp = HERE / "voice_gateway.py"
    try:
        py_compile.compile(str(vgp), doraise=True)
        check("O voice_gateway 可编译", True)
    except Exception as e:  # noqa: BLE001
        check("O voice_gateway 可编译", False, str(e)[:120])
    vgt = vgp.read_text(encoding="utf-8", errors="replace") if vgp.is_file() else ""
    for mark, why in [('KWS_THRESHOLD = float(os.environ.get("HUD_KWS_TH", "0.15"))',
                       "阈值（探针下限 0.15 给远场留余量,2026-08-13 现场定档;HUD_KWS_TH 可调）"),
                      ("lazy_pinyin", "拼音归一（ASR 别字免疫）"),
                      ("yinsheng", "韵声别字别名（音声实锤）"),
                      ("VERBS", "动词白名单（P2 ops_gateway 前身）"),
                      ("\"checkup\"", "体检动词"), ("\"ack\"", "确认告警动词"),
                      ("\"intro\"", "品牌讲解固定词"), ("INTRO_SAY", "讲解词文本"),
                      ("verdict == \"danger\"", "推流拒收二闸"),
                      ("T3_DENY", "T3 禁区明确拒答（不进 LLM）"),
                      ("不落盘", "隐私口径"), ("RING_S", "唤醒回灌环形缓冲"),
                      ("voice_wake", "唤醒埋点"), ("voice_intent", "意图埋点"),
                      ("voice_deny", "拒答埋点"), ("_llm_fallback", "小界 LLM 兜底"),
                      ("def configure", "依赖注入口")]:
        check(f"O 网关 {why}", mark in vgt)
    check("O 网关零反向 import（防循环）", "import hud_server" not in vgt)
    for mark, why in [("/api/voice/stream", "流式路由"), ("/api/voice/state", "就绪态路由"),
                      ("/api/voice/tts/", "应答音频路由"), ("import voice_gateway", "模块挂载"),
                      ("LAST_VERDICT", "忙态缓存（8Hz 入口性能地板）"),
                      ("vg.configure", "依赖注入"), ("\"voice\": vg.status()", "心跳带语音态"),
                      ("ctx[\"voice\"]", "快照带语音相位")]:
        check(f"O 服务 {why}", mark in srv)
    for mark, why in [("PARAMS.get(\"voice\")", "voice=1 逐机 opt-in"),
                      ("VIRTUAL_MIC", "麦克风钉扎黑名单"),
                      ("voicemeeter", "voicemeeter 排除（普查实锤）"),
                      ("cable", "CABLE 排除（系统默认麦=同传管道）"),
                      ("AudioContext({sampleRate:16000})", "16k 采集"),
                      ("audioWorklet", "Worklet 采集管线"),
                      ("id=\"vorb\"", "聆听球"), ("id=\"voicebar\"", "六屏字幕带"),
                      ("SELF!==\"zhongshu\"", "中枢默认禁播应答"),
                      ("stopVoice", "冻结连带停麦"), ("_injectVoice", "语音注入钩"),
                      ("语音内存识别·不落盘", "隐私话术"),
                      ("voicestate", "demo 语音态直达"), ("dimHold", "调暗覆盖位")]:
        check(f"O HUD {why}", mark in html)
    check("O 遥控页幕3解除候场", tour.count("hold:1") == 1 and "小界" in tour)
    check("O 实拍矩阵含语音三态", "voice_listen" in shots_txt and "voice_deny" in shots_txt)
    doc = Path(r"C:\模仿音色\doctor.py").read_text(encoding="utf-8", errors="replace")
    check("O doctor 语音指挥牌", "语音指挥" in doc and "vo.get(\"ready\")" in doc)
    check("O 周报挂号语音三键", "voice_wake" in rep_p.read_text(encoding="utf-8", errors="replace"))

    # P. P2 隔空指挥（2026-08-08）：四重护栏 / 白名单 / 审计先行 / 双入口 / 台账
    ogp = HERE / "ops_gateway.py"
    try:
        py_compile.compile(str(ogp), doraise=True)
        check("P ops_gateway 可编译", True)
    except Exception as e:  # noqa: BLE001
        check("P ops_gateway 可编译", False, str(e)[:120])
    ogt = ogp.read_text(encoding="utf-8", errors="replace") if ogp.is_file() else ""
    for mark, why in [("\"wol\":", "白名单 wol"), ("\"svc_restart\":", "白名单 svc_restart"),
                      ("\"route_switch\":", "白名单 route_switch"),
                      ("审计写入失败", "审计先行（写不进即拒）"),
                      ("FUSE_MAX_FIRES", "熔断"), ("DISABLE_FLAG", "应急总闸旗标"),
                      ("ARM_TTL_S = 8", "武装 8s 自动撤防"), ("UNDO_TTL_S = 10", "10s 撤销窗"),
                      ("_hub_busy_now", "点火实时忙态二查"), ("fail-closed", "拿不到忙态即拒"),
                      ("verdict != \"safe\"", "T2 忙态否决"),
                      ("FACESWAP_TARGETS", "路由白名单（拒任意 URL）"),
                      ("只救死的", "svc_restart 只动离线服务"),
                      ("src not in (\"gesture\", \"voice\", \"key\", \"mouse\")", "在场信号授权（遥控不点火）"),
                      ("AUTH_MODE", "在场信号授权常量（刷脸层 2026-08-21 已拆除）"),
                      ("ops_audit.jsonl", "审计流文件"),
                      ("def recent_events", "操作即叙事（拼事件流）")]:
        check(f"P 网关 {why}", mark in ogt)
    check("P 网关零反向 import（防循环）", "import hud_server" not in ogt)
    for mark, why in [("/api/ops/", "指挥路由"), ("import ops_gateway", "模块挂载"),
                      ("ctx[\"ops\"]", "快照带指挥相位"), ("og.recent_events(2)", "事件流拼接"),
                      ("\"ops\": og.state()", "心跳带指挥态"), ("og.configure", "依赖注入")]:
        check(f"P 服务 {why}", mark in srv)
    vgt2 = vgp.read_text(encoding="utf-8", errors="replace")
    for mark, why in [("def _match_ops", "指挥句式"), ("ops_confirm", "语音确认=第二段点火"),
                      ("ops_cancel", "语音撤防"), ("\"ops_undo\"", "语音撤销"),
                      ("SVC_ALIASES", "服务别名表"), ("说「确认」点火", "两段式话术")]:
        check(f"P 语音 {why}", mark in vgt2)
    try:
        order_ok = vgt2.index("ops = _match_ops") < vgt2.index("if any(w in t for w in T3_DENY)")
    except ValueError:
        order_ok = False
    check("P 语音 指挥句式先于 T3 判定（重启数字人=武装，重启脸备=拒）", order_ok)
    voice_ops_verbs = set(re.findall(r"\"ops_verb\": \"(\w+)\"", vgt2))
    og_verbs = set(re.findall(r"^    \"(\w+)\":\s*\{\"tier\"", ogt, re.M))
    check("P 语音句式动词 ⊆ 网关白名单（两侧同源）",
          bool(voice_ops_verbs) and voice_ops_verbs <= og_verbs,
          f"voice={sorted(voice_ops_verbs)} gateway={sorted(og_verbs)}")
    for mark, why in [("id=\"opscard\"", "武装卡"), ("opsArmed", "武装态判定"),
                      ("fireOps", "点火入口"), ("disarmOps", "撤防入口"), ("undoOps", "撤销入口"),
                      ("if(opsArmed()) fireOps(\"gesture\")", "竖拇指武装态优先点火"),
                      ("e.key===\"Enter\"", "回车点火（平权）"),
                      ("opsstate", "demo 指挥态直达"), ("_injectOps", "指挥注入钩")]:
        check(f"P HUD {why}", mark in html)
    try:
        mj = json.loads(Path(r"D:\boundless\deploy\machines.json").read_text(encoding="utf-8"))
        macs_ok = all(m.get("mac") for m in mj.get("machines", []))
    except Exception:
        macs_ok = False
    check("P 台账 六机 MAC 齐全（WOL 前提）", macs_ok)
    check("P doctor 隔空指挥牌", "隔空指挥" in doc)
    check("P 周报挂号指挥四键", "ops_arm" in rep_p.read_text(encoding="utf-8", errors="replace"))
    check("P 实拍矩阵含指挥两态", "ops_armed" in shots_txt and "ops_fired" in shots_txt)

    # Q. P2b 刷脸授权（2026-08-21 用户拍板整体拆除——face_auth.py 留作独立工具，
    #    网关/服务端/HUD 零引用=负向锚）+ P3 手机万能外设（2026-08-08）
    fap = HERE / "face_auth.py"
    try:
        py_compile.compile(str(fap), doraise=True)
        check("Q face_auth 可编译", True)
    except Exception as e:  # noqa: BLE001
        check("Q face_auth 可编译", False, str(e)[:120])
    fat = fap.read_text(encoding="utf-8", errors="replace") if fap.is_file() else ""
    for mark, why in [("buffalo_l", "insightface 模型（gallery_audit 同款）"),
                      ("FACE_THRESH = 0.40", "阈值判据同源"),
                      ("secrets", "登记表入 secrets（不入库红线）"),
                      ("def enroll", "登记入口") if "def enroll" in fat else ("enroll", "登记入口"),
                      ("HUD_OPS_AUTH", "应急压制阀"), ("不落盘", "帧隐私口径"),
                      ("登记即生效", "零配置叠加层")]:
        check(f"Q 刷脸 {why}", mark in fat)
    # 拆除断言（2026-08-21）：功能面零 face_auth 引用；点火在场信号收编鼠标（点击即切的服务端半边）
    check("Q 网关 刷脸层已拆除（零 import face_auth）", "import face_auth" not in ogt)
    check("Q 网关 刷脸帧参数已拆除", "frame_b64" not in ogt)
    check("Q 网关 点火在场信号含鼠标", "\"mouse\"" in ogt)
    check("Q 网关 点火记名审计", "fired_by" in ogt)
    for name, anchors in [("join.html", ["角色即租约", "手势站", "扬声器", "魔杖", "监看", "展演遥控"]),
                          ("speaker.html", ["wakeLock", "开嗓", "EventSource", "告警提示音"]),
                          ("wand.html", ["requestPermission", "deviceorientation", "校准"])]:
        p = HERE / name
        txt = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        check(f"Q 角色页 {name} 在位", p.is_file())
        check(f"Q 角色页 {name} 零外链", txt != "" and not re.findall(r"https?://(?!\$\{)", txt.replace("https://${hub()}", "")))
        for a in anchors:
            check(f"Q {name} {a}", a in txt)
    for mark, why in [("JOIN_HTML", "join 路由"), ("SPEAKER_HTML", "speaker 路由"),
                      ("WAND_HTML", "wand 路由"), ("/api/roles/beat", "角色心跳"),
                      ("/api/wand", "魔杖坐标"), ("ROLES_FRESH_S", "角色新鲜窗"),
                      ("ctx[\"wands\"]", "魔杖布尔级广播"),
                      ("setdefault(\"role\", \"gesture\")", "角色合并（中继手势站）")]:
        check(f"Q 服务 {why}", mark in Path(HUD_SRV).read_text(encoding="utf-8", errors="replace"))
    for mark, why in [("checkWand", "魔杖光标"), ("/api/wand/", "魔杖拉取"),
                      ("speaker:\"#E8B341\"", "卫星角色着色")]:
        check(f"Q HUD {why}", mark in html)
    check("Q HUD 刷脸抓帧已拆除", "grabAuthFrame" not in html)
    check("Q 周报挂号 phone_role", "phone_role" in rep_p.read_text(encoding="utf-8", errors="replace"))
    srv2 = Path(HUD_SRV).read_text(encoding="utf-8", errors="replace")
    check("Q 预检带点火授权项", "点火授权" in srv2)
    check("Q 快照带授权模式（武装卡提示语）", "ctx[\"ops\"][\"auth\"]" in srv2)
    check("Q 武装卡纯在场信号提示语", "竖拇指 1.2s / 回车 / 说「确认」＝点火" in html)

    # R. 展演收官（2026-08-09）：提词卡 + 全场营销包
    rs = HERE / "runsheet.html"
    check("R 提词卡在位", rs.is_file())
    rst = rs.read_text(encoding="utf-8", errors="replace") if rs.is_file() else ""
    check("R 提词卡零外链", rst != "" and not re.findall(r"https?://", rst))
    for mark, why in [("/api/tour", "跟随幕次"), ("主持人说", "台词栏"),
                      ("主持人做", "动作栏"), ("如果翻车", "逃生门栏"),
                      ("const ACTS=[", "八幕剧本"), ("localStorage.hudTourPin", "PIN 兼容")]:
        check(f"R 提词卡 {why}", mark in rst)
    check("R 提词卡幕数=7", rst.count('{ac:') == 7)
    check("R 服务 runsheet 路由", "RUNSHEET_HTML" in srv2 and "/runsheet" in srv2)
    check("R 遥控页链提词卡", "/runsheet" in tour)
    pack2 = (HERE / "hud_shot_pack.py").read_text(encoding="utf-8", errors="replace")
    for mark, why in [("show_welcome", "开场欢迎入包"), ("show_ops_armed", "武装 money shot 入包"),
                      ("show_ops_fired", "点火态入包"), ("show_voice", "语音态入包"),
                      ("show_curtain", "谢幕品牌时刻入包"), ("join_phone", "手机入口入包")]:
        check(f"R 营销包 {why}", mark in pack2)
    check("R 营销包仍排除导播墙（IP 纪律）", "/wall" not in pack2 or "不入包" in pack2)
    check("R doctor 点火授权牌", "点火授权" in srv2)

    # S. 六屏逐屏在线（P3b 演示可靠性 · 2026-08-09）
    for mark, why in [("def screens()", "屏在线聚合"), ("_screens", "连接注册表"),
                      ("_screens.pop(conn_id", "断开即摘"), ("\"screens\": screens()", "随 /api/tour 下发"),
                      ("六屏在线", "预检逐屏项"),
                      ("modes = [m for m in modes if m in", "输入模式白名单")]:
        check(f"S 服务 {why}", mark in srv2)
    check("S HUD 自报机器+模式", 'EventSource("/api/stream"+_q)' in html and "&modes=" in html)
    for name in ("runsheet.html", "tour.html"):
        t = (HERE / name).read_text(encoding="utf-8", errors="replace")
        check(f"S {name} 六屏灯", "paintScreens" in t and "d.screens" in t and "MODE_ICON" in t)

    # T. 展演全链演练（P3b 收官 · 2026-08-09）：常驻端到端 drill + doctor 牌
    dp = HERE / "showcase_drill.py"
    try:
        py_compile.compile(str(dp), doraise=True)
        check("T showcase_drill 可编译", True)
    except Exception as e:  # noqa: BLE001
        check("T showcase_drill 可编译", False, str(e)[:120])
    dt = dp.read_text(encoding="utf-8", errors="replace") if dp.is_file() else ""
    for mark, why in [("drill_tour", "幕次段"), ("drill_screens", "六屏段"),
                      ("drill_ops_guards", "指挥护栏段"), ("drill_face", "刷脸段"),
                      ("drill_roles", "手机角色段"), ("drill_voice", "语音段"),
                      ("--live-ops", "真点火链 opt-in"), ("默认零生产写", "安全纪律声明"),
                      ("showcase_drill.json", "报告落盘")]:
        check(f"T 演练 {why}", mark in dt)
    check("T 真点火链藏在 opt-in 后（默认不点火）", "if a.live_ops" in dt and "drill_ops_live" in dt)
    check("T doctor 展演演练牌", "展演演练" in doc and "showcase_drill.json" in doc)

    # U. 隔空拉屏 P0（只读镜像+激光笔 · 2026-08-09）
    dap = HERE / "desktop_agent.py"
    try:
        py_compile.compile(str(dap), doraise=True)
        check("U desktop_agent 可编译", True)
    except Exception as e:  # noqa: BLE001
        check("U desktop_agent 可编译", False, str(e)[:120])
    dat = dap.read_text(encoding="utf-8", errors="replace") if dap.is_file() else ""
    # 只读铁证：agent 里绝不能有任何输入注入的**实际用法**（import/调用形式；docstring 里
    # 声明「刻意不含 SendInput/pynput…」的裸词提及不算——那正是本 P0 的安全承诺文本）。
    INJECT = ["import pynput", "import pyautogui", "import keyboard", "SendInput(",
              "keybd_event(", "mouse_event(", "SetCursorPos(", "windll.user32", "ctypes.windll"]
    hit = [w for w in INJECT if w in dat]
    check("U agent 零输入注入用法（P0 只读铁证）", not hit, f"发现注入用法：{hit}")
    for mark, why in [("def wanted", "按需捕获（问中枢要不要）"),
                      ("if not wanted", "没人看不截屏（不打扰生产）"),
                      ("read-only", "只读声明"), ("MAX_W", "下采样省带宽")]:
        check(f"U agent {why}", mark in dat)
    # 帧不落盘：内存 BytesIO 编码（img.save(buf,...) 是内存不算落盘；只拦 imwrite / save 到路径串）
    check("U agent 帧不落盘（内存编码）", "io.BytesIO(" in dat and "imwrite" not in dat
          and ".save(\"" not in dat and ".save(r\"" not in dat)
    for mark, why in [("/api/desktop/push", "推帧端点"), ("/api/desktop/wanted", "按需闸"),
                      ("/api/desktop/want", "想看刷新"), ("/api/desktop/list", "在推列表"),
                      ("DESKTOP: dict", "屏帧内存表"), ("WANT: dict", "想看表"),
                      ("b\"\\xff\\xd8\"", "JPEG SOI 校验"), ("DESKTOP_MAX_BYTES", "帧大小闸")]:
        check(f"U 服务 {why}", mark in srv)
    for mark, why in [("function deskGrab", "拉屏"), ("deskLaserAt", "激光笔覆盖层"),
                      ("mid===SELF", "中枢不拉自己（决策点5）"),
                      ("只读镜像", "只读标"), ("不向远端发送任何输入", "只读铁证话术"),
                      ("仅内网流转不落盘", "隐私脚注"), ("_injectDeskGrab", "拉屏验证钩"),
                      ("?t=\"+Date.now()", "帧防缓存") if "?t=\"+Date.now()" in html else ("pullDeskFrame", "拉帧轮询")]:
        check(f"U HUD {why}", mark in html)
    check("U TDZ 根治：输入模式常量前置", "TDZ 根治" in html
          and html.index("const GEST_WANT=") < html.index("EventSource(\"/api/stream\""))
    # agent 用 mss+PIL（可移植优化，跨异构 ML venv 滚动摩擦最小）——不引 cv2/numpy
    check("U agent 用 mss+PIL 不引 cv2/numpy", "from PIL import Image" in dat
          and "import cv2" not in dat and "import numpy" not in dat)
    dep = HERE / "deploy_desktop_agent.ps1"
    check("U 部署助手在位", dep.is_file())
    dept = dep.read_text(encoding="utf-8", errors="replace") if dep.is_file() else ""
    check("U 部署助手纯 ASCII（无 BOM 顾虑）", dept != "" and all(ord(c) < 128 for c in dept))
    check("U 部署助手用 onlogon 交互任务（ssh 截黑屏教训）", "/it" in dept and "onlogon" in dept)
    check("U 部署助手不碰 ML venv（standalone python 参数化）", "-Py" in dept)
    # P0b grab 手势化：星座指向-dwell 抓桌面 + 鼠标点节点平权
    for mark, why in [("consNodeVP", "星座节点视口坐标"), ("function nodeAt", "节点命中"),
                      ("function grabTick", "指向-dwell 抓取"), ("GRAB_DWELL", "抓取 dwell 阈值"),
                      ("consHover", "抓取悬停高亮"),
                      ("nodeAt(e.clientX", "鼠标点节点平权")]:
        check(f"U grab 手势 {why}", mark in html)
    # 拉屏可用性可见（P0b+ · 2026-08-10）：agent 轮询即存活信号 → 可拉屏指示 + doctor 覆盖牌
    for mark, why in [("AGENTS[mid] = time.time()", "轮询记存活（零改 agent）"),
                      ("ctx[\"pullable\"]", "可拉屏随快照下发"), ("AGENT_FRESH_S", "存活窗"),
                      ("\"pull\": sorted", "心跳带覆盖")]:
        check(f"U 服务 {why}", mark in srv)
    check("U HUD 可拉屏指示", "pullable" in html and "未部署拉屏" in html)
    check("U grab 只抓可拉屏机", "mid&&pullable&&pointing" in html)
    check("U doctor 拉屏覆盖牌", "拉屏覆盖" in doc)
    # P0c 桌面墙（多屏只读拉屏；全程只读零注入）
    for mark, why in [("id=\"wall\"", "墙容器"), ("function wallOpen", "开墙"),
                      ("function pullWall", "墙拉帧"), ("wallClose", "关墙"),
                      ("每块 ~3fps", "墙低帧礼待被拉机"), ("view===2) wallOpen", "星座双手撑开开墙"),
                      ("仅内网流转不落盘", "墙只读隐私脚注"), ("_injectWallOpen", "墙验证钩")]:
        check(f"U 桌面墙 {why}", mark in html)

    # ---- P2 隔空受控（输入注入；2026-08-10 主人拍板：脸备/口型/声备）----
    cap = HERE / "control_agent.py"
    try:
        py_compile.compile(str(cap), doraise=True)
        check("V control_agent 可编译", True)
    except Exception as e:  # noqa: BLE001
        check("V control_agent 可编译", False, str(e)[:120])
    cat = cap.read_text(encoding="utf-8", errors="replace") if cap.is_file() else ""
    # 分家铁证之一：注入代码只在 control_agent；desktop_agent 仍必须零注入（P0 铁证不因 P2 破功）
    check("V desktop_agent 仍零注入（分家铁证）", "windll.user32" not in dat and "SendInput(" not in dat)
    # 分家铁证之二：control_agent 确实是注入器（有 SendInput）但 fail-closed 且密钥闸
    for mark, why in [("SendInput", "确为注入器（ctypes）"), ("X-Ctrl-Secret", "密钥闸"),
                      ("fail-closed", "失效即停声明"), ("def selftest", "无害自测"),
                      ("MAX_EPS", "自卫限速"), ("def dispatch", "事件→注入单一接缝"),
                      ("_send(inp", "唯一系统出口（可 monkeypatch）"),
                      ("granted", "仅授予时才注入")]:
        check(f"V control_agent {why}", mark in cat)
    check("V control_agent 零第三方依赖（stdlib+ctypes）",
          "import mss" not in cat and "import cv2" not in cat and "import numpy" not in cat
          and "pyautogui" not in cat and "pynput" not in cat)
    check("V control_agent 无密钥拒跑（fail-closed）", "refuse to run" in cat)
    # 网关四重护栏（会话式；判据全在服务端 ops_gateway）
    ogt2 = ogp.read_text(encoding="utf-8", errors="replace") if ogp.is_file() else ""
    for mark, why in [("CTRL_TARGETS = {\"lianbei\", \"kouxing\", \"shengbei\"}", "白名单硬编码（生产机永久只读）"),
                      ("def ctrl_arm", "武装"), ("def ctrl_action", "动作"),
                      ("def ctrl_pull", "agent 领取"), ("def ctrl_disarm", "撤防"),
                      ("CTRL_DISABLE_FLAG", "受控专用总闸"),
                      ("verdict != \"safe\"", "推流硬闸"),
                      ("_ctrl_end(f\"busy", "会话中途开播即硬切"),
                      ("CTRL_FUSE_MAX", "动作熔断"), ("_audit(\"ctrl_act\"", "点按逐条审计"),
                      ("secret != want", "密钥不符不投递"),
                      ("_ctrl_q[mid] = []", "未授权清空积压（绝不迟发）"),
                      ("_ctrl_alive", "受控 agent 在线信号")]:
        check(f"V 网关 {why}", mark in ogt2)
    # 服务端路由 + SSE 六屏同源 + 心跳给 doctor
    for mark, why in [("/api/ctrl/", "受控路由"), ("og.ctrl_arm", "武装挂载"),
                      ("og.ctrl_action", "动作挂载"), ("og.ctrl_pull", "agent 拉挂载"),
                      ("X-Ctrl-Secret", "密钥透传"), ("ctx[\"ctrl\"]", "受控相位随快照"),
                      ("og.ctrl_state()", "心跳带受控态")]:
        check(f"V 服务 {why}", mark in srv2)
    # HUD 受控层
    for mark, why in [("function ctrlArm", "武装（在场信号，刷脸层已拆除）"), ("function ctrlActionAt", "发动作"),
                      ("function ctrlDisarm", "撤防"), ("function ctrlArmed", "会话活判定"),
                      ("ctrlCanControl", "可控判定（白名单∩已部署）"),
                      ("armed", "琥珀武装边框"),
                      ("竖拇指授权", "授权手势话术"), ("if(m===\"frozen\") ctrlDisarm", "推流冻结即撤防"),
                      ("ctrlDisarm(\"close\")", "关窗即撤防"),
                      ("_injectCtrlArm", "武装验证钩"), ("_injectCtrlAction", "动作验证钩")]:
        check(f"V HUD 受控 {why}", mark in html)
    check("V 受控部署助手在位且纯 ASCII", (HERE / "deploy_control_agent.ps1").is_file()
          and all(ord(c) < 128 for c in (HERE / "deploy_control_agent.ps1").read_text(encoding="utf-8", errors="replace")))
    check("V 受控部署助手白名单拒非法机 + 交互任务",
          "REFUSE" in (HERE / "deploy_control_agent.ps1").read_text(encoding="utf-8", errors="replace")
          and "/it" in (HERE / "deploy_control_agent.ps1").read_text(encoding="utf-8", errors="replace"))
    check("V doctor 隔空受控牌", "隔空受控" in doc)

    # ---- P2b 语音听写→远端 type + Victory 滚动手势（2026-08-10）----
    vgt3 = vgp.read_text(encoding="utf-8", errors="replace") if vgp.is_file() else ""
    for mark, why in [("def set_dictate", "听写开关"), ("def _handle_dictation", "听写路由"),
                      ("_dictate", "听写状态位"), ("_DICT_NAV", "导航词表"),
                      ("_DICT_OFF", "退出词"), ("先武装某台机", "开听写需先武装"),
                      ("if _dictate[\"on\"]", "关时早返回（管道零改动）"),
                      ("ctrl_dictate", "打字依赖"), ("ctrl_key", "按键依赖")]:
        check(f"V2 语音听写 {why}", mark in vgt3)
    for mark, why in [("def ctrl_dictate", "往会话打字"), ("def ctrl_key_active", "往会话按键"),
                      ("def ctrl_active_machine", "当前受控机"),
                      ("_ctrl_inject_active", "进程内可信注入复用全闸")]:
        check(f"V2 网关 {why}", mark in ogt2)
    check("V2 服务 听写开关端点", "/api/voice/dictate" in srv2 and "vg.set_dictate" in srv2)
    check("V2 服务 听写依赖注入", "ctrl_dictate=og.ctrl_dictate" in srv2
          and "ctrl_active=og.ctrl_active_machine" in srv2)
    check("V2 服务 ctx.voice 带 dictate", "ctx[\"voice\"][\"dictate\"]" in srv2)
    # ---- P0 三维全息舰桥（vanilla three.js，离线自托管，无构建；三视角方案 20260811 §P0）----
    three_js = HERE / "vendor" / "three" / "three.module.js"
    check("W three.js 离线自托管在位", three_js.is_file() and three_js.stat().st_size > 200_000)
    if three_js.is_file():
        tjs = three_js.read_text(encoding="utf-8", errors="replace")
        check("W three.js 为 ESM(export) 且含 WebGLRenderer", "export{" in tjs and "WebGLRenderer" in tjs)
    for mark, why in [("function webglOK", "WebGL 可用探测"), ("function initHolo", "懒加载初始化"),
                      ("function renderHolo", "逐帧渲染"), ("function buildHoloNodes", "六机场景构建"),
                      ("function holoCardTex", "节点仪表卡"),
                      ("import(\"/vendor/three/three.module.js\")", "离线自托管懒加载(禁 CDN)"),
                      ("id=\"holo\"", "全息容器"), ("holoview", "全息视图 body 类"),
                      ("function viewCount", "视图数随 WebGL 可用性"),
                      ("holoOK=webglOK()", "启动即探测"),
                      ("holoOK=false; if(view===3){ view=2", "WebGL/导入失败秒回退 2D"),
                      ("view===3&&holoS.renderer) renderHolo", "仅 full(rAF) 渲染=生产忙态自然让位"),
                      ("_holoColor", "节点色数据驱动(复用 utilColor/ST_TXT)"),
                      ("CONS_ANG[m.id]", "复用星座同源角度/数据"),
                      ("_injectHoloView", "全息验证钩")]:
        check(f"W 全息 {why}", mark in html)
    # ---- P1 手控声控接进 3D（复用手势/语音/拉屏基座；三视角方案 20260811 §P1）----
    for mark, why in [("function holoPick", "屏幕→射线拾取"), ("Raycaster", "three 射线"),
                      ("setFromCamera", "相机射线投射"), ("userData.mid", "拾取球回映机器 id"),
                      ("SphereGeometry(rad*1.55", "隐形宽容拾取球"),
                      ("function holoGrabTick", "指向 dwell 抓取(镜 grabTick)"),
                      ("GRAB_DWELL){ holoBleep(); deskGrab", "dwell 满→拉屏(复用)"),
                      ("pullable&&pointing", "只抓可拉屏机(复用判据)"),
                      ("holoHover", "悬停高亮态"), ("orbitManual", "捏合拖拽轨道偏转"),
                      ("function holoFocusNode", "声控聚焦→拉屏"),
                      ("if(view===3){ holoFocusNode(a.m)", "语音机器意图复用(全息态飞向节点)"),
                      ("function holoBleep", "抓取音效(WebAudio 无外部资产)"),
                      ("const mid=holoPick(e.clientX,e.clientY)", "鼠标点节点平权"),
                      ("_injectHoloPick", "拾取验证钩"), ("_injectHoloFocus", "聚焦验证钩")]:
        check(f"W2 手控声控 {why}", mark in html)
    # ---- P2 JARVIS 化视觉 + 算力模式联动（三视角方案 20260811 §P2）----
    for mark, why in [("function holoMat", "全息材质工厂"), ("ShaderMaterial", "自定义着色器(无 addon)"),
                      ("float fres=pow", "Fresnel 边缘辉光"), ("float scan=0.5", "扫描线"),
                      ("FogExp2", "景深雾"), ("方舟反应堆", "中枢反应堆环"),
                      ("TorusGeometry(rad*(1.35", "反应堆环几何"), ("holoS.rings", "环集合"),
                      ("holoS.reveal", "揭幕亮起动画"), ("1-Math.pow(1-rp,3)", "easeOutCubic 揭幕(逐节点)"),
                      ("innerMat.uniforms.uColor", "全息核色 uniform 更新"),
                      ("clustermode||{}), acc=cm.accent||", "算力模式联动配色"),
                      ("算力模式 · ", "模式绶带话术")]:
        check(f"W3 全息视觉 {why}", mark in html)
    check("W3 服务 ctx.clustermode 暴露", "ctx[\"clustermode\"]" in srv2 and "cluster_mode.json" in srv2)
    # ---- 收尾打磨（三视角方案 20260811 §P2 收尾）：tour 招牌镜头 + 错峰揭幕 + 彩色绶带 + 链路模式色 ----
    for mark, why in [("if(holoOK&&view!==3&&state.mode===\"full\"){ view=3", "欢迎幕自动切全息(生产忙态不夺主权)"),
                      ("revDelay:isHub?0", "错峰揭幕延迟(中枢先亮)"),
                      ("holoS.reveal-(nd.revDelay", "逐节点级联揭幕"),
                      ("hint.style.borderLeft=\"3px solid \"+col", "彩色绶带(模式/姿态色边条)"),
                      ("lk.line.material.color.set(alert?\"#DE3C46\":acc)", "链路随模式色/告警变色")]:
        check(f"W4 收尾 {why}", mark in html)
    # ---- P1+ 交互升级（三视角方案 20260811 §P1 backlog）：双手俯仰/缩放 + 遮挡感知拾取 ----
    for mark, why in [("holoS.zoom", "双手缩放"), ("holoS.pitch", "双手俯仰"),
                      ("holoS.thb={span,midY,ang}", "双手基准(拦在全息不落墙/详情分支;2026-08-18 三轮+扭转角)"),
                      ("*(holoS.zoom||1)", "相机应用缩放"), ("+(holoS.pitch||0)+Math.sin", "相机应用俯仰"),
                      ("遮挡感知", "遮挡感知拾取"), ("best.d < Math.max(30", "投影中心最近阈值"),
                      ("holoS.zoom=1; holoS.pitch=0", "进入复位相机"),
                      ("deskGrab(holoHover", "捏合抓悬停节点(自然抓取，真机走查反馈)"),
                      ("t.step===1&&holoOK&&view!==3", "欢迎幕进行中刷新落回3D")]:
        check(f"W5 全息交互 {why}", mark in html)
    # ---- P2+ 真 Bloom（UnrealBloomPass 自托管 addon 树 + import map + 硬回退保零回归）----
    addon_pp = HERE / "vendor" / "three" / "addons" / "postprocessing"
    addon_sh = HERE / "vendor" / "three" / "addons" / "shaders"
    for fn in ["EffectComposer.js", "RenderPass.js", "UnrealBloomPass.js", "OutputPass.js",
               "Pass.js", "ShaderPass.js", "MaskPass.js"]:
        check(f"W6 addon {fn} 自托管", (addon_pp / fn).is_file() and (addon_pp / fn).stat().st_size > 200)
    for fn in ["CopyShader.js", "LuminosityHighPassShader.js", "OutputShader.js"]:
        check(f"W6 shader {fn} 自托管", (addon_sh / fn).is_file())
    for mark, why in [("type=\"importmap\"", "import map(离线)"),
                      ("\"three\":\"/vendor/three/three.module.js\"", "裸 three 映射自托管(同 URL 同实例)"),
                      ("const BLOOM=PARAMS.get(\"bloom\")", "bloom 开关(?bloom=0 关)"),
                      ("import(\"/vendor/three/addons/postprocessing/EffectComposer.js\")", "懒加载自托管 addon(禁 CDN)"),
                      ("new UnrealBloomPass", "真 bloom pass"), ("new OutputPass", "色彩输出 pass"),
                      ("holoS.composer=comp", "composer 挂载"),
                      ("holoS.composer=null; console.warn", "硬回退(失败即直接渲染)"),
                      ("if(holoS.composer) holoS.composer.render(); else renderer.render", "渲染路径二选一"),
                      ("if(holoS.composer) holoS.composer.setSize", "composer 随窗 resize")]:
        check(f"W6 真Bloom {why}", mark in html)
    # ---- 手势自视小窗（真机走查缺口：手势"盲指"→看得见地指）----
    for mark, why in [("id=\"selfcam\"", "自视小窗容器"), ("function drawSelfCam", "绘制骨架/指尖/姿态名"),
                      ("HAND_CONN", "手部骨架连线"), ("GNAME_ZH", "姿态名中文映射"),
                      ("$(\"selfcam\").classList.add(\"on\")", "手势开=显小窗"),
                      ("drawSelfCam(src,out.landmarks", "逐帧绘制挂 pumpFrame")]:
        check(f"W7 自视 {why}", mark in html)
    # holo CSS 去重（曾 6 份重复）：#holo 定义唯一
    check("W7 holo CSS 无重复", html.count("#holo{display:none;position:relative;width:100%;height:296px}") == 1)
    # ---- P0 二次美化（真机反馈：光太强/球太丑/无未来感/功能太少）----
    for mark, why in [("function holoChipTex", "静息紧凑章(去卡片拥挤)"), ("function holoCardTex", "悬停完整仪表卡"),
                      ("function _hbar", "读数条(GPU/显存/温度)"), ("function _hbrk", "四角框(HUD 语言)"),
                      ("function _htemp", "温度热力色(蓝→红)"),
                      ("hov?holoCardTex(THREE,m):holoChipTex", "悬停展开/静息收拢(渐进披露)"),
                      ("id=\"holovig\"", "暗角容器"), ("#holovig{", "暗角径向渐变"),
                      ("new THREE.Points", "星野景深"), ("PointsMaterial", "星点材质"),
                      ("wireframe:true,transparent:true,opacity:0.4", "暗线框笼架(不参与 bloom)"),
                      (",0.5,0.5,0.9)", "bloom 降强升阈(灭洗白)")]:
        check(f"W8 二次美化 {why}", mark in html)
    # ---- P0b 能量核着色器：六边形能量网格 + 噪声呼吸 ----
    for mark, why in [("float hexEdge", "六边形网格函数"), ("float vnoise", "程序噪声"),
                      ("vUvS", "球面 UV(投六边形)"), ("六边形能量网格", "能量护盾注释"),
                      ("uColor*(0.5+fres*1.3+hex*0.6)", "Fresnel+hex 合成发光")]:
        check(f"W9 能量核 {why}", mark in html)

    for mark, why in [("function ctrlDictateToggle", "听写开关"), ("function voiceDictating", "听写态读取"),
                      ("id=\"deskdict\"", "听写章"), ("name===\"Victory\"", "剪刀手滚动手势"),
                      ("\"scroll\"", "远端滚轮动作"), ("scrollY", "滚动纵向跟踪"),
                      ("说「停打字」退出", "听写指示话术")]:
        check(f"V2 HUD {why}", mark in html)

    # W10 P1 功能加厚：节点操作盘（从孪生体直接指挥）+ 集群总览条 —— 动作全复用既有通道，
    # 危险动词只做「武装」（确认仍走竖拇指/回车=两段式零旁路；模式条例外=点击直通 2026-08-21）
    for mark, why in [("id=\"holopanel\"", "操作盘 DOM"), ("id=\"holostat\"", "总览条 DOM"),
                      ("function openHoloPanel", "开盘函数"), ("function closeHoloPanel", "收盘函数"),
                      ("function buildHoloStat", "总览条构建"),
                      ("deskGrab(mid,\"panel\")", "拉屏复用"), ("openDetail(m,\"panel\")", "详情复用"),
                      ("fireAck(al.key,\"panel\")", "确认告警复用"),
                      ("holoArm(\"wol\"", "唤醒走 ops 武装"), ("holoArm(\"route_switch\"", "切路由走 ops 武装"),
                      ("toast(\"武装被拒：", "武装被拒 toast 反馈"),
                      ("contextmenu", "鼠标右键平权"), ("holoLastHov", "粘性悬停(换姿不丢标)"),
                      ("hvSticky", "竖拇指用粘性悬停"), ("btn.click()", "捏合按下面板按钮"),
                      ("集群显存", "总览条显存水位"),
                      ("if(doLabels) buildHoloStat()", "总览条节流同拍"),
                      ("closeHoloPanel(); else collapseAll(\"key\")", "Esc 先收盘")]:
        check(f"W10 操作盘 {why}", mark in html)
    # 护栏不旁路：面板/下钻共用 holoArm=只 arm（fire 侧只认 gesture/voice/key/mouse 在场信号；
    # 模式条在自己的 click 里接 fire 直通（2026-08-21 拍板），面板/下钻仍两段式）
    _pnl = html.split("function openHoloPanel", 1)[1].split("function closeHoloPanel", 1)[0]
    _arm = html.split("function holoArm", 1)[1].split("\n}", 1)[0]
    check("W10 操作盘 面板只武装不点火", "holoArm(" in _pnl and "opsPost(\"fire\"" not in _pnl
          and "opsPost(\"arm\"" in _arm and "by:\"holo\",src:\"mouse\"" in _arm
          and "opsPost(\"fire\"" not in _arm)

    # W11 P2 未来感叙事：全息 chrome（角框+扫描带）+ 目标锁定准星 + 告警红脉冲（真事件驱动）+ 语音开盘
    for mark, why in [("id=\"holosweep\"", "扫描带 DOM"), ("@keyframes hsweep", "扫描带动画"),
                      ("class=\"hcb tl\"", "角框左上"), ("class=\"hcb br\"", "角框右下"),
                      ("id=\"holoret\"", "准星 DOM"), ("@keyframes hlockin", "锁定 snap-in"),
                      ("· 已锁定", "锁定名牌"), ("const rt=$(\"holoret\"), tgt=holoPanelMid||holoHover",
                       "准星靶优先级(选中>悬停)"),
                      ("holoS.alertSet", "告警集(0.8s 同拍)"),
                      ("alert?\"#DE3C46\"", "告警脉冲变红"), ("alert?1.5", "告警脉冲加速"),
                      ("a.act===\"panel\"&&a.m", "语音 panel 动作处理"),
                      ("function holoPanelWhenReady", "开盘就绪等待"),
                      ("if(!holoOK){", "WebGL 缺席退详情卡")]:
        check(f"W11 叙事 {why}", mark in html)
    # 语音网关：panel 动词在 detail 之前（「打开X的操作盘」含 detail 触发词「打开」,dict 序=优先级）
    check("W11 语音 panel 动词注册", "\"panel\":" in vgt and "操作盘" in vgt)
    check("W11 语音 panel 先于 detail", vgt.find("\"panel\":") != -1
          and vgt.find("\"panel\":") < vgt.find("\"detail\":"))
    check("W11 语音 panel 应答", "的操作盘。" in vgt and "{\"act\": \"panel\", \"m\": mid}" in vgt)

    # W12 P3 逐层下钻：ctx.svc 数据端（归属 cluster_map 拓扑 SSOT + 死活 Hub /health +
    # 可救=svc_restart 合法集同源）+ 子星环绕 + 三色语义 + 拥挤自适应 + 聚焦 + 救活只武装
    for mark, why in [("def svc_by_machine", "服务清单聚合"),
                      ("og._dead_engines()", "可救合法集=ops 同一函数(判据单一真相)"),
                      ("cluster_map.json", "归属=拓扑 SSOT"), ("/health", "死活=Hub 健康"),
                      ("ctx[\"svc\"] = svc_by_machine()", "快照带 svc"),
                      ("保留上次数据", "Hub 掉线保上次(诚实降级)")]:
        check(f"W12 下钻数据 {why}", mark in srv)
    for mark, why in [("function holoDrillEnter", "进入下钻"), ("function holoDrillExit", "退出下钻"),
                      ("function holoPickSub", "子星拾取"), ("function holoDrillAct", "子星动作"),
                      ("_HSUB_COL", "三色语义表"), ("park", "泊车态"),
                      ("holoS.zoom=Math.min", "下钻即聚焦"), ("zoom0", "退出还原相机"),
                      ("crowded?(inner2?0.86:1.36):1.10", "拥挤双环珠牌(方案C 双列→开坛双环沿革)"),
                      ("hov?0.55:0.42", "拥挤渐进披露=悬停放大读铭文"),
                      ("id!==holoDrill.mid", "其余机器淡出"),
                      ("id=\"holodrill\"", "下钻章 DOM"), ("开坛 · ", "开坛章文案"),
                      ("不在可救清单", "泊车人话"), ("holo_drill", "下钻埋点"),
                      ("else if(holoDrill) holoDrillExit()", "父节点消失安全退出")]:
        check(f"W12 下钻 {why}", mark in html)
    # 救活=只武装（arm svc_restart），下钻体内无点火
    _drl = html.split("function holoDrillAct", 1)[1].split("\n}", 1)[0]
    check("W12 下钻 救活只武装不点火",
          "holoArm(\"svc_restart\"" in _drl and "opsPost(\"fire\"" not in _drl)

    # W13 P4 展演飞览（欢迎幕×全息=逐机巡览,零服务端改动）+ 语音下钻 + 营销 2x 档
    for mark, why in [("function holoTourBeat", "节拍函数"), ("tr.step!==1", "寄生欢迎幕"),
                      ("beats.push({mid:hub,d:12,drill:true})", "中枢拍自动下钻"),
                      ("(tr.at||0)+srvSkew", "时间轴锚服务端+钟差(六屏同帧)"),
                      ("typeof holoDrag!==\"number\"", "捏合拖拽中让位"),
                      ("holoS.tourFly=false", "幕切走一次性清场"),
                      ("if(!holoS.tourFly) holoS.orbit+=dt*0.12", "飞览期停自动轨道"),
                      ("飞览节拍钉死", "headless 落位快进"),
                      ("PARAMS.get(\"tourat\")", "时间轴回拨直达"),
                      ("a.act===\"drill\"&&a.m", "语音下钻动作处理"),
                      ("function holoDrillWhenReady", "下钻就绪等待"),
                      ("else openHoloPanel(mid,src)", "无编目服务退操作盘")]:
        check(f"W13 飞览 {why}", mark in html)
    check("W13 语音 drill 动词注册", "\"drill\":" in vgt and "下钻" in vgt and "的服务" in vgt)
    check("W13 语音 drill 序位(panel<drill<detail)",
          -1 < vgt.find("\"panel\":") < vgt.find("\"drill\":") < vgt.find("\"detail\":"))
    check("W13 语音 drill 应答", "已开坛拆解" in vgt and "{\"act\": \"drill\", \"m\": mid}" in vgt)
    shots_t = (HERE / "hud_state_shots.py").read_text(encoding="utf-8", errors="replace")
    check("W13 营销 2x 档", "--marketing" in shots_t and "force-device-scale-factor" in shots_t
          and "tour_flyby" in shots_t)

    # W14 真麦钉扎+视图记忆（2026-08-12 现场工单：喊小界没响应+F5 丢 3D。
    # 复盘：识别链端到端健康（合成样本 KWS→ASR→意图→应答全绿）、麦在推流有底噪——
    # 根因=PD100X 动圈麦近讲设计,坐姿距离人声到不了唤醒阈;修法=?mic= 换 BRIO 或凑近说）
    for mark, why in [("ah_hud_mic", "麦选择记忆(?mic= 落 localStorage)"),
                      ("\"podcast\",\"pd100\",\"usb\"", "真麦优先级链(USB 兜底)"),
                      ("vMicLabel", "选中麦名亮在语音章"),
                      ("ah_hud_view", "视图记忆(F5 回上次)"),
                      ("sv>=1&&sv<viewCount()", "恢复守卫(3D 要 WebGL 在场)"),
                      ("else if(!DEMO){ try{", "demo 不恢复(实拍确定性)")]:
        check(f"W14 现场修 {why}", mark in html)
    # 拾音观测（同工单防线：muted 麦=精确 0 / 底噪≈0.003+ / 人声 0.05+,从此可读不用猜）
    check("W14 拾音观测 会话级 RMS/峰值入 status",
          "\"mics\":" in vgt and "s[\"rms\"]" in vgt and "s[\"pk\"]" in vgt)

    # W15 算力模式姿态巡检（P0 · 2026-08-13：模式=声明,声明≠现实曾静默 1.5 天——
    # 期望=modes.json 派生/探测只读/判据在 tools/cluster_posture.py,slo_watch 10min 巡+告警,
    # HUD/壁纸同读 logs/cluster_posture.json）
    for mark, why in [("cluster_posture.json", "姿态文件接入 ctx"),
                      ("\"last_ok\": _lr.get(\"ok\")", "上次切换结果入 ctx"),
                      ("30min 龄内才认", "新鲜度闸(巡检停摆不装健康)")]:
        check(f"W15 姿态数据 {why}", mark in srv)
    for mark, why in [("cm.posture||null", "绶带读姿态"),
                      ("姿态失守", "失守红字"), ("姿态观察", "观察琥珀"),
                      ("姿态健康", "健康常态"), ("· 切换中 ", "切换中琥珀(带 n/N 步进度)"),
                      ("const modeKey=cm.mode+", "姿态变化触发重绘")]:
        check(f"W15 姿态绶带 {why}", mark in html)

    # W16 P1 算力模式切换进指挥链（2026-08-13：看得到/守得住→切得动。武装点火之外，
    # 执行不带 force——执行器闸门受理时再实判；刷脸层 2026-08-21 拆除、模式条点击直通）
    for mark, why in [("\"mode_switch\":  {\"tier\": \"T2\", \"reversible\": False}", "动词注册 T2 不可撤销"),
                      ("def _cluster_mode_now", "SSOT 同源读取"),
                      ("已经是", "同模式拒绝人话"),
                      ("dry_run\": True", "武装带执行器闸门预览"),
                      ("\"reason\": \"隔空指挥（ops_gateway 武装点火）\"", "执行带记名原因"),
                      ("执行器拒绝：", "409 闸门转人话")]:
        check(f"W16 切模式 {why}", mark in ogt)
    check("W16 切模式 执行不越闸(无 force:true)", "\"force\": True" not in
          ogt.split("if verb == \"mode_switch\":", 2)[-1].split("return False, \"unreachable\"")[0])
    for mark, why in [("\"模式\" in t", "语音强特征词"),
                      ("必须先于换脸路由判定", "撞词序位注释"),
                      ("\"ops_verb\": \"mode_switch\"", "语音武装动词")]:
        check(f"W16 语音切模式 {why}", mark in vgt)
    check("W16 语音 模式判定先于换脸路由",
          vgt.find("\"ops_verb\": \"mode_switch\"") < vgt.find("\"ops_verb\": \"route_switch\""))
    for mark, why in [("holoArm(\"mode_switch\"", "中枢盘按钮走武装"),
                      ("算力切到", "按钮文案"),
                      ("mid===\"zhongshu\"&&cmode", "集群级动作锚中枢"),
                      ("姿态失守 <b>", "总览条失守徽章"), ("姿态 <b>✓", "总览条健康徽章")]:
        check(f"W16 HUD {why}", mark in html)

    # W16b 三模式+姿态归位（2026-08-14 互斥三模式联动收口）：模式按钮由服务端 modes 表
    # 数据驱动（增删模式零改前端）、归位=T2 动词走 Hub /api/cluster/repair（复用执行器
    # 全部护栏，ops 层永不带 force——force 语义只活在修复端点内）、语音可问模式、
    # 绶带切换中带 n/N 步进度。
    for mark, why in [("\"posture_repair\": {\"tier\": \"T2\", \"reversible\": False}", "归位动词注册 T2"),
                      ("if verb == \"posture_repair\":", "归位执行分支"),
                      ("/api/cluster/repair", "归位走执行器修复端点")]:
        check(f"W16b 归位 {why}", mark in ogt)
    for mark, why in [("\"ops_verb\": \"posture_repair\"", "语音归位武装"),
                      ("\"mode_query\"", "模式问询动词(T0 只读)"),
                      ("说「一键归位」可以拉回", "问询指路归位")]:
        check(f"W16b 语音 {why}", mark in vgt)
    for mark, why in [("(cmm.modes||[]).filter", "三模式按钮数据驱动(服务端 modes 表)"),
                      ("holoArm(\"posture_repair\"", "归位按钮走武装")]:
        check(f"W16b HUD {why}", mark in html)
    check("W16b 服务端 模式表下发(zh/accent)", "\"modes\": [{\"id\": k" in srv)

    # W17 C0 U 型指挥舱编排（2026-08-13：cockpit.json=屏↔机↔座次 SSOT;甩桌面跨屏/告警飞中央/空间声像）
    ckp = Path(r"D:\boundless\deploy\boundless-hud\cockpit.json")
    try:
        ckd = json.loads(ckp.read_text(encoding="utf-8-sig"))
        scr = ckd.get("screens") or []
        ids = [s.get("id") for s in scr]
        orders = [s.get("order") for s in scr]
        mach_ok = True
        try:
            mj = json.loads(Path(r"D:\boundless\deploy\machines.json")
                            .read_text(encoding="utf-8-sig"))
            known = {m.get("id") for m in mj.get("machines") or []}
            mach_ok = all(s.get("machine") in known for s in scr)
        except Exception:
            pass
        check("W17 台账 可解析且 ≥2 屏", len(scr) >= 2)
        check("W17 台账 id 唯一", len(ids) == len(set(ids)))
        check("W17 台账 座次唯一", len(orders) == len(set(orders)))
        check("W17 台账 恰一块中央屏", sum(1 for s in scr if s.get("center")) == 1)
        check("W17 台账 machine 全在 machines.json", mach_ok)
    except Exception as e:  # noqa: BLE001
        check("W17 台账 可解析且 ≥2 屏", False, str(e)[:80])
    for mark, why in [("COCKPIT_JSON", "台账路径"), ("def cockpit_neighbor", "座次邻接推导(不双写)"),
                      ("/api/cockpit/throw", "甩桌面端点"), ("HANDOFF", "接力单槽"),
                      ("_tour_cond.notify_all()", "SSE 即时唤醒"),
                      ("ctx[\"cockpit\"] = ck", "台账入快照"),
                      ("U 型尽头", "无邻屏人话")]:
        check(f"W17 服务 {why}", mark in srv)
    for mark, why in [("PARAMS.get(\"screen\")", "?screen= 绑屏"),
                      ("function ckSelf", "认座(缺参回退按机)"), ("function ckNeighbor", "邻接查询"),
                      ("function throwDesk", "甩桌面"), ("function deskFling", "甩出动画"),
                      ("function handleHandoff", "接力认领"), (">10) return", "接力新鲜度闸"),
                      ("deskGrab(h.machine,\"handoff\")", "目标屏自动拉屏"),
                      ("function cockpitAlertFx", "告警飞行编排"), ("function alertFlyer", "飞行章"),
                      ("function spatialBeep", "空间声像"),
                      ("function alertMachineOf", "告警归属判据收口"),
                      ("alertMachineOf(e)===mid", "操作盘用同一把尺"),
                      ("id=\"dthrowl\"", "邻屏按钮(鼠标平权)"),
                      ("GEST.SWIPE_V*1.15", "疾扫甩送(阈值高于切视图防误甩)"),
                      ("h.src===\"drill\"", "演练甩送页面静默(防扰生产屏)"),
                      ("id=\"dthrowtv\"", "上大屏直达按钮"),
                      ("上大屏 · ", "上大屏文案带屏名")]:
        check(f"W17 HUD {why}", mark in html)
    # 2026-08-13d：电视演示大屏=命名甩送目标（173 电视在左端、日常位在右端,邻接跳七跳不现实）
    check("W17 台账 恰一块电视大屏",
          sum(1 for s in (ckd.get("screens") or []) if s.get("tv")) == 1)
    check("W17 服务 命名目标 tv/center",
          "(\"left\", \"right\", \"tv\", \"center\")" in srv and "台账没有电视大屏" in srv)
    # 2026-08-13 工单固化：预检必须能看出「几屏在听/听的哪支麦」——voice=1 逐屏 opt-in,
    # 跑单漏参=全场没耳朵,「说了小界没响应」不能再靠人猜
    check("W17 预检 语音采集屏数", "语音采集" in srv and "0 屏在听" in srv)

    # W19 C1 多目接力·指挥权仲裁（2026-08-13：多摄像头同时看到操作者时只有一块屏响应——
    # 各屏本地识别保延迟,只上报手感质量心跳,中枢纯函数仲裁唯一指挥权,迟滞防抖接力）
    for mark, why in [("def gest_arbitrate", "仲裁纯函数"), ("GEST_FRESH_S = 1.5", "候选新鲜窗"),
                      ("GEST_KEEP = 0.75", "迟滞连任系数"), ("/api/gest/beat", "心跳端点"),
                      ("ctx[\"gestlead\"]", "指挥权入快照"), ("不在台账", "野屏心跳人话拒")]:
        check(f"W19 服务 {why}", mark in srv)
    for mark, why in [("function gestBeat", "心跳上报"), ("function gestQuality", "手感质量"),
                      ("function gestSuppressed", "压制判定"), ("id=\"chip-gestlead\"", "压制章"),
                      ("指挥权在", "压制话术"), ("PARAMS.get(\"gestlead\")!==\"0\"", "退路开关")]:
        check(f"W19 HUD {why}", mark in html)
    check("W10 操作盘 实拍直达(快进揭幕)", "PARAMS.get(\"holopanel\")" in html
          and "holoS.reveal=1" in html)

    # W20 首屏直给（2026-08-16：控制台徽章撤下后 HUD 成鼠标切模式主入口，但入口此前只在
    # 三维全息中枢盘=第一屏找不到实锤）：模式条/视图页签/界面版本自愈/事件区鼠标平权补全。
    for mark, why in [("id=\"modebar\"", "模式条 DOM"),
                      ("function renderModeBar", "模式条渲染"),
                      ("holoArm(\"mode_switch\"", "切模式走武装口"),
                      ("opsPost(\"fire\",{id:r.id,src:\"mouse\"})", "点击即点火直通(2026-08-21 拍板)"),
                      ("cm.modes||[]).filter(x=>x.id!==cm.mode)", "按钮数据驱动(服务端 modes 表)"),
                      ("body.evx #modebar,body.consview #modebar,body.holoview #modebar",
                       "仅第一屏可见"),
                      ("if(!cm.mode){ el.style.display=\"none\"", "未配置机整条隐藏(零回归)"),
                      ("cmFb=", "武装行内回执(成功指引/被拒原因常驻)"),
                      ("id=\"viewtabs\"", "视图页签 DOM"),
                      ("function buildViewTabs", "页签数据驱动 viewCount"),
                      ("src:\"tab\"", "页签埋点(与方向键同键分源)"),
                      ("id=\"verpill\"", "版本自愈胶囊 DOM"),
                      ("assetMismatchAt", "版本漂移检测(首帧基线)"),
                      ("location.reload()", "自愈刷新动作"),
                      ("class=\"ack\"", "告警单击确认钮(双击平权保留)"),
                      ("function relTime", "事件相对时间"),
                      ("vreason\").classList.toggle(\"open\")", "判定理由可展开"),
                      ("编制满载", "高水位语义拆分(姿态健康=编制内)"),
                      ("cmstate", "模式条实拍直达参数")]:
        check(f"W20 首屏直给 {why}", mark in html)
    check("W20 服务端 asset_ver 下发", "ctx[\"asset_ver\"]" in srv)

    # W21 电视台档+全屏舞台+帮助层（P0 · 2026-08-18 电视台化方案）：HUD 的日常岗位——
    # 非展演时段自动巡览+告警插播+切换叙事；全息 3D 铺满视口收面板成悬浮 chrome；
    # 操作词表卡治「知者自知」。让位纪律与秒回退是本节命根。
    for mark, why in [("const AUTOPLAY_P=PARAMS.get(\"autoplay\")", "电视台档显式参数(优先)"),
                      ("function apWanted", "台账 SSOT 驱动(tv 屏缺参自动开)"),
                      ("return !!(s&&s.tv)", "tv=true 即开播判据"),
                      ("function apEngaged", "让位判定收口(单一函数)"),
                      ("tr.step>0) return false", "真展演一票让位"),
                      ("AP_IDLE_S", "用户操作静默窗"),
                      ("function apFlyBeats", "并联节拍表(与 holoTourBeat 同构)"),
                      ("holoTourBeat()||apBeat()", "并联入口(真 tour 优先)"),
                      ("ap.alert={key:e.key,mid:amid,until:now+10}", "告警插播 10s 聚焦"),
                      ("stat(\"autoplay_alert\"", "插播埋点"),
                      ("stat(\"autoplay_on\"", "开播曝光埋点"),
                      ("function apSwitchTick", "切换/暖机叙事卡"),
                      ("id=\"apswitch\"", "叙事卡 DOM"),
                      ("id=\"chip-ap\"", "电视台在岗章"),
                      ("function stageWanted", "全屏舞台判定(缺省跟随电视台档)"),
                      ("function apStageTick", "舞台结算(动态切换+首开曝光埋点)"),
                      ("body.stagefull.holoview #holo{position:fixed;inset:0", "3D 铺满视口"),
                      ("body.stagefull.holoview #panel{width:100vw", "面板收成 chrome"),
                      ("stageOnNow&&view===3)) stepRain", "舞台档雨层省笔"),
                      ("id=\"helpcard\"", "帮助层 DOM"),
                      ("function buildHelp", "词表卡构建"),
                      ("stat(\"help_open\"", "帮助层埋点"),
                      ("e.key===\"?\"", "? 键唤出"),
                      ("if(helpOn()) helpClose()", "Esc 链帮助层居首"),
                      ("?autoplay=0 电视台档关", "回退开关词表(卡内自述)")]:
        check(f"W21 电视台/舞台/帮助 {why}", mark in html)
    try:
        _scr21 = (ckd.get("screens") or [])
        _tv21 = next((s for s in _scr21 if s.get("tv")), {})
        check("W21 台账 tv 屏默认电视台档", "autoplay=1" in str(_tv21.get("params") or ""))
    except Exception as e:  # noqa: BLE001
        check("W21 台账 tv 屏默认电视台档", False, str(e)[:60])

    # W22 导演位+飞览解说+质感包（P1 · 2026-08-18）：遥控页点机器名=飞览聚焦（T0 呈现层，
    # 幕切自动清）；每拍一句解说（小界 TTS 缓存链，字幕永出、出声仅 ?speak=1 屏）；
    # 空中面板方案 P0b 克制子集=音族/物质化/光尾/冲击波（?fx=0 素颜，reduced-motion 全静）。
    for mark, why in [("\"focus\" in payload", "聚焦端点(step 之外的第二动词)"),
                      ("TOUR[\"focus\"]", "聚焦单槽"),
                      ("log_stat(\"tour_focus\"", "聚焦埋点"),
                      ("幕切走一次性清场", "幕切清聚焦"),
                      ("/api/narrate", "解说 TTS 代理端点"),
                      ("_NARR_CACHE", "解说内存缓存"),
                      ("ProxyHandler({})", "LAN 直连绕系统代理(红线纪律)"),
                      ("data[\"kouxing\"]", "Lite 自治机直探(svc 盲区补齐,远端不可救语义)")]:
        check(f"W22 服务 {why}", mark in srv)
    for mark, why in [("_fc&&_fc.m&&holoS.nodes[_fc.m]", "聚焦覆盖节拍"),
                      ("<90) beat={mid:_fc.m", "聚焦 90s 新鲜窗"),
                      ("function apNarr", "解说播报"),
                      ("function narrLineOf", "台词固定模板(缓存可命中)"),
                      ("id=\"narrbar\"", "解说字幕 DOM"),
                      ("const NARRATE", "?narrate=0 退路"),
                      ("if(!SPEAK||DEMO) return", "出声只在 speak 屏·demo 静默"),
                      ("function fxSnd", "合成音族"),
                      ("createStereoPanner", "声像随屏幕 x"),
                      ("function fxRing", "抓取冲击波"),
                      ("const FX=PARAMS.get(\"fx\")!==\"0\"", "?fx=0 素颜开关"),
                      ("@keyframes hmat", "面板物质化 scanline"),
                      ("_fxTrail", "指尖光尾"),
                      (".fxring{display:none}", "reduced-motion 冲击波全静"),
                      ("fxSnd(\"error\")", "被拒错误音(音画联动)")]:
        check(f"W22 HUD {why}", mark in html)
    _tour22 = (HERE / "tour.html").read_text(encoding="utf-8", errors="replace")
    for mark, why in [("id=\"dirrow\"", "导演位按钮区"),
                      ("function postFocus", "聚焦请求"),
                      ("恢复自动巡览", "取消聚焦按钮"),
                      ("d.tour.focus&&d.tour.focus.m", "聚焦态回显")]:
        check(f"W22 遥控页 {why}", mark in _tour22)

    # W23 实控升维+特效层（P2 · 2026-08-18 三轮，拍板「不要视频要实控+炫酷」）：空中面板升维
    # （DOM 盘保判据单一真相：伪 3D 悬浮倾斜+活体跟随+捏标题栏抓移+双手缩放+按钮磁吸+1-Euro）、
    # 数据流粒子层（密度=GPU util 真数据,拒造假流量;预算闸+自适应降档+?fx=0）、双手扭转、
    # 双色光标、「演示状态」语音宏。安静纪律不破：非 full 全停、reduced-motion 全静。
    for mark, why in [("function initFxParticles", "数据流粒子建层"),
                      ("function updateFxParticles", "粒子逐帧更新"),
                      ("function fxBurstAt", "抓取/按下爆发粒"),
                      ("setDrawRange(0,n)", "粒子活跃数裁剪"),
                      ("fx.scale=Math.max(0.25", "帧预算自适应降档(下限0.25)"),
                      ("(5+u*30)", "粒子密度=GPU util 真数据"),
                      ("SHOW?2:1", "演示状态粒子翻倍"),
                      ("function mkEuro", "1-Euro 滤波器"),
                      ("const _euX=mkEuro", "手势光标滤波接线"),
                      ("function setShow", "演示状态开关"),
                      ("id=\"chip-show\"", "演示状态章"),
                      ("a.act===\"show\"", "语音宏客户端接线"),
                      ("holoDrag=\"mov\"", "面板捏合抓移"),
                      ("磁吸：未直中时吸附", "按钮磁吸补偿(44px)"),
                      ("pnS._scale", "双手缩放面板"),
                      ("da*1.15", "双手扭转=偏航"),
                      ("pn.style.transform=\"perspective(", "伪 3D 悬浮倾斜"),
                      ("_pinned=false", "开卡复位摆位"),
                      ("拖标题栏=抓移", "鼠标平权抓移"),
                      ("#holopanel{animation:none}", "reduced-motion 面板全静")]:
        check(f"W23 实控升维 {why}", mark in html)
    for mark, why in [("\"show\":", "演示状态动词注册"),
                      ("{\"act\": \"show\"}", "演示状态动作广播"),
                      ("演示状态已开", "应答话术")]:
        check(f"W23 语音 {why}", mark in vgt)

    # W24 过夜自愈+环幕近似（2026-08-19 晨检：电视位隔夜 clients=0）——
    # kiosk 看门狗保 Edge 进程、SSE/WebGL 页内自刷保僵尸页、C2 按 side 偏航不写猜测 pose。
    _kiosk24 = (HERE / "cockpit_kiosk.ps1").read_text(encoding="utf-8", errors="replace")
    for mark, why in [("[switch]$Watch", "看门狗开关"),
                      ("BoundlessCockpitKioskWatch", "2 分钟保活任务"),
                      ("function Count-Kiosk", "存活计数(活着不杀)"),
                      ("function Test-InteractiveSession", "会话 0 拒拉"),
                      ("SessionId -ne 0", "交互会话判据"),
                      ("GetFullPath($PSCommandPath)", "同路径自拷跳过")]:
        check(f"W24 kiosk {why}", mark in _kiosk24)
    for mark, why in [("function ckCamYaw", "环幕偏航(按 side 推导)"),
                      ("me.tv) return 0", "电视位英雄机位不偏"),
                      ("+ckCamYaw()", "相机接入偏航"),
                      ("webglcontextlost", "GPU 上下文丢失自刷"),
                      ("ui_reload\",{src:\"webgl\"}", "webgl 自刷埋点"),
                      ("let connLostAt=0", "SSE 断连计时"),
                      ("ui_reload\",{src:\"sse\"}", "SSE 90s 自刷埋点")]:
        check(f"W24 HUD {why}", mark in html)

    # W25 页活看门狗（2026-08-19 五轮）：不造 ping 端点——SSE screens 即页活。
    # Edge 进程活 ≠ 页活；hub 不可达 hold；连续两拍 miss 才杀僵尸（宽限 vs 中枢重启）。
    for mark, why in [("GetEmptyWebProxy", "LAN 探测绕系统代理"),
                      ("function Get-LiveNames", "页活名单(/health.screens 优先)"),
                      ("watch ok: page live", "页活即放行"),
                      ("hub unreachable -- hold", "中枢不可达不杀"),
                      ("page zombie -- relaunching", "僵尸页才重拉"),
                      ("miss $miss/2 -- hold", "两拍宽限"),
                      ("kiosk_watch", "重拉埋点"),
                      ("page-alive", "Register 声明页活")]:
        check(f"W25 kiosk {why}", mark in _kiosk24)
    for mark, why in [("def air_snapshot", "页活快照函数"),
                      ("tv_on_air", "心跳/health 电视台在播")]:
        check(f"W25 服务 {why}", mark in srv)

    # W26 模式条排版预算化（2026-08-21 排版错乱五视角方案 P0）：chatx+姿态失守把
    # 「单行禁换行×内容随状态膨胀×面板 940px 定宽」的零余量结构病晒出来——归位钮冲出
    # 面板右缘、武装回执被挤出不可见。修=预算压缩（短时间戳/短按钮文案/失守徽章与归位钮
    # 合体）+flex-wrap 换行保险丝+回执独立行；配套=麦名短化闭合（旧 slice(0,26) 硬切出
    # 括号不闭合的「…Podcast Microp」）、窗口化隐藏屏角 ✕、kiosk 之外的响应式基线。
    for mark, why in [("#modebar{display:none;align-items:center;flex-wrap:wrap",
                       "模式条换行保险丝"),
                      ("cmbtn cmfix", "失守胶囊=告警即动作(徽章与归位合体)"),
                      ("data-t='__drill'", "失守胶囊=先明细后动手(P1-1 演进)"),
                      ("cm.switching?\"<span class='cmpo bad'>", "切换中只亮徽章不给归位钮"),
                      ("const _cmSince", "时间戳短化(全文进 title)"),
                      ("const _cmShort", "按钮短文案(全文进 title)"),
                      ("flex-basis:100%", "回执独立行(失守态曾被挤出不可见)"),
                      ("function _micShort", "麦名短化闭合(全名进语音章 title)"),
                      ("body.windowed #closebtn{display:none}", "窗口化隐藏屏角关闭钮"),
                      ("function _winMode", "窗口化判定(视口贴屏=kiosk/F11)"),
                      ("@media (max-width:1080px)", "响应式基线·窄屏让宽度"),
                      ("@media (max-height:660px)", "响应式基线·矮屏收事件区"),
                      ("cmstate=idle|switching|repair|fired", "实拍直达含点火回执态")]:
        check(f"W26 排版 {why}", mark in html)

    # W27 全息沙盘重做（2026-08-21 五视角方案A「全息指挥沙盘」渐进落地：交互 API 零改动，
    # 只换视觉装配层——灭棉花球/极坐标地台/三段式浮筒/能量拱桥列车/涟漪事件池/物质化揭幕/
    # 2x 名牌/TV 标定/fx=0 蓝图素颜。契约锁的是「结构与数据诚实」不锁调色数值。
    for mark, why in [("function holoFloorMat", "极坐标全息地台材质"),
                      ("function holoArcMat", "弧形仪表环材质"),
                      ("function holoRipple", "涟漪事件池(真事件驱动)"),
                      ("HOLO_FLOOR_Y", "台面高度单一常量"),
                      ("uRip[4]", "涟漪 uniform 池(上限4)"),
                      ("QuadraticBezierCurve3", "能量拱桥曲线"),
                      ("TubeGeometry(curve", "管道几何(替代1px直线)"),
                      ("_icount", "管道揭幕 drawRange 满量程"),
                      ("lk.cars", "数据包列车车厢"),
                      ("cp.x+wob", "粒子沿拱桥曲线飞行"),
                      ("悬浮高度=GPU util 真数据", "高度=第二编码通道"),
                      ("nd.group.position.y=nd.h", "浮筒高度逐帧应用"),
                      ("baseArc", "基座环=显存弧"),
                      ("utilArc", "数据环=GPU 弧"),
                      ("nd.anchor", "锚线 grounding"),
                      ("nd.spot", "投影光斑(假倒影)"),
                      ("uniforms.uMat", "物质化扫描升起 uniform"),
                      ("holoS._evMode", "模式切换冲击波边沿检测"),
                      ("holoS._evAlerts", "告警新燃/息燃涟漪边沿检测"),
                      ("function _holoPR", "TV 档 pixelRatio 自适应"),
                      ("body.holoview #rain", "汉字雨全息视图压暗"),
                      ("#holo::before", "屏幕空间扫描线(CSS 减法暗带)"),
                      ("FX?0.4:0.62", "fx=0 蓝图态线框增强"),
                      ("drillSelf", "下钻聚焦机退紧凑章(去冗余大卡)")]:
        check(f"W27 全息沙盘 {why}", mark in html)

    # W28 下钻层·开坛拆解（2026-08-22 道家法器方案 D2：下钻第三代 子星环绕→数字孪生机柜→开坛验器；
    # ctx.svc 数据面/分诊排序/救活两段式护栏/语音手势通道逐字不变，只换呈现骨架=法器分瓣+灵珠符牌环列）：
    # 珠牌=canvas 竖牌（状态词三件套+服务名+功效铭文+白话+实测显存徽章）；>10 珠双环（对位机柜双列）；
    # 环组缓转朝相机（分诊珠面向操作者）；配套=E 档蓝图深化（uFx 密径线+跑马近静）+B 元素（环进动+光锥扫掠）。
    for mark, why in [("function _hfuTex", "灵珠符牌纹理(机柜槽位条接棒)"),
                      ("开坛拆解", "开坛化注释锚"),
                      ("_rank={fix:0,ok:1,park:2}", "分诊排序(可救顶置)"),
                      ("EdgesGeometry", "线描剪影/笼架"),
                      ("ringG", "珠牌环组(随法器悬浮跟随)"),
                      ("getWorldPosition", "珠牌拾取世界坐标化"),
                      ("捏红牌=救活", "开坛章话术(槽→牌)"),
                      ("捏合=武装救活", "悬停牌救活提字"),
                      ("userData.exd", "分瓣爆炸向量(出生烘焙)"),
                      ("userData.bp", "分瓣基位(收坛精确回位)"),
                      ("uFx", "E 档蓝图 uniform"),
                      ("48 根密径线", "蓝图密径线"),
                      ("rg.rotation.x+=dt*0.022", "反应堆环倾角进动"),
                      ("光锥扫掠", "揭幕光锥扫掠")]:
        check(f"W28 开坛拆解 {why}", mark in html)

    # W29 告警可解释+事件降噪+满载色语义（2026-08-21 五视角方案 P1，表格视图线）：失守胶囊
    # 点开=巡检人话明细+就地归位（值班员不再翻 logs/cluster_posture.json）；>7 天旧闻折叠一行；
    # 姿态健康的「编制满载」显存条走品牌青紫（红色留给真事故）；高水位徽章降为描边章。
    check("W29 服务 失守明细入快照(err_items 各裁6条)", "err_items" in srv)
    for mark, why in [("t===\"__drill\"", "胶囊开合明细(读为先·不进武装链)"),
                      ("class='cmdrill'", "明细块 DOM(模式条整行子块)"),
                      ("const _poLine", "明细人话化(机器id→中文名+IP尾号,剥CLI修复尾)"),
                      ("确认归位 · 按当前模式重放", "明细内归位动词(执行器护栏照常)"),
                      ("||cm.switching) cmDrillOpen=false", "痊愈/切换开始明细自动收"),
                      ("let evOldOpen", "旧闻折叠开关"),
                      ("天前的旧闻", "折叠行文案(点击展开)"),
                      ("_planFull&&f>=.75", "编制满载条色青紫(红留给真事故)"),
                      ("background:transparent;color:var(--amber)", "高水位徽章降噪(描边章)")]:
        check(f"W29 P1 {why}", mark in html)

    # W30 色彩收编+徽章合一（2026-08-21 五视角方案 P2，表格视图线）：模式条按钮静息=
    # 中性文字+识别色点（一行五色的「色彩沙拉」收成一套安静语言，悬停才亮模式色）；
    # 警示动作统一琥珀；.flag 与 .cmpo 徽章规格合一；手势 ack 靶限定未折叠区；
    # 语音章麦名显示位收 16 字（全名 title）。
    for mark, why in [("style='--mac:", "按钮识别色走自定义属性(静息不染文字)"),
                      ("class='bdot'", "模式识别色点(电视距离可辨)"),
                      (".cmbtn.cmwarn{color:var(--amber)", "警示动作统一琥珀(金色退场)"),
                      (".flag{font-size:11.5px;font-weight:800;border-radius:8px",
                       "徽章规格与 cmpo 合一"),
                      ("state.focusKey=(evFresh.find", "ack 靶限定未折叠区(无隐形靶)"),
                      ("vMicLabel.length>16", "语音章麦名收 16 字(全名 title)")]:
        check(f"W30 P2 {why}", mark in html)

    # W31 八卦罗盘中国风重塑（2026-08-21 五视角方案·青铜鎏金观星仪，全息视图线）：盘面=canvas
    # 烘焙鎏金蚀刻叠加层（底层 procedural 地台管揭幕/涟漪/模式环不动）；数据诚实三通道=时辰环
    # 高亮(真时钟)/干支纪日章(真日历,1949-10-01 甲子日锚)/天池寻凶针(真告警指病机,平时隐匿)；
    # 辐条=天圆地方玉琮方框+五行本命色(结构层,病机让位状态色)+卦符佩章(后天卦宫方位派生·台座
    # 铭牌外置)；中枢=太极北辰（保浑天仪线框锚）；汉字雨干支卦符化=全视图世界观统一；
    # 只取方位·计时·星官器物层不上占断内容（D8）。
    for mark, why in [("function holoLuopanTex", "罗盘盘面烘焙"),
                      ("子癸丑艮寅甲卯乙辰巽巳丙午丁未坤申庚酉辛戌乾亥壬", "二十四山正针序列(考据)"),
                      ("HOLO_BAGUA", "后天八卦爻表"),
                      ("HOLO_WUXING", "五行本命表"),
                      ("function holoGanzhiDay", "干支纪日(真日历)"),
                      ("2433191", "甲子日锚(1949-10-01)"),
                      ("function holoShichen", "时辰钟(真时间)"),
                      ("function holoGuaTex", "卦符佩章纹理"),
                      ("function holoTaijiTex", "太极北辰章"),
                      ("寻凶针", "告警指针"),
                      ("holoS._ndlV", "寻凶针显隐驱动(平时隐匿)"),
                      ("function holoRelic", "六器成形工厂(2026-08-21 夜北斗方案 D3：玉琮方框→身份器物+线描剪影)"),
                      ("nd.wxCol||colStr", "五行结构色(病机让位状态色)"),
                      ("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥☰", "汉字雨干支卦符化"),
                      ("回纹", "回纹边带"),
                      ("SpriteMaterial 无图编译后补 map", "精灵材质带图出生纪律(白块实锤)")]:
        check(f"W31 罗盘 {why}", mark in html)

    # W32 观星仪收官（2026-08-21 罗盘方案 P2 余项+P3 转正）：二十八宿星官连线（四象各取一宿，
    # 方位对齐罗盘=天地呼应；宿形示意星图，心宿二按「大火」礼制微红；静态结构层不随星野缓旋）+
    # 飞览解说五行化（固定串保 TTS 缓存）+ 提词卡欢迎幕罗盘解说 + 太极旋纹核（中枢专属 uTaiji，
    # ±20% 亮度双臂回旋，健康色语义不受扰）。
    for mark, why in [("SKY28", "二十八宿数据表"),
                      ("心宿", "东苍龙宿"), ("斗宿", "北玄武宿"),
                      ("参宿", "西白虎宿"), ("井宿", "南朱雀宿"),
                      ("大火", "心宿二微红礼制"),
                      ("holoS._skyO", "星官随揭幕后段点亮"),
                      ("uTaiji", "太极旋纹 uniform(中枢专属)"),
                      ("五行属", "飞览解说五行化"),
                      ("斗为帝车，临制四方", "中枢解说词(北斗方案 D6：北辰居中→帝车)")]:
        check(f"W32 观星仪 {why}", mark in html)
    check("W32 提词卡 罗盘解说入欢迎幕", "罗盘观星仪" in rst and "金针一出" in rst)

    # W33 北斗七星阵与法器成形（2026-08-21 夜 六器方案 · 2026-08-22 道家法器方案换装）：
    # 座次=《晋书·天文志》职掌正典逐席有出处（枢为天=中枢/璇为地=声备/玑为人=脸备/权为时=听写/
    # 衡为音=韵声/阳为律=口型/光为星=虚位），总叙事=《史记·天官书》「斗为帝车」；六法器=号令牌/
    # 三清铃/宝葫芦(一鸣一藏主备对)/敕令符/八卦照妖镜(接棒变脸面具)/法螺，全套 holoMat=全息法器+
    # 同几何线描剪影；管道自天枢席扇出（帝车放射）；摇光虚位以待（扩容席，虚线鎏金不可拾取）；
    # 辅星傅乎开阳（丞相之象彩蛋）；斗柄授时章（鹖冠子，真日期→季节）；CONS_ANG 保留=2D 星座视图兜底。
    for mark, why in [("HOLO_SEAT", "七星座次表(单一真相)"),
                      ("枢为天", "晋书职掌正典注释"),
                      ("玉衡", "衡为音席(韵声)"),
                      ("摇光", "光为星席(虚位)"),
                      ("虚位以待", "扩容席叙事"),
                      ("holoS.douDeco", "斗链+虚位+辅星一体生命周期"),
                      ("斗魁合口", "魁四星闭合刻线"),
                      ("辅星傅乎开阳", "辅星彩蛋(晋书出处)"),
                      ("帝车放射", "管道自天枢席扇出"),
                      ("function holoRelic", "六法器几何工厂"),
                      ("三清铃", "韵声法器(振铃出声)"), ("一鸣一藏", "葫芦=声备法器(成对成语主备对)"),
                      ("敕令符", "听写法器(落音成文)"), ("照妖镜", "脸备法器(鉴形辨妖·接棒变脸面具)"),
                      ("启唇吹号", "法螺=口型法器"),
                      ("function _glyphArtTex", "云篆/爻纹/令字浮层(雅正=抽象笔意)"),
                      ("function _relicEdges", "线描剪影层(白描+光体双层)"),
                      ("nd.relic", "器物与线描同步旋转"),
                      ("function holoDouBiaoTex", "斗柄授时章(真日期)"),
                      ("holoS._dbMo", "授时章跨月换面"),
                      ("斗为帝车，六台机各居北斗一席", "提词卡帝车词")]:
        check(f"W33 北斗 {why}", mark in (html if mark!="斗为帝车，六台机各居北斗一席" else rst))

    # W34 道家法器与开坛拆解（2026-08-22 五视角方案）：世界观收官=罗盘+北斗+太极+六法器=「七星法坛」；
    # 开坛拆解=下钻第三代（手型双捏拉开/操作盘/语音/巡览四入口同一 act 通道，跟手预拆 nd._preX）；
    # 灵珠=真实 AI（引擎∪大模型：hud_server 后台线程 /api/ps 直探实测显存，期望=modes.json 净期望）；
    # 功效词表=静态身份层（前缀匹配，未登记=未铭之灵原名照显）；雅正纪律=云篆抽象笔意不写真符咒。
    for mark, why in [("HOLO_RELIC_ZH", "法器名录(单一真相)"),
                      ("号令牌", "中枢法器(召神遣将=集群调度)"),
                      ("宝葫芦", "声备法器(悬壶储真)"),
                      ("SVC_LORE", "功效词表(静态身份层)"),
                      ("未铭之灵", "词表兜底(原名照显)"),
                      ("function svcLore", "词表前缀匹配"),
                      ("pA<GEST.PINCH_D&&pB<GEST.PINCH_D", "双捏对象通道(与张掌相机通道正交)"),
                      ("_preX", "跟手预拆(松手回合)"),
                      ("gesture2", "双捏埋点源"),
                      ("已收坛", "收坛回执"),
                      ("拆解法器 · ", "操作盘拆解口"),
                      ("内藏 ", "仪表卡内藏N灵(可发现性)"),
                      ("实测 ", "灵珠显存徽章(实测口径)"),
                      ("kind===\"model\"", "大模型灵珠客户端判别"),
                      ("双手捏拉=拆解法器", "holohint 手势提示"),
                      ("双手各捏 · 拉开/合拢", "帮助卡词表行"),
                      ("执号令牌", "飞览解说法器化(固定串)")]:
        check(f"W34 法器 {why}", mark in html)
    for mark, why in [("_merge_model_pearls", "大模型灵珠富化入口"),
                      ("/api/ps", "ollama 装载直探(实测显存)"),
                      ("size_vram", "实测显存字段"),
                      ("wsl_unit_start", "vLLM 健康口收编"),
                      ("\"kind\": \"model\"", "模型珠标记"),
                      ("_pearl_loop", "后台探测线程(SSE 零阻塞)")]:
        check(f"W34 法器 服务端 {why}", mark in srv)
    check("W34 法器 语音同义词(拆解/开坛→drill)", "拆解" in vgt3 and "开坛" in vgt3)
    check("W34 法器 提词卡七星法坛", "七星法坛" in rst and "三清铃" in rst)

    bad = [m for ok, m in results if not ok]
    for ok, m in results:
        print(("PASS " if ok else "FAIL ") + m)
    print(f"== hud_contract_test: {len(results) - len(bad)}/{len(results)} ==")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
