"""群脉 CrowdX 导演控制台门禁（路由 + 页面 + 纯函数序列化）。

覆盖：
- 四个 API 各自 200 与结构契约（剧本库/逐拍/排练/历史）；
- 排练 API 用**占位生成器**真跑出 lines（默认档，秒出、可复现、CI 可跑）；
- 非法 playbook_id → 404 且 detail 走 ``tr``（EN 会话拿到英文，不是硬编码中文）；
- 页面 200 且渲染出导播台关键元素。

app 走 conftest 的整机 fixture（``auth_client``）——本路由的页面依赖 base.html 外壳
与 nav_schema 单源，假 Ctx 起不来模板层；API 侧顺带验真实鉴权链路。
剧本目录取仓库自带 ``config/playbooks``（路由的回落路径），场次库随 tmp config 目录走，
不碰生产库。
"""
from __future__ import annotations

import pytest

from src.web.routes.group_show_routes import (
    demo_actors,
    parse_human,
    playbook_dir,
    split_problems,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _isolated_show_store():
    """场次库单例按测试清零——排练会落档，绝不能写进生产 config/group_show.db。"""
    from src.companion.group_show.store import reset_group_show_store

    reset_group_show_store()
    yield
    reset_group_show_store()


@pytest.fixture()
def _books():
    """仓库自带剧本库（没有剧本就没什么可测的，直接跳过而不是假绿）。"""
    from src.companion.group_show.playbook import load_playbook_dir

    books = load_playbook_dir(playbook_dir(None))
    if not books:
        pytest.skip("config/playbooks 为空，跳过导播台端到端用例")
    return books


# ── 纯函数（入参解析 / 校验分流） ────────────────────────────────────────────

def test_split_problems_separates_hard_and_warn():
    hard, warn = split_problems(["beat[b1] 缺 intent", "warn: 缺 skeptic 角"])
    assert hard == ["beat[b1] 缺 intent"]
    assert warn == ["缺 skeptic 角"]


def test_demo_actors_bounded_and_independent_fingerprints():
    actors = demo_actors(99)          # 上限截断，防几十个号拖慢页面
    assert len(actors) == 12
    assert len({a["fingerprint_group"] for a in actors}) == 12
    assert demo_actors("oops")[0]["account_id"] == "demo1"   # 坏入参回落默认 4 人
    assert len(demo_actors("oops")) == 4


def test_parse_human_accepts_cli_string_and_struct_and_skips_junk():
    parsed = parse_human([
        "2:老王:这个会不会封号啊",
        {"after": 5, "who": "小李", "text": "多少钱"},
        "格式不对",            # 静默跳过：一行手滑不该让整场排练 400
        "3:只有两段",
    ])
    assert parsed == [(2, "老王", "这个会不会封号啊"), (5, "小李", "多少钱")]
    # textarea 直接整块传也吃（前端就是这么发的）
    assert parse_human("1:A:hi\n\n2:B:yo") == [(1, "A", "hi"), (2, "B", "yo")]


# ── API：剧本库 / 逐拍详情 ──────────────────────────────────────────────────

def test_api_playbooks_lists_with_validation(auth_client, _books):
    r = auth_client.get("/api/group-show/playbooks")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["count"] == len(_books)
    row = d["playbooks"][0]
    assert {"id", "name", "system", "beat_count", "soft_ad_level",
            "errors", "warnings", "valid"} <= set(row)
    assert row["valid"] is (not row["errors"])
    assert all(not w.startswith("warn:") for w in row["warnings"])   # 前缀已剥


def test_api_playbook_detail_returns_beat_sheet(auth_client, _books):
    pid = sorted(_books)[0]
    r = auth_client.get(f"/api/group-show/playbooks/{pid}")
    assert r.status_code == 200
    pb = r.json()["playbook"]
    assert pb["id"] == pid and pb["beats"]
    beat = pb["beats"][0]
    assert {"id", "role", "intent", "product", "soft", "pace"} <= set(beat)
    assert isinstance(beat["soft"], int)      # 已折算成本拍生效值，前端不再算


def test_api_playbook_detail_unknown_id_is_4xx_localized(auth_client):
    """非法 id → 404，且 detail 走 tr（EN 会话拿英文文案，不是硬编码中文）。"""
    from src.web.web_i18n import get_translations

    r = auth_client.get("/api/group-show/playbooks/no_such_playbook")
    assert r.status_code == 404
    zh_detail = r.json()["detail"]
    assert "no_such_playbook" in zh_detail

    r_en = auth_client.get("/api/group-show/playbooks/no_such_playbook?lang=en")
    en_detail = r_en.json()["detail"]
    assert r_en.status_code == 404
    assert en_detail == get_translations("en")["err.gs.playbook_not_found"].format(
        pid="no_such_playbook")
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in en_detail)


# ── API：排练（占位生成器） ─────────────────────────────────────────────────

def test_api_rehearse_with_stub_generator_produces_lines(auth_client, _books):
    pid = sorted(_books)[0]
    r = auth_client.post("/api/group-show/rehearse", json={
        "playbook_id": pid, "actors": 4, "seed": 7,
        "human": ["2:老王:这个会不会封号啊"],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["generator"] == "stub"   # 默认档不碰 LLM
    assert d["dry_run"] is True
    assert d["line_count"] > 0 and d["lines"]
    line = d["lines"][0]
    assert {"seq", "at_seconds", "display_name", "role", "beat_id",
            "kind", "text"} <= set(line)
    assert line["text"]
    # 真人插话进流（另一种 kind，前端据此换样式）
    assert any(ln["kind"] == "human" for ln in d["lines"])
    # 自然度面板三轴 + verdict
    nat = d["naturalness"]
    assert nat["verdict"] in ("natural", "robotic", "chaotic", "insufficient")
    assert {"score", "entropy", "interval_cv", "balance", "issues"} <= set(nat)
    assert d["casting"]["members"]


def test_api_rehearse_same_seed_is_reproducible(auth_client, _books):
    """同 seed 同剧本 → 同节奏（改完剧本用同一 seed 跑，差异只来自剧本本身）。"""
    pid = sorted(_books)[0]
    body = {"playbook_id": pid, "actors": 4, "seed": 42}
    a = auth_client.post("/api/group-show/rehearse", json=body).json()
    b = auth_client.post("/api/group-show/rehearse", json=body).json()
    assert [ln["at_seconds"] for ln in a["lines"]] == \
           [ln["at_seconds"] for ln in b["lines"]]
    assert a["session_id"] != b["session_id"]     # 每场独立 id


def test_api_rehearse_requires_playbook_id(auth_client):
    r = auth_client.post("/api/group-show/rehearse", json={})
    assert r.status_code == 400
    r2 = auth_client.post("/api/group-show/rehearse",
                          json={"playbook_id": "nope"})
    assert r2.status_code == 404


# ── API：历史场次 ───────────────────────────────────────────────────────────

def test_api_sessions_lists_recent_rehearsals(auth_client, _books):
    pid = sorted(_books)[0]
    run = auth_client.post("/api/group-show/rehearse",
                           json={"playbook_id": pid, "seed": 3}).json()
    r = auth_client.get("/api/group-show/sessions?limit=5")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    if not d.get("available"):
        pytest.skip("场次库不可用（只读介质），历史卡走空态分支")
    ids = [s["session_id"] for s in d["sessions"]]
    assert run["session_id"] in ids
    row = d["sessions"][0]
    assert {"session_id", "playbook_id", "group_key", "status",
            "beat_cursor", "started_at"} <= set(row)
    assert "casting" not in row      # 对象视图不进 JSON（只回 cast 字典）


# ── API：关联风险体检 ───────────────────────────────────────────────────────

def test_api_linkage_reports_real_account_seat_cap(auth_client):
    """体检接口必须回「真号最多能上几个」，且不外泄账号明细。

    这是导播台唯一说真话的地方：排练用的是占位号，永远演得热热闹闹；运营据此以为
    「可以真发了」，而真发时同出口的号只能上一个。座位数不出来，这张卡就白做了。
    """
    r = auth_client.get("/api/group-show/linkage")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert {"total", "max_concurrent", "by_source", "problems", "advice"} <= set(d)
    assert isinstance(d["max_concurrent"], int) and d["max_concurrent"] >= 0
    assert d["max_concurrent"] <= d["total"]     # 座位数不可能多于号数
    # groups 是 {组名: [account_id...]} 的全表，页面用不上，不该经接口散出去
    assert "groups" not in d


def test_api_linkage_never_500s_without_a_registry(auth_client, monkeypatch):
    """注册表缺失/损坏一律走空态，不能把导播台整页打崩。

    体检是辅助信息，它挂了不该连累主功能——运营还得靠这个页面排练。
    """
    import src.web.routes.group_show_routes as mod

    monkeypatch.setattr(mod, "_online_accounts",
                        lambda _cm: (_ for _ in ()).throw(RuntimeError("db gone")))
    r = auth_client.get("/api/group-show/linkage")
    # helper 自己抛也得被 linkage 层兜住；真抛穿了这里会是 500
    assert r.status_code in (200, 500)
    if r.status_code == 500:
        pytest.fail("体检接口把异常抛穿了，导播台会整页红")


def test_linkage_advice_is_actionable_when_all_on_one_host():
    """全部号挤在同一出口时，报告不能只说「演不了」，必须给出怎么才能演。

    只报问题不给解法，运营下一步动作是「来问开发」——这条闭不上环，功能就等于没做。
    """
    from src.companion.group_show.linkage import linkage_readiness

    rows = [{"account_id": f"a{i}", "platform": "telegram"} for i in range(4)]
    rep = linkage_readiness(rows)
    assert rep["max_concurrent"] == 1
    assert rep["advice"], "全挤一个出口却没给任何解法"
    joined = " ".join(rep["advice"])
    assert "真机" in joined or "代理" in joined


# ── 页面 ────────────────────────────────────────────────────────────────────

# ── API：出席矩阵 ───────────────────────────────────────────────────────────

def test_api_attendance_answers_how_many_groups_the_pool_can_cover(auth_client):
    """不给群清单时也要能出「现有号池能安全铺几个群」——这是运营的第一个问题。"""
    r = auth_client.post("/api/group-show/attendance", json={"seats": 3})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["plan"] is None
    cap = d["capacity"]
    assert {"pool_size", "seats", "max_safe_groups"} <= set(cap)
    assert cap["seats"] == 3
    assert isinstance(cap["max_safe_groups"], int)


def test_api_attendance_plans_a_roster_with_metrics_and_roles(auth_client):
    r = auth_client.post("/api/group-show/attendance",
                         json={"group_count": 12, "seats": 3})
    assert r.status_code == 200
    plan = r.json()["plan"]
    if plan is None:
        pytest.skip("本机注册表没有在线号，排班走空态分支")
    assert len(plan["assignments"]) == 12
    assert {"max_pair_co", "clique_ratio", "verdict"} <= set(plan["metrics"])
    # 卡片的「单号进群数」读总敞口（含历史群）而非本批派了几个——少了这个字段，
    # 页面会回落成只报本批，把「已经挂了 32 个老群」的号显示成 1~2。
    assert "total_groups_per_account" in plan["metrics"]
    # 角色随排班一起给：出席矩阵管不到「A 在每个群都是那个夸产品的」这条内容指纹
    assert plan["roles"]["casting"]
    assert plan["total_joins"] == sum(len(v) for v in plan["joins"].values())
    # 排期一并给出：静态表最自然的执行方式是「今天全加完」，那正是要防的
    sched = plan["schedule"]
    assert sched["day_count"] >= 3        # 每群每天只进一个号 ⇒ 3 座位 ≥3 天
    assert all(len({t["group"] for t in day}) == len(day)
               for day in sched["days"]), "同一天同一个群进了两个号"


def test_api_attendance_accepts_a_pasted_group_list(auth_client):
    """群清单多半是从别处复制粘贴来的，逼运营转成 JSON 数组没有意义。"""
    r = auth_client.post("/api/group-show/attendance",
                         json={"groups": "群A\n群B\n群A\n\n群C", "seats": 2})
    assert r.status_code == 200
    plan = r.json()["plan"]
    if plan is None:
        pytest.skip("本机注册表没有在线号")
    assert list(plan["assignments"]) == ["群A", "群B", "群C"]   # 去重且保序


def test_api_attendance_never_500s_on_garbage(auth_client):
    for body in ({}, {"seats": "x"}, {"group_count": -5},
                 {"groups": 42}, {"existing": "nope"}):
        r = auth_client.post("/api/group-show/attendance", json=body)
        assert r.status_code == 200, f"脏入参把接口打挂了: {body}"


def test_api_attendance_caps_the_group_count(auth_client):
    """排班是 O(群×号×席位) 的贪心，几千个群会把请求拖死。"""
    from src.web.routes.group_show_routes import MAX_PLAN_GROUPS, group_list

    assert len(group_list({"group_count": 99999})) == MAX_PLAN_GROUPS
    assert len(group_list({"groups": [f"g{i}" for i in range(9999)]})) \
        == MAX_PLAN_GROUPS


# ── 演出矩阵端点 ────────────────────────────────────────────────────────────


def test_api_exposure_reports_both_axes_and_a_speaker_budget(auth_client):
    """双读数缺一不可：只报成员面运营会放弃，只报演出面他会继续加群。"""
    r = auth_client.get("/api/group-show/exposure")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    if not d.get("available"):
        pytest.skip("本机场次库不可用，端点走空态分支")
    report = d["report"]
    assert {"membership", "performance", "verdict", "budget", "advice"} <= set(report)
    assert "max_pair_co" in report["membership"]
    assert "max_pair_co" in report["performance"]
    # 预算是这张卡最该被看见的产出——开口人数是二次杠杆，不给数字等于没给建议
    assert "max_speakers" in report["budget"]
    assert report["budget"]["options"]
    assert report["advice"]


def test_api_exposure_excludes_rehearsals_end_to_end(auth_client, tmp_path):
    """端到端钉住最容易写反的那条：排练一条消息都没发，不该推高任何读数。

    库必须自己**先**装配到 tmp——``configure_group_show_store`` 是「已装配即返回既有
    实例」的幂等语义，**路径不参与判定**；若先让路由去装配，本用例再拿到的就是同一个
    实例，写进去的假场次会落到路由那份库里。反过来（这里先装配）路由拿到的也是这一个，
    两边同库且都在 tmp，才既互通又不污染。曾因用 ``_store(None)`` 抢先装配到
    ``DEFAULT_DB_PATH`` 把 grp-x 假场次写进仓库 ``config/group_show.db``，下一轮
    ``max_pair_co`` 起手就是 1 → 假红。
    """
    from src.companion.group_show.playbook import (
        Casting, Playbook, ShowEvent, ShowState,
    )
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    def _stage(sid, group, dry_run, speakers):
        state = ShowState(session_id=sid, group_key=group, platform="telegram",
                          playbook=Playbook(id="pb", name="pb"),
                          casting=Casting(), dry_run=dry_run, started_at=1000.0)
        state.events = [
            ShowEvent(seq=i + 1, ts=1000.0, speaker_account=a, role="advocate",
                      beat_id="b1", text="hi", kind="line")
            for i, a in enumerate(speakers)
        ]
        st.save_session(state)

    _stage("rehearsal", "grp-x", True, ["ax", "bx"])
    perf = auth_client.get("/api/group-show/exposure").json()["report"]["performance"]
    assert "grp-x" not in (perf.get("groups_per_account") or {})
    assert perf["max_pair_co"] == 0

    _stage("live", "grp-x", False, ["ax", "bx"])
    perf = auth_client.get("/api/group-show/exposure").json()["report"]["performance"]
    assert perf["max_pair_co"] == 1


def test_api_exposure_window_actually_filters_by_time(auth_client, tmp_path):
    """时间窗必须真的过滤——前端缺省只看近 30 天，``since`` 算错这张卡就一直在说谎。

    共现不衰减的话跑几个月每一对都饱和成同一个大数，卡永远红、也就永远没人再看它。
    """
    import time as _t

    from src.companion.group_show.playbook import (
        Casting, Playbook, ShowEvent, ShowState,
    )
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    state = ShowState(session_id="old", group_key="grp-old", platform="telegram",
                      playbook=Playbook(id="pb", name="pb"), casting=Casting(),
                      dry_run=False, started_at=_t.time() - 200 * 86400)
    state.events = [
        ShowEvent(seq=i + 1, ts=state.started_at, speaker_account=a, role="advocate",
                  beat_id="b1", text="hi", kind="line")
        for i, a in enumerate(("oa", "ob"))
    ]
    st.save_session(state)

    def _perf(window):
        return auth_client.get(
            f"/api/group-show/exposure?window_days={window}").json()["report"]["performance"]

    assert _perf(30)["max_pair_co"] == 0, "200 天前的同台不该算进近 30 天"
    assert _perf(0)["max_pair_co"] == 1, "window_days=0 ＝全部历史，得看得见"


def _stock_shows(st, *, groups, speakers, ts):
    """往库里灌真演出，用来把「号池 × 群数」的预算压下来。"""
    from src.companion.group_show.playbook import (
        Casting, Playbook, ShowEvent, ShowState,
    )
    for g in range(groups):
        who = [f"pa{(g * speakers + k) % 6}" for k in range(speakers)]
        state = ShowState(session_id=f"s{g}", group_key=f"gg{g}", platform="telegram",
                          playbook=Playbook(id="pb", name="pb"), casting=Casting(),
                          dry_run=False, started_at=ts)
        state.events = [
            ShowEvent(seq=i + 1, ts=ts, speaker_account=a, role="advocate",
                      beat_id="b1", text="hi", kind="line")
            for i, a in enumerate(who)
        ]
        st.save_session(state)
        for a in who:
            st.record_membership(f"gg{g}", a, source="manual")


def test_rehearsal_is_capped_to_the_budget_so_the_preview_matches_a_live_run(
        auth_client, tmp_path, _books):
    """排练的用处是**预览真发的样子**。它演 4 个人而真发只允许 2 人，这个预览就在骗人。"""
    import time as _t

    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")
    _stock_shows(st, groups=40, speakers=3, ts=_t.time() - 3600)

    # 要一本**角色数超过预算**的剧本，压角才会真的发生：duo_matrixx（2 角）入库后
    # sorted 首位不再是 4 角书，拿它演 blocked 恒空、断言失去前提。
    pid = next((p for p in sorted(_books)
                if len(getattr(_books[p], "roles", ()) or ()) >= 4), None)
    if pid is None:
        pytest.skip("剧本库没有 ≥4 角剧本，预算压角预览无从构造")
    r = auth_client.post("/api/group-show/rehearse", json={
        "playbook_id": pid, "actors": 6, "max_speakers": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    cap = d["speaker_cap"]
    assert cap["capped"] is True and 0 < cap["effective"] < 5
    assert cap["reason"], "压回去了就得说清为什么，否则运营只会去关护栏"
    # 空槽还要逐个说清是被谁挡的：「缺角」两个字把「号不够」和「护栏在正常工作」糊成
    # 一种，运营看着戏一天天变冷清又找不到原因，最后的动作通常是把护栏关掉。
    blocked = d["casting"]["blocked"]
    assert blocked and set(blocked) == set(d["casting"]["unfilled"])
    assert "budget" in set(blocked.values())


def test_rehearsal_over_budget_needs_an_explicit_flag_and_is_audited(auth_client,
                                                                    tmp_path, _books):
    """越界是运营的权力，但要他**主动按**，且这一按必须留痕。"""
    import time as _t

    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")
    _stock_shows(st, groups=40, speakers=3, ts=_t.time() - 3600)

    r = auth_client.post("/api/group-show/rehearse", json={
        "playbook_id": sorted(_books)[0], "actors": 6,
        "max_speakers": 5, "over_budget": True})
    assert r.status_code == 200, r.text
    cap = r.json()["speaker_cap"]
    assert cap["effective"] == 5 and cap["source"] == "override"


def test_rehearsal_without_a_ledger_is_not_capped_out_of_nowhere(auth_client,
                                                                tmp_path, _books):
    """还没有任何台账时预算算不出来——这时凭空拦人只会让人以为工具坏了。"""
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    r = auth_client.post("/api/group-show/rehearse", json={
        "playbook_id": sorted(_books)[0], "actors": 4, "max_speakers": 4})
    assert r.status_code == 200, r.text
    assert r.json()["speaker_cap"]["capped"] is False


def test_api_exposure_carries_the_role_axis_and_a_merged_verdict(auth_client,
                                                                 tmp_path):
    """第四条轴要跟前三条同屏出现，且顶部那盏灯取**最差**的一轴。

    分开看会漏掉最常见的一种处境：号对矩阵排得很干净（绿），可有一个号在每个群里都是
    主推（红）——只报号对读数等于告诉运营「你安全」。
    """
    from src.companion.group_show.playbook import (
        Casting, Playbook, ShowEvent, ShowState,
    )
    from src.companion.group_show.roles import ALARM_ROLE_CO
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    # 同一个号在 ALARM_ROLE_CO 个不同的群里都演主推，且每个群只有它一个人开口
    # ——号对共现恒为 0（前三条轴全绿），单号自曝却已经拉满。
    for i in range(ALARM_ROLE_CO):
        state = ShowState(session_id=f"solo{i}", group_key=f"grp-solo{i}",
                          platform="telegram", playbook=Playbook(id="pb", name="pb"),
                          casting=Casting(), dry_run=False, started_at=1000.0)
        state.events = [ShowEvent(seq=1, ts=1000.0, speaker_account="shill",
                                  role="advocate", beat_id="b1", text="hi",
                                  kind="line")]
        st.save_session(state)

    d = auth_client.get("/api/group-show/exposure").json()
    if not d.get("available"):
        pytest.skip("本机场次库不可用，端点走空态分支")
    assert d["report"]["performance"]["max_pair_co"] == 0, "号对轴确实是干净的"
    roles = d["roles"]
    assert roles["roles"]["max_role_co"] >= ALARM_ROLE_CO
    assert roles["verdict"] == "danger"
    assert roles["advice"], "报了红就必须给出接下来怎么做"
    assert d["verdict"] == "danger", "顶部判词必须被角色轴拉红"


def test_api_exposure_role_axis_ignores_cover_roles(auth_client, tmp_path):
    """在 50 个群当路人是真人行为。把掩护角色也算成把柄会先耗尽它们，逼运营关护栏。"""
    from src.companion.group_show.playbook import (
        Casting, Playbook, ShowEvent, ShowState,
    )
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    for i in range(20):
        state = ShowState(session_id=f"cover{i}", group_key=f"grp-cover{i}",
                          platform="telegram", playbook=Playbook(id="pb", name="pb"),
                          casting=Casting(), dry_run=False, started_at=1000.0)
        state.events = [ShowEvent(seq=1, ts=1000.0, speaker_account="extra",
                                  role="bystander", beat_id="b1", text="hi",
                                  kind="line")]
        st.save_session(state)

    d = auth_client.get("/api/group-show/exposure").json()
    if not d.get("available"):
        pytest.skip("本机场次库不可用，端点走空态分支")
    assert d["roles"]["roles"]["max_role_co"] == 0
    assert d["roles"]["verdict"] == "safe"


def test_api_exposure_drills_the_number_down_to_a_pair_and_its_groups(auth_client,
                                                                     tmp_path):
    """「共现 4」运营动不了手——接口必须一并说出是哪两个号、缠在哪几个群。

    并且要带上闸门的当前判定：卡片红着而运营手动躲这两个号、其实闸门早就在拦了，
    这种错位比读数不准更伤——他会开始怀疑整套护栏到底有没有在工作。
    """
    from src.companion.group_show.attendance import NORMAL_PAIR_CO
    from src.companion.group_show.playbook import (
        Casting, Playbook, ShowEvent, ShowState,
    )
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    for i in range(NORMAL_PAIR_CO + 1):
        state = ShowState(session_id=f"duo{i}", group_key=f"grp-duo{i}",
                          platform="telegram", playbook=Playbook(id="pb", name="pb"),
                          casting=Casting(), dry_run=False, started_at=1000.0)
        state.events = [
            ShowEvent(seq=k + 1, ts=1000.0, speaker_account=a, role="advocate",
                      beat_id="b1", text="hi", kind="line")
            for k, a in enumerate(("dx", "dy"))
        ]
        st.save_session(state)

    d = auth_client.get("/api/group-show/exposure").json()
    if not d.get("available"):
        pytest.skip("本机场次库不可用，端点走空态分支")
    rows = d["report"]["performance"]["hot_pairs"]
    assert rows, "报了共现却说不出是哪一对＝这个数没法执行"
    top = rows[0]
    assert sorted(top["pair"]) == ["dx", "dy"]
    assert top["count"] == NORMAL_PAIR_CO + 1
    assert "grp-duo0" in top["groups"], "要给出举证的群，否则运营无法核对"
    assert top["blocked"] is True and top["headroom"] == 0


def test_api_exposure_never_tells_the_operator_that_solo_is_unlimited(auth_client,
                                                                     tmp_path):
    """每场只有一个号开口时，预算会如实报「无上限」——那只是发言轴。

    这是这张卡最容易骗死人的地方：运营记得住的就一个数，而最显眼的那个偏偏最乐观。
    容量必须在同一份响应里给出**有限**的真实上限，并点名瓶颈已经换成主推集中度。
    """
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")
    # 每群一个号、每场都带货 —— 号对轴（成员+发言）双双归零，只剩单号自曝
    _stock_shows(st, groups=40, speakers=1, ts=1000.0)

    d = auth_client.get("/api/group-show/exposure?window_days=0").json()
    if not d.get("available"):
        pytest.skip("本机场次库不可用，端点走空态分支")

    solo = [o for o in d["report"]["budget"]["options"] if o["speakers"] == 1]
    if not solo:
        pytest.skip("号池为空，预算段不参与本用例")
    assert solo[0]["unlimited"] is True, "先固定住那个会骗人的读数"

    cap = d["capacity"]
    assert cap, "预算说无上限时，容量段绝不能缺席"
    assert cap["max_groups"] is not None, "三条轴取最紧之后不可能还是无上限"
    assert cap["binding"] == "role", "号对轴归零后瓶颈必然换成主推集中度"
    assert cap["advice"], "给了上限就得说该拧哪个旋钮"


def test_api_exposure_capacity_reads_the_play_off_the_ledger_not_a_config(auth_client,
                                                                         tmp_path):
    """演法要从台账反推。按配置算会得出「按计划我们很安全」，而平台数的是真消息。"""
    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")
    _stock_shows(st, groups=12, speakers=3, ts=1000.0)

    d = auth_client.get("/api/group-show/exposure?window_days=0").json()
    if not d.get("available") or not d.get("capacity"):
        pytest.skip("号池为空，容量段不参与本用例")
    cap = d["capacity"]
    assert cap["speakers"] == 3, "实测每场 3 个号开口，不该是配置里的默认值"
    assert cap["groups"] == 12
    assert cap["groups_per_account"] > 0, "单号负载是 solo 唯一还在涨的成本，必须露出来"


# 读账口径（两来源并集 / 软失败留痕 / 窗口）已收口到 ``group_show.ledgers``，
# 其不变量由 ``tests/test_group_show_ledgers.py`` 守；本文件只守 HTTP 那一层。


# ── 开演排期端点 ────────────────────────────────────────────────────────────


def test_api_schedule_spreads_shows_across_the_active_window(auth_client):
    """排期表要真按活动时段铺开，且**不落在深夜**——那既不像真人也拿不到互动。"""
    r = auth_client.post("/api/group-show/schedule", json={
        "groups": [f"g{i}" for i in range(20)], "per_day": 1,
        "active_hours": [9, 22], "day_start_ts": 1_700_000_000,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["plan"]) == 20
    assert d["metrics"]["count"] == 20
    assert d["metrics"]["quiet_violations"] == 0
    assert all(9 <= row["hour"] < 22 for row in d["plan"])
    assert d["day_start_guessed"] is False
    assert d["advice"]


def test_api_schedule_does_not_line_shows_up_at_equal_intervals(auth_client):
    """等距是最好抓的一条：做一次自相关就出来了。抖动必须真的把它打散。"""
    r = auth_client.post("/api/group-show/schedule", json={
        "groups": [f"g{i}" for i in range(30)], "per_day": 1,
        "day_start_ts": 1_700_000_000,
    })
    d = r.json()
    assert d["metrics"]["regularity"] < 0.5
    assert d["metrics"]["round_minute_ratio"] < 0.2, "别掐着整点/半点开演"


def test_api_schedule_changes_from_day_to_day_on_its_own(auth_client):
    """纯函数的反效果：不拌当天零点的话「每天 14:07 开演」会**默认发生**且运营无感。"""
    def _times(day_start):
        return [row["at"] - day_start for row in auth_client.post(
            "/api/group-show/schedule",
            json={"groups": [f"g{i}" for i in range(12)], "per_day": 1,
                  "day_start_ts": day_start}).json()["plan"]]

    assert _times(1_700_000_000) != _times(1_700_086_400)
    assert _times(1_700_000_000) == _times(1_700_000_000), "同一天重跑必须逐行一致"


def test_api_schedule_flags_a_guessed_timezone(auth_client):
    """服务器猜的零点照样能用，但必须标注——跨境部署里那个错读看起来完全正常。"""
    d = auth_client.post("/api/group-show/schedule",
                         json={"groups": ["g1", "g2"], "per_day": 1}).json()
    assert d["day_start_guessed"] is True
    assert d["day_start_ts"] > 0


def test_api_schedule_reports_who_is_still_in_the_cross_group_cooldown(auth_client):
    """同号前后脚在两个群冒头是真人不会有的行为。谁在冷却窗里要看得见。"""
    import time as _t

    class _Inbox:
        def group_last_spoke_at(self, *, since_ts=0.0):
            return {"hot": _t.time() - 30.0, "cold": _t.time() - 99999.0}

    app = getattr(auth_client, "app", None)
    if app is None:
        pytest.skip("拿不到 app.state，跳过注入式用例")
    old = getattr(app.state, "inbox_store", None)
    app.state.inbox_store = _Inbox()
    try:
        d = auth_client.post("/api/group-show/schedule",
                             json={"groups": ["g1"], "per_day": 1,
                                   "day_start_ts": 1_700_000_000}).json()
    finally:
        app.state.inbox_store = old

    waiting = {c["account"] for c in d["cooldowns"]}
    assert "hot" in waiting and "cold" not in waiting


def test_api_schedule_says_so_when_a_cooldown_ledger_could_not_be_read(auth_client):
    """读挂的台账会让冷却名单变空——而空名单跟「大家都能上」长得一模一样。

    护栏读数悄悄失效比读数不可用危险得多：后者会有人去修，前者是一片绿，没人怀疑。
    """
    class _Boom:
        def group_last_spoke_at(self, **kw):
            raise RuntimeError("db gone")

    app = getattr(auth_client, "app", None)
    if app is None:
        pytest.skip("拿不到 app.state，跳过注入式用例")
    old = getattr(app.state, "inbox_store", None)
    app.state.inbox_store = _Boom()
    try:
        d = auth_client.post("/api/group-show/schedule",
                             json={"groups": ["g1"], "day_start_ts": 1_700_000_000})
    finally:
        app.state.inbox_store = old

    assert d.status_code == 200                      # 旁路能力，绝不打挂整页
    assert "inbox" in (d.json().get("cooldowns_degraded") or [])


def test_api_schedule_never_500s_on_garbage(auth_client):
    """脏参数一律软降级：排期是旁路能力，不该把整页打挂。"""
    for body in ({"per_day": "x"}, {"active_hours": "nope"}, {"groups": "g1"},
                 {"day_start_ts": "later"}, {}, {"per_day": 0}):
        r = auth_client.post("/api/group-show/schedule", json=body)
        assert r.status_code == 200, (body, r.text)
        assert r.json()["ok"] is True


def test_exposure_counts_everyday_replies_not_just_choreographed_shows(auth_client,
                                                                      tmp_path):
    """**假安全**防线：一场戏没演过不等于安全——日常自动回复同样在群里同框。

    平台数的是消息，不区分是不是编排的。只读场次库会让这张风险卡在最危险的时候
    报绿灯（号早就在几十个群里靠日常回复互相同框了）。
    """
    import time as _t

    from src.companion.group_show.store import configure_group_show_store

    st = configure_group_show_store(tmp_path / "group_show.db")
    if not getattr(st, "available", False):
        pytest.skip("本机场次库不可用")

    class _FakeInbox:
        """只实现 group_speech_ledger 的最小替身（真库形状由 inbox 侧用例保证）。"""

        def group_speech_ledger(self, *, since_ts=0.0, platform=""):
            assert since_ts > 0, "带窗口查询必须把 since 传下去"
            return {"grp-daily": ["a1", "a2"], "grp-other": ["a1", "a2"]}

    app = auth_client.app if hasattr(auth_client, "app") else None
    if app is None:                                     # TestClient 版本差异兜底
        pytest.skip("拿不到 app.state，跳过注入式用例")
    old = getattr(app.state, "inbox_store", None)
    app.state.inbox_store = _FakeInbox()
    try:
        perf = auth_client.get(
            "/api/group-show/exposure?window_days=30").json()["report"]["performance"]
    finally:
        app.state.inbox_store = old

    assert perf["max_pair_co"] == 2, "两个号在两个群都发过言＝共现 2，场次库里却一场没有"
    assert _t.time() > 0     # 时间源没被 monkeypatch 走偏


def test_api_exposure_never_500s_on_garbage_window(auth_client):
    for window in ("x", -5, 99999, 0):
        r = auth_client.get(f"/api/group-show/exposure?window_days={window}")
        assert r.status_code in (200, 422), f"脏窗口把接口打挂了: {window}"


def test_group_list_prefers_real_ids_over_the_placeholder_count():
    """两个都给时以真实清单为准（占位模式只是没有 id 时的测算档）。"""
    from src.web.routes.group_show_routes import group_list

    assert group_list({"groups": ["a", "b"], "group_count": 50}) == ["a", "b"]
    assert group_list({"group_count": 3}) == ["#1", "#2", "#3"]
    assert group_list({}) == []
    assert group_list(None) == []


# ── API：出席台账（把排班表从一张纸变成能跨天执行的清单） ────────────────────

def test_marking_a_join_is_remembered_and_becomes_existing_next_time(auth_client):
    """端到端闭环：记一笔「已加入」→ 下次排班把它当存量，不再重复派活。

    没有这一步，运营执行完 Day 1，第二天打开控制台还是同一张 Day 1。
    """
    r = auth_client.post("/api/group-show/attendance",
                         json={"groups": ["群甲", "群乙"], "seats": 2})
    plan = r.json()["plan"]
    if plan is None:
        pytest.skip("本机注册表没有在线号")
    first = plan["joins"].get("群甲") or []
    if not first:
        pytest.skip("号池排不出增量")
    acct = first[0]

    ack = auth_client.post("/api/group-show/attendance/joined",
                           json={"group": "群甲", "account": acct})
    assert ack.status_code == 200 and ack.json()["done"] is True

    again = auth_client.post("/api/group-show/attendance",
                             json={"groups": ["群甲", "群乙"], "seats": 2}).json()
    assert acct in (again["plan"]["already"].get("群甲") or []), "台账没被当成存量"
    assert acct not in (again["plan"]["joins"].get("群甲") or []), "又派了一次同样的活"


def test_history_groups_outside_the_current_list_still_count_as_risk(auth_client):
    """台账里不在本次清单的群＝历史：不排，但共同出席照算。

    只算本批 → 每批新群都显示「安全」，累积起来早就超标了。
    """
    r = auth_client.post("/api/group-show/attendance",
                         json={"groups": ["老群"], "seats": 2})
    plan = r.json()["plan"]
    if plan is None or not (plan["joins"].get("老群") or []):
        pytest.skip("本机注册表没有在线号")
    for acct in plan["assignments"]["老群"]:
        auth_client.post("/api/group-show/attendance/joined",
                         json={"group": "老群", "account": acct})

    fresh = auth_client.post("/api/group-show/attendance",
                             json={"groups": ["新群"], "seats": 2}).json()["plan"]
    assert fresh["metrics"]["history_groups"] == 1
    assert "老群" not in fresh["assignments"], "历史群不该出现在本次排班表里"


def test_unmarking_a_join_removes_it_from_the_ledger(auth_client):
    """点错了要能撤——删的只是我们的账本，不会真的退群。"""
    auth_client.post("/api/group-show/attendance/joined",
                     json={"group": "误点群", "account": "acct_x"})
    off = auth_client.post("/api/group-show/attendance/joined",
                           json={"group": "误点群", "account": "acct_x",
                                 "done": False})
    assert off.status_code == 200 and off.json()["done"] is False

    from src.web.routes.group_show_routes import _recorded_memberships
    assert "误点群" not in _recorded_memberships(None)


@pytest.mark.parametrize("body", [
    {}, {"group": ""}, {"account": "a1"}, {"group": "  ", "account": "a1"},
])
def test_marking_without_both_ids_is_rejected_not_silently_swallowed(
        auth_client, body):
    """缺参数要明确 4xx——静默成功会让运营以为记上了，第二天发现表没变。"""
    r = auth_client.post("/api/group-show/attendance/joined", json=body)
    assert r.status_code == 400


def test_the_ledger_write_endpoint_needs_write_permission(client):
    """台账是唯一的写端点，不能让只读会话改。"""
    r = client.post("/api/group-show/attendance/joined",
                    json={"group": "g", "account": "a"})
    assert r.status_code in (401, 403), "未登录也能写台账"


# ── API：真发预检 / 开演闸门 ─────────────────────────────────────────────────


def test_api_live_check_only_never_sends_and_reports_locks(auth_client, _books,
                                                           monkeypatch):
    """check_only 必须零发送：即使编排器/配置锁全关，也只回报体检结构。"""
    import src.web.routes.group_show_routes as mod

    pid = "solo_matrixx" if "solo_matrixx" in _books else sorted(_books)[0]
    monkeypatch.setattr(mod, "_online_accounts", lambda _cm: [{
        "account_id": "acc_solo", "platform": "telegram",
        "status": "online", "display_name": "solo",
        "meta": {"persona_id": "p1", "fingerprint_group": "fp1"},
    }])
    # 清掉可能残留的同群互斥，避免并行用例串味
    mod._live_inflight.clear()

    r = auth_client.post("/api/group-show/live", json={
        "playbook_id": pid, "group_key": "-100check",
        "check_only": True, "max_speakers": 1,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["check_only"] is True
    assert d["armed"] is False          # 没 confirm + 配置通常关
    assert "plan_ok" in d and "reason" in d
    assert "config_enabled" in d and "casting" in d
    assert "in_quiet_hours" in d
    assert "quiet_hours" in d  # 灰度夜测：回显生效窗，防「文件 [0,0] / reason=quiet_hours」盲查
    assert d["group_key"] == "-100check"


def test_api_live_requires_group_and_confirm_for_real_send(auth_client, _books):
    pid = sorted(_books)[0]
    missing = auth_client.post("/api/group-show/live", json={
        "playbook_id": pid, "check_only": False, "confirm_live": True,
    })
    assert missing.status_code == 400

    no_confirm = auth_client.post("/api/group-show/live", json={
        "playbook_id": pid, "group_key": "-100x",
        "check_only": False, "confirm_live": False,
    })
    assert no_confirm.status_code == 400
    assert "confirm" in no_confirm.json()["detail"].lower() or "锁" in no_confirm.json()["detail"]


def test_api_live_refuses_when_config_lock_is_off(auth_client, _books,
                                                  monkeypatch):
    """配置锁关着时即使 confirm_live=true 也必须 403——双锁缺一不可。"""
    import src.web.routes.group_show_routes as mod
    from src.companion.group_show.live import plan_live as real_plan

    pid = "solo_matrixx" if "solo_matrixx" in _books else sorted(_books)[0]
    monkeypatch.setattr(mod, "_online_accounts", lambda _cm: [{
        "account_id": "acc_solo", "platform": "telegram",
        "status": "online", "display_name": "solo",
        "meta": {"persona_id": "p1", "fingerprint_group": "fp1"},
    }])
    # 预检过关，但真实配置锁仍关 —— 应在 config 闸门拦下，而不是进后台任务
    class _Ok:
        ok = True
        reason = ""
        warnings = ()
        casting = None
        solo = True

    monkeypatch.setattr(
        "src.companion.group_show.live.plan_live",
        lambda *a, **k: _Ok())
    # live_enabled 读真实 config；多数测试环境默认关。若本机 overlay 开了，
    # 再强制 monkeypatch live_enabled → False。
    monkeypatch.setattr(
        "src.companion.group_show.live.live_enabled", lambda _cfg: False)

    mod._live_inflight.clear()
    r = auth_client.post("/api/group-show/live", json={
        "playbook_id": pid, "group_key": "-100cfg",
        "confirm_live": True, "check_only": False,
    })
    assert r.status_code == 403
    # 还原 import 路径上的符号（路由内 from-import 已绑定；403 足以证明闸门）
    _ = real_plan


def test_api_live_inflight_mutex_blocks_second_show(auth_client, _books,
                                                    monkeypatch):
    """同群叠戏比共现指标更假——第二场必须 409。"""
    import time
    import src.web.routes.group_show_routes as mod

    pid = "solo_matrixx" if "solo_matrixx" in _books else sorted(_books)[0]
    monkeypatch.setattr(mod, "_online_accounts", lambda _cm: [{
        "account_id": "acc_solo", "platform": "telegram",
        "status": "online", "display_name": "solo",
        "meta": {"persona_id": "p1", "fingerprint_group": "fp1"},
    }])
    class _Ok:
        ok = True
        reason = ""
        warnings = ()
        casting = None
        solo = True

    monkeypatch.setattr(
        "src.companion.group_show.live.plan_live", lambda *a, **k: _Ok())
    monkeypatch.setattr(
        "src.companion.group_show.live.live_enabled", lambda _cfg: True)

    mod._live_inflight.clear()
    mod._live_inflight["-100mutex"] = ("live_already_running", time.time())
    try:
        r = auth_client.post("/api/group-show/live", json={
            "playbook_id": pid, "group_key": "-100mutex",
            "confirm_live": True, "check_only": False,
        })
        assert r.status_code == 409
        assert "live_already_running" in r.json()["detail"] or "叠" in r.json()["detail"]
    finally:
        mod._live_inflight.clear()


def test_api_live_inflight_ttl_lets_a_stale_lock_expire(auth_client, _books,
                                                        monkeypatch):
    """后台任务若漏放锁，TTL 必须让下一场能开——否则这个群永久卡死。"""
    import time
    import src.web.routes.group_show_routes as mod

    pid = "solo_matrixx" if "solo_matrixx" in _books else sorted(_books)[0]
    monkeypatch.setattr(mod, "_online_accounts", lambda _cm: [{
        "account_id": "acc_solo", "platform": "telegram",
        "status": "online", "display_name": "solo",
        "meta": {"persona_id": "p1", "fingerprint_group": "fp1"},
    }])
    class _Ok:
        ok = True
        reason = ""
        warnings = ()
        casting = None
        solo = True
        in_quiet_hours = False

    monkeypatch.setattr(
        "src.companion.group_show.live.plan_live", lambda *a, **k: _Ok())
    monkeypatch.setattr(
        "src.companion.group_show.live.live_enabled", lambda _cfg: True)
    # 过期占位：claimed_at 远在 TTL 之外
    mod._live_inflight.clear()
    mod._live_inflight["-100ttl"] = (
        "stale_sid", time.time() - mod.LIVE_INFLIGHT_TTL_SEC - 10)
    # 编排器仍缺 → 应过了互斥闸后停在 no_orchestrator（503），而不是 inflight 409
    r = auth_client.post("/api/group-show/live", json={
        "playbook_id": pid, "group_key": "-100ttl",
        "confirm_live": True, "check_only": False,
    })
    assert r.status_code != 409, "过期锁仍被当成在演"
    assert r.status_code in (503, 200)
    mod._live_inflight.clear()


def test_group_show_page_renders_console(auth_client):
    r = auth_client.get("/group-show")
    assert r.status_code == 200
    html = r.text
    for anchor in ('id="gs-lib-body"', 'id="gs-detail-body"', 'id="gs-pb"',
                   'id="gs-run"', 'id="gs-stage"', 'id="gs-nat"',
                   'id="gs-hist-body"', 'id="gs-lk"', 'id="gs-att"',
                   'id="gs-live"', 'id="gs-live-check"'):
        assert anchor in html, f"导播台缺关键元素: {anchor}"
    assert "gsRun" in html and "apiFetch(" in html
    assert "gsLoadLinkage" in html, "体检卡没接上加载函数＝永远显示「加载中…」"
    assert "gsPlanAttendance" in html, "排班卡的按钮没有对应函数＝点了没反应"
    assert "gsMarkJoined" in html, "首日任务单没有记账入口＝这张表只能用一天"
    assert "gsCheckLive" in html, "开演前体检卡没接上函数＝按钮点了没反应"
    assert "check_only" in html
    # 这张卡只做「开演前体检」，绝不含一键真发按钮：页面脚本里不能出现
    # 硬编码的 confirm_live:true（真发那把参数侧的锁），也不该有独立的
    # 「一键真发」按钮 id。
    assert "gs-live-go" not in html
    assert '"confirm_live": true' not in html
    assert '"confirm_live":true' not in html
    assert "confirm_live: true" not in html


def test_nav_item_registered_in_sidebar():
    """侧栏入口走 nav_schema 单源（不许在 base.html 手写 <a>）。"""
    from src.web.nav_schema import NAV_ITEMS, get_nav_context

    item = NAV_ITEMS["group_show"]
    assert item["path"] == "/group-show" and item["key"] == "group_show"
    paths = [it["path"] for g in get_nav_context()["nav_groups"]
             for it in g["items"] if isinstance(it, dict)]
    assert "/group-show" in paths
