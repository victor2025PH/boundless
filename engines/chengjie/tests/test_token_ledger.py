# -*- coding: utf-8 -*-
"""Token 账本门禁（2026-08-19 定价改版 P2 地基；模块当前未接线，纯库级契约）。

钉住四类不变量：
1. **对客公示费率**（金标）——与官网 /pricing 计价表逐项一致；改费率=产品决策，两处同批改。
2. 批次核算纯函数：先到期先扣、过期作废、支出保守分摊（永不多扣客户）。
3. 幂等入账（订单 ref / 月度键）与 fail-open（账本异常绝不阻断主链）。
4. 官网源码交叉钉（同机有 website/ 时）：engine TOKEN_RATES ⟺ chatx-pricing.ts TOKEN_RATES。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.licensing.token_ledger import (
    BONUS_VALID_MONTHS,
    CHARS_PER_TOKEN_LEGACY,
    PACK_VALID_MONTHS,
    TOKEN_RATES,
    TokenLedgerStore,
    allocate_spend,
    check_token_balance,
    configure_token_ledger,
    record_token_action,
    reset_token_ledger,
    token_ledger_enabled,
    tokens_for,
)


@pytest.fixture(autouse=True)
def _isolated_ledger():
    """每例独立内存账本；测试绝不落生产数据区。"""
    reset_token_ledger()
    store = TokenLedgerStore(":memory:")
    configure_token_ledger(store=store)
    yield store
    reset_token_ledger()


# ── 1) 对客公示费率金标 ──────────────────────────────────────────────────────

def test_published_rates_golden():
    """费率 = 对外承诺。任何漂移先红这里，再走产品决策同批改官网。"""
    golden = {
        "std_translate": (0, 1000),
        "ai_reply": (10, 1),
        "pro_translate": (10, 1000),
        "deepl_translate": (40, 1000),
        "voice_clone": (10, 100),
        "ai_image": (50, 1),
        "asr": (5, 1),
    }
    assert set(TOKEN_RATES) == set(golden)
    for k, (tok, size) in golden.items():
        assert TOKEN_RATES[k]["tokens"] == tok, k
        assert TOKEN_RATES[k]["unit_size"] == size, k
    # 旧字符池并账口径 = 专业翻译费率的精确倒数（1M 字符 = 10,000 Token）
    assert CHARS_PER_TOKEN_LEGACY == 100
    assert 1_000_000 // CHARS_PER_TOKEN_LEGACY == 10_000


def test_tokens_for_ceil_and_edges():
    assert tokens_for("ai_reply", 1) == 10
    assert tokens_for("pro_translate", 1500) == 20      # ceil(1.5) × 10
    assert tokens_for("pro_translate", 1000) == 10
    assert tokens_for("pro_translate", 1) == 10          # 不足一档进一档
    assert tokens_for("deepl_translate", 2500) == 120    # ceil(2.5) × 40
    assert tokens_for("voice_clone", 40) == 10           # 40 字符 → 1 档
    assert tokens_for("voice_clone", 101) == 20
    assert tokens_for("ai_image", 3) == 150
    assert tokens_for("asr", 2.5) == 15
    # 免费/零量/未知动作 → 0（未知动作宁可漏计不误扣）
    assert tokens_for("std_translate", 999_999) == 0
    assert tokens_for("ai_reply", 0) == 0
    assert tokens_for("no_such_action", 5) == 0


# ── 2) 批次核算纯函数 ────────────────────────────────────────────────────────

def test_allocate_spend_expiry_order_and_loss():
    """按到期序吸收（含过期批次）——2026-08-19 P6 修正语义：上月支出记在上月批次头上，
    绝不侵蚀本月新含量（初版「过期不吸收」会系统性少给客户，被 P6 月度续杯门禁抓出）。"""
    now = 1_000_000.0
    grants = [
        (10_000, now + 100),   # 快到期（月度含量）
        (60_000, now + 9_999), # Token 包
        (5_000, now - 1),      # 已过期 → 最先吸收支出（它活跃期内的消费记它头上）
    ]
    out = allocate_spend(grants, total_spend=12_000, now=now)
    # 过期 5k 全被支出吃掉（无作废）→ 10k 批再吸 7k → 余 3k + 完整 60k
    assert out["expired_lost"] == 0
    assert out["active_granted"] == 70_000
    assert out["balance"] == 63_000
    assert out["spend_unmet"] == 0
    # 无支出时过期批次原样作废
    out2 = allocate_spend(grants, total_spend=0, now=now)
    assert out2["expired_lost"] == 5_000 and out2["balance"] == 70_000


def test_allocate_spend_conservative_and_unmet():
    now = 1_000_000.0
    # 支出超过现存活批次（历史批次已过期）：余额 0、溢出如实报 unmet，绝不出负数
    out = allocate_spend([(1_000, now + 10)], total_spend=5_000, now=now)
    assert out["balance"] == 0
    assert out["spend_unmet"] == 4_000
    # 永不过期批次（bonus）排在最后扣
    out2 = allocate_spend([(100, None), (100, now + 5)], total_spend=100, now=now)
    assert out2["balance"] == 100  # 先扣掉快过期那 100，bonus 完整保留


# ── 3) 入账幂等 / 余额 / 闸门语义 ────────────────────────────────────────────

def test_pack_grant_idempotent_by_order_ref(_isolated_ledger):
    s = _isolated_ledger
    assert s.grant_pack("lic1", 60_000, ref="AH-20260819-XYZ") is True
    assert s.grant_pack("lic1", 60_000, ref="AH-20260819-XYZ") is False  # 同订单重复入账拒绝
    assert s.balance("lic1")["balance"] == 60_000


def test_monthly_grant_once_per_month(_isolated_ledger):
    s = _isolated_ledger
    now = time.time()
    assert s.grant_monthly("lic1", 30_000, now=now) is True
    assert s.grant_monthly("lic1", 30_000, now=now) is False   # 同月幂等
    assert s.balance("lic1", now=now)["balance"] == 30_000
    # 月度含量在月底过期：跨月核算时旧含量作废
    far = now + 40 * 86400
    bal = s.balance("lic1", now=far)
    assert bal["balance"] == 0
    assert bal["expired_lost"] == 30_000


def test_pack_expires_after_valid_months(_isolated_ledger):
    s = _isolated_ledger
    now = time.time()
    s.grant_pack("lic1", 10_000, ref="o1", now=now)
    assert s.balance("lic1", now=now)["balance"] == 10_000
    after = now + 13 * 31 * 86400  # 13 个月后
    assert s.balance("lic1", now=after)["balance"] == 0


def test_bonus_expires_and_spends_before_paid_pack(_isolated_ledger):
    """「赠送 6 个月有效且先扣」（实施50 P2 修正）：bonus 默认 6 个月过期 →
    到期序天然排在 12 个月实付包之前——先赠后实扣由结构成立，不靠优先级分支。
    旧语义（bonus 永不过期）实际先扣实付包，与官网公示相反。"""
    assert BONUS_VALID_MONTHS == 6 and PACK_VALID_MONTHS == 12
    s = _isolated_ledger
    now = time.time()
    s.grant_bonus("lic1", 1_000, ref="signup", now=now)
    s.grant_pack("lic1", 10_000, ref="o1", now=now)
    s.record_spend("lic1", "ai_reply", 600, now=now)
    bal = s.balance("lic1", now=now)
    assert bal["balance"] == 10_400  # 赠送 1000 先被吃 600，实付包完整
    # 7 个月后：赠送批过期；只作废「没被支出吸收」的 400，实付 10k 完整存活
    after = now + 7 * 30.44 * 86400
    bal2 = s.balance("lic1", now=after)
    assert bal2["balance"] == 10_000
    assert bal2["expired_lost"] == 400


def test_bonus_explicit_never_expire_and_migration_kind(_isolated_ledger):
    """显式 valid_months=None 仍可发永不过期赠送（产品决策口子）；
    并账批次走独立 kind=migration（付费存量价值，永不过期，与赠送分类隔离）。"""
    s = _isolated_ledger
    now = time.time()
    s.grant_bonus("lic1", 500, ref="evergreen", valid_months=None)
    s.grant_migration("lic1", 2_000, ref="mig:lic1")
    far = now + 36 * 30.44 * 86400
    assert s.balance("lic1", now=far)["balance"] == 2_500
    # 幂等
    assert s.grant_migration("lic1", 2_000, ref="mig:lic1") is False


def test_spend_order_monthly_first(_isolated_ledger):
    s = _isolated_ledger
    now = time.time()
    s.grant_monthly("lic1", 1_000, now=now)          # 月底过期（先扣）
    s.grant_pack("lic1", 10_000, ref="o1", now=now)  # 12 个月（后扣）
    s.record_spend("lic1", "ai_reply", 1_200, now=now)
    bal = s.balance("lic1", now=now)
    assert bal["balance"] == 9_800                    # 1000 月度吃满 + 包扣 200


def test_check_balance_semantics(_isolated_ledger):
    s = _isolated_ledger
    # 空钱包（从未发过批次）= 未启用语义：放行、不告警
    out = check_token_balance("licX")
    assert out["allowed"] is True and out["exhausted"] is False
    # 有批次且耗尽：enforce 关 → 放行 + exhausted；enforce 开 → 阻断
    s.grant_pack("licX", 100, ref="o1")
    s.record_spend("licX", "ai_reply", 100)
    soft = check_token_balance("licX", enforce=False)
    assert soft["allowed"] is True and soft["exhausted"] is True
    hard = check_token_balance("licX", enforce=True)
    assert hard["allowed"] is False and hard["exhausted"] is True


def test_record_token_action_end_to_end(_isolated_ledger):
    s = _isolated_ledger
    s.grant_pack("lic1", 1_000, ref="o1")
    n = record_token_action("lic1", "pro_translate", 2_000)  # 2k 字符 → 20 Token
    assert n == 20
    assert s.balance("lic1")["balance"] == 980
    assert record_token_action("lic1", "std_translate", 50_000) == 0  # 免费动作零记账
    usage = s.usage("lic1")
    assert usage["by_action"] == {"pro_translate": 20}
    assert usage["grants"][0]["ref"] == "o1"


def test_default_disabled_and_fail_open():
    assert token_ledger_enabled({}) is False
    assert token_ledger_enabled({"licensing": {"token_ledger": {"enabled": True}}}) is True
    # store 缺席（reset 后未配置且不落盘）→ check/record 全 fail-open
    reset_token_ledger()
    configure_token_ledger(db_path=":memory:")
    out = check_token_balance("any")
    assert out["allowed"] is True
    assert record_token_action("any", "ai_reply", 1) == 10  # 内存库可记


# ── 4) 官网源码交叉钉（同机有 website/ 才跑；CI 无 → skip）──────────────────

def test_rates_match_website_source():
    site = Path(__file__).resolve().parents[3] / "website" / "lib" / "chatx-pricing.ts"
    if not site.exists():
        pytest.skip("website/ 不在本机（独立 CI 上下文），跳过跨仓费率比对")
    src = site.read_text(encoding="utf-8")
    blocks = re.findall(r'key:\s*"(\w+)",[\s\S]{0,400}?tokens:\s*(\d+),', src)
    site_rates = {k: int(v) for k, v in blocks if k in TOKEN_RATES}
    missing = set(TOKEN_RATES) - set(site_rates)
    assert not missing, f"官网计价表缺动作：{missing}"
    for k, v in site_rates.items():
        assert v == TOKEN_RATES[k]["tokens"], (
            f"费率分叉：{k} 官网 {v} vs 引擎 {TOKEN_RATES[k]['tokens']}（两处同批改）"
        )
