"""相册媒体「时段(tod)/场景/系列」元数据回填——P0 图文一致性的数据补齐层。

一致性护栏（``persona_media.select_media`` 的时段过滤 / ``SelfieProvider.
_pick_from_album`` 的 ``_meta.json`` sidecar）依赖每张图的 {scene, series, tod}：

- scene/series：策展 ``manifest.json``（scene/outfit 字段）与文件名约定
  （``<scene>_<series>_<nn>.jpg``）已有现成数据；
- **tod（day|night）此前没有任何数据源**——不回填则时段过滤永远「不限」，
  「深夜发大白天照」只能靠运气避开。

本模块是**纯函数核心**（VLM 分类结果由调用方注入；文件/DB IO 由 CLI
``scripts/persona_media_backfill_meta.py`` 驱动）。关键语义：

- **保守分类**（``parse_tod_response``）：只认画面里有明确昼/夜证据（天空/
  阳光/夜景灯光）；室内灯光下的自拍昼夜天然歧义 → ``unknown``＝不打标＝
  不过滤——宁可放过不误杀（与 select_media 的软过滤语义配套）。
- **人工值优先**（``merge_file_meta``）：已有 ``_meta.json`` 里的键（可能是
  运营手改）绝不覆盖；manifest/文件名只补缺失键。
- **scene 归一**：sidecar 里存 canonical 场景类（``scene_class_of`` 产物，如
  ``park``/``home``）——运行时 ``_pick_from_album`` 按「场景类相等」硬匹配，
  存原始 slug（``outdoor-park``）会永远匹配不上。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

TOD_DAY = "day"
TOD_NIGHT = "night"

# 图片扩展名（与 companion_selfie._ALBUM_IMAGE_EXT 同口径；视频不参与 tod 回填
# ——运行时时段过滤只看照片，视频场景由触发词/运营配文兜底）。
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# 相册目录里不参与投放/回填的特殊文件。
SKIP_STEMS = ("face_ref",)
SKIP_NAMES = ("manifest.json", "_meta.json", "_ref.json")


def build_tod_prompt() -> str:
    """VLM 昼夜分类 prompt（纯函数）：严格 JSON、允许 unknown、证据导向。"""
    return (
        "You are labeling a photo for a photo library. Judge ONLY the "
        "time-of-day from lighting evidence visible in the image:\n"
        '- "day": clear daylight (bright sky, sunshine, daylight through '
        "windows)\n"
        '- "night": clearly nighttime (dark sky, night city lights, dark '
        "outdoor scene, or indoor shot where windows clearly show darkness "
        "outside)\n"
        '- "unknown": indoor shot with no window/sky visible, ambiguous '
        "lighting (dusk/dawn), or you are not sure\n"
        'Answer with strict JSON only: {"tod": "day"} or {"tod": "night"} '
        'or {"tod": "unknown"}'
    )


_TOD_JSON_RE = re.compile(r'"tod"\s*:\s*"(day|night|unknown)"', re.IGNORECASE)


def parse_tod_response(raw: Any) -> str:
    """解析 VLM 昼夜分类回答 → ``day``/``night``/``""``（unknown/解析失败）。

    保守语义：任何不确定（unknown/坏 JSON/超长闲聊）一律返回空串＝不打标。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            v = str(data.get("tod") or "").strip().lower()
            return v if v in (TOD_DAY, TOD_NIGHT) else ""
    except Exception:
        pass
    m = _TOD_JSON_RE.search(s)
    if m:
        v = m.group(1).lower()
        return v if v in (TOD_DAY, TOD_NIGHT) else ""
    # 裸词兜底（有的 VLM 不吐 JSON 只答一个词）；夹杂长解释不认（歧义大）。
    low = s.lower()
    if len(low) <= 24:
        if "night" in low and "day" not in low:
            return TOD_NIGHT
        if "day" in low and "night" not in low:
            return TOD_DAY
    return ""


def canonical_scene(slug: Any) -> str:
    """场景 slug/短语 → canonical 场景类（认不出返回 ""＝未知，不落 sidecar）。"""
    try:
        from src.companion.persona_media import scene_class_of
        return scene_class_of(slug)
    except Exception:
        return ""


def is_album_photo(name: str) -> bool:
    """是否参与回填的相册照片（排除视频/manifest/sidecar/face_ref 锁脸基准照）。"""
    n = str(name or "").strip()
    if not n or n in SKIP_NAMES:
        return False
    low = n.lower()
    if not low.endswith(IMAGE_EXTS):
        return False
    stem = low.rsplit(".", 1)[0]
    return stem not in SKIP_STEMS


def manifest_entries(manifest: Any) -> Dict[str, Dict[str, str]]:
    """策展 ``manifest.json`` → ``{文件名: {"scene", "series"}}``（scene 已归一）。

    manifest 结构（scripts 策展产物）：``{"photos": [{"file", "scene",
    "outfit", ...}]}``；坏结构/缺字段一律跳过（回填是补齐层，绝不因脏数据崩）。
    """
    out: Dict[str, Dict[str, str]] = {}
    photos = (manifest or {}).get("photos") if isinstance(manifest, dict) else None
    if not isinstance(photos, list):
        return out
    for item in photos:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file") or "").strip()
        if not name:
            continue
        out[name] = {
            "scene": canonical_scene(item.get("scene")),
            "series": str(item.get("outfit") or "").strip(),
        }
    return out


def merge_file_meta(
    name: str,
    manifest_entry: Optional[Dict[str, str]] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """单文件 sidecar 条目合并（纯函数）：已有 sidecar 值 > manifest > 文件名约定。

    只输出**非空**键（scene/series/tod）；全空返回 {}（该文件不落 sidecar，
    运行时按文件名约定兜底，向后兼容零膨胀）。
    """
    ex = existing if isinstance(existing, dict) else {}
    mf = manifest_entry if isinstance(manifest_entry, dict) else {}
    out: Dict[str, str] = {}
    tod = str(ex.get("tod") or "").strip().lower()
    if tod in (TOD_DAY, TOD_NIGHT):
        out["tod"] = tod
    scene = (str(ex.get("scene") or "").strip().lower()
             or str(mf.get("scene") or "").strip().lower())
    if not scene:
        try:
            from src.ai.companion_selfie import album_scene_class_of_path
            scene = album_scene_class_of_path(name)
        except Exception:
            scene = ""
    if scene:
        out["scene"] = scene
    series = (str(ex.get("series") or "").strip()
              or str(mf.get("series") or "").strip())
    if not series:
        try:
            from src.ai.companion_selfie import album_series_of_path
            series = album_series_of_path(name)
        except Exception:
            series = ""
    if series:
        out["series"] = series
    return out


def plan_album_backfill(
    files: List[str],
    manifest: Any = None,
    existing_meta: Optional[Dict[str, Any]] = None,
    *,
    force_tod: bool = False,
) -> Dict[str, Any]:
    """一个相册目录的回填计划（纯函数）。

    返回 ``{"meta": {文件名: 条目}, "need_tod": [文件名]}``：
    - ``meta``＝合并后的完整 sidecar 内容（写盘由 CLI 决定）；
    - ``need_tod``＝还缺 tod 标注、需要 VLM 分类的照片（``force_tod=True``
      时全部照片重分类——换相册图后用）。
    调用方拿到 VLM 结果后经 ``apply_tod_results`` 并回 meta。
    """
    ex_all = existing_meta if isinstance(existing_meta, dict) else {}
    mf = manifest_entries(manifest)
    meta: Dict[str, Any] = {}
    need: List[str] = []
    for f in files:
        name = str(f or "").strip()
        if not is_album_photo(name):
            continue
        entry = merge_file_meta(name, mf.get(name), ex_all.get(name))
        if entry:
            meta[name] = entry
        if force_tod or "tod" not in entry:
            need.append(name)
    return {"meta": meta, "need_tod": need}


def apply_tod_results(
    meta: Dict[str, Any], results: Dict[str, str],
) -> Tuple[Dict[str, Any], int]:
    """把 VLM 昼夜结果并进 sidecar 内容（纯函数）；返回 (新 meta, 实际打标数)。

    只接受 ``day``/``night``（unknown/空串不写键——运行时空=不限，语义一致）。
    """
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in (meta or {}).items()}
    n = 0
    for name, tod in (results or {}).items():
        t = str(tod or "").strip().lower()
        if t not in (TOD_DAY, TOD_NIGHT):
            continue
        entry = out.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry["tod"] = t
        out[name] = entry
        n += 1
    return out, n


# ── 注册相册（DB）侧：tod:/scene: 标签补齐 ─────────────────────────────────


def row_tag_value(row: Optional[Dict[str, Any]], prefix: str) -> str:
    for t in (row or {}).get("tags") or []:
        ts = str(t or "")
        if ts.startswith(prefix):
            return ts[len(prefix):].strip()
    return ""


def db_rows_needing_tod(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """挑出还缺 ``tod:`` 标签的照片行（视频不参与；已有标注跳过=幂等可重跑）。"""
    out = []
    for r in rows or []:
        if str((r or {}).get("media_type") or "photo") != "photo":
            continue
        if row_tag_value(r, "tod:"):
            continue
        out.append(r)
    return out


def tags_with_tod(tags: Any, tod: str) -> Optional[List[str]]:
    """在标签列表上追加 ``tod:<v>``（幂等纯函数）；无需变更返回 None。"""
    t = str(tod or "").strip().lower()
    if t not in (TOD_DAY, TOD_NIGHT):
        return None
    cur = [str(x) for x in (tags or []) if str(x or "").strip()]
    if any(x.startswith("tod:") for x in cur):
        return None
    return cur + [f"tod:{t}"]


__all__ = [
    "TOD_DAY", "TOD_NIGHT", "IMAGE_EXTS",
    "build_tod_prompt", "parse_tod_response", "canonical_scene",
    "is_album_photo", "manifest_entries", "merge_file_meta",
    "plan_album_backfill", "apply_tod_results",
    "row_tag_value", "db_rows_needing_tod", "tags_with_tod",
]
