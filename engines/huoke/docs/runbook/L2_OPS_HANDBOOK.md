# L2 中央花名册 — 运营手册

> 受众: 真人客服坐席 + 运营经理
>
> 业务定位: 日本 37-60 岁女性情感陪护 → 引流加 LINE → 真人接管 → 真人聊到成交

---

## 一、客服日常工作流

### 1. 登录后台

打开 **http://192.168.0.118:8000/dashboard** 在浏览器, 用预先分配的客服账号登录:

| username | 角色 | 默认密码 |
|---|---|---|
| `agent_001` | customer_service | `agent001@openclaw` |
| `agent_002` | customer_service | `agent002@openclaw` |
| `agent_003` | customer_service | `agent003@openclaw` |
| `supervisor` | supervisor | `super@openclaw` |
| `admin` | admin | (运营经理掌握) |

⚠️ **首次登录后立即改密码** (后台个人设置页).

### 2. 查看待接管队列

主菜单 → "Lead Mesh" → 点 **🤝 接收方工作台**, 看 "pending" tab. 这里列的是机器人在 messenger 里聊到合适时机, 已经发出引流话术等真人接管的客户.

### 3. 决定接不接

每个客户卡片显示:
- 👤 **客户姓名** + persona (jp_female_midlife = 日本中年女性)
- 💬 **聊天历史** (展开 details, 已脱敏: 电话/邮箱/LINE-ID 自动 mask)
- 📌 **AI 摘要** ai_summary (例如 "聊了 7 轮, 客户主动要 LINE")
- ⏳ **等待时长**

**接管策略**:
- AI 摘要含 "客户主动要 LINE" / "高情感分" → **优先接** (高意向)
- 等待 > 30 分钟 → **抢着接** (客户失去耐心前)
- 老主管接管中 (蓝色 "已被 X 接管") → **跳过**

### 4. 客服 4 个动作

每个 pending/acknowledged 卡片下半部有真人客服动作区:

| 按钮 | 用途 | 弹窗输入 |
|---|---|---|
| **🙋 我接手** | 抢锁: 把客户绑定到自己; **同时暂停 worker AI 自动回**, 客户下次发消息 worker 不会自动回 | 客户姓名 + 设备 ID (从聊天卡片可看到, 留空则不暂停 AI — 不推荐) |
| **💬 回复** | 输入要发给客户的话; PR-6.6 之后会通过 agent_mesh → worker → 物理手机真发出 | 文本 (任何字符, 含 emoji) |
| **📝 备注** | 加内部备注 (不发给客户), 给同事看 | 备注文字 |
| **🏁 标记结果** | 完结此客户. converted/lost 终态会自动释放 AI 接管 | 1=成交, 2=流失, 3=待跟进; 备注 |

⚠️ 客户聊得太晚不接? **30 分钟无操作**系统会自动归还队列 (PR-6.6 暂未实现, 后续加).

---

## 二、L3 看板 — 运营经理日报

打开 **http://192.168.0.118:8000/static/l2-dashboard.html** (移动端也能看).

### 6 个核心指标

| 指标 | 含义 | 健康范围 |
|---|---|---|
| **客户总数** | PG `customers` 表总条数 | 累计涨 |
| **in_messenger 中** | 还在跟机器人聊的客户 | 看波动, 不掉到 0 |
| **in_line (已引)** | 引流话术发出, 还没真人接管 | **不超过队列容量, 防积压** |
| **accepted_by_human** | 真人正在跟进的 | 看是否对得上"客服在线人数 × 单人产能" |
| **已转化** | converted 终态 | 累计涨, 转化率 = converted / 客户总数 |
| **流失** | lost | 关注比例 |

### 待接管队列

主电脑这一面看的是 `customer_handoffs` PG 表 (跨 worker 全局视角). 跟 lead_mesh `lead_handoffs` SQLite 是双轨 (双写策略, 长期 v2 合并).

### 7 天事件统计

例:
```
greeting_sent: 320 件
message_received: 580 件
messenger_message_sent: 460 件
wa_referral_sent: 45 件
referral_rejected: 12 件
```

健康指标:
- `greeting_sent` → `message_received` ≈ 60% 回复率
- `message_received` 多于 `messenger_message_sent` → 聊不完, 客服产能不够
- `referral_rejected` / `wa_referral_sent` < 30% → 引流时机判断准

### 推送队列状态

实时看主控 push metrics + drain 后台线程状态:
- `queue_pending` 持续涨 → 主控 PG 写入压力问题, 或 worker 连主控有延迟
- `dead_letter_pending > 0` → 有客户数据彻底失败, **必须人工查 SQL**
- `drain.running == false` → drain 后台线程死了, 必须重启主控

---

## 三、运营经理 — 调引流策略

如果发现:
- 客户被引流后流失率高 → 调 `config/referral_strategies.yaml.jp_female_midlife.min_turns` (现 7) 改 8 或 10 (聊更久)
- 客户聊久不引 → 加新关键词到 `config/referral_trigger_keywords.yaml.ja`
- 客户拒绝多 → 加新拒绝词到 `config/referral_trigger_keywords.yaml.rejection_ja`
- AI 话术风格不对 → 改 `src/ai/chat_brain.py:BOT_PERSONA_IDENTITIES.jp_caring_male`

改完不需要重启服务 (referral_gate yaml hot-reload).
chat_brain 改后下次入站消息 LLM prompt 自动用新身份块.

---

## 四、突发情况

### A. AI 在跟客户聊"风控关键词" (色情/政治/赌博)

立即在 SQL 加该客户 `referral_rejected_at` 触发冷却:
```sql
UPDATE customers SET ai_profile = ai_profile || '{"referral_rejected_at": "2026-04-26T12:00:00Z"}'::jsonb WHERE primary_name = 'Xxx';
```

后续 7 天该客户被 referral_gate 完全 block.

### B. 客户投诉

1. 后台搜该 username (有 lead_journey 全部 actor 历史)
2. 看是哪个 worker / device 操作的
3. 该 device 对应的物理手机暂停业务: 调 `POST /tasks/dispatch?type=adb_pause_device device_id=xxx`
4. 给上级 escalate.

### C. 主控 PG 满了

```bash
# 看 PG 占用
psql -h 127.0.0.1 -U openclaw_app -d openclaw -c "SELECT pg_size_pretty(pg_database_size('openclaw'));"
# 6 个月以前的 customer_chats 归档:
psql ... -c "DELETE FROM customer_chats WHERE ts < NOW() - INTERVAL '6 months';"
```

⚠️ 不要碰 customers / customer_handoffs (业务中数据).

### D. drain 死信表非空

```sql
SELECT id, path, attempts, last_error, moved_at FROM push_dead_letter ORDER BY moved_at DESC LIMIT 20;
```

如果死信都是同一种 error (主控某个端点 bug), 修了 bug 后:
```sql
INSERT INTO push_queue (path, body, enqueued_at, next_retry_at)
SELECT path, body, enqueued_at, 0 FROM push_dead_letter;
DELETE FROM push_dead_letter;
```

让 drain 线程重发.

---

## 五、PR 进度参考

| PR | 内容 | 状态 |
|---|---|---|
| #88 | L2 schema/store/SDK | ✅ MERGED |
| #89-95 (super-PR) | greeting/messenger/drain/触发器/情感/真人后端 | ✅ MERGED 一次性 |
| (本 PR-6.6) | worker listener 真发消息 + drain 启动钩子 + 客服账号 + L3 看板 + 运营手册 | 进行中 |

---

## 六、下一阶段路线图

| 优先级 | 内容 | 何时做 |
|---|---|---|
| P0 | 真业务跑一轮 (W03 真发 1 个加好友请求) | victor 拍板谁 |
| P1 | worker .env 同步 API_KEY 启用安全模式 | 内网兼容期通过后 |
| P1 | 30 分钟无操作自动归还接管队列 | 客服上量后 |
| P2 | L3 dashboard 升级真人 SLA 看板 | 数据攒够 |
| P2 | A/B 实验框架 (不同 persona 转化率对比) | 业务定成不成 |
