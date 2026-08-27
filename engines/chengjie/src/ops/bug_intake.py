# -*- coding: utf-8 -*-
"""报障群 AI 值守（bug_intake，2026-08-18）。

场景：官方 Telegram 报障群（如 @BUGTIJIAO）由专用支持账号值守——用户在群里
报 bug / 问用法，AI 判类、答疑、登记工单、限频、分级告警；坐席在统一收件箱
「群组动态」随时可人工接管。

设计要点（改动前先读）：
- **单模块收口**：分类词表 / 限频 / 工单台账 / 触发三态 / prompt 块 / 回执
  footer / 告警发布全在本模块；四个消费点各只插一小段（trigger.py 群触发顶部
  三态、sender.py 语音压制、skill_manager 观察+注入+footer、ai_client 块消费），
  配置 ``bug_intake.enabled=false``（出货默认）或群不在 ``groups`` 里 = 全链
  no-op，绝不伤既有群逻辑（Katie 群演三群走原路径零变更）。
- **触发三态**（``trigger_verdict``）：None=不是报障群/无信号，走原有触发链
  （@提及/回复链/追问窗照旧）；True=报障关键词或收集窗内消息，引燃回复；
  False=限频超额 / 危机词，**硬压制**（连 @提及也不回——刷屏比漏回伤害大）。
- **误判代价不对称**：bug 词优先于用法词（把真 bug 当用法问题打发掉，比把
  用法问题记成工单严重得多）；拿不准（other）不引燃、不登记，留给 @提及。
- **回执 footer 是代码拼的**（``ticket_footer``）：LLM 复述编号会抄错/漏写，
  工单号必须确定性出现（skill_manager 在媒体承诺守卫后追加）。
- **台账**＝``<config_dir>/bug_intake.db``（licensing.data_paths 契约，测试经
  AITR_DATA_DIR 落 tmp）；查重＝同群 7 天内开放工单标题相似度 ≥0.75（difflib，
  归一化后），命中并单 ``report_count+1``——报告人数本身是优先级信号。
- **告警**：新 P0/P1 工单 / 危机词 → EventBus ``bug_intake_alert``（webhook
  订阅别名 ``bug_intake``，technical 受众；formatter 分支在 webhook_notifier）
  + logger.warning 落日志兜底可见。P2 级工单刻意不逐条告警（防轰炸）。
  （P2 升级注：首日曾借道 host_alert 避开 cp-i18n.js 双树镜像施工面；镜像门禁
  转绿后已切回专属别名——运营在告警渠道面板可单独订阅报障群事件。）
- **修复回访**（P2）：工单标 ``fixed`` → 群里 @报障人请其验证（文案
  ``build_fix_notify_text``，发送在路由层走该账号 worker 的 pyro client）；
  ``notify_ts``/``notify_note`` 记账，未送达可经 ``/notify`` 端点重试，
  积压批量冲刷走 ``/notify-pending``（worker 离线期标 fixed 的单）。
- **verified 自动闭环**（P3）：报障人在**已回访**（notify_ts>0）的 fixed 单上
  群里回确认（``detect_verify_intent``——「好了/可以了」= verified 关单，
  「还是不行」= 重开回 confirmed 并**重新拉起收集窗**让 TA 描述新表现）；
  疑问句（带 ？/吗）宁可不判——「修复了吗」是追问不是确认。
- **截图收集**（P3）：收集窗内的图片消息由 trigger 层 ``note_screenshot``
  记进工单（[截图已收到]），纯图无文字**刻意不引燃 AI 回复**（空文本走
  A 线是未经验证的路径，记录比接话重要）。
- **遥测快照**（P2）：新工单落库时 best-effort 附内部遥测摘要（前端哑按钮错误
  Top + 近 2h ops 事件分布）——工程师拿单即有第一现场线索，失败静默不阻塞。
- **语音压制**（``voice_suppressed``）：报障群 / 支持账号一律不发语音——
  sender 的 voice_reply 闸读全局配置，支持人设没有克隆声，放行会落到全局
  voice_profile 的陪伴参考音（错声=「换人」级穿帮）。

门禁 tests/test_bug_intake.py。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 词表（保守：宁可漏引燃，@提及兜底；bug 优先于 usage）─────────────────────
BUG_WORDS = (
    "报错", "闪退", "崩溃", "卡死", "白屏", "黑屏", "打不开", "发不出", "收不到",
    "失败", "异常", "无法", "用不了", "不能用", "死机", "掉线", "断线", "丢失",
    "重复发", "双发", "乱码", "没反应", "点不了", "点了没", "不显示", "显示不",
    "出错", "错误", "不工作", "失灵", "卡住", "转圈", "加载不", "登录不上",
    "登不上", "连不上", "bug", "error", "crash", "freeze", "broken", "not working",
    "stuck", "failed",
    # 「设置不生效」族（2026-08-20 实测漏网：「默认全局人设，设定完毕之后，
    # 登陆的账号人设没有更改过来」被判 other → 新压制逻辑当闲聊静默）
    "没有生效", "没生效", "不生效", "没有更改", "没更改", "改不过来",
    "没有变化", "没有改过来", "设置无效", "设定无效", "无效",
    # 「界面异常/操作未成」族（2026-08-20 同日实测漏网：「客户界面消失，填写
    # 档案的界面也没有填写成功，排查原因」）
    "消失", "不见", "排查", "没有成功", "不成功", "没成功", "空白",
    # 「未解决」跟进族（同日五轮：「这个问题没有解决」——报障群最高频的
    # 跟进句式，压制它=对用户装死）
    "没有解决", "没解决", "还没解决", "未解决", "还是不行", "还是老样子",
    "问题依旧", "还是一样",
    # 「求分析」指令族（同日六轮：「分析」「这个问题现在分析排查」——本群报告人
    # 的高频口头指令，静默=晾着用户 4.5 小时的实录）
    "分析", "帮我看", "看一下这个", "查一下",
    # 第七轮（2026-08-21 凌晨实录，5 条漏答被闸门压制）：「根本没法再点」
    # （没法=无法的口语变体）、「只拟稿没有自动发出」「所以没有触发」
    "没法", "没有自动", "没有触发",
)
USAGE_WORDS = (
    "怎么", "如何", "怎样", "在哪", "哪里", "哪儿", "什么意思", "教程", "教一下",
    "请问", "支持吗", "能不能", "可不可以", "有没有办法", "how do", "how to",
    "where is", "can i", "什么区别", "是什么",
    # 功能发现型（2026-08-19 实测漏网：「新建人设时看不到语音克隆功能」未引燃）
    "看不到", "找不到", "不见了", "没有这个", "没找到",
)
# 产品建议/体验反馈型（2026-08-20 内测群实测三连漏网：「功能导向不明，建议隐藏
# 或删除」「实时日志…用户端不实用」「开发者工具…建议关闭」全被判 other →
# 新压制逻辑当闲聊静默。内测群高频形态，独立分类＝独立应答口径（确认+登记+
# 感谢，不承诺采纳/时间，不争辩）。
FEEDBACK_WORDS = (
    "建议", "不实用", "导向不明", "没必要", "多余", "不需要开放", "用不上",
    "希望能", "希望增加", "希望支持", "最好能", "应该隐藏", "应该关闭",
    "体验不好", "不好用", "太复杂", "suggest", "feature request",
    # 2026-08-20 四轮：「这个逻辑不对…非常影响用户体验」仍漏（用户在纠正
    # 产品设计而非报故障——典型内测反馈语体）
    "用户体验", "逻辑不对", "设计不合理", "不应该消失", "不该消失",
    # 2026-08-21 五轮（凌晨漏答实录）：「该段提示可以删除…不需要」「操作设置
    # 增加了操作的难度，逻辑上也不合理」「这个操作逻辑有问题」
    "可以删除", "不合理", "逻辑有问题", "增加了操作的难度",
)
CRISIS_WORDS = (
    "退款", "骗子", "骗人", "诈骗", "报警", "投诉到底", "卖了我们", "数据泄露",
    "曝光你们", "scam", "fraud", "refund",
)
P0_WORDS = (
    "崩溃", "闪退", "全部", "都不能", "数据丢", "丢失", "发错人", "封号", "被封",
    "打不开", "起不来", "全挂", "没了", "crash", "data loss",
)
P1_WORDS = (
    "发不出", "收不到", "卡死", "白屏", "登录不上", "登不上", "连不上", "掉线",
    "断线", "没反应", "重复发", "双发",
)

# 触发引燃需要「像在说产品问题」——单独一个「失败」也可能是闲聊；
# 引燃词 = bug/usage 词表 + 显式喊支持（群成员点显示名 @ 出的是 text_mention，
# _contains_mention_of_self 认不出 @username，这里按显示名兜住）。
ENGAGE_EXTRA = ("智聊支持", "官方支持", "客服", "支持人员")

_COLLECT_TTL_DEFAULT_MIN = 30

# ── 进程态（限频 / 收集窗 / 计数器）────────────────────────────────────────────
_LOCK = threading.Lock()
_RATE: Dict[str, List[float]] = {}          # "u:<chat>:<user>" / "g:<chat>" → ts 列表
_COLLECT: Dict[str, Dict[str, Any]] = {}    # "<chat>:<user>" → {ticket_id, ts, got}
_STATS: Dict[str, int] = {
    "observed": 0, "bug_new": 0, "bug_dup": 0, "usage": 0, "other": 0,
    "crisis_hold": 0, "rate_capped": 0, "engaged": 0, "alerts": 0,
    "voice_suppressed": 0, "screenshots": 0, "verify_yes": 0, "verify_no": 0,
    "smalltalk_suppressed": 0, "photo_suppressed": 0, "feedback": 0,
    # B33（实施49 2026-08-21）：被限频拦下的真反馈登记数 / 被压制截图归档数
    "rate_capped_report": 0, "capped_photo_archived": 0,
}
_DB_CONN: Optional[sqlite3.Connection] = None


def _bump(key: str, n: int = 1) -> None:
    with _LOCK:
        _STATS[key] = int(_STATS.get(key, 0)) + n


# ── 配置 ───────────────────────────────────────────────────────────────────────
def parse_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``bug_intake`` 配置段（缺省全关；异常按关，绝不抛）。"""
    out = {
        "enabled": False,
        "groups": set(),
        "support_accounts": set(),
        "max_replies_per_user_hour": 4,
        "max_replies_per_group_hour": 20,
        "collect_window_min": _COLLECT_TTL_DEFAULT_MIN,
        "ai_silent": True,
    }
    try:
        raw = (config or {}).get("bug_intake") or {}
        if not isinstance(raw, dict):
            return out
        out["enabled"] = bool(raw.get("enabled", False))
        out["ai_silent"] = bool(raw.get("ai_silent", True))
        out["groups"] = {str(g).strip() for g in (raw.get("groups") or [])
                         if str(g).strip()}
        out["support_accounts"] = {
            str(a).strip() for a in (raw.get("support_accounts") or [])
            if str(a).strip()}
        for k in ("max_replies_per_user_hour", "max_replies_per_group_hour",
                  "collect_window_min"):
            try:
                v = int(raw.get(k, out[k]))
                if v > 0:
                    out[k] = v
            except (TypeError, ValueError):
                pass
    except Exception:
        logger.debug("[bug_intake] 配置解析失败，按关闭处理", exc_info=True)
    return out


def is_bug_group(config: Optional[Dict[str, Any]], chat_id: Any) -> bool:
    cfg = parse_cfg(config)
    return bool(cfg["enabled"] and str(chat_id).strip() in cfg["groups"])


def voice_suppressed(config: Optional[Dict[str, Any]], chat_id: Any,
                     account_id: Any = "") -> bool:
    """报障群 / 支持账号一律不发语音（错声穿帮防线，见模块 docstring）。"""
    cfg = parse_cfg(config)
    if not cfg["enabled"]:
        return False
    hit = (str(chat_id).strip() in cfg["groups"]
           or (str(account_id).strip()
               and str(account_id).strip() in cfg["support_accounts"]))
    if hit:
        _bump("voice_suppressed")
    return hit


# ── 分类 ───────────────────────────────────────────────────────────────────────
def classify_message(text: str) -> str:
    """bug | feedback | usage | other。bug 词优先（误判代价不对称，见 docstring）；
    feedback 先于 usage（「建议…」的诉求是被登记而非被教学）。"""
    t = str(text or "").strip().lower()
    if not t:
        return "other"
    if any(w in t for w in BUG_WORDS):
        return "bug"
    if any(w in t for w in FEEDBACK_WORDS):
        return "feedback"
    if any(w in t for w in USAGE_WORDS):
        return "usage"
    if ("?" in t or "？" in t) and len(t) >= 6:
        return "usage"
    return "other"


def classify_severity(text: str) -> str:
    t = str(text or "").lower()
    if any(w in t for w in P0_WORDS):
        return "P0"
    if any(w in t for w in P1_WORDS):
        return "P1"
    return "P2"


def is_crisis(text: str) -> bool:
    t = str(text or "").lower()
    return any(w in t for w in CRISIS_WORDS)


# 修复确认/否认词表（verified 自动闭环）。保守：只在「该报障人有已回访的
# fixed 单」时才参与判定；疑问句一律弃权（「修复了吗」是追问不是确认）。
_VERIFY_NO_WORDS = (
    "还是不行", "还是不能", "还是失败", "还是一样", "还是坏", "还没好",
    "没修好", "没好", "依然不行", "仍然不行", "还是报错", "还是打不开",
    "still broken", "not fixed", "still not",
)
_VERIFY_YES_WORDS = (
    "好了", "可以了", "没问题了", "正常了", "修复了", "能用了", "修好了",
    "解决了", "可以用了", "没事了", "验证通过", "works now", "fixed now",
    "all good", "it works",
)


def detect_verify_intent(text: str) -> str:
    """'yes' | 'no' | ''。先判否认（「还是不行」含糊命中确认词的风险高于反向）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 80:
        return ""
    if any(w in t for w in _VERIFY_NO_WORDS):
        return "no"
    if "?" in t or "？" in t or "吗" in t:
        return ""
    if any(w in t for w in _VERIFY_YES_WORDS):
        return "yes"
    return ""


_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_title(text: str) -> str:
    return _NORM_RE.sub("", str(text or "").lower())[:120]


def titles_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


# ── 限频（进程内滑动窗；重启清零=宽松方向，安全）──────────────────────────────
def _rate_ok(key: str, limit: int, now: float, window_sec: float = 3600.0) -> bool:
    with _LOCK:
        lst = [t for t in _RATE.get(key, []) if now - t < window_sec]
        _RATE[key] = lst
        return len(lst) < limit


def _rate_mark(key: str, now: float) -> None:
    with _LOCK:
        _RATE.setdefault(key, []).append(now)


# ── 台账 ───────────────────────────────────────────────────────────────────────
def _db_path() -> Path:
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / "bug_intake.db"
    except Exception:
        return Path("config") / "bug_intake.db"


def _db() -> sqlite3.Connection:
    global _DB_CONN
    with _LOCK:
        if _DB_CONN is not None:
            return _DB_CONN
        p = _db_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        con = sqlite3.connect(str(p), check_same_thread=False, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            """CREATE TABLE IF NOT EXISTS bug_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
                chat_id TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT 'telegram',
                account_id TEXT NOT NULL DEFAULT '',
                reporter_id TEXT NOT NULL DEFAULT '',
                reporter_name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'bug',
                severity TEXT NOT NULL DEFAULT 'P2',
                status TEXT NOT NULL DEFAULT 'new',
                dup_of INTEGER NOT NULL DEFAULT 0,
                report_count INTEGER NOT NULL DEFAULT 1)""")
        con.execute(
            """CREATE TABLE IF NOT EXISTS bug_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, chat_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                reporter_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '')""")
        # 幂等迁移（house 约定：集中列表，逐条 try——已存在即跳过）
        for stmt in (
            "ALTER TABLE bug_tickets ADD COLUMN notify_ts REAL NOT NULL DEFAULT 0",
            "ALTER TABLE bug_tickets ADD COLUMN notify_note TEXT NOT NULL DEFAULT ''",
        ):
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass
        con.commit()
        _DB_CONN = con
        return con


def reset_state_for_tests() -> None:
    """测试隔离入口：清进程态 + 关 DB 连接（下次访问按当前 env 重建路径）。"""
    global _DB_CONN
    with _LOCK:
        _RATE.clear()
        _COLLECT.clear()
        for k in _STATS:
            _STATS[k] = 0
        if _DB_CONN is not None:
            try:
                _DB_CONN.close()
            except Exception:
                pass
            _DB_CONN = None


def _record_event(chat_id: Any, kind: str, reporter_id: Any = "",
                  detail: str = "") -> None:
    try:
        con = _db()
        with _LOCK:
            con.execute(
                "INSERT INTO bug_events (ts, chat_id, kind, reporter_id, detail)"
                " VALUES (?,?,?,?,?)",
                (time.time(), str(chat_id), kind, str(reporter_id),
                 str(detail or "")[:500]))
            con.commit()
    except Exception:
        logger.debug("[bug_intake] 事件落库失败（忽略）", exc_info=True)


def _find_open_dup(chat_id: Any, title: str, now: float) -> Optional[sqlite3.Row]:
    """同群 7 天内非关闭工单里找相似标题（返回主单，dup 链已归并到主单）。"""
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM bug_tickets WHERE chat_id=? AND dup_of=0"
            " AND status NOT IN ('closed','verified') AND created_ts>=?"
            " ORDER BY id DESC LIMIT 50",
            (str(chat_id), now - 7 * 86400)).fetchall()
        for r in rows:
            if titles_similar(title, r["title"]):
                return r
    except Exception:
        logger.debug("[bug_intake] 查重失败（按新单处理）", exc_info=True)
    return None


def _telemetry_snapshot(now: Optional[float] = None) -> str:
    """内部遥测摘要（新工单附带的第一现场线索）。全程 best-effort，失败返空。

    口径刻意窄：前端哑按钮错误 Top3（进程累计）+ 近 2h ops 事件按 kind 计数。
    只取聚合数字不取原文——工单 body 是给工程师的线索，不是日志转储。
    """
    ts = float(now if now is not None else time.time())
    parts: List[str] = []
    try:
        from src.web.frontend_error_stats import get_frontend_error_stats
        d = get_frontend_error_stats().dump()
        total = int(d.get("total") or 0)
        if total > 0:
            by_fn = d.get("by_fn") or {}
            top = sorted(by_fn.items(), key=lambda kv: -int(kv[1] or 0))[:3]
            top_txt = " ".join(f"{k}×{v}" for k, v in top)
            parts.append(f"前端错误累计 {total}" + (f"（{top_txt}）" if top_txt else ""))
    except Exception:
        pass
    try:
        from src.ops.ops_events import get_ops_event_store
        rows = get_ops_event_store().recent(limit=40)
        kinds: Dict[str, int] = {}
        for r in rows:
            if float(r.get("ts") or 0) >= ts - 7200:
                k = str(r.get("kind") or "?")
                kinds[k] = kinds.get(k, 0) + 1
        if kinds:
            kt = " ".join(f"{k}×{v}" for k, v in sorted(
                kinds.items(), key=lambda kv: -kv[1])[:4])
            parts.append(f"近2h运维事件 {kt}")
    except Exception:
        pass
    return ("[遥测] " + "；".join(parts)) if parts else ""


def record_bug_ticket(
    *, chat_id: Any, account_id: Any, reporter_id: Any, reporter_name: str,
    text: str, now: Optional[float] = None,
) -> Dict[str, Any]:
    """登记/归并一条疑似 bug。返回 {ticket_id, is_new, severity, report_count}。"""
    ts = float(now if now is not None else time.time())
    title = str(text or "").strip()[:200]
    severity = classify_severity(text)
    dup = _find_open_dup(chat_id, title, ts)
    con = _db()
    try:
        if dup is not None:
            new_count = int(dup["report_count"] or 1) + 1
            # 多人报告抬升严重度（P2→P1 需 ≥3 人；绝不降级）
            sev_rank = {"P0": 0, "P1": 1, "P2": 2}
            eff = min(sev_rank.get(str(dup["severity"]), 2),
                      sev_rank.get(severity, 2))
            if new_count >= 3 and eff == 2:
                eff = 1
            eff_sev = {0: "P0", 1: "P1", 2: "P2"}[eff]
            with _LOCK:
                con.execute(
                    "UPDATE bug_tickets SET report_count=?, updated_ts=?,"
                    " severity=?, body=substr(body || char(10) || ?, 1, 4000)"
                    " WHERE id=?",
                    (new_count, ts, eff_sev,
                     f"[+1 {reporter_name or reporter_id}] {title}",
                     int(dup["id"])))
                con.commit()
            return {"ticket_id": int(dup["id"]), "is_new": False,
                    "severity": eff_sev, "report_count": new_count}
        body = str(text or "")[:2000]
        tele = _telemetry_snapshot(ts)
        if tele:
            body = (body + "\n" + tele)[:4000]
        with _LOCK:
            cur = con.execute(
                "INSERT INTO bug_tickets (created_ts, updated_ts, chat_id,"
                " account_id, reporter_id, reporter_name, title, body,"
                " category, severity) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, ts, str(chat_id), str(account_id), str(reporter_id),
                 str(reporter_name or "")[:80], title, body, "bug", severity))
            con.commit()
            tid = int(cur.lastrowid)
        return {"ticket_id": tid, "is_new": True, "severity": severity,
                "report_count": 1}
    except Exception:
        logger.debug("[bug_intake] 工单落库失败", exc_info=True)
        return {"ticket_id": 0, "is_new": False, "severity": severity,
                "report_count": 0}


def append_ticket_note(ticket_id: int, note: str) -> None:
    if not ticket_id or not str(note or "").strip():
        return
    try:
        con = _db()
        with _LOCK:
            con.execute(
                "UPDATE bug_tickets SET updated_ts=?,"
                " body=substr(body || char(10) || ?, 1, 4000) WHERE id=?",
                (time.time(), str(note).strip()[:500], int(ticket_id)))
            con.commit()
    except Exception:
        logger.debug("[bug_intake] 工单补录失败（忽略）", exc_info=True)


VALID_STATUSES = ("new", "confirmed", "in_progress", "fixed", "verified",
                  "closed")


def set_ticket_status(ticket_id: int, status: str) -> bool:
    if status not in VALID_STATUSES:
        return False
    try:
        con = _db()
        with _LOCK:
            cur = con.execute(
                "UPDATE bug_tickets SET status=?, updated_ts=? WHERE id=?",
                (status, time.time(), int(ticket_id)))
            con.commit()
        return cur.rowcount > 0
    except Exception:
        logger.debug("[bug_intake] 状态更新失败", exc_info=True)
        return False


def get_ticket(ticket_id: int) -> Optional[Dict[str, Any]]:
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM bug_tickets WHERE id=?",
                          (int(ticket_id),)).fetchone()
        return dict(row) if row else None
    except Exception:
        logger.debug("[bug_intake] 取工单失败", exc_info=True)
        return None


def _html_esc(s: Any) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def build_fix_notify_text(row: Dict[str, Any]) -> str:
    """修复回访文案（HTML parse mode；@报障人用 tg://user 深链 mention）。

    刻意由代码拼（与回执 footer 同理由：编号/@目标必须确定性正确）；
    reporter_id 非数字（异常数据）时退化为纯文本称呼不带链接。
    """
    tid = int(row.get("id") or 0)
    name = _html_esc(str(row.get("reporter_name") or "").strip() or "朋友")
    rid = str(row.get("reporter_id") or "").strip()
    mention = (f'<a href="tg://user?id={rid}">{name}</a>'
               if rid.isdigit() else name)
    title = _html_esc(str(row.get("title") or "").strip()[:60])
    return (f"🔧 {mention} 你反馈的「{title}」（#{tid}）已修复上线，"
            "方便的话帮忙验证一下；确认没问题我们就关单，谢谢反馈！")


def mark_notified(ticket_id: int, ok: bool, note: str = "") -> None:
    """记录回访通知结果（成功写 notify_ts；失败只记 note 供 /notify 重试）。"""
    try:
        con = _db()
        with _LOCK:
            con.execute(
                "UPDATE bug_tickets SET notify_ts=?, notify_note=?,"
                " updated_ts=? WHERE id=?",
                (time.time() if ok else 0, str(note or "")[:200],
                 time.time(), int(ticket_id)))
            con.commit()
    except Exception:
        logger.debug("[bug_intake] 通知记账失败（忽略）", exc_info=True)


def pending_verify_ticket(chat_id: Any, reporter_id: Any,
                          now: Optional[float] = None
                          ) -> Optional[Dict[str, Any]]:
    """该报障人在此群「已回访待验证」的最新 fixed 单（14 天窗）。"""
    ts = float(now if now is not None else time.time())
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM bug_tickets WHERE chat_id=? AND reporter_id=?"
            " AND status='fixed' AND notify_ts>0 AND updated_ts>=?"
            " ORDER BY id DESC LIMIT 1",
            (str(chat_id), str(reporter_id), ts - 14 * 86400)).fetchone()
        return dict(row) if row else None
    except Exception:
        logger.debug("[bug_intake] 待验证单查询失败", exc_info=True)
        return None


def list_pending_notify(limit: int = 20) -> List[Dict[str, Any]]:
    """fixed 但回访未送达（notify_ts=0）的工单——worker 离线期积压的冲刷对象。

    刻意排除 assistant 悬浮球来的 web 工单（chat_id=webuser:*，2026-08-19）：
    它们没有 TG 会话可发（int() 必失败进 bad_chat_id），回访路径 =
    /api/assistant/tickets 面板拉取时盖 notify_ts；混进本队列只会永久滞留
    冲刷名单、污染 pending_notify KPI。
    """
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM bug_tickets WHERE status='fixed' AND notify_ts=0"
            " AND chat_id NOT LIKE 'webuser:%'"
            " ORDER BY id ASC LIMIT ?", (max(1, min(int(limit), 100)),)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("[bug_intake] 回访积压查询失败", exc_info=True)
        return []


def note_screenshot(config: Optional[Dict[str, Any]], chat_id: Any,
                    sender_id: Any, now: Optional[float] = None) -> bool:
    """收集窗内的图片消息记进工单（trigger 层调用，纯图不引燃回复）。"""
    try:
        cfg = parse_cfg(config)
        cid = str(chat_id).strip()
        if not (cfg["enabled"] and cid in cfg["groups"]):
            return False
        ts = float(now if now is not None else time.time())
        st = _in_collect_window(cid, sender_id, cfg["collect_window_min"], ts)
        if not st:
            return False
        tid = int(st.get("ticket_id") or 0)
        if not tid:
            return False
        with _LOCK:
            st.setdefault("got", set()).add("screenshot")
            st["ts"] = ts
            _COLLECT[_collect_key(cid, sender_id)] = st
        append_ticket_note(tid, "[截图已收到]")
        _bump("screenshots")
        _record_event(cid, "screenshot", sender_id, f"#{tid}")
        return True
    except Exception:
        logger.debug("[bug_intake] 截图记录失败（忽略）", exc_info=True)
        return False


def record_capped_photo(chat_id: Any, sender_id: Any, media_url: str) -> None:
    """B33（实施49 2026-08-21）：被压制消息的截图已归档——落台账事件。

    文字可 sync 还原、媒体不可（06:33-06:38 实录 6 条真反馈连图被拦即永久丢失）。
    trigger 层压制带图消息时先把图下载进 protocol_media，再经此登记 URL 进
    bug_events——值守翻台账即可找回证据，不必再请用户重发。best-effort 绝不抛。
    """
    try:
        _bump("capped_photo_archived")
        _record_event(chat_id, "capped_photo_archived", sender_id,
                      str(media_url or "")[:400])
    except Exception:
        logger.debug("[bug_intake] 压制截图登记失败（忽略）", exc_info=True)


def list_tickets(status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        q = "SELECT * FROM bug_tickets"
        args: List[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [dict(r) for r in con.execute(q, args).fetchall()]
    except Exception:
        logger.debug("[bug_intake] 工单列表失败", exc_info=True)
        return []


# ── 告警（EventBus 专属事件 + 日志兜底；best-effort 绝不阻塞主链）─────────────
def _publish_alert(kind: str, payload: Dict[str, Any]) -> None:
    body = dict(payload)
    body["kind"] = kind
    body.setdefault("rate_key",
                    f"bug_intake:{kind}:{payload.get('chat_id', '')}")
    try:
        logger.warning(
            "[bug_intake] 告警 kind=%s ticket=%s sev=%s reporter=%s",
            kind, payload.get("ticket_id", "-"), payload.get("severity", "-"),
            payload.get("reporter", "-"))
    except Exception:
        pass
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("bug_intake_alert", body)
        _bump("alerts")
    except Exception:
        logger.debug("[bug_intake] 告警发布失败（忽略）", exc_info=True)


# ── 收集窗 ────────────────────────────────────────────────────────────────────
def _collect_key(chat_id: Any, user_id: Any) -> str:
    return f"{chat_id}:{user_id}"


def _in_collect_window(chat_id: Any, user_id: Any, window_min: int,
                       now: float) -> Optional[Dict[str, Any]]:
    with _LOCK:
        st = _COLLECT.get(_collect_key(chat_id, user_id))
    if not st:
        return None
    if now - float(st.get("ts") or 0) > window_min * 60:
        with _LOCK:
            _COLLECT.pop(_collect_key(chat_id, user_id), None)
        return None
    return st


_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")


def _update_collected(st: Dict[str, Any], text: str) -> None:
    got = st.setdefault("got", set())
    t = str(text or "")
    if _VERSION_RE.search(t):
        got.add("version")
    if any(w in t for w in ("先", "然后", "再点", "步骤", "第一步", "之后",
                            "点了", "打开", "点击")):
        got.add("steps")


# ── 主入口 ────────────────────────────────────────────────────────────────────
def trigger_verdict(config: Optional[Dict[str, Any]], chat_id: Any,
                    sender_id: Any, text: str,
                    now: Optional[float] = None,
                    has_photo: bool = False,
                    is_direct: bool = False) -> Optional[bool]:
    """群触发三态（接线在 trigger._should_reply_to_group_message 顶部）。

    None=非报障群（走原有触发链一字不变）；True=引燃；False=硬压制。
    **报障群内不再返回 None**（2026-08-20 串戏事故收口）：闲聊/纯图落回原生
    follow_window 链会让支持号用陪伴人设接话（实录：官方支持号在报障群聊
    「Burrata 意面」+ 对用户截图当闲图点评）——报障群里 bug_intake 是唯一
    触发裁决者：非报障、非点名（``is_direct``＝@提及/回复本账号）一律静默。
    ``has_photo``：图片在收集窗内记进工单（P3）；纯图无文字不引燃也不放行
    （空文本走 A 线是未验证路径，记录 > 接话）。
    """
    try:
        cfg = parse_cfg(config)
        cid = str(chat_id).strip()
        if not (cfg["enabled"] and cid in cfg["groups"]):
            return None
        ts = float(now if now is not None else time.time())
        t = str(text or "").strip()
        # 危机词：压制 AI（人来处理）+ 即时告警（每用户 30min 去抖）
        if t and is_crisis(t):
            _bump("crisis_hold")
            if _rate_ok(f"c:{cid}:{sender_id}", 1, ts, window_sec=1800.0):
                _rate_mark(f"c:{cid}:{sender_id}", ts)
                _record_event(cid, "crisis_hold", sender_id, t[:200])
                _publish_alert("crisis", {
                    "chat_id": cid, "reporter": str(sender_id),
                    "text": t[:200], "severity": "P0"})
            return False
        # 限频：超额硬压制（含 @提及路径——刷屏比漏回伤害大；防刷屏契约有门禁钉住）。
        # B33（实施49 2026-08-21）：真反馈被限频拦下时**先登记再压制**——回复可以省
        # （预算语义不变），但报障内容/截图证据不能丢（06:33-06:38 实录 6 条真反馈
        # 被拦：文字靠人工 sync 才还原、随图媒体直接永久丢失）。登记面＝bug_events
        # 台账收全文 + 收集窗截图照记 + trigger 层把被压制的图归档进 protocol_media。
        # 判据保守：分类命中 bug/usage/feedback / 带字截图 / 收集窗内（正在补材料）。
        _report_like = bool(
            (t and classify_message(t) in ("bug", "usage", "feedback"))
            or (has_photo and t)
            or _in_collect_window(cid, sender_id, cfg["collect_window_min"], ts))

        def _note_capped_report() -> None:
            if not _report_like:
                return
            _bump("rate_capped_report")
            _record_event(cid, "rate_capped_report", sender_id, t[:300])
            if has_photo:
                note_screenshot(config, cid, sender_id, now=ts)

        if not _rate_ok(f"u:{cid}:{sender_id}",
                        cfg["max_replies_per_user_hour"], ts):
            _note_capped_report()
            _bump("rate_capped")
            return False
        if not _rate_ok(f"g:{cid}", cfg["max_replies_per_group_hour"], ts):
            _note_capped_report()
            _bump("rate_capped")
            return False
        # 图片消息：收集窗内记进工单；纯图（无文字）记录后**静默**——放回
        # 原生链会被 follow_window 接走当闲图点评（2026-08-20 实录）
        if has_photo:
            note_screenshot(config, cid, sender_id, now=ts)
            if not t:
                _bump("photo_suppressed")
                return False
        # 收集窗内的后续消息：持续接话（用户在补工单信息，别装死）
        if _in_collect_window(cid, sender_id, cfg["collect_window_min"], ts):
            _bump("engaged")
            return True
        if not t:
            return False
        # 修复确认/否认（verified 自动闭环）：无报障关键词也要接话
        if detect_verify_intent(t) and pending_verify_ticket(
                cid, sender_id, now=ts):
            _bump("engaged")
            return True
        cat = classify_message(t)
        if cat in ("bug", "usage", "feedback") or any(w in t for w in ENGAGE_EXTRA):
            _bump("engaged")
            return True
        # 点名支持号（@提及/回复本账号）：接话，但语气由 observe 的
        # 值守语境块钉死（简短带回产品话题，不陪聊）
        if is_direct:
            _bump("engaged")
            return True
        # 报障群不陪聊：非报障、非点名一律静默
        _bump("smalltalk_suppressed")
        return False
    except Exception:
        logger.debug("[bug_intake] trigger_verdict 异常（放行原链）", exc_info=True)
        return None


#: 值守语言锚（2026-08-20 实录：用户发英文聊天截图 → 视觉描述携英文进上下文 →
#: 支持号跟着说英文 + 会话语言字段被污染成 en，用户被迫用英文喊
#: 「pls use chinese」。所有值守提示块统一追加，不随截图/引用内容的语言漂移。）
LANG_ANCHOR = (
    "\n【语言】本群工作语言为中文：一律用中文回复（包括回应图片/截图内容时"
    "——不要跟随截图里的语言），除非对方明确要求使用其他语言。"
    "\n【截图纪律】用户发的聊天记录/界面截图是**报障证据**：只提取与产品问题"
    "相关的信息（报错文字/界面状态/操作路径），绝不评论截图里的对话内容本身"
    "（不调侃、不接话茬、不点评人物）——那是用户与其客户的私人对话。")


def _with_lang_anchor(out: Dict[str, Any]) -> Dict[str, Any]:
    if out.get("prompt_block"):
        out["prompt_block"] = str(out["prompt_block"]) + LANG_ANCHOR
    return out


def observe_group_message(
    config: Optional[Dict[str, Any]], *, chat_id: Any, account_id: Any,
    reporter_id: Any, reporter_name: str, text: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """报障群消息观察（接线在 skill_manager.process_message 群 hint 之后）。

    返回 {active, category, ticket_id, is_new, severity, report_count,
    prompt_block, footer}；active=False 时其余键为空——调用方零分支透传。
    """
    out = {"active": False, "category": "", "ticket_id": 0, "is_new": False,
           "severity": "", "report_count": 0, "prompt_block": "", "footer": "",
           "ai_silent": False}
    try:
        cfg = parse_cfg(config)
        cid = str(chat_id).strip()
        if not (cfg["enabled"] and cid in cfg["groups"]):
            return out
        ts = float(now if now is not None else time.time())
        t = str(text or "").strip()
        out["active"] = True
        # 2026-08-21 11:05 老板纪律（B36 收紧版）：报障群是「真正解决问题的群」，
        # 群内登记/回复/整理全部由值守人工来——AI（本地模型/云端）一条不发。
        # observe 的工单登记/告警/收集窗照常运转（值守的内部台账工具），
        # skill_manager 消费本标记在生成前短路（模型不跑，比丢弃草稿省 16-45s）。
        out["ai_silent"] = bool(cfg.get("ai_silent"))
        _bump("observed")
        # 本轮要回复 → 记限频（mention/回复链路径也从这里计数）
        _rate_mark(f"u:{cid}:{reporter_id}", ts)
        _rate_mark(f"g:{cid}", ts)

        # verified 自动闭环（P3）：已回访的 fixed 单 + 确认/否认口径 → 直接流转
        _vi = detect_verify_intent(t)
        if _vi:
            vt = pending_verify_ticket(cid, reporter_id, now=ts)
            if vt is not None:
                vtid = int(vt.get("id") or 0)
                if _vi == "yes":
                    set_ticket_status(vtid, "verified")
                    _bump("verify_yes")
                    _record_event(cid, "verify_yes", reporter_id, f"#{vtid}")
                    with _LOCK:
                        _COLLECT.pop(_collect_key(cid, reporter_id), None)
                    out.update({
                        "category": "verify", "ticket_id": vtid,
                        "footer": f"✅ 工单 #{vtid} 已确认修复，感谢反馈！",
                        "prompt_block": (
                            "【回访确认】用户确认之前报的问题已修复（工单 #"
                            f"{vtid} 已关单）。简短道谢即可，欢迎随时再反馈；"
                            "不要展开闲聊。"),
                    })
                else:
                    set_ticket_status(vtid, "confirmed")
                    append_ticket_note(
                        vtid, f"[验证未过 {reporter_name or reporter_id}] {t}")
                    _bump("verify_no")
                    _record_event(cid, "verify_no", reporter_id, f"#{vtid}")
                    with _LOCK:
                        _COLLECT[_collect_key(cid, reporter_id)] = {
                            "ticket_id": vtid, "ts": ts, "got": set()}
                    out.update({
                        "category": "verify", "ticket_id": vtid,
                        "footer": f"🔁 工单 #{vtid} 已转回工程师复查",
                        "prompt_block": (
                            "【回访未通过】用户反馈修复后问题仍存在（工单 #"
                            f"{vtid} 已转回复查）。诚恳致歉并确认收到；"
                            "请对方描述现在的具体表现（和之前一样还是变了），"
                            "一次只问一个问题；绝不承诺具体修复时间。"),
                    })
                return _with_lang_anchor(out)

        st = _in_collect_window(cid, reporter_id, cfg["collect_window_min"], ts)
        if st is not None:
            # 收集窗内：补录进工单，不新开单
            _update_collected(st, t)
            tid = int(st.get("ticket_id") or 0)
            append_ticket_note(tid, f"[{reporter_name or reporter_id}] {t}")
            with _LOCK:
                st["ts"] = ts
                _COLLECT[_collect_key(cid, reporter_id)] = st
            got = st.get("got") or set()
            missing = [x for x in ("version", "steps") if x not in got]
            miss_txt = ("；还缺：" + "、".join(
                {"version": "版本号", "steps": "复现步骤"}[m] for m in missing)
                if missing else "；关键信息已齐，告知用户已完整登记")
            out.update({
                "category": "collect", "ticket_id": tid,
                "prompt_block": (
                    "【报障值守·信息收集中】用户正在补充工单 #"
                    f"{tid} 的信息{miss_txt}。确认收到并简短复述要点；"
                    "还缺信息就只追问一项，不要一次问一串；"
                    "绝不承诺具体修复时间。"),
            })
            return _with_lang_anchor(out)

        cat = classify_message(t)
        out["category"] = cat
        if cat == "bug":
            rec = record_bug_ticket(
                chat_id=cid, account_id=account_id, reporter_id=reporter_id,
                reporter_name=reporter_name, text=t, now=ts)
            out.update(rec)
            tid = int(rec.get("ticket_id") or 0)
            if tid:
                with _LOCK:
                    _COLLECT[_collect_key(cid, reporter_id)] = {
                        "ticket_id": tid, "ts": ts, "got": set()}
                st2 = {"got": set()}
                _update_collected(st2, t)
                with _LOCK:
                    _COLLECT[_collect_key(cid, reporter_id)]["got"] = st2["got"]
            if rec.get("is_new"):
                _bump("bug_new")
                _record_event(cid, "bug_new", reporter_id, t[:200])
                if rec.get("severity") in ("P0", "P1"):
                    _publish_alert("ticket", {
                        "chat_id": cid, "ticket_id": tid,
                        "severity": rec.get("severity"),
                        "title": t[:120],
                        "reporter": str(reporter_name or reporter_id),
                        "report_count": rec.get("report_count", 1)})
            else:
                _bump("bug_dup")
                _record_event(cid, "bug_dup", reporter_id, f"#{tid}")
            sev = str(rec.get("severity") or "P2")
            if rec.get("is_new"):
                out["footer"] = f"🎫 已登记工单 #{tid}（{sev}）"
                out["prompt_block"] = (
                    "【报障值守】本条被判定为疑似 bug（已登记工单 #"
                    f"{tid}，严重度 {sev}）。你的回复：1) 确认收到并简短共情；"
                    "2) 只追问一项最关键的缺失信息（版本号 > 复现步骤 > 截图；"
                    "消息里已有的不要重复问）；3) 绝不承诺具体修复时间，"
                    "不编造原因。若其实是使用方法问题，直接给操作步骤并说明"
                    "这不是故障。")
            elif tid:
                out["footer"] = (
                    f"🎫 已并入工单 #{tid}（第 {rec.get('report_count')} 人反馈）")
                out["prompt_block"] = (
                    f"【报障值守】已有其他用户报告过同类问题（工单 #{tid}，"
                    f"累计 {rec.get('report_count')} 人）。告知用户该问题已在"
                    "跟进中，如有新细节（版本/步骤）欢迎补充；"
                    "绝不承诺具体修复时间。")
        elif cat == "feedback":
            _bump("feedback")
            _record_event(cid, "feedback", reporter_id, t[:200])
            out["footer"] = "💡 建议已登记"
            out["prompt_block"] = (
                "【产品建议值守】本条是产品建议/体验反馈（不是故障也不是提问）。"
                "你的回复：1) 确认收到并用一句话复述你理解的建议要点（证明真看懂"
                "了）；2) 告知已登记、会转给产品团队评估；3) 真诚感谢。硬约束："
                "绝不承诺会采纳或给时间表，绝不争辩解释「为什么现在这样设计」，"
                "绝不展开闲聊。")
        elif cat == "usage":
            _bump("usage")
            _record_event(cid, "usage", reporter_id, t[:200])
            out["prompt_block"] = (
                "【答疑值守】本条是使用咨询。优先依据知识库参考给出明确、"
                "可操作的步骤（先结论后步骤）。硬约束：登录/安装/配置这类"
                "操作路径**只许**按知识库参考回答——知识库里没有的操作，"
                "绝不按通用软件的常识猜（本产品的正确做法可能与常识相反，"
                "如登录必须扫码防封而非手机号验证码），如实说"
                "「这个我记下来让工程师确认后答复您」。"
                "如果对方其实在描述故障，按报障处理：请对方提供版本号和"
                "操作步骤。")
        else:
            _bump("other")
            out["prompt_block"] = (
                "【报障群值守·硬约束】你是官方技术支持号，正在官方报障群值守。"
                "对方这条与产品无关。你的回复只许一句话：礼貌带过并把话题引回"
                "产品（例：「这里是官方支持群哈，产品上有任何问题随时发我～」）。"
                "绝对禁止：聊吃饭/生活/心情/天气等任何生活话题、描述你自己的"
                "生活状态或喜好、对截图内容做闲聊式点评、反问与产品无关的问题。")
        return _with_lang_anchor(out)
    except Exception:
        logger.debug("[bug_intake] observe 异常（no-op）", exc_info=True)
        return out


# ── 观测 ───────────────────────────────────────────────────────────────────────
def dump_stats() -> Dict[str, Any]:
    """进程计数 + 台账口径（今日/开放工单），供 /api/workspace/metrics 消费。"""
    with _LOCK:
        d: Dict[str, Any] = dict(_STATS)
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        day_start = time.time() - 86400
        row = con.execute(
            "SELECT COUNT(*) AS n FROM bug_tickets WHERE created_ts>=?",
            (day_start,)).fetchone()
        d["tickets_24h"] = int(row["n"] if row else 0)
        rows = con.execute(
            "SELECT severity, COUNT(*) AS n FROM bug_tickets"
            " WHERE status IN ('new','confirmed','in_progress') AND dup_of=0"
            " GROUP BY severity").fetchall()
        d["open_by_severity"] = {str(r["severity"]): int(r["n"]) for r in rows}
        d["open_total"] = sum(d["open_by_severity"].values())
        row2 = con.execute(
            "SELECT COUNT(*) AS n FROM bug_tickets"
            " WHERE status='fixed' AND notify_ts=0"
            " AND chat_id NOT LIKE 'webuser:%'").fetchone()
        d["pending_notify"] = int(row2["n"] if row2 else 0)
    except Exception:
        d["tickets_24h"] = 0
        d["open_by_severity"] = {}
        d["open_total"] = 0
        d["pending_notify"] = 0
    d["active"] = bool(
        d.get("observed") or d.get("tickets_24h") or d.get("open_total"))
    return d


__all__ = [
    "parse_cfg", "is_bug_group", "voice_suppressed", "classify_message",
    "classify_severity", "is_crisis", "normalize_title", "titles_similar",
    "record_bug_ticket", "append_ticket_note", "set_ticket_status",
    "list_tickets", "get_ticket", "build_fix_notify_text", "mark_notified",
    "detect_verify_intent", "pending_verify_ticket", "list_pending_notify",
    "note_screenshot", "trigger_verdict", "observe_group_message",
    "dump_stats", "reset_state_for_tests", "VALID_STATUSES",
]
