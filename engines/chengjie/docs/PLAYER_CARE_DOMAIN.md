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
{"q": <客户消息>, "phone": <可选 639…>, "uid": <可选>}   →  {"chatx_text": "..."}
```

- 手机号任意写法（9… / 09… / +63 9… / WA JID）归一为 `639xxxxxxxxx`；会员号只在带 `uid / member id / account no` 前缀时抽取；
- 默认关：`enabled` 不为真或缺 url / key → 不发请求；401 / 超时 / bad json → 一律「没资料」；404 = 网关通但没这个人；
- 同联系人 60 s 缓存；密钥只从 `player_gateway.key` 或环境变量 `GATEWAY_KEY` 读，不进日志不进 git；
- 与 117 机 chengjie 树 `wujie_player.py` 用同一组配置键，以后合并不打架。

## 4. 两道闸（`hooks.py`，由 `generate_inbox_draft` 3f/9d 与 `process_message` 派发）

| 分支 | 触发 | 进提示词的块 | 数字闸 |
|---|---|---|---|
| **明用** `visible` | 对方问自己账户（balance / deposit / withdraw / turnover / bonus…）**且**有手机号或会员号，网关查到 | 「只读事实」块 = `chatx_text` 原文 + 「像帮朋友问了一下」口吻 | 回复里出现事实外 ≥3 位数字 → 整句换安全句 |
| **无资料** `missing` | 问账户但查不到 / 超时 / 网关未配 | 「没查到，稍后再看，绝不编数字」 | 任何 ≥3 位数字 → 安全句 |
| **不知是谁** `need_identity` | 问账户但没有手机号 / 会员号（TG 数字 id） | 「不编数字；朋友口吻问一句是哪个号」；即使网关按 q 命中也**不给数字** | 同上 |
| **暗用** `hidden` | 没问账户但有身份线索且查到 | 只有一条**不含数字**的隐藏提示：对方玩过哪些游戏，「你不知道来源、不能提及、对方先聊到才接」 | 不启（日常数字放行） |
| 无线索 | 既没身份也没问账户 | 不打网关，不注入 | 不启 |

每轮结果写进持久 `user_context["_player_facts"]`（phone / uid / found / text / games / ts）供画像与阶段用；提示块 `_domain_context_block` 用完即清，不跨轮泄漏。
`get_escalation_line()` 返回空——基类的中文客服话术绝不带出去。

## 5. 顺手修的核心缺口（对所有域生效）

`DomainHook.on_message_pre_process` / `on_reply_post_process` 在 `src/hooks/base.py` 定义已久，但**核心从没派发过它们**——story_matrix 域的身份词 / 联系方式兜底 `on_reply_post_process` 一直是死代码（真正挡住的是智拓侧 `chatx_reply_blocked` 硬闸）。本分支在两条产线接上：

- B 线 `SkillManager.generate_inbox_draft`：3f（pre，enrich/目标注入之后、意图识别之前）+ 9d（post，危机兜底之后）；
- A 线 `SkillManager.process_message`：hook ctx 构建后（pre）+ 5d 危机兜底后（post，5d2）；
- `ai_client._build_context_prompt` 新消费 `_domain_context_block`（目标块之后、坐席指令之前）。

基类默认返回 None / 原文 → 无域包或域包未覆写时逐字节零影响。**副作用**：story 实例升级到本分支后，story_matrix 的 post hook 开始真的生效（更严：识别到 AI 字样 / 链接 / 号码的回复会被换成频道安全句）。

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
  ⚠ 契约目前只列了 `greet / reply`，这三种 kind 是按老板要求新增的，**待老板同步进契约 + 智拓侧 executor**；本域没改契约文件。
- **STOP 一律先发 stop**：入箱 stop 时同号未拉走的 reengage/note 全部 cancelled，stop 排最前，该号进 stopped 名单 → 之后 reengage 一律拒（`clear_stop` 显式解）。
  触发：① hooks 入站整句 `STOP / unsubscribe / tanggalin / wag mo na ako i-message / 别再发了…`（`is_stop_message`，整句匹配宁漏勿误）且有手机号 → stop；
  ② sync 置 dormant 且有手机号 → reengage（`reengage_on_dormant`）；③ 运营 `POST /api/player-care/commands {kind, account, phone, text|messages}`（写权限），`GET` 同路径看出箱/统计。
- 配置 `player_care.commandbus.{enabled=false(默认关，智拓 executor 对齐后再开), db_path, reengage_on_dormant=true, stop_on_keyword=true, pull_limit=20}`；关着时 pull 永远空、ack 只回 available（fail-soft，手机 poll 空转）。
- 执行权归手机：本域仍没有任何直接发消息的代码；失败全吞。测试 `tests/test_player_care_commandbus.py`。

## 6c. 尚未做（B5–B6，按序）

4. **B5 handoff**：Messenger → WA/TG 迁移用 `handoff_tokens` 合并身份，手机号是主键；
5. **B6 看板**：每实例 `/player-care/overview`（联系人数 / 阶段分布 / 明用命中 / 数字闸命中 / 网关健康），上报智控。

## 7. 待老板 / 117 机补的输入

- `GATEWAY_KEY`（写进 player 实例机器的环境变量，不发聊天记录）；
- `/lookup` **三种真实返回样例**：查到 / 查不到 / 出错或部分字段——`extract_games` 的解析规则按真实 `chatx_text` 格式定稿；
- 是否有 `agent`（代理号）字段——有则 owner 归因用它，没有则用「我方手机槽位 + 我方号码」；
- player 实例要用的 WA / TG 账号，以及每个账号真实在玩的 2–3 个游戏名（填 persona `style_hint`）。
