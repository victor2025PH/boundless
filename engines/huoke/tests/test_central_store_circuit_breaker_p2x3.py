"""P2.X-3 hotfix (2026-04-30): central_customer_store circuit breaker.

真实日志症状:
  10:41:26 ERROR [central_store] PG pool init failed: 'utf-8' codec can't
                  decode byte 0xd6 in position 55: invalid continuation byte
  10:41:26 WARNING [central_store] init/get failed (will reset+retry): ...
  10:41:26 ERROR [central_store] reset+retry also failed: ...
  ... (每秒数十次重复) ...

PG 在 zh_CN.GBK locale 下返认证错误消息 → psycopg2 client_encoding=utf8
解码失败 → init 抛 UnicodeDecodeError. 由于无 circuit breaker, 每次 API
调用都重试 init+reset+retry 共 2 次 → 4 设备并发 callback 把 CPU 烧爆 +
日志爆炸 (152KB/2min). host 进程在 ~3 分钟内退化无响应被外层认定 down.

修复: 在 `get_store()` 内加 circuit breaker:
  - 连续 N=3 次 init 失败 → 进入 OPEN 状态, 后续调用 60s 内直接 raise 上次
    缓存的 exception, 不再 attempt PG.
  - 60s 后 HALF_OPEN, 允许一次试探性 init.
  - 成功 → CLOSED (清计数). 仍失败 → 继续 OPEN 60s.
"""
from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

# CI 环境无 psycopg2 时 ``central_customer_store`` 顶层 import 会 ModuleNotFoundError,
# 跟原 ``test_central_customer_store.py`` 一致地走 importorskip 跳过整个文件
# (不影响其他 P2.X-3 相关 contract 在装了 psycopg2 的环境上跑).
pytest.importorskip("psycopg2")


def _reset_breaker(ccs):
    ccs._init_breaker_state.update({
        "consecutive_failures": 0,
        "last_failure_at": 0.0,
        "last_exception": None,
        "open_until": 0.0,
    })


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个 test 前后清干净 module-level 单例 / 熔断器状态 (保留 key 结构)."""
    from src.host import central_customer_store as ccs
    ccs._store_singleton = None
    _reset_breaker(ccs)
    yield
    ccs._store_singleton = None
    _reset_breaker(ccs)


def test_init_failure_first_3_calls_attempt_real_init():
    """前 3 次 init 失败必须真的尝试连接 PG (不要立刻熔断)."""
    from src.host import central_customer_store as ccs

    with patch.object(ccs, "CentralCustomerStore",
                       side_effect=UnicodeDecodeError(
                           "utf-8", b"\xd6", 55, 56, "invalid continuation byte"
                       )) as mock_ctor:
        for _ in range(3):
            with pytest.raises(UnicodeDecodeError):
                ccs.get_store()

    assert mock_ctor.call_count == 3, (
        f"前 3 次必须真的 attempt init, got {mock_ctor.call_count}"
    )


def test_init_failure_4th_call_short_circuits_without_attempt():
    """第 4 次起进入熔断 OPEN, 不再 attempt PG, 直接 raise 缓存的 exception."""
    from src.host import central_customer_store as ccs

    err = UnicodeDecodeError("utf-8", b"\xd6", 55, 56, "invalid continuation byte")
    with patch.object(ccs, "CentralCustomerStore", side_effect=err) as mock_ctor:
        # 触发 3 次失败 → 熔断 OPEN
        for _ in range(3):
            with pytest.raises(UnicodeDecodeError):
                ccs.get_store()
        attempts_after_3 = mock_ctor.call_count

        # 第 4-10 次应短路, 不增 attempt 计数
        for _ in range(7):
            with pytest.raises(Exception):
                ccs.get_store()

        assert mock_ctor.call_count == attempts_after_3, (
            f"熔断 OPEN 期间不应再 attempt init, got {mock_ctor.call_count}"
        )


def test_init_failure_short_circuit_raises_cached_exception_type():
    """熔断 OPEN 时短路的异常类型应保留原始 (UnicodeDecodeError)."""
    from src.host import central_customer_store as ccs

    err = UnicodeDecodeError("utf-8", b"\xd6", 55, 56, "invalid continuation byte")
    with patch.object(ccs, "CentralCustomerStore", side_effect=err):
        for _ in range(3):
            try:
                ccs.get_store()
            except Exception:
                pass

        with pytest.raises(UnicodeDecodeError) as exc_info:
            ccs.get_store()
        assert "circuit_open" in str(exc_info.value).lower() or \
               "0xd6" in str(exc_info.value).lower() or \
               isinstance(exc_info.value, UnicodeDecodeError)


def test_init_after_breaker_window_attempts_again():
    """熔断超时后 (HALF_OPEN) 应再尝试一次 init."""
    from src.host import central_customer_store as ccs

    err = UnicodeDecodeError("utf-8", b"\xd6", 55, 56, "invalid continuation byte")
    with patch.object(ccs, "CentralCustomerStore", side_effect=err) as mock_ctor:
        for _ in range(3):
            with pytest.raises(Exception):
                ccs.get_store()
        breaker_attempts = mock_ctor.call_count

        # 模拟熔断窗口已过 (HALF_OPEN): 直接把 open_until 设回过去
        ccs._init_breaker_state["open_until"] = 0.0
        with pytest.raises(Exception):
            ccs.get_store()

        # HALF_OPEN 试探性 init 必须真的发生
        assert mock_ctor.call_count == breaker_attempts + 1, (
            "HALF_OPEN 应放行一次试探性 init"
        )


def test_init_success_resets_breaker():
    """init 成功后熔断器复位, 后续不再短路."""
    from src.host import central_customer_store as ccs

    fake_store = MagicMock()
    err = UnicodeDecodeError("utf-8", b"\xd6", 55, 56, "invalid continuation byte")
    side_effects = [err, err, fake_store]   # 前 2 失败, 第 3 次成功

    with patch.object(ccs, "CentralCustomerStore",
                       side_effect=side_effects) as mock_ctor:
        for _ in range(2):
            with pytest.raises(Exception):
                ccs.get_store()

        # 第 3 次成功
        result = ccs.get_store()
        assert result is fake_store

        # 单例已建立, 后续调用不再 attempt init
        result2 = ccs.get_store()
        assert result2 is fake_store
        assert mock_ctor.call_count == 3, (
            "成功后应直接复用单例, 不再 attempt"
        )


def test_dsn_includes_lc_messages_C_for_safer_decoding():
    """DSN 应含 options='-c lc_messages=C' 让 PG 错误消息走 C locale (ASCII).

    这样万一 PG 仍返中文错误, 至少不会触发 utf-8 decode crash.
    """
    from src.host.central_customer_store import CentralCustomerStore

    with patch("src.host.central_customer_store.ThreadedConnectionPool") as mock_pool:
        try:
            CentralCustomerStore(
                host="127.0.0.1", port=5432,
                dbname="x", user="x", password="x",
                pool_min=1, pool_max=2,
            )
        except Exception:
            pass

        # ThreadedConnectionPool 被 dsn= kwarg 调用
        call = mock_pool.call_args
        if call is None:
            pytest.skip("pool not called")
        dsn = call.kwargs.get("dsn", "") or (call.args[2] if len(call.args) >= 3 else "")
        assert "lc_messages" in dsn, (
            f"DSN 应注入 lc_messages=C, got: {dsn!r}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
