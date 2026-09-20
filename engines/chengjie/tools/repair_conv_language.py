# -*- coding: utf-8 -*-
"""修复被出站镜像污染的会话「客户语言」列（2026-08-15，一次性数据修复）。

事故：ingest 的 ``_conv_from_chat`` 曾不分方向直通末条消息语言 → AI 的英文
译文镜像把中文客户的 ``conversations.language`` 反复钉成 en，出站翻译于是永远
瞄准英文（自锁）。写入侧已修（方向卫生 + burst 层），本工具只管**存量**行：
按「入站消息证据」重算每个会话的客户语言，与列值不符则修正。

口径与线上判定同源（勿另算一套）：
  1. ``current_inbound_burst_lang``（末尾入站段强证据）优先；
  2. 无 burst → ``vote_language``（入站加权多数决）；
  3. 两者皆无证据 → 不动（宁可保留旧值也不瞎改）。

用法（默认 dry-run 只报告，绝不写库）：
    python tools/repair_conv_language.py [--data-root PATH] [--platform messenger]
    python tools/repair_conv_language.py --apply        # 真写

dry-run 用只读连接（mode=ro URI）对活体生产库零写风险；--apply 走短事务 +
busy_timeout，且 UPDATE 带旧值守卫（服务并发改过就跳过，不盲覆盖）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.inbox.outbound_translate import (  # noqa: E402
    current_inbound_burst_lang, normalize_target, vote_language,
)

try:  # Windows 控制台 GBK 防乱码（阿拉伯文/泰文样本打印会炸）
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass


def _inbox_db_path(root: Path) -> Path:
    cfg = load_merged_config(root)
    raw = str((((cfg.get("inbox") or {}) if isinstance(cfg, dict) else {})
               ).get("db_path") or "")
    cfg_dir = root / "config"
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (cfg_dir / p)
    return cfg_dir / "inbox.db"


def _evidence_lang(msgs, detect) -> str:
    burst = current_inbound_burst_lang(msgs)
    if burst:
        return burst
    try:
        return vote_language(msgs, detect=detect) or ""
    except Exception:
        return ""


def repair_root(root: Path, *, apply: bool, platform: str, limit_msgs: int) -> int:
    db = _inbox_db_path(root)
    if not db.is_file():
        print(f"[skip] {root} 无 inbox 库（{db}）")
        return 0
    from src.ai.translation_service import detect_language

    ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    where = "WHERE platform = ?" if platform else ""
    args = (platform,) if platform else ()
    convs = ro.execute(
        f"SELECT conversation_id, platform, display_name, language "
        f"FROM conversations {where}", args).fetchall()

    fixes = []  # (conversation_id, old, new, name)
    for c in convs:
        cid = c["conversation_id"]
        rows = ro.execute(
            "SELECT direction, text FROM messages WHERE conversation_id = ? "
            "ORDER BY ts DESC LIMIT ?", (cid, limit_msgs)).fetchall()
        msgs = [dict(r) for r in reversed(rows)]
        ev = _evidence_lang(msgs, detect_language)
        old = normalize_target(c["language"])
        if ev and ev != old:
            fixes.append((cid, c["language"], ev, c["display_name"]))
    ro.close()

    print(f"[{root}] 会话 {len(convs)} 条，需修正 {len(fixes)} 条"
          f"（{'APPLY' if apply else 'dry-run'}）")
    for cid, old, new, name in fixes:
        print(f"  {cid}  {old or '?'} -> {new}  ({name})")

    if not apply or not fixes:
        return len(fixes)

    rw = sqlite3.connect(str(db), timeout=15)
    rw.execute("PRAGMA busy_timeout = 15000")
    changed = 0
    with rw:
        for cid, old, new, _name in fixes:
            cur = rw.execute(
                "UPDATE conversations SET language = ? "
                "WHERE conversation_id = ? AND language = ?",
                (new, cid, old))
            changed += cur.rowcount
    rw.close()
    print(f"  已写入 {changed}/{len(fixes)} 条（差额=服务并发已改，跳过）")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="", help="显式数据根（缺省自动发现活跃实例）")
    ap.add_argument("--platform", default="", help="只处理某平台（如 messenger）")
    ap.add_argument("--limit-msgs", type=int, default=30, help="每会话取证消息窗口")
    ap.add_argument("--apply", action="store_true", help="真写库（缺省 dry-run）")
    args = ap.parse_args()
    total = 0
    for root in resolve_data_roots(args.data_root):
        total += repair_root(Path(root), apply=args.apply,
                             platform=args.platform.strip().lower(),
                             limit_msgs=max(5, args.limit_msgs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
