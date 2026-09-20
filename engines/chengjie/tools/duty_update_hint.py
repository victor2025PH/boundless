# -*- coding: utf-8 -*-
"""修复回访「获取方式」提示（bug_intake.update_hint）读写 CLI（实施81 P2-3）。

update_hint 决定回访文案的「📦 获取方式」段（如「已推送热补丁，重启智聊即
生效」）。此前只能手编 overlay——本工具把它变成一条命令，并接进 hotpatch
推送脚本（``push_chatx_hotpatch.ps1 -UpdateHint "..."``），发版顺手更新：

    python tools/duty_update_hint.py                # 查看当前值（只读）
    python tools/duty_update_hint.py --set "已推送热补丁，重启智聊即生效"
    python tools/duty_update_hint.py --clear

写入走 ``set_yaml_key_preserving``（ruamel round-trip，**保 overlay 全部
运维注释**——2026-08-01 有过 yaml.dump 剃光 30 行注释的实锤事故）；ruamel
不可用时**拒绝写入**而非降级整写（宁可让操作员手编，不当注释粉碎机）。
写入后引擎 ~30s 热重载生效（check_and_hot_reload），免重启。

⚠ ``--set``/``--clear`` 改的是**生产 overlay**——属运营动作，由操作员显式
执行（hotpatch 脚本也只在显式传参时才调用）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


def overlay_path(data_root: Path) -> Path:
    return Path(data_root) / "config" / "config.local.yaml"


def read_hint(data_root: Path) -> str:
    """合并视图（base+overlay）里的当前值——引擎真正读到的那份。"""
    try:
        cfg = load_merged_config(data_root)
        return str(((cfg.get("bug_intake") or {}).get("update_hint")) or "").strip()
    except Exception:
        return ""


def apply_hint(data_root: Path, value: str) -> tuple:
    """写 overlay（保注释）。返回 (ok, note)。ruamel 缺席/失败＝拒写不降级。"""
    from src.utils.config_manager import set_yaml_key_preserving
    p = overlay_path(data_root)
    text = str(value or "").strip()[:200]
    if set_yaml_key_preserving(p, ["bug_intake", "update_hint"], text):
        return True, str(p)
    return False, ("ruamel 写入失败（保注释前提不满足）——请手工编辑 "
                   f"{p} 的 bug_intake.update_hint，勿用整文件重写工具")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="回访「获取方式」提示读写")
    ap.add_argument("--set", dest="set_value", default=None,
                    help="写入新提示（≤200 字；改生产 overlay，热重载生效）")
    ap.add_argument("--clear", action="store_true",
                    help="清空（回访不再带更新段）")
    ap.add_argument("--data-root", default="")
    args = ap.parse_args()

    data_root = resolve_data_roots(args.data_root)[0]
    cur = read_hint(data_root)
    _out(f"[now] bug_intake.update_hint = {cur!r}（{data_root}）")
    if args.set_value is None and not args.clear:
        return 0
    new_val = "" if args.clear else str(args.set_value)
    ok, note = apply_hint(data_root, new_val)
    if not ok:
        _out(f"[err] {note}")
        return 1
    _out(f"[ok] 已写入 {note}：{new_val!r}（引擎 ~30s 热重载生效，免重启）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
