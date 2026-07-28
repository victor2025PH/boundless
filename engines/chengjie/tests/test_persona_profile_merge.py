"""人设保存 merge 语义 + 完整度评分门禁（2026-07-27 后端 A 线）。

背景：Studio 前端用表单字段**重建**整个 persona 发 PUT，后端 upsert 是整体
替换 → 富人设字段（background/life_arc/tastes/appearance/names/context…）
被一次保存全部抹掉。修复＝请求体 ``merge: true`` 时与既有人设深合并。

覆盖四层：
1. 纯函数 ``PersonaManager.deep_merge_profile``（递归合并/list 整体替换/
   标量覆盖/空 patch 不丢 base/不改入参）。
2. 纯函数 ``persona_completeness``（空盘 0 分、富盘高分、personality 字符串
   算 style、坏类型不炸）。
3. 路由级 merge：富人设 + 表单式部分 PUT → 富字段原样保留、表单字段更新。
4. 向后兼容：不带 merge 的 PUT 仍是整体替换旧契约。
"""

import asyncio
import copy
import sys
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.persona_completeness import FIELD_WEIGHTS, persona_completeness
from src.utils.persona_manager import PersonaManager

_PROFILES = {
    "personas": {
        "profiles": [
            {"id": "chen_mo", "name": "陈默", "role": "陪伴"},
        ]
    }
}

_HDRS = {"Authorization": "Bearer test-token",
         "Content-Type": "application/json"}


def _rich_persona():
    """su_wan 风格富人设 fixture（覆盖全部计分字段）。"""
    return {
        "name": "苏婉",
        "role": "在菲律宾宿务的中国创业者 / 咖啡店主，32岁",
        "age": 32,
        "gender": "female",
        "appearance": "a 32-year-old Chinese woman living in Cebu Philippines",
        "selfie_scenes": [
            "behind the counter of her coffee shop, morning light",
            "Cebu IT Park street cafe with laptop, afternoon",
        ],
        "life_arc": {
            "theme": "宿务咖啡店步入正轨，跨境电商副线在扩",
            "stride_days": 3,
            "beats": ["店员培训到一半鸡飞狗跳", "去马尼拉见供应商谈豆价"],
        },
        "tastes": {
            "likes": ["手冲咖啡", "芒果干", "海边散步"],
            "dislikes": ["拖泥带水的合作方", "台风季断电"],
            "opinions": ["省下来的时间比省下来的钱值钱"],
        },
        "names": {"nickname": "婉姐", "usage_notes": "圈子里熟人叫婉姐"},
        "background": "福建人，32岁。大学毕业后在厦门做过四年外贸跟单，"
                      "2019 年来菲律宾，2022 年在宿务盘下一家小咖啡店。",
        "tags": ["女性", "32岁", "菲律宾", "创业者"],
        "personality": {
            "traits": ["干练爽利不啰嗦", "温暖有分寸感", "务实精明"],
            "style": "像微信里认识的靠谱生意人朋友：口语化、短句",
            "emoji_level": "medium",
            "quirks": "爱用「说实话」「讲真」开头讲心得",
            "humor": "自嘲式幽默，不损聊天对象",
            "temperament": "情绪稳定，被质疑时用事实和数字说话",
        },
        "speaking": {
            "openers": ["刚打烊，坐下来喘口气，你那边忙完没"],
            "forbidden_phrases": ["作为AI", "您好", "很高兴为您服务"],
            "reply_length": "short",
        },
        "identity": {"deny_ai": True, "claim_human": True},
        "context": {
            "hobbies": ["手冲咖啡", "逛本地市场", "记账和复盘"],
            "specific_memories": [
                "2019 年第一次落地马尼拉，行李箱轮子摔坏",
                "咖啡店开业第一个月亏到怀疑人生",
                "客服半夜漏回大客户消息，丢了整柜订单",
                "第一次用翻译工具顺畅谈完价格",
                "帮商会大姐跑了两趟使馆",
            ],
            "emotional_triggers": {
                "positive": "对方认真做事、聊具体的业务困难",
                "negative": "对方吹牛画饼、打听收入资产",
                "deep_empathy": "同是漂在海外做生意的人",
            },
        },
        "boundaries": {"topics_to_avoid": ["政治", "赌博博彩推广", "成人内容"]},
        "voice_profile": {"backend": "avatar_clone",
                          "voice": "zh-CN-XiaoxiaoNeural"},
    }


# 前端表单式部分保存（C 线将发的形状；契约样例原文）
_FORM_PATCH = {
    "persona": {
        "name": "新名",
        "role": "新角色",
        "personality": {"style": "新风格"},
        "tags": ["a", "b", "c"],
        "speaking": {"reply_length": "balanced", "emoji_level": "moderate",
                     "forbidden_phrases": [], "openers": []},
        "identity": {"deny_ai": True, "claim_human": True},
        "voice_profile": {"backend": "", "voice": ""},
    },
    "merge": True,
}


# ── 1. deep_merge_profile 纯函数 ─────────────────────────────────────────────

def test_deep_merge_profile_semantics():
    base = {
        "a": {"x": 1, "y": {"deep": "keep"}},
        "lst": [1, 2, 3],
        "s": "old",
        "flag": True,
        "base_only": "survives",
    }
    patch = {
        "a": {"x": 2, "z": 3},          # 嵌套 dict → 递归合并
        "lst": [9],                      # list → 整体替换
        "s": "new",                      # 标量 → 覆盖
        "flag": None,                    # None 也覆盖
        "extra": {"n": 1},               # 新键 → 追加
    }
    base_snap = copy.deepcopy(base)
    patch_snap = copy.deepcopy(patch)

    out = PersonaManager.deep_merge_profile(base, patch)
    assert out["a"] == {"x": 2, "y": {"deep": "keep"}, "z": 3}
    assert out["lst"] == [9]
    assert out["s"] == "new"
    assert out["flag"] is None
    assert out["base_only"] == "survives"
    assert out["extra"] == {"n": 1}

    # 不就地修改入参
    assert base == base_snap
    assert patch == patch_snap
    # 返回值与入参解耦（改返回值不脏 base）
    out["a"]["y"]["deep"] = "mutated"
    assert base["a"]["y"]["deep"] == "keep"


def test_deep_merge_profile_empty_patch_keeps_base():
    base = _rich_persona()
    out = PersonaManager.deep_merge_profile(base, {})
    assert out == base
    assert out is not base
    # 坏类型入参不炸（宁可空盘不可崩）
    assert PersonaManager.deep_merge_profile(None, {"a": 1}) == {"a": 1}
    assert PersonaManager.deep_merge_profile({"a": 1}, None) == {"a": 1}
    # dict 覆盖非 dict（自由格式 personality 字符串被结构化 patch 替换）
    out2 = PersonaManager.deep_merge_profile(
        {"personality": "自由文本"}, {"personality": {"style": "x"}})
    assert out2["personality"] == {"style": "x"}


# ── 2. persona_completeness 纯函数 ───────────────────────────────────────────

def test_completeness_empty_dict_zero():
    r = persona_completeness({})
    assert r["score"] == 0
    assert r["filled"] == []
    assert "background" in r["missing"]
    # missing 只列权重 >= 4 的缺口
    assert all(FIELD_WEIGHTS[k] >= 4 for k in r["missing"])
    assert "selfie_scenes" not in r["missing"]  # 权重 2 不进 missing


def test_completeness_rich_persona_high_score():
    r = persona_completeness(_rich_persona())
    assert r["score"] >= 80
    assert r["missing"] == []
    for key in ("background", "personality.style", "context.specific_memories",
                "tastes", "names", "voice_profile"):
        assert key in r["filled"], key


def test_completeness_personality_string_counts_as_style():
    r = persona_completeness({"personality": "自由格式人设描述"})
    assert "personality.style" in r["filled"]
    assert r["score"] > 0
    # 但其余 personality 子键不算填了
    assert "personality.traits" in r["missing"]


def test_completeness_partial_memories_half_credit():
    full = persona_completeness(
        {"context": {"specific_memories": ["a", "b", "c", "d", "e"]}})
    half = persona_completeness({"context": {"specific_memories": ["a"]}})
    none = persona_completeness({"context": {"specific_memories": []}})
    assert full["score"] > half["score"] > none["score"] == 0
    assert "context.specific_memories" in half["filled"]  # 半分也算已填
    assert "context.specific_memories" in none["missing"]
    # tags 阈值：>=3 才算
    assert "tags" in persona_completeness({"tags": ["a", "b", "c"]})["filled"]
    assert "tags" in persona_completeness({"tags": ["a", "b"]})["missing"]


def test_completeness_bad_types_do_not_crash():
    junk = {
        "personality": 42,
        "tags": "not-a-list",
        "context": "junk-string",
        "names": 3.14,
        "voice_profile": "edge",
        "speaking": ["wrong"],
        "boundaries": None,
        "tastes": 0,
        "selfie_scenes": {},
        "background": ["列表也行", ""],
    }
    r = persona_completeness(junk)
    assert isinstance(r["score"], int) and 0 <= r["score"] <= 100
    # 非 dict 入参按空盘处理
    for bad in (None, "str", 42, ["list"]):
        r2 = persona_completeness(bad)
        assert r2["score"] == 0


# ── 3/4. 路由级 merge + 向后兼容 ─────────────────────────────────────────────

@pytest.fixture
def app_on(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": _PROFILES["personas"],
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    PersonaManager.get_instance().load_profiles_from_config(_PROFILES)

    from src.web.admin import create_app
    app = create_app(cm)
    yield app
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_route_merge_preserves_rich_fields(app_on):
    """富人设 + 表单式部分 PUT(merge=true) → 富字段原样保留、表单字段更新。"""
    rich = _rich_persona()
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/su_wan", headers=_HDRS,
                        json={"persona": rich})
        assert r.status_code == 200
        assert r.json()["merged"] is False  # 首存无既有盘，非 merge

        r = await c.put("/api/personas/profiles/su_wan", headers=_HDRS,
                        json=_FORM_PATCH)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "profile_id": "su_wan", "merged": True}

        r = await c.get("/api/personas/profiles/su_wan", headers=_HDRS)
        assert r.status_code == 200
        p = r.json()["persona"]

    # 富字段原样保留（merge 前整体替换语义下这些全会被抹掉）
    for key in ("background", "life_arc", "tastes", "appearance", "names",
                "context"):
        assert p[key] == rich[key], key
    # personality：表单只发 style → 其余子键保留、style 更新
    assert p["personality"]["style"] == "新风格"
    for sub in ("traits", "quirks", "humor", "temperament"):
        assert p["personality"][sub] == rich["personality"][sub], sub
    # 表单字段已更新
    assert p["name"] == "新名"
    assert p["role"] == "新角色"
    assert p["tags"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_route_put_without_merge_replaces(app_on):
    """向后兼容契约：不带 merge 的 PUT 仍是整体替换。"""
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/su_wan", headers=_HDRS,
                        json={"persona": _rich_persona()})
        assert r.status_code == 200

        slim = {"name": "只有名字", "role": "极简"}
        r = await c.put("/api/personas/profiles/su_wan", headers=_HDRS,
                        json={"persona": slim})
        assert r.status_code == 200
        assert r.json()["merged"] is False

        r = await c.get("/api/personas/profiles/su_wan", headers=_HDRS)
        p = r.json()["persona"]

    assert p["name"] == "只有名字"
    assert "background" not in p          # 整体替换 → 富字段确实没了
    assert "life_arc" not in p


@pytest.mark.asyncio
async def test_route_merge_on_missing_profile_falls_back(app_on):
    """merge=true 但人设不存在 → 按原行为直接落库（不 404 不炸）。"""
    async with _client(app_on) as c:
        r = await c.put("/api/personas/profiles/brand_new", headers=_HDRS,
                        json={"persona": {"name": "新人"}, "merge": True})
        assert r.status_code == 200
        assert r.json()["merged"] is False

        r = await c.get("/api/personas/profiles/brand_new", headers=_HDRS)
        assert r.json()["persona"]["name"] == "新人"


# ── 5. list_profiles_summary 完整度评分 ──────────────────────────────────────

def test_list_profiles_summary_has_completeness():
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm.load_profiles_from_config(_PROFILES)
        pm.upsert_profile("rich_p", _rich_persona())
        by_id = {s["id"]: s for s in pm.list_profiles_summary()}

        for entry in by_id.values():
            assert isinstance(entry["completeness"], int)
            assert 0 <= entry["completeness"] <= 100
        assert by_id["rich_p"]["completeness"] >= 80
        # 出厂瘦人设（只有 name/role）分数应显著更低
        assert by_id["chen_mo"]["completeness"] < by_id["rich_p"]["completeness"]
    finally:
        PersonaManager.reset()
