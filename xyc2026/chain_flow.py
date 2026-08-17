# 链上流水拉取：TRC20/TON 等，按地址取最近 N 笔，脱敏与缓存
# 用于「绑定收款地址」后展示流水，增强用户间信任
import base64
import re
import time
from typing import Any

# USDT TRC20 合约地址
TRC20_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# 简单内存缓存：(chain, address) -> (expiry_ts, list[tx])
_flow_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
FLOW_CACHE_TTL_SEC = 120  # 2 分钟


def _mask_address(addr: str) -> str:
    if not addr or len(addr) < 12:
        return "***"
    return f"{addr[:6]}...{addr[-4:]}"


def _validate_trc20(address: str) -> bool:
    # Tron Base58 34 字符
    if not address or len(address) != 34:
        return False
    return bool(re.match(r"^T[A-HJ-NP-Za-km-z1-9]{33}$", address.strip()))


def _validate_ton(address: str) -> bool:
    # TON 常见格式：EQ 开头 base64 或 0: 开头 hex
    if not address or len(address) < 40:
        return False
    s = address.strip()
    if s.startswith("EQ") and len(s) <= 64:
        return bool(re.match(r"^EQ[0-9a-zA-Z_-]{46,48}$", s))
    if s.startswith("0:") or s.startswith("1:"):
        return bool(re.match(r"^[01]:[0-9a-fA-F]{64}$", s))
    return False


def validate_address(chain: str, address: str) -> bool:
    a = (address or "").strip()[:128]
    if chain == "TRC20":
        return _validate_trc20(a)
    if chain == "TON":
        return _validate_ton(a)
    return False


async def fetch_trc20_flow(address: str, limit: int = 30, api_key: str = "") -> list[dict[str, Any]]:
    """拉取 TRC20 USDT 转账记录；value 为 sun，需 /1e6 为 USDT。"""
    import httpx
    base = "https://api.trongrid.io"
    url = f"{base}/v1/accounts/{address}/transactions/trc20"
    params = {
        "limit": min(limit, 50),
        "only_confirmed": "true",
        "contract_address": TRC20_USDT_CONTRACT,
        "order_by": "block_timestamp,desc",
    }
    headers = {}
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    out: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params, headers=headers or None)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return out
    items = data.get("data") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return out
    decimals = 6  # USDT
    for tx in items[:limit]:
        if not isinstance(tx, dict):
            continue
        from_addr = tx.get("from") or ""
        to_addr = tx.get("to") or ""
        value_str = tx.get("value") or "0"
        try:
            value_sun = int(value_str)
        except (TypeError, ValueError):
            value_sun = 0
        amount = round(value_sun / (10**decimals), 2)
        ts_ms = tx.get("block_timestamp") or 0
        try:
            ts_ms = int(ts_ms)
        except (TypeError, ValueError):
            ts_ms = 0
        # 相对当前地址的方向
        addr_lower = address.strip().lower()
        if (to_addr or "").lower() == addr_lower:
            direction = "in"
            counterparty = from_addr
        else:
            direction = "out"
            counterparty = to_addr
        out.append({
            "direction": direction,
            "amount": amount,
            "currency": "USDT",
            "counterparty_masked": _mask_address(counterparty),
            "time": ts_ms,
            "tx_hash": (tx.get("transaction_id") or "")[:16],
            "chain": "TRC20",
        })
    return out


def _ton_normalize_for_compare(addr: str) -> str:
    """TON 地址归一化便于比较：raw 形式 0:hex/1:hex 统一为小写 hex，EQ 形式仅 strip。"""
    s = (addr or "").strip()
    if not s:
        return s
    m = re.match(r"^([01]):([0-9a-fA-F]+)$", s)
    if m:
        return f"{m.group(1)}:{m.group(2).lower()}"
    return s


def _ton_eq_to_raw(eq: str) -> str | None:
    """TON EQ (base64) 转 raw 形式 0:hex。解码后 36 字节：flags(1), workchain(1), hash(32), crc(2)。"""
    s = (eq or "").strip()
    if not s.startswith("EQ") or len(s) < 40:
        return None
    try:
        # TON 使用 URL-safe base64，补足 padding
        pad = (4 - len(s) % 4) % 4
        raw_bytes = base64.urlsafe_b64decode(s + "=" * pad)
    except Exception:
        return None
    if len(raw_bytes) < 34:
        return None
    workchain = raw_bytes[1] if raw_bytes[1] < 128 else raw_bytes[1] - 256
    hash_hex = raw_bytes[2:34].hex()
    return f"{workchain}:{hash_hex}"


def _ton_raw_to_eq(workchain: int, hash_hex: str) -> str:
    """raw 形式转 EQ（无 CRC 的简单形式，仅用于比较时生成候选）。不生成标准 bounceable，仅用于内部比对。"""
    if len(hash_hex) < 64:
        hash_hex = hash_hex.zfill(64)
    try:
        body = bytes([0x11, workchain & 0xFF]) + bytes.fromhex(hash_hex[:64])
    except Exception:
        return ""
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii", errors="ignore")


def _ton_addresses_equal(a: str, b: str) -> bool:
    if not a or not b:
        return a == b
    na = _ton_normalize_for_compare(a)
    nb = _ton_normalize_for_compare(b)
    if na == nb:
        return True
    # 若一方为 EQ、一方为 raw，尝试 EQ 转 raw 再比
    if a.strip().startswith("EQ") and re.match(r"^[01]:[0-9a-fA-F]+$", nb):
        raw_a = _ton_eq_to_raw(a)
        return raw_a is not None and _ton_normalize_for_compare(raw_a) == nb
    if b.strip().startswith("EQ") and re.match(r"^[01]:[0-9a-fA-F]+$", na):
        raw_b = _ton_eq_to_raw(b)
        return raw_b is not None and _ton_normalize_for_compare(raw_b) == na
    return False


async def fetch_ton_flow(address: str, limit: int = 30, api_key: str = "") -> list[dict[str, Any]]:
    """拉取 TON 链上转账记录（TON Center API v2 getTransactions）；value 为 nanoton，/1e9 为 TON。"""
    import httpx
    base = "https://toncenter.com/api/v2"
    url = f"{base}/getTransactions"
    params = {"address": address, "limit": min(limit, 50)}
    if api_key:
        params["api_key"] = api_key
    out: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return out
    if not data.get("ok") or "result" not in data:
        return out
    result = data["result"]
    if not isinstance(result, list):
        return out
    our_norm = _ton_normalize_for_compare(address)
    NANOTON_PER_TON = 1_000_000_000
    for tx in result[:limit]:
        if not isinstance(tx, dict):
            continue
        utime = 0
        if "utime" in tx:
            try:
                utime = int(tx["utime"]) * 1000
            except (TypeError, ValueError):
                pass
        in_msg = tx.get("in_msg")
        if isinstance(in_msg, dict) and in_msg:
            val = in_msg.get("value") or 0
            try:
                val = int(val)
            except (TypeError, ValueError):
                val = 0
            dest = in_msg.get("destination") or ""
            src = in_msg.get("source") or ""
            if _ton_addresses_equal(dest, address) and val > 0:
                amount = round(val / NANOTON_PER_TON, 4)
                out.append({
                    "direction": "in",
                    "amount": amount,
                    "currency": "TON",
                    "counterparty_masked": _mask_address(src),
                    "time": utime,
                    "tx_hash": (tx.get("transaction_id") or str(tx.get("hash", "")))[:16],
                    "chain": "TON",
                })
        for out_msg in tx.get("out_msgs") or []:
            if not isinstance(out_msg, dict):
                continue
            val = out_msg.get("value") or 0
            try:
                val = int(val)
            except (TypeError, ValueError):
                val = 0
            if val <= 0:
                continue
            src = out_msg.get("source") or ""
            dest = out_msg.get("destination") or ""
            if _ton_addresses_equal(src, address):
                amount = round(val / NANOTON_PER_TON, 4)
                out.append({
                    "direction": "out",
                    "amount": amount,
                    "currency": "TON",
                    "counterparty_masked": _mask_address(dest),
                    "time": utime,
                    "tx_hash": (tx.get("transaction_id") or str(tx.get("hash", "")))[:16],
                    "chain": "TON",
                })
    out.sort(key=lambda x: (x.get("time") or 0), reverse=True)
    return out[:limit]


async def fetch_flow_uncached(
    chain: str, address: str, limit: int = 30, api_key: str = "", ton_api_key: str | None = None
) -> list[dict[str, Any]]:
    if chain == "TRC20":
        return await fetch_trc20_flow(address, limit=limit, api_key=api_key)
    if chain == "TON":
        key = (ton_api_key or "").strip() or api_key
        return await fetch_ton_flow(address, limit=limit, api_key=key)
    return []


async def get_flow_for_address(
    chain: str, address: str, limit: int = 30, api_key: str = "", ton_api_key: str | None = None
) -> list[dict[str, Any]]:
    """带缓存的流水拉取。"""
    key = (chain, address.strip().lower())
    now = time.time()
    if key in _flow_cache:
        expiry, cached = _flow_cache[key]
        if now < expiry:
            return cached[:limit]
    try:
        rows = await fetch_flow_uncached(chain, address, limit=limit, api_key=api_key, ton_api_key=ton_api_key)
    except Exception:
        rows = []
    _flow_cache[key] = (now + FLOW_CACHE_TTL_SEC, rows)
    return rows[:limit]
