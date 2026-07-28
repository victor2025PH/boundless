"""相册场景缺口「自动补货」（P2 图文一致性，2026-07-27）。

闭环定位（与语音侧 auto_stock→夜间渲染 同哲学）：P1 的缺口报告
（``media_gap.scene_gap_report``）已能指出「哪个场景被点名了多少次、哪些人设
零备货」——本模块把「照单补图」也自动化：

- **watchdog 侧**（``HealthWatchdog._check_media_restock``，每小时）：读进程内
  需求计数 + 相册供给 → ``qualify_restock_targets`` 选出达标缺口（unmet ≥ 阈值
  且该人设该场景零库存）→ 写进**补货计划文件**（JSON，带每日预算与去重）；
- **渲染侧**（CLI ``scripts/album_restock.py``，建议夜间低峰跑）：读计划文件逐项
  真出图——场景×衣着联动（P2-1）定穿搭、``generate_with_gate`` 全套体检
  **含场景后验**（P2-2，补进相册的图保证真是那个场景）、策展命名
  ``<scene>_<series>_<nn>.jpg`` 落 ``album_dir/<persona>/`` 并登记 ``_meta.json``
  （scene/tod/series）——下次客户再点名该场景，相册就有货了。

计划文件是两侧的解耦点：watchdog 在主进程（能读进程级需求计数），渲染在
独立进程（不占用主进程、可挂计划任务）。全链软失败；配置默认关
（``companion.selfie.consistency.auto_restock``，新子系统约定）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PLAN_PATH = "config/album_restock_plan.json"
_ALBUM_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def resolve_restock_cfg(scfg: Any) -> Dict[str, Any]:
    """``companion.selfie.consistency.auto_restock`` 配置（**默认关**）。

    - ``min_unmet``：场景未兑现次数达标线（进程级计数，重启清零——阈值即
      「最近一段真实流量里被拒了几次」）；
    - ``per_scene``：每 (人设,场景) 一次补几张；
    - ``max_per_day``：每日最多入队多少个补货项（防灌爆计划/夜间 GPU）；
    - ``plan_path``：计划文件路径。
    父级 ``consistency.enabled=false`` 一并关闭。
    """
    c = (scfg or {}).get("consistency") if isinstance(scfg, dict) else None
    c = c if isinstance(c, dict) else {}
    parent_on = bool(c.get("enabled", True))
    raw = c.get("auto_restock")
    rc = dict(raw) if isinstance(raw, dict) else {}
    return {
        "enabled": parent_on and bool(rc.get("enabled", False)),
        "min_unmet": max(1, int(rc.get("min_unmet", 2) or 2)),
        "per_scene": max(1, min(8, int(rc.get("per_scene", 2) or 2))),
        "max_per_day": max(0, int(rc.get("max_per_day", 6) or 6)),
        "plan_path": str(rc.get("plan_path") or DEFAULT_PLAN_PATH),
    }


def qualify_restock_targets(
    rows: Any, supply: Any, *, min_unmet: int = 2, max_targets: int = 6,
) -> List[Tuple[str, str]]:
    """从缺口报告行选补货目标（纯函数）：``[(persona_id, scene), ...]``。

    - 行准入：``unmet ≥ min_unmet``（真实流量里反复要不到才补，偶发不补）；
      且场景类**可渲染**（``other``/``unknown`` 这类归并桶没有对应的生图短语，
      补出来是无意义图——如实跳过，交人工判断那部分流量到底在要什么）；
    - 目标人设＝报告的 ``missing_personas``（有相册基建但该场景零备货；
      共享池有货的场景报告侧已不算缺）；
    - **单人设布局**（供给只有根目录 ``""`` 键、无人设分册）：该场景全局
      零库存 → 目标记 ``("", scene)``＝补进根相册；
    - 排序即报告排序（未兑现多的先补），截断 ``max_targets``。
    """
    out: List[Tuple[str, str]] = []
    supply = supply if isinstance(supply, dict) else {}
    has_persona_albums = any(k for k in supply.keys())
    for row in (rows or []):
        if len(out) >= max(0, int(max_targets)):
            break
        if not isinstance(row, dict):
            continue
        scene = str(row.get("scene") or "").strip()
        if not scene or scene not in RENDERABLE_SCENES:
            continue
        if int(row.get("unmet", 0) or 0) < int(min_unmet):
            continue
        missing = row.get("missing_personas")
        if isinstance(missing, list) and missing:
            for pid in missing:
                if len(out) >= max(0, int(max_targets)):
                    break
                out.append((str(pid), scene))
        elif not has_persona_albums and int(row.get("supply", 0) or 0) <= 0:
            out.append(("", scene))
    return out


# ── 计划文件（watchdog 写 / CLI 读，JSON）────────────────────────────────────

def load_plan(path: Any) -> Dict[str, Any]:
    """读补货计划；无/坏文件返回空计划（容错优先）。"""
    try:
        p = Path(str(path or "") or DEFAULT_PLAN_PATH)
        if not p.is_file():
            return {"items": []}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {"items": []}
        data["items"] = [it for it in data["items"] if isinstance(it, dict)]
        return data
    except Exception:
        return {"items": []}


def save_plan(path: Any, plan: Dict[str, Any]) -> bool:
    """写计划文件（UTF-8；软失败 False）。"""
    try:
        p = Path(str(path or "") or DEFAULT_PLAN_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        logger.debug("[media_restock] 计划文件写入失败", exc_info=True)
        return False


def pending_items(plan: Any) -> List[Dict[str, Any]]:
    items = (plan or {}).get("items") if isinstance(plan, dict) else None
    return [it for it in (items or [])
            if isinstance(it, dict) and str(it.get("status")) == "pending"]


def plan_add(
    plan: Dict[str, Any], persona_id: str, scene: str, count: int, *,
    now: Any = None,
) -> bool:
    """入队一个补货项（同 (persona,scene) 已有 pending → 去重不加）。"""
    scene = str(scene or "").strip()
    if not scene:
        return False
    pid = str(persona_id or "").strip()
    items = plan.setdefault("items", [])
    for it in items:
        if (isinstance(it, dict) and str(it.get("status")) == "pending"
                and str(it.get("persona_id") or "") == pid
                and str(it.get("scene") or "") == scene):
            return False
    ts = float(now if isinstance(now, (int, float)) else time.time())
    items.append({
        "persona_id": pid, "scene": scene,
        "count": max(1, int(count or 1)), "status": "pending",
        "requested_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
    })
    return True


def mark_item_done(item: Dict[str, Any], rendered: int, *, now: Any = None) -> None:
    """标记计划项完成（rendered=实际入册张数；0 张也标 done 防死循环重试——
    失败原因看日志，需要重试由运营改回 pending）。"""
    ts = float(now if isinstance(now, (int, float)) else time.time())
    item["status"] = "done"
    item["rendered"] = int(rendered)
    item["done_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ── 渲染入册（CLI 侧）────────────────────────────────────────────────────────

def restock_tod_for_scene(scene_class: str) -> str:
    """补货图的目标时段：夜景场景渲夜、其余渲白天（白天照适用窗口最宽；
    时段软过滤只剔「冲突」，白天照在傍晚也能发）。"""
    return "night" if str(scene_class or "").strip() == "night_city" else "day"


_SCENE_RENDER_PHRASES: Dict[str, str] = {
    "beach": "at a sunny beach, sea in the background",
    "cafe": "sitting in a cozy cafe",
    "office": "in a modern office",
    "gym": "at the gym, workout equipment in the background",
    "kitchen": "cooking in a bright kitchen",
    "park": "walking in a green park",
    "bedroom": "relaxing in a cozy bedroom",
    "library": "in a quiet library, bookshelves behind",
    "street": "on a city street",
    "campus": "on a university campus",
    "restaurant": "at a restaurant table",
    "night_city": "on a city street at night, neon lights",
    "home": "relaxing at home on the sofa",
    "car": "sitting in a car",
}

# 可补货的场景类＝有生图短语的（``other``/``unknown`` 归并桶不可渲染）。
RENDERABLE_SCENES = frozenset(_SCENE_RENDER_PHRASES)


def scene_render_phrase(scene_class: str, tod: str = "") -> str:
    """场景类 → 生图场景短语（带明确光照词——补货在凌晨跑，绝不能让
    ``ensure_time_of_day`` 语义把深夜光线掺进白天备货图）。"""
    sc = str(scene_class or "").strip()
    base = _SCENE_RENDER_PHRASES.get(sc) or (sc.replace("_", " ") or "casual scene")
    t = str(tod or "").strip() or restock_tod_for_scene(sc)
    if t == "night":
        return base if "night" in base else base + ", at night"
    return base + ", daytime, natural daylight"


def sanitize_album_key(key: str) -> str:
    """与 ``_list_album`` 同口径的人设分册目录名净化。"""
    return "".join(c for c in str(key or "") if c.isalnum() or c in ("-", "_"))


def next_curated_name(
    dir_path: Any, scene: str, series: str, *, ext: str = ".jpg",
) -> str:
    """下一个策展文件名 ``<scene>_<series>_<nn><ext>``（扫描现有同前缀取最大序号+1；
    目录不存在按 01 起）。scene/series 里的分隔符规整成 ``-``。"""
    sc = re.sub(r"[^a-z0-9-]+", "-", str(scene or "scene").strip().lower()).strip("-")
    sr = re.sub(r"[^a-z0-9-]+", "-", str(series or "auto").strip().lower()).strip("-")
    sc, sr = sc or "scene", sr or "auto"
    prefix = f"{sc}_{sr}_"
    best = 0
    try:
        d = Path(str(dir_path or ""))
        if d.is_dir():
            for f in d.iterdir():
                if not (f.is_file() and f.suffix.lower() in _ALBUM_IMG_EXT):
                    continue
                name = f.stem
                if name.startswith(prefix):
                    tail = name[len(prefix):]
                    if tail.isdigit():
                        best = max(best, int(tail))
    except Exception:
        best = 0
    e = str(ext or ".jpg").lower()
    if not e.startswith("."):
        e = "." + e
    return f"{prefix}{best + 1:02d}{e}"


def write_album_meta_entry(
    dir_path: Any, filename: str, *, scene: str, tod: str, series: str,
) -> bool:
    """把补货图登记进相册 ``_meta.json``（merge 保留现有条目；软失败 False）。"""
    try:
        from src.ai.companion_selfie import ALBUM_META_NAME
        p = Path(str(dir_path or "")) / ALBUM_META_NAME
        data: Dict[str, Any] = {}
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                data = raw if isinstance(raw, dict) else {}
            except Exception:
                data = {}
        entry: Dict[str, str] = {}
        if str(scene or "").strip():
            entry["scene"] = str(scene).strip().lower()
        if str(tod or "").strip():
            entry["tod"] = str(tod).strip().lower()
        if str(series or "").strip():
            entry["series"] = str(series).strip()
        data[str(filename)] = entry
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception:
        logger.debug("[media_restock] _meta.json 登记失败", exc_info=True)
        return False


async def render_restock_item(
    item: Dict[str, Any], scfg: Dict[str, Any], root_config: Dict[str, Any], *,
    provider: Any = None, count: Optional[int] = None, now: Any = None,
) -> Dict[str, Any]:
    """渲染一个补货项并入册。返回 ``{ok, rendered, files, reason}``。

    - 衣着走 P2-1 场景联动（``current_outfit(scene=...)``：健身房补运动装图、
      海边不补毛衣图），系列名即衣着 slug——补进的图天然参与服装连续窗；
    - 体检走 ``generate_with_gate`` 全套 + **场景后验**（P2-2，expect_scene=
      目标场景）：补进相册的图保证「真是那个场景」，不合格换种子重试，
      仍不合格如实少补；
    - 随机种子（stable_seed 会让同人设补货图全长一样，备货要的就是多样性）。
    """
    persona_id = str((item or {}).get("persona_id") or "").strip()
    scene = str((item or {}).get("scene") or "").strip()
    n = max(1, int(count if count is not None else (item or {}).get("count", 1) or 1))
    out: Dict[str, Any] = {"ok": False, "rendered": 0, "files": [], "reason": ""}
    if not scene:
        out["reason"] = "no_scene"
        return out
    prov = (scfg or {}).get("provider") if isinstance(scfg, dict) else None
    album_dir = str(((prov or {}).get("album_dir")) or "").strip()
    if not album_dir:
        out["reason"] = "no_album_dir"
        return out
    if provider is None:
        try:
            from src.ai.companion_selfie import get_selfie_provider
            provider = get_selfie_provider(prov or {})
        except Exception:
            out["reason"] = "provider_init_fail"
            return out
    backend = str(getattr(provider, "backend", "")).lower()
    if backend in ("", "disabled", "album"):
        out["reason"] = f"no_generation_backend({backend or 'none'})"
        return out
    try:
        from src.inbox.image_autosend import _resolve_persona
        persona = _resolve_persona(persona_id)
    except Exception:
        persona = persona_id
    tod = restock_tod_for_scene(scene)
    # 衣着（P2-1 场景联动；确定性——同日同场景同一套，多张图同系列成套）。
    outfit = ""
    try:
        from src.companion.outfit_state import current_outfit, outfit_slug
        outfit = current_outfit(
            persona, scfg, persona_key=persona_id, scene=scene,
            now=now)["outfit"]
        series = outfit_slug(outfit) or "auto"
    except Exception:
        outfit, series = "", "auto"
    try:
        from src.ai.companion_selfie import (build_selfie_prompt,
                                             resolve_persona_lora,
                                             resolve_variety_salt)
        _lora = resolve_persona_lora(persona, scfg)
        _vsalt = resolve_variety_salt(scfg)
        prompt = build_selfie_prompt(
            persona,
            scene_hint=scene_render_phrase(scene, tod),
            style=str((scfg or {}).get("style") or ""),
            default_appearance=str((scfg or {}).get("appearance") or ""),
            content_rating=str((scfg or {}).get("content_rating") or ""),
            variety_salt=_vsalt,
            lora_trigger=_lora["trigger"],
            outfit=outfit,
        )
    except Exception:
        out["reason"] = "prompt_build_fail"
        return out
    # 目标分册目录（与 _list_album 同净化口径）；锁脸基础图沿用生产逻辑。
    key = sanitize_album_key(persona_id)
    target_dir = Path(album_dir) / key if key else Path(album_dir)
    base = ""
    try:
        if backend in ("openai", "command"):
            base = provider.reference_image(persona_id)
    except Exception:
        base = ""
    from src.ai.image_gate import generate_with_gate, resolve_gate_cfg
    import shutil
    gcfg = resolve_gate_cfg(scfg if isinstance(scfg, dict) else {})
    # 时段后验的期望取**目标 tod 的代表小时**（白天备货给 12、夜景给 23）——
    # 不是「现在几点」：补货常在凌晨跑，白天照被模型画成夜景时闸门要抓得住。
    _hour = 23 if tod == "night" else 12
    rendered = 0
    last_err = ""
    for _i in range(n):
        try:
            res = await generate_with_gate(
                provider, prompt, persona=persona,
                root_config=root_config if isinstance(root_config, dict) else {},
                gate_cfg=gcfg, seed=-1,
                expect_scene=scene,           # P2-2：补货图自带场景后验
                expect_hour=_hour,            # 时段后验对齐目标 tod（非当前时刻）
                album_key=persona_id, base_image=base,
                lora=_lora["file"], lora_weight=_lora["weight"],
                allow_album_fallback=False)   # 补相册不许从相册兜底（自我复制）
        except TypeError:
            # 后端不认 allow_album_fallback 等扩展参：降参重试一次。
            try:
                res = await generate_with_gate(
                    provider, prompt, persona=persona,
                    root_config=root_config if isinstance(root_config, dict) else {},
                    gate_cfg=gcfg, seed=-1,
                    expect_scene=scene, expect_hour=_hour)
            except Exception as ex:  # noqa: BLE001
                last_err = f"{type(ex).__name__}"
                continue
        except Exception as ex:  # noqa: BLE001
            last_err = f"{type(ex).__name__}"
            continue
        if not getattr(res, "ok", False) or not getattr(res, "image_path", ""):
            last_err = str(getattr(res, "error", "") or "generate_fail")
            continue
        src_p = Path(str(res.image_path))
        if str(getattr(res, "provider", "")).lower() == "album":
            last_err = "album_result_skipped"   # 兜底图不能当新备货
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            fname = next_curated_name(
                target_dir, scene, series, ext=src_p.suffix or ".jpg")
            shutil.copy2(str(src_p), str(target_dir / fname))
            write_album_meta_entry(
                target_dir, fname, scene=scene, tod=tod, series=series)
            out["files"].append(str(target_dir / fname))
            rendered += 1
        except Exception:
            logger.debug("[media_restock] 入册失败", exc_info=True)
            last_err = "copy_fail"
    out["rendered"] = rendered
    out["ok"] = rendered > 0
    out["reason"] = "" if rendered > 0 else (last_err or "no_render")
    if rendered:
        # 供给缓存失效：下一轮缺口报告立刻看到新库存。
        try:
            from src.companion.media_gap import invalidate_supply_cache
            invalidate_supply_cache()
        except Exception:
            pass
        try:
            from src.companion.outfit_state import invalidate_wardrobe_cache
            invalidate_wardrobe_cache()
        except Exception:
            pass
    return out


__all__ = [
    "DEFAULT_PLAN_PATH",
    "RENDERABLE_SCENES",
    "resolve_restock_cfg",
    "qualify_restock_targets",
    "load_plan",
    "save_plan",
    "pending_items",
    "plan_add",
    "mark_item_done",
    "restock_tod_for_scene",
    "scene_render_phrase",
    "sanitize_album_key",
    "next_curated_name",
    "write_album_meta_entry",
    "render_restock_item",
]
