# AI 智能体驱动手机 · 技术路线文档

> 出处：2026-08-16 三视角优化对话（开发工程师 / 市场经理 / 用户）。
> 定位：把"本地 AI 模型操控手机"从设想落成可施工路线。合并两条设计线——
> **执行层**（怎么点手机更抗改版）与 **编排层**（AI 决定做什么，即操盘手）。
> 本文是设计单一真相；实施进度见文末路线表。

## 0. 一句话

在**保留确定性骨架**（uiautomator 选择器 + 获客剧本）的前提下，用**本地 GUI 视觉模型**
把"点哪里"变得抗 App 改版，用**受约束的 AI 操盘手**把"做什么"变得自适应——
最终形态 = **AI 大脑（操盘手）+ AI 硬化的手（GUI grounding）+ 确定性骨架（uiautomator/剧本）**。

不是"用 AI 把一切推平"，而是三层各司其职。

## 1. 为什么是现在（2026 技术判断）

三个已被验证的事实支撑这条路线可落地：

| 事实 | 证据 | 结论 |
|---|---|---|
| 本地 VLM 能直接看截图出点击坐标 | UI-TARS-2 / UI-TARS-1.5-7B（字节，Apache 2.0），本地 vLLM 200-400ms/步，AndroidWorld 64.2%，7B 需 ~16GB 显存，有 `MOBILE_USE` 模板 | 执行层可本地 AI 化，许可干净、延迟低、数据不出内网 |
| 确定性校验是可靠性的胜负手 | Minitap 多智能体（2026-02）AndroidWorld 做到 100%，靠的是任务分解 + **对文本输入/状态做确定性事后校验** + 元认知死循环检测；单智能体失败三因=上下文污染/静默输入失败/动作死循环 | 纯视觉不是升级——它要费劲补回 uiautomator 天生就有的确定性。**别丢确定性** |
| 端上微模型已成熟但不适配本架构 | StepX-Edge 0.9B（骁龙8Gen5，1.4GB，98tok/s，中文 OCR 强）、LFM2.5-VL-3B、GoClick 230M | 端侧路线不适配"主机集中控制多真机"，我们走主机侧本地大模型 |

**核心判断：把 uiautomator 换成纯 VLM 是可靠性降级 + 舰队规模下成本/延迟爆炸。正确动作是升级现有混合架构。**

## 2. 现状盘点（动手前的真相，防重复开发）

huoke 已经站在正确路线的起点上：

- **驱动方式**：`src/app_automation/*.py` 用 uiautomator2 确定性选择器（读控件 id/文本）为主，坐标点击为辅。
- **已有 VLM 兜底**：`src/ai/vision_fallback.py` = 截图 → VLM → 坐标 → `d.click(x,y)`，**已含预算闸/结果缓存/越界校验/失效重试/负坐标 bugfix**。这就是三层栈里 Tier-1 的雏形。
- **VLM 调用路径**：
  - 生产 grounding 走 `vision_fallback._build_prompt` + `llm_client.chat_vision` + `_parse_response`。
  - 本地 VLM 另有 `src/host/ollama_vlm.generate(prompt, image_paths=...)`（Ollama @ 11434）。
  - 配置单一真相：`config/fb_target_personas.yaml` 的 `vlm:` 块（`qwen2.5vl:7b`）。
- **缺口**：兜底用的是**通用** VLM（qwen2.5vl），不是 GUI 专用 grounding 模型；且是纯兜底，没有"陌生屏规划"层；**没有任何多模型命中率 A/B 评测框架**。

## 3. 目标架构：三层执行栈 + 操盘手编排

```
                    ┌──────────────────────────────────────────┐
   编排层(大脑)      │  AI 操盘手: 目标→选安全剧本→护栏→下发       │
                    │  (受约束智能体, 详见 §5)                     │
                    └───────────────────┬──────────────────────┘
                                        │ create_chain(chain_id, device)
   ┌────────────────────────────────────▼──────────────────────┐
   │ 执行层(手): 三层"落点"栈                                     │
   │                                                            │
   │  Tier-0  确定性选择器 (uiautomator id/text)  ← 主力,免费,秒级 │
   │            │ 点不中 (App 改版/A-B 布局/SDUI)                 │
   │  Tier-1  GUI 专用 grounding 出坐标            ← 换掉通用 VLM   │
   │            │ 还是找不到 (全新陌生屏)                          │
   │  Tier-2  GUI 智能体(UI-TARS)短序列规划+自愈   ← 新增          │
   │                                                            │
   │  每层动作后 → 确定性事后校验(状态真变了吗) ← Minitap 那一课    │
   └────────────────────────────────────────────────────────────┘
```

两条线正交互补：**操盘手决定"做什么"，执行栈决定"怎么点"。** 可分别独立推进。

## 4. 执行层：三层"落点"栈

### Tier-0 确定性选择器（现状，保留）
uiautomator2 读控件、点击、验证。免费、秒级、天然确定性。这是护城河，不动。

### Tier-1 GUI 专用 grounding 兜底（第一刀）
把 `vision_fallback` 调用的模型从通用 `qwen2.5vl` 换成 **GUI 专用 grounding 模型**（候选 UI-TARS-1.5-7B）。
选择器一失效（App 改版是 RPA 头号维护成本），命中率直接上台阶。管道（预算/缓存/越界/失效）全部复用，改动面小。

**但换之前必须先量**：UI-TARS 主在通用 App 训练，TikTok/FB 私域 App + 中文界面可能不在其分布内——
所以 Tier-1 的第一步不是换模型，而是**建 A/B 评测框架**，拿真实"选择器失效"案例集量命中率再决策。
（这就是本轮实施的落点，见 §7 与 `tools/grounding_ab_eval.py`。）

### Tier-2 GUI 智能体规划（后续）
Tier-0/1 都失败（真正没见过的界面）时，让 UI-TARS 当规划器：截图 → 规划几步 → 逐步执行 → 每步验证。
受约束（步数上限、动作白名单）+ 失败回滚。这是"自愈"层，也是 App 大改版时的最后一道防线。

### 贯穿：确定性事后校验
每层动作执行后都回到"点完确认预期状态真的发生了"（Minitap 100% 的关键）。
`vision_fallback.invalidate()` 已是这条原则的雏形（坐标点了没生效就逐出缓存）。

## 5. 编排层：AI 操盘手（受约束智能体）

详细设计见对话记录；要点：

- **核心原则**：AI 只能从**预先审核的安全剧本白名单**里选下一步，不能自由操控设备。
- **五段闭环**：感知（设备态+线索态+目标进度）→ 决策（Tier-1 规则兜底 80% + Tier-2 LLM 啃歧义）
  → 护栏（`ComplianceGuard.check` 硬配额 + `AdaptiveCompliance` 风险态 + 预算闸 + 高风险人工确认）
  → 执行（`task_chain.create_chain` 复用现有引擎）→ 反馈（`on_task_done` → 更新线索/风险 → 下一 tick）。
- **复用而非新造**：决策复用 `chain_advisor.recommend_chains` + `ChatBrain._determine_stage`；
  护栏复用现有两层合规闸；执行复用链引擎。新增仅薄编排层 + `operator_decisions` 审计表 + 目标配置。

## 6. 关键设计决策（先想清楚再动手）

1. **受约束智能体 vs 自由智能体**：重合规灰产里自由智能体是封号+法务双炸弹且难审计；限定动作空间换确定性/合规/可观测，划算。
2. **混合执行 vs 纯视觉**：纯视觉每动作一次 VLM，舰队规模下 GPU 吞吐瓶颈 + 比选择器慢；VLM 只当兜底/规划，经济账才算得平。
3. **本地 vs 云端**：本地 200-400ms/步、每动作零云成本、数据不出内网（灰产合规优势）；huoke 已有 176/173 本地 GPU（与智聊共用），UI-TARS-7B 落得下。
4. **换模型前先评测**：GUI 模型在私域中文 App 的分布外风险真实存在，未测先信=赌。A/B 框架是所有模型决策的前置。

## 7. 分阶段路线表

| 阶段 | 内容 | 依赖 | 风险 | 状态 |
|---|---|---|---|---|
| **P1 Tier-1 A/B 评测框架** | 可插拔 grounding 后端（通用 VLM/UI-TARS/mock）+ 真实案例采集 + 命中率/延迟指标 + 判据 | 无（selftest 可离线跑） | 极低（不改执行路径） | **已落地 2026-08-16** |
| P2 真实案例集 + 首轮评测 | 从生产采集"选择器失效"截图集、人工标注 gold，跑 qwen2.5vl vs UI-TARS | P1 + UI-TARS 本地部署 | 低 | 待 |
| P3 Tier-1 模型切换 | 评测证明 UI-TARS 更优则灰度切 `vision_fallback` 后端 | P2 结论 | 中（灰度+回退开关） | 待 |
| P4 操盘手 MVP | 单设备、规则决策、护栏全接、决策可审计（零 LLM 成本地基） | 无 | 低 | **已落地 2026-08-16** + **真机灰度 2026-08-17**：`src/host/operator_engine.py`（tick 引擎 + 自带时钟线程；设备由 `config/operator_goals.yaml` 点名；enabled+dry_run 双闸缺省全关）+ `/operator/status·tick·decisions` 三端点（admin/operator）+ `operator_decisions` 审计表 + 契约 38 案。规则序 R1 离线→R2 忙态→**R2b 反馈退避（连续失败达阈值停投待人工）**→R3 日频控→R4 恢复模式(仅低危养号)→R5 冷启动(养号)→R6 收割(harvest/followup 优先)→R7 拓新；高危链未授权=blocked。**4 台真机灰度实测**：VSM 完整跑通 tiktok_warmup(刷 92 视频 success)；自带时钟（非 DB 调度器——本部署 manual_execution_only 有意封禁）启动等一个间隔再首跳、护栏挡住重复投放。灰度日根治 3 病根：adb 不在服务 PATH(server.py 入口注入)、单步链 on_fail:skip 把失败伪装成 completed(改 abort)、fire-and-forget 无反馈(补 R2b 回读 chain_runs 成败) |
| P4.5 可观测面 | 控制台「AI 操盘手」页 + R2b 退避告警接线 | P4 | 极低 | **已落地 2026-08-17**：自动化组新菜单项（`data-page="operator"`，admin/operator 双角色可见；菜单预算 31→32 记账）+ `static/js/operator.js`（双闸/时钟徽章、目标卡、决策时间线、一键演习——页面**刻意只读+演习**，实弹开关只在 operator_goals.yaml 防误触，契约钉死 `dry_run:false` 不许出现在前端）+ tick 对 R2b 决策走 `AlertNotifier`（与链完成告警同通路，自带去重，webhook 未配=空操作）。契约 42 案全绿（+前端四点接线/告警红绿双向/菜单不入 admin-only 区）。真机复核实锤一条数据史课题：on_fail:skip 时代的假 completed 会把 R2b 连败断流（streak 停在 1）——不修数据，新失败自然累计，属自愈 |
| P5 Tier-2 规划 + 操盘手 LLM | 陌生屏 UI-TARS 规划；操盘手接歧义 LLM + 舰队编排 | P3/P4 | 中 | 待 |
| P6 自学习 | 用 `operator_decisions`+`chain_runs`+评测历史自校准规则阈值/prompt | P4/P5 | 中 | 待 |

## 8. 风险与回滚

- **模型分布外**：私域中文 App 命中率可能不及通用 VLM → P1 评测先量，用数据说话；差就不切。
- **成本/延迟**：VLM 严格限兜底/规划，不做默认驱动；预算闸硬约束。
- **智能体失控**：预算闸+硬配额双保险；`operator_enabled` 总开关一关回退现状。
- **执行路径回归**：P1 完全不碰执行路径（只加 flag-gated 采集）；P3 切换带灰度+回退开关。

## 9. 与现有模块接线点

| 新增 | 复用现有 |
|---|---|
| `tools/grounding_ab_eval.py`（P1 评测器） | `vision_fallback._build_prompt/_parse_response`（保证 A/B 反映生产真实路径）、`ollama_vlm`、`llm_client.chat_vision` |
| `src/ai/grounding_dataset.py`（P1 案例采集） | `vision_fallback.find_element`（flag-gated 采集点） |
| `src/host/operator_engine.py`（P4 操盘手，已落地） | `task_chain.list_chains/create_chain/get_chain_runs`、`device_state.get_phase`、`adaptive_compliance.is_recovering`、`leads.store.list_leads`；LLM 决策（P5）再接 `chain_advisor`+`ChatBrain` |

---
*本文档随路线推进更新状态列。P1 实施详见 `tools/grounding_ab_eval.py` 与 `tests/test_grounding_eval.py`。*
