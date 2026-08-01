# 坐席「工作目标」入口与体验优化方案（P17 · 已实施首版）

> 2026-07-28 调研产出；同日落地 A/B/C/D/E 首版（见文末 §6）。
> 背景：运营反馈「坐席端工作目标/工作计划的按钮找不到」。
> 本文＝入口现状考古 + 开发/市场/坐席/美术四视角分析 + 分包优化方案 + 度量口径。
> 配套上下文：goals 引擎侧见 `src/companion/goals/`（P1-P16 已闭环），本文只谈**坐席端 UI**。

---

## 0. 按钮到底在哪（为什么找不到）

**唯一坐席操作入口**（网页端与桌面壳同源）：

```
统一收件箱 → 选中一个会话 → 右栏「业务助手」
  → 切到第二个 tab「客户&关系」（默认停在「回复台」）
    → 往下第三张卡「🎯 工作目标」（在「客户信息」「关系进展」之下）
      → 空态时才出现「设定目标」按钮
```

- 组件：`shared/copilot/components/cp-goal.js`（web 与 `desktop/renderer/` 双份同源），
  宿主卡在 `src/web/templates/unified_inbox.html` L777-784（`data-cp-card="goal"`）。
- 管理端另有 `/ops-overview` 的「🎯 营销目标」聚合卡（漏斗读数/就绪/校准），**只读不是操作入口**。

**「找不到」的五个结构性原因（都有实据）**：

| # | 原因 | 实据 |
|---|------|------|
| 1 | 默认 tab 是「回复台」，目标卡在「客户&关系」tab，tab 名对功能零暗示 | `unified_inbox.html` L696-698 |
| 2 | 即使切对 tab，前两卡（客户信息+关系进展）默认展开，目标卡常在折叠视野以下 | L753-784 卡序 |
| 3 | 功能未开（`companion.goals.enabled=false` → API 403）时**整卡自动消失**——「关闭」与「不存在」同表现，坐席永远不会发现有此功能 | `cp-goal.js` `_hideCard(true)` L189/171 |
| 4 | 全站可发现性为零：导航 `nav_schema.py`、悬浮词典 `help_terms.py`、命令面板均未登记「工作目标/工作计划/营销目标」任何词条 | 三处 grep 均无命中 |
| 5 | 术语三分裂：坐席端叫「工作目标」、配置与 ops 叫「营销目标」、用户嘴里是「工作计划」——搜什么都对不上 | `goals.py` i18n pack vs `ov2_s_goal` |
| 6 | （附）未选会话时右栏组件无数据，目标卡也不可见——新人打开页面第一眼看不到 | 组件 context 依赖 conversationId |

---

## 1. 四视角分析

### 1.1 开发工程师视角

**做得好的**（保持，别在优化时破坏）：
- 组件化干净：`CpPanelBase` 继承 + shadow DOM + `data-act` 事件委托（无内联 handler，过全站哑按钮门禁）；
- i18n 双通道（`inbox.goal.*` 走宿主整包 `window.T/Tf`，共享键走 `CopilotShared.t`）；
- 容错完备：网络错误静默重试一次→可点重试行；403 自动隐藏；写失败回落服务器权威态（`refresh()`）；
- 后端单一入口 `/api/goals/for-conversation`（settle-on-read），前端无状态机负担。

**问题清单**：
- D1. **403=隐藏的语义过载**：功能关闭、无权限、路由挂了三种情况都表现为「卡凭空消失」，排查只能开 devtools 看网络面板；组件无任何「为何隐藏」的痕迹（连 console.debug 都没有）。
- D2. **双份文件人肉同步**：`shared/copilot/` 与 `desktop/renderer/shared/copilot/` 两份 cp-goal.js，靠手动拷贝 + `?v=20260727d` 手动 bump 缓存版本；漂移风险已知（git status 里两份都在改）。
- D3. **表单无偏好记忆**：每个会话建目标都要重选模板/自治档/期限；坐席批量作业时重复劳动。
- D4. **UI 层零埋点**：后端 goal stats 漏斗很全（P3/P15），但「卡曝光了几次、设定目标按钮点了几次」没有数据——本次「找不到」问题就是 UI 漏斗盲区的直接后果（曝光率≈0 却无人知道）。
- D5. 无会话选中时的空态交给基类，未显式设计。

### 1.2 市场经理视角

- M1. **这张卡是「1-3 天摸底、7-10 天成交」漏斗的坐席端唯一操作面**，但曝光路径三步深。手动建目标率趋零 = 只剩 auto_create 一条腿（覆盖不了老会话/特殊客户）；新坐席培训成本高（功能靠口口相传）。
- M2. **缺「今日工作清单」全局视图**：经理想看「今天哪些会话有推进安排、哪些被驳回、哪些在让路」，现在只能逐会话点开右栏；ops 卡是聚合计数（多少拍/多少让路），不是可执行的工作列表——「数字」与「名单」之间断层。
- M3. **今日拍反馈是最值钱的信号**（采纳/驳回直接回流 planner 降档），但按钮是 11px 的 👍👎，无培训引导，反馈率必然低——数据侧 P12 校准依赖这个信号。
- M4. 推荐产品/授权活动 chips 已经在卡里（与 prompt 注入同源，口径一致，这个设计很好），但与「AI 下一步」英雄卡未打通：坐席看到建议后要自己组织话术，没有「按此意图一键生成草稿」。
- M5. 术语分裂影响汇报：ops 叫营销目标、坐席叫工作目标，跨角色沟通要先对齐名词。

### 1.3 用户（坐席）视角

- U1. **发现成本**：三步（选会话→切 tab→滚动）；没有任何 badge/红点提示「这个会话今天有推进安排」——即使天天用的人也要主动去翻。
- U2. **术语搜不到**：想找「工作计划」，帮助词典、命令面板、导航全无结果（见 0 节 #4/#5）。
- U3. **空态按钮弱**：「设定目标」是普通按钮且藏在折叠卡里；空态文案「为这段关系设一个工作目标，AI 会按里程碑分天推进」写得好，但没人看得到。
- U4. **驳回的后果不透明**：驳回 confirm 里一句「连续驳回会自动降低推进力度」一闪而过；驳回后无撤销（误触 👎 只能等明天）。
- U5. **成就感弱**：标成交只有 confirm→🎉 badge；里程碑推进（4 段条变绿）无任何庆祝反馈——这是坐席日常最正向的时刻。
- U6. 窄屏/移动端右栏收起后，目标卡彻底不可达（无替代入口）。

### 1.4 美术设计工程师视角

**好的底子**：里程碑分段条（done 绿/cur 主题色 pulse 动画/待灰）、push pill 语义色、CSS token 化（`--cp-*` 带 fallback）、chips 体系统一。

- A1. **图标体系不一致**：目标卡标题用 emoji 🎯，同排其他卡全是 `data-cp-ic` 的 SVG 线稿图标（msg/heart/users/mic…）——一眼看出「外来户」。
- A2. **里程碑条信息哑**：4 段条只有 hover title 显示里程碑名（触屏无 hover）；当前处于哪个里程碑没有文字常显，进度感全靠猜。
- A3. **卡体过长无分区**：活跃态 = 标题行+段条+meta+今日拍+客户画像+推荐产品+按钮组 7 层堆叠，无视觉分组/呼吸感；画像和产品是 dashed 分隔，今日拍却是色块，分区语言不统一。
- A4. **按钮组危险动作布局**：「暂停 / 标成交 / 放弃」平铺一排，主动作（标成交）与破坏性动作（放弃，不可恢复）相邻，误触成本高。
- A5. **push pill 语义色误导**：「可直说」用 warn 橙色——直说是「许可」不是「警告」，坐席第一反应是「出问题了」。
- A6. **空态无图形**：纯两行文字+按钮，与英雄卡/其他空态的视觉品质不齐。
- A7. **终态色彩责备感**：failed/expired 用默认灰 badge 尚可，但「未达成」文案+灰的组合在连续多个失败时压抑；应中性化（客观陈述+「再设一个」引导已有，色彩可以更轻）。
- A8. 暗色主题风险：`--cp-*` fallback 全是浅色值，桌面壳暗色模式下若 token 未注入会刺白（需验证，属工程性视觉债）。

---

## 2. 优化方案（四个包，可独立上，建议顺序 A→B→C→D）

### 包 A：可发现性（解决「找不到」，最优先，约 1-2 天）

| # | 改动 | 落点 | 说明 |
|---|------|------|------|
| A1 | 「客户&关系」tab 徽标点亮 | `unified_inbox.html`（badge 机制已有 `ws-cp-tab-badge-customer`） | 当前会话有 active 目标 or 今日拍待反馈 → tab 上出 ● （tone 区别于未读语义） |
| A2 | 英雄卡「AI 下一步」加目标摘要行 | cp-hero 区 | 有目标：`🎯 第X/Y天 · 今日：<intent>（去反馈）`，点击自动切 customer tab + 展开目标卡 + 滚动定位；无目标且 goals 开启：淡一行「为 TA 设个工作目标 →」。英雄卡跨 tab 常驻 = 零发现成本 |
| A3 | 命令面板 + 悬浮词典登记 | `nav_schema.py` 命令项 / `help_terms.py` | 词条：「工作目标」「工作计划」「营销目标」同义词都指向说明+跳转动作 |
| A4 | 「关闭」≠「不存在」 | cp-goal.js `_hideCard` | 403 时不再整卡消失：显示一行灰字「功能未启用，需管理员开 companion.goals」（配置 `inbox.goal.show_disabled_hint` 可关回旧行为）；同时 console.debug 记录隐藏原因（D1） |
| A5 | 术语统一 | i18n packs | 坐席端全部「工作目标（工作计划）」首现互注；ops 端「营销目标」标题后括注「坐席端叫工作目标」；两端文档同步 |

### 包 B：今日工作清单（市场经理的全局面，约 2 天）

| # | 改动 | 落点 | 说明 |
|---|------|------|------|
| B1 | 轻量清单 API | `goal_routes.py` 新增 `GET /api/goals/agenda?scope=today` | 返回 `[{conversation_id, peer名, platform, intent, push_level, hold, feedback_state}]`——把 ops 的「计数」变成坐席的「名单」；复用 settle-on-read，无新状态 |
| B2 | 收件箱列表过滤器 | 会话列表工具条 | 新过滤 chip：「今日有推进」「待反馈」「让路中」；会话行加 🎯 微标（有 active 目标） |
| B3 | （可选）ops 卡「今日清单」抽屉 | ops_overview | 经理视角一屏看名单，点行深链进收件箱对应会话 |

### 包 C：交互效率（坐席顺手度，约 1-2 天）

| # | 改动 | 说明 |
|---|------|------|
| C1 | 表单偏好记忆 | localStorage 记上次模板/自治档/期限，下次预填（D3） |
| C2 | 驳回可撤销 | 驳回后行内显示「已驳回 · 明日自动降档 [撤销 5s]」，撤销窗口内可回滚（后端 feedback 已有 adopt/reject，撤销=复位当日拍状态，需小改） |
| C3 | 「按此意图生成草稿」 | 今日拍意图行加按钮：emit `cp-goal-drive-draft` 事件带 intent 文本 → cp-draft 以该意图为指令生成（M4，打通看→做） |
| C4 | 标成交带归因 | confirm 升级为小表单：可选填产品/金额 → 传给 `/status {action:done, meta}`，报表归因更准 |
| C5 | 里程碑推进庆祝 | 段条从 cur→done 的瞬间做一次轻量动效（scale+对勾），零成本正反馈（U5） |
| C6 | 画像补录回车提交 + 保存 toast | 现在只有按钮提交、成功无反馈 |

### 包 D：视觉美化（美术包，约 1-2 天，纯模板/组件热更免重启）

| # | 改动 | 说明 |
|---|------|------|
| D1 | 图标统一 | `data-cp-ic` 体系新增 `target` SVG 线稿，替换 emoji 🎯（A1） |
| D2 | 里程碑条升级 | 段上加节点圆点；当前里程碑名**常显**在条下（`当前：走近关系`），不再依赖 hover（A2） |
| D3 | 卡内分区语言统一 | 今日拍区：浅底色块 + 左侧 3px 语义色条（soft=主题色 / direct=品牌深档 / hold=灰斜纹）；画像/产品区同样用浅底块替代 dashed 线（A3） |
| D4 | 按钮组重排 | 「标成交」primary 右置；「暂停」次级；「放弃」降为文字链移入 `⋯` 溢出菜单，加二次确认（A4） |
| D5 | push pill 换色 | direct 从 warn 橙 → 品牌强调深档（许可语义）；hold 态灰斜纹底（A5） |
| D6 | 空态插画 | 轻量 SVG 线稿（山峰/旗帜）+ 现有文案 + primary 按钮（A6） |
| D7 | 终态情绪降压 | failed/expired badge 用中性蓝灰；「再设一个」按钮为视觉主角（A7） |
| D8 | 暗色主题体检 | 桌面壳暗色下过一遍 `--cp-*` token 注入，补缺失映射（A8） |

### 配套工程项（随任一包一起做）

- E1. **UI 埋点**（D4 的解药）：goal 卡曝光/展开/设定点击/反馈点击 → 复用 `frontend_error_stats` 同款轻量 beacon 通道（新 kind=ui_funnel）或 goal stats 增 UI 计数段；改版前先埋点拿基线。
- E2. **双份组件同步检查**：门禁测试比对 `shared/copilot/**` 与 `desktop/renderer/shared/copilot/**` 同名文件 hash（漂移即红），替代人肉记忆（D2）。

---

## 3. 实施顺序与风险

1. **先 E1 埋点 → 再 A 包**：没有基线，改完说不清效果。
2. A 包 1-2 天（除 A4 外全是前端+i18n，热更免重启）；B 包后端 1 天 + 前端 1 天；C/D 各 1-2 天。
3. 风险：
   - cp-goal.js 是**两端共享文件**：每次改动必须同步 desktop/renderer 副本 + bump `?v=` 缓存参（E2 门禁上了以后自动兜底）；
   - tab badge 与未读消息 badge 同位置，必须用不同 tone/形状（A1 注明）；
   - 会话列表过滤器（B2）动的是收件箱主列表——当前 sibling 线正在改 `unified_inbox.html`（standby/降档功能未收口），**动工前先确认对方收口，避免同文件互踩**（本仓有过事故）。
4. 全程遵守：模板/i18n 热更免重启；业务 .py（仅 B1）攒批走 `restart_instance.ps1`。

---

## 4. 度量（改完怎么知道有效）

| 指标 | 基线（改版前采 1 周） | 目标 |
|------|----------------------|------|
| goal 卡曝光次数/坐席/天（E1） | 待采（预计≈0-个位数） | ×5 |
| 手动建目标数/周 | 待采 | 显著>0（现在趋零） |
| 今日拍反馈率（adopt+reject / planned） | 后端 stats 已有 | ≥30% |
| 「找不到功能」类反馈 | 本次 1 起 | 0 |
| 驳回误触撤销率（C2 上后） | — | 观测项 |

---

## 5. 待决策问题（开工前问一句）→ 实施时已拍板

1. A4 未启用提示：**默认关**（`localStorage inbox.goal.show_disabled_hint=1` 或 `window.__GOAL_SHOW_DISABLED_HINT__=true` 才显示灰字）
2. B2 过滤 chips：**放展开后的 list-header-body**（不挤主工具条），会话行加 target 微标
3. C4 金额：**可选**（后端 `sanitize_won_meta` 已支持）
4. 术语：坐席端保持「工作目标」，括注/词典互注「工作计划」；ops 标题括注「坐席端叫工作目标」

---

## 6. 实施状态（2026-07-28）

### 已落地

| 包 | 状态 | 落点 |
|----|------|------|
| E1 埋点 | ✅ | 复用 `/api/telemetry/ui-event`（未新建通道）；曝光/设定/反馈/驱动草稿/深链/筛选 |
| E2 双份同步门禁 | ✅ | `tests/test_copilot_shared_sync.py` |
| A 可发现性 | ✅ | tab accent ●、英雄摘要行、命令面板+词典（先前已有）、403 灰字可选、术语互注 |
| B 今日清单 | ✅ | API 先前已有；列表 chips + 行微标 + 60s 刷新 |
| C 交互 | ✅ | 偏好记忆 / 驳回 5s 撤销 / 按意图生成草稿 / 成交归因表单 / 里程碑 celebrate / 画像回车+toast |
| D 视觉 | ✅ | target 线稿图标、里程碑常显、分区色条、直说深档色、放弃进 ⋯、空态插画、终态 muted |
| B3 ops 抽屉 | 🚧 施工中（另一线） | ops_overview「今日清单」按钮骨架已在 |
| D8 暗色体检 | ✅ 已完成（静态化） | 门禁 `tests/test_copilot_theme_tokens.py`：引用即须定义 + 明暗双表集合一致 + 语义色不混比例尺。首扫补齐 3 个缺口（`--cp-accent-deep` 直说 pill 暗色取亮档 `#93c5fd`、`--cp-accent-bg`、`--cp-bg-soft`），theme css 双树已同步 + 宿主 `?v=20260728a` |

门禁：`tests/test_goal_ui_revamp.py` + 既有 discoverability/agenda/sync（本轮 68 全绿）。

宿主接线（`unified_inbox.html`）已补齐并验收：英雄行 / tab accent / agenda chips+行微标 /
`?card=goal` 深链 / `cp-goal-drive-draft` / 60s agenda 轮询。模板热更新，**无需重启实例**。

### 相对原方案的优化（实施时加的）

1. **E1 不造新通道**：直接复用已有 `ui_event_stats` / `POST /api/telemetry/ui-event`，少一套存储与 ops 接线。
2. **C3 零后端改动**：活跃目标本就经 `context_block` 注入 prompt——「按此意图生成草稿」= 切回复台 + 触发生成 + 意图预填输入框。
3. **深链消费闭环**：命令面板 `?card=goal` 会清参、无会话时 toast、选中会话后自动展开目标卡。
4. **英雄行优先吃组件事件**：`cp-goal-loaded` / `cp-data-loaded` 驱动摘要，减少重复拉数；agenda 单独 60s 缓存服务列表筛选。
5. **放弃进溢出菜单**且成交改为可选归因小表单（比 confirm 对话框信息密度更高、误触更低）。
6. **筛选放可折叠 list-header**：不挤主工具条；与行微标联动，点 chip 即 narrowing 会话列表。
7. **JS fallback 全英文**：宿主新代码走 `window.T/Tf` + English fallback，避开 inbox 脚本裸 CJK 债务。

### Phase 2（同日下午，上表「下一阶段建议」1-6 全部落地）

| # | 项 | 落点 |
|---|----|------|
| B3 | ops goals 卡「今日清单」抽屉 | `ops_overview.html`：`#goalAgendaBtn`/`#goalAgendaWrap`（置于 `#goalDetail` 外，防 60s refreshAll 重绘吞掉展开态）；懒取 `/api/goals/agenda?scope=today&names=1&limit=100`；行深链 `/workspace?conv=<cid>&card=goal` 新页打开；403 latch 永久藏入口；13 个 `ov2_goal_ag_*` 键 zh+en |
| B3 配套 | agenda 名称富集 | `goal_routes.py`：`names=1` → 信封加 `names:{cid:display_name}`（batch 走 `get_conversations_for_ids`，软失败 `{}`，items 键集不动）；**.py 改动，随下次攒批重启生效**——抽屉在此之前回落 `platform:chat_key` 显示 |
| E1 基线可读 | ops goals 卡「UI 漏斗」行 | 复用同一 metrics 响应读 `ui_events.by_action` 的 `goal_*` Top8 chips——7 天基线直接看板读数，无需翻 Prometheus |
| D8 | 暗色体检 | 幽灵 token `--cp-accent-deep`（组件引用但从未定义）+ `--cp-accent-bg`/`--cp-bg-soft` 正式进 `theme-light/dark.css` 四份（web+桌面）；暗色 accent-deep 取**更亮**档（语义=更强调而非色值更深）；组件内 4 处亮色硬编码收口 token |
| U6 | 窄屏快捷入口 | 右栏收起且 goals 可用且选中会话 → 会话顶栏「工作目标」芯片（`#goal-quick-btn`，`_openGoalQuick` 已挂 window 过哑按钮门禁），点击开侧栏+展开目标卡 |
| 主动曝光 | 待反馈自动展开 | 切「客户&关系」tab 且今日拍待反馈 → **每会话一次**自动展开目标卡（比原方案「无条件自动展开」克制，防打扰）；埋点 `goal_auto_expand` |
| 驱动草稿 | 防覆盖 | composer 已有手打内容时意图不清空（只在空时预填）；焦点照常 |
| bug 修复 | 深链时序 | `?conv=<id>&card=goal` 同行时不再先误弹「请先选择会话」——消费延迟到会话打开（B3 行深链的前置条件） |

门禁：Phase2 集成全量 **177 passed**（goal_ui_revamp / copilot_shared_sync / discoverability /
agenda / routes / ops_agenda_ui / ops_overview + 哑按钮 / 重复 id / 孤儿引用）。
已知无关红灯：`_channel_body_telegram.html` / `workspace_channels.html` 的 CJK 泄漏系
渠道中心重构线当日提交（f7732a4）遗留，不属本工作面。

### 下一阶段（Phase 3 候选）

1. **采基线 7 天后复盘**：ops goals 卡「UI 漏斗」行直接读数，对照 §4 目标定 A/B 后续（曝光 ×5 是否达成、反馈率 ≥30%）。
2. **agenda 名称富集投产**：随下次攒批重启生效（无独立重启价值，勿单独重启）。
3. **移动端目标卡体验**：快捷芯片打开的是全宽侧栏，目标卡表单在窄屏的可用性（输入框/日期选择）值得实机过一遍。
4. **反馈率强化（看数据再做）**：若 7 天后反馈率仍低，考虑在今日拍 push 行加「一键采纳并生成草稿」复合按钮（合并两次点击）。
5. **agenda 分页**：settle cap 100/请求对当前规模足够；会话规模上千时再考虑游标分页。

---

## 7. P18（2026-07-31）：「会用」层——P17 修「找不到」，本轮修「找到了也不会用」

> 触发：运营实测反馈「用户在使用上不会，需要引导」+ 截图实锤（新手停在
> 「自定义目标」模板上写不出「给 AI 看的推进方向」，而 7 个场景模板藏在下拉里；
> 「已预填上次偏好」还会把一次错误选择永久固化）。

### 已落地（模板/i18n/共享组件热更即生效；.py 两处随下次攒批重启）

| # | 项 | 落点 |
|---|----|------|
| 1 | **目标表单场景化**：模板下拉 → 场景卡片（每模板一句人话适用场景，i18n `inbox.goal.tmpl_desc.<id>` 8 键双语）；custom 收进「进阶」入口且**不参与默认预选**（偏好里存了 custom 也只回放期限/自治档）；沉默 ≥72h 自动标「推荐」沉默唤回（宿主 ctx.goalHint.silentHours 喂数，高置信才推荐） | `cp-goal.js`（双树）+ `goals.py` pack |
| 2 | **自治档诚实文案**：auto 档 hint 改为「若系统开启了主动触达才会主动发起」；`/api/goals/templates` 增 `caps:{bridge_enabled,proactive_enabled}`（.py，待重启），bridge 关着时表单灰字如实注明「不会自己主动发消息」——修「文案过度承诺 vs 默认配置不出门」的预期错位；caps 缺失（旧后端）不猜不注解 | `cp-goal.js` + `goal_routes.py` |
| 3 | **三步上手引导**：右栏首次可见自动放一遍 coach marks（①AI 下一步 ②设工作目标 ③你来把关发送），❓ 随时重看；锚点缺失（403 整卡隐藏）自动回落备选锚点；窄屏(<900px)不自动打扰；App 模式跳过 | `unified_inbox.html`（`_cpTour*` 全套 + window 暴露）+ `unified-inbox.css` + `inbox_workspace.py` pack（`inbox.cptour.*`） |
| 4 | **建目标后「接下来会发生什么」**：按所选自治档出一次性提示（observe/suggest/auto 三套文案，`inbox.goal.created_next.*`），修「建完然后呢」断崖 | `cp-goal.js` |
| 5 | **「AI 做了什么」进展时间线**：目标卡内懒取 `GET /api/goals/{id}` 拍史（接口 P3 起就有、坐席端首次消费），行=日期+状态（已计划/已带入回复/已发出/已跳过/已让路）+意图，修「AI 推进过程完全不可见」 | `cp-goal.js` |
| 6 | **「点了没反馈」收口**：宿主 `cp-fill` 统一 toast「已填入输入框」+按来源埋点；`cp-action-done` 出「已执行/执行失败」toast；协作卡推荐话题从下划线文字改按钮式 chip（带 tooltip），无 opener 的话题不再渲染（静默哑点击清零） | `unified_inbox.html` + `cp-collab.js`（双树） |
| 7 | **术语人话化**：剧本内置话题「共同梗 callback」→「回访上次话题」（.py 待重启）；「自治档」标签加括注「AI 参与度」 | `conversation_script.py` + `goals.py` pack |
| 8 | **新埋点**（复用 ui-event 通道）：`goal_tour_show/replay/done/skip`、`goal_form_pick_scenario/custom`、`goal_progress_open`、`cp_fill_<source>`、`nba_exec_ok/fail` | 组件+宿主 |
| 9 | 坐席一页纸 SOP + 30 秒演示脚本 + 常见问题（培训物料） | `docs/坐席上手_业务助手三步SOP.md` |

门禁：`test_goal_ui_revamp.py` 新增 P18 段 10 例（场景卡/推荐/诚实注解/建后提示/时间线/
caps/动态键双语/宿主接线/tour CSS）；版本戳断言更新至 `?v=20260731`。缓存戳已 bump：
cp-goal/cp-collab/cp-i18n `?v=20260731a` + unified-inbox.css + ui-build.txt。

### 刻意不做 / 避让

- **help_terms 词条不加**：调研实锤工作台 `workspace_base.html` 没有 TERM_DICT 悬浮词典
  （只在管理后台 base.html 生效），加词条对坐席零收益——术语解释改由 tour + 卡内 hint 承担。
- **功能中心「使用指南」链接**：settings 功能总览卡是另一条线当日活跃工作面（feature_registry
  P1/P2），避让不动；建议对方收口后加一行 goals 行动作链接指向 `?card=goal` 深链或 SOP 文档。
- **NBA 采纳率后端表**：暂用 ui-event 埋点口径（nba_exec_*、cp_fill_nba），若 7 天读数
  证明需要更准的会话级归因再上库。

### 验收口径（上线 7 天读 ops goals 卡「UI 漏斗」行）

- `goal_tour_done / goal_tour_show` ≥ 60%（引导完成率）；
- `goal_form_pick_custom` 占比显著低于 `goal_form_pick_scenario`（场景卡片起效）；
- 今日拍反馈率 ≥30%（P17 目标沿用）；
- 「不会用」类反馈清零。

### 冻结基线（2026-07-31 02:15 实测，`/api/workspace/metrics?format=prometheus`）

> ⚠️ 进程级计数，随实例重启清零；本快照窗口=上次重启起 ~10.3h。8/7 复盘用增量口径对比。

- `ui_events_by_action_total`：goal_card_expose=11 / goal_set_click=2 / goal_create_ok=1
  （**表单放弃率 1/2**；tour/scenario 事件尚未产生——P18 刚上线）；
- `goals_created_total`=1；`goals_beats_total{planned}`=30；
  **`goals_beat_feedback_total` adopt/reject/undo = 0/0/0 → 反馈率 0%**；
- `goals_injected_total` reply=4 / draft=1（注入链活着）；`goals_terminal_total{done}`=0。

## 8. P19（2026-07-31 深夜）：基线数据驱动的两项加强

反馈率 0% 当场坐实（30 拍 0 反馈）→ 原「7 天后看数据再做」的复合按钮**提前落地**：

| # | 项 | 落点 |
|---|----|------|
| 1 | **「采纳并拟稿」复合按钮**：今日拍主动作合并「采纳 → 按意图生成草稿」两连击（先服务端落账采纳，成功才驱动草稿）；👍 降级为「只采纳」；已采纳态保留拟稿入口。埋点 `goal_feedback_adopt_draft` | `cp-goal.js`（双树，`?v=20260731b`） |
| 2 | **本周成果 chip**（坐席成就感面）：会话列表 agenda 行右侧读 `/api/goals/report?days=7`（totals.done + active_now），有成交=庆祝色、仅推进中=中性、**双零=整条隐藏**（空数字是反激励）；10min 轮询、403/异常静默 | `unified_inbox.html` + `unified-inbox.css`（`?v=20260731b`）+ `goals.py` pack |

门禁：`test_goal_ui_revamp.py` P19 段 4 例。刻意不做：功能中心「使用指南」链接继续避让
（交付分级线 27min 前仍在动文档，收益低风险中，等对方收口）；移动端窄屏表单需实机，
列入下阶段。

## 9. P20（2026-08-01）：画像区排版收口 + 缺口 chips 动作化（截图实锤两 bug）

> 触发：运营截图实锤——「AI 做了什么」下方画像区文字**逐字竖排**（客户画像/关系/
> 商机/补录全部竖着长）+「业务痛点?/在用平台?/团队规模?」**点不动**。

**根因（都在 cp-goal.js）**：① 画像头部单行 flex 硬塞「标题+双固定宽(52px) bar+补录
按钮」≈304px，右栏默认 300px（内容区 ~236px）必然溢出 → 文本被压到 min-content →
CJK 逐字换行成竖排；侧栏可拖到 480px，开发态宽栏不复现所以漏网。② 缺口 chip 是纯
`<span>` 无 `data-act`（基类点击委托只认 `[data-act]`），但样式与可点的产品 chip 同族
——可供性错位，坐席必然以为能点。

| # | 项 | 说明 |
|---|----|------|
| 1 | **头部两行化**（P0-1） | 行1=标题+补录（全 nowrap）；行2=`gl-fillrow` 关系/商机双 bar（bar 从固定 52px 改 `flex:1 1`，标签/百分比 nowrap+tabular-nums，容器可 wrap——280px 最窄档亦不竖排） |
| 2 | **双零不渲染 0% 条**（P0-3） | `hasFill` 守卫——空数字是反激励（与 P19 wins chip 同哲学）；冷启动由 empty 文案+缺口 chips 承担 |
| 3 | **缺口 chip 动作化**（P0-2） | span→button+data-act：点击展开追问行「可以这样问：<建议问法>」+ 双出口——**拟稿去问**（`ask_intent` 包装成自然追问指令，复用 `cp-goal-drive-draft` 既有宿主链=切回复台+意图作生成种子，**零宿主 JS 改动**）/ **我来补录**（开表单+聚焦该字段）。建议问法走前端 i18n 键 `inbox.goal.profile.ask.<slot key>`（11 槽位 zh+en，热更零重启；键缺失回落 label 绝不裸奔键名） |
| 4 | **已填 chip 可编辑**（P1） | 点击开补录聚焦该字段；来源标注（自动/手录）收进 tooltip 省宽度 |
| 5 | **补录表单分组+placeholder**（P1） | 按轨分组（关系/商机小节头，lifecycle 随商机组尾）；建议问法当 placeholder |
| 6 | **产品行升级**（P1） | chip→两行 mini 卡：名称+↗外链线稿 / 价格右对齐 / pitch 从 hover title 提为常显副行（触屏可达——那是坐席的现成话术） |
| 7 | **新埋点** | `goal_slot_menu/ask/fill/edit`（复用 ui-event 通道）——读「追问点击→BANT 填充率」是否联动 |

落点：`cp-goal.js`（双树，`?v=20260801a`）+ `goals.py` pack（+28 键）+ ui-build.txt。
门禁：`test_goal_ui_revamp.py` P20 段 6 例（含竖排根因回归钉：禁 `width:52px` 回潮、
`.gl-prof-hd` 必须 wrap、bar 必须弹性；`ask.<key>` 双语与 profile_slots 注册表联动钉）。
本轮 375 例全绿（唯一红灯=sibling 线 copilot-client/cp-i18n 双树中间态漂移，非本工作面）。

**下一阶段（P21 候选）**：① 280px 窄宽真浏览器回归探针（verify_* 家族，环境缺失 SKIP）；
② 7 天读 `goal_slot_ask` 漏斗（追问点击→填充率→成交联动）；③ 模板级 win-rate 报表 +
per-goal LLM 成本行（.py，攒批）；④ 成交前置「发试用」接 chatx_fulfillment（产品拍板）。
→ 实施见 §10（③ 经调研改判为数据闸口项，理由在 §10.2）。

## 10. P21（2026-08-01 上午）：窄宽/交互真浏览器门禁 + 数据闸口改判

### 10.1 已落地：`tools/verify_goal_card_ui.py`（29 项，全绿；挂 gate_sweep -Full）

P20 竖排事故能上生产的根因是**没有任何门禁在默认宽度下渲染过组件**（静态门禁钉
CSS 源码文本，钉不住 shadow DOM 计算布局）。本探针与 verify_* 家族刻意不同——
**file:// 自包含夹具页**（真组件文件 + 假 fetch + 假 sendBeacon + goals.py pack
真实 zh 词条内嵌替身），三个理由都来自当天现场：
1. **零实例依赖**：实例宕/重启冷却/忙时照样跑，CI 可跑；仅缺 playwright 才 SKIP。
2. **零遥测污染**：探针要点「拟稿去问」——在真工作台跑会给 `goal_slot_ask` 灌水，
   恰好污染 7 天复盘要读的数。实测：探针 29 项跑完，生产 `goal_slot_*` 计数仍为 0。
3. **免疫 sibling 中间态**：不加载 cp-i18n.js（当日它正被另一条线编辑）。
覆盖：216/236/276px 三档布局（标题/按钮单行、零横向溢出、双 bar）+ 双零冷启动
（不渲染 0% 条、空态文案、缺口 chips 仍在）+ 交互链（缺口 chip→追问行→drive-draft
事件带已替换 intent / 补录聚焦对位 / 已填 chip 编辑聚焦 / 表单分组+placeholder / 产品
pitch 常显+价格）。踩坑记录：夹具模板占位符 `__ZH__` 与变量名 `window.__ZH__` 自撞
→ str.replace 连变量名一起换成 JSON → 内联脚本语法崩溃；占位符一律用 `@@..@@` 形。

### 10.2 改判（实施中的再优化，都有依据）

- **模板级 win-rate 呈现 → 数据闸口项**：调研发现后端 `store.outcome_report` 的
  by_template（成功率/均天数）P2 起就有、`/api/goals/report` 一直在出，ops 卡也已
  展示总体 done_rate——真正缺的不是代码是**终态样本**（7 天窗 0 终态、历史 done=0）。
  现在加模板级表格=给全零数据造 UI（「空数字是反激励」）。判据：`report.totals.n
  ≥ 10` 或任一模板 organic ≥5 时再把 by_template 表进 ops goals 卡。
- **per-goal LLM 成本 → 设计后置**：精确归因要在回复链打 goal 标签（skill_manager
  热区、跨切面大）；粗估口径（全量成本÷成交数）会误导定价决策。等成交样本 >0 后
  用「goals_injected 次数 × 注入块均 token × 单价」的**边际成本**口径做报表行。
- **基线快照（2026-08-01 09:20，进程窗口≈上次重启起 1.4h）**：ui_events goal_* =
  expose 14 / auto_expand 4 / drive_draft 3 / **adopt_draft 1（P19 复合按钮首次被真实
  使用）** / tour_show 4；`goal_slot_*` = 0（P20 刚上线，干净基线）；report(7d) 终态
  0、**active_now=31**（自动建目标链在跑，对比 P18 冻结时 created=1 大幅增长）。
  8/8 复盘对比此快照 + outreach 口径的 DB 数据。

### 10.3 决策件（需产品/运营拍板，代码侧已就绪）

**成交前置「发 7 天试用」**：链路件齐全——SKU→license payload 权威映射
（`chatx_fulfillment.py`，签名只在厂商机）+ 试用 claim/履约守护（trial_claim/
fulfill_trial_service）+ 兑换码交付（cs_redeem_bot）。缺的只是目标卡入口（direct 档
出「发试用」按钮 → 建 claim 带 ref=conversation_id → 现有守护出码 → 坐席发送）。
拍板点：试用 SKU/期限、每 contact 限一次的幂等口径、是否计入 funnel。**在拍板前
刻意不动代码**（发放真实授权=真金白银的运营动作）。

### P22 候选（已落地，见 §11）

原候选里「采纳并拟稿语义断链 / 设定目标失踪」因坐席新截图升级为 P0，先行实施。
数据闸口项（8/8 复盘 / win-rate / 试用）顺延 P23。

---

## 11. P22（2026-08-01）：采纳并拟稿语义接通 + 目标管理入口

### 11.1 坐席实锤根因

1. **「采纳并拟稿」产出闲聊**：宿主把今日意图写进 `#reply-ta`，但 `cp-draft._generate`
   的 payload **没有 instruction**；后端 `_goal_block` 在 push=none 时写「今天只陪伴」
   → 模型「正确」地闲聊，坐席以为按钮坏了。
2. **「设定目标」消失**：按钮只在空态；`auto_create` 人设开闸后几乎永有目标；无「换方向」。

### 11.2 已实施（相对原方案的再优化标 ★）

| 项 | 内容 |
|---|---|
| A1 | `smart-reply.instruction` → `persona_reply.agent_instruction` → `generate_inbox_draft` → `user_context["_agent_instruction"]` → prompt【坐席指令】；finally 清掉防落库 |
| A2 | `cp-draft.setDirective` + 可关 chip「按今日意图拟稿」；gen 带 `instruction` |
| A3 | 宿主**停写 composer**，改 `setDirective`；toast「已切到回复台…」 |
| B | ⋯「换个方向」；`created_by`→「AI 自建」徽标；点自治档循环切换；陪伴日 tip |
| ★ | 指令拼进**力度规则**（none/soft/direct），陪伴日不会被硬拧成推销 |
| ★ | 指令封顶 400 字；自治档 update 保留本地 `today`（端点 goal_view 不带拍） |
| ★ | `app.html` 同文档也接 drive-draft（iframe 模式不依赖父页） |
| 门禁 | `test_goal_ui_revamp` P22 段 + `test_agent_instruction_prompt`；探针查 instruction+pushLevel |
| 双树 | cp-goal / cp-draft / cp-i18n / app.html；`?v=20260801c`；ui-build `20260801-1045` |

### 11.3 P22.1（同日再优化，相对 P22 的二次加深）

复查发现五处缺口并落地（★＝相对「只接 instruction」的再优化）：

| 缺口 | 修复 |
|---|---|
| opener 模式不吃 instruction →「采纳」静默丢意图 | ★ `setDirective` 强制 `_mode=reply`；opener 产线仍透传 `agent_instruction` 双保险 |
| chip 展示整段多行指令挤版 | ★ chip 只显 `summary`（意图首行），全文进 title tooltip，payload 仍发完整 instruction |
| SkillManager 挂掉走 `ai.chat` 兜底时无指令 | ★ 兜底 prompt 拼【坐席指令——本条必须完成】 |
| 英雄卡有今日拍却要再点「采纳并拟稿」才发现 | ★ 英雄卡「拟稿」→ `driveDraftFromToday`（与卡内同指令拼装）；宿主缺 instruction 时用 intent+push 回拼 |
| 线上看不见指令是否真进了产线 | ★ `[smart_reply] … instr=0/1` 日志锚点 |

P23 候选里「英雄卡一键拟稿」已前移到 P22.1（发现成本优先于数据闸口）。

### 11.4 生效说明

- JS / i18n / 模板 / CSS：**热更新**（刷新工作台；旧标签看 ui-build 横幅）。
- `.py`（desktop 路由 opener 透传 / persona_reply 兜底+opener）：需
  **攒批重启**一次 `restart_instance.ps1 -Instance zhiliao`（注意冷却，勿连环 `-Force`）。

### 11.5 P23（2026-08-01 下午）：复盘工具化——8/8 读数面全部 DB/文件口径

原则：**复盘本身要等一周数据，但读数面必须先就位**（8/8 当天是「读结论」不是
「翻日志」）。进程计数（GoalStats/UiEventStats）重启即清零、本机重启频繁 →
全部读数下沉到耐久口径（goals.db / JSONL），跨重启可比。

| 项 | 内容 |
|---|---|
| win-rate 底座 | `outcome_report.by_template/totals` 增 `won/won_rate`（won=result 前缀 `order:`/`manual:`，与 churn_outcomes 同判定；winback「回话」done 刻意不进 won；分母与 done_rate 同=organic 排除 cancelled，两率可横比） |
| 指令拟稿耐久漏斗 | cp-draft payload 随行 `goal_id`+`instruction_source`（beat/hero/slot，cp-goal 早已 emit source，host/app.html 透传）→ smart-reply 落 `goal_events kind=drive_draft`（`peek_goal_store` 只取既有单例，goals 关=零成本）→ `outcome_report.drive_draft{total,by_source}` |
| 画像填充漏斗 | `outcome_report.profile_fills{total,by_src,by_track}`——按**槽位自身 ts** 落窗（行级 updated_at 会被别的槽刷新），src=auto/agent/llm、轨=relation/bant。ask→fill→won 三段齐 |
| 指令遵循抽检 | `src/inbox/instr_samples.py` JSONL 留样 ring（≤512KB 保尾 300；指令≤200/产出≤240；**不存客户原文**）；读出 `GET /api/goals/instr-samples`（与 readiness 同豁免，goals 关也 200；路由台账已登记） |
| 周审 CLI | `growth_review --days 7 --samples 20`：win-rate 表（organic<5 标「样本不足只读不判」）+ 指令生成 by_source + 填充漏斗 + 抽检样本；判词：指令 0 使用=发现性问题、有拟稿但 bant 零填充=「问了没采到」；趋势行增 won/drive_draft/profile_fills |
| ops 卡 | 「真成交」行（won>0 才显）+「模板 win-rate」Top2（**organic ≥5 才呈现**——小样本读数会误导调模板，宁缺毋滥）；i18n `ov2_goal_won/winrate/winrate_tip` zh+en |
| 门禁 | `test_goal_store`（won 口径/drive_draft 分桶/fills 窗口）+ `test_instr_samples`（ring/截断/best-effort/接线契约）+ `test_goal_growth_loop` P23 渲染判词 + 路由台账 + `test_goal_ui_revamp` payload 契约 |

**8/8 复盘操作**：`python -m scripts.growth_review --days 7 --samples 20`
一屏读完；照判词行动（模板句/入口发现性/采集断链），有样本再谈 win-rate。

**生效与验证实录（2026-08-01 中午）**：
- `.py` 经他线 11:34 重启**搭车装载**（共享代码根，未额外吃重启窗口）；曾备
  `scripts/ensure_restart_once.ps1` 低峰兜底任务（搭车检测+编译闸门+唯一入口），
  搭车确认后任务已删、脚本留档休眠。装载前对全树 106 个脏 `.py` 跑过编译闸门
  （唯一「FAIL」是他线刻意删除的 conversation_script.py，无活引用）。
- 端到端冒烟：带指令真调 smart-reply → 回复精确执行指令（只问团队规模一个
  问题+软性提产品）→ 样本落 `<数据根>/logs/instr_samples.jsonl` → API 可读 →
  app.log `instr=1` 锚点在。首拉真数据：7 天画像填充 53 槽（bant 33 / llm 23）。
- **周批已挂**：计划任务 `GrowthReviewWeekly`（周六 07:20，
  `scripts/growth_review_weekly.ps1`，token 从实例配置自取不进任务定义；趋势行
  追加 `logs/goals/growth_trend.jsonl`，2026-08-01 基线行已种：profile_fills=53 /
  drive_draft=0 / won=0）。

### 11.6 P24 候选（下一阶段）

① **意图文案 i18n**（设计已验证可零迁移：`pick_intent` 是 crc(goal_id,day) 确定性
   挑池，视图层同参重算英文池即可，存量 DB 不动；代价=全模板×里程碑英文案打磨，
   等有英文坐席再做，别为空需求写百句文案）；
② 试用发放 CTA（产品拍板后接 `trial_claim` 既有路由）；
③ 8/8 读数后的动作项：win-rate 样本 ≥5 的模板调参、mode_gate 类比的「低效模板
   降频」（有数据地基后再谈机制）；
④ 抽检样本 ≥20 条后：若「指令遵循」人耳不合格率 >20%，把【坐席指令】块升权重
   或改 few-shot 注入（质量闸决策，读数说话）。
