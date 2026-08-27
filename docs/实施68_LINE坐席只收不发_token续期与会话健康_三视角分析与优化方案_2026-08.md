# 实施68：LINE 坐席聊天「只收不发」— token 续期与会话健康 · 三视角分析与优化方案（2026-08-24）

> 报障原话：「LINE 的在坐席聊天中，只能收到消息，消息发送不出去。」
> 报障场景：内测 bug 群 08-24 07:23 带图报障（Vision 摘要：vic001 与「无界科技·在线顾问 林小雨」的
> LINE 会话，最后一条 "hi" 07:11 无回应），随后用户表态「这个账号要删除」。
> 本文 = 生产实证（117 zhiliao）→ 根因链 → 开发/市场/用户三视角 → P0/P1/P2 优化方案。

---

## 一、一页结论

**「只收不发」不是一个 bug，是同一根因链的两张脸：LINE 协议账号的 token 会
「慢性死亡」，而系统对此既不自愈、不显示、也不报人话。**

实证到的根因链（全部有日志/库/线上探测证据，见 §二）：

1. **token 永不续期（根因核心）**：okline 只在 **HTTP 401** 时触发自动刷新，而 LINE
   服务端对过期 accessToken 返回的是 **Thrift 业务错误 code=119**（"Access token
   refresh required"，HTTP 层非 401）→ 刷新钩子永远不触发 → 账号登录后约若干天
   静默失效。实测：旧号 `Uk4bhjJ4…`（07-25 接入）今日 getProfile 报 119=已死。
2. **僵尸账号照常展示**：token 已死，但注册表 `platform_accounts.status` 仍是
   `online`，坐席账号卡显示在线；该号 10 个通讯录占位会话（norman/anna chen/
   melody/宝贝…）在聊天列表照常可点、可输入、可点发送——发送必然失败。
3. **worker 启动不验 token**：`LineProtocolWorker.start()` 只做
   `OkLine.from_tokens_file()`（纯读文件、零网络），死号 worker 也标 `running` →
   编排器 `owns()=True` → 发送进 okline 才炸出 `LineApiError 119` → 坐席看到
   502「protocol 发送失败: …code=119」天书；若 worker 恰好没起来，则回落 RPA
   适配器 → 本机无 line_rpa（adb）服务 → 503「**LINE 服务未启用**」——更误导。
4. **收线程死亡静默 + LINE 未接会话健康**：okline Bot 长轮询跑在 daemon 线程，
   `receiver 循环退出` 只打 **debug** 日志且**永不自动重启**；`LineProtocolWorker`
   是五个协议 worker 里唯一没接 `platform_session_health` 的（Messenger/WA/Zalo/IG
   都有 `_session_unhealthy` 快速失败闸 + `note_send_auth_failure` 登记）→ 掉线
   全程无告警、无红点。
5. **掉线窗口的客户消息永久丢失**：LINE 副设备协议**不下发历史消息**（代码注释
   已钉死：官方 Chrome 扩展登录后同样空列表）→ 掉线期间客户发的消息**补不回来**。
   实锤：今日 07:11 客户发的 "hi" 不在库里（新号 04:49 后 worker 失联，17:08 随
   实例重启才恢复）；7 天窗内 LINE 入站 **0 条**、出站仅 4 条（04:46-04:50 测试）。
6. **登录风控无引导**：QR 重新登录被 LINE 风控（"Verification is temporarily
   unavailable"），08-23 15:16 ～ 08-24 03:53 失败 **7 次**（用户连扫连败，越扫
   越封），直到 04:08 才登上新号 `UBStXHv7…`（displayName=vic001，token 活、
   E2EE ready，探测确认）。UI 对失败原因与「该等多久」零解释。

**用户的体验合成**：新号 worker 活着时消息进得来（「只能收到消息」）；点开的会话
一半属于死号、或 worker 正处失联窗（「发送不出去」）；重连扫码七连败；客户消息
黑洞丢失——最终「这个账号要删除」。

---

## 二、实证时间线与证据（117 zhiliao，2026-08-23 ～ 08-24）

| 时间 | 事件 | 证据源 |
|---|---|---|
| 07-25 | 旧号 `Uk4bhjJ4…` 接入（persona lin_xiaoyu），此后 token 从未续期 | 注册表 created_at；今日 getProfile 报 119 |
| 08-23 15:16 | QR 登录失败 code=10052 / checkQrCodeVerified | app.log |
| 08-24 01:57~03:53 | QR 登录再败 5 次（"Verification is temporarily unavailable"） | app.log |
| 08-24 04:08 | 新号 `UBStXHv7…`(vic001) 登上，worker 起，通讯录同步 | app.log + 注册表 |
| 08-24 04:46~04:50 | 坐席试发 4 条全部成功（tisay 会话 + LINE 官方号）——**新号发送链当时是通的** | inbox.db（7 天窗仅有的 4 条出站） |
| 08-24 04:49 后 | 新号 worker 失联（无任何 op；`receiver 退出` 是 debug 级，日志不可见） | 07:11 消息未落库反推 |
| 08-24 07:11 | 客户给 vic001 发 "hi"——**未落库=永久丢失**（副设备协议不补历史） | 报障截图 Vision 摘要 + 库中无此会话 |
| 08-24 07:23 | 内测群带图报障 +「这个账号要删除」 | app.log bug_intake |
| 08-24 17:08 | 实例重启，worker 随之恢复（通讯录再同步） | app.log |
| 今日探测 | 旧号 getProfile → `LineApiError 119`（token 死）；新号 getProfile → ALIVE + e2ee ready；两号注册表 status **都是** `online` | 只读探针 |

代码层证据：

- `okline/transport.py`：`if resp.status_code == 401 and allow_refresh and self._refresh_hook`
  ——**唯一**的刷新触发点；`auth.py::refresh_access_token`（/api/auth/tokenRefresh）
  实现完好但没人调它。
- `account_orchestrator.py::LineProtocolWorker.start()`：`from_tokens_file` 后直接
  `state="running"`，无探活。
- `_start_receiver()._run()`：`except Exception: logger.debug("receiver 循环退出")`
  ——线程死即终局。
- `platform_session_health` 消费者 grep：WhatsApp/Messenger/Zalo/Instagram 四家有，
  **LINE 无**。
- `channel_adapters.py::_send_via_rpa_queue`：无 RPA 服务时报
  501/503「LINE 暂不支持主动发送 / LINE 服务未启用」——协议号用户看到这句话
  完全无法自救。

---

## 三、三视角分析

### 3.1 用户（坐席 / 内测用户）视角

**他经历了什么**：
- 会话列表里 LINE 会话长得都一样，分不清哪些挂在活号、哪些挂在死号——死号的
  10 个占位会话可以点开、可以打字、可以点发送，点了才报错。
- 报错是天书：「LINE 服务未启用」（明明账号卡显示在线）、「protocol 发送失败:
  …code=119」（这是什么？我该干嘛？）。
- 重新扫码 7 次失败，没有任何「为什么失败、要等多久」的解释，只能反复扫（而
  反复扫正是 LINE 风控加重的原因——产品在诱导用户自伤）。
- 客户 07:11 发来的消息永远消失了，他和客户都不知道——**这是最致命的**：坐席
  以为没人找他，客户以为被已读不回。
- 最终情绪：「这个账号要删除」——不是要删账号，是对通道失去信任。

**他需要什么（按痛感排序）**：
1. 发不出去时**一句人话**：坏在哪、我现在该点哪里（「登录已过期 → [重新扫码]」）。
2. 账号死活**在列表里就看得见**（红点/置灰），而不是点发送才知道。
3. 掉线**自动恢复**，恢复后告诉我「x:xx~y:yy 掉线，期间客户消息可能未收到，
   建议主动问一句」。
4. 扫码失败给**冷却倒计时**，别让我连扫七次。

### 3.2 开发工程师视角

**为什么会走到今天**：LINE 通道是五个协议通道里最晚补齐运维件的——Messenger 有
send_auth_failure 登记 + relogin 按钮，WA 有断线重连 + unhealthy 快速失败，Zalo/IG
照抄了同款；LINE 只做了「能收能发」的功能面，**生命周期三件套（续期/探活/健康
登记）全缺**。且它的失效模式最阴险：token 慢性死亡（不像 WA 掉线有事件）、
副设备协议不补历史（丢了就是丢了）、receiver 是 daemon 线程（死了进程还活着）。

**工程判断**：
- 修复全部是**接线活**，不是新基建：refresh 函数 okline 已有、health 登记表已有、
  relogin UI 模式 Messenger 已有、watchdog 升级提醒已有——LINE 把这四样接上即可。
- okline 是自家逆向库，transport 层补「119 → 刷新重试」是最小侵入点（±15 行）；
  引擎侧再包一层兜底（`_api_call` 捕 119 → refresh → 单次重试）双保险。
- **观测缺口比功能缺口更值得修**：这次事故里没有一条 WARNING 级日志指向真相
  （receiver 死=debug、发送失败=前端 4xx/5xx 不落 app.log、token 死=无人探测），
  排障只能靠用户截图倒推。send 路由失败必须 warning 落日志。
- 测试面：token 刷新（mock 119→refresh→重试成功/失败两路）、start 探活门禁、
  session health 接线门禁、回落报错文案门禁——全部可 pytest 化，零真机依赖。

### 3.3 市场经理视角

- **LINE 是日台泰与东南亚市场的第一大 IM**，是「多平台 AI 客服」对外叙事里除
  Telegram/WhatsApp 外最重的一张牌。内测用户**第一次**认真用 LINE 通道就遇到
  收发半瘫 + 扫码七连败 + 客户消息丢失——这是信任级事故：客户丢的不是消息，
  是询盘和单子。
- 我们的差异化卖点是「个人号直连、零企业认证门槛」（对比 LINE 官方 OA API 要
  认证+按量计费）；逆向协议天然换来稳定性折价，**这个折价必须用「自愈 + 透明」
  买回来**：掉线分钟级自愈、死活状态实时可见、丢失窗口主动告知。卖「个人号
  直连」不能卖成「薛定谔的在线」。
- **预期管理**：LINE 通道对外标注 Beta；SLA 口径用可守的（「掉线 5 分钟内自动
  恢复并告警」）而非「永不掉线」；封号/风控风险写进使用建议（扫码冷却、
  单号并发上限）。
- **把修复变成弹药**：LINE 通道健康 SLI（在线时长占比、发送成功率、登录成功率、
  掉线 MTTR）进 ops 看板并留存——30 天后这些数字直接进销售材料与官网合规能力页
  （实施45 的既有版面）。
- 营销价值排序与工程排序一致：报错人话化（感知最直接）≥ 死活可见（信任）>
  token 续期（根治）> 丢失告知（诚实即品牌）。

---

## 四、优化方案

### P0 止血 + 根治主因（本周）

| # | 改动 | 落点 | 验收 |
|---|---|---|---|
| P0-1 | **token 自动续期**：okline transport 把 Thrift `code=119`（及 401 同族 auth 错误）并入 `_refresh_hook` 触发条件，刷新成功后原请求重放一次；`from_tokens_file` 场景刷新后回写 tokens 文件（已有 `_persist` 钩子） | `okline/transport.py`（±15 行） | 单测：mock 119 → refresh 被调 → 重放成功；旧号真机探测 getProfile 由 119 → 200 |
| P0-2 | **启动探活**：`LineProtocolWorker.start()` 建 client 后调一次 `get_profile()`（线程池，超时 8s）——119 且 refresh 失败 → `RuntimeError("LINE 登录已过期，需重新扫码")` → worker 进 failed 态（不再假 running） | `account_orchestrator.py` | 死 token 账号 worker 不得进 running；账号卡显示离线 |
| P0-3 | **LINE 接会话健康**（对齐 Messenger 模式）：send/`_api_call` 捕获 auth 类 LineApiError → `note_send_auth_failure("line", account_id, detail)`；`send`/`send_media` 入口加 `_session_unhealthy` 快速失败闸 | `account_orchestrator.py`（LineProtocolWorker） | pytest：auth 失败一次后，后续 send 快速失败且账号卡红点 |
| P0-4 | **报错人话化**：坐席发送失败三分类——`login_expired`（119/auth → 「该 LINE 账号登录已过期，请到渠道中心重新扫码」+ relogin 深链）、`not_connected`（无 worker 无 RPA → 「该 LINE 账号未接入或已退出」）、`send_failed`（其余 → 「LINE 发送失败，请稍后重试」）；同时 send 路由失败落 **WARNING** 日志（platform/account/错误类，本次事故 app.log 零痕迹的直接补丁） | `unified_inbox_send_routes.py` + `channel_adapters.py` + `errors.py` i18n pack（zh+en） | 门禁：三类场景返回对应键；日志有 warning 行 |
| P0-5 | **注册表僵尸降级**：watchdog 巡检（复用 `session_stale_remind` 族）——`status=online` 但 worker 连续 N 轮 failed/token 探活死 → 注册表降 `offline` + ops 告警（webhook 别名 `platform_session`），坐席界面即刻显示「已退出，点重连」 | `health_watchdog.py` | 旧号 `Uk4bhjJ4…` 在一轮巡检内被降级并告警 |

### P1 防复发 + 观测（下周）

| # | 改动 | 说明 |
|---|---|---|
| P1-1 | **receiver 自愈**：`_run()` 异常退出 → WARNING 日志 + 指数退避自动重启（1/2/5/15min，上限后标 unhealthy 交 P0-3 链）；worker status 暴露 `last_op_ts`（最近一次收到 op 的时刻） | 「收线程死=永久失聪」是 07:11 丢消息的直接机制 |
| P1-2 | **失联巡检**：watchdog 检查「worker running 且 token 活，但 `last_op_ts` 超阈值（默认 2h）且非账号静默时段」→ 主动 getProfile 探活 + 重启 receiver | 防「假活」——SSE 半死不报错 |
| P1-3 | **掉线窗口告知**：worker 从 failed/unhealthy 恢复时，往该账号会话流插系统提示「⚠ x:xx~y:yy 本账号掉线，期间客户消息可能未收到，建议主动问候一句」（账号级 banner + 会话内系统行） | 副设备协议补不回历史（硬限制），只能诚实告知 + 引导补救；这是把「丢消息」从事故变成可运营动作的唯一方式 |
| P1-4 | **QR 登录风控退避**：连续失败 ≥2 次 → UI 冷却倒计时（30min 起步递增）+ 失败原因翻译（"Verification is temporarily unavailable" → 「LINE 暂时限制扫码验证（风控），请约 30-60 分钟后再试，连续重试会延长限制」）；登录失败原因进 `login_funnel_stats` 既有漏斗 | 用户连扫 7 次正是风控加重的原因，产品必须拦住这个自伤循环 |
| P1-5 | **门禁**：`tests/test_line_worker_lifecycle.py`（token 刷新/探活/health 接线/receiver 自愈/报错分类，全 mock 零真机） | 对齐平台能力矩阵门禁风格 |

### P2 产品化（跟随）

| # | 改动 | 说明 |
|---|---|---|
| P2-1 | **LINE 通道 SLI 进 ops 看板**：在线时长占比、发送成功率、登录成功率、掉线 MTTR（数据源=platform_session_health 事件 + send_route_stats 既有计数） | 30 天后数字进销售材料/官网合规能力页 |
| P2-2 | **僵尸账号清理**：渠道中心对「offline 且 tokens 过期」账号给「重新扫码 / 删除」双按钮；占位会话（1970 时间戳）在死号下折叠隐藏，不再与活号会话混排 | 直接回应「这个账号要删除」 |
| P2-3 | **官方 OA API 双轨**：对企业客户给 LINE 官方通道选项（`official_api_worker` 骨架已在）——个人号直连（Beta，灵活）与官方 OA（稳定，需认证）双轨话术 | 市场侧预期管理的产品支撑 |

### 刻意不做

- **不做**「掉线期间消息找回」：LINE 副设备协议不下发历史是服务端硬限制，任何
  「补拉」方案都是虚假承诺；资源投给缩短掉线窗口（P0-1/P1-1 后从「天级」到
  「分钟级」）+ 诚实告知（P1-3）。
- **不做**收件箱层的「LINE 会话只读锁」：P0-3 的快速失败闸 + P0-4 的人话报错已
  覆盖；再加只读锁会在 worker 恢复瞬间产生状态不同步的新麻烦。
- **不动** line_rpa（adb）回落链：本机部署形态没有 LINE 真机，RPA 回落是其他部署
  形态的能力，不因本事故改语义（只改它的报错文案）。

---

## 五、风险与依赖

1. **okline 改动风险**：transport 是所有请求的咽喉，119 重试必须带「单次」护栏
   （防 refresh 成功但业务仍 119 时的无限循环）；改后跑 okline selftest + 引擎
   LINE 门禁。
2. **tokenRefresh 本身可能被风控**：若 refreshToken 也失效（旧号可能已是这种状态），
   刷新失败 → 走 P0-2/P0-5 的显式失效链，绝不静默重试轰炸（每账号刷新失败后
   冷却 ≥1h）。
3. **重启纪律**：P0 全部是 `.py` 改动，攒批进每日重启窗（04:00/12:30/22:30），
   走 `restart_preflight.ps1` → `restart_instance.ps1 -Instance zhiliao`。
4. **多线协作**：`account_orchestrator.py` 是热区，动工前跑 `agent_probe.ps1` 探活
   + 意向板登记（本文档即意向声明）。

## 六、验收口径（事故场景回放）

1. 旧号（token 死）：巡检一轮内账号卡转「已退出」+ ops 告警；点其会话发送 →
   「该 LINE 账号登录已过期，请重新扫码」+ 深链按钮；app.log 有 WARNING。
2. 新号：accessToken 人为改坏 → 首次请求 119 → 自动 refresh → 发送成功，全程
   坐席无感；tokens 文件已回写新 token。
3. kill receiver 线程 → 1min 内自动重启 + WARNING 日志；连杀 5 次 → 账号标
   unhealthy + 告警。
4. worker 恢复后，账号 banner 出现掉线窗口提示。
5. QR 连败 2 次 → 登录页出现冷却倒计时，按钮禁用。
