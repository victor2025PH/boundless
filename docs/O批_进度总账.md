# O 批进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。**每个 commit 落地后立刻补落点表一行。**
> 建立于 2026-09-08 05:5x。公共底座 `docs/修复指令_O批_并行_2026-09-08.md`（去向 / 归属 / 决策 D-O1–D-O6）；复盘 `docs/三日复盘_2026-09-05至08.md`。

## 一句话现状

09-08 00:49–02:30 skuio 14 份报告：WhatsApp 客户明说「你是 AI，别再写了」后系统仍连发两条（`stop_contact` risk=high 直发——09-04 放行无豁免）；一天 5 条 em dash、客服三段式、固定 12 秒回复、账号名≠人设名；干净包记忆抽取从安装起为死（`memory.extract.intents` 不在最小配置）；目标摸底 100% 被动（主动腿 8h 未发）。N 批四线 + M-7 代码齐待 N-5 打 1.0.77。
**O-1 A（停联硬停）与 O-2 A（记忆基线）先开，止血件可并入 1.0.77；其余并行。**

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **O-1** | 拟人与反识破 | #252 #253 #254 #255 | $200 | **进行中：A 已收工**（1.0.77 07:43 已发、止血件未赶上 → A 随 1.0.78 第一批） | **A** `ddc5d282`（stop_contact / self_harm 硬停「最多一条」+ 冻结切人工 + risk=high 转人工审 + 词表补 never write me again / 拉黑；新 `src/inbox/stop_contact.py`；全 .py 需重启；65 新测 + 相邻 1380 绿，3 既有红非本线） | B 去 AI 标点 + 句式 / C 陪伴系统提示词 + 客服腔守卫 / D 拟人节奏默认档 / E 账号名提醒 + 出站落日志 + 盲评门禁 | `发版对账_v1.0.78_O1.md`（§〇 已答「为什么 09-04 放行没豁免」；待老板点头：self_harm 按「AI 一句陪伴照发 → 冻结切人工」解读） | ≈$35 |
| **O-2** | 记忆抽取基线 | WYNN22 → **#256**（P0，O-5 08:30 自 #201 改立；commit 文案里的 #201 即此单）| $100 | **全部收工（A/B/C/D）→ 随 1.0.78 第一批**（1.0.77 07:43 已发，止血未赶上） | **A 止血 `ad5c7dbd`**：`memory.extract.{enabled,use_llm,intents}` 进 feature_registry A 类 + min 种子首带 memory.extract 块 + 启动日志四态说真话（白名单空即 WARNING 不写「已启用」）。**B `45b56b64`**：origin=manual 出站同走抽取（客户这一轮的话作 user_msg、坐席句作 reply、同桶同链、两道去重、`source=manual_out`）。**C `6b42dc10`**：`memory_scope` 单一来源（生日 / 日期 / 第一二人称属性强锚点，星期·钟点·场所合取），DailyLearner 分流切到它 + `[episodic] scope=` 去向日志（验收三例过）。**D `63daf5e3`**：记忆页「自安装以来新增 N 条 · 今日 +T · 已转正 P」+ 入站 ≥50 且 N=0 红字链 /logs。测试：A 87（3 预存红 bubbles 遗留）+ episodic 160 / B 94 / C 152 + i18n 22 / D 87 + i18n 全量 333，全绿。docs：`21a02beb` `1a9b558b` `b32deec5` + 收工 | 交下批 / 待拍板：`memory.consolidation.enabled` 缺省关（客户机永不晋升 stable）；手机端镜像出站不抽；zh_hant regen 随 N-5；回访文案在落点表 §5.1 交 O-5 | `发版对账_v1.0.78_O2.md`（§〇 「基线键清单核对」五分钟步骤） | ~$75 |
| **O-3** | 目标摸底主动腿 | HM7XBA 5XRQ7C → #257（挂 #236 族） | $150 | **收工**（09-08 11:1x；`.py` 十一处待装载，`cp-goal.js` 双树 / 三宿主 `?v=20260908d` / i18n 已热更） | §〇 根因五选一＝**没到期**（自然档 day 0 不排 + 10–20 窗口；watchdog 4h/8h 对自然档误报）+ 次日客户来消息 `consumed` 掐死主动腿（嫌疑①）；采集链纠偏（槽位采集≠情景记忆抽取，真因是从没问）。A `c3c64176`（`due_daily` 摸底目标 consumed≠asked 不让位 / 主动拍钉问法 / 全局 stall 按首拍资格起算 + 逐目标 WARNING / probe 分型 / `frozen` 闸）/ B `5d1cc20b`（第 1 天起每天 ≥1 问句进计划 / 30min ≥5 条活跃提前 / `[goal-plan]` / 里程碑 0「不急着问」文案退场 / 英文反查）/ C `183e43c1`（线索词→槽位 / 【本轮必问】硬行 / 出站校验 `probe_asked|missed` + 同槽重试 / `[goal-inject] target= cue= result=` / 注入计数）/ D `26d8a0e0`（卡片三计数 + 零出手红字按首拍资格 + 采集不可用黄条两条真实原因 / i18n 42 键三语 / `stage_hunks` 选择性暂存）/ E `1d41c7da`（`probe/preview` + `probe/send` 经 care `send_now` 三闸 + 冻结检查 → `beat_sent` 计入主动出手 / `[goal-probe] manual=1`）+ `36e2e754` 测试 loop 修。tests 32 例 + 4 处随改；全量 `-k goal` **1164 passed** | 路由清单红＝微信 KF 线 3 个在途路由（非本线）；`profile_llm` 进基线待老板拍板 **D-O3-1**；O-1 A 落地后对齐 `_FREEZE_META_KEYS` 键名一处；回访口径见落点表 §四 交 O-5 | `发版对账_v1.0.78_O3.md` | ≈$110 |
| **O-4** | 引用回复 MVP | 47KNBV 3HNCJ7（挂 #254） | $120 | **一期收工**（09-08 09:0x），随 1.0.78 | A/B `1c4ced51`：规则引擎（必引 burst/stale>30min · 随机 10–15% 会话级种子 · 禁引 short_quick/连引冷却/每小时≤6/self≤5% · 历史从消息行 reply_to_id 推导）+ 能力位 TG/WA（LINE/Messenger `rule=unsupported`）+ `[quote] conv= reply_to_mid= reason= rule=` 每条出站一行 + 默认开（代码缺省）；C `63c66713`：设置页拟人节奏区「引用回复」开关（FIELDS 一键、feat 探测、i18n）；D 二期 $150 / 三期 $120 评估 + 3HNCJ7 答复要点写进落点表 §评估（交 O-5）；48+2 tests 绿 | 无（`answers_mid` 与多条各引归二期；LINE 能力位二期翻格）。交回：`test_platforms_match_worker_registry` 红＝DY 线 douyin 未进 `PLATFORMS`（落点表 §四）；`config.example.yaml` quote_reply 注释「默认关」过期 | `发版对账_v1.0.78_O4.md` | ≈$100 |
| **O-5** | 值守（不与 N-5 重叠） | #252 #254（confirmed P0）/ **#256**（WYNN22 → O-2）/ **#257**（HM7XBA → O-3）/ #253 #255 closed | $60 | **收工**（09-08 08:5x） | **A** 08:34:31 回 3HNCJ7 挂 #254 + confirmed（`.ops/duty_0908/o5_r254_3hncj7.txt`，2,076 字：一件事一个单号、只给版本号——止血 O-1 A / O-2 A 随 1.0.78 第一批、O-1 B/C/D + O-3 + O-4 一期 = 1.0.78、二期 / 三期估算排 1.0.79、出站落日志在 O-1 B、验证留 1.0.78 回访；**1.0.77 07:43 已发、止血件未赶上**）/ **B** `tmp/o5_b_ledger.py` dry-run → apply 08:30：WYNN22 自 #201 摘出改立 **#256**（P0 v1.0.76，#201 note、ticket.txt、evidence received）；HM7XBA / 5XRQ7C 自 #255 摘出改立 **#257**（P1，挂 #236 族不用 dup_of，#236 / #216 note）；#253 → #252（六码归 #252）、#255 → #254（TDNJHQ 归 #254，P0 随并入抬）；#252 P2 → P0（D-O1）；四条回声全带单号 08:34:42 #256 / 08:34:52 #252 confirmed / 08:35:03 #257；N-5 A 范围核对不重叠、`duty_auto_ticket.py` 未碰 / **C** 落点表 §5 回访清单（#252 / #254 / #256 / #257 / #254 兑现 五行，O-2 A 口径已填、其余草稿待各线定稿）；底座去向表补 #256 / #257；O-2 落点表头补 #256 | 三日复盘一句话（§6 草稿）等老板拍再发；O-1 / O-3 / O-4 收工时把回访文案填进 O-5 落点表 §5（或各自 §回访，1.0.78 发版线汇总）；O-4 欠 3HNCJ7 二期 / 三期估算回 #254 | `发版对账_v1.0.78_O5.md` §0–§7 | ≈$28 |

## 老板拍板记录

（默认值见底座「老板决策」表；老板改口在这里追加一行：日期 / 决策号 / 改为）

## 全批收口（随 N-5 E 或 1.0.78）

- [x] ~~O-1 A / O-2 A 若在 N-5 打包前收工 → 并入 1.0.77~~ 1.0.77 07:23 已发，止血件未赶上 → 全部随 1.0.78
- [ ] 其余随 1.0.78：装载 → 回归 → 三包 → 三渠道 → 两机 → 台账 → 回访（含 3HNCJ7 排期答复兑现）
- [ ] 发版门禁：200 条样例 0 em dash / 0 分号 / 0 总结尾句；50 条拒绝·冷淡·告别场景盲评「像真人」

### 验收核对 09-08 11:45（值守，在 HEAD `d303ad05` 的干净临时工作树上跑，不含任何在途未提交文件）

- **O 批自有测试 18 文件：480 passed / 1 skipped / 2 failed**，两条红都不是 O 批的：`test_takeover_trigger_surface::test_mode_writer_files_are_allowlisted`（`src/assistant/actions.py` 小智线 09-02 写点未登记，O-1 落点表已注「既有红未动」）；`test_admin_route_inventory::test_all_baseline_routes_still_registered`（`POST /api/desktop/heartbeat` 05:55 被 N-5 代登记进清单，但路由文件 `src/web/desktop_bridge_presence.py` 仍是微信 PC 线的**未提交**文件——共享树绿、HEAD 红；微信线提交即绿）。
- **O-1 B 门禁 `scripts/outbound_style_gate.py`：200 样例 violations=0**（punct 534 / style 246 / trimmed 48）。
- **跨线 `gate_sweep`：16 红**。7 条是 N-5 06:44 已记的老债（`deploy_profiles` / `seed_switch`×2 / `inline_color_ratchet`×3 / `orphan_refs`）；其余 9 条逐一核过**均非 O 批 commit 所致**：`copilot_tiny_font_ratchet` cp-goal.js 29>24 在 1.0.77 发版点已是 29（O-3 D/E 零新增）；`input_theme_contrast` `--t1` 两处分别来自 09-05 / 09-07；`workspace_emoji_ratchet` 56>54 与 `legacy_blue_ratchet` 18>15 / css 2>0 来自抖音 UI 线；`silent_exception_ratchet` 多模块整体超限属存量；`assistant_qa_eval`×2 小智问答金标（小智 / 教程页线）；`takeover_trigger_surface` 与 `admin_route_inventory` 见上。**R78 发版前请以此表对号，别把这些算到 O 批头上，也别在别人的编辑窗口里替他们修。**
- **R78 注意**：O-1 行仍是「进行中」（D 拟人节奏 / E 三小件在途，`autosend_worker.py` / `reply_pacing_settings.py` / `humanize.py` / `reply_settings.html` 有未提交改动），微信 PC 线 11:1x 仍在写。三包从共享树打包＝会把在途半成品带进包；**O-1 行未变「收工」前不要 bump / dist**。
