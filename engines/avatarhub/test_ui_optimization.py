#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UI 统一与美化 回归测试（P0~P4）。
用法：先起静态服务 .venv_launcher\\Scripts\\python.exe -m http.server 8099
然后 .venv_launcher\\Scripts\\python.exe test_ui_optimization.py
"""
import os, sys, io, urllib.request, urllib.error
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).parent
# 可指向 Hub（门禁集成时 UI_TEST_BASE/HUB_URL 注入）；缺省回退到独立静态服务 8099。
BASE = os.environ.get("UI_TEST_BASE") or os.environ.get("HUB_URL") or "http://localhost:8099"

results = []
def check(name, cond, detail=""):
    # cond 可为 True/False/None；None 表示「跳过」（如静态服务未起，不计入失败）
    results.append((name, cond if cond is None else bool(cond), detail))

def read(p):
    return (ROOT / p).read_text(encoding="utf-8", errors="ignore")

def http_ok(path):
    try:
        with urllib.request.urlopen(BASE.rstrip("/") + path, timeout=8) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False   # 服务在、但状态码异常 → 真实失败
    except Exception:
        return None    # 连不上服务 → 跳过（离线门禁里不算失败）

def run():
    results.clear()
    css = read("static/brand.css")
    js = read("static/brand.js")
    ui = read("static/ui.html")
    phone = read("static/phone.html")
    conv = read("static/converse.html")
    hub = read("static/hub.js")
    # ui.html 的行为脚本已抽离到 hub.js（ui.html 通过 <script src> 加载）。存在性断言按「整体 UI」检索，
    # 使「JS 状态/方法」类断言随抽离而不失效。hub.js 不含 HTML（bd-card / bd-card-title / <main / 旧卡串
    # 计数均为 0，已核实），故下方计数/结构/not-in 类断言不受此并入影响。
    ui = ui + "\n/*__hub.js__*/\n" + hub

    # P0 令牌
    check("P0 字阶 label=12px", "--bd-fs-label: 12px" in css)
    check("P0 xs 提到 12px", "--bd-fs-xs: 12px" in css)
    check("P0 间距阶梯", "--bd-sp-4: 16px" in css)
    check("P0 动效 token", "--bd-dur-fast: 150ms" in css)
    check("P0 进度条组件", ".bd-progress{" in css and ".bd-progress.indeterminate" in css)
    check("P0 Toast 组件", ".bd-toast{" in css and ".bd-toast-wrap{" in css)
    check("P0 徽标三态", ".bd-badge{" in css and ".bd-badge.muted" in css)

    # P1 后台接入
    check("P1 hub.blue 走 --hub-blue", "rgb(var(--hub-blue) / <alpha-value>)" in ui)
    check("P1 默认品牌蓝三元组", "--hub-blue:79 122 255" in ui)
    check("P1 JS 默认 brand 蓝", "brand:'79 122 255'" in ui)
    check("P1 重置回退蓝", "setProperty('--hub-blue','79 122 255')" in ui)
    check("P1 语义色对齐 brand", "green:'#34d399'" in ui and "red:'#f87171'" in ui)
    check("P1 可读性层 9px>=12", ".text-\\[9px\\]{font-size:12px" in ui)
    check("P1 无旧主色默认残留", "--hub-blue:88 166 255" not in ui)

    # P2 组件统一 + 进度
    check("P2 Toast 用 bd-toast", 'class="bd-toast"' in ui)
    check("P2 体检计时器", "_probeTimer" in ui and "probeElapsed" in ui)
    check("P2 阶段文案", "probeStageText" in ui)
    check("P2 向导进度条", 'class="bd-progress indeterminate"' in ui)

    # P3 Demo 模式 + 报错收口
    check("P3 demoMode 状态", "demoMode:" in ui and "hub_demo" in ui)
    check("P3 toggleDemo 方法", "toggleDemo()" in ui)
    check("P3 ops 受 demo 控制", "opsAlerts.length && !demoMode" in ui)
    check("P3 WS 条受 demo 控制", "!wsConnected && !demoMode" in ui)
    # [降噪·WS2] 告警 demo 过滤已从模板下沉到 hub.js hubAlerts()（中枢化排序/去重的单一入口）；两处任一即通过
    check("P3 用户告警 demo 过滤", "demoMode && (a.level==='warn'||a.level==='info')" in ui or "demoMode && (a.level==='warn'||a.level==='info')" in hub)

    # P4 客户端品牌统一
    check("P4 phone accent 品牌蓝", "--acc-rgb: var(--bd-acc-rgb, 79 122 255)" in phone)
    check("P4 phone 无旧 accent", "--acc-rgb: 88 166 255" not in phone)
    check("P4 converse 品牌蓝", "rgb(var(--bd-acc-rgb, 79 122 255))" in conv)

    # P5 后台侧边导航 + Tab 持久化
    check("P5 桌面侧边栏", "桌面左侧分组导航" in ui and "hidden lg:flex flex-col shrink-0" in ui)
    check("P5 移动横向导航保留", "lg:hidden tabscroll" in ui)
    check("P5 goTab 方法", "goTab(id)" in ui and "localStorage.setItem('hub_tab'" in ui)
    check("P5 侧边栏用 goTab", '@click="goTab(t.id)"' in ui)
    check("P5 visitedTabs 状态", "visitedTabs:" in ui)
    check("P5 init 恢复 Tab", "localStorage.getItem('hub_tab')" in ui)
    check("P5 main 自适应", "lg:flex-1 lg:min-w-0" in ui)
    check("P5 flex 包裹闭合", '</div><!-- /导航+内容 flex 包裹 -->' in ui)
    # 结构平衡：导航 flex 包裹的 div 开/闭对齐（粗校验 main 仍唯一）
    check("P5 main 唯一", ui.count("<main ") == 1 and ui.count("</main>") == 1)

    # P6 iframe 懒加载 + Hero 强化
    check("P6 看板 iframe 懒加载", "visitedTabs.includes('dashboard') ? '/dashboard' : ''" in ui)
    check("P6 同传 iframe 懒加载", "visitedTabs.includes('interp') ? interpUrl : ''" in ui)
    check("P6 预览 iframe 按需载", "videoModal ? '/realtime/dual_preview' : ''" in ui)
    check("P6 无裸 dashboard src", 'src="/dashboard"' not in ui)
    check("P6 无裸 dual_preview src", 'src="/realtime/dual_preview"' not in ui)
    check("P6 Hero 价值主张", "克隆你的声音与形象，几分钟上线数字人" in ui)
    check("P6 Hero 主CTA向导", "🚀 三步上手向导" in ui)
    check("P6 Hero 次CTA开播", "goTab('stream')" in ui and "📡 开播" in ui)   # 2026-07-08 改名:一键开播→开播·出画面

    # P7 侧栏折叠 + 命令面板 + 按钮组件
    check("P7-a 折叠状态", "sidebarCollapsed:" in ui and "hub_sidebar_collapsed" in ui)
    check("P7-a 折叠方法", "toggleSidebar()" in ui)
    check("P7-a 侧栏宽度绑定", "sidebarCollapsed ? 'w-14' : 'w-52'" in ui)
    check("P7-b 命令面板状态", "cmdShow:false" in ui and "cmdQuery" in ui)
    check("P7-b 命令面板方法", "openCmd()" in ui and "cmdRun(" in ui and "cmdResults" in ui)
    check("P7-b Ctrl/⌘+K 绑定", "(e.ctrlKey||e.metaKey) && (e.key==='k'||e.key==='K')" in ui)
    check("P7-b Esc 关面板", "if(this.cmdShow){ this.cmdShow=false; return true; }" in ui)
    check("P7-b 面板输入框", 'id="cmdInput"' in ui)
    check("P7-c bd-btn 基座", ".bd-btn{" in css and ".bd-btn-primary{" in css and ".bd-btn-ghost{" in css)
    check("P7-c Hero 用 bd-btn", 'class="bd-btn bd-btn-primary"' in ui and 'class="bd-btn bd-btn-ghost"' in ui)

    # P8 卡片收敛 .bd-card + 按钮 success 变体
    check("P8 bd-card 定义", ".bd-card{" in css and "background:#141a24" in css and "border:1px solid #262f42" in css)
    check("P8 bd-card 含投影", ".bd-card{" in css and "box-shadow:0 1px 3px rgba(0,0,0,.28)" in css)
    check("P8 bd-btn-success 变体", ".bd-btn-success{" in css)
    check("P8 ui 已用 bd-card", ui.count("bd-card") - ui.count("bd-card-title") >= 24)  # P3 改下限式：卡片继续用 token 即可（历史：31→26 语音页面板化；26→24 v3.6 命令面板/快捷键卡升 bd-modal）
    check("P8 旧标准卡片串清零", "bg-hub-card border border-hub-border rounded-xl" not in ui)
    # 2026-07-08 v3.6 重基线：模态卡面收敛为 .bd-modal 组件（原「≥3 处 bg-hub-card…rounded-2xl」串正式清零转防回潮）
    check("P8 模态卡面收敛 bd-modal", ui.count('"bd-modal ') + ui.count('"bd-modal"') >= 10
                                    and "bg-hub-card border border-hub-border rounded-2xl" not in ui)
    check("P8 黄/蓝异色卡保留", "border border-hub-yellow/40 rounded-xl" in ui and "border border-hub-blue/40 rounded-xl" in ui)

    # P9 卡片标题收敛 + 可访问性(a11y)
    check("P9 bd-card-title 定义", ".bd-card-title{" in css and "rgb(var(--hub-blue" in css)
    check("P9 标题用 token 26 处", ui.count("bd-card-title") >= 29)  # P3 改下限式：新卡标题继续用 token 即可（历史：28→29 监控面板）
    check("P9 旧标题串清零", "text-hub-blue font-bold text-sm" not in ui)
    check("P9 语义色标题保留", "text-hub-purple font-bold text-sm" in ui and "text-hub-orange font-bold text-sm" in ui)
    check("P9 焦点环 focus-visible", "input:focus-visible" in css and "outline:2px solid var(--bd-acc" in css)
    check("P9 减少动效偏好", "@media (prefers-reduced-motion: reduce)" in css)

    # P10 客户端跨设备白标联动（--bd-acc-rgb 中央三元组）
    check("P10 中央三元组令牌", "--bd-acc-rgb:" in css and "79 122 255" in css)
    check("P10 brand.js 同步三元组", "setProperty('--bd-acc-rgb'" in js)
    check("P10 brand.js 重置清三元组", "removeProperty('--bd-acc-rgb')" in js)
    check("P10 phone 跟随中央令牌", "--acc-rgb: var(--bd-acc-rgb" in phone)
    check("P10 phone 移除旧单键桥", "if(b) document.documentElement.style.setProperty('--acc-rgb', b)" not in phone)
    check("P10 converse 跟随中央令牌", "--accent:  rgb(var(--bd-acc-rgb" in conv)
    check("P10 converse 无硬编码主色", "--accent:  #4f7aff;" not in conv)

    # V2 全站深度美化（2026-07）：面板/表单/分段/开关组件 + 开播页双栏面板化 + 微字号源头清零
    check("V2 bd-panel 组件", ".bd-panel{" in css and ".bd-panel-head{" in css and ".bd-panel-sum{" in css)
    check("V2 表单字段组件", ".bd-form{" in css and ".bd-field{" in css and ".bd-hint{" in css)
    check("V2 分段控件", ".bd-seg{" in css and ".bd-seg>button.on{" in css)
    check("V2 开关与数值徽章", ".bd-switch{" in css and ".bd-val{" in css)
    check("V2 stream 面板化", ui.count("bd-panel-head") >= 6 and "togglePanel(" in ui and "fxSummary()" in ui)
    check("V2 stream 双栏骨架", "xl:grid-cols-12" in ui and "xl:sticky xl:top-14" in ui)
    check("V2 微字号源头清零", "text-[9px]" not in ui and "text-[10px]" not in ui and "text-[11px]" not in ui)

    # V3 按钮体系（2026-07-08）：bd-act 面板动作按钮 + bd-btn 大 CTA 全站收编，防散拼回潮
    check("V3 bd-act 组件定义", ".bd-act{" in css and ".bd-act.sm{" in css and ".bd-act.primary{" in css
                              and ".bd-act.green{" in css and ".bd-act.red{" in css and ".bd-act.amber{" in css)
    check("V3 bd-btn-amber 变体", ".bd-btn-amber{" in css)
    check("V3 bd-act 控件行高令牌", "--bd-act-h:" in css)
    check("V3 ui 已用 bd-act ≥200", ui.count("bd-act") >= 200)
    # 防回潮：按钮级散拼串出现即回潮（徽章 span 用 rounded-full 不受影响；file: 伪元素前缀不匹配）
    check("V3 散拼蓝实底按钮清零", "rounded-lg bg-hub-blue text-white" not in ui and "rounded-xl bg-hub-blue text-white" not in ui)
    check("V3 散拼绿实底按钮清零", "rounded-lg bg-hub-green text-black" not in ui)
    check("V3 散拼黄实底按钮清零", "py-0.5 rounded-lg bg-hub-yellow/90" not in ui and "py-1 rounded-lg bg-hub-yellow/90" not in ui)
    check("V3 phone 无游离主色", "#5b8cff" not in phone)

    # V3.6 弹窗骨架（2026-07-08）：bd-modal 卡面/头部/关闭钮组件化，红圆常驻关闭钮清零
    check("V36 bd-modal 组件定义", ".bd-modal{" in css and ".bd-modal-head{" in css
                                 and ".bd-modal-title{" in css and ".bd-modal-x{" in css)
    check("V36 关闭钮 hover 转红", ".bd-modal-x:hover{background:var(--bd-danger" in css)
    check("V36 红圆关闭钮清零", "bg-hub-red text-white w-6 h-6 rounded-full" not in ui
                              and "bg-hub-red text-white w-7 h-7 rounded-full" not in ui)

    # V3.7 空态/提示条收敛（2026-07-08）：bd-strip 补 mute 中性档，手拼「muted 底框」串清零；
    # 小号控件 .bd-ctl-sm 与 .bd-act.sm 同排零错位（资产抽屉/音色库列表行）
    check("V37 bd-strip.mute 定义", ".bd-strip.mute{" in css)
    check("V37 ui 空态/提示已条带化 ≥15", ui.count("bd-strip mute") >= 15)
    check("V37 手拼 muted 底框清零", "text-hub-muted bg-hub-bg border border-hub-border rounded-lg" not in ui
                                   and "text-hub-muted bg-hub-bg/60 border border-hub-border/60 rounded-lg" not in ui)
    check("V37 bd-ctl-sm 定义", ".bd-ctl-sm{" in css and "height:26px" in css)
    check("V37 列表行控件已对齐 ≥5", ui.count("bd-ctl-sm") >= 5)

    # V3.8 弹层动效节奏统一（2026-07-08）：三层节奏单源化——
    # 弹窗卡面=CSS bd-modal-in（零标记成本）；行内展开/换页=fade 150ms；popover/角卡=Alpine 默认 pop。
    check("V38 bd-modal-in 动画定义", "@keyframes bd-modal-in{" in css and "animation:bd-modal-in" in css)
    check("V38 卡面缩放散拼清零", 'x-transition:enter-start="opacity-0 scale-95"' not in ui)
    check("V38 面板体裸过渡清零", 'x-transition class="bd-panel-body' not in ui)
    check("V38 preparing 横幅不跳位", "x-show=\"stageOf(active)==='preparing'\" x-transition.opacity" in ui)
    check("V38 历史行可点悬停线索", "hover:border-hub-blue/30" in ui)

    # V3.9 窄屏收口（2026-07-08）：390px 全页签零横向溢出——顶栏/卡头 flex-wrap 化，
    # 摄像头 select 加 min-w-0 可收缩，硬件矩阵行 flex-wrap 响应式。中文短语一律 nowrap 防竖排。
    check("V39 顶栏可换行", 'class="flex items-center gap-x-2.5 gap-y-1 flex-wrap min-w-0"' in ui)
    check("V39 摄像头下拉可收缩", 'x-model.number="selectedCamera"' in ui and
          'class="flex-1 min-w-0 text-xs py-1"' in ui)
    check("V39 硬件矩阵行响应式", 'class="flex flex-wrap items-start gap-x-2 gap-y-0.5 py-1 border-t border-white/5 text-[13px]"' in ui)
    check("V39 硬件说明列换行降级", 'class="basis-full sm:basis-0 sm:grow min-w-0 text-hub-muted"' in ui)

    # V3.11 触控目标层（2026-07-09）：pointer:coarse 才生效（按输入设备不按视口宽），
    # headless 截图为 fine 指针→像素基线天然零扰动。令牌改写抬 .bd-act 全族；
    # .bd-tap 垫高字形钮/文字链命中区；data-touch-show 让 hover 才显的入口触屏常显。
    check("V311 触控层媒体查询", "@media (pointer:coarse)" in css)
    check("V311 主动作抬 40px", ":root{--bd-act-h:40px}" in css)
    check("V311 bd-tap 定义", ".bd-tap{min-width:34px;min-height:34px" in css)
    check("V311 bd-tap 挂载 ≥8", ui.count("bd-tap") >= 8)
    check("V311 触屏常显入口", "[data-touch-show]{opacity:1!important}" in css and "data-touch-show" in ui)
    check("V311 角色卡胶囊不竖排", 'class="flex flex-wrap justify-center gap-1.5 mt-2"' in ui)

    # P11 客户端可读性收敛（字号下限 11px，集中可回退层）
    check("P11 phone 可读性层", "可读性层（P11）" in phone)
    check("P11 phone 字号下限11", "font-size:11px !important;" in phone)
    check("P11 phone 角标保守10", ".pcard .preg { font-size:10px !important; }" in phone)
    check("P11 层覆盖音色轴", ".pcard .pq, .pcard .pq.pct," in phone)

    # P-Home 首页(landing)回归护栏：本轮把搜索/拼音/最近使用/快速开始/信任徽标/PRO 弹窗/埋点/版位取真锁进门禁
    home = read("static/home.html")
    check("PH 搜索框+清除", 'id="q"' in home and 'id="qClear"' in home)
    check("PH 搜索提示含拼音", "支持中文/拼音" in home)
    check("PH 拼音映射表", "var PY = {" in home and '"直":"zhi"' in home and '"播":"bo"' in home)
    check("PH pyIndex 全拼+首字母", "function pyIndex(" in home and "full+=p" in home and "ini+=p[0]" in home)
    check("PH data-search 拼音兜底", "pyIndex(feat.name) + pyIndex(feat.line)" in home)
    check("PH 最近使用(localStorage)", "bd_recent_v1" in home and "renderRecentSection(" in home and "pushRecent" in home)
    check("PH 快速开始分流", 'class="quickstart-h"' in home and "快速开始" in home and "data-line" in home)
    check("PH 搜索态隐藏分流", "body.searching .roles,body.searching .quickstart-h{display:none}" in home)
    check("PH 信任三徽标", "全程本地运行" in home and "内容可验真" in home and "授权合规" in home)
    check("PH 版位可点开 PRO", 'button class="edition"' in home and 'id="proDlg"' in home and "buildProDialog(" in home)
    check("PH PRO 深链 #pro", 'location.hash==="#pro"' in home)
    check("PH 版位取真 lic.state", "(lic && lic.state) || lic" in home)
    check("PH 埋点独立通道", "/api/ui/event" in home and "navigator.sendBeacon" in home)
    # 拼音下沉后端（单一真源）+ 前端优先消费、字表兜底
    hub_py = read("avatar_hub.py")
    check("PH 后端拼音下沉", "_augment_registry_pinyin" in hub_py and 'f["pysearch"]' in hub_py)
    check("PH 后端 pypinyin 快路径", "from pypinyin import" in hub_py and '_PY_ENGINE = "builtin"' in hub_py)
    check("PH 前端优先 pysearch", "feat.pysearch" in home)
    # 核心服务判定统一到模式感知的 broadcast.core（消除 CRITICAL 静态清单误报/漏报）
    hm_py = read("health_monitor.py")
    check("PH 核心判定注入 broadcast", "def _observe_health" in hub_py and '_compute_broadcast(svc_status).get("core")' in hub_py)
    check("PH health_monitor 可注入关键集", "def set_critical" in hm_py and "_critical_override" in hm_py and "def _is_critical" in hm_py)
    # selfcheck 巡检核心判定也统一到模式感知 broadcast.core（回落静态 SERVICES.core）
    sc_py = read("selfcheck_pipeline.py")
    check("PH selfcheck 核心模式感知", "def _effective_core" in sc_py and 'j.get("broadcast")' in sc_py and "def _static_core_set" in sc_py)
    check("PH selfcheck 回落静态兜底", 'return _static_core_set(), "static"' in sc_py)
    # broadcast 暴露 core_busy（核心仅去抖判活=降级可见，不改 ok/告警），状态条呈现"繁忙"
    check("PH broadcast 降级可见 core_busy", '"core_busy": core_busy' in hub_py and "_compute_broadcast(status, busy_names)" in hub_py)
    check("PH 状态条呈现核心繁忙", "bc.core_busy" in home and "项核心繁忙" in home)
    # 搜索体验收尾：结果计数 + ↑/↓ 选中 + Enter 直达（纯前端、零重载）
    check("PH 搜索结果计数", 'id="qCount"' in home and "项匹配" in home and ".qcount" in home)
    check("PH 键盘导航结果集", "function resultCards(" in home and ":not(.recent-sec):not(.hide) .card:not(.hide)" in home)
    check("PH 键盘选中活动项", "function setActive(" in home and "var _kbActive" in home and 'toggle("kbdon"' in home)
    check("PH 键盘高亮描边", ".card.kbdon{" in home and "scrollIntoView" in home)
    check("PH ↑↓选中 Enter 直达", 'e.key==="ArrowDown"' in home and 'e.key==="ArrowUp"' in home
                                 and 'e.key==="Enter"' in home and "]).click();" in home)
    # 首屏冷启骨架：复用 brand .bd-skeleton（单一真源），render() 清空 #sections 即替换
    check("PH 冷启骨架占位", 'class="skeleton"' in home and "bd-skeleton skel-card" in home
                          and "bd-skeleton skel-h" in home and 'aria-hidden="true"' in home)
    check("PH 骨架复用 brand 组件", ".bd-skeleton{" in read("static/brand.css") and 'root.innerHTML = ""' in home)
    # 搜索无障碍收尾：离屏 live region 播报选中项+位置、aria-current 标记、首次方向键不跳过
    check("PH 无障碍离屏播报", 'id="srStatus"' in home and "bd-sr-only" in home
                            and ".bd-sr-only{" in read("static/brand.css"))
    check("PH 播报选中项+位置", '"，第 "+(_kbActive+1)+" 项 / 共 "' in home and 'setActive(list, true)' in home)
    check("PH aria-current 标记活动项", 'setAttribute("aria-current","true")' in home and 'removeAttribute("aria-current")' in home)
    check("PH 首次方向键先读当前项", "var _kbActive = -1, _kbNav = false;" in home and "if(!_kbNav){ _kbNav=true;" in home)
    # 最近使用管理：置顶(pin，独立存储永不淘汰) + 一键清除(只清历史保留置顶) + 就地重渲
    check("PH 置顶独立存储", "bd_pinned_v1" in home and "function togglePin(" in home and "function pinnedIds(" in home)
    check("PH 置顶排前去重", "pinItems.concat(recItems)" in home and "!pinSet[f.id]" in home)
    check("PH 置顶按钮无障碍", 'el("button","pin-btn"' in home and 'aria-pressed' in home and ".pin-btn{" in home)
    check("PH 一键清除保留置顶", "function clearRecent(" in home and "recent-clear" in home and 'removeItem("bd_recent_v1")' in home)
    check("PH 最近就地重渲", "function rerenderRecent(" in home and "insertBefore(sec, root.firstChild)" in home and "lastHealth = health || lastHealth" in home)

    # PL 客户端平实化 / 信任措辞 / 无障碍 回归护栏（锁定会话 P6–P10 成果，防平实化/无障碍被无意改回）
    landing = read("static/landing.html")
    # P6/P8：验真措辞跨页统一为「可验真伪」，旧「C2PA 可验真」不再外泄给消费者/操作者页
    check("PL 验真措辞统一可验真伪", "可验真伪" in phone and "可验真伪" in conv)
    check("PL 无旧 C2PA 可验真串", "C2PA 可验真" not in phone and "C2PA 可验真" not in conv)
    # P6：主流程阶段提示改人话，不外泄引擎名（Whisper/LLM/Fish-Speech）
    check("PL phone 阶段人话文案", "正在听懂你说的话" in phone and "正在组织回答" in phone and "正在用克隆音色发声" in phone)
    check("PL phone 无引擎术语外泄", "Whisper 语音识别" not in phone and "LLM 生成回复" not in phone and "Fish-Speech 克隆合成" not in phone)
    # P10：home 版位按钮打开原生 <dialog>，向辅助技术声明 haspopup（置于 class="edition" 之后，不破坏 PH 断言）
    check("PL home 版位弹窗 a11y", 'aria-haspopup="dialog"' in home)
    # P9：分享死链不再扑空——有其它角色转橱窗，无角色给暖文案；加载/播放失败均暖化
    check("PL landing 死链转橱窗", "if(list.length){ showGallery(); return; }" in landing and "这个声音暂时找不到了" in landing)
    check("PL landing 暖化报错", "加载遇到点问题" in landing and "这段暂时听不了" in landing and "再点一次「听我说一句」" in landing)

    # S1-S3 开播页改版（信息收口 / 主舞台 / 修复动作）回归护栏
    check("S1 就绪度组件存在", "开播就绪度" in ui and "readyLine()" in ui and "readySub()" in ui)
    check("S1 相位状态机", "streamPhase()" in ui)
    check("S1 主裁决仅直播中", "tab==='stream' && streamPhase()==='live'" in ui)
    check("S1 预检药丸行已删", "开播前置体检" not in ui)
    check("S1 显存卡限直播中", "perf.streaming && vramTight && canFreeVram()" in ui)
    check("S1 旧未播裁决去重", "未选出镜角色，无法开播" in ui)  # preflight 数据源仍在（单一真相未动）
    check("S2 禁用原因外显", "startDisabledReason()" in ui and "reasonCta(" in ui)
    check("S2 预设单源定义表", "_presetDefs" in ui and "_presetFields" in ui)
    check("S2 预设选中态反推", "presetActive()" in ui and "自定义参数" in ui)
    check("S2 直播观众视角缩略图", "openLivePreview()" in ui and "观众视角" in ui and "/realtime/swapped.jpg?t='+streamClock" in ui)
    check("S3 修复原地反馈", "fixState" in ui and "fixScan(" in ui and "_fixNoteFor" in ui)
    check("S3 一键修复全部", "fixAll()" in ui and "fixableKeys()" in ui)
    check("S3 VB-Cable 安装向导", "cableWiz" in ui and "vb-audio.com/Cable" in ui and "cableRecheck()" in ui)
    check("S3 设备内联下拉", "pickReadyCam()" in ui and "自动选择（推荐 CABLE Input）" in ui)
    check("S3 动作词升级", "去角色库启用" in ui and "重新扫描" in ui and "安装向导" in ui)

    # S4-S5 开播页视觉统一 + 开播仪式感（批次二）回归护栏
    check("S4 条带四态令牌", ".bd-strip{" in css and ".bd-strip.ok{" in css and ".bd-strip.warn{" in css and ".bd-strip.err{" in css and ".bd-strip.info{" in css)
    check("S4 主舞台卡强调", ".bd-hero{" in css and 'class="bd-card bd-hero p-4 sm:p-5"' in ui)
    check("S4 主裁决走条带", "'ok':linkVerdict().level==='ok'" in ui)
    check("S4 旧一次性色组合清退(开播页)", "bg-green-900/25 border border-hub-green/40" not in ui and "bg-orange-900/15 border border-hub-orange/40" not in ui)
    check("S5 启动里程碑面板", "startMilestones()" in ui and "startingStageIdx()" in ui and "startingElapsed()" in ui)
    check("S5 启动面板走相位", "streamPhase()==='starting'" in ui)
    check("S5 入播弹出仪式", "bd-strip ok clone-pop" in ui)
    check("S5 成绩单四格", ".bd-stat{" in css and "本场直播成绩单" in ui and "画面稳定度" in ui and "🔁 再开一场" in ui)
    check("S5 稳定度前端采样", "sessTicksTotal" in ui and "stabilityPct" in ui and "stabilityTone(" in ui)
    check("S5 成绩单复制含稳定度", "稳定度 '+s.stabilityPct+'%'" in ui)

    # S6-S8 新手引导 + 手机提位 + 工程底座（批次三）回归护栏
    check("S6 引导状态与方法", "streamGuideDone" in ui and "hub_stream_guide_done" in ui
                            and "guideSteps()" in ui and "guideVisible()" in ui and "guideDismiss()" in ui)
    # 重基线(2026-07-06)：英雄区提示限流上线后 x-show 追加了 && heroTipVisible('guide')，改为前缀匹配（护栏意图不变：引导条仍须挂在开播页且由 guideVisible() 把关）
    check("S6 引导条在开播页", "第一次开播？三步搞定" in ui and 'x-show="tab===\'stream\' && guideVisible()' in ui)
    check("S6 首播成功自动退场", "if(!this.streamGuideDone) this.guideDismiss();" in ui)
    check("S6 老操作者免打扰", "localStorage.getItem('hub_broadcast_mode')) this.guideDismiss()" in ui)
    check("S6 无摄像头推荐数字人", "🤖 改用数字人" in ui and "无需摄像头" in ui
                                and "或改用「AI 数字人」(无需摄像头)" in ui)
    check("S7 终端在线行动条", "手机终端已在线" in ui and "phoneGuideOpen=true; wirelessStart()" in ui)
    check("S7 行动条仅真人换脸+已扫码", "phoneRelay.ok && broadcastMode==='real_faceswap' && streamPhase()!=='starting'" in ui)
    check("S7 静默链接兜底(数字人不出现)", "broadcastMode!=='avatar_lipsync' && !(phoneRelay.ok && broadcastMode==='real_faceswap')" in ui
                                        and "想用手机出镜" in ui)
    check("S8 相位截图工具", (ROOT / "tools" / "stream_state_shots.py").exists()
                          and "STATES" in read("tools/stream_state_shots.py"))
    # 刚停播的叙事顺序=时间顺序：先「这场怎么样」(成绩单)，再「下一场是否就绪」(就绪度)
    check("S5 成绩单先于就绪度", 0 < ui.find("本场直播成绩单") < ui.find("[S1 开播就绪度]"))

    # S9 开播页优化（2026-07-06）：危险确认 / 监控面板 / 可见性门控 / 用户背景入口
    check("S9 停止二次确认", "clickStop()" in ui and "armDanger('stop')" in ui)
    check("S9 在播重启二次确认", "clickRestart()" in ui and "armDanger('restart')" in ui)
    check("S9 监控与守护面板", "📊 监控与守护" in ui and "monitorSummary()" in ui and "applySensPreset(" in ui)
    check("S9 近窗失败率口径", "swapFailRateLive()" in ui and "swapStatsLine()" in ui)
    check("S9 轮询可见性门控", "_bindVisibility()" in ui and "document.hidden" in ui)
    check("S9 用户模式背景入口", "quickBgMode('blur')" in ui and "quickBgMode('none')" in ui)
    check("S9 导出对比图", "exportPreviewCompare()" in ui)
    check("S9 窄屏预览优先 order", "order-1 xl:order-none" in ui and "order-2 xl:order-none" in ui)
    check("S9 延迟趋势线", "latSparkPoints()" in ui and "lat-spark" in css)
    check("S9 智能守护徽章", "🛡 智能守护" in ui)
    check("S9 bd-badge 扩展", ".bd-badge.info{" in css and ".bd-btn-sm{" in css)

    # S10 开播页 P2（2026-07-06）：分享卡 / 市场化文案 / Pass B / 大字模式
    check("S10 成绩单分享图", "exportRecapCard()" in ui and "recapCardStats()" in ui and "_canvasDownload(" in ui)
    check("S10 预设市场化文案", "~4.5×" in ui)
    check("S10 toneClass 单源", "toneClass(" in ui and "devCheckStripClass()" in ui and "qcStripClass()" in ui)
    check("S10 试音条 bd-strip", "micTestStripClass()" in ui and "outTestStripClass()" in ui)
    check("S10 大字模式", "toggleLargeText()" in ui and "bd-large-text" in css and "hub_large_text" in ui)

    # S11 开播页 P3（2026-07-06）：一键分享 / 成绩单字段单源 / 视觉 Pass C
    check("S11 一键分享", "shareRecapCard()" in ui and "webShareOk" in ui and "_buildRecapCanvas(" in ui)
    check("S11 成绩单字段单源", "swapCropVal()" in ui and "swapLatVal()" in ui and "swapDegVal()" in ui)
    check("S11 Pass C 条带化", ui.count("bd-strip warn text-hub-orange") >= 8
                             and ui.count("bd-strip info text-hub-blue") >= 4
                             and ui.count("bd-strip ok text-hub-green") >= 3)
    check("S11 生效档徽章化", "'bd-badge warn' : 'bd-badge ok'" in ui and "facesChip().dual ? 'bd-badge info'" in ui)
    check("S11 标定结果条带", "calib.level==='good'?'bd-strip ok" in ui)

    # S12 开播页 P4（2026-07-07）：授权预设分层 / 成绩单近场趋势 / Pass D / 大字跨页
    lic_py = read("license.py")
    check("S12 授权预设分层(后端)", '"preset_ultra"' in lic_py and '"preset_vocal"' in lic_py
                                  and 'allowed("preset_ultra")' in lic_py and 'allowed("preset_vocal")' in lic_py)
    check("S12 授权预设分层(前端)", "presetLocked(" in ui and "presetLockTip(" in ui
                                  and "window.__bdLic" in ui and "bd-lic" in ui)
    check("S12 预设锁引导", "presetLocked(pk)?'🔒 '" in ui and "licChip" in ui)
    check("S12 成绩单近场趋势", "recapTrend()" in ui and "swapHist" in ui and "swap/sessions?limit=6" in ui)
    check("S12 Pass D 克隆页条带", "'bd-strip err'" in ui and "'bd-strip warn'" in ui and "'bd-strip ok'" in ui)
    check("S12 大字跨页(converse)", "toggleLargeText" in conv and "hub_large_text" in conv and "bd-large-text" in conv)

    # S13 开播页 P5（2026-07-07）：预设锁服务端执行层 / 档位对比表 / 趋势双轨
    hub_py = read("avatar_hub.py")
    check("S13 预设锁服务端闸门", "_license_preset_gate(" in hub_py and "def preset_gate" in lic_py
                                and "preset_ultra" in lic_py and "preset_vocal" in lic_py)
    check("S13 闸门降级如实告知", "lic_note" in hub_py and "lic_note" in ui)
    check("S13 档位对比表(后端单源)", "def editions_matrix" in lic_py and "editions_matrix()" in hub_py)
    check("S13 档位对比表(授权卡)", "tierTable(" in ui and "档位对比" in ui and "lastMatrix" in ui)
    check("S13 稳定度环形账本", "_pushSessHist(" in ui and "sessHistStab()" in ui and "hub_sess_hist" in ui)
    check("S13 趋势双轨", "场稳定度%" in ui and "场换脸时延(ms)" in ui)

    # S14 开播页 P7（2026-07-07）：Pass E 实心 chip 家族化 / 大字过目参数 / 相位存档挂门禁
    css = read("static/brand.css")
    hubjs = read("static/hub.js")
    check("S14 bd-chip令牌族(样式单源)", ".bd-chip.info{" in css and ".bd-chip.ok.dim{" in css
                                      and ".bd-chip.warn.strong{" in css and ".bd-chip.muted{" in css)
    check("S14 cardStatus走令牌", "cls:'warn dim'" in hubjs and "cls:'ok dim'" in hubjs
                                and "cls:'warn strong'" in hubjs and "cls:'muted'" in hubjs)
    check("S14 散拼色清退(cardStatus)", "bg-orange-900/60 text-orange-300" not in hubjs
                                      and "bg-green-900/50 text-green-400" not in hubjs
                                      and "bg-amber-900/60 text-amber-300" not in hubjs)
    check("S14 宿主挂bd-chip", ui.count('class="bd-chip') >= 3)
    check("S14 大字chip护栏", "html.bd-large-text .bd-chip{" in css)
    check("S14 大字过目参数lt=1", "[?&#]lt=1" in hubjs)
    check("S14 相位存档挂门禁", "stream_state_shots.py" in read("gate.py") and "相位存档" in read("gate.py"))

    # S15 开播页 P8（2026-07-07）：战报海报融合 / 顶栏微章家族化
    check("S15 战报海报带", "_drawRecapPosterBand(" in hubjs and "_roundRect(" in hubjs
                                and "本场直播战报" in hubjs and "_recapHeadH(" in hubjs)
    check("S15 海报金标口径", "★ 金标" in hubjs and "avatarSeed" in hubjs)
    check("S15 顶栏bd-chip.sm", ".bd-chip.sm{" in css and 'class="bd-chip sm ok"' in ui
                                and 'class="bd-chip sm accent"' in ui)
    # S16 开播页 P9（2026-07-07）：战报同网 QR 深链 / 门禁 GPU 争用预检
    acc = read("acceptance.py")
    check("S16 战报同网QR", "/s?profile=" in hubjs and "同网扫码" in hubjs
                                and "api/qr?data=" in hubjs)
    check("S16 QR优雅降级", ".catch(()=>null)" in hubjs and "qrH=qr?" in hubjs.replace(" ", ""))
    check("S16 QR可达域名(非127)", "_shareOrigin(" in hubjs and "phoneRelay.https_url" in hubjs)
    check("S16 实拍工具在位", (ROOT / "tools" / "_recap_card_shot.py").exists()
                                and "_buildRecapCanvas" in read("tools/_recap_card_shot.py"))
    check("S16 门禁GPU争用预检", "GPU_HEAVY" in acc and "_gpu_wait_idle(" in acc
                                and "ACCEPT_GPU_BUSY_UTIL" in acc and "ACCEPT_GPU_WAIT_SEC" in acc)
    check("S16 争用SKIP不假红", "GPU 争用" in acc and "--only %s" in acc)

    # S17 开播页 P10（2026-07-07）：授权卡自助激活三入口 / 战报QR归因 / 战报实拍挂门禁
    hub_py = read("avatar_hub.py")
    lsrv = read("license_server.py")
    lpy = read("license.py")
    check("S17 试用升级链路", "/api/license/trial_upgrade" in hub_py
                                and "/api/license/trial_restore" in hub_py
                                and "trial_upgrade_online" in lpy and "def trial_upgrade(" in lsrv)
    check("S17 激活端点(码/导入)", "/api/license/activate" in hub_py
                                and "activate_online" in hub_py and "activate_from_text" in hub_py)
    check("S17 授权卡三入口", "licTrialBtn" in ui and "licCodeIn" in ui and "licKeyIn" in ui
                                and "activation_configured" in ui)
    check("S17 试用一机一次台账", "_load_trials" in lsrv and "trials.json" in lsrv
                                and "trial-" in lsrv and "已用过试用升级" in lsrv
                                and "--trial-days" in lsrv)
    check("S17 试用备份还原闭环", "_backup_before_trial" in lpy and "def trial_restore(" in lpy
                                and "license_prev.key" in lpy and "licTrialRestore" in ui)
    check("S17 战报QR归因", "from=recap" in hubjs and "land_recap" in read("static/landing.html")
                                and '"land_recap"' in hub_py.replace("'", '"'))
    check("S17 海报QR归因", "from=poster" in read("static/phone.html")
                                and "land_poster" in read("static/landing.html")
                                and '"land_poster"' in hub_py.replace("'", '"'))
    check("S17 战报实拍挂门禁", "_recap_card_shot.py" in read("gate.py") and "战报实拍" in read("gate.py"))
    check("S17 授权卡实拍工具", (ROOT / "tools" / "_lic_card_shot.py").exists()
                                and "licChip" in read("tools/_lic_card_shot.py"))
    # S17 P10-3 canvas_brand.js 画布品牌基元单源：金标阈值/金渐变只许出现在公共模块，
    # hub.js 与 phone 的 canvas 代码只准经 BD_CANVAS 取用（CSS 类里的金色不在此列）
    cbrand = read("static/canvas_brand.js")
    check("S17 canvas_brand单源", "BD_CANVAS" in cbrand and "GOLD_MIN" in cbrand
                                and "canvas_brand.js" in ui and "canvas_brand.js" in phone)
    check("S17 金标阈值无副本", "isGold" in hubjs and "isGold" in phone
                                and ">=0.75" not in hubjs.replace(" ", "")
                                and "cosine>=0.75" not in phone.replace(" ", ""))
    check("S17 金渐变无副本", "#f59e0b" not in hubjs
                                and "createLinearGradient(bx" not in phone)
    # S17 P10-5 GPU 争用闸扩展：lang 补入 GPU_HEAVY（6 用例逐个真出声，占卡时同样假红）
    check("S17 lang入GPU_HEAVY", '"lang"' in acc.split("GPU_HEAVY")[1][:120].replace("'", '"'))

    # S18 开播页 P11（2026-07-07）：试用到期软着陆 / 授权审计线 / 扫码归因列 / 输码容错 / 画布文本基元
    dash = read("static/dashboard.html")
    check("S18 试用到期自动还原", "def trial_autorestore_if_due" in lpy
                                and hub_py.count("trial_autorestore_if_due") >= 2)   # 状态轮询+开机自愈双钩子
    check("S18 试用徽章/横幅接管", "试用" in ui and "trial_up.active" in ui
                                and "旗舰试用中" in ui and "自动还原" in ui)
    check("S18 授权操作审计线", "_lic_audit" in hub_py and "notify_event" in hub_py
                                and hub_py.count("_lic_audit(") >= 5)   # 激活/试用/还原/自动还原x2
    check("S18 看板扫码归因列", "扫码进站" in dash and "land_recap" in dash and "land_poster" in dash)
    check("S18 输码容错", "_norm_code" in lsrv and "toUpperCase" in ui)
    check("S18 画布文本基元单源", "wrapText" in cbrand and "ellipsize" in cbrand
                                and "BD_CANVAS.wrapText" in phone and "measureText(line+ch)" not in phone
                                and "ellipsize" in hubjs)

    # S19 开播页 P12（2026-07-07）：授权运维一页纸 / 试用转化漏斗 / 联系厂商动线 / 授权态矩阵 / 趋势扫码序列
    ops_doc = read("授权运维一页纸_LICENSE_OPS.md")
    lic_shot = read("tools/_lic_card_shot.py")
    check("S19 运维一页纸交付物", "listtrials" in ops_doc and "revocations" in ops_doc
                                and "--trial-days" in ops_doc and "alerts.jsonl" in ops_doc)
    check("S19 台账查询命令", "def cmd_listtrials" in lsrv and "def cmd_stats" in lsrv
                                and '"listtrials"' in lsrv and '"stats"' in lsrv)
    check("S19 试用转化漏斗", "def funnel_stats" in lsrv and "/api/funnel" in lsrv
                                and "conversion_pct" in lsrv)
    check("S19 serve试用参数补全", "--trial-days" in lsrv and '_STATE["trial_days"] = int(args.trial_days)' in lsrv
                                and "--trials" in lsrv)
    check("S19 联系厂商动线", '"contact"' in hub_py and "bdContactInput" in ui
                                and "vendorContact" in ui and "licBnContact" in ui and "licCardContact" in ui)
    check("S19 授权态矩阵实拍", "--matrix" in lic_shot and '"grace"' in lic_shot
                                and "_lic_card_shot" in read("gate.py"))
    check("S19 趋势图扫码序列", '"scan"' in hub_py.split("def api_share_trend")[1][:2000]
                                and "hasScan" in dash and "'扫码'" in dash)

    # S20 开播页 P13（2026-07-07）：uivr 确定性收口 / 漏斗时序 / 临期主动通知 / 一页纸对账门禁 / 看板扫码聚焦
    phone = read("static/phone.html")
    uivr_py = read("ui_visual_regress.py")
    check("S20 uivr确定性收口(phone)", "const _UIVR" in phone and phone.count("_UIVR") >= 7
                                and "avatarSeed(p.name)" in phone)
    check("S20 uivr钉死角色预检", "钉死角色" in uivr_py and "/profiles" in uivr_py)
    check("S20 漏斗时序(按签发周)", "def funnel_weekly" in lsrv and '"weekly"' in lsrv
                                and "--weeks" in lsrv and "转化时序" in lsrv)
    check("S20 临期主动通知", "def scan_expiring_trials" in lsrv and "notified_48h" in lsrv
                                and "_expiry_watch_loop" in lsrv and "def cmd_expiring" in lsrv
                                and '"expiring"' in lsrv)
    check("S20 一页纸对账门禁", read("tools/_ops_doc_gate.py").count("check(") >= 3
                                and "_ops_doc_gate" in read("gate.py")
                                and "expiring" in ops_doc and "--weeks" in ops_doc)
    check("S20 看板扫码聚焦联动", "toggleScanFocus" in dash and "scanFocus" in dash
                                and "按扫码量排" in dash and "_p13_dash_probe" in read("gate.py"))
    check("S20 厂商看板同屏(反竞态)", "allSettled" in lsrv and "--self-serve" in read("tools/_vendor_dash_shot.py")
                                and "_vendor_dash_shot" in read("gate.py"))
    check("S20 声测矩阵免疫临时角色", 'p["name"].startswith("_")' in read("_voice_quality.py"))

    # S21 开播页 P14（2026-07-07）：环境清理契约 / 一键发码 / 客户健康度 / 扫码第二跳 / 断连可感知
    wall = read("static/wall.html")
    dc = read("deliver_check.py")
    check("S21 Hub启动清扫e2e孤儿", "_sweep_test_orphan_profiles" in hub_py
                                and 'startswith("_e2e")' in hub_py)
    check("S21 交付环境快照-对账", "stage_envsnap" in dc and "stage_envguard" in dc
                                and "_env_fingerprint" in dc and '"envguard"' in dc)
    check("S21 一键发码签名链接", "def qi_sign" in lsrv and "def qi_verify" in lsrv
                                and "def qi_link" in lsrv and "/quickissue" in lsrv
                                and "compare_digest" in lsrv)
    check("S21 一键发码幂等出码", "def qi_issue" in lsrv and '"via": "quickissue"' in lsrv
                                and "--qi-edition" in lsrv and "--public-base" in lsrv)
    check("S21 临期通知附发码链接", "qi_link(h['fp'], base)" in lsrv)
    check("S21 客户健康度视图", "def customers_view" in lsrv and "/api/customers" in lsrv
                                and "def cmd_customers" in lsrv and '"customers"' in lsrv
                                and "客户健康度" in lsrv)
    check("S21 扫码归因日×角色", 'action in ("land_recap", "land_poster")' in hub_py
                                and '"by_profile"' in hub_py.split("def api_share_track")[1][:1600])
    check("S21 趋势端点角色下钻", "profile: str = \"\"" in hub_py.split("def api_share_trend")[1][:200]
                                and '"recap"' in hub_py.split("def api_share_trend")[1][:2600])
    check("S21 看板扫码第二跳", "scanDrill" in dash and "scanDrillHtml" in dash
                                and "扫码时序" in dash)
    check("S21 观众墙断连贴片", "reChip" in wall and "reToast" in wall
                                and "_wsWasUp" in wall and "uivr" in wall)
    check("S21 语义打断通道重连", "_lsRetry" in phone and "语义打断通道已恢复" in phone
                                and "能量打断兜底生效" in phone)
    check("S21 发码冒烟入闸", "_p14_smoke" in read("gate.py"))

    # S22 开播页 P15（2026-07-07）：发码激活闭环 / 健康告警化 / 归因治理 / 弱网演练 / 门禁抗并发
    gate_py = read("gate.py")
    smoke = read("tools/_p14_smoke.py")
    check("S22 激活转化回推", "def _notify_activation" in lsrv and "notify_activate" in lsrv
                            and "--no-notify-activate" in lsrv and "闭环达成" in lsrv
                            and "is_new_seat" in lsrv)
    check("S22 发码四级漏斗", "def qi_funnel" in lsrv and '"quickissue": qi_funnel' in lsrv
                            and "qi_opened" in lsrv and "一键发码漏斗" in lsrv)
    check("S22 健康告警扫描", "def scan_unhealthy_customers" in lsrv
                            and "_health_watch_loop" in lsrv and "--health-th" in lsrv
                            and "客户健康度破线" in lsrv and "min_receipts" in lsrv)
    check("S22 扫码月度归档", '_share_stats.setdefault("monthly"' in hub_py
                            and 'd.setdefault("monthly", {})' in hub_py)
    check("S22 传播CSV导出", "/api/share/export" in hub_py and "text/csv" in hub_py
                           and "date,profile,action,count" in hub_py and "导出明细 CSV" in dash)
    check("S22 弱网演练探针", "_p15_reconnect_probe" in gate_py
                            and "reChip" in read("tools/_p15_reconnect_probe.py")
                            and "set_offline" in read("tools/_p15_reconnect_probe.py"))
    check("S22 门禁单实例锁", "gate_lock_acquire" in gate_py and "_pid_alive" in gate_py
                            and "--wait-lock" in gate_py and "_gate.lock" in gate_py)
    check("S22 门禁漂移自愈", "_mtime_snapshot" in gate_py and "_run_tier" in gate_py
                            and "自动重跑一次" in gate_py and "_GATE_DRIFT" in gate_py)
    check("S22 冒烟覆盖P15", "qi_opened" in smoke and "quickissue" in smoke
                           and "share/export" in smoke)

    check("S15 顶栏散拼清退", "bg-green-900/50 text-hub-green px-1.5 rounded-full" not in ui
                                and "bg-purple-900/50 text-hub-purple px-1.5 rounded-full" not in ui
                                and "bg-yellow-900/50 text-yellow-400 px-1.5 rounded-full" not in ui)

    # UI-P2 语音页（2026-07-07）：流式吃情感 / 能力声明接口化 / 内存治理 / 最近历史内嵌
    check("UP2 流式情感路由(后端)", "_emo_stream" in hub_py and '"stream_sse_emotion"' in hub_py
                                  and '"tts_route"' in hub_py)
    check("UP2 情感路由跳过中性缓存", "sent_idx == 1 and not _emo_stream" in hub_py)
    # UP3 重基线：P3-1 引入 _emo_use（auto→整文检出值）后，落库表达式由 _emo_req 判别式收敛为 _emo_use
    check("UP2 流式历史真实情感", '(_emo_use if _emo_stream else "neutral")' in hub_py)
    check("UP2 前端路由回读", "tts_route" in hubjs and "streamRoute" in hubjs)
    check("UP2 流式全失败真报错", "未产出任何音频" in hubjs)
    check("UP2 能力声明接口化", "emotionSvcUp()" in hubjs and "lipsyncSvcUp()" in hubjs
                              and "emotionSvcUp()" in ui and "lipsyncSvcUp()" in ui)
    check("UP2 已播句出队释放", "ctrl.buf[ctrl.playIdx-1]=null" in hubjs)
    check("UP2 最近历史内嵌", "voiceRecent" in ui and "loadVoiceRecent" in hubjs and "reSynthesize(r)" in ui)
    check("UP2 语音页英雄卡", "打字，让你的数字人开口说话" in ui and "bd-card bd-hero p-4" in ui)
    check("UP2 字数句数统计", "_smartSplitClient(speakText).length" in ui)

    # UI-P3 语音页（2026-07-07）：流式 auto 情感 / 整段 C2PA 凭证 / 口型定论固化 / 情感映射单源
    check("UP3 流式auto整文检测", "_detected_stream" in hub_py and '_prelude["detected_emotion"]' in hub_py
                                and '_metrics["speak_emotion_auto"] += 1   # 决策①数据' in hub_py)
    check("UP3 流式整段凭证", '"stream": True,' in hub_py
                            and "_pres = await asyncio.to_thread(" in hub_py)
    check("UP3 banner流式恢复", 'x-show="speakEmotion===\'auto\' && speakText.trim().length>=2"' in ui
                              and "speakEmotion==='auto' && speakMode!=='stream' && speakText" not in ui)
    check("UP3 检出情感回读", "streamEmo" in hubjs and "d.detected_emotion" in hubjs)
    check("UP3 首句承诺不吹牛", "speakDetectedEmotion==='neutral'" in ui)
    check("UP3 口型定论文案", "要实时口型直播 → 开播中枢" in ui)
    check("UP3 情感映射单源", hubjs.count("emotionCN(e) {") == 1 and "calm:'平静'" in hubjs
                            and "calm:'😌'" in hubjs)

    # UI-P4 语音/批量页（2026-07-07）：批量示例互通 / 结果卡跨刷新找回（引用持久化+按需取回）
    check("UP4 批量示例互通", "batchExampleFill(ex){" in hubjs and "batchExampleFill(ex)" in ui
                            and "每个示例填 3 行" in ui)
    check("UP4 结果卡引用持久化", "ah_speak_results_v1" in hubjs and "_persistSpeakResults()" in hubjs
                                and "_restoreSpeakResults()" in hubjs)
    check("UP4 音频按需取回", "_ensureResultAudio(" in hubjs and "downloadSpeakResult(it)" in ui)
    check("UP4 找回态可见性", 'x-show="audioSrc || speakResults.length"' in ui
                            and "(!audioSrc && speakResults.length)" in ui)

    # UI-P5（2026-07-07）：找回三态 / 批量成绩单 / 决策①指标曝光
    check("UP5 取回三态", "return 'gone'" in hubjs and "it.fetching=true" in hubjs
                          and "it.dead" in ui and "取回中…" in ui and "已从历史清理" in ui)
    check("UP5 批量成绩单", "batchRunMs" in hubjs and "batchEmoDist()" in hubjs
                          and "batchSavedHist" in hubjs and "批量完成" in ui)
    check("UP5 决策①指标", '"speak_emotion_auto"' in read("avatar_hub.py")
                          and "decision1Hint()" in hub and "决策①" in ui)

    # UI-P6（2026-07-07）：情感词表扩充（带货/安抚/吐槽惊讶）/ 批量行听-改闭环
    _emo = read("emotion_detector.py")
    check("UP6 词表带货语境", "家人们" in _emo and "优惠券" in _emo and "闭眼入" in _emo)
    check("UP6 词表安抚语境", "别怕" in _emo and "有我在" in _emo and "晚安" in _emo)
    check("UP6 词表吐槽惊讶", "离谱" in _emo)
    check("UP6 批量行内试听", "batchPlayLine(line){" in hub and "batchPlayingIdx" in hub
                            and "试听这一行" in ui)
    check("UP6 批量行送润色", "batchLineToVoice(line){" in hub and "送到语音页改情感" in ui
                            and "reSynthesize({text:line.text" in hub)

    # UI-P7（2026-07-07）：批量连播验收 / 「情感不对？」误判反馈闭环
    check("UP7 批量连播验收", "batchPlayAll(){" in hub and "_batchPlayNext(){" in hub
                            and "batchPlayAllOn" in hub and "连播试听" in ui and "停止连播" in ui)
    check("UP7 情感误判反馈", "emotionAutoMiss(){" in hub and "情感不对？" in ui
                            and "api/metrics/emotion_miss" in hub)
    check("UP7 误判指标后端", '"emotion_auto_miss"' in read("avatar_hub.py")
                            and "api/metrics/emotion_miss" in read("avatar_hub.py"))
    # UD1 演进：误判阈值判定从前端 miss/auto>0.2 上收到后端 _d1_block（verdict 单点），前端只展示
    check("UP7 决策①含误判维度", "误判反馈" in ui and "miss_r > 0.2" in read("avatar_hub.py"))

    # UC1 对话页阶段一（2026-07-07）：角色动态化 / 状态点三态 / 重发 / 👍👎 / 串流 id bug
    check("UC1 角色动态化", "loadProfiles" in conv and "converse_profile" in conv
                          and "currentProfileName()" in conv)
    check("UC1 不再硬编码人名", "刘德华" not in conv)
    check("UC1 状态点三态", ".status-dot.warn" in conv and ".status-dot.err" in conv
                          and "pollHealth" in conv and "语音合成离线" in conv)
    check("UC1 失败可重发", "addRetry(" in conv and "重发这条" in conv)
    check("UC1 回复反馈闭环", "attachFeedback(" in conv and "source: 'converse'" in conv
                          and "api/metrics/feedback" in conv)
    check("UC1 串流不再用固定id", "bot-bubble" not in conv and "getElementById('bot-text')" not in conv
                          and 'querySelector(\'.bot-text\')' in conv)

    # UC2 对话页阶段二（2026-07-07）：会话持久化 / 头像 / 打断标记 / 免提状态胶囊
    check("UC2 会话持久化", "converse_session_v1" in conv and "restoreSession" in conv
                          and "_recordTurn" in conv and "语音未保留" in conv
                          and "sessionStorage" in conv)
    check("UC2 头像进气泡", "profileThumbs" in conv and "avatarHtml" in conv
                          and "thumbnail" in conv)
    check("UC2 打断标记", "markInterrupted" in conv and "已打断" in conv)
    check("UC2 免提状态胶囊", "hf-state" in conv and "hfStateText" in conv
                          and "开口即打断" in conv)

    # UH1 历史页阶段一（2026-07-07）：播放器修复（懒挂载+字节端点）/ 单条下载 / 验真
    check("UH1 播放器懒挂载", 'x-if="item._open && !historyMultiSelect"' in ui
                          and "/audio.wav'\" controls" in ui)
    check("UH1 播放器禁回退", "'/api/history/'+item.id+'/audio'\" controls" not in ui)
    check("UH1 单条下载", "⬇ 下载 wav" in ui)
    check("UH1 历史验真", "verifyHistory(item)" in ui and "verifyHistory(item," in hub
                          and "疑似被篡改" in hub)
    check("UH1 深链直达加载", "if(this.tab==='history') this.loadHistory();" in hub
                          and "if(this.tab==='settings')" in hub)
    check("UH1 分组用本地日期", "_loc(Date.now()-86400000)" in hub)
    check("UH1 sing 人话映射", "sing:'唱歌'" in hub and "sing:'🎵'" in hub
                          and "item.emotion!=='sing'" in ui)

    # UH2 四页模式巡检（2026-07-07）：批量情感门控 / 语音页验真 / 对话页 STT 门控
    check("UH2 批量情感门控", ':disabled="!emotionSvcUp()"' in ui and "带情感的行会失败" in ui)
    check("UH2 语音页验真", "verifyHistory(_lastSpeakItem, speakHistId)" in ui
                          and "verifyHistory(item, hid){" in hub and "这条还没落历史" in hub)
    check("UH2 对话页STT门控", "_applySttGate" in conv and "免提暂不可用" in conv
                          and "文字输入仍可" in conv)

    # UR1 克隆页阶段一（2026-07-07）：重名覆盖保护 / 参考音试听修复 / 引擎对比诚实降级 / goTab 统一
    check("UR1 重名覆盖保护", "cloneNameTaken" in hub and "cloneOverwriteOk" in hub
                          and "我确认要覆盖这个角色" in ui
                          and "cloneOverwriteOk&&" not in ui.replace(" ",""))  # 勾选只解锁按钮，不反向隐藏警示条
    check("UR1 参考音试听修复", "cloneResult?.voice_b64" in ui and "已嵌 AI 水印的原录音" in ui
                          and "cloneResult?.audio_b64" not in ui)  # audio_b64 键不存在，绑定它=永不显示
    check("UR1 引擎对比降级", "cloneEngineRecErr" in hub and "cloneEngineRecErr" in ui
                          and "可稍后在角色编辑里手动选引擎" in hub)
    check("UR1 goTab 统一", '@click="tab=' not in ui
                          and "this.goTab('interp')" in hub and "this.goTab('clone')" in hub
                          and "this.tab='voice'" not in hub)  # 用户动作一律走 goTab（hash/最近页/停播），init 同步定 Tab 除外

    # UK1 「去启动」承接闭环（2026-07-07）：体检页服务就绪面板 + 一键启动 + 来源聚焦
    _hubpy = read("avatar_hub.py")
    check("UK1 服务目录端点", '"/api/services/catalog"' in _hubpy and "app_config.SERVICES.items()" in _hubpy)
    check("UK1 服务就绪面板", "loadSvcCatalog" in hub and "svc-row-" in ui and "一键启动" in ui
                          and "重新探测" in ui)
    check("UK1 一键代启+命令兜底", "svcStart(" in hub and "/api/engine/start?name=" in hub
                          and "svcCmd(" in hub and "conda activate " in hub)
    check("UK1 来源聚焦", "goFix(" in hub and "selfcheckFocus" in hub and "你要找的就是它" in ui
                          and "goFix('emotion_tts')" in ui and "goFix('fish_tts')" in hub)
    check("UK1 体检页深链装载", "if(this.tab==='selfcheck')" in hub)

    # UK2 跨页着陆（2026-07-07）：独立页面经 ?fix=服务名 直落体检页服务行
    check("UK2 fix 深链着陆", "_qs.get('fix')" in hub and "this.goFix(_fix)" in hub
                          and "[400, 1500].forEach" in hub)  # 深链场景行未渲染，定位重试两拍
    check("UK2 对话页可点去启动", "/ui?fix=stt" in conv and "_sttGateHintOn" in conv
                          and "文字输入仍可用" in conv)

    # UH3 历史页批量验真（2026-07-07）：整批交付自证 + 行尾角标 + 克隆历史豁免架桥
    check("UH3 批量验真动线", "bulkVerify" in hub and "bulkVrfBusy" in hub
                          and "批量验真" in ui)
    check("UH3 复用单条口径", "await this.verifyHistory(it)" in hub)  # 五档结论只有一处实现
    check("UH3 并发限流", "worker(), worker()" in hub)  # 2 路并发防打爆 Hub（水印解码是 CPU 活）
    check("UH3 摘要与角标", "bulkVrfSummary" in ui and "存疑" in ui
                          and "item._vrf" in ui)  # 不展开也能看到⚠条
    check("UH3 克隆历史豁免架桥", "看我克隆过的声音" in ui and "vaOpen('voice')" in ui)  # 资产面板承接，不重造列表

    # UD1 决策①读数基线化（2026-07-07）：终身计数器分母含 auto 上线前历史，结构性低估——
    #   基线快照 + since 增量是拍板唯一口径，verdict 判定在后端单点实现
    _hub_py2 = read("avatar_hub.py")
    check("UD1 基线快照", "metrics_baseline.json" in _hub_py2 and "_d1_baseline" in _hub_py2
                          and "_d1_block" in _hub_py2)
    check("UD1 metrics 出块", '"decision1"' in _hub_py2 and "_d1_block()" in _hub_py2)
    check("UD1 负差自愈", "计数器比基线还小" in _hub_py2)  # reset/清档后旧基线自动重打，不出负数
    check("UD1 重新起算端点", "api/metrics/rebaseline" in _hub_py2 and "force=True" in _hub_py2)
    check("UD1 前端读 verdict", "d1.verdict" in hub and "metricsData.decision1" in hub
                          and "d1Rebaseline" in hub)
    check("UD1 旧后端优雅回退", "读数基线未生效" in hub)  # Hub 未重启时不给失真数字冒充结论
    check("UD1 面板 since 口径", "decision1.auto_since" in ui and "重新起算" in ui
                          and "decision1.miss_since" in ui)

    # UC3 对话页会话导出（2026-07-07）：手机页→桌面页模式回灌——sessionStorage 关页即清是隐私设计，
    #   「聊出好话术想留底」需要出口；两页共用 /api/converse/session/export 一个后端口径
    check("UC3 对话页会话导出", "exportSession" in conv and "/api/converse/session/export" in conv
                          and "导出记录" in conv)
    check("UC3 空会话防呆", "还没有对话内容" in conv)  # 空导出=下载空文件 dead-end
    _phone = read("static/phone.html")
    check("UC3 两页同一端点", "/api/converse/session/export" in _phone)  # 手机页原有，禁止分家

    # AS5 资产阶段五（2026-07-07）：预设滑条面板 / 导出选项面板 / 资产巡检轻横幅
    _hub_py = read("avatar_hub.py")
    check("AS5 预设滑条内联面板", "vaPresetToggle(a)" in hub and "vaPresetForm" in hub
                              and 'x-model.number="vaPresetForm.pitch"' in ui
                              and "保存为模型默认" in ui)
    check("AS5 预设从角色复制", "vaPresetCopyFrom(" in hub and "从角色复制参数…" in ui)
    check("AS5 预设不再用 prompt", "prompt(" not in hub.split("vaPresetToggle")[1][:2400])
    check("AS5 导出选项面板", "expShow" in hub and "expConfirm()" in hub
                            and "get expEstBytes()" in hub and "导出配置包" in ui
                            and 'x-model="expFace"' in ui and 'x-model="expRvc"' in ui)
    check("AS5 导出口令加密入口", 'x-model="expPw"' in ui and "password=" in hub)
    check("AS5 导出体积预估接口", '"est_bytes"' in _hub_py and "est_face" in _hub_py
                              and "est_rvc" in _hub_py)
    check("AS5 巡检轻横幅", "assetNudge" in hub and "loadAssetHealth()" in hub
                          and "去资产面板" in ui and "nudgeDismiss()" in ui)
    check("AS5 巡检后端接口", "/api/asset_health" in _hub_py and '"orphans": orphans' in _hub_py)
    check("AS5 巡检不扰截图回归", "this._uivr || this.nudgeDismissed" in hub)
    check("AS5 导出按钮不再限有声", 'x-show="drawerP().has_voice" @click="exportPackage' not in ui)

    # AS6 资产阶段六（2026-07-07）：滑条即听 / 导入预览两步流 / 一键清孤儿
    _pkg_py = read("profile_package.py")
    check("AS6 试听吃未保存滑条", "_vaRvcPreview(a, settings)" in hub and "vaPresetPreview(" in hub
                              and "试听当前参数" in ui
                              and 'body.get("settings")' in _hub_py and "_rvc_clean_settings" in _hub_py)
    check("AS6 导入预览接口", "package_peek" in _hub_py and '"exists": pname in _profiles' in _hub_py
                            and "def encryption_kind" in _pkg_py)
    check("AS6 导入两步流前端", "pkgDoPeek()" in hub and "pkgConfirm()" in hub and "pkgReset()" in hub
                              and "解锁预览" in ui and "确认覆盖导入" in ui)
    check("AS6 加密包进得来", 'accept=".zip,.ahpkg"' in ui and "(zip|ahpkg)" in hub)
    check("AS6 一键清孤儿接口", "purge_orphans" in _hub_py and "only 须为文件名数组" in _hub_py)
    check("AS6 一键清孤儿前端", "async vaPurgeOrphans(" in hub and "一键清理孤儿" in ui
                              and "全部清入回收站" in ui)   # AS9 起签名带可选 names（勾选批量清理复用）
    check("AS6 静态口型横幅不扰截图回归", "return !this._uivr && this.staticMouthNormal()" in hub)

    # AS7 资产阶段七（2026-07-07）：回收站清旧 / 导入质量评分 / 试听样本可选
    check("AS7 回收站清旧接口", "older_than_days" in _hub_py and "trash_old_n" in _hub_py
                              and "older_than_days 须为数字" in _hub_py)
    check("AS7 回收站清旧前端", "vaPurgeTrashOld()" in hub and "vaTrashOld" in hub
                              and "清 30 天前" in ui and "assetHealth.trash_old_n" in ui)
    check("AS7 导入预览质量评分", "包内质量评分" in ui and "音色贴合" in ui and "评分偏低" in ui)
    check("AS7 试听样本可选后端", 'body.get("sample_profile")' in _hub_py
                                and "没有绑定参考音" in _hub_py)
    check("AS7 试听样本可选前端", "vaPreviewSample" in hub and "sample_profile" in hub
                                and "试听用声" in ui)

    # AS8 资产阶段八（2026-07-07）：回收站试听 / 自动清理策略 / 绑定跟随试听
    check("AS8 回收站试听接口", "/api/asset_trash/audio" in _hub_py
                              and "只有克隆音支持试听" in _hub_py)
    check("AS8 回收站试听前端", "vaTrashPlay" in hub and "试听回收站声音" in ui)
    check("AS8 自动清理策略后端", "_trash_auto_clean" in _hub_py and "trash_policy" in _hub_py
                                and "须在 {lo}~{hi} 之间" in _hub_py)
    check("AS8 自动清理策略前端", "trashPolicySave" in hub and "自动清理体积阈值MB" in ui
                                and "🤖 自动清理" in ui)
    check("AS8 自动清理钩子齐", all(s in _hub_py for s in
                                ('_trash_auto_clean("clone_soft_delete")', '_trash_auto_clean("rvc_soft_delete")',
                                 '_trash_auto_clean("purge_orphans")', '_trash_auto_clean("hourly")',
                                 'args=("startup",)')))
    check("AS8 绑定跟随试听", "试听用声已切到该角色" in hub
                            and "this.vaPreviewSample=rp.name" in hub)
    check("AS8 唱歌页默认模式不扰截图回归", "!this._songModeTouched && !this._uivr" in hub)

    # AS9 资产阶段九（2026-07-07）：搜索 / 批量多选 / 还原冲突三态 / 清理透明化
    check("AS9 资产搜索前端", all(s in hub for s in ("vaQuery", "vaAssetsF", "vaRvcF", "vaTrashF", "_vaHit"))
                            and "搜索资产" in ui and "没有匹配" in ui)
    check("AS9 回收站批量多选", all(s in hub for s in ("vaSelToggle", "vaTrashSel", "vaTrashSelAll",
                                                      "vaTrashRestoreSel", "vaTrashPurgeSel"))
                              and "还原选中" in ui and "彻删选中" in ui)
    check("AS9 批量彻删后端", "items 须为 {kind,name} 对象数组" in _hub_py)
    check("AS9 孤儿勾选批量清理", "vaOrphanSel" in hub and "选中孤儿" in ui and "清入回收站 (" in ui)
    check("AS9 还原冲突三态后端", "on_conflict" in _hub_py and "原位置已存在同名文件" in _hub_py
                                and 'raise HTTPException(409' in _hub_py)
    check("AS9 还原冲突前端交互", "body.on_conflict=strategy" in hub
                                and "r.status===409" in hub and "覆盖现有文件" in hub)
    check("AS9 清理透明化", all(s in _hub_py for s in ("last_clean_ts", "last_clean_n", "total_cleaned"))
                          and "上次自动清理" in ui and "历史累计" in ui)

    # AS10 资产阶段十（2026-07-07）：角色声音链路体检 + 断链一键修复 + 开播预检联动
    check("AS10 体检接口", "/api/profile_voice_health" in _hub_py
                         and all(s in _hub_py for s in ("voice_unbacked", "voice_lib_missing", "rvc_file_missing")))
    check("AS10 修复接口", "/api/profile_voice_repair" in _hub_py and "backup_voice" in _hub_py
                         and "existed" in _hub_py)
    check("AS10 前端体检加载", "loadVoiceHealth" in hub and "vh(name)" in hub
                             and "vhRepair" in hub and "vhClearRvc" in hub)
    check("AS10 卡片断链徽标", "素材断链" in ui and "vh(p.name).level==='bad'" in ui)
    check("AS10 抽屉修复卡", "一键落盘备份" in ui and "清除失效绑定" in ui and "去换绑模型" in ui
                           and "链路完整" in ui)
    check("AS10 开播预检联动", "key:'voicelink'" in hub and "sev==='break'" in hub)

    # AS11 资产阶段十一（2026-07-07）：形象链路并入体检 + 无备份批量落盘 + 换绑智能跳转
    # 注意：issue 码由 f-string 动态拼（{field}_missing / clear_{field}），断言认生成器而非字面量
    check("AS11 形象体检后端", all(s in _hub_py for s in ('f"{field}_missing"', 'f"clear_{field}"',
                                                          '"idle_video", "待机循环视频"', '"body_video", "口型底视频"',
                                                          'clear_idle_video", "clear_body_video')))
    check("AS11 批量落盘后端", "backup_voice_all" in _hub_py and "backed_n" in _hub_py
                             and "_vh_backup_voice" in _hub_py)
    check("AS11 批量落盘前端", "vhBackupAll" in hub and "全部落盘" in ui)
    check("AS11 清视频引用前端", "vhClearVideo" in hub and "清除失效视频引用" in ui)
    check("AS11 换绑智能跳转", "vhJumpRebind" in hub and "vaOpen(tab, query)" in hub
                             and "vhJumpRebind(editP.orig_name)" in ui)

    # AS12 资产阶段十二（2026-07-07）：搜索命中跨页签 + 徽标直达/一键全修 + 导出导入体检联动
    check("AS12 页签命中数", "get vaTabHits()" in hub and "vaTabHits.voice" in ui
                           and "vaTabHits.rvc" in ui and "vaTabHits.trash" in ui)
    check("AS12 徽标直达", "vhOpenCard" in hub and "vhOpenCard(p)" in ui and 'id="vh-card"' in ui)
    check("AS12 一键全修", "vhFixAll" in hub and "vhAutoFixes" in hub and "一键全修" in ui)
    check("AS12 导出断链预警", "断链会跟着配置包传播" in ui and "先去修复" in ui)
    check("AS12 peek链路研判后端", '"rvc_bound": rvc_bound, "rvc_link": rvc_link' in _hub_py
                                 and '"packed"' in _hub_py)
    check("AS12 peek链路研判前端", "rvc_link==='local'" in ui and "rvc_link==='missing'" in ui)

    # VN1/VL1 命名体系与三库分区（2026-07-09）：显示名去编号 + 角色库三库页签
    #   命名是数据（names.json）不是代码——门禁同时校验数据完整性与代码接线，防止
    #   后续重建音色包时 names.json 被落下、编号名从兜底悄悄回流成显示名。
    import json as _json, re as _re
    try:
        _idx = _json.loads((ROOT / "voice_pack_aishell3" / "index.json").read_text(encoding="utf-8"))
        _ft = {r["spk"]: r.get("title") or "" for r in
               _json.loads((ROOT / "voice_pack_aishell3" / "featured.json").read_text(encoding="utf-8"))}
        _nm = _json.loads((ROOT / "voice_pack_aishell3" / "names.json").read_text(encoding="utf-8"))
        _need = {r["spk"] for r in _idx} - set(_ft)
        _missing = _need - set(_nm)
        check("VN1 命名表全覆盖", not _missing, f"缺 {len(_missing)}: {sorted(_missing)[:5]}")
        _titles = [v.get("title", "") for v in _nm.values()] + list(_ft.values())
        _numbered = [t for t in _titles if _re.search(r"\d{3,}", t)]
        check("VN1 人设名无裸编号", not _numbered, str(_numbered[:5]))
        check("VN1 人设名唯一", len(_titles) == len(set(_titles)))
    except Exception as _e:
        check("VN1 命名表全覆盖", False, f"读取失败: {_e}")
    _hub_vn = read("avatar_hub.py")
    check("VN1 hub 接入命名表", "_vp_names()" in _hub_vn and "_VP_NAMES_FILE" in _hub_vn
                             and 'nm.get("title")' in _hub_vn)   # featured > names > label > 自动名
    check("VN1 命名表进缓存键", "_VP_NAMES_FILE.stat().st_mtime" in _hub_vn)
    check("VN1 档案号退居 tooltip", "'档案 '+s.spk" in ui)
    check("VN2 迁移可回滚", (ROOT / "tools" / "rename_numbered_profiles.py").is_file()
                          and (ROOT / "logs" / "rename_map_20260709.json").is_file())
    check("VL1 后端 lib 派生", '"lib": ("human" if _face and _voice else' in _hub_vn)
    check("VL1 前端三库页签", "libOf(p)" in hub and "get libCounts()" in hub
                            and "setLib(" in hub and "hub_prof_lib" in hub and "🧑 数字人" in ui)
    check("VL1 声音卡试听 CTA", "playVoicePreview(p.name)" in ui and "🎧 试听" in ui)
    check("VL1 照片卡配声 CTA", "🎤 配声音" in ui and "openEdit(p,'edit')" in ui)
    check("VL1 声音卡话筒封面", "libOf(p)==='voice' ? '🎙'" in ui)

    # P2 资源动线统一（2026-07-09）：DFM 中文名直出 / RVC 编号男声别名 / 底视频对账清单
    #   同样是"数据+接线"双校验：别名表/注册表是数据，回填与显示是代码，缺一半都算断。
    try:
        _reg = _json.loads((ROOT / "dfm_workspace" / "dfm_registry.json").read_text(encoding="utf-8"))
        _regf = {e["file"] for e in _reg.get("entries", [])}
        check("P2 DFM 官方3款入registry", {"Bryan_Greynolds.dfm", "Jackie_Chan.dfm",
                                          "Keanu_Reeves_320.dfm"} <= _regf)
        check("P2 DFM registry无裸根目录游离款", all("dfl_official" in e.get("path", "") or
              "community" in e.get("path", "") for e in _reg.get("entries", [])))
    except Exception as _e:
        check("P2 DFM 官方3款入registry", False, f"读取失败: {_e}")
    check("P2 DFM cn回填代理", 'm["cn"] = e.get("cn")' in _hub_vn and "_dfm_registry_cn" in _hub_vn)
    check("P2 DFM 对账横幅中文名", "i.cn ? i.cn+'（'+i.model+'）'" in ui)
    check("P2 DFM 抽屉chip中文名", "dfmCn(drawerP().dfm_model)" in ui and "dfmCn(model)" in hub)
    try:
        _al = _json.loads((ROOT / "rvc_alias_map.json").read_text(encoding="utf-8"))["aliases"]
        _avals = [v["alias"] for v in _al.values()]
        check("P2 RVC 编号男声全有别名", len(_al) == 9 and all("号" in k for k in _al)
                                        and len(set(_avals)) == 9)
        check("P2 RVC 别名无编号残留", not any(_re.search(r"\d", a) for a in _avals), str(_avals))
    except Exception as _e:
        check("P2 RVC 编号男声全有别名", False, f"读取失败: {_e}")
    check("P2 RVC 后端aliases字段", "_rvc_aliases()" in _hub_vn and '"alias": al.get("alias")' in _hub_vn)
    check("P2 RVC 前端别名接线", "rvcLabel(m)" in ui and "rvcAliases" in hub
                               and "a.alias ? a.alias+'（'+a.name+'）'" in ui)
    check("P2 底视频对账清单", (ROOT / "tools" / "avatar_video_labels.py").is_file()
                             and (ROOT / "avatar_videos" / "_labels.json").is_file())

    # HTTP 200（页面可达）
    for pg in ["/static/ui.html", "/static/phone.html", "/static/converse.html", "/static/landing.html", "/static/home.html"]:
        check("HTTP 200 " + pg, http_ok(pg))

if __name__ == "__main__":
    passes = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    all_ok = True
    for i in range(1, passes + 1):
        run()
        failed = [r for r in results if r[1] is False]
        skipped = [r for r in results if r[1] is None]
        passed = [r for r in results if r[1] is True]
        print(f"\n===== PASS {i}/{passes} =====")
        print(f"通过 {len(passed)}/{len(results)}" + (f"（跳过 {len(skipped)}）" if skipped else ""))
        for name, ok, detail in results:
            if ok is False:
                print(f"  [FAIL] {name} {detail}")
            elif ok is None:
                print(f"  [SKIP] {name} {detail}")
        if failed:
            all_ok = False
    print("\n" + ("ALL TESTS PASSED ✅" if all_ok else "SOME TESTS FAILED ❌"))
    sys.exit(0 if all_ok else 1)
