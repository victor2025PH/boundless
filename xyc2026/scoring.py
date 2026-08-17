# Rule-based scoring engine (MVP). Pure functions: features -> score, level, tips.
# Weights and thresholds are configurable for future A/B and model replacement.
from datetime import date, datetime, timedelta
from typing import Any

# Risk level thresholds (score 0-100): extreme / high / medium / low
THRESHOLDS = {"extreme": (0, 35), "high": (36, 55), "medium": (56, 75), "low": (76, 100)}

# Weights for internal 300-900 style (optional); MVP we use 0-100 directly
DEFAULT_WEIGHTS = {
    "account_age": 0.20,
    "premium": 0.10,
    "high_risk_groups": -0.25,
    "blacklist": -0.40,
    "report_count": -0.15,
    "base": 50,
}


def _months_since(d: date | None) -> float:
    if not d:
        return 0.0
    today = date.today()
    return max(0, (today - d).days / 30.0)


def _score_account_age(register_date: date | None) -> float:
    """Older account -> higher score. New account (<3 months) penalized."""
    months = _months_since(register_date)
    if months >= 24:
        return 1.0
    if months >= 12:
        return 0.8
    if months >= 6:
        return 0.6
    if months >= 3:
        return 0.4
    return 0.1  # <3 months -> "新账号"


def _score_premium(is_premium: bool) -> float:
    return 1.0 if is_premium else 0.5


def _score_high_risk_groups(count: int) -> float:
    """0 groups = 1, many = 0."""
    if count == 0:
        return 1.0
    if count >= 10:
        return 0.0
    return max(0, 1.0 - count / 10.0)


def _score_blacklist(blacklist: bool) -> float:
    return 0.0 if blacklist else 1.0


def _score_report_count(report_count: int) -> float:
    if report_count == 0:
        return 1.0
    if report_count >= 5:
        return 0.0
    return max(0, 1.0 - report_count / 5.0)


def compute_score_and_tips(
    register_date: date | None = None,
    is_premium: bool = False,
    high_risk_group_count: int = 0,
    blacklist: bool = False,
    report_count: int = 0,
    effective_report_count: float | None = None,
    weights: dict[str, Any] | None = None,
) -> tuple[int, str, list[str]]:
    """
    Returns (risk_score 0-100, risk_level, tips).
    P2: effective_report_count 为按举报人可信度加权后的「有效举报数」；若不传则用 report_count。
    """
    w = weights or DEFAULT_WEIGHTS
    base = w.get("base", 50)
    report_val = (effective_report_count if effective_report_count is not None else report_count)
    report_val = max(0, min(report_val, 100))  # 限制范围，避免异常值

    s_age = _score_account_age(register_date) * (w.get("account_age", 0.2) * 100)
    s_premium = _score_premium(is_premium) * (w.get("premium", 0.1) * 100)
    s_groups = _score_high_risk_groups(high_risk_group_count) * abs(w.get("high_risk_groups", -0.25)) * 100
    s_black = _score_blacklist(blacklist) * abs(w.get("blacklist", -0.40)) * 100
    s_report = _score_report_count(int(round(report_val))) * abs(w.get("report_count", -0.15)) * 100

    # When register_date is None (no data), keep neutral so score stays at base
    age_term = (s_age - 10) if register_date is not None else 0
    raw = (
        base
        + age_term
        + (s_premium - 5)
        - (1 - _score_high_risk_groups(high_risk_group_count)) * 25
        - (40 if blacklist else 0)
        - (1 - _score_report_count(int(round(report_val)))) * 15
    )
    score = max(0, min(100, int(round(raw))))

    if score <= 35:
        level = "extreme"
    elif score <= 55:
        level = "high"
    elif score <= 75:
        level = "medium"
    else:
        level = "low"

    tips = []
    if _months_since(register_date) < 3 and register_date:
        tips.append("New account (<3 months)")
    if high_risk_group_count >= 1:
        tips.append(f"High-risk group exposure ({high_risk_group_count})")
    if blacklist:
        tips.append("Listed on blacklist")
    if report_count > 0:
        tips.append(f"Reported by {report_count} user(s)")
    if is_premium:
        tips.append("Telegram Premium")
    if not tips:
        tips.append("No special risk indicators")

    return score, level, tips


def compute_risk_factor_breakdown(
    register_date: date | None = None,
    is_premium: bool = False,
    high_risk_group_count: int = 0,
    blacklist: bool = False,
    report_count: int = 0,
    linked_tg_count: int = 0,
) -> dict[str, int]:
    """
    V2.0: 返回各风险因子占比（百分比，总和 100）。用于报告「风险来源结构」展示。
    report / blacklist / high_risk_groups / new_account / wallet_anomaly；无风险时返回均衡或全 0。
    """
    s_report = _score_report_count(report_count)
    s_groups = _score_high_risk_groups(high_risk_group_count)
    age_term = (_score_account_age(register_date) * 20 - 10) if register_date is not None else 0
    contrib_report = (1 - s_report) * 15
    contrib_blacklist = 40 if blacklist else 0
    contrib_groups = (1 - s_groups) * 25
    contrib_new_account = max(0, -age_term) if register_date is not None else 0
    contrib_wallet = 10 if linked_tg_count > 1 else 0
    total = contrib_report + contrib_blacklist + contrib_groups + contrib_new_account + contrib_wallet
    if total <= 0:
        return {"report": 0, "blacklist": 0, "high_risk_groups": 0, "new_account": 0, "wallet_anomaly": 0}
    return {
        "report": max(0, int(round(100 * contrib_report / total))),
        "blacklist": max(0, int(round(100 * contrib_blacklist / total))),
        "high_risk_groups": max(0, int(round(100 * contrib_groups / total))),
        "new_account": max(0, int(round(100 * contrib_new_account / total))),
        "wallet_anomaly": max(0, int(round(100 * contrib_wallet / total))),
    }
