# Phase 5 · Lead Mesh (Lead Dossier + Agent Mesh + Handoff + Webhook)

> 2026-04-23。A 机 (feat-a-phase5-mesh 分支)。依赖 Phase 4 多渠道引流已就位。

## 一、本轮交付的 8 张新表 + 8 个 Python 模块 + 22 个 API 端点

### 表 (database.py)

| 表 | 作用 |
|----|------|
| `leads_canonical` | 跨平台统一 lead 抽象 (UUID 主键) |
| `lead_identities` | 平台身份映射 (fb profile / line id / phone / ...) |
| `lead_journey` | **append-only** 事件流, 所有 agent/人动作 |
| `lead_handoffs` | 引流交接状态机 |
| `agent_messages` | Agent 间消息队列 (SQLite + HTTP) |
| `lead_locks` | 软锁 + TTL, 防多 agent 并发 |
| `lead_merges` | 合并审计日志 (支持撤销) |
| `webhook_dispatches` | 外发 webhook 队列 + 重试 + 死信 |

### 模块 (src/host/lead_mesh/)

```
lead_mesh/
├── canonical.py          resolve_identity / auto_merge / manual_merge / revert
├── journey.py            append_journey / get_journey / count_actions / last_action
├── dossier.py            get_dossier / search_leads (聚合视图)
├── lock_manager.py       acquire_lock / release_lock / is_locked (TTL 软锁)
├── handoff.py            create/ack/complete/reject + 脱敏 + 去重查询
├── agent_mesh.py         send_message / poll / query_sync / reply_to
└── webhook_dispatcher.py enqueue / flush / dead_letter / retry
```

### API (22 endpoints, `/lead-mesh/*`)

全部可被人 (curl) 或 AI Agent 直接调:

```
# Leads
GET  /lead-mesh/leads/{cid}                        # 完整 dossier
GET  /lead-mesh/leads/search?name_like=...
GET  /lead-mesh/leads/{cid}/journey
POST /lead-mesh/leads/resolve                      # identity → canonical_id
GET  /lead-mesh/leads/{cid}/merge-candidates
POST /lead-mesh/leads/merge                        # 手动合并
POST /lead-mesh/leads/merges/{id}/revert

# Handoffs
GET  /lead-mesh/handoffs/check-duplicate           # ⚠ B 发引流前调
GET  /lead-mesh/handoffs
POST /lead-mesh/handoffs
GET  /lead-mesh/handoffs/{id}
POST /lead-mesh/handoffs/{id}/acknowledge
POST /lead-mesh/handoffs/{id}/complete
POST /lead-mesh/handoffs/{id}/reject

# Agent Mesh
POST /lead-mesh/agents/messages                    # send
GET  /lead-mesh/agents/messages?to_agent=...       # poll
POST /lead-mesh/agents/messages/{id}/deliver
POST /lead-mesh/agents/messages/{id}/ack
POST /lead-mesh/agents/query-sync                  # 阻塞 request-response

# Webhooks
POST /lead-mesh/webhooks/flush                     # 触发发送
GET  /lead-mesh/webhooks/dead-letters
POST /lead-mesh/webhooks/{id}/retry                # 死信重置
```

## 二、B 机集成指南

### 2.1 B 的 `_ai_reply_and_send` 引流前置流程

**强制要求**: B 发引流话术前必须过去重检查 + 锁。

```python
from src.host.lead_mesh import (
    resolve_identity, acquire_lock, create_handoff,
    append_journey,
)
from src.host.lead_mesh.handoff import check_duplicate_handoff

def _send_referral_with_mesh(self, *, did: str, peer_name: str,
                              peer_profile_url: str, persona_key: str,
                              channel: str, account_value: str,
                              snippet: str, recent_msgs: list):
    # 1) 拿 canonical_id (自动创建/合并)
    cid = resolve_identity(
        platform="facebook",
        account_id=peer_profile_url or f"fb:{peer_name}",
        display_name=peer_name,
        discovered_via="inbox",
        discovered_by_device=did,
        language="ja",
        persona_key=persona_key,
    )

    # 2) 去重 check
    dup = check_duplicate_handoff(cid, channel, since_days=30)
    if dup:
        log.info("[referral] duplicate: handoff=%s already exists", dup["handoff_id"])
        append_journey(cid, actor="agent_b", action="referral_blocked",
                       actor_device=did, platform=channel,
                       data={"reason": "duplicate_handoff", "existing": dup["handoff_id"]})
        return None, "skip_duplicate"

    # 3) 拿锁(防并发)
    with acquire_lock(cid, "referring", by=f"agent_b:{did}", ttl_sec=120) as ok:
        if not ok:
            return None, "skip_locked"

        # 4) 实际发送 UI 操作 (原有代码)
        self._send_snippet_to_ui(snippet)

        # 5) 成功后 append journey + 创建 handoff (后者自动 enqueue webhook)
        append_journey(cid, actor="agent_b", action="referral_sent",
                       actor_device=did, platform=channel,
                       data={"channel": channel, "account_masked": account_value[:4] + "***"})

        handoff_id = create_handoff(
            canonical_id=cid,
            source_agent="agent_b",
            source_device=did,
            channel=channel,
            receiver_account_key=f"{channel}_jp_01",  # 按设备+渠道派生
            conversation_snapshot=recent_msgs[-20:],
            snippet_sent=snippet,
            enqueue_webhook=True,
        )

    return snippet, f"wa_referral:{handoff_id[:8]}"
```

### 2.2 B 需要补的一个数据源: `peer_profile_url`

A 端 `extract_group_members` 时应该存 profile_url (B 读), B 端 Messenger 聊天页点 "View profile" 也能抓。这是**两边都做** (互为备份) 的共享区改动, 需要 A 先在 `lead_identities` 里存, B 读。

---

## 三、Agent Mesh 通信示例

### 3.1 A 告诉 B "我给 X 发了 greeting"

```python
from src.host.lead_mesh import send_message
send_message(
    from_agent="agent_a",
    to_agent="agent_b",
    canonical_id=cid,
    message_type="notification",
    payload={
        "event": "greeting_sent",
        "template_id": "yaml:jp:3",
        "sent_at": iso_now,
    },
)
```

### 3.2 B 从 A 拉 "这个 lead 的背景" (同步 query)

```python
from src.host.lead_mesh import query_sync
reply = query_sync(
    from_agent="agent_b",
    to_agent="agent_a",
    payload={"type": "get_lead_summary", "canonical_id": cid},
    timeout_sec=30,
)
# A 侧需要:
#   while True:
#       msgs = poll_messages("agent_a", message_type="query")
#       for m in msgs:
#           if m["payload"]["type"] == "get_lead_summary":
#               summary = build_summary(m["payload"]["canonical_id"])
#               reply_to(m, from_agent="agent_a", payload=summary)
#               mark_acknowledged(m["id"])
```

### 3.3 HTTP 通道 (curl 也能调, 人工 debug 方便)

```bash
# A 发消息给 B
curl -X POST http://localhost:18080/lead-mesh/agents/messages \
  -H 'Content-Type: application/json' \
  -d '{"from_agent":"agent_a","to_agent":"agent_b",
       "message_type":"notification",
       "canonical_id":"xxx","payload":{"event":"greeting_sent"}}'

# B 拉取消息
curl "http://localhost:18080/lead-mesh/agents/messages?to_agent=agent_b"
```

---

## 四、高置信度自动合并 (决策 2)

### 触发条件

置信度 = Σ(维度得分):

| 维度 | 得分 | 说明 |
|------|------|------|
| `primary_name` 精确一致 | 0.35 | 去空格 emoji 后字符全同 |
| `primary_name` 规范化一致 | 0.25 | 小写 + 去符号后同 |
| `avatar_hash` 一致 | 0.40 | 最强信号 |
| 电话后 4 位一致 | 0.20 | 要求 phone 长度 ≥4 |
| `bio_hash` 一致 | 0.15 | 自我介绍 hash |

**阈值 `AUTO_MERGE_THRESHOLD = 0.85`**: 达到则自动合并。

### 审计 + 撤销

每次自动合并写 `lead_merges` 行 (`merge_mode='auto_soft_identity'`, 含 confidence + reasons)。Dashboard `POST /lead-mesh/leads/merges/{id}/revert` 可撤销。

### Journey 影响

合并后新 identity 挂到目标 canonical_id, journey 事件仍挂原 canonical_id 但 `get_dossier` 聚合时跟 `merged_into` 链路递归合并显示。

---

## 五、Webhook 外发 (决策 3)

### 配置 (`config/webhook_targets.yaml`, **未入库, 需运营创建**)

```yaml
subscribers:
  handoff.created:
    - url: "https://ops-company/webhook/handoff"
      secret_key_env: "WEBHOOK_SECRET_OPS"
      enabled: true
    - url: "https://slack.xxx/services/..."
      secret_key_env: "WEBHOOK_SECRET_SLACK"
      enabled: true

  handoff.completed:
    - url: "https://crm.company/webhook/lead-accepted"
      secret_key_env: "WEBHOOK_SECRET_CRM"

  "*":   # 匹配所有事件 (审计日志/总后台)
    - url: "https://audit.company/webhook/all"
      secret_key_env: "WEBHOOK_SECRET_AUDIT"

retry_schedule_sec: [60, 300, 1800]  # 第 1/2/3 次重试间隔
max_attempts: 3
timeout_sec: 10
```

### 事件枚举

| event_type | 触发 |
|-----------|------|
| `handoff.created` | 新交接单产生 |
| `handoff.acknowledged` | 接收方已看到 |
| `handoff.completed` | 已接上对话 |
| `handoff.rejected` | 对方拒接 |
| `handoff.expired` | 72h 未 ack 自动过期 |
| `lead.merged` | 发生合并(auto 或 manual) |

### HMAC 签名

Webhook POST body 签名方式:

```
X-OpenClaw-Signature: sha256=<HMAC-SHA256(secret, body)>
X-OpenClaw-Event: handoff.created
X-OpenClaw-Dispatch-Id: 42
X-OpenClaw-Timestamp: 2026-04-23T15:00:00Z
```

接收方应校验签名, 拒绝无效请求。

### 死信 + 重试

- 前 3 次失败按 60s / 5min / 30min 指数退避
- 3 次后进 `status=dead_letter`, Dashboard `POST /webhooks/{id}/retry` 手动重置

---

## 六、遗留 / 下一阶段

### 本机可独立继续做 (Phase 5.5)

- [ ] Dashboard **Lead 时间轴**视图 (journey 可视化)
- [ ] Dashboard **接收方工作台**(按 receiver_account_key 分组的待处理队列)
- [ ] Webhook flush 定时任务 (每 30s 调一次 `flush_pending_webhooks`)
- [ ] Expire 定时任务 (每 6h 调一次 `expire_pending_handoffs`)
- [ ] Journey old records 归档 (30 天后压缩/清)

### 需要 B 配合

- [ ] B 在 `_ai_reply_and_send` 里按 §2.1 示例接入 Lead Mesh
- [ ] B 端提取 `peer_profile_url` 作为 identity 主键
- [ ] B 回写 `greeting_replied` journey 事件 (Phase 3 契约已定)

### 未来 (多机扩展)

- SQLite 消息队列 → 真·分布式消息 (Redis pub-sub / NATS)
- 身份软匹配加入图神经网络 (识别更隐蔽的关联)
- Lead Dossier 聚合支持全文搜索 (Elasticsearch)
- 接收方 Dashboard 支持多租户 (每个 receiver 只看自己 handoffs)

---

## 七、测试与验证

- 30 个单元+集成测试覆盖全部核心路径 (`tests/test_lead_mesh.py`)
- HTTP API TestClient 走通 resolve / handoff / check-duplicate / agents
- 全量 pytest 非真机套件: 通过

## 八、性能预期

- SQLite 单机 2-10 QPS 完全够两个 Claude 通信 (实际远低于)
- 锁 TTL 180s, 正常操作 30-60s 够用
- Webhook 异步发, 不阻塞业务流
