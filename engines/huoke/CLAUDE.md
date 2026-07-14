# mobile-auto0423 — Claude Code 项目指令

> 本仓库 = **Facebook / Messenger 移动端自动化 bot**，双 worker 同 repo 协作。
> 本文件是 Claude Code 在本 cwd 下自动读取的项目级 memory，所有 Claude session（A/B）都会加载。

## 🆘 出问题 / 新 session 进入时

**先读 [`docs/SYSTEM_RUNBOOK.md`](docs/SYSTEM_RUNBOOK.md)** — 系统运维 SSOT，包含：
- §0 30 秒速查（端口/启停/日志路径）
- §1 进程拓扑 + A/B 业务链分流
- §3 应急恢复 SOP（后台打不开 / 设备失控 / 故障字典）
- §4 Claude 新窗口 onboarding

**配套文档**：[`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md)（架构图谱）、[`docs/CAPABILITIES.md`](docs/CAPABILITIES.md)（业务能力）。

**日常启停**：根目录 `start.bat` / `stop.bat` / `status.bat` 三件套。

⚠️ **不要重新做"进程结构分析"** —— RUNBOOK §1 已有权威拓扑。

## 仓库定位

- **A worker**（greeting bot）：`add_friend` / `send_greeting` / `extract_members` / `browse_feed`
- **B worker**（messenger chat bot）：`check_messenger_inbox` / `_ai_reply_and_send` / `send_message` / `check_message_requests`
- 两者在同一 repo、共享 `src/app_automation/facebook.py` / `src/host/fb_store.py` / `src/host/database.py`，按契约分工

**硬边界、数据库列语义、task_type 命名空间、gate 注册表、device_section_lock 命名空间**
→ 全部看 [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md)（权威契约，任何跨边界修改必须先改本文件再改代码）。

## 明确不在本 repo 范围（防混淆）

| 内容 | 实际归属 |
|---|---|
| contacts / handoff 跨平台 Contact / HandoffToken 子系统 | `telegram-mtproto-ai` repo（另一个项目） |
| Telegram / LINE RPA runner | `telegram-mtproto-ai` |
| Android Messenger RPA runner（adb + UIAutomator 驱动手机里的 Messenger App） | `telegram-mtproto-ai/src/integrations/messenger_rpa/` |
| skill_manager / KB / trigger / 回复生成的主体 | `telegram-mtproto-ai` |

本 repo 的 "Messenger" 指 **Facebook Messenger 通过 FB App / Messenger App 的 UI 自动化**（`facebook.py::send_message` / `check_messenger_inbox` 等），走的是 mobile-auto0423 自己的 VLM Level 4 fallback 栈，**与 telegram-mtproto-ai 的 messenger_rpa runner 完全独立**（两套实现，不共享代码）。

## Claude 协同约定（A/B 通用）

- **身份**：本 repo 双 worker 协作（A = greeting bot，B = messenger chat bot）；两 Claude 共用 `victor2025PH` token，身份由**物理机器 + `INTEGRATION_CONTRACT.md §二` 的独占区**决定，**不**由本文件自动 assign。进入本 repo 前先确认自己改的文件归谁。
- **Git workflow**：`feat-a-*` / `feat-b-*` 分支 → PR → approve-equivalent review → squash merge。不直推 main。
- **approve-equivalent 模式**：共用 token 导致 GitHub 拒绝 author `APPROVED`。用 `COMMENTED` + marker `"✅ A 侧 review 通过 (approve-equivalent)"` 代替；`auto_merge_stack.py` / `check_a_activity.py` 识别该 marker 放行。
- **共享 Claude 记忆**：`~/.claude/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组（同一 Claude 账号轮流在 A/B repo 工作，单点记忆库）。

## 崩溃恢复

Claude 崩溃后重入本 repo：

1. **先读 [`docs/SYSTEM_RUNBOOK.md`](docs/SYSTEM_RUNBOOK.md)**（运维 SSOT，§0 §1 §3 §4）
2. `git fetch origin && git checkout main && git pull`
3. `gh pr list --author victor2025PH --state open` 看自己还有哪些 PR 在开
4. 读 `~/.claude/projects/C--telegram-mtproto-ai/memory/MEMORY.md` 的 "Project: mobile-auto0423" 段，重建上下文
5. （历史参考）`docs/runbook/B_RESUME_2026-04-23-EVENING.md` 是 B Claude 当时的恢复脚本

## sibling Claude 长任务前置检查（2026-04-26 加，PR #111/#112 事故教训）

启动**任何** commit/refactor 工作前必跑（任选其一）：

1. **`repo_health.bat -Fetch`** — 一键 fetch + 看 branch + dirty + 仓库状态（推荐）
2. **`sync_with_main.bat`** — 专项 fetch + 报告 ahead/behind（read-only，安全）

**新分支创建**：用 **`branch_create.bat [name]`** 一键 `git checkout -b feat-ops-name-yyyy-MM-dd main`，避免手敲漏切。

**rebase 同步**（feat-* 分支落后 origin/main 时）：**`sync_with_main.bat -Rebase`** 把当前分支 rebase 到 origin/main，自动检查 dirty + 冲突提示。

### 事故案例（2026-04-26）

- **PR #111** 合并期间：victor squash 我之前 6 个 commit 到 main，我不知情继续工作，第 7 个 commit (4fbee97) 误落 main。修复：`git branch new + reset --hard HEAD~1`。
- **PR #112** 合并期间：又一次 squash 我 4fbee97 到 main，stage-b 分支 obsolete。修复：基于 main 起 stage-c + cherry-pick 新 commit。

频率: ~7 min 一次 PR 合到 main。**每个 commit 前必查 origin/main 状态**。

### 防呆工具链

| 工具 | 时机 | 抓什么 |
|------|------|------|
| `repo_health.bat` | 任何时候 | branch + dirty + behind local main + **\[7/7\] sibling collision** |
| `repo_health.bat -Fetch` | commit/push 前 | + behind origin/main（实时） |
| `sync_with_main.bat` | sibling 协同期间 | fetch + ahead/behind 报告 |
| `sync_with_main.bat -Rebase` | feat-* 落后 origin/main 时 | fetch + rebase + 冲突提示 |
| `branch_create.bat` | 开新工作前 | 一键建 feat-ops-* 分支 |
| `safe_commit.bat "<msg>"` | **commit 时（替代 git commit -a）** | branch + staged 清单 + 跨模块风险 + 二次确认 |
| `install_hooks.bat` | 仓库初始化时一次 | 把 `pre_commit.ps1` 装到 `.git/hooks/pre-commit` |
| `start.ps1` | 启动服务前（自动） | main+dirty 警告 |

### P2-⑥ sibling commit 防护（2026-04-28 升级）

事故根因：PR #142 期间 sibling Claude 用 `git commit -a` 把我 staged 的 5 个 P2 文件**打包进它的 OPT-FP1 messenger commit 6f80638**。CLAUDE.md 文字警告"共享 worktree"靠人 review 不可靠，工具兜底:

- **`safe_commit.bat`** 强制不用 `-a`，commit 前显示 branch + staged + 跨模块风险 + 二次确认
- **`pre_commit.ps1`** 加 4 个新检查（warning 不 block）：跨 src/ 模块 staged / `feat-ops-*` 分支 staged facebook.py / stash > 5 / untracked > 8
- **`repo_health.bat \[7/7\]`** 章节：stash count / 跨模块 dirty / 1h 内 sibling commit / branch vs dirty 不匹配，输出 collision risk score

### P3-A stacked PR 链 merge 教训（2026-04-28 加，事故 #141 → #154 教训）

**绝对不要建 stacked PR 链**（`#142 base=#141`, `#143 base=#142` ...）。GitHub 的 squash merge + delete-branch 会**直接 close 下游 PR**（不是改 base 到 main），且 **closed PR 无法 reopen**（API 拒绝 `Cannot change base branch of closed PR`）。

**遇到了怎么救**：

1. **新 worktree 隔离 sibling**：`git worktree add ../mobile-auto-pr-recover origin/main` 起独立工作树，sibling 切主 worktree 不影响。
2. **累积 cherry-pick + 多分支 ref**：在新 worktree 上 `cherry-pick` 每个上游 PR 的 commit，每步 `git branch recover-N HEAD` 打 ref。最后 push 6 个分支 + create 6 个 PR base=main。
3. **每个 PR diff 显示累积，但 squash 时 GitHub 自动 patch-id dedup**：`gh pr merge --squash --delete-branch` 顺序合并 6 个，main 上每个 commit 只含 net diff，0 conflict。
4. **`UNSTABLE != blocked`**：CI 还在跑就显示 UNSTABLE，但只要 `mergeable=MERGEABLE` + repo 没强制 required checks，可以直接 squash merge，省下 ~14min × N 的 CI 等待。

**正确 PR 工作流**：每个独立改动建一个 base=main 的 PR；如果改动逻辑相关但跨多个独立模块，**用累积 cherry-pick 拆 N 个独立 PR**（base 都=main，diff 累积，merge 时自然 dedup）。**绝不让任何 PR base = 另一个未 merge 的 PR**。

### 崩溃恢复扩展

**Claude 新 session / 崩溃后**：除了上面"崩溃恢复"5 步，还应跑 `repo_health.bat -Fetch` 看 sibling 期间发生了什么。

## 日常测试

```bash
python -m pytest tests/ -x -q --ignore=tests/e2e -k "not real"
```

commit 前必跑。跨边界改 PR 必须 @对方 review。
