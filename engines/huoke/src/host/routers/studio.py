# -*- coding: utf-8 -*-
"""
内容工作室 API 路由 — /studio/*

全部端点:
  POST /studio/jobs              — 创建新的内容生成任务
  GET  /studio/jobs              — 列出所有任务
  GET  /studio/jobs/{job_id}     — 获取任务详情和进度
  GET  /studio/pending           — 待审核内容列表 (半自动模式)
  POST /studio/approve/{id}      — 审核通过并发布
  POST /studio/reject/{id}       — 拒绝重新生成
  POST /studio/publish/{id}      — 直接发布指定内容
  GET  /studio/stats             — 统计数据
  GET  /studio/personas          — 列出所有人设配置
  POST /studio/preview           — 快速预览 (只生成文案，不生成视频)
  GET  /studio/config            — 获取当前配置
  POST /studio/config            — 更新配置 (切换模式/人设/目标国家)
"""

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.host.device_registry import config_file, data_dir

_STUDIO_DATA = data_dir() / "studio"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/studio", tags=["studio"])


# ---------------------------------------------------------------------------
# Pydantic 请求模型
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    """创建内容生成任务的请求体。"""
    persona_id: str
    platforms: List[str] = ["tiktok", "instagram", "telegram", "facebook", "twitter", "linkedin", "whatsapp"]
    content_type: Optional[str] = None   # video / slideshow / text
    mode: Optional[str] = None           # full_auto / semi_auto
    content_brief: Optional[dict] = None  # ContentBrief.to_dict()


class StoryboardRequest(BaseModel):
    """故事板预览请求（花钱前先看计划）。"""
    brief: dict
    persona_id: str
    platform: str = "tiktok"


class UserBriefRequest(BaseModel):
    """用户自然语言描述 → ContentBrief 结构化。"""
    description: str
    persona_id: str
    platform: str = "tiktok"
    tone: str = "energetic"
    framework_id: Optional[str] = None


class ApproveRequest(BaseModel):
    """审核通过请求，可指定发布设备。"""
    device_id: Optional[str] = None


class RejectRequest(BaseModel):
    """拒绝内容请求，附带拒绝原因。"""
    reason: str = "内容不符合要求"


class ScheduleRequest(BaseModel):
    """定时发布请求。publish_at_utc 为 None 时取消排期。"""
    publish_at_utc: Optional[str] = None   # UTC ISO 时间字符串；None = 取消定时
    device_id: Optional[str] = None


class UpdateConfigRequest(BaseModel):
    """更新工作室配置请求体。"""
    mode: Optional[str] = None               # full_auto / semi_auto
    active_persona: Optional[str] = None     # personas.yaml 中的 key
    enabled_platforms: Optional[List[str]] = None
    cta_link: Optional[str] = None           # 引流链接
    strategy: Optional[dict] = None          # 内容策略 {competitors, daily_posts, preferred_hour}
    serper_api_key: Optional[str] = None     # Serper 趋势注入 key（写入环境变量）


class PreviewRequest(BaseModel):
    """快速预览请求（只生成文案，不生成视频）。"""
    persona_id: str
    platform: str = "tiktok"


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _get_manager():
    """获取 StudioManager 单例，不可用时抛 503。"""
    try:
        from ...studio.studio_manager import get_studio_manager
        return get_studio_manager()
    except Exception as exc:
        logger.error("StudioManager 初始化失败: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"Content Studio 服务不可用: {exc}",
        )


# ---------------------------------------------------------------------------
# 任务管理端点
# ---------------------------------------------------------------------------

@router.post("/jobs", status_code=202)
def create_job(req: CreateJobRequest):
    """
    创建新的内容生成任务。

    - 立即返回 job_id，实际生成在后台线程中进行
    - 通过 GET /studio/jobs/{job_id} 轮询进度
    - content_type: video / slideshow / text（不填则从人设配置自动选）
    - mode: full_auto(生成后自动发布) / semi_auto(等待人工审核)
    """
    manager = _get_manager()

    # 校验平台列表非空
    if not req.platforms:
        raise HTTPException(status_code=400, detail="platforms 不能为空")

    # 校验 content_type
    if req.content_type and req.content_type not in ("video", "slideshow", "text"):
        raise HTTPException(status_code=400, detail="content_type 必须是 video / slideshow / text")

    # 校验 mode
    if req.mode and req.mode not in ("full_auto", "semi_auto"):
        raise HTTPException(status_code=400, detail="mode 必须是 full_auto / semi_auto")

    job_id = manager.run_job(
        persona_id=req.persona_id,
        platforms=req.platforms,
        content_type=req.content_type,
        mode=req.mode,
        content_brief=req.content_brief,
    )
    return {"job_id": job_id, "status": "accepted", "message": "内容生成任务已提交，正在后台运行"}


@router.get("/jobs")
def list_jobs(limit: int = 20):
    """
    列出最近的内容生成任务（按创建时间倒序）。

    - limit: 返回条数上限，默认 20
    """
    manager = _get_manager()
    jobs = manager.list_jobs(limit=limit)
    return {"jobs": jobs, "total": len(jobs)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    """
    获取指定任务的详情和各平台内容进度。

    返回:
    - job: 任务基本信息（状态/人设/平台/时间）
    - contents: 各平台内容条目（脚本/视频路径/发布状态）
    """
    manager = _get_manager()
    result = manager.get_job_status(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# 审核端点（semi_auto 模式专用）
# ---------------------------------------------------------------------------

@router.get("/pending")
def list_pending():
    """
    返回所有等待人工审核的内容列表（semi_auto 模式专用）。

    内容状态为 ready 且 approved=0 时出现在此列表。
    """
    manager = _get_manager()
    items = manager.list_pending_approvals()
    return {"pending": items, "total": len(items)}


@router.post("/approve/{content_id}")
def approve_content(content_id: int, req: ApproveRequest):
    """
    审核通过指定内容并立即触发发布。

    - content_id: studio_content 表的主键 ID
    - device_id: 可选，指定执行发布的 ADB 设备序列号
    """
    manager = _get_manager()
    result = manager.approve_and_publish(content_id, device_id=req.device_id)

    if not result.get("success"):
        # 发布失败时返回 500，但保留错误详情
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "发布失败，请查看日志"),
        )
    return result


@router.post("/reject/{content_id}")
def reject_content(content_id: int, req: RejectRequest):
    """
    拒绝指定内容，标记为 failed 并记录拒绝原因。

    被拒绝的内容不会自动重新生成，需手动创建新任务。
    """
    manager = _get_manager()
    try:
        manager.studio_db.reject_content(content_id, req.reason)
        return {"success": True, "content_id": content_id, "reason": req.reason}
    except Exception as exc:
        logger.error("拒绝内容失败 content_id=%d: %s", content_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# 直接发布端点
# ---------------------------------------------------------------------------

@router.post("/publish/{content_id}")
def publish_content(content_id: int, device_id: Optional[str] = None):
    """
    直接发布指定内容（跳过审核流程）。

    - content_id: studio_content 表的主键 ID
    - device_id: 查询参数，指定发布设备（不填则自动选择）
    """
    manager = _get_manager()
    result = manager.publish_content(content_id, device_id=device_id)

    if not result.get("success"):
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "发布失败，请查看日志"),
        )
    return result


# ---------------------------------------------------------------------------
# 统计端点
# ---------------------------------------------------------------------------

@router.get("/stats")
def get_stats():
    """
    获取 Content Studio 整体统计数据。

    包含: 任务状态分布、内容状态分布、发布帖子总数、互动汇总、活跃人设排行。
    """
    manager = _get_manager()
    return manager.get_stats()


# ---------------------------------------------------------------------------
# 人设端点
# ---------------------------------------------------------------------------

@router.get("/personas")
def list_personas():
    """
    返回所有已配置的人设配置（来自 config/personas.yaml）。

    返回 dict 格式: {persona_id: config, ...}，方便前端按 id 查找。
    每个人设包含: 名称/领域/内容风格/目标国家/CTA策略等信息。
    """
    manager = _get_manager()
    personas_list = manager.list_personas()
    # 转换为 dict 格式（keyed by persona_id）
    personas_dict = {}
    for p in personas_list:
        pid = p.get("persona_id", "")
        if pid:
            personas_dict[pid] = {k: v for k, v in p.items() if k != "persona_id"}
    return {"personas": personas_dict, "total": len(personas_dict)}


@router.get("/platforms")
def list_platforms():
    """返回所有已支持的发布平台列表。"""
    from src.studio.publishers import list_platforms as _lp
    platforms = _lp()
    platform_info = {
        "tiktok":      {"name": "TikTok",      "icon": "🎵", "type": "video"},
        "instagram":   {"name": "Instagram",   "icon": "📸", "type": "reels"},
        "telegram":    {"name": "Telegram",    "icon": "✈️",  "type": "channel"},
        "twitter":     {"name": "X (Twitter)", "icon": "🐦", "type": "video"},
        "facebook":    {"name": "Facebook",    "icon": "👥", "type": "reels"},
        "linkedin":    {"name": "LinkedIn",    "icon": "💼", "type": "video"},
        "whatsapp":    {"name": "WhatsApp",    "icon": "💬", "type": "status"},
        "xiaohongshu": {"name": "小红书",       "icon": "📕", "type": "slideshow"},
    }
    result = []
    for p in platforms:
        info = platform_info.get(p, {"name": p.title(), "icon": "🌐", "type": "video"})
        result.append({"id": p, **info})
    return {"platforms": result, "total": len(result)}


# ---------------------------------------------------------------------------
# 快速预览端点
# ---------------------------------------------------------------------------

@router.post("/preview")
def quick_preview(req: PreviewRequest):
    """
    快速生成内容预览（只生成文案，不生成视频）。

    适合在前端实时预览脚本/文案/标签，响应速度快，不消耗 fal.ai 配额。
    返回: script / caption / hashtags / visual_prompts / voiceover_text
    """
    manager = _get_manager()
    result = manager.quick_generate_preview(
        persona_id=req.persona_id,
        platform=req.platform,
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# 配置管理端点
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config():
    """
    获取当前工作室配置（API Key 已脱敏）。

    包含: 发布模式、激活人设、启用平台、CTA引流链接、生成参数等。
    返回格式: {"config": {...}} 方便前端统一处理。
    """
    manager = _get_manager()
    cfg = manager.get_config()
    # 展开常用字段到顶层，方便前端访问
    cfg.setdefault("enabled_platforms", (cfg.get("publishing") or {}).get("enabled_platforms", []))
    return {"config": cfg}


@router.post("/config")
def update_config(req: UpdateConfigRequest):
    """
    更新工作室配置，同时写入 YAML 文件和更新内存配置。

    可更新字段:
    - mode: 切换 full_auto / semi_auto
    - active_persona: 切换活跃人设
    - enabled_platforms: 更新启用平台列表
    - cta_link: 更新主引流链接

    注意: API Key 不可通过此接口修改，请直接编辑环境变量或配置文件。
    """
    manager = _get_manager()

    # 校验 mode
    if req.mode and req.mode not in ("full_auto", "semi_auto"):
        raise HTTPException(status_code=400, detail="mode 必须是 full_auto / semi_auto")

    # Serper API Key：写入运行时环境变量（本进程生效，重启后需重新设置）
    if req.serper_api_key:
        import os
        os.environ["SERPER_API_KEY"] = req.serper_api_key
        logger.info("SERPER_API_KEY 已更新（运行时）")

    updated = manager.update_config(req.model_dump(exclude_none=True))
    return {"success": True, "config": updated}


# ---------------------------------------------------------------------------
# 定时发布端点
# ---------------------------------------------------------------------------

@router.post("/schedule/{content_id}")
def schedule_content(content_id: int, req: ScheduleRequest):
    """
    设置内容的定时发布时间（而非立即发布）。

    - content_id: studio_content 表的主键 ID
    - publish_at_utc: UTC ISO 时间字符串，如 "2026-04-12T20:00:00+00:00"

    设置后系统将在到达时间时自动触发发布，无需再次调用 approve。
    """
    manager = _get_manager()
    if req.publish_at_utc is None:
        # 取消定时：直接写库
        try:
            from ...studio.studio_db import schedule_content as _sched_db
            _sched_db(content_id, None)
            return {"success": True, "message": "定时已取消"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    result = manager.schedule_publish(content_id, req.publish_at_utc)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "设置失败"))
    return result


# ---------------------------------------------------------------------------
# 媒体文件服务端点
# ---------------------------------------------------------------------------

@router.get("/media/{file_path:path}")
def serve_studio_media(file_path: str):
    """
    提供 data/studio/ 目录下生成的媒体文件（图片/视频/音频）。

    前端用于内容审核时的图片预览。
    安全限制：只允许访问 data/studio/ 目录内的文件。
    """
    # 安全检查：防止路径穿越
    try:
        target = (_STUDIO_DATA / file_path).resolve()
        _STUDIO_DATA.resolve()
        target.relative_to(_STUDIO_DATA.resolve())
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=403, detail="禁止访问该路径")

    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(str(target))


# ---------------------------------------------------------------------------
# 内容建议引擎端点
# ---------------------------------------------------------------------------

@router.get("/suggestions")
def get_suggestions(persona_id: Optional[str] = None, n: int = 3, platform: Optional[str] = None):
    """
    获取今日内容建议卡片列表（系统主动推荐，无需用户描述）。

    - persona_id: 指定人设，默认使用配置中的 active_persona
    - n: 建议数量，默认 3
    - platform: 过滤特定平台的框架（可选）
    """
    try:
        from ...studio.studio_advisor import get_daily_suggestions
        manager = _get_manager()

        active_persona = persona_id or manager.config.get("studio", {}).get("active_persona", "")
        persona_config = manager.personas.get(active_persona, {})
        if not persona_config:
            # 取第一个人设作为默认
            persona_config = next(iter(manager.personas.values()), {})
            active_persona = persona_config.get("persona_id", "default")

        target_platforms = [platform] if platform else None
        suggestions = get_daily_suggestions(
            persona_config=persona_config,
            persona_id=active_persona,
            n=n,
            target_platforms=target_platforms,
        )
        return {"suggestions": suggestions, "persona_id": active_persona, "total": len(suggestions)}
    except Exception as exc:
        logger.error("建议生成失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/suggestions/all-personas")
def get_all_personas_suggestions(n: int = 2):
    """
    批量返回所有激活人设的今日建议（运营大盘用）。
    每个人设返回 n 条建议。
    """
    manager = _get_manager()
    try:
        from ...studio.studio_advisor import StudioAdvisor
        advisor = StudioAdvisor()
        result = {}
        for persona_id, persona_config in manager.personas.items():
            try:
                sugs = advisor.get_daily_suggestions(
                    persona_config=persona_config,
                    persona_id=persona_id,
                    n=n,
                )
                result[persona_id] = {
                    "persona_name": persona_config.get("display_name", persona_id),
                    "suggestions": sugs,
                }
            except Exception as e:
                result[persona_id] = {"persona_name": persona_id, "suggestions": [], "error": str(e)}
        return {"personas": result, "total_personas": len(result)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/storyboard")
def generate_storyboard(req: StoryboardRequest):
    """
    根据 ContentBrief 生成可视化故事板（脚本预览）。
    零成本操作 — 只生成文字，不生成图片/视频。
    用户确认后再调用 POST /studio/jobs 触发实际生成。
    """
    try:
        from ...studio.studio_advisor import get_storyboard
        manager = _get_manager()
        persona_config = manager.personas.get(req.persona_id, {})
        if not persona_config:
            persona_config = {"persona_id": req.persona_id}

        storyboard = get_storyboard(
            brief_dict=req.brief,
            persona_config=persona_config,
            platform=req.platform,
        )
        return storyboard
    except Exception as exc:
        logger.error("故事板生成失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/brief-from-text")
def brief_from_text(req: UserBriefRequest):
    """
    将用户自然语言描述转为结构化 ContentBrief。
    无需 LLM Key，基于关键词匹配实现。
    """
    try:
        from ...studio.studio_advisor import get_advisor
        manager = _get_manager()
        persona_config = manager.personas.get(req.persona_id, {})
        advisor = get_advisor()
        brief = advisor.build_brief_from_user_input(
            user_description=req.description,
            persona_config=persona_config,
            platform=req.platform,
            tone=req.tone,
            framework_id=req.framework_id,
        )
        return {"brief": brief.to_dict(), "framework_id": brief.framework_id}
    except Exception as exc:
        logger.error("Brief 生成失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/frameworks")
def get_frameworks():
    """返回所有内容框架库（20 种爆款结构）。"""
    try:
        from ...studio.studio_advisor import get_frameworks
        return {"frameworks": get_frameworks(), "total": 20}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# SSE 实时任务进度端点
# ---------------------------------------------------------------------------

from starlette.responses import StreamingResponse
import asyncio
import json as _json

@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str):
    """
    SSE 端点：实时推送任务进度事件。

    前端用 EventSource('/studio/jobs/{job_id}/stream') 订阅。
    事件类型: progress (进度更新) | done (完成) | error (失败)
    自动在 done/error 时关闭流。
    超时限制：5分钟后强制关闭。
    """
    manager = _get_manager()

    async def event_generator():
        import time
        start = time.time()
        last_status = None
        last_content_count = 0
        timeout = 300  # 5分钟超时

        while time.time() - start < timeout:
            try:
                raw = manager.get_job_status(job_id)
                # get_job_status 返回 {"job": {...}, "contents": [...]} 或 {"error": "..."}
                if not raw or "error" in raw:
                    yield f"event: error\ndata: {_json.dumps({'message': raw.get('error', '任务不存在') if raw else '任务不存在'})}\n\n"
                    return

                job      = raw.get("job") or {}
                status   = job.get("status", "unknown")
                contents = raw.get("contents", [])
                n_done   = len([c for c in contents if c.get("status") in ("ready","approved","published","failed")])
                n_total  = len(contents)

                # 只在状态变化时推送，减少噪音
                if status != last_status or n_done != last_content_count:
                    last_status        = status
                    last_content_count = n_done

                    payload = {
                        "job_id":   job_id,
                        "status":   status,
                        "progress": round(n_done / max(n_total, 1) * 100),
                        "n_done":   n_done,
                        "n_total":  n_total,
                        "elapsed":  round(time.time() - start),
                    }

                    if status == "generating":
                        yield f"event: progress\ndata: {_json.dumps(payload)}\n\n"
                    elif status in ("ready", "done", "completed"):
                        payload["contents"] = contents
                        yield f"event: done\ndata: {_json.dumps(payload)}\n\n"
                        return
                    elif status == "failed":
                        payload["error"] = job.get("error", "生成失败")
                        yield f"event: error\ndata: {_json.dumps(payload)}\n\n"
                        return

                # 心跳包（每15秒），防止连接超时
                yield f": heartbeat\n\n"
                await asyncio.sleep(2)

            except Exception as exc:
                yield f"event: error\ndata: {_json.dumps({'message': str(exc)})}\n\n"
                return

        yield f"event: error\ndata: {_json.dumps({'message': 'SSE 超时（5分钟）'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# 框架性能数据端点
# ---------------------------------------------------------------------------

@router.get("/framework-perf")
def get_framework_perf():
    """
    返回各框架的历史性能数据（审核通过率、发布次数）。
    用于前端在框架库中显示真实效果数据。
    """
    try:
        from ...studio.studio_db import get_framework_perf as _get_perf
        return {"perf": _get_perf(), "success": True}
    except Exception as exc:
        logger.warning("get_framework_perf 失败: %s", exc)
        return {"perf": {}, "success": False}


# ---------------------------------------------------------------------------
# 就绪度检查端点
# ---------------------------------------------------------------------------

@router.get("/readiness")
def check_readiness():
    """
    检查 Content Studio 运行所需的所有条件是否满足。
    前端在进入 Studio 时调用，引导用户完成配置。

    就绪等级:
    - ready: 全部满足，可完整运行
    - partial: 核心功能可用，但部分高级功能受限
    - not_ready: 缺少关键配置，无法生成内容
    """
    import os
    import urllib.request

    manager = _get_manager()
    cfg = manager.config.get("studio", {})
    strategy = cfg.get("strategy", {})

    # ── 检查各项 ──────────────────────────────────────────────────
    checks = {}

    # 1. FAL_KEY（图片/视频生成）
    fal_key = os.environ.get("FAL_KEY") or _read_key_file("fal_key.txt")
    checks["fal_key"] = {
        "ok": bool(fal_key),
        "label": "FAL API Key（图片/视频生成）",
        "hint": "在 .env 中设置 FAL_KEY=xxx，或将 key 写入 config/fal_key.txt",
        "blocking": True,   # 缺少则无法生成视觉内容
    }

    # 2. LLM Key（AI文案）
    llm_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    checks["llm_key"] = {
        "ok": bool(llm_key),
        "label": "LLM API Key（AI文案生成）",
        "hint": "设置 OPENAI_API_KEY 或 ANTHROPIC_API_KEY；未配置时走模板生成，质量较低",
        "blocking": False,  # 有模板回退，不blocking
    }

    # 3. Ollama 本地 LLM（备选）
    ollama_ok = False
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            ollama_ok = r.status == 200
    except Exception:
        pass
    checks["ollama"] = {
        "ok": ollama_ok,
        "label": "Ollama 本地 LLM（离线AI文案）",
        "hint": "安装 Ollama 并运行 `ollama pull qwen2.5:3b` 启用免费本地LLM",
        "blocking": False,
    }

    # 4. 激活人设
    active_persona = cfg.get("active_persona", "")
    persona_exists = active_persona and active_persona in manager.personas
    checks["active_persona"] = {
        "ok": bool(persona_exists),
        "label": "激活人设",
        "hint": "在「人设配置」标签页选择并激活一个人设，或在设置中指定 active_persona",
        "blocking": True,
    }

    # 5. CTA 引流链接
    cta_link = cfg.get("cta", {}).get("primary_link", "")
    checks["cta_link"] = {
        "ok": bool(cta_link and cta_link not in ("t.me/yourchannel", "")),
        "label": "CTA 引流链接",
        "hint": "在「设置」→「CTA引流链接」填写你的 Telegram/WhatsApp 链接",
        "blocking": False,
    }

    # 6. 发布设备在线（ADB）
    try:
        from ...device_control.device_manager import get_device_manager
        dm = get_device_manager()
        online_devices = [d for d in (dm.get_all_devices() if hasattr(dm, 'get_all_devices') else []) if getattr(d, 'online', False)]
        devices_ok = len(online_devices) > 0
        device_count = len(online_devices)
    except Exception:
        devices_ok = False
        device_count = 0
    checks["adb_device"] = {
        "ok": devices_ok,
        "label": f"ADB 发布设备（{device_count} 台在线）",
        "hint": "连接 Android 设备并开启 USB 调试，或配置 Worker 节点",
        "blocking": False,  # 半自动模式可先生成再手动发布
    }

    # 7. Serper 趋势注入（可选）
    serper_ok = bool(os.environ.get("SERPER_API_KEY") or strategy.get("serper_api_key_set"))
    checks["serper_key"] = {
        "ok": serper_ok,
        "label": "Serper API Key（趋势注入）",
        "hint": "在「设置」→「内容策略」配置 Serper key，启用实时趋势话题注入",
        "blocking": False,
    }

    # ── 计算整体就绪级别 ────────────────────────────────────────
    blocking_failed = [k for k, v in checks.items() if v.get("blocking") and not v["ok"]]
    all_ok = all(v["ok"] for v in checks.values())
    level = "ready" if all_ok else ("not_ready" if blocking_failed else "partial")

    return {
        "level": level,
        "checks": checks,
        "blocking_failed": blocking_failed,
        "summary": {
            "total": len(checks),
            "passed": sum(1 for v in checks.values() if v["ok"]),
            "failed": sum(1 for v in checks.values() if not v["ok"]),
        }
    }


def _read_key_file(filename: str) -> str:
    """尝试从 config/ 目录读取 key 文件。"""
    try:
        key_path = config_file(filename)
        if key_path.exists():
            return key_path.read_text().strip()
    except Exception:
        pass
    return ""


@router.get("/account-health")
def get_account_health():
    """返回所有账号的健康状态（发布成功率、连续失败次数、是否暂停）。"""
    try:
        from ...studio.account_health import get_health_monitor
        monitor = get_health_monitor()
        accounts = monitor.get_all_health()
        paused = [a for a in accounts if a["status"] in ("paused", "blocked")]
        return {
            "accounts": accounts,
            "summary": {
                "total": len(accounts),
                "paused": len(paused),
                "alerts": [a for acc in accounts for a in acc.get("alerts", [])],
            }
        }
    except Exception as exc:
        return {"accounts": [], "summary": {}, "error": str(exc)}


@router.get("/competitor-analysis")
def get_competitor_analysis():
    """手动触发一次竞品分析，并返回结果。"""
    manager = _get_manager()
    try:
        from ...studio.competitor_analyzer import run_competitor_analysis
        strategy = manager.config.get("studio", {}).get("strategy", {})
        results = run_competitor_analysis(manager.personas, strategy)
        return {"results": results, "total": len(results), "success": True}
    except Exception as exc:
        logger.warning("竞品分析失败: %s", exc)
        return {"results": [], "error": str(exc), "success": False}


@router.get("/post-stats")
def get_post_stats(days: int = 7):
    """查询最近N天已发布帖子的效果数据。"""
    from ...studio.studio_db import list_published_posts, get_top_performing_frameworks
    posts = list_published_posts(days=days)
    top_frameworks = get_top_performing_frameworks(limit=5)
    total_likes = sum(p.get("likes", 0) or 0 for p in posts)
    total_comments = sum(p.get("comments", 0) or 0 for p in posts)
    total_shares = sum(p.get("shares", 0) or 0 for p in posts)
    return {
        "days": days,
        "post_count": len(posts),
        "total_likes": total_likes,
        "total_comments": total_comments,
        "total_shares": total_shares,
        "top_frameworks": top_frameworks,
        "posts": posts[:20],  # 前端只展示前20条
    }


@router.post("/update-post-stats/{post_id}")
def update_single_post_stats(post_id: int, views: int = 0, likes: int = 0,
                              comments: int = 0, shares: int = 0):
    """手动更新指定帖子的互动数据，并触发框架权重回传。"""
    from ...studio.studio_db import update_post_stats, get_top_performing_frameworks
    update_post_stats(post_id, views, likes, comments, shares)

    # 若效果优异（likes>100），自动提升关联框架的权重
    if likes > 100:
        try:
            from ...studio.competitor_analyzer import _boost_framework_perf
            _boost_framework_perf({"engagement_high": 1}, source="post_feedback")
        except Exception:
            pass

    return {"ok": True, "post_id": post_id, "updated": {"views": views, "likes": likes,
                                                          "comments": comments, "shares": shares}}
