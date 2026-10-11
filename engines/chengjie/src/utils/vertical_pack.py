"""垂直行业模板包加载器（智语 2026-10-08）。

公开包在 ``config/presets/packs/<id>/``，内部包在 ``config/presets/internal/<id>/``。
此前只有测试和 ``src/eval/persona_quality_eval.load_pack`` 读这些 YAML，运行时
``--init`` / ``/api/kb/seed-pack`` 走的是另一套起步包（``kb_starter``），未知域
仍回落 general，本模块不改变那条回落。

可见性（``src.utils.product_edition``，与 WhatsApp 回退模板同一判断）：

- ``include_internal=True`` 强制列出内部包（CLI ``--internal``）；
- ``include_internal=False`` 强制隐藏；
- ``None`` 读环境变量：只有 ``CHATX_FLAVOR=internal``（flavor 为空时才看
  ``CHATX_EDITION=internal``）可见。未设置、``public`` / ``clean`` / ``client`` /
  ``dev`` / ``full`` 都隐藏。公开 flavor 优先于 edition=internal。

HTTP 不接受请求体里的 ``allow_internal``。公开版环境变量不能被请求体绕过。

激活方式是显式的：CLI 默认 dry-run；``--apply`` 或 ``POST /api/kb/seed-pack``
带 ``vertical_pack`` 才写 KB。带未替换 ``{placeholder}`` 的条目拒绝写入。
不写 ``profiles_runtime.yaml``（那是热加载的生产人设库）。

可选配置（空 id = 没有激活包，本模块不读取它来自动播种）::

    vertical_pack:
      id: ""
      allow_internal: false
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

_ENGINE = Path(__file__).resolve().parents[2]
_PRESETS = _ENGINE / "config" / "presets"
_PUBLIC_ROOT = _PRESETS / "packs"
_INTERNAL_ROOT = _PRESETS / "internal"

_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_ENTRY_TEXT_KEYS = (
    "category", "title", "scenario", "steps", "principles", "example_reply_zh",
)


class VerticalPackError(Exception):
    code = "vertical_pack_error"


class UnknownPack(VerticalPackError):
    code = "unknown_pack"


class InternalPackHidden(VerticalPackError):
    code = "internal_pack_hidden"


def engine_root() -> Path:
    return _ENGINE


def edition_shows_internal(include_internal: Optional[bool] = None) -> bool:
    """内部包是否可见。显式布尔优先于环境变量；未设置环境变量时隐藏（公开包）。"""
    from src.utils.product_edition import is_internal_edition
    return is_internal_edition(include_internal)


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _iter_pack_dirs(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "pack.yaml").is_file():
            yield child


def _pack_meta(directory: Path, *, internal: bool) -> Dict[str, Any]:
    meta = _load_yaml(directory / "pack.yaml")
    pack_id = str(meta.get("id") or directory.name)
    return {
        "id": pack_id,
        "name": str(meta.get("name") or pack_id),
        "edition": str(meta.get("edition") or ("internal_only" if internal else "public")),
        "preset": meta.get("preset"),
        "langs": list(meta.get("langs") or []),
        "internal": internal or str(meta.get("edition") or "") == "internal_only",
        "dir": directory,
        "meta": meta,
    }


def _visible_metas(include_internal: Optional[bool] = None) -> List[Dict[str, Any]]:
    rows = [_pack_meta(d, internal=False) for d in _iter_pack_dirs(_PUBLIC_ROOT)]
    if edition_shows_internal(include_internal):
        rows.extend(_pack_meta(d, internal=True) for d in _iter_pack_dirs(_INTERNAL_ROOT))
    return rows


def list_packs(include_internal: Optional[bool] = None) -> List[Dict[str, Any]]:
    """公开包（及当前可见的内部包）概览，供冷启动向导。不含正文。"""
    out = []
    for row in _visible_metas(include_internal):
        loaded = _read_pack_files(row)
        out.append({
            "id": row["id"],
            "name": row["name"],
            "edition": row["edition"],
            "preset": row["preset"],
            "langs": row["langs"],
            "internal": bool(row["internal"]),
            "placeholders": sorted(collect_placeholders(loaded)),
        })
    return out


def _read_pack_files(row: Dict[str, Any]) -> Dict[str, Any]:
    directory: Path = row["dir"]
    files = (row.get("meta") or {}).get("files") or {}
    persona_doc = _load_yaml(directory / str(files.get("persona") or "persona.yaml"))
    kb_doc = _load_yaml(directory / str(files.get("kb_seed") or "kb_seed.yaml"))
    stop_doc = _load_yaml(directory / str(files.get("stop_confirm") or "stop_confirm.yaml"))
    sop_doc = _load_yaml(directory / str(files.get("handoff_sop") or "handoff_sop.yaml"))
    compliance_name = files.get("compliance")
    compliance = _load_yaml(directory / str(compliance_name)) if compliance_name else {}
    if not compliance and (directory / "compliance.yaml").is_file():
        compliance = _load_yaml(directory / "compliance.yaml")
    persona = persona_doc.get("persona") if isinstance(persona_doc.get("persona"), dict) else persona_doc
    return {
        "id": row["id"],
        "name": row["name"],
        "edition": row["edition"],
        "preset": row["preset"],
        "langs": row["langs"],
        "internal": bool(row["internal"]),
        "dir": str(directory),
        "meta": row["meta"],
        "persona": persona if isinstance(persona, dict) else {},
        "kb": kb_doc,
        "stop": stop_doc,
        "sop": sop_doc,
        "compliance": compliance,
    }


def load_pack(pack_id: str, include_internal: Optional[bool] = None) -> Dict[str, Any]:
    """按 id 加载。未知 id → UnknownPack；内部包当前不可见 → InternalPackHidden。"""
    want = str(pack_id or "").strip()
    if not want:
        raise UnknownPack(want)
    hidden = None
    for internal, root in ((False, _PUBLIC_ROOT), (True, _INTERNAL_ROOT)):
        for directory in _iter_pack_dirs(root):
            row = _pack_meta(directory, internal=internal)
            if row["id"] != want and directory.name != want:
                continue
            if row["internal"] and not edition_shows_internal(include_internal):
                hidden = want
                continue
            return _read_pack_files(row)
    if hidden:
        raise InternalPackHidden(hidden)
    raise UnknownPack(want)


def _walk_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_strings(value)


def collect_placeholders(pack: Dict[str, Any]) -> Set[str]:
    """人设 / KB / STOP / 待人工话术里的 ``{name}``。隔离清单里的占位字面量不算待填项。"""
    found: Set[str] = set()
    for key in ("persona", "kb", "stop", "sop"):
        for text in _walk_strings(pack.get(key)):
            found.update(_PH_RE.findall(text))
    return found


def _lookup(values: Optional[Dict[str, Any]], key: str) -> Optional[str]:
    if not values or key not in values:
        return None
    raw = values.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def fill_text(text: str, values: Optional[Dict[str, Any]] = None) -> str:
    def repl(match: re.Match) -> str:
        got = _lookup(values, match.group(1))
        return got if got is not None else match.group(0)
    return _PH_RE.sub(repl, str(text))


def fill_obj(obj: Any, values: Optional[Dict[str, Any]] = None) -> Any:
    if isinstance(obj, str):
        return fill_text(obj, values)
    if isinstance(obj, list):
        return [fill_obj(v, values) for v in obj]
    if isinstance(obj, dict):
        return {k: fill_obj(v, values) for k, v in obj.items()}
    return obj


def leftover_placeholders(obj: Any) -> Set[str]:
    found: Set[str] = set()
    for text in _walk_strings(obj):
        found.update(_PH_RE.findall(text))
    return found


def persona_profile(pack: Dict[str, Any], values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给人设档案编辑器用的字典。调用方自己决定写到哪，本函数不落盘。"""
    raw = pack.get("persona") or {}
    filled = fill_obj(raw, values)
    if not isinstance(filled, dict):
        filled = {}
    kind = str(filled.get("kind") or "")
    profile = {
        "id": filled.get("id") or "",
        "name": filled.get("name") or "",
        "role": filled.get("role") or "",
        "background": filled.get("background") or "",
        "personality": filled.get("personality") or {},
        "speaking": filled.get("speaking") or {},
        "identity": filled.get("identity") or {},
        "boundaries": filled.get("boundaries") or {},
        "kind": kind,
        "meta": {"vertical_pack": pack.get("id") or "", "kind": kind},
    }
    return profile


def assert_persona_out_allowed(path: Path) -> None:
    """拒绝写热加载的生产人设库。"""
    if path.name.lower() == "profiles_runtime.yaml":
        raise VerticalPackError("refusing to write profiles_runtime.yaml")


def stop_confirm_line(
    pack: Dict[str, Any], lang: str, *, voice: str = "persona",
    values: Optional[Dict[str, Any]] = None,
) -> str:
    templates = (pack.get("stop") or {}).get("templates") or {}
    node = templates.get(lang) or templates.get("en") or {}
    if not isinstance(node, dict):
        return ""
    text = node.get(voice) or node.get("persona") or ""
    return fill_text(str(text), values)


def holding_line(
    pack: Dict[str, Any], lang: str, *, kind: str = "holding_reply",
    values: Optional[Dict[str, Any]] = None,
) -> str:
    table = (pack.get("sop") or {}).get(kind) or {}
    if not isinstance(table, dict):
        return ""
    text = table.get(lang) or table.get("en") or ""
    return fill_text(str(text), values)


def _existing_titles(kb_store) -> Set[str]:
    try:
        return {str(e.get("title") or "").strip() for e in kb_store.list_entries()}
    except Exception:
        return set()


def _entry_payload(entry: Dict[str, Any], values: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Set[str]]:
    triggers = [fill_text(str(t), values) for t in (entry.get("triggers") or [])]
    payload = {
        "category": fill_text(str(entry.get("category") or ""), values),
        "title": fill_text(str(entry.get("title") or ""), values).strip(),
        "triggers": triggers,
        "scenario": fill_text(str(entry.get("scenario") or ""), values),
        "steps": fill_text(str(entry.get("steps") or ""), values),
        "principles": fill_text(str(entry.get("principles") or ""), values),
        "example_reply_zh": fill_text(str(entry.get("example_reply_zh") or ""), values),
        "reply_mode": "ai_guided",
        "enabled": 1,
        "source": "import",
    }
    leftovers: Set[str] = set()
    for key in _ENTRY_TEXT_KEYS:
        leftovers.update(_PH_RE.findall(str(payload.get(key) or "")))
    for trig in triggers:
        leftovers.update(_PH_RE.findall(trig))
    return payload, leftovers


def apply_kb_seed(
    kb_store,
    pack_id: str,
    values: Optional[Dict[str, Any]] = None,
    *,
    apply: bool = False,
    dedup: bool = True,
    include_internal: Optional[bool] = None,
) -> Dict[str, Any]:
    """把垂直包 KB 种子写成计划或写入。

    ``apply=False`` 不调用 ``add_entry``。仍含 ``{placeholder}`` 的条目记入
    ``blocked_entries``，两种模式下都不写入。``source=import``。
    """
    pack = load_pack(pack_id, include_internal=include_internal)
    if apply and kb_store is None:
        raise RuntimeError("kb_unavailable")
    existing = _existing_titles(kb_store) if (dedup and kb_store is not None) else set()
    added = skipped = blocked = 0
    added_titles: List[str] = []
    would_add: List[str] = []
    blocked_entries: List[Dict[str, Any]] = []
    entries = (pack.get("kb") or {}).get("entries") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload, leftovers = _entry_payload(entry, values)
        title = payload["title"]
        if leftovers:
            blocked += 1
            blocked_entries.append({"title": title, "placeholders": sorted(leftovers)})
            continue
        if dedup and title in existing:
            skipped += 1
            continue
        if apply:
            kb_store.add_entry(payload)
            added += 1
            added_titles.append(title)
            existing.add(title)
        else:
            would_add.append(title)
            existing.add(title)
    return {
        "pack_id": pack["id"],
        "edition": pack["edition"],
        "apply": bool(apply),
        "added": added,
        "skipped": skipped,
        "blocked": blocked,
        "blocked_entries": blocked_entries,
        "added_titles": added_titles,
        "would_add_titles": would_add,
    }


def handle_seed_request(kb_store, body: Dict[str, Any]) -> Dict[str, Any]:
    """``POST /api/kb/seed-pack`` 在带 ``vertical_pack`` 时的处理。

    忽略请求体里的 ``allow_internal`` / ``include_internal``，可见性只看环境变量。
    ``dry_run`` 缺省为 false：显式点播种即写入（与起步包端点一致）；预览传 ``dry_run: true``。
    """
    pack_id = str((body or {}).get("vertical_pack") or "").strip()
    placeholders = body.get("placeholders") if isinstance(body.get("placeholders"), dict) else None
    if placeholders is None and isinstance(body.get("vars"), dict):
        placeholders = body.get("vars")
    dry_run = bool((body or {}).get("dry_run", False))
    try:
        report = apply_kb_seed(
            kb_store, pack_id, placeholders or {},
            apply=not dry_run, include_internal=None,
        )
    except UnknownPack:
        return {"ok": False, "detail": "unknown_pack"}
    except InternalPackHidden:
        return {"ok": False, "detail": "internal_pack_hidden"}
    if report["blocked"] and report["added"] == 0 and not dry_run:
        return {"ok": False, "detail": "unfilled_placeholders", **report}
    return {"ok": True, **report}
