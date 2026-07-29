# -*- coding: utf-8 -*-
"""评测器必须读「运行态真实配置」而非 CWD 相对的引擎根旧副本。

2026-07-29 实锤：`src/eval/*` 四处各自 `open("config/config.yaml")`（CWD 相对）。
双实例迁移后，从引擎根跑 `python -m scripts.run_eval`（文档给的用法，也是周批任务
`TranslationEvalWeekly` 的 Set-Location 落点）读到的是**迁移时刻遗留的旧副本**，
与实例在跑的配置 4/9 个关键键不一致（端点拓扑 / per_lang_order / 嵌入端点）。

这类缺陷**不会让报告变红**——两套配置下都 10/10 PASS，只是评错了对象、归因失真
（「弱语对该不该进 per_lang_order」正是读这些数字决策的）。所以只能靠门禁钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parents[1] / "src" / "eval"

#: 除 eval_config 自身的兜底实现外，src/eval 里不允许再出现 CWD 相对读配置
_CWD_CONFIG_READ = re.compile(r"""open\(\s*["']config/config(\.local)?\.yaml["']""")


def test_no_cwd_relative_config_reads_in_eval_modules():
    offenders = []
    for f in sorted(_EVAL_DIR.rglob("*.py")):
        if f.name == "eval_config.py":
            continue  # 兜底实现刻意保留旧语义（离线/无实例环境）
        for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _CWD_CONFIG_READ.search(line):
                offenders.append(f"{f.name}:{i}  {line.strip()}")
    assert not offenders, (
        "评测模块出现 CWD 相对读配置（生产 CWD≠实例数据根 → 评的不是在跑的配置）：\n  "
        + "\n  ".join(offenders)
        + "\n改法：from src.eval.eval_config import load_runtime_config；"
          "cfg = load_runtime_config(config)")


def test_load_runtime_config_passes_through_explicit_config():
    """调用方显式给 config 时必须原样返回——否则会悄悄改掉单测/横比的输入。"""
    from src.eval.eval_config import load_runtime_config

    sentinel = {"marker": object()}
    assert load_runtime_config(sentinel) is sentinel


def test_load_runtime_config_prefers_data_root_over_cwd(tmp_path, monkeypatch):
    """AITR_DATA_ROOT 指定的根优先于 CWD——即「读实例而非引擎根」的核心不变量。"""
    from src.eval import eval_config

    root = tmp_path / "inst"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(
        "ai:\n  embedding_base_url: http://from-data-root:11434\n", encoding="utf-8")
    (root / "config" / "config.local.yaml").write_text(
        "ai:\n  model: overlay-wins\n", encoding="utf-8")

    monkeypatch.setenv("AITR_DATA_ROOT", str(root))
    cfg = eval_config.load_runtime_config(None)

    assert cfg.get("ai", {}).get("embedding_base_url") == "http://from-data-root:11434"
    # overlay 必须深合并进来（端点等运营开关常只写在 overlay，不合并会漏评）
    assert cfg.get("ai", {}).get("model") == "overlay-wins"


def test_cwd_config_wins_when_cwd_is_not_engine_root(tmp_path, monkeypatch):
    """CWD 里刻意摆的 config 必须优先于自动发现的活跃实例——**测试密闭性**靠这条。

    回归 2026-07-29 的真缺陷：首版实现无条件走数据根解析，于是
    ``monkeypatch.chdir(tmp)`` 的单测会静默去读**生产实例**配置，
    结果取决于跑在哪台机器上（本机红、CI 绿），与 global_rules 那次
    「测试写生产」属同一类互串事故。`test_embedding_providers` 两条就是这么红的。
    """
    from src.eval import eval_config

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        'ai:\n  embedding_base_url: "http://from-cwd:11434"\n', encoding="utf-8")
    (tmp_path / "config" / "config.local.yaml").write_text(
        'ai:\n  embedding_model: "cwd-overlay"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # 即便同时存在可发现的活跃实例/环境变量，CWD 显式配置仍应胜出
    monkeypatch.delenv("AITR_DATA_ROOT", raising=False)

    cfg = eval_config.load_runtime_config(None)
    assert cfg["ai"]["embedding_base_url"] == "http://from-cwd:11434"
    assert cfg["ai"]["embedding_model"] == "cwd-overlay", "CWD overlay 也要合并"


def test_engine_root_cwd_defers_to_active_instance(monkeypatch, tmp_path):
    """反面：CWD == 引擎根时那份是**迁移遗留旧副本**，必须让位给活跃实例。

    这正是本模块存在的理由（文档用法 `python -m scripts.run_eval` 就在引擎根跑）。
    """
    from src.eval import eval_config

    inst = tmp_path / "inst"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "config.yaml").write_text(
        'ai:\n  embedding_base_url: "http://active-instance:11434"\n', encoding="utf-8")
    monkeypatch.setenv("AITR_DATA_ROOT", str(inst))
    monkeypatch.setattr(eval_config, "_cwd_is_engine_root", lambda: True)
    monkeypatch.setattr(eval_config, "_cwd_has_config", lambda: True)

    cfg = eval_config.load_runtime_config(None)
    assert cfg["ai"]["embedding_base_url"] == "http://active-instance:11434"


def test_falls_back_softly_when_data_root_chain_unusable(tmp_path, monkeypatch):
    """数据根链不可用时软回落、绝不抛——评测缺配置应优雅 skip，不该把回归搞崩。"""
    from src.eval import eval_config

    monkeypatch.setenv("AITR_DATA_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.chdir(tmp_path)  # CWD 下也没有 config/
    assert eval_config.load_runtime_config(None) == {}


@pytest.mark.parametrize("mod,fn", [
    ("src.eval.translation_eval", "_load_config"),
    ("src.eval.embedding_providers", "_load_config_if_none"),
])
def test_eval_loaders_delegate_to_runtime_config(mod, fn, tmp_path, monkeypatch):
    """两个公开 loader 必须走同一解析（否则一处修好、另一处仍读旧副本）。"""
    import importlib

    root = tmp_path / "inst"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(
        "ai:\n  embedding_base_url: http://delegated:11434\n", encoding="utf-8")
    monkeypatch.setenv("AITR_DATA_ROOT", str(root))

    loader = getattr(importlib.import_module(mod), fn)
    assert loader(None).get("ai", {}).get("embedding_base_url") == "http://delegated:11434"
