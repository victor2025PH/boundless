# -*- coding: utf-8 -*-
"""桥接驱动（PC 副驾）心跳 → 工作台在线态（实施97 线 B 第三轮）。纯函数 + 心跳载荷收敛。"""
from src.web.desktop_bridge_presence import (
    ALIVE_WITHIN_SEC, FAIL_AFTER_SEC, bridge_driver_problems, bridge_presence, heartbeat_meta,
)


def _row(account_id, hb_ts, *, status="online", mode="desktop", label="", tier="semi"):
    meta = {"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "tier": tier}, now=hb_ts)} if hb_ts else {}
    return {"platform": "wechat", "account_id": account_id, "mode": mode, "status": status,
            "label": label, "meta": meta}


def test_bridge_driver_problems_severity_by_tier_and_exclusions():
    now = 10_000.0
    rows = [
        _row("alive", now - 30, label="个人微信 · PC 副驾"),                      # 新鲜 → 不报
        _row("warn", now - 200, label="副驾B", tier="copilot"),                   # 只读档过期 200s → warn
        _row("dead", now - FAIL_AFTER_SEC - 5, label="副驾C", tier="copilot"),    # 只读档过期 >10min → fail
        _row("semi", now - 120, label="副驾D", tier="semi"),                       # 半自动档过期 2min → 直接 fail
        _row("loggedout", now - 5000, status="offline"),                          # 运营登出 → 不报
        _row("removed", now - 5000, status="removed"),                            # 运营移除 → 不报
        _row("shell", 0),                                                         # 普通桌面壳镜像（无心跳）→ 不报
        {"platform": "telegram", "account_id": "p1", "mode": "protocol", "status": "online", "meta": {}},
    ]
    probs = bridge_driver_problems(rows, now=now)
    assert [p["id"] for p in probs] == ["wechat:warn", "wechat:dead", "wechat:semi"]
    w, d, s = probs
    assert w["status"] == "warn" and w["kind"] == "bridge_offline" and w["age_sec"] == 200
    assert w["name"] == "副驾B（wechat/warn）" and "3 分钟无心跳" in w["detail"] and "只读建议" in w["detail"]
    assert d["status"] == "fail" and d["restarts"] == 0 and d["platform"] == "wechat" and d["account_id"] == "dead"
    assert s["status"] == "fail" and "半自动" in s["detail"], "该发消息的档位断了＝业务中断，立刻红"
    assert bridge_driver_problems([], now=now) == [] and bridge_driver_problems([{"bad": 1}], now=now) == []


def test_heartbeat_meta_is_compact_and_validated():
    m = heartbeat_meta({"bridge": "pcui", "tier": "semi", "readonly": False,
                        "stats": {"ticks": 12, "inbound": 3, "sent": 1, "secret_blob": {"x": 1},
                                  "last_disposition": "none", "offline": False}}, now=1000.0)
    assert m["ts"] == 1000.0 and m["kind"] == "pcui" and m["tier"] == "semi" and m["readonly"] is False
    assert m["stats"] == {"ticks": 12, "inbound": 3, "sent": 1, "last_disposition": "none", "offline": False}
    assert "secret_blob" not in m["stats"], "只留展示要用的字段"
    # 非法档位回落 copilot；缺 bridge 回落 desktop
    m2 = heartbeat_meta({"tier": "root", "readonly": "yes"}, now=1.0)
    assert m2["tier"] == "copilot" and m2["kind"] == "desktop" and m2["readonly"] is True


def test_bridge_presence_carries_readable_bit():
    """进程活着 ≠ 能干活：微信收进托盘时驱动仍心跳但 last_readable=False → presence.readable=False（账号卡显琥珀）。"""
    meta = {"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "tier": "semi",
                                               "stats": {"ticks": 3, "last_readable": False}}, now=1000.0)}
    p = bridge_presence(meta, now=1005.0)
    assert p["alive"] is True and p["readable"] is False and p["stats"]["last_readable"] is False
    meta_ok = {"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "stats": {"last_readable": True}}, now=1000.0)}
    assert bridge_presence(meta_ok, now=1001.0)["readable"] is True
    # 老驱动没报这一位 → None（前端按「在线」处理，不误报琥珀）
    meta_old = {"bridge_heartbeat": heartbeat_meta({"bridge": "pcui"}, now=1000.0)}
    assert bridge_presence(meta_old, now=1001.0)["readable"] is None


def test_bridge_presence_flags_multi_wechat_without_binding():
    """桌面上两个微信主窗而驱动没绑 → 工作台该提醒主人绑窗；绑了 / 单开 → 不提醒。"""
    def _p(st):
        return bridge_presence({"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "stats": st}, now=1000.0)}, now=1001.0)
    p = _p({"window_hwnd": 1001, "window_pid": 501, "window_bound": False, "main_windows": 2})
    assert p["window_pid"] == 501 and p["main_windows"] == 2 and p["window_bound"] is False
    assert p["multi_wechat_unbound"] is True
    assert _p({"window_pid": 501, "window_bound": True, "main_windows": 2})["multi_wechat_unbound"] is False
    assert _p({"window_pid": 501, "window_bound": False, "main_windows": 1})["multi_wechat_unbound"] is False
    old = _p({})
    assert old["window_pid"] == 0 and old["main_windows"] == 0 and old["multi_wechat_unbound"] is False


def test_bridge_presence_carries_freeze_reason_and_identity():
    """「副驾在线但不回」要能说成人话：冻结原因 / 剩余秒数 / 会话级冻结数；双开时靠昵称/微信号分账号。"""
    st = {"frozen_until": 1600.0, "freeze_reason": "logged_out", "chat_frozen": 2,
          "account_nick": "小北", "account_wxid": "xb_2020"}
    meta = {"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "tier": "auto_reply", "stats": st}, now=1000.0)}
    p = bridge_presence(meta, now=1000.0)
    assert p["frozen"] is True and p["freeze_reason"] == "logged_out" and p["freeze_remaining_sec"] == 600
    assert p["chat_frozen"] == 2 and p["account_nick"] == "小北" and p["account_wxid"] == "xb_2020"
    # 冻结到期 → 原因清空（老心跳里残留的 reason 不再展示）
    p2 = bridge_presence(meta, now=1700.0)
    assert p2["frozen"] is False and p2["freeze_reason"] == "" and p2["freeze_remaining_sec"] == 0
    old = bridge_presence({"bridge_heartbeat": heartbeat_meta({"bridge": "pcui"}, now=1000.0)}, now=1001.0)
    assert old["frozen"] is False and old["chat_frozen"] == 0 and old["account_nick"] == ""
    assert old["account_bound_wxid"] == "" and old["account_mismatch"] is False
    # 窗里登的不是绑定的号 → 工作台要能指出来（并说明发送已冻结）
    st2 = {"account_wxid": "xn_1999", "account_bound_wxid": "xb_2020", "account_mismatch": True,
           "frozen_until": 4600.0, "freeze_reason": "account_mismatch"}
    p3 = bridge_presence({"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "stats": st2}, now=1000.0)}, now=1000.0)
    assert p3["account_mismatch"] is True and p3["account_bound_wxid"] == "xb_2020" and p3["freeze_reason"] == "account_mismatch"


def test_bridge_presence_alive_window():
    meta = {"bridge_heartbeat": heartbeat_meta({"bridge": "pcui", "tier": "semi"}, now=1000.0)}
    fresh = bridge_presence(meta, now=1000.0 + 30)
    assert fresh and fresh["alive"] is True and fresh["age_sec"] == 30 and fresh["tier"] == "semi"
    edge = bridge_presence(meta, now=1000.0 + ALIVE_WITHIN_SEC)
    assert edge and edge["alive"] is True
    stale = bridge_presence(meta, now=1000.0 + ALIVE_WITHIN_SEC + 1)
    assert stale and stale["alive"] is False and stale["age_sec"] == int(ALIVE_WITHIN_SEC + 1)
    # 普通桌面壳镜像账号（无心跳记录）→ None；坏数据 → None
    assert bridge_presence({}) is None and bridge_presence(None) is None
    assert bridge_presence({"bridge_heartbeat": {"ts": "abc"}}) is None
    assert bridge_presence({"bridge_heartbeat": "junk"}) is None
