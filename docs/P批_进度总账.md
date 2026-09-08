# P 批进度总账（跨对话 / 跨账号交接的唯一入口）

> 任何新对话、新账号、崩掉后重开，**第一件事就是读这里**。不依赖聊天记录。
> **谁改这里：** 每条指令的执行方在**收工时（或额度将尽时）**更新自己那一行，**只改自己那一行**。**每个 commit 落地后立刻补落点表一行。**
> 建立于 2026-09-08 15:3x。公共底座 `docs/修复指令_P批_并行_2026-09-08.md`（去向 / 归属 / 决策 D-P1–D-P6）；方案 `docs/全自动出站链_三视角方案_2026-09-08.md`。

## 一句话现状

1.0.77 复测（skuio 13:32–15:03，11 份报告）：登录 3 秒 / 切全自动即对沉寂老客户批量起草（含昨晚停联的 Sinue，停联随重装丢失）；开场白编造「你之前提过的徒步」；AI 两次谎称已发图（RRYKF6 第三次）；自聊会话入箱；首装 KB 残留客服域；后台监听 0.0.0.0；打包缺 handoff yaml、多了 fatex.db；值守链误挂 #106 + 12 条幻影 out 行。**O 批随 1.0.78 由 R78 在发（不等 P 批）；P 批 = 1.0.79。**

## 进度表

| 指令 | 主题 | 工单 | 预算 | 状态 | 已完成 | 剩余 | 落点表 | 已花 |
|---|---|---|---|---|---|---|---|---|
| **P-1** | 起草即净化 + 引用锚点守卫 | #259 #254 | $150 | **收工（待 R79 装载 / 真机）** 09-08 15:46–17:3x | A+B `2d9fb3f0`（`enrich_draft` 一处 `apply_draft_humanize`：claim_guard → humanize，草稿=将发文本；新 `claim_guard.py` 三语引用锚点守卫；care/冲刺出口同净化；发送门 `[outbound-leak]`；新 `ai_fingerprint_stats` 四格 24h 计数 JSONL 持久）/ C `ce673364`（新 `draft_style_hint.py` 硬禁 + en/zh/ja few-shot，persona_reply 回复链 + opener 两处；`outbound_style_gate --draft-log` 生成侧 <5% 门禁）/ D `c176fff5`（`/admin/ops`「🧬 AI 指纹」卡 + `/api/workspace/metrics.ai_fingerprint` + i18n 小包 zh/en/zh_hant）；落点表 `d5e2def8 / 620218bd / a1efe770` + §五回访口径（交 P-5） | **P-6 可开**：`outbound_humanize.py` 既有函数签名**未变**，新增 `apply_draft_humanize(...)`（起草层入口，口语简写请挂在其内 `humanize` 之后）与 `_fp_stats`；发送门多 `[outbound-leak]` + `record_gate`。**留给 R79**：`.py` 需重启；生成侧门禁 `python scripts/outbound_style_gate.py --draft-log <backend.log>`（≥20 稿、<5%）；真浏览器看四格 + grep 对账（落点表 §三 D 行）。**未做 / 越界**（落点表 §四）：MaxTokens 60–80（ai_client 地板 256 有既有事故注记，篮幅由 humanize ≤2 句兜底）；draft 行无 meta 列（计数走 `[draft]` 日志 + JSONL）；工作台「重新生成」路由 / 协议链 / A 线 GreetingSkill 未挂起草层净化（发送门兜底 + leak 告警指路）；P-3 落 `promise_checked / promise_no_action` 后卡片第四格自动有值 | `发版对账_v1.0.79_P1.md` | ≈$95 |
| **P-2** | 登录 / 切档不群发 + 停联持久 + 自聊排除 | #259 #252 | $220 | 未开工 | | A 回填标记不进自动化 / B 入站年龄闸 72h / C 切档·登录确认框 / D 沉寂清单 / E 停联账号级持久 + 登录扫描 / F 自聊会话排除 | `发版对账_v1.0.79_P2.md` | |
| **P-3** | 媒体承诺执行型 | #259 #252 | $160 | **收工（待 R79 装载）** | A 文字绑动作 `[media-bind]` / B strip→rewrite→review `[media_promise]` / C lie_caught 固定动作 resend·honest·handoff + 复诉词 + 弱词采信闸 / D photos=false 诚实拒绝三语 / E 5 分钟第二句配文体必附图否则改写（`second_caption_window_sec`）；commit `07f2168b`（识别层）`5d63a9d5`（动作层 + 回执账本 + 65 tests）；6SRA2B 四轮回放绿 | 无（`autosend_worker.py` 未动，E 在投递层等效；`persona_reply.py` 无结构化段可动；A 线 skill_manager 不在归属）；回访文案见落点表 §四 交 P-5；skuio 需确认 14:49 气泡排除 CW6RP4 | `发版对账_v1.0.79_P3.md` | ~$60 |
| **P-4** | 出厂态与安全 | MTRCH2（P-5 新立单）#254 | $150 | 未开工 | | A 后台默认回环 + 局域网开关 / B 陪伴域首装 KB 空 + 模板 + 系统话术移位 / C 打包清单两张 + handoff 进包 + fatex 出包 / D 文案 + 日志标签 | `发版对账_v1.0.79_P4.md` | |
| **P-5** | 值守链三修 + 归位 | #106 #259 #254 → 新立 **#260** | $80 | **收工**（C 段随 1.0.79 装载；A/B 工具侧已生效） | A `cd76850c`（`#N` 只在挂单上下文匹配 + fixed/closed 不挂不升 + auto_severity 只对开放单；#106 P0→P1 复原）/ B `6c80d322`（回执重试 ≤3 → `.ops/duty_alerts.log`、发前查收件箱同文本带 mid 幂等、空备注 / selfcheck 包不回执）/ C `29405d67`（`undelivered_reason()` 502 必带原因：`adapter_not_ready`「适配器未就绪（后端启动中）」/ `adapter_no_reason`；留痕 fail_reason 同源；`[send] fail conv=` 日志；存量 12 行补原因——它们本就 `status=failed`，缺的只是原因）/ D：MTRCH2 从 #106 摘出立 **#260**（出厂态四件 P1 1.0.77.0 → P-4），#259 / #252 去向注记，回访两条 16:54 / 16:55（#259 #260 → confirmed） | 交 R79：装载后 C 段验收（关适配器发一条 → 502 带原因 + failed 行带原因）；1.0.79 装机后按 §2 回访清单再回一轮 | `发版对账_v1.0.79_P5.md` | ≈$45 |
| **P-6**（第二波，P-1 收工后开） | 口语简写层 | #259（34585H） | $150 | 未开工（等 P-1） | | A 词典三语 + 概率 + 会话内一致 / B 称呼词随人设 / C 偶发拼写错可关 / D 「口语化程度」滑块 + 联动 / E 门禁 + 盲评 | `发版对账_v1.0.79_P6.md` | |

## 老板拍板记录

- 09-08 15:1x：D-P1～D-P6 按建议生效；**D-P3 修正为「1.0.78 不等 P 批」**（O-1 已收工、R78 在跑）。

## 全批收口（1.0.79）

- [ ] P-1～P-5 收工 → P-6 收工 → 发版线 R79：装载 → 全量回归 + 门禁（含 `outbound_style_gate` + P-1 新增「引用锚点」门禁 + P-4 「必须在 / 必须不在」打包门禁）→ 三包 → 三渠道 → 两机 → 台账（#259 #252 #254 + P-5 新立单 标 fixed）→ 回访（版本号口径）
- [ ] 回归验收（真机）：重装 + 登录 → 零自动草稿、Sinue 会话冻结；工作台任意草稿无 —；照片会话回放三轮无谎话；`netstat` 只见 127.0.0.1:18799；首装 KB 为空
