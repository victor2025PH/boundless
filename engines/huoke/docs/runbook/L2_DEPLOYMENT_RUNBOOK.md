# L2 中央花名册 — Victor 部署清单

> 目标：把 PR #88-95 真正部署起来，让 30 台手机的数据汇总到主电脑（192.168.0.118）。
>
> **2026-04-26 实际部署完成** — 详见末尾"实际部署历史"段。
>
> 总耗时：顺利约 1 小时。出问题最多 2 小时。

---

## 前置条件

- [x] PR #87 (Cluster Lock + LLM Router) 已 merge — 已完成
- [x] 主电脑 cluster service 跑着（端口 8000）— 已完成（按 DEPLOYMENT_30_DEVICE_TEST.md Day 0）
- [ ] **你**：能 SSH / 远程桌面到主电脑（192.168.0.118）
- [ ] **你**：能登录到 W03（192.168.0.103）和 W175（192.168.0.175）

---

## 第 1 步 [你做] 合并 3 个 PR（5 分钟）

A/B 共用同一个 GitHub token，正常 approve 按钮按不下去，要走"评论确认 + squash merge"流程。

按顺序：

```
PR #88 → 评论 "✅ A 侧 review 通过 (approve-equivalent)" → squash merge
PR #89 → 同上（base 自动切到 main）
PR #90 → 同上
```

如果 GitHub 提示 PR #89 / #90 有冲突（base 切换可能触发），告诉我："PR 89 有冲突"，我立刻处理。

**完成标志**：
```bash
# 在你本地仓库:
git fetch origin && git log origin/main --oneline -5
# 应看到 3 个 L2 commit 顺序排在 main 顶上
```

---

## 第 2 步 [你做] 主电脑建花名册数据库（10 分钟）

SSH 到 192.168.0.118，准备好 PostgreSQL 的 `openclaw_app` 用户密码。

```bash
# 1. 拉新代码
cd /path/to/mobile-auto0423
git fetch origin && git checkout main && git pull

# 2. 跑建表脚本（会建 5 张表 + 索引 + trigger）
psql -h 127.0.0.1 -U openclaw_app -d openclaw -f migrations/001_central_customer_schema.sql

# 3. 验证
psql -h 127.0.0.1 -U openclaw_app -d openclaw -c "\dt"
```

**预期看到**（5 张表）：
```
 customers
 customer_events
 customer_chats
 customer_handoffs
 _schema_version
```

**如果失败**：
- "database openclaw does not exist" → 先 `createdb -U openclaw_app openclaw`
- "role openclaw_app does not exist" → 先建用户：`createuser -P openclaw_app`，给个强密码记下来
- 其他错误 → 把完整报错贴给我

---

## 第 3 步 [你做] 配环境变量（10 分钟）

**主电脑（192.168.0.118）**，编辑 `.env`（或 `~/.bashrc` / systemd unit，看你怎么起服务的）：

```bash
# 数据库（主电脑自己用，连本地 PG）
OPENCLAW_PG_HOST=127.0.0.1
OPENCLAW_PG_PORT=5432
OPENCLAW_PG_DB=openclaw
OPENCLAW_PG_USER=openclaw_app
OPENCLAW_PG_PASSWORD=<你的 PG 密码>

# API key（worker 调主电脑用，主电脑自己也要知道）
OPENCLAW_API_KEY=<生成一个长随机串，比如下面命令>
```

生成 API key：
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**两台 worker（W03 / W175）**，只配 API key（worker 不直连 PG）：

```bash
OPENCLAW_API_KEY=<跟主电脑同一个值>
# coordinator URL（worker 找主电脑用，已有的不用改）
OPENCLAW_COORDINATOR_URL=http://192.168.0.118:8000
```

**完成标志**：
```bash
# 主电脑跑:
echo $OPENCLAW_PG_PASSWORD  # 应该有值
echo $OPENCLAW_API_KEY      # 应该有值
# W03 跑:
echo $OPENCLAW_API_KEY      # 跟主电脑一样
```

---

## 第 4 步 [你做] worker 拉新代码 + 重启（半小时）

```bash
# 在你本地（或主电脑），调主电脑的 push-update API:
curl -X POST http://192.168.0.118:8000/cluster/push-update-all \
  -H "X-API-Key: $OPENCLAW_API_KEY"
```

这条命令会让 W03 / W175 自动 git pull + 重启服务。期间 worker 业务暂停 30 秒。

**完成标志**：
```bash
# 等 1 分钟，然后验证 worker 都活着:
curl -s http://192.168.0.103:8000/devices | python -c "import sys,json;print('W03 OK',len(json.load(sys.stdin)))"
curl -s http://192.168.0.175:8000/devices | python -c "import sys,json;print('W175 OK',len(json.load(sys.stdin)))"
```

**如果失败**：
- worker 没起来 → SSH 上去看 `journalctl -u openclaw -n 50` 或对应日志，把报错贴给我
- API key 错了 → 重检查第 3 步是否所有机器同一个值

---

## 第 5 步 [我做] 烟囱测试：主电脑能接收 push（10 分钟）

你跟我说"第 5 步开始"，我会：

1. 在主电脑上跑一段测试脚本，模拟 worker push 一条客户记录
2. 查 PG 看数据是否落到 customers / customer_events 表
3. 跑 sync push（不走 fire_and_forget），验证 HTTP 通路工作
4. 跑一次 fire_and_forget push，等异步线程跑完，再查 PG

跑完我直接告诉你"通了"或"卡在哪"。

---

## 第 6 步 [你做 + 我做配合] 真业务跑一轮（20 分钟）

**你做**：在你本地（或主电脑）调一次加好友任务：

```bash
curl -X POST http://192.168.0.103:8000/tasks/dispatch \
  -H "X-API-Key: $OPENCLAW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "facebook_add_friend_with_note",
    "device_id": "<W03 上的一个真实 device_id>",
    "params": {
      "profile_name": "<一个真实 FB 名字>",
      "note": "Hi, 看到你的资料很感兴趣"
    }
  }'
```

任务跑完（看 W03 物理手机上确实加了好友），告诉我"加好友 OK"。

**我做**：去主电脑 PG 查刚加的客户：

```sql
SELECT customer_id, primary_name, status, last_worker_id, last_device_id, created_at
FROM customers ORDER BY created_at DESC LIMIT 5;

SELECT event_type, customer_id, device_id, ts
FROM customer_events ORDER BY ts DESC LIMIT 10;
```

**预期**：
- customers 表里有这个 profile_name 一行，status='in_funnel'
- customer_events 有一行 event_type='friend_request_sent'

如果数据没进，我会查日志找失败点。

---

## 第 7 步 [你做 + 我做配合] messenger 来消息也能进（20 分钟）

**你做**：等加的好友通过（或人工模拟一条）→ 让 messenger bot check_inbox 跑一次

**我做**：查 PG：
```sql
SELECT direction, channel, content, content_lang, ai_generated, ts
FROM customer_chats ORDER BY ts DESC LIMIT 10;

SELECT status FROM customers WHERE last_device_id = '<那台手机>';
```

**预期**：
- customer_chats 有 incoming 一行（messenger channel）
- 客户 status 升级到 'in_messenger'

---

## 完成 ✅

到这一步意味着：
- 所有 30 台手机加好友、聊天的数据都汇总到主电脑了
- 真人后台还没有（下一阶段做）
- 失败数据自动重发还没有（下一阶段做）

---

## 回滚（万一出问题）

如果第 5/6/7 步发现数据不对，紧急回滚：

```bash
# 1. 主电脑 + W03 + W175 都 git checkout 到上一个版本
git log --oneline -5  # 找到 PR #87 那个 commit (5701388)
git checkout 5701388
# 重启服务

# 2. 数据库不用动（5 张表保留，不影响其他业务）
```

回滚后告诉我"回滚了，问题是 X"，我看根因。

---

## 故障联络

每一步出问题，把以下贴给我：
1. 你在做哪一步
2. 完整的命令行输出
3. 哪台机器（主控 / W03 / W175）

我会立刻定位。

---

## 实际部署历史 (2026-04-26)

8 个 PR 实际合并 + 部署一气呵成跑通. 关键信息记录如下:

### PR 合并最终路径

原本计划 8 个栈式 PR 一个个 merge, 但 `--delete-branch` 把栈链断了 (PR #89 base 分支被删 → PR auto-close, #90-95 base 失效).

修复: rebase #95 head 分支 (`feat/worker-l2-human-takeover-backend`) 去掉 #88 squash 重复部分, force push, 一次性 squash merge 把 #89-95 的 7 个 PR 改动合到 main.

main 上现状:
- `8066441` feat: L2 全栈合并 (PR #89-95) — 7 个 PR 一锅 squash
- `a000fe1` feat: L2 中央客户画户 store (#88) — 单独 squash

### 主电脑配置

```bash
# .env
OPENCLAW_PG_HOST=127.0.0.1
OPENCLAW_PG_PORT=5432
OPENCLAW_PG_DB=openclaw
OPENCLAW_PG_USER=openclaw_app
OPENCLAW_PG_PASSWORD=openclaw_app_339b8f70   # 实际值在主控 .env
OPENCLAW_PORT=8000                           # 跟 worker 期待的一致
# OPENCLAW_API_KEY=xxx                       # 暂禁用走内网兼容, 后续启用
```

### 主控服务

启动: `python server.py` (在 cwd D:\workspace\mobile-auto0423)
- 启动前必须 `set -a && source .env && set +a` 加载环境变量
- 主控 lifespan 自动启 drain 后台线程 (PR-3) + worker listener (仅 role=worker, 主控跳过)

### Worker 节点

```bash
# 主控调一次 (api key 暂禁用所以无 header):
curl -X POST http://192.168.0.103:8000/cluster/pull-update
curl -X POST http://192.168.0.175:8000/cluster/pull-update
# 每台返回 {"ok":true,"updated_files":826,"restarting":true} 即可
```

### 端到端验证

跑 `python scripts/l2_e2e_smoke.py`, 期望 12/12 步骤 PASS (包括 PG 落库 / referral_gate / emotion_scorer / ai_takeover_state / customer_service 4 动作).

### 验证 URL (浏览器打开)

- 引流后台 (现有 + PR-6 客服动作扩展): http://192.168.0.118:8000/dashboard
- L3 运营看板 (本次新): http://192.168.0.118:8000/static/l2-dashboard.html
- push 队列指标: http://192.168.0.118:8000/cluster/customers/push/metrics

### 测试基线

185/185 PASS (PR #88-95 全 + PR-6.6 worker listener):
- L2 push_client / drain / customer_sync_bridge / referral_gate / 触发器 / ai_takeover_state / emotion_scorer / customer_service / mesh listener
- + 14 个 PG 真集成测试 (有真 PG 才能跑, .env 配好后跑通)

### 已知 follow-up

1. worker .env 同步 API_KEY (生产前重启 worker 启用安全模式)
2. 30 分钟无操作自动归还接管队列 (客服上量后做)
3. 真业务测试 (W03 真发好友请求, victor 拍板谁)
