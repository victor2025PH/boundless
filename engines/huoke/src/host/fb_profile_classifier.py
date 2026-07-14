# -*- coding: utf-8 -*-
"""Facebook 目标画像识别器（2026-04-21 P2-4 Sprint A）。

两级识别：
    L1（规则）：
        * 输入：display_name / bio / username / locale（从 UI 可免费拿到）
        * 逻辑：按 config/fb_target_personas.yaml 里 persona.l1.rules 打分
        * 输出：score ∈ [0, 100]。阈值以下直接淘汰，不走 VLM。

    L2（VLM 深判）：
        * 输入：L1 通过的目标 + 已采集的截图（头像、封面、下滑主页）
        * 逻辑：调 ollama_vlm.classify_images 拿 {age_band, gender, is_japanese,
          topics, overall_confidence, ...}；与 persona.match_criteria 比对
        * 输出：match=True/False + 详细 insights_json

配额 / 去重 / 风控：
    * 每台设备每天 L1 1000 次 / L2 100 次硬上限。
    * 同一 target_key 在 dedup_window_hours 内不重复判定（直接复用结果）。
    * 最近 N 小时有风控提示，pause L2（只做 L1，保留审计）。

此模块**纯算法层**，不点屏幕/不截图。屏幕操作由 automation/facebook.py 提供，
本模块只消费传进来的文本 + 图片路径。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from src.host import fb_target_personas as fb_personas
# 2026-05-03 v28: 切换到智谱 GLM-4V-flash 云端视觉模型. 真机第 30-32 轮
# ollama 本地 6GB qwen2.5vl:7b 内存不足无法加载, 全部 L2 失败. 智谱云端
# API 不占本地资源, 延迟 ~4s, 调用稳定. zhipu_vlm.classify_images 与
# ollama_vlm 同签名, 零侵入切换.
from src.host import zhipu_vlm as ollama_vlm

logger = logging.getLogger(__name__)


# 2026-05-03 v27: VLM healthcheck 全局缓存. 真机第 30 轮显示 ollama 服务挂时
# 每候选 3 次重试浪费 ~10s, 20 候选累计超 task timeout 1200s 被 watchdog
# 杀. 第一次 classify_images 失败后, 5 分钟内全局降级跳过 VLM (直接走 L1
# fallback PASS), 节省 ~10s × N 候选 ≈ 数分钟. 5 分钟后自动重新探测一次,
# 让运营恢复 ollama 后无需重启 server 自动恢复.
_VLM_HEALTH = {
    "down_until": 0.0,
    "last_err": "",
    "down_count": 0,
}
_VLM_DOWN_TTL_SEC = 300.0   # 5 分钟


def _vlm_globally_down() -> bool:
    """是否处于 VLM 全局降级窗口期内."""
    return time.time() < _VLM_HEALTH["down_until"]


def _mark_vlm_down(err: str) -> None:
    """记录 VLM 故障, 触发 5 分钟全局降级."""
    _VLM_HEALTH["down_until"] = time.time() + _VLM_DOWN_TTL_SEC
    _VLM_HEALTH["last_err"] = (err or "")[:200]
    _VLM_HEALTH["down_count"] = int(_VLM_HEALTH.get("down_count") or 0) + 1
    logger.warning(
        "[vlm-health] 标记 VLM 全局降级 5 分钟 (累计 %d 次), err=%s",
        _VLM_HEALTH["down_count"], _VLM_HEALTH["last_err"],
    )


def _clear_vlm_down() -> None:
    """VLM 调用成功时清除 down 状态."""
    if _VLM_HEALTH["down_until"] > 0:
        logger.info("[vlm-health] VLM 恢复, 清除全局降级状态")
    _VLM_HEALTH["down_until"] = 0.0
    _VLM_HEALTH["last_err"] = ""

# 日文脚本检测（假名 / 汉字）
_HIRAGANA_RE = re.compile(r"[\u3040-\u309F]")
_KATAKANA_RE = re.compile(r"[\u30A0-\u30FF]")
_CJK_RE = re.compile(r"[\u4E00-\u9FFF]")
_LATIN_RE = re.compile(r"[A-Za-z]")


# ── L1 规则评分 ────────────────────────────────────────────────────

def _text_has_japanese(text: str) -> bool:
    if not text:
        return False
    return bool(_HIRAGANA_RE.search(text) or _KATAKANA_RE.search(text))


def _text_is_pure_japanese(text: str) -> bool:
    """昵称脚本判断：含假名/汉字 且 不含拉丁字母（防 ASCII-only id）。"""
    if not text:
        return False
    has_jp = _HIRAGANA_RE.search(text) or _KATAKANA_RE.search(text) or _CJK_RE.search(text)
    return bool(has_jp) and not _LATIN_RE.search(text)


def _apply_rule(rule: Dict[str, Any], ctx: Dict[str, str]) -> Tuple[bool, str]:
    """执行一条 L1 规则，返回 (命中, 理由)。"""
    kind = (rule.get("kind") or "").lower()
    name = ctx.get("display_name") or ""
    bio = ctx.get("bio") or ""
    username = ctx.get("username") or ""
    locale = ctx.get("locale") or ""

    if kind == "name_contains_any":
        vals = rule.get("value") or []
        hit = next((v for v in vals if v and v in name), None)
        return (True, f"昵称含 {hit}") if hit else (False, "")
    if kind == "name_contains_any_ci":
        # 不区分大小写、按词边界匹配（解决 "Miyuki Tanaka" 这类 romaji 日文名）
        vals = rule.get("value") or []
        name_l = name.lower()
        # 用正则 \b 避免 "ken" 误匹配 "token"
        for v in vals:
            if not v:
                continue
            if re.search(r"\b" + re.escape(v.lower()) + r"\b", name_l):
                return True, f"昵称含罗马字日文名 {v}"
        return False, ""
    if kind == "bio_contains_any":
        vals = rule.get("value") or []
        hit = next((v for v in vals if v and v in bio), None)
        return (True, f"bio 含 {hit}") if hit else (False, "")
    if kind == "name_script_japanese":
        ok = _text_is_pure_japanese(name)
        return (ok, "昵称为纯日文假名/汉字") if ok else (False, "")
    if kind == "bio_has_japanese":
        ok = _text_has_japanese(bio)
        return (ok, "bio 含日文假名") if ok else (False, "")
    if kind == "username_japanese_like":
        ok = bool(re.search(r"\.jp$|_jp$|jp\d", username or "")) or _text_has_japanese(username)
        return (ok, "username 带日本标识") if ok else (False, "")
    if kind == "locale_equals":
        want = (rule.get("value") or "").lower()
        ok = (locale or "").lower() == want
        return (ok, f"locale={locale}") if ok else (False, "")
    # 负向规则：命中时返回 True，score_l1 中 weight 为负数做减分
    if kind == "name_contains_any_negative":
        vals = rule.get("value") or []
        hit = next((v for v in vals if v and v in name), None)
        return (True, f"昵称含男性/非目标标识 {hit}") if hit else (False, "")
    if kind == "name_contains_any_ci_negative":
        vals = rule.get("value") or []
        name_l = name.lower()
        for v in vals:
            if not v:
                continue
            if re.search(r"\b" + re.escape(v.lower()) + r"\b", name_l):
                return True, f"昵称含非目标罗马字名 {v}"
        return False, ""
    if kind == "name_no_japanese_script":
        has_jp = _HIRAGANA_RE.search(name) or _KATAKANA_RE.search(name) or _CJK_RE.search(name)
        if not has_jp and _LATIN_RE.search(name):
            return True, "昵称为纯拉丁字母(无日文字符)"
        return False, ""

    logger.debug("未知 L1 规则: %s", rule)
    return False, ""


def score_l1(persona: Dict[str, Any], ctx: Dict[str, str]) -> Tuple[float, List[str]]:
    """对单个候选做 L1 打分。返回 (score, reasons[])。

    支持负权重规则：weight 为负数时命中即减分（用于男性/非目标名惩罚）。
    """
    rules = ((persona.get("l1") or {}).get("rules") or [])
    score = 0.0
    reasons: List[str] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        hit, why = _apply_rule(r, ctx)
        if hit:
            w = float(r.get("weight") or 0)
            score += w
            if why:
                sign = "+" if w >= 0 else ""
                reasons.append(f"{why}({sign}{int(w)})")
    score = max(0.0, min(100.0, score))
    return score, reasons


# ── L2 VLM 深判 ────────────────────────────────────────────────────

def _build_vlm_prompt(persona: Dict[str, Any], ctx: Dict[str, str]) -> str:
    tpl = persona.get("vlm_prompt") or ""
    name = ctx.get("display_name", "")
    bio = ctx.get("bio", "")[:400]
    prompt = tpl.replace("{persona_name}", persona.get("name") or "")
    if name or bio:
        prompt = prompt + f"\n\n参考情報:\n名前: {name}\nプロフィール: {bio}"
    return prompt


def _evaluate_match(persona: Dict[str, Any], insights: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """用 persona.match_criteria 判断 VLM 结果是否命中。

    Phase 10.1 (2026-04-24): 加 ``require_has_face`` / ``require_is_profile_page``
    早期 reject — 防 VLM 在 launcher / 设置 / 空白 / 非 profile 截图上瞎猜
    age/gender 误判 PASS (partial smoke 实测 launcher 截图也判 95.5).
    """
    crit = persona.get("match_criteria") or {}
    reasons: List[str] = []
    ok = True

    # Phase 10.1: 早期 face / profile page 门禁 (默认 backward-compat)
    # 如果 persona 没设 require_has_face 字段, 跳过此检查 (现有 persona 不变行为)
    if crit.get("require_has_face"):
        if not insights.get("has_face"):
            return False, ["VLM 判定 has_face=false (没看到清晰人脸 / 非 profile 截图)"]
    if crit.get("require_is_profile_page"):
        if not insights.get("is_profile_page"):
            return False, ["VLM 判定 is_profile_page=false (不是 FB/IG profile UI)"]

    age_band = (insights.get("age_band") or "").lower()
    allowed_bands = [b.lower() for b in (crit.get("age_bands_allowed") or [])]
    if allowed_bands and age_band not in allowed_bands:
        ok = False
        reasons.append(f"年龄段 {age_band} 不在允许范围 {allowed_bands}")

    gender = (insights.get("gender") or "").lower()
    allowed_g = [g.lower() for g in (crit.get("genders_allowed") or [])]
    if allowed_g and gender not in allowed_g:
        ok = False
        reasons.append(f"性别 {gender} 不在允许范围 {allowed_g}")

    if crit.get("require_is_japanese"):
        if not insights.get("is_japanese"):
            ok = False
            reasons.append("VLM 判定 is_japanese=false")
        else:
            jc = float(insights.get("is_japanese_confidence", 0) or 0)
            if jc < float(crit.get("min_japanese_confidence", 0.5)):
                ok = False
                reasons.append(f"is_japanese 置信度 {jc:.2f} 过低")

    oc = float(insights.get("overall_confidence", 0) or 0)
    if oc < float(crit.get("min_overall_confidence", 0.55)):
        ok = False
        reasons.append(f"overall_confidence {oc:.2f} 过低")

    if ok:
        reasons.append(f"age_band={age_band}, gender={gender}, ja_conf>={crit.get('min_japanese_confidence')}")
    return ok, reasons


def _score_l2(insights: Dict[str, Any], passed: bool) -> float:
    """L2 得分：命中 70 分 + overall_confidence * 30。"""
    if not passed:
        return 0.0
    oc = float(insights.get("overall_confidence", 0) or 0)
    return min(100.0, 70.0 + 30.0 * oc)


# ── 数据库交互 ─────────────────────────────────────────────────────

def _db_count_today(device_id: str, stage: str) -> int:
    try:
        from src.host.database import get_conn
    except Exception:
        return 0
    try:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM fb_profile_insights
                   WHERE device_id=? AND stage=? AND classified_at >= date('now', 'localtime')""",
                (device_id, stage),
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_today 失败: %s", e)
        return 0


def _db_count_recent_hours(device_id: str, stage: str, hours: int) -> int:
    """Sprint C-1: 近 N 小时内该 device_id + stage 的判定次数（小时配额用）。"""
    try:
        from src.host.database import get_conn
    except Exception:
        return 0
    try:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM fb_profile_insights
                   WHERE device_id=? AND stage=?
                     AND classified_at >= datetime('now', ?)""",
                (device_id, stage, f"-{max(1, int(hours))} hours"),
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_recent_hours 失败: %s", e)
        return 0


def _db_get_recent(persona_key: str, target_key: str, window_hours: int) -> Optional[Dict[str, Any]]:
    try:
        from src.host.database import get_conn
    except Exception:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT stage, match, score, insights_json, classified_at
                   FROM fb_profile_insights
                   WHERE persona_key=? AND target_key=?
                     AND classified_at >= datetime('now', ?)
                   ORDER BY id DESC LIMIT 1""",
                (persona_key, target_key, f"-{int(window_hours)} hours"),
            ).fetchone()
        if not row:
            return None
        return {
            "stage": row[0], "match": bool(row[1]), "score": float(row[2]),
            "insights": json.loads(row[3] or "{}"), "at": row[4],
        }
    except Exception as e:
        logger.debug("get_recent 失败: %s", e)
        return None


def _db_insert_insight(**kw) -> Optional[int]:
    try:
        from src.host.database import get_conn
    except Exception:
        return None
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO fb_profile_insights (
                    device_id, task_id, persona_key, target_key, display_name,
                    stage, match, score, confidence, insights_json, image_paths,
                    vlm_model, latency_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    kw.get("device_id", ""), kw.get("task_id", ""),
                    kw.get("persona_key", ""), kw.get("target_key", ""),
                    kw.get("display_name", ""),
                    kw.get("stage", "L1"),
                    1 if kw.get("match") else 0,
                    float(kw.get("score", 0)),
                    float(kw.get("confidence", 0)),
                    json.dumps(kw.get("insights", {}) or {}, ensure_ascii=False),
                    json.dumps(kw.get("image_paths", []) or []),
                    kw.get("vlm_model", ""),
                    int(kw.get("latency_ms", 0)),
                ),
            )
            return cur.lastrowid
    except sqlite3.Error as e:
        logger.warning("insert_insight 失败: %s", e)
        return None


def _normalize_interest(topic: str) -> str:
    """把 VLM 返回的 interest 字符串标准化（去空格、lower、截断），
    便于聚合统计（"yoga / Yoga/ yoga " → "yoga"）。"""
    if not topic:
        return ""
    s = str(topic).strip().lower()
    # 折叠连续空白
    s = " ".join(s.split())
    return s[:48]


def _db_insert_content_exposure(
    *, device_id: str, task_id: str, persona_key: str,
    target_key: str, display_name: str, insights: Dict[str, Any],
) -> int:
    """Sprint D-2: 把 L2 命中用户的 VLM interests 写入 fb_content_exposure 表。

    逻辑：
        · VLM 返回的 insights.interests 可能是 list[str]、str、或其他
        · 拆成多行 topic 入库（每个兴趣一行）
        · dwell_ms=0, liked=0 表示"仅看到，未行为"
        · meta_json 存回溯字段：{target_key, display_name, persona_key, confidence}
        · 返回：成功写入的条数
    """
    raw_interests = (insights or {}).get("interests")
    topics_list: List[str] = []
    if isinstance(raw_interests, list):
        topics_list = [str(x) for x in raw_interests if x]
    elif isinstance(raw_interests, str):
        topics_list = [s.strip() for s in raw_interests.replace("、", ",").split(",") if s.strip()]
    topics_norm = [_normalize_interest(t) for t in topics_list if _normalize_interest(t)]
    # 去重保序
    seen: set = set()
    topics: List[str] = []
    for t in topics_norm:
        if t not in seen:
            seen.add(t)
            topics.append(t)
    if not topics:
        topics = ["other"]   # 没识别到 interests 时兜底一行，保持"看过"事实

    lang = str((insights or {}).get("language") or (insights or {}).get("lang") or "")
    conf = float((insights or {}).get("overall_confidence") or 0)
    meta = {
        "target_key": target_key,
        "display_name": display_name[:60],
        "persona_key": persona_key,
        "confidence": round(conf, 3),
        "source": "fb_profile_l2_match",
    }
    try:
        from src.host.database import get_conn
    except Exception:
        return 0
    written = 0
    try:
        with get_conn() as conn:
            for t in topics[:8]:  # 上限 8 条，避免垃圾
                conn.execute(
                    """INSERT INTO fb_content_exposure
                       (device_id, task_id, topic, lang, liked, dwell_ms, meta_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (device_id, task_id, t, lang, 0, 0,
                     json.dumps(meta, ensure_ascii=False)),
                )
                written += 1
    except sqlite3.Error as e:
        logger.warning("insert_content_exposure 失败: %s", e)
    return written


def _recent_risk_hours(device_id: str, hours: int) -> int:
    """最近 N 小时的 **CRITICAL 级** 风控事件数, 用于 pause_l2 判断.

    2026-04-24 v2: 之前查所有 risk events (包含 content_blocked 'other' 类),
    导致一次 greeting 被 FB 拒发就 pause L2 12 小时, 过严.
    改为只查 CRITICAL (identity_verify / captcha / checkpoint / account_review /
    policy_warning) — 这些才真正意味着账号有长期风险, 需要冷却.
    """
    try:
        from src.host import fb_store
        return int(fb_store.count_critical_risk_events_recent(device_id, hours))
    except Exception:
        return 0


# ── 对外编排 ──────────────────────────────────────────────────────

def classify(
    *,
    device_id: str,
    task_id: str = "",
    persona_key: Optional[str] = None,
    target_key: str,                 # profile_url / user_id / username，唯一标识
    display_name: str = "",
    bio: str = "",
    username: str = "",
    locale: str = "",
    image_paths: Optional[List[str]] = None,  # L1 截图（头像等），可选
    l2_image_paths: Optional[List[str]] = None,  # L2 深判用（主页滚动截图）
    do_l2: bool = True,
    dry_run: bool = False,
    force_reclassify: bool = False,  # Phase 10.2: True 时跳过 7 天去重缓存, 强制重判
) -> Dict[str, Any]:
    """对单个目标做 L1+L2 分级识别。

    返回结构:
        {
            "match": bool, "stage_reached": "L1" | "L2",
            "l1": {"score": float, "reasons": [...]},
            "l2": {...} | None,
            "insights": {...},          # L2 的 VLM 原始 JSON
            "from_cache": bool,         # 是否复用去重窗口内结果
            "quota": {"l1_used": int, "l2_used": int, "exceeded": str | None},
            "persona_key": str,
        }
    """
    image_paths = image_paths or []
    l2_image_paths = l2_image_paths or image_paths  # L2 默认复用 L1 图

    # Sprint E-0.1 (ext): classifier 入口 warmup。
    # 若 warmup 未 fresh → 同步跑一次（~6s），消掉"冷启动 + 多图推理耦合"
    # 导致的 30s×3 timeout 重试循环（真机实测 90s 全失败 → warmup 后约 15s 成功）。
    # 若已 fresh → 立刻返回，无额外开销。
    try:
        _wst = ollama_vlm.get_warmup_state()
        if not _wst.get("fresh"):
            # 只在有 L2 图、真要跑 VLM 时同步 warmup；纯 L1 路径不花这笔开销
            if do_l2 and l2_image_paths:
                ollama_vlm.warmup(force=False)
        else:
            # 已 fresh，顺手 fire 一次 async 续命（幂等 TTL）
            ollama_vlm.warmup_async(force=False)
    except Exception:
        pass
    persona = fb_personas.get_persona(persona_key)
    pk = persona["persona_key"]
    quotas = fb_personas.get_quotas()
    guard = fb_personas.get_risk_guard()
    dedup_h = fb_personas.get_dedup_window_hours()

    ctx = {"display_name": display_name, "bio": bio, "username": username, "locale": locale}

    # 0) 去重：窗口内已判过 → 复用 (force_reclassify=True 时跳过, Phase 10.2)
    # use case: peer 真改头像 / 上次 launcher 截图被 REJECT 想用真 profile 截图重判 / debug
    cached = None if force_reclassify else _db_get_recent(pk, target_key, dedup_h)
    if cached and not dry_run:
        logger.info("[classifier] 命中去重缓存 target=%s persona=%s match=%s",
                    target_key, pk, cached["match"])
        return {
            "match": cached["match"],
            "stage_reached": cached["stage"],
            "l1": None,
            "l2": None,
            "insights": cached.get("insights") or {},
            "from_cache": True,
            "quota": {"l1_used": 0, "l2_used": 0, "exceeded": None},
            "persona_key": pk,
            "score": cached["score"],
        }

    # 1) L1
    l1_used = _db_count_today(device_id, "L1") if not dry_run else 0
    l1_cap = int(quotas.get("l1_per_device_per_day") or 1000)
    if not dry_run and l1_used >= l1_cap:
        return {
            "match": False, "stage_reached": "none", "l1": None, "l2": None,
            "insights": {}, "from_cache": False,
            "quota": {"l1_used": l1_used, "l2_used": _db_count_today(device_id, "L2"),
                      "exceeded": "l1_daily_cap"},
            "persona_key": pk, "score": 0.0,
        }

    l1_score, l1_reasons = score_l1(persona, ctx)
    l1_pass_th = float((persona.get("l1") or {}).get("pass_threshold") or 30)
    l1_pass = l1_score >= l1_pass_th

    result: Dict[str, Any] = {
        "match": False,
        "stage_reached": "L1",
        "l1": {"score": l1_score, "reasons": l1_reasons, "pass_threshold": l1_pass_th, "pass": l1_pass},
        "l2": None,
        "insights": {},
        "from_cache": False,
        "quota": {"l1_used": l1_used, "l2_used": 0, "exceeded": None},
        "persona_key": pk,
        "score": l1_score,
    }

    # L1 未过或显式跳过 L2 → 落 L1 记录返回
    # 2026-04-24 修: match 要跟随 l1_pass. 之前固定 match=False, 导致 do_l2=False
    #   时即使 L1 通过, DB 也存了 match=False, 下次缓存命中误拦截后续 add_friend.
    if not l1_pass or not do_l2:
        result_match = bool(l1_pass) if (l1_pass and not do_l2) else False
        result["match"] = result_match
        if not dry_run:
            _db_insert_insight(
                device_id=device_id, task_id=task_id, persona_key=pk,
                target_key=target_key, display_name=display_name,
                stage="L1", match=result_match, score=l1_score, confidence=0.0,
                insights={"l1_reasons": l1_reasons}, image_paths=image_paths,
                vlm_model="", latency_ms=0,
            )
        return result

    # 2) L2 前置检查：配额（日/小时） + 风控
    l2_used = _db_count_today(device_id, "L2") if not dry_run else 0
    l2_cap = int(quotas.get("l2_per_device_per_day") or 100)
    result["quota"]["l2_used"] = l2_used
    if not dry_run and l2_used >= l2_cap:
        result["quota"]["exceeded"] = "l2_daily_cap"
        if not dry_run:
            _db_insert_insight(
                device_id=device_id, task_id=task_id, persona_key=pk,
                target_key=target_key, display_name=display_name,
                stage="L1", match=False, score=l1_score, confidence=0.0,
                insights={"l1_reasons": l1_reasons, "l2_skip": "quota_exceeded"},
                image_paths=image_paths, vlm_model="", latency_ms=0,
            )
        return result

    # Sprint C-1: 小时上限（防止一次性烧满 GPU）
    l2_per_hour = int(quotas.get("l2_per_device_per_hour") or 0)
    if not dry_run and l2_per_hour > 0:
        l2_used_hour = _db_count_recent_hours(device_id, "L2", hours=1)
        result["quota"]["l2_used_hour"] = l2_used_hour
        if l2_used_hour >= l2_per_hour:
            result["quota"]["exceeded"] = "l2_hourly_cap"
            _db_insert_insight(
                device_id=device_id, task_id=task_id, persona_key=pk,
                target_key=target_key, display_name=display_name,
                stage="L1", match=False, score=l1_score, confidence=0.0,
                insights={"l1_reasons": l1_reasons, "l2_skip": "hourly_cap"},
                image_paths=image_paths, vlm_model="", latency_ms=0,
            )
            return result

    pause_hours = int(guard.get("pause_l2_after_risk_hours") or 0)
    if pause_hours > 0 and _recent_risk_hours(device_id, pause_hours) > 0:
        result["quota"]["exceeded"] = "l2_paused_by_risk"
        if not dry_run:
            _db_insert_insight(
                device_id=device_id, task_id=task_id, persona_key=pk,
                target_key=target_key, display_name=display_name,
                stage="L1", match=False, score=l1_score, confidence=0.0,
                insights={"l1_reasons": l1_reasons, "l2_skip": "risk_pause"},
                image_paths=image_paths, vlm_model="", latency_ms=0,
            )
        return result

    # 3) L2 VLM
    # v27 healthcheck: ollama 全局降级窗口内不调 classify_images, 立即走 L1
    # fallback. 节省每候选 ~10s 重试时间.
    if _vlm_globally_down():
        insights = {}
        meta = {
            "ok": False,
            "error": (
                f"vlm_globally_down (last_err={_VLM_HEALTH['last_err'][:80]})"
            ),
            "model": "vlm_globally_down",
        }
        latency_ms = 0
    else:
        prompt = _build_vlm_prompt(persona, ctx)
        t0 = time.time()
        insights, meta = ollama_vlm.classify_images(
            prompt=prompt,
            image_paths=l2_image_paths,
            scene="fb_profile_l2",
            task_id=task_id,
            device_id=device_id,
        )
        latency_ms = int((time.time() - t0) * 1000)
        # v27: 调用结果反馈 healthcheck 状态
        if meta.get("ok") and insights:
            _clear_vlm_down()
        elif meta.get("error") or meta.get("parse_error"):
            _mark_vlm_down(
                str(meta.get("error") or meta.get("parse_error") or "")
            )

    # 2026-05-03 v24: VLM 不可用 (Ollama 未启动 / 模型未加载) 时 healthy
    # degradation — 真机第 25-28 轮显示 ollama 服务挂了, 所有 L2 调用 insights
    # 空, passed=False, score=0, 100% reject. 但 L1 (姓名启发式) 已经能给出
    # 合理判别. fallback: 当 insights 空 + meta 含 vlm_error 时, 用 L1 score
    # 作为 L2 score, 高分 (>=50) 直接 PASS, 让 L1-strong 候选不被 VLM 故障拖
    # 累. 运营恢复 ollama 后行为自动回归原样 (insights 非空走原路径).
    _vlm_error = meta.get("error") or meta.get("parse_error", "")
    _vlm_ok = bool(meta.get("ok", False)) and bool(insights)
    if not _vlm_ok and _vlm_error:
        # VLM 故障降级 — v25 (2026-05-03): 第 29 轮看到 candidate l1 普遍 20
        # (FB 罗马字日文姓 = 15 + bio_has_japanese 等 = 20). 阈值 50 永远不达.
        # 改为复用 yaml l1_pass_threshold=20 (本来 L1 pass 就该进 L2). 等价于:
        # "L1 已判合格 + VLM 不可用 → 视同 PASS, 不让 VLM 故障拖累已 L1-pass
        # 的候选". 比硬编码 50 更尊重 yaml 配置.
        _l1_qualified = bool(l1_pass)   # l1_score >= l1_pass_th
        passed = _l1_qualified
        match_reasons = [
            f"vlm_unavailable_l1_fallback (l1={l1_score}, "
            f"l1_pass_th={l1_pass_th}, qualified={_l1_qualified}, "
            f"vlm_err={_vlm_error[:60]})"
        ]
        insights = insights or {}
        l2_score = float(l1_score) if _l1_qualified else 0.0
        confidence = 0.0
        logger.warning(
            "[classify] VLM 故障 L1 降级: name=%r l1=%d/%d passed=%s vlm_err=%s",
            display_name, l1_score, int(l1_pass_th), passed,
            _vlm_error[:80],
        )
    else:
        passed, match_reasons = (
            _evaluate_match(persona, insights) if insights
            else (False, ["VLM 返回为空"])
        )
        l2_score = _score_l2(insights, passed)
        confidence = float(insights.get("overall_confidence", 0) or 0)

    result["stage_reached"] = "L2"
    result["l2"] = {
        "ok": meta.get("ok", False),
        "latency_ms": latency_ms,
        "model": meta.get("model", ""),
        "score": l2_score,
        "passed": passed,
        "match_reasons": match_reasons,
        "vlm_error": meta.get("error") or meta.get("parse_error", ""),
    }
    result["insights"] = insights or {}
    result["match"] = passed
    result["score"] = l2_score

    if not dry_run:
        _db_insert_insight(
            device_id=device_id, task_id=task_id, persona_key=pk,
            target_key=target_key, display_name=display_name,
            stage="L2", match=passed, score=l2_score, confidence=confidence,
            insights={**(insights or {}), "l1_reasons": l1_reasons,
                      "match_reasons": match_reasons},
            image_paths=l2_image_paths, vlm_model=meta.get("model", ""),
            latency_ms=latency_ms,
        )
        # Sprint D-2: L2 命中时把 VLM 看到的 interests 写入 content_exposure，
        # 用于后续「相似兴趣帖子自动点赞」做温和触达（只记"看到"，dwell=0 liked=0）。
        if passed:
            try:
                _db_insert_content_exposure(
                    device_id=device_id, task_id=task_id,
                    persona_key=pk, target_key=target_key,
                    display_name=display_name, insights=insights or {},
                )
            except Exception as _e:
                logger.debug("content_exposure 写入失败（忽略）: %s", _e)

    return result
