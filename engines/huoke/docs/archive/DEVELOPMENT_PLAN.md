# OpenClaw 升级开发文档 v2.0

> **文档版本**: 2.0 (优化版)
> **日期**: 2026-03-19
> **项目代号**: OpenClaw / 手机全自动操作系统
> **当前版本**: v0.4.0 (Host API)
> **目标版本**: v1.0.0

---

## 目录

1. [项目现状评估](#1-项目现状评估)
2. [技术趋势与选型分析](#2-技术趋势与选型分析)
3. [升级架构设计](#3-升级架构设计)
4. [分阶段开发计划](#4-分阶段开发计划)
5. [业务功能矩阵](#5-业务功能矩阵)
6. [成本与收益分析](#6-成本与收益分析)
7. [风险与应对](#7-风险与应对)
8. [验收标准与里程碑](#8-验收标准与里程碑)

---

## 1. 项目现状评估

### 1.1 已完成模块一览

| 模块 | 路径 | 完成度 | 说明 |
|------|------|--------|------|
| 设备管理 | `src/device_control/device_manager.py` | 95% | u2 + ADB 双通道，发现/连接/输入/截图/dump |
| 多设备编排 | `src/device_control/device_matrix.py` | 90% | SQLite 任务队列，原子抢占，设备亲和，负载均衡 |
| 设备看门狗 | `src/device_control/watchdog.py` | 85% | ADB/u2/网络/App 崩溃/验证码检测 + 自动恢复 |
| Telegram | `src/app_automation/telegram.py` | 95% | ~1800 行，u2 原子操作，搜索/发消息/文件/多账号/群组 |
| LinkedIn | `src/app_automation/linkedin.py` | 90% | ~900 行，搜索/连接/消息/发帖/点赞/评论/smart_outreach |
| WhatsApp | `src/app_automation/whatsapp.py` | 85% | ~550 行，搜索/消息/媒体/群组/状态 |
| Twitter/X | `src/app_automation/twitter.py` | 75% | 搜索/关注/点赞/转推/DM/时间线 |
| TikTok | `src/app_automation/tiktok.py` | 70% | 浏览/搜索/DM/关注/点赞 |
| Instagram | `src/app_automation/instagram.py` | 40% | 有 Python 类但依赖 GenericPlugin |
| Facebook | `src/app_automation/facebook.py` | 30% | 有 Python 类但依赖 GenericPlugin |
| 通用插件 | `src/app_automation/generic_plugin.py` | 80% | YAML → ActionFlow → AutoSelector 执行 |
| 应用注册表 | `src/app_automation/app_registry.py` | 90% | 加载 YAML 定义，创建插件实例 |
| 工作流引擎 | `src/workflow/engine.py` | 95% | 顺序/条件/循环/并行/变量/for_each/重试/依赖 |
| Action 注册 | `src/workflow/actions.py` | 85% | 全局注册表，内置 util/compliance actions |
| 获客管线 | `src/workflow/acquisition.py` | 80% | 发现→预热→触达→筛选→转化，升级规则 |
| 事件总线 | `src/workflow/event_bus.py` | 90% | Glob 订阅，异步处理，历史查询 |
| 智能调度 | `src/workflow/smart_schedule.py` | 85% | 时区/活跃窗口/抖动/黑名单/最佳发送时间 |
| Host API | `src/host/api.py` | 90% | FastAPI，80+ 端点，鉴权/设备/任务/调度/AI/工作流/leads |
| 任务执行器 | `src/host/executor.py` | 85% | 按 TaskType 分发到各平台自动化模块 |
| Worker 池 | `src/host/worker_pool.py` | 85% | 线程池 + 设备锁，并行不冲突 |
| 调度器 | `src/host/scheduler.py` | 80% | Cron 表达式，30s 轮询 |
| LLM 客户端 | `src/ai/llm_client.py` | 90% | DeepSeek/OpenAI/Local，重试/缓存/用量追踪 |
| 消息改写 | `src/ai/message_rewriter.py` | 85% | LLM 改写 + 预生成池 + 离线替换 |
| 自动回复 | `src/ai/auto_reply.py` | 80% | 意图分类 + 人设 + 历史上下文 |
| 视觉降级 | `src/ai/vision_fallback.py` | 75% | 截图→LLM→坐标（u2 失败时） |
| 意图分类 | `src/ai/intent_classifier.py` | 80% | 规则 + LLM 混合，Lead 意图判断 |
| 视觉后端 | `src/vision/backends.py` | 80% | LLMVision / OmniParser / Hybrid 三种后端 |
| 屏幕解析 | `src/vision/screen_parser.py` | 80% | XML + Vision 融合 → ParsedElement |
| 自动选择器 | `src/vision/auto_selector.py` | 85% | Vision→XML→YAML 缓存，自学习选择器 |
| 人类行为 | `src/behavior/human_behavior.py` | 90% | Bezier 滑动/高斯打字/泊松等待/会话节奏 |
| 合规限流 | `src/behavior/compliance_guard.py` | 90% | 每平台/动作/账号限频，滑动窗口 |
| Lead 存储 | `src/leads/store.py` | 85% | SQLite CRM，去重/评分/状态管线 |
| 可观测性 | `src/observability/` | 80% | 结构化日志/指标/执行存储/告警/密钥管理 |
| 容器化 | `Dockerfile` + `docker-compose.yml` | 70% | Python 3.13 + ADB，端口 8000 |

### 1.2 核心瓶颈

| 瓶颈 | 影响范围 | 严重程度 |
|------|---------|---------|
| **每个 App 需硬编码 u2 选择器** | 新 App 接入慢，App 更新需维护 | 高 |
| **Instagram/Facebook 仅有骨架** | 缺少完整的两大流量平台 | 高 |
| **Action 注册分散** | TG/LI/WA 需要手动实例化后注册 | 中 |
| **VisionFallback 仅用于 u2 失败时** | 未发挥 VLM 的主动屏幕理解能力 | 中 |
| **LLMClient 只支持 DeepSeek/OpenAI** | 无法用 Gemini/Qwen VL 等更优选项 | 中 |
| **无 Accessibility Service 通道** | 完全依赖 ADB/u2，延迟较高 | 中 |

### 1.3 现有优势（必须保留复用）

以下模块设计优秀，升级方案应在其基础上扩展而非重写：

1. **`VisionBackend` 抽象类** — 已有 `LLMVisionBackend`/`OmniParserBackend`/`HybridBackend`，新增云端 VLM 只需加一个子类
2. **`AutoSelector` 缓存机制** — Vision → XML → YAML 自学习，极大减少重复 VLM 调用（省钱）
3. **`ScreenParser` 融合模型** — XML + Vision 双源融合，新增 Accessibility 数据源水到渠成
4. **`BaseAutomation` 的 `guarded()` 上下文管理器** — 合规检查→动作→记录→延迟一体化
5. **`WorkflowExecutor`** — 完备的 DAG 执行引擎
6. **`AcquisitionPipeline`** — 完整的获客生命周期编排
7. **`EventBus`** — 解耦的跨平台联动
8. **`HumanBehavior`** — 7 个平台的行为画像

---

## 2. 技术趋势与选型分析

### 2.1 2026 年移动 AI 自动化技术全景

#### 视觉语言模型 (VLM) — 替代硬编码选择器

| 技术 | 来源 | 参数量 | AndroidWorld 得分 | 特点 |
|------|------|--------|------------------|------|
| **UI-TARS 2.0** | 字节跳动 (开源) | 7B/72B | 46.6 | 看截图输出动作，针对 GUI 训练 |
| **MobileVLM** | 小米 (开源) | — | — | 专为手机 UI 设计的两阶段预训练 |
| **gWorld** | trillion-labs | 8B/32B | — | 生成可执行代码，0.3s 渲染 |
| **Gemini 2.5 Flash** | Google (API) | — | — | 视觉能力强，免费额度，延迟低 |
| **GPT-4.1** | OpenAI (API) | — | — | 最强综合理解，1M 上下文 |
| **Qwen VL Plus** | 阿里 (API) | — | — | 最便宜，中文理解好 |
| **Claude Sonnet 4.6** | Anthropic (API) | — | — | Computer Use 原生支持 |

**关键洞察**：VLM 让自动化从 "每个 App 写选择器" 变成 "AI 看图操作"。App 更新不影响，新 App 零代码接入。

#### Android 无障碍服务 Agent

| 技术 | 类型 | 特点 |
|------|------|------|
| **Orb Eye** | Java APK，HTTP API (端口 7333) | UI 树/截图/通知/输入(含中文)，为 AI Agent 设计 |
| **DroidClaw** | TypeScript Agent | 感知-推理-行动循环，多 App 工作流 |
| **MobClaw** | Kotlin 原生 | `AccessibilityService.dispatchGesture`，可插拔 LLM |
| **Google Gemini Bonobo** | Android 16 原生 | 内置多步骤自动化（仅限合作 App） |

**关键洞察**：无障碍服务比 ADB dump 更快（系统级实时数据），比 u2 更稳定（无需 atx-agent 进程），且不需要 root。

#### 云手机方案

| 技术 | 特点 | 局限 |
|------|------|------|
| **AutoGLM 2.0** (智谱) | 云端执行，~$0.2/任务 | 不可控，依赖平台，不适合大规模获客 |
| **Google Gemini Bonobo** | Android 原生 | 仅限合作 App，无法定制 |

**结论**：云手机方案受限于平台策略和可控性，**不适合**本项目的获客场景。

### 2.2 云端 VLM vs 本地 GPU 对比

#### 成本模型（每台设备每天 500 次截图识别）

| 模型 | Input $/M tokens | Output $/M tokens | 月成本/台 | UI 理解能力 |
|------|------------------|-------------------|----------|------------|
| **Qwen VL Plus** | $0.14 | $0.41 | ~$4.5 | 良好 |
| **Gemini 2.5 Flash Vision** | $0.30 | $2.50 | ~$13 | 良好 |
| **Gemini 2.5 Pro** | $1.25 | $10.00 | ~$51 | 优秀 |
| **GPT-4.1** | $2.00 | $8.00 | ~$69 | 优秀 |
| **Claude Sonnet 4.6** | $3.00 | $15.00 | ~$110 | 优秀 (Computer Use) |

| 本地 GPU | VRAM | 一次性投入 | 月摊 + 电费 | 可服务设备数 |
|----------|------|-----------|------------|------------|
| RTX 4060 Ti 16GB | 16GB | ~$400 | ~$55 | 3-5 台 |
| RTX 3090 24GB (二手) | 24GB | ~$650 | ~$84 | 5-10 台 |

#### 延迟对比

| 方案 | 单次推理 | 10 步操作总耗时 |
|------|---------|---------------|
| 本地 UI-TARS 7B | 0.5-1.5s | 5-15s |
| Gemini 2.5 Flash | 1.5-2.5s | 15-25s |
| Qwen VL Plus | 2-3s | 20-30s |
| GPT-4.1 | 2-3.5s | 20-35s |

#### 选型结论

**初期（1-20 台设备）→ 云端为主**：

- 零硬件投入，即开即用
- 模型能力更强（GPT-4.1 > UI-TARS 7B）
- 开发调试更快
- 弹性伸缩

**规模化（20+ 台设备）→ 混合方案**：

- 热路径（简单确认/重复操作）走本地 GPU
- 冷路径（复杂判断/新场景）走云端
- AutoSelector 缓存减少 80%+ 的 VLM 调用

### 2.3 最终技术选型

| 组件 | 选型 | 备选 | 理由 |
|------|------|------|------|
| **主力 VLM** | Gemini 2.5 Flash Vision | Qwen VL Plus | 价格/能力/延迟均衡，有免费额度 |
| **复杂判断 VLM** | GPT-4.1 | Claude Sonnet 4.6 | 最强理解力，1M 上下文 |
| **廉价确认 VLM** | Qwen VL Plus | Gemini Flash | 最便宜，$0.14/M |
| **文案 LLM** | DeepSeek V3 | GPT-4.1 | 中文强 + 便宜，已集成 |
| **无障碍服务** | Orb Eye | MobClaw | HTTP API 简洁，支持中文，开源 |
| **执行层** | Orb Eye API + u2 fallback | — | 渐进迁移，不破坏现有功能 |
| **工作流/获客** | 保留现有 | — | 设计优秀，无需重写 |
| **行为/合规** | 保留现有 | — | 已覆盖 7 平台画像 |

---

## 3. 升级架构设计

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Host API (FastAPI)                        │
│   /tasks  /workflows  /leads  /devices  /dashboard  /ai  ...   │
└────────────┬──────────────────────────────────────┬─────────────┘
             │                                      │
┌────────────▼────────────┐        ┌────────────────▼─────────────┐
│    WorkflowExecutor     │        │      AcquisitionPipeline     │
│    (engine.py 保留)     │        │      (acquisition.py 保留)    │
│    + ActionRegistry     │        │      + EventBus + LeadStore  │
└────────────┬────────────┘        └────────────────┬─────────────┘
             │                                      │
             └──────────────┬───────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                    UniversalAppAgent (新增)                       │
│                                                                  │
│   给定 goal → 自主完成多步操作                                      │
│   内部循环: 感知 → 理解 → 决策 → 执行 → 验证                         │
│                                                                  │
│   ┌─────────────┐  ┌───────────────┐  ┌──────────────────────┐  │
│   │ 现有专用模块  │  │ UniversalMode │  │ YAML GenericPlugin   │  │
│   │ TG/LI/WA等   │  │ (VLM 驱动)    │  │ (已有, 用 VLM 增强)  │  │
│   │ 高可靠路径    │  │ 任意 App 通用  │  │ 中间路径             │  │
│   └──────┬──────┘  └───────┬───────┘  └──────────┬───────────┘  │
│          │                 │                      │              │
│          └─────────────────┼──────────────────────┘              │
│                            │                                     │
└────────────────────────────┼─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│                       感知理解层 (VLMRouter)                       │
│                                                                   │
│   ┌──────────┐  ┌──────────────┐  ┌───────────┐  ┌───────────┐  │
│   │ Tier 1   │  │ Tier 2       │  │ Tier 3    │  │ Tier 4    │  │
│   │ Qwen VL  │  │ Gemini Flash │  │ GPT-4.1   │  │ Local GPU │  │
│   │ $0.14/M  │  │ $0.30/M      │  │ $2.00/M   │  │ (未来)    │  │
│   │ 简单确认  │  │ 日常主力     │  │ 复杂判断   │  │ 热路径    │  │
│   └──────────┘  └──────────────┘  └───────────┘  └───────────┘  │
│                                                                   │
│   + AutoSelector 缓存 (已有) → 减少 80% VLM 调用                   │
│   + ScreenParser 融合 (已有) → XML + VLM + Accessibility 三源融合  │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│                        执行控制层                                  │
│                                                                   │
│   ┌─────────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│   │ Orb Eye API     │  │ u2 (现有)    │  │ ADB (现有)        │  │
│   │ 无障碍服务 新增  │  │ 保留为主通道  │  │ 保留为最终降级     │  │
│   │ 端口 7333       │  │ atx-agent    │  │ shell input       │  │
│   │ 零延迟 设备端    │  │ 设备端执行   │  │ 主机端执行         │  │
│   └─────────────────┘  └──────────────┘  └───────────────────┘  │
│                                                                   │
│   优先级: Orb Eye → u2 → ADB                                     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│                    设备基础设施 (保留)                               │
│                                                                   │
│   DeviceManager + DeviceMatrix + Watchdog + WorkerPool            │
│   + HumanBehavior + ComplianceGuard + HealthMonitor               │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 核心新增模块设计

#### 3.2.1 VLMRouter — 多模型智能路由

**位置**: `src/ai/vlm_router.py`

**设计原则**:
- 建立在现有 `VisionBackend` 抽象类之上
- 每个云端 VLM 实现为一个 `VisionBackend` 子类
- 根据任务复杂度和上下文自动选择 Tier
- AutoSelector 缓存命中时跳过 VLM 调用（成本为零）
- 主模型超时/失败 → 自动降级到备选

```
VLMRouter
├── GeminiVisionBackend(VisionBackend)     # Tier 2 主力
├── QwenVisionBackend(VisionBackend)       # Tier 1 廉价
├── OpenAIVisionBackend(VisionBackend)     # Tier 3 复杂
├── LLMVisionBackend(VisionBackend)        # 现有 DeepSeek
├── OmniParserBackend(VisionBackend)       # 现有
├── LocalUITarsBackend(VisionBackend)      # Tier 4 未来
└── AutoSelector 缓存层                    # 命中则跳过 VLM
```

**Tier 选择逻辑**:

```
if AutoSelector 缓存命中:
    return 缓存结果 (零成本)

if 任务类型 == "简单确认" (如: 检查页面是否加载完):
    use Tier 1 (Qwen VL, 最便宜)

elif 任务类型 == "标准操作" (如: 找到搜索框并点击):
    use Tier 2 (Gemini Flash, 性价比最高)

elif 任务类型 == "复杂判断" (如: 未知弹窗、验证码、异常UI):
    use Tier 3 (GPT-4.1, 最强理解)

elif 网络不可用:
    use Tier 4 (本地 GPU, 如已部署)
    else: 退回到 u2 选择器
```

**成本优化关键**: `AutoSelector` 的 YAML 缓存让同一 App 的同一操作在首次 VLM 调用后产生可复用的选择器。实测中，成熟 App 的缓存命中率可达 80-95%，这意味着实际 VLM 调用量远低于理论上限。

#### 3.2.2 OrbEyeChannel — 无障碍服务执行通道

**位置**: `src/device_control/orb_eye.py`

**设计原则**:
- 封装 Orb Eye 的 HTTP API (端口 7333)
- 实现与 DeviceManager 相同的输入接口
- 自动检测 Orb Eye 是否可用，不可用时回退 u2

**核心 API 映射**:

| DeviceManager 方法 | Orb Eye HTTP API | 优势 |
|-------------------|-----------------|------|
| `input_tap(x, y)` | `POST /tap {x, y}` | 设备端执行，零延迟 |
| `input_swipe(...)` | `POST /swipe {...}` | 支持复合手势 |
| `input_text(text)` | `POST /type {text}` | 原生中文支持 |
| `dump_ui_hierarchy()` | `GET /tree` | 实时无障碍树，比 dump 快 |
| `capture_screen()` | `GET /screenshot` | 通过无障碍服务截图 |
| — | `GET /notifications` | 新增：通知监听 |
| — | `GET /clipboard` | 新增：剪贴板读写 |

**输入通道优先级**:
```
Orb Eye (无障碍, 最快最稳) → u2 (atx-agent) → ADB (shell input, 最终降级)
```

#### 3.2.3 UniversalAppAgent — 通用 App 操作器

**位置**: `src/app_automation/universal_agent.py`

**核心理念**:
不再为每个 App 写专用自动化类。给 Agent 一个目标描述，它自主完成。

**工作循环**:

```
                    ┌──────────────────┐
                    │  接收 Goal       │
                    │  "在 Instagram   │
                    │   搜索 XXX 并    │
                    │   发送消息 YYY"  │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
          ┌────────│  感知 (Perceive) │
          │        │  截图 + UI 树    │
          │        └────────┬─────────┘
          │                 │
          │        ┌────────▼─────────┐
          │        │  理解 (Understand)│
          │        │  VLMRouter 分析   │
          │        │  当前屏幕状态     │
          │        └────────┬─────────┘
          │                 │
          │        ┌────────▼─────────┐
          │        │  决策 (Decide)   │
     重复  │        │  LLM 决定下一步  │
     直到  │        │  动作 + 参数     │
     完成  │        └────────┬─────────┘
          │                 │
          │        ┌────────▼─────────┐
          │        │  执行 (Execute)  │
          │        │  Orb Eye / u2    │
          │        │  + HumanBehavior │
          │        └────────┬─────────┘
          │                 │
          │        ┌────────▼─────────┐
          │        │  验证 (Verify)   │
          │        │  操作是否成功？   │
          │        │  是 → 下一步     │
          └────────│  否 → 重试/回退  │
                   └──────────────────┘
```

**与现有专用模块的关系**:

```
操作请求到达
    │
    ├─ 有专用模块且选择器有效？(TG/LI/WA)
    │   └─ YES → 用专用模块 (最快最稳，保留现有投入)
    │
    ├─ 有 YAML GenericPlugin 定义？(IG/FB/TT/X)
    │   └─ YES → 用 GenericPlugin + VLM 增强找元素
    │
    └─ 都没有？(任何新 App)
        └─ UniversalAppAgent 完全自主操作
```

这个 **三级降级策略** 确保：
1. 已投入的 TG/LI/WA 专用代码继续发挥价值（最高可靠性）
2. 有 YAML 配置的 App 用 VLM 增强（中等可靠性 + 低维护）
3. 全新 App 用 UniversalAgent（零代码接入）

#### 3.2.4 App Profile YAML — 轻量级 App 描述

**位置**: `config/apps/<app>.yaml` (扩展现有格式)

现有的 YAML 格式已经很好，只需增加一个 `hints` 字段给 VLM 提供上下文提示：

```yaml
# config/apps/instagram.yaml — 升级后
package: com.instagram.android
name: Instagram
behavior_profile: instagram

compliance:
  hourly_total: 50
  daily_total: 250
  actions:
    follow: {hourly: 10, daily: 60}
    like: {hourly: 20, daily: 100}
    send_dm: {hourly: 5, daily: 20}

# 新增: VLM 提示词 — 告诉 AI 这个 App 的 UI 特征
hints:
  navigation: "底部有5个 Tab: 首页/搜索/Reels/购物/个人"
  search_entry: "点击底部搜索图标(放大镜)进入搜索页"
  dm_entry: "首页右上角纸飞机图标进入私信"
  common_popups:
    - "登录弹窗: 点击 Not Now"
    - "通知权限: 点击 Not Now"
    - "推荐关注: 滑动关闭或点击 X"

# 现有的 actions 定义保留，VLM 用 hints 做补充
actions:
  send_dm:
    description: "Send a direct message"
    params: [recipient, message]
    hints: "私信页面顶部有搜索框，输入后选择第一个匹配用户"
    steps:
      - find: "Direct messages icon (paper plane)"
        action: tap
      # ... 现有步骤保留
```

**关键设计**：`hints` 是**可选的**。UniversalAppAgent 在没有 hints 时也能工作（纯 VLM 理解），有 hints 时准确率更高、VLM 调用更少。

### 3.3 配置文件升级

#### config/ai.yaml — 新增 VLM 路由配置

```yaml
llm:
  provider: deepseek
  model: deepseek-chat
  # ... 现有配置保留

# 新增: VLM 路由配置
vlm:
  default_tier: "standard"
  
  providers:
    gemini:
      api_key_env: "GEMINI_API_KEY"
      model: "gemini-2.5-flash"
      vision_model: "gemini-2.5-flash"
      base_url: "https://generativelanguage.googleapis.com/v1beta"
      tier: "standard"
      timeout_sec: 15.0
      max_retries: 2
    
    qwen:
      api_key_env: "DASHSCOPE_API_KEY"
      model: "qwen-vl-plus"
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
      tier: "fast"
      timeout_sec: 15.0
      max_retries: 2
    
    openai:
      api_key_env: "OPENAI_API_KEY"
      model: "gpt-4.1"
      tier: "complex"
      timeout_sec: 30.0
      max_retries: 3
    
    local:
      base_url: "http://localhost:8080/v1"
      model: "UI-TARS-1.5-7B"
      tier: "local"
      enabled: false  # 有本地 GPU 时开启

  tier_routing:
    fast: "qwen"
    standard: "gemini"
    complex: "openai"
    local: "local"

  fallback_chain: ["gemini", "qwen", "openai"]

  cache:
    enabled: true
    max_entries: 5000
    ttl_sec: 3600

# 新增: 无障碍服务配置
accessibility:
  orb_eye:
    enabled: true
    port: 7333
    connect_timeout_sec: 5.0
    prefer_over_u2: true

vision:
  hourly_budget: 50   # 从 20 提升到 50（云端 VLM 成本更低）
  cache_ttl_sec: 600  # 延长缓存
```

---

## 4. 分阶段开发计划

### Phase 1: VLM 云端集成 + 感知升级（第 1-3 周）

**目标**: 让系统能通过云端 VLM "看懂"任何 App 的界面

#### 第 1 周: VLM Provider 实现

| 任务 | 文件 | 说明 | 预计 |
|------|------|------|------|
| 1.1 GeminiVisionBackend | `src/vision/backends.py` | 继承 VisionBackend，调用 Gemini 2.5 Flash Vision API | 1.5 天 |
| 1.2 QwenVisionBackend | `src/vision/backends.py` | 继承 VisionBackend，调用 Qwen VL Plus API | 1 天 |
| 1.3 OpenAIVisionBackend | `src/vision/backends.py` | 继承 VisionBackend，调用 GPT-4.1 Vision API | 1 天 |
| 1.4 VLMRouter | `src/ai/vlm_router.py` | 多 Tier 路由 + 降级 + 成本追踪 | 1.5 天 |

**代码路径**:
```
现有 VisionBackend (ABC)
  ├── LLMVisionBackend   ← 已有 (DeepSeek)
  ├── OmniParserBackend  ← 已有
  ├── HybridBackend      ← 已有
  ├── GeminiVisionBackend   ← 新增
  ├── QwenVisionBackend     ← 新增
  └── OpenAIVisionBackend   ← 新增

VLMRouter (新增)
  ├── 根据 tier 选择 backend
  ├── 调用 AutoSelector 缓存 (已有)
  └── fallback chain
```

#### 第 2 周: 感知层融合

| 任务 | 文件 | 说明 | 预计 |
|------|------|------|------|
| 2.1 ScreenParser 支持 VLMRouter | `src/vision/screen_parser.py` | parse() 方法接受 VLMRouter | 1 天 |
| 2.2 VisionFallback 切换到 VLMRouter | `src/ai/vision_fallback.py` | 替换单一 LLM 调用为路由调用 | 0.5 天 |
| 2.3 AutoSelector 集成 VLMRouter | `src/vision/auto_selector.py` | find 未命中时用 VLMRouter | 0.5 天 |
| 2.4 config/ai.yaml 升级 | `config/ai.yaml` | 添加 VLM providers/tier 配置 | 0.5 天 |
| 2.5 Host API 端点 | `src/host/api.py` | /vlm/stats, /vlm/test 端点 | 0.5 天 |
| 2.6 集成测试 | `tests/test_vlm_router.py` | Mock 测试 + 真实 API 冒烟测试 | 2 天 |

**验收标准**:
- [ ] `VLMRouter.analyze_screen(screenshot)` 正确返回 UI 元素列表
- [ ] Gemini/Qwen/GPT-4.1 三个 backend 均可独立工作
- [ ] AutoSelector 缓存命中时跳过 VLM 调用
- [ ] fallback chain: Gemini 失败 → Qwen → OpenAI

#### 第 3 周: Orb Eye 集成

| 任务 | 文件 | 说明 | 预计 |
|------|------|------|------|
| 3.1 OrbEyeChannel | `src/device_control/orb_eye.py` | HTTP API 封装 | 2 天 |
| 3.2 DeviceManager 集成 | `src/device_control/device_manager.py` | 新增 Orb Eye 通道，优先级路由 | 1 天 |
| 3.3 ScreenParser 接入 Accessibility 数据 | `src/vision/screen_parser.py` | 三源融合: XML + VLM + Accessibility | 1 天 |
| 3.4 手机端部署 Orb Eye APK | — | 在 Redmi 13C 上安装测试 | 0.5 天 |
| 3.5 端到端测试 | `tests/test_orb_eye.py` | Orb Eye → tap/swipe/type 全流程 | 0.5 天 |

**验收标准**:
- [ ] Orb Eye HTTP API 在 Redmi 13C 上正常工作
- [ ] 输入通道优先级: Orb Eye → u2 → ADB 自动切换
- [ ] 中文文本输入通过 Orb Eye 正常工作
- [ ] ScreenParser 支持 Accessibility 树作为数据源

---

### Phase 2: UniversalAppAgent + 全平台覆盖（第 4-6 周）

**目标**: 一个 Agent 操作所有 App，6 平台全部可用

#### 第 4 周: UniversalAppAgent 核心

| 任务 | 文件 | 说明 | 预计 |
|------|------|------|------|
| 4.1 Agent 核心循环 | `src/app_automation/universal_agent.py` | 感知→理解→决策→执行→验证 循环 | 3 天 |
| 4.2 动作原语标准化 | `src/app_automation/universal_agent.py` | tap/type/swipe/back/home/scroll/wait | 1 天 |
| 4.3 App hints 加载 | `src/app_automation/app_registry.py` | 从 YAML 读取 hints 字段 | 0.5 天 |
| 4.4 弹窗自动处理 | `src/app_automation/universal_agent.py` | VLM 识别弹窗 → LLM 判断动作 | 0.5 天 |

**Agent 决策 Prompt 模板**:
```
你是一个 Android 手机操作 Agent。

当前目标: {goal}
当前 App: {app_name} ({package})
已完成步骤: {history}
App 提示: {hints}

屏幕截图已附上。请分析当前屏幕状态，并决定下一步操作。

可用动作:
- tap(x, y) — 点击坐标
- type(text) — 输入文字（当前焦点输入框）
- swipe(x1, y1, x2, y2) — 滑动
- back() — 返回键
- home() — 主页键
- scroll(direction) — 上/下/左/右滚动
- wait(seconds) — 等待
- done(result) — 任务完成
- fail(reason) — 任务失败

返回 JSON:
{
  "screen_state": "描述当前屏幕",
  "reasoning": "为什么选择这个动作",
  "action": "tap",
  "params": {"x": 540, "y": 960},
  "confidence": 0.9
}
```

#### 第 5 周: 全平台覆盖

| 任务 | 说明 | 预计 |
|------|------|------|
| 5.1 Instagram hints 完善 | 完善 config/apps/instagram.yaml 的 hints | 0.5 天 |
| 5.2 Facebook hints 完善 | 完善 config/apps/facebook.yaml 的 hints | 0.5 天 |
| 5.3 统一 Action 注册 | 系统启动时自动注册所有平台 | 1 天 |
| 5.4 三级降级策略实现 | 专用模块 → GenericPlugin+VLM → UniversalAgent | 1 天 |
| 5.5 各平台 E2E 测试 | 6 平台逐一测试核心流程 | 2 天 |

**验收标准**:
- [ ] Instagram: 搜索用户、关注、点赞、评论、发 DM — 全部通过
- [ ] Facebook: 搜索、添加好友、发消息 — 全部通过
- [ ] 现有 TG/LI/WA 功能不受影响（三级降级策略生效）
- [ ] Twitter/TikTok 可通过 UniversalAgent 补全缺失功能

#### 第 6 周: 异常处理 + 稳定性

| 任务 | 说明 | 预计 |
|------|------|------|
| 6.1 操作失败自动重试 | VLM 验证操作结果，失败自动调整策略 | 1.5 天 |
| 6.2 App 更新自适应 | AutoSelector 缓存失效检测 + 自动重学习 | 1 天 |
| 6.3 验证码/异常弹窗 | Watchdog 检测 + UniversalAgent 处理 | 1 天 |
| 6.4 多分辨率适配 | VLM 坐标按屏幕尺寸归一化 | 0.5 天 |
| 6.5 性能优化 | 截图压缩、VLM 并发、缓存预热 | 1 天 |

---

### Phase 3: 业务功能全覆盖（第 7-10 周）

**目标**: 添加好友、获客、养号、维护、转化全流程闭环

#### 第 7-8 周: 获客与养号功能

| 功能 | 实现方式 | 涉及平台 | 预计 |
|------|---------|---------|------|
| **自动添加好友/关注** | 搜索 → 浏览资料 → 点击关注/Connect/添加好友 | 全 6 平台 | 2 天 |
| **关键词获客发现** | 按关键词搜索 → VLM 读取资料 → 存入 LeadStore | 全 6 平台 | 2 天 |
| **浏览养号** | 定时刷 Feed → 随机点赞/评论 → HumanBehavior 控制节奏 | 全 6 平台 | 2 天 |
| **内容互动** | 看帖 → AI 生成评论 → 发布 | LI/TW/TT/IG | 1.5 天 |
| **账号资料维护** | 定期更新头像/简介/状态 | TG/WA/LI | 1 天 |
| **多账号轮换** | 同设备多账号切换，均匀分配操作量 | TG/WA | 1 天 |
| **行为多样化** | 每天变化操作顺序和时间，避免模式固定 | 全 6 平台 | 1.5 天 |

#### 第 9-10 周: 维护与转化功能

| 功能 | 实现方式 | 涉及平台 | 预计 |
|------|---------|---------|------|
| **消息监控与自动回复** | 监控新消息 → IntentClassifier 判断 → AutoReply 回复 | TG/WA/LI | 2 天 |
| **对话管理** | 历史对话存储 → 上下文感知回复 | TG/WA/LI | 1.5 天 |
| **Lead 评分自动升级** | 互动次数/质量 → 分数变化 → EventBus 触发升级 | 全平台 | 1 天 |
| **跨平台迁移** | LI 发现 → TW 预热 → TG/WA 转化 | 跨平台 | 2 天 |
| **个性化话术** | 根据 Lead 资料 + 平台 + 阶段 → AI 生成消息 | 全平台 | 1.5 天 |
| **转化追踪** | 从发现到成交全链路记录在 LeadStore | 全平台 | 1 天 |
| **批量操作优化** | 批量发送/关注，合规限频内最大化效率 | 全 6 平台 | 1 天 |

---

### Phase 4: 规模化与智能化（第 11-14 周）

| 任务 | 说明 | 预计 |
|------|------|------|
| **多设备并行优化** | DeviceMatrix 自动分配，每台设备独立 Agent | 1.5 周 |
| **智能时段调度** | SmartSchedule 根据 Lead 时区和平台最佳时段自动排期 | 0.5 周 |
| **自适应风控** | 检测限流/封号预警 → 降频 → 切设备/账号 | 1 周 |
| **数据看板升级** | Dashboard: 获客漏斗、转化率、每平台活跃度、成本 | 1 周 |

---

## 5. 业务功能矩阵

### 5.1 获客全生命周期

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   ① 发现 Discovery                                         │
│   ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐      │
│   │  LI   │ │  TW   │ │  TT   │ │  IG   │ │  FB   │      │
│   │ 搜索  │ │ 搜索  │ │ 搜索  │ │ 搜索  │ │ 搜索  │      │
│   │ 关键词│ │ 话题  │ │ 用户  │ │ 话题  │ │ 群组  │      │
│   └───┬───┘ └───┬───┘ └───┬───┘ └───┬───┘ └───┬───┘      │
│       └─────────┼─────────┼─────────┼─────────┘           │
│                 ▼                                           │
│   ② 预热 Warm-up (养号 + 建立认知)                          │
│   • 浏览 Feed, 随机点赞                                     │
│   • 观看视频/内容, 停留真实时长                               │
│   • 评论 (AI 生成有价值的内容)                               │
│   • 关注/Connect (分批, HumanBehavior 控制节奏)              │
│                 │                                           │
│                 ▼                                           │
│   ③ 触达 Engage                                             │
│   • 发 DM / 私信 (AI 个性化消息)                             │
│   • 发连接请求 + 备注 (LI)                                  │
│   • 回复评论深入互动                                         │
│                 │                                           │
│                 ▼                                           │
│   ④ 筛选 Qualify                                            │
│   • IntentClassifier 分析回复意图                            │
│   • Lead 评分: 互动次数 + 回复质量 + 资料完整度              │
│   • 达标 → 升级; 无回应 → 重新预热或放弃                     │
│                 │                                           │
│                 ▼                                           │
│   ⑤ 转化 Convert                                            │
│   • 跨平台迁移: LI/TW → TG/WA (更私密的对话)               │
│   • 深度对话: AutoReply + 历史上下文                         │
│   • 成交记录: LeadStore status → converted                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 养号策略

**目标**: 让账号看起来是真人在用，避免被平台封号

| 行为 | 频率 | 实现方式 |
|------|------|---------|
| 浏览 Feed | 每天 3-5 次，每次 5-15 分钟 | SmartSchedule + HumanBehavior |
| 点赞 | 每天 10-30 个 | ComplianceGuard 限频 + 随机间隔 |
| 评论 | 每天 3-10 条 | AI 生成有价值评论 + 合规限频 |
| 关注 | 每天 5-15 人 | 分散在活跃时段 + 抖动 |
| 发帖/状态 | 每周 2-5 条 | AI 生成内容 + 定时发布 |
| 回复消息 | 实时或延迟 5-30 分钟 | AutoReply + 阅读延迟模拟 |
| 切换 App | 自然切换，非连续操作同一 App | SessionProfile 控制 |

**关键**: HumanBehavior 的 SessionProfile 确保每次操作都有自然的预热期（warmup_rate_factor），操作间有 Gaussian 分布的随机延迟，滑动用 Bezier 曲线。

### 5.3 功能 × 平台覆盖矩阵

| 功能 | TG | WA | LI | TW | TT | IG | FB |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 搜索用户 | ✅ | ✅ | ✅ | ✅ | ✅ | 🔶 | 🔶 |
| 添加好友/关注 | ✅ | — | ✅ | ✅ | ✅ | 🔶 | 🔶 |
| 发私信/DM | ✅ | ✅ | ✅ | ✅ | ✅ | 🔶 | 🔶 |
| 浏览 Feed | ✅ | ✅ | ✅ | ✅ | ✅ | 🔶 | 🔶 |
| 点赞 | — | — | ✅ | ✅ | ✅ | 🔶 | 🔶 |
| 评论 | — | — | ✅ | ✅ | ✅ | 🔶 | 🔶 |
| 发帖/状态 | ✅ | ✅ | ✅ | ✅ | — | 🔶 | 🔶 |
| 多账号 | ✅ | — | — | — | — | — | — |
| 消息监控 | ✅ | ✅ | ✅ | — | — | — | — |
| 自动回复 | ✅ | ✅ | ✅ | — | — | — | — |
| Lead 收集 | ✅ | — | ✅ | ✅ | ✅ | 🔶 | 🔶 |

图例: ✅ = 专用模块已实现, 🔶 = 升级后通过 UniversalAgent/GenericPlugin 实现

---

## 6. 成本与收益分析

### 6.1 VLM API 成本预估

**场景**: 5 台手机，每台每天 500 次截图识别

**优化后实际调用量**（考虑 AutoSelector 缓存）:

| 阶段 | 缓存命中率 | 实际 VLM 调用/天/台 | 说明 |
|------|-----------|-------------------|------|
| 初始（第 1 周） | 0% | 500 | 全部需要 VLM |
| 学习期（第 2-3 周） | 50% | 250 | 常见操作已缓存 |
| 成熟期（第 4+ 周） | 80-90% | 50-100 | 仅新场景/异常需要 VLM |

**成熟期月成本（5 台设备）**:

| 项目 | 调用量/天 | 月成本 |
|------|----------|-------|
| Gemini Flash (80% 调用) | 5×80=400 | ~$10 |
| Qwen VL (15% 简单确认) | 5×15=75 | ~$1 |
| GPT-4.1 (5% 复杂判断) | 5×5=25 | ~$3 |
| DeepSeek V3 (文案生成) | — | ~$5 |
| **总计** | — | **~$19/月** |

**对比纯选择器方案**: $0/月 API 费用，但每次 App 更新需 1-3 天人工维护选择器。
**对比无缓存的云端方案**: ~$78/月。AutoSelector 缓存节省约 75%。

### 6.2 开发投入预估

| Phase | 时间 | 人力 | 核心产出 |
|-------|------|------|---------|
| Phase 1 | 3 周 | 1 人 | VLM 路由 + Orb Eye 集成 |
| Phase 2 | 3 周 | 1 人 | UniversalAgent + 6 平台 |
| Phase 3 | 4 周 | 1 人 | 获客/养号/维护/转化全功能 |
| Phase 4 | 4 周 | 1 人 | 规模化 + 智能化 |
| **总计** | **14 周** | — | **v1.0 全功能版** |

### 6.3 效益对比

| 指标 | 当前 (v0.4) | 升级后 (v1.0) |
|------|------------|--------------|
| 支持平台数 | 3 个完整 + 2 个部分 | **6 个全覆盖** + 任意新 App |
| 新 App 接入时间 | 3-5 天 (写选择器) | **0.5 天** (写 hints YAML) |
| App 更新维护 | 1-3 天/次 | **~0** (VLM 自适应) |
| 操作可靠性 | 85% (选择器可能过期) | **95%+** (VLM + 缓存 + 降级) |
| 自动化率 | 60% (需人工处理异常) | **90%+** (VLM 处理弹窗/异常) |
| 每台设备日处理 Lead | ~20 | **50-100** (更快更稳) |

---

## 7. 风险与应对

| 风险 | 概率 | 影响 | 应对方案 |
|------|------|------|---------|
| VLM API 大幅涨价 | 低 | 中 | VLMRouter 抽象层 → 快速切换提供商；AutoSelector 缓存降低依赖 |
| VLM API 停服 | 低 | 高 | fallback chain + 本地 GPU 备选 + u2 选择器降级 |
| Orb Eye 在部分机型不兼容 | 中 | 中 | 保留 u2 为主通道，Orb Eye 为增强通道 |
| 平台封号 | 高 | 高 | ComplianceGuard 限频 + HumanBehavior + 多账号 + 自适应降频 |
| VLM 误判导致操作错误 | 中 | 中 | 操作前后截图对比验证 + 重要操作需高 confidence |
| 数据隐私（截图上传云端） | 中 | 中 | 截图脱敏（模糊非操作区域）+ Qwen 在阿里云 |
| 多设备并发 VLM 调用被限流 | 中 | 低 | 多 API Key 轮转 + 请求队列 + 缓存减少调用 |

---

## 8. 验收标准与里程碑

### 里程碑 1: v0.5.0 — 感知升级（第 3 周末）

- [ ] VLMRouter 正常工作，Gemini/Qwen/GPT-4.1 三个 backend 可用
- [ ] AutoSelector 缓存 + VLM 联动，首次 VLM → 缓存 → 后续直接使用
- [ ] Orb Eye 在 Redmi 13C 上正常工作
- [ ] 现有 TG/LI/WA 功能全部正常（不退化）
- [ ] API 端点: /vlm/stats 可查询调用量和成本

### 里程碑 2: v0.6.0 — 通用操作（第 6 周末）

- [ ] UniversalAppAgent 能完成 "打开 App → 搜索用户 → 发消息" 全流程
- [ ] Instagram 搜索/关注/DM 功能可用
- [ ] Facebook 搜索/添加好友/消息 功能可用
- [ ] 三级降级策略生效: 专用模块 → GenericPlugin → UniversalAgent
- [ ] 弹窗自动处理成功率 > 90%

### 里程碑 3: v0.8.0 — 业务闭环（第 10 周末）

- [ ] 获客漏斗完整: 发现 → 预热 → 触达 → 筛选 → 转化
- [ ] 养号策略运行 7 天无封号
- [ ] 自动回复 + 意图分类工作正常
- [ ] 跨平台迁移 (LI→TG) 工作流可执行
- [ ] 每台设备日处理 > 50 个 Lead

### 里程碑 4: v1.0.0 — 规模化生产（第 14 周末）

- [ ] 5 台设备并行运行 48 小时无故障
- [ ] 智能调度根据时区自动安排
- [ ] 自适应风控: 检测到限流自动降频
- [ ] Dashboard 展示完整获客数据
- [ ] VLM 月成本 < $30（5 台设备）
- [ ] 文档完整，可交付

---

## 附录 A: 环境变量清单

```bash
# 必须
GEMINI_API_KEY=xxx            # Google Gemini API
DEEPSEEK_API_KEY=xxx          # DeepSeek (已有)

# 推荐
DASHSCOPE_API_KEY=xxx         # 阿里云 Qwen VL
OPENAI_API_KEY=xxx            # OpenAI GPT-4.1

# 可选
OPENCLAW_API_KEY=xxx          # Host API 鉴权 (已有)
```

## 附录 B: 技术依赖新增

```txt
# requirements.txt 新增
google-genai>=1.0.0           # Gemini API SDK
openai>=1.50.0                # OpenAI SDK (已有, 确认版本)
dashscope>=1.20.0             # 阿里云 Qwen VL SDK
httpx>=0.27.0                 # Orb Eye HTTP 客户端 (异步)
```

## 附录 C: 参考资料

| 技术 | 链接 |
|------|------|
| UI-TARS 2.0 | https://github.com/bytedance/UI-TARS |
| Orb Eye | https://github.com/KarryViber/orb-eye |
| DroidClaw | https://github.com/unitedbyai/droidclaw |
| Gemini API | https://ai.google.dev/gemini-api/docs |
| Qwen VL | https://help.aliyun.com/zh/model-studio/ |
| AutoGLM 2.0 | https://ai-bot.cn/autoglm |

---

*文档版本: 2.0 | 作者: AI 架构师 | 最后更新: 2026-03-19*
