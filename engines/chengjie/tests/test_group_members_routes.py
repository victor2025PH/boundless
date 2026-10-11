"""group_members_routes 路由层测试（开关门控 / 参数校验 / 无在线号 503 / CSV 导出）。

不依赖真实 Telegram：``_get_tg_pyro_for_account`` 在测试 app 里取不到 client → 提取端点
如实 503（而非静默假成功）。读端点用 :memory: store 直接验数据形状。
"""
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.companion.group_members_store import (
    configure_group_members_store,
    get_group_members_store,
    reset_group_members_store,
)
from src.web.routes.group_members_routes import register_group_members_routes


class _CM:
    def __init__(self, enabled=True):
        self.config = {"companion": {"group_members": {
            "enabled": enabled, "daily_cap_per_account": 50, "scan_limit": 100,
            # 路由测试不看墙上时钟：时段全开
            "outreach_hours": [0, 24]}}}


class _Registry:
    """假注册表：accA 是养熟的老号，accNew 刚注册。"""
    def __init__(self):
        self.rows = {"accA": {"created_at": time.time() - 90 * 86400},
                     "accNew": {"created_at": time.time()}}

    def get(self, platform, account_id):
        return self.rows.get(account_id)


@pytest.fixture()
def client_cm():
    reset_group_members_store()
    st = configure_group_members_store(":memory:")
    st.record_members([{
        "group_id": "-100", "user_id": "1", "username": "alice", "first_name": "A",
        "spoke": True, "is_admin": False, "group_title": "G",
        "source_account_id": "accA", "extracted_at": time.time(),
    }])
    cm = _CM(enabled=True)
    app = FastAPI()
    app.state.account_registry = _Registry()

    async def _auth():
        return True

    register_group_members_routes(app, auth_dep=_auth, audit_store=None, config_manager=cm)
    with TestClient(app) as c:
        yield c, cm
    reset_group_members_store()


def test_disabled_gate_403(client_cm):
    c, cm = client_cm
    cm.config["companion"]["group_members"]["enabled"] = False
    assert c.get("/api/tg-members/quota?account_id=accA").status_code == 403
    assert c.get("/api/tg-members/members?group_id=-100").status_code == 403


def test_visibility_checkbox_opens_the_gate(client_cm):
    """开发者页勾上群成员提取即可用，不必再写 companion.group_members.enabled。"""
    c, cm = client_cm
    cm.config["companion"]["group_members"]["enabled"] = False
    cm.config["ui_visibility"] = {"group_extract": True}
    assert c.get("/api/tg-members/quota?account_id=accA").status_code == 200


def test_quota_shape(client_cm):
    c, _ = client_cm
    r = c.get("/api/tg-members/quota?account_id=accA")
    assert r.status_code == 200
    d = r.json()
    assert d["cap"] == 50 and d["used_today"] == 1 and d["remaining"] == 49


def test_members_list_and_missing_group(client_cm):
    c, _ = client_cm
    assert c.get("/api/tg-members/members").status_code == 400  # 缺 group_id
    r = c.get("/api/tg-members/members?group_id=-100")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1 and d["spoke"] == 1 and len(d["items"]) == 1


def test_groups_and_jobs_list(client_cm):
    c, _ = client_cm
    assert c.get("/api/tg-members/groups").json()["groups"][0]["group_id"] == "-100"
    assert c.get("/api/tg-members/jobs").json()["jobs"] == []


def test_create_job_validation(client_cm):
    c, _ = client_cm
    assert c.post("/api/tg-members/jobs", json={}).status_code == 400          # 缺参
    assert c.post("/api/tg-members/jobs",
                  json={"account_id": "accA", "group": "-100",
                        "filter": "bogus"}).status_code == 400                  # 过滤器非法


def test_create_job_no_live_client_503(client_cm):
    c, _ = client_cm
    # 测试 app 无 orchestrator / app.state.telegram_client → 取不到活体 client → 503
    r = c.post("/api/tg-members/jobs",
               json={"account_id": "accA", "group": "-100", "filter": "spoke_no_admin"})
    assert r.status_code == 503


def test_outreach_queue_is_separate_from_extract_cap(client_cm):
    """提取配额 50 不能变成开口配额；200 会被夹到 10。没确认不能发。"""
    c, cm = client_cm
    st = get_group_members_store()
    st.record_members([{
        "group_id": "-100", "user_id": "9", "username": "neo", "first_name": "Neo",
        "spoke": True, "is_admin": False, "group_title": "G",
        "source_account_id": "accA", "hash_account_id": "accA",
        "access_hash": "12345", "last_spoke_ts": time.time(), "score": 90,
    }])
    prev = c.get("/api/tg-members/outreach/preview?account_id=accA")
    assert prev.status_code == 200
    body = prev.json()
    assert body["cap"] == 8
    assert body["remaining"] == 8
    assert len(body["candidates"]) == 1
    assert "12345" not in prev.text
    listed = c.get("/api/tg-members/members?group_id=-100")
    assert "12345" not in listed.text

    queued = c.post("/api/tg-members/outreach/queue", json={"account_id": "accA"})
    assert queued.status_code == 200
    assert queued.json()["queued_now"] == 1

    denied = c.post("/api/tg-members/outreach/release", json={
        "account_id": "accA", "group_id": "-100", "user_id": "9",
        "text": "你好", "confirm": False,
    })
    assert denied.status_code == 400

    pitched = c.post("/api/tg-members/outreach/release", json={
        "account_id": "accA", "group_id": "-100", "user_id": "9",
        "text": "加我微信 https://t.me/x", "confirm": True,
    })
    assert pitched.status_code == 400

    no_client = c.post("/api/tg-members/outreach/release", json={
        "account_id": "accA", "group_id": "-100", "user_id": "9",
        "text": "在群里看到你发言，打个招呼。", "confirm": True,
    })
    assert no_client.status_code == 503
    row = st.get_member("-100", "9")
    assert row["outreach_state"] == "queued"

    cm.config["companion"]["group_members"]["outreach_daily_cap"] = 200
    assert c.get("/api/tg-members/outreach/preview?account_id=accA").json()["cap"] == 10

    assert c.post("/api/tg-members/outreach/stop", json={"account_id": "accA"}).status_code == 200
    held = c.post("/api/tg-members/outreach/queue", json={"account_id": "accA"})
    assert held.status_code == 409
    assert c.post("/api/tg-members/outreach/resume", json={"account_id": "accA"}).status_code == 200


def _seed_queue(c, st, uids=("9", "10")):
    st.record_members([{
        "group_id": "-100", "user_id": u, "username": "n%s" % u, "first_name": "N%s" % u,
        "spoke": True, "is_admin": False, "group_title": "G群",
        "source_account_id": "accA", "hash_account_id": "accA",
        "access_hash": "12345", "last_spoke_ts": time.time(), "score": 90,
        "last_msg_text": "有人用过这个吗", "lang_code": "zh",
    } for u in uids])
    assert c.post("/api/tg-members/outreach/queue", json={"account_id": "accA"}).json()["queued_now"] == len(uids)


def test_outreach_compose_approve_skip_mode_flow(client_cm):
    """拟稿（无 LLM → 模板）→ 坐席改稿批准 → 跳过 → 切模式 → 预览带人设/目标/汇总。"""
    c, cm = client_cm
    st = get_group_members_store()
    _seed_queue(c, st)
    composed = c.post("/api/tg-members/outreach/compose", json={"account_id": "accA"})
    assert composed.status_code == 200
    d = composed.json()
    assert d["composed"] == 2 and d["template"] == 2 and d["ai"] == 0
    assert "12345" not in composed.text
    row9 = st.get_member("-100", "9")
    assert row9["opener_text"] and row9["opener_source"] == "template"
    assert "N9" in row9["opener_text"] and "G群" in row9["opener_text"]
    # 再拟一次：已有文案不动
    assert c.post("/api/tg-members/outreach/compose",
                  json={"account_id": "accA"}).json()["skipped"] == 2

    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert pv["mode"] == "manual" and "persona" in pv and "goal" in pv
    assert pv["settings"]["hours"] == [0, 24] and pv["summary"]["queued"] == 2
    assert pv["queue"][0]["opener_text"] and pv["queue"][0]["last_msg_text"] == "有人用过这个吗"

    # 带链接的手改稿被拒；干净的落为 manual 并批准
    bad = c.post("/api/tg-members/outreach/approve", json={
        "account_id": "accA", "items": [{"group_id": "-100", "user_id": "9", "text": "看 https://x"}]})
    assert bad.status_code == 400
    ok = c.post("/api/tg-members/outreach/approve", json={
        "account_id": "accA", "items": [{"group_id": "-100", "user_id": "9", "text": "N9，群里见过，聊两句？"}]})
    assert ok.status_code == 200 and ok.json()["approved"] == 1
    row9 = st.get_member("-100", "9")
    assert row9["outreach_state"] == "approved" and row9["opener_source"] == "manual"
    # 已批准的人在预览队列最前
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert pv["queue"][0]["user_id"] == "9" and pv["summary"]["approved"] == 1

    assert c.post("/api/tg-members/outreach/skip",
                  json={"account_id": "accA", "group_id": "-100", "user_id": "10"}).status_code == 200
    assert st.get_member("-100", "10")["outreach_state"] == "skipped"
    assert c.post("/api/tg-members/outreach/skip",
                  json={"account_id": "accA", "group_id": "-100", "user_id": "10"}).status_code == 409

    assert c.post("/api/tg-members/outreach/mode",
                  json={"account_id": "accA", "mode": "bogus"}).status_code == 400
    assert c.post("/api/tg-members/outreach/mode",
                  json={"account_id": "accA", "mode": "approve"}).json()["mode"] == "approve"
    assert c.get("/api/tg-members/outreach/preview?account_id=accA").json()["mode"] == "approve"

    # 批准态的人也能手动发（无 client → 503，退回原来的 approved，不丢批准）
    r = c.post("/api/tg-members/outreach/release", json={
        "account_id": "accA", "group_id": "-100", "user_id": "9", "confirm": True})
    assert r.status_code == 503
    assert st.get_member("-100", "9")["outreach_state"] == "approved"


def test_outreach_approve_whole_queue_composes_first(client_cm):
    c, _ = client_cm
    st = get_group_members_store()
    _seed_queue(c, st)
    r = c.post("/api/tg-members/outreach/approve", json={"account_id": "accA"})
    assert r.status_code == 200 and r.json()["approved"] == 2
    assert all(st.get_member("-100", u)["opener_text"] for u in ("9", "10"))


def test_outreach_settings_write_overlay_or_503(client_cm):
    c, cm = client_cm
    # _CM 没有 set_overlay_flag → 503，且 preview 标 writable=False
    assert c.get("/api/tg-members/outreach/preview?account_id=accA").json()["settings"]["writable"] is False
    assert c.post("/api/tg-members/outreach/settings", json={"hours": [9, 22]}).status_code == 503

    written = {}

    def _set(path, value):
        written[path] = value
        node = cm.config
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        return True, "ok"

    cm.set_overlay_flag = _set
    assert c.post("/api/tg-members/outreach/settings", json={"hours": [22, 9]}).status_code == 400
    r = c.post("/api/tg-members/outreach/settings", json={"hours": [9, 22], "daily_cap": 200})
    assert r.status_code == 200
    assert r.json()["hours"] == [9, 22] and r.json()["daily_cap"] == 10
    assert written["companion.group_members.outreach_hours"] == [9, 22]
    assert written["companion.group_members.outreach_daily_cap"] == 10


def test_outreach_auto_mode_gate_followup_and_stats(client_cm):
    """新号切 auto 被拒；老号可切；跟进端点校验；stats 形状。"""
    c, cm = client_cm
    st = get_group_members_store()
    _seed_queue(c, st)
    # accNew 刚注册 → 全自动被拒（409 + 原因），预览标出不够格
    r = c.post("/api/tg-members/outreach/mode", json={"account_id": "accNew", "mode": "auto"})
    assert r.status_code == 409 and "14" in r.json()["detail"]
    pv = c.get("/api/tg-members/outreach/preview?account_id=accNew").json()
    assert pv["auto_eligible"]["ok"] is False and pv["auto_eligible"]["reason"] == "age"
    # accA 90 天老号、无风控、无样本 → 可切
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert pv["auto_eligible"]["ok"] is True and pv["followup"]["enabled"] is True
    assert pv["followups_due"] == []
    assert c.post("/api/tg-members/outreach/mode",
                  json={"account_id": "accA", "mode": "auto"}).json()["mode"] == "auto"
    # 把 9 推成三天前发出 → 预览列出待跟进；跟进端点：无确认 400 / 太早 409 / 无 client 503 且坑还回去
    import time as _t
    now = _t.time()
    st.set_opener("-100", "9", "开场", "ai")
    assert st.cas_outreach("-100", "9", expect_states=("queued",), new_state="sent",
                           account_id="accA", now=now - 73 * 3600)
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert [m["user_id"] for m in pv["followups_due"]] == ["9"]
    assert "12345" not in c.get("/api/tg-members/outreach/preview?account_id=accA").text
    body = {"account_id": "accA", "group_id": "-100", "user_id": "9"}
    assert c.post("/api/tg-members/outreach/followup", json=body).status_code == 400
    r = c.post("/api/tg-members/outreach/followup", json=dict(body, confirm=True))
    assert r.status_code == 503
    assert st.get_member("-100", "9")["followup_at"] == 0
    # 10 还在 queued → followup_state 409
    r = c.post("/api/tg-members/outreach/followup",
               json=dict(body, user_id="10", confirm=True, text="再冒个泡"))
    assert r.status_code == 409
    # stats
    s = c.get("/api/tg-members/outreach/stats?account_id=accA&days=7").json()
    assert s["days"] == 7 and s["funnel"]["sent"] == 1 and s["funnel"]["queued"] == 1
    assert {r["key"] for r in s["by_source"]} == {"ai"}
    assert "by_variant" in s and s["reply_latency"]["n"] == 0 and s["funnel"]["inbound_first"] == 0
    assert c.get("/api/tg-members/outreach/stats").status_code == 200
    # 9 回了 → 预览「今日回音」带首回文案 + 会话深链；仍不漏 access_hash
    from src.companion.group_member_outreach import note_inbound_reply
    assert note_inbound_reply({"platform": "telegram", "account_id": "accA", "chat_key": "9",
                               "direction": "in", "text": "在的，啥事", "ts": now}, st) == 1
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA")
    assert "12345" not in pv.text
    rep = pv.json()["replied"]
    assert len(rep) == 1 and rep[0]["reply_text"] == "在的，啥事"
    assert rep[0]["conversation_id"] == "telegram:accA:9" and rep[0]["outreach_state"] == "replied"
    s = c.get("/api/tg-members/outreach/stats?account_id=accA&days=7").json()
    assert s["reply_latency"]["n"] == 1 and s["reply_latency"]["median_sec"] >= 72 * 3600
    assert s["funnel"]["answered"] == 0 and len(s["recent_replies"]) == 1
    assert "access_hash" not in s["recent_replies"][0] and s["recent_replies"][0]["user_id"] == "9"
    # 第二跳：预览 stalled 为空（刚回）；把回话时间推到 3 天前 → 列出没接上；我方回一句 → 消失
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert pv["stalled"] == [] and pv["stalled_after_hours"] == 48
    st._conn.execute("UPDATE tg_group_members SET replied_at=? WHERE user_id='9'", (now - 72 * 3600,))
    st._conn.commit()
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert [m["user_id"] for m in pv["stalled"]] == ["9"] and pv["stalled"][0]["answered_at"] == 0
    from src.companion.group_member_outreach import note_outbound_answer
    assert note_outbound_answer({"platform": "telegram", "account_id": "accA", "chat_key": "9",
                                 "direction": "out", "ts": now}, st) == 1
    pv = c.get("/api/tg-members/outreach/preview?account_id=accA").json()
    assert pv["stalled"] == []
    assert c.get("/api/tg-members/outreach/stats?account_id=accA").json()["funnel"]["answered"] == 1


def test_outreach_new_account_ramps_and_hours_close(client_cm):
    """刚注册的号只给 3 个坑；时段关了排队照常、发送 409。"""
    c, cm = client_cm
    st = get_group_members_store()
    st.record_members([{
        "group_id": "-100", "user_id": str(i), "username": "u%d" % i, "first_name": "U",
        "spoke": True, "is_admin": False, "group_title": "G",
        "source_account_id": "accNew", "hash_account_id": "accNew",
        "access_hash": "4%d" % i, "last_spoke_ts": time.time(), "score": 90,
    } for i in range(1, 7)])
    body = c.get("/api/tg-members/outreach/preview?account_id=accNew").json()
    assert body["effective_cap"] == 3 and body["cap"] == 8 and body["warming_up"] is True
    assert body["age_days"] == 0.0 and len(body["candidates"]) == 3
    assert body["gate"] is None                      # 总发送闸门没开 → 不显示
    assert c.post("/api/tg-members/outreach/queue",
                  json={"account_id": "accNew"}).json()["queued_now"] == 3

    cm.config["companion"]["group_members"]["outreach_hours"] = [0, 1] if time.localtime().tm_hour > 1 else [23, 24]
    pv = c.get("/api/tg-members/outreach/preview?account_id=accNew").json()
    assert pv["hours"]["ok"] is False and len(pv["queue"]) == 3
    uid = pv["queue"][0]["user_id"]
    r = c.post("/api/tg-members/outreach/release", json={
        "account_id": "accNew", "group_id": "-100", "user_id": uid,
        "text": "打个招呼", "confirm": True,
    })
    assert r.status_code == 409
    assert st.get_member("-100", uid)["outreach_state"] == "queued"


def test_outreach_respects_overall_send_gate(client_cm, monkeypatch):
    """总发送闸门拦了（红灯/总额度/急停）→ 开口 409，人留在队列，不翻 sending。"""
    import src.integrations.shared.send_guard as sg

    c, _ = client_cm
    st = get_group_members_store()
    st.record_members([{
        "group_id": "-100", "user_id": "9", "username": "neo", "first_name": "Neo",
        "spoke": True, "is_admin": False, "group_title": "G",
        "source_account_id": "accA", "hash_account_id": "accA",
        "access_hash": "12345", "last_spoke_ts": time.time(), "score": 90,
    }])
    assert c.post("/api/tg-members/outreach/queue", json={"account_id": "accA"}).status_code == 200
    calls = []

    def _blocked(platform, account_id, **kw):
        calls.append((platform, account_id, kw.get("origin")))
        return True, "send_gate:daily_cap"

    monkeypatch.setattr(sg, "send_blocked", _blocked)
    r = c.post("/api/tg-members/outreach/release", json={
        "account_id": "accA", "group_id": "-100", "user_id": "9",
        "text": "打个招呼", "confirm": True,
    })
    assert r.status_code == 409
    assert calls == [("telegram", "accA", "auto")]
    assert st.get_member("-100", "9")["outreach_state"] == "queued"


def test_export_csv(client_cm):
    c, _ = client_cm
    r = c.get("/api/tg-members/members/export?group_id=-100")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    body = r.text
    assert body.splitlines()[0].startswith("user_id,username")
    assert "alice" in body
