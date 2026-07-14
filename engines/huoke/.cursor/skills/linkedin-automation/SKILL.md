---
name: linkedin-automation
description: Automate LinkedIn on real Android devices via uiautomator2. Covers profile search, connection requests, messaging, post interactions (like/comment), endorsements, and content publishing. Strict compliance with LinkedIn rate limits. Use when working with LinkedIn automation, outreach, networking, or professional social media tasks.
---

# LinkedIn Automation Skill

## Module Location

`src/app_automation/linkedin.py` — `LinkedInAutomation` class

## Architecture

```
LinkedInAutomation
├── 社交操作
│   ├── search_profile(query, filters)
│   ├── send_connection(profile, note)
│   ├── send_message(profile, message)
│   ├── accept_invitations()
│   └── view_profile(profile_url)
├── 内容互动
│   ├── like_post(post_selector)
│   ├── comment_post(post_selector, text)
│   ├── share_post(post_selector, comment)
│   └── publish_post(content, media)
├── 技能认可
│   └── endorse_skill(profile, skill_name)
├── 账号管理
│   ├── switch_account(account_name)
│   └── get_current_profile()
└── 信息提取
    ├── extract_profile_data(profile_url)
    └── extract_feed_posts(count)
```

## Critical: LinkedIn 合规红线

LinkedIn 是三平台中**风险最高**的。2023-2025 限制量增长 340%，23% 自动化用户在 90 天内被限制。

### 安全阈值 (2026)

```yaml
linkedin:
  daily_connections: 25            # 官方 100-200/周，保守 25/天
  daily_messages: 30               # 只发高度个性化消息
  daily_profile_views: 80
  daily_total_actions: 100         # 所有操作合计
  action_interval_sec: [30, 120]   # 操作间最小等待（关键！）
  typing_speed_cpm: [150, 280]     # 打字速度：字/分钟
  profile_read_sec: [15, 45]       # 模拟阅读资料时间
  post_read_sec: [5, 20]           # 模拟阅读帖子时间
  session_max_min: 45              # 单次会话最长时间
  cooldown_between_sessions_min: [30, 90]  # 会话间冷却
```

### 检测因素 (按风险排序)

1. **重复消息模式 (34%)** → 必须 LLM 改写每条消息
2. **非自然操作节奏 (28%)** → HumanBehavior 引擎模拟
3. **操作量异常 (20%)** → ComplianceGuard 限速
4. **会话模式异常 (18%)** → 模拟工作-休息周期

### 消息个性化流程

```
模板 → LLM 改写(带收件人上下文) → HumanBehavior 延迟 → 逐字输入 → 发送
                                   ↑
                    读取对方资料摘要作为改写上下文
```

## UI 选择器策略

LinkedIn 使用 Server-Driven UI (SDUI)，元素 ID 经常变化:

1. **优先**: `content-desc` (相对稳定)
2. **次选**: 文本匹配 `d(text="Connect")`, `d(textContains="Message")`
3. **Fallback**: XML dump + 相对坐标
4. **最终**: 截图 → Qwen3-VL 视觉识别

### 常用选择器

```python
# 搜索
search_bar = d(resourceId="com.linkedin.android:id/search_bar_text")

# 连接按钮 — SDUI 中经常变化，优先用 text
connect_btn = d(text="Connect") or d(description="Connect")

# 消息输入
message_input = d(resourceId="com.linkedin.android:id/msg_edit_text")

# Feed 帖子 — 需要 XML dump 解析
like_btn = d(descriptionContains="Like")
comment_btn = d(descriptionContains="Comment")
```

## 反检测策略

| 策略 | 实现 |
|------|------|
| 消息唯一化 | LLM 改写，确保 0% 重复率 |
| 自然浏览路径 | 搜索 → 浏览结果 → 翻几页 → 点击目标（非直达） |
| 阅读模拟 | 打开资料后等待 15-45s，期间滚动 |
| 打字模拟 | 逐字输入，高斯分布间隔 |
| 会话周期 | 20-45min 活跃 → 30-90min 冷却 |

## Device Map

| 设备 | Device ID | 状态 |
|------|-----------|------|
| Phone1 | 89NZVGKFD6BYUO5P | LinkedIn 已安装 |
| Phone2 | R8CIFUBIOVCIUW5H | LinkedIn 已安装 (APK from Phone1) |

## API Endpoints (FastAPI)

```
POST /api/linkedin/search           # 搜索用户
POST /api/linkedin/connect          # 发送连接请求
POST /api/linkedin/message          # 发送消息
POST /api/linkedin/accept           # 接受邀请
POST /api/linkedin/like             # 点赞
POST /api/linkedin/comment          # 评论
POST /api/linkedin/publish          # 发布动态
POST /api/linkedin/endorse          # 技能认可
GET  /api/linkedin/profile          # 提取个人资料
GET  /api/linkedin/quota            # 查询剩余配额
```
