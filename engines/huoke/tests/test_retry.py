# -*- coding: utf-8 -*-
"""retry / timeout 工具单元测试。"""

import pytest
import time

from src.utils.retry import retry, run_with_timeout, TaskTimeout


class TestRetry:

    def test_succeeds_first_try(self):
        call_count = [0]

        @retry(max_attempts=3, delay=0.01)
        def ok():
            call_count[0] += 1
            return "done"

        assert ok() == "done"
        assert call_count[0] == 1

    def test_retries_then_succeeds(self):
        call_count = [0]

        @retry(max_attempts=3, delay=0.01, exceptions=(ValueError,))
        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("not yet")
            return "ok"

        assert flaky() == "ok"
        assert call_count[0] == 3

    def test_exhausts_retries(self):
        @retry(max_attempts=2, delay=0.01, exceptions=(RuntimeError,))
        def always_fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            always_fail()

    def test_on_retry_callback(self):
        attempts_seen = []

        @retry(max_attempts=3, delay=0.01, exceptions=(ValueError,),
               on_retry=lambda a, e: attempts_seen.append(a))
        def flaky():
            if len(attempts_seen) < 2:
                raise ValueError("fail")
            return "ok"

        flaky()
        assert attempts_seen == [1, 2]

    def test_does_not_retry_wrong_exception(self):
        @retry(max_attempts=3, delay=0.01, exceptions=(ValueError,))
        def wrong_exc():
            raise TypeError("wrong")

        with pytest.raises(TypeError):
            wrong_exc()


class TestRunWithTimeout:

    def test_completes_in_time(self):
        result = run_with_timeout(lambda: 42, 2.0)
        assert result == 42

    def test_timeout_raises(self):
        def slow():
            time.sleep(10)

        with pytest.raises(TaskTimeout):
            run_with_timeout(slow, 0.3)

    def test_propagates_exception(self):
        def bad():
            raise ValueError("oops")

        with pytest.raises(ValueError, match="oops"):
            run_with_timeout(bad, 2.0)
