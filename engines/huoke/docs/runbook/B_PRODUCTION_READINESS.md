# B 生产就绪状态 (2026-04-24 pm)

> **作者**: B Claude
> **日期**: 2026-04-24
> **范围**: 本文件是 B Claude 在崩溃恢复 + 自主开发回合 (P1 audit) 完成后的生产就绪度盘点, 供未来 B 会话重入 / A 状态查询 / 运维参考。
> **git snapshot**: main HEAD = `cbf8b08` (PR #1 merged); 工作目录 `C:\code\mobile-auto0423`

---

## 一、已合 main (2026-04-24)

| Merge commit | PR | 内容 | 合入者 |
|---|---|---|---|
| `1f57353` | #6 | P0 inbox: 跨 bot 归因回写 + 多语言意图识别 | 崩溃前 B |
| `46ddb98` | #7 | P1 check_friend_requests_inbox 双维度 lead_score gate | 崩溃前 B |
| `860de18` | #26 | check_a_activity review 看板 + `_is_approve_equivalent` | 本次 recovery |
| `4c67268` | #28 | auto_merge_stack + `fffa7a5` 实战 `unknown mergeable_state` race fix | 本次 recovery |
| `cbf8b08` | #1 | P2 send_message 结构化 MessengerError + 7 档 error code | 本次 recovery (rebase 后) |

## 二、本次开出但等 A review 的 B 自主 PR

| PR | 标题 | 用途 |
|---|---|---|
| **#31** | `fix(auto_merge_stack): base 非 main 且 parent PR 未合 → blocked` | 修 `auto_merge_stack.py` 踩过的 base→main 自检坑 (PR #10 误入 #9 分支的根因) |
| **#32** | `chore(b-minors): Round 3 review 的 5 个 non-block follow-up` | 打包实施 M2.1/M3.1/M4.2/M5.1/M5.2 全部 5 项 A 标注的 B 侧 minor |

## 三、14 等 A review 的栈 PR (无新增)

按栈拓扑:

| PR | base | 一句话 |
|---|---|---|
| #11 | main | docs: B→A Round 3 review 请求 |
| #8  | main | docs: B→A review 请求索引 + 10 问 |
| #2  | main | P3 长久记忆系统 (LLM prompt 注入派生画像) |
| #3  | #2   | P4 意图分类器 (rule-first + LLM fallback) |
| #4  | #3   | P5 引流时机决策闸 (3 层决策矩阵) |
| #5  | #4   | P6 消息请求陌生人自动回复 (stranger gate) |
| **#9** | #5 | **follow-up F2+F3+F4-support+F6 (Phase 7c 触发依赖)** |
| #12 | #10→main (auto retarget) | workflow smoke runner + 部署指南 |
| #13 | #12 | P8+P9+P10 漏斗 + 健康反馈 + L3 记忆读侧 |
| #14 | #13 | P10b LLM facts 抽取写侧 (默认关) |
| #15 | #14 | P11 真机 live smoke (只读路径) |
| #20 | feat-b-production-dryrun | P11c dry-run 矩阵 + P14 意大利语规则 |
| #21 | #20 | P13 运维观察看板 (markdown + --diff) |
| #22 | #21 | rebase_assistant (dry-run + apply) |

## 四、Production Readiness Checklist

### ✅ Code Quality (main + PR #32)
- `pytest tests/ --ignore=tests/e2e` → **1179 passed, 2 failed, 178 warnings, 318s** (on `feat-b-round3-minors` == main + 5 minor)
- 2 个 failing test **均为 pre-existing, 与 B recovery/minor PR 无关**:
  - `tests/test_api.py::TestTaskEndpoints::test_create_task` — AssertionError
  - `tests/test_behavior.py::TestComplianceGuard::test_cleanup_old` — assert failure
- Syntax/Lint + Py 3.11/3.12/3.13 + Playwright E2E: 由 CI 跑 (base=main 的 PR 有覆盖, 栈上层 PR 合入 main 时才触发 — 正常配置)

### ✅ Tool Chain
- `scripts/check_a_activity.py` — approve-equivalent 识别实战 verified (exit 0, 2/2 ✅), 44/44 tests
- `scripts/auto_merge_stack.py` — 本次发现并修 base→main 自检 bug (PR #31 等 review), 35/35 tests
- `scripts/rebase_assistant.py` — 在 PR #22 栈上 (未合 main), 本次未动
- 小改进留 future (非紧急): `github_api` 加分页 (目前 PR 数 < 100 未触顶) / 429 指数退避

### ✅ 契约合规 (INTEGRATION_CONTRACT §二)
- A 独占区任何时候都不动 (本次 recovery 全程尊重)
- MessengerError 7 码 + `send_blocked_by_content`: docstring 契约固化 + §7.6 分流矩阵对齐
- `fb_contact_events` 3 触发点 (PR #10): `_emit_contact_event_safe` + `_messenger_active_lock` + `message_received` / `wa_referral_sent` / `add_friend_accepted`
- `messenger_active` 共享锁 section (§7.7): B 端 `_messenger_active_lock` 用对了
- `mark_greeting_replied_back` → `record_contact_event(greeting_replied)` 双写 (§7.8): `_sync_greeting_replied_contact_event` 实施 + M2.1 注释固化契约

### ⚠️ 已知 issues (不 block main 但需关注)

1. **PR #10 `09ccc79c` 误合到 `feat-b-followup-a-review` 分支** (而非 main): 原因是旧 `auto_merge_stack.py` 的 base check 缺口, PR #31 已提修复等 A review。补救: A review PR #9 时看到 combined diff (原 #9 + #10 fb_contact_events) 一并 re-approve, 不单独 revert history。

2. **Pre-existing pytest 2 failures** (见 §四 Code Quality): 非 B 引入, 非本次 minor 回归。建议 A 侧排查 `test_api.py::test_create_task` 和 `test_behavior.py::test_cleanup_old`; 不 block B 栈合并。

3. **Ops runbook gap**: `docs/` 下缺 messenger-bot 专用部署/oncall/observability doc。存在的 A 侧 `messenger_rpa_progress_report.md` 和 `CONTACTS_RPA_INTEGRATION.md` 覆盖 contacts subsystem (另一个项目), 不是本 repo。建议后续开 `docs/B_OPERATIONS_GUIDE.md` 补: 启停/日志路径/风控排查/Phase 重置/DB migrations。本次 P1 范围内不生成以避免 scope creep。

4. **栈上层 PR 无 CI checks**: 由 CI 配置 `on.pull_request.branches: [main]` 决定; 栈 PR 合进 main 时才触发, 不是 bug。A review 时 CI 绿牌要在 `auto_merge_stack` 把 base retarget 到 main 之后看。

### ⏳ Production Blockers

- **14 open 栈 PR 等 A review** (尤其 **#9** 是 Phase 7c 触发门槛, 它会带着被"暗叠"的 #10 一起进 main)
- **PR #31** (auto_merge_stack base safety): 未 review / 未合
- **PR #32** (b-minors M2-M5): 未 review / 未合

A review 全部通过 + 合 main 后, B 侧即达"功能就绪"; **真·生产就绪** 还需:
- (a) 真机 smoke 测试 (INTEGRATION_CONTRACT §八 留有 item: "真机 smoke 谁跑"待定)
- (b) Phase 7c PR (A 侧) 合入 — 带来 A1 `LockTimeoutError` 子类 (让 B `_messenger_active_lock` 从字符串 matching 改 exception subclass) + A2 `send_blocked_by_content` A 侧分流实现 + Phase 5 `record_contact_event` infrastructure
- (c) ops runbook (§四.3)

## 五、Recovery path (给未来 B 会话)

1. **身份确认**: 读 `~/.claude/projects/C--telegram-mtproto-ai/memory/` 下的 `reference_mobile_auto0423.md` + `project_stack_rebase_done.md` + `project_approve_equivalent_pattern.md` + `project_auto_merge_stack_base_bug.md`
2. **环境重建** (`reference_mobile_auto0423.md`):
   ```bash
   # gh portable: 如果 C:\Users\victo\bin\gh.exe 不在, zip 装
   # token: 每个 gh 调用前 prefix
   export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | awk -F= '/^password=/{print substr($0,10)}')
   # commit 身份
   export GIT_AUTHOR_NAME='claude-messenger-bot' GIT_AUTHOR_EMAIL='claude-messenger@local'
   export GIT_COMMITTER_NAME='claude-messenger-bot' GIT_COMMITTER_EMAIL='claude-messenger@local'
   ```
3. **状态核查**: `gh pr list --state open --author victor2025PH` 对比本文件 §三; `check_a_activity --hours 24` 看 A 近期动作; `auto_merge_stack.py`  (dry-run) 看栈可合情况
4. **禁忌**: A 独占区不碰 (见 `INTEGRATION_CONTRACT.md §二`); 已合 main 的 PR 不 rewrite history; 不替 A 合 A 的 PR; force-push 只限 feature branch 且 `--force-with-lease`

## 六、历史变更

- 2026-04-24 首版 — 由 B 在崩溃恢复 + 自主 P1 audit 完成后起草
