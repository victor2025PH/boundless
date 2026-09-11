"""成本计量 + 账本门禁（2026-09-08 成本对账 P0/P1）。

钉住：
- llm_cost 按用途分桶、CNY 计价、sink 每笔同步；未知用途归 unknown；
- 演练号段判定与 case_center 同源；
- cost_ledger 写穿落盘、按天汇总、真值/充值/对账结果读写；
- 硅基账单 CSV 解析（列名模糊匹配、计费项 → 模型名、按天汇总）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai import llm_cost as lc  # noqa: E402
from src.ai.cost_ledger import (  # noqa: E402
    CostLedger,
    model_from_bill_item,
    parse_bill_csv,
)


def _tracker():
    t = lc.LlmCostTracker()
    t.set_pricing({"deepseek-ai/DeepSeek-V3.2": {"prompt": 0.004, "completion": 0.006},
                   "Qwen/Qwen3-VL-8B-Instruct": {"prompt": 0.0005, "completion": 0.002}})
    return t


def test_record_buckets_by_purpose_and_prices_in_cny():
    t = _tracker()
    t.record(model="deepseek-ai/DeepSeek-V3.2", prompt_tokens=1000, completion_tokens=1000,
             purpose=lc.PURPOSE_CUSTOMER_REPLY, provider="siliconflow")
    t.record(model="deepseek-ai/DeepSeek-V3.2", prompt_tokens=1000, completion_tokens=0,
             purpose=lc.PURPOSE_MEMORY_EXTRACT, provider="siliconflow")
    d = t.dump()
    assert d["currency"] == "CNY"
    assert abs(d["total_cost"] - 0.014) < 1e-9
    purposes = {r["purpose"]: r for r in d["rows"]}
    assert abs(purposes["customer_reply"]["cost"] - 0.010) < 1e-9
    assert abs(purposes["memory_extract"]["cost"] - 0.004) < 1e-9
    # 旧看板字段仍在
    assert d["total_cost_usd"] == d["total_cost"]
    assert purposes["customer_reply"]["cost_usd"] == purposes["customer_reply"]["cost"]


def test_unknown_purpose_normalized_and_prom_has_purpose_label():
    t = _tracker()
    t.record(model="x", prompt_tokens=1, completion_tokens=1, purpose="typo_here")
    assert t.dump()["rows"][0]["purpose"] == "unknown"
    assert 'purpose="unknown"' in t.dump_prom()


def test_pricing_fuzzy_matches_bill_style_model_names():
    t = _tracker()
    # 账单里的写法：小写 + 无厂商前缀
    assert abs(t.estimate_cost("deepseek-v3.2", 1000, 0) - 0.004) < 1e-9
    assert abs(t.estimate_cost("qwen/qwen3-vl-8b-instruct", 0, 1000) - 0.002) < 1e-9
    assert t.estimate_cost("never-heard", 1000, 1000) == 0.0


def test_sink_called_per_record_and_failures_swallowed():
    t = _tracker()
    got = []
    t.set_sink(lambda rec: got.append(rec))
    t.record(model="m", prompt_tokens=5, completion_tokens=2, purpose=lc.PURPOSE_PROBE,
             provider="siliconflow", status="ok")
    assert len(got) == 1 and got[0]["purpose"] == "probe" and got[0]["provider"] == "siliconflow"

    def boom(_):
        raise RuntimeError("sink down")
    t.set_sink(boom)
    t.record(model="m", prompt_tokens=1, completion_tokens=1)  # 不抛
    assert t.dump()["total_calls"] == 2


def test_suspected_calls_tracked_separately():
    t = _tracker()
    t.record(model="deepseek-ai/DeepSeek-V3.2", prompt_tokens=1000, completion_tokens=0,
             purpose=lc.PURPOSE_CUSTOMER_REPLY, status="timeout", suspected=True)
    row = t.dump()["rows"][0]
    assert row["suspected_calls"] == 1 and abs(row["suspected_cost"] - 0.004) < 1e-9


def test_drill_range_matches_case_center():
    from src.ai import llm_purpose
    from src.utils.case_center import DRILL_UID_RANGES, is_drill_uid
    # B2（9bb145fc）把用途 API 单一实现挪到 llm_purpose，llm_cost 只 re-export；
    # 演练号段真值随之搬家，本测试跟着指过去（与 case_center 同源断言不变）。
    assert llm_purpose._DRILL_RANGES == DRILL_UID_RANGES
    for v in ("990001001", "telegram:8244899900:990001006", 990001999):
        assert lc.is_drill_id(v) and is_drill_uid(str(v))
    for v in ("8244899900", "telegram:x:5433982810", "U1234abc", "", None):
        assert not lc.is_drill_id(v)
    assert lc.purpose_for_reply({"chat_id": "990001002"}) == "drill"
    assert lc.purpose_for_reply({"chat_id": "5433982810"}) == "customer_reply"
    assert lc.purpose_for_reply(None) == "customer_reply"


def test_provider_from_base_url():
    assert lc.provider_from_base_url("https://api.siliconflow.cn/v1") == "siliconflow"
    assert lc.provider_from_base_url("https://api.deepseek.com/v1") == "deepseek"
    assert lc.provider_from_base_url("http://192.168.0.173:8001/v1") == "lan"
    assert lc.provider_from_base_url("") == ""


def test_record_usage_from_response_handles_sdk_and_dict(monkeypatch):
    t = _tracker()
    monkeypatch.setattr(lc, "_SINGLETON", t)

    class U:
        prompt_tokens = 10
        completion_tokens = 3

    class R:
        usage = U()
        model = "deepseek-ai/DeepSeek-V3.2"

    lc.record_usage_from_response(R(), model="deepseek-ai/DeepSeek-V3.2",
                                  purpose=lc.PURPOSE_ASSISTANT, provider="siliconflow")
    lc.record_usage_from_response({"prompt_eval_count": 7, "eval_count": 1}, model="chatx",
                                  purpose=lc.PURPOSE_TRANSLATE, provider="lan")
    rows = {r["purpose"]: r for r in t.dump()["rows"]}
    assert rows["assistant"]["prompt_tokens"] == 10 and rows["assistant"]["completion_tokens"] == 3
    assert rows["translate"]["prompt_tokens"] == 7


# ── 账本 ─────────────────────────────────────────────────────────────────────

def _ledger(tmp_path):
    return CostLedger(str(tmp_path / "cost.db"))


def test_ledger_write_through_and_day_summary(tmp_path):
    led = _ledger(tmp_path)
    base = {"ts": 1_800_000_000.0, "provider": "siliconflow", "model": "m", "account_id": "default"}
    led.record_usage({**base, "purpose": "customer_reply", "prompt_tokens": 100,
                      "completion_tokens": 10, "cost": 0.5})
    led.record_usage({**base, "purpose": "customer_reply", "prompt_tokens": 100,
                      "completion_tokens": 10, "cost": 0.5})
    led.record_usage({**base, "purpose": "drill", "prompt_tokens": 50,
                      "completion_tokens": 5, "cost": 0.2, "suspected": True})
    from src.ai.cost_ledger import day_of
    day = day_of(1_800_000_000.0)
    s = led.day_summary(day, "siliconflow")
    assert s["calls"] == 3 and abs(s["cost"] - 1.2) < 1e-6
    assert s["by_purpose"]["customer_reply"]["calls"] == 2
    assert s["suspected_calls"] == 1 and abs(s["suspected_cost"] - 0.2) < 1e-6
    # 重开库仍在（落盘）
    led.close()
    led2 = CostLedger(str(tmp_path / "cost.db"))
    assert led2.day_summary(day)["calls"] == 3
    assert led2.providers() == ["siliconflow"]


def test_ledger_daily_costs_fills_zero_days(tmp_path):
    led = _ledger(tmp_path)
    led.record_usage({"ts": 1_800_000_000.0, "provider": "p", "model": "m",
                      "purpose": "probe", "cost": 1.0})
    from src.ai.cost_ledger import day_of
    rows = led.daily_costs(3, end_day=day_of(1_800_000_000.0))
    assert len(rows) == 3 and rows[-1]["cost"] == 1.0 and rows[0]["cost"] == 0.0


def test_ledger_truth_recharge_recon_roundtrip(tmp_path):
    led = _ledger(tmp_path)
    led.set_truth("2026-09-08", "siliconflow", 1.25, balance=96.5, source="manual", note="手填")
    led.set_truth("2026-09-08", "siliconflow", 1.30, source="csv")   # 覆盖金额，余额保留
    t = led.get_truth("2026-09-08", "siliconflow")
    assert t["amount"] == 1.30 and t["balance"] == 96.5 and t["source"] == "csv"
    assert led.latest_balance("siliconflow")["balance"] == 96.5
    led.add_recharge("siliconflow", 300, note="上次充值")
    assert led.recharges("siliconflow")[0]["amount"] == 300
    led.save_recon("2026-09-08", "siliconflow", internal=1.2, truth=1.3, diff_pct=-7.7,
                   verdict="ok", reasons=[{"rule": "x"}], summary="s")
    r = led.recon_rows("siliconflow")[0]
    assert r["verdict"] == "ok" and r["reasons"] == [{"rule": "x"}]


# ── 账单 CSV ─────────────────────────────────────────────────────────────────

_CSV = """计费周期,费用流水ID,费用发生时间,计费项,计费项原始用量,资源包抵扣用量,资源包抵扣后余量,用量单位,计费项单价,计费金额
2026-09-08 14:58,B1,2026-09-08 14:57,deepseek-ai/deepseek-v3.2.online.input-tokens,1.1360,0,1.1360,K tokens,0.004,0.0045
2026-09-08 14:58,B2,2026-09-08 14:57,deepseek-ai/deepseek-v3.2.online.output-tokens,0.0050,0,0.0050,K tokens,0.006,0.0000
2026-09-08 14:47,B3,2026-09-08 14:46,qwen/qwen3-vl-8b-instruct.online.input-tokens,0.0830,0,0.0830,K tokens,0.0005,0.0000
2026-09-07 23:10,B4,2026-09-07 23:09,deepseek-ai/deepseek-v3.2.online.input-tokens,3000,0,3000,K tokens,0.004,12.0
"""


def test_parse_bill_csv_groups_by_day_and_model():
    r = parse_bill_csv(_CSV)
    assert r["ok"], r["error"]
    assert r["rows"] == 4
    assert abs(r["days"]["2026-09-08"] - 0.0045) < 1e-9
    assert abs(r["days"]["2026-09-07"] - 12.0) < 1e-9
    assert set(r["by_model"]["2026-09-08"]) == {"deepseek-ai/deepseek-v3.2",
                                                "qwen/qwen3-vl-8b-instruct"}


def test_parse_bill_csv_tolerates_bom_preamble_and_tsv():
    tsv = "\ufeff导出说明\n" + _CSV.replace(",", "\t")
    r = parse_bill_csv(tsv)
    assert r["ok"] and r["rows"] == 4


def test_parse_bill_csv_reports_missing_columns_in_plain_words():
    r = parse_bill_csv("a,b,c\n1,2,3\n")
    assert not r["ok"] and "表头" in r["error"]
    assert not parse_bill_csv("")["ok"]


def test_model_from_bill_item():
    assert model_from_bill_item("deepseek-ai/deepseek-v3.2.online.input-tokens") == "deepseek-ai/deepseek-v3.2"
    assert model_from_bill_item("Qwen/Qwen3-VL-8B-Instruct.output_tokens") == "Qwen/Qwen3-VL-8B-Instruct"
    assert model_from_bill_item("plain") == "plain"
