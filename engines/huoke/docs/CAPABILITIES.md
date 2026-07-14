# OpenClaw 业务能力矩阵（CAPABILITIES）

> 给市场 / 客户 / 老板看的"我们能干什么"。技术细节看 [`SYSTEM_ARCHITECTURE`](SYSTEM_ARCHITECTURE.md)，运维看 [`SYSTEM_RUNBOOK`](SYSTEM_RUNBOOK.md)。
>
> 维护人：victor / 创建：2026-04-26

---

## §1 — 一句话定位

OpenClaw 是一个**移动端社交平台 RPA 自动化系统**，用真实手机批量做：
- **加粉获客**：搜索目标用户 → 加好友 → 自动打招呼
- **客服回复**：监控收件箱 → AI 自动回复 → 引流
- **群体提取**：进入指定群组 → 提取成员 → 转化

支持平台：Facebook / Messenger / TikTok / Instagram / Twitter / LinkedIn / Telegram。

---

## §2 — 能力矩阵

| 平台 | 能力 | 状态 | 模块 | KPI 关注 |
|------|------|------|------|---------|
| **Facebook** | 加好友（带 personalized note） | ✅ 上线 | A / `add_friend_with_note` | 加粉成功率 / 当日加粉数 |
| **Facebook** | 加好友后自动打招呼 | ✅ 上线 | A / `send_greeting_after_add_friend` | 打招呼成功率 |
| **Facebook** | 群成员批量提取 | ✅ 上线 | A / `extract_group_members` | 单群提取量 |
| **Facebook** | 信息流浏览（养号） | ✅ 上线 | A / `browse_feed` | 浏览时长 / 互动数 |
| **Messenger** | 收件箱监控 + AI 自动回复 | ✅ 上线 | B / `check_messenger_inbox` + `_ai_reply_and_send` | 回复响应时间 / 回复率 |
| **Messenger** | Message requests（陌生人请求）处理 | ✅ 上线 | B / `check_message_requests` | 通过率 |
| **Messenger** | 主动发送消息 | ✅ 上线 | B / `send_message` | 送达率 |
| **客户画像** | L1 本地 / L2 中央双层画像 | 🚧 in-progress | central_customer_store | 客户量 / 标签覆盖 |
| **A/B 实验** | 自动毕业 winner 复制到其他设备 | ✅ 上线（Phase 7+） | ab_auto_graduate | 实验数 / 毕业率 |
| **客服转人** | SLA 报警 + 人类客服接管 | ✅ 上线（Phase 8） | handoff_sla | 超时数 / 接管率 |
| **智能引流** | readiness 维度 + SLA priority 分层 | ✅ 上线（Phase 9） | referral_gate | 引流量 / 转化率 |
| **TikTok** | 内容发布 / 互动 / 通讯录获客 | ✅ 部分（Phase 6+） | tiktok platform actions | TODO |
| **多平台编排** | WorkflowEngine 跨平台业务流 | ✅ 上线 | platform_actions_bridge | 21 个 action |
| **VPN 池调度** | 多 IP 池子轮换 + 出口 IP 健康检查 | ✅ 上线 | proxy_health + vpn_manager | 出口 IP 可用率 |
| **设备集群** | 多机分布式 / Worker 主从 | ✅ 上线 | multi_host | 在线设备数 / 心跳健康 |
| **风控** | 平台风控事件捕获 + 冷却 | ✅ 上线 | facebook_risk + tiktok_escalation | 风控触发数 |

---

## §3 — KPI 看板

> 仪表盘地址：**http://localhost:8000/dashboard**

### 3.1 实时指标

| 指标 | 端点 | 说明 |
|------|------|------|
| 设备在线数 | `GET /devices` | `connected` 状态计数 |
| 当日任务量 | dashboard / `GET /tasks?date=today` | 按 task_type 分组 |
| FB 漏斗 | `GET /facebook/funnel` | 搜索 → 加好友 → 打招呼 → 回复 |
| 风控状态 | `GET /facebook/risk/status` | 当前活跃风控事件 |
| 客服 SLA | dashboard 客服页面 | 待处理 / 超时数 |

### 3.2 周报

```bash
python scripts/phase8_funnel_report.py
```
> TODO: 包装成 `scripts/ops/weekly_report.bat`，市场可一键跑

### 3.3 关键 KPI（待填）

> 这一节是 **TODO**，需要 victor 或市场填实际数据。

| KPI | 当前值 | 目标值 | 趋势 |
|-----|--------|--------|------|
| 单台手机日加粉数 | TODO | TODO | TODO |
| Messenger 回复中位响应时间 | TODO | TODO | TODO |
| 加粉成功率（请求/通过） | TODO | TODO | TODO |
| 引流转化率（chat → 转人 → 成单） | TODO | TODO | TODO |
| 风控触发数 / 周 | TODO | TODO | TODO |
| 设备在线率 | TODO | TODO | TODO |

---

## §4 — 当前覆盖业务场景

### 4.1 主战场（已稳定）

- **日本女性中年群体获客**（默认 persona = `jp_female_midlife`）
- 流程：FB 搜索/群提取 → AI persona 分类 → 加好友 → 打招呼 → Messenger 进入 AI 客服回复

### 4.2 实验中

- A/B 实验自动毕业（Phase 7+）
- 客服 LLM 客户洞察（Phase 8）
- 智能引流 readiness（Phase 9）

### 4.3 计划中（未上线）

> 见仓库根目录 `P*-PLAN.md` / `docs/P*-*.md`（多数已部分实施，参考价值大于规划价值）

---

## §5 — 对外可讲的"卖点"

| 卖点 | 简述 | 对应实现 |
|------|------|---------|
| **真机 RPA 不被风控** | 不走 API、不走 webview，纯 UI 操作（adb + uiautomator2 + VLM 兜底） | facebook.py 全套 |
| **多平台一套系统** | FB / Messenger / TikTok / IG / TW / LI / Telegram 同代码栈 | platform_actions_bridge 21 个 action |
| **多机分布式** | 1 主控 + N worker 横向扩展，单主控可管 200+ 设备 | multi_host + heartbeat |
| **AI 智能回复** | LLM 接入，按 persona/客户画像生成定制回复 | _ai_reply_and_send + chat_messages.yaml |
| **客服 SLA 转人** | 自动检测难处理对话，超时报警 + 转人接管 | handoff_sla |
| **A/B 实验自动化** | 多套话术 / 加好友 note 跑 A/B，自动选优 | ab_auto_graduate |
| **L2 中央客户画像** | 多 worker 客户数据统一沉淀到中央 PG | central_customer_store（in-progress） |

---

## §6 — 不能做 / 有边界的事（防客户期望管理）

- ❌ **不能**调用平台 API（一律真机 UI 自动化，速度受手机/网络/平台 UI 限制）
- ❌ **不能**24/7 全速 — 每个 task 有冷却（FB 加好友最少分钟级、Messenger 收件箱 15min）
- ❌ **不能**完全无人值守 — `task_execution_policy: manual_only=True` 是当前默认，自动调度需开关 + 监控
- ❌ **设备 unauthorized 时无法控制** — 需现场点 USB 调试授权
- ❌ **手机断网 / 黑屏 / VPN 异常时**该设备临时停摆，watchdog 会标 disconnected

---

## §7 — 客户问什么用什么数据回答（cheatsheet）

| 客户问 | 你看哪 | 怎么答 |
|--------|--------|--------|
| "你们一天能加多少粉？" | dashboard / phase8_funnel_report | 按设备数 × 单设备配额（看 facebook_playbook.yaml） |
| "回复客户多快？" | `GET /facebook/funnel` 中的 inbox 段 | 中位响应时间 |
| "我们今天获客多少？" | dashboard 漏斗页 / `GET /facebook/funnel` | 按 funnel stage 分级数 |
| "系统稳吗 / 出过事吗？" | `logs/openclaw.log` 的 ERROR 数量 / `service_wrapper.log` 重启次数 | 用 `status.bat` 输出截图 |
| "支持哪些平台？" | 本文件 §2 | 直接发本文件 |
| "能不能加 X 平台 / X 功能？" | 本文件 §6 | 看是不是边界外，是的话明说不能 |
| "出问题怎么办？" | [`SYSTEM_RUNBOOK §3`](SYSTEM_RUNBOOK.md#3--应急恢复-sop) | 故障字典 |

---

> 本文件由市场和开发**共同维护**：能力新增/下线由开发改 §2，KPI 数据/客户对话 cheatsheet 由市场填 §3 §7。
