# 指令 TK-2：TikTok 二期——网页托管边车（assistOnly）· 公开评论回复 API · Messaging Ads 归因 · Shop 订单连接器 · TK-1 溢出

> 状态：**只立项，不开工**（2026-09-10 由 TK-1 续做收工时写）。开工前置：① Business Messaging 权限获批且 TK-1 A（真流量联调）/ B（`auto_message` / `capabilities` 真值）收尾；② 老板拍板本条预算与顺序。
> 类型：TikTok 传输层二期（三条新通道 + 一个连接器 + 一期溢出）。TikTok 与抖音仍是**两个平台**；抖音（DY）线冻结，本条不碰 DY 代码，公共层（`platform_registry` / `channel_policy` / `window_guard`）只追加或参数化 `tiktok` 相关条目。
> 底座：`docs/指令_TK-1_TikTok接入_官方通道与地区模型_2026-09-07.md`（§2 事实表、§3 D-TK-1～8 仍有效）、`docs/指令_TikTok线_续做_2026-09-10.md`、落点表 `docs/发版对账_v1.0.80_TK.md`（TK-1 全部落点）、总账 `docs/抖音TikTok渠道族_进度总账.md` TK-1 / TK-2 行。
> 建议预算 **$240**（边车 $90 / 评论回复 API $50 / Messaging Ads 归因 $30 / Shop 订单连接器 $40 / 溢出 + 门禁 $30）。开工时建 `docs/发版对账_v1.0.8x_TK2.md`。

## 0. 为什么要二期（一句话各说一条）

- **网页托管边车**：Business Messaging API 只覆盖注册地可用区（EEA / 瑞士 / 英国 / 美国 / 印度不可用）的 Business Account；非可用区客户、以及未申到 API 权限的客户，唯一合规可行的「看得见收件箱」路径是浏览器托管（客户自己登录、边车只读 DOM + 辅助起草），与 Messenger Web / WhatsApp Web 边车同族。
- **公开评论回复 API**：TK-1 E 段 huoke 桥把「回评论」留给真机（准入区 + `notice_unofficial`）；官方 Comment API（Marketing Partner 资质，越南 / 泰国 / 印尼灰度）一旦对我们可用，主站就能放官方评论回复，huoke 真机退为兜底。
- **Messaging Ads 归因**：TikTok Messaging Ads（点广告直接开私信）是海外客户投放主场景；私信首条带 `ad_id / campaign_id`，进收件箱要打「来源=广告」标并回流到成交漏斗，否则客户看不出投放 ROI。
- **Shop 订单连接器**：TK-1 C 段订单卡只读（一次拉摘要）；`ecommerce_tools` 的 TikTok 连接器把订单 / 物流 / 售后查询做成正式工具，供小智与坐席在会话里调用。
- **TK-1 溢出**：Shop 会话按事实无 48h 窗却仍受 `channel_policy(tiktok)` 48h/10 保守判定（TK-1 C 段已知项）；huoke 侧 `reply_engine` 开关（兄弟仓自己 review）；A/B 权限后收尾项。

## 1. 决策（默认值；老板改口在总账「拍板记录」追加一行）

| 编号 | 决策 | 默认 | 理由 |
|---|---|---|---|
| **D-TK2-1** 边车定位 | `services/tiktok-web` 网页托管边车 **assistOnly**：只读 DOM 进收件箱 + 起草 + 坐席一键粘贴；**不自动发送** | 合规分层同 D-TK-3：非官方路径不承诺不封号；assistOnly 把风险压到「读」 | 与 Messenger Web 边车 `assistOnly` 口径一致 |
| **D-TK2-2** DOM 采样 | 只用**可用区真账号**、真浏览器采 TikTok 网页收件箱 DOM，采样进 `services/tiktok-web/fixtures/`；DY 的 P0-4 边车经验（未开工）**不可借** | 抖音网页与 TikTok 网页是两套前端 | 门禁：fixtures 缺失 → 边车测试 skip 而非假绿 |
| **D-TK2-3** 评论回复 API | 只在 Marketing Partner 资质到手后做；进主站 `official`；无资质时 huoke 真机（TK-1 E 段桥）仍是唯一回评论路径 | 资质门在外部 | 与 D-TK-3「主站只放官方」一致 |
| **D-TK2-4** 广告归因 | 私信 `source.ad = {ad_id, adgroup_id, campaign_id}` 进 `source`（这是**上游原始字段**，不是窗口状态——不违反 TK-1 D 段「窗口状态不进 source」决议）；来源标签枚举新增 `ads` | 归因是消息属性，稳定不漂移 | 漏斗 `unified_inbox_conversion_outreach_routes` 按 `source.ad` 聚合 |
| **D-TK2-5** 订单连接器 | `ecommerce_tools` 新增 `tiktok_shop` 连接器：订单 / 物流 / 售后**只读**；写操作（取消 / 退款）不做 | 客服机器人误操作订单不可逆 | 复用 TK-1 C 段 `TikTokShopApi` 签名与令牌 |
| **D-TK2-6** 回复窗按 source 放宽 | `channel_policy.window_rule(platform, *, source=)`：`tiktok` + `source=shop` → 无窗、无条数上限；`source=comment` → 桥自己的「一入站一条」；默认（私信）保持 48h/10 | 事实表：Shop 客服无 48h 窗 | **公共层改动需与 DY 线协调**（`window_rule` 签名加可选参数，DY 调用方零改动） |

## 2. 分段（开工顺序建议：E 溢出 → A 边车 → C 归因 → D 连接器 → B 评论 API）

| 段 | 内容 | 验收 | 落点 |
|---|---|---|---|
| **A 网页托管边车** | `services/tiktok-web`（Node，同 Messenger Web 边车骨架）：客户登录 → 会话列表 / 线程 DOM 只读 → `POST /api/internal/protocol/ingest`（`platform=tiktok`, `mode=web`, `source={"source":"web","assist_only":true}`）→ 收件箱 + 起草 → 坐席「复制到剪贴板」；桌面 assistOnly 卡；登录态失效 → 会话健康 `expired` | fixtures 驱动 DOM 解析 8 例 + e2e 1 例；`platform_login.tiktok.web_enabled` 默认关；能力矩阵进表「TikTok（web）」（发文本 `-`：assistOnly） | `services/tiktok-web/`、`src/integrations/tiktok_web_worker.py` |
| **B 公开评论回复 API** | Comment API：视频评论列表 / 回复 / 隐藏；进主站 `official`；`tiktok:comment:<user_id>` 会话键**与 TK-1 E 段桥同键**（同一评论用户同一会话；`source.source` 区分 `huoke` / `official`）；桥适配器让位：官方可用则 `TikTokHuokeAdapter` 退为兜底 | 假 transport 6 例；`test_tiktok_huoke_bridge` 4 例保持绿 | `src/integrations/tiktok_comment_api.py` |
| **C Messaging Ads 归因** | `tiktok_official.handle_webhook` 解析 `im_receive_msg` 的广告字段 → `source.ad`；来源标签 `ads`；漏斗按 `campaign_id` 聚合 + 会话头「来自广告：<campaign>」 | 真载荷样本（A 段权限后采）2 例；假载荷 2 例；i18n 六语 | `tiktok_official.py`（+20 行）、`normalizer.py` 来源标签（stage_hunks） |
| **D Shop 订单连接器** | `ecommerce_tools` `tiktok_shop`：`get_order / list_orders / get_fulfillment / get_return`，只读；小智工具描述；订单卡侧栏从「一次拉摘要」升级为连接器数据 | 假 transport 6 例；工具描述门禁 | `src/tools/ecommerce/tiktok_shop.py`（路径以 `ecommerce_tools` 现状为准） |
| **E TK-1 溢出** | ① `channel_policy.window_rule(source=)` 放宽（D-TK2-6，与 DY 协调）；② huoke 侧 `reply_engine: local \| chengjie` 开关 + `check_inbox` 分支（兄弟仓，走 huoke review）；③ TK-1 A/B 权限后收尾：真载荷替换假载荷、`implemented=True`、事实卡「已支持」、发版说明首提 TikTok | `test_channel_policy` +2、`test_window_guard` +1；huoke 侧既有测试零 diff 绿 | `channel_policy.py`、`window_guard.py`（stage_hunks）、huoke 仓 |

## 3. 刻意不做

- 任何形式的主动私信 / 群发（官方 API 不允许；真机侧属 huoke 智拓产品）。
- 协议逆向、非可用区账号 + VPN 联调。
- 边车自动发送（assistOnly 是硬边界；升级为可发送需老板单独拍板 + 72 h 去风险验证）。
- 订单写操作（取消 / 退款 / 改地址）。
- 抖音（DY）任何代码。

## 4. 红线（沿用 TK-1）

- 公共层只追加 / 参数化 `tiktok` 相关，动前 `agent_probe` 看板确认 wx / qq / dy 线不在同文件活动窗；共享文件只 `tools/stage_hunks.py --mine tiktok --apply`；`git add` 显式路径；提交前 `git diff --cached --name-only` 只含本线；**不用 `git commit -- <paths>`**（pathspec 提交取工作树而非 index，会把他线在途行与手工 index blob 一起覆盖——TK-1 E 段 09-10 21:15 实锤，已 amend 修正）。
- 权限 / 资质未到前不模拟「已支持」：`implemented=False`、事实卡不变、小智不得答「已支持」。
- 每个 commit 补落点表一行；收工改总账 TK-2 行 + `agent_probe -Done`。

## 5. 开新对话粘贴（开工时用）

```
读 docs/指令_TK-2_TikTok二期_网页托管边车与评论回复_2026-09.md，按它做 TK-2（前置：Business Messaging 权限已批、TK-1 A/B 已收尾；抖音线冻结一行不动）。顺序 E 溢出（window_rule source= 放宽与 DY 协调 / huoke reply_engine 开关 / A-B 收尾）→ A 网页托管边车 assistOnly（可用区真账号采 DOM 进 fixtures，缺则 skip 不假绿）→ C Messaging Ads 归因（source.ad + 来源标签 ads + 漏斗聚合）→ D Shop 订单连接器只读 → B 公开评论回复 API（Marketing Partner 资质到手才做）。预算 $240。先 engines\chengjie\scripts\agent_probe.ps1 -Intent 登记；共享文件只用 tools/stage_hunks.py --mine tiktok，git add 显式路径，不用 git commit -- <paths>；每个 commit 补 docs/发版对账_v1.0.8x_TK2.md 一行；收工改 docs/抖音TikTok渠道族_进度总账.md TK-2 行并 agent_probe -Done。
```
