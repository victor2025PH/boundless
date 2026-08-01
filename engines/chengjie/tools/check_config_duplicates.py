# -*- coding: utf-8 -*-
"""配置重复键自检（只读；2026-07-31 一次真实事故后补的护栏）。

**事故**：往实例 overlay `config.local.yaml` 的 `platform_login:` 下新插了一个
`telegram:` 块，没注意到那里**已经有一个** `telegram:`。PyYAML 对重复键**不报错、
不警告，静默取最后一个** → 原块整段被丢弃（`protocol_enabled` / `companion_runtime`
/ credpool 服务令牌 / 设备指纹 / sync 全没了）→ 重启后 `ensure_builtin_workers`
不再注册 Telegram worker → **5 个 TG 号离线约 2 分钟**，直到发现并合并修复。

这类错误的可怕之处是**完全无声**：YAML 解析成功、服务正常启动、日志没有异常，
只是少了一整块配置。人不可能靠「下次小心」防住，所以做成可跑的检查。

判据本身在 ``src/utils/yaml_duplicates.py``（单一事实源，三个消费口共用：本 CLI /
``restart_instance.ps1`` 重启前置闸门 / ``config_check`` 启动自检与 ``--check``）。

用法::

    python tools/check_config_duplicates.py            # 查本机所有实例数据根
    python tools/check_config_duplicates.py --repo     # 顺带查仓库自带 config/
    python tools/check_config_duplicates.py --json

退出码：发现重复键 → 1（便于挂进重启前置或计划任务）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


from src.utils.yaml_duplicates import find_duplicate_keys  # noqa: E402


def _targets(repo: bool, data_root: str = "") -> List[Path]:
    from scripts._data_root import resolve_data_roots

    out: List[Path] = []
    for root in resolve_data_roots(data_root):
        for name in ("config.yaml", "config.local.yaml"):
            p = Path(root) / "config" / name
            if p.is_file():
                out.append(p)
    if repo:
        for name in ("config.yaml", "config.local.yaml", "config.example.yaml"):
            p = ENGINE_ROOT / "config" / name
            if p.is_file():
                out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="配置重复键自检（只读）")
    ap.add_argument("--repo", action="store_true", help="顺带检查仓库自带 config/")
    ap.add_argument("--data-root", default="",
                    help="只查这个实例数据根（重启前置按实例定向检查用）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report: Dict[str, List[str]] = {}
    for p in _targets(args.repo, args.data_root):
        try:
            dups = find_duplicate_keys(p.read_text(encoding="utf-8"))
        except OSError as exc:
            dups = ["<read-error: %s>" % exc]
        if dups:
            report[str(p)] = dups

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("-- config duplicate-key check --")
        if not report:
            print("  OK: no duplicate keys")
        for path, dups in report.items():
            print("  DUP  %s" % path)
            for d in dups:
                print("        %s" % d)
            print("        ^ PyYAML 静默取最后一个 = 前面同名块被整段丢弃")
    return 1 if report else 0


if __name__ == "__main__":
    raise SystemExit(main())
