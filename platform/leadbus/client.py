# -*- coding: utf-8 -*-
"""platform/leadbus/client.py — 线索交接总线『瘦客户端』(纯 stdlib，可降级 + 本地 outbox)。

见 CONTRACT.md：上游获客(TG-AI智控王 / huoke)把一条**线索业务数据**交给承接中台
(chengjie 的 /api/leadbus/ingest)。本客户端两条铁律：

  1) 总线可选：环境变量 BOUNDLESS_BUS_URL 未设置 → 单机模式：线索写进本地 outbox 落盘、
     不投递(返回 queued=True, mode="standalone")；上游照常独立运行，一条线索都不丢。
     设置了 → 联网模式：POST 投递；投递失败(超时/连接失败/5xx)自动落 outbox 待重投。
  2) fail-soft：publish() 任何情况都**不抛异常**——投递不了就排队，业务主路径(获客)绝不被拖垮。

隐私边界(与 observability EVENT_CONTRACT 隐私红线配合)：
  线索业务数据(external_id / handle / profile)是**点对点业务通道**上的载荷，
  只在"获客→承接"两端之间流转，**绝不可转发进 observability 事件 spool / 集团数仓**。
  线索的"计数 / 意向分 / 来源渠道"才由调用方另发 observability 事件(zhituo.lead.captured 等)。
  本客户端刻意只做投递、不发遥测(单一职责 + 零依赖)，指标由调用方就近 emit。

依赖铁律：只用 stdlib(urllib/json/os/threading)，不 import engines/products/website，
也不 import 第三方包 —— 守住 "platform 不反向依赖"。

用法：
    from client import LeadBusClient
    lb = LeadBusClient()                      # bus_url 读环境变量 BOUNDLESS_BUS_URL
    r = lb.publish({
        "source": {"product": "zhituo", "platform": "telegram", "campaign": "utm_x"},
        "lead":   {"external_id": "tg:123", "handle": "@who",
                   "profile": {"lang": "en", "funnel_stage": "new", "intent_score": 0.72}},
        "assign_hint": {"domain": "ecommerce", "persona": "sales"},
    })
    # r -> {"available":True,"delivered":True,...} 或 {"available":False,"queued":True,...}
    lb.drain_outbox()                          # 总线恢复后补投 outbox 里积压的线索
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

BUS_ENV_VAR = "BOUNDLESS_BUS_URL"
OUTBOX_ENV_VAR = "LEADBUS_OUTBOX_DIR"
# 缺省 outbox：<仓库根>/data/leadbus/outbox/（本文件位于 <仓库根>/platform/leadbus/）
DEFAULT_OUTBOX_DIR = Path(__file__).resolve().parents[2] / "data" / "leadbus" / "outbox"
INGEST_PATH = "/api/leadbus/ingest"
STATUS_PATH = "/api/leadbus/status"

_WRITE_LOCK = threading.Lock()   # 串行化 outbox 追加，保证多线程下行不交错


class LeadBusClient:
    def __init__(self, bus_url: Optional[str] = None, outbox_dir: Optional[str] = None,
                 timeout: float = 8.0, auth_token: Optional[str] = None):
        # bus_url 显式参数 > 环境变量 BOUNDLESS_BUS_URL > None(单机模式)
        raw = bus_url if bus_url is not None else os.environ.get(BUS_ENV_VAR, "").strip()
        self.bus_url = raw.rstrip("/") if raw else None
        self.outbox_dir = str(outbox_dir) if outbox_dir else (
            os.environ.get(OUTBOX_ENV_VAR, "").strip() or str(DEFAULT_OUTBOX_DIR))
        self.timeout = timeout
        # 2026-07-19 追加：chengjie 若配置了 web_admin.auth_token，/api/leadbus/*
        # 走同一套 _api_auth（支持 Bearer），无 token 时会一直 401→落 outbox 重投不止。
        # 显式参数 > 环境变量 BOUNDLESS_BUS_TOKEN > 不发该头(向后兼容，默认行为不变)。
        self.auth_token = (auth_token if auth_token is not None
                           else os.environ.get("BOUNDLESS_BUS_TOKEN", "").strip() or None)

    def _headers(self, base: Dict[str, str]) -> Dict[str, str]:
        if self.auth_token:
            base = dict(base)
            base["Authorization"] = f"Bearer {self.auth_token}"
        return base

    # ---- 校验（线索信封最小约束；宽松但拦住明显残缺）----
    @staticmethod
    def envelope_error(lead: Any) -> Optional[str]:
        if not isinstance(lead, dict):
            return f"线索必须是 dict，得到 {type(lead).__name__}"
        src = lead.get("source")
        if not isinstance(src, dict) or not src.get("product") or not src.get("platform"):
            return "source.product 与 source.platform 必填"
        core = lead.get("lead")
        if not isinstance(core, dict) or not core.get("external_id"):
            return "lead.external_id 必填"
        prof = core.get("profile")
        if prof is not None:
            if not isinstance(prof, dict):
                return "lead.profile 必须是 dict"
            score = prof.get("intent_score")
            if score is not None and (not isinstance(score, (int, float))
                                      or isinstance(score, bool) or not 0.0 <= float(score) <= 1.0):
                return "profile.intent_score 必须是 0..1 的数值"
        return None

    # ---- 内部 HTTP（可降级：任何失败都收敛为 dict，不抛给调用方）----
    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.bus_url:
            return {"available": False, "error": "BOUNDLESS_BUS_URL 未配置(单机模式)"}
        url = self.bus_url + path
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers=self._headers({"Content-Type": "application/json", "Accept": "application/json"}),
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                out = json.loads(body) if body else {}
                if isinstance(out, dict):
                    out.setdefault("available", True)
                    return out
                return {"available": True, "data": out}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"available": False, "error": f"HTTP {e.code}", "detail": detail}
        except Exception as e:   # 连接失败/超时/JSON 解析失败 —— 一律降级
            return {"available": False, "error": str(e)[:200]}

    def _get(self, path: str) -> Dict[str, Any]:
        if not self.bus_url:
            return {"available": False, "error": "BOUNDLESS_BUS_URL 未配置(单机模式)"}
        req = urllib.request.Request(self.bus_url + path,
                                     headers=self._headers({"Accept": "application/json"}), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                out = json.loads(body) if body else {}
                if isinstance(out, dict):
                    out.setdefault("available", True)
                    return out
                return {"available": True, "data": out}
        except Exception as e:
            return {"available": False, "error": str(e)[:200]}

    # ---- outbox 落盘（点对点业务通道的兜底；按天分文件，append-only）----
    def _outbox_file(self) -> str:
        day = time.strftime("%Y%m%d", time.gmtime())
        return os.path.join(self.outbox_dir, f"leads-{day}.jsonl")

    def _spool(self, envelope: Dict[str, Any]) -> bool:
        try:
            line = json.dumps(envelope, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")) + "\n"
            with _WRITE_LOCK:
                os.makedirs(self.outbox_dir, exist_ok=True)
                with open(self._outbox_file(), "a", encoding="utf-8", newline="\n") as f:
                    f.write(line)
            return True
        except Exception:
            return False

    # ---- 契约方法（见 CONTRACT.md §2）----
    def publish(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """投递一条线索到承接中台。永不抛异常。

        返回：
          - 联网且投递成功：{"available":True,"delivered":True,"lead_id":..., ...服务端回执}
          - 联网但投递失败：{"available":False,"queued":True,"error":...}（已落 outbox 待重投）
          - 单机模式：      {"available":False,"queued":True,"mode":"standalone"}（已落 outbox）
          - 线索信封非法：  {"available":False,"error":"...","rejected":True}（不落 outbox）
        """
        err = self.envelope_error(lead)
        if err is not None:
            return {"available": False, "rejected": True, "error": err}
        # 补一个稳定的投递 ID（幂等键），不覆盖调用方已给的
        envelope = dict(lead)
        envelope.setdefault("lead_id", "lead_" + secrets.token_hex(12))
        envelope.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z")

        if not self.bus_url:
            self._spool(envelope)
            return {"available": False, "queued": True, "mode": "standalone",
                    "lead_id": envelope["lead_id"]}

        resp = self._post(INGEST_PATH, {"lead": envelope})
        if resp.get("available"):
            resp.setdefault("delivered", True)
            resp.setdefault("lead_id", envelope["lead_id"])
            return resp
        # 投递失败：落 outbox 待重投
        self._spool(envelope)
        return {"available": False, "queued": True,
                "lead_id": envelope["lead_id"], "error": resp.get("error", "deliver_failed")}

    def drain_outbox(self, max_batch: int = 500) -> Dict[str, Any]:
        """总线恢复后补投 outbox 里积压的线索。永不抛异常。

        逐行重投，成功的行丢弃、失败的行保留回写。返回 {"delivered":n,"remaining":m,...}。
        单机模式(无 bus_url)直接返回，不动 outbox。
        """
        if not self.bus_url:
            return {"available": False, "error": "单机模式，不补投", "delivered": 0}
        delivered = 0
        remaining: List[str] = []
        errors = 0
        try:
            if not os.path.isdir(self.outbox_dir):
                return {"available": True, "delivered": 0, "remaining": 0}
            files = sorted(f for f in os.listdir(self.outbox_dir)
                           if f.startswith("leads-") and f.endswith(".jsonl"))
            for fname in files:
                fpath = os.path.join(self.outbox_dir, fname)
                with _WRITE_LOCK:
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                    except Exception:
                        continue
                    kept: List[str] = []
                    for raw in lines:
                        raw = raw.strip()
                        if not raw:
                            continue
                        if delivered >= max_batch:
                            kept.append(raw)
                            continue
                        try:
                            env = json.loads(raw)
                        except Exception:
                            errors += 1
                            continue   # 脏行丢弃
                        resp = self._post(INGEST_PATH, {"lead": env})
                        if resp.get("available"):
                            delivered += 1
                        else:
                            kept.append(raw)
                    # 回写剩余（原子：写临时再替换）
                    if kept:
                        tmp = fpath + ".tmp"
                        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                            f.write("\n".join(kept) + "\n")
                        os.replace(tmp, fpath)
                        remaining.extend(kept)
                    else:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
        except Exception as e:
            return {"available": False, "error": str(e)[:200],
                    "delivered": delivered, "remaining": len(remaining)}
        return {"available": True, "delivered": delivered,
                "remaining": len(remaining), "dropped_bad_lines": errors}

    def status(self) -> Dict[str, Any]:
        """/api/leadbus/status —— 承接中台线索入口是否就绪。单机模式返回 available=False。"""
        return self._get(STATUS_PATH)

    def available(self) -> bool:
        """承接中台 leadbus 入口是否可达(HTTP 可达且入口已加载)。"""
        st = self.status()
        return bool(st.get("available")) and bool(st.get("ready", st.get("available")))


def _selftest() -> int:
    """在临时目录验证：单机模式落 outbox / 非法信封拒收 / mock 服务投递 / drain 补投。"""
    import tempfile
    import http.server
    import socketserver

    failures: List[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    good = {
        "source": {"product": "zhituo", "platform": "telegram", "campaign": "utm_x"},
        "lead": {"external_id": "tg:123", "handle": "@who",
                 "profile": {"lang": "en", "funnel_stage": "new", "intent_score": 0.72}},
        "assign_hint": {"domain": "ecommerce", "persona": "sales"},
    }

    print("== leadbus 瘦客户端自测（client.py --selftest）==")

    with tempfile.TemporaryDirectory(prefix="leadbus_selftest_") as tmp:
        print("[1/4] 单机模式：无 bus_url → 线索落 outbox、queued")
        lb0 = LeadBusClient(bus_url="", outbox_dir=tmp)
        r0 = lb0.publish(good)
        check("单机 publish 返回 queued/standalone", r0.get("queued") is True and r0.get("mode") == "standalone")
        files = [f for f in os.listdir(tmp) if f.startswith("leads-")]
        check("outbox 生成 leads-YYYYMMDD.jsonl 且含 1 行",
              len(files) == 1 and sum(1 for _ in open(os.path.join(tmp, files[0]), encoding="utf-8")) == 1)

        print("[2/4] 非法信封拒收（不落 outbox）")
        bad_cases = [
            ("缺 source", {"lead": {"external_id": "x"}}),
            ("缺 external_id", {"source": {"product": "zhituo", "platform": "tg"}, "lead": {}}),
            ("intent_score 越界", {"source": {"product": "zhituo", "platform": "tg"},
                                    "lead": {"external_id": "x", "profile": {"intent_score": 9}}}),
            ("lead 非 dict", "oops"),
        ]
        for why, bad in bad_cases:
            r = lb0.publish(bad)
            check(f"拒绝 {why}", r.get("rejected") is True and r.get("available") is False)
        files = [f for f in os.listdir(tmp) if f.startswith("leads-")]
        n_after = sum(1 for _ in open(os.path.join(tmp, files[0]), encoding="utf-8"))
        check("非法发射后 outbox 行数不变（仍 1 行）", n_after == 1)

        print("[3/4] mock 承接服务：联网投递成功")
        received: List[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    received.append(json.loads(body))
                except Exception:
                    received.append({})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ready": true, "assigned": true}')

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ready": true}')

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                base = f"http://127.0.0.1:{port}"
                tmp2 = os.path.join(tmp, "ob2")
                lb1 = LeadBusClient(bus_url=base, outbox_dir=tmp2)
                check("available() 探针为真", lb1.available() is True)
                r1 = lb1.publish(good)
                check("联网 publish delivered=True", r1.get("delivered") is True and r1.get("available") is True)
                check("mock 服务端收到 1 条且信封含 lead.external_id",
                      len(received) == 1 and received[0].get("lead", {}).get("lead", {}).get("external_id") == "tg:123")

                print("[4/4] drain_outbox：把单机模式积压的补投出去")
                # lb2 指向单机 outbox(tmp)，但这次配了 bus_url → 应能补投第 1 步那条
                lb2 = LeadBusClient(bus_url=base, outbox_dir=tmp)
                before = len(received)
                d = lb2.drain_outbox()
                check("drain 投递数=1、剩余=0", d.get("delivered") == 1 and d.get("remaining") == 0)
                check("mock 服务端累计再收到 1 条", len(received) == before + 1)
                leftover = [f for f in os.listdir(tmp) if f.startswith("leads-")]
                check("补投后 outbox 日文件已清空删除", len(leftover) == 0)
            finally:
                srv.shutdown()

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if sys.argv[1:2] == ["--selftest"]:
        sys.exit(_selftest())
    # 缺省：打印当前配置(不联网)
    lb = LeadBusClient()
    print(f"[leadbus.client] bus_url={lb.bus_url or '(单机模式)'}  outbox={lb.outbox_dir}")
    print(f"  available()={lb.available()}  (承接中台未在线/单机属正常，客户端已降级不抛错)")
    sys.exit(0)
