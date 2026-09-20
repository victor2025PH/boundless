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
    "kimono", "yukata", "swimsuit", "bikini", "swimwear", "tank",
    "sweatpants", "joggers", "tracksuit", "sportswear",
))

# ── 场景×衣着联动（P2）────────────────────────────────────────────────────
# 衣着类别（token → 类别；一件衣着可命中多类："denim jacket over a white tee"
# ＝outer+casual）。类别只服务场景/季节适配的排除与优先，不追求服装学完备。
_OUTFIT_CATEGORY_TOKENS: Dict[str, frozenset] = {
    "swim": frozenset(("swimsuit", "bikini", "swimwear")),
    "sleep": frozenset(("pajama", "pajamas", "nightgown", "robe")),
    "sport": frozenset(("leggings", "joggers", "sweatpants", "tracksuit",
                        "sportswear")),
    "work": frozenset(("suit", "blazer", "uniform")),
    "dressy": frozenset(("dress", "gown", "skirt", "qipao", "cheongsam",
                         "hanfu", "kimono", "yukata", "jumpsuit", "romper")),
    "outer": frozenset(("coat", "parka", "jacket", "windbreaker", "cardigan",
                        "sweater", "hoodie", "sweatshirt", "turtleneck",
                        "knit")),
    "light": frozenset(("tank", "shorts", "camisole")),   # 轻薄单穿
}

# 场景类 → 衣着约束：exclude=该场景绝不注入的类别（命中→换装，无可换→放弃注入
# 交生图模型按场景自由穿）；prefer=优先从衣橱挑的类别（有货才换，没货不硬凑）。
# 词表外场景类无约束。刻意保守：只写高置信违和（泳装进咖啡馆/西装进健身房）。
_SCENE_OUTFIT_RULES: Dict[str, Dict[str, frozenset]] = {
    "gym": {"exclude": frozenset(("sleep", "work", "dressy", "swim")),
            "prefer": frozenset(("sport",))},
    "beach": {"exclude": frozenset(("work", "sleep", "outer")),
              "prefer": frozenset(("dressy", "light"))},
    "office": {"exclude": frozenset(("sleep", "swim", "sport")),
               "prefer": frozenset(("work",))},
    "bedroom": {"exclude": frozenset(("swim",)), "prefer": frozenset()},
    "home": {"exclude": frozenset(("swim",)), "prefer": frozenset()},
    "kitchen": {"exclude": frozenset(("swim",)), "prefer": frozenset()},
    "cafe": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "restaurant": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "library": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "campus": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "street": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "park": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "night_city": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
    "car": {"exclude": frozenset(("swim", "sleep")), "prefer": frozenset()},
}

# 季节排除（P2-4，token 级）：冬天不发泳装、夏天不裹大衣；冬季**户外**场景再排
# 轻薄单穿（室内暖气吊带合理，不动）。月份粗分（默认北半球，config 可换 south）；
# 春秋不约束。jacket/cardigan 夏夜也合理 → 夏季只排重外套 token。
_SEASON_EXCLUDE_TOKENS: Dict[str, frozenset] = {
    "summer": frozenset(("parka", "coat", "turtleneck")),
    "winter": frozenset(("swimsuit", "bikini", "swimwear")),
}
_WINTER_OUTDOOR_TOKENS = frozenset(("tank", "shorts", "camisole"))
_OUTDOOR_SCENES = frozenset(("beach", "park", "street", "campus", "night_city"))

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


def _outfit_tokens(phrase: Any) -> frozenset:
    """衣着短语的小写 token 集（纯函数）。"""
    s = str(phrase or "").strip().lower()
    if not s:
        return frozenset()
    return frozenset(t for t in re.split(r"[-_\s]+", s) if t)


def outfit_categories(phrase: Any) -> frozenset:
    """衣着短语 → 命中的类别集合（可多类；认不出返回空集=不受约束）。"""
    toks = _outfit_tokens(phrase)
    if not toks:
        return frozenset()
    return frozenset(
        cat for cat, words in _OUTFIT_CATEGORY_TOKENS.items() if toks & words)


def season_of(now: Any = None, hemisphere: str = "north") -> str:
    """月份 → 季节（纯函数，粗分即可）：summer|winter|spring|autumn。"""
    import datetime as _dt

    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    m = t.month
    if str(hemisphere or "").strip().lower() == "south":
        m = ((m + 5) % 12) + 1   # 南半球倒扣半年
    if m in (6, 7, 8):
        return "summer"
    if m in (12, 1, 2):
        return "winter"
    return "spring" if m in (3, 4, 5) else "autumn"


def outfit_scene_conflict(
    outfit: Any, scene_class: str, *,
    now: Any = None, season_on: bool = True, hemisphere: str = "north",
) -> bool:
    """衣着与 场景类/季节 是否**高置信违和**（纯函数；存疑不判）。"""
    toks = _outfit_tokens(outfit)
    if not toks:
        return False
    cats = outfit_categories(outfit)
    sc = str(scene_class or "").strip()
    rules = _SCENE_OUTFIT_RULES.get(sc)
    if rules and (cats & rules["exclude"]):
        return True
    if season_on:
        season = season_of(now, hemisphere)
        if toks & _SEASON_EXCLUDE_TOKENS.get(season, frozenset()):
            return True
        if season == "winter" and sc in _OUTDOOR_SCENES \
                and (toks & _WINTER_OUTDOOR_TOKENS):
            return True
    return False


def adapt_outfit_to_scene(
    outfit: str, pool: Any, scene_class: str, key: str, *,
    now: Any = None, season_on: bool = True, hemisphere: str = "north",
) -> Tuple[str, str]:
    """把当前衣着适配到场景（纯函数）。返回 ``(衣着, tag)``，
    tag ∈ ``""``（未动）/``switched``（换装）/``vetoed``（放弃注入）。

    语义（P2 场景×衣着联动）：
    - 场景无约束 / 当前衣着不违和且已属场景优先类（或场景无优先类）→ 原样保持
      （「一天一套」仍是主旋律）；
    - 场景有优先类（gym→运动装 / beach→裙装轻装 / office→通勤）且衣橱有货 →
      确定性换装（换装本身是真实行为：去健身当然换运动服）；
    - 当前衣着违和（泳装进咖啡馆/冬天短袖户外）→ 从池里挑不违和的换；
      **全池都违和 → 返回空**（放弃注入，生图模型按场景自由穿——比硬注入
      冲突衣着诚实）。同 key 同日同场景确定性稳定。
    """
    o = str(outfit or "").strip()
    if not o:
        return "", ""
    sc = str(scene_class or "").strip()
    rules = _SCENE_OUTFIT_RULES.get(sc)

    def _bad(phrase: str) -> bool:
        return outfit_scene_conflict(
            phrase, sc, now=now, season_on=season_on, hemisphere=hemisphere)

    def _pick(cands: List[str]) -> str:
        import datetime as _dt
        t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
        payload = f"{str(key or '')}#outfit-scene#{t.strftime('%Y-%m-%d')}#{sc}"
        return cands[zlib.crc32(payload.encode("utf-8")) % len(cands)]

    items = [str(x).strip() for x in (pool or []) if str(x).strip()]
    prefer = rules["prefer"] if rules else frozenset()
    cur_ok = not _bad(o)
    if cur_ok and (not prefer or (outfit_categories(o) & prefer)):
        return o, ""
    if prefer:
        cands = [p for p in items
                 if (outfit_categories(p) & prefer) and not _bad(p)]
        if cands:
            picked = _pick(cands)
            return (picked, "") if picked == o else (picked, "switched")
    if cur_ok:
        return o, ""    # 无优先货可换，但当前不违和 → 保持
    cands = [p for p in items if not _bad(p)]
    if cands:
        return _pick(cands), "switched"
    return "", "vetoed"


def resolve_outfit_cfg(scfg: Any) -> Dict[str, Any]:
    """``companion.selfie.consistency.outfit`` 配置（默认开，随 P0 一致性总闸）。

    ``outfit`` 可为 bool（开关）或 dict（``{enabled, pool, season, hemisphere}``）。
    父级 ``consistency.enabled=false`` 一并关闭（与 P0 护栏同一杀开关）；
    ``season``（默认开）＝季节排除；``hemisphere``（north|south）季节倒扣。
    """
    c = (scfg or {}).get("consistency") if isinstance(scfg, dict) else None
    c = c if isinstance(c, dict) else {}
    parent_on = bool(c.get("enabled", True))
    o = c.get("outfit", True)
    if isinstance(o, dict):
        enabled = bool(o.get("enabled", True))
        pool = o.get("pool")
        season_on = bool(o.get("season", True))
        hemisphere = str(o.get("hemisphere") or "north")
    else:
        enabled = bool(o)
        pool = None
        season_on = True
        hemisphere = "north"
    return {"enabled": parent_on and enabled,
            "pool": pool if isinstance(pool, (list, tuple)) else None,
            "season": season_on, "hemisphere": hemisphere}


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
    scene: str = "",
    now: Any = None,
) -> Dict[str, str]:
    """「此刻该穿什么」单一入口（A/B 生成链 + 聊天状态块共用）。

    优先级：连续窗内刚发过的衣着系列（照片事实，``recent_series`` 由调用方从
    P0 连续性信号取）→ 今日确定性衣着（衣橱池）；随后按 ``scene``（本次照片/
    聊天状态的场景短语）做**场景×季节适配**（P2）：违和→换装/放弃注入，
    场景优先类（gym→运动装等）衣橱有货→换装。关闭/异常返回空衣着
    （调用方不注入 = 旧行为）。返回 ``{"outfit", "source"}``，source ∈
    ``continuity|daily|scene|disabled|none``（scene=被场景适配改过）。
    """
    try:
        cfg = resolve_outfit_cfg(scfg)
        if not cfg["enabled"]:
            return {"outfit": "", "source": "disabled"}
        pid = ""
        if isinstance(persona, dict):
            pid = str(persona.get("id") or "").strip()
        key = str(persona_key or "").strip() or pid
        rec = series_outfit_phrase(recent_series)
        pool: Optional[List[str]] = None

        def _pool() -> List[str]:
            nonlocal pool
            if pool is None:
                pool = outfit_pool(
                    persona=persona, cfg_pool=cfg["pool"],
                    wardrobe=wardrobe_for(pid or key, scfg))
            return pool

        if rec:
            outfit, source = rec, "continuity"
        else:
            outfit = resolve_today_outfit(key or pid, _pool(), now=now)
            source = "daily" if outfit else "none"
        # 场景×季节适配（P2）：换装是真实行为（去健身当然换运动服）；
        # 违和且无可换 → 放弃注入（比硬注入冲突衣着诚实）。
        if outfit:
            sc = ""
            try:
                from src.companion.persona_media import scene_class_of
                sc = scene_class_of(scene)
            except Exception:
                sc = ""
            # 半球优先取人设居住地（南半球悉尼/墨尔本/巴厘等季节倒扣），
            # 配置显式 hemisphere 仍作缺省；place 解析失败回落 cfg。
            _hemi = str(cfg.get("hemisphere") or "north")
            try:
                from src.companion.persona_location import resolve_place_with_fallback
                _pl = resolve_place_with_fallback(persona)
                if _pl is not None:
                    _hemi = _pl.hemisphere
            except Exception:
                pass
            adapted, tag = adapt_outfit_to_scene(
                outfit, _pool(), sc, key or pid, now=now,
                season_on=bool(cfg.get("season", True)),
                hemisphere=_hemi)
            if tag:
                outfit, source = adapted, "scene"
        return {"outfit": outfit, "source": source}
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
