"""P2.X 后续: consume_last_extract_error 模块状态正确性 + executor outcome 三段映射"""
from __future__ import annotations

import time

import pytest


def test_record_and_consume_returns_step():
    from src.app_automation import facebook as fb
    fb._record_extract_error("dev1", "enter_group_failed")
    assert fb.consume_last_extract_error("dev1") == "enter_group_failed"
    # consume 后应清空, 第二次返 None
    assert fb.consume_last_extract_error("dev1") is None


def test_consume_returns_none_when_no_error():
    from src.app_automation import facebook as fb
    # 确保 dev2 未被记录
    fb._LAST_EXTRACT_ERROR.pop("dev2", None)
    assert fb.consume_last_extract_error("dev2") is None


def test_consume_returns_none_when_ttl_expired(monkeypatch):
    from src.app_automation import facebook as fb
    # 写一条 65 秒前的记录 (超过 60s TTL)
    fb._LAST_EXTRACT_ERROR["dev3"] = ("zero_after_enter", time.time() - 65)
    assert fb.consume_last_extract_error("dev3") is None
    # 即便如此, pop 已发生, 不会重复消费
    assert "dev3" not in fb._LAST_EXTRACT_ERROR


def test_consume_handles_empty_device_id():
    from src.app_automation import facebook as fb
    assert fb.consume_last_extract_error("") is None
    assert fb.consume_last_extract_error(None) is None


def test_record_and_consume_isolation_between_devices():
    """两个设备各自记录, 不应互相干扰"""
    from src.app_automation import facebook as fb
    fb._record_extract_error("devA", "enter_group_failed")
    fb._record_extract_error("devB", "members_tab_not_found")
    assert fb.consume_last_extract_error("devA") == "enter_group_failed"
    assert fb.consume_last_extract_error("devB") == "members_tab_not_found"


def test_outcome_map_in_executor_covers_all_steps():
    """executor 端 _outcome_map 必须覆盖所有 record 调用点的 step 名"""
    from src.host import executor
    src = open(executor.__file__, encoding="utf-8").read()
    # 三个步骤名都应出现在 executor.py 里 (作为 dict key)
    for key in ("enter_group_failed", "members_tab_not_found", "zero_after_enter"):
        assert f'"{key}"' in src, f"executor _outcome_map 缺 step={key}"
    # 三个 outcome 字符串都应出现
    for oc in ("automation_enter_group_failed",
               "automation_members_tab_not_found",
               "automation_extract_zero_after_enter"):
        assert oc in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
