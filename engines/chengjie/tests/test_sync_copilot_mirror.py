# -*- coding: utf-8 -*-
"""镜像同步工具门禁（scripts/sync_copilot_mirror.py）。

工具的存在理由=把「人肉拷贝双树」这个既产漂移又出过编码事故的动作变成安全命令；
本门禁钉四件事：计划正确（新增/变更/多余三类）、幂等（同步后再算计划=空）、
字节级一致（与 test_copilot_shared_sync 同判据）、**源树损毁必须拒绝同步**
（2026-08-22 乱码镜像差点随打包出货——扩散防线是本工具的核心价值）。
"""
from __future__ import annotations

from pathlib import Path

from scripts.sync_copilot_mirror import (
    apply_sync,
    plan_sync,
    source_encoding_problems,
)


def _mk(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")


def test_plan_covers_new_changed_extra(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _mk(src, {"a.js": "console.log(1);", "sub/b.css": ".x{color:red}", "new.js": "// 新增"})
    _mk(dst, {"a.js": "console.log(1);", "sub/b.css": ".x{color:blue}", "stale.js": "// 镜像多余"})
    plan = plan_sync(src, dst)
    assert ("copy", "new.js") in plan, "新增文件必须拷贝"
    assert ("copy", "sub/b.css") in plan, "内容变更必须拷贝"
    assert ("delete", "stale.js") in plan, "镜像多余文件必须删除（文件集合一致是门禁语义）"
    assert ("copy", "a.js") not in plan, "字节相同不许重拷（幂等基础）"


def test_apply_then_plan_is_empty(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _mk(src, {"a.js": "中文注释：会话列表。", "i18n/cp-i18n.js": "var L={'zh':'知识库'};"})
    _mk(dst, {"old.js": "x"})
    apply_sync(src, dst, plan_sync(src, dst))
    assert plan_sync(src, dst) == [], "同步后必须幂等（再算计划=空）"
    # 字节级一致（与 test_copilot_shared_sync 同判据）
    for rel in ("a.js", "i18n/cp-i18n.js"):
        assert (src / rel).read_bytes() == (dst / rel).read_bytes()
    assert not (dst / "old.js").exists()


def test_mojibake_source_is_detected(tmp_path):
    src = tmp_path / "src"
    healthy = "// 收件箱主脚本：会话列表按最近活动排序，未读优先。需人工置顶。\n"
    corrupted = healthy.encode("utf-8").decode("gb18030", errors="replace")
    _mk(src, {"bad.js": corrupted, "good.js": healthy})
    problems = source_encoding_problems(src)
    assert any("bad.js" in x for x in problems), "损毁源必须被体检抓到（扩散防线）"
    assert not any("good.js" in x for x in problems), "健康中文被误判＝阈值过紧"


def test_binary_assets_pass_through(tmp_path):
    """非文本扩展名（字体/图片）不做编码体检、按字节同步。"""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _mk(src, {"font.woff2": b"\x00\x01\xfe\xffBINARY", "a.js": "ok()"})
    assert source_encoding_problems(src) == []
    apply_sync(src, dst, plan_sync(src, dst))
    assert (dst / "font.woff2").read_bytes() == b"\x00\x01\xfe\xffBINARY"
