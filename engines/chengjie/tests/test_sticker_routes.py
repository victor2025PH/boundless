"""表情包 API 契约门禁：flag 闸门/CRUD/上传规范化/收藏/官方播种/跨平台发送。

全程 tmp 隔离（store :memory: + 落盘根 tmp_path + manifest tmp），零 repo 写入。
"""
import io
import json
from typing import Any, Dict

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import src.web.routes.sticker_routes as srt
from src.inbox import sticker_store as ss

PIL = pytest.importorskip("PIL", reason="Pillow 未安装（requirements 既有依赖）")
from PIL import Image  # noqa: E402


def _png(w=64, h=64, color=(255, 0, 0, 255)) -> bytes:
    im = Image.new("RGBA", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class _CfgMgr:
    def __init__(self, cfg: Dict[str, Any]):
        self.config = cfg


class _FakeOrch:
    """假编排器：记录 send_media 入参，模拟四平台 worker。"""

    def __init__(self):
        self.calls = []
        self.line_worker = None
        self.media_owned = True

    def owns(self, platform, account_id):
        return True

    def owns_media(self, platform, account_id):
        return self.media_owned

    def worker_for(self, platform, account_id):
        return self.line_worker

    async def send_media(self, platform, account_id, chat_key, **kw):
        self.calls.append({"platform": platform, "account_id": account_id,
                           "chat_key": chat_key, **kw})
        return {"delivered": True, "message_id": "m1"}


class _FakeLineWorker:
    def __init__(self):
        self.sent = []

    async def send_line_sticker(self, chat_key, package_id, sticker_id):
        self.sent.append((chat_key, package_id, sticker_id))
        return {"delivered": True, "message_id": "lm1"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    ss.reset_sticker_store()
    ss.configure_sticker_store(":memory:")
    ss.configure_sticker_root(tmp_path / "sticker_packs")
    srt._reset_wa_caps_cache()

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    cfg = {"inbox": {"stickers": {"enabled": True, "max_packs": 3,
                                  "max_per_pack": 4}}}
    app.state.config_manager = _CfgMgr(cfg)

    @app.get("/_login")
    def _login(request: Request, role: str = ""):
        request.session["username"] = "tester"
        request.session["role"] = role
        return {"ok": True}

    srt.register_sticker_routes(app, auth_dep=lambda: True)

    orch = _FakeOrch()
    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator", lambda: orch)
    # send 路径的出站副本（save_outbound_media）绝不许写 repo static → tmp
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.protocol_media_root",
        lambda: tmp_path / "protocol_media")

    async def _no_wa_caps(_cfg):
        return False

    monkeypatch.setattr(srt, "_wa_sticker_capable", _no_wa_caps)
    client = TestClient(app)
    yield {"client": client, "orch": orch, "cfg": cfg, "tmp": tmp_path,
           "app": app, "monkeypatch": monkeypatch}
    ss.reset_sticker_store()
    ss.configure_sticker_root(None)


def _mkpack(client, title="我的包"):
    r = client.post("/api/stickers/packs", json={"title": title})
    assert r.status_code == 200, r.text
    return r.json()["pack"]


def _upload(client, pack_id, files):
    return client.post(
        f"/api/stickers/packs/{pack_id}/items",
        files=[("file", (n, d, "application/octet-stream")) for n, d in files])


# ── flag 闸门 ────────────────────────────────────────────────────────────────

def test_disabled_flag_soft_reads_hard_writes(env):
    c = env["client"]
    env["cfg"]["inbox"]["stickers"]["enabled"] = False
    assert c.get("/api/stickers/status").json() == {"ok": True, "enabled": False}
    body = c.get("/api/stickers/packs").json()
    assert body["enabled"] is False and body["packs"] == []
    assert c.post("/api/stickers/packs", json={"title": "x"}).status_code == 403
    assert c.post("/api/unified-inbox/send-sticker", json={
        "platform": "telegram", "chat_key": "1", "sticker_id": "x",
    }).status_code == 403


def test_viewer_readonly(env):
    c = env["client"]
    c.get("/_login", params={"role": "viewer"})
    assert c.post("/api/stickers/packs", json={"title": "x"}).status_code == 403
    # 读不受限
    assert c.get("/api/stickers/packs").status_code == 200


# ── 包/条目管理 ──────────────────────────────────────────────────────────────

def test_pack_crud_and_limits(env):
    c = env["client"]
    p = _mkpack(c, "第一包")
    assert c.get("/api/stickers/packs").json()["packs"][0]["title"] == "第一包"
    r = c.patch(f"/api/stickers/packs/{p['id']}", json={"title": "改名"})
    assert r.json()["pack"]["title"] == "改名"
    _mkpack(c, "二")
    _mkpack(c, "三")
    assert c.post("/api/stickers/packs",
                  json={"title": "超限"}).status_code == 409  # max_packs=3
    assert c.delete(f"/api/stickers/packs/{p['id']}").json()["ok"] is True
    assert c.get(f"/api/stickers/packs/{p['id']}/items").status_code == 404


def test_upload_normalize_dedupe_and_pack_full(env, tmp_path):
    c = env["client"]
    p = _mkpack(c)
    r = _upload(c, p["id"], [("a.png", _png()), ("b.png", _png(color=(0, 255, 0, 255))),
                             ("dup.png", _png()), ("junk.txt", b"nope"),
                             ("bad.png", b"not an image")])
    body = r.json()
    assert len(body["added"]) == 2, body
    assert body["deduped"] == 1
    reasons = {f["name"]: f["reason"] for f in body["failed"]}
    assert reasons["junk.txt"] == "ext"
    # 落盘根内应有 webp + png 兄弟件
    files = list((tmp_path / "sticker_packs").rglob("*"))
    exts = sorted({f.suffix for f in files if f.is_file()})
    assert exts == [".png", ".webp"]
    # 条目清单
    items = c.get(f"/api/stickers/packs/{p['id']}/items").json()["items"]
    assert len(items) == 2
    assert all(i["url"].startswith("/static/sticker_packs/") for i in items)
    assert all(isinstance(i.get("keywords"), list) for i in items)
    # pack_full：max_per_pack=4 → 再传 3 张只进 2 张
    r2 = _upload(c, p["id"], [("c.png", _png(color=(0, 0, 255, 255))),
                              ("d.png", _png(color=(9, 9, 9, 255))),
                              ("e.png", _png(color=(1, 2, 3, 255)))])
    b2 = r2.json()
    assert len(b2["added"]) == 2
    assert any(f["reason"] == "pack_full" for f in b2["failed"])


def test_item_delete_removes_files(env, tmp_path):
    c = env["client"]
    p = _mkpack(c)
    added = _upload(c, p["id"], [("a.png", _png())]).json()["added"][0]
    sid = added["id"]
    assert (tmp_path / "sticker_packs").rglob(f"{sid}.webp")
    r = c.delete(f"/api/stickers/packs/{p['id']}/items/{sid}")
    assert r.json()["ok"] is True
    assert not list((tmp_path / "sticker_packs").rglob(f"{sid}.*"))


# ── 收藏入站贴纸 ────────────────────────────────────────────────────────────

def test_collect_from_protocol_media(env, tmp_path, monkeypatch):
    c = env["client"]
    proot = tmp_path / "protocol_media"
    (proot / "telegram").mkdir(parents=True)
    (proot / "telegram" / "in1.webp").write_bytes(_png())
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.protocol_media_root", lambda: proot)
    r = c.post("/api/stickers/collect",
               json={"media_ref": "/static/protocol_media/telegram/in1.webp"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["item"]["pack_id"] == "collected"
    # 再收同一张 → 去重
    r2 = c.post("/api/stickers/collect",
                json={"media_ref": "/static/protocol_media/telegram/in1.webp"})
    assert r2.json().get("deduped") is True
    # 根外引用拒绝
    r3 = c.post("/api/stickers/collect",
                json={"media_ref": "/static/other/evil.webp"})
    assert r3.status_code == 404


# ── 官方包播种 ──────────────────────────────────────────────────────────────

def test_seed_official_range_expansion_idempotent(env, tmp_path, monkeypatch):
    c = env["client"]
    manifest = tmp_path / "official" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"packs": [{
        "id": "line-test", "title": "LINE Test", "sort": 1,
        "line_sticker_range": {
            "package_id": "11537", "from": 52002734, "to": 52002736},
    }]}), encoding="utf-8")
    monkeypatch.setattr(srt, "_OFFICIAL_MANIFEST", manifest)
    r = c.post("/api/stickers/seed-official")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "packs_added": 1, "items_added": 3}
    items = c.get("/api/stickers/packs/line-test/items").json()["items"]
    assert len(items) == 3 and all(i["line_only"] for i in items)
    assert items[0]["url"].startswith("https://stickershop.line-scdn.net/")
    # 幂等：再播不重复
    r2 = c.post("/api/stickers/seed-official")
    assert r2.json() == {"ok": True, "packs_added": 0, "items_added": 0}


def test_seed_official_local_files_dict_tags_and_containment(
        env, tmp_path, monkeypatch):
    """files 条目双形态：字典带 emoji/keywords 落库；字符串旧形态兼容（同 sha
    去重）；目录穿越/缺路径条目静默跳过；再播幂等。"""
    c = env["client"]
    official = tmp_path / "official"
    (official / "starter").mkdir(parents=True)
    (official / "starter" / "a.png").write_bytes(_png())
    (tmp_path / "evil.png").write_bytes(_png(color=(0, 255, 0, 255)))  # 目录外
    manifest = official / "manifest.json"
    manifest.write_text(json.dumps({"packs": [{
        "id": "starter-test", "title": "Starter", "sort": 1,
        "files": [
            {"file": "starter/a.png", "emoji": "😊",
             "keywords": ["好的", "ok", " "]},
            "starter/a.png",                      # 字符串旧形态：同 sha 去重
            {"file": "../evil.png", "emoji": "x"},  # 穿越 → 跳过
            {"emoji": "x"},                        # 缺路径 → 跳过
            "",                                    # 空串 → 跳过
        ]}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(srt, "_OFFICIAL_MANIFEST", manifest)
    r = c.post("/api/stickers/seed-official")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "packs_added": 1, "items_added": 1}
    items = c.get("/api/stickers/packs/starter-test/items").json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["emoji_tag"] == "😊"
    assert it["keywords"] == ["好的", "ok"]  # 空白关键词剔除
    assert it["url"].endswith(".webp") and not it["line_only"]
    # 幂等：再播零新增
    assert c.post("/api/stickers/seed-official").json() == {
        "ok": True, "packs_added": 0, "items_added": 0}


# ── 发送（四平台方案）────────────────────────────────────────────────────────

def _seed_file_sticker(client, animated=False):
    p = _mkpack(client, "发包")
    name = "a.gif" if animated else "a.png"
    if animated:
        ims = [Image.new("RGB", (64, 64), (i * 50, 0, 0)) for i in range(3)]
        buf = io.BytesIO()
        ims[0].save(buf, format="GIF", save_all=True, append_images=ims[1:],
                    duration=80, loop=0)
        data = buf.getvalue()
    else:
        data = _png()
    added = _upload(client, p["id"], [(name, data)]).json()["added"]
    assert added, "seed 上传失败"
    return added[0]


def _send(client, sticker_id, platform="telegram", chat_key="123",
          client_msg_id=""):
    return client.post("/api/unified-inbox/send-sticker", json={
        "platform": platform, "account_id": "default", "chat_key": chat_key,
        "sticker_id": sticker_id, "client_msg_id": client_msg_id})


def test_send_telegram_native_sticker(env):
    c, orch = env["client"], env["orch"]
    it = _seed_file_sticker(c)
    r = _send(c, it["id"], "telegram", chat_key="tg1")
    assert r.status_code == 200, r.text
    assert r.json()["sent_as"] == "sticker"
    call = orch.calls[-1]
    assert call["media_type"] == "sticker"
    assert call["mirror_media_type"] == "sticker"
    assert call["media_path"].endswith(".webp")
    # 出站副本落 protocol_media URL（非贴纸库原件——历史消息不随包删除破图）
    assert r.json()["media_ref"].startswith("/static/protocol_media/")


def test_send_telegram_animated_uses_gif_animation(env):
    c, orch = env["client"], env["orch"]
    it = _seed_file_sticker(c, animated=True)
    r = _send(c, it["id"], "telegram", chat_key="tg2")
    assert r.json()["sent_as"] == "sticker"
    call = orch.calls[-1]
    assert call["media_type"] == "animation"
    assert call["media_path"].endswith(".gif")
    assert call["mirror_media_type"] == "sticker"


def test_send_whatsapp_fallback_image_when_sidecar_old(env):
    c, orch = env["client"], env["orch"]
    it = _seed_file_sticker(c)
    r = _send(c, it["id"], "whatsapp", chat_key="wa1")
    assert r.json()["sent_as"] == "image"
    call = orch.calls[-1]
    assert call["media_type"] == "image" and call["media_path"].endswith(".png")


def test_send_whatsapp_native_when_sidecar_capable(env, monkeypatch):
    c, orch = env["client"], env["orch"]

    async def _yes(_cfg):
        return True

    monkeypatch.setattr(srt, "_wa_sticker_capable", _yes)
    it = _seed_file_sticker(c)
    r = _send(c, it["id"], "whatsapp", chat_key="wa2")
    assert r.json()["sent_as"] == "sticker"
    assert orch.calls[-1]["media_type"] == "sticker"


def test_send_messenger_image_fallback(env):
    c, orch = env["client"], env["orch"]
    it = _seed_file_sticker(c)
    r = _send(c, it["id"], "messenger", chat_key="ms1")
    assert r.json()["sent_as"] == "image"
    assert orch.calls[-1]["media_type"] == "image"


def test_send_line_native_shop_sticker(env):
    c, orch = env["client"], env["orch"]
    lw = _FakeLineWorker()
    orch.line_worker = lw
    st = ss.get_sticker_store()
    st.create_pack("line", pack_id="lp")
    row = st.add_sticker("lp", sticker_id="lp-1", line_package_id="11537",
                         line_sticker_id="52002734", url="https://cdn/x.png")
    r = _send(c, row["id"], "line", chat_key="u1")
    assert r.status_code == 200, r.text
    assert r.json()["sent_as"] == "sticker" and r.json()["native"] == "line"
    assert lw.sent == [("u1", "11537", "52002734")]
    assert orch.calls == [], "LINE 官方贴纸不走 send_media"


def test_send_line_id_sticker_to_other_platform_409(env):
    c = env["client"]
    st = ss.get_sticker_store()
    st.create_pack("line", pack_id="lp2")
    row = st.add_sticker("lp2", sticker_id="lp2-1", line_package_id="11537",
                         line_sticker_id="52002734")
    r = _send(c, row["id"], "telegram", chat_key="tg9")
    assert r.status_code == 409


def test_send_dedup_duplicate_client_msg_id(env):
    c = env["client"]
    it = _seed_file_sticker(c)
    r1 = _send(c, it["id"], "telegram", chat_key="dd1", client_msg_id="stk-x1")
    assert r1.json().get("duplicate") is None
    r2 = _send(c, it["id"], "telegram", chat_key="dd1", client_msg_id="stk-x1")
    assert r2.json().get("duplicate") is True


def test_send_unknown_sticker_404(env):
    c = env["client"]
    assert _send(c, "nope", "telegram").status_code == 404


def test_send_records_usage_recent(env):
    c = env["client"]
    it = _seed_file_sticker(c)
    _send(c, it["id"], "telegram", chat_key="rc1")
    recent = c.get("/api/stickers/packs").json()["recent"]
    assert recent and recent[0]["id"] == it["id"]


def test_send_no_media_worker_501(env):
    c, orch = env["client"], env["orch"]
    orch.media_owned = False
    it = _seed_file_sticker(c)
    assert _send(c, it["id"], "telegram").status_code == 501
