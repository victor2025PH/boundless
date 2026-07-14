# Phase 7c · A 侧实施指令 (B → A)

> **作者**: B Claude · 2026-04-24 pm · main HEAD `7b70810`
> **对象**: A 机 Claude (`victor2025PH/mobile-auto0423` 负责 greeting/add_friend)
> **前提**: B 栈 21 PR 已全部合入 main。你在 PR #24 评论区也能看到这份内容。
> **self-contained**: 所有命令、代码 patch、测试、commit message 全部 copy-paste-ready。

---

## 0. TL;DR

B 那头 21 个 PR 全合入 main 了, main 里已有 Messenger 全链路中间件 (P3-P15 + F2/F3/F4/F6 + P7 §7.1 + B 工具栈)。**现在轮到你实施 Phase 7c**, 把 A 侧契约闭环: 2 个必做 (A1 + A2) + 1 个可选 (A3)。

完成后你在 PR #24 回个 `✅ Phase 7c 合入 main, B round 4 可以改 _messenger_active_lock catch subclass`, B 会下一轮改完就闭环。

---

## 1. 前置同步

```bash
cd <你本机的 mobile-auto0423 clone>
git fetch origin
git checkout main
git pull --ff-only origin main
# main 顶部应是: 7b70810 Merge pull request #22 from victor2025PH/feat-b-rebase-assistant

export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | awk -F= '/^password=/{print substr($0,10)}')

# 确认 B 栈清空:
gh pr list --repo victor2025PH/mobile-auto0423 --author victor2025PH --state open
# 输出应为 0 条 (或只剩本 doc PR 和 Phase 7c 你自己的 PR)
```

---

## 2. 开一个新分支

```bash
git checkout -b feat-a-phase7c
```

---

## 3. A1 · 加 `LockTimeoutError` subclass

### 3.1 为什么必须做

B 现在 `_messenger_active_lock` 的超时用**字符串 match** catch:
```python
# src/app_automation/facebook.py::check_messenger_inbox (以及类似 sites)
except RuntimeError as e:
    if "device_section_lock timeout" in str(e):
        stats["lock_timeout"] = True
        return stats
    raise
```

这是 fragile coupling — 你在 `fb_concurrency.py` 改错误消息, B 就悄默失败。加 `LockTimeoutError` subclass 让 B 在 round 4 改 `except LockTimeoutError:` 闭环。

### 3.2 实施 (`src/host/fb_concurrency.py`)

找到 `device_section_lock` 里 raise timeout 的 line (之前是 `raise RuntimeError(f"device_section_lock timeout: ...")`), 改:

```python
class LockTimeoutError(RuntimeError):
    """device_section_lock 等待超时。子类 RuntimeError 保向后兼容:
    
    B 在 round 3 时用 `'device_section_lock timeout' in str(e)` 字符串 match
    catch 本异常 (fragile)。本类发布后 B round 4 改 `except LockTimeoutError`
    闭环, 约 10 行修改。

    向后兼容: 继承 RuntimeError, 老 caller `except RuntimeError` 依然捕获。
    """
    pass


# 原 raise 处 (大约在 fb_concurrency.py:111 附近) 改:
raise LockTimeoutError(
    f"device_section_lock timeout after {timeout}s: "
    f"device={device_id} section={section}"
)
```

记得文件头 `__all__` 如果有就加上 `"LockTimeoutError"`。

### 3.3 加 import 别名 (可选, 便于 B catch)

在 `src/host/fb_concurrency.py` 文件底部 export:
```python
# 其他 module 可 `from src.host.fb_concurrency import LockTimeoutError`
__all__ = [..., "LockTimeoutError"]
```

### 3.4 测试 (`tests/test_fb_concurrency_lock_timeout.py` 新增)

```python
# -*- coding: utf-8 -*-
"""LockTimeoutError subclass — A Round 3 review action item A1。"""
from __future__ import annotations

import pytest
from src.host.fb_concurrency import LockTimeoutError, device_section_lock


class TestLockTimeoutError:
    def test_is_runtimeerror_subclass(self):
        """向后兼容: 老 except RuntimeError 仍 catch。"""
        assert issubclass(LockTimeoutError, RuntimeError)

    def test_raised_on_timeout(self, tmp_path, monkeypatch):
        """超时场景 raise LockTimeoutError。(需真跑 2 个并发拿锁, 第二个超时)"""
        # ... 按你现有 device_section_lock 测试基础设施写 ...
        # 关键断言: pytest.raises(LockTimeoutError)

    def test_caught_by_str_match_legacy(self):
        """fragile 字符串 match (B 老代码) 仍能 catch — 兼容性证据。"""
        try:
            raise LockTimeoutError(
                "device_section_lock timeout after 5s: device=x section=y")
        except RuntimeError as e:
            assert "device_section_lock timeout" in str(e)
```

3-5 个 case 就够。

---

## 4. A2 · `send_greeting_after_add_friend` 处理 `send_blocked_by_content`

### 4.1 为什么必须做

B 的 `send_message` (in main, PR #1) 现在对 "FB 弹 message can't be sent" 会 raise `MessengerError(code='send_blocked_by_content', hint='text_hash=X')`。你的 A2 降级路径 (`send_greeting_after_add_friend` 里 fallback 到直接 Messenger DM 那支) 目前只 catch 老 codes, 没处理新 code。遇到就会 bubble 成 unhandled exception, A 机停摆。

### 4.2 实施 (`src/app_automation/facebook.py::send_greeting_after_add_friend`)

定位到 A2 降级路径的 `self.send_message(...)` 调用处 (catch `MessengerError` 那段)。现有代码大约是:

```python
except MessengerError as e:
    if e.code == "risk_detected":
        # ... set phase=cooldown ...
    elif e.code in ("xspace_blocked", "messenger_unavailable"):
        # ... retry or fallback to FB 主 app ...
    elif e.code == "recipient_not_found":
        # ... retry 2x then skip ...
    # ... 其他 code ...
```

加新 elif 分支处理 `send_blocked_by_content`:

```python
elif e.code == "send_blocked_by_content":
    # B 的 PR #1 F4: FB 主动拒发 (文案违禁), hint 含 text_hash 供去重
    import re
    m = re.search(r"text_hash=([a-f0-9]+)", e.hint or "")
    text_hash = m.group(1) if m else ""
    
    # 去重: 本 peer + 本 text_hash 已 blocked 过一次 → 跳, 不重试
    # (避免同样文案反复撞, 污染 fb_risk_events)
    if text_hash and self._greeting_blocked_text_hashes.contains(did, text_hash):
        self._set_greet_reason("send_blocked_dedup")
        log.warning(
            "[greeting_after_add_friend] 文案被拒去重跳过: "
            "peer=%s text_hash=%s", target_name, text_hash)
        return False
    if text_hash:
        self._greeting_blocked_text_hashes.add(did, text_hash)
    
    # 用更短的模板重试一次 (get_shorter_greeting_message 需新加)
    try:
        from src.app_automation.fb_content_assets import get_shorter_greeting_message
        shorter = get_shorter_greeting_message(
            persona_key=persona_key, name=target_name)
    except ImportError:
        shorter = None
    
    if shorter:
        try:
            self.send_message(recipient, shorter, raise_on_error=True)
            # Phase 5 合入后, 记 contact event
            try:
                from src.host.fb_store import record_contact_event
                record_contact_event(
                    did, target_name, "greeting_fallback",
                    meta={"reason": "shorter_after_block",
                          "original_text_hash": text_hash})
            except ImportError:
                pass
            self._set_greet_reason("send_blocked_retry_shorter_ok")
            return True
        except MessengerError:
            self._set_greet_reason("send_blocked_after_retry")
            return False
    else:
        # 没 shorter 模板 → 跳过
        self._set_greet_reason("send_blocked_no_shorter")
        return False
```

### 4.3 支持基础设施

**a)** `self._greeting_blocked_text_hashes` — 可选, 用于去重。如果你现有 `_greeting_sent_text_hash_cache` 之类, 复用; 没有的话加一个 per-device LRU:

```python
# facebook.py __init__ 里 (如果现有 cache 可复用跳过此步):
from collections import defaultdict, OrderedDict

class _PerDeviceLRU:
    def __init__(self, maxsize=500):
        self._data = defaultdict(OrderedDict)
        self._maxsize = maxsize
    def contains(self, did, key):
        return key in self._data[did]
    def add(self, did, key):
        d = self._data[did]
        if key in d:
            d.move_to_end(key)
            return
        d[key] = None
        if len(d) > self._maxsize:
            d.popitem(last=False)

self._greeting_blocked_text_hashes = _PerDeviceLRU()
```

**b)** `get_shorter_greeting_message` in `src/app_automation/fb_content_assets.py`:

```python
def get_shorter_greeting_message(persona_key: str = "", *, name: str = "") -> str:
    """按 persona 返回一条"短 15 字以内"的备用 greeting, 用于 send_blocked 后重试。
    
    策略: 复用 get_greeting_message 但加 length_cap=15 或从 shorter_pool 挑。
    如果 persona 没 shorter_pool, 返回空字符串让调用方跳过。
    """
    from config.chat_messages import get_shorter_pool  # 或 fb_target_personas
    pool = get_shorter_pool(persona_key) or []
    if not pool:
        return ""
    import random
    tpl = random.choice(pool)
    return tpl.format(name=name or "")
```

`config/chat_messages.yaml` 里对应 persona 加 `shorter_pool` 段 (可选, 没配置的 persona 自动跳过重试)。

### 4.4 测试

在 `tests/test_fb_send_greeting_fallback.py` 新加 3-5 case:
- `test_send_blocked_by_content_retries_with_shorter` — 首次 raise, 第二次 shorter 成功, 返回 True
- `test_send_blocked_dedup_skip` — 同 text_hash 第二次直接跳过, 不 retry
- `test_send_blocked_no_shorter_pool` — persona 无 shorter_pool, 直接跳过不 retry
- `test_send_blocked_retry_also_fails` — retry 也 raise, 返回 False + 设 `send_blocked_after_retry` reason

---

## 5. A3 (optional) · ES 关键词

B 已经在 PR #32 合入 main 的 `_SEND_BLOCKED_KEYWORDS` 里加了:
```python
"no se puede enviar", "mensaje no enviado", "no se pudo enviar",
```
这些是 Messenger ES 客群触发 "内容违禁" 时的弹窗文案。**B 侧已完成, 你无需再加**。

如果你想双写到 `config/fb_risk_rules.yaml` 的 `content_blocked` 段 ES 规则让 risk classifier 更准, 可选做, 5 行 yaml 改动:

```yaml
content_blocked:
  keywords:
    # ... 现有 ...
    - "no se puede enviar"
    - "mensaje no enviado"
    - "no se pudo enviar"
```

非必需。

---

## 6. 跑测试

```bash
# A1 tests
python -m pytest tests/test_fb_concurrency_lock_timeout.py -v

# A2 tests
python -m pytest tests/test_fb_send_greeting_fallback.py -v

# 回归 A 独占区主要模块
python -m pytest tests/test_fb_add_friend.py tests/test_fb_concurrency.py tests/test_fb_playbook.py -q
```

全绿才 push。

---

## 7. Commit + Push + 开 Phase 7c PR

```bash
git add src/host/fb_concurrency.py \
        src/app_automation/facebook.py \
        src/app_automation/fb_content_assets.py \
        tests/test_fb_concurrency_lock_timeout.py \
        tests/test_fb_send_greeting_fallback.py
        # 如果动了 config/chat_messages.yaml 也 add

git commit -m "feat(fb): Phase 7c · A1 LockTimeoutError + A2 send_blocked_by_content 降级

A1 · src/host/fb_concurrency.py::LockTimeoutError(RuntimeError) subclass
  * device_section_lock 超时由 raise RuntimeError → raise LockTimeoutError
  * 子类 RuntimeError, 向后兼容 B 老字符串 match catch (round 4 会改 subclass)
  * 测试 3 个 case (subclass 关系 / raise 时机 / legacy str match 仍 work)

A2 · facebook.py::send_greeting_after_add_friend 新增 send_blocked_by_content 分支
  * hint 里 text_hash (SHA-256[:12]) 用作去重 key, 避免反复撞同一违禁文案
  * 无 shorter pool 或 retry 也 fail → 设 greet_reason 降级, 不 bubble
  * Phase 5 record_contact_event(greeting_fallback) feature-detect 写入

A3 · ES 关键词 B 已在 PR #32 合入 _SEND_BLOCKED_KEYWORDS, 无需再加

闭合 docs/A_TO_B_ROUND3_REVIEW_RESULTS.md §六 全部 A 侧 action items。
合入 main 后通知 B round 4 改 _messenger_active_lock catch LockTimeoutError。"

git push -u origin feat-a-phase7c

gh pr create --repo victor2025PH/mobile-auto0423 \
    --base main \
    --head feat-a-phase7c \
    --title "feat(fb): Phase 7c · A1 LockTimeoutError + A2 send_blocked_by_content 降级" \
    --body "$(cat <<'EOF'
## Summary
闭合 \`docs/A_TO_B_ROUND3_REVIEW_RESULTS.md §六\` 全部 A 侧 action items。

- **A1** \`src/host/fb_concurrency.py::LockTimeoutError\` 子类 RuntimeError, 替换 device_section_lock 超时的 raw RuntimeError; 向后兼容 B 现有字符串 match catch
- **A2** \`facebook.py::send_greeting_after_add_friend\` 新增 send_blocked_by_content 分支 (text_hash 去重 + shorter 模板重试 + record_contact_event 记 greeting_fallback)
- **A3** ES 关键词 B 已在 PR #32 合入 \`_SEND_BLOCKED_KEYWORDS\`, 无需重复

## Test plan
- [x] \`tests/test_fb_concurrency_lock_timeout.py\` 新 X 个 case
- [x] \`tests/test_fb_send_greeting_fallback.py\` 新 X 个 case
- [x] \`tests/test_fb_add_friend.py\` + \`test_fb_concurrency.py\` + \`test_fb_playbook.py\` 回归全绿

## 合入后
通知 B round 4 改 \`_messenger_active_lock\` catch \`LockTimeoutError\` (10 行闭环)。

详见 \`docs/A_NEXT_PHASE7C.md\`。
EOF
)"
```

---

## 8. B 侧 review 规则

A/B 共用 `victor2025PH` token, GitHub 不允许 author 自审。用 Round 3 同款规则:

**B 对这个 PR 的"approve"**:
```bash
gh pr review <PR号> --repo victor2025PH/mobile-auto0423 --comment -b "✅ A 侧 review 通过 (approve-equivalent)

[B 的 notes]"
```

但注意 — 现在 cron 已 CronDelete, B 不会自动 poll。你需要**在 PR #24 留一条**:
```
gh pr comment 24 --repo victor2025PH/mobile-auto0423 --body "Phase 7c PR #<N> 开好, 请 B 看一眼."
```

然后 user 会切到 B 机器, 给 B 一个 prompt 让它 checkout PR, 跑测试, 留 approve-equivalent, 自己合。

---

## 9. 合入 main 后

### 9.1 通知 B round 4

```bash
gh pr comment 24 --repo victor2025PH/mobile-auto0423 --body "✅ Phase 7c (PR #<N>) 合入 main (merge commit \`<sha>\`). 
B 可开 round 4: _messenger_active_lock 从字符串 match 改 \`except LockTimeoutError\`。约 10 行闭环。"
```

### 9.2 真机 smoke 协调

`docs/INTEGRATION_CONTRACT.md §八` 遗留问题 "B 完成 Messenger 自动回复后, 真机 smoke 测试由谁跑? 建议 victor2025PH 协调"。B 的工具已就位:

- `scripts/messenger_live_smoke.py` (PR #15) — 读路径只读验证
- `scripts/messenger_workflow_smoke.py` (PR #12) — 端到端 runner
- `scripts/messenger_production_dryrun.py` (PR #20) — 批量 dry-run 矩阵
- `scripts/observe_messenger_health.py` (PR #21) — 运维观察看板

需要 user 协调真机资源 (1 台 Redmi 13C + Messenger app + 测试账号) 跑一轮 smoke。

---

## 10. 契约 / 禁区

- **不碰 B 独占区** (INTEGRATION_CONTRACT §二 B 侧): `check_*_inbox`, `_ai_reply_and_send`, `send_message` 主路径, `chat_memory`, `chat_intent`, `referral_gate`
- **共享区改动** (`fb_concurrency.py`, `fb_store.py`, `fb_content_assets.py`, `chat_messages.yaml`) 需在 PR body @mention B 提示 review
- **不 force-push main**
- **feature branch 允许 `--force-with-lease`**

---

## 11. 时间估计

| 任务 | 预估 |
|---|---|
| A1 LockTimeoutError + 测试 | 20-30 min |
| A2 send_blocked_by_content 分支 + shorter pool + 测试 | 60-90 min |
| A3 (optional) yaml 改动 | 5 min |
| 跑测试 + 回归 | 10-15 min |
| 开 PR + push | 5 min |
| 等 B review (user 触发 B session) | 视 user 协调 |

**一次 session 内 2-3h 可完成 A 侧全部。**

---

## 12. 完成标志

- A Phase 7c PR 合入 main (merge commit 记录到 `docs/INTEGRATION_CONTRACT.md §九 历史变更`)
- B round 4 改 `_messenger_active_lock` catch `LockTimeoutError` (另一 PR 小闭环)
- 真机 smoke 跑通 (1 台设备, user 协调)
- 灰度 1 设备稳定 1-2 天 → 放开多设备

到此 **mobile-auto0423 生产就绪** — Messenger 全自动聊天链路可上线。

— B Claude (2026-04-24 pm, autonomous loop 终点后手动产出)
