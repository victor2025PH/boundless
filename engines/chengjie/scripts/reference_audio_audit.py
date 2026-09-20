# -*- coding: utf-8 -*-
"""参考音质量审计 CLI — Phase E「按报告换参考音」的运营入口。

对所有 avatar_clone 人设的参考音跑确定性体检（时长/削波/静音/能量动态/音高动态/
逐字稿），打印报告并写 logs/reference_audio_audit.json（avatar-status API 读它出
reference_quality，看板可见）。零 GPU、纯 CPU 分析，随时可跑。

用法：python -m scripts.reference_audio_audit [--persona lin_jiaxin] [--json]
"""
from __future__ import annotations

import argparse
import json
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

# 产物按「运行根契约」逐根落 <数据根>/logs/reference_audio_audit.json
# （app 的 avatar-status 按实例 CWD 相对路径读它出 reference_quality）。


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="avatar_clone 参考音质量审计")
    ap.add_argument("--persona", default="", help="只审指定人设（默认全部）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    ap.add_argument("--data-root", default="",
                    help="实例数据根（缺省按运行根契约解析，可多实例）")
    args = ap.parse_args(argv)

    from scripts._data_root import resolve_data_roots
    from scripts.avatar_prerender import _collect_avatar_personas
    from scripts.voice_similarity_probe import _load_config
    from src.ai.reference_audio_audit import audit_reference_file

    roots = resolve_data_roots(args.data_root)
    print(f"[*] 数据根 ×{len(roots)}: " + " | ".join(str(r) for r in roots))

    by_ref: dict = {}      # 跨根共享：同参考音只审一次（多人设/多实例共用音色）
    payload = None
    for root in roots:
        cfg = _load_config(root)
        targets = _collect_avatar_personas(cfg, root=root)
        if args.persona:
            targets = [(p, r) for p, r in targets if p == args.persona]
        if not targets:
            print(f"[!] [{root}] 无 avatar_clone 人设（或参考音不在盘）")
            continue

        personas_of: dict = {}
        for pid, ref in targets:
            key = str(Path(ref).resolve())
            personas_of.setdefault(key, []).append(pid)
            if key not in by_ref:
                by_ref[key] = audit_reference_file(ref)

        results = []
        worst = "ok"
        order = {"ok": 0, "warn": 1, "bad": 2}
        for key, pids in personas_of.items():
            report = by_ref[key]
            row = dict(report)
            row["personas"] = pids
            results.append(row)
            if order.get(report["level"], 2) > order.get(worst, 0):
                worst = report["level"]

        payload = {
            "ts": time.time(),
            "date": time.strftime("%Y-%m-%d"),
            "worst": worst,
            "results": results,
        }
        out_json = Path(root) / "logs" / "reference_audio_audit.json"
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if payload is None:
        print("[!] 所有数据根均无目标，未写审计产物")
        return 0

    worst = payload["worst"]
    results = payload["results"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 1 if worst == "bad" else 0

    mark = {"ok": "✓", "warn": "⚠", "bad": "✗"}
    for row in results:
        m = row.get("metrics") or {}
        head = (f"  {mark[row['level']]} {'/'.join(row['personas'])}: "
                f"{Path(row['ref']).name}")
        if m:
            head += (f"  {m.get('duration_sec')}s/{m.get('sample_rate')}Hz"
                     f" 能量std={m.get('energy_db_std')}dB"
                     f" 音高std={m.get('f0_semi_std')}semi"
                     f" 逐字稿={'有' if row.get('has_sidecar') else '无'}")
        print(head)
        for issue in row.get("issues") or []:
            print(f"      - {issue}")
        for tip in row.get("tips") or []:
            print(f"      → {tip}")
    print(f"[*] 审计完成：{len(results)} 条参考音，最差={worst}"
          "（报告已写各数据根 logs/reference_audio_audit.json）")
    return 1 if worst == "bad" else 0


if __name__ == "__main__":
    raise SystemExit(main())
