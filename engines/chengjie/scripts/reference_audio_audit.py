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

OUT_JSON = _ROOT / "logs" / "reference_audio_audit.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="avatar_clone 参考音质量审计")
    ap.add_argument("--persona", default="", help="只审指定人设（默认全部）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    args = ap.parse_args()

    from scripts.avatar_prerender import _collect_avatar_personas
    from scripts.voice_similarity_probe import _load_config
    from src.ai.reference_audio_audit import audit_reference_file

    cfg = _load_config()
    targets = _collect_avatar_personas(cfg)
    if args.persona:
        targets = [(p, r) for p, r in targets if p == args.persona]
    if not targets:
        print("[!] 无 avatar_clone 人设（或参考音不在盘）")
        return 0

    # 同参考音只审一次（多人设共用音色）
    by_ref: dict = {}
    personas_of: dict = {}
    for pid, ref in targets:
        key = str(Path(ref).resolve())
        personas_of.setdefault(key, []).append(pid)
        if key not in by_ref:
            by_ref[key] = audit_reference_file(ref)

    results = []
    worst = "ok"
    order = {"ok": 0, "warn": 1, "bad": 2}
    for key, report in by_ref.items():
        row = dict(report)
        row["personas"] = personas_of.get(key, [])
        results.append(row)
        if order.get(report["level"], 2) > order.get(worst, 0):
            worst = report["level"]

    payload = {
        "ts": time.time(),
        "date": time.strftime("%Y-%m-%d"),
        "worst": worst,
        "results": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

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
    print(f"[*] 审计完成：{len(results)} 条参考音，最差={worst}（报告已写 {OUT_JSON.name}）")
    return 1 if worst == "bad" else 0


if __name__ == "__main__":
    raise SystemExit(main())
