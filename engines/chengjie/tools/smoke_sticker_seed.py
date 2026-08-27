# -*- coding: utf-8 -*-
"""表情包播种线上冒烟（2026-08-17 P2；重启窗装载贴纸路由后跑一次即验收）。

固化「重启后该做什么」四步，谁值守重启窗都不会漏：
  S1  /api/stickers/status —— flag 开 + 路由已装载（404=还没重启，默认 SKIP）；
  S2  POST /api/stickers/seed-official —— 幂等播种（starter-faces 本地包 +
      三个 LINE 官方包；重复跑零新增）；
  S3  GET /api/stickers/packs —— starter-faces 应有 10 张、LINE 包应在列；
  S4  拉一张 starter 贴纸的 /static URL 验 **magic bytes**（RIFF/WEBP）——
      媒体产物验证纪律：HTTP 200 / 体积 KB 数不构成内容验证。
  S5  WA 边车 /health caps.sticker（仅信息：false=Node 还没升级，WA 回退图片）。
  S6  （``--send`` 显式开）真发一张 starter 贴纸到 TG **收藏消息**验证端到端
      （chat_key 钉死 'me' 只发给账号自己；核对 sent_as=sticker + 观测计数入账）。

副作用：S2 的幂等播种（官方包本来就要播）；默认**不发送任何消息**，
仅 ``--send`` 时真发一条到自己的收藏消息。
WhatsApp 原生贴纸另有前置：Node 边车（whatsapp-baileys）需重启到带
caps.sticker 的版本，否则 send-sticker 自动回退图片（sent_as=image，不算坏）。

用法::

    python tools/smoke_sticker_seed.py            # 路由未装载 → SKIP exit 0
    python tools/smoke_sticker_seed.py --strict   # 重启窗验收：未装载算 FAIL
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：Windows ::1 回退每连 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
STARTER_PACK_ID = "starter-faces"
STARTER_EXPECT_ITEMS = 10
LINE_PACK_IDS = ("line-brown-cony-sally", "line-choco-friends",
                 "line-universtar-bt21")


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--strict", action="store_true",
                    help="路由未装载/flag 关也算 FAIL（重启窗验收模式）")
    ap.add_argument("--send", action="store_true",
                    help="S6：真发一张 starter 贴纸到 TG **收藏消息**"
                         "（chat_key 钉死 'me'，只发给账号自己）验证端到端")
    ap.add_argument("--account", default="",
                    help="S6 指定账号 id（跳过自动发现；排障隔离用）")
    args = ap.parse_args()

    import requests
    sess = requests.Session()
    fails = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(name)
        return ok

    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到 web_admin.auth_token（数据根不对？）")
        return 1 if args.strict else 0
    # Bearer 主令牌：CSRF 中间件对 Bearer 直接放行（admin.py csrf_middleware），
    # 页面鉴权/API 鉴权也都收主令牌——脚本调用不必模拟表单登录+CSRF 对。
    sess.headers["Authorization"] = f"Bearer {token}"
    try:
        sess.post(args.base + "/login", data={"auth_token": token}, timeout=10)
    except Exception as ex:  # noqa: BLE001
        print(f"[SKIP] 实例不可达 {args.base}: {ex}")
        return 1 if args.strict else 0

    def csrf_headers() -> dict:
        """cookie+header 成对（Bearer 之外的第二重，防鉴权口径日后变化）。"""
        tok = sess.cookies.get("csrf_token", "")
        return {"X-CSRF-Token": tok} if tok else {}

    print("== S1 路由/flag 探测 ==")
    r = sess.get(args.base + "/api/stickers/status", timeout=10)
    if r.status_code == 404:
        print("[SKIP] 贴纸路由未装载（.py 还没随重启窗上车）")
        return 1 if args.strict else 0
    st = r.json()
    if not st.get("enabled"):
        print("[SKIP] flag 关（inbox.stickers.enabled=false）")
        return 1 if args.strict else 0
    check("status 可读", True,
          f"packs={st.get('packs')} stickers={st.get('stickers')}")

    print("== S2 幂等播种 ==")
    r = sess.post(args.base + "/api/stickers/seed-official",
                  headers=csrf_headers(), timeout=120)
    ok = r.status_code == 200
    d = r.json() if ok else {}
    check("seed-official 200", ok, "" if ok else r.text[:200])
    if ok:
        print(f"      packs+{d.get('packs_added')} items+{d.get('items_added')}"
              "  (重复跑应为 +0/+0)")

    print("== S3 包清单核对 ==")
    packs = sess.get(args.base + "/api/stickers/packs", timeout=15).json()
    by_id = {p.get("id"): p for p in packs.get("packs") or []}
    starter = by_id.get(STARTER_PACK_ID) or {}
    n_starter = int(starter.get("count") or 0)
    check(f"{STARTER_PACK_ID} 在列且 >= {STARTER_EXPECT_ITEMS} 张",
          bool(starter) and n_starter >= STARTER_EXPECT_ITEMS,
          f"count={n_starter}")
    for pid in LINE_PACK_IDS:
        check(f"{pid} 在列", pid in by_id)

    print("== S4 静态产物 magic bytes ==")
    items = sess.get(
        args.base + f"/api/stickers/packs/{STARTER_PACK_ID}/items",
        timeout=15).json().get("items") or []
    file_items = [i for i in items if not i.get("line_only")]
    if check("starter 有本地条目", bool(file_items),
             f"n={len(file_items)}"):
        url = str(file_items[0].get("url") or "")
        rr = sess.get(args.base + url, timeout=15)
        data = rr.content or b""
        check("产物是真 WEBP（RIFF magic）",
              rr.status_code == 200 and data[:4] == b"RIFF"
              and data[8:12] == b"WEBP",
              f"http={rr.status_code} head={data[:4]!r} {len(data)}B")

    print("== S5 WA 边车原生贴纸能力（仅信息，不计败）==")
    # caps.sticker=false 只说明 Node 边车还没重启到带贴纸分支的版本——
    # send-sticker 会自动回退图片（sent_as=image），不算坏。
    try:
        import yaml
        cfg: dict = {}
        for name in ("config.yaml", "config.local.yaml"):
            fp = Path(args.data_root) / "config" / name
            if fp.exists():
                part = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
                if isinstance(part, dict):
                    cfg.update(part)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.integrations.whatsapp_baileys_login import service_base_url
        health = sess.get(service_base_url(cfg) + "/health", timeout=5).json()
        cap = bool(((health or {}).get("caps") or {}).get("sticker"))
        print(f"  [INFO] caps.sticker = {cap}"
              + ("" if cap else "（Node 边车待低峰重启后 WA 才走原生贴纸）"))
    except Exception as ex:  # noqa: BLE001
        print(f"  [INFO] 边车不可达/未配置（WA 发贴纸将回退图片）: {ex}")

    if args.send:
        print("== S6 真发验收（TG 收藏消息，只发给账号自己）==")
        # 账号自动发现：'default' 不是受管协议账号（owns_media 要确切 id）——
        # 从会话列表收集 telegram 账号，逐个探 send-caps 找 can_media 的
        # （smoke_voice_reuse S1.5 同款纪律）。
        acct = str(args.account or "")
        cand: list = [acct] if acct else []
        if not acct:
            ch = sess.get(args.base + "/api/unified-inbox/chats"
                          "?platform=telegram&limit=60", timeout=15).json()
            rows = ch.get("chats") or ch.get("conversations") or []
            for r0 in rows:
                a = str((r0 or {}).get("account_id") or "")
                if a and a not in cand:
                    cand.append(a)
            for a in cand[:8]:
                cap = sess.get(args.base + "/api/unified-inbox/send-caps",
                               params={"platform": "telegram",
                                       "account_id": a},
                               timeout=10).json()
                if cap.get("can_media"):
                    acct = a
                    break
        if check("S6 找到可发媒体的协议账号", bool(acct),
                 f"candidates={cand[:8]} picked={acct}") and file_items:
            import time as _t
            sid = str(file_items[0].get("id") or "")
            r = sess.post(
                args.base + "/api/unified-inbox/send-sticker",
                json={"platform": "telegram", "account_id": acct,
                      "chat_key": "me", "sticker_id": sid,
                      "client_msg_id": f"stk-smoke-{int(_t.time())}"},
                headers=csrf_headers(), timeout=60)
            d = r.json() if r.headers.get("content-type", "").startswith(
                "application/json") else {}
            check("S6 发送成功且 sent_as=sticker",
                  r.status_code == 200 and d.get("ok")
                  and d.get("sent_as") == "sticker",
                  f"http={r.status_code} resp={str(d)[:160]}")
            m = sess.get(args.base + "/api/workspace/metrics",
                         timeout=15).json()
            stk_m = (m or {}).get("stickers") or {}
            check("S6 观测计数已入账（sends>=1）",
                  int(stk_m.get("sends") or 0) >= 1, f"stats={stk_m}")

    n_fail = len(fails)
    print(f"\n== 冒烟结果: {'FAIL ' + str(fails) if n_fail else 'ALL PASS'} ==")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
