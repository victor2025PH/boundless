# 抖音 / TikTok 渠道族进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条线的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。老板改口在「拍板记录」追加一行。
> 建立于 2026-09-07。方案底座 `docs/实施96_抖音接入全自动聊天系统_三视角分析与优先级方案_2026-09-07.md`（§2.3 抖音 × TikTok 对照、§4 目标架构、§5 分期、§6 刻意不做）；TikTok 指令 `docs/指令_TK-1_TikTok接入_官方通道与地区模型_2026-09-07.md`。

## 一句话现状

抖音与 TikTok 是**两个平台**（账号、开放平台、协议、规则、法律全不同），按「一个渠道族、两套传输层」并行：**DY 线**（抖音，方案对话本身）负责公共层（`platform_registry.py` / `channel_policy.py` / 回复窗倒计时组件 / 卡片气泡框架 / 来源标签）+ 抖音传输层；**TK 线**（TikTok，新对话）负责 TikTok 传输层（Business Messaging API / TikTok Shop CS API / 地区模型 / huoke 桥 opt-in），公共层只追加 `tiktok` 条目、绝不创建。共用的二十处平台散表各加自己一行，动前 `agent_probe`。

**前置资质（老板侧，越早越好）**：抖音——企业主体小程序上线 + 能力实验室（私信/群管理、主动授权私信、留资卡、图片上传）+ 抖店服务商入驻 + 飞鱼线索推送规则；TikTok——TikTok for Business 开发者账号 + Business Messaging 权限申请 + 自家 Business Account（注册地须在可用区）+ Partner Center 应用（Customer Service 类目）+ 测试店铺。

## 进度表

| 线 | 主题 | 指令 / 方案 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **DY** | 抖音接入（公共层 + 抖音传输层） | `实施96` §5 P0–P3 | 按批另定 | **P0-3 公共层已落地并提交**（09-08 02:xx，三 commit `26347522` 注册表 / `9080bdb8` 策略层 / `fa0860d9` 演示 worker；均经 `stage_hunks --preview` 在 HEAD+暂存的独立树上跑绿后再提交，共享文件只提本线 hunk）；P0-1/P0-2 待老板；P0-4 未开工 | **P0-3 三件**：① `src/integrations/platform_registry.py` 平台注册表（13 平台含 qqbot/wechat_kf 与规划中的 qq/wechat/douyin/tiktok，`implemented` 标位）+ `scripts/platform_registry_export.py` → `src/web/static/platform_registry.json` + 门禁 `tests/test_platform_registry.py`（14 例：散表不漂移、已知色漂移登记 `ACCEPTED_COLOR_DRIFT`、桌面图标副本滞后登记、事实卡随注册表）；② `src/inbox/channel_policy.py` 渠道出站策略层（抖音 1000 字·禁链·24h/6·进私 30s/3·2 气泡·仅图/视频·enforce；TikTok 48h/10；wechat_kf 与 `kf_window_guard` 同源数值有门禁；qqbot 60min/4；whatsapp:official 24h 窗）+ 接线：编排器 `send`/`send_media` 拒收（+30 行）、`reply_split.cap_max_parts_for_platform` + `should_split_for_delivery`、发送路由封顶、`autosend_policy.decide(platform=)` + B 线 `_risk_policy_decide(platform)`；门禁 `tests/test_channel_policy.py` 14 例；③ `src/integrations/douyin_mock_worker.py` 抖音演示 worker（默认关，`platform_login.douyin.mock_enabled`）+ `ensure_builtin_workers` 注册钩子 + `tests/test_douyin_mock_worker.py` 4 例（进线→落库→外链被拦→回复→镜像→回声全链）；④ 验收补件：`tests/test_douyin_e2e_alignment.py` 3 例——用 conftest **完整 admin app** 走边车同款路径 `POST /api/internal/protocol/ingest` → `GET /api/unified-inbox/chats`（can_send / send_modes / automation_mode 与其它平台同形）→ `GET /thread` → `POST /send` 外链 409 `send_blocked/policy_link_denied` 出平台规则人话 → 正常送达镜像回线程；拦因族谱 `send_gate_status.blocked_reason_key` 新增 `policy_link / policy_len / policy_media / policy` 四族 + `errors.py` zh/en 文案。回归：相关 18 个既有测试文件 342 通过；`test_platform_registry_consistency.py` 4 红为实施97 线 `wechat_kf` 在途未同步（非本线） | 待索引空闲后把 `channel_policy:` / `platform_login.douyin.mock_*` 注释块补进 `config.example.yaml`（本次因该文件被他线暂存而撤回）；散表迁移到注册表（PC/PN/PLAT_COLOR 收口后删 `ACCEPTED_COLOR_DRIFT`）；`kf_window_guard` 按 `channel_policy.window_rule()` 参数化覆盖抖音/TikTok（需与实施97 线协调）；System Z `drafts.py` 的 `decide(platform=)` 接线需 D-M10 式例外；P0-4 边车与桌面 assistOnly（需真实抖音网页 DOM 采样） | 待写 `发版对账_v1.0.77_DY1.md` | — |
| **TK-1** | TikTok 接入（官方通道 + 地区模型 + huoke 桥 opt-in） | `指令_TK-1` | $220 | 未开工 | — | A 地区模型 + 登记 / B Business Messaging worker + webhook + 令牌 + 向导卡 / F 图标·i18n·事实卡·门禁 / D 策略参数 + 回复窗字段 / C Shop CS API / E huoke `reply_engine=chengjie` | `发版对账_v1.0.77_TK1.md` | — |
| **TK-2**（待立） | TikTok 网页托管边车 / 公开评论回复 API / Messaging Ads 归因深化 / Shop 订单连接器 / TK-1 溢出 | 待写 | — | 待立 | — | — | — | — |

## 老板拍板记录

（默认值见 `实施96` §5 P0-1 与 `指令_TK-1` §3 D-TK-1～D-TK-8；老板改口在这里追加一行：日期 / 决策号 / 改为）

- 待拍板（DY）：企业主体小程序由谁持有（建议自建「无界智聊」小程序）；人民币收款主体与开票。
- 待拍板（TK）：TikTok 官方接入是否从 P3 提前到与抖音 P1 并行（TK-1 即按并行写；不并行则 TK-1 延后开）。

## 跨线交接

- DY → TK：`platform_registry.py` / `channel_policy.py` 落地时刻与条目格式；回复窗组件参数化接口（窗长、条数）；来源标签枚举。
- TK → DY：二十处散表「待迁入注册表」清单；`tiktok_policy.py` 待并入参数；`reply_window_deadline` / `window_sent_count` 两个 source 字段名；TikTok 图标族与地区徽标。
