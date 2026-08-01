"""功能矩阵导出（scripts/feature_matrix.py，P3）门禁。

核心不变量＝**生成物与注册表逐字同步**：`docs/功能矩阵_ChatX.md` 是对外物料的
单一出口，注册表改了矩阵没重生成 → 「卖的」与「交付的」再次分叉——这正是
整条工作线要消灭的事故形态，所以红起来必须够响、修起来必须一条命令。
"""

from __future__ import annotations

from pathlib import Path

from scripts.feature_matrix import _DOC_DEFAULT, render_matrix
from src.utils.feature_registry import ui_features
from src.web.web_i18n import get_translations

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def test_render_covers_all_ui_features_and_is_deterministic():
    text = render_matrix()
    zh = get_translations("zh")
    missing = [f.slug for f in ui_features()
               if zh.get(f"fc_f_{f.slug}", f.slug) not in text]
    assert not missing, f"矩阵漏功能: {missing}"
    assert "全档位（标配）" in text, "A 类必须标「全档位（标配）」"
    assert "及以上" in text, "已归档位的功能必须标最低档位"
    assert render_matrix() == text, "渲染必须确定性（同步门禁的前提）"


def test_doc_in_sync_with_registry():
    assert _DOC_DEFAULT.exists(), (
        "docs/功能矩阵_ChatX.md 不存在——跑 "
        "`python -m scripts.feature_matrix --out docs/功能矩阵_ChatX.md` 生成")
    on_disk = _DOC_DEFAULT.read_text(encoding="utf-8")
    assert on_disk == render_matrix(), (
        "功能矩阵与注册表脱节（改了 feature_registry / i18n 词条 / FEATURE_MIN_PLAN "
        "后没有重新导出）。修法一条命令：\n"
        "  python -m scripts.feature_matrix --out docs/功能矩阵_ChatX.md")
