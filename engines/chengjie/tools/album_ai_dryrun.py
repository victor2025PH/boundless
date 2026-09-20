"""相册 AI 打标只读验收（实施90 阶段二）——重启装载前就能看真数据准确率。

动机：打标代码在磁盘上等重启窗，但「VLM 对我们的真实素材打得准不准」不必等
——本工具对生产注册相册**只读采样**（``mode=ro`` URI，零写库零写盘，连缩略图
都不生成），逐张真跑 LAN VLM 打标管线（与线上同一套 prompt/解析/合并纯函数），
把「建议标签 vs 现有标注」摆出来给人裁决。

用法（默认 dry 且只 dry，没有 --apply——写入永远走线上 retag 端点/重启后的批任务）：

  python tools/album_ai_dryrun.py --sample 8                      # 仓库默认库
  python tools/album_ai_dryrun.py ^
      --db D:/chengjie-instances/zhiliao/data/config/persona_media.db --sample 10
  python tools/album_ai_dryrun.py --persona lin_xiaoyu --json

输出：逐张（现有标签/触发词 → 建议 scene/tod/season/place/敏感度/描述/触发词 +
红旗）+ 汇总（解析成功率、各维度产出率、平均耗时）。退出码恒 0（报告工具）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.persona_media_backfill_meta import (  # noqa: E402（复用单源）
    _load_cfg,
    _read_rows_ro,
)
from src.companion.media_auto_tag import (  # noqa: E402
    _vision_describe,
    build_auto_tag_prompt,
    derive_tags,
    detect_tag_conflicts,
    parse_auto_tag_response,
)

_DIM_KEYS = ("scene", "tod", "season", "country", "desc")


def _short(v, n=60):
    s = str(v or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    # Windows 控制台默认 GBK：⚠/emoji 会直接 UnicodeEncodeError 砍断报告
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="相册 AI 打标只读验收（零写入）")
    ap.add_argument("--db", default="config/persona_media.db",
                    help="persona_media 注册库（生产=实例 data/config 下那份）")
    ap.add_argument("--config", default=str(_ROOT / "config" / "config.yaml"),
                    help="config.yaml（取 vision 块；同目录 config.local.yaml 自动 overlay）")
    ap.add_argument("--persona", default="", help="只采样该人设")
    ap.add_argument("--sample", type=int, default=8, help="采样张数（照片）")
    ap.add_argument("--seed", type=int, default=7, help="采样随机种子（可复现）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    rows = _read_rows_ro(str(args.db), args.persona)
    if rows is None:
        print(f"库不可读：{args.db}", file=sys.stderr)
        return 0
    photos = [r for r in rows
              if str(r.get("media_type") or "photo") == "photo"
              and str(r.get("file_path") or "")
              and Path(str(r.get("file_path"))).is_file()]
    if not photos:
        print("没有可采样的照片（库为空或文件缺失）", file=sys.stderr)
        return 0
    rng = random.Random(args.seed)
    picked = photos if len(photos) <= args.sample else rng.sample(
        photos, args.sample)

    vcfg = dict((_load_cfg(Path(args.config)) or {}).get("vision") or {})
    prompt = build_auto_tag_prompt()
    out = {"db": str(args.db), "sampled": len(picked),
           "photos_total": len(photos), "items": [],
           "vlm_fail": 0, "parse_fail": 0, "flags": 0,
           "dim_filled": {k: 0 for k in _DIM_KEYS}, "avg_ms": 0}
    total_ms = 0.0
    for r in picked:
        fp = str(r.get("file_path"))
        t0 = time.time()
        raw = _vision_describe(vcfg, fp, prompt)
        dt = (time.time() - t0) * 1000.0
        total_ms += dt
        item = {"id": str(r.get("id")), "persona": str(r.get("persona_id")),
                "file": Path(fp).name, "ms": int(dt),
                "existing_tags": r.get("tags") or [],
                "existing_triggers": r.get("triggers") or []}
        if raw is None:
            out["vlm_fail"] += 1
            item["error"] = "vlm_unavailable"
            out["items"].append(item)
            continue
        parsed = parse_auto_tag_response(raw)
        if parsed is None:
            out["parse_fail"] += 1
            item["error"] = "parse_fail"
            item["raw_head"] = _short(raw, 120)
            out["items"].append(item)
            continue
        for k in _DIM_KEYS:
            if parsed.get(k):
                out["dim_filled"][k] += 1
        if parsed.get("nsfw") or parsed.get("explicit") or parsed.get("underage"):
            out["flags"] += 1
        item.update({
            "scene": parsed.get("scene"), "tod": parsed.get("tod"),
            "season": parsed.get("season"),
            "place": (parsed.get("country")
                      if parsed.get("country_conf") == "high" else ""),
            "sensitivity": parsed.get("sensitivity"),
            "quality": parsed.get("quality"),
            "desc": parsed.get("desc"),
            "triggers_suggest": parsed.get("triggers"),
            "flags": {k: bool(parsed.get(k))
                      for k in ("nsfw", "explicit", "underage")},
            "would_add_tags": [t for t in (derive_tags(parsed, {}, r.get("tags"))
                                           or []) if t not in (r.get("tags") or [])],
        })
        conflicts = detect_tag_conflicts(parsed, r.get("tags"))
        if conflicts:
            item["conflicts"] = conflicts
        out["items"].append(item)
    done = len(picked) - out["vlm_fail"] - out["parse_fail"]
    out["avg_ms"] = int(total_ms / max(1, len(picked)))
    out["tag_ok"] = done

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"[DRY-RUN 只读] db={args.db}  照片 {out['photos_total']} 张，"
          f"采样 {out['sampled']}，成功 {done}，VLM 失败 {out['vlm_fail']}，"
          f"解析失败 {out['parse_fail']}，平均 {out['avg_ms']}ms/张")
    for it in out["items"]:
        if it.get("error"):
            print(f"  ✗ {it['persona']}/{it['file']}: {it['error']}")
            continue
        print(f"  ● {it['persona']}/{it['file']}  ({it['ms']}ms)")
        print(f"    现有: tags={it['existing_tags']} triggers={it['existing_triggers']}")
        print(f"    建议: scene={it['scene'] or '-'} tod={it['tod'] or '-'} "
              f"season={it['season'] or '-'} place={it['place'] or '-'} "
              f"敏感度={it['sensitivity']} 画质={it['quality'] or '-'}")
        print(f"    描述: {_short(it['desc'])}")
        print(f"    触发词建议: {it['triggers_suggest']}"
              + (f"  → 会补标签 {it['would_add_tags']}" if it["would_add_tags"] else ""))
        fl = [k for k, v in (it.get('flags') or {}).items() if v]
        if fl:
            print(f"    ⚠ 红旗: {fl}")
        if it.get("conflicts"):
            pairs = "; ".join(
                f"{k}: 人工 {v.get('manual')} ↔ AI {v.get('ai')}"
                for k, v in it["conflicts"].items())
            print(f"    ⚠ 标注分歧: {pairs}")
    df = out["dim_filled"]
    print(f"  维度产出率（{max(1, done)} 张成功里）: scene {df['scene']}/{done} · "
          f"tod {df['tod']}/{done} · season {df['season']}/{done} · "
          f"place {df['country']}/{done} · desc {df['desc']}/{done} · "
          f"红旗 {out['flags']}")
    print("  （零写入；正式打标走相册页「AI 补标」或上传自动打标）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
