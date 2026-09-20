"""托管到期提醒读取端门禁（src/utils/tenant_notice.py）。

写入方（tenant_ops watch / fulfill 守护）在进程外；这里守读取端的三条安全轨：
非托管零影响（无文件=None）、陈旧数据不吓客户（written_at 超 72h=None）、
days_left 读取时点重算（越过零点自动升级 expired）。全部 tmp_path 隔离。
"""
from __future__ import annotations

import json
import time

from src.utils.tenant_notice import STALE_SEC, parse_tenant_notice, read_tenant_notice

NOW = 1_700_000_000.0


def _fmt(off_days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW + off_days * 86400))


def _payload(**over):
    base = {
        "kind": "expiry", "level": "expiring", "days_left": 2.0,
        "expires_at": _fmt(2), "written_at": int(NOW - 600),
        "renew_url": "https://bd2026.cc/order?plan=autochat-team&delivery=hosted",
    }
    base.update(over)
    return base


def test_parse_happy_path_recomputes_days():
    n = parse_tenant_notice(_payload(days_left=99.0), NOW)  # 写入快照给错也不怕
    assert n["level"] == "expiring"
    assert n["days_left"] == 2.0  # 按 expires_at 于读取时点重算
    assert n["renew_url"].startswith("https://")


def test_parse_crossing_zero_upgrades_to_expired():
    # 写入时还是 expiring，读取时已越过到期点 → 自动升级 expired（防「还剩 -0.1 天」）
    n = parse_tenant_notice(_payload(expires_at=_fmt(-0.5)), NOW)
    assert n["level"] == "expired" and n["days_left"] == -0.5


def test_parse_rejects_stale_and_garbage():
    # 写入方停摆（超 72h）→ None：宁可不显示也不拿陈旧急迫度吓客户
    assert parse_tenant_notice(
        _payload(written_at=int(NOW - STALE_SEC - 1)), NOW) is None
    assert parse_tenant_notice(_payload(written_at=0), NOW) is None
    assert parse_tenant_notice({"kind": "other"}, NOW) is None
    assert parse_tenant_notice({"kind": "expiry", "level": "weird",
                                "written_at": NOW}, NOW) is None
    assert parse_tenant_notice(None, NOW) is None
    # 非 https 的 renew_url 不透传（横幅上放的是可点链接，来源必须是我方站点）
    n = parse_tenant_notice(_payload(renew_url="javascript:alert(1)"), NOW)
    assert n["renew_url"] == ""


def test_read_missing_file_is_none(tmp_path):
    assert read_tenant_notice(now=NOW, path=tmp_path / "nope.json") is None


def test_read_roundtrip_and_bad_json(tmp_path):
    p = tmp_path / "tenant_notice.json"
    p.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    n = read_tenant_notice(now=NOW, path=p)
    assert n and n["level"] == "expiring" and n["days_left"] == 2.0
    p.write_text("{broken", encoding="utf-8")
    assert read_tenant_notice(now=NOW, path=p) is None
