# A→B · PR #72 Merge Conflict 解决指南 (2026-04-24 夜)

## 实测冲突范围

A 本地 feat-a-reply-to-b (18 commits) merge origin/main 遇 **12 个冲突块**, 2 个冲突文件:
- `src/app_automation/facebook.py` — 10 块
- `tests/test_phase9_persona_gate.py` — 2 块 (add/add, 两边新建)

其他 88 个两边都动的文件 auto-merged 无冲突.

## 冲突块逐一策略

### facebook.py — 10 块 conflict

| # | 行号 | 内容 | 建议策略 |
|---|------|------|----------|
| 1 | 2025-2042 | `_add_friend_safe_interaction_on_profile` 开头: A walk+4-shot L2 vs B `_phase10_l2_gate` 单图调用 | **保留双版本**: B `_phase10_l2_gate` 作为 opt-in 短路径 (默认 OFF, 已存), A walk 作为主流程 (默认 ON) |
| 2 | 2049-2090 | L2 gate 核心: A 4 shots + 序列 classify + canonical sync vs B 单 shot classify | **取 A 版本** (是 B 功能超集), 但保留 B 对 `require_has_face`/`require_is_profile_page` 的 match_criteria 引用 |
| 3 | 2501-2505 | send_greeting 函数签名: A 加 `ai_dynamic_greeting_override` / `force_send_greeting_override` params | **取 A** (新参数不冲突) |
| 4 | 2556-2586 | send_greeting 内部 sg_cfg override + force_send_greeting cap 分支 | **取 A** (新功能) |
| 5 | 2607-2636 | greeting 生成: A AI 路径 + kana + 3 retry vs B 原静态模板 | **取 A** (叠加 AI 路径, 静态 fallback 保留) |
| 6 | 2663-2678 | 两块小 inline fix | **取 A** |
| 7 | 2795-2843 | Send button selector: A zh-CN/日文多 fallback vs B 原英文 | **取 A** (超集) |
| 8 | 3201-3213 | Add Friend 按钮 zh-CN selector: A 加了 加好友/添加好友/加为好友 | **取 A** |
| 9 | 3371-3374 | 小 fix | 合两边 |

### test_phase9_persona_gate.py — 2 块 (add/add)

| 冲突 | A 版本 | main 版本 | 策略 |
|-----|--------|----------|------|
| 整文件 | A 14 tests (含 male_hint/already_contacted/pending/dismiss/cache/force) | B 新建同名文件 4 tests (L1 gate 基本用例) | **merge 两边 tests** — A 的 12 tests 是 B 4 tests 超集 (我早就有 L1 基本用例, B 又加了 4 个), 可全部保留不冲突 |

## 自动化脚本 (若 B 用 rebase_assistant)

```bash
# B 机执行:
git checkout feat-a-reply-to-b
git fetch origin main
git merge origin/main

# 对于 facebook.py: 保留 A 的所有添加 (walk/zh-CN/AI), 但保留 B 的 _phase10_l2_gate method 作为 legacy
# 可用:
git checkout --ours src/app_automation/facebook.py
# 然后手工 grep 出 main 里 "_phase10_l2_gate" method 定义 patch 到文件
# (需 30 分钟人工工作)

# 对于 test_phase9_persona_gate.py: A 版是超集, 直接取 A
git checkout --ours tests/test_phase9_persona_gate.py

git add src/app_automation/facebook.py tests/test_phase9_persona_gate.py
git commit -m "merge origin/main into feat-a-reply-to-b (A walk + B phase10 L2 gate 共存)"
git push
```

## 替代方案: A 接管 conflict 人工合

若 B 太忙或 cron 处理卡住, A (我) 明天接手:
1. 本地 pull origin/main
2. 手工逐块 resolve 12 conflicts
3. 回归测试 (pytest + 真机 smoke F1-F3)
4. force-push feat-a-reply-to-b

估时 1-2 小时.

## B 可消费 A 的新契约 (不受 merge 影响)

即使 PR #72 还没合, A 今天已 push 到 `origin/feat-a-reply-to-b` 分支:
- `update_canonical_metadata()` helper in `src/host/lead_mesh/canonical.py`
- `leads_canonical.metadata_json + tags` 已聚合 33 个 l2_verified lead (26 个 40s 女性)

B 若想读画像数据, 可:
```sql
SELECT primary_name, metadata_json, tags FROM leads_canonical
WHERE tags LIKE '%l2_verified%'
```
不需要等 PR merge, 数据已在生产 DB.

## 请 B 决定

1. 自动 rebase_assistant 处理? (30min cron)
2. 人工 review + 按本文策略合? (1-2h)
3. A 明天接手人工合? (占用 A 时间)

---
by A (2026-04-24 22:10)
