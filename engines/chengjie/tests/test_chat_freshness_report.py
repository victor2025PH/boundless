# -*- coding: utf-8 -*-
"""聊天新鲜度观测工具门禁（tools/chat_freshness_report.py，2026-08-03）。

纯函数吃构造 rows（不碰真库）：
  1. 回合切分：≤120s 同回合（含恰好 120s）、>120s 分回合、入站打断分回合、
     跨会话独立、占比计算；
  2. 语音履约：10 分钟窗边界（600s 兑现 / 601s 不算 / 早于请求不算）、
     audio 也算、请求正则变体、媒体分布与最后语音时间；
  3. 口头禅：哈哈家族按条计（同条多个只算 1）、单「哈」不算、hhh/haha 算、
     句尾语气剥终端标点后判尾字；
  4. 场景词按条计；复读归一（大小写/标点/emoji 不敏感）+ 开头 8 字桶；
  5. 记忆回带中英句式；空 rows 全函数零除防御；
CLI 壳（tmp sqlite 最小 messages 表，subprocess 端到端）：--json 结构 +
人读渲染 + 库缺失 exit 2。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.chat_freshness_report import (  # noqa: E402
    BASELINE,
    catchphrase_density,
    collect_freshness,
    memory_callback,
    render_report,
    repeat_detection,
    scene_word_counts,
    split_outbound_bursts,
    voice_fulfillment,
)

TOOL = Path(__file__).resolve().parent.parent / "tools" / "chat_freshness_report.py"
T0 = 1785600000.0


def _r(conv, direction, text, ts, media=""):
    return {"conversation_id": conv, "direction": direction,
            "text": text, "media_type": media, "ts": ts}


# ── 1) 回合切分 ─────────────────────────────────────────────────────────

def test_burst_gap_boundary_inclusive():
    rows = [
        _r("c", "out", "a", T0),
        _r("c", "out", "b", T0 + 120),      # 恰好 120s → 同回合
        _r("c", "out", "c", T0 + 120 + 121),  # 121s → 新回合
    ]
    b = split_outbound_bursts(rows)
    assert b["rounds"] == 2
    assert b["size_2"] == 1 and b["size_1"] == 1


def test_burst_inbound_interrupt_splits():
    rows = [
        _r("c", "out", "a", T0),
        _r("c", "in", "客户插话", T0 + 10),
        _r("c", "out", "b", T0 + 20),  # 间隔 20s 但被入站打断 → 新回合
    ]
    b = split_outbound_bursts(rows)
    assert b["rounds"] == 2 and b["size_1"] == 2 and b["size_2"] == 0


def test_burst_cross_conversation_independent_and_pct():
    rows = [
        _r("c1", "out", "a", T0),
        _r("c2", "out", "b", T0 + 10),  # 不同会话，10s 内也不并回合
        _r("c1", "out", "c", T0 + 30),
        _r("c1", "out", "d", T0 + 60),
        _r("c1", "out", "e", T0 + 90),  # c1 第一回合共 4 条（3+）
        _r("c2", "out", "f", T0 + 500),  # c2 两个单条回合
    ]
    b = split_outbound_bursts(rows)
    assert b["rounds"] == 3
    assert b["size_1"] == 2 and b["size_3plus"] == 1
    assert abs(b["pct_1"] - 66.7) < 0.1
    assert abs(b["pct_3plus"] - 33.3) < 0.1


# ── 2) 语音履约 ─────────────────────────────────────────────────────────

def test_voice_window_boundary():
    rows = [
        _r("c1", "in", "发个语音呗", T0),
        _r("c1", "out", "", T0 + 600, media="voice"),   # 恰好 600s → 兑现
        _r("c2", "in", "发个语音呗", T0),
        _r("c2", "out", "", T0 + 601, media="voice"),   # 601s → 不算
        _r("c3", "out", "", T0 - 60, media="voice"),    # 早于请求 → 不算
        _r("c3", "in", "想听你的声音", T0),
    ]
    v = voice_fulfillment(rows)
    assert v["requests"] == 3 and v["fulfilled"] == 1
    assert abs(v["rate_pct"] - 33.3) < 0.1


def test_voice_regex_variants_and_media_dist():
    rows = [
        _r("c", "in", "给我唱首歌嘛", T0),
        _r("c", "out", "", T0 + 60, media="audio"),      # audio 也算语音
        _r("c", "in", "语音吧", T0 + 200),
        _r("c", "in", "今天天气不错", T0 + 300),          # 非请求不计
        _r("c", "out", "好呀", T0 + 400),
        _r("c", "out", "", T0 + 500, media="image"),
    ]
    v = voice_fulfillment(rows)
    assert v["requests"] == 2
    # audio(T0+60) 只兑现第一个请求；第二个请求(T0+200)在它之后 → 未兑现
    assert v["fulfilled"] == 1
    assert v["rate_pct"] == 50.0
    assert v["media_dist"] == {"audio": 1, "text": 1, "image": 1}
    assert v["last_voice_ts"] == T0 + 60


# ── 3) 口头禅密度 ───────────────────────────────────────────────────────

def test_catchphrase_haha_family_per_message():
    rows = [
        _r("c", "out", "哈哈哈这也太逗了哈哈", T0),   # 同条多个只算 1
        _r("c", "out", "哈", T0 + 1),                # 单「哈」不算
        _r("c", "out", "hhh good one", T0 + 2),      # 拉丁家族算
        _r("c", "out", "平平无奇", T0 + 3),
        _r("c", "in", "哈哈哈哈", T0 + 4),           # 入站不计
    ]
    c = catchphrase_density(rows)
    assert c["out_total"] == 4
    assert c["haha_msgs"] == 2
    assert c["haha_per_100"] == 50.0


def test_catchphrase_tail_particles():
    rows = [
        _r("c", "out", "今天好热呀", T0),
        _r("c", "out", "走啦~", T0 + 1),
        _r("c", "out", "好呀！", T0 + 2),   # 剥终端标点后仍算语气尾
        _r("c", "out", "嗯好的", T0 + 3),   # 不算
        _r("c", "out", "到家了。", T0 + 4),  # 剥句号后尾字「了」不在表 → 不算
    ]
    c = catchphrase_density(rows)
    assert c["tail_msgs"] == 3
    assert c["tail_per_100"] == 60.0


# ── 4) 场景词 ───────────────────────────────────────────────────────────

def test_scene_word_counts_per_message():
    rows = [
        _r("c", "out", "今天喝了抹茶拿铁，抹茶超浓", T0),  # 同条两次只算 1 条
        _r("c", "out", "路过便利店买了关东煮", T0 + 1),
        _r("c", "in", "抹茶好喝吗", T0 + 2),               # 入站不计
    ]
    got = scene_word_counts(rows, ["抹茶", "便利店", "关东煮", "手冲"])
    assert got == {"抹茶": 1, "便利店": 1, "关东煮": 1, "手冲": 0}


# ── 5) 复读探测 ─────────────────────────────────────────────────────────

def test_repeat_normalization_and_heads():
    rows = [
        _r("c", "out", "好呀～", T0),
        _r("c", "out", "好呀!", T0 + 1),          # 归一后与上一条同句
        _r("c", "out", "好呀", T0 + 2),
        _r("c", "out", "今天路过那家咖啡店了哦", T0 + 3),
        _r("c", "out", "今天路过那家花店，超好看", T0 + 4),  # 开头 8 字同桶
        _r("c", "out", "独一无二的句子", T0 + 5),  # 无重复不进 Top
    ]
    rp = repeat_detection(rows)
    assert rp["full"][0] == ("好呀", 3)
    assert ("独一无二的句子", 1) not in rp["full"]
    heads = dict(rp["heads"])
    assert heads.get("今天路过那家咖啡") is None  # 两条开头第 7 字起分叉
    assert heads.get("今天路过那家") is None      # 桶键固定 8 字
    # 开头 8 字：咖啡店/花店 在第 7 字分叉 → 各自成桶不满 2 → 不入 Top；
    # 造两条真同头验证
    rows += [_r("c", "out", "今天路过那家咖啡店关门了", T0 + 6)]
    heads2 = dict(repeat_detection(rows)["heads"])
    assert heads2.get("今天路过那家咖啡") == 2


def test_repeat_skips_media_placeholders():
    """存量镜像占位「[语音]×N/[图片]」不算复读；媒体配文剥前缀标记后照常
    参与，并与同句纯文本合并计数
    （真库首跑实锤：「语音」×81 霸榜挤掉真配文复读）。"""
    rows = [
        _r("c", "out", "[语音]", T0),
        _r("c", "out", "[语音]", T0 + 1),
        _r("c", "out", "[语音] ×3", T0 + 2),
        _r("c", "out", "[语音] ×3", T0 + 3),
        _r("c", "out", "[图片] 翻到一张之前拍的给你看", T0 + 4),
        _r("c", "out", "[图片] 翻到一张之前拍的给你看", T0 + 5),
        _r("c", "out", "翻到一张之前拍的给你看", T0 + 6),  # 纯文本同句合并
    ]
    rp = repeat_detection(rows)
    full = dict(rp["full"])
    assert "语音" not in full and "语音3" not in full
    assert full.get("翻到一张之前拍的给你看") == 3  # 配文复读是真复读
    assert dict(rp["heads"]).get("翻到一张之前拍的") == 3


# ── 6) 记忆回带 ─────────────────────────────────────────────────────────

def test_memory_callback_zh_and_en():
    rows = [
        _r("c", "out", "上次你说想去海边，安排上了吗", T0),
        _r("c", "out", "Last time you said you were tired", T0 + 1),
        _r("c", "out", "你之前提到的那家店我去了", T0 + 2),
        _r("c", "out", "普通一句", T0 + 3),
        _r("c", "in", "你说过要带我去", T0 + 4),  # 入站不计
    ]
    m = memory_callback(rows)
    assert m["hits"] == 3 and m["out_total"] == 4
    assert m["pct"] == 75.0


# ── 7) 空输入防御 + 汇总 ────────────────────────────────────────────────

def test_empty_rows_no_crash():
    assert split_outbound_bursts([])["rounds"] == 0
    assert voice_fulfillment([])["rate_pct"] == 0.0
    assert catchphrase_density([])["haha_per_100"] == 0.0
    assert scene_word_counts([], ["抹茶"]) == {"抹茶": 0}
    assert repeat_detection([]) == {"full": [], "heads": []}
    assert memory_callback([])["pct"] == 0.0
    rep = collect_freshness([])
    assert rep["baseline"]["burst_single_pct"] == BASELINE["burst_single_pct"]
    # 渲染空报告不抛
    assert "聊天新鲜度报告" in render_report(
        dict(rep, data_root="X", days=7.0))


# ── 8) CLI 壳端到端（tmp sqlite + subprocess）───────────────────────────

def _make_db(root: Path, rows):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg / "inbox.db")
    con.execute(
        "CREATE TABLE messages (conversation_id TEXT, direction TEXT, "
        "text TEXT, media_type TEXT, ts REAL)")
    con.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _run_cli(*args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, timeout=120, env=env)


def test_cli_end_to_end_json(tmp_path):
    now = time.time()
    _make_db(tmp_path, [
        ("c1", "out", "今天好热呀", "", now - 3600),
        ("c1", "out", "想你了", "", now - 3600 + 30),
        ("c1", "in", "发个语音听听", "", now - 1800),
        ("c1", "out", "", "voice", now - 1800 + 120),
        ("c2", "out", "抹茶好喝哈哈", "", now - 7200),
        ("c2", "out", "十天前的旧消息", "", now - 10 * 86400),  # 窗外
    ])
    p = _run_cli("--data-root", str(tmp_path), "--days", "7",
                 "--scene-words", "抹茶,夜跑", "--json")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    reports = json.loads(p.stdout.decode("utf-8"))
    assert len(reports) == 1
    rep = reports[0]
    assert rep["data_root"] == str(tmp_path)
    assert rep["bursts"]["rounds"] == 3          # c1×2 + c2×1（窗外行不计）
    assert rep["bursts"]["size_2"] == 1
    assert rep["voice"]["requests"] == 1 and rep["voice"]["fulfilled"] == 1
    assert rep["voice"]["media_dist"] == {"text": 3, "voice": 1}
    assert rep["catchphrase"]["out_total"] == 4
    assert rep["catchphrase"]["haha_msgs"] == 1
    assert rep["scene_words"] == {"抹茶": 1, "夜跑": 0}
    assert rep["baseline"]["voice_fulfill_pct"] == 18.75


def test_cli_human_render(tmp_path):
    now = time.time()
    _make_db(tmp_path, [("c1", "out", "今天好热呀", "", now - 60)])
    p = _run_cli("--data-root", str(tmp_path), "--days", "7")
    assert p.returncode == 0
    out = p.stdout.decode("utf-8", "replace")
    assert "聊天新鲜度报告" in out
    assert "出站回合分条分布" in out
    assert "体检基线 91.0% 单条" in out
    assert "语音履约" in out and "18.75%" in out


def test_cli_missing_db_exit2(tmp_path):
    p = _run_cli("--data-root", str(tmp_path), "--days", "7")
    assert p.returncode == 2
    assert "找不到收件箱库" in p.stderr.decode("utf-8", "replace")


# ── 7) 测试消息排除 + 趋势行（2026-08-03 周批化增量）────────────────────────

def test_filter_test_rows_drops_qa_markers():
    from tools.chat_freshness_report import filter_test_rows
    rows = [
        _r("c", "out", "[验收QA103] 压测消息", T0),
        _r("c", "out", "验收一下这个功能哈", T0 + 1),   # 句中「验收」不在句首标记形态→保留
        _r("c", "in", "[QA1] probe", T0 + 2),
        _r("c", "out", "正常聊天", T0 + 3),
    ]
    kept = filter_test_rows(rows)
    texts = [r["text"] for r in kept]
    assert "[验收QA103] 压测消息" not in texts
    assert "[QA1] probe" not in texts
    assert "正常聊天" in texts and "验收一下这个功能哈" in texts
    # 空 pattern / 非法 pattern → 原样返回
    assert len(filter_test_rows(rows, "")) == 4
    assert len(filter_test_rows(rows, "([bad")) == 4


def test_trend_line_shape_and_cli_out_jsonl(tmp_path):
    from tools.chat_freshness_report import collect_freshness, trend_line
    rows = [
        _r("c1", "out", "今天好热呀哈哈", T0),
        _r("c1", "in", "发个语音听听", T0 + 60),
        _r("c1", "out", "", T0 + 120, media="voice"),
    ]
    rep = {"data_root": "x", "days": 7.0, "ts": T0 + 200}
    rep.update(collect_freshness(rows, scene_words=["抹茶"]))
    line = trend_line(rep)
    assert line["voice_requests"] == 1 and line["voice_fulfilled"] == 1
    assert line["out_total"] == 2 and 0 <= line["burst_single_pct"] <= 100
    assert line["scene_words"] == {"抹茶": 0}
    assert line["date"] and line["data_root"] == "x"
    # CLI --out-jsonl 追加一行有效 JSON
    now = time.time()
    _make_db(tmp_path, [("c1", "out", "正常消息", "", now - 60),
                        ("c1", "out", "[验收QA9] 测试", "", now - 50)])
    out = tmp_path / "trend.jsonl"
    p = _run_cli("--data-root", str(tmp_path), "--days", "7",
                 "--out-jsonl", str(out))
    assert p.returncode == 0
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    # 测试标记行被默认排除 → 出站仅 1 条
    assert parsed["out_total"] == 1
