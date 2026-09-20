# -*- coding: utf-8 -*-
"""spoken_style 桥接契约（红绿双向）：默认关=零影响；开了=三层生效；包缺席=永久 no-op。

零 chengjie 依赖（桥接本身只用 stdlib），也可独立直跑：
    python tests/test_spoken_style_bridge.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai import spoken_style_bridge as br


class _Cfg:
    """ConfigManager 形态替身（bridge 只碰 .config dict）。"""

    def __init__(self, ss: dict | None):
        self.config = {"ai": ({"spoken_style": ss} if ss is not None else {})}


_ON = {"enabled": True}


def _fresh():
    """重置桥接的惰性加载态（模块级缓存在用例间串味的防线）。"""
    br._MOD = None
    br._FAILED = False
    br._PKG_DIR_OVERRIDE = None


def test_disabled_is_noop():
    _fresh()
    cfg = _Cfg({"enabled": False})
    assert br.system_block(cfg) == ""
    assert br.turn_tail(cfg, "给我讲讲你小时候的事") == ""
    assert br.clean_reply_text(cfg, "[情绪:开心|中] 哈喽") == "[情绪:开心|中] 哈喽"
    # 配置段整个缺席也一样安静
    assert br.turn_tail(_Cfg(None), "你好呀") == ""


def test_tail_zh_gate():
    _fresh()
    cfg = _Cfg(dict(_ON))
    zh = br.turn_tail(cfg, "你周末一般都干嘛呀")
    assert "【说话稿】" in zh and "1～3 句" in zh
    # zh_only 缺省开：英文消息不注入（防中文口语指令污染外语对话）
    assert br.turn_tail(cfg, "What do you usually do on weekends?") == ""
    # 显式关掉 zh_only 才对外语注入
    cfg2 = _Cfg({"enabled": True, "zh_only": False})
    assert "【说话稿】" in br.turn_tail(cfg2, "What's up bro")


def test_tail_story_autolevel():
    _fresh()
    tail = br.turn_tail(_Cfg(dict(_ON)), "给我讲讲你小时候印象最深的一件事")
    assert "半句重启" in tail            # 故事意图自动升到档 3
    plain = br.turn_tail(_Cfg(dict(_ON)), "今天天气怎么样呀")
    assert "半句重启" not in plain       # 普通闲聊停在档 2


def test_system_block_opt_in_pieces():
    _fresh()
    # 缺省（无指纹角色、副语言关、情绪协议关）：没有可注入的稳定段
    assert br.system_block(_Cfg(dict(_ON))) == ""
    got = br.system_block(_Cfg({"enabled": True, "emotion_tags": True, "paraling": True}))
    assert "【副语言】" in got and "情绪标记" in got
    # 指纹角色（包内样例）
    got2 = br.system_block(_Cfg({"enabled": True, "role": "美月"}))
    assert got2.startswith("【说话指纹】")


def test_clean_strips_but_never_empties():
    _fresh()
    cfg = _Cfg(dict(_ON))
    out = br.clean_reply_text(cfg, "[情绪:开心|中|有点得意] 真的假的？太棒了[轻笑]。")
    assert "情绪" not in out and "轻笑" not in out and "真的假的" in out
    # 清洁不许清成空：整条只有标记时兜底原句
    only_tag = "[轻笑]"
    assert br.clean_reply_text(cfg, only_tag) == only_tag


async def test_rewrite_gates_and_fact_lock():
    _fresh()
    # rewrite 未开 → 原样
    assert await br.rewrite_reply(_Cfg(dict(_ON)), "今天挺好的。") == "今天挺好的。"
    cfg = _Cfg({"enabled": True, "rewrite": True, "role": "美月"})
    ss = br._load()
    cr = ss.colloquial_rewrite

    async def _fake_ok(role, text, timeout, client=None):
        return "他家人均 120 块诶，是真的真的好呢。"

    async def _fake_drop_num(role, text, timeout, client=None):
        return "他家挺贵的，但是真的好。"

    orig = cr._llm_rewrite
    try:
        cr._llm_rewrite = _fake_ok
        out = await br.rewrite_reply(cfg, "这家店人均 120 块，味道特别好。")
        assert "120" in out and "诶" in out            # 合法改写放行
        cr._llm_rewrite = _fake_drop_num
        src = "这家店人均 120 块，味道特别好。"
        assert await br.rewrite_reply(cfg, src) == src  # 丢数字→事实锁拒→原句直通
        # 非中文回复不碰（改写器是中文口语手艺）
        en = "Sure, the average cost is 120 yuan per person."
        assert await br.rewrite_reply(cfg, en) == en
        # 无指纹角色不开（文本×声学配套是包侧拍板）
        cfg2 = _Cfg({"enabled": True, "rewrite": True, "role": "查无此人"})
        assert await br.rewrite_reply(cfg2, src) == src
    finally:
        cr._llm_rewrite = orig
        cr._flag_cache["t"] = 0.0
        cr._flag_cache["v"] = None


def test_dynamic_role_overrides_config():
    _fresh()
    # 会话人设口称名（参数）优先于配置静态 role：配置没配角色也能按人设分流指纹
    # （指纹块是指纹内容不含名字字面，按两人区分性内容断言：美月=想词日语系，秦震=短句直给系）
    got = br.system_block(_Cfg(dict(_ON)), role="美月")
    assert got.startswith("【说话指纹】") and "そうだね" in got
    # 参数压过配置里写死的另一个角色
    got2 = br.system_block(_Cfg({"enabled": True, "role": "秦震"}), role="美月")
    assert "そうだね" in got2 and "冷幽默" not in got2
    # 查无此人=退回通用层（无指纹段，不报错）
    assert br.system_block(_Cfg(dict(_ON)), role="查无此人") == ""


async def test_rewrite_dynamic_role_and_stats():
    _fresh()
    cfg = _Cfg({"enabled": True, "rewrite": True})   # 配置不写 role，全靠会话人设
    ss = br._load()
    cr = ss.colloquial_rewrite
    seen_roles = []

    async def _fake(role, text, timeout, client=None):
        seen_roles.append(role)
        return None                                   # 模拟后端失败/拒绝 → 直通

    orig = cr._llm_rewrite
    base = br.stats()
    try:
        cr._llm_rewrite = _fake
        src = "这家店人均 120 块，味道特别好。"
        assert await br.rewrite_reply(cfg, src, role="美月") == src
        assert seen_roles == ["美月"]                 # 动态 role 真到了改写器
        # 无 role（配置空+参数空）→ 连尝试都不发起
        assert await br.rewrite_reply(cfg, src) == src
        assert seen_roles == ["美月"]
        now = br.stats()
        assert now["l4_attempt"] - base["l4_attempt"] == 1
        assert now["l4_passthrough"] - base["l4_passthrough"] == 1
        assert now["l4_applied"] == base["l4_applied"]
    finally:
        cr._llm_rewrite = orig
        cr._flag_cache["t"] = 0.0
        cr._flag_cache["v"] = None


def test_stats_count_injections():
    _fresh()
    base = br.stats()
    cfg = _Cfg(dict(_ON))
    br.turn_tail(cfg, "你周末一般都干嘛呀")                       # l2_inject
    br.turn_tail(cfg, "What do you usually do on weekends?")      # l2_skip_lang
    br.system_block(cfg, role="美月")                             # l1_inject
    br.clean_reply_text(cfg, "[轻笑]哈喽呀")                      # l3_changed
    now = br.stats()
    for k in ("l1_inject", "l2_inject", "l2_skip_lang", "l3_changed"):
        assert now[k] - base[k] == 1, k


def test_missing_package_is_permanent_noop():
    _fresh()
    br._PKG_DIR_OVERRIDE = r"Z:\no\such\dir"
    cfg = _Cfg(dict(_ON))
    assert br.system_block(cfg) == ""
    assert br.turn_tail(cfg, "你好呀今天") == ""
    assert br.clean_reply_text(cfg, "原样") == "原样"
    assert br._FAILED is True            # 失败被记住，不反复重试
    _fresh()


if __name__ == "__main__":
    import asyncio
    import inspect

    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if inspect.iscoroutinefunction(fn):
                    asyncio.run(fn())
                else:
                    fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}  {e}")
    raise SystemExit(1 if fails else 0)
