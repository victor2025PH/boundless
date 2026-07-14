# TikTok 引流方案 — 开发文档

> **日期**: 2026-03-19
> **设备**: 9 台 Redmi 13C (Android 13)，全部安装 TikTok Trill v43.x
> **包名**: `com.ss.android.ugc.trill`（海外版）
> **分辨率**: 720×1600

---

## 一、方案总览

### 1.1 引流全流程

```
养号 (7天)          获客 (持续)                    转化
┌─────────┐      ┌────────────────────────┐      ┌──────────────┐
│ 浏览Feed │      │ 关键词搜索目标用户       │      │ TikTok私信    │
│ 随机点赞  │  →   │ 浏览竞品粉丝列表        │  →   │      ↓        │
│ 看完视频  │      │ 评论区截流活跃用户       │      │ 引导到        │
│ 搜索内容  │      │ 话题标签找精准受众       │      │ WhatsApp/TG  │
│ 关注大V   │      │         ↓               │      └──────────────┘
└─────────┘      │ 关注 → 互动 → 私信      │
                  └────────────────────────┘
```

### 1.2 9 台设备分工

| 角色 | 设备数 | 任务 | 说明 |
|------|--------|------|------|
| **养号机** | 2 台 | 新号养号、内容浏览、建立标签 | 新账号专用 |
| **获客机** | 5 台 | 搜索→关注→互动→私信 | 主力引流 |
| **内容机** | 1 台 | 发布视频/内容、维护账号形象 | 提升权重 |
| **备用机** | 1 台 | 替补/轮换/测试 | 有设备故障时顶替 |

---

## 二、实测 UI 选择器（基于 2026-03-19 真机 dump）

### 2.1 底部导航

```python
class TT:
    """TikTok (Trill) u2 选择器 — 基于 Redmi 13C v43.8.3 实测"""

    # ── 底部 Tab ──
    TAB_HOME = [
        {"description": "Home"},
        {"resourceId": "com.ss.android.ugc.trill:id/mvd"},
    ]
    TAB_SHOP = [
        {"description": "Shop"},
        {"resourceId": "com.ss.android.ugc.trill:id/e09"},
    ]
    TAB_CREATE = [
        {"description": "Create"},
        {"resourceId": "com.ss.android.ugc.trill:id/mva"},
    ]
    TAB_INBOX = [
        {"description": "Inbox"},
        {"resourceId": "com.ss.android.ugc.trill:id/mve"},
    ]
    TAB_PROFILE = [
        {"description": "Profile"},
        {"resourceId": "com.ss.android.ugc.trill:id/mvf"},
    ]
```

### 2.2 顶部 Feed 导航

```python
    # ── 顶部 Feed Tab ──
    TAB_FOR_YOU = [
        {"description": "For You"},
        {"text": "For You"},
    ]
    TAB_FOLLOWING = [
        {"description": "Following"},
        {"text": "Following"},
    ]
    TAB_EXPLORE = [
        {"description": "Explore"},
        {"text": "Explore"},
    ]
    TAB_FRIENDS = [
        {"description": "Friends"},
        {"text": "Friends"},
    ]

    # ── 搜索 ──
    SEARCH_ICON = [
        {"description": "Search"},
        {"resourceId": "com.ss.android.ugc.trill:id/izy"},
    ]
```

### 2.3 视频互动（右侧面板）

```python
    # ── 视频互动 (右侧) ──
    # 注意: desc 包含动态数字，需用 descContains 匹配

    LIKE_BUTTON = [
        {"descriptionContains": "Like video"},
        {"descriptionContains": "like"},
    ]
    COMMENT_BUTTON = [
        {"descriptionContains": "Read or add comments"},
        {"descriptionContains": "comments"},
    ]
    FAVORITE_BUTTON = [
        {"descriptionContains": "Favorites"},
    ]
    SHARE_BUTTON = [
        {"descriptionContains": "Share video"},
    ]
    FOLLOW_BUTTON = [
        {"descriptionContains": "Follow"},   # desc = "Follow <username>"
        {"resourceId": "com.ss.android.ugc.trill:id/hpm"},
    ]
    CREATOR_AVATAR = [
        {"descriptionContains": "profile"},  # desc = "<name>... profile"
        {"resourceId": "com.ss.android.ugc.trill:id/zkr"},
    ]
```

### 2.4 视频信息（底部左侧）

```python
    # ── 视频信息 ──
    CREATOR_NAME = {"resourceId": "com.ss.android.ugc.trill:id/title"}
    VIDEO_DESC = {"resourceId": "com.ss.android.ugc.trill:id/desc"}
    MUSIC_INFO = {"resourceId": "com.ss.android.ugc.trill:id/nv0"}
    SEE_TRANSLATION = {"text": "See translation"}
```

### 2.5 搜索页

```python
    # ── 搜索页 ──
    SEARCH_INPUT = [
        {"resourceId": "com.ss.android.ugc.trill:id/et_search_kw"},
        {"className": "android.widget.EditText",
         "packageName": "com.ss.android.ugc.trill"},
    ]
    SEARCH_TAB_TOP = {"text": "Top"}
    SEARCH_TAB_USERS = [
        {"text": "Users"},
        {"text": "Accounts"},
        {"text": "People"},
    ]
    SEARCH_TAB_VIDEOS = {"text": "Videos"}
    SEARCH_TAB_SOUNDS = {"text": "Sounds"}
    SEARCH_TAB_HASHTAGS = [
        {"text": "Hashtags"},
        {"text": "Tags"},
    ]
```

### 2.6 私信页

```python
    # ── 私信 ──
    DM_NEW_MESSAGE = [
        {"descriptionContains": "New message"},
        {"descriptionContains": "compose"},
    ]
    DM_SEARCH_INPUT = [
        {"className": "android.widget.EditText",
         "packageName": "com.ss.android.ugc.trill"},
    ]
    DM_MESSAGE_INPUT = [
        {"descriptionContains": "Send a message"},
        {"className": "android.widget.EditText",
         "packageName": "com.ss.android.ugc.trill"},
    ]
    DM_SEND_BUTTON = [
        {"descriptionContains": "Send"},
        {"resourceId": "com.ss.android.ugc.trill:id/cz3"},
    ]
```

### 2.7 个人资料页

```python
    # ── 个人资料 ──
    PROFILE_FOLLOW_BTN = [
        {"text": "Follow"},
        {"descriptionContains": "Follow"},
    ]
    PROFILE_MESSAGE_BTN = [
        {"text": "Message"},
        {"descriptionContains": "Message"},
    ]
    PROFILE_FOLLOWERS_COUNT = [
        {"resourceId": "com.ss.android.ugc.trill:id/followers_count"},
    ]
    PROFILE_FOLLOWING_COUNT = [
        {"resourceId": "com.ss.android.ugc.trill:id/following_count"},
    ]
```

---

## 三、功能模块开发计划

### Phase 1: 基础操作 + 养号（第 1 周）

#### 1.1 重写 TikTokAutomation 选择器

**文件**: `src/app_automation/tiktok.py`

当前代码 (~393行) 的 `smart_tap("Search icon")` 完全依赖 VLM 猜测。
改为用上面实测的 u2 选择器 + smart_tap fallback 的双保险策略：

```
优先: u2 选择器 (实测数据，最快最准)
  ↓ 失败
备选: smart_tap + VLM (AI 识别，适应 UI 变化)
  ↓ 失败
兜底: 坐标点击 (按比例计算，最不可靠)
```

**具体任务**:

| 任务 | 说明 | 预计 |
|------|------|------|
| 1.1.1 新增 TT 选择器类 | 基于实测数据，定义所有 UI 元素 | 0.5 天 |
| 1.1.2 修正包名优先级 | `trill` → `musically` → `musically.go` | 0.5 小时 |
| 1.1.3 重写 `_go_for_you` | 用 TT.TAB_FOR_YOU 选择器 | 0.5 小时 |
| 1.1.4 重写 `_like_video` | 用 TT.LIKE_BUTTON 选择器替代双击 | 0.5 小时 |
| 1.1.5 重写 `_follow_creator` | 用 TT.FOLLOW_BUTTON 选择器 | 0.5 小时 |
| 1.1.6 重写 `_comment_video` | 用 TT.COMMENT_BUTTON 选择器 | 0.5 小时 |
| 1.1.7 重写 `search_users` | 用实测搜索页选择器 | 1 天 |
| 1.1.8 重写 `send_dm` | 用实测私信页选择器 | 1 天 |

#### 1.2 养号模块 (新增)

**文件**: `src/app_automation/tiktok.py` 新增方法

**养号 SOP（基于行业最佳实践）**:

```
第 1 天: 环境初始化
  ├─ 完善个人资料 (头像/昵称/简介)
  ├─ 浏览 For You 30分钟 (完播 > 70%)
  ├─ 点赞 5-10 个视频 (相关领域)
  ├─ 不关注任何人
  └─ 不发私信

第 2 天: 建立兴趣标签
  ├─ 搜索行业关键词 3-5 个
  ├─ 浏览搜索结果视频 20分钟
  ├─ 关注 3-5 个同领域大V
  ├─ 点赞 10-15 个视频
  └─ 看完 > 70% 的视频

第 3 天: 轻度互动
  ├─ 继续浏览 30分钟
  ├─ 评论 2-3 条 (有价值的内容)
  ├─ 关注 5-8 人
  ├─ 点赞 15-20 个视频
  └─ 检查是否被推送垂直内容

第 4-5 天: 增加互动强度
  ├─ 浏览 45分钟 (分 2-3 次)
  ├─ 评论 3-5 条
  ├─ 关注 8-12 人
  ├─ 收藏 3-5 个视频
  ├─ 分享 1-2 个视频
  └─ 可以发第一条视频

第 6-7 天: 达到正常水平
  ├─ 浏览 30-60分钟
  ├─ 可以开始发私信 (5-10 条/天)
  ├─ 关注 10-15 人
  ├─ 评论 5-8 条
  └─ 发 1-2 条视频
```

**代码方法**:

```python
def warmup_session(self, day: int, keywords: list,
                   device_id: str = None) -> dict:
    """
    执行一次养号会话。根据 day (1-7) 自动调整行为强度。
    keywords: 行业关键词列表，用于建立兴趣标签。
    """
```

| 参数 | Day 1 | Day 2 | Day 3 | Day 4-5 | Day 6-7 |
|------|-------|-------|-------|---------|---------|
| browse_minutes | 30 | 20 | 30 | 45 | 40 |
| max_likes | 8 | 12 | 18 | 25 | 30 |
| max_comments | 0 | 0 | 3 | 5 | 8 |
| max_follows | 0 | 5 | 8 | 12 | 15 |
| max_favorites | 0 | 0 | 2 | 5 | 5 |
| max_shares | 0 | 0 | 0 | 2 | 2 |
| search_keywords | 0 | 3 | 3 | 5 | 5 |
| allow_dm | ✗ | ✗ | ✗ | ✗ | ✓ |

### Phase 2: 获客引流核心（第 2-3 周）

#### 2.1 竞品粉丝采集

**思路**: 找到同行业的 TikTok 账号 → 进入其粉丝列表 → 逐个采集用户信息

```python
def scrape_followers(self, target_username: str,
                     max_count: int = 50,
                     device_id: str = None) -> List[dict]:
    """
    采集目标账号的粉丝列表。

    流程:
    1. 搜索 target_username → 进入其个人页
    2. 点击 "Followers" 数字
    3. 滚动粉丝列表，逐个提取:
       - 用户名、昵称、头像描述
       - 是否已关注
    4. 存入 LeadStore
    """
```

#### 2.2 评论区截流

**思路**: 找到热门视频 → 浏览评论 → 采集活跃评论者

```python
def scrape_commenters(self, keyword: str,
                      video_count: int = 5,
                      max_per_video: int = 20,
                      device_id: str = None) -> List[dict]:
    """
    从热门视频评论区采集活跃用户。

    流程:
    1. 搜索关键词 → 选择 Videos tab
    2. 依次打开前 N 个视频的评论区
    3. 滚动评论列表，提取评论者:
       - 用户名、评论内容、点赞数
    4. 过滤: 排除蓝V/大号(粉丝>10万)、保留活跃小号
    5. 存入 LeadStore
    """
```

#### 2.3 关注 + 互动引流

**思路**: 先关注 → 用户收到通知 → 回关率 10-20% → 对回关用户私信

```python
def follow_and_engage(self, lead_ids: List[int],
                      comment_probability: float = 0.3,
                      like_probability: float = 0.8,
                      device_id: str = None) -> dict:
    """
    对 Lead 列表执行关注 + 互动。

    流程 (每个 Lead):
    1. 搜索用户 → 进入个人页
    2. 浏览最新 1-3 个视频 (真实观看)
    3. 点赞最新视频 (80% 概率)
    4. 评论一条 (30% 概率, AI 生成)
    5. 点击 Follow
    6. 回到搜索页, 下一个

    间隔: HumanBehavior 控制，每人 30-90 秒
    限额: ComplianceGuard (每小时 8 关注, 每天 30)
    """
```

#### 2.4 私信触达

**思路**: 对已互关/已互动的用户发送私信

```python
def mass_dm(self, lead_ids: List[int],
            message_template: str,
            device_id: str = None) -> dict:
    """
    批量发送私信。

    流程:
    1. 从 LeadStore 获取 Lead 信息
    2. MessageRewriter 生成个性化消息
    3. 进入 Inbox → 新消息 → 搜索用户 → 发送
    4. 记录发送状态到 LeadStore

    限额: 每小时 5 条, 每天 15 条 (compliance)
    话术原则:
    - 不发链接 (TikTok会屏蔽)
    - 不发营销词 (免费/赚钱/优惠)
    - 用关联性开头: "看了你的视频..."
    - 弱行动号召: "方便的话可以加我WA聊..."
    """
```

**私信话术模板**:

```yaml
# config/tiktok_dm_templates.yaml

templates:
  # 通用引流到 WhatsApp
  general_to_wa:
    - "Hi {name}! Love your content about {topic}. I'm also in this space and would love to connect. My WA is in my bio if you want to chat more 😊"
    - "Hey {name}, your {topic} videos are really inspiring. I've been working on something similar, would love to share ideas. Check my profile for contact!"
    - "{name} your content is amazing! I have some ideas that might interest you. DM here gets buried so check my bio for better ways to reach me 🙌"

  # 针对特定行业
  ecommerce:
    - "Hi {name}! I noticed your product posts. I work with suppliers in this space and might have some useful connections. My contact is in my bio!"

  # 回复已互动用户 (回关后)
  follow_back:
    - "Thanks for the follow back {name}! I really enjoy your content. Would love to collaborate sometime - my contact info is on my profile 🤝"
```

### Phase 3: 跨平台转化（第 3-4 周）

#### 3.1 TikTok → WhatsApp/Telegram 转化流程

```
TikTok 上的操作:
  1. 个人资料 Bio 写: "💬 WA: +63xxx 或 TG: @xxx"
  2. 私信引导: "我这边消息太多看不过来，加我WA方便聊"
  3. 评论引导: "详细的发我WA了，号在主页"

WhatsApp/Telegram 上的操作 (EventBus 触发):
  1. 用户加了 WA/TG → 自动问候
  2. 用 AutoReply + IntentClassifier 管理对话
  3. 根据 Lead Score 决定跟进深度
```

**跨平台事件联动** (利用现有 EventBus):

```yaml
# config/workflows/tiktok_to_wa.yaml
escalation_rules:
  - trigger_event: "tiktok.follow_received"
    from_stage: "discovered"
    to_stage: "warmed_up"
    delay_min: 1800      # 30分钟后
    delay_max: 7200      # 最多2小时后
    actions:
      - action: tiktok.send_dm
        params:
          message: ""    # AI 生成

  - trigger_event: "tiktok.dm_reply_received"
    from_stage: "contacted"
    to_stage: "qualified"
    actions:
      - action: util.log
        params:
          message: "Lead replied on TikTok! Check for WA conversion"

  - trigger_event: "whatsapp.message_received"
    from_stage: "qualified"
    to_stage: "converting"
    actions:
      - action: whatsapp.send_message
        params:
          message: ""    # AI 生成欢迎消息
```

#### 3.2 个人资料优化

```python
def optimize_profile(self, bio: str, device_id: str = None) -> bool:
    """
    优化 TikTok 个人资料以最大化引流效果。

    Bio 应包含:
    - 行业关键词 (被搜索到)
    - 价值主张 (一句话说清楚做什么)
    - 联系方式 (WA号/TG用户名, 不要用链接)
    - emoji 增加可读性
    """
```

---

## 四、反检测策略

### 4.1 行为模拟

基于 TikTok 2026 年检测机制（50+ 设备属性 + 行为分析），必须做到:

| 维度 | 检测点 | 应对策略 |
|------|--------|---------|
| **观看时长** | 视频完播率 < 30% 异常 | 70%+ 完播率，正态分布的观看时长 |
| **滑动速度** | 匀速滑动 = 机器人 | Bezier 曲线滑动 + 惯性模拟 (HumanBehavior) |
| **点击位置** | 固定坐标 = 机器人 | 高斯分布偏移 ±5-20px |
| **操作间隔** | 均匀间隔 = 机器人 | 泊松分布 + 偶尔长停顿 (HumanBehavior) |
| **会话模式** | 24h 不停 = 异常 | SessionProfile: 活跃 20-40min → 休息 10-30min |
| **关注模式** | 连续关注 = 异常 | 关注穿插浏览、点赞、评论 |
| **私信模式** | 相同内容 = 异常 | MessageRewriter 每条不同 + 表情随机 |

### 4.2 合规限流 (已有 ComplianceGuard 的基础上细化)

```yaml
# config/compliance.yaml — TikTok 部分

tiktok:
  actions:
    browse_feed:     { hourly: 30, daily: 200, cooldown_sec: 0 }
    like:            { hourly: 15, daily: 80,  cooldown_sec: 3 }
    comment:         { hourly: 6,  daily: 30,  cooldown_sec: 30 }
    follow:          { hourly: 8,  daily: 50,  cooldown_sec: 15 }
    unfollow:        { hourly: 10, daily: 60,  cooldown_sec: 10 }
    send_dm:         { hourly: 5,  daily: 20,  cooldown_sec: 60 }
    search:          { hourly: 10, daily: 50,  cooldown_sec: 5 }
    favorite:        { hourly: 10, daily: 50,  cooldown_sec: 5 }
    share:           { hourly: 5,  daily: 20,  cooldown_sec: 15 }
    view_profile:    { hourly: 15, daily: 80,  cooldown_sec: 5 }
    scrape_follower: { hourly: 20, daily: 100, cooldown_sec: 3 }
  daily_total: 350
  hourly_total: 50

  # 养号期更严格的限制 (day 1-7)
  warmup_overrides:
    day_1: { daily_total: 50,  follow: {daily: 0},  send_dm: {daily: 0} }
    day_2: { daily_total: 80,  follow: {daily: 5},  send_dm: {daily: 0} }
    day_3: { daily_total: 120, follow: {daily: 8},  send_dm: {daily: 0} }
    day_4: { daily_total: 150, follow: {daily: 12}, send_dm: {daily: 0} }
    day_5: { daily_total: 200, follow: {daily: 15}, send_dm: {daily: 5} }
    day_6: { daily_total: 250, follow: {daily: 20}, send_dm: {daily: 10} }
    day_7: { daily_total: 300, follow: {daily: 30}, send_dm: {daily: 15} }
```

### 4.3 设备指纹隔离

| 策略 | 实现方式 |
|------|---------|
| IP 隔离 | 每台手机配独立 SIM 卡（已有 DITO SIM） |
| 账号隔离 | 每台手机一个 TikTok 账号，不交叉登录 |
| 行为差异化 | 每台设备用不同的 BehaviorProfile 参数 |
| 操作时段差异 | SmartSchedule 给每台设备错开 ±30 分钟 |

---

## 五、开发排期

### 第 1 周: 基础重写 + 养号

| 天 | 任务 | 文件 |
|----|------|------|
| Day 1 | 新增 TT 选择器类 + 修正包名 + 更新 devices.yaml | `tiktok.py`, `devices.yaml` |
| Day 2 | 重写 browse_feed / like / comment / follow | `tiktok.py` |
| Day 3 | 重写 search_users / send_dm (用实测选择器) | `tiktok.py` |
| Day 4 | 实现 warmup_session 养号模块 | `tiktok.py` |
| Day 5 | compliance.yaml 添加 TikTok 限流 + 测试 | `compliance.yaml`, `tests/` |

**验收**: 在 1 台设备上跑通: 启动 → 浏览 10 个视频 → 点赞 3 个 → 关注 1 人 → 搜索用户

### 第 2 周: 获客核心

| 天 | 任务 | 文件 |
|----|------|------|
| Day 1-2 | 实现 scrape_followers (竞品粉丝采集) | `tiktok.py` |
| Day 2-3 | 实现 scrape_commenters (评论区截流) | `tiktok.py` |
| Day 3-4 | 实现 follow_and_engage (关注互动) | `tiktok.py` |
| Day 4-5 | 实现 mass_dm (批量私信) + 话术模板 | `tiktok.py`, YAML |

**验收**: 在 3 台设备上并行: 搜索关键词 → 采集 50 个 Lead → 关注 20 人 → 发 10 条私信

### 第 3 周: 跨平台 + 工作流

| 天 | 任务 | 文件 |
|----|------|------|
| Day 1 | TikTok Action 注册到 ActionRegistry | `acquisition.py` |
| Day 2 | 编写 tiktok_lead_gen.yaml 工作流 | `config/workflows/` |
| Day 3 | TikTok → WA/TG 跨平台事件联动 | `event_bus`, workflow |
| Day 4 | 9 台设备全量部署 + 分角色配置 | `devices.yaml`, scripts |
| Day 5 | 全流程 E2E 测试 + 调参 | tests |

**验收**: 9 台设备 24 小时运行: 2 台养号 + 5 台获客 + 1 台内容 + 1 台备用

### 第 4 周: 优化 + 规模化

| 天 | 任务 |
|----|------|
| Day 1-2 | 反检测调优: 观看时长/操作间隔/会话模式 |
| Day 3 | 数据分析: 关注→回关率、私信→回复率、WA 转化率 |
| Day 4 | Host API 添加 TikTok Dashboard 端点 |
| Day 5 | 根据数据调整策略参数 + 文档 |

---

## 六、可量化目标

### 单台设备日产出（成熟期，养号完成后）

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 浏览视频 | 100-200 个 | 养号 + 算法标签维护 |
| 点赞 | 50-80 个 | 建立互动权重 |
| 评论 | 10-20 条 | AI 生成有价值评论 |
| 关注 | 30-50 人 | 主要获客手段 |
| 采集 Lead | 50-100 个 | 从粉丝列表/评论区 |
| 发私信 | 15-20 条 | 对已互动用户 |

### 9 台设备月度目标

| 指标 | 目标值 | 计算方式 |
|------|--------|---------|
| 新增 Lead | 7,000-15,000 | 5台×50-100×30天 |
| 发起关注 | 4,500-7,500 | 5台×30-50×30天 |
| 获得回关 | 450-1,500 | 10-20% 回关率 |
| 发送私信 | 2,250-3,000 | 5台×15-20×30天 |
| 私信回复 | 225-600 | 10-20% 回复率 |
| WA/TG 转化 | 50-150 | 回复中 20-25% 加 WA |
| 成交线索 | 10-30 | 加 WA 中 20% |

### 关键转化漏斗

```
采集 Lead:      10,000
     ↓ (关注)
获得回关:         1,000  (10%)
     ↓ (私信)
私信送达:           800  (80% 送达率)
     ↓
获得回复:           120  (15%)
     ↓ (引导)
加 WhatsApp/TG:      30  (25%)
     ↓ (转化)
有效线索:            10  (33%)
```

---

## 七、文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/app_automation/tiktok.py` | **大改** | 重写选择器、新增养号/采集/引流方法 |
| `config/apps/tiktok.yaml` | 更新 | 修正包名为 trill，更新 compliance |
| `config/compliance.yaml` | 新增 | 添加 tiktok 部分 + warmup_overrides |
| `config/devices.yaml` | 更新 | 添加全部 9 台设备序列号 + 角色分配 |
| `config/workflows/tiktok_lead_gen.yaml` | **新增** | TikTok 获客工作流 |
| `config/workflows/tiktok_to_wa.yaml` | **新增** | 跨平台转化工作流 |
| `config/tiktok_dm_templates.yaml` | **新增** | 私信话术模板 |
| `src/workflow/acquisition.py` | 小改 | 注册 TikTok actions |
| `src/host/api.py` | 小改 | 添加 TikTok 相关 API 端点 |
| `src/host/schemas.py` | 小改 | 添加 TikTok TaskType |
| `tests/test_tiktok.py` | **新增** | TikTok 模块测试 |

---

*文档版本: 1.0 | 日期: 2026-03-19 | 基于 9 台 Redmi 13C 真机实测数据*
