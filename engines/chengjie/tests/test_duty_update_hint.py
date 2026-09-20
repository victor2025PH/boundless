# -*- coding: utf-8 -*-
"""update_hint 读写 CLI（tools/duty_update_hint.py）门禁（实施81 P2-3）。

核心不变量：写 overlay 必须**保注释**（ruamel round-trip）——2026-08-01
yaml.dump 剃光 30 行运维注释的事故不许在这个工具身上重演；ruamel 路径
不满足时拒写（返回 False），绝不降级整文件重写。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_update_hint import apply_hint, overlay_path, read_hint  # noqa: E402


def _mk_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(
        "bug_intake:\n  enabled: true\n", encoding="utf-8")
    (root / "config" / "config.local.yaml").write_text(
        "# 运维注释：这行绝不能被写入操作剃掉\n"
        "bug_intake:\n"
        "  groups: ['-100123']   # 行尾注释也要活着\n",
        encoding="utf-8")
    return root


def test_apply_preserves_comments_and_read_back(tmp_path):
    root = _mk_root(tmp_path)
    ok, note = apply_hint(root, "  已推送热补丁，重启智聊即生效  ")
    assert ok, note
    raw = overlay_path(root).read_text(encoding="utf-8")
    assert "运维注释：这行绝不能被写入操作剃掉" in raw, "写入把注释剃了"
    assert "行尾注释也要活着" in raw
    assert "已推送热补丁，重启智聊即生效" in raw
    # 合并视图读回 == 引擎将读到的值（前后空白已剥）
    assert read_hint(root) == "已推送热补丁，重启智聊即生效"


def test_apply_truncates_and_clear(tmp_path):
    root = _mk_root(tmp_path)
    ok, _ = apply_hint(root, "x" * 999)
    assert ok and len(read_hint(root)) == 200
    ok2, _ = apply_hint(root, "")
    assert ok2 and read_hint(root) == ""


def test_read_hint_missing_root_is_empty(tmp_path):
    assert read_hint(tmp_path / "nope") == ""
