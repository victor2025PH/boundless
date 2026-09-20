"""成本页路由门禁（2026-09-08 成本对账 P1/P2）：汇总形状、CSV 导入写真值+对账、手填、充值、立即对账。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai import cost_ledger as cl  # noqa: E402
from src.ai.cost_ledger import CostLedger, day_of  # noqa: E402

P = "siliconflow"


def _ts(day: str, hour: int = 12) -> float:
    return time.mktime(time.strptime(day, "%Y-%m-%d")) + hour * 3600


def _prev(n: int) -> str:
    return day_of(time.time() - n * 86400)


@pytest.fixture
def ledger(tmp_path):
    cl.reset_cost_ledger()
    led = CostLedger(str(tmp_path / "c.db"))
    cl.configure_cost_ledger(ledger=led)
    yield led
    cl.reset_cost_ledger()


def _spend(led, day, cost, purpose="customer_reply", provider=P):
    led.record_usage({"ts": _ts(day), "provider": provider, "model": "m", "purpose": purpose,
                      "prompt_tokens": 1000, "completion_tokens": 10, "cost": cost})


def _app(cfg=None):
    from fastapi import FastAPI
    from src.web.routes.cost_routes import register_cost_routes

    class _CM:
        config = cfg or {"ai": {"base_url": "https://api.siliconflow.cn/v1",
                               "pricing": {"m": {"prompt": 1, "completion": 1}},
                               "cost_guard": {"daily_budget_cny": 10}}}

    app = FastAPI()
    register_cost_routes(app, page_auth=lambda: None, api_auth=lambda: None,
                         templates=None, config_manager=_CM())
    return app


@pytest.mark.asyncio
async def test_summary_shape(ledger):
    from httpx import ASGITransport, AsyncClient
    today, yest = _prev(0), _prev(1)
    _spend(ledger, today, 0.6, purpose="customer_reply")
    _spend(ledger, today, 1.2, purpose="drill")
    _spend(ledger, yest, 2.0)
    ledger.set_truth(yest, P, 2.1, balance=50.0, source="manual")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/api/cost/summary?days=14")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["provider"] == P and d["pricing_configured"]
        assert abs(d["today"]["cost"] - 1.8) < 1e-6
        assert [p["purpose"] for p in d["today"]["purposes"]] == ["drill", "customer_reply"]
        assert d["today"]["purposes"][0]["label"] == "夜间演练"
        assert d["yesterday"]["truth"] == 2.1 and d["budget"]["daily"] == 10.0
        assert len(d["series"]) == 14 and d["series"][-1]["day"] == today
        assert d["balance"] == pytest.approx(50.0 - 1.8) and d["balance_basis"] == "manual"
        assert "prompt_cache" in d and set(d["prompt_cache"]) >= {
            "calls", "prompt_tokens", "cache_hit_tokens", "hit_ratio", "saved_cny"}


@pytest.mark.asyncio
async def test_summary_prompt_cache_from_trace(ledger, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from src.ai import prompt_trace
    from src.web.routes import cost_routes as cr

    prompt_trace.reset()
    prompt_trace.record(
        messages=[{"role": "user", "content": "hi"}],
        model="deepseek-flash",
        usage={"prompt_tokens": 2000, "completion_tokens": 10,
               "prompt_cache_hit_tokens": 1500, "prompt_cache_miss_tokens": 500},
    )
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        d = (await c.get("/api/cost/summary")).json()
    pc = d["prompt_cache"]
    assert pc["calls"] == 1 and pc["cache_hit_tokens"] == 1500 and pc["hit_ratio"] == 0.75
    assert pc["saved_cny"] == round(1500 * (cr._DS_MISS_CNY_PER_M - cr._DS_HIT_CNY_PER_M) / 1_000_000, 4)
    prompt_trace.reset()


@pytest.mark.asyncio
async def test_import_bill_writes_truth_skips_today_and_recons(ledger):
    from httpx import ASGITransport, AsyncClient
    today, yest, d2 = _prev(0), _prev(1), _prev(2)
    _spend(ledger, yest, 1.0)
    _spend(ledger, d2, 5.0)
    csv = ("计费周期,费用流水ID,费用发生时间,计费项,计费金额\n"
           f"x,1,{yest} 10:00,deepseek-ai/deepseek-v3.2.online.input-tokens,0.9\n"
           f"x,2,{yest} 11:00,deepseek-ai/deepseek-v3.2.online.output-tokens,0.15\n"
           f"x,3,{d2} 09:00,deepseek-ai/deepseek-v3.2.online.input-tokens,2.0\n"
           f"x,4,{today} 09:00,deepseek-ai/deepseek-v3.2.online.input-tokens,0.3\n")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/api/cost/import-bill",
                         files={"file": ("bill.csv", csv.encode("utf-8-sig"), "text/csv")})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["rows"] == 4 and set(j["days_written"]) == {yest, d2}
        assert j["skipped_today"] == [today]
        by_day = {x["day"]: x for x in j["recon"]}
        assert by_day[yest]["verdict"] == "ok"          # 1.0 vs 1.05
        assert by_day[d2]["verdict"] == "mismatch"      # 5.0 vs 2.0
        assert ledger.get_truth(yest, P)["amount"] == pytest.approx(1.05)
        assert ledger.get_truth(today, P) is None
        # JSON text 形态 + 坏文件报人话
        r2 = await c.post("/api/cost/import-bill", json={"text": "a,b\n1,2\n"})
        assert r2.status_code == 400 and "表头" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_manual_truth_balance_recharge_and_run(ledger):
    from httpx import ASGITransport, AsyncClient
    yest = _prev(1)
    _spend(ledger, yest, 1.0)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.post("/api/cost/truth", json={"day": yest, "amount": 1.05, "balance": 96.5})
        assert r.status_code == 200 and r.json()["recon"][0]["verdict"] == "ok"
        assert ledger.get_truth(yest, P)["balance"] == 96.5
        # 只填余额：金额沿用已有真值
        r = await c.post("/api/cost/truth", json={"day": yest, "balance": 90})
        assert r.status_code == 200 and ledger.get_truth(yest, P)["amount"] == pytest.approx(1.05)
        r = await c.post("/api/cost/truth", json={"day": yest})
        assert r.status_code == 400
        r = await c.post("/api/cost/truth", json={"day": yest, "amount": "abc"})
        assert r.status_code == 400
        r = await c.post("/api/cost/recharge", json={"amount": 300, "day": _prev(5), "note": "上次充值"})
        assert r.status_code == 200 and ledger.recharges(P)[0]["amount"] == 300
        assert (await c.post("/api/cost/recharge", json={"amount": -1})).status_code == 400
        r = await c.post("/api/cost/recon/run", json={"day": yest})
        assert r.status_code == 200 and r.json()["results"][0]["day"] == yest


@pytest.mark.asyncio
async def test_summary_without_ledger_reports_unavailable():
    from httpx import ASGITransport, AsyncClient
    cl.reset_cost_ledger()
    cl.configure_cost_ledger(path=":memory:")   # 每次 get 建新内存库 → 空但可用
    try:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
            d = (await c.get("/api/cost/summary")).json()
            assert d["available"] and d["today"]["cost"] == 0 and d["last_recon"] is None
    finally:
        cl.reset_cost_ledger()
