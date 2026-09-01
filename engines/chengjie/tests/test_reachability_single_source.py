# -*- coding: utf-8 -*-
"""可加回口径单源门禁（卡片聚合 ≡ 迁移包逐行，2026-08-28）。

「可加回句柄覆盖率」有两个消费面：

- **迁移包**逐行走 `derive_handles()`（Python，每人一行）；
- **资产中心卡片**走一条聚合 SQL（`reachability_over_union`；几万人不可能拉进
  内存逐行算）。

两边必须**人群相同、规则相同**。2026-08-28 实测踩到的两个真 bug：

1. **规则**：WhatsApp 的 chat_key 就是手机号（协议层用号码寻址），而 `phone`
   身份列只有 11/47 条填上 → 不派生就把该平台低报到 6%（真实接近 100%）；
2. **人群**：卡片原先按**私聊会话行**算分母，却在旁边显示并集口径的「联系人」
   总数 —— 某 WA 账号 9 vs 165，两个数字挨着摆、看起来后者是前者的比例。

本文件用同一批数据跑两条**真路径**对账。共享常量只能保证「看起来一致」，
行为等价必须测出来——本测试的首版就因为夹具里「每个人恰好一条会话」而把人群
差异藏住了，故现在**刻意放入通讯录-only 联系人**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.inbox.migration_export import (
    CHAT_KEY_IS_PHONE_PLATFORMS,
    PHONE_MAX_DIGITS,
    PHONE_MIN_DIGITS,
    collect_contacts,
    derived_phone_sql,
    reachability_over_union,
    summarize_reachability,
)
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore

#: 有会话的人：(platform, account, chat_key, username, phone)
_CONVS = [
    # 身份列齐 / 半齐（两条路径都不派生）
    ("whatsapp", "wa", "639000000001", "ann", "+15550001"),
    ("whatsapp", "wa", "639000000002", "bob", ""),
    ("whatsapp", "wa", "639000000003", "", "+15550003"),
    # 身份列全空 + 白名单平台 + 裸号码 → 两条路径都该派生
    ("whatsapp", "wa", "639273815533", "", ""),
    ("whatsapp", "wa", "+85263115820", "", ""),
    # 白名单内但形态不是号码 → 都不派生
    ("whatsapp", "wa", "12345", "", ""),                    # 太短
    ("whatsapp", "wa", "9" * 16, "", ""),                   # 太长
    ("whatsapp", "wa", "8506426282abc", "", ""),            # 尾部字母
    ("whatsapp", "wa", "639000abc001", "", ""),             # 中间字母
    # 关键反例：同形态但平台不在白名单 → 绝不派生（TG 数字 id）
    ("telegram", "tg", "8506426282", "", ""),
    ("telegram", "tg", "8595708452", "tgname", ""),
    ("telegram", "tg", "8762705170", "S198807", "85244356532"),
    # LINE MID / Messenger FB id：都不是可加回句柄
    ("line", "ln", "UIq2koGBkmzY7hBMCoKrk24SBeDO4jfjsMnrNVaZMpEo", "", ""),
    ("messenger", "ms", "1054679137531045", "", ""),
    # 身份列是空白串（平台常回填空格）→ 视同没有
    ("whatsapp", "wa2", "639000000009", "  ", "  "),
]

#: **只在通讯录里、从未聊过**的人：(platform, account, chat_key, name)
#: 这批人没有任何 conversations 行 → 无身份列。WA 的仍应因 chat_key 派生而算
#: 可加回；TG 的如实算无句柄（偏低但为真，优于偏高的假数字）。
_BOOK_ONLY = [
    ("whatsapp", "wa", "639684974869", "MBB P"),
    ("whatsapp", "wa", "639685471428", "ROBB E"),
    ("whatsapp", "wa", "not-a-number", "Weird"),
    ("telegram", "tg", "367388154", "阿耿"),
    ("telegram", "tg", "388260067", "邱南"),
]


@pytest.fixture()
def store(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    for plat, acct, ck, user, phone in _CONVS:
        st.ingest_batch(
            InboxConversation(
                conversation_id=f"{plat}:{acct}:{ck}", platform=plat,
                account_id=acct, chat_key=ck, display_name=ck,
                username=user, phone=phone, chat_type="private",
                last_ts=10.0, last_text="x"),
            [])
    by_acct: dict = {}
    for plat, acct, ck, name in _BOOK_ONLY:
        by_acct.setdefault((plat, acct), []).append(
            {"chat_key": ck, "name": name})
    for (plat, acct), rows in by_acct.items():
        st.upsert_protocol_contacts(plat, acct, rows)
    yield st
    try:
        st.close()
    except Exception:
        pass


def _accounts():
    seen = []
    for plat, acct, *_ in _CONVS:
        if (plat, acct) not in seen:
            seen.append((plat, acct))
    return seen


def _py_buckets(store, plat, acct):
    """迁移包路径：`collect_contacts` → `summarize_reachability`，桶名对齐卡片。"""
    s = summarize_reachability(collect_contacts(store, plat, acct))
    return {"both": s["both"], "username_only": s["username"],
            "phone_only": s["phone"], "none": s["none"]}


def test_card_aggregate_equals_kit_rowwise(store):
    """**本文件的全部意义**：同一批数据，两条真路径逐桶相同。"""
    for plat, acct in _accounts():
        card = reachability_over_union(Path(store._db_path), plat, acct)
        py = _py_buckets(store, plat, acct)
        for bucket in ("both", "username_only", "phone_only", "none"):
            assert card[bucket] == py[bucket], (
                f"{plat}:{acct} 的 {bucket} 桶不一致："
                f"卡片聚合={card[bucket]} vs 迁移包逐行={py[bucket]}"
                "——两个消费面又各算一套了")


def test_population_is_the_union_not_just_conversations(store):
    """人群必须含通讯录-only 联系人：卡片旁边展示的「联系人」就是这个并集。

    这条是首版夹具漏掉的那个 bug 的回归钉——当时每个人恰好一条会话，会话口径
    与并集口径数值相同，于是分母差异被完全藏住。
    """
    card = reachability_over_union(Path(store._db_path), "whatsapp", "wa")
    total = sum(card[k] for k in
                ("both", "username_only", "phone_only", "none"))
    # wa 组：9 条会话 + 3 个通讯录-only = 12 人
    assert total == 12, f"并集人群应为 12，实际 {total}"
    # 且与卡片展示的「联系人」总数同源同值
    cs = store.protocol_contacts_summary("whatsapp", "wa", include_chats=True)
    assert total == int(cs["total"]), (
        "可加回分母与同卡展示的联系人总数不一致——两个数字并排摆着必须同人群")


def test_book_only_whatsapp_contacts_count_as_reachable(store):
    """WA 通讯录-only 联系人没有身份列，但 chat_key 就是号码 → 可加回。

    这 229 人（生产实数）原先被整体排除在分母外，等于把实实在在能加回的客户
    藏起来了。
    """
    card = reachability_over_union(Path(store._db_path), "whatsapp", "wa")
    # 会话侧：1 both + 1 username_only + 1 真 phone + 2 派生 + 4 形态不合格
    # 通讯录侧：2 个裸号码派生成功 + 1 个 'not-a-number' 无句柄
    assert card["derived"] == 4, "两个来源的裸号码都应派生"
    assert card["phone_only"] == 5      # 1 真 phone + 2 会话派生 + 2 通讯录派生
    assert card["both"] == 1
    assert card["username_only"] == 1
    assert card["none"] == 5            # 4 形态不合格 + 1 'not-a-number'


def test_book_only_telegram_contacts_honestly_unreachable(store):
    """TG 通讯录-only 联系人确实没有句柄 → 如实计入 none，绝不按形态派生。"""
    card = reachability_over_union(Path(store._db_path), "telegram", "tg")
    total = sum(card[k] for k in
                ("both", "username_only", "phone_only", "none"))
    assert total == 5                   # 3 会话 + 2 通讯录-only
    assert card["derived"] == 0, "TG 的数字 id 与手机号形态无从区分，绝不能派生"
    assert card["none"] == 3            # 1 会话无句柄 + 2 通讯录-only


def test_non_whitelisted_platforms_never_derive(store):
    for plat, acct in (("telegram", "tg"), ("line", "ln"), ("messenger", "ms")):
        card = reachability_over_union(Path(store._db_path), plat, acct)
        assert card["derived"] == 0, f"{plat} 不应派生"


def test_blank_identity_columns_treated_as_absent(store):
    card = reachability_over_union(Path(store._db_path), "whatsapp", "wa2")
    assert card["derived"] == 1 and card["phone_only"] == 1


def test_missing_db_degrades_to_zeros_not_crash(tmp_path):
    z = reachability_over_union(tmp_path / "nope.db", "whatsapp", "wa")
    assert z == {"both": 0, "username_only": 0, "phone_only": 0, "none": 0,
                 "derived": 0}
    assert reachability_over_union(None, "whatsapp", "wa")["none"] == 0


def test_sql_projection_uses_full_digit_check():
    """`GLOB '[0-9]*'` 只校验首字符，是个静默错判的坑——必须用 NOT GLOB 全串判。"""
    sql = derived_phone_sql()
    assert "NOT GLOB '*[^0-9]*'" in sql
    assert f"BETWEEN {PHONE_MIN_DIGITS}" in sql
    assert str(PHONE_MAX_DIGITS) in sql
    for p in CHAT_KEY_IS_PHONE_PLATFORMS:
        assert f"'{p}'" in sql
    assert "telegram" not in sql and "line" not in sql


def test_sql_projection_platform_known_shortcuts_in_python():
    """平台已知时不把平台名拼进 SQL；非白名单平台退化成「只认身份列」。"""
    wa = derived_phone_sql(platform="whatsapp")
    assert "'whatsapp'" not in wa and "NOT GLOB" in wa
    tg = derived_phone_sql(platform="telegram")
    assert "NOT GLOB" not in tg and "ltrim" not in tg
    # 大小写与空格不敏感（平台名来自库/注册表，形态不保证规整）
    assert derived_phone_sql(platform=" WhatsApp ") == wa


def test_sql_projection_column_names_are_overridable():
    sql = derived_phone_sql(phone_col="v.phone", username_col="v.username",
                            chat_key_col="c.chat_key")
    assert "v.phone" in sql and "c.chat_key" in sql


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
