# -*- coding: utf-8 -*-
"""
内容工作室主编排器 — Content Studio Manager。

协调以下模块的完整内容生产流水线:
  1. content_agent.py   → 生成脚本/文案/AI提示词
  2. image_generator.py → 生成图片素材 (图文混剪模式)
  3. video_generator.py → 生成AI视频 (全视频模式)
  4. tts_generator.py   → 生成配音
  5. video_processor.py → 合成最终视频
  6. publishers/        → ADB发布到各平台

全自动模式: 生成后直接排队发布
半自动模式: 生成后等待人工审核确认
"""

import logging
import os
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import PROJECT_ROOT, config_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 可选依赖导入
# ---------------------------------------------------------------------------

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    logger.warning("PyYAML 未安装，将使用空配置")

def _load_yaml(path: Path) -> Dict[str, Any]:
    """安全加载 YAML 文件，失败返回空 dict。"""
    if not YAML_AVAILABLE or not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("加载 YAML 失败 %s: %s", path, e)
        return {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Publisher 工厂
# ---------------------------------------------------------------------------

def get_publisher(platform: str, device_id: Optional[str] = None):
    """根据平台名称返回对应的 Publisher 实例，未知平台返回 None。"""
    try:
        if platform == "tiktok":
            from .publishers.tiktok_publisher import TikTokPublisher
            return TikTokPublisher(device_id=device_id)
        elif platform == "instagram":
            from .publishers.instagram_publisher import InstagramPublisher
            return InstagramPublisher(device_id=device_id)
        elif platform == "telegram":
            from .publishers.telegram_publisher import TelegramPublisher
            return TelegramPublisher(device_id=device_id)
        else:
            logger.warning("不支持的平台: %s", platform)
            return None
    except ImportError as e:
        logger.error("加载发布器失败 [%s]: %s", platform, e)
        return None


# ---------------------------------------------------------------------------
# StudioManager
# ---------------------------------------------------------------------------

class StudioManager:
    """
    Content Studio 主编排器。

    负责协调内容生成 → 审核 → 发布的完整流水线。
    每次 run_job() 在后台线程中运行，不阻塞调用方。
    """

    def __init__(self, config_path: Optional[str] = None):
        # ── 加载配置文件 ──────────────────────────────────────────────────
        cfg_path = Path(config_path) if config_path else config_file("studio_config.yaml")
        personas_path = config_file("personas.yaml")

        self.config: Dict[str, Any] = _load_yaml(cfg_path)
        # personas.yaml 顶层有 "personas" key，取内层 dict
        _raw_personas = _load_yaml(personas_path)
        self.personas: Dict[str, Any] = _raw_personas.get("personas", _raw_personas)
        self._config_path = cfg_path

        studio_cfg = self.config.get("studio", {})

        # ── API Keys: 优先环境变量，其次配置文件 ──────────────────────────
        api_keys = studio_cfg.get("api_keys", {})
        self.fal_ai_key: str = os.getenv("FAL_KEY", "") or api_keys.get("fal_ai", "")
        self.llm_key: str = (
            os.getenv("OPENAI_API_KEY", "")
            or os.getenv("ANTHROPIC_API_KEY", "")
            or api_keys.get("openai", "")
            or api_keys.get("anthropic", "")
        )

        # ── 存储路径 ──────────────────────────────────────────────────────
        storage = studio_cfg.get("storage", {})
        self.final_dir = PROJECT_ROOT / storage.get("final_dir", "data/studio/final")
        self.final_dir.mkdir(parents=True, exist_ok=True)

        # ── 默认配置 ──────────────────────────────────────────────────────
        gen_cfg = studio_cfg.get("generation", {})
        self.default_mode: str = studio_cfg.get("mode", "semi_auto")
        self.default_content_type: str = gen_cfg.get("default_style", "slideshow")

        # ── 初始化数据库 ──────────────────────────────────────────────────
        from . import studio_db
        self.studio_db = studio_db
        studio_db.init_studio_db()

        # 启动定时发布调度线程
        self._schedule_thread = threading.Thread(
            target=self._run_schedule_loop, daemon=True, name="studio-scheduler"
        )
        self._schedule_thread.start()

        logger.info(
            "StudioManager 初始化完成 | mode=%s | default_type=%s",
            self.default_mode, self.default_content_type,
        )

    # -----------------------------------------------------------------------
    # 公开接口
    # -----------------------------------------------------------------------

    def run_job(
        self,
        persona_id: str,
        platforms: List[str],
        content_type: Optional[str] = None,
        mode: Optional[str] = None,
        content_brief: Optional[dict] = None,
    ) -> str:
        """
        创建内容生成任务并在后台线程中执行完整流水线。

        :param persona_id:     人设 ID，对应 personas.yaml 中的 key
        :param platforms:      目标平台列表，如 ["tiktok", "instagram"]
        :param content_type:   "video" | "slideshow" | "text" | None（从人设/配置自动选）
        :param mode:           "full_auto" | "semi_auto" | None（从配置自动选）
        :param content_brief:  ContentBrief.to_dict()，控制内容方向（可选）
        :return:               job_id (UUID 字符串)
        """
        # 确定最终 content_type 和 mode
        resolved_type = content_type or self._resolve_content_type(persona_id)
        resolved_mode = mode or self.default_mode

        # 在数据库创建 job 记录
        job_id = self.studio_db.create_job(persona_id, platforms, resolved_mode)
        logger.info("新任务已创建 job_id=%s persona=%s platforms=%s mode=%s type=%s brief=%s",
                    job_id, persona_id, platforms, resolved_mode, resolved_type,
                    bool(content_brief))

        # 后台线程执行流水线，不阻塞调用方
        t = threading.Thread(
            target=self._run_job_pipeline,
            args=(job_id, persona_id, platforms, resolved_type, resolved_mode, content_brief),
            daemon=True,
            name=f"studio-job-{job_id[:8]}",
        )
        t.start()
        return job_id

    def _run_job_pipeline(
        self,
        job_id: str,
        persona_id: str,
        platforms: List[str],
        content_type: str,
        mode: str,
        content_brief: Optional[dict] = None,
    ) -> None:
        """
        后台线程中运行的完整内容生产流水线。

        各平台独立处理，单平台失败不影响其他平台。
        """
        self.studio_db.update_job_status(job_id, "generating")
        persona_config = self.personas.get(persona_id, {})
        studio_cfg = self.config.get("studio", {})
        cta_cfg = studio_cfg.get("cta", {})

        any_success = False

        for platform in platforms:
            # 每个平台独立 try/except，互不干扰
            content_id: Optional[int] = None
            try:
                logger.info("[%s] 开始生成 platform=%s type=%s", job_id[:8], platform, content_type)

                # (a) 创建 content 记录，状态 pending
                style = persona_config.get("style", "general")
                content_id = self.studio_db.create_content(job_id, platform, content_type, style)

                # (b) 调用 content_agent 生成脚本/文案/AI提示词
                from .content_agent import generate_content
                content_pkg = generate_content(
                    persona_config=persona_config,
                    platform=platform,
                    content_type=content_type,
                    cta_link=cta_cfg.get("primary_link", ""),
                    llm_api_key=self.llm_key,
                    content_brief=content_brief,
                )

                # 写入脚本/文案/标签/视觉提示词
                self.studio_db.update_content(
                    content_id,
                    script=content_pkg.get("script", ""),
                    caption=content_pkg.get("caption", ""),
                    hashtags=content_pkg.get("hashtags", []),
                    visual_prompts=content_pkg.get("visual_prompts", []),
                    voiceover_text=content_pkg.get("voiceover_text", ""),
                    status="generating",
                )

                final_video_path: Optional[str] = None

                # (c) 根据内容类型生成媒体
                if content_type == "slideshow":
                    final_video_path = self._generate_slideshow(
                        content_id=content_id,
                        content_pkg=content_pkg,
                        persona_config=persona_config,
                        platform=platform,
                    )
                elif content_type == "video":
                    final_video_path = self._generate_video(
                        content_id=content_id,
                        content_pkg=content_pkg,
                        persona_config=persona_config,
                        platform=platform,
                    )
                elif content_type == "text":
                    # 纯文字帖，无需视频生成
                    final_video_path = None
                    logger.info("[%s] text模式，跳过视频生成", job_id[:8])

                # (d) 保存最终路径，更新状态为 ready
                self.studio_db.update_content(
                    content_id,
                    final_video_path=final_video_path,
                    status="ready",
                )
                any_success = True
                logger.info("[%s] platform=%s 内容就绪 content_id=%d", job_id[:8], platform, content_id)

                # (e/f) 根据模式决定后续动作
                if mode == "full_auto":
                    logger.info("[%s] full_auto 模式，直接触发发布 content_id=%d", job_id[:8], content_id)
                    self.publish_content(content_id)
                else:
                    # semi_auto: 保持 ready 状态等待人工审核
                    logger.info("[%s] semi_auto 模式，等待人工审核 content_id=%d", job_id[:8], content_id)

            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                logger.error("[%s] platform=%s 生成失败: %s", job_id[:8], platform, err_msg)
                logger.debug(traceback.format_exc())
                if content_id is not None:
                    self.studio_db.update_content(content_id, status="failed", error_msg=err_msg)

        # 更新 job 最终状态
        final_status = "published" if any_success and mode == "full_auto" else (
            "ready" if any_success else "failed"
        )
        self.studio_db.update_job_status(job_id, final_status)
        logger.info("任务完成 job_id=%s final_status=%s", job_id, final_status)

        # ── Telegram 通知 ─────────────────────────────────────────────────
        try:
            from .studio_notifier import notify_job_ready, notify_job_failed
            if final_status == "ready" and mode == "semi_auto":
                # 拉取刚生成的 content 列表（用于通知预览）
                pending = self.studio_db.list_pending_approval()
                job_contents = [c for c in pending if c.get("job_id") == job_id]
                notify_job_ready(job_id, persona_id, platforms, job_contents)
            elif final_status == "failed":
                notify_job_failed(job_id, persona_id, "所有平台生成均失败，请检查日志")
        except Exception as _ne:
            logger.debug("通知发送异常（不影响主流程）: %s", _ne)

    # -----------------------------------------------------------------------
    # 媒体生成辅助方法
    # -----------------------------------------------------------------------

    def _generate_slideshow(
        self, content_id: int, content_pkg: dict, persona_config: dict, platform: str
    ) -> Optional[str]:
        """图文混剪流水线: 生成图片 → TTS配音 → MoviePy合成。"""
        from .image_generator import generate_images
        from .tts_generator import generate_tts
        from .video_processor import compose_slideshow

        visual_prompts = content_pkg.get("visual_prompts", [])
        voiceover_text = content_pkg.get("voiceover_text", "") or content_pkg.get("script", "")

        # 生成图片
        image_paths = generate_images(
            prompts=visual_prompts,
            fal_api_key=self.fal_ai_key,
            persona_config=persona_config,
            platform=platform,
        )
        self.studio_db.update_content(content_id, image_paths=image_paths)

        # 生成 TTS 配音
        audio_path: Optional[str] = None
        if voiceover_text:
            audio_path = generate_tts(
                text=voiceover_text,
                persona_config=persona_config,
                platform=platform,
            )
            if audio_path:
                self.studio_db.update_content(content_id, audio_path=audio_path)

        # MoviePy 合成最终视频
        output_path = str(self.final_dir / f"{content_id}_{platform}_slideshow.mp4")
        final_path = compose_slideshow(
            image_paths=image_paths,
            audio_path=audio_path,
            output_path=output_path,
            platform=platform,
        )
        return final_path

    def _generate_video(
        self, content_id: int, content_pkg: dict, persona_config: dict, platform: str
    ) -> Optional[str]:
        """全视频流水线: fal.ai生成AI视频 → MoviePy后处理。"""
        from .video_generator import generate_ai_video
        from .video_processor import post_process_video

        studio_cfg = self.config.get("studio", {}).get("generation", {})
        video_model = studio_cfg.get("video_model", "wan/v2.6/text-to-video")
        video_duration = studio_cfg.get("video_duration", 15)

        prompt = content_pkg.get("video_prompt") or (content_pkg.get("visual_prompts") or [""])[0]

        # fal.ai 生成原始视频
        raw_video_path = generate_ai_video(
            prompt=prompt,
            fal_api_key=self.fal_ai_key,
            model=video_model,
            duration=video_duration,
            persona_config=persona_config,
            platform=platform,
        )
        if raw_video_path:
            self.studio_db.update_content(content_id, video_path=raw_video_path)

        # TTS 配音
        from .tts_generator import generate_tts
        voiceover_text = content_pkg.get("voiceover_text", "") or content_pkg.get("script", "")
        audio_path: Optional[str] = None
        if voiceover_text:
            audio_path = generate_tts(
                text=voiceover_text,
                persona_config=persona_config,
                platform=platform,
            )
            if audio_path:
                self.studio_db.update_content(content_id, audio_path=audio_path)

        # MoviePy 后处理（加字幕/音轨/水印等）
        output_path = str(self.final_dir / f"{content_id}_{platform}_video.mp4")
        final_path = post_process_video(
            video_path=raw_video_path,
            audio_path=audio_path,
            output_path=output_path,
            platform=platform,
        )
        return final_path

    # -----------------------------------------------------------------------
    # 审核与发布
    # -----------------------------------------------------------------------

    def approve_and_publish(self, content_id: int, device_id: Optional[str] = None) -> dict:
        """
        人工审核通过并立即触发发布（semi_auto 模式专用）。
        """
        content = self.studio_db.get_content(content_id)
        if not content:
            return {"success": False, "error": f"content_id={content_id} 不存在"}

        # 更新审核状态
        self.studio_db.approve_content(content_id)
        logger.info("内容已批准 content_id=%d，开始发布", content_id)

        # 触发发布
        result = self.publish_content(content_id, device_id=device_id)
        return result

    def publish_content(self, content_id: int, device_id: Optional[str] = None) -> dict:
        """
        对指定 content 执行 ADB 发布。

        1. 从 studio_db 读取 final_video_path 和 caption
        2. 根据 platform 实例化对应 Publisher
        3. 调用 publisher.publish()
        4. 记录结果到 studio_posts 表
        """
        content = self.studio_db.get_content(content_id)
        if not content:
            return {"success": False, "content_id": content_id, "error": "记录不存在"}

        platform = content.get("platform", "")
        caption = content.get("caption", "")
        hashtags = content.get("hashtags", [])
        final_video_path = content.get("final_video_path")
        _persona_id = content.get("persona_id", "unknown")

        # 健康监控：检查账号是否被暂停/封禁
        from .account_health import get_health_monitor
        _hm = get_health_monitor()
        if _hm.is_paused(_persona_id, platform):
            logger.warning("账号 %s@%s 已暂停，跳过发布 content_id=%d", _persona_id, platform, content_id)
            self.studio_db.update_content(content_id, status="pending_manual",
                                          error_msg=f"账号 {_persona_id}@{platform} 健康监控已暂停")
            return {
                "success": False,
                "content_id": content_id,
                "platform": platform,
                "post_id": None,
                "error": f"账号 {_persona_id}@{platform} 已被健康监控暂停，需人工处理",
            }

        # 更新状态为发布中
        self.studio_db.update_content(content_id, status="publishing")

        publisher = get_publisher(platform, device_id=device_id)
        if publisher is None:
            err = f"平台 {platform} 没有可用的发布器"
            self.studio_db.update_content(content_id, status="failed", error_msg=err)
            return {"success": False, "content_id": content_id, "platform": platform, "error": err}

        try:
            # 组装完整文案（caption + hashtags）
            full_caption = caption
            if hashtags:
                full_caption = f"{caption}\n\n{' '.join(hashtags)}"

            result = publisher.publish(
                video_path=final_video_path,
                caption=full_caption,
                hashtags=hashtags,
            )
            success = getattr(result, "success", False)
            post_id = getattr(result, "post_id", None)
            error = getattr(result, "error", None)
        except Exception as exc:
            success = False
            post_id = None
            error = f"{type(exc).__name__}: {exc}"
            logger.error("发布异常 content_id=%d platform=%s: %s", content_id, platform, error)

        # 健康监控：记录发布结果
        if success:
            _hm.record_success(_persona_id, platform)
        else:
            _hm.record_failure(_persona_id, platform, error or "")

        # 更新 content 状态
        new_status = "published" if success else "failed"
        self.studio_db.update_content(
            content_id, status=new_status, error_msg=None if success else error
        )

        # 记录 studio_posts
        if success:
            self.studio_db.create_post(content_id, platform, post_id, device_id)

        logger.info("发布结果 content_id=%d platform=%s success=%s", content_id, platform, success)
        return {
            "success": success,
            "content_id": content_id,
            "platform": platform,
            "post_id": post_id,
            "error": error,
        }

    # -----------------------------------------------------------------------
    # 查询接口
    # -----------------------------------------------------------------------

    def get_job_status(self, job_id: str) -> dict:
        """返回 job 信息 + 该 job 下所有 content 条目。"""
        job = self.studio_db.get_job(job_id)
        if not job:
            return {"error": f"job_id={job_id} 不存在"}
        contents = self.studio_db.list_content_by_job(job_id)
        return {"job": job, "contents": contents}

    def list_pending_approvals(self) -> List[dict]:
        """返回所有待人工审核的内容（semi_auto 模式）。"""
        return self.studio_db.list_pending_approval()

    def get_stats(self) -> dict:
        """返回 Content Studio 整体统计数据。"""
        return self.studio_db.get_studio_stats()

    def quick_generate_preview(self, persona_id: str, platform: str) -> dict:
        """
        快速预览：只生成文案/脚本/标签，不生成视频。

        适合在前端实时预览内容方向，速度快、成本低。
        返回: { script, caption, hashtags, visual_prompts, voiceover_text }
        """
        persona_config = self.personas.get(persona_id, {})
        studio_cfg = self.config.get("studio", {})
        cta_link = studio_cfg.get("cta", {}).get("primary_link", "")

        try:
            from .content_agent import generate_content
            content_pkg = generate_content(
                persona_config=persona_config,
                platform=platform,
                content_type="text",          # text 模式：只生成文案
                cta_link=cta_link,
                llm_api_key=self.llm_key,
            )
            return {
                "persona_id": persona_id,
                "platform": platform,
                "script": content_pkg.get("script", ""),
                "caption": content_pkg.get("caption", ""),
                "hashtags": content_pkg.get("hashtags", []),
                "visual_prompts": content_pkg.get("visual_prompts", []),
                "voiceover_text": content_pkg.get("voiceover_text", ""),
                "generated_at": _now_utc(),
            }
        except Exception as exc:
            logger.error("快速预览失败 persona=%s platform=%s: %s", persona_id, platform, exc)
            return {"error": str(exc), "persona_id": persona_id, "platform": platform}

    # -----------------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------------

    def _resolve_content_type(self, persona_id: str) -> str:
        """从 persona 配置或全局配置中解析 content_type。"""
        persona = self.personas.get(persona_id, {})
        # persona 可以显式指定自己的内容风格
        persona_type = persona.get("content_type") or persona.get("default_style")
        if persona_type:
            return persona_type
        return self.default_content_type

    def list_jobs(self, limit: int = 20) -> List[dict]:
        """列出最近的任务（供 API 使用）。"""
        return self.studio_db.list_jobs(limit=limit)

    def list_personas(self) -> List[dict]:
        """返回所有人设配置列表。"""
        result = []
        for pid, cfg in self.personas.items():
            item = {"persona_id": pid}
            item.update(cfg)
            result.append(item)
        return result

    def get_config(self) -> dict:
        """返回当前 studio 配置（脱敏 API Key）。"""
        import os
        cfg = dict(self.config.get("studio", {}))
        # 脱敏 API Keys
        api_keys = dict(cfg.get("api_keys", {}))
        for k in api_keys:
            if api_keys[k]:
                api_keys[k] = api_keys[k][:4] + "****"
        cfg["api_keys"] = api_keys
        # 注入策略状态：has_serper 标志（前端据此显示"已配置"提示）
        strategy = dict(cfg.get("strategy", {}))
        strategy["has_serper"] = bool(
            os.environ.get("SERPER_API_KEY")
            or strategy.get("serper_api_key")
            or strategy.get("serper_api_key_set")
        )
        cfg["strategy"] = strategy
        return cfg

    def update_config(self, updates: dict) -> dict:
        """
        更新运行时配置并持久化到 YAML 文件。

        支持字段: mode, active_persona, enabled_platforms, cta_link
        """
        studio_cfg = self.config.setdefault("studio", {})
        publishing = studio_cfg.setdefault("publishing", {})
        cta = studio_cfg.setdefault("cta", {})

        if "mode" in updates and updates["mode"]:
            studio_cfg["mode"] = updates["mode"]
            self.default_mode = updates["mode"]

        if "active_persona" in updates and updates["active_persona"]:
            studio_cfg["active_persona"] = updates["active_persona"]

        if "enabled_platforms" in updates and updates["enabled_platforms"]:
            publishing["enabled_platforms"] = updates["enabled_platforms"]

        if "cta_link" in updates and updates["cta_link"]:
            cta["primary_link"] = updates["cta_link"]

        if "strategy" in updates and updates["strategy"]:
            strategy = studio_cfg.setdefault("strategy", {})
            strategy.update(updates["strategy"])

        if "serper_api_key" in updates and updates["serper_api_key"]:
            import os
            os.environ["SERPER_API_KEY"] = updates["serper_api_key"]
            # 写入 strategy 配置（脱敏存储前4位）
            strategy = studio_cfg.setdefault("strategy", {})
            strategy["serper_api_key_set"] = True
            strategy["serper_api_key_hint"] = updates["serper_api_key"][:4] + "****"

        # 持久化到磁盘
        if YAML_AVAILABLE:
            try:
                with open(self._config_path, "w", encoding="utf-8") as f:
                    yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)
                logger.info("配置已保存到 %s", self._config_path)
            except Exception as e:
                logger.error("保存配置失败: %s", e)

        return self.get_config()

    # -----------------------------------------------------------------------
    # 定时发布调度循环
    # -----------------------------------------------------------------------

    def _run_schedule_loop(self) -> None:
        """后台线程：每60秒检查一次到期的定时发布内容并自动触发发布。"""
        import time
        logger.info("定时发布调度线程启动")
        while True:
            try:
                due_items = self.studio_db.list_due_scheduled_content()
                for item in due_items:
                    content_id = item.get("id")
                    platform   = item.get("platform", "unknown")
                    persona_id = item.get("persona_id", "")
                    logger.info("定时发布触发 content_id=%s platform=%s", content_id, platform)
                    try:
                        result = self.publish_content(content_id)
                        if result.get("success"):
                            logger.info("定时发布成功 content_id=%s", content_id)
                            # 发送发布成功通知
                            from .studio_notifier import notify_published
                            notify_published(platform, persona_id, item)
                        else:
                            logger.warning("定时发布失败 content_id=%s: %s",
                                           content_id, result.get("error"))
                    except Exception as pub_err:
                        logger.error("定时发布异常 content_id=%s: %s", content_id, pub_err)
            except Exception as e:
                logger.error("定时发布调度循环异常: %s", e)
            time.sleep(60)  # 每分钟检查一次

    def schedule_publish(self, content_id: int, publish_at_utc: str) -> dict:
        """
        设置内容的定时发布时间。

        :param content_id:     studio_content 的 id
        :param publish_at_utc: UTC ISO 时间字符串，如 "2026-04-12T14:00:00+00:00"
        :return: {"success": True, "scheduled_for": publish_at_utc}
        """
        try:
            self.studio_db.schedule_content(content_id, publish_at_utc)
            logger.info("内容已设置定时发布 content_id=%d at=%s", content_id, publish_at_utc)
            return {"success": True, "content_id": content_id, "scheduled_for": publish_at_utc}
        except Exception as e:
            logger.error("设置定时发布失败 content_id=%d: %s", content_id, e)
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_manager_instance: Optional[StudioManager] = None
_manager_lock = threading.Lock()


def get_studio_manager() -> StudioManager:
    """获取 StudioManager 单例，线程安全懒加载。"""
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                _manager_instance = StudioManager()
    return _manager_instance
