"""人生 K 线（逐年运势曲线）——评分纯函数 + PIL 出图（对标 AuraMate「人生K线」）。

评分模型（确定性、可解释、可单测——不是玄学随机数）：
  基准 50 分，四个加权分量全部来自排盘引擎的真实数据：
    ① 流年天干五行 × 命局喜忌（±16）   ② 流年地支主气五行 × 喜忌（±10）
    ③ 所处大运天干五行 × 喜忌（±8）    ④ 大运地支主气五行 × 喜忌（±5）
  再叠加十神语义微调（吉神 +2 / 需驾驭 −2）；中和命局喜忌分量为 0 → 曲线平缓
  （诚实反映「没有明显好坏年」而非硬造波动）。分数夹在 8..92——刻意不给 0/100
  （命理是参考视角，不出「绝对好/绝对坏」的视觉断言）。

出图：PIL 深底卡片（对标「极简新东方未来主义」），大运分段底色 + 逐年折线 +
高低分点着色 + 干支标注；中文字体缺失时自动退化 ASCII 标签（CI 环境不崩）。
图片本体免费（可分享传播），逐年详解文本仍走详批变现门控——图引流、深度变现。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.companion.bazi_engine import (
    GAN_INFO,
    ZHI_MAIN_GAN,
    liunian_detail,
)

logger = logging.getLogger(__name__)

_ALL_ELEMENTS = ("金", "木", "水", "火", "土")

# 十神语义微调：传统「四吉神」略加、「需驾驭之神」略减、中性归零。
_SHISHEN_BONUS = {
    "正印": 2, "食神": 2, "正财": 2, "正官": 2,
    "偏印": -2, "伤官": -2, "七杀": -2,
    "比肩": 0, "劫财": 0, "偏财": 0,
}

_W_YEAR_GAN = 16
_W_YEAR_ZHI = 10
_W_DAYUN_GAN = 8
_W_DAYUN_ZHI = 5
_SCORE_MIN, _SCORE_MAX = 8, 92


def _affinity(xi_yong: List[str], element: str) -> int:
    """五行对命局的喜忌：喜用 +1 / 忌 −1 / 中和（空喜用表）0。"""
    if not xi_yong or element not in _ALL_ELEMENTS:
        return 0
    return 1 if element in xi_yong else -1


def dayun_for_year(chart: Dict[str, Any], year: int) -> Optional[Dict[str, Any]]:
    """某年所处大运（chart.dayun 已按 start_year 升序）；起运前/无大运 → None。"""
    cur = None
    for d in (chart or {}).get("dayun") or []:
        if int(d.get("start_year", 0)) <= int(year):
            cur = d
        else:
            break
    return cur


def year_score(chart: Dict[str, Any], year: int) -> Optional[Dict[str, Any]]:
    """单年评分：流年/大运五行×喜忌 + 十神微调。排不出流年 → None。"""
    day_master = str((chart or {}).get("day_master") or "")
    if not day_master:
        return None
    detail = liunian_detail(day_master[0], year)
    if not detail:
        return None
    xi_yong = list(
        ((chart.get("strength") or {}).get("xi_yong_candidates")) or [])
    score = 50.0
    score += _W_YEAR_GAN * _affinity(xi_yong, detail.get("gan_wuxing", ""))
    score += _W_YEAR_ZHI * _affinity(xi_yong, detail.get("zhi_main_wuxing", ""))
    dy = dayun_for_year(chart, year)
    dy_gz = str((dy or {}).get("ganzhi") or "")
    if len(dy_gz) == 2:
        dy_gan_wx = (GAN_INFO.get(dy_gz[0]) or ("",))[0]
        dy_zhi_wx = (GAN_INFO.get(ZHI_MAIN_GAN.get(dy_gz[1], "")) or ("",))[0]
        score += _W_DAYUN_GAN * _affinity(xi_yong, dy_gan_wx)
        score += _W_DAYUN_ZHI * _affinity(xi_yong, dy_zhi_wx)
    score += _SHISHEN_BONUS.get(detail.get("gan_shishen", ""), 0)
    score += _SHISHEN_BONUS.get(detail.get("zhi_shishen", ""), 0) / 2.0
    score = max(_SCORE_MIN, min(_SCORE_MAX, score))
    return {
        "year": int(year),
        "ganzhi": detail.get("ganzhi", ""),
        "score": round(score, 1),
        "gan_shishen": detail.get("gan_shishen", ""),
        "dayun": dy_gz,
    }


def build_kline_series(
    chart: Dict[str, Any], *, start_year: int, years: int = 10,
) -> Optional[Dict[str, Any]]:
    """逐年序列（含所处大运）；chart 无效/全年排不出 → None。"""
    n = max(1, min(int(years or 10), 30))
    points: List[Dict[str, Any]] = []
    for y in range(int(start_year), int(start_year) + n):
        p = year_score(chart, y)
        if p:
            points.append(p)
    if not points:
        return None
    return {
        "points": points,
        "day_master": str((chart or {}).get("day_master") or ""),
        "verdict": str(((chart or {}).get("strength") or {}).get("verdict") or ""),
        "hour_known": bool((chart or {}).get("hour_known")),
        "has_dayun": bool((chart or {}).get("dayun")),
    }


# ── 渲染 ─────────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _load_font(size: int):
    """(font, cjk_ok)：按候选路径找 CJK 字体；全缺 → PIL 默认字体 + ASCII 标签。"""
    try:
        from PIL import ImageFont
    except Exception:
        return None, False
    import os
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size), True
            except Exception:
                continue
    try:
        return ImageFont.load_default(), False
    except Exception:
        return None, False


def render_kline_png(
    series: Dict[str, Any], out_path: str, *,
    title: str = "人生 K 线 · 十年运势曲线",
    footnote: str = "仅供参考 · 运势是倾向不是命令",
) -> bool:
    """序列 → PNG 卡片；任何异常 → False（软失败，绝不阻塞聊天）。"""
    try:
        from PIL import Image, ImageDraw
        pts = (series or {}).get("points") or []
        if len(pts) < 2:
            return False
        W, H = 1080, 640
        L, T, R, B = 84, 118, 36, 96  # 绘图区边距
        plot_w, plot_h = W - L - R, H - T - B
        bg, panel = (16, 20, 35), (22, 30, 52)
        grid = (38, 48, 74)
        ink, muted = (226, 232, 240), (135, 148, 172)
        line_c = (122, 162, 255)
        good_c, bad_c, mid_c = (74, 222, 128), (248, 113, 113), (203, 213, 225)

        img = Image.new("RGB", (W, H), bg)
        dr = ImageDraw.Draw(img)
        f_title, cjk = _load_font(30)
        f_label, _ = _load_font(18)
        f_small, _ = _load_font(14)

        _title = title if cjk else "Life K-Line (10y)"
        _foot = footnote if cjk else "for reference only"
        dr.text((L, 34), _title, fill=ink, font=f_title)

        def sx(i: int) -> float:
            return L + plot_w * (i / max(1, len(pts) - 1))

        def sy(score: float) -> float:
            return T + plot_h * (1.0 - (float(score) / 100.0))

        # 大运分段底色（交替深浅 + 段首标注干支）
        if series.get("has_dayun"):
            seg_start, seg_dy, shade = 0, str(pts[0].get("dayun") or ""), False
            segs = []
            for i, p in enumerate(pts):
                d = str(p.get("dayun") or "")
                if d != seg_dy:
                    segs.append((seg_start, i - 1, seg_dy))
                    seg_start, seg_dy = i, d
            segs.append((seg_start, len(pts) - 1, seg_dy))
            for (a, b, dyz) in segs:
                x0 = sx(a) - (plot_w / (len(pts) - 1)) * 0.5 if a > 0 else L
                x1 = sx(b) + (plot_w / (len(pts) - 1)) * 0.5 if b < len(pts) - 1 else L + plot_w
                if shade:
                    dr.rectangle([x0, T, x1, T + plot_h], fill=panel)
                shade = not shade
                # 窄段（起运边界落在窗口边缘）跳过标注，防相邻大运标签重叠
                if dyz and cjk and (x1 - x0) >= 96:
                    dr.text((max(x0 + 6, L), T + 6), f"大运 {dyz}",
                            fill=muted, font=f_small)

        # 水平网格 25/50/75
        for gv in (25, 50, 75):
            y = sy(gv)
            dr.line([(L, y), (L + plot_w, y)], fill=grid, width=1)
            dr.text((L - 34, y - 8), str(gv), fill=muted, font=f_small)

        # 折线 + 点 + 逐年标注
        coords = [(sx(i), sy(p["score"])) for i, p in enumerate(pts)]
        dr.line(coords, fill=line_c, width=4, joint="curve")
        for i, p in enumerate(pts):
            x, y = coords[i]
            sc = float(p["score"])
            c = good_c if sc >= 60 else (bad_c if sc <= 42 else mid_c)
            dr.ellipse([x - 6, y - 6, x + 6, y + 6], fill=c, outline=bg, width=2)
            dr.text((x - 12, y - 30), f"{p['score']:g}", fill=c, font=f_small)
            dr.text((x - 18, T + plot_h + 10), str(p["year"]),
                    fill=ink, font=f_small)
            gz = str(p.get("ganzhi") or "")
            if cjk and gz:
                dr.text((x - 15, T + plot_h + 30), gz, fill=muted, font=f_small)

        dr.text((L, H - 34), _foot, fill=muted, font=f_label)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out), format="PNG")
        return True
    except Exception:
        logger.debug("render_kline_png failed", exc_info=True)
        return False


__all__ = [
    "build_kline_series",
    "dayun_for_year",
    "render_kline_png",
    "year_score",
]
