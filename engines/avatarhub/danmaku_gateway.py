# -*- coding: utf-8 -*-
"""弹幕/评论实时互动网关（P0-3 竞争力增强：直播电商场景的"数字人接住弹幕"）。

定位：把各直播平台的弹幕流接入【现有】观众问答链路——
    弹幕源 → 本网关(过滤/限流/改写) → AudienceQueue → 自动应答循环
    → /api/converse(RAG+LLM) → 克隆音 TTS + 口型 → vcam/OBS
  链路后半段全部复用已有实现(观众队列的优先级准入/去重/点赞热度、自动应答的单飞与冷却)，
  本模块只做"接入 + 挑选"，与主链路解耦：网关崩了直播不受影响。

弹幕源(可插拔)：
  - bilibili   直连 B 站弹幕 WS(公开协议,仅需 room_id;部分房间需 key/buvid 才回全量)
  - generic_ws 任意第三方抓取工具的 WS 转发(JSON 行,字段名可配)——抖音/快手等无公开
               弹幕 API 的平台,用户用自己的合规抓取端(如开放平台/中控台工具)转发进来
  - webhook    POST /api/danmaku/push 直推(最通用,任何脚本 curl 就能接)
  设计立场：不内置逆向各平台私有协议(脆弱且有 ToS 风险)——协议边界给足,接入成本降到最低。

安全：观众文本永不直接进 TTS——必经 AudienceQueue 轻校验 + 对话管道的输入安全闸兜底。
纯内存有界；所有计数器/环形缓冲设上限。
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time
import zlib
import logging
from collections import deque

logger = logging.getLogger("danmaku")

_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "danmaku_config.json")

# ── 问题判定启发(mode=question 时)：疑问词/问号 → 值得数字人开口回答 ──
_QUESTION_HINTS = ("?", "？", "吗", "嘛", "怎么", "什么", "为什么", "多少", "几点", "哪",
                   "咋", "能不能", "可不可以", "有没有", "是不是", "how", "what", "why",
                   "when", "where", "price", "多少钱", "怎么买", "怎么用", "发货", "尺码")


def _clean(s: str, max_len: int = 200) -> str:
    if not s:
        return ""
    s = "".join(ch for ch in str(s) if ch == "\n" or ch >= " ")
    return " ".join(s.split())[:max_len].strip()


def classify(text: str, mode: str = "all", keywords: list | None = None) -> bool:
    """该条弹幕是否值得转入问答队列。mode: all=全收 question=像问题 keyword=命中关键词。"""
    t = (text or "").strip()
    if not t:
        return False
    if mode == "all":
        return True
    if mode == "question":
        low = t.lower()
        return any(h in low for h in _QUESTION_HINTS)
    if mode == "keyword":
        low = t.lower()
        return any((k or "").strip().lower() in low for k in (keywords or []) if k)
    return True


class DanmakuFilter:
    """入队前的挑选：长度窗、每用户冷却、全局每分钟限额、模式判定。纯内存有界。"""

    def __init__(self, mode: str = "question", keywords: list | None = None,
                 min_len: int = 2, max_len: int = 120,
                 user_cooldown: float = 5.0, max_per_min: int = 20):
        self.mode = mode if mode in ("all", "question", "keyword") else "question"
        self.keywords = list(keywords or [])[:50]
        self.min_len = max(1, int(min_len))
        self.max_len = max(self.min_len, int(max_len))
        self.user_cooldown = max(0.0, float(user_cooldown))
        self.max_per_min = max(1, int(max_per_min))
        self._user_last: dict = {}
        self._fwd_ts: deque = deque(maxlen=self.max_per_min)

    def check(self, name: str, text: str) -> tuple:
        """(ok, reason)。reason 用于分桶统计。"""
        t = _clean(text, self.max_len + 8)
        if len(t) < self.min_len:
            return False, "too_short"
        if len(t) > self.max_len:
            return False, "too_long"
        if not classify(t, self.mode, self.keywords):
            return False, "mode_miss"
        now = time.time()
        key = (name or "?")[:40]
        last = self._user_last.get(key, 0.0)
        if self.user_cooldown > 0 and now - last < self.user_cooldown:
            return False, "user_cooldown"
        if len(self._fwd_ts) >= self.max_per_min and now - self._fwd_ts[0] < 60.0:
            return False, "rate_limit"
        # 通过：登记(限表大小,防无界)
        if len(self._user_last) > 2000:
            self._user_last.clear()
        self._user_last[key] = now
        self._fwd_ts.append(now)
        return True, ""


# ══════════ B 站弹幕二进制协议(公开)：16B 头 + JSON/压缩体 ══════════
#   header: packlen(u32) headlen(u16)=16 ver(u16) op(u32) seq(u32)，大端。
#   op: 2=心跳(上行) 3=人气(下行) 5=通知(弹幕等) 7=进房(上行) 8=进房回执(下行)
#   ver: 0/1=原文 2=zlib(体内是多条子包串联) 3=brotli(同)

def bili_pack(op: int, body: bytes, ver: int = 1, seq: int = 1) -> bytes:
    return struct.pack(">IHHII", 16 + len(body), 16, ver, op, seq) + body


def bili_join_body(room_id: int, key: str = "", buvid: str = "") -> bytes:
    d = {"uid": 0, "roomid": int(room_id), "protover": 2, "platform": "web", "type": 2}
    if key:
        d["key"] = key
    if buvid:
        d["buvid"] = buvid
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def parse_bili_packets(data: bytes) -> list:
    """一帧 WS 二进制 → [{op, body(dict|int|bytes)}...]。压缩体递归展开。健壮：坏包丢弃。"""
    out = []
    i, n = 0, len(data)
    while i + 16 <= n:
        try:
            plen, hlen, ver, op, _seq = struct.unpack(">IHHII", data[i:i + 16])
        except Exception:
            break
        if plen < hlen or i + plen > n:
            break
        body = data[i + hlen:i + plen]
        i += plen
        if op == 5 and ver == 2:
            try:
                out.extend(parse_bili_packets(zlib.decompress(body)))
            except Exception:
                pass
            continue
        if op == 5 and ver == 3:
            try:
                import brotli                      # 可选依赖：无则丢帧计数
                out.extend(parse_bili_packets(brotli.decompress(body)))
            except Exception:
                out.append({"op": -3, "body": None})   # 标记"brotli 不可用"
            continue
        if op == 3:
            try:
                out.append({"op": 3, "body": struct.unpack(">I", body[:4])[0]})
            except Exception:
                pass
            continue
        if op in (5, 8):
            try:
                out.append({"op": op, "body": json.loads(body.decode("utf-8", "replace"))})
            except Exception:
                pass
    return out


def bili_extract(msg: dict) -> dict | None:
    """通知包 → 统一事件 {type: chat|gift|superchat, name, text, gift, num}。不认识的返回 None。"""
    try:
        cmd = str(msg.get("cmd") or "")
        if cmd.startswith("DANMU_MSG"):
            info = msg.get("info") or []
            return {"type": "chat", "name": str(info[2][1]), "text": str(info[1])}
        if cmd == "SUPER_CHAT_MESSAGE":
            d = msg.get("data") or {}
            return {"type": "superchat", "name": str((d.get("user_info") or {}).get("uname", "")),
                    "text": str(d.get("message", ""))}
        if cmd == "SEND_GIFT":
            d = msg.get("data") or {}
            return {"type": "gift", "name": str(d.get("uname", "")),
                    "gift": str(d.get("giftName", "")), "num": int(d.get("num", 1) or 1),
                    "text": ""}
    except Exception:
        return None
    return None


def generic_extract(raw: str, name_field: str = "name", text_field: str = "text",
                    type_field: str = "type") -> dict | None:
    """generic_ws 源的一行 JSON → 统一事件。非 JSON/缺字段 → None。"""
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            return None
        text = _clean(d.get(text_field, ""))
        name = _clean(d.get(name_field, "") or "观众", 40)
        typ = str(d.get(type_field, "chat") or "chat")
        if typ not in ("chat", "gift", "superchat"):
            typ = "chat"
        ev = {"type": typ, "name": name, "text": text}
        if typ == "gift":
            ev["gift"] = _clean(d.get("gift", "礼物"), 40)
            try:
                ev["num"] = max(1, int(d.get("num", 1)))
            except Exception:
                ev["num"] = 1
        return ev
    except Exception:
        return None


# ══════════ 网关本体 ══════════

class DanmakuGateway:
    def __init__(self, get_audience, log=None):
        self._get_audience = get_audience
        self.log = log or logger
        self.cfg: dict = {}
        self.filter: DanmakuFilter | None = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.connected = False
        self.source = ""
        self.stats = {"received": 0, "forwarded": 0, "gifts": 0,
                      "dropped": {}, "last_error": "", "started_ts": 0}
        self.recent: deque = deque(maxlen=20)

    # ── 生命周期 ────────────────────────────────────────────────────
    def start(self, cfg: dict) -> dict:
        if self.running:
            return {"ok": False, "reason": "已在运行,先 /api/danmaku/stop"}
        aud = self._get_audience()
        if aud is None:
            return {"ok": False,
                    "reason": "观众提问模块未加载(需 AVATARHUB_AUDIENCE=1 并重启 Hub)"}
        source = str(cfg.get("source") or "webhook").strip().lower()
        if source not in ("bilibili", "generic_ws", "webhook"):
            return {"ok": False, "reason": f"未知弹幕源 {source}(可选 bilibili/generic_ws/webhook)"}
        if source == "bilibili" and not cfg.get("room_id"):
            return {"ok": False, "reason": "bilibili 源需要 room_id"}
        if source == "generic_ws" and not cfg.get("ws_url"):
            return {"ok": False, "reason": "generic_ws 源需要 ws_url"}
        self.cfg = dict(cfg)
        self.source = source
        self.filter = DanmakuFilter(
            mode=str(cfg.get("mode", "question")),
            keywords=cfg.get("keywords") or [],
            min_len=int(cfg.get("min_len", 2)), max_len=int(cfg.get("max_len", 120)),
            user_cooldown=float(cfg.get("user_cooldown", 5.0)),
            max_per_min=int(cfg.get("max_per_min", 20)))
        self.stats = {"received": 0, "forwarded": 0, "gifts": 0,
                      "dropped": {}, "last_error": "", "started_ts": int(time.time())}
        self.recent.clear()
        self.running = True
        self.connected = (source == "webhook")     # webhook 源无连接态,视为常通
        if source in ("bilibili", "generic_ws"):
            self.task = asyncio.get_running_loop().create_task(self._run_ws())
        self._save_cfg()
        self.log.info(f"[Danmaku] 网关启动: source={source} mode={self.filter.mode}")
        return {"ok": True, "status": self.status()}

    async def stop(self) -> dict:
        self.running = False
        self.connected = False
        t, self.task = self.task, None
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.log.info("[Danmaku] 网关已停止")
        return {"ok": True}

    def status(self) -> dict:
        return {"running": self.running, "source": self.source,
                "connected": self.connected, "cfg": {k: v for k, v in self.cfg.items()
                                                     if k not in ("key",)},
                "stats": {**self.stats, "dropped": dict(self.stats["dropped"])},
                "recent": list(self.recent),
                "audience_loaded": self._get_audience() is not None}

    # ── 事件入口(所有源共用) ───────────────────────────────────────
    def ingest(self, ev: dict, via: str = "") -> dict:
        """统一事件 → 过滤 → 观众队列。返回 {ok, reason}。"""
        if not self.running:
            return {"ok": False, "reason": "网关未运行"}
        self.stats["received"] += 1
        typ = ev.get("type", "chat")
        name = _clean(ev.get("name", "") or "观众", 40)
        if typ == "gift":
            self.stats["gifts"] += 1
            if not self.cfg.get("gift_thanks"):
                return {"ok": False, "reason": "gift_ignored"}
            text = f"请用一句话感谢观众 {name} 送出的{ev.get('gift', '礼物')}×{ev.get('num', 1)}"
            return self._forward(name or "礼物", text, via, kind="gift")
        text = _clean(ev.get("text", ""), 200)
        ok, why = self.filter.check(name, text)
        if not ok:
            self.stats["dropped"][why] = self.stats["dropped"].get(why, 0) + 1
            return {"ok": False, "reason": why}
        return self._forward(name, text, via, kind=typ)

    def _forward(self, name: str, text: str, via: str, kind: str = "chat") -> dict:
        aud = self._get_audience()
        if aud is None:
            self.stats["dropped"]["no_audience"] = self.stats["dropped"].get("no_audience", 0) + 1
            return {"ok": False, "reason": "观众模块未加载"}
        # 每观众一个虚拟 IP 桶：复用 AudienceQueue 的按 IP 限流/防刷,互不干扰
        r = aud.submit(text, name=name, ip=f"dm:{via}:{name[:32]}")
        if r.get("ok"):
            self.stats["forwarded"] += 1
            self.recent.append({"ts": int(time.time()), "name": name, "text": text[:80],
                                "kind": kind, "via": via})
            return {"ok": True}
        why = f"queue:{r.get('reason', 'rejected')}"
        self.stats["dropped"][why] = self.stats["dropped"].get(why, 0) + 1
        return {"ok": False, "reason": why}

    # ── WS 源连接循环(bilibili / generic_ws) ────────────────────────
    async def _run_ws(self):
        backoff = 2.0
        while self.running:
            try:
                if self.source == "bilibili":
                    await self._bili_session()
                else:
                    await self._generic_session()
                backoff = 2.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.connected = False
                self.stats["last_error"] = str(e)[:200]
                self.log.warning(f"[Danmaku] {self.source} 断线({e}),{backoff:.0f}s 后重连")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, 60.0)

    async def _bili_session(self):
        import websockets
        room = int(self.cfg.get("room_id"))
        url = self.cfg.get("bili_ws") or "wss://broadcastlv.chat.bilibili.com/sub"
        async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
            await ws.send(bili_pack(7, bili_join_body(room, key=self.cfg.get("key", ""),
                                                      buvid=self.cfg.get("buvid", ""))))
            self.connected = True
            self.stats["last_error"] = ""
            self.log.info(f"[Danmaku] B站已进房 room={room}")

            async def _heartbeat():
                while True:
                    await ws.send(bili_pack(2, b"[object Object]"))
                    await asyncio.sleep(30)

            hb = asyncio.ensure_future(_heartbeat())
            try:
                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    for p in parse_bili_packets(bytes(raw)):
                        if p["op"] == -3:
                            self.stats["dropped"]["brotli_missing"] = \
                                self.stats["dropped"].get("brotli_missing", 0) + 1
                            continue
                        if p["op"] != 5 or not isinstance(p["body"], dict):
                            continue
                        ev = bili_extract(p["body"])
                        if ev:
                            self.ingest(ev, via="bili")
            finally:
                hb.cancel()

    async def _generic_session(self):
        import websockets
        url = str(self.cfg.get("ws_url"))
        headers = {}
        if self.cfg.get("ws_token"):
            headers["Authorization"] = f"Bearer {self.cfg['ws_token']}"
        try:
            conn = websockets.connect(url, max_size=2 ** 20, ping_interval=20,
                                      additional_headers=headers or None)
        except TypeError:
            conn = websockets.connect(url, max_size=2 ** 20, ping_interval=20,
                                      extra_headers=headers or None)
        async with conn as ws:
            self.connected = True
            self.stats["last_error"] = ""
            self.log.info(f"[Danmaku] generic_ws 已连接 {url}")
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    raw = bytes(raw).decode("utf-8", "replace")
                ev = generic_extract(raw,
                                     name_field=self.cfg.get("name_field", "name"),
                                     text_field=self.cfg.get("text_field", "text"),
                                     type_field=self.cfg.get("type_field", "type"))
                if ev:
                    self.ingest(ev, via="ws")

    # ── 配置持久化(方便下次一键重启同房间) ──────────────────────────
    def _save_cfg(self):
        try:
            os.makedirs(os.path.dirname(_CFG_PATH), exist_ok=True)
            safe = {k: v for k, v in self.cfg.items() if k not in ("ws_token", "key")}
            with open(_CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(safe, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    @staticmethod
    def load_saved_cfg() -> dict:
        try:
            with open(_CFG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


# ══════════ FastAPI 挂载(hub 调一行 attach 即可) ══════════

def attach(app, get_audience, log=None):
    """注册 /api/danmaku/* 路由。返回网关实例(hub 侧留引用即可观测)。"""
    from pydantic import BaseModel

    gw = DanmakuGateway(get_audience, log=log)

    class DanmakuStart(BaseModel):
        source: str = "webhook"           # bilibili | generic_ws | webhook
        room_id: int = 0                  # bilibili 房间号
        key: str = ""                     # bilibili 进房 key(部分房间需要)
        buvid: str = ""
        ws_url: str = ""                  # generic_ws 转发端地址
        ws_token: str = ""
        name_field: str = "name"
        text_field: str = "text"
        type_field: str = "type"
        mode: str = "question"            # all | question | keyword
        keywords: list = []
        min_len: int = 2
        max_len: int = 120
        user_cooldown: float = 5.0
        max_per_min: int = 20
        gift_thanks: bool = False         # 礼物→数字人一句致谢(走同一问答链)

    class DanmakuPush(BaseModel):
        name: str = "观众"
        text: str = ""
        type: str = "chat"                # chat | gift | superchat
        gift: str = ""
        num: int = 1

    @app.post("/api/danmaku/start")
    async def api_danmaku_start(req: DanmakuStart):
        return gw.start(req.dict())

    @app.post("/api/danmaku/stop")
    async def api_danmaku_stop():
        return await gw.stop()

    @app.get("/api/danmaku/status")
    def api_danmaku_status():
        return gw.status()

    @app.get("/api/danmaku/last_config")
    def api_danmaku_last_config():
        return {"ok": True, "cfg": DanmakuGateway.load_saved_cfg()}

    @app.post("/api/danmaku/push")
    def api_danmaku_push(req: DanmakuPush):
        """webhook 源：任意外部工具直推一条弹幕/礼物事件。"""
        ev = {"type": req.type, "name": req.name, "text": req.text,
              "gift": req.gift, "num": req.num}
        return gw.ingest(ev, via="push")

    @app.post("/api/danmaku/test")
    def api_danmaku_test():
        """联调自检：注入一条测试弹幕,验证 网关→观众队列→自动应答 全链路。"""
        if not gw.running:
            return {"ok": False, "reason": "网关未运行,先 POST /api/danmaku/start"}
        return gw.ingest({"type": "chat", "name": "联调",
                          "text": "这个产品今天有什么优惠吗？"}, via="test")

    return gw


# ══════════ 离线自测 ══════════

def _selftest() -> int:
    # 1) B站帧编解码(含 zlib 聚合包)
    danmu = json.dumps({"cmd": "DANMU_MSG", "info": [[], "这个多少钱？", [123, "小明"]]},
                       ensure_ascii=False).encode()
    gift = json.dumps({"cmd": "SEND_GIFT", "data": {"uname": "阿强", "giftName": "小心心",
                                                    "num": 3}}, ensure_ascii=False).encode()
    inner = bili_pack(5, danmu) + bili_pack(5, gift)
    frame = bili_pack(5, zlib.compress(inner), ver=2) + bili_pack(3, struct.pack(">I", 999))
    pkts = parse_bili_packets(frame)
    evs = [bili_extract(p["body"]) for p in pkts if p["op"] == 5]
    evs = [e for e in evs if e]
    assert len(evs) == 2, f"应解出2条: {evs}"
    assert evs[0] == {"type": "chat", "name": "小明", "text": "这个多少钱？"}
    assert evs[1]["type"] == "gift" and evs[1]["num"] == 3
    assert any(p["op"] == 3 and p["body"] == 999 for p in pkts), "人气包解析失败"
    print("[1/4] B站二进制帧(zlib聚合+人气) ... OK")

    # 2) generic_ws JSON 提取(自定义字段名)
    ev = generic_extract('{"u":"老王","c":"怎么发货？","t":"chat"}',
                         name_field="u", text_field="c", type_field="t")
    assert ev == {"type": "chat", "name": "老王", "text": "怎么发货？"}
    assert generic_extract("not json") is None
    print("[2/4] generic_ws JSON 提取 ... OK")

    # 3) 过滤器：模式/冷却/限频
    f = DanmakuFilter(mode="question", user_cooldown=5.0, max_per_min=3)
    assert f.check("a", "这个怎么用？")[0] is True
    assert f.check("b", "哈哈哈哈")[0] is False           # 非问题
    assert f.check("a", "还有优惠吗")[0] is False          # 同人冷却中
    assert f.check("c", "价格多少")[0] is True
    assert f.check("d", "有没有红色的")[0] is True
    ok, why = f.check("e", "为什么这么贵？")                 # 第4条/分钟 → 限频
    assert ok is False and why == "rate_limit", (ok, why)
    f2 = DanmakuFilter(mode="keyword", keywords=["上链接"], user_cooldown=0)
    assert f2.check("x", "主播快上链接！")[0] is True
    assert f2.check("y", "今天天气不错")[0] is False
    print("[3/4] 过滤器(模式/冷却/限频/关键词) ... OK")

    # 4) 网关→(假)观众队列 端到端(webhook 源,不出网)
    class FakeAudience:
        def __init__(self):
            self.items = []

        def submit(self, text, name="", ip=""):
            self.items.append((name, text, ip))
            return {"ok": True}

    fake = FakeAudience()
    gw = DanmakuGateway(lambda: fake)

    async def run():
        r = gw.start({"source": "webhook", "mode": "all", "user_cooldown": 0,
                      "gift_thanks": True})
        assert r["ok"], r
        assert gw.ingest({"type": "chat", "name": "小红", "text": "有优惠券吗？"},
                         via="test")["ok"]
        assert gw.ingest({"type": "gift", "name": "土豪", "gift": "火箭", "num": 1},
                         via="test")["ok"]
        assert not gw.ingest({"type": "chat", "name": "", "text": ""}, via="test")["ok"]
        await gw.stop()

    asyncio.run(run())
    assert len(fake.items) == 2 and fake.items[0][0] == "小红"
    assert "感谢观众 土豪" in fake.items[1][1]
    st = gw.status()
    assert st["stats"]["forwarded"] == 2 and st["stats"]["gifts"] == 1
    print("[4/4] 网关→观众队列端到端(含礼物致谢) ... OK")
    print("Danmaku selftest 全部通过")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
