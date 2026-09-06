# M 批进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。
> 建立于 2026-09-06 23:0x。公共底座 `docs/修复指令_M批_并行_2026-09-06.md`（去向表 / 文件归属 / 决策 D-M1–D-M10 + L-4 四问补答）。

## 一句话现状

1.0.75 首日复验：skuio 51 份报告 + 钧 3 条群提交 → 新单 #214–#235（22 张）+ 补证，登记链全部自动挂单/立单并回执，**内容此前无人分析**。核心结论：1.0.75 把关怀真发 / 目标冲刺真发 / Messenger 新号直接全自动三条链打开了，而内容与通道校验没跟上——「错但没发」变成「错且发到客户」（#218 关怀内容错误真发、#234 中文直投外国客户、#232 Messenger 零送达却显 ✓）。目标版本 1.0.76。
**M-1 / M-2 今晚先开**（对客事故面；两条都碰 `autosend_worker.py`，各动各的三处），M-3 / M-4 / M-5 并行，M-6 等前五条收工。

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **M-1** | 出站三闸止血 | #214 #218 #234 #219 | $150 | **收工**（09-07 01:2x；`.py` 待装载，`care_schedule.html`/`care_page.py` 已热更） | A `fe8f7a8a`（关怀默认 verbatim；AI 润色 add 无 `preview_confirmed` 不入队 → 预览稿 + 档案 + 校验；新 `care_profile.py` 档案组装 + `fabrication_guard.detect_profile_contradiction` 本地人当外国人/老客当新客 → 拦 skip；首次真发/地名/人名 → `hold:*` 待运营点头「就这样发」；`effective_dry_run()` 单一真值 + 翻转 INFO + 到期组真实状态条 + 模拟中「立即发」说清；`[care-gen]` 落日志；36 例）/ B `d7735f97`（`resolve_outbound_lang` 手动>档案>消息/文字系统>出站历史>人设，全无 → HOLD `translate_hold:lang_unknown` 转人工不投递；mismatch 等校验失败一律 HOLD 带原因；表情/单字 identity 不进引擎；`[xlate] outbound … decided_by=`；worker 只动两处 + 共用文案助手，M-2 hunk 未动；38 例）/ C `13b58e3b`（**根因**：pyrogram 同 group 只跑第一个命中 handler，删除同步 handler 被已读回执遮蔽从未执行 → 独立 group；出站行 `revoke_own_by_platform_msg_ids`（store 唯一新函数）灰显不消失 + `list_recent_messages` AI 口径剔 revoked（点名）；A 线 assistant 历史/last_reply/recent_replies/_human_said_log 剔除 + 己方撤回账本注入「不得再提」；轮询 top 对账兜底；WA/Messenger `message-op revoke` 补同一钩子；13 例） | 交 M-4/M-6：己方撤回气泡文案「你撤回了一条消息」（`unified_inbox.html` 1 行 + 1 键，热区未动）；LINE 己方 unsend 不同步 → 1.0.77；M-2 E 原因卡读 `translate_hold:` 前缀；J-8 遗留 2 红 + 跨午夜 1 flake 见落点表 | `发版对账_v1.0.76_M1.md` | ≈$95 |
| **M-2** | 通道真相与登录默认 | #232 #233 #235 #223 #156补 | $180 | 未开工 | | A 发送 ✓ 以边车回执为准 + 通道状态上屏 + 未连接禁投递 / B 登录默认半自动 + 冷静期 + 账号级批量 + 连续失败降级 / C 退避锁手动优先 + 登录重置 backoff / D Messenger 会话持久化 + 500 落日志 / E 发前确认原因可见 + 作用域文案 + 手动粘性 | `发版对账_v1.0.76_M2.md` | |
| **M-3** | 媒体发送链 | #227 #228 #229 #231 钧#215 | $150 | **收工（后端待装载）** | A `7899cc9a`（真根因＝admin.py 全局 2MB body 闸拦 send-media，>2MB 一律 413+掐连接、后端零记录；豁免 + 裸流落盘 + Content-Length 预检 + 视频压 50MB 同源 + `[http]`/`[send-media]` 访问日志；前端半被 `ece8a0aa` 卷走）/ C `c943261e` / B `04ad5bbf`（.MOV 有随包 ffmpeg 自动换封装 MP4，D-M4 两步同批）/ D `b80a3907`（四分类 + 探活自动重试） | 真机四平台 12 次矩阵（本机上传层版已过、见落点表）/ 钧 #215 包未到，规则与先验在落点表 / D-M5 放回 50MB / 其它 8 个 multipart 上传口同吃 2MB 闸→立单 | `发版对账_v1.0.76_M3.md` | ≈$95 |
| **M-4** | 工作台第三轮 | #222 #221 #230 #206补 | $150 | **收工**（09-07 01:1x） | A `b9ea59f3`（角标 = 当前 scope 内能点开的未读 + 悬浮「另有 群/频道/归档」+ `/account-unread?scope=` 同 WHERE + 群/频道视图说明条 + 看门狗 `[buried] startup_sweep` 72h 存量清扫 + 横幅「全部标已读」+ 被埋计数口径统一）/ B `898715ac`+修正 `4358ca4b`（`wsNav`/`wsNavUrl` 带 ?theme=&lang=，30 处裸导航替换 + window 捕获相位链接改写 + `__wsGoHome` 补 url；胶囊页内筛选钩子 **`window.wsFilterAutoConversations()`**（接口名，行为归 M-2 E，缺席回落带 theme 跳收件箱）；徽标改回「旗舰版」override 进悬浮（闭 L-4 待老板②）；顶栏收缩四档；主题分段控件本就在头像菜单）/ D `198d1e17`（会话工具整块删除、状态卡迁 AI 状态弹层、认领回头部、manifest convops app-only）/ C `dfa46687`（多选工具条「已选 N ｜ 归档·标签·删除 ｜ ✕」+ 批量删除同门同端点）；新测 38 例绿。⚠ B 提交误卷 M-3 C 两处在途 hunk，`4358ca4b` 已只在 index 回退、工作树未动（M-3 随后自提）。.py 四处待装载（`unread_aggregate` / `health_watchdog` / `unified_inbox_read_routes` / `unified_inbox_account_routes`） | 无（三档顶栏截图装机后取；`unified_inbox.html` 剩 4 处裸 `location.href=` 在 M-2/M-3 热区，ratchet 天花板 4，谁动谁改） | `发版对账_v1.0.76_M4.md` | ≈$95 |
| **M-5** | 产品清理与上游 | #217 #224 #226 #225 #220 | $120 | **收工**（09-07 01:1x；`.py` 待装载，模板/i18n/copilot 组件已热更） | A `b5c24bc8` 转化成交类目 client 隐藏（`/templates` 不下发 + 新建 403 + 存量卡标下线）+ `goals/product_guard.py` 无产品禁报价/开户/支付（`goal_no_product` 拦截）+ 首拍进 L1（`reason=goal_first_send`）/ C `169bcb6e` 出图 client `dev_state` 开发中态 + `[image_gen] probe` 落日志 + 去「联系运维」/ D `40249ce5` 工具箱转录默认复用工作台 `voice_transcriber` + 收件核查 `received` + `err.asr.*` 六分类 + DATAS 补 `assets/probe` / E `1090c4cf` 学习队列 `query_lang` 按文字系统 + 原语真实标注 + 未译标「重试」+ 同题合并 / B `0a19204e` 养号 `summary` 判词与计数同口径 + 灰置原因就地 + 三阶段人话说明 / A 追问①② `d55f8ebb`（老板拍板）生命周期自建用户版也闸 + 回复链注入块加「产品边界」行（首拍预览不扩到回复链；出图措辞不分两套） | 交 M-6：三个宿主的 `?v=` bump（宿主文件归 M-2/3/4）、#225 夹具随 1.0.76 打包才生效、`zh_hant_auto.py` 再生 | `发版对账_v1.0.76_M5.md` | ≈$100 |
| **M-6** | 值守收口 | 钧 #215 + 22 单 + 1.0.76 | $100 | 进行中（23:03 起；A/B/C 等 M-1…M-5） | 基线核完（水位干净、#214–#235 全 new、钧未回 1285）；yunsheng exe 已自更 1.0.75 但后端 DOWN | A 钧 #215 定性回访（等 M-3 日志）/ B 22 单验收口径回访 + 46 行对账表 1.0.76 版 / C 1.0.76 装载·两包·两渠道·装机·台账 | `发版对账_v1.0.76_M6.md` | ~$8 |

## 老板拍板记录

（默认值见底座「老板决策」表；老板改口在这里追加一行：日期 / 决策号 / 改为）

- 09-07 01:2x / D-M6 补三问（M-5 A）/ 按 M-5 建议：① `auto_create` / `retention_auto` / `winback_auto` / `reconvert_auto` 在用户版也不建「转化成交」类目模板（`lifecycle_template_blocked`，按部署形态判）；② 首拍预览**不**扩到客户来消息时的顺势推进，改把「无产品禁报价」以产品边界行补进回复链注入块（direct/close 降 soft）；③ 出图失败卡「客服」措辞不按 partner/internal 分两套。落 `d55f8ebb`。

## 全批收口（M-6 做）

- [ ] 六条落点表齐 → 合并 `docs/发版对账_v1.0.76.md`
- [ ] `restart_preflight` → zhiliao 装载 → 全量回归 + gate_sweep
- [ ] 1.0.76 两包 → 坐席三台 → publish internal + lite + clean 三渠道（L-5 落点表）→ 两机核版本
- [ ] 台账：#214–#235 按落点表标 fixed（dry-run 先过目）；回访两份；对账表重跑 `tmp/mapping_c6efrs.py`
- [ ] `docs/值守症状族对账单.md` 更新（新族：通道真相 / 登录默认）
