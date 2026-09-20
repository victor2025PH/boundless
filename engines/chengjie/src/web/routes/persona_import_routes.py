# -*- coding: utf-8 -*-
"""「从文档创建人设」API（B 线，2026-07-27）——persona_doc_import 管线的路由层。

Endpoints（全部 ``Depends(auth_dep)``）：
- GET  /api/personas/import-doc/status          — flag 探测（不因 flag 关而 403）
- POST /api/personas/import-doc/parse           — 上传 .docx/.txt（multipart）或 JSON
  ``{"text": ...}`` → 提取/清洗文本预览（**不**受 ``doc_import.enabled`` 拦截：
  只抽文本，无 LLM；开关只拦下面的 extract）
- POST /api/personas/import-doc/extract         — 提交抽取任务（后台线程两段 LLM），
  满载 429；返回 ``{ok, job_id}``
- GET  /api/personas/import-doc/jobs/{job_id}   — 任务轮询（status/stage/progress/result）；
  抽取与长传记补向量共用同一注册表，两条线都轮询这里
- POST /api/personas/{profile_id}/bio-doc/reembed — 给缺向量的块/句补嵌（后台任务），
  无库存 409、满载 429；返回 ``{ok, job_id, chunks}``

Feature flag：``personas.doc_import.enabled``（默认关；生产在 config.local.yaml 开）
只拦 LLM ``extract`` / finalize，不拦 ``parse``（docx→文本）。
LLM 经 ``AIClient.rewrite_cloud``（system+user → 原始回复的低层工具口，不走
generate_reply 全链）在请求所在事件循环上 marshalling——任务线程经
``run_coroutine_threadsafe`` 把协程投回 web loop，与 autosend_helpers 同口径，
避免跨 loop 使用生产 AsyncOpenAI 客户端。测试可注入 ``app.state.persona_doc_chat_fn``。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import Depends, HTTPException, Request

from src.web.web_i18n import tr

try:
    from src.utils.persona_import_stats import record_import_event  # 第二波落地
except Exception:
    def record_import_event(*a, **k):
        pass

logger = logging.getLogger("ai_chat_assistant.persona_import_routes")

_ROLE_VIEWER = "viewer"
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_ALLOWED_SUFFIXES = (".docx", ".txt")


def _spawn_extract_watch(job_id: str) -> None:
    """旁路观察线程：轮询任务注册表，把抽取终态记进导入观测漏斗。

    为什么不改走 ``create_job_runner`` 包 runner 记录：``pdi.create_job`` 是既有
    对外提交入口与**可测缝**（tests/test_persona_doc_import.py 以 monkeypatch 它
    模拟满载 429），必须保持其提交地位；观测改经旁路线程读同一注册表快照
    （``get_job``），对提交/满载/审计语义零侵入。耗时口径＝提交→观察到 done
    （含队列等待与 ≤0.15s 轮询步长，均值观测足够）。任务被 TTL 清理 /
    测试 ``reset_jobs`` → 静默退出不记。
    """
    t0 = time.time()

    def _watch() -> None:
        from src.utils import persona_doc_import as pdi

        deadline = t0 + float(getattr(pdi, "JOB_TTL_SEC", 1800)) + 60.0
        while time.time() < deadline:
            try:
                job = pdi.get_job(job_id)
            except Exception:
                return
            if job is None:                      # TTL 清理/注册表被重置 → 不记
                return
            status = job.get("status")
            if status in ("done", "error"):
                try:
                    if status == "done":
                        result = job.get("result") or {}
                        comp = ((result.get("completeness") or {}).get("score"))
                        record_import_event(
                            "extract_done",
                            duration_ms=(time.time() - t0) * 1000.0,
                            completeness=comp
                            if isinstance(comp, (int, float)) and comp >= 0
                            else None)
                    else:
                        record_import_event("extract_error")
                except Exception:
                    pass
                return
            time.sleep(0.15)

    threading.Thread(target=_watch, name=f"persona-import-watch-{job_id}",
                     daemon=True).start()


def register_persona_import_routes(app, auth_dep, audit_store=None,
                                   config_manager=None):
    """Register document-to-persona import endpoints."""

    def _live_config(request: Request) -> dict:
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        return getattr(cm, "config", None) or {}

    def _flag_enabled(request: Request) -> bool:
        cfg = _live_config(request)
        sect = (cfg.get("personas") or {}).get("doc_import") or {}
        return bool(sect.get("enabled", False))

    def _require_enabled(request: Request) -> None:
        if not _flag_enabled(request):
            raise HTTPException(403, tr(request, "err.psn.doc_import_off"))

    def _check_write_role(request: Request) -> None:
        """Raises 403 if session role is viewer (read-only)。与 persona_routes 同口径。"""
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.persona.readonly_no_edit"))

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
        """构建 ``chat_fn(system, user, timeout) -> str``。

        可测缝：优先取 ``app.state.persona_doc_chat_fn``（测试注入假函数）；
        否则从 skill_manager / telegram_client 拿生产 AIClient，把
        ``rewrite_cloud`` 协程 marshalling 回当前（web）loop 执行。
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

        from src.utils.persona_doc_import import LLM_MAX_TOKENS, LLM_TEMPERATURE
        loop = asyncio.get_running_loop()

        def chat_fn(system: str, user: str, timeout: float) -> str:
            fut = asyncio.run_coroutine_threadsafe(
                client.rewrite_cloud(
                    system, user, timeout_sec=float(timeout),
                    max_tokens=LLM_MAX_TOKENS, temperature=LLM_TEMPERATURE),
                loop)
            try:
                return fut.result(max(5.0, float(timeout)) + 30.0) or ""
            except Exception as exc:
                logger.warning("persona doc-import LLM 调用失败: %s", exc)
                try:
                    fut.cancel()
                except Exception:
                    pass
                return ""

        return chat_fn

    @app.get("/api/personas/import-doc/status")
    async def api_persona_doc_import_status(request: Request,
                                            _=Depends(auth_dep)):
        """Flag 探测：LLM 提取是否开启。不因 flag 关而 403；前端用它决定能否走 AI 提取，不藏导入入口。"""
        return {"ok": True, "enabled": _flag_enabled(request)}

    @app.post("/api/personas/import-doc/parse")
    async def api_persona_doc_import_parse(request: Request,
                                           _=Depends(auth_dep)):
        """上传 .docx/.txt（multipart ``file``）或 JSON ``{"text"}`` → 文本预览。

        不受 ``personas.doc_import.enabled`` 拦截：这是抽文本，不是 LLM。
        """
        from src.utils import persona_doc_import as pdi

        was_docx = False
        ctype = (request.headers.get("content-type") or "").lower()
        if "multipart/form-data" in ctype:
            form = await request.form()
            up = form.get("file")
            filename = str(getattr(up, "filename", "") or "").strip().lower()
            if up is None or not filename:
                raise HTTPException(400, tr(request, "err.psn.doc_empty"))
            if not filename.endswith(_ALLOWED_SUFFIXES):
                raise HTTPException(400, tr(request, "err.psn.doc_bad_type"))
            data = await up.read()
            if len(data) > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    413, tr(request, "err.psn.doc_too_large", limit="5MB"))
            if filename.endswith(".docx"):
                was_docx = True
                raw_text = pdi.extract_docx_text(data)
            else:
                raw_text = data.decode("utf-8", errors="replace")
        else:
            try:
                body = await request.json()
            except Exception:
                body = {}
            raw_text = str((body or {}).get("text") or "")

        text, truncated = pdi.prepare_text(raw_text)
        if not text:
            raise HTTPException(400, tr(request, "err.psn.doc_empty"))
        record_import_event("parse")
        if was_docx:
            record_import_event("parse_docx")
        return {
            "ok": True, "text": text, "text_chars": len(text),
            "paragraphs": pdi.count_paragraphs(text), "truncated": truncated,
        }

    @app.post("/api/personas/import-doc/extract")
    async def api_persona_doc_import_extract(request: Request,
                                             _=Depends(auth_dep)):
        """提交后台抽取任务：两段 LLM（身份与性格 / 传记与记忆）→ 轮询取结果。"""
        from src.utils import persona_doc_import as pdi

        _require_enabled(request)
        _check_write_role(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_text = str((body or {}).get("text") or "")
        if len(raw_text) > pdi.MAX_TEXT_CHARS:
            raise HTTPException(
                413, tr(request, "err.psn.doc_too_large",
                        limit=f"{pdi.MAX_TEXT_CHARS} chars"))
        text, _truncated = pdi.prepare_text(raw_text)
        if not text:
            raise HTTPException(400, tr(request, "err.psn.doc_empty"))

        chat_fn = _build_chat_fn(request)
        job_id = pdi.create_job(text, chat_fn)
        if not job_id:
            raise HTTPException(429, tr(request, "err.psn.jobs_busy"))
        record_import_event("extract_start")
        _spawn_extract_watch(job_id)     # 终态（done/error+耗时/完整度）旁路观测
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_import_extract",
                                f"chars={len(text)}")
            except Exception:
                pass
        return {"ok": True, "job_id": job_id}

    @app.get("/api/personas/import-doc/jobs/{job_id}")
    async def api_persona_doc_import_job(request: Request, job_id: str,
                                         _=Depends(auth_dep)):
        """任务轮询：{id, status(queued|running|done|error), stage, progress, result, error}。"""
        from src.utils import persona_doc_import as pdi

        job = pdi.get_job(job_id)
        if job is None:
            raise HTTPException(404, tr(request, "err.psn.job_not_found"))
        return {"ok": True, "job": job}

    # ── 长传记检索层（D2 线）：原始文档全文分块入库，聊天被追问时按需注入 ──

    @app.post("/api/personas/{profile_id}/bio-doc")
    async def api_persona_bio_doc_stash(request: Request, profile_id: str,
                                        _=Depends(auth_dep)):
        """原始文档全文入库（整体替换旧块）。

        导入向导先入库后保存人设（时序上 bio 可先到）→ **不要求 profile 已存在**。
        """
        from src.companion.persona_bio_store import MAX_BIO_CHARS, replace_bio_doc

        _require_enabled(request)
        _check_write_role(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_text = str((body or {}).get("text") or "")
        if len(raw_text) > MAX_BIO_CHARS:
            raise HTTPException(
                413, tr(request, "err.psn.bio_too_large",
                        limit=f"{MAX_BIO_CHARS} chars"))
        if not raw_text.strip():
            raise HTTPException(400, tr(request, "err.psn.bio_empty"))

        # to_thread：全量嵌入（真实 33k 文档 ≈ 183 块 + 数百句 ≈ 2 分钟）曾直接
        # 跑在 web loop 上——期间全站请求冻结。挪进线程后本请求仍要等
        # （浏览器超时自负），但站点其余部分不再陪绑；大文档请改走 finalize
        # 流水线（后台任务 + 轮询）。
        res = await asyncio.to_thread(replace_bio_doc, profile_id, raw_text)
        if not res:
            return {"ok": False, "chunks": 0, "chars": 0}
        if audit_store:
            try:
                audit_store.log(
                    _actor(request), "profile_bio_stash",
                    f"{profile_id} chunks={res.get('chunks')} chars={res.get('chars')}")
            except Exception:
                pass
        record_import_event("bio_stash")
        return {"ok": True, "chunks": int(res.get("chunks") or 0),
                "chars": int(res.get("chars") or 0)}

    @app.get("/api/personas/{profile_id}/bio-doc")
    async def api_persona_bio_doc_meta(request: Request, profile_id: str,
                                       _=Depends(auth_dep)):
        """传记库存元数据：{chunks, chars, updated_ts, preview} 或 null（无库存）。"""
        from src.companion.persona_bio_store import get_bio_meta

        _require_enabled(request)
        return {"ok": True, "meta": get_bio_meta(profile_id)}

    @app.post("/api/personas/{profile_id}/bio-doc/search")
    async def api_persona_bio_doc_search(request: Request, profile_id: str,
                                         _=Depends(auth_dep)):
        """检索自测（I1 线）：给一句「客户可能问的问题」，看命中哪些语义单元、
        实际会注入什么文本——运营验货用，**只读**（与 GET bio-doc 同权限口径）。

        入参 ``{"query": ..., "top_k": 1..5}``；返回命中单元（含 kw/sem 分项）、
        真实注入块、库存单元数与检索观测计数。无库存不报错，回空结果。
        """
        from src.companion.persona_bio_store import (
            build_bio_block, explain_query_aliases, get_bio_meta,
            retrieval_stats_snapshot, search_bio,
        )

        _require_enabled(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        query = str((body or {}).get("query") or "").strip()
        if not query:
            raise HTTPException(400, tr(request, "err.psn.bio_query_empty"))
        try:
            top_k = int((body or {}).get("top_k") or 3)
        except (TypeError, ValueError):
            top_k = 3
        top_k = max(1, min(5, top_k))

        chunks = int(((get_bio_meta(profile_id) or {}).get("chunks")) or 0)
        if chunks <= 0:
            return {"ok": True, "hits": [], "block": None, "chunks": 0,
                    "stats": retrieval_stats_snapshot()}

        hits = search_bio(profile_id, query, top_k=top_k)
        # hits 复用给 build_bio_block：同一次自测只应计一次检索观测
        block = build_bio_block(profile_id, query, hits=hits) if hits else None
        return {
            "ok": True,
            "chunks": chunks,
            "hits": [{
                "idx": int(h.get("idx") or 0),
                "score": h.get("score"),
                "kw": int(h.get("kw") or 0),
                "sem": round(float(h.get("sem") or 0.0), 4),
                "text": str(h.get("text") or "")[:300],
            } for h in hits],
            "block": block,
            # 别名可见性（M9 收尾）：这条 query 触发了哪些别名键、值里哪些
            # 真命中了返回块——「为什么这句搜得到/搜不到」从翻代码变成看面板
            "aliases": explain_query_aliases(
                profile_id, query, [h.get("text") or "" for h in hits]),
            "stats": retrieval_stats_snapshot(),
        }

    @app.post("/api/personas/import-doc/finalize")
    async def api_persona_doc_import_finalize(request: Request,
                                              _=Depends(auth_dep)):
        """一键完成上线尾巴：建档（同步）→ 传记入库 → 考题验收（后台任务）。

        向导的人工审核闸**保留在前**（本端点在运营看完抽取字段点「保存并验收」
        时才调用）；账号绑定**刻意不在**流水线里——那是运营决策
        （2026-07-28 误删事故的边界教训）。设计要点：
        - 档案保存在请求内同步做（毫秒级、且避免 PersonaManager 从任务线程写）；
          传记嵌入（分钟级）与逐题问 LLM（分钟级）进后台任务，轮询走既有
          ``GET /api/personas/import-doc/jobs/{job_id}``。
        - **每阶段结果如实分报**：bio 失败不掩盖档案已存，quiz 异常不吞掉
          bio 已入库——result 里 ``bio``/``quiz``/``quiz_error`` 各说各话。
        - quiz flag 关 / 调用方 ``quiz:false`` → 跳过验收阶段（``quiz: null``），
          不算失败。判分裁判与考题路由同口径（``llm_judge`` 开则复用 chat_fn）。
        Body: ``{profile_id, persona?, merge=true, bio_text="", quiz=true, quiz_n=10}``
        → ``{ok, job_id, profile_id, merged}``。``persona`` 可省：向导的常规保存链
        已经落库档案时（保存成功 → 自动入库传记的既有时序），本端点只补
        「传记入库 + 考题」两段——此时要求档案已存在（否则 404，防瞎考空档案）。
        """
        from src.companion.persona_bio_store import MAX_BIO_CHARS, replace_bio_doc
        from src.utils import persona_doc_import as pdi
        from src.utils import persona_quiz as pq

        _require_enabled(request)
        _check_write_role(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        profile_id = str((body or {}).get("profile_id") or "").strip()
        persona_data = (body or {}).get("persona")
        has_persona = isinstance(persona_data, dict) and bool(persona_data)
        if not profile_id or (persona_data is not None and not has_persona):
            raise HTTPException(400, tr(request, "err.psn.finalize_bad_body"))
        bio_text = str((body or {}).get("bio_text") or "")
        if len(bio_text) > MAX_BIO_CHARS:
            raise HTTPException(
                413, tr(request, "err.psn.bio_too_large",
                        limit=f"{MAX_BIO_CHARS} chars"))
        want_quiz = bool((body or {}).get("quiz", True))
        try:
            quiz_n = max(3, min(20, int((body or {}).get("quiz_n") or 10)))
        except (TypeError, ValueError):
            quiz_n = 10

        # ── 同步段：建档（merge 语义与 PUT profiles/{id} 逐字同款；persona 缺省
        #    = 档案须已由常规保存链落库）────────────────────────────────────
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        did_merge = False
        if has_persona:
            store_data = persona_data
            if bool((body or {}).get("merge", True)):
                existing = pm.get_persona_by_id(profile_id)
                if existing:
                    store_data = pm.deep_merge_profile(existing, persona_data)
                    did_merge = True
            pm.upsert_profile(profile_id, store_data)
            try:
                cm = (getattr(request.app.state, "config_manager", None)
                      or config_manager)
                pm.persist_profiles(cm)
            except Exception:
                pass
        saved_persona = pm.get_persona_by_id(profile_id)
        if saved_persona is None:
            raise HTTPException(
                404, tr(request, "err.persona.profile_not_found",
                        name=profile_id))

        # quiz 依赖 web loop 上的 chat_fn，必须在请求内构建；flag 关则整段跳过
        quiz_cfg = ((_live_config(request).get("personas") or {})
                    .get("quiz") or {})
        quiz_on = want_quiz and bool(quiz_cfg.get("enabled", False))
        chat_fn = _build_chat_fn(request) if quiz_on else None
        judge_fn = chat_fn if (quiz_on and pq.llm_judge_enabled(
            _live_config(request))) else None

        def _runner(on_stage):
            out = {"profile_id": profile_id, "merged": did_merge,
                   "bio": None, "quiz": None}
            if bio_text.strip():
                on_stage("bio_stash", 5)
                res = replace_bio_doc(profile_id, bio_text)
                if res:
                    out["bio"] = {"chunks": int(res.get("chunks") or 0),
                                  "chars": int(res.get("chars") or 0)}
                    try:
                        record_import_event("bio_stash")
                    except Exception:
                        pass
            if quiz_on and chat_fn is not None:
                def _qstage(stage, pct):
                    try:
                        on_stage(stage, 40 + int(float(pct) * 0.55))
                    except Exception:
                        pass
                try:
                    result = pq.run_quiz(saved_persona, chat_fn, n=quiz_n,
                                         on_stage=_qstage, judge_fn=judge_fn)
                    out["quiz"] = {k: result.get(k) for k in
                                   ("score", "passed", "total", "judged")}
                    try:
                        record_import_event("quiz_done",
                                            score=result.get("score"))
                    except Exception:
                        pass
                    try:
                        from src.utils import persona_quiz_store as pqs
                        if pqs.save_report(profile_id, result) is not None:
                            record_import_event("quiz_saved")
                    except Exception:
                        logger.debug("finalize quiz save_report 失败",
                                     exc_info=True)
                except Exception as exc:   # noqa: BLE001 — 尾部失败不掩盖头部成功
                    out["quiz_error"] = str(exc)[:200]
            on_stage("finalize", 100)
            return out

        job_id = pdi.create_job_runner(_runner)
        if not job_id:
            raise HTTPException(429, tr(request, "err.psn.jobs_busy"))
        if quiz_on:
            try:
                record_import_event("quiz_run")
            except Exception:
                pass
        if audit_store:
            try:
                audit_store.log(
                    _actor(request), "profile_finalize",
                    f"{profile_id} bio_chars={len(bio_text)} "
                    f"quiz={'on' if quiz_on else 'off'} merged={did_merge}")
            except Exception:
                pass
        return {"ok": True, "job_id": job_id, "profile_id": profile_id,
                "merged": did_merge}

    @app.get("/api/personas/bio-inventory")
    async def api_persona_bio_inventory(request: Request, _=Depends(auth_dep)):
        """传记库存盘点（只读）：每份库存 × 档案在否 × 绑定账号，一眼对齐三维。

        2026-07-28 两次踩坑各缺一维：把 pid=mizuki 的在用库存当孤儿删掉
        （没看绑定），又平行建出第二套档案（没看已有库存）。库存/档案/绑定
        三张表各说各话时，错位只能靠翻库发现——这里给一张对齐视图，
        ``orphans``（有库存无档案）单列供 Studio 顶部横幅直接消费。
        """
        from src.companion.persona_bio_store import get_bio_meta, list_bio_personas
        from src.integrations.account_registry import persona_binding_refs
        from src.utils.persona_manager import PersonaManager

        _require_enabled(request)
        pm = PersonaManager.get_instance()
        rows = []
        for pid in list_bio_personas():
            meta = get_bio_meta(pid) or {}
            rows.append({
                "persona_id": pid,
                "chunks": int(meta.get("chunks") or 0),
                "chars": int(meta.get("chars") or 0),
                "emb_chunks": meta.get("emb_chunks"),
                "sents": meta.get("sents"),
                "alias_keys": meta.get("alias_keys"),
                "has_profile": pm.get_persona_by_id(pid) is not None,
                "bound_accounts": persona_binding_refs(pid),
            })
        return {"ok": True, "rows": rows,
                "orphans": [r["persona_id"] for r in rows
                            if not r["has_profile"]]}

    @app.post("/api/personas/{profile_id}/bio-doc/reembed")
    async def api_persona_bio_doc_reembed(request: Request, profile_id: str,
                                          _=Depends(auth_dep)):
        """补齐缺失的块/句向量（J1 线运维口）：返回 ``{ok, job_id, chunks}``。

        **为什么走后台任务而不是同步返回**：``reembed_bio_doc`` 是同步阻塞调用，
        对每个缺向量的单元/句子各打一次嵌入端点（真实文档 183 单元 + 数百句）
        → 在 ``async def`` 里直接调会把整个 web loop 按住数分钟，全站坐席一起卡；
        HTTP 层也过不了反代/浏览器超时。故复用 ``persona_doc_import``
        的通用任务注册表（``create_job_runner``，与考题线同口径），
        轮询走既有 ``GET /api/personas/import-doc/jobs/{job_id}``（同一注册表，
        不另立端点）。任务终态：``result={profile_id,chunks,updated,sents_added}``。

        两种「补不了」如实分流，前端文案可区分：
        - **无库存**（该人设根本没入过传记）→ 提交前 ``get_bio_meta`` 判死，409；
        - **嵌入端点不可用**（``reembed_bio_doc`` 返 None 而库存还在）→ 任务
          ``status=error`` + ``error="embed_unavailable"``（机器码，前端映射文案），
          **绝不**报成功。库存在提交后被删的竞态记 ``error="bio_missing"``。
        """
        from src.companion.persona_bio_store import get_bio_meta, reembed_bio_doc
        from src.utils import persona_doc_import as pdi

        _require_enabled(request)
        _check_write_role(request)
        chunks = int(((get_bio_meta(profile_id) or {}).get("chunks")) or 0)
        if chunks <= 0:
            raise HTTPException(409, tr(request, "err.psn.bio_no_stock"))

        def _reembed_runner(on_stage) -> dict:
            on_stage("embedding", 10)
            res = reembed_bio_doc(profile_id)
            if res is None:
                # 库存还在 → 只可能是没有可用 embed_fn（端点未配/不可达）
                raise RuntimeError(
                    "bio_missing" if get_bio_meta(profile_id) is None
                    else "embed_unavailable")
            on_stage("finalize", 100)
            return {
                "profile_id": profile_id,
                "chunks": int(res.get("chunks") or 0),
                "updated": int(res.get("updated") or 0),
                "sents_added": int(res.get("sents_added") or 0),
            }

        job_id = pdi.create_job_runner(_reembed_runner)
        if not job_id:
            raise HTTPException(429, tr(request, "err.psn.jobs_busy"))
        record_import_event("bio_reembed")
        if audit_store:
            try:
                audit_store.log(_actor(request), "profile_bio_reembed",
                                f"{profile_id} chunks={chunks}")
            except Exception:
                pass
        return {"ok": True, "job_id": job_id, "chunks": chunks}

    @app.delete("/api/personas/{profile_id}/bio-doc")
    async def api_persona_bio_doc_delete(request: Request, profile_id: str,
                                         _=Depends(auth_dep)):
        """删除该人设的传记分块库存。

        **绑定护栏**（2026-07-28 误删实锤）：该 pid 被账号显式绑定时删库存 =
        在用账号的传记注入静默消失，而删除者往往不知道有人在用——「无档案/
        无审计」都不是「没人用」的证据，绑定表才是。有绑定 → 409 列明账号，
        ``?force=1`` 显式越过（运维明知故删的口子）。registry 不可用 fail-open。
        """
        from src.companion.persona_bio_store import delete_bio_doc
        from src.integrations.account_registry import persona_binding_refs

        _require_enabled(request)
        _check_write_role(request)
        force = str(request.query_params.get("force") or "").lower() in (
            "1", "true", "yes")
        refs = [] if force else persona_binding_refs(profile_id)
        if refs:
            raise HTTPException(
                409, tr(request, "err.psn.bio_bound",
                        accounts=", ".join(refs[:5]), n=len(refs)))
        deleted = delete_bio_doc(profile_id)
        if deleted and audit_store:
            try:
                audit_store.log(_actor(request), "profile_bio_delete",
                                str(profile_id))
            except Exception:
                pass
        return {"ok": True, "deleted": bool(deleted)}
