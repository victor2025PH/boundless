"""refresh_backend_datas 增量刷新门禁 —— 「仅刷数据资产」的诚实性契约。

背景（2026-08-18 1.0.41 发版实录）：predist freshness 把任何树漂移都打回 ~7.5 分钟
全量 PyInstaller，而共享树 sibling 高频保存的多是 templates/static（包内 datas，
运行时按路径读文件）。增量档省时间的前提是绝不产出「stamp 说新、exe 是旧」的包：
  · 编进 exe 的 .py 变了 → 必须拒绝（exit 3）
  · stamp 无逐文件明细（旧格式）→ 必须拒绝（exit 4）
  · 数据资产变更 → 刷新后 verify_stamp 必须真变绿
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
_BUILD = _ENGINE / "desktop" / "build"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fp_mod():
    return _load("_rbd_fp", _BUILD / "backend_source_fingerprint.py")


@pytest.fixture(scope="module")
def rd_mod():
    return _load("_rbd_refresh", _BUILD / "refresh_backend_datas.py")


# ---------------------------------------------------------------- 纯函数层


def test_diff_detects_changed_added_removed(rd_mod):
    stamped = {"roots": [{"label": "src", "digest": "old",
                          "files": {"a.py": "1", "b.py": "2", "gone.py": "3"}}]}
    current = {"roots": [{"label": "src", "digest": "new",
                          "files": {"a.py": "1", "b.py": "22", "new.py": "4"}}]}
    changes, no_detail = rd_mod.diff_stamp_files(stamped, current)
    assert no_detail == []
    kinds = {(rel, kind) for _, rel, kind in changes}
    assert kinds == {("b.py", "changed"), ("new.py", "added"), ("gone.py", "removed")}


def test_diff_flags_old_stamp_without_detail(rd_mod):
    stamped = {"roots": [{"label": "src", "digest": "old"}]}  # 无 files 明细
    current = {"roots": [{"label": "src", "digest": "new", "files": {"a.py": "1"}}]}
    _, no_detail = rd_mod.diff_stamp_files(stamped, current)
    assert no_detail == ["src"]


def test_diff_same_digest_skips_root(rd_mod):
    stamped = {"roots": [{"label": "src", "digest": "same"}]}  # 无明细但根本没变
    current = {"roots": [{"label": "src", "digest": "same", "files": {"a.py": "1"}}]}
    changes, no_detail = rd_mod.diff_stamp_files(stamped, current)
    assert changes == [] and no_detail == []


def test_classify_datas_labels_are_safe(rd_mod):
    changes = [("src/web/templates", "a.html", "changed"),
               ("shared/copilot", "cp.js", "changed"),
               ("config/profiles", "cloud_light.yaml", "changed"),
               ("domains", "x/manifest.yaml", "added"),
               ("platform/credpool", "client.py", "changed")]
    zones, unsafe = rd_mod.classify_changes(changes)
    assert unsafe == []
    assert zones == {"src/web/templates", "shared/copilot", "config/profiles",
                     "domains", "platform/credpool"}


def test_classify_src_root_splits_datas_vs_code(rd_mod):
    changes = [("src", "web/templates/unified_inbox.html", "changed"),
               ("src", "web/static/workspace/ui-build.txt", "changed"),
               ("src", "skills/skill_manager.py", "changed")]
    zones, unsafe = rd_mod.classify_changes(changes)
    assert zones == {"src/web/templates", "src/web/static"}
    assert unsafe == [("src", "skills/skill_manager.py", "changed")]


def test_classify_i18n_pack_word_files_safe_via_both_roots(rd_mod):
    """词条 pack .py 变更：src 根与专属根两路都必须归并到同一 zone（双根重复计入）。"""
    changes = [("src", "web/i18n_packs/ops_overview_page.py", "changed"),
               ("src/web/i18n_packs", "ops_overview_page.py", "changed"),
               ("src/web/i18n_packs", "new_domain_page.py", "added")]
    zones, unsafe = rd_mod.classify_changes(changes)
    assert unsafe == []
    assert zones == {"src/web/i18n_packs"}


def test_classify_i18n_init_and_underscore_files_unsafe(rd_mod):
    """__init__.py（frozen 文件优先逻辑的宿主，走 PYZ）变更必须判不安全 → 全量重打。

    这条守卫同时保证「旧安装包 + 新刷新脚本」组合安全：文件优先逻辑落地那次
    __init__.py 必然变哈希 → 增量被拒 → 强制全量，无需额外版本闸。
    """
    for label, rel in [("src", "web/i18n_packs/__init__.py"),
                       ("src/web/i18n_packs", "__init__.py"),
                       ("src", "web/i18n_packs/_helper.py"),
                       ("src/web/i18n_packs", "_helper.py"),
                       ("src/web/i18n_packs", "sub/pack.py")]:  # 子目录（当前无）保守拒
        zones, unsafe = rd_mod.classify_changes([(label, rel, "changed")])
        assert zones == set(), (label, rel)
        assert unsafe == [(label, rel, "changed")], (label, rel)


def test_classify_main_py_and_unknown_labels_unsafe(rd_mod):
    zones, unsafe = rd_mod.classify_changes(
        [("main.py", "main.py", "changed"), ("mystery", "x", "added")])
    assert zones == set()
    assert len(unsafe) == 2


# ---------------------------------------------------------------- 端到端（合成仓库）


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "web" / "templates").mkdir(parents=True)
    (repo / "src" / "web" / "static").mkdir(parents=True)
    (repo / "src" / "web" / "i18n_packs").mkdir(parents=True)
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v1</p>", encoding="utf-8")
    (repo / "src" / "web" / "static" / "s.css").write_text("body{}", encoding="utf-8")
    (repo / "src" / "web" / "i18n_packs" / "__init__.py").write_text(
        "PACKS = 'v1'\n", encoding="utf-8")
    (repo / "src" / "web" / "i18n_packs" / "demo_page.py").write_text(
        "ZH = {'k': '一'}\nEN = {'k': 'one'}\n", encoding="utf-8")
    return repo


def _make_dist(tmp_path: Path, repo: Path, fp_mod, *, detail: bool = True) -> Path:
    out = tmp_path / "backend-dist"
    internal = out / "_internal"
    exe = "backend.exe" if sys.platform == "win32" else "backend"
    for rel in ("src/web/templates", "src/web/static", "src/web/i18n_packs"):
        dest = internal / rel
        shutil.copytree(repo / rel, dest)
    (out / exe).write_bytes(b"MZ fake")
    fp = fp_mod.compute_fingerprint(repo, detail=detail)
    fp_mod.write_stamp(out, fp)
    return out


def _fake_bb(repo: Path, tmp_path: Path):
    """最小 build_backend 替身：static 暂存＝直拷（真实现的剔除断言另有归属）。

    i18n 暂存复刻真实语义（剔 __pycache__ + 逐文件 compile 自检）——e2e 要验
    「坏词表文件在刷新期被拦」这条诚实契约。
    """
    def _stage_static():
        staged = tmp_path / "static-staged"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(repo / "src" / "web" / "static", staged)
        return staged

    def _stage_i18n_packs():
        staged = tmp_path / "i18n-packs-staged"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(repo / "src" / "web" / "i18n_packs", staged,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for p in sorted(staged.glob("*.py")):
            compile(p.read_text(encoding="utf-8-sig"), str(p), "exec")
        return staged

    return SimpleNamespace(_stage_static=_stage_static,
                           _stage_i18n_packs=_stage_i18n_packs,
                           _stage_domains=lambda: (_ for _ in ()).throw(AssertionError),
                           _stage_platform_pkg=lambda pkg: None)


def test_e2e_template_change_refreshes_and_gate_goes_green(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")

    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 0
    shipped = (out / "_internal" / "src" / "web" / "templates" / "a.html").read_text(encoding="utf-8")
    assert shipped == "<p>v2</p>"
    ok, reason, _, _ = fp_mod.verify_stamp(repo, out)
    assert ok, reason


def test_e2e_static_change_goes_through_staging(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    (repo / "src" / "web" / "static" / "s.css").write_text("body{color:red}", encoding="utf-8")

    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 0
    shipped = (out / "_internal" / "src" / "web" / "static" / "s.css").read_text(encoding="utf-8")
    assert shipped == "body{color:red}"
    ok, _, _, _ = fp_mod.verify_stamp(repo, out)
    assert ok


def test_e2e_i18n_pack_change_refreshes_and_gate_goes_green(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    (repo / "src" / "web" / "i18n_packs" / "demo_page.py").write_text(
        "ZH = {'k': '二'}\nEN = {'k': 'two'}\n", encoding="utf-8")

    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 0
    shipped = (out / "_internal" / "src" / "web" / "i18n_packs" / "demo_page.py").read_text(encoding="utf-8")
    assert "'二'" in shipped
    ok, reason, _, _ = fp_mod.verify_stamp(repo, out)
    assert ok, reason


def test_e2e_i18n_init_change_refuses(rd_mod, fp_mod, tmp_path):
    """__init__.py 属 PYZ 逻辑：改它绝不允许「刷数据装新鲜」。"""
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    (repo / "src" / "web" / "i18n_packs" / "__init__.py").write_text(
        "PACKS = 'v2'\n", encoding="utf-8")
    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 3
    ok, _, _, _ = fp_mod.verify_stamp(repo, out)
    assert not ok  # 门禁保持红（诚实）


def test_e2e_broken_i18n_pack_blocks_refresh(rd_mod, fp_mod, tmp_path):
    """语法坏词表在暂存 compile 自检被拦——绝不把坏文件刷进包再指望运行时回落。"""
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    (repo / "src" / "web" / "i18n_packs" / "demo_page.py").write_text(
        "ZH = {'k': ", encoding="utf-8")  # 残缺（模拟半写保存）
    with pytest.raises(SyntaxError):
        rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    shipped = (out / "_internal" / "src" / "web" / "i18n_packs" / "demo_page.py").read_text(encoding="utf-8")
    assert "'一'" in shipped  # 产物未被污染


def test_e2e_py_change_refuses_and_leaves_dist_untouched(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")

    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 3  # 代码变更混入 → 整体拒绝，绝不「刷模板装新鲜」
    shipped = (out / "_internal" / "src" / "web" / "templates" / "a.html").read_text(encoding="utf-8")
    assert shipped == "<p>v1</p>"  # 产物一字未动
    ok, _, _, _ = fp_mod.verify_stamp(repo, out)
    assert not ok  # 门禁保持红（诚实）


def test_e2e_old_stamp_without_detail_refuses(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod, detail=False)
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")
    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 4


def test_e2e_fresh_tree_upgrades_old_stamp_in_place(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod, detail=False)
    rc = rd_mod.run(repo, out, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 0
    stamped = fp_mod.load_stamp(out)
    assert all(isinstance(r.get("files"), dict) for r in stamped["roots"])


def test_e2e_dry_run_touches_nothing(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    old_stamp = (out / fp_mod.STAMP_NAME).read_text(encoding="utf-8")
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")

    rc = rd_mod.run(repo, out, dry_run=True, fp=fp_mod, bb=_fake_bb(repo, tmp_path))
    assert rc == 0
    shipped = (out / "_internal" / "src" / "web" / "templates" / "a.html").read_text(encoding="utf-8")
    assert shipped == "<p>v1</p>"
    assert (out / fp_mod.STAMP_NAME).read_text(encoding="utf-8") == old_stamp


# ------------------------------------------------- frozen 文件优先加载（P1 契约）


def test_frozen_collect_prefers_disk_file(monkeypatch, tmp_path):
    """frozen 态：磁盘数据文件存在即 exec 文件——增量刷新的词条改动才真生效。"""
    import src.web.i18n_packs as pkg
    d = tmp_path / "packs"
    d.mkdir()
    (d / "zz_probe_pack.py").write_text(
        "ZH = {'zz.probe': '磁盘版'}\nEN = {'zz.probe': 'from-disk'}\n",
        encoding="utf-8")
    monkeypatch.setattr(pkg, "_FROZEN", True)
    monkeypatch.setattr(pkg, "_PKG_DIR", d)
    zh, en, _ = pkg.collect_all()
    assert zh == {"zz.probe": "磁盘版"} and en == {"zz.probe": "from-disk"}
    # 新增文件（不在 PYZ）也必须被枚举到——pkgutil 口径会漏，glob 口径不会
    assert list(pkg.iter_pack_names()) == ["zz_probe_pack"]


def test_frozen_broken_disk_file_falls_back_to_import(monkeypatch, tmp_path):
    """磁盘文件坏（半写/损伤）→ 回落 import（PYZ 副本语义）：坏文件绝不放倒全站词表。"""
    import src.web.i18n_packs as pkg
    d = tmp_path / "packs"
    d.mkdir()
    # 与真实存在的模块同名，但磁盘内容残缺 → exec 失败 → importlib 拿到真模块
    (d / "audit_actions.py").write_text("ZH = {'x': ", encoding="utf-8")
    monkeypatch.setattr(pkg, "_FROZEN", True)
    monkeypatch.setattr(pkg, "_PKG_DIR", d)
    zh, en, _ = pkg.collect_all()
    assert zh and en  # 回落成功，词条来自真模块而非空


def test_dev_mode_path_unchanged(tmp_path):
    """非 frozen（dev/生产实例）：collect_all 走 importlib 旧路径，行为零回归。"""
    import src.web.i18n_packs as pkg
    assert pkg._FROZEN is False
    zh, en, _ = pkg.collect_all()
    assert len(zh) > 1000 and len(en) > 1000  # 95 packs 的真实词量级


def test_missing_dist_returns_2(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    rc = rd_mod.run(repo, tmp_path / "nope", fp=fp_mod)
    assert rc == 2


def test_i18n_packs_wired_into_build_and_fingerprint():
    """三处接线缺一即断链：staging 进 datas / STAGED_DESTS 口径 / 指纹根覆盖。"""
    bb_text = (_BUILD / "build_backend.py").read_text(encoding="utf-8")
    fp_text = (_BUILD / "backend_source_fingerprint.py").read_text(encoding="utf-8")
    assert "_stage_i18n_packs" in bb_text
    assert '"src/web/i18n_packs"' in bb_text   # STAGED_DESTS
    assert '"src/web/i18n_packs"' in fp_text   # 指纹根（stamp/verify 两路同源）


def test_build_backend_stamps_with_detail():
    """build_backend 必须以 detail=True 落 stamp，否则增量档永远 exit 4。"""
    text = (_BUILD / "build_backend.py").read_text(encoding="utf-8")
    assert "detail=True" in text


def test_npm_script_registered():
    import json
    pkg = json.loads((_ENGINE / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"].get("refresh:datas") == "python build/refresh_backend_datas.py"
    # 智能档：增量刷新失败/被拒（exit 2/3/4/5）自动落全量再打包
    # cmd 语义 (a || b) && c —— a 成功跳过 b 直接 c；a 失败跑 b，b 成功才 c
    smart = pkg["scripts"].get("dist:win:smart", "")
    assert smart.startswith("python build/refresh_backend_datas.py || npm run build:backend")
    assert smart.endswith("&& npm run dist:win")


def test_fingerprint_detail_does_not_change_aggregate(fp_mod, tmp_path):
    """detail 只是附加明细，聚合哈希必须与旧算法逐位一致（predist 两路口径不炸）。"""
    repo = _make_repo(tmp_path)
    plain = fp_mod.compute_fingerprint(repo)
    rich = fp_mod.compute_fingerprint(repo, detail=True)
    assert plain["aggregate"] == rich["aggregate"]
    assert "files" not in plain["roots"][0]
    assert isinstance(rich["roots"][0]["files"], dict)


# ------------------------------------------- 坐席差量推送判定（P2 --diff-stamps）


def _stamp_of(fp_mod, repo: Path, *, detail: bool = True) -> dict:
    return fp_mod.compute_fingerprint(repo, detail=detail)


def test_diff_two_stamps_in_sync(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    s = _stamp_of(fp_mod, repo)
    assert rd_mod.diff_two_stamps(s, s)["verdict"] == "in_sync"


def test_diff_two_stamps_data_only_gives_sync_with_dirs(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    seat = _stamp_of(fp_mod, repo)
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")
    (repo / "src" / "web" / "i18n_packs" / "demo_page.py").write_text(
        "ZH = {'k': '二'}\nEN = {'k': 'two'}\n", encoding="utf-8")
    res = rd_mod.diff_two_stamps(seat, _stamp_of(fp_mod, repo))
    assert res["verdict"] == "sync"
    assert res["zones"] == ["src/web/i18n_packs", "src/web/templates"]
    assert res["dirs"] == ["src/web/i18n_packs", "src/web/templates"]
    # 两个 zone 都是热生效——坐席无需重启壳
    assert res["hot_zones"] == res["zones"] and res["restart_zones"] == []


def test_diff_two_stamps_py_change_is_unsafe(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    seat = _stamp_of(fp_mod, repo)
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")
    res = rd_mod.diff_two_stamps(seat, _stamp_of(fp_mod, repo))
    assert res["verdict"] == "unsafe"   # 混入代码差 → 整包安装，绝不「刷数据装同版」
    assert res["unsafe_total"] == 1


def test_diff_two_stamps_seat_old_format_no_detail(rd_mod, fp_mod, tmp_path):
    repo = _make_repo(tmp_path)
    seat = _stamp_of(fp_mod, repo, detail=False)
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")
    res = rd_mod.diff_two_stamps(seat, _stamp_of(fp_mod, repo))
    assert res["verdict"] == "no_detail"


def test_diff_two_stamps_seat_extra_root_is_unsafe(rd_mod, fp_mod, tmp_path):
    """坐席 stamp 有本机没有的根＝构建脚本版本分叉，差量语义不成立。"""
    repo = _make_repo(tmp_path)
    local = _stamp_of(fp_mod, repo)
    seat = json.loads(json.dumps(local))
    seat["aggregate"] = "different"
    seat["roots"].append({"label": "ghost_root", "digest": "g", "files": {}})
    res = rd_mod.diff_two_stamps(seat, local)
    assert res["verdict"] == "unsafe"
    assert res["unsafe"][0][2] == "root_only_on_seat"


def test_diff_two_stamps_aggregate_drift_without_file_diff_is_unsafe(rd_mod, fp_mod, tmp_path):
    """聚合异变却 diff 不出文件级差异——宁可拒绝也不猜。"""
    repo = _make_repo(tmp_path)
    local = _stamp_of(fp_mod, repo)
    seat = json.loads(json.dumps(local))
    seat["aggregate"] = "drifted"
    res = rd_mod.diff_two_stamps(seat, local)
    assert res["verdict"] == "unsafe"


def test_zones_to_internal_dirs_dedups_nested(rd_mod):
    dirs = rd_mod.zones_to_internal_dirs({"config", "config/profiles", "src/web/static"})
    assert dirs == ["config", "src/web/static"]  # profiles 已被 config 整目录覆盖
    # 所有 zone 都必须有映射（新增安全区忘了配目录 = KeyError 立即点名）
    assert rd_mod.zones_to_internal_dirs(set(rd_mod._SAFE_LABELS)
                                         | {z for _, z in rd_mod._SRC_SAFE_MAP})


def test_run_diff_stamps_cli_exit_codes(rd_mod, fp_mod, tmp_path, capsys):
    repo = _make_repo(tmp_path)
    out = _make_dist(tmp_path, repo, fp_mod)
    seat_file = tmp_path / "seat-stamp.json"

    # in_sync → 0
    seat_file.write_text(json.dumps(_stamp_of(fp_mod, repo)), encoding="utf-8")
    assert rd_mod.run_diff_stamps(seat_file, out, fp=fp_mod) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "in_sync"

    # 数据差 → 6 + JSON 带 dirs（PS 消费面）
    (repo / "src" / "web" / "templates" / "a.html").write_text("<p>v2</p>", encoding="utf-8")
    fp_mod.write_stamp(out, _stamp_of(fp_mod, repo))
    assert rd_mod.run_diff_stamps(seat_file, out, fp=fp_mod) == 6
    res = json.loads(capsys.readouterr().out)
    assert res["dirs"] == ["src/web/templates"] and res["hot_zones"]

    # 代码差 → 3
    (repo / "main.py").write_text("print('v2')\n", encoding="utf-8")
    fp_mod.write_stamp(out, _stamp_of(fp_mod, repo))
    assert rd_mod.run_diff_stamps(seat_file, out, fp=fp_mod) == 3
    capsys.readouterr()

    # 坐席 stamp 损坏/缺失 → 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert rd_mod.run_diff_stamps(bad, out, fp=fp_mod) == 2
    assert rd_mod.run_diff_stamps(tmp_path / "nope.json", out, fp=fp_mod) == 2
    capsys.readouterr()


def test_cli_flag_wired(rd_mod):
    text = (_BUILD / "refresh_backend_datas.py").read_text(encoding="utf-8")
    assert "--diff-stamps" in text and "run_diff_stamps" in text


# ---------------------------------------------------------------- 使用账本（观测）


def test_usage_log_skips_non_real_out(rd_mod, tmp_path, monkeypatch):
    """tests/tmp 产物目录绝不落账——账本读数只反映真实构建链。"""
    monkeypatch.setattr(rd_mod, "HERE", tmp_path / "here")
    rd_mod._maybe_log_usage(tmp_path / "backend-dist", {"mode": "refresh", "rc": 0})
    assert not (tmp_path / "here" / rd_mod.USAGE_LOG_NAME).exists()


def test_usage_log_appends_jsonl_for_real_out(rd_mod, tmp_path, monkeypatch):
    here = tmp_path / "here"
    here.mkdir()
    out = tmp_path / "backend-dist"
    out.mkdir()
    monkeypatch.setattr(rd_mod, "HERE", here)
    monkeypatch.setattr(rd_mod, "OUT", out)
    rd_mod._maybe_log_usage(out, {"mode": "refresh", "rc": 0, "secs": 1.2})
    rd_mod._maybe_log_usage(out, {"mode": "diff_stamps", "verdict": "sync"})
    lines = (here / rd_mod.USAGE_LOG_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["mode"] == "refresh" and row["rc"] == 0 and "ts" in row


def test_usage_log_write_failure_is_silent(rd_mod, tmp_path, monkeypatch):
    """账本绝不影响主流程：HERE 指向不可写位置也不抛。"""
    out = tmp_path / "backend-dist"
    out.mkdir()
    monkeypatch.setattr(rd_mod, "HERE", tmp_path / "no" / "such" / "dir")
    monkeypatch.setattr(rd_mod, "OUT", out)
    rd_mod._maybe_log_usage(out, {"mode": "refresh", "rc": 0})  # 不抛即过
