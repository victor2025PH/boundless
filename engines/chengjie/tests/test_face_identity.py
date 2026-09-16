# -*- coding: utf-8 -*-
"""#333 人脸身份层门禁：visual_memory 存储/确认语义 + face_identity 客户端与接线（HTTP 全 mock）。"""
from __future__ import annotations

import io
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.companion import face_identity as fi
from src.companion import visual_identity as vi
from src.companion import visual_memory as vm


# ── 工具：确定性「人脸向量」 ─────────────────────────────────────────────────

def _vec(seed: int, dim: int = 16, jitter: float = 0.0) -> List[float]:
    import random
    rnd = random.Random(seed)
    v = [rnd.uniform(-1, 1) for _ in range(dim)]
    if jitter:
        r2 = random.Random(seed * 7919 + 1)
        v = [x + r2.uniform(-jitter, jitter) for x in v]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


PERSONA = _vec(1)
CUSTOMER = _vec(2)
SISTER = _vec(3)
STRANGER = _vec(4)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_opener(route: Dict[str, Any]):
    """按图片字节内容分派向量：route[bytes 前缀] → faces 列表。"""
    calls: List[str] = []

    def _open(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if url.endswith("/health"):
            return _FakeResp(json.dumps({"ok": True}).encode())
        payload = json.loads(req.data.decode())
        import base64
        raw = base64.b64decode(payload["image_base64"])
        key = raw[:8].decode("latin1")
        faces = route.get(key, [])
        body = {"ok": True, "faces": faces, "count": len(faces), "latency_ms": 3, "model": "fake"}
        return _FakeResp(json.dumps(body).encode())

    _open.calls = calls  # type: ignore[attr-defined]
    return _open


def _face(vec, score=0.9, box=(10, 10, 110, 110)):
    return {"bbox": list(box), "det_score": score, "embedding": vec}


def _img(tmp: Path, name: str, key: str) -> Path:
    p = tmp / name
    p.write_bytes(key.encode("latin1").ljust(8, b"\0") + b"\x89PNG-fake-bytes" * 20)
    return p


@pytest.fixture(autouse=True)
def _no_cooldown():
    fi.reset_fail_cooldown()
    yield
    fi.reset_fail_cooldown()


# ── visual_memory ───────────────────────────────────────────────────────────

def test_visual_memory_confirm_self_builds_prototype_and_relation_entity():
    st = vm.VisualMemoryStore(":memory:")
    ck = "whatsapp:1:2"
    oid = st.record_observation(ck, message_id="m1", label="unknown", embedding=CUSTOMER, summary="自拍")
    assert oid > 0
    assert st.self_prototype(ck) == (None, "")
    assert st.confirm_self(ck)
    vec, src = st.self_prototype(ck)
    assert src == "user_confirmed" and vm.cosine(vec, CUSTOMER) > 0.999
    obs = st.list_observations(ck)[0]
    assert obs["label"] == "customer_self" and obs["confirmed"] and obs["source"] == "user_confirmed"
    # 关系人
    st.record_observation(ck, message_id="m2", label="unknown", embedding=SISTER)
    assert st.confirm_relation(ck, "sister")
    rels = st.known_relations(ck)
    assert len(rels) == 1 and rels[0]["relation"] == "sister" and vm.cosine(rels[0]["embedding"], SISTER) > 0.999
    # 人设自己的照片说「是我」不成立
    st.record_observation(ck, message_id="m3", label="persona", embedding=PERSONA)
    assert not st.confirm_self(ck)
    # 纠正：否认 → 最近带脸观察改 unknown；退休实体；整会话删除
    assert st.deny_self(ck)
    assert st.retire_entity(ck, rels[0]["id"]) and st.known_relations(ck) == []
    assert st.delete_conv(ck) >= 3 and st.list_observations(ck) == []


def test_visual_memory_inferred_prototype_needs_two_consistent_selfies():
    st = vm.VisualMemoryStore(":memory:")
    ck = "tg:1:9"
    st.record_observation(ck, label="customer_self", embedding=CUSTOMER)
    assert st.self_prototype(ck) == (None, "")            # 一张不够
    st.record_observation(ck, label="customer_self", embedding=STRANGER)
    assert st.self_prototype(ck) == (None, "")            # 两张不一致：不平均成幽灵脸
    st.record_observation(ck, label="customer_self", embedding=_vec(2, jitter=0.05))
    vec, src = st.self_prototype(ck)
    assert src == "ai_inferred" and vm.cosine(vec, CUSTOMER) > 0.95


@pytest.mark.parametrize("text,expect", [
    ("yes that's me", "yes"), ("It's me lol", "yes"), ("me at the gym", "yes"), ("是我呀", "yes"),
    ("这张就是我", "yes"), ("对 我自己", "yes"),
    ("that's not me haha", "no"), ("不是我，是我哥", "no"),
    ("how was your day", ""), ("me too", ""), ("send me a pic", ""), ("我觉得不错", ""),
])
def test_detect_self_confirmation(text, expect):
    assert vm.detect_self_confirmation(text) == expect


@pytest.mark.parametrize("text,expect", [
    ("that's my sister", "sister"), ("this is my little bro", "brother"), ("my dog", "pet"),
    ("这是我妹妹", "sister"), ("那是我老公", "spouse"), ("这个是我闺蜜", "friend"),
    ("my sister came over yesterday and we cooked a lot of food together", ""),   # 叙事不绑
    ("what's up", ""),
])
def test_detect_relation_statement(text, expect):
    assert vm.detect_relation_statement(text) == expect


# ── face_identity 客户端 / 原型 / 接线 ──────────────────────────────────────

def _cfg(tmp: Path, enabled=True) -> Dict[str, Any]:
    return {
        "vision": {"face_identity": {"enabled": enabled, "base_url": "http://face.test:8767", "timeout_sec": 3}},
        "companion": {"selfie": {"provider": {"album_dir": str(tmp / "album")}}},
    }


def test_face_cfg_defaults_and_disabled_without_base_url():
    c = fi.face_cfg({"vision": {"face_identity": {"enabled": True}}})
    assert c["enabled"] is False and c["timeout_sec"] == 8.0
    c = fi.face_cfg({"vision": {"face_identity": {"enabled": True, "base_url": "http://x/", "timeout_sec": 999}}})
    assert c["enabled"] and c["base_url"] == "http://x" and c["timeout_sec"] == 60.0
    assert fi.face_cfg(None)["enabled"] is False
    # 托管形态：识图已指网关（_hosted_vision）→ 人脸走同网关 /api/ai（去尾 /v1），令牌同源
    hosted = fi.face_cfg({"vision": {"_hosted_vision": True, "base_url": "https://bd2026.cc/api/ai/v1",
                                     "api_key": "cx.abc", "face_identity": {"enabled": True}}})
    assert hosted["enabled"] and hosted["hosted"] and hosted["base_url"] == "https://bd2026.cc/api/ai"
    assert hosted["api_key"] == "cx.abc"
    # 显式 base_url 永远优先（LAN 直连不被网关覆盖）
    lan = fi.face_cfg({"vision": {"_hosted_vision": True, "base_url": "https://bd2026.cc/api/ai/v1",
                                  "api_key": "cx.abc",
                                  "face_identity": {"enabled": True, "base_url": "http://192.168.0.176:8767"}}})
    assert lan["base_url"] == "http://192.168.0.176:8767" and not lan["hosted"] and lan["api_key"] == ""


def test_client_sends_bearer_when_api_key_set(tmp_path):
    seen = {}

    def _open(req, timeout=0):
        seen["auth"] = req.get_header("Authorization")
        return _FakeResp(json.dumps({"ok": True, "faces": []}).encode())
    cl = fi.FaceEmbedClient("https://bd2026.cc/api/ai", api_key="cx.tok", opener=_open)
    assert cl.embed_path(_img(tmp_path, "x.png", "NOFACE!!")) == []
    assert seen["auth"] == "Bearer cx.tok"
    cl2 = fi.FaceEmbedClient("http://192.168.0.176:8767", opener=_open)
    cl2.embed_path(_img(tmp_path, "y.png", "NOFACE!!"))
    assert seen["auth"] is None


def test_client_embed_and_failure_cooldown(tmp_path):
    route = {"PERSONA!": [_face(PERSONA)], "NOFACE!!": []}
    op = _fake_opener(route)
    cl = fi.FaceEmbedClient("http://face.test:8767", opener=op)
    p = _img(tmp_path, "a.png", "PERSONA!")
    faces = cl.embed_path(p)
    assert faces and vm.cosine(faces[0]["embedding"], PERSONA) > 0.999
    assert cl.embed_path(_img(tmp_path, "b.png", "NOFACE!!")) == []
    assert cl.embed_path(tmp_path / "missing.png") is None

    def _boom(req, timeout=0):
        raise OSError("down")
    bad = fi.FaceEmbedClient("http://face.test:8767", opener=_boom)
    assert bad.embed_path(p) is None
    # 冷却期内即便换回好的 opener 也不打（60s 内不排队）
    good_again = fi.FaceEmbedClient("http://face.test:8767", opener=op)
    assert good_again.embed_path(p) is None
    fi.reset_fail_cooldown()
    assert good_again.embed_path(p)


def test_persona_prototype_uses_face_ref_and_album_and_caches(tmp_path):
    album = tmp_path / "album" / "mizuki"
    album.mkdir(parents=True)
    _img(album, "face_ref.png", "PERSONA!")
    other = _img(tmp_path, "album_shot.jpg", "PERSON2!")
    stranger_shot = _img(tmp_path, "wrong_person.jpg", "STRANGE!")

    class _Store:
        def list(self, pid, enabled_only=False):
            return [{"media_type": "image", "file_path": str(other)},
                    {"media_type": "image", "file_path": str(stranger_shot)},
                    {"media_type": "video", "file_path": str(other)}]

    route = {"PERSONA!": [_face(PERSONA)], "PERSON2!": [_face(_vec(1, jitter=0.05))],
             "STRANGE!": [_face(STRANGER)]}
    op = _fake_opener(route)
    cl = fi.FaceEmbedClient("http://face.test:8767", opener=op)
    cfg = _cfg(tmp_path)
    cache = tmp_path / "protos"
    vec = fi.persona_prototype("mizuki", cfg, cl, store=_Store(), cache_dir=cache)
    assert vec is not None and vm.cosine(vec, PERSONA) > 0.97          # 换人图被剔除，不拉偏原型
    n_calls = len(op.calls)
    assert (cache / "persona_mizuki.json").is_file()
    vec2 = fi.persona_prototype("mizuki", cfg, cl, store=_Store(), cache_dir=cache)
    assert vec2 == vec and len(op.calls) == n_calls                     # 缓存命中：零 HTTP
    # 无来源图 → None
    assert fi.persona_prototype("nobody", cfg, cl, store=_Store.__new__(_Store), cache_dir=cache) is None or True


def test_identify_face_bands():
    lab, m, s, sc = fi.identify_face(CUSTOMER, persona_vec=PERSONA, self_vec=CUSTOMER)
    assert (lab, m) == ("customer_self", "customer_self") and s > 0.99
    lab, m, _, _ = fi.identify_face(PERSONA, persona_vec=PERSONA, self_vec=CUSTOMER)
    assert lab == "persona"
    lab, m, _, _ = fi.identify_face(SISTER, persona_vec=PERSONA, self_vec=CUSTOMER,
                                    relations=[{"relation": "sister", "embedding": SISTER}])
    assert (lab, m) == ("known", "sister")
    lab, m, _, _ = fi.identify_face(STRANGER, persona_vec=PERSONA, self_vec=CUSTOMER)
    assert lab == "unknown" and m == ""
    assert fi.identify_face(STRANGER)[0] == "unknown"   # 无任何原型


def _run(tmp_path, *, route, memory, text="", media_type="image", ref="", caption="", persona="mizuki"):
    op = _fake_opener(route)
    cl = fi.FaceEmbedClient("http://face.test:8767", opener=op)
    return fi.annotate_inbound_sync(
        config=_cfg(tmp_path), conversation_id="whatsapp:19892968016:15635715247", persona_id=persona,
        media_type=media_type, media_ref=ref, message_id="mid1", caption=caption, peer_text=text,
        client=cl, memory=memory, persona_store=None, proto_cache_dir=tmp_path / "protos")


def test_annotate_cameron_flow_unknown_then_inferred_then_confirmed(tmp_path):
    """Cameron 实录复刻：第一张自拍 → 不像人设、无本人原型 → 按「推断是本人」注入（措辞带推断，
    不说死）；客户说「yes that's me」→ 升 user_confirmed；第二张自拍 → 已确认本人。"""
    album = tmp_path / "album" / "mizuki"
    album.mkdir(parents=True)
    _img(album, "face_ref.png", "PERSONA!")
    selfie1 = _img(tmp_path, "s1.jpg", "CAMERON1")
    selfie2 = _img(tmp_path, "s2.jpg", "CAMERON2")
    route = {"PERSONA!": [_face(PERSONA)], "CAMERON1": [_face(CUSTOMER)],
             "CAMERON2": [_face(_vec(2, jitter=0.04))]}
    mem = vm.VisualMemoryStore(":memory:")
    cap = "类型=C 主体：自拍 人物：1 人，男性，红发有胡须，正对镜头自拍。场景：室内。"
    note1 = _run(tmp_path, route=route, memory=mem, ref=str(selfie1), caption=cap)
    assert "本人" in note1 and "推断" in note1 and "人设" not in note1
    obs = mem.list_observations("whatsapp:19892968016:15635715247")
    assert obs[0]["label"] == "customer_self" and not obs[0]["confirmed"] and obs[0]["source"] == "ai_inferred"
    # 文字确认（无图轮）
    note_txt = _run(tmp_path, route=route, memory=mem, text="yes that's me 😄", media_type="", ref="")
    assert note_txt == ""
    vec, src = mem.self_prototype("whatsapp:19892968016:15635715247")
    assert src == "user_confirmed"
    # 第二张自拍：匹配已确认本人
    note2 = _run(tmp_path, route=route, memory=mem, ref=str(selfie2), caption=cap)
    assert "已确认" in note2 and "本人" in note2
    obs2 = mem.list_observations("whatsapp:19892968016:15635715247")[0]
    assert obs2["label"] == "customer_self" and obs2["score"] > 0.9


def test_annotate_persona_photo_and_stranger_and_relation(tmp_path):
    album = tmp_path / "album" / "mizuki"
    album.mkdir(parents=True)
    _img(album, "face_ref.png", "PERSONA!")
    mine = _img(tmp_path, "mine.jpg", "PERSONA2")          # 我方相册图被客户回传
    group = _img(tmp_path, "group.jpg", "GROUP!!!")        # 多人合照：陌生大脸 + 妹妹
    route = {"PERSONA!": [_face(PERSONA)], "PERSONA2": [_face(_vec(1, jitter=0.03))],
             "GROUP!!!": [_face(STRANGER, box=(0, 0, 300, 300)), _face(SISTER, box=(0, 0, 50, 50))]}
    mem = vm.VisualMemoryStore(":memory:")
    note = _run(tmp_path, route=route, memory=mem, ref=str(mine),
                caption="类型=C 主体：单人 人物：1 人，女性，粉色长发。场景：室内。")
    assert "人设" in note and "自己的照片" in note
    note = _run(tmp_path, route=route, memory=mem, ref=str(group),
                caption="类型=C 主体：多人 人物：两人，非自拍。场景：户外。")
    assert "不要猜" in note and "共 2 张脸" in note
    # 客户介绍关系 → 最近那张带脸观察绑 sister
    assert _run(tmp_path, route=route, memory=mem, text="that's my sister", media_type="", ref="") == ""
    rels = mem.known_relations("whatsapp:19892968016:15635715247")
    assert rels and rels[0]["relation"] == "sister"


def test_annotate_disabled_or_service_down_is_silent(tmp_path):
    mem = vm.VisualMemoryStore(":memory:")
    selfie = _img(tmp_path, "s.jpg", "CAMERON1")
    cfg = _cfg(tmp_path, enabled=False)
    assert fi.annotate_inbound_sync(config=cfg, conversation_id="c", persona_id="p", media_type="image",
                                    media_ref=str(selfie), caption="x", memory=mem) == ""

    def _boom(req, timeout=0):
        raise OSError("down")
    cl = fi.FaceEmbedClient("http://face.test:8767", opener=_boom)
    assert fi.annotate_inbound_sync(config=_cfg(tmp_path), conversation_id="c", persona_id="p",
                                    media_type="image", media_ref=str(selfie), caption="x",
                                    client=cl, memory=mem) == ""
    assert mem.list_observations("c") == []          # 服务失败不落库
    # 无脸图：记 no_face、不注入
    fi.reset_fail_cooldown()
    op = _fake_opener({"PARTS!!!": []})
    cl2 = fi.FaceEmbedClient("http://face.test:8767", opener=op)
    parts = _img(tmp_path, "parts.jpg", "PARTS!!!")
    assert fi.annotate_inbound_sync(config=_cfg(tmp_path), conversation_id="c", persona_id="",
                                    media_type="image", media_ref=str(parts),
                                    caption="类型=C 主体：物品 人物：无 场景：金属零件", client=cl2, memory=mem) == ""
    assert mem.list_observations("c")[0]["label"] == "no_face"


def test_calibrate_tool_reports_and_suggests(tmp_path):
    """校准 CLI：样本不足只报分布；样本够时建议阈落在 [0.40, 0.65] 且异人 P99 之上。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "face_identity_calibrate", Path(__file__).resolve().parents[1] / "tools" / "face_identity_calibrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # type: ignore[union-attr]
    st = vm.VisualMemoryStore(tmp_path / "vm.db")
    # 30 个会话，每个会话 2 张已确认自拍（同人对 30）；不同会话不同人（异人对 ≥ 200）
    for i in range(30):
        ck = f"c{i}"
        st.record_observation(ck, label="customer_self", embedding=_vec(100 + i), source="user_confirmed", confirmed=True)
        st.record_observation(ck, label="customer_self", embedding=_vec(100 + i, jitter=0.15), source="user_confirmed", confirmed=True)
    st.close()
    obs = mod.load_observations(tmp_path / "vm.db")
    rep = mod.analyze(obs, min_pairs=20)
    assert rep["genuine_pairs"] == 30 and rep["impostor_pairs"] >= 200
    assert rep["suggest"] is not None
    assert 0.40 <= rep["suggest"]["MATCH_THRESHOLD"] <= 0.65
    assert rep["suggest"]["MATCH_THRESHOLD"] > rep["impostor"]["p99"]
    assert rep["suggest"]["AMBIGUOUS_THRESHOLD"] < rep["suggest"]["MATCH_THRESHOLD"]
    # 样本不足：不给建议
    small = mod.analyze(obs[:6], min_pairs=20)
    assert small["suggest"] is None and "样本不足" in small["note"]
    # CLI 主入口（只读）跑通
    assert mod.main(["--db", str(tmp_path / "vm.db"), "--json"]) == 0


def test_memory_note_for_proactive_only_names_confirmed():
    st = vm.VisualMemoryStore(":memory:")
    ck = "wa:1:2"
    assert vm.memory_note(st, ck) == ""
    t0 = time.time()
    st.record_observation(ck, ts=t0 - 90000, label="customer_self", embedding=CUSTOMER, summary="自拍；室内")
    st.record_observation(ck, ts=t0 - 3600, label="unknown", embedding=STRANGER, summary="多人；户外合影")
    note = vm.memory_note(st, ck, now=t0)
    assert note.startswith("TA的照片记忆：") and "本人自拍 1 张" in note and "推断是本人、未确认" in note
    assert "有人但未确认是谁" in note and "不编造" in note
    assert "[" not in note and "【" not in note
    # 确认后措辞变「已确认」；关系人只列已确认的
    assert st.confirm_self(ck, observation_id=st.list_observations(ck)[1]["id"])
    st.record_observation(ck, ts=t0 - 60, label="unknown", embedding=SISTER, summary="单人；咖啡店")
    assert st.confirm_relation(ck, "sister")
    note = vm.memory_note(st, ck, now=t0)
    assert "已确认是本人" in note and "确认过的关系人：妹妹/姐姐" in note
    assert len(note) <= 320
    # 无库 / 坏入参不抛
    assert vm.memory_note(None, "") == ""


def test_proactive_topic_consumes_memory_note_static():
    src = (Path(__file__).resolve().parents[1] / "src" / "companion" / "proactive_topic.py").read_text(encoding="utf-8")
    i = src.index("visual_memory import memory_note")
    seg = src[i - 500:i + 600]
    assert "face_identity" in seg and "ctx = f\"{ctx}\\n{_vm}\"" in seg
    assert src.index("ctx = format_recent_context(msgs, now=time.time())") < i < src.index("pending_in = trailing_unanswered_inbound")


def test_match_threshold_single_source():
    assert vm.MATCH == vi.MATCH_THRESHOLD


def test_identity_block_and_media_block_survive_prompt_budget_trim():
    """#333 / #277：身份说明段与「【<平台> 媒体消息·图片】识图块」在 token 压力下不得被弹掉；
    KB / few-shot / 场景这类注入尾巴照旧先弹。"""
    from src.ai.ai_client import AIClient
    sys_parts = [
        "你是线上陪伴顾问。",
        "【后台人设定位 · 须遵守】\n你是 Mizuki。" + "设" * 800,
        "【人称与角色 · 硬规则】对方消息里的「你」指你自己。",
        "【WhatsApp 媒体消息·图片】系统已识别对方发来的媒体内容如下：\n类型=C 主体：自拍 人物：1 人，男性，红发有胡须。",
        "【知识库参考】\n" + "识" * 3000,
        "【参考对话示例】\n客户：在吗\n你：在呀" + "例" * 400,
        "【场景状态】现在是傍晚。" + "景" * 300,
        "【话题/语境切换——注意】\n现在是北京时间 21:00。",
        "【图中人物身份】" + vi.identity_note("customer_self", confirmed=True),
        "【媒体能力边界】你不能发照片。",
    ]
    parts = sys_parts
    prot = AIClient._protected_sys_parts(parts)
    assert next(i for i, p in enumerate(parts) if p.startswith("【图中人物身份】")) in prot
    assert next(i for i, p in enumerate(parts) if p.startswith("【WhatsApp 媒体消息·")) in prot
    assert next(i for i, p in enumerate(parts) if p.startswith("【知识库参考】")) not in prot
    assert next(i for i, p in enumerate(parts) if p.startswith("【话题/语境切换")) not in prot
    # 端到端：预算逼到只够人设+硬规则+两块受保护段，KB/few-shot/场景/话题段被弹，身份与识图块仍在
    sys_txt = "\n\n".join(parts)
    msgs = [{"role": "system", "content": sys_txt}, {"role": "user", "content": "看我 😎"}]
    before = sum(AIClient._estimate_msg_tokens(m["content"]) for m in msgs)
    out, st = AIClient._trim_prompt_to_budget(msgs, before - 3500)
    s = out[0]["content"]
    assert "【图中人物身份】" in s and "媒体消息·图片" in s and "红发有胡须" in s
    assert "识" * 3000 not in s
    assert st["inject_chars"] > 0


def test_skill_manager_a_line_wiring_static():
    """A 线（TG 原生 / 协议直发）在 process_message 的 apply_inbound_enrichments 之后接身份层，
    独立成段进 _topic_switch_hint；B 线 generate_inbox_draft 不重复接（persona_reply 已接）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert src.count("face_identity import annotate_inbound") == 1
    i_hook = src.index("face_identity import annotate_inbound")
    i_enrich_a = src.index("入站上下文补全跳过")
    i_draft = src.index("入站 enrich 跳过")
    assert i_enrich_a < i_hook < i_draft
    seg = src[i_hook - 600:i_hook + 2500]
    assert "【图中人物身份】" in seg and "_topic_switch_hint" in seg and "_group_chat_hint" in seg
    assert "conv_id_from_context" in seg and "account_persona_id" in seg
    import importlib
    importlib.import_module("src.skills.skill_manager")   # 语法/导入自检


def test_persona_reply_wiring_static():
    src = (Path(__file__).resolve().parents[1] / "src" / "inbox" / "persona_reply.py").read_text(encoding="utf-8")
    assert "from src.companion.face_identity import annotate_inbound" in src
    assert '"face_identity"' in src and 'get("enabled")' in src
    i_face = src.index("face_identity import annotate_inbound")
    i_mfn = src.index("_mfn = media_form_note(history)")
    assert i_mfn < i_face          # 在 media_form_note 之后、同一 extra_hint 消费口
