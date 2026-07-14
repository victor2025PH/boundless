# P4 开发计划 — 三平台统一自动化 + AI Agent

## 一、技术调研结论

### 1.1 最新技术趋势（2025-2026）

| 技术方向 | 最新方案 | 我们的融合策略 |
|----------|---------|---------------|
| **AI 手机 Agent** | ClawMobile — 分层架构：LLM 规划层 + 确定性执行层 | 采用同样的分层设计，LLM 负责决策，u2/ADB 负责执行 |
| **UI 理解** | OmniParser V2 (Microsoft) + Qwen3-VL | 截图 → 多模态 LLM 识别 UI → 生成操作指令，作为 XML dump 的补充 |
| **人类行为模拟** | 贝塞尔曲线滑动、高斯分布打字节奏、阅读时间模拟 | 构建 `HumanBehavior` 引擎替代简单的 random.sleep |
| **反检测** | 行为指纹 > 操作量；LinkedIn 34% 检测来自重复消息模式 | 消息模板 + LLM 改写，确保每条消息唯一 |
| **跨平台编排** | 工作流引擎 + 事件驱动 | 构建轻量工作流引擎，支持跨 App 串联任务 |

### 1.2 各平台合规红线

| 平台 | 安全阈值 | 检测重点 | 我们的策略 |
|------|---------|---------|-----------|
| **Telegram** | 无官方限制，但大量操作会触发 flood wait | 短时间大量消息、频繁搜索 | 消息间隔 3-8s 随机，每小时 < 30 条 |
| **LinkedIn** | 20-30 连接/天，30-50 消息/天，100 总操作/天 | 打字节奏(28%)、重复消息(34%)、滑动速度 | LLM 改写每条消息，模拟阅读+思考时间 |
| **WhatsApp** | 无官方公开限制，但批量操作会被封号 | 短时间大量消息、非联系人群发 | 仅已有联系人，每小时 < 20 条 |

---

## 二、统一架构设计

### 2.1 分层架构（借鉴 ClawMobile）

```
┌─────────────────────────────────────────────┐
│               AI Brain Layer                 │
│  LLM 决策引擎（DeepSeek / Qwen3-VL）         │
│  - 理解用户意图                                │
│  - 生成工作流                                  │
│  - 消息改写 / 自动回复                          │
│  - 截图理解（视觉 fallback）                    │
├─────────────────────────────────────────────┤
│            Orchestration Layer               │
│  工作流引擎 + 账号调度器 + 合规限速器            │
│  - 跨平台任务编排                              │
│  - 多账号轮换                                  │
│  - 速率限制 & 配额管理                          │
│  - 人类行为模拟引擎                             │
├─────────────────────────────────────────────┤
│           Platform Skill Layer              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │ Telegram │ │ LinkedIn │ │ WhatsApp │     │
│  │  Skill   │ │  Skill   │ │  Skill   │     │
│  └──────────┘ └──────────┘ └──────────┘     │
│  每个 Skill 独立封装：                         │
│  - UI 选择器（XML + 视觉双通道）               │
│  - 平台特定操作（发消息/搜索/加好友...）         │
│  - 平台特定合规规则                             │
├─────────────────────────────────────────────┤
│            Device Control Layer             │
│  DeviceManager + u2 + ADB                   │
│  WorkerPool + HealthMonitor                 │
│  截图 / XML dump / 坐标点击                   │
└─────────────────────────────────────────────┘
│               ↕ USB / WiFi                  │
┌─────────────────────────────────────────────┐
│  Phone 1 (89NZVGKF)  │  Phone 2 (R8CIFUBI) │
│  3 TG accounts        │  1 TG account       │
│  1 LinkedIn account   │  1 LinkedIn account  │
│  WhatsApp             │  WhatsApp            │
└─────────────────────────────────────────────┘
```

### 2.2 核心新增组件

| 组件 | 作用 | 技术选型 |
|------|------|---------|
| **HumanBehavior 引擎** | 模拟人类操作节奏 | 贝塞尔曲线滑动、高斯打字、泊松等待 |
| **MessageRewriter** | 确保每条消息唯一 | LLM API (DeepSeek) 改写模板 |
| **WorkflowEngine** | 跨平台任务编排 | JSON 工作流定义 + DAG 执行 |
| **AccountRouter** | 多账号智能轮换 | 基于配额、冷却期、平台规则 |
| **VisionFallback** | XML dump 失败时用截图理解 UI | Qwen3-VL / GPT-4o 识别 |
| **ComplianceGuard** | 速率限制 + 配额跟踪 | 滑动窗口计数器 + SQLite 持久化 |

---

## 三、三平台 Skill 设计

### 3.1 Telegram Skill

**当前能力**: 发消息、读消息、发文件、搜索用户、多账号切换、跨设备互聊 ✅

**需要新增**:
- `telegram_switch_account` — API 化账号切换
- `telegram_list_accounts` — 列出设备上的所有账号
- `telegram_auto_reply` — AI 自动回复
- `telegram_monitor` — 消息监听 + Webhook
- `telegram_forward` — 消息转发
- `telegram_join_group` — 加入群组/频道

### 3.2 LinkedIn Skill

**当前能力**: 搜索用户、发连接请求、发消息、发动态、接受邀请 ✅

**需要新增**:
- `linkedin_switch_account` — 账号切换（Phone2 已安装 LinkedIn）
- `linkedin_like_post` — 点赞动态
- `linkedin_comment` — 评论动态
- `linkedin_endorse` — 技能认可
- `linkedin_view_profile_stealth` — 低风险浏览资料（模拟自然浏览路径）
- LinkedIn 合规限速器（最关键：23% 用户 90 天内被限制）

### 3.3 WhatsApp Skill

**当前能力**: 基础发消息、读消息模块存在但未真机测试

**需要新增**:
- 真机 UI dump + 选择器校准
- `whatsapp_switch_account`（如果有多账号）
- `whatsapp_send_media` — 发送图片/视频
- `whatsapp_read_group` — 群消息读取
- `whatsapp_auto_reply` — 自动回复
- `whatsapp_status` — 发朋友圈/状态

---

## 四、人类行为模拟引擎（HumanBehavior）

这是**所有平台共享**的核心组件，直接决定是否被检测。

### 4.1 模拟维度

| 维度 | 简单方案（当前） | 高级方案（目标） |
|------|----------------|----------------|
| **点击** | `d.click(x, y)` | 贝塞尔曲线移动 → 轻微偏移 → 点击 |
| **打字** | `set_text(msg)` 一次性 | 逐字输入，间隔 50-200ms 高斯分布 |
| **等待** | `random.uniform(0.5, 2)` | 泊松分布 + 上下文感知（长消息多等） |
| **滑动** | 固定速度 | 加速→匀速→减速物理模型 |
| **阅读** | 无 | 根据文本长度模拟阅读时间（200-300 字/分） |
| **会话节奏** | 连续操作 | 模拟"工作-休息"周期（20-40min 活跃 → 5-15min 暂停） |

### 4.2 消息唯一化

LinkedIn 34% 的检测来自重复消息。解决方案：

```
模板: "Hi {name}, I noticed your work in {field}. Would love to connect!"
  ↓ LLM 改写（5 个变体）
变体1: "Hey {name}, your {field} background caught my eye. Let's connect!"
变体2: "{name}, interesting profile! Your {field} experience resonates with me."
变体3: "Hi there {name} — fellow {field} professional here. Happy to connect."
...
```

---

## 五、工作流引擎设计

### 5.1 工作流 JSON 格式

```json
{
  "name": "cross_platform_outreach",
  "steps": [
    {
      "id": "step1",
      "platform": "linkedin",
      "action": "search_profile",
      "device": "auto",
      "params": {"query": "software engineer Manila"},
      "output": "profiles"
    },
    {
      "id": "step2",
      "platform": "linkedin",
      "action": "send_connection",
      "for_each": "$profiles[:5]",
      "params": {"note_template": "Hi {name}, ..."},
      "delay": {"min": 30, "max": 120}
    },
    {
      "id": "step3",
      "platform": "telegram",
      "action": "switch_account",
      "device": "89NZVGKFD6BYUO5P",
      "params": {"account": "Vivian"}
    },
    {
      "id": "step4",
      "platform": "telegram",
      "action": "send_message",
      "params": {"username": "@vyanks", "message": "LinkedIn outreach done for today"}
    }
  ],
  "on_failure": "skip_and_continue",
  "compliance": {
    "linkedin_daily_connections": 25,
    "linkedin_daily_messages": 30,
    "telegram_hourly_messages": 25
  }
}
```

### 5.2 账号调度策略

```
AccountRouter 逻辑:
1. 检查目标平台所有可用账号
2. 过滤掉已达当日配额的账号
3. 过滤掉在冷却期的账号（上次操作 < N 分钟）
4. 从剩余账号中按"最少使用"原则选择
5. 选择持有该账号的设备
6. 如果需要切换账号，先执行 switch_account
7. 执行任务
8. 更新配额计数器
```

---

## 六、AI 集成策略

### 6.1 两种 LLM 使用模式

| 模式 | 场景 | 推荐模型 | 调用频率 |
|------|------|---------|---------|
| **文本智能** | 消息改写、自动回复、模板生成 | DeepSeek V3 / Qwen3 | 每条消息 1 次 |
| **视觉智能** | XML dump 失败时截图理解 UI | Qwen3-VL / GPT-4o | 低频 fallback |

### 6.2 自动回复流程

```
新消息到达（监听检测）
  → 提取发送者 + 消息内容
  → 查询人设配置（当前账号的角色/语气）
  → 构建 prompt: 系统提示 + 历史对话 + 新消息
  → 调用 LLM 生成回复
  → HumanBehavior 延迟（模拟阅读+思考）
  → 发送回复
  → 记录到对话历史
```

### 6.3 视觉 Fallback（OmniParser V2 思路）

```
正常流程: XML dump → 解析元素 → 坐标点击（快速、确定性）

当 XML dump 失败或元素找不到:
  → 截图
  → 发送到 Qwen3-VL: "这个界面上，{目标操作} 应该点击哪里？"
  → 返回坐标
  → 点击
```

---

## 七、实施路线

### Phase 1 — 基础统一（1-2 天）
1. **HumanBehavior 引擎** — 贝塞尔滑动、高斯打字、泊松等待
2. **ComplianceGuard** — 滑动窗口限速器 + 配额持久化
3. **账号切换 API 化** — Telegram/LinkedIn 的 switch/list 端点
4. 创建三个平台 SKILL.md

### Phase 2 — 平台补齐（2-3 天）
5. **WhatsApp 真机校准** — dump UI + 更新选择器 + 测试
6. **LinkedIn 增强** — 点赞/评论/技能认可 + 合规限速
7. **Telegram 增强** — 消息监听 + 转发 + 群组操作

### Phase 3 — AI 集成（2-3 天）
8. **MessageRewriter** — LLM 消息改写（消除重复模式）
9. **AutoReply** — AI 自动回复模块
10. **VisionFallback** — 截图理解作为 XML 的后备

### Phase 4 — 编排引擎（1-2 天）
11. **WorkflowEngine** — JSON 工作流解析 + DAG 执行
12. **AccountRouter** — 多账号智能轮换
13. **跨平台测试** — TG+LinkedIn+WhatsApp 联动场景

### Phase 5 — 管理面板（2-3 天）
14. **Web Dashboard** — 设备/任务/账号/配额可视化
15. **实时日志** — WebSocket 推送
16. **工作流编辑器** — 可视化创建工作流

---

## 八、文件结构规划

```
mobile-auto-project/
├── .cursor/skills/                    # 新增: Cursor 技能文件
│   ├── telegram-automation/SKILL.md
│   ├── linkedin-automation/SKILL.md
│   ├── whatsapp-automation/SKILL.md
│   ├── human-behavior/SKILL.md
│   └── workflow-engine/SKILL.md
├── src/
│   ├── app_automation/
│   │   ├── telegram.py               # 已有，增强
│   │   ├── linkedin.py               # 已有，增强
│   │   ├── whatsapp.py               # 已有，需真机校准
│   │   └── base_automation.py        # 新增: 三平台共享基类
│   ├── ai/                            # 新增: AI 模块
│   │   ├── llm_client.py             # DeepSeek/Qwen API 客户端
│   │   ├── message_rewriter.py       # 消息改写
│   │   ├── auto_reply.py             # 自动回复
│   │   └── vision_fallback.py        # 截图理解
│   ├── behavior/                      # 新增: 人类行为模拟
│   │   ├── human_behavior.py         # 贝塞尔曲线/高斯打字/泊松等待
│   │   ├── compliance_guard.py       # 限速器 + 配额
│   │   └── account_router.py         # 账号轮换调度
│   ├── workflow/                      # 新增: 工作流引擎
│   │   ├── engine.py                 # 工作流执行器
│   │   ├── parser.py                 # JSON 工作流解析
│   │   └── models.py                 # 工作流数据模型
│   ├── host/                          # 已有
│   ├── device_control/                # 已有
│   └── utils/                         # 已有
├── config/
│   ├── devices.yaml                   # 已有
│   ├── compliance.yaml                # 新增: 各平台限速配置
│   ├── personas.yaml                  # 新增: 账号人设配置（AI用）
│   └── workflows/                     # 新增: 预定义工作流
│       ├── linkedin_outreach.json
│       ├── telegram_broadcast.json
│       └── cross_platform.json
└── tests/                             # 已有，扩展
```

---

## 九、关键技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| LLM 文本模型 | DeepSeek V3 API | 性价比最高，中文优秀，API 便宜 |
| LLM 视觉模型 | Qwen3-VL (Apache 2.0) | 开源可本地部署，UI 理解排名第一 |
| 工作流格式 | JSON | 简单直观，易于 Web 编辑器集成 |
| 配额存储 | SQLite (复用现有) | 无需新依赖，已有基础设施 |
| 行为模拟 | 纯 Python 实现 | 不引入额外依赖，贝塞尔/高斯/泊松都是数学公式 |
| 消息监听 | 轮询 (30s 间隔) | 比推送更可靠，u2 不支持事件订阅 |

---

## 十、风险与应对

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| LinkedIn 账号被限制 | 高(23%) | 中 | 严格遵守限速，每操作间歇 30-120s |
| UI 改版导致选择器失效 | 中 | 高 | 视觉 Fallback + 选择器版本管理 |
| u2 连接不稳定 | 中 | 中 | HealthMonitor 自动重连（已有） |
| LLM API 不稳定 | 低 | 中 | 本地 Qwen3-VL 作为后备 |
| 设备断连 | 低 | 低 | WiFi ADB 作为 USB 后备 |
