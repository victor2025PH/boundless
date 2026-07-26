"""「今日衣着」确定性状态（P1 图文一致性，2026-07-27）。

背景：P0 只有 90 分钟服装连续窗（同会话相册优先同系列）——窗外/生成链的衣着
仍然每张随机（appearance 不写衣服时由生图模型自由发挥），「上一张连衣裙、
下一张睡衣」在同一天内反复穿帮；聊天文本被问「今天穿了什么」也全靠 LLM 现编，
与照片必然对不上。

方案与 ``resolve_current_scene``（场景状态）/``meal_state``（饮食状态）同哲学：
**persona+日期 确定性取值，零 LLM、零存储、明天自动换**——

- 衣橱池优先级：persona ``outfits`` 配置 → config ``consistency.outfit.pool``
  → **相册衣橱**（策展系列 slug 里认得出服装词的，如 ``white-dress`` →
  ``white dress``——生成的照片穿相册里真实存在的衣服，两链视觉世界同源）
  → 内置中性便装池。
- 「今天穿什么」= crc32(persona#日期) 从池确定性取一条，全天恒定；
  聊天状态块与 A/B 两条生成链都从这里取 → 图-文-跨链三同源。
- **连续性覆盖**：短窗口内刚发过相册某系列（P0 的 prefer_series 信号）→
  衣着跟随该系列（照片事实 > 每日默认，「再拍一张」不换装）。

纯函数为主；唯一带 IO 的 ``wardrobe_for``（相册/DB 扫系列）自带 TTL 缓存，
聊天热路径按字典读。全链软失败返回空衣着（生图 prompt 不注入 = 旧行为）。
"""
from __future__ import annotations

import re
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 内置兜底衣橱（中性便装，任何性别人设不违和；具体风格靠 persona/相册池覆盖）。
_DEFAULT_OUTFITS: Tuple[str, ...] = (
    "white t-shirt and light blue jeans",
    "beige knit sweater",
    "light gray hoodie",
    "denim jacket over a white tee",
    "black long-sleeve top and jeans",
    "soft cream cardigan",
    "navy casual shirt",
    "oversized pastel sweatshirt",
)

# 服装词表（token 级精确匹配，防 rooftop→top 误命中）：系列 slug 含任一词 →
# 认作「衣着系列」可进衣橱池 / 可作连续性衣着；否则（night-selfie / auto-* 等
# 场景系列）不当衣服用。刻意保守：认不出宁可不注入。
_GARMENT_WORDS = frozenset((
    "dress", "skirt", "shirt", "tshirt", "tee", "top", "blouse", "hoodie",
    "sweater", "cardigan", "jacket", "coat", "jeans", "denim", "pants",
    "trousers", "shorts", "leggings", "suit", "blazer", "uniform",
    "pajama", "pajamas", "nightgown", "camisole", "sweatshirt", "polo",
    "vest", "overalls", "jumpsuit", "romper", "gown", "knit", "turtleneck",
    "parka", "windbreaker", "robe", "qipao", "cheongsam", "hanfu",
    "kimono", "yukata", "swimsuit", "tank", "sweatpants", "joggers",
))

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def series_outfit_phrase(series: Any) -> str:
    """系列 slug → 衣着短语（``white-dress`` → ``white dress``）；
    不像衣着的系列（无服装词）返回 ""。纯函数。"""
    s = str(series or "").strip().lower()
    if not s:
        return ""
    tokens = [t for t in re.split(r"[-_\s]+", s) if t]
    if not tokens or not any(t in _GARMENT_WORDS for t in tokens):
        return ""
    return " ".join(tokens)


def outfit_slug(phrase: Any) -> str:
    """衣着短语 → 系列 slug（``white dress`` → ``white-dress``）：生成链发图后
    以此记连续性账本，与相册策展系列同一命名空间（round-trip 恒等）。"""
    s = str(phrase or "").strip().lower()
    if not s:
        return ""
    return _SLUG_RE.sub("-", s).strip("-")


def outfit_pool(
    persona: Any = None,
    cfg_pool: Any = None,
    wardrobe: Any = None,
) -> List[str]:
    """衣橱池解析（纯函数）：persona ``outfits`` → config 池 → 相册衣橱 →
    内置兜底。首个非空层胜出（不混层——运营显式配置即完全接管）。"""
    if isinstance(persona, dict):
        raw = persona.get("outfits")
        if isinstance(raw, (list, tuple)):
            pool = [str(x).strip() for x in raw if str(x).strip()]
            if pool:
                return pool
    if isinstance(cfg_pool, (list, tuple)):
        pool = [str(x).strip() for x in cfg_pool if str(x).strip()]
        if pool:
            return pool
    if isinstance(wardrobe, (list, tuple)):
        pool = [str(x).strip() for x in wardrobe if str(x).strip()]
        if pool:
            return pool
    return list(_DEFAULT_OUTFITS)


def resolve_today_outfit(key: str, pool: Any, now: Any = None) -> str:
    """「今天穿什么」：crc32(key#日期) 从池确定性取一条（纯函数）。

    同 persona 同一天恒定（跨请求/跨进程/跨 A、B 链一致），明天自动换；
    不同 persona 同日各异。空池返回 ""。
    """
    items = [str(x).strip() for x in (pool or []) if str(x).strip()]
    if not items:
        return ""
    import datetime as _dt

    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    payload = f"{str(key or '')}#outfit#{t.strftime('%Y-%m-%d')}"
    return items[zlib.crc32(payload.encode("utf-8")) % len(items)]


def resolve_outfit_cfg(scfg: Any) -> Dict[str, Any]:
    """``companion.selfie.consistency.outfit`` 配置（默认开，随 P0 一致性总闸）。

    ``outfit`` 可为 bool（开关）或 dict（``{enabled, pool}``）。父级
    ``consistency.enabled=false`` 一并关闭（与 P0 护栏同一杀开关）。
    """
    c = (scfg or {}).get("consistency") if isinstance(scfg, dict) else None
    c = c if isinstance(c, dict) else {}
    parent_on = bool(c.get("enabled", True))
    o = c.get("outfit", True)
    if isinstance(o, dict):
        enabled = bool(o.get("enabled", True))
        pool = o.get("pool")
    else:
        enabled = bool(o)
        pool = None
    return {"enabled": parent_on and enabled,
            "pool": pool if isinstance(pool, (list, tuple)) else None}


# ── 相册衣橱（带 IO，TTL 缓存）─────────────────────────────────────────────
_WARDROBE_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_WARDROBE_LOCK = threading.Lock()
_WARDROBE_TTL_SEC = 600.0
_ALBUM_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _fs_album_series(album_dir: str, persona_id: str) -> List[str]:
    """文件系统相册的系列 slug 收集（镜像 ``_list_album`` 的跨人设隔离语义：
    人设分册优先；多人设布局下根目录不兜底）。软失败返回空。"""
    out: List[str] = []
    try:
        root = Path(str(album_dir or ""))
        if not root.is_dir():
            return out
        key = "".join(c for c in str(persona_id or "")
                      if c.isalnum() or c in ("-", "_"))
        cand = root / key if key else root
        if key and not cand.is_dir():
            # 人设分册不存在：多人设布局（根下有别的分册）不许借根目录素材。
            has_sub = any(
                p.is_dir() and any(
                    f.is_file() and f.suffix.lower() in _ALBUM_IMG_EXT
                    for f in p.iterdir())
                for p in root.iterdir() if p.is_dir())
            if has_sub:
                return out
            cand = root
        if not cand.is_dir():
            return out
        names = [p.name for p in cand.iterdir()
                 if p.is_file() and p.suffix.lower() in _ALBUM_IMG_EXT]
        from src.ai.companion_selfie import album_series_of_path, load_album_meta
        metas = load_album_meta(str(cand))
        for n in names:
            m = metas.get(n) if isinstance(metas, dict) else None
            s = str((m or {}).get("series") or "") or album_series_of_path(n)
            if s:
                out.append(s)
    except Exception:
        return out
    return out


def wardrobe_for(persona_id: str, scfg: Any, *, now: Any = None) -> List[str]:
    """人设「相册衣橱」：注册相册(DB) + 文件系统相册的系列 slug 里认得出服装词的
    → 排序去重的衣着短语列表。TTL 缓存（600s）——聊天热路径零 IO。"""
    pid = str(persona_id or "").strip()
    album_dir = ""
    try:
        prov = (scfg or {}).get("provider") if isinstance(scfg, dict) else None
        album_dir = str((prov or {}).get("album_dir") or "").strip()
    except Exception:
        album_dir = ""
    ckey = f"{pid}|{album_dir}"
    ts = float(now if isinstance(now, (int, float)) else time.time())
    with _WARDROBE_LOCK:
        ent = _WARDROBE_CACHE.get(ckey)
        if ent and (ts - ent[0]) < _WARDROBE_TTL_SEC:
            return list(ent[1])
    slugs: List[str] = []
    if pid:
        try:
            from src.companion.persona_media_store import get_persona_media_store
            store = get_persona_media_store()
            if store is not None:
                from src.companion.persona_media import series_of
                for row in store.list(pid):
                    s = series_of(row)
                    if s:
                        slugs.append(s)
        except Exception:
            pass
    if album_dir:
        slugs.extend(_fs_album_series(album_dir, pid))
    phrases = sorted({p for p in (series_outfit_phrase(s) for s in slugs) if p})
    with _WARDROBE_LOCK:
        _WARDROBE_CACHE[ckey] = (ts, list(phrases))
        if len(_WARDROBE_CACHE) > 256:
            _WARDROBE_CACHE.pop(next(iter(_WARDROBE_CACHE)))
    return phrases


def invalidate_wardrobe_cache() -> None:
    """清衣橱缓存（测试/相册批量变更后用）。"""
    with _WARDROBE_LOCK:
        _WARDROBE_CACHE.clear()


def current_outfit(
    persona: Any,
    scfg: Any,
    *,
    persona_key: str = "",
    recent_series: str = "",
    now: Any = None,
) -> Dict[str, str]:
    """「此刻该穿什么」单一入口（A/B 生成链 + 聊天状态块共用）。

    优先级：连续窗内刚发过的衣着系列（照片事实，``recent_series`` 由调用方从
    P0 连续性信号取）→ 今日确定性衣着（衣橱池）。关闭/异常返回空衣着
    （调用方不注入 = 旧行为）。返回 ``{"outfit", "source"}``，source ∈
    ``continuity|daily|disabled|none``。
    """
    try:
        cfg = resolve_outfit_cfg(scfg)
        if not cfg["enabled"]:
            return {"outfit": "", "source": "disabled"}
        rec = series_outfit_phrase(recent_series)
        if rec:
            return {"outfit": rec, "source": "continuity"}
        pid = ""
        if isinstance(persona, dict):
            pid = str(persona.get("id") or "").strip()
        key = str(persona_key or "").strip() or pid
        pool = outfit_pool(
            persona=persona, cfg_pool=cfg["pool"],
            wardrobe=wardrobe_for(pid or key, scfg))
        outfit = resolve_today_outfit(key or pid, pool, now=now)
        return {"outfit": outfit, "source": ("daily" if outfit else "none")}
    except Exception:
        return {"outfit": "", "source": "none"}


def outfit_chat_line(outfit: str) -> str:
    """衣着事实行（进 ``scene_chat_note`` 状态块）：LLM 被问「穿了什么」按此答，
    与照片同源。空衣着返回 ""。"""
    o = str(outfit or "").strip()
    if not o:
        return ""
    return ("你今天穿着：" + o +
            "（英文描述，被问到穿什么时口语化转述成对话语言，与之保持一致，"
            "不要另编一套穿搭）。")
