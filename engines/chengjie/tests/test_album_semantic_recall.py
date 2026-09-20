"""相册语义召回·影子门禁（实施90 阶段二）：默认关零动作 / 命中写 jsonl /
嵌入失败软降级 / pick_media miss 时挂影子（后台、绝不影响返回值）。"""
import json
import time

import src.companion.album_semantic_recall as asr
from src.companion.album_semantic_recall import (
    candidate_text,
    cosine,
    resolve_semantic_recall_cfg,
    shadow_check,
)


def setup_function(_fn):
    asr.reset_for_tests()


def test_cfg_defaults_off_and_clamps():
    c = resolve_semantic_recall_cfg({})
    assert c == {"enabled": False, "min_cosine": 0.62, "max_candidates": 40}
    c2 = resolve_semantic_recall_cfg({"companion": {"selfie": {"album_ai": {
        "semantic_recall": {"enabled": True, "min_cosine": 5,
                            "max_candidates": 9999}}}}})
    assert c2["enabled"] is True
    assert c2["min_cosine"] == 0.99 and c2["max_candidates"] == 200


def test_cosine_pure():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([], [1]) == 0.0
    assert cosine([1, 2], [1]) == 0.0
    assert cosine([0, 0], [0, 0]) == 0.0


def test_candidate_text_sources():
    row = {"auto_meta": {"desc": "夜市烤串摊"}, "triggers": ["烤串", "夜市"],
           "tags": ["scene:street", "tod:night"]}
    t = candidate_text(row)
    assert "夜市烤串摊" in t and "烤串" in t and "street" in t
    assert candidate_text({"triggers": [], "tags": []}) == ""
    assert candidate_text(None) == ""


def _cfg(enabled=True, min_cos=0.6):
    return {"enabled": enabled, "min_cosine": min_cos, "max_candidates": 40}


_ROWS = [
    {"id": "m1", "auto_meta": {"desc": "夜市烤串摊冒着热气"},
     "triggers": ["烤串"], "tags": ["scene:street"]},
    {"id": "m2", "auto_meta": {"desc": "车内自拍"}, "triggers": [],
     "tags": ["scene:car"]},
]


def _embed_hit(texts):
    # 查询与 m1 同向、m2 正交
    out = [[1.0, 0.0]]
    for t in texts[1:]:
        out.append([0.95, 0.05] if "烤串" in t else [0.0, 1.0])
    return out


def test_shadow_disabled_is_noop(tmp_path):
    got = shadow_check(_ROWS, "lin", "有没有夜宵摊的照片",
                       cfg=_cfg(enabled=False), embed_fn=_embed_hit,
                       log_dir=str(tmp_path))
    assert got is None
    assert asr.snapshot()["checked"] == 0
    assert not list(tmp_path.glob("*.jsonl"))


def test_shadow_hit_writes_jsonl_and_counts(tmp_path):
    got = shadow_check(_ROWS, "lin", "有没有夜宵摊的照片", cfg=_cfg(),
                       embed_fn=_embed_hit, log_dir=str(tmp_path),
                       now=1700000000.0)
    assert got is not None and got[0] == "m1" and got[1] > 0.9
    snap = asr.snapshot()
    assert snap["checked"] == 1 and snap["would_hit"] == 1
    assert snap["last_hit"]["media_id"] == "m1"
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert line["media_id"] == "m1" and line["persona"] == "lin"
    assert line["cosine"] > 0.9


def test_shadow_miss_and_error_paths(tmp_path):
    # 阈值拉满 → miss
    got = shadow_check(_ROWS, "lin", "有没有夜宵摊的照片",
                       cfg=_cfg(min_cos=0.999), embed_fn=_embed_hit,
                       log_dir=str(tmp_path))
    assert got is None and asr.snapshot()["miss"] == 1
    # 嵌入挂了 → errors，绝不抛
    got2 = shadow_check(_ROWS, "lin", "有没有夜宵摊的照片", cfg=_cfg(),
                        embed_fn=lambda t: None, log_dir=str(tmp_path))
    assert got2 is None and asr.snapshot()["errors"] == 1
    # 没有可参赛素材（全空索引文本）→ 不计 checked
    got3 = shadow_check([{"id": "x", "triggers": [], "tags": []}], "lin",
                        "来张照片", cfg=_cfg(), embed_fn=_embed_hit,
                        log_dir=str(tmp_path))
    assert got3 is None and asr.snapshot()["checked"] == 2  # 前两次的


def test_pick_media_miss_spawns_shadow(monkeypatch):
    from src.companion.persona_media import pick_media
    from src.companion.persona_media_store import PersonaMediaStore
    calls = []
    monkeypatch.setattr(asr, "maybe_shadow",
                        lambda store, pid, text: calls.append((pid, text)) or True)
    st = PersonaMediaStore(":memory:")   # 空册 → 泛化要图必 miss
    assert pick_media(st, "lin", "来张照片", generic_ok=True) is None
    assert calls == [("lin", "来张照片")]
    # 非泛化要图（generic_ok=False）miss 不挂影子（本来就不该发）
    calls.clear()
    assert pick_media(st, "lin", "随便聊聊") is None
    assert calls == []


def test_summarize_shadow_lines_windows_and_verdicts():
    from src.companion.album_semantic_recall import summarize_shadow_lines
    now = 1_800_000_000.0
    # 零命中判词
    r0 = summarize_shadow_lines([], now=now)
    assert r0["n"] == 0 and any("零命中" in v for v in r0["verdict"])
    # 窗口过滤 + 坏行安全跳过 + 样本不足判词
    lines = [
        {"ts": now - 100, "persona": "lin", "media_id": "m1",
         "cosine": 0.7, "text": "有没有夜宵摊", "desc": "夜市烤串摊"},
        {"ts": now - 30 * 86400, "persona": "lin", "media_id": "mX",
         "cosine": 0.9},                       # 窗外
        "not a dict", {"ts": "junk"}, {"ts": now, "media_id": ""},
    ]
    r1 = summarize_shadow_lines(lines, now=now, days=14)
    assert r1["n"] == 1 and r1["distinct_media"] == 1
    assert any("样本不足" in v for v in r1["verdict"])
    # 样本充足 + 高余弦 + 集中度判词
    big = [{"ts": now - i * 3600, "persona": "lin", "media_id": f"m{i % 5}",
            "cosine": 0.8 + (i % 10) * 0.01, "text": f"t{i}", "desc": "d"}
           for i in range(25)]
    r2 = summarize_shadow_lines(big, now=now, days=14, min_cosine=0.62)
    assert r2["n"] == 25 and r2["by_persona"]["lin"] == 25
    assert r2["cosine"]["p50"] >= 0.8
    assert any("整体偏高" in v for v in r2["verdict"])
    assert any("高度集中" in v for v in r2["verdict"])
    assert len(r2["top"]) == 10
    assert r2["top"][0]["cosine"] >= r2["top"][-1]["cosine"]
    # 贴阈值判词
    flat = [{"ts": now - i, "persona": f"p{i % 3}", "media_id": f"m{i}",
             "cosine": 0.63, "text": "t", "desc": "d"} for i in range(21)]
    r3 = summarize_shadow_lines(flat, now=now, min_cosine=0.62)
    assert any("贴着阈值" in v for v in r3["verdict"])


def test_maybe_shadow_thread_smoke(monkeypatch):
    hits = []
    monkeypatch.setattr(asr, "_runtime_cfg", lambda: _cfg())
    monkeypatch.setattr(
        asr, "shadow_check",
        lambda rows, pid, text, cfg: hits.append((pid, len(rows))) or None)

    class _St:
        def list(self, pid, enabled_only=True):
            return list(_ROWS)

    assert asr.maybe_shadow(_St(), "lin", "有没有照片") is True
    deadline = time.time() + 3
    while not hits and time.time() < deadline:
        time.sleep(0.02)
    assert hits == [("lin", 2)]
    # 空入参不起线程
    assert asr.maybe_shadow(None, "lin", "x") is False
    assert asr.maybe_shadow(_St(), "", "有没有照片") is False
