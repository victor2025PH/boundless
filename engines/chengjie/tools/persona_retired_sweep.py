# -*- coding: utf-8 -*-
"""撤销设定「复活」清扫 CLI（2026-08-04 P2 收尾）——直改 YAML 写入方的自查工具。

用途：agent 线 / 运维**直接编辑** ``profiles_runtime.yaml``（批量丰富、手改）后，
Studio 保存路径的落库前检测（``persona_routes`` → ``retired_conflicts``）碰不到
这次写入——跑本工具自证「没把运营删过的设定写回去」。看门狗
（``health_watchdog.persona_retired_remind``，默认 60min 一轮）是运行时兜底；
本工具是**写入前后**的即时门禁（退出码可进脚本/CI）。

用法::

    python tools/persona_retired_sweep.py            # 自动发现活跃实例数据根
    python tools/persona_retired_sweep.py --data-root D:/chengjie-instances/zhiliao/data
    python tools/persona_retired_sweep.py --json     # 机器可读输出

只读；发现冲突退出码 1，全净 0（数据根缺文件按「无档案可查」算净——工具的
职责是查冲突，不是查部署完整性）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.utils.persona_retired import retired_conflicts  # noqa: E402


def _load_profiles(data_root: Path) -> dict:
    """数据根 → ``{pid: persona}``（runtime 覆盖 canonical，与服务装载序一致）。

    只看 personas.yaml + profiles_runtime.yaml 两层文件（config.yaml 基础层的
    内置示例人设不含运营撤销语义，扫它只添噪音）。任何一层坏/缺 → 跳过该层。
    """
    import yaml
    out: dict = {}
    for fname in ("personas.yaml", "profiles_runtime.yaml"):
        p = data_root / "config" / fname
        if not p.is_file():
            continue
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"  [warn] {p} 解析失败：{e}", file=sys.stderr)
            continue
        profiles = raw.get("profiles")
        if isinstance(profiles, dict):
            for pid, pdata in profiles.items():
                if isinstance(pdata, dict) and pid:
                    out[str(pid)] = pdata
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="撤销设定复活清扫（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    report = []
    total = 0
    for root in resolve_data_roots(args.data_root):
        profiles = _load_profiles(Path(root))
        for pid, persona in sorted(profiles.items()):
            try:
                hits = retired_conflicts(persona)
            except Exception:
                continue
            if hits:
                total += len(hits)
                report.append({
                    "data_root": str(root), "persona_id": pid,
                    "conflicts": [
                        {"term": h.get("term", ""), "path": h.get("path", ""),
                         "text": str(h.get("text") or "")[:80]}
                        for h in hits
                    ],
                })

    if args.json:
        print(json.dumps({"ok": total == 0, "total": total, "items": report},
                         ensure_ascii=False, indent=2))
    elif not report:
        print("OK：全部人设与撤销清单零冲突")
    else:
        print(f"发现 {total} 处「被删设定被写回」冲突：")
        for item in report:
            print(f"\n  [{item['persona_id']}] @ {item['data_root']}")
            for c in item["conflicts"]:
                print(f"    - {c['path']}  锚词「{c['term']}」：{c['text']}")
        print("\n处置：人设工作室 → 该人设 → 预览 → 内容排查删掉复活内容；"
              "有意恢复设定请先删对应撤销钉子（boundaries.retired_facts）。")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
