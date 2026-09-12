# -*- coding: utf-8 -*-
"""ASR P2：逐条转写元数据 KV（asr_meta）+ 改正路由（asr_correction_routes）门禁。

真 InboxStore（tmp）+ 真 ingest_incoming 落一条语音消息 → 元数据落 KV → thread 形状挂 asr 字段
→ POST 改正四处同步（台账 / 正文 / 元数据 / 缓存）→ GET 台账读口。零网络零模型。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox import asr_meta
from src.inbox import asr_suspect as sus


# ── 假 KV store（只带 app_settings 三个方法）──────────────────────────────
class _KV:
    def __init__(self):
        self.kv = {}

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def list_app_settings(self, prefix=""):
        return [{"key": k, "value": v} for k, v in sorted(self.kv.items()) if k.startswith(prefix)]


def test_compact_and_confidence_label():
    meta = {"language": "zh", "language_probability": 0.9819, "avg_logprob": -0.2231,
            "no_speech_prob": 0.0, "duration": 11.034, "provider": "OpenAITranscriber",
            "level": 0, "cache_hit": False, "lang_retry": True, "timeout_sec": 6.5, "elapsed_ms": 650}
    c = asr_meta.compact(meta, machine_text="现在开始测试", ts=100.0)
    assert c["language"] == "zh" and c["language_probability"] == 0.9819 and c["level"] == 0
    assert c["lang_retry"] is True and "cache_hit" not in c        # False 不落
    assert "timeout_sec" not in c and "elapsed_ms" not in c          # 白名单外不落
    assert c["machine_text"] == "现在开始测试" and c["ts"] == 100.0 and "suspect" not in c
    assert asr_meta.confidence_label(c) == "high"
    assert asr_meta.confidence_label({"avg_logprob": -0.95, "language_probability": 0.9}) == "medium"
    assert asr_meta.confidence_label({"suspect": "low_confidence", "avg_logprob": -0.2}) == "low"
    assert asr_meta.confidence_label({"no_speech_prob": 0.4}) == "medium"
    assert asr_meta.confidence_label({}) == "" and asr_meta.confidence_label({"provider": "x"}) == ""


def test_save_load_attach_and_ttl(monkeypatch):
    st = _KV()
    assert asr_meta.save(st, "wa:a:1", "m1", {"language": "zh", "avg_logprob": -1.3},
                         suspect="low_confidence", machine_text="大造成呢")
    assert asr_meta.save(st, "wa:a:1", "m2", {"language": "zh", "avg_logprob": -0.2,
                                              "language_probability": 0.98})
    assert not asr_meta.save(st, "", "m3", {}) and not asr_meta.save(None, "c", "m", {})
    table = asr_meta.load_map(st, "wa:a:1")
    assert set(table) == {"m1", "m2"} and table["m1"]["suspect"] == "low_confidence"
    msgs = [
        {"message_id": "m1", "media_type": "voice", "text": "大造成呢"},
        {"message_id": "m2", "media_type": "voice", "text": "你好"},
        {"message_id": "m3", "media_type": "", "text": "文字"},
        {"message_id": "m9", "media_type": "voice", "text": "无元数据"},
    ]
    assert asr_meta.attach(st, "wa:a:1", msgs) == 2
    assert msgs[0]["asr"]["suspect"] == "low_confidence" and msgs[0]["asr"]["confidence"] == "low"
    assert msgs[0]["asr"]["machine_text"] == "大造成呢" and msgs[0]["asr"]["corrected"] is False
    assert msgs[1]["asr"]["confidence"] == "high" and msgs[1]["asr"]["suspect"] == ""
    assert "asr" not in msgs[2] and "asr" not in msgs[3]
    # 无语音行 → 零 KV 访问
    assert asr_meta.attach(st, "wa:a:1", [{"message_id": "m1", "media_type": ""}]) == 0
    # 改正 → 清可疑、记人
    rec = asr_meta.mark_corrected(st, "wa:a:1", "m1", corrected_text="打造成呢", agent="op1")
    assert rec["corrected"] is True and rec["suspect"] == "" and rec["corrected_by"] == "op1"
    assert rec["machine_text"] == "大造成呢"
    view = asr_meta.public_view(json.loads(st.kv[asr_meta.key("wa:a:1", "m1")]))
    assert view["corrected"] is True and view["confidence"] != "low"
    # 键既可以是 store message_id 也可以是边车 platform_msg_id（协议入站只知道后者）
    asr_meta.save(st, "wa:a:1", "wamid-x", {"language": "zh"}, suspect="no_speech")
    row = {"message_id": "store-9", "platform_msg_id": "wamid-x", "media_type": "voice"}
    assert asr_meta.attach(st, "wa:a:1", [row]) == 1 and row["asr"]["suspect"] == "no_speech"
    rec2 = asr_meta.mark_corrected(st, "wa:a:1", "store-9", corrected_text="好", platform_msg_id="wamid-x")
    assert rec2["corrected"] is True and asr_meta.key("wa:a:1", "wamid-x") in st.kv
    # TTL：过期条目读时懒删（记录 ts 是真实 now → 把 now 推到 3×TTL 之后）
    import time as _t
    far = _t.time() + asr_meta.TTL_SEC * 3
    monkeypatch.setattr(asr_meta.time, "time", lambda: far)
    assert asr_meta.load_map(st, "wa:a:1") == {}
    assert not any(k.startswith(asr_meta.KEY_PREFIX) for k in st.kv)


# ── 路由：真 InboxStore + ingest_incoming ─────────────────────────────────
def _app(tmp_path, *, voice_recognition=None):
    from src.inbox.store import InboxStore
    from src.integrations.protocol_bridge import ingest_incoming
    from src.web.routes.asr_correction_routes import register_asr_correction_routes

    store = InboxStore(tmp_path / "inbox.db")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()

    class _CM:
        config_path = str(cfg_dir / "config.yaml")
        config = {"voice_recognition": voice_recognition or {"enabled": False}}

    app = FastAPI()
    app.state.inbox_store = store
    register_asr_correction_routes(app, api_auth=lambda request=None: None, config_manager=_CM())
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS" + b"\x07" * 128)
    cid = ingest_incoming(store, platform="whatsapp", account_id="acc", chat_key="639",
                          text="你那边几点怎么会说大造成呢", ts=1000.0, msg_id="wamid1",
                          media_type="voice", media_ref=str(audio))
    ingest_incoming(store, platform="whatsapp", account_id="acc", chat_key="639",
                    text="纯文字", ts=1001.0, msg_id="wamid2")
    # 协议入站侧只知道边车 msg_id（= platform_msg_id）→ 元数据按它落键
    asr_meta.save(store, cid, "wamid1", {"language": "zh", "avg_logprob": -1.3, "low_confidence": True},
                  suspect="low_confidence", machine_text="你那边几点怎么会说大造成呢")
    sus.note(cid, "low_confidence", "你那边几点怎么会说大造成呢")
    return app, store, cid, cfg_dir, str(audio)


def _store_mid(store, cid, platform_msg_id):
    """前端拿到的是 thread 行的 store message_id（不是边车 wamid）——测试照此取键。"""
    for r in store.list_recent_messages(cid, limit=10):
        if str(r.get("platform_msg_id") or "") == platform_msg_id:
            return str(r.get("message_id"))
    raise AssertionError(f"no row for {platform_msg_id}")


def test_correction_route_syncs_ledger_text_meta_and_registry(tmp_path):
    sus._reset_for_tests()
    app, store, cid, cfg_dir, audio = _app(tmp_path)
    c = TestClient(app)
    mid = _store_mid(store, cid, "wamid1")
    body = {"platform": "whatsapp", "account_id": "acc", "chat_key": "639",
            "message_id": mid, "corrected_text": "你那边几点，怎么会说打造成呢"}
    with patch("src.integrations.protocol_bridge.static_media_ref_to_path", return_value=audio):
        r = c.post("/api/unified-inbox/asr-correction", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["ledger"] and d["text_updated"] and d["meta_updated"]
    assert d["machine_text"] == "你那边几点怎么会说大造成呢" and d["message_id"] == mid
    # ① 台账落数据根 config/asr_corrections.jsonl
    ledger = cfg_dir / "asr_corrections.jsonl"
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 1 and rows[0]["corrected_text"] == "你那边几点，怎么会说打造成呢"
    assert rows[0]["conversation_id"] == cid and rows[0]["platform"] == "whatsapp"
    assert rows[0]["message_id"] == mid and rows[0]["audio_sha1"]
    # ② 消息正文已是人改过的话
    assert store.get_message(mid)["text"] == "你那边几点，怎么会说打造成呢"
    # ③ 逐条元数据：清可疑、记改正（落在入站侧的 platform_msg_id 键上，不另开一条）
    table = asr_meta.load_map(store, cid)
    assert set(table) == {"wamid1"}
    assert table["wamid1"]["corrected"] is True and table["wamid1"]["suspect"] == ""
    # ④ 可疑登记已清（起草不再走「先确认」）
    assert sus.peek(cid) == ""
    # thread 形状挂 asr 字段（按 platform_msg_id 命中）
    from src.inbox.normalizer import store_message_to_obj
    msgs = [store_message_to_obj(x) for x in store.list_recent_messages(cid, limit=10)]
    voice = [m for m in msgs if m["message_id"] == mid][0]
    assert asr_meta.attach(store, cid, msgs) == 1
    assert voice["asr"]["corrected"] is True and voice["asr"]["machine_text"].startswith("你那边几点")
    # 台账读口 + 热词候选（大造成→打造成 差异片段）
    g = c.get("/api/admin/asr-corrections?limit=10")
    assert g.status_code == 200
    gd = g.json()
    assert gd["total"] == 1 and gd["items"][0]["corrected_text"].endswith("打造成呢")
    assert "audio_path" not in gd["items"][0]
    assert gd["ledger_path"].endswith("asr_corrections.jsonl")
    from src.ai.asr_stats import get_asr_stats
    assert get_asr_stats().dump()["events"]["correction"] >= 1


def test_correction_route_updates_transcript_cache_when_chain_available(tmp_path):
    sus._reset_for_tests()
    vr = {"enabled": True, "provider": "faster_whisper", "whisper": {"model_size": "tiny", "device": "cpu"},
          "language": "auto", "temp_dir": str(tmp_path / "t")}
    app, store, cid, _cfg_dir, audio = _app(tmp_path, voice_recognition=vr)
    from src.voice_transcriber import get_transcript_cache, transcript_cache_key
    get_transcript_cache().reset()
    c = TestClient(app)
    body = {"platform": "whatsapp", "account_id": "acc", "chat_key": "639",
            "message_id": _store_mid(store, cid, "wamid1"), "corrected_text": "打造成呢"}
    with patch("src.integrations.protocol_bridge.static_media_ref_to_path", return_value=audio):
        r = c.post("/api/unified-inbox/asr-correction", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["cache_updated"] is True
    # 同一音频再被识别 → 直接得到人改的话（键 = 内容 sha1 + 语言 + 后端作用域）
    hits = [k for k in get_transcript_cache()._d if k.startswith(transcript_cache_key(audio, "auto"))]
    assert hits and get_transcript_cache().get(hits[0])[0] == "打造成呢"


def test_correction_route_rejections(tmp_path):
    app, store, cid, _cfg_dir, _audio = _app(tmp_path)
    c = TestClient(app)
    voice_mid = _store_mid(store, cid, "wamid1")
    text_mid = _store_mid(store, cid, "wamid2")
    base = {"platform": "whatsapp", "account_id": "acc", "chat_key": "639"}
    assert c.post("/api/unified-inbox/asr-correction",
                  json={**base, "message_id": voice_mid, "corrected_text": "  "}).status_code == 400
    assert c.post("/api/unified-inbox/asr-correction",
                  json={**base, "corrected_text": "x"}).status_code == 400
    # 文字消息不是语音 → 400
    assert c.post("/api/unified-inbox/asr-correction",
                  json={**base, "message_id": text_mid, "corrected_text": "x"}).status_code == 400
    # 别的会话拿不到这条 → 404（越权：拿别人会话的 message_id 改不了）
    assert c.post("/api/unified-inbox/asr-correction",
                  json={**base, "chat_key": "other", "message_id": voice_mid,
                        "corrected_text": "x"}).status_code == 404
    assert c.post("/api/unified-inbox/asr-correction",
                  json={**base, "message_id": "no-such-id", "corrected_text": "x"}).status_code == 404
    assert c.post("/api/unified-inbox/asr-correction",
                  json={"platform": "", "chat_key": "", "message_id": "m", "corrected_text": "x"}).status_code == 400
    assert c.post("/api/unified-inbox/asr-correction",
                  json={**base, "message_id": voice_mid, "corrected_text": "x" * 2001}).status_code == 400
    # 无台账文件 → 空列表
    g = c.get("/api/admin/asr-corrections").json()
    assert g["total"] == 0 and g["items"] == [] and g["hotword_candidates"] == []
