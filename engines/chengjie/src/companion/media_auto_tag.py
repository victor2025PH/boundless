"""相册媒体 VLM 自动打标（实施90 P0-3）：上传识图 → 场景/时段/季节/地点标签
+ 触发词/门槛/配文**建议**，把「82 张图逐张手填」变成「AI 建议 + 一键采纳」。

分层：
- **纯函数层**（prompt / 解析 / 标签合并 / 建议构造）零 IO 可单测；
- **执行层**＝单工位线程执行器（同一时刻至多一路打标流量在打 VLM，不与聊天
  链抢 GPU；176/140 端点切换由 VisionClient 自己管）。

三条硬语义：
1. **建议态**：触发词 / 关系门槛只进 ``auto_meta`` 供 UI 采纳，绝不自动生效；
   canonical 维度标签（``scene:``/``tod:``/``season:``/``place:``）直接落 ``tags``
   ——但**运营已有值优先，机器只补缺**（与 tod 回填 merge_file_meta 同哲学）。
2. **保守**：VLM 答 unknown / 低置信 / 解析失败一律不落维度标签；地点标签只收
   「EXIF GPS（上传时抽的结论级提示）> VLM 高置信地标」；季节以**画面证据**
   为准（客户看到什么就是什么），EXIF 拍摄月只在画面无证据时补位。
3. **隐私**：本路径只走**自家 GPU**——LAN VisionClient（provider 钉 openai_compatible +
   ``vision.base_urls`` 私网端点），或 ``hosted_gateway`` 注入的官网网关
   （``vision._hosted_vision`` 标记；bd2026.cc → 隧道 → 同一批 176/140，D-N3 2026-09-07：
   外网客户机识图走官网网关）。真人素材照**绝不**回落第三方云端视觉；两者都没有＝打标失败。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TAG_PENDING = "pending"
TAG_TAGGED = "tagged"
TAG_FAILED = "failed"
TAG_SKIPPED = "skipped"

_META_VERSION = 1
_DESC_MAX = 80
_TRIGGER_MAX_LEN = 12
_TRIGGER_MAX_N = 6


def resolve_album_ai_cfg(cfg: Any) -> Dict[str, Any]:
    """``companion.selfie.album_ai`` 配置。

    - ``enabled``（默认 **True**，D-N3 2026-09-07 #238）：上传即自动打标的总闸。此前默认 False
      ＝ skuio 144 张全部 ``autotag=False``、AI 发图只能随机（K5XHJ2 325 张同病）；识图走自家 GPU
      （LAN 或官网网关），成本可接受。运营要关请显式 ``enabled: false``；
    - ``auto_on_upload``（默认 True，受 enabled 闸）：上传成功后排队打标；
    - ``max_batch``（默认 200）：一次「补标」批任务的条目上限；
    - ``face_check``（默认 True）：打标时顺带与锁脸基准照做同人比对——
      **只写建议**（auto_meta.face_match，UI 黄标），绝不自动拦/下架
      （VLM 同人判定误杀代价高，人来裁决）。无基准照自然跳过。
    手动「补标 / 重新打标」端点**不受 enabled 闸**（运营显式动作，与回填 CLI
    同语义）——但仍需 vision LAN 端点在场才真的打得动。
    """
    c = cfg if isinstance(cfg, dict) else {}
    sel = ((c.get("companion") or {}).get("selfie") or {}) if isinstance(
        c.get("companion"), dict) else {}
    a = sel.get("album_ai") if isinstance(sel.get("album_ai"), dict) else {}
    try:
        max_batch = int(a.get("max_batch", 200) or 200)
    except (TypeError, ValueError):
        max_batch = 200
    return {
        "enabled": bool(a.get("enabled", True)),
        "auto_on_upload": bool(a.get("auto_on_upload", True)),
        "max_batch": max(1, max_batch),
        "face_check": bool(a.get("face_check", True)),
        # Q-6 E：建议词默认直接参与匹配（auto）；confirm = 只展示等一键采纳。
        "apply": "confirm" if str(a.get("apply") or "auto").strip().lower() == "confirm" else "auto",
    }


def build_auto_tag_prompt() -> str:
    """单次 VLM 调用出全部维度的严格 JSON 指令（英文——本地 VLM 遵循度最好）。"""
    from src.companion.persona_media import SCENE_CLASSES
    scenes = ", ".join(SCENE_CLASSES)
    return (
        "You are labeling ONE photo for a persona photo library. "
        "Answer with STRICT JSON only, no prose. Fields:\n"
        f'- "scene": one of [{scenes}] or "other"\n'
        '- "tod": "day" | "night" | "unknown" (lighting evidence only; '
        "indoor with no visible window/sky => unknown)\n"
        '- "season": "spring" | "summer" | "autumn" | "winter" | "unknown" '
        "(ONLY explicit visual evidence: snow or bare trees => winter; "
        "beachwear + strong sun => summer; cherry blossoms => spring; "
        "red fallen leaves => autumn; otherwise unknown)\n"
        '- "place_country": ISO-3166 two-letter country code or "" (ONLY when '
        "a well-known landmark or unmistakable location cue identifies the "
        "country)\n"
        '- "place_confidence": "high" | "low"\n'
        '- "landmark": short landmark name or ""\n'
        '- "indoor": "indoor" | "outdoor" | "unknown"\n'
        '- "people_count": integer, MAIN foreground human subjects only '
        "(ignore blurred or distant passers-by)\n"
        '- "outfit": short English phrase of the main subject\'s clothing '
        'or ""\n'
        '- "objects": up to 5 short English nouns of prominent objects\n'
        '- "nsfw": true if any sexy/revealing content\n'
        '- "explicit": true only for truly explicit content\n'
        '- "looks_underage": true if the main subject appears under 18\n'
        '- "quality": "ok" | "blurry" | "dark"\n'
        '- "desc_zh": one short Simplified-Chinese sentence (<= 40 chars) '
        "describing the photo\n"
        '- "triggers_zh": 3-6 short Simplified-Chinese keywords a customer '
        "might say that this photo answers (each <= 6 chars, no punctuation)\n"
        '- "sensitivity": 0 (fine for anyone) | 1 (casual friends) | '
        "2 (close relationship) | 3 (intimate/private) based on how "
        "private or revealing the photo is\n"
        '- "kind": "selfie" | "indoor" | "outdoor" | "food" | "pet" | "other" '
        "(selfie=close-up of the persona; outdoor=landscape/scenery; "
        "food=meals; pet=animals; indoor=rooms without being a selfie)\n"
        "Rules: be conservative — when unsure use \"unknown\" / \"\" / false. "
        "STRICT JSON only."
    )


_JSON_BLOB_RE = re.compile(r"\{.*\}", re.DOTALL)

_FACE_VALUES = ("yes", "no", "unsure")


def build_face_check_prompt() -> str:
    """锁脸基准照 vs 相册照片的同人比对指令（图1=基准，图2=待检）。"""
    return (
        "Image 1 is the reference face of a persona. Image 2 is a photo "
        "from her album. Judge ONLY whether the MAIN person in image 2 is "
        "the SAME person as image 1.\n"
        'Answer with STRICT JSON only: {"same_person": "yes"} or '
        '{"same_person": "no"} or {"same_person": "unsure"}.\n'
        "Rules: different angle/makeup/lighting of the same person => yes; "
        "clearly a different face => no; face too small, occluded, or you "
        "are not confident => unsure."
    )


_FACE_JSON_RE = re.compile(r'"same_person"\s*:\s*"(yes|no|unsure)"',
                           re.IGNORECASE)


def parse_face_check_response(raw: Any) -> str:
    """同人比对回答 → yes/no/unsure；解析不出 ""（保守=不写建议）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            v = str(data.get("same_person") or "").strip().lower()
            return v if v in _FACE_VALUES else ""
    except Exception:
        pass
    m = _FACE_JSON_RE.search(s)
    if m:
        return m.group(1).lower()
    low = s.lower()
    if len(low) <= 16:
        for v in _FACE_VALUES:
            if v in low:
                return v
    return ""


def _clean_trigger(t: Any) -> str:
    s = re.sub(r"[\s,，、;；:：!！?？\.。\"'`]+", "", str(t or ""))
    return s if 0 < len(s) <= _TRIGGER_MAX_LEN else ""


def parse_auto_tag_response(raw: Any) -> Optional[Dict[str, Any]]:
    """VLM 回答 → 归一化结论 dict；完全解析不出返回 None（保守）。

    所有维度值经 media_taxonomy 归一：unknown/非法 → ""＝该维度不落标签。
    """
    from src.companion.media_taxonomy import (
        normalize_country, normalize_scene, normalize_season, normalize_tod,
    )
    s = str(raw or "").strip()
    if not s:
        return None
    data: Any = None
    try:
        data = json.loads(s)
    except Exception:
        m = _JSON_BLOB_RE.search(s)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return None

    def _s(key: str, cap: int) -> str:
        v = str(data.get(key) or "").strip()
        if v.lower() in ("none", "unknown", "n/a", "null"):
            return ""
        return v[:cap]

    try:
        people = int(data.get("people_count"))
        people = max(0, min(20, people))
    except (TypeError, ValueError):
        people = -1  # 未知
    try:
        sens = max(0, min(3, int(data.get("sensitivity"))))
    except (TypeError, ValueError):
        sens = 0
    indoor = str(data.get("indoor") or "").strip().lower()
    if indoor not in ("indoor", "outdoor"):
        indoor = ""
    quality = str(data.get("quality") or "").strip().lower()
    if quality not in ("ok", "blurry", "dark"):
        quality = ""
    conf = str(data.get("place_confidence") or "").strip().lower()
    if conf not in ("high", "low"):
        conf = "low"
    triggers: List[str] = []
    for t in (data.get("triggers_zh") or []) if isinstance(
            data.get("triggers_zh"), list) else []:
        c = _clean_trigger(t)
        if c and c not in triggers:
            triggers.append(c)
        if len(triggers) >= _TRIGGER_MAX_N:
            break
    objects: List[str] = []
    for o in (data.get("objects") or []) if isinstance(
            data.get("objects"), list) else []:
        c = str(o or "").strip()[:24]
        if c and c not in objects:
            objects.append(c)
        if len(objects) >= 5:
            break
    scene_n = normalize_scene(data.get("scene"))
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("selfie", "indoor", "outdoor", "food", "pet", "other"):
        try:
            from src.companion.persona_media import infer_scene_kind
            kind = infer_scene_kind(
                {"objects": objects, "desc": _s("desc_zh", _DESC_MAX),
                 "indoor": indoor, "people_count": people, "scene": scene_n},
                scene_n)
        except Exception:
            kind = "other"
    return {
        "scene": scene_n,
        "tod": normalize_tod(data.get("tod")),
        "season": normalize_season(data.get("season")),
        "country": normalize_country(data.get("place_country")),
        "country_conf": conf,
        "landmark": _s("landmark", 40),
        "indoor": indoor,
        "people_count": people,
        "outfit": _s("outfit", 60),
        "objects": objects,
        "nsfw": bool(data.get("nsfw")),
        "explicit": bool(data.get("explicit")),
        "underage": bool(data.get("looks_underage")),
        "quality": quality,
        "desc": _s("desc_zh", _DESC_MAX),
        "triggers": triggers,
        "sensitivity": sens,
        "kind": kind,
    }


def hard_reject(parsed: Optional[Dict[str, Any]]) -> bool:
    """硬红线（与 image_gate 同语义）：疑似未成年 × 性感/露骨 → 必须下架。"""
    p = parsed or {}
    return bool(p.get("underage")) and (
        bool(p.get("nsfw")) or bool(p.get("explicit")))


def detect_tag_conflicts(
    parsed: Dict[str, Any],
    existing_tags: Optional[List[Any]] = None,
) -> Dict[str, Dict[str, str]]:
    """人工已有维度标注 vs AI 结论的**分歧提示**（建议态纯函数）。

    人工值永远优先（derive_tags 绝不覆盖），但「人工标 park、VLM 看是
    night_city」这类分歧值得点名给运营复核（2026-08-30 生产 dry-run 实锤
    两例）。防误报三条：AI 无结论不算分歧；场景走等价组互认（镜前自拍
    home vs bedroom 都对）；place 只拿 AI 高置信结论对比。
    返回 ``{维度: {"manual": v, "ai": v}}``；无分歧 {}。
    """
    from src.companion.media_taxonomy import (
        normalize_country, normalize_scene, normalize_season, normalize_tod,
        scenes_equivalent,
    )
    tags = [str(t) for t in (existing_tags or []) if str(t or "").strip()]

    def _manual(prefix: str) -> str:
        for t in tags:
            if t.startswith(prefix):
                return t[len(prefix):].strip()
        return ""

    out: Dict[str, Dict[str, str]] = {}
    m_scene = normalize_scene(_manual("scene:"))
    a_scene = str(parsed.get("scene") or "")
    if m_scene and a_scene and not scenes_equivalent(m_scene, a_scene):
        out["scene"] = {"manual": m_scene, "ai": a_scene}
    m_tod = normalize_tod(_manual("tod:"))
    a_tod = str(parsed.get("tod") or "")
    if m_tod and a_tod and m_tod != a_tod:
        out["tod"] = {"manual": m_tod, "ai": a_tod}
    m_season = normalize_season(_manual("season:"))
    a_season = str(parsed.get("season") or "")
    if m_season and a_season and m_season != a_season:
        out["season"] = {"manual": m_season, "ai": a_season}
    m_place = normalize_country(_manual("place:"))
    a_place = (normalize_country(parsed.get("country"))
               if str(parsed.get("country_conf") or "") == "high" else "")
    if m_place and a_place and m_place != a_place:
        out["place"] = {"manual": m_place, "ai": a_place}
    return out


def derive_tags(
    parsed: Dict[str, Any],
    exif_hints: Optional[Dict[str, Any]] = None,
    existing_tags: Optional[List[Any]] = None,
) -> Optional[List[str]]:
    """结论 → canonical 维度标签合并（纯函数）。

    - 运营已有的同前缀标签**绝不覆盖**（人工值优先）；
    - season：画面证据（VLM）优先，EXIF（拍摄月+GPS 半球）只补缺；
    - place：EXIF GPS 国别 > VLM 高置信地标；低置信只留在 auto_meta。
    返回合并后的完整 tags 列表；无任何新增返回 None（调用方零写）。
    """
    from src.companion.media_taxonomy import normalize_country, normalize_season
    hints = exif_hints if isinstance(exif_hints, dict) else {}
    cur = [str(t) for t in (existing_tags or []) if str(t or "").strip()]
    have = {t.split(":", 1)[0] for t in cur if ":" in t}
    add: List[str] = []
    scene = str(parsed.get("scene") or "")
    if scene and "scene" not in have:
        add.append(f"scene:{scene}")
    kind = str(parsed.get("kind") or "").strip().lower()
    if kind in ("selfie", "indoor", "outdoor", "food", "pet", "other") and "kind" not in have:
        add.append(f"kind:{kind}")
    tod = str(parsed.get("tod") or "")
    if tod and "tod" not in have:
        add.append(f"tod:{tod}")
    season = str(parsed.get("season") or "") or normalize_season(
        hints.get("season"))
    if season and "season" not in have:
        add.append(f"season:{season}")
    place = normalize_country(hints.get("country"))
    if not place and str(parsed.get("country_conf") or "") == "high":
        place = normalize_country(parsed.get("country"))
    if place and "place" not in have:
        add.append(f"place:{place}")
    if not add:
        return None
    return cur + add


def build_auto_meta(
    parsed: Dict[str, Any],
    exif_hints: Optional[Dict[str, Any]] = None,
    *, model: str = "", now: Optional[float] = None,
) -> Dict[str, Any]:
    """打标结论 → ``auto_meta``（建议态载荷，UI 消费）。"""
    from src.companion.media_taxonomy import suggest_min_bond
    hints = exif_hints if isinstance(exif_hints, dict) else {}
    sens = int(parsed.get("sensitivity") or 0)
    return {
        "v": _META_VERSION,
        "ts": float(now if now is not None else time.time()),
        "model": str(model or ""),
        "desc": str(parsed.get("desc") or ""),
        "triggers_suggest": list(parsed.get("triggers") or []),
        "sensitivity": sens,
        "suggest_min_bond": suggest_min_bond(sens),
        "quality": str(parsed.get("quality") or ""),
        "outfit": str(parsed.get("outfit") or ""),
        "objects": list(parsed.get("objects") or []),
        "landmark": str(parsed.get("landmark") or ""),
        "country": str(parsed.get("country") or ""),
        "country_conf": str(parsed.get("country_conf") or ""),
        "indoor": str(parsed.get("indoor") or ""),
        "people_count": int(parsed.get("people_count", -1)),
        "scene_kind": str(parsed.get("kind") or ""),
        "flags": {
            "nsfw": bool(parsed.get("nsfw")),
            "explicit": bool(parsed.get("explicit")),
            "underage": bool(parsed.get("underage")),
        },
        "exif": {
            "year": int(hints.get("year") or 0),
            "month": int(hints.get("month") or 0),
            "country": str(hints.get("country") or ""),
            "season": str(hints.get("season") or ""),
        },
    }


# ── 执行层：LAN VisionClient + 单工位执行器 ─────────────────────────────────

_LOCK = threading.Lock()
_EXECUTOR: Optional[ThreadPoolExecutor] = None
_BATCH_ACTIVE = False
_STATS: Dict[str, Any] = {
    "queued": 0, "done": 0, "failed": 0, "skipped": 0,
    "last_error": "", "last_ts": 0.0,
}
_VISION_CACHE: Dict[str, Any] = {"fp": "", "client": None}
# 识图端点熔断（#238）：一次 vlm_unavailable 后 _VLM_COOLDOWN_SEC 内后续任务直接判 failed，
# 不再逐条打死端点——否则 144 张上传排队的 144 个任务会在单工位上把 60s 超时跑满两个多小时，
# 期间「AI 补标」也排不进去。到点自动放行一条探路，成功即恢复。
_VLM_COOLDOWN_SEC = 90.0
_VLM_DOWN_UNTIL = 0.0
VISION_REASON_NO_ENDPOINT = "no_endpoint"        # 无私网端点、也无官网网关注入 → 识图未配置
VISION_REASON_INIT_FAILED = "client_init_failed"  # 端点在但 VisionClient 起不来
VISION_REASON_COOLDOWN = "unreachable"            # 刚失败过、熔断中（端点不可达）


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="album-autotag")
        return _EXECUTOR


def _bump(key: str, err: str = "") -> None:
    with _LOCK:
        _STATS[key] = int(_STATS.get(key) or 0) + 1
        _STATS["last_ts"] = time.time()
        if err:
            _STATS["last_error"] = err[:200]


def stats_snapshot() -> Dict[str, Any]:
    with _LOCK:
        out = dict(_STATS)
        out["batch_active"] = _BATCH_ACTIVE
        return out


def _is_private_url(url: Any) -> bool:
    """打标允许的端点：仅私网/回环地址（IP 判 is_private；域名一律视为公网）。"""
    try:
        from urllib.parse import urlparse
        host = str(urlparse(str(url or "")).hostname or "")
        if not host:
            return False
        if host.lower() == "localhost":
            return True
        import ipaddress
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False   # 非 IP（域名）＝公网，一律不给相册照片
    except Exception:
        return False


def _is_hosted_gateway_url(url: Any, vision_cfg: Any) -> bool:
    """官网网关端点（D-N3）：仍是自家 GPU（bd2026.cc → 117 → VPS 隧道 → 176/140），不是第三方云。

    只在 ``hosted_gateway.ensure_hosted_vision`` / ``ConfigManager._apply_hosted_vision_env``
    打了 ``_hosted_vision`` 标记、且 URL 就是它们注入的那一条（== ``base_url`` 或 env
    ``AITR_HOSTED_VISION_BASE_URL``，https + ``/api/ai/v1``）时放行——任何别的公网域名仍一律拒绝。
    """
    if not (isinstance(vision_cfg, dict) and vision_cfg.get("_hosted_vision")):
        return False
    u = str(url or "").strip().rstrip("/")
    if not u.startswith("https://") or not u.endswith("/api/ai/v1"):
        return False
    injected = {
        str(vision_cfg.get("base_url") or "").strip().rstrip("/"),
        (os.environ.get("AITR_HOSTED_VISION_BASE_URL") or "").strip().rstrip("/"),
    }
    injected.discard("")
    return u in injected


def _lan_vision_cfg(vision_cfg: Any) -> Optional[Dict[str, Any]]:
    """强制「自家 GPU」形态的 vision 配置；无私网端点也无官网网关注入返回 None（隐私硬闸）。

    2026-08-30 生产实锤加固：实例 vision 是聊天识图的调优形态——``base_urls``
    里混着**云端点**（siliconflow）且 176 的 ``endpoint_timeouts`` 压到 3s。
    直接透传＝① 慢一点的打标调用被 3s 掐死，② 掐死后 failover 把真人素材照
    送云（隐私硬线被配置形态击穿）。故这里**按 host 过滤只留私网端点**（+ D-N3
    官网网关注入的那一条，见 ``_is_hosted_gateway_url``）、剥掉分端点超时并给打标
    专用宽超时（60s，批处理不赶时间）。
    """
    c = dict(vision_cfg) if isinstance(vision_cfg, dict) else {}
    urls = c.get("base_urls") or ([c["base_url"]] if c.get("base_url") else [])
    lan = [u for u in urls if _is_private_url(u) or _is_hosted_gateway_url(u, c)]
    if not lan:
        return None
    c["provider"] = "openai_compatible"
    c["zhipu_api_key"] = ""   # 双保险：绝不带云 key
    c["base_urls"] = lan
    c["base_url"] = lan[0]
    c.pop("endpoint_timeouts", None)   # 聊天快车道的 3s 掐打标；打标用全局宽限
    try:
        c["timeout"] = max(60, int(c.get("timeout") or 0))
    except (TypeError, ValueError):
        c["timeout"] = 60
    return c


def _get_vision_client(vision_cfg: Any) -> Optional[Any]:
    """LAN VisionClient 获取（模块级缓存，配置指纹变了才重建）。"""
    cfg = _lan_vision_cfg(vision_cfg)
    if cfg is None:
        return None
    fp = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
    with _LOCK:
        client = _VISION_CACHE["client"] if _VISION_CACHE["fp"] == fp else None
    if client is not None:
        return client
    try:
        from src.vision_client import VisionClient
        vc = VisionClient(cfg)
        if not vc.initialize():
            return None
        with _LOCK:
            _VISION_CACHE["fp"], _VISION_CACHE["client"] = fp, vc
        return vc
    except Exception:
        logger.debug("[media_auto_tag] VisionClient 初始化失败", exc_info=True)
        return None


def _vlm_cooling(now: Optional[float] = None) -> bool:
    with _LOCK:
        return (now if now is not None else time.time()) < _VLM_DOWN_UNTIL


def _vlm_mark_down(now: Optional[float] = None) -> None:
    global _VLM_DOWN_UNTIL
    with _LOCK:
        _VLM_DOWN_UNTIL = (now if now is not None else time.time()) + _VLM_COOLDOWN_SEC


def _vlm_mark_up() -> None:
    global _VLM_DOWN_UNTIL
    with _LOCK:
        _VLM_DOWN_UNTIL = 0.0


def vision_probe(vision_cfg: Any) -> Dict[str, Any]:
    """识图能不能打（配置层 + 熔断层，**不发网络请求**）→ ``{ready, reason}``（#238）。

    上传 / 补标路由据此决定要不要排任务，前端据此把「识图服务暂不可用」说出来而不是显示 0/144：
    - ``no_endpoint``：无私网端点也无官网网关注入（识图未配置 / 纯云形态）；
    - ``client_init_failed``：端点在、VisionClient 起不来；
    - ``unreachable``：刚有任务 vlm_unavailable、熔断冷却中（真不可达要靠任务结果，这里只报最近结论）。
    """
    if _lan_vision_cfg(vision_cfg) is None:
        return {"ready": False, "reason": VISION_REASON_NO_ENDPOINT}
    if _vlm_cooling():
        return {"ready": False, "reason": VISION_REASON_COOLDOWN}
    if _get_vision_client(vision_cfg) is None:
        return {"ready": False, "reason": VISION_REASON_INIT_FAILED}
    return {"ready": True, "reason": ""}


def _vision_describe(vision_cfg: Any, image_path: str, prompt: str) -> Optional[str]:
    if _vlm_cooling():
        return None
    client = _get_vision_client(vision_cfg)
    if client is None:
        return None
    try:
        out = client.describe_image_sync(str(image_path), prompt)
    except Exception:
        logger.debug("[media_auto_tag] VLM 调用失败", exc_info=True)
        _vlm_mark_down()
        return None
    if out is None and str(getattr(client, "last_fail", "") or ""):
        # 端点循环全部抛异常（VisionClient 内部已吞、last_fail 记 kind）＝不可达 → 熔断；
        # 单张图转码失败 / 模型空答（last_fail 为空）不算端点问题，不熔断。
        _vlm_mark_down()
    elif out is not None:
        _vlm_mark_up()
    return out


def _row_image_path(row: Dict[str, Any]) -> str:
    """打标输入图：照片用原图；视频用抽帧封面（无封面＝没法看＝skipped）。"""
    fp = str(row.get("file_path") or "")
    if str(row.get("media_type") or "photo") == "video":
        cand = Path(fp + ".thumb.jpg") if fp else None
        return str(cand) if cand is not None and cand.is_file() else ""
    return fp if fp and Path(fp).is_file() else ""


def _vision_describe_pair(vision_cfg: Any, paths: Any, prompt: str) -> Optional[str]:
    """多图同一调用（同人比对用）；与 ``_vision_describe`` 共享 LAN 客户端。"""
    client = _get_vision_client(vision_cfg)
    if client is None:
        return None
    try:
        return client.describe_images_sync([str(p) for p in paths], prompt)
    except Exception:
        logger.debug("[media_auto_tag] 多图 VLM 调用失败", exc_info=True)
        return None


def tag_media_row(
    store: Any, row: Dict[str, Any], vision_cfg: Any, *,
    describe: Optional[Callable[[Any, str, str], Optional[str]]] = None,
    describe_pair: Optional[Callable[[Any, Any, str], Optional[str]]] = None,
    face_ref: str = "",
    now: Optional[float] = None,
) -> str:
    """给一行媒体打标（同步）；返回最终 ``tag_status``。绝不抛。

    ``face_ref`` 非空且照片主体单人 → 顺带与基准照做同人比对（建议态：
    只写 ``auto_meta.face_match``，UI 黄标，绝不自动拦）。
    """
    mid = str((row or {}).get("id") or "")
    if not mid or store is None:
        return TAG_FAILED
    path = _row_image_path(row)
    if not path:
        store.set_auto_tag(mid, tag_status=TAG_SKIPPED)
        _bump("skipped")
        return TAG_SKIPPED
    fn = describe or _vision_describe
    try:
        raw = fn(vision_cfg, path, build_auto_tag_prompt())
    except Exception:
        raw = None
    if raw is None:
        store.set_auto_tag(mid, tag_status=TAG_FAILED)
        _bump("failed", "vlm_unavailable")
        return TAG_FAILED
    parsed = parse_auto_tag_response(raw)
    if parsed is None:
        store.set_auto_tag(mid, tag_status=TAG_FAILED)
        _bump("failed", "parse")
        return TAG_FAILED
    old_meta = row.get("auto_meta") if isinstance(row.get("auto_meta"), dict) else {}
    exif_hints = old_meta.get("exif") if isinstance(old_meta.get("exif"), dict) else {}
    meta = build_auto_meta(parsed, exif_hints, model="lan_vlm", now=now)
    # 标注分歧提示（建议态）：人工值 vs AI 结论不一致就点名（人工值不动）
    conflicts = detect_tag_conflicts(parsed, row.get("tags"))
    if conflicts:
        meta["conflicts"] = conflicts
    # 脸一致性抽检（实施90 阶段二）：仅 照片 × 单人主体 × 有基准照 才比；
    # 比不了/解析不出=不写键（宁缺勿错，绝不因抽检失败拖垮打标主流程）。
    if (face_ref and Path(str(face_ref)).is_file()
            and str(row.get("media_type") or "photo") == "photo"
            and int(parsed.get("people_count", -1)) == 1):
        pfn = describe_pair or _vision_describe_pair
        try:
            fraw = pfn(vision_cfg, [str(face_ref), path],
                       build_face_check_prompt())
        except Exception:
            fraw = None
        fm = parse_face_check_response(fraw)
        if fm:
            meta["face_match"] = fm
    new_tags = derive_tags(parsed, exif_hints, row.get("tags"))
    try:
        store.set_auto_tag(
            mid, auto_meta=meta, tag_status=TAG_TAGGED,
            tags=new_tags if new_tags is not None else None)
        if hard_reject(parsed):
            # 疑似未成年×性感＝硬红线：直接下架待人工复核（UI 红标说明）。
            store.update(mid, enabled=False)
            logger.warning("[media_auto_tag] 硬红线下架 media=%s（underage×nsfw）",
                           mid)
    except Exception:
        logger.debug("[media_auto_tag] 落库失败", exc_info=True)
        _bump("failed", "store_write")
        return TAG_FAILED
    _bump("done")
    return TAG_TAGGED


def _safe_dir_name(pid: Any) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(pid or ""))[:64]
    return s or "default"


def _publish_static_copy(
    row: Dict[str, Any], publish_root: str, url_base: str,
) -> Optional[Dict[str, str]]:
    """url 为空的策展素材「发布」进 /static（幂等：同名同大小直接复用）。

    实施90 生产实锤：CLI 入册的素材 ``file_path`` 在素材目录、``url`` 为空 →
    UI 网格 `src=""` 永远裂图（比缺缩略图更深的根因）。发布＝拷一份进
    ``<publish_root>/<pid>/``（原件不动——发送链按 file_path 走），回填 url。
    """
    fp = Path(str((row or {}).get("file_path") or ""))
    if not publish_root or not url_base or not fp.is_file():
        return None
    sub = _safe_dir_name(row.get("persona_id"))
    d = Path(publish_root) / sub
    try:
        d.mkdir(parents=True, exist_ok=True)
        name = fp.name
        dst = d / name
        if dst.exists() and dst.stat().st_size != fp.stat().st_size:
            import uuid as _uuid
            name = f"{_uuid.uuid4().hex[:8]}_{fp.name}"
            dst = d / name
        if not dst.exists():
            import shutil as _sh
            _sh.copy2(fp, dst)
        return {"url": f"{url_base.rstrip('/')}/{sub}/{name}",
                "path": str(dst)}
    except Exception:
        logger.debug("[media_auto_tag] 发布进 static 失败", exc_info=True)
        return None


def backfill_row_assets(
    store: Any, row: Dict[str, Any], *,
    publish_root: str = "", url_base: str = "",
) -> Dict[str, bool]:
    """单行资产补齐：发布（补 url）→ 缩略图/视频封面 → pHash。全软失败。

    - 照片：缩略图 ``.thumb.webp``（发布件旁或上传件旁，URL 可服）；
    - 视频：封面 ``.thumb.jpg`` 写在 ``file_path`` 旁（打标输入按此约定找），
      发布态另拷一份到 static 供 thumb_url 直服；
    - pHash：照片指纹（近重复防线的数据地基）。
    """
    out = {"published": False, "thumb": False, "cover": False, "phash": False}
    mid = str((row or {}).get("id") or "")
    if not mid or store is None:
        return out
    mt = str(row.get("media_type") or "photo")
    fp = str(row.get("file_path") or "")
    url = str(row.get("url") or "")
    pub_path = ""
    if not url and publish_root and url_base:
        pub = _publish_static_copy(row, publish_root, url_base)
        if pub:
            got = store.set_auto_tag(mid, url=pub["url"])
            row = got or dict(row, url=pub["url"])
            url = pub["url"]
            pub_path = pub["path"]
            out["published"] = True
    if (mt == "photo" and url and not str(row.get("thumb_url") or "")
            and fp and Path(fp).is_file()):
        try:
            from src.companion.media_probe import make_photo_thumbnail
            tfile = (pub_path or fp) + ".thumb.webp"
            if make_photo_thumbnail(fp, tfile):
                store.set_auto_tag(mid, thumb_url=url + ".thumb.webp")
                out["thumb"] = True
        except Exception:
            logger.debug("[media_auto_tag] 缩略图补齐失败", exc_info=True)
    if (mt == "video" and url and not str(row.get("thumb_url") or "")
            and fp and Path(fp).is_file()):
        try:
            from src.companion.media_probe import make_video_thumbnail
            cover_src = fp + ".thumb.jpg"     # 打标输入按 file_path 旁约定找
            ok = Path(cover_src).is_file() or make_video_thumbnail(
                fp, cover_src, at_sec=1.0)
            if ok:
                serve_path = cover_src
                if pub_path and str(Path(pub_path)) != str(Path(fp)):
                    serve_path = pub_path + ".thumb.jpg"
                    if not Path(serve_path).is_file():
                        import shutil as _sh
                        _sh.copy2(cover_src, serve_path)
                if Path(serve_path).is_file():
                    store.set_auto_tag(mid, thumb_url=url + ".thumb.jpg")
                    out["cover"] = True
        except Exception:
            logger.debug("[media_auto_tag] 视频封面补齐失败", exc_info=True)
    if mt == "photo" and not str(row.get("phash") or ""):
        try:
            from src.companion.image_phash import phash_file
            h = phash_file(fp)
            if h:
                store.set_auto_tag(mid, phash=h)
                out["phash"] = True
        except Exception:
            logger.debug("[media_auto_tag] pHash 补齐失败", exc_info=True)
    return out


def schedule_tag(store: Any, media_id: str, vision_cfg: Any, *,
                 face_ref: str = "") -> bool:
    """上传后异步打标（fire-and-forget，单工位串行）。"""
    mid = str(media_id or "")
    if not mid or store is None:
        return False

    def _job() -> None:
        try:
            row = store.get(mid)
            if row is not None:
                tag_media_row(store, row, vision_cfg, face_ref=face_ref)
        except Exception:
            logger.debug("[media_auto_tag] 异步打标任务异常", exc_info=True)

    try:
        _get_executor().submit(_job)
        _bump("queued")
        return True
    except Exception:
        return False


def run_batch(
    store: Any, vision_cfg: Any, *,
    persona_id: Optional[str] = None,
    only_missing: bool = True,
    limit: int = 200,
    inline: bool = False,
    describe: Optional[Callable[[Any, str, str], Optional[str]]] = None,
    face_ref: str = "",
    publish_root: str = "",
    url_base: str = "",
) -> Dict[str, Any]:
    """批量补标 + 资产补齐（发布进 static / 缩略图 / 视频封面 / pHash）。

    ``inline=True``＝同步跑完（测试/CLI）；否则整批作为**一个**执行器任务后台跑
    （路由立即返回 queued 数，进度靠前端轮询 ``tag_status``/``thumb_url`` 变化）。
    ``only_missing=True`` 跳过已 tagged 行（幂等可重跑——failed/skipped 会重试，
    视频封面补齐后 skipped 自然转正）；批任务互斥防双跑。
    """
    global _BATCH_ACTIVE
    if store is None:
        return {"ok": False, "error": "store_unavailable"}
    try:
        rows = store.list(persona_id)
    except Exception:
        return {"ok": False, "error": "list_failed"}
    todo: List[Dict[str, Any]] = []
    assets: List[Dict[str, Any]] = []
    for r in rows:
        mt = str(r.get("media_type") or "photo")
        no_thumb = not str(r.get("thumb_url") or "")
        need_assets = (
            (mt == "photo" and (no_thumb or not str(r.get("phash") or "")))
            or (mt == "video" and no_thumb)
            or (not str(r.get("url") or "") and bool(publish_root)))
        if need_assets:
            assets.append(r)
        st = str(r.get("tag_status") or "")
        if only_missing and st == TAG_TAGGED:
            continue
        todo.append(r)
    todo = todo[: max(1, int(limit))]
    # #238：识图打不动（无端点 / 起不来 / 熔断中）→ 资产补齐照做、打标一条不排，
    # 把 vision_ready=False + reason 明说出去；否则前端只看到「已排 144」然后全部 failed。
    probe = vision_probe(vision_cfg) if describe is None else {"ready": True, "reason": ""}
    if not probe.get("ready"):
        todo = []
    with _LOCK:
        if _BATCH_ACTIVE and not inline:
            return {"ok": True, "queued": 0, "assets": 0, "batch_active": True,
                    "vision_ready": bool(probe.get("ready")), "vision_reason": probe.get("reason") or ""}
        _BATCH_ACTIVE = True

    def _job() -> None:
        global _BATCH_ACTIVE
        try:
            for r in assets:
                backfill_row_assets(store, r, publish_root=publish_root,
                                    url_base=url_base)
            for r in todo:
                fresh = store.get(str(r.get("id"))) or r
                tag_media_row(store, fresh, vision_cfg, describe=describe,
                              face_ref=face_ref)
        except Exception:
            logger.debug("[media_auto_tag] 批任务异常", exc_info=True)
        finally:
            with _LOCK:
                _BATCH_ACTIVE = False

    vision_out = {"vision_ready": bool(probe.get("ready")),
                  "vision_reason": probe.get("reason") or ""}
    if inline:
        _job()
        return dict({"ok": True, "queued": len(todo), "assets": len(assets),
                     "batch_active": False}, **vision_out)
    try:
        _get_executor().submit(_job)
    except Exception:
        with _LOCK:
            _BATCH_ACTIVE = False
        return {"ok": False, "error": "executor_failed"}
    return dict({"ok": True, "queued": len(todo), "assets": len(assets),
                 "batch_active": True}, **vision_out)


def reset_for_tests() -> None:
    """测试钩子：清执行器/批互斥/统计/熔断（生产勿用）。"""
    global _EXECUTOR, _BATCH_ACTIVE, _VLM_DOWN_UNTIL
    with _LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=True)
        _EXECUTOR = None
        _BATCH_ACTIVE = False
        _VLM_DOWN_UNTIL = 0.0
        for k in ("queued", "done", "failed", "skipped"):
            _STATS[k] = 0
        _STATS["last_error"] = ""
        _VISION_CACHE["fp"], _VISION_CACHE["client"] = "", None


__all__ = [
    "TAG_PENDING", "TAG_TAGGED", "TAG_FAILED", "TAG_SKIPPED",
    "VISION_REASON_NO_ENDPOINT", "VISION_REASON_INIT_FAILED", "VISION_REASON_COOLDOWN",
    "vision_probe",
    "resolve_album_ai_cfg", "build_auto_tag_prompt", "parse_auto_tag_response",
    "build_face_check_prompt", "parse_face_check_response",
    "hard_reject", "derive_tags", "detect_tag_conflicts", "build_auto_meta",
    "tag_media_row",
    "backfill_row_assets", "schedule_tag", "run_batch", "stats_snapshot",
    "reset_for_tests",
]
