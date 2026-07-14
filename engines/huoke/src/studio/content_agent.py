# -*- coding: utf-8 -*-
"""
内容生成 Agent — 基于 CrewAI 的多 Agent 内容生产流水线。

四个 Agent 角色:
1. TrendResearcher  — 研究目标国家当前趋势话题
2. ContentStrategist — 制定内容策略和钩子话术
3. ScriptWriter      — 撰写视频脚本 (Hook + 内容 + CTA)
4. VisualDirector    — 生成每个镜头的 AI 视觉提示词

输出: JSON 格式的完整内容包，包含脚本/文案/AI提示词/话题标签
"""

import os
import json
import logging
import random
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


def _check_ollama_available() -> tuple:
    """检查 Ollama 是否在线，返回 (可用, 推荐模型名)。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            import json as _j
            data = _j.loads(r.read())
            models = [m["name"] for m in data.get("models", [])]
            # 优先选择轻量中文友好模型
            preferred = ["qwen2.5:3b", "qwen2.5:7b", "llama3.2:3b", "llama3.2:1b", "llama3.1:8b"]
            for p in preferred:
                if any(p in m for m in models):
                    return True, p
            if models:
                return True, models[0]  # 用第一个可用模型
    except Exception:
        pass
    return False, ""


# CrewAI 可选导入
try:
    from crewai import Agent, Task, Crew, Process, LLM
    CREWAI_AVAILABLE = True
except ImportError:
    logger.warning("crewai not installed. Using template fallback only.")
    CREWAI_AVAILABLE = False

# YAML 配置加载
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ---------------------------------------------------------------------------
# Template data for fallback generation
# ---------------------------------------------------------------------------

_NICHE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "lifestyle_fitness": {
        "hooks": [
            "This morning routine changed my life 🌅",
            "Nobody talks about THIS fitness hack...",
            "I tried this for 30 days — here's what happened",
        ],
        "topics": ["morning routine", "workout tips", "healthy habits", "mindset"],
        "hashtags": ["#fitness", "#lifestyle", "#healthylife", "#motivation", "#wellness"],
        "cta": ["Follow for daily tips!", "Save this for tomorrow's workout!", "Link in bio ↑"],
        "visual_style": "bright natural light, athletic wear, outdoor or modern gym setting",
    },
    "beauty_fashion": {
        "hooks": [
            "The beauty secret no one tells you 💄",
            "This $5 product beats luxury brands...",
            "Outfit formula that works for every body type",
        ],
        "topics": ["skincare routine", "outfit ideas", "makeup tips", "fashion trends"],
        "hashtags": ["#beauty", "#fashion", "#ootd", "#skincare", "#style"],
        "cta": ["Shop the look — link in bio ↑", "Tag a friend who needs this!", "Follow for more!"],
        "visual_style": "soft ring light, neutral background, close-up product shots",
    },
    "food_cooking": {
        "hooks": [
            "The easiest recipe you'll make this week 🍳",
            "This dish takes 10 minutes and tastes incredible",
            "Chef's secret ingredient revealed!",
        ],
        "topics": ["quick recipes", "meal prep", "healthy eating", "restaurant dupes"],
        "hashtags": ["#foodie", "#cooking", "#recipe", "#healthyfood", "#mealprep"],
        "cta": ["Full recipe in bio ↑", "Save this recipe!", "Tag someone to cook this with!"],
        "visual_style": "overhead flat lay, natural light, rustic kitchen counter, fresh ingredients",
    },
    "travel": {
        "hooks": [
            "Hidden gem that tourists don't know about 🗺️",
            "Pack this ONE thing for every trip...",
            "Why I quit my job to travel full time",
        ],
        "topics": ["travel tips", "hidden gems", "budget travel", "packing hacks"],
        "hashtags": ["#travel", "#wanderlust", "#travelgram", "#adventure", "#explore"],
        "cta": ["Full guide in bio ↑", "Save for your next trip!", "Follow for travel inspo!"],
        "visual_style": "golden hour lighting, wide landscape shots, candid travel moments",
    },
    "tech_business": {
        "hooks": [
            "This AI tool saves me 3 hours every day ⚡",
            "The productivity hack top CEOs won't share",
            "I automated my entire workflow — here's how",
        ],
        "topics": ["AI tools", "productivity", "business growth", "automation"],
        "hashtags": ["#tech", "#business", "#productivity", "#AI", "#entrepreneur"],
        "cta": ["Free template in bio ↑", "Follow for more business tips!", "DM me 'tool' for the list!"],
        "visual_style": "clean desk setup, minimal aesthetic, screen recordings, modern office",
    },
}

_DEFAULT_NICHE = "lifestyle_fitness"

_PLATFORM_SHOT_COUNT = {
    "tiktok": 6,
    "instagram": 6,
    "xiaohongshu": 5,
    "linkedin": 4,
    "telegram": 4,
}

_PLATFORM_DURATION = {
    "tiktok": 15,
    "instagram": 20,
    "xiaohongshu": 18,
    "linkedin": 30,
    "telegram": 20,
}


def _get_niche_for_persona(persona_config: Optional[Dict]) -> str:
    if persona_config:
        return persona_config.get("niche", _DEFAULT_NICHE)
    return _DEFAULT_NICHE


def _get_country_lang(country_config: Optional[Dict], persona_config: Optional[Dict]) -> tuple:
    country = "US"
    language = "English"
    if country_config:
        country = country_config.get("country_code", "US")
        language = country_config.get("language", "English")
    elif persona_config:
        country = persona_config.get("target_country", "US")
        language = persona_config.get("language", "English")
    return country, language


# ---------------------------------------------------------------------------
# Template fallback (works without any API keys)
# ---------------------------------------------------------------------------

def _generate_from_template(
    persona_config: Optional[Dict],
    platform: str,
    content_type: str,
    content_brief: Optional[Dict] = None,
) -> dict:
    """
    完全基于模板生成内容包，无需任何 API Key。
    根据 niche 和 country 配置生成真实感内容。
    """
    niche = _get_niche_for_persona(persona_config)
    template = _NICHE_TEMPLATES.get(niche, _NICHE_TEMPLATES[_DEFAULT_NICHE])
    country, language = _get_country_lang(None, persona_config)

    persona_id = (persona_config or {}).get("persona_id", "default_persona")

    # ContentBrief 注入：有 brief 时使用 brief 控制方向，否则随机
    _brief = content_brief or {}
    variance_seed = _brief.get("variance_seed", int(random.random() * 100000))
    _rng = random.Random(variance_seed)

    # 主题：优先使用 brief.topic，其次轮转 persona content_themes，再次随机
    persona_themes = (persona_config or {}).get("content_themes", [])
    if _brief.get("topic"):
        topic = _brief["topic"].split("(")[0].strip()
    elif persona_themes:
        # 用 variance_seed 轮转主题而非纯随机，保证不重复
        topic = persona_themes[variance_seed % len(persona_themes)]
    else:
        topic = _rng.choice(template["topics"])

    # 钩子：根据 brief.hook_type 选择对应风格
    hook_type = _brief.get("hook_type", "")
    if hook_type == "stat":
        hooks_filtered = [h for h in template["hooks"] if any(c.isdigit() for c in h)]
        hook = _rng.choice(hooks_filtered) if hooks_filtered else _rng.choice(template["hooks"])
    elif hook_type == "story":
        hooks_filtered = [h for h in template["hooks"] if any(w in h.lower() for w in ["i ", "my ", "me ", "ago", "was"])]
        hook = _rng.choice(hooks_filtered) if hooks_filtered else _rng.choice(template["hooks"])
    else:
        # 用 variance_seed 轮转 hooks（不纯随机，避免重复）
        hook = template["hooks"][variance_seed % len(template["hooks"])]

    cta = _rng.choice(template["cta"])
    tone = _brief.get("tone", "energetic")
    key_message = _brief.get("key_message", "")
    hashtags = template["hashtags"][:]
    # add country-specific hashtag
    hashtags.append(f"#{country.lower()}{niche.split('_')[0]}")

    n_shots = _PLATFORM_SHOT_COUNT.get(platform, 6)
    estimated_duration = _PLATFORM_DURATION.get(platform, 15)
    per_shot_dur = round(estimated_duration / n_shots, 1)

    visual_prompts = []
    shot_descriptions = [
        f"Opening hook scene — {template['visual_style']}",
        f"Problem/pain point illustration — relatable moment",
        f"Solution reveal — {topic} in action",
        f"Key tip or demonstration closeup",
        f"Result/transformation showcase",
        f"CTA outro with creator in frame",
    ]
    for i in range(n_shots):
        desc = shot_descriptions[i] if i < len(shot_descriptions) else f"Supporting scene {i+1}"
        prompt = (
            f"Cinematic {template['visual_style']}, {desc}, "
            f"ultra HD 4K, professional photography, "
            f"vibrant colors, social media optimized vertical format"
        )
        visual_prompts.append({
            "shot": i + 1,
            "duration": per_shot_dur,
            "prompt": prompt,
            "description": desc,
        })

    # 根据 tone 调整脚本风格
    tone_style = {
        "energetic":   ("Let's GO!", "No excuses — start NOW!", "This WILL change your life!"),
        "educational": ("Here's what the research shows:", "The key insight is:", "Most people don't realize that"),
        "inspiring":   ("I've been where you are.", "Everything changed when I discovered", "You're closer than you think."),
        "casual":      ("Okay so real talk —", "This is gonna sound simple but", "Honestly?"),
        "emotional":   ("I know how hard this is.", "You're not alone in this.", "This one's for the ones who are struggling."),
    }.get(tone, ("Here's the truth:", "The key is:", "Let me show you"))

    # 生成有框架感的脚本
    framework_id = _brief.get("framework_id", "hook_question")
    script_body = {
        "myth_busting":     f"Everyone tells you to {topic.split()[0] if topic.split() else 'do this'}. But {tone_style[0]} the real truth is most conventional advice gets this completely backwards. Here's what actually works:",
        "before_after":     f"I used to struggle with {topic} every single day. {tone_style[1]} a simple shift in my approach. Now the results speak for themselves. Here's the exact thing I changed:",
        "5_step_tutorial":  f"Step 1: Start with the foundation. Step 2: Build consistency. Step 3 — this is the one most people skip — track your progress. Step 4: Adjust based on results. Step 5: Scale what works.",
        "mistake_correction": f"Mistake #1: Trying to do too much too fast. Mistake #2: Ignoring the fundamentals of {topic}. Mistake #3 — the biggest one — not being consistent enough. Here's the fix for all three:",
        "quick_win":        f"Here's the 30-second version: {tone_style[0]} For {topic}, you only need to do one thing consistently. That's it. I'll show you exactly what that is.",
        "emotional_hook":   f"{tone_style[0]} If you've been struggling with {topic}, this is for you. {key_message or 'You have everything you need to succeed.'} Let me show you how.",
    }.get(framework_id, (
        f"Today I'm sharing the real approach to {topic}. "
        f"{tone_style[0]} This works even if you're a complete beginner. "
        f"The key insight: {key_message or 'consistency beats perfection every time.'}. "
        f"Here's exactly how to apply this starting today."
    ))

    script = (
        f"[HOOK - 0-3s]\n{hook}\n\n"
        f"[CONTENT - 3-{estimated_duration - 3}s]\n"
        f"{script_body}\n\n"
        f"[CTA - last 3s]\n{cta}"
    )

    voiceover = (
        f"{hook} {script_body.split('.')[0] if '.' in script_body else script_body[:80]}. "
        f"{key_message + '.' if key_message else ''} "
        f"Stay with me to the end. {cta}"
    )

    caption = f"{hook}\n\n{' '.join(hashtags[:5])}"

    video_prompt = (
        f"Cinematic video about {topic}, {template['visual_style']}, "
        f"dynamic camera movement, professional color grading, "
        f"social media vertical 9:16 format, ultra HD quality"
    ) if content_type == "video" else ""

    return {
        "persona_id": persona_id,
        "platform": platform,
        "content_type": content_type,
        "script": script,
        "hook": hook,
        "voiceover": voiceover,
        "caption": caption,
        "hashtags": hashtags,
        "cta_text": cta,
        "visual_prompts": visual_prompts,
        "video_prompt": video_prompt,
        "estimated_duration": estimated_duration,
        "_source": "template",
    }


# ---------------------------------------------------------------------------
# CrewAI multi-agent class
# ---------------------------------------------------------------------------

class ContentGenerationCrew:
    """
    四 Agent CrewAI 内容生成流水线。

    即使未配置 API Key，实例化和 generate_content 也不会崩溃
    —— 自动降级为模板生成。
    """

    def __init__(
        self,
        llm_provider: str = "openai",
        api_key: str = "",
        persona_config: Optional[Dict] = None,
        country_config: Optional[Dict] = None,
    ):
        self.llm_provider = llm_provider
        self.api_key = api_key
        self.persona_config = persona_config or {}
        self.country_config = country_config or {}
        self._llm = None
        self._agents_ready = False

        if CREWAI_AVAILABLE:
            try:
                self._llm = self._build_llm()
                if self._llm is not None:
                    self._agents_ready = True
            except Exception as exc:
                logger.warning("LLM init failed, will use templates: %s", exc)

    def _build_llm(self) -> Any:
        if not self.api_key:
            # 尝试 Ollama 本地 LLM
            ollama_ok, ollama_model = _check_ollama_available()
            if ollama_ok:
                try:
                    from langchain_ollama import ChatOllama
                    llm = ChatOllama(model=ollama_model, temperature=0.7, base_url="http://localhost:11434")
                    logger.info("使用 Ollama 本地 LLM: %s", ollama_model)
                    return llm
                except ImportError:
                    try:
                        from langchain_community.llms import Ollama
                        llm = Ollama(model=ollama_model, base_url="http://localhost:11434")
                        logger.info("使用 Ollama (langchain_community): %s", ollama_model)
                        return llm
                    except ImportError:
                        logger.warning("langchain_ollama 未安装，请运行: pip install langchain-ollama")
                except Exception as e:
                    logger.warning("Ollama 初始化失败: %s", e)
            return None
        if self.llm_provider == "anthropic":
            return LLM(
                model="anthropic/claude-haiku-20240307",
                api_key=self.api_key,
            )
        return LLM(
            model="openai/gpt-4o-mini",
            api_key=self.api_key,
        )

    def _build_agents(self, platform: str, content_type: str) -> Dict[str, Any]:
        country, language = _get_country_lang(self.country_config, self.persona_config)
        niche = _get_niche_for_persona(self.persona_config)

        trend_researcher = Agent(
            role="Trend Researcher",
            goal=(
                f"Research current trending topics in {country} for {niche} content on {platform}. "
                f"Identify viral content patterns, trending hashtags, and audience pain points."
            ),
            backstory=(
                f"You are an expert social media analyst specializing in {country} markets. "
                f"You track viral content daily across all major platforms and understand "
                f"what makes content resonate with {language}-speaking audiences."
            ),
            llm=self._llm,
            verbose=False,
            allow_delegation=False,
        )

        content_strategist = Agent(
            role="Content Strategist",
            goal=(
                f"Create a compelling content strategy with a powerful hook for {platform} "
                f"in the {niche} niche targeting {country} audience."
            ),
            backstory=(
                "You are a top-tier content strategist who has helped 100+ creators "
                "grow to 1M+ followers. You know the proven Hook-Content-CTA formula "
                "and adapt it for each platform's algorithm."
            ),
            llm=self._llm,
            verbose=False,
            allow_delegation=False,
        )

        script_writer = Agent(
            role="Script Writer",
            goal=(
                f"Write a complete, engaging video script in {language} for a {platform} "
                f"{content_type} video about {niche}. Include Hook, main content, and CTA."
            ),
            backstory=(
                f"You are a professional scriptwriter who specializes in short-form video content. "
                f"Your scripts are punchy, conversational, and optimized for {platform}'s audience. "
                f"You write naturally in {language}."
            ),
            llm=self._llm,
            verbose=False,
            allow_delegation=False,
        )

        visual_director = Agent(
            role="Visual Director",
            goal=(
                "Generate detailed AI image/video generation prompts for each shot in the video. "
                "Prompts should be specific, cinematic, and optimized for Midjourney/DALL-E/Sora."
            ),
            backstory=(
                "You are a visual director with expertise in AI-generated content. "
                "You translate scripts into precise visual prompts that generate stunning, "
                "on-brand imagery consistent with social media aesthetics."
            ),
            llm=self._llm,
            verbose=False,
            allow_delegation=False,
        )

        return {
            "trend_researcher": trend_researcher,
            "content_strategist": content_strategist,
            "script_writer": script_writer,
            "visual_director": visual_director,
        }

    def _build_tasks(self, agents: Dict, platform: str, content_type: str) -> List[Any]:
        country, language = _get_country_lang(self.country_config, self.persona_config)
        niche = _get_niche_for_persona(self.persona_config)
        n_shots = _PLATFORM_SHOT_COUNT.get(platform, 6)

        task_research = Task(
            description=(
                f"Research trending topics for {niche} content in {country} on {platform}. "
                f"Output: top 3 trending topics, 10 relevant hashtags, and 1 viral content angle."
            ),
            expected_output="JSON with keys: trending_topics, hashtags, content_angle",
            agent=agents["trend_researcher"],
        )

        task_strategy = Task(
            description=(
                f"Based on research, create a content strategy. "
                f"Develop a powerful 3-second hook and overall content structure for {platform}."
            ),
            expected_output="JSON with keys: hook, content_outline, cta_text",
            agent=agents["content_strategist"],
            context=[task_research],
        )

        task_script = Task(
            description=(
                f"Write a complete video script in {language} for a {platform} {content_type}. "
                f"Include: hook (0-3s), main content, CTA (last 3s). "
                f"Also write the voiceover text and platform caption with hashtags."
            ),
            expected_output=(
                "JSON with keys: script, hook, voiceover, caption, hashtags, cta_text, estimated_duration"
            ),
            agent=agents["script_writer"],
            context=[task_research, task_strategy],
        )

        task_visuals = Task(
            description=(
                f"Generate {n_shots} shot-by-shot AI visual prompts for this {content_type} video. "
                f"Each prompt must be detailed (50+ words) for AI image/video generation. "
                f"Also create one full video_prompt if content_type is 'video'."
            ),
            expected_output=(
                f"JSON with key: visual_prompts (array of {n_shots} objects with shot, duration, prompt, description), "
                f"and video_prompt (string)"
            ),
            agent=agents["visual_director"],
            context=[task_script],
        )

        return [task_research, task_strategy, task_script, task_visuals]

    def _parse_crew_output(self, result: Any, persona_id: str, platform: str, content_type: str) -> dict:
        """解析 CrewAI 输出，合并为最终 JSON。"""
        base = _generate_from_template(self.persona_config, platform, content_type)
        base["persona_id"] = persona_id
        base["_source"] = "crewai"

        raw = str(result)
        # 尝试从输出中提取 JSON 片段
        try:
            start = raw.rfind("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(raw[start:end])
                base.update({k: v for k, v in parsed.items() if v})
        except Exception:
            pass
        return base

    def generate_content(
        self,
        persona_id: str,
        platform: str,
        content_type: str = "slideshow",
    ) -> dict:
        """
        生成完整内容包。
        若 API Key 未配置或 crewai 不可用，自动降级为模板生成。
        """
        if not self._agents_ready or not CREWAI_AVAILABLE:
            logger.info("Using template fallback for persona=%s platform=%s", persona_id, platform)
            result = _generate_from_template(self.persona_config, platform, content_type)
            result["persona_id"] = persona_id
            return result

        try:
            agents = self._build_agents(platform, content_type)
            tasks = self._build_tasks(agents, platform, content_type)
            crew = Crew(
                agents=list(agents.values()),
                tasks=tasks,
                process=Process.sequential,
                verbose=False,
            )
            output = crew.kickoff()
            return self._parse_crew_output(output, persona_id, platform, content_type)
        except Exception as exc:
            logger.error("CrewAI generation failed, using template: %s", exc)
            result = _generate_from_template(self.persona_config, platform, content_type)
            result["persona_id"] = persona_id
            return result

    def generate_content_batch(
        self,
        persona_id: str,
        platforms: List[str],
        content_type: str = "slideshow",
    ) -> List[dict]:
        """
        批量为多个平台生成内容。
        每个平台适配对应的字幕/话题标签/格式。
        """
        results = []
        for platform in platforms:
            logger.info("Generating content for platform=%s", platform)
            content = self.generate_content(persona_id, platform, content_type)
            results.append(content)
        return results


# ---------------------------------------------------------------------------
# Standalone helper
# ---------------------------------------------------------------------------

def _load_yaml_config(path: str) -> Dict:
    if not YAML_AVAILABLE or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to load yaml %s: %s", path, exc)
        return {}


def generate_content_simple(
    persona_id: str,
    platform: str,
    content_type: str = "slideshow",
    config_path: Optional[str] = None,
) -> dict:
    """
    无需手动构建 ContentGenerationCrew 的便捷函数。

    自动从 studio_config.yaml 和 personas.yaml 加载配置。
    若 API Key 未配置，完全使用模板生成（始终可用）。
    """
    # 推断配置目录
    if config_path is None:
        config_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "config")
        )

    studio_cfg = _load_yaml_config(os.path.join(config_path, "studio_config.yaml"))
    personas_cfg = _load_yaml_config(os.path.join(config_path, "personas.yaml"))

    # 查找 persona 配置
    persona_config = None
    if isinstance(personas_cfg, dict):
        persona_config = personas_cfg.get(persona_id) or personas_cfg.get("default")
    elif isinstance(personas_cfg, list):
        for p in personas_cfg:
            if isinstance(p, dict) and p.get("persona_id") == persona_id:
                persona_config = p
                break
    if persona_config is None:
        persona_config = {"persona_id": persona_id}

    # 读取 LLM 配置
    llm_provider = studio_cfg.get("llm_provider", "openai")
    api_key = (
        studio_cfg.get("openai_api_key", "")
        or studio_cfg.get("anthropic_api_key", "")
        or os.environ.get("OPENAI_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )

    if not api_key:
        logger.info("No API key configured — using template generation.")
        result = _generate_from_template(persona_config, platform, content_type)
        result["persona_id"] = persona_id
        return result

    crew = ContentGenerationCrew(
        llm_provider=llm_provider,
        api_key=api_key,
        persona_config=persona_config,
    )
    return crew.generate_content(persona_id, platform, content_type)


def apply_content_variance(content: dict, persona_id: str, platform: str, seed: int = None) -> dict:
    """
    对生成的内容应用轻微变体，使同一批素材在不同账号/平台间产生细微差异。
    防止平台算法因内容完全相同而判定为机器行为。

    变体维度：
    1. Caption 开头词替换（问候语/感叹词多样化）
    2. Hashtag 顺序打乱 + 随机丢弃1-2个非核心标签
    3. CTA 文案轮换（几种不同说法）
    4. Emoji 位置微调
    """
    import hashlib

    # 用 persona_id + platform + 日期 生成确定性种子（同一人设同一天同一变体）
    if seed is None:
        from datetime import date
        seed_str = f"{persona_id}:{platform}:{date.today().isoformat()}"
        seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)

    rng = random.Random(seed)

    result = dict(content)

    # 1. Caption 开头变体
    caption = result.get("caption", "")
    openers = [
        "", "✨ ", "🔥 ", "💡 ", "👇 ", "📌 ", "→ ",
        "Watch this → ", "Stop scrolling! ", "Real talk: ",
    ]
    if caption and not caption.startswith(tuple(["✨", "🔥", "💡", "👇", "📌", "→"])):
        chosen_opener = rng.choice(openers)
        result["caption"] = chosen_opener + caption

    # 2. Hashtag 变体（打乱顺序 + 随机丢弃边缘标签）
    hashtags = result.get("hashtags", [])
    if len(hashtags) > 3:
        core = hashtags[:3]      # 前3个核心标签保留
        extra = hashtags[3:]     # 后面的随机处理
        rng.shuffle(extra)
        drop_n = rng.randint(0, min(2, len(extra)))
        extra = extra[drop_n:]   # 随机丢弃0-2个边缘标签
        result["hashtags"] = core + extra

    # 3. CTA 文案轮换
    cta_variants = [
        "Link in bio 👆", "Check bio for details", "Bio link 🔗",
        "Tap bio link ↑", "Visit link in bio", "See bio for more",
    ]
    script = result.get("script", "")
    if "link in bio" in script.lower() or "bio" in script.lower():
        new_cta = rng.choice(cta_variants)
        # 简单替换最后出现的 CTA
        for phrase in ["Link in bio", "link in bio", "bio link", "Bio link"]:
            if phrase in script:
                script = script.replace(phrase, new_cta, 1)
                break
        result["script"] = script

    result["_variance_applied"] = True
    result["_variance_seed"] = seed

    return result


def generate_content(
    persona_config: dict,
    platform: str,
    content_type: str = "slideshow",
    cta_link: str = "",
    llm_api_key: str = "",
    content_brief: Optional[Dict] = None,
) -> dict:
    """
    带完整参数的内容生成入口（供 StudioManager 调用）。

    persona_config — personas.yaml 中的完整人设配置 dict
    platform       — 目标平台 (tiktok/instagram/telegram/...)
    content_type   — video / slideshow / text
    cta_link       — 引流链接，注入到脚本 CTA 段
    llm_api_key    — LLM API Key，空则走模板回退
    content_brief  — ContentBrief.to_dict()，提供内容方向控制
    """
    persona_id = persona_config.get("persona_id", "default")

    if not llm_api_key:
        llm_api_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")

    if not llm_api_key:
        # 尝试 Ollama 本地 LLM 回退
        ollama_ok, ollama_model = _check_ollama_available()
        if ollama_ok and CREWAI_AVAILABLE:
            try:
                crew = ContentGenerationCrew(
                    llm_provider="openai",  # placeholder, _build_llm 会走 Ollama 分支
                    api_key="",
                    persona_config=persona_config,
                )
                if crew._agents_ready:
                    result = crew.generate_content(persona_id, platform, content_type)
                    result["generation_mode"] = "ollama"
                    if cta_link:
                        result["cta_link"] = cta_link
                    # 应用内容变体（防同质化）
                    try:
                        result = apply_content_variance(result, persona_id, platform)
                    except Exception as _ve:
                        logger.debug("变体应用失败（非致命）: %s", _ve)
                    return result
            except Exception as exc:
                logger.warning("Ollama 生成失败，回退到模板: %s", exc)

        result = _generate_from_template(persona_config, platform, content_type, content_brief=content_brief)
        result["persona_id"] = persona_id
        result["generation_mode"] = "template"
        if cta_link:
            result["cta_link"] = cta_link
        # 应用内容变体（防同质化）
        try:
            result = apply_content_variance(result, persona_id, platform)
        except Exception as _ve:
            logger.debug("变体应用失败（非致命）: %s", _ve)
        return result

    # 推断 provider
    llm_provider = "openai"
    if os.environ.get("ANTHROPIC_API_KEY") == llm_api_key:
        llm_provider = "anthropic"

    crew = ContentGenerationCrew(
        llm_provider=llm_provider,
        api_key=llm_api_key,
        persona_config=persona_config,
    )
    result = crew.generate_content(persona_id, platform, content_type)
    result["generation_mode"] = "llm"
    if cta_link:
        result["cta_link"] = cta_link
    # 应用内容变体（防同质化）
    try:
        result = apply_content_variance(result, persona_id, platform)
    except Exception as _ve:
        logger.debug("变体应用失败（非致命）: %s", _ve)
    return result
