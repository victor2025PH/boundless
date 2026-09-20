# -*- coding: utf-8 -*-
"""song_stock 纯函数门禁：意图检测/清单/选曲/账本/频控/备货盘点（零网络零实例）。"""
from __future__ import annotations

import json
import time

from src.companion import song_stock as ss


# ── 意图检测：宁可漏判不可误判 ────────────────────────────────────────────────
def test_detect_song_request_positives():
    for t in (
        "唱首歌给我听吧",
        "给我唱个歌呗",
        "你会唱歌吗",
        "想听你唱歌",
        "唱两句听听嘛",
        "来一首呗",
        "你可以清唱一段吗",
        "唱给我听好不好",
        "sing me a song please",
        "can you sing?",
        "I want to hear you sing",
    ):
        assert ss.detect_song_request(t), f"应判为要歌: {t}"


def test_detect_song_request_negatives():
    for t in (
        "",
        "我去唱歌了",
        "我在KTV唱歌呢",
        "我们去唱歌吧",
        "别唱了",
        "不要唱了求你",
        "他唱歌很好听",
        "昨天去唱K了",
        "唱什么唱",
        "今天天气不错",
        "singing in the rain is a great movie",
    ):
        assert not ss.detect_song_request(t), f"不该判为要歌: {t}"


def test_requested_song_scene():
    assert ss.requested_song_scene("今天我生日，唱首歌呗") == "birthday"
    assert ss.requested_song_scene("唱个摇篮曲哄我睡") == "goodnight"
    assert ss.requested_song_scene("sing me a lullaby") == "goodnight"
    assert ss.requested_song_scene("唱首歌") == ""


# ── 清单：坏行跳过、路径消毒、重复 id 拒绝 ────────────────────────────────────
def _write_manifest(d, rows):
    d.mkdir(parents=True, exist_ok=True)
    (d / ss.MANIFEST_NAME).write_text(
        json.dumps({"templates": rows}, ensure_ascii=False), encoding="utf-8")


def test_load_manifest_skips_bad_rows(tmp_path):
    d = tmp_path / "tpl"
    _write_manifest(d, [
        {"id": "a", "title": "甲", "file": "a.wav", "lyrics": "啦啦"},
        {"id": "", "title": "缺id", "file": "x.wav"},
        {"id": "b", "title": "路径穿越", "file": "../evil.wav"},
        {"id": "a", "title": "重复id", "file": "dup.wav"},
        "not-a-dict",
        {"id": "c", "title": "乙", "file": "c.wav", "lang": "EN",
         "scene": "Birthday", "enabled": False},
    ])
    rows = ss.load_song_manifest(d)
    assert [t.id for t in rows] == ["a", "c"]
    assert rows[1].lang == "en" and rows[1].scene == "birthday"
    assert rows[1].enabled is False


def test_load_manifest_missing_dir(tmp_path):
    assert ss.load_song_manifest(tmp_path / "nope") == []


# ── 选曲：语言闸/场景偏好/避重/确定性轮换 ─────────────────────────────────────
def _tpl(tid, lang="zh", scene="", enabled=True):
    return ss.SongTemplate(id=tid, title=tid, file=f"{tid}.wav",
                           lang=lang, scene=scene, enabled=enabled)


def test_pick_song_lang_gate_and_fallback():
    pool = [_tpl("zh1"), _tpl("zh2")]
    assert ss.pick_song(pool, lang="zh", variety_key="c", day_key="d") is not None
    # 英文会话无英文模板：默认不放宽（宁可不唱）
    assert ss.pick_song(pool, lang="en", variety_key="c", day_key="d") is None
    got = ss.pick_song(pool, lang="en", variety_key="c", day_key="d",
                       allow_lang_fallback=True)
    assert got is not None


def test_pick_song_scene_preference_and_exclusion():
    pool = [_tpl("generic"), _tpl("bday", scene="birthday")]
    got = ss.pick_song(pool, lang="zh", scene_hint="birthday",
                       variety_key="c", day_key="d")
    assert got is not None and got.id == "bday"
    # 场景曲被避重排除后回落通用池不硬套场景
    got2 = ss.pick_song(pool, lang="zh", scene_hint="birthday",
                        exclude_ids=["bday"], variety_key="c", day_key="d")
    assert got2 is not None and got2.id == "generic"
    # 全排除 → None
    assert ss.pick_song(pool, lang="zh", exclude_ids=["generic", "bday"],
                        variety_key="c", day_key="d") is None


def test_pick_song_deterministic_per_day():
    pool = [_tpl(f"t{i}") for i in range(5)]
    a = ss.pick_song(pool, lang="zh", variety_key="conv1", day_key="20260822")
    b = ss.pick_song(pool, lang="zh", variety_key="conv1", day_key="20260822")
    assert a is not None and b is not None and a.id == b.id
    picks = {ss.pick_song(pool, lang="zh", variety_key="conv1",
                          day_key=f"2026082{i}").id for i in range(8)}
    assert len(picks) > 1, "隔日应有轮换"


def test_disabled_templates_never_picked():
    pool = [_tpl("only", enabled=False)]
    assert ss.pick_song(pool, lang="zh", variety_key="c", day_key="d") is None


# ── 备货文件：尺寸地板 + 扩展名优先级 ────────────────────────────────────────
def test_find_stock_file(tmp_path):
    sroot = tmp_path / "stock"
    d = sroot / "lin_xiaoyu" / "songs"
    d.mkdir(parents=True)
    (d / "t1.ogg").write_bytes(b"OggS" + b"\x00" * ss.MIN_STOCK_BYTES)
    (d / "small.ogg").write_bytes(b"OggS123")   # 空壳：低于地板
    assert ss.find_stock_file("lin_xiaoyu", "t1", sroot=sroot) is not None
    assert ss.find_stock_file("lin_xiaoyu", "small", sroot=sroot) is None
    assert ss.find_stock_file("lin_xiaoyu", "missing", sroot=sroot) is None
    assert ss.find_stock_file("", "t1", sroot=sroot) is None


# ── 账本：记账/窗口查询/剪枝/坏文件容错 ──────────────────────────────────────
def test_ledger_roundtrip_and_prune(tmp_path):
    lp = tmp_path / "ledger.json"
    led = ss.SongSendLedger(lp)
    now = time.time()
    led.record("conv1", "t1", now=now - 3600)
    led.record("conv1", "t2", now=now)
    led.record("conv2", "tx", now=now)
    assert set(led.recent_template_ids("conv1", 1.0, now=now)) == {"t1", "t2"}
    assert led.recent_template_ids("conv1", 0.01, now=now) == ["t2"]
    assert led.count_since("conv1", now - 10) == 1
    assert led.last_ts("conv2") > 0
    # 重载持久性
    led2 = ss.SongSendLedger(lp)
    assert set(led2.recent_template_ids("conv1", 1.0, now=now)) == {"t1", "t2"}
    # 剪枝：老于 KEEP_DAYS 的行在下次保存时被清
    led3 = ss.SongSendLedger(tmp_path / "l3.json")
    led3.record("c", "old", now=now - ss.SongSendLedger.KEEP_DAYS * 86400 - 10)
    led3.record("c", "new", now=now)
    led4 = ss.SongSendLedger(tmp_path / "l3.json")
    assert led4.recent_template_ids("c", 9999, now=now) == ["new"]


def test_ledger_bad_file_tolerated(tmp_path):
    lp = tmp_path / "bad.json"
    lp.write_text("{not json", encoding="utf-8")
    led = ss.SongSendLedger(lp)
    assert led.recent_template_ids("c", 1.0) == []
    led.record("c", "t1")   # 记账仍可用（坏账本被替换）
    assert led.last_ts("c") > 0


# ── 频控裁决 ─────────────────────────────────────────────────────────────────
def test_song_gate_verdict():
    scfg = {"daily_cap": 2, "cooldown_hours": 6}
    now = time.time()
    ok, why = ss.song_gate_verdict(scfg, today_count=0, last_ts=0, now=now)
    assert ok and why == ""
    ok, why = ss.song_gate_verdict(scfg, today_count=2, last_ts=0, now=now)
    assert not ok and why == "capped_daily"
    ok, why = ss.song_gate_verdict(scfg, today_count=0,
                                   last_ts=now - 3600, now=now)
    assert not ok and why == "capped_cooldown"
    ok, _ = ss.song_gate_verdict(scfg, today_count=0,
                                 last_ts=now - 7 * 3600, now=now)
    assert ok
    # cap=0 / cooldown=0 = 关闭该闸
    ok, _ = ss.song_gate_verdict({"daily_cap": 0, "cooldown_hours": 0},
                                 today_count=99, last_ts=now - 1, now=now)
    assert ok


# ── 状态快照（观测面） ───────────────────────────────────────────────────────
def test_singing_status_snapshot(tmp_path):
    tdir = tmp_path / "tpl"
    _write_manifest(tdir, [
        {"id": "t1", "title": "一", "file": "t1.wav"},
        {"id": "t2", "title": "二", "file": "t2.wav", "enabled": False},
    ])
    sroot = tmp_path / "stock"
    d = sroot / "lin_xiaoyu" / "songs"
    d.mkdir(parents=True)
    (d / "t1.ogg").write_bytes(b"OggS" + b"\x00" * ss.MIN_STOCK_BYTES)
    cfg = {"companion": {"singing": {
        "enabled": True, "templates_dir": str(tdir), "stock_dir": str(sroot)}}}
    snap = ss.singing_status_snapshot(cfg, force=True)
    assert snap["enabled"] is True
    assert snap["templates"] == 2 and snap["templates_enabled"] == 1
    assert snap["stock"] == {"lin_xiaoyu": 1}
    assert "counters" in snap["stats"]


# ── 干声窗：静默终止 / 相对跌落 / 归一 / 歌词估时 ────────────────────────────
def _sine_wav(seconds, *, amp=0.2, freq=220.0, sr=8000, ch=1):
    """合成 PCM16 WAV（测试用，不进生产）。"""
    import io, math, struct, wave
    n = int(seconds * sr)
    vals = []
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / sr))
        vals.extend([v] * ch)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{len(vals)}h", *vals))
    return buf.getvalue()


def _cat_wavs(*chunks, sr=8000, ch=1):
    import io, struct, wave
    frames = []
    for raw in chunks:
        with wave.open(io.BytesIO(raw), "rb") as w:
            frames.append(w.readframes(w.getnframes()))
    raw = b"".join(frames)
    vals = struct.unpack(f"<{len(raw)//2}h", raw)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{len(vals)}h", *vals))
    return buf.getvalue()


def test_expected_sung_sec_cjk_and_floor():
    # 26 字慢板 ≈ 20.8s（0.8s/字，按 2026-08-22 选角轮实测校准——0.45s/字的
    # 说话语速估计曾把完整唱段在 17s 拦腰切断）
    assert 19.0 <= ss.expected_sung_sec("月亮爬上了窗台晚风把心事吹开我把想说的话慢慢唱给你猜") <= 22.0
    assert ss.expected_sung_sec("") == 12.0
    assert ss.expected_sung_sec("hi") == 6.0


def test_prepare_dry_vocal_trims_silence_tail():
    # A 型：8s 人声 + 10s 静音 → 裁到唱段
    voiced = _sine_wav(8.0, amp=0.25)
    silent = _sine_wav(10.0, amp=0.0)
    raw = _cat_wavs(voiced, silent)
    out, meta = ss.prepare_dry_vocal(raw)
    assert meta["ok"] is True
    assert meta["reason"] == "silence"
    assert 7.5 <= meta["end_t"] <= 9.0
    assert meta["out_dur"] < 11.0


def test_prepare_dry_vocal_cuts_relative_drop_bleed():
    # B 型：8s 人声 + 1s 半能量跌落（仍高于静默地板）+ 10s 乐器 → 相对跌落切断
    voiced = _sine_wav(8.0, amp=0.25, freq=220)
    dip = _sine_wav(1.0, amp=0.03, freq=440)
    bleed = _sine_wav(10.0, amp=0.22, freq=440)
    raw = _cat_wavs(voiced, dip, bleed)
    out, meta = ss.prepare_dry_vocal(raw)
    assert meta["ok"] is True
    assert meta["reason"] == "rel_drop"
    assert 7.5 <= meta["end_t"] <= 9.5
    assert meta["out_dur"] < 12.0


def test_prepare_dry_vocal_normalizes_and_stereo():
    quiet = _sine_wav(7.0, amp=0.02)
    out, meta = ss.prepare_dry_vocal(quiet)
    assert meta["ok"] is True
    assert meta["gain"] > 5.0
    import io, wave
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2


def test_prepare_dry_vocal_rejects_too_short():
    raw = _sine_wav(2.0, amp=0.2)
    out, meta = ss.prepare_dry_vocal(raw)
    assert meta["ok"] is False
    assert out == raw


def test_resolve_singing_cfg_defaults():
    scfg = ss.resolve_singing_cfg({})
    assert scfg["enabled"] is False
    assert scfg["daily_cap"] == 2
    assert scfg["allow_lang_fallback"] is False
    scfg2 = ss.resolve_singing_cfg({"companion": {"singing": "garbage"}})
    assert scfg2["enabled"] is False


# ── 实施66：双档检测 / 粘性窗 / 逼唱压力 / 文字假唱（2026-08-23 实录金标） ─────

def test_song_topic_state_incident_golden():
    """实录：「给我唱首歌吧」→ 拒 →「不行，必须唱」逃逸窄词表＝事故根因。
    粘性窗内含「唱」追加必须补判 demand；无粘性证据同句必须静默（防误唱）。"""
    assert ss.song_topic_state("不行，必须唱", sticky=True) == "demand"
    assert ss.song_topic_state("不行，必须唱", sticky=False) == ""
    assert ss.song_topic_state("给我唱首歌吧", sticky=False) == "demand"
    assert ss.song_topic_state("快点呀", sticky=True) == "loose"
    assert ss.song_topic_state("快点呀", sticky=False) == ""
    assert ss.song_topic_state("说好的呢", sticky=True) == "loose"


def test_song_topic_state_guards():
    assert ss.song_topic_state("别唱了别唱了", sticky=True) == ""
    assert ss.song_topic_state("必须唱票才能进场", sticky=True) == ""  # 固定搭配
    assert ss.song_topic_state("我去KTV唱歌了", sticky=True) == ""     # 自述
    long_txt = "这个方案必须今天定下来，不然我们就真的来不及了，抓紧一点"
    assert ss.song_topic_state(long_txt, sticky=True) == ""            # 超长不判
    assert ss.song_topic_state("", sticky=True) == ""


def test_sticky_helpers_roundtrip():
    uc: dict = {}
    assert not ss.song_sticky_active(uc)
    assert ss.song_pressure(uc) == 1
    p1 = ss.touch_song_request(uc, now=1000.0)
    assert p1 == 1
    assert ss.song_sticky_active(uc, now=1000.0 + 60)
    p2 = ss.touch_song_request(uc, now=1000.0 + 120)
    assert p2 == 2 and ss.song_pressure(uc) == 2
    assert not ss.song_sticky_active(
        uc, now=1000.0 + 120 + ss.SONG_STICKY_SEC + 1)
    # 窗口外重触 → 压力重置（旧逼唱不跨窗记仇）
    p3 = ss.touch_song_request(uc, now=1000.0 + 120 + ss.SONG_STICKY_SEC + 60)
    assert p3 == 1


def test_song_sticky_from_texts():
    assert ss.song_sticky_from_texts(["给我唱首歌吧", "哈哈"])
    assert not ss.song_sticky_from_texts(["今天好累", ""])
    assert not ss.song_sticky_from_texts(None)
    assert not ss.song_sticky_from_texts([])


def test_gate_pressure_exempts_cooldown_not_cap():
    """P0-4：被动逼唱 ≥2 豁免**冷却**（encore 是真需求）；日帽绝不豁免。"""
    scfg = {"daily_cap": 2, "cooldown_hours": 6}
    now = time.time()
    ok, why = ss.song_gate_verdict(
        scfg, today_count=1, last_ts=now - 600, now=now, demand_pressure=2)
    assert ok is True and why == "pressure_exempt"
    ok2, why2 = ss.song_gate_verdict(
        scfg, today_count=1, last_ts=now - 600, now=now, demand_pressure=1)
    assert ok2 is False and why2 == "capped_cooldown"
    ok3, why3 = ss.song_gate_verdict(
        scfg, today_count=2, last_ts=now - 600, now=now, demand_pressure=5)
    assert ok3 is False and why3 == "capped_daily"


_INCIDENT_REPLY = (
    "哎妈呀，你这是要逼我现眼啊……"
    "行，那我唱了，就一句啊，跑调了不许笑我。"
    "\"月亮代表我的心～\"……"
    "完了，我自己先起鸡皮疙瘩了。")


def test_detect_song_performance_incident_golden():
    """实录四连必须命中（宣告+唱词双证据，无需语境）。"""
    assert ss.detect_song_performance(_INCIDENT_REPLY)
    assert ss.detect_song_performance(_INCIDENT_REPLY, song_context=True)


def test_detect_song_performance_counterexamples():
    # 夸对方唱歌（自评词单独出现）
    assert not ss.detect_song_performance(
        "你唱得真好听，别笑我不懂欣赏", song_context=True)
    # 经历句（时长前瞻排除）
    assert not ss.detect_song_performance(
        "我唱了三年京剧，别笑我", song_context=True)
    # 讨论歌词（无语境单证据）
    assert not ss.detect_song_performance(
        "这首歌词是\"月亮代表我的心～\"，超经典")
    # 诚实拒唱（自评词+远期承诺，绝不能被误剥）
    assert not ss.detect_song_performance(
        "今天嗓子哑了，改天唱给你～", song_context=True)
    assert not ss.detect_song_performance("")


def test_detect_song_performance_context_combos():
    t = "\"晚风轻轻吹过～\"……跑调了别笑我嘛"
    assert ss.detect_song_performance(t, song_context=True)   # 唱词+自评
    assert not ss.detect_song_performance(t, song_context=False)
    t2 = "行，那我唱了哈，跑调了不许笑"
    assert ss.detect_song_performance(t2, song_context=True)  # 宣告+自评
    assert not ss.detect_song_performance(t2, song_context=False)


def test_strip_song_performance_keeps_neutral():
    out = ss.strip_song_performance(_INCIDENT_REPLY)
    assert "月亮代表我的心" not in out
    assert "那我唱了" not in out
    assert "鸡皮疙瘩" not in out
    assert "现眼" in out   # 中性打趣句保留


def test_strip_empty_and_deflection_line():
    only_perf = "那我唱了。\"月亮代表我的心～\"……"
    assert ss.strip_song_performance(only_perf) == ""
    line = ss.song_deflection_line("zh", key="c1")
    assert line and line == ss.song_deflection_line("zh", key="c1")
    assert ss.song_deflection_line("en", key="c1")
    assert "唱" not in ss.song_deflection_line("en", key="c1")


def test_snapshot_reports_ledger_exists(tmp_path, monkeypatch):
    """P0-5「零发送一眼可见」：账本不存在 → ledger_exists=False。"""
    led = ss.SongSendLedger(tmp_path / "ledger.json")
    monkeypatch.setattr(ss, "_LEDGER", led)
    snap = ss.singing_status_snapshot({}, root=tmp_path, force=True)
    assert snap.get("ledger_exists") is False
    led.record("c1", "t1")
    snap2 = ss.singing_status_snapshot({}, root=tmp_path, force=True)
    assert snap2.get("ledger_exists") is True
