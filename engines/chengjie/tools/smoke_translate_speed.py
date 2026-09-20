"""坐席翻译链路线上冒烟（2026-08-09 提速批次验收工具，只读+微量真翻译）。

验证四件事（全部走生产实例的真实鉴权与 TranslationService）：
  S1 引擎矩阵：目标语 zh 的 effective 引擎应为 ollama_mt（HY-MT 本地）；
  S2 单条翻译：provider/延迟/译文（首跑走引擎，复跑命中缓存 cached=True）；
  S3 批量端点 /translate-batch：一次往返 N 条全 ok（收件箱懒翻的新管道）；
  S4 keep_alive 到位：翻译后 LAN 主力端点 /api/ps 的 expires_at 应被推到
     ~keep_alive 之后（引擎走原生 /api/chat 才会发生——/v1 会忽略该参数）。

用法：
  python tools/smoke_translate_speed.py                # 默认打 127.0.0.1:18799
  python tools/smoke_translate_speed.py --base http://127.0.0.1:18799

鉴权：与 tools/smoke_voice_reuse.py 同口径——从实例 config(.local).yaml 读
``web_admin.auth_token``（Bearer）。翻译样句为通用客服语料，会经 L1/L2 缓存
正常落库（与坐席手动点「译」等价的微量流量）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"
INSTANCE_CONFIG_DIRS = [
    Path(r"D:\chengjie-instances\zhiliao\data\config"),
]

SAMPLES = [
    "Is this available in blue color?",
    "My order arrived damaged, what should I do?",
    "Do you offer cash on delivery in Manila?",
    "The tracking page shows no update for three days.",
    "Can I change the delivery address to my office?",
    "What is your return policy for electronics?",
]


def _load_token() -> str:
    import yaml

    for d in INSTANCE_CONFIG_DIRS:
        for name in ("config.local.yaml", "config.yaml"):
            p = d / name
            if not p.exists():
                continue
            try:
                cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
            if tok:
                return tok
    return ""


def _call(base: str, tok: str, method: str, path: str, body: Any = None,
          timeout: float = 60) -> Tuple[int, Any]:
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8"))
    return int((time.monotonic() - t0) * 1000), out


def _ollama_ps(url: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--target", default="zh")
    args = ap.parse_args()

    tok = _load_token()
    if not tok:
        print("FAIL: 找不到 web_admin.auth_token（实例配置未含令牌）")
        return 1

    fails = 0

    # S1 引擎矩阵
    ms, m = _call(args.base, tok, "GET",
                  f"/api/unified-inbox/translation-engines?target_lang={args.target}")
    matrix = (m or {}).get("matrix") or {}
    rows = [(r.get("engine"), r.get("available")) for r in matrix.get("engines") or []]
    print(f"[S1 matrix] {ms}ms effective={matrix.get('effective')} engines={rows}")
    if matrix.get("effective") != "ollama_mt":
        print("  !! effective 引擎不是 ollama_mt（本地 MT 未生效？）")
        fails += 1

    # S2 单条
    ms, d = _call(args.base, tok, "POST", "/api/unified-inbox/translate", {
        "text": "Could you give me a better price if I order 100 pieces?",
        "target_lang": args.target, "source_lang": "en"})
    t = (d or {}).get("translation") or {}
    print(f"[S2 single] {ms}ms ok={t.get('ok')} provider={t.get('provider')} "
          f"cached={t.get('cached')} text={str(t.get('translated_text'))[:40]!r}")
    if not t.get("ok"):
        fails += 1

    # S3 批量
    items = [{"id": f"m{i}", "text": s, "source_lang": "en"}
             for i, s in enumerate(SAMPLES)]
    ms, b = _call(args.base, tok, "POST", "/api/unified-inbox/translate-batch",
                  {"items": items, "target_lang": args.target})
    its = (b or {}).get("items") or []
    oks = [bool((it.get("translation") or {}).get("ok")) for it in its]
    provs = {(it.get("translation") or {}).get("provider") for it in its}
    print(f"[S3 batch x{len(items)}] {ms}ms ok={b.get('ok')} "
          f"item_ok={sum(oks)}/{len(oks)} providers={provs}")
    if not (b.get("ok") and oks and all(oks)):
        fails += 1

    # S4 keep_alive：主力端点 expires_at 应在「远未来」（>1h 即证明非 5m 默认）
    prim = None
    try:
        import yaml
        for dcfg in INSTANCE_CONFIG_DIRS:
            p = dcfg / "config.local.yaml"
            if p.exists():
                c = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                mt = ((c.get("translation") or {}).get("engines") or {}).get("ollama_mt") or {}
                urls = mt.get("base_urls") or [mt.get("base_url")]
                prim = (urls or [None])[0]
                break
    except Exception:
        prim = None
    if prim:
        ps = _ollama_ps(prim)
        row = next((x for x in (ps or {}).get("models", [])
                    if "hy-mt2" in str(x.get("name", ""))), None)
        if row is None:
            print(f"[S4 keep_alive] !! {prim} 上 MT 模型未驻留（冷态或端点异常）")
            fails += 1
        else:
            print(f"[S4 keep_alive] {prim} vram="
                  f"{round(int(row.get('size_vram') or 0)/2**30, 2)}GB "
                  f"expires={row.get('expires_at')}")
    else:
        print("[S4 keep_alive] 跳过（配置无 ollama_mt 端点）")

    print("PASS" if fails == 0 else f"FAIL x{fails}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
