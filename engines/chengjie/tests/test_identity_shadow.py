"""P2.3 跨平台身份影子匹配门禁。

钉住的语义（改 src/utils/identity_shadow.py 的阈值/规则必须先改这里）：
电话规范化各变体与垃圾拒收、后缀比对护栏（防撞尾误配）、三档 tier、
防误报（短名/常见名/占位 username/空字段）、whatsapp chat_key 兜底当电话
而 telegram 数字 id 绝不当电话、同平台不配对、确定性输出、已关联标注、
CLI 只读 + JSON 冒烟 + feature flag 默认关闸门。
"""

import json
import random

from scripts.identity_shadow_scan import main as cli_main
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.utils.cross_platform_identity import CrossPlatformIdentity
from src.utils.identity_shadow import (
    TIER_HIGH,
    TIER_LOW,
    TIER_MEDIUM,
    IdentityCandidate,
    annotate_already_linked,
    candidates_from_conversations,
    display_name_key,
    match_candidates,
    normalize_phone,
    phones_match,
    run_shadow_scan,
    signal_coverage,
    username_key,
)


def _c(platform, chat_key, name="", phone="", username=""):
    return IdentityCandidate(platform=platform, chat_key=chat_key,
                             display_name=name, phone=phone, username=username)


# ── 电话规范化 ──────────────────────────────────────────────────────────────

def test_normalize_phone_variants():
    # +86 / 86 / 0086 / 空格 / 连字符 / 括号 归一到同一形态
    assert normalize_phone("+86 138-1234-5678") == "8613812345678"
    assert normalize_phone("0086 138 1234 5678") == "8613812345678"
    assert normalize_phone("86(138)12345678") == "8613812345678"
    # 菲律宾：+63 / 0063 / 63 同形
    assert normalize_phone("+63 927 013 5480") == "639270135480"
    assert normalize_phone("00639270135480") == "639270135480"
    assert normalize_phone("639270135480") == "639270135480"
    # 国内 trunk-0 不在规范化期剥（比对期变体处理）
    assert normalize_phone("0927 013 5480") == "09270135480"


def test_normalize_phone_rejects_junk():
    assert normalize_phone("") == ""
    assert normalize_phone(None) == ""
    assert normalize_phone("1234567") == ""            # 7 位过短
    assert normalize_phone("abc-def") == ""            # 无数字
    assert normalize_phone("0" * 12) == ""             # 全同数字占位
    assert normalize_phone("1" * 16) == ""             # 超 E.164 上限
    # 00 冠码剥掉后过短也拒收
    assert normalize_phone("00123456") == ""


def test_phones_match_cc_and_trunk_variants():
    # 精确相等
    assert phones_match("8613812345678", "8613812345678")
    # 带国家码 vs 缺国家码（后缀护栏放行：短号 11 位 ≥9、位差 2 ≤4）
    assert phones_match("8613812345678", "13812345678")
    # 国际形 vs 国内 trunk-0 形（经变体剥 0 后后缀命中）
    assert phones_match("639270135480", "09270135480")
    # 对称性
    assert phones_match("13812345678", "8613812345678")


def test_phones_match_guards():
    # 不同号码（末位不同）绝不配
    assert not phones_match("8613812345678", "8613812345679")
    # 短号后缀 <9 位不参与后缀比对：8 位合法号不因是别人的尾巴而配对
    assert not phones_match("8612345678", "12345678")
    # 位差 >4 拒配（防长号「碰巧同尾」）：9 位短号 vs 前面多挂 5 位的 14 位长号
    assert not phones_match("912345678", "12345912345678")
    # 空/无效永不匹配
    assert not phones_match("", "8613812345678")


# ── 三档 tier 语义 ──────────────────────────────────────────────────────────

def test_high_tier_phone_pair():
    pairs = match_candidates([
        _c("whatsapp", "639270135480", name="Zhang Wei"),      # chat_key 即电话
        _c("telegram", "8921664288", name="张伟", phone="+63 927 013 5480"),
    ])
    assert len(pairs) == 1
    p = pairs[0]
    assert p.tier == TIER_HIGH
    assert any(ev.startswith("phone:") for ev in p.evidence)
    assert {p.a.platform, p.b.platform} == {"telegram", "whatsapp"}


def test_whatsapp_chat_key_is_phone_but_telegram_id_is_not():
    # telegram 的数字 chat_key 是 user id，即使形如电话也绝不当电话参与匹配
    pairs = match_candidates([
        _c("telegram", "639270135480"),                        # 数字 uid，无 phone 列
        _c("whatsapp", "639270135480"),                        # chat_key 兜底当电话
    ])
    assert pairs == []                                         # 无第二个电话来源 → 配不上
    # 而 whatsapp chat_key ↔ 对方平台 phone 列可以配上（上一测已覆盖）


def test_medium_tier_username():
    pairs = match_candidates([
        _c("telegram", "111", username="@NightWolf88"),
        _c("line", "U-abc", username="nightwolf88"),
    ])
    assert len(pairs) == 1
    assert pairs[0].tier == TIER_MEDIUM
    assert pairs[0].evidence == ["username:nightwolf88"]


def test_username_guards():
    # 占位词 / 过短 / 纯数字 → 无匹配键
    assert username_key("admin") == ""
    assert username_key("bot") == ""
    assert username_key("abc") == ""            # <4
    assert username_key("12345678") == ""       # 无字母
    assert username_key("@NightWolf88") == "nightwolf88"
    # 占位 username 两边相同也不配对
    assert match_candidates([
        _c("telegram", "1", username="admin"),
        _c("line", "2", username="admin"),
    ]) == []


def test_low_tier_display_name_semantics():
    # ≥2 CJK 配上；low 只进报告（tier 标注 low）
    pairs = match_candidates([
        _c("telegram", "111", name="张伟强"),
        _c("whatsapp", "639000000123", name="张伟强"),
    ])
    assert len(pairs) == 1 and pairs[0].tier == TIER_LOW
    assert pairs[0].evidence == ["display_name:张伟强"]
    # ≥5 拉丁字母配上
    pairs = match_candidates([
        _c("telegram", "111", name="Rodel Bautista"),
        _c("line", "U-1", name="Rodel Bautista"),
    ])
    assert len(pairs) == 1 and pairs[0].tier == TIER_LOW


def test_display_name_guards():
    assert display_name_key("王") == ""                 # 单字
    assert display_name_key("Liam") == ""               # 拉丁 4 字母 <5
    assert display_name_key("Maria") == ""              # 常见名 skip 表
    assert display_name_key("小明") == ""               # 常见名 skip 表
    assert display_name_key("微信用户") == ""           # 平台占位
    assert display_name_key("12345678") == ""           # 纯数字＝id 回落
    assert display_name_key("  张  伟强 ") == "张 伟强"  # 空白折叠
    # 名字 == chat_key（裸 id 回落名）无信号 → 不配对
    assert match_candidates([
        _c("telegram", "88888888", name="88888888"),
        _c("line", "U-2", name="88888888"),
    ]) == []


# ── 结构性护栏 ──────────────────────────────────────────────────────────────

def test_same_platform_never_pairs():
    pairs = match_candidates([
        _c("telegram", "111", phone="8613812345678", username="wolfking",
           name="张伟强"),
        _c("telegram", "222", phone="8613812345678", username="wolfking",
           name="张伟强"),
    ])
    assert pairs == []


def test_pair_dedup_merges_tiers_and_evidence():
    # 同一对命中 电话+username+名字 → 只出一条，tier=high，evidence 三条全保留
    pairs = match_candidates([
        _c("telegram", "111", phone="+63 927 013 5480",
           username="nightwolf88", name="张伟强"),
        _c("whatsapp", "639270135480", username="nightwolf88", name="张伟强"),
    ])
    assert len(pairs) == 1
    p = pairs[0]
    assert p.tier == TIER_HIGH
    kinds = {ev.split(":", 1)[0] for ev in p.evidence}
    assert kinds == {"phone", "username", "display_name"}


def test_deterministic_output_under_shuffle():
    base = [
        _c("telegram", "111", phone="+8613812345678", name="张伟强"),
        _c("whatsapp", "13812345678", name="张伟强"),
        _c("line", "U-9", username="nightwolf88"),
        _c("telegram", "222", username="nightwolf88"),
        _c("whatsapp", "639000000123", name="Rodel Bautista"),
        _c("line", "U-7", name="Rodel Bautista"),
    ]
    expect = [p.to_dict() for p in match_candidates(base)]
    rng = random.Random(42)
    for _ in range(5):
        shuffled = list(base)
        rng.shuffle(shuffled)
        got = [p.to_dict() for p in match_candidates(shuffled)]
        assert got == expect
    # tier 排序：high 在 medium/low 前
    tiers = [p["tier"] for p in expect]
    assert tiers == sorted(tiers, key=lambda t: {"high": 0, "medium": 1, "low": 2}[t])




def test_duplicate_rows_merge_phone_signal():
    """同 (platform, chat_key) 多行时合并 phone，顺序无关（生产漏检回归）。"""
    # 先出现无 phone 的镜像行，再出现有 phone 的回填行——旧逻辑保先会丢电话
    empty_first = [
        _c("telegram", "6107037825", name="Maj", username="mcoy415"),
        _c("telegram", "6107037825", name="Maj", phone="639649321471",
           username="mcoy415"),
        _c("whatsapp", "639649321471", name="639649321471"),
        _c("whatsapp", "639649321471", name="Wisley", phone="639649321471"),
    ]
    phone_first = list(reversed(empty_first))
    for batch in (empty_first, phone_first):
        pairs = match_candidates(batch)
        assert len(pairs) == 1 and pairs[0].tier == TIER_HIGH
        assert any("639649321471" in ev for ev in pairs[0].evidence)
        # display_name 合并：号码占位让位真人名
        wa = pairs[0].a if pairs[0].a.platform == "whatsapp" else pairs[0].b
        assert wa.display_name == "Wisley"


def test_annotate_already_linked():
    pairs = match_candidates([
        _c("telegram", "111", phone="8613812345678"),
        _c("whatsapp", "13812345678"),
    ])
    assert len(pairs) == 1
    # 未关联
    annotate_already_linked(pairs, {})
    assert pairs[0].already_linked is False
    # 双方同 canonical → 已关联
    links = {("telegram", "111"): "telegram:111",
             ("whatsapp", "13812345678"): "telegram:111"}
    annotate_already_linked(pairs, links)
    assert pairs[0].already_linked is True
    # 各挂各的 canonical → 不算已关联
    links = {("telegram", "111"): "telegram:111",
             ("whatsapp", "13812345678"): "whatsapp:13812345678"}
    annotate_already_linked(pairs, links)
    assert pairs[0].already_linked is False
    # 账号分桶键（acct:chat_key）也能标已关联——生产 CPI 真实口径
    links = {("telegram", "acctA:111"): "canon-x",
             ("whatsapp", "acctB:13812345678"): "canon-x"}
    annotate_already_linked(pairs, links)
    assert pairs[0].already_linked is True


def test_candidates_from_conversations_filters():
    rows = [
        # 有效候选（whatsapp chat_key 即电话）
        {"platform": "whatsapp", "account_id": "a", "chat_key": "639270135480",
         "display_name": "Zhang Wei", "chat_type": "private"},
        # 群聊 → 跳过
        {"platform": "telegram", "account_id": "a", "chat_key": "-100123",
         "display_name": "水群", "chat_type": "group"},
        # Telegram Saved Messages（chat_key == account_id）→ 系统号跳过
        {"platform": "telegram", "account_id": "8244899900",
         "chat_key": "8244899900", "display_name": "收藏夹",
         "chat_type": "private"},
        # whatsapp 广播 → 系统号跳过
        {"platform": "whatsapp", "account_id": "a",
         "chat_key": "status@broadcast", "display_name": "Status",
         "chat_type": "private"},
        # 无信号（名字过短、无电话无 username）→ 跳过
        {"platform": "line", "account_id": "a", "chat_key": "U-1",
         "display_name": "王", "chat_type": "private"},
    ]
    cands, skipped = candidates_from_conversations(rows)
    assert [c.chat_key for c in cands] == ["639270135480"]
    assert skipped == {"group_or_channel": 1, "system_peer": 2, "no_signal": 1}


# ── 信号覆盖（P7：ops 卡自解释「配对 0 = 覆盖不足还是匹配问题」）────────────────

def test_signal_coverage_semantics():
    rows = [
        # whatsapp 私聊：chat_key 即号码 → 有效 phone（列本身为空）
        {"platform": "whatsapp", "account_id": "a", "chat_key": "639270135480",
         "display_name": "Zhang Wei", "chat_type": "private"},
        # telegram：有 phone 列 + username
        {"platform": "telegram", "account_id": "a", "chat_key": "111",
         "display_name": "张伟", "phone": "+63 927 013 5480",
         "username": "nightwolf88", "chat_type": "private"},
        # telegram：无信号行也计入分母（coverage 的意义就是看缺口）
        {"platform": "telegram", "account_id": "a", "chat_key": "222",
         "display_name": "王", "chat_type": "private"},
        # username 仅 "@" 规范化后为空 → 不算有效 username
        {"platform": "line", "account_id": "a", "chat_key": "U-1",
         "display_name": "李雷雷", "username": "@", "chat_type": "private"},
        # 群聊 / 系统号 → 与候选池同口径，不进分母
        {"platform": "telegram", "account_id": "a", "chat_key": "-100123",
         "display_name": "水群", "chat_type": "group"},
        {"platform": "whatsapp", "account_id": "a",
         "chat_key": "status@broadcast", "display_name": "Status",
         "chat_type": "private"},
    ]
    cov = signal_coverage(rows)
    assert list(cov.keys()) == ["line", "telegram", "whatsapp"]  # 平台名有序
    assert cov["whatsapp"] == {"total": 1, "phone": 1, "username": 0}
    assert cov["telegram"] == {"total": 2, "phone": 1, "username": 1}
    assert cov["line"] == {"total": 1, "phone": 0, "username": 0}


# ── 端到端（真实 InboxStore + CrossPlatformIdentity，全部临时库）────────────────

def _seed_conv(store, platform, chat_key, *, name="", phone="", username="",
               account="acct", chat_type="private", ts=1000.0):
    store.upsert_conversation(InboxConversation(
        conversation_id=f"{platform}:{account}:{chat_key}",
        platform=platform, account_id=account, chat_key=chat_key,
        display_name=name, phone=phone, username=username,
        chat_type=chat_type, last_ts=ts))


def _seed_dbs(tmp_path):
    """临时 inbox.db（2 对可配 + 1 群 + 1 无信号）+ bot.db（预关联其中一对）。"""
    inbox_db = tmp_path / "inbox.db"
    store = InboxStore(inbox_db)
    _seed_conv(store, "telegram", "8921664288", name="张伟",
               phone="+63 927 013 5480", ts=4000)
    _seed_conv(store, "whatsapp", "639270135480", name="Zhang Wei", ts=3000)
    _seed_conv(store, "telegram", "555000111", username="nightwolf88", ts=2000)
    _seed_conv(store, "line", "U-deadbeef", username="@NightWolf88", ts=1500)
    _seed_conv(store, "telegram", "-100999", name="羊毛群", chat_type="group",
               ts=1000)
    _seed_conv(store, "line", "U-nosignal", name="王", ts=500)

    identity_db = tmp_path / "bot.db"
    cpi = CrossPlatformIdentity(identity_db)
    cpi.link("telegram", "8921664288", "whatsapp", "639270135480")
    cpi.close()
    return inbox_db, identity_db


def test_run_shadow_scan_end_to_end(tmp_path):
    inbox_db, identity_db = _seed_dbs(tmp_path)
    report = run_shadow_scan(inbox_db, identity_db, limit=100)
    assert report["ok"] is True
    assert report["scanned_conversations"] == 6
    assert report["candidates"] == 4
    assert report["skipped"]["group_or_channel"] == 1
    assert report["skipped"]["no_signal"] == 1
    assert report["counts"] == {"high": 1, "medium": 1, "low": 0,
                                "already_linked": 1}
    # P7 信号覆盖随报告出（群聊不进分母；无信号行进分母）
    assert report["coverage"] == {
        "line": {"total": 2, "phone": 0, "username": 1},
        "telegram": {"total": 2, "phone": 1, "username": 1},
        "whatsapp": {"total": 1, "phone": 1, "username": 0},
    }
    by_tier = {p["tier"]: p for p in report["pairs"]}
    assert by_tier["high"]["already_linked"] is True     # 手动关联过的那对
    assert by_tier["medium"]["already_linked"] is False


def test_run_shadow_scan_is_read_only(tmp_path):
    """扫描前后两库字节不变（mode=ro 的硬证据）。"""
    inbox_db, identity_db = _seed_dbs(tmp_path)
    before = (inbox_db.read_bytes(), identity_db.read_bytes())
    run_shadow_scan(inbox_db, identity_db, limit=100)
    assert (inbox_db.read_bytes(), identity_db.read_bytes()) == before


def test_run_shadow_scan_missing_db(tmp_path):
    report = run_shadow_scan(tmp_path / "nope.db")
    assert report["ok"] is False and "read_inbox_failed" in report["error"]


# ── CLI ────────────────────────────────────────────────────────────────────

def test_cli_json_smoke(tmp_path, capsys):
    inbox_db, identity_db = _seed_dbs(tmp_path)
    rc = cli_main(["--force", "--json",
                   "--inbox-db", str(inbox_db),
                   "--identity-db", str(identity_db)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["counts"]["high"] == 1
    assert len(data["pairs"]) == 2
    # 人类可读模式冒烟（不校验全文，只保证跑通且带关键行）
    rc = cli_main(["--force",
                   "--inbox-db", str(inbox_db),
                   "--identity-db", str(identity_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "疑似配对" in out and "high" in out


def test_cli_respects_feature_flag(tmp_path, capsys):
    """flag 默认关（仓库铁律）→ 不带 --force 时拒跑（exit 2），--force 放行。"""
    inbox_db, identity_db = _seed_dbs(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "contacts:\n  identity_shadow:\n    enabled: false\n", encoding="utf-8")
    rc = cli_main(["--json", "--config", str(cfg),
                   "--inbox-db", str(inbox_db),
                   "--identity-db", str(identity_db)])
    assert rc == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["error"] == "disabled"
    # enabled: true → 无需 --force 即可跑
    cfg.write_text(
        "contacts:\n  identity_shadow:\n    enabled: true\n", encoding="utf-8")
    rc = cli_main(["--json", "--config", str(cfg),
                   "--inbox-db", str(inbox_db),
                   "--identity-db", str(identity_db)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
