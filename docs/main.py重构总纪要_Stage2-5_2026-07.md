# main.py 重构总纪要（Stage 2–5）

> 起止：2026-07-11 ~ 2026-07-13　|　目标仓库：`telegram-mtproto-ai`　|　配套：`REFACTOR_BLUEPRINT_main.md`（原始蓝图）

## 一、背景与目标

`main.py` 是一个 **4098 行的 God-file**，核心是 `AIChatAssistant`，其中 `initialize()` 单方法就 1125 行、`_maybe_start_companion_proactive()` 619 行——巨型方法内联了配置装载、日志、AI/技能/Telegram/RPA/设备/Contacts 初始化、FastAPI 后台装配、自动草稿、主动话题、生命周期等所有逻辑，难读、难测、难维护。

目标：在**不改变运行行为**的前提下，把巨石拆成清晰、可测、模块化的结构，并全程可回滚、可验证。

## 二、成果总览

| 指标 | 起点 | 终点 | 降幅 |
|---|---|---|---|
| `main.py` 总行数 | 4098 | **910** | **−78%** |
| `initialize()` | 1125 | **181** | −84% |
| 最大方法 | 1125 | 181（合理编排器） | — |
| 重构单测 | 0 | **42（全过）** | — |

- 抽出 **10 个可测模块**（见下表），全部经 编译 + 单测 + smoke/直启 验证；
- 全部提交已 **push 到 origin**，远端检查点最新；
- `initialize()` 现为"读配置 → 初始化服务 → 调各 `setup_*` → 起 web"的精简编排器，**不再是 God-method**。

## 三、抽出的模块清单

| 模块 | 内容 | 来源 |
|---|---|---|
| `src/bootstrap/cli.py` | `run_config_check` / `run_init` | Stage 1 |
| `src/bootstrap/env_probe.py` | `_is_desktop_mode` / `_telegram_configured` / `_resolve_mobile_auto_openclaw_db` | Stage 1.5 |
| `src/bootstrap/web_app.py` | `start_web_server_thread` / `start_monitoring_thread` / `make_api_auth` / **`setup_web_app`(527行)** | Stage 2 / 5 |
| `src/inbox/autosend_helpers.py` | `autosend_voice` / `autosend_image` / `build_autosend_callbacks`（deliver+translate） | Stage 2 |
| `src/inbox/autodraft_helpers.py` | `enrich_auto_draft` / `make_auto_draft_cb`(AutoDraftConfig) / `setup_auto_draft` | Stage 2 / 3 |
| `src/bootstrap/services.py` | `setup_contacts_subsystem` / `setup_device_management` / `setup_rpa_services` / `setup_telegram_clients` | Stage 3 |
| `src/companion/proactive_topic.py` | `maybe_start_companion_proactive`(619行整方法) | Stage 4 |
| `src/bootstrap/lifecycle.py` | `start_assistant`(188) / `stop_assistant`(139) | Stage 4 |
| `src/bootstrap/background_tasks.py` | 6 个 startup 辅助方法（proactive_care / reactivation_loop / deferred_outbox / warmup_embeddings / init_monetization / episodic_backfill） | Stage 4 |
| `src/bootstrap/logging_setup.py` | `setup_logging`(76行) | Stage 5 |

## 四、分阶段实施

- **Stage 2（闭包抽取）**：把 `initialize()` 内联的 FastAPI 装配/自动草稿/自动发送闭包逐簇抽成可测工厂——web 线程、监控线程、API 鉴权去重、autosend voice/image/deliver/translate、enrich、auto_draft_cb。亮点：`AutoDraftConfig` frozen dataclass 把 12 捕获收敛为 6 参并脱离 self，首次实现**行为级单测**。
- **Stage 3（顺序 setup 块）**：把 `initialize()` 里"同主题相邻块"打包成 `setup_*()`——auto_draft、Contacts(113)、设备三件套(52)、RPA 三件套(82)、Telegram(75, async)。
- **Stage 4（巨型方法）**：全局扫描发现并整方法搬迁 companion_proactive(619)、start/stop 生命周期(327)、6 个 background 方法。
- **Stage 5（深嵌套突破）**：做依赖地图分析，发现 `initialize` 内 527 行 web-app 块与 76 行 logging 块**仅捕获少数自由变量**，干净抽出。

关键提交：`be77fa2`(首簇) → … → `f5871bf`(web-app 527行) → `a44dd97`(logging 收尾)。

## 五、方法论（可复用）

1. **AST 自由变量守卫**：抽取脚本内置 AST 分析，自由变量超出"预期集合"就 `assert` 中止、绝不迁出——把"整块搬迁"上了自动安全闸（含 lambda 参数、comprehension、except-as 的正确绑定）。
2. **逐字迁出 + `self`→`assistant`**：主体一字不改，只做机械改名，行为零漂移。
3. **透传 wrapper**：main.py 留 `*args/**kwargs` 透传 wrapper，兼容任意签名（如带 `web_app=None` 的方法），保持类接口不变。
4. **分层验证**：`py_compile` → 单测（可测的写行为测试，难测的写导入/签名/早返守卫）→ smoke_boot（桌面模式隔离启动，端口就绪=块跑通）→ 对 smoke 有竞态盲区的（start/stop）加"直接全量启动 40s"验证。
5. **worktree 隔离 + rebase→FF→push 纪律**：在独立 worktree 重构，逐簇 rebase 到最新 main（零冲突）后 FF 合并、push；与用户并行编辑 main.py 互不干扰。

## 六、关键经验教训

- **"看起来难"≠"真的难"**：深嵌套的 web-app 块曾被判定为"高风险专项"，实测依赖地图后发现仅捕获 `web_cfg`+stdlib，一次干净抽出（单轮 −525 行）。**先测量、再下结论。**
- **数据驱动选目标**：给所有方法打 [CLEAN]/[MED]/[MANY] 标签，用数据锁定最高 ROI（如发现 start/stop 都仅依赖 asyncio），而非机械按顺序啃。
- **退一步看全局**：重构到瓶颈时全局扫描，发现 619 行的 companion god-method（比死磕深嵌套 initialize 更优）。
- **把调试痛点固化为工具**：一次 smoke 假失败后，加固 smoke_boot（端口等待 / `-u` 无缓冲 / 双流打印）。
- **并行编辑摩擦**：用户与助手并行改 main.py 多次造成 FF 撞车，靠 `merge-base` 诊断 + 经授权代提交 + rebase 化解。**建议：改 main.py 前先 commit 在途改动。**

## 七、遗留与后续

- `initialize()` 现 181 行，是**正当的启动编排器**，继续拆 ROI 已很低——重构告一段落；
- 深嵌套专项已完成（web-app / logging 均已抽）；
- 与本重构无关的历史遗留（P0 集群 secret/云端 Key 轮换、worker 密钥同步）见 `P0实施记录.md`，需在维护窗口/控制台操作。
