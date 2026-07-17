# -*- coding: utf-8 -*-
"""Phase 12 (A+B+C+D+E) 离线门禁：入口治理 + 同传产品化 + 视觉扩展 + 基础设施 + 商业化包装。"""
import ast
import json
import os
import re
import sys
from pathlib import Path

# gate.py 以管道跑本脚本时 stdout 默认 GBK：任何非 GBK 字符(如 ↔)会让整个门禁崩成红灯。
# 统一重配 utf-8(errors=replace)，门禁的死活只由断言决定，不由控制台编码决定。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
FAIL = []


def ok(msg):
    print(f"  [OK] {msg}")


def ng(msg):
    print(f"  [NG] {msg}")
    FAIL.append(msg)


def test_hair_activate_alias():
    src = (ROOT / "hair_api.py").read_text(encoding="utf-8")
    if '@app.post("/hair_styles/activate")' not in src:
        ng("hair_api 缺少 /hair_styles/activate 别名")
    else:
        ok("hair_api /hair_styles/activate 别名存在")


def test_tryon_in_services():
    cfg = (ROOT / "app_config.py").read_text(encoding="utf-8")
    if '"tryon"' not in cfg or "tryon_api.py" not in cfg:
        ng("app_config 未登记 tryon 服务")
    else:
        ok("app_config 已登记 tryon(8002)")


def test_hub_endpoints_phase_a():
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for path in ("/api/audio/setup_wizard", "/api/lab/services", "/api/clone_engine_recommend"):
        if path not in hub:
            ng(f"avatar_hub 缺少 {path}")
        else:
            ok(f"avatar_hub {path} 存在")


def test_hub_endpoints_phase_b():
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for path in ("/interp/call_pack/start", "/interp/call_pack/stop",
                 "/interp/call_pack/status", "/interp/subtitle_overlay"):
        if path not in hub:
            ng(f"avatar_hub 缺少 Phase B {path}")
        else:
            ok(f"avatar_hub {path} 存在")


def test_interp_phase_b():
    li = (ROOT / "live_interpreter.py").read_text(encoding="utf-8")
    checks = [
        ("def _translate_status", "_translate_status 翻译状态"),
        ('"translate": _translate_status()', "/metrics 含 translate"),
        ("bargein_count", "bargein_count 观测"),
        ("def _trigger_bargein", "barge-in 触发器"),
        ('@app.get("/subtitle_overlay"', "/subtitle_overlay 路由"),
        ('@app.get("/overlay"', "/overlay 字幕页"),
    ]
    for needle, label in checks:
        if needle not in li:
            ng(f"live_interpreter 缺少 {label}")
        else:
            ok(f"live_interpreter {label} 存在")


def test_phase_c_bg_replace():
    """C-1: 虚拟背景模块 + realtime_stream 接入 + Hub 代理。"""
    if not (ROOT / "bg_replace.py").exists():
        ng("缺少 bg_replace.py")
        return
    ok("bg_replace.py 存在")
    bg = (ROOT / "bg_replace.py").read_text(encoding="utf-8")
    for needle, label in [("class BackgroundReplacer", "BackgroundReplacer 类"),
                          ('"green"', "绿幕模式"), ("SelfieSegmentation", "MediaPipe 引擎")]:
        if needle not in bg:
            ng(f"bg_replace 缺少 {label}")
        else:
            ok(f"bg_replace {label} 存在")
    rt = (ROOT / "realtime_stream.py").read_text(encoding="utf-8")
    for needle, label in [("from bg_replace import", "realtime 引入 bg_replace"),
                          ("/bg/status", "realtime /bg/status 端点"),
                          ("/bg/set", "realtime /bg/set 端点"),
                          ("_bg.process(out)", "vcam 前挂背景处理")]:
        if needle not in rt:
            ng(f"realtime_stream 缺少 {label}")
        else:
            ok(f"realtime_stream {label}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    if '"/realtime/bg"' not in hub:
        ng("avatar_hub 缺少 /realtime/bg 代理")
    else:
        ok("avatar_hub /realtime/bg 代理存在")


def test_phase_c_face_map():
    """C-2: 双人 face_map（引擎槽位映射 + Hub 配置端点 + 注入）。"""
    fs = (ROOT / "faceswap_api.py").read_text(encoding="utf-8")
    for needle, label in [("source_map", "SwapRequest.source_map"),
                          ("face_map_used", "响应 face_map_used"),
                          ("valid_tgt.sort", "目标脸按 x 排序")]:
        if needle not in fs:
            ng(f"faceswap_api 缺少 {label}")
        else:
            ok(f"faceswap_api {label}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [('"/api/face_map"', "Hub /api/face_map 端点"),
                          ("_face_map_sources", "Hub source_map 注入"),
                          ("face_map.json", "face_map 持久化")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 {label}")
        else:
            ok(f"avatar_hub {label}")


def test_phase_c_hair_tryon():
    """C-3 发型定妆写入角色 + C-4 tryon 专家入口。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("hair_preset", "Hub hair_preset 端点"),
                          ("_profile_swap_face", "定妆脸优先取值"),
                          ("face_styled_b64", "定妆脸字段")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 {label}")
        else:
            ok(f"avatar_hub {label}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("runHairPreset", "hub.js 定妆动作"),
                          ("applyBg", "hub.js 背景热切"),
                          ("saveFaceMap", "hub.js face_map 保存"),
                          ("loadLabServices", "hub.js 实验室就绪态")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 {label}")
        else:
            ok(f"hub.js {label}")
    for needle, label in [("虚拟背景", "ui 虚拟背景控件"),
                          ("双人换脸", "ui 双人换脸控件"),
                          ("生成定妆脸", "ui 定妆按钮"),
                          ("labSvc.tryon", "ui tryon 就绪显隐")]:
        if needle not in ui:
            ng(f"ui.html 缺少 {label}")
        else:
            ok(f"ui.html {label}")


def test_lab_ui_markers():
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    faceswap = (ROOT / "faceswap_api.py").read_text(encoding="utf-8")
    if "🧪" not in ui and "实验室" not in ui:
        ng("ui.html 缺少实验室标记(发型)")
    else:
        ok("ui.html 发型入口带实验室标记")
    if "clone_engine_recommend" not in hub_js:
        ng("hub.js 未接入 clone_engine_recommend")
    else:
        ok("hub.js 克隆引擎推荐已接入")
    # 2026-07-16 去重复改版：Hub 侧双 CTA(直播同传/通话同传)退役，开始入口单一真相在
    # live_interpreter 面板「开始 ▾」菜单(通话向导/直播同传/演示)。断言改盯新架构：
    # Hub 只留跳转 startInterp + 面板内 livemode/callmode 编排项。
    li = (ROOT / "live_interpreter.py").read_text(encoding="utf-8")
    if "startInterpCallPack" in hub_js or "startInterpLive" in hub_js:
        ng("hub.js 残留已退役的同传双 CTA(startInterpLive/CallPack)")
    else:
        ok("hub.js 同传双 CTA 已退役(开始入口归一 iframe 面板)")
    if "startInterp()" not in hub_js or "goTab('interp')" not in hub_js:
        ng("hub.js 缺少 startInterp 跳转入口(命令面板/动线引用)")
    else:
        ok("hub.js 保留 startInterp 跳转入口")
    if "id=livemode" not in li or "id=callmode" not in li:
        ng("live_interpreter 开始▾菜单缺少 直播同传/通话向导 编排项")
    else:
        ok("live_interpreter 开始▾菜单含 直播同传/通话向导")
    if "interpOverlayUrl" not in hub_js:
        ng("hub.js 缺少 interpOverlayUrl(OBS 字幕)")
    else:
        ok("hub.js OBS 字幕 URL 已接入")
    if "实验室" not in faceswap and "离线" not in faceswap:
        ng("faceswap_api 控制页未标注实验室/离线")
    else:
        ok("faceswap_api 控制页已标注实验室/离线")


def test_no_bare_tryon_in_quick_ops():
    """快捷操作区不应有未标注的「虚拟试衣」直链（ui 快捷区已移除 tryon）。"""
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    block = re.search(r"快捷操作[\s\S]{0,800}", ui)
    if block and "虚拟试衣" in block.group(0):
        ng("ui 快捷操作仍含「虚拟试衣」未治理")
    else:
        ok("ui 快捷操作无裸 tryon 链接")


def test_phase_d_checkup():
    """D-1: 设备体检分端点 + 画质探针帧高 + UI 体检卡。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [('"/api/device/checkup"', "Hub /api/device/checkup 端点"),
                          ("_checkup_mic_sample", "麦克风采样(噪声底/SNR)"),
                          ("_CHECKUP_MIC_LOCK", "录音防并发锁")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 D-1 {label}")
        else:
            ok(f"avatar_hub D-1 {label}")
    rt = (ROOT / "realtime_stream.py").read_text(encoding="utf-8")
    if '"frame_h": h' not in rt:
        ng("realtime_stream /swap/quality 缺少 frame_h(脸占比口径)")
    else:
        ok("realtime_stream /swap/quality 含 frame_h")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "deviceCheckup" not in hub_js:
        ng("hub.js 缺少 deviceCheckup()")
    else:
        ok("hub.js deviceCheckup() 存在")
    if "设备体检" not in ui:
        ng("ui.html 缺少 设备体检 按钮")
    else:
        ok("ui.html 设备体检卡存在")


def test_phase_d_webrtc_fallback():
    """D-2: vcam MJPEG 预览 + Hub 同源代理 + 手机页自动兜底 + TTFV 回传。"""
    vc = (ROOT / "vcam_server.py").read_text(encoding="utf-8")
    for needle, label in [('"/preview.mjpeg"', "vcam /preview.mjpeg 端点"),
                          ("multipart/x-mixed-replace", "MJPEG 分帧响应")]:
        if needle not in vc:
            ng(f"vcam_server 缺少 D-2 {label}")
        else:
            ok(f"vcam_server D-2 {label}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [('"/api/vcam/preview.mjpeg"', "Hub MJPEG 同源代理"),
                          ('"/api/client_metric"', "Hub 客户端指标回传")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 D-2 {label}")
        else:
            ok(f"avatar_hub D-2 {label}")
    ph = (ROOT / "static" / "phone.html").read_text(encoding="utf-8")
    for needle, label in [("startMjpegFallback", "WebRTC 失败自动兜底"),
                          ("webrtc_ttfv_ms", "WebRTC TTFV 上报"),
                          ("mjpeg_ttfv_ms", "MJPEG TTFV 上报"),
                          ("liveFallback", "兜底态与对话管线联动")]:
        if needle not in ph:
            ng(f"phone.html 缺少 D-2 {label}")
        else:
            ok(f"phone.html D-2 {label}")


def test_phase_d_cluster_launcher():
    """D-4 分机部署向导 + D-5 launcher 设备灯。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    if '"/api/cluster/wizard"' not in hub:
        ng("avatar_hub 缺少 /api/cluster/wizard")
    else:
        ok("avatar_hub /api/cluster/wizard 存在")
    if "_load_topology_lint" not in hub:
        ng("向导未复用 topology_lint(单一 lint 口径)")
    else:
        ok("向导复用 tools/topology_lint.check")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("clusterWizard", "ops 向导卡 JS"),
                          ("cwCopyEnv", "env 片段一键复制")]:
        if needle not in ops:
            ng(f"ops.html 缺少 D-4 {label}")
        else:
            ok(f"ops.html D-4 {label}")
    lq = (ROOT / "launcher_qt.py").read_text(encoding="utf-8")
    for needle, label in [("device_ready", "设备状态信号"),
                          ("_apply_device", "设备灯渲染"),
                          ("checkup?quick=1", "quick 轮询(免录音)")]:
        if needle not in lq:
            ng(f"launcher_qt 缺少 D-5 {label}")
        else:
            ok(f"launcher_qt D-5 {label}")


def test_phase_e_docs():
    """E-1/E-2/E-3: 官网对照表刷新 + 硬件分档速查 + 销售一页纸(md+可打印 html)。"""
    bd = (ROOT / "官网对齐方案_BOUNDLESS.md").read_text(encoding="utf-8")
    for needle, label in [("虚拟背景 / 场景更换", "对照表·虚拟背景行"),
                          ("双人同框换脸", "对照表·双人换脸行"),
                          ("OBS 直播双语字幕", "对照表·OBS 字幕行"),
                          ("实测口径速查", "硬件节·实测口径速查")]:
        if needle not in bd:
            ng(f"官网对齐方案 缺少 E-1 {label}")
        else:
            ok(f"官网对齐方案 E-1 {label}")
    manual = (ROOT / "使用说明.md").read_text(encoding="utf-8")
    if "硬件分档速查表" not in manual:
        ng("使用说明 缺少 E-2 硬件分档速查表")
    else:
        ok("使用说明 E-2 硬件分档速查表存在")
    if "192.168.1.43" in manual or "192.168.1.51" in manual:
        ng("使用说明 仍引用已退役旧机 192.168.1.x(拓扑口径过期)")
    else:
        ok("使用说明 拓扑口径已更新(无退役机残留)")
    op = ROOT / "销售一页纸_BOUNDLESS.md"
    if not op.exists():
        ng("缺少 销售一页纸_BOUNDLESS.md")
    else:
        src = op.read_text(encoding="utf-8")
        for needle in ("四产品线", "定价锚点", "不承诺的事"):
            if needle not in src:
                ng(f"销售一页纸 缺少「{needle}」")
            else:
                ok(f"销售一页纸 含「{needle}」")
    pg = ROOT / "static" / "sales_onepager.html"
    if not pg.exists():
        ng("缺少 static/sales_onepager.html(可打印版)")
    else:
        h = pg.read_text(encoding="utf-8")
        for needle, label in [("brand.css", "复用品牌令牌"), ("@media print", "打印样式"),
                              ("无审查", None)]:
            if needle == "无审查":
                if needle in h:
                    ng("sales_onepager 混入「无审查」叙事(违反物理分页红线)")
                else:
                    ok("sales_onepager 无「无审查」叙事(分页红线守住)")
            elif needle not in h:
                ng(f"sales_onepager 缺少 {label}")
            else:
                ok(f"sales_onepager {label}")


def test_phase_e_hw_guide():
    """E-2: 硬件档位引导端点 + 设置页卡片(与销售一页纸同口径)。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [('"/api/hardware/guide"', "Hub /api/hardware/guide 端点"),
                          ("_HW_TIERS", "档位表"), ("_HW_FEATURES", "功能预期表")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 E-2 {label}")
        else:
            ok(f"avatar_hub E-2 {label}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    if "loadHwGuide" not in hub_js:
        ng("hub.js 缺少 loadHwGuide()")
    else:
        ok("hub.js loadHwGuide() 存在")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "硬件档位与能力预期" not in ui:
        ng("ui.html 缺少 硬件档位卡")
    else:
        ok("ui.html 硬件档位卡存在")


def test_phase_e_claims():
    """E-1 机器化: capability_matrix(能力↔证据契约) + claims_lint(证据核验/措辞禁区/lab 审计)。"""
    if not (ROOT / "capability_matrix.json").exists():
        ng("缺少 capability_matrix.json（能力对照单一数据源）")
        return
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("claims_lint", ROOT / "tools" / "claims_lint.py")
    mod = _ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        violations, oks = mod.check()
    except Exception as e:
        ng(f"claims_lint 运行失败: {e}")
        return
    if violations:
        for v in violations:
            ng(f"claims_lint: {v}")
    else:
        ok(f"claims_lint 全对齐（{len(oks)} 项：宣称能力证据在位 + 营销面无过度承诺）")


def test_phase_e_checkup_gate():
    """E 附加: 一键开播叠加设备体检 quick 卡点(红灯先修再播,探测失败放行)。"""
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    m = re.search(r"async guardedStart\(\)\{[\s\S]{0,1200}?oneClickStart\(\)", hub_js)
    if not m:
        ng("hub.js guardedStart 未升级为 async(体检卡点缺失)")
        return
    blk = m.group(0)
    for needle, label in [("checkup?quick=1", "quick 体检探测"),
                          ("grade==='red'", "仅红灯拦截"),
                          ("AbortController", "1.5s 超时放行"),
                          ("catch", "探测失败放行")]:
        if needle not in blk:
            ng(f"guardedStart 体检卡点缺少 {label}")
        else:
            ok(f"guardedStart 体检卡点 {label}")


def test_b5_asr_route():
    """B-5 收尾: ASR 路由真相常驻可见(引擎+原因入 /metrics) + 运行中切语向的路由提醒。"""
    li = (ROOT / "live_interpreter.py").read_text(encoding="utf-8")
    for needle, label in [("self.asr_route", "State.asr_route 路由真相"),
                          ('"asr_route": (ST.asr_route or None)', "/metrics 透出 asr_route"),
                          ("Whisper·分段", "分段引擎标签"),
                          ("asr_advice", "切语向路由提醒(set_langs)"),
                          ("新语向含流式弱语种", "弱语种切入警告文案")]:
        if needle not in li:
            ng(f"live_interpreter 缺少 B-5 {label}")
        else:
            ok(f"live_interpreter B-5 {label}")
    # 2026-07-16 去重复改版：Hub 外层「直播观测」条退役，ASR 徽标唯一呈现面=面板底部 mbar
    if "'ASR·'" not in li and "ASR·" not in li:
        ng("live_interpreter mbar 缺少 ASR 引擎徽标")
    else:
        ok("live_interpreter mbar ASR 引擎徽标存在(Hub 外层观测条已退役)")


def test_voice_guard():
    """P0g(2026-07-10 无声事故根治): 角色无音色样本→克隆配音必败(cosyvoice/fish 400/500)，
    曾整场静默零提示,用户误以为"切语种没生效"。守四层:
    ①源头跳过+30s 节流告警(不再每句撞 2~3 个引擎刷 traceback)
    ②开播/切角色事件 + /start 回传 voice_ok(未录音色 vs Hub拉取失败分话术)
    ③选择时下拉 🔇 标记 + 观测条常驻徽章 + 通话向导"配音音色"红灯
    ④参考音预热按引擎分流(cosyvoice→register_spk,根治常年 404 且预热真正命中)。"""
    li = (ROOT / "live_interpreter.py").read_text(encoding="utf-8")
    for needle, label in [
            ("def _voice_ready", "守卫谓词 _voice_ready"),
            ("def _note_novoice_skip", "节流跳过告警"),
            ("def _novoice_reset", "新语境计数清零"),
            ("def _profile_voice_optional", "SBV2 白名单免参考音判定"),
            ("def _voice_probe_hint", "未录音色/拉取失败分话术"),
            ("def _fallback_voice", "env 兜底音色(默认关)"),
            # 2026-07-15 起 /start 的 voice_ok 升级为 _dub_ready() 联合判定(音色+引擎在线)，
            # 向导红灯同步改名「配音就绪(音色+引擎)」——断言跟实现走,守的仍是同一条防线。
            ('"voice_ok": _dub["voice_ok"]', "/start 回传 voice_ok(音色+引擎联合判定)"),
            ('"voice_ok": (_voice_ready() if ST.running else None)', "status/metrics 暴露 voice_ok"),
            ("参考音预热完成@", "预热按引擎分流"),
            ("/v1/tts/register_spk", "cosyvoice spk 预注册"),
            ("无音色·仅字幕", "观测条常驻徽章"),
            ("data-novoice", "角色下拉无音色标记"),
            ('_step("配音就绪(音色+引擎)"', "通话向导配音红灯"),
            ('"session_running": ST.running', "向导红灯与会话真相同步")]:
        if needle not in li:
            ng(f"live_interpreter 缺少 无音色守卫 {label}")
        else:
            ok(f"live_interpreter 无音色守卫 {label}")
    if li.count("if not _voice_ready():") < 3:
        ng("无音色守卫前置点不足(应≥3: 配音入队/直播输出/开播)")
    else:
        ok("无音色守卫前置点 ≥3(配音入队/直播输出/开播)")
    if "has_voice" not in li.split("def hub_profiles", 1)[1][:700]:
        ng("hub_profiles 代理未透传 has_voice(下拉无从标记)")
    else:
        ok("hub_profiles 代理透传 has_voice")
    env = (ROOT / "env_config.bat").read_text(encoding="utf-8", errors="ignore")
    if "INTERP_FALLBACK_VOICE" not in env:
        ng("env_config 缺少 INTERP_FALLBACK_VOICE 说明(兜底音色开关)")
    else:
        ok("env_config INTERP_FALLBACK_VOICE 已登记")
    # ── P0g-2 角色库治理(2026-07-10 二期): 无音色沉底分组/直达修复链接/全端可见/夜检巡检 ──
    for needle, label in [("无音色（仅字幕，先去配音色）", "下拉无音色沉底分组"),
                          ("去角色库配声音", "选择提示直达修复链接")]:
        if needle not in li:
            ng(f"live_interpreter 缺少 治理 {label}")
        else:
            ok(f"live_interpreter 治理 {label}")
    # 2026-07-16 去重复改版：Hub 外层观测条退役,无音色徽章唯一呈现面=面板 mbar(上方 li 断言已覆盖)
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("没有音色样本：对话/配音不可用", "角色卡待配音色后果说明")]:
        if needle not in ui:
            ng(f"ui.html 缺少 治理 {label}")
        else:
            ok(f"ui.html 治理 {label}")
    if "voice_ok===false" in li and "无音色·仅字幕" in li:
        ok("live_interpreter mbar 无音色徽章在位(Hub 外层已退役)")
    else:
        ng("live_interpreter mbar 缺少无音色徽章(voice_ok===false)")
    relay = (ROOT / "monitor_relay.py").read_text(encoding="utf-8")
    for needle, label in [('"voice_ok": s.get("voice_ok")', "手机代理透传 voice_ok"),
                          ("同传:无音色·仅字幕", "手机状态灯无音色警示")]:
        if needle not in relay:
            ng(f"monitor_relay 缺少 治理 {label}")
        else:
            ok(f"monitor_relay 治理 {label}")
    acc = (ROOT / "acceptance.py").read_text(encoding="utf-8")
    for needle, label in [("def test_voice_assets", "夜检音色完整性函数"),
                          ('("voiceassets"', "夜检 SUITE 登记")]:
        if needle not in acc:
            ng(f"acceptance 缺少 治理 {label}")
        else:
            ok(f"acceptance 治理 {label}")


def test_p0h_subtitle_sanity():
    """P0h(2026-07-10 中→日实测实录): ①GER 仲裁模型把句尾标点幻改成全角字母/数字/替换符
    (『…呢?』→『…呢Ｂ』『…声音。』→『…声音７』『，』→『�』)——拼音闸对非发音字符无感,垃圾直上字幕；
    ②LLM 译文冒出源文没有的 ASCII 词('あ、これ何 helfulnoの！？')——克隆音会把乱码念出来。
    守三层: 垃圾字符闸(AST 抽函数真跑) / 标点-only 纠错按无修正处理 / 译文健全闸带源文对照。
    附: acceptance --only 部分跑不覆盖主报告/不入历史/不碰告警(2026-07-10 实撞假绿+假恢复)。"""
    li_p = ROOT / "live_interpreter.py"
    li = li_p.read_text(encoding="utf-8")
    for needle, label in [("def _ger_garbage_introduced", "垃圾字符闸函数"),
                          ("垃圾字符闸拒绝", "gate 接线(拒绝留痕)"),
                          ("_flat_text(cand) == _flat_text(text)", "标点-only 纠错拦截"),
                          ("_llm_out_sane(out, dest, src_text=text)", "译文健全闸带源文"),
                          ("_MT_CJK_DESTS", "CJK 目标语集合")]:
        if needle not in li:
            ng(f"live_interpreter 缺少 P0h {label}")
        else:
            ok(f"live_interpreter P0h {label}")
    # 行为级：AST 抽 _ger_garbage_introduced/_llm_out_sane 真跑(实录样本矩阵,不起服务)
    try:
        import ast as _ast
        import re as _re_mod
        tree = _ast.parse(li)
        wanted = {"_ger_garbage_introduced", "_llm_out_sane"}
        assigns = {"_GER_GARBAGE_RE", "_ASCII_WORD_RE", "_MT_CJK_DESTS"}
        parts = []
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef) and node.name in wanted:
                parts.append(_ast.get_source_segment(li, node))
            elif isinstance(node, _ast.Assign) and any(
                    isinstance(t, _ast.Name) and t.id in assigns for t in node.targets):
                parts.append(_ast.get_source_segment(li, node))
        ns = {"_re": _re_mod,
              "_asr_hotwords": lambda lang: "LingoX 凌克斯",
              "_SCRIPT_CHECKS": {}}
        exec("\n\n".join(parts), ns)
        g = ns["_ger_garbage_introduced"]; sane = ns["_llm_out_sane"]
        cases = [
            (g("哪一个速度最快呢?", "哪一个速度最快呢Ｂ"), True, "全角字母Ｂ拒"),
            (g("现在你有几种声音。", "现在你有几种声音７"), True, "全角数字７拒"),
            (g("更新声音, 我的声音还不够标准吗?", "更新声音�我的声音还不够标准吗?"), True, "替换符�拒"),
            (g("凌克斯真好用", "LingoX真好用", "zh"), False, "术语热词 ASCII 放行"),
            (g("前面说 STORYTELLING 是啥", "前面说 STORYTELLING 到底是啥"), False, "源文已有 ASCII 放行"),
            (g("我好想听个笑话呀", "我好想唱个笑话呀"), False, "普通同音字修正放行"),
            (not sane("あ、これ何 helfulnoの！？", "ja", "哦,你这个是什么声音?"), True, "ja 译文凭空 ASCII 词拒"),
            (sane("あ、これ何の声？", "ja", "哦,你这个是什么声音?"), True, "ja 干净译文放行"),
            (sane("OKです、わかりました", "ja", "好的知道了"), True, "常用缩写 OK 放行"),
            (sane("LingoXを使ってみて", "ja", "试试凌克斯"), True, "术语热词译文放行"),
            (not sane("Great! 我们成功了呢！", "zh", "我们成功了"), True, "zh 混英开头拒"),
        ]
        bad = [label for got, want, label in cases if bool(got) != bool(want)]
        if bad:
            ng("P0h 行为矩阵失败: " + "、".join(bad))
        else:
            ok(f"P0h 垃圾闸/健全闸行为矩阵 {len(cases)}/{len(cases)} 真跑通过")
    except Exception as e:
        ng(f"P0h 行为矩阵异常: {e}")
    acc = (ROOT / "acceptance.py").read_text(encoding="utf-8")
    for needle, label in [('_partial.json', "--only 报告分流(不盖主报告)"),
                          ("if not only:", "部分跑不入历史/不碰告警")]:
        if needle not in acc:
            ng(f"acceptance 缺少 P0h {label}")
        else:
            ok(f"acceptance P0h {label}")


def test_p0i_quality_observability():
    """P0i(2026-07-10): ①GER 拒绝分桶精细化(garbage/overfix 与拼音拒去重)；②过度纠错闸真跑；
    ②LLM 译文健全闸拒绝计数；③deliver_check --only 不盖主报告(与 acceptance 同构隔离)。"""
    li_p = ROOT / "live_interpreter.py"
    li = li_p.read_text(encoding="utf-8")
    for needle, label in [("def _ger_gate_check", "GER 分桶闸函数"),
                          ('"overfix": 0', "过度纠错分桶初始化"),
                          ("self.mt_stats", "译文健全闸计数器"),
                          ('fail == "pinyin"', "拼音拒才计 rejected(去重)"),
                          ("过度纠错闸拒绝", "过度纠错留痕"),
                          ('"mt": dict(ST.mt_stats)', "音频健康暴露 mt 块"),
                          ("过度纠错拦", "观测条 overfix 提示")]:
        if needle not in li:
            ng(f"live_interpreter 缺少 P0i {label}")
        else:
            ok(f"live_interpreter P0i {label}")
    try:
        import ast as _ast
        import difflib as _difflib
        tree = _ast.parse(li)
        wanted = {"_ger_overcorrected", "_flat_text"}
        parts = []
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef) and node.name in wanted:
                parts.append(_ast.get_source_segment(li, node))
            elif isinstance(node, _ast.Assign) and any(
                    isinstance(t, _ast.Name) and t.id == "_re_flat_all" for t in node.targets):
                parts.append(_ast.get_source_segment(li, node))
        ns = {"difflib": _difflib, "_re": __import__("re")}
        exec("\n\n".join(parts), ns)
        over = ns["_ger_overcorrected"]
        cases = [
            (over("嗯对他", "横距它"), True, "整句语义漂移拒"),
            (over("我好想听个笑话呀", "我好想唱个笑话呀"), False, "单字同音修正放行"),
            (over("哪一个速度最快呢?", "哪一个速度最快呢"), False, "无改动放行"),
        ]
        bad = [label for got, want, label in cases if bool(got) != bool(want)]
        if bad:
            ng("P0i 过度纠错行为矩阵失败: " + "、".join(bad))
        else:
            ok(f"P0i 过度纠错闸行为矩阵 {len(cases)}/{len(cases)} 真跑通过")
    except Exception as e:
        ng(f"P0i 过度纠错行为矩阵异常: {e}")
    dc = (ROOT / "deliver_check.py").read_text(encoding="utf-8")
    for needle, label in [("deliver_report_partial.json", "--only 报告分流"),
                          ('"partial": only', "部分跑标记 partial")]:
        if needle not in dc:
            ng(f"deliver_check 缺少 P0i {label}")
        else:
            ok(f"deliver_check P0i {label}")


def test_swap_session_report():
    """观察自动化: 停播自动聚合场次换脸质量报告(裁剪命中/降档/时延/增强)——真人实测零人工记数。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("_swap_sess_tick", "健康守护采样钩子"),
                          ('"/realtime/swap/sessions"', "场次账本端点"),
                          ("swap_sessions.jsonl", "报告落盘"),
                          ('"/api/swap_sess/selftest"', "聚合自检端点"),
                          ("carry", "计数器重置进位"),
                          # P7 三期(2026-07-06 真人直播实测暴露): 停播=整树关窗→hub 猝死,内存半场蒸发。
                          # 在播断点落盘 + 启动对账转正(recovered 标记),宿主怎么死都不丢场。
                          ("_swap_sess_ckpt", "在播断点落盘"),
                          ("_swap_sess_reconcile", "启动对账转正"),
                          ("swap_sess_active.json", "断点文件"),
                          ('rep["recovered"] = True', "断点恢复标记")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 观察自动化 {label}")
        else:
            ok(f"avatar_hub 观察自动化 {label}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("fetchSwapRecap", "停播拉取报告"),
                          ("swapRecapEnh", "主用精修引擎")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 {label}")
        else:
            ok(f"hub.js {label} 存在")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle in ("裁剪命中", "自动降档"):
        if needle not in ui:
            ng(f"ui.html 成绩单缺少「{needle}」格")
        else:
            ok(f"ui.html 成绩单「{needle}」格存在")


def test_obs_ops_view():
    """观察自动化二期: /ops 甘特复盘并入画质报告(同一场次视图,不另立卡) + 成绩单对比上场趋势。"""
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("hgQualityFor", "甘特↔画质报告按开播时刻配对"),
                          ("画质报告（自动采样）", "下钻画质报告栏"),
                          ("裁剪命中%", "CSV 画质列")]:
        if needle not in ops:
            ng(f"ops.html 缺少 {label}")
        else:
            ok(f"ops.html {label}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("swapPrev", "上场报告配对"),
                          ("swapDeltaTxt", "对比上场文案"),
                          ("swapDeltaTone", "趋势好坏着色")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 {label}")
        else:
            ok(f"hub.js {label} 存在")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "对比上场" not in ui:
        ng("ui.html 成绩单缺少「对比上场」趋势行")
    else:
        ok("ui.html 成绩单「对比上场」趋势行存在")


def test_p8_main_face_away():
    """P8(首场直播复盘落地): 仅换主脸 + 离席兜底画面 + 离席拆账 + 孤儿流探测 + 启动溯源。"""
    eng = (ROOT / "faceswap_api.py").read_text(encoding="utf-8")
    for needle, label in [("main_face_only", "引擎仅换主脸参数"),
                          ("req.main_face_only and map_faces is None", "face_map 优先于锁主脸")]:
        if needle not in eng:
            ng(f"faceswap_api.py 缺少 {label}")
        else:
            ok(f"faceswap_api.py {label}")
    rt = (ROOT / "realtime_stream.py").read_text(encoding="utf-8")
    for needle, label in [("SWAP_MAIN_FACE", "锁主脸开关(默认开)"),
                          ("/swap/main_face", "锁主脸热切口"),
                          ("SWAP_AWAY_AFTER", "离席判定秒数"),
                          ("_away_frame", "离席渐进模糊兜底"),
                          ("_away_badge_img", "离席角标预渲染"),
                          ("probe and SWAP_MAIN_FACE and _crop_active", "探测帧不让窗给窗外大脸")]:
        if needle not in rt:
            ng(f"realtime_stream.py 缺少 {label}")
        else:
            ok(f"realtime_stream.py {label}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("by_presence", "在席/离席成败归桶"),
                          ("fail_pct_active", "有效失败率(剔除离席)"),
                          ('"away_s"', "离席时长入报告"),
                          ("_orphan_probe", "孤儿画面进程探测"),
                          ("_ORPHAN_STREAM", "孤儿旗标"),
                          ('"/realtime/swap/main_face"', "锁主脸代理端点"),
                          ("_alert_beep", "坏态本机蜂鸣"),
                          ("_log_boot_provenance", "启动溯源breadcrumb")]:
        if needle not in hub:
            ng(f"avatar_hub.py 缺少 {label}")
        else:
            ok(f"avatar_hub.py {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for cond, label in [("facesChip" in js and "facesChip()" in ui, "多脸在镜chip"),
                        ("swapAwayTxt" in js and "离席" in ui, "成绩单离席行"),
                        ("orphanStream" in js and "orphanStream" in ui, "孤儿流横幅")]:
        if not cond:
            ng(f"前端缺少 {label}")
        else:
            ok(f"前端 {label}")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("有效失败率", "ops下钻离席拆账"), ("离席s", "CSV离席列")]:
        if needle not in ops:
            ng(f"ops.html 缺少 {label}")
        else:
            ok(f"ops.html {label}")


def test_checkup_ledger():
    """观察自动化 IV: 体检留痕 + 误拦联查(红灯后健康场=疑似误拦,机器可数) + 自检端点。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("_checkup_log_append", "体检留痕落盘"),
                          ("device_checkups.jsonl", "留痕文件"),
                          ('"/api/device/checkup/history"', "留痕/误拦联查端点"),
                          ("_checkup_history_core", "联查纯函数"),
                          ("false_block_rate", "误拦率口径"),
                          ('"/api/device/checkup/selftest"', "联查自检端点")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 体检留痕 {label}")
        else:
            ok(f"avatar_hub 体检留痕 {label}")
    # avatar_hub 导入副作用重(起线程/读配置),离线门禁做源码级约束验证;真跑联查靠 /selftest 端点
    st = hub.find("def _checkup_history_core")
    if st < 0 or "break   # 只配最近的一场" not in hub[st:st + 2600]:
        ng("_checkup_history_core 缺「只配最近一场」约束(误拦会被多场重复计数)")
    else:
        ok("_checkup_history_core 单场配对约束在位")
    if 'if src in ("gate", "manual")' not in hub:
        ng("体检留痕缺 src 过滤(launcher 30s 轮询会刷爆账本/污染误拦分母)")
    else:
        ok("体检留痕 src 过滤在位(轮询/soak 不落账)")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("quick=1&src=gate", "开播卡点体检带 src=gate"),
                          ("src=manual", "手动体检带 src=manual")]:
        if needle not in hub_js:
            ng(f"hub.js 缺 {label}")
        else:
            ok(f"hub.js {label}")


def test_device_humanize():
    """P0 设备人话层：device_enum 分类器纯函数真跑 + hub 结构化返回 + 前端分组/试音/危险确认接线。"""
    try:
        sys.path.insert(0, str(ROOT))
        import device_enum as de
        r = de.humanize_audio_devices([
            "麦克风 (DroidCam Virtual Audio) (MME)",
            "麦克风 (DroidCam Virtual Audio) (Windows WASAPI)",
            "CABLE Output (VB-Audio Virtual  (MME)",
            "立体声混音 (Realtek High Definiti (MME)",
        ], "in")
        devs = r["devices"]
        droid = [d for d in devs if d["group"] == "phone"]
        if len(droid) == 1 and len(droid[0]["variants"]) == 2 and droid[0]["value"].endswith("(MME)"):
            ok("device_enum 去重合并(hostapi 变体→1条,MME 为提交值)")
        else:
            ng("device_enum 去重合并失效")
        if all(d["danger"] and d["hidden"] for d in devs if d["group"] == "virtual"):
            ok("device_enum 危险项标记+默认折叠(CABLE回收口/立体声混音)")
        else:
            ng("device_enum 危险项标记/折叠失效")
        if de.pick_best(devs, "in").get("group") == "phone":
            ok("device_enum pick_best 推荐手机麦")
        else:
            ng("device_enum pick_best 推荐失效")
    except Exception as e:
        ng(f"device_enum 人话层异常: {e}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("humanize_audio_devices", "结构化人话层接入 /rvc/devices"),
                          ("_resolve_audio_device", "设备名→索引解析"),
                          ('"/api/audio/mic_test"', "试音端点"),
                          ('"/api/audio/output_test"', "试听端点(CABLE 回环自证)"),
                          ("_AUDIO_TEST_LOCK", "试音试听互斥锁"),
                          ("_ENV_CHECK_META", "前置检查人话元数据")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 设备人话 {label}")
        else:
            ok(f"avatar_hub 设备人话 {label}")
    if "async def api_device_checkup" in hub and 'device: str = ""' in hub:
        ok("avatar_hub 体检录音支持 device 参数(测选中麦)")
    else:
        ng("avatar_hub 体检录音缺 device 参数")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("audioGroups(", "下拉分组渲染"),
                          ("audioDevMeta(", "设备元数据查询"),
                          ("onPickAudioInput", "危险设备确认"),
                          ("audioOutWarn(", "非CABLE输出软警告"),
                          ("micTestRun(", "试音"),
                          ("outTestRun(", "试听"),
                          ("audioChain(", "用户模式声音线路"),
                          ("envFixAct(", "前置检查修复分发"),
                          ("devShowAll", "显示全部原始设备开关")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 {label}")
        else:
            ok(f"hub.js 设备人话 {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("<optgroup", "optgroup 分组下拉"),
                          ("显示全部原始设备", "原始列表开关"),
                          ("声音线路", "用户模式声音线路卡"),
                          ("跟随系统默认", "默认项人话"),
                          ("音色贴合度", "RVC 参数人话(index)"),
                          ("咬字保护", "RVC 参数人话(protect)")]:
        if needle not in ui:
            ng(f"ui.html 缺少 {label}")
        else:
            ok(f"ui.html 设备人话 {label}")


def test_device_prefs():
    """P2 设备偏好持久化+拔插自愈：词典外置纯函数真跑 + prefs 端点/粘性回退 + 前端恢复/警告/就绪度接线。"""
    try:
        sys.path.insert(0, str(ROOT))
        import importlib
        import device_enum as de
        importlib.reload(de)
        # P2-3 词典外置：audio_brands.json 增量词条参与分类（elgato wave 在扩展表里）
        r = de.humanize_audio_devices(["麦克风 (Elgato Wave:3) (MME)"], "in")
        if r["devices"] and r["devices"][0]["group"] == "usb":
            ok("audio_brands.json 扩展词条生效(Elgato Wave→独立麦)")
        else:
            ng(f"audio_brands.json 扩展词条未生效: {r['devices'] and r['devices'][0]['group']}")
        # 坏文件兜底：内置词典仍在
        if "droidcam" in de._KW_PHONE and de._PHONE_PICK_PRI and de._PHONE_PICK_PRI[0] == "droid":
            ok("词典内置兜底与手机麦优先序完整")
        else:
            ng("词典内置兜底缺失")
        if de.label_for("CABLE Input (VB-Audio Virtual Cable) (MME)", "out").startswith("直播声卡"):
            ok("label_for 单名翻译(离线设备也能人话)")
        else:
            ng("label_for 翻译失效")
    except Exception as e:
        ng(f"P2 词典/label_for 异常: {e}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [('_AUDIO_PREFS_FILE', "偏好落盘文件"),
                          ('"/api/audio/prefs"', "偏好读写端点"),
                          ("_pref_find_entry", "偏好→在线条目匹配"),
                          ("_pref_missing", "偏好缺席标记(粘性回退)"),
                          ("你在界面上选定的麦克风", "一键开播尊重界面选择"),
                          ("detail_raw", "开播步骤保留原始名"),
                          ('"cable" not in out.lower()', "馈线探针只认CABLE护栏")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P2 {label}")
        else:
            ok(f"avatar_hub P2 {label}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("applyAudioPrefs(", "偏好恢复/缺席回退/插回自愈"),
                          ("saveAudioPref(", "选择即存偏好"),
                          ("devLostLine(", "缺席驻留警告条"),
                          ("devicechange", "系统拔插事件监听"),
                          ("_userPickTs", "手选护窗(防在途刷新回滚)"),
                          ("micTestJudge(", "试音结论入就绪度"),
                          ("hub_mic_test", "试音摘要持久化")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 P2 {label}")
        else:
            ok(f"hub.js P2 {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("devLostLine('in')", "输入缺席警告条"),
                          ("devLostLine('out')", "输出缺席警告条"),
                          ("it.key==='mictest'", "就绪度试音黄灯项")]:
        if needle not in ui:
            ng(f"ui.html 缺少 P2 {label}")
        else:
            ok(f"ui.html P2 {label}")
    if (ROOT / "audio_brands.json").exists():
        ok("audio_brands.json 词典文件在位")
    else:
        ng("audio_brands.json 缺失")


def test_p3_hotswitch_prefs_audit():
    """P3 设备三件套：①拔插热切端点(顺序=config→stop→start,失败不伤在跑转换) ②偏好审计轨迹
    (真跑 AST 抽出的 _audio_prefs_save：变更才记账/同值不记/封顶10条/带来源) ③试听回环入就绪度。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    # ── ① 热切端点契约与顺序 ──
    try:
        tree = ast.parse(hub)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "rvc_hot_switch")
        body = ast.get_source_segment(hub, fn)
        # 热切主链可能被抽成 _rvc_hot_switch_inner(锁外壳+主链分离的重构)——顺序断言看主链所在体
        inner = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == "_rvc_hot_switch_inner"), None)
        if inner is not None:
            body = ast.get_source_segment(hub, inner) + "\n" + body
        i_cfg, i_stop, i_start = body.find("/config"), body.find("/stop"), body.find("/start")
        if 0 < i_cfg < i_stop < i_start:
            ok("hot_switch 顺序 config→stop→start(校验失败不伤在跑转换)")
        else:
            ng(f"hot_switch 顺序错乱: config@{i_cfg} stop@{i_stop} start@{i_start}")
        if "time.sleep" in body and "block_time" in body:
            ok("hot_switch stop→start 之间等旧拾音流退出(防双流叠音)")
        else:
            ng("hot_switch 缺 stop→start 间隔等待")
        for needle, label in [("_RVC_HOTSWITCH_LOCK", "并发去重锁"),
                              ("configs/config.json", "运行配置单一真相(RVC落盘)"),
                              ("_pick_audio_devices", "显式>偏好>推荐选择链"),
                              ("was_running", "未在跑时只换线路不强启")]:
            if needle not in body and needle not in hub:
                ng(f"hot_switch 缺 {label}")
            else:
                ok(f"hot_switch {label}")
    except StopIteration:
        ng("avatar_hub 缺 /rvc/hot_switch 端点")
    # ── ② 偏好审计轨迹：AST 抽 _audio_prefs_load/_audio_prefs_save 真跑（免起 FastAPI）──
    try:
        import json as _json
        import tempfile
        import threading as _th
        import time as _time
        tree = ast.parse(hub)
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name in ("_audio_prefs_load", "_audio_prefs_save")]
        assert len(fns) == 2, "找不到偏好读写函数"
        mod = ast.Module(body=fns, type_ignores=[])
        ast.fix_missing_locations(mod)
        with tempfile.TemporaryDirectory() as td:
            ns = {"_AUDIO_PREFS_LOCK": _th.Lock(), "_AUDIO_PREFS_FILE": Path(td) / "prefs.json",
                  "json": _json, "time": _time, "Path": Path}
            exec(compile(mod, "<hub-prefs>", "exec"), ns)
            save = ns["_audio_prefs_save"]
            d = save(inp="麦A (MME)", src="ui-pick")                    # 首次设置=1条
            assert len(d["history"]) == 1 and d["history"][0]["src"] == "ui-pick" \
                and d["history"][0]["from"] is None and d["history"][0]["to"] == "麦A (MME)"
            d = save(inp="麦A (MME)", src="ui-pick")                    # 同值不记账
            assert len(d["history"]) == 1, f"同值重存不该记账: {d['history']}"
            d = save(inp="麦B (MME)", out="CABLE Input (MME)", src="phone-setup")   # 双侧变更各一条
            assert len(d["history"]) == 3 and d["history"][1]["from"] == "麦A (MME)" \
                and d["history"][2]["side"] == "output"
            for i in range(12):                                          # 封顶10条
                save(inp=f"麦{i} (MME)", src="test")
            d = save(inp="麦Z (MME)", src="test")
            assert len(d["history"]) == 10 and d["history"][-1]["to"] == "麦Z (MME)"
            assert d["input"] == "麦Z (MME)" and d["output"] == "CABLE Input (MME)"
        ok("偏好审计轨迹真跑(变更才记/同值不记/双侧分记/封顶10/来源留痕)")
    except Exception as e:
        ng(f"偏好审计轨迹行为测试失败: {e}")
    if '"src"' in hub or "'src'" in hub or 'body or {}).get("src")' in hub:
        ok("偏好端点接收来源(src)")
    else:
        ng("偏好端点缺 src 来源字段")
    # ── ③ 前端接线 ──
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("devHotSwitch(", "拔插热切动作"),
                          ("devHotOffer(", "热切CTA判据(在跑的正是缺席那只)"),
                          ("_rvcRunDevs", "在跑设备快照(热切后CTA退场)"),
                          ("devBackLine(", "首选插回·切回CTA(不自动断声)"),
                          ("audioHotSwap", "无声诊断→热切CTA分发"),
                          ("hub_out_test", "试听回环摘要持久化"),
                          ("outTestJudge(", "试听结论入就绪度"),
                          ("_cableOutDev(", "就绪度试听直打直播声卡")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 P3 {label}")
        else:
            ok(f"hub.js P3 {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("devHotOffer('in')", "输入缺席条热切按钮"),
                          ("devHotOffer('out')", "输出缺席条热切按钮"),
                          ("devBackLine(", "首选插回·切回按钮"),
                          ("it.key==='outtest'", "就绪度试听黄灯项"),
                          ("outTestRun(_cableOutDev())", "试听CTA直打CABLE")]:
        if needle not in ui:
            ng(f"ui.html 缺少 P3 {label}")
        else:
            ok(f"ui.html P3 {label}")
    # P3-3 顺手修正的历史缺陷：就绪度内联下拉此前绕过偏好保存/危险确认
    if "@change=\"rvc.inputDevice=audioInput\"" in ui or "@change=\"rvc.outputDevice=audioOutput\"" in ui:
        ng("就绪度内联下拉仍在绕过 onPickAudioInput/Output(偏好不落盘)")
    else:
        ok("就绪度内联下拉已走 onPick(危险确认+偏好落盘)")


def test_p4_ledger_funnel_fresh():
    """P4 设备三件套：①热切入场次账本(timeline event=device→场次聚合/甘特标记，AST 抽真函数跑合成时间线)
    ②缺席→热切漏斗(纯函数 _devflow_bump_core/_devflow_funnel 真跑：分桶/来源/封顶/转化率)
    ③冻结表柔性补洞(_merge_fresh_names 真跑 + 转换旗标/前端 🆕 接线)。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    import json as _json
    import tempfile
    import time as _time

    def _pull(names):
        tree = ast.parse(hub)
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
        assert len(fns) == len(names), f"缺函数: {set(names) - {f.name for f in fns}}"
        mod = ast.Module(body=fns, type_ignores=[])
        ast.fix_missing_locations(mod)
        ns = {"json": _json, "time": _time, "Path": Path, "_SH_TIMELINE_PATH": Path("_nonexistent_")}
        exec(compile(mod, "<hub-p4>", "exec"), ns)
        return ns

    # ── ① 场次账本：合成时间线(开播→热切×2→停播)跑真聚合函数 ──
    try:
        ns = _pull(["_health_sessions", "_health_gantt"])
        base = int(_time.time()) - 300
        evs = [
            {"ts": base, "event": "transition", "from": "idle", "to": "warmup"},
            {"ts": base + 5, "event": "transition", "from": "warmup", "to": "ok"},
            {"ts": base + 60, "event": "device", "from": "device", "to": "device",
             "label": "热切成功 手机麦克风（DroidCam）→播客麦克风（PD100X） (1.4s) 来源=strip"},
            {"ts": base + 120, "event": "device", "from": "device", "to": "device",
             "label": "热切失败@config 设备不可用"},
            {"ts": base + 200, "event": "transition", "from": "ok", "to": "idle"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tl.jsonl"
            p.write_text("\n".join(_json.dumps(e, ensure_ascii=False) for e in evs) + "\n", encoding="utf-8")
            ses = ns["_health_sessions"](path=p)
            assert len(ses) == 1 and ses[0]["hot_switch"] == 2, f"场次热切计数错: {ses}"
            gt = ns["_health_gantt"](path=p)
            mks = [m for m in gt[0]["markers"] if m["kind"] == "device"]
            assert len(mks) == 2 and gt[0]["hot_switch"] == 2, f"甘特热切标记错: {gt[0]}"
        ok("热切入账本真跑(场次聚合 hot_switch=2 + 甘特 device 标记)")
    except Exception as e:
        ng(f"热切入账本聚合测试失败: {e}")
    for needle, label in [("_hot_switch_ledger", "热切记账函数"),
                          ('kind="device"', "timeline device 事件"),
                          ("dev_hot_switch", "热切累计计数(stats)")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P4 {label}")
        else:
            ok(f"avatar_hub P4 {label}")
    # ── ② 漏斗纯函数真跑 ──
    try:
        ns = _pull(["_devflow_bump_core", "_devflow_funnel"])
        ns["_DEVFLOW_EVS"] = ("expose", "click", "ok", "fail")
        ns["_DEVFLOW_ADVICE_EVS"] = ("advice_expose", "advice_enable", "advice_dismiss")  # P8-2 建议条事件桩
        bump, funnel = ns["_devflow_bump_core"], ns["_devflow_funnel"]
        doc = {}
        doc = bump(doc, "expose", "in", "strip", "2026-07-06")
        doc = bump(doc, "expose", "in", "strip", "2026-07-06")
        doc = bump(doc, "click", "in", "strip", "2026-07-06")
        doc = bump(doc, "ok", "in", "strip", "2026-07-06")
        doc = bump(doc, "click", "out", "diag", "2026-07-07")
        doc = bump(doc, "fail", "out", "diag", "2026-07-07")
        doc = bump(doc, "bogus", "in", "x", "2026-07-07")          # 非法事件不记
        assert doc["days"]["2026-07-06"]["expose_in"] == 2
        assert doc["days"]["2026-07-06"]["src"]["click:strip"] == 1
        assert doc["days"]["2026-07-07"]["src"]["fail:diag"] == 1
        f = funnel(doc)
        assert f["expose"] == 2 and f["click"] == 2 and f["ok"] == 1 and f["fail"] == 1
        assert f["click_rate"] == 1.0 and f["success_rate"] == 0.5
        for i in range(70):                                         # 天数封顶60
            doc = bump(doc, "click", "in", "", f"2026-08-{i:02d}")
        assert len(doc["days"]) <= 60
        ok("漏斗纯函数真跑(分桶/来源细分/非法拒记/天数封顶/转化率)")
    except Exception as e:
        ng(f"漏斗纯函数测试失败: {e}")
    for needle, label in [('"/api/metrics/devflow"', "漏斗端点"),
                          ("devflow_stats.json", "漏斗落盘文件")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P4 {label}")
        else:
            ok(f"avatar_hub P4 {label}")
    # ── ③ 冻结表柔性补洞 ──
    try:
        ns = _pull(["_merge_fresh_names"])
        merged, fresh = ns["_merge_fresh_names"](
            ["麦A (MME)", "麦B (WASAPI)"], ["麦a  (mme)", "新麦 (MME)", "", None])
        assert fresh == ["新麦 (MME)"] and merged[-1] == "新麦 (MME)" and len(merged) == 3, \
            f"合并错: {merged} / {fresh}"
        merged2, fresh2 = ns["_merge_fresh_names"](["A (MME)"], ["A (MME)"])
        assert fresh2 == [] and merged2 == ["A (MME)"]
        ok("_merge_fresh_names 真跑(规范化去重/只补新名/空值安全)")
    except Exception as e:
        ng(f"_merge_fresh_names 测试失败: {e}")
    for needle, label in [("_RVC_CONV", "转换在跑旗标"),
                          ("_rvc_conv_mark", "旗标更新入口"),
                          ("fresh_note", "新插设备提示"),
                          ('e["fresh"] = True', "结构化条目 fresh 标")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P4 {label}")
        else:
            ok(f"avatar_hub P4 {label}")
    # ── 前端接线 ──
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("_devFlow(", "漏斗埋点助手"),
                          ("/api/metrics/devflow", "埋点上报端点"),
                          ("_devFlowSeen", "曝光去重(每缺席episode一次)"),
                          ("devHotSwitch(src, kind, dev)", "热切带来源/侧别/显式设备"),
                          ("'fresh-pick'", "🆕设备选中一键热切"),
                          ("rvcFreshNote", "新插设备说明行状态")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 P4 {label}")
        else:
            ok(f"hub.js P4 {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("devHotSwitch('strip','in')", "缺席条热切带来源"),
                          ("devHotSwitch('back','out')", "切回按钮带来源"),
                          ("a.fresh?' 🆕'", "下拉 🆕 徽章"),
                          ("rvcFreshNote", "新插设备说明行")]:
        if needle not in ui:
            ng(f"ui.html 缺少 P4 {label}")
        else:
            ok(f"ui.html P4 {label}")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    if "device" in ops and "🎚" in ops:
        ok("ops.html P4 甘特热切标记(🎚)")
    else:
        ng("ops.html 缺少 P4 甘特热切标记")


def test_p5_devflow_ops_recap():
    """P5 呈现层双件套：①ops「设备自愈漏斗」卡（GET 端点 src_total 跨天聚合真跑 + 卡片/渲染接线）
    ②停播成绩单设备热切行（时间窗过滤账本 + 复盘时间线 🎚 + 场次摘要 + 复制带热切行）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    # ── ① GET 端点 src_total 聚合逻辑真跑（AST 抽端点函数，注掉装饰器依赖：直接以纯逻辑复算）──
    import json
    try:
        import ast as _ast
        tree = _ast.parse(hub)
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef) and n.name == "api_devflow_get")
        # 端点体内依赖 _DEVFLOW_PATH/_devflow_funnel/time——注入桩后真跑
        fn.decorator_list = []
        ns = {"json": json, "time": __import__("time")}

        class _FakePath:
            def __init__(self, text): self._t = text
            def read_text(self, encoding="utf-8"): return self._t
        doc = {"days": {"2026-07-05": {"src": {"click:strip": 2, "ok:strip": 1}},
                        "2026-07-06": {"src": {"click:strip": 1, "click:diag": 3}}},
               "total": {"expose_in": 4, "click_in": 6, "ok_in": 4, "fail_in": 0}}
        ns["_DEVFLOW_PATH"] = _FakePath(json.dumps(doc))
        ns["_devflow_funnel"] = lambda d: {"expose": 4, "click": 6, "ok": 4, "fail": 0}
        ns["_devflow_suggest_auto"] = lambda d, en: {"suggest": False}   # P7-2 字段桩（真跑见 test_p7）
        ns["_devflow_advice_total"] = lambda d: {"expose": 0, "enable": 0, "dismiss": 0, "enable_rate": None}  # P8-2 字段桩
        ns["_DEV_AUTOSW_ON"] = False
        exec(compile(_ast.Module(body=[fn], type_ignores=[]), "avatar_hub.api_devflow_get", "exec"), ns)
        r = ns["api_devflow_get"]()
        assert r["ok"] and r["src_total"] == {"click:strip": 3, "ok:strip": 1, "click:diag": 3}, \
            f"src_total 聚合错: {r.get('src_total')}"
        assert r["days_kept"] == 2
        ok("GET /api/metrics/devflow src_total 跨天聚合真跑")
    except Exception as e:
        ng(f"devflow GET src_total 聚合测试失败: {e}")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("devflowCard", "设备自愈漏斗卡"),
                          ("devflowTick", "漏斗渲染函数"),
                          ("setInterval(devflowTick", "漏斗定时刷新"),
                          ("src_total", "来源细分 chips"),
                          ("成功率", "转化率读数")]:
        if needle not in ops:
            ng(f"ops.html 缺少 P5-1 {label}")
        else:
            ok(f"ops.html P5-1 {label}")
    # ── ② 成绩单热切行 ──
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("fetchHotSwitchRecap", "停播拉热切账本"),
                          ("hotSwitch={ n:", "成绩单热切状态"),
                          ("e.event==='device'", "时间窗过滤 device 事件"),
                          ("if(x.hot_switch)p.push('设备热切'", "场次摘要带热切"),
                          ("lineHs", "复制成绩单带热切行")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 P5-2 {label}")
        else:
            ok(f"hub.js P5-2 {label}")
    if "if(e.event==='device') return '🎚'" in hub_js:
        ok("hub.js P5-2 复盘时间线 device 图标(🎚)")
    else:
        ng("hub.js 缺少 P5-2 复盘时间线 device 图标")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [("lastSession?.hotSwitch", "成绩单热切行显隐"),
                          ("设备热切", "热切行标题"),
                          ("hotSwitch?.items", "热切明细列表")]:
        if needle not in ui:
            ng(f"ui.html 缺少 P5-2 {label}")
        else:
            ok(f"ui.html P5-2 {label}")


def test_p6_autoswitch_weekly():
    """P6 双件套：①设备缺席自动热切（纯决策函数 _dev_autosw_decide 真跑全护栏：确认窗/冷却/
    每场上限/旗标空窗防误清/撤防；开关持久化+守护注册+同账本接线）
    ②漏斗周报（_devflow_week_report/_devflow_week_text 真跑：上一自然周窗口/聚合/转化率/零事件文案；
    自动外发去重标记+手动端点接线）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    import json

    def _pull(names):
        tree = ast.parse(hub)
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        assert len(fns) == len(names), f"缺函数: {set(names) - {f.name for f in fns}}"
        for f in fns:
            f.decorator_list = []
        ns = {"json": json, "time": __import__("time")}
        exec(compile(ast.Module(body=fns, type_ignores=[]), "avatar_hub._p6", "exec"), ns)
        return ns

    # ── ① 自动热切决策纯函数 ──
    try:
        dec = _pull(["_dev_autosw_decide"])["_dev_autosw_decide"]
        kw = dict(confirm_s=25.0, cooldown=90.0, max_n=3)
        st = {"miss_since": 0.0, "last_ts": 0.0, "count": 0, "idle_ticks": 0}
        cases = {}
        cases["转换没跑=idle"] = dec(st, enabled=True, conv_on=False, dev_missing=True, now=1000, **kw) == "idle"
        cases["开关关=idle"] = dec(st, enabled=False, conv_on=True, dev_missing=True, now=1010, **kw) == "idle"
        cases["缺席首见=armed(进确认窗)"] = dec(st, enabled=True, conv_on=True, dev_missing=True, now=1020, **kw) == "armed"
        cases["确认窗内=armed"] = dec(st, enabled=True, conv_on=True, dev_missing=True, now=1030, **kw) == "armed"
        cases["过确认窗=fire"] = dec(st, enabled=True, conv_on=True, dev_missing=True, now=1046, **kw) == "fire"
        st["count"] += 1; st["last_ts"] = 1046.0; st["miss_since"] = 0.0   # 守护开火后的状态推进
        cases["设备回来=撤防idle"] = dec(st, enabled=True, conv_on=True, dev_missing=False, now=1050, **kw) == "idle"
        dec(st, enabled=True, conv_on=True, dev_missing=True, now=1060, **kw)          # 再缺席重新进窗
        cases["冷却中=armed(过窗也不开火)"] = dec(st, enabled=True, conv_on=True, dev_missing=True, now=1090, **kw) == "armed"
        cases["冷却过=fire"] = dec(st, enabled=True, conv_on=True, dev_missing=True, now=1140, **kw) == "fire"
        st["count"] = 3; st["last_ts"] = 0.0                                            # 弹药耗尽
        cases["每场上限=armed"] = dec(st, enabled=True, conv_on=True, dev_missing=True, now=1200, **kw) == "armed"
        dec(st, enabled=True, conv_on=False, dev_missing=False, now=1210, **kw)         # 旗标空窗第1拍
        cases["旗标空窗1拍不清弹药"] = st["count"] == 3
        dec(st, enabled=True, conv_on=False, dev_missing=False, now=1220, **kw)         # 连续第2拍=真收场
        cases["连续2拍idle=弹药恢复"] = st["count"] == 0
        for label, passed in cases.items():
            if passed:
                ok(f"_dev_autosw_decide {label}")
            else:
                ng(f"_dev_autosw_decide {label}")
    except Exception as e:
        ng(f"自动热切决策纯函数测试失败: {e}")
    for needle, label in [("_bg_dev_autoswitch", "自动热切守护"),
                          ("create_task(_bg_dev_autoswitch", "守护注册进启动"),
                          ("_dev_autosw_missing", "运行设备缺席判定"),
                          ('"dev_autoswitch"', "运行时开关(heal config)"),
                          ("_devflow_bump_local", "服务端同账本记账"),
                          ('"expose", kind, "auto"', "auto 来源曝光入漏斗"),
                          ('rvc_hot_switch, {"src": "auto"}', "复用同一条热切链")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P6-1 {label}")
        else:
            ok(f"avatar_hub P6-1 {label}")
    # ── ② 周报纯函数 ──
    try:
        import datetime as _dt
        ns = _pull(["_devflow_week_report", "_devflow_week_text"])
        rep_f, txt_f = ns["_devflow_week_report"], ns["_devflow_week_text"]
        today = _dt.date(2026, 7, 15)                     # 周三 → 上一自然周=07-06(一)~07-12(日)
        doc = {"days": {
            "2026-07-06": {"expose_in": 3, "click_in": 2, "ok_in": 2, "src": {"click:strip": 2}},
            "2026-07-12": {"expose_in": 1, "click_in": 1, "fail_in": 1, "src": {"fail:auto": 1}},
            "2026-07-13": {"expose_in": 9, "click_in": 9, "ok_in": 9},   # 本周一：不该被算进上周
            "2026-06-30": {"expose_in": 7}}}              # 上上周：也不该算
        r = rep_f(doc, today=today)
        checks = {
            "统计窗=上一自然周": r["span"] == "2026-07-06~2026-07-12" and r["monday"] == "2026-07-06",
            "计数聚合(只算窗内)": r["expose"] == 4 and r["click"] == 3 and r["ok"] == 2 and r["fail"] == 1,
            "转化率": r["click_rate"] == 0.75 and abs(r["success_rate"] - 0.667) < 1e-9,
            "来源聚合": r["src"] == {"click:strip": 2, "fail:auto": 1},
            "覆盖天数": r["days_active"] == 2,
        }
        t = txt_f(r)
        checks["报文含达成率"] = ("成功率 67%" in t and "点击率 75%" in t)
        checks["报文含来源"] = "fail:auto×1" in t
        rz = rep_f({"days": {}}, today=today)
        checks["零事件周如实报健康"] = "设备链路健康" in txt_f(rz)
        for label, passed in checks.items():
            if passed:
                ok(f"周报纯函数 {label}")
            else:
                ng(f"周报纯函数 {label}")
    except Exception as e:
        ng(f"周报纯函数测试失败: {e}")
    for needle, label in [('"/api/metrics/devflow/weekly"', "周报端点"),
                          ("weekly_sent", "本周已发去重标记"),
                          ("_bg_devflow_weekly", "周报守护"),
                          ("create_task(_bg_devflow_weekly", "周报守护注册进启动"),
                          ("notify_event", "经 alerts 外发")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P6-2 {label}")
        else:
            ok(f"avatar_hub P6-2 {label}")
    # ── 前端接线 ──
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("devAutoSwOn", "自动热切开关状态"),
                          ("'dev_autoswitch' in patch", "开关切换提示(含断声后果)")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 P6 {label}")
        else:
            ok(f"hub.js P6 {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "设备缺席自动热切" in ui and "dev_autoswitch:devAutoSwOn" in ui:
        ok("ui.html P6 自动热切开关(带后果说明)")
    else:
        ng("ui.html 缺少 P6 自动热切开关")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("auto=服务端自动热切", "漏斗口径含 auto 来源"),
                          ("devflowWeeklyNote", "周报状态脚注")]:
        if needle not in ops:
            ng(f"ops.html 缺少 P6 {label}")
        else:
            ok(f"ops.html P6 {label}")


def test_p7_instant_notice_advice():
    """P7 双件套：①自动热切前端即时感知（_DEV_AUTOSW_LAST 顺风车挂 /realtime/status +
    前端基线防重放/toast/刷设备条接线）②数据驱动「建议开启」（_devflow_suggest_auto 纯函数真跑：
    剔除 auto 分量/样本门槛/成功率门槛/已开启不推销 + 前端一次性提示接线）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    import json

    # ── ① 顺风车接线（后端）──
    for needle, label in [("_DEV_AUTOSW_LAST", "最近自动热切结果暂存"),
                          ('"dev_autoswitch_last"', "status 顺风车字段"),
                          ("_DEV_AUTOSW_LAST.update(ts=time.time()", "守护写入结果")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 P7-1 {label}")
        else:
            ok(f"avatar_hub P7-1 {label}")
    # ── ② 建议裁决纯函数真跑 ──
    try:
        tree = ast.parse(hub)
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name in ("_devflow_suggest_auto",)]
        assert fns, "缺 _devflow_suggest_auto"
        for f in fns:
            f.decorator_list = []
        ns = {"json": json}
        exec(compile(ast.Module(body=fns, type_ignores=[]), "avatar_hub._p7", "exec"), ns)
        sug = ns["_devflow_suggest_auto"]
        # 人工战绩达标：曝光4/点击4/成功4（其中 auto 各1 应被剔除→人工 3/3/3 率100%）
        doc = {"days": {"2026-07-01": {
            "expose_in": 4, "click_in": 4, "ok_in": 4,
            "src": {"expose:auto": 1, "click:auto": 1, "ok:auto": 1}}}}
        cases = {}
        r = sug(doc, False)
        cases["达标→建议(剔除auto后3/3/100%)"] = r["suggest"] is True and r["manual"]["click"] == 3
        cases["reason 人话含成功率"] = "成功率 100%" in r["reason"]
        cases["已开启→不推销"] = sug(doc, True)["suggest"] is False
        doc2 = {"days": {"2026-07-01": {"expose_in": 2, "click_in": 2, "ok_in": 2}}}
        cases["样本不足→不建议"] = sug(doc2, False)["suggest"] is False
        doc3 = {"days": {"2026-07-01": {"expose_in": 5, "click_in": 5, "ok_in": 4, "fail_in": 1}}}
        cases["成功率80%<95%→不建议"] = sug(doc3, False)["suggest"] is False
        doc4 = {"days": {"2026-07-01": {"expose_in": 3, "click_in": 3, "ok_in": 3}}}
        cases["恰好踩线3/3/100%→建议"] = sug(doc4, False)["suggest"] is True
        for label, passed in cases.items():
            if passed:
                ok(f"_devflow_suggest_auto {label}")
            else:
                ng(f"_devflow_suggest_auto {label}")
    except Exception as e:
        ng(f"建议裁决纯函数测试失败: {e}")
    if '"auto_advice"' in hub:
        ok("avatar_hub P7-2 devflow GET 带 auto_advice")
    else:
        ng("avatar_hub 缺少 P7-2 auto_advice 字段")
    # ── 前端接线 ──
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("_autoSwNotice", "即时感知处理器"),
                          ("dev_autoswitch_last", "status 顺风车消费"),
                          ("_autoSwBaselined", "首轮基线防重放"),
                          ("已自动热切到", "成功 toast 文案"),
                          ("checkAutoSwAdvice", "建议裁决拉取"),
                          ("hub_autosw_hint_done", "一次性提示永久退场"),
                          ("autoSwHintEnable", "提示条一键开启")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 P7 {label}")
        else:
            ok(f"hub.js P7 {label}")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if ui.count("autoSwAdvice") >= 2 and "不再提示" in ui:
        ok("ui.html P7-2 建议条（用户/专家两视图 + 不再提示）")
    else:
        ng("ui.html 缺少 P7-2 建议条")
    # ── RVC PortAudio 三重防猝死（23:42/23:57 两起事故的回归锁）──
    api = (ROOT / "Retrieval-based-Voice-Conversion-WebUI" / "api_240604.py").read_text(encoding="utf-8")
    for needle, label in [("_dev_snapshot", "转换中枚举回内存快照(零原生调用)"),
                          ("_vc_transition_ts", "起停沿宽限计时"),
                          ("_dev_enum_lock", "枚举互斥锁"),
                          ("if self.flag_vc and getattr(self, \"_dev_snapshot\"", "转换中直接回快照"),
                          ("get_devices(force=True)", "set_devices 主动解冻")]:
        if needle not in api:
            ng(f"api_240604 缺少防猝死件: {label}")
        else:
            ok(f"api_240604 {label}")
    # 语义断言：pyaudiowpatch 第二实例只许空闲时碰（allow_native 守着），转换中碰=复发 23:57 事故
    seg = api[api.find("def get_devices"):api.find("def set_devices")]
    if "if allow_native:" in seg and seg.find("allow_native = not self.flag_vc") < seg.find("import pyaudiowpatch"):
        ok("api_240604 paw 第二实例枚举被 allow_native 看守")
    else:
        ng("api_240604 paw 枚举缺 allow_native 看守")


def test_p8_rescue_advice_metrics():
    """P8 双件套：①自动热切失败自救卡（rvc_conv 顺风车 + 常驻卡进/退场 + 再试/重启双动作 + rescue 来源）
    ②建议条效果埋点（advice_* 独立键纯函数真跑：不污染主漏斗/裁决分母 + 周报/ops 出数 + 前端三事件）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    import json

    # ── ① 自救卡后端顺风车 ──
    for needle, label in [('"rvc_conv"', "status 顺风车转换旗标"),
                          ("_DEVFLOW_ADVICE_EVS", "advice 事件白名单")]:
        if needle in hub:
            ok(f"avatar_hub P8 {label}")
        else:
            ng(f"avatar_hub 缺少 P8 {label}")
    # ── ② advice_* 纯函数真跑：独立键、不污染主漏斗与 P7-2 裁决 ──
    try:
        tree = ast.parse(hub)
        want = ("_devflow_bump_core", "_devflow_funnel", "_devflow_advice_total",
                "_devflow_suggest_auto", "_devflow_week_report", "_devflow_week_text")
        fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in want]
        assert len(fns) == len(want), f"缺函数: {want} 只找到 {[f.name for f in fns]}"
        for f in fns:
            f.decorator_list = []
        ns = {"json": json, "_DEVFLOW_EVS": ("expose", "click", "ok", "fail"),
              "_DEVFLOW_ADVICE_EVS": ("advice_expose", "advice_enable", "advice_dismiss")}
        exec(compile(ast.Module(body=fns, type_ignores=[]), "avatar_hub._p8", "exec"), ns)
        bump, funnel = ns["_devflow_bump_core"], ns["_devflow_funnel"]
        adv_tot, sug = ns["_devflow_advice_total"], ns["_devflow_suggest_auto"]
        doc = {}
        for ev in ("advice_expose", "advice_expose", "advice_enable"):
            doc = bump(doc, ev, "", "", "2026-07-07")
        doc = bump(doc, "advice_dismiss", "in", "", "2026-07-07")   # kind 应被忽略
        doc = bump(doc, "expose", "in", "strip", "2026-07-07")      # 主漏斗对照组
        cases = {}
        b = doc["days"]["2026-07-07"]
        cases["advice 键无 in/out 后缀"] = b.get("advice_expose") == 2 and b.get("advice_dismiss") == 1
        cases["主漏斗不被 advice 污染(expose=1)"] = funnel(doc)["expose"] == 1
        a = adv_tot(doc)
        cases["advice_total 聚合+采纳率(2/1/1,50%)"] = (a["expose"], a["enable"], a["dismiss"], a["enable_rate"]) == (2, 1, 1, 0.5)
        cases["P7-2 裁决分母不含 advice(样本不足)"] = sug(doc, False)["manual"]["expose"] == 1
        cases["非法 ev 原样返回"] = bump({"x": 1}, "advice_bogus", "", "", "2026-07-07") == {"x": 1}
        rep = ns["_devflow_week_report"]({"days": {"2026-06-29": {"advice_expose": 3, "advice_enable": 1}}},
                                         today=__import__("datetime").date(2026, 7, 7))
        cases["周报含 advice 段(3/1/0)"] = (rep.get("advice") or {}).get("expose") == 3
        txt = ns["_devflow_week_text"](rep)
        cases["周报文案含开启建议行"] = "开启建议" in txt and "曝光 3" in txt
        rep0 = ns["_devflow_week_report"]({}, today=__import__("datetime").date(2026, 7, 7))
        cases["零 advice 周不占行"] = "开启建议" not in ns["_devflow_week_text"](rep0)
        for label, passed in cases.items():
            if passed:
                ok(f"P8 advice {label}")
            else:
                ng(f"P8 advice {label}")
    except Exception as e:
        ng(f"P8 advice 纯函数测试失败: {e}")
    if '"advice": _devflow_advice_total(doc)' in hub:
        ok("avatar_hub devflow GET 带 advice 聚合")
    else:
        ng("avatar_hub devflow GET 缺 advice 聚合")
    # ── 前端接线 ──
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("autoSwFail", "自救卡状态"),
                          ("_autoSwFailShow", "失败进场(基线轮也挂卡)"),
                          ("autoSwRescueRestart", "直接重启变声动作"),
                          ("d.rvc_conv", "转换旗标消费(退场判据)"),
                          ("_advFlow", "advice 埋点 helper"),
                          ("advice_expose", "曝光埋点"),
                          ("advice_enable", "采纳埋点"),
                          ("advice_dismiss", "婉拒埋点")]:
        if needle in hub_js:
            ok(f"hub.js P8 {label}")
        else:
            ng(f"hub.js 缺少 P8 {label}")
    seg = hub_js[hub_js.find("_autoSwNotice(asw, convOn)"):hub_js.find("_autoSwFailShow(asw, toast)")]
    if "convOn && this.autoSwFail" in seg and "asw.ok===false && !convOn" in seg:
        ok("hub.js 自救卡退场(转换恢复)+基线失败进场语义")
    else:
        ng("hub.js 自救卡进/退场语义缺失")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if ui.count("autoSwFail") >= 4 and "devHotSwitch('rescue','in')" in ui and ui.count("autoSwRescueRestart()") >= 2:
        ok("ui.html P8-1 自救卡（用户/专家两视图·再试+重启双动作）")
    else:
        ng("ui.html 缺少 P8-1 自救卡")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    if "d.advice" in ops and "开启建议" in ops and "rescue=" in ops:
        ok("ops.html P8-2 建议条效果读数 + rescue 口径注")
    else:
        ng("ops.html 缺少 P8-2 建议条效果读数")


def test_p9dev_interact_dedupe():
    """P9 双件套：①交互级前端冒烟（tools/_fe_interact.py 真点自救卡/建议条，按钮文案与 ui.html 互锁）
    ②设备重名根治（PortAudio 截断名撞 x-for :key → Alpine 崩渲染；后端 _devices_payload 纯函数真跑去重 + 前端双保险）。"""
    tool = ROOT / "tools" / "_fe_interact.py"
    if not tool.exists():
        ng("缺 tools/_fe_interact.py 交互冒烟脚本")
        return
    src = tool.read_text(encoding="utf-8")
    for needle, label in [("advice_enable", "采纳埋点断言"), ("advice_dismiss", "婉拒埋点断言"),
                          ('"rescue"', "自救来源断言"), ("rvc/hot_switch", "热切请求拦截"),
                          ("rvc/start", "重启请求拦截"), ("pageerror", "零页错兜底"),
                          ("Duplicate key on x-for", "key 撞车哨兵"),
                          ("_autoSwNotice(null, true)", "按真相收敛断言"),
                          ("api/audio/mic_test", "试音请求拦截(P10)"),
                          ("api/audio/output_test", "试听请求拦截(P10)"),
                          ("micTestJudge().state==='good'", "试音7天裁决断言(P10)"),
                          ("outTestJudge().state", "回环裁决断言(P10)"),
                          ("i=>i.key==='mictest'", "就绪度联动断言(P10)")]:
        if needle in src:
            ok(f"_fe_interact {label}")
        else:
            ng(f"_fe_interact 缺 {label}")
    # 按钮文案互锁：交互脚本按可见文案定位按钮——ui.html 改文案不同步改脚本，这里先红
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for label in ("开启自动热切", "不再提示", "再试一次热切", "直接重启变声",
                  "试音", "试听", "设备缺席自动热切"):
        if label in src and label in ui:
            ok(f"按钮文案互锁「{label}」(脚本↔ui.html 同在)")
        else:
            ng(f"按钮文案互锁「{label}」断裂(脚本 {label in src} / ui {label in ui})")
    # 契约锁：交互冒烟 mock 的响应形状必须与真端点一致（字段改名→mock 假绿），钉住产出字段
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    probe_src = (ROOT / "tools" / "dev_probe.py").read_text(encoding="utf-8")
    for where, needle, label in [
            ("hub", '"level": level, "verdict": verdict', "mic_test 产出 level/verdict"),
            ("hub", 'res["probe"] = r["probe"]', "output_test 透传 probe"),
            ("probe", "heard=bool(", "回环产出 heard 布尔")]:
        if needle in (hub if where == "hub" else probe_src):
            ok(f"契约锁 {label}")
        else:
            ng(f"契约锁断裂: {label}（真端点字段变了，_fe_interact mock 需同步）")
    # ── ② 设备重名根治：_devices_payload 纯函数真跑（结构化层缺依赖走 except，不影响去重主径）──
    try:
        tree = ast.parse(hub)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_devices_payload")
        ns = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "avatar_hub._devices_payload", "exec"), ns)
        r = ns["_devices_payload"](["麦A", "麦A", "麦B"], ["出X", "出X"], "test")
        if r["input_devices"] == ["麦A", "麦B"] and r["output_devices"] == ["出X"]:
            ok("_devices_payload 保序去重(重名截断设备只留首个)")
        else:
            ng(f"_devices_payload 去重失效: {r['input_devices']} / {r['output_devices']}")
        if r["ok"] and r["source"] == "test":
            ok("_devices_payload 旧字段契约不变")
        else:
            ng("_devices_payload 旧字段契约被破坏")
    except Exception as e:
        ng(f"_devices_payload 纯函数测试失败: {e}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    if "new Set(d.input_devices" in hub_js and "new Set(d.output_devices" in hub_js:
        ok("hub.js 设备列表双保险去重(旧后端/直连场景兜底)")
    else:
        ng("hub.js 缺设备列表双保险去重")
    if "feinteract" in (ROOT / "acceptance.py").read_text(encoding="utf-8"):
        ok("acceptance 纳编 feinteract 交互冒烟")
    else:
        ng("acceptance 未纳编 feinteract")


def test_p11dev_fe_patrol():
    """P11 前端交互日巡：tools/_fe_patrol.py 编排 + bat/ps1 注册 + /ops 读数 + alerts 同步接线。"""
    patrol = ROOT / "tools" / "_fe_patrol.py"
    if not patrol.exists():
        ng("缺 tools/_fe_patrol.py 日巡脚本")
        return
    src = patrol.read_text(encoding="utf-8")
    for needle, label in [("_fe_smoke.py", "smoke 子项"),
                          ("tools/_fe_interact.py", "interact 子项"),
                          ("hub_offline", "Hub 离线 SKIP"),
                          ("hub_died_midrun", "Hub 半途掉线降级 SKIP"),
                          ("playwright_missing", "playwright 缺失 SKIP"),
                          ('f"fe_patrol:{key}"', "告警 key 模板"),
                          ("fe_patrol_history.jsonl", "历史落盘"),
                          ("== 前端日巡", "机器可解析摘要行")]:
        if needle in src:
            ok(f"_fe_patrol {label}")
        else:
            ng(f"_fe_patrol 缺 {label}")
    bat = ROOT / "fe_patrol_once.bat"
    if bat.exists() and "tools\\_fe_patrol.py" in bat.read_text(encoding="utf-8"):
        ok("fe_patrol_once.bat 接线")
    else:
        ng("fe_patrol_once.bat 缺失或未调用 _fe_patrol.py")
    ps1 = ROOT / "register_fe_patrol_task.ps1"
    if ps1.exists() and "AvatarHub_FePatrol" in ps1.read_text(encoding="utf-8"):
        ok("register_fe_patrol_task.ps1 任务名接线")
    else:
        ng("register_fe_patrol_task.ps1 缺失")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    if "/api/ops/fe_patrol" in hub and "fe_patrol_history.jsonl" in hub:
        ok("avatar_hub /api/ops/fe_patrol 端点")
    else:
        ng("avatar_hub 缺 /api/ops/fe_patrol")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    if "fePatrol" in ops and "/api/ops/fe_patrol" in ops and "AvatarHub_FePatrol" in ops:
        ok("ops.html 前端日巡卡")
    else:
        ng("ops.html 缺前端日巡卡")
    # Hub 离线 → skip：子进程 + 不可达端口（真跑 _hub_up，不依赖 exec）
    try:
        import subprocess
        env = dict(os.environ)
        env["ACCEPT_HUB"] = "http://127.0.0.1:1"
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run(
            [sys.executable, "-X", "utf8", str(patrol), "--json"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15)
        data = json.loads((p.stdout or "").strip().splitlines()[-1])
        if data.get("skip") and data.get("skip_reason") == "hub_offline" and p.returncode == 0:
            ok("_fe_patrol Hub 离线 SKIP 子进程真跑")
        else:
            ng(f"_fe_patrol Hub 离线 SKIP 失效: rc={p.returncode} {data}")
    except Exception as e:
        ng(f"_fe_patrol Hub 离线 SKIP 测试异常: {e}")


def test_human_rating():
    """P7 四期: 主观两问速记——真人观察仅剩主观项(贴缝/精修观感)点选即入场次账本,量化+主观同一本账。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [('"/realtime/swap/sessions/rate"', "评注端点"),
                          ("_swap_sess_rate_apply", "评注合并纯函数"),
                          ("_SWAP_SESS_FILE_LOCK", "账本文件互斥锁"),
                          ('"评注·改选覆盖+备注保留"', "selftest 覆盖评注路径")]:
        if needle not in hub:
            ng(f"avatar_hub 缺少 主观速记 {label}")
        else:
            ok(f"avatar_hub 主观速记 {label}")
    hub_js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("rateSwapRecap", "点选评注"),
                          ("noteSwapRecap", "备注入账"),
                          ("主观: 贴缝", "复制成绩单带主观行")]:
        if needle not in hub_js:
            ng(f"hub.js 缺少 {label}")
        else:
            ok(f"hub.js {label} 存在")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "主观速记" not in ui:
        ng("ui.html 成绩单缺「主观速记」行")
    else:
        ok("ui.html 成绩单「主观速记」行存在")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("q.human", "下钻显示主观速记"), ("主观贴缝", "CSV 主观列")]:
        if needle not in ops:
            ng(f"ops.html 缺少 {label}")
        else:
            ok(f"ops.html {label}")


def test_port_guard():
    """06o: uvicorn 系服务双开预检——Windows REUSE 双绑串线的通用防线(hub/vcam/interp 接入)。"""
    try:
        sys.path.insert(0, str(ROOT))
        import port_guard
        r = port_guard.selftest()      # 真演练:REUSE 监听者在场必须探出、释放后必须放行
        if r.get("ok"):
            ok("port_guard 自检(占用探出+释放放行)")
        else:
            ng(f"port_guard 自检失败: {r}")
    except Exception as e:
        ng(f"port_guard 导入/自检异常: {e}")
    for f, needle, label in [
            ("avatar_hub.py", "port_guard.ensure_port_free", "hub 双开预检"),
            ("vcam_server.py", "port_guard.ensure_port_free", "vcam 双开预检"),
            ("live_interpreter.py", "port_guard.ensure_port_free", "interpreter 双开预检")]:
        if needle not in (ROOT / f).read_text(encoding="utf-8"):
            ng(f"{f} 缺 {label}")
        else:
            ok(f"{f} {label}接入")


def test_port_override():
    """2026-07-17 两套安装并存：端口覆盖层(config.json ports/port_offset)三场景回归。
    子进程隔离跑(app_config 端口在 import 时定格，不能污染本进程)：
    ① 零配置=出厂端口(零回归)；② 偏移整段平移(含 SERVICES/env 注入/faceswap2 联动)；
    ③ config.json 精确覆盖+偏移并用优先级；④ 冲突自检要能抓到 +100 这类会撞回出厂集的偏移。"""
    import subprocess
    code = r'''
import sys, os, json, tempfile, pathlib
os.environ.pop("AVATARHUB_PORT_OFFSET", None)
tmp0 = tempfile.mkdtemp()                      # 空目录当 BASE:保证无 config.json 干扰
os.environ["AVATARHUB_BASE"] = tmp0
sys.path.insert(0, sys.argv[1])
import app_config as ac
assert ac.PORT_OFFSET == 0 and ac.port("hub") == 9000 and ac.port("interpreter") == 7900
assert ac.port("faceswap2") == 8003 and ac.port_env_extra("fish_tts") == {}
assert ac.SERVICES["faceswap2"]["env_extra"]["FACESWAP_PORT"] == "8003"
os.environ["AVATARHUB_PORT_OFFSET"] = "2000"   # ② 偏移(推荐值:与出厂集无交集)
del sys.modules["app_config"]
import app_config as ac2
assert ac2.port("hub") == 11000 and ac2.SERVICES["interpreter"]["port"] == 9900
assert ac2.port_env_extra("fish_tts") == {"FISH_PORT": "9855"}
assert ac2.SERVICES["faceswap2"]["env_extra"]["FACESWAP_PORT"] == "10003"
assert not ac2.PORT_COLLISIONS, ac2.PORT_COLLISIONS   # +2000 必须零冲突
os.environ["AVATARHUB_PORT_OFFSET"] = "100"    # ④ 坏偏移:同传 7900+100 撞出厂换脸 8000
del sys.modules["app_config"]
import app_config as acx
assert any("interpreter" in c for c in acx.PORT_COLLISIONS), acx.PORT_COLLISIONS
os.environ["AVATARHUB_PORT_OFFSET"] = "1000"   # ④b 坏偏移:换脸 8000+1000 撞出厂 hub 9000
del sys.modules["app_config"]
import app_config as acy
assert any("faceswap" in c for c in acy.PORT_COLLISIONS), acy.PORT_COLLISIONS
del os.environ["AVATARHUB_PORT_OFFSET"]        # ③ 精确覆盖+偏移并用(真 config.json)
tmp = tempfile.mkdtemp()
pathlib.Path(tmp, "config.json").write_text(
    json.dumps({"port_offset": 2000, "ports": {"interpreter": 7999}}), encoding="utf-8")
os.environ["AVATARHUB_BASE"] = tmp
del sys.modules["app_config"]
import app_config as ac3
assert ac3.port("interpreter") == 7999 and ac3.port("hub") == 11000
print("PORT-OVERRIDE-OK")
'''
    try:
        r = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                           capture_output=True, text=True, timeout=90)
        if "PORT-OVERRIDE-OK" in (r.stdout or ""):
            ok("端口覆盖层三场景(零配置/偏移/精确覆盖)+冲突自检")
        else:
            ng(f"端口覆盖层回归失败: {(r.stderr or r.stdout or '')[-400:]}")
    except Exception as e:
        ng(f"端口覆盖层测试异常: {e}")
    # 静态门禁:消费方不许再绕过覆盖层硬编码默认端口
    for f, needle, label in [
            ("avatar_hub.py", 'app_config.port("hub")', "hub 端口经覆盖层"),
            ("launcher_qt.py", "_auto_port_avoid", "启动器端口自动避让"),
            ("service_supervisor.py", "port_env_extra", "守护注入子进程端口"),
            ("service_manager.py", "port_env_extra", "启动器注入子进程端口"),
            ("static/hub.js", "/api/ports", "前端端口注入")]:
        if needle not in (ROOT / f).read_text(encoding="utf-8"):
            ng(f"{f} 缺 {label}")
        else:
            ok(f"{f} {label}")


def test_p8s_mainface_hyst():
    """06s 锁主脸双人档联动: 主脸滞回纯函数真跑(AST 抽函数,免模型加载) + hint 链路 + UI 让位语义。"""
    src = (ROOT / "faceswap_api.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_pick_main_face")
        ns = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "faceswap_api._pick_main_face", "exec"), ns)
        pick = ns["_pick_main_face"]

        class F:                                # 只要 .bbox 的鸭子脸
            def __init__(self, x1, y1, x2, y2):
                self.bbox = [x1, y1, x2, y2]
        left = F(0, 0, 100, 100)                # 面积 10000
        right = F(200, 0, 310, 110)             # 面积 12100（1.21×，近等大）
        giant = F(200, 0, 350, 150)             # 面积 22500（2.25×，真更大）
        cases = {
            "无hint=最大脸(旧行为)": pick([left, right], None) is right,
            "hint在位者·挑战1.21×<1.3不换主": pick([left, right], [50.0, 50.0]) is left,
            "hint在位者·挑战2.25×≥1.3换主": pick([left, giant], [50.0, 50.0]) is giant,
            "hint指向大脸=大脸继续在位": pick([left, right], [255.0, 55.0]) is right,
            "hint畸形→回退最大脸不崩": pick([left, right], ["x"]) is right,
        }
        for label, passed in cases.items():
            if passed:
                ok(f"_pick_main_face {label}")
            else:
                ng(f"_pick_main_face {label}")
    except StopIteration:
        ng("faceswap_api.py 缺 _pick_main_face(主脸滞回纯函数)")
    except Exception as e:
        ng(f"_pick_main_face 行为验证异常: {e}")
    if "main_face_hint" not in src:
        ng("faceswap_api.py 缺 main_face_hint 请求字段")
    else:
        ok("faceswap_api.py main_face_hint 请求字段")
    rt = (ROOT / "realtime_stream.py").read_text(encoding="utf-8")
    if 'payload["main_face_hint"]' not in rt:
        ng("realtime_stream 未下发主脸滞回提示")
    else:
        ok("realtime_stream 下发主脸滞回提示(裁剪/全帧坐标映射)")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    uihtml = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "dual:true" not in js or "锁主脸」自动让位" not in js:
        ng("hub.js facesChip 缺双人档让位态")
    else:
        ok("hub.js facesChip 双人档让位态(蓝)")
    if "facesChip().dual" not in uihtml:
        ng("ui.html 多脸chip 缺双人档配色分支")
    else:
        ok("ui.html 多脸chip 双人档配色分支")


def test_p8t_orphan_adopt():
    """06t 孤儿流无闪断收养: _AdoptedProc AST 抽类真跑(真收养/真杀/PID复用防线) + 接线 needles。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(hub)
        nodes = [n for n in tree.body
                 if (isinstance(n, ast.FunctionDef) and n.name == "_port_owner_pid")
                 or (isinstance(n, ast.ClassDef) and n.name == "_AdoptedProc")]
        if len(nodes) != 2:
            ng(f"avatar_hub 缺 _port_owner_pid/_AdoptedProc(找到 {len(nodes)}/2)")
            return
        import logging as _lg
        import subprocess as _sp
        ns = {"_subprocess": _sp, "_logger": _lg.getLogger("p8t")}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "avatar_hub._AdoptedProc", "exec"), ns)
        Adopted = ns["_AdoptedProc"]
        # ① 真收养真杀：起一个哑子进程,仅凭 PID 认领→poll 在世→terminate→poll 判死
        child = _sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        try:
            a = Adopted(child.pid)
            if a.poll() is None:
                ok("_AdoptedProc 收养在跑进程→poll 在世")
            else:
                ng("_AdoptedProc 收养在跑进程却判死")
            a.terminate()
            import time as _t
            deadline = _t.time() + 8
            while _t.time() < deadline and child.poll() is None:
                _t.sleep(0.3)
            if child.poll() is not None and a.poll() is not None:
                ok("_AdoptedProc terminate 真杀 + poll 判死")
            else:
                ng("_AdoptedProc terminate 未杀死被收养进程")
        finally:
            try:
                child.kill()
            except Exception:
                pass
        # ② PID 复用防线：端口属主≠登记 PID → terminate 拒杀现任。
        #    属主反查打桩成"别人"(定值)——真反查依赖运行环境(gate 的无窗口宿主里 netstat
        #    可返回空、基础环境无 psutil)，会把环境问题误报成防线失效;防线逻辑本身与
        #    反查实现无关。真反查已由 ① 的真杀路径 + hub 运行时(psutil)覆盖。
        child2 = _sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                           creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        try:
            import os as _os
            ns["_port_owner_pid"] = lambda p: _os.getpid()   # 端口属主=测试进程,绝非 child2
            b = Adopted(child2.pid, 65530)
            b.terminate()
            import time as _t
            _t.sleep(0.6)
            if child2.poll() is None and b.poll() is not None:
                ok("_AdoptedProc PID复用防线: 端口易主→拒杀现任进程")
            else:
                ng("_AdoptedProc PID复用防线失效(端口易主仍杀)")
        finally:
            try:
                child2.kill()
            except Exception:
                pass
    except Exception as e:
        ng(f"_AdoptedProc 行为验证异常: {e}")
    for needle, label in [("HUB_ORPHAN_ADOPT", "收养开关(默认开)"),
                          ("无闪断接管孤儿画面进程", "收养日志/时间线事件"),
                          ('"orphan_adopted"', "status 收养字段")]:
        if needle not in hub:
            ng(f"avatar_hub 缺 {label}")
        else:
            ok(f"avatar_hub {label}")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    uihtml = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "orphanAdopted" not in js or "orphan_adopted" not in js:
        ng("hub.js 未接收收养状态")
    else:
        ok("hub.js 接收收养状态")
    if "已接管在跑画面" not in uihtml:
        ng("ui.html 缺已接管 chip")
    else:
        ok("ui.html 已接管 chip")


def test_p9_hot_params_persist_calib():
    """P9(换脸画质方案收口): 参数热更端点 + 效果配置服务端持久化 + 一键画质标定接线。"""
    rt = (ROOT / "realtime_stream.py").read_text(encoding="utf-8")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    uihtml = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    for needle, label in [('/swap/params', "参数热更端点"),
                          ('face_params[k] = upd[k]', "热更写入请求级参数"),
                          ('upd["crossfade"]', "crossfade 热更"),
                          ('upd["out_q"]', "输出画质热更")]:
        if needle not in rt:
            ng(f"realtime_stream 缺 {label}")
        else:
            ok(f"realtime_stream {label}")
    for needle, label in [('"/realtime/swap/params"', "hub 参数热更代理"),
                          ('"/api/effect_cfg"', "效果配置持久化端点"),
                          ('_EFFECT_CFG_FILE', "效果配置落盘文件"),
                          ('"/api/swap/calibrate"', "一键标定任务端点"),
                          ('"/api/swap/calibrate/status"', "标定状态端点"),
                          ('_video_env()', "标定阈值注入开播环境"),
                          ('SWAP_CALIB_APPLY', "标定注入逃生门")]:
        if needle not in hub:
            ng(f"avatar_hub 缺 {label}")
        else:
            ok(f"avatar_hub {label}")
    for needle, label in [("liveParamsPush", "前端参数热更推送"),
                          ("/api/effect_cfg", "前端服务端配置同步"),
                          ("calibRun", "前端一键标定"),
                          ("calibSavedLine", "前端已标定摘要")]:
        if needle not in js:
            ng(f"hub.js 缺 {label}")
        else:
            ok(f"hub.js {label}")
    for needle, label in [('liveParamsPush()', "参数控件挂热更"),
                          ('在播即调即生效', "在播徽章文案"),
                          ('一键画质标定', "标定按钮")]:
        if needle not in uihtml:
            ng(f"ui.html 缺 {label}")
        else:
            ok(f"ui.html {label}")
    # 语义护栏：文案不再声称「下次开播生效」唯一真相(在播可热更)
    if "高级 · 下次开播生效" in uihtml:
        ng("ui.html 仍残留固定「下次开播生效」徽章(应随在播态切换)")
    else:
        ok("ui.html 「下次开播生效」徽章已随在播态切换")


def test_p8u_adopt_backfill_away():
    """06u: 收养场景自愈回填(_adopt_body_from_status 纯函数真跑) + 离席画面自定义链路。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(hub)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_adopt_body_from_status")
        ns = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "hub._adopt_body_from_status", "exec"), ns)
        f = ns["_adopt_body_from_status"]
        cases = {
            "target优先+fps_cap": f({"auto": {"target": "hd", "effective": "natural"},
                                     "params": {"fps_cap": 12}}) == {"swap_preset": "hd", "swap_fps": 12},
            "无auto退preset": f({"preset": "beauty", "params": {}}) == {"swap_preset": "beauty"},
            "非法档名不回填": f({"auto": {"target": "ultra"}, "params": {}}) == {},
            "垃圾输入→空body": f({"auto": None, "params": None}) in ({},),
        }
        for label, passed in cases.items():
            (ok if passed else ng)(f"_adopt_body_from_status {label}")
    except StopIteration:
        ng("avatar_hub 缺 _adopt_body_from_status")
    except Exception as e:
        ng(f"_adopt_body_from_status 行为验证异常: {e}")
    if "_LAST_VIDEO_BODY = _adopt_body_from_status(j)" not in hub:
        ng("收养未回填 _LAST_VIDEO_BODY(自愈重启会丢画质档)")
    else:
        ok("收养回填 _LAST_VIDEO_BODY 接线")
    if '"/realtime/swap/away"' not in hub:
        ng("hub 缺 /realtime/swap/away 代理")
    else:
        ok("hub /realtime/swap/away 代理")
    rt = (ROOT / "realtime_stream.py").read_text(encoding="utf-8")
    for needle, label in [("SWAP_AWAY_TEXT", "离席角标文案可自定义"),
                          ("SWAP_AWAY_IMAGE", "离席品牌图路径"),
                          ('"/swap/away"', "离席画面热切口"),
                          ("_away_brand_img", "品牌图 cover 填充(坏图退 blur)"),
                          ("0 < bw < w", "空角标跳过叠加"),
                          ("预览恒与观众同源", "/swapped 预览=观众所见(离席画面不隐身)")]:
        if needle not in rt:
            ng(f"realtime_stream 缺 {label}")
        else:
            ok(f"realtime_stream {label}")


def test_p8v_away_settings():
    """06v: 离席画面设置闭环——持久(effect_cfg)/开播注入(_away_env_from_cfg 纯函数真跑)/UI 卡。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    try:
        import os as _os
        tree = ast.parse(hub)
        fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name in ("_away_env_from_cfg", "_effect_cfg_str_clean")]
        if len(fns) != 2:
            raise RuntimeError("缺函数定义")
        ns = {"os": _os, "_EFFECT_CFG_STR": {"awayText": 40, "awayImage": 120}}
        exec(compile(ast.Module(body=fns, type_ignores=[]), "hub.p8v", "exec"), ns)
        env_fn, clean = ns["_away_env_from_cfg"], ns["_effect_cfg_str_clean"]
        d = r"C:\x\bg_images"
        full = env_fn({"awayStyle": "image", "awayText": "去去就回", "awayImage": "brand.jpg"}, d)
        cases = {
            "全量注入": full == {"SWAP_AWAY_STYLE": "image", "SWAP_AWAY_TEXT": "去去就回",
                                 "SWAP_AWAY_IMAGE": _os.path.join(d, "brand.jpg")},
            "image无图降blur": env_fn({"awayStyle": "image"}, d).get("SWAP_AWAY_STYLE") == "blur",
            "空文案显式注入(撤角标)": env_fn({"awayText": ""}, d) == {"SWAP_AWAY_TEXT": ""},
            "没存过=不注入": env_fn({}, d) == {},
            "文件名合法通过": clean("awayImage", "brand.jpg") == "brand.jpg",
            "路径成分拒绝": clean("awayImage", r"..\..\sam.jpg") is None,
            "文案超40字拒绝": clean("awayText", "长" * 41) is None,
        }
        for label, passed in cases.items():
            (ok if passed else ng)(f"away 设置 {label}")
    except Exception as e:
        ng(f"away 设置纯函数验证异常: {e}")
    for needle, label in [("_EFFECT_CFG_STR", "effect_cfg 字符串白名单"),
                          ('"/api/bg_images"', "bg_images 目录清单口"),
                          ("_away_env_from_cfg(_effect_cfg_load()", "开播 env 注入接线"),
                          ("await _away_align_adopted()", "收养后离席配置热推对齐(06x)")]:
        (ok if needle in hub else ng)(f"hub {label}")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    (ok if ("saveAway" in js and "loadAwayCfg" in js and "applyAway" in js) else ng)("hub.js 离席卡逻辑")
    (ok if ("离席画面" in html and "awayCfg.style" in html) else ng)("ui.html 离席画面卡")


def test_p10_devprobe_isolation():
    """P10(Hub 稳定性收口): 原生设备栈(sounddevice/DirectShow)全部隔离进 dev_probe 子进程。
    行为级：dev_probe 真跑 audio_devices/named_cameras/resolve 三个子命令(不开录音/摄像头,
    快且无副作用)；接线级：hub 内不得再残留进程内原生调用。"""
    probe = ROOT / "tools" / "dev_probe.py"
    if not probe.exists():
        ng("tools/dev_probe.py 不存在")
        return
    import json
    import subprocess as _sp
    # 与生产同解释器：hub._dev_probe 用 facefusion 环境(带 sounddevice/pygrabber)跑探测；
    # 门禁若用 sys.executable(基础环境,缺 sounddevice)等于测一个生产永远不用的组合。
    try:
        import app_config as _ac
        _probe_py = _ac.conda_python("facefusion")
    except Exception:
        _probe_py = sys.executable
    for cmd, chk, label in [
            (["audio_devices"], lambda r: isinstance(r.get("inputs"), list) and isinstance(r.get("outputs"), list), "音频枚举"),
            (["named_cameras"], lambda r: isinstance(r.get("cameras"), list), "DirectShow 摄像头名"),
            (["resolve", "--name", "不存在的设备 (MME)", "--kind", "in"], lambda r: r.get("index") is None, "设备名解析(未命中→默认)")]:
        try:
            p = _sp.run([_probe_py, "-X", "utf8", str(probe), *cmd],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=40, cwd=str(ROOT),
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            r = None
            for ln in reversed((p.stdout or "").strip().splitlines()):
                if ln.strip().startswith("{"):
                    r = json.loads(ln)
                    break
            if r and r.get("ok") and chk(r):
                ok(f"dev_probe {label} 真跑通过")
            else:
                ng(f"dev_probe {label} 失败: rc={p.returncode} out={(p.stdout or '')[-120:]}")
        except Exception as e:
            ng(f"dev_probe {label} 异常: {e}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("def _dev_probe(", "探测子进程调用器"),
                          ("_pick_camera_source_safe", "摄像头选源隔离包装"),
                          ("_probe_live_safe", "活帧探测隔离包装"),
                          ("_named_cameras_cached", "DirectShow 名单缓存")]:
        if needle not in hub:
            ng(f"avatar_hub 缺 {label}")
        else:
            ok(f"avatar_hub {label}")
    # 语义护栏：hub 进程内不得再直接触碰原生设备栈(崩溃面回归防线)
    # 2026-07-10 精化: cv2.VideoCapture 只禁「开摄像头」(设备索引/整数参)。素材上传/抠像新增的
    # 「视频文件解码」VideoCapture(str(路径)) 走 ffmpeg 文件路径,不碰 DirectShow/相机崩溃面,放行——
    # 原样禁整个子串会把文件解码误伤成红灯(2026-07-10 实撞)。
    _cam_opens = [mm.group(0) for mm in
                  re.finditer(r"cv2\.VideoCapture\(\s*([^)\n]*)\)", hub)
                  if "str(" not in mm.group(1)]
    if _cam_opens:
        ng(f"avatar_hub 残留进程内原生调用: cv2 摄像头进程内打开 {_cam_opens[:2]}")
    else:
        ok("avatar_hub 无进程内 cv2 摄像头打开(视频文件解码已豁免)")
    for banned, label in [("import sounddevice", "sounddevice 进程内导入"),
                          ("device_enum.list_named_cameras", "pygrabber 进程内枚举"),
                          ("device_enum._probe_live", "cv2 活帧进程内探测"),
                          ("device_enum.pick_camera_source", "开摄像头选源进程内跑")]:
        if banned in hub:
            ng(f"avatar_hub 残留进程内原生调用: {label}")
        else:
            ok(f"avatar_hub 无进程内 {label}")
    # P10-2 标定周期化
    if "needs_calib" not in hub:
        ng("avatar_hub 标定状态缺 needs_calib 裁决")
    else:
        ok("avatar_hub 标定周期化裁决(needs_calib)")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    uihtml = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    if "calibStaleLine" not in js or "calibStaleLine" not in uihtml:
        ng("前端缺标定过期提示(calibStaleLine)")
    else:
        ok("前端标定过期提示接线")


def test_p11_stability_ledger():
    """P11(稳定性账本): 守护重启×崩溃事件×启动溯源三源关联。
    行为级：真解析三种日志行(含实际生产格式)+归因窗口；接线级：hub 端点 + /ops 卡片。"""
    import importlib.util
    import time as _t
    srp = ROOT / "tools" / "stability_report.py"
    if not srp.exists():
        ng("tools/stability_report.py 不存在")
        return
    spec = importlib.util.spec_from_file_location("_stab_mod", srp)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:
        ng(f"stability_report 加载失败: {e}")
        return
    # 1) 守护拉活行——用生产实际格式(2026-07-06 实录)
    wd = m.parse_watchdog_lines([
        "[2026-07-06 17:34:49] ⛔[死] avatar_hub 进程不存在(核心服务) → 自动拉起(第 1 次，注入完整环境)",
        "[2026-07-06 17:30:00] 内存 稳48% 可用31.8/62G | ...",           # 噪声行
        "[2026-07-06 12:00:00] ⛔[死] fish_tts 进程不存在(x) → 自动拉起(第 2 次)",  # 非 hub,应滤掉
    ])
    if len(wd) == 1 and abs(wd[0]["ts"] - _t.mktime(_t.strptime("2026-07-06 17:34:49", "%Y-%m-%d %H:%M:%S"))) < 1:
        ok("账本解析: 守护拉活行(生产格式)")
    else:
        ng(f"账本解析: 守护拉活行失败 {wd}")
    # 2) Boot 溯源行——logger 行入账,print 副本(无时间戳)天然滤掉不双计
    boots = m.parse_boot_lines([
        "2026-07-06 18:34:03 [INFO] [-] [Boot] pid=43776 ppid=27852 由谁拉起 → cmd.exe :: cmd  /c _launch_hub_detached.bat",
        "[Boot] pid=43776 ppid=27852 parent → cmd.exe :: cmd /c _launch_hub_detached.bat",
    ])
    if len(boots) == 1 and boots[0]["pid"] == 43776 and "_launch_hub" in boots[0]["parent"]:
        ok("账本解析: Boot 溯源行(print 副本不双计)")
    else:
        ng(f"账本解析: Boot 溯源行失败 {boots}")
    # 3) 崩溃模块归类 + 重启归因窗口
    b = m.classify_crash_module
    if (b("virtualcam_x64.dll"), b("libportaudio64bit.dll"), b("ntdll.dll"), b("weird.dll")) == \
            ("camera-native", "audio-native", "runtime", "other"):
        ok("账本解析: 崩溃模块归类(camera/audio/runtime/other)")
    else:
        ng("账本解析: 崩溃模块归类失败")
    t0 = 1783330000.0
    att = m.correlate(
        [{"ts": t0 + 18, "service": "avatar_hub"}, {"ts": t0 + 9000, "service": "avatar_hub"}],
        [{"ts": t0, "module": "virtualcam_x64.dll", "code": "c0000005", "bucket": "camera-native"}])
    if att[0]["crash"] and att[0]["crash"]["module"] == "virtualcam_x64.dll" and att[1]["crash"] is None:
        ok("账本解析: 崩溃→重启归因窗口(命中+不乱归因)")
    else:
        ng(f"账本解析: 归因失败 {att}")
    # 4) 接线：hub 端点 + /ops 卡片
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    if "/api/ops/stability" in hub and "stability_report.py" in hub:
        ok("hub 稳定性账本端点(/api/ops/stability→子进程)")
    else:
        ng("hub 缺 /api/ops/stability 端点或未走子进程")
    ops = (ROOT / "static" / "ops.html").read_text(encoding="utf-8")
    for needle, label in [("stabCard", "/ops 稳定性卡片"),
                          ("async function stability()", "/ops 取数函数"),
                          ("crashes_not_hub_after_p10", "已隔离崩溃口径")]:
        if needle in ops:
            ok(f"ops 稳定性账本 {label}")
        else:
            ng(f"ops 缺 {label}")


def test_p12_sentinel_probe_exit():
    """P12(哨兵+善终): ①dev_probe 交卷后 os._exit 硬退出——跳过 PortAudio/DirectShow 原生
    teardown(18:39 实锤崩溃点),返回码必须是干净的 0/1 而非原生崩溃码(0xC0000005=3221225477)；
    ②hub 稳定性哨兵接线(后台环+告警键+共享执行器)。"""
    import json
    import subprocess as _sp
    probe = ROOT / "tools" / "dev_probe.py"
    src = probe.read_text(encoding="utf-8")
    if "os._exit(" in src:
        ok("dev_probe 交卷即硬退出(os._exit,原生 teardown 崩溃面归零)")
    else:
        ng("dev_probe 缺 os._exit 善终硬退出")
    try:
        import app_config as _ac
        _probe_py = _ac.conda_python("facefusion")
    except Exception:
        _probe_py = sys.executable
    try:
        p = _sp.run([_probe_py, "-X", "utf8", str(probe), "audio_devices"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=40, cwd=str(ROOT),
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        r = None
        for ln in reversed((p.stdout or "").strip().splitlines()):
            if ln.strip().startswith("{"):
                r = json.loads(ln)
                break
        if r is not None and p.returncode in (0, 1):
            ok(f"dev_probe 退出码干净(rc={p.returncode},JSON 完整交回)")
        else:
            ng(f"dev_probe 退出异常: rc={p.returncode}(疑似原生 teardown 崩溃)")
    except Exception as e:
        ng(f"dev_probe 善终验证异常: {e}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("_bg_stability_sentinel", "哨兵后台环"),
                          ("hub_stability_regression", "回归告警键(fire/clear)"),
                          ("_stability_report_run", "报告执行器(哨兵/端点共用)"),
                          ("_stability_sentinel_eval", "哨兵裁决函数")]:
        if needle in hub:
            ok(f"hub 稳定性哨兵 {label}")
        else:
            ng(f"hub 缺 {label}")
    if "asyncio.create_task(_bg_stability_sentinel())" in hub:
        ok("哨兵已挂 lifespan 后台任务")
    else:
        ng("哨兵未挂 lifespan(不会自动跑)")
    # P12 补强：Boot 溯源专用账页(经 start_avatar_hub.bat 直启时控制台日志隐身,19:37 实证)
    if "hub_boots.jsonl" in hub:
        ok("hub Boot 溯源落专用账页(hub_boots.jsonl,不依赖启动方式)")
    else:
        ng("hub 缺 hub_boots.jsonl 专用账页")
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("_stab_mod2", ROOT / "tools" / "stability_report.py")
    m2 = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m2)
    got = m2.parse_boots_jsonl([
        '{"ts": 1783340000.0, "pid": 111, "ppid": 22, "parent": "cmd.exe :: start_avatar_hub"}',
        "not-json-line",
    ])
    if len(got) == 1 and got[0]["pid"] == 111 and "start_avatar_hub" in got[0]["parent"]:
        ok("账本解析: hub_boots.jsonl 主源(坏行跳过)")
    else:
        ng(f"账本解析: hub_boots.jsonl 失败 {got}")


def test_p14_swapcore_watch():
    """P14(换脸核发布哨兵): 官方 512-live 仅授权分发/社区 512 在 To-Do(2026-07-07 循证)——
    「记得去看看」改机器日查。行为级：_diff 纯函数 6 用例真跑(--selftest,不出网)；
    接线级：双源+镜像兜底+告警键+hub 日查环挂载。"""
    import json as _json
    import subprocess as _sp
    w = ROOT / "tools" / "swapcore_watch.py"
    if not w.exists():
        ng("缺 tools/swapcore_watch.py")
        return
    src = w.read_text(encoding="utf-8")
    for needle, label in [("somanchiu/reswapper", "社区线 HF 源"),
                          ("deepinsight/inswapper-512-live", "官方线 GH 源"),
                          ("hf-mirror.com", "HF 镜像兜底(本网主站不稳)"),
                          ("swapcore:release", "告警键"),
                          ("swapcore_watch.json", "基线落盘"),
                          ("net_fail", "出网失败 SKIP 不告警")]:
        if needle in src:
            ok(f"swapcore_watch {label}")
        else:
            ng(f"swapcore_watch 缺 {label}")
    try:
        p = _sp.run([sys.executable, "-X", "utf8", str(w), "--selftest"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=30, cwd=str(ROOT),
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        r = _json.loads((p.stdout or "").strip().splitlines()[-1])
        if r.get("ok") and r.get("pass") == r.get("total") and p.returncode == 0:
            ok(f"swapcore_watch diff 自测真跑({r['pass']}/{r['total']})")
        else:
            ng(f"swapcore_watch 自测失败: rc={p.returncode} {r}")
    except Exception as e:
        ng(f"swapcore_watch 自测异常: {e}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("_bg_swapcore_watch", "日查后台环"),
                          ("/api/ops/swapcore_watch", "读数端点"),
                          ("_swapcore_watch_run", "子进程执行器(出网不进 Hub)")]:
        if needle in hub:
            ok(f"hub swapcore {label}")
        else:
            ng(f"hub 缺 swapcore {label}")
    if "asyncio.create_task(_bg_swapcore_watch())" in hub:
        ok("swapcore 哨兵已挂 lifespan")
    else:
        ng("swapcore 哨兵未挂 lifespan(不会自动跑)")


def test_p13_activate_debounce():
    """P13(激活幂等防抖): 手机页把 /activate 打成连发(实测 12s 内 9 次,访问日志取证),
    每发全量预热链(切脸+跨机 spk/fish 预热+开场白缓存重载+口型预计算) → Hub RAM
    高水位棘轮(0.2→2.8G,守护误报泄漏) + 远端 TTS 被预热挤占。防抖三要素:
    ①同角色+同配置版本+窗口内 → 短路返回 deduped；②key 含 _profiles_version,
    改垫话/情绪参考后重激活必须穿透；③固定窗口(不滑动),保留手动重试语义。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [("_ACTIVATE_LAST", "防抖状态记录"),
                          ("_ACTIVATE_DEBOUNCE_S", "防抖窗口(env 可调)"),
                          ('"deduped": True', "短路响应标记"),
                          ("(name, _profiles_version)", "key 含配置版本(改内容穿透防抖)")]:
        if needle in hub:
            ok(f"activate 防抖 {label}")
        else:
            ng(f"activate 防抖缺 {label}")
    # 结构：短路判断必须在 _PROFILES_LOCK/预热链之前(白跑锁与线程=防抖失义)；
    #   全量路径落 key 必须在锁后(先验证角色存在再记账,防 404 也占坑)。
    i_fn = hub.find('async def activate_profile(')
    seg = hub[i_fn:i_fn + 4000]
    i_dedup = seg.find('"deduped": True')
    i_lock = seg.find("async with _PROFILES_LOCK")
    i_stamp = seg.find('_ACTIVATE_LAST["key"]')
    if 0 < i_dedup < i_lock < i_stamp:
        ok("activate 防抖结构(短路→锁→落 key 顺序正确)")
    else:
        ng(f"activate 防抖结构异常 dedup={i_dedup} lock={i_lock} stamp={i_stamp}")
    if "time.monotonic()" in seg[:i_dedup + 200]:
        ok("activate 防抖用单调钟(不受系统改时影响)")
    else:
        ng("activate 防抖未用单调钟")


def test_uivr_routine():
    """06y: UI 可视化回归纳入例行——acceptance 快速集含 uivr 项(夜检 5:00 自动跑) +
    gate --online Tier U 实跑后对嵌套 acceptance 的 uivr 去重(不双跑浏览器)。"""
    acc = (ROOT / "acceptance.py").read_text(encoding="utf-8")
    for needle, label in [('("uivr",', "SUITE 含 uivr 项"),
                          ("def test_uivr", "uivr 运行器"),
                          ("ACCEPT_SKIP_UIVR", "嵌套去重开关"),
                          ('key == "uivr"', "main 分派接线"),
                          ("returncode == 2", "退出码2=环境SKIP不阻断")]:
        (ok if needle in acc else ng)(f"acceptance {label}")
    gate = (ROOT / "gate.py").read_text(encoding="utf-8")
    (ok if 'os.environ["ACCEPT_SKIP_UIVR"] = "1"' in gate else ng)("gate Tier U 实跑后置去重标记")
    # 行为：置 ACCEPT_SKIP_UIVR=1 时 test_uivr 必须秒回 SKIP(不起浏览器)
    try:
        import os as _os, subprocess as _sp, time as _t
        tree = ast.parse(acc)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "test_uivr")
        ns = {"os": _os, "subprocess": _sp, "time": _t,
              "HERE": str(ROOT), "HUB": "http://127.0.0.1:1", "PY": sys.executable}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "acc.test_uivr", "exec"), ns)
        _os.environ["ACCEPT_SKIP_UIVR"] = "1"
        try:
            r = ns["test_uivr"](timeout=5)
        finally:
            _os.environ.pop("ACCEPT_SKIP_UIVR", None)
        (ok if (r[0] is True and str(r[1]).startswith("SKIP") and r[2] == 0.0)
         else ng)("去重路径秒回 SKIP(未起浏览器)")
    except Exception as e:
        ng(f"uivr 去重行为验证异常: {e}")


def test_secrets_selfheal():
    """06y: secrets.bat 密钥自愈——hub 裸启(未经 env_config 链)时云端密钥缺失 → deepseek 全 401
    静默回落本地模型(2026-07-06 19:37 实证)。AST 抽 _secrets_selfheal 真跑：只补缺、不覆盖、值不落账。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    try:
        import tempfile as _tf
        import types as _types
        import re as _re
        tree = ast.parse(hub)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_secrets_selfheal")
        with _tf.TemporaryDirectory() as td:
            (Path(td) / "secrets.bat").write_text(
                "@echo off\r\nrem comment set NOT_A_VAR=x\r\n"
                "set CONV_DEEPSEEK_API_KEY=sk-test123\r\n"
                'set "QUOTED_KEY=qv"\r\n'
                "set EMPTY_KEY=\r\n", encoding="utf-8")
            fake_os = _types.SimpleNamespace(environ={"CONV_DEEPSEEK_API_KEY": "already-set"})
            ns = {"Path": Path, "_BASE": td, "os": fake_os, "re": _re}
            exec(compile(ast.Module(body=[fn], type_ignores=[]), "hub._secrets_selfheal", "exec"), ns)
            healed = ns["_secrets_selfheal"]()
            cases = {
                "已有值不覆盖": fake_os.environ["CONV_DEEPSEEK_API_KEY"] == "already-set",
                "缺失被补齐(含引号形)": fake_os.environ.get("QUOTED_KEY") == "qv",
                "空值行跳过": "EMPTY_KEY" not in fake_os.environ,
                "healed只记补上的": healed == ["QUOTED_KEY"],
            }
            # 无 secrets.bat → 空表不炸
            ns2 = {"Path": Path, "_BASE": str(Path(td) / "nope"), "os": fake_os, "re": _re}
            exec(compile(ast.Module(body=[fn], type_ignores=[]), "hub._secrets_selfheal", "exec"), ns2)
            cases["无secrets.bat→[]"] = ns2["_secrets_selfheal"]() == []
        for label, passed in cases.items():
            (ok if passed else ng)(f"secrets自愈 {label}")
    except StopIteration:
        ng("avatar_hub 缺 _secrets_selfheal")
    except Exception as e:
        ng(f"secrets自愈行为验证异常: {e}")
    (ok if "_SECRETS_HEALED = _secrets_selfheal()" in hub else ng)("启动时自愈接线")


def test_sing_p0():
    """Sing-P0(2026-07-06 唱歌页诚实化): 背景=页面卖「GPT-SoVITS v4 唱歌/生成旋律」，
    实际引擎运行时已清空、每次都静默降级 CosyVoice 念白且绿✅伪装成功；产物比 speak
    低权(无水印/无历史/无并发闸)。本闸门守住: ①话术诚实(旧虚假承诺文案必须绝迹)
    ②engine_used 结构化(降级可见可埋点) ③产物三件套与 speak 同权 ④前端防线(取消/上限/估时)。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")

    # ── 后端：/avatar/sing 契约 ──
    for needle, label in [("engine_used", "SingResponse.engine_used 结构化引擎标识"),
                          ("_SING_TEXT_LIMIT", "歌词长度服务端防线"),
                          ("_SING_EMOTIONS", "演绎风格白名单"),
                          ("history_id", "历史 id 回传")]:
        (ok if needle in hub else ng)(f"sing 后端 {label}")
    i_fn = hub.find("async def avatar_sing(")
    seg = hub[i_fn:i_fn + 6000] if i_fn > 0 else ""
    i_acq = seg.find("_SPEAK_SEM.acquire")
    i_rel = seg.find("_SPEAK_SEM.release")
    i_hist = seg.find("_write_history")
    i_wm = seg.find("_provenance.attach_credentials")
    if 0 < i_acq < i_rel < i_hist < i_wm:
        ok("sing 结构(并发闸→释放→历史→水印 顺序正确，产物三件套与 speak 同权)")
    else:
        ng(f"sing 结构异常 acq={i_acq} rel={i_rel} hist={i_hist} wm={i_wm}")
    (ok if '"sing")' in seg or "'sing')" in seg else ng)("sing 历史入库以 emotion='sing' 标记")

    # ── 前端 UI：话术诚实化（旧虚假承诺必须绝迹）──
    for gone, label in [("GPT-SoVITS v4)", "标题引擎黑话"),
                        ("将降级为 EmotionTTS 柔和模式", "端口号恐吓横幅"),
                        ("AI 逐句生成旋律与咬字", "『生成旋律』虚假承诺")]:
        (ok if gone not in ui else ng)(f"sing UI 旧文案已移除: {label}")
    for needle, label in [("深情念白模式", "诚实模式徽章"),
                          ("谁来唱", "音色来源区(回答谁在唱)"),
                          ("完整唱歌引擎", "引擎徽章文案"),
                          ("开始演绎", "主按钮")]:
        (ok if needle in ui else ng)(f"sing UI {label}")

    # ── 前端 JS：防线与结构化 ──
    for needle, label in [("cancelSing(", "取消入口"),
                          ("AbortController", "可中断请求"),
                          ("singHumanErr(", "报错人话层"),
                          ("d.engine_used", "读结构化引擎标识"),
                          ("singEtaSec(", "按歌词长度估时"),
                          ("10*1024*1024", "参考音频 10MB 防线"),
                          ("singVoiceReady(", "音色就绪判定(不拦生成只提示)"),
                          ("静夜思", "公有领域示例扩充")]:
        (ok if needle in js else ng)(f"sing 前端 {label}")


def test_song_cover_p1():
    """Song-P1(2026-07-06 AI 翻唱落地): 背景=「真唱」此前为空承诺——7853 GPT-SoVITS 运行时
    2026-06 已清空，唱歌页只有念白。本闸门守住翻唱管线四件事：
    ①服务能力旗标诚实(权重缺失→capabilities.cover=False，绝不假在线)
    ②Hub 编排产物三件套与 speak 同权(水印+历史+贴合度)且不占 _SPEAK_SEM(分钟级任务会饿死对话)
    ③前端能力驱动(引擎未就绪→部署指引卡而非报错) ④部署脚本/启动链/注册表同步换轨。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")

    # ── 服务：song_studio_server.py 契约 ──
    srv_p = ROOT / "song_studio_server.py"
    if not srv_p.exists():
        ng("song_studio_server.py 存在"); return
    srv = srv_p.read_text(encoding="utf-8")
    for needle, label in [('"/v1/cover"', "翻唱任务提交端点"),
                          ("sing_from_lyrics", "SVS 能力如实上报 False"),
                          ("def _cap(", "能力旗标按权重齐备计算"),
                          ("CancelledError", "协作取消"),
                          ("bs_roformer", "人声分离(BS-RoFormer)"),
                          ("preprocess_voice_conversion", "自动升降调"),
                          ("SONG_KEEP_LOADED", "任务后卸载显存(默认与三件套共存)")]:
        (ok if needle in srv else ng)(f"song 服务 {label}")

    # ── Hub：编排段(产物三件套 + 并发闸豁免) ──
    i0 = hub.find("Song-P1: AI 翻唱")
    i1 = hub.find('@app.post("/avatar/speak/batch")')
    seg = hub[i0:i1] if 0 < i0 < i1 else ""
    if not seg:
        ng("hub Song-P1 编排段定位"); return
    ok("hub Song-P1 编排段定位")
    for needle, label in [('"/api/song/cover"', "上传编排端点"),
                          ("yingmusic_svc", "结构化引擎标识"),
                          ("attach_credentials", "水印凭证"),
                          ("_write_history", "历史入库"),
                          ("[翻唱]", "历史文本前缀(与念白区分)"),
                          ("clone_scorer", "人声贴合度评分"),
                          ("stem\": \"vocals", "干声用于评分(混音会被伴奏污染)"),
                          ("audio.wav", "成品直链(audio 元素/下载可直接用)"),
                          ("调门", "历史文本带调门(同曲不同 key 不被24h去重合并)")]:
        (ok if needle in seg else ng)(f"hub song {label}")
    (ok if "_SPEAK_SEM.acquire" not in seg else ng)(
        "hub song 不占 _SPEAK_SEM(分钟级任务不得饿死对话链)")
    # 真跑回归修复①：/api/history/{hid}/audio.wav 原始字节端点（老 /audio 是 JSON+b64，
    # <audio src> 播不了——首跑 e2e 实际踩坑）
    (ok if '/audio.wav"' in hub and "audio/wav" in hub else ng)(
        "hub 历史音频原始字节端点(audio.wav)")
    # 真跑回归修复②：录音棚常导出 IEEE float WAV(fmt=3)，标准库 wave 拒收 →
    # clone_scorer 需手动 RIFF 解析兜底，否则上传参考声评分必挂（首跑 e2e 实际踩坑）
    scorer = (ROOT / "clone_scorer.py").read_text(encoding="utf-8")
    (ok if "_decode_wav_manual" in scorer and "0xFFFE" in scorer else ng)(
        "clone_scorer 兼容 float/extensible WAV(录音棚导出格式)")
    # 真跑回归修复③：run_svc 必须整体 no_grad——rmvpe 产出 inference tensor，
    # 缺这层在 length_regulator 触发 autograd 报错（首跑 spike 实际踩坑）
    (ok if "def run_svc" in srv and "with torch.no_grad():" in srv else ng)(
        "song 服务 SVC 推理整体 no_grad(inference tensor 兼容)")

    # ── 前端：能力驱动 + 人话 ──
    for needle, label in [("AI 翻唱", "模式入口"),
                          ("开始翻唱", "主按钮"),
                          ("翻唱引擎未就绪", "未部署诚实徽章"),
                          ("setup_song_studio", "部署指引(可复制命令)"),
                          ("只听人声", "干声试听(判断像不像)"),
                          ("我传的是清唱干声", "干声直转选项")]:
        (ok if needle in ui else ng)(f"song UI {label}")
    for needle, label in [("songHealthCheck(", "能力探测"),
                          ("doCover(", "提交入口"),
                          ("_songPoll(", "任务轮询"),
                          ("cancelCover(", "取消入口"),
                          ("songHumanErr(", "报错人话层"),
                          ("/api/song/cover", "编排端点调用"),
                          ("dry_vocal", "干声参数")]:
        (ok if needle in js else ng)(f"song 前端 {label}")

    # ── 部署换轨：启动链/服务清单/注册表/部署脚本 ──
    appcfg = (ROOT / "app_config.py").read_text(encoding="utf-8")
    (ok if "song_studio_server.py" in appcfg and '"ymsvc"' in appcfg else ng)(
        "app_config singing → song_studio_server(ymsvc)")
    bat = (ROOT / "start_all_services.bat").read_text(encoding="utf-8", errors="replace")
    (ok if "song_studio_server.py" in bat else ng)("启动链接入 song_studio_server")
    reg = (ROOT / "engine_registry.py").read_text(encoding="utf-8")
    (ok if "yingmusic_svc" in reg else ng)("引擎注册表含 yingmusic_svc")
    setup = ROOT / "tools" / "setup_song_studio.py"
    (ok if setup.exists() and "YingMusic-SVC-full.pt" in setup.read_text(encoding="utf-8")
     else ng)("一键部署脚本(权重清单齐)")


def test_song_station_p2():
    """Song-P2(2026-07-07 唱歌×直播): 本闸门守住四块新地基：
    ①O1 预分离缓存——同一首歌换角色/换调门重唱免二次分离(内容哈希+LRU 逐出)，
      缓存键必须带 overlap 档（精细档 stems 不能被标准档缓存污染）
    ②O2/O5 引擎档位与排队人话——sep_overlap TTA 档随 quality=fine 下发；排队位次回显
    ③F1 点歌台——弹幕「点歌 歌名」→ 曲库模糊匹配 → 自动备歌(复用 _song_finalize 同权收尾)
      → vcam 纯音频上麦(待机脸不动)；防刷(冷却+队满+去重)；状态可持久化恢复
    ④15s MV——副歌能量选段 + 现有口型管线出片，文件名白名单防路径穿越。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    srv = (ROOT / "song_studio_server.py").read_text(encoding="utf-8")
    vcam = (ROOT / "vcam_server.py").read_text(encoding="utf-8")

    # ── ① 引擎：预分离缓存 ──
    for needle, label in [("def _sep_cache_get", "缓存读(命中刷 mtime)"),
                          ("def _sep_cache_put", "缓存写(stems 落盘)"),
                          ("SONG_SEP_CACHE_GB", "容量上限可配"),
                          ("st_mtime", "LRU 按 mtime 逐出"),
                          ('f"_ov{', "缓存键带 overlap 档(精细/标准不互污)"),
                          ("sep_cache_hit", "命中标记回传(结果可核验)")]:
        (ok if needle in srv else ng)(f"song O1 预分离缓存 {label}")

    # ── ② 引擎：TTA 档位 + 排队人话 ──
    (ok if "sep_overlap" in srv and "num_overlap" in srv else ng)(
        "song O2 分离 TTA 档(sep_overlap→num_overlap 覆盖)")
    (ok if "def _queue_pos" in srv and "queue_ahead" in srv else ng)(
        "song O5 排队位次回显(前面还有 n 首)")
    (ok if '"sep_overlap": 4 if quality == "fine"' in hub else ng)(
        "hub 精细档同时提升分离质量(TTA=4)")
    (ok if "_songNotify" in js and "Notification" in js else ng)(
        "前端 O5 完成通知(切页/最小化也能收到)")

    # ── ③ Hub：点歌台 ──
    i0 = hub.find("Song-P2/F1: 直播间点歌台")
    i1 = hub.find('@app.post("/avatar/speak/batch")')
    seg = hub[i0:i1] if 0 < i0 < i1 else ""
    if not seg:
        ng("hub Song-P2 点歌台段定位"); return
    ok("hub Song-P2 点歌台段定位")
    for needle, label in [('"/api/song/station"', "全量快照端点"),
                          ("station/request", "运营点歌入队"),
                          ("station/chat", "弹幕点歌入口(外部桥可 POST)"),
                          ("_station_parse_chat", "「点歌 歌名」解析"),
                          ("_station_match_song", "曲库模糊匹配(不匹配不入队)"),
                          ("difflib", "近似匹配兜底"),
                          ("_STATION_CHAT_COOLDOWN_S", "同人点歌冷却(防刷)"),
                          ("_STATION_MAX_Q", "队列容量上限"),
                          ("已在队列", "同曲去重"),
                          ("_song_finalize", "备歌收尾复用(水印+历史+贴合度同权)"),
                          ("play_audio", "vcam 纯音频上麦(待机脸不动)"),
                          ("stop_audio", "切歌硬停"),
                          ("_station_play_watch", "播完守望(auto_play 接播)"),
                          ("_station_save", "状态快照(重启可恢复)"),
                          ("station/{rid}/top", "插队置顶"),
                          ("station/{rid}/retry", "失败重试")]:
        (ok if needle in seg else ng)(f"hub 点歌台 {label}")
    (ok if "_SPEAK_SEM.acquire" not in seg else ng)(
        "hub 点歌台不占 _SPEAK_SEM(备歌是分钟级任务)")
    (ok if "_station_parse_chat(req.text)" in hub else ng)(
        "互动墙提问「点歌」自动改道点歌队列")

    # ── ③b vcam：纯音频停止 + 整曲清理时序 ──
    (ok if '"/stop_audio"' in vcam and "SND_PURGE" in vcam else ng)(
        "vcam /stop_audio(桌面声硬停)")
    (ok if "def flush" in vcam else ng)("vcam WebRTC 音轨 flush(切歌即刻静音)")
    (ok if "dur + 10.0" in vcam else ng)(
        "vcam play_audio 整曲时长感知清理(3 分钟歌不再 30s 被删)")

    # ── ④ 15s MV ──
    for needle, label in [("def _pick_chorus_start", "副歌能量选段"),
                          ("def _cut_wav_segment", "帧级截段"),
                          ('"/api/song/mv"', "MV 生成端点"),
                          ("lipsync/generate", "复用现有口型管线"),
                          (r"mv_\d+_[0-9a-f]{8}\.mp4", "成片文件名白名单(防穿越)"),
                          ("48 * 3600", "48h 清老片")]:
        (ok if needle in hub else ng)(f"hub MV {label}")

    # ── 前端：点歌台 + MV ──
    for needle, label in [("直播点歌台", "模式入口"),
                          ("点歌队列", "队列面板"),
                          ("开启点歌台", "总开关"),
                          ("弹幕点歌", "弹幕开关"),
                          ("自动备歌", "自动备歌开关"),
                          ("自动连播", "连播开关"),
                          ("上麦", "上麦按钮"),
                          ("15秒高光MV", "MV 按钮")]:
        (ok if needle in ui else ng)(f"station UI {label}")
    for needle, label in [("stationRefresh(", "快照拉取"),
                          ("stationConfig(", "开关下发"),
                          ("stationRequest(", "曲库点歌"),
                          ("stationAct(", "队列操作(置顶/上麦/重试)"),
                          ("stationStop(", "停播"),
                          ("song_station", "WS 事件驱动刷新"),
                          ("doSongMv(", "MV 生成入口")]:
        (ok if needle in js else ng)(f"station 前端 {label}")


def test_song_p3():
    """Song-P3(2026-07-07 直播让路×精细分离×点歌台增值): 守四块：
    ①O6 直播让路——唱歌重活(分离/SVC/MV)与直播抢同一张 5090；Hub 出单一真相
      /api/song/yield(换脸直播/同传/对话活跃/显存高压/手动挂起)，引擎每阶段前询问，
      忙则挂起(fail-open: Hub 不可达不影响独立唱歌)；MV 直播中 409+可强制
    ②O2 完整版——Kim Mel-Band RoFormer 精细档分离(缺权重自动回退 BS)；
      缓存键带模型标记(mel/bs stems 不互污)；配置文件必须 ASCII(GBK 平台读取会炸)
    ③点歌台增值——播报点歌人(复用 /avatar/speak 全链)、演唱字幕(vcam /subtitle)、
      礼物插队(外部桥 POST /gift)
    ④F3 前置——ACE-Step 权重拉取脚本(复用断点续传管线)。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    srv = (ROOT / "song_studio_server.py").read_text(encoding="utf-8")

    # ── ① O6 直播让路：Hub 单一真相 ──
    for needle, label in [('"/api/song/yield"', "让路状态端点"),
                          ("def _song_yield_state", "让路判定(单一真相)"),
                          ("_live_session_busy()", "换脸直播/同传判据复用"),
                          ("_conv_last_active", "对话活跃窗口判据"),
                          ("_vram_pressure()", "显存高压判据"),
                          ('"/api/song/yield/hold"', "手动挂起(开播清场+e2e 可控)"),
                          ("_SONG_YIELD_CACHE", "3s TTL(interpreter 探测别拖垮轮询)"),
                          ("SONG_LIVE_YIELD", "总开关可关")]:
        (ok if needle in hub else ng)(f"O6 hub {label}")
    # MV 直播中默认拒 + 可强制
    (ok if "ys.get(\"yield\") and not req.force" in hub else ng)(
        "O6 MV 直播中 409(人话+可强制)")
    (ok if "force: bool = False" in hub else ng)("O6 MV force 字段")

    # ── ① O6 引擎侧：挂起等待 ──
    for needle, label in [("def _yield_probe", "让路探测(fail-open)"),
                          ("def _yield_wait", "挂起等待(可取消)"),
                          ("SONG_HUB_URL", "Hub 地址可配"),
                          ('timings["yield_ms"]', "让路耗时可观测"),
                          ("_yield_wait(tid, t)", "任务开工前让路")]:
        (ok if needle in srv else ng)(f"O6 引擎 {label}")
    (ok if srv.count("_yield_wait(tid, t)") >= 2 else ng)(
        "O6 引擎 SVC 前二次让路(分离期间开播也让)")
    (ok if 'except Exception:\n        st = {"yield": False' in srv else ng)(
        "O6 引擎 fail-open(Hub 挂了不影响独立唱歌)")

    # ── ② O2 完整版：Mel-Band 精细档 ──
    for needle, label in [("_SEP_KINDS", "双模型登记表"),
                          ('"sep_mel"', "mel 权重路径"),
                          ("separate_mel", "能力如实上报"),
                          ("mel_band_roformer", "模型类型接入"),
                          ("回退 BS", "缺权重自动回退"),
                          ('"_mel"', "缓存键带模型标记(stems 不互污)"),
                          ("sep_model_used", "实际用了谁如实记录")]:
        (ok if needle in srv else ng)(f"O2 引擎 {label}")
    (ok if '"sep_model": "mel" if quality == "fine"' in hub else ng)(
        "O2 hub 精细档下发 mel 分离")
    setup = ROOT / "tools" / "setup_song_studio.py"
    (ok if "KimberleyJSN/melbandroformer" in setup.read_text(encoding="utf-8")
     else ng)("O2 权重清单含 Mel-Band")
    cfg = (ROOT / "YingMusic-SVC" / "accom_separation" / "ckpt"
           / "mel_band_roformer" / "config_vocals_mel_band_roformer_kj.yaml")
    if cfg.exists():
        raw = cfg.read_bytes()
        (ok if all(b < 128 for b in raw) else ng)(
            "O2 mel 配置 ASCII-only(GBK 平台默认编码读取不炸)")
        (ok if b"target_instrument: vocals" in raw else ng)(
            "O2 mel 配置 target_instrument(demix 单 stem 语义)")
    else:
        ng("O2 mel 配置文件存在")

    # ── ③ 点歌台增值位 ──
    for needle, label in [("def _station_announce", "点歌播报(角色开口谢点歌人)"),
                          ("/avatar/speak", "播报复用 speak 全链(TTS+口型+水印)"),
                          ('"incognito": True', "播报不写历史"),
                          ("def _station_subtitle", "演唱字幕(vcam 叠加)"),
                          ("停歌即清演唱字幕", "切歌清字幕"),
                          ('"/api/song/station/gift"', "礼物插队入口"),
                          ("def _station_top_queued", "置顶公共路径(手动+礼物共用)"),
                          ('"announce"', "播报开关入配置")]:
        (ok if needle in hub else ng)(f"P3 点歌台 {label}")
    (ok if "播报点歌人" in ui else ng)("P3 UI 播报开关")
    (ok if "直播让路中" in ui else ng)("P3 UI 让路提示(备歌为何暂停说人话)")
    (ok if "station/gift" in ui else ng)("P3 UI 礼物桥提示")
    (ok if "announce:false" in js else ng)("P3 前端 announce 默认态")
    (ok if "r.status===409 && !force" in js else ng)(
        "P3 前端 MV 409 确认后强制重发")

    # ── ④ F3 前置：ACE-Step 权重管线 ──
    ace = ROOT / "tools" / "setup_ace_step.py"
    (ok if ace.exists() and "ACE-Step/ACE-Step-v1-3.5B" in ace.read_text(encoding="utf-8")
     else ng)("F3 ACE-Step 权重拉取脚本(断点续传管线复用)")


def test_song_p4():
    """Song-P4(2026-07-07 原创歌服务化): 守四块：
    ①ace_studio 引擎——ACE-Step 3.5B 整曲文本成曲(端口 7859/ymsvc)：懒加载+任务后
      卸载(bf16 峰值 ~8.4GB，与直播三件套不共存)、O6 让路协议对齐 song_studio
      (加载前就让：加载本身 8GB)、权重不齐诚实下线；
    ②Hub 编排——/api/song/create 两阶段状态机(gen→可选 svc_swap 换声)：换声段
      完全复用 song_studio+_song_finalize(水印/历史/贴合度零新代码，历史前缀可定制)；
      歌词 LLM 辅写(没配 LLM 人话 503，不硬依赖)；
    ③UI 第四模式「原创歌」——风格预设/AI 写词/时长/用角色声唱、能力门控不假在线；
    ④MV 加长——副歌窗 30→60s(整曲 MV 属 P5 异步化范围)。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    eng = (ROOT / "ace_studio_server.py").read_text(encoding="utf-8")

    # ── ① ace_studio 引擎 ──
    for needle, label in [("ACE_KEEP_LOADED", "任务后默认卸载可关"),
                          ("def _yield_probe", "O6 让路探测(与 song_studio 同协议)"),
                          ("def _yield_wait", "O6 挂起等待(可取消)"),
                          ('timings["yield_ms"] = _yield_wait(tid, t)', "加载前就让路(加载本身 8GB)"),
                          ("def _weights_ok", "权重自检"),
                          ("原创歌引擎权重未就绪", "权重不齐人话拒单"),
                          ("HF_HUB_OFFLINE", "运行期零 HF 网络"),
                          ('"/v1/create"', "提交端点"),
                          ("def _unload", "显存归还"),
                          ("service_auth", "服务面鉴权接入")]:
        (ok if needle in eng else ng)(f"P4 引擎 {label}")
    (ok if 'except Exception:\n        st = {"yield": False' in eng else ng)(
        "P4 引擎 fail-open(Hub 挂了不影响独立创作)")
    # 显存清场：不直播但闲置引擎占卡 → 请 Hub free_unused（不动核心），不够人话拒
    for needle, label in [("def _ensure_vram", "加载前显存预检"),
                          ("/api/gpu/free_unused", "请 Hub 腾闲置引擎(不打断直播)"),
                          ("ACE_MIN_FREE_MB", "门槛可调(offload 档更低)"),
                          ("_ensure_vram(tid, t)", "worker 接入预检"),
                          ("绝不裸 OOM", "OOM 前置为人话错误")]:
        (ok if needle in eng else ng)(f"P4 引擎显存 {label}")
    # vram 源让路防死锁：显存高压且非直播 → 宽限后放行给 _ensure_vram 主动腾
    # （2026-07-07 真跑实锤：闲置 latentsync 等占满 31.5G，任务在 vram 让路里挂 20 分钟）
    (ok if "VRAM_YIELD_GRACE_S" in eng and '"source"' in eng else ng)(
        "P4 引擎 vram 让路带宽限(防闲置占卡死等)")
    sng = (ROOT / "song_studio_server.py").read_text(encoding="utf-8")
    for needle, label in [("VRAM_YIELD_GRACE_S", "vram 让路宽限"),
                          ("def _ensure_vram", "加载前显存预检"),
                          ("_ensure_vram(tid, t)", "worker 接入预检"),
                          ("SONG_MIN_FREE_MB", "门槛可调")]:
        (ok if needle in sng else ng)(f"P4 翻唱引擎同享 {label}")
    # app_config 服务登记（supervisor 自愈/GPU 总开关/doctor 全套自动获得）
    ac = (ROOT / "app_config.py").read_text(encoding="utf-8")
    (ok if '"ace_studio"' in ac and "ace_studio_server.py" in ac else ng)(
        "P4 app_config SERVICES 登记(纳管自愈+显存编排)")

    # ── ② Hub 编排 ──
    for needle, label in [('"/api/song/create"', "原创歌提交"),
                          ("class SongCreateBody", "请求契约"),
                          ("_ACE_TASKS", "编排状态机登记表"),
                          ("def _ace_start_swap", "gen→换声交接"),
                          ("def _ace_finalize_direct", "纯生成收尾(水印+历史)"),
                          # P5 起前缀改为透传父任务（魔改换声=[魔改]），默认仍 [原创]
                          ('"hist_prefix": t.get("hist_prefix", "[原创]")', "换声段历史前缀定制"),
                          ('"engine_label": "ace_step+yingmusic_svc"', "换声段引擎如实标注"),
                          ('"/api/song/lyrics_assist"', "歌词辅写入口"),
                          ("还没配置对话大模型", "没 LLM 人话降级(不硬依赖)"),
                          ("还没有克隆声音", "换声无克隆音提交前人话拒")]:
        (ok if needle in hub else ng)(f"P4 hub {label}")
    # _song_finalize 复用改造：前缀/引擎标记由登记表定制（翻唱默认值不变）
    (ok if 'hub_t.get("hist_prefix", "[翻唱]")' in hub else ng)(
        "P4 _song_finalize 前缀可定制(翻唱默认不变)")
    (ok if 'hub_t.get("engine_label", "yingmusic_svc")' in hub else ng)(
        "P4 _song_finalize 引擎标记可定制")
    # /api/song/health 同时上报 create 能力（UI 门控数据源）
    (ok if '"create"' in hub and "_ace_base()" in hub else ng)(
        "P4 health 上报 create 能力(不假在线)")

    # ── ③ UI 第四模式 ──
    for needle, label in [("setSingMode('create')", "模式入口"),
                          ("songCreateReady()", "能力门控"),
                          ("createStylePresets", "风格预设"),
                          ("doLyricsAssist()", "AI 写词按钮"),
                          ("用角色声唱", "换声开关"),
                          ("setup_ace_step.py", "未就绪部署指引")]:
        (ok if (needle in ui or needle in js) else ng)(f"P4 UI {label}")
    (ok if "'cover','lyrics','station','create'" in js else ng)("P4 UI 模式白名单")
    (ok if "stage:d.stage" in js.replace(" ", "") or "createStage=d.stage" in js
     else ng)("P4 UI 两阶段进度(gen/swap)")

    # ── ④ MV 加长 ──
    (ok if "min(60.0, req.seconds)" in hub else ng)("P4 MV 副歌窗上限 60s")


def test_song_p5():
    """Song-P5(2026-07-07 任务持久化+整曲MV+风格魔改): 守六块：
    ①任务持久化——_SONG_TASKS/_ACE_TASKS/_MV_TASKS 写穿 SQLite(song_tasks.db)，
      ref_b64 大字段不落库(收尾按角色名回取)，lifespan 回载续办；
    ②后台对账——_bg_song_reconcile 不靠前端轮询也推进/收尾(浏览器关了歌照出)；
      收尾幂等靠 _finalize_lock(前端轮询与对账并发到达只收一次)；引擎重启丢任务
      如实标记 _gone 不装死；
    ③队列看板——GET /api/song/tasks(零引擎请求,吃对账快照) + UI「我的任务」；
    ④整曲 MV 异步——/api/song/mv_task 任务化(排队+让路+可取消+重启重排)，
      渲染核 _mv_render 与同步端点共用；同步端点保持 60s 上限不破坏 P4 契约；
    ⑤F5 风格魔改——引擎 audio2audio(ref_b64/ref_strength)，Hub remix_of 取历史
      音频截副歌窗做参考，历史前缀 [魔改]，换声可叠加；
    ⑥点歌台增强——礼物插队价值门槛(低于门槛只谢不插队) + 今日点歌榜。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    eng = (ROOT / "ace_studio_server.py").read_text(encoding="utf-8")

    # ── ① 任务持久化 ──
    for needle, label in [("CREATE TABLE IF NOT EXISTS song_tasks", "song_tasks 建表"),
                          ("def _task_persist", "写穿保存"),
                          ("def _song_store_load", "启动回载"),
                          ("async def _song_store_hydrate", "回灌登记表"),
                          ("await _song_store_hydrate()", "lifespan 接入回载"),
                          ('_SONG_STORE_SKIP = ("ref_b64",)', "大字段不落库"),
                          ("_SONG_TASK_TTL_S", "过期清理"),
                          ('_task_persist("cover", tid, _SONG_TASKS[tid])', "翻唱提交落库"),
                          ('_task_persist("create", tid, _ACE_TASKS[tid])', "原创提交落库"),
                          ('_task_persist("mv", tid, _MV_TASKS[tid])', "MV 提交落库")]:
        (ok if needle in hub else ng)(f"P5 持久化 {label}")
    # 纯生成终态也必须写穿（e2e run2 抓到的缺口：done 不落库 → 重启回载重跑）
    (ok if '_task_persist("create", tid, t)          # P5: 终态落库' in hub else ng)(
        "P5 持久化 纯生成终态写穿 done=1")
    # 历史搜索：用户输入按字面搜（[魔改]/[原创] 的方括号不再当 MATCH 语法炸掉）
    (ok if "def _fts_safe_query" in hub
     and "params.append(_fts_safe_query(search))" in hub
     and "count_params = [_fts_safe_query(search)]" in hub else ng)(
        "P5 历史搜索 FTS5 字面化(方括号前缀可搜)")
    (ok if "def _task_refetch_ref" in hub
     and "await _task_refetch_ref(t)" in hub else ng)(
        "P5 持久化 ref_b64 按角色名回取(重启后换声/评分不断链)")

    # ── ② 后台对账 + 收尾幂等 ──
    for needle, label in [("async def _bg_song_reconcile", "对账循环"),
                          ("async def _song_reconcile_tick", "对账单步"),
                          ("asyncio.create_task(_bg_song_reconcile())", "lifespan 挂载"),
                          ("def _finalize_lock", "收尾并发闸"),
                          ('async with _finalize_lock(tid)', "收尾锁接入"),
                          ('t["_gone"] = True', "引擎丢任务如实标记"),
                          ("任务在引擎侧已丢失", "丢任务人话说明")]:
        (ok if needle in hub else ng)(f"P5 对账 {label}")
    # 收尾锁必须双检缓存（锁内二次 final 检查，后到者吃缓存不重复入历史）
    (ok if hub.count('if hub_t.get("final"):\n            return hub_t["final"]') >= 1
     else ng)("P5 对账 收尾锁内双检(不重复入历史)")

    # ── ③ 队列看板 ──
    for needle, label in [('"/api/song/tasks"', "看板端点"),
                          ('"_snap"', "对账快照(看板零引擎请求)")]:
        (ok if needle in hub else ng)(f"P5 看板 {label}")
    for needle, label in [("songBoardRefresh", "看板拉取"),
                          ("songBoardStartPoll", "看板轮询"),
                          ("songBoardCancel", "看板取消"),
                          ("我的任务", "看板入口")]:
        (ok if (needle in js or needle in ui) else ng)(f"P5 看板 UI {label}")

    # ── ④ 整曲 MV 异步 ──
    for needle, label in [('"/api/song/mv_task"', "异步提交端点"),
                          ("_MV_TASKS", "任务登记表"),
                          ("async def _bg_mv_worker", "串行渲染 worker"),
                          ("asyncio.create_task(_bg_mv_worker())", "lifespan 挂载"),
                          ("async def _mv_render", "渲染核共用"),
                          ("_MV_VRAM_GRACE_S", "vram 源宽限(与引擎同语义)"),
                          ("已重新排队", "重启重排如实告知")]:
        (ok if needle in hub else ng)(f"P5 整曲MV {label}")
    (ok if "min(60.0, req.seconds)" in hub else ng)("P5 整曲MV 同步端点 60s 上限不破坏 P4 契约")
    (ok if "want_s <= 0" in hub else ng)("P5 整曲MV seconds=0 整曲语义")
    for needle, label in [("doSongMvFull", "整曲MV 按钮"),
                          ("mvTaskRunning", "任务态门控"),
                          ("整曲MV", "入口文案")]:
        (ok if (needle in js or needle in ui) else ng)(f"P5 整曲MV UI {label}")

    # ── ⑤ F5 风格魔改 ──
    for needle, label in [("ref_b64", "参考音频入参"),
                          ("ref_strength", "贴原曲程度"),
                          ("audio2audio_enable", "a2a 管线开关"),
                          ("ref_audio_input", "参考落盘传参"),
                          ('"remix": _weights_ok()', "能力如实上报")]:
        (ok if needle in eng else ng)(f"P5 魔改引擎 {label}")
    dcae = (ROOT / "ACE-Step" / "acestep" / "music_dcae" /
            "music_dcae_pipeline.py").read_text(encoding="utf-8")
    (ok if "soundfile" in dcae and "torchaudio.load(audio_path)" not in dcae else ng)(
        "P5 魔改引擎 load_audio 去 torchcodec 依赖(soundfile 读)")
    for needle, label in [("remix_of", "历史参考入参"),
                          ("remix_strength", "强度透传"),
                          ('"[魔改]"', "历史前缀区分"),
                          ("_pick_chorus_start(wav, want_s)", "参考截副歌窗(复用 MV 探测)"),
                          ('t.get("hist_prefix", "[原创]")', "换声段前缀透传(魔改换声=[魔改])")]:
        (ok if needle in hub else ng)(f"P5 魔改 hub {label}")
    for needle, label in [("startRemix", "魔改入口"),
                          ("createRemixOf", "魔改状态"),
                          ("createRemixStrength", "强度滑杆"),
                          ("风格魔改", "按钮文案")]:
        (ok if (needle in js or needle in ui) else ng)(f"P5 魔改 UI {label}")

    # ── ⑥ 点歌台增强 ──
    for needle, label in [('"gift_min_value"', "礼物门槛配置"),
                          ("can_top", "低于门槛只谢不插队"),
                          ('"/api/song/station/leaderboard"', "点歌榜端点"),
                          ("gift_value", "礼物价值累计")]:
        (ok if needle in hub else ng)(f"P5 点歌台 {label}")
    for needle, label in [("gift_min_value", "门槛输入"),
                          ("stationLoadBoard", "点歌榜拉取"),
                          ("今日点歌榜", "点歌榜入口")]:
        (ok if (needle in js or needle in ui) else ng)(f"P5 点歌台 UI {label}")


def test_song_p6():
    """Song-P6(2026-07-07 空转治理+音乐人格+专辑化+全自动点歌): 守五块：
    ①空转直播治理——僵尸直播(noface/stalled 分钟级驻留)自动下播+告警，独立于
      autoheal 开关(僵尸饿死创作队列的风险恒在)；纯决策进 _sh_plan(selftest 可演练)，
      开关随 heal_config.json 持久化，前端可切；
    ②唱腔 LoRA(音乐人格)——引擎 capabilities.loras 如实上报就绪名单(不假在线)，
      per-任务挂载(managed pipeline __call__ 幂等 load_lora)，历史文本带唱腔标
      (绕开 24h 去重合并)，setup_ace_lora 断点续传补权重；
    ③批量魔改(专辑化)——/api/song/remix_batch 一歌×N风格复用单条提交路径，
      成品名 base·label 保唯一(防历史去重互吞)，部分失败如实回报；
    ④点歌台全自动闭环——auto_play 补「冷启动第一首」：就绪且无歌在播即自动上麦
      (原实现只有播完接下一首，第一首永远要人点)；
    ⑤历史成品筛选——[翻唱]/[原创]/[魔改] 快捷胶囊(借道 P5 FTS 字面化)。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    eng = (ROOT / "ace_studio_server.py").read_text(encoding="utf-8")

    # ── ① 空转直播治理 ──
    for needle, label in [("_IDLE_LIVE_GOVERN", "治理总开关"),
                          ("_IDLE_LIVE_NOFACE_MIN", "无人上镜阈(分钟级)"),
                          ("_IDLE_LIVE_STALLED_MIN", "画面死更阈"),
                          ('"stop_idle_live"', "纯决策进 _sh_plan"),
                          ('plan["stop_idle_live"]', "守护执行停播"),
                          ("idle_live_autostop", "事件留痕+广播"),
                          ('"idle_live_govern"', "运行时开关(heal_config)"),
                          ("空转直播已自动下播", "人话告警")]:
        (ok if needle in hub else ng)(f"P6 空转治理 {label}")
    # 治理独立于 autoheal：决策不在 if autoheal_on 分支里(靠 selftest 场景卡死)
    (ok if "govern_on and \"stop_idle_live\" not in episode" in hub else ng)(
        "P6 空转治理 独立开关+回合去重")
    for needle, label in [("僵尸noface超阈·自动下播", "selftest noface 场景"),
                          ("治理独立于自愈开关", "selftest 独立性场景"),
                          ("本回合已停播·去重", "selftest 去重场景")]:
        (ok if needle in hub else ng)(f"P6 空转治理 {label}")
    (ok if "idleLiveGovOn" in js and "空转直播自动下播" in ui else ng)("P6 空转治理 UI 开关")

    # ── ② 唱腔 LoRA（音乐人格）──
    for needle, label in [("def _loras_avail", "就绪名单探测"),
                          ('"loras": _loras_avail()', "能力如实上报"),
                          ("lora_name_or_path", "管线挂载传参"),
                          ("lora not in _loras_avail()", "未装报人话错"),
                          ('"lora": _lora or None', "成品元数据留痕")]:
        (ok if needle in eng else ng)(f"P6 LoRA 引擎 {label}")
    for needle, label in [('lora:       str = ""', "创建入参"),
                          ('"lora": (req.lora or "").strip()', "透传引擎"),
                          ("def _lora_hist_tag", "历史标(防24h去重互吞)"),
                          ("（RAP腔）", "RAP 短标")]:
        (ok if needle in hub else ng)(f"P6 LoRA hub {label}")
    (ok if (ROOT / "tools" / "setup_ace_lora.py").exists() else ng)("P6 LoRA 权重部署脚本")
    for needle, label in [("songLoras", "名单读取"),
                          ("createLora", "选择状态"),
                          ("唱腔", "入口文案")]:
        (ok if (needle in js or needle in ui) else ng)(f"P6 LoRA UI {label}")

    # ── ③ 批量魔改（专辑化）──
    for needle, label in [('"/api/song/remix_batch"', "批量端点"),
                          ("class SongRemixBatchBody", "入参模型"),
                          ("一批最多 6 个风格", "批量上限人话"),
                          ("seen_names", "成品名去重(防历史互吞)"),
                          ('"errors": errors', "部分失败如实回报")]:
        (ok if needle in hub else ng)(f"P6 批量魔改 {label}")
    for needle, label in [("doRemixBatch", "批量提交"),
                          ("toggleRemixBatch", "风格勾选"),
                          ("批量魔改", "入口文案")]:
        (ok if (needle in js or needle in ui) else ng)(f"P6 批量魔改 UI {label}")

    # ── ④ 点歌台全自动闭环 ──
    (ok if "not _station.get(\"playing_id\")" in hub
     and "自动上麦(就绪即播)" in hub else ng)("P6 点歌台 冷启动第一首自动上麦")
    (ok if "auto_play" in hub and "_station_play_watch" in hub else ng)(
        "P6 点歌台 播完自动接下一首(P3 既有,回归在位)")
    # 兜底扫描：重启恢复出 ready / 后开 auto_play 两个事件盲区（run4 实锤缺口）
    (ok if "def _station_autoplay_kick" in hub
     and "自动上麦(兜底扫描)" in hub else ng)("P6 点歌台 auto_play 兜底扫描在位")
    for hook, label in [("_station_autoplay_kick()\n    return _station_snapshot()",
                         "快照拉取挂兜底扫描(重启恢复 ready 补上麦)"),
                        ("_station_autoplay_kick()      # P6",
                         "config 变更挂兜底扫描(后开 auto_play 补上麦)")]:
        (ok if hook in hub else ng)(f"P6 点歌台 {label}")
    (ok if "_station_autoplay_busy" in hub else ng)(
        "P6 点歌台 兜底扫描防抖(并发快照只放一个上麦在途)")

    # ── ⑤ 历史成品筛选 ──
    (ok if "'[翻唱]'" in ui and "'[原创]'" in ui and "'[魔改]'" in ui else ng)(
        "P6 历史筛选 歌曲成品快捷胶囊")


def test_dashboard_v2():
    """看板 v2(2026-07-06 三视角优化): ①指标持久化——turns/clone/naturalness 历史全在内存
    deque,Hub 一重启看板即失忆(满屏 0ms/暂无数据),现统一落 metrics.db 且启动回载;
    ②清空拆语义——旧 reset 连 SQLite 好评账本一起 DELETE(客户演示前清个屏就把 60 条听感
    评分删了),现拆 window/all/all_with_feedback 三档,默认软清;③轮询收敛——前端每 5s
    扇出 12 条 HTTP(且 /profiles 不带字段过滤,把全部角色缩略图 base64 一起拖回来),
    现合并为 1 条 /api/dashboard/snapshot(服务端并发聚合+TTL 缓存+单区失败不连坐);
    ④iframe 逃逸——看板嵌 /ui 的 iframe 里,页内 /ui 链接会在 iframe 里套娃打开。"""
    # —— metrics.py 静态契约 ——
    m = (ROOT / "metrics.py").read_text(encoding="utf-8")
    for needle, label in [
            ("CREATE TABLE IF NOT EXISTS turns", "turns 建表"),
            ("CREATE TABLE IF NOT EXISTS clone_scores", "clone_scores 建表"),
            ("CREATE TABLE IF NOT EXISTS naturalness", "naturalness 建表"),
            ("def _load_series_from_db", "启动回载序列"),
            ("def daily_trend", "按日聚合"),
            ("all_with_feedback", "清空三档语义"),
            ("total_turns_alltime", "全时累计轮数"),
            ("_RETENTION_DAYS", "保留期清老账")]:
        (ok if needle in m else ng)(f"metrics 持久化 {label}")
    # —— metrics.py 行为验证(隔离 DB 的子进程,不污染真库) ——
    import subprocess as _sp
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        script = (
            "import os,sys,time\n"
            f"os.environ['CONV_METRICS_DB']=os.path.join(r'{td}','t.db')\n"
            f"sys.path.insert(0,r'{ROOT}')\n"
            "import metrics\n"
            "metrics.record_turn(profile='甲', ttfa_ms=800, perceived_ttfa_ms=500)\n"
            "metrics.record_feedback(profile='甲', rating=1)\n"
            "s1=metrics.snapshot()\n"
            "assert s1['total_turns']==1 and s1['total_turns_alltime']==1, 'record 落账失败'\n"
            "metrics.reset(scope='window')\n"
            "s2=metrics.snapshot()\n"
            "assert s2['total_turns']==0, '软清后视图未归零'\n"
            "assert s2['feedback']['n']==0, '软清后评分视图未归零'\n"
            "assert metrics.feedback_db_count()==1, '软清动了好评账本!'\n"
            "import sqlite3\n"
            "c=sqlite3.connect(os.environ['CONV_METRICS_DB'])\n"
            "assert c.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==1, '软清删了 turns 历史!'\n"
            "metrics.reset(scope='all')\n"
            "assert c.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==0, '硬清未删 turns'\n"
            "assert metrics.feedback_db_count()==1, '硬清(不含评分)动了好评账本!'\n"
            "metrics.reset(scope='all_with_feedback')\n"
            "assert metrics.feedback_db_count()==0, '三档硬清未删评分'\n"
            "d=metrics.daily_trend(days=7)\n"
            "assert len(d['trend'])==7, 'daily_trend 桶数不对'\n"
            "print('BEHAV_OK')\n")
        try:
            p = _sp.run([sys.executable, "-c", script], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
            if "BEHAV_OK" in (p.stdout or ""):
                ok("metrics 行为: 落账/软清保账本/硬清三档/按日桶 全过")
            else:
                ng(f"metrics 行为验证失败: {(p.stderr or p.stdout or '').strip()[-200:]}")
        except Exception as e:
            ng(f"metrics 行为验证异常: {e}")
    # —— hub 契约 ——
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [
            ('@app.get("/api/dashboard/snapshot")', "合并快照端点"),
            ('@app.get("/api/dashboard/report")', "客户版战报端点"),
            ('@app.get("/api/metrics/daily")', "按日趋势端点"),
            ("def api_metrics_reset(scope", "reset 带 scope 参数"),
            ("_DASH_SNAP_TTL", "快照 TTL 缓存(多端共享)"),
            ("def _dash_section", "单区失败不连坐"),
            ("def _dash_profiles_lean", "角色瘦身(不再拖缩略图 base64)"),
            ('.replace("<", "\\\\u003c")', "战报数据防 </script> 注入")]:
        (ok if needle in hub else ng)(f"hub 看板v2 {label}")
    # —— dashboard.html 契约 ——
    dash = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    for needle, label in [
            ('<base target="_top"', "iframe 逃逸(base target)"),
            ("/api/dashboard/snapshot", "走合并快照(1 条轮询)"),
            ("document.hidden", "后台标签停轮询"),
            ("if(inflight) return", "轮询不叠发"),
            ("scope=window", "软清按钮走 window"),
            ("all_with_feedback", "硬清三档确认"),
            ("typed:'RESET'", "硬清需输入 RESET"),
            ("hub_demo", "演示模式联动"),
            ("window.self!==window.top", "嵌入自动识别"),
            ("renderSection(", "分区签名守卫(不闪不丢下钻)"),
            ("brand.css", "接入品牌设计系统"),
            ("感知 TTFA", "黑话入悬停(人话化)")]:
        (ok if needle in dash else ng)(f"dashboard {label}")
    for bad, label in [("setInterval(load, 5000)", "旧 12 路扇出轮询"),
                       ("alert(", "原生 alert"),
                       ("confirm(", "原生 confirm"),
                       ("font-size:8px", "8px 蚂蚁字"),
                       ("'/profiles'", "前端裸拉 /profiles(缩略图 base64)")]:
        (ok if bad not in dash else ng)(f"dashboard 已移除 {label}")
    # esc() 必须补单引号转义(旧版 onclick='...${esc(名)}...' 单引号即可穿透)
    if "&#39;" in dash:
        ok("dashboard esc() 含单引号转义")
    else:
        ng("dashboard esc() 缺单引号转义")
    # —— 战报页 ——
    rep = ROOT / "static" / "report.html"
    if rep.exists():
        r = rep.read_text(encoding="utf-8")
        for needle, label in [("/*__REPORT_DATA__*/null", "数据占位符(服务端内嵌)"),
                              ("@media print", "打印/存PDF 适配"),
                              ("bd_brand_config", "白标联动")]:
            (ok if needle in r else ng)(f"report.html {label}")
    else:
        ng("static/report.html 缺失")
    # —— ui.html 门禁串不被破坏(嵌入检测改为 window.top,不改 iframe src) ——
    ui = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    (ok if "visitedTabs.includes('dashboard') ? '/dashboard' : ''" in ui
     else ng)("ui.html 看板 iframe 懒加载串保持不变")


def test_dashboard_v3():
    """看板 v3(2026-07-07 下一阶段四件套): ①角色下钻——点角色名开单角色档案(SQLite 全史,
    刻意不吃软清窗口标记,清屏不抹履历);②SSE 推流——v2 的 5s 轮询升级为服务端变更即推
    (无变化只发心跳注释帧),断流自动回退轮询 60s 后重试;③战报长图——?fmt=png 子进程无头
    截图(浏览器不进 Hub 事件环),微信直发不用教客户 Ctrl+S;④夜间归档——每日 5 点后补发式
    落 logs/reports/report_YYYYMMDD.html(文件存在即幂等,跨重启去重),形成可回溯运营周志。"""
    # —— metrics.py 静态 + 行为(隔离 DB) ——
    m = (ROOT / "metrics.py").read_text(encoding="utf-8")
    (ok if "def profile_series" in m else ng)("metrics 角色档案查询 profile_series")
    import subprocess as _sp
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        script = (
            "import os,sys\n"
            f"os.environ['CONV_METRICS_DB']=os.path.join(r'{td}','t.db')\n"
            f"sys.path.insert(0,r'{ROOT}')\n"
            "import metrics\n"
            "metrics.record_turn(profile='甲', ttfa_ms=900, perceived_ttfa_ms=600)\n"
            "metrics.record_turn(profile='乙', ttfa_ms=500)\n"
            "metrics.record_clone_score(profile='甲', cosine=0.71)\n"
            "metrics.record_feedback(profile='甲', rating=1)\n"
            "s=metrics.profile_series('甲', days=7)\n"
            "assert s['ok'] and s['turns']['alltime']==1, '按角色过滤失败(混入他人轮次)'\n"
            "assert len(s['clone'])==1 and s['feedback']['n']==1, '角色序列/账本缺失'\n"
            "assert len(s['daily'])==7, '角色日桶数不对'\n"
            "metrics.reset(scope='window')\n"
            "s2=metrics.profile_series('甲', days=7)\n"
            "assert s2['turns']['alltime']==1, '下钻档案被软清抹掉了(应看全史)'\n"
            "print('BEHAV3_OK')\n")
        try:
            p = _sp.run([sys.executable, "-c", script], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
            if "BEHAV3_OK" in (p.stdout or ""):
                ok("metrics 行为: 角色档案按名过滤/全史不吃软清/日桶")
            else:
                ng(f"metrics 角色档案行为失败: {(p.stderr or p.stdout or '').strip()[-200:]}")
        except Exception as e:
            ng(f"metrics 角色档案行为异常: {e}")
    # —— hub 契约 ——
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [
            ('@app.get("/api/dashboard/profile")', "角色下钻端点"),
            ('@app.get("/api/dashboard/stream")', "SSE 推流端点"),
            ('media_type="text/event-stream"', "SSE 媒体类型"),
            ('yield ": hb\\n\\n"', "心跳注释帧(无变化不推数据)"),
            ('fmt == "png"', "战报长图分支"),
            ("report_shot.py", "长图子进程脚本接线"),
            ("def _report_archive_run", "归档执行器(幂等)"),
            ('@app.post("/api/dashboard/report/archive")', "手动归档端点"),
            ("asyncio.create_task(_bg_report_archive())", "夜间归档环已注册"),
            ("_REPORT_ARCHIVE_KEEP", "归档保留数清老档")]:
        (ok if needle in hub else ng)(f"hub 看板v3 {label}")
    # —— dashboard.html 契约 ——
    dash = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    for needle, label in [
            ("new EventSource", "SSE 客户端"),
            ("sseRetryTs", "断流回退轮询+定时重试"),
            ("stopSSE()", "页面不可见断流省资源"),
            ("async function openDrill", "角色下钻浮层"),
            ("closest('[data-prof]')", "下钻走 data 属性委托(无注入面)"),
            ("/api/dashboard/profile", "下钻取数端点"),
            ("#p=", "下钻深链(hash)"),
            ("e.key!=='Escape'", "Esc 关闭浮层(下钻/对比)")]:
        (ok if needle in dash else ng)(f"dashboard v3 {label}")
    # —— 战报长图 ——
    rep = (ROOT / "static" / "report.html").read_text(encoding="utf-8")
    (ok if "savePng" in rep and "fmt=png" in rep else ng)("report.html 保存长图按钮")
    shot = ROOT / "tools" / "report_shot.py"
    if shot.is_file():
        s = shot.read_text(encoding="utf-8")
        (ok if "full_page=True" in s and ".no-print" in s
         else ng)("report_shot 整页截图+隐藏工具条")
    else:
        ng("tools/report_shot.py 缺失")


def test_dashboard_v45():
    """看板 v4+v5: v4=质量告警外发(alerts.py 去抖+恢复报平安)/历史战报/A-B 对比;
    v5=告警文案带直达链接(看板下钻+调音台)+auto_tune 联动(在跑就不催人)+告警通路自检按钮
    +对比赢家 👑 高亮(双方有数才比)+「较昨日」差分(看板 hero 与战报头条)。
    v4 端点上轮实现但门禁没落档,这里一并补上。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [
            ('@app.get("/api/dashboard/alerts/status")', "告警状态端点"),
            ('@app.post("/api/dashboard/alerts/eval")', "手动裁决端点"),
            ('@app.post("/api/dashboard/alerts/test")', "通路自检端点(v5)"),
            ('@app.get("/api/dashboard/compare")', "A/B 对比端点"),
            ('@app.get("/api/dashboard/reports")', "历史战报列表端点"),
            ("def _dashboard_quality_sentinel_eval", "告警裁决器"),
            ("asyncio.create_task(_bg_dashboard_quality_sentinel())", "告警后台环已注册"),
            ('clear_alert(key, note="指标已回到正常范围")', "恢复报平安"),
            ("def _dash_public_base", "直达链接地址前缀(v5)"),
            ("/dashboard#p={_uq(prof)}", "告警文案带下钻深链(v5)"),
            ("def _dash_autotune_running_job", "auto_tune 联动(v5)"),
            ("自动修复已在跑", "在跑任务提示不催人(v5)")]:
        (ok if needle in hub else ng)(f"hub 看板v4/v5 {label}")
    # v5 单快照复用：差评路径不得再做第二次全量快照
    (ok if "full_snap = _convmetrics.snapshot" not in hub
     else ng)("差评路径单快照复用(不再二次全量快照)")
    dash = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    for needle, label in [
            ("async function openCompare", "A/B 对比浮层"),
            ("#compare=", "对比深链(hash)"),
            ("async function openReportHistory", "历史战报入口"),
            ("async function loadAlertChannels", "告警通道状态展示"),
            ("async function testAlerts", "通路自检按钮(v5)"),
            ("/api/dashboard/alerts/test", "自检端点接线(v5)"),
            ("function compareWins", "对比赢家判定(v5)"),
            (".compare-col .kpi.win", "赢家高亮样式(v5)"),
            ("👑", "赢家皇冠标(v5)"),
            ("function dayDiffTxt", "较昨日差分(v5)"),
            ("window.addEventListener('hashchange', applyHashRoute)",
             "深链热路由(开着的页里改 hash 也开浮层)(v5)")]:
        (ok if needle in dash else ng)(f"dashboard v4/v5 {label}")
    rep = (ROOT / "static" / "report.html").read_text(encoding="utf-8")
    (ok if "daydiff" in rep and "较昨日" in rep
     else ng)("report.html 头条「较昨日」差分(v5)")
    # —— 行为(隔离 DB)：连续差评 → feedback_alerts 应携带角色/严重度/调音直达 ——
    import subprocess as _sp
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        script = (
            "import os,sys\n"
            f"os.environ['CONV_METRICS_DB']=os.path.join(r'{td}','t.db')\n"
            f"sys.path.insert(0,r'{ROOT}')\n"
            "import metrics\n"
            "for _ in range(3):\n"
            "    metrics.record_feedback(profile='甲', rating=-1)\n"
            "al=metrics.snapshot().get('feedback_alerts') or []\n"
            "hit=[a for a in al if a.get('profile')=='甲']\n"
            "assert hit, '连续差评未产出角色级告警'\n"
            "a=hit[0]\n"
            "assert a['severity']=='critical', '好评率0应为 critical'\n"
            "assert 'open=tune' in (a.get('tune_url') or ''), '告警缺调音台直达'\n"
            "print('BEHAV45_OK')\n")
        try:
            p = _sp.run([sys.executable, "-c", script], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
            if "BEHAV45_OK" in (p.stdout or ""):
                ok("metrics 行为: 连续差评→角色级 critical 告警+调音直达")
            else:
                ng(f"metrics 告警行为失败: {(p.stderr or p.stdout or '').strip()[-200:]}")
        except Exception as e:
            ng(f"metrics 告警行为异常: {e}")


def test_dashboard_v6():
    """看板 v6: ①告警卡片化——钉钉/企微 markdown 卡片(链接可点,企微 critical 补 @,
    AVATARHUB_ALERT_MARKDOWN=0 逃生门,其他 webhook 退纯文本不丢链接);②日报推送——每日
    HUB_REPORT_PUSH_HOUR(默认9)点后补发式推昨日摘要卡片(三道闸:已发过/无 webhook/昨日零数据),
    企微群尽力附战报长图(≤2MB);③外链地址进设置——HUB_PUBLIC_BASE 环境变量之外,
    看板菜单可视化保存(data/dash_config.json),非技术用户也能让告警链接外网可点。"""
    al = (ROOT / "alerts.py").read_text(encoding="utf-8")
    for needle, label in [
            ("def _md_payload", "平台 markdown 载荷"),
            ("def _fmt_md", "统一卡片正文排版"),
            ("MARKDOWN_ON", "纯文本逃生门"),
            ('"msgtype": "markdown"', "markdown 消息类型"),
            ("def _notify_md", "卡片外发(不认识的 webhook 退纯文本)"),
            ("links=None", "raise_alert links 参数"),
            ("md_body", "notify_event 整段卡片正文"),
            ('_payload(u, "☝ "', "企微 critical 补 @(markdown 无法 @手机号)")]:
        (ok if needle in al else ng)(f"alerts 卡片化 {label}")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    for needle, label in [
            ('@app.get("/api/dashboard/public_base")', "外链地址读取端点"),
            ('@app.post("/api/dashboard/public_base")', "外链地址保存端点"),
            ("dash_config.json", "外链地址持久化"),
            ("def _report_push_digest", "日报摘要构建"),
            ("def _report_push_run", "日报推送执行器"),
            ('"already_sent"', "日报闸1: 当日去重"),
            ('"no_webhook"', "日报闸2: 无通道跳过"),
            ('"empty_yesterday"', "日报闸3: 昨日零数据不空喊"),
            ("def _report_push_png_wecom", "企微长图尽力而为"),
            ('"msgtype": "image"', "企微图片消息载荷"),
            ("1_400_000", "长图 2MB 限额留余量"),
            ('@app.post("/api/dashboard/report/push")', "手动推送端点"),
            ("asyncio.create_task(_bg_report_push())", "日报推送环已注册"),
            ('source="dashboard", links=links)', "质量告警传结构化链接")]:
        (ok if needle in hub else ng)(f"hub 看板v6 {label}")
    dash = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    for needle, label in [
            ("async function pushReport", "推送日报按钮"),
            ("/api/dashboard/report/push", "推送端点接线"),
            ("async function editPublicBase", "外链地址设置弹窗"),
            ("/api/dashboard/public_base", "外链端点接线"),
            ("pbInput", "地址输入框")]:
        (ok if needle in dash else ng)(f"dashboard v6 {label}")
    # v6 顺手修的性能病根：vcam 离线时 stream_out 探测把冷快照拖到 12s+（首屏全「加载中」）
    so = (ROOT / "stream_out.py").read_text(encoding="utf-8")
    for needle, label in [
            ("_PROBE_TTL", "探测结果短 TTL 缓存"),
            ("def _get_json", "同 URL 探测共享一次结果"),
            ("return_exceptions=True", "插件状态并发收集(离线只付一轮超时)")]:
        (ok if needle in so else ng)(f"stream_out 冷快照提速 {label}")
    # —— 行为(隔离 state/无 webhook)：卡片载荷结构 + links 入 state + 自检/事件不炸 ——
    import subprocess as _sp
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        script = (
            "import os,sys,json\n"
            "from pathlib import Path\n"
            "os.environ.pop('AVATARHUB_ALERT_WEBHOOK', None)\n"
            "os.environ['AVATARHUB_ALERT_TOAST']='0'\n"
            f"sys.path.insert(0,r'{ROOT}')\n"
            "import alerts\n"
            f"alerts.STATE=Path(r'{td}')/'s.json'; alerts.HIST=Path(r'{td}')/'h.jsonl'\n"
            "p=alerts._md_payload('https://oapi.dingtalk.com/robot/send?access_token=x','t','### hi',['138'])\n"
            "assert p['msgtype']=='markdown' and '138' in p['at']['atMobiles'] and '@138' in p['markdown']['text']\n"
            "p=alerts._md_payload('https://qyapi.weixin.qq.com/x','t','### hi')\n"
            "assert p['msgtype']=='markdown' and p['markdown']['content']=='### hi'\n"
            "assert alerts._md_payload('https://example.com/hook','t','x') is None\n"
            "md=alerts._fmt_md('T',fields=[('a','1'),('b',None)],links=[('看板','http://x')],level='critical')\n"
            "assert '[看板](http://x)' in md and '严重' in md and 'b' not in md\n"
            "alerts.raise_alert('t6','标题',detail='d',level='warn',links=[('看板','http://x/d')])\n"
            "st=json.loads(alerts.STATE.read_text(encoding='utf-8'))\n"
            "assert st['t6']['links'] and st['t6']['links'][0][1]=='http://x/d'\n"
            "assert alerts.clear_alert('t6',note='ok') is True\n"
            "r=alerts.send_test('自检',links=[('打开','http://x')])\n"
            "assert r['ok'] and r['markdown'] and r['webhook_count']==0\n"
            "assert alerts.notify_event('日报',md_body='### 摘要',links=[('看板','http://x')])==0\n"
            "print('BEHAV6_OK')\n")
        try:
            p = _sp.run([sys.executable, "-c", script], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT))
            if "BEHAV6_OK" in (p.stdout or ""):
                ok("alerts 行为: 卡片载荷/links 入 state/自检与事件通路")
            else:
                ng(f"alerts 卡片行为失败: {(p.stderr or p.stdout or '').strip()[-200:]}")
        except Exception as e:
            ng(f"alerts 卡片行为异常: {e}")


def main():
    print("Phase 12-A+B+C+D+E 离线门禁")
    test_hair_activate_alias()
    test_tryon_in_services()
    test_hub_endpoints_phase_a()
    test_hub_endpoints_phase_b()
    test_interp_phase_b()
    test_phase_c_bg_replace()
    test_phase_c_face_map()
    test_phase_c_hair_tryon()
    test_lab_ui_markers()
    test_no_bare_tryon_in_quick_ops()
    test_phase_d_checkup()
    test_phase_d_webrtc_fallback()
    test_phase_d_cluster_launcher()
    test_phase_e_docs()
    test_phase_e_hw_guide()
    test_phase_e_claims()
    test_phase_e_checkup_gate()
    test_b5_asr_route()
    test_voice_guard()
    test_p0h_subtitle_sanity()
    test_p0i_quality_observability()
    test_swap_session_report()
    test_obs_ops_view()
    test_p8_main_face_away()
    test_checkup_ledger()
    test_device_humanize()
    test_device_prefs()
    test_p3_hotswitch_prefs_audit()
    test_p4_ledger_funnel_fresh()
    test_p5_devflow_ops_recap()
    test_p6_autoswitch_weekly()
    test_p7_instant_notice_advice()
    test_p8_rescue_advice_metrics()
    test_p9dev_interact_dedupe()
    test_p11dev_fe_patrol()
    test_port_guard()
    test_port_override()
    test_human_rating()
    test_p8s_mainface_hyst()
    test_p8t_orphan_adopt()
    test_p8u_adopt_backfill_away()
    test_p8v_away_settings()
    test_p9_hot_params_persist_calib()
    test_p10_devprobe_isolation()
    test_p11_stability_ledger()
    test_p12_sentinel_probe_exit()
    test_p13_activate_debounce()
    test_p14_swapcore_watch()
    test_uivr_routine()
    test_secrets_selfheal()
    test_sing_p0()
    test_dashboard_v2()
    test_dashboard_v3()
    test_dashboard_v45()
    test_dashboard_v6()
    test_song_cover_p1()
    test_song_station_p2()
    test_song_p3()
    test_song_p4()
    test_song_p5()
    test_song_p6()
    if FAIL:
        print(f"\n合计 FAIL {len(FAIL)}")
        for f in FAIL:
            print(" -", f)
        return 1
    print("\n全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
