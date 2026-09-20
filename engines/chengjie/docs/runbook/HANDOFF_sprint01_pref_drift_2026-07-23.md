# 便签：`test_pref_drift_release_after_stable_switch` 红灯 = 测试陈旧（非代码回归）

> 分支：`sprint01/security-and-message-invariants`　日期：2026-07-23　留言人：ops/日志线 agent
> 面向：正在做 `lang_policy` / `skill_manager` 语言偏好线的 sibling agent

## 一句话
`tests/test_lang_e2e_acceptance.py::test_pref_drift_release_after_stable_switch` **当前为红**，
`assert 'zh' == 'ja'`（`:269`）。经只读定位：**代码行为是对的（期望的），是测试的时序模型
落后于代码**。请改测试，别改代码。

## 根因
偏好释放规则（`src/ai/lang_policy.py:559-604`）：释放需要「**本条 + 最近一条历史强证据**」
两条同语言 L，且 L ≠ 偏好语言 ≠ 请求时书写语言（`lang_pref_input`）。即 current + 历史 1 条 = 连续 2 条。

`src/skills/skill_manager.py:1114-1122` 有一段**时序补齐**——把 `last_message`（上一轮用户消息）
临时并入语言扫描窗口，**故意**让 `process_message` 与收件箱 draft 产线（本就传入含当前消息的
完整历史、第 2 条即释放）**对齐同一轮释放**：

```python
_lang_hist = list(user_context.get("_conversation_history") or [])
_lm_prev = str(user_context.get("last_message") or "").strip()
if _lm_prev and _lm_prev != _stripped:
    _lang_hist.append({"role": "user", "content": _lm_prev})
```

时序推演（第 2 条中文 msg2 决策时）：`last_message`=上一轮 msg1(zh) → 补齐后窗口含 msg1(zh) →
current msg2(zh) + 历史 msg1(zh) = 连续 2 条 → **第 2 条即 `stable_switch` 释放**。

而测试（`tests/test_lang_e2e_acceptance.py:239-247` 的注释 + `:265-277` 的断言）还锁着**补齐前**的
滞后模型（第 3 条才释放，第 2 条仍 `ja`）。测试注释自己都写了「收件箱产线第 2 条即释放」，却对
`process_message` 断言第 3 条 —— 就是补齐上线后测试没同步。

## 建议修复（改测试）
在 `test_pref_drift_release_after_stable_switch`：
1. 第 2 条中文后（`:267-270`）：改断言**已释放** → `reply_lang == "zh"`、`"user_lang_pref" not in uc`、
   `"user_lang_pref_input" not in uc`；`stable_switch` 埋点在此已 ≥1。
2. 原第 3 条块（`:272-280`）：改成「保持 zh」的幂等断言，或直接删（已冗余）。
3. 顶部时序注释（`:239-247`）：重写为「补齐后 process_message 与收件箱产线**同轮**释放（第 2 条
   强证据即释放）」。

## 为什么别改代码
去掉 `skill_manager.py:1119-1122` 的 `last_message` 补齐虽能让测试原样过，但会**重新制造两条
链路释放时机不一致**（process_message 晚一轮、收件箱早一轮），是 regression。补齐是对齐的正解。
仅当产品明确要求「process_message 比收件箱晚一轮释放」才该动代码 —— 不推荐。

## 如何验证
```
python -m pytest tests/test_lang_e2e_acceptance.py::test_pref_drift_release_after_stable_switch -q
```
改测试后应绿；顺带全套：
```
python -m pytest tests/test_lang_policy.py tests/test_lang_e2e_acceptance.py tests/test_lang_policy_golden.py -q
```

## 如何被发现（给 orchestrator / 其他 agent）
- 本便签在 `git log` 里由提交主题点名（含 `pref_drift`）；`rg pref_drift engines/chengjie/docs/` 可直达。
- 该失败测试文件 `tests/test_lang_e2e_acceptance.py` 目前是 sibling 的**未跟踪**文件，我**没有触碰**。
- 若想让 CI 先绿+保留可见：sibling 可自行把该用例标 `@pytest.mark.xfail(reason="stale timing, see docs/runbook/HANDOFF_sprint01_pref_drift_2026-07-23.md")`（我不代改他人在建测试）。
