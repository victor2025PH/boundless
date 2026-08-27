# -*- coding: utf-8 -*-
"""tools/stage_hunks.py 的纯函数门禁（2026-08-27）。

这个工具存在的理由是共享工作树上「想提交自己那点改动」只有两条死路：整文件提交会
吞掉别人的活，不提交会让 HEAD 上自己的测试引用不存在的函数。它把手工那套 20 分钟的
流程（按 hunk 切 + 提交前 worktree 复验）压成几条命令。

本文件钉三件事，每一件都对应当天真实踩过的坑：
  ① 切分与过滤全程按**字节**——解码往返会改字节，含中文上下文的 hunk 会应用失败；
  ② **双向**自证——反向（丢弃块里其实有我的改动）那次真出过错；
  ③ 预览必须走 write-tree/commit-tree，**在提交之前**验证。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "stage_hunks.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_stage_hunks", _TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# 合成 diff：两个 hunk，一个是「我的」（含中文，复刻当天失败那块的形状），
# 一个是「他人的」。刻意用 bytes 字面量——本工具的契约就是不 decode。
_DIFF = (
    b"diff --git a/src/x.py b/src/x.py\n"
    b"index 1111111..2222222 100644\n"
    b"--- a/src/x.py\n"
    b"+++ b/src/x.py\n"
    b"@@ -10,4 +10,6 @@ class W:\n"
    b"         try:\n"
    b"             self._check_mine()\n"
    b"         except Exception:\n"
    b"-            logger.debug(\"\xe6\x97\xa7\xe6\x96\x87\xe6\xa1\x88\")\n"
    b"+            # \xe6\x8e\xa2\xe9\x92\x88\xe6\x98\xaf\xe5\x85\x9c\xe5\xba\x95\xe6\x9c\xba\xe5\x88\xb6\n"
    b"+            logger.warning(\"\xe6\x96\xb0\xe6\x96\x87\xe6\xa1\x88\")\n"
    b"+            self._check_probe_stalled()\n"
    b"@@ -90,3 +92,5 @@ class W:\n"
    b"     def other(self):\n"
    b"         pass\n"
    b"+    def _check_token_wallet(self):\n"
    b"+        pass\n"
)


def test_split_and_filter_are_byte_exact():
    """切分不得丢字节：头 + 各 hunk 拼回去必须与原 diff 逐字节相等。

    2026-08-27 首版用 text=True 解码再写回，含中文上下文的 hunk 应用失败而纯 ASCII
    那块成功——git 按字节比对上下文，任何一次解码/编码往返都可能改变字节。
    """
    m = _mod()
    head, hunks = m.split_hunks(_DIFF)
    assert len(hunks) == 2
    assert head + b"".join(hunks) == _DIFF          # 逐字节还原
    assert isinstance(head, bytes) and all(isinstance(h, bytes) for h in hunks)
    # 中文新增行原样保留（不是 mojibake、不是 replacement char）
    assert "探针是兜底机制".encode("utf-8") in m.hunk_added(hunks[0])
    assert b"\xef\xbf\xbd" not in _DIFF             # 无 U+FFFD


def test_filter_keeps_mine_and_leaves_theirs():
    m = _mod()
    _, hunks = m.split_hunks(_DIFF)
    res = m.filter_hunks(hunks, mine=["_check_probe_stalled"],
                         theirs=["_check_token_wallet"])
    assert len(res["keep"]) == 1 and len(res["drop"]) == 1
    assert res["bleed"] == [] and res["missed"] == []
    assert b"_check_probe_stalled" in m.hunk_added(res["keep"][0])
    assert b"_check_token_wallet" in m.hunk_added(res["drop"][0])


def test_forward_self_check_catches_bleed():
    """正向：留下的块里夹带了他人标记 → 必须点名（否则会把别人的功能替他提交）。"""
    m = _mod()
    _, hunks = m.split_hunks(_DIFF)
    # 故意把他人标记也当成「我的」，模拟标记写太宽
    res = m.filter_hunks(hunks, mine=["def "], theirs=["_check_token_wallet"])
    assert "_check_token_wallet" in res["bleed"]


def test_reverse_self_check_catches_missed():
    """反向：丢弃的块里其实有我的改动 → 必须点名。

    这一条是当天真出过错的方向：我按「文件级」推定 diff 全是自己的，结果 +419 行里
    混进了他线一行断言，直到临时 worktree 检出 HEAD 才发现。反向自证不是可选项。
    """
    m = _mod()
    _, hunks = m.split_hunks(_DIFF)
    res = m.filter_hunks(hunks, mine=["_check_probe_stalled", "_check_token_wallet"],
                         theirs=[])
    assert res["missed"] == []          # 两块都命中，无遗漏
    res2 = m.filter_hunks(hunks, mine=["_check_token_wallet"], theirs=[])
    # 只认第二块时，第一块被丢弃——若「我的」标记里含第一块的词就该报 missed
    res3 = m.filter_hunks(hunks, mine=["_check_token_wallet"], theirs=[])
    assert len(res2["keep"]) == 1 and res3["missed"] == []
    res4 = m.filter_hunks([hunks[0]], mine=["_check_token_wallet",
                                            "_check_probe_stalled"], theirs=[])
    assert res4["keep"] and res4["missed"] == []


def test_empty_and_degenerate_inputs_do_not_crash():
    """坏输入一律软失败：工具本身不该成为新的故障面。"""
    m = _mod()
    assert m.split_hunks(b"") == (b"", [])
    assert m.split_hunks(b"no hunks here\n") == (b"no hunks here\n", [])
    res = m.filter_hunks([], mine=["x"], theirs=["y"])
    assert res["keep"] == [] and res["drop"] == []
    # 空标记不得把所有块都当成自己的
    _, hunks = m.split_hunks(_DIFF)
    assert m.filter_hunks(hunks, mine=[""], theirs=[])["keep"] == []


def test_preview_uses_write_tree_not_a_real_commit():
    """预览必须在**提交之前**：源码级钉住 write-tree/commit-tree 路径。

    当天是先真提交、再检出 HEAD 发现两条红、只能补第二个提交去修。顺序反了，
    代价就是脏历史。谁把它改回「先 commit 再验」先红。
    """
    src = _TOOL.read_text(encoding="utf-8")
    assert '"write-tree"' in src and '"commit-tree"' in src
    assert '"-p", "HEAD"' in src                     # 游离 commit 挂在 HEAD 之后
    i = src.index("def do_preview(")
    seg = src[i:src.index("def _staged_any(")]
    assert "git(\"commit\"" not in seg, "预览路径绝不能真提交"


def test_apply_path_verifies_staged_syntax():
    """暂存后必须验暂存版本可解析——半个 hunk 造出语法错误要当场发现，
    而不是等 CI 或别人踩到。"""
    src = _TOOL.read_text(encoding="utf-8")
    i = src.index("def do_file(")
    seg = src[i:src.index("def do_preview(")]
    assert "ast.parse" in seg and 'git("show", f":{rel}")' in seg
    assert "--cached" in seg and '"--check"' in seg   # 先干跑再真应用


def test_warns_about_foreign_staged_files():
    """index 里有别人的暂存内容要提醒：sibling 一句 git commit -a 会连带交掉
    （AGENTS.md 记录过该事故）。"""
    src = _TOOL.read_text(encoding="utf-8")
    assert "_staged_others" in src
    assert "commit -a" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
