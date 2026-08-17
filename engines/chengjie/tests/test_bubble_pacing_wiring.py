"""分条发送节奏接线门禁（2026-08-09）。

背景：老板要求「全自动回复拆成几句发送时必须带拟人打字间隔，不能几句秒发」。
纯函数层（reply_split.plan_bubble_gaps / typing_time_sec）由 test_reply_split 覆盖；
本文件钉**三条投递链的接线不变量**——纯函数写得再对，消费方漏传一个参数
（latin_per_char_sec / total_budget_sec）就会在对应链路上静默退化成机关枪，
且没有任何报错。静态源码断言（与 test_human_deliver_independent_of_autosend
的「静态接线」门禁同风格）：

  1. 三链（A 线 telegram_client / B 线 autosend_helpers / 手动 send_routes）
     条间隔一律走 plan_bubble_gaps 预排（total_budget_sec 等比压缩 +
     latin_per_char_sec 英文真实手速），不再逐条调 inter_part_delay_sec
     （逐条调=没有总预算概念，per_sentence 5 条×20s 可拖到 80s+）。
  2. 条间「正在输入」时长（estimate_typing_lead）同样带 latin 手速——
     间隔对了但打字气泡只挂 1 秒，英文客户看到的还是假节奏。
  3. holdout 保留组抽中时必须 collapse_paragraphs 折叠成单段再发——
     bubbles 开启时拟稿合同是「每行一句」，多行原样单条发出＝
     2026-08-08 客户实锤质疑「why always 2 parts」的形态，比拆条更糟。
     A/B 两线都要（A 线 2026-08-09 补齐对齐 B 线）。
  4. 手动路径 record_bubble_gap("manual", ...) 观测入列——三个来源
     （autosend/aline/manual）在 autosend-status.pacing 同口径可见。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_ALINE = ROOT / "src" / "client" / "telegram_client.py"
_BLINE = ROOT / "src" / "inbox" / "autosend_helpers.py"
_MANUAL = ROOT / "src" / "web" / "routes" / "unified_inbox_send_routes.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 1. 三链条间隔全部走 plan_bubble_gaps 预排 ────────────────────────────────

def _call_window(src: str, call_token: str, size: int = 700) -> str:
    """取 ``call_token``（形如 ``_pbg(``）**调用点**起的窗口文本。

    刻意不用裸函数名定位——那会先命中 import 行（``plan_bubble_gaps as _pbg``），
    窗口切出来全是 import 块（首版踩过）。找不到调用点＝断言失败信息里直接说。
    """
    i = src.find(call_token)
    assert i >= 0, f"未找到调用点 {call_token!r}"
    return src[i: i + size]


def test_aline_plans_gaps_with_budget_and_latin():
    src = _src(_ALINE)
    assert "plan_bubble_gaps" in src, "A 线丢失条间隔预排（plan_bubble_gaps）"
    # 预排调用必须带英文手速与总预算（缺一个=对应维度静默退化）
    call = _call_window(src, "_pbg_bub(")
    assert "latin_per_char_sec" in call, "A 线预排未传英文手速 latin_per_char_sec"
    assert "total_budget_sec" in call, "A 线预排未传总预算 total_budget_sec"


def test_bline_plans_gaps_with_budget_and_latin():
    src = _src(_BLINE)
    assert "plan_bubble_gaps" in src, "B 线丢失条间隔预排（plan_bubble_gaps）"
    call = _call_window(src, "_pbg(")
    assert "latin_per_char_sec" in call, "B 线预排未传英文手速"
    assert "total_budget_sec" in call, "B 线预排未传总预算"


def test_manual_route_plans_gaps_with_budget_and_latin():
    src = _src(_MANUAL)
    assert "plan_bubble_gaps" in src, "手动路径丢失条间隔预排（plan_bubble_gaps）"
    seg = src[src.index("def _deliver_bubble_parts"):]
    body = seg[: seg.index("\nasync def ") if "\nasync def " in seg[10:] else len(seg)]
    assert "latin_per_char_sec" in body, "手动路径预排未传英文手速"
    assert "total_budget_sec" in body, "手动路径预排未传总预算"


def test_no_chain_calls_per_part_inter_delay_directly():
    """inter_part_delay_sec 是 plan_bubble_gaps 的内部积木；消费链直调=绕过
    总预算。三链源码不得再出现直调（纯函数层/测试自身除外）。"""
    for p, name in ((_ALINE, "A线"), (_BLINE, "B线"), (_MANUAL, "手动")):
        assert "inter_part_delay_sec" not in _src(p), (
            f"{name} 仍在逐条直调 inter_part_delay_sec（绕过 total_budget_sec）")


# ── 2. 条间打字气泡时长带英文手速 ────────────────────────────────────────────

def test_typing_lead_carries_latin_speed_on_all_chains():
    # 各链的调用点别名不同（import as），按调用 token 定位而非裸函数名
    # （裸名只会命中 import 行，首版踩过）。
    for p, name, token in (
        (_ALINE, "A线", "_etl_bub("),
        (_BLINE, "B线", "_etl("),
        (_MANUAL, "手动", "estimate_typing_lead("),
    ):
        window = _call_window(_src(p), token, size=500)
        assert "latin_per_char_sec" in window, (
            f"{name} 的条间打字时长（estimate_typing_lead）未带 latin_per_char_sec")


# ── 3. holdout 抽中必须折叠成单段 ────────────────────────────────────────────

def test_aline_holdout_collapses_paragraphs():
    src = _src(_ALINE)
    assert "holdout_pct" in src, "A 线缺 holdout 保留组（恒定拆条=可识别模式）"
    # 抽中分支必须折叠（collapse_paragraphs 的别名 _clp_bub 被调用）
    seg = src[src.index("holdout_pct"):]
    assert "_clp_bub(" in seg[:2000], (
        "A 线保留组抽中未折叠成单段——多行原样单条发出＝2026-08-08 被投诉形态")


def test_bline_holdout_collapses_paragraphs():
    src = _src(_BLINE)
    assert "holdout_pct" in src
    seg = src[src.index("_holdout_parts:"):]
    assert "_cp(" in seg[:1600], (
        "B 线保留组抽中未折叠成单段（collapse_paragraphs）")


# ── 4. 手动路径观测入列 ─────────────────────────────────────────────────────

def test_manual_route_records_bubble_gap():
    src = _src(_MANUAL)
    assert 'record_bubble_gap' in src and '"manual"' in src, (
        "手动分条路径未记 record_bubble_gap(\"manual\", ...)——"
        "设置页「实测节奏」看不到手动链分布")


# ── 5. P1：RPA 分条收编 reply_split 单一事实源（2026-08-09） ────────────────

_HP = ROOT / "src" / "integrations" / "line_rpa" / "human_pacing.py"
_LINE_RUNNER = ROOT / "src" / "integrations" / "line_rpa" / "runner.py"
_WA_RUNNER = ROOT / "src" / "integrations" / "whatsapp_rpa" / "runner.py"
_MSGR_RUNNER = ROOT / "src" / "integrations" / "messenger_rpa" / "runner.py"


def _hp_cfg(**kw):
    from src.integrations.line_rpa.human_pacing import PacingConfig
    return PacingConfig.from_dict(kw)


def test_rpa_sentence_split_no_mid_word_cut():
    """英文长句（无句读）不得再被按 max_chars 拦腰硬切（legacy 实锤 bug）。"""
    from src.integrations.line_rpa.human_pacing import split_message
    text = ("this is one very long english sentence without any punctuation "
            "that keeps going and going and would previously get chopped "
            "right in the middle of a word by the legacy splitter")
    parts = split_message(text, _hp_cfg(split_mode="sentence",
                                        split_max_chars=80, split_max_parts=3))
    assert parts == [text], f"无句界的英文长句应整条保留，实得 {parts!r}"


def test_rpa_sentence_split_ends_at_boundaries():
    from src.integrations.line_rpa.human_pacing import split_message
    text = ("你好呀今天过得怎么样。我这边刚忙完手头的事情呢。"
            "晚上想不想一起吃个饭。随便找个地方坐坐聊聊天。")
    parts = split_message(text, _hp_cfg(split_mode="sentence",
                                        split_max_chars=20, split_max_parts=3))
    assert len(parts) >= 2, "CJK 多句超预算应拆条"
    for p in parts[:-1]:
        assert p[-1] in "。！？；…", f"切点必须在句末标点：{p!r}"
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_rpa_url_never_broken():
    from src.integrations.line_rpa.human_pacing import split_message
    url = "https://example.com/very/long/path/keeps/going/and/going/on"
    text = f"看下这个链接 {url} 然后告诉我你的想法哈。剩下的我们再商量。"
    parts = split_message(text, _hp_cfg(split_mode="sentence",
                                        split_max_chars=20, split_max_parts=3))
    joined = "".join(parts)
    assert url in joined.replace("\n", ""), "URL 不得被切断（可点性）"


def test_rpa_disabled_or_none_collapses_multiline():
    """不拆条的出口（pacing 关 / none 模式）多行必须折叠成单段——
    多行原样单条发出＝2026-08-08 被投诉形态。"""
    from src.integrations.line_rpa.human_pacing import split_message
    for cfg in (_hp_cfg(enabled=False), _hp_cfg(split_mode="none")):
        parts = split_message("今天好累\n想你了", cfg)
        assert len(parts) == 1
        assert "\n" not in parts[0], f"多行未折叠：{parts!r}"
        assert "今天好累" in parts[0] and "想你了" in parts[0]


def test_rpa_sentence_split_newline_contract():
    """多行合同（每行一句）在 RPA sentence 模式按行成条（与 orch 链同语义）。"""
    from src.integrations.line_rpa.human_pacing import split_message
    parts = split_message("哈哈真的假的\n我还以为你忘了呢",
                          _hp_cfg(split_mode="sentence"))
    assert parts == ["哈哈真的假的", "我还以为你忘了呢"]


def test_rpa_parts_never_contain_newline():
    """合同最多 5 行 × RPA max_parts 3 → 尾条并行以 \\n 连接的产物必须条内折叠
    （每个分条＝一条独立消息，条内换行注入设备＝又一条「消息带换行」）。"""
    from src.integrations.line_rpa.human_pacing import split_message
    text = ("今天路过那家咖啡店买了杯燕麦拿铁味道特别好。\n"
            "顺便帮你也看了下你说的那款蛋糕还有货。\n"
            "下午的会议临时取消了所以提前回家了。\n"
            "路上看到夕阳特别美拍了好几张照片。\n"
            "晚点发给你看看你肯定喜欢。")
    parts = split_message(text, _hp_cfg(split_mode="sentence",
                                        split_max_chars=120,
                                        split_max_parts=3))
    assert 2 <= len(parts) <= 3
    for p in parts:
        assert "\n" not in p, f"分条内残留换行：{p!r}"


def test_rpa_delegation_fallback_to_legacy(monkeypatch):
    """reply_split 委托异常 → 回落 legacy 切分，绝不空手/抛错。"""
    import src.inbox.reply_split as rs
    from src.integrations.line_rpa.human_pacing import split_message

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(rs, "split_reply_parts", _boom)
    text = "你好。今天天气不错。晚点再聊。"
    parts = split_message(text, _hp_cfg(split_mode="sentence",
                                        split_max_chars=20, split_max_parts=3))
    assert parts and "".join(parts).replace(" ", "") == text


def test_rpa_runners_record_bubble_gap():
    assert '_rbg_rpa("rpa", "line"' in _src(_LINE_RUNNER), (
        "LINE runner 条间隔未采样进 bubble_gap 观测")
    assert '_rbg_rpa("rpa", "whatsapp"' in _src(_WA_RUNNER), (
        "WA runner 条间隔未采样进 bubble_gap 观测")


def test_messenger_send_collapses_multiline():
    src = _src(_MSGR_RUNNER)
    assert "_clp_mr" in src and "collapse_paragraphs" in src, (
        "messenger _send_reply 未折叠多行——该路径整段注入+单次发送，"
        "bubbles 开启后多行合同会以「一条消息带换行」形态泄漏")
