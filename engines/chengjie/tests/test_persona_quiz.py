# -*- coding: utf-8 -*-
"""人设一致性考题（E 线）门禁——全部离线，fake chat_fn，绝不真调 LLM。

覆盖四层：
1. 出题 —— ``build_quiz``（富档案 ≥6 题 / 确定性 / expect 全部来自档案原文 /
   实体真的被挖掉 / 瘦档案 []）。
2. 判分 —— ``score_answers``（CJK 子串 / 拉丁大小写不敏感（含重音）/ 未命中 /
   空回答与缺位 / 空卷不炸）。
3. 编排 —— ``run_quiz``（全对=100 / 一半答错 items 对位 / 单题异常记 fail 不炸 /
   on_stage 逐题汇报 / system=真实人设装配 prompt + 附加行）。
4. 路由 —— TestClient（仿 test_persona_doc_import.py）：status 探测 / flag 关 403 /
   persona 404 / 素材不足 400 / 注入 ``app.state.persona_doc_chat_fn`` 假函数 →
   POST → 轮询 jobs 到 done → score 正确 / n 夹取 / job 404 / 满载 429。

任务注册表：D1 的通用入口 ``persona_doc_import.create_job_runner`` 若已落地则用
真实现；尚未落地则由 ``job_runner`` fixture 装**同契约**本地假实现
（``create_job_runner(runner) -> job_id|None``，``runner(on_stage) -> result``，
落真 ``_JOBS`` 表 → ``get_job`` 轮询原样工作）——集成负责人以真实现终验。
"""

import asyncio
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils import persona_doc_import as pdi
from src.utils import persona_import_stats as pis
from src.utils import persona_quiz as pq
from src.utils import persona_quiz_store as pqs

# ── 富档案 fixture（Mizuki 风格：专名/年份/职业记忆 + background + tastes + role/age）──

_RICH_PERSONA = {
    "id": "mizuki_test",
    "name": "美月",
    "role": "在西班牙巴塞罗那的W Barcelona酒店运营总监助理，32岁",
    "age": 32,
    "gender": "female",
    "background": (
        "美月1992年9月13日出生于西班牙潘普洛纳。"
        "2010年考入都灵大学，主修酒店管理与旅游，2014年毕业。"
    ),
    "personality": {"traits": ["开朗爱笑", "善良"], "style": "真诚直接"},
    "context": {
        "hobbies": ["看书", "旅游", "游泳"],
        "specific_memories": [
            "父亲José Leandro Navarro是建筑师，2019年5月因肺癌去世，享年68岁。",
            "母亲Misaki Fujimura在潘普洛纳开设寿司外卖店Sushi Ya。",
            "姐姐Lucía Yukiko Navarro是米兰跨国企业数据分析师，已婚有一子。",
            "姑姑María Joséfa Navarro是巴塞罗那W酒店运营总监，58岁。",
            "2016年10月在米兰意大利原野贸易公司任国际贸易专员。",
            "出生于1992年9月13日西班牙Navarra地区Pamplona私人医院。",
        ],
    },
    "tastes": {"likes": ["抹茶", "海边散步", "Barolo红酒"]},
}

_THIN_PERSONA = {"id": "thin_one", "name": "小薄"}

# ── 真机人设快照（config/profiles_runtime.yaml 逐字摘录，只留出题相关字段）────
# 前两个是 K2 的覆盖率回归钉：出题守卫上线后它们的可出题数跌破 MIN_QUIZ_ITEMS，
# 被整个跳过（连「档案 22 岁 / AI 答 20 岁」这种真 bug 都测不到了）。
_REAL_STUDENT = {                      # 林小雨：非在职 + 英文外貌 + 无 context
    "id": "lin_xiaoyu", "name": "林小雨",
    "role": "大学生 / 生活博主", "age": 22, "gender": "female",
    "appearance": ("a 22-year-old East Asian college girl, long straight dark "
                   "brown hair, bright playful smile, slim figure"),
    "background": ("日语系大三在读，半工半读，在便利店打工攒旅费。 热爱 K-pop、"
                   "抹茶甜点和日系穿搭，IG 上有 2000 粉丝的日常博主。 独自住宿舍，"
                   "喜欢和陌生人聊天，觉得每段对话都是新故事。"),
    "tastes": {"likes": ["抹茶", "K-pop", "日系穿搭", "猫", "深夜便利店的关东煮"],
               "dislikes": ["香菜", "早八课", "被已读不回", "男生油腻的搭讪"]},
    "boundaries": {"topics_to_avoid": ["政治", "宗教极端", "成人内容"]},
}
_REAL_RETIRED = {                      # 赵老师：退休身份 + 亲属句陷阱（帮儿子带孙子）
    "id": "zhao_laoshi", "name": "赵老师", "role": "退休高中数学老师",
    "age": 58, "gender": "male",
    "appearance": ("a 58-year-old East Asian man, short grey-streaked hair, "
                   "reading glasses, gentle grandfatherly demeanor"),
    "background": ("执教 30 年高中数学，带出过省市状元，桃李遍全国。 退休后帮儿子"
                   "带孙子，顺带养花、钓鱼、写字。 最喜欢的事是跟年轻人聊天，"
                   "觉得“教学相长”，什么年纪都在学习。 偶尔唠叨，但每句话都是真心话。"),
    "tastes": {"likes": ["下棋", "钓鱼", "写毛笔字", "养花", "跟年轻人聊天"],
               "dislikes": ["浪费", "年轻人熬夜", "不懂装懂", "急功近利"]},
}
_REAL_TRAPPED = {                      # 陈默：档案很厚但结构化槽位少（K2d 缺口）
    "id": "chen_mo", "name": "陈默（阿默）", "age": 27, "gender": "male",
    "role": "困在马尼拉帕赛封闭园区的湖南年轻人，白天装睡、夜里被逼着上班",
    "names": {"nickname": "阿默",
              "usage_notes": "本名陈默，熟人叫阿默。"},
    "background": ("湖南人，27岁。曾经拼了命创业，最后一地鸡毛，背上大约45万的债；"
                   "父亲因此病倒瘫痪，妹妹还在读大学。"),
    "tastes": {"likes": ["红万宝路", "芙蓉王", "槟榔", "湖南口味的便宜泡面", "听雨"],
               "dislikes": ["假惺惺的可怜", "被逼着演戏", "狗庄画大饼"]},
    "context": {"hobbies": ["抽烟发呆", "听雨"],
                "specific_memories": ["护照被收走那天，铁门在身后关上的声音"]},
}
_REAL_NOMAD = {                        # 晴子：自由职业 role（旧词表全不命中）
    "id": "haruko_traveler", "name": "晴子", "age": 28, "gender": "female",
    "role": "旅行博主 / 自由插画师",
    "background": ("放弃了外企 offer 独自背包走了东南亚、日本、中亚，"
                   "途中开始用插画记录沿途故事。"),
    "tastes": {"likes": ["清晨的光", "手冲咖啡", "旧书店", "慢节奏"],
               "dislikes": ["赶行程", "网红打卡", "喧闹的团"]},
}
_REAL_FAMILY = {                       # 林佳欣：names / context.family / 在地文化
    "id": "lin_jiaxin", "name": "林佳欣", "age": 36,
    "role": "注册护士 / 线上艺术品竞拍爱好者，36岁，加拿大温哥华，频繁往返香港",
    "names": {"english": "Fiona", "french": "Claire",
              "full_western": "Fiona Claire Lin"},
    "tastes": {"likes": ["海边", "瑜伽", "心理学的书", "妈妈做的菲律宾菜"]},
    "context": {
        "family": {
            "father": "林志强（Michael Lin），加拿大华裔IT高管，沉稳理性，是佳欣的精神支柱",
            "mother": "陈慧琳（Elena Lin），香港/菲律宾混血艺术品策展人，热情浪漫",
            "brother": "林景然（Jason Lin），佳士得拍卖公司香港市场经理，佳欣的艺术启蒙人",
            "daughter": "林佳悦（Jade），小学高年级，活泼好奇，书桌上贴着一幅全家福",
        },
        "filipino_connection": {
            "language": "偶尔用 Tagalog 表达惊叹（Nako！/ Grabe！），和妈妈家族通话后尤其",
            "food": "妈妈的拿手菜是 Adobo 和 Sinigang，她会做但做得没妈妈好",
            "values": "菲律宾家族文化里的 bayanihan（互助精神）深植在她的护士职业观里",
        },
    },
}


@pytest.fixture(autouse=True)
def _clean_jobs(tmp_path):
    pdi.reset_jobs()
    pis.reset_for_test()
    pqs.reset()
    pqs.configure(str(tmp_path / "quiz_reports.db"))
    yield
    pdi.reset_jobs()
    pis.reset_for_test()
    pqs.reset()


# ── 1. build_quiz ────────────────────────────────────────────────────────────

def test_build_quiz_rich_profile_shape_and_provenance():
    quiz = pq.build_quiz(_RICH_PERSONA, n=10)
    assert len(quiz) >= 6
    assert len(quiz) <= 10
    corpus = json.dumps(_RICH_PERSONA, ensure_ascii=False)
    srcs = set()
    for item in quiz:
        assert item["q"].strip()
        assert 1 <= len(item["expect"]) <= 3
        assert item["src"] in ("memory", "background", "tastes", "role", "age")
        srcs.add(item["src"])
        for kw in item["expect"]:
            # 判分靠子串命中 → expect 关键词必须逐字来自档案原文
            assert kw in corpus, f"expect 关键词必须来自档案原文: {kw!r}"
    assert {"memory", "role", "age", "tastes"} <= srcs
    # 题目文本唯一（去重生效）
    qs = [it["q"] for it in quiz]
    assert len(qs) == len(set(qs))


def test_build_quiz_deterministic_same_seed():
    assert pq.build_quiz(_RICH_PERSONA, n=10) == pq.build_quiz(_RICH_PERSONA, n=10)
    assert (pq.build_quiz(_RICH_PERSONA, n=10, seed=42)
            == pq.build_quiz(_RICH_PERSONA, n=10, seed=42))


def test_build_quiz_father_memory_entity_dug_out():
    """规格样例：「父亲José Leandro Navarro是建筑师…」→ 名字+职业被挖掉变问题。"""
    quiz = pq.build_quiz(_RICH_PERSONA, n=20)   # n 放大到全量，防洗牌抽不到
    father = [it for it in quiz if it["q"].startswith("你父亲")]
    assert father, "富档案应出「你父亲…」题"
    item = father[0]
    assert item["q"] == "你父亲叫什么名字？他是做什么的？"
    assert item["expect"] == ["José", "建筑师"]
    assert item["src"] == "memory"
    # 实体已挖掉：问题文本不得泄漏答案
    assert "José" not in item["q"] and "建筑师" not in item["q"]


def test_build_quiz_covers_year_birthplace_and_fixed_slots():
    quiz = pq.build_quiz(_RICH_PERSONA, n=20)
    by_q = {it["q"]: it for it in quiz}
    assert by_q["你今年多大？"]["expect"] == ["32"]
    assert by_q["你是在哪里出生的？"]["expect"] == ["Navarra", "Pamplona"]
    assert by_q["你喜欢喝什么酒？"]["expect"] == ["Barolo"]
    role_item = by_q["你现在在哪里工作？做什么职位？"]
    assert "Barcelona" in role_item["expect"]
    # 年份题：年份从题面挖掉、进 expect
    year_items = [it for it in quiz if "哪一年" in it["q"]]
    assert year_items
    for it in year_items:
        assert it["expect"][0] not in it["q"]
        assert it["expect"][0].isdigit()


def test_question_from_text_kinship_guard_no_first_person_misattribution():
    """真机事故回归（2026-07-27 Mizuki 冒烟）：亲属的学校/出生地不得出成第一人称题。

    「2004年姐姐考入米兰理工大学」曾生成「你读的大学叫什么名字？」（expect=米兰理工
    大学），而档案主人读的是都灵大学 → AI 答对反被判错。含亲属词的记忆只允许
    年份引用题（引文自带归属）。"""
    it = pq._question_from_text("2004年姐姐考入米兰理工大学，姐妹第一次分离。", "memory")
    assert it is not None
    assert it["q"].startswith("你提到过")
    assert it["expect"] == ["2004"]
    assert "你读的大学" not in it["q"]
    # 无年份可引用 → 宁缺勿错，直接不出题
    assert pq._question_from_text("姐姐考入米兰理工大学。", "memory") is None
    assert pq._question_from_text("姐姐出生在Osaka郊区。", "memory") is None
    # 本人的学校/出生地不受守卫影响
    it2 = pq._question_from_text("考入都灵大学，主修酒店管理。", "memory")
    assert it2 is not None and it2["expect"] == ["都灵大学"]


def test_school_stem_trims_passive_verb_prefix():
    """真机回归（Mizuki 冒烟第 2 轮）：「被都灵大学录取」曾把期望词抽成
    「被都灵大学」——AI 答「都灵大学」子串不中被误判。词干清洗须剥「被/经」。"""
    it = pq._question_from_text("2010年高考失利，被都灵大学录取，专业酒店管理。", "memory")
    assert it is not None
    assert it["q"] == "你读的大学叫什么名字？"
    assert it["expect"] == ["都灵大学"]


def test_build_quiz_tastes_open_question_replaced_by_pointed_wine():
    """开放题「你平时喜欢做什么？」经两轮真机证伪（AI 换措辞聊同样真实的爱好，
    「素食主义者」→「做素食料理」，子串判分必然误伤）→ 已砍掉；tastes 只保留
    可逐字验证的尖问（带拉丁品牌词的酒类条目）。"""
    quiz = pq.build_quiz(_RICH_PERSONA, n=20)
    qs = [it["q"] for it in quiz]
    assert "你平时喜欢做什么？" not in qs
    wine = {it["q"]: it for it in quiz}["你喜欢喝什么酒？"]
    assert wine["expect"] == ["Barolo"] and wine["src"] == "tastes"
    # 品牌词跨措辞稳定：换说法照样命中
    assert pq.score_answers([wine], ["最爱来一杯Barolo配奶酪"])["passed"] == 1
    # 没有酒类拉丁条目的档案 → 不出 tastes 题（宁缺勿错）
    p2 = dict(_RICH_PERSONA)
    p2["tastes"] = {"likes": ["抹茶", "海边散步"]}
    qs2 = [it["q"] for it in pq.build_quiz(p2, n=20)]
    assert "你喜欢喝什么酒？" not in qs2 and "你平时喜欢做什么？" not in qs2


# ── 1b. 出题守卫（2026-07-28 夜间回归事故：4 人设 3 类坏题）────────────────

def test_year_guard_quantity_four_digits_never_becomes_year_question():
    """事故 A：「IG 上有 2000 粉丝」被当年份出题，题干还被挖成「某年粉丝」。

    4 位数要当年份必须过三重闸：合理年份区间 + 无紧邻数量词 + 正面年份特征。
    """
    bad = "热爱K-pop、抹茶甜点和日系穿搭，IG 上有 2000 粉丝的日常博主。"
    assert pq._question_from_text(bad, "memory") is None
    # 同句里数量 4 位数不得被挖空（题干必须读得通）
    assert pq._year_spans(bad) == []
    for text in ("月薪 8000 元的运营。", "跑了 1500 公里。", "攒了 2000 多粉丝。",
                 "一次充值 1999 元。", "班里有 1200 人。"):
        assert pq._year_spans(text) == [], text
        assert pq._question_from_text(text, "memory") is None, text


def test_year_guard_real_years_still_pass():
    """反例面：真年份照出题（守卫不能把好题一起杀了）。"""
    it = pq._question_from_text("2015年离职，开始自由接稿。", "memory")
    assert it is not None
    assert it["expect"] == ["2015"] and "哪一年" in it["q"]
    assert "2015" not in it["q"] and "某年" in it["q"]
    # 「在1998」「2015-06」两类年份特征
    assert [s[2] for s in pq._year_spans("在1998，我们搬到了大阪。")] == ["1998"]
    assert [s[2] for s in pq._year_spans("2015-06 入职。")] == ["2015"]
    # 区间外（未来年/太古早）不算年份
    assert pq._year_spans("预计3000年殖民火星。") == []
    assert pq._year_spans("1600年的古董。") == []


def test_year_guard_mixed_sentence_masks_only_the_real_year():
    """混合句：年份挖空、数量原样保留。"""
    it = pq._question_from_text("2018年开始做博主，现在有 2000 粉丝。", "memory")
    assert it is not None and it["expect"] == ["2018"]
    assert "某年开始做博主" in it["q"]
    assert "2000 粉丝" in it["q"]      # 数量没被误挖


def test_name_slot_rejects_verb_phrase_expect():
    """事故 B：「你儿子叫什么名字？」expect=「带孙子」——AI 答对反判 0 分。

    名字抽取失败时要么退化成职业题，要么整题丢弃，绝不硬凑期望答案。
    """
    it = pq._question_from_text("退休后帮儿子带孙子，日子过得平淡。", "memory")
    assert it is None, f"不该出题，实际出了 {it!r}"
    # 抽得到真名字 → 正常出题
    ok = pq._question_from_text("我那儿子赵明远，在科技公司当程序员。", "memory")
    assert ok is not None
    assert "赵明远" in ok["expect"]
    assert ok["q"].startswith("你儿子叫什么名字？")
    # 名字不可信但职业可信 → 退化成职业题（不带名字槽）
    deg = pq._question_from_text("儿子帮我照看店铺，他是程序员。", "memory")
    assert deg is None or ("叫什么名字" not in deg["q"] and deg["expect"] == ["程序员"])


def test_looks_like_person_name_pure_function():
    for good in ("赵明远", "李娜", "美月", "José", "Navarro"):
        assert pq._looks_like_person_name(good), good
    for bad in ("带孙子", "孙女", "在东京", "很开心", "的儿子", "王医生", "",
                "一", "考入都灵大学", "j"):
        assert not pq._looks_like_person_name(bad), bad


def test_item_slot_ok_rejects_mismatch_and_answer_leak():
    assert pq._item_slot_ok({"q": "你儿子叫什么名字？", "expect": ["赵明远"]})
    assert not pq._item_slot_ok({"q": "你儿子叫什么名字？", "expect": ["带孙子"]})
    # 亲属职业槽必须是词表职业
    assert not pq._item_slot_ok({"q": "你父亲是做什么的？", "expect": ["去世了"]})
    # 题干泄漏答案
    assert not pq._item_slot_ok({"q": "你2015年在做什么？", "expect": ["2015"]})
    # 年份槽必须是合法年份
    assert not pq._item_slot_ok({"q": "这件事发生在哪一年？", "expect": ["2000粉丝"]})
    assert not pq._item_slot_ok({"q": "这件事发生在哪一年？", "expect": ["3000"]})
    assert not pq._item_slot_ok(None) and not pq._item_slot_ok({"q": "x", "expect": []})


def test_role_student_no_workplace_question():
    """事故 C：`role=学生` 却问「你现在在哪里工作？做什么职位？」，
    AI 答「在便利店打工」（学生兼职完全合理）被判 0 分。"""
    student = {
        "id": "lin_xiaoyu", "name": "林小雨", "age": 20,
        "role": "在东京读设计的留学生，20岁",
        "context": {"specific_memories": [
            "父亲林建国是工程师。",
            "母亲在大阪开设居酒屋Yamato。",
            "2015年全家搬到东京。",
        ]},
    }
    qs = [it["q"] for it in pq.build_quiz(student, n=20)]
    assert "你现在在哪里工作？做什么职位？" not in qs
    assert qs, "非在职身份只砍工作题，其余素材照常出题"
    # 有校名 → 改问学校（可逐字验证）
    with_school = dict(student, role="早稻田大学在读研究生")
    qs2 = {it["q"]: it for it in pq.build_quiz(with_school, n=20)}
    assert "你现在在哪里工作？做什么职位？" not in qs2
    assert qs2["你在哪所学校念书？"]["expect"] == ["早稻田大学"]


def test_role_non_working_table_is_conservative():
    for role in ("大学生", "在东京读设计的留学生", "已退休的中学老师", "退休老师",
                 "无业，靠积蓄生活", "全职妈妈", "在读研究生"):
        assert pq._role_is_non_working(role), role
    for role in ("学生公寓管理员", "退休金核算专员", "酒店运营总监助理",
                 "咖啡店主", "自由摄影师", ""):
        assert not pq._role_is_non_working(role), role


# ── 1c. 出题范围 × prompt 数据链注册表（K2，2026-07-28）─────────────────────

def test_field_consumed_subtree_and_degrade():
    """纯函数：子树登记覆盖子键 / 未登记字段拒绝 / 空字段拒绝 / 无注册表放行。"""
    allowed = frozenset({"age", "context.family", "tastes.likes"})
    assert pq._field_consumed("age", allowed)
    assert pq._field_consumed("tastes.likes", allowed)
    assert pq._field_consumed("context.family", allowed)
    assert pq._field_consumed("context.family.father", allowed)   # 子树
    assert not pq._field_consumed("tastes.dislikes", allowed)
    assert not pq._field_consumed("location", allowed)
    assert not pq._field_consumed("", allowed)                    # 漏填 field 不蒙混
    assert not pq._field_consumed(None, allowed)
    # 注册表不可用（老版本 persona_manager）→ 降级为不校验
    assert pq._field_consumed("whatever.path", None)
    assert pq._field_consumed("", None)


def test_quiz_only_asks_fields_that_reach_the_prompt():
    """出题范围 ⊆ ``PROMPT_CONSUMED_FIELDS``（K1 注册表）——真常量对真题目。

    没进 prompt 的字段 AI 根本没被告知，考它必然是假阴性；反过来，进了 prompt 的
    字段答错就是真 bug。两套系统的耦合点就是这一条断言。
    """
    from src.utils.persona_manager import PROMPT_CONSUMED_FIELDS

    for persona in (_RICH_PERSONA, _REAL_STUDENT, _REAL_RETIRED, _REAL_FAMILY):
        quiz = pq.build_quiz(persona, n=20)
        assert quiz, persona.get("id")
        for it in quiz:
            assert it.get("field"), f"每题必须记字段来源: {it['q']}"
            assert pq._field_consumed(it["field"], frozenset(PROMPT_CONSUMED_FIELDS)), \
                f"{it['field']} 没进 prompt，考它必是假阴性: {it['q']}"


def test_build_quiz_drops_items_whose_field_is_not_consumed(monkeypatch):
    """注册表收窄 → 依赖被剔字段的题目当场消失，其余不受影响。"""
    from src.utils import persona_manager as pm

    full = pq.build_quiz(_REAL_FAMILY, n=20)
    assert "tastes.likes" in {it["field"] for it in full}

    narrowed = frozenset(f for f in pm.PROMPT_CONSUMED_FIELDS
                         if not f.startswith("tastes."))
    monkeypatch.setattr(pm, "PROMPT_CONSUMED_FIELDS", narrowed)
    quiz = pq.build_quiz(_REAL_FAMILY, n=20)
    assert quiz, "剔掉 tastes 后仍有其它题源"
    assert not [it for it in quiz if it["field"].startswith("tastes.")]
    assert {it["q"] for it in quiz} < {it["q"] for it in full}


def test_build_quiz_degrades_gracefully_without_registry(monkeypatch):
    """老版本 persona_manager 没有该常量 → 降级为不校验，绝不把出题器搞崩。"""
    from src.utils import persona_manager as pm

    monkeypatch.delattr(pm, "PROMPT_CONSUMED_FIELDS", raising=False)
    assert pq._consumed_fields() is None
    quiz = pq.build_quiz(_REAL_STUDENT, n=20)
    assert len(quiz) >= pq.MIN_QUIZ_ITEMS      # 照常出题
    # 空表同样按「不可用」处理（别把所有题都过滤光）
    monkeypatch.setattr(pm, "PROMPT_CONSUMED_FIELDS", frozenset(), raising=False)
    assert pq._consumed_fields() is None
    assert len(pq.build_quiz(_REAL_STUDENT, n=20)) >= pq.MIN_QUIZ_ITEMS


def test_split_by_consumed_reports_reason():
    items = [{"q": "a", "expect": ["x"], "field": "age"},
             {"q": "b", "expect": ["x"], "field": "location"},
             {"q": "c", "expect": ["x"]}]
    kept, dropped = pq._split_by_consumed(items, frozenset({"age"}))
    assert [it["q"] for it in kept] == ["a"]
    assert [(it["q"], reason) for it, reason in dropped] == [
        ("b", "field_not_in_prompt:location"), ("c", "field_missing")]


# ── 1d. K2 扩题源：每类新题型正例 + 「资料不合格就不出」反例 ─────────────────

def test_food_noun_pure_function():
    """吃喝名词抽取：白名单尾词 + 剥定语 + 反活动/反比喻。"""
    cases = {
        "抹茶": "抹茶",
        "香菜": "香菜",
        "深夜便利店的关东煮": "关东煮",      # 定语剥掉
        "湖南口味的便宜泡面": "泡面",        # 尾词 ≥2 字 → 去掉修饰语才不假阴性
        "布根地红酒": "红酒",
        "黑咖啡": "咖啡",
        "妈妈做的菲律宾菜": "菲律宾菜",
    }
    for raw, want in cases.items():
        assert pq._food_noun(raw) == want, raw
    for bad in ("钓鱼", "养花", "做饭", "K-pop", "日系穿搭", "早八课",
                "狗庄画大饼",           # 以食物字收尾的比喻，不是吃的
                "被已读不回", "男生油腻的搭讪", "深夜有人真心回话", "听雨", ""):
        assert pq._food_noun(bad) == "", bad


def test_taste_food_questions_positive_and_negative():
    base = {"id": "t", "name": "小食", "age": 22, "background": "日语系大三在读。"}
    quiz = {it["q"]: it for it in pq.build_quiz(
        dict(base, tastes={"likes": ["抹茶", "K-pop", "深夜便利店的关东煮"],
                           "dislikes": ["香菜", "早八课"]}), n=20)}
    likes = quiz["你平时爱吃什么、爱喝什么？"]
    assert likes["expect"] == ["抹茶", "关东煮"] and likes["field"] == "tastes.likes"
    dislikes = quiz["有什么东西是你特别不爱吃的？"]
    assert dislikes["expect"] == ["香菜"] and dislikes["field"] == "tastes.dislikes"
    # 反例：likes/dislikes 里没有吃喝 → 不出这两道题（宁缺勿滥）
    qs = [it["q"] for it in pq.build_quiz(
        dict(base, tastes={"likes": ["下棋", "钓鱼", "写毛笔字"],
                           "dislikes": ["浪费", "不懂装懂"]}), n=20)]
    assert "你平时爱吃什么、爱喝什么？" not in qs
    assert "有什么东西是你特别不爱吃的？" not in qs


def test_major_question_positive_and_negative():
    assert pq._major_name("日语系大三在读，半工半读") == "日语"
    assert pq._major_name("2010年考入都灵大学，主修酒店管理与旅游") == "酒店管理"
    assert pq._major_name("专业是临床医学，毕业后进了三甲") == "临床医学"
    # 反例：没有学业语境的「系」/ 系统系列 → 不当专业
    assert pq._major_name("跟她没关系，我只是路过") == ""
    assert pq._major_name("这套系统很稳定") == ""
    assert pq._major_name("在读一本很厚的书") == ""
    # 亲属句不出第一人称专业题（与既有 has_kin 守卫同口径）
    kin = {"id": "k", "name": "阿测", "age": 30,
           "background": "姐姐主修法律，在米兰工作。我在便利店打工。"}
    assert "你读的是什么专业？" not in [it["q"] for it in pq.build_quiz(kin, n=20)]


def test_years_question_positive_and_negative():
    it = pq._years_item("执教 30 年高中数学，带出过省市状元", "background", "background")
    assert it["q"] == "你教了多少年书？" and it["expect"] == ["30"]
    it2 = pq._years_item("10 年品牌营销经验，带过百人团队", "background", "background")
    assert it2["q"] == "你在这一行做了多少年了？" and it2["expect"] == ["10"]
    # 判分认中文数字（AI 答「教了三十年」= 对）
    assert pq.score_answers([it], ["我教了三十年书咯"])["passed"] == 1
    # 反例：中文数字原文不抽（宁缺勿错）/ 年数越界
    assert pq._years_item("做过四年外贸跟单", "background", "background") is None
    assert pq._years_item("执教 99 年", "background", "background") is None
    assert pq._years_item("在这儿住了 3 年", "background", "background") is None


def test_subject_and_retired_role_questions():
    persona = dict(_REAL_RETIRED)
    by_q = {it["q"]: it for it in pq.build_quiz(persona, n=20)}
    assert by_q["你教的是什么科目？"]["expect"] == ["数学"]
    retired = by_q["你退休前是做什么的？"]
    assert retired["expect"] == ["老师", "数学"] and retired["field"] == "role"
    assert "你现在在哪里工作？做什么职位？" not in by_q     # 退休不问在职
    # 反例：非教师退休身份 → 不出科目题；「历史悠久」不是任教科目
    assert pq._subject_name("退休前是国企出纳") == ""
    assert pq._subject_name("住在历史悠久的老城区") == ""
    assert pq._subject_name("退休高中数学老师") == "数学"


def test_nickname_question_positive_and_negative():
    """K2d：昵称题（称呼答错＝当场穿帮）。只放行专名形，短语/单字/与本名同一律不出。"""
    # 基线自带 3 题（role/age/tastes）→ 反例分支仍出得来卷，断言「没有昵称题」非空转
    base = {"id": "n", "name": "陈默", "age": 27, "role": "咖啡店主",
            "background": "湖南人。", "tastes": {"likes": ["泡面", "红茶"]}}
    by_q = {it["q"]: it for it in pq.build_quiz(
        dict(base, names={"nickname": "阿默"}), n=20)}
    assert by_q["别人一般怎么称呼你？"]["expect"] == ["阿默"]
    assert by_q["别人一般怎么称呼你？"]["field"] == "names.nickname"
    # 拉丁昵称同样放行
    by_q2 = {it["q"]: it for it in pq.build_quiz(
        dict(base, names={"nickname": "Momo"}), n=20)}
    assert by_q2["别人一般怎么称呼你？"]["expect"] == ["Momo"]
    for bad in ({}, {"nickname": ""}, {"nickname": "默"},          # 单字子串遍地命中
                {"nickname": "陈默"},                              # 与本名相同=无信息
                {"nickname": "熟一点的人叫我阿默"},                 # 抽取残渣
                {"nickname": "小名的阿默"}):                        # 含虚词
        qs = [it["q"] for it in pq.build_quiz(dict(base, names=bad), n=20)]
        assert qs and "别人一般怎么称呼你？" not in qs, bad


def test_modern_occupations_cover_freelance_roles():
    """K2d 真机：`role=旅行博主 / 自由插画师` 两词都不在词表 → role 题出不来，
    该人设总题量跌破 MIN 被整体跳过（等于零监控）。词表补齐后必须出题。"""
    assert pq._occupation_hits("旅行博主 / 自由插画师", limit=2) == ["插画师", "博主"]
    persona = {"id": "haruko", "name": "晴子", "age": 28,
               "role": "旅行博主 / 自由插画师",
               "tastes": {"likes": ["手冲咖啡", "旧书店"]}}
    items = pq.build_quiz(persona, n=20)
    assert len(items) >= pq.MIN_QUIZ_ITEMS, "补词表后该档案不应再被整体跳过"
    by_q = {it["q"]: it for it in items}
    assert set(by_q["你现在在哪里工作？做什么职位？"]["expect"]) == {"插画师", "博主"}
    # 词表仍保持保守：动作型泛词不收（收了会在无关句子里乱命中）
    for word in ("运营", "策划", "护理", "管理"):
        assert word not in pq._OCCUPATIONS, word


def test_english_name_question_positive_and_negative():
    base = {"id": "n", "name": "林佳欣", "age": 36, "role": "注册护士",
            "background": "10 年护理经验。"}
    by_q = {it["q"]: it for it in pq.build_quiz(
        dict(base, names={"english": "Fiona"}), n=20)}
    assert by_q["你的英文名叫什么？"]["expect"] == ["Fiona"]
    assert by_q["你的英文名叫什么？"]["field"] == "names.english"
    for bad in ({}, {"english": ""}, {"english": "无"}, {"english": "n/a"}):
        qs = [it["q"] for it in pq.build_quiz(dict(base, names=bad), n=20)]
        assert "你的英文名叫什么？" not in qs, bad


def test_appearance_question_only_cross_language_stable_features():
    """外貌题只取跨语言稳定的特征（CJK 发色 / 身高数字）。

    本机人设的 appearance 全是**英文生图锚点**（"long straight dark brown hair"），
    中文提问配英文 expect＝必然假阴性 → 这类档案一道外貌题都不出。
    """
    base = {"id": "a", "name": "小外", "age": 22,
            "tastes": {"likes": ["抹茶"], "dislikes": ["香菜"]}}
    by_q = {it["q"]: it for it in pq.build_quiz(
        dict(base, appearance="二十出头的女生，深棕色的长直发，身高165cm"), n=20)}
    # 「深棕色」取基础色「棕色」：子串更宽，答「深棕色」同样命中
    assert by_q["你头发是什么颜色的？"]["expect"] == ["棕色"]
    assert by_q["你头发是什么颜色的？"]["field"] == "appearance"
    assert by_q["你有多高？"]["expect"] == ["165"]
    assert pq.score_answers([by_q["你头发是什么颜色的？"]],
                            ["深棕色的长直发呀"])["passed"] == 1
    # 反例：英文外貌 → 不出外貌题
    qs = [it["q"] for it in pq.build_quiz(
        dict(base, appearance="a 22-year-old girl, long straight dark brown hair"),
        n=20)]
    assert "你头发是什么颜色的？" not in qs and "你有多高？" not in qs


def test_family_questions_unambiguous_relations_only():
    by_q = {it["q"]: it for it in pq.build_quiz(_REAL_FAMILY, n=20)}
    father = by_q["你父亲叫什么名字？"]
    assert father["expect"] == ["林志强", "Michael"]      # 中英双名都算对
    assert father["field"] == "context.family"
    assert by_q["你女儿叫什么名字？"]["expect"] == ["林佳悦", "Jade"]
    # 反例①：brother/sister 称谓有歧义（哥哥还是弟弟）→ 不出，防「我没有哥哥」假阴性
    assert "你哥哥叫什么名字？" not in by_q and "你弟弟叫什么名字？" not in by_q
    # 反例②：家人条目里抽不出可信名字 → 整题丢弃
    thin_family = {"id": "f", "name": "阿家", "age": 40, "role": "自由摄影师",
                   "background": "10 年摄影经验。",
                   "context": {"family": {"father": "很严厉，话不多",
                                          "mother": "退休后帮我带孩子"}}}
    qs = [it["q"] for it in pq.build_quiz(thin_family, n=20)]
    assert "你父亲叫什么名字？" not in qs and "你母亲叫什么名字？" not in qs


def test_culture_questions_latin_tokens_only():
    by_q = {it["q"]: it for it in pq.build_quiz(_REAL_FAMILY, n=20)}
    assert by_q["除了中文，你还会说什么语言？"]["expect"] == ["Tagalog"]
    assert by_q["你家最拿手的家常菜是什么？"]["expect"] == ["Adobo", "Sinigang"]
    assert by_q["你家最拿手的家常菜是什么？"]["field"] == "context.filipino_connection"
    # 反例：没有拉丁专名（纯中文描述跨措辞不稳）→ 不出题
    p = dict(_REAL_FAMILY)
    p["context"] = dict(p["context"],
                        filipino_connection={"language": "偶尔说两句家乡话",
                                             "food": "妈妈拿手的是家常小炒"})
    qs = [it["q"] for it in pq.build_quiz(p, n=20)]
    assert "除了中文，你还会说什么语言？" not in qs
    assert "你家最拿手的家常菜是什么？" not in qs


def test_low_value_and_unsafe_fields_never_become_questions():
    """明确不出题的字段：gender（AI 必对，零鉴别力）、emoji_level（风格不是事实）、
    schedule（「17:00」判分器命中不了「下午五点」＝假阴性）、
    topics_to_avoid / identity（考它等于诱导 AI 说出边界内容）。"""
    persona = {
        "id": "s", "name": "小守", "age": 27, "gender": "male",
        "role": "夜班客服",
        "background": "5 年客服经验。",
        "tastes": {"likes": ["泡面"], "dislikes": ["香菜"]},
        "speaking": {"emoji_level": "low"},
        "context": {"schedule": {"work_hours": "约 17:00–05:00 上班，白天补觉",
                                 "rest_day": "周日"}},
        "boundaries": {"topics_to_avoid": ["政治", "成人内容"]},
        "identity": {"deny_ai": True, "deny_ai_reply": "我就是个普通人"},
    }
    quiz = pq.build_quiz(persona, n=20)
    assert quiz
    fields = {it["field"] for it in quiz}
    assert fields.isdisjoint({"gender", "speaking.emoji_level", "context.schedule",
                              "boundaries.topics_to_avoid", "identity.deny_ai",
                              "identity.deny_ai_reply"})
    joined = " ".join(it["q"] for it in quiz)
    for banned in ("几点", "男的", "女的", "性别", "表情", "不聊", "避免", "机器人"):
        assert banned not in joined, banned


def test_real_personas_regain_coverage_after_source_expansion():
    """覆盖率回归钉（K2 主线）：守卫上线后这两个真人设跌破 ``MIN_QUIZ_ITEMS``
    被整个跳过，「档案 22 岁 / AI 答 20 岁」那类真 bug 反而测不到了。

    扩题源之后必须重新达标——且**不许靠放宽守卫**：下面逐条题目都要能人工判为好题
    （题干问什么、expect 就是档案里那个答案）。
    """
    student = pq.build_quiz(_REAL_STUDENT, n=10)
    assert len(student) >= pq.MIN_QUIZ_ITEMS
    assert {it["q"]: it["expect"] for it in student} == {
        "你今年多大？": ["22"],
        "你平时爱吃什么、爱喝什么？": ["抹茶", "关东煮"],
        "有什么东西是你特别不爱吃的？": ["香菜"],
        "你读的是什么专业？": ["日语"],
    }
    retired = pq.build_quiz(_REAL_RETIRED, n=10)
    assert len(retired) >= pq.MIN_QUIZ_ITEMS
    assert {it["q"]: it["expect"] for it in retired} == {
        "你退休前是做什么的？": ["老师", "数学"],
        "你教的是什么科目？": ["数学"],
        "你今年多大？": ["58"],
        "你教了多少年书？": ["30"],
    }
    # 「IG 上有 2000 粉丝」仍不得出年份题（守卫没被扩题源绕过）
    assert not [it for it in student if "哪一年" in it["q"]]


def test_real_personas_zero_question_gap_closed():
    """覆盖率回归钉（K2d，2026-07-28）：这两个真人设各只出得来 2 题（差 1 题）
    → 整档跳过 = 零监控。补昵称题源 / 现代职业词表后必须达标。

    盯的是**同一类系统性缺口**：档案很厚（chen_mo 34 个字段进 prompt）却因为
    题源覆盖面窄而无题可出——这类「假性资料太薄」必须与真薄档案区分开。
    """
    trapped = pq.build_quiz(_REAL_TRAPPED, n=10)
    assert len(trapped) >= pq.MIN_QUIZ_ITEMS
    assert {it["q"]: it["expect"] for it in trapped} == {
        "你今年多大？": ["27"],
        "别人一般怎么称呼你？": ["阿默"],
        "你平时爱吃什么、爱喝什么？": ["泡面"],
    }
    nomad = pq.build_quiz(_REAL_NOMAD, n=10)
    assert len(nomad) >= pq.MIN_QUIZ_ITEMS
    assert {it["q"]: it["expect"] for it in nomad} == {
        "你现在在哪里工作？做什么职位？": ["插画师", "博主"],
        "你今年多大？": ["28"],
        "你平时爱吃什么、爱喝什么？": ["咖啡"],
    }
    # 真薄档案仍必须被判定为不值得考（本钉不是把 MIN 闸门放宽）
    assert pq.build_quiz(_THIN_PERSONA) == []


def test_build_quiz_thin_profile_returns_empty():
    assert pq.build_quiz(_THIN_PERSONA) == []
    assert pq.build_quiz({"name": "美月"}) == []
    assert pq.build_quiz({}) == []
    assert pq.build_quiz("not a dict") == []


# ── 2. score_answers ─────────────────────────────────────────────────────────

def test_score_answers_cjk_and_latin_case():
    quiz = [
        {"q": "你父亲是做什么的？", "expect": ["建筑师"], "src": "memory"},
        {"q": "你父亲叫什么名字？", "expect": ["José"], "src": "memory"},
        {"q": "你今年多大？", "expect": ["32"], "src": "age"},
        {"q": "你平时喜欢做什么？", "expect": ["看书", "旅游"], "src": "tastes"},
    ]
    answers = [
        "我爸爸是建筑师呀，怎么突然问这个",   # CJK 子串命中
        "他叫JOSÉ LEANDRO，很怀念他",          # 拉丁大小写不敏感（含重音 casefold）
        "三十五了啦",                            # 真答错（35≠32）→ 归一化后仍不命中
        "宅家看书追剧咯",                        # 任一关键词命中即 pass
    ]
    out = pq.score_answers(quiz, answers)
    assert out["total"] == 4 and out["passed"] == 3 and out["score"] == 75
    assert [it["pass"] for it in out["items"]] == [True, True, False, True]
    assert out["items"][1]["answer"] == answers[1]
    assert out["items"][3]["expect"] == ["看书", "旅游"]
    # 出题元数据透传：提案诊断卡靠 field/src 对字段，不能再被 score 丢掉
    assert [it.get("src") for it in out["items"]] == ["memory", "memory", "age", "tastes"]
    assert all("field" not in it for it in out["items"])  # 本题没带 field 就不造空键


def test_score_answers_keeps_field_and_src():
    quiz = [{
        "q": "你今年多大？", "expect": ["32"], "src": "age", "field": "age",
    }]
    out = pq.score_answers(quiz, ["我三十五了"])
    item = out["items"][0]
    assert item["pass"] is False
    assert item["field"] == "age" and item["src"] == "age"


def test_normalize_for_match_pure_function():
    n = pq.normalize_for_match
    # 中文数字 ↔ 阿拉伯（结构式 / 位串式 / 大写 / 两）
    assert n("五十八") == "58"
    assert n("六十八") == "68"
    assert n("十八岁") == "18岁"
    assert n("二十") == "20"
    assert n("一百零五") == "105"
    assert n("伍拾捌") == "58"
    assert n("二零一五年") == "2015年"
    assert n("一八年") == "18年"
    assert n("两百块") == "200块"
    # 全角 → 半角、大小写、空白折叠
    assert n("２０１５") == "2015"
    assert n("ＪＯＳÉ") == "josé"
    assert n(" 建筑  师 ") == "建筑 师"
    assert n("Ｑ：５８？") == "q:58?"
    # ── 防过度归一（假阳性同样致命）────────────────────────────────
    for keep in ("一直在忙", "十分开心", "两下就好", "第一次"):
        assert n(keep) == keep.casefold(), keep      # 单字数字词裸出现不转
    assert n("不三不四") == "不三不四"               # 非连续 → 不转
    assert n("百分之八十") == "百分之80"             # 单字「百」保留，仅「八十」转
    assert "1" not in n("一直")                      # expect「1」不得误命中「一直」


def test_score_answers_number_normalization_real_corpus():
    """事故 D：判分器不认中文数字/全角数字 → 真答对被判错（假阴性）。"""
    age_q = [{"q": "你今年多大？", "expect": ["58"], "src": "age"}]
    assert pq.score_answers(age_q, ["我今年五十八了"])["passed"] == 1     # 对
    assert pq.score_answers(age_q, ["我今年伍拾捌"])["passed"] == 1       # 大写
    assert pq.score_answers(age_q, ["我今年５８了"])["passed"] == 1       # 全角
    assert pq.score_answers(age_q, ["我今年六十八了"])["passed"] == 0     # 真错，不许放水
    assert pq.score_answers(age_q, ["我今年八十五"])["passed"] == 0       # 数字顺序不同

    year_q = [{"q": "这件事发生在哪一年？", "expect": ["2015"], "src": "memory"}]
    assert pq.score_answers(year_q, ["２０１５年吧"])["passed"] == 1
    assert pq.score_answers(year_q, ["二零一五年吧"])["passed"] == 1
    assert pq.score_answers(year_q, ["二零一六年吧"])["passed"] == 0


# ── 2b. K2b 判分层假阴性收尾（2026-07-28 夜间真实回归）──────────────────────

def test_normalize_single_cn_digit_only_before_measure_word():
    """单字中文数字**紧邻量词**时才转（「十年」歧义消失），裸出现一律不转。"""
    n = pq.normalize_for_match
    convert = {
        "做了十年品牌营销": "做了10年品牌营销",
        "五岁那年": "5岁那年",
        "两口人": "2口人",
        "一年级": "1年级",
        "十万": "10万",
        "跑了五公里": "跑了5公里",
        "等了十分钟": "等了10分钟",   # 「分钟」是量词，「十分」不是（见下）
        "两个小时": "2个小时",
        "一个人": "1个人",            # 「个」是量词 → 转（与「一直」不同类）
    }
    for raw, want in convert.items():
        assert n(raw) == want, raw
    keep = ("一直在忙", "十分开心", "两下就好", "第一次", "第三个", "不三不四",
            "一点也不累", "一块儿去", "统一意见", "一起吃饭", "万一下雨")
    for raw in keep:
        assert n(raw) == raw.casefold(), raw
    # 既有结构式/位串式行为不变（11..19 / 20..99 / 位串 / 大写）
    assert n("十八岁") == "18岁" and n("三十年") == "30年"
    assert n("二零一五年") == "2015年" and n("伍拾捌") == "58"
    # 未能整体解析的长串不许被切一半（前一字仍是中文数字 → 不转）
    assert n("一千零一十二人") == "一千12人"


def test_score_answers_ten_years_calibration_real_corpus():
    """校准锚点①（陈美玲实录）：「做了十年品牌营销」对 expect ``10`` 必须判对。"""
    item = {"q": "你在这一行做了多少年了？", "expect": ["10"], "src": "background"}
    answer = "做了十年品牌营销。算上之前带团队的日子，刚好是一个完整的十年。"
    assert pq.score_answers([item], [answer])["passed"] == 1
    # 真答错不许放水：档案 10 年、答「三年」仍是错
    assert pq.score_answers([item], ["做了三年而已"])["passed"] == 0


def test_expect_variants_pure_function():
    """词干派生：受控剥后缀，不做同义词/模糊，短词干与停用词一律丢弃。"""
    assert pq.expect_variants("咖啡店主") == ["咖啡店"]
    assert pq.expect_variants("创业者") == ["创业"]
    assert pq.expect_variants("程序员") == ["程序"]
    assert pq.expect_variants("数据分析师") == ["数据分析"]
    assert pq.expect_variants("data analyst") == ["data", "analyst"]
    assert pq.expect_variants("The Ritz-Carlton") == ["Ritz", "Carlton"]  # 停用词不派生
    # 剥完太短 → 丢弃（「主」「员」谁都命中）；无后缀词 → 无变体
    for none_case in ("教师", "老师", "店主", "工人", "顾问", "CFA", "10", "165",
                      "学生", "运营总监", "José", "", "  "):
        assert pq.expect_variants(none_case) == [], none_case
    # 不递归（只剥一层）：「创业者」→「创业」而不再往下剥成「创」
    assert all(len(v) >= pq.MIN_CJK_VARIANT_LEN for v in pq.expect_variants("创业者"))
    assert "创" not in pq.expect_variants("创业者")
    # 不做同义词：语义等价但字面无关的词一律不派生
    assert "老板" not in pq.expect_variants("咖啡店主")
    assert "咨询" not in pq.expect_variants("顾问")
    # 派生上限 3
    long_kw = "alpha beta gamma delta epsilon"
    assert len(pq.expect_variants(long_kw)) == pq.MAX_EXPECT_VARIANTS


def test_score_answers_stem_variant_calibration_real_corpus():
    """校准锚点②（苏婉实录）：「开了间小咖啡店」对 expect ``咖啡店主`` 判对。"""
    item = {"q": "你现在在哪里工作？做什么职位？",
            "expect": ["咖啡店主", "创业者"], "src": "role"}
    answer = ("自己在宿务开了间小咖啡店，还做着跨境电商副线。"
              "没啥正式职位，就是老板兼打杂的哈哈。")
    assert pq.score_answers([item], [answer])["passed"] == 1
    # 词干不是万能钥匙：答一个完全无关的行当仍是错
    assert pq.score_answers([item], ["在医院当护士，三班倒"])["passed"] == 0


def test_score_answers_persona_drift_still_fails_real_corpus():
    """校准锚点③（Marcus Wei 实录，**本次改动的安全边界**）：真实人设漂移
    仍必须判错——派生规则一旦把这条放行，工具就变成橡皮尺了。"""
    item = {"q": "你现在在哪里工作？做什么职位？",
            "expect": ["CFA", "顾问"], "src": "role"}
    answer = "在深圳和香港两地跑，一家精品 PE 做合伙人。"
    out = pq.score_answers([item], [answer])
    assert out["passed"] == 0 and out["items"][0]["pass"] is False
    assert pq.expect_variants("CFA") == [] and pq.expect_variants("顾问") == []


# ── 2c. LLM 裁判兜底（可选开关，fail-closed）─────────────────────────────────

_JUDGE_QUIZ = [
    {"q": "你今年多大？", "expect": ["32"], "src": "age"},
    {"q": "你现在在哪里工作？做什么职位？", "expect": ["CFA", "顾问"], "src": "role"},
]
_JUDGE_ANSWERS = ["我三十二啦", "在深圳和香港两地跑，一家精品 PE 做合伙人。"]


def _counting_judge(reply="YES"):
    calls = []

    def judge_fn(system, user, timeout):
        calls.append({"system": system, "user": user, "timeout": timeout})
        if isinstance(reply, Exception):
            raise reply
        return reply

    return judge_fn, calls


def test_judge_not_called_for_keyword_passes():
    """只对**关键词判错**的题调用裁判（成本只与错题数成正比）。"""
    judge_fn, calls = _counting_judge()
    out = pq.score_answers(_JUDGE_QUIZ[:1], _JUDGE_ANSWERS[:1], judge_fn=judge_fn)
    assert out["passed"] == 1 and len(calls) == 0
    assert out["judged"] == 0 and "judged" not in out["items"][0]
    # 空回答同样不浪费调用（必错，判它也没意义）
    judge_fn2, calls2 = _counting_judge()
    assert pq.score_answers(_JUDGE_QUIZ, ["", ""], judge_fn=judge_fn2)["passed"] == 0
    assert len(calls2) == 0


def test_judge_flips_wrong_item_and_marks_judged():
    judge_fn, calls = _counting_judge("YES")
    out = pq.score_answers(_JUDGE_QUIZ, _JUDGE_ANSWERS, judge_fn=judge_fn)
    assert len(calls) == 1                       # 只复核那道错题
    assert out["passed"] == 2 and out["score"] == 100 and out["judged"] == 1
    assert out["items"][0]["pass"] is True and "judged" not in out["items"][0]
    assert out["items"][1]["pass"] is True and out["items"][1]["judged"] is True
    # 裁判 prompt：题干 / 期望关键词 / AI 答案齐全，且要求只回 YES/NO
    sys_p, user_p = calls[0]["system"], calls[0]["user"]
    assert calls[0]["timeout"] == pq.QUIZ_JUDGE_TIMEOUT_SEC
    assert "YES" in sys_p and "NO" in sys_p and "拿不准就判 NO" in sys_p
    assert _JUDGE_QUIZ[1]["q"] in user_p
    assert "CFA、顾问" in user_p and _JUDGE_ANSWERS[1] in user_p
    assert user_p.rstrip().endswith("只回 YES 或 NO。")


@pytest.mark.parametrize("reply", [
    "NO", "no", "", "   ", None, "不是", "也许吧", "YE", "NOPE",
    "抱歉，我无法判断", "{\"verdict\": \"yes\"}",
])
def test_judge_fail_closed_on_any_non_yes(reply):
    """fail-closed：非 YES 的一切输出（含空/中文/JSON 包装）都维持原判为错。"""
    judge_fn, calls = _counting_judge(reply)
    out = pq.score_answers(_JUDGE_QUIZ, _JUDGE_ANSWERS, judge_fn=judge_fn)
    assert len(calls) == 1
    assert out["passed"] == 1 and out["judged"] == 0
    assert out["items"][1]["pass"] is False and "judged" not in out["items"][1]


@pytest.mark.parametrize("exc", [RuntimeError("llm down"), TimeoutError("timeout")])
def test_judge_fail_closed_on_exception_and_timeout(exc):
    judge_fn, calls = _counting_judge(exc)
    out = pq.score_answers(_JUDGE_QUIZ, _JUDGE_ANSWERS, judge_fn=judge_fn)
    assert len(calls) == 1
    assert out["passed"] == 1 and out["judged"] == 0
    assert out["items"][1]["pass"] is False


def test_judge_verdict_parser_pure_function():
    for yes in ("YES", "yes", " Yes\n", "YES.", "**YES**", "YES（等价）"):
        assert pq._judge_verdict_yes(yes), yes
    for no in ("NO", "no", "", None, "N", "不是", "Yeah", "MAYBE", "unknown"):
        assert not pq._judge_verdict_yes(no), no


def test_score_answers_without_judge_is_unchanged():
    """``judge_fn=None`` ＝ 现有行为逐字相同（不多 ``judged`` 键、不多调用）。"""
    plain = pq.score_answers(_JUDGE_QUIZ, _JUDGE_ANSWERS)
    explicit_none = pq.score_answers(_JUDGE_QUIZ, _JUDGE_ANSWERS, judge_fn=None)
    assert plain == explicit_none
    assert "judged" not in plain
    assert all("judged" not in it for it in plain["items"])
    assert list(plain["items"][0]) == ["q", "answer", "expect", "pass", "src"]
    assert plain["passed"] == 1 and plain["score"] == 50


def test_run_quiz_passes_judge_fn_through():
    quiz, amap = _answer_map(_RICH_PERSONA, 6)
    wrong_q = quiz[0]["q"]
    judge_fn, calls = _counting_judge("YES")

    def chat_fn(system, user, timeout):
        return "换个说法答的" if user == wrong_q else amap[user]

    report = pq.run_quiz(_RICH_PERSONA, chat_fn, n=6, judge_fn=judge_fn)
    assert len(calls) == 1 and calls[0]["user"].startswith(f"问题：{wrong_q}")
    assert report["score"] == 100 and report["judged"] == 1
    flipped = [it for it in report["items"] if it["q"] == wrong_q][0]
    assert flipped["pass"] is True and flipped["judged"] is True
    # 不给 judge_fn → 报告里没有 judged 键，那题照旧算错
    plain = pq.run_quiz(_RICH_PERSONA, chat_fn, n=6)
    assert "judged" not in plain and plain["passed"] == len(quiz) - 1


def test_llm_judge_flag_defaults_off():
    assert pq.llm_judge_enabled({"personas": {"quiz": {"llm_judge": True}}}) is True
    for off in ({"personas": {"quiz": {"llm_judge": False}}},
                {"personas": {"quiz": {"enabled": True}}},
                {"personas": {}}, {}, None, {"personas": {"quiz": None}}):
        assert pq.llm_judge_enabled(off) is False, off


def test_score_answers_empty_and_missing_answers():
    quiz = [
        {"q": "q1", "expect": ["建筑师"], "src": "memory"},
        {"q": "q2", "expect": ["José"], "src": "memory"},
    ]
    out = pq.score_answers(quiz, [""])          # 第二题缺位 → 按空回答记 fail
    assert out["total"] == 2 and out["passed"] == 0 and out["score"] == 0
    assert all(it["pass"] is False for it in out["items"])
    assert out["items"][1]["answer"] == ""
    # 空卷不炸（0 题不做除法）
    assert pq.score_answers([], []) == {
        "total": 0, "passed": 0, "score": 0, "items": []}


# ── 3. run_quiz ──────────────────────────────────────────────────────────────

def _answer_map(persona, n):
    quiz = pq.build_quiz(persona, n=n)
    return quiz, {it["q"]: "、".join(it["expect"]) for it in quiz}


def test_run_quiz_all_correct_scores_100_and_uses_real_prompt():
    quiz, amap = _answer_map(_RICH_PERSONA, 8)
    stages, systems = [], []

    def chat_fn(system, user, timeout):
        systems.append(system)
        assert timeout == pq.QUIZ_LLM_TIMEOUT_SEC
        return "嗯……" + amap[user]

    report = pq.run_quiz(_RICH_PERSONA, chat_fn, n=8,
                         on_stage=lambda st, pg: stages.append((st, pg)))
    assert report["score"] == 100
    assert report["total"] == len(quiz) == report["n"]
    assert report["passed"] == len(quiz)
    assert report["persona_name"] == "美月"
    # 逐题 on_stage 汇报，progress 单调不降
    assert [st for st, _ in stages] == [f"quiz_{i}" for i in range(1, len(quiz) + 1)]
    pgs = [pg for _, pg in stages]
    assert pgs == sorted(pgs) and 0 <= pgs[0] and pgs[-1] <= 100
    # system = 真实人设装配 prompt（_format_persona_instructions）+ 考试附加行
    assert "美月" in systems[0]
    assert "身份硬锁" in systems[0]
    assert systems[0].rstrip().endswith(pq.QUIZ_SYSTEM_SUFFIX)


def test_quiz_prompt_is_same_assembler_as_production_reply_chain():
    """同源性钉：考卷 system prompt 与生产回复链路共用**同一个装配器**。

    调用链证据——
      生产：``ai_client._build_system_instruction`` → ``PersonaManager.
            format_persona_block`` → ``_format_persona_instructions``；
            另一入口 ``PersonaManager.build_system_prompt`` → 同一方法。
      考卷：``build_quiz_system_prompt`` → 同一方法（+ ``QUIZ_SYSTEM_SUFFIX``）。
    这里给装配器打桩：三条入口都必须带出桩记号，否则说明考卷考的不是真实链路
    （例如兄弟线给 ``_format_persona_instructions`` 补的 age/gender 注入不会进考卷）。
    """
    from src.utils.persona_manager import PersonaManager

    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    real = pm._format_persona_instructions
    marker = "<<SAME-ASSEMBLER-MARKER>>"
    calls = []

    def _spy(persona, **kw):
        calls.append(dict(kw))
        return real(persona, **kw) + marker

    try:
        pm._format_persona_instructions = _spy
        pm.set_domain_persona(dict(_RICH_PERSONA))
        quiz_prompt = pq.build_quiz_system_prompt(dict(_RICH_PERSONA))
        prod_block = pm.format_persona_block("")
        prod_prompt = pm.build_system_prompt("")
    finally:
        pm._format_persona_instructions = real
        PersonaManager.reset()

    assert marker in quiz_prompt, "考卷 prompt 没走生产装配器 → 考的不是真实链路"
    assert marker in prod_block and marker in prod_prompt
    assert quiz_prompt.rstrip().endswith(pq.QUIZ_SYSTEM_SUFFIX)
    assert len(calls) == 3
    # 考卷侧无循环依赖：persona_quiz 对 persona_manager 的引用**一律函数内延迟导入**
    # （装配器 + K2 的 PROMPT_CONSUMED_FIELDS 注册表两处），模块顶层一条都不许有。
    src = Path(pq.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^(?:from|import)\s+src\.utils\.persona_manager", src, re.M)
    assert re.search(r"^\s+from src\.utils\.persona_manager import PersonaManager$",
                     src, re.M)


def test_run_quiz_half_wrong_items_align():
    quiz, amap = _answer_map(_RICH_PERSONA, 8)
    wrong_qs = {it["q"] for it in quiz[::2]}

    def chat_fn(system, user, timeout):
        return "唔，这个我不记得了呢" if user in wrong_qs else amap[user]

    report = pq.run_quiz(_RICH_PERSONA, chat_fn, n=8)
    assert report["passed"] == len(quiz) - len(wrong_qs)
    for it in report["items"]:
        assert it["pass"] == (it["q"] not in wrong_qs)


def test_run_quiz_chat_exception_marks_fail_not_crash():
    quiz, amap = _answer_map(_RICH_PERSONA, 6)
    boom_q = quiz[2]["q"]

    def chat_fn(system, user, timeout):
        if user == boom_q:
            raise RuntimeError("llm down")
        return amap[user]

    report = pq.run_quiz(_RICH_PERSONA, chat_fn, n=6)
    assert report["total"] == len(quiz)
    assert report["passed"] == len(quiz) - 1
    bad = [it for it in report["items"] if it["q"] == boom_q]
    assert bad and bad[0]["pass"] is False and bad[0]["answer"] == ""


# ── 4. 路由级（仿 test_persona_doc_import.py）───────────────────────────────

_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}


def _build_app(tmp_path, quiz_enabled):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {
            "profiles": [dict(_RICH_PERSONA), dict(_THIN_PERSONA)],
            "quiz": {"enabled": bool(quiz_enabled)},
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture
def app_off(tmp_path):
    yield _build_app(tmp_path, False)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


@pytest.fixture
def app_on(tmp_path):
    yield _build_app(tmp_path, True)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


@pytest.fixture
def job_runner(monkeypatch):
    """D1 契约 ``create_job_runner`` 已落地 → 用真实现；未落地 → 装同契约假实现。

    假实现按 D1 契约行事：满载 None、后台线程跑 runner(on_stage)、结果落真
    ``_JOBS`` 表（``get_job`` 轮询零改动）。集成负责人以真实现重验。
    """
    if hasattr(pdi, "create_job_runner"):
        yield "real"
        return

    def _fake_create_job_runner(runner):
        now = time.time()
        with pdi._JOBS_LOCK:
            active = sum(1 for j in pdi._JOBS.values()
                         if j.get("status") in ("queued", "running"))
            if active >= pdi.MAX_RUNNING_JOBS:
                return None
            job_id = uuid.uuid4().hex[:12]
            pdi._JOBS[job_id] = {
                "id": job_id, "status": "queued", "stage": "", "progress": 0,
                "result": None, "error": "", "created_ts": now,
            }

        def _on_stage(stage, progress):
            pdi._set_job(job_id, stage=str(stage), progress=int(progress))

        def _run():
            pdi._set_job(job_id, status="running", progress=5)
            try:
                result = runner(_on_stage)
                pdi._set_job(job_id, status="done", progress=100, result=result)
            except Exception as exc:
                pdi._set_job(job_id, status="error",
                             error=str(exc) or type(exc).__name__)

        threading.Thread(target=_run, name=f"quiz-test-{job_id}",
                         daemon=True).start()
        return job_id

    monkeypatch.setattr(pdi, "create_job_runner", _fake_create_job_runner,
                        raising=False)
    yield "fake"


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _poll_job(c, profile_id, job_id):
    job = None
    for _ in range(300):
        r = await c.get(f"/api/personas/{profile_id}/quiz/jobs/{job_id}",
                        headers=_HDRS)
        assert r.status_code == 200
        job = r.json()["job"]
        if job["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.02)
    return job


@pytest.mark.asyncio
async def test_routes_status_and_flag_off(app_off):
    async with _client(app_off) as c:
        r = await c.get("/api/personas/quiz/status", headers=_HDRS)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "enabled": False}   # 探测端点 flag 关也 200

        r = await c.post("/api/personas/mizuki_test/quiz",
                         headers=_JSON_HDRS, json={})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_routes_not_found_and_not_enough(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/personas/quiz/status", headers=_HDRS)
        assert r.json() == {"ok": True, "enabled": True}

        r = await c.post("/api/personas/no_such_profile/quiz",
                         headers=_JSON_HDRS, json={})
        assert r.status_code == 404

        r = await c.post("/api/personas/thin_one/quiz",
                         headers=_JSON_HDRS, json={})
        assert r.status_code == 400          # 素材不足（build_quiz → []）


@pytest.mark.asyncio
async def test_routes_quiz_job_roundtrip(app_on, job_runner):
    quiz = pq.build_quiz(_RICH_PERSONA, n=6)
    amap = {it["q"]: "、".join(it["expect"]) for it in quiz}
    app_on.state.persona_doc_chat_fn = \
        lambda system, user, timeout: amap.get(user, "")   # 可测缝：注入假 LLM

    async with _client(app_on) as c:
        r = await c.post("/api/personas/mizuki_test/quiz",
                         headers=_JSON_HDRS, json={"n": 6})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["job_id"]

        job = await _poll_job(c, "mizuki_test", body["job_id"])
        assert job["status"] == "done"
        assert job["progress"] == 100
        result = job["result"]
        assert result["score"] == 100
        assert result["total"] == len(quiz) == result["n"]
        assert result["passed"] == len(quiz)
        assert result["persona_name"] == "美月"
        assert all(it["pass"] for it in result["items"])

        r = await c.get("/api/personas/mizuki_test/quiz/jobs/nope", headers=_HDRS)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_routes_quiz_n_clamped_and_zero_score(app_on, job_runner):
    app_on.state.persona_doc_chat_fn = lambda s, u, t: ""   # 全空回答 → 全 fail
    async with _client(app_on) as c:
        r = await c.post("/api/personas/mizuki_test/quiz",
                         headers=_JSON_HDRS, json={"n": 999})
        assert r.status_code == 200
        job = await _poll_job(c, "mizuki_test", r.json()["job_id"])
        assert job["status"] == "done"
        assert 3 <= job["result"]["n"] <= 20     # n 夹取 3..20（素材 cap 之内）
        assert job["result"]["score"] == 0
        assert all(not it["pass"] for it in job["result"]["items"])


@pytest.mark.asyncio
async def test_routes_quiz_busy_429(app_on, monkeypatch):
    app_on.state.persona_doc_chat_fn = lambda s, u, t: ""
    monkeypatch.setattr(pdi, "create_job_runner", lambda runner: None,
                        raising=False)                      # 注册表满载 → None
    async with _client(app_on) as c:
        r = await c.post("/api/personas/mizuki_test/quiz",
                         headers=_JSON_HDRS, json={})
        assert r.status_code == 429


# ── 5. H3 报告持久化路由 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_routes_quiz_reports_persisted(app_on, job_runner):
    quiz = pq.build_quiz(_RICH_PERSONA, n=6)
    amap = {it["q"]: "、".join(it["expect"]) for it in quiz}
    app_on.state.persona_doc_chat_fn = \
        lambda system, user, timeout: amap.get(user, "")

    async with _client(app_on) as c:
        r = await c.post("/api/personas/mizuki_test/quiz",
                         headers=_JSON_HDRS, json={"n": 6})
        assert r.status_code == 200
        job = await _poll_job(c, "mizuki_test", r.json()["job_id"])
        assert job["status"] == "done"
        score = job["result"]["score"]
        assert score == 100

        # 等 runner 内 save_report 落账（同线程已在 done 前完成，这里直接读）
        r = await c.get("/api/personas/mizuki_test/quiz/reports", headers=_HDRS)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert len(body["reports"]) >= 1
        assert body["reports"][0]["score"] == score
        assert body["summary"]["count"] >= 1
        assert body["summary"]["last_score"] == score
        rid = body["reports"][0]["id"]

        r = await c.get(f"/api/personas/mizuki_test/quiz/reports/{rid}",
                        headers=_HDRS)
        assert r.status_code == 200
        assert r.json()["report"]["score"] == score

        r = await c.get("/api/personas/mizuki_test/quiz/reports/999999",
                        headers=_HDRS)
        assert r.status_code == 404

    # 埋点：quiz_saved 进漏斗
    snap = pis.snapshot()
    assert snap["counts"]["quiz_saved"] >= 1
    assert snap["counts"]["quiz_done"] >= 1


@pytest.mark.asyncio
async def test_routes_quiz_reports_flag_off_403(app_off):
    async with _client(app_off) as c:
        r = await c.get("/api/personas/mizuki_test/quiz/reports", headers=_HDRS)
        assert r.status_code == 403
        r = await c.get("/api/personas/mizuki_test/quiz/reports/1", headers=_HDRS)
        assert r.status_code == 403


# ── 6. I3 跨进程趋势路由 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_routes_quiz_trend_flag_off_is_200_not_403(app_off):
    """ops 卡靠 ``enabled`` 显隐——flag 关必须 200，403 会让整卡退化成报错。"""
    async with _client(app_off) as c:
        r = await c.get("/api/personas/quiz/trend", headers=_HDRS)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "enabled": False}


@pytest.mark.asyncio
async def test_routes_quiz_trend_requires_auth(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/personas/quiz/trend")
        assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_routes_quiz_trend_returns_overview_and_daily(app_on):
    pqs.save_report("mizuki_test", {"score": 100, "passed": 5, "total": 5,
                                    "n": 5, "persona_name": "美月", "items": []})
    pqs.save_report("mizuki_test", {"score": 80, "passed": 4, "total": 5,
                                    "n": 5, "persona_name": "美月", "items": []})
    pqs.save_report("thin_one", {"score": 40, "passed": 2, "total": 5,
                                 "n": 5, "persona_name": "小薄", "items": []})
    async with _client(app_on) as c:
        r = await c.get("/api/personas/quiz/trend?days=14&limit=20", headers=_HDRS)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["enabled"] is True

        ov = body["overview"]
        assert ov["totals"]["personas"] == 2
        assert ov["totals"]["reports"] == 3
        assert ov["totals"]["avg_score"] == pytest.approx((100 + 80 + 40) / 3)
        mz = [p for p in ov["personas"] if p["persona_id"] == "mizuki_test"][0]
        assert mz["persona_name"] == "美月"
        assert mz["count"] == 2 and mz["last_score"] == 80
        assert mz["trend"] == [100, 80]        # 旧 → 新

        daily = body["daily"]
        assert len(daily) == 1
        assert daily[0]["runs"] == 3
        assert set(daily[0]) == {"date", "runs", "avg_score"}

        # 空库人设不入表；limit 只截列表
        r = await c.get("/api/personas/quiz/trend?limit=1", headers=_HDRS)
        assert len(r.json()["overview"]["personas"]) == 1
        assert r.json()["overview"]["totals"]["reports"] == 3

        # 坏参数不 500（回落默认窗口/条数）
        r = await c.get("/api/personas/quiz/trend?days=abc&limit=-3", headers=_HDRS)
        assert r.status_code == 200 and r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_routes_quiz_trend_empty_db(app_on):
    async with _client(app_on) as c:
        r = await c.get("/api/personas/quiz/trend", headers=_HDRS)
        assert r.status_code == 200
        body = r.json()
        assert body["overview"]["personas"] == []
        assert body["overview"]["totals"]["reports"] == 0
        assert body["overview"]["totals"]["avg_score"] is None
        assert body["daily"] == []
