#!/usr/bin/env python3
r"""无界 Boundless 全域运营事件发射器（仅 Python 标准库）。

把一条运营指标事件包成契约信封（EVENT_CONTRACT.md §2），追加写入本地 spool
目录的按天 JSONL 文件（events-YYYYMMDD.jsonl，UTC，append-only）。本期(P0)
只落盘不联网；收割/上传是下期收集器的职责。

【重要】目录名陷阱 —— 顶层目录 platform 与 Python 标准库 platform 模块同名！
  - 不要把仓库根加入 sys.path 后 ``import platform.observability``：platform/
    没有 __init__.py 时只是命名空间包候选，标准库的常规模块 platform 会赢得
    解析，该 import 直接失败，且行为随路径顺序微妙变化；
  - 绝对不要给 platform/ 或 platform/observability/ 添加 __init__.py：那会
    遮蔽标准库 platform，大量依赖 platform.system() 的第三方库会莫名炸掉。

推荐加载方式（二选一，与 platform/identity/ids.py 同款约定）：

    # 方式 A：把本目录（platform/observability/）加入 sys.path，然后 import emitter
    import sys
    sys.path.insert(0, r"D:\workspace\boundless\platform\observability")
    import emitter
    emitter.emit("tongyi", "tongyi.translation.chars_metered",
                 props={"chars": 1280, "src_lang": "zh", "dst_lang": "en"})

    # 方式 B：importlib 按文件路径加载，不动 sys.path
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "boundless_emitter",
        r"D:\workspace\boundless\platform\observability\emitter.py")
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)

行为要点：
  - emit() **fail-silent**：信封非法（product_id/name/props 不符合契约）静默
    丢弃返回 ""；磁盘满/目录只读等 IO 异常同样吞掉返回 ""。埋点绝不允许把
    业务主路径打挂。成功时返回 event_id（evt_ULID）。
  - 对照 events_registry.json **宽松校验**：未注册事件不拒收、照常落盘，只在
    信封上追加 ``"_unregistered": true`` 供集团端治理点名。
  - 隐私红线（EVENT_CONTRACT.md 顶部）：props 永远不带聊天/翻译原文、生物
    特征、完整手机号/证件号。发射器不做内容审查，红线靠事件注册评审把关。

自测（在临时目录发射 注册/未注册/非法 事件并读回断言，不碰仓库 data/）：

    python platform/observability/emitter.py --selftest
    python platform/observability/emitter.py --validate <某个 events-*.jsonl>
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "CROCKFORD_ALPHABET",
    "DEFAULT_SPOOL_DIR",
    "EVENT_ID_PATTERN",
    "NAME_PATTERN",
    "PRODUCT_IDS",
    "REGISTRY_PATH",
    "SPOOL_ENV_VAR",
    "emit",
    "load_registry",
    "validate_file",
]

# ── 契约常量（与 EVENT_CONTRACT.md / events_registry.json 一致，改动即破坏契约） ──

# 九个合法 product_id：七产品 + 官网 + 平台层
PRODUCT_IDS = frozenset({
    "zhituo", "zhiliao", "tongyi", "tongchuan",
    "huansheng", "huanying", "huanyan", "website", "platform",
})

# 事件名三段式 <namespace>.<domain>.<action>；namespace 必须等于 product_id
NAME_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$"
_NAME_RE = re.compile(NAME_PATTERN)

# event_id = evt_<26 字符 Crockford Base32 大写 ULID>
EVENT_ID_PATTERN = r"^evt_[0-9A-HJKMNP-TV-Z]{26}$"
_EVENT_ID_RE = re.compile(EVENT_ID_PATTERN)

# ts = ISO8601 UTC 毫秒，如 2026-07-18T04:00:00.123Z
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# Crockford Base32：32 字符按码点升序，不含 I/L/O/U（与 platform/identity 一致）
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_MAP = {ch: i for i, ch in enumerate(CROCKFORD_ALPHABET)}
_ULID_LEN = 26
_RANDOM_BYTES = 10                     # 随机部分 80 bit
_MAX_TIMESTAMP_MS = (1 << 48) - 1      # 时间戳部分 48 bit

SPOOL_ENV_VAR = "EVENT_SPOOL_DIR"
# 缺省 spool：<仓库根>/data/events/spool/（本文件位于 <仓库根>/platform/observability/）
DEFAULT_SPOOL_DIR = Path(__file__).resolve().parents[2] / "data" / "events" / "spool"
REGISTRY_PATH = Path(__file__).resolve().parent / "events_registry.json"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_WRITE_LOCK = threading.Lock()   # 串行化文件追加，保证多线程下行不交错
_REG_LOCK = threading.Lock()     # 保护 registry 缓存的一次性加载
_REG_CACHE: "frozenset[str] | None" = None


# ── ULID ────────────────────────────────────────────────────────────────────

def _ulid_encode(timestamp_ms: int, randomness: bytes) -> str:
    """(毫秒时间戳, 10 字节随机数) → 26 字符 Crockford Base32 大写 ULID。

    高 48 bit 时间戳 + 低 80 bit 随机数拼成 128 bit，按 5 bit 一组从高位取字符。
    26 字符共 130 bit，最高 2 bit 恒 0，故合法首字符必在 0-7。
    """
    if not 0 <= timestamp_ms <= _MAX_TIMESTAMP_MS:
        raise ValueError(f"时间戳超出 48 bit 范围: {timestamp_ms!r}")
    if not isinstance(randomness, (bytes, bytearray)) or len(randomness) != _RANDOM_BYTES:
        raise ValueError(f"随机部分必须是 {_RANDOM_BYTES} 字节: {randomness!r}")
    value = (timestamp_ms << 80) | int.from_bytes(randomness, "big")
    return "".join(CROCKFORD_ALPHABET[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def _ulid_timestamp_ms(ulid: str) -> int:
    """从 26 字符 ULID 里解出毫秒时间戳（自测/调试用）。非法字符抛 ValueError。"""
    if len(ulid) != _ULID_LEN:
        raise ValueError(f"ULID 必须 {_ULID_LEN} 字符: {ulid!r}")
    value = 0
    for ch in ulid:
        idx = _DECODE_MAP.get(ch)
        if idx is None:
            raise ValueError(f"非法 Crockford 字符: {ch!r}")
        value = (value << 5) | idx
    return value >> 80


# ── 时间与路径 ───────────────────────────────────────────────────────────────

def _iso_ms(ts_ms: int) -> str:
    """Unix 毫秒 → ISO8601 UTC 毫秒字符串（整数毫秒运算，无浮点精度损失）。"""
    dt = _EPOCH + timedelta(milliseconds=ts_ms)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond // 1000:03d}Z")


def _day_filename(ts_ms: int) -> str:
    """事件 ts 的 UTC 日期决定落进哪个日文件（契约 §4/§7：与本地时区无关）。"""
    dt = _EPOCH + timedelta(milliseconds=ts_ms)
    return f"events-{dt.year:04d}{dt.month:02d}{dt.day:02d}.jsonl"


def _resolve_spool_dir(spool_dir=None) -> str:
    """优先级：显式参数 > 环境变量 EVENT_SPOOL_DIR > 仓库缺省目录。"""
    if spool_dir:
        return os.fspath(spool_dir)
    env = os.environ.get(SPOOL_ENV_VAR, "").strip()
    if env:
        return env
    return str(DEFAULT_SPOOL_DIR)


# ── registry ────────────────────────────────────────────────────────────────

def load_registry(path=None) -> dict:
    """读取事件字典 events_registry.json（工具/校验用，文件缺失或损坏会抛异常）。"""
    with open(path or REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _registered_names() -> "frozenset[str]":
    """registry 中已注册事件名集合（进程内缓存一次）。

    加载失败（文件缺失/损坏）不抛异常，返回空集合——此时所有事件都会被标
    _unregistered，数据照常落盘，同时在治理报表里暴露部署问题。
    """
    global _REG_CACHE
    if _REG_CACHE is None:
        with _REG_LOCK:
            if _REG_CACHE is None:
                try:
                    _REG_CACHE = frozenset(
                        e["name"] for e in load_registry().get("events", [])
                        if isinstance(e, dict) and isinstance(e.get("name"), str)
                    )
                except Exception:
                    _REG_CACHE = frozenset()
    return _REG_CACHE


# ── 校验 ────────────────────────────────────────────────────────────────────

def _props_error(props) -> "str | None":
    """props 必须是扁平 JSON 对象：str 键 + 标量值(str/int/float/bool/None)。"""
    if not isinstance(props, dict):
        return f"props 必须是 dict，得到 {type(props).__name__}"
    for k, v in props.items():
        if not isinstance(k, str):
            return f"props 键必须是 str: {k!r}"
        if v is not None and not isinstance(v, (str, int, float, bool)):
            return f"props[{k!r}] 必须是标量，得到 {type(v).__name__}"
    return None


def _ts_valid(ts) -> bool:
    if not isinstance(ts, str) or _TS_RE.fullmatch(ts) is None:
        return False
    try:
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
        return True
    except ValueError:   # 形如 2026-13-99 的假日期
        return False


def _envelope_error(obj: dict) -> "str | None":
    """校验一条已落盘事件的完整信封。合法返回 None，否则返回原因。

    信封外的未知字段按契约 §2 容忍不报错（向前兼容）。
    """
    eid = obj.get("event_id")
    if not isinstance(eid, str) or _EVENT_ID_RE.fullmatch(eid) is None:
        return f"event_id 不符合 {EVENT_ID_PATTERN}: {eid!r}"
    if eid[4] > "7":   # 首字符 >7 即 128 bit 溢出（与全域 ID 规范一致）
        return f"event_id ULID 数值溢出 128 bit: {eid!r}"
    if not _ts_valid(obj.get("ts")):
        return f"ts 不是 ISO8601 UTC 毫秒格式: {obj.get('ts')!r}"
    pid = obj.get("product_id")
    if pid not in PRODUCT_IDS:
        return f"product_id 不在枚举内: {pid!r}"
    name = obj.get("name")
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        return f"name 不符合三段式 {NAME_PATTERN}: {name!r}"
    if name.split(".", 1)[0] != pid:
        return f"namespace 必须等于 product_id: {name!r} vs {pid!r}"
    for key in ("workspace_id", "customer_id", "actor"):
        if key in obj and not isinstance(obj[key], str):
            return f"{key} 必须是 str: {obj[key]!r}"
    if "props" not in obj:
        return "缺少 props 字段"
    perr = _props_error(obj["props"])
    if perr:
        return perr
    if "_unregistered" in obj and obj["_unregistered"] is not True:
        return f"_unregistered 只允许为 true: {obj['_unregistered']!r}"
    return None


# ── 对外 API ────────────────────────────────────────────────────────────────

def emit(product_id, name, props=None, workspace_id=None,
         customer_id=None, actor=None, spool_dir=None) -> str:
    """发射一条运营事件到本地 spool，返回 event_id；任何失败返回 ""。

    - 信封非法（product_id 不在枚举 / name 不符合三段式或 namespace 不等于
      product_id / props 非扁平标量对象 / 可选字段非 str）→ 静默丢弃返回 ""；
    - 未注册事件（不在 events_registry.json）→ 照常落盘并加 "_unregistered": true；
    - IO / 序列化异常 → 吞掉返回 ""（fail-silent，绝不影响业务主路径）；
    - 线程安全：写文件持有模块级 threading.Lock。
    """
    try:
        if product_id not in PRODUCT_IDS:
            return ""
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            return ""
        if name.split(".", 1)[0] != product_id:
            return ""
        if props is None:
            props = {}
        if _props_error(props) is not None:
            return ""
        for opt in (workspace_id, customer_id, actor):
            if opt is not None and not isinstance(opt, str):
                return ""

        ts_ms = time.time_ns() // 1_000_000
        event_id = "evt_" + _ulid_encode(ts_ms, secrets.token_bytes(_RANDOM_BYTES))
        envelope: dict = {
            "event_id": event_id,
            "ts": _iso_ms(ts_ms),
            "product_id": product_id,
            "name": name,
        }
        if workspace_id is not None:
            envelope["workspace_id"] = workspace_id
        if customer_id is not None:
            envelope["customer_id"] = customer_id
        if actor is not None:
            envelope["actor"] = actor
        envelope["props"] = dict(props)   # 浅拷贝，避免落盘后调用方改动串味
        if name not in _registered_names():
            envelope["_unregistered"] = True

        # 先序列化再开文件：NaN/Infinity 等序列化失败时不留半行脏数据
        line = json.dumps(envelope, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")) + "\n"
        directory = _resolve_spool_dir(spool_dir)
        path = os.path.join(directory, _day_filename(ts_ms))
        with _WRITE_LOCK:
            os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
        return event_id
    except Exception:
        return ""


def validate_file(path) -> dict:
    """校验一个 spool jsonl 文件，返回统计 dict（不修改文件）。

    统计项：total（数据行数）、valid、invalid、unregistered（合法但未注册，
    含带 _unregistered 标记的）、duplicate_event_ids、date_mismatch（文件名
    是 events-YYYYMMDD.jsonl 时，ts 的 UTC 日期与文件名不符的行数）、
    by_product / by_name 分布、errors（前 20 条错误样本）。
    文件不存在等 IO 错误正常抛异常（这是运维工具，不 fail-silent）。
    """
    stats = {
        "path": str(path),
        "total": 0, "valid": 0, "invalid": 0,
        "unregistered": 0, "duplicate_event_ids": 0, "date_mismatch": 0,
        "by_product": {}, "by_name": {}, "errors": [],
    }
    registered = _registered_names()
    m = re.fullmatch(r"events-(\d{8})\.jsonl", os.path.basename(os.fspath(path)))
    expect_date = m.group(1) if m else None
    seen: set = set()
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            stats["total"] += 1
            err = None
            obj = None
            try:
                obj = json.loads(raw)
            except ValueError:
                err = "JSON 解析失败"
            if err is None and not isinstance(obj, dict):
                err = f"顶层必须是 JSON 对象，得到 {type(obj).__name__}"
            if err is None:
                err = _envelope_error(obj)
            if err is not None:
                stats["invalid"] += 1
                if len(stats["errors"]) < 20:
                    stats["errors"].append(f"第 {lineno} 行: {err}")
                continue
            stats["valid"] += 1
            eid = obj["event_id"]
            if eid in seen:
                stats["duplicate_event_ids"] += 1
            seen.add(eid)
            if obj["name"] not in registered:
                stats["unregistered"] += 1
            stats["by_product"][obj["product_id"]] = stats["by_product"].get(obj["product_id"], 0) + 1
            stats["by_name"][obj["name"]] = stats["by_name"].get(obj["name"], 0) + 1
            if expect_date and obj["ts"][:10].replace("-", "") != expect_date:
                stats["date_mismatch"] += 1
    return stats


# ── 自测 ────────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """在临时目录发射 注册/未注册/非法 事件并读回断言。全过返回 0，否则 1。"""
    import tempfile

    failures: "list[str]" = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    def read_lines(p) -> "list[dict]":
        if not os.path.exists(p):
            return []
        with open(p, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def only_file(d) -> str:
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
        return os.path.join(d, names[0]) if len(names) == 1 else ""

    print("== 无界事件发射器自测（emitter.py --selftest）==")

    print("[1/7] 事件字典 registry 一致性")
    try:
        reg = load_registry()
        events = reg.get("events", [])
        names = [e["name"] for e in events]
        check(f"registry 可加载，含 {len(events)} 个事件（>= 30）", len(events) >= 30)
        check("version 为正整数", isinstance(reg.get("version"), int) and reg["version"] >= 1)
        check("事件名全局唯一", len(set(names)) == len(names))
        check("全部事件名符合三段式正则", all(_NAME_RE.fullmatch(n) for n in names))
        check("全部 namespace == product 且 product 在枚举内",
              all(e["name"].split(".", 1)[0] == e["product"] and e["product"] in PRODUCT_IDS
                  for e in events))
        check("全部 tier 为 core/optional", all(e.get("tier") in ("core", "optional") for e in events))
        check("registry 的 product_ids 与代码常量一致",
              set(reg.get("product_ids", [])) == set(PRODUCT_IDS))
        check("每个事件的 props 字段名不重复",
              all(len({p["name"] for p in e.get("props", [])}) == len(e.get("props", []))
                  for e in events))
        must_have = {
            "website.lead.submitted", "website.order.created", "website.order.paid",
            "website.order.activated", "website.order.cancelled",
            "platform.license.issued", "platform.license.renewed",
            "platform.license.revoked", "platform.license.expired",
        }
        check("官网漏斗 + 授权生命周期事件齐全", must_have <= set(names))
    except Exception as exc:   # registry 坏了直接判失败，但继续跑后续项
        check(f"registry 加载异常: {exc!r}", False)

    with tempfile.TemporaryDirectory(prefix="boundless_events_selftest_") as tmp:
        dir_a = os.path.join(tmp, "a")
        dir_env = os.path.join(tmp, "env")
        dir_thr = os.path.join(tmp, "thr")

        print("[2/7] 注册事件发射与读回")
        before = time.time_ns() // 1_000_000
        eid = emit("website", "website.order.paid",
                   props={"order_id": "ord_01KXS8BM00008J4CT4ANK7F24S",
                          "amount": 78.0, "currency": "USD", "payment_method": "stripe"},
                   customer_id="cust_01KXS8BM00008J4CT4ANK7F24S", spool_dir=dir_a)
        after = time.time_ns() // 1_000_000
        check(f"返回合法 event_id: {eid}",
              bool(_EVENT_ID_RE.fullmatch(eid)) and eid[4] <= "7")
        check("event_id 内嵌时间戳与当前时刻一致",
              bool(eid) and before <= _ulid_timestamp_ms(eid[4:]) <= after)
        spool_file = only_file(dir_a)
        check("生成唯一日文件且文件名匹配 events-YYYYMMDD.jsonl",
              bool(re.fullmatch(r"events-\d{8}\.jsonl", os.path.basename(spool_file or ""))))
        rows = read_lines(spool_file)
        row = rows[0] if rows else {}
        check("读回 1 行且信封字段逐项一致",
              len(rows) == 1
              and row.get("event_id") == eid
              and row.get("product_id") == "website"
              and row.get("name") == "website.order.paid"
              and row.get("customer_id") == "cust_01KXS8BM00008J4CT4ANK7F24S"
              and row.get("props", {}).get("amount") == 78.0
              and "workspace_id" not in row and "actor" not in row)
        check("ts 为合法 ISO8601 UTC 毫秒且日期与文件名一致",
              _ts_valid(row.get("ts", ""))
              and row.get("ts", "")[:10].replace("-", "") in os.path.basename(spool_file or ""))
        check("信封校验器判定合法且无 _unregistered 标记",
              _envelope_error(row) is None and "_unregistered" not in row)

        print("[3/7] 未注册事件宽松落盘")
        eid2 = emit("zhituo", "zhituo.rocket.launched", props={"n": 1}, spool_dir=dir_a)
        rows = read_lines(spool_file)
        check(f"未注册事件仍返回 event_id: {eid2}", bool(_EVENT_ID_RE.fullmatch(eid2)))
        check("落盘并带 _unregistered: true",
              len(rows) == 2 and rows[1].get("_unregistered") is True
              and rows[1].get("name") == "zhituo.rocket.launched")

        print("[4/7] 非法信封静默丢弃（返回 \"\" 且不落盘）")
        bad_cases = [
            ("product_id 不在枚举", lambda: emit("wechat", "wechat.order.paid", spool_dir=dir_a)),
            ("product_id 非字符串", lambda: emit(123, "website.order.paid", spool_dir=dir_a)),
            ("name 只有两段", lambda: emit("website", "website.paid", spool_dir=dir_a)),
            ("name 含大写", lambda: emit("website", "Website.Order.Paid", spool_dir=dir_a)),
            ("namespace != product_id",
             lambda: emit("website", "platform.license.issued", spool_dir=dir_a)),
            ("props 非 dict", lambda: emit("website", "website.order.paid",
                                           props="oops", spool_dir=dir_a)),
            ("props 值为嵌套对象", lambda: emit("website", "website.order.paid",
                                                props={"a": {"b": 1}}, spool_dir=dir_a)),
            ("props 值为数组", lambda: emit("website", "website.order.paid",
                                            props={"a": [1, 2]}, spool_dir=dir_a)),
            ("props 键非字符串", lambda: emit("website", "website.order.paid",
                                              props={1: "x"}, spool_dir=dir_a)),
            ("props 含 NaN", lambda: emit("website", "website.order.paid",
                                          props={"a": float("nan")}, spool_dir=dir_a)),
            ("workspace_id 非 str", lambda: emit("website", "website.order.paid",
                                                 workspace_id=123, spool_dir=dir_a)),
            ("actor 非 str", lambda: emit("website", "website.order.paid",
                                          actor=1.5, spool_dir=dir_a)),
        ]
        for why, fn in bad_cases:
            ret = fn()
            check(f"拒绝 {why}", ret == "")
        check("非法发射后文件行数不变（仍 2 行）", len(read_lines(spool_file)) == 2)

        print("[5/7] spool 目录解析：EVENT_SPOOL_DIR 环境变量")
        saved = os.environ.get(SPOOL_ENV_VAR)
        try:
            os.environ[SPOOL_ENV_VAR] = dir_env
            eid3 = emit("platform", "platform.license.issued",
                        props={"license_id": "lic_01KXS8BM00008J4CT4ANK7F24S",
                               "sku_id": "voicex-pro", "product": "huansheng",
                               "edition": "pro", "valid_days": 365})
        finally:
            if saved is None:
                os.environ.pop(SPOOL_ENV_VAR, None)
            else:
                os.environ[SPOOL_ENV_VAR] = saved
        env_file = only_file(dir_env)
        env_rows = read_lines(env_file)
        check("未传 spool_dir 时按环境变量落盘且内容正确",
              bool(_EVENT_ID_RE.fullmatch(eid3)) and len(env_rows) == 1
              and env_rows[0].get("name") == "platform.license.issued"
              and "_unregistered" not in env_rows[0])

        print("[6/7] 线程安全（8 线程 x 50 条并发追加）")
        n_threads, per_thread = 8, 50
        returned: "list[str]" = []

        def worker(base: int) -> None:
            for j in range(per_thread):
                returned.append(emit(
                    "tongyi", "tongyi.translation.chars_metered",
                    props={"chars": base * 1000 + j, "src_lang": "zh", "dst_lang": "en"},
                    spool_dir=dir_thr))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        thr_rows = read_lines(only_file(dir_thr))
        total = n_threads * per_thread
        check(f"全部 {total} 次 emit 均返回 event_id", all(returned) and len(returned) == total)
        check(f"文件恰好 {total} 行且每行均可解析", len(thr_rows) == total)
        check("event_id 全局无重复", len({r["event_id"] for r in thr_rows}) == total)
        check("并发写入无行交错（每行信封均合法）",
              all(_envelope_error(r) is None for r in thr_rows))
        check("props.chars 无丢失（0..399 各出现一次）",
              {r["props"]["chars"] % 1000 + (r["props"]["chars"] // 1000) * per_thread
               for r in thr_rows} == set(range(total)))

        print("[7/7] validate_file 统计")
        s = validate_file(spool_file)
        check("dir_a: total=2 valid=2 unregistered=1 invalid=0 无重复无日期错位",
              (s["total"], s["valid"], s["unregistered"], s["invalid"],
               s["duplicate_event_ids"], s["date_mismatch"]) == (2, 2, 1, 0, 0, 0))
        check("dir_a: by_product 分布正确",
              s["by_product"] == {"website": 1, "zhituo": 1})
        s_thr = validate_file(only_file(dir_thr))
        check(f"并发文件: total=valid={total} 且 unregistered=0",
              (s_thr["total"], s_thr["valid"], s_thr["unregistered"]) == (total, total, 0))
        # 人工构造脏文件：合法行 + 垃圾行 + 假信封行 + 重复行
        mixed = os.path.join(tmp, "mixed.jsonl")
        with open(spool_file, "r", encoding="utf-8") as f:
            good_lines = f.readlines()
        with open(mixed, "w", encoding="utf-8", newline="\n") as f:
            f.write(good_lines[0])
            f.write("这不是JSON\n")
            f.write('{"event_id":"evt_bad","name":"x"}\n')
            f.write(good_lines[0])   # 重复 event_id
        s_mix = validate_file(mixed)
        check("脏文件: total=4 valid=2 invalid=2 duplicate=1",
              (s_mix["total"], s_mix["valid"], s_mix["invalid"],
               s_mix["duplicate_event_ids"]) == (4, 2, 2, 1))
        check("脏文件: errors 采样含行号", len(s_mix["errors"]) == 2
              and s_mix["errors"][0].startswith("第 2 行"))

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


_USAGE = """用法:
  python emitter.py --selftest           在临时目录自测（注册/未注册/非法/并发/校验）
  python emitter.py --validate <path>    校验一个 spool jsonl 文件并打印统计 JSON

作为库使用时注意顶层目录 platform 与标准库同名，加载方式见本文件顶部注释。
"""


def main(argv: "list[str]") -> int:
    if argv == ["--selftest"]:
        return _selftest()
    if len(argv) == 2 and argv[0] == "--validate":
        stats = validate_file(argv[1])
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0 if stats["invalid"] == 0 else 1
    print(_USAGE, end="")
    return 2


if __name__ == "__main__":
    # Windows 下 stdout 重定向时默认本地代码页（cp936 等），打中文会炸，统一 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main(sys.argv[1:]))
