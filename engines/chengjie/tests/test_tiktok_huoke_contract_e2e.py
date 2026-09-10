"""TK-3 P2 跨引擎契约：真 chengjie 桥路由 × 真 huoke 客户端代码（探针 / 轮询决策 / 认领回执），零网络。

联调最容易断的地方是两边各自想象的路径 / 字段：这里把 ``engines/huoke/src/app_automation`` 的模块按别名包
装进来（两仓顶层包都叫 ``src``，不能直接 import），用 TestClient 充当 HTTP，把「探针全绿 → 人工发送入队 →
轮询器决定唤醒 → 认领 → 真机发出 → 回执 → 镜像升 sent」整条线走一遍。huoke 树不存在则 skip（不假绿）。"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

import pytest

from src.integrations import protocol_bridge as pb
from src.integrations import tiktok_huoke_bridge as hb
from src.inbox.store import InboxStore

HUOKE_AA = Path(__file__).resolve().parents[2] / "huoke" / "src" / "app_automation"
ACC, DEV, T0 = "acc-probe", "DEV-PROBE", 1_800_000_000.0
CFG = {"tiktok": {"huoke_bridge": {"enabled": True, "dm_daily_cap": 5, "min_gap_sec": 0}}}
HUOKE_CFG = {"reply_engine": "chengjie",
             "chengjie": {"endpoint": "http://chengjie.test", "token": "t", "account_id": ACC, "username": "shop_probe",
                          "timezone": "Asia/Manila", "handback_poll_sec": 60,
                          "device_account_map": {DEV: {"account_id": ACC, "username": "shop_probe", "timezone": "Asia/Manila"}}}}


def _load_huoke():
    if not (HUOKE_AA / "tiktok_chengjie_bridge.py").is_file() or not (HUOKE_AA / "tiktok_chengjie_probe.py").is_file():
        pytest.skip("engines/huoke 不在本 checkout，跨引擎契约跳过")
    pkg_name = "huoke_app_automation_tk3"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(HUOKE_AA)]  # type: ignore[attr-defined]
    sys.modules[pkg_name] = pkg

    def load(mod: str):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{mod}", HUOKE_AA / f"{mod}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        return m

    return load("tiktok_chengjie_bridge"), load("tiktok_chengjie_probe")


async def _noop(_m):
    return None


def test_huoke_client_against_real_chengjie_routes(tmp_path, monkeypatch):
    cj, pr = _load_huoke()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    store = InboxStore(tmp_path / "inbox_contract.db")
    pb.register_inbox_store_getter(lambda: store)
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    st = hb.TikTokHuokeStateStore(":memory:")
    monkeypatch.setattr(hb, "get_state_store", lambda *_a, **_k: st)
    monkeypatch.setattr(pb, "maybe_auto_reply", _noop)
    reg: Dict[str, Any] = {}
    monkeypatch.setattr("src.integrations.account_registry.get_account_registry",
                        lambda: SimpleNamespace(upsert=lambda platform, account_id, **kw: reg.__setitem__(f"{platform}:{account_id}", kw)))
    monkeypatch.setattr("src.web.routes.unified_inbox_aggregate._INBOX_ADAPTERS", [])  # 挂路由会追加适配器，别漏进进程级注册表
    app = FastAPI()
    assert hb.register_tiktok_huoke_routes(app, SimpleNamespace(config=CFG)) is True
    c = TestClient(app)
    wire: List[Tuple[str, str]] = []

    def http(method: str, url: str, headers: Dict[str, str], body: Any) -> Tuple[int, Any]:
        u = urlsplit(url)
        assert u.netloc == "chengjie.test" and headers.get("Authorization") == "Bearer t"
        wire.append((method, u.path))
        r = c.request(method, u.path + (f"?{u.query}" if u.query else ""), json=body, headers=headers)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"error": r.text[:200]}

    try:
        # 1) 探针全绿：status → devices → dm(探针进线) → pending
        rep = pr.run_probe(HUOKE_CFG, http=http, bind=True, roundtrip=True, now=T0)
        assert rep["exit_code"] == 0, rep
        assert [p for _, p in wire] == [hb.STATUS_ROUTE, hb.DEVICES_ROUTE, hb.DM_ROUTE, hb.HANDBACK_PENDING_ROUTE]
        acc = st.account(ACC)
        assert acc["device_id"] == DEV and acc["timezone"] == "Asia/Manila" and reg[f"tiktok:{ACC}"]["mode"] == "personal_rpa"
        probe_chat = f"tiktok:user:{pr.PROBE_PEER}"
        assert st.ctx(ACC, probe_chat)["last_inbound_ts"] > 0
        steps = {s["step"]: s for s in rep["steps"]}
        assert steps["roundtrip:pending"]["ok"] and "queued=0" in steps["roundtrip:pending"]["detail"]

        # 2) 轮询决策：队列空 → 不唤醒；人工回复入队 → 该设备该建任务
        plan = cj.plan_handback_tasks([DEV], {}, cfg=HUOKE_CFG, http=http)
        assert plan["create"] == [] and plan["skipped"] == {DEV: "empty"}
        from src.inbox import send_context
        monkeypatch.setattr(send_context, "is_manual_send", lambda: True, raising=False)
        import asyncio
        adapter = hb.TikTokHuokeAdapter(lambda: CFG)  # 工作台发送真实走的适配器：入队 + 无勾镜像
        ok = asyncio.run(adapter.send(None, ACC, probe_chat, "probe reply from operator"))
        assert ok["ok"] is True and ok["message_id"] == f"hb:{ok['item_id']}", ok
        assert st.item(ok["item_id"])["origin"] == "manual"
        plan = cj.plan_handback_tasks([DEV], {}, cfg=HUOKE_CFG, http=http)
        assert [x["device_id"] for x in plan["create"]] == [DEV] and plan["create"][0]["queued"] == 1

        # 3) 真机任务体：认领 → send_dm → 回执 → chengjie 侧 sent + 线程镜像升 sent
        sent: List[Tuple[str, str]] = []
        r = cj.drain_handback(device_id=DEV, send_dm=lambda who, text: sent.append((who, text)) or True, cfg=HUOKE_CFG, http=http)
        assert r["claimed"] == 1 and r["sent"] == 1 and sent == [(pr.PROBE_PEER, "probe reply from operator")]
        assert st.item(ok["item_id"])["status"] == "sent" and st.pending(DEV)["queued"] == 0
        from src.inbox.normalizer import conv_id
        outs = [m for m in store.list_messages(conv_id("tiktok", ACC, probe_chat), limit=20) if m.get("direction") == "out"]
        assert outs and str(outs[-1].get("status") or "") == "sent"
        # 4) 再来一轮：空队列 → 不建任务（轮询器不会空转唤醒真机）
        assert cj.plan_handback_tasks([DEV], {}, cfg=HUOKE_CFG, http=http)["skipped"] == {DEV: "empty"}
    finally:
        pb.register_inbox_store_getter(None)
        pb.register_inbox_sink(None)
        for k in [k for k in sys.modules if k.startswith("huoke_app_automation_tk3")]:
            sys.modules.pop(k, None)
