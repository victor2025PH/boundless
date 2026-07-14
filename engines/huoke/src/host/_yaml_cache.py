# -*- coding: utf-8 -*-
"""通用 YAML 文件缓存 —— 基于 mtime 的自动热加载。

动机
----
项目里多个模块（task_policy / exit_profile / audience_preset / task_param_rules）
各自实现了一份 "``_cached = None``、首次加载后永不刷新" 的缓存，导致改完 YAML 需要
重启进程或手动调 ``/reload`` 才能让新值生效（参考 2026-04-21 gate 放行配置未生效事件）。

本工具把这个模式抽出来：

* **mtime 自动热加载** —— 每次 ``get()`` 比较文件时间戳，新则重读。
* **后处理钩子** —— 比如 task_policy 需要在读到 YAML 后合并默认值、推导 disable_*。
* **零侵入** —— 旧模块公开 API 不变（``load_xxx(force_reload=False)``），只是内部改为代理到本类。

用法示例
--------
::

    from src.host._yaml_cache import YamlCache

    _CACHE = YamlCache(
        path=_POLICY_PATH,
        defaults={"version": 1, "items": []},
        post_process=_merge_defaults,  # 可选
        log_label="task_execution_policy",
    )

    def load_xxx(force_reload: bool = False):
        return _CACHE.get(force_reload=force_reload)

    def reload_xxx():
        return _CACHE.reload()

线程安全
--------
读/写 ``_data`` 和 ``_mtime`` 都是单个 Python 对象的赋值，依赖 GIL 保证原子性。
不加锁以保持与原模块完全一致的行为（高频 hot path，加锁会引入不必要的开销）。
多线程并发 ``get()`` 最坏情况是同一秒内重复读盘 N 次，结果一致、幂等，无损坏风险。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

import yaml

_logger = logging.getLogger(__name__)

_PostProcess = Callable[[Any], Any]


class YamlCache:
    """单个 YAML 文件的 mtime 热加载缓存。

    Parameters
    ----------
    path:
        目标 YAML 文件的绝对路径。
    defaults:
        文件缺失 / 解析失败 / 内容为空时返回的默认结构。
        使用时会 ``copy.deepcopy`` 防止调用方误改污染缓存。
    post_process:
        可选回调 ``fn(data) -> data``：在原始 ``yaml.safe_load`` 后调用，
        用于补默认值、归一化结构、打印摘要日志等。若返回 ``None`` 按 ``{}`` 处理。
    log_label:
        日志里展示的简称（默认取文件名）。
    logger:
        可注入自定义 logger；不传则用本模块 logger。
    """

    __slots__ = ("_path", "_defaults", "_post_process", "_log_label", "_logger",
                 "_data", "_mtime")

    def __init__(
        self,
        path: Union[str, Path],
        defaults: Any = None,
        post_process: Optional[_PostProcess] = None,
        log_label: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._path = Path(path)
        self._defaults = defaults if defaults is not None else {}
        self._post_process = post_process
        self._log_label = log_label or self._path.name
        self._logger = logger or _logger
        self._data: Any = None
        self._mtime: float = 0.0

    # -- 对外 API ---------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def mtime(self) -> float:
        """返回当前 YAML 文件的 mtime（秒，float）；文件不存在时返回 0.0。"""
        return self._file_mtime()

    def get(self, force_reload: bool = False) -> Any:
        """取最新数据。若文件 mtime 比缓存新，自动重读。"""
        cur_mtime = self._file_mtime()
        stale = self._data is not None and cur_mtime > self._mtime
        if stale:
            self._logger.info(
                "%s 已变更（mtime %.3f → %.3f），自动热加载",
                self._log_label, self._mtime, cur_mtime,
            )
        if self._data is not None and not force_reload and not stale:
            return self._data
        self._data = self._load(cur_mtime)
        self._mtime = cur_mtime
        return self._data

    def reload(self) -> Any:
        """强制清缓存并重读。等价于 ``get(force_reload=True)``，但日志更明确。"""
        self._logger.info("%s 被强制 reload", self._log_label)
        self._data = None
        self._mtime = 0.0
        return self.get(force_reload=True)

    def is_loaded(self) -> bool:
        return self._data is not None

    # -- 内部 -------------------------------------------------------------

    def _file_mtime(self) -> float:
        try:
            return self._path.stat().st_mtime if self._path.exists() else 0.0
        except OSError:
            return 0.0

    def _load(self, cur_mtime: float) -> Any:
        if not self._path.exists():
            self._logger.info("未找到 %s（%s），使用默认值", self._path, self._log_label)
            return self._apply_post_process(copy.deepcopy(self._defaults))

        try:
            with open(self._path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if raw is None:
                raw = copy.deepcopy(self._defaults)
        except Exception as e:
            self._logger.warning(
                "%s 读取失败，回退到默认值: %s", self._log_label, e,
            )
            return self._apply_post_process(copy.deepcopy(self._defaults))

        return self._apply_post_process(raw)

    def _apply_post_process(self, data: Any) -> Any:
        if self._post_process is None:
            return data
        try:
            out = self._post_process(data)
        except Exception as e:
            self._logger.exception("%s post_process 抛错，返回原始数据: %s", self._log_label, e)
            return data
        return data if out is None else out
