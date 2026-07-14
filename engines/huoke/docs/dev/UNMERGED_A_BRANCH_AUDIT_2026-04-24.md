# A 分支未合并工作 audit · 2026-04-24

> **作者**: A 机 Claude (会话 audit)
> **触发**: 评估 Phase 10 (L2 VLM gate) prep 时发现 Phase 9 prerequisite 不在 main
> **结论严重性**: **高** — 10 个 feature commit (净 +3248 / -17278 行) 累积在 `feat-a-reply-to-b` 分支，**未合 main**，生产用户使用 main 时缺失这些功能

---

## 〇 TL;DR

PR #23 (`feat-a-reply-to-b → main`) 于 2026-04-23 合入后，**有 A 会话继续在该分支 push commit 但没开新 PR**。导致 10 个 commit (Phase 7d / Phase 8 系列 / Phase 8h / Phase 9 / Phase 10-lite) 漂在分支上，main 没有。

**冲突预测**: ✅ Light 预测显示 10 个 commit 触碰的所有文件 main 后续都没动过，**单个 cherry-pick 大概率 clean**（caveat: 未测 sequential 组合，但 file-level overlap 是 0）。

**推荐**: **(B) 逐个评估 + 合入** — 风险低（无文件冲突），价值高（恢复 Phase 7d/8/9 全部 A 侧工作）。我可以一次性把 10 commit cherry-pick 到一个新 PR，CI 通过即合。

---

## 一 怎么发生的

```
2026-04-23 14:00  PR #23 合入 main (feat-a-reply-to-b → main, merge commit 85a191f)
2026-04-23 ~     PR #23 之后, 有 A 会话继续在 feat-a-reply-to-b 分支 push 10 个 commit
                  (但没开新 PR — 工作流误以为 push 即合, 或忘了 gh pr create)
2026-04-24 早    另一个 A 会话写 phase9_dev_plan.md memory, 描述"Phase 9 完成 1099 tests passed"
                  但其实是在 feat-a-reply-to-b 分支跑的, 不是 main
2026-04-24 晚    本 audit 会话评估 Phase 10 readiness, 发现 main 上 add_friend_with_note
                  无 persona_l1_rejected gate → 反推发现 10 commit 全部漏合
```

**根因**: PR-based workflow 中, 同一 branch 在 PR 合并后继续累积 commit 是合法的（branch 没自动删）, 但 GitHub UI 不会自动提醒"此 branch 又有新 commit, 要不要开新 PR"。**A 侧没有定期检查 `git log origin/main..origin/feat-a-*` 的习惯**。

---

## 二 10 个未合 commit 详细

### 全表 (按时间序, 老 → 新)

| # | SHA | Phase | 标题 | 触碰文件 | Conflict 预测 |
|---|---|---|---|---|---|
| 1 | `6b2552b` | 7d P0+P1 | Messenger send 硬化 + 自适应时序 + 精确匹配 | `src/app_automation/facebook.py`, `tests/test_phase7c_messenger_greeting.py` | ✅ clean |
| 2 | `d67eff4` | 8 准备 | 漏斗报告 + AutoSelector cache 健康扫描 | `scripts/autoselector_cache_health.py`, `scripts/phase8_funnel_report.py`, `src/host/autoselector_health.py`, `src/host/lead_mesh/funnel_report.py`, `tests/test_autoselector_health.py`, `tests/test_phase8_funnel_report.py` | ✅ clean |
| 3 | `f211c1b` | 8b | Command Center 集成 A 端获客漏斗实时卡片 | `src/app_automation/facebook.py`, `src/host/routers/lead_mesh.py`, `src/host/static/js/lead-mesh-ui.js`, `tests/test_phase8b_funnel_api.py` | ✅ clean |
| 4 | `d2e2881` | 8d | Command Center 过滤器 + 瓶颈下钻 | `src/host/lead_mesh/funnel_report.py`, `src/host/routers/lead_mesh.py`, `src/host/static/js/lead-mesh-ui.js`, `tests/test_phase8_funnel_report.py`, `tests/test_phase8b_funnel_api.py` | ✅ clean |
| 5 | `06ef254` | 8e | 时序 sparkline (纯 SVG 零依赖) | `src/host/lead_mesh/funnel_report.py`, `src/host/routers/lead_mesh.py`, `src/host/static/js/lead-mesh-ui.js`, `tests/test_phase8_funnel_report.py`, `tests/test_phase8b_funnel_api.py` | ✅ clean |
| 6 | `57944c1` | 8g | Sparkline 点击下钻 + 单日过滤 | `src/host/lead_mesh/funnel_report.py`, `src/host/routers/lead_mesh.py`, `src/host/static/js/lead-mesh-ui.js`, `tests/test_phase8_funnel_report.py`, `tests/test_phase8b_funnel_api.py` | ✅ clean |
| 7 | `3751c95` | 10-lite | Command Center 15s 自动刷新 + toast 防抖 | `src/host/static/js/lead-mesh-ui.js` | ✅ clean |
| 8 | `a6f2cbc` | 8h | 一键 blocklist 机制 (跨 device 跨 agent 骚扰保护) | `src/app_automation/facebook.py`, `src/host/database.py`, `src/host/lead_mesh/__init__.py`, `src/host/lead_mesh/blocklist.py`, `src/host/routers/lead_mesh.py`, `src/host/static/js/lead-mesh-ui.js`, `tests/test_phase8h_blocklist.py` | ✅ clean |
| 9 | `da9bf4e` | 9-extract P0 | 群成员打招呼断点修复 + fail-fast 诊断工具 | `scripts/debug_extract_members_trace.py`, `scripts/smoke_extract_members_realdevice.py`, `src/app_automation/facebook.py`, `src/host/executor.py` | ✅ clean |
| 10 | `f969dbc` | 9 C | add_friend persona L1 gate (名字启发式自动拦截) | `src/app_automation/facebook.py`, `tests/test_phase9_persona_gate.py` | ✅ clean |

**总 diff vs main**: `+3248 / -17278` 行 (净 −14000，主要是 A 这条线删了 17000+ 行旧实现重写为新 dashboard 模型)

### 各 commit 价值 (主观评估, 视用户需求)

- **必合**: #10 Phase 9 C persona gate (用户明确要求, 阻止"日本女性 37-60"客群外的 add_friend)
- **必合**: #1 Phase 7d Messenger send 硬化 (生产 send_message 稳定性)
- **必合**: #8 Phase 8h blocklist (反骚扰保护)
- **强烈推荐**: #2-#7 + #11 Phase 8 dashboard 系列 (Lead Mesh 可视化, B 也做了部分但走的是不同方向, 不重叠)
- **价值待定**: #9 Phase 9-extract (群提取断点修复 — Phase 9 doc 说"FB UI 对 u2 自动化有盲区, 群提取需视觉方法", 本 commit 可能只修了部分; 跑真机才知)

---

## 三 其它 A 分支扫描

```bash
$ for ref in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/feat-a-*); do
    count=$(git log origin/main..$ref --oneline | wc -l)
    [ "$count" -gt 0 ] && echo "$ref: $count commit(s) 未合"
done

origin/feat-a-reply-to-b: 10 commit(s) 未合 main
```

**只有 `feat-a-reply-to-b` 一个分支有累积。** 其它历史 feat-a-* 分支 (round3-reply / b-next-steps / b-resume-v2 / phase7c / unmerged-audit / 等) 都干净 (PR 合后无新 commit)。

---

## 四 Conflict 预测方法 + caveat

**方法**: 对每个 commit, 列出其触碰文件, 然后 `git log 7b70810..origin/main -- <file>` 看 PR #23 之后 main 是否动过这些文件。**file-level overlap 为 0 → 单个 cherry-pick 大概率 clean**。

**Caveat (诚实提示)**:
- ✗ 未测 **sequential 组合**: 10 commit 顺序 cherry-pick 时, 后续 commit 可能依赖前序 commit 引入的 hunk 上下文。但因为这 10 commit 本身就是顺序写的, sequential CP 应该等价于"重放分支历史"
- ✗ 未测 **import / API 表面变化**: 比如 `_add_friend_with_note_locked` 在 Phase 9 commit 加了 persona gate 调用 `fb_profile_classifier.classify`, 但 main 上 `fb_profile_classifier.classify` 的签名可能演变了。这种 API 漂移 file-level 看不出, runtime 才暴露
- ✗ 未跑 **真测试**: 全 cherry-pick 后 pytest 全回归是真正验证

**降低 caveat 风险的策略**: 
- 用 `git rebase --onto origin/main 7b70810^ feat-a-reply-to-b` 实测 (rebase 失败会 abort), 不直接 cherry-pick
- 或起 worktree 做 `git cherry-pick -x <sha1>..<sha10>` 一次过, 失败 abort

---

## 五 处置 3 选项

### (A) 全 cherry-pick 合 main · low effort, low-medium risk

```bash
git checkout -b feat-a-recover-phase78910 origin/main
git cherry-pick 6b2552b^..f969dbc   # 一次性 10 commit
# 如果失败 → abort, 走方案 B
git push -u origin feat-a-recover-phase78910
gh pr create --base main --head feat-a-recover-phase78910 --title "feat(fb): 恢复 Phase 7d/8/8h/9/10-lite (10 commit cherry-pick)"
```

- **预期**: file-level 无冲突 → cherry-pick 大概率成功
- **风险**: 未测 sequential / API 漂移. 如失败 abort 后走 (B)
- **耗时**: 5 分钟 (顺利) / 30+ 分钟 (失败需手解)
- **优势**: 一次性恢复全部
- **劣势**: PR body 要描述 10 个 phase 的工作, B 不一定能秒 review

### (B) 逐个 cherry-pick + per-commit PR · medium effort, lowest risk · ⭐ 推荐

按 #1 → #10 顺序, 每个 commit 独立 PR:

```bash
# 例: #10 Phase 9 C persona gate
git checkout -b feat-a-phase9-persona-gate origin/main
git cherry-pick f969dbc
python -m pytest tests/test_phase9_persona_gate.py -v   # verify
git push -u origin feat-a-phase9-persona-gate
gh pr create --base main --head feat-a-phase9-persona-gate --title "feat(fb): Phase 9 C · add_friend persona L1 gate"
gh pr merge ... --merge --auto
```

- **预期**: 10 个独立 PR (Phase 7d / Phase 8 / Phase 8b / 8d / 8e / 8g / 8h / 10-lite / 9-extract / 9 C)
- **风险**: 几乎零 (每 PR 独立 verify, 失败只 affect 1 PR)
- **耗时**: 1-2 小时 (10 PR × 5-10 分钟)
- **优势**: 最稳, B 可逐个 review
- **劣势**: PR 数量多, 看起来 noisy

### (C) Discard 接受 main 现状 · zero effort, lose features

- 10 commit 永远在 feat-a-reply-to-b 分支, 不进 main
- **优势**: 0 工作量
- **劣势**: 用户损失 Phase 7d/8/9/10-lite 全部 A 侧 feature, 包括用户明确要求的 persona gate
- **何时合理**: 仅当确认 B 的工作已等价覆盖 (本 audit 已 verify **没有覆盖**, 所以 (C) 不合理)

---

## 六 推荐执行 + 改进 workflow

### 立即执行 (我建议 (B))

我可以现在按 (B) 自动跑 10 个 cherry-pick + PR. 你授权一句"开始 (B)" 即可。

### Workflow 改进 (防再发生)

1. **A 侧 session 启动 sanity check** (新 routine):
   ```bash
   for ref in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/feat-a-*); do
     count=$(git log origin/main..$ref --oneline 2>/dev/null | wc -l)
     [ "$count" -gt 0 ] && echo "⚠️  $ref: $count 个未合 commit, 检查是否需 PR"
   done
   ```
   建议加入 A 的"启动 doc"或 memory `dual_claude_ab_protocol.md`

2. **memory 写"完成"前 verify main**: `phase9_dev_plan.md` 那种 "1099 tests passed" 的 claim, 必须先 `git fetch origin && git log origin/main..HEAD` 确认 commit 已合 main 再写

3. **PR-merged-with-pending-followup 提示**: B 写的 `auto_merge_stack.py` 已有合并工具, 可加 post-merge hook "branch 上还有 N 个 commit, 是否开 followup PR?"

---

## 七 附录 — git 命令速查

```bash
# 重新跑 conflict 预测 (万一今天到明天 main 又推进了)
for sha in 6b2552b d67eff4 f211c1b d2e2881 06ef254 57944c1 3751c95 a6f2cbc da9bf4e f969dbc; do
  msg=$(git log -1 --format='%h %s' "$sha" | cut -c1-90)
  files=$(git show --name-only --format='' "$sha" | grep -v "^$")
  conflict=""
  for f in $files; do
    if git log --oneline 7b70810..origin/main -- "$f" | head -1 | grep -q .; then
      conflict="$conflict $f"
    fi
  done
  [ -n "$conflict" ] && echo "⚠️ $msg → $conflict" || echo "✓ $msg"
done

# 预览所有 10 commit (顺序合到 main 后会是怎样)
git log --oneline 7b70810..origin/feat-a-reply-to-b
```

— A 机 Claude
