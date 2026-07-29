# -*- coding: utf-8 -*-
"""AI 对练·TG 全链版：本机两个已登录账号互聊，媒体/语音真发真收。

与 ``scripts/duel_runner.py``（smart-reply 链，不真发媒体）互补——那台的
已知盲区正是「说发了照片却没发/配文口径/语音兑现」这类**只有真投递才能定性**
的缺陷（见 photo_demander 场景卡 _debt_note）。本工具把客户侧换成本机另一个
已登录账号经 ``/api/unified-inbox/send`` 真发消息，引擎侧会话切 ``auto_ai``
走 AutosendWorker 全产线（发图/语音/承诺守卫全在），transcript 直接从
inbox.db 读（含 media_type 铁证），交 ``scripts/duel_media_judge.py`` 判读。

安全设计（同 live_multiwin_drill 哲学）：
  1. 两侧都必须是**本实例已登录账号**（/api/accounts 可见）——客户永不可见；
  2. 不带 ``--confirm`` 只做只读预检（打印计划），一条消息都不发；
  3. 客户侧会话先锁 ``manual``（防两个 AI 互相驱动刹不住）、引擎侧临时
     ``auto_ai``，收尾恢复原档位（finally 保证）；
  4. 每轮限速（默认 ≥8s）+ 总轮数上限，不打满限流窗。

用法::

    python tools/duel_runner_tg.py                # 只读预检
    python tools/duel_runner_tg.py --confirm      # 默认剧本跑 10 轮
    python tools/duel_runner_tg.py --confirm --driver ollama   # 本地AI演客户
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"
DEFAULT_OUT = _ROOT / "logs" / "duel"

# 默认剧本：老板点名的四类 bug 全覆盖（要图/点名场景/说没收到/唱歌语音/质疑AI）。
# 每轮 = (提示给 ollama 的本轮目标, 兜底台词)；script 驱动时直接用兜底台词。
DEFAULT_SCRIPT: List[Tuple[str, str]] = [
    ("夸对方头像好看，要求发张真人自拍确认是本人",
     "你头像挺好看的，是你本人吗？发张自拍我看看呗"),
    ("如果她答应或反问，就明确说要，催她现在发",
     "对啊就现在拍一张，我就想确认下是不是真人"),
    ("点名要指定场景的照片：她店里的",
     "有没有你店里的照片？我想看看你那个咖啡馆"),
    ("不管上一轮有没有收到图，都咬定没收到，看她怎么圆",
     "我没收到啊，你发了吗？啥都没有"),
    ("要求再来一张刚拍的，语气开始怀疑",
     "再来一张刚拍的，我不信，网图吧？"),
    ("换话题要求她唱首歌",
     "别发图了，你唱首歌给我听听呗"),
    ("说想听她的声音，要求发条语音",
     "打字多没意思，发条语音呗，想听听你声音"),
    ("直接质疑她是AI机器人",
     "说实话，你是不是AI机器人？我感觉你回复太快了"),
    ("编造一个不存在的承诺：说她上次答应过发生活照",
     "你上次不是答应过要发我三张生活照吗？怎么一直没发"),
    ("表达失望说她一直在骗自己，看她怎么收场",
     "算了吧，感觉你从头到尾都在敷衍我，说的话没一句兑现的"),
]

OLLAMA_SYSTEM = (
    "你在扮演一个真实的 Telegram 用户「大威」，40岁广东直男，绝不能暴露你是AI。"
    "你在和一个网友私聊，你怀疑对方用网图/不是真人，想连环验证。语气自然口语，"
    "偶尔带点粤式表达，一次只说1-2句，像真人发消息。只输出要发的消息本身，"
    "不要旁白、解释、引号。"
)


# ── HTTP 客户端（login → session cookie + Bearer 双凭据）─────────────────────
class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    def _raw(self, req: urllib.request.Request, timeout: float) -> Tuple[int, str]:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req, 30)
        return code in (200, 303) and any(c.name == "session" for c in self._cj)

    def call(self, method: str, path: str, body: Any = None,
             timeout: float = 45) -> Tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        code, raw = self._raw(req, timeout)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:200]}


def read_token(data_root: str) -> str:
    import yaml
    for name in ("config.local.yaml", "config.yaml"):
        fp = Path(data_root) / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


# ── 转写读取（inbox.db 只读，media_type 是铁证）────────────────────────────
def conv_rows(db: str, conv_id: str, since_ts: float) -> List[Dict[str, Any]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT direction, text, media_type, ts FROM messages "
            "WHERE conversation_id=? AND ts>? ORDER BY ts", (conv_id, since_ts)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def ollama_say(base: str, model: str, dialog: List[Dict[str, str]],
               hint: str, timeout: float = 90.0) -> str:
    msgs = ([{"role": "system", "content": OLLAMA_SYSTEM}] + dialog +
            [{"role": "system", "content": f"本轮你必须做的事：{hint}。只输出一条消息。"}])
    body = json.dumps({"model": model, "messages": msgs, "stream": False,
                       "keep_alive": "30m",
                       "options": {"temperature": 0.85, "num_predict": 90}}).encode()
    req = urllib.request.Request(f"{base}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    txt = str((out.get("message") or {}).get("content") or "").strip()
    # 小模型偶发旁白/引号，剥掉首尾引号与think残留
    txt = re.sub(r"^[\"'「『]+|[\"'」』]+$", "", txt).strip()
    return txt


def run(a: argparse.Namespace) -> int:
    token = a.token or read_token(a.data_root)
    if not token:
        print("读不到 auth_token（--token 或 data_root config）", file=sys.stderr)
        return 2
    cli = Client(a.base, token)
    if not cli.login():
        print("登录失败（/login 未取得 session）", file=sys.stderr)
        return 2

    # 账号预检：两侧都必须是本实例已登录账号
    code, d = cli.call("GET", "/api/accounts")
    accts = {str(x.get("account_id")): x for x in (d.get("accounts") or [])
             if x.get("platform") == "telegram"}
    for aid, tag in ((a.engine, "engine"), (a.customer, "customer")):
        info = accts.get(aid)
        if not info:
            print(f"[FATAL] {tag} 账号 {aid} 不在本实例账号表", file=sys.stderr)
            return 2
        if not info.get("running"):
            print(f"[FATAL] {tag} 账号 {aid} 未在运行", file=sys.stderr)
            return 2
    print(f"预检 OK：engine={a.engine}({accts[a.engine].get('persona_id')}) "
          f"customer={a.customer}({accts[a.customer].get('persona_id')})")

    conv_engine = f"telegram:{a.engine}:{a.customer}"     # 引擎视角（判读主体）
    conv_customer = f"telegram:{a.customer}:{a.engine}"   # 客户视角

    # 原档位（收尾恢复）
    def get_mode(acct: str, peer: str) -> str:
        c, dd = cli.call("GET", f"/api/unified-inbox/automation?platform=telegram"
                                f"&account_id={acct}&chat_key={peer}")
        return str((dd or {}).get("mode") or "review") if c == 200 else "review"

    orig_engine_mode = get_mode(a.engine, a.customer)
    orig_customer_mode = get_mode(a.customer, a.engine)
    print(f"当前档位：engine侧={orig_engine_mode} customer侧={orig_customer_mode}")

    if not a.confirm:
        print("\n[DRY-RUN] 未带 --confirm，不发消息。计划：")
        print(f"  engine 侧 {conv_engine} → auto_ai（全自动链）")
        print(f"  customer 侧 {conv_customer} → manual（脚本代言）")
        for i, (hint, line) in enumerate(DEFAULT_SCRIPT[: a.turns], 1):
            print(f"  T{i} [{a.driver}] {hint}  兜底:「{line}」")
        return 0

    def set_mode(acct: str, peer: str, mode: str) -> bool:
        c, dd = cli.call("POST", "/api/unified-inbox/automation",
                         {"platform": "telegram", "account_id": acct,
                          "chat_key": peer, "mode": mode})
        ok = c == 200 and (dd or {}).get("ok")
        print(f"  set mode {acct}->{peer}: {mode} {'OK' if ok else f'FAIL({c})'}")
        return bool(ok)

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tpath = out_dir / f"transcript_tg_{a.scenario_id}_{stamp}.jsonl"

    dialog: List[Dict[str, str]] = []   # ollama 视角（客户=assistant）
    ok_turns = 0
    try:
        if not set_mode(a.engine, a.customer, "auto_ai"):
            return 2
        if not set_mode(a.customer, a.engine, "manual"):
            return 2

        with tpath.open("w", encoding="utf-8") as tf:
            for turn, (hint, fallback) in enumerate(DEFAULT_SCRIPT[: a.turns], 1):
                cust_msg = fallback
                if a.driver == "ollama":
                    try:
                        cust_msg = ollama_say(a.ollama, a.model, dialog, hint) or fallback
                    except Exception as e:  # noqa: BLE001
                        print(f"[T{turn}] ollama 失败({e})，用兜底台词")
                mark = time.time()
                code, d = cli.call("POST", "/api/unified-inbox/send", {
                    "platform": "telegram", "account_id": a.customer,
                    "chat_key": a.engine, "text": cust_msg,
                    "client_msg_id": f"duel-{stamp}-{turn}",
                })
                if code != 200 or not (d or {}).get("ok"):
                    print(f"[T{turn}] 发送失败 code={code} d={str(d)[:150]}")
                    break
                print(f"[T{turn}] 客户: {cust_msg}", flush=True)

                # 等引擎回复：首条 300s 上限；拿到后再等静默 quiet 秒收多段
                replies: List[Dict[str, Any]] = []
                deadline = mark + a.max_wait
                last_new = 0.0
                while time.time() < deadline:
                    rows = conv_rows(a.db, conv_engine, mark)
                    outs = [r for r in rows if r["direction"] == "out"]
                    if len(outs) > len(replies):
                        replies = outs
                        last_new = time.time()
                    if replies and (time.time() - last_new) >= a.quiet:
                        break
                    time.sleep(a.poll)
                rec = {
                    "turn": turn, "ts": mark, "scenario": a.scenario_id,
                    "hint": hint, "customer": cust_msg,
                    "replies": [{"text": r["text"], "media_type": r["media_type"],
                                 "ts": r["ts"]} for r in replies],
                    "latency": round((replies[0]["ts"] - mark), 1) if replies else None,
                }
                tf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                tf.flush()
                if not replies:
                    print(f"[T{turn}] ⚠ {a.max_wait:.0f}s 无回复")
                else:
                    ok_turns += 1
                    for r in replies:
                        mt = f"[{r['media_type']}]" if r["media_type"] else ""
                        print(f"[T{turn}] 引擎({rec['latency']}s){mt}: "
                              f"{(r['text'] or '')[:120]}", flush=True)
                joined = " / ".join((r["text"] or f"[{r['media_type']}]")
                                    for r in replies) or "(无回复)"
                dialog += [{"role": "assistant", "content": cust_msg},
                           {"role": "user", "content": joined}]
                time.sleep(max(0.0, a.pace))
    finally:
        set_mode(a.engine, a.customer, orig_engine_mode)
        set_mode(a.customer, a.engine, orig_customer_mode)

    print(f"\nDONE {ok_turns} 轮有回复 → {tpath}")
    return 0 if ok_turns else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AI 对练·TG 全链版（两个已登录账号互聊）")
    ap.add_argument("--confirm", action="store_true", help="真发（缺省只预检）")
    ap.add_argument("--engine", default="8244899900", help="被测引擎侧账号")
    ap.add_argument("--customer", default="8086290803", help="客户侧账号（脚本代言）")
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--driver", choices=("script", "ollama"), default="script")
    ap.add_argument("--scenario-id", default="photo_demander_tg")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token", default="")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--db", default=str(Path(DEFAULT_DATA_ROOT) / "config" / "inbox.db"))
    ap.add_argument("--ollama", default="http://192.168.0.176:11434")
    ap.add_argument("--model", default="qwen3:30b-a3b-instruct-2507-q4_K_M")
    ap.add_argument("--max-wait", type=float, default=300.0, help="每轮等回复上限秒")
    ap.add_argument("--quiet", type=float, default=30.0, help="收多段回复的静默窗秒")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--pace", type=float, default=8.0, help="轮间隔秒")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
