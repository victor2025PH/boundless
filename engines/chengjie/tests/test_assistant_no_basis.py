# -*- coding: utf-8 -*-
"""小智「没依据」哨兵门禁（实施74 P4，2026-08-27）。

## 背景

提示词里一直写着「参考条目覆盖不到时诚实说明」，但那句话**只对用户生效**：
路由无从判断 LLM 究竟是答了还是拒了，于是

  * `qa_log` 恒记 `answered=True` → 自答率虚高、ops「未答清单」看不见缺口；
  * `report_hint` 不亮 → 用户在最该有出路的时刻反而没有出路。

## 为什么是哨兵，不是「答前先判一次」

先做过可行性测量，两条路都否了：

  * **嵌入余弦分不开**（2026-08-27 实测）：正样本 query↔命中条目余弦最低
    0.455，而最危险的「产品形状但语料没覆盖」类负样本是 0.56~0.61，**全部落在
    正样本区间内**。余弦量的是话题相关性，不是「这段文档能否回答这个问题」。
  * **再调一次 LLM 当裁判**要多一个往返，给每条问答加延迟。

而**正在答题的那个 LLM 本来就同时看着问题和参考条目**——它就是最合适的裁判，
让它自己声明 `NO_BASIS` 等于零额外成本。

## 本门禁守的三条

1. **命中哨兵时用户一个字都看不到**（首段缓冲；先吐半句再撤回比不撤更糟）；
2. **`answered=False` 要记真话**（否则自答率继续骗人）；
3. **fail-open**：哨兵出现在正文中间不算数，正常回答绝不被误伤。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.web.routes.assistant_routes import _is_no_basis

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "src" / "web" / "routes" / "assistant_routes.py"
BALL = ROOT / "shared" / "assistant" / "assistant-ball.js"


# ── 1. 判定函数本身 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "NO_BASIS",
    "NO_BASIS\n",
    "  NO_BASIS  ",
    "no_basis",                      # 大小写不敏感
    "NO_BASIS 参考条目里没有相关内容",
])
def test_sentinel_detected(text):
    assert _is_no_basis(text) is True


@pytest.mark.parametrize("text", [
    "",
    "在「用量与额度」页可以看到本月消耗。[S1]",
    # 关键：哨兵出现在**正文中间**不算数——否则一条正常回答里只要提到这个词
    # 就会被整段吞掉（用户什么也看不到，比答错更糟）
    "你可以这样做：如果系统返回 NO_BASIS 说明没有依据。",
    "no basis",                      # 少了下划线，不是哨兵
    None,
])
def test_sentinel_not_falsely_triggered(text):
    assert _is_no_basis(text) is False


# ── 2. 流式门闸：命中哨兵必须零输出 ─────────────────────────────────────────

def _simulate_stream(pieces, sentinel="NO_BASIS"):
    """复刻路由里的首段缓冲门闸逻辑，返回 (吐给用户的文本, 完整答案)。

    刻意复刻而不是导入：那段逻辑长在一个巨大的 async 生成器里，端到端跑要拉起
    整个 app + 假 LLM。这里守的是**算法不变量**；接线由
    `test_route_wiring_uses_the_gate` 用静态断言兜住。
    """
    answer = ""
    emitted = []
    head_buf = ""
    gate_open = False
    for piece in pieces:
        answer += piece
        if gate_open:
            emitted.append(piece)
            continue
        head_buf += piece
        if len(head_buf.lstrip()) < len(sentinel):
            continue
        if head_buf.lstrip().upper().startswith(sentinel):
            break
        gate_open = True
        emitted.append(head_buf)
    if not gate_open and head_buf and not head_buf.lstrip().upper().startswith(sentinel):
        emitted.append(head_buf)
    return "".join(emitted), answer


def test_stream_emits_nothing_when_sentinel():
    """逐 token 到达时也必须一个字都不吐。"""
    out, answer = _simulate_stream(["NO", "_BA", "SIS", "\n没有依据"])
    assert out == "", f"命中哨兵却吐了内容：{out!r}"
    assert answer.startswith("NO_BASIS")


def test_stream_emits_everything_when_normal():
    """正常回答一个字都不能少（缓冲不得吞掉开头）。"""
    pieces = ["在「用量", "与额度」", "页可以看到", "本月消耗。[S1]"]
    out, answer = _simulate_stream(pieces)
    assert out == answer == "".join(pieces)


def test_stream_handles_answer_shorter_than_sentinel():
    """回答比哨兵还短时不能把它吞掉（首版最容易漏的边界）。"""
    out, answer = _simulate_stream(["好的"])
    assert out == answer == "好的"


def test_stream_sentinel_midway_is_not_swallowed():
    """哨兵词出现在正文中间 → 正常输出，不误伤。"""
    pieces = ["这个功能在设置页；", "若返回 NO_BASIS 表示没查到。"]
    out, _ = _simulate_stream(pieces)
    assert out == "".join(pieces)


# ── 3. 接线（静态）─────────────────────────────────────────────────────────

def test_route_wiring_uses_the_gate():
    src = ROUTES.read_text(encoding="utf-8")
    assert "_NO_BASIS = \"NO_BASIS\"" in src, "哨兵常量丢了"
    assert "def _is_no_basis" in src, "判定函数丢了"
    assert "head_buf" in src and "gate_open" in src, "流式首段缓冲门闸丢了"
    # 非流式回落路径也必须先判后吐
    assert "if not _is_no_basis(answer):" in src, (
        "非流式回落路径未做哨兵判定——那条路会把 NO_BASIS 原样吐给用户"
    )


def test_prompt_teaches_the_sentinel_in_both_languages():
    """中英两套提示词都要教 LLM 这个约定，否则对应语种全程不生效。"""
    src = ROUTES.read_text(encoding="utf-8")
    zh = src.split("你是本客服系统的产品内置帮助助手", 1)
    en = src.split("You are the in-product help assistant", 1)
    assert len(zh) == 2 and len(en) == 2, "找不到中/英提示词块"
    for name, blob in (("zh", zh[1][:1500]), ("en", en[1][:1500])):
        assert "_NO_BASIS" in blob or "NO_BASIS" in blob, (
            f"{name} 提示词没有教 NO_BASIS 约定"
        )


def test_no_basis_records_answered_false():
    """自答率必须记真话——这是 ops「未答清单」能看见缺口的前提。"""
    src = ROUTES.read_text(encoding="utf-8")
    block = src.split("if _is_no_basis(answer):", 1)
    assert len(block) == 2, "找不到哨兵处置分支"
    seg = block[1][:900]
    assert "answered=False" in seg, "哨兵分支仍记 answered=True，自答率会继续骗人"
    assert "record_query(answered=False" in seg, "stats 未同步记未答"
    assert "asb.a.no_hit" in seg, "未给用户诚实说明（复用零命中同一文案）"


def test_client_shows_report_entry_on_unanswered():
    """答不上来时前端必须给报障入口——只看 meta.report_hint 会漏掉这一刻。"""
    js = BALL.read_text(encoding="utf-8")
    assert re.search(r"done\.answered\s*===\s*false", js), (
        "assistant-ball.js 未按 done.answered===false 补报障入口"
    )
    assert "noBasis" in js and "to-report" in js


# ── 4. 端到端：真跑一遍路由 ─────────────────────────────────────────────────
# 复用 test_assistant_routes 的夹具（单一事实源，别再造第二套 app 装配）。

def test_end_to_end_sentinel_never_leaks_to_user(monkeypatch, tmp_path):
    """假 LLM 返回 `NO_BASIS` → 用户看到的是诚实说明，且**绝不出现哨兵**。

    这条比模拟流更硬：真的过一遍路由（非流式回落路径），验证
    「判定 → 不吐原文 → 换诚实文案 → answered=False」整条链。
    """
    from tests.test_assistant_routes import (
        _FakeAI, _client, _mk_app, _stream_events,
    )

    app, ai, log, *_ = _mk_app(monkeypatch, tmp_path,
                               fake_ai=_FakeAI(answer="NO_BASIS"))
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "怎么发语音", "page": "/workspace"})
    assert r.status_code == 200
    evs = _stream_events(r)
    kinds = [e["ev"] for e in evs]
    assert kinds == ["meta", "delta", "done"], kinds

    body = evs[1].get("text") or ""
    assert "NO_BASIS" not in body.upper(), f"哨兵泄露给用户了：{body!r}"
    assert body.strip(), "拒答时也必须给用户一句话，不能空白"
    assert evs[2]["answered"] is False, "哨兵命中却记成答上了"


def test_end_to_end_records_refusal_kind_and_health_surfaces_it(
        monkeypatch, tmp_path):
    """端到端：哨兵拒答后，健康端点能看见「是哨兵拒的」而不只是「没答上」。

    这是本机制**能不能被观测**的验收：哨兵依赖模型行为（LLM 得肯输出
    NO_BASIS），不单独计数就无从判断它到底在不在工作。
    """
    from tests.test_assistant_routes import _FakeAI, _client, _mk_app

    app, *_ = _mk_app(monkeypatch, tmp_path, fake_ai=_FakeAI(answer="NO_BASIS"))
    c = _client(app)
    c.post("/api/assistant/query", json={"q": "怎么发语音", "page": "/workspace"})

    h = c.get("/api/assistant/health").json()
    proc = h["process"]
    assert proc["miss_no_basis"] == 1, f"进程计数没记到哨兵拒答：{proc}"
    assert proc["miss_no_hit"] == 0
    qa = h["qa_7d"]
    assert qa["miss_no_basis"] == 1, f"持久口径没记到哨兵拒答：{qa}"
    assert qa["no_basis_share"] == 1.0


def test_end_to_end_zero_hit_counts_as_no_hit(monkeypatch, tmp_path):
    """检索零命中要记成 no_hit（补语料），别和哨兵混成一个数。

    两者处置完全不同：no_hit 是语料缺条目，no_basis 是条目沾边但答不了。
    """
    from tests.test_assistant_routes import _client, _mk_app

    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    # 纯拉丁乱词 → BM25 零命中（与 test_assistant_core 同款探针）
    c.post("/api/assistant/query",
           json={"q": "qqxyzzy foobar zzzz", "page": "/workspace"})

    h = c.get("/api/assistant/health").json()
    assert h["process"]["miss_no_hit"] == 1, h["process"]
    assert h["process"]["miss_no_basis"] == 0
    assert h["qa_7d"]["miss_no_hit"] == 1
    assert h["qa_7d"]["no_basis_share"] == 0.0


def test_qa_log_infers_kind_from_top_score_without_schema_change(tmp_path):
    """分型靠既有 top_score 列推断——**零改表**，且对历史数据同样成立。

    判据：`answered=0 且 top_score>0` = 检索命中但 LLM 拒答（哨兵）；
    `answered=0 且 top_score<=0` = 检索零命中。这条不变量一旦被改（比如
    有人给零命中分支补传 top_score），历史账会被重新解释成另一个含义。
    """
    from src.assistant.qa_log import AssistantQALog

    log = AssistantQALog(tmp_path / "qa.db")
    common = dict(user_id="u", role="admin", page="/p")
    log.record(q="a", answered=True, top_score=88.0, **common)
    log.record(q="b", answered=False, top_score=0.0, **common)     # 零命中
    log.record(q="c", answered=False, top_score=61.5, **common)    # 哨兵
    log.record(q="d", answered=False, top_score=42.0, **common)    # 哨兵

    s = log.stats(days=7)
    assert s["n"] == 4 and s["answered"] == 1
    assert s["miss"] == 3
    assert s["miss_no_hit"] == 1
    assert s["miss_no_basis"] == 2
    assert s["no_basis_share"] == round(2 / 3, 3)


def test_stats_refusal_kind_is_backward_compatible():
    """旧调用方不传 refusal 也不能炸，且只进总数不污染分型。"""
    from src.assistant.stats import AssistantStats

    st = AssistantStats()
    st.record_query(answered=False)                      # 旧签名
    st.record_query(answered=False, refusal="no_hit")
    st.record_query(answered=False, refusal="no_basis")
    st.record_query(answered=True)
    d = st.dump()
    assert d["miss"] == 3, "总数必须包含未分型的那条"
    assert d["miss_no_hit"] == 1 and d["miss_no_basis"] == 1
    assert d["answered"] == 1
    prom = st.dump_prom()
    assert "assistant_miss_no_basis_total 1" in prom
    assert "assistant_miss_no_hit_total 1" in prom


def test_route_tags_both_refusal_sites():
    """两个拒答点必须各自标明类型——漏一个就把两种成因混成一个数。"""
    src = ROUTES.read_text(encoding="utf-8")
    assert 'refusal="no_hit"' in src, "零命中分支未标 refusal 类型"
    assert 'refusal="no_basis"' in src, "哨兵分支未标 refusal 类型"


# ── 5. 拒答分型的可观测性（实施74 P5）──────────────────────────────────────
# 哨兵是**依赖模型行为**的机制（LLM 得肯输出约定词）。它不工作时不会报错，
# 只会退回「把沾边条目硬凑成答案」——在别处完全看不出来。所以「哨兵触发了
# 多少次」必须可观测，否则整个机制是黑箱。

def test_stats_split_is_derived_from_top_score(tmp_path):
    """分型语义：`answered=0 且 top_score>0` = 哨兵拒答，`<=0` = 零命中。

    这是**零迁移**方案（不加列，靠既有 top_score 推导），代价是语义隐含在
    两个 record() 调用点的参数差异里——本用例把它变成显式契约。
    """
    from src.assistant.qa_log import AssistantQALog

    log = AssistantQALog(tmp_path / "qa.db")
    log.record(user_id="u", role="admin", page="/p", q="答得上的",
               answered=True, top_score=88.0, sources="howto:x")
    # 零命中：不传 top_score/sources（与路由零命中分支一致）
    log.record(user_id="u", role="admin", page="/p", q="零命中的", answered=False)
    # 哨兵：命中了但 LLM 自认答不了 → 必带 top_score
    log.record(user_id="u", role="admin", page="/p", q="没依据的",
               answered=False, top_score=96.4, sources="term:btn_export")

    s = log.stats(days=7)
    assert s["n"] == 3 and s["answered"] == 1
    assert s["miss"] == 2
    assert s["miss_no_hit"] == 1, "零命中未被正确归类"
    assert s["miss_no_basis"] == 1, "哨兵拒答未被正确归类"
    assert s["no_basis_share"] == 0.5


def test_zero_hit_branch_must_not_pass_top_score():
    """路由的零命中分支**不得**传 top_score——传了分型就静默反转。

    这是零迁移方案唯一的脆弱点：两个 record() 调用点的参数差异就是全部语义，
    而写错不会报错、不会变红，只会让看板上的数字互换。
    """
    src = ROUTES.read_text(encoding="utf-8")
    head, sep, tail = src.partition("if not strong:")
    assert sep, "找不到零命中分支"
    branch = tail[:600]
    assert "answered=False" in branch, "零命中分支语义变了"
    assert "top_score" not in branch, (
        "零命中分支传了 top_score——它会被算成「哨兵拒答」，"
        "看板上零命中与没依据两个数会对调（且不会有任何报错）"
    )


def test_sentinel_branch_must_pass_top_score():
    src = ROUTES.read_text(encoding="utf-8")
    head, sep, tail = src.partition("if _is_no_basis(answer):")
    assert sep, "找不到哨兵分支"
    branch = tail[:600]
    assert "answered=False" in branch and "top_score=" in branch, (
        "哨兵分支未带 top_score——它会被算成「零命中」，运营会去补根本不缺的语料"
    )


def test_ops_card_surfaces_the_breakdown():
    """分型必须真的画到 ops 卡上——算出来没人看等于没做。

    额外钉住「哨兵零触发亮黄」：那是本行存在的**主要理由**（有拒答但哨兵
    一次没响 = 机制没在工作），不能被简化掉。
    """
    tpl = (ROOT / "src" / "web" / "templates" / "ops_overview.html").read_text(
        encoding="utf-8")
    for key in ("ov2_as_refuse", "ov2_as_refuse_hit", "ov2_as_refuse_basis",
                "ov2_as_refuse_silent", "ov2_as_refuse_tip"):
        assert key in tpl, f"ops 卡未消费 {key}"
    assert "miss_no_basis" in tpl and "miss_no_hit" in tpl, "ops 卡未读分型字段"
    assert "opsSetCardLight('assist', 'yellow')" in tpl, "哨兵零触发未亮黄"


def test_refusal_i18n_keys_are_bilingual():
    from src.web.i18n_packs.assistant_ball import EN, ZH

    for key in ("ov2_as_refuse", "ov2_as_refuse_hit", "ov2_as_refuse_basis",
                "ov2_as_refuse_tip", "ov2_as_refuse_none",
                "ov2_as_refuse_silent"):
        assert ZH.get(key), f"{key} 缺中文"
        assert EN.get(key), f"{key} 缺英文"


def test_end_to_end_normal_answer_unaffected(monkeypatch, tmp_path):
    """正常回答一字不改地到达用户（哨兵机制绝不能误伤主路径）。"""
    from tests.test_assistant_routes import (
        _FakeAI, _client, _mk_app, _stream_events,
    )

    normal = "在坐席工作台右栏「语音」组件生成后发送。[S1]"
    app, *_ = _mk_app(monkeypatch, tmp_path, fake_ai=_FakeAI(answer=normal))
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "怎么发语音", "page": "/workspace"})
    evs = _stream_events(r)
    assert evs[1]["text"] == normal
    assert evs[2]["answered"] is True
