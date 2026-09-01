# -*- coding: utf-8 -*-
"""AI 助手悬浮球（「小智」）路由族 /api/assistant/*（2026-08-19 P0）。

端点：
  GET  /api/assistant/bootstrap  组件启动探针（enabled/白标名/快捷 chips）
  GET  /api/assistant/faq        「常问」面板（高频分组 + 帮助库搜索）
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


def _reply_lang_and_name(request, data) -> tuple:
    """小智回复语言：跟随智聊 UI 语言（``request.state.ui_lang``，由 i18n 中间件写入），
    默认中文、可切换、覆盖全语言。返回 ``(code, name)``——code 必为 AIClient._LANG_NAMES
    的键（否则回落 zh），name 为该语言自称名（空=按中文默认，不追加语言硬指令）。

    优先级：ui_lang（智聊语言选择器，权威）→ 请求体 lang（旧前端兼容）→ zh。
    zh-tw/zh-hk 等中文变体归 zh（书面主回复用简体；繁/粤变体由 translation 子系统另管）。
    """
    try:
        from src.ai.ai_client import AIClient
        names = AIClient._LANG_NAMES
    except Exception:
        names = {"zh": "中文", "en": "English"}
    raw = ""
    try:
        raw = str(getattr(getattr(request, "state", None), "ui_lang", "") or "").strip().lower()
    except Exception:
        raw = ""
    if not raw:
        raw = str((data or {}).get("lang") or "").strip().lower()
    code = "zh"
    if raw in names:
        code = raw
    else:
        base = raw.split("-")[0].split("_")[0]
        if base in names:
            code = base
        elif base == "en":
            code = "en"
    return code, names.get(code, "")
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


def _faq_seed(lang: str, limit: int) -> list[dict]:
    """「常问」空态 seed（实施73 P1-6）。

    新装机 ``qa_log`` 必然为空，而空面板会让人以为功能坏了。回落 how-to
    语料的标题——那本来就是一组「怎么做 X」的真问题，且与检索栈同源，点了
    一定答得上；语料缺席则如实返回空表（不编题目）。
    """
    try:
        from src.assistant.howto_pack import build_howto_entries

        rows = build_howto_entries()
    except Exception:
        logger.warning("assistant faq seed 取 how-to 失败", exc_info=True)
        return []
    key = "title_en" if lang == "en" else "title"
    out: list[dict] = []
    for r in rows[: max(1, limit)]:
        title = str(r.get(key) or r.get("title") or "").strip()
        if title:
            out.append({"q": title[:80], "n": 0})
    return out


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
# 第二枚哨兵（2026-08-29 P0，老板拍板「不能只调用帮助库」）：无文档依据时这一
# 轮仍然作答，但必须让用户知道**依据是什么**。LLM 自报家门：以 GENERAL 开头＝
# 「这不是产品文档内容，是通用知识」；不带前缀＝用产品事实卡答的。
# 为什么用哨兵而不是再调一次 LLM 分类：首段缓冲机制（head_buf）本就已经在等
# NO_BASIS，多认一个前缀是零成本；多一次往返则要给每条问答加一整个 RTT。
_GENERAL = "GENERAL"


def _is_no_basis(text: str) -> bool:
    """LLM 是否自认「参考条目回答不了」（哨兵必须在开头，防正文误伤）。"""
    return str(text or "").lstrip().upper().startswith(_NO_BASIS)


def _is_general(text: str) -> bool:
    """LLM 是否自报「这是通用知识，不是产品文档内容」。"""
    return str(text or "").lstrip().upper().startswith(_GENERAL)


def _strip_general(text: str) -> str:
    """剥掉 GENERAL 前缀（含其后的冒号/空白），只留正文给用户看。"""
    s = str(text or "").lstrip()
    if not _is_general(s):
        return text
    s = s[len(_GENERAL):]
    return s.lstrip(" :：\t\r\n")


def build_docless_prompt(q: str, ctx_block: str, hist_block: str,
                         lang: str = "zh") -> str:
    """帮助库零命中时的 prompt：产品事实卡兜底 + 通用知识放行 + 拒答哨兵。

    三条出路互斥且必须自报家门，否则用户分不清「这是产品承诺」还是「模型随口
    说的」——那比不回答更危险（销售会拿去当承诺）。红线仍是**绝不编造产品功能**：
    事实卡没写、文档也没有的产品问题一律走 NO_BASIS，不许拿通用知识去猜产品。
    """
    from src.assistant.product_facts import product_facts_block

    facts = product_facts_block(lang)
    if str(lang or "").lower().startswith("en"):
        return (
            "You are 小智, the in-product assistant of this customer-service "
            "system. The help corpus returned NOTHING for this question, so "
            "follow this decision order strictly:\n"
            "1) If the PRODUCT FACTS below answer it (capability/boundary "
            "questions such as which platforms are supported) — answer from "
            "them, concisely.\n"
            f"2) If it is NOT asking about this product's features, settings, "
            f"operations or data — e.g. write me something, translate, explain "
            f"a concept, give advice, chit-chat — then **even if the topic "
            f"relates to support/sales/customers it counts as general**: "
            f"answer helpfully but start your reply with `{_GENERAL}` on its "
            "own, so the UI can label it as general knowledge.\n"
            f"3) ONLY when it really asks whether THIS product can do "
            f"something, or how to operate it, and neither the facts nor the "
            f"docs cover it — output exactly `{_NO_BASIS}` and nothing else.\n"
            "NEVER invent product features. Do not use general knowledge to "
            "guess how THIS product behaves. But do not mistake a "
            "'write something for me' request for a product question.\n\n"
            f"PRODUCT FACTS:\n{facts}\n\n"
            f"User context:\n{ctx_block}{hist_block}\n\n"
            f"User question: {q}"
        )
    return (
        "你是本客服系统的产品内置助手「小智」。这个问题在帮助库里**零命中**，"
        "请严格按下面的顺序决定怎么答：\n"
        "1）如果下面的【产品事实】能回答它（如「支持哪些平台」这类能力边界"
        "问题）——就用事实卡回答，简洁给结论。\n"
        f"2）如果它**不是在问本产品的功能、设置、操作或数据**——例如让你帮忙"
        "写文案、翻译、解释概念、出主意、闲聊、常识问答——那么**即使话题与"
        "客服/销售/客户有关，也算通用问题**：正常热心地回答，但**回复开头单独"
        f"写 `{_GENERAL}`**，好让界面标注这是通用知识而非产品文档。\n"
        f"3）只有当它确实在问**本产品**能不能做某事、或该怎么操作，而事实卡与"
        f"文档都没有覆盖时——才只输出 `{_NO_BASIS}`，别写其他任何内容。\n"
        "**绝不编造产品功能**；不要用通用知识去猜本产品的行为。但也不要把"
        "「帮我写点什么」这类请求误判成产品问题而拒答——那类请求你答得了。\n\n"
        f"【产品事实】\n{facts}\n\n"
        f"用户上下文：\n{ctx_block}{hist_block}\n\n"
        f"用户问题：{q}"
    )


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
    # vLLM 上的 Qwen3 系（本机 LAN 落点 173:8001 chatx=Qwen3.6-27B-abl）**默认
    # 开 thinking**：正文全进 reasoning、message.content 恒为 null，预算还被思考
    # 吃光（finish_reason=length）。2026-08-28 这个坑已经让 LAN 翻译兜底静默失效
    # 过一次；docless 轮改打这个端点，必须同款关掉，否则用户看到的是空回答。
    # 键名与 translation_engines / voice_colloquial_llm / ai_client 三处同源。
    if not reasoning and (":8001" in base or "vllm" in base
                          or model_l.startswith("chatx")
                          or model_l.startswith("qwen3")):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


# SSE 注释行（`:` 开头）：不是事件，前端 readNdjson 的 handleLine 直接忽略，
# 但它让连接上一直有字节流动 —— 故加心跳**零前端改动**。
_SSE_KEEPALIVE = ": ka\n\n"
_KA_INTERVAL_SEC = 10.0


async def stream_with_keepalive(agen, interval: float = _KA_INTERVAL_SEC):
    """把上游异步生成器包成「带心跳的流」，产出 ('ka',None) 或 ('piece',数据)。

    为什么必须有（2026-08-28 事故实测）：本端点先推 meta，随后在等 LLM 首个
    token 期间**完全零字节**（qa_log 里存过 latency_ms=37638 的真实案例）。
    任何中间层（桌面壳 net stack / 反向 SSH 隧道 / nginx proxy_read_timeout）
    的空闲超时都会把这段静默当成死连接掐断 → 生成器收 CancelledError →
    那时再 yield err 已经没有接收者 → 前端只拿到 meta，落到兜底文案
    「网络异常，请重试」，而用户的网络其实好得很（当日实测云端 0.28s 可达）。

    上游异常原样转交调用方（保持原 try/except 语义不变）；上游正常结束即
    return。**getter 用持久 future 而不是 wait_for(q.get())**：后者在超时
    取消时存在「已取到值却被丢弃」的竞态，会静默吞掉一个 token。
    """
    q: "asyncio.Queue[tuple]" = asyncio.Queue()

    async def _pump():
        try:
            async for item in agen:
                await q.put(("piece", item))
        except BaseException as exc:  # noqa: BLE001 - 原样转交，不在此判类型
            await q.put(("exc", exc))
        else:
            await q.put(("end", None))

    pump = asyncio.ensure_future(_pump())
    getter = None
    try:
        while True:
            if getter is None:
                getter = asyncio.ensure_future(q.get())
            done_set, _pending = await asyncio.wait({getter}, timeout=interval)
            if not done_set:
                yield ("ka", None)
                continue
            kind, payload = getter.result()
            getter = None
            if kind == "end":
                return
            if kind == "exc":
                raise payload
            yield ("piece", payload)
    finally:
        # 只取消不 await：被取消的 task 不会触发「exception never retrieved」，
        # 而在生成器 finally 里 await 一个可能已被取消的上游是新的挂起风险。
        if getter is not None:
            getter.cancel()
        if not pump.done():
            pump.cancel()


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
        _ai = _cfg().get("ai")
        _ai = _ai if isinstance(_ai, dict) else {}
        base = str(llm_cfg.get("base_url") or "").strip()
        model = str(llm_cfg.get("model") or "").strip()
        # base_url / model 同样支持 inherit（2026-08-28 错配事故的根治）：
        # 主链端点迁移（api.deepseek.com → api.siliconflow.cn）时这里没跟着改，
        # 而 api_key: inherit 取到的是**新家的** key —— 拿 A 家钥匙开 B 家门，
        # 每次提问必 401，坐席看到的却是「网络异常」。三个键同源就不会再分叉；
        # 显式值仍然优先（要单独指一个更便宜/更快的模型时照旧可配）。
        if base in ("", "inherit"):
            base = str(_ai.get("base_url") or "").strip()
        if model in ("", "inherit"):
            model = str(_ai.get("model") or "").strip()
        if not base or not model:
            raise RuntimeError("assistant llm not configured")
        key = str(llm_cfg.get("api_key") or "inherit").strip()
        if key in ("", "inherit"):
            key = str(_ai.get("api_key") or "")
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

    # ------------------------------------------------------------ faq
    @app.get("/api/assistant/faq")
    async def api_assistant_faq(request: Request):
        """「常问」面板数据源（实施73 P1-6，2026-08-28）。

        一个端点两种模式：

        * 无 ``q`` → 高频问题**分组**（本页 / 全站）+ 频次，数据源是真实问答
          日志 ``qa_log``。**刻意不做人工维护的 FAQ 表**——人工表必然与实际
          漂移（实施73 §5 已拍板）；零记录时回落 how-to 标题当入门问题集。
        * 有 ``q`` → 走 ``help_kb`` BM25，与问答链**同一套检索栈**：搜得到的
          就是答得出的，不新建第二套语料。
        """
        _api_auth(request)
        _enabled_or_403(request)
        qp = request.query_params
        q = str(qp.get("q") or "").strip()[:120]
        lang = "en" if str(qp.get("lang") or "").lower().startswith("en") else "zh"
        try:
            limit = int(qp.get("limit") or 12)
        except Exception:
            limit = 12
        limit = max(1, min(limit, 30))
        if q:
            from src.assistant.help_kb import get_help_kb

            try:
                hits = get_help_kb().search(q, top_k=limit, lang=lang)
            except Exception:
                logger.warning("assistant faq 检索失败", exc_info=True)
                hits = []
            return {
                "ok": True,
                "mode": "search",
                "items": [
                    {"title": str(h.get("title") or "")[:120],
                     "path": str(h.get("path") or "")}
                    for h in hits if str(h.get("title") or "").strip()
                ],
            }
        scope = str(qp.get("scope") or "both").strip().lower()
        if scope not in ("page", "global", "both"):
            scope = "both"
        from src.assistant.qa_log import get_qa_log

        try:
            grouped = get_qa_log().top_questions_detail(
                days=14, limit=limit, page=str(qp.get("page") or "")[:120])
        except Exception:
            logger.warning("assistant faq 高频取数失败", exc_info=True)
            grouped = {"page": [], "global": []}
        page_items = grouped.get("page", []) if scope != "global" else []
        global_items = grouped.get("global", []) if scope != "page" else []
        out: Dict[str, Any] = {
            "ok": True,
            "mode": "top",
            "page_items": page_items,
            "global_items": global_items,
            "seed_items": [],
        }
        if not page_items and not global_items:
            out["seed_items"] = _faq_seed(lang, limit)
        return out

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
        # 小智回复语言跟随智聊 UI 语言（默认中文、可切换、全语言）
        _reply_lang, _reply_lang_name = _reply_lang_and_name(request, data)
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
            ka_sent = 0
            # 取消定位用的阶段游标（2026-08-28）：本端点被取消时**整段都没有
            # 现场**——CancelledError 是 BaseException，`except Exception` 抓不到，
            # 而 yield 是暂停点，取消可以落在任意两个 yield 之间。只报「被取消」
            # 等于只说了「它死了」，说不出死在哪一步。
            stage = "start"
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
                stage = "kb_searched"
                yield _sse({"ev": "meta", "sources": sources,
                            "report_hint": bool(report_hint or not strong)})
                stage = "meta_sent"

                # 零命中**不再直接拒答**（2026-08-29 P0，老板拍板：「每个问题不能
                # 只调用帮助，要和 deepseek/硅基的接口结合处理」）。旧行为把三种
                # 完全不同的情形压成同一句「没找到可靠依据」：
                #   ① 产品能力边界问题（支持抖音吗）——答案在产品事实卡里，**有**；
                #   ② 与产品无关的通用问题（帮我写句话）——通用模型答得了；
                #   ③ 产品问题且确实没依据——这才该拒答。
                # 实录代价：连问两个不同问题得到逐字相同的回复，用户判定「AI 没用」。
                # 现在零命中转入 docless 链，由 LLM 自报 GENERAL/NO_BASIS 哨兵分流，
                # 前端按 basis 标注依据来源——**答什么** 和 **凭什么答** 分开说清楚。
                docless = not strong
                ctx_block = build_context_block(
                    page=page, role=role, lang=lang,
                    ui_build=str(data.get("ui_build") or "")[:40],
                )
                stage = "ctx_built"
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
                if docless:
                    prompt = build_docless_prompt(q, ctx_block, hist_block, lang)
                elif lang == "en":
                    prompt = (
                        "You are the in-product help assistant for this "
                        "customer-service system. Answer ONLY product-usage "
                        "questions, strictly based on the reference entries "
                        "below. Rules: for operations give the path, never "
                        "perform actions (if an entry explicitly states the "
                        "asked operation/feature does not exist or is not "
                        "supported, relaying that fact plus the entry's "
                        "alternative IS the correct answer); "
                        "deleting/restarting/config changes "
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
                        "entry into an answer.\n"
                        "BUT: when an entry explicitly states that the asked "
                        "capability does not exist / is not supported / has a "
                        "boundary (often with an alternative), that IS the "
                        f"answer — relay it honestly instead of `{_NO_BASIS}` "
                        "(\"no\" backed by the docs is a valid answer, not a "
                        "missing basis). Example: entry says \"bulk phone-"
                        "number export does not exist; the list CSV has no "
                        "phone column\", user asks \"how do I export all "
                        "phone numbers\" -> correct answer: relay that fact "
                        f"plus the alternative entry point, NOT `{_NO_BASIS}`."
                        "\n\n"
                        f"User context:\n{ctx_block}\n\n"
                        "Reference entries:\n" + "\n\n".join(src_lines)
                        + hist_block
                        + f"\n\nUser question: {q}"
                    )
                else:
                    prompt = (
                        "你是本客服系统的产品内置帮助助手。只回答本产品的使用"
                        "问题，且严格基于下方参考条目作答。规则：操作类问题只"
                        "给路径与步骤，不代替执行（条目若明确说明所问操作/功能"
                        "**不存在或不支持**，「如实转告 + 给条目里的替代做法」"
                        "就是正确答案）；涉及删除/重启/修改配置必须"
                        "提示「请管理员确认后操作」；参考条目覆盖不到时诚实说"
                        "明并建议走「报障」标签——**禁止编造功能**。回答内用 "
                        "[S1] 这样的标号引用来源。用中文简洁作答。\n"
                        "**重要**：如果参考条目确实回答不了这个问题，请把 "
                        f"`{_NO_BASIS}` 作为回答的最开头原样输出，且不要再写"
                        "别的内容——系统会替你给出诚实说明与报障入口。"
                        "不要把只是沾边的条目硬凑成答案。\n"
                        "**但注意**：若条目**明确说明**所问功能不存在/不支持/"
                        "有边界（通常还给了替代做法），那本身就是答案——请如实"
                        f"转告而不是输出 `{_NO_BASIS}`（有据可依的「不能」是"
                        "有效回答，不是没依据）。例：条目写着「没有批量导出"
                        "手机号的功能，列表 CSV 不含手机号」，用户问「怎么导出"
                        "所有客户的手机号」→ 正确做法是转告这一事实并给出条目里"
                        f"的替代入口，而不是输出 `{_NO_BASIS}`。\n\n"
                        f"用户上下文：\n{ctx_block}\n\n"
                        "参考条目：\n" + "\n\n".join(src_lines)
                        + hist_block
                        + f"\n\n用户问题：{q}"
                    )
                # 语言收口：zh/en 已由上面 scaffold 覆盖；其余语言在 prompt 末尾追加硬指令
                # 覆盖 scaffold 的语言——直连流式与 generate_reply 兜底两条出话路径同享。
                if _reply_lang != "zh" and not (_reply_lang == "en" and lang == "en") and _reply_lang_name:
                    prompt += (f"\n\n[LANGUAGE] 请只用 {_reply_lang_name} 回复，不要混入其它语言。"
                               f"Reply ONLY in {_reply_lang_name}.")
                # 出话（无 wait_for 超时——2026-08-21 老板拍板）：
                # ① 直连端点流式逐 token 推（快路径+打字机）；开流前失败回落
                # ② 主链容灾 generate_reply（非流式整段一次推）。已吐半截再炸
                #    → 如实 err+重试（比静默换答案诚实）。
                max_tok = max(256, int(qcfg.get("max_tokens", 8000) or 8000))
                llm_cfg = (qcfg.get("llm")
                           if isinstance(qcfg.get("llm"), dict) else {})
                # docless 轮走**局域网无审查模型**（老板 8/29 指定）：它既要答
                # 通用问题（云端模型会拒绝或打太极的那些），又要省云端 token——
                # 这一轮本就没有文档依据，用不着云端的强检索理解力。
                # 缺省继承 ai.fallback（173:8001 chatx，OpenAI 兼容），所以不配
                # general_llm 也能工作；显式配置优先。
                if docless:
                    _gen_cfg = qcfg.get("general_llm")
                    if not isinstance(_gen_cfg, dict) or not _gen_cfg.get("base_url"):
                        _fb = (_cfg().get("ai") or {}).get("fallback")
                        _gen_cfg = _fb if isinstance(_fb, dict) else {}
                    llm_cfg = _gen_cfg or llm_cfg
                answer = ""
                streamed = False
                # 首段缓冲：攒够哨兵长度才决定放不放行——命中哨兵时用户
                # **一个字都看不到**（否则先吐半句再撤回比不撤更糟）。
                head_buf = ""
                gate_open = False
                if llm_cfg.get("base_url"):
                    stage = "llm_direct_open"
                    try:
                        async for _kind, piece in stream_with_keepalive(
                                _direct_llm_stream(llm_cfg, prompt, max_tok)):
                            if _kind == "ka":
                                ka_sent += 1
                                yield _SSE_KEEPALIVE
                                continue
                            stage = "llm_direct_streaming"
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
                            # 剥 GENERAL 前缀：它是给系统看的分层信号，不是给
                            # 用户看的内容（原样吐出去=回答开头挂着个英文单词）。
                            yield _sse({"ev": "delta",
                                        "text": _strip_general(head_buf)})
                        # 回答比哨兵还短（极少见）：出循环时仍未开闸，补吐
                        if not gate_open and head_buf and not _is_no_basis(head_buf):
                            gate_open = True
                            streamed = True
                            yield _sse({"ev": "delta",
                                        "text": _strip_general(head_buf)})
                    except Exception as _exc:
                        # 认证类失败要单独点名：它不是「网络抖动」而是**配置错了**
                        # （三键分叉），文案直接给出复查位置，省掉下一次的排查。
                        _s = str(_exc)
                        _authish = any(k in _s for k in (
                            "401", "403", "Authentication", "api key",
                            "Unauthorized", "invalid_api_key"))
                        logger.warning(
                            "[assistant] 直连流式失败%s%s",
                            "（已部分输出）" if streamed else "，回落主链",
                            "｜认证被拒：assistant.query.llm 的 base_url/model/"
                            "api_key 与 ai.* 分叉了（三者必须同源，"
                            "推荐全写 inherit）" if _authish else "",
                            exc_info=True)
                        if streamed:
                            raise
                        answer = ""
                if not answer:
                    stage = "fallback_main"
                    sm = _resolve_sm()
                    if sm is None or not hasattr(sm, "ai_client"):
                        raise RuntimeError("skill_manager unavailable")
                    # 回落主链是**非流式**整段返回，静默窗比直连更长（云端降级
                    # 时尤甚）→ 同样要心跳，否则这条容灾路径自己会被掐断。
                    _fb = asyncio.ensure_future(sm.ai_client.generate_reply(
                        user_message=prompt,
                        context={"current_intent": "assistant_help",
                                 "kb_context": "", "reply_lang": _reply_lang},
                        strategy_overrides={"temperature": 0.3,
                                            "max_tokens": max_tok},
                        route="assistant_qa",
                    ))
                    while True:
                        _done, _p = await asyncio.wait(
                            {_fb}, timeout=_KA_INTERVAL_SEC)
                        if _done:
                            break
                        ka_sent += 1
                        yield _SSE_KEEPALIVE
                    answer = str(_fb.result() or "").strip()
                    if not answer:
                        raise RuntimeError("empty answer")
                    # 非流式路径同样先判后吐（这里还一个字都没发出去）
                    if not _is_no_basis(answer):
                        yield _sse({"ev": "delta",
                                    "text": _strip_general(answer)})
                latency = int((time.time() - t0) * 1000)
                # top_score 与 **strong 绑定**（不是与分支绑定）：docless 轮压根
                # 没有检索命中，硬取 strong[0] 会 IndexError。语义上也只有真检索
                # 到东西才有「最高分」这个数，记 0 会污染自答率的分位分析。
                _top = float(strong[0]["score"]) if strong else None
                _src_ids = ",".join(s["id"] for s in sources)[:800]
                _rec: Dict[str, Any] = {
                    "user_id": uid, "role": role, "page": page, "q": q,
                    "latency_ms": latency,
                }
                if strong:
                    _rec["top_score"] = _top
                    _rec["sources"] = _src_ids
                # LLM 自认没依据 → 诚实说明 + 报障入口，且 answered=False 让
                # qa_log / 自答率 / ops「未答清单」记真话。
                if _is_no_basis(answer):
                    # ── NO_BASIS 客观复核（2026-09-01，实施93 飞轮批）──────
                    # 实测（DeepSeek-V3.2 流式，temp 0.3）：条目**明确写着**
                    # 「所问功能不存在 + 替代做法」时，模型 2/3 概率仍把它误判
                    # 成「答不了」输出 NO_BASIS（快答路径；慢思考路径就答对）。
                    # 提示词加例已到收益边界 → 确定性护栏：检索最高分 ≥120
                    # （校准：金标负样本历史最高 109.8，「问题≈条目标题」的
                    # 目标问句 148/196）时判定客观不成立，逐字转告条目原文
                    # （零编造——不重新生成，只引用），answered=True。
                    # 分数 <120 维持原拒答语义，负样本行为零变化。
                    _top_hit = strong[0] if strong else None
                    if _top_hit and float(_top_hit["score"]) >= 120.0:
                        logger.info(
                            "[assistant] NO_BASIS 被强命中推翻 score=%.1f id=%s",
                            float(_top_hit["score"]), _top_hit["id"])
                        answer = (f"[S1] {_top_hit['title']}："
                                  f"{str(_top_hit['content'])[:400]}")
                        yield _sse({"ev": "delta", "text": answer})
                        qa_id = get_qa_log().record(answered=True, **_rec)
                        stats.record_query(answered=True, latency_ms=latency)
                        yield _sse({"ev": "done", "ms": latency,
                                    "qa_id": qa_id, "answered": True,
                                    "basis": "doc"})
                        return
                    qa_id = get_qa_log().record(answered=False, **_rec)
                    # 拒答归因分两种：检索就没命中（no_hit）vs 命中了但 LLM 判定
                    # 答不了（no_basis）。ops 卡靠这个分辨「该补语料」还是「语料
                    # 有但检索/理解不对」，混成一个数就没法照单补货了。
                    stats.record_query(
                        answered=False, latency_ms=latency,
                        refusal="no_basis" if strong else "no_hit")
                    yield _sse({"ev": "delta",
                                "text": tr(request, "asb.a.no_hit")})
                    yield _sse({"ev": "done", "ms": latency, "qa_id": qa_id,
                                "answered": False, "basis": "none"})
                    return
                # 依据分层（2026-08-29）：**答什么**与**凭什么答**必须分开告诉
                # 用户。doc＝有文档依据；product＝产品事实卡（能力边界）；
                # general＝通用知识，不是产品文档——最后这条尤其要标，否则销售
                # 会把模型随口说的当成产品承诺。
                basis = ("general" if _is_general(answer)
                         else ("product" if docless else "doc"))
                qa_id = get_qa_log().record(answered=True, **_rec)
                stats.record_query(answered=True, latency_ms=latency)
                yield _sse({"ev": "done", "ms": latency, "qa_id": qa_id,
                            "answered": True, "basis": basis})
            except asyncio.CancelledError:
                # 直连流收尾/客户端断开都可能是 CancelledError（BaseException），
                # 不接 = meta 之后静默掐流，前端只能显示「网络异常」。
                # ka/elapsed 是这条日志唯一的诊断价值所在（2026-08-28 补）：
                # ka>0 ＝连接上一直有字节在流动却照样被掐断，那就不是空闲
                # 超时，得往桌面壳/代理的别的策略查；ka=0 且耗时很短 ＝客户端
                # 自己很快走了（切页/关面板）。此前只有一句「被取消」，两种
                # 完全不同的成因分不开。
                logger.warning(
                    "[assistant] query 被取消（ka=%d, %.1fs, stage=%s）",
                    ka_sent, time.time() - t0, stage, exc_info=True)
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
            # no-transform：与 admin.py 的另一个 SSE 对齐，显式禁止中间层
            # 改写/缓冲正文（本进程的 CompressionMiddleware 已按 content-type
            # 豁免 SSE，但链路上游的代理不看那个）。
            headers={"Cache-Control": "no-cache, no-transform",
                     "X-Accel-Buffering": "no"})

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
