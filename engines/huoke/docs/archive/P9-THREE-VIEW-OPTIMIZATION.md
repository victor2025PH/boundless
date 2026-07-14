# OpenClaw P9 三视角全盘优化方案

> 本文以 **开发工程师 / 市场经理 / 最终用户** 三种视角审视 OpenClaw 现状，
> 并给出 **P0（立刻做）/ P1（2 周内）/ P2（1 季度内）** 三级路线图。
> 同期 P0 动作已在本次提交落地，见文末"已落地清单"。

---

## 一、项目现状速览

| 维度 | 现状数据（估） | 关键文件 |
|---|---|---|
| 代码体量 | `src/host` 近 70 个 Python 模块 + 15+ 前端 JS | `src/host/`, `static/js/` |
| 部署形态 | 1 台主控 + N 台 Worker PC + 30~200 台手机（真机 ADB）| `multi_host.py`, `w03_event_bridge.py` |
| 业务目标 | 在 TikTok / Facebook / Telegram / WhatsApp / LinkedIn / IG / X 上做**精准获客 + AI 私信转化** | `routers/platforms.py` 的 7 大平台配置 |
| 核心机制 | FastAPI 路由 → SQLite 存任务 → WorkerPool 调度 → executor 分发到真机 | `api.py → task_store.py → worker_pool.py → executor.py` |

---

## 二、本次故障根因（先把"为什么养号任务不跑"讲清楚）

### 现象

某台设备（如 `4HUSIB4T…`）控制台状态"空闲"，但后端有 13 条 `facebook_*`
pending 任务；而另外两台同时在跑。

### 根因（2 条并存）

**A. AI 快捷指令只 `create_task` 不 `pool.submit`（最常见）**

`routers/ai.py` 里 `ai_execute_intent` 和 `ai_quick_command` 两处直接调
`task_store.create_task`，没有后续入池。这类任务依赖手机 agent 主动拉取；
如果该手机没在轮询，就永远挂在 `pending`。历史上 13 条 FB 任务多来自
"AI 快捷指令 + 批量设备"组合。

**B. `get_retry_ready_tasks` 是孤儿函数**

`task_store.py:498` 有函数，全仓库没有调用方。失败任务被写成
`pending + next_retry_at`，没人扫，重试消失。

> 另外还有两条**次级诱因**：
>
> - `risk_auto_heal._enqueue_warmup` 风控降级任务也只 create 不 submit；
> - 进程重启时 WorkerPool 是内存态，任何还未完成的任务如果没有兜底扫描就会"丢内存"。

### 修复（已落地）

1. **统一派发入口** `src/host/task_dispatcher.py::dispatch_after_create`
   把"集群路由 → 本机 pool.submit"的流程抽出来，四个入口统一复用：
   - `routers/tasks.py` POST /tasks
   - `routers/ai.py` AI 快捷指令（2 处）
   - `risk_auto_heal.py` 风控降级
2. **pending 救援循环** `start_pending_rescue_loop`：
   每 15s 扫描 `pending + updated_at > 120s + 不在 futures` 的任务，
   自动补一次 `dispatch_after_create`。同时兜底 `get_retry_ready_tasks`。
3. **只读诊断脚本** `scripts/diagnose_stuck_tasks.py`：
   PM/运维可以随时跑，五步定位某台设备的 pending 根因。

---

## 三、开发工程师视角

### 3.1 代码健康度

| 项 | 现状 | 问题 |
|---|---|---|
| 路由数 | `api.py` 注册 35+ 个 router | 路由膨胀但 lifespan 里 17 个 try/except 导致启动错误被静默。 |
| 跨入口逻辑复制 | `POST /tasks`、`ai.py`、`risk_auto_heal.py`、`routers/platforms.py` 各自写一份派发 | 修 bug 要改 4 处，容易漏（本次就漏了两处）。 |
| 前端状态源 | `TASK_NAMES`（JS）、`PLATFORMS[*].label`（PY）、`_QUICK_TASK_NAMES`（PY）| 中文名三套真源，改一个地方其他不更新。 |
| 配置散落 | 手机列表在 `config/devices.yaml` + `docs/抖音/手机列表.yaml` + `chat.yaml` 别名 | 新增一台设备要改 3 个文件。 |
| `.bak` 残留 | `static/js/dashboard.js.bak`、其他若干 | 前端热加载偶尔加载到旧版本。 |
| 测试覆盖 | `scripts/smoke_*` + `tests/` 零散 | 没有"create_task 必定 submit"的契约测试，这次的 bug 才能存在半年。 |

### 3.2 P0（已落地或强烈建议 1 天内完成）

- [x] 抽 `task_dispatcher.dispatch_after_create` + `start_pending_rescue_loop`（本次提交）
- [x] `task_labels_zh.py` 单一中文标签源（本次提交）
- [x] `GET /tasks/meta/labels`、`GET /tasks/today-funnel` 两个只读诊断 API
- [ ] **契约测试**：`tests/test_task_dispatch_contract.py`，断言每个已知入口 create 后必然进 WorkerPool `_futures`
- [ ] 删除所有 `.bak`（前需要 `git tag pre-p9-cleanup` 做保险）

### 3.3 P1（2 周内）

- **Router 拆包**：`api.py` 拆成 `api.py(core) + lifespan.py + router_registry.py`，
  lifespan 的 17 个 try/except 抽成可测的 `startup_steps`，每一步名字化后可在
  `/health/startup` 看谁起没起来。
- **SQLite → 配置化** 当前绑死 SQLite；上生产集群前改为 SQLAlchemy 抽象，
  单机继续 SQLite，主控/共享库切 PostgreSQL。`task_store` 的 `get_conn()` 是
  唯一连接入口，替换风险可控。
- **结构化日志**：所有 `[rescue] / [dispatch] / [pool]` 前缀统一成 `event_type` 字段，
  用 `structlog` 或简单的 `logging.LoggerAdapter`，然后前端"今日漏斗"直接 query 日志。
- **前端 i18n 文件化**：把 `task_labels_zh._OVERRIDES` 下沉成
  `config/i18n/tasks.zh.yaml`，后端启动读取；增加 `en.yaml` 做第二语言准备
  （海外同事协同一定会要）。
- **WebSocket 收敛**：`w03_event_bridge`、`streaming.py`、`ws_routes.py` 各自一个 WS 命名空间，
  前端 `core.js` 维护了好几条连接。统一到 `event_stream` 里做扇出。

### 3.4 P2（1 季度）

- **模块化插件**：`facebook_*` / `tiktok_*` 现在是硬编码在 executor 的 if/elif 链。
  改成每个平台一个 `Plugin` 类（`resolve_task(task_type, params) -> Action`），
  在启动时扫描 `src/platforms/*.py` 动态注册，新接一个平台不用改核心。
- **前端 SSR → SPA 或至少改组件化**：现在 `dashboard.py` 把 2800 行 HTML 拼字符串，
  静态 JS 也是一堆全局函数。用 Vite + Vue3/React 重写控制台；先做 **"任务中心"
  一个页签**作为第一步，和旧 SSR 并存跑一个月再整体切换。
- **策略与业务解耦**：`task_policy.py` 的 `manual_execution_only` 等一堆开关
  散在 lifespan，抽成 `class ExecutionPolicy` 单例，执行器任何分岔点都问它
  `policy.allow_auto_inbox()`；新增策略不用再去各处 try/except。

---

## 四、市场经理视角

> "我在拿这套系统跑意大利 30+ 男性的 TikTok 获客，我关心的是**今天跑了几条、下单了几个、账号安不安全**。"

### 4.1 现状痛点（按 ROI 倒序）

1. **漏斗看不见**。总览能看 N 台设备 / N 条任务，但**"今天我的 LTV 漏斗是什么"**
   完全要打开 CRM 看。
2. **风控看不见直接后果**。风控降级现在只在日志里说"已为 4HUS.../facebook 排入降级
   任务"，市场不知道 —— 对外没通知、对内没 Badge。
3. **成本看不见**。Gemini/DeepSeek/Ollama 调用、VPN 流量、每台手机实际工时都有
   数据，但没有一个"今天花了多少钱 / 每单多少钱 / 单位 ROAS"的单页。
4. **A/B 看不见结论**。`strategy_optimizer.py` 写了一堆自动调参，但市场看不到
   "A 版本 vs B 版本"差多少，也没法停掉某个 A/B。
5. **时段与地域不对齐**。任务默认 UTC；市场脑子里是罗马时间。`smart_schedule`
   里有 timezone，但仪表盘不显示。

### 4.2 P0 建议（本周）

- [x] 已加 `GET /tasks/today-funnel`，下一步前端做一张 **"今日漏斗卡"**：
  **创建 → 待发 → 运行中 → 成功 / 失败 / 被卡 >5min**，点击卡片能筛任务中心。
- [ ] **风控事件** 在 dashboard 顶部做 Toast + 徽章，复用 `alert_notifier`
  已有的 telegram channel。
- [ ] **AI 成本** 接一张 `/ai/stats` 的只读卡（token / 请求数 / 估算美元）。

### 4.3 P1（2 周内）

- **单条线索的生命周期视图**：把"获取 - 关注 - 回关 - 私信 - 转化"串起来，
  复用 `leads` + `campaigns` + `conversations` 三张表。需要补一个 cron：
  `scripts/rollup_lead_funnel.py` 每 15 min 聚合一次，避免前端每次扫全表。
- **A/B 结果面板**：在 experiments 路由基础上做一个 `GET /experiments/summary`，
  返回每个实验的 p95、转化率差、CI；前端出柱状图。允许**一键发布获胜版本**、
  **一键回滚**、**一键暂停**。
- **市场运营 Playbook**：
  - 意大利时段（Europe/Rome 09:00-11:30 / 19:00-22:30）自动 × 1.2 速率，
    午休 12:00-14:00 × 0.3；
  - 美国时段直接读 `config/global_growth_roadmap.yaml` 配置好提前注入。
- **每日日报**自动发 Telegram：设备在线 / 创建任务 / 完成率 / AI 成本 / Top 5 线索。

### 4.4 P2（1 季度）

- **多主控控制台**：Worker-01 意大利市场、Worker-02 美国市场，上一层
  Dashboard 做"跨市场对比"。Federation 的地基 `openclaw_agent.py` 已有，
  只是上层视图还没画。
- **用户分级运营**：线索进站后按"画像分数 × 行业"自动分配话术模板，
  AI 写三条建议回复（已有 `/ai/suggest-reply`），再人工一秒钟定稿发送。
- **KPI 仪表盘 对外分享链接**：给老板 / 渠道方一个只读链接，带水印。

---

## 五、最终用户视角

> 这里的"最终用户"是日常做 30~200 台手机矩阵运营的**操作员**（通常 1~3 人），
> 和业务本身的目标客户（TikTok / Facebook 上的意大利男性）是两个人。

### 5.1 操作员看到的问题

| 场景 | 现状 | 想要 |
|---|---|---|
| 某台手机卡 pending | 看不出来为什么，只能问开发 | **看到"⏸ 排队超过 7 分钟未执行（等待救援）"**（已做） |
| 要给 30 台手机一键挂 VPN | `/vpn/reconnect-all` 需要手动按 | 首页"快速操作"里做一个大按钮 |
| 想看某台手机最近一小时都干了啥 | 要开详情、翻日志 | 设备卡片展开就能看最近 10 条任务时间线 |
| 凌晨任务失败没人知道 | 失败就失败，日志里躺着 | Telegram 告警 + 明早一开 dashboard 就看到红色 badge |
| 投屏每次都要敲命令 | `scripts/start_scrcpy_second_screen.ps1` | 设备卡片右键"投屏到副屏" |

### 5.2 P0（本周）

- [x] **任务中心"卡在哪儿"提示**（`stuck_reason_zh`）已做。
- [x] **所有任务名中文化**（`TASK_NAMES` + `type_label_zh`）已做。
- [ ] 设备卡片右键菜单统一：`投屏 / 查看最近任务 / 立即养号 30 分钟 / 断网重连`。
- [ ] `scripts/diagnose_stuck_tasks.py` 挂一个前端按钮"诊断这台设备"，
  后台直接调脚本把结果显示在 modal 里。

### 5.3 P1（2 周内）

- **"手机状态地图"**：一张大屏，每台手机一个方块，颜色表示状态
  （绿 = 运行中 / 黄 = pending / 灰 = 离线 / 红 = 风控告警），
  点一下弹详细 + 实时缩略图（已有 `digest` 接口）。
- **一键剧本**：把"开 VPN → 启动 TikTok → 刷 15 分钟 → 关注 20 人 → 查收件箱"
  做成一个可视化编辑器（workflow 里有引擎基础），保存成模板给运营直接拖。
- **账号矩阵风险看板**：每个账号一条"安全血条"，用 `risk_auto_heal` 的历史
  打分，连续触发降级后血条红。血条低于 30 的账号自动从批量选择里排除。
- **导入导出线索**：CSV 导入 + Google Sheets 同步（`routers/crm_sync.py` 有雏形），
  让市场可以把自己的线索直接塞进来打私信。

### 5.4 P2（1 季度）

- **移动端控制台**：PWA 已经有骨架（`routers/pwa.py`），做成"手机扫一眼就知道
  所有机器状态"的轻量版，在外地也能看。
- **离线回放**：任务跑完录制一段"我做了啥"的 UI 操作脚本，失败时可以原地重播，
  大幅降低排查成本。`scripts/smoke_*` 里已有关于 UI dump 的基础。
- **自然语言控制**：`/chat` 已经能把"给所有手机养号 30 分钟"翻译成任务，
  下一步是从"控制层"升级到"策略层"，
  比如"本周转化率偏低，帮我查原因并推荐 3 个动作"。

---

## 六、已落地清单（本次提交）

| 文件 | 动作 | 作用 |
|---|---|---|
| `scripts/diagnose_stuck_tasks.py` | 新增 | 只读诊断 pending 堆积根因 |
| `src/host/task_dispatcher.py` | 新增 | 统一派发入口 + pending 救援循环 |
| `src/host/api.py` | 修改 | lifespan 启停救援循环；`_to_response` 注入 `type_label_zh` |
| `src/host/schemas.py` | 修改 | `TaskResponse` 补 `type_label_zh / stuck_reason_zh` |
| `src/host/task_ui_enrich.py` | 修改 | 新增 `_stuck_reason_zh` 产出卡住原因 |
| `src/host/task_labels_zh.py` | 新增 | 中文标签单一真源（合并 platforms + overrides）|
| `src/host/routers/tasks.py` | 修改 | 新增 `/tasks/meta/labels`、`/tasks/today-funnel`；`POST /tasks` 改走 dispatcher |
| `src/host/routers/ai.py` | 修改 | AI 快捷指令两处补 dispatcher；删除 `_QUICK_TASK_NAMES` |
| `src/host/risk_auto_heal.py` | 修改 | 降级任务也走 dispatcher |
| `static/js/core.js` | 修改 | `TASK_NAMES` 改 `let`；启动拉 `/tasks/meta/labels`；暴露 `taskDisplayName` |
| `static/js/tasks-chat.js` | 修改 | 列表行优先用 `type_label_zh`；pending 行显示 `stuck_reason_zh` |

---

## 七、验证步骤（把 13 条 FB 任务跑起来）

```bash
# 1. 诊断 4HUS 设备
python mobile-auto-project/scripts/diagnose_stuck_tasks.py --device 4HUS

# 2. 重启 host 进程，观察日志里的 pending_rescue_loop 启动行：
#    [rescue] pending_rescue_loop 启动，间隔 15s，批量 100，孤儿阈值 120s

# 3. 约 2-3 分钟后再次诊断，应看到 pending 数下降、resubmit 日志：
#    [rescue] scanned=13 resubmit=13 cost=1.23s
#    [dispatch] task=abcd1234 → local pool device=4HUSIB4T

# 4. 前端「任务中心」所有列表项现在是中文（Facebook 浏览我的群组 / FB 群组互动 等），
#    老的 pending 行会显示橙色 ⏸ 提示"排队超过 N 分钟未执行（等待 pending 救援补派）"。
```

---

## 八、风险与回滚

- **pending_rescue_loop 误派**：我们只在 `updated_at > 120s` 且不在 `_futures` 时重派，
  不会抢刚创建的任务。若出现"任务被跑两次"，把 `_ORPHAN_AGE_SEC` 调到 300s；
  再极端时 `stop_pending_rescue_loop()`（立即生效，见 lifespan 结束段）。
- **前端标签字典拉取失败**：`refreshTaskLabels()` 吞异常，保留旧兜底 `TASK_NAMES`，
  不会白屏。
- **回滚**：全部改动都在 git 上，救援循环用 `--patch` 粒度可单独回退；
  建议先 `git tag p9-three-view-optimization` 再继续。
