# Web 工作台与 Bot 引擎解耦——可行性评估（P2-2）

> 2026-08-12 可靠性复盘产物。结论先行：**现在不做全量进程拆分**——按实测数据
> ROI 为负；改走三个便宜得多的替代路径，并给出何时该回头重启拆分的触发条件。
> 本文引用的数字均可复查（来源标注在行内）；文档会过期，判断以代码与账本为准。

## 背景

单进程架构：`main.py` = FastAPI（web 线程）+ Telegram 协议端 + RPA runner +
autosend/care/proactive 循环 + 各 watchdog。任何 `.py` 改动都要整进程重启，
重启窗口内工作台全站不可用（坐席看到维护蓝条/红条）。

问题：值不值得把 Web 工作台拆成独立进程，让「重启 bot 不断工作台」？

## 实测数据（2026-08-12）

| 指标 | 值 | 来源 |
|---|---|---|
| 重启全断窗（kill → /login 200） | 13s / 22s（两次实测） | `restart_cooldown/last_restart_*.json` 的 `window_sec` |
| 进程启动到就绪 | ≈12s（其中 import+store ≈7s、`ai_client=4.3s`、其余各 <0.5s） | `logs/boot_20260812_124025.out.log` 的 `boot phases` 行 |
| 重启频率基线 | 16 天均值 8.4 次/天，88% 为开发批次 | `restart_events.jsonl`（223 条） |
| P0-3 窗口制度后的上限 | ≤3 次/天（04:00/12:30/22:30 ±45min） | AGENTS/CLAUDE 重启纪律 + preflight advisory |
| 重启的**真实**日停机 | ≤3 × ~20s ≈ **1 分钟/天 ≈ 0.07% 可用率** | 上两行相乘 |
| 本周真实损失结构 | 隧道僵尸事故 ~18min（已治本）≫ 重启 ~3min | edge watchdog 日志 + entrance_slo |
| SLO 口径的重启税虚高 | 5 分钟采样把 20s 窗记成 5min（放大 ~15×） | `entrance_slo` 卡片 hint 有同款说明 |

## 耦合面盘点（拆分的真实成本）

- 104 个路由文件中 **53 个直接消费 `app.state`**（进程内共享对象：stores、
  worker、coordinator、config_manager…）；**11 个直接调用 orchestrator**
  （发送/RPA 控制类端点）。
- 拆分即把这 53 个文件的状态来源改成「跨进程 DB 读 + RPC 写」：SQLite 多进程
  写并发、进程内单例缓存（TTL 缓存、dedup 表、stats 单例）全部要重新设计；
  发送链（send/send-media/send-voice/RPA 动作）要新建 IPC 层。
- Telegram 会话是**单属主**（session 文件锁 + 双进程同答风险）——bot 侧永远
  只能单实例，蓝绿只可能发生在 Web 层，而 Web 层单独蓝绿的前提恰是先完成
  上面的拆分。
- 估算工作量：数周级 + 永久性的双进程运维负担（本机已有十余个计划任务/看门狗）。

## 结论与推荐

**不拆。** 拆分买回的是每天 ~1 分钟的计划内停机，成本是数周工程 + 新增故障
模式。当前可用率的真实敌人（隧道僵尸、办公室线路、无人知晓的断连）已在
P0/P1 用远低的成本处理掉了。

### 替代路径（已做/待做）

1. **已做（P0-3）**：重启窗口制度（频率封顶）+ 停机前维护预告播（体验缓冲）+
   草稿逐键快照/发送幂等（数据安全网）。
2. **已做（P3，2026-08-12 同日落地）**——实测数据把原「web-first 排序」候选
   拆成了三个更小的动作，全部完成：
   - **启动探针后台化**（`ai_client.initialize(defer_probe=True)`，仅 main 冷启动
     传参）：boot phases 实测探针 4.3s = init 段最大单项，而 assistant.initialize
     从不消费其返回值；后台任务保留日志/坏 key 告警。`reload_ai_runtime`
     （桌面模式切换 UX 消费成败布尔）不传参数，阻塞语义原样。
     门禁 `tests/test_ai_client_boot_probe.py`（5 例）。
   - **`main.py` 顶层死 import 移除**：`TelegramClient` 顶层 import 是死代码
     （构造点 services.py 自己函数内 import），却把 pyrogram ~5s（raw.types
     生成）挡在进程第 0 秒。移除后 `import main` 实测 **7.2s → 1.6s**；pyrogram
     成本落回 telegram_clients 阶段（该阶段本来就需要它），boot 归因变诚实。
   - **`vision_client` 的 zhipuai 懒加载**：顶层 import 实测 ~1.0s，而智谱云兜底
     自 07-12 key 失效起停用——所有 import 该模块的进程（boot/worker/CLI）都在
     为死路径买单。改 `_initialize_zhipu` 内按需 import（未配 key 连 import 都
     不付）。
   - 预期合计：冷启动 ~12s → **~7s**，全断窗 13-22s → **~8-16s**。
     以 22:30 窗口装载后的 `boot phases` 日志行验收。
3. **P4 候选（本轮解锁，暂缓）**：web-first 排序——runway 清理后 pre-web 阶段
   只剩 `openai` import 1.3s + config 0.2s，把 `setup_web_app` 提到 ai_client 之前
   理论上 /login ≈ 2-3s 可服务。动手前置：审计 `create_app` 构造期消费的
   assistant 属性清单（skill_manager/contacts/stores 为 None 时各路由的 fail-soft
   完备性）。等 P3 验收数字出来再定值不值。

### 回头重启拆分的触发条件（满足任一即重评）

- `entrance_slo` 的坐席端 `conn_*` 数据显示重启窗口造成的中断回执占比可观
  （体感真值，非 5 分钟采样虚高口径）；
- 开发节奏使重启无法稳定压在 ≤3 次/天；
- 租户 SLA 承诺进入 ≥99.9% 区间（0.07% 的重启税开始占预算的大头）。

## 附：为什么不是「蓝绿双实例」

蓝绿的换血单元是整进程，而 Telegram 会话单属主使 bot 侧无法双活；
「Web 蓝绿 + bot 单例」= 先完成整个拆分再谈蓝绿，回到同一个 ROI 问题。
portproxy 前置（P0-1 已就位）让**将来**的 Web 蓝绿有现成的流量切换点，
这是本次评估留下的免费选项，不是现在动工的理由。
