# -*- coding: utf-8 -*-
"""跨平台身份影子扫描 CLI —— 只读发现「疑似同一人」，一行关联都不写。

    # 人类可读报告（默认读 config/inbox.db + config/bot.db；受 feature flag 管）
    python -m scripts.identity_shadow_scan

    # flag 关着也要手动评估（影子模式的常态用法）
    python -m scripts.identity_shadow_scan --force

    # 机器可读
    python -m scripts.identity_shadow_scan --force --json

    # 指定库（双实例部署下各实例库不同）
    python -m scripts.identity_shadow_scan --force \\
        --inbox-db D:/chengjie-instances/zhiliao/config/inbox.db \\
        --identity-db D:/chengjie-instances/zhiliao/config/bot.db

**与真实关联的关系**：报告里的每一对都只是线索；确认同一人后走既有手动接口
``POST /api/identity/link``（存储=bot.db ``user_identity_map``，见
:mod:`src.utils.cross_platform_identity`）。已手动关联过的对在报告里带
「已关联」标注（用于核对自动发现的查全率）。

**只读保证**：两个库都经 sqlite ``mode=ro`` URI 打开（不建表、不迁移、不写），
对正在服务的生产实例零干扰。feature flag ``contacts.identity_shadow.enabled``
默认关；``--force`` 无视开关（便于开闸前人工评估）。后台任务将来接线时直接调
:func:`src.utils.identity_shadow.run_shadow_scan`（本次刻意不接调度器）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.identity_shadow import (  # noqa: E402
    format_report,
    run_shadow_scan,
)

_DEFAULT_CONFIG = _ROOT / "config" / "config.yaml"


def _load_cfg(config_path: Path) -> Dict[str, Any]:
    """读 config.yaml + 同目录 config.local.yaml overlay（浅递归合并，overlay 胜）。

    只为取 feature flag / db_path 几个键——不动 ConfigManager（async、重依赖），
    读不了一律回空 dict（CLI 用默认路径继续，不因配置解析挂掉）。
    """
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


def _flag_enabled(cfg: Dict[str, Any]) -> bool:
    node = ((cfg.get("contacts") or {}).get("identity_shadow") or {})
    return bool(node.get("enabled", False)) if isinstance(node, dict) else False


def _resolve_inbox_db(cfg: Dict[str, Any], cfg_dir: Path) -> Path:
    """镜像 bootstrap/web_app.py 的解析：inbox.db_path（相对则挂 cfg_dir）→ 缺省 inbox.db。"""
    raw = str(((cfg.get("inbox") or {}).get("db_path")) or "").strip()
    p = Path(raw) if raw else (cfg_dir / "inbox.db")
    return p if p.is_absolute() else (cfg_dir / p)


def _resolve_identity_db(cfg: Dict[str, Any], cfg_dir: Path) -> Path:
    """镜像 skill_manager 的解析：memory.db_path → 缺省 bot.db（CPI 同库）。"""
    raw = str(((cfg.get("memory") or {}).get("db_path")) or "").strip()
    p = Path(raw) if raw else (cfg_dir / "bot.db")
    return p if p.is_absolute() else (cfg_dir / p)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="identity_shadow_scan",
        description="跨平台身份影子扫描（只读、只报告，绝不写关联）",
    )
    ap.add_argument("--inbox-db", default="", help="inbox.db 路径（默认按 config 解析）")
    ap.add_argument("--identity-db", default="",
                    help="bot.db 路径（user_identity_map 所在库；默认按 config 解析）")
    ap.add_argument("--config", default=str(_DEFAULT_CONFIG),
                    help="config.yaml 路径（读 feature flag 与默认库路径）")
    ap.add_argument("--limit", type=int, default=5000,
                    help="最多扫描的会话数（last_ts 降序取最近，默认 5000）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--force", action="store_true",
                    help="无视 contacts.identity_shadow.enabled 开关强制跑（手动评估用）")
    args = ap.parse_args(argv)

    # Windows 控制台 GBK 防线（验收要求 PYTHONIOENCODING=utf-8，这里再兜一层）
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    config_path = Path(args.config)
    cfg = _load_cfg(config_path)
    if not args.force and not _flag_enabled(cfg):
        msg = ("影子扫描未启用（contacts.identity_shadow.enabled=false，新子系统默认关）；"
               "手动评估请加 --force。")
        if args.json:
            print(json.dumps({"ok": False, "error": "disabled", "hint": msg},
                             ensure_ascii=False))
        else:
            print(msg)
        return 2

    cfg_dir = config_path.parent
    inbox_db = Path(args.inbox_db) if args.inbox_db else _resolve_inbox_db(cfg, cfg_dir)
    identity_db = (Path(args.identity_db) if args.identity_db
                   else _resolve_identity_db(cfg, cfg_dir))

    report = run_shadow_scan(inbox_db, identity_db, limit=args.limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
