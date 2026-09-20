"""src.* 命名空间日志落盘（2026-07-12 排障盲区修复）。

钉住三条语义：
- src.* 的 INFO 落进 file handler（此前 root=WARNING 全体隐身）；
- 同一记录不重复写（propagate=False，root 也挂同文件 handler 的场景）；
- 幂等：重复 attach 不叠 handler。
"""

import logging

from src.utils.log_setup import attach_src_file_handler


def _mk_handler(tmp_path, name="app.log"):
    f = tmp_path / name
    h = logging.FileHandler(str(f), encoding="utf-8")
    h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    return f, h


def _cleanup(handlers):
    src = logging.getLogger("src")
    root = logging.getLogger()
    for h in handlers:
        src.removeHandler(h)
        root.removeHandler(h)
        h.close()
    src.propagate = True
    src.setLevel(logging.NOTSET)


def test_src_info_reaches_file(tmp_path):
    f, h = _mk_handler(tmp_path)
    try:
        attach_src_file_handler(h, level=logging.INFO)
        logging.getLogger("src.utils.config_manager").info("配置热重载完成 probe")
        h.flush()
        assert "配置热重载完成 probe" in f.read_text(encoding="utf-8")
    finally:
        _cleanup([h])


def test_no_duplicate_lines_when_root_has_same_file(tmp_path):
    """root 也挂同文件 handler（main.py 现状）：src 的 WARNING 只写一行。"""
    f, h = _mk_handler(tmp_path)
    root = logging.getLogger()
    try:
        root.addHandler(h)
        attach_src_file_handler(h, level=logging.INFO)
        logging.getLogger("src.foo").warning("只此一行 probe")
        h.flush()
        assert f.read_text(encoding="utf-8").count("只此一行 probe") == 1
    finally:
        _cleanup([h])


def test_attach_idempotent(tmp_path):
    f, h = _mk_handler(tmp_path)
    try:
        attach_src_file_handler(h)
        attach_src_file_handler(h)
        src = logging.getLogger("src")
        same = [x for x in src.handlers
                if getattr(x, "baseFilename", None) == getattr(h, "baseFilename", None)]
        assert len(same) == 1
        logging.getLogger("src.bar").info("幂等 probe")
        h.flush()
        assert f.read_text(encoding="utf-8").count("幂等 probe") == 1
    finally:
        _cleanup([h])


def test_third_party_not_affected(tmp_path):
    """三方库（非 src.*）不因本修复获得 INFO 落盘（root 仍 WARNING 口径）。"""
    f, h = _mk_handler(tmp_path)
    root = logging.getLogger()
    old_level = root.level
    try:
        root.setLevel(logging.WARNING)
        root.addHandler(h)
        attach_src_file_handler(h, level=logging.INFO)
        logging.getLogger("httpx").info("三方 INFO 不该出现 probe")
        h.flush()
        assert "三方 INFO 不该出现 probe" not in f.read_text(encoding="utf-8")
    finally:
        root.setLevel(old_level)
        _cleanup([h])


# ── mirror_handlers_to_src（P1-198，2026-08-05：桌面无 file 时 console 也镜像） ──

def test_mirror_console_handler_reaches_src(tmp_path):
    """桌面形态（主 logger 只有 console/stream handler）：src.* 必须能出声。

    198 取证实锤：五个进程会话 backend.log 里编排器零日志，法证只能拉库反推。
    """
    from src.utils.log_setup import mirror_handlers_to_src
    import io
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    main_logger = logging.getLogger("ai_chat_assistant_mirror_test")
    main_logger.handlers.clear()
    main_logger.addHandler(h)
    try:
        mirror_handlers_to_src(main_logger, level=logging.INFO)
        logging.getLogger("src.integrations.account_orchestrator").info(
            "编排器出声 probe")
        h.flush()
        assert "编排器出声 probe" in buf.getvalue()
    finally:
        _cleanup([h])
        main_logger.handlers.clear()


def test_mirror_idempotent_and_dedups_with_file_attach(tmp_path):
    """镜像与 attach_src_file_handler 混用：同一 handler 对象绝不重复挂。"""
    from src.utils.log_setup import mirror_handlers_to_src
    f, h = _mk_handler(tmp_path)
    main_logger = logging.getLogger("ai_chat_assistant_mirror_test2")
    main_logger.handlers.clear()
    main_logger.addHandler(h)
    try:
        attach_src_file_handler(h, level=logging.INFO)   # 生产先走 file 补丁
        mirror_handlers_to_src(main_logger, level=logging.INFO)   # 再整体镜像
        mirror_handlers_to_src(main_logger, level=logging.INFO)   # 幂等重入
        src = logging.getLogger("src")
        assert src.handlers.count(h) == 1
        logging.getLogger("src.foo2").info("双通道去重 probe")
        h.flush()
        assert f.read_text(encoding="utf-8").count("双通道去重 probe") == 1
    finally:
        _cleanup([h])
        main_logger.handlers.clear()
