# -*- coding: utf-8 -*-
"""alert_templates / 去重指纹 冒烟测试"""

from src.host.alert_templates import dedup_fingerprint, render_alert_pair


def test_render_phone_offline():
    zh, en = render_alert_pair(
        "PHONE_OFFLINE",
        {"host_tag": "[主控] ", "display": "D1", "n": 3},
    )
    assert "主控" in zh
    assert "连续第3次" in zh
    assert "streak 3" in en
    assert "Coordinator" not in en  # host_tag 保持原样


def test_render_predictive():
    zh, en = render_alert_pair(
        "DEVICE_PREDICTIVE_HIGH",
        {
            "reasons": "10分钟内掉线2次",
            "reasons_en": "2 disconnect(s) within 10 min",
            "score": 55,
        },
    )
    assert "55" in zh and "55" in en
    assert "风险分" in zh


def test_dedup_stable():
    a = dedup_fingerprint("warning", "did", "CODE", {"n": 1}, "")
    b = dedup_fingerprint("warning", "did", "CODE", {"n": 1}, "")
    assert a == b


def test_cluster_worker_drop():
    zh, en = render_alert_pair(
        "CLUSTER_WORKER_ONLINE_DROP",
        {
            "host": "W03",
            "adb_o": "5",
            "adb_n": "3",
            "reg_o": "5",
            "reg_n": "3",
        },
    )
    assert "W03" in zh and "5" in zh
    assert "ADB online" in en


def test_vpn_health():
    zh, en = render_alert_pair(
        "VPN_HEALTH",
        {"text": "测试消息", "text_en": "Test msg"},
    )
    assert zh.startswith("[VPN]")
    assert en.startswith("[VPN]")
