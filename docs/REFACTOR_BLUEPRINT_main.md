# main.py 拆巨石重构蓝图（telegram-mtproto-ai）

**日期**：2026-07-11
**对象**：`telegram-mtproto-ai/main.py`（重构前 4098 行 → Stage 1 后 4022 行）
**原则**：行为不变、分阶段、每阶段可独立验证、每阶段可回滚。
**验证工具**：`python main.py --check`（轻量，仅体检配置退出）+ 全量 `pytest`（重量，需隔离环境）。

---

## 一、现状结构（基于真实行号盘点）

| 区块 | 行号 | 规模 | 性质 |
|---|---|---|---|
| 模块级纯helper `_resolve_mobile_auto_openclaw_db`/`_telegram_configured`/`_is_desktop_mode` | 37–108 | ~70 | 纯函数，易抽 |
| `AIChatAssistant.__init__`（~30 个状态属性） | 112–160 | ~50 | 状态容器 |
| **`initialize()`** | 161–2058 | **~1900** | God-method，含大量嵌套闭包 |
| ├─ 日志/配置/AI/skill/RPA 构建 | 161–540 | ~380 | 组件装配 |
| ├─ Contacts 子系统 + Mobile Bridge | 541–760 | ~220 | 可选子系统 |
| ├─ **内联 FastAPI 应用工厂**（autosend voice/image、草稿 enrich、contacts api、web-in-thread 等闭包） | 761–1990 | **~1230** | 最该抽 |
| └─ 收尾装配 | 1991–2058 | ~70 | |
| `start()` | 2059–2247 | ~190 | 启动编排 |
| ~40 个 `_maybe_*/_ensure_*/_init_*/_periodic_*` 子系统装配方法 | 2248–3815 | ~1570 | 可选子系统装配 |
| `stop()` | 3816–3955 | ~140 | 优雅停止 |
| 信号处理 | 3956–3966 | ~10 | |
| ~~`run_config_check`/`run_init`~~（CLI） | ~~3967–4043~~ | ~~77~~ | **Stage 1 已抽走** |
| `main()` + `__main__` argparse | 3971–4022 | ~50 | 入口 |

---

## 二、目标包结构 `src/bootstrap/`

```text
src/bootstrap/
├── __init__.py        # 已建：包说明
├── cli.py             # 已建 (Stage 1)：run_config_check / run_init
├── web_app.py         # Stage 2：FastAPI 应用工厂（从 initialize 抽出）
├── subsystems.py      # Stage 3：可选子系统装配（_maybe_*/_ensure_*/_init_*）
└── lifecycle.py       # Stage 4：initialize/start/stop 编排骨架（可选）
```

---

## 三、分阶段计划

### Stage 1 — CLI 抽取 ✅ 已完成（2026-07-11）

- 动作：`run_config_check`/`run_init` → `src/bootstrap/cli.py`，main.py 改为 import。
- 验证：`py_compile` OK；隔离 import OK；`python main.py --check` 与基线**逐字一致**（exit 0，0 错误/1 警告）。
- 风险：极低（仅 `__main__` 调用，不触碰 initialize/start）。故可在**服务运行中**安全落地。

### Stage 2 — FastAPI 应用工厂抽取（最大收益，最高风险）

- 动作：把 initialize() 761–1990 的内联 `create_app` + 所有嵌套闭包（autosend、draft enrich、contacts api、web-in-thread）抽成 `web_app.py::build_web_app(assistant, ...)`，返回 `(app, thread_starter)`。
- 难点：这些闭包**大量捕获 `self.*` 与局部变量**。抽取需把依赖显式化为函数参数或传入 assistant 引用。
- 验证：**必须跑全量 pytest**（web/inbox/drafts/autosend 相关用例最密集），`--check` 不足以覆盖。
- 风险：高。**不建议在服务运行中做**——见第四节隔离方案。

### Stage 3 — 子系统装配抽取

- 动作：~40 个 `_maybe_*/_ensure_*/_init_*/_periodic_*` 按域分组迁到 `subsystems.py`（如 care/companion/quality/trend/episodic 各一组），main 里改为调用。
- 验证：全量 pytest + 各 feature flag 开关组合的冒烟。
- 风险：中（多为可选、feature flag 默认关，但彼此有启动顺序依赖）。

### Stage 4 — lifecycle 骨架（可选，收尾）

- 动作：把 initialize/start/stop 的编排顺序抽成 `lifecycle.py` 的显式步骤列表，main 只保留 `AIChatAssistant` 状态 + 委托。
- 验证：全量 pytest + 一次真实重启冒烟。

---

## 四、Stage 2–4 的安全执行方案（关键）

这些阶段改动运行时装配，**`--check` 无法验证**，必须跑全量 pytest；而当前 telegram 主程序在跑，直接在主工作树跑全量测试会与线上**抢 SQLite/端口**。故推荐 **worktree 隔离**：

```powershell
# 1) 建独立 worktree（不影响线上主工作树/在跑服务）
cd D:\workspace\telegram-mtproto-ai
git worktree add ../_wt-refactor-main -b refactor/main-bootstrap

# 2) 在 worktree 里重构 + 跑全量回归（独立目录、独立 test DB）
cd ../_wt-refactor-main
python -m pytest tests/ -n auto -q     # 基线先跑一遍，确认全绿
#   ...实施 Stage 2/3/4，每步再跑 pytest 对比...

# 3) 全绿后合回，再择维护窗口重启线上
```

- 每阶段一个 commit，`pytest` 全绿才进下一阶段；失败即 `git reset` 回滚。
- 合并后需一次**维护窗口重启**验证真实启动（--check 之外的 initialize/start 路径）。

---

## 五、收益预估

| 阶段 | main.py 预计行数 | 累计降幅 |
|---|---|---|
| 重构前 | 4098 | — |
| Stage 1 ✅ | 4022 | -2% |
| Stage 2 | ~2800 | -32% |
| Stage 3 | ~1300 | -68% |
| Stage 4 | ~600 | -85% |

目标：main.py 从 4098 行的 God-file 收敛到 ~600 行的"状态 + 编排委托"骨架，其余按职责分布到 `bootstrap/` 子模块，显著提升可 review 性与新人上手速度。

---

## 六、结论与建议

- Stage 1 已安全落地并验证（服务运行中即可做的那类）。
- **Stage 2–4 属于"必须隔离 + 全量测试 + 维护窗口重启"的大改**，建议单独立项、在 worktree 分支上小步推进，不与日常运行混做。
- 优先级建议：先完成 P0/P1 的安全收尾（见《维护窗口执行手册》），再启动 Stage 2。

---

## 七、Stage 2 启动状态（2026-07-12）

### 已完成的铺垫

| 项 | 状态 |
|---|---|
| worktree 隔离环境 | ✅ `D:\workspace\_wt-stage2-webapp`，分支 `refactor/main-stage2-webapp`（基于含 Stage1 的 main，不影响在跑服务） |
| pytest 全量基线 | ✅ **7997 passed / 2 failed / 37 skipped**（14m37s，8036 collected） |

**基线的 2 个预存失败**（非本次重构所致，HEAD 上已存在）：
- `tests/test_send_path_audit.py::test_no_unexpected_raw_client_sends`
- `tests/test_send_path_audit.py::test_no_stale_allowlist_entries`
- 现象：发送点审计白名单与 sender 代码不同步。**疑与主工作树的 sender 在途重构有关**。Stage 2 验收时以"仍是这 2 个、无新增失败"为通过标准。

### ⚠️ 阻塞：Stage 2 抽取暂不能做

深挖发现：主工作树 `main.py` 有 **11 段未提交的在途工作，落在 `initialize()` 的 1055–1866 行**，而 Stage 2 要抽取的内联 FastAPI 工厂在 **761–1990 行**——**完全重叠**。

若现在在 worktree（基于 HEAD，不含在途工作）抽工厂，等在途工作提交后，两边改同一区域**必然合并冲突**（重构正被编辑的代码）。

**结论**：Stage 2 的实际抽取**必须等主工作树那 11 段在途工作先提交/合并**，再在 worktree 基于最新 main 进行。当前 worktree + 基线已就绪，届时可直接开工。

### Stage 2 抽取待办（在途工作落地后执行）

1. worktree `git rebase origin/main`（取到含在途工作的最新 main）。
2. 抽 `initialize()` 的 create_app + 嵌套闭包（autosend voice/image、draft enrich、contacts api、web-in-thread）→ `src/bootstrap/web_app.py::build_web_app(assistant, ...)`，把闭包捕获的 `self.*`/局部依赖显式化为参数。
3. 每步跑全量 pytest，对照基线（≤2 failed、无新增）。
4. 全绿后合并 + 维护窗口重启验证真实启动路径。

---

## 八、Stage 2 已解阻塞 + 就绪（2026-07-12 更新）

### 阻塞已解除

- 主工作树的在途工作已全部提交并 push 到 origin/main（`10dc0b2`/`f7753c7`/`a97dd01`）。
- worktree `_wt-stage2-webapp` 已 `reset --hard main`，现基于含在途工作的最新 main（`a97dd01`）。
- **验证基线（新）**：worktree 全量 pytest = **8312 passed / 38 skipped / 0 failed**（15m33s）。之前的 2 个 send_path_audit 失败已修复。Stage 2 每步验收标准：**≤0 failed、无新增失败**。

### 当前工厂定位（a97dd01 行号）

- `create_app` 本体已在 `src/web/admin.py`（main.py L691 import、L733 调用）。
- 待抽取块 = `initialize()` 内 **L733–~2000 的 `web_app.state.*` 装配 + 嵌套闭包**：
  - autosend 簇：`_try_autosend_voice`(857) / `_try_autosend_image`(1052) / `_autosend_deliver`(1170) / `_autosend_translate`(1275)
  - draft：`_enrich_auto_draft`(1470)、`_drafts_api_auth`(811)
  - contacts：`_contacts_api_auth`(1955)
  - 各后台 worker 注入：sla_watcher/auto_claim_worker/webhook_notifier/health_watchdog/scheduled_reporter/chat_assistant_service/translation_service/ecommerce_tools 等
- 目标：抽成 `src/bootstrap/web_app.py::wire_web_app(assistant, web_app, ...)`，闭包捕获的 `self.*`/局部依赖显式化为参数。

### 执行方式（迭代，非一次性）

这是 ~1200 行深度嵌套闭包的精细重构，须**逐簇抽取、每簇验证**（快速档：`python main.py --check` + 定向 web/inbox 测试 ~2min；里程碑档：全量 pytest ~15min），全绿再进下一簇。**不在超长轮次末尾仓促整体抽取**，以免半成品破坏会自动重启的线上服务。worktree + 基线已就绪，可随时逐步开工。

---

## 九、进展与关键可验证性发现（2026-07-12）

### 已完成

- **Stage 1**（`cli.py`）✅ 已合入 main 并 push。
- **Stage 1.5**（`env_probe.py`：`_resolve_mobile_auto_openclaw_db`/`_telegram_configured`/`_is_desktop_mode`）✅ 本次完成，commit `395eec2` 已 push。验证：py_compile + `--check` + **`test_desktop_boot_gate.py` 12 passed**（这 2 个 helper 有测试覆盖）。main.py 4022→3977。

### ⚠️ web 工厂（Stage 2 主体）的可验证性障碍（决定执行方式）

核查确认：`initialize()` 的 ~1200 行 web 装配块 **既不被 pytest 覆盖、也不被 `--check` 执行**：
- web 测试用的是 `src/web/admin.create_app`，不经过 main.py 里的装配闭包；
- `--check` 只加载模块 + 跑 `run_config_check`，不进入 `initialize()`；
- 唯一能真正验证这块的是**完整启动 `python main.py`**——但那会与在跑的线上服务抢端口/DB，且需要 config。

**因此**：Stage 1.5 这类「有测试覆盖/纯函数」的抽取可安全增量做并已做；而 web 工厂主体**不能在当前环境盲抽**（漏一个闭包捕获，只有下次真实重启才暴露 → 崩线上）。两条安全路径二选一：
1. **维护窗口**：停服后抽取 + 真实 `python main.py` 启动验证 + 重启。
2. **隔离 smoke 启动器**：在 worktree 用桌面模式（`AITR_DESKTOP_MODE=1` 跳过 Telegram）+ 备用端口 + example config，做一个「启动到 initialize 完成即退出」的冒烟脚本，让这块变得可自动验证，再逐簇抽取。

---

## 十、Stage 2 执行中（2026-07-12，方案 2 落地）

**smoke 启动器已建**：`scripts/smoke_boot.ps1`（桌面模式 + 端口 18787/19190，启动到 initialize() 完成即 PASS，~10-30s，与线上零冲突）。每簇抽取都用它验证。

**已抽取簇（refactor/main-stage2-webapp 分支，已 rebase 到最新 main，各簇 smoke PASS）**：
| 簇 | 抽到 web_app.py | 验证 |
|---|---|---|
| web 服务线程启动 | `start_web_server_thread(assistant, server, host, port)` | smoke PASS |
| 监控 API 线程启动 | `start_monitoring_thread(assistant)` | smoke PASS |
| API 鉴权闭包（去重 2→1） | `make_api_auth(web_app)` | smoke PASS |

main.py：4098（起点）→ **3909**（worktree）；`bootstrap/web_app.py` 101 行。

**已合并回 main**：3 簇（web线程/监控线程/鉴权去重）2026-07-12 已 rebase 到最新 main（含 P0/P1 集成，零冲突）+ smoke PASS + FF 合并进本地 main（`cddc378`）。⚠️ 本地 main 领先 origin 24 提交（含用户 P0/P1 集成 20），**未 push（待用户决定）**。

**autosend 簇（794-1242 区，~450 行）—— 暂缓盲抽，需测试化改造**：
深挖发现它**不能用 smoke 安全验证**：
1. 整块包在 `try/except: logger.debug("AutosendWorker 启动跳过")` 里 —— 抽取 bug 会被**静默吞掉**，smoke（看 web 绑定）仍 PASS；
2. 4 个闭包在 `if _deliver:` 内，example 配置 `deliver=False` → smoke **根本不执行**这些闭包；
3. 闭包**惰性捕获**，捕获 bug 只在真正处理草稿发送时才 NameError → 生产环境才暴露 → 静默毁掉 autosend。

**安全方案（下一步）**：把 autosend 设置抽成**可测工厂** `build_autosend_callbacks(assistant, web_app, ...)` 返回 `(_send_cb, _translate_cb)`，并写**单元测试**用 mock 草稿实际调用回调，验证捕获正确性；测试通过再替换 main.py。不靠 smoke 兜底。

**其余待抽**：`_enrich_auto_draft`、`_auto_draft_cb`（同样需评估验证方式）。
