"""舰队自嗨排除 + 统一候选卫生门禁（P1 2026-08-03）——真实事故 + 边界钉死。"""
from src.companion.proactive_peer_hygiene import (
    build_own_fleet_index,
    build_peer_filter,
    is_own_fleet_peer,
    is_service_peer_name,
    proactive_candidate_ok,
)


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return self._rows


_FLEET = _FakeRegistry([
    {"platform": "telegram", "account_id": "8041810715"},
    {"platform": "telegram", "account_id": "8755679833"},
    {"platform": "telegram", "account_id": "8244899900"},
    {"platform": "whatsapp", "account_id": "639270135480"},
])


class TestBuildIndex:
    def test_groups_by_platform(self):
        idx = build_own_fleet_index(_FLEET)
        assert idx["telegram"] == {"8041810715", "8755679833", "8244899900"}
        assert idx["whatsapp"] == {"639270135480"}

    def test_bad_registry_returns_empty(self):
        assert build_own_fleet_index(None) == {}

        class Boom:
            def list(self):
                raise RuntimeError("db down")

        assert build_own_fleet_index(Boom()) == {}


class TestIsOwnFleetPeer:
    def test_accident_account_to_account(self):
        # 今日实锤：8041810715 给自家账号 8755679833 发 ritual_morning
        idx = build_own_fleet_index(_FLEET)
        assert is_own_fleet_peer("telegram", "8041810715", "8755679833", idx) is True

    def test_accident_self_to_self(self):
        # 今日实锤：whatsapp 账号给自己发早安语音（chat_key==account_id）
        idx = build_own_fleet_index(_FLEET)
        assert is_own_fleet_peer(
            "whatsapp", "639270135480", "639270135480", idx) is True

    def test_self_to_self_without_registry(self):
        # registry 拿不到时，自己给自己仍能判出（不依赖索引）
        assert is_own_fleet_peer("telegram", "8041810715", "8041810715", {}) is True

    def test_real_customer_passes(self):
        idx = build_own_fleet_index(_FLEET)
        # 6012557795 是真实 peer（神马搜索那条 chat_key），不在自家账号集
        assert is_own_fleet_peer("telegram", "8041810715", "6012557795", idx) is False

    def test_cross_platform_isolation(self):
        # telegram 账号 id 不应命中 whatsapp 的 peer 判定
        idx = build_own_fleet_index(_FLEET)
        assert is_own_fleet_peer("whatsapp", "x", "8041810715", idx) is False

    def test_empty_chat_key(self):
        assert is_own_fleet_peer("telegram", "8041810715", "", {}) is False


_GUARD_ON = {"inbox": {"peer_bot_guard": {"enabled": True, "proactive_filter": True}}}


class TestProactiveCandidateOk:
    def test_bot_row_blocked(self):
        row = {"platform": "telegram", "account_id": "a", "chat_key": "x",
               "peer_is_bot": 1}
        ok, reason = proactive_candidate_ok(row, _GUARD_ON)
        assert ok is False and reason == "bot"

    def test_own_fleet_blocked(self):
        idx = build_own_fleet_index(_FLEET)
        row = {"platform": "telegram", "account_id": "8041810715",
               "chat_key": "8755679833"}
        ok, reason = proactive_candidate_ok(row, _GUARD_ON, own_index=idx)
        assert ok is False and reason == "own_fleet"

    def test_real_customer_ok(self):
        idx = build_own_fleet_index(_FLEET)
        row = {"platform": "telegram", "account_id": "8041810715",
               "chat_key": "6012557795", "peer_is_bot": 0}
        ok, reason = proactive_candidate_ok(row, _GUARD_ON, own_index=idx)
        assert ok is True and reason == ""

    def test_guard_disabled_still_catches_own_fleet(self):
        # 守卫关时 bot 不拦，但自嗨排除不依赖守卫开关
        idx = build_own_fleet_index(_FLEET)
        row = {"platform": "telegram", "account_id": "8041810715",
               "chat_key": "8755679833", "peer_is_bot": 1}
        ok, reason = proactive_candidate_ok(row, {}, own_index=idx)
        assert ok is False and reason == "own_fleet"


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def get_conversation(self, cid):
        return self._rows.get(cid)


class TestBuildPeerFilter:
    def test_skips_bot_conversation(self):
        store = _FakeStore({
            "telegram:a:x": {"platform": "telegram", "account_id": "a",
                             "chat_key": "x", "peer_is_bot": 1},
        })
        f = build_peer_filter(store, _GUARD_ON, registry=_FLEET)
        assert f("telegram", "a", "x") is True   # 该跳过

    def test_passes_real_customer(self):
        store = _FakeStore({
            "telegram:8041810715:6012557795": {
                "platform": "telegram", "account_id": "8041810715",
                "chat_key": "6012557795", "peer_is_bot": 0},
        })
        f = build_peer_filter(store, _GUARD_ON, registry=_FLEET)
        assert f("telegram", "8041810715", "6012557795") is False

    def test_skips_own_fleet_even_missing_row(self):
        # 会话行查不到，但 chat_key==account_id（自己给自己）仍拦
        store = _FakeStore({})
        f = build_peer_filter(store, _GUARD_ON, registry=_FLEET)
        assert f("telegram", "8041810715", "8041810715") is True

    def test_no_store_fail_open(self):
        f = build_peer_filter(None, _GUARD_ON, registry=_FLEET)
        assert f("telegram", "a", "x") is False   # 无 store 恒放行


class TestServicePeerName:
    """业务号显示名识别（P2 2026-08-04，news_share 群发事故连带发现）。"""

    def test_incident_goldens_excluded(self):
        # 事故当天真的收到「霍尔木兹」问候的两个业务号
        assert is_service_peer_name(
            "泷玥🍀全球一手实卡接码&代开会员(转账&文件语音确认）") is True
        assert is_service_peer_name("丽娜/全能业务小客服") is True

    def test_more_service_shapes(self):
        assert is_service_peer_name("XX官方客服") is True
        assert is_service_peer_name("USDT 换汇-收款秒到") is True
        assert is_service_peer_name("推广引流一条龙") is True

    def test_real_customers_pass(self):
        # 事故当天同批收到消息的真人客户——绝不误伤
        for name in ("Lao ds", "华哥", "Tom", "Mr. Baihe",
                     "小雨", "linda 美女", "དགའ་པོ་"):
            assert is_service_peer_name(name) is False, name

    def test_bad_shapes(self):
        assert is_service_peer_name("") is False
        assert is_service_peer_name(None) is False
