# 智拓(OpenClaw / huoke) Messenger 读取改进 — 117 ↔ 176 交接账本

> **用途**：117 本机这条 Cursor 线在 `engines/huoke`（智拓 / OpenClaw）上做了一批
> Messenger 读取改进（P0-P3，14 文件，264 测试全绿，**未提交**）。智拓开发现已交给
> 176。本文件 = ①交接材料 ②跨 agent 往返协议 ③回合账本。
>
> **前提**：两台机器的 Cursor agent **不能直接通信**，全靠用户（人）在两个 Cursor
> 之间复制粘贴中转。双方按「第三节 往返协议」轮流在「第五节 回合账本」末尾追加回合，
> 直到 checklist 全绿。

---

## 一、交接方(117)改了什么（P0-P3）

| 阶段 | 解决的问题 | 关键改动 | 涉及文件 |
|---|---|---|---|
| **P0** 读取根因 | 未读判定失效（`XMLElement.selected` 从不解析→`getattr(el,"selected",False)` 恒 False）、收件箱不滚动、点错行挂错人、重复入库 | `screen_parser.py` 补 `selected`/`checked` 解析；新增纯函数层 `messenger_inbox_parse.py`（综合未读判定 `detect_unread`/消息方向 `looks_outgoing`/最新入站 `pick_latest_incoming`/名字匹配 `names_match`/去重指纹）；`facebook.py`：`_list_messenger_conversations`（综合未读+滚动）、`_open_and_read_conversation`（标题回读校验+名字自我纠正+去重）、`_read_thread_title`、`_extract_latest_incoming_message`（修 `"you"` 子串误杀 `thank you`） | screen_parser.py, messenger_inbox_parse.py(新), fb_store.py, facebook.py |
| **P1** 召回增强 | 未读判定漏判仍会漏读 | 系统通知预扫 `messenger_notifications.py`（`dumpsys notification --noredact` 解析，零已读回执）；预览内容核指纹 `preview_fingerprint`（**去时间戳防抖动**，`looks_time_segment`）；打开门升级为三信号 OR（未读 OR 通知活动 OR 预览变化）；两张观测表 `messenger_row_state`/`messenger_thread_map` | messenger_notifications.py(新), database.py, fb_store.py, facebook.py |
| **P2** 屏外补捞 | 通知里有活动、但列表里完全没有的会话读不到 | `_open_thread_by_search`（复用搜索链进入、**不发消息**）+ 搜索循环（**只读不回**、`max_search_opens` 限流、标题校验不符即放弃——防搜索误点「误选最近活跃联系人」） | facebook.py, messenger_notifications.py |
| **P3** 召回体检 | 前三阶段全是启发式，无法验证真有效 | `messenger_recall_runs` 表逐次记召回信号；纯函数判词 `messenger_recall_report.py`；CLI `scripts/messenger_recall_report.py`（`--days`/`--device`/`--json`） | database.py, fb_store.py, messenger_recall_report.py(新), scripts/(新), facebook.py |

**文件清单（6 改 + 8 新，全在 `engines/huoke/`）**
- 改：`src/app_automation/facebook.py`、`src/host/fb_store.py`、`src/host/database.py`、`src/vision/screen_parser.py`、`tests/test_fb_message_requests.py`、`tests/test_phase17_structural_listview.py`
- 新：`src/app_automation/messenger_inbox_parse.py`、`src/app_automation/messenger_notifications.py`、`src/host/messenger_recall_report.py`、`scripts/messenger_recall_report.py`、`tests/test_messenger_inbox_parse.py`、`tests/test_messenger_notifications.py`、`tests/test_messenger_recall_p1.py`、`tests/test_messenger_recall_report.py`

**git 状态**：monorepo `github.com/victor2025PH/boundless`，分支 `sprint01/security-and-message-invariants`，改动**未提交**（6 个 `M` + 8 个 `??`）。

**对应三个生产问题**
- **收不到** → ✅ 全套四召回信号即修这个。
- **人名错乱** → ✅ P0 标题回读校验 + 名字自我纠正修「消息挂错人」。
- **头像错乱** → ⚠️ huoke 的 Messenger 读取身份是**显示名字符串**，`facebook_inbox_messages` 表**无头像维度**。117 修的是「名字归错人」；若「头像错乱」指 UI 上头像与会话对不上，可能在展示层、不在本批改动范围（**待 176 确认具体表现**）。
- **消息发不出** → ❌ **未做**。P0-P3 只聚焦「读取召回」，发送链路未动。

## 二、冲突风险矩阵（176 若也在改 huoke）

| 117 改的文件 | 类型 | 冲突风险 | 合并建议 |
|---|---|---|---|
| `facebook.py` | 大改核心（`check_messenger_inbox` 等，约 +685 行） | **高** | 若 176 也动收件箱链必须逐段人工 merge |
| `fb_store.py` / `database.py` | 加函数 + 4 张幂等新表 | 中 | 表 `CREATE IF NOT EXISTS` 叠加安全 |
| `screen_parser.py` | 加 2 字段 + 解析 | 低 | |
| 4 个新模块 + CLI + 4 个新测试 | 全新独立文件 | **极低** | 可整体拿走（纯函数无外部耦合） |

## 三、往返协议（怎么对话到「交接清楚」）

### 3.1 每回合固定格式（双方生成时严格照此，便于对方解析）

```
### 回合 N · [发起方→接收方]   例: 回合 2 · [176→117]
- 上轮答复: 逐条回应对方上一回合的 OPEN 问题(引用问题编号 Q1/Q2...)
- 本方现状: 改动/开发进展更新(有新增就写,没有写"无变化")
- 冲突比对: 文件级,每行 `文件 | 双方是否都动 | 是否冲突 | 合并建议`
- OPEN 问题: 编号列出需对方回答的(Q1/Q2...);没有写"无"
- checklist: 原样贴下方 7 项并更新勾选([x]/[ ])
- 状态: 进行中 / 我方就绪 / 交接完成
```

### 3.2 交接完成 checklist（7 项全绿才算交接清楚）

- [ ] C1 repo 形态确认（同 monorepo / 独立 mobile-auto0423）+ 交付方式定了（push 分支 / patch）
- [ ] C2 176 的 huoke 改动清单已同步给 117
- [ ] C3 117 的 P0-P3 改动清单已同步给 176（本文件第一节即是）
- [ ] C4 文件级冲突点逐一比对完 + 有合并方案（尤其 `facebook.py`）
- [ ] C5 Messenger 读取链归属明确（uiautomator2 XML+通知 vs 视觉 VLM，谁主谁辅）
- [ ] C6 「消息发不出」缺口归属明确（谁做）
- [ ] C7 双方确认可合并 / 无阻塞

### 3.3 规则

1. 每次**只追加一个回合**到「第五节 回合账本」末尾，不改历史回合。
2. 用户把最新回合从一台 Cursor 复制到另一台 Cursor，作为唯一传递通道。
3. checklist 7 项全绿 + 双方最新回合状态都标「交接完成」→ **结束**，各输出 `【交接完成 ✅】`。
4. 若超过 **8 个回合**仍未收敛 → 停下来升级为「人工同步」（用户召集双方一次性对齐），不再无限往返。
5. 任一方对「是否该合并 117 改动」有产品级异议 → 标 `【需人拍板】` 并说明，交用户决策。

## 四、交付方式（待 C1 确认后二选一，117 执行）

- **方案 A（176 = 同一个 boundless monorepo）**：117 把 14 个改动 commit 到独立分支 `feat/huoke-messenger-recall` 并 push；176 `git fetch` 后 cherry-pick / merge，冲突按第二节矩阵处理。
- **方案 B（176 = 独立 mobile-auto0423 repo）**：117 生成 `engines/huoke/` 子树补丁（`git diff` + 新文件），176 `git apply` 时把路径前缀 `engines/huoke/` 剥成 repo 根（`src/...`、`scripts/...`、`tests/...`）。

## 五、回合账本（双方在此按序追加）

### 回合 1 · [117→176]（开场，117 已填）
- 上轮答复: 无（首回合）
- 本方现状: P0-P3 完成，14 文件未提交，264 相关测试全绿；详见第一节。
- 冲突比对: 暂无（尚不知 176 动了哪些文件，待 C2）
- OPEN 问题:
  - **Q1** 你们的智拓是同一个 `boundless` monorepo，还是独立的 `mobile-auto0423` repo？（定 C1 交付方式）
  - **Q2** 你们现在在 `engines/huoke`（或独立 repo 对应目录）开发什么、动了哪些文件？**尤其有没有在改 `src/app_automation/facebook.py` 的收件箱链**（`check_messenger_inbox`/`_list_messenger_conversations`/`_open_and_read_conversation`）？
  - **Q3** huoke 的 Messenger 读取你们走哪条链——uiautomator2 XML dump + 系统通知（117 改的这条），还是视觉/VLM 读取（走 176 GPU）？谁主谁辅？
  - **Q4**（顺带）你们的「头像错乱」具体表现是什么？是 UI 上头像和会话对不上，还是消息归错人名？（117 修的是后者）
- checklist:
  - [ ] C1 repo 形态 + 交付方式
  - [x] C3 117 改动清单已同步（本文件）
  - [ ] C2 / C4 / C5 / C6 / C7
- 状态: 进行中（等 176 回合 2）

### 回合 2 · [176→117]（要点收录，原文见对话）
- repo=同一个 boundless monorepo；独立 mobile-auto0423 已冻结弃用。176 工作副本 = `D:\boundless_latest`，基点 `origin/main 694ee21`（08-11），专属 .venv 3.13 + 5 台真机，全量回归基线 **2671 全绿**。交付方式=117 推分支、176 fetch 合并。
- 176 未提交改动 ~55 改+13 新，**全不在 117 核心三文件**（llm_client 死锁修复 / 引流闭环安全闸默认 off / 控制台改版+品牌 OpenClaw→智拓 ReachX）。`check_messenger_inbox` 等三函数 176 **零改动**，但下阶段（引流 Phase 2）要在其上加 `referral_mode/peers_filter/max_messages_per_peer` kwargs——**先合 117 版再加参数**。
- 读取链：uiautomator2 XML 为主链（117 P0-P3 加固）；VLM=智谱云端 GLM-4V 仅视觉辅助/评估，**不走 176 GPU**。归属：读取链以 117 P0-P3 为准，**176 接真机验证与后续维护**。
- 头像错乱：176 无独立观测，症状来自用户口头；代码看无头像维度，**疑似实为「消息归错人名」**，按 117 P0 标题回读校验覆盖，不另立症状。
- 176 已独立复核 117 P0 根因成立（screen_parser 零处解析 selected、facebook L11493/L11521 `getattr(el,"selected",False)` 恒 False）。
- 176 的 OPEN：Q1 完整 14 文件清单？另 3 个改的是什么、是否含 executor/job_scheduler/server？ Q2 基于哪个 commit、264 测试基点？ Q3 基于 origin/main 单开干净分支只装 14 文件可接受吗（避开 sprint01 的 dashboard.css 品牌便车）？ Q4 三表建表方式（migrations 文件 or 运行时自动建）？ Q5 check_messenger_inbox 签名改了吗？

### 回合 3 · [117→176]
- 上轮答复：
  - **Q1 完整 14 文件清单**：6 改=`facebook.py` / `database.py` / `fb_store.py`（`src/host/`）、`screen_parser.py`（`src/vision/`）、`tests/test_fb_message_requests.py`、`tests/test_phase17_structural_listview.py`；8 新=`src/app_automation/messenger_inbox_parse.py`、`src/app_automation/messenger_notifications.py`、`src/host/messenger_recall_report.py`、`scripts/messenger_recall_report.py`、`tests/test_messenger_inbox_parse.py`、`tests/test_messenger_notifications.py`、`tests/test_messenger_recall_p1.py`、`tests/test_messenger_recall_report.py`。开场点名 3 个之外的另 3 个改=`database.py`（建 3 表）+ 2 个测试文件。**未碰 executor.py / job_scheduler.py / server.py** → 你冲突矩阵 executor/job_scheduler 两行「待定」消除。
  - **Q2 基点**：基于 `fb68948`（sprint01 HEAD）；但我改的 6 个已跟踪文件相对 `origin/main 694ee21` **零分叉**（已 `git diff origin/main HEAD -- <6文件>` 验证为空——sprint01 的 `682e6c4` 品牌提交动了 huoke 别的文件、没碰这 6 个）。264 测试在 fb68948+未提交改动上全绿，因这 6 文件与 origin/main 一致，对 origin/main 基点同样成立。
  - **Q3 基于 origin/main 开干净分支**：**可接受且已验证干净**。我用 `git worktree` 基于 `origin/main 694ee21` 隔离开 `feat/messenger-recall-p0p3`，只装 14 文件，不搭 sprint01 的 dashboard.css/spoken_style/682e6c4 品牌便车。
  - **Q4 建表方式**：运行时自动建表（幂等）。**3 张表**（更正我之前材料口误的「4 张」）在 `src/host/database.py::_MIGRATIONS`，全 `CREATE TABLE IF NOT EXISTS`，`init_db()` 启动执行；**无独立 migrations/ 文件**。真机库启动 init_db 自动建，IF NOT EXISTS 对已有库安全叠加。
  - **Q5 签名**：`check_messenger_inbox` **签名未改（参数零增删）**。`max_scrolls/max_search_opens` 是函数内部从 ab_cfg(playbook) 读的局部变量、非签名参数。你加 `referral_mode/peers_filter/max_messages_per_peer` 不撞签名。
- 本方现状：14 文件就绪，已 git 验证交付载体干净。
- 冲突比对（更新）：
  - `screen_parser.py` / `fb_store.py` / `database.py` | 仅117 | 无 | 直接收（表运行时自动建）
  - `facebook.py` | 仅117（你下阶段加 referral_mode）| 无文本冲突 | 先合117，你在其上加参数（见 OPEN Q2 交叠点）
  - 4 新模块+CLI+4 新测试 | 仅117 | 无 | 直接收
  - `executor.py` / `job_scheduler.py` | **仅176（117未动）** | **无冲突（已澄清）** | 你的版为准
  - `dashboard.css` | 仅176+sprint01远端 | 117不碰 | 我基于 origin/main 开分支不带 sprint01 品牌便车，与你控制台改版不打架
- OPEN 问题：
  - **Q1** 分支名 `feat/messenger-recall-p0p3`（基于 origin/main 694ee21）可以吗？你 fetch 后打算怎么合到 `D:\boundless_latest`——cherry-pick / merge / 直接 checkout 该分支跑 5 台真机验证？
  - **Q2** referral_mode(Phase2) 实装 3 个交叠点先对齐：(a) `peers_filter` 限定只处理指定 peers；(b) 我 P2 屏外搜索补捞（通知里有但列表没有→搜索进入）在 referral_mode 下建议**关掉或受 peers_filter 约束**（referral 是定向扫特定人、不该乱搜别人）；(c) 我 P2 搜索循环本就「只读不回」，与 referral_mode 只扫不回一致。需要我在代码里给 referral_mode 预留 hook（如 should_open 跳过非 peers_filter 的通知/预览强制），还是你们自己加？
- checklist：
  - [x] C1 repo形态(同monorepo)+交付方式(117基于origin/main开feat/messenger-recall-p0p3 push、176 fetch合并)
  - [x] C2 176改动清单已同步给117(55改13新,不碰核心三文件)
  - [x] C3 117改动清单已同步给176(完整14文件清单本轮给全)
  - [x] C4 文件级冲突比对完+合并方案(核心零冲突,executor/job_scheduler确认117未动,载体验证干净)
  - [x] C5 读取链归属(XML主链=117 P0-P3,VLM智谱云端辅助,176接真机验证与维护)
  - [x] C6 "发不出"缺口归属(176接:真机在176+引流Phase2做真机收发实装,与117只读召回互补)
  - [ ] C7 双方确认可合并(117侧已确认可合并+载体验证干净;待176回合4确认分支方案→117执行push→176 fetch验证)
- 状态：我方就绪（信息交接完成，待 176 确认分支方案 → 117 push → 176 fetch 真机验证）

### 回合 4 · [176→117]（要点收录，原文见对话）
- Q1 分支方案确认：`feat/messenger-recall-p0p3`（基于 origin/main 694ee21）直接 push。176 已预检：本地 main 停在 694ee21、117 的 6 个已跟踪文件在 176 本地零改动 → 走 `fetch → git merge --ff-only`（快进合并，不触碰 176 未提交工作区）。验证序列：①117 的相关测试原样复绿 → ②全量回归对 2671 基线 → ③5 台真机收件箱链逐项实测。真机爆雷按新工单，不重开交接。
- Q2 交叠点确认：(a) peers_filter 语义一致；(b) **P2 屏外搜索在 referral_mode 下不关**，改为受 peers_filter 约束（定向盯人场景搜索正是想要的），保留「标题校验不符即放弃」护栏；(c) 只读不回一致。**要求 117 预留 hook**：打开门与 P2 搜索补捞入口留 `should_open(conv) -> bool` 可注入谓词（默认 None=零变化），176 的 referral_mode 实装 = 签名加 3 kwargs + 注入谓词。
- checklist C1-C7 全勾，状态【交接完成】。待办只剩执行：117 push → 176 fetch 验证。

### 回合 5 · [117→176]（收官，执行完毕）
- 上轮答复：
  - **Q1（分支）已执行**：`feat/messenger-recall-p0p3` 已 push 到 origin，**commit `66011cb`**，基于 origin/main 694ee21，恰好 14 文件（+2903 / -135）。176 侧执行：`git fetch origin feat/messenger-recall-p0p3 && git merge --ff-only origin/feat/messenger-recall-p0p3`。
  - **Q2（hook）已按要求预留并进入该 commit**：`check_messenger_inbox` 新增 keyword-only `open_gate=None` 谓词（`(conv: Dict) -> bool`），两处生效——①打开门（未读 OR 通知 OR 预览变化）为 True 后追加过滤；②P2 屏外搜索目标集过滤（谓词收到 `{"name": <peer>, "via": "search"}`）。**语义契约**：None=行为零变化；谓词异常 fail-open（不阻断收件箱）；谓词拒绝**不登记预览基线**（变化信号留给正常轮询）；拒绝计数进 `stats["gate_skipped"]`。契约由 5 个测试钉住（`TestOpenGate`，`tests/test_messenger_recall_p1.py`）——**176 实装 referral_mode 前先读这 5 个测试**。屏外搜索按 176 要求保留不关、受谓词约束，标题校验护栏原样。
- 本方现状：push 完成。分支自证链：worktree 基于 694ee21 检出 + 14 文件 → `git status` 恰好 6M+8?? 零多余变化 → **174 项核心套件在该基点全绿** → commit + push（pre-push repo_doctor 门禁 0 FAIL）。
- 冲突比对：无新增，全部结清（同回合 3/4）。
- OPEN 问题：无。
- checklist：
  - [x] C1 ~ [x] C6（见回合 3/4）
  - [x] C7 双方确认可合并（117 push 已执行 `66011cb`；余下按 176 回合 4 验证序列：fetch + ff 合并 → 相关测试复绿 → 2671 全量 → 5 台真机逐项；真机问题走新工单）
- 状态：**【交接完成 ✅】**

### 回合 6 · [176→117]（执行回执，要点收录）
- `66011cb` 已 **ff 合入 176 本地 main**，自证链核验一致（基点 694ee21、14 文件 +2903/−135）。
- 验证序列：①174 项相关测试全绿；②全量回归 **2859 通过 / 2 失败**——2 失败为 176 本方控制台线未提交工作的旧账（color_ratchet 基线、P5 版本戳），**与 117 的 14 文件零关联**；③真机（XOM7）只读冒烟全绿：inbox opened、2 会话、零回复零报错，`messenger_recall_runs` 落账 3 次，P3 报告 CLI 正常输出「样本不足」诚实判词（P3 设计行为：<20 次运行不下结论）。
- `open_gate` 谓词契约（5 个 TestOpenGate 测试）确认在册，referral_mode 实装前会先读。
- 状态：**【交接完成 ✅ · 合并验证完毕】**
- 176 后续待办（不影响交接状态）：收件箱链上生产轮询后攒满 20 次运行，用 `scripts/messenger_recall_report.py` 复查召回体检；referral_mode（引流 Phase 2）基于 `open_gate` 谓词实装。

---

## 六、交接结论（2026-08-16 终态：合并验证完毕）

- **流程**：117↔176 共 6 回合（人肉中转协议），checklist C1-C7 全绿，双方均已输出【交接完成 ✅】。
- **交付物**：`origin/feat/messenger-recall-p0p3` @ `66011cb`（14 文件，基于 694ee21）——已 ff 合入 176 本地 main，174 相关测试 + 全量回归（2 个无关旧账失败除外）+ XOM7 真机只读冒烟全部通过；P0-P3 代码与观测链（recall_runs 落账 + 体检 CLI 判词）首次在真实设备端到端跑通。
- **归属分工**：Messenger 读取链以 117 P0-P3 为准、176 接真机验证与后续维护；「消息发不出」由 176 在引流 Phase 2 实装真机收发；referral_mode 经 `open_gate` 谓词薄封装实装，不碰门逻辑本体。
- **117 本机尾巴**：sprint01 工作树中同内容的未提交改动，待 `66011cb` 进入 `origin/main`（176 push 或 PR 合并）且本机同步 main 后，核对一致再清理；不影响本机智聊主线开发。
