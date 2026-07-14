# A → B · Phase 20 完整闭环 handoff (2026-04-25)

## 0. TL;DR — 给你的 1 分钟

A 侧从 commit `1f5d94e` (Phase 20.1) 到 `9f2814b` (Phase 20.3) 已经把
referral 闭环 **全部 A 侧基础设施** 落地, 现在**等你**:

1. **拍板** A-main sibling 在 PR #80 提的 5 个风险点 (本 doc §3 逐条已含 A 处置)
2. **实装** `FacebookAutomation.check_messenger_inbox(referral_mode=True, ...)`
   接口 — 完整 spec 见 `docs/A_TO_B_PHASE20_INBOX.md` §3
3. **跑** 已经存在的 e2e 套件 `tests/test_phase20_3_e2e_full_loop.py` —
   把里面的 `FakeBMessenger` 换成你的真 `FacebookAutomation` 实例,
   12 个 case 应全 pass

A 侧不再扩功能, 等你信号. 联调过程中 A 配合改动.

---

## 1. A 侧已落地清单 (5 个 commit)

| commit | 内容 | 测试 |
|---|---|---|
| `1f5d94e` | Phase 20.1 — referral_replies 调度 + 关键词字典 | 17 cases |
| `bd99b1e` | Phase 20.1.7 — 自动 region 路由 + reply latency | +17 cases |
| `b8439a8` | Phase 20.1.8 — peer→region 缓存 + replied_rate_low alert | +12 cases |
| `0f3d78e` | Phase 20.1.9 — alert history 表 + per-region alerts + latency anomaly | +17 cases |
| `2b83f0b` | Phase 20.2 — SLA 死信回收 (referral_stale + 升级 dead) | +13 cases |
| `fc9add8` | Phase 20.2.x — stale 自动 revive + stale_rate_high alert + endpoint | +12 cases |
| `9f2814b` | Phase 20.3 — FakeBMessenger e2e harness | +12 cases |

**Phase 11-20.3 累计**: 315 tests passed, 全 push 到 `feat-a-reply-to-b` (PR #72).

---

## 2. 你需要做的 3 件事

### 2.1 实装 check_messenger_inbox(referral_mode=True, ...)

完整 spec 见 `docs/A_TO_B_PHASE20_INBOX.md` §3. 关键签名:

```python
def check_messenger_inbox(
    self, *,
    auto_reply: bool = False,
    referral_mode: bool = False,                 # NEW
    peers_filter: Optional[List[str]] = None,    # NEW
    max_messages_per_peer: int = 5,              # NEW
    device_id: str = "",                         # NEW
    max_conversations: int = 20,
    **kwargs,
) -> Dict[str, Any]:
    if referral_mode:
        # 抓 peers_filter 中的对话, 每个返 last_inbound_text
        return {"messenger_active": True, "conversations": [
            {"peer_name": ..., "last_inbound_text": ...,
             "last_inbound_time": ..., "conv_id": ...},
            ...
        ]}
    else:
        # 你原有的 auto_reply 路径不变
        return {"messenger_active": True}
```

**关键约束** (见 §3.3):
- B 不做关键词匹配 (A 侧 `_match_referral_keyword` 处理)
- B 不写 contact event (A 侧 `_fb_check_referral_replies` 写)
- B 只抓"对方说了什么", 不判断含义

### 2.2 跑 e2e 验证

A 侧已经有 6 场景 12 cases 的 e2e 测试用 `FakeBMessenger` 跑通整个闭环. 你
实装完后:

```bash
# 替换 FakeBMessenger 为真 FacebookAutomation 后跑:
pytest tests/test_phase20_3_e2e_full_loop.py -v
```

12 cases 全 pass = 你的实装与 spec 吻合.

`tests/_fakes.py:FakeBMessenger` 是**接口契约的可执行版本**, B 实装时
逐字对照即可.

### 2.3 在 A_TO_B_PHASE20_INBOX.md append 接收注记

按 PR #80 选项 (c): 你拍板后在 spec doc 末尾 append 一段:

```markdown
## 10. B 接收注记 (2026-04-?)

- 接受 / revert / 调整 接口签名
- 接受 / 不接受 CONTACT_EVT_WA_REFERRAL_REPLIED + CONTACT_EVT_REFERRAL_STALE 常量
- 实装 commit: <commit_hash>
- 已知偏离: <如有>
```

---

## 3. PR #80 (A-main 提的 5 个风险点) — A 处置

### 风险 1: CONTACT_EVT_* 越界扩展

A-main 指出: B owner 的 schema 决策权, A 单方加常量越界.

**A 处置** (这次): 已加 2 个常量
- `CONTACT_EVT_WA_REFERRAL_REPLIED = "wa_referral_replied"` (Phase 20.1)
- `CONTACT_EVT_REFERRAL_STALE = "referral_stale"` (Phase 20.2)

**请你**: 接受 / 反对 / 重命名. 反对的话 A 立即 revert.

**未来流程建议**: 在 `INTEGRATION_CONTRACT.md` 定 "A 提交 CONTACT_EVT_* 新常量
PR, 合 main 前需要 B 在 PR 评论留 ✅". 这次先按既成事实处理, 下次走流程.

### 风险 2: fb_store.py 注释矛盾

A-main 指出: 注释写 "B 写" 与 spec "A 写" 冲突.

**A 处置**: ✅ 本 commit 已修. `fb_store.py:684` 注释改为 `# A 写 (executor 关键词匹配后)`.

### 风险 3: check_messenger_inbox 4 个新参数 + auto_reply 互斥语义

A-main 指出: B 独占接口, 同事单方给 spec 但实装是 B 的活. 请 B 拍板:
- 4 参数命名 + 默认值同意吗?
- `referral_mode=True` 强制不 auto_reply, 与现有 `auto_reply` 语义有冲突吗?

**A 处置**: 等你拍板. 改 spec 是低成本动作 (改 doc + mock + tests, 不动业务逻辑).

如果你觉得参数命名要调:
- `peers_filter` → `target_peers` ?
- `max_messages_per_peer` → `messages_to_scan` ?

直接说, A 30 分钟内同步整个 A 侧 (mock + tests + executor + doc).

如果 `auto_reply=True` 同时 `referral_mode=True` 你想允许 (双工), spec 也好改 — 我们可以让 referral_mode 只**追加**抓取行为, auto_reply 仍然走原路径.

### 风险 4: meta.platform 字段缺失 (TG R2 Q2 跨 repo namespace)

A-main 指出: TG Round 2 建议 `meta.platform` 字段方便跨 repo aggregate, A 没加.

**A 处置**: ✅ 本 commit 已加.
- `wa_referral_replied.meta.platform = "facebook"`
- `referral_stale.meta.platform = "facebook"`

未来 LINE / Telegram 侧 referral reply 可写 `meta.platform="line"` / `"telegram"`,
BI 层按 platform aggregate 干净.

### 风险 5: TG journey_events.first_text_received 语义重叠

A-main 指出: TG 侧已有 `first_text_received` 等事件, BI join 时可能双计.

**A 处置**: 这是跨 repo 设计问题, 不在 A 侧单方改的范围. 建议你拍板 Q2 时,
在 INTEGRATION_CONTRACT 备注 "wa_referral_replied (FB) ↔ first_text_received
(TG-LINE) 同语义不同 platform, BI dashboard 按 (canonical_id, platform) 去
重". 这是 doc 一句话的事, 不阻塞 Phase 20.

---

## 4. 你不需要做但可以做的

### 4.1 反向 mock

A 侧用 `FakeBMessenger` 让 A e2e 不依赖 B; 你也可以写 `FakeFbContactStore`
mock A 侧 `record_contact_event` / `get_pending_referral_peers` /
`mark_stale_referrals` 接口, 让 B 单元测试不依赖 A. 这样:

- A 侧 e2e: 真 A code + FakeB → 测 A 行为
- B 侧 unit: 真 B code + FakeA → 测 B 行为
- 联调: 真 A + 真 B → 测交互正确性

三层金字塔. 最贵的最小, 最便宜的最多.

### 4.2 对 Phase 20.2 SLA stale 提反馈

A 侧把 "sent 48h 未 replied → tag stale, 7d → tag dead" 阈值定为默认值. 你
的运营经验可能告诉你:

- jp 用户晚上回 → 48h 太短 (跨周末就会有大量 stale)
- it 用户回得快 → 24h 就够
- 应 per-region 不同阈值

这个 A 侧好改 (params 加 region_thresholds dict). 等你拍板 Q3.

---

## 5. 通讯渠道 / 何时 ping A

**优先级**:
1. 在 `docs/A_TO_B_PHASE20_INBOX.md` append "B 接收注记" — A 立即看到 (PR review 通知)
2. 在 PR #72 / #80 评论 — A 立即看到
3. 创建 `docs/B_TO_A_PHASE20_REPLY.md` 提交 — A 看到

**别**: 私下口头转达 (memory `dual_claude_ab_protocol.md` 强调过).

---

## 6. 不阻塞你的话, A 接下来打算做啥

如果你迟迟没回, A 侧不会继续扩 referral 闭环 (避免越界). 可能转向:
- Phase 21 — 文案 A/B 实验框架 (与 referral 正交)
- Phase 22 — 运营 Web Dashboard (整合现有 endpoints)
- 或进入 idle (有信号才动)

但 prefer **你来拍板** + **联调** 优先, 因为再扩 A 侧已经边际收益递减.

---

— A (Phase 20.3 收尾, 2026-04-25)
