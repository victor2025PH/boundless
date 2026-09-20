"""相册场景缺口补货渲染 CLI（P2 图文一致性，2026-07-27）。

与语音侧「watchdog 自动入库 → 夜间 AvatarPrerenderNightly 渲染」同哲学的图片版：
主进程 watchdog（``_check_media_restock``）把「点名场景反复要不到 + 相册零备货」
写进计划文件（``config/album_restock_plan.json``），本 CLI 在**独立进程**读计划
逐项真出图入册：

- 衣着走 P2-1 场景联动（健身房补运动装、海边不补毛衣），系列=衣着 slug；
- 体检走 ``generate_with_gate`` 全套 + P2-2 场景后验（补进的图保证真是该场景）；
- 策展命名 ``<scene>_<series>_<nn>.jpg`` 落 ``album_dir/<persona>/``，登记
  ``_meta.json``（scene/tod/series）——下次点名即有货。

用法（引擎根目录）：
  python -m scripts.album_restock                     # 处理计划文件全部 pending
  python -m scripts.album_restock --list              # 只看计划不渲染
  python -m scripts.album_restock --persona lin_jiaxin --scene beach --count 2
                                                      # 手动补指定人设/场景（不走计划）
  python -m scripts.album_restock --dry-run           # 演练：列出将做什么

生成走生产同款 provider（command/ComfyUI 或 openai）——**建议夜间低峰跑**，
与在线出图共享同一后端。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass


def _load_config() -> dict:
    """读合并后的项目配置（config.yaml + config.local.yaml overlay）。"""
    import yaml

    cfg_path = _ROOT / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    local = _ROOT / "config" / "config.local.yaml"
    if local.is_file():
        overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}

        def _deep_merge(dst: dict, src: dict) -> dict:
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _deep_merge(dst[k], v)
                else:
                    dst[k] = v
            return dst

        _deep_merge(data, overlay)
    return data


async def _run(args: argparse.Namespace) -> int:
    from src.companion.media_restock import (load_plan, mark_item_done,
                                             pending_items,
                                             render_restock_item,
                                             resolve_restock_cfg, save_plan)

    config = _load_config()
    scfg = ((config.get("companion") or {}).get("selfie") or {})
    if not isinstance(scfg, dict) or not scfg.get("enabled", False):
        print("[restock] companion.selfie 未启用，退出")
        return 1
    rc = resolve_restock_cfg(scfg)
    plan_path = args.plan or rc["plan_path"]

    # 手动模式：--scene（可选 --persona）直接渲染，不读写计划文件。
    if args.scene:
        item = {"persona_id": args.persona or "", "scene": args.scene,
                "count": max(1, int(args.count or rc["per_scene"]))}
        if args.dry_run:
            print(f"[restock] DRY-RUN 将补：{item}")
            return 0
        t0 = time.time()
        rv = await render_restock_item(item, scfg, config)
        print(f"[restock] {item['persona_id'] or '(root)'}:{item['scene']} → "
              f"rendered={rv['rendered']}/{item['count']} "
              f"reason={rv['reason'] or 'ok'} ({time.time() - t0:.0f}s)")
        for f in rv["files"]:
            print(f"  + {f}")
        return 0 if rv["ok"] else 2

    plan = load_plan(plan_path)
    todo = pending_items(plan)
    if args.list or args.dry_run:
        print(f"[restock] 计划 {plan_path}：pending={len(todo)}")
        for it in todo:
            print(f"  - {it.get('persona_id') or '(root)'}:{it.get('scene')} "
                  f"x{it.get('count', 1)}（{it.get('requested_at', '?')}）")
        return 0
    if not todo:
        print(f"[restock] 计划无 pending 项（{plan_path}），退出")
        return 0
    total_ok = 0
    for it in todo:
        t0 = time.time()
        try:
            rv = await render_restock_item(it, scfg, config)
        except Exception as ex:  # noqa: BLE001
            rv = {"ok": False, "rendered": 0, "files": [],
                  "reason": f"{type(ex).__name__}"}
        mark_item_done(it, int(rv.get("rendered", 0)))
        save_plan(plan_path, plan)   # 逐项落盘：中途崩溃已完成项不重跑
        total_ok += int(rv.get("rendered", 0))
        print(f"[restock] {it.get('persona_id') or '(root)'}:{it.get('scene')} → "
              f"rendered={rv.get('rendered', 0)}/{it.get('count', 1)} "
              f"reason={rv.get('reason') or 'ok'} ({time.time() - t0:.0f}s)")
        for f in rv.get("files") or []:
            print(f"  + {f}")
    print(f"[restock] 完成：{len(todo)} 项计划，共入册 {total_ok} 张")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="相册场景缺口补货渲染")
    ap.add_argument("--plan", default="", help="计划文件路径（默认读配置）")
    ap.add_argument("--persona", default="", help="手动模式：目标人设 id")
    ap.add_argument("--scene", default="", help="手动模式：目标场景类（beach/gym/...）")
    ap.add_argument("--count", type=int, default=0, help="手动模式：补几张")
    ap.add_argument("--list", action="store_true", help="只列计划不渲染")
    ap.add_argument("--dry-run", action="store_true", help="演练：列出将做什么")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
