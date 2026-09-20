# -*- coding: utf-8 -*-
"""ASR 金标策展 CLI（tools/asr_gold_curate.py）门禁：只读扫库 → 候选 → 导入清单去重 → 现状。"""
from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "asr_gold_curate.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("asr_gold_curate_under_test", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _wav(path: Path, seconds: float) -> str:
    byte_rate = 32000
    n = int(seconds * byte_rate)
    hdr = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 16000, byte_rate, 2, 16) + b"data" + struct.pack("<I", n)
    path.write_bytes(hdr + b"\x00" * n)
    return str(path)


def _seed_root(tmp_path):
    """数据根：config/inbox.db（真 InboxStore + ingest_incoming）+ 两条语音 + 一条文字 + 元数据。"""
    from src.inbox import asr_meta
    from src.inbox.store import InboxStore
    from src.integrations.protocol_bridge import ingest_incoming
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    store = InboxStore(root / "config" / "inbox.db")
    a1 = _wav(root / "v1.wav", 3.0)
    a2 = _wav(root / "v2.wav", 1.2)
    cid = ingest_incoming(store, platform="whatsapp", account_id="acc", chat_key="639",
                          text="你那边几点怎么会说大造成呢", ts=1000.0, msg_id="w1",
                          media_type="voice", media_ref=a1)
    ingest_incoming(store, platform="whatsapp", account_id="acc", chat_key="639",
                    text="嗯", ts=1001.0, msg_id="w2", media_type="voice", media_ref=a2)
    ingest_incoming(store, platform="telegram", account_id="t", chat_key="9",
                    text="文字消息", ts=1002.0, msg_id="t1")
    asr_meta.save(store, cid, "w1", {"language": "zh", "avg_logprob": -1.3}, suspect="low_confidence",
                  machine_text="你那边几点怎么会说大造成呢")
    asr_meta.mark_corrected(store, cid, "w1", corrected_text="你那边几点怎么会说打造成呢", agent="op",
                            platform_msg_id="w1")
    store.close() if hasattr(store, "close") else None
    return root, cid, a1, a2


def test_list_rows_and_candidates(tool, tmp_path):
    root, cid, a1, a2 = _seed_root(tmp_path)
    rows = tool.list_voice_rows(root / "config" / "inbox.db", days=365 * 100, limit=10, now=2000.0)
    assert [r["media_ref"] for r in rows] == [a2, a1]            # 最近优先，文字行不入
    assert rows[1]["corrected"] and rows[1]["corrected_text"].endswith("打造成呢")
    assert rows[1]["suspect"] == ""                                # 改正后可疑已清
    only_tg = tool.list_voice_rows(root / "config" / "inbox.db", days=365 * 100, platform="telegram", now=2000.0)
    assert only_tg == []
    cands = tool.build_candidates(rows, root)
    c1 = [c for c in cands if c["audio"] == a1][0]
    assert c1["ref"] == "你那边几点怎么会说打造成呢"           # 已改正 → ref 预填
    assert c1["duration"] == 3.0 and c1["audio_sha1"] and not c1["audio_missing"]
    c2 = [c for c in cands if c["audio"] == a2][0]
    assert c2["ref"] == "" and c2["duration"] == 1.2
    # 库不存在 → []
    assert tool.list_voice_rows(root / "nope.db") == []


def test_import_dedup_and_status(tool, tmp_path):
    root, _cid, a1, a2 = _seed_root(tmp_path)
    manifest = tmp_path / "eval" / "asr_samples.jsonl"
    cands = tool.build_candidates(
        tool.list_voice_rows(root / "config" / "inbox.db", days=365 * 100, now=2000.0), root)
    st = tool.append_samples(manifest, cands)
    assert st == {"added": 1, "skipped_dup": 0, "skipped_noref": 1, "skipped_missing": 0}
    rows = tool.load_manifest(manifest)
    assert len(rows) == 1 and rows[0]["audio"] == a1 and "corrected" in rows[0]["tags"]
    # 人耳填了 ref 再导入：第二条进来，第一条按 sha1 去重
    for c in cands:
        if c["audio"] == a2:
            c["ref"] = "嗯"
    st2 = tool.append_samples(manifest, cands)
    assert st2 == {"added": 1, "skipped_dup": 1, "skipped_noref": 0, "skipped_missing": 0}
    # 音频不在 → skipped_missing
    st3 = tool.append_samples(manifest, [{"audio": str(tmp_path / "gone.wav"), "ref": "x"}])
    assert st3["skipped_missing"] == 1
    status = tool.manifest_status(manifest)
    # ingest 已按文本检出 source_lang=zh → 样本自带语种（评测按语种聚合正好用得上）
    assert status["n"] == 2 and status["by_lang"] == {"zh": 2} and status["ref_unverified"] == 0
    assert status["by_bucket"] == {"2-5s": 1, "<2s": 1} and status["enough_to_judge"] is False
    # CLI：status / add
    assert tool.main(["status", "--manifest", str(manifest)]) == 0
    a3 = _wav(tmp_path / "v3.wav", 6.0)
    assert tool.main(["add", "--audio", a3, "--ref", "第三条", "--lang", "zh-CN", "--manifest", str(manifest)]) == 0
    rows3 = tool.load_manifest(manifest)
    assert rows3[-1]["ref"] == "第三条" and rows3[-1]["lang"] == "zh"
    # 重复 add → 去重、退出码 1
    assert tool.main(["add", "--audio", a3, "--ref", "第三条", "--manifest", str(manifest)]) == 1


def test_export_and_import_roundtrip(tool, tmp_path):
    root, _cid, a1, a2 = _seed_root(tmp_path)
    out = tmp_path / "cands.jsonl"
    rc = tool.main(["--data-root", str(root), "export", "--days", str(365 * 100), "--out", str(out)])
    assert rc == 0
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert {l["audio"] for l in lines} == {a1, a2}
    pre = [l for l in lines if l["ref"]]
    assert len(pre) == 1 and pre[0]["audio"] == a1
    manifest = tmp_path / "m.jsonl"
    assert tool.main(["import", str(out), "--manifest", str(manifest)]) == 0
    assert len(tool.load_manifest(manifest)) == 1
    # 评测轨能直接吃这份清单
    from src.eval.asr_eval import load_asr_samples
    loaded = load_asr_samples(str(manifest))
    assert len(loaded["samples"]) == 1 and loaded["samples"][0].ref.endswith("打造成呢")
