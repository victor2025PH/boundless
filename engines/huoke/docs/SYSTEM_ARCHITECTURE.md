# OpenClaw 系统架构图谱（SYSTEM_ARCHITECTURE）

> 给开发工程师看的"系统怎么搭起来"。运维操作看 [`SYSTEM_RUNBOOK`](SYSTEM_RUNBOOK.md)。
>
> 维护人：victor / 创建：2026-04-26

---

## §1 — 进程层级（一图流）

```
┌─────────────────────────────────────────────────────────────────┐
│  service_wrapper.py（守护进程 / Windows 计划任务自启）            │
│  ├─ 30s health_check(/health)                                   │
│  ├─ 5min update_check(/cluster/update-package/info)             │
│  ├─ .restart-required 哨兵 → 立即重启                            │
│  └─ 连续重启 ≥ 20 次 → 自动放弃                                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ subprocess.Popen
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  server.py → uvicorn → src.host.api:app（FastAPI / single proc） │
│  ├─ WorkerPool          (ThreadPoolExecutor, max 4)             │
│  │   └─ run_task(task_id) → executor._execute_facebook()        │
│  │       └─ FacebookAutomation.{add_friend, send_greeting,      │
│  │          check_messenger_inbox, _ai_reply_and_send, ...}     │
│  ├─ Scheduler           (daemon, 30s 轮询 schedules 表)          │
│  ├─ HealthMonitor       (daemon, 60s/10s 双速)                   │
│  ├─ Watchdog            (30s 设备掉线判定)                        │
│  ├─ DeviceManager       (adb + uiautomator2，惰性创建 u2 客户端)  │
│  ├─ ProxyHealth         (5min 出口 IP 验证)                      │
│  ├─ RouterManager       (5min 检查 4G/Wi-Fi 路由)                │
│  ├─ W03LeadsCache       (30s 刷新 leads/stats/funnel)            │
│  ├─ device_stats_agg    (按天聚合设备统计)                        │
│  ├─ central_push_drain  (60s 推 worker→coordinator)              │
│  ├─ ab_auto_graduate    (A/B 实验毕业线程)                        │
│  └─ pending_rescue_loop (15s 扫 orphan pending 任务)             │
└─────────────────────────────────────────────────────────────────┘
        │                                          │
        ▼                                          ▼
┌──────────────┐                          ┌──────────────────┐
│ SQLite WAL    │                          │ Android 设备集群  │
│ data/openclaw │                          │ (adb + u2)       │
│   .db         │                          │ ├─ 4HUSIB4T      │
│              │                          │ ├─ IJ8HZLOR      │
│              │                          │ └─ 8DWOF6CY ⚠     │
└──────────────┘                          └──────────────────┘
```

---

## §2 — 任务派发时序（A 业务链典型，B 类似）

```
[调用方] (UI / API / Scheduler)
  │
  │ POST /tasks {"task_type":"facebook_add_friend", "params":{...}}
  ▼
[src/host/routers/tasks.py]
  │ task_store.create_task() → SQLite INSERT tasks (status='pending')
  ▼
[Scheduler thread, 30s tick]   或   [pending_rescue_loop, 15s tick]
  │ SELECT FROM tasks WHERE status='pending'
  ▼
[WorkerPool.submit(run_task, task_id)]
  │ ThreadPoolExecutor 入队（max 4 线程）
  ▼
[executor.py::run_task(task_id)]
  │ acquire device_section_lock
  │ task_gate 检查 (config/task_execution_policy.yaml)
  │ timeout wrapper
  ▼
[executor._execute_facebook(manager, resolved, task_type, params)]
  │ _fresh_facebook() → 启动 FB App
  │ fb.add_friend_with_note(target, note=...)
  ▼
[src/app_automation/facebook.py::add_friend_with_note]
  │ search_people() → 搜索框
  │ click profile → fill note → 点击 "Add Friend"
  │ __with_device_lock(): record_friend_request() 写 DB
  ▼
[set_task_result(task_id, ok, error, extra)]
  │ UPDATE tasks SET status='completed' / 'failed'
  │ push_event("task.completed", ...)
```

---

## §3 — 数据层

### 3.1 主 DB（SQLite WAL）

`data/openclaw.db` — A/B 共享。关键表（不完整列举）：

| 表 | 用途 | 主写入方 |
|----|------|---------|
| `tasks` | 任务队列 | task_store / executor |
| `schedules` | 定时任务定义 | Scheduler |
| `facebook_friend_requests` | A 加好友记录 | `add_friend_with_note` |
| `facebook_inbox_messages` | A/B 双写：A 写 outgoing greeting，B 写 incoming + AI 回复 | `send_greeting_after_add_friend` / `_ai_reply_and_send` |
| `facebook_groups` | A 群成员打招呼 | `extract_group_members` |
| `facebook_phase_state` | 设备阶段状态 | facebook.py |
| `customer_profiles` | A/B 共享客户画像（L1/L2） | central_customer_store |
| `cluster_hosts` | 集群主机（coordinator-only） | multi_host |

> 完整 schema 看 `migrations/` 或 `src/host/database.py`。

### 3.2 中央推送队列

`config/central_push_queue.db` — worker → coordinator 推送临时队列。
正常状态有 `.db-shm` / `.db-wal` 这两个 WAL 副本文件，**进程跑着就有**，不是异常。

### 3.3 集群状态

`config/cluster_state.json` — coordinator 写，记录已知主机心跳，10s 一次更新。

---

## §4 — 配置文件清单

| 文件 | 谁读 | 谁写 | 触发时机 |
|------|------|------|---------|
| `config/cluster.yaml` | service_wrapper / multi_host | 手工 | 启动 |
| `config/cluster_state.json` | multi_host | multi_host | 10s 心跳 |
| `config/devices.yaml` | DeviceManager | 手工 | 启动 + preflight |
| `config/device_aliases.json` | DeviceManager | API + 手工 | 任务前重载 |
| `config/device_registry.json` | executor | adb 发现 + API | 每次 discover |
| `config/facebook_playbook.yaml` | facebook.py | 手工 | 热重载 `POST /facebook/playbook/reload` |
| `config/chat_messages.yaml` | B AI 回复 | 手工 | 启动 |
| `config/fb_target_personas.yaml` | A persona 分类 | 手工 | 启动 |
| `config/task_execution_policy.yaml` | TaskPolicy | 手工 | 启动 |
| `config/scheduled_jobs.json` | job_scheduler | 手工 + API | 启动 + 修改 |
| `config/notify_config.json` | alert_notifier | 手工 + API | 启动 |
| `config/ai.yaml` | LLM 调用 | 手工 | 启动 |

---

## §5 — 端口 / 网络

| 用途 | 端口 | 谁监听 | 备注 |
|------|------|--------|------|
| OpenClaw API | 18080 默认 / 当前 8000 | server.py | `OPENCLAW_PORT` 环境变量覆盖 |
| Coordinator 集群 | 同上（共享 server） | server.py 内 multi_host 模块 | `cluster.yaml::local_port` |
| ADB server | 5037 | adb daemon | `ADB_SERVER_HOST` 可指向远端 |
| u2 atx-agent (per device) | 7912 (设备端) | uiautomator2 | adb forward |
| scrcpy server (per device) | 27183 (设备端) | scrcpy | 端口转发 |
| Wi-Fi backup ADB (per device) | 5555 (设备端) | adb tcpip | health_monitor 自动建立 |

---

## §6 — A/B 业务链调用链（详）

### 6.1 A 链：facebook_add_friend

```
POST /tasks
  → routers/tasks.py
  → task_store.create_task
  → [SQLite INSERT]
  → Scheduler 唤醒
  → WorkerPool.submit
  → executor.run_task
  → executor._execute_facebook (task_type='facebook_add_friend')
  → FacebookAutomation.add_friend_with_note(target, note)
       ├─ search_people(target)
       ├─ click profile_card
       ├─ fill_note_box(note)
       ├─ tap_add_friend_button
       └─ __with_device_lock:
            record_friend_request → fb_store.facebook_friend_requests INSERT
  → set_task_result
```

### 6.2 B 链：facebook_check_inbox

```
Scheduler tick (config/scheduled_jobs.json::facebook_check_inbox)
  → 创建 task (cooldown 900s, executor.py:165)
  → WorkerPool.submit
  → executor._execute_facebook (task_type='facebook_check_inbox')
  → FacebookAutomation.check_messenger_inbox()
       ├─ 进入 Messenger 收件箱
       ├─ 列出未读对话
       ├─ 逐个进入 → _ai_reply_and_send(thread)
       │     ├─ 抽取上下文
       │     ├─ 调 AI 生成回复
       │     ├─ 输入框输入 + 发送
       │     └─ fb_store.facebook_inbox_messages INSERT (direction='outgoing', ai_decision=...)
       └─ 退出
  → set_task_result
```

---

## §7 — 启动流程（完整）

```
1. service_wrapper.py 启动
   └─ 解析 args (--no-auto-update, --update-interval N)
   └─ 设置 logging → logs/service_wrapper.log
   └─ 检查 .restart-required（如存在则上次是哨兵触发）
   └─ subprocess.Popen([python, server.py])

2. server.py 启动
   └─ logging → logs/host_api.log
   └─ uvicorn.run(src.host.api:app, host=$OPENCLAW_HOST or 0.0.0.0, port=$OPENCLAW_PORT or 18080)

3. src/host/api.py FastAPI lifespan startup
   ├─ DB 初始化
   ├─ DeviceManager 加载 config/devices.yaml + adb 发现
   ├─ WorkerPool 启动 (max_workers=4)
   ├─ Scheduler 启动
   ├─ HealthMonitor 启动 + ADB keepalive
   ├─ Watchdog 启动
   ├─ multi_host (coordinator 角色) 启动 → 监听集群心跳
   ├─ W03 CRM Cache 启动 + 预热
   ├─ device_stats_aggregator 启动
   ├─ W03 event bridge 启动
   ├─ pending_rescue_loop 启动
   ├─ ProxyHealth 启动
   ├─ RouterManager 启动
   ├─ central_push_drain 启动
   ├─ ab_auto_graduate 启动
   └─ VLM warmup 异步排队

4. service_wrapper 进入主循环
   ├─ 30s 一次 health_check(/health)
   ├─ 5min 一次 update_check（如开启 auto-update）
   └─ 监听 .restart-required
```

---

## §8 — 关键模块责任划分

| 模块 | 文件 | 职责 |
|------|------|------|
| 任务存储 | `src/host/task_store.py` | tasks 表读写 |
| 任务调度 | `src/host/scheduler.py` + `src/host/job_scheduler.py` | 定时拉起任务 |
| 任务执行 | `src/host/executor.py` | task_type 路由 + 设备锁 + 执行 |
| 任务策略 | `src/host/task_policy.py` + `config/task_execution_policy.yaml` | 哪些任务允许跑 |
| 设备管理 | `src/device_control/device_manager.py` | adb 发现 + u2 连接 |
| 设备健康 | `src/host/health_monitor.py` | 60s 检查 + ADB keepalive + Wi-Fi 备份 |
| 设备守护 | `src/device_control/watchdog.py` | 30s 掉线判定 |
| FB 业务 | `src/app_automation/facebook.py` | 全部 A/B 业务函数 |
| FB 数据 | `src/host/fb_store.py` | A/B 共享写入接口（CONTACT_EVT_*） |
| 集群多主 | `src/host/multi_host.py` | coordinator/worker 心跳 |
| 中央存储 | `src/host/central_customer_store.py` | PG 客户画像（L2） |

---

## §9 — 不在本仓库的部分（防混淆）

详见 [`CLAUDE.md`](../CLAUDE.md) 的"明确不在本 repo 范围"章节。

简记：contacts/handoff 子系统、Telegram/LINE RPA、telegram-mtproto-ai 那一套 messenger_rpa runner，全在另一个 repo `telegram-mtproto-ai`。本仓库的 "Messenger" = FB Messenger UI 自动化。
