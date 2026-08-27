"""i18n P0 门禁（2026-08-19）：英文界面「后端展示串漏中文」防回归。

背景（四张老板截图实锤）：UI 切英文后仍大面积中文——今日节拍（goals 意图池
中文-only）、关系阶段「初识」（STAGE_LABEL_ZH 直发 UI）、来源「其他平台→Telegram」
（origin_context.CHANNEL_LABELS 本是 prompt 内部词表被 origin_pill 带上 UI）。
修法＝**载荷带机器码/英文变体，前端查词典渲染**；本文件钉住三层契约：

1. goal_view / agenda_item 载荷必须带 ``intent_en``（planner 产的意图能反查出
   英文、运营手输 {note}/{item} 数据原样保留不翻译）；
2. 后端展示词表（STAGE_LABEL_ZH / FUNNEL_STAGE_LABELS / CHANNEL_LABELS 中文项）
   与前端词典（web i18n pack + cp-i18n.js 双段）**zh 逐字一致 + en 存在且零 CJK**
   ——两边漂移＝中文界面同一阶段两种叫法，比漏翻更糟；
3. 本批动过的副驾共享文件双树（shared ↔ desktop/renderer）字节级同步
   （cp-goal.js 已有专项门禁在 test_goal_ui_revamp，此处补齐其余三件）。

意图池本体的双语结构（每条必须 (zh, en) 对）由 test_goal_templates.py 钉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(s or ""))


# ── 1. goals 载荷带英文展示态 ────────────────────────────────────────────────

def test_goal_view_today_carries_intent_en():
    from src.companion.goals.service import agenda_item, goal_view
    from src.companion.goals.templates import TEMPLATES, pick_intent

    params = {"note": "推广新品"}
    goal = {
        "goal_id": "g-en-gate", "template": "custom", "params": params,
        "status": "active", "milestone_idx": 0,
        "start_ts": 1000.0, "deadline_ts": 1000.0 + 14 * 86400,
    }
    zh = pick_intent(TEMPLATES["custom"], 0, "g-en-gate", "2026-08-19", params=params)
    action = {"action_id": "a1", "day": "2026-08-19", "intent": zh,
              "push_level": "soft", "status": "planned", "detail": ""}
    view = goal_view(goal, action, lang="en")
    en = str(view["today"]["intent_en"])
    assert en, "goal_view.today 丢了 intent_en——英文界面今日节拍会漏中文"
    # 运营手输的 {note} 数据原样保留（数据不译），剥掉后句子必须零 CJK
    assert "推广新品" in en
    assert not _has_cjk(en.replace("推广新品", ""))
    # agenda 行同口径透传
    item = agenda_item(view)
    assert item["intent_en"] == en


def test_goal_view_unknown_intent_falls_back_empty_not_wrong():
    """人工改过/历史孤儿意图 → intent_en=""（前端回落中文原文），绝不错译。"""
    from src.companion.goals.service import goal_view

    goal = {"goal_id": "g2", "template": "custom", "params": {},
            "status": "active", "milestone_idx": 0,
            "start_ts": 1000.0, "deadline_ts": 1000.0 + 86400}
    action = {"action_id": "a2", "day": "2026-08-19",
              "intent": "运营手工改写过的今日安排", "push_level": "soft",
              "status": "planned", "detail": ""}
    view = goal_view(goal, action, lang="en")
    assert view["today"]["intent_en"] == ""


# ── 2. 后端词表 ↔ 前端词典 契约对齐 ─────────────────────────────────────────

def _cp_i18n_text() -> str:
    return (_REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js").read_text(
        encoding="utf-8")


def test_rel_stage_labels_pinned_to_frontend_dicts():
    from src.utils.companion_relationship import STAGE_LABEL_ZH
    from src.web.web_i18n import get_translations

    zh_map = get_translations("zh")
    en_map = get_translations("en")
    cp = _cp_i18n_text()
    for code, zh_label in STAGE_LABEL_ZH.items():
        wkey = f"inbox.rel.stage.{code}"
        # web 词典：zh 与后端词表逐字一致（防「同一阶段两种叫法」）
        assert zh_map.get(wkey) == zh_label, (wkey, zh_map.get(wkey), zh_label)
        en_v = en_map.get(wkey)
        assert en_v and not _has_cjk(en_v), (wkey, en_v)
        # cp 词典：zh/en 两段都必须有该键（正则级存在性；双语齐平由值断言兜）
        assert f'"cp.rel.stage.{code}": "{zh_label}"' in cp, code
        assert f'"cp.rel.stage.{code}": "{en_v}"' in cp or (
            f'"cp.rel.stage.{code}": "' in cp), code


def test_funnel_labels_pinned_to_frontend_dicts():
    from src.web.routes.unified_inbox_helpers import FUNNEL_STAGE_LABELS
    from src.web.web_i18n import get_translations

    zh_map = get_translations("zh")
    en_map = get_translations("en")
    cp = _cp_i18n_text()
    for code, zh_label in FUNNEL_STAGE_LABELS.items():
        wkey = f"inbox.funnel.{code.lower()}"
        assert zh_map.get(wkey) == zh_label, (wkey, zh_map.get(wkey), zh_label)
        en_v = en_map.get(wkey)
        assert en_v and not _has_cjk(en_v), (wkey, en_v)
        assert f'"cp.rel.funnel.{code.lower()}": "{zh_label}"' in cp, code


def test_origin_channel_zh_labels_pinned_to_frontend_dicts():
    """CHANNEL_LABELS 里带中文的渠道（网页/手机端/微信/其他平台）必须有前端
    双语键——origin pill/trail 靠这些键在英文界面渲染。"""
    from src.contacts.origin_context import CHANNEL_LABELS
    from src.web.web_i18n import get_translations

    zh_map = get_translations("zh")
    en_map = get_translations("en")
    cp = _cp_i18n_text()
    for code, zh_label in CHANNEL_LABELS.items():
        if not _has_cjk(zh_label):
            continue          # Telegram/LINE 等 ASCII 品牌名不需要词条
        wkey = f"inbox.cp.ch.{code}"
        assert zh_map.get(wkey) == zh_label, (wkey, zh_map.get(wkey), zh_label)
        en_v = en_map.get(wkey)
        assert en_v and not _has_cjk(en_v), (wkey, en_v)
        assert f'"cp.origin.ch.{code}": "{zh_label}"' in cp, code


# ── 3. 副驾共享文件双树同步（cp-goal.js 由 test_goal_ui_revamp 专项钉） ──────

@pytest.mark.parametrize("rel", [
    "i18n/cp-i18n.js",
    "components/cp-rel-stage.js",
    "components/cp-origin.js",
])
def test_copilot_shared_mirror_synced(rel):
    a = _REPO / "shared" / "copilot" / rel
    b = _REPO / "desktop" / "renderer" / "shared" / "copilot" / rel
    assert a.is_file(), rel
    assert b.is_file(), f"desktop 镜像缺 {rel}"
    assert a.read_bytes() == b.read_bytes(), (
        f"{rel} 双树漂移：桌面壳会跑旧组件（改 shared 后必须同步镜像）")


# ── 4. 宿主接线钉（模板热更新直上生产，静态钉防手滑回退） ────────────────────

def test_unified_inbox_stage_helpers_wired():
    html = (_REPO / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    # 码→词典 helper 必须在场且被 hero/toast 消费
    assert "function _relStageL(" in html
    assert "function _originPillL(" in html
    assert "_relStageL(d.display_stage||d.stage" in html
    # 来源行不得再裸渲染服务端中文 pill
    assert "var pillTxt=_originPillL(d);" in html
