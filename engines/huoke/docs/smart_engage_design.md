# 智能社群互动引擎 — 技术设计文档

> **版本**: v1.0 | **日期**: 2026-05-12 | **状态**: 设计评审

---

## 一、目标与定位

### 1.1 业务目标

将当前的"搜群 → 提取成员 → 批量加好友"粗暴模式，升级为模拟真人社交行为的
**"浏览帖子 → 分析用户 → 互动评论 → 深度确认 → 加好友"** 精细化流程。

### 1.2 核心指标

| 指标 | 当前值 | 目标值 |
|------|--------|--------|
| 好友请求通过率 | ~10-15% | 30-50% |
| 单次会话精准触达人数 | 0-3 | 5-8 |
| 被举报/限制风险 | 中 | 低 |
| 每条评论个性化程度 | 0（模板） | 80%+ |

---

## 二、系统架构

### 2.1 模块总览

```
┌───────────────────────────────────────────────────────────────┐
│                    配置层 (YAML + API)                         │
│  smart_engage.yaml    fb_target_personas.yaml                 │
│  chat_messages.yaml   facebook_playbook.yaml                  │
└────────────────────────┬──────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────┐
│               编排层 (executor.py)                             │
│  _run_facebook_campaign                                       │
│    新 step: "smart_engage"                                    │
│    调用 EngagePipeline.run(device_id, group_name, params)     │
└────────────────────────┬──────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────┐
│          互动流水线 (fb_engage_pipeline.py) [新建]              │
│                                                               │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐           │
│  │ PostScan │→│PostAnalyzer  │→│EngageDecider  │           │
│  │ 帖子发现  │  │帖子+作者分析  │  │互动决策引擎    │           │
│  └──────────┘  └──────────────┘  └───────┬───────┘           │
│                                          │                    │
│  ┌──────────────┐  ┌──────────────┐  ┌───▼───────────┐       │
│  │ReplyGenerator│  │ProfileProbe  │  │ActionExecutor │       │
│  │个性化回复生成  │  │资料页深度分析  │  │点赞/评论/加友  │       │
│  └──────────────┘  └──────────────┘  └───────────────┘       │
└───────────────────────────────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────┐
│              UI 自动化层 (facebook.py)                         │
│  已有: comment_on_post, add_friend_with_note,                 │
│        inspect_user_profile_posts, group_engage_session,      │
│        enter_group, browse_feed, _is_likely_fb_profile_page   │
│  新增: tap_post_author_avatar, extract_visible_post_content,  │
│        navigate_back_to_group_feed                            │
└───────────────────────────────────────────────────────────────┘
                         │
┌────────────────────────▼──────────────────────────────────────┐
│              数据层 (SQLite)                                   │
│  已有: facebook_friend_requests, facebook_groups,              │
│        crm_interactions                                       │
│  新增: fb_post_engagements (帖子互动记录)                      │
│        device_group_claims (跨设备群组去重, 已创建)             │
└───────────────────────────────────────────────────────────────┘
```

### 2.2 数据流图

```
[群组 Feed]
    │
    ▼
PostScan: 滚屏 → dump hierarchy → 提取帖子节点
    │
    ├─ 帖子正文 (text 20-800 chars)
    ├─ 作者名 (desc: "More options for X's post")
    ├─ 作者头像 (content-desc: "X Profile picture")
    ├─ 互动数据 (likes/comments count, 如可见)
    │
    ▼
PostAnalyzer: 双层过滤
    │
    ├─ L1: 作者名评分 (_name_signal) → 排除男性/非目标
    ├─ 内容关键词: positive/negative 词表匹配 → 话题分类
    ├─ (可选 L2) LLM: 帖子内容 → 是否适合互动 + 话题标签
    │
    ▼
EngageDecider: 决策矩阵
    │
    ├─ A 类 (L1≥40 + 内容匹配): 点赞 → 评论 → 等待 → 进 profile → 加友
    ├─ B 类 (L1≥20 + 内容中性): 点赞 → 进 profile → 看简介确认 → 加友
    ├─ C 类 (L1<20 或内容负面): 跳过
    │
    ▼
ActionExecutor: 执行互动
    │
    ├─ 点赞: smart_tap Like button
    ├─ 评论: ReplyGenerator → comment_on_post
    ├─ 等待: 随机 60-180s (浏览其他帖子填充)
    ├─ 进 Profile: tap 头像 → inspect_user_profile_posts
    ├─ 加友: add_friend_with_note (from_current_profile=True)
    │
    ▼
[fb_post_engagements] ← 记录每次互动
[facebook_friend_requests] ← 记录好友请求
```

---

## 三、新增 DB Schema

### 3.1 `fb_post_engagements` 表

```sql
CREATE TABLE IF NOT EXISTS fb_post_engagements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT    NOT NULL,
    group_name    TEXT    NOT NULL,
    author_name   TEXT    NOT NULL,
    post_snippet  TEXT    DEFAULT '',   -- 帖子正文前 200 字
    post_topic    TEXT    DEFAULT '',   -- 话题分类标签 (parenting/lifestyle/hobby...)
    l1_score      REAL    DEFAULT 0,
    content_score REAL    DEFAULT 0,    -- 内容匹配得分
    tier          TEXT    DEFAULT '',   -- A/B/C 分层
    action_liked  INTEGER DEFAULT 0,
    action_commented INTEGER DEFAULT 0,
    comment_text  TEXT    DEFAULT '',   -- 实际发出的评论内容
    action_profile_visited INTEGER DEFAULT 0,
    action_friend_sent INTEGER DEFAULT 0,
    friend_request_id INTEGER,          -- 关联 facebook_friend_requests.id
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(device_id, author_name, group_name, created_at)
);
CREATE INDEX IF NOT EXISTS idx_fpe_device ON fb_post_engagements(device_id);
CREATE INDEX IF NOT EXISTS idx_fpe_author ON fb_post_engagements(author_name);
CREATE INDEX IF NOT EXISTS idx_fpe_tier   ON fb_post_engagements(tier);
```

### 3.2 与现有表的关联

```
fb_post_engagements.friend_request_id → facebook_friend_requests.id
fb_post_engagements.device_id + group_name → facebook_groups.device_id + group_name
```

---

## 四、模块接口定义

### 4.1 `PostScan` — 帖子发现

```python
# 位置: src/app_automation/facebook.py (新方法)

def scan_group_feed_posts(
    self,
    device_id: str,
    max_posts: int = 8,
    max_scrolls: int = 12,
    min_post_chars: int = 15,
) -> List[Dict[str, Any]]:
    """在当前群组 feed 页面滚屏，提取可见帖子信息。

    Returns:
        [
            {
                "author_name": "田中花子",
                "author_avatar_bounds": (24, 1274, 104, 1354),
                "post_text": "今日は子供の運動会でした...",
                "post_bounds": (0, 1200, 720, 1500),  # 帖子区域 bounds
                "has_like_button": True,
                "has_comment_button": True,
                "likes_count": 12,       # 如可见, 否则 -1
                "comments_count": 3,     # 如可见, 否则 -1
                "scroll_index": 2,       # 第几次滚动时发现
            },
            ...
        ]
    """
```

**实现策略:**
- 复用现有 `_FB_FEED_AUTHOR_DESC_PATTERN` 提取作者名
- 复用现有 `XMLParser.parse()` 提取帖子文本节点
- 在同一次 dump 中同时提取作者名、帖子正文、头像 bounds
- 每条帖子 = 一个 "作者名 anchor" + 其下方最近的长文本节点

### 4.2 `PostAnalyzer` — 帖子内容分析

```python
# 位置: src/ai/fb_post_analyzer.py [新建]

class PostAnalyzer:
    """分析帖子内容，判断作者是否为目标客户、帖子是否适合互动。"""

    def __init__(self, persona_key: str = "jp_female_midlife"):
        self.persona_key = persona_key
        self._load_keyword_config()

    def analyze(self, post: Dict[str, Any]) -> Dict[str, Any]:
        """分析单条帖子。

        Args:
            post: scan_group_feed_posts 返回的单条帖子 dict

        Returns:
            {
                "author_name": "田中花子",
                "l1_score": 45,           # 名字评分
                "content_score": 35,      # 内容匹配得分
                "total_score": 80,        # 综合得分
                "tier": "A",              # A/B/C 分层
                "topic": "parenting",     # 话题分类
                "topic_label": "子育て",   # 日文话题标签
                "engage_reason": "育児の話題、共感ポイントあり",
                "suggested_reply_seed": "運動会、お疲れ様でした！",
                "skip_reason": "",        # 如果跳过，原因
            }
        """

    def _score_content(self, text: str) -> Tuple[float, str, str]:
        """规则层: 关键词匹配 → (score, topic, topic_label)"""

    def _score_content_llm(self, text: str, author_name: str) -> Dict:
        """LLM 层 (可选): 深度理解帖子内容"""
```

**内容评分规则 (规则层, 不需要 LLM):**

```python
# 正向关键词 → 话题分类
POSITIVE_KEYWORDS = {
    "parenting": {
        "keywords": ["子育て", "育児", "ママ", "幼稚園", "小学校", "運動会",
                     "子供", "息子", "娘", "赤ちゃん", "保育園"],
        "score": 30,
        "label": "子育て",
    },
    "lifestyle": {
        "keywords": ["日常", "暮らし", "生活", "料理", "家事", "掃除",
                     "お弁当", "ランチ", "カフェ"],
        "score": 25,
        "label": "日常生活",
    },
    "hobby": {
        "keywords": ["趣味", "手芸", "ガーデニング", "旅行", "読書",
                     "ヨガ", "散歩", "写真", "映画", "韓ドラ"],
        "score": 25,
        "label": "趣味",
    },
    "health": {
        "keywords": ["健康", "更年期", "ダイエット", "運動", "ウォーキング",
                     "睡眠", "体調"],
        "score": 20,
        "label": "健康",
    },
    "self_intro": {
        "keywords": ["はじめまして", "自己紹介", "よろしく", "初めて投稿"],
        "score": 35,  # 自我介绍帖 = 最适合互动
        "label": "自己紹介",
    },
}

# 负向关键词 → 排除
NEGATIVE_KEYWORDS = {
    "commercial": {
        "keywords": ["販売", "セミナー", "ビジネス", "副業", "投資",
                     "稼ぐ", "収入", "LINE@", "無料相談", "セッション",
                     "コンサル", "募集中", "お申し込み"],
        "score": -40,
        "label": "商业推广",
    },
    "male_topic": {
        "keywords": ["釣り", "パチンコ", "競馬", "野球", "サッカー観戦",
                     "筋トレ", "プロテイン"],
        "score": -20,
        "label": "男性话题",
    },
}
```

### 4.3 `ReplyGenerator` — 个性化回复生成

```python
# 位置: src/ai/fb_reply_generator.py [新建]

class ReplyGenerator:
    """基于帖子内容生成个性化评论。"""

    def __init__(self, persona_key: str = "jp_female_midlife"):
        self.persona_key = persona_key

    def generate(self, post_text: str, topic: str,
                 topic_label: str, author_name: str,
                 style: str = "sympathetic") -> str:
        """生成一条自然的日文评论。

        Args:
            post_text: 帖子正文
            topic: 英文话题标签 (parenting/lifestyle/hobby...)
            topic_label: 日文话题标签
            author_name: 帖子作者名
            style: 回复风格 (sympathetic/curious/supportive)

        Returns:
            评论文本 (日文, 40-80字, 含 1-2 个 emoji)
        """

    def _generate_rule_based(self, topic: str, post_text: str) -> str:
        """规则层: 基于话题的模板 + 帖子关键词插值"""

    def _generate_llm(self, post_text: str, author_name: str,
                      style: str) -> str:
        """LLM 层 (可选): 真正理解帖子内容后生成回复"""
```

**规则层回复模板示例:**

```python
REPLY_TEMPLATES = {
    "parenting": [
        "お子さんの{event}、お疲れ様でした！うちも同じ年頃なので共感です😊",
        "素敵な{event}ですね🌸子育て頑張ってるママさん応援してます✨",
        "わかります！{topic_keyword}って大変ですよね。一緒に頑張りましょう💪",
    ],
    "lifestyle": [
        "素敵な{topic_keyword}ですね🌿私も{topic_keyword}が好きです😊",
        "美味しそう！{topic_keyword}のレシピ気になります✨",
        "いいですね〜{topic_keyword}って癒されますよね🌸",
    ],
    "self_intro": [
        "はじめまして🌸同じグループにいて嬉しいです！よろしくお願いします😊",
        "はじめまして！プロフィール拝見しました。共通点が多そうで嬉しいです✨",
    ],
    "hobby": [
        "わぁ、{topic_keyword}いいですね！私も興味あります😊",
        "{topic_keyword}素敵ですね🌸もっとお話聞きたいです✨",
    ],
    "health": [
        "体調管理大事ですよね🌿私も{topic_keyword}気をつけてます😊",
        "共感します！{topic_keyword}って悩みますよね。一緒に頑張りましょう✨",
    ],
}
```

### 4.4 `EngagePipeline` — 互动流水线

```python
# 位置: src/app_automation/fb_engage_pipeline.py [新建]

class EngagePipeline:
    """完整的 浏览→分析→互动→加友 流水线。"""

    def __init__(self, fb: "FacebookAutomation",
                 config: Dict[str, Any]):
        self.fb = fb
        self.config = config
        self.analyzer = PostAnalyzer(config.get("persona_key", ""))
        self.replier = ReplyGenerator(config.get("persona_key", ""))

    def run(self, device_id: str, group_name: str,
            params: Dict[str, Any]) -> Dict[str, Any]:
        """执行一次完整的群组互动会话。

        流程:
          1. 进入群组
          2. 扫描 feed 帖子
          3. 逐帖分析 + 决策 + 执行互动
          4. 对 A/B 类帖子作者: 进 profile → 深度确认 → 加友
          5. 返回统计

        Returns:
            {
                "posts_scanned": 8,
                "posts_analyzed": 6,
                "tier_a": 2,
                "tier_b": 3,
                "tier_c": 1,
                "likes": 5,
                "comments": 2,
                "profiles_visited": 3,
                "friend_requests_sent": 2,
                "engagements": [
                    {
                        "author": "田中花子",
                        "tier": "A",
                        "topic": "parenting",
                        "actions": ["like", "comment", "profile", "add_friend"],
                        "comment_text": "運動会お疲れ様でした！...",
                    },
                    ...
                ],
            }
        """

    def _process_single_post(self, device_id: str,
                              post: Dict, analysis: Dict) -> Dict:
        """处理单条帖子的互动流程。

        A 类流程:
          1. 点赞 (100%)
          2. 阅读停顿 (3-8s, 模拟思考)
          3. 生成评论 → 发布
          4. 浏览 1-2 条其他帖子 (填充等待时间)
          5. 回到该帖子作者 → 点头像进 profile
          6. 浏览 profile (5-10s)
          7. inspect_user_profile_posts → L2 确认
          8. 发送好友请求 + 个性化附言
          9. 返回群组 feed

        B 类流程:
          1. 点赞 (80%)
          2. 点头像进 profile
          3. inspect_user_profile_posts → L2 确认
          4. 如确认 → 发送好友请求
          5. 返回群组 feed
        """

    def _natural_wait(self, min_sec: float, max_sec: float,
                       fill_action: str = "scroll"):
        """自然等待: 在等待期间执行填充动作 (滑动浏览其他帖子)。"""
```

### 4.5 `ActionExecutor` — 细粒度 UI 操作

```python
# 位置: src/app_automation/facebook.py (新方法, 集成到现有 class)

def tap_post_author_avatar(self, avatar_bounds: Tuple[int,int,int,int],
                            device_id: str) -> bool:
    """点击帖子作者头像进入其 profile 页。

    Args:
        avatar_bounds: 头像区域 (left, top, right, bottom)
    Returns:
        True if 成功跳转到 profile 页
    """

def extract_visible_post_content(self, device_id: str) -> Dict[str, Any]:
    """提取当前可见区域的帖子信息 (单条)。

    Returns:
        {
            "author_name": str,
            "post_text": str,
            "avatar_bounds": tuple,
            "like_button_bounds": tuple,
            "comment_button_bounds": tuple,
        }
    """

def navigate_back_to_group_feed(self, device_id: str,
                                 max_back_presses: int = 3) -> bool:
    """从 profile 页返回到群组 feed。"""
```

---

## 五、配置文件设计

### 5.1 `config/smart_engage.yaml` [新建]

```yaml
# 智能社群互动引擎配置
# 每个 persona_key 可以有独立配置; 未指定时使用 default

default:
  # ─── 基础节奏 ───
  posts_per_session: 8          # 每次进群浏览帖子数
  max_scrolls: 12               # 最大滚屏次数
  min_read_time_sec: 3          # 每条帖子最少阅读时间
  max_read_time_sec: 12         # 最多阅读时间
  session_max_duration_sec: 1800 # 单次会话最长 30 分钟

  # ─── 互动决策 ───
  tier_a_threshold: 60          # 综合得分 >= 60 为 A 类
  tier_b_threshold: 30          # 综合得分 >= 30 为 B 类
  like_probability_a: 1.0       # A 类帖子点赞概率
  like_probability_b: 0.8       # B 类帖子点赞概率
  comment_tier_a: true          # A 类是否评论
  comment_tier_b: false         # B 类是否评论

  # ─── 加友策略 ───
  add_friend_after_comment: true    # 评论后才加友 (A 类)
  add_friend_tier_b: true           # B 类是否加友 (跳过评论直接加)
  delay_after_comment_sec: [60, 180] # 评论后等待再加友 (秒, [min, max])
  delay_between_adds_sec: [120, 300] # 两次加友间隔
  max_friends_per_session: 5         # 单次会话最多加友数

  # ─── L1 + 内容过滤 ───
  min_l1_score: 20              # L1 名字评分最低分
  use_content_scoring: true     # 是否启用内容关键词评分
  use_llm_scoring: false        # 是否启用 LLM 深度分析 (需要 ollama)

  # ─── 回复风格 ───
  reply_style: "sympathetic"    # sympathetic / curious / supportive
  reply_language: "ja"
  reply_max_chars: 80
  reply_min_chars: 15
  reply_emoji_count: [1, 2]     # emoji 数量范围

  # ─── 好友请求附言 ───
  # 支持占位符: {group_name}, {topic_label}, {author_name}
  friend_note_templates:
    - "こんにちは🌸{group_name}で{topic_label}の投稿を拝見しました。共感したのでぜひお友達に🌿"
    - "はじめまして😊{group_name}のグループで見かけました。よろしくお願いします🌸"
    - "{topic_label}の投稿、素敵でした✨ぜひつながりたいです☺️"

  # ─── 安全限制 ───
  max_comments_per_hour: 5
  max_likes_per_hour: 15
  max_profile_visits_per_hour: 10
  risk_cooldown_sec: 600        # 检测到风险后冷却时间

jp_female_midlife:
  # 继承 default, 覆盖以下字段
  min_l1_score: 25
  reply_language: "ja"
  reply_style: "sympathetic"
  posts_per_session: 10
  max_friends_per_session: 6
```

### 5.2 Campaign Steps 注册

在 `executor.py` 的 `_FB_CAMPAIGN_DEFAULT_STEPS` 中新增 `smart_engage` step:

```python
# 旧:
_FB_CAMPAIGN_DEFAULT_STEPS = ["warmup", "group_engage", "extract_members",
                              "add_friends", "check_inbox"]
# 新 (smart_engage 取代 group_engage + extract_members + add_friends):
_FB_CAMPAIGN_SMART_STEPS = ["warmup", "smart_engage", "check_inbox"]
```

**关键设计**: `smart_engage` 是一个合并步骤，内部包含了：
- 进群
- 浏览帖子
- 分析 + 互动
- 加好友

不再需要先 extract 再 add_friends 的两阶段流程。

### 5.3 API Endpoint

```python
# 位置: src/host/routers/facebook.py

# 新增 preset
"smart_growth": {
    "steps": ["warmup", "smart_engage", "check_inbox"],
    "smart_engage_config": "jp_female_midlife",
    # ... 其他参数
}

# 新增 API: 查看/修改 smart_engage 配置
GET  /facebook/smart-engage/config
PUT  /facebook/smart-engage/config
GET  /facebook/smart-engage/stats    # 互动统计看板
```

---

## 六、执行时序 (单帖完整流程)

```
时间轴 (模拟真人 5-8 分钟/人)
─────────────────────────────────────────────────
T+0s    滚动到帖子 → dump → 提取作者名 + 帖子内容
T+2s    L1 评分: 田中花子 → score=45 (JP female)
T+2s    内容评分: "運動会" → topic=parenting, +30
T+2s    综合 75 → A 类 → 决定: 点赞+评论+加友

T+3s    截图存证 (step=engage_pre_like)
T+4s    点赞帖子 ❤️
T+5s    阅读停顿 (3-5s, 模拟思考)

T+10s   生成评论: "運動会お疲れ様でした！うちも同じ年頃..."
T+12s   点击 Comment → 输入评论 → 发送
T+15s   截图存证 (step=engage_post_comment)

T+16s   自然等待: 继续滑动浏览 2-3 条其他帖子
T+90s   (填充时间: 60-180s 随机)

T+92s   回到该帖子 (或直接从 feed 点头像)
T+93s   点击头像 → 进入 profile
T+95s   截图存证 (step=engage_profile_enter)
T+96s   浏览 profile (5-10s)
T+106s  inspect_user_profile_posts → 采集简介+帖子
T+108s  L2 确认: 简介+帖子内容符合 → PASS

T+110s  点击 Add Friend
T+112s  填入个性化附言 (含群名+话题)
T+114s  截图存证 (step=engage_friend_sent)
T+115s  发送好友请求 ✓

T+116s  press("back") → 返回群组 feed
T+120s  继续下一条帖子
─────────────────────────────────────────────────
```

---

## 七、与现有代码的集成点

### 7.1 复用的现有函数

| 函数 | 来源 | 用途 |
|------|------|------|
| `_name_signal` | `fb_lead_scorer.py` | L1 名字评分 |
| `inspect_user_profile_posts` | `facebook.py:1322` | Profile 页帖子采集 |
| `comment_on_post` | `facebook.py:8586` | 发布评论 |
| `add_friend_with_note` | `facebook.py:3101` | 发送好友请求 |
| `enter_group` | `facebook.py:8295` | 进入群组 |
| `_is_likely_fb_profile_page` | `facebook.py` | Profile 页检测 |
| `_FB_FEED_AUTHOR_DESC_PATTERN` | `facebook.py:8705` | 从 desc 提取作者名 |
| `_detect_risk_dialog` | `facebook.py` | 风险弹窗检测 |
| `rewrite_message` | `base_automation.py` | LLM 改写文案 |
| `capture_immediate` | `task_forensics.py` | 截图存证 |
| `_dedup_groups_across_devices` | `executor.py:3748` | 跨设备群组去重 |

### 7.2 需要新增的 UI 操作 (facebook.py)

| 方法 | 难度 | 说明 |
|------|------|------|
| `scan_group_feed_posts` | 中 | 在现有 `extract_group_feed_authors` 基础上扩展，同时提取帖子内容 |
| `tap_post_author_avatar` | 低 | 通过 bounds 直接 tap 头像区域 |
| `navigate_back_to_group_feed` | 低 | press("back") + 验证是否回到 feed |
| `like_current_post` | 低 | 封装 `smart_tap("Like")` + 风险检查 |

### 7.3 需要修改的现有逻辑

| 文件 | 修改点 |
|------|--------|
| `executor.py` | 在 step 循环中添加 `smart_engage` 分支 |
| `routers/facebook.py` | 新增 `smart_growth` preset + smart_engage 配置 API |
| `database.py` | 添加 `fb_post_engagements` 表 migration |
| `_launch_all.py` | 支持选择 `smart_growth` preset |

---

## 八、开发顺序 (建议 4 阶段)

### Phase 1: 基础管道 (1-2天)
- [ ] 创建 `config/smart_engage.yaml`
- [ ] 创建 `src/ai/fb_post_analyzer.py` (规则层)
- [ ] 创建 `src/ai/fb_reply_generator.py` (模板层)
- [ ] 添加 `fb_post_engagements` DB migration
- [ ] 在 `facebook.py` 添加 `scan_group_feed_posts`

### Phase 2: 流水线 + 集成 (1-2天)
- [ ] 创建 `src/app_automation/fb_engage_pipeline.py`
- [ ] 在 `executor.py` 注册 `smart_engage` step
- [ ] 在 `routers/facebook.py` 添加 `smart_growth` preset
- [ ] 更新 `_launch_all.py` 支持新 preset

### Phase 3: 真机验证 + 调优 (1天)
- [ ] 单设备测试完整流程
- [ ] 调整 timing 参数 (阅读时间/等待间隔)
- [ ] 验证截图存证
- [ ] 校准内容评分关键词

### Phase 4: LLM 增强 (可选, 1天)
- [ ] 接入 LLM 内容分析 (ollama qwen2.5)
- [ ] LLM 个性化评论生成
- [ ] A/B 测试: 模板回复 vs LLM 回复 的好友通过率

---

## 九、风险与对策

| 风险 | 概率 | 对策 |
|------|------|------|
| FB 检测异常评论频率 | 中 | 严格限速 5条/小时, 每条间隔 5min+ |
| 评论内容不自然被举报 | 低 | 模板基于真实日文社交语境, LLM 增强可选 |
| Profile 页加载失败 | 中 | 3 次重试 + 截图存证 + 降级为仅点赞 |
| 群组被踢 | 低 | 限制单群互动次数, 多群轮转 |
| 帖子内容提取不完整 | 中 | 多次 dump + 滚屏补充 + 最小字数兜底 |
