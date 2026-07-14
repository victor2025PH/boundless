# OpenClaw 运维工具腰带（OPS_TOOLBELT）

> 根目录 11 个 `.bat` 工具速查。详细用法看每个 `.bat` 的 `-?` / `-DryRun` 或 [SYSTEM_RUNBOOK](SYSTEM_RUNBOOK.md)。

---

## 🚀 服务运维（每天用）

| 工具 | 用途 | 常用 |
|------|------|------|
| `start.bat` | 启动 service_wrapper（含健康检查 + OTA） | `start.bat` |
| `stop.bat` | 优雅停止 wrapper + server | `stop.bat` |
| `status.bat` | 5+1 项巡检 + verdict + exit code | `status.bat` / `status.bat -Json` / `status.bat -Watch` |
| `migrate.bat` | uvicorn 直起 → service_wrapper 标准化 | `migrate.bat` / `migrate.bat -Force` |
| `cleanup_logs.bat` | 删 logs/_archive/ 旧 *.log | `cleanup_logs.bat -DryRun -Days 30` |

## 📊 业务报告（按需）

| 工具 | 用途 |
|------|------|
| `weekly_report.bat` | 跑 phase8_funnel_report.py → logs/reports/*.md |
| `diagnose_crash.bat` | 调查 server.py 历史 crash + 关联 ERROR | 

## 🔧 仓库工具（commit/push 前）

| 工具 | 用途 | 时机 |
|------|------|------|
| **`repo_health.bat`** | 6 项仓库健康（branch / dirty / disk / vendor） | 每次 commit 前 |
| **`sync_with_main.bat`** | sibling 协同（fetch + ahead/behind 报告） | 长任务期间定期 |
| **`branch_create.bat`** | 一键建 feat-ops-* 分支（防误 commit main） | 开新工作前 |
| `cleanup_branches.bat` | 删除已被 squash 到 main 的本地分支 | 偶尔（每周 1 次） |
| `install_hooks.bat` | 装 pre-commit hook（拦 main + runtime config） | 一次性安装 |

---

## 🎯 工作流决策树（v7）

### 1️⃣ 开新工作前

```
要开新功能 / 修 bug?
        │
        ▼
检查仓库状态:  repo_health.bat
        │
   ┌────┴────┐
verdict 0   verdict 1
HEALTHY     NEEDS ATTENTION
   │            │
   │      看输出按推荐 fix
   ▼            │
建分支:  branch_create.bat my-name
        (默认 fetch origin 防 stale main)
        │
        ▼
开始改代码
```

### 2️⃣ commit/push 前

```
准备 commit?
        │
        ▼
检查健康:  repo_health.bat -Fetch
        │
        ▼
git add ... && git commit
        │
        ▼ (pre-commit hook 自动跑)
hook 通过? ──Yes──> commit 成功
        │
        No
        │
        ▼
按 hook 提示修
(常见: 误在 main / 误 stage runtime config)
```

### 3️⃣ sibling 协同期间（origin/main 有更新）

```
长任务前先看节奏:  sync_with_main.bat -Stats
        │
        ▼ (high freq >=10/24h?)
       是 ─────────────> 短任务原则: 频繁 commit + push
        │
       否
        │
        ▼
工作期间定期: sync_with_main.bat
        │
        ▼ (有 [SUGGEST]?)
       是
        │
        ▼
        sync_with_main.bat -Auto    ← 推荐 (一键)
                │
        ┌───────┴───────┐
     rebase 成功      失败 (真冲突)
        │              │
     全部完成    自动 fallback -CherryPick
                       │
                   还失败? 手解 / 联系 sibling
```

**核心原则**：`-Auto` 是首选，git rebase 自带 patch-content detection 能处理多数 squash-merge 场景（详见 RUNBOOK §7）。

### 4️⃣ 服务挂了 / 异常

```
        服务异常
            │
            ▼
        status.bat
            │
   ┌────────┼────────┬────────┐
verdict 0  1        2       AUTH
HEALTHY  DEGRADED  DOWN    设备
   │        │       │       │
   ▼        ▼       ▼       ▼
正常用   看 [5/6]  start.bat  手机授权
        ERROR        │       USB 调试
        ↓            ▼
    diagnose_crash.bat  status.bat
       (查 crash 历史)   (验证)
```

---

## 🔥 紧急场景速查

| 场景 | 命令 |
|------|------|
| 后台打不开 | 用 `localhost:8000/dashboard`（RUNBOOK F1）；`status.bat` 看进程 |
| 设备 8DWOF6CY unauthorized | 手机点"始终允许此电脑调试"（RUNBOOK F2） |
| 服务反复 crash | `diagnose_crash.bat -Hours 1` 看 root cause |
| L2 PG init failed | RUNBOOK F9：PG superuser 跑 `ALTER ROLE openclaw_app SET lc_messages='C';` |
| 我误 commit 到 main | `git branch new + git reset --hard HEAD~1`（RUNBOOK §7 案例 #1） |
| sibling 合 PR 我落后 | `sync_with_main.bat -Auto`（RUNBOOK §7 案例 #5） |
| 一堆 obsolete 分支 | `cleanup_branches.bat` (dry-run) → `cleanup_branches.bat -Apply` |

---

## 📋 完整命令矩阵（含选项）

### status.bat
```
status.bat                    # 6 项巡检（默认）
status.bat -Json              # 结构化 JSON
status.bat -Watch [-Interval N]  # 持续刷新（默认 5s）
status.bat -Open              # 巡检后开 dashboard
status.bat -Beep              # AUTH/DEGRADED/DOWN 时蜂鸣
```

### sync_with_main.bat
```
sync_with_main.bat                        # 仅报告（read-only）
sync_with_main.bat -Auto                  # ⭐ 一键 rebase + fallback cherry-pick
sync_with_main.bat -Rebase -AutoStash     # 显式 rebase（处理 dirty）
sync_with_main.bat -CherryPick -AutoStash # 显式 cherry-pick（rebase 撞冲突时）
sync_with_main.bat -Pull                  # 仅 main 上 fast-forward
```

### repo_health.bat
```
repo_health.bat               # 6 项检查（不 fetch，快）
repo_health.bat -Fetch        # + git fetch origin main（5-10s 网络）
repo_health.bat -Json         # 结构化 JSON 输出
repo_health.bat -Verbose      # 详细文件列表
```

### branch_create.bat
```
branch_create.bat                      # feat-ops-yyyy-MM-dd-HHmm
branch_create.bat my-name              # feat-ops-my-name-yyyy-MM-dd
branch_create.bat -DryRun              # 看不跑
branch_create.bat -NoFetch             # 跳过 fetch（offline）
```

### cleanup_branches.bat
```
cleanup_branches.bat                   # dry-run（默认）
cleanup_branches.bat -Apply            # 真删
cleanup_branches.bat -Pattern "feat-*" # 自定义匹配
```

### diagnose_crash.bat
```
diagnose_crash.bat                     # last 5 crashes in 1 day
diagnose_crash.bat -Hours 1            # last hour
diagnose_crash.bat -Last 10 -Days 7    # last 10 in week
diagnose_crash.bat -Json               # 结构化输出
```

### cleanup_logs.bat
```
cleanup_logs.bat                       # dry-run, > 30 days
cleanup_logs.bat -Apply                # 真删
cleanup_logs.bat -Days 7               # 自定义保留天数
cleanup_logs.bat -IncludeRotated       # 含 logs/openclaw.log.N
```

---

## 维护

新增 .bat 工具时记得：
1. 在本文件加一行
2. 更新 SYSTEM_RUNBOOK.md §X（如需要）
3. 加 `pre_commit.ps1` 拦截（如关乎仓库纪律）

---

> 版本：v1（2026-04-26 创建，对应防呆 v5）
