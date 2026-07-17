#!/usr/bin/env python3
r"""无界 Boundless 全域统一 ID 参考实现（仅 Python 标准库）。

ID 形如 ``<prefix>_<ULID>``：
  - prefix：2-5 个小写字母，必须已在 PREFIXES 注册表登记（cust/org/ord/lic/prs/evt/wsp）；
  - ULID：26 字符 Crockford Base32 大写（字母表 0123456789ABCDEFGHJKMNPQRSTVWXYZ，
    不含 I/L/O/U），高 48 bit 为 Unix 毫秒时间戳，低 80 bit 为加密安全随机数。

规范全文（格式契约、前缀注册表、遗留 ID 共存策略、跨语言要求）见同目录 ID_SPEC.md。

【重要】目录名陷阱 —— 顶层目录 platform 与 Python 标准库 platform 模块同名！
  - 不要把仓库根加入 sys.path 后 ``import platform.identity``：platform/ 没有
    __init__.py 时只是命名空间包候选，标准库的常规模块 platform 会赢得解析，
    该 import 直接失败，且行为随路径顺序微妙变化；
  - 绝对不要给 platform/ 或 platform/identity/ 添加 __init__.py：那会遮蔽标准库
    platform，大量依赖 platform.system() 的第三方库会在难以排查的位置炸掉。

推荐加载方式（二选一）：

    # 方式 A：把本目录（platform/identity/）加入 sys.path，然后 import ids
    import sys
    sys.path.insert(0, r"D:\workspace\boundless\platform\identity")
    import ids
    print(ids.new_id("cust"))

    # 方式 B：importlib 按文件路径加载，不动 sys.path
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "boundless_ids", r"D:\workspace\boundless\platform\identity\ids.py")
    ids = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ids)

自测（生成/校验/解析回环、已知答案向量、时间有序、1 万个无重复、非法输入拒绝）：

    python platform/identity/ids.py --selftest
"""

from __future__ import annotations

import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

__all__ = [
    "CROCKFORD_ALPHABET",
    "ID_PATTERN",
    "ID_RE",
    "PREFIXES",
    "ParsedId",
    "is_valid",
    "new_id",
    "parse",
    "ulid_decode",
    "ulid_encode",
]

# ── 契约常量（与 ID_SPEC.md §2 逐字符一致，改动即破坏跨语言兼容） ──────────────

# Crockford Base32 字母表：32 字符，按码点升序，不含 I/L/O/U（避免与 1/0 混淆）
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_MAP = {ch: i for i, ch in enumerate(CROCKFORD_ALPHABET)}

ULID_LEN = 26          # 26 × 5 bit = 130 bit，容纳 128 bit 值（最高 2 bit 恒 0）
RANDOM_BYTES = 10      # 随机部分 80 bit
MAX_TIMESTAMP_MS = (1 << 48) - 1  # 48 bit 毫秒时间戳，可用到公元 10889 年

# 语法校验正则（已拍板，不得更改）。注意 Python 端必须用 fullmatch：
# re.match + '$' 会放过结尾带换行的 "cust_...\n"（Python 的 '$' 允许结尾换行）。
ID_PATTERN = r"^[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}$"
ID_RE = re.compile(ID_PATTERN)

# 前缀注册表（本期全部；新增前缀须同步修改 ID_SPEC.md §3 与 TypeScript 端常量）。
# SKU 是唯一例外：不用生成式 ID，沿用 platform/licensing/sku_registry.json 的语义化
# sku_id（如 lingox-pro、voicex-starter），详见 ID_SPEC.md §3.2。
PREFIXES: dict[str, str] = {
    "cust": "客户",
    "org": "组织",
    "ord": "订单",
    "lic": "授权",
    "prs": "人设（persona）",
    "evt": "事件",
    "wsp": "工作区（workspace）",
}

# 跨语言已知答案向量（ID_SPEC.md §6）：任何语言的实现对同一输入必须产出逐字节相同的
# 输出。格式：(timestamp_ms, randomness 的 hex, 期望的 26 字符 ULID)。
_TEST_VECTORS = [
    (0, "00000000000000000000", "00000000000000000000000000"),
    (MAX_TIMESTAMP_MS, "ffffffffffffffffffff", "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"),
    # 2026-07-18T00:00:00Z
    (1784332800000, "00112233445566778899", "01KXS8BM00008J4CT4ANK7F24S"),
]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ParsedId(NamedTuple):
    """parse() 的返回值：前缀、UTC 时间戳、随机部分、原始毫秒值。"""

    prefix: str
    timestamp: datetime   # 生成时刻（UTC，毫秒精度）；仅供调试/粗排，业务时间用显式字段
    randomness: bytes     # 80 bit 随机部分（10 字节）
    timestamp_ms: int     # 原始 Unix 毫秒时间戳（无浮点精度损失）


# ── ULID 编解码 ──────────────────────────────────────────────────────────────

def ulid_encode(timestamp_ms: int, randomness: bytes) -> str:
    """把 (毫秒时间戳, 10 字节随机数) 编码为 26 字符 Crockford Base32 大写 ULID。

    高 48 bit 放时间戳、低 80 bit 放随机数，拼成 128 bit 值后按 5 bit 一组
    从高位到低位取字母表字符。26 字符共 130 bit，最高 2 bit 恒为 0，
    因此合法输出的首字符必然落在 0-7。
    """
    if not 0 <= timestamp_ms <= MAX_TIMESTAMP_MS:
        raise ValueError(f"时间戳超出 48 bit 范围 [0, {MAX_TIMESTAMP_MS}]: {timestamp_ms!r}")
    if not isinstance(randomness, (bytes, bytearray)) or len(randomness) != RANDOM_BYTES:
        raise ValueError(f"随机部分必须是 {RANDOM_BYTES} 字节 bytes: {randomness!r}")
    value = (timestamp_ms << 80) | int.from_bytes(randomness, "big")
    return "".join(CROCKFORD_ALPHABET[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def ulid_decode(ulid: str) -> "tuple[int, bytes]":
    """把 26 字符 ULID 解码为 (毫秒时间戳, 10 字节随机数)。

    严格模式：只接受规范形（大写、无 I/L/O/U、无连字符），不做 Crockford 的
    宽松纠错映射；长度错误、非法字符、数值溢出 128 bit 一律抛 ValueError。
    """
    if not isinstance(ulid, str) or len(ulid) != ULID_LEN:
        raise ValueError(f"ULID 必须是 {ULID_LEN} 字符字符串: {ulid!r}")
    value = 0
    for ch in ulid:
        idx = _DECODE_MAP.get(ch)
        if idx is None:
            raise ValueError(f"非法 Crockford Base32 字符 {ch!r}（字母表不含 I/L/O/U，且必须大写）")
        value = (value << 5) | idx
    if value >> 128:
        raise ValueError(f"ULID 数值溢出 128 bit（首字符必须在 0-7）: {ulid!r}")
    return value >> 80, (value & ((1 << 80) - 1)).to_bytes(RANDOM_BYTES, "big")


# ── 对外 API ────────────────────────────────────────────────────────────────

def new_id(prefix: str) -> str:
    """生成一个新的全域统一 ID。prefix 必须已在 PREFIXES 注册，否则抛 ValueError。

    时间戳取本机 Unix 毫秒；随机部分来自 secrets（CSPRNG）。同毫秒内不保证
    单调递增（规范拍板如此，任何实现不得私自加单调逻辑，见 ID_SPEC.md §5）。
    """
    if prefix not in PREFIXES:
        raise ValueError(f"未注册的前缀 {prefix!r}（本期可用: {', '.join(PREFIXES)}）")
    timestamp_ms = time.time_ns() // 1_000_000
    return f"{prefix}_{ulid_encode(timestamp_ms, secrets.token_bytes(RANDOM_BYTES))}"


def is_valid(id_str: object) -> bool:
    """完整两级校验：语法正则 + 前缀已注册 + ULID 数值不溢出 128 bit。

    保证 is_valid(x) 为 True 当且仅当 parse(x) 能成功解析。
    非字符串输入返回 False（不抛异常），便于直接校验外部数据。
    """
    if not isinstance(id_str, str) or ID_RE.fullmatch(id_str) is None:
        return False
    prefix, ulid = id_str.split("_", 1)
    # 正则已保证 ULID 字符都在字母表内，首字符 <= '7' 即等价于数值 < 2^128
    return prefix in PREFIXES and ulid[0] <= "7"


def parse(id_str: str) -> ParsedId:
    """解析 ID，返回 ParsedId(prefix, timestamp, randomness, timestamp_ms)。

    非法输入（格式、前缀、字符、溢出）一律抛 ValueError。
    timestamp 用整数毫秒 + timedelta 构造，避免浮点除法的精度损失。
    """
    if not isinstance(id_str, str) or ID_RE.fullmatch(id_str) is None:
        raise ValueError(f"ID 不符合格式 {ID_PATTERN}: {id_str!r}")
    prefix, ulid = id_str.split("_", 1)
    if prefix not in PREFIXES:
        raise ValueError(f"未注册的前缀 {prefix!r}: {id_str!r}")
    timestamp_ms, randomness = ulid_decode(ulid)
    return ParsedId(prefix, _EPOCH + timedelta(milliseconds=timestamp_ms), randomness, timestamp_ms)


# ── 自测 ────────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """运行全部自测，全部通过返回 0，任一失败返回 1。"""
    failures: "list[str]" = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    print("== 无界统一 ID 自测（ids.py --selftest）==")

    print("[1/6] 契约常量与注册表")
    check("字母表 32 字符、按码点升序、不含 I/L/O/U",
          len(CROCKFORD_ALPHABET) == 32
          and list(CROCKFORD_ALPHABET) == sorted(CROCKFORD_ALPHABET)
          and not set("ILOU") & set(CROCKFORD_ALPHABET))
    check("PREFIXES 为本期拍板的 7 个前缀",
          set(PREFIXES) == {"cust", "org", "ord", "lic", "prs", "evt", "wsp"})
    check("所有前缀符合 ^[a-z]{2,5}$",
          all(re.fullmatch(r"[a-z]{2,5}", p) for p in PREFIXES))

    print("[2/6] 已知答案向量（跨语言逐字节兼容锚点）")
    for ts, rnd_hex, expect in _TEST_VECTORS:
        rnd = bytes.fromhex(rnd_hex)
        check(f"encode({ts}, {rnd_hex}) == {expect}", ulid_encode(ts, rnd) == expect)
        check(f"decode({expect}) 还原为原始输入", ulid_decode(expect) == (ts, rnd))

    print("[3/6] 生成/校验/解析回环（全部 7 个前缀）")
    now = datetime.now(timezone.utc)
    for prefix in PREFIXES:
        id_str = new_id(prefix)
        parsed = parse(id_str)
        ok = (
            is_valid(id_str)
            and parsed.prefix == prefix
            and abs(parsed.timestamp - now) < timedelta(seconds=10)
            and f"{prefix}_{ulid_encode(parsed.timestamp_ms, parsed.randomness)}" == id_str
        )
        check(f"{prefix}: {id_str}", ok)

    print("[4/6] 时间有序（跨毫秒后字典序可排序）")
    first = new_id("evt")
    first_ms = parse(first).timestamp_ms
    deadline = time.monotonic() + 2.0
    # 按规范要求 sleep 2ms；同时确认毫秒值确实前进了（防低分辨率时钟造成假阴性）
    while time.time_ns() // 1_000_000 <= first_ms and time.monotonic() < deadline:
        time.sleep(0.002)
    second = new_id("evt")
    check(f"{first} < {second}", first < second)

    print("[5/6] 唯一性")
    ids = {new_id("evt") for _ in range(10_000)}
    check("连续生成 10000 个 ID 无重复", len(ids) == 10_000)

    print("[6/6] 非法输入拒绝（is_valid 返回 False 且 parse 抛 ValueError）")
    u = "01ARZ3NDEKTSV4RRFFQ69G5FAV"  # 固定的合法 ULID 样例
    check(f"基准样例本身合法: cust_{u}", is_valid(f"cust_{u}") and parse(f"cust_{u}").prefix == "cust")
    bad_cases = [
        (f"zzz_{u}", "未注册前缀 zzz"),
        (f"CUST_{u}", "前缀大写"),
        (f"c_{u}", "前缀过短（1 字母）"),
        (f"customer_{u}", "前缀过长（8 字母）"),
        (f"cust-{u}", "分隔符不是下划线"),
        (f"cust{u}", "缺分隔符"),
        (f"cust_{u[:-1]}", "ULID 长度 25"),
        (f"cust_{u}V", "ULID 长度 27"),
        (f"cust_{u[:-1]}I", "含被排除字符 I"),
        (f"cust_{u[:-1]}L", "含被排除字符 L"),
        (f"cust_{u[:-1]}O", "含被排除字符 O"),
        (f"cust_{u[:-1]}U", "含被排除字符 U"),
        (f"cust_{u.lower()}", "ULID 小写"),
        ("cust_8ZZZZZZZZZZZZZZZZZZZZZZZZZ", "数值溢出 128 bit（首字符 >7）"),
        (f"cust_{u}\n", "结尾混入换行"),
        ("", "空字符串"),
    ]
    for bad, why in bad_cases:
        rejected = not is_valid(bad)
        try:
            parse(bad)
            rejected = False
        except ValueError:
            pass
        check(f"拒绝 {why}: {bad!r}", rejected)
    check("is_valid(None) / is_valid(123) 返回 False", not is_valid(None) and not is_valid(123))
    try:
        new_id("nope")
        check("new_id 拒绝未注册前缀", False)
    except ValueError:
        check("new_id 拒绝未注册前缀", True)

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


_USAGE = """用法:
  python ids.py --selftest    运行自测（编码向量/回环/时间有序/唯一性/非法输入）

作为库使用时注意 platform 目录与标准库同名，加载方式见本文件顶部注释或 ID_SPEC.md。
"""


def main(argv: "list[str]") -> int:
    if argv == ["--selftest"]:
        return _selftest()
    print(_USAGE, end="")
    return 2


if __name__ == "__main__":
    # Windows 下 stdout 重定向到管道/文件时默认用本地代码页（cp936/cp1252 等），
    # 打印中文会 UnicodeEncodeError，这里统一切到 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main(sys.argv[1:]))
