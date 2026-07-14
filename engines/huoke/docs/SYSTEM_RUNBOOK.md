# OpenClaw 系统运维手册（SYSTEM_RUNBOOK）

> 这是**系统运维 SSOT**。"系统是什么 / 怎么起 / 出问题怎么救"先看这里。
> 配套文档：[SYSTEM_ARCHITECTURE](SYSTEM_ARCHITECTURE.md)（架构）、[CAPABILITIES](CAPABILITIES.md)（业务能力）、[INTEGRATION_CONTRACT](INTEGRATION_CONTRACT.md)（A/B 契约）。
>
> 维护人：victor / 创建：2026-04-26 / 最后更新：2026-04-26

---

## §0 — 30 秒速查（崩溃 / 新窗口必读）

| 项 | 值 |
|----|-----|
| 是什么 | FB/Messenger 移动端 RPA bot，主控 + 多手机集群 |
| 当前角色 | **coordinator（主控）** — `config/cluster.yaml::role` |
| 进程链 | `service_wrapper.py` →（subprocess）→ `server.py` → `uvicorn` → `src.host.api:app` |
| 默认端口 | **18080**（`src/openclaw_env.py::DEFAULT_OPENCLAW_PORT`） |
| 当前生效端口 | 看 `OPENCLAW_PORT` 环境变量；本机当前 = `8000` |
| 后台地址 | **http://localhost:8000/dashboard** ⚠️ 用 `localhost`，不要用 `192.168.x.x`（看 §3 F1） |
| 启停 | 根目录 `start.bat` / `stop.bat` / `status.bat` 三件套 |
| 主日志 | `logs/openclaw.log`（最完整）+ `logs/host_api.log`（API） |
| 主 DB | `data/openclaw.db`（SQLite WAL） |

---

## §1 — 系统全貌

### 1.1 进程拓扑（同一进程内多线程，非多进程）

```
service_wrapper.py（守护，30s 健康检查 + 5min 自动更新检查 + .restart-required 哨兵）
  └─ server.py（subprocess） → uvicorn → src/host/api:app（FastAPI）
       ├─ WorkerPool         (ThreadPoolExecutor, 4 线程, 任务执行)
       ├─ Scheduler          (daemon thread, 30s 轮询 schedules 表)
       ├─ HealthMonitor      (daemon thread, 60s 设备健康)
       ├─ Watchdog           (30s 设备掉线检测)
       ├─ DeviceManager      (adb + uiautomator2)
       ├─ W03 CRM Cache      (30s 刷新)
       ├─ ProxyHealth        (5min 检查代理出口 IP)
       ├─ central_push_drain (60s 清 config/central_push_queue.db)
       └─ ab_auto_graduate   (A/B 实验毕业线程)
```

### 1.2 A 业务链 vs B 业务链（同一进程内 task_type 分流）

| 业务 | task_type | 核心函数 | 文件 |
|------|-----------|----------|------|
| **A — 加好友 / 打招呼 / 群提取** | `facebook_add_friend` / `facebook_send_greeting` / `facebook_extract_members` / `facebook_browse_feed` | `add_friend_with_note()` / `send_greeting_after_add_friend()` / `extract_group_members()` | `src/app_automation/facebook.py` |
| **B — 聊天系统** | `facebook_check_inbox` / `facebook_check_message_requests` / `facebook_send_message` | `check_messenger_inbox()` / `check_message_requests()` / `_ai_reply_and_send()` | `src/app_automation/facebook.py` |
| **共享** | — | `task_store` / `fb_store` / `database` | `src/host/*.py` |

> 边界权威：[`docs/INTEGRATION_CONTRACT.md`](INTEGRATION_CONTRACT.md)。

### 1.3 端口与角色

| 角色 | `cluster.yaml::local_port` | 默认 | 当前生效（覆盖来源） |
|------|---------------------------|------|---------------------|
| coordinator (主控) | 18080 | 18080 | `OPENCLAW_PORT=8000` 环境变量 → **8000** |
| worker | 18080 | 18080 | — |

### 1.4 关键路径速查

| 类型 | 路径 |
|------|------|
| 主 DB | `data/openclaw.db`（A/B 共享，SQLite WAL） |
| 中央推送队列 | `config/central_push_queue.db`（worker → coordinator 推送临时队列；`.db-shm` / `.db-wal` 是正常的 WAL 文件） |
| 设备配置 | `config/devices.yaml` / `config/device_aliases.json` / `config/device_registry.json` |
| 集群配置 | `config/cluster.yaml` / `config/cluster_state.json` |
| 业务配置 | `config/facebook_playbook.yaml` / `config/chat_messages.yaml` / `config/fb_target_personas.yaml` |
| 任务策略 | `config/task_execution_policy.yaml`（控制自动调度 on/off） |
| 主日志 | `logs/openclaw.log`（最完整，按大小滚动 .log.1 .log.5） |
| API 日志 | `logs/host_api.log` |
| 守护日志 | `logs/service_wrapper.log` |
| 截图 | `logs/screenshots/task_*.png` |

---

## §2 — 日常操作

### 2.1 启停（每天用）

```bat
start.bat       :: 启动 service_wrapper（生产推荐）
stop.bat        :: 优雅停止 service_wrapper + server
status.bat      :: 5 项巡检 → GO/NO-GO
```

### 2.2 看日志

```bash
# 主日志（最完整，可 -f 持续看）
tail -f logs/openclaw.log

# 看 B 收件箱
grep "check_messenger_inbox" logs/openclaw.log

# 看 A 加好友
grep "add_friend_with_note" logs/openclaw.log

# 看 MessengerError 错误码
grep "MessengerError(code=" logs/openclaw.log

# 看设备掉线 / 重连
grep -E "u2 连接|首次上线|掉线" logs/openclaw.log
```

### 2.3 热重载 B 配置（无需重启）

```bash
curl -X POST http://localhost:8000/facebook/playbook/reload
```

### 2.4 查任务 / 设备 / 漏斗

```bash
curl http://localhost:8000/health                  # 健康
curl http://localhost:8000/devices                 # 设备列表
curl http://localhost:8000/facebook/funnel         # B 漏斗
curl http://localhost:8000/facebook/risk/status    # 风控状态
curl http://localhost:8000/tasks/{task_id}         # 任务详情
```

### 2.5 手动派任务

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_type":"facebook_add_friend","device_id":"4HUSIB4TBQC69TJZ","params":{...}}'
```

---

## §3 — 应急恢复 SOP

### 三步速查

```powershell
:: Step 1 — 进程
tasklist | findstr python.exe

:: Step 2 — 端口
netstat -ano | findstr "LISTENING" | findstr ":8000 :18080"

:: Step 3 — 端点
curl http://127.0.0.1:8000/health
```

或一键：`status.bat`

### 故障字典

#### F1 — 后台页面打不开（最常见）

| | |
|---|---|
| **症状** | 浏览器报错 / SYN_SENT / 连接被重置 |
| **根因 (3 选 1)** | (A) **进程启动时用了 `--host 127.0.0.1` 命令行参数**（最常见，绕过 server.py 默认值，见 F8）<br>(B) 环境变量 `OPENCLAW_HOST=127.0.0.1` 强制 loopback<br>(C) 服务确实在跑但你浏览器用了局域网 IP 访问 |
| **快速判定** | `status.bat` 输出 [2/5] 看 `bind=` 段：<br>• `bind=127.0.0.1:xxxx (loopback only)` → 是 |
| **快速修** | 改用 **http://localhost:8000/dashboard**（loopback 仍可访问） |
| **永久修** | 1) `stop.bat`；2) `echo %OPENCLAW_HOST%` 看环境变量，若有 127.0.0.1 → `set OPENCLAW_HOST=`；3) `start.bat`（标准方式起 service_wrapper → server.py，server.py 默认 `0.0.0.0`）；4) 重启后 `status.bat` 应显示 `bind=0.0.0.0:xxxx (LAN reachable)` |

#### F2 — 设备 unauthorized

| | |
|---|---|
| **症状** | log: `设备 XXX 状态异常: unauthorized — 请在手机上确认USB调试授权` |
| **根因** | 手机重启 / 换电脑 / USB 调试授权失效 |
| **修** | 1) 手机上点弹窗"始终允许此电脑调试"；2) 重新插拔 USB；3) `adb kill-server && adb start-server` |

#### F3 — u2 连接失效循环

| | |
|---|---|
| **症状** | log 反复 `u2 连接失效，重连中` |
| **根因** | 手机端 uiautomator2 服务挂 / 屏幕黑屏 / 输入法异常 |
| **修** | 1) `adb -s <serial> shell am force-stop com.github.uiautomator`；2) `python scripts/unify_ime.py`；3) 解锁手机屏幕 |

#### F4 — task_execution_policy: manual_only=True（不是 bug）

| | |
|---|---|
| **症状** | 任务不自动跑 |
| **真相** | 当前**刻意**配的手动模式。log 显示 `manual_only=True gate_mode=balanced db_sched=False json_jobs=False recover=False wp_thread=False preflight=False geo=False` |
| **配置** | `config/task_execution_policy.yaml` |
| **要自动跑就改这个文件** | 改完重启生效 |

#### F5 — service_wrapper 不重启 server（连续重启爆炸）

| | |
|---|---|
| **症状** | server 挂了但 wrapper 不拉起 |
| **根因** | 连续重启 ≥ 20 次，wrapper 自动放弃（`service_wrapper.py:278`） |
| **修** | 1) 删 `.restart-required` 哨兵文件；2) `stop.bat && start.bat`；3) 看 `logs/service_wrapper.log` 找连重启原因（多半是 server 启动崩） |

#### F6 — 端口被占（`OSError: [Errno 10048]`）

| | |
|---|---|
| **症状** | 启动报端口被占 |
| **修** | 1) `netstat -ano \| findstr :8000`；2) `taskkill /F /PID <pid>`；3) 重启 |

#### F7 — central_push_queue.db-wal/-shm 一直存在

| | |
|---|---|
| **症状** | git status 显示这两个文件存在 |
| **真相** | 这是 SQLite WAL 模式正常文件。**进程在跑就会有**。只有干净退出后会消失 |
| **修** | 不用修。如果 git status 烦人，加 `.gitignore` |

#### F8 — 进程是 uvicorn 直起，没有 service_wrapper 守护

| | |
|---|---|
| **症状** | `status.bat` [1/5] 显示 `[OK] uvicorn PID=xxx` 且警告 `Not started via service_wrapper`，或者历史上"程序自己挂了没人拉起" |
| **根因** | 有人手动跑了 `python -m uvicorn src.host.api:app --host ... --port ...` 或类似命令，绕过了 `service_wrapper.py` |
| **后果** | 1) 没有 30s 健康检查自动重启；2) 没有 5min OTA 自动更新检查；3) 没有 `.restart-required` 哨兵机制；4) 命令行 `--host 127.0.0.1` 还会触发 F1 |
| **检测命令** | `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` `\| Where-Object { $_.CommandLine -match 'uvicorn' } \| Select CommandLine` |
| **修** | 一键迁移：**`migrate.bat`**（自动检测当前端口 → 写入 config/launch.env → stop uvicorn → start wrapper → 验证） |

#### F9 — central_store PG init UnicodeDecodeError (byte 0xd6)

| | |
|---|---|
| **症状** | `logs/openclaw.log` 反复 `[central_store] PG pool init failed: 'utf-8' codec can't decode byte 0xd6 in position 55: invalid continuation byte` 或 `reset+retry also failed: ...` |
| **真因（已诊断）** | PG server locale = `zh_CN.GBK` → server 用 GBK 编码错误消息（如认证失败 / 拒绝连接的握手期错误）→ psycopg2 默认用 utf-8 解码 → 撞 GBK 字节（如 0xd6 = "误"的首字节）→ UnicodeDecodeError |
| **影响范围** | 仅影响 L2 中央客户画像（central_customer_store）；**不影响**主功能（FB 加好友 / Messenger 收件箱 / 设备控制） |
| **检测命令** | `grep -E "central_store.*PG.*failed\|UnicodeDecodeError" logs/openclaw.log` |
| **2026-04-26 已应用 fix（src/host/central_customer_store.py）** | 1) DSN `client_encoding=utf8` — 查询/数据传输用 utf-8（L88-92）<br>2) `_conn()` catch `UnicodeDecodeError` 标记 `discard=True` 让 pool 丢弃中毒连接（Phase-13 已有兜底，L124-134） |
| **⚠️ 试过的错路（不要再走）** | 在 DSN 加 `options='-c lc_messages=C'` — **PG 会拒绝**：`lc_messages` 是 PG SUSET（superuser-set），普通用户在 DSN options 里设不被允许 |
| **真正根治（需 PG superuser 一次性执行）** | 在 PG 上以 superuser 跑：<br>`ALTER ROLE openclaw_app SET lc_messages='C';`<br>之后该用户所有 session 错误消息都是英文 ASCII，不再撞 GBK 0xd6。**这是推荐的最终修复**。 |
| **其它方案** | 1) PG server 全局 `postgresql.conf` 改 `lc_messages='C'` + 重启；2) PG server locale utf-8（`initdb -E UTF8 --locale=C`，需重做 cluster）；3) 临时绕过：注释 `src/host/api.py` 里 central_store init 调用 |
| **TODO** | 完整诊断 → `docs/runbook/L2_OPS_HANDBOOK.md` |

### 故障码字典（MessengerError）

源代码：`src/app_automation/facebook.py`，grep `MessengerError(code=`。常见码（详见代码注释）：
- `code=0`: 未知/通用失败
- `code=1`: 收件箱入口找不到
- `code=2`: 列表 timeout
- `code=3`: 对话页打开失败
- `code=4`: AI 回复生成失败
- `code=5`: 发送按钮找不到
- `code=6`: 发送后未回到列表

> TODO: 完整对照表搬到这里（grep 输出后整理）

### 已知陷阱（来自 memory 沉淀）

| 陷阱 | 后果 | 规避 |
|------|------|------|
| **smart_tap 自学习污染导航 selector** | FB 主导航点错位置 | 导航类操作必须硬编码 u2 selector + 页面自检；见 `memory/autoselector_pitfall.md` |
| **MIUI 红米手机管家弹窗** | 安装/操作被弹窗打断 | `python scripts/disable_miui_security_popups.py` |
| **手机输入法不支持中文** | adb 输入中文乱码 | `python scripts/unify_ime.py` 切到 ADBKeyboard |
| **共用 GitHub token 自审被拒** | `gh pr review --approve` 永远失败 | 用 `--comment` + marker `"✅ A 侧 review 通过 (approve-equivalent)"` |

---

## §4 — Claude 新窗口 onboarding

进入新 Claude session 时（无论崩溃恢复 / 新开窗口）：

1. ✅ **本文件 §0 §1 §3** 先读（30 秒拿到全局）
2. ✅ 出问题 → §3 故障字典
3. ❌ **不要重做"进程结构分析"** — §1 已有权威拓扑
4. 业务能力问题 → [`docs/CAPABILITIES.md`](CAPABILITIES.md)
5. 进程/数据/端口架构 → [`docs/SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md)
6. A/B 边界争议 → [`docs/INTEGRATION_CONTRACT.md`](INTEGRATION_CONTRACT.md)（权威）
7. Git workflow / approve-equivalent → [`CLAUDE.md`](../CLAUDE.md)

---

## §5 — 配置变更日志

| 日期 | 变更 | 谁 |
|------|------|-----|
| 2026-04-26 | 创建 SYSTEM_RUNBOOK / SYSTEM_ARCHITECTURE / CAPABILITIES + 4 件套 (start/stop/status/migrate.bat) | victor + Claude |
| 2026-04-26 | 新增 `config/launch.env` 启动配置（OPENCLAW_PORT=8000 持久化）；start.ps1 启动时自动加载 | Claude |
| 2026-04-26 | 从 uvicorn 直起 (`--host 127.0.0.1 --port 8000`) **迁移到 service_wrapper** 标准启动 → bind 0.0.0.0 LAN 可达 + 健康检查/OTA 恢复 | Claude (migrate.bat) |
| 2026-04-26 | docs/ 归档分层：archive/ runbook/ dev/ 三子目录（28 个 git mv）+ docs/_INDEX.md 导览 | Claude |
| 2026-04-26 | 根目录瘦身：DLL → vendor/，旧 *.log → logs/_archive/，孤儿 openclaw.db → data/_archive/，test_*.py → tools/_legacy_tests/，migrate_*.py → scripts/migrations/ | Claude |
| 2026-04-26 | scripts/ 分层：_archive/ 收纳 60+ 一次性 (w0_/debug_/dump_/_smoke_/test_gemini_) + setup/ 收纳安装脚本 | Claude |
| 2026-04-26 | README.md 重写顶部"我是..."导览（4 入口） | Claude |
| 2026-04-26 | 新增 F8 (uvicorn 直起) + F9 (PG UTF-8 错误) 故障字典 | Claude |
| 2026-04-26 | 新增 `config/launch.env.example` 模板 + `.gitignore` 加 `config/launch.env` | Claude |
| 2026-04-26 | start.ps1 加 dirty config 检查（git status config/） + launch.env 模板提示 | Claude |
| 2026-04-26 | 新增 `weekly_report.bat`（包装 phase8_funnel_report.py，输出 markdown 到 logs/reports/） | Claude |
| 2026-04-26 | scrcpy_manager.py 候选路径加 `vendor/scrcpy-server`；`scrcpy-server` 文件 git mv 到 `vendor/` | Claude |
| 2026-04-26 | `mobile-auto-project/` 历史子目录归档到 `docs/_archive_old_subproject/` | Claude |
| 2026-04-26 | F9 修复尝试 #1 — DSN 加 `options='-c lc_messages=C'`（**后被 linter/用户修正**: lc_messages 是 SUSET，普通用户改会被 PG 拒） | Claude |
| 2026-04-26 | F9 修复尝试 #2（最终）— DSN 只保留 `client_encoding=utf8`；中毒连接由 `_conn()` 的 UnicodeDecodeError catch + discard 兜底（Phase-13 已有）；真正根治需 PG superuser 跑 `ALTER ROLE openclaw_app SET lc_messages='C';` | victor + Claude |

## §7 — sibling Claude 协同事故图谱（2026-04-26）

**事故频率**：单次 Claude 长任务期间 victor 通过 GitHub PR squash merge 到 main，频率 ~7-20 min/次。事故 4 次，自救 4 次成功。

| # | 时间 | 事故 | 根因 | 修复方式 | 用时 |
|---|------|------|------|---------|------|
| 1 | 15:19 | 4fbee97 commit 误落 main | git checkout 自动切回 main + 我没察觉 + 没用 `branch_create.bat` | `git branch new + reset --hard HEAD~1` | 1 min |
| 2 | 15:26 | PR #112 squash 我 4fbee97 | sibling 期间合 PR 我不知情 | `git checkout -b stage-c main + cherry-pick 218f516` | 2 min |
| 3 | ~16:25 | PR #113 + sync_with_main -Rebase 撞 conflict | squash merge 让 rebase 重放原始 commits 撞 main 上 squash 内容 | `git rebase --abort + cherry-pick 806ada1 → stage-d` | 3 min |
| 4 | ~16:55 | PR #114 squash 我 581ba2a | 同 #2，工具盲区（git cherry 不识别 multi-commit squash） | `git checkout -b stage-e main + cherry-pick 5262c21` | 30 sec |

**防呆工具链演进**（每次事故学一个教训）：

| 事故 | 工具教训 |
|------|---------|
| #1 | `branch_create.bat` — 一键建 feat-* 分支防忘切 |
| #1 | `repo_health.bat` 加 main+dirty 警告 |
| #1 | `start.ps1` 启动前加 branch sanity |
| #2 | `sync_with_main.bat` — fetch + 报告 ahead/behind |
| #2 | CLAUDE.md 加 "sibling Claude 长任务前置检查" 章节 |
| #3 | `repo_health.bat -Fetch` — 显式 fetch + origin/main 对比 |
| #3 | `sync_with_main.bat -Rebase --AutoStash` — 处理 dirty |
| #4 | `sync_with_main.bat -CherryPick` — 用 git cherry detect squash |
| #4 | `sync_with_main.bat -CherryPick -AutoStash` — handle dirty |
| #4 | `cleanup_branches.bat` — 自动 detect obsolete branches |
| #4 | RUNBOOK §5 加 "F9 已应用 fix" |
| #4 | smart Recommendation：检测 squash → 推荐 -CherryPick 而非 -Rebase |

**已知工具盲区**：
- `git cherry origin/main HEAD` 只 detect 1:1 patch-id 等价
- 多个原始 commit squash 成 1 个 PR commit 时，patch-id 不匹配 → 工具不识别
- 用户手动判断时可用 `gh pr list --state merged --search "<branch>"` 看 PR 历史

### 🎯 突破性认知：git rebase 自带 patch-content detection（2026-04-26 实测发现）

**之前的错误认知**（第 1-4 次事故时）：
- "squash merge 让 rebase 重放原始 commits → 必撞 conflict → 必须用 -CherryPick"

**实测真相**（第 5 次事故 PR #115 期间发现）：
```
git rebase --autostash origin/main
  Rebasing (1/2)
  dropping c7a5f1f02b9082fa9fa9c794b4474f10665c1666 ... -- patch contents already upstream
  Rebasing (2/2)
  Applied autostash.
  Successfully rebased and updated refs/heads/feat-ops-stage-e-2026-04-26.
```

**原理**：
- `git cherry` 用 **patch-id**（hash of patch text）→ 1:1 匹配，multi-commit squash 不识别
- `git rebase` 用 **patch-content detection** → 检查 patch 应用后的 tree 是否已等于 upstream tree → **能识别 squash N→1 后的 already-upstream commits 自动 drop**
- 真正撞 conflict 的情况：main 和 branch 都修改了同一行，**不是因为 squash**

**新认知下的工具推荐顺序**：
1. **首选**：`sync_with_main.bat -Rebase -AutoStash`（git 自带智能，多数 squash 自动平稳处理）
2. **一键**：`sync_with_main.bat -Auto`（自动 fallback：rebase 失败 → cherry-pick）
3. **手动 fallback**：`sync_with_main.bat -CherryPick -AutoStash`（rebase 真撞 conflict 时用）

**从此事故学到的工程原则**：
1. **squash merge workflow + Claude long task = 反复事故**（结构性问题，不是 bug）
2. **防呆工具不能 100% 完美** — 但可以**降低事故修复成本**（5 次：3min → 30s → **0s 全自动**）
3. **每个 commit 前必跑 repo_health** — 工具链已让这成为反射动作
4. **加注 RUNBOOK** 比工具增强更重要 — 让人知道局限
5. **质疑权威认知，实测 > 推断** — git rebase 比想象的智能

> 之后每次改 `cluster.yaml` / `devices.yaml` / `task_execution_policy.yaml` / `facebook_playbook.yaml` / `launch.env` 留一行：日期 + 改了啥 + 谁。

---

## §6 — 日常巡检清单

- [ ] `status.bat` 输出 GO（每次开工）
- [ ] dashboard 能开 → http://localhost:8000/dashboard
- [ ] `/devices` 在线设备数 = 预期
- [ ] `logs/openclaw.log` 末尾 ERROR 无堆积（可 `status.bat` 看末尾 3 条）
- [ ] `logs/screenshots/` 不要爆磁盘（定期清）

---

> 看完本手册若发现疏漏 / 过期，**直接改本文件**。本手册的可信度 = 维护频率。
