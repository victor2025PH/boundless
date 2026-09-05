# K 批进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。
> 建立于 2026-09-05 18:4x。公共底座 `docs/修复指令_K批_并行_2026-09-05.md`（去向表 / 文件归属 / 决策 K-D1–K-D7）。

## 一句话现状

报障群 193 单：fixed 97 / closed 43 / verified 4 / **confirmed 25 / new 24**。49 张未关闭里 **35 张代码已在 HEAD 但没装载没发版没标台账**（zhiliao 末次重启 09-04 19:38，两台内测机 1.0.73），**6 张零代码**（人设工作室，J-7 指令现成），**若干张改了没提交**（I-5 收口 / D1b 收尾，裸奔 10–28 小时），4 张返工单里两张是 verify 误归属、两张按钮点在修复入包前。61 张 fixed 单从未回访。
**K-1 先开**（提交在途 hunk），K-2 / K-3 / K-4 可同时开，K-5 分五段跟进。

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **K-1** | 在途改动收口（I-5 五件 / D1b 收尾 / phantom_unread_alert / 孤儿线盘点） | #32 #145③ #155 #156 #159 #166 #120 #160(guard-check) | $120 | **已完成**（09-05 20:3x） | A1 #160 `82ee11a5` / A2 #145③ `71355568` / A4 #32+#155+#156+#159 `69e62aba`（CSS 单 hunk 切不开故合一）/ B D1b P0-6 #166 `124dd921`（18 文件，`test_goal_*` 962 passed）/ C #120/#159 族 `35b224c4`（`test_source_alerts_all_have_e2e_payload` 转绿，唯一手写的是 2 条 e2e 载荷）/ A3 #146 族 `c468483a`（`skill_manager.py` 静默 35 min 后只提 1 hunk）；全部在 index 预览 worktree 过门禁；**`.py` 全部等 K-5 ①段重启装载** | **D 只盘点**：孤儿线「unlimited outbound mode」实为 **21 文件**（含 `telegram_client.py` `admin.py` `proactive_topic.py` 等，非指令写的 8 个），`rate_limiter.py` / `account_limiter.py` 被整体改成 CRLF，`test_outbound_policy` 24 passed，等 K-D1；#164 sticker 尾巴不完整（键缺 + 测试红）交 K-3/J-4；`care_dispatcher.py` `Dict` 未导入红是 J-8 B `a4a5ab63` 预存；`err.ca.*` 两键无消费方交 J-8；`phantom_unread_remind` 文档键交 K-3。**K-3 可碰 `unified_inbox.html` / `config.example.yaml`；K-5 ①段可重启。** 事故：A4 首提 `80371f3c` 卷走 J-10 暂存 5 文件，已用 `update-ref` 重建为 `69e62aba`、J-10 暂存复原（落点表 §6） | `发版对账_v1.0.74_K1.md` | ~$75（估） |
| **K-2** | 人设工作室体验与报错（= J-7） | #191 #193 #189 #190 #192 #186 | $150 | **已完成**（09-05 21:0x；六件全 commit，10 个 commit） | C #191 `8b1f7775`（真因＝`JSON.stringify(id)` 裸拼双引号 onclick 把属性截断 → 每行 SyntaxError；新门禁 `test_template_inline_handler_arg_quoting` 首轮全扫另修 3 页 9 处 `1dbda120`）/ E #193 `e46c615d`+壳 `324f92ef`（真因＝全局规则 beforeunload 在 Electron 被静默吞、载入层 15s 误报「未能打开」；页面侧显式 confirm + 壳侧 `will-prevent-unload` 原生对话框）/ A #189 `7c9e39d2`（「备货」→「上线准备」四项清单 档案/声音/相册/绑定账号，台词库整块移出、指纹进高级区、都不进分母）/ B #190 `7466f250`（相册面板重写：试触发吸顶+命中定位、卡片精简+高级、AI 建议触发词 chips + 上传引导、底部一处保存、60 张分页 + 搜索筛选；顺带落地 09-01 只进了 i18n 包的实施90 相册 UI）/ D #192 `b513ded9`（头部控件防缩、删页头重复「新建人设」、标签云 top12 + 更多）/ F #186 `547c3c8a`（真因＝壳每次启动 token 直登 INSERT 新会话不回收；同设备只留最近一条 + 空闲 7 天失效 + 30 天清理 + 主帐号记 last_login + 页面人话化/用量单位/说明）；新测试 6 文件 + 门禁 113 passed；**#178/#179 未代做**（J-4 已做） | **交 K-5**：① 下次装载带 F 的 `web_user_store.py`/`auth_user_routes.py`（20:50 那次装到 `b513ded9`，A 的两个 `.py` 已进）；② `python -m scripts.i18n_hant generate`（zh_hant ratchet 本就红，他线 170 键 + 本条 45 键）；③ 1.0.74 打包含 `desktop/main.js`；④ 回访 #192 请 skuio 补「2…/7…」原图确认控件 | `发版对账_v1.0.74_J7.md` | ~$110 |
| **K-3** | 工作台交办小件 | #181(DNS 提示) #183 #177(记忆 chips) #171(ops 行) LINE 入站上限 / config.example 键 / 语音合并窗 | $150 | **已完成**（2026-09-05 20:40；六件全 commit，含可选项 F；K-1 收工交办的 2 件也收了） | D `88c442f9` / C `26298844` / F `569997d8` / A `ba19ac50` / B `5315830c` / E `ee7f8dda` + K-1 交办：`phantom_unread_remind` 文档键 `28089c22` / #164 sticker 尾巴 `8b545d37`；指定测试集 200 passed；`unified-inbox.css?v=20260905d`、`sticker-panel.js?v=20260904a`；**`.py` 改动（line_media / whatsapp_baileys_login / login_routes / inbound_debounce / autodraft_helpers）等 K-5 ①段重启装载** | 无。既有红（非本单）见落点表「顺手发现」：css_token_refs 2 例 / legacy_blue 天花板 15→17 / unified_inbox_stage1 7 例旧钉，交 K-5 ②段按红灯账本处理；B 的 chips 装机后由值守在 skuio 机看一眼 | `发版对账_v1.0.74_K3.md` | ~$90 |
| **K-4** | 返工单与复合单复核（只读 + 回归钉 + 二次结论） | #61 #63 #64 #67 #145 #36 #170 | $100 | **已完成**（09-05 20:1x） | 四单结论：#61 **B**（按钮点在 #78 层，本体用户 1.0.62 已实测）/ #63 **B+A**（verify_no 误归属 → #101）/ #64 **A**（14:37 在 1.0.63 入包前；**#97 是真复发**，1.0.65 收口点已补）/ #67 **B+C**（误归属 + 必须重传相册；#94 是机器人消息误抓非验收）；#145 五行表（①③⑤ A、② 随 1.0.74 真机、④ 等 K-1 A3 提交）；#36 A（1.0.66 + 1.0.73）/ #170 A（K-D5）；**无 D**；`tests/test_rework_pins_k4.py` 21 例（工作树 + 纯 HEAD 导出树均全绿） | 无。K-5 ④段按落点表 §4 写台账（含把钧 09-01 那两句 LINE 反馈补记到 #101/#145②）；#145④ 定 A 前看 K-1 行 A3 是否已 commit | `发版对账_v1.0.74_K4.md` | ~$50（估） |
| **K-5** | 值守：①装载 → ②全量回归 → ③发版 1.0.74 + 装机 → ④台账写入 → ⑤回访 + 真机验收 | 去向表 35 张 + K-1/K-4 结论单 + 61 单回访 | $120 | 未开工（①段等 K-1 提交；②段等 K-1/K-2/K-3；④段等两机装 1.0.74 + K-4 结论） | | ①②③④⑤ | `发版对账_v1.0.74_K5.md` | |

状态取值：`未开工` / `进行中` / `已完成` / `部分完成(等下一账号)` / `阻塞(原因)`

## 指令文件

| 指令 | 文件 |
|---|---|
| K-1 | `docs/指令_K-1_在途收口_2026-09-05.md` |
| K-2 | `docs/指令_J-7_人设工作室体验_2026-09-05.md`（**直接用**） |
| K-3 | `docs/指令_K-3_工作台交办小件_2026-09-05.md` |
| K-4 | `docs/指令_K-4_返工单复核_2026-09-05.md` |
| K-5 | `docs/指令_K-5_值守装载发版回访_2026-09-05.md` |
| 公共底座 / 去向表 / 归属矩阵 / 老板决策 | `docs/修复指令_K批_并行_2026-09-05.md` |

## 投放顺序与依赖

```
K-1 ──┬─► K-3（unified_inbox.html / config.example.yaml 要等 K-1 A4 / B 提交；D、C 两件不用等）
      ├─► K-5 ①装载（K-1 提交即可重启）
      │
K-2 ──┼─► K-5 ②全量回归（K-1 / K-2 / K-3 都收工后）
K-3 ──┤
      │
K-4 ──┴─► K-5 ④台账写入（要 K-4 结论 + 两机已装 1.0.74）─► ⑤回访 + 真机验收
```
- `skill_manager.py`：J-10 phase2 ACTIVE（意向板 18:3x 仍在改）。K-1 A3 只提交 L5017 一个 hunk，且要等它静默 ≥10 分钟。
- `desktop/build/**` / `package.json`：installer brand 线 ACTIVE。K-5 ③段 bump 版本前等它静默 ≥30 分钟。
- 「unlimited outbound mode」孤儿线 8 文件：谁都不碰，K-1 盘点后等 K-D1。

## 老板决策记录

| 日期 | 决策 | 影响 |
|---|---|---|
| 2026-09-05 | 见公共底座 K-D1–K-D7（孤儿线只盘点 / 台账标 fixed 要等装机 / 61 单回访发 / drafts.py 一处例外 / #170 fixed / #145 拆件 / D1b 由 K-1 接管）——**默认按底座表执行，老板划掉的除外** | 全批 |

## 开对话粘贴话术（首次开工用；续传话术在各指令文件末尾）

**K-1**
```
执行 K-1（在途改动收口）。先读 D:\boundless\docs\K批_进度总账.md 与 D:\boundless\docs\修复指令_K批_并行_2026-09-05.md，再读 D:\boundless\docs\指令_K-1_在途收口_2026-09-05.md，按 §2 盘点 → §3 A1–A4 → §4 B → §5 C → §6 D 顺序做。零新功能，只提交树上已有的 hunk；先 stage_hunks.py --snapshot 上保险；skill_manager.py 是 HOT-ZONE 要等 J-10 静默。预算上限 $120。
```

**K-2**
```
执行 J-7（人设工作室体验与报错：#191 #193 #189 #190 #192 #186；K 批编号 K-2）。先读 D:\boundless\docs\K批_进度总账.md K-2 行与 D:\boundless\docs\J批_进度总账.md J-4 行（确认 #178/#179 已做、personas.html 已释放），再读 D:\boundless\docs\指令_J-7_人设工作室体验_2026-09-05.md，从 C（#191）开工。落点表写 docs/发版对账_v1.0.74_J7.md，总账更新 K批_进度总账.md 的 K-2 行。预算上限 $150。
```

**K-3**
```
执行 K-3（工作台交办小件）。先读 D:\boundless\docs\K批_进度总账.md（看 K-1 行 A4 是否已提交——决定现在能不能碰 unified_inbox.html），再读 D:\boundless\docs\指令_K-3_工作台交办小件_2026-09-05.md，按 D → A → C → B → E → F 做；能碰 unified_inbox.html 之前先做 D 和 C。所有接口契约已写在指令里，照契约消费，不重新设计。预算上限 $150。
```

**K-4**
```
执行 K-4（返工单复核：#61 #63 #64 #67 + #145 拆件 + #36 #170）。先读 D:\boundless\docs\K批_进度总账.md 与公共底座决策 K-D5/K-D6，再读 D:\boundless\docs\指令_K-4_返工单复核_2026-09-05.md。只读代码 + 写 tests/test_rework_pins_k4.py 回归钉 + 每单给 A/B/C/D 结论和回访必带的一句，不修 bug。结论表写 docs/发版对账_v1.0.74_K4.md 交 K-5。预算上限 $100。
```

**K-5**
```
执行 K-5（值守：装载 → 回归 → 发版 1.0.74 → 台账 → 回访 → 真机验收）。先读 D:\boundless\docs\K批_进度总账.md 看 K-1/K-2/K-3/K-4 状态，再读 D:\boundless\docs\指令_K-5_值守装载发版回访_2026-09-05.md，从第①段开始，段与段之间的依赖不能跳。不改 src；台账只经 /api/admin/bug-intake 端点；重启前必跑 restart_preflight；发群文案先贴落点表。预算上限 $120。
```

## 换账号 / 额度用尽的通用流程

**收工方：** ① 已完成的工作必须已 commit ② 写落点表（未完成项也留行）③ 更新本文件自己那一行 ④ 清意向板。
**接手方：** 读本文件 → 读落点表 → `git log --oneline -15 -- engines/chengjie` 核 commit → 读指令文件从第一个未完成项继续。**以 commit 和落点表为准，总账是索引。**

## 全批收口（由 K-5 ②③段承担）

1. 合并 J1–J10 + K1–K4 落点表进 `docs/发版对账_v1.0.74.md`
2. 全量回归 `python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread`
3. `scripts\gate_sweep.ps1 -Full`
4. 更新 `docs/值守症状族对账单.md`
5. 发版 1.0.74 → 两位内测机装完 → 按 #号逐单回访（含 I-6 那 61 单）
