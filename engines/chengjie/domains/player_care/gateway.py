"""玩家网关客户端（player_gateway）——只读事实的唯一来源。

接口（运营方网关，2026-09-20 老板给的口径）：
    POST {url}/lookup   请求头 X-Gateway-Key: <key>
    JSON: {"q": <客户消息>, "phone": <可选>, "uid": <可选>}
    成功（200）→ ``{ok, query, phone_hits, missing_phones, players[], chatx_text}``：
      ``chatx_text`` 是给模型的中文摘要（每个玩家一行 ``UID x / 名 / VIP / 状态；余额…；
      充 总额×次数，提 …；总投注…；打码…。 风险：…``），**游戏不在文本里**，在
      ``players[i].top_games[] = {platform, game, bet_amount, last_played_at, …}``；
      代理号在 ``players[i].agent``；充值在 ``players[i].has_deposited / deposit_count``。
      同一手机号多账号 → 200 + ``multi: true`` + ``candidates``，无 chatx_text（按没查到）。
    404 = 没这个玩家；401 = 密钥错；400 = 空查询。查不到或超时 → 当没资料，绝不编数字。
    （2026-09-21 真网关三探针定稿，样例见 tests/fixtures/player_care_lookup_samples.json）
    手机号 9 开头 / 补 0 / 补 63 网关都认；我们自己归一成 ``639xxxxxxxxx`` 用来对账。

纪律（与 companion.goals.order_pull 同一套「新子系统约定」）：
    - 默认关：``player_gateway.enabled`` 不为真、或 url / key 缺失 → ``lookup`` 直接返回
      ``configured=False`` 的空结果，不发网络请求；
    - 永不抛：网络 / 解析 / 鉴权异常一律收敛成 ``LookupResult(ok=False, error=...)``；
    - 密钥只从配置 ``player_gateway.key`` 或环境变量（默认 ``GATEWAY_KEY``）读，不落日志；
    - 单联系人短 TTL 缓存（默认 60 s），一句话里多次触发不重复打网关。

与另一台电脑 chengjie 树里的 ``wujie_player.py`` 用同一组配置键
（``player_gateway.enabled / url / key``），两边以后合并不打架。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("PlayerGateway")

DEFAULT_TIMEOUT_SEC = 4.0
DEFAULT_CACHE_TTL_SEC = 60.0
DEFAULT_KEY_ENV = "GATEWAY_KEY"
HEADER_KEY = "X-Gateway-Key"

# ── 手机号（菲律宾移动号）归一 ─────────────────────────────────────────────
# 接受：9xxxxxxxxx（10 位）/ 09xxxxxxxxx（11 位）/ 639xxxxxxxxx（12 位）/ +63 9xx…；
# 分隔符（空格 / - / . / 括号）一律剥掉。归一结果固定 12 位 ``639xxxxxxxxx``。
_PH_MOBILE_RE = re.compile(
    r"(?<!\d)(?:\+?63[\s\-.]?|0)?9\d{2}[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)"
)
_UID_RE = re.compile(
    r"(?:\b(?:uid|u\.?id|member\s*(?:id|no\.?|number)|account\s*(?:id|no\.?|number)|"
    r"player\s*id|user\s*id|id)(?![A-Za-z]))\s*[:：#\-]?\s*([A-Za-z0-9]{4,20})",
    re.IGNORECASE,
)
_NUMBER_TOKEN_RE = re.compile(r"\d[\d,\.]*")


def normalize_ph_phone(raw: Any) -> str:
    """任意写法 → ``639xxxxxxxxx``；不是菲律宾移动号返回空串。"""
    s = re.sub(r"[\s\-\.\(\)]", "", str(raw or ""))
    if not s:
        return ""
    if s.startswith("+"):
        s = s[1:]
    # WhatsApp JID：639xxxxxxxxx@s.whatsapp.net / @c.us
    if "@" in s:
        s = s.split("@", 1)[0]
    if not s.isdigit():
        return ""
    if len(s) == 12 and s.startswith("639"):
        return s
    if len(s) == 11 and s.startswith("09"):
        return "63" + s[1:]
    if len(s) == 10 and s.startswith("9"):
        return "63" + s
    return ""


def phone_variants(canonical: str) -> Tuple[str, str, str]:
    """``639xxxxxxxxx`` → (9…, 09…, 639…)，对账 / 展示用。"""
    c = normalize_ph_phone(canonical)
    if not c:
        return ("", "", "")
    local = c[2:]
    return (local, "0" + local, c)


def extract_phone(text: Any) -> str:
    """从自由文本里抽第一个菲律宾移动号（归一后）。"""
    for m in _PH_MOBILE_RE.finditer(str(text or "")):
        p = normalize_ph_phone(m.group(0))
        if p:
            return p
    return ""


def extract_uid(text: Any) -> str:
    """从自由文本里抽会员号：必须带 uid / member id / account no 之类前缀，
    否则任何数字串都可能被当成会员号（手机号、金额）。手机号样式不算 uid。"""
    for m in _UID_RE.finditer(str(text or "")):
        cand = m.group(1).strip()
        if normalize_ph_phone(cand):
            continue
        if cand.isdigit() and len(cand) >= 10:
            continue  # 十位以上纯数字更像电话 / 流水号
        return cand
    return ""


def number_tokens(text: Any) -> List[str]:
    """文本里的数字 token（去掉千分位与末尾标点），供数字闸对账。"""
    out: List[str] = []
    for m in _NUMBER_TOKEN_RE.finditer(str(text or "")):
        tok = m.group(0).replace(",", "").rstrip(".")
        if tok:
            out.append(tok)
    return out


def numbers_not_in_facts(reply: Any, facts: Any) -> List[str]:
    """回复里出现、事实里没有的数字（只看 ≥3 位或含小数点的，1–2 位数字放行：
    「2 araw」「3 games」这类日常数字不该被闸）。"""
    fact_set = set(number_tokens(facts))
    fact_digits = {t.replace(".", "") for t in fact_set}
    bad: List[str] = []
    for tok in number_tokens(reply):
        plain = tok.replace(".", "")
        if len(plain) < 3 and "." not in tok:
            continue
        if tok in fact_set or plain in fact_digits:
            continue
        bad.append(tok)
    return bad


# ── 配置 ────────────────────────────────────────────────────────────────────

def resolve_gateway_cfg(cfg_root: Any) -> Dict[str, Any]:
    """根配置 ``player_gateway`` 段 → 规范化 dict（缺省 = 关）。

    ``key`` 优先取配置里的明文；为空则读环境变量 ``key_env``（默认 GATEWAY_KEY）。
    """
    raw: Dict[str, Any] = {}
    try:
        cfg = getattr(cfg_root, "config", cfg_root)
        if isinstance(cfg, dict):
            raw = dict(cfg.get("player_gateway") or {})
    except Exception:
        raw = {}
    key_env = str(raw.get("key_env") or DEFAULT_KEY_ENV).strip() or DEFAULT_KEY_ENV
    key = str(raw.get("key") or "").strip() or str(os.environ.get(key_env) or "").strip()
    url = str(raw.get("url") or "").strip().rstrip("/")

    def _num(name: str, default: float) -> float:
        try:
            v = float(raw.get(name, default))
            return v if v > 0 else default
        except Exception:
            return default

    return {
        "enabled": bool(raw.get("enabled", False)),
        "url": url,
        "key": key,
        "key_env": key_env,
        "timeout_sec": _num("timeout_sec", DEFAULT_TIMEOUT_SEC),
        "cache_ttl_sec": _num("cache_ttl_sec", DEFAULT_CACHE_TTL_SEC),
        "lookup_path": str(raw.get("lookup_path") or "/lookup"),
    }


# ── 结果 ────────────────────────────────────────────────────────────────────

@dataclass
class LookupResult:
    ok: bool = False              # 网关正常应答（HTTP 2xx + JSON 可解析）
    found: bool = False           # ok 且 chatx_text 非空 = 查到资料
    configured: bool = True       # False = 未启用 / 缺 url / 缺 key，未发请求
    chatx_text: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""               # timeout / http_401 / bad_json / ...
    status: int = 0
    latency_ms: int = 0
    cached: bool = False
    multi: bool = False           # 同一手机号下多个账号，网关只给候选、不给资料
    candidates: List[str] = field(default_factory=list)   # multi 时的候选 UID（只存 uid，不存昵称等）

    @property
    def usable(self) -> bool:
        return self.ok and self.found and bool(self.chatx_text.strip())


# ── 客户端 ──────────────────────────────────────────────────────────────────

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]


def _urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310（内网网关，url 来自配置）
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return int(e.code), payload


class PlayerGateway:
    """``lookup(q, phone, uid)`` 的同步客户端；hook 里用 ``asyncio.to_thread`` 包一层。"""

    def __init__(self, cfg: Dict[str, Any], *, transport: Optional[Transport] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = dict(cfg or {})
        self._transport = transport or _urllib_transport
        self._clock = clock
        self._cache: Dict[Tuple[str, str, str], Tuple[float, LookupResult]] = {}
        self.calls = 0
        self.cache_hits = 0

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("enabled") and self.cfg.get("url") and self.cfg.get("key"))

    def lookup(self, q: str, *, phone: str = "", uid: str = "") -> LookupResult:
        if not self.configured:
            return LookupResult(ok=False, configured=False, error="not_configured")
        phone_n = normalize_ph_phone(phone) or ""
        uid_s = str(uid or "").strip()
        q_s = str(q or "").strip()[:500]
        if not (phone_n or uid_s or q_s):
            return LookupResult(ok=False, error="empty_query")

        ck = (phone_n, uid_s, q_s if not (phone_n or uid_s) else "")
        ttl = float(self.cfg.get("cache_ttl_sec") or DEFAULT_CACHE_TTL_SEC)
        now = self._clock()
        hit = self._cache.get(ck)
        if hit and now - hit[0] < ttl:
            self.cache_hits += 1
            r = hit[1]
            return LookupResult(**{**r.__dict__, "cached": True})

        payload: Dict[str, Any] = {"q": q_s or "player info"}
        if phone_n:
            payload["phone"] = phone_n
        if uid_s:
            payload["uid"] = uid_s
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", HEADER_KEY: str(self.cfg["key"])}
        url = f"{self.cfg['url']}{self.cfg.get('lookup_path') or '/lookup'}"
        timeout = float(self.cfg.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)

        t0 = time.monotonic()
        self.calls += 1
        try:
            status, raw = self._transport(url, headers, body, timeout)
        except Exception as e:  # timeout / connection refused / dns
            name = type(e).__name__
            err = "timeout" if "timeout" in name.lower() or "timed out" in str(e).lower() else f"transport_{name}"
            logger.debug("[player_gateway] transport error: %s", e)
            return LookupResult(ok=False, error=err, latency_ms=int((time.monotonic() - t0) * 1000))
        latency = int((time.monotonic() - t0) * 1000)

        if status == 401 or status == 403:
            logger.warning("[player_gateway] 鉴权失败 http_%s（检查 player_gateway.key / %s）", status, self.cfg.get("key_env"))
            return LookupResult(ok=False, error=f"http_{status}", status=status, latency_ms=latency)
        if status == 404:
            # 有的网关用 404 表示「没这个玩家」；按查不到处理但 ok=True（网关是通的）
            res = LookupResult(ok=True, found=False, status=404, latency_ms=latency, error="")
            self._cache[ck] = (now, res)
            return res
        if status < 200 or status >= 300:
            return LookupResult(ok=False, error=f"http_{status}", status=status, latency_ms=latency)

        try:
            data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            return LookupResult(ok=False, error="bad_json", status=status, latency_ms=latency)
        if not isinstance(data, dict):
            return LookupResult(ok=False, error="bad_json", status=status, latency_ms=latency)

        text = str(data.get("chatx_text") or "").strip()
        explicit_fail = data.get("ok") is False or data.get("success") is False or bool(data.get("error"))
        multi = data.get("multi") is True and not text
        res = LookupResult(ok=True, found=bool(text) and not explicit_fail, chatx_text=text,
                           raw=data, status=status, latency_ms=latency,
                           error=str(data.get("error") or "") if explicit_fail else "",
                           multi=multi, candidates=candidate_uids(data) if multi else [])
        self._cache[ck] = (now, res)
        return res


# ── 结构化事实（真样例定稿：优先读 players[]，文本正则只作兜底）─────────────

def candidate_uids(source: Any) -> List[str]:
    """multi 应答里的候选 UID（去重、保序）；候选项其余字段（昵称 / 代理等）不外露。"""
    raw = source.raw if isinstance(source, LookupResult) else source
    if not isinstance(raw, dict):
        return []
    out: List[str] = []
    for c in (raw.get("candidates") or []):
        u = str((c.get("uid") if isinstance(c, dict) else c) or "").strip()
        if u and u not in out:
            out.append(u)
    return out


def _players(source: Any) -> List[Dict[str, Any]]:
    raw = source.raw if isinstance(source, LookupResult) else source
    if not isinstance(raw, dict):
        return []
    return [p for p in (raw.get("players") or []) if isinstance(p, dict)]


def extract_agent(source: Any) -> str:
    """``players[0].agent``（代理号 / 渠道码）；没有返回空串。"""
    for p in _players(source):
        a = str(p.get("agent") or "").strip()
        if a:
            return a
    return ""


def has_deposit(source: Any) -> bool:
    """任一玩家 ``has_deposited`` 为真或 ``deposit_count`` / ``deposit_total`` > 0（只看事实）。"""
    for p in _players(source):
        if p.get("has_deposited") is True:
            return True
        for k in ("deposit_count", "deposit_total"):
            try:
                if float(p.get(k) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


_RISK_TAIL_RE = re.compile(r"\s*风险[:：].*$", re.MULTILINE)
_WRAPPER_LINE_RE = re.compile(r"^\s*[【（(].*[】）)]\s*$")


def friend_facts_text(source: Any) -> str:
    """给「朋友」人设看的事实：去掉网关的客服式包头包尾（【玩家后台资料…】/（余额…为准…））
    和每行的「风险：同IP关联 / 禁提…」尾巴——风控是后台的事，朋友不该知道也不该说。
    数字（余额 / 充提 / 打码 / 时间）原样保留，数字闸照旧只放行事实里出现过的数。"""
    text = source.chatx_text if isinstance(source, LookupResult) else str(source or "")
    keep: List[str] = []
    for ln in str(text).splitlines():
        if not ln.strip() or _WRAPPER_LINE_RE.match(ln):
            continue
        keep.append(_RISK_TAIL_RE.sub("", ln).rstrip())
    return "\n".join(keep).strip()


# ── 事实文本的轻解析（真样例里游戏不在文本，正则只兜底别家网关格式）────────

_GAME_LINE_RE = re.compile(
    r"(?:platform|厂商|供应商|provider)\s*[:：]\s*(?P<platform>[^\n,;，；|]+?)\s*[,;，；|/]?\s*"
    r"(?:game|游戏)\s*[:：]\s*(?P<game>[^\n,;，；|]+)",
    re.IGNORECASE,
)
_GAME_ONLY_RE = re.compile(r"(?:game|游戏)\s*[:：]\s*(?P<game>[^\n,;，；|]+)", re.IGNORECASE)


def extract_games(source: Any, limit: int = 5) -> List[Dict[str, str]]:
    """抽「厂商 / 游戏名」对（不含任何数字字段）。抽不到返回空表。

    ``source`` 可以是 LookupResult / 返回体 dict（读 ``players[].top_games[]``，真网关格式）
    或 chatx_text 字符串（``platform: X, game: Y`` 正则兜底）。"""
    out: List[Dict[str, str]] = []
    seen = set()
    for p in _players(source):
        for g in (p.get("top_games") or []):
            if not isinstance(g, dict):
                continue
            name = str(g.get("game") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append({"platform": str(g.get("platform") or "").strip(), "game": name})
    if out:
        return out[:limit]
    if isinstance(source, LookupResult):
        text = source.chatx_text
    elif isinstance(source, dict):
        text = str(source.get("chatx_text") or "")
    else:
        text = str(source or "")
    for m in _GAME_LINE_RE.finditer(text):
        g = m.group("game").strip()
        p = m.group("platform").strip()
        if g and g.lower() not in seen:
            seen.add(g.lower())
            out.append({"platform": p, "game": g})
    if not out:
        for m in _GAME_ONLY_RE.finditer(text):
            g = m.group("game").strip()
            if g and g.lower() not in seen:
                seen.add(g.lower())
                out.append({"platform": "", "game": g})
    return out[:limit]
