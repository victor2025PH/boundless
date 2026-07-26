"""群戏关联风险分组门禁。

这个文件守的是 group_show 唯一一条「错了会直接导致封号」的不变量：**指纹归属未知的号
不得同台**。其余所有门禁失败最多让戏演得难看，只有这一条失败会赔掉一批账号。

回归锚点是真实数据：2026-07 本仓账号注册表 9 个号的 proxy_id / fingerprint_id 全为空串，
早期实现会把它们判成 9 个互相独立的指纹组、全部准许同台。:func:`test_real_registry_shape_*`
就是把那个形状钉死在这里，防止「未知＝安全」的写法哪天再摸回来。
"""
from __future__ import annotations

import pytest

from src.companion.group_show.casting import (
    SHARED_HOST_GROUP,
    cast_roles,
    eligible_candidates,
    validate_casting,
)
from src.companion.group_show.linkage import (
    DEFAULT_HOST_TAG,
    derive_fingerprint_groups,
    derive_group,
    format_readiness,
    linkage_readiness,
    overrides_from_config,
    unblock_advice,
)
from src.companion.group_show.playbook import Playbook, Role


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _playbook(slots=("advocate", "asker", "skeptic", "bystander")) -> Playbook:
    """最小可用剧本：只有选角关心的 id 与 roles，beats 与选角无关。"""
    return Playbook(
        id="pb_test",
        name="测试剧本",
        system="growth",
        products=("matrixx",),
        roles=tuple(Role(slot=s, desc="") for s in slots),
        beats=(),
    )


def _account(account_id, **kw):
    """账号注册表行的形状（字段缺省对齐生产库默认值：空串）。"""
    base = {
        "account_id": account_id,
        "platform": "telegram",
        "mode": "protocol",
        "proxy_id": "",
        "fingerprint_id": "",
        "meta": {},
    }
    base.update(kw)
    return base


def _candidate(account_id, **kw):
    """选角候选的形状（默认健康在线、无指纹信息——即生产实况）。"""
    base = {
        "account_id": account_id,
        "platform": "telegram",
        "persona_id": f"p_{account_id}",
        "display_name": account_id,
        "health": "online",
    }
    base.update(kw)
    return base


# ── derive_group：四条分组规则 ──────────────────────────────────────────────


def test_proxy_is_the_grouping_key_when_present():
    """有独立代理＝真正的网络隔离，代理就是分组主键。"""
    group, source = derive_group(_account("a1", proxy_id="px-7"))
    assert group == "proxy:px-7"
    assert source == "proxy"


def test_same_proxy_means_same_group():
    """两个号共用一个代理出口 → 同组 → 不得同台。"""
    g1, _ = derive_group(_account("a1", proxy_id="px-7"))
    g2, _ = derive_group(_account("a2", proxy_id="px-7"))
    assert g1 == g2


def test_proxy_in_meta_is_also_honoured():
    """有些登录流程只把代理写进 meta，不能因为落点不同就漏掉这个信号。"""
    group, source = derive_group(_account("a1", meta={"proxy_id": "px-9"}))
    assert group == "proxy:px-9"
    assert source == "proxy"


def test_device_mode_with_serial_gets_its_own_group():
    """手机走自己的蜂窝网，出口独立于宿主机。"""
    group, source = derive_group(
        _account("a1", mode="device", meta={"device_serial": "SN123"}))
    assert group == "device:SN123"
    assert source == "device"


def test_device_mode_without_serial_falls_back_to_host():
    """**边界**：只知道 mode=device 却不知道是哪台机器时，没有任何依据断言它独立。

    这是最容易写错的一处——「反正是设备模式，应该各自独立吧」正是那种听起来合理、
    实际上把未知当安全的推断。
    """
    group, source = derive_group(_account("a1", mode="device"))
    assert group == f"host:{DEFAULT_HOST_TAG}"
    assert source == "host"


def test_no_signal_at_all_falls_back_to_shared_host():
    """什么都没配 → 宿主兜底组（本仓 9 个号的真实处境）。"""
    group, source = derive_group(_account("a1"))
    assert group == f"host:{DEFAULT_HOST_TAG}"
    assert source == "host"


def test_fingerprint_id_alone_does_not_create_a_group():
    """配了浏览器画像但没配代理 → 仍然是同一出口 → 仍然同组。

    画像在同出口之下只能削弱、不能消除关联，所以它不参与分组决策。
    """
    g1, s1 = derive_group(_account("a1", fingerprint_id="fp_aaa"))
    g2, s2 = derive_group(_account("a2", fingerprint_id="fp_bbb"))
    assert g1 == g2 == f"host:{DEFAULT_HOST_TAG}"
    assert s1 == s2 == "host"


def test_manual_override_wins_over_everything():
    """设备台账比程序猜测更可信，人工表优先级最高。"""
    group, source = derive_group(
        _account("a1", proxy_id="px-7"), overrides={"a1": "机房B"})
    assert group == "机房B"
    assert source == "override"


def test_override_accepts_platform_qualified_key():
    """``platform:account_id`` 写法同样认（两种写法都很自然，不该让人 debug）。"""
    group, _ = derive_group(
        _account("a1"), overrides={"telegram:a1": "机房C"})
    assert group == "机房C"


def test_host_tag_separates_machines():
    """多机部署：不同宿主的兜底组必须互相区分，否则跨机的号会被误判为互斥。"""
    g1, _ = derive_group(_account("a1"), host_tag="host-117")
    g2, _ = derive_group(_account("a2"), host_tag="host-140")
    assert g1 != g2


@pytest.mark.parametrize("junk", [None, {}, {"account_id": None}, {"account_id": ""}])
def test_derive_group_never_raises_on_junk(junk):
    """脏数据必须落到最保守的一档，绝不抛。"""
    group, source = derive_group(junk or {})
    assert group == f"host:{DEFAULT_HOST_TAG}"
    assert source == "host"


# ── 批量派生 ────────────────────────────────────────────────────────────────


def test_derive_fingerprint_groups_maps_every_account():
    accounts = [
        _account("a1", proxy_id="px-1"),
        _account("a2", proxy_id="px-2"),
        _account("a3"),
    ]
    groups = derive_fingerprint_groups(accounts)
    assert groups == {
        "a1": "proxy:px-1",
        "a2": "proxy:px-2",
        "a3": f"host:{DEFAULT_HOST_TAG}",
    }


def test_derive_fingerprint_groups_output_feeds_cast_roles():
    """派生表能直接喂给选角——这是两个模块之间唯一的契约，必须钉住。"""
    accounts = [_account("a1", proxy_id="px-1"), _account("a2", proxy_id="px-2")]
    groups = derive_fingerprint_groups(accounts)
    casting = cast_roles(
        _playbook(("advocate", "asker")),
        [_candidate("a1"), _candidate("a2")],
        fingerprint_groups=groups,
    )
    assert len(casting.members) == 2


# ── 体检报告 ────────────────────────────────────────────────────────────────


def test_readiness_reports_hard_problem_when_everyone_shares_one_exit():
    """本仓当前的真实处境：多个号、一个出口 → 最多上一人 → 这场戏演不起来。"""
    report = linkage_readiness([_account(f"a{i}") for i in range(9)])
    assert report["total"] == 9
    assert report["max_concurrent"] == 1
    assert len(report["shared_host"]) == 9
    hard = [p for p in report["problems"] if not p.startswith("warn:")]
    assert hard, "9 个号挤一个出口必须报硬伤，不能只给软警告"
    assert "独立代理" in hard[0]


def test_readiness_counts_independent_groups():
    accounts = [
        _account("a1", proxy_id="px-1"),
        _account("a2", proxy_id="px-2"),
        _account("a3", mode="device", meta={"device_serial": "SN1"}),
        _account("a4"),
    ]
    report = linkage_readiness(accounts)
    assert report["max_concurrent"] == 4
    assert report["by_source"] == {"proxy": 2, "device": 1, "host": 1}


def test_readiness_flags_fingerprint_without_proxy():
    """配了画像没配代理是个很容易自我安慰的状态，必须单独点名。"""
    report = linkage_readiness([
        _account("a1", fingerprint_id="fp_a"),
        _account("a2", fingerprint_id="fp_b"),
    ])
    assert any("画像" in p for p in report["problems"])


def test_readiness_never_raises_on_junk():
    report = linkage_readiness([None, "not a dict", {}, _account("ok")])
    assert report["total"] == 1


def test_advice_offers_device_route_before_buying_proxies():
    """「演不了」必须配「怎么才能演」，且优先给不用花钱的那条路。

    真机走蜂窝网是本仓现成资源（兄弟仓已有 Android 自动化），常被忽略而直接跳到买代理。
    """
    report = linkage_readiness([_account(f"a{i}") for i in range(3)])
    advice = report["advice"]
    assert advice, "报了硬伤却不给解法，等于把问题原样丢回去"
    assert "真机" in advice[0], "成本最低的那条路要排在最前面"
    assert any("代理" in a for a in advice)
    assert any("单号真实参与" in a for a in advice), (
        "必须给一条「今天就能做」的降级路径")


def test_advice_is_silent_when_the_cast_is_already_viable():
    """够演了就别啰嗦——建议只在真的卡住时出现，否则会被当成噪声忽略。"""
    rows = [_account(f"a{i}", proxy_id=f"px-{i}") for i in range(3)]
    assert linkage_readiness(rows)["advice"] == []


def test_advice_counts_how_many_more_exits_are_needed():
    """建议要说清楚「还差几个」，而不是笼统地说「去配代理」。"""
    rows = [_account("a1", proxy_id="px-1")] + [
        _account(f"b{i}") for i in range(3)]
    # 1 个独立代理 + 1 个宿主兜底组 = 2 组，距离像样的 3 人还差 1
    advice = linkage_readiness(rows)["advice"]
    assert any("1 个号" in a for a in advice)


def test_advice_never_suggests_moving_more_accounts_than_exist():
    """只有 1 个号时不能建议「把其中 2 个号迁到真机」——自相矛盾的建议会让人不再信这份报告。

    号本身不够时，正确的第一步是补号；此时谈迁真机/配代理是空话，照做完了还是演不了。
    """
    advice = unblock_advice(total=1, max_concurrent=1, shared_host=["a1"])
    assert any("补到至少" in a for a in advice), "号不够却没提「先补号」"
    assert not any("2 个号" in a or "3 个号" in a for a in advice), \
        f"建议迁移的号数超过了实际持有量: {advice}"


def test_format_readiness_renders_advice():
    text = format_readiness(linkage_readiness([_account("a1"), _account("a2")]))
    assert "怎么解：" in text
    assert "真机" in text


def test_format_readiness_marks_mutually_exclusive_groups():
    text = format_readiness(linkage_readiness([_account("a1"), _account("a2")]))
    assert "互斥" in text
    assert "最多可同台：1 人" in text


# ── 选角侧：兜底方向（本文件的核心） ────────────────────────────────────────


def test_unknown_fingerprints_collapse_into_one_group():
    """**核心不变量**：指纹全未知的一批号，最多只能有一个上台。"""
    casting = cast_roles(
        _playbook(),
        [_candidate(f"a{i}") for i in range(5)],
    )
    assert len(casting.members) == 1, (
        "指纹未知的号必须并入同一兜底组；放行多个＝把一锅端姿势当成安全配置")
    assert "advocate" == casting.members[0].slot, "仅剩的名额要给核心角"
    assert set(casting.unfilled) == {"asker", "skeptic", "bystander"}


def test_unknown_fingerprint_candidates_share_the_sentinel_group():
    pool = eligible_candidates([_candidate("a1"), _candidate("a2")])
    assert {c["fingerprint_group"] for c in pool} == {SHARED_HOST_GROUP}


def test_allow_shared_host_restores_independence_for_dry_run():
    """dry-run 一条消息都不发，零关联风险，允许显式放宽——否则排练根本排不起来。"""
    casting = cast_roles(
        _playbook(),
        [_candidate(f"a{i}") for i in range(4)],
        allow_shared_host=True,
    )
    assert len(casting.members) == 4


def test_allow_shared_host_defaults_to_false():
    """默认必须是安全的那一档：调用方什么都不想时，得到的是保守行为。"""
    many = [_candidate(f"a{i}") for i in range(4)]
    assert len(cast_roles(_playbook(), many).members) == 1
    assert len(eligible_candidates(many)) == 4  # 池子不过滤，约束发生在选角阶段


def test_real_fingerprints_allow_a_full_cast():
    """配齐独立代理后，同一批号就能演完整一台戏——安全约束不是「永远演不了」。"""
    candidates = [
        _candidate("a1", fingerprint_group="proxy:px-1"),
        _candidate("a2", fingerprint_group="proxy:px-2"),
        _candidate("a3", fingerprint_group="proxy:px-3"),
        _candidate("a4", fingerprint_group="proxy:px-4"),
    ]
    casting = cast_roles(_playbook(), candidates)
    assert len(casting.members) == 4
    assert not casting.unfilled


def test_partial_fingerprint_coverage_is_conservative():
    """两个号有真代理、三个号未知 → 2 + 1 = 3 个可用组。"""
    candidates = [
        _candidate("a1", fingerprint_group="proxy:px-1"),
        _candidate("a2", fingerprint_group="proxy:px-2"),
        _candidate("a3"), _candidate("a4"), _candidate("a5"),
    ]
    casting = cast_roles(_playbook(), candidates)
    assert len(casting.members) == 3


# ── 校验侧：沉默的跳过是事故温床 ────────────────────────────────────────────


def test_validate_warns_loudly_when_no_fingerprint_table_supplied():
    """没查和查过了长得一样，是最危险的一种沉默。"""
    casting = cast_roles(
        _playbook(),
        [_candidate("a1", fingerprint_group="proxy:px-1"),
         _candidate("a2", fingerprint_group="proxy:px-2"),
         _candidate("a3", fingerprint_group="proxy:px-3")],
    )
    problems = validate_casting(casting, _playbook())
    assert any("跳过" in p and "关联复查" in p for p in problems)


def test_validate_catches_collision_for_accounts_missing_from_the_table():
    """外部表没覆盖到的号＝归属未知＝并入兜底组 → 两个未覆盖的号必须撞出硬错。"""
    casting = cast_roles(
        _playbook(),
        [_candidate("a1", fingerprint_group="proxy:px-1"),
         _candidate("a2", fingerprint_group="proxy:px-2"),
         _candidate("a3", fingerprint_group="proxy:px-3")],
    )
    problems = validate_casting(
        casting, _playbook(), fingerprint_groups={"a1": "proxy:px-1"})
    hard = [p for p in problems if not p.startswith("warn:")]
    assert any("撞车" in p for p in hard)


def test_validate_passes_with_full_distinct_table():
    casting = cast_roles(
        _playbook(),
        [_candidate("a1", fingerprint_group="proxy:px-1"),
         _candidate("a2", fingerprint_group="proxy:px-2"),
         _candidate("a3", fingerprint_group="proxy:px-3")],
    )
    problems = validate_casting(
        casting, _playbook(),
        fingerprint_groups={"a1": "proxy:px-1", "a2": "proxy:px-2",
                            "a3": "proxy:px-3"},
    )
    assert not [p for p in problems if not p.startswith("warn:")]


# ── 端到端：真实注册表形状 ──────────────────────────────────────────────────


def test_real_registry_shape_yields_single_seat():
    """回归锚点：2026-07 生产库的真实形状（proxy/fingerprint 全空）。

    如果哪天这条测试变红成「可以上 9 个人」，说明「未知＝安全」的写法又回来了。
    """
    registry_rows = [
        _account("8244899900", meta={"phone": "x", "session_name": "s"}),
        _account("8118214990", meta={"phone": "y", "session_name": "t"}),
        _account("639273815533", platform="whatsapp",
                 meta={"baileys_login_id": "b1"}),
        _account("100089088819384", platform="messenger", mode="web",
                 meta={"messenger_login_id": "m1"}),
        _account("u9", platform="line", meta={"tokens_path": "p"}),
        _account("639270135480", platform="whatsapp", mode="device",
                 meta={"self_name": "n"}),
    ]
    report = linkage_readiness(registry_rows)
    assert report["max_concurrent"] == 1

    groups = derive_fingerprint_groups(registry_rows)
    casting = cast_roles(
        _playbook(),
        [_candidate(r["account_id"], platform=r["platform"])
         for r in registry_rows],
        fingerprint_groups=groups,
    )
    assert len(casting.members) == 1, "共享同一出口的号里只能有一个上台"


def test_configuring_proxies_unblocks_the_show():
    """同一批号配上独立代理后立刻可演——体检报告给出的是可执行的解法，不是死结。"""
    rows = [_account(f"a{i}", proxy_id=f"px-{i}") for i in range(4)]
    report = linkage_readiness(rows)
    assert report["max_concurrent"] == 4
    assert not [p for p in report["problems"] if not p.startswith("warn:")]


# ── 运营关联台账（config linkage_overrides） ─────────────────────────────────


def test_overrides_from_config_reads_the_ops_ledger():
    """台账从 ``companion.group_show.linkage_overrides`` 读；键值都归一成串。

    YAML 里不加引号的纯数字账号会被解析成 **int** 键——正是本机注册表账号的形态，
    归一化丢了它，台账就静默失效。
    """
    cfg = {"companion": {"group_show": {"linkage_overrides": {
        8244899900: "gray:a",          # int 键（YAML 不加引号的数字）
        "8755679833": " gray:b ",       # 两侧空白要洗掉
        "": "gray:c",                   # 空键丢弃
        "acc_no_group": "",             # 空值丢弃
    }}}}
    assert overrides_from_config(cfg) == {
        "8244899900": "gray:a", "8755679833": "gray:b"}


@pytest.mark.parametrize("junk", (
    None, {}, {"companion": None},
    {"companion": {"group_show": {"linkage_overrides": ["not", "a", "map"]}}},
    {"companion": {"group_show": {"linkage_overrides": "gray:a"}}},
    object(),
))
def test_overrides_from_config_tolerates_junk(junk):
    """任何取不出来的形态都按「没有台账」处理——绝不抛、绝不半张表。"""
    assert overrides_from_config(junk) == {}


def test_config_ledger_splits_a_shared_host_pair():
    """复刻 2026-07-27 双号灰度实况：两个 TG 号 ``proxy_id`` 全空。

    无台账 → 同落宿主兜底组（互斥，双角戏必 understaffed）；overlay 写台账声明
    出口独立 → 两组，可同台。台账是 ``derive_group`` 的正门，不是 allow_shared_host
    那道只许 dry-run 的后门。
    """
    rows = [{"account_id": "8244899900", "platform": "telegram"},
            {"account_id": "8755679833", "platform": "telegram"}]
    assert len(set(derive_fingerprint_groups(rows).values())) == 1, \
        "无台账时必须互斥——这是防关联的底线语义"
    ov = overrides_from_config({"companion": {"group_show": {
        "linkage_overrides": {
            "8244899900": "gray:test_a", "8755679833": "gray:test_b"}}}})
    fps = derive_fingerprint_groups(rows, overrides=ov)
    assert fps == {"8244899900": "gray:test_a", "8755679833": "gray:test_b"}
    report = linkage_readiness(rows, overrides=ov)
    assert report["max_concurrent"] == 2
    assert report["by_source"].get("override") == 2, \
        "体检要如实标注这两个号的独立性是台账**声明**出来的"
