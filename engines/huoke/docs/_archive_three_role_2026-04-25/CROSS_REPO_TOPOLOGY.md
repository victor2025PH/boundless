# 跨 repo + 同机协同拓扑权威 (2026-04-25)

> **本文件是三方 (A-main / A-sibling / B-Claude) 协同拓扑的权威 source of truth.**
> 之前所有 docs/A_TO_B_*.md / docs/B_TO_A_*.md / memory 里的"双机同 repo 双 owner"叙述全部作废, 改用本文件.

## 一、历史误判修正

之前 A 一直按"A/B 是同 repo (mobile-auto0423) 双 owner, 各分独占区"工作, **错的**. 2026-04-25 victor2025PH 澄清:

- **mobile-auto0423 全 A 写** (本机 A-main + A-sibling 两窗口都是 A)
- **B 把 mobile-auto0423 git clone 下来只读契约**, 不写代码
- "TG-MTProto Round 1/2/3" 就是 **B 的 Round 1/2/3** (B 的本职 repo 是 telegram-mtproto-ai), 不是第三方
- **B 实际开发主战场**: telegram-mtproto-ai (聊天转化 / Telegram + LINE + 独立 Messenger App RPA / contacts/handoff / 知识库 / Web 后台)

**所以**:
- `CONTACT_EVT_*` 常量族 / `fb_contact_events` 表 / `fb_concurrency.messenger_active` 锁 / `check_messenger_inbox` / `src/ai/chat_*.py` 等 — **全是 A 自己的代码 A 自己的 owner, 没有"B 越界"概念**
- 之前 PR #79 / PR #80 给 B 的"5 协调问 B1-B5"和"5 风险点 1/2/3"都基于错误前提, 撤回
- 之前 `INTEGRATION_CONTRACT.md` 里的"A/B 独占区"划分是历史误判产物, 待重写

## 二、真实拓扑 (2026-04-25 起)

### 2.1 物理层

```
本机 (一台电脑, D:\workspace\)
├── mobile-auto0423/                ← A 的 repo (A-main + A-sibling 共享)
├── telegram-mtproto-ai/            ← B 的 repo (待 B 拷过来)
├── coord-board/.agent-board.md     ← 三方共享本机留言板 (非 git)
├── coordinator/  (Step 6 待建)     ← localhost:9810 device pool 服务
└── archive/                        ← 旧目录归档
```

### 2.2 Claude session 层

| Session | repo 工作区 | memory key |
|---|---|---|
| **A-main** (我) | `D:\workspace\mobile-auto0423` | `~/.claude/projects/D--workspace--mobile-auto0423/` |
| **A-sibling** (本机另一窗口) | 同 A-main (共享 WT + stash) | 同 A-main (共享 memory dir) |
| **B-Claude** (本机第三窗口) | `D:\workspace\telegram-mtproto-ai` | `~/.claude/projects/D--workspace--telegram-mtproto-ai/` |

注: A-main + A-sibling 共享 memory **磁盘文件**, 但单 session 内 in-memory cache 不同步. 一方改 memory 文件, 另一方要重启 session 才看到.

### 2.3 协同通道

1. **A-main ↔ A-sibling** (同 WT, 同 memory): git stash + .agent-board.md + 共享 memory dir + stash 命名带 prefix `A-<main|sib>-<timestamp>-<task>`
2. **A 全体 ↔ B** (跨 repo): 各自 repo 的 docs/ 下 + .agent-board.md
   - A 在 `mobile-auto0423/docs/A_TO_B_*.md` 写给 B (B clone 后 git pull 看到)
   - B 在 `telegram-mtproto-ai/docs/B_TO_A_*.md` 写给 A (A clone 后 git pull 看到)
   - 不再有"双机口头转发", 都走 git
3. **/loop 自动监控**: 每 20 分钟 fetch 两 repo docs commits + PR 评论
4. **Coordinator service** (Step 6 起): localhost:9810 跑设备注册 / 跨 repo 锁 / 事件总线

## 三、跨 repo 接触面 (只 4 个真实存在的)

只有这 4 个, 其他都是历史误解:

| # | 接触面 | A 责任 | B 责任 | 协同方式 |
|---|---|---|---|---|
| 1 | `chat_messages.yaml` 文案口径 | A 维护 (`mobile-auto0423/config/`), `template_optimizer` 自动回写 weight | B 在 `telegram-mtproto-ai/src/contacts/handoff/renderer.py` 拉取 `referral_*` 字段 | B 拉, A 推送 schema 冻结契约 |
| 2 | 跨 repo event aggregate | A 写 `fb_contact_events.greeting_replied / wa_referral_replied / etc.` | B 写 `journey_events.first_text_received / handoff_*` | BI 层用 `meta.platform` 做 namespace 区分, **不强制单 event 字符串跨表对齐** |
| 3 | 真机设备池共享 (21 台 + 云手机) | A 调度 facebook 任务 | B 调度 telegram/LINE/独立 Messenger 任务 | Step 6 起走 Coordinator distributed lock + 设备注册表 |
| 4 | Coordinator service 本身 | A 接 client SDK | B 接 client SDK | TG R3 spec 简化版, 同机 localhost:9810 |

## 四、路径迁移 (2026-04-25 完成 Step 1)

| 旧路径 | 新路径 | 状态 |
|---|---|---|
| `D:\mobile-auto-0327\mobile-auto-project\` | `D:\workspace\mobile-auto0423\` | ✅ 复制完 (738M, 含 .git + 未提交 WT) |
| `D:\tgmtp-readonly\` (A clone 的 read-only) | `D:\workspace\telegram-mtproto-ai-readonly\` | ✅ 复制完 (12M, 临时占位) |
| (B 那台本机) | `D:\workspace\telegram-mtproto-ai\` | 🔜 待 B 拷过来 |
| `~/.claude/projects/D--mobile-auto-0327/` | `~/.claude/projects/D--workspace--mobile-auto0423/` | ✅ 复制完 |

旧路径 `D:\mobile-auto-0327\` 暂留作冻结备份 (1 周后归档到 `D:\workspace\archive\`).

## 五、真机共用方案 (Step 6, Coordinator service)

复用 TG R3 提议的 Coordinator service (同机简化版):
- FastAPI + SQLite, 单进程, ~500 行
- 部署 `D:\workspace\coordinator\`, listen `localhost:9810`
- 4 能力: 设备注册+心跳 / 分布式锁 / 事件总线 / Actor 身份
- A 改 `fb_concurrency.py` 加 `lock_backend = local | coordinator` 热切换
- B `telegram-mtproto-ai` 同样 client 接入
- 真机全部 USB 接本机 (B 把 bg_phone_{1,2} USB 一起搬来)
- ADB 看到 21 台 (19 Redmi + 2 bg_phone)
- A/B 通过 `acquire(device, "messenger_app")` 序列化, 同时只一方持锁

## 六、之前判断的撤回

### 6.1 PR #79 (Round 1 五条 B1-B5)
- B1 权威分工 / B2 已读状态 / B3 时序 / B4 决策权 / B5 旁知风险
- **全部失效**: B 不在 mobile-auto0423 owner 链里, 没有"权威分工"/"决策权"问题
- 真正应该问 B 的 = 4 个跨 repo 接触面 (Q1-Q4) — 已在 R1/R2/R3 通讯链里说

### 6.2 PR #80 (Phase 20.1 NOTICE 5 风险点)
- 风险 1 (CONTACT_EVT_* 越界): **失效**, 是 A 自己的常量族, sibling agent 加常量不算越界
- 风险 2 (注释"B 写"): **失效**, B 不写, 注释应改全 A
- 风险 3 (`check_messenger_inbox` 是 B 独占): **失效**, 是 A 自己的接口
- 风险 4 (TG R2 提的 `meta.platform`): **仍成立**, 这是真跨 repo BI aggregate 问题
- 风险 5 (与 TG `journey_events.first_text_received` 重叠): **仍成立**, 真跨 repo 语义问题

### 6.3 docs/A_TO_B_*.md 历史
- 之前所有 `A_TO_B_*.md` 里"等 B 拍板 mobile-auto0423 内部决策"的部分**全部失效**
- B 拍板的范围只在 4 个跨 repo 接触面 + telegram-mtproto-ai 自己的代码

## 七、下一步执行清单

| Step | 谁 | 内容 | 状态 |
|---|---|---|---|
| 0 | A-main | 停 cron + 验证 sibling 状态 | ✅ |
| 1a | A-main | 复制 mobile-auto0423 / memory / tgmtp-readonly 到 D:\workspace\ | ✅ |
| 1b | A-main (本 commit) | 写本 doc + 改 memory + PR #79/#80 撤错 comment | 🟡 进行中 |
| 2 | victor2025PH | 从 B 电脑拷 telegram-mtproto-ai 完整 + memory + 真机 USB 搬本机 | 🔜 |
| 3 | A-main | 把 D:\ 上 30+ 旧目录归到 `D:\workspace\archive\` | 🔜 |
| 4 | A-main 重启后 | grep + 改硬编码路径 (`D:\mobile-auto-0327` 出现的地方) | 🔜 |
| 5 | victor2025PH | bg_phone_{1,2} 接 USB, `adb devices` 验 21 台, 加 device_registry 注册 | 🔜 |
| 6 | A-sibling 异步 | 起 Coordinator MVP (按 TG R3 spec) + A/B client SDK 接入 | 🔜 |

## 八、三方 Claude session 启动后的第一件事

- 读本 doc (`D:\workspace\mobile-auto0423\docs\CROSS_REPO_TOPOLOGY.md`)
- 读自己的 memory (`~/.claude/projects/D--workspace--<repo>/memory/MEMORY.md`)
- 起 `/loop 20m` 监控两 repo docs (命令模板见 `.agent-board.md`)
- 看 `.agent-board.md` 最新留言

— A-main (2026-04-25)
