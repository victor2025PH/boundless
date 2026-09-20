"""相册媒体元数据回填 CLI（P0 图文一致性）：``_meta.json`` sidecar + DB ``tod:`` 标签。

一致性护栏的时段过滤（「深夜不发大白天照」）依赖每张图的 ``tod`` 标注，而存量
相册（策展 manifest 只有 scene/outfit）与注册相册 DB（tags 只有 scene:/series:）
都没有昼夜数据——本脚本一次回填、跑完即闭环：

1. **文件系统相册**（``SelfieProvider`` 消费）：每个相册目录合并
   manifest.json（scene/outfit）+ 文件名约定 + 已有 ``_meta.json``（人工值优先）
   → 写 ``_meta.json``；缺 tod 的照片用局域网 VLM（qwen2.5-vl，176/140 双活）
   保守分类（室内歧义=unknown 不打标，宁放过不误杀）。
2. **注册相册 DB**（``pick_media`` 消费）：photo 行缺 ``tod:`` 标签 → 同一套
   VLM 分类 → ``store.update`` 追加标签（幂等，重跑跳过已标注行）。

**默认 dry-run**（只报计划），``--apply`` 才写盘/写库；``--vlm`` 才真调 VLM
（缺省只合并 manifest/文件名元数据，零网络依赖）。

用法（生产双实例的相册在各自 data/config 下，用 --album-dir/--db 指路）：
  python -m scripts.persona_media_backfill_meta                        # 扫仓库默认相册，dry-run
  python -m scripts.persona_media_backfill_meta --vlm --apply          # 合并+VLM 补 tod+写盘
  python -m scripts.persona_media_backfill_meta ^
      --album-dir D:/chengjie-instances/zhiliao/data/config/persona_albums ^
      --db D:/chengjie-instances/zhiliao/data/config/persona_media.db --vlm --apply
  python -m scripts.persona_media_backfill_meta --persona lin_xiaoyu --json

退出码：0=正常（含 dry-run）；1=有错误（VLM 全灭/写盘失败等）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.companion.persona_media_meta_backfill import (  # noqa: E402
    apply_tod_results,
    build_tod_prompt,
    db_rows_needing_tod,
    is_album_photo,
    parse_tod_response,
    plan_album_backfill,
    tags_with_tod,
)

_DEFAULT_CONFIG = _ROOT / "config" / "config.yaml"
_META_NAME = "_meta.json"


def _load_cfg(config_path: Path) -> Dict[str, Any]:
    """config.yaml + 同目录 config.local.yaml overlay（浅递归合并，overlay 胜）。
    只为取 ``vision`` 块；读不动回空 dict（--no-vlm 流程零配置可跑）。"""
    def _read(p: Path) -> Dict[str, Any]:
        try:
            import yaml
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        for k, v in (over or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _merge(out[k], v)
            else:
                out[k] = v
        return out

    base = _read(config_path)
    overlay = _read(config_path.parent / "config.local.yaml")
    return _merge(base, overlay)


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _album_dirs(root: Path, only_persona: str = "") -> List[Path]:
    """待回填目录：根目录本身（平铺布局）+ 每个一级子目录（分册布局）。"""
    dirs: List[Path] = []
    if not root.is_dir():
        return dirs
    if only_persona:
        sub = root / only_persona
        return [sub] if sub.is_dir() else []
    dirs.append(root)
    try:
        dirs.extend(sorted(p for p in root.iterdir() if p.is_dir()))
    except Exception:
        pass
    return dirs


class _VlmClassifier:
    """VLM 昼夜分类器（薄封装：VisionClient 懒初始化 + 串行调用 + 计数）。

    进程内按**解析后路径**缓存结论（含 unknown）——相册遍与 DB 遍指向同一批
    文件（策展图先写 sidecar 再注册进 DB），不缓存等于同图烧两次 GPU。
    """

    def __init__(self, vision_cfg: Dict[str, Any]):
        self._cfg = vision_cfg or {}
        self._vc = None
        self.calls = 0
        self.errors = 0
        self.cache_hits = 0
        self._cache: Dict[str, str] = {}

    def available(self) -> bool:
        if not self._cfg:
            return False
        if self._vc is None:
            try:
                from src.vision_client import VisionClient
                vc = VisionClient(dict(self._cfg))
                if not vc.initialize():
                    return False
                self._vc = vc
            except Exception:
                return False
        return True

    @staticmethod
    def _key(image_path: str) -> str:
        try:
            return str(Path(image_path).resolve())
        except OSError:
            return str(image_path)

    def cached(self, image_path: str) -> Optional[str]:
        """已有结论（含 ""=unknown）；从未判过返回 None。"""
        return self._cache.get(self._key(image_path))

    def seed(self, image_path: str, tod: str) -> None:
        """外部已知结论灌缓存（如相册 sidecar 已标注 → DB 遍零 VLM 调用）。"""
        if tod in ("day", "night"):
            self._cache.setdefault(self._key(image_path), tod)

    async def classify(self, image_path: str) -> str:
        """单图昼夜分类 → day/night/""（失败按 unknown 处理，绝不抛出）。"""
        key = self._key(image_path)
        hit = self._cache.get(key)
        if hit is not None:
            self.cache_hits += 1
            return hit
        if not self.available():
            return ""
        self.calls += 1
        try:
            raw = await self._vc.describe_image(  # type: ignore[union-attr]
                str(image_path), prompt=build_tod_prompt())
            tod = parse_tod_response(raw)
        except Exception:
            self.errors += 1
            return ""  # 传输层失败不缓存（下次可重试）；解析 unknown 才缓存
        self._cache[key] = tod
        return tod


async def _backfill_album_dir(
    d: Path, clf: Optional[_VlmClassifier], *,
    apply: bool, force_tod: bool,
) -> Dict[str, Any]:
    """单目录回填：合并计划 → （可选）VLM 补 tod → （可选）写 _meta.json。"""
    try:
        files = sorted(p.name for p in d.iterdir() if p.is_file())
    except Exception:
        files = []
    photos = [f for f in files if is_album_photo(f)]
    if not photos:
        return {"dir": str(d), "photos": 0}
    manifest = _read_json(d / "manifest.json")
    existing = _read_json(d / _META_NAME)
    plan = plan_album_backfill(
        photos, manifest, existing if isinstance(existing, dict) else None,
        force_tod=force_tod)
    meta, need = plan["meta"], plan["need_tod"]
    tagged = 0
    vlm_used = 0
    if clf is not None and need:
        results: Dict[str, str] = {}
        for name in need:
            tod = await clf.classify(str(d / name))
            vlm_used += 1
            if tod:
                results[name] = tod
        meta, tagged = apply_tod_results(meta, results)
    # 已知结论灌分类器缓存：DB 遍多半指向同一批策展文件，免同图二次烧 GPU。
    if clf is not None:
        for name, m in meta.items():
            if isinstance(m, dict) and m.get("tod") in ("day", "night"):
                clf.seed(str(d / name), str(m["tod"]))
    changed = (meta != (existing if isinstance(existing, dict) else {}))
    wrote = False
    if apply and changed and meta:
        try:
            (d / _META_NAME).write_text(
                json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8")
            wrote = True
        except Exception as ex:
            return {"dir": str(d), "photos": len(photos), "error": str(ex)}
    return {
        "dir": str(d), "photos": len(photos), "meta_entries": len(meta),
        "need_tod": len(need), "vlm_calls": vlm_used, "tod_tagged": tagged,
        "changed": changed, "wrote": wrote,
    }


def _read_rows_ro(db_path: str, only_persona: str = "") -> Optional[List[Dict[str, Any]]]:
    """只读列 persona_media 行（dry-run 用）：``mode=ro`` URI，对活体生产库
    **零写事务**（store 构造器会跑 DDL/PRAGMA——审计路径不该碰）。失败返回 None。"""
    import sqlite3
    try:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT * FROM persona_media"
            args: tuple = ()
            if only_persona:
                sql += " WHERE persona_id = ?"
                args = (only_persona,)
            raw = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    rows: List[Dict[str, Any]] = []
    for r in raw:
        d = dict(r)
        for col in ("tags", "triggers"):
            try:
                v = d.get(col)
                d[col] = json.loads(v) if isinstance(v, str) and v else []
            except Exception:
                d[col] = []
        rows.append(d)
    return rows


async def _backfill_db(
    db_path: str, clf: Optional[_VlmClassifier], *,
    apply: bool, only_persona: str = "",
) -> Dict[str, Any]:
    """注册相册 DB 的 ``tod:`` 标签补齐（photo 行、幂等）。

    dry-run 走只读连接；--apply 才经 store 打开（写标签）。"""
    store = None
    if apply:
        from src.companion.persona_media_store import configure_persona_media_store
        store = configure_persona_media_store(db_path)
        if store is None:
            return {"db": db_path, "error": "store_unavailable"}
        rows = store.list(only_persona or None)
    else:
        ro = _read_rows_ro(db_path, only_persona)
        if ro is None:
            return {"db": db_path, "error": "db_unreadable"}
        rows = ro
    todo = db_rows_needing_tod(rows)
    tagged = 0
    missing_file = 0
    sidecar_hits = 0
    for r in todo:
        fp = Path(str(r.get("file_path") or ""))
        if not fp.is_file():
            missing_file += 1
            continue
        # sidecar 优先：文件所在目录 _meta.json 已有 tod（相册遍刚写的/人工标的）
        # → 直接采信零 VLM 调用；没有才问分类器（其自带跨遍缓存）。
        tod = ""
        try:
            from src.ai.companion_selfie import album_file_meta, load_album_meta
            tod = str(album_file_meta(
                fp.name, load_album_meta(fp.parent)).get("tod") or "")
            if tod not in ("day", "night"):
                tod = ""
            elif clf is not None:
                sidecar_hits += 1
        except Exception:
            tod = ""
        if not tod and clf is not None:
            tod = await clf.classify(str(fp))
        new_tags = tags_with_tod(r.get("tags"), tod)
        if not new_tags:
            continue
        if apply and store is not None:
            store.update(str(r.get("id")), tags=new_tags)
        tagged += 1
    return {"db": db_path, "rows": len(rows), "need_tod": len(todo),
            "tagged": tagged, "missing_file": missing_file,
            "sidecar_hits": sidecar_hits}


async def _amain(args: argparse.Namespace) -> int:
    clf: Optional[_VlmClassifier] = None
    if args.vlm:
        cfg = _load_cfg(Path(args.config))
        vcfg = dict(cfg.get("vision") or {})
        clf = _VlmClassifier(vcfg)
        if not clf.available():
            print("警告：VLM 不可用（vision 配置缺失/初始化失败）——"
                  "本次只合并 manifest/文件名元数据，不打 tod 标注",
                  file=sys.stderr)
            clf = None

    out: Dict[str, Any] = {"apply": bool(args.apply), "vlm": clf is not None,
                           "albums": [], "db": None}
    root = Path(args.album_dir)
    for d in _album_dirs(root, args.persona):
        res = await _backfill_album_dir(
            d, clf, apply=bool(args.apply), force_tod=bool(args.force_tod))
        if res.get("photos"):
            out["albums"].append(res)
    if args.db:
        out["db"] = await _backfill_db(
            str(args.db), clf, apply=bool(args.apply),
            only_persona=args.persona)
    if clf is not None:
        out["vlm_calls"] = clf.calls
        out["vlm_errors"] = clf.errors
        out["vlm_cache_hits"] = clf.cache_hits

    errors = [a for a in out["albums"] if a.get("error")]
    if isinstance(out.get("db"), dict) and out["db"].get("error"):
        errors.append(out["db"])

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] album_root={root} vlm={'on' if clf else 'off'}")
        for a in out["albums"]:
            if a.get("error"):
                print(f"  {a['dir']}: ERROR {a['error']}")
                continue
            print(f"  {a['dir']}: photos={a['photos']} "
                  f"meta={a.get('meta_entries', 0)} need_tod={a.get('need_tod', 0)} "
                  f"tod_tagged={a.get('tod_tagged', 0)} "
                  f"{'wrote' if a.get('wrote') else ('changed' if a.get('changed') else 'no-change')}")
        if isinstance(out.get("db"), dict):
            db = out["db"]
            if db.get("error"):
                print(f"  DB {db['db']}: ERROR {db['error']}")
            else:
                print(f"  DB {db['db']}: rows={db['rows']} need_tod={db['need_tod']} "
                      f"tagged={db['tagged']} missing_file={db['missing_file']}")
        if not args.apply:
            print("（这是 dry-run；加 --apply 才写 _meta.json / 更新 DB 标签）")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="相册媒体 tod/场景元数据回填（_meta.json sidecar + DB 标签）")
    ap.add_argument("--album-dir", default="config/persona_albums",
                    help="相册根目录（默认 config/persona_albums；生产实例指向其 data/config/persona_albums）")
    ap.add_argument("--db", default="",
                    help="persona_media 注册库路径（可选；给了才回填 DB tod: 标签）")
    ap.add_argument("--config", default=str(_DEFAULT_CONFIG),
                    help="config.yaml 路径（取 vision 块；同目录 config.local.yaml 自动 overlay）")
    ap.add_argument("--persona", default="", help="只回填该人设分册/该人设的 DB 行")
    ap.add_argument("--vlm", action="store_true",
                    help="用局域网 VLM 给缺 tod 的照片打昼夜标注（缺省只合并 manifest/文件名）")
    ap.add_argument("--force-tod", action="store_true",
                    help="全部照片重打 tod（换图重策展后用；须配 --vlm）")
    ap.add_argument("--apply", action="store_true",
                    help="真写 _meta.json / 更新 DB（缺省 dry-run 只报计划）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
