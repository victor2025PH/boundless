"""P2：overlay 保注释写入门禁（set_yaml_key_preserving）。

背景（2026-08-01 实锤）：`set_overlay_flag` 旧实现 yaml.dump 整文件重写，一次
enable_dry 把 config.local.yaml 约 30 行人工运维注释全部剃光。新路径 ruamel
round-trip 保注释；任何失败返回 False 由调用方回落旧路径（写入永不因此丢失）。
"""
from __future__ import annotations

import yaml

from src.utils.config_manager import _nested_patch, set_yaml_key_preserving

SAMPLE = """# 运营开关 overlay（深合并覆盖 config.yaml）。
# 请勿提交到 git。
companion:
  # P0 2026-07-29 频控收紧：历史原因说明，这行注释很宝贵
  proactive_topic:
    enabled: true
    min_silent_hours: 24   # 行尾注释也要活下来
  proactive_care:
    interval_sec: 300
ai:
  model: deepseek-v4-flash
  system_prompt: "你是「顾嘉」，多行\\n带引号的值"
"""


def test_preserves_comments_and_values(tmp_path):
    p = tmp_path / "config.local.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    ok = set_yaml_key_preserving(p, ["companion", "proactive_care", "enabled"], True)
    assert ok
    text = p.read_text(encoding="utf-8")
    # 注释三连全部存活（文件头 / 段内 / 行尾）
    assert "运营开关 overlay" in text
    assert "频控收紧" in text
    assert "行尾注释也要活下来" in text
    # 目标键写入 + 其余值零变化
    data = yaml.safe_load(text)
    assert data["companion"]["proactive_care"]["enabled"] is True
    assert data["companion"]["proactive_care"]["interval_sec"] == 300
    assert data["companion"]["proactive_topic"]["min_silent_hours"] == 24
    assert data["ai"]["model"] == "deepseek-v4-flash"
    assert "顾嘉" in data["ai"]["system_prompt"]


def test_creates_missing_file_and_nested_path(tmp_path):
    p = tmp_path / "config.local.yaml"
    assert set_yaml_key_preserving(p, ["a", "b", "c"], 7)
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"a": {"b": {"c": 7}}}


def test_overwrites_scalar_intermediate(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    assert set_yaml_key_preserving(p, ["a", "b"], 2)
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"a": {"b": 2}}


def test_non_mapping_root_returns_false(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    assert set_yaml_key_preserving(p, ["a"], 1) is False
    # 原文件未被破坏
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == [1, 2]


def test_empty_keys_returns_false(tmp_path):
    p = tmp_path / "y.yaml"
    assert set_yaml_key_preserving(p, [], 1) is False
    assert not p.exists()


def test_nested_patch_shape():
    assert _nested_patch(["a"], 1) == {"a": 1}
    assert _nested_patch(["a", "b", "c"], True) == {"a": {"b": {"c": True}}}


# ── P3：merge_yaml_patch_preserving（save_overlay_patch 的保注释路径） ──
def _merge_replace_aware(dst, patch, replace, _prefix=""):
    """镜像 ConfigManager._merge_patch_replace_aware 语义的独立实现（契约即
    「callable(dst, patch, replace)」，helper 不关心来源）。"""
    for k, v in patch.items():
        path = f"{_prefix}{k}"
        if (path not in replace and isinstance(v, dict)
                and isinstance(dst.get(k), dict)):
            _merge_replace_aware(dst[k], v, replace, path + ".")
        else:
            dst[k] = v


def test_merge_patch_preserves_comments(tmp_path):
    from src.utils.config_manager import merge_yaml_patch_preserving
    p = tmp_path / "config.local.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    ok = merge_yaml_patch_preserving(
        p, {"companion": {"proactive_care": {"dry_run": False}},
            "new_top": {"x": 1}},
        set(), _merge_replace_aware)
    assert ok
    text = p.read_text(encoding="utf-8")
    assert "频控收紧" in text
    assert "行尾注释也要活下来" in text
    data = yaml.safe_load(text)
    assert data["companion"]["proactive_care"]["dry_run"] is False
    assert data["companion"]["proactive_care"]["interval_sec"] == 300  # 深合并不丢兄弟键
    assert data["new_top"] == {"x": 1}
    assert data["companion"]["proactive_topic"]["enabled"] is True


def test_merge_patch_replace_path_replaces_subtree(tmp_path):
    from src.utils.config_manager import merge_yaml_patch_preserving
    p = tmp_path / "y.yaml"
    p.write_text("# keep me\nintent:\n  keywords:\n    old_key:\n    - x\n",
                 encoding="utf-8")
    ok = merge_yaml_patch_preserving(
        p, {"intent": {"keywords": {"greeting": ["hi"]}}},
        {"intent.keywords"}, _merge_replace_aware)
    assert ok
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    # replace 路径整树替换：old_key 必须消失（深合并会让它赖着不走）
    assert data["intent"]["keywords"] == {"greeting": ["hi"]}
    assert "# keep me" in p.read_text(encoding="utf-8")


def test_merge_patch_rejects_bad_inputs(tmp_path):
    from src.utils.config_manager import merge_yaml_patch_preserving
    p = tmp_path / "z.yaml"
    assert merge_yaml_patch_preserving(p, {}, set(), _merge_replace_aware) is False
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    assert merge_yaml_patch_preserving(
        p, {"a": 1}, set(), _merge_replace_aware) is False
