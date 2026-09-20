# 实施88：小智「操作本软件 + 操作用户电脑」Computer-Use 深度调研报告

- 日期：2026-08-29　|　性质：**只调研，不改代码**（老板明令）
- 目标：让小智可以通过**语音 + 文字 + UI/UX 交互**，①全自动操作本软件（智聊），②操作用户的电脑
- 方法：历史对话回溯（实施58 系）＋ 全仓代码盘点 ＋ 联网 20+ 来源交叉核对
- 标注约定：**【事实】**=有一手来源可验证；**【宣称】**=厂商/项目自报未经第三方复核；**【观点】**=本线判断（附依据）

---

## 0. 结论先行（TL;DR）

1. **【事实】小智今天已经是「应用内白名单 Agent」**：问答/教学/替我做三模式、20 个注册动作、plan→确认→执行→审计→撤销 24h 全链在内测包上线；语音提问（176 Whisper）与 TTS 播报已通。它**没有**任何 OS 级键鼠/截屏/跨应用能力（壳内无 robotjs/nut-js/desktopCapturer 工具面）。
2. **【事实】2026 年 Computer-Use 赛道已收敛为三条技术路线**：
   - **结构化通道**（无障碍树/DOM/API）：微软 UFO²、terminator、Windows-MCP——快、稳、可审计；
   - **纯视觉通道**（截屏→VLM 出坐标）：Claude computer use（2026-08-20 GA）、GPT-5.4 原生 computer 工具、Gemini 3.5/3.6 Flash 内置 computer_use、Qwen3-VL 原生 computer-use、Midscene.js——通用但更慢更贵；
   - **混合**（结构化优先、视觉兜底）：UFO² 的 Hybrid GUI+API 是公开工程里最成熟的范式。
3. **【事实】开源权重已能打平乃至领先闭源**：2026-08 聚合榜 OSWorld-Verified 榜首是开源权重 Qwen3.8 Max（86.1%），Qwen3.8-27B（84.3%）以 27B 体量进前五——**本地化路线成立**（本机群 176 的 5090 已在跑 Qwen 系 VL）。
4. **【观点】选型建议**（依据见 §5）：操作本软件走「扩白名单动作表 + 前端 DOM 动作总线」，**不引入**像素级 computer-use；操作用户电脑新开一个「Windows 操控 runner」子系统，架构抄 UFO² 混合模式（UIA 优先 + 视觉兜底），模型主选本地 Qwen3-VL、云端按需切 Claude/Gemini，协议用 MCP；语音层零新基建，复用现有 ASR/TTS。安全三板斧（白名单、高危人审、审计撤销）从小智现有实现平移，这是全行业共识红线。
5. **【事实】命名撞车提醒**：GitHub 2.9 万星开源项目 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)（MIT，v2.4.2，2026-08-06）就叫「小智」，也是「语音入口 + MCP 多端控制（含 PC 桌面操作）」定位，对外传播需注意品牌区分；其架构反而是很好的参考。

---

## 1. 小智现状（代码事实，2026-08-29 盘点）

| 能力 | 状态 |
|------|------|
| 问答（SSE 流式 RAG，NO_BASIS 拒答，主链容灾） | ✅ 上线 |
| 教学（点哪教哪，161 词条，拦截点击不真执行） | ✅ 上线 |
| 替我做（`/api/assistant/agent/plan` → 逐步 `/act`，L2 确认卡 + diff + 撤销 + ⏹停止 + 流星进度） | ✅ 上线 |
| 动作注册表 `src/assistant/actions.py` | ✅ 20 个：L0 查询×4、L1 导航×1、L2 写配置×15；**L3 代发客户消息刻意未进表** |
| 语音提问（MediaRecorder → `/api/assistant/transcribe` → 176 Whisper）+ TTS 播报 | ✅ 内测包开 |
| 手机遥控（扫码配对 PWA `/xz`） | ✅ 上线（遥控小智，非遥控 OS） |
| Flow 编排 | ✅ 仅 2 条（TG/LINE 登录「登陆飞机」） |
| 规划器 | 严格 JSON 计划 + 结构复验（**非**协议级 function-calling） |
| OS 级键鼠/截屏/跨应用 | ❌ 无（实施58 P6 明确只作远期研究） |
| DOM 级「替你点任意按钮」 | ❌ 无（只有 goto_page 导航 + 教学高亮） |

**差距一句话**：往「全自动操作本软件」是**扩容问题**（动作覆盖 + DOM 动作总线）；往「操作用户电脑」是**新子系统**（缺一整层 computer-use 基建）。

---

## 2. 赛道全景（截至 2026-08-29）

### 2.1 大厂模型层（决定「脑子」）

| 玩家 | 现状【事实】 | 出处 |
|------|------|------|
| **Anthropic** | computer use 于 **2026-08-20 GA**：`computer_toolset_20260801`，17 个成员工具（screenshot/zoom/left_click/type/key/scroll/wait…），支持批量动作，免 beta 头；**执行环境由你自己实现**（客户端工具集）；同批 GA 还有 browser use / Skills API / Files API。官方文档明示：屏幕内容里的指令可能覆盖你的指令 | [官方文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)、[Channel Insider 报道](https://www.channelinsider.com/ai/news-anthropic-claude-computer-use-skills-files-api/) |
| **OpenAI** | Operator 独立产品 **2025-08-31 关停**并入 ChatGPT agent；**GPT-5.4 是其首个原生带 computer-use 能力的通用模型**（API 走升级版 `computer` 工具，1M 上下文） | [OpenAI 官方 Operator 页更新](https://openai.com/index/introducing-operator/)、[GPT-5.4 发布](https://openai.com/index/introducing-gpt-5-4/)、[OpenAI 帮助中心](https://help.openai.com/en/articles/11752874-chatgpt-agent) |
| **Google** | Gemini 2.5 computer-use 专用模型已转 **Legacy**；computer use 现为 **Gemini 3.5/3.6 Flash 内置工具**（2026-06-24 官宣），覆盖 browser/mobile/desktop，自带 prompt injection 检测；官方参考实现默认 `gemini-3.6-flash` | [Google 官方博客](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-computer-use-gemini-3-5-flash/)、[Gemini API 文档](https://ai.google.dev/gemini-api/docs/generate-content/computer-use)、[官方参考仓库](https://github.com/google-gemini/computer-use-preview) |
| **阿里 Qwen** | **Qwen3-VL 原生 computer-use**：官方 cookbook + 1000×1000 相对坐标协议（官方团队在 issue 里确认），OSWorld 官方仓有 qwen3vl_agent 适配；聚合榜显示 **Qwen3.8 Max（开源权重）86.1% 居 OSWorld-Verified 榜首** | [Qwen3-VL issue 官方回复](https://github.com/QwenLM/Qwen3-VL/issues/1521)、[vLLM 部署指南](https://docs.vllm.ai/projects/recipes/en/stable/Qwen/Qwen3-VL.html)、[BenchLM 榜](https://benchlm.ai/benchmarks/osworld-verified) |
| **字节 Seed** | UI-TARS-1.0（2B/7B/72B）与 1.5-7B **权重开源**（Apache-2.0）；**UI-TARS-2 只有技术报告（2025-09-04），权重未上 HF**——「UI-TARS-2 开源」的说法不成立，开源的是桌面壳 | [UI-TARS 仓库](https://github.com/bytedance/ui-tars)、[HF 1.5-7B](https://huggingface.co/ByteDance-Seed/UI-TARS-1.5-7B)、[UI-TARS-2 报告](https://arxiv.org/abs/2509.02544) |
| **智谱** | GLM-PC 是产品（不开源）；底座 **CogAgent-9B-20241220 开源**但已偏旧（2024-12）；GLM-4.6V 在 Midscene 支持列表中 | [CogAgent 仓库](https://github.com/THUDM/CogAgent)、[GLM-PC v1.1 介绍](https://developer.volcengine.com/articles/7464068947613548563) |

### 2.2 开源框架/执行层（决定「手」）

| 项目 | 定位与要点【事实】 | 许可/热度 | 出处 |
|------|------|------|------|
| **Microsoft UFO³/UFO²** | 最完整的 **Windows 原生 AgentOS**：UFO²（LTS）= UIA + Win32 + WinCOM 深度集成、**Hybrid Actions（GUI 点击 + API 调用混合）**、Speculative Multi-Action（批量预测省 51% LLM 调用）、Visual+UIA 混合控件检测、RAG 知识底座；UFO³ Galaxy = 多设备 DAG 编排（Win/Linux/macOS/Android/Web，WebSocket + MCP）。v3.0.0 2025-11-09，最新 3.0.7（2026-06-12），仍活跃 | MIT，9.3k★，Python | [仓库](https://github.com/microsoft/UFO)、[官方文档](https://microsoft.github.io/UFO/)、[UFO³ 发布页](https://github.com/microsoft/UFO/releases/tag/v3.0.0) |
| **mediar-ai/terminator** | 「Windows 版 Playwright」：UIA 无障碍树**缓存批量读取**（避免逐属性 IPC 往返），Playwright 风格选择器（role/name/automationid，抗主题/DPI 变化），Rust 核心 + TS/Python SDK + **35 个 MCP 工具**；仅 Windows 稳定 | MIT，1.6k★ | [仓库](https://github.com/mediar-ai/terminator)、[技术说明](https://t8r.tech/t/automation-tools-for-windows) |
| **CursorTouch/Windows-MCP + Windows-Use** | 轻量 **Windows 控制 MCP 服务器**（PyPI `uvx windows-mcp`，进了官方 MCP Registry；自称 Claude Desktop 扩展 2M+ 用户【宣称】）；Windows-Use 是其上的 Python agent：UIA 读屏（可不用视觉模型）、PowerShell、文件、虚拟桌面、**内置 STT/TTS** | MIT | [Windows-MCP](https://github.com/CursorTouch/Windows-MCP)、[Windows-Use](https://github.com/CursorTouch/Windows-Use) |
| **Midscene.js**（字节 web-infra） | **纯视觉 UI 自动化 SDK**：自然语言 `aiAct/aiQuery/aiAssert`，覆盖 web/Android/iOS/HarmonyOS/桌面/「任何可截屏界面」；模型可自托管（Qwen3.x/UI-TARS/GLM-4.6V/gemini-3.5-flash）；JS 生态与本仓 Electron 壳同构；AndroidWorld 93.1%【宣称】 | MIT 系开源，14k+★ | [仓库](https://github.com/web-infra-dev/midscene)、[官网](https://midscenejs.com/) |
| **UI-TARS-desktop / Agent TARS** | 字节官方桌面 GUI Agent 应用（本地+远程 operator，Electron），v0.3.0（2025-11-04）支持 UI-TARS-2 模型（模型走 API） | Apache-2.0，38.7k★ | [仓库](https://github.com/bytedance/UI-TARS-desktop)、[v0.3.0](https://github.com/bytedance/UI-TARS-desktop/releases/tag/v0.3.0) |
| **Agent S3（Simular gui-agents）** | 研究型框架：2025-12 以 72.6% **宣称首超 OSWorld 人类基线 72.36%**；bBoN 宽扩展（多 rollout 择优）+ 原生编码代理；论文进 TMLR 2026 | Apache-2.0 | [仓库](https://github.com/simular-ai/Agent-S)、[论文](https://arxiv.org/abs/2510.02250)、[官方博客](https://www.simular.ai/articles/agent-s3) |
| **browser-use** | 浏览器域最大开源 agent（**110k★**，MIT）；自称 Odysseys 榜第一 87.4%【宣称】；workflow-use（RPA 2.0）仍早期 | MIT | [仓库](https://github.com/Browser-Use/Browser-Use) |
| ~~Bytebot~~ | 容器化 Linux 桌面 agent，**2026-03 原仓已归档**（靠社区分叉续命）——不宜选型 | Apache-2.0（归档） | [仓库](https://github.com/bytebot-ai/bytebot) |
| ~~Open Interpreter~~ | **已转向**：现主线是 Rust 重写的编码代理（基于 Codex，68k★），不再是「操作电脑」方向；Python 原版靠社区分叉维护——旧认知作废 | Apache-2.0 | [仓库](https://github.com/openinterpreter/openinterpreter) |
| Fazm / Auto-Use | Fazm：macOS 专属语音优先桌面 agent（MIT，本机群全 Windows 用不上；其「2026 十佳」榜自封第一，**内容营销**）；Auto-Use：混合无障碍+视觉、多 agent，较新未证 | MIT / — | [Fazm](https://fazm.ai/blog/fazm-ai-mac-agent)、[Auto-Use](https://github.com/auto-use/auto-use) |

### 2.3 榜单横截面（2026-08，聚合口径）

OSWorld-Verified（361 桌面真任务）前列：**Qwen3.8 Max 86.1%（开源权重）** > Claude Mythos/Fable 5 各 85% > Kimi K3 84.8%（开源） > **Qwen3.8-27B 84.3%（开源，27B 体量）** > Claude Opus 4.8 83.4% > Gemini 3.6 Flash 83% > Claude Sonnet 5 81.2% > GPT-5.5 78.7%。
- 来源：[BenchLM（8/28 更新）](https://benchlm.ai/benchmarks/osworld-verified)、[AnotherWrapper（8/27）](https://anotherwrapper.com/tools/llm-pricing/leaderboards/osworld)、[llm-stats](https://llm-stats.com/benchmarks/osworld-verified)、[Steel.dev（附各家 harness 出处）](https://leaderboard.steel.dev/leaderboards/osworld/)——四家独立聚合站数字互相吻合（±0.5pt）。
- **【方法论警告，事实】** Steel 榜标注：Anthropic 的 85% 系其**修订版 harness** 上的自报 system card 数字，与旧条目**不可直接比**；聚合榜普遍收录自报值。读数看趋势即可，别抠个位数。
- **【观点】** 两个结论：①榜首集群 85%±1 → 该基准接近饱和，前沿模型「会操作电脑」已是普遍能力，不再是护城河；②Agent S3（2025-12 的 72.6%）半年内被原生模型甩开 → 复杂框架的红利在被「模型原生 CU 能力」吃掉，选型应「薄框架 + 强模型」而不是重框架。

### 2.4 语音层

- **【代码事实】自有栈已齐**：小智 `transcribe`（176 GPU Whisper，140 AvatarHub STT 兜底，CPU 三级）+ TTS（IndexTTS/CosyVoice/edge 全链 + 预渲染命中层）——语音输入输出**不是缺口**，纯接线问题。
- **【事实】同名项目 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)**（29k★，MIT，v2.4.2 2026-08-06）：流式 ASR+LLM+TTS + **设备端/云端 MCP 双层工具面**（官方 README 明列「PC桌面操作」为云端 MCP 扩展场景）；另有社区后端 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)。**架构参考价值**：语音入口与工具执行面通过 MCP 解耦——正是我们建议的形态；**品牌风险**：对外叫「小智」注意与 xiaozhi.me 区分。

### 2.5 安全共识（全行业红线，做之前必须认账）

| 结论【事实】 | 来源 |
|------|------|
| 屏幕上渲染的一切（网页/邮件/文档/弹窗）都可能藏指令，agent 会照做；Anthropic 官方文档原话承认此风险 | [Claude 官方文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)（Prompt injection 节） |
| VPI-Bench 实测：computer-use agent 被视觉注入骗成功率最高 **51%**，浏览器 agent 最高 **100%**；system prompt 层防御**基本无效**；有效防线在**动作层**（权限门控、沙箱、运行时监控） | [VPI-Bench（arXiv 2506.02456）](https://arxiv.org/abs/2506.02456) |
| CSA 2026-04 研究报告：最小权限 OS 账户/网络出口、渲染内容按不可信输入处理（剥零宽字符/CSS 隐藏文本）、**金融/凭据/外发/装软件四类动作无论 agent 多强都要人确认**；OpenAI 公开承认浏览器注入「可能永远无法完全解决」 | [CSA Research Note PDF](https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/04/CSA_research_note_computer-use-agent-safety-blindspots_20260415-csa-styled.pdf) |
| 实践共识：高危动作人审是**单点最高杠杆**防线；硬限制必须写在代码里而不是 prompt 里 | [System Hardening 生产模式文](https://www.systemshardening.com/articles/ai-landscape/claude-computer-use-sandboxing/)、[AI/TLDR 沙箱指南](https://ai-tldr.dev/learn/ai-agents/multi-agent-computer-use/computer-use-agent-safety/) |

**【观点】** 这套共识与小智现有的「白名单 + L2 确认卡 + 审计 + 撤销」完全同构——我们的安全骨架不用重造，平移到 OS 层即可。

---

## 3. 关键说法交叉核对（事实 vs 宣称）

| # | 说法 | 核对结论 |
|---|------|------|
| 1 | 「UI-TARS-2 已开源」 | **❌ 不成立**。开源的是 UI-TARS-desktop 应用壳（Apache-2.0）与 1.x 权重；UI-TARS-2 只有 [技术报告](https://arxiv.org/abs/2509.02544)，HF 无权重仓 |
| 2 | 「Agent S3 首超 OSWorld 人类基线（72.6% vs 72.36%）」 | **宣称属实但已过时**：Simular 自报 + TMLR 论文背书；2026-08 前沿模型已 85%+，框架成绩被原生模型超越 |
| 3 | 「Qwen3.8 Max 开源权重登顶 OSWorld 86.1%」 | **四家独立聚合榜一致**（BenchLM/AnotherWrapper/llm-stats/Steel），可信；但注意各家 harness 口径差异，横比只看趋势 |
| 4 | 「Windows-MCP 有 2M+ 用户」 | **仅项目自报**（README），无第三方验证——热度参考，不作决策依据 |
| 5 | 「Bytebot/Open Interpreter 是当前推荐」（2025 旧认知） | **❌ 已过时**：Bytebot 2026-03 归档；Open Interpreter 转向 Rust 编码代理 |
| 6 | 「Fazm 是 2026 桌面 agent 第一名」 | **内容营销**：排名文出自 fazm.ai 自家博客，且 macOS-only 与本机群无关 |
| 7 | 「Operator 还能用」 | **❌** 2025-08-31 已关停，能力并入 ChatGPT agent + API `computer` 工具（OpenAI 官方） |
| 8 | 「像素级视觉是唯一通用路」 | **有分歧的观点**：Midscene 主张纯视觉（结构拿不到时唯一解）；terminator/UFO 用数据证明 UIA 树在 Windows 上更快更稳。**本线判断：能拿到结构就用结构，拿不到才用视觉** |

---

## 4. 来源可信度评级

| 级别 | 来源 | 说明 |
|------|------|------|
| **高**（一手权威） | [Anthropic 官方文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)、[OpenAI 官网](https://openai.com/index/introducing-gpt-5-4/)、[Google 官方博客](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-computer-use-gemini-3-5-flash/)、[microsoft/UFO](https://github.com/microsoft/UFO)、[bytedance 各仓](https://github.com/bytedance/UI-TARS-desktop)、[QwenLM issue 官方回复](https://github.com/QwenLM/Qwen3-VL/issues/1521)、[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)、[CSA PDF](https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/04/CSA_research_note_computer-use-agent-safety-blindspots_20260415-csa-styled.pdf)、[VPI-Bench 论文](https://arxiv.org/abs/2506.02456)、[Agent S3 论文](https://arxiv.org/abs/2510.02250) | 官方文档/仓库/经同行评审论文；GitHub 星数、归档状态、发布日期均为平台一手数据 |
| **中**（独立聚合/大媒体） | [BenchLM](https://benchlm.ai/benchmarks/osworld-verified)、[llm-stats](https://llm-stats.com/benchmarks/osworld-verified)、[AnotherWrapper](https://anotherwrapper.com/tools/llm-pricing/leaderboards/osworld)、[Steel.dev 榜](https://leaderboard.steel.dev/leaderboards/osworld/)、[Channel Insider](https://www.channelinsider.com/ai/news-anthropic-claude-computer-use-skills-files-api/)、[t8r.tech 技术文](https://t8r.tech/t/automation-tools-for-windows)、火山引擎/智源社区转载 | 数字可互相印证但收录自报值；Steel 会标注 harness 出处，质量最好；t8r 是 terminator 自家域名（技术细节可信、结论带立场） |
| **中低**（厂商自报） | Simular 博客 72.6%、Midscene 官网 93.1%、browser-use「Odysseys 第一」、Windows-MCP「2M 用户」、Anthropic system card 自报分 | 通常非造假，但 harness/口径利己，一律标【宣称】 |
| **低**（SEO/内容营销） | [Fazm 系博客](https://fazm.ai/blog/best-ai-agents-desktop-automation-2026)、Presenc/chatai.guide/explainx 等追踪站 | 只用作时间线线索，所有关键事实已另行对照官方来源确认 |

---

## 5. 结论与选型建议（【观点】，附依据）

### 5.1 总架构：一脑两手，语音免费

```
语音(现有 ASR) ─┐
文字/面板 ──────┤→ 小智规划层(现有 plan→确认→审计→撤销 骨架)
                │       ├─ 手① 应用内动作总线(白名单 API + DOM 锚点) ← 操作本软件
                │       └─ 手② Windows 操控 runner(MCP 服务, UIA 优先+视觉兜底) ← 操作用户电脑
TTS 播报(现有) ←┘
```

### 5.2 手①：操作本软件——**不引入 computer-use**

- 做法：动作表 20 → 全设置/KB/会话处置扩容；补「前端 DOM 动作总线」（`data-anchor` 铺点 + 事件级白名单 + 教学模式已有的元素定位复用），让「替我做」能真点自家按钮；Flow 目录从 2 条铺开。
- 依据：自家 Electron+webview 里 DOM/CDP 是**确定性通道**，比截屏猜坐标快且稳一个数量级（连纯视觉派 Midscene 也承认结构化通道是拿得到结构时的首选；terminator 用 IPC 数据证明树读取碾压像素）；实施58/74 已两次拍板此路线，本次调研维持结论。

### 5.3 手②：操作用户电脑——新开「Windows 操控 runner」子系统

- **执行层**：UIA 无障碍树优先（参考/复用 [terminator](https://github.com/mediar-ai/terminator) 或 [Windows-MCP](https://github.com/CursorTouch/Windows-MCP)，都是 MIT 可自托管），截屏视觉兜底；混合范式抄 [UFO²](https://microsoft.github.io/UFO/)（Hybrid GUI+API、Speculative Multi-Action 省 51% LLM 调用是已验证的工程红利）。
- **模型层**：主选**本地 Qwen3-VL**（176 的 5090 在跑 Qwen 系 VL；官方原生 computer-use 协议 + 1000×1000 相对坐标；开源权重榜证明 Qwen 系 CU 能力第一梯队），云端兜底按 config 可切 [Claude computer_toolset](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) / [Gemini computer_use](https://ai.google.dev/gemini-api/docs/generate-content/computer-use)——与本仓「本地为主、云为容灾」的既有算力哲学一致。
- **协议层**：小智 ↔ runner 用 **MCP**（与 xiaozhi-esp32 的「语音入口+MCP 工具面」同构；UFO³/terminator/Windows-MCP/Midscene 全生态互通；将来接多机 = UFO³ Galaxy 的设备池模式）。
- **安全层（不可裁剪）**：屏幕内容一律不可信；金融/凭据/外发/装软件强制人审（CSA 四类）；白名单硬编码在动作层而非 prompt（VPI-Bench 实证 prompt 防御无效）;独立低权限 Windows 账户运行；全程审计 + 一键停——全部复用小智已有交互件（确认卡/⏹/审计 JSONL）。

### 5.4 明确不选 / 缓选

- ❌ Bytebot（归档）、Open Interpreter（转向）、Fazm（macOS）、CogAgent-9B（旧）;
- ❌ 重研究框架（Agent S3 的 bBoN 多 rollout 择优：成本×N，适合刷榜不适合生产）;
- 缓：UI-TARS-desktop 整机引入（Electron 应用形态与我们壳冲突，取其模型协议与交互设计即可）；browser-use（我们的浏览器场景大多可走自家 DOM 总线，需要时再挂）。

### 5.5 落地路线草案（待老板拍板后另立实施文档排期）

- P0：动作表扩容 + DOM 动作总线（操作本软件闭环）——纯应用内，风险最低收益最大；
- P1：Windows runner MVP（UIA 只读侦察 + 白名单动作 + 确认卡），先管 117/坐席机自己人;
- P2：视觉兜底（本地 Qwen3-VL 截屏 grounding）+ 高危动作策略引擎;
- P3：多机（MCP 设备池，参照 UFO³ Galaxy）+ 客户机灰度。

---

## 6. 衔接

- 历史对话：实施58 方案线（小智全能助教四视角）、小智接入提案线、三功能深化线、教学→替我做闭环线;
- 相关文档：`docs/实施58_小智全能助教_点哪教哪_代客操作_语音手机遥控_四视角方案_2026-08.md`（P6「computer-use」远期项即本报告主题）、实施73/74（IA 三模式与报障闭环）;
- 本报告为**选型依据**，未改任何代码；开工前按惯例过意向板。

---

## 6.5 P0 实施记录（2026-08-30 晚，老板拍板后首批）

**范围＝§5.5 的 P0「操作本软件闭环」：DOM 动作总线 + 动作表扩容。** 前端已热更
上线（agent.js `VER=20260830c`，生产探针 200）；`.py` 落盘待 22:30 重启窗装载。

### 交付一：DOM 动作总线（「替我做」能真点自家按钮了）

- **新模块 `src/assistant/ui_anchors.py`**：页面控件锚点注册表，首批 6 条
  （账号抽屉/会话搜索/知识库新建+搜索/新建人设/额度守卫带看）。三条铁律：
  ① 只收**揭示类**目标（click=开抽屉表单、fill=填搜索框、show=聚光带看；
  发送/提交/删除永不进表——真正的状态变更仍走 L2 确认卡链）；② 选择器
  服务端策展，LLM 只能提名锚点 id，**永不执行 LLM 生成的选择器**；③ 热区
  模板零改动（unified_inbox 等用既有稳定 id/属性选择器；非热区 knowledge
  补了 1 枚 `data-anchor`，教学模式顺带受益）。
- **actions.py 新增 kind="ui" 的 `ui_act`（L1）**：plan_action 按注册表复验
  （未注册锚点/fill 缺词/超 80 字全拒）；`/api/assistant/act` 对 ui 计划回
  注册表换出的 sel/gesture（与 nav 同信任级）。
- **规划器**：prompt 带「页面控件白名单」块；ui 步带锚点人话标签；
  `_maybe_insert_home_nav` 扩展到 ui 步——人不在控件所在页时自动先插一步
  goto_page（跨页任务闭环，复用既有 sessionStorage 续跑）。
- **前端执行器（assistant-agent.js）**：ui 步就地执行——流星飞向真实控件
  （先瞬时 scrollIntoView 防坐标漂移）→ click 真点 / fill 真填并派发
  input+change 事件 / show 聚光；控件不存在=**诚实失败**绝不装 ok；手机
  standalone 如实跳过；埋点 `asb_agent_ui_<gesture>`。

### 交付二：动作表 20 → 29

新 L0：`query_version`（版本号，取不到如实说）。新 L2（全部走既有
FIELDS 白名单路径+确认/撤销/审计三板斧，零新写端点）：打字状态、回复前
已读、智能延迟、消息分条、每日回复额度（0=不限）、上下班时间
（新 `hhmm` 参数类型，半配置态必拒、跨夜合法）、攒批缓发。
路由 `_try_hot_apply` 从「set_reply_delay 专例」泛化为**按路径分派**
（deliver_delay 族→apply_deliver_delay；拟人链开关→apply_humanize_flags，
与 reply-settings 保存路由同链），`hot_applied` 口径继续诚实。

### 门禁与验证

- 新 `tests/test_assistant_ui_anchors.py`（16 例，已登记 gate_sweep）：锚点
  清单**显式钉死**（新增锚点必改测试=强制评审点）、手势/等级白名单、每条
  锚点有**模板物证**（控件改名即红，不留「流星飞向空气」的幽灵锚点）、
  LLM 提名 sel 被忽略、home-nav 插步、两处 `_HUMANIZE_FLAGS` 映射同步。
- `test_assistant_actions.py` +5 例（hhmm/班表横向校验/额度边界/新动作
  等级钉/版本摘要诚实）。小智全家 **236 passed**；真浏览器门禁扩 **M8 系
  4 场景**（fill 真触发 input 计数器、click 真进 onclick、任务卡两步 ✓、
  控件不存在=fail 态）→ **115/115**；广域 `gate_sweep` **ALL GREEN
  （700 passed）**；路由清单 4/4（零新路由）。
- 版本戳七点同步：agent.js VER / 两壳 loader ?v= / `_AGENT_JS_VER` /
  ui-build.txt（`20260830-1907`）。

### 刻意不做（本批）

- Flow 目录扩容：现有登录形态运行器（YAGNI 决策）不为非登录流程强造
  通用解释器；「知识库加条目」类目标由 多步计划+DOM 总线 部分覆盖。
- L2 级 UI 点击（点「保存」类按钮）：需先给前端补 ui 步确认卡链，
  且与「写走服务端 API」原则相抵，等真实需求出现再议。
- P1「Windows 操控 runner」：新子系统，属下一阶段拍板项（见 §5.5）。

---

## 7. 补查（2026-08-29 晚）：云端国产（DeepSeek/智谱）vs Claude/Gemini 差距与性价比

> 老板追问：云端若用国内 DeepSeek 或智谱，对比 Claude/Gemini 差多少、谁性价比最高。

### 7.1 能力×价格对照表（computer-use 口径）

| 模型 | OSWorld 成功率 | 输入/输出（$/百万 tokens） | 权重 | 成绩可信度 |
|------|------|------|------|------|
| Qwen3.8 Max（阿里） | **86.1%（榜首）** | $2 / $6 | 开源 | 四家聚合榜互证【中】 |
| Claude Opus/Fable/Mythos 5 | 85% | $10 / $50 | 闭源 | Anthropic system card 自报【中低】+聚合 |
| Kimi K3（月之暗面） | 84.8% | $3 / $15 | 开源 | 聚合【中】 |
| Qwen3.8-27B（阿里） | 84.3% | **$0.45 / $3.20** | 开源 | 聚合【中】 |
| Claude Opus 4.8 | 83.4% | $5 / $25 | 闭源 | 自报（修订 harness）【中低】 |
| Gemini 3.6 Flash | 83% | $0.75~1.5 / $3.75~7.5（两家聚合口径不一） | 闭源 | 聚合【中】 |
| Claude Sonnet 5 | 81.2% | $2 / $10 | 闭源 | 自报+聚合【中】 |
| **DeepSeek v4-flash-vision-exp**（2026-08-21 新发） | 官方称多模态 agent「接近 Opus 4.8」（=83.4 档）；第三方博客流传 79.5%【低可信】；**尚无独立榜单收录（太新）** | **谷 $0.22 / $0.66；峰 $0.44 / $1.32；缓存命中 $0.007**（¥1.5/¥4.5 谷） | 闭源（API-only） | 价格=官方文档【高】；成绩=厂商宣称【中低】 |
| 智谱 GLM-5V-Turbo（2026-04，不开源） | **62.3** | ~$0.8~0.96 / ~$3.2 | 闭源 | 厂商技术报告【中低】 |
| 智谱 GLM-4.6V（2025-12 开源） | **38.1** | ¥1 / ¥3（<32K 档；Flash 9B 免费） | 开源 | 厂商对照表【中低】 |

出处：[DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek 官方发布公告](https://api-docs.deepseek.com/news/news260821/)、[智谱开放平台价格页](https://bigmodel.cn/pricing)、[GLM-5V-Turbo 技术报告转述（OSWorld 62.3/AndroidWorld 75.7）](https://chainweeks.com/news/150098.html)、[GLM-4.6V 开源与降价（新浪/IT之家）](https://finance.sina.com.cn/tech/digi/2025-12-08/doc-inhaavuy3526263.shtml)、OSWorld 聚合榜见 §2.3。

### 7.2 三个关键事实

1. **【事实】DeepSeek 的独家成本结构**：视觉版与纯文本 V4-Flash **同价**，且**每张图片计费封顶 384 tokens**（官方文档）——computer-use 是截图密集型（每步一张），别家一张 1080p 截图往往折 1~2k+ tokens，DeepSeek 这条规则把「看屏」成本再砍 3~5 倍；叠加缓存命中 $0.007/M（系统提示词在多步循环里天然高命中）。峰谷计价：北京时间工作日 9:00-12:00/14:00-18:00 翻倍，其余时段半价。
2. **【事实】智谱当前不在一个档**：最强的 GLM-5V-Turbo OSWorld 62.3，比 Claude Sonnet 5（81.2）低约 19 分、比 Gemini 3.6 Flash（83）低约 21 分、比 Claude 旗舰（85）低约 23 分；上一代开源的 GLM-4.6V 只有 38.1。智谱自家对标也只敢对 Opus **4.6**（上上代）。做 computer-use 智谱暂不入围，观望后续版本。
3. **【事实+提醒】「国产 vs 海外」的真话是 Qwen 已反超**：老板问的是 DeepSeek/智谱，但国产阵营真正的第一是阿里 Qwen3.8（86.1% 开源登顶）；Kimi K3 也在 84.8。国产不等于将就。

### 7.3 每成功任务成本估算【估算，按 30 步/任务、每步一截图 + 上下文】

| 模型 | 单次尝试 | ÷成功率 ≈ 每成功任务 |
|------|------|------|
| DeepSeek vision-exp（谷时） | ~$0.03~0.06 | **~$0.04~0.08** |
| Qwen3.8-27B | ~$0.10 | ~$0.12 |
| GLM-5V-Turbo | ~$0.15 | ~$0.24（且 38% 失败的返工/风险另计） |
| Gemini 3.6 Flash | ~$0.2~0.3 | ~$0.25~0.35 |
| Claude Sonnet 5 | ~$0.4~0.9 | ~$0.5~1.1 |
| Claude Opus/Fable 5 | ~$2~4 | ~$2.5~5 |

注意：**对「操作电脑」这种有副作用的任务，失败成本＞token 成本**（失败可能把机器留在中间态），所以成功率低于 ~75% 的模型（智谱现状）省的钱是假的；成功率相近时才轮到拼单价。

### 7.4 性价比结论

- **第一梯队（建议双轨实测）**：①云端 **DeepSeek v4-flash-vision-exp**——单价是 Claude Sonnet 5 的约 1/10~1/45、图片计费封顶、我们已有官方账号（小智问答本来就直连 DeepSeek）接入成本≈0；风险=刚发布 8 天、带 `-exp` 后缀无稳定性承诺、成绩未经独立验证 → **先小流量跑自建评测集坐实再转主力**。②**Qwen3.8-27B**（百炼/硅基流动若已上架）——84.3% 独立聚合榜可查、$0.45/$3.20 白菜价、开源权重意味着将来可搬回 176 本地跑，是「云验证→本地化」的天然过渡。
- **闭源对照组**：Gemini 3.6 Flash 是 Claude/Gemini 阵营里性价比最好的（83% @ 低价、自带注入检测）；Claude Sonnet 5 买的是 GA 工具集成熟度与生态；Opus/Fable 5（$10/$50）只留给疑难任务升级档。两家都需海外支付与网络通道，运维成本国内三家为零。
- **智谱**：现阶段不用于 computer-use；GLM-4.6V-Flash（免费、9B、开源）可当边角料（低风险只读判断），等 GLM-5V 后续版本再评。

**一句话**：智谱与 Claude/Gemini 差约 19~23 分（一个档位），暂不可用；DeepSeek 若官方宣称坐实则与 Claude Sonnet 5 打平而价格低一个数量级，是性价比之王候选；但当下**已验证**的性价比之王是 Qwen3.8-27B，且开源可回迁本地——建议 DeepSeek+Qwen 双轨实测定主力。

### 7.5 追问核实：Qwen3.8-27B 本地有没有？（2026-08-29 21:45 实测）

**没有。本地现役是上一代 Qwen3.6-27B，差一代在 computer-use 上是一个档位。**

- 【实测】173:8001 vLLM 在线，`model=chatx max_len=24576` = `qwen36-27b-abl-awq`（**Qwen3.6**-27B 社区 abliterated AWQ 量化，文本兜底/翻译用）；176:11434 Ollama 在线，视觉主力是 `qwen3-vl:8b-instruct`（5.7GB），另有 qwen2.5vl:7b 等存量 tag。全网无任何 3.8 系权重。
- 【事实，官方模型卡】[Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)（2026-08-14 发布，Apache 2.0，**原生 VL**，dense 27B）官方对照表：OSWorld-Verified **3.8-27B = 84.3 vs 3.6-27B = 63.9**（+20.4 分）——现役 3.6-27B 做 computer-use 与智谱 GLM-5V-Turbo（62.3）同档，不够格。
- 【事实】官方仓只有 BF16 safetensors 55.6GB（单卡放不下）；社区量化已有 300+（unsloth/lmstudio 等，AWQ/4bit ≈ 15~16GB）→ **一张 5090 跑得动**，形态与 173 现役完全一致；国内拉权重走 ModelScope。
- 【现实约束，实施80 实测】176 空闲 2.3GB / 140 空闲 0.4GB / 198 被零调用的 qwen3:8b 钉死且 12GB 装不下 27B → **唯一顺路落点 = 173 原位换代**（同 vLLM 换模型路径；173 账：桌面常驻 8GB + utilization 0.70 = 21.6GB 预算，权重 15~16GB + KV 5~6GB 与现状同账）。3.8-27B 原生 VL，换完一台机器同时升级「兜底聊天+翻译+computer-use 大脑」三个角色。**但 173 是生产兜底链，换代属排产项**：等社区 AWQ 量化验证 + 过意向板 + 断云演习回归，不是顺手活。
- 立刻可用的替代：①云端百炼/硅基调 Qwen3.8-27B（$0.45/$3.20）；②本地 POC 先用 176 的 `qwen3-vl:8b-instruct`（官方同协议 computer-use，8B grounding 精度有限，小图标定位会失手）。

### 7.6 追问核实：173 从 3.6 换 3.8 可行吗？对聊天有影响吗？（2026-08-29 22:00 补查）

**结论：可行，且是「同形态原位换代」；正常时期聊天零感知（主链在云端硅基，老板锁），影响面只在断云兜底窗口与翻译兜底——预期变好，但有三个必验风险点。**

可行性三件事全部落实【事实】：
1. **去审查版已有现成**：[huihui-ai/Huihui-Qwen3.8-27B-abliterated](https://huggingface.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated)——与 173 现役 `qwen36-27b-abl` **同一作者家族**（176 的 Ollama 里就躺着 huihui_ai 的 qwen3-coder-abliterated），本代只削 18–51 层（保留更多原模型性能），视觉与 MTP 未动；配套 [AWQ 量化（vLLM 就绪，Marlin kernel + MTP 投机解码，A800 实测 110+ tok/s）](https://huggingface.co/shawnw3i/Huihui-Qwen3.8-27B-abliterated-AWQ-MTP) 与 [GGUF 全档](https://huggingface.co/mradermacher/Huihui-Qwen3.8-27B-abliterated-GGUF)（Q4_K_M 16.9GB）都已发布。
2. **显存同账**：AWQ 4bit 权重 ~15–16GB，与现役 3.6-27B AWQ 完全同量级，173 的 21.6GB vLLM 预算（utilization 0.70，桌面常驻 8GB）原样够用。⚠ NVFP4 快 1.5× 但要 24.6GB（[vLLM 官方 recipe 实测](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)）——173 装不下，放弃。
3. **vLLM 门槛**：[官方 recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B) 要求 ≥0.17.0（混合注意力 kernel）+ transformers ≥5.8.0（VL processor）。3.6/3.8 同属 qwen3.5 架构族，173 既然在跑 3.6-27B，vLLM 大概率已达标——**上机 `pip show vllm transformers` 核对即可**，文本模式服务可能连 transformers 都不用升。

对聊天的影响拆解：
- **正常时期＝零感知**：主对话在云端（硅基 DeepSeek-V3.2，老板锁），173 只在断云时顶班 + 做翻译兜底。
- **断云窗口＝预期更好**：3.8 对 3.6 是全面代差（官方卡数字，agentic/推理/token 效率都涨）。
- **三个必验风险**：① **thinking 空壳复发**——3.8 带 thinking 控制 + `--reasoning-parser qwen3`，而 chatx 昨天（08-28）刚修过一次「thinking 空壳」翻译 bug，换代后同坑必须重验（禁 thinking 或正确解析，翻译断言 `finish=stop` 且译文非空）；② **拒答/口吻漂移**——换模型=换嘴，7 人设 system prompt + 亲密陪聊/命理/危机话术三类样本跑拒答率与 persona eval 对比 3.6 基线（本代 abliterated 削层策略更保守，行为可能更贴官方，好事但要实测）；③ **量化来源**——AWQ 是三方转档（shawnw3i），上线前过自家门禁（断云演习 22 发 + 翻译回译质量 + persona_consistency），不过关就从 huihui BF16 自转 AWQ。
- **切换操作**：173 显存装不下双实例，影子对比不可行——走「低峰停 3.6 → 起 3.8 → 验证清单 → 不合格换回模型路径秒回滚（旧权重目录不删）」，与 08-15 换 3.6 同款流程；注意这台机器 08-28 刚出过「重启循环 2 小时不可用」事故，操作严格按实施80 修好的 drop-in（utilization 0.70 + drain 阈值 12000）执行，`served-model-name chatx` 保持不变＝引擎侧零配置改动。
- **附带红利**：3.8-27B 原生 VL（abliterated 版视觉未动）——第一阶段按纯文本模式上线（不开图像输入，显存账不变），将来 computer-use 立项时同一台服务开图像通道就是现成的本地大脑。

#### 7.6.1 换代执行记录（2026-08-30 00:18 实施完成）

- **结果**：173 vLLM 已从 3.6 换到 3.8 去限制版并在线服务。`chatx @ 192.168.0.173:8001` HTTP 200、`NRestarts=0`、`Application startup complete`（Uvicorn on :8001）。`served-model-name chatx` 不变＝引擎侧零改动。
- **崩溃根因（已确证并修复）**：AWQ 仓（`shawnw3i/Huihui-Qwen3.8-27B-abliterated-AWQ-MTP`）原始批量下载**未落全** `preprocessor_config.json` / `processor_config.json`（`.cache` 里只剩 `.lock`+`.incomplete`、无最终文件）→ vLLM 加载 VL 图像预处理器失败、反复崩溃（历史重启计数 112）。修法＝从**同一 AWQ 仓**（不是基座——`Qwen/Qwen3.8-27B` repo id 经 hf CLI 与直连均不可达）用 `curl` 从 hf-mirror **直取**这两个 config（HTTP 200，校验为合法 JSON、`image_processor_type=Qwen2VLImageProcessor`）补进 `/root/models/qwen38-27b-abl-awq/`。`video_preprocessor_config.json` 该仓不存在（无 `.lock`），无需补。
- **显存实况**：权重 18.24 GiB，加载 13.8s + `torch.compile` 63.9s（首启 HTTP 迟迟 000 的真实原因，非卡死）；稳定后 GPU **31.6 / 32.6 GiB**。⚠ 实机 drop-in 实为 `--gpu-memory-utilization 0.93 --max-model-len 24576`（沿用现役 3.6 的 override），**非本节前文设想的 0.70**；**视觉通道保留未关也未 OOM**，第一阶段仍只做文本兜底/翻译，`--limit-mm-per-prompt` 未启用（留作 OOM 逃生手段）。
- **功能验证通过**：`/v1/models=['chatx']`；`1+1=? → "2"`（content 非空、`reasoning_content` 为空＝**无 thinking 空壳**，08-28 的翻译空壳 bug 未复发）；翻译「你今天过得怎么样？」→ 仅输出 `How are you doing today?`；闲聊「一句话介绍南京」→ 连贯成句。
- **回滚预案（已就绪未使用）**：`cp /root/dropin.bak_pre_qwen38 /etc/systemd/system/vllm.service.d/override-temp-20260826.conf && systemctl daemon-reload && systemctl reset-failed vllm && systemctl restart vllm`（备份=切换前的 3.6 drop-in；旧权重目录 `qwen36-27b-abl-awq` 保留）。
