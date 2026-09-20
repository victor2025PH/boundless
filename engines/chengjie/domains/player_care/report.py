"""player_care 日报 CLI：按我方账号 × 阶段的画像快照 + 当日计数。

    .venv\\Scripts\\python.exe -m domains.player_care.report --config config_player\\config.yaml
    .venv\\Scripts\\python.exe -m domains.player_care.report --config ... --day 2026-09-20 --json

跑之前先做一遍沉默 → dormant 的扫描（--no-sweep 关掉）。只读 contacts.db 旁表，不碰网关。
"""
from __future__ import annotations

import argparse
import json
import sys

from .profile import PlayerProfileService, get_profile_service


def build_report(config_path: str, *, day: str = "", sweep: bool = True) -> dict:
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager(config_path)
    svc = get_profile_service(cm)
    if svc is None:
        raise SystemExit("player_care.profile 未启用或画像库不可用（检查 --config 路径 / player_care.profile.enabled）")
    if sweep:
        svc.apply_dormancy()
    return svc.daily_report(day or None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="player_care 日报（按账号 / 阶段）")
    ap.add_argument("--config", required=True, help="player 实例 config.yaml 路径")
    ap.add_argument("--day", default="", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是文本表")
    ap.add_argument("--no-sweep", action="store_true", help="不先做沉默→dormant 扫描")
    args = ap.parse_args(argv)
    rep = build_report(args.config, day=args.day, sweep=not args.no_sweep)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(PlayerProfileService.render_daily_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
