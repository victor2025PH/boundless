"""知识库冷启动起步包（P1-2）— 让空 KB「一键有话术」。

新用户接入渠道后最大的断层是：KB 是空的，AI 没有私域知识可答。本模块提供
**按场景的起步 FAQ 包**（与 P0-2 config 预设同域：ecommerce / payment / outreach
+ general 兜底），一键播种为 KB 条目，新用户随即可在 ``/knowledge`` 微调、在
``/api/kb/sandbox`` 试答。

设计：纯数据 + 薄逻辑，``seed_starter_pack`` 按标题去重避免重复播种；
``kb_readiness`` 复用 ``KnowledgeBaseStore.stats`` 判断是否「冷」。零额外依赖。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 起步包：每条 = 一个可直接命中的 FAQ。reply_mode=ai_guided（KB 作上下文，AI 润色）。
# 话术刻意保守、可改；目的是「立刻有东西答」，而非成品文案。
_STARTER_PACKS: Dict[str, Dict[str, Any]] = {
    "ecommerce": {
        "name": "电商客服",
        "desc": "下单/物流/退换/支付等高频问法",
        "entries": [
            {"title": "如何下单", "category": "常规咨询",
             "triggers": ["怎么买", "下单", "怎么下单", "购买"],
             "example_reply": "您可以在商品页选择规格后点击「立即购买」，按提示填写收货信息并完成支付即可。需要我帮您推荐合适的款式吗？"},
            {"title": "物流多久到货", "category": "订单查询",
             "triggers": ["多久到", "物流", "发货", "快递", "几天"],
             "example_reply": "正常下单后 24 小时内发货，国内一般 2-4 天送达，偏远地区稍久。您把订单号发我，我帮您查实时进度。"},
            {"title": "支持哪些支付方式", "category": "常规咨询",
             "triggers": ["怎么付款", "支付方式", "能用什么付"],
             "example_reply": "我们支持主流的在线支付方式，下单时按页面提示选择即可。如遇支付失败可换一种方式重试，或把截图发我帮您排查。"},
            {"title": "如何退换货", "category": "退款投诉",
             "triggers": ["退货", "换货", "退款", "不想要了"],
             "example_reply": "支持 7 天无理由退换（影响二次销售除外）。请把订单号和原因发我，我为您发起售后流程并跟进。"},
            {"title": "有没有优惠", "category": "常规咨询",
             "triggers": ["优惠", "折扣", "便宜点", "活动"],
             "example_reply": "近期有满减/新人券等活动，下单时可在结算页查看可用优惠。需要我帮您看看当前最划算的组合吗？"},
        ],
    },
    "payment": {
        "name": "支付/通道客服",
        "desc": "充值/提现/到账/汇率/通道状态",
        "entries": [
            {"title": "如何充值", "category": "常规咨询",
             "triggers": ["怎么充值", "充值", "入款", "怎么存"],
             "example_reply": "请在「充值」页选择金额与通道，按页面指引完成转账，到账后系统会自动上分。如有延迟把订单号发我帮您催。"},
            {"title": "提现多久到账", "category": "通道状态",
             "triggers": ["提现", "出款", "多久到账", "没到账"],
             "example_reply": "提现一般 5-30 分钟到账，高峰或银行维护时稍慢。超时未到请把提现单号发我，我立即帮您核查通道状态。"},
            {"title": "汇率怎么算", "category": "余额汇率",
             "triggers": ["汇率", "怎么换算", "多少钱"],
             "example_reply": "以下单时系统实时汇率为准，结算页会显示最终金额。需要我帮您按当前汇率估算一下吗？"},
            {"title": "通道维护/无法支付", "category": "通道状态",
             "triggers": ["支付不了", "通道", "维护", "失败"],
             "example_reply": "可能是该通道临时维护，请稍后重试或换一个通道。把失败截图发我，我帮您确认当前可用通道。"},
            {"title": "账户余额查询", "category": "余额汇率",
             "triggers": ["余额", "查余额", "我还有多少"],
             "example_reply": "您可在「我的-余额」查看实时余额。如显示异常，把账号发我帮您核对流水。"},
        ],
    },
    "outreach": {
        "name": "引流/转化客服",
        "desc": "首次接触/留资/引导转化常见问法",
        "entries": [
            {"title": "你们是做什么的", "category": "常规咨询",
             "triggers": ["你们是", "做什么", "干嘛的", "介绍一下"],
             "example_reply": "我们专注为您提供 [业务简介]。方便了解下您目前最关心哪方面？我按您的需求给您详细介绍。"},
            {"title": "怎么联系/加好友", "category": "常规咨询",
             "triggers": ["怎么联系", "加微信", "加好友", "联系方式"],
             "example_reply": "可以的～方便留个常用的联系方式吗？我让专属顾问第一时间和您对接，给您发详细资料。"},
            {"title": "价格/费用", "category": "常规咨询",
             "triggers": ["多少钱", "价格", "费用", "收费"],
             "example_reply": "费用会根据您的具体需求有所不同。简单说下您的情况，我给您一个更准确的方案和报价。"},
            {"title": "有没有案例/效果", "category": "常规咨询",
             "triggers": ["案例", "效果", "靠谱吗", "真的假的"],
             "example_reply": "我们有不少同类客户的成功案例，可以发您参考。您留个联系方式，我把最贴近您情况的案例整理给您。"},
            {"title": "考虑一下/再看看", "category": "常规咨询",
             "triggers": ["考虑", "再看看", "再说", "不着急"],
             "example_reply": "完全理解～您可以先留个联系方式，有最新优惠或资料我同步给您，不打扰您决定。"},
        ],
    },
    "bazi": {
        "name": "命理陪聊",
        "desc": "八字/十神/大运流年大白话 + 高危问题安全话术（companion.bazi 技能配套）",
        "entries": [
            {"title": "什么是八字/四柱", "category": "命理",
             "triggers": ["什么是八字", "八字是什么", "四柱是什么", "生辰八字是啥"],
             "example_reply": "八字就是把你出生的年、月、日、时换成天干地支，各两个字、一共八个字，也叫四柱。它像一张出生时刻的「天气快照」，我们聊运势都是从这张快照展开的～想看的话把出生年月日和大概几点告诉我就行。"},
            {"title": "十神是什么", "category": "命理",
             "triggers": ["十神", "正印偏印", "食神伤官", "正官七杀", "比肩劫财", "正财偏财"],
             "example_reply": "十神是描述其他干支跟你日主关系的十个称呼，说人话就是：印星像滋养你的人和资源，食伤是你的表达和才华，财星是你能抓住的东西，官杀是规矩和压力，比劫是同伴和竞争。别被名字吓到，聊到哪个我就给你翻译到哪个～"},
            {"title": "日主强弱是什么意思", "category": "命理",
             "triggers": ["日主强弱", "身强身弱", "身强", "身弱", "偏强偏弱"],
             "example_reply": "日主就是代表你自己的那个字，强弱说的是它在整张盘里「底气足不足」——帮衬它的字多就偏强，消耗它的字多就偏弱。强弱没有好坏之分，只是用力方向不同：偏强适合多输出多闯，偏弱适合多积累多借力。"},
            {"title": "喜用神大白话", "category": "命理",
             "triggers": ["喜用神", "喜用", "用神", "忌神"],
             "example_reply": "喜用神就是「对你命局最顺手的五行」：偏强的人适合消耗和约束（克泄耗），偏弱的人适合滋养和帮衬（生扶）。它是个参考倾向，不是护身符——落到生活里就是多做让你顺的事、少硬碰让你堵的事。"},
            {"title": "大运是什么", "category": "命理",
             "triggers": ["大运是什么", "什么是大运", "大运怎么看", "走什么运"],
             "example_reply": "大运是十年一换的人生「季节」，同一个人在不同大运里状态会很不一样。它不决定具体某件事，更像背景气候——顺的季节多播种，紧的季节稳着走。想知道你现在走哪步运，我看下你的盘就能说。"},
            {"title": "流年是什么", "category": "命理",
             "triggers": ["流年是什么", "什么是流年", "今年流年", "流年运势"],
             "example_reply": "流年就是每一年的干支给你带来的「当年天气」，跟大运叠着看：大运是季节、流年是当天天气。所以同一年不同人的感受不一样，这也是为什么我要按你的盘来聊，而不是给所有人一样的答案～"},
            {"title": "五行缺什么怎么办", "category": "命理",
             "triggers": ["五行缺", "缺金", "缺木", "缺水", "缺火", "缺土", "怎么补"],
             "example_reply": "先说结论：缺不可怕，很多好盘也缺一两样。缺的五行更像「配置偏科」，用日常小事就能平衡：比如缺水的多亲近水、作息滋润点；缺火的多晒太阳多运动。别花冤枉钱买什么开运神物，行动比物件管用得多。"},
            {"title": "时辰对照", "category": "命理",
             "triggers": ["时辰对照", "子时是几点", "什么时辰", "时辰怎么算"],
             "example_reply": "十二时辰每个占两小时：子23-1点、丑1-3、寅3-5、卯5-7、辰7-9、巳9-11、午11-13、未13-15、申15-17、酉17-19、戌19-21、亥21-23。只记得大概「早上/傍晚」也行，我按范围帮你对。"},
            {"title": "农历公历报哪个", "category": "命理",
             "triggers": ["农历还是公历", "阳历还是阴历", "报农历", "报公历"],
             "example_reply": "都可以！你说清楚是哪种就行，我这边会自动换算。排盘按节气分界（比如立春才换年），这些技术活交给我，你只管把日期和大概钟点告诉我～"},
            {"title": "算命准不准", "category": "命理",
             "triggers": ["算命准吗", "算的准吗", "准不准", "命理可信吗", "是不是迷信"],
             "example_reply": "我的看法：把它当天气预报，不当判决书。盘面说的是倾向和节奏，准不准三分在盘、七分在人怎么走。它最大的用处是帮你换个角度看自己——该做的决定还是你自己做，我陪你把利弊聊清楚。"},
            {"title": "问生死病灾怎么答", "category": "命理",
             "triggers": ["我什么时候死", "会不会死", "有没有大病", "血光之灾", "活多久"],
             "example_reply": "这个我不算也不猜——生死病灾从来不是盘面能断言的事，身体的事请一定交给医生和体检。盘上能聊的是哪段时间适合多休息、多注意节奏。你最近是不是有点担心什么？跟我说说，咱们一起想办法。"},
            {"title": "赌博彩票财运怎么答", "category": "命理",
             "triggers": ["赌运", "赌博财运", "买彩票", "偏财运能赢吗", "下注"],
             "example_reply": "偏财旺不等于赌能赢，这点我得说实话——盘面从来保不了投机的输赢，靠运下注十有九亏。真要聊财运，我们聊怎么让正财稳一点、机会来了接得住，这比赌桌实在多了。"},
            {"title": "合盘是什么", "category": "命理",
             "triggers": ["合盘", "合八字", "配不配", "我们合适吗"],
             "example_reply": "合盘就是把两个人的八字放一起看互动：五行合不合拍、节奏能不能咬合。它看的是「相处模式」不是「判死刑」——再合的盘也要经营，再冲的盘也有解法。想看的话需要对方的出生信息哦。"},
            {"title": "本命年犯太岁", "category": "命理",
             "triggers": ["本命年", "犯太岁", "太岁", "值太岁"],
             "example_reply": "本命年/犯太岁说的是流年跟你的年支撞上了，节奏容易乱一点，但绝不是「必倒霉」。老话讲究稳：这一年少冲动决定、多留余量，红色穿不穿随你开心～重要的是心态别自己吓自己。"},
            {"title": "灵签和今日能量", "category": "命理",
             "triggers": ["灵签", "今日签", "抽签是什么", "今日能量"],
             "example_reply": "每日灵签是按当天干支跟你日主的关系算的「今日能量」：有的日子适合输出表达，有的适合收敛休整。它是当天的小提示，不是任务清单——想试试就说「抽个签」，我给你翻今天的～"},
            {"title": "怎么改运", "category": "命理",
             "triggers": ["怎么改运", "改运", "转运", "开运方法"],
             "example_reply": "我不推荐花钱买转运物——真正管用的「改运」是顺着盘面调整行为：喜用是什么就多靠近什么，节奏紧的年份就稳扎稳打。命理给的是地图，路还是你自己走，这才是「天衍四九，人遁其一」的意思。"},
        ],
    },
    "general": {
        "name": "通用客服",
        "desc": "问候/转人工/营业时间等通用兜底",
        "entries": [
            {"title": "问候", "category": "常规咨询",
             "triggers": ["你好", "在吗", "hi", "hello"],
             "example_reply": "您好～很高兴为您服务，请问有什么可以帮您？"},
            {"title": "转人工", "category": "常规咨询",
             "triggers": ["转人工", "人工", "客服", "找人"],
             "example_reply": "好的，正在为您转接人工客服，请稍候片刻～"},
            {"title": "营业时间", "category": "常规咨询",
             "triggers": ["营业时间", "几点", "上班", "在线时间"],
             "example_reply": "我们的服务时间为 [请填写营业时间]，其余时间留言也会尽快回复您。"},
            {"title": "感谢/再见", "category": "常规咨询",
             "triggers": ["谢谢", "再见", "拜拜", "感谢"],
             "example_reply": "不客气～有任何问题随时找我，祝您生活愉快！"},
        ],
    },
}

_COLD_THRESHOLD = 5  # 业务条目数 < 此值视为「冷启动」


def list_starter_packs() -> List[Dict[str, Any]]:
    """所有起步包概览（id/名称/描述/条目数），供向导渲染选择。"""
    return [
        {"id": pid, "name": p["name"], "desc": p["desc"],
         "count": len(p["entries"])}
        for pid, p in _STARTER_PACKS.items()
    ]


def get_starter_pack(domain: str) -> Dict[str, Any]:
    """取某域起步包；未知域回落 general。"""
    return _STARTER_PACKS.get(str(domain or "").lower()) or _STARTER_PACKS["general"]


def kb_readiness(kb_store) -> Dict[str, Any]:
    """KB 冷启动现状：条目/分类数 + 是否「冷」。kb 不可用时返回 available=False。"""
    if kb_store is None:
        return {"available": False, "is_cold": True, "total_entries": 0,
                "enabled_entries": 0, "category_count": 0}
    try:
        st = kb_store.stats() or {}
    except Exception:
        return {"available": False, "is_cold": True, "total_entries": 0,
                "enabled_entries": 0, "category_count": 0}
    total = int(st.get("total_entries", 0) or 0)
    enabled = int(st.get("enabled_entries", 0) or 0)
    cats = st.get("categories") or st.get("category_stats") or {}
    cat_count = len(cats) if isinstance(cats, (dict, list)) else 0
    return {
        "available": True,
        "total_entries": total,
        "enabled_entries": enabled,
        "category_count": cat_count,
        "is_cold": enabled < _COLD_THRESHOLD,
        "cold_threshold": _COLD_THRESHOLD,
    }


def _existing_titles(kb_store) -> set:
    try:
        return {str(e.get("title") or "").strip() for e in kb_store.list_entries()}
    except Exception:
        return set()


def seed_starter_pack(
    kb_store, domain: str, *, dedup: bool = True,
) -> Tuple[int, int, List[str]]:
    """把某域起步包播种进 KB。

    - ``dedup=True`` 时跳过标题已存在的条目（可重复点不会灌重复）；
    - 每条标 ``source=\"starter:<domain>\"`` 便于日后识别/清理；
    - reply_mode=ai_guided。
    返回 ``(added, skipped, added_titles)``。kb 不可用时抛 RuntimeError。
    """
    if kb_store is None:
        raise RuntimeError("KB store 不可用")
    pack = get_starter_pack(domain)
    existing = _existing_titles(kb_store) if dedup else set()
    added = skipped = 0
    added_titles: List[str] = []
    for e in pack["entries"]:
        title = str(e.get("title") or "").strip()
        if dedup and title in existing:
            skipped += 1
            continue
        data = {
            "title": title,
            "category": e.get("category") or "常规咨询",
            "triggers": list(e.get("triggers") or []),
            # KB 落库字段是 example_reply_zh（add_entry 只读这个键）
            "example_reply_zh": e.get("example_reply") or "",
            "reply_mode": "ai_guided",
            "enabled": 1,
        }
        try:
            kb_store.add_entry(data)
            added += 1
            added_titles.append(title)
            existing.add(title)
        except Exception:
            skipped += 1
    return added, skipped, added_titles
