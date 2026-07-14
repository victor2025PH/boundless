# -*- coding: utf-8 -*-
"""轻量重试与超时工具。零外部依赖。"""

import functools
import logging
import time
import threading
from typing import Callable, Tuple, Type

logger = logging.getLogger(__name__)


class TaskTimeout(Exception):
    """任务执行超时"""


def retry(max_attempts: int = 3,
          delay: float = 1.0,
          backoff: float = 2.0,
          exceptions: Tuple[Type[BaseException], ...] = (Exception,),
          on_retry: Callable = None):
    """
    纯 Python 重试装饰器，指数退避。

    max_attempts: 最大尝试次数（含首次）
    delay:        首次重试前等待秒数
    backoff:      退避乘数
    exceptions:   触发重试的异常类型
    on_retry:     每次重试前的回调 fn(attempt, exc)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            wait = delay
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        logger.error("[retry] %s 已达最大重试次数 %d: %s",
                                     func.__name__, max_attempts, e)
                        raise
                    logger.warning("[retry] %s attempt %d/%d failed: %s — %.1fs 后重试",
                                   func.__name__, attempt, max_attempts, e, wait)
                    if on_retry:
                        on_retry(attempt, e)
                    time.sleep(wait)
                    wait *= backoff
            raise last_exc  # unreachable, but makes type checkers happy
        return wrapper
    return decorator


def run_with_timeout(func: Callable, timeout_sec: float, *args, **kwargs):
    """
    在独立线程中执行 func，超时则抛 TaskTimeout。
    注意：超时后线程仍在运行（Python 无法安全杀线程），
    但调用方可以继续而不阻塞。
    """
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        raise TaskTimeout(f"{func.__name__} 执行超过 {timeout_sec}s 超时")
    if error[0]:
        raise error[0]
    return result[0]
