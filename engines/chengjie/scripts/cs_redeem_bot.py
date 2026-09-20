"""客服赠量核销 bot —— 保守自动化（人在环）。

链路：客户把绑定码（BC-XXXX-XXXX）发给客服 → **客服**把码转给这个 bot（或在
客服群里 @ 它）→ bot 调官网 console 核销接口 → 厂商机签发加量凭证 → 客户端
后台轮询自动入账。bot 只是替客服省掉「登录 console 贴码」这一步，**决定权仍
在客服手里**（只有 allowlist 里的客服号发的码才处理，陌生人直接无视）。

防滥用设计（宁可保守）：
* **allowlist 空 = 全拒**。必须显式配置客服的 Telegram user id 才会动手。
* 每日核销封顶（默认 50），到顶只回提示不再核销。
* 官网侧本就幂等：同一码重复核销回 already_redeemed，不会重复入账。
* bot 只用 console 账号（建议单独建一个 admin 角色的 bot 账号），不碰任何签发
  密钥——签发仍在厂商机离线私钥手里。

运行：
    python scripts/cs_redeem_bot.py --config D:\\chengjie-instances\\vendor\\cs_redeem_bot.json
    （--dry-run 只识别不核销；配置样例见 cs_redeem_bot.example.json）

依赖：仅标准库（urllib 长轮询 Telegram Bot API）。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("cs_redeem_bot")

BIND_CODE_RE = re.compile(r"\bBC-([A-Z0-9]{4})-([A-Z0-9]{4})\b", re.IGNORECASE)
HTTP_TIMEOUT = 35
DEFAULT_DAILY_CAP = 50

#: 传输注入点：(url, method, body, headers, timeout) -> {status, json, set_cookie}
Transport = Callable[..., Dict[str, Any]]


def _http(url: str, method: str = "GET", body: Optional[dict] = None,
          headers: Optional[Dict[str, str]] = None, timeout: int = HTTP_TIMEOUT) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return {
                "status": resp.status,
                "json": json.loads(raw) if raw else {},
                "set_cookie": resp.headers.get_all("Set-Cookie") or [],
            }
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        return {"status": e.code, "json": payload, "set_cookie": []}
    except Exception as e:  # noqa: BLE001 - 网络层异常统一收敛
        logger.warning("http 失败 %s: %s", url, e)
        return {"status": 0, "json": {}, "set_cookie": []}


# ── 纯逻辑 ─────────────────────────────────────────────────────────────────

def extract_bind_codes(text: str) -> List[str]:
    """从消息文本抓绑定码：大写规整 + 保序去重。"""
    out: List[str] = []
    for m in BIND_CODE_RE.finditer(str(text or "")):
        code = f"BC-{m.group(1).upper()}-{m.group(2).upper()}"
        if code not in out:
            out.append(code)
    return out


def is_allowed(user_id: Any, allowlist: Any) -> bool:
    """allowlist 空 = 全拒（必须显式配置客服 id 才会动手）。"""
    try:
        ids = {int(x) for x in (allowlist or [])}
    except Exception:
        return False
    try:
        return bool(ids) and int(user_id) in ids
    except Exception:
        return False


def today_key(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def cap_reached(counts: Dict[str, int], day: str, cap: int) -> bool:
    return int(counts.get(day, 0)) >= int(cap)


def format_reply(res: Dict[str, Any], code: str) -> str:
    """核销结果 → 给客服看的一句话。所有分支都要能一眼看懂下一步该干嘛。"""
    if res.get("ok"):
        chars = int(res.get("chars") or 0)
        contact = str(res.get("contact") or "")
        if res.get("already_redeemed"):
            return f"⚠️ {code} 之前已核销过（不会重复入账）。联系方式：{contact or '—'}"
        tail = "，厂商机签发后自动到账" if res.get("pending_voucher") else ""
        return f"✅ 已核销 {code} → {contact or '—'} +{chars:,} 字符{tail}"
    err = str(res.get("error") or "")
    if err == "not_found":
        return f"❌ {code} 不存在——让客户核对下有没有抄错（码在客户端「会员中心」能重新看）"
    if err == "bad_code":
        return f"❌ {code} 格式不对"
    if err == "unauthorized":
        return "❌ console 登录失效且重登失败——检查 bot 的 console 账号密码"
    return f"❌ 核销失败（{err or 'network'}），稍后重试或登录 console 手动核销"


# ── console 会话 ───────────────────────────────────────────────────────────

class ConsoleSession:
    """官网 console 登录态（12h 会话，401 时自动重登一次）。"""

    def __init__(self, site: str, username: str, password: str,
                 transport: Optional[Transport] = None):
        self.site = str(site or "").rstrip("/")
        self.username = username
        self.password = password
        self._t = transport or _http
        self._cookie = ""

    def login(self) -> bool:
        r = self._t(f"{self.site}/api/console/login", "POST",
                    {"username": self.username, "password": self.password})
        if r.get("status") != 200 or not (r.get("json") or {}).get("ok"):
            logger.error("console 登录失败 status=%s", r.get("status"))
            return False
        for line in r.get("set_cookie") or []:
            if "console_session=" in line:
                self._cookie = line.split(";", 1)[0].strip()
                return True
        logger.error("console 登录响应没有会话 cookie")
        return False

    def redeem(self, code: str, chars: Optional[int] = None) -> Dict[str, Any]:
        if not self._cookie and not self.login():
            return {"ok": False, "error": "unauthorized"}
        body: Dict[str, Any] = {"code": code}
        if chars:
            body["chars"] = int(chars)
        r = self._t(f"{self.site}/api/console/trial-redeem", "POST", body,
                    headers={"cookie": self._cookie})
        if r.get("status") == 401:
            # 会话过期（12h TTL）→ 重登一次再试；再失败就如实报 unauthorized。
            self._cookie = ""
            if not self.login():
                return {"ok": False, "error": "unauthorized"}
            r = self._t(f"{self.site}/api/console/trial-redeem", "POST", body,
                        headers={"cookie": self._cookie})
        out = dict(r.get("json") or {})
        if not out and r.get("status") != 200:
            out = {"ok": False, "error": f"http_{r.get('status')}"}
        return out


# ── bot 主体 ───────────────────────────────────────────────────────────────

class RedeemBot:
    def __init__(self, cfg: Dict[str, Any], transport: Optional[Transport] = None):
        self.cfg = cfg
        self.session = ConsoleSession(
            cfg.get("site") or "https://bd2026.cc",
            str(cfg.get("console_user") or ""),
            str(cfg.get("console_pass") or ""),
            transport=transport,
        )
        self.allowlist = cfg.get("allowed_user_ids") or []
        self.daily_cap = int(cfg.get("daily_cap") or DEFAULT_DAILY_CAP)
        self.dry_run = bool(cfg.get("dry_run"))
        self.counts: Dict[str, int] = {}

    def handle_message(self, msg: Dict[str, Any]) -> Optional[str]:
        """一条消息 → 回复文本（None = 不理：非客服 / 没有码）。"""
        from_id = ((msg or {}).get("from") or {}).get("id")
        if not is_allowed(from_id, self.allowlist):
            return None
        codes = extract_bind_codes((msg or {}).get("text") or "")
        if not codes:
            return None
        day = today_key()
        lines: List[str] = []
        for code in codes:
            if cap_reached(self.counts, day, self.daily_cap):
                lines.append(f"⏸️ 已达今日核销上限（{self.daily_cap}），{code} 未处理——"
                             "明天再发，或登录 console 手动核销")
                continue
            if self.dry_run:
                lines.append(f"🧪 试运行：识别到 {code}（未核销）")
                continue
            res = self.session.redeem(code)
            # 幂等重复不占当日预算（already 不产生新入账）
            if res.get("ok") and not res.get("already_redeemed"):
                self.counts[day] = self.counts.get(day, 0) + 1
            lines.append(format_reply(res, code))
        return "\n".join(lines) if lines else None


# ── Telegram 长轮询 ────────────────────────────────────────────────────────

def tg_api(token: str, method: str, body: Optional[dict] = None,
           transport: Optional[Transport] = None, timeout: int = HTTP_TIMEOUT) -> Dict[str, Any]:
    t = transport or _http
    r = t(f"https://api.telegram.org/bot{token}/{method}", "POST", body or {}, timeout=timeout)
    return r.get("json") or {}


def run_loop(cfg: Dict[str, Any], state_file: Path,
             transport: Optional[Transport] = None) -> None:
    bot = RedeemBot(cfg, transport=transport)
    token = str(cfg.get("bot_token") or "")
    offset = 0
    try:
        offset = int(json.loads(state_file.read_text(encoding="utf-8")).get("offset") or 0)
    except Exception:
        pass
    logger.info("cs_redeem_bot 启动（allowlist=%s 个 · 日上限=%s · dry_run=%s）",
                len(bot.allowlist), bot.daily_cap, bot.dry_run)
    while True:
        resp = tg_api(token, "getUpdates",
                      {"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                      transport=transport, timeout=45)
        if not resp.get("ok"):
            time.sleep(5)
            continue
        for upd in resp.get("result") or []:
            offset = max(offset, int(upd.get("update_id") or 0) + 1)
            msg = upd.get("message") or {}
            try:
                reply = bot.handle_message(msg)
            except Exception as e:  # noqa: BLE001 - 单条消息失败不拖垮循环
                logger.exception("处理消息失败: %s", e)
                reply = None
            if reply:
                chat_id = (msg.get("chat") or {}).get("id")
                tg_api(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": reply,
                    "reply_to_message_id": msg.get("message_id"),
                }, transport=transport)
                logger.info("回复 chat=%s: %s", chat_id, reply.replace("\n", " | "))
            try:
                state_file.write_text(json.dumps({"offset": offset}), encoding="utf-8")
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="客服赠量核销 bot（保守自动化）")
    ap.add_argument("--config", required=True, help="配置 JSON 路径（含 bot_token/console 凭证）")
    ap.add_argument("--dry-run", action="store_true", help="只识别不核销")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if args.dry_run:
        cfg["dry_run"] = True
    missing = [k for k in ("bot_token", "console_user", "console_pass") if not cfg.get(k)]
    if missing:
        print(f"配置缺字段: {', '.join(missing)}（参照 cs_redeem_bot.example.json）")
        return 2
    if not cfg.get("allowed_user_ids"):
        print("allowed_user_ids 为空 = 谁的消息都不处理。先把客服的 Telegram user id 填进去。")
        return 2
    run_loop(cfg, cfg_path.with_suffix(".state.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
