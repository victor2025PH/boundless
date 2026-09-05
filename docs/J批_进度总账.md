# J 批进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。
> 建立于 2026-09-05 03:2x（值守线）。背景对账 `docs/报障对账_钧_花无缺_20260905.md`；公共底座 `docs/修复指令_J批_并行_2026-09-05.md`。

## 一句话现状

1.0.73 已装两位内测机。09-04 夜批共 17 个不同问题，已按一单一事立成 #164–#181；skuio 机远程包 88MP86 + 13 份 Cursor 报告在手，钧机远程包等他下次开机自动上传。本批五条指令全部可独立开工。

**06:38 值守复核（第一波三条）：J-1 / J-2 / J-3 代码侧完成——三份落点表里全部 commit 在 HEAD，21 个关键落点 rg 全命中，三条声明的测试集在当前树合跑 569 passed / 0 failed。** 未完成的只剩「装载」与「真机」：所有 `.py` 改动**尚未装进 zhiliao**（末次重启 09-04 19:38，下一窗 12:30），#177「我有个女儿」/ #166 goal-inject 可见性 / #172 E2EE 视频等真机验收都要等装载 + 1.0.74 装机。J-2 另有追加对话 **D1b** 仍在改（`goals/**`、`cp-goal.js`、`unified_inbox.html` 的 `?v=` 行——与 J-4 共用文件，J-4 改 `unified_inbox.html` 前先 `agent_probe.ps1` 看它是否 ACTIVE）。06:5x 追加 J-6…J-9（见下）。

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **J-1** | AI 出站内容正确性 + 记忆一致性 | #171 #175 #176 #177 | $190 | 已完成（代码侧） | A `7a3fc6c9` / B `9d6426f3` / C `24229010` / D `5d46c4ee`（模块+prompt 消费；send 路由一行接线已由 J-3 落地 `dc923110`） | 交 J-2：SOP 步 prompt 人设口吻钉子；~~交 J-3 接线~~ 已完成；值守：skuio 机跑 `xlate_memory_purge --apply`、mutual_chat 阈值复核 | `发版对账_v1.0.74_J1.md` | ~$120 |
| **J-2** | 目标 / 工作链 / 自动化档位：UI 承诺 vs 引擎实际 | #166 #168 #167 | $190 | 已完成 | A1 `d0aa6560` / A2 `e4a62ee5` / B `983e05ba` / C `45261c78`；D1b 未拍板（只翻 sprint.enabled 不够真出手） | 交 J-4：cp-goal `?v=`、链卡三态、批量挂链 tooltip | `发版对账_v1.0.74_J2.md` | |
| **J-3** | LINE 媒体链 | #169 #172 #180（+#177 接线 / #181 观测） | $190 | 已完成（代码侧，未重启） | A #172 `c9d19638`（Letter Sealing 媒体 em* 路径+解密；404≠过期 7 天；回填排程）/ B #180 `28ce86ba`（501 三分因+文案+receiver 退避）/ C #169 `e3573a66`+`65e122a2`（上限单源 line 100/tg 200/wa 64；OBS 上传+编排器超时按体积；真机 30/60/100MB 全送达）/ #177 J-1 交办接线 `dc923110` / D #181 只读观测已记 | 交 J-4：fetch-media 重试路由改传真消息（`发版对账_v1.0.74_J3.md`「交 J-4」）+ 媒体按钮灰 tooltip 消费 `send-caps.caps_reason`；交 J-6：skuio 风暴＝单个 403 forbidden 账号被 `close-policy.js` 无限重连（10h 38 轮），钧机＝`ENOTFOUND web.whatsapp.com` DNS；值守：下个重启窗装载 `.py`；后续评估 LINE 入站缺省 20MB→32~40MB（服务端转码件 100MB→27.5MB 会被拒下） | `发版对账_v1.0.74_J3.md` | ~$150 |
| **J-2 追加 D1b** | 自动推进真开（冲刺脱离 care 灰度门 / 三平台白名单 / 自然档每日拍 / 运行时闸单源 + preflight / 停摆看门狗） | #166 | （随 J-2） | **进行中**（06:38 仍有未提交改动：`service.py` `sprint_ticker.py` `goal_routes.py` `health_watchdog.py` `webhook_notifier.py` `cp-goal.js`×2 `app.html`×2 `i18n goals.py` `config.example` `unified_inbox.html` + 新 `liveness.py`/`goal_sprint_drill.py`/`test_goal_liveness.py`） | P0-1..3 `d34666f3` / P0-4 `abe96ba3`（含 cp-goal `?v=20260905a` + ui-build，**替 J-4 做掉了 J-2 交办的 bump**）/ P0-5 `981790e3` | 收尾提交 + 更新本行 + 落点表（建议追加进 `发版对账_v1.0.74_J2.md`） | `发版对账_v1.0.74_J2.md` | |
| **J-4** | 工作台 UI 小件 | #170 #173 #174 #178 #179 #164 nag（+ J-3 交办 fetch-media 路由 / 媒体按钮 tooltip、J-2 交办链卡三态 / 批量挂链 tooltip、#188） | $150 | **已完成**（七件 + 交办四项 + #188 全 commit；模板/CSS/组件已热更生效，三处 `.py` 待 12:30 装载） | A `09bd0294` / B `3c101f0e`+`7b56f894` / C `345241a6` / D `17230974` / E `ed1b746e` / F `38bfda40` / G `9f045de9` / J-2 链卡 `3c1213e6` / J-3 tooltip `c9759aea` / J-3 fetch-media `500d9b4d` / #188 `3fee4790`；cp-goal `?v=` 由 D1b `abe96ba3` 已 bump、G 再 bump `20260905c` | **#170 无后端项**（归档视图 mark-read 无早退、`store._unarchive_on_inbound` 已存在——88MP86 的 7 条 64h 被埋＝人工归档后无入站，非缺陷）；值守：① 12:30 装载 `persona_routes.py`（#179）/ `admin.py`+`goal_routes.py`（G）/ `unified_inbox_account_routes.py`（fetch-media）；② **催 I-2 线提交** `unified_inbox.html`/`unified-inbox.css`/`inbox_workspace.py` 里 09-04 #156/#159/#155 未 commit hunk（1.0.73 已发 #159 与 J-4 A 场景 13 都依赖）；③ ops 卡「已发假声明」行（J-1 A 提到）未做；J-7 可接管 `personas.html` / `persona_routes.py` | `发版对账_v1.0.74_J4.md`；骨架 `发版对账_v1.0.74.md` 已建 | ~$110 |
| **J-5** | 登记链与值守工具 | 限频/分类/backfill/verify + #138 #140 #142（+#165 收集窗） | $120 | 已完成（代码侧，未重启；台账已改） | A `da1bbb86`（backfill 共享 seen 集 + observe mid 去重）/ B `7be18026`（verify 只认 #N / reply_to）/ C `9ec70d37`（限频不管登记）/ D `1287f489`（分类器补句 + 已知报障人图+字立单 + smalltalk 落事件）/ E-1 `6ed68d53`（`duty_ledger_fix` 已 `--apply`：#138/#140/#142 verified→fixed，notify_ts 保留，`ledger_fix` #750-752）/ E-2 `e4cf6896`（回复支持号消息续单）/ F `5f4f68f1`（AGENTS v1.3）；三测试文件 130 passed；61 单 fixed 未回访只记数不发 | 交值守：① `trigger.py` L299 / `skill_manager` L977 / `telegram_client._fetch` 透传 `account_id/sender_name/msg_id/reply_to_msg_id/last_name`（他线文件，落点表「交值守」①）；② AGENTS_JUN/SKUIO v1.3 重发两机并明说覆盖；③ 12:30 窗装载 | `发版对账_v1.0.74_J5.md` | ~$100 |
| **J-6** | WhatsApp 边车稳定性（Node） | #181 + 403 无限重连 + ENOTFOUND + Bad MAC | $150 | 未开工 | | | `发版对账_v1.0.74_J6.md` | |
| **J-7** | 人设工作室体验与报错 | #189 #190 #191 #192 #193 #186（+#178/#179 若 J-4 未做） | $150 | 未开工（**J-4 已于 08:4x 释放 personas.html / persona_routes.py**，#178/#179 已由 J-4 做掉，可整批开工） | | | `发版对账_v1.0.74_J7.md` | |
| **J-8** | 陪伴安全 + 关怀语义 | #185（P1）#182 | $190 | 未开工 | | | `发版对账_v1.0.74_J8.md` | |
| **J-9** | 知识库隔离 + 检索自检 | #184 | $150 | 未开工 | | | `发版对账_v1.0.74_J9.md` | |

状态取值：`未开工` / `进行中` / `已完成` / `部分完成(等下一账号)` / `阻塞(原因)`

## 指令文件

| 指令 | 文件 |
|---|---|
| J-1 | `docs/指令_J-1_出站内容与记忆_2026-09-05.md` |
| J-2 | `docs/指令_J-2_目标与工作链引擎_2026-09-05.md` |
| J-3 | `docs/指令_J-3_LINE媒体链_2026-09-05.md` |
| J-4 | `docs/指令_J-4_工作台UI小件_2026-09-05.md` |
| J-5 | `docs/指令_J-5_登记链与值守_2026-09-05.md` |
| J-6 | `docs/指令_J-6_WhatsApp边车稳定性_2026-09-05.md` |
| J-7 | `docs/指令_J-7_人设工作室体验_2026-09-05.md` |
| J-8 | `docs/指令_J-8_陪伴安全与关怀语义_2026-09-05.md` |
| J-9 | `docs/指令_J-9_知识库隔离与检索_2026-09-05.md` |
| 公共底座 / 归属矩阵 / 老板决策 | `docs/修复指令_J批_并行_2026-09-05.md` |

## 投放顺序与依赖

```
J-1 ─ J-2 ─ J-3 ─ J-4 ─ J-5   全部独立，可同时开（J-1/J-2/J-3 已完成代码侧）
J-6（Node 边车）─ J-8（wellbeing/care）─ J-9（KB）  三条互不重叠，可立即开
J-7（人设工作室）：J-4 已做完 #178 `ed1b746e` / #179 `17230974` 并释放 personas.html / persona_routes.py，可整批开工
D1b（J-2 追加）仍在改 goals/** + cp-goal.js + unified_inbox.html 的 ?v= 行 → J-4 改 unified_inbox.html 前先看意向板
J-8 加 wellbeing 两键的 cloud_light.yaml / config.desktop.internal.yaml：D1b 在同文件 goals 段有未提交 hunk → 用 tools/stage_hunks.py 只提交自己的
```

## 老板决策记录

| 日期 | 决策 | 影响 |
|---|---|---|
| 2026-09-05 | 见公共底座 D1–D6（sprint 出厂开 / 电话点击揭示 / 桌面包隐藏告警 nag / 限频不管登记 / verify 认 #N / 三单改回 fixed）——**默认按底座表执行，老板划掉的除外** | J-2 / J-4 / J-5 |

## 03:3x–05:4x 新增（skuio 又传 16 份 Cursor 报告，已立单 #182–#193；06:5x 已分派）

| 单 | 级 | 主题 | 归属 |
|---|---|---|---|
| #182 | P2 | 主动关怀「补一条关怀」意图反转 | **J-8** |
| #183 | P2 | 记忆「待确认」积压非闸门 + 抽取漏检（产品提案）| **产品决策后再派**；J-1 D（#177 人工出站进记忆 `5d46c4ee`）已覆盖其中「人工出站视为人设真实意志」一条 |
| #184 | P2 | 知识库首装预置 110 条厂商产品；`source=vendor` 隔离 + 检索自检 | **J-9** |
| #185 | **P1 安全** | 危机审计页空态混淆——值守已核：**识别与红线在 skuio 机是开的**（`wellbeing.enabled` 出厂 true），关着的是 R9 审计与 R8 升级 | **J-8**（决策 D7） |
| #186 | P2 | 用户管理页 8 条僵尸会话 + 术语 | **J-7** |
| ~~#187~~ | — | ~~拟稿 KB hits 恒空~~ → **误读关单**：`hits=` 是风控命中词字段；「用户 KB 是否为空」并入 #184 | J-9 |
| #188 | P2 | 钧：手动发送气泡短暂双显，手机端一条 | **J-4** |
| #189 | P2 | 人设编辑器·备货 tab：台词库/说话指纹是研发缓存却当用户功能，就绪度卡 75% | **J-7** |
| #190 | P2 | 人设编辑器·相册 tab：试触发在底部、325 套表单、触发词全空、两套保存 | **J-7** |
| #191 | P2 | 人设编辑器·预览 tab 历史质检点击 → 「页面脚本出错」 | **J-7**（先做） |
| #192 | P2 | 人设工作室页头：控件截断 / 重复新建按钮 / 90 个标签 | **J-7** |
| #193 | P2 | 人设工作室弹「页面未能打开 /admin/voice-eval」 | **J-7**（先做） |

## 值守收尾清单（不开对话，值守自己做）

- [ ] **12:30 重启窗装载 zhiliao**（`restart_preflight` → `restart_instance.ps1 -Instance zhiliao`）：J-1/J-2/J-3/D1b 全部 `.py` 才生效；装完用 88MP86 复盘口径核 #166（`[goal-inject]` 行）、#177（真机「我有个女儿」→ 下轮草稿含「你亲口说过」块）。
- [ ] skuio 机 1.0.74 装完跑 `python tools/xlate_memory_purge.py --db %APPDATA%\telegram-ai-desktop\data\config\translation_memory.db --apply`（J-1 C 交办；不跑也会在命中时自愈）。
- [ ] LINE 入站缺省 `DEFAULT_INBOUND_MAX_BYTES` 20MB → 32–40MB（J-3 C 发现：服务端转码件 100MB→27.5MB 会被拒下）；一行常量 + 一条测试，`line_media.py`。
- [ ] `config.example.yaml`：`companion.media_promise_guard.sent_claim.{enabled,window_min}`（J-1 A）与 `inbox.workflows.auto_start.max_per_tick: 5`（J-2 B）两处**文档键**——等 D1b 收工后再加（该文件他在改）。
- [ ] `mutual_chat` 判据（88MP86 把 John / BABY BEAR 判成「双向高频疑 AI 对聊」）阈值复核（J-1 顺手项）。
- [ ] 钧机远程包到手 → 补 #180（00:48 LINE worker 状态）/ #181（00:20–00:25 wa-sidecar）证据回读；#164 回访文案（1.0.74 装完）。
- [ ] skuio 两张截图（#171 John 原话 / #166 目标详情）03:36 已第二次提醒；仍不到则 J-1 A 的 EN 词表按常见形态收口，#166 键核对靠 A1 的 INFO 日志（装载后自证），不再追问。

## 等待项（不阻塞开工，到了由值守回读）

| 等 | 内容 | 到手后谁用 |
|---|---|---|
| ~~钧~~ | ✅ 03:12 老板代转答复：那张图**对方收到了**、手机端**一条** → #164 无第二因（只改提示，J-4 C）；双显立 #188 | 已回写台账 |
| 钧 | v1.3.1 selfcheck 短码（Cursor 通道恢复） | 值守 |
| 钧机 | 远程包 `dr-mtnarhxg-o8apf7`（DutyWatchTick 自动拉到 `tmp_diag/`） | J-3（00:48 LINE worker 状态）；#181 另开 J-6 |
| skuio | John 要照片原话截图（#171）；BABY BEAR 目标详情截图（#166） | J-1 / J-2（**不要等**，两条指令的代码侧确定性缺陷先修） |

## 换账号 / 额度用尽的通用流程

**收工方：** ① 已完成的工作必须已 commit ② 写落点表（未完成项也留行）③ 更新本文件自己那一行 ④ 清意向板。
**接手方：** 读本文件 → 读落点表 → `git log --oneline -15 -- engines/chengjie` 核 commit → 读指令文件从第一个未完成项继续。**以 commit 和落点表为准，总账是索引。**

## 全批收口（五条都完成后，由值守做）

1. 合并五份落点表进 `docs/发版对账_v1.0.74.md`（J-4 建骨架）
2. 全量回归：`python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread`
3. `scripts\gate_sweep.ps1 -Full`
4. 更新 `docs/值守症状族对账单.md`
5. 才谈发版 1.0.74 → 两位内测机装完 → 按 #号逐单回访（含 I-6 那 61 单）
