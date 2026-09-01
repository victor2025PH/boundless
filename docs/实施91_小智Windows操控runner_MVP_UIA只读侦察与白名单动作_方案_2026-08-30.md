# 实施91：小智「Windows 操控 runner」MVP——UIA 只读侦察 + 白名单动作 + 确认卡（方案）

> 日期：2026-08-30 ｜ 状态：**方案 v1（未动代码）**，待老板拍板后另开对话实施
> 承接：实施88 §5.3（「操作用户电脑」手②子系统）+ §5.5（P1 落地草案）；实施58（小智
> 全能助教 P0-P6，安全骨架源头）；实施80（算力实况）；实施90（相册 AI，并行线）
> 前置已完成：**实施88 P0「操作本软件闭环」已上线**（DOM 动作总线 + 动作表 20→29，
> 2026-08-30 晚，前端热更 + `.py` 待重启窗；门禁 236 绿 / 浏览器 115 / sweep 700 绿）

---

## 0. 一句话

P0 让小智能**操作本软件**（自家 DOM，确定性动作总线）；本案是实施88 选型里的**手②**
——让小智能**操作用户的 Windows 电脑**。MVP 刻意最小面：**只读侦察为主 + 极小白名单
动作 + 每个动作走确认卡 + 只管内网自己人的机器**，安全骨架 100% 平移小智现有实现
（确认 token / 审计 / 撤销 / 配对），执行层用**纯 Python `uiautomation`**（零外购、pip 一
装），把 terminator/MCP/视觉兜底全部留到 MVP 数据说话之后。

**一句话边界**：这一期要证明的是「小智能安全地看见并轻触一台受控 Windows 机」，
**不是**「小智能自动跑完任意桌面任务」——后者是 P2/P3，且有 51%~100% 的注入攻破率
（实施88 §2.5，VPI-Bench），必须靠动作层白名单一步步爬，不能一步到位。

---

## 1. 定位与承接

### 1.1 小智能力版图（本案的位置）

```
                          小智规划层（现有 plan→确认→审计→撤销 骨架，全部复用）
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────┐
   手① 操作本软件（P0 已上线）                              手② 操作用户电脑（本案 MVP）
   DOM 动作总线 + 白名单动作表                             Windows 操控 runner
   ui_anchors.py / actions.py(29)                        · 只读侦察（UIA 控件树）
   在自家页面点开/填入/带看                                 · 极小白名单动作（launch/focus/click）
   确定性、毫秒级、零 GPU                                   · 确认卡 + 审计 + 一键停 + 机器配对
```

眼/脑/嘴/耳复用现成：脑=`ai_client`（主链+本地兜底）、嘴耳=现有 ASR/TTS、
规划=`agent_planner`（JSON 计划 + 结构性复验）。本案只新建**手②**。

### 1.2 实施88 已定的结论（本案照此落地，不重开选型）

- 操作用户电脑走「**UIA 无障碍树优先 + 视觉兜底**」混合范式（抄 UFO²），**不**做应用内
  像素级 computer-use；
- 模型主选本地 Qwen3-VL（视觉兜底档，本案 MVP **不启用**）；
- 协议向 MCP 靠拢（生态互通）；
- 安全三板斧（白名单 / 高危人审 / 审计撤销）是全行业红线，从小智现有实现平移。

本案是把上面这段的**第一块砖**落地成可运行的 MVP。

---

## 2. 现状盘点（代码实况，2026-08-30）

### 2.1 可直接复用的地基（安全骨架全齐，不重造）

| 零件 | 位置 | 对本案的意义 |
|------|------|--------------|
| 动作注册表 + 三板斧 | `src/assistant/actions.py`（29 动作，确认 token 单次核销/TTL + undo 快照 24h + 审计 JSONL） | runner 动作走**同一套** plan→confirm→apply→undo，不开第二条写通道 |
| 规划器（JSON 计划 + 结构复验） | `src/assistant/agent_planner.py`（LLM 只提名、逐步 `plan_action` 复验、越权即丢） | runner 侦察结果注入规划上下文；runner 步复用同一复验链 |
| 配对/会话/踢下线模型 | `src/assistant/pairing.py`（一次性 token TTL 120s + 会话注册表 + `revoke` 立即失效 + `@mobile` 审计后缀） | **机器配对直接类比**：被控机注册表 + 一次性配对 + 随时踢下线 |
| DOM 动作总线（P0，新鲜范本） | `src/assistant/ui_anchors.py` + `actions.py::kind="ui"` + 前端执行器 | runner 是它的「跨机版」——同样「LLM 提名 id、服务端换出真身、前端/runner 执行」 |
| 路由骨架 | `src/web/routes/assistant_action_routes.py`（`/act` 两段式 + 节流 + 角色闸 + mobile_guard） | 加 runner 分支即可，鉴权/节流/审计现成 |
| 机器台账 + mesh 推送 | `deploy/machines.json`（六机 SSH/IP/角色）+ `tools/setup_machine_mesh.ps1` | runner 部署复用现成推送机制 |

**关键事实：仓库里没有任何 computer-use / MCP 客户端 / robotjs / pywinauto 代码**（全库
grep 确认）——本案是干净的新子系统，不与任何在跑的东西冲突。

### 2.2 机器拓扑（`deploy/machines.json`，全内网 192.168.0.x）

| 机器 | 角色 | 与本案的关系 |
|------|------|--------------|
| 117 zhuji | 智聊生产 + 坐席（小智后端 :18799 所在机） | **MVP 首个受控机**（后端↔本机 runner，无跨机，最简起步） |
| 198 kouxing | dev，识图 :11434 + 一张**几乎空转的 4070**（实施80） | MVP 第二个受控机（跨机链路验证；将来视觉兜底本地大脑落点） |
| 173/104/140 | 坐席/算力节点 | 受控机候选（灰度扩面） |
| 176 zhongshu | hub，qwen3-vl :11434 | 视觉兜底档模型落点（P2，本案不用） |

六台都是**内网可信自有机**（"先管自己人"的字面含义）；无任何真客户机。

### 2.3 执行层选型（联网核实，2026-08-30）

| 候选 | 事实 | MVP 判定 |
|------|------|----------|
| **`uiautomation`**（yinkaisheng，3K★，Python 3 纯封装 MS UIAutomation） | `pip install uiautomation`；`WalkControl` 遍历控件树；支持 Win32/WinForm/WPF/Metro/Qt/Chrome/**Electron**（需 `--force-renderer-accessibility`）；需管理员权限枚举 | ✅ **选定**：零外部服务、零 Rust、纯 Python，只读侦察 + 少量白名单动作足够 |
| **`terminator`**（mediar-ai，MIT，Rust 核 + PyO3 `terminator.py` + 自带 MCP Server 35+ 工具） | `pip install terminator.py`；Playwright 风格选择器（抗主题/DPI）；需 asyncio 包装；Windows 主目标 | ⏸ **升级位**：价值在选择器稳定性 + MCP 多机编排，MVP 用不到；选择器漂移/接 MCP 时 drop-in |
| **Windows-MCP / UFO²** | MCP 服务器 / 完整 AgentOS，功能强但重 | ⏸ 远期（多机编排/复杂任务再评） |
| **视觉兜底（Qwen3-VL 截屏 grounding）** | 176/198 本地可跑；UIA 拿不到结构时的补丁 | ⏸ **P2**：MVP 只碰能拿到 UIA 树的目标，拿不到就诚实说「看不懂这个界面」 |

**选型一句话**：MVP 站在纯 Python UIA 的肩膀上（最省、最稳、零外购），动作面**按 MCP
工具语义命名**（`read_tree` / `list_windows` / `launch_app` / `focus_window` / `click` /
`screenshot`），将来换 terminator 或接 MCP 生态**零重构**。与 P0「通用 Flow DSL 降级为
登录模式运行器」的 YAGNI 判断同源。

---

## 3. 架构

### 3.1 拓扑

```
坐席/老板（语音·文字·面板）
        │
        ▼
小智规划层（117 :18799，现有）  ──plan→确认卡→审计→撤销──┐
        │  runner 动作经 /api/assistant/act 的 kind="runner" 分支         │
        ▼                                                                 │
  ┌───────────── HTTP（内网，一次性配对 token）─────────────┐             │
  ▼                                                          ▼             │
runner@117（本机，MVP 首落）                        runner@198（跨机，MVP 次落）
  · Python uiautomation 常驻服务（独立进程/端口）                          │
  · 只读侦察：list_windows / read_tree → 结构化摘要                        │
  · 白名单动作：launch_app / focus_window / click（都要 confirm token）    │
  · 独立低权限账户运行 · 审计 JSONL · 一键停                               │
  └──────────────────────────────────────────────────────────────────────┘
                                                                          │
                          审计/撤销/观测 ◄──────────────────────────────────┘
```

**动作真身在 runner，小智只是编排 + 确认闸门**——与 P0「动作真身在服务端 API，前端只
是执行器」同构，跨机版把「前端」换成「被控机 runner」。

### 3.2 runner 服务（新建，被控机上常驻）

- 独立进程（不寄生小智后端），监听内网端口（如 `:18760`），**只接受内网 + 携带有效
  配对 token 的请求**；
- **纯 Python uiautomation**，进程以**独立低权限 Windows 账户**运行（CSA 建议：agent 不
  用管理员日常账户；但 UIA 枚举需要一定权限——MVP 用受控机上的专用坐席账户，不用
  Administrator 跑业务，权限边界写进部署 SOP）；
- 动作面（MCP 语义，白名单硬编码）：

| 工具 | 类别 | 确认 | 说明 |
|------|------|------|------|
| `list_windows` | 只读 | 无 | 枚举前台/可见顶层窗口（标题/进程/是否前台） |
| `read_tree(window)` | 只读 | 无 | 某窗口的控件树结构化摘要（role/name/automationId/可点性，深度封顶、节点数封顶） |
| `screenshot(window)` | 只读 | 无 | 窗口截图（给人看/存证，**MVP 不喂 VLM**） |
| `launch_app(app_id)` | 动作 | **确认卡** | 只许启动**白名单登记**的应用（记事本/计算器/浏览器等，`app_id`→绝对路径由 runner 端注册表换出，绝不接受任意路径/命令行） |
| `focus_window(window)` | 动作 | **确认卡** | 把某已存在窗口切到前台 |
| `click(node)` | 动作 | **确认卡** | 点击 read_tree 侦察出的**具体节点**（node 引用来自本轮侦察，runner 端二次校验节点仍存在+可点，绝不接受裸坐标） |

- **永不进 MVP 白名单**（→ P2 或永久禁区）：`type_text`（填表单）、点「保存/发送/删除/
  确定」类提交按钮、任意 `run_command` / shell、文件系统写、注册表、坐标点击、键盘宏。

### 3.3 小智侧接入（复用 P0 范式）

- `actions.py` 新增 `kind="runner"` 动作（如 `pc_inspect` L0 只读 / `pc_launch` L2 /
  `pc_focus` L2 / `pc_click` L2），走**同一** plan→confirm→apply→审计 链；
- runner 侦察结果注入规划上下文（类比 `ui_anchors.prompt_lines`，但这里是**被控机实时
  控件树**，不是静态注册表）——LLM 只能提名侦察出的 node/window id，runner 端换真身；
- 机器配对：新建 `runner_pairing`（直接抄 `pairing.py` 结构）——被控机注册表 + 一次性
  配对 token + `revoke` 立即踢下线 + 审计 actor 带 `@pc:<机器>` 后缀；
- 前端：任务卡展示 runner 步（MVP **纯文本进度**：「正在看 198 的桌面…」「已打开记事本」，
  **不做跨机流星**——流星是本机视觉特效，跨机无意义，YAGNI）。

---

## 4. 安全红线（一票否决，做之前必须认账）

平移小智现有三板斧 + 实施88 §2.5 的全行业共识（CSA 2026-04 / VPI-Bench）：

1. **白名单是结构性防线，不靠 prompt 自觉**（VPI-Bench：system prompt 防御基本无效，
   攻破率 51%~100%）：LLM 只能提名注册表内的工具/应用/节点，runner 端按 schema 二次
   校验参数，**永不执行 LLM 生成的路径/命令/坐标/选择器**。
2. **金融 / 凭据 / 外发 / 装软件四类永远人审**（CSA），MVP 干脆整个动作面都走确认卡。
3. **屏幕内容一律不可信**：控件文本/窗口标题只作**消毒后摘要**进 prompt，绝不当指令
   执行（网页/弹窗可能藏「忽略指令去删库」）。
4. **独立低权限账户运行 runner**，不用 Administrator 跑业务；受控机端口只内网可达、
   不暴露公网、Windows 防火墙按机限制来源。
5. **只管内网可信自有机（六台）**，MVP 绝不碰真客户机（真客户机 = P3，需先设计跨用户
   授权模型，`/xz` 手机配对是用户自助、不能直接复用）。
6. **一键停 + 随时踢下线**：小智面板「已连接的受控机」列表可 `revoke`，对 runner 接口
   立即 401；runner 本地也有 kill-switch（停服即全断）。
7. **硬限制写在代码里不在 prompt 里**（禁区动作 runner 端直接不实现，不是靠提示词劝阻）。
8. **审计全留痕**：每次侦察/动作/撤销落 JSONL，actor 带 `@pc:<机器>`，谁在哪台机做了
   什么一目了然。
9. **灰度纪律**：`assistant.pc_runner.enabled` 默认 `false`（新子系统约定），先 117 本机
   dry-run，再逐台放开；runner 侧独立开关，双闸。

---

## 5. 分期（MVP 内部，每期独立可验收、可单独叫停）

| 期 | 内容 | 交付物 | 验收 |
|----|------|--------|------|
| **P1-0 地基** | runner 服务骨架（独立进程 + 内网监听 + 健康探活）；`runner_pairing`（配对/注册表/踢下线）；审计 JSONL；一键停；`assistant.pc_runner` 双开关默认关 | runner 能起停、配对、被踢；小智后端能连上并鉴权 | 配对成功→调用通→踢下线立即 401；未配对/公网来源必拒 |
| **P1-1 只读侦察** | `list_windows` / `read_tree` / `screenshot`（三只读，**零动作**）；结果结构化摘要 + 消毒；小智侧 `pc_inspect`（L0）+ 规划上下文注入 | 小智能准确报出受控机「前台是什么窗口、有哪些可点控件」 | 与人肉眼所见一致；深度/节点数封顶不卡死；截图 magic bytes 验证（媒体产物纪律） |
| **P1-2 极小白名单动作** | `launch_app`（白名单应用）+ `focus_window`，各走**确认卡**；小智侧 `pc_launch`/`pc_focus`（L2）；审计 | 语音「在 198 上打开记事本」→ 确认卡 → runner 真打开 → 回执 | 端到端成功；非白名单 app_id 必拒；确认前零副作用 |
| **P1-3 click 白名单** | `click`（点侦察出的具体节点）+ 确认卡 + 审计；节点二次校验（仍存在+可点） | 「帮我点一下那个『新建』按钮」→ 确认 → 点中 | 点中正确节点；节点失效诚实失败不乱点；无撤销声明（点击不可逆，确认卡讲清） |
| **P1-4 观测 + 灰度** | ops 卡「🖥️ PC 操控」（受控机在线数 / 侦察次数 / 动作次数 / 拒绝次数）；埋点 `pc_*`；灰度从 117→198 | 看板可见用量与拒绝；灰度可控 | 零流量整卡隐藏；拒绝路径可观测 |

**依赖顺序**：P1-0→P1-1→P1-2→P1-3 严格串行（层层叠信任）；P1-4 贯穿。
**MVP 完成线 = P1-3**（能看、能开应用、能轻触一个按钮，全程确认+审计）。

---

## 6. 复用与新建清单

| 模块 | 动作 |
|------|------|
| `src/assistant/runner_client.py` | **新建**：小智后端侧的 runner HTTP 客户端（配对/侦察/动作调用，超时+重试+失败诚实回落） |
| `src/assistant/runner_pairing.py` | **新建**：抄 `pairing.py`（被控机注册表 + 一次性 token + revoke） |
| `runner/`（新目录，独立可打包） | **新建**：runner 服务本体（Python uiautomation + 内网 HTTP + 白名单注册表 + 审计 + kill-switch）；MVP 单文件起步 |
| `src/assistant/actions.py` | 扩展：`kind="runner"` 动作族（pc_inspect/pc_launch/pc_focus/pc_click），复用三板斧 |
| `src/assistant/agent_planner.py` | 扩展：runner 侦察上下文注入 + runner 步复验（同 ui 步范式） |
| `src/web/routes/assistant_action_routes.py` | 扩展：`kind="runner"` 分支 + runner_guard（配对校验）+ 「已连接受控机」列表/踢下线路由 |
| `shared/assistant/assistant-agent.js` | 扩展：runner 步的纯文本进度渲染（不做跨机流星） |
| 门禁 | **新建** `tests/test_runner_pairing.py`（配对/踢下线/token）、`tests/test_pc_actions.py`（白名单/禁区必拒/节点校验/确认链）、`tests/test_runner_client.py`（超时回落）；登记 gate_sweep |
| 部署 | runner 推送复用 `tools/setup_machine_mesh.ps1`；受控机开机自启任务 + 独立账户 SOP |
| 依赖 | `uiautomation` 进 requirements（+ CI 跳过非 Windows） |

---

## 7. 验收指标（MVP 上线后看什么）

| 指标 | 口径 | 目标 |
|------|------|------|
| 侦察准确率 | runner 报的前台窗口/关键控件 vs 人肉眼 | ≥95%（抽样人审） |
| 动作成功率 | 确认后 launch/focus/click 成功 / 全部确认 | ≥90% |
| 禁区拦截 | 非白名单 app / 裸坐标 / 任意命令 的拒绝率 | 100%（门禁钉死） |
| 注入抵抗 | 屏幕文本含「忽略指令…」时 runner 不越白名单 | 100%（结构性，非 prompt） |
| 配对安全 | 过期/被踢/公网来源调用的拒绝 | 100% |
| 灰度 | 受控机在线数、按机用量分布 | 117→198 逐台，可回退 |

---

## 8. 刻意不做（本 MVP）与理由

| 不做 | 理由 / 归属 |
|------|-------------|
| 视觉兜底（Qwen3-VL 截屏 grounding） | UIA 树对 MVP 要碰的目标够用；拿不到结构就诚实说「看不懂」。→ P2（176/198 本地 VL 已就位） |
| terminator / MCP 生态 | MVP 单机少量白名单动作用不到 Playwright 选择器/MCP 编排；动作面已按 MCP 语义留位 → 需要时 drop-in |
| `type_text` / 点「保存·发送·删除」/ 任意 shell / 文件写 / 坐标点击 / 键盘宏 | 高危不可逆 + 注入放大面；等只读侦察数据证明可靠后**逐个**放开，或永久禁区 |
| 真客户机操控 | 需先设计跨用户授权模型 → P3；MVP 只管内网自有机 |
| 跨机流星特效 | 视觉特效是本机的，跨机无意义（YAGNI）；MVP 纯文本进度 |
| 多机 DAG 编排（UFO³ Galaxy 式） | 远期；MVP 一次只操一台 |

---

## 9. 待老板拍板

1. **MVP 完成线**：做到 P1-3（看+开应用+轻触按钮）就交，还是先只做 P1-1 只读侦察看数据？
2. **首批受控机**：117 本机 + 198（空转 4070 的 dev 机）是否合适？还是先只 117 单机验证？
3. **runner 运行账户**：受控机上用专用低权限坐席账户跑 runner（推荐），需要运维配一个；
   可否接受？
4. **白名单应用清单**：MVP 的 `launch_app` 先放哪几个（记事本/计算器/浏览器/资源管理器…）？
5. **依赖引入**：`uiautomation`（纯 Python，3K★ MIT-like）进 requirements 是否 OK？
6. **命名**：对外仍叫「小智」，但实施88 §0 提醒有同名开源项目 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)（也是「语音+MCP 控 PC」定位）——「PC 操控」功能对外话术是否要与之区分？

---

## 附：与实施88 的对应

- 实施88 §5.3「手②：操作用户电脑——新开 Windows 操控 runner」= 本案架构（§3）；
- 实施88 §5.5 P1「Windows runner MVP（UIA 只读侦察 + 白名单动作 + 确认卡），先管
  117/坐席机自己人」= 本案 §5 分期；
- 实施88 §2.5 安全共识（CSA 四类 + VPI-Bench 动作层防御）= 本案 §4 红线；
- 实施88 §5.2「手①不引 computer-use」= P0 已交付（本案不重复）。
