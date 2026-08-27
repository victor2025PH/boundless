# -*- coding: utf-8 -*-
"""人设一致性考题 API（E 线 + H3 持久化，2026-07-27）——persona_quiz 管线的路由层。

Endpoints（全部 ``Depends(auth_dep)``）：
- GET  /api/personas/quiz/status                       — flag 探测（不因 flag 关而 403）
- POST /api/personas/{profile_id}/quiz                 — 提交考题任务（后台线程逐题问
  LLM），body 可选 ``{"n": 3..20}``；满载 429；返回 ``{ok, job_id}``
- GET  /api/personas/{profile_id}/quiz/jobs/{job_id}   — 任务轮询（status/stage/progress/result）
- GET  /api/personas/{profile_id}/quiz/reports         — 历史报告列表 + summary
- GET  /api/personas/{profile_id}/quiz/reports/{id}    — 单份完整报告
- GET  /api/personas/quiz/trend                        — 跨进程趋势（flag 关也 200，
  ops 卡据 ``enabled`` 显隐；见 I3）

Feature flag：``personas.quiz.enabled``（默认关；生产在 config.local.yaml 开）。
LLM 经 ``AIClient.rewrite_cloud`` 在 web loop 上 marshalling（与 persona_import_routes
同口径）；测试可注入 ``app.state.persona_doc_chat_fn``。任务注册表复用
``persona_doc_import`` 的通用入口 ``create_job_runner``（D1 契约：
``create_job_runner(runner) -> job_id | None``，``runner(on_stage) -> result dict``）
+ ``get_job`` 轮询。考题成功后经 ``persona_quiz_store.save_report`` 落库（软失败）。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

try:
    from src.utils.persona_import_stats import record_import_event
except Exception:  # pragma: no cover — 观测模块由第二波观测代理落地，缺席时零依赖
    def record_import_event(event: str) -> None:  # type: ignore[misc]
        return None

logger = logging.getLogger("ai_chat_assistant.persona_quiz_routes")

_N_MIN, _N_MAX, _N_DEFAULT = 3, 20, 10
_ANSWER_TRUNCATE = 200  # list 端点截断 items.answer，防响应过大


def _clamp_n(raw) -> int:
    """body.n → 3..20（非法/缺省 → 10）。"""
    try:
        n = int(raw)
    except Exception:
        return _N_DEFAULT
    return max(_N_MIN, min(_N_MAX, n))


def _truncate_report_answers(report: dict, max_chars: int = _ANSWER_TRUNCATE) -> dict:
    """浅拷贝报告并把 items[].answer 截到 max_chars（list 端点用）。"""
    out = dict(report or {})
    items = out.get("items")
    if not isinstance(items, list):
        return out
    clipped = []
    for it in items:
        if not isinstance(it, dict):
            clipped.append(it)
            continue
        row = dict(it)
        ans = row.get("answer")
        if isinstance(ans, str) and len(ans) > max_chars:
            row["answer"] = ans[:max_chars]
        clipped.append(row)
    out["items"] = clipped
    return out


def _create_job_runner(runner):
    """经 persona_doc_import 通用任务注册表提交后台任务（D1 契约，lazy 取用）。

    ``create_job_runner(runner) -> job_id | None``（满载 None → 调用方转 429）；
    ``runner(on_stage) -> result dict``，``on_stage(stage: str, progress: int)``。
    本文件编写时该入口可能尚未合入——lazy 属性访问使路由注册/status 探测不受影响，
    仅真正提交考题任务时才需要它（集成负责人以真实现终验）。
    """
    from src.utils import persona_doc_import as pdi

    return pdi.create_job_runner(runner)


def register_persona_quiz_routes(app, auth_dep, audit_store=None,
                                 config_manager=None):
    """Register persona consistency quiz endpoints."""

    def _live_config(request: Request) -> dict:
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        return getattr(cm, "config", None) or {}

    def _flag_enabled(request: Request) -> bool:
        sect = (_live_config(request).get("personas") or {}).get("quiz") or {}
        return bool(sect.get("enabled", False))

    def _require_enabled(request: Request) -> None:
        if not _flag_enabled(request):
            raise HTTPException(403, tr(request, "err.psn.quiz_off"))

    def _actor(request: Request) -> str:
        try:
            # 生产 session 键是 username（登录只写它，见 auth_user_routes:137）；
            # 旧 "user" 键从不存在 → 审计行全记成 "api"（2026-08-18 goal_routes
            # 同病实锤后连带修复）。保留 "user" 回落兼容测试桩。
            return (request.session.get("username")
                    or request.session.get("user", "") or "api")
        except Exception:
            return "api"

    def _build_chat_fn(request: Request):
        """构建 ``chat_fn(system, user, timeout) -> str``（persona_import_routes 同款）。

        可测缝：优先取 ``app.state.persona_doc_chat_fn``（测试注入假函数）；否则从
        skill_manager / telegram_client 拿生产 AIClient，把 ``rewrite_cloud`` 协程
        marshalling 回当前（web）loop 执行——任务线程经 run_coroutine_threadsafe 投递，
        避免跨 loop 使用生产 AsyncOpenAI 客户端。
        """
        injected = getattr(request.app.state, "persona_doc_chat_fn", None)
        if callable(injected):
            return injected

        from src.web.web_context import resolve_skill_manager
        tg = getattr(request.app.state, "telegram_client", None)
        sm = resolve_skill_manager(tg, request.app)
        client = getattr(sm, "ai_client", None) if sm is not None else None
        if client is None and tg is not None:
            client = getattr(tg, "ai_client", None)
        if client is None or not hasattr(client, "rewrite_cloud"):
            raise HTTPException(503, tr(request, "err.psn.ai_unavailable"))

        from src.utils.persona_quiz import (
            QUIZ_LLM_MAX_TOKENS,
            QUIZ_LLM_TEMPERATURE,
        )
        loop = asyncio.get_running_loop()

        def chat_fn(system: str, user: str, timeout: float) -> str:
            fut = asyncio.run_coroutine_threadsafe(
                client.rewrite_cloud(
                    system, user, timeout_sec=float(timeout),
                    max_tokens=QUIZ_LLM_MAX_TOKENS,
                    temperature=QUIZ_LLM_TEMPERATURE),
                loop)
            try:
                return fut.result(max(5.0, float(timeout)) + 30.0) or ""
            except Exception as exc:
                logger.warning("persona quiz LLM 调用失败: %s", exc)
                try:
                    fut.cancel()
                except Exception:
                    pass
                return ""

        return chat_fn

    @app.get("/api/personas/quiz/status")
    async def api_persona_quiz_status(request: Request, _=Depends(auth_dep)):
        """Flag 探测端点：不因 flag 关而 403，前端据此显隐入口。"""
        return {"ok": True, "enabled": _flag_enabled(request)}

    @app.get("/api/personas/quiz/trend")
    async def api_persona_quiz_trend(request: Request, _=Depends(auth_dep)):
        """跨进程考题趋势：每人设读数 + 近 N 天均分（读 quiz.db，重启不清零）。

        与 status 同款「flag 关也 200」——ops 卡靠 ``enabled`` 决定显隐，403 会让
        整卡退化成报错。读权限口径同 ``quiz/reports``（``Depends(auth_dep)``）。
        """
        from src.utils import persona_quiz_store as pqs

        if not _flag_enabled(request):
            return {"ok": True, "enabled": False}
        try:
            days = int(request.query_params.get("days") or pqs.DEFAULT_TREND_DAYS)
        except Exception:
            days = pqs.DEFAULT_TREND_DAYS
        try:
            limit = int(request.query_params.get("limit") or 20)
        except Exception:
            limit = 20
        days = max(1, min(90, days))
        limit = max(1, min(50, limit))
        return {
            "ok": True,
            "enabled": True,
            "overview": pqs.overview(limit_personas=limit),
            "daily": pqs.daily_scores(days=days),
        }

    @app.post("/api/personas/{profile_id}/quiz")
    async def api_persona_quiz_run(profile_id: str, request: Request,
                                   _=Depends(auth_dep)):
        """提交考题任务：档案确定性出题 → 真人设 prompt 逐题问 LLM → 判分报告。"""
        from src.utils import persona_quiz as pq

        _require_enabled(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        n = _clamp_n((body or {}).get("n", _N_DEFAULT))

        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(profile_id)
        if persona is None:
            raise HTTPException(
                404, tr(request, "err.persona.profile_not_found", name=profile_id))

        quiz = pq.build_quiz(persona, n=n)
        if not quiz:
            raise HTTPException(400, tr(request, "err.psn.quiz_not_enough"))

        chat_fn = _build_chat_fn(request)
        # 裁判兜底与 nightly 同口径（scripts/persona_quiz_nightly.py）：flag 开 →
        # 复用同一 chat_fn 复核「关键词判错」的题（fail-closed，只能把错改对）。
        # 修复 2026-07-28：此前 web 链漏传 judge_fn → `llm_judge: true` 配了也
        # 不生效（验收实锤：「酒店运营总监助理」类长期望词假阴性没人救）。
        judge_fn = chat_fn if pq.llm_judge_enabled(_live_config(request)) else None

        def _quiz_runner(on_stage):
            result = pq.run_quiz(persona, chat_fn, n=n, on_stage=on_stage,
                                 judge_fn=judge_fn)
            try:      # 观测 best-effort：考卷成绩绝不因埋点失败而丢
                record_import_event("quiz_done", score=result.get("score"))
            except Exception:
                pass
            try:      # H3：完整答卷落库（软失败，不影响 job result 返回）
                from src.utils import persona_quiz_store as pqs
                rid = pqs.save_report(profile_id, result)
                if rid is not None:
                    try:
                        record_import_event("quiz_saved")
                    except Exception:
                        pass
            except Exception:
                logger.debug("persona quiz save_report 失败", exc_info=True)
            return result

        job_id = _create_job_runner(_quiz_runner)
        if not job_id:
            raise HTTPException(429, tr(request, "err.psn.jobs_busy"))
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_quiz_run",
                                f"profile={profile_id} n={n} questions={len(quiz)}")
            except Exception:
                pass
        try:
            record_import_event("quiz_run")
        except Exception:
            pass
        return {"ok": True, "job_id": job_id}

    @app.post("/api/personas/{profile_id}/retired-quiz")
    async def api_persona_retired_quiz_run(profile_id: str, request: Request,
                                           _=Depends(auth_dep)):
        """撤销设定行为验证（P3 期，2026-08-04）：挑衅题 × 守卫判分。

        确定性验证（retire-verify）只证「prompt 里没有旧设定」；本任务补行为层：
        真实人设 prompt（含撤销钉子）跑通用日常题（「在干嘛」——原始事故触发面）
        + 假记忆攻击题（「我记得你上次说过你的X」——生产实录攻击面），答案交给
        **出站守卫同一判定器**打分。不及格≠客户可见（生产有 sanitize 兜底剥离），
        而是「钉子没压住模型」的早期信号。轮询复用 ``quiz/jobs/{job_id}`` 端点。
        """
        from src.utils import persona_quiz as pq

        _require_enabled(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(profile_id)
        if persona is None:
            raise HTTPException(
                404, tr(request, "err.persona.profile_not_found", name=profile_id))
        if not pq.build_retired_quiz(persona):
            raise HTTPException(400, tr(request, "err.persona.no_retired_terms"))

        chat_fn = _build_chat_fn(request)

        def _retired_runner(on_stage):
            return pq.run_retired_quiz(persona, chat_fn, on_stage=on_stage)

        job_id = _create_job_runner(_retired_runner)
        if not job_id:
            raise HTTPException(429, tr(request, "err.psn.jobs_busy"))
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_retired_quiz_run",
                                f"profile={profile_id}")
            except Exception:
                pass
        return {"ok": True, "job_id": job_id}

    @app.get("/api/personas/{profile_id}/quiz/jobs/{job_id}")
    async def api_persona_quiz_job(profile_id: str, job_id: str, request: Request,
                                   _=Depends(auth_dep)):
        """任务轮询：{id, status(queued|running|done|error), stage, progress, result, error}。"""
        from src.utils import persona_doc_import as pdi

        job = pdi.get_job(job_id)
        if job is None:
            raise HTTPException(404, tr(request, "err.psn.job_not_found"))
        return {"ok": True, "job": job}

    @app.get("/api/personas/{profile_id}/quiz/reports")
    async def api_persona_quiz_reports(profile_id: str, request: Request,
                                       _=Depends(auth_dep)):
        """历史报告列表（新到旧）+ summary；items.answer 截到 200 字。"""
        from src.utils import persona_quiz_store as pqs

        _require_enabled(request)
        try:
            lim = int(request.query_params.get("limit") or 10)
        except Exception:
            lim = 10
        lim = max(1, min(50, lim))
        reports = [
            _truncate_report_answers(r) for r in pqs.list_reports(profile_id, limit=lim)
        ]
        return {"ok": True, "reports": reports, "summary": pqs.summary(profile_id)}

    @app.get("/api/personas/{profile_id}/quiz/reports/{report_id}")
    async def api_persona_quiz_report_one(profile_id: str, report_id: int,
                                          request: Request, _=Depends(auth_dep)):
        """单份完整报告（含完整 answer）。"""
        from src.utils import persona_quiz_store as pqs

        _require_enabled(request)
        report = pqs.get_report(profile_id, report_id)
        if report is None:
            raise HTTPException(404, tr(request, "err.psn.quiz_report_not_found"))
        return {"ok": True, "report": report}
