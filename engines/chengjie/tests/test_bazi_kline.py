"""人生 K 线门禁：逐年评分（喜忌×流年/大运，确定性可解释）+ PNG 渲染 + Stage 接线。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.companion.bazi_engine import BirthInfo, bazi_available, compute_bazi
from src.companion.bazi_kline import (
    build_kline_series,
    dayun_for_year,
    render_kline_png,
    year_score,
)

pytestmark = pytest.mark.skipif(
    not bazi_available(), reason="lunar_python 未安装")

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


@pytest.fixture()
def chart():
    # 1995-03-05 08:30 女：日主乙木偏强，喜用 金/火/土（忌 水/木）
    return compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))


# ── 评分 ─────────────────────────────────────────────────────────────────────

def test_year_score_favorable_vs_unfavorable(chart):
    """2026 丙午（火/火=双喜用）应显著高于 2032 壬子（水/水=双忌）。"""
    good = year_score(chart, 2026)
    bad = year_score(chart, 2032)
    assert good["ganzhi"] == "丙午" and bad["ganzhi"] == "壬子"
    assert good["score"] > 60
    assert bad["score"] < 45
    assert good["score"] - bad["score"] >= 20


def test_year_score_deterministic(chart):
    a = year_score(chart, 2027)
    b = year_score(chart, 2027)
    assert a == b


def test_year_score_clipped_range(chart):
    for y in range(2020, 2040):
        s = year_score(chart, y)
        assert 8 <= s["score"] <= 92, (y, s)


def test_neutral_chart_flat_curve():
    """中和命局（喜用表空）→ 只剩十神微调，曲线平缓（|50-score| ≤ 3）。"""
    c = compute_bazi(BirthInfo(1995, 3, 5, 8, 30, gender="female"))
    import copy
    c2 = copy.deepcopy(c)
    c2["strength"] = {"verdict": "中和", "xi_yong_candidates": []}
    for y in range(2024, 2034):
        s = year_score(c2, y)
        assert abs(s["score"] - 50) <= 3, (y, s)


def test_dayun_for_year(chart):
    dy = chart["dayun"]
    assert dayun_for_year(chart, dy[0]["start_year"])["ganzhi"] == dy[0]["ganzhi"]
    assert dayun_for_year(chart, dy[1]["start_year"] + 1)["ganzhi"] == dy[1]["ganzhi"]
    assert dayun_for_year(chart, dy[0]["start_year"] - 1) is None  # 起运前


def test_series_structure(chart):
    s = build_kline_series(chart, start_year=2024, years=10)
    pts = s["points"]
    assert len(pts) == 10
    assert [p["year"] for p in pts] == list(range(2024, 2034))
    assert all(p["ganzhi"] and p["dayun"] for p in pts)
    assert s["has_dayun"] is True


def test_series_invalid_chart():
    assert build_kline_series({}, start_year=2024) is None
    assert year_score({}, 2026) is None


# ── 渲染 ─────────────────────────────────────────────────────────────────────

def test_render_png(tmp_path, chart):
    pytest.importorskip("PIL")
    s = build_kline_series(chart, start_year=2024, years=10)
    out = tmp_path / "kline.png"
    assert render_kline_png(s, str(out)) is True
    assert out.is_file() and out.stat().st_size > 2000
    from PIL import Image
    with Image.open(out) as img:
        assert img.size == (1080, 640)


def test_render_soft_fail(tmp_path):
    assert render_kline_png({}, str(tmp_path / "x.png")) is False
    assert render_kline_png({"points": [{"year": 2024, "score": 50}]},
                            str(tmp_path / "y.png")) is False  # 少于 2 点


# ── Stage C 接线（轻量绑定） ───────────────────────────────────────────────────

class _Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def list_rows(self, prefix="", limit=100, source=""):
        return [{"content": c} for c in self.rows]


class _SM:
    _bazi_cfg = _SMcls._bazi_cfg
    resolve_birth_info = _SMcls.resolve_birth_info
    _handle_bazi_kline_request = _SMcls._handle_bazi_kline_request

    def __init__(self, *, bazi_cfg=None, rows=None, send_ok=True, out_dir=""):
        comp = {}
        if bazi_cfg is not None:
            if out_dir:
                bazi_cfg = dict(bazi_cfg, kline_out_dir=out_dir)
            comp["bazi"] = bazi_cfg
        self.config = SimpleNamespace(config={"companion": comp})
        self.logger = logging.getLogger("test_kline")
        self._episodic_store = _Store(rows)
        self._send_ok = send_ok
        self.sent = []

    def _episodic_storage_key(self, user_id_str, chat_id, platform=""):
        return f"u:{user_id_str}"

    async def _try_send_selfie_media(self, user_context, chat_id, image_path,
                                     caption, **kw):
        self.sent.append((image_path, caption))
        return self._send_ok


_ON = {"enabled": True}
_FACT = "用户的出生信息：公历1995年3月5日 8时30分出生 性别女"


@pytest.fixture(autouse=True)
def _stats_clean():
    from src.companion.bazi_stats import get_bazi_stats
    get_bazi_stats().reset()
    yield
    get_bazi_stats().reset()


@pytest.mark.asyncio
async def test_kline_request_sends_image(tmp_path):
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], out_dir=str(tmp_path))
    out = await sm._handle_bazi_kline_request(
        "帮我画一下我的人生K线吧", "u1", {}, "c1")
    assert out == ""  # 图已发出
    assert len(sm.sent) == 1
    img_path, caption = sm.sent[0]
    assert Path(img_path).is_file()
    assert "仅供参考" in caption
    from src.companion.bazi_stats import get_bazi_stats
    assert get_bazi_stats().dump()["kline"]["sent"] == 1


@pytest.mark.asyncio
async def test_kline_not_a_request_none(tmp_path):
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], out_dir=str(tmp_path))
    assert await sm._handle_bazi_kline_request("今天天气不错", "u1", {}, "c1") is None
    assert sm.sent == []


@pytest.mark.asyncio
async def test_kline_disabled_none(tmp_path):
    sm = _SM(bazi_cfg={"enabled": False}, rows=[_FACT], out_dir=str(tmp_path))
    assert await sm._handle_bazi_kline_request(
        "画一下我的运势曲线", "u1", {}, "c1") is None
    sm2 = _SM(bazi_cfg=dict(_ON, kline=False), rows=[_FACT], out_dir=str(tmp_path))
    assert await sm2._handle_bazi_kline_request(
        "画一下我的运势曲线", "u1", {}, "c1") is None


@pytest.mark.asyncio
async def test_kline_no_birth_none(tmp_path):
    """缺生辰 → None（交注入路径顺势采集，不发半张错图）。"""
    sm = _SM(bazi_cfg=_ON, out_dir=str(tmp_path))
    assert await sm._handle_bazi_kline_request(
        "画一下我的人生K线", "u1", {}, "c1") is None
    assert sm.sent == []


@pytest.mark.asyncio
async def test_kline_send_fail_returns_none_and_counts(tmp_path):
    sm = _SM(bazi_cfg=_ON, rows=[_FACT], send_ok=False, out_dir=str(tmp_path))
    out = await sm._handle_bazi_kline_request(
        "画一下我的运势曲线", "u1", {}, "c1")
    assert out is None  # 发送失败 → 回落文字聊天
    from src.companion.bazi_stats import get_bazi_stats
    assert get_bazi_stats().dump()["kline"]["failed"] == 1


@pytest.mark.asyncio
async def test_kline_same_turn_birth_in_message(tmp_path):
    """同轮报生辰 + 求 K 线 → 直接出图（同轮闭环延伸到出图链）。"""
    sm = _SM(bazi_cfg=_ON, out_dir=str(tmp_path))
    out = await sm._handle_bazi_kline_request(
        "我1995年3月5日早上8点半出生的女生，画一下我的人生K线", "u1", {}, "c1")
    assert out == ""
    assert len(sm.sent) == 1
