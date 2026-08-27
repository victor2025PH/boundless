# -*- coding: utf-8 -*-
"""目标完成通知链冒烟工具门禁（P4 2026-08-18，tools/smoke_goal_notify.py）。

钉住：verdict 三档语义（BROKEN=配置级缺口重启也没用 / RIDES_RESTART=只差装载 /
READY=链路通，提示级原因不降档）、渠道选择与订阅判定、collect 的文件真相读取
（tmp 根密闭，绝不碰生产）。--push 真发不在此测（人工工具语义）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from tools.smoke_goal_notify import (   # noqa: E402
    channel_covers_goal,
    collect,
    pick_channel,
    verdict,
)

_GOOD = {
    "goals_enabled": True, "notify_enabled": True, "push_agent": True,
    "channel_present": True, "goal_subscribed": True, "bindings": 1,
    "http_new_routes": "loaded",
}


def test_verdict_ready_and_informational_reasons_do_not_downgrade():
    status, reasons = verdict(dict(_GOOD))
    assert status == "READY" and reasons == []
    # push_agent 关 / 零绑定＝提示不降档（管理员渠道推送不依赖这两项）
    status2, reasons2 = verdict(dict(_GOOD, push_agent=False, bindings=0))
    assert status2 == "READY" and len(reasons2) == 2


def test_verdict_rides_restart_only_when_config_truth_ready():
    status, reasons = verdict(dict(_GOOD, http_new_routes="missing"))
    assert status == "RIDES_RESTART"
    assert any("重启窗" in r for r in reasons)
    # 配置缺口优先于装载判定：坏配置+404 仍是 BROKEN（重启救不了）
    status2, _ = verdict(dict(_GOOD, notify_enabled=False,
                              http_new_routes="missing"))
    assert status2 == "BROKEN"


def test_verdict_broken_reasons_enumerated():
    status, reasons = verdict({
        "goals_enabled": True, "notify_enabled": False,
        "channel_present": False, "goal_subscribed": False,
        "bindings": 0, "http_new_routes": "loaded"})
    assert status == "BROKEN" and len(reasons) == 3


def test_verdict_disabled_is_choice_not_failure():
    """整域关闸（试点/隔离实例常态）＝DISABLED 单列，绝不算 BROKEN——
    否则 pilot 实例永远把冒烟退出码打红，验证人会对着不相干的根修配置。"""
    status, reasons = verdict({
        "goals_enabled": False, "notify_enabled": False,
        "channel_present": False, "goal_subscribed": False,
        "bindings": 0, "http_new_routes": "loaded"})
    assert status == "DISABLED" and len(reasons) == 1


def test_verdict_partial_routes_ready_with_note():
    """半装载（早批 live、本批增量待窗）＝READY + 解释行——P0-P2 链已可用，
    不该被读成故障，也不该无解释装满绿。"""
    status, reasons = verdict(dict(_GOOD, http_new_routes="partial"))
    assert status == "READY"
    assert any("半装载" in r for r in reasons)


def test_channel_pick_and_subscription_semantics():
    hooks = [
        {"name": "off", "format": "telegram", "token": "T1", "enabled": False,
         "events": ["goal_complete"]},
        {"name": "json-hook", "format": "json", "url": "http://x",
         "events": ["all"]},
        {"name": "tg", "format": "telegram", "token": "T2",
         "events": ["host_alert"]},
    ]
    # 停用/非 telegram 跳过 → 选 tg
    assert pick_channel(hooks)["name"] == "tg"
    # 订阅判定只认启用渠道：json-hook 的 all 通配也算覆盖（notifier 同语义）
    assert channel_covers_goal(hooks) is True
    assert channel_covers_goal([hooks[0], hooks[2]]) is False   # off 不算、tg 没订
    assert pick_channel([]) is None and pick_channel(None) is None


def test_collect_reads_tmp_root_file_truth(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "companion:\n  goals:\n    enabled: false\n", encoding="utf-8")
    (cfg_dir / "config.local.yaml").write_text(
        "companion:\n  goals:\n    enabled: true\n"
        "    notify:\n      enabled: true\n      push_agent: true\n",
        encoding="utf-8")
    (cfg_dir / "notify_webhooks.json").write_text(json.dumps([
        {"name": "tg-ops", "format": "telegram", "token": "T",
         "target": "111", "enabled": True,
         "events": ["host_alert", "goal_complete"]},
    ]), encoding="utf-8")
    from src.utils.web_user_store import WebUserStore
    store = WebUserStore(cfg_dir / "web_users.db")
    u = store.create_user("smokeagent", "pass123456", "agent")
    store.update_user(u["id"], notify_tg_chat_id="5433982810")
    checks = collect(tmp_path, base_url="")     # 空 base=跳过 HTTP（密闭）
    assert checks["goals_enabled"] is True      # overlay 深合并生效
    assert checks["notify_enabled"] is True and checks["push_agent"] is True
    assert checks["channel_present"] is True and checks["goal_subscribed"] is True
    assert checks["bindings"] == 1
    assert checks["binding_users"] == [
        {"username": "smokeagent", "chat_tail": "2810"}]
    assert checks["http_new_routes"] == "skipped"
    status, _ = verdict(checks)
    assert status == "READY"


def test_collect_missing_files_soft(tmp_path):
    (tmp_path / "config").mkdir()
    checks = collect(tmp_path, base_url="")
    assert checks["bindings"] == 0 and checks["channel_present"] is False
    # 空根＝goals 未开 → DISABLED（选择语义；BROKEN 只留给「开了却不通」）
    status, _ = verdict(checks)
    assert status == "DISABLED"
