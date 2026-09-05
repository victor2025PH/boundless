# I 批进度总账（跨对话 / 跨账号交接的唯一入口）

> **这份文件的作用：** 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**，
> 判断该做什么、别重做什么。不依赖聊天记录。
>
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行。
> **只改自己那一行**，别动别人的行（这份文件也在共享工作树上）。
>
> 建立于 2026-09-04（值守线）。背景对账见 `docs/报障对账_钧_花无缺_20260904.md`。

## 一句话现状

1.0.72 的 P1 修复分 6 条指令并行/串行推进，**不发版**。
H 批的对话 B（B1–B4）已完成，对话 A 的 A1 已完成，A2/A3/A4/A5 拆进了本批。

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **I-1** | #160 风险扣稿全放行（影子台账档）+ 分类器整词化 + #162 观测补丁 | #160 #162 | **$150** | 已完成（2026-09-04 10:4x，含二批） | B `da7a8787` / A `58aed354` / C `a667783c` / D `10dc0a84`；二批台账维度+放行去向 `067a0b62` `28864be4`；三批 peer_msg_id+delivery_failed `fb9a04be`；四批加固 `ca83333d`；五批收编协议号自动回复 `79688321`；验收 10/10（含 enforce 反向验证）；**不发版不重启**，装载等下个重启窗 | 交 I-2：`unified_inbox.html` 两处 `?v=`（cp-voice.js / cp-i18n.js）bump 到 20260904a（他线存量红 + 本条词典）；**待老板拍板**：桌面壳 `/api/desktop/guard-check` 对坐席「填入并发送」文本的 `block: risk==high`（人在环 UI，属桌面客服产品线）是否也走影子；一个月后读台账定 enforce 规则表（`autosend_policy.decide` enforce 分支） | `发版对账_v1.0.72_A.md` | ≈$55（估） |
| **I-2** | #158 审批页进得去出不来 + #154 铆定收尾 + 建 1.0.72 发版对账 | #158 #154 | $190 | 已完成（2026-09-04 10:5x） | A2 `c0412669`（定层＝壳层：收件箱 webview 自漂子页 + renderer 只切标签＝静默吞点击；三层修 + desktop 44 断言门禁）/ §5 `?v=` `69316cc3` / 尾巴 c 复核已在 `2afc4e0f` 未重做 / 尾巴 d `8ff7f422`（✎ 行 `_origReadable` 锁「收→」）/ A5 `3f844829`（`发版对账_v1.0.72.md` 骨架 + 症状族族2第5层·族4第9层）/ 顺手 `2e36ebdd`（#156 封顶层坐席文案，gate_sweep 存量红）；**不发版不重启**，模板/i18n 热更已生效、壳侧随包 | ① #158 打包态两条进入路径 + renderer.log 无 Uncaught 需 1.0.72 包在坐席机实跑（代码层三处契约已钉）；② 对话 B 交的 `unified_inbox.html` 三项 UI 未落地：账号栏「未选人设」入口 / 未读数字可点直达（#159 `account-unread`）/ 双向称呼两格（#155）——见 `发版对账_v1.0.72.md` §三；③ I-3/I-4/I-5 结束报告里交 I-2 的前端落点待收；④ 他线红未动：`test_alert_delivery_e2e`（phantom_unread 未提交在途）、`test_stale_update_prompt` ×3（I-5 的 app.html） | `发版对账_v1.0.72_I2.md` | ≈$70（估） |
| **I-3** | 工作目标不推进（节奏档接线 / 摸底硬约束 / 画像兜底） | #153 #65 #40 #38 #152② | $190 | 已完成（2026-09-04 11:4x） | C1 `e95994c4`；C2 `b24266ea`；C3 画像 grounding 兜底（`memory_grounding` 只读复用 + `llm_pending` 待确认，见 `发版对账_v1.0.72_I3.md`） | 无（交 I-2：画像卡「待确认」chip 读 `src=llm_pending`） | `发版对账_v1.0.72_I3.md` | ≈$130（估） |
| **I-4** | 地区语气自适应 + AI 自动链引用回复 | #40 #36后半 #37 | $190 | 已完成（2026-09-04 11:4x） | D1 `907faa83`（**新** `src/ai/persona_region.py` SSOT：zh-CN/zh-TW/zh-HK 三档 profile + 六级解析 显式字段→dialect_flavor→居住地国家码→会话「发→zh-tw/yue」→全局默认→CN；`spoken_style_bridge.system_block(context=)` 追加地区 L1 块；禁用词只观测不改文本；**CN 档逐字回归=空块**；23+56 例绿）/ D2 `70af9595`（**新** `src/inbox/reply_quote_policy.py` 纯函数：未回复入站 ≥2 且最相关候选过地板才引用、单条恒不引用、同分取更早；`autosend_helpers._send_one(reply_to)` 首条带引用、失败去引用重发；只在编排器路径；config.example 默认关，**zhiliao overlay 已开** `quote_reply.enabled:true`；25+59 例绿）/ 优化① ④ `d3a41597`（`quote_reply` 挂 autosend-status、`persona_region` 挂 metrics、ops 新卡「地区语气档·引用回复」；HK 字形硬钉子 + 简体漏出观测 `script_hit/observed`）/ 优化②③前置 `f1e324a7`（**新** `src/ops/region_quote_gray.py` 持久账本 `logs/i4_gray/*.jsonl` 不落原文 + `tools/region_quote_gray_report.py` 周读：建议 min_relevance / L4 判词；9 例）；**不发版不重启**，D1/D2 .py 已被 watchdog 12:26 重启顺路装载，优化批 .py 等下个窗 | ① 灰度验证：TW/HK 人设会话看 L1 块 + `[reply_quote] 引用` 日志；② 交 I-2 `unified_inbox.html`（可选）：会话级「地区语气」选择器写 `_spoken_region`（见 I4 对账表结束报告）；③ **一周后**跑 `python tools/region_quote_gray_report.py --days 7`——按「建议 min_relevance」调 overlay `inbox.l2_autosend.quote_reply.min_relevance`、按 L4 判词决定是否上负样本改写（数据闸口：low_relevance ≥20 / observed ≥30） | `发版对账_v1.0.72_I4.md` | ≈$60（估） |
| **I-5** | 老单清理五小件（LINE 媒体 / 克隆声 / 人设串味 / 删除同步 / UI） | #145 #32 | $190 | 部分完成（2026-09-04 11:5x）→ **代码侧剩余项已由 K-1 收口提交（09-05 20:3x）** | E2 不施工（B1 `929ba165` `23cb7dba` 已拆「疑似无声/念错」）/ E3 `bdae979e`（目录按人设过滤 + persona_guard 出站剥）/ E4 `60c2d428`（withdrawn_cite：记得但不主动提）/ E1 `96cf53cd`（探针拣 online + magic）/ E5③ `96b59d73`（群数量变化 INFO）；**K-1 收口**：E3 另一半 `pick_products` 传 `persona_id` `71355568` / E4 另一半 `apply_to_reply` 接 `skill_manager` 出稿口 + `drafts.enrich_draft` `c468483a` / E5① 悬浮清单下线 + I-2 交接三项 UI（#159 未读可点 / #156 未选人设入口 / #155 双向称呼）`69e62aba` / guard-check 影子（I-1 遗留）`82ee11a5`；`.py` 等 K-5 ①段重启装载 | E1 语音/视频/贴纸双向真机未跑（探针只覆盖图；群聊入站默认关）；~~E5① 悬浮清单等确认未删~~（`69e62aba`）；~~E4 `sanitize_outbound` 未挂发送链~~（`c468483a`）；~~E3 `service.py` `pick_products` 未传 persona_id~~（`71355568`）；~~交 I-2 三项 UI~~（`69e62aba`）；交 I-2 气泡隐藏/群组折叠仍未落；交 I-6 E2+E5②+E1 钧侧验收 | `发版对账_v1.0.72_I5.md`；K-1 收口见 `发版对账_v1.0.74_K1.md` §1 | ≈$120（估） |
| **I-6** | 61 单回访冲刷 + zl_collect 下发 + 台账校正 + 监控补窟窿 | #64 #36 #61 #63 #67 #145 | $190 | 进行中（2026-09-04 10:4x，等老板点头两份文案 + F3 台账写入） | F1① 结论出（三层：CLI 标 fixed 不触发回访 / flush 无计划任务 / 无积压告警）；补洞 `98323a00`（--alert + DutyNotifyBacklog 6h + 汇总记账 CLI）；F2 两机文件已发 mid 1157/1158 `c83d7ddd`；F4① `ffb745d9`；F4② 已答复(reply-to 1156)；F4③ 归属=对话 B #159 看门狗段（未提交，非本线） | F1② 发两份汇总回访（等点头）→ F1③ `duty_notify_summary --apply`；F3 台账写入（dry-run 已备，等点头）；F2 等两机 selfcheck 短码；收尾三份文档 | `发版对账_v1.0.72_I6.md` | ≈$35（估） |

状态取值：`未开工` / `进行中` / `已完成` / `部分完成(等下一账号)` / `阻塞(原因)`

## 指令文件在哪

| 指令 | 文件 |
|---|---|
| I-1 | `docs/指令_I-1_放行策略_2026-09-04.md` |
| I-2 | `docs/指令_I-2_审批页与铆定收尾_2026-09-04.md` |
| I-3 | `docs/指令_I-3_目标推进_2026-09-04.md` |
| I-4 | `docs/指令_I-4_地区语气与引用回复_2026-09-04.md` |
| I-5 | `docs/指令_I-5_老单清理_2026-09-04.md` |
| I-6 | `docs/指令_I-6_值守流程与积压_2026-09-04.md` |
| 共同纪律 / 省钱纪律 / 文件归属矩阵 | `docs/修复指令_I批_并行_2026-09-04.md` |

## 投放顺序与依赖

```
I-1 ─────────────► I-4（D2 碰发送链附近，必须等 I-1 提交）
I-2 （持有 unified_inbox.html + store.py，其他条的前端落点都汇给它）
I-3 （独立）
I-5 （独立，但 store.py / unified_inbox.html 落点交 I-2）
I-6 （零代码风险，随时可与任何条并行）
```

- **推荐顺序：I-1 → I-6 → I-2 → I-3 → I-4 → I-5**
  （I-6 排第二是因为它最便宜、且 61 单回访对用户信任的修复效果比任何代码改动都大）
- **硬依赖只有一条**：I-4 的 D2 必须在 I-1 之后（D1 无依赖，可先做）
- **文件独占**：`unified_inbox.html` 和 `store.py` **只有 I-2 能改**。
  I-3/I-4/I-5 需要动这两个文件的，把「插在哪 + 改什么 + 文案」写进结束报告交 I-2。

## 老板决策记录（**会影响施工，先看这里再看指令**）

| 日期 | 决策 | 影响 |
|---|---|---|
| 2026-09-04 | **风险扣稿 v2：一条不留，全部放行，触发只写台账。** 四条硬底线（`stop_contact` / `self_harm` / AI 稿 `reply_risk=high` / 客户索要凭证）**全部取消**。攒满一个月真实数据后再定新规则。台账 ops_only，坐席侧零改动。 | I-1 全文按此写；`docs/修复指令_H批_并行AB_2026-09-04.md` 的 A3 策略段**已作废** |
| 2026-09-04 | 手机端删除消息 → **同步删除**（显得专业）；AI 可以有记忆但不主动提删掉的信息 | I-5 的 E4 |
| 2026-09-04 | 预算：I-1 $150，I-2..I-6 各 $190 | 各条 §0 已写死上限与"该停下来"的阈值 |
| 2026-09-04 12:5x | **桌面壳 `/api/desktop/guard-check` 对坐席「填入并发送」的 `block: risk==high` 也改影子**（只记录不拦）。理由与 I-1 同：坐席自己按的发送键不该被后台判高风险拦下，且拦了坐席不知道为什么。 | I-1 遗留的待拍板项已定；施工归桌面客服产品线，落点 `guard-check` 路由 + 影子台账复用 `autosend_shadow_log` |
| 2026-09-04 12:5x | **I-5 的 E5① 悬浮清单：删掉**（#32 报的就是它多余） | I-5 剩余项，需接手方施工 |
| 2026-09-04 12:5x | 收尾顺序：**先打 1.0.72 包装坐席机**，把 #158 打包态两条路径 + I-5 的 LINE 语音/视频/贴纸真机验收一起做掉；I-6 的 61 单回访冲刷排其后 | 本条即当前在做的事 |

## 换账号 / 额度用尽的通用流程

**收工方（额度将尽时立刻做，别等做完）：**
1. 已完成的工作**必须已 commit**（dirty 树上的未提交工作在这棵多线共享树上是裸奔状态）
2. 写落点表 `docs/发版对账_v1.0.72_<本条>.md`，**未完成项也留一行**写「已改 X，还差 Y，下一步做 Z」
3. **更新本文件里自己那一行**（状态 / 已完成 / 剩余 / 已花）
4. 清意向板：`scripts\agent_probe.ps1 -Intent "<同一主题串>" -Done`

**接手方（新账号的新对话）：**
每条指令文件的最后一节都有一段现成的接力话术，直接粘。通用形态是：
```
接手 I-<N>（<主题>）。按顺序做三件事，不要重新分析已完成的项：
1. 读 D:\boundless\docs\I批_进度总账.md 找 I-<N> 行，看已完成到哪
2. 读 D:\boundless\docs\发版对账_v1.0.72_<本条>.md —— 这是断点，逐行核对
3. 跑 git -C D:\boundless log --oneline -15 -- engines/chengjie 确认第 2 步的 commit 真在
然后读 D:\boundless\docs\指令_I-<N>_*.md，从落点表第一个未完成项继续。
预算上限 $<N==1?150:190>（含前一轮已花的，按总账里记的余额算）。
```

⚠ **接手方不要相信总账里"未开工"就直接开工** —— 先跑第 3 步的 `git log` 核一遍。
上一轮可能额度断在"改完没来得及更新总账"那一刻。以 **commit 和落点表为准，总账是索引**。

## 复查记录（2026-09-04 13:2x，值守线全批核验）

**结论：未全部完成。** 六条里 I-1/I-2/I-3/I-4 代码已落地，I-5 部分、I-6 卡在等老板点头。

已核实为**真的做完了**：
- **I-1 在真流量上验证成立**：坐席机 kouxing `logs/autosend_shadow/shadow_20260904.jsonl`
  有 `would_hold_level=L4 / hold_reason=adult / policy_mode=shadow / kind=hold` 的记录，
  二三批新增字段（lang/intent/emotion/persona_id/peer_text_fp/peer_msg_id/peer_msg_match）全齐——
  旧策略会扣稿转人工的稿子，v2 下**放行了且只留台账**。zhiliao 侧 0 条（12:53 才装载，窗口短）。
- **1.0.72 已装三台坐席**（yunsheng / kouxing / lianbei 全部 UP / 1.072），
  三台 `renderer.log` 的 `Uncaught` 计数都是 **0**（#158 的"无 JS 异常"这一半成立）。
- **广域门禁 792 过 / 1 红**：唯一红是他线在途的 `test_alert_delivery_e2e::
  test_source_alerts_all_have_e2e_payload`（`phantom_unread_alert` 未提交，红龄 2.8h，非本线）。

核实为**仍然没做**（有代码证据，不是猜）：
- **I-5 E4 `withdrawn_cite.sanitize_outbound` 全库无调用方**（只在 `withdrawn_cite.py:140`
  定义、`:215` 导出）——"记得但不主动提"的**出站那一半没接线**，入站半边有效。
- **I-5 E3 `pick_products` 两个调用点都不传 `persona_id`**（`goals/service.py:1278`、
  `web/routes/goal_routes.py:190`），而 `_persona_id` 这个回落键**全库没有任何写入方**
  → `pid=""` → `offers.visible_to_persona` 是 fail-closed → **绑定了人设的产品在
  目标/选品注入链上一条都出不来**。串味确实修好了，但代价是绑定货全黑，需要两个调用点
  把 `persona_id` 传进去（`service.py:1386` 已有 `resolve_account_persona_id` 可用）。
- I-5 E5①（悬浮清单，老板已定"删"）、E1（LINE 语音/视频/贴纸真机）未动。
- I-2 交接的三项 `unified_inbox.html` UI 未落地（未选人设入口 / 未读数字可点 #159 / 双向称呼 #155）。
- **I-6 三项全卡在等老板点头**：F1② 61 单回访文案（复查仍是 61 单：skuio 47 / 钧 13 / 其他 1）、
  F3 台账写入、F2 两机 selfcheck 短码。工单库 25 单非终态（`new` 6 / `verified` 4 /
  `confirmed` 15），其中 I 批那些**代码已修、台账没跟**——正是 F3 要清的账。
- **I-4 优化批未装载**：`d3a41597`(13:08) / `f1e324a7`(13:17) 晚于 zhiliao 末次重启
  （12:53:35 watchdog_start），等下个重启窗。

**本次复查新发现的 P0（1.0.72 装机触发，已止血）**：
- kouxing 装完 1.0.72 后后端**启动 85 秒即自杀**：
  `ValueError('too many file descriptors in select()')` → 防幽灵 exit 78 → 坐席掉线约 5 分钟。
  根因＝**入站语音落库前转录没有并发闸**：`unified_inbox_account_routes.py:1506`
  每条入站语音各自 `await transcribe_voice_message`（25s 墙钟），而 WhatsApp 边车在冷启动时
  把积压语音一次性灌进来（日志里 13:17:06–13:17:17 十几路 `调用OpenAI Whisper API` 并发），
  `voice_transcriber._transcribe_impl` 还**每次新建 `openai.OpenAI` 客户端**（各带连接池）
  → FD 数越过 Windows `select()` 的 512 上限 → uvicorn 事件循环整体异常终止。
- **第二个 bug：exit 78 之后壳不会重新拉起后端**——壳进程（pid 8044）一直在 `SYN_SENT`
  空打 18799，无人 LISTENING，坐席看到的就是"一直连不上"。要人工 stop+relaunch 才回来。
- 处置：`stop_chatx_node.ps1` + `relaunch_chatx_node.ps1` → 后端 pid 3636 已连续心跳 180s+
  （越过 85s 那个崩点）稳住；另两台该崩溃计数为 0。
- 待修（**未施工**，且当前有他线正在改 `skill_manager.py` / `rate_limiter.py` /
  `account_limiter.py`（意向板"unlimited outbound mode"），碰发送链要先避让）：
  ① 入站 ASR 加并发闸（信号量，建议 3）+ 复用单个 OpenAI 客户端；
  ② exit 78 让壳侧要么重拉后端、要么弹红条告诉坐席（现在是静默死）。

## 全批收口（6 条都完成后，由值守做）

1. 合并六份落点表进 `docs/发版对账_v1.0.72.md`（I-2 建的骨架）
2. 跑一次全量回归：`python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread`
3. 跑 `scripts\gate_sweep.ps1 -Full`
4. 更新 `docs/值守症状族对账单.md`
5. 才谈发版
