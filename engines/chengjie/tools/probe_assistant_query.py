# -*- coding: utf-8 -*-
"""小智问答链线上只读探针。

打一发真实 `/api/assistant/query`，把 SSE 流逐帧摊开：心跳（`: ka`）/ meta /
delta / done / err 各自第几秒到达，末尾给 PASS/FAIL 判词（FAIL＝坐席此刻会看到
错误气泡）。**静态门禁证不了这条链活着**——它跨了路由、心跳包装、7 层
BaseHTTPMiddleware、压缩层、StreamingResponse 与真实云端，任一环断掉的症状都
只是前端一句「网络异常」。

用法：
    python tools/probe_assistant_query.py ["问题"]

只读：不写业务库，只消耗一次问答额度（会在 qa_log 落一条台账行，那本就是问答
账本）。token 从实例配置读，绝不打印。

排障读法（2026-08-28 那次事故沉淀的三个判别位）：
  · `cache-control` 带 no-transform ＝ 心跳那批代码已装载；
  · FAIL 且只有 meta ＝ 流在等 LLM 首 token 时被掐 → 看服务端日志的
    `stage=` 与 `ka=`：ka=0 且亚秒即死 ＝ 有人给了伪 disconnect（历史真凶是
    body_size_limit_middleware 的 replay_receive，见 admin.py 那段注释），
    ka>0 才是真的空闲超时；
  · FAIL 且日志有「认证被拒」＝ assistant.query.llm 的 base_url/model/api_key
    与 ai.* 分叉了（三键必须同源，推荐全写 inherit）。
"""
from __future__ import annotations

import json
import sys
import time

import requests
import yaml

CFG = r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml"
BASE = "http://127.0.0.1:18799"
Q = sys.argv[1] if len(sys.argv) > 1 else "怎么发语音"

with open(CFG, encoding="utf-8") as f:
    token = str((yaml.safe_load(f) or {}).get("web_admin", {}).get("auth_token") or "")
if not token:
    print("NO_TOKEN in instance config")
    raise SystemExit(2)

t0 = time.time()
ka = deltas = 0
done = err = meta = None
answer_chars = 0

resp = requests.post(
    BASE + "/api/assistant/query",
    headers={"Authorization": "Bearer " + token,
             "Content-Type": "application/json"},
    data=json.dumps({"q": Q, "page": "/workspace", "lang": "zh"}),
    stream=True, timeout=180,
)
print(f"HTTP {resp.status_code}  content-type={resp.headers.get('content-type')}")
print(f"cache-control={resp.headers.get('cache-control')!r}"
      f"  x-accel-buffering={resp.headers.get('x-accel-buffering')!r}")
if resp.status_code != 200:
    print("BODY:", resp.text[:400])
    raise SystemExit(1)

for raw in resp.iter_lines(decode_unicode=True):
    if not raw:
        continue
    el = round(time.time() - t0, 2)
    line = raw.strip()
    if line.startswith(":"):
        ka += 1
        print(f"  [{el:6.2f}s] KEEPALIVE {line!r}")
        continue
    if not line.startswith("data:"):
        print(f"  [{el:6.2f}s] RAW {line[:80]!r}")
        continue
    try:
        ev = json.loads(line[5:].strip())
    except Exception:
        print(f"  [{el:6.2f}s] UNPARSEABLE {line[:80]!r}")
        continue
    kind = ev.get("ev")
    if kind == "meta":
        meta = ev
        print(f"  [{el:6.2f}s] meta sources={len(ev.get('sources') or [])} "
              f"report_hint={ev.get('report_hint')}")
    elif kind == "delta":
        deltas += 1
        answer_chars += len(str(ev.get("text") or ""))
        if deltas <= 2:
            print(f"  [{el:6.2f}s] delta#{deltas} {str(ev.get('text'))[:40]!r}")
    elif kind == "done":
        done = ev
        print(f"  [{el:6.2f}s] done answered={ev.get('answered')} "
              f"ms={ev.get('ms')} qa_id={ev.get('qa_id')}")
    elif kind == "err":
        err = ev
        print(f"  [{el:6.2f}s] ERR {ev}")

total = round(time.time() - t0, 2)
print(f"\nRESULT q={Q!r} ka={ka} deltas={deltas} answer_chars={answer_chars} "
      f"done={bool(done)} err={err} total={total}s")
verdict = "PASS" if (done and not err and answer_chars > 0) else "FAIL"
print(f"VERDICT {verdict}"
      + ("" if verdict == "PASS" else "  <-- 前端此刻会显示错误气泡"))
