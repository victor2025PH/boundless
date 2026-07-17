# deploy/instances/ · 智聊 ChatX / 通译 LingoX 双实例部署手册

> P3 第一步：把「智聊 ChatX 与通译 LingoX 共用一个 chengjie 后台」拆成**两个产品独立后台**。
> 路线（依 实施09 §五.2 调研拍板）：**双实例** —— 同一份 `engines/chengjie` 代码，
> 两套 config / 端口 / 数据目录，各自品牌 overlay，通译按 `lingox.overlay.example.yaml` 裁剪。
> 不做单实例多租户（`workspace_id` 只是预留脚手架，业务无隔离）。

---

## 0. 引擎侧机制侦察结论（本方案的依据，改引擎前先读这里）

`src/utils/config_manager.py::_get_default_config_path()` 的解析优先级（**引擎原生支持，零代码改动**）：

1. `AITR_CONFIG_PATH` 环境变量 —— 显式指向某个 `config.yaml`；
2. **`AITR_DATA_DIR` 环境变量（本方案采用）** —— config = `$AITR_DATA_DIR/config/config.yaml`；
3. 缺省 —— `<引擎根>/config/config.yaml`（**按模块文件定位，不是按 cwd**；现网单实例走的就是这条）。

配套事实（全部读码核实）：

| 事实 | 位置 | 对双实例的含义 |
|---|---|---|
| `config.local.yaml` 是与 `config.yaml` **同目录**的 overlay，深合并覆盖，30s 双文件热重载 | `config_manager._merge_overlay()` / `check_and_hot_reload()` | 每实例改运营开关只动自己的 overlay |
| 端口/监听来自 `web_admin.host/port`（可被 `AITR_WEB_HOST/PORT/TOKEN` 环境变量覆盖，本方案不用，直接写 overlay） | `config_manager._apply_env_overrides()` | 两实例端口写各自 overlay 即隔离 |
| 业务 DB 全部落「**活动 config 目录**」= `config_path.parent`：`inbox.db`、`web_users.db`、`knowledge_base.db`、`bot.db`、`runtime_flags.db`、`translation_memory.db`、`contacts.db`… | `bootstrap/web_app.py`、`web/admin.py` 等（`Path(config_path).parent / "xxx.db"`） | `AITR_DATA_DIR` 一变，全套 DB 自然分家 |
| `sessions/`（Telethon 登录态）、`logs/app.log` 及少量 `Path("config/…")` 兜底路径按 **cwd 相对**解析 | `src/client/telegram_client.py`（`workdir="sessions"`）、`logging.file` 等 | 每实例用**自己的工作目录**（= 数据根）即隔离 |
| 域包目录 = `config_path.parent.parent / "domains"`；插件目录 = 同级 `plugins/`（自动建、默认关） | `skill_manager.py`、`web/admin.py` | 数据根下需要一个 `domains` junction 指回引擎（见 §3） |
| **例外①** `license.key` 缺省路径按**引擎根**定位（`<引擎根>/config/license.key`），`licensing.key_path` 配置键在 example 里是注释、代码并未读取；但读取顺序是 **`LICENSE_KEY` 环境变量 > 文件** | `src/licensing/license_manager.py::_default_license_path()/_read_token()` | 双实例各自授权走 `LICENSE_KEY` 环境变量（启动脚本自动从实例目录读入，见 §5） |
| **例外②** `license_quota.db` 同样按引擎根定位（`<引擎根>/config/license_quota.db`），两实例**共享此文件**；但记账按 `lic_id` 分键 | `src/licensing/quota_store.py::_default_db_path()` | 两实例授权用**不同 sub（lic_id）**即互不串账；文件级共享是已知残留（见 §9 建议） |
| `--config` CLI 参数只作用于 `--init` / `--check`，**主服务运行时不读它**（`main.py` 里 `ConfigManager()` 无参构造） | `main.py::main()` | 别指望 `python main.py --config` 起双实例，必须走 `AITR_DATA_DIR` |
| `AITR_DATA_DIR` 命中且 config 缺失时会**自动从内置 example 播种** | `config_manager._ensure_seeded()` | 启动脚本先检查 config 存在，防止误播种占位配置（防呆） |
| `AITR_DESKTOP_MODE` 会强制 `web_admin.enabled=true` 并改播种行为 | `config_manager._apply_env_overrides()` | 服务器双实例**不得设置**，启动脚本会显式清掉 |

结论：**双实例 = `AITR_DATA_DIR` + 每实例独立工作目录 + 一个 domains junction**，引擎零改动。

---

## 1. 双实例架构（文字版）

```
                     ┌────────────────────────────────────────────┐
                     │  共享代码（git 单一源，运维只读）           │
                     │  engines/chengjie/   main.py + src/ + domains/
                     └──────────┬─────────────────┬───────────────┘
                                │                 │
              python main.py    │                 │    python main.py
        AITR_DATA_DIR=…zhiliao\data      AITR_DATA_DIR=…tongyi\data
              cwd=…zhiliao\data │                 │ cwd=…tongyi\data
                                ▼                 ▼
        ┌──────────────────────────┐   ┌──────────────────────────┐
        │ 智聊 ChatX 实例          │   │ 通译 LingoX 实例         │
        │ product_id=zhiliao       │   │ product_id=tongyi        │
        │ web 0.0.0.0:18799        │   │ web 127.0.0.1:18899      │
        │ （备用 18787）           │   │ （备用 18887）           │
        │ domain=conversion(现网)  │   │ domain=general           │
        │ companion/RPA/voice 全开 │   │ 纯翻译工作台（重模块全关）│
        │ data\config\*.db 全套    │   │ data\config\*.db 全套    │
        │ data\sessions 登录态     │   │ data\sessions（空）      │
        │ data\events\spool 事件   │   │ data\events\spool 事件   │
        │ data\ledger_outbox 台账  │   │ data\ledger_outbox 台账  │
        │ LICENSE_KEY=智聊授权     │   │ LICENSE_KEY=通译授权     │
        └──────────────────────────┘   └──────────────────────────┘
```

隔离边界：**代码共享（git 管），其余全部按实例分家**——配置、端口、全套 SQLite、
登录态、日志、事件 spool、台账 outbox、授权。唯二共享残留见 §0 例外②与 §9。

## 2. 目录约定

```
deploy/instances/
├─ README.md                  ← 本手册
├─ start_zhiliao.ps1          ← 智聊实例启动（防呆：config 缺失即报错，绝不自动建/覆盖）
├─ start_tongyi.ps1           ← 通译实例启动（同上）
├─ status_instances.ps1       ← 双实例健康探测（端口 + HTTP + 数据目录体检）
├─ zhiliao/
│  ├─ config.local.yaml       ← 模板（git 跟踪，无密钥）：部署时拷贝/并入实例 config 目录
│  └─ data/                   ← 实例运行根（git 忽略：data/ 规则），初始化时手动创建
│     ├─ config/              ← config.yaml + config.local.yaml + 全套 *.db + license.key
│     ├─ sessions/            ← Telethon 登录态（cwd 相对）
│     ├─ logs/                ← app.log + boot_*.log（cwd 相对）
│     ├─ events/spool/        ← EVENT_SPOOL_DIR（events-YYYYMMDD.jsonl）
│     ├─ ledger_outbox/       ← CHENGJIE_LEDGER_OUTBOX（授权台账钩子落盘）
│     ├─ domains  ⇒ junction → engines/chengjie/domains（必须，见 §3）
│     └─ plugins/             ← 可选 junction；不建则引擎自动建空目录（plugins 默认关）
└─ tongyi/                    ← 同构
   ├─ config.local.yaml
   └─ data/…
```

> 数据根也可以放仓库外（如 `D:\chengjie-instances\zhiliao`）：改启动脚本顶部的
> `$DataRoot` 一个变量即可，其余逻辑不变。

## 3. 初始化

### 3.1 通译 LingoX（全新实例，先做——不碰现网）

```powershell
$eng  = "D:\workspace\boundless\engines\chengjie"
$data = "D:\workspace\boundless\deploy\instances\tongyi\data"

# 1) 建目录骨架（只建目录，不建文件）
New-Item -ItemType Directory -Force -Path "$data\config","$data\sessions","$data\logs","$data\events\spool","$data\ledger_outbox" | Out-Null

# 2) 主配置：从引擎 example 起底（通译不继承现网任何数据）
Copy-Item "$eng\config\config.example.yaml" "$data\config\config.yaml"

# 3) 品牌+裁剪 overlay：拷贝本目录模板（深合并覆盖 config.yaml，勿改模板本体）
Copy-Item "D:\workspace\boundless\deploy\instances\tongyi\config.local.yaml" "$data\config\config.local.yaml"

# 4) 域包 junction（域包/人设/KB 种子随代码走，两实例共用一份，只读）
New-Item -ItemType Junction -Path "$data\domains" -Target "$eng\domains" | Out-Null

# 5) 必填项（编辑 $data\config\config.local.yaml）：
#    ai.api_key / ai.base_url / ai.model     ← 翻译兜底引擎，必填
#    web_admin.auth_token / secret_key       ← 后台登录令牌，必填强随机
#    translation.engines.ollama_mt.*         ← 有本地 MT 就填，没有留空自动回落 ai
# 6) 授权（可选，社区模式可跳过）：把通译的授权码存成 $data\config\license.key

powershell -ExecutionPolicy Bypass -File D:\workspace\boundless\deploy\instances\start_tongyi.ps1
```

> 通译不配置 Telegram 协议号（模板刻意保持 example 占位）：引擎检测到占位值会走
> 「跳过协议客户端、纯收件箱 / 网页翻译」启动路径（`_telegram_configured` 判定），
> 这正是通译 MVP 形态；web_chat widget（manual 人工模式）是零依赖入站通道。

### 3.2 智聊 ChatX（承接现网数据——**现网数据属智聊**）

```powershell
$eng  = "D:\workspace\boundless\engines\chengjie"
$data = "D:\workspace\boundless\deploy\instances\zhiliao\data"
# 现网实例的运行目录（按实际情况二选一）：
#   迁移前老路径 D:\workspace\telegram-mtproto-ai ，或已在 boundless 里跑的 $eng
$live = "D:\workspace\telegram-mtproto-ai"

# 0) ★ 先停现网单实例，并禁用它的开机计划任务（见 §8 迁移步骤，防双起互踩）
# 1) 建骨架
New-Item -ItemType Directory -Force -Path "$data\sessions","$data\logs","$data\events\spool","$data\ledger_outbox" | Out-Null
# 2) 整个 config 目录照搬（config.yaml/config.local.yaml/全套 *.db/presets/voice_refs/…）
Copy-Item "$live\config" "$data\config" -Recurse
# 3) 登录态照搬
Copy-Item "$live\sessions\*" "$data\sessions\" -Recurse -ErrorAction SilentlyContinue
# 4) 品牌模板【手工并入】$data\config\config.local.yaml ——
#    ★ 现网 overlay 里有在用的运营开关（翻译双活/发送闸门等），绝不能整文件覆盖；
#    打开 deploy\instances\zhiliao\config.local.yaml，把 web_admin/brand 两段并进去。
# 5) 域包 junction
New-Item -ItemType Junction -Path "$data\domains" -Target "$eng\domains" | Out-Null
# 6) 授权：智聊授权码存 $data\config\license.key（原引擎根 config\license.key 若有即拷来）

powershell -ExecutionPolicy Bypass -File D:\workspace\boundless\deploy\instances\start_zhiliao.ps1
```

## 4. 启动 / 停止 / 健康检查

| 操作 | 命令 |
|---|---|
| 启动智聊 | `powershell -ExecutionPolicy Bypass -File deploy\instances\start_zhiliao.ps1` |
| 启动通译 | `powershell -ExecutionPolicy Bypass -File deploy\instances\start_tongyi.ps1` |
| 健康检查 | `powershell -ExecutionPolicy Bypass -File deploy\instances\status_instances.ps1`（`-Json` 供监控；退出码 0=全 GO 1=部分 2=全 DOWN） |
| 停止 | 查端口持有者后停进程：`Get-NetTCPConnection -LocalPort 18799 -State Listen \| % OwningProcess \| % { Stop-Process -Id $_ }`（通译换 18899）。也可走 `deploy\deploy.ps1 -Action down -Only chengjie_zhiliao -Force`（stack.json 已登记，条目默认 enabled=false，需 `-Only` 显式点名） |

启动脚本防呆行为（两个 start 脚本一致）：

- 数据根 / `config\config.yaml` **不存在 → 报错退出**，提示先按本 README §3 初始化；**绝不自动创建、绝不播种、绝不覆盖任何现有数据**；
- 实例端口已被监听：持有者是 `python …main.py` → 视为已在跑，幂等退出 0；持有者是别的进程 → 报错退出，**不杀任何进程**（与老 `start_main.ps1` 的"清幽灵"不同——双实例下贸然清端口可能误杀另一实例）；
- 显式清空 `AITR_DESKTOP_MODE` / `AITR_CONFIG_PATH` / `AITR_WEB_*`，防外部环境串味；
- 日志：`<数据根>\logs\boot_时间戳.{out,err}.log`（引擎自身 `logs/app.log` 同目录）。

## 5. 授权（两实例各自 license.key，可同档不同 sub）

- 每实例把自己的授权码放 `<数据根>\config\license.key`；启动脚本读到该文件即注入
  **`LICENSE_KEY` 环境变量**（引擎读取顺序：env > 引擎根 `config/license.key` 文件，见 §0 例外①）。
- 两实例可以是同一档位（plan）但 **sub / lic_id 必须不同**——共享的 `license_quota.db`
  按 `lic_id` 分键记账，不同 lic_id 互不串账。
- ⚠ 双实例模式下**不要用后台设置页的「粘贴激活」**：它写的是共享的
  `<引擎根>\config\license.key`，会同时影响两个实例；换授权 = 替换实例自己的
  `license.key` 文件后重启该实例。

## 6. 数据隔离说明

**各自 config 目录 = 各自全套 DB。** `AITR_DATA_DIR` 决定活动 config 目录，引擎所有业务
DB（`inbox.db`、`web_users.db`、`knowledge_base.db`、`bot.db`、`runtime_flags.db`、
`translation_memory.db`、`contacts.db`、`identity_trend.db`、`account_registry.db`…）都建在
`config_path.parent` 下；`sessions/`、`logs/` 按工作目录隔离。SQLite 单进程写的引擎约束
（deploy/README.md 已注明）在双实例下天然满足——**两实例没有任何共写的业务库**。
已知共享残留仅 §0 例外②（`license_quota.db`，按 lic_id 分键）。

## 7. 事件上报约定（对齐 platform/observability 事件契约）

- 每实例设 **`EVENT_SPOOL_DIR` 指向自己的数据目录**（启动脚本已设）：
  `<数据根>\events\spool\` → `events-YYYYMMDD.jsonl`（UTC 按天，append-only）。
- 产品号（`product_id` / 事件名 namespace）：**智聊实例=`zhiliao`，通译实例=`tongyi`**
  （EVENT_CONTRACT.md §产品枚举；发射器 fail-silent，未注册事件打 `_unregistered` 不丢弃）。
- spool 只写不删；收割/上传是下期收集器（P4 `/api/collect` + shipper）的职责。
- **`CHENGJIE_LEDGER_OUTBOX`**：授权台账钩子的落盘目录（实施09 §五.3「签发即导出」，
  另一位同事正在引擎侧接线；环境变量名以他为准）。启动脚本已按实例设为
  `<数据根>\ledger_outbox\`，引擎侧钩子上线后即自动分实例落盘，脚本无需再改。

## 8. 从现有单实例迁移到双实例（现网数据属智聊，通译全新起）

1. **先起通译**（§3.1）：全新数据、独立端口 18899，与现网互不影响，先跑通再动现网。
2. 选维护窗口，**停现网单实例**：
   `powershell -File deploy\deploy.ps1 -Action down -Only chengjie -Force`（或手动停 18799 持有进程）。
3. **禁用老的自启计划任务**（原 `engines/chengjie/start_main.ps1` 或
   `D:\workspace\telegram-mtproto-ai` 下的登录触发任务）：
   `schtasks /Query | findstr /i main` 查名，`schtasks /Change /TN <任务名> /DISABLE`。
   ⚠ 不禁用的话，老脚本开机会按它的「清幽灵端口」逻辑**强杀 18799 持有者**（= 杀掉智聊新实例）再抢启动。
4. 按 §3.2 把现网 `config/` + `sessions/` 照搬进 `zhiliao\data`，并入品牌模板，起 `start_zhiliao.ps1`。
5. `status_instances.ps1` 复核双 GO；老目录数据**保留原地不删**，作为回滚点（回滚 = 停新实例、重启老任务）。
6. 观察期过后，在 `deploy/stack.json` 把 `chengjie` 旧条目 `enabled` 手动改 `false`、
   两个 `chengjie_*` 新条目改 `true`（条目已备好，profile=`chengjie-dual`，默认全关）。

## 9. 配置漂移防线 + 后续建议

**漂移防线：**

- **改代码走 git**：两实例共享 `engines/chengjie` 一份代码，任何代码改动只能经 git 提交/拉取，运维对该目录只读；禁止在引擎目录手改热修。
- **改配置只改各自 `config.local.yaml`**：`config.yaml` 迁移/初始化后视为冻结基线；日常运营开关全部写实例自己的 overlay（30s 热重载，双文件监视）。两实例的 overlay 永不互拷。
- 本目录下的两份模板（`zhiliao/config.local.yaml`、`tongyi/config.local.yaml`）是 git 跟踪的**无密钥基线**：实例 overlay 发生结构性变化（新开关、端口变更）时应同步回写模板（密钥/令牌除外），保证任何机器可按本手册重建实例。
- 端口登记单一源：`deploy/stack.json`（`chengjie_zhiliao`/`chengjie_tongyi` 条目）；改端口先改实例 overlay，再同步 stack.json 条目，避免探活漂移。

**后续建议（本期不改引擎，记录在案）：**

1. 建议引擎支持 `CHENGJIE_CONFIG_DIR`（或让 `licensing.key_path`/quota `db_path` 真正生效）：
   把 §0 例外①②的 `license.key` / `license_quota.db` 从「引擎根」改为跟随活动 config 目录，
   消掉最后两个共享残留；`LICENSE_KEY` 环境变量方案在此之前完全够用。
2. 引擎侧接入事件发射器时（P4），按 §7 的 product_id 约定分实例埋点，无需新增配置。
3. 若未来要把数据根挪出仓库/挪盘，只改启动脚本 `$DataRoot` 一处 + stack.json 备注。

---

### 附：端口分配一览（先查 `deploy/cluster_map.json` + `deploy/stack.json` 确认过不冲突）

| 端口 | 归属 | 说明 |
|---|---|---|
| 18799 | 智聊 web 后台 | 现网既有端口，迁移后归智聊实例 |
| 18787 | 智聊备用 | example 默认端口，作探活备用位登记 |
| 19199 | 智聊 monitoring | 现网 metrics 线程（随迁移配置走） |
| **18899** | **通译 web 后台** | 新分配；cluster_map/stack 均未占用 |
| **18887** | **通译备用** | 新分配备用位（默认不绑定） |
| — | 通译 monitoring | overlay 已关（`monitoring.enabled: false`） |

已占用参照（勿再分配）：18080/8000(huoke)、3000(website)、7899(indextts)、9000(avatarhub)、
7852/7854/7857/7858(TTS/STT)、11434(ollama)、19190/19199(chengjie metrics)。
