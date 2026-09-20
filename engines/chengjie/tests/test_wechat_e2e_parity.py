# -*- coding: utf-8 -*-
"""微信两条线 · 真实 app 端到端 + 与既有平台能力对齐门禁（实施97）。

与单测的区别：这里跑的是 **真实 ``create_app()``**（路由/收件箱 store/编排器/发送护栏/注册表全真），
只把腾讯接口换成进程内假企微服务器（``FakeWeCom``，按官方契约应答），验证：

线 A（微信客服）
  1. 接入向导保存凭证（``POST /api/setup/channels/wechat_kf``）→ 注册表自动开 ``mode=official`` 账号；
  2. 编排器拉起 ``WeChatKfWorker``（真 token 流程）→ ``orch.owns`` 成立、``send-caps`` 报可发媒体；
  3. 客户消息经 ``sync_msg`` 进统一收件箱（``/api/unified-inbox/chats`` 与 ``/thread`` 可见，昵称头像齐）；
  4. 坐席从工作台发送（``POST /api/unified-inbox/send``）→ 假服务器收到 ``kf/send_msg``，出站镜像进线程；
  5. 48h/5 条配额：第 6 条被护栏拦（409），客户再发一条即重置；自动链在剩 1 条时让路、敏感词 HOLD；
  6. ``msg_send_fail(fail_type=4)`` 事件 → 窗口关闭 → 发送被拦；
  7. 回调路由（真实加解密）→ worker 被唤醒并带 token 拉取；
  8. 向导现状 / 登录方式 / 就绪度三处元数据对 wechat_kf 自洽。

线 B（个人微信 PC 副驾）
  9. ``WeChatPcService`` + ``FakeBackend`` 对着真实桌面桥端点：入站进收件箱（账号自动以 desktop 入册）、
     ``DesktopOutboundQueue`` 命令被认领 → 五步守卫发送 → 回执 sent。

对齐矩阵（``test_parity_matrix_against_existing_platforms``）把「微信客服 vs 现有平台」逐项能力钉成回归网。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from src.integrations import wechat_kf as WK

_TOKEN = "QDG6eK"
_AES = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
_CORP = "ww1234567890"
_KFID = "wkKF0001"
_UID = "wmU1_external"


# ── 假企微服务器（按官方契约应答） ──────────────────────────────────────────

class FakeWeCom:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any], Any]] = []
        self.msgs: List[Dict[str, Any]] = []
        self.sent: List[Dict[str, Any]] = []
        self.sync_tokens: List[str] = []
        self.state_calls: List[Dict[str, Any]] = []
        self.customers = {_UID: {"external_userid": _UID, "nickname": "小明", "avatar": "https://a/b.png"}}
        self.extra_accounts: List[Dict[str, Any]] = []
        self.uploads: List[Dict[str, Any]] = []
        self._seq = 0

    def push_customer_text(self, text: str, uid: str = _UID) -> str:
        self._seq += 1
        mid = f"from_msgid_{self._seq}"
        self.msgs.append({"msgid": mid, "open_kfid": _KFID, "external_userid": uid,
                          "send_time": int(time.time()), "origin": 3, "msgtype": "text",
                          "text": {"content": text}})
        return mid

    def push_event(self, event: Dict[str, Any]) -> None:
        self._seq += 1
        self.msgs.append({"msgid": f"ev_{self._seq}", "open_kfid": _KFID, "send_time": int(time.time()),
                          "origin": 4, "msgtype": "event", "event": event})

    @staticmethod
    def _j(d: Dict[str, Any]) -> bytes:
        return json.dumps(d).encode("utf-8")

    async def __call__(self, method: str, url: str, *, params=None, json=None, data=None,
                       headers=None, timeout=None):
        params = dict(params or {})
        path = url.split("/cgi-bin/", 1)[1].split("?", 1)[0]
        self.calls.append((method, path, params, json))
        if path == "gettoken":
            return 200, self._j({"errcode": 0, "access_token": "TOK", "expires_in": 7200}), "application/json", {}
        if params.get("access_token") != "TOK":
            return 200, self._j({"errcode": 40014, "errmsg": "invalid access_token"}), "application/json", {}
        if path == "kf/account/list":
            return 200, self._j({"errcode": 0, "account_list": [
                {"open_kfid": _KFID, "name": "客服A", "manage_privilege": 1}] + list(self.extra_accounts)}), "application/json", {}
        if path == "media/upload":
            self.uploads.append({"type": params.get("type"), "size": len((data or {}).get("media", b"")) if isinstance(data, dict) else -1})
            return 200, self._j({"errcode": 0, "media_id": f"MEDIA{len(self.uploads)}"}), "application/json", {}
        if path == "kf/account/add":
            self._seq += 1
            kfid = f"wkNEW{self._seq}"
            self.extra_accounts.append({"open_kfid": kfid, "name": str((json or {}).get("name") or ""),
                                        "manage_privilege": 1, "media_id": (json or {}).get("media_id")})
            return 200, self._j({"errcode": 0, "open_kfid": kfid}), "application/json", {}
        if path == "kf/sync_msg":
            self.sync_tokens.append(str((json or {}).get("token") or ""))
            cur = str((json or {}).get("cursor") or "")
            start = int(cur) if cur.isdigit() else 0
            batch = self.msgs[start:]
            return 200, self._j({"errcode": 0, "msg_list": batch, "next_cursor": str(len(self.msgs)),
                                 "has_more": 0}), "application/json", {}
        if path == "kf/send_msg":
            self._seq += 1
            self.sent.append(dict(json or {}))
            return 200, self._j({"errcode": 0, "msgid": f"sent_{self._seq}"}), "application/json", {}
        if path == "kf/customer/batchget":
            ids = (json or {}).get("external_userid_list") or []
            return 200, self._j({"errcode": 0, "customer_list": [self.customers[i] for i in ids if i in self.customers]}), "application/json", {}
        if path in ("kf/service_state/get", "kf/service_state/trans"):
            self.state_calls.append({"path": path, **(json or {})})
            return 200, self._j({"errcode": 0, "service_state": 1, "msg_code": "MC"}), "application/json", {}
        if path == "auth/getuserinfo":
            code = str(params.get("code") or "")
            if code == "good-code":
                return 200, self._j({"errcode": 0, "userid": "zhangsan"}), "application/json", {}
            if code == "outsider":
                return 200, self._j({"errcode": 0, "openid": "oXYZ"}), "application/json", {}
            return 200, self._j({"errcode": 40029, "errmsg": "invalid code"}), "application/json", {}
        if path == "kf/servicer/list":
            return 200, self._j({"errcode": 0, "servicer_list": [
                {"userid": "zhangsan", "status": 1}, {"userid": "lisi", "status": 0}]}), "application/json", {}
        if path == "kf/add_contact_way":
            return 200, self._j({"errcode": 0, "url": "https://work.weixin.qq.com/kf/kfcXXXX"}), "application/json", {}
        return 200, self._j({"errcode": 0}), "application/json", {}


@pytest.fixture()
def fake_wecom(monkeypatch) -> FakeWeCom:
    fake = FakeWeCom()

    async def _http(self, method, url, *, params=None, json_body=None, data=None, headers=None, timeout=None):
        return await fake(method, url, params=params, json=json_body, data=data, headers=headers, timeout=timeout)

    monkeypatch.setattr(WK.WeChatKfClient, "_http", _http)
    return fake


@pytest.fixture()
def wx_env(config_dir, _config_data, fake_wecom, tmp_path):
    """真实 app：基础测试配置 + 微信客服块 + 编排器开 + 桌面桥开。"""
    from src.utils.config_manager import ConfigManager
    from src.utils.audit_store import AuditStore
    from src.web.admin import create_app
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore

    cfg = dict(_config_data["cfg"])
    cfg["wechat_kf"] = {
        "enabled": True, "corpid": _CORP, "secret": "s3cret", "open_kfid": _KFID,
        "poll_interval_sec": 2, "welcome_text": "您好，有什么可以帮您？",
        "callback": {"token": _TOKEN, "encoding_aes_key": _AES},
    }
    cfg["platform_login"] = {"orchestrator_enabled": True, "wechat_kf": {"enabled": True}}
    cfg["inbox"] = {"read_from_store": True,
                    "l2_autosend": {"desktop_bridge": {"enabled": True}}}
    (config_dir / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(config_dir / "config.yaml"))
    asyncio.run(cm.load())
    app = create_app(cm, audit_store=AuditStore(db_path=config_dir / "audit.db"), boot_ts=0,
                     telegram_client=None, event_tracker=None, log_buffer=None)
    # 完整 app 的 inbox_store 由 main.py 生命周期挂载；测试里按其它收件箱 e2e 用例同款手挂
    from src.inbox.store import InboxStore
    app.state.inbox_store = InboxStore(config_dir / "inbox_e2e.db")
    # ⚠ 桌面出站队列默认落 CWD 相对 ``config/desktop_outbound.db``（不走 AITR_DATA_DIR）——
    # pytest cwd＝引擎根，会把测试命令写进仓库/生产数据目录（本文件首版实锤 7 行）。隔离到 tmp。
    from src.inbox.desktop_outbound import DesktopOutboundQueue, reset_desktop_outbound_queue
    reset_desktop_outbound_queue(DesktopOutboundQueue(str(config_dir / "desktop_outbound_e2e.db")))
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("admin", "test-token-123", ROLE_MASTER)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.get("/login")
        client.post("/login", data={"username": "admin", "password": "test-token-123"}, follow_redirects=True)
        client.headers.update({"Authorization": "Bearer test-token-123"})
        yield {"app": app, "client": client, "cm": cm, "fake": fake_wecom, "config_dir": config_dir}
    # 停编排器，免得 worker 轮询任务跨测试；队列单例归零（下一个测试重建自己的）
    try:
        from src.integrations.account_orchestrator import get_orchestrator_if_running
        orch = get_orchestrator_if_running()
        if orch is not None:
            asyncio.run(orch.stop_loop())
    except Exception:
        pass
    try:
        reset_desktop_outbound_queue(None)
    except Exception:
        pass


def _chats(client) -> List[Dict[str, Any]]:
    r = client.get("/api/unified-inbox/chats", params={"limit": 50})
    assert r.status_code == 200, r.text
    d = r.json()
    return d.get("chats") or d.get("items") or d.get("conversations") or []


def _thread(client, platform: str, account_id: str, chat_key: str) -> List[Dict[str, Any]]:
    r = client.get("/api/unified-inbox/thread", params={
        "platform": platform, "account_id": account_id, "chat_key": chat_key, "limit": 50})
    assert r.status_code == 200, r.text
    d = r.json()
    return d.get("messages") or d.get("items") or []


# ── 线 A ────────────────────────────────────────────────────────────────────

async def test_wechat_kf_end_to_end_through_real_app(wx_env):
    client, cm, fake, app = wx_env["client"], wx_env["cm"], wx_env["fake"], wx_env["app"]
    from src.integrations.account_orchestrator import ensure_builtin_workers, get_orchestrator
    from src.integrations.account_registry import get_account_registry
    from src.inbox import kf_window_guard as G

    # 1. 向导保存 → 账号自动开通（走真实路由；overlay 落 tmp）
    r = client.post("/api/setup/channels/wechat_kf", json={"values": {
        "corpid": _CORP, "secret": "s3cret", "open_kfid": _KFID}})
    assert r.status_code == 200 and r.json().get("ok"), r.text
    assert r.json().get("official_account") == _KFID
    row = get_account_registry().get("wechat_kf", _KFID)
    assert row and row["mode"] == "official" and row["status"] == "online"
    ch = r.json()["channel"]
    assert ch["id"] == "wechat_kf" and ch["ready"] and ch["ready_by"] == "api"

    # 2. 编排器拉起 worker（真 token 流程经假服务器）
    cfg = cm.config or {}
    ensure_builtin_workers(cfg)
    orch = get_orchestrator(cfg)
    assert await orch.start_account(row) is True
    assert orch.owns("wechat_kf", _KFID)
    worker = orch.worker_for("wechat_kf", _KFID)
    assert worker.status()["type"] == "wechat_kf" and worker.open_kfid == _KFID
    caps = client.get("/api/unified-inbox/send-caps", params={"platform": "wechat_kf", "account_id": _KFID}).json()
    assert caps.get("can_media") is True and caps.get("can_voice") is True, caps
    assert caps.get("bubbles", caps.get("bubbles_on", False)) in (False, None, 0), "配额平台不亮分条 chip"

    # 3. 客户消息 → 收件箱
    fake.push_customer_text("你好，想问下报价")
    assert await worker.sync_once() == 1
    chats = _chats(client)
    mine = [c for c in chats if c.get("platform") == "wechat_kf"]
    assert mine, chats[:3]
    conv = mine[0]
    conv_id = conv["conversation_id"]
    assert conv["account_id"] == _KFID and conv["chat_key"] == f"wxkf:user:{_UID}"
    assert conv.get("name") == "小明", conv
    msgs = _thread(client, "wechat_kf", _KFID, f"wxkf:user:{_UID}")
    assert any(m.get("text") == "你好，想问下报价" and m.get("direction") == "in" for m in msgs), msgs

    # 4. 坐席从工作台发送 → 假服务器收到 → 出站镜像
    r = client.post("/api/unified-inbox/send", json={
        "platform": "wechat_kf", "account_id": _KFID, "chat_key": f"wxkf:user:{_UID}", "text": "在的，稍等"})
    assert r.status_code == 200, r.text
    assert fake.sent and fake.sent[-1]["touser"] == _UID and fake.sent[-1]["open_kfid"] == _KFID
    assert fake.sent[-1]["msgtype"] == "text" and fake.sent[-1]["text"]["content"] == "在的，稍等"
    msgs = _thread(client, "wechat_kf", _KFID, f"wxkf:user:{_UID}")
    assert any(m.get("text") == "在的，稍等" and m.get("direction") == "out" for m in msgs)

    # 5. 配额：再发 4 条到满 5 条；第 6 条被护栏拦
    for i in range(4):
        r = client.post("/api/unified-inbox/send", json={
            "platform": "wechat_kf", "account_id": _KFID, "chat_key": f"wxkf:user:{_UID}", "text": f"第{i + 2}条"})
        assert r.status_code == 200, r.text
    assert len(fake.sent) == 5
    r = client.post("/api/unified-inbox/send", json={
        "platform": "wechat_kf", "account_id": _KFID, "chat_key": f"wxkf:user:{_UID}", "text": "第6条"})
    assert r.status_code in (409, 502), (r.status_code, r.text)
    assert G.REASON_QUOTA_EXHAUSTED in r.text
    assert len(fake.sent) == 5, "被拦的那条绝不能真发出去"
    snap = G.snapshot("wechat_kf", _KFID, f"wxkf:user:{_UID}", config=cfg)
    assert snap["sent"] == 5 and snap["remaining"] == 0 and snap["reason"] == G.REASON_QUOTA_EXHAUSTED
    # 工作台可见面：send-caps?chat_key= 带 reply_window（与抖音/TikTok 同一信息带契约）→ 坐席先看见「0/5」
    caps2 = client.get("/api/unified-inbox/send-caps", params={
        "platform": "wechat_kf", "account_id": _KFID, "chat_key": f"wxkf:user:{_UID}"}).json()
    rw = caps2.get("reply_window") or {}
    assert rw.get("cap") == 5 and rw.get("sent") == 5 and rw.get("remaining") == 0, caps2
    assert rw.get("no_inbound") is False and rw.get("expired") is False and rw.get("remaining_sec", 0) > 47 * 3600
    assert rw.get("manual_allowed") is False and rw.get("reason") == G.REASON_QUOTA_EXHAUSTED

    # 客户再发 → 本轮重置 → 自动链可发；剩 1 条时自动链让路；敏感词 HOLD
    fake.push_customer_text("好的")
    await worker.sync_once()
    res = await orch.send("wechat_kf", _KFID, f"wxkf:user:{_UID}", "收到，马上给您报价", origin="auto")
    assert res.get("delivered") is True and fake.sent[-1]["text"]["content"] == "收到，马上给您报价"
    for i in range(3):
        assert (await orch.send("wechat_kf", _KFID, f"wxkf:user:{_UID}", f"auto{i}", origin="auto")).get("delivered")
    held = await orch.send("wechat_kf", _KFID, f"wxkf:user:{_UID}", "最后一条", origin="auto")
    assert held.get("blocked") == G.REASON_RESERVED_FOR_MANUAL
    assert len(fake.sent) == 9
    sens = await orch.send("wechat_kf", _KFID, f"wxkf:user:{_UID}", "可以用 USDT 付款", origin="manual")
    assert sens.get("delivered") is True, "人工原文不拦敏感词"
    fake.push_customer_text("再问一句")
    await worker.sync_once()
    sens_auto = await orch.send("wechat_kf", _KFID, f"wxkf:user:{_UID}", "可以用 USDT 付款", origin="auto")
    assert str(sens_auto.get("blocked") or "").startswith(G.REASON_SENSITIVE)

    # 6. 发送失败事件（fail_type=4 超 48h）→ 关窗
    fake.push_event({"event_type": "msg_send_fail", "open_kfid": _KFID, "external_userid": _UID,
                     "fail_msgid": fake.sent[-1].get("msgid", "x"), "fail_type": 4})
    await worker.sync_once()
    closed = await orch.send("wechat_kf", _KFID, f"wxkf:user:{_UID}", "还在吗", origin="manual")
    assert closed.get("blocked") == G.REASON_WINDOW_CLOSED

    # 7. 回调（真实加解密）→ worker.kick 带 token → 下一次 sync 带 token
    crypto = WK.KfCallbackCrypto(_TOKEN, _AES, _CORP)
    inner = (f"<xml><ToUserName><![CDATA[{_CORP}]]></ToUserName><CreateTime>1</CreateTime>"
             "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[kf_msg_or_event]]></Event>"
             f"<Token><![CDATA[CBTOKEN]]></Token><OpenKfId><![CDATA[{_KFID}]]></OpenKfId></xml>").encode()
    enc = crypto.encrypt(inner)
    body = f"<xml><ToUserName><![CDATA[{_CORP}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
    # 企微服务器不带任何 cookie/CSRF 头——用干净客户端打，钉住回调路径的 CSRF 豁免（此前被登录客户端掩盖）
    from fastapi.testclient import TestClient as _TC
    r = _TC(app).post("/wechat/kf/callback", params={"msg_signature": crypto.signature("9", "8", enc),
                                                     "timestamp": "9", "nonce": "8"}, content=body.encode())
    assert r.status_code == 200 and r.text == "success", r.text
    for _ in range(80):
        await asyncio.sleep(0.05)
        if "CBTOKEN" in fake.sync_tokens:
            break
    assert "CBTOKEN" in fake.sync_tokens, fake.sync_tokens[-5:]
    echo = crypto.encrypt(b"9876543210")
    r = client.get("/wechat/kf/callback", params={"msg_signature": crypto.signature("1", "2", echo),
                                                  "timestamp": "1", "nonce": "2", "echostr": echo})
    assert r.status_code == 200 and r.text == "9876543210"

    # 7b. 坐席在工作台「转企微人工」：自动挑接待中的客服 → service_state/trans(3) → 本端会话打接管标（AI 停手）
    r = client.get("/api/unified-inbox/kf/session-state", params={"account_id": _KFID, "chat_key": f"wxkf:user:{_UID}"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["service_state"] == 1
    # 会话头状态条数据：send-caps 带 kf_session（30s 缓存：连拉两次只打一次企微接口）
    from src.integrations.wechat_kf_webhook import invalidate_kf_session_snapshot
    invalidate_kf_session_snapshot(_KFID, f"wxkf:user:{_UID}")   # 第 5 步的 send-caps 已经热了缓存，先清掉再数
    n_state_calls = len([c for c in fake.state_calls if c["path"] == "kf/service_state/get"])
    for _ in range(2):
        caps3 = client.get("/api/unified-inbox/send-caps", params={
            "platform": "wechat_kf", "account_id": _KFID, "chat_key": f"wxkf:user:{_UID}"}).json()
        assert caps3.get("kf_session", {}).get("state") == 1 and caps3["kf_session"]["state_key"] == "bot", caps3
    assert len([c for c in fake.state_calls if c["path"] == "kf/service_state/get"]) == n_state_calls + 1
    r = client.post("/api/unified-inbox/kf/transfer", json={"account_id": _KFID, "chat_key": f"wxkf:user:{_UID}"})
    assert r.status_code == 200, r.text
    tr = r.json()
    assert tr["ok"] and tr["servicer_userid"] == "lisi", tr           # status=0（接待中）优先于 zhangsan
    trans = fake.state_calls[-1]
    assert trans["path"] == "kf/service_state/trans" and trans["service_state"] == 3
    assert trans["servicer_userid"] == "lisi" and trans["external_userid"] == _UID and trans["open_kfid"] == _KFID
    am = app.state.inbox_store.get_automation_mode_meta(conv_id)
    assert am and am.get("mode") == "manual" and "takeover" in str(am.get("source") or ""), am
    # 转人工后状态缓存失效：下一次 send-caps 重新打企微接口
    n_state_calls = len([c for c in fake.state_calls if c["path"] == "kf/service_state/get"])
    client.get("/api/unified-inbox/send-caps", params={"platform": "wechat_kf", "account_id": _KFID, "chat_key": f"wxkf:user:{_UID}"})
    assert len([c for c in fake.state_calls if c["path"] == "kf/service_state/get"]) == n_state_calls + 1
    r = client.post("/api/unified-inbox/kf/transfer", json={"account_id": "kf_not_running", "chat_key": "wxkf:user:x"})
    assert r.status_code == 409
    r = client.post("/api/unified-inbox/kf/close", json={"account_id": _KFID, "chat_key": f"wxkf:user:{_UID}"})
    assert r.status_code == 200 and r.json()["ok"] and fake.state_calls[-1]["service_state"] == 4

    # 8. 元数据自洽：向导现状 / 登录方式 / 就绪度
    st = next(c for c in client.get("/api/setup/channels").json()["channels"] if c["id"] == "wechat_kf")
    assert st["ready"] and st["paths"]["api"]["is_transport"] and st["paths"]["login"] is None
    from src.integrations.platform_login import list_modes, mode_available
    modes = list_modes("wechat_kf")
    assert [m["mode"] for m in modes] == ["official"] and mode_available("wechat_kf", "official")
    from src.integrations.platform_readiness import diagnose_mode
    diag = diagnose_mode("wechat_kf", "official", cfg)
    assert not diag.get("blockers"), diag

    await orch.stop_account(f"wechat_kf:{_KFID}")


# ── 线 B ────────────────────────────────────────────────────────────────────

async def test_wechat_pc_service_against_real_desktop_bridge(wx_env):
    client, cm, app = wx_env["client"], wx_env["cm"], wx_env["app"]
    from src.inbox.desktop_outbound import get_desktop_outbound_queue
    from src.integrations.account_registry import get_account_registry
    from src.integrations.wechat_pc.backend import Bubble, FakeBackend, SessionRow
    from src.integrations.wechat_pc.policy import resolve_policy
    from src.integrations.wechat_pc.send_guard import GuardedSender
    from src.integrations.wechat_pc.service import BridgeClient, WeChatPcService

    http_log: List[Any] = []

    def _http(method, url, body):
        path = url.split("http://x", 1)[1] if url.startswith("http://x") else url
        r = client.request(method, path, json=body) if body is not None else client.request(method, path)
        try:
            d = r.json()
        except Exception:
            d = {}
        http_log.append((method, path, r.status_code, str(body)[:120], str(d)[:200]))
        return r.status_code, d

    from src.integrations.wechat_pc.identity import ChatIdentityCache
    bridge = BridgeClient("http://x", "test-token-123", http=_http)
    fb = FakeBackend()
    fb.sessions = [SessionRow("张三", unread=1)]
    fb.messages["张三"] = [Bubble("在吗，报个价", runtime_id="1")]
    fb.wxids["张三"] = "微信号: zhangsan_88"
    policy = resolve_policy({"platform_login": {"wechat_pc": {"tier": "auto_reply", "risk_ack": True,
                                                              "work_hours": [0, 24]}}})
    identity_path = str(wx_env["config_dir"] / "wechat_pc_identity.json")
    svc = WeChatPcService(fb, bridge, account_id="wx-pc-1", policy=policy,
                          identity=ChatIdentityCache(path=identity_path),
                          sender=GuardedSender(fb, sleep=lambda s: None, read_pause_sec=0, echo_wait_sec=0),
                          connected_at=time.time() - 30 * 86400)
    s = svc.tick()
    assert s["readable"] and s["inbound"] == 1
    row = get_account_registry().get("wechat", "wx-pc-1")
    assert row and row["mode"] == "desktop" and row["status"] == "online", "桌面桥首见即入册"
    chats = [c for c in _chats(client) if c.get("platform") == "wechat"]
    assert chats and chats[0]["chat_key"] == "wx:id:zhangsan_88" and chats[0].get("name") == "张三"

    # 出站：经受控队列（enqueue 内建闸门）→ 服务认领 → 五步守卫 → 回执 sent
    q = get_desktop_outbound_queue()
    res = q.enqueue("wechat", "wx-pc-1", "wx:id:zhangsan_88", "在的，报价 99 元", config=cm.config or {})
    assert res.get("enqueued"), res
    s2 = svc.tick()
    assert s2["sent"] == 1 and fb.messages["张三"][-1].text == "在的，报价 99 元" and fb.messages["张三"][-1].is_self
    stats = client.get("/api/desktop/outbound/stats").json()
    assert stats.get("ok") and int((stats.get("summary") or {}).get("sent", 0)) >= 1, stats
    # 坐席从工作台手动发送 → 桌面桥回落入队（kind=manual）→ 半自动档服务带身份核对发出 → 回显不重复镜像
    r = client.post("/api/unified-inbox/send", json={
        "platform": "wechat", "account_id": "wx-pc-1", "chat_key": "wx:id:zhangsan_88", "text": "工作台发的"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body.get("result") or body).get("queued") is True or body.get("ok"), body
    # 模拟驱动进程重启：新服务只靠落盘的身份映射就能把 wx:id:<微信号> 定位回会话格
    semi = WeChatPcService(fb, bridge, account_id="wx-pc-1",
                           policy=resolve_policy({"platform_login": {"wechat_pc": {"tier": "semi", "work_hours": [0, 24]}}}),
                           identity=ChatIdentityCache(path=identity_path),
                           sender=GuardedSender(fb, sleep=lambda s: None, read_pause_sec=0, echo_wait_sec=0),
                           connected_at=time.time() - 30 * 86400)
    semi._last_inbound["wx:id:zhangsan_88"] = time.time()
    http_log.clear()
    s3 = semi.tick()
    assert s3["sent"] == 1 and fb.messages["张三"][-1].text == "工作台发的" and fb.messages["张三"][-1].is_self, (
        s3, semi.stats.as_dict(), body, http_log[-6:], fb.actions[-6:])
    # 出站镜像由桥端回流（与桌面壳 DOM 同步同一惯例）：发送路由/队列不落行，下一轮扫到己方回显
    # 才镜像一条 out——工作台里这句话恰好一行
    fb.sessions[0].unread = 1
    before = [m for m in _thread(client, "wechat", "wx-pc-1", "wx:id:zhangsan_88") if m.get("text") == "工作台发的"]
    semi.tick()
    after = [m for m in _thread(client, "wechat", "wx-pc-1", "wx:id:zhangsan_88") if m.get("text") == "工作台发的"]
    assert before == [] and len(after) == 1 and after[0].get("direction") == "out", (before, after)
    # 副驾档：同样的命令永不认领
    fb2 = FakeBackend()
    fb2.sessions = [SessionRow("张三", unread=0)]
    svc2 = WeChatPcService(fb2, bridge, account_id="wx-pc-1", policy=resolve_policy({}))
    q.enqueue("wechat", "wx-pc-1", "wx:id:zhangsan_88", "不该发出去", config=cm.config or {})
    svc2.tick()
    assert not any(b.text == "不该发出去" for b in fb2.messages.get("张三", []))
    # 驱动心跳 → 工作台账号名录里该 desktop 账号带 bridge 在线态（每轮 tick 都报，含只读档）
    hb_calls = [c for c in http_log if c[1] == "/api/desktop/heartbeat"]
    assert hb_calls and hb_calls[-1][2] == 200, hb_calls[-1:]
    summary = client.get("/api/unified-inbox/chats?limit=1").json().get("accounts_summary") or []
    me = next((r for r in summary if r.get("platform") == "wechat" and r.get("account_id") == "wx-pc-1"), None)
    assert me and me["status"] == "desktop" and isinstance(me.get("bridge"), dict), me
    assert me["bridge"]["alive"] is True and me["bridge"]["kind"] == "pcui" and me["bridge"]["tier"] == "copilot"
    assert me["bridge"]["readonly"] is True, "副驾档不发送 → readonly"
    # 心跳不复活运营移除的账号：status 仍由运营态决定，只更新 meta
    row = get_account_registry().get("wechat", "wx-pc-1")
    assert row["status"] == "online" and row["meta"]["bridge_heartbeat"]["kind"] == "pcui"


# ── 实施97 · 接入引导页后端（微信客服五步 / 个人微信六步）────────────────────

def test_wechat_kf_connect_guide_backend(wx_env, monkeypatch):
    """五步向导的每一步都有后端支撑：出站 IP → 凭证测试三检查 → 客服账号列表/新建 → 绑定拉起 → 客户二维码 → AI 接待；
    个人微信：环境检测 / 档位（全自动强制知情同意）/ 启动命令；两张引导页可渲染。"""
    client, cm, fake, app = wx_env["client"], wx_env["cm"], wx_env["fake"], wx_env["app"]
    import src.web.routes.wechat_kf_setup_routes as R
    from src.integrations.account_registry import get_account_registry
    from src.integrations.wechat_kf_setup import reset_egress_cache
    reset_egress_cache()
    monkeypatch.setattr(R, "fetch_egress_ip", lambda: {"ok": True, "ip": "8.8.8.8", "source": "test"})

    # ① 出站 IP
    r = client.get("/api/setup/wechat_kf/egress-ip")
    assert r.status_code == 200 and r.json()["ip"] == "8.8.8.8"
    # ② 凭证测试（不保存）：三项检查全绿 + 账号卡
    r = client.post("/api/setup/wechat_kf/test", json={"corpid": _CORP, "secret": "s3cret"})
    d = r.json()
    assert r.status_code == 200 and d["ok"] and [c["id"] for c in d["checks"]] == ["credentials", "kf_permission", "manageable_accounts"]
    assert d["egress_ip"] == "8.8.8.8" and any(a["open_kfid"] == _KFID and a["name"] == "客服A" for a in d["accounts"])
    # 没填凭证 → 用已保存的配置测（向导重进时不必重填 Secret）
    r = client.post("/api/setup/wechat_kf/test", json={"corpid": "", "secret": ""})
    assert r.json()["ok"] is True and r.json()["corpid"] == _CORP
    # ③ 客服账号列表 / 新建（头像默认品牌图 → media/upload → kf/account/add）
    r = client.get("/api/setup/wechat_kf/accounts")
    assert r.status_code == 200 and r.json()["ok"] and any(a["open_kfid"] == _KFID for a in r.json()["accounts"])
    r = client.post("/api/setup/wechat_kf/accounts", json={"name": "售后客服"})
    d = r.json()
    assert r.status_code == 200 and d["ok"] and d["open_kfid"].startswith("wkNEW"), d
    assert fake.uploads and fake.uploads[-1]["type"] == "image", "头像先经 media/upload（默认品牌图）"
    new_kfid = d["open_kfid"]
    assert any(a["open_kfid"] == new_kfid and a["name"] == "售后客服" for a in client.get("/api/setup/wechat_kf/accounts").json()["accounts"])
    r = client.post("/api/setup/wechat_kf/accounts", json={"name": ""})
    assert r.status_code == 400
    # 绑定：注册表 official 行 + 配置 open_kfid + 热拉起（编排器已开）
    r = client.post("/api/setup/wechat_kf/bind", json={"accounts": [{"open_kfid": new_kfid, "name": "售后客服"}]})
    d = r.json()
    assert r.status_code == 200 and d["ok"] and d["accounts"][0]["account_id"] == new_kfid and d["accounts"][0]["status"] == "online"
    row = get_account_registry().get("wechat_kf", new_kfid)
    assert row and row["mode"] == "official" and row["label"] == "售后客服" and row["meta"]["open_kfid"] == new_kfid
    assert (cm.config.get("wechat_kf") or {}).get("open_kfid") == new_kfid
    assert any(a["open_kfid"] == new_kfid and a["bound"] for a in client.get("/api/setup/wechat_kf/accounts").json()["accounts"])
    # ⑤ 客户二维码：企微返回链接 → 服务端渲染 PNG data URL
    r = client.post("/api/setup/wechat_kf/contact-way", json={"open_kfid": new_kfid})
    d = r.json()
    assert d["ok"] and d["url"].startswith("https://work.weixin.qq.com/kf/") and d["qr_image"].startswith("data:image/png;base64,")
    # ④ AI 接待：档位 + 人设 + 欢迎语
    r = client.post("/api/setup/wechat_kf/reception", json={"open_kfid": new_kfid, "tier": "review",
                                                            "persona_id": "", "welcome_text": "您好，我是售后助手"})
    d = r.json()
    assert r.status_code == 200 and d["ok"] and d["tier"] == "review" and d["welcome_saved"] is True
    assert (cm.config.get("wechat_kf") or {}).get("welcome_text") == "您好，我是售后助手"
    meta = get_account_registry().get("wechat_kf", new_kfid)["meta"]
    assert meta.get("persona_ids") == [] and meta.get("persona_id_auto") is False
    r = client.post("/api/setup/wechat_kf/reception", json={"open_kfid": "wk_unbound", "tier": "review"})
    assert r.status_code == 404
    r = client.post("/api/setup/wechat_kf/reception", json={"open_kfid": new_kfid, "tier": "yolo"})
    assert r.status_code == 400

    # 个人微信：环境 / 档位 / 启动命令
    r = client.get("/api/setup/wechat_pc/env")
    assert r.status_code == 200 and {"os_windows", "running", "version_ok", "copilot", "can_manage", "driver",
                                     "installed", "supervisor", "voice"} <= set(r.json())
    # 2026-09-19 P1 语音通路：/env 并入 voice 静态探测（可选能力：字段齐、绝不抛，非 Windows 也返回骨架）
    v = r.json()["voice"]
    assert v is None or {"voice_version_ok", "audio_libs", "cable", "ready", "min_version"} <= set(v), v
    # 回环自测 / 采样率对齐：主管专属；打桩 env_check / audio_cable，CI 没有声卡也能验路由契约
    import src.integrations.wechat_pc.env_check as _envmod
    import src.integrations.wechat_pc.audio_cable as _cable
    calls = {}

    def _fake_voice_env(**kw):
        calls["selftest"] = kw
        return {"voice_version_ok": True, "audio_libs": True, "ready": True,
                "cable": {"present": True, "rates_match": True,
                          "selftest": {"ok": True, "dominant_hz": 1000.2, "rms": 0.21}}}
    monkeypatch.setattr(_envmod, "voice_environment", _fake_voice_env)
    r = client.post("/api/setup/wechat_pc/voice/selftest")
    d = r.json()
    assert r.status_code == 200 and d["ok"] is True and d["selftest"]["dominant_hz"] == 1000.2, d
    assert calls["selftest"] == {"selftest": True}, "自测端点必须真跑回环（selftest=True），不是复用静态探测"
    monkeypatch.setattr(_cable, "available", lambda: True)
    monkeypatch.setattr(_cable, "align_formats", lambda *a, **k: {"ok": True, "before": 44100, "after": 48000, "render_sr": 48000})
    monkeypatch.setattr(_cable, "cable_status", lambda **k: {"present": True, "rates_match": True, "ready": True})
    r = client.post("/api/setup/wechat_pc/voice/align")
    d = r.json()
    assert r.status_code == 200 and d["ok"] is True and d["align"]["after"] == 48000 and d["cable"]["ready"] is True, d
    monkeypatch.setattr(_cable, "available", lambda: False)
    d = client.post("/api/setup/wechat_pc/voice/align").json()
    assert d["ok"] is False and d["error"] == "audio_libs_missing", "缺音频库 → 明确原因，不是 500"
    r = client.get("/api/setup/wechat_pc/policy")
    assert r.status_code == 200 and r.json()["tier"] in ("copilot", "semi", "auto_reply")
    r = client.post("/api/setup/wechat_pc/policy", json={"tier": "auto_reply"})
    assert r.status_code == 400 and "risk_ack_required" in r.text, "全自动必须知情同意（服务端强制）"
    # 模拟出厂态（fixture 为线 B 其他用例预开了桥）：桥关 → 页面能看见「出站通道未开」
    assert cm.save_overlay_patch({"inbox": {"l2_autosend": {"desktop_bridge": {"enabled": False}}}})
    assert client.get("/api/setup/wechat_pc/policy").json()["bridge_enabled"] is False, "出厂桥关"
    r = client.post("/api/setup/wechat_pc/policy", json={"tier": "auto_reply", "risk_ack": True, "work_hours": [9, 21]})
    d = r.json()
    assert r.status_code == 200 and d["tier"] == "auto_reply" and d["risk_ack"] is True and d["work_hours"] == [9, 21]
    assert d["restart_required"] is False and d["hot_reload"] is True and d["risk_ack_at"] > 0, "驱动按配置文件热生效"
    assert d["saved_at"] > 0 and client.get("/api/setup/wechat_pc/policy").json()["saved_at"] == d["saved_at"], \
        "保存时间落 overlay：页面刷新/深链到第③步也能把第②步标为真完成"
    blk = ((cm.config.get("platform_login") or {}).get("wechat_pc")) or {}
    assert blk.get("tier") == "auto_reply" and blk.get("risk_ack") is True and blk.get("risk_ack_at")
    # 2026-09-19：发送档位必须同时打开桌面桥出站通道，否则手发 400 / 全自动只拟稿（实测缺口）
    assert d["bridge_saved"] is True and d["bridge_enabled"] is True
    assert ((cm.config.get("inbox") or {}).get("l2_autosend") or {}).get("desktop_bridge", {}).get("enabled") is True
    r = client.post("/api/setup/wechat_pc/policy", json={"tier": "copilot"})
    assert r.json()["bridge_saved"] is None and r.json()["bridge_enabled"] is True, "降回只读不关桥（别的桌面账号在用）"
    r = client.post("/api/setup/wechat_pc/policy", json={"tier": "auto_reply"})
    assert r.status_code == 200 and r.json()["bridge_saved"] is None, "桥已开不重复写"
    r = client.get("/api/setup/wechat_pc/start-command?account_id=my-wx")
    d = r.json()
    assert "wechat_pc_devlink.ps1" in d["command"] and "-AccountId my-wx" in d["command"]
    assert "-Tier" not in d["command"] and "-Tier" not in d["autostart_command"], "档位不再写死进命令行（配置文件权威）"
    assert d["prepared"] is False and "<" not in d["command"], "命令里是真实路径，不再有占位符"
    # 主管点第 ③ 步：后端把管理员令牌落到数据目录，之后 prepared=True
    r = client.post("/api/setup/wechat_pc/prepare", json={})
    assert r.status_code == 200 and r.json()["ok"]
    tf = r.json()["token_file"]
    from pathlib import Path as _P
    assert _P(tf).exists() and _P(tf).read_text(encoding="utf-8").strip() == (cm.config.get("web_admin") or {}).get("auth_token")
    assert str(wx_env["config_dir"]) in tf, "令牌文件落在后端配置目录下的 wechat_pc/"
    d = client.get("/api/setup/wechat_pc/start-command").json()
    assert d["prepared"] is True and tf in d["command"] and tf in d["autostart_command"]

    # 一键启停（2026-09-19 P1）：后端托管驱动子进程——用假 Popen，不真拉 python
    from src.web.routes.wechat_pc_setup_routes import get_or_create_supervisor

    class _P:
        def __init__(self, cmd, **kw):
            self.cmd, self.kw, self.pid, self._rc = list(cmd), kw, 5150, None

        def poll(self):
            return self._rc

        def kill(self):
            self._rc = 1

        def wait(self, timeout=None):
            return self._rc

    spawned: List[Any] = []

    def _popen(cmd, **kw):
        p = _P(cmd, **kw)
        spawned.append(p)
        return p

    app.state.wechat_pc_supervisor = None
    sup = get_or_create_supervisor(app, popen=_popen)
    sup.driver_ready_provider = lambda: {"ok": True, "uiautomation": True}
    sup._assign_job = lambda pid: None
    sup._kill_tree = lambda proc: proc.kill()
    import src.integrations.wechat_pc.env_check as _ec
    monkeypatch.setattr(_ec, "driver_ready", lambda: {"ok": True, "uiautomation": True, "os_windows": True, "python": "py"})
    try:
        st = client.get("/api/setup/wechat_pc/copilot/status").json()
        assert st["ok"] and st["state"] in ("idle", "offline") and st["managed"] is False
        r = client.post("/api/setup/wechat_pc/copilot/start")
        d = r.json()
        assert r.status_code == 200 and d["ok"] and d["action"] == "start" and d["state"] == "starting" and d["pid"] == 5150
        cmd = spawned[0].cmd
        assert "src.integrations.wechat_pc" in cmd and "--tier" not in cmd and "--token" not in cmd
        env = spawned[0].kw["env"]
        assert env["CHATX_ADMIN_TOKEN"] == (cm.config.get("web_admin") or {}).get("auth_token"), "令牌走环境变量"
        assert str(wx_env["config_dir"]) in d["log_path"], "驱动日志落在后端数据目录 wechat_pc/ 下"
        assert client.post("/api/setup/wechat_pc/copilot/start").json()["action"] == "already_running"
        st = client.get("/api/setup/wechat_pc/copilot/status").json()
        assert st["state"] == "starting" and st["managed"] is True and st["pid"] == 5150
        r = client.post("/api/setup/wechat_pc/copilot/restart").json()
        assert r["ok"] and r["action"] == "start" and len(spawned) == 2 and spawned[0]._rc == 1
        r = client.post("/api/setup/wechat_pc/copilot/stop").json()
        assert r["ok"] and r["action"] == "stop" and r["state"] == "offline" and spawned[1]._rc == 1
        # 自启开关落 overlay 并即时生效
        r = client.post("/api/setup/wechat_pc/autostart", json={"enabled": True}).json()
        assert r["ok"] and r["autostart"] is True and sup.autostart is True
        assert ((cm.config.get("platform_login") or {}).get("wechat_pc") or {}).get("autostart") is True
        assert client.get("/api/setup/wechat_pc/policy").json()["autostart"] is True
        r = client.post("/api/setup/wechat_pc/autostart", json={"enabled": False}).json()
        assert r["autostart"] is False and sup.autostart is False
        # env 带上 supervisor 摘要
        e = client.get("/api/setup/wechat_pc/env").json()
        assert e["supervisor"]["state"] == "offline" and e["supervisor"]["autostart"] is False
        assert e["accounts"] == [sup.account_id] and "main_windows" in e

        # 多账号（2026-09-20 P1）：加一个 B 号 → 池里两个 supervisor，各绑各的 pid，老端点不带 account_id 仍是主账号
        import src.integrations.wechat_pc.win32_windows as _ww

        class _W:
            def __init__(self, hwnd, pid, title):
                self.hwnd, self.pid, self.title, self.width, self.height = hwnd, pid, title, 1000, 700

        monkeypatch.setattr(_ww, "find_wechat_main_windows", lambda: [_W(11, 101, "微信"), _W(22, 202, "微信")])
        w = client.get("/api/setup/wechat_pc/windows").json()
        assert w["ok"] and [x["pid"] for x in w["windows"]] == [101, 202] and all(not x["bound_account_id"] for x in w["windows"])
        r = client.post("/api/setup/wechat_pc/accounts", json={"account_id": "wx-b", "label": "B 号", "window_pid": 202}).json()
        assert r["ok"] and r["added"] == ["wx-b"] and r["account"]["account_id"] == "wx-b" and r["account"]["binding"]["window_pid"] == 202
        pool = app.state.wechat_pc_supervisors
        assert pool.account_ids == [sup.account_id, "wx-b"] and app.state.wechat_pc_supervisor is sup, "主账号单例不变"
        saved_accounts = ((cm.config.get("platform_login") or {}).get("wechat_pc") or {}).get("accounts")
        assert saved_accounts and saved_accounts[0]["account_id"] == "wx-b" and saved_accounts[0]["window_pid"] == 202
        supb = pool.get("wx-b")
        supb.driver_ready_provider = sup.driver_ready_provider
        supb._assign_job = lambda pid: None
        supb._kill_tree = lambda proc: proc.kill()
        r = client.post("/api/setup/wechat_pc/copilot/start?account_id=wx-b").json()
        assert r["ok"] and r["action"] == "start" and "--pid" in spawned[-1].cmd and spawned[-1].cmd[spawned[-1].cmd.index("--pid") + 1] == "202"
        assert spawned[-1].cmd[spawned[-1].cmd.index("--account-id") + 1] == "wx-b"
        assert "copilot.wx-b.log" in r["log_path"]
        assert client.get("/api/setup/wechat_pc/copilot/status").json()["managed"] is False, "不带 account_id = 主账号，没在跑"
        assert client.get("/api/setup/wechat_pc/copilot/status?account_id=wx-b").json()["managed"] is True
        assert client.get("/api/setup/wechat_pc/copilot/status?account_id=nope").status_code == 404
        acc = client.get("/api/setup/wechat_pc/accounts").json()
        assert acc["primary_account_id"] == sup.account_id and [a["account_id"] for a in acc["accounts"]] == [sup.account_id, "wx-b"]
        w = client.get("/api/setup/wechat_pc/windows").json()
        assert [x["bound_account_id"] for x in w["windows"]] == ["", "wx-b"]
        # 同一个窗口不能绑给两个账号；非法 id 拒绝
        assert client.post("/api/setup/wechat_pc/accounts", json={"window_pid": 202}).status_code == 400
        assert client.post("/api/setup/wechat_pc/accounts", json={"account_id": "a b"}).status_code == 400
        # 改 B 的绑定，进程在跑 → 自动重拉一次（新 pid 进命令行）
        n = len(spawned)
        r = client.post("/api/setup/wechat_pc/accounts", json={"account_id": "wx-b", "window_pid": 101}).json()
        assert r["ok"] and r["updated"] == ["wx-b"] and r["restarted"] is True and len(spawned) == n + 1
        assert spawned[-1].cmd[spawned[-1].cmd.index("--pid") + 1] == "101"
        # B 号自启开关落到 accounts[]，不碰主账号顶层
        r = client.post("/api/setup/wechat_pc/autostart?account_id=wx-b", json={"enabled": True}).json()
        assert r["ok"] and supb.autostart is True and sup.autostart is False
        blk = (cm.config.get("platform_login") or {}).get("wechat_pc") or {}
        assert blk.get("autostart") is False and blk["accounts"][0]["autostart"] is True
        # 删 B → 进程停、池里没了；主账号不能删
        assert client.delete(f"/api/setup/wechat_pc/accounts/{sup.account_id}").status_code == 400
        r = client.delete("/api/setup/wechat_pc/accounts/wx-b").json()
        assert r["ok"] and r["removed"] == ["wx-b"] and spawned[-1]._rc == 1 and pool.account_ids == [sup.account_id]
        assert client.get("/api/setup/wechat_pc/copilot/status?account_id=wx-b").status_code == 404
    finally:
        try:
            asyncio.run(app.state.wechat_pc_supervisors.shutdown())
        except Exception:
            pass

    # 两张引导页 + 未知平台 404
    r = client.get("/workspace/connect/wechat_kf")
    assert r.status_code == 200 and 'id="cg-rail"' in r.text and 'data-plat="wechat_kf"' in r.text and 'data-supervisor="1"' in r.text
    r = client.get("/workspace/connect/wechat_pc")
    assert r.status_code == 200 and 'data-plat="wechat_pc"' in r.text
    assert "data-faq='[{" in r.text and '"code":' in r.text, "教程 FAQ（症状/原因/处理）随页下发（tojson 转义为 \\uXXXX）"
    assert client.get("/workspace/connect/douyin").status_code == 404


# ── 实施97 P1 · 企业微信成员扫码登录智聊 ──────────────────────────────────────

def test_wecom_member_sso_login(wx_env):
    """登录页按钮 → /login/wecom 302 到企微官方登录页（签名 state 存会话）→ 回调校验 state → 换 userid →
    首次自动开户（坐席）+ 绑定 → 建会话进工作台；二次登录复用同一用户；state 篡改/过期、非成员、白名单外都拒。"""
    from fastapi.testclient import TestClient
    from urllib.parse import parse_qs, urlparse
    from src.utils.web_user_store import WebUserStore
    client, cm, app = wx_env["client"], wx_env["cm"], wx_env["app"]
    # 未启用：登录页没有按钮，/login/wecom 回登录页并给人话
    anon = TestClient(app)
    assert "/login/wecom" not in anon.get("/login").text
    r = anon.get("/login/wecom", follow_redirects=False)
    assert r.status_code == 200 and "企业微信登录未启用" in r.text
    # 启用（复用微信客服同一自建应用的凭证；可信域名给公网 https）
    assert cm.save_overlay_patch({"wecom_login": {"enabled": True, "agentid": "1000002",
                                                  "redirect_base": "https://chatx.example.com"}})
    st = anon.get("/api/auth/wecom/status").json()
    assert st["enabled"] and st["ready"] and st["redirect_uri"] == "https://chatx.example.com/login/wecom/callback"
    assert "/login/wecom" in anon.get("/login").text and "企业微信扫码登录" in anon.get("/login").text
    r = anon.get("/login/wecom", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    u = urlparse(loc)
    assert u.netloc == "login.work.weixin.qq.com"
    q = parse_qs(u.query)
    assert q["login_type"] == ["CorpApp"] and q["appid"] == [_CORP] and q["agentid"] == ["1000002"]
    assert q["redirect_uri"] == ["https://chatx.example.com/login/wecom/callback"]
    state = q["state"][0]
    # 篡改 state → 拒；没有 code → 拒
    r = anon.get(f"/login/wecom/callback?code=good-code&state={state}x", follow_redirects=False)
    assert r.status_code == 200 and "已过期或被改动" in r.text
    r = anon.get(f"/login/wecom/callback?state={state}", follow_redirects=False)
    assert r.status_code == 200 and "未返回授权码" in r.text
    # 正常回调：首次登录自动开户 wecom_zhangsan（坐席）→ 303 /workspace，会话可用
    r = anon.get("/login/wecom", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = anon.get(f"/login/wecom/callback?code=good-code&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/workspace", r.text[:200]
    me = anon.get("/api/workspace/me").json()
    assert me.get("role") == "agent" and me.get("display_name") == "zhangsan", me
    ustore = WebUserStore(wx_env["config_dir"] / "web_users.db")
    u = ustore.get_user("wecom_zhangsan")
    assert u and u["role"] == "agent" and u["display_name"] == "zhangsan" and int(u["enabled"]) == 1
    # 同一 state 不能重放
    r = anon.get(f"/login/wecom/callback?code=good-code&state={state}", follow_redirects=False)
    assert r.status_code == 200 and "已过期或被改动" in r.text
    # 二次登录：复用同一本地用户（绑定表），不重复开户
    anon2 = TestClient(app)
    r = anon2.get("/login/wecom", follow_redirects=False)
    state2 = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = anon2.get(f"/login/wecom/callback?code=good-code&state={state2}", follow_redirects=False)
    assert r.status_code == 303 and anon2.get("/api/workspace/me").json().get("display_name") == "zhangsan"
    assert len([x for x in ustore.list_users() if str(x.get("username", "")).startswith("wecom_")]) == 1, "不重复开户"
    bindings = json.loads((wx_env["config_dir"] / "wecom_bindings.json").read_text(encoding="utf-8"))
    assert bindings == {"zhangsan": "wecom_zhangsan"}
    # 非企业成员（只回 openid）→ 拒；换码失败 → 拒
    for code, msg in (("outsider", "换取成员身份失败"), ("bad", "换取成员身份失败")):
        c = TestClient(app)
        r = c.get("/login/wecom", follow_redirects=False)
        s3 = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = c.get(f"/login/wecom/callback?code={code}&state={s3}", follow_redirects=False)
        assert r.status_code == 200 and msg in r.text
    # 企微「可信域名」归属验证文件：主管贴文件名+内容 → 域名根路径可访问（配置的公网域名下）
    r = client.post("/api/auth/wecom/verify-file", json={"filename": "WW_verify_AbC123.txt", "content": "AbC123"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["url"] == "https://chatx.example.com/WW_verify_AbC123.txt"
    assert client.get("/WW_verify_AbC123.txt").text == "AbC123"
    assert client.get("/WW_verify_nope.txt").status_code == 404
    assert client.post("/api/auth/wecom/verify-file", json={"filename": "../evil.txt", "content": "x"}).status_code == 400
    assert anon.post("/api/auth/wecom/verify-file", json={"filename": "WW_verify_zz9.txt", "content": "x"}).status_code in (401, 403), "坐席以外不可写"
    # 白名单：不在名单里的成员拒
    assert cm.save_overlay_patch({"wecom_login": {"allowed_userids": ["lisi"]}})
    c = TestClient(app)
    r = c.get("/login/wecom", follow_redirects=False)
    s4 = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get(f"/login/wecom/callback?code=good-code&state={s4}", follow_redirects=False)
    assert r.status_code == 200 and "未被允许登录" in r.text


# ── 实施97 · 官网中继：NAT 后的实例经中继收企微回调、成员登录回跳 302 直回 ─────────

def test_relay_carries_wecom_callbacks_into_nat_instance(wx_env, tmp_path):
    """真中继（relay/app.py）+ 真 app：① 企微回调 POST 打中继公网前缀 → 经 WebSocket 到设备端 → 本机 /wechat/kf/callback
    真实解密 → worker.kick；② 成员登录 redirect_uri 自动落中继前缀，state 签着浏览器来源，中继 302 把浏览器直接送回本实例，
    本实例照常校验 state 并建会话。"""
    import importlib
    import socket
    import threading
    import uvicorn
    from fastapi.testclient import TestClient
    from urllib.parse import parse_qs, urlparse
    from src.integrations.relay_client import RelayClient
    client, cm, fake, app = wx_env["client"], wx_env["cm"], wx_env["fake"], wx_env["app"]
    # 起中继
    relay_dir = Path(__file__).resolve().parents[3] / "relay"
    import os as _os
    _os.environ["RELAY_STATE"] = str(tmp_path / "devices.json")
    _os.environ["RELAY_VERIFY_DIR"] = str(tmp_path / "verify")
    _os.environ["RELAY_FORWARD_TIMEOUT"] = "5.0"
    import sys as _sys
    _sys.path.insert(0, str(relay_dir))
    relay_app = importlib.reload(importlib.import_module("app"))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(relay_app.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    # 设备端：fetch 桥到本 app（浏览器来源用私网地址，中继才肯 302 回来）
    local = TestClient(app, base_url="http://127.0.0.1:18899")

    async def fetch(method, url, headers, body):
        u = urlparse(url)
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, lambda: local.request(method, u.path + (f"?{u.query}" if u.query else ""),
                                                                   headers=headers, content=body))
        return r.status_code, dict(r.headers), r.content

    rc = RelayClient(relay_url=f"ws://127.0.0.1:{port}", device_id="dev-e2e-01", secret="e" * 32,
                     local_base="http://127.0.0.1:18899", fetch=fetch, ping_sec=5)
    app.state.relay_client = rc
    rth = threading.Thread(target=lambda: asyncio.run(rc.run_forever()), daemon=True); rth.start()
    for _ in range(100):
        if rc.connected:
            break
        time.sleep(0.05)
    assert rc.connected, rc.last_error
    public = rc.public_base
    try:
        # ① 探活经中继
        r = httpx.get(public + "/api/desktop/ping", timeout=10)
        assert r.status_code == 200 and r.json()["app"] == "chengjie"
        # ② 企微回调经中继：真实加解密，worker 被 kick（带 token 的 sync）
        from src.integrations.account_orchestrator import get_orchestrator
        from src.integrations.account_registry import get_account_registry
        orch = get_orchestrator()
        if not orch.owns("wechat_kf", _KFID):
            row = get_account_registry().get("wechat_kf", _KFID) or get_account_registry().upsert(
                "wechat_kf", _KFID, mode="official", status="online")
            asyncio.run(orch.start_account(row))
        crypto = WK.KfCallbackCrypto(_TOKEN, _AES, _CORP)
        inner = (f"<xml><ToUserName><![CDATA[{_CORP}]]></ToUserName><CreateTime>1</CreateTime>"
                 "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[kf_msg_or_event]]></Event>"
                 f"<Token><![CDATA[RELAYTOKEN]]></Token><OpenKfId><![CDATA[{_KFID}]]></OpenKfId></xml>").encode()
        enc = crypto.encrypt(inner)
        body = f"<xml><ToUserName><![CDATA[{_CORP}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        r = httpx.post(public + "/wechat/kf/callback", params={"msg_signature": crypto.signature("9", "8", enc),
                                                                "timestamp": "9", "nonce": "8"}, content=body.encode(), timeout=10)
        assert r.status_code == 200 and r.text == "success", r.text
        echo = crypto.encrypt(b"relay-echo-1")
        r = httpx.get(public + "/wechat/kf/callback", params={"msg_signature": crypto.signature("1", "2", echo),
                                                               "timestamp": "1", "nonce": "2", "echostr": echo}, timeout=10)
        assert r.status_code == 200 and r.text == "relay-echo-1", "GET 验签 echostr 同步往返"
        # ③ 成员登录：redirect_uri 自动落中继前缀；state 带签名来源；中继 302 直回本实例
        assert cm.save_overlay_patch({"wecom_login": {"enabled": True, "agentid": "1000002", "redirect_base": "",
                                                      "allowed_userids": []}})
        st = local.get("/api/auth/wecom/status").json()
        assert st["redirect_source"] == "relay" and st["redirect_uri"] == public + "/login/wecom/callback"
        assert st["relay"]["connected"] is True
        r = local.get("/login/wecom", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["redirect_uri"] == [public + "/login/wecom/callback"]
        state = q["state"][0]
        assert state.count(".") == 3, "state 第 4 段＝签名过的浏览器来源"
        # 企微把浏览器带到中继回调 → 中继 302 回本实例（私网来源）
        r = httpx.get(public + f"/login/wecom/callback?code=good-code&state={state}", follow_redirects=False, timeout=10)
        assert r.status_code == 302 and r.headers["location"].startswith("http://127.0.0.1:18899/login/wecom/callback?code=good-code&state=")
        loc = urlparse(r.headers["location"])
        r = local.get(loc.path + "?" + loc.query, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/workspace", r.text[:200]
        assert local.get("/api/workspace/me").json().get("display_name") == "zhangsan"
    finally:
        rc.stop()
        app.state.relay_client = None
        server.should_exit = True
        th.join(timeout=5)


# ── 能力对齐矩阵 ────────────────────────────────────────────────────────────

def test_parity_matrix_against_existing_platforms():
    """微信客服 vs 现有平台：逐项能力钉成回归网（能力面 = worker 方法 + 管线接线 + 元数据）。"""
    from src.integrations import platform_capabilities as PC
    from src.integrations.wechat_kf_worker import WeChatKfWorker
    from src.integrations.platform_login import DEFAULT_PLATFORM_MODES, SUPPORTED_PLATFORMS
    from src.integrations.platform_readiness import _IMPLEMENTED_MODES
    from src.utils.channel_setup import get_channel
    from src.assistant.product_facts import CHANNEL_PLATFORM_KEYS, SUPPORTED_CHANNELS
    from src.inbox.kf_window_guard import QUOTA_WINDOW_PLATFORMS
    from src.compliance import FORCED_COMPLIANCE_PLATFORMS

    w = WeChatKfWorker({"account_id": _KFID, "meta": {}}, {"wechat_kf": {"corpid": "c", "secret": "s"}})
    caps = PC.worker_capabilities(w)
    # 与 Zalo/Instagram 个人号同档：发文本+媒体 Y；已读/typing 为协议层硬限制（微信客服 API 没有）
    assert caps == {"send_text": True, "send_media": True, "mark_read": False, "typing": False}
    matrix = PC.capability_matrix({})
    zalo = matrix["zalo:web"]["caps"] if matrix.get("zalo:web", {}).get("available") else None
    if zalo:
        assert caps == zalo, "微信客服的发送能力面应与 Zalo 个人号一档"
    # 元数据五处齐全（渠道向导 / 登录模式 / 就绪度 / 事实卡 / 平台栏由 test_inbox_platform_rail 钉）
    assert get_channel("wechat_kf") is not None and get_channel("wechat_kf").official_platform == "wechat_kf"
    assert "wechat_kf" in SUPPORTED_PLATFORMS and DEFAULT_PLATFORM_MODES["wechat_kf"]["modes"] == ["official"]
    assert ("wechat_kf", "official") in _IMPLEMENTED_MODES
    assert CHANNEL_PLATFORM_KEYS.get("微信客服（企业微信）") == "wechat_kf" and "微信客服（企业微信）" in SUPPORTED_CHANNELS
    # 微信客服独有的两条硬规则已产品化：配额守卫 + 合规恒开（其它平台不受影响）
    assert QUOTA_WINDOW_PLATFORMS == {"wechat_kf"} and FORCED_COMPLIANCE_PLATFORMS == {"wechat_kf"}
    # 会话状态动作（转企微人工 / 结束会话）——其它平台没有的额外能力面
    assert hasattr(w, "transfer_to_human") and hasattr(w, "close_session") and hasattr(w, "session_state")
