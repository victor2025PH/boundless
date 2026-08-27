# -*- coding: utf-8 -*-
"""团队话术多语言变体旁挂档（templates_i18n.yaml）的**唯一写入口**（V2 起草工作台）。

分工（与 V0/V1 合流线的读侧契约）：
- 读侧单一事实源＝``unified_inbox_context._load_templates_i18n``（mtime 缓存
  → 本模块落盘后**免重启热生效**）；schema 同构：``键: {语言: [条目]}``，
  条目＝纯字符串（已审核）或 ``{text, approved}``（approved 缺省 False=机器稿）。
- 本模块管写：/templates 起草工作台端点与起草 CLI 共用；**写侧统一落显式
  ``{text, approved}`` 形态**（读侧两种都认，显式形态 diff/审阅可读）。
- 路径解析复用读侧同一双落点（``_qtpl_i18n_candidates``：实例 config → 引擎根）：
  写「读侧当前会命中的那个文件」；都不存在 → 创建实例目录那份（与
  ``save_templates`` 写实例的语义一致，绝不产生「写了 A 读的是 B」的双真相）。
- 原子写（tmp + os.replace）+ 进程内互斥锁（起草/确认是低频管理操作，单进程够用）。

起草纪律（钉死在写入口，不靠调用方自觉）：
- ``has_placeholders``：含 ``{var}`` 占位符的源文本**拒绝机器起草**——机翻会把
  占位符名一起翻掉，破坏面板变量补全表单的契约（P2 实测判定）。
- ``draft_variant`` 语义＝替换该语言全部**未审核**条目（旧机器稿不堆积），
  已审核条目永不被机器稿触碰。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.web.routes.unified_inbox_context import _qtpl_i18n_candidates

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_VAR_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
LANG_CODE_RE = re.compile(r"^[a-z]{2}$")


def has_placeholders(text: Any) -> bool:
    """源文本含 {var} 占位符＝禁止机器起草（机翻会翻掉占位符名）。"""
    return bool(_VAR_RE.search(str(text or "")))


def resolve_path(config_manager) -> Path:
    """读侧当前命中的文件；都不存在 → 首候选（实例 config 目录）。"""
    cands = _qtpl_i18n_candidates(config_manager)
    for cand in cands:
        try:
            if Path(cand).exists():
                return Path(cand)
        except Exception:
            continue
    return Path(cands[0])


def _norm_entry(item: Any) -> Optional[Dict[str, Any]]:
    """与读侧 _load_templates_i18n 同一条目语义（str=已审核 / dict 缺省=机器稿）。"""
    if isinstance(item, str) and item.strip():
        return {"text": item.strip(), "approved": True}
    if isinstance(item, dict):
        t = str(item.get("text") or "").strip()
        if t:
            return {"text": t, "approved": bool(item.get("approved", False))}
    return None


def load_all(config_manager) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """全量读（绕缓存直读文件——写侧要最新真相；坏档回空绝不抛）。"""
    path = resolve_path(config_manager)
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("templates_i18n.yaml 直读失败", exc_info=True)
        return {}
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    if not isinstance(raw, dict):
        return {}
    for key, langs in raw.items():
        k = str(key or "").strip()
        if not k or k.startswith("_") or not isinstance(langs, dict):
            continue
        lang_map: Dict[str, List[Dict[str, Any]]] = {}
        for lang, items in langs.items():
            lcode = str(lang or "").strip().lower()
            if not lcode or lcode == "zh":
                continue
            lst = items if isinstance(items, list) else [items]
            norm = [e for e in (_norm_entry(it) for it in lst) if e]
            if norm:
                lang_map[lcode] = norm
        if lang_map:
            out[k] = lang_map
    return out


def dump_yaml(data: Dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)


def _write(path: Path, data: Dict[str, Any]) -> None:
    """原子落盘：空数据也写空映射（文件在=读侧缓存键稳定）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(dump_yaml(data), encoding="utf-8")
    os.replace(tmp, path)


def _mutate(
    config_manager, key: str, lang: str, fn,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """读-改-写骨架：fn(entries)->（新 entries|None=拒绝, err）。返回 (ok, err, 新列表)。"""
    k = str(key or "").strip()
    lcode = str(lang or "").strip().lower()
    if not k or k.startswith("_"):
        return False, "bad_key", []
    if not LANG_CODE_RE.match(lcode) or lcode == "zh":
        return False, "bad_lang", []
    with _LOCK:
        data = load_all(config_manager)
        entries = list((data.get(k) or {}).get(lcode) or [])
        new_entries, err = fn(entries)
        if err:
            return False, err, entries
        if new_entries:
            data.setdefault(k, {})[lcode] = new_entries
        else:
            (data.get(k) or {}).pop(lcode, None)
            if k in data and not data[k]:
                data.pop(k, None)
        try:
            _write(resolve_path(config_manager), data)
        except Exception:
            logger.warning("templates_i18n.yaml 写入失败", exc_info=True)
            return False, "write_failed", entries
        return True, "", new_entries


def draft_variant(
    config_manager, key: str, lang: str, text: str,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """机器起草：替换该语言全部未审核条目（不堆积），已审核条目原样保留。"""
    t = str(text or "").strip()
    if not t:
        return False, "empty_text", []

    def _fn(entries):
        kept = [e for e in entries if e.get("approved")]
        if any(e["text"] == t for e in kept):
            return None, "duplicate"
        return kept + [{"text": t, "approved": False}], ""

    return _mutate(config_manager, key, lang, _fn)


def upsert_variant(
    config_manager, key: str, lang: str, text: str, *,
    approved: bool = True, index: Optional[int] = None,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """人工保存：index=None 追加；否则原位替换（改稿即人工产物，approved 随参）。"""
    t = str(text or "").strip()
    if not t:
        return False, "empty_text", []

    def _fn(entries):
        dup_at = next((i for i, e in enumerate(entries) if e["text"] == t), -1)
        if index is None:
            if dup_at >= 0:
                return None, "duplicate"
            return entries + [{"text": t, "approved": bool(approved)}], ""
        if not (0 <= index < len(entries)):
            return None, "not_found"
        if dup_at >= 0 and dup_at != index:
            return None, "duplicate"
        out = list(entries)
        out[index] = {"text": t, "approved": bool(approved)}
        return out, ""

    return _mutate(config_manager, key, lang, _fn)


def confirm_variant(
    config_manager, key: str, lang: str, index: int,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """人工确认机器稿（对齐 KB kb_translations 的 auto_translated→确认 心智）。"""

    def _fn(entries):
        if not (0 <= index < len(entries)):
            return None, "not_found"
        out = list(entries)
        out[index] = {"text": out[index]["text"], "approved": True}
        return out, ""

    return _mutate(config_manager, key, lang, _fn)


def delete_variant(
    config_manager, key: str, lang: str, index: int,
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    def _fn(entries):
        if not (0 <= index < len(entries)):
            return None, "not_found"
        return entries[:index] + entries[index + 1:], ""

    return _mutate(config_manager, key, lang, _fn)
