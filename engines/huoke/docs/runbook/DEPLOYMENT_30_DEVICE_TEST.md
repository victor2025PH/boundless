# 30 设备首测部署指南 (2026-04-26 → 2026-04-27)

## 拓扑

| 节点 | IP | 角色 | 当前 phones | 计划 phones |
|---|---|---|---|---|
| 主控 (这台) | 192.168.0.118 | coordinator + dev/test | 4 (3 connected + 1 unauth) | 4 (不变) |
| W03 | 192.168.0.103 | worker | 0 | ~15 |
| W175 | 192.168.0.175 | worker | 13 | ~15 (维持当前 + 续接) |

**总目标**: 主控 4 + W03 15 + W175 15 = 34, 业务焦点 30 台 (W03 + W175)。

## Day 0 (2026-04-26 已完成)

✅ 主控 cluster service 启动 (port 8000), 接 W03 + W175 心跳
✅ Cluster Lock Service 部署 (50 测全绿, 含 17 lock + 10 client + 23 LLM router)
✅ LLM Router 部署 (主控 ollama qwen2.5:latest 验证可用)
✅ 旧"三角色"协同 docs 归档
✅ PR #86 关闭 (静态 yaml 不适用跨机)

## Day 1 (2026-04-27 早上) — 物理接入

### 1. USB 接入 30 台手机

victor 操作:
- 接 ~15 台 phones 到 W03 USB hub
- 接 ~15 台 phones 到 W175 USB hub (现 13 台保留 + 续接 2 台或维持 13)
- 确认每台 ADB authorized (手机弹窗 "始终允许")

### 2. 验证 worker 探测到新 device

```bash
# 主控这台跑:
curl -s http://192.168.0.103:8000/devices | python -c "import sys,json;print(len(json.load(sys.stdin)))"
curl -s http://192.168.0.175:8000/devices | python -c "import sys,json;print(len(json.load(sys.stdin)))"
curl -s http://127.0.0.1:8000/cluster/overview
```

预期: total_devices_online ≥ 30

### 3. (可选) Worker 拉最新代码

如 W03 / W175 需要新 lock client SDK (本次新增, 还没在 worker 上):

```bash
# 主控调 push-update API, 让 worker pull 最新代码 + restart
curl -X POST http://127.0.0.1:8000/cluster/push-update-all
```

⚠️ 仅在 worker 业务空闲时做; restart 期间 worker 业务暂停 ~30s。

## Day 1 (下午) — 30 台首跑 + 监控

### 业务流水线

每台 worker 上跑同样的:
1. **FB add_friend program** — 搜索 + 加好友
2. **Messenger reply program** — 收到好友确认后开始 chat
3. **LINE handoff** — 引导客户加 LINE
4. **人工接管 LINE** (人工 - 暂无后台, 看 messenger 上下文凑合)

### 关键监控点

实时:
```bash
# 集群总览 (每 5s 刷)
watch -n 5 'curl -s http://127.0.0.1:8000/cluster/overview | python -m json.tool'

# Lock 争用 (任意时刻应 < 30 个 active)
curl -s http://127.0.0.1:8000/cluster/locks | python -m json.tool

# 各 worker funnel (现各 worker 独立 db, 跨 worker 聚合 P2)
curl -s http://192.168.0.103:8000/funnel/snapshot
curl -s http://192.168.0.175:8000/funnel/snapshot
```

需要看的指标:
- `total_devices_online` 持续 ≥ 30
- `tasks_active` per worker (业务进行中)
- `cluster/locks::metrics::wait_timeout_total` 不应快速增长 (大量 wait timeout = 锁争用过高)
- `cluster/locks::metrics::evicted_total` 应为 0 (除非有 priority>=90 任务)
- 各 worker `health.uptime_seconds` 不重置 (重置 = 崩溃后被人工重启)

### 告警条件 (人工监控)

| 指标 | 告警阈值 | 处置 |
|---|---|---|
| device_online < 25 | 5 min 持续 | 检查掉线 phones, 重插 USB |
| wait_timeout_total/min > 20 | 10 min 持续 | 锁争用过高, 看 task 派发逻辑 |
| heartbeat 失败 worker | 立即 | 看该 worker daemon 是否健康 |
| LLM 全 fallback 走 cloud | 10 min 持续 | ollama 故障, 重启 ollama |

## 已知限制 (今天没做的)

| 项 | 说明 | 何时做 |
|---|---|---|
| **客户画像中央 PG** | 各 worker 自写 SQLite, 跨 worker 查难 | L2 本周内 |
| **人工接管工作台** | LINE 引流后无统一 panel 看客户上下文 | L3 下周 |
| **跨 worker funnel 聚合** | /cluster/funnel 还没 | 明天若需要 1 小时实现 |
| **WebSocket lock 事件** | 当前 polling 200ms; 大规模时改 push | 200 设备稳定后 |
| **Idempotency tokens** | acquire 网络重试可能创 2 锁 | 200 设备稳定后 |
| **Worker pull-update CI** | 现手动触发, 没 canary | L4 |

## Worker 上启用 Cluster Lock Client (可选)

业务代码当前用 `worker_pool` 内的 thread lock (单进程内有效). 若想用 cluster 锁:

```python
from src.host.cluster_lock_client import device_lock

with device_lock(device_id, "send_greeting", priority=50, ttl_sec=300):
    # 业务逻辑, heartbeat 自动续 lease
    ...
```

worker 需要的环境:
- `OPENCLAW_COORDINATOR_URL=http://192.168.0.118:8000` (W175 的 cluster.yaml 已配)
- `OPENCLAW_API_KEY` (如启 auth, 主控+所有 worker 必须一致, 当前未启)
- `pip install pyyaml` (已装)

集成切换策略:
- 渐进迁移, 优先在跨 worker 共享 device 的场景 (本次 30 台不涉及, 仍 worker_pool)
- 30 台稳定 1 周 → 高优 task (priority>=90, 比如紧急回复) 切 cluster lock
- 200 设备时全切

## 应急预案

| 场景 | 处置 |
|---|---|
| 主控挂 | worker 自治继续运行 (heartbeat 失败但业务继续, lock 走 fallback_local) |
| W03/W175 挂 | 主控仍可通过 worker_device_proxy 显示该 worker offline, 业务 task 不派发 |
| 主控 ollama 挂 | LLM Router circuit breaker 自动切 cloud (需 ANTHROPIC_API_KEY) |
| Lock 服务死锁 | TTL 自动过期 (默认 300s), worker 重新拿锁 |
| 30 台都掉线 | 检查 USB hub 供电 / driver / adb connection |

## 测试快速 sanity 检查 (跑 30 台前)

```bash
# 主控本机自测
python -m pytest tests/test_cluster_lock.py tests/test_cluster_lock_client.py tests/test_llm_router.py -q
# 预期: 50 passed

# 主控 + worker 网络通讯
curl -s http://127.0.0.1:8000/cluster/overview        # 看 hosts_online >= 2
curl -s http://192.168.0.103:8000/health              # W03 健康
curl -s http://192.168.0.175:8000/health              # W175 健康
curl -s http://127.0.0.1:11434/api/tags               # ollama 模型列表

# Lock service 端到端
curl -X POST http://127.0.0.1:8000/cluster/lock/acquire \
     -H "Content-Type: application/json" \
     -d '{"worker_id":"sanity","device_id":"test","ttl_sec":10}'
# 预期: {"granted":true,"lock_id":"..."}

# LLM Router 端到端
python -c "from src.host.llm_router import llm_complete; print(llm_complete(prompt='1+1=', max_tokens=10))"
# 预期: ok=True, backend=ollama_central
```

## Owner & 责任划分

- **架构 / 服务**: A (我, claude opus 4.7) 负责实施 + 监控
- **物理接入** (USB / 手机授权): victor
- **业务流水线 (FB / Messenger / LINE)**: 已有代码, sibling 之前实施
- **人工接管 (明天 LINE 引流后)**: victor 看 messenger 上下文 (人工后台 L3 下周)

— 部署 ready. 明早 victor 接好 USB 后即可开始。
