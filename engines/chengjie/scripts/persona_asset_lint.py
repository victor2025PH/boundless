"""人设资产验收 lint 运行器（B3）：对 config/profiles_runtime.yaml 全量体检。

用法（repo 根目录）:
    python scripts/persona_asset_lint.py            # 报告 + error 时退出码 1
    python scripts/persona_asset_lint.py --strict   # warn 也算失败（上线门禁档）
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from src.companion.persona_asset_lint import format_report, lint_personas  # noqa: E402


def main() -> int:
    strict = "--strict" in sys.argv[1:]
    cfg_path = Path("config/profiles_runtime.yaml")
    if not cfg_path.is_file():
        print(f"找不到 {cfg_path}（请在 repo 根目录运行）")
        return 2
    data = yaml.safe_load(io.open(cfg_path, encoding="utf-8")) or {}
    profiles = data.get("profiles") or {}
    issues = lint_personas(profiles)
    print(format_report(issues))
    has_error = any(i["severity"] == "error" for i in issues)
    has_warn = any(i["severity"] == "warn" for i in issues)
    return 1 if (has_error or (strict and has_warn)) else 0


if __name__ == "__main__":
    sys.exit(main())
