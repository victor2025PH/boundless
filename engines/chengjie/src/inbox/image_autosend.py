"""全自动「按需发图」（System Z autosend 的图片出站，与 ``voice_autosend`` 对称）。

统一收件箱 autosend 之前只会发**文本 / 语音**：对方在对话里要照片（"發個照片給我看看"、
"你煮的面拍张照给我看"）时，AI 只会**嘴上答应**却从不真发图——线上实测对方会质问
"你快拍啊，你是不是騙我的"。本模块给 **System Z 全自动 autosend** 补上「按客户请求出图并发出」
的能力，一处生效、全平台共用（经 ``orch.send_media(media_type="image")``）。

分工（复用既有纯逻辑，避免重复造轮子）：
- 意图判定：``companion_selfie.detect_selfie_request``（要人设自拍）/
  ``contextual_image.plan_contextual_image``（要对话里提到的东西的图）。
- 出图：``companion_selfie.SelfieProvider``——``album`` 后端从预制相册挑（人设自拍，零 API）；
  ``openai``/``command`` 后端 text2img/img2img（自拍可用相册基础图锁脸；物体图走 text2img）。
- 落盘：``protocol_bridge.save_outbound_media``（与坐席/语音出站同一出站媒体目录 → /static URL）。

**默认关**（``companion.selfie.enabled=false``）→ 全自动仍纯文本/语音，零行为变更。
任何环节失败/不满足都返回「不发图」让调用方回落文本/语音，绝不卡住全自动主流程。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_SELFIE = "selfie"
KIND_OBJECT = "object"

# ── 可观测性（进程内累计；与 voice_autosend 同风格，供 autosend-status 暴露）────────
# 只在「已判定该发图」之后计数：sent=真发出图；fallback=出图/投递失败回落文本/语音。
# caption_*=配文来源分布（llm=文图协同 LLM 配文 / registry=运营配文 / fixed=固定配文 /
# draft=草稿文本兜底），供评估 llm_caption 质量与覆盖率。
_METRICS: Dict[str, Any] = {
    "sent": 0, "fallback": 0, "last_reason": "", "last_kind": "", "last_ts": 0.0,
    "caption_llm": 0, "caption_registry": 0, "caption_fixed": 0, "caption_draft": 0,
    # Q-39 A③（#327）：配文近重复被换掉的次数（源可能是 registry / fixed / 空配文）
    "caption_dedup": 0,
    "last_caption": "",
    # 失败原因分布（与 voice_autosend.fallback_counts 同口径，供看板读 Top 原因）
    "fallback_reasons": {},
    "last_failure_detail": "",
    # 收图后质疑信号（P0 一致性观测）：客户质疑「重复/不像/假图」计数 + 末样本
    "complaints": 0, "last_complaint": "",
    # P1 分维度：质疑按类型/人设分布 + 点名场景需求（demand）与未兑现（unmet）
    "complaints_by_kind": {}, "complaints_by_persona": {},
    "scene_demand": {}, "scene_unmet": {},
}
_METRICS_LOCK = threading.Lock()

# GPU 出图 in-flight（真出图后端 generate_with_gate；album 秒发不计）
_IMAGE_GEN_INFLIGHT = 0
_IMAGE_GEN_LOCK = threading.Lock()


def image_gen_inflight() -> int:
    with _IMAGE_GEN_LOCK:
        return int(_IMAGE_GEN_INFLIGHT)


def _image_gen_begin() -> None:
    global _IMAGE_GEN_INFLIGHT
    with _IMAGE_GEN_LOCK:
        _IMAGE_GEN_INFLIGHT += 1


def _image_gen_end() -> None:
    global _IMAGE_GEN_INFLIGHT
    with _IMAGE_GEN_LOCK:
        _IMAGE_GEN_INFLIGHT = max(0, _IMAGE_GEN_INFLIGHT - 1)


def _bump_counter(bucket: Dict[str, int], key: str) -> None:
    k = str(key or "").strip() or "unknown"
    bucket[k] = int(bucket.get(k, 0)) + 1


def record_caption(source: str, caption: str = "") -> None:
    key = f"caption_{source}"
    with _METRICS_LOCK:
        if key in _METRICS:
            _METRICS[key] = int(_METRICS[key]) + 1
        if caption:
            _METRICS["last_caption"] = str(caption)[:80]


def record_image_sent(kind: str = "", source: str = "") -> None:
    """发图成功计数。``source``（Phase19 观察维度）＝意图判定来源：
    llm_directive（LLM [PHOTO 标记）/ keyword（关键词生成链）/ registry（注册相册）
    ——看板据此观察「LLM 决策 vs 关键词兜底」占比，指导 hybrid → llm 收敛节奏。

    2026-08-19 Token 计量（P5b，licensing.token_ledger.enabled 默认关=零行为）：
    只对**现场生成**的图记 ai_image（50 Token/张）——source 带 _album 后缀（相册
    存货顶上）/ registry（注册相册）/ bazi_kline（本地 PIL 渲染，产品决策免费引流）
    都是免费路径不计，与「预渲染语音/缓存命中不计费」同一原则。B 线单点覆盖；
    A 线自拍链（skill_manager，旗舰本地经济学）刻意不接，P6 再议。
    """
    try:
        if source in ("llm_directive", "keyword"):
            from src.licensing.token_ledger import record_action_for_status

            record_action_for_status("ai_image", 1)
    except Exception:
        pass
    with _METRICS_LOCK:
        _METRICS["sent"] = int(_METRICS["sent"]) + 1
        _METRICS["last_kind"] = str(kind or "")
        if source:
            k = "src_" + str(source)
            _METRICS[k] = int(_METRICS.get(k, 0) or 0) + 1
        _METRICS["last_ts"] = time.time()


def record_image_fallback(reason: str, *, detail: str = "") -> None:
    r = str(reason or "").strip() or "unknown"
    with _METRICS_LOCK:
        _METRICS["fallback"] = int(_METRICS["fallback"]) + 1
        _METRICS["last_reason"] = r
        _bump_counter(_METRICS.setdefault("fallback_reasons", {}), r)
        if detail:
            _METRICS["last_failure_detail"] = str(detail)[:120]
        _METRICS["last_ts"] = time.time()


def record_media_complaint(
    detail: str = "", *, kind: str = "", persona_id: str = "",
) -> None:
    """「图被客户质疑」信号计数（P0 观测补盲，2026-07-27；P1 分维度）。

    客户在收图后短窗口内发出「骗人/假的/网图/怎么又是这张/衣服怎么变了」类
    质疑 → 这里+1 并留样本——线上图文一致性劣化的**第一现场信号**（此前只能
    人工翻聊天记录发现）。经 autosend-status / metrics 出口供看板读。
    ``kind``（repeat/not_you/fake，缺省从 detail 的 ``kind:`` 前缀解析）与
    ``persona_id`` 分维计数（P1 看板：哪个人设、哪类质疑最多）。
    """
    k = str(kind or "").strip()
    if not k and ":" in str(detail or ""):
        k = str(detail).split(":", 1)[0].strip()
    with _METRICS_LOCK:
        _METRICS["complaints"] = int(_METRICS.get("complaints", 0) or 0) + 1
        if k:
            _bump_counter(_METRICS.setdefault("complaints_by_kind", {}), k)
        if persona_id:
            _bump_counter(
                _METRICS.setdefault("complaints_by_persona", {}),
                str(persona_id)[:40])
        if detail:
            _METRICS["last_complaint"] = str(detail)[:120]
        _METRICS["last_ts"] = time.time()


def record_scene_request(scene: str, *, unmet: bool) -> None:
    """「点名场景」需求计数（P1 补货闭环的需求侧）。

    客户/承诺点名了具体场景（硬要求）→ 按归一场景类记需求；最终没能出
    该场景的图（相册无匹配+生成失败/未启用）→ 追记 unmet——「客户要了但
    库里没有」的真实需求排序，直接驱动相册补货清单（与库存缺口报告 join）。
    """
    try:
        from src.companion.persona_media import scene_class_of
        cls = scene_class_of(scene) or "other"
    except Exception:
        cls = "other"
    with _METRICS_LOCK:
        _bump_counter(_METRICS.setdefault("scene_demand", {}), cls)
        if unmet:
            _bump_counter(_METRICS.setdefault("scene_unmet", {}), cls)
        _METRICS["last_ts"] = time.time()
    # P3 2026-08-22：需求侧落库（跨重启记忆——本机日均 8+ 次重启把进程计数
    # 清成摆设）。**peek 不懒建**：生产单例早被媒体链建过=写得进；测试进程
    # 没建过=零磁盘写（防「测试写仓库 config/」）。绝不因账本失败影响计数。
    if cls != "other":
        try:
            from src.companion.persona_media_store import peek_persona_media_store
            st = peek_persona_media_store()
            if st is not None:
                st.record_scene_demand(cls, unmet=unmet)
        except Exception:
            logger.debug("[image_autosend] 场景需求落账失败（已忽略）", exc_info=True)


def record_promise_event(name: str) -> None:
    """出站媒体承诺守卫事件计数（detected/fulfilled/retracted/offer_accept…）。

    与 sent/fallback 同一快照出口（autosend-status / metrics），供看板读
    「承诺兑现率」——文本承诺发图后真发出去的占比。"""
    key = "promise_" + str(name or "").strip()
    with _METRICS_LOCK:
        _METRICS[key] = int(_METRICS.get(key, 0) or 0) + 1
        _METRICS["last_ts"] = time.time()
    # Phase22c：按日趋势落库（默认关；启用后单一 choke point 旁路写入，绝不阻塞承诺流）
    try:
        from src.inbox.media_promise_trend_store import record_media_promise_trend
        record_media_promise_trend(str(name or "").strip())
    except Exception:
        pass


def record_sent_claim_event(name: str) -> None:
    """「已发假声明」守卫事件计数（#171：detected/fulfilled/retracted；与 promise_*
    同族、同一快照出口）。快照键 ``sent_claim_<name>``——「AI 说『刚发了』而近窗
    没真发」被抓了多少、其中多少补成真图、多少改成诚实文本。"""
    key = "sent_claim_" + str(name or "").strip()
    with _METRICS_LOCK:
        _METRICS[key] = int(_METRICS.get(key, 0) or 0) + 1
        _METRICS["last_ts"] = time.time()


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        snap = dict(_METRICS)
        for k in ("fallback_reasons", "complaints_by_kind",
                  "complaints_by_persona", "scene_demand", "scene_unmet"):
            snap[k] = dict(_METRICS.get(k) or {})
    snap["gen_inflight"] = image_gen_inflight()
    # 出站前图文核对（P2）：核过几张、改写过几条、软放行原因分布。
    try:
        from src.ai.caption_image_guard import stats_snapshot as _cg
        snap["caption_guard"] = _cg()
    except Exception:
        pass
    return snap


def resolve_image_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``companion.selfie`` 块（与 ``skill_manager._selfie_cfg`` 同口径；缺失返回 {}）。

    随块捎带 ``_weather_cfg``＝``companion.weather``（2026-08-18 天气匹配生活照）：
    ``stage_image_file`` 只收 scfg 不收整份 config，天气闸门/坐标解析要用它——
    单点捎带让全部调用方零改动获得天气接线。下划线前缀=内部键，勿进 example。
    """
    try:
        comp = (config or {}).get("companion") or {}
        sc = comp.get("selfie")
        out = dict(sc) if isinstance(sc, dict) else {}
        wx = comp.get("weather")
        if isinstance(wx, dict) and out:
            out["_weather_cfg"] = dict(wx)
        return out
    except Exception:
        return {}


def plan_autosend_image(
    peer_text: str,
    history: Optional[List[Dict[str, Any]]],
    scfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """按客户最近一条入站文本判断该发什么图（纯函数）。None=不发图（回落文本/语音）。

    - 命中 ``image_send_gate.explicit_photo_ask``（索要我方照片）→ ``{kind: "selfie"}``
      （人设自拍，可走相册）；只是谈到照片不算。
    - 否则开了 ``contextual_images`` 且命中 ``plan_contextual_image`` →
      ``{kind: "object", subject, prompt}``（对话里提到的东西，需真出图后端）。
    自拍优先于物体图（``detect_selfie_request`` 已把"你煮的…"排除，二者互斥不重叠）。
    """
    if not scfg or not bool(scfg.get("enabled", False)):
        return None
    pt = str(peer_text or "")
    if not pt.strip():
        return None
    try:
        from src.ai.companion_selfie import extract_requested_scene
        from src.inbox.image_send_gate import explicit_photo_ask
        if explicit_photo_ask(pt):
            # 客户显式点名场景（"发张你在海边的照片"）→ 随 directive 覆盖轮换场景。
            return {"kind": KIND_SELFIE,
                    "scene": extract_requested_scene(pt)}
    except Exception:
        logger.debug("[image_autosend] selfie 意图判定异常", exc_info=True)
    if bool(scfg.get("contextual_images", False)):
        try:
            from src.ai.contextual_image import plan_contextual_image
            plan = plan_contextual_image(pt, history, style=str(scfg.get("style") or ""))
            if plan:
                return {"kind": KIND_OBJECT, "subject": str(plan.get("subject") or ""),
                        "prompt": str(plan.get("prompt") or "")}
        except Exception:
            logger.debug("[image_autosend] 上下文要图判定异常", exc_info=True)
    return None


def _resolve_persona(persona_id: str) -> Any:
    """取出图用 persona（dict 含 name/appearance 等）；拿不到则回 persona_id 字符串/空。"""
    try:
        if persona_id:
            from src.utils.persona_manager import PersonaManager
            p = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
            if isinstance(p, dict):
                return p
    except Exception:
        logger.debug("[image_autosend] persona 解析失败", exc_info=True)
    return str(persona_id or "")


def _album_key_for(persona_id: str) -> str:
    """album 分册键：多人设时用 persona id/name 选 ``album_dir/<key>`` 子目录；缺则空（用根）。"""
    p = _resolve_persona(persona_id)
    if isinstance(p, dict):
        return str(p.get("id") or p.get("persona_id") or p.get("name") or "").strip()
    return str(p or "").strip()


# 文件系统相册「服装连续性」账本（P0）：conv_key → (series, ts)。
# 注册相册（DB）走 persona_media_store.record_send 持久账本；文件系统相册无 DB，
# 用进程内 bounded LRU 兜底——重启丢失可接受（连续窗本就只有 ~90min）。
_ALBUM_SERIES: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
_ALBUM_SERIES_CAP = 4000
_ALBUM_SERIES_LOCK = threading.Lock()


def _album_series_note(conv_key: str, series: str) -> None:
    if not conv_key or not series:
        return
    with _ALBUM_SERIES_LOCK:
        _ALBUM_SERIES[str(conv_key)] = (str(series), time.time())
        _ALBUM_SERIES.move_to_end(str(conv_key))
        while len(_ALBUM_SERIES) > _ALBUM_SERIES_CAP:
            _ALBUM_SERIES.popitem(last=False)


def _album_series_recent(conv_key: str, *, within_minutes: float) -> str:
    """连续窗内本会话最近一次相册出图的 series（过窗/无记录返 ""）。"""
    if not conv_key or within_minutes <= 0:
        return ""
    with _ALBUM_SERIES_LOCK:
        ent = _ALBUM_SERIES.get(str(conv_key))
    if not ent:
        return ""
    series, ts = ent
    if (time.time() - float(ts)) > within_minutes * 60.0:
        return ""
    return str(series or "")


async def stage_image_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    persona_id: str,
    directive: Dict[str, Any],
    *,
    llm_refine: Optional[Callable[[], Awaitable[str]]] = None,
    conv_key: str = "",
) -> Optional[Tuple[str, str, str, Dict[str, Any]]]:
    """按 ``directive`` 出图并落到出站媒体目录，返回 ``(本地路径, /static URL, kind,
    info)``；失败/不满足返回 None。

    调用方据此 ``orch.send_media(media_path=local, media_url=url, media_type="image")``。
    - selfie：``album`` 后端挑现成图；``openai``/``command`` 后端 build_selfie_prompt + 相册基础图 img2img。
    - object：仅真出图后端（非 disabled/album）；可选 ``llm_refine`` 把 prompt 提炼得更准。

    P0 图文一致性（2026-07-27）：
    - ``info``＝真实来源记账 ``{"provider", "fallback_from", "series",
      "scene_class", "tod_softened"}``——相册兜底冒充「生成」曾把观测口径
      全部带偏（配文按"刚拍"写、相册旧照被再入册），调用方按 info 对齐配文
      口径（**provider=album → 旧照口径**；``fallback_from``=失败的主后端名，
      非空即「相册是生成失败的兜底」）与计数。
    - ``directive["scene_strict"]``＝场景硬要求（客户/LLM/媒体日志显式点名）：
      相册兜底必须场景类匹配，挑不到 → 如实失败。
    - **object 绝不回落相册**（要海景发人像自拍=实录「你真会骗人」事故）。
    - ``conv_key`` 非空＝启用文件系统相册的**服装连续性**（同会话连续窗内偏好
      同一 series 子目录——十分钟前海边连衣裙、现在居家睡衣的「瞬移换装」收口）。
    """
    scfg = resolve_image_autosend_cfg(config)
    try:
        from src.ai.companion_selfie import (
            build_selfie_prompt, get_selfie_provider, resolve_persona_lora,
            resolve_variety_salt, stable_selfie_seed,
        )
        provider = get_selfie_provider(scfg.get("provider") or {})
    except Exception:
        logger.debug("[image_autosend] provider 构造失败", exc_info=True)
        return None
    backend = str(getattr(provider, "backend", "")).lower()
    if not bool(getattr(provider, "enabled", False)) or backend in ("", "disabled"):
        return None
    kind = str((directive or {}).get("kind") or "")
    # R87 #329a（3TCW5P）：真出图后端 + 人设有真人相册且未显式开 capabilities.photo_generate
    # → 不生成（相册挑不到就交诚实文字），绝不用一张生成脸冒充相册里的人。
    # 所有生成路径（0a [PHOTO] 指令 / 2) 回落生成 / 1b 物体图）都经这里，一处即全覆盖。
    if backend != "album":
        try:
            from src.companion.photo_capability import persona_generate_allowed_by_id
            _gen_ok = persona_generate_allowed_by_id(persona_id)
        except Exception:
            _gen_ok = True
        if not _gen_ok:
            record_image_fallback("gen_disabled", detail=kind)
            logger.info("[album_send] skip=gen_disabled persona=%s kind=%s conv=%s：人设有真人相册且"
                        "未开「AI 生成」→ 不出生成图，交诚实文字", persona_id, kind, conv_key or "-")
            return None
    album_key = _album_key_for(persona_id)
    # 出图预算护栏（护 API 账单，与 process_message 自拍/上下文共用同一份全局跟踪器）：
    # 仅真出图后端(openai/command)计数——album 挑现成图零成本不计。达上限→回落不发（不烧钱）。
    _tracker = None
    if backend != "album":
        try:
            _cap = int(scfg.get("daily_global_cap", 0) or 0)
        except Exception:
            _cap = 0
        if _cap > 0:
            try:
                from src.utils.selfie_cap import get_selfie_cap_tracker
                _tracker = get_selfie_cap_tracker(_cap)
            except Exception:
                _tracker = None
            if _tracker is not None and _tracker.would_exceed(1):
                logger.info("[image_autosend] daily_global_cap=%d 已达上限，回落不发", _cap)
                record_image_fallback("global_cap")
                return None
    # 一致性护栏参数（P0）：场景硬要求 + 当前时段 + 服装连续性（相册两路共用）。
    cc = resolve_consistency_cfg(scfg)
    _scene_req = ""
    if bool((directive or {}).get("scene_strict")):
        _scene_req = str((directive or {}).get("scene") or "").strip()
    _now_hour: Optional[int] = cc["now_hour"]
    _prefer_series = str((directive or {}).get("prefer_series") or "")
    if (not _prefer_series and conv_key
            and float(cc["continuity_minutes"] or 0) > 0):
        _prefer_series = _album_series_recent(
            conv_key, within_minutes=float(cc["continuity_minutes"]))
    _outfit = ""  # 生成链今日衣着（P1）；相册/物体路径恒空

    def _scene_outcome(met: bool) -> None:
        """点名场景的最终结局计数（P1 补货需求侧；无点名=零开销）。"""
        if _scene_req:
            record_scene_request(_scene_req, unmet=(not met))

    res = None
    try:
        if kind == KIND_SELFIE:
            if backend == "album":
                res = await provider.generate(
                    "", album_key=album_key, album_scene=_scene_req,
                    now_hour=_now_hour, prefer_series=_prefer_series,
                    # 实施69 P1：非硬要求的 directive 场景（轮换/LLM 软场景）
                    # 作软偏好——有匹配存货贴场景，没有绝不拒发（与 A 线同口径）。
                    prefer_scene=("" if _scene_req else str(
                        (directive or {}).get("scene") or "").strip()))
            else:
                persona = _resolve_persona(persona_id)
                # 天气快照（2026-08-18）：轮换池滤天气冲突场景（暴雨×沙滩）+
                # 生图 prompt 强天气氛围。闸门/坐标缺失 → None＝逐位旧行为。
                _wx_snap = None
                try:
                    from src.companion.weather_state import snap_for_persona
                    _wx_snap = snap_for_persona(
                        persona, scfg.get("_weather_cfg"))
                except Exception:
                    _wx_snap = None
                # 场景：directive 可显式指定（proactive 文案-场景对齐用，Phase17）；
                # 否则按日期/时段/相册张数从场景池轮换（人设 selfie_scenes →
                # config scene_rotation → 固定 scene_hint）。同种子下场景变、人设不变。
                scene = str((directive or {}).get("scene") or "").strip()
                if not scene:
                    from src.ai.companion_selfie import pick_scene_hint
                    scene = pick_scene_hint(
                        persona,
                        default_scene=str(scfg.get("scene_hint") or ""),
                        fallback_scenes=scfg.get("scene_rotation"),
                        salt=_auto_photo_count(persona_id),
                        weather_snap=_wx_snap,
                    )
                # 今日衣着状态（P1，与 A 线/聊天注入同 key 同函数）：连续窗内
                # 刚发过照片 → 跟随其系列；否则今日确定性衣着。发出成功后以
                # slug 记连续性账本（下条相册挑图也优先同系列——跨链跟装）。
                # P2：传本次场景做 场景×季节 适配。
                _outfit = ""
                try:
                    from src.companion.outfit_state import current_outfit
                    _outfit = current_outfit(
                        persona, scfg,
                        persona_key=(persona_id or album_key),
                        recent_series=_prefer_series, scene=scene)["outfit"]
                except Exception:
                    _outfit = ""
                # 多样性 salt（治头位置/表情千篇一律，默认关，overlay opt-in）：开时
                # 每次发送取随机 salt → prompt 姿态/表情/取景 + 底噪一起变，同一人不同瞬间。
                _vsalt = resolve_variety_salt(scfg)
                # per-persona 角色 LoRA spec（file/weight/trigger）：多人设各自 LoRA。
                _lora = resolve_persona_lora(persona, scfg)
                # 时段光线兜底（P1-2）：轮换场景无时间词时补当前时段光线（凌晨
                # 不出正午烈日照）；只进生图 prompt，scene 原文留给场景记录。
                from src.ai.companion_selfie import ensure_time_of_day
                # 人设级 appearance 优先（多人设各有长相），config 全局 appearance 兜底。
                prompt = build_selfie_prompt(
                    persona,
                    scene_hint=ensure_time_of_day(scene, weather_snap=_wx_snap),
                    style=str(scfg.get("style") or ""),
                    default_appearance=str(scfg.get("appearance") or ""),
                    content_rating=str(scfg.get("content_rating") or ""),
                    variety_salt=_vsalt,
                    lora_trigger=_lora["trigger"],
                    outfit=_outfit,
                )
                base = ""
                try:
                    base = provider.reference_image(album_key)
                except Exception:
                    base = ""
                if _tracker is not None:
                    _tracker.record_sent(1)
                # 固定种子（按人设派生）：同一人设自拍共享噪声基线，外观漂移显著减小；
                # 配 scene_hint 变化仍有画面差异。config 置 selfie.stable_seed:false 可关。
                # 开 variety 时掺入 salt → 同人设每次底噪不同（构图各异，身份靠 PuLID/LoRA）。
                _seed = -1
                if bool(scfg.get("stable_seed", True)):
                    _seed = stable_selfie_seed(persona_id or album_key, salt=(_vsalt or 0))
                logger.info("[image_autosend] selfie prompt=%r seed=%s base=%s",
                            prompt, _seed, bool(base))
                # 出图自检闸门（vision_gate）：发出前 VLM 体检（单人/性别年龄贴人设/
                # 无水印/非动物），不合格换种子重试，仍不合格回落——狗图事故的最后防线。
                from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
                _image_gen_begin()
                try:
                    res = await generate_with_gate(
                        provider, prompt, persona=persona, root_config=config,
                        gate_cfg=resolve_gate_cfg(scfg), seed=_seed,
                        expect_scene=_scene_req, expect_hour=_now_hour,
                        album_key=album_key, base_image=base,
                        lora=_lora["file"], lora_weight=_lora["weight"],
                        album_scene=_scene_req, now_hour=_now_hour,
                        prefer_series=_prefer_series)
                    # 像素级换脸（可选，默认关）：把 face_ref 的脸贴到生成图上——同一张脸
                    # 一致性远强于 PuLID。已通过体检的生成图才换；换完补一次体检，
                    # 不过则弃换脸结果、保留原生成图（换脸失败绝不劣化已合格的图）。
                    try:
                        from src.ai.face_swap import maybe_swap_face, resolve_face_swap_cfg
                        _fs_cfg = resolve_face_swap_cfg(scfg)
                        if (res is not None and getattr(res, "ok", False)
                                and getattr(res, "image_path", "") and base
                                and bool(_fs_cfg.get("enabled", False))):
                            _orig_path = res.image_path
                            _swapped = await maybe_swap_face(_orig_path, base, _fs_cfg)
                            if _swapped and _swapped != _orig_path:
                                _keep = True
                                try:
                                    from src.ai.image_gate import (
                                        check_image, resolve_gate_cfg as _rg,
                                    )
                                    _gc = _rg(scfg)
                                    if bool(_gc.get("enabled", True)):
                                        _ok2, _rsn2 = await check_image(
                                            _swapped, persona, config,
                                            age_tolerance=int(_gc.get("age_tolerance", 18) or 18),
                                            content_rating=str(_gc.get("content_rating") or "sfw"))
                                        _keep = _ok2
                                        if not _ok2:
                                            logger.info(
                                                "[image_autosend] 换脸后体检不过(%s)，保留原生成图",
                                                _rsn2)
                                except Exception:
                                    _keep = True  # 体检异常不否定换脸（软放行同口径）
                                if _keep:
                                    res.image_path = _swapped
                    except Exception:
                        logger.debug("[image_autosend] 换脸阶段异常（用原图）", exc_info=True)
                finally:
                    _image_gen_end()
        elif kind == KIND_OBJECT:
            if backend == "album":
                # 相册无法凭空生成任意物体图 → 回落（不发图）。
                _scene_outcome(False)
                return None
            prompt = str((directive or {}).get("prompt") or "")
            if bool(scfg.get("contextual_images_llm_prompt", False)) and callable(llm_refine):
                try:
                    refined = str(await llm_refine() or "").strip().strip('"').strip()
                    if refined and len(refined) <= 400:
                        prompt = refined
                except Exception:
                    logger.debug("[image_autosend] prompt LLM 精炼跳过", exc_info=True)
            if not prompt.strip():
                return None
            if _tracker is not None:
                _tracker.record_sent(1)
            # 体检主体：启发式 subject（客户原话的东西，防 LLM 精炼跑题）优先；
            # 抽不出时回落用最终 prompt 片段——保证闸门始终有"期望描述"可校验。
            _subject = str((directive or {}).get("subject") or "").strip()
            if not _subject:
                _subject = prompt.strip()[:80]
            logger.info("[image_autosend] object prompt=%r subject=%r", prompt, _subject)
            # 物体图走 text2img（不带人设的脸）+ 主体匹配体检（要蛋糕别发面条）。
            # P0：object 生成失败**绝不回落相册人像**（要海景/抹茶拿铁发车内自拍
            # =实录「你真会骗人」事故）——如实失败，调用方走承诺撤回/诚实文字。
            from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
            _image_gen_begin()
            try:
                res = await generate_with_gate(
                    provider, prompt, root_config=config,
                    gate_cfg=resolve_gate_cfg(scfg), seed=-1,
                    kind="object", subject=_subject,
                    allow_album_fallback=False)
            finally:
                _image_gen_end()
        else:
            return None
    except Exception:
        logger.debug("[image_autosend] 出图异常", exc_info=True)
        _scene_outcome(False)
        return None
    if not (res is not None and getattr(res, "ok", False) and getattr(res, "image_path", "")):
        _scene_outcome(False)
        return None
    try:
        with open(res.image_path, "rb") as fh:
            data = fh.read()
    except Exception:
        logger.debug("[image_autosend] 读取出图文件失败", exc_info=True)
        _scene_outcome(False)
        return None
    if not data:
        _scene_outcome(False)
        return None
    _extra = dict(getattr(res, "extra", {}) or {})
    info: Dict[str, Any] = {
        "provider": str(getattr(res, "provider", "") or "").lower(),
        "fallback_from": str(_extra.get("fallback_from") or ""),
        "series": str(_extra.get("series") or ""),
        "scene_class": str(_extra.get("scene_class") or ""),
        "tod_softened": bool(_extra.get("tod_softened", False)),
    }
    # 生成图的「系列」＝本次注入的今日衣着 slug（P1）：与相册策展系列同一命名
    # 空间，连续窗内下条（相册挑图/再生成）都能跟住这身衣服。
    if kind == KIND_SELFIE and not info["series"] and info["provider"] != "album":
        try:
            from src.companion.outfit_state import outfit_slug
            info["series"] = outfit_slug(_outfit)
        except Exception:
            pass
    # 服装连续性账本：出图记住本会话用的 series（相册系列/生成衣着），
    # 连续窗内下次优先同系列。
    if conv_key and info["series"]:
        _album_series_note(conv_key, info["series"])
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(res.image_path), data)
        _scene_outcome(True)
        return (local, url, kind, info)
    except Exception:
        logger.debug("[image_autosend] 落出站媒体失败", exc_info=True)
        _scene_outcome(False)
        return None


# ── 注册相册（DB）优先：关键词命中/通用池秒发预制图/视频，零生成成本 ──────────
# 每会话「上一条发出的注册媒体 id」——加权轮播时避重（bounded LRU，防连发同一张）。
_LAST_SENT: "OrderedDict[str, str]" = OrderedDict()
_LAST_SENT_CAP = 4000
_LAST_SENT_LOCK = threading.Lock()


def last_media_sent(conv_key: str) -> str:
    with _LAST_SENT_LOCK:
        return _LAST_SENT.get(str(conv_key or ""), "")


def note_media_sent(conv_key: str, media_id: str) -> None:
    if not conv_key or not media_id:
        return
    with _LAST_SENT_LOCK:
        _LAST_SENT[str(conv_key)] = str(media_id)
        _LAST_SENT.move_to_end(str(conv_key))
        while len(_LAST_SENT) > _LAST_SENT_CAP:
            _LAST_SENT.popitem(last=False)


# ── #259 P-3 真发回执账本（每会话最近一次 send_media 的平台回执）──────────────
# 「真发成功」= 平台回执（``delivered=True`` + message_id，M-2 D 口径），不是
# dispatch。记 {media_id, path, url, media_type, mid, ts}：C 段 lie_caught 固定动作
# 「重发上一张」直接拿 path/url 重发同一文件（含生成图——它没有相册 id，旧账本
# ``_LAST_SENT`` 只记相册条目）；E 段「5 分钟内第二句配文体必附图」按 ts 判。
_LAST_RECEIPT: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _send_result(res: Any) -> Tuple[bool, str]:
    """``send_fn`` 返回值归一：bool（旧契约）或 ``{"delivered", "message_id"}``
    （回执契约）→ ``(delivered, mid)``。"""
    if isinstance(res, dict):
        return bool(res.get("delivered")), str(res.get("message_id") or "")
    return bool(res), ""


def note_media_receipt(
    conv_key: str, *, media_id: str = "", path: str = "", url: str = "",
    media_type: str = "image", mid: str = "", now: Optional[float] = None,
    sha256: str = "",
) -> None:
    if not conv_key or not (path or url):
        return
    digest = str(sha256 or "").strip()
    if not digest and path:
        try:
            from src.companion.media_ownership import sha256_path as _sha
            digest = _sha(path)
        except Exception:
            digest = ""
    rec = {
        "media_id": str(media_id or ""), "path": str(path or ""),
        "url": str(url or ""), "media_type": str(media_type or "image"),
        "mid": str(mid or ""),
        "sha256": digest,
        "ts": float(now if now is not None else time.time()),
    }
    with _LAST_SENT_LOCK:
        _LAST_RECEIPT[str(conv_key)] = rec
        _LAST_RECEIPT.move_to_end(str(conv_key))
        while len(_LAST_RECEIPT) > _LAST_SENT_CAP:
            _LAST_RECEIPT.popitem(last=False)


def last_media_receipt(conv_key: str) -> Dict[str, Any]:
    # Q-20 D 桥：persona_reply 在组 prompt 前恰以本会话调一次本函数（Q-6 C/D 的
    # media_pending 判定），生成侧附加段（prompt_addenda）无会话参数时从这里取
    # 「当前正在为哪个会话组 prompt」——见 bind_prompt_conv。
    bind_prompt_conv(conv_key)
    with _LAST_SENT_LOCK:
        return dict(_LAST_RECEIPT.get(str(conv_key or ""), {}) or {})


# ── Q-20 D（#178 VDUJX6 / PQE9ZF · 2026-09-11）：刚发的图必须认领 ─────────────────
# 实录：坐席人工发了一张照片，3 分钟后客户问「是你吗」，AI 走「相册无图 → 拒绝」分支
# 回「我没这个功能」。根因两处：① 人工发的媒体不在相册投放账本（persona_media_sends）
# 与进程回执账本里，生成侧「本会话已发媒体」段据此写死「你从未给 TA 发过照片」；
# ② 「是你的照片吗」被当成新的索图请求 → 相册无匹配 → 「只许自然拒绝」。
# 修法：以 **inbox 消息表**（人工 / AI 出站媒体行都在）+ 两本账本合并判「30 分钟内
# 我方发过媒体」，生成侧改注入「刚发过一张（描述）——TA 问是不是你就认领」。
RECENT_MEDIA_CLAIM_SEC = 30 * 60.0

_PROMPT_CONV_TTL_SEC = 20.0
try:
    import contextvars as _contextvars
    _PROMPT_CONV: "Any" = _contextvars.ContextVar(
        "image_autosend_prompt_conv", default=("", 0.0))
except Exception:  # pragma: no cover
    _PROMPT_CONV = None


def bind_prompt_conv(conv_key: str) -> None:
    """记「当前任务正在为哪个会话组 prompt」（ContextVar，同一 asyncio task / 线程内可见，
    TTL 20s）。Q-20 D 桥：``persona_reply._prompt_addenda`` 的 ``sent_media_addendum`` /
    ``album_miss_addendum`` 调用点没有会话参数、且本线不动 persona_reply——它们在没被
    显式传 ``conv_key`` 时读 :func:`bound_prompt_conv`。Q-6 把 ``conv_key=ck`` 传进去后
    本桥即可拆。软失败。"""
    try:
        if _PROMPT_CONV is not None:
            _PROMPT_CONV.set((str(conv_key or "").strip(), time.monotonic()))
    except Exception:
        pass


def bound_prompt_conv(max_age_sec: float = _PROMPT_CONV_TTL_SEC) -> str:
    """最近 ``max_age_sec`` 内绑定的会话 key；过期 / 未绑定 → ""。"""
    try:
        if _PROMPT_CONV is None:
            return ""
        ck, t0 = _PROMPT_CONV.get()
        if not ck or (time.monotonic() - float(t0 or 0)) > float(max_age_sec):
            return ""
        return str(ck)
    except Exception:
        return ""


_MEDIA_TYPES_VISUAL = ("image", "photo", "picture", "sticker_image", "video", "gif")


def _row_is_outbound_media(row: Dict[str, Any]) -> str:
    """inbox 消息行是否我方发出的图 / 视频（人工或 AI）；返回归一 media_type 或 ""。"""
    try:
        if str(row.get("direction") or "") not in ("out", "outbound"):
            return ""
        if row.get("revoked") or str(row.get("deleted_by") or "") == "peer":
            return ""
        mt = str(row.get("media_type") or "").strip().lower()
        ref = str(row.get("media_ref") or row.get("media_url") or "").strip()
        if mt in ("video", "gif"):
            return "video"
        if mt in _MEDIA_TYPES_VISUAL or (not mt and ref and re.search(
                r"\.(?:jpe?g|png|webp|gif|heic|mp4|mov)(?:\?|$)", ref, re.I)):
            return "image"
        return ""
    except Exception:
        return ""


def recent_outbound_media(
    conv_key: str, *, within_sec: float = RECENT_MEDIA_CLAIM_SEC,
    now: Optional[float] = None, store: Any = None,
) -> Dict[str, Any]:
    """会话 ``within_sec`` 内**我方（人工或 AI）**最近一次发出的媒体。

    三源合并取最新：① 进程回执账本（AI autosend 真发）；② inbox 消息表出站媒体行
    （人工手发 / 任何链的出站都在这里——VDUJX6 的人工发图只有它记得）；③ 相册投放账本
    （跨重启）。返回 ``{ts, age_sec, media_type, source∈receipt/store/album, caption}``；
    没有 → ``{}``。绝不抛。
    """
    ck = str(conv_key or "").strip()
    if not ck:
        return {}
    ts_now = float(now if now is not None else time.time())
    best: Dict[str, Any] = {}

    def _consider(ts: float, mt: str, source: str, caption: str = "") -> None:
        nonlocal best
        if ts <= 0 or (ts_now - ts) > float(within_sec) or ts > ts_now + 120:
            return
        if not best or ts > float(best.get("ts") or 0):
            best = {"ts": ts, "age_sec": max(0.0, ts_now - ts),
                    "media_type": mt or "image", "source": source,
                    "caption": str(caption or "")[:80]}

    try:
        rec = last_media_receipt_peek(ck)
        if rec:
            _consider(float(rec.get("ts") or 0), str(rec.get("media_type") or "image"),
                      "receipt")
    except Exception:
        pass
    try:
        st = store
        if st is None:
            from src.integrations.protocol_bridge import get_inbox_store
            st = get_inbox_store()
        if st is not None and hasattr(st, "list_recent_messages"):
            rows = st.list_recent_messages(ck, limit=30) or []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                mt = _row_is_outbound_media(r)
                if mt:
                    _consider(float(r.get("ts") or 0), mt, "store",
                              str(r.get("text") or ""))
    except Exception:
        logger.debug("[image_autosend] recent_outbound_media store 查询失败", exc_info=True)
    try:
        from src.companion.persona_media_store import get_persona_media_store
        pms = get_persona_media_store()
        if pms is not None:
            items = list((pms.sent_history(ck, max_age_days=1) or {}).get("items") or [])
            for it in items:
                _consider(float((it or {}).get("ts") or 0), "image", "album")
    except Exception:
        pass
    return best


def last_media_receipt_peek(conv_key: str) -> Dict[str, Any]:
    """同 ``last_media_receipt`` 但**不**触发 prompt 会话绑定（内部查询用）。"""
    with _LAST_SENT_LOCK:
        return dict(_LAST_RECEIPT.get(str(conv_key or ""), {}) or {})


# 「这是你吗 / 像 P 的 / 是你的照片吗」——对**刚收到的图**的身份追问（与 companion_selfie
# 的 not_you / fake 质疑词表同族，这里只做「刚发过图 → 认领」的判定，不计投诉）。
_MEDIA_IDENTITY_Q_RE = re.compile(
    r"(?:这|這|那)?(?:是|係|系)\s*(?:你|妳|您)\s*(?:吗|嗎|么|麼|不|嘅|咩|呀|啊|嗎\?|\?|？)|"
    r"(?:是|係)\s*(?:你|妳)\s*(?:本人|自己|的照片|的相片|的自拍|的图|的圖)|"
    r"(?:你|妳)\s*(?:本人|真人)\s*(?:吗|嗎|么|麼|\?|？)|"
    r"(?:真的|真係|真系)\s*(?:是|係)\s*(?:你|妳)|"
    r"(?:像|好像|似|好似)\s*(?:P|p|ps|PS|修|修图|修圖|加工|滤镜|濾鏡)|"
    r"(?:P|p)\s*(?:的|過|过)\s*(?:吧|嗎|吗|\?|？)|"
    r"\b(?:is|was)\s+(?:that|this|it)\s+(?:really\s+)?(?:you|u)\b|"
    r"\b(?:that|this)\s+(?:you|u)\s*\?|"
    r"\b(?:is\s+)?(?:that|this|it)\s+(?:really\s+)?(?:your|ur)\s+(?:pic|picture|photo|selfie|face)\b|"
    r"\b(?:looks?|seems?)\s+(?:like\s+)?(?:photoshopped|edited|filtered|a\s+filter|ai|fake)\b|"
    r"\bfor\s+real\s*\?|\breally\s+you\b|\bthat'?s\s+(?:really\s+)?(?:you|u)\b|"
    r"(?:これ|それ|あれ)\s*(?:は)?\s*(?:あなた|君|きみ|お前)\s*(?:なの|ですか|\?|？)|"
    r"(?:本人|加工|修正)\s*(?:なの|ですか|\?|？)",
    re.IGNORECASE)


def is_media_identity_question(text: str) -> bool:
    """客户是否在追问「刚发的图是不是你 / 是不是 P 的」（纯函数）。"""
    t = str(text or "").strip()
    if not t or len(t) > 160:
        return False
    return bool(_MEDIA_IDENTITY_Q_RE.search(t))


def recent_media_claim_note(
    conv_key: str, *, lang: str = "zh", inbound: str = "",
    within_sec: float = RECENT_MEDIA_CLAIM_SEC, recent: Optional[Dict[str, Any]] = None,
    store: Any = None, now: Optional[float] = None,
) -> str:
    """「刚发过一张——TA 问是不是你就认领」注入句（Q-20 D）。

    ``recent`` 显式给（测试 / 调用方已查）优先，否则 :func:`recent_outbound_media`；
    窗口外 / 没发过 → ""。``inbound`` 命中身份追问时措辞更硬（「本条就是在问这个」）。
    """
    rec = recent if recent is not None else recent_outbound_media(
        conv_key, within_sec=within_sec, store=store, now=now)
    if not rec:
        return ""
    try:
        mins = max(1, int(round(float(rec.get("age_sec") or 0) / 60.0)))
    except (TypeError, ValueError):
        mins = 1
    mt = str(rec.get("media_type") or "image")
    cap = str(rec.get("caption") or "").strip()
    asking = is_media_identity_question(inbound)
    lg = str(lang or "zh").lower()
    if lg.startswith("en"):
        what = "a video" if mt == "video" else "a photo"
        desc = f' (caption: "{cap}")' if cap else ""
        head = (f"【just sent media】About {mins} min ago YOU (or a teammate on this account) sent "
                f"TA {what} in this chat{desc}. ")
        ask = ("TA is asking right now whether that is you / your photo / edited — " if asking
               else "If TA asks whether that is you / your photo / edited — ")
        return (head + ask +
                "CLAIM IT: yes, that's the one I just sent you, then keep chatting about it. "
                "Never say you never sent it, never say 'I can't send photos / no such feature / "
                "text only', never refuse, never promise another one.")
    if lg.startswith("ja"):
        what = "動画" if mt == "video" else "写真"
        desc = f"（キャプション「{cap}」）" if cap else ""
        head = f"【直前に送った{what}】約{mins}分前、あなた（またはこのアカウントの同僚）がこのチャットで{what}を送っている{desc}。"
        ask = "相手は今それがあなた本人か / 加工かを聞いている——" if asking else "相手が本人か / 加工かを聞いてきたら——"
        return (head + ask + "認める：「うん、さっき送ったやつだよ」と受けて、その話を続ける。"
                "「送ってない / 写真は送れない / そんな機能はない / 文字だけ」は絶対に言わない。断らない。")
    what = "视频" if mt == "video" else "照片"
    desc = f"（配文「{cap}」）" if cap else ""
    head = f"【刚发过{what}】约 {mins} 分钟前，你（或这个账号的同事）刚在本会话给 TA 发过一张{what}{desc}。"
    ask = "TA 这条就是在问那张是不是你 / 是不是你的照片 / 是不是 P 的——" if asking else \
        "TA 若问「这是你吗 / 是你的照片吗 / 像 P 的」——"
    return (head + ask + "**认领**：「对，就是刚发你的那张」，然后顺着那张图聊下去。"
            "绝不说没发过、绝不说「没这个功能 / 不支持发图 / 不能发照片 / 只能发文字」、"
            "绝不拒绝、也不要承诺再发一张。")


# Q-6 B：无匹配回喂（进程内）。生成侧 consume 后注入「无可用照片（场景 X）」。
_ALBUM_MISS: Dict[str, Dict[str, Any]] = {}
_ALBUM_MISS_LOCK = threading.Lock()


def note_album_miss(conv_key: str, query: str = "", scene: str = "",
                    trigger: str = "") -> None:
    ck = str(conv_key or "").strip()
    if not ck:
        return
    with _ALBUM_MISS_LOCK:
        _ALBUM_MISS[ck] = {
            "query": str(query or "")[:120],
            "scene": str(scene or "")[:80],
            "trigger": str(trigger or "")[:24],
            "ts": time.time(),
        }


def consume_album_miss(conv_key: str) -> Dict[str, Any]:
    ck = str(conv_key or "").strip()
    if not ck:
        return {}
    with _ALBUM_MISS_LOCK:
        rec = _ALBUM_MISS.pop(ck, None)
    return dict(rec or {})


def peek_album_miss(conv_key: str) -> Dict[str, Any]:
    ck = str(conv_key or "").strip()
    if not ck:
        return {}
    with _ALBUM_MISS_LOCK:
        return dict(_ALBUM_MISS.get(ck) or {})


def resolve_last_sent_media(conv_key: str, persona_id: str = "") -> Dict[str, Any]:
    """该会话最近一次**真发**的媒体（可重发的文件）：进程内回执账本优先，跨重启
    回落相册投放账本（``persona_media_sends`` 最新一条 → 条目 file_path/url）。
    查不到 → 空 dict（调用方走「从未真发过」分支）。"""
    if not str(conv_key or "").strip():
        return {}
    rec = last_media_receipt(conv_key)
    if rec.get("path") or rec.get("url"):
        return rec
    try:
        from src.companion.persona_media_store import get_persona_media_store
        st = get_persona_media_store()
        if st is None:
            return {}
        items = list((st.sent_history(str(conv_key or "")) or {}).get("items") or [])
        if not items:
            return {}
        latest = max(items, key=lambda it: float((it or {}).get("ts") or 0))
        row = st.get(str(latest.get("id") or "")) or {}
        if not row:
            return {}
        mt = str(row.get("media_type") or "photo")
        return {
            "media_id": str(row.get("id") or ""),
            "path": str(row.get("file_path") or ""),
            "url": str(row.get("url") or ""),
            "media_type": "video" if mt == "video" else "image",
            "mid": "", "ts": float(latest.get("ts") or 0),
        }
    except Exception:
        logger.debug("[image_autosend] resolve_last_sent_media 失败", exc_info=True)
        return {}


def resolve_consistency_cfg(scfg: Dict[str, Any]) -> Dict[str, Any]:
    """``companion.selfie.consistency`` 一致性护栏配置（P0，默认开）。

    与 media_promise_guard 同族的**出站正确性守卫**：修「发的图和说的话/时间/
    衣服对不上」——enabled=false 一键回旧行为。默认值集中在这里（config 可覆盖）：
    ``resend_cooldown_hours``（同图同会话重发冷却）/``continuity_minutes``
    （同会话服装连续窗）/时段过滤随 enabled。
    """
    c = scfg.get("consistency") if isinstance(scfg.get("consistency"), dict) else {}
    try:
        cooldown = float(c.get("resend_cooldown_hours", 24) or 0)
    except (TypeError, ValueError):
        cooldown = 24.0
    try:
        continuity = float(c.get("continuity_minutes", 90) or 0)
    except (TypeError, ValueError):
        continuity = 90.0
    enabled = bool(c.get("enabled", True))
    # 实施90 三个新键（随 enabled 总闸）：
    # resend_policy: cooldown(默认，旧行为「翻旧照」) | strict(同会话绝不重发，
    #   素材耗尽交生成链/诚实文字)；season_gate/place_gate（默认开）——只对
    #   带 season:/place: 标注的条目生效，未打标部署零行为变化。
    policy = str(c.get("resend_policy", "cooldown") or "cooldown").strip().lower()
    return {
        "enabled": enabled,
        "now_hour": (time.localtime().tm_hour if enabled else None),
        "resend_cooldown_hours": (cooldown if enabled else 0),
        "continuity_minutes": (continuity if enabled else 0),
        "no_resend": bool(enabled and policy == "strict"),
        "season_gate": bool(enabled and c.get("season_gate", True)),
        "place_gate": bool(enabled and c.get("place_gate", True)),
    }


# Q-39（#327 / #323）：最近一次相册匹配的 candidates / picked（供 [album_send] 行回填；进程内）
_LAST_MATCH: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _note_last_match(conv_key: str, candidates: int, picked: str) -> None:
    if not conv_key:
        return
    with _LAST_SENT_LOCK:
        _LAST_MATCH[str(conv_key)] = {"candidates": int(candidates), "picked": str(picked or "-")}
        _LAST_MATCH.move_to_end(str(conv_key))
        while len(_LAST_MATCH) > _LAST_SENT_CAP:
            _LAST_MATCH.popitem(last=False)


def last_match(conv_key: str) -> Dict[str, Any]:
    with _LAST_SENT_LOCK:
        return dict(_LAST_MATCH.get(str(conv_key or ""), {}) or {})


def pick_registered_media(
    config: Dict[str, Any], persona_id: str, peer_text: str, *,
    avoid_id: str = "", bond_level: Optional[int] = None,
    force_generic: bool = False, conv_key: str = "",
    required_scene: str = "",
    deny_generic: bool = False,
    intent_gate: Any = None,
    inbound_mid: str = "",
    inbound_media_type: str = "",
) -> Optional[Dict[str, Any]]:
    """查该人设注册相册：关键词命中，或（是泛化「要照片/自拍」请求时）通用池。命中返回行 dict。

    关键词触发**独立于** selfie/object 意图——运营给某条视频配 ``跳舞`` 触发词，客户说
    "给我跳个舞" 就命中，无需它是"自拍/物体图"请求。泛化要图（``detect_selfie_request``）则
    额外放开无触发词的通用相册池（对齐老"随机挑自拍"）。
    ``force_generic=True``＝调用方已从别处判定这是一次要图（承诺兑现/offer-接受桥），
    peer_text 没有关键词也放开通用池。
    ``conv_key`` 非空＝启用防复读记忆（同会话不重发同一张/同一系列，见 select_media）。
    ``required_scene``（P0 一致性）＝显式点名场景（客户原话/承诺原文抽取）：
    通用池只出场景类匹配条目，挑不到 → None（交生成/诚实文字）。
    ``deny_generic``（P1 图文一致性，2026-08-08）＝对方点名要看的是**非人像主体**
    （「你煮的燕窝粥」）：通用人像池一律不放开，只有运营给该主体配过触发词的
    条目才算有货——没货就该发诚实文字，绝不拿随机自拍顶包。
    另自动带时段过滤 + 重发冷却 + 服装连续窗（``companion.selfie.consistency``）。

    Q-39（#327 / #323）三个新参：``intent_gate``＝调用方已算好的 :class:`image_send_gate.IntentGate`
    （缺省在此现算）——``intent`` 为假 → **不跑匹配、不写 miss、不刷红条**，只留一行 DEBUG
    ``[album_match] skip=no_intent``；``inbound_mid``＝本次入站消息 id，同一 ``conv:mid`` 的匹配只跑
    一次（拟稿期 persona_reply 自探 + 投递期再跑 ＝ 红条「今天 2 次」的来源）；``inbound_media_type``
    ＝入站媒体类型（图入站只看客户配文，识图描述里的「自拍」不算索图）。
    """
    scfg = resolve_image_autosend_cfg(config)
    if not scfg.get("enabled", False):
        return None
    try:
        from src.companion.persona_media import pick_media, scene_class_of
        from src.companion.persona_media_store import get_persona_media_store
    except Exception:
        return None
    store = get_persona_media_store()
    if store is None:
        return None
    # Q-6 E：建议词默认参与匹配（apply=auto）；confirm 档只等一键采纳。
    _suggest_ok = True
    try:
        from src.companion.media_auto_tag import resolve_album_ai_cfg
        _apply = str(resolve_album_ai_cfg(config).get("apply") or "auto").lower()
        _suggest_ok = _apply != "confirm"
    except Exception:
        _suggest_ok = True
    # Q-39 意图闸（在 Q-6 匹配**之前**）：识图描述 / 「enjoy the view」泛匹配都不算索图。
    from src.inbox import image_send_gate as _isg
    _gate = intent_gate
    if _gate is None or not hasattr(_gate, "intent"):
        try:
            _gate = _isg.compute_image_intent(
                str(peer_text or ""), None,
                inbound_media_type=str(inbound_media_type or ""),
                assume_intent=("selfie" if force_generic else ""),
                trigger_terms=_isg.album_trigger_terms(str(persona_id or ""), suggest_ok=_suggest_ok),
                offer_bridge=bool(scfg.get("offer_accept_bridge", True)))
        except Exception:
            logger.debug("[album_match] 意图闸异常（按无意图跳过）", exc_info=True)
            _gate = _isg.IntentGate(False, _isg.TRIGGER_NONE, _isg.REASON_NO_INTENT)
    if not bool(getattr(_gate, "intent", False)):
        logger.debug(
            "[album_match] conv=%s skip=%s trigger=%s query=%s",
            conv_key or "-", getattr(_gate, "reason", "") or _isg.REASON_NO_INTENT,
            getattr(_gate, "trigger", "-"),
            str(peer_text or "").replace("\n", " ")[:60])
        return None
    if inbound_mid and conv_key and _isg.album_match_seen(str(conv_key), str(inbound_mid)):
        logger.info("[album_match] conv=%s skip=%s mid=%s", conv_key, _isg.REASON_DUP_MID, inbound_mid)
        return None
    if inbound_mid and conv_key:
        _isg.mark_album_match(str(conv_key), str(inbound_mid))
    # 图入站（带索图配文）只用客户自己敲的字去匹配触发词，识图描述不参与
    if bool(getattr(_gate, "inbound_image", False)) and str(getattr(_gate, "words", "") or ""):
        peer_text = str(getattr(_gate, "words"))
    # 通用人像池（无触发词的本人照）只对**显式出图意图**放开：索要（ask）/ 承诺兑现
    # （commitment，含 force_generic）/ offer-accept / LLM 指令。触发词命中（keyword）是
    # 「客户点名要那条」，不开通用池；谈照片 ≠ 要照片（photo_mention_no_ask）早在闸前拦下。
    _trigger = str(getattr(_gate, "trigger", "") or "")
    _explicit = bool(force_generic) or _trigger in (
        _isg.TRIGGER_ASK, _isg.TRIGGER_COMMITMENT,
        _isg.TRIGGER_OFFER_ACCEPT, _isg.TRIGGER_DIRECTIVE)
    generic_ok = (not deny_generic) and _explicit
    try:
        _resend_days = float(scfg.get("resend_after_days", 90) or 0)
    except (TypeError, ValueError):
        _resend_days = 90.0
    cc = resolve_consistency_cfg(scfg)
    # 场景硬要求：调用方显式给的优先，否则从客户原话提取点名场景。
    _scene_cls = ""
    _scene_kind = ""
    if cc["enabled"]:
        _req = str(required_scene or "").strip()
        if not _req:
            try:
                from src.ai.companion_selfie import extract_requested_scene
                _req = extract_requested_scene(str(peer_text or ""))
            except Exception:
                _req = ""
        if _req:
            _scene_cls = scene_class_of(_req) or _req.strip().lower()
    try:
        from src.companion.persona_media import requested_scene_kind
        _scene_kind = requested_scene_kind(str(peer_text or ""))
    except Exception:
        _scene_kind = ""
    # 「自拍」是通用人像池的默认语义，不是「风景/食物」那种点名 kind。
    # 未打 kind 标签的本人照就是自拍存货——把 kind=selfie 当成硬过滤会让
    # 「发张自拍」在只有未标注相册时全部拒发。
    if _scene_kind == "selfie":
        _scene_kind = ""
    # Q-39 C（#323）：scene_class_of / requested_scene_kind 的泛匹配不再单独把 _ask 置真——
    # 闸已判过「须与索图词共现」（strict_requested_scene_kind）；运营触发词命中（keyword）
    # 是「客户点名要那条」不是「要自拍」，无命中不记 miss（与旧行为一致）。
    _ask = _explicit
    # 通用池在 select_media 里只经 random 层出图（无词条目本就没有「命中」可言）：
    # 显式意图且客户没点名场景 kind → 放行随机挑一张本人照；点名 kind 时通用池
    # 须过 kind 过滤，挑不到就诚实 miss——绝不拿随机照顶包点名场景。
    _allow_random = generic_ok and not _scene_kind
    # 实施90 季节/地点门：人设「此刻季节 + 所在国」上下文（软失败=不设门）。
    _geo = {"season": "", "country": ""}
    if cc.get("season_gate") or cc.get("place_gate"):
        try:
            from src.companion.media_taxonomy import persona_geo_context
            _geo = persona_geo_context(str(persona_id or ""))
        except Exception:
            _geo = {"season": "", "country": ""}
    _trace: Dict[str, Any] = {}
    row = pick_media(
        store, str(persona_id or ""), str(peer_text or ""),
        generic_ok=generic_ok, avoid_id=avoid_id, bond_level=bond_level,
        conv_key=conv_key, resend_after_days=_resend_days,
        now_hour=cc["now_hour"], required_scene_class=_scene_cls,
        resend_cooldown_hours=cc["resend_cooldown_hours"],
        continuity_minutes=cc["continuity_minutes"],
        no_resend=bool(cc.get("no_resend")),
        now_season=(str(_geo.get("season") or "")
                    if cc.get("season_gate") else ""),
        home_country=(str(_geo.get("country") or "")
                      if cc.get("place_gate") else ""),
        allow_random=_allow_random, suggest_ok=_suggest_ok,
        required_scene_kind=_scene_kind, trace=_trace)
    _picked = str((row or {}).get("id") or "-")
    _fb = str(_trace.get("fallback") or "none")
    if _fb not in ("none", "random"):
        _fb = "none"
    logger.info(
        "[album_match] conv=%s query=%s candidates=%s picked=%s fallback=%s",
        conv_key or "-",
        str(peer_text or "").replace("\n", " ")[:80],
        int(_trace.get("start") or 0),
        _picked,
        _fb,
    )
    _note_last_match(str(conv_key or ""), int(_trace.get("start") or 0), _picked)
    if row is None and _ask:
        note_album_miss(str(conv_key or ""), str(peer_text or ""),
                        _scene_kind or _scene_cls or "",
                        trigger=str(getattr(intent_gate, "trigger", "") or ""))
        # Q-35 #306（BPMEWX）：坐席侧可见——AI 侧已改口（上面那条 + album_miss_addendum），
        # 但状态带一字不见，用户只会问「后台传了很多图为什么不发」。旁路记一条持久 note
        # （conv_state 第七只读源 → 琥珀「相册没有匹配『自拍』的图 · AI 已改口 · 去相册补标签」）。
        if conv_key:
            try:
                from src.inbox.album_miss_marker import mark as _album_miss_mark
                _album_miss_mark(str(conv_key), query=str(peer_text or ""),
                                 scene=_scene_kind or _scene_cls or "",
                                 persona_id=str(persona_id or ""),
                                 trigger=str(getattr(intent_gate, "trigger", "") or ""))
            except Exception:
                logger.debug("[album_match] album_miss_marker.mark 失败（忽略）", exc_info=True)
    elif row is not None and conv_key:
        # 命中即清：相册补了标签 / 换了问法命中 → 状态带那行自动消失（无 note 时零写）
        try:
            from src.inbox.album_miss_marker import clear as _album_miss_clear
            _album_miss_clear(str(conv_key))
        except Exception:
            logger.debug("[album_match] album_miss_marker.clear 失败（忽略）", exc_info=True)
    return row


def media_caption(row: Optional[Dict[str, Any]], lang: str = "", *, fallback: str = "") -> str:
    try:
        from src.companion.persona_media import caption_for
        return caption_for(row, lang, fallback=fallback)
    except Exception:
        return str((row or {}).get("caption") or "").strip() or fallback


AUTO_REG_TAG = "auto_generated"


def _auto_photo_count(persona_id: str) -> int:
    """该人设注册相册里 auto_generated 条目数（相册扩容上限判断/场景轮换 salt）。"""
    try:
        from src.companion.persona_media_store import get_persona_media_store
        st = get_persona_media_store()
        if st is None or not persona_id:
            return 0
        return sum(1 for r in st.list(str(persona_id))
                   if AUTO_REG_TAG in (r.get("tags") or []))
    except Exception:
        return 0


def _should_grow_album(
    scfg: Dict[str, Any], persona_id: str,
    row: Optional[Dict[str, Any]], conv_key: str,
) -> bool:
    """相册自动扩容判定：通用池即将**连发同一张 auto 照片**（池里已无可轮换项）
    且未达 ``register_generated_max`` → 放弃相册、改走生成（新场景照，随后自动入册）。

    效果：相册从 1 张有机长到 max 张（不同场景、同一人），之后回到纯轮换零成本。
    仅对 auto_generated 条目生效——运营手动上传的图连发重复交由 avoid_id 轮换语义。
    """
    try:
        if not row or AUTO_REG_TAG not in (row.get("tags") or []):
            return False
        if str(row.get("id")) != str(last_media_sent(conv_key) or ""):
            return False
        if not bool(scfg.get("register_generated", True)):
            return False
        backend = str(((scfg.get("provider") or {}).get("backend")) or "").lower()
        if backend in ("", "disabled", "album"):
            return False
        _max = int(scfg.get("register_generated_max", 3) or 3)
        return _auto_photo_count(persona_id) < _max
    except Exception:
        return False


def _fixed_caption(scfg: Dict[str, Any], kind: str, freshness: str,
                   lang: str = "", *, chat_key: str = "") -> str:
    """固定配文兜底（运营/LLM 配文都缺时），按图片来源新旧取**诚实口径**（P0）。

    ``freshness="old"``（相册存货/注册相册）→ ``caption_album`` 配置 → 双语
    旧照文案池——绝不回落全局 ``caption``（那是「刚拍」口径，配旧照是实录
    穿帮点）；``fresh``（刚生成）→ 原有 ``caption``/``contextual_caption``。
    video 无旧照文案池（措辞是"照片"），只认 ``caption_album`` 配置。
    ``chat_key``（2026-08-03 复读治理）：透传给池选取——crc32(会话+日期)
    确定性 + 近用避重（发出成功后调用方经 ``_note_caption_sent`` 记账），治
    「同一客户几天内反复收到逐字相同配文」；空＝旧游标行为（向后兼容）。
    """
    if kind == KIND_OBJECT:
        return str(scfg.get("contextual_caption") or "")
    if str(freshness or "").strip().lower() == "old":
        cap = str(scfg.get("caption_album") or "")
        if cap or kind == "video":
            return cap
        try:
            from src.ai.companion_selfie import selfie_stage_text
            _lg = "zh" if str(lang or "").lower() == "yue" else str(lang or "")
            return selfie_stage_text("caption_album", _lg,
                                     chat_key=str(chat_key or ""))
        except Exception:
            return ""
    return str(scfg.get("caption") or "")


def _note_caption_sent(conv_key: str, caption: str) -> None:
    """固定池配文**真发出后**记近用账本（软失败；LLM/运营配文不记——
    3 条避重窗只服务池选取，掺入天然多样的 LLM 配文会挤掉池指纹）。"""
    try:
        from src.ai.companion_selfie import note_caption_used
        note_caption_used(conv_key, caption)
    except Exception:
        logger.debug("[image_autosend] 配文近用记账失败（忽略）", exc_info=True)


async def _llm_caption_safe(fn, *, kind: str, subject: str = "", scene: str = "",
                            freshness: str = "fresh",
                            wanted_subject: str = "") -> str:
    """调 LLM 写照片配文（知道图已发出的上下文配文）；失败/超长回空串让调用方回落。

    ``scene``（Phase18）＝照片实际拍摄场景，透传给配文指令（图文叙事一体）。
    ``freshness``（P0 一致性）＝``old`` 时配文指令按「之前拍的存货」口径写
    （禁止「刚拍」谎言）；``fresh``=刚生成的图（可说刚拍）。
    ``wanted_subject``（P0 图文一致性，2026-08-08）＝对方点名想看的非人像主体
    （相册只有自拍时会走到这里）——指令据此禁止「假装照片里有它」。
    回调旧签名 ``fn(kind, subject[, scene[, freshness]])`` 兼容（TypeError 逐级回退），
    防漏改的调用方拿不到配文。
    """
    if fn is None:
        return ""
    try:
        try:
            raw = await fn(kind, subject, scene, freshness, wanted_subject)
        except TypeError:
            try:
                raw = await fn(kind, subject, scene, freshness)
            except TypeError:
                try:
                    raw = await fn(kind, subject, scene)
                except TypeError:
                    raw = await fn(kind, subject)
        s = str(raw or "").strip().strip('"').strip("“”'").strip()
        if not s:
            return ""
        s = s.splitlines()[0].strip()
        return s[:80]
    except Exception:
        logger.debug("[image_autosend] LLM 配文失败（回落固定配文）", exc_info=True)
        return ""


def _maybe_register_generated_selfie(
    scfg: Dict[str, Any], persona_id: str, image_path: str,
    scene: str = "",
) -> str:
    """「自动定妆」：把刚**生成并成功发出**的自拍复制进人设相册目录并登记注册相册（通用池）。

    收益：下次"看看你/近照"请求由注册相册**秒发同一张**——人脸绝对一致、零 GPU 成本，
    比每次重新生成更像"同一个人"。条目打 ``auto_generated`` 标签 + 上限
    ``register_generated_max``（默认 3），运营可随时在 Studio 相册删除/替换成真人图。
    返回新条目 id（供会话避重记录）；跳过/失败返回空串。软失败：绝不影响已完成的发送。
    """
    if not persona_id or not image_path:
        return ""
    if not bool(scfg.get("register_generated", True)):
        return ""
    try:
        from src.companion.persona_media_store import get_persona_media_store
        st = get_persona_media_store()
        if st is None:
            return ""
        rows = st.list(str(persona_id))
        auto_n = sum(1 for r in rows if AUTO_REG_TAG in (r.get("tags") or []))
        if auto_n >= int(scfg.get("register_generated_max", 3) or 3):
            return ""
        album_root = Path(str(((scfg.get("provider") or {}).get("album_dir"))
                              or "assets/persona_media"))
        key = "".join(c for c in str(persona_id) if c.isalnum() or c in ("-", "_"))
        ddir = album_root / (key or "default")
        ddir.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(image_path)).suffix or ".png"
        dst = ddir / f"auto_selfie_{int(time.time())}_{uuid.uuid4().hex[:6]}{suffix}"
        shutil.copy2(image_path, dst)
        _tags = [AUTO_REG_TAG]
        if scene:
            # series 标签让"同场景生成图"互为一个系列（防复读账本按系列排除）
            _tags += [f"scene:{scene}", f"series:auto-{scene}"]
        # 生成图必是人设本人自拍：场景推不出 kind 时显式落 kind:selfie（读路径不再按标签兜底）
        from src.companion.persona_media import infer_scene_kind, scene_class_of
        if infer_scene_kind({}, scene_class_of(scene) if scene else "") == "other":
            _tags.append("kind:selfie")
        row = st.add(str(persona_id), "photo", str(dst), "", triggers=[],
                     tags=_tags, created_by="image_autosend")
        logger.info("[image_autosend] 生成自拍已入册（自动定妆） persona=%s file=%s n=%d",
                    persona_id, dst.name, auto_n + 1)
        return str((row or {}).get("id") or "")
    except Exception:
        logger.debug("[image_autosend] 生成自拍入册失败（忽略）", exc_info=True)
        return ""


async def run_autosend_image(
    config: Dict[str, Any], platform: str, account_id: str, chat_key: str,
    persona_id: str, peer_text: str, history: Optional[List[Dict[str, Any]]], *,
    send_fn: Callable[[str, str, str, str, str], Awaitable[bool]],
    ai_text: str = "", llm_refine: Optional[Callable[[], Awaitable[str]]] = None,
    llm_caption: Optional[Callable[[str, str], Awaitable[str]]] = None,
    conv_key: str = "", lang: str = "",
    assume_intent: str = "",
    assume_scene: str = "",
    directive_override: Optional[Dict[str, str]] = None,
    requested_scene: str = "",
    on_sent: Optional[Callable[[str, str], None]] = None,
    inbound_media_type: str = "",
    inbound_mid: str = "",
) -> bool:
    """autosend「按需发图」总编排（可单测）：注册相册优先（关键词/通用池，图/视频秒发），
    否则回落生成（selfie 相册/openai、物体图 text2img）。任一步真发出即返回 True（跳过语音/文本）。

    ``send_fn(media_path, media_url, media_type, caption, inbox_text) -> awaitable[bool]``
    由调用方提供（负责编排器发送 + 事件循环 marshalling）。
    ``llm_caption(kind, subject) -> awaitable[str]``（可选）：**知道图已发出**的上下文配文
    （文图协同）；缺省/失败回落固定配文 → ai_text。
    ``assume_intent="selfie"``＝调用方已判定要发自拍（出站文本承诺了照片的「兑现」路径），
    跳过 peer_text 意图判定直接走自拍链（相册通用池/生成；预算与关系闸门照常）。
    ``assume_scene``（P0 一致性）＝承诺兑现路径的**承诺内容场景**（从 AI 承诺
    原文抽取，如「发你张海景」→ beach）：兑现必须贴承诺场景（相册硬匹配/生成
    带场景），绝不拿随机人像顶包——「承诺海景兑现成车内自拍」实录事故收口。
    ``directive_override={"kind","scene"}``＝主 LLM 的发图指令（[PHOTO …] 标记，
    2026-07-14 决策权上移）：跳过关键词判定与注册相册，直接按 LLM 给的类型+对话内
    场景生成（scene 直通 ``stage_image_file``；预算/vision_gate 闸门照常）。
    ``requested_scene``（Phase20）＝调用方解析出的期望场景（如「跟上次一样的」
    取自已发媒体日志），优先于场景轮换（低于 directive_override/客户显式点名）。
    ``on_sent(note, scene, series)``（Phase20，可选同步回调）＝真发出后通知调用方
    「发了什么/什么场景/什么衣着系列」——B 线借此把媒体记进与 A 线共用的
    ``_media_sent_log``（series 供 P1 跨链衣着连续窗）。
    ``inbound_media_type`` / ``inbound_mid``（Q-39 #327 #323）＝本次入站的媒体类型 / 消息 id：
    入站是图且客户配文无索图 → 本轮**任何路径**不出相册（承诺交撤回改写）；同 mid 相册匹配只跑一次。
    """
    scfg = resolve_image_autosend_cfg(config)
    if not scfg.get("enabled", False):
        return False
    # 人设级发图闸（2026-07-31，默认关；photo_capability SSOT）：人设没开相册/
    # 发图 → 注册相册、生成、[PHOTO] 指令直通全部不放行（承诺由调用方 promise
    # 链撤回，文字兜底照常）。放在 0a 之前＝指令直通也过闸。
    from src.companion.photo_capability import persona_photos_enabled_by_id
    if not persona_photos_enabled_by_id(persona_id):
        return False
    ck = conv_key or f"{platform}:{account_id}:{chat_key}"

    # Q-39 A/C（#327 / #323）意图闸——在 0a 指令直通 / offer 桥 / 相册 / 生成**全部之前**：
    # 「入站是图 ∧ 客户配文无索图」→ 相册、承诺兑现、[PHOTO] 一律不放行（返回 False 让
    # 调用方 promise 链走撤回改写）；文本入站无索图（「enjoy the view」）→ 不匹配不记 miss。
    from src.inbox import image_send_gate as _isg
    _suggest_ok_g = True
    try:
        from src.companion.media_auto_tag import resolve_album_ai_cfg as _raic
        _suggest_ok_g = str(_raic(config).get("apply") or "auto").lower() != "confirm"
    except Exception:
        _suggest_ok_g = True
    try:
        _gate = _isg.compute_image_intent(
            peer_text, history, inbound_media_type=str(inbound_media_type or ""),
            assume_intent=str(assume_intent or ""), directive_override=directive_override,
            trigger_terms=_isg.album_trigger_terms(str(persona_id or ""), suggest_ok=_suggest_ok_g),
            offer_bridge=bool(scfg.get("offer_accept_bridge", True)))
    except Exception:
        logger.debug("[image_autosend] 意图闸异常（按无意图跳过）", exc_info=True)
        _gate = _isg.IntentGate(False, _isg.TRIGGER_NONE, _isg.REASON_NO_INTENT)
    if not _gate.intent:
        if _gate.reason == _isg.REASON_INBOUND_IMAGE and _gate.trigger in (
                _isg.TRIGGER_COMMITMENT, _isg.TRIGGER_DIRECTIVE):
            # 承诺 / 指令撞上「客户刚发了图」：不兑现、不出相册，记原因让看板能数
            record_image_fallback(_isg.REASON_INBOUND_IMAGE, detail=_gate.trigger)
            logger.info(
                "[image_autosend] conv=%s skip=%s trigger=%s：客户刚发了图且没要图 → "
                "不跟发相册、承诺交撤回改写", ck, _gate.reason, _gate.trigger)
        else:
            logger.debug("[album_match] conv=%s skip=%s trigger=%s", ck, _gate.reason, _gate.trigger)
        return False
    # Q-39 A②：同会话图片跟随冷却（非显式索图触发）+ 每日上限（全部出图）
    _fb_reason = _isg.follow_budget_check(ck, config, _gate.trigger)
    if not _fb_reason and _isg.promise_streak_blocks_follow(ck, _gate.trigger):
        _fb_reason = _isg.REASON_PROMISE_STREAK
    if _fb_reason:
        _fs = _isg.follow_stats(ck)
        record_image_fallback(_fb_reason, detail=_gate.trigger)
        logger.info(
            "[image_autosend] conv=%s skip=%s trigger=%s today=%d last_age=%.0fs → 只发文字",
            ck, _fb_reason, _gate.trigger, int(_fs.get("today") or 0),
            (time.time() - float(_fs.get("last_ts") or 0)) if _fs.get("last_ts") else -1.0)
        return False

    def _dedup_cap(cap_text: str, cap_src: str, alternatives: List[Tuple[str, str]]) -> Tuple[str, str]:
        """Q-39 A③：配文与最近 3 张出图配文近重复（≥0.8）→ 换备选 / 空配文。"""
        try:
            new_cap, new_src, sim = _isg.dedup_caption(ck, cap_text, cap_src, alternatives)
        except Exception:
            return cap_text, cap_src
        if new_cap != cap_text:
            logger.info(
                "[album_send] caption_dedup conv=%s sim=%.2f src=%s->%s",
                ck, sim, cap_src or "-", new_src or "-")
            record_caption("dedup", new_cap)
            return new_cap, new_src
        return cap_text, cap_src

    def _album_sent(cap_text: str, cap_src: str, *, picked: str, source: str, mid: str) -> None:
        """Q-39：每次出图落一行 [album_send] + 记跟随预算 / 配文历史（软失败）。"""
        try:
            _isg.note_follow_sent(ck)
            _isg.note_caption_sent(ck, cap_text)
            _lm = last_match(ck)
            logger.info(_isg.format_album_send_log(
                ck, trigger=_gate.trigger, intent=_gate,
                candidates=(_lm.get("candidates") if _lm else -1),
                picked=picked or str((_lm or {}).get("picked") or "-"),
                caption_src=cap_src, mid=mid, source=source))
        except Exception:
            logger.debug("[album_send] 记账 / 日志异常（忽略）", exc_info=True)

    # Q-20 D（#178 VDUJX6）认领分支：客户在问「这是你吗 / 是你的照片吗 / 像 P 的」而
    # 30 分钟内我方（人工或 AI）刚发过媒体 → 这不是新的索图请求，**不发第二张**、
    # 也绝不走「相册无图 → 拒绝」；交文字链按 recent_media_claim_note 认领。
    # 调用方显式兑现（assume_intent / directive_override）不受此影响。
    if not assume_intent and not directive_override:
        try:
            if is_media_identity_question(peer_text):
                _recent = recent_outbound_media(ck)
                if _recent:
                    logger.info(
                        "[image_autosend] claim_recent conv=%s age=%.0fs src=%s: 「是你吗」"
                        "追问刚发的图 → 不发新图、文字认领", ck,
                        float(_recent.get("age_sec") or 0), _recent.get("source"))
                    return False
        except Exception:
            logger.debug("[image_autosend] claim_recent 判定异常（忽略）", exc_info=True)

    def _notify_sent(note: str, scene: str, series: str = "") -> None:
        """发出成功后的调用方通知（软失败：日志记录绝不影响已完成的发送）。"""
        if on_sent is None:
            return
        try:
            on_sent(str(note or ""), str(scene or ""), str(series or ""))
        except Exception:
            logger.debug("[image_autosend] on_sent 回调异常（忽略）", exc_info=True)

    async def _guard_caption(path: str, cap_text: str, cap_src: str,
                             kind_: str, fresh_: str) -> Tuple[str, str]:
        """P2 出站前图文核对：配文声称画面里有的东西，VLM 说没有 → 换诚实兜底。

        只核 LLM 写的配文（运营配的 registry 配文与固定文案池本就不声称画面内容）；
        软失败/未开一律原样返回。返回 ``(配文, 来源)``。"""
        if cap_src != "llm" or not cap_text:
            return cap_text, cap_src
        try:
            from src.ai.caption_image_guard import ensure_truthful_caption
            fb = _fixed_caption(scfg, kind_, fresh_, lang, chat_key=ck)
            new_cap, reason = await ensure_truthful_caption(
                path, cap_text, root_config=config, scfg=scfg, kind=kind_,
                fallback=fb)
        except Exception:
            logger.debug("[image_autosend] 图文核对异常（原样发）", exc_info=True)
            return cap_text, cap_src
        if new_cap != cap_text:
            record_image_fallback("caption_guard_rewrite", detail=reason)
            return new_cap, "fixed"
        return cap_text, cap_src

    # P1 图文一致性（2026-08-08）：对方这次想看的**非人像主体**（AI 自己 offer 的
    # 「我煮了燕窝粥」也算）。非空＝相册里的人像照不能顶包：通用池关闭，只有运营
    # 给该主体配过触发词的条目才算有货，否则不发图交诚实文字。
    _wanted = ""
    try:
        from src.ai.companion_selfie import detect_selfie_request
        from src.ai.outbound_promise_guard import wanted_media_subject
        # 泛化要图（「有照片就发来看看」）同样要回溯 offer 主体——最常见的顶包
        # 现场正是「AI 说煮了粥 → 客户泛泛要图 → 系统发自拍」。
        _wanted = wanted_media_subject(
            peer_text, history,
            generic_request=bool(assume_intent) or detect_selfie_request(peer_text))
    except Exception:
        logger.debug("[image_autosend] 想看主体判定异常（忽略）", exc_info=True)

    # 0a) LLM 发图指令直通（photo_directive）：意图与场景都来自主 LLM 对上下文的
    # 理解，不再经关键词/相册池。失败回落 False（正文承诺由调用方 promise 链撤回）。
    if directive_override and str(directive_override.get("kind") or "") in (
            KIND_SELFIE, KIND_OBJECT):
        _scene = str(directive_override.get("scene") or "")
        if str(directive_override.get("kind")) == KIND_SELFIE:
            # 时间兜底（Phase19）：LLM 场景漏了时间词 → 补当前时段光线，
            # 凌晨要图不出正午烈日照（有时间词则尊重 LLM 对话内理解）。
            try:
                from src.ai.companion_selfie import ensure_time_of_day
                _scene = ensure_time_of_day(_scene)
            except Exception:
                pass
        _d = {"kind": str(directive_override.get("kind")),
              "subject": _scene, "scene": _scene,
              # LLM 亲口点名的场景＝硬要求：相册兜底必须同场景类，挑不到如实失败
              # （正文"图书馆自习"配车内自拍=打脸）。
              "scene_strict": bool(_scene.strip())}
        if _d["kind"] == KIND_OBJECT:
            # object 链吃 prompt 字段；LLM 已给英文主体 → 直接组生图 prompt，
            # 无需 llm_refine 二次调用（那是给正则抽中文主体兜底的）。
            from src.ai.contextual_image import build_object_image_prompt
            _d["prompt"] = build_object_image_prompt(
                _scene, style=str(scfg.get("style") or ""))
        staged = await stage_image_file(
            config, platform, account_id, persona_id, _d, llm_refine=None,
            conv_key=ck)
        if not staged:
            record_image_fallback("directive_stage_failed")
            # P2-6：AI 自己想发图却没货——客户没要图，AI 不必对客户改口（承诺由调用方
            # promise 链撤回），但坐席得看见「AI 想发、没货」→ 复用状态带旁注，走 directive 文案。
            if ck:
                try:
                    from src.inbox.album_miss_marker import mark as _album_miss_mark
                    _album_miss_mark(str(ck), query=_d["scene"], scene=_d["scene"],
                                     persona_id=str(persona_id or ""), trigger="directive")
                except Exception:
                    logger.debug("[image_autosend] album_miss_marker.mark 失败（忽略）", exc_info=True)
            return False
        local, url, kind, sinfo = staged
        # provider=album＝相册来源（后端直选或生成失败兜底两路都是；fallback_from
        # 记的是**失败的主后端名**，不能用它判相册）。
        _from_album = (sinfo.get("provider") == "album")
        _fresh = "old" if _from_album else "fresh"
        # 配文优先级与关键词链相反：**LLM 正文优先**——标记和正文出自同一次思考
        # （正文"刚在图书馆自习完啦"+图书馆场景图），天然文图一致，无需再调 LLM 配文。
        # 例外（P0 一致性）：LLM 指令期待现拍，但实际回落了相册旧图 → 正文的
        # 「刚拍」措辞会撒谎 → 弃正文改走 freshness 感知配文。
        cap = str(ai_text or "").strip() if _fresh == "fresh" else ""
        cap_src = "draft" if cap else ""
        if not cap:
            cap = await _llm_caption_safe(
                llm_caption, kind=kind, subject=_d["subject"],
                scene=(_d["scene"] if kind == KIND_SELFIE else ""),
                freshness=_fresh,
                wanted_subject=(_wanted if kind == KIND_SELFIE else ""))
            cap_src = "llm" if cap else ""
        if not cap:
            cap = _fixed_caption(scfg, kind, _fresh, lang, chat_key=ck)
            cap_src = "fixed" if cap else ""
        cap, cap_src = await _guard_caption(local, cap, cap_src, kind, _fresh)
        cap, cap_src = _dedup_cap(cap, cap_src, [
            (_fixed_caption(scfg, kind, _fresh, lang, chat_key=ck), "fixed")])
        _mid = ""
        try:
            ok, _mid = _send_result(await send_fn(
                local, url, "image", cap, ("[图片] " + (cap or "")).strip()))
        except Exception:
            logger.debug("[image_autosend] 指令图投递异常", exc_info=True)
            ok = False
        if ok:
            note_media_receipt(ck, path=local, url=url, media_type="image", mid=_mid)
            if ck:
                try:
                    from src.inbox.album_miss_marker import clear as _album_miss_clear
                    _album_miss_clear(str(ck))
                except Exception:
                    logger.debug("[image_autosend] album_miss_marker.clear 失败（忽略）", exc_info=True)
            if cap_src:
                record_caption(cap_src, cap)
            if cap_src == "fixed":
                _note_caption_sent(ck, cap)
            record_image_sent(kind, source=(
                "llm_directive_album" if _fresh == "old" else "llm_directive"))
            _album_sent(cap, cap_src, picked=os.path.basename(local or url or ""),
                        source=("llm_directive_album" if _fresh == "old" else "llm_directive"),
                        mid=_mid)
            _notify_sent("[图片] " + (cap or ""),
                         str(sinfo.get("scene_class") or (
                             _d["scene"] if kind == KIND_SELFIE else "")),
                         str(sinfo.get("series") or ""))
            logger.info(
                "[autosend image] 已发图(LLM指令) platform=%s acct=%s kind=%s scene=%r src=%s mid=%s",
                platform, account_id, kind, _d["scene"][:120],
                sinfo.get("provider") or "?", _mid or "-")
        else:
            record_image_fallback("directive_deliver_failed")
        return ok

    # 0) offer-accept 桥：上一轮 AI 提议「要不要看照片」、本条客户只回「好呀」——
    # detect_selfie_request("好呀") 抓不住，offer 会变空头支票。桥判定后按自拍链走。
    if not assume_intent and bool(scfg.get("offer_accept_bridge", True)):
        try:
            from src.ai.outbound_promise_guard import offer_accepted
            if offer_accepted(peer_text, history) == "image":
                assume_intent = KIND_SELFIE
                record_promise_event("offer_accept")
                logger.info("[image_autosend] offer-accept 桥命中（客户短肯定接受了发照片提议）")
        except Exception:
            logger.debug("[image_autosend] offer-accept 判定异常（忽略）", exc_info=True)

    # 1) 注册相册优先（DB）——关键词命中或泛化要图的通用池；图/视频均可，秒发零成本。
    # conv_key 启用防复读记忆：同会话不重发同一张/同一系列（分层回落见 select_media）。
    # required_scene（P0 一致性）：承诺兑现（assume_scene）/「跟上次一样的」
    # （requested_scene）时，注册相册只允许出**场景匹配**的条目——没有匹配条目
    # 就放弃相册走生成链，绝不拿随机人像顶包承诺场景。
    _need_scene = str(assume_scene or requested_scene or "").strip()
    # P1：想看的是具体主体时——能归到场景类（「卧室」→bedroom）就按场景硬匹配，
    # 归不到（「燕窝粥」这类食物/物件）就彻底关掉通用人像池。
    _wanted_scene = ""
    if _wanted and not _need_scene:
        try:
            from src.companion.persona_media import scene_class_of
            _wanted_scene = scene_class_of(_wanted)
        except Exception:
            _wanted_scene = ""
    row = pick_registered_media(
        config, persona_id, peer_text, avoid_id=last_media_sent(ck),
        force_generic=bool(assume_intent), conv_key=ck,
        required_scene=(_need_scene or _wanted_scene),
        deny_generic=bool(_wanted and not _wanted_scene),
        intent_gate=_gate, inbound_mid=str(inbound_mid or ""),
        inbound_media_type=str(inbound_media_type or ""))
    # 相册自动扩容：通用池只剩「上次刚发过的那张 auto 照片」且额度未满 → 本次改走
    # 生成（新场景照，发完自动入册），相册有机长到 max 张后回到纯轮换。
    if row and _should_grow_album(scfg, persona_id, row, ck):
        logger.info("[image_autosend] 相册避重耗尽→生成新照扩容 persona=%s", persona_id)
        row = None
    if row:
        local = str(row.get("file_path") or "")
        url = str(row.get("url") or "")
        mt = str(row.get("media_type") or "photo")
        # #67-③（0830 skuio 机实锤）：发送前先验条目文件真实存在——DB 指向的
        # 文件蒸发（旧安装目录被版本更新替换）时，pyrogram 会把路径当 file_id
        # 报「Failed to decode」，此前被吞成泛化投递失败，排障被带偏一整轮。
        # 独立归因 album_file_missing + 点名路径；条目跳过交回落链（诚实文字/
        # 生成），绝不拿一条死路径反复撞。只验**相册管辖**路径（persona_albums
        # 树下=上传链落的，事故类全在此）；运营外挂的任意路径维持旧语义。
        if (local and "persona_albums" in local.replace("\\", "/")
                and not Path(local).is_file()):
            record_image_fallback("album_file_missing",
                                  detail=os.path.basename(local)[:60])
            logger.warning(
                "[image_autosend] 相册条目文件缺失（DB 指向已蒸发的路径）："
                "id=%s path=%s —— 按 album_file_missing 归因，条目跳过",
                row.get("id"), local)
            row = None
    if row:
        # 配文语言对齐（2026-07-22）：粤语客户取 caption_i18n["yue"]（语言检测器
        # 把粤语归 zh，靠特征字识别补路由）；其余按调用方 lang（en 等）。
        _cap_lang = lang
        try:
            from src.ai.lang_voice_route import is_cantonese_text
            if is_cantonese_text(str(peer_text or "")):
                _cap_lang = "yue"
        except Exception:
            pass
        # 条目实际场景/系列：配文与账本都以它为准（配文层先要，别等发完再算）。
        _row_scene = ""
        _row_series = ""
        try:
            from src.companion.persona_media import row_scene_class, series_of
            _row_scene = row_scene_class(row)
            _row_series = series_of(row)
        except Exception:
            pass
        cap = media_caption(row, _cap_lang, fallback="")
        cap_src = "registry" if cap else ""
        # 时刻词守卫（2026-08-12，与 A 线 skill_manager 同口径）：固定配文里
        # 「下午的阳光」类现在时态时刻词与发送时刻硬冲突 → 弃用回落下层配文链。
        if cap:
            try:
                from src.companion.persona_media import caption_tod_conflict
                import datetime as _dt_cap
                if caption_tod_conflict(cap, _dt_cap.datetime.now().hour):
                    logger.info(
                        "[image_autosend] 注册配文时刻词冲突，回落 id=%s",
                        row.get("id"))
                    cap, cap_src = "", ""
            except Exception:
                pass
        if not cap:
            # 相册条目无运营配文（如 auto 定妆照）→ LLM 按当前对话写配文 → 固定配文。
            # freshness=old：相册图是「之前拍的」，配文不得写「刚拍的」。
            # scene/wanted_subject（P0，2026-08-08）：不给条目真实场景，LLM 只能
            # 顺着对话瞎编（自拍被配成「给你瞅瞅我卧室」正是这么来的）。
            _k = "video" if mt == "video" else KIND_SELFIE
            cap = await _llm_caption_safe(
                llm_caption, kind=_k, scene=_row_scene, freshness="old",
                wanted_subject=_wanted)
            cap_src = "llm" if cap else ""
        if not cap:
            # 注册相册＝备货旧照：固定兜底走旧照口径（caption_album 配置/双语池），
            # 绝不用全局 caption 的「刚拍」措辞（P0 实录穿帮点）。
            cap = _fixed_caption(scfg, ("video" if mt == "video" else KIND_SELFIE),
                                 "old", _cap_lang, chat_key=ck)
            cap_src = "fixed" if cap else ""
        if not cap:
            cap = str(ai_text or "")
            cap_src = "draft" if cap else ""
        if local or url:
            tag = "[视频] " if mt == "video" else "[图片] "
            cap, cap_src = await _guard_caption(
                local, cap, cap_src, ("video" if mt == "video" else KIND_SELFIE),
                "old")
            # Q-39 A③：与最近 3 张出图配文近重复 → 换运营配文 / 固定池 / 空配文
            cap, cap_src = _dedup_cap(cap, cap_src, [
                (media_caption(row, _cap_lang, fallback=""), "registry"),
                (_fixed_caption(scfg, ("video" if mt == "video" else KIND_SELFIE),
                                "old", _cap_lang, chat_key=ck), "fixed")])
            # 工单 #143：相册条目的 media_type 是 photo/video（persona_media_store
            # 口径），发送层白名单是 image/…——发出前归一（与 A 线
            # skill_manager._try_send_selfie_media 同口径），"photo" 裸传会让
            # WhatsApp 边车按 document 发出（对方看到点不开的「文档」）。
            _send_mt = "video" if mt == "video" else "image"
            _mid = ""
            try:
                ok, _mid = _send_result(await send_fn(
                    local, url, _send_mt, cap, (tag + (cap or "")).strip()))
            except Exception:
                logger.debug("[image_autosend] 注册媒体投递异常", exc_info=True)
                ok = False
            if ok:
                note_media_receipt(ck, media_id=str(row.get("id") or ""),
                                   path=local, url=url, media_type=_send_mt, mid=_mid)
                if cap_src:
                    record_caption(cap_src, cap)
                if cap_src == "fixed":
                    _note_caption_sent(ck, cap)
                note_media_sent(ck, str(row.get("id")))
                try:
                    from src.companion.persona_media import series_of
                    from src.companion.persona_media_store import get_persona_media_store
                    st = get_persona_media_store()
                    if st is not None:
                        st.record_hit(str(row.get("id")))
                        # 防复读账本：记「这张（这系列）发给过这个会话」，跨重启持久。
                        # file_key＝文件名：生成链的文件系统相册按文件名比对，缺这个
                        # 键就认不出同一张图刚发过（2026-07-28 重复发图事故）。
                        st.record_send(ck, str(row.get("id")),
                                       persona_id=str(persona_id or ""),
                                       series=series_of(row),
                                       file_key=os.path.basename(local or ""))
                except Exception:
                    pass
                record_image_sent(mt, source="registry")
                _album_sent(cap, cap_src, picked=str(row.get("id") or "-"),
                            source="registry", mid=_mid)
                if _need_scene:
                    record_scene_request(_need_scene, unmet=False)
                # 相册现成图：场景取条目 scene:* 标签（配文层已算过，见上）
                _notify_sent((tag + (cap or "")).strip(), _row_scene, _row_series)
                logger.info(
                    "[autosend image] 已发相册媒体 platform=%s acct=%s type=%s id=%s mid=%s",
                    platform, account_id, mt, row.get("id"), _mid or "-")
                return True
            record_image_fallback("registry_deliver_failed")

    # 1b) P1「无货就别发图」：对方想看的是非人像主体，而相册没有对应货。
    # 相册后端只出人像 → 继续走下去必然拿自拍顶包（实录：燕窝粥→人脸照配
    # 「家里随便吃吃嘛」）；真出图后端则改按**物体图**出这个主体，走 image_gate
    # 的 subject_match 后验，出不对宁可失败回落文字。
    _forced_directive: Optional[Dict[str, Any]] = None
    if _wanted and not row:
        _bk0 = str(((scfg.get("provider") or {}).get("backend")) or "").lower()
        if _bk0 in ("", "disabled", "album"):
            record_image_fallback("wanted_subject_no_stock", detail=str(_wanted)[:40])
            logger.info(
                "[image_autosend] 对方想看「%s」而相册无对应货 → 不发图（交诚实文字）"
                " platform=%s acct=%s", _wanted, platform, account_id)
            return False
        try:
            from src.ai.contextual_image import build_object_image_prompt
            _forced_directive = {
                "kind": KIND_OBJECT, "subject": _wanted,
                "prompt": build_object_image_prompt(
                    _wanted, style=str(scfg.get("style") or "")),
            }
        except Exception:
            logger.debug("[image_autosend] 物体图 prompt 构造异常", exc_info=True)
            record_image_fallback("wanted_subject_no_stock", detail=str(_wanted)[:40])
            return False

    # 2) 回落生成（原有：selfie 相册/openai img2img、物体图 text2img）。
    # 承诺兑现/offer-接受路径（assume_intent）跳过 peer_text 意图判定——意图来自
    # 出站承诺或上一轮 offer，本条客户文本可能没有任何要图关键词。
    if _forced_directive is not None:
        directive: Optional[Dict[str, Any]] = _forced_directive
    elif assume_intent:
        directive: Optional[Dict[str, Any]] = {"kind": str(assume_intent)}
    else:
        directive = plan_autosend_image(peer_text, history, scfg)
    if not directive:
        return False
    # Phase18 场景显式化：自拍时把本次将用的场景**先算出来**放进 directive
    # （承诺内容 → 调用方期望场景（Phase20「跟上次一样的」取自媒体日志）→
    # 场景状态轮换含相册扩容 salt——与 stage_image_file 内部回落同口径）
    # → 配文 LLM 能拿到「照片实际拍摄场景」，图、配文、（草稿链的）聊天场景状态
    # 三者同源。
    # 场景优先级：承诺内容（assume_scene，AI 亲口说过的必须兑现）→ requested_scene
    # →（仅真出图后端）场景状态轮换。前两者是「说出口的场景」＝硬要求
    # （scene_strict，album 后端靠 _meta 场景过滤同样能兑现）；轮换场景仅是默认值，
    # 相册兜底可放宽（发别的场景不算撒谎，没人点过名）。
    _backend = str(((scfg.get("provider") or {}).get("backend")) or "").lower()
    if (str(directive.get("kind") or "") == KIND_SELFIE
            and not str(directive.get("scene") or "").strip()):
        if str(assume_scene or "").strip():
            directive["scene"] = str(assume_scene).strip()
            directive["scene_strict"] = True
        elif str(requested_scene or "").strip():
            directive["scene"] = str(requested_scene).strip()
            directive["scene_strict"] = True
        elif _backend not in ("", "disabled", "album"):
            try:
                from src.ai.companion_selfie import resolve_current_scene
                from src.companion.persona_location import resolve_persona_now
                from src.companion.weather_state import snap_for_persona
                _p_img = _resolve_persona(persona_id)
                directive["scene"] = resolve_current_scene(
                    _p_img, scfg,
                    now=resolve_persona_now(_p_img),
                    salt=_auto_photo_count(persona_id),
                    # 2026-08-18：滤天气冲突场景（暴雨×沙滩）；闸关/无坐标=None 旧行为
                    weather_snap=snap_for_persona(
                        _p_img, scfg.get("_weather_cfg")))
            except Exception:
                logger.debug("[image_autosend] 场景解析跳过", exc_info=True)
    staged = await stage_image_file(
        config, platform, account_id, persona_id, directive,
        llm_refine=llm_refine, conv_key=ck)
    if not staged:
        record_image_fallback("stage_failed")
        return False
    local, url, kind, sinfo = staged
    # 相册来源（album 后端直选 or 生成失败兜底，两路 provider 都=album）＝
    # 「之前拍的」照片：配文按旧照口径写（B 线文件系统相册无跨天防复读账本，
    # 声称「刚拍」重复发同图必穿帮）。
    _from_album = (sinfo.get("provider") == "album")
    _fresh = "old" if _from_album else "fresh"
    # 配文（文图协同）：LLM 上下文配文（知道图已发出+场景+新旧）→ 固定配文 → 草稿兜底。
    # 场景以 staged 实际产出为准（生成失败回落相册时 directive.scene 已不成立）。
    _subject = str((directive or {}).get("subject") or "")
    _scene = str(sinfo.get("scene_class") or (
        (directive or {}).get("scene") if _fresh == "fresh" else "") or "") \
        if kind == KIND_SELFIE else ""
    cap = await _llm_caption_safe(
        llm_caption, kind=kind, subject=_subject, scene=_scene, freshness=_fresh,
        wanted_subject=(_wanted if kind == KIND_SELFIE else ""))
    cap_src = "llm" if cap else ""
    if not cap:
        cap = _fixed_caption(scfg, kind, _fresh, lang, chat_key=ck)
        cap_src = "fixed" if cap else ""
    if not cap:
        cap = str(ai_text or "")
        cap_src = "draft" if cap else ""
    cap, cap_src = await _guard_caption(local, cap, cap_src, kind, _fresh)
    cap, cap_src = _dedup_cap(cap, cap_src, [
        (_fixed_caption(scfg, kind, _fresh, lang, chat_key=ck), "fixed")])
    _mid = ""
    try:
        ok, _mid = _send_result(await send_fn(
            local, url, "image", cap, ("[图片] " + (cap or "")).strip()))
    except Exception:
        logger.debug("[image_autosend] 生成图投递异常", exc_info=True)
        ok = False
    if ok:
        note_media_receipt(ck, path=local, url=url, media_type="image", mid=_mid)
        if cap_src:
            record_caption(cap_src, cap)
        if cap_src == "fixed":
            _note_caption_sent(ck, cap)
        record_image_sent(kind, source=(
            "keyword_album" if _fresh == "old" else "keyword"))
        _album_sent(cap, cap_src, picked=os.path.basename(local or url or ""),
                    source=("keyword_album" if _fresh == "old" else "keyword"), mid=_mid)
        _notify_sent("[图片] " + (cap or ""), _scene,
                     str(sinfo.get("series") or ""))
        logger.info(
            "[autosend image] 已发图(生成) platform=%s acct=%s kind=%s mid=%s",
            platform, account_id, kind, _mid or "-")
        # 自动定妆：真出图后端生成的自拍入册，下次同类请求秒发同一张（脸恒定、零 GPU）。
        # album 后端不入册（图本来就来自相册，登记是循环）。入册后记会话避重，
        # 使下一次请求轮换到旧照或触发下一轮扩容。
        # P0：生成失败回落的相册旧照（_from_album）同样不入册——否则同一张照片
        # 会被反复登记成"新 auto 照"，相册被重复项灌爆且轮换失真。
        _backend = str(((scfg.get("provider") or {}).get("backend")) or "").lower()
        if (kind == KIND_SELFIE and not _from_album
                and _backend not in ("", "album", "disabled")):
            _new_id = _maybe_register_generated_selfie(
                scfg, persona_id, local, scene=_scene)
            if _new_id:
                note_media_sent(ck, _new_id)
                # T2（2026-07-22）：生成图同样入防复读持久账本——入册后它就是
                # 相册候选，不记账本则同客户可能再次收到这张"新照片"。
                # series=auto-<scene>（与入册 tags 同名：同场景生成图互为一个系列）。
                try:
                    from src.companion.persona_media_store import (
                        get_persona_media_store,
                    )
                    _st = get_persona_media_store()
                    if _st is not None:
                        _st.record_send(
                            ck, _new_id, persona_id=str(persona_id or ""),
                            series=f"auto-{_scene}" if _scene else "",
                            file_key=os.path.basename(local or ""))
                except Exception:
                    pass
    else:
        record_image_fallback("deliver_failed")
    return ok


# ── 命理「人生 K 线」autosend（Phase 4：多平台出图）─────────────────────────────
# 独立于 companion.selfie 开关（bazi 单独可用）；与原生 A 线 Stage C 同一套
# 纯组件（意图检测/排盘/评分/渲染），只是发送走 orch.send_media（经调用方 send_fn）。


async def run_autosend_kline(
    config: Dict[str, Any], platform: str, account_id: str, peer_text: str, *,
    send_fn: Callable[[str, str, str, str, str], Awaitable[bool]],
    resolve_birth: Optional[Callable[[], Any]] = None,
) -> bool:
    """客户在多平台会话里求「人生 K 线/运势曲线」→ 渲染 PNG 经 send_fn 发出。

    True=已作为图片发出（调用方跳过语音/文本）；False=未发（未开/非请求/缺生辰/
    渲染或投递失败）→ 回落正常草稿流（缺生辰时草稿注入路径会顺势要生辰）。
    ``resolve_birth``：由调用方注入的「从记忆解析生辰」回调（拿不到 → None）。
    """
    from src.fatex.config import fatex_cfg
    bcfg = fatex_cfg(config)  # FateX 合并视图（fatex.* 优先，companion.bazi 兼容）
    if not (isinstance(bcfg, dict) and bcfg.get("enabled", False)
            and bcfg.get("kline", True)):
        return False
    pt = str(peer_text or "").strip()
    if not pt:
        return False
    try:
        from src.companion.bazi_context import detect_kline_intent
        if not detect_kline_intent(pt):
            return False
        from src.companion.bazi_engine import bazi_available, compute_bazi
        from src.companion.bazi_profile import extract_birth_info
        if not bazi_available():
            return False
    except Exception:
        logger.debug("[autosend kline] 组件导入失败", exc_info=True)
        return False
    from src.companion.bazi_stats import get_bazi_stats
    info = extract_birth_info(pt)
    if info is None and callable(resolve_birth):
        try:
            info = resolve_birth()
        except Exception:
            info = None
    if info is None:
        return False  # 缺生辰 → 回落文字（草稿注入路径顺势采集）
    chart = compute_bazi(info)
    if not chart:
        return False
    try:
        from src.companion.bazi_kline import build_kline_series, render_kline_png
        now_year = time.localtime().tm_year
        series = build_kline_series(
            chart, start_year=now_year - 2,
            years=int(bcfg.get("kline_years", 10) or 10))
        if not series:
            return False
        out_dir = str(bcfg.get("kline_out_dir") or "tmp_bazi")
        tmp_path = os.path.join(
            out_dir,
            f"kline-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png")
        if not render_kline_png(series, tmp_path):
            get_bazi_stats().record_kline(ok=False)
            record_image_fallback("kline_render_failed")
            return False
        with open(tmp_path, "rb") as fh:
            data = fh.read()
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(tmp_path), data)
    except Exception:
        logger.debug("[autosend kline] 出图/落盘失败", exc_info=True)
        get_bazi_stats().record_kline(ok=False)
        record_image_fallback("kline_stage_failed")
        return False
    cap = str(bcfg.get("kline_caption") or "").strip() or (
        "给你画好啦～这是你近十年的运势曲线（仅供参考哦），想细看哪一年跟我说😊")
    try:
        ok = bool(await send_fn(local, url, "image", cap,
                                ("[图片] " + cap).strip()))
    except Exception:
        logger.debug("[autosend kline] 投递异常", exc_info=True)
        ok = False
    get_bazi_stats().record_kline(ok=ok)
    if ok:
        record_image_sent("bazi_kline")
        logger.info(
            "[autosend kline] 已发 K 线卡 platform=%s acct=%s", platform, account_id)
    else:
        record_image_fallback("kline_deliver_failed")
    return ok


__all__ = [
    "KIND_SELFIE", "KIND_OBJECT",
    "resolve_image_autosend_cfg", "plan_autosend_image", "stage_image_file",
    "pick_registered_media", "media_caption", "run_autosend_image",
    "run_autosend_kline",
    "last_media_sent", "note_media_sent",
    "note_media_receipt", "last_media_receipt", "resolve_last_sent_media",
    "note_album_miss", "consume_album_miss", "peek_album_miss",
    "last_match",
    "record_image_sent", "record_image_fallback", "metrics_snapshot",
    "record_promise_event", "record_sent_claim_event",
    "image_gen_inflight",
]
