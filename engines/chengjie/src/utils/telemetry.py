"""无界全域运营事件埋点适配器（P4）——chengjie 引擎 → platform/observability。

把引擎业务埋点转发到仓库根 ``platform/observability/emitter.py``（契约见同目录
EVENT_CONTRACT.md / events_registry.json）。本模块**仅用 Python 标准库**，全程
fail-silent：任何异常都不允许影响业务主路径；emitter 找不到/加载失败 → 本进程
永久降级为 no-op。

【重要】顶层目录 ``platform`` 与标准库 ``platform`` 模块同名——绝不能
``import platform.observability``（会被标准库遮蔽，见 emitter.py 顶部警告）。
本模块从 ``__file__`` 向上找到仓库根后用 ``importlib`` **按文件路径**加载。

环境变量：
  - ``CHENGJIE_PRODUCT_ID``：事件 product_id（缺省 ``zhiliao``；通译实例由部署
    脚本设 ``tongyi``）。事件名 namespace 必须等于 product_id，因此调用方传
    两段式短名（如 ``session.started``），本模块自动补 ``<product_id>.`` 前缀。
  - ``CHENGJIE_TELEMETRY=off``：埋点总开关（off/0/false/no/disabled 均视为关）。
  - ``EVENT_SPOOL_DIR``：spool 目录（最高优先级）。未设置时落引擎 config 目录
    ``<config_dir>/events/spool``（config 目录定位复刻 config_manager 机制：
    AITR_CONFIG_PATH → 其父目录；AITR_DATA_DIR → <dir>/config；否则仓内
    ``engines/chengjie/config``）；连 config 目录都取不到 → 退
    ``engines/chengjie/data/events/spool``。
  - ``CHENGJIE_TELEMETRY_FLUSH_SEC``：翻译字符量聚合 flush 窗口秒数（缺省 300）。

对外 API（全部绝不抛异常）：
  - ``track(name, props=None, **kw)``            发一条事件，返回 event_id 或 ""
  - ``track_once(key, name, props=None, **kw)``  进程内按 key 去重的一次性事件
  - ``add_translated_chars(chars, src, dst)``    翻译字符量增量（按语向聚合，
    窗口到期/进程退出时才发 ``translation.chars_metered``，绝不逐条发事件）
  - ``flush_translated_chars()``                 立即冲刷聚合桶（atexit 已挂）

自测：``python src/utils/telemetry.py --selftest``（临时 spool 目录内验证
注册/未注册事件、双产品前缀、off 开关、emitter 缺失降级、去重与聚合）。
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import threading
import time
from pathlib import Path

__all__ = [
    "add_translated_chars",
    "flush_translated_chars",
    "product_id",
    "track",
    "track_once",
]

_DEFAULT_PRODUCT_ID = "zhiliao"
_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})
_DEFAULT_FLUSH_SEC = 300.0

_STATE_LOCK = threading.Lock()
_EMITTER = None                  # 加载成功的 emitter 模块（进程内缓存）
_EMITTER_FAILED = False          # True → 永久降级 no-op（不再重试加载）
_EMITTER_PATH_OVERRIDE = None    # 测试钩子：强制 emitter 路径（不存在 → 降级）
_ONCE_SEEN: "dict[str, bool]" = {}   # track_once 进程内去重表（容量有上限）
_ONCE_CAP = 8192
_AGG: "dict[tuple, list]" = {}   # (src_lang, dst_lang) -> [chars 累计, 首笔时间]
_AGG_CAP = 32                    # 语向桶数上限，超出立即全量 flush（防异常膨胀）


# ── 路径解析 ─────────────────────────────────────────────────────────────────

def _engine_root() -> Path:
    """引擎根（engines/chengjie）：本文件位于 <engine>/src/utils/ 下。"""
    return Path(__file__).resolve().parents[2]


def _find_emitter_path():
    """从本文件向上逐级找 ``platform/observability/emitter.py``；找不到 → None。"""
    if _EMITTER_PATH_OVERRIDE is not None:
        return Path(_EMITTER_PATH_OVERRIDE)
    for parent in Path(__file__).resolve().parents:
        cand = parent / "platform" / "observability" / "emitter.py"
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _config_dir():
    """复刻 config_manager 的 config 目录定位（不 import 它：那会拖进 yaml 依赖）。"""
    try:
        env_path = (os.environ.get("AITR_CONFIG_PATH") or "").strip()
        if env_path:
            return Path(env_path).expanduser().parent
        env_dir = (os.environ.get("AITR_DATA_DIR") or "").strip()
        if env_dir:
            return Path(env_dir).expanduser() / "config"
        return _engine_root() / "config"
    except Exception:
        return None


def _spool_dir() -> str:
    """spool 目录：EVENT_SPOOL_DIR > <config_dir>/events/spool > <engine>/data/events/spool。"""
    env = (os.environ.get("EVENT_SPOOL_DIR") or "").strip()
    if env:
        return env
    cfg = _config_dir()
    if cfg is not None:
        return str(cfg / "events" / "spool")
    return str(_engine_root() / "data" / "events" / "spool")


# ── emitter 惰性加载（失败即永久 no-op）──────────────────────────────────────

def _load_emitter():
    global _EMITTER, _EMITTER_FAILED
    if _EMITTER is not None:
        return _EMITTER
    if _EMITTER_FAILED:
        return None
    with _STATE_LOCK:
        if _EMITTER is not None or _EMITTER_FAILED:
            return _EMITTER
        try:
            path = _find_emitter_path()
            if path is None or not path.is_file():
                raise FileNotFoundError("platform/observability/emitter.py 未找到")
            spec = importlib.util.spec_from_file_location(
                "chengjie_boundless_emitter", str(path))
            if spec is None or spec.loader is None:
                raise ImportError("spec_from_file_location 失败")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _EMITTER = mod
        except Exception:
            _EMITTER = None
            _EMITTER_FAILED = True
        return _EMITTER


# ── 开关与身份 ───────────────────────────────────────────────────────────────

def _telemetry_off() -> bool:
    return (os.environ.get("CHENGJIE_TELEMETRY") or "").strip().lower() in _OFF_VALUES


def product_id() -> str:
    """当前实例的事件 product_id（同引擎双产品：智聊 zhiliao / 通译 tongyi）。"""
    return (os.environ.get("CHENGJIE_PRODUCT_ID") or "").strip().lower() \
        or _DEFAULT_PRODUCT_ID


def _flush_interval_sec() -> float:
    try:
        raw = (os.environ.get("CHENGJIE_TELEMETRY_FLUSH_SEC") or "").strip()
        return max(0.0, float(raw)) if raw else _DEFAULT_FLUSH_SEC
    except (TypeError, ValueError):
        return _DEFAULT_FLUSH_SEC


# ── 对外 API ────────────────────────────────────────────────────────────────

def track(name: str, props: "dict | None" = None, **kw) -> str:
    """发射一条运营事件；成功返回 event_id，任何失败/关闭返回 ""（绝不抛）。

    ``name`` 传两段式短名（``session.started``）时自动补 ``<product_id>.`` 前缀，
    使 namespace 恒等于 product_id（契约 §3）；传满三段则原样发射。
    ``kw`` 支持可选透传：workspace_id / customer_id / actor。
    """
    try:
        if _telemetry_off():
            return ""
        emitter = _load_emitter()
        if emitter is None:
            return ""
        pid = product_id()
        full = str(name or "")
        if full.count(".") == 1:
            full = pid + "." + full
        return emitter.emit(
            pid, full, props=dict(props or {}),
            workspace_id=kw.get("workspace_id"),
            customer_id=kw.get("customer_id"),
            actor=kw.get("actor"),
            spool_dir=_spool_dir(),
        ) or ""
    except Exception:
        return ""


def track_once(key: str, name: str, props: "dict | None" = None, **kw) -> str:
    """按 ``key`` 进程内去重的一次性事件（额度触顶/首次承接等边沿信号用）。"""
    try:
        if _telemetry_off():
            return ""
        k = str(key)
        with _STATE_LOCK:
            if k in _ONCE_SEEN:
                return ""
            _ONCE_SEEN[k] = True
            while len(_ONCE_SEEN) > _ONCE_CAP:   # FIFO 淘汰最旧 key，防无界增长
                _ONCE_SEEN.pop(next(iter(_ONCE_SEEN)))
        return track(name, props, **kw)
    except Exception:
        return ""


def add_translated_chars(chars: int, src_lang: str = "", dst_lang: str = "") -> None:
    """翻译字符量增量：按 (src, dst) 语向进程内聚合，窗口到期才发一条
    ``translation.chars_metered``（契约 ``*_metered`` 按批上报语义），
    绝不逐条消息发事件。单次调用仅 dict 累加 + 到期判断，微秒级。
    """
    try:
        n = int(chars or 0)
        if n <= 0 or _telemetry_off():
            return
        key = (str(src_lang or ""), str(dst_lang or ""))
        now = time.time()
        due = []
        with _STATE_LOCK:
            row = _AGG.get(key)
            if row is None:
                _AGG[key] = [n, now]
            else:
                row[0] += n
            flush_all = len(_AGG) > _AGG_CAP
            interval = _flush_interval_sec()
            for k in list(_AGG):
                if flush_all or now - _AGG[k][1] >= interval:
                    due.append((k, _AGG.pop(k)[0]))
        for (src, dst), total in due:
            _emit_chars(src, dst, total)
    except Exception:
        pass


def flush_translated_chars() -> None:
    """立即冲刷全部聚合桶（atexit 自动调用；测试/停机前也可手动调）。"""
    try:
        with _STATE_LOCK:
            due = [(k, row[0]) for k, row in _AGG.items()]
            _AGG.clear()
        for (src, dst), total in due:
            _emit_chars(src, dst, total)
    except Exception:
        pass


def _emit_chars(src: str, dst: str, total: int) -> None:
    props = {"chars": int(total)}
    if src:
        props["src_lang"] = src
    if dst:
        props["dst_lang"] = dst
    track("translation.chars_metered", props)


def _reset_for_test(emitter_path_override=None) -> None:
    """测试钩子：清空缓存/降级标记/去重表/聚合桶，并可强制 emitter 路径。"""
    global _EMITTER, _EMITTER_FAILED, _EMITTER_PATH_OVERRIDE
    with _STATE_LOCK:
        _EMITTER = None
        _EMITTER_FAILED = False
        _EMITTER_PATH_OVERRIDE = emitter_path_override
        _ONCE_SEEN.clear()
        _AGG.clear()


try:
    atexit.register(flush_translated_chars)   # 进程退出前把未满窗的增量落盘
except Exception:
    pass


# ── 自测 ────────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """临时 spool 内验证全链路。全过返回 0，否则 1。不碰仓库 data/config。"""
    import json
    import tempfile

    failures: "list[str]" = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    def read_rows(d) -> "list[dict]":
        rows = []
        p = Path(d)
        if not p.is_dir():
            return rows
        for f in sorted(p.glob("events-*.jsonl")):
            with open(f, "r", encoding="utf-8") as fh:
                rows.extend(json.loads(line) for line in fh if line.strip())
        return rows

    print("== chengjie 埋点适配器自测（telemetry.py --selftest）==")
    saved_env = {k: os.environ.get(k) for k in (
        "EVENT_SPOOL_DIR", "CHENGJIE_TELEMETRY", "CHENGJIE_PRODUCT_ID",
        "CHENGJIE_TELEMETRY_FLUSH_SEC")}
    try:
        with tempfile.TemporaryDirectory(prefix="chengjie_telemetry_selftest_") as tmp:
            spool = os.path.join(tmp, "spool")
            os.environ["EVENT_SPOOL_DIR"] = spool
            os.environ.pop("CHENGJIE_TELEMETRY", None)
            os.environ.pop("CHENGJIE_PRODUCT_ID", None)
            os.environ.pop("CHENGJIE_TELEMETRY_FLUSH_SEC", None)
            _reset_for_test()

            print("[1/7] emitter 定位与加载")
            emitter = _load_emitter()
            check("向上定位仓库根并按文件加载 emitter 成功", emitter is not None)
            check("加载的确是事件发射器（有 emit/PRODUCT_IDS）",
                  hasattr(emitter, "emit") and hasattr(emitter, "PRODUCT_IDS"))

            print("[2/7] 注册事件（缺省产品 zhiliao，两段短名自动补前缀）")
            eid = track("session.started",
                        {"session_id": "telegram:demo:1", "platform": "telegram"})
            rows = read_rows(spool)
            check(f"发射成功返回 event_id: {eid}", bool(eid))
            check("落盘 1 行且 name=zhiliao.session.started / product_id=zhiliao",
                  len(rows) == 1
                  and rows[0].get("name") == "zhiliao.session.started"
                  and rows[0].get("product_id") == "zhiliao")
            check("注册事件不带 _unregistered 标记", "_unregistered" not in rows[0])

            print("[3/7] 未注册事件照发并标 _unregistered")
            eid2 = track("license.quota_exceeded", {"used": 10, "included": 10})
            rows = read_rows(spool)
            check(f"未注册事件仍返回 event_id: {eid2}", bool(eid2))
            check("落盘并带 _unregistered: true",
                  len(rows) == 2 and rows[1].get("_unregistered") is True)

            print("[4/7] 双产品：CHENGJIE_PRODUCT_ID=tongyi 时 namespace 跟随")
            os.environ["CHENGJIE_PRODUCT_ID"] = "tongyi"
            eid3 = track("translation.chars_metered",
                         {"chars": 128, "src_lang": "zh", "dst_lang": "en"})
            rows = read_rows(spool)
            os.environ.pop("CHENGJIE_PRODUCT_ID", None)
            check("tongyi.translation.chars_metered / product_id=tongyi / 已注册",
                  bool(eid3) and len(rows) == 3
                  and rows[2].get("name") == "tongyi.translation.chars_metered"
                  and rows[2].get("product_id") == "tongyi"
                  and "_unregistered" not in rows[2])

            print("[5/7] CHENGJIE_TELEMETRY=off 总开关")
            os.environ["CHENGJIE_TELEMETRY"] = "off"
            r_off = track("session.started", {"session_id": "x", "platform": "t"})
            add_translated_chars(999, "zh", "en")
            flush_translated_chars()
            rows = read_rows(spool)
            os.environ.pop("CHENGJIE_TELEMETRY", None)
            check("off 时 track 返回 \"\" 且 spool 不增行（含聚合路径）",
                  r_off == "" and len(rows) == 3)

            print("[6/7] emitter 缺失 → 永久降级 no-op（不抛不落盘）")
            _reset_for_test(os.path.join(tmp, "no_such_emitter.py"))
            r_deg = track("session.started", {"session_id": "y", "platform": "t"})
            r_deg2 = track("session.started", {"session_id": "y", "platform": "t"})
            check("降级后 track 恒返回 \"\" 且不抛", r_deg == "" and r_deg2 == "")
            check("降级路径不落盘", len(read_rows(spool)) == 3)
            _reset_for_test()   # 恢复正常加载

            print("[7/7] track_once 去重 + 翻译字符量聚合")
            a = track_once("k1", "session.handed_off", {"session_id": "s1"})
            b = track_once("k1", "session.handed_off", {"session_id": "s1"})
            rows = read_rows(spool)
            check("同 key 第二次 track_once 被去重（只落 1 行）",
                  bool(a) and b == "" and len(rows) == 4)
            os.environ["CHENGJIE_TELEMETRY_FLUSH_SEC"] = "3600"
            add_translated_chars(100, "zh", "en")
            add_translated_chars(28, "zh", "en")
            add_translated_chars(7, "en", "zh")
            check("窗口未到期时增量只进内存不落盘", len(read_rows(spool)) == 4)
            flush_translated_chars()
            rows = read_rows(spool)
            os.environ.pop("CHENGJIE_TELEMETRY_FLUSH_SEC", None)
            metered = [r for r in rows[4:]
                       if r.get("name") == "zhiliao.translation.chars_metered"]
            zh_en = [r for r in metered if r["props"].get("src_lang") == "zh"]
            en_zh = [r for r in metered if r["props"].get("src_lang") == "en"]
            check("flush 后每语向恰 1 条且 chars 为累计和（128 / 7）",
                  len(metered) == 2
                  and len(zh_en) == 1 and zh_en[0]["props"].get("chars") == 128
                  and len(en_zh) == 1 and en_zh[0]["props"].get("chars") == 7)
            check("聚合事件为注册名（zhiliao.translation.chars_metered）",
                  all("_unregistered" not in r for r in metered))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reset_for_test()

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 重定向默认代码页防乱码
    except (AttributeError, OSError):
        pass
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(_selftest())
    print("用法: python src/utils/telemetry.py --selftest")
    sys.exit(2)
