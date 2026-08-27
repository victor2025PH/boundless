"""轮转容忍日志处理器门禁（2026-08-27 06:11 宕机事故沉淀）。

事故：外部 tail 工具长持 app.log 句柄 → RotatingFileHandler 轮转 rename 永败 →
每条日志经 handleError 向 stderr 倒整段堆栈 → 45 分钟 1.5GB 拖死实例。
本文件钉住 ``ResilientRotatingFileHandler`` 的三层容忍语义：

① 正常路径＝原生轮转（行为零变化）；
② rename 被占 → copytruncate 降级（.1 拿到快照、base 就地截断、不换 inode）；
③ 连截断都失败 → 冷却窗内 shouldRollover 返 0（绝不逐条重试/逐条报错）；
④ 全程绝不向 emit 调用方抛异常；仍是 RotatingFileHandler 子类
   （logging_setup 的 root 去重 isinstance 检查依赖它）。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from src.utils.resilient_logging import ResilientRotatingFileHandler

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _mk_logger(tmp_path, name, **kw):
    log_file = tmp_path / "app.log"
    handler = ResilientRotatingFileHandler(
        str(log_file), maxBytes=kw.get("max_bytes", 400), backupCount=3,
        encoding="utf-8")
    lg = logging.getLogger(f"test_resilient_{name}")
    lg.handlers[:] = [handler]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, handler, log_file


def test_normal_rotation_unchanged(tmp_path):
    lg, handler, log_file = _mk_logger(tmp_path, "normal")
    for i in range(30):
        lg.info("x" * 60)
    handler.close()
    assert log_file.exists()
    assert (tmp_path / "app.log.1").exists(), "正常路径必须照旧产出 .1 备份"


def test_rename_blocked_falls_back_to_copytruncate(tmp_path, monkeypatch):
    """句柄占用（rename 抛 PermissionError）→ .1 拿到快照、base 截断、不抛异常。"""
    lg, handler, log_file = _mk_logger(tmp_path, "copytrunc")

    def _deny(*_a, **_k):
        raise PermissionError(32, "held by another process")

    # 只拦 logging.handlers 命名空间的 rename/replace（rename 链的全部路径），
    # 本模块 fallback 用自己的 os/shutil 引用不受影响——与真实占用的语义一致。
    monkeypatch.setattr(logging.handlers.os, "rename", _deny)
    monkeypatch.setattr(logging.handlers.os, "replace", _deny, raising=False)

    for i in range(30):
        lg.info("y" * 60)          # 任何一条都不得抛
    backup = tmp_path / "app.log.1"
    assert backup.exists(), "copytruncate 降级必须产出 .1 快照"
    assert backup.stat().st_size > 0
    # base 被就地截断过：末态体量应远小于 30 条全量
    assert log_file.stat().st_size < 30 * 60
    # 降级后日志继续可写（最后一条应落在 base）
    lg.info("tail-after-fallback")
    handler.close()
    assert "tail-after-fallback" in log_file.read_text(encoding="utf-8")


def test_total_failure_enters_cooldown_not_storm(tmp_path, monkeypatch):
    """rename+copytruncate+截断全败 → 冷却窗内不再尝试轮转（风暴结构性不可能）。"""
    import src.utils.resilient_logging as rl

    lg, handler, log_file = _mk_logger(tmp_path, "cooldown")

    def _deny(*_a, **_k):
        raise PermissionError(32, "held")

    monkeypatch.setattr(logging.handlers.os, "rename", _deny)
    monkeypatch.setattr(logging.handlers.os, "replace", _deny, raising=False)
    monkeypatch.setattr(rl.shutil, "copyfile", _deny)

    real_open = open
    calls = {"n": 0}

    def _open_deny_truncate(file, mode="r", *a, **kw):
        if str(file) == str(log_file) and "w" in str(mode):
            raise PermissionError(32, "held")
        return real_open(file, mode, *a, **kw)

    # 模块级 open 覆写只影响 resilient_logging 内部的截断调用
    monkeypatch.setattr(rl, "open", _open_deny_truncate, raising=False)

    rollover_attempts = {"n": 0}
    orig_do = logging.handlers.RotatingFileHandler.doRollover

    def _counting_do(self):
        rollover_attempts["n"] += 1
        return orig_do(self)

    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "doRollover",
                        _counting_do)

    for i in range(40):
        lg.info("z" * 60)          # 全程不抛
    handler.close()
    # 首次失败进入冷却后，剩余 emit 不得再逐条尝试轮转
    assert rollover_attempts["n"] <= 2, (
        f"冷却未生效：40 条 emit 触发了 {rollover_attempts['n']} 次轮转尝试")
    assert handler._retry_after > 0


def test_is_rotating_subclass_and_wired_into_logging_setup():
    """isinstance 契约（root 去重依赖）+ logging_setup 接线 ratchet。"""
    assert issubclass(ResilientRotatingFileHandler,
                      logging.handlers.RotatingFileHandler)
    src = (_ENGINE_ROOT / "src" / "bootstrap"
           / "logging_setup.py").read_text(encoding="utf-8")
    assert "ResilientRotatingFileHandler(" in src, (
        "logging_setup 退回原生 RotatingFileHandler？句柄占用风暴防线被拆")
