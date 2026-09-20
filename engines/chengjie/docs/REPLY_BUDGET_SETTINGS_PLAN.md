# 回复额度守卫（peer_bot_guard）进「自动回复设置」页 —— 实施方案

> 状态：**P0 已落地并上线**（2026-08-12 12:08 zhiliao 重启装载，线上探针 8/8 PASS + 真机截图验收）。
> P1/P2 未动。budget=0 语义已核实＝**不限额**（`evaluate` 的 `budget > 0` 闸 + `budget_flags.enabled`，
> 有门禁 `test_budget_flags_zero_budget_means_unlimited` 钉住）→ UI clamp [5,500]，0 仅 YAML。
> 日期：2026-08-12。来源对话：豁免按钮（今日继续）后台化需求。

## 0. 需求原文

> 「豁免按钮（今日继续）这个设置要加到后台的自动回复设置里。有一个总体开关。
> 以开发工程师、市场经理、用户、美术设计 UI/UX 工程师这几个角度对这些页面功能
> 进行分析，对它进行优化还有美化。」

## 1. 现状（已核实的代码事实）

| 事实 | 位置 |
|---|---|
| 「自动回复设置」页已存在，10 张卡片 | `src/web/templates/reply_settings.html`（`/reply-settings`） |
| 设置管线＝白名单 spec → sanitize → overlay 落盘 → 审计 → worker 热更 | `src/inbox/reply_pacing_settings.py`（spec/sanitize_patch/cross_validate）+ `src/web/routes/reply_settings_routes.py`（GET/save/audit） |
| 守卫配置键（目前只能改 YAML） | `inbox.peer_bot_guard.{enabled, daily_reply_budget=40, repeat_streak_n=3, instant_reply_sec=3.0, instant_repeat_n=2, proactive_filter, sweep_legacy, heuristics, suspect_threshold=0.6}`，解析在 `src/inbox/peer_bot_guard.py::parse_cfg`（**每次调用现读 config → overlay 热更天然生效，无 worker 固化问题**） |
| 豁免（今日继续）已有 API + 收件箱触顶横幅 | `POST /api/unified-inbox/reply-budget/relief`（`unified_inbox_stored_read_routes.py:278`）+ `unified_inbox.html` `reliefReplyBudget()`（仅触顶时可见） |
| 预算台账 | `inbox.db::peer_reply_ledger`（conversation_id, day, auto_replies, relief_day）；`budget_state()` 出 used/limit/exhausted/relief |
| ops 总览已有守卫观测卡（只读） | `ops_overview.html` |

**问题**：同一个守卫的「配置 / 救济 / 观测」散落三处；配置层完全没有 UI；
豁免入口只在事后（触顶）出现，无法事前管理，也看不到「今天谁被闸了」。

## 2. 四角色分析

### 2.1 开发工程师
- **复用 > 新建**：设置页管线（spec 白名单 + overlay + 审计 + i18n pack + 门禁）已成熟，
  新卡只是「加键 + 加卡 + 加一个只读列表端点」，边际成本低、回归面小。
- **热更零风险**：`parse_cfg` 逐调用现读，写 overlay 即生效（~30s 热重载节流），
  不涉及 AutosendWorker 构造期固化（deliver_delay 那类 worker 热更桥不需要）。
- **危险开关要有摩擦**：总开关关闭＝防轰炸失守（2026-08-03 SpamBot 空转实锤：
  80 秒 78 轮 LLM 空转）。关闭动作必须有确认弹窗 + 落审计日志（管线自带 audit）。
- **额度语义要钉死**：`daily_reply_budget=0` 在 `parse_cfg` 里 clamp 到 ≥0；
  `budget_state` 对 0 的语义（0=不限 or 0=全拦）实施前先读代码确认并在 UI 文案里写明，
  防止运营把 0 当「不限」误用。UI 输入 clamp 建议 [5, 500]。
- **新端点只读优先**：「今日被闸/已豁免会话列表」查 `peer_reply_ledger`（当天行 +
  used≥limit 或 relief_day=今天），JOIN 会话表出显示名。豁免动作**复用现有** relief
  端点（勿造第二个写入口——幂等/审计/横幅刷新已在那条链上）。
- **门禁**：spec 键增补进现有 `test_reply_settings*` 门禁族 + 路由契约
  `test_admin_route_inventory` + i18n zh/en 双语齐备 + ratchet 不新增路由 CJK。

### 2.2 市场经理
- 「静默熔断」的体感＝产品坏了——客户演示中途触顶不回话是最伤单的事故形态。
  设置页有显式开关+额度+豁免列表 ⇒ 演示前可一键放宽，**演示保险**。
- 叙事翻转：40/天不是「限制」而是「账号安全额度」（防对轰烧号、防风控），
  在 UI 文案里按卖点写（「保护你的账号信誉」），而不是按阀门写。
- 竞品对照：同类工具多数只有全局开关没有会话级救济——「按会话今日继续」
  是差异点，值得在卡片里显眼呈现。

### 2.3 用户（运营/坐席）
- 现状痛点：触顶后才知道（收件箱横幅），想**事前**看「今天哪些会话在烧额度」；
  测试期想全局关掉或调大，不想找工程师改 YAML。
- 高频操作路径：设置页开总开关/调额度（低频）→ 收件箱横幅点「今日继续」（高频，
  保留不动）→ 设置页列表批量豁免（中频，新增）。
- 认知负担：`repeat_streak_n`/`suspect_threshold` 这类启发式参数用户看不懂，
  必须折叠进「高级」，默认只露「开关 + 每日额度」两个控件。

### 2.4 美术设计 UI/UX
- **新卡位置**：插在「🔌 上游总闸」卡之后（同属"闸门"语义组），命名
  「🛡️ 回复额度守卫」。
- **卡内布局**（上→下）：
  1. 主开关行：switch + 一句人话（"防止与机器人互相轰炸、保护账号额度"）+
     状态徽标（运行中=绿 / 已关闭=灰）。
  2. 每日额度：数字输入 + 快捷档（40 默认 / 100 宽松 / 200 演示），带
     「今天全局已用最多的会话 top1」小字提示（用数据说话）。
  3. 「今日额度状态」表：会话名 | 已用/上限 | 状态（正常/触顶/已豁免）|
     操作（今日继续按钮，触顶行才亮）。空态＝「✅ 今日没有会话触顶」。
  4. 折叠「高级参数」：repeat_streak_n / instant_reply_sec / heuristics /
     suspect_threshold（每项带一句白话解释）。
- **色彩语义与收件箱横幅一致**：触顶=琥珀、豁免=蓝、正常=绿、关闭=灰
  （用现有 `--th-*` 主题 token，暗色模式免费获得）。
- **危险操作视觉**：总开关关闭走红色确认弹窗（复用页内既有 confirm 风格）。
- **整页美化（顺带，克制）**：卡片数将到 11，页面纵向偏长 → 顶部加锚点
  chips 目录（闸门/节奏/内容/语音 分组跳转）；统一各卡 `<h3>` emoji+标题
  风格（已基本统一，补齐即可）。**不做整页重构**——reply_settings.html
  属多线活跃区，控制爆炸半径。

## 3. 实施分期

### P0（本批必做，一次交付可用）
1. `reply_pacing_settings.py` spec 白名单增补 2 键：
   `inbox.peer_bot_guard.enabled`（bool）、`inbox.peer_bot_guard.daily_reply_budget`
   （int, clamp 5..500）。落盘/审计/热更走既有管线，零新机制。
2. 新只读端点 `GET /api/reply-settings/budget-today`：
   查 `peer_reply_ledger` 当天行 → `[{conversation_id, title, used, limit,
   exhausted, relieved}]`（JOIN conversations 出显示名；上限 50 行按 used 降序）。
3. `reply_settings.html` 新卡「🛡️ 回复额度守卫」：主开关 + 额度输入 + 快捷档 +
   今日状态表（豁免按钮复用 `POST /api/unified-inbox/reply-budget/relief`，
   传 platform/account_id/chat_key——列表端点需回传这三元组）+ 高级折叠。
4. i18n：新词条进 `src/web/i18n_packs/`（reply_settings 所属 pack，键前缀
   `rps_guard_*`），zh+en 双语齐备。
5. 门禁：spec 增键用例 + 新端点路由契约 + 模板哑按钮/重复 id 门禁自动覆盖 +
   `gate_sweep.ps1` 收口。
6. 发布：模板/i18n 热更新即生效；**bump `ui-build.txt`**；`.py`（spec+路由）
   改动攒批重启（走 `restart_preflight.ps1` → `restart_instance.ps1`）。

### P1（次批）
1. 高级参数四键进 spec（repeat_streak_n / instant_reply_sec / heuristics /
   suspect_threshold），折叠区接线。
2. 触顶**预警**：会话额度用到 80% 时收件箱会话列表行加小徽标；watchdog 增
   `budget_exhausted` 聚合告警别名（复用升级式提醒模式，恢复自动清零）。
3. ops 守卫观测卡 ↔ 设置页互跳链接。

### P2（观察后决定）
1. 额度分档进「场景预设档」联动（保守/平衡/演示 预设顺带调守卫额度）。
2. 页面锚点 chips 目录 + 卡片分组（闸门/节奏/内容/语音）。
3. 豁免审计视图（谁在什么时候给哪个会话开过豁免——audit log 已落，缺读取 UI）。

## 4. 风险与红线
- **勿造第二个豁免写入口**：设置页豁免必须复用收件箱同一 POST 端点。
- **总开关默认值不动**：example 基线 `enabled: false`，zhiliao overlay `true`；
  UI 只改 overlay，勿动 config.yaml/example。
- **overlay 写入必须走 `save_overlay_patch`**（ruamel 保注释），禁止裸 yaml.dump。
- **热区纪律**：动工前 `agent_probe.ps1` 探活 + `-Intent` 登记；
  `reply_settings.html` 若 ACTIVE 先避让。
- **budget=0 语义**：实施第一步先读 `budget_state`/`_budget_gate` 确认 0 的行为，
  在 UI 文案与 clamp 中显式处理。

## 5. 验收清单（P0 落地记录，2026-08-12）
- [x] 设置页开关/额度改动 → overlay 落盘且注释保留 → ~30s 内新会话生效（免重启）
      ——机制验证：走既有 `save_overlay_patch` 管线（ruamel 保注释既有门禁）+
      `parse_cfg` 逐调用现读（hot=True）；路由端到端有 fake-cm 用例。真机 UI 保存
      刻意未演（避免为验收改动生产 overlay 值）。
- [x] 触顶会话在设置页列表可见并可一键豁免；收件箱横幅同步消失
      ——真机截图：测试会话 telegram:8244899900:5433982810 显示「已豁免 74/40」蓝 chip；
      豁免按钮复用 `POST /api/unified-inbox/reply-budget/relief`（触顶行才渲染，事件委托）；
      横幅消失走其自身 automation 轮询（既有机制）。真发豁免未演（当日测试会话已豁免，
      对其他生产会话点豁免＝真实运营动作，不为验收乱点）。
- [x] 关总开关有确认弹窗 + 审计行（`rpsSave` 拦截 `enabled→false` 走 `confirm`；
      审计随既有保存管线自动落 `reply_settings_audit.jsonl`）
- [x] zh/en 双语（i18n 门禁 313 绿）、暗色模式（真机截图）、哑按钮/重复 id/孤儿引用/
      动态拼接门禁 26 绿
- [x] `gate_sweep.ps1`：703 过；5 红全为他线既有/活跃编辑窗所有（emoji/th-tokens/duel/
      alert-e2e + unified_inbox 系，与前日意向板点名一致，本批文件零红）；
      `ui-build.txt` → 20260812-1158
- 实施补充：状态位语义收口为 `peer_bot_guard.budget_flags` 纯函数（budget_state 委托，
  横幅/设置页同源）；store 新增只读 `list_reply_budget_today`（LEFT JOIN conversations，
  台账孤儿行如实空三元组）；新端点 `GET /api/reply-settings/budget-today` 已进路由清单基线。
