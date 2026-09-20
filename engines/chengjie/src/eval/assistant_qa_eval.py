# -*- coding: utf-8 -*-
"""小智「产品问答」检索质量评测（实施74 P2，2026-08-27）。

## 为什么需要它

小智的核心定位是**解决软件使用问题**，而它能不能答上来，第一步取决于
`help_kb.search()`（BM25）能不能把对的条目捞出来——LLM 只是把捞到的条目
组织成话。2026-08-27 语料从 32 条 how-to 扩到 57 条，覆盖 30/40 个页面，
但「补进去的条目能不能被真实问法检索到」**零数据**：关键词是人按经验写的，
写偏了就是「补了等于没补」，而这种失败**完全静默**（用户只会看到一句
「不知道」，没有任何信号说明是语料缺失还是关键词没对上）。

本模块是全仓 AI 能力里最后一个没有离线评测的（翻译/记忆/人设/情绪/危机/
命理/语音一致性都有 `src/eval/` 轨与门禁）。

## 设计

- **只评检索、不评 LLM 措辞**：检索是确定性的（BM25，无网络无模型），
  因此可以做**常驻门禁**；LLM 答得好不好属另一层，需要 `EVAL_LLM` opt-in，
  不在本模块范围。检索捞不到，后面再好的模型也只能拒答。
- **语料从代码构建**（`seed_corpus.build_all_entries()`）而不是读线上库：
  评的是「随包发出去的那份语料」，与客户新装机首启拿到的完全一致；也避免
  被某台实例手工灌过的数据污染。
- **金标问句刻意不照抄标题**。照抄标题去搜必然命中，那是自证的同义反复、
  零鉴别力。这里全部按**用户真会怎么打字**来写（口语、别名、错位说法），
  所以基线不会是 100%——**低分本身就是产出**，它直接告诉你该往哪条的
  keywords 里补什么词。
- **同时评正反两面**：正样本（`GOLD`，该答的）测命中率，负样本
  （`NEGATIVE`，该拒的）测拒答率。只测正面会得出「阈值越低越好」的错误结论。

## 校准结论（2026-08-27，min_score 1.6 → 35 的依据）

线上此前用 `min_score=1.6` 判「有没有把握回答」。实测真实 BM25 分数落在
**25~240** 区间——**这道闸门从来没拦下过任何东西**，「无依据就诚实拒答」
这条设计从未生效。线上 qa_log 里 `answered=1` 却答非所问，就是这么来的。

40 正样本 / 15 负样本实测：

| 阈值 | 误拒正样本 | 拦住负样本 | 备注 |
|---|---|---|---|
| 1.6 | 0/40 | 1/15 | 只拦得住纯乱码 |
| 35 | 0/40 | **7/15** | **取这个**，离最低正样本 41.2 留 6.2 余量 |
| 40 | 0/40 | 8/15 | 只多拦 1 条，余量却只剩 1.2，太脆 |
| 50 | 3/40 | 8/15 | 开始误拒真问题 |

**正负样本分布重叠**（正样本最低 41.2、负样本最高 109.8），所以单一 BM25
阈值**不可能干净分开**，只能选代价点。

**刻意不追求拦住全部**：剩下的负样本几乎都是 `product` 类——「能不能自动
打电话给客户」「怎么把数据迁移到别的服务器」这种**产品形状但语料没覆盖**的
问题，用的全是产品词汇、分数天然高（72~110），落在正样本分布中间。抬阈值
只会先误拒真问题。这一类要靠出站前的**语义核对**（「检索到的条目真能回答
这个问题吗」）才拦得住，属下一阶段。语料补覆盖后要把对应负样本**转正**
（2026-09-01 首例：抖音/导出手机号两条，见 NEGATIVE 头注）。

⚠ 实例配置默认**不带** `assistant.query` 段，所以 `assistant_routes` 里那个
硬编码回落值才是线上真正生效的值——只改 `config.example.yaml` 对生产无效。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 生产默认（assistant_routes 里的硬编码回落值，实例配置默认不带 assistant.query
# 段，所以那个默认值就是线上真正生效的值）。评测必须与它同口径，否则
# 「评测过了线上还是拒答」。
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 35.0

# 负样本：**应该被诚实拒答**的问题（语料里没有依据）。
# 三类刻意分开，因为它们的可拦截性差别极大（见模块 docstring「校准结论」）：
#   chat/common —— 闲聊与常识，用词与产品无关，分数天然低，好拦；
#   product     —— **产品形状但语料没覆盖**，用的全是产品词汇，分数天然高，
#                  单靠 BM25 分数拦不住，是最危险的一类（会被自信地硬答）；
#   garbage     —— 纯乱码，零命中。
# 2026-09-01 金标迁移：「支持抖音吗」「怎么导出所有客户的手机号」原是 product 类
# 负样本（产品形状但语料没覆盖）——gap_report 首跑坐实它们是真实用户高频问题后，
# howto_pack 补了**如实**条目（四平台清单+暂不支持 / CSV 字段+明说不含手机号），
# 「该拒答」的存在理由消失，两条转入 GOLD 正样本（见下）。负样本的 product 类
# 剩余成员仍然成立：语料覆盖到哪天，就哪天照此迁移，别为拦截率硬留过期负样本。
NEGATIVE: Tuple[Tuple[str, str], ...] = (
    ("今天天气怎么样", "chat"),
    ("帮我写一首关于春天的诗", "chat"),
    ("给我讲个笑话", "chat"),
    ("美国总统是谁", "common"),
    ("红烧肉怎么做才好吃", "common"),
    ("明天股市会涨吗", "common"),
    ("能不能自动打电话给客户", "product"),
    ("可以对接我们公司的 ERP 系统吗", "product"),
    ("能不能让 AI 自动给客户报价", "product"),
    ("有没有安卓 App", "product"),
    ("怎么把数据迁移到别的服务器", "product"),
    ("支持多少个并发坐席", "product"),
    ("zzzzqwertyuiopasdfghjkl1234567890notatopic", "garbage"),
)

# (用户问法, 期望命中的条目 id, 备注/这条在考什么)
# 写法纪律：问句用**用户的词**，不许把条目标题抄进来。
GOLD: Tuple[Tuple[str, str, str], ...] = (
    # ── 总览：新用户的第一句话（2026-08-29 老板实录「回复的内容没一点帮助」）
    # 当时 44 条 how-to 全是**具体任务**，没有一条讲「整体怎么用」。更隐蔽的是
    # 它不是零命中：纯词汇检索把「使用」匹配到 multiwin 的「在此**使用**/保持
    # 待机」(62.01)、「操作」匹配到「**操作**记录」(79.59)，双双越过 min_score，
    # 于是走 NO_BASIS 哨兵诚实拒答——看起来像「模型不行」，实为语料缺一条。
    # 这三条钉住那个缺口：只要有人把 getting-started 删了或稀释了它的词表，先红。
    # 刻意只钉两条：第三条候选「整个流程是怎样的」只有 33.61 分（TOP1 对但不过
    # min_score），而要抬它就得往词表塞「怎样/整个」这类零区分度疑问词——正是
    # 上面那个把红烧肉吸过来的坑。金标宁可少一条，也不能为了变绿去污染词表。
    ("怎么使用这个软件", "howto:getting-started", "老板实录原话；曾假命中 multiwin"),
    ("小白怎么上手", "howto:getting-started", "纯口语，与条目标题零重叠"),
    # ── 接入账号（客户装机后的第一件事）──────────────────────────────────
    ("我想加一个新的电报号", "howto:connect-telegram", "别名：电报=Telegram"),
    ("tg 怎么登录账号", "howto:connect-telegram", "缩写 tg"),
    ("line 号在哪里登录", "howto:connect-line", "平台名小写"),
    ("messenger 掉线了怎么重新登录", "howto:connect-messenger",
     "掉线重登是 Messenger 独有能力"),
    ("whatsapp 显示离线了怎么办", "howto:connect-whatsapp", "口语「显示离线」"),
    # ── 团队与权限 ──────────────────────────────────────────────────────
    ("怎么给员工开一个账号", "howto:add-agent-user", "口语：员工/开账号"),
    ("怎么新增一个客服人员", "howto:add-agent-user", "同义：客服人员=坐席"),
    ("我怀疑账号被别人登录了", "howto:kick-sessions", "场景化描述，非功能名"),
    ("忘记密码了怎么办", "howto:change-password", "既有条目，反向验证不退化"),
    # ── 花钱与额度 ──────────────────────────────────────────────────────
    ("字符不够用了在哪充值", "howto:usage-quota", "口语：字符不够/充值"),
    ("怎么看这个月花了多少", "howto:usage-quota", "完全口语化"),
    ("在哪里买套餐", "howto:membership-page", "口语：买套餐"),
    # ── 日常运营 ────────────────────────────────────────────────────────
    ("有客户要找真人怎么办", "howto:escalation-config", "场景描述 → 配置项"),
    ("哪些对话需要我亲自处理", "howto:cases-page", "场景描述 → 案例跟进"),
    ("怎么知道哪个客户快流失了", "howto:relations-health-page", "口语"),
    ("能不能让 AI 主动关心客户", "howto:care-schedule-page", "能力提问"),
    ("AI 记错了客户的事情", "howto:episodic-memory-page", "问题描述 → 记忆页"),
    ("怎么让 AI 答得更准", "howto:learner-page", "目标描述 → 学习队列"),
    ("我想看每个客服接了多少", "howto:queue-board", "口语：客服接了多少"),
    # ── 发消息与媒体（既有条目，防扩容后退化）──────────────────────────
    ("怎么发语音给客户", "howto:send-voice", "既有条目"),
    ("能发图片吗", "howto:send-media", "既有条目"),
    ("客户发的图看不懂什么意思", "howto:image-translate", "场景 → 图片翻译"),
    ("不想让 AI 自动回了", "howto:takeover", "口语：不想让 AI 自动回"),
    # ── 出问题时 ────────────────────────────────────────────────────────
    ("软件坏了找谁", "howto:report-bug", "口语：软件坏了"),
    ("我提交的问题处理得怎么样了", "howto:ticket-status", "口语追问进度"),
    ("顶上有个红条说通道离线", "howto:channel-down", "照着界面描述"),
    ("回复变得很慢", "howto:ai-degraded", "症状描述"),
    ("账号被封了", "howto:account-banned", "既有条目"),
    # ── 界面与个性化 ────────────────────────────────────────────────────
    ("界面能调成黑色的吗", "howto:personal-settings", "口语：黑色=暗色主题"),
    ("怎么换成英文界面", "howto:lang-switch", "既有条目"),
    # ── 管理与审计 ──────────────────────────────────────────────────────
    ("谁把设置改了", "howto:audit-log", "口语：谁改的"),
    ("我想导出操作记录", "howto:audit-log", "明确动作"),
    ("有没有那种一页看全部数据的", "howto:ops-overview-page", "口语描述"),
    # ── 边界：该被引导「你不用管这页」的 ────────────────────────────────
    ("开发者工具的密码是多少", "howto:developer-page",
     "必须命中该条（条目会说去找技术支持，而不是给出密码）"),
    ("在哪看服务器日志", "howto:logs-page", "技术页，条目会说明日常用不到"),
    # ── 设置类 ──────────────────────────────────────────────────────────
    ("AI 说话太啰嗦了怎么调短", "howto:ai-style", "既有条目，口语"),
    ("怎么让 AI 别自己发消息给客户", "howto:proactive-config", "既有条目"),
    # 刻意避开标题措辞（标题是「发出去的消息会自动翻译吗」，只改一个字＝抄题，
    # 首版就是这么写的、被 test_gold_questions_do_not_copy_titles 当场抓出）
    ("客户是外国人我打中文行不行", "howto:outbound-translate", "场景描述而非功能名"),
    ("知识库怎么加东西", "howto:kb-add", "既有条目"),
    ("新手教程在哪", "howto:rewatch-tour", "既有条目"),
    # ── 真实缺口批②（2026-09-01 gap_report 首跑高频未答 → 补语料 → 转正）───
    ("帮我写一句给客户的问候语", "howto:ai-draft-for-me",
     "老板拍板口径：引到主输入框「AI回复」拟稿；隔壁「写一首诗」仍是负样本"),
    ("怎么导出所有客户的手机号", "howto:export-contacts",
     "原 product 负样本转正：条目如实说 CSV 不含手机号 + 列表导出入口"),
    ("支持抖音吗", "howto:supported-platforms",
     "原 product 负样本转正：如实答四平台清单 + 抖音暂不支持"),
    # ── 用户管理 P0 实录（2026-09-11「子帐号登不上 / 退出无效」）：随包出货的语料
    # 必须答得上这三问，否则客户问小智全是「没有找到可靠依据」（发版纪律第 3 条）
    ("坐席忘记密码了怎么办", "howto:reset-user-password", "口语：忘记密码 → 管理员代重置"),
    ("刚开的账号登录不进去", "howto:sub-account-cannot-login", "老板实录场景，口语"),
    ("我想让坐席用自己的号登桌面端", "howto:logout-switch-account", "退出→登录页→子帐号"),
)


def build_eval_kb(db_path: Optional[str] = None) -> Any:
    """用**代码里的语料**（随包发货的那份）建一个临时 HelpKB。

    刻意不读线上 `assistant_help.db`：那份可能被某台实例手工灌过，评出来的
    数字不代表客户装机拿到的东西。
    """
    from src.assistant.help_kb import HelpKB
    from src.assistant.seed_corpus import build_all_entries

    if db_path is None:
        tmp = tempfile.mkdtemp(prefix="asb_eval_")
        db_path = str(Path(tmp) / "help_eval.db")
    kb = HelpKB(db_path)
    kb.upsert_entries(build_all_entries())
    return kb


def evaluate_assistant_qa(
    kb: Any = None,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    gold: Optional[Tuple[Tuple[str, str, str], ...]] = None,
) -> Dict[str, Any]:
    """跑金标问句，统计检索命中率。

    三个口径（逐级严格）：
      * ``top1`` —— 期望条目排第一（用户一眼看到的就是对的）
      * ``topk`` —— 期望条目进 top_k（LLM 拿到了正确素材，答得对的前提）
      * ``answerable`` —— 最高分 ≥ min_score（**过线才不会被拒答**；
        与生产同阈值，这一项不达标 = 线上直接一句「不知道」）

    ``misses`` 逐条列出没进 top_k 的问句与实际捞到的条目——这就是补关键词的
    照单，比一个总分有用得多。
    """
    kb = kb or build_eval_kb()
    samples = gold if gold is not None else GOLD
    top1 = topk = answerable = 0
    misses: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for q, expect, note in samples:
        hits = kb.search(q, top_k=top_k) or []
        ids = [str(h.get("id") or "") for h in hits]
        best = float(hits[0].get("score") or 0.0) if hits else 0.0
        in_top1 = bool(ids) and ids[0] == expect
        in_topk = expect in ids
        ok_score = best >= min_score
        top1 += 1 if in_top1 else 0
        topk += 1 if in_topk else 0
        answerable += 1 if ok_score else 0
        rows.append({"q": q, "expect": expect, "got": ids, "score": round(best, 2),
                     "top1": in_top1, "topk": in_topk, "answerable": ok_score})
        if not in_topk:
            misses.append({"q": q, "expect": expect, "got": ids,
                           "score": round(best, 2), "note": note})
    n = len(samples) or 1

    # 负样本：应被拒答（最高分 < min_score）。分类统计——product 类是最危险的
    # 一档（用产品词汇提问、分数天然高），单独看它的拦截率才知道真实风险。
    neg_rows: List[Dict[str, Any]] = []
    refused = 0
    by_kind: Dict[str, Dict[str, int]] = {}
    for q, kind in NEGATIVE:
        hits = kb.search(q, top_k=top_k) or []
        best = float(hits[0].get("score") or 0.0) if hits else 0.0
        top = str(hits[0].get("id") or "") if hits else ""
        ok = best < min_score
        refused += 1 if ok else 0
        b = by_kind.setdefault(kind, {"n": 0, "refused": 0})
        b["n"] += 1
        b["refused"] += 1 if ok else 0
        neg_rows.append({"q": q, "kind": kind, "score": round(best, 2),
                         "would_answer_with": top, "refused": ok})
    nn = len(NEGATIVE) or 1

    return {
        "n": len(samples),
        "top1": top1, "topk": topk, "answerable": answerable,
        "top1_rate": round(top1 / n, 4),
        "topk_rate": round(topk / n, 4),
        "answerable_rate": round(answerable / n, 4),
        "top_k": top_k, "min_score": min_score,
        "misses": misses,
        "rows": rows,
        # 负样本口径
        "neg_n": len(NEGATIVE),
        "refused": refused,
        "refusal_rate": round(refused / nn, 4),
        "refusal_by_kind": by_kind,
        "neg_rows": neg_rows,
        # 校准余量：正样本最低分离阈值还有多远（越小越脆）
        "pos_min_score": round(min([r["score"] for r in rows], default=0.0), 2),
        "margin": round(min([r["score"] for r in rows], default=0.0) - min_score, 2),
    }


def targets() -> Dict[str, float]:
    """门禁阈值（env 可覆盖，便于收紧而不改代码）。"""
    # 基线（2026-08-27 补完关键词后实测，BM25 确定性无抖动）：top1 75% / top3 100%。
    #
    # **top3 是硬底线（1.0，不留余量）**：进不了 top3 = LLM 手里没有正确素材，
    # 后面答得再漂亮也是编的，必须当场修。
    #
    # **top1 是趋势指标（门槛 0.65，低于实测值）**，刻意留余量——逐条看过 10 条
    # top1 未命中，**全部**是「同一页面的 `page:` 入口条目压过了 `howto:` 操作
    # 条目」（如「知识库怎么加东西」第一名是 page:knowledge）。两者都相关，属
    # 近似命中而非错误，为它把构建拖红不值。真正想改进的是让「怎么…」类问句
    # 优先 howto 源——那要动 BM25 权重，属下一阶段。
    return {
        "topk": float(os.environ.get("AITR_ASB_TOPK_TARGET", "1.0")),
        "top1": float(os.environ.get("AITR_ASB_TOP1_TARGET", "0.65")),
        "answerable": float(os.environ.get("AITR_ASB_ANSWERABLE_TARGET", "1.0")),
    }


def format_report(res: Dict[str, Any]) -> str:
    """人读报告（CLI 与门禁失败信息共用，口径单一）。"""
    lines = [
        f"小智问答检索评测：{res['n']} 条金标"
        f"（top_k={res['top_k']} min_score={res['min_score']}）",
        f"  命中 top1 : {res['top1']}/{res['n']}  ({res['top1_rate']:.0%})",
        f"  命中 top{res['top_k']} : {res['topk']}/{res['n']}  ({res['topk_rate']:.0%})",
        f"  可作答     : {res['answerable']}/{res['n']}  ({res['answerable_rate']:.0%})"
        "   ← 低于阈值即线上拒答",
        f"  校准余量   : 正样本最低分 {res.get('pos_min_score', '?')}"
        f"（离阈值 +{res.get('margin', '?')}）",
    ]
    if res.get("neg_n"):
        lines.append(
            f"  诚实拒答   : {res['refused']}/{res['neg_n']}"
            f"  ({res['refusal_rate']:.0%})   ← 无依据的问题被拦下的比例")
        for kind in sorted(res.get("refusal_by_kind") or {}):
            b = res["refusal_by_kind"][kind]
            lines.append(f"      {kind:<8} {b['refused']}/{b['n']}")
        leaked = [r for r in res.get("neg_rows", []) if not r["refused"]]
        if leaked:
            lines.append(f"  仍会硬答 {len(leaked)} 条（分数天然高，需语义核对才拦得住）：")
            for r in leaked:
                lines.append(f"    {r['score']:>7} 「{r['q']}」")
                lines.append(f"            → 会拿 {r['would_answer_with']} 去答")
    if res["misses"]:
        lines.append(f"  未命中 {len(res['misses'])} 条（补 keywords 照单）：")
        for m in res["misses"]:
            got = ", ".join(m["got"][:3]) or "（零命中）"
            lines.append(f"    「{m['q']}」 期望 {m['expect']}")
            lines.append(f"        实得 {got}  best={m['score']}")
    return "\n".join(lines)


__all__ = [
    "GOLD", "build_eval_kb", "evaluate_assistant_qa", "targets",
    "format_report", "DEFAULT_TOP_K", "DEFAULT_MIN_SCORE",
]
