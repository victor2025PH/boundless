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
    "last_caption": "",
    # 失败原因分布（与 voice_autosend.fallback_counts 同口径，供看板读 Top 原因）
    "fallback_reasons": {},
    "last_failure_detail": "",
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
    ——看板据此观察「LLM 决策 vs 关键词兜底」占比，指导 hybrid → llm 收敛节奏。"""
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


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        snap = dict(_METRICS)
        snap["fallback_reasons"] = dict(_METRICS.get("fallback_reasons") or {})
        snap["gen_inflight"] = image_gen_inflight()
        return snap


def resolve_image_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``companion.selfie`` 块（与 ``skill_manager._selfie_cfg`` 同口径；缺失返回 {}）。"""
    try:
        sc = ((config or {}).get("companion") or {}).get("selfie")
        return dict(sc) if isinstance(sc, dict) else {}
    except Exception:
        return {}


def plan_autosend_image(
    peer_text: str,
    history: Optional[List[Dict[str, Any]]],
    scfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """按客户最近一条入站文本判断该发什么图（纯函数）。None=不发图（回落文本/语音）。

    - 命中 ``detect_selfie_request`` → ``{kind: "selfie"}``（人设自拍，可走相册）。
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
        from src.ai.companion_selfie import (
            detect_selfie_request,
            extract_requested_scene,
        )
        if detect_selfie_request(pt):
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


async def stage_image_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    persona_id: str,
    directive: Dict[str, Any],
    *,
    llm_refine: Optional[Callable[[], Awaitable[str]]] = None,
) -> Optional[Tuple[str, str, str]]:
    """按 ``directive`` 出图并落到出站媒体目录，返回 ``(本地路径, /static URL, kind)``；失败/不满足返回 None。

    调用方据此 ``orch.send_media(media_path=local, media_url=url, media_type="image")``。
    - selfie：``album`` 后端挑现成图；``openai``/``command`` 后端 build_selfie_prompt + 相册基础图 img2img。
    - object：仅真出图后端（非 disabled/album）；可选 ``llm_refine`` 把 prompt 提炼得更准。
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
    res = None
    try:
        if kind == KIND_SELFIE:
            if backend == "album":
                res = await provider.generate("", album_key=album_key)
            else:
                persona = _resolve_persona(persona_id)
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
                    )
                # 多样性 salt（治头位置/表情千篇一律，默认关，overlay opt-in）：开时
                # 每次发送取随机 salt → prompt 姿态/表情/取景 + 底噪一起变，同一人不同瞬间。
                _vsalt = resolve_variety_salt(scfg)
                # per-persona 角色 LoRA spec（file/weight/trigger）：多人设各自 LoRA。
                _lora = resolve_persona_lora(persona, scfg)
                # 人设级 appearance 优先（多人设各有长相），config 全局 appearance 兜底。
                prompt = build_selfie_prompt(
                    persona,
                    scene_hint=scene,
                    style=str(scfg.get("style") or ""),
                    default_appearance=str(scfg.get("appearance") or ""),
                    content_rating=str(scfg.get("content_rating") or ""),
                    variety_salt=_vsalt,
                    lora_trigger=_lora["trigger"],
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
                        album_key=album_key, base_image=base,
                        lora=_lora["file"], lora_weight=_lora["weight"])
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
            from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
            _image_gen_begin()
            try:
                res = await generate_with_gate(
                    provider, prompt, root_config=config,
                    gate_cfg=resolve_gate_cfg(scfg), seed=-1,
                    kind="object", subject=_subject)
            finally:
                _image_gen_end()
        else:
            return None
    except Exception:
        logger.debug("[image_autosend] 出图异常", exc_info=True)
        return None
    if not (res is not None and getattr(res, "ok", False) and getattr(res, "image_path", "")):
        return None
    try:
        with open(res.image_path, "rb") as fh:
            data = fh.read()
    except Exception:
        logger.debug("[image_autosend] 读取出图文件失败", exc_info=True)
        return None
    if not data:
        return None
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(res.image_path), data)
        return (local, url, kind)
    except Exception:
        logger.debug("[image_autosend] 落出站媒体失败", exc_info=True)
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


def pick_registered_media(
    config: Dict[str, Any], persona_id: str, peer_text: str, *,
    avoid_id: str = "", bond_level: Optional[int] = None,
    force_generic: bool = False,
) -> Optional[Dict[str, Any]]:
    """查该人设注册相册：关键词命中，或（是泛化「要照片/自拍」请求时）通用池。命中返回行 dict。

    关键词触发**独立于** selfie/object 意图——运营给某条视频配 ``跳舞`` 触发词，客户说
    "给我跳个舞" 就命中，无需它是"自拍/物体图"请求。泛化要图（``detect_selfie_request``）则
    额外放开无触发词的通用相册池（对齐老"随机挑自拍"）。
    ``force_generic=True``＝调用方已从别处判定这是一次要图（承诺兑现/offer-接受桥），
    peer_text 没有关键词也放开通用池。
    """
    scfg = resolve_image_autosend_cfg(config)
    if not scfg.get("enabled", False):
        return None
    try:
        from src.ai.companion_selfie import detect_selfie_request
        from src.companion.persona_media import pick_media
        from src.companion.persona_media_store import get_persona_media_store
    except Exception:
        return None
    store = get_persona_media_store()
    if store is None:
        return None
    generic_ok = bool(force_generic) or bool(
        detect_selfie_request(str(peer_text or "")))
    return pick_media(
        store, str(persona_id or ""), str(peer_text or ""),
        generic_ok=generic_ok, avoid_id=avoid_id, bond_level=bond_level)


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


async def _llm_caption_safe(fn, *, kind: str, subject: str = "", scene: str = "") -> str:
    """调 LLM 写照片配文（知道图已发出的上下文配文）；失败/超长回空串让调用方回落。

    ``scene``（Phase18）＝照片实际拍摄场景，透传给配文指令（图文叙事一体）。
    回调旧签名 ``fn(kind, subject)`` 兼容（TypeError 回退），防漏改的调用方拿不到配文。
    """
    if fn is None:
        return ""
    try:
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
        row = st.add(str(persona_id), "photo", str(dst), "", triggers=[],
                     tags=[AUTO_REG_TAG], created_by="image_autosend")
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
    directive_override: Optional[Dict[str, str]] = None,
    requested_scene: str = "",
    on_sent: Optional[Callable[[str, str], None]] = None,
) -> bool:
    """autosend「按需发图」总编排（可单测）：注册相册优先（关键词/通用池，图/视频秒发），
    否则回落生成（selfie 相册/openai、物体图 text2img）。任一步真发出即返回 True（跳过语音/文本）。

    ``send_fn(media_path, media_url, media_type, caption, inbox_text) -> awaitable[bool]``
    由调用方提供（负责编排器发送 + 事件循环 marshalling）。
    ``llm_caption(kind, subject) -> awaitable[str]``（可选）：**知道图已发出**的上下文配文
    （文图协同）；缺省/失败回落固定配文 → ai_text。
    ``assume_intent="selfie"``＝调用方已判定要发自拍（出站文本承诺了照片的「兑现」路径），
    跳过 peer_text 意图判定直接走自拍链（相册通用池/生成；预算与关系闸门照常）。
    ``directive_override={"kind","scene"}``＝主 LLM 的发图指令（[PHOTO …] 标记，
    2026-07-14 决策权上移）：跳过关键词判定与注册相册，直接按 LLM 给的类型+对话内
    场景生成（scene 直通 ``stage_image_file``；预算/vision_gate 闸门照常）。
    ``requested_scene``（Phase20）＝调用方解析出的期望场景（如「跟上次一样的」
    取自已发媒体日志），优先于场景轮换（低于 directive_override/客户显式点名）。
    ``on_sent(note, scene)``（Phase20，可选同步回调）＝真发出后通知调用方
    「发了什么/什么场景」——B 线借此把媒体记进与 A 线共用的 ``_media_sent_log``。
    """
    scfg = resolve_image_autosend_cfg(config)
    if not scfg.get("enabled", False):
        return False
    ck = conv_key or f"{platform}:{account_id}:{chat_key}"

    def _notify_sent(note: str, scene: str) -> None:
        """发出成功后的调用方通知（软失败：日志记录绝不影响已完成的发送）。"""
        if on_sent is None:
            return
        try:
            on_sent(str(note or ""), str(scene or ""))
        except Exception:
            logger.debug("[image_autosend] on_sent 回调异常（忽略）", exc_info=True)

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
              "subject": _scene, "scene": _scene}
        if _d["kind"] == KIND_OBJECT:
            # object 链吃 prompt 字段；LLM 已给英文主体 → 直接组生图 prompt，
            # 无需 llm_refine 二次调用（那是给正则抽中文主体兜底的）。
            from src.ai.contextual_image import build_object_image_prompt
            _d["prompt"] = build_object_image_prompt(
                _scene, style=str(scfg.get("style") or ""))
        staged = await stage_image_file(
            config, platform, account_id, persona_id, _d, llm_refine=None)
        if not staged:
            record_image_fallback("directive_stage_failed")
            return False
        local, url, kind = staged
        # 配文优先级与关键词链相反：**LLM 正文优先**——标记和正文出自同一次思考
        # （正文"刚在图书馆自习完啦"+图书馆场景图），天然文图一致，无需再调 LLM 配文。
        cap = str(ai_text or "").strip()
        cap_src = "draft" if cap else ""
        if not cap:
            cap = await _llm_caption_safe(
                llm_caption, kind=kind, subject=_d["subject"],
                scene=(_d["scene"] if kind == KIND_SELFIE else ""))
            cap_src = "llm" if cap else ""
        if not cap:
            cap = (str(scfg.get("contextual_caption") or "")
                   if kind == KIND_OBJECT else str(scfg.get("caption") or ""))
            cap_src = "fixed" if cap else ""
        try:
            ok = bool(await send_fn(local, url, "image", cap,
                                    ("[图片] " + (cap or "")).strip()))
        except Exception:
            logger.debug("[image_autosend] 指令图投递异常", exc_info=True)
            ok = False
        if ok:
            if cap_src:
                record_caption(cap_src, cap)
            record_image_sent(kind, source="llm_directive")
            _notify_sent("[图片] " + (cap or ""),
                         _d["scene"] if kind == KIND_SELFIE else "")
            logger.info(
                "[autosend image] 已发图(LLM指令) platform=%s acct=%s kind=%s scene=%r",
                platform, account_id, kind, _d["scene"][:120])
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
    row = pick_registered_media(
        config, persona_id, peer_text, avoid_id=last_media_sent(ck),
        force_generic=bool(assume_intent))
    # 相册自动扩容：通用池只剩「上次刚发过的那张 auto 照片」且额度未满 → 本次改走
    # 生成（新场景照，发完自动入册），相册有机长到 max 张后回到纯轮换。
    if row and _should_grow_album(scfg, persona_id, row, ck):
        logger.info("[image_autosend] 相册避重耗尽→生成新照扩容 persona=%s", persona_id)
        row = None
    if row:
        local = str(row.get("file_path") or "")
        url = str(row.get("url") or "")
        mt = str(row.get("media_type") or "photo")
        cap = media_caption(row, lang, fallback="")
        cap_src = "registry" if cap else ""
        if not cap:
            # 相册条目无运营配文（如 auto 定妆照）→ LLM 按当前对话写配文 → 固定配文。
            _k = "video" if mt == "video" else KIND_SELFIE
            cap = await _llm_caption_safe(llm_caption, kind=_k)
            cap_src = "llm" if cap else ""
        if not cap:
            cap = str(scfg.get("caption") or "")
            cap_src = "fixed" if cap else ""
        if not cap:
            cap = str(ai_text or "")
            cap_src = "draft" if cap else ""
        if local or url:
            tag = "[视频] " if mt == "video" else "[图片] "
            try:
                ok = bool(await send_fn(local, url, mt, cap, (tag + (cap or "")).strip()))
            except Exception:
                logger.debug("[image_autosend] 注册媒体投递异常", exc_info=True)
                ok = False
            if ok:
                if cap_src:
                    record_caption(cap_src, cap)
                note_media_sent(ck, str(row.get("id")))
                try:
                    from src.companion.persona_media_store import get_persona_media_store
                    st = get_persona_media_store()
                    if st is not None:
                        st.record_hit(str(row.get("id")))
                except Exception:
                    pass
                record_image_sent(mt, source="registry")
                _notify_sent((tag + (cap or "")).strip(), "")  # 相册现成图场景未知
                logger.info(
                    "[autosend image] 已发相册媒体 platform=%s acct=%s type=%s id=%s",
                    platform, account_id, mt, row.get("id"))
                return True
            record_image_fallback("registry_deliver_failed")

    # 2) 回落生成（原有：selfie 相册/openai img2img、物体图 text2img）。
    # 承诺兑现/offer-接受路径（assume_intent）跳过 peer_text 意图判定——意图来自
    # 出站承诺或上一轮 offer，本条客户文本可能没有任何要图关键词。
    if assume_intent:
        directive: Optional[Dict[str, Any]] = {"kind": str(assume_intent)}
    else:
        directive = plan_autosend_image(peer_text, history, scfg)
    if not directive:
        return False
    # Phase18 场景显式化：自拍 + 真出图后端时，把本次将用的场景**先算出来**放进
    # directive（客户点名场景 → 调用方期望场景（Phase20「跟上次一样的」取自媒体
    # 日志）→ 场景状态轮换含相册扩容 salt——与 stage_image_file 内部回落同口径）
    # → 配文 LLM 能拿到「照片实际拍摄场景」，图、配文、（草稿链的）聊天场景状态
    # 三者同源。album 后端挑现成图，场景未知不注。
    _backend = str(((scfg.get("provider") or {}).get("backend")) or "").lower()
    if (str(directive.get("kind") or "") == KIND_SELFIE
            and not str(directive.get("scene") or "").strip()
            and _backend not in ("", "disabled", "album")):
        if str(requested_scene or "").strip():
            directive["scene"] = str(requested_scene).strip()
        else:
            try:
                from src.ai.companion_selfie import resolve_current_scene
                directive["scene"] = resolve_current_scene(
                    _resolve_persona(persona_id), scfg,
                    salt=_auto_photo_count(persona_id))
            except Exception:
                logger.debug("[image_autosend] 场景解析跳过", exc_info=True)
    staged = await stage_image_file(
        config, platform, account_id, persona_id, directive, llm_refine=llm_refine)
    if not staged:
        record_image_fallback("stage_failed")
        return False
    local, url, kind = staged
    # 配文（文图协同）：LLM 上下文配文（知道图已发出+场景）→ 固定配文 → 草稿文本兜底。
    _subject = str((directive or {}).get("subject") or "")
    _scene = str((directive or {}).get("scene") or "") if kind == KIND_SELFIE else ""
    cap = await _llm_caption_safe(
        llm_caption, kind=kind, subject=_subject, scene=_scene)
    cap_src = "llm" if cap else ""
    if not cap:
        if kind == KIND_OBJECT:
            cap = str(scfg.get("contextual_caption") or "")
        else:
            cap = str(scfg.get("caption") or "")
        cap_src = "fixed" if cap else ""
    if not cap:
        cap = str(ai_text or "")
        cap_src = "draft" if cap else ""
    try:
        ok = bool(await send_fn(local, url, "image", cap, ("[图片] " + (cap or "")).strip()))
    except Exception:
        logger.debug("[image_autosend] 生成图投递异常", exc_info=True)
        ok = False
    if ok:
        if cap_src:
            record_caption(cap_src, cap)
        record_image_sent(kind, source="keyword")
        _notify_sent("[图片] " + (cap or ""), _scene)
        logger.info(
            "[autosend image] 已发图(生成) platform=%s acct=%s kind=%s",
            platform, account_id, kind)
        # 自动定妆：真出图后端生成的自拍入册，下次同类请求秒发同一张（脸恒定、零 GPU）。
        # album 后端不入册（图本来就来自相册，登记是循环）。入册后记会话避重，
        # 使下一次请求轮换到旧照或触发下一轮扩容。
        _backend = str(((scfg.get("provider") or {}).get("backend")) or "").lower()
        if kind == KIND_SELFIE and _backend not in ("", "album", "disabled"):
            _new_id = _maybe_register_generated_selfie(scfg, persona_id, local)
            if _new_id:
                note_media_sent(ck, _new_id)
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
    bcfg = ((config or {}).get("companion") or {}).get("bazi") or {}
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
    "record_image_sent", "record_image_fallback", "metrics_snapshot",
    "image_gen_inflight",
]
