# -*- coding: utf-8 -*-
"""AI 助手悬浮球（「小智」）路由族 /api/assistant/*（2026-08-19 P0）。

端点：
  GET  /api/assistant/bootstrap  组件启动探针（enabled/白标名/快捷 chips）
  POST /api/assistant/query      产品问答（ndjson 流式：meta→delta→done|err）
  POST /api/assistant/report     一键报障（写 bug_tickets + 附件落盘 + 告警）
  GET  /api/assistant/tickets    我的工单（含面板即回访的 notify_ts 盖章）
  POST /api/assistant/feedback   问答 👍👎（写 qa_log verdict）
  GET  /api/assistant/health     语料/台账/LLM 可达（ops 卡数据源）

设计红线（改前先读）：
- RBAC 在 context_builder（agent 拿不到管理页说明），不靠 LLM 自觉；
- 帮助语料独立库（assistant_help.db），检索零命中 = 诚实说不知道 +
  引导报障，**绝不让 LLM 裸编**（qa_log 记 miss = 运营补语料照单）；
- 报障复用 bug_tickets 生命周期（chat_id=webuser:<uid>，platform=assistant
  事后盖章）；TG 回访冲刷队列已按 webuser: 前缀排除（bug_intake），web 工单
  的回访路径 = 本面板 tickets 端点盖 notify_ts；
- 附件消毒：服务端命名 + magic bytes 校验（PNG/JPEG）+ 尺寸上限；
- 线协议按事件对象（meta/delta/done/err）设计；**P0 刻意不用流式响应**
  （2026-08-20 线上实锤：CompressionMiddleware 只豁免 text/event-stream，
  application/x-ndjson 的 StreamingResponse 会在首块后被压缩层掐断——meta
  之后 delta/done 全丢且零异常日志）。P0 单大 delta 本无流式收益，改
  JSON `{ok, events:[...]}` 一次返回；P1 真 token 流式直接换 SSE
  （text/event-stream 已在压缩层豁免清单，中间件零改动）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.assistant.routes")

# 2026-08-21 老板实测拍板：问答链**不设字数/秒数限制**（此前 500 字拒答 +
# 45s wait_for 超时；本地链答长问经常 >45s 直接被掐）。仅保留防病态载荷的
# 静默上限（人类输入永远打不到）。
_MAX_Q_CHARS = 20000  # 静默截断，不再 400 拒绝
_MIN_DESC_CHARS = 5


def _assistant_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = config.get("assistant") if isinstance(config, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def _query_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    q = cfg.get("query")
    return q if isinstance(q, dict) else {}


def _report_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    r = cfg.get("report")
    return r if isinstance(r, dict) else {}


def _sniff_image(raw: bytes) -> str:
    """magic bytes 判型（媒体产物验证纪律：绝不信 content-type/后缀）。"""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:2] == b"\xff\xd8":
        return "jpg"
    return ""


def _decode_shot(shot_b64: str, max_kb: int) -> tuple[bytes, str]:
    """data-url / 裸 base64 → (bytes, ext)；非法/超限抛 ValueError。"""
    s = str(shot_b64 or "").strip()
    if "," in s and s[:5] == "data:":
        s = s.split(",", 1)[1]
    if not s:
        raise ValueError("empty")
    # base64 长度先粗筛（4/3 膨胀），防解码 DoS
    if len(s) > max_kb * 1024 * 4 // 3 + 16:
        raise ValueError("too_large")
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError("bad_base64") from e
    if len(raw) > max_kb * 1024:
        raise ValueError("too_large")
    ext = _sniff_image(raw)
    if not ext:
        raise ValueError("not_image")
    return raw, ext


def _reports_dir() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / "assistant_reports"


def _session_user(request: Request) -> tuple[str, str, str]:
    """(user_id, username, role) —— token 会话无 user_id 时按 username/token 记。"""
    sess = request.session
    uid = str(sess.get("user_id") or "").strip()
    name = str(sess.get("username") or "").strip()
    role = str(sess.get("role") or "").strip() or "admin"
    if not uid:
        uid = name or "token"
    return uid, name or uid, role


# ── 「没依据」哨兵（2026-08-27）────────────────────────────────────────────
# 提示词里一直写着「参考条目覆盖不到时诚实说明」，但那句话只对用户生效——
# **路由无从判断 LLM 到底是答了还是拒了**，于是 qa_log 恒记 answered=True
# （自答率虚高）、report_hint 不亮（用户没有出路）、ops「未答清单」看不见缺口。
#
# 为什么用哨兵而不是「答前先判一次」：
#   · 嵌入余弦实测**分不开**（2026-08-27：正样本余弦最低 0.455，而危险的
#     「产品形状但语料没覆盖」类负样本是 0.56~0.61，全在正样本区间内——
#     余弦量的是话题相关性，不是「这段文档能否回答这个问题」）；
#   · 再调一次 LLM 当裁判要多一个往返，对每条问答都加延迟；
#   · 而**正在答题的那个 LLM 本来就同时看着问题和条目**，它就是最合适的裁判，
#     让它自己声明＝零额外成本。
# 流式下靠首段缓冲实现「命中哨兵就一个字都不吐」，代价只是首 8 字节的等待。
_NO_BASIS = "NO_BASIS"


def _is_no_basis(text: str) -> bool:
    """LLM 是否自认「参考条目回答不了」（哨兵必须在开头，防正文误伤）。"""
    return str(text or "").lstrip().upper().startswith(_NO_BASIS)


def _detect_report_hint(q: str) -> bool:
    ql = str(q or "").lower()
    return any(k in ql for k in (
        "报错", "坏了", "点了没反应", "没反应", "闪退", "打不开", "崩溃",
        "bug", "error", "broken", "crash", "not working", "doesn't work",
    ))


def assistant_llm_extra_body(base_url: str, model: str, *,
                             reasoning: bool = False) -> Dict[str, Any]:
    """与 AIClient 同口径：deepseek-v4 默认关思维链。

    助手直连曾漏下这一档（2026-08-23）：v4 思维链默认开且与正文共享
    max_tokens，复杂帮助 prompt 会把 content 挤成 0 字；前端只认
    delta/done，流在 meta 后结束 → 误显示「网络异常」。
    """
    base = str(base_url or "").lower()
    model_l = str(model or "").lower()
    if ("deepseek" in base and model_l.startswith("deepseek-v4")
            and not reasoning):
        return {"thinking": {"type": "disabled"}}
    return {}


def register_assistant_routes(app, ctx) -> None:
    _api_auth = ctx.api_auth
    config_manager = ctx.config_manager
    telegram_client = ctx.telegram_client

    def _cfg() -> Dict[str, Any]:
        c = getattr(config_manager, "config", None)
        return c if isinstance(c, dict) else {}

    def _enabled_or_403(request: Request) -> Dict[str, Any]:
        cfg = _assistant_cfg(_cfg())
        if not cfg.get("enabled"):
            raise HTTPException(403, tr(request, "asb.err.disabled"))
        return cfg

    def _resolve_sm():
        try:
            from src.web.web_context import resolve_skill_manager

            return resolve_skill_manager(telegram_client, app)
        except Exception:
            return None

    async def _direct_llm_stream(llm_cfg: Dict[str, Any], prompt: str,
                                 max_tokens: int):
        """助手专属直连 LLM **流式**（2026-08-21 P2）：逐 token 产出——
        面板打字机实时渲染，「不设秒数限制」后的体验闭环（等 8 秒黑盒 →
        看着答案长出来）。api_key='inherit' 复用 ai.api_key（不复制密钥）。
        无超时（老板拍板；SDK 600s 兜底防死连接）。"""
        base = str(llm_cfg.get("base_url") or "").strip()
        model = str(llm_cfg.get("model") or "").strip()
        if not base or not model:
            raise RuntimeError("assistant llm not configured")
        key = str(llm_cfg.get("api_key") or "inherit").strip()
        if key in ("", "inherit"):
            key = str((_cfg().get("ai") or {}).get("api_key") or "")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=base, api_key=key, max_retries=0)
        create_kw: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": True,
        }
        extra = assistant_llm_extra_body(
            base, model, reasoning=bool(llm_cfg.get("reasoning")))
        if extra:
            create_kw["extra_body"] = extra
        try:
            stream = await client.chat.completions.create(**create_kw)
            yielded = False
            async for chunk in stream:
                piece = ""
                try:
                    piece = chunk.choices[0].delta.content or ""
                except Exception:
                    piece = ""
                if piece:
                    yielded = True
                    yield piece
            if not yielded:
                raise RuntimeError("assistant llm empty stream")
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass

    # ------------------------------------------------------------ bootstrap
    @app.get("/api/assistant/bootstrap")
    async def api_assistant_bootstrap(request: Request):
        """组件启动探针：未启用/后端未装载时前端保持旧帮助球行为。"""
        _api_auth(request)
        cfg = _assistant_cfg(_cfg())
        if not cfg.get("enabled"):
            return {"ok": True, "enabled": False}
        brand = ""
        try:
            brand = str(
                _cfg().get("web_admin", {}).get("site_name", "") or ""
            ).strip()
        except Exception:
            brand = ""
        chips: list[str] = []
        try:
            from src.assistant.qa_log import get_qa_log

            # P1：页级 chips——loader 带 ?page=，本页高频优先全局补位；
            # 旧 loader 不带参 → page 空 → 与旧行为完全一致。
            chips = get_qa_log().top_questions(
                days=14, limit=5,
                page=str(request.query_params.get("page") or "")[:120])
        except Exception:
            chips = []
        from src.assistant.help_kb import get_help_kb

        vcfg = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
        out = {
            "ok": True,
            "enabled": True,
            "name": str(cfg.get("name") or "").strip(),
            "brand": brand,
            "report_enabled": bool(_report_cfg(cfg).get("enabled", True)),
            "voice": bool(vcfg.get("enabled")),
            "kb_entries": get_help_kb().count(),
            "chips": chips,
        }
        # 对话球画质档后端旋钮（实施59 P2）：assistant.orb_level ∈ {0,1,2}。
        # 缺省不下发 → 前端默认档 2（组件内置）；坐席本地「动效」偏好
        # （localStorage asb_orb_lvl）与系统 reduced-motion 仍压过此值。
        olv = cfg.get("orb_level", None)
        if isinstance(olv, (int, float)) and not isinstance(olv, bool):
            out["orb_level"] = max(0, min(2, int(olv)))
        return out

    # ------------------------------------------------------------ terms
    @app.get("/api/assistant/terms")
    async def api_assistant_terms(request: Request):
        """教学模式讲解词典下发（实施58 教学深化 P0，2026-08-23）。

        背景：TERM_DICT 全局变量只在 admin 壳 base.html 注入——workspace
        坐席壳的教学模式点什么都是空泛兜底（等于没词典）。本端点把
        help_terms.py 的同一份词典按需下发（前端 localStorage 6h 缓存，
        admin 壳仍优先用模板注入的 TERM_DICT，零多余请求）。"""
        _api_auth(request)
        _enabled_or_403(request)
        from src.web.help_terms import HELP_TERMS

        return {"ok": True, "count": len(HELP_TERMS), "terms": HELP_TERMS}

    # ------------------------------------------------------------ query
    @app.post("/api/assistant/query")
    async def api_assistant_query(request: Request):
        _api_auth(request)
        cfg = _enabled_or_403(request)
        data = await request.json()
        q = str(data.get("q") or "").strip()[:_MAX_Q_CHARS]
        if not q:
            raise HTTPException(400, tr(request, "asb.err.q_empty"))
        page = str(data.get("page") or "")[:120]
        lang = "en" if str(data.get("lang") or "").lower().startswith("en") else "zh"
        uid, _uname, role = _session_user(request)

        # ── 多轮上下文（2026-08-23，老板批准 token 成本）：前端带最近几轮
        #    q/a；只作指代理解素材，事实仍以参考条目为准（诚实拒答不变）。
        #    旧前端不带 history → turns 为空 → 行为与单轮完全一致。
        turns: list[tuple[str, str]] = []
        raw_hist = data.get("history")
        if isinstance(raw_hist, list):
            for h in raw_hist[-3:]:
                if not isinstance(h, dict):
                    continue
                hq = str(h.get("q") or "").strip()[:200]
                ha = str(h.get("a") or "").strip()[:600]
                if hq:
                    turns.append((hq, ha))

        qcfg = _query_cfg(cfg)
        from src.assistant.rate_limit import get_rate_limiter
        from src.assistant.stats import get_assistant_stats

        stats = get_assistant_stats()
        verdict = get_rate_limiter().check_and_record(
            f"q:{uid}",
            int(qcfg.get("rate_per_min", 10) or 0),
            int(qcfg.get("rate_per_day", 200) or 0),
        )
        if not verdict.allowed:
            stats.record_rate_limited()
            raise HTTPException(429, tr(request, "asb.err.rate_limited"))

        top_k = max(1, min(int(qcfg.get("top_k", 3) or 3), 8))
        # 强命中线 1.6 → 35（2026-08-27 用 assistant_qa_eval 实测校准）。
        # 旧值 1.6 **从来没拦下过任何东西**：真实 BM25 分数落在 25~240 区间，
        # 于是「无依据就诚实拒答」这条设计从未生效——线上 qa_log 里
        # `answered=1` 却答非所问就是这么来的。
        # 校准数据（40 条正样本 / 15 条负样本，见 src/eval/assistant_qa_eval.py）：
        #   正样本最低 41.2（「界面能调成黑色的吗」），负样本最高 109.8；
        #   两者**重叠**，单一 BM25 阈值不可能干净分开，只能选代价点：
        #     阈值 1.6 → 误拒 0/40，拦住 1/15（只拦得住纯乱码）
        #     阈值 35  → 误拒 0/40，拦住 7/15   ← 取这个
        #     阈值 40  → 误拒 0/40，拦住 8/15（但离最低正样本只剩 1.2，太脆）
        # 35 留 6.2 分余量吸收语料漂移。**刻意不追求拦住全部**：剩下 8 条
        # 「产品形状但语料没覆盖」的问题（如「怎么导出所有客户的手机号」96 分）
        # 用的就是产品词汇，分数天然高，靠分数拦不住——那需要出站前再加一道
        # 「检索到的条目真能回答这个问题吗」的语义核对，属下一阶段。
        # 注意：实例配置默认**不带** assistant.query 段，所以这个硬编码默认值
        # 就是线上真正生效的值（只改 config.example.yaml 对生产无效）。
        min_score = float(qcfg.get("min_score", 35.0) or 0)
        report_hint = _detect_report_hint(q)

        # ── 真流式（2026-08-21 P2）：text/event-stream 逐 token 推送。
        # 为什么是 SSE 而不是 ndjson：CompressionMiddleware 只豁免
        # text/event-stream（compression.py:109）——8/20 的 ndjson 流式被
        # 压缩层掐断正是本端点弃流式改 JSON 的原因；SSE 走豁免通道，中间件
        # 零改动。事件对象与 JSON 版完全同形（meta/delta/done/err），前端
        # 双模读取器按 content-type 分流。
        def _sse(obj: Dict[str, Any]) -> str:
            return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

        async def _gen():
            t0 = time.time()
            try:
                from src.assistant.context_builder import build_context_block
                from src.assistant.help_kb import get_help_kb
                from src.assistant.qa_log import get_qa_log

                hits = get_help_kb().search(q, top_k=top_k, lang=lang)
                strong = [h for h in hits if h["score"] >= min_score]
                if not strong and turns:
                    # 追问粘性窗：短指代问（「那第二步呢」）BM25 天然 miss，
                    # 拼上一轮问题重检一次——救指代，不降诚实线（还不中照旧拒答）。
                    hits2 = get_help_kb().search(
                        (turns[-1][0] + " " + q)[:200], top_k=top_k, lang=lang)
                    strong2 = [h for h in hits2 if h["score"] >= min_score]
                    if strong2:
                        strong = strong2
                sources = [
                    {"id": h["id"], "title": h["title"], "path": h["path"],
                     "anchor": str(h.get("anchor") or ""),  # P3 聚光灯目标
                     "score": h["score"]}
                    for h in strong
                ]
                # 零命中必给报障入口（2026-08-27 修：意图与实现原本对不上）。
                # 下面的零命中分支注释写的是「诚实说不知道 + 报障入口」，但
                # report_hint 此前**只**由问题里的 bug 关键词（报错/闪退/crash…）
                # 触发——用户问「运营漏斗这页怎么用」而语料没货时，小智只说一句
                # 「不知道」就没了，没有任何出路。而「答不上来」恰恰是最该转成
                # 报障/工单线索的时刻：它同时是用户的死路和我们补语料的照单。
                yield _sse({"ev": "meta", "sources": sources,
                            "report_hint": bool(report_hint or not strong)})

                if not strong:
                    # 零命中：诚实说不知道 + 报障入口，绝不让 LLM 裸编
                    latency = int((time.time() - t0) * 1000)
                    qa_id = get_qa_log().record(
                        user_id=uid, role=role, page=page, q=q,
                        answered=False, latency_ms=latency,
                    )
                    stats.record_query(answered=False, latency_ms=latency,
                                       refusal="no_hit")
                    yield _sse({"ev": "delta",
                                "text": tr(request, "asb.a.no_hit")})
                    yield _sse({"ev": "done", "ms": latency, "qa_id": qa_id,
                                "answered": False})
                    return

                ctx_block = build_context_block(
                    page=page, role=role, lang=lang,
                    ui_build=str(data.get("ui_build") or "")[:40],
                )
                src_lines = []
                for i, h in enumerate(strong, 1):
                    src_lines.append(
                        f"[S{i}] {h['title']}"
                        + (f"（入口 {h['path']}）" if h["path"] else "")
                        + f"\n{h['content'][:500]}"
                    )
                hist_block = ""
                if turns:
                    pair_lines = []
                    for hq, ha in turns:
                        pair_lines.append(
                            ("User: " if lang == "en" else "用户：") + hq)
                        if ha:
                            pair_lines.append(
                                ("Assistant: " if lang == "en" else "助手：")
                                + ha)
                    if lang == "en":
                        hist_block = ("\n\nRecent conversation (context for "
                                      "pronouns/follow-ups ONLY; facts still "
                                      "come from the references):\n"
                                      + "\n".join(pair_lines))
                    else:
                        hist_block = ("\n\n最近对话（仅供理解上下文与指代；"
                                      "事实仍以参考条目为准）：\n"
                                      + "\n".join(pair_lines))
                if lang == "en":
                    prompt = (
                        "You are the in-product help assistant for this "
                        "customer-service system. Answer ONLY product-usage "
                        "questions, strictly based on the reference entries "
                        "below. Rules: for operations give the path, never "
                        "perform actions; deleting/restarting/config changes "
                        "must carry an 'ask an admin to confirm' note; if the "
                        "references do not cover it, say so honestly and "
                        "suggest the report tab — NEVER invent features. Cite "
                        "sources inline like [S1]. Answer in English, "
                        "concisely.\n"
                        "IMPORTANT: if the reference entries genuinely cannot "
                        f"answer the question, output exactly `{_NO_BASIS}` as "
                        "the very first thing and write nothing else — the "
                        "system will then show the user an honest note plus a "
                        "way to report it. Do not stretch a loosely related "
                        "entry into an answer.\n\n"
                        f"User context:\n{ctx_block}\n\n"
                        "Reference entries:\n" + "\n\n".join(src_lines)
                        + hist_block
                        + f"\n\nUser question: {q}"
                    )
                else:
                    prompt = (
                        "你是本客服系统的产品内置帮助助手。只回答本产品的使用"
                        "问题，且严格基于下方参考条目作答。规则：操作类问题只"
                        "给路径与步骤，不代替执行；涉及删除/重启/修改配置必须"
                        "提示「请管理员确认后操作」；参考条目覆盖不到时诚实说"
                        "明并建议走「报障」标签——**禁止编造功能**。回答内用 "
                        "[S1] 这样的标号引用来源。用中文简洁作答。\n"
                        "**重要**：如果参考条目确实回答不了这个问题，请把 "
                        f"`{_NO_BASIS}` 作为回答的最开头原样输出，且不要再写"
                        "别的内容——系统会替你给出诚实说明与报障入口。"
                        "不要把只是沾边的条目硬凑成答案。\n\n"
                        f"用户上下文：\n{ctx_block}\n\n"
                        "参考条目：\n" + "\n\n".join(src_lines)
                        + hist_block
                        + f"\n\n用户问题：{q}"
                    )
                # 出话（无 wait_for 超时——2026-08-21 老板拍板）：
                # ① 直连端点流式逐 token 推（快路径+打字机）；开流前失败回落
                # ② 主链容灾 generate_reply（非流式整段一次推）。已吐半截再炸
                #    → 如实 err+重试（比静默换答案诚实）。
                max_tok = max(256, int(qcfg.get("max_tokens", 8000) or 8000))
                llm_cfg = (qcfg.get("llm")
                           if isinstance(qcfg.get("llm"), dict) else {})
                answer = ""
                streamed = False
                # 首段缓冲：攒够哨兵长度才决定放不放行——命中哨兵时用户
                # **一个字都看不到**（否则先吐半句再撤回比不撤更糟）。
                head_buf = ""
                gate_open = False
                if llm_cfg.get("base_url"):
                    try:
                        async for piece in _direct_llm_stream(
                                llm_cfg, prompt, max_tok):
                            answer += piece
                            if gate_open:
                                streamed = True
                                yield _sse({"ev": "delta", "text": piece})
                                continue
                            head_buf += piece
                            if len(head_buf.lstrip()) < len(_NO_BASIS):
                                continue  # 还不够判，继续攒
                            if _is_no_basis(head_buf):
                                break     # 自认没依据：停流，不吐任何内容
                            gate_open = True
                            streamed = True
                            yield _sse({"ev": "delta", "text": head_buf})
                        # 回答比哨兵还短（极少见）：出循环时仍未开闸，补吐
                        if not gate_open and head_buf and not _is_no_basis(head_buf):
                            gate_open = True
                            streamed = True
                            yield _sse({"ev": "delta", "text": head_buf})
                    except Exception:
                        logger.warning("[assistant] 直连流式失败%s",
                                       "（已部分输出）" if streamed else "，回落主链",
                                       exc_info=True)
                        if streamed:
                            raise
                        answer = ""
                if not answer:
                    sm = _resolve_sm()
                    if sm is None or not hasattr(sm, "ai_client"):
                        raise RuntimeError("skill_manager unavailable")
                    answer = str(await sm.ai_client.generate_reply(
                        user_message=prompt,
                        context={"current_intent": "assistant_help",
                                 "kb_context": ""},
                        strategy_overrides={"temperature": 0.3,
                                            "max_tokens": max_tok},
                    ) or "").strip()
                    if not answer:
                        raise RuntimeError("empty answer")
                    # 非流式路径同样先判后吐（这里还一个字都没发出去）
                    if not _is_no_basis(answer):
                        yield _sse({"ev": "delta", "text": answer})
                latency = int((time.time() - t0) * 1000)
                # LLM 自认没依据 → 与「零命中」同一出口：诚实说明 + 报障入口，
                # 且 answered=False 让 qa_log / 自答率 / ops「未答清单」记真话。
                if _is_no_basis(answer):
                    qa_id = get_qa_log().record(
                        user_id=uid, role=role, page=page, q=q, answered=False,
                        top_score=float(strong[0]["score"]),
                        sources=",".join(s["id"] for s in sources)[:800],
                        latency_ms=latency,
                    )
                    stats.record_query(answered=False, latency_ms=latency,
                                       refusal="no_basis")
                    yield _sse({"ev": "delta",
                                "text": tr(request, "asb.a.no_hit")})
                    yield _sse({"ev": "done", "ms": latency, "qa_id": qa_id,
                                "answered": False})
                    return
                qa_id = get_qa_log().record(
                    user_id=uid, role=role, page=page, q=q, answered=True,
                    top_score=float(strong[0]["score"]),
                    sources=",".join(s["id"] for s in sources)[:800],
                    latency_ms=latency,
                )
                stats.record_query(answered=True, latency_ms=latency)
                yield _sse({"ev": "done", "ms": latency, "qa_id": qa_id,
                            "answered": True})
            except asyncio.CancelledError:
                # 直连流收尾/客户端断开都可能是 CancelledError（BaseException），
                # 不接 = meta 之后静默掐流，前端只能显示「网络异常」。
                logger.warning("[assistant] query 被取消")
                stats.record_query(answered=False, error=True,
                                   latency_ms=int((time.time() - t0) * 1000))
                try:
                    yield _sse({"ev": "err", "key": "asb.err.answer_failed",
                                "text": tr(request, "asb.err.answer_failed")})
                except Exception:
                    pass
                raise
            except Exception:
                logger.warning("[assistant] query 失败", exc_info=True)
                stats.record_query(answered=False, error=True,
                                   latency_ms=int((time.time() - t0) * 1000))
                yield _sse({"ev": "err", "key": "asb.err.answer_failed",
                            "text": tr(request, "asb.err.answer_failed")})

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ report
    @app.post("/api/assistant/report")
    async def api_assistant_report(request: Request):
        _api_auth(request)
        cfg = _enabled_or_403(request)
        rcfg = _report_cfg(cfg)
        if not rcfg.get("enabled", True):
            raise HTTPException(403, tr(request, "asb.err.report_disabled"))
        data = await request.json()
        desc = str(data.get("desc") or "").strip()
        if len(desc) < _MIN_DESC_CHARS:
            raise HTTPException(400, tr(request, "asb.err.desc_too_short"))
        page = str(data.get("page") or "")[:120]
        uid, uname, role = _session_user(request)

        from src.assistant.rate_limit import get_rate_limiter
        from src.assistant.stats import get_assistant_stats

        stats = get_assistant_stats()
        verdict = get_rate_limiter().check_and_record(
            f"r:{uid}", per_min=3, per_day=40
        )
        if not verdict.allowed:
            stats.record_rate_limited()
            raise HTTPException(429, tr(request, "asb.err.rate_limited"))

        # 截图先验（尺寸/magic bytes），坏附件整单拒绝——比落了单再丢附件诚实
        shot_raw: bytes = b""
        shot_ext = ""
        shot_b64 = str(data.get("shot_b64") or "")
        if shot_b64:
            try:
                shot_raw, shot_ext = _decode_shot(
                    shot_b64, int(rcfg.get("max_shot_kb", 2048) or 2048)
                )
            except ValueError as e:
                raise HTTPException(
                    400, tr(request, "asb.err.bad_shot", reason=str(e))
                ) from None

        # 环境块（服务端自采，用户零复述；红线：不含聊天原文/密钥/cookie）
        env_lines = [f"页面: {page or '?'}", f"角色: {role}"]
        ua = str(request.headers.get("user-agent") or "")[:200]
        if ua:
            env_lines.append(f"UA: {ua}")
        ui_build = str(data.get("ui_build") or "")[:40]
        if ui_build:
            env_lines.append(f"UI build: {ui_build}")
        # 应用版本 + 机器码（2026-08-27 补）：此前环境块只有 UA 与 UI build——
        # UA 里有 Electron/Chrome 版本却**没有本产品版本号**，于是「这个 bug 在
        # 哪个版本上」全靠猜；机器码是坐席机的唯一标识，客服要按它找机器。
        # 两者服务端直接可取（与 /api/support/info 同源），不必让前端多打一次
        # 请求、也不必信任客户端上报值。取不到＝不写该行（宁缺勿假）。
        try:
            from src.utils.app_identity import app_version

            _ver = str(app_version() or "").strip()
            if _ver:
                env_lines.append(f"应用版本: {_ver}")
        except Exception:
            logger.debug("[assistant] 版本读取失败（忽略）", exc_info=True)
        try:
            from src.utils.diag_upload import machine_code

            _mc = str(machine_code() or "").strip()
            if _mc:
                env_lines.append(f"机器码: {_mc}")
        except Exception:
            logger.debug("[assistant] 机器码读取失败（忽略）", exc_info=True)
        if bool(data.get("include_env", True)):
            try:
                from src.web.frontend_error_stats import get_frontend_error_stats

                fe = get_frontend_error_stats().dump()
                if fe.get("total"):
                    top_fn = list(fe.get("by_fn", {}).items())[:3]
                    env_lines.append(
                        "近窗前端错误: total="
                        + str(fe["total"]) + " top_fn=" + repr(top_fn)
                    )
            except Exception:
                pass
        body_text = desc + "\n[env] " + "；".join(env_lines)

        from src.ops.bug_intake import (
            _record_event,
            append_ticket_note,
            record_bug_ticket,
        )

        chat_key = f"webuser:{uid}"
        res = record_bug_ticket(
            chat_id=chat_key, account_id="assistant", reporter_id=uid,
            reporter_name=uname, text=body_text,
        )
        tid = int(res.get("ticket_id") or 0)
        if not tid:
            raise HTTPException(500, tr(request, "asb.err.report_failed"))
        dup = not bool(res.get("is_new"))

        # 来源盖章（record_bug_ticket 无 platform 形参；新单补章，并单已有）
        if not dup:
            try:
                from src.ops.bug_intake import _db, _LOCK

                con = _db()
                with _LOCK:
                    con.execute(
                        "UPDATE bug_tickets SET platform='assistant' WHERE id=?",
                        (tid,),
                    )
                    con.commit()
            except Exception:
                logger.debug("[assistant] platform 盖章失败（忽略）", exc_info=True)

        # 附件落盘（服务端命名，杜绝用户可控路径）
        shot_path = None
        if shot_raw:
            try:
                d = _reports_dir() / str(tid)
                d.mkdir(parents=True, exist_ok=True)
                n = len(list(d.glob("shot_*"))) + 1
                fp = d / f"shot_{n}.{shot_ext}"
                fp.write_bytes(shot_raw)
                shot_path = fp
                append_ticket_note(tid, f"[assistant 截图] {fp.name}")
                _record_event(chat_key, "assistant_attach", uid, fp.name)
            except Exception:
                logger.warning("[assistant] 附件落盘失败", exc_info=True)

        # VLM 视觉摘要（P1 2026-08-20，assistant.vision.enabled 默认关）：
        # 开发方在工单台账/TG 里先看到「截图里是什么」，不用逐张点开附件。
        # best-effort：VLM 掉线/超时/空答一律静默，绝不影响已落的工单。
        vis_cfg = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
        if shot_path is not None and vis_cfg.get("enabled"):
            try:
                from src.vision_client import VisionClient

                vcfg_root = _cfg().get("vision") or {}
                v_prompt = (
                    "这是客服系统后台的报障截图。用中文 2-3 句简述画面里可见的"
                    "界面/报错/异常状态（如红条、弹窗、空白区域、按钮文案），"
                    "不要猜测截图之外的原因。"
                )
                desc, _vtag = await asyncio.wait_for(
                    VisionClient.describe_image_with_ollama_zhipu_fallback(
                        vcfg_root, vcfg_root, str(shot_path), prompt=v_prompt,
                    ),
                    timeout=25,
                )
                desc = str(desc or "").strip()
                if desc:
                    append_ticket_note(tid, "[AI 视觉摘要] " + desc[:400])
            except Exception:
                logger.debug("[assistant] 视觉摘要失败（忽略）", exc_info=True)

        stats.record_report(dup=dup)
        try:
            from src.integrations.shared.event_bus import get_event_bus

            get_event_bus().publish(
                "assistant_report_alert",
                {
                    "ticket_id": tid,
                    "dup": dup,
                    "severity": str(res.get("severity") or "P2"),
                    "reporter": uname,
                    "page": page,
                    "text": desc[:120],
                    # 有没有截图要在通知里说：截图是报障里信息密度最高的东西，
                    # 客服知道「有图」才会去点开看（取图端点见 bug_intake_routes）
                    "shots": 1 if shot_path is not None else 0,
                    "rate_key": f"assistant:{uid}",
                },
            )
        except Exception:
            logger.debug("[assistant] 报障告警发布失败（忽略）", exc_info=True)
        return {"ok": True, "ticket_id": tid, "dup": dup,
                "severity": res.get("severity")}

    # ------------------------------------------------------------ tickets
    @app.get("/api/assistant/tickets")
    async def api_assistant_tickets(request: Request):
        """我的工单。面板即回访：本人拉取时对 fixed 且未回访的单盖 notify_ts
        （用户真的看到了=回访送达，也把它从 TG 冲刷积压口径里摘掉）。"""
        _api_auth(request)
        _enabled_or_403(request)
        uid, _uname, _role = _session_user(request)
        chat_key = f"webuser:{uid}"
        try:
            import sqlite3 as _sq

            from src.ops.bug_intake import _db, mark_notified

            con = _db()
            con.row_factory = _sq.Row
            rows = con.execute(
                "SELECT id, created_ts, updated_ts, title, status, severity,"
                " report_count, notify_ts, notify_note FROM bug_tickets"
                " WHERE chat_id=? ORDER BY id DESC LIMIT 30",
                (chat_key,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                if d.get("status") == "fixed" and not float(d.get("notify_ts") or 0):
                    try:
                        mark_notified(int(d["id"]), True, "assistant-panel")
                        d["notify_ts"] = time.time()
                    except Exception:
                        pass
                out.append(d)
            return {"ok": True, "tickets": out}
        except Exception:
            logger.warning("[assistant] tickets 查询失败", exc_info=True)
            return {"ok": True, "tickets": []}

    # ------------------------------------------------------------ feedback
    @app.post("/api/assistant/feedback")
    async def api_assistant_feedback(request: Request):
        _api_auth(request)
        _enabled_or_403(request)
        data = await request.json()
        qa_id = int(data.get("qa_id") or 0)
        verdict = str(data.get("verdict") or "").strip().lower()
        if not qa_id or verdict not in ("up", "down"):
            raise HTTPException(400, tr(request, "asb.err.bad_feedback"))
        from src.assistant.qa_log import get_qa_log
        from src.assistant.stats import get_assistant_stats

        ok = get_qa_log().set_verdict(qa_id, verdict)
        if ok:
            get_assistant_stats().record_feedback(verdict)
        return {"ok": bool(ok)}

    # ------------------------------------------------------------ transcribe
    @app.post("/api/assistant/transcribe")
    async def api_assistant_transcribe(request: Request):
        """语音提问转写（P1 2026-08-20）：MediaRecorder 音频 → 176 GPU ASR →
        文本回填输入框（用户确认后才发送，防误转写直接提交）。复用
        voice_translate 的 decode（mime 白名单+25MB 护栏自带）与 audio_pipeline
        配置（zhiliao overlay 已指 176 whisper）。assistant.voice.enabled 闸。"""
        _api_auth(request)
        cfg = _enabled_or_403(request)
        vcfg = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
        if not vcfg.get("enabled"):
            raise HTTPException(403, tr(request, "asb.err.voice_disabled"))
        data = await request.json()
        uid, _uname, _role = _session_user(request)

        from src.assistant.rate_limit import get_rate_limiter
        from src.assistant.stats import get_assistant_stats

        verdict = get_rate_limiter().check_and_record(
            f"v:{uid}", per_min=6, per_day=120
        )
        if not verdict.allowed:
            get_assistant_stats().record_rate_limited()
            raise HTTPException(429, tr(request, "asb.err.rate_limited"))

        from src.ai.voice_translate import (
            build_audio_transcribe_fn,
            decode_audio_to_temp,
        )

        path, reason = decode_audio_to_temp(str(data.get("audio_b64") or ""))
        if not path:
            raise HTTPException(
                400, tr(request, "asb.err.bad_audio", reason=reason))
        import os as _os

        try:
            acfg = _cfg().get("audio_pipeline")
            if not (isinstance(acfg, dict) and acfg.get("enabled")):
                raise HTTPException(
                    503, tr(request, "asb.err.asr_unavailable"))
            fn = build_audio_transcribe_fn(acfg)
            # 300s=防死连接的病态守卫（录音本身 ≤45s，正常转写秒级）；
            # 转写文本不截断（2026-08-21 与问答同批取消字数限制）
            text = await asyncio.wait_for(fn(path), timeout=300)
            if isinstance(text, tuple):  # want_segments 契约防御
                text = text[0]
            text = str(text or "").strip()
            if not text:
                raise HTTPException(422, tr(request, "asb.err.asr_empty"))
            return {"ok": True, "text": text}
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            raise HTTPException(
                504, tr(request, "asb.err.timeout")) from None
        except Exception:
            logger.warning("[assistant] transcribe 失败", exc_info=True)
            raise HTTPException(
                500, tr(request, "asb.err.asr_failed")) from None
        finally:
            try:
                _os.unlink(path)
            except Exception:
                pass

    # ------------------------------------------------------------ health
    @app.get("/api/assistant/health")
    async def api_assistant_health(request: Request):
        _api_auth(request)
        cfg = _assistant_cfg(_cfg())
        from src.assistant.help_kb import get_help_kb
        from src.assistant.qa_log import get_qa_log
        from src.assistant.stats import get_assistant_stats

        sm = _resolve_sm()
        return {
            "ok": True,
            "enabled": bool(cfg.get("enabled")),
            "kb_entries": get_help_kb().count(),
            "llm_wired": bool(sm is not None and hasattr(sm, "ai_client")),
            "qa_7d": get_qa_log().stats(days=7),
            "miss_top": get_qa_log().miss_list(days=14, limit=5),
            "process": get_assistant_stats().dump(),
        }
