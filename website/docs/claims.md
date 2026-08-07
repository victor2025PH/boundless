# 对外宣传口径举证表（claims ledger）

> 官网（含落地页/下单页/物料）出现的**每一个对外数字与硬口径**都必须在本表有据可查。
> 方法论对齐 engines/chengjie 的 ratchet 门禁文化：**事实先行，文案跟随，禁词不回潮**。
> 与本表联动的工程护栏：
> - `npm run sync:facts` —— 从 `products/*/product.yaml` + `platform/licensing/sku_registry.json`
>   生成 `lib/generated/product-facts.json`（价格/定位的事实源）。
> - `npm run gate:content` —— 内容完整性门禁（资产存在性 / 价格一致性 / 禁用语黑名单 /
>   测试数 ratchet），已挂进 `prebuild` 与 GitHub Actions（`website-content-gate.yml`），
>   黑名单与本表「已禁用清单」同源维护。

## 分级定义

| 级别 | 含义 | 使用规则 |
| --- | --- | --- |
| **L1 硬事实** | 可复现实测 / 有仓内文件・记录背书 | 可直接对外；数字变了必须先改本表再改文案 |
| **L2 可辩护口径** | 有依据但口径需限定（如按模型能力而非实测全集） | 对外须按「备注」限定措辞，勿加码 |
| **L3 合法夸张** | 主观感受类营销修辞，无量化承诺 | 允许，但**不得**配具体虚构数字 |

## 口径举证

| 宣传语 | 分级 | 举证来源 | 复核日期 | 备注 |
| --- | --- | --- | --- | --- |
| "1100+ 自动化回归测试" | L1 | `engines/chengjie/tests` 直下 995 个 + `engines/huoke/tests`（含子目录）149 个 `test_*.py`，实测计数 2026-08-04；合计 1144 | 2026-08-04 | 以**文件数**计非用例计，用例数更大；数字随仓库增长，复核时重数（历史口径：2026-07-26 计 855，当时宣传 850+） |
| "7×24 生产运行 · 看门狗 5 分钟自愈巡检" | L1 | `deploy/instances/README.md` + `watchdog_instances.ps1`（每 5 分钟计划任务探活自愈、重启冷却闸门、维护预告播报） | 2026-08-04 | 2026-08 起对外**不再点名实例数量**（通译并入智聊后实例拓扑属部署细节，以 deploy/instances 实况为准）；「自愈」指看门狗自动拉起，非双机房容灾 |
| "断云演习 22/22 本地接管" | L1 | `engines/chengjie/AGENTS.md`「断云真流量演习」（2026-07-12 凌晨低峰实施）：防火墙封 DeepSeek 出站，22 发含并发全部由本地 LLM 兜底出真话，熔断全周期自动闭合 | 2026-07-26 | 演习为真流量口径（经 `/api/copilot/query` 全链）；对外表述限「演习」，勿说成常态无云运行 |
| "多语种回译评测 50/50 通过、语义均分 0.939" | L1 | `engines/chengjie/logs/eval/translation_trend.jsonl` 2026-08-01 周批（`translation_samples_hymt.yaml` 宽集 50 样本：zh→xx + xx→zh 双向；ollama_mt pass=50/50，mean_semantic=0.939） | 2026-08-04 | 口径为**回译 + 语义双轨**自动评测，非人工满意度；周批每周六自动重跑，数字漂移时以最新 JSONL 行为准更新本行 |
| "30+ 语种拟人互译" | L2 | 底层翻译模型语种覆盖：Hunyuan-MT 系列模型卡 33 语种 + DeepSeek 多语对话；实测评测集覆盖 17 语种（`translation_samples_hymt.yaml` 默认宽集） | 2026-07-26 | 对外用「30+」时以**模型口径**为准，**勿写"实测 30+"**；实测口径只能说 17 语种评测集 |
| "5 大平台统一接入" | L1 | `engines/chengjie/docs/PROJECT_SCOPE.md`（Telegram / LINE / Messenger 三端 RPA runner + 网页聊天 Widget）；WhatsApp 见 `engines/chengjie/src/integrations/whatsapp_rpa/` 与 `services/whatsapp-baileys/` | 2026-07-26 | 各平台接入方式不同（MTProto 协议 / RPA / 网页），个别平台按部署开启；对外勿承诺"全平台同等功能" |
| "20+ 专项质量评测门禁" | L1 | `engines/chengjie/scripts/run_eval.py` 评测轨道计数：--faq/--translation/--memory/--semantic-dedup/--memory-extract/--persona/--emotion/--crisis/--crisis-response/--xlate-confidence/--proactive-guard/--emotion-intensity/--crisis-resource/--crisis-overview/--voice-language/--bazi/--bazi-reading/--media-consistency/--offer-guard/--outbound-claims/--duel-semantic 共 21 条（2026-08-04 计数） | 2026-08-04 | 以 run_eval CLI 评测轨道数计；对外表述「专项质量评测」，勿混同 pytest 测试文件数口径 |
| 平台墙分层口径（已深度对接 / 陆续接入） | L1+L2 | 第一层「已深度对接」＝5 大统一收件箱平台（见上行）+ Facebook（真机获客链路，`engines/huoke`）；第二层「陆续接入」＝规划中平台，UI 以灰阶+角标显式区分 | 2026-08-04 | 第二层 logo 仅表达路线图，**不得**去掉「陆续接入/规划中」标注单独使用；Facebook 注明「真机获客」勿写成收件箱聚合 |
| "1 人顶 10 人团队" | L3 | 主观夸张，无需举证 | 2026-07-26 | 不得配具体虚构数字（如"实测节省 XX 万"）；可配 L1 事实做支撑但不得混写 |
| "像跟老乡聊天一样自然" | L3 | 主观夸张，无需举证 | 2026-07-26 | 同上；涉及翻译质量的量化表述必须回落到上面 L1 评测口径 |

## 已禁用清单（与 `scripts/check-content-integrity.mjs` 的 `BANNED_CLAIMS` 同源）

本轮内容治理清理掉的虚构数字 / 高风险表述，门禁扫描 `lib/content.ts`、`lib/landingContent.ts`、
`lib/matrixxContent.ts`、`lib/chatxContent.ts`、`lib/growthContent.ts`、`lib/downloads.ts`，
命中即 build 失败：

| 禁用子串 | 禁用原因 |
| --- | --- |
| `2000万` | 虚构规模数字，无任何后端数据支撑 |
| `98%` | 虚构百分比，无评测口径支撑（真实评测口径见上表 L1 行） |
| `300+` | 虚构数量级，无清单支撑 |
| `封号自动换号` | 公开承诺规避平台风控 = 自证违反平台 ToS，法务红线 |
| `九产品` | 品牌层口径（`brand.ts` 的 `PRODUCT_COUNT` 驱动）；销售面禁止手写产品数量数字，防双源漂移 |

> `幻缘` / `FateX` 曾在禁用清单（销售面不出现）；2026-07-26 经营层拍板上线独立落地页
> `/fate` 后解禁。命理内容的合规边界见落地页文案约束：吉凶零断言、不预言死亡/重病/灾祸、
> 重大决策仅供参考、低落情绪先共情（与引擎 `companion.bazi` 的安全红线同口径）。
| `防封` | 暗示对抗平台风控，与「封号自动换号」同族，法务红线 |

> 注：2026-07-26 清理收口后，`lib/growthContent.ts`、`lib/downloads.ts` 已进
> `BANNED_TARGET_FILES`。`lib/brand.ts` **刻意不进**：它是品牌层单一事实源，合法包含
> 幻缘/FateX 产品定义与九产品口径（`PRODUCT_ORDER`/`PRODUCT_COUNT`），扫它会对合法
> 内容误报；brand.ts 的高风险词（防封类）已于同日清零，靠 review 守住。

## 维护规则

1. **新增对外数字必须先入此表**：给出分级与举证来源后才可进文案；无法举证的数字一律不上线。
2. **数字变更走事实源**：价格改 `products/*/product.yaml` → `npm run sync:facts` → 文案跟着改；
   测试数/评测分等工程口径复核后更新本表「复核日期」。
3. **黑名单与本表联动**：`gate:content` 的 `BANNED_CLAIMS` 与本表「已禁用清单」必须同步增删
   （脚本部分联动——黑名单由脚本强制执行，本表记录禁用原因与背景）。
4. **复核节律**：对外物料上新 / 大改版前逐行复核本表；测试数已由 CI 自动复核
   （`gate:content` 检查 4「测试数 ratchet」：宣传数 > `engines/*/tests` 实际测试文件数
   即红，每次运行都输出实际计数），季度复审只需读门禁输出的实际数决定是否上调宣传
   数字并更新复核日期；其余 L1 底层数字（评测分等）仍至少每季度重测一次并更新复核日期。
5. **门禁不放水**：`gate:content` 挂在 `prebuild`，命中清单只能靠改文案或（有据地）改事实源
   消除，禁止为跑绿删检查、加白名单遮真问题。
