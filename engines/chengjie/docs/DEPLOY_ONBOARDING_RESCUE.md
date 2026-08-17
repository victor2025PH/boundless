# 部署清单：接入救援三批（P0–P3）上线前置

> 2026-08-10 大陆用户 `API_ID_INVALID` 事故沉淀的四轮改动（P0 止血 → P1 自愈 →
> P2 商业化 → P3 归因/工具化）**代码已全部落盘、测试全绿，但没有一行在生产生效**。
> 本文是让它们真正帮到用户的部署顺序与验证步骤。分三个交付面，各自独立。

## 交付面 A：官网（VPS，Next.js）—— 立即可部署，收益最大

改动全在 `website/`，纯 server 侧，不依赖客户端出新版即可生效（对**已装机**用户
的托管派发链当场改善：换发/隔离/智能派发都在服务端）。

**部署**：VPS 上 `git pull && npm run build && 重启站点`（pm2/systemd 按现状）。

**可选 env**（都有安全默认，不配也能跑）：
| env | 默认 | 作用 |
|---|---|---|
| `POOL_TG_QUARANTINE_THRESHOLD` | 3 | 几台不同机器举报同组 → 自动隔离（P1） |
| `ACTIVATION_SLO_MIN_PCT` | 50 | 激活率破线阈值（P2） |
| `ACTIVATION_SLO_MIN_CLAIMS` | 5 | 样本不足不下判（P2） |
| `POOL_TG_CREDS[_FILE]` 组内 `proxy` | 无 | 组级出口，优先派给直连不通的机器（P2-⑨；需采购出口 IP） |

**验证**：
1. `/console/funnel` 打得开（激活漏斗 + 48h cohort + 挽回名单）。
2. `/console/trial` 池条出隔离红标/出口蓝标位（有数据时）。
3. `POST /api/pool/telegram-cred` 带 `invalid_api_id` 能换组（可用 curl 拿测试指纹试）。
4. 每日健康日报（`POST /api/admin/health-check?digest=1`）里出现「激活 SLO」行。

## 交付面 B：生产实例 zhiliao（客户端 .py）—— 搭批量重启车

P0/P1/P2 的引擎侧 `.py`（`telegram_protocol_login` / `hosted_gateway` /
`credpool_bridge` / `config_manager` / `telemetry_beacon` / `inbox/store` /
`login_funnel_stats` / 登录路由 / ops 路由）**已落盘，待下次 zhiliao 批量重启装载**。

- 多条 sibling 线也在等这次批量重启（见 `agent_probe` 意向板）——**搭车，别单独重启**。
- 重启前**必跑**：`scripts\restart_preflight.ps1`（GO/NO-GO：树静默/语法/装配/冷却/间隔）。
- 唯一入口：`deploy\instances\restart_instance.ps1 -Instance zhiliao`。
- 装载后只读验证（不必再吃一次重启窗口）：
  - `GET /api/platforms/telegram/login/preflight` 返回 `{ok, reachable}`；
  - `GET /api/admin/diagnostic-upload?probe=1` 返回 `{ok:true}`；
  - 工作台接入弹窗制造一次失败，看分因文案 + `/console/errors` 出带 `reason=` 的 ERROR。

## 交付面 C：桌面安装包 1.0.20 —— 终端用户拿到自愈能力

面 B 的 `.py` 进生产实例只惠及 zhiliao 的坐席；**终端桌面用户**要等新安装包：
- 无感换发、接入预检黄条、一键诊断上传、激活里程碑上报（account_online/first_reply）
  全部随 1.0.20 客户端才到用户手里。
- 出包流程照 `desktop/` 现有 build/release；出包后走灰度推更新。

## 当前那位大陆用户（1.0.19）怎么办

面 A/B/C 都到不了他手上（旧版 + 需重启）。按 `RUNBOOK_TG_CRED_INCIDENT.md`：
1. VPS 上查粘定表定位坏源（§1-§2）；
2. 是池组废 → 撤组（§3a），粘定表自愈；
3. 通知他重启应用重扫。

## 数据成熟期（面 A/C 上线后）

P2-⑩⑪ / P3-⑫⑬ 的读数（SLO、cohort、挽回、破线归因）都依赖 1.0.20+ 客户端里程碑，
**出包 + 用户升级后约一周**才有可读数字。这一周的观察是下一阶段（出口效果拆分、
隔离误报率调阈）的前置——数据到位再动手，别对着空表做分析。
