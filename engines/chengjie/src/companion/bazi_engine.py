"""八字排盘引擎（lunar-python 薄封装，纯计算、软失败、可单测）——命理陪伴技能的算法底座。

设计原则：
  1. **单一事实源**：四柱/十神/五行/大运/流年全部出自 ``compute_bazi`` 一个函数的一次输出，
     所有消费方（prompt 注入 / 将来报告 / 每日灵签）共用同一结构——从根上避免
     「各报告喜忌口径不一致」这类竞品踩过的坑（AuraMate v1.0.1 修过整整一版）。
  2. **软失败**：``lunar_python`` 为可选依赖，缺库 / 输入非法 / 库内异常一律返回 None，
     绝不向聊天主链抛异常（陪伴对话零阻断）。
  3. **诚实边界**：时辰未知 → 不出时柱、大运标注「约」；性别未知 → 不排大运
     （大运顺逆依阳男阴女，瞎猜错一半）。强弱/喜用为**粗判参考**，输出中明确标注，
     解读交给 LLM 以陪伴口吻展开，不假装精算。

历法口径：年柱以**立春**分界（八字标准），非农历正月初一；农历输入直接经 Lunar 构造。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── 可选依赖探测（缺库时整个引擎优雅退化为「不可用」） ─────────────────────────
try:  # pragma: no cover - import 探测
    from lunar_python import Lunar, Solar  # type: ignore

    _LUNAR_OK = True
except Exception:  # pragma: no cover
    Lunar = Solar = None  # type: ignore
    _LUNAR_OK = False


def bazi_available() -> bool:
    """排盘引擎是否可用（lunar_python 已安装）。"""
    return _LUNAR_OK


# ── 出生信息（引擎唯一输入契约；抽取/落库在 bazi_profile.py） ──────────────────
@dataclass
class BirthInfo:
    year: int
    month: int
    day: int
    hour: int = -1          # -1 = 时辰未知（不出时柱）
    minute: int = 0
    is_lunar: bool = False  # True = 用户报的是农历生日
    gender: str = ""        # "male" / "female" / ""（未知 → 不排大运）

    def hour_known(self) -> bool:
        return 0 <= int(self.hour) <= 23

    def valid(self) -> bool:
        try:
            y, m, d = int(self.year), int(self.month), int(self.day)
        except (TypeError, ValueError):
            return False
        if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
            return False
        if self.hour_known() and not (0 <= int(self.minute) <= 59):
            return False
        return True

    def cache_key(self) -> tuple:
        return (int(self.year), int(self.month), int(self.day),
                int(self.hour) if self.hour_known() else -1,
                int(self.minute) if self.hour_known() else 0,
                bool(self.is_lunar), str(self.gender or ""))


# ── 五行生克关系（强弱粗判用） ─────────────────────────────────────────────────
_SHENG_ME = {"木": "水", "火": "木", "土": "火", "金": "土", "水": "金"}  # 生我者
_ME_SHENG = {v: k for k, v in _SHENG_ME.items()}                          # 我生者
_KE_ME = {"木": "金", "火": "水", "土": "木", "金": "火", "水": "土"}      # 克我者
_ME_KE = {v: k for k, v in _KE_ME.items()}                                # 我克者

# ── 干支基础表（十神/流年细节的确定性计算，不依赖 lunar_python） ─────────────────
# 天干 → (五行, 是否阳干)
GAN_INFO = {
    "甲": ("木", True), "乙": ("木", False), "丙": ("火", True), "丁": ("火", False),
    "戊": ("土", True), "己": ("土", False), "庚": ("金", True), "辛": ("金", False),
    "壬": ("水", True), "癸": ("水", False),
}
# 地支 → 藏干主气（十神只按主气粗判；全藏干展开留给排盘主路径的 shishen_zhi）
ZHI_MAIN_GAN = {
    "子": "癸", "丑": "己", "寅": "甲", "卯": "乙", "辰": "戊", "巳": "丙",
    "午": "丁", "未": "己", "申": "庚", "酉": "辛", "戌": "戊", "亥": "壬",
}


def shishen_between(day_gan: str, other_gan: str) -> str:
    """``other_gan`` 相对日主 ``day_gan`` 的十神；非法输入 → ""。

    经典规则：同我=比肩/劫财、我生=食神/伤官、生我=偏印/正印、我克=偏财/正财、
    克我=七杀/正官（同性取前者、异性取后者）。与 lunar_python 的 getShiShen 口径
    一致（金标命例交叉验证在测试里）。
    """
    di = GAN_INFO.get(str(day_gan or ""))
    oi = GAN_INFO.get(str(other_gan or ""))
    if not di or not oi:
        return ""
    d_wx, d_yang = di
    o_wx, o_yang = oi
    same_polarity = d_yang == o_yang
    if o_wx == d_wx:
        return "比肩" if same_polarity else "劫财"
    if _ME_SHENG[d_wx] == o_wx:      # 我生者 → 食伤
        return "食神" if same_polarity else "伤官"
    if _SHENG_ME[d_wx] == o_wx:      # 生我者 → 印
        return "偏印" if same_polarity else "正印"
    if _ME_KE[d_wx] == o_wx:         # 我克者 → 财
        return "偏财" if same_polarity else "正财"
    return "七杀" if same_polarity else "正官"  # 克我者 → 官杀

# 强弱粗判阈值（同党占比；月令双计）
_STRONG_RATIO = 0.55
_WEAK_RATIO = 0.40

# 进程级排盘缓存（排盘纯计算但含节气查表，缓存省重复功；上限防撑爆）
_CHART_CACHE: Dict[tuple, Dict[str, Any]] = {}
_CHART_CACHE_MAX = 256


def reset_chart_cache() -> None:
    _CHART_CACHE.clear()


def _pillar(gan: str, zhi: str, shishen_gan: str, shishen_zhi: List[str],
            nayin: str) -> Dict[str, Any]:
    return {
        "ganzhi": f"{gan}{zhi}",
        "gan": gan,
        "zhi": zhi,
        "shishen_gan": shishen_gan,             # 天干十神（日柱 = 日主）
        "shishen_zhi": list(shishen_zhi or []),  # 地支藏干十神（主气在前）
        "nayin": nayin,
    }


def _judge_strength(day_wx: str, counts: Dict[str, float],
                    total: float) -> Dict[str, Any]:
    """日主强弱粗判：同党（生我+同我）加权占比。月令已在 counts 中双计。

    输出 verdict ∈ {偏强, 偏弱, 中和}；xi_yong 为喜用**候选**五行（粗判参考）。
    """
    same_party = counts.get(day_wx, 0.0) + counts.get(_SHENG_ME.get(day_wx, ""), 0.0)
    ratio = (same_party / total) if total > 0 else 0.5
    if ratio >= _STRONG_RATIO:
        verdict = "偏强"
        xi_yong = [_KE_ME[day_wx], _ME_SHENG[day_wx], _ME_KE[day_wx]]  # 克泄耗
    elif ratio <= _WEAK_RATIO:
        verdict = "偏弱"
        xi_yong = [_SHENG_ME[day_wx], day_wx]  # 生扶（印比）
    else:
        verdict = "中和"
        xi_yong = []  # 中和以流通为喜，不硬给单一五行
    return {"verdict": verdict, "same_party_ratio": round(ratio, 3),
            "xi_yong_candidates": xi_yong}


def _current_dayun(dayun_list: List[Dict[str, Any]], now_year: int) -> Optional[Dict[str, Any]]:
    cur = None
    for d in dayun_list:
        if int(d.get("start_year", 0)) <= now_year:
            cur = d
        else:
            break
    return cur


def liunian_ganzhi(year: int, month: int = 7, day: int = 1) -> str:
    """某公历年（默认年中）按立春口径的流年干支；缺库返回 ""。"""
    if not _LUNAR_OK:
        return ""
    try:
        return str(
            Solar.fromYmdHms(int(year), int(month), int(day), 12, 0, 0)
            .getLunar().getYearInGanZhiByLiChun()
        )
    except Exception:
        return ""


def liunian_detail(day_gan: str, year: int) -> Optional[Dict[str, Any]]:
    """某公历年流年相对日主的细节（干支 + 干/支主气十神）——给 LLM 真数据，防它
    自己编干支（LLM 徒手推干支错误率高，是命理场景最常见的事实性硬伤）。"""
    gz = liunian_ganzhi(int(year)) if year else ""
    if len(gz) != 2:
        return None
    gan, zhi = gz[0], gz[1]
    return {
        "year": int(year),
        "ganzhi": gz,
        "gan_shishen": shishen_between(day_gan, gan),
        "zhi_shishen": shishen_between(day_gan, ZHI_MAIN_GAN.get(zhi, "")),
        "gan_wuxing": (GAN_INFO.get(gan) or ("",))[0],
        "zhi_main_wuxing": (GAN_INFO.get(ZHI_MAIN_GAN.get(zhi, "")) or ("",))[0],
    }


def format_liunian_line(detail: Optional[Dict[str, Any]]) -> str:
    """流年细节 → 一行事实（拼进命盘摘要，喂给 LLM 防编造干支）。"""
    if not isinstance(detail, dict) or not detail.get("ganzhi"):
        return ""
    return (f"所问{detail.get('year')}年流年：{detail.get('ganzhi')}"
            f"（天干对日主为{detail.get('gan_shishen') or '?'}、"
            f"地支主气为{detail.get('zhi_shishen') or '?'}）")


def format_dayun_line(chart: Dict[str, Any], *, max_steps: int = 5) -> str:
    """大运序列 → 一行事实（详批模式喂给 LLM，防它自己编后续大运走向）。"""
    dy = (chart or {}).get("dayun") or []
    if not dy:
        return ""
    steps = []
    for d in dy[: max(1, int(max_steps))]:
        steps.append(f"{d.get('ganzhi')}({d.get('start_age')}岁起)")
    return "大运序列：" + "、".join(steps)


def day_ganzhi(now_ts: Optional[float] = None) -> str:
    """某时刻（默认当下，本地时区）的日柱干支；缺库返回 ""。"""
    if not _LUNAR_OK:
        return ""
    try:
        lt = time.localtime(now_ts if now_ts is not None else time.time())
        return str(
            Solar.fromYmdHms(lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0)
            .getLunar().getDayInGanZhi()
        )
    except Exception:
        return ""


def current_jieqi(now_ts: Optional[float] = None) -> str:
    """当下所处节气名（最近已过的节气）；缺库/异常返回 ""。"""
    if not _LUNAR_OK:
        return ""
    try:
        lt = time.localtime(now_ts if now_ts is not None else time.time())
        lu = Solar.fromYmdHms(lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0).getLunar()
        jq = lu.getPrevJieQi(True)
        return str(jq.getName()) if jq else ""
    except Exception:
        return ""


def compute_bazi(info: BirthInfo, *, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """排盘：BirthInfo → 结构化命盘 dict；缺库/非法输入/内部异常 → None（软失败）。

    时辰未知：以正午 12:00 计（日柱在绝大多数情况下不受影响），**不输出时柱**、
    五行计数不含时柱两字、大运起运岁数标注近似。性别未知：不排大运。
    """
    if not _LUNAR_OK or not isinstance(info, BirthInfo) or not info.valid():
        return None
    ck = info.cache_key()
    cached = _CHART_CACHE.get(ck)
    if cached is not None:
        return cached
    try:
        hh = int(info.hour) if info.hour_known() else 12
        mm = int(info.minute) if info.hour_known() else 0
        if info.is_lunar:
            lunar = Lunar.fromYmdHms(int(info.year), int(info.month), int(info.day), hh, mm, 0)
        else:
            lunar = Solar.fromYmdHms(int(info.year), int(info.month), int(info.day), hh, mm, 0).getLunar()
        solar = lunar.getSolar()
        ec = lunar.getEightChar()

        pillars: Dict[str, Any] = {
            "year": _pillar(ec.getYearGan(), ec.getYearZhi(), ec.getYearShiShenGan(),
                            ec.getYearShiShenZhi(), ec.getYearNaYin()),
            "month": _pillar(ec.getMonthGan(), ec.getMonthZhi(), ec.getMonthShiShenGan(),
                             ec.getMonthShiShenZhi(), ec.getMonthNaYin()),
            "day": _pillar(ec.getDayGan(), ec.getDayZhi(), "日主",
                           ec.getDayShiShenZhi(), ec.getDayNaYin()),
        }
        if info.hour_known():
            pillars["time"] = _pillar(ec.getTimeGan(), ec.getTimeZhi(), ec.getTimeShiShenGan(),
                                      ec.getTimeShiShenZhi(), ec.getTimeNaYin())

        # 五行计数：干支各 1，月令（月支）双计以体现「得令」权重
        wuxing_strs = [ec.getYearWuXing(), ec.getMonthWuXing(), ec.getDayWuXing()]
        if info.hour_known():
            wuxing_strs.append(ec.getTimeWuXing())
        counts: Dict[str, float] = {"金": 0.0, "木": 0.0, "水": 0.0, "火": 0.0, "土": 0.0}
        for s in wuxing_strs:
            for ch in str(s):
                if ch in counts:
                    counts[ch] += 1.0
        month_zhi_wx = str(ec.getMonthWuXing())[-1]  # 月支五行（月令）
        if month_zhi_wx in counts:
            counts[month_zhi_wx] += 1.0
        total_weight = sum(counts.values())

        day_wx = str(ec.getDayWuXing())[0]  # 日主五行 = 日干五行
        strength = _judge_strength(day_wx, counts, total_weight)

        # 大运（需性别；阳男阴女顺排由库内处理）
        dayun_out: List[Dict[str, Any]] = []
        gender = str(info.gender or "").strip().lower()
        if gender in ("male", "female"):
            yun = ec.getYun(1 if gender == "male" else 0)
            for d in yun.getDaYun()[1:9]:  # [0] 为起运前区间，跳过；取 8 步足够
                dayun_out.append({
                    "ganzhi": d.getGanZhi(),
                    "start_year": d.getStartYear(),
                    "start_age": d.getStartAge(),
                })

        now = time.localtime(now_ts if now_ts is not None else time.time())
        chart: Dict[str, Any] = {
            "solar_date": solar.toYmd(),
            "lunar_date": f"{lunar.getYearInChinese()}年{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
            "hour_known": info.hour_known(),
            "gender": gender,
            "shengxiao": lunar.getYearShengXiaoByLiChun(),
            "day_master": f"{ec.getDayGan()}{day_wx}",
            "pillars": pillars,
            "wuxing_counts": {k: v for k, v in counts.items()},
            "strength": strength,
            "dayun": dayun_out,
            "current_dayun": _current_dayun(dayun_out, now.tm_year),
            "now_liunian": {"year": now.tm_year,
                            "ganzhi": liunian_ganzhi(now.tm_year, now.tm_mon, now.tm_mday)},
        }
        if len(_CHART_CACHE) >= _CHART_CACHE_MAX:
            _CHART_CACHE.clear()
        _CHART_CACHE[ck] = chart
        return chart
    except Exception:
        return None


def format_chart_summary(chart: Dict[str, Any]) -> str:
    """命盘 → 紧凑中文摘要（供 prompt 注入；~200-400 字符，信息密度优先）。"""
    if not isinstance(chart, dict) or not chart.get("pillars"):
        return ""
    p = chart["pillars"]
    parts: List[str] = []
    four = [p["year"]["ganzhi"], p["month"]["ganzhi"], p["day"]["ganzhi"]]
    if "time" in p:
        four.append(p["time"]["ganzhi"])
    else:
        four.append("时辰未知")
    parts.append(
        f"四柱：{' '.join(four)}（{chart.get('solar_date', '')} 生，属{chart.get('shengxiao', '')}）")
    parts.append(f"日主：{chart.get('day_master', '')}")
    ss = [f"年{p['year']['shishen_gan']}", f"月{p['month']['shishen_gan']}"]
    if "time" in p:
        ss.append(f"时{p['time']['shishen_gan']}")
    parts.append("透干十神：" + "、".join(ss))
    wx = chart.get("wuxing_counts") or {}
    wx_txt = " ".join(f"{k}{v:g}" for k, v in wx.items() if v > 0)
    missing = [k for k, v in wx.items() if v <= 0]
    if missing:
        wx_txt += f"（缺{''.join(missing)}）"
    st = chart.get("strength") or {}
    xy = st.get("xi_yong_candidates") or []
    xy_txt = ("、".join(xy)) if xy else "以流通为喜"
    parts.append(
        f"五行（月令双计）：{wx_txt}；日主{st.get('verdict', '?')}（粗判），喜用候选：{xy_txt}")
    cd = chart.get("current_dayun")
    if cd:
        approx = "" if chart.get("hour_known") else "约"
        parts.append(
            f"当前大运：{cd.get('ganzhi')}（{approx}{cd.get('start_age')}岁起）")
    ln = chart.get("now_liunian") or {}
    if ln.get("ganzhi"):
        parts.append(f"今年流年：{ln.get('year')} {ln.get('ganzhi')}")
    return "\n".join(parts)


__all__ = [
    "BirthInfo",
    "GAN_INFO",
    "ZHI_MAIN_GAN",
    "bazi_available",
    "compute_bazi",
    "current_jieqi",
    "day_ganzhi",
    "format_chart_summary",
    "format_dayun_line",
    "format_liunian_line",
    "liunian_detail",
    "liunian_ganzhi",
    "reset_chart_cache",
    "shishen_between",
]
