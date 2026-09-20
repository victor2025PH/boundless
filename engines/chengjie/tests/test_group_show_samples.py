# -*- coding: utf-8 -*-
"""剧本台词样例门禁（`src/companion/group_show/samples.py`）。

样例是给**市场侧选戏**看的「客户真会读到的话」。它坏掉的两种方式都不报错：

1. **陈旧样例照常展示**——剧本改了、样例还是旧的，看起来完全正常，而运营会照着一份
   与当前剧本对不上的文案做投放判断。指纹必须覆盖所有影响台词的字段。
2. **占位台词冒充真台词**——stub 生成的是「台词1台词2」，存进去等于骗人。只有真 LLM
   排练的产物才入库（该判定在路由层，这里钉住纯函数不挑真人插话那一半）。

另有一条落盘纪律：写入只能进实例可写区，**绝不写仓库 config/playbooks/**（共享只读
代码根 + 被 git 跟踪，写进去会把仓库改脏、打包态根本存不下）。
"""
import json

import pytest

from src.companion.group_show.samples import (
    MAX_ENTRIES,
    SAMPLE_MAX_CHARS,
    build_sample_entry,
    is_stale,
    load_samples,
    pick_sample_lines,
    playbook_fingerprint,
    samples_for,
    samples_path,
    save_sample,
)


class _Beat:
    def __init__(self, bid, role="advocate", intent="种草", product="",
                 soft=3, pace="normal"):
        self.id, self.role, self.intent = bid, role, intent
        self.product, self.soft, self.pace = product, soft, pace


class _PB:
    def __init__(self, pid="duo_matrixx", name="双号对话", soft_ad_level=4,
                 beats=None):
        self.id, self.name, self.soft_ad_level = pid, name, soft_ad_level
        self.beats = tuple(beats or [_Beat("b1"), _Beat("b2", soft=7)])


class _Line:
    def __init__(self, seq, text, soft_level=0, kind="line",
                 role="advocate", beat_id="b1"):
        self.seq, self.text, self.soft_level = seq, text, soft_level
        self.kind, self.role, self.beat_id = kind, role, beat_id


# ── 指纹：任何影响台词的改动都必须让旧样例失效 ────────────────────────────────


@pytest.mark.parametrize("mutate", [
    lambda pb: setattr(pb, "soft_ad_level", 9),
    lambda pb: setattr(pb, "beats", (_Beat("b1", intent="换了个意图"),)),
    lambda pb: setattr(pb, "beats", (_Beat("b1", soft=10), _Beat("b2", soft=7))),
    lambda pb: setattr(pb, "beats", (_Beat("b1", role="skeptic"), _Beat("b2"))),
    lambda pb: setattr(pb, "beats", (_Beat("b1", product="matrixx"), _Beat("b2"))),
    lambda pb: setattr(pb, "beats", (_Beat("b1", pace="slow"), _Beat("b2"))),
    lambda pb: setattr(pb, "beats", (_Beat("b1"),)),           # 删了一拍
])
def test_fingerprint_changes_when_copy_relevant_field_changes(mutate):
    pb = _PB()
    before = playbook_fingerprint(pb)
    mutate(pb)
    assert playbook_fingerprint(pb) != before, "改动没让指纹变 → 会展示对不上的旧样例"


def test_fingerprint_ignores_display_name():
    """改个显示名不影响台词，不该白白作废一批还准确的样例。"""
    pb = _PB()
    before = playbook_fingerprint(pb)
    pb.name = "换个名字"
    assert playbook_fingerprint(pb) == before


def test_fingerprint_is_stable_across_calls():
    pb = _PB()
    assert playbook_fingerprint(pb) == playbook_fingerprint(_PB())


def test_is_stale_detects_mismatch_and_missing():
    assert is_stale(None, "abc") is True
    assert is_stale({}, "abc") is True
    assert is_stale({"fp": "xyz"}, "abc") is True
    assert is_stale({"fp": "abc"}, "abc") is False


# ── 选句：种草那句 + 开场，且真人插话绝不混入 ─────────────────────────────────


def test_picks_the_highest_soft_line_because_that_is_the_selling_moment():
    lines = [_Line(1, "开场闲聊", 0), _Line(2, "中间", 2), _Line(3, "种草那句", 7)]
    picked = pick_sample_lines(lines)
    texts = [p["text"] for p in picked]
    assert "种草那句" in texts, "没挑出软广最高的一句，市场看不到露不露骨"
    assert "开场闲聊" in texts, "没挑出开场，看不出像不像真人起头"


def test_human_interjections_never_become_samples():
    """真人插话是运营自己敲的测试话，混进样例＝拿自己的话冒充 AI 的话。"""
    lines = [_Line(1, "AI 说的", 3), _Line(2, "我自己敲的", 9, kind="human")]
    picked = pick_sample_lines(lines)
    assert [p["text"] for p in picked] == ["AI 说的"]


def test_empty_or_blank_lines_produce_no_sample():
    assert pick_sample_lines([]) == []
    assert pick_sample_lines([_Line(1, "   ")]) == []
    assert build_sample_entry(_PB(), []) is None


def test_samples_keep_original_order():
    picked = pick_sample_lines([_Line(5, "后面那句", 9), _Line(1, "开场", 0)])
    assert [p["text"] for p in picked] == ["开场", "后面那句"]


def test_overlong_line_is_truncated():
    long_text = "啊" * (SAMPLE_MAX_CHARS + 50)
    picked = pick_sample_lines([_Line(1, long_text, 5)])
    assert len(picked[0]["text"]) <= SAMPLE_MAX_CHARS + 1


# ── 落盘：只写传入的可写目录，读写闭环，坏文件不炸 ────────────────────────────


def test_save_then_read_roundtrip(tmp_path):
    pb = _PB()
    entry = build_sample_entry(pb, [_Line(1, "开场", 0), _Line(2, "种草", 7)])
    assert save_sample(tmp_path, pb.id, entry) is True
    got = samples_for(tmp_path, pb)
    assert got["stale"] is False and len(got["lines"]) == 2
    assert got["ts"] > 0


def test_edited_playbook_hides_old_samples_but_says_why(tmp_path):
    """陈旧样例必须停止展示，但要如实说「有过样例、剧本改了」——和「从没排过」
    该给的指引完全不同（前者重排一次即可，后者要先教用户勾真 LLM）。"""
    pb = _PB()
    save_sample(tmp_path, pb.id, build_sample_entry(pb, [_Line(1, "旧台词", 5)]))
    pb.soft_ad_level = 9                      # 剧本改了
    got = samples_for(tmp_path, pb)
    assert got["lines"] == [], "剧本改过还在展示旧台词"
    assert got["stale"] is True, "没告诉前端这是陈旧而非从未排练"


def test_never_rehearsed_is_not_reported_as_stale(tmp_path):
    got = samples_for(tmp_path, _PB(pid="never_run"))
    assert got["lines"] == [] and got["stale"] is False


def test_unwritable_target_is_skipped_not_crashed():
    """拿不到可写位置就不存——少一份样例远好过污染共享代码根。"""
    assert samples_path(None) is None
    assert save_sample(None, "pid", {"fp": "x", "ts": 1, "lines": []}) is False
    assert load_samples(None) == {}


def test_corrupt_file_is_treated_as_empty(tmp_path):
    (tmp_path / "playbook_samples.json").write_text("{ not json",
                                                    encoding="utf-8")
    assert load_samples(tmp_path) == {}
    assert samples_for(tmp_path, _PB())["lines"] == []


def test_library_is_capped_and_drops_oldest(tmp_path):
    for i in range(MAX_ENTRIES + 5):
        save_sample(tmp_path, f"pb{i}",
                    {"fp": "f", "ts": float(i), "lines": [{"text": "x"}]})
    data = json.loads((tmp_path / "playbook_samples.json").read_text(
        encoding="utf-8"))
    assert len(data) <= MAX_ENTRIES
    assert "pb0" not in data, "超量时该先丢最旧的"


def test_saving_one_playbook_does_not_clobber_others(tmp_path):
    save_sample(tmp_path, "a", {"fp": "f", "ts": 1.0, "lines": [{"text": "A"}]})
    save_sample(tmp_path, "b", {"fp": "f", "ts": 2.0, "lines": [{"text": "B"}]})
    data = load_samples(tmp_path)
    assert set(data) == {"a", "b"}
