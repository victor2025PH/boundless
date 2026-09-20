"""群戏剧本 Schema 门禁 —— 守「数据契约」这条地基。

``playbook`` 是本包所有模块的单一事实源：选角读 ``roles``、导演读 ``beats``、
落库读 ``ShowState``、ECP 读 ``BeatDirective``。它一旦悄悄改语义，上面四层全部
跟着错，而且错得很安静（不会抛，只会演得不对）。所以这里钉的主要是两类东西：

1. **契约常量的语义**——``DEFAULT_MUST_NOT`` 四条禁止项是 2026-07-25 灰度实测
   三连击换来的，删一条就等于把那个事故重新放回线上；
2. **容错方向**——剧本是人手写的 YAML，脏数据是常态不是异常。``load_playbook``
   / ``playbook_from_dict`` 承诺软失败，``validate_playbook`` 承诺「硬错直接给
   文案、软警告带 ``warn:`` 前缀」。谁静默放行谁就是事故温床。
"""
from __future__ import annotations

import pytest

from src.companion.group_show.playbook import (
    DEFAULT_MUST_NOT,
    EVENT_KINDS,
    PACE_BASE_SECONDS,
    SHOW_STATUSES,
    SOFT_AD_MAX,
    STANDARD_SLOTS,
    VALID_SYSTEMS,
    Beat,
    BeatDirective,
    CastMember,
    Casting,
    Playbook,
    Role,
    ShowEvent,
    ShowState,
    load_playbook,
    load_playbook_dir,
    playbook_from_dict,
    validate_playbook,
)


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _good_playbook(**kw) -> Playbook:
    """一份四角俱全、能通过全部硬校验的剧本（各用例在此之上只改一处）。"""
    base = dict(
        id="pb_ok",
        name="标准四角戏",
        system="growth",
        products=("matrixx",),
        soft_ad_level=5,
        roles=(Role("asker"), Role("advocate"), Role("bystander"), Role("skeptic")),
        beats=(
            Beat("b1", "asker", "抛痛点：多号管理切到崩溃", soft=0),
            Beat("b2", "advocate", "分享自己在用的东西", product="matrixx"),
            Beat("b3", "skeptic", "泼一句冷水"),
            Beat("b4", "bystander", "附和一句"),
        ),
    )
    base.update(kw)
    return Playbook(**base)


def _hard(problems) -> list:
    """只取硬错（软警告一律带 ``warn:`` 前缀，这个约定本身也被下面的用例钉住）。"""
    return [p for p in problems if not p.startswith("warn:")]


# ── 契约常量 ────────────────────────────────────────────────────────────────


def test_default_must_not_still_carries_all_four_incident_lessons():
    """``DEFAULT_MUST_NOT`` 是事故清单不是文案建议，删任何一条＝把那次事故放回线上。

    2026-07-25 群聊灰度三连击：进群自我介绍像私聊开场 / 把群里闲聊当「对你说」/
    答非所问长篇输出。第三条「不要提及或编造私聊经历」还兼任 ECP 账号隔离墙的
    文本层兜底——结构层漏了它是最后一道网。
    """
    joined = "\n".join(DEFAULT_MUST_NOT)
    assert "自我介绍" in joined
    assert "私聊" in joined, "私聊经历禁止项是账号隔离墙的文本层兜底，不能删"
    assert "广告腔" in joined or "刷屏" in joined
    assert "长篇" in joined
    assert len(DEFAULT_MUST_NOT) >= 4


def test_pace_table_covers_exactly_the_three_documented_gears():
    """语速档是剧本与 ``pacing`` 之间的键约定：多一档少一档都会让排期静默回落 normal。"""
    assert set(PACE_BASE_SECONDS) == {"chatty", "normal", "slow"}
    assert PACE_BASE_SECONDS["chatty"] < PACE_BASE_SECONDS["normal"] < PACE_BASE_SECONDS["slow"]


def test_standard_slots_and_statuses_are_the_shared_vocabulary():
    """四角标准戏与场次状态机是跨模块共享词汇（选角/导演/落库都按它取值）。"""
    assert set(STANDARD_SLOTS) == {"asker", "advocate", "bystander", "skeptic"}
    assert "skeptic" in STANDARD_SLOTS, "质疑角是可信度关键角，不能从标准戏里消失"
    assert set(SHOW_STATUSES) >= {"pending", "running", "done", "aborted"}
    assert set(EVENT_KINDS) >= {"line", "media", "human"}


# ── Beat / Playbook 取值语义 ────────────────────────────────────────────────


def test_beat_soft_minus_one_means_inherit_not_zero():
    """``soft=-1`` 是「没写，跟剧本走」，绝不能被当成「写了 0」。

    两者语义天差地别：继承＝按剧本级强度种草，0＝这拍一个字产品都不许提。
    """
    assert Beat("b", "r", "i", soft=-1).effective_soft(7) == 7


def test_beat_soft_zero_is_an_explicit_silence_order():
    """``soft=0`` 是硬意图（开场拍纯闲聊），必须原样生效而不是被剧本级覆盖掉。"""
    assert Beat("b", "r", "i", soft=0).effective_soft(9) == 0


def test_beat_level_soft_overrides_playbook_level():
    assert Beat("b", "r", "i", soft=3).effective_soft(9) == 3


def test_beat_at_returns_none_past_the_end_meaning_wrap_up():
    """越界＝演完了。返回 None 而不是抛，导演据此判「该收尾」。"""
    pb = _good_playbook()
    assert pb.beat_at(0).id == "b1"
    assert pb.beat_at(len(pb.beats)) is None
    assert pb.beat_at(-1) is None, "负游标是脏状态，不能悄悄取到最后一拍"


def test_slots_preserves_declaration_order():
    """选角要按声明序输出演员表，靠的就是这个顺序，不能被排序或去重打乱。"""
    assert _good_playbook().slots == ("asker", "advocate", "bystander", "skeptic")


# ── Casting / CastMember 查询语义 ───────────────────────────────────────────


def test_account_key_is_platform_qualified():
    """同一串 id 在两个平台是两个号，账号唯一键必须带平台前缀。"""
    assert CastMember("asker", "a1", "p1", platform="whatsapp").account_key == "whatsapp:a1"


def test_casting_lookups_return_none_instead_of_raising():
    """查不到就是查不到——导演拿 None 去走替补逻辑，抛 KeyError 会掀翻整场戏。"""
    casting = Casting(members=(CastMember("asker", "a1", "p1"),), unfilled=("skeptic",))
    assert casting.by_slot("asker").account_id == "a1"
    assert casting.by_slot("skeptic") is None
    assert casting.by_account("a1").slot == "asker"
    assert casting.by_account("nope") is None
    assert casting.filled_slots == ("asker",)


def test_by_account_coerces_numeric_ids():
    """账号 id 从库里捞出来可能是 int，查询不该因为类型不同就查不到。"""
    casting = Casting(members=(CastMember("asker", "12345", "p1"),))
    assert casting.by_account(12345) is not None


# ── ShowState 记账语义 ──────────────────────────────────────────────────────


def test_next_seq_starts_at_one_because_store_drops_seq_zero():
    """落库层把 ``seq<=0`` 的事件直接丢掉，所以序号必须从 1 起。

    这两处是一对隐式契约：``next_seq`` 若哪天改成从 0 起，第一条台词会在落库时
    被静默吞掉，续演时看不到开场——而且不会有任何报错。
    """
    state = ShowState(session_id="s", group_key="g",
                      playbook=_good_playbook(), casting=Casting())
    assert state.next_seq == 1
    state.append_event(ShowEvent(seq=1, ts=1.0, speaker_account="a1",
                                 role="asker", beat_id="b1", text="hi"))
    assert state.next_seq == 2


def test_last_event_is_none_on_a_fresh_show():
    """开场时没有「上一条」，导演的承接判断依赖这个 None。"""
    state = ShowState(session_id="s", group_key="g",
                      playbook=_good_playbook(), casting=Casting())
    assert state.last_event() is None


def test_lines_by_account_counts_only_what_was_really_said():
    """刷屏闸的分母口径：只算真发出去的 line/media。

    把 human（真人原话）或 yield（导演控制事件）算进某个号的发言数，会让刷屏闸
    在错误的时刻触发换人——它保护的是「别让一个号包场」，不是「统计事件条数」。
    """
    state = ShowState(session_id="s", group_key="g",
                      playbook=_good_playbook(), casting=Casting())
    for kind in ("line", "media", "human", "yield", "terminate"):
        state.append_event(ShowEvent(seq=state.next_seq, ts=1.0,
                                     speaker_account="a1", role="asker",
                                     beat_id="b1", text="x", kind=kind))
    assert state.lines_by_account("a1") == 2
    assert state.lines_by_account(0) == 0


def test_current_beat_follows_the_cursor():
    state = ShowState(session_id="s", group_key="g",
                      playbook=_good_playbook(), casting=Casting(), beat_cursor=2)
    assert state.current_beat.id == "b3"


def test_show_state_defaults_to_dry_run():
    """**安全缺省**：忘了设 ``dry_run`` 时必须是排练档，绝不能默认真发。"""
    state = ShowState(session_id="s", group_key="g",
                      playbook=_good_playbook(), casting=Casting())
    assert state.dry_run is True
    assert state.status == "pending"


def test_beat_directive_defaults_carry_the_ban_list():
    """任何一条 directive 忘了带禁止项，生成层就会裸奔——所以缺省值必须已经带上。"""
    assert BeatDirective(beat_id="b", intent="i").must_not == DEFAULT_MUST_NOT


# ── validate_playbook：硬错 ─────────────────────────────────────────────────


def test_a_well_formed_playbook_has_no_hard_problems():
    """基准锚点：标准四角戏必须干净通过，否则下面所有「只改一处」的用例都不可信。"""
    assert _hard(validate_playbook(_good_playbook())) == []


def test_empty_id_is_hard_because_sessions_are_keyed_by_it():
    """场次落库、续演、转化归因全靠 playbook id 串起来，空 id 等于这场戏无法归档。"""
    assert any("id" in p for p in _hard(validate_playbook(_good_playbook(id=" "))))


def test_playbook_without_beats_is_hard():
    """没有 beats ＝ 没有戏。导演拿到空队列会立刻判 completed，等于白开一场。"""
    assert any("beats" in p for p in _hard(validate_playbook(_good_playbook(beats=()))))


def test_unknown_system_is_hard():
    """产品系与官网 brand 对齐，写错会让软广口径挂到不存在的产品线上。"""
    problems = _hard(validate_playbook(_good_playbook(system="unknown_sys")))
    assert any("system" in p for p in problems)
    assert all(s in VALID_SYSTEMS for s in ("growth", "studio", "lingo"))


def test_empty_system_is_allowed():
    """system 是可选的（通用剧本不绑产品线），空值不该被当成写错。"""
    assert _hard(validate_playbook(_good_playbook(system=""))) == []


@pytest.mark.parametrize("level", [-1, SOFT_AD_MAX + 1, 999])
def test_soft_ad_level_out_of_range_is_hard(level):
    """软广强度越界会让 ECP 的分档提示落到边界外，生成层拿到无意义的口径。"""
    assert any("soft_ad_level" in p
               for p in _hard(validate_playbook(_good_playbook(soft_ad_level=level))))


def test_duplicate_role_slot_is_hard():
    """两个同名槽 → 导演按槽取人只会取到第一个，第二个号整场干坐着。"""
    pb = _good_playbook(roles=(Role("asker"), Role("asker"), Role("advocate"),
                               Role("skeptic")))
    assert any("重复 slot" in p for p in _hard(validate_playbook(pb)))


def test_duplicate_beat_id_is_hard_because_dequeue_matches_by_id():
    """导演按 ``beat_id`` 精确出队，重复 id 会让一次发言把两拍同时踢出队列。"""
    pb = _good_playbook(beats=(Beat("b1", "asker", "i1"), Beat("b1", "advocate", "i2")))
    assert any("重复" in p for p in _hard(validate_playbook(pb)))


def test_blank_beat_id_is_hard():
    pb = _good_playbook(beats=(Beat("  ", "asker", "i1"),))
    assert any("id 为空" in p for p in _hard(validate_playbook(pb)))


def test_beat_referencing_an_undeclared_slot_is_hard():
    """引用了没声明的槽 → 选角永远配不上人 → 这一拍要么被替补硬顶要么整拍失踪。"""
    pb = _good_playbook(beats=(Beat("b1", "ghost_role", "i1"),))
    assert any("未声明的角色槽" in p for p in _hard(validate_playbook(pb)))


def test_beat_without_intent_is_hard():
    """意图是导演给生成层的唯一输出——空意图＝让 LLM 自由发挥，那就不是演戏了。"""
    pb = _good_playbook(beats=(Beat("b1", "asker", "   "),))
    assert any("intent" in p for p in _hard(validate_playbook(pb)))


def test_beat_product_must_be_declared_at_playbook_level():
    """拍里植入了剧本没声明的产品 → 软广走到了这场戏的授权范围之外。"""
    pb = _good_playbook(beats=(Beat("b1", "asker", "i", product="not_declared"),))
    assert any("未在 playbook.products 声明" in p for p in _hard(validate_playbook(pb)))


def test_product_check_is_skipped_when_playbook_declares_nothing():
    """剧本没声明 products ＝ 没设授权白名单，此时不该反过来判每一拍都违规。"""
    pb = _good_playbook(products=(), beats=(Beat("b1", "asker", "i", product="x"),))
    assert not any("products" in p for p in _hard(validate_playbook(pb)))


def test_beat_soft_out_of_range_is_hard_but_minus_one_is_not():
    """``-1`` 是「继承」的哨兵值，不能和真正的越界值混为一谈。"""
    over = _good_playbook(beats=(Beat("b1", "asker", "i", soft=99),))
    assert any("soft 越界" in p for p in _hard(validate_playbook(over)))
    inherit = _good_playbook(beats=(Beat("b1", "asker", "i", soft=-1),))
    assert not any("soft 越界" in p for p in _hard(validate_playbook(inherit)))


def test_unknown_pace_is_hard_at_validation_even_though_runtime_degrades():
    """排期是热路径、会安静回落 normal；拼写错误必须在这道离线校验里被抓住。

    两边职责刻意不同：``pacing`` 不抛（真发时宁可降级），``validate_playbook``
    要吵（上线前必须发现）。少了这道校验，``pace: 快`` 会一直被当 normal 演。
    """
    pb = _good_playbook(beats=(Beat("b1", "asker", "i", pace="快"),))
    assert any("pace" in p for p in _hard(validate_playbook(pb)))


# ── validate_playbook：软警告 ───────────────────────────────────────────────


def test_missing_skeptic_is_only_a_warning():
    """缺质疑角会让戏一边倒好评（可信度打折），但戏能演——必须是软警告不是硬错。"""
    pb = _good_playbook(
        roles=(Role("asker"), Role("advocate")),
        beats=(Beat("b1", "asker", "i1"), Beat("b2", "advocate", "i2")),
    )
    problems = validate_playbook(pb)
    assert _hard(problems) == []
    assert any(p.startswith("warn:") and "skeptic" in p for p in problems)


def test_warn_prefix_is_the_only_way_callers_tell_blocking_from_advisory():
    """``warn:`` 前缀是调用方区分「不能开」与「能开但打折」的唯一依据，不能改写法。"""
    pb = _good_playbook(id="", roles=(Role("asker"),),
                        beats=(Beat("b1", "asker", "i"),))
    problems = validate_playbook(pb)
    assert any(p.startswith("warn:") for p in problems)
    assert _hard(problems), "同时存在硬错时，硬错不能被 warn 前缀掩盖"


# ── playbook_from_dict：容错 ────────────────────────────────────────────────


def test_from_dict_on_empty_and_none_gives_an_empty_shell_not_a_crash():
    """载入器碰到空文件不该炸；产出的空壳会在 ``validate_playbook`` 里被判死。"""
    for raw in ({}, None):
        pb = playbook_from_dict(raw)
        assert pb.id == "" and pb.beats == ()


def test_from_dict_normalizes_system_and_pace_casing_and_whitespace():
    """YAML 是人手打的，``System: Growth `` / ``pace: ' CHATTY'`` 都会出现。"""
    pb = playbook_from_dict({
        "id": " pb ", "system": " GROWTH ",
        "roles": [{"slot": " asker "}],
        "beats": [{"id": " b1 ", "role": " asker ", "intent": " i ", "pace": " CHATTY "}],
    })
    assert pb.id == "pb" and pb.system == "growth"
    assert pb.slots == ("asker",)
    assert pb.beats[0].pace == "chatty" and pb.beats[0].id == "b1"


def test_from_dict_accepts_a_bare_string_role():
    """``roles: [asker, advocate]`` 这种简写在手写剧本里最常见，必须认。"""
    pb = playbook_from_dict({"id": "x", "roles": ["asker", "advocate"]})
    assert pb.slots == ("asker", "advocate")


def test_from_dict_wraps_a_single_product_string():
    """``products: matrixx``（漏写列表）不该变成逐字符的产品清单。"""
    assert playbook_from_dict({"id": "x", "products": "matrixx"}).products == ("matrixx",)


def test_from_dict_drops_blank_products():
    assert playbook_from_dict({"id": "x", "products": ["a", "", "  "]}).products == ("a",)


@pytest.mark.parametrize("bad", ["abc", None, [], {}])
def test_from_dict_falls_back_on_dirty_soft_ad_level(bad):
    """强度写成非数字时回落缺省 5，而不是让整份剧本载不进来。"""
    assert playbook_from_dict({"id": "x", "soft_ad_level": bad}).soft_ad_level == 5


@pytest.mark.parametrize("bad", ["abc", None, {}, [1]])
def test_from_dict_falls_back_on_dirty_beat_soft(bad):
    """脏 soft 回落 ``-1``（继承），而不是回落 0——0 会静默把这拍变成禁言拍。"""
    pb = playbook_from_dict({
        "id": "x", "roles": ["asker"],
        "beats": [{"id": "b", "role": "asker", "intent": "i", "soft": bad}],
    })
    assert pb.beats[0].soft == -1


def test_from_dict_defaults_name_to_id():
    assert playbook_from_dict({"id": "pb_x"}).name == "pb_x"


def test_from_dict_output_never_makes_validate_playbook_explode():
    """**跨函数不变量**：载入器的产物必须永远能被校验器消费。

    这两个函数是一条流水线（YAML → dict → Playbook → 校验）。载入器若放出一个
    校验器噎得住的对象，运营看到的就是一个 traceback 而不是「你的剧本哪里写错了」。
    """
    hostile = {
        "id": 123, "name": None, "system": ["growth"], "products": {"a": 1},
        "soft_ad_level": "x",
        "roles": [None, "", {"slot": None}, {"slot": "asker"}],
        "beats": [None, {}, {"id": "b", "role": "asker", "intent": "i", "soft": "z",
                            "pace": None}],
    }
    problems = validate_playbook(playbook_from_dict(hostile))
    assert isinstance(problems, list) and problems


@pytest.mark.parametrize("field", ["roles", "beats"])
def test_from_dict_should_tolerate_non_iterable_collections(field):
    pb = playbook_from_dict({"id": "x", field: 5})
    assert getattr(pb, field) == ()


def test_validate_playbook_should_not_raise_on_non_numeric_soft():
    problems = validate_playbook(Playbook(
        id="x", name="x", soft_ad_level="abc",
        roles=(Role("asker"),), beats=(Beat("b", "asker", "i", soft="3"),)))
    assert problems


# ── load_playbook / load_playbook_dir：软失败 ───────────────────────────────


def test_load_playbook_returns_none_for_a_missing_file(tmp_path):
    """剧本文件没了不该让编排链崩——返回 None，调用方跳过这份剧本。"""
    assert load_playbook(tmp_path / "nope.yaml") is None


def test_load_playbook_returns_none_for_broken_yaml(tmp_path):
    """半截 YAML（编辑器崩溃 / 传输截断）是真实运营场景，必须软失败。"""
    p = tmp_path / "broken.yaml"
    p.write_text("id: x\n  roles: [\n   - bad", encoding="utf-8")
    assert load_playbook(p) is None


@pytest.mark.parametrize("content", ["- just\n- a\n- list\n", "just a string\n", ""])
def test_load_playbook_rejects_non_mapping_documents(tmp_path, content):
    """YAML 顶层不是映射 → 不是剧本 → None，而不是拿一个残废对象往下走。"""
    p = tmp_path / "x.yaml"
    p.write_text(content, encoding="utf-8")
    assert load_playbook(p) is None


def test_load_playbook_rejects_a_playbook_without_id(tmp_path):
    """无 id 的剧本进不了 ``{id: Playbook}`` 索引，放行只会在后面变成 KeyError。"""
    p = tmp_path / "x.yaml"
    p.write_text("name: 没有 id 的剧本\n", encoding="utf-8")
    assert load_playbook(p) is None


def test_load_playbook_round_trips_a_real_file(tmp_path):
    p = tmp_path / "pb.yaml"
    p.write_text(
        "id: pb_demo\n"
        "name: 演示\n"
        "system: growth\n"
        "products: [matrixx]\n"
        "roles:\n  - slot: asker\n  - slot: advocate\n"
        "beats:\n"
        "  - {id: b1, role: asker, intent: 抛痛点, soft: 0}\n"
        "  - {id: b2, role: advocate, intent: 种草, product: matrixx, pace: chatty}\n",
        encoding="utf-8",
    )
    pb = load_playbook(p)
    assert pb is not None and pb.id == "pb_demo"
    assert pb.slots == ("asker", "advocate")
    assert pb.beats[0].soft == 0 and pb.beats[1].pace == "chatty"


def test_load_playbook_dir_returns_empty_for_a_non_directory(tmp_path):
    assert load_playbook_dir(tmp_path / "nope") == {}
    assert load_playbook_dir(None) == {}


def test_load_playbook_dir_skips_bad_files_instead_of_failing_the_batch(tmp_path):
    """**批量载入的关键不变量**：一份坏剧本不能拖垮其余所有剧本。

    运营在剧本目录里手改一份改坏了，整个群戏子系统「一份剧本都没有」是最糟糕的
    失败模式——因为看起来像功能没开，而不是像某个文件坏了。
    """
    (tmp_path / "ok.yaml").write_text("id: good\nname: 好的\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    (tmp_path / "noid.yaml").write_text("name: 无 id\n", encoding="utf-8")
    loaded = load_playbook_dir(tmp_path)
    assert set(loaded) == {"good"}


def test_load_playbook_dir_only_picks_up_dot_yaml(tmp_path):
    """只认 ``*.yaml``——``.yml`` 会被静默忽略，这个约定必须显式钉住。

    钉它不是因为它对，而是因为它安静：运营存成 ``x.yml`` 后剧本不出现在列表里，
    没有任何报错可查。哪天要放宽，也该是改这条测试而不是意外发现。
    """
    (tmp_path / "a.yaml").write_text("id: a\n", encoding="utf-8")
    (tmp_path / "b.yml").write_text("id: b\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("id: c\n", encoding="utf-8")
    assert set(load_playbook_dir(tmp_path)) == {"a"}
