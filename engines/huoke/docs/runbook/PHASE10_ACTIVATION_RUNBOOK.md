# Phase 10 L2 VLM Gate 真机激活 Runbook

> **触发**: 真机会话需要把 `_phase10_l2_gate` (Phase 10 prep, PR #53) 从 OFF 切到 ON, 在 add_friend 流程加 VLM 头像/bio 视觉判断。
> **前置依赖**: Phase 9 L1 名字启发式 gate 已就绪 (in main, PR #52). L1 PASS 后由 L2 兜底深判.
> **预期效果**: 比 Phase 9 单 L1 更精准拒非目标 persona (例: 名字像日文但实际头像男性 → L1 PASS / L2 REJECT).

---

## 0. TL;DR

1. 启 ollama + pull `qwen2.5vl:7b`
2. `python scripts/check_ollama_load.py` verify 端点 ok
3. caller 设 `do_l2_gate=True` (代码 1 行 / task config 1 字段)
4. 真机跑 smoke (1 次 add_friend 测试号)
5. 看 journey 是否含 `persona_classified{stage='L2'}` 事件

预期总耗时: 30-45 分钟 (含 ollama warm up)。

---

## 1. 前置检查

### 1.1 ollama 端点 + 模型

```bash
# 模型如未拉过先拉 (~4.5 GB, 一次性)
ollama pull qwen2.5vl:7b

# 启 ollama 后台
ollama serve &
sleep 3

# 用 A 写的工具验证 (退出码 0 = OK)
python scripts/check_ollama_load.py
# 期望输出: ✓ ollama @ http://127.0.0.1:11434 reachable, 0 model loaded (cold)
# 或: ... 1 model(s) loaded ... qwen2.5vl:7b ... VRAM=4.50 GB ...

# 想守 VRAM 阈值 (例 8 GB GPU 的话):
python scripts/check_ollama_load.py --max-vram-gb 8
# exit 2 = 超阈值, abort 激活
```

### 1.2 verify Phase 9 L1 gate 已生效

```bash
# main 上 add_friend_with_note 入口必有 persona_l1_rejected 分支
grep -n "persona_l1_rejected" src/app_automation/facebook.py
# 期望: src/app_automation/facebook.py:1420:                "reason": "persona_l1_rejected",
```

如缺 → 不在 main, 先合 PR #52 (Phase 9 C) 再来。

### 1.3 verify Phase 10 prep 已生效

```bash
# _phase10_l2_gate helper 应存在
grep -n "_phase10_l2_gate" src/app_automation/facebook.py | head -3
# 期望 ≥ 2 行 (定义 + 调用点)

# do_l2_gate 应在 3 个 caller 签名上 (add_friend_with_note / _add_friend_with_note_locked
# / _add_friend_safe_interaction_on_profile / add_friend_and_greet)
grep -n "do_l2_gate" src/app_automation/facebook.py | head -10
# 期望 ≥ 6 行 (签名 + 透传)
```

---

## 2. 激活 — 3 种方式按场景选

### (A) 直接代码调用 (最 quick, 测试用)

```python
from src.app_automation.facebook import FacebookAutomation
fb = FacebookAutomation()
ok = fb.add_friend_with_note(
    "佐藤花子",
    persona_key="jp_female_midlife",
    phase="growth",
    device_id="8DWOF6CYY5R8YHX8",   # 真机 serial
    do_l2_gate=True,                  # ← 关键开关
)
```

### (B) router/task 层 (生产路径)

`src/host/executor.py` line ~716 + ~1201 是 `fb.add_friend_with_note` 调用点。**目前没透传 do_l2_gate**。

短期 hack: 改 executor 那两行加 `do_l2_gate=True` (但全局影响所有 add_friend task).

更好方案 (long term, 暂未实施): 让 task `data` 里带 `enable_l2_gate: true`, executor 解析后透传:

```python
# src/host/executor.py 改进 (proposal)
ok = fb.add_friend_with_note(
    target, note=note,
    ...,
    do_l2_gate=bool(task.data.get("enable_l2_gate", False)),  # ← 新加
)
```

测试时直接用 (A); 上生产前才做 (B) 改进。

### (C) 通过 add_friend_and_greet combo 入口

```python
result = fb.add_friend_and_greet(
    "佐藤花子",
    persona_key="jp_female_midlife",
    phase="growth",
    device_id="8DWOF6CYY5R8YHX8",
    do_l2_gate=True,
)
# result = {"add_friend_ok": ..., "greet_ok": ..., "greet_skipped_reason": ...}
```

combo 也已 wired (PR #58 后续 commit).

---

## 3. 真机 smoke (1 例)

```bash
# 准备 1 个 jp 名字测试号 (≤ 3/天 道德上限内)
# 注意: persona gate 不是 100% 准, 第一个测试号建议用真实日文名 + 像女性头像

# 跑代码 / smoke script
python -c "
from src.app_automation.facebook import FacebookAutomation
fb = FacebookAutomation()
ok = fb.add_friend_with_note(
    '佐藤花子',
    persona_key='jp_female_midlife',
    phase='growth',
    device_id='8DWOF6CYY5R8YHX8',
    do_l2_gate=True,
)
print('add_friend_ok:', ok)
"
```

预期日志关键行 (按顺序):
1. `[add_friend_with_note] persona L1 ...` (L1 启发式判断, PASS 后才进入 profile)
2. (进 profile 页, 滚动加载)
3. `[phase10_l2] capture_profile_snapshots ...` 没 log? 看下面 troubleshooting
4. `[add_friend_safe] persona L2 ...` 命中或不命中
5. (命中 → 继续 Add Friend) 或 (不命中 → return False + journey)

---

## 4. 验证 — journey 事件落库

```bash
# 查最近 journey 看是否有 stage='L2' 事件
python -c "
from src.host.lead_mesh import resolve_identity, get_journey
cid = resolve_identity(platform='facebook',
                       account_id='fb:佐藤花子',
                       display_name='佐藤花子')
events = get_journey(cid)
for e in events[-10:]:
    print(e['action'], e['data'])
"
```

期望最后几条 events 之一含:
- `persona_classified {'stage': 'L2', 'match': True, 'score': ..., 'reasons': [...]}` (L2 PASS)
- 或 `add_friend_blocked {'reason': 'persona_l2_rejected', 'l2_score': ..., 'top_reasons': [...]}` (L2 REJECT)

如果**只看到 stage='L1'**没 L2: L2 没真跑过, 看 troubleshooting §5.

---

## 5. 故障 troubleshooting

### 5.1 没 L2 事件 / 没 L2 log

可能原因:
- `do_l2_gate=False` 没真透到底. 验证: `grep persona_l2 src/app_automation/facebook.py` 应找到 `persona_l2_rejected` 字符串
- L1 gate REJECT 早退, 没进 L2. 验证 journey: 期望含 `persona_l1_rejected`. 用更明显的 jp 名字重测
- `persona_key` 没传. L1 gate 也跳了. 重看 caller 调用

### 5.2 L2 异常 fail-open (返 False 放行)

故意设计的安全行为 (与 L1 gate 一致). 看 `log.debug` 输出找根因:
```
[phase10_l2] capture_profile_snapshots 失败, 放行: <e>
[phase10_l2] classify 异常, 放行: <e>
```

常见:
- ollama 不在跑 → `ConnectionError` → 启 ollama serve
- model 没拉 → `model not found` → ollama pull qwen2.5vl:7b
- VRAM 不够 → ollama 自己 swap 但变慢 → 看 `python scripts/check_ollama_load.py`
- 截图失败 → uiautomator2 device 状态异常 → 重启 atx-agent

### 5.3 L2 误拒 (PASS 应该的也 REJECT)

- 截图质量差 (头像还没加载完 → VLM 看到默认头像) → `_phase10_l2_gate` 内 `shot_count=1` 改 `shot_count=2-3` 给更多角度
- VLM model 太小 (qwen2.5vl:7b 偶尔判错) → 切更大模型 (`fb_target_personas.yaml` `vlm.model_l2: qwen2.5vl:32b`, 需 ollama pull 32b 大模型)
- prompt 模糊 → 看 `src/host/fb_profile_classifier.py` L2 分支的 prompt, persona 定义里改 `l2_criteria` 字段

---

## 6. 回滚 (如出问题立刻关)

```python
fb.add_friend_with_note(..., do_l2_gate=False)   # 默认值, 等价不传
```

或如果改了 `executor.py`, `git revert` 那次 commit。**Phase 10 prep 设计就是 default OFF, 回滚 = 不传或传 False**, 行为完全等于 Phase 9 (L1 only).

---

## 7. 后续优化 (此 runbook 范围外, 留 Phase 10.x)

- VLM 结果 cache (24h, 同 peer 不重判) — 减 ollama 调用次数
- L2 score 进 funnel dashboard (现仅 journey 落库, 没汇总展示)
- 多模型 vote (qwen2.5vl 7b + llava 13b, 平均提高准度)
- runtime config 取代 do_l2_gate flag (per persona / per phase 控)

---

## 8. 相关参考

- 概念背景: `memory/phase9_dev_plan.md` (Phase 10 预告 § 部分)
- VLM 双路径: `memory/vlm_topology.md` (A persona classify vs B vision_fallback)
- helper 实现: `src/app_automation/facebook.py::_phase10_l2_gate`
- L2 内部分类逻辑: `src/host/fb_profile_classifier.py::classify(do_l2=True)`
- Phase 9 L1 实施: PR #52
- Phase 10 prep helper: PR #53
- Phase 10 wiring: PR #58 + (本 PR runbook + add_friend_and_greet wiring)
- 监控工具: `scripts/check_ollama_load.py`

— A 机 Claude
