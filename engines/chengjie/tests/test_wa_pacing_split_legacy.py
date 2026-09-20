"""WhatsApp 拟人节奏分条键名统一（split_mode）+ legacy split_strategy 兼容门禁。

历史事故：渠道页保存 payload 写 ``human_pacing.split_strategy``，service 默认块
也用 ``split_strategy``，但消费方 ``PacingConfig`` 只读 ``split_mode`` → 用户在
UI 改「按长度」完全不生效（安慰剂开关）。修复三处协同：

- ``line_rpa/human_pacing.py::PacingConfig.from_dict``（WhatsApp/LINE/Messenger
  三链共用消费方）：读 ``split_mode``，缺失时回落 legacy ``split_strategy``；
- ``whatsapp_rpa/service.py``：默认块改 ``split_mode``，且 ``_merged()`` 对
  「用户只存过 legacy 键」的旧配置做归一（否则默认块的 split_mode 会遮蔽用户旧值）;
- 模板保存 payload 写 ``split_mode``（对象键形式的 ``split_strategy:`` 清零）。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.integrations.line_rpa.human_pacing import PacingConfig
from src.integrations.whatsapp_rpa.service import WhatsAppRpaService

_ROOT = Path(__file__).resolve().parents[1]
_WA_TEMPLATE = _ROOT / "src" / "web" / "templates" / "_channel_body_whatsapp.html"
_WA_SERVICE = _ROOT / "src" / "integrations" / "whatsapp_rpa" / "service.py"


# ── ① PacingConfig 解析：split_mode 为准，legacy split_strategy 回落 ──────────

def test_pacing_split_mode_key():
    c = PacingConfig.from_dict({"split_mode": "length"})
    assert c.split_mode == "length"


def test_pacing_legacy_split_strategy_fallback():
    # 线上已保存的旧键（修复前 UI 写的就是它）必须仍然生效
    c = PacingConfig.from_dict({"split_strategy": "length"})
    assert c.split_mode == "length"


def test_pacing_split_mode_wins_over_legacy():
    c = PacingConfig.from_dict({"split_mode": "none", "split_strategy": "length"})
    assert c.split_mode == "none"


def test_pacing_split_default_when_both_absent():
    assert PacingConfig.from_dict({}).split_mode == "sentence"
    assert PacingConfig.from_dict(None).split_mode == "sentence"


def test_pacing_legacy_value_normalized_lowercase():
    # 回落值与主键同一套规整（str().lower()），取值集合不变
    c = PacingConfig.from_dict({"split_strategy": "LENGTH"})
    assert c.split_mode == "length"


# ── ①b service._merged：默认块 split_mode 不得遮蔽用户 legacy 旧值 ────────────

def _merged_with(user_cfg: dict) -> dict:
    """不落盘、不起 runner：只借 _merged/_defaults 两个纯方法。"""
    svc = object.__new__(WhatsAppRpaService)
    svc._cfg = user_cfg
    return svc._merged()


def test_service_merge_legacy_split_strategy_survives_defaults():
    # 用户旧配置只有 split_strategy（修复前 UI 保存的形态）：
    # 默认块现在提供 split_mode=sentence，若不归一会把用户的 length 遮蔽回 sentence
    merged = _merged_with({"human_pacing": {"split_strategy": "length"}})
    assert merged["human_pacing"]["split_mode"] == "length"
    assert PacingConfig.from_dict(merged["human_pacing"]).split_mode == "length"


def test_service_merge_split_mode_wins_over_stale_legacy():
    # 重新保存后 split_mode 与陈旧 split_strategy 并存：split_mode 优先
    merged = _merged_with(
        {"human_pacing": {"split_mode": "none", "split_strategy": "length"}})
    assert merged["human_pacing"]["split_mode"] == "none"


def test_service_merge_defaults_when_user_empty():
    merged = _merged_with({})
    assert merged["human_pacing"]["split_mode"] == "sentence"


# ── ② 静态断言：写侧全部统一 split_mode ──────────────────────────────────────

# 对象键形式＝裸键 `split_strategy:` 或引号键 `"split_strategy":`；
# 负查放行读取表达式 `hp.split_strategy`（legacy 回落读）与更长标识符。
_SPLIT_KEY_RE = re.compile(r"(?<![.\w])['\"]?split_strategy['\"]?\s*:")


def test_template_payload_writes_split_mode_only():
    tpl = _WA_TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"split_mode:\s*\$\('cfg-wa-split'\)\.value", tpl), (
        "保存 payload 必须写运行时真读的键 split_mode")
    bad = [
        f"line {tpl.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}"
        for m in _SPLIT_KEY_RE.finditer(tpl)
    ]
    assert not bad, "模板不得再以对象键形式写 split_strategy: → " + "; ".join(bad)


def test_service_defaults_use_split_mode():
    src = _WA_SERVICE.read_text(encoding="utf-8")
    assert '"split_mode": "sentence"' in src, "service 默认块应提供 split_mode"
    bad = [m.group(0) for m in _SPLIT_KEY_RE.finditer(src)]
    assert not bad, (
        "service.py 不得再定义 split_strategy: 键（legacy 只允许 .get 读取回落）→ "
        + "; ".join(bad))
