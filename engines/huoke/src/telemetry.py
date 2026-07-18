# -*- coding: utf-8 -*-
r"""telemetry.py — 智拓(zhituo)全域运营事件适配器（P4 埋点，仅标准库）。

把 huoke 引擎的业务里程碑转发到集团统一事件流（platform/observability/
emitter.py，契约见同目录 EVENT_CONTRACT.md）。业务代码只需：

    from src.telemetry import track
    track("zhituo.task.started", {"task_id": tid, "platform": "facebook"})

设计要点：
  - product_id 固定 ``zhituo``（智拓）；事件名必须是 zhituo.* 三段式，
    namespace 不符时集团发射器直接丢弃；
  - 发射器按文件路径 importlib 加载 —— 顶层目录 platform 与 Python 标准库
    platform 模块同名，**绝不能** ``import platform.observability``（会被
    标准库遮蔽，详见 emitter.py 顶部警告）；
  - 仓库根自动探测：从本文件向上逐级找 platform/observability/emitter.py
    （src → huoke → engines → <仓库根>）；找不到（如 worker 单独部署）时
    整体降级为 no-op，业务照常跑；
  - spool 目录：env ``EVENT_SPOOL_DIR`` 优先，缺省 engines/huoke/data/events/spool；
  - 总开关：env ``HUOKE_TELEMETRY=off``（或 0/false/no）时静默不发；
  - fail-silent：任何异常一律吞掉返回 ""，绝不影响 RPA 主流程。

隐私红线（EVENT_CONTRACT.md 顶部）：props 只允许计数/枚举/内部 ID 引用，
永远不带聊天原文、手机号、客户姓名、画像文本。

自测（在临时 spool 目录发射并读回断言，不碰真实数据）：

    python src/telemetry.py --selftest
"""
from __future__ import annotations

import importlib.util
import os
import threading
from pathlib import Path

__all__ = ["PRODUCT_ID", "enabled", "track"]

PRODUCT_ID = "zhituo"
DISABLE_ENV = "HUOKE_TELEMETRY"          # =off/0/false/no 时总关
SPOOL_ENV = "EVENT_SPOOL_DIR"            # 集团契约统一 spool 环境变量
# 缺省 spool：engines/huoke/data/events/spool（本文件位于 engines/huoke/src/）
DEFAULT_SPOOL_DIR = Path(__file__).resolve().parents[1] / "data" / "events" / "spool"
_EMITTER_RELPATH = ("platform", "observability", "emitter.py")

_lock = threading.Lock()
_emitter = None          # 加载成功的 emitter 模块（进程内缓存一次）
_load_failed = False     # 探测/加载失败 → 本进程内永久 no-op，不反复重试


def _find_emitter_path():
    """从本文件向上找 <仓库根>/platform/observability/emitter.py，找不到返回 None。"""
    try:
        for parent in Path(__file__).resolve().parents:
            cand = parent.joinpath(*_EMITTER_RELPATH)
            if cand.is_file():
                return cand
    except Exception:
        pass
    return None


def _get_emitter():
    """懒加载集团发射器（importlib 按文件路径，双检锁）；失败降级 None。"""
    global _emitter, _load_failed
    if _emitter is not None or _load_failed:
        return _emitter
    with _lock:
        if _emitter is not None or _load_failed:
            return _emitter
        try:
            path = _find_emitter_path()
            if path is None:
                _load_failed = True
                return None
            spec = importlib.util.spec_from_file_location(
                "boundless_emitter", str(path))
            if spec is None or spec.loader is None:
                _load_failed = True
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _emitter = mod
        except Exception:
            _emitter = None
            _load_failed = True
    return _emitter


def enabled() -> bool:
    """总开关：env HUOKE_TELEMETRY=off/0/false/no 时返回 False（默认开）。"""
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in (
        "off", "0", "false", "no")


def track(name, props=None, **kw) -> str:
    """发射一条 zhituo 运营事件到本地 spool；成功返回 event_id，任何失败返回 ""。

    Args:
        name:  三段式事件名，namespace 必须是 zhituo（如 "zhituo.friend.added"）
        props: 扁平标量 dict（str/int/float/bool/None），缺省 {}
        **kw:  可选透传 workspace_id / customer_id / actor / spool_dir

    未注册事件照常落盘（emitter 会打 _unregistered 标记，集团端治理点名）。
    本函数 fail-silent，调用方不需要（也不应该）包 try/except。
    """
    try:
        if not enabled():
            return ""
        emitter = _get_emitter()
        if emitter is None:
            return ""
        spool_dir = kw.get("spool_dir")
        if not spool_dir:
            env = os.environ.get(SPOOL_ENV, "").strip()
            spool_dir = env or str(DEFAULT_SPOOL_DIR)
        return emitter.emit(
            PRODUCT_ID, name, props=props,
            workspace_id=kw.get("workspace_id"),
            customer_id=kw.get("customer_id"),
            actor=kw.get("actor"),
            spool_dir=spool_dir,
        ) or ""
    except Exception:
        return ""


# ── 自测 ────────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """在临时 spool 目录发射 注册/未注册/非法/关开关 事件并读回断言。全过返回 0。"""
    import json
    import re
    import tempfile

    failures = []

    def check(desc, ok):
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    def read_events(d):
        rows = []
        p = Path(d)
        if p.is_dir():
            for f in sorted(p.glob("events-*.jsonl")):
                with open(f, "r", encoding="utf-8") as fh:
                    rows.extend(json.loads(line) for line in fh if line.strip())
        return rows

    print("== 智拓 telemetry 适配器自测（src/telemetry.py --selftest）==")

    print("[1/5] 发射器定位与加载")
    path = _find_emitter_path()
    check(f"找到 emitter: {path}", path is not None)
    emitter = _get_emitter()
    check("importlib 加载成功（未降级 no-op）", emitter is not None)
    if emitter is None:
        print("== 结果：emitter 缺失，无法继续 ==")
        return 1

    eid_re = re.compile(r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")
    saved_env = {k: os.environ.get(k) for k in (DISABLE_ENV, SPOOL_ENV)}
    try:
        os.environ.pop(DISABLE_ENV, None)
        os.environ.pop(SPOOL_ENV, None)
        with tempfile.TemporaryDirectory(prefix="huoke_tele_selftest_") as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_env = os.path.join(tmp, "env")

            print("[2/5] 注册事件发射与读回")
            eid = track("zhituo.task.started",
                        {"task_id": "t_selftest", "platform": "facebook",
                         "task_type": "facebook_add_friend", "device_count": 1},
                        spool_dir=dir_a)
            check(f"返回合法 event_id: {eid}", bool(eid_re.fullmatch(eid)))
            rows = read_events(dir_a)
            check("落盘 1 行且信封字段正确",
                  len(rows) == 1
                  and rows[0].get("event_id") == eid
                  and rows[0].get("product_id") == "zhituo"
                  and rows[0].get("name") == "zhituo.task.started"
                  and rows[0].get("props", {}).get("platform") == "facebook"
                  and "_unregistered" not in rows[0])

            print("[3/5] 未注册事件宽松落盘（打 _unregistered 标记）")
            eid2 = track("zhituo.account.risk_detected",
                         {"platform": "facebook", "cancelled_tasks": 2},
                         spool_dir=dir_a)
            rows = read_events(dir_a)
            check(f"未注册事件仍返回 event_id: {eid2}", bool(eid_re.fullmatch(eid2)))
            check("第 2 行带 _unregistered: true",
                  len(rows) == 2 and rows[1].get("_unregistered") is True)

            print("[4/5] 非法/关开关/坏目录 → 静默返回 \"\"")
            check("namespace 非 zhituo 被丢弃",
                  track("website.order.paid", {"amount": 1.0},
                        spool_dir=dir_a) == "")
            check("两段式事件名被丢弃",
                  track("zhituo.started", spool_dir=dir_a) == "")
            check("props 嵌套对象被丢弃",
                  track("zhituo.task.started", {"a": {"b": 1}},
                        spool_dir=dir_a) == "")
            os.environ[DISABLE_ENV] = "off"
            off_ret = track("zhituo.task.started", {"task_id": "x",
                                                    "platform": "facebook"},
                            spool_dir=dir_a)
            os.environ.pop(DISABLE_ENV, None)
            check("HUOKE_TELEMETRY=off 时不发射", off_ret == "")
            bad_dir = os.path.join(tmp, "occupied")
            with open(bad_dir, "w", encoding="utf-8") as fh:
                fh.write("x")   # 占位文件让 makedirs 失败
            check("spool 目录不可建时 fail-silent",
                  track("zhituo.task.started", {"task_id": "y",
                                                "platform": "facebook"},
                        spool_dir=os.path.join(bad_dir, "sub")) == "")
            check("非法发射后文件行数不变（仍 2 行）",
                  len(read_events(dir_a)) == 2)

            print("[5/5] spool 目录解析：EVENT_SPOOL_DIR 优先")
            os.environ[SPOOL_ENV] = dir_env
            eid3 = track("zhituo.friend.added", {"platform": "facebook"})
            os.environ.pop(SPOOL_ENV, None)
            env_rows = read_events(dir_env)
            check("未传 spool_dir 时按环境变量落盘",
                  bool(eid_re.fullmatch(eid3)) and len(env_rows) == 1
                  and env_rows[0].get("name") == "zhituo.friend.added")
            check("缺省目录常量指向 engines/huoke/data/events/spool",
                  str(DEFAULT_SPOOL_DIR).replace("\\", "/").endswith(
                      "engines/huoke/data/events/spool"))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    import sys

    # Windows 下重定向 stdout 默认本地代码页，打中文会炸，统一 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(_selftest())
    print("用法: python src/telemetry.py --selftest")
    sys.exit(2)
