# B 下一步操作指令 — 2026-04-23

> **场景**：A 的 PR #23 (Phase 3-7a + audit_logs 修复 + FB 搜索真机修复) 已合入 main (commit `85a191f`)；A 的 round 3 答复 PR #24 也已合入 main (commit `a2ae0af`)。B 启动会话后按本文件执行。

---

## 1. 同步代码

```bash
git fetch origin && git pull origin main
# main 现在含 A 的 PR #23 + PR #24
```

## 2. 读 A 的答复 — 重点 §一 / §二 / §五

```bash
cat docs/A_TO_B_ROUND3_REPLY.md
```

## 3. 跑 rebase_assistant 出冲突预测（dry-run，不改东西）

```bash
python scripts/rebase_assistant.py
```

输出会告诉你 17 个 `feat-b-*` PR 各自跟新 main 的冲突情况。

## 4. 如果预测全可解 → 批量执行

```bash
python scripts/rebase_assistant.py --apply --test --push
```

自动 rebase + 真机测试 + 推送，conflict auto-abort + backup。

## 5. 如果出现不可解冲突

- 不要手 rebase
- 把冲突清单 + backup 路径贴到 PR #24 评论
- A 24h 内协助手解，告诉你哪边是 source-of-truth

## 6. rebase 全绿后通知 A

在 PR #24 评论留言：

> rebase 完成 (X/17 PR 绿)，请 review §二 4 项

这会触发 A 的 24h review window，A 会 review:

- PR #10 (P7 §7.1 fb_contact_events)
- PR #6 append `aafe1d4`
- PR #7 append `6b1c249` + `bf984a8`
- PR #1 append `fd1e9dc`

review 结果会写到 `docs/A_TO_B_ROUND3_REVIEW_RESULTS.md`。

## 7. A review 通过后，按拓扑顺序合并

```
#6 → #7/#1 → #2→#3→#4→#5 → #9→#10 → #12→#13→#14→#15 → #20→#21→#22 → #8/#11
```

## 8. 你 PR #9 (`count_unreplied_greetings_to_peer` helper) 合入 main 后

通知 A：A 在下个 PR (Phase 7c) 里加 peer 5x 上限消费 3 行到 `send_greeting_after_add_friend`。

---

## A 侧重要变化（无需你改代码，但要知道）

- `audit_logs` 用了 `init_db._PRE_MIGRATIONS` 机制 (commit `a2ba0dd`) — 你现 migration 不变。日后改 audit_logs 列名时按 `_PRE_MIGRATIONS` vs `_MIGRATIONS` 决策（见 PR #24 §一.1.1 判定规则）。
- `requirements.txt` 启用了 `httpx` (之前注释为可选，但 `src/ai/llm_client.py` / `openclaw_agent.py` / `vision/backends.py` + `fastapi.testclient` 硬依赖)。
- 新增 `src/app_automation/{fb_profile_signals, fb_search_markers}.py` (A 独占)。你 Messenger 路径若需"是不是 Home / Messenger 页"判断可直接 `from .fb_search_markers import hierarchy_looks_like_messenger_or_chats` 复用。
- `tests/test_phase7.py` 5 个 hardcoded `D:\...` Windows 路径已修，main CI 现在真反映健康（之前 main CI 一直被这个死测拖红）。

---

## A 当前状态

A 现在**不会再开新 PR**（Phase 7c WIP 9 改 + 11 新文件等你栈进 main 后才动），避免你多跑一轮 rebase。

不清楚的回 A (在 PR #24 评论)。开始执行。
