# player_care 域包 —— WA / TG「普通朋友」人设 + 玩家网关只读事实

> 分支 `feat-player-care-loop-2026-09-20`。智拓故事矩阵闭环（拓客 → 聊天转化 → 维护）里
> **智聊侧**的第二个实例所用域。本文是设计 + 运维单一入口；接口契约见智拓仓
> `docs/CHATX_COMMANDBUS_CONTRACT.md`（commandbus，智聊侧 pull/ack 已接，见 §6b）。

## 1. 一台电脑两套智聊实例（为什么、怎么隔）

| 实例 | 域 | 接什么 | 人设 | 端口 | 配置目录 |
|---|---|---|---|---|---|
| story | `story_matrix` | 智拓 Messenger 前台 `replybus.decide` / `leadbus` | 4+1 位频道主持 | 18796 | `config/` |
| player | `player_care` | WhatsApp（网页版扫码）/ Telegram（API）协议号 7×24 | 也在玩的普通朋友 | 18797 | `config_player/` |

同一份代码、同一台机、**不同 `--config` 目录**（inbox / contacts / goals / memory 全部随配置目录落盘 → 数据天然隔离）。
一个实例只能激活一个域，这正是老板要的「游戏类型与频道完全隔离」：频道主持永远不知道游戏，朋友人设永远不提频道。

```powershell
python main.py --init player_care --config config_player\config.yaml   # 从预设脚手架
python main.py --check           --config config_player\config.yaml   # 0 错误才启动
python main.py                   --config config_player\config.yaml
```

## 2. 人设：朋友，不是客服

- 一个也在玩手游的普通人（`personas.example.yaml`：`jomar` 骑手 / `kath` 夜班客服）；
- 游戏只作为「自己在玩的东西」出现——对方聊到游戏 / 无聊 / 想放松才一句带过；**不催充、不承诺赢、不讲赔率**；对方说输钱 / 手头紧 / 不想玩 → 站对方那边并放下话题；
- 永不出现 AI / bot / 真人 字样；STOP 就停；不见面、不要照片、不撩；
- `style_hint` 里「在玩的游戏」由运营按该账号**真实登录的游戏**填，不写不存在的名字。

## 3. 只读事实：玩家网关（`domains/player_care/gateway.py`）

```
POST {player_gateway.url}/lookup   X-Gateway-Key: <key>
{"q": <客户消息>, "phone": <可选 639…>, "uid": <可选>}
  → 200 {"ok": true, "query", "phone_hits", "missing_phones", "players": […], "chatx_text": "..."}
  → 404 {"error": "not_found", …}   401 {"error": "unauthorized"}   400 {"error": "empty_query"}
```

**真网关格式（2026-09-21 三探针定稿，脱敏样例 `tests/fixtures/player_care_lookup_samples.json`）**：

- `chatx_text` 是中文摘要：首尾各一行客服式包装（`【玩家后台资料…】` / `（余额/充提/打码以本资料为准…）`），中间每个玩家一行
  `UID x / 昵称 / VIP0 / 正常；余额 a（投注中 b）；充 总额×次数，提 总额×次数，净存 n；总投注 t，输赢 w，最近投注 时间；打码已达标|待打码 r。 风险：同IP关联 k，禁提…`；
- **游戏不在文本里**：`players[i].top_games[] = {platform, game, bet_amount, win_loss, bet_count, last_played_at, first_played_at}`（实测 JILI / PP / PG 等厂商，游戏名如 Fortune Gems 2 / Gates of Olympus / Super Ace）；
  `extract_games(res)` 优先读这里，`platform: X, game: Y` 文本正则只作兜底；
- **有 `agent` 字段**：`players[i].agent`（代理号 / 渠道码，字符串），`extract_agent(res)` 取它 → 画像 `agent` 列；
- 充值：`players[i].has_deposited / deposit_count / deposit_total`（`has_deposit(res)`）；文本兜底 `detect_deposit` 认 `充 <金额>×<次数>`（`充 0×0` = 没充）；
- 同一手机号多账号 → 200 + `multi: true` + `candidates`，没有 `chatx_text` → `LookupResult.multi=True`，只留 `candidates` 里的 uid（昵称 / 代理等不外露）；hook 走 `ambiguous` 分支问一句会员号，对方回的数字**恰好等于**某候选 uid 才认，复查只带 uid 不带手机号（带手机号会再撞 multi）；sync 里画像已有 uid 同样只用 uid 复查；一次传 phone + uid 且是两个人 → `players` 两条；
- 给人设的事实用 `friend_facts_text(res)`：去掉包头包尾和每行「风险：…」尾巴（同IP关联 / 禁提是后台风控，朋友不该知道），余额 / 充提 / 打码 / 时间原样保留；
  明用块再附一行「近期玩过：游戏（厂商）…」，数字闸的事实集 = 朋友版文本 + 游戏名（游戏名里的数字不误杀）。

- 手机号任意写法（9… / 09… / +63 9… / WA JID）归一为 `639xxxxxxxxx`；会员号只在带 `uid / member id / account no` 前缀时抽取；
- 默认关：`enabled` 不为真或缺 url / key → 不发请求；401 / 超时 / bad json → 一律「没资料」；404 = 网关通但没这个人；
- 同联系人 60 s 缓存；密钥只从 `player_gateway.key` 或环境变量 `GATEWAY_KEY` 读，不进日志不进 git；
- 与 117 机 chengjie 树 `wujie_player.py` 用同一组配置键，以后合并不打架。

**真网关自检（B1.5 前置，不依赖密钥就能写好）**：`domains/player_care/gateway_probe.py`

```
$env:PYTHONPATH=""; .\.venv\Scripts\python.exe -m domains.player_care.gateway_probe --config config_player\config.yaml --phone 09xxxxxxxxx [--uid ...] [--out tests\fixtures\player_care_lookup_samples.json]
```

在装了 `GATEWAY_KEY` 的机器上跑一次，依次打三个探针：`found`（真号）/ `not_found`（不存在的号）/ `bad_key`（故意错密钥，应 401/403），
每个探针输出：HTTP 状态 / ok / found / error / 延迟 / 返回体顶层键名 / `agent_fields`（递归找 agent/代理/affiliate/upline 类键的路径，及 chatx_text 里的 agent 行；空 = 没有）/ 脱敏后的 `chatx_text`
（≥ 3 位数字换成同长度 `100…`，日期/时间/版本号保形，源号码全遮成 `*`）/ `extract_games` 与 `detect_deposit` 当前解析结果。密钥永不进输出。
`--out` 写的 JSON 就是测试够吃的样例文件格式：`tests/test_player_care_domain.py::test_lookup_samples_file` 逐条回放（假 transport 回放同一 status/body），
断言 `found` / `error` / `games` / `deposit` 与样例里的 `expect` 一致。拿到真样例后只要把文件换成真的、改 `expect`，再改 `extract_games` 直到绿——不用重写测试。

## 4. 两道闸（`hooks.py`，由 `generate_inbox_draft` 3f/9d 与 `process_message` 派发）

| 分支 | 触发 | 进提示词的块 | 数字闸 |
|---|---|---|---|
| **明用** `visible` | 对方问自己账户（balance / deposit / withdraw / turnover / bonus…）**且**有手机号或会员号，网关查到 | 「只读事实」块 = `friend_facts_text` 事实行 + 「近期玩过：…」 + 「像帮朋友问了一下」口吻 + tl/en 时「事实是中文摘要，用对方语言转述，数字 / 时间 / 游戏名原样照抄、不换币种」+「只答问到的一两项」 | 回复里出现事实外 ≥3 位数字 → 整句换安全句 |
| **多账号** `ambiguous` | 问账户，网关返 `multi`（一号多户） | 「手机号下不止一个账号，不报数、不猜；朋友口吻问会员号」，不列候选 uid | 任何 ≥3 位数字 → 安全句 |
| **无资料** `missing` | 问账户但查不到 / 超时 / 网关未配 | 「没查到，稍后再看，绝不编数字」 | 任何 ≥3 位数字 → 安全句 |
| **不知是谁** `need_identity` | 问账户但没有手机号 / 会员号（TG 数字 id） | 「不编数字；朋友口吻问一句是哪个号」；即使网关按 q 命中也**不给数字** | 同上 |
| **暗用** `hidden` | 没问账户但有身份线索且查到 | 只有一条**不含数字**的隐藏提示：对方玩过哪些游戏，「你不知道来源、不能提及、对方先聊到才接」 | 不启（日常数字放行） |
| 无线索 | 既没身份也没问账户 | 不打网关，不注入 | 不启 |

每轮结果写进持久 `user_context["_player_facts"]`（phone / uid / found / text / games / deposit / agent / ts，multi 轮另带 candidates uid 列表）供画像与阶段用——网关原始返回体（same_ip_users / history_ips / scripts / credit_score 等风控与人身字段）**不进** user_context、不进画像、不进提示词，只活在 60s 内存缓存里；提示块 `_domain_context_block` 用完即清，不跨轮泄漏。
`get_escalation_line()` 返回空——基类的中文客服话术绝不带出去。

## 5. 顺手修的核心缺口（对所有域生效）

`DomainHook.on_message_pre_process` / `on_reply_post_process` 在 `src/hooks/base.py` 定义已久，但**核心从没派发过它们**——story_matrix 域的身份词 / 联系方式兜底 `on_reply_post_process` 一直是死代码（真正挡住的是智拓侧 `chatx_reply_blocked` 硬闸）。本分支在两条产线接上：

- B 线 `SkillManager.generate_inbox_draft`：3f（pre，enrich/目标注入之后、意图识别之前）+ 9d（post，危机兜底之后）；
- A 线 `SkillManager.process_message`：hook ctx 构建后（pre）+ 5d 危机兜底后（post，5d2）；
- `ai_client._build_context_prompt` 新消费 `_domain_context_block`（目标块之后、坐席指令之前）。

基类默认返回 None / 原文 → 无域包或域包未覆写时逐字节零影响。**副作用**：story 实例升级到本分支后，story_matrix 的 post hook 开始真的生效（更严：识别到 AI 字样 / 链接 / 号码的回复会被换成频道安全句）。

**域页可见角色**：域清单 `web.pages[].roles` 一直有人写（payment 也写了 `[master, admin, viewer]`）但核心从没读过，域页一律掉进「无条目 → 仅 master」。
现在 `create_app` 读清单后调 `web_user_store.register_domain_page_permissions(pages, domain)`：核心表已有的键不动（域不能放宽核心页），
未声明 roles / 角色名全不认识的页仍仅 master，master 恒在允许集；按域记账——同域重注册以最新清单为准（消失的旧键退回仅 master），别的域已占的键不覆盖；侧栏 / 命令面板 / 新手引导三处域页列表同步按 `page_perms` 过滤，看不到的角色不再看到一个点进去就 403 的入口。

## 6. B2 画像 + 阶段（已做）

- 旁表 `player_profiles` / `player_daily_stats`（`src/contacts/store.py`，与 contacts.db 同文件，`CREATE IF NOT EXISTS` 幂等）：
  `profile_key`（手机号 639… 主键；没手机号时 `platform:external_id` 占位，拿到后 `rebind_player_profile` 合并）、
  `phone_e164 / uid / agent / account_id(owner_slot) / games_json / stage / first_seen / last_seen / inbound_count /
  registered_at / deposit_days / lookups / visible_hits / gate_hits / facts_text`。
- `domains/player_care/profile.py`：`PlayerProfileService.record_inbound` 由 hook 每轮入站调（查不查网关都记，失败吃掉不影响回复）；
  阶段只前进：`chatting`=第 2 条入站；`mentioned_game`=对方自己聊到游戏 / 无聊（`GAME_MENTION_RE`）；`registered`=网关查到；
  `depositing`=事实里有充值记录（`detect_deposit` 保守正则，B1.5 真样例后定稿）或 B3 `note_deposit`；`active`=不同自然日充值 ≥ 2；
  `dormant`=`apply_dormancy` 扫到沉默 > 7 天（再来消息回原阶段）。一切信号只来自对方原话或网关事实，不推断。
- 配置 `player_care.profile.{enabled,db_path,dormant_after_days,active_min_deposit_days}`（见 defaults.yaml 注释）；无 `config_path` 的裸 dict / None 不落盘。
- A 线 hook ctx.extra 补 `platform / account_id / chain`，与 B 线同口径（owner_slot 才有来源）。
- 日报：`python -m domains.player_care.report --config config_player\config.yaml [--day] [--json]` → 按我方账号 × 阶段快照 + 当日入站 / 查网关 / 查到 / 明用 / 数字闸 / 新增 / 升阶。
- 测试 `tests/test_player_care_profile.py`。

## 6a. B3 player_sync（已做）

- `domains/player_care/sync.py::run_player_sync`，由 `health_watchdog._check_player_sync` 在巡检 tick 里稀疏节流调（仿 `_check_goal_order_pull`）；
  **只有 `domain: player_care` 的实例跑**，story_matrix 实例直接返回。配置 `player_care.sync.{enabled=true, interval_min=30(下限 5), active_days=7, batch=50, goals.{enabled, autonomy=auto, max_per_day=20, reengage_days=7, after_deposit_days=3}}`。
- 一轮：① `dormant_sweep` 沉默 > N 天 → dormant，每个**新**置入的起 `player_reengage` 目标；② last_seen 在 active_days 内、有手机号/UID、上次 lookup 距今 ≥ interval 的画像逐个 `gateway.lookup` →
  `PlayerProfileService.record_sync`（不动 last_seen / inbound_count，dormant 不唤醒只刷 `stage_before_dormant`）；事实里**新**出现充值（`detect_deposit`，换日才算新）→ 起 `player_after_deposit` 目标。
- goals 护栏：`companion.goals.enabled` 关一律不建；同会话（`platform:account_id:external_id`）已有 active 目标不重建；每日预算（`created_by=player_sync` 进库计数）。
- 模板在 `domains/player_care/goal_templates.py`（`player_reengage` / `player_after_deposit`，`push_curve` 全 none：不催充不提优惠不承诺赢不报数字），
  `register_goal_templates()` 以 setdefault 挂进 `src/companion/goals/templates.TEMPLATES`（核心模板表源码不改；只在本域 hook 随真实配置装载 / sync 跑时才挂）。
- 网关数字只原样落 `facts_text`，不算不推；失败全吞，不影响巡检主流程。watchdog 侧 `total_player_sync_runs / last_player_sync` 留给 B6 看板。
- 测试 `tests/test_player_care_sync.py`。

## 6b. B4 commandbus 发送端（已做）

- 契约（智拓仓 `docs/CHATX_COMMANDBUS_CONTRACT.md`，只读）是 **智聊产指令、手机拉取执行**：智聊侧只需实现
  `GET /api/commandbus/pull?account=&limit=` → `{"available": true, "commands": [...]}` 和 `POST /api/commandbus/ack {command_id, status: done|failed|rejected, detail}` → `{"available": true}`（幂等）。
  两端点在 `domains/player_care/web/routes.py`（manifest `web_routes: true`，由 admin 按激活域自动挂 → **只有 player_care 实例有**），鉴权同 leadbus/replybus（Bearer `BOUNDLESS_BUS_TOKEN`）。
- 出箱 `domains/player_care/commandbus.py::CommandOutbox`（`<配置目录>/player_commandbus.db`，不动核心表）。三种指令，信封形状同契约 `command_id / kind / account / phone / dry_run + 种类字段`：
  `reengage {messages[], reason}`（话术池：普通朋友问候，不提游戏无数字，手机侧套自己的养号/时段/配额）、`stop {reason}`、`note {text}`。
  2026-09-22 已同步进契约（智拓仓分支 `feat-chatx-commandbus-kinds-2026-09-22`：契约 §3 五种 kind 表 + §4 STOP 双保险；`src/integrations/chatx/commandbus.py` 五种 kind 解析、
  `command_executor.py` 本机 stop 名单 / note 台账 / reengage 挑话术；任务 `chatx_command_poll`（`params.account/limit/dry_run`）；`tests/test_chatx_commandbus.py` 23 例）。
  端到端已对着本机 player 实例真跑通：`scripts/ops/chatx_commandbus_e2e.py`（入箱 → pull 顺序 stop→note→reengage → 执行(假 sender) → ack → 二次 pull 空 → ack 幂等）。
  手机侧 `account` 默认=设备序列号，任务 `params.account` 可改成智聊侧 WA/TG 账号名（两边约定同一串即可）。
- **STOP 一律先发 stop**：入箱 stop 时同号未拉走的 reengage/note 全部 cancelled，stop 排最前，该号进 stopped 名单 → 之后 reengage 一律拒（`clear_stop` 显式解）。
  触发：① hooks 入站整句 `STOP / unsubscribe / tanggalin / wag mo na ako i-message / 别再发了…`（`is_stop_message`，整句匹配宁漏勿误）且有手机号 → stop；
  ② sync 置 dormant 且有手机号 → reengage（`reengage_on_dormant`）；③ 运营 `POST /api/player-care/commands {kind, account, phone, text|messages}`（写权限），`GET` 同路径看出箱/统计。
- 配置 `player_care.commandbus.{enabled=false(预设默认关；本机 config_player 已开——智拓分支合入且手机侧排上 chatx_command_poll 后才会真发), db_path, reengage_on_dormant=true, stop_on_keyword=true, pull_limit=20}`；关着时 pull 永远空、ack 只回 available（fail-soft，手机 poll 空转）。
- 手机侧接入步骤：智拓 `config/chatx.yaml` 填 `bus_url=http://<player 实例>:18797` + token（=player 实例 `web_admin.auth_token`）；对设备 `POST /tasks {type: chatx_command_poll, params:{account, dry_run:true}}` 先跑一轮 dry_run 看台账，再关 dry_run / 进 `scheduled_jobs.json` 定时。
- 执行权归手机：本域仍没有任何直接发消息的代码；失败全吞。测试 `tests/test_player_care_commandbus.py`。

## 6c. B5 handoff：Messenger → WA/TG（已做）

- 复用核心 `src/contacts/handoff.py` 的 `handoff_tokens`（6 位码、72h、原子消费）：story 实例在 Messenger 发引流话术时已签码；对方在 WA/TG 首条消息里打回来 → `domains/player_care/handoff.py::PlayerHandoffService.try_merge`（hooks 入站落画像后调用，同一 user_context 只合一次，失败全吞）。
- 找码顺序：**本实例 contacts.db** → `player_care.handoff.source_db_path`（story 实例的 contacts.db，相对配置目录；不存在就只查本库）。
  - 同库命中（`via=local`）：确保 WA/TG `ChannelIdentity`，走核心 `MergeService.apply_token_merge` 把它 relink 到 Messenger 的 Contact（真身份合并，`linked_via=token` 0.95）；
  - 跨库命中（`via=source_db`）：来源库只消费 token + 记 `handoff_consumed` 事件 + 漏斗 HANDOFF_SENT→LINE_ENGAGED（核心把「私域接上」统称 LINE_*），不 relink，本地画像记 `handoff_contact_id / handoff_ci_id` 关联。
- **手机号主键**：WA JID 本身是号；TG 没号时借 Messenger 侧留资属性 `phone`（`capture_lead`）→ `PlayerProfileService.resolve_key` 把占位键 `telegram:<id>` 并进手机号键（计数/时间/阶段按 rebind 规则合）。两边都没号 → 仍占位键，但 handoff 字段照记。
- 画像新增列（老库幂等 ALTER）：`handoff_source / handoff_token / handoff_contact_id / handoff_ci_id / handoff_via / handoff_at`；阶段至少推到 `chatting`（只前进）。
- 合并那一轮给人设一条提示（`HANDOFF_CONTEXT_BLOCK`）：像老朋友换地方继续聊，不问“你是谁”、不提码/系统/迁移、无数字。
- 配置 `player_care.handoff.{enabled=true, source_db_path="", link_local_identity=true}`。不动 story 实例任何画像；不发消息。测试 `tests/test_player_care_handoff.py`。

## 6d. B6 看板（已做）

- `GET /player-care/overview`（HTML，页面鉴权 `page_auth`）+ `GET /api/player-care/overview`（JSON，`api_auth`：登录会话或 Bearer），
  都在 `domains/player_care/web/routes.py`；页面模板 `domains/player_care/web/templates/player_care_overview.html`（域模板目录由 admin 自动加进 Jinja loader）。
  manifest `web.pages` 挂侧栏「玩家朋友看板」（运营区）；页面键 `player_care_overview` 的可见角色由 manifest `roles: [master, admin, supervisor, viewer]` 声明，
  核心 `register_domain_page_permissions` 在 `create_app` 读清单时注册进 `PAGE_PERMISSIONS`（见 §5）；agent 不给（只用工作台）。
  同样只在 `domain=player_care` 实例出现，story_matrix 实例无此页。
- 聚合在 `domains/player_care/overview.py::build_overview`（只读，不查网关不发指令，库打不开 → 空计数 + `notes`）：
  - `contacts`：总数 / 有手机号 / handoff 接入 / 7 日活跃 / 按账号；
  - `stages`：七阶段分布（总 + 按账号）；
  - `today`：当日 `inbound / lookups / found / visible（明用命中）/ gate_hits（数字闸命中）/ new_profiles / stage_ups` + 按账号行（复用 `daily_report`）；
  - `gateway`：启用 / url / 密钥是否已设（不回密钥本身）/ 累计查询 / 最近查询时间 / 24h 内各画像最后一次 lookup 的 found⁄not_found⁄各 error 分布；
    `status`：`unconfigured`（没配）/ `idle`（24h 没查过）/ `ok` / `degraded`（有传输或鉴权错）/ `down`（错占比 ≥ 50%）；`not_found` 不算错——查不到是正常业务；
  - `sync`：开关 / 间隔 / 本进程上一轮 `run_player_sync` 摘要（`sync.last_sync_summary()`，含 `ts`）；
  - `commandbus`：开关 + 出箱 `stats()`（开着才查）。
- 库侧新增 `ContactStore.player_overview_counts(active_since, lookup_since)` 一次 SQL 聚合，不加表不加列。
- 测试 `tests/test_player_care_overview.py`。

## 6e. 看板上报智控（跨实例汇总，已做，2026-09-22）

- 方向选「智控拉、智聊不推」：与 commandbus/leadbus 同向（智控 → 智聊），智聊侧零新代码、零新出口 token；
  本域 `GET /api/player-care/overview`（Bearer = 本实例 `web_admin.auth_token`）就是上报口。
- 智控侧（智拓仓同分支 `feat-chatx-commandbus-kinds-2026-09-22`）：`src/integrations/chatx/player_care_client.py` 按 `config/chatx.yaml`
  `player_instances: [{name,url,token|token_file}]` 逐台拉，联系人/七阶段/当日/出箱计数相加、网关健康取最差、按账号阶段合并；
  `GET /chatx/player-care/overview`（60s 缓存，`?refresh=1`）/ `GET /chatx/player-care/instances`。单台不通只标
  `unreachable / needs_token / no_player_care(打到 story 实例=404)`，其余照常汇总。本机对 18797 + 18796 实拉验证通过。
- 接入：每加一台 player 实例，在智控 chatx.yaml 追加一条 `{url, token_file}` 即可；不需要改本域。

## 6f. 运行态小修（2026-09-22）

- `python main.py --config config_player\config.yaml` 以前只对 `--init/--check` 生效，运行态 `AIChatAssistant` 走 `ConfigManager()` 缺省路径 → player 实例会去绑 18796 / 写 `config/` 的库。
  现在 `__main__` 把 `--config` 注入 `AITR_CONFIG_PATH`（ConfigManager 缺省路径的第一优先级），两套实例真正并行；`config_player/` 整目录进 `.gitignore`（含 token 与运行库）。

## 7. 待老板 / 117 机补的输入

- ~~`GATEWAY_KEY`~~ 已在网关项目 `D:\用户数据拉取\.env`（player 实例机上要把它设进环境变量 `GATEWAY_KEY`，不写配置文件）；
- ~~`/lookup` 三种真实返回样例~~ 2026-09-21 已跑真探针：found 200 / not_found 404 / bad_key 401，脱敏样例已进 `tests/fixtures/player_care_lookup_samples.json`，解析按 §3 定稿；
- ~~是否有 `agent` 字段~~ 有：`players[i].agent`，已接进画像 `agent` 列；owner 归因用它；
- 真 LLM 明用 / 暗用各跑一轮确认数字闸不误杀（需 player 实例配上真模型，待做）；
- player 实例要用的 WA / TG 账号，以及每个账号真实在玩的 2–3 个游戏名（填 persona `style_hint`；网关实测到的厂商 / 游戏名见 §3，可从中选）。
