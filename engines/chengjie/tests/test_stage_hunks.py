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


def test_preview_clean_retries_and_reports_honestly(tmp_path):
    """清理必须以磁盘实况为判据，且真能删掉带只读文件的树。

    首版无条件打印「已清理」，而刚跑过 pytest 的预览树被 __pycache__ 占用其实没删掉
    （2026-08-27 实锤）。谎报比不清理更坏——它让人以为干净了。
    """
    m = _mod()
    tree = tmp_path / "wt"
    (tree / "engines" / "chengjie" / "__pycache__").mkdir(parents=True)
    f = tree / "engines" / "chengjie" / "__pycache__" / "x.pyc"
    f.write_bytes(b"\x00")
    f.chmod(0o444)                                   # 只读也要能删掉
    assert m.remove_tree_with_retry(tree, attempts=3, sleep_sec=0.01) is True
    assert not tree.exists()
    # 不存在的目录视为已清理（幂等，重跑不报错）
    assert m.remove_tree_with_retry(tmp_path / "nope") is True

    src = _TOOL.read_text(encoding="utf-8")
    i = src.index("def do_preview_clean(")
    seg = src[i:src.index("def main(")]
    assert "target.exists()" in seg                  # 以实况为判据
    assert "return 1" in seg                         # 没清干净必须非零退出


def _scratch_repo(repo: Path):
    """全隔离的临时 git 仓（绝不碰真仓库）：一个已提交文件 + 一处改动 + 一个未跟踪文件。"""
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)

    def g(*a):
        return subprocess.run(["git", *a], cwd=str(repo), capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    g("add", "tracked.txt")
    g("commit", "-qm", "base")
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")     # 已跟踪改动
    (repo / "loose.txt").write_text("untracked\n", encoding="utf-8")  # 未跟踪
    return g


def test_snapshot_is_side_effect_free(tmp_path, monkeypatch):
    """快照必须**零副作用**：HEAD / index / 工作树前后完全不变。

    2026-08-27 事故：为了覆盖未跟踪文件走了 write-tree + commit-tree，结果分支 HEAD
    被推到了那个「快照」commit 上。本测试在**隔离空仓**里真跑一遍快照路径——这正是
    当天缺失的那道防线（当时只在真仓库上边做边看）。
    """
    m = _mod()
    repo = tmp_path / "repo"
    g = _scratch_repo(repo)
    monkeypatch.setattr(m, "REPO_ROOT", repo)

    head0 = g("rev-parse", "HEAD").stdout.decode().strip()
    # zip 必须落在仓库**之外**：写进仓内它自己就变成一个未跟踪文件，
    # 快照工具会把自己打包进去（首版测试就这么绊了一跤）
    zip_path = tmp_path / "out" / "untracked.zip"
    assert m.do_snapshot("snap-test", str(zip_path)) == 0

    # ① HEAD 一动不动（事故当天正是这里出的问题）
    assert g("rev-parse", "HEAD").stdout.decode().strip() == head0
    # ② index 干净、工作树改动仍在
    assert g("diff", "--cached", "--name-only").stdout.decode().strip() == ""
    assert "tracked.txt" in g("diff", "--name-only").stdout.decode()
    # ③ tag 指向一个真的承载了改动的游离 commit
    sha = g("rev-parse", "snap-test").stdout.decode().strip()
    assert sha and sha != head0
    assert "tracked.txt" in g("diff", "--name-only", "HEAD", sha).stdout.decode()
    # ④ 未跟踪文件进了 zip（走文件系统，不进 git）
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        assert "loose.txt" in zf.namelist()
    assert g("ls-files", "--others", "--exclude-standard").stdout.decode().strip() \
        == "loose.txt", "未跟踪文件不该被 git 收编"


def test_snapshot_path_never_creates_a_commit():
    """源码级红线：快照路径不得出现 commit-tree / write-tree。

    这条是纯纪律钉子——当天就是这两个命令把「只读快照」变成了分支上的真提交。
    未跟踪文件一律走 zip，绝不进 git 对象库。
    """
    src = _TOOL.read_text(encoding="utf-8")
    i = src.index("def do_snapshot(")
    seg = src[i:src.index("def _staged_any(")]
    # 只查**调用形式**（带引号的实参）——注释里必须能写出这两个词来说明为什么禁用它们
    assert '"commit-tree"' not in seg and '"write-tree"' not in seg
    assert '"stash", "create"' in seg          # 唯一许可路径
    assert "zipfile" in seg                    # 未跟踪走文件打包
    assert "repo_state()" in seg               # 前后比对零副作用


def test_refuses_when_index_holds_foreign_staged_files():
    """发现外来暂存必须**拒绝**而不只是警告（2026-08-27 升级）。

    警告不够：`git commit` 提交的是整个 index，只要有一个外来文件在里面，一次手滑就
    把别人未完成的活连带交掉。当天 index 被别的线占了 40+ 分钟，全靠人克制才没出事。
    豁免要显式（--allow-foreign-staged），且必须在 index 未改动的前提下退出。
    """
    src = _TOOL.read_text(encoding="utf-8")
    i = src.index("if args.apply:")
    seg = src[i:i + 1600]
    assert "_staged_others" in seg
    assert "not args.allow_foreign_staged" in seg
    assert "return 1" in seg, "必须以非零退出，且不得继续去写 index"
    # 拒绝分支里不能有任何写 index 的动作（do_file 在其后才被调用）
    assert "do_file(" not in seg.split("return 1")[0]
    assert "--allow-foreign-staged" in src, "必须留显式豁免出口"


def test_index_discipline_documented():
    """index 是共享单一资源这条纪律必须写在工具文档里——机制拦得住手滑，
    拦不住「占着不放」，那要靠人知道规矩。"""
    src = _TOOL.read_text(encoding="utf-8")
    assert "index 使用纪律" in src
    assert "git reset" in src and "共享" in src


def test_warns_about_foreign_staged_files():
    """index 里有别人的暂存内容要提醒：sibling 一句 git commit -a 会连带交掉
    （AGENTS.md 记录过该事故）。"""
    src = _TOOL.read_text(encoding="utf-8")
    assert "_staged_others" in src
    assert "commit -a" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
