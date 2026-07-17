# 无界 BOUNDLESS · UI 统一与美化 开发文档（持久记忆）

> 本文件是本次 UI 优化工作的**唯一真相 / 持久记忆**。
> 每完成一个阶段：① 回写"已完成内容" ② 回写"测试结果" ③ 记录"下一阶段优化点"。
> 中断或忘记进度时：**先读本文件，从"当前进度"继续**。

---

## 0. 背景与目标

三套界面（客户对话页 `converse/phone.html`、运营后台 `ui.html`、落地/看板 `landing.html` 等）
共用视觉但定位不同，问题：太乱、小字泛滥（9/10/11px）、无主题、等待无反馈、报错糊首屏、品牌不统一。

**核心结论**：项目已有成熟令牌文件 `static/brand.css`（"全站单一真相"，含 `--bd-*` 配色/圆角/阴影/字阶 + 品牌架构），
但 `ui.html` 另起炉灶用了 `hub-*` 配色和散落魔法值。**美化 = 让好底子真正落地 + 补"字阶/组件"两层，而非推倒重来。**

品牌架构（沿用 brand.css）：母品牌 `无界 BOUNDLESS`；产品线 `幻颜 FaceX · 幻声 VoiceX · 幻影 LiveX · 通译 LingoX · 智聊 ChatX`。

---

## 1. 设计令牌规范（目标值）

- 配色：统一用 `--bd-*`（主色 `#4f7aff`，幻紫 `#a855f7`，成功 `#34d399`，提示 `#fbbf24`，危险 `#f87171`）。
- 字阶（**最小 12px**）：Hero 30 / H1 22 / H2 18 / Body 15 / Small 13 / Label 12。
- 间距：4/8/12/16/24/32。圆角：控件12 / 卡片16 / 大面板20 / 胶囊30。
- 动效：hover 150ms、弹层 250ms；耗时操作用进度条而非无限脉冲。
- 徽标"离线/未启用"用中性灰，不用红。

---

## 2. 阶段计划

| 阶段 | 内容 | 主要文件 | 状态 |
|---|---|---|---|
| P0 | 令牌补全：brand.css 增补字阶(12px下限)/间距/动效 token | `brand.css` | ✅ 完成 |
| P1 | 后台接入唯一真相：`hub-*` → `bd-*`，强化可读性层（字号下限12） | `ui.html` | ✅ 完成 |
| P2 | 组件统一：按钮/卡片/徽标/进度条/Toast，替换无限脉冲为进度 | 全站 | ✅ 完成 |
| P3 | 报错收口 + Demo 模式（侧边导航改造延后，见 §7）| `ui.html` | ✅ 完成 |
| P4 | 客户端&落地页：品牌头统一/accent 对齐（角色卡去参数化延后，见 §7）| `converse/phone/landing.html` | ✅ 完成 |
| 终验 | 跨页/跨端跑通测试 ≥2 次，全部通过 | 全部 | ✅ 完成（30/30 ×2）|
| P5 | 后台侧边导航重构（桌面侧边栏+移动横向）+ Tab 持久化 + 懒加载预埋 | `ui.html` | ✅ 完成（39/39 ×2）|
| P6 | iframe 懒加载（看板/同传/预览）+ 首屏 Hero 强化（价值主张+主CTA）| `ui.html` | ✅ 完成（47/47 ×2）|
| P7 | 侧栏可折叠 + 命令面板 Ctrl/⌘+K + `.bd-btn` 按钮组件基座 | `ui.html`/`brand.css` | ✅ 完成（57/57 ×2）|
| P8 | `.bd-card` 卡片收敛（32× 标准卡片串归一，零视觉变化）+ `.bd-btn-success` 变体 | `ui.html`/`brand.css` | ✅ 完成（64/64 ×2）|
| P9 | 可访问性(a11y)：键盘 `:focus-visible` 焦点环 + `prefers-reduced-motion` 降级 + `.bd-card-title` 标题收敛(26×) | `ui.html`/`brand.css` | ✅ 完成（70/70 ×2）|
| P10 | 客户端跨设备白标联动：中央三元组令牌 `--bd-acc-rgb`（brand.js 服务端+本地同步），phone/converse 主色统一跟随 | `brand.css`/`brand.js`/`phone/converse.html` | ✅ 完成（77/77 ×2）|
| P11 | 客户端可读性收敛：phone 追加"可读性层"，字号下限抬到 11px（角标 10px），消灭 8~10px 杂乱 | `phone.html` | ✅ 完成（81/81 ×2）|

---

## 3. 测试方法

1. **静态校验**（每阶段必做）：用搜索确认 token 落地、无 <12px 残留、颜色统一、关键结构存在。
2. **渲染校验**（里程碑 P1/P3/P4/终验）：本地静态服务 `python -m http.server` + 浏览器加载页面截图，肉眼核对视觉层级与无报错糊屏。
3. 失败即修复并回写；终验阶段连续跑通 2 次，跑不通则新增一条针对性测试直到全绿。

---

## 4. 当前进度（持续更新）

- [开始] 已读 brand.css / ui.html 头部；确认"可读性增强层"已存在，P1 改为强化该层 + 集中映射颜色（更优方案）。
- [P0 ✅] brand.css 增补：字阶(--bd-fs-h2/label，xs 提到 12px) + 行高 + 间距阶梯(--bd-sp-*) + 控件高 + 动效(--bd-dur-*) + 通用工具类(.bd-fs-*/.bd-progress/.bd-toast/.bd-badge)。
- [P1 ✅] ui.html：tailwind hub 色集中对齐 brand（blue→品牌蓝三元组 79 122 255，green/red/orange/purple/yellow/bg/card/border/text 全部对齐 --bd-*）；body/输入/滚动条配色统一；可读性层下限提到 12px；JS 默认 brand 主色与重置回退同步为 79 122 255。
- [P2 ✅] ui.html：Toast 统一为 `.bd-toast`（支持 ok/warn/err/info 图标与配色，error 停留 4.5s）；新增体检进度反馈——`probeElapsed` 计时器 + `probeStageText` 阶段文案 + `.bd-progress.indeterminate` 渐变进度条，替代向导第 2 步的单一"体检中…"脉冲。
- [P3 ✅] ui.html：新增**演示模式 Demo Mode**（localStorage `hub_demo` 持久化 + 顶栏开关 + `toggleDemo()`）；开启后隐藏 WS 断连条、运维告警入口(opsAlerts)、warn/info 级用户告警，首屏零噪音；error 级用户告警仍保留（安全考虑）。
- [P4 ✅] phone.html 默认 accent `--acc-rgb` 88 166 255 → 79 122 255（与 brand.css/后台同源）；converse.html 已用 `--accent:#4f7aff` 无需改；三端 logo/品牌色统一。
- [终验 ✅] 回归脚本 30/30 连续 2 次通过。**P0~P4 全部完成。**
- [P5 ✅] ui.html 后台导航重构：①桌面（≥lg）**左侧分组侧边栏**（角色/创作/运营，sticky 可滚，active 左边框+底色高亮），把原一长排 12 个横向 Tab 改为分区竖排；②移动端（<lg）**保留原横向滚动条**（不牺牲触屏体验）；③新增 `goTab(id)` 统一切换：更新 `tab` + URL hash + `localStorage('hub_tab')` 持久化；④`init()` 无 hash 时恢复上次 Tab；⑤新增 `visitedTabs` 记录已访问（为内容懒加载预埋）。回归 39/39 ×2，浏览器实测桌面侧边栏 + 移动横向 + 切换/持久化均正常。
- **先行确认**：实施前已确认侧边栏/懒加载/`goTab`/持久化在代码中均不存在（grep 无匹配），非重复开发。
- [P6 ✅] ui.html：①**3 个 iframe 懒加载**——看板 `/dashboard`、同传 `:7900`、实时预览放大 `/realtime/dual_preview`，原先无论在哪个 Tab 都会在启动时各加载一个完整子页面；改为 `:src` 按 `visitedTabs`/`videoModal` 门控，首访才加载（实测：进站时 dashboard iframe src 为空，访问后变 `/dashboard`）。②**首屏 Hero 强化**——无出镜角色时显示价值主张「克隆你的声音与形象，几分钟上线数字人」+ 三步流程 + 单一主 CTA「🚀 三步上手向导」；有出镜角色时放大头像/角色名(bd-fs-h2)，主 CTA「💬 开始对话」加辉光 + 次 CTA「📡 一键开播」。回归 47/47 ×2。
  - **更优方案（再优化）**：原 §7 计划是把 12 个面板逐个 `x-if` 懒加载（风险高：含轮询/canvas 的面板首访才存在元素易崩）。深度思考后发现**真正的启动开销是 3 个 iframe 子页面**，其余面板只是廉价 HTML 表单。于是改为「只懒加载 3 个 iframe」——用 `:src` 门控（元素留在 DOM，仅推迟 fetch），拿到几乎全部性能收益且**零结构性风险**，比逐面板 x-if 更安全更省。
- **先行确认(P6)**：实施前 grep 确认 `x-if="visitedTabs` / 懒加载 iframe 门控均不存在，非重复开发。
- [P7 ✅] ①**侧栏可折叠**：`sidebarCollapsed`（localStorage `hub_sidebar_collapsed` 持久化）+ `toggleSidebar()` + 顶部 «/» 开关；折叠为 w-14 图标条（隐藏分组/标签，居中图标，title 悬浮），展开 w-52。②**命令面板 Ctrl/⌘+K**：`openCmd/cmdResults/cmdRun/cmdMove/cmdEnter` + `cmdItems`（Tab 跳转 / 角色搜索打开对话 / 动作：向导·演示模式）；↑↓ 选择、↵ 执行、Esc 关（已并入 escClose）、? 帮助卡新增条目。③**`.bd-btn` 按钮基座**（brand.css，additive）：`.bd-btn` + `-primary/-ghost/-danger`，已示范应用到 Hero 三个 CTA。回归 57/57 ×2，浏览器实测折叠态/命令面板过滤+高亮/按钮样式均正常。
  - **更优方案（再优化）**：命令面板复用既有 keydown 总线与 `escClose()` 分层关闭，未新增全局监听；`.bd-btn` 采取"先建基座 + 示范应用"而非一次性全量替换 5500 行里的按钮，零回归风险、可渐进推广。
- **先行确认(P7)**：grep 确认 `sidebarCollapsed/cmdShow/.bd-btn/Ctrl+K` 均不存在，非重复开发。
- [P8 ✅] **卡片收敛 `.bd-card`**：brand.css 新增 `.bd-card`（像素级复刻后台标准卡面：底 #141a24 / 边 1px #262f42 / 圆角 12px(rounded-xl) / `0 1px 3px` 主区微投影 + 边框/投影过渡，padding 仍由调用处 p-3/p-4/p-5 控制）；ui.html 把 **32 处** `bg-hub-card border border-hub-border rounded-xl` 一次性归一为 `bd-card`（replace_all，精确串匹配，零视觉变化）。同时 brand.css 新增 `.bd-btn-success`（保留绿色"创建/成功"语义、统一焦点/禁用/动效基座，供新建标准 CTA）。回归 64/64 ×2，浏览器实测卡片外观与之前一致、命令面板(Ctrl+K)/侧栏折叠展开均未回归。
  - **精确收敛、不误伤**：仅归一"标准内容卡"这一种组合；刻意保留 17 处异类——弹窗 `rounded-2xl`(4)、下拉浮层 `rounded-lg`、黄/蓝**异色描边**警示/浮卡(`border-hub-yellow/40`、`border-hub-blue/40`)、以及 header/侧栏/移动 nav 的结构性 `bg-hub-card`（带 border-r/border-b）。grep 逐条核对，确认未误并。
  - **更优方案（再优化 / 反思）**：原 §7 把 **P8-a「`.bd-btn` 全量推广」列为首选**。深度核查现有按钮后**推翻该优先级**：站内按钮要么是 `text-[11px] px-2.5` 紧凑工具 pill（强行套 `.bd-btn` 反而撑大、降低密度），要么是 `w-full rounded-lg` 且带**语义色**（green=创建 / indigo=生成 / blue=确认）的整条 CTA（套 `.bd-btn-primary` 会丢失语义色与圆角）。结论：**`.bd-btn` 应作为"新建独立 CTA"的标准（Hero 已示范），而非回头硬替换既有按钮**——硬替换是反模式、会降低清晰度。于是 P8 改以**卡片收敛**为主交付（机械、安全、真维护收益），按钮侧仅补 `.bd-btn-success` 变体备用，不做全量替换。
- **先行确认(P8)**：grep 确认 `.bd-card` 在 brand.css 不存在、`bg-hub-card border border-hub-border rounded-xl` 恰好 32 处，非重复开发。
- [P9 ✅] **可访问性 + 卡片标题收敛**：①brand.css 新增键盘 **`:focus-visible` 焦点环**（`2px solid var(--bd-acc)` + 2px 偏移，仅键盘可见、鼠标点击不出现；选择器含元素名→特异性高于散落 `.outline-none`，确保输入框键盘聚焦也有环）——与 P7 命令面板的键盘流形成闭环；②新增 **`prefers-reduced-motion` 降级**（系统开启"减少动态"时把动画/过渡降到 ~0，含进度条无限动画）；③新增 **`.bd-card-title`** 收敛 ui.html **26 处** `text-hub-blue font-bold text-sm` 卡片标题（24 个 `<h2>` + 2 个弹窗 `<h3>`）。回归 70/70 ×2，浏览器实测：标题颜色 `rgb(79,122,255)`/14px/700 与改前**像素级一致**，且改 `--hub-blue` 时标题**仍实时联动**（→红→还原），focus-visible 与 reduced-motion 规则已在 CSSOM 正确解析。
  - **关键防回归（深度思考）**：`text-hub-blue` 走 `--hub-blue`，而后台**品牌取色器**会运行时改 `--hub-blue` 给标题换色。若把 `.bd-card-title` 硬绑 `var(--bd-acc)`，出厂重置(line 6020 只重置 --hub-blue 不重置 --bd-acc)后标题会与全站脱节。故刻意让 `.bd-card-title` 绑 `rgb(var(--hub-blue, 79 122 255))`——**完全复刻原联动+重置语义**，非后台页用回退值。
  - **更优方案（再优化 / 推翻原建议）**：上轮我建议的下一步是 P9「卡片头 `.bd-card-head` 收敛」。本轮核查 26 个卡头后发现它们只是**纯 `<h2>` 文字**（非"图标+标题+动作位"容器），单纯抽类价值很薄、还要处理 purple/orange 语义色与 mb-* 间距变体。于是**把更高价值、同样纯 additive 的可访问性收敛并入 P9**：键盘焦点环 + 减少动效是**用户可感知**的专业度提升，且与命令面板键盘流契合；`.bd-card-title` 仍做但仅作轻量令牌化（零视觉变化）。
- **先行确认(P9)**：grep 确认 `focus-visible`/`prefers-reduced-motion`/`.bd-card-title` 全站 CSS 均不存在，非重复开发。
- [P10 ✅] **客户端跨设备白标联动**：核查发现"客户端接入设计系统"其实**大半已完成**（phone/converse 早已 `<link brand.css>`、配色 hex 已与 brand 令牌对齐、P9 的 a11y 焦点环/减少动效已自动继承）。真正的**缺口**是白标换色到不了客户页：①`brand.js` 只同步 `--bd-acc`（`rgb()`/hex），而 phone 用平行的 `--acc-rgb`（三元组）；②phone 仅在加载时读**旧 localStorage 单键** `avatarhub_brand`——而客户手机是**另一台设备**，其 localStorage 为空→始终默认蓝，收不到运营商在后台设的白标色；③converse 干脆**硬编码** `--accent:#4f7aff` 无任何桥接。**方案（更优）**：新增**中央三元组令牌 `--bd-acc-rgb`**（brand.css 定义 + brand.js 在 `applyColor` 同步、`reset` 清除——而 brand.js 本就从**服务端 `/api/brand`**+本地拉取，天然跨设备），令 `phone --acc-rgb=var(--bd-acc-rgb)`、`converse --accent=rgb(var(--bd-acc-rgb))`。`#4f7aff`==`rgb(79 122 255)` 精确相等→**默认零变化**，但换色后**跨设备**直达两个客户页。回归 77/77 ×2。浏览器实测：phone/converse 默认 `rgb(79 122 255)` 不变；直接置 `--bd-acc-rgb=255 0 0` → 两页主色实时变红→还原；converse 已继承 a11y 焦点环；brand.js 线上文件确含 set/reset `--bd-acc-rgb`（开发时浏览器缓存旧 js 属正常，真实用户取新 js）。
  - **更优方案（再优化 / 修正认知）**：原 §7「P10-A 客户端接入设计系统」预期是"把 `.bd-card`/`.bd-btn` 推到客户端"。核查后发现 phone 已有成熟自有组件（`.pcard`/`.hbtn` 等，且已对齐 brand 配色），强推通用组件类反而是 P8 同款反模式（撑大、丢语义）。于是把 P10 聚焦到**真正缺失且高价值**的"跨设备白标联动"——以单个中央令牌打通三端换色，零回归。客户端**可读性（<12px 小字）**与组件级收敛改列下阶段，因需逐组件设计判断（聊天 UI 的微标签强行 12px 会破紧凑布局）。
- **先行确认(P10)**：grep 确认 `--bd-acc-rgb` 在 brand.css/brand.js 均不存在；converse 无任何品牌桥接、phone 仅旧单键桥——非重复开发。
- [P11 ✅] **客户端可读性收敛**：盘点 phone.html 共 8px×1（`.preg` 角标）、9px×1（`.pcard .pq` 音色/自然度轴）、10px×约20、11px×37；converse 仅 4 处 11px（已达下限，无需动）。**方案**：沿用后台 P1 验证过的"**可读性层**"策略——在 phone.html `<style>` 末尾追加一段**集中、可整段回退**的覆盖层，把"需阅读"的信息类文字下限统一抬到 **11px**，角色卡可瞥视角标 `.preg` 保守抬到 10px（不破角标布局）。下限取 11px 而非后台的 12px：移动高 DPI + 聊天 UI 密集，11px 更稳且已彻底消灭 8~10px 杂乱。回归 81/81 ×2。浏览器实测（注入样例角色卡）：`.pq` 9→11、`.pq.pct` 10→11、`.preg` 8→10、`.pbadge` 10→11、`.ms-group-label`/`#streamBadge` →11，`.pname` 仍 12 不变；样例卡 176×63px、音色轴**单行未换行无溢出**，"相似度81% 自然81%" 明显比原 9px 易读。
  - **更优方案（再优化）**：相比"逐条改 ~20 个 font-size 声明"，采用**单一追加层**：①改动是 `<style>` 末尾**一个连续块**，审阅/回退极简；②**不触碰**散落在 CSS 早段、与工作区那批无关改动（免提/Ditto/STT）交织的原始行，后续**选择性暂存更干净**；③下限可在一处统一微调。代价是用了 `!important`（但语义明确，就是"可读性下限覆盖层"，与后台 P1 同源）。
- **先行确认(P11)**：grep 确认 phone/converse 无"可读性/readability"层、无 `font-size:11px !important`，非重复开发。
- 测试环境：本机无系统 python，使用仓库内 `.venv_launcher\Scripts\python.exe -m http.server 8099` 起静态服务，浏览器加载 `http://localhost:8099/static/<page>` 截图核对。

---

## 5. 决策与更优方案记录（深度思考）

- D1：不逐个替换 `text-[10px]`，改为强化 ui.html 既有"可读性增强层"（纯 CSS 覆盖、可整段回退）+ 在 tailwind.config 把 `hub` 色指向 `--bd-*`。理由：低风险、零布局位移、可回退。
- D2：品牌不新造，沿用 brand.css 既定母品牌+产品线命名。

---

## 6. 测试结果记录（持续追加）

（每阶段测试后在此追加：阶段 / 时间 / 用例 / 结果 / 修复）

- **P0** 2026-06-24：CSS 括号平衡(28/28 OK)；新 token 与工具类 grep 全部命中。✅ 通过。
- **P1** 2026-06-24：静态——无残留旧主色(88 166 255 仅存于调色板预设与已改默认值)；text-[9/10/11px] 共 286 处由可读性层统一上调至 ≥12px（采用覆盖层策略，不逐个改）。渲染——浏览器加载 ui.html 200，截图确认 logo/激活Tab/主按钮均品牌蓝、状态徽标绿色、版式可读。✅ 通过。
- **P2** 2026-06-24：渲染——经 Alpine 注入将向导推进到体检态，截图确认渐变进度条 + "正在合成语音样本…" + "已用 12s · 约 20~40s" 正常显示，替代了脉冲文字。✅ 通过。
- **P3** 2026-06-24：渲染——注入 2 条 ops 告警 + 1 条 user warn + WS 断连；演示模式关→三类噪音可见(运维2 徽标/黄条/橙条)，开→全部隐藏、仅"演示中"绿徽标。✅ 通过。
- **P4** 2026-06-24：渲染——phone.html 200，截图确认"按住说话/发送/logo"均品牌蓝渐变，标题"幻影 LiveX · 无界科技 BOUNDLESS"。✅ 通过。
- **终验** 2026-06-24：新增回归脚本 `test_ui_optimization.py`（30 条断言，覆盖 P0~P4 + 4 页 HTTP 200）。连续跑 2 次：PASS 1 = 30/30，PASS 2 = 30/30，**ALL TESTS PASSED ✅**。
  - 复跑方式：`.venv_launcher\Scripts\python.exe -m http.server 8099`（另开）→ `.venv_launcher\Scripts\python.exe test_ui_optimization.py 2`。
- **P11** 2026-06-24：回归扩至 **81 条**（新增 4 条：phone 可读性层标记、字号下限 11px、`.preg` 角标 10px、层覆盖音色轴选择器）。连续跑 2 次：81/81、81/81，**ALL TESTS PASSED ✅**。渲染——CDP 注入样例角色卡，计算字号 `.pq/.pq.pct/.pbadge/.ms-group-label/#streamBadge`=11px、`.preg`=10px、`.pname`=12px 不变；卡片 176×63px、音色轴单行不溢出。
- **P10** 2026-06-24：回归扩至 **77 条**（新增 7 条：`--bd-acc-rgb` 令牌、brand.js set/reset 三元组、phone/converse 跟随中央令牌、phone 旧单键桥已移除、converse 无硬编码主色；并更新 P4 两条断言为中央令牌形态）。连续跑 2 次：77/77、77/77，**ALL TESTS PASSED ✅**。渲染——phone/converse 默认蓝不变；CDP 置 `--bd-acc-rgb` 两页主色实时联动并可还原；converse 继承 a11y 焦点环；brand.js 线上含 set/reset。
- **P9** 2026-06-24：回归扩至 **70 条**（新增 6 条：`.bd-card-title` 定义且绑 `--hub-blue`、标题令牌 26 处、旧标题串清零、purple/orange 语义标题保留、`input:focus-visible` 焦点环、`prefers-reduced-motion` 媒体查询）；并修正 P8「bd-card 计数」断言为 `count(bd-card)-count(bd-card-title)==32`（因 `bd-card-title` 含子串 `bd-card`）。连续跑 2 次：70/70、70/70，**ALL TESTS PASSED ✅**。渲染——标题像素级一致 + 品牌取色器联动正常 + a11y 规则 CSSOM 已解析。
- **P8** 2026-06-24：回归扩至 **64 条**（新增 7 条：`.bd-card` 定义/含投影、`.bd-btn-success`、ui 中 bd-card 恰 32 处、旧标准卡片串清零、模态 rounded-2xl 保留 ≥3、黄/蓝异色卡保留）。连续跑 2 次：64/64、64/64，**ALL TESTS PASSED ✅**。渲染——浏览器实测：标准卡片（角色库/三步卡/新建角色/配置包导入）背景边框与改前一致；Ctrl+K 命令面板正常弹出+分组+键位提示；侧栏 »/« 折叠展开 w-14↔w-52 正常。
- **阶段5–10（UI 回归门禁）** 2026-06-27：见 §8。
  - 阶段5：reduced-motion 补进 `ui.html`；像素回归矩阵扩入 ui.html（默认+窄屏）。
  - 阶段6：`ui_visual_regress.py` 新增红色高亮差异图 + 健壮退出码(0/1/2)；接入 `gate.py` **Tier U**（契约层 `test_ui_optimization.py` 离线恒跑 + 像素层 `--online` 跑）；`test_ui_optimization.py` 的 HTTP 项改「连不上即 `[SKIP]`」。实测 `gate.py --online`：Tier U ✓（契约 77/81 跳过 4、像素 12/12 通过）。
  - 阶段7：逐 Tab 覆盖（克隆/唱歌/设置）。踩坑：URL hash 直达 Tab 会截到默认页（hash 在 init 多个 await 后才赋值）→ 改临时副本设 Alpine 初始 `tab`。
  - 阶段9（跨机基线）：按机器指纹分目录 `baseline/windows-edge149/`；新增 `capture_settled()` 采集即等稳定；钉死 `?profile=`；新机无基线→退出码 2 跳过。**关键反复**：先加渲染归一化参数反而引入抖动（device-scale-factor 触发头像二次解码、font-hinting 放大回流）→ 全删；voice/batch 因慢漂移剔除。最终 **11 张连续 2 次复跑（间隔 10s/35s）全 PASS（最大 0.21%）**，新机模拟退出码 2。
  - 阶段10：本节（§8）落文档；`.gitignore` 注明分目录基线入库。

## 7. 下一阶段优化点（待实施 / 需产品确认）

- ~~后台侧边导航改造~~ → **P5 已完成**。
- ~~Tab 内容懒加载 / 首屏 Hero~~ → **P6 已完成**（懒加载收敛为 3 个 iframe 门控；Hero 价值主张+主CTA）。
- ~~P7-a 侧栏可折叠 / P7-b 命令面板 / P7-c .bd-btn 基座~~ → **P7 已完成**。
- ~~P8-b `.bd-card` 卡片收敛~~ → **P8 已完成**（32× 标准卡片串归一为 `.bd-card`，零视觉变化）。
- ~~P8-a `.bd-btn` 全量推广~~ → **已评估并放弃（反模式）**：见 §4「P8 更优方案/反思」。`.bd-btn` 定位为"新建独立 CTA 标准"（Hero 已示范、`.bd-btn-success` 已备），既有紧凑工具 pill 与语义色整条 CTA **不回头硬替换**。
- ~~P9 卡片头/可访问性收敛~~ → **P9 已完成**（深度思考后由"卡头别名"升级为"a11y 焦点环 + 减少动效 + 标题令牌化"，价值更高、同样零回归）。
- ~~P10-A 客户端接入设计系统~~ → **P10 已完成**（核查后聚焦为"跨设备白标联动"中央令牌 `--bd-acc-rgb`；a11y 已随 brand.css 自动继承）。
- ~~P11-A 客户端可读性收敛~~ → **P11 已完成**（phone 可读性层，下限 11px；至此三端"小字太多"诉求基本根治）。
- **P12（候选 A，推荐）键盘可达性补全**：给后台侧栏 Tab/卡片操作补 `role`/`aria-label`/方向键导航，让 P9 焦点环覆盖**全键盘路径**，与命令面板成完整无鼠标闭环。低风险、a11y 价值高。
- **P12（候选 B）空状态/骨架屏一致化**：抽 `.bd-skeleton` + 统一空状态，替换各面板零散"加载中/暂无数据"，减少等待焦虑（呼应最初"等待无提示"）。
- **P12（候选 C）`.bd-card-head` 卡头/分节收敛**（后台）：标准卡内重复"图标+标题+右侧动作"可抽组件，进一步降重复；ROI 中、低风险。
- **客户端角色卡去参数化（需产品确认，仍挂起）**：phone.html 卡片"相似度81%/自然81%"对终端用户是噪音但兼具"金标"信任。需产品定取舍。涉及 1541-1552 行动态生成。
- **客户端角色卡去参数化（需产品确认，仍挂起）**：phone.html 卡片"相似度81%/自然81%"对终端用户是噪音但兼具"金标"信任。需产品定：是否对终端用户隐藏，或仅留"金标"。涉及 1541-1552 行动态生成。
- **可选：剩余面板懒加载**：若首屏仍偏重，再评估对 logs/history 等非 iframe 重面板做 `x-if` 懒挂载（先确认轮询/初始化健壮性）。当前 ROI 低。
- **客户端（phone/converse）命令面板与品牌细节对齐**：后台体验成熟后，可把命令面板/快捷操作思路下沉到客户端。
- ~~UI 回归门禁（可视化基线 / 接入发布 / 跨机策略 / 落文档）~~ → **阶段5–10 已完成，见 §8。**

---

## 8. UI 回归门禁（阶段 5–10 · 可视化基线 + 发布闸门）

> 在 §3「静态校验 + 肉眼渲染校验」之上，补一套**自动化双层 UI 门禁**，把前面所有
> UI 改造「锁死、防回退」。改完 UI 必须跑；改对了就采纳新基线，改错了门禁会拦下。

### 8.1 两层互补

| 层 | 脚本 | 抓什么 | 依赖 |
|---|---|---|---|
| 契约层 | `test_ui_optimization.py` | **字符串契约**：token 落地 / 无 <下限 小字 / 关键结构存在（81 条断言） | 纯离线读文件；HTTP 200 项有服务才测、无则 `[SKIP]` |
| 像素层 | `ui_visual_regress.py` | **渲染后**才暴露的问题：右栏被裁切 / 抽屉覆盖 / 布局位移 | 无头 Edge + Pillow + Hub 在线 |

### 8.2 怎么跑

```bash
# 契约层（离线，秒级）
.venv_launcher\Scripts\python.exe test_ui_optimization.py 1

# 像素层（需 Hub 在线，默认 http://127.0.0.1:9000）
.venv_launcher\Scripts\python.exe ui_visual_regress.py                # 与本机基线对比
.venv_launcher\Scripts\python.exe ui_visual_regress.py --update-baseline  # 采纳当前为新基线
.venv_launcher\Scripts\python.exe ui_visual_regress.py --list-baselines   # 列出已有机器基线

# 一把过（推荐）：测试门禁 Tier U 自动串起两层
gate.bat --online            # = python gate.py --online（Tier U 含像素层，需 Hub 在线）
gate.bat                     # 离线：Tier U 只跑契约层，像素层自动跳过
```

### 8.3 退出码语义（`ui_visual_regress.py`）

| 码 | 含义 | 门禁动作 |
|---|---|---|
| 0 | 全部通过 / 基线已更新 | 放行 |
| 1 | **稳定帧**超阈差异 = 真实视觉回归 | **阻断发布** |
| 2 | 不可用/跳过（无 Edge / Hub 未起 / **本机尚无基线** / 采集环境异常） | 跳过，不阻断 |

> Tier U 把 1 视为失败、2 视为跳过。失败时在 `ui_snapshots/diff/` 生成**红色高亮差异图**（变化标红、其余压暗灰底），一眼定位回归位置。

### 8.4 截图矩阵（11 张，机器=`<平台-edge大版本>`）

- **phone（5）**：`1280x600`(原裁切高度·回归重点)、`1180x470`(极矮屏)、`1280x860`(正常)、`390x844`(手机单栏)、`drawer 1200x640`(「更多」抽屉覆盖层)。
- **ui（3）**：`1440x900`、`1280x800`、`820x900`(窄屏响应式)。
- **ui 逐 Tab（3）**：克隆 / 唱歌 / 设置。
- **刻意排除**（保证零误报）：dashboard/stream/interp/history/logs/selfcheck（iframe/实时/流式）、batch（大 textarea 文案/光标漂移）、voice（异步内容 ~20s 尺度整体位移）。**可靠性优先于覆盖面。**

### 8.5 健壮性机制（踩坑后的最终方案）

1. **冻结装饰动画**：截图加 `--force-prefers-reduced-motion`，触发页面 reduced-motion 降级（头像呼吸/光环静止）。为此把 reduced-motion 也补进了 `ui.html`（阶段5）。
2. **钉死头像**：phone 用 `?profile=刘亦菲` 固定出镜角色——否则默认取 `profiles[0]`/localStorage 会跨次漂移，整块头像变红误报。
3. **采集即等稳定（capture-until-settled）**：一次性 `--screenshot` 会落在异步内容（角色卡/服务状态/字体回流）的不确定加载相位上。`capture_settled()` 连续截图直到相邻两帧基本一致才采纳；**比对时：稳定帧超阈=真实回归(阻断)，未收敛帧超阈=判抖动跳过(不阻断)**。
4. **按机器指纹分目录基线**：像素基线天然含本机字体渲染+Edge 版本，换机直接比会满屏误报。故基线存 `ui_snapshots/baseline/<平台-edge大版本>/`（如 `windows-edge149/`）。**新机首次跑→该目录不存在→退出码 2 跳过**并提示 `--update-baseline` 自建，绝不误报。
5. **临时副本设状态**：抽屉态 / 逐 Tab 经临时副本（注入 `show` 类 / 改 Alpine 初始 `tab`）直连渲染——比 URL hash 可靠（hash 在 init 的多个 await 之后才赋值，截图可能早于它）。
6. **不叠加渲染归一化参数**：实测 `font-render-hinting/disable-lcd-text` 会扰动时序放大异步竞态、`force-device-scale-factor=1` 触发头像图二次解码竞态——跨机差异已由「分目录基线」解决，故全删，只留 reduced-motion。

### 8.6 改了 UI 之后的标准动作

1. 改完先跑 `gate.bat --online`（或单独跑像素层）。
2. 若像素层报 FAIL：看 `ui_snapshots/diff/<名>.png` 高亮图。
   - **是预期改动** → `ui_visual_regress.py --update-baseline` 采纳为新基线（连同 git 提交 `ui_snapshots/baseline/<机器>/`）。
   - **不是预期** → 修复回归后再跑。
3. 新增/调整截图项 → 改 `SHOTS`，重建基线，连续复跑 2 次确认零抖动再提交。

### 8.7 入库约定（.gitignore）

- **入库**：`ui_snapshots/baseline/<机器指纹>/*.png`（基线）。
- **忽略**：`ui_snapshots/current/`（当前截图）、`ui_snapshots/diff/`（高亮差异图）、`static/_regress_*.html`（临时副本）。
