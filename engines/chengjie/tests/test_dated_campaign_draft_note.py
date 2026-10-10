"""过期活动稿只加注记，历史正文保持原句。"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_MARKER = "historical-draft: not current product claims"
_HISTORICAL_THEME = (
    "社群福利 · 智聊 ChatX 下载即免费开始：标准翻译不限量 + 注册送体验 Token，新人 6U 大礼包双倍到账"
)

_NOTED = (
    "website/lib/daily-content.ts",
    "website/docs/claims.md",
    "deliverables/video_songs_20260916/README.md",
    "deliverables/video_sample_20260827/README.md",
    "docs/实施52_官网顾问小界_频道日更ChatX聚焦_三视角定传方案_2026-08.md",
    "docs/实施77_智聊获客与营销作战方案_渠道打法与90天执行清单_2026-08.md",
    "docs/实施77A_TG广告弹药包_频道定向素材与投放SOP_2026-08.md",
    "docs/方案_视频教程与介绍体系_竖屏横屏唱歌式场景_2026-09-16.md",
)
_LISTED_IN_CLAIMS = (
    "website/lib/daily-content.ts",
    "deliverables/video_songs_20260916/",
    "deliverables/video_sample_20260827/",
    "docs/实施52_官网顾问小界_频道日更ChatX聚焦_三视角定传方案_2026-08.md",
    "docs/实施77_智聊获客与营销作战方案_渠道打法与90天执行清单_2026-08.md",
    "docs/实施77A_TG广告弹药包_频道定向素材与投放SOP_2026-08.md",
    "docs/方案_视频教程与介绍体系_竖屏横屏唱歌式场景_2026-09-16.md",
)


def test_dated_drafts_are_flagged_and_theme_text_unchanged():
    daily = (_REPO / "website/lib/daily-content.ts").read_text(encoding="utf-8")
    assert "export const DATED_CAMPAIGN_DRAFT = true" in daily
    assert _HISTORICAL_THEME in daily
    for rel in _NOTED:
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert _MARKER in text, rel
    claims = (_REPO / "website/docs/claims.md").read_text(encoding="utf-8")
    for rel in _LISTED_IN_CLAIMS:
        assert rel in claims, rel
