# Runbook：Telegram 接入/凭据池事故应急处置

> 沉淀自 2026-08-10 实锤事故：大陆新装用户扫码「登录失败」，查看详情为
> `[400 API_ID_INVALID]`——官网凭据池派发了一组废凭据（或用户自填错值），
> 客户端旧版只记 DEBUG，事故靠用户拍照上报才被发现。
> 本文是「新用户接不进来」这一类事故的标准处置流程（SOP）。

## 0. 事故分级（先定级再动手）

| 级别 | 判据 | 本质 | 响应时限 |
|---|---|---|---|
| P0 | 所有新用户都接不进（池全废 / 网关挂 / 官网挂） | 获客通道归零 | 立即，1h 内止血 |
| P1 | 一部分用户接不进（单组凭据废、特定网络环境） | 按组/按地区流失 | 当天 |
| P2 | 个别用户（自填错凭据、本机环境特殊） | 客服个案 | 48h |

## 1. 定位（10 分钟）

服务端数据都在 **VPS 官网数据目录**（默认 `~/hualing-leads/`，env `LEADS_DIR` 可改）：

```bash
# 已知用户 IP → 反查机器指纹（开机心跳/错误回传都带 fp）
grep '"ip":"<用户IP>"' ~/hualing-leads/client-logs.jsonl | tail -5

# 该指纹被粘定派发到哪组凭据（无记录 = 没走池，用户自填的）
sqlite3 ~/hualing-leads/ai-gateway.db \
  "SELECT mid, api_id, datetime(assigned_at,'unixepoch') FROM tg_cred_assign WHERE mid='<指纹>';"
```

远程看板：`bd2026.cc/console/errors`（客户端错误回传，按机器/版本/错误聚类）。
自 2026-08-10 批次起，扫码失败以 **ERROR 级**回传并带归因码：

| reason_code | 含义 | 处置 |
|---|---|---|
| `cred_invalid` | api_id/api_hash 本体无效（池组废 / 自填错） | 走本文第 3 节 |
| `tg_unreachable` | 连不上 Telegram DC（被墙/代理没开） | 指导用户配代理（弹窗文案已内置指引） |
| `rate_limited` | FLOOD 限流 | 等待，无需处置 |
| `creds_missing` | 凭据根本没派发到 | 查官网池是否配置 / 托管链（下方 doctor） |

客户端侧链路自检：`python -m scripts.hosted_chain_doctor --live`（哪一环断了逐条给修法）。

## 2. 圈爆炸半径（5 分钟）

```bash
# 每组各挂多少台机器——坏组的机器数 = 受影响用户数
sqlite3 ~/hualing-leads/ai-gateway.db \
  "SELECT api_id, COUNT(*) FROM tg_cred_assign GROUP BY api_id;"
```

同时给**全部组**做真伪体检（工具见第 5 节）：可能不止一组坏。

## 3. 止血

### 3a. 池组废（`tg_cred_assign` 里有该指纹的记录）

1. 在 VPS 把坏组从 `POOL_TG_CREDS_FILE`（或 env `POOL_TG_CREDS`）撤下/替换。
2. **粘定表不用手动清**：`assignCred` 发现旧组已不在池里会自动删粘定行并重新
   分配健康组（`website/lib/tg-cred-pool.ts`）。
3. 通知受影响用户**重启应用**再扫码（1.0.19 及以下）。**1.0.20+ 客户端自带无感
   换发**：扫码撞 `API_ID_INVALID` 当场向官网举报换新组并同轮重试（120s 冷却），
   同组被 ≥N 台机器举报会**自动隔离停发 + 管理员 TG 告警**（阈值 env
   `POOL_TG_QUARANTINE_THRESHOLD`，默认 3；误报解除用 `unquarantine`）——多数
   情况下用户点一次「刷新二维码」即自愈，无需重启。

### 3b. 用户自填错值（粘定表无记录 / 池未配置）

自填值写在用户本机 overlay（`config.local.yaml` 的 `telegram.api_id/api_hash`），
**只要它存在，托管注入永远让位**。两条路：

- 客服引导在接入弹窗的「填 API ID / Hash」表单里填正确值；
- 或清掉 overlay 里那两行 → 重启 → 池重新派发。

## 4. 触达（当天，事故变服务）

试用台账里**每个指纹都留了联系方式**（这正是资格闸设计的价值）：

```bash
# 指纹 → 联系方式（TG/WA/邮箱）
grep '<指纹>' ~/hualing-leads/trial-claims.jsonl | tail -1
```

按第 2 步的受影响指纹清单逐个反查，客服**主动**通知：
「我们发现并修复了一个接入问题，请重启应用再扫一次二维码即可」。
用户还没来投诉、我们先上门。

## 5. 预防（每次复盘必查这三件）

### 5a. 池凭据真伪探针（上架门禁 + 日巡检）

`scripts/tg_cred_probe.py`（自包含单文件；依赖 `pip install pyrogram tgcrypto`）：

```bash
# 上架门禁：换池/加组前必跑，exit 1 = 有废组，禁止上架
python -m scripts.tg_cred_probe --file new_creds.json --proxy socks5://127.0.0.1:7890

# VPS 日巡检（直连可达 Telegram；坏组自动经 /api/ops/alert 推管理员 TG）
# crontab -e：
0 7 * * * cd /opt/chatx && python3 tg_cred_probe.py --file /etc/chatx/tg_creds.json \
    --alert-url https://bd2026.cc/api/ops/alert --alert-key "$EVENT_INGEST_KEY"
```

判定：`ok`（真实有效）/ `bad`（API_ID_INVALID 族，必须下架）/ `rate_limited`
（限流，本轮不下判）/ `unreachable`（探针环境问题，凭据无罪）。
exit：`0` 全好；`1` 有废组；`2` 一组都没探成功（巡检环境自身坏了也会响）。

> 背景：官网池上架只验**格式**（32 位 hex），从不验 Telegram 认不认——真伪只能靠探针。

### 5b. 远程观测别再失明

- 扫码失败已 ERROR 化 + 归因码（`telegram_protocol_login.classify_login_exception`），
  beacon 自动回传 → `/console/errors`。**同码多机聚集 = 池事故**，不用等客诉。
- 弹窗文案已按归因码给行动指引（`cred_invalid` → 重启/联系客服；
  `tg_unreachable` → 配代理步骤 + 常见端口预设），i18n 键
  `inbox.connect.err_cred_invalid` / `err_tg_unreachable`（zh+en）。

### 5d. 组级出口代理（P2-⑨，大陆客群根治）

池条目可带 `proxy`，**带出口的组会优先派给直连不通的机器**（客户端登录时自报
`tg_direct`，服务端软偏好匹配；出口组满员时照常回落无出口组，接入优先）：

```json
[
  {"api_id":"111","api_hash":"…","max":50,"name":"direct"},
  {"api_id":"222","api_hash":"…","max":30,"name":"cn-proxy",
   "proxy":{"scheme":"socks5","host":"1.2.3.4","port":1080,"username":"u","password":"p"}}
]
```

出口随凭据下发、随账号落库（换组后 runner 仍按登录时的出口连，IP 恒定）。
`/console/trial` 池条给带出口的组标「出口」蓝标。半个代理（缺 host/port）自动丢弃。

### 5e. 隔离组的 console 操作（P2）

`/console/trial` 池条：被隔离的组标红 + 「解除隔离」按钮（二次确认）。
**解除前必须先跑 `tg_cred_probe` 核实为误报**——真废组解除了只会被再次举报隔离。
真废组的正确处置是换池（撤下坏 `api_id`），不是解除。

### 5f. 接入 SLO 与挽回（P2-⑩⑪）

- **激活漏斗**：`/console/funnel`——装机→领试用→派发→首账号接入→首条出站
  五段（7/30 天）+ 48h 同期群激活率 + **挽回名单**（领了试用没接入的机器 + 台账
  联系方式，客服外呼素材）。
- **SLO 告警**：每日健康日报（`/api/admin/health-check`，cron 触发）并入
  「领试用→接入」成功率；低于阈值（env `ACTIVATION_SLO_MIN_PCT`，默认 50%）
  单独推一张破线告警卡，指路 funnel→errors→挽回名单。口径只算新版客户端
  （老版本机器无里程碑数据，混进分母会假红），样本不足（`ACTIVATION_SLO_MIN_CLAIMS`，
  默认 5）不下判。

### 5c. 每次复盘的必答题

> 「这个故障为什么是用户告诉我们的，而不是看板告诉我们的？」

缺哪个探针补哪个，本 runbook 随之更新。

## 6. 相关工具/文件索引

| 东西 | 位置 |
|---|---|
| 凭据池实现（粘定/自愈/容量） | `website/lib/tg-cred-pool.ts` |
| 派发接口 | `website/app/api/pool/telegram-cred/route.ts` |
| 池台账（SQLite） | VPS `~/hualing-leads/ai-gateway.db` 表 `tg_cred_assign` |
| 客户端错误回传 | VPS `~/hualing-leads/client-logs.jsonl` + `/console/errors` |
| 试用台账（指纹→联系方式） | VPS `~/hualing-leads/trial-claims.jsonl` |
| 令牌签发流水（含 IP） | VPS `~/hualing-leads/ai-gateway.jsonl`（`ev:"mint"`） |
| 真伪探针 | `scripts/tg_cred_probe.py`（本仓；VPS 上 scp 单文件即可用） |
| 托管链自检 | `scripts/hosted_chain_doctor.py --live` |
| 归因分类 + 回归网 | `src/integrations/telegram_protocol_login.py::classify_login_exception`，`tests/test_tg_login_reason_codes.py` |
| 诊断包（用户侧一键导出） | 设置 → 诊断包（`/api/admin/diagnostic-bundle`）；直传接口 `bd2026.cc/api/diag-upload`（回 6 位短码，自动推客服 TG） |
