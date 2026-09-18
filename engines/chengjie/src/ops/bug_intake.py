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
- **序号连续性哨兵**（C1-②，#123 族，2026-09-02）：``note_group_msg_seq`` /
  ``due_seq_gaps`` / ``seq_gap_backfilled``——群消息 mid 跳号即候选漏收，
  宽限期后落 ``bug_events(seq_gap)`` + 告警 + 交 telegram_client 云端定点补拉
  重放主链（顶部比对的 gap-probe 探不出序号中间的洞，0902 21:1x 实锤）。

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
from typing import Any, Dict, List, Optional, Set, Tuple

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
    # J-5 D（2026-09-05，09-04 夜钧/skuio 五句漏网实录：「文字转语音克隆成功了，
    # 不能发送」「媒体这里发不了任何东西了」「WHATSAPP登陆了几分钟，登陆不上」
    # 「这个红色框框的要修复」——全判 other → smalltalk_suppressed 无痕）。
    # 「隐藏」刻意不进表：单字会把「建议隐藏或删除」这类 feedback 吃成 bug。
    "不能发送", "发不了", "登陆不上", "要修复", "不同步", "不消失", "点不进",
    "发送不了", "收不了", "打不了", "传不了", "传不上",
)
# J-5 D：正则形态的 bug 判据（词表管不住的量词/结构句）。
# ① 「显示 N 条」——「发了一条显示2条」重复渲染族；② 「<界面部件>(是)空的」
#    ——「图标空的」（「空的」单独不进词表：「有空的话」是闲聊高频）；
# ③ 否定式问句「为什么/为啥/怎么会 … <产品名词>」——问句语体的报障，此前只有
#    带问号且 ≥6 字才算 usage，无问号的直接 other。
_BUG_PRODUCT_NOUNS = (
    "消息", "媒体", "语音", "登录", "登陆", "角标", "图片", "视频", "文件", "头像",
    "人设", "翻译", "账号", "发送", "接收", "克隆", "红点", "未读", "通知", "界面",
    "按钮", "图标", "列表", "工单", "群", "贴图", "表情", "转写", "同步", "会话",
)
_BUG_PATTERNS = (
    re.compile(r"显示\s*\d+\s*条"),
    re.compile(r"(图标|界面|页面|列表|框|头像|内容|栏|区)\s*(是|都)?\s*空的"),
    re.compile(r"(为什么|为啥|怎么会|为何)[^。！？!?]{0,30}("
               + "|".join(_BUG_PRODUCT_NOUNS) + ")"),
    re.compile(r"(" + "|".join(_BUG_PRODUCT_NOUNS)
               + r")[^。！？!?]{0,12}(为什么|为啥|怎么会|为何)"),
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

# L-7 C（2026-09-06）：报障人**向值守要东西**的请求语——清单 / 进度 / 修复了什么 /
# 对账 / changelog / 什么时候修 / 修好了吗。0906 00:45 skuio「先列出41张单以及修复的
# 结果」判 other → smalltalk_suppressed（ev#753）无痕，他随后自己做了 44 项总表。
# 这类话 AI 不该编清单（数据在台账里，人来答），但也绝不能静默：记 request_for_info
# 事件 + 推值守告警。「列表」刻意不进表（「会话列表不刷新」是 bug 词先命中；
# 「这个列表好看」是闲聊）。
INFO_REQUEST_WORDS = (
    "列出", "清单", "进度", "修复了什么", "修了什么", "哪些修了", "哪些已修", "哪些修复",
    "修复结果", "修复情况", "修复进度", "修复状态", "对账", "总表", "汇总",
    "changelog", "release note", "更新日志", "发版说明", "什么时候修", "何时修",
    "什么时候发", "何时发版", "修好了吗", "修复了吗", "修了吗", "修好了没", "有没有修",
)


def is_info_request(text: str) -> bool:
    """报障群里「向值守要清单/进度/答复」的话：请求词命中，或 ≥6 字且以 吗/么/嘛
    收尾的问句（0906 04:07 钧「那个表情在客户手机上会动吗」无问号 → other → 静默 ev#810）。"""
    t = str(text or "").strip().lower()
    if not t:
        return False
    if any(w in t for w in INFO_REQUEST_WORDS):
        return True
    return len(t) >= 6 and t.rstrip("。！!～~ ").endswith(("吗", "么", "嘛"))

_COLLECT_TTL_DEFAULT_MIN = 30

# ── 进程态（限频 / 收集窗 / 计数器）────────────────────────────────────────────
_LOCK = threading.Lock()
_RATE: Dict[str, List[float]] = {}          # "u:<chat>:<user>" / "g:<chat>" → ts 列表
_COLLECT: Dict[str, Dict[str, Any]] = {}    # "<chat>:<user>" → {ticket_id, ts, got}
_STATS: Dict[str, int] = {
    "observed": 0, "bug_new": 0, "bug_dup": 0, "usage": 0, "other": 0,
    "crisis_hold": 0, "rate_capped": 0, "engaged": 0, "alerts": 0,
    "voice_suppressed": 0, "screenshots": 0, "verify_yes": 0, "verify_no": 0,
    # J-5 B（决策 D5）：确认/否认词命中、有待验单、但无 #N / reply_to 定不了
    # 归属 → 不翻单只记事件的次数
    "verify_ambiguous": 0,
    "smalltalk_suppressed": 0, "photo_suppressed": 0, "feedback": 0,
    # B33（实施49 2026-08-21）：被限频拦下的真反馈登记数 / 被压制截图归档数
    "rate_capped_report": 0, "capped_photo_archived": 0,
    # 实施82：官方 bot 消息被自咬环守卫压制的次数（bot 代发后应恒 >0）
    "official_bot_suppressed": 0,
    # 2026-09-02：本方账号（支持号/本 worker）消息被立单链自身守卫压制的次数
    "self_msg_suppressed": 0,
    # J-5 A（2026-09-05）：observe 入口按 mid / (reporter,文本,60s) 判为二次观察
    # 而短路的次数（实时链与 backfill 回放对同一条消息只许一次登记）
    "observe_dup": 0,
    # J-5 D（决策 D4 后半）：已知报障人「图+任意文字」判 other 被升格立单的次数
    "photo_report": 0,
    # J-5 E-2：报障人**回复支持号消息**（回访 / 带 #N 的播报）→ 不看 30 分钟收集窗
    # 直接续写该单的次数（0905 #165＝41 分钟后的追问被立成新单）
    "reply_continue": 0,
    # C1-②（#123 族，2026-09-02）：群消息序号哨兵——跳号告警次数 / 补拉回来的
    # 真消息数 / 核实为空（已删/服务消息）的缺号数
    "seq_gap": 0, "seq_gap_filled": 0, "seq_gap_empty": 0,
    # L-7 C（2026-09-06）：序号哨兵收割时核实为**本账号 API 出站**的号（镜像在
    # inbox messages direction='out'，TelegramClient 收不到自己的 update）→ 不告警不补拉
    "seq_gap_own": 0,
    # L-7 C：报障人向值守要清单/进度/修复结果（0906 00:45「先列出41张单以及修复的结果」
    # 被判闲聊静默 ev#753）→ 记事件 + 推值守告警，不再无痕
    "request_for_info": 0,
}
_DB_CONN: Optional[sqlite3.Connection] = None
# J-5 E-2：本方账号群发消息 → 其文中 #N 工单号（"<chat>:<mid>" → [tid,...]）。
# 进程内热路；重启后由 bug_events 里 self_msg_suppressed 事件的 ``mid=`` 前缀
# 反查兜底（见 ticket_for_reply）。
_OWN_MSG_REFS: Dict[str, List[int]] = {}
_OWN_MSG_REFS_MAX = 2000


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
        # J-5 C（决策 D4）：限频只管「要不要回复」；True 才回到旧语义「超额连登记
        # 一起丢」。默认 False——AI 静默档下限频唯一效果就是丢登记（0904 三条真报障）。
        "rate_limit_registration": False,
    }
    try:
        raw = (config or {}).get("bug_intake") or {}
        if not isinstance(raw, dict):
            return out
        out["enabled"] = bool(raw.get("enabled", False))
        out["ai_silent"] = bool(raw.get("ai_silent", True))
        out["rate_limit_registration"] = bool(
            raw.get("rate_limit_registration", False))
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


def _is_own_sender(cfg: Dict[str, Any], sender_id: Any,
                   account_id: Any = "") -> bool:
    """发送者是否本方账号（support_accounts / 本 worker / 官方 bot）。

    镜像层的「跳过: 自身发送的消息」只认得本 worker 自己（from_user.id ==
    user_info.id）：值守换了个支持号在群里发回访播报时，别的 worker 的观察链
    会把它当客户消息立单（2026-09-02 实锤：#123/#125/#135/#136 全是
    8506426282(BOUNDLESS) 的回访播报被 6834964252 的观察链登记成新工单——
    播报文案满是 bug 词与工单号，正是分类器的靶子）。这里按发送者 id 收口，
    立单链（observe/trigger）对本方消息一律 no-op。
    """
    sid = str(sender_id or "").strip()
    if not sid:
        return False
    if sid in cfg["support_accounts"]:
        return True
    aid = str(account_id or "").strip()
    if aid and sid == aid:
        return True
    try:
        from src.ops.bug_bot import is_official_bot
        return is_official_bot(sid)
    except Exception:
        return False


# ── 分类 ───────────────────────────────────────────────────────────────────────
def classify_message(text: str) -> str:
    """bug | feedback | usage | other。bug 词优先（误判代价不对称，见 docstring）；
    feedback 先于 usage（「建议…」的诉求是被登记而非被教学）。"""
    t = str(text or "").strip().lower()
    if not t:
        return "other"
    if any(w in t for w in BUG_WORDS):
        return "bug"
    if any(p.search(t) for p in _BUG_PATTERNS):
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
            # 修复说明（实施81 P1-4）：标 fixed 时一句话写清改了什么，回访文案引用
            "ALTER TABLE bug_tickets ADD COLUMN fix_note TEXT NOT NULL DEFAULT ''",
            # 回访出站消息 id（实施82 P0）：bot 发送成功落此列——后续 reaction
            # 验证轮询/编辑消息都锚它；0=未知（pyro 回落链不取 id，如实记 0）
            "ALTER TABLE bug_tickets ADD COLUMN notify_msg_id INTEGER NOT NULL DEFAULT 0",
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
        _SEQ.clear()
        _OBSERVED_MID.clear()
        _OBSERVED_TXT.clear()
        _OWN_MSG_REFS.clear()
        for k in _STATS:
            _STATS[k] = 0
        if _DB_CONN is not None:
            try:
                _DB_CONN.close()
            except Exception:
                pass
            _DB_CONN = None
    try:
        from src.ops.bug_intake_backfill import reset_seen_for_tests
        reset_seen_for_tests()
    except Exception:
        pass


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


def set_ticket_status(ticket_id: int, status: str,
                      fix_note: Optional[str] = None) -> bool:
    """状态流转；``fix_note``（实施81 P1-4）＝「本次改了什么」一句话，标 fixed 时
    随手写上，回访文案引用——回访从「已修复」变成「你报的 X 已改成 Y」。
    传 None/空串不动既有值（重试回访等场景不许把已写的说明抹掉）。"""
    if status not in VALID_STATUSES:
        return False
    try:
        con = _db()
        with _LOCK:
            note = str(fix_note or "").strip()[:300]
            if note:
                cur = con.execute(
                    "UPDATE bug_tickets SET status=?, fix_note=?, updated_ts=?"
                    " WHERE id=?",
                    (status, note, time.time(), int(ticket_id)))
            else:
                cur = con.execute(
                    "UPDATE bug_tickets SET status=?, updated_ts=? WHERE id=?",
                    (status, time.time(), int(ticket_id)))
            con.commit()
        return cur.rowcount > 0
    except Exception:
        logger.debug("[bug_intake] 状态更新失败", exc_info=True)
        return False


def merge_ticket(child_id: int, parent_id: int, *, note: str = "",
                 by: str = "") -> Dict[str, Any]:
    """把子单并入主单：``dup_of=parent``、``status=closed``、写 ``fix_note``、落 ``merge`` 事件。

    P2-1（#334/#335 巡检单进修复队列）：值守核对题不应各开一条修复线。
    已并入同一父单 → 幂等 ``ok``。子/父不存在、自并、环 → ``ok=False``。
    """
    try:
        cid, pid = int(child_id), int(parent_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_id"}
    if cid <= 0 or pid <= 0 or cid == pid:
        return {"ok": False, "error": "bad_id"}
    child = get_ticket(cid)
    parent = get_ticket(pid)
    if not child:
        return {"ok": False, "error": "child_missing"}
    if not parent:
        return {"ok": False, "error": "parent_missing"}
    # 父单若已是别人的子单，跟到真正的主单
    root = pid
    seen = {cid}
    while True:
        prow = get_ticket(root)
        if not prow:
            return {"ok": False, "error": "parent_missing"}
        d = int(prow.get("dup_of") or 0)
        if d <= 0:
            break
        if d in seen:
            return {"ok": False, "error": "cycle"}
        seen.add(d)
        root = d
    if int(child.get("dup_of") or 0) == root and str(child.get("status") or "") == "closed":
        return {"ok": True, "ticket_id": cid, "parent_id": root, "already": True}
    why = str(note or "").strip()[:300] or f"并入 #{root}"
    ts = time.time()
    try:
        con = _db()
        with _LOCK:
            con.execute(
                "UPDATE bug_tickets SET dup_of=?, status='closed', fix_note=?,"
                " updated_ts=? WHERE id=?",
                (root, why, ts, cid))
            extra = f"\n[+merge #{cid} {by or ''}] {why}".strip()
            con.execute(
                "UPDATE bug_tickets SET body=substr(body || ?, 1, 4000), updated_ts=?"
                " WHERE id=?",
                (extra, ts, root))
            con.commit()
        _record_event(str(child.get("chat_id") or ""), "merge",
                      str(child.get("reporter_id") or ""),
                      f"#{cid}→#{root} {why}"[:500])
        return {"ok": True, "ticket_id": cid, "parent_id": root, "already": False}
    except Exception:
        logger.debug("[bug_intake] merge_ticket 失败", exc_info=True)
        return {"ok": False, "error": "db"}


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


def _mention_html(row: Dict[str, Any]) -> str:
    """@报障人（tg://user 深链；reporter_id 非数字退化为纯文本称呼）。"""
    name = _html_esc(str(row.get("reporter_name") or "").strip() or "朋友")
    rid = str(row.get("reporter_id") or "").strip()
    return (f'<a href="tg://user?id={rid}">{name}</a>'
            if rid.isdigit() else name)


def resolve_update_hint(config: Optional[Dict[str, Any]]) -> str:
    """「怎么拿到修复」提示（``bug_intake.update_hint``，实施81 P0-3）。

    运营按发版节奏改 overlay（如「已推送热补丁，重启智聊即生效」/「请更新到
    1.0.59」）；**缺省空串＝回访文案不带更新段**（服务端修复即时生效，硬塞
    「请更新」反而是误导），与旧行为完全一致。
    """
    try:
        raw = ((config or {}).get("bug_intake") or {}).get("update_hint")
        return str(raw or "").strip()[:200]
    except Exception:
        return ""


def build_fix_notify_text(row: Dict[str, Any], update_hint: str = "") -> str:
    """修复回访文案（HTML parse mode；@报障人用 tg://user 深链 mention）。

    刻意由代码拼（与回执 footer 同理由：编号/@目标必须确定性正确）。
    群里要能直接看到「问题核心 + 修复方法」：标题 → 「问题：」段，
    ``fix_note``（工单行内，P1-4）→ 「修复：」段；``update_hint``（P0-3，
    配置或调用方传入）→ 「获取方式」段——后两段可缺省。
    """
    tid = int(row.get("id") or 0)
    mention = _mention_html(row)
    title = _html_esc(str(row.get("title") or "").strip()[:80])
    lines = [f"🔧 {mention} #{tid} 已修复上线"]
    if title:
        lines.append(f"问题：{title}")
    fix_note = str(row.get("fix_note") or "").strip()
    if fix_note:
        lines.append(f"修复：{_html_esc(fix_note[:200])}")
    hint = str(update_hint or "").strip()
    if hint:
        lines.append(f"📦 获取方式：{_html_esc(hint[:200])}")
    lines.append("方便的话帮忙验证一下；确认没问题我们就关单，谢谢反馈！")
    return "\n".join(lines)


def build_reply_text(row: Dict[str, Any], text: str, mention: bool = True) -> str:
    """处置台「群内回复」文案（HTML parse mode，实施81 P0-2）。

    @报障人 + 工单号前缀由代码拼（与回执 footer 同一理由），正文原样转义——
    值守写什么发什么，不代改措辞。
    """
    tid = int(row.get("id") or 0)
    body = _html_esc(str(text or "").strip())
    head = (_mention_html(row) + " ") if mention else ""
    footer = f"\n🎫 工单 #{tid}" if tid else ""
    return f"{head}{body}{footer}"


def mark_notified(ticket_id: int, ok: bool, note: str = "",
                  msg_id: int = 0) -> None:
    """记录回访通知结果（成功写 notify_ts；失败只记 note 供 /notify 重试）。

    ``msg_id``（实施82）＝bot 出站消息 id，reaction 验证/编辑消息的锚；
    pyro 回落链不取 id 传 0——0 不覆盖既有非零值（重试失败别抹掉锚）。
    """
    try:
        con = _db()
        with _LOCK:
            if int(msg_id or 0) > 0:
                con.execute(
                    "UPDATE bug_tickets SET notify_ts=?, notify_note=?,"
                    " notify_msg_id=?, updated_ts=? WHERE id=?",
                    (time.time() if ok else 0, str(note or "")[:200],
                     int(msg_id), time.time(), int(ticket_id)))
            else:
                con.execute(
                    "UPDATE bug_tickets SET notify_ts=?, notify_note=?,"
                    " updated_ts=? WHERE id=?",
                    (time.time() if ok else 0, str(note or "")[:200],
                     time.time(), int(ticket_id)))
            con.commit()
    except Exception:
        logger.debug("[bug_intake] 通知记账失败（忽略）", exc_info=True)


def candidate_verify_tickets(chat_id: Any, reporter_id: Any,
                             now: Optional[float] = None,
                             limit: int = 50) -> List[Dict[str, Any]]:
    """该报障人在此群「已回访待验证」的全部 fixed 单（14 天窗，id 降序）。

    J-5 B（决策 D5）：候选是**列表**不是「最新一张」——同一报障人回访后几十张
    单同时待验时，一句「还是不行」以前打到 id 最大那张（I-6 结论 B / 0905
    #142→#140→#138 连翻）。归属由 ``resolve_verify_target`` 按 ``#N`` / reply_to
    决定，决定不了就不翻单；本列表同时供值守面板挑选。
    """
    ts = float(now if now is not None else time.time())
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM bug_tickets WHERE chat_id=? AND reporter_id=?"
            " AND status='fixed' AND notify_ts>0 AND updated_ts>=?"
            " ORDER BY id DESC LIMIT ?",
            (str(chat_id), str(reporter_id), ts - 14 * 86400,
             max(1, min(int(limit), 200)))).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("[bug_intake] 待验证单查询失败", exc_info=True)
        return []


def pending_verify_ticket(chat_id: Any, reporter_id: Any,
                          now: Optional[float] = None
                          ) -> Optional[Dict[str, Any]]:
    """兼容壳：``candidate_verify_tickets`` 的首张（最新）。

    ⚠ 只用于「有没有待验单」的存在性判断（trigger_verdict / 面板）；**归属**
    一律走 ``resolve_verify_target``——拿本函数结果直接翻状态就是 D5 要禁的
    「短句打到 id 最大那张」。
    """
    cands = candidate_verify_tickets(chat_id, reporter_id, now=now, limit=1)
    return cands[0] if cands else None


def is_known_reporter(chat_id: Any, reporter_id: Any) -> bool:
    """该 reporter 在此群曾立过任一工单（任何状态）。

    J-5 D（决策 D4 后半）：已知报障人在报障群发「图 + 任意文字」＝报障材料——
    「这个」「看图」配截图判 other 被静默（0904 skuio 三张带图报障落
    capped_photo_archived 无单）。只认**本群**历史，避免别群报障人在此群晒图
    被当报障。
    """
    rid = str(reporter_id or "").strip()
    if not rid:
        return False
    try:
        con = _db()
        row = con.execute(
            "SELECT 1 FROM bug_tickets WHERE chat_id=? AND reporter_id=? LIMIT 1",
            (str(chat_id), rid)).fetchone()
        return row is not None
    except Exception:
        logger.debug("[bug_intake] 已知报障人查询失败", exc_info=True)
        return False


def _remember_own_message(chat_id: Any, msg_id: int, text: str) -> List[int]:
    """本方账号群发（回访 / 播报）落 mid→#N 映射，供 reply 续写反查。"""
    refs = extract_ticket_refs(text)
    if not (msg_id > 0 and refs):
        return refs
    key = f"{chat_id}:{int(msg_id)}"
    with _LOCK:
        _OWN_MSG_REFS[key] = refs
        if len(_OWN_MSG_REFS) > _OWN_MSG_REFS_MAX:
            for k in list(_OWN_MSG_REFS)[:_OWN_MSG_REFS_MAX // 2]:
                _OWN_MSG_REFS.pop(k, None)
    return refs


def ticket_for_reply(chat_id: Any, reply_to_msg_id: Any,
                     reporter_id: Any = "") -> int:
    """报障人**回复的那条支持号消息**对应哪张单（J-5 E-2，0=定不了）。

    三级反查：① 某单 ``notify_msg_id``（bot 回访消息，最可靠锚点）；② 进程内
    ``_OWN_MSG_REFS``（本方播报文中 #N）；③ bug_events 里 ``self_msg_suppressed``
    事件 detail 的 ``mid=<id> refs=#a,#b`` 前缀（重启后兜底）。播报提到多张单时
    只认**该 reporter 自己的**那张，仍多于一张＝定不了（宁可走普通链，不乱续）。
    命中的单若已并入他单（``dup_of>0``）跟到主单。
    """
    try:
        rmid = int(reply_to_msg_id or 0)
    except (TypeError, ValueError):
        return 0
    if rmid <= 0:
        return 0
    cid = str(chat_id).strip()
    rid = str(reporter_id or "").strip()
    try:
        con = _db()
        row = con.execute(
            "SELECT id, dup_of FROM bug_tickets WHERE chat_id=? AND notify_msg_id=?"
            " ORDER BY id DESC LIMIT 1", (cid, rmid)).fetchone()
        if row:
            return int(row[1] or 0) or int(row[0])
        with _LOCK:
            refs = list(_OWN_MSG_REFS.get(f"{cid}:{rmid}") or [])
        if not refs:
            ev = con.execute(
                "SELECT detail FROM bug_events WHERE chat_id=? AND"
                " kind='self_msg_suppressed' AND detail LIKE ? ORDER BY id DESC"
                " LIMIT 1", (cid, f"mid={rmid} %")).fetchone()
            if ev:
                m = re.match(r"mid=\d+ refs=([#\d,]+)", str(ev[0]))
                refs = extract_ticket_refs(m.group(1)) if m else []
        if not refs:
            return 0
        if len(refs) > 1 and rid:
            q = ",".join("?" for _ in refs)
            mine = [int(r[0]) for r in con.execute(
                f"SELECT id FROM bug_tickets WHERE chat_id=? AND reporter_id=?"
                f" AND id IN ({q})", [cid, rid, *refs]).fetchall()]
            refs = [t for t in refs if t in mine]
        if len(refs) != 1:
            return 0
        row = con.execute("SELECT id, dup_of FROM bug_tickets WHERE id=?",
                          (refs[0],)).fetchone()
        if not row:
            return 0
        return int(row[1] or 0) or int(row[0])
    except Exception:
        logger.debug("[bug_intake] reply 归属反查失败", exc_info=True)
        return 0


_TICKET_REF_RE = re.compile(r"#\s*(\d{1,7})")


def extract_ticket_refs(text: str) -> List[int]:
    """文中 ``#N`` 工单号（去重保序）。「#142 好了」→ [142]。"""
    out: List[int] = []
    for m in _TICKET_REF_RE.finditer(str(text or "")):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def resolve_verify_target(text: str, candidates: List[Dict[str, Any]],
                          reply_to_msg_id: Any = 0
                          ) -> Tuple[Optional[Dict[str, Any]], str]:
    """确认/否认句该归到哪张单（决策 D5，纯函数）。

    返回 ``(ticket, how)``：
    - 文中 ``#N`` 且 N 在候选集 → 该单，``how="hash"``；
    - 无可用 ``#N`` 但本条是**对支持号回访消息的回复**（``reply_to_msg_id`` ==
      某候选的 ``notify_msg_id``）→ 该单，``how="reply"``；
    - 其余 → ``(None, reason)``：``no_candidates`` / ``hash_unknown``（带了号但
      不属该报障人待验集）/ ``no_anchor``（裸短句「可以了/好了」——群闲聊里出现
      概率太高，不翻单，调用方记 ``verify_ambiguous`` 事件交值守）。
    ``#N`` 优先于 reply_to：用户回着 A 的回访说「#B 好了」，以其明说的为准。
    """
    cands = [c for c in (candidates or []) if isinstance(c, dict)]
    if not cands:
        return None, "no_candidates"
    by_id = {}
    for c in cands:
        try:
            by_id[int(c.get("id") or 0)] = c
        except (TypeError, ValueError):
            continue
    refs = extract_ticket_refs(text)
    for n in refs:
        if n in by_id:
            return by_id[n], "hash"
    try:
        rmid = int(reply_to_msg_id or 0)
    except (TypeError, ValueError):
        rmid = 0
    if rmid > 0:
        for c in cands:
            try:
                if int(c.get("notify_msg_id") or 0) == rmid:
                    return c, "reply"
            except (TypeError, ValueError):
                continue
    if refs:
        return None, "hash_unknown"
    return None, "no_anchor"


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


#: C2（2026-09-02 两踩实锤）：值守可见性扫描面——被防刷屏限频静默的真反馈
#: （rate_capped_report）与使用咨询（usage）都不产生 bot 回执/工单，此前只存在
#: bug_events 台账里；duty_watchdog 默认把这两类纳入「未应答告警」。
DUTY_VISIBLE_EVENT_KINDS = ("rate_capped_report", "usage")


def list_events(kinds: Any = None, since_ts: float = 0.0, chat_id: Any = "",
                limit: int = 200) -> List[Dict[str, Any]]:
    """读 bug_events 台账（升序）。``kinds`` 为空＝不按类型过滤；``since_ts``＝只取
    该时刻之后；``chat_id`` 非空＝只看该群。供 duty_watchdog / 处置台消费。"""
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        q = "SELECT id, ts, chat_id, kind, reporter_id, detail FROM bug_events WHERE 1=1"
        args: List[Any] = []
        ks = [str(k).strip() for k in (kinds or []) if str(k).strip()]
        if ks:
            q += " AND kind IN (%s)" % ",".join("?" * len(ks))
            args.extend(ks)
        if float(since_ts or 0) > 0:
            q += " AND ts >= ?"
            args.append(float(since_ts))
        if str(chat_id or "").strip():
            q += " AND chat_id = ?"
            args.append(str(chat_id).strip())
        q += " ORDER BY ts ASC, id ASC LIMIT ?"
        args.append(max(1, min(int(limit or 200), 2000)))
        return [dict(r) for r in con.execute(q, args).fetchall()]
    except Exception:
        logger.debug("[bug_intake] 事件列表失败", exc_info=True)
        return []


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


# ── 序号连续性哨兵（C1-②，#123 族，2026-09-02）──────────────────────────────
# 0902 21:1x 实锤：群 mid=1084（用户提问原话）监听秒推与轮询兜底双双漏掉，
# 1085 正常到达 → 顶部比对的 gap-probe 报 gap=false，序号中间的洞没有任何
# 东西在看。supergroup 消息 id 按群单调连续（服务消息/已删消息也占号），
# 所以「本条 mid > 上一条 mid + 1」＝中间有号没到本进程＝候选漏收。
#
# 状态机（进程内，按 account:chat 分键；重启清零＝首条只立水位不判洞）：
#   note_group_msg_seq(cfg, chat, mid) —— 每条群消息（handler 顶部）调用：
#       · mid 是 pending 里的洞 → 晚到而已，摘掉，不告警；
#       · mid > last+1 → (last+1 .. mid-1) 入 pending（单跳 ≤ _SEQ_MAX_JUMP，
#         更大的跳号是停机窗，顶部链/backfill 管，这里只登记不逐 id 展开）；
#   due_seq_gaps(cfg, now) —— 宽限期（乱序到达的容忍窗）后仍在 pending 的洞
#       → 落 bug_events(seq_gap) + 告警 + 返回给调用方去云端定点补拉；
#   seq_gap_backfilled(chat, ids, filled, empty) —— 补拉结果记账（可见性）。
# 只对报障群启用（is_bug_group）；任何异常吞掉不影响主链。
_SEQ: Dict[str, Dict[str, Any]] = {}
_SEQ_GRACE_SEC = 20.0          # 乱序到达容忍窗：pending 洞至少等这么久再判漏
_SEQ_MAX_JUMP = 50             # 单次跳号超过此值不逐 id 展开（停机窗归别的链管）
_SEQ_MAX_PENDING = 200         # 单群 pending 上限（防异常放大）


def _seq_key(account_id: Any, chat_id: Any) -> str:
    return f"{str(account_id or '').strip()}:{str(chat_id).strip()}"


def seq_grace_sec() -> float:
    return _SEQ_GRACE_SEC


def note_group_msg_seq(config: Optional[Dict[str, Any]], chat_id: Any,
                       msg_id: Any, account_id: Any = "",
                       now: Optional[float] = None) -> List[int]:
    """登记一条已到达的报障群消息 id；返回**本条新暴露出的**候选洞列表（升序）。

    非报障群 / 无效 id → 空列表且不落任何状态。返回非空＝调用方应在
    ``seq_grace_sec()`` 后调 ``due_seq_gaps`` 收尾（补拉+告警）。
    """
    try:
        cfg = parse_cfg(config)
        cid = str(chat_id).strip()
        if not (cfg["enabled"] and cid in cfg["groups"]):
            return []
        try:
            mid = int(msg_id or 0)
        except (TypeError, ValueError):
            return []
        if mid <= 0:
            return []
        ts = float(now if now is not None else time.time())
        # J-5 A：消息一进实时链就登记「已观察」（早于去重/限频/observe——之后无论
        # 被哪一层压下，backfill 都不该再把它当漏网重喂）。持久化跨重启。
        try:
            from src.ops.bug_intake_backfill import mark_seen
            mark_seen(cid, mid, now=ts)
        except Exception:
            logger.debug("[bug_intake] mark_seen 失败（忽略）", exc_info=True)
        key = _seq_key(account_id, cid)
        with _LOCK:
            st = _SEQ.get(key)
            if st is None:
                # 首见（进程启动后第一条）：只立水位。重启窗口的洞归顶部链/backfill
                _SEQ[key] = {"last_mid": mid, "pending": {}, "chat_id": cid,
                             "account_id": str(account_id or "")}
                return []
            pending: Dict[int, float] = st["pending"]
            if mid in pending:
                pending.pop(mid, None)      # 晚到：洞自愈
                return []
            last = int(st.get("last_mid") or 0)
            if mid <= last:
                return []                   # 重投/编辑/乱序旧号：无新信息
            new_holes: List[int] = []
            span = mid - last - 1
            if 0 < span <= _SEQ_MAX_JUMP:
                for h in range(last + 1, mid):
                    if h not in pending and len(pending) < _SEQ_MAX_PENDING:
                        pending[h] = ts
                        new_holes.append(h)
            elif span > _SEQ_MAX_JUMP:
                logger.info(
                    "[bug_intake] 序号哨兵 大跳号 chat=%s %s→%s（跨 %s，交顶部"
                    "缺口链/backfill，不逐 id 展开）", cid, last, mid, span)
            st["last_mid"] = mid
            return new_holes
    except Exception:
        logger.debug("[bug_intake] 序号哨兵登记异常（忽略）", exc_info=True)
        return []


def _own_outbound_mids(config: Optional[Dict[str, Any]], account_id: str,
                       chat_id: str, ids: List[int]) -> Set[int]:
    """L-7 C（2026-09-06）：``ids`` 里哪些是**本账号经 API 发出的群消息**。

    0906 03:43 实锤：值守用支持号经 ``/api/unified-inbox/send`` 发出 mid 1224，
    TelegramClient 收不到自己 API 出站的 update → 1225 到达时 1224 成「洞」→ 宽限期后
    P1 ``seq_gap`` 告警 + 云端补拉（拉回的是自己那条）。出站在成功路径一定镜像进
    inbox ``messages``（direction='out'，conversation_id = platform:account:chat），
    这里只读 inbox.db 核一遍即可；任何异常返回空集＝退回旧行为（宁可多告警不漏告警）。
    """
    try:
        want = sorted({int(i) for i in ids if int(i) > 0})
        if not want or not str(account_id or "").strip():
            return set()
        raw = str(((config or {}).get("inbox") or {}).get("db_path") or "").strip()
        if raw:
            db = Path(raw)
        else:
            from src.licensing.data_paths import config_dir
            db = Path(config_dir()) / "inbox.db"
        if not db.is_file():
            return set()
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            conv = f"telegram:{str(account_id).strip()}:{str(chat_id).strip()}"
            q = ",".join("?" for _ in want)
            rows = con.execute(
                "SELECT platform_msg_id FROM messages WHERE conversation_id=?"
                " AND direction='out' AND CAST(platform_msg_id AS INTEGER) IN ("
                + q + ")", [conv, *want]).fetchall()
        finally:
            con.close()
        own: Set[int] = set()
        for (pmid,) in rows:
            try:
                own.add(int(pmid))
            except (TypeError, ValueError):
                continue
        return own
    except Exception:
        logger.debug("[bug_intake] 序号哨兵 自发消息核查失败（按旧行为告警）", exc_info=True)
        return set()


def due_seq_gaps(config: Optional[Dict[str, Any]],
                 now: Optional[float] = None,
                 grace_sec: Optional[float] = None,
                 account_id: Any = None) -> List[Dict[str, Any]]:
    """收割过了宽限期仍未自愈的洞：落台账 + 告警，返回
    ``[{account_id, chat_id, missing_ids, last_mid}]`` 供调用方云端定点补拉。

    每个洞只会被收割一次（从 pending 摘除）；补拉结果经 ``seq_gap_backfilled``
    记账。``account_id`` 非 None ＝只收割该账号的洞（同进程多 worker 各自收割
    各自的，别把别人的洞摘走却不补）。非报障群配置（已关闭）→ 清空状态返回空。
    L-7 C：收割前先剔掉本账号自己 API 出站的号（``_own_outbound_mids``）——只记
    ``seq_gap_own`` 事件，不告警不补拉；剔完为空的洞整组静默。
    """
    out: List[Dict[str, Any]] = []
    try:
        cfg = parse_cfg(config)
        ts = float(now if now is not None else time.time())
        grace = float(_SEQ_GRACE_SEC if grace_sec is None else grace_sec)
        only_acct = None if account_id is None else str(account_id or "").strip()
        harvested: List[Dict[str, Any]] = []
        with _LOCK:
            if not cfg["enabled"]:
                _SEQ.clear()
                return []
            for key, st in list(_SEQ.items()):
                cid = str(st.get("chat_id") or "")
                if cid not in cfg["groups"]:
                    _SEQ.pop(key, None)
                    continue
                if only_acct is not None and \
                        str(st.get("account_id") or "") != only_acct:
                    continue
                pending: Dict[int, float] = st.get("pending") or {}
                due = sorted(h for h, t0 in pending.items()
                             if ts - float(t0 or 0) >= grace)
                if not due:
                    continue
                for h in due:
                    pending.pop(h, None)
                harvested.append({
                    "account_id": str(st.get("account_id") or ""),
                    "chat_id": cid, "missing_ids": due,
                    "last_mid": int(st.get("last_mid") or 0),
                })
        for item in harvested:
            own = _own_outbound_mids(config, item["account_id"], item["chat_id"],
                                     item["missing_ids"])
            if own:
                own_ids = sorted(own)
                _bump("seq_gap_own", len(own_ids))
                _record_event(item["chat_id"], "seq_gap_own", "",
                              f"own={','.join(str(i) for i in own_ids)} last={item['last_mid']}")
                logger.info(
                    "[bug_intake] 序号哨兵 缺号为本账号出站 chat=%s acct=%s 号=%s（不告警不补拉）",
                    item["chat_id"], item["account_id"] or "-",
                    ",".join(str(i) for i in own_ids[:20]))
                item["missing_ids"] = [i for i in item["missing_ids"] if i not in own]
                if not item["missing_ids"]:
                    continue
            ids = item["missing_ids"]
            _bump("seq_gap")
            _record_event(
                item["chat_id"], "seq_gap", "",
                f"missing={','.join(str(i) for i in ids)} last={item['last_mid']}")
            logger.warning(
                "[bug_intake] 序号哨兵 跳号疑似漏收 chat=%s acct=%s 缺号=%s（已触发补拉）",
                item["chat_id"], item["account_id"] or "-",
                ",".join(str(i) for i in ids[:20]) + ("…" if len(ids) > 20 else ""))
            _publish_alert("seq_gap", {
                "chat_id": item["chat_id"], "missing_ids": list(ids),
                "last_mid": item["last_mid"], "seen_mid": item["last_mid"],
                "severity": "P1",
                "rate_key": f"bug_intake:seq_gap:{item['chat_id']}:{ids[0]}",
            })
            out.append(item)
    except Exception:
        logger.debug("[bug_intake] 序号哨兵收割异常（忽略）", exc_info=True)
    return out


def seq_gap_backfilled(chat_id: Any, missing_ids: List[int],
                       filled: int, empty: int, note: str = "") -> None:
    """补拉结果记账：``filled``＝补回的真消息数（重放进主链），``empty``＝核实
    为空的号（已删/服务消息）。差额（既没补回也没核实）＝拉取失败，日志可见。"""
    try:
        ids = [int(i) for i in (missing_ids or [])]
        _bump("seq_gap_filled", int(filled or 0))
        _bump("seq_gap_empty", int(empty or 0))
        unresolved = max(0, len(ids) - int(filled or 0) - int(empty or 0))
        detail = (f"missing={','.join(str(i) for i in ids)} filled={int(filled or 0)}"
                  f" empty={int(empty or 0)} unresolved={unresolved}"
                  + (f" note={str(note)[:80]}" if note else ""))
        _record_event(chat_id, "seq_gap_backfill", "", detail)
        logger.log(
            logging.WARNING if (filled or unresolved) else logging.INFO,
            "[bug_intake] 序号哨兵 补拉结果 chat=%s 缺号=%s 补回=%s 空洞=%s 未决=%s%s",
            chat_id, len(ids), int(filled or 0), int(empty or 0), unresolved,
            f" note={note}" if note else "")
    except Exception:
        logger.debug("[bug_intake] 序号哨兵记账异常（忽略）", exc_info=True)


def seq_sentinel_snapshot() -> Dict[str, Any]:
    """观测：各群水位与 pending 洞数（/api/admin/bug-intake stats 里可见）。"""
    with _LOCK:
        return {
            key: {"last_mid": int(st.get("last_mid") or 0),
                  "pending": len(st.get("pending") or {})}
            for key, st in _SEQ.items()
        }


# ── 主入口 ────────────────────────────────────────────────────────────────────
def trigger_verdict(config: Optional[Dict[str, Any]], chat_id: Any,
                    sender_id: Any, text: str,
                    now: Optional[float] = None,
                    has_photo: bool = False,
                    is_direct: bool = False,
                    account_id: Any = "", sender_name: str = "",
                    msg_id: Any = 0, reply_to_msg_id: Any = 0) -> Optional[bool]:
    """群触发三态（接线在 trigger._should_reply_to_group_message 顶部）。

    ``account_id`` / ``sender_name`` / ``msg_id`` / ``reply_to_msg_id``（J-5 C/E，
    可选）：限频压制路径里就地登记时透传给 ``observe_group_message``（调用方
    trigger.py 现状不传，工单 reporter 名退化为 id、reply 续单不生效——值守
    接线后补齐）。

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
        # 官方 bot 自咬环守卫（实施82 P0）：bot 代发后，回访/周公示会以「入站」
        # 形态回到观察管线，文案里满是 bug 词（工单标题原文）——不压制就会被
        # 自己的登记链当成新报障，dup 链滚雪球。硬压制 + 独立计数。
        try:
            from src.ops.bug_bot import is_official_bot
            if is_official_bot(sender_id):
                _bump("official_bot_suppressed")
                return False
        except Exception:
            logger.debug("[bug_intake] bot 守卫异常（放行后续闸门）",
                         exc_info=True)
        # 本方支持号守卫（2026-09-02）：值守用另一个支持号发的回访播报，对本
        # worker 是「他人消息」，镜像层拦不住——按 support_accounts 硬压制，
        # 防止满是 bug 词的播报文案引燃回复/立单。
        if str(sender_id or "").strip() in cfg["support_accounts"]:
            _bump("self_msg_suppressed")
            return False
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
        # J-5 C（决策 D4，2026-09-05）：限频**只管回复不管登记**——超额时
        # _report_like 的消息就地走 observe_group_message（立单/收集窗补录/
        # verify 归属全套），只是 return False 不回复；rate_capped_report 事件
        # 保留但语义改为「已登记、未回复」（detail 带 [registered #N] 前缀）。
        # 0904 22:55 / 00:58 / 01:11 skuio 三条真报障被拍扁＝旧语义的代价；
        # AI 静默档下限频本就没有第二个效果。`rate_limit_registration: true`
        # 回旧语义（超额连登记一起丢）。
        _report_like = bool(
            (t and classify_message(t) in ("bug", "usage", "feedback"))
            or (has_photo and t)
            or _in_collect_window(cid, sender_id, cfg["collect_window_min"], ts)
            or (t and detect_verify_intent(t)
                and candidate_verify_tickets(cid, sender_id, now=ts)))

        def _note_capped_report() -> None:
            if not _report_like:
                return
            _bump("rate_capped_report")
            tag = ""
            if not cfg["rate_limit_registration"]:
                try:
                    res = observe_group_message(
                        config, chat_id=cid, account_id=account_id,
                        reporter_id=sender_id,
                        reporter_name=str(sender_name or ""),
                        text=t, now=ts, msg_id=msg_id,
                        reply_to_msg_id=reply_to_msg_id, count_reply=False)
                    if res.get("dup"):
                        tag = "[dup] "
                    elif res.get("ticket_id"):
                        tag = (f"[registered #{res['ticket_id']} "
                               f"{res.get('category') or ''}] ")
                    else:
                        tag = f"[observed {res.get('category') or '-'}] "
                except Exception:
                    logger.warning("[bug_intake] 限频路径就地登记失败",
                                   exc_info=True)
                    tag = "[register_failed] "
            _record_event(cid, "rate_capped_report", sender_id,
                          (tag + t)[:300])
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
            # J-5 D（决策 D4 后半）：已知报障人「图 + 任意文字」＝报障材料——
            # 文字判 other 也不许进闲聊静默，就地 observe 立单（收集窗内则由
            # observe 自然补录）。返回 False：AI 静默档下与 True 等价；AI 开
            # 启档下该形态此前本就是 smalltalk_suppressed，不算回归。
            if (classify_message(t) == "other"
                    and not detect_verify_intent(t)
                    and _in_collect_window(cid, sender_id,
                                           cfg["collect_window_min"], ts) is None
                    and is_known_reporter(cid, sender_id)):
                try:
                    observe_group_message(
                        config, chat_id=cid, account_id=account_id,
                        reporter_id=sender_id,
                        reporter_name=str(sender_name or ""),
                        text=t, now=ts, msg_id=msg_id,
                        reply_to_msg_id=reply_to_msg_id, count_reply=False,
                        has_photo=True)
                    # 新单已开收集窗 → 这张图本身记进它的证据链
                    note_screenshot(config, cid, sender_id, now=ts)
                except Exception:
                    logger.warning("[bug_intake] 已知报障人随图立单失败",
                                   exc_info=True)
                return False
        # 收集窗内的后续消息：持续接话（用户在补工单信息，别装死）
        if _in_collect_window(cid, sender_id, cfg["collect_window_min"], ts):
            _bump("engaged")
            return True
        # J-5 E-2：回复支持号消息（回访 / 带 #N 播报）＝续写那张单，超出 30 分钟
        # 收集窗也放进 observe（否则判 other 落 smalltalk_suppressed，续写丢失）
        if t and ticket_for_reply(cid, reply_to_msg_id, sender_id):
            _bump("engaged")
            return True
        if not t:
            return False
        # 修复确认/否认（verified 自动闭环）：无报障关键词也要接话
        # （接话≠翻单：归属由 observe 的 resolve_verify_target 定，这里只判
        #  「有待验单可能在说它」——AI 静默档下仅影响 engaged 计数）
        if detect_verify_intent(t) and candidate_verify_tickets(
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
        # L-7 C：向值守要清单/进度/答复 ≠ 闲聊——AI 不接（不许编台账），但记事件
        # + 推值守告警（每用户 10 分钟去抖），值守人工答。
        if is_info_request(t):
            _bump("request_for_info")
            _record_event(cid, "request_for_info", sender_id, t[:200])
            if _rate_ok(f"q:{cid}:{sender_id}", 1, ts, window_sec=600.0):
                _rate_mark(f"q:{cid}:{sender_id}", ts)
                _publish_alert("request_for_info", {
                    "chat_id": cid, "reporter": str(sender_id),
                    "text": t[:200], "severity": "P2",
                    "rate_key": f"bug_intake:request_for_info:{cid}:{sender_id}"})
            return False
        # 报障群不陪聊：非报障、非点名一律静默。
        # J-5 D：记事件（此前无痕——0904 五句漏网报障连一条痕迹都没有，值守
        # 面板从这里就能盯「被静默的都是什么」，词表缺口照单补）。
        _bump("smalltalk_suppressed")
        _record_event(cid, "smalltalk_suppressed", sender_id, t[:200])
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


_OBSERVED_MID: Dict[str, Dict[int, float]] = {}      # chat → {mid: ts}
_OBSERVED_TXT: Dict[str, Tuple[float, int]] = {}     # "chat:reporter:norm(text)" → (ts, mid)
_OBSERVED_TXT_WINDOW_SEC = 60.0
_OBSERVED_MAX = 4000


def _observe_dedup_locked(cid: str, reporter_id: Any, text: str,
                          mid: int, ts: float) -> bool:
    """observe 入口的二次观察判定（调用方持 ``_LOCK``）。返回 True＝重复。

    - ``mid>0``：同群同 mid 已观察过 → 重复（精确）；
    - 近似兜底（实时链目前不传 mid——skill_manager 调用点没有 msg_id）：同群
      同 reporter 同归一化文本、``_OBSERVED_TXT_WINDOW_SEC`` 内 → 重复，**但**
      两侧都带 mid 且不同时不算（用户 60s 内真发两句一样的话是两条消息）。
    非重复即登记；两表有界。
    """
    key = f"{cid}:{str(reporter_id or '').strip()}:{normalize_title(text)}"
    mids = _OBSERVED_MID.setdefault(cid, {})
    if mid > 0 and mid in mids:
        return True
    prev = _OBSERVED_TXT.get(key)
    if prev is not None:
        pts, pmid = prev
        if 0.0 <= ts - pts <= _OBSERVED_TXT_WINDOW_SEC \
                and (mid <= 0 or pmid <= 0 or pmid == mid):
            return True
    if mid > 0:
        mids[mid] = ts
        if len(mids) > _OBSERVED_MAX:
            for old in sorted(mids.keys())[:len(mids) // 2]:
                mids.pop(old, None)
    _OBSERVED_TXT[key] = (ts, mid)
    if len(_OBSERVED_TXT) > _OBSERVED_MAX:
        for k, _ in sorted(_OBSERVED_TXT.items(),
                           key=lambda kv: kv[1][0])[:len(_OBSERVED_TXT) // 2]:
            _OBSERVED_TXT.pop(k, None)
    return False


def observe_group_message(
    config: Optional[Dict[str, Any]], *, chat_id: Any, account_id: Any,
    reporter_id: Any, reporter_name: str, text: str,
    now: Optional[float] = None, msg_id: Any = 0, reply_to_msg_id: Any = 0,
    count_reply: bool = True, has_photo: bool = False,
) -> Dict[str, Any]:
    """报障群消息观察（接线在 skill_manager.process_message 群 hint 之后）。

    ``reply_to_msg_id``（J-5 B，可选）：本条所回复的消息 id；等于某待验单的
    ``notify_msg_id``（bot 回访消息）时确认/否认归属该单。不带＝只认文中 ``#N``。
    ``count_reply``（J-5 C）：False＝本次只登记不回复（限频压制路径 / 回放链），
    不记回复限频——否则被拍扁的登记反过来把预算烧得更死。
    ``has_photo``（J-5 D）：本条随图。已知报障人（本群曾立单）随图的任意文字
    判 other 时升格为 bug 立单/补录（事件 ``photo_report``）——截图配「这个」
    就是报障材料，不是闲图。

    返回 {active, category, ticket_id, is_new, severity, report_count,
    prompt_block, footer}；active=False 时其余键为空——调用方零分支透传。

    ``msg_id``（J-5 A，可选）：带上时按 (chat, mid) 精确判「二次观察」；不带
    （实时链 skill_manager 现状）退到 (reporter, 文本, 60s) 近似去重。命中重复
    → ``active=False, dup=True``，不落事件不改工单不计限频——0904 实录同一句
    「都好了」实时被限频压下、4 分钟后 backfill 重喂又在另一张单上 verify_yes。
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
        try:
            mid = int(msg_id or 0)
        except (TypeError, ValueError):
            mid = 0
        # 立单链自身守卫（2026-09-02 #123/#125/#135/#136 误立单收口）：本方
        # 账号（support_accounts / 本 worker / 官方 bot）的群发绝不进登记链
        # ——回访播报满是 bug 词与工单号，混进来就成新工单。实时链与补拉链
        # （bug_intake_backfill 回喂）共用本入口，一处收口两条链都干净。
        if _is_own_sender(cfg, reporter_id, account_id):
            _bump("self_msg_suppressed")
            # J-5 E-2：记下本方消息 mid → 文中 #N，报障人「回复这条」时据此续单。
            # detail 前缀 ``mid=<id> refs=#a,#b | `` 固定格式——ticket_for_reply
            # 重启后靶它反查，别改。
            refs = _remember_own_message(cid, mid, t) if mid > 0 else []
            prefix = (f"mid={mid} refs=" + ",".join(f"#{r}" for r in refs) + " | "
                      if mid > 0 and refs else "")
            _record_event(cid, "self_msg_suppressed", reporter_id,
                          prefix + t[:200])
            return out
        with _LOCK:
            dup = _observe_dedup_locked(cid, reporter_id, t, mid, ts)
        if dup:
            _bump("observe_dup")
            out["dup"] = True
            logger.info("[bug_intake] 二次观察短路 chat=%s mid=%s reporter=%s",
                        cid, mid or "-", reporter_id)
            return out
        if mid > 0:
            try:
                from src.ops.bug_intake_backfill import mark_seen
                mark_seen(cid, mid, now=ts)
            except Exception:
                logger.debug("[bug_intake] mark_seen 失败（忽略）", exc_info=True)
        out["active"] = True
        # 2026-08-21 11:05 老板纪律（B36 收紧版）：报障群是「真正解决问题的群」，
        # 群内登记/回复/整理全部由值守人工来——AI（本地模型/云端）一条不发。
        # observe 的工单登记/告警/收集窗照常运转（值守的内部台账工具），
        # skill_manager 消费本标记在生成前短路（模型不跑，比丢弃草稿省 16-45s）。
        out["ai_silent"] = bool(cfg.get("ai_silent"))
        _bump("observed")
        # 本轮要回复 → 记限频（mention/回复链路径也从这里计数）。
        # J-5 C：限频压制路径就地登记 / 回放链 count_reply=False 不计数——
        # 超额后登记不再反过来把预算烧得更死。（ai_silent 档预算照记：那只影响
        # verdict 三态，登记已由 trigger_verdict 压制路径兜住，不必改语义。）
        if count_reply:
            _rate_mark(f"u:{cid}:{reporter_id}", ts)
            _rate_mark(f"g:{cid}", ts)

        # verified 自动闭环（P3）：已回访的 fixed 单 + 确认/否认口径 → 直接流转。
        # J-5 B（决策 D5）：归属只认文中 #N 或「回复 bot 回访消息」；裸短句
        # 「可以了/好了」有待验单却定不了是哪张 → 不翻状态，只记
        # verify_ambiguous 事件（值守面板可见），消息继续走普通链。
        _vi = detect_verify_intent(t)
        if _vi:
            _cands = candidate_verify_tickets(cid, reporter_id, now=ts)
            vt, _how = resolve_verify_target(t, _cands, reply_to_msg_id)
            if vt is None and _cands:
                _bump("verify_ambiguous")
                _record_event(
                    cid, "verify_ambiguous", reporter_id,
                    f"{_vi}:{_how} cands="
                    + ",".join(f"#{c.get('id')}" for c in _cands[:8])
                    + f" | {t[:120]}")
                logger.info(
                    "[bug_intake] verify 归属不明（%s/%s）chat=%s reporter=%s "
                    "候选=%s 不翻单", _vi, _how, cid, reporter_id,
                    [c.get("id") for c in _cands[:8]])
            if vt is not None:
                vtid = int(vt.get("id") or 0)
                if _vi == "yes":
                    set_ticket_status(vtid, "verified")
                    _bump("verify_yes")
                    _record_event(cid, "verify_yes", reporter_id,
                                  f"#{vtid} via={_how}")
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
                    _record_event(cid, "verify_no", reporter_id,
                                  f"#{vtid} via={_how}")
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
        # J-5 E-2：回复支持号消息（回访 / 带 #N 播报）＝续写那张单，**不看**
        # 30 分钟收集窗，且显式锚点压过时间窗（窗内正在补录别的单也改锚这张）。
        # 0905 #165：追问距首报 41 分钟超窗被立成新单，其实是 #164 的续写。
        _rt = ticket_for_reply(cid, reply_to_msg_id, reporter_id)
        if _rt and (st is None or int(st.get("ticket_id") or 0) != _rt):
            st = {"ticket_id": _rt, "ts": ts, "got": set()}
            _bump("reply_continue")
            _record_event(cid, "reply_continue", reporter_id,
                          f"#{_rt} rmid={reply_to_msg_id} | {t[:120]}")
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
        if (cat == "other" and has_photo and t
                and is_known_reporter(cid, reporter_id)):
            # J-5 D：已知报障人「图 + 任意文字」→ 按报障立单（不进闲聊静默）
            cat = "bug"
            _bump("photo_report")
            _record_event(cid, "photo_report", reporter_id, t[:200])
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
    try:
        d["seq_sentinel"] = seq_sentinel_snapshot()
    except Exception:
        d["seq_sentinel"] = {}
    return d


__all__ = [
    "parse_cfg", "is_bug_group", "voice_suppressed", "classify_message",
    "classify_severity", "is_crisis", "normalize_title", "titles_similar",
    "record_bug_ticket", "append_ticket_note", "set_ticket_status", "merge_ticket",
    "list_tickets", "get_ticket", "build_fix_notify_text", "build_reply_text",
    "resolve_update_hint", "mark_notified",
    "detect_verify_intent", "pending_verify_ticket", "candidate_verify_tickets",
    "extract_ticket_refs", "resolve_verify_target", "is_known_reporter",
    "ticket_for_reply",
    "list_pending_notify",
    "note_screenshot", "trigger_verdict", "observe_group_message",
    "dump_stats", "reset_state_for_tests", "VALID_STATUSES",
    "is_info_request", "INFO_REQUEST_WORDS",
    "note_group_msg_seq", "due_seq_gaps", "seq_gap_backfilled",
    "seq_grace_sec", "seq_sentinel_snapshot",
    "list_events", "DUTY_VISIBLE_EVENT_KINDS",
]
