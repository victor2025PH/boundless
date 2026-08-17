"""统一收件箱——翻译路由域（巨石拆分 slice 31 + slice 35，slice 40 合并）。

``register_translate_routes(app, *, api_auth)`` 挂载全部翻译端点：

- ``unified-inbox/translate``：通用文本翻译（含 ``target_lang:"auto"``）
- ``unified-inbox/translation-engines``：目标语引擎能力矩阵
- ``unified-inbox/translate-image`` / ``translate-voice`` / ``translate-message-media``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Depends, HTTPException, Request

from src.ai.translation_service import normalize_lang
from src.utils.agent_char_usage import check_request_quota, record_request_chars
from src.web.web_i18n import tr
from src.web.routes.unified_inbox_auth import _session_agent
from src.web.routes.unified_inbox_services import (
    _DEFAULT_LANG_KEY,
    _REPLY_LANG_KEY,
    _conv_id,
    _get_translation_service,
    _inbox_store,
    _list_default_langs,
    _resolve_conv_engine,
    _resolve_conv_language,
    _resolve_default_lang,
    _resolve_default_reply_lang,
)

logger = logging.getLogger(__name__)

# ── P1（2026-08-16）：坐席级「我的语言」偏好 ─────────────────────────────────
# 前端 _agentLang() 的服务端层（解析序：本偏好 > UI 语言 en > 浏览器语言 > zh）。
# 身份取会话 session（_session_agent；无 SessionMiddleware 回落 "agent"＝单坐席
# 部署共享一份，语义正确）。键：inbox.agent_lang.{agent_id}；lang="" = 清除。
# 语种白名单与前端翻译目标集（_XL_TARGETS）同源，防任意串进 KV。
_AGENT_LANG_KEY = "inbox.agent_lang"
_AGENT_LANG_ALLOWED = {"zh", "en", "th", "vi", "id", "ja", "ko", "ru", "es", "pt"}


def _perm_ok(request: Request, perm: str) -> bool:
    """按登录坐席判能力权限（P2 管理面改造：perms_json 按人覆写，master 恒 True）。

    懒 import：``resolve_user_perm`` 由并行批次在 web_user_store 落地，模块未就绪
    （ImportError）/ user_store 未暴露 / 未登录（token 链）/ 任何异常 → **一律放行**
    （fail-open：权限守卫绝不能因装配时序把翻译/发送主链打挂）。
    """
    try:
        from src.utils.web_user_store import resolve_user_perm
        us = getattr(request.app.state, "user_store", None)
        sess = request.session
        uname = str(sess.get("username") or "")
        role = str(sess.get("role") or "")
        if us is None or not uname:
            return True
        return resolve_user_perm(us, uname, role, perm)
    except Exception:
        return True


def _deny_capability(request: Request, perm: str) -> None:
    """能力未授予 → 403（i18n 文案）；放行则无副作用。"""
    if not _perm_ok(request, perm):
        raise HTTPException(403, tr(request, "err.perm.capability_denied"))


def _enforce_agent_quota(request: Request) -> None:
    """坐席字符额度硬闸（enforce 开才可能拦；fail-open 语义在 check_request_quota 内）。"""
    q = check_request_quota(request)
    if not q["allowed"]:
        raise HTTPException(403, tr(
            request, "err.quota.agent_chars_exhausted",
            used=q["used"], quota=q["quota"]))


async def _do_document_translation(
    *, xlate, data: bytes, kind: str, target_lang: str, source_lang: str,
    style: str, engine: str, base: str, progress=None,
) -> dict:
    """L2/L2b/L2c：按 kind 分派文档翻译，统一产出端点响应 dict。

    ``.docx/.xlsx`` → 存令牌存储返回 ``download_url``；``.pdf`` → 返回 ``text``。
    ``progress(done,total)`` 透传给底层翻译（供 SSE 进度）。同步路径传 None。
    """
    from src.ai import document_file_translate as dft

    if kind == "pdf":
        result = await dft.translate_pdf_to_text(
            data, xlate=xlate, target_lang=target_lang,
            source_lang=source_lang, style=style, engine=engine, progress=progress)
        if not result.get("ok"):
            return result
        return {"ok": True, "kind": "text", "filename": f"{base}.{target_lang}.txt",
                "text": result.get("text", ""), "stats": result.get("stats", {})}

    fn = dft.translate_docx if kind == "docx" else dft.translate_xlsx
    result = await fn(
        data, xlate=xlate, target_lang=target_lang,
        source_lang=source_lang, style=style, engine=engine, progress=progress)
    if not result.get("ok"):
        return {k: v for k, v in result.items() if k != "data"}
    # L2c-1：译后二进制存临时令牌存储 → 返回短链，避免 JSON 塞大 base64（内存翻倍）
    out_name = f"{base}.{target_lang}.{kind}"
    ctype = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if kind == "docx"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    from src.web.translated_file_store import get_translated_file_store
    token = get_translated_file_store().put(result["data"], out_name, ctype)
    return {
        "ok": True,
        "kind": "file",
        "filename": out_name,
        "download_url": f"/api/unified-inbox/translated-file/{token}",
        "stats": result.get("stats", {}),
    }


def _media_base_dirs(request: Request) -> list:
    """媒体解析白名单根目录（config.media.base_dirs）。仅在白名单内的文件可被读取。"""
    cm = getattr(request.app.state, "config_manager", None)
    try:
        full = getattr(cm, "config", None) or {}
        dirs = list((full.get("media") or {}).get("base_dirs") or [])
    except Exception:
        dirs = []
    return [str(d) for d in dirs if str(d or "").strip()]


def _remote_fetch_cfg(request: Request) -> dict:
    """config.media.remote_fetch（受控远程媒体下载，默认关）。"""
    cm = getattr(request.app.state, "config_manager", None)
    try:
        full = getattr(cm, "config", None) or {}
        return dict((full.get("media") or {}).get("remote_fetch") or {})
    except Exception:
        return {}


def _within_base_dirs(path: str, base_dirs: list) -> bool:
    """容纳检查：resolved 真实路径必须落在某个白名单根内（防路径穿越）。
    未配置白名单时放行（media_ref 来自我方 store/平台，非终端用户输入）。"""
    if not base_dirs:
        return True
    try:
        rp = os.path.realpath(path)
        for b in base_dirs:
            br = os.path.realpath(str(b))
            if rp == br or rp.startswith(br + os.sep):
                return True
    except Exception:
        return False
    return False


def _lookup_stored_media(request: Request, conversation_id: str, message_id: str):
    """从 store 按 message_id 取该消息的 (media_type, media_ref)。取不到返回 ('','')。"""
    store = _inbox_store(request)
    if store is None or not conversation_id:
        return "", ""
    try:
        rows = store.list_messages(conversation_id, limit=500)
    except Exception:
        return "", ""
    mid = str(message_id or "")
    for r in rows:
        if mid and str(r.get("platform_msg_id") or "") == mid:
            return str(r.get("media_type") or ""), str(r.get("media_ref") or "")
    return "", ""


def register_translate_routes(app, *, api_auth) -> None:
    """挂载全部翻译端点（文本 + 媒体集群）。"""

    @app.post("/api/unified-inbox/translate")
    async def api_unified_inbox_translate(request: Request, _=Depends(api_auth)):
        """通用翻译。

        P1-2（翻译单一真相源）：``target_lang`` 支持 ``"auto"``，由服务端用与 ``/send``
        完全相同的 ``_resolve_conv_language`` + ``normalize_lang`` 解析客户语言，
        消除「预览在前端解析 vs 一击在后端解析」的 drift。需随 body 传 platform/
        account_id/chat_key 以定位会话。返回 ``resolved_target`` 告知实际目标语；
        ``"auto"`` 无法解析（客户语言 unknown）时返回 resolved_target="" 且不翻译，
        前端据此回落「按原文发送」。
        """
        # 能力权限 + 坐席额度闸（2026-08-16）：手动翻译主入口，拦在 svc.translate
        # 之前——额度耗尽还烧一次引擎纯属浪费；translate-batch（视口懒翻）/后台
        # enrich 刻意不闸（那些不是坐席主动消费，闸了=「看历史消息」也会被拒）。
        _deny_capability(request, "ai.translate")
        _enforce_agent_quota(request)
        body = await request.json()
        text = str(body.get("text") or "")
        target_lang = str(body.get("target_lang") or "zh").strip()
        source_lang = normalize_lang(str(body.get("source_lang") or ""))
        style = str(body.get("style") or "chat")
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")

        if target_lang.lower() == "auto":
            target_lang = _resolve_conv_language(request, platform, account_id, chat_key)
        else:
            target_lang = normalize_lang(target_lang)

        if not target_lang:
            return {
                "ok": False,
                "resolved_target": "",
                "translation": {
                    "ok": False, "translated_text": text, "original_text": text,
                    "target_lang": "", "source_lang": source_lang or "",
                    "provider": "none", "error": "auto_unresolved",
                },
            }

        # F+：调用方未显式指定引擎时，回落会话首选引擎（多线路对照择优后记住的）
        engine = str(body.get("engine") or "").strip().lower()
        if not engine:
            engine = _resolve_conv_engine(request, platform, account_id, chat_key)

        svc = _get_translation_service(request)
        result = await svc.translate(
            text,
            target_lang=target_lang,
            source_lang=source_lang,
            style=style,
            engine=engine,
        )
        # 坐席字符计量归因（2026-08-16）：只接**手动翻译主入口**——translate-batch
        # （视口懒翻，打开长会话即被动触发）与后台 enrich 刻意不接，那些不是坐席的
        # 主动消费，记进个人额度会把「看历史消息」变成扣费动作。len(text) 与授权池
        # record_license_chars 同口径；开关默认关 / 未登录 / 异常均零副作用。
        if result.ok:
            record_request_chars(request, "translation", len(text))
        # P4-B：入站显示翻译（客户→坐席）按日聚合，供经理看板量化「常驻双语」成本/语言分布。
        # 仅计 purpose=inbound_display；命中翻译记忆缓存的不计（非新增 API 成本，与 record 语义一致）。
        if str(body.get("purpose") or "").strip() == "inbound_display":
            try:
                ibx = _inbox_store(request)
                if ibx is not None and hasattr(ibx, "record_inbound_xlate"):
                    if result.ok and not getattr(result, "cached", False):
                        src = normalize_lang(getattr(result, "source_lang", "") or source_lang or "")
                        ibx.record_inbound_xlate(translated=1, by_lang=({src: 1} if src else None))
                    elif not result.ok:
                        ibx.record_inbound_xlate(failed=1)
            except Exception:
                logger.debug("[translate] 入站翻译漏斗记账失败（忽略）", exc_info=True)
        resp = {
            "ok": result.ok,
            "resolved_target": target_lang,
            "pref_engine": engine,
            "translation": result.to_dict(),
        }
        # P0-XL3（2026-08-16）反译回显：body.back=true 时把译文再译回源语言，
        # 坐席在「先预览再发」里对照确认「发出去的外语到底说了什么」。
        # - 反译目标 = 正向翻译探测出的源语言（服务端单一真相，前端不猜）；
        # - 反译走 failover 缺省序（本机首位是零成本本地 HY-MT），刻意不带会话
        #   首选引擎——校验轨要独立于被校验的那条线才有对照价值；
        # - 刻意不计坐席字符额度（校验辅助不是消费，计了=劝退使用）；
        # - 任何失败只降级 back.ok=false，绝不影响正向翻译结果（fail-open）。
        if bool(body.get("back")) and result.ok and (result.translated_text or "").strip():
            back_target = normalize_lang(
                getattr(result, "source_lang", "") or source_lang or "")
            if back_target and back_target != "unknown" and back_target != target_lang:
                try:
                    bres = await svc.translate(
                        result.translated_text, target_lang=back_target,
                        source_lang=target_lang, style=style,
                    )
                    resp["back"] = {
                        "ok": bool(bres.ok),
                        "text": (bres.translated_text or "") if bres.ok else "",
                        "engine": str(getattr(bres, "provider", "") or ""),
                        "target_lang": back_target,
                    }
                except Exception:
                    resp["back"] = {"ok": False, "text": "", "engine": "",
                                    "target_lang": back_target, "error": "back_failed"}
            else:
                resp["back"] = {"ok": False, "text": "", "engine": "",
                                "target_lang": back_target, "error": "no_source_lang"}
        return resp

    @app.post("/api/unified-inbox/translate-batch")
    async def api_unified_inbox_translate_batch(request: Request, _=Depends(api_auth)):
        """批量翻译（2026-08-09 提速批次）：一次往返译整批消息。

        收件箱「视口懒翻」此前逐条 POST /translate——N 条消息 = N 个 HTTP 往返，
        译文行一条条蹦。本端点把一批收进一个请求：逐条复用与 ``/translate``
        **完全相同**的 TranslationService（L1/L2 缓存、术语 mask、F+ 会话首选
        引擎、置信度评分），语义零分叉；服务端 gather 并发 + 信号量 8 封顶
        （A 线/autosend 出站翻译与本端点共享同一 LAN GPU 引擎，别让一次打开
        长会话的突发塞满推理队列）。

        body：``{items:[{id,text,source_lang?}...], target_lang(支持 auto),
        style?, engine?, platform?, account_id?, chat_key?, purpose?}``。
        条目上限 50，超出部分计入 ``skipped``（调用方下一批再来）；
        ``target_lang:"auto"`` 解析不出 → resolved_target="" 整批不译。
        每条结果独立成败（translation.ok），单条失败不拖垮整批。
        """
        import asyncio as _aio

        body = await request.json()
        raw_items = [it for it in (body.get("items") or []) if isinstance(it, dict)]
        target_lang = str(body.get("target_lang") or "zh").strip()
        style = str(body.get("style") or "chat")
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        purpose = str(body.get("purpose") or "").strip()

        if target_lang.lower() == "auto":
            target_lang = _resolve_conv_language(request, platform, account_id, chat_key)
        else:
            target_lang = normalize_lang(target_lang)
        if not target_lang:
            return {"ok": False, "resolved_target": "", "items": [], "skipped": 0}

        engine = str(body.get("engine") or "").strip().lower()
        if not engine:
            engine = _resolve_conv_engine(request, platform, account_id, chat_key)

        svc = _get_translation_service(request)
        _MAX_ITEMS = 50
        sem = _aio.Semaphore(8)

        async def _one(it):
            iid = str(it.get("id") or "")
            text = str(it.get("text") or "")
            src = normalize_lang(str(it.get("source_lang") or ""))
            async with sem:
                result = await svc.translate(
                    text, target_lang=target_lang, source_lang=src,
                    style=style, engine=engine,
                )
            return iid, result

        items = raw_items[:_MAX_ITEMS]
        pairs = await _aio.gather(*[_one(it) for it in items]) if items else []

        n_new = n_fail = 0
        by_lang: dict = {}
        out = []
        for iid, result in pairs:
            if result.ok and not getattr(result, "cached", False):
                n_new += 1
                src = normalize_lang(getattr(result, "source_lang", "") or "")
                if src:
                    by_lang[src] = by_lang.get(src, 0) + 1
            elif not result.ok:
                n_fail += 1
            out.append({"id": iid, "translation": result.to_dict()})

        # 与单条端点同口径：仅 purpose=inbound_display 记入站漏斗；缓存命中不计新增成本
        if purpose == "inbound_display" and (n_new or n_fail):
            try:
                ibx = _inbox_store(request)
                if ibx is not None and hasattr(ibx, "record_inbound_xlate"):
                    ibx.record_inbound_xlate(
                        translated=n_new, failed=n_fail, by_lang=by_lang or None)
            except Exception:
                logger.debug("[translate-batch] 入站翻译漏斗记账失败（忽略）", exc_info=True)

        return {
            "ok": True,
            "resolved_target": target_lang,
            "pref_engine": engine,
            "items": out,
            "skipped": max(0, len(raw_items) - _MAX_ITEMS),
        }

    @app.get("/api/unified-inbox/conv-engine")
    async def api_unified_inbox_get_conv_engine(
        request: Request, platform: str = "", account_id: str = "default",
        chat_key: str = "", _=Depends(api_auth),
    ):
        """F+2：读会话当前首选翻译引擎（前端切会话时取来显示徽标 / 离线提示）。"""
        platform = str(platform or "").lower()
        if not chat_key or not platform:
            return {"ok": False, "pref_engine": ""}
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "pref_engine": ""}
        eng = _resolve_conv_engine(request, platform, account_id, chat_key)
        return {"ok": True, "pref_engine": eng}

    @app.post("/api/unified-inbox/conv-engine")
    async def api_unified_inbox_set_conv_engine(request: Request, _=Depends(api_auth)):
        """F+：设置 / 清除会话首选翻译引擎（坐席多线路对照择优后记住，跨刷新/重启生效）。

        body：``{platform, account_id?, chat_key, engine}``。``engine=""`` → 清除偏好（回 failover）。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        engine = str(body.get("engine") or "").strip().lower()
        if not chat_key or not platform:
            return {"ok": False, "error": "missing_conversation"}
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        ok = ibx.set_conversation_pref_engine(_conv_id(platform, account_id, chat_key), engine)
        return {"ok": bool(ok), "pref_engine": engine}

    @app.get("/api/unified-inbox/default-lang")
    async def api_unified_inbox_get_default_lang(
        request: Request, platform: str = "", account_id: str = "default",
        _=Depends(api_auth),
    ):
        """P3：读「默认译文显示语言」解析结果 + 各维度原始值。

        前端切会话时取 ``resolved`` 作为默认（优先级：账号 > 平台 > 全局，置于「会话级偏好」
        之下、「浏览器本地默认」之上）；``scopes`` 供运营在弹层回显已配置的各维度值。
        """
        info = _resolve_default_lang(request, platform, account_id)
        return {"ok": True, **info}

    @app.get("/api/unified-inbox/default-lang/all")
    async def api_unified_inbox_list_default_lang(request: Request, _=Depends(api_auth)):
        """P4-A：列出所有已配置的「默认译文语言」（运营管理面板：全局/各平台/各账号 + 谁/何时改）。"""
        return {"ok": True, "items": _list_default_langs(request)}

    @app.post("/api/unified-inbox/default-lang")
    async def api_unified_inbox_set_default_lang(request: Request, _=Depends(api_auth)):
        """P3：设置/清除「默认译文显示语言」（运营级，换机/换坐席生效）。

        body：``{scope: global|platform|account, platform?, account_id?, lang, updated_by?}``。
        ``lang=""`` → 清除该维度；scope=platform/account 需带 platform（account 还需 account_id）。
        P4-A：``updated_by`` 记录修改人（best-effort 审计）。
        """
        body = await request.json()
        scope = str(body.get("scope") or "global").strip().lower()
        lang = normalize_lang(str(body.get("lang") or "").strip())
        platform = str(body.get("platform") or "").lower().strip()
        account_id = str(body.get("account_id") or "default").strip()
        updated_by = str(body.get("updated_by") or "").strip()
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        if scope == "global":
            key = _DEFAULT_LANG_KEY
        elif scope == "platform":
            if not platform:
                return {"ok": False, "error": "missing_platform"}
            key = f"{_DEFAULT_LANG_KEY}.platform.{platform}"
        elif scope == "account":
            if not platform:
                return {"ok": False, "error": "missing_platform"}
            key = f"{_DEFAULT_LANG_KEY}.account.{platform}.{account_id}"
        else:
            return {"ok": False, "error": "bad_scope"}
        ok = ibx.set_app_setting(key, lang, updated_by=updated_by)
        return {"ok": bool(ok), "scope": scope, "lang": lang}

    @app.get("/api/unified-inbox/default-reply-lang")
    async def api_unified_inbox_get_default_reply_lang(
        request: Request, platform: str = "", account_id: str = "default",
        _=Depends(api_auth),
    ):
        """P4-C：读「默认回复语言」（出站轴）解析结果 + 各维度原始值。

        桌面 copilot 草稿语言选择器在无会话级记忆时取 ``resolved`` 作默认（账号 > 平台 >
        全局），``resolved=""`` 则回落原有「跟随人设/客户」。``scopes`` 供运营回显。
        """
        info = _resolve_default_reply_lang(request, platform, account_id)
        return {"ok": True, **info}

    @app.get("/api/unified-inbox/default-reply-lang/all")
    async def api_unified_inbox_list_default_reply_lang(request: Request, _=Depends(api_auth)):
        """P4-C：列出所有已配置的「默认回复语言」（运营管理面板：全局/各平台/各账号 + 谁/何时改）。"""
        return {"ok": True, "items": _list_default_langs(request, base=_REPLY_LANG_KEY)}

    @app.post("/api/unified-inbox/default-reply-lang")
    async def api_unified_inbox_set_default_reply_lang(request: Request, _=Depends(api_auth)):
        """P4-C：设置/清除「默认回复语言」（出站轴，运营级，桌面草稿默认）。

        body 同 default-lang：``{scope: global|platform|account, platform?, account_id?, lang, updated_by?}``。
        ``lang=""`` → 清除该维度；scope=platform/account 需带 platform（account 还需 account_id）。
        """
        body = await request.json()
        scope = str(body.get("scope") or "global").strip().lower()
        lang = normalize_lang(str(body.get("lang") or "").strip())
        platform = str(body.get("platform") or "").lower().strip()
        account_id = str(body.get("account_id") or "default").strip()
        updated_by = str(body.get("updated_by") or "").strip()
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        if scope == "global":
            key = _REPLY_LANG_KEY
        elif scope == "platform":
            if not platform:
                return {"ok": False, "error": "missing_platform"}
            key = f"{_REPLY_LANG_KEY}.platform.{platform}"
        elif scope == "account":
            if not platform:
                return {"ok": False, "error": "missing_platform"}
            key = f"{_REPLY_LANG_KEY}.account.{platform}.{account_id}"
        else:
            return {"ok": False, "error": "bad_scope"}
        ok = ibx.set_app_setting(key, lang, updated_by=updated_by)
        return {"ok": bool(ok), "scope": scope, "lang": lang}

    @app.get("/api/unified-inbox/agent-lang")
    async def api_unified_inbox_get_agent_lang(request: Request, _=Depends(api_auth)):
        """P1：读当前坐席的「我的语言」偏好（服务端持久，换机/清缓存不丢）。

        身份取会话 session（无 SessionMiddleware 部署回落共享 "agent"）。
        ``lang=""`` = 未设置（前端回落浏览器语言推导）。
        """
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        agent = str(_session_agent(request).get("agent_id") or "agent")
        lang = normalize_lang(ibx.get_app_setting(f"{_AGENT_LANG_KEY}.{agent}"))
        if lang not in _AGENT_LANG_ALLOWED:
            lang = ""
        return {"ok": True, "agent_id": agent, "lang": lang}

    @app.post("/api/unified-inbox/agent-lang")
    async def api_unified_inbox_set_agent_lang(request: Request, _=Depends(api_auth)):
        """P1：设置/清除当前坐席「我的语言」。body：``{lang}``（``""`` = 清除）。

        语种限白名单（与前端翻译目标集同源）；写入随 ``updated_by=agent_id`` 留痕。
        """
        body = await request.json()
        lang = normalize_lang(str(body.get("lang") or "").strip())
        if lang and lang not in _AGENT_LANG_ALLOWED:
            return {"ok": False, "error": "bad_lang"}
        ibx = _inbox_store(request)
        if ibx is None:
            return {"ok": False, "error": "inbox_unavailable"}
        agent = str(_session_agent(request).get("agent_id") or "agent")
        ok = ibx.set_app_setting(f"{_AGENT_LANG_KEY}.{agent}", lang, updated_by=agent)
        return {"ok": bool(ok), "agent_id": agent, "lang": lang}

    @app.get("/api/unified-inbox/translation-engines")
    async def api_unified_inbox_translation_engines(
        request: Request, target_lang: str = "zh", _=Depends(api_auth)
    ):
        """指定目标语的引擎能力矩阵：让坐席在切换目标语时即知主引擎是否兜底。"""
        svc = _get_translation_service(request)
        return {"ok": True, "matrix": svc.engine_matrix(target_lang)}

    @app.post("/api/unified-inbox/translate-compare")
    async def api_unified_inbox_translate_compare(request: Request, _=Depends(api_auth)):
        """多线路对照选译：所有引擎各译一遍，返回候选列表供坐席择优（对标拓译多线路对照）。

        body：``{text, target_lang?(支持 auto), source_lang?, style?, platform?, account_id?, chat_key?}``。
        不写缓存/记忆；坐席择优后仍走 ``/translate`` 或 ``/send`` 正常落库。
        """
        # 对照选译=坐席主动消费（一次点击打全部引擎），同受 ai.translate 能力闸
        _deny_capability(request, "ai.translate")
        body = await request.json()
        text = str(body.get("text") or "")
        target_lang = str(body.get("target_lang") or "zh").strip()
        source_lang = normalize_lang(str(body.get("source_lang") or ""))
        style = str(body.get("style") or "chat")

        if target_lang.lower() == "auto":
            target_lang = _resolve_conv_language(
                request,
                str(body.get("platform") or "").lower(),
                str(body.get("account_id") or "default"),
                str(body.get("chat_key") or ""),
            )
        else:
            target_lang = normalize_lang(target_lang)

        if not target_lang:
            return {"ok": False, "resolved_target": "", "error": "auto_unresolved",
                    "candidates": []}

        svc = _get_translation_service(request)
        data = await svc.compare_translations(
            text, target_lang=target_lang, source_lang=source_lang, style=style,
        )
        cands = data.get("candidates") or []
        # P1-XF（2026-08-16）智能融合：fuse=true → 成功候选交 LLM 择优合成一条
        # 「融合译文」（Chimera 思路）。闸门/回落语义全在 translation_fusion 模块；
        # 不过闸 → fusion.ok=false 带原因，前端不出卡，对照功能本身零影响。
        if bool(body.get("fuse")):
            try:
                from src.ai.translation_fusion import fuse_compare_candidates
                data["fusion"] = await fuse_compare_candidates(
                    svc, text=text, candidates=cands,
                    source_lang=str(data.get("source_lang") or source_lang or ""),
                    target_lang=target_lang,
                )
            except Exception:
                data["fusion"] = {"ok": False, "reason": "fusion_error"}
        # 坐席字符计量归因（2026-08-16）：对照选译一次点击=每个引擎各译一遍，
        # 真实消耗是 len(text)×成功引擎数（失败候选没产出译文不计），与单引擎
        # /translate 的 len(text) 口径同源——只是乘上真实的引擎次数。
        # 融合成功再计一次 len(text)（多一次 LLM 编辑调用，同口径）。
        _ok_engines = sum(1 for c in cands if c.get("ok"))
        if (data.get("fusion") or {}).get("ok"):
            _ok_engines += 1
        if _ok_engines > 0:
            record_request_chars(request, "translation", len(text) * _ok_engines)
        return {
            "ok": any(c.get("ok") for c in cands),
            "resolved_target": target_lang,
            "compare": data,
        }

    @app.post("/api/unified-inbox/translate-document")
    async def api_unified_inbox_translate_document(request: Request, _=Depends(api_auth)):
        """Phase L：长文 / 文档整篇翻译（.txt / 粘贴）。

        body：``{text, target_lang?(支持 auto), source_lang?, style?, engine?, platform?, account_id?, chat_key?}``。
        逐段复用 ``/translate`` 同一 TranslationService（缓存/术语/F+ 会话首选引擎），按原排版重组。
        """
        # 文档整篇翻译=坐席主动消费（体量还大），同受 ai.translate 能力闸
        _deny_capability(request, "ai.translate")
        body = await request.json()
        text = str(body.get("text") or "")
        target_lang = str(body.get("target_lang") or "zh").strip()
        source_lang = normalize_lang(str(body.get("source_lang") or ""))
        style = str(body.get("style") or "chat")
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")

        if target_lang.lower() == "auto":
            target_lang = _resolve_conv_language(request, platform, account_id, chat_key)
        else:
            target_lang = normalize_lang(target_lang)
        if not target_lang:
            return {"ok": False, "reason": "auto_unresolved",
                    "message": "目标语未解析（会话客户语言未知）", "translated_text": ""}

        engine = str(body.get("engine") or "").strip().lower()
        if not engine:
            engine = _resolve_conv_engine(request, platform, account_id, chat_key)

        from src.ai.document_translate import DocumentTranslateService
        svc = DocumentTranslateService(_get_translation_service(request))
        _doc_res = await svc.translate_document(
            text, target_lang=target_lang, source_lang=source_lang,
            style=style, engine=engine,
        )
        # 坐席字符计量归因（2026-08-16）：源文本就在手上，按 len(text) 记——与
        # /translate 同口径（整篇成功才记；分段部分失败时 ok=False 不记，宁少勿多）。
        if _doc_res.get("ok"):
            record_request_chars(request, "translation", len(text))
        return _doc_res

    @app.post("/api/unified-inbox/translate-document-file")
    async def api_unified_inbox_translate_document_file(request: Request, _=Depends(api_auth)):
        """Phase L2/L2b：上传文档整篇翻译。

        - ``.docx`` / ``.xlsx``：**保版式**真往返，返回译后文件（base64 + filename）。
        - ``.pdf``：抽取文本→译→**纯文本**（pdf 不可结构化回填），返回 ``text``。

        body：``{file_b64, filename, target_lang?(支持 auto), source_lang?, style?, engine?,
                platform?, account_id?, chat_key?}``。复用 TranslationService（F+ 引擎/术语/缓存）。
        """
        # 上传文档翻译同受 ai.translate 能力闸。坐席字符计量刻意**不记**：源文本
        # 藏在 docx/xlsx/pdf 二进制里，端点拿到的 stats 只有分段计数（total/
        # translated）没有字符数，路由层无法可靠还原源文本长度——宁可少记不虚记
        # （SSE stream 路径的翻译更是发生在另一个 GET 长连接里，身份归因也断）。
        _deny_capability(request, "ai.translate")
        import base64

        body = await request.json()
        file_b64 = str(body.get("file_b64") or "")
        filename = str(body.get("filename") or "document.docx")
        target_lang = str(body.get("target_lang") or "zh").strip()
        source_lang = normalize_lang(str(body.get("source_lang") or ""))
        style = str(body.get("style") or "chat")
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")

        low = filename.lower()
        if low.endswith(".docx"):
            kind = "docx"
        elif low.endswith(".xlsx"):
            kind = "xlsx"
        elif low.endswith(".pdf"):
            kind = "pdf"
        else:
            return {"ok": False, "reason": "unsupported_ext",
                    "message": "文档翻译支持 .docx / .xlsx / .pdf（其他请用「文档翻译」粘贴文本）"}

        raw = file_b64.partition(",")[2] if file_b64.startswith("data:") else file_b64
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception:
            return {"ok": False, "reason": "decode_failed", "message": "文件解码失败"}
        if not data:
            return {"ok": False, "reason": "empty", "message": "空文件"}
        if len(data) > 10 * 1024 * 1024:
            return {"ok": False, "reason": "too_large", "message": "文件过大（上限 10MB）"}

        if target_lang.lower() == "auto":
            target_lang = _resolve_conv_language(request, platform, account_id, chat_key)
        else:
            target_lang = normalize_lang(target_lang)
        if not target_lang:
            return {"ok": False, "reason": "auto_unresolved",
                    "message": "目标语未解析（会话客户语言未知）"}

        engine = str(body.get("engine") or "").strip().lower()
        if not engine:
            engine = _resolve_conv_engine(request, platform, account_id, chat_key)

        base = filename.rsplit(".", 1)[0]
        params = dict(data=data, kind=kind, target_lang=target_lang,
                      source_lang=source_lang, style=style, engine=engine, base=base)

        # L2c-2：stream=true → 暂存输入换 job_id，翻译在 SSE GET 长连接内执行并推进度
        if bool(body.get("stream")):
            from src.web.document_job_store import get_document_job_store
            job_id = get_document_job_store().create(params)
            return {"ok": True, "job_id": job_id,
                    "progress_url": f"/api/unified-inbox/translate-document-progress/{job_id}"}

        xlate = _get_translation_service(request)
        return await _do_document_translation(xlate=xlate, **params)

    @app.get("/api/unified-inbox/translated-file/{token}")
    async def api_unified_inbox_translated_file(token: str, request: Request, _=Depends(api_auth)):
        """L2c-1：凭一次性 token 下载译后文档（取回即删，TTL 10 分钟）。

        浏览器 ``<a download>`` 导航携会话 cookie 过鉴权；二进制直传，不经 base64。
        """
        from fastapi import Response
        from urllib.parse import quote

        from src.web.translated_file_store import get_translated_file_store
        entry = get_translated_file_store().take(token)
        if entry is None:
            return Response(content="link expired or not found", status_code=404)
        # filename* 用 RFC5987 编码兼容非 ASCII 文件名
        disp = f"attachment; filename*=UTF-8''{quote(entry.filename)}"
        return Response(
            content=entry.data,
            media_type=entry.content_type,
            headers={"Content-Disposition": disp},
        )

    @app.get("/api/unified-inbox/translate-document-progress/{job_id}")
    async def api_unified_inbox_translate_document_progress(
        job_id: str, request: Request, _=Depends(api_auth)
    ):
        """L2c-2：SSE 进度流。翻译在本 GET 长连接内执行，逐段推 ``{status,done,total}``，
        结束 event 带 ``download_url``（file）或 ``text``（pdf）。job_id 来自 POST stream=true。
        """
        import asyncio as _aio
        import json as _json

        from fastapi.responses import StreamingResponse

        from src.web.document_job_store import get_document_job_store

        params = get_document_job_store().take(job_id)
        xlate = _get_translation_service(request)

        async def _gen():
            if params is None:
                yield f"data: {_json.dumps({'status': 'error', 'reason': 'job_not_found', 'message': '任务不存在或已过期'})}\n\n"
                return
            prog = {"done": 0, "total": 0}

            def _cb(done, total):
                prog["done"] = int(done)
                prog["total"] = int(total)

            task = _aio.ensure_future(
                _do_document_translation(xlate=xlate, progress=_cb, **params)
            )
            last = None
            while not task.done():
                cur = (prog["done"], prog["total"])
                if cur != last:
                    yield f"data: {_json.dumps({'status': 'running', 'done': cur[0], 'total': cur[1]})}\n\n"
                    last = cur
                await _aio.sleep(0.2)
            try:
                res = task.result()
            except Exception:
                logger.warning("[doc-job] SSE 翻译作业异常", exc_info=True)
                yield f"data: {_json.dumps({'status': 'error', 'reason': 'exception', 'message': '翻译作业异常'})}\n\n"
                return
            if res.get("ok"):
                done_evt = {"status": "done", "kind": res.get("kind", ""),
                            "filename": res.get("filename", ""),
                            "stats": res.get("stats", {})}
                if res.get("kind") == "file":
                    done_evt["download_url"] = res.get("download_url", "")
                else:
                    done_evt["text"] = res.get("text", "")
                yield f"data: {_json.dumps(done_evt, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {_json.dumps({'status': 'error', 'reason': res.get('reason', 'error'), 'message': res.get('message', '')}, ensure_ascii=False)}\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.post("/api/unified-inbox/translate-image")
    async def api_unified_inbox_translate_image(request: Request, _=Depends(api_auth)):
        """P58：图片 OCR → 翻译。前端传 base64 图片，返回逐字 OCR 文本 + 译文。"""
        # 图片翻译=坐席主动消费（OCR+翻译双烧），同受 ai.translate 能力闸
        _deny_capability(request, "ai.translate")
        import os as _os

        from src.ai.image_translate import (
            ImageTranslateService,
            build_vision_ocr_fn,
            decode_image_to_temp,
        )

        body = await request.json()
        image_b64 = str(body.get("image_b64") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        cm = getattr(request.app.state, "config_manager", None)
        vision_cfg = {}
        try:
            full = getattr(cm, "config", None) or {}
            vision_cfg = dict(full.get("vision") or {})
        except Exception:
            vision_cfg = {}
        if not vision_cfg.get("enabled", False):
            return {"ok": False, "reason": "vision_disabled",
                    "message": "图像识别未启用（config.vision.enabled）"}

        try:
            from src.vision_client import has_any_vision_backend
            if not has_any_vision_backend(vision_cfg, vision_cfg):
                return {"ok": False, "reason": "no_vision_backend",
                        "message": "未配置可用的图像识别后端（Ollama base_url 或智谱 api_key）"}
        except Exception:
            pass

        path, reason = decode_image_to_temp(image_b64)
        if path is None:
            return {"ok": False, "reason": reason, "message": f"图片无效：{reason}"}
        try:
            svc = ImageTranslateService(
                _get_translation_service(request),
                build_vision_ocr_fn(vision_cfg, vision_cfg),
            )
            _img_res = await svc.translate_image(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
            # 坐席字符计量归因（2026-08-16）：真正送进翻译引擎的是 OCR 出的
            # 文本（响应自带 ocr_text），按其长度记——与 /translate 的
            # 「源文本长度」同口径；OCR 失败/无文字（ok=False）不记。
            if _img_res.get("ok"):
                record_request_chars(
                    request, "translation", len(str(_img_res.get("ocr_text") or "")))
            return _img_res
        finally:
            try:
                _os.remove(path)
            except Exception:
                pass

    @app.post("/api/unified-inbox/translate-voice")
    async def api_unified_inbox_translate_voice(request: Request, _=Depends(api_auth)):
        """P58-2：语音转写(ASR) → 翻译。前端传 base64 音频，返回转写文本 + 译文。"""
        import os as _os

        from src.ai.voice_translate import (
            VoiceTranslateService,
            build_audio_transcribe_fn,
            decode_audio_to_temp,
        )

        body = await request.json()
        audio_b64 = str(body.get("audio_b64") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        cm = getattr(request.app.state, "config_manager", None)
        audio_cfg = {}
        try:
            full = getattr(cm, "config", None) or {}
            audio_cfg = dict(full.get("audio_pipeline") or {})
        except Exception:
            audio_cfg = {}
        if not audio_cfg.get("enabled", False):
            return {"ok": False, "reason": "asr_disabled",
                    "message": "语音转写未启用（config.audio_pipeline.enabled）"}

        path, reason = decode_audio_to_temp(audio_b64)
        if path is None:
            return {"ok": False, "reason": reason, "message": f"音频无效：{reason}"}
        try:
            svc = VoiceTranslateService(
                _get_translation_service(request),
                build_audio_transcribe_fn(audio_cfg),
            )
            return await svc.translate_voice(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
        finally:
            try:
                _os.remove(path)
            except Exception:
                pass

    @app.post("/api/unified-inbox/translate-message-media")
    async def api_unified_inbox_translate_message_media(request: Request, _=Depends(api_auth)):
        """P61-2：会话内媒体一键翻译（可解析则免上传）。"""
        from src.inbox.media_resolver import resolve_for_translate

        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        media_type, media_ref = _lookup_stored_media(request, conversation_id, message_id)
        if not media_ref:
            media_ref = str(body.get("media_ref") or "")
            media_type = media_type or str(body.get("media_type") or "")

        base_dirs = _media_base_dirs(request)
        try:
            from src.integrations.protocol_bridge import (
                protocol_media_root, static_media_ref_to_path,
            )
            _local = static_media_ref_to_path(media_ref)
            if _local:
                media_ref = _local
                base_dirs = base_dirs + [str(protocol_media_root())]
        except Exception:
            logger.debug("protocol 媒体路径映射失败", exc_info=True)

        message = {"media_type": media_type, "media_ref": media_ref}
        path, kind, reason = resolve_for_translate(message, base_dirs=base_dirs)

        _tmp_download: Optional[str] = None
        if reason == "remote_unsupported":
            _rf = _remote_fetch_cfg(request)
            if _rf.get("enabled", False):
                from src.inbox.media_fetch import fetch_remote_media
                _dl_path, _dl_reason = await fetch_remote_media(
                    media_ref,
                    kind=kind,
                    max_bytes=int(_rf.get("max_mb", 10) or 10) * 1024 * 1024,
                    timeout_sec=float(_rf.get("timeout_sec", 8) or 8),
                    allow_domains=list(_rf.get("allow_domains") or []),
                )
                if _dl_path:
                    path, reason, _tmp_download = _dl_path, "ok", _dl_path
                else:
                    return {"ok": False, "reason": _dl_reason, "fallback": "upload",
                            "message": "远程媒体下载失败，请上传文件"}

        if reason != "ok":
            msg = {
                "no_ref": "该消息无媒体引用",
                "remote_unsupported": "媒体为远程链接，暂不支持免上传翻译，请上传文件",
                "not_found": "未找到本地媒体文件，请上传文件",
                "unsupported_kind": "暂不支持该媒体类型翻译",
            }.get(reason, reason)
            return {"ok": False, "reason": reason, "fallback": "upload", "message": msg}

        if _tmp_download is None and not _within_base_dirs(path, base_dirs):
            return {"ok": False, "reason": "outside_base_dirs", "fallback": "upload",
                    "message": "媒体文件不在允许目录内"}

        try:
            if kind == "image":
                from src.ai.image_translate import ImageTranslateService, build_vision_ocr_fn
                cm = getattr(request.app.state, "config_manager", None)
                try:
                    vision_cfg = dict((getattr(cm, "config", None) or {}).get("vision") or {})
                except Exception:
                    vision_cfg = {}
                if not vision_cfg.get("enabled", False):
                    return {"ok": False, "reason": "vision_disabled",
                            "message": "图像识别未启用（config.vision.enabled）"}
                try:
                    from src.vision_client import has_any_vision_backend
                    if not has_any_vision_backend(vision_cfg, vision_cfg):
                        return {"ok": False, "reason": "no_vision_backend",
                                "message": "未配置可用的图像识别后端"}
                except Exception:
                    pass
                svc = ImageTranslateService(
                    _get_translation_service(request),
                    build_vision_ocr_fn(vision_cfg, vision_cfg),
                )
                out = await svc.translate_image(
                    path, target_lang=target_lang, source_lang=source_lang, style=style,
                )
                out["media_kind"] = "image"
                out["from_upload"] = False
                out["from_remote"] = _tmp_download is not None
                return out

            from src.ai.voice_translate import VoiceTranslateService, build_audio_transcribe_fn
            cm = getattr(request.app.state, "config_manager", None)
            try:
                audio_cfg = dict((getattr(cm, "config", None) or {}).get("audio_pipeline") or {})
            except Exception:
                audio_cfg = {}
            if not audio_cfg.get("enabled", False):
                return {"ok": False, "reason": "asr_disabled",
                        "message": "语音转写未启用（config.audio_pipeline.enabled）"}
            svc = VoiceTranslateService(
                _get_translation_service(request),
                build_audio_transcribe_fn(audio_cfg),
            )
            out = await svc.translate_voice(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
            out["media_kind"] = "voice"
            out["from_upload"] = False
            out["from_remote"] = _tmp_download is not None
            return out
        finally:
            if _tmp_download:
                try:
                    os.unlink(_tmp_download)
                except Exception:
                    pass
