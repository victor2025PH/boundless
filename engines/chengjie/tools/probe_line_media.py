# -*- coding: utf-8 -*-
"""LINE 媒体链路探针（运维显式执行，非 pytest；2026-07-31 随 LINE 媒体补齐落地）。

**为什么需要它**：LINE 出站媒体默认关，而「该不该开」卡在两个只有真机能回答的问题上：

1. 这个号的会话是不是 **Letter Sealing(E2EE)**？okline 的媒体走 V1 明文流程，
   E2EE 会话会在「发占位消息」那一步被服务端拒。LINE 自己有
   ``determineMediaMessageFlow(chatMid)`` 这个判据接口，但**响应形状未知**--
   照着猜写预检，猜错就会变成「拒绝发送一切」。本探针把它打出来看。
2. 两步链（占位 → OBS 上传）在真实网络下稳不稳。

**为什么不做成 pytest**：本仓测试纪律是「测试自建 app/store，不依赖常驻服务」
（见 AGENTS.md）。这里要拿真 tokens 打 LINE 服务端，属运维演练，与
``tools/live_multiwin_drill.py`` 同族--故落 tools/ 并要求 ``--confirm`` 才真发。

**安全设计**：

  1. **默认一条消息都不发**：只读探能力开关 + 打 ``determineMediaMessageFlow``
     （查询类 RPC）+ 报告。要真发必须 ``--confirm``。
  2. 真发的**默认目标是账号自己的 mid**（LINE 的 Keep memo，客户永不可见）；
     指向真人 peer 必须同时给 ``--to`` 与 ``--allow-peer``，且会二次告警。
  3. 真发用的是**当场生成的一张小测试图**，不是人设自拍/相册内容。
  4. 建**一次性 client**（``OkLine.from_tokens_file`` + finally close），与运行中
     worker 的长轮询连接分离--这正是 ``LineProtocolWorker._sync_bootstrap_blocking``
     每次重启都在用的既有模式，对活体账号已被生产验证。
  5. 只读连接读注册表（``mode=ro``），绝不写生产库。

用法::

    python tools/probe_line_media.py                       # 只读探测（默认）
    python tools/probe_line_media.py --json                # 机器可读
    python tools/probe_line_media.py --send-self --confirm # 真发一张测试图到 Keep memo
    python tools/probe_line_media.py --to U<mid> --allow-peer --confirm
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402


def _registry_line_accounts(root: Path) -> List[Dict[str, Any]]:
    """只读列出该数据根下的 LINE 账号（含 meta.tokens_path）。"""
    db = root / "config" / "account_registry.db"
    if not db.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        uri = "file:%s?mode=ro" % str(db).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute(
                "SELECT account_id, mode, status, meta_json FROM platform_accounts"
                " WHERE platform='line'"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        print("  [warn] 读注册表失败：%s" % exc)
        return []
    for account_id, mode, status, meta_json in rows:
        try:
            meta = json.loads(meta_json or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({
            "account_id": str(account_id or ""),
            "mode": str(mode or ""),
            "status": str(status or ""),
            "tokens_path": str((meta or {}).get("tokens_path") or ""),
        })
    return out


def _resolve_tokens_path(cfg: Dict[str, Any], acct: Dict[str, Any],
                         root: Path) -> str:
    """与 ``LineProtocolWorker.start`` 同一口径：meta 优先，回落约定路径。

    ⚠ 注册表里存的是 **CWD 相对**路径（``sessions\\line\\<id>.json``）：服务进程的
    CWD 就是实例数据根，所以它在生产里恰好正确；而从引擎根跑的 CLI 会解析到
    ``<引擎根>\\sessions\\…`` 找不到文件——正是 AGENTS.md「CWD 相对路径＝迁移后的
    静默失真」那一节讲的坑。故相对路径一律按**数据根**展开。
    """
    def _try(p: str) -> str:
        if not p:
            return ""
        cand = Path(p)
        if not cand.is_absolute():
            cand = Path(root) / p
        return str(cand) if cand.is_file() else ""

    hit = _try(str(acct.get("tokens_path") or ""))
    if hit:
        return hit
    try:
        from src.integrations.line_protocol_login import tokens_path as _tp
        return _try(_tp(cfg, acct["account_id"]) or "")
    except Exception:  # noqa: BLE001
        return ""


def pick_line_account(accounts, wanted_id=""):
    """拣探测账号：显式 id 优先；否则 online 优先，避免离线号让 get_profile 空转。"""
    wanted = str(wanted_id or "").strip()
    alive = []
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("status") or "") == "removed":
            continue
        if wanted and str(a.get("account_id") or "") != wanted:
            continue
        alive.append(a)
    if not alive:
        return None
    online = [a for a in alive if str(a.get("status") or "") == "online"]
    return (online or alive)[0]


def check_media_magic(path: str, kind: str = "") -> dict:
    """落盘媒体的 magic-byte 校验（2026-08-13 事故：HTTP 200 + KB 数不等于真音频）。

    返回 ``{"ok": bool, "container": str, "reason": str}``。缺文件 / 头对不上 → ok=False。
    """
    if not path or not os.path.isfile(path):
        return {"ok": False, "container": "", "reason": "missing_file"}
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return {"ok": False, "container": "", "reason": "unreadable:%s" % exc}
    tag = ""
    if head[:2] == b"\xff\xd8":
        tag = "JPEG"
    elif head[:4] == b"\x89PNG":
        tag = "PNG"
    elif head[:4] == b"GIF8":
        tag = "GIF"
    elif head[:4] == b"OggS":
        tag = "OGG"
    elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        tag = "WAV"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        tag = "WEBP"
    elif len(head) >= 8 and head[4:8] == b"ftyp":
        tag = "MP4"
    elif head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        tag = "MP3"
    if not tag:
        return {
            "ok": False, "container": "",
            "reason": "bad_magic head=%s" % head[:8].hex(),
        }
    kind_l = str(kind or "").strip().lower()
    expect = {
        "image": {"JPEG", "PNG", "GIF", "WEBP"},
        "voice": {"OGG", "WAV", "MP3", "MP4"},
        "audio": {"OGG", "WAV", "MP3", "MP4"},
        "video": {"MP4", "OGG"},
        "sticker": {"JPEG", "PNG", "GIF", "WEBP"},
    }.get(kind_l)
    if expect and tag not in expect:
        return {
            "ok": False, "container": tag,
            "reason": "kind_mismatch kind=%s container=%s" % (kind_l, tag),
        }
    return {"ok": True, "container": tag, "reason": ""}


def _make_test_image() -> str:
    """当场画一张明确写着「测试」的小图（不用人设自拍/相册内容）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (480, 240), (24, 28, 38))
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, 471, 231], outline=(90, 140, 220), width=3)
    d.text((28, 100), "ChatX LINE media probe", fill=(210, 225, 245))
    path = os.path.join(tempfile.gettempdir(), "line_media_probe.jpg")
    img.save(path, "JPEG", quality=88)
    return path


def probe(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    cfg = load_merged_config(root)
    from src.integrations.line_media import resolve_line_media_cfg

    mcfg = resolve_line_media_cfg(cfg)
    report: Dict[str, Any] = {
        "data_root": str(root),
        "media_cfg": mcfg,
        # 出站开关就是编排器 owns_media() 的实际判据（worker 构造时按它绑 send_media）
        "expected_owns_media": bool(mcfg.get("outbound")),
        "accounts": [],
        "media_flow": [],
        "send": None,
    }

    accounts = _registry_line_accounts(root)
    report["accounts"] = accounts
    if not accounts:
        report["error"] = "该数据根下没有 LINE 账号"
        return report

    acct = pick_line_account(accounts, args.account)
    if acct is None:
        report["error"] = "没有可用的 LINE 账号（--account 不匹配或全部 removed）"
        return report
    report["picked_account"] = acct["account_id"]
    report["picked_status"] = acct.get("status") or ""

    tokens = _resolve_tokens_path(cfg, acct, root)
    report["tokens_path"] = tokens
    if not tokens:
        report["error"] = "找不到该账号的 session tokens 文件"
        return report

    try:
        from src.integrations.line_protocol_login import ensure_node_runtime
        report["node_runtime"] = bool(ensure_node_runtime(cfg))
    except Exception as exc:  # noqa: BLE001
        report["node_runtime"] = False
        report["node_error"] = str(exc)[:200]

    from okline import OkLine
    client = OkLine.from_tokens_file(tokens)
    try:
        try:
            prof = client.get_profile() or {}
            own_mid = str(prof.get("mid") or "") if isinstance(prof, dict) else ""
        except Exception as exc:  # noqa: BLE001
            own_mid = ""
            report["profile_error"] = str(exc)[:200]
        report["own_mid"] = own_mid
        if not own_mid:
            # 默认只读若拣到离线号会走到这里：必须红字，不许空 flowMap 假装探过。
            extra = report.get("profile_error") or "empty mid"
            report["error"] = (
                "get_profile 失败，无法探 media flow（账号 status=%s）: %s"
                % (acct.get("status") or "?", extra))
            return report

        # ── 只读：媒体流程判据（本探针存在的首要理由）───────────────────────
        targets = [t for t in ([own_mid] + list(args.flow_mid or [])) if t]
        for mid in targets[:5]:
            item: Dict[str, Any] = {"chat_mid": mid}
            try:
                item["response"] = client.determine_media_message_flow(mid)
            except Exception as exc:  # noqa: BLE001
                item["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            report["media_flow"].append(item)

        # ── 只读：回读某条已发媒体，确认服务端真的登记了内容 ─────────────────
        # 「delivered=true」只说明两步调用都没抛；这一步才回答「客户点开真有图吗」
        # （破图正是本模块最防的失败形态）。
        if args.verify_msg:
            # 走 getRecentMessagesV2 而不是 getMessagesByIds——后者 okline 的参数形状
            # 与服务端对不上（真机实测 THRIFT_INVALID_ARGUMENT code=10102），不值得
            # 为一次校验去逆向它。
            box = str(args.verify_box or "").strip() or own_mid
            item: Dict[str, Any] = {"message_id": args.verify_msg, "box": box}
            try:
                rows = client.get_recent_messages(box, 20) or []
                hit = next((r for r in rows
                            if str((r or {}).get("id") or "") == args.verify_msg), None)
                if hit is None:
                    item["found"] = False
                else:
                    item["found"] = True
                    item["contentType"] = hit.get("contentType")
                    item["hasContent"] = hit.get("hasContent")
                    item["contentMetadata"] = hit.get("contentMetadata")
            except Exception as exc:  # noqa: BLE001
                item["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            report["verify"] = item

        # ── 只读：入站链路真机验证 ───────────────────────────────────────────
        # 入站此前只有单测：LINE 号历史 0 条入站消息，没人给它发过图，所以「下载 →
        # 落 /static → VLM 识图」这条链从没在真机上跑通过。这里拿一个**真实存在的
        # OBS 对象**（比如本探针自己发出去的那张图的 message id）走**生产同一个**
        # download_line_media，把那半条链也证明掉。全程只读（下载自己的对象）。
        if args.verify_inbound:
            from src.integrations.line_media import download_line_media
            fake_msg = {
                "id": args.verify_inbound,
                "contentType": int(args.content_type),
                "contentMetadata": {},
            }
            item: Dict[str, Any] = {"message_id": args.verify_inbound,
                                    "contentType": int(args.content_type)}
            kind, url = download_line_media(
                client, fake_msg, acct["account_id"], cfg=mcfg)
            item["kind"], item["media_ref"] = kind, url
            local = ""
            if url:
                from src.integrations.protocol_bridge import static_media_ref_to_path
                local = static_media_ref_to_path(url) or ""
                item["local"] = local
                if local and os.path.isfile(local):
                    item["bytes"] = os.path.getsize(local)
                    magic = check_media_magic(local, kind)
                    item["magic"] = magic
                    if not magic.get("ok"):
                        report["error"] = (
                            "入站落盘未过 magic bytes（kind=%s bytes=%s %s）——HTTP 200/KB 数不算验证"
                            % (kind, item.get("bytes"), magic.get("reason") or "fail"))
                else:
                    report["error"] = "入站拿到 URL 但本地文件不存在 ref=%s" % url
            else:
                report["error"] = "入站下载未取到媒体（kind=%s ref 空）" % (kind or "?")
            # 再把它喂给平台无关的识别层——这一步才回答「AI 到底看不看得见」
            if local and args.enrich:
                import asyncio
                from src.inbox.media_enrich import enrich_inbound_media_text
                try:
                    text, desc = asyncio.run(enrich_inbound_media_text(
                        media_type=kind, media_ref=url, config=cfg))
                    item["ai_text"] = (text or "")[:300]
                    item["vlm_desc"] = (desc or "")[:300]
                except Exception as exc:  # noqa: BLE001
                    item["enrich_error"] = "%s: %s" % (type(exc).__name__,
                                                       str(exc)[:200])
            report["inbound"] = item

        if not args.confirm:
            return report

        # ── 真发（需 --confirm）────────────────────────────────────────────
        to = str(args.to or "").strip() or own_mid
        if not to:
            report["send"] = {"skipped": "no_target"}
            return report
        if args.to and not args.allow_peer:
            report["send"] = {"skipped": "peer_target_needs_--allow-peer"}
            return report
        if args.to and args.allow_peer:
            print("  [!] 目标是真人会话 %s -- 对方会真的收到一张测试图。" % to)

        # ⚠ 一次性 client 的 ``_reqseq`` 从 0 起，而服务端去重键是
        # **(reqSeq, 消息内容)**（2026-07-31 真机实测）。两次探针都以 reqSeq=1 发
        # **完全相同**的图片占位 → 第二次被判重、返回上一条的 message id → 上传撞
        # HTTP 423 Locked。2026-09-02 起统一走 ``bump_client_reqseq``（与长驻
        # worker 启动同一套时间基线，跨进程/跨重启单调不重叠）。
        from src.integrations.line_media import bump_client_reqseq
        bump_client_reqseq(client, account_id=own_mid or "probe")

        from src.integrations.line_media import send_line_media
        img = _make_test_image()
        res = send_line_media(
            client, to, media_path=img, media_type="image",
            caption=str(args.caption or ""), cfg=mcfg,
        )
        report["send"] = {"to": to, "image": img, "result": res}
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return report


def _print(report: Dict[str, Any]) -> None:
    # 输出刻意不用 v/x 之类符号：PS5.1 控制台是 GBK，编不出就整条 traceback
    # （本仓 watchdog_emotion_tts.ps1 踩过同一个坑，那边的结论是 ASCII-only）。
    print("-- LINE media probe ----------------------------------")
    print("  data_root      : %s" % report.get("data_root"))
    m = report.get("media_cfg") or {}
    print("  开关            : inbound=%s outbound=%s stickers=%s groups=%s"
          % (m.get("inbound"), m.get("outbound"), m.get("stickers"), m.get("groups")))
    print("  owns_media 预期 : %s（出站开关即编排器判据）"
          % report.get("expected_owns_media"))
    for a in report.get("accounts") or []:
        print("  账号            : %s mode=%s status=%s"
              % (a["account_id"], a["mode"], a["status"]))
    if report.get("picked_account"):
        print("  拣号            : %s status=%s"
              % (report.get("picked_account"), report.get("picked_status")))
    # 错误不提前 return：入站 magic 失败时仍要把路径/字节打出来，方便对账。
    if report.get("error") and not report.get("own_mid") and not report.get("inbound"):
        print("  x %s" % report["error"])
        return
    print("  own_mid         : %s" % (report.get("own_mid") or "(取不到)"))
    print("  node_runtime    : %s" % report.get("node_runtime"))
    print("  -- determineMediaMessageFlow（E2EE 预检的形状探明）--")
    for it in report.get("media_flow") or []:
        if "error" in it:
            print("    %s → x %s" % (it["chat_mid"], it["error"]))
        else:
            print("    %s → %s" % (it["chat_mid"], json.dumps(
                it.get("response"), ensure_ascii=False)[:400]))
    ib = report.get("inbound")
    if ib:
        print("  -- 入站链路（真实 OBS 对象 -> 生产下载函数）--")
        print("    kind=%s bytes=%s ref=%s"
              % (ib.get("kind"), ib.get("bytes"), ib.get("media_ref") or "(未取到)"))
        mag = ib.get("magic") or {}
        if mag:
            print("    magic          : ok=%s container=%s %s"
                  % (mag.get("ok"), mag.get("container") or "-", mag.get("reason") or ""))
        if ib.get("vlm_desc") is not None or ib.get("enrich_error"):
            print("    VLM: %s" % (ib.get("vlm_desc") or ib.get("enrich_error")))
            print("    喂给 AI 的文本: %s" % (ib.get("ai_text") or ""))
    snd = report.get("send")
    if snd is None:
        print("  -- 真发：未执行（默认只读；加 --confirm 才发）--")
    elif snd.get("skipped"):
        print("  -- 真发：跳过（%s）--" % snd["skipped"])
    else:
        r = snd.get("result") or {}
        ok = "v 送达" if r.get("delivered") else "x 未送达"
        print("  -- 真发 → %s to=%s %s --" % (ok, snd.get("to"),
                                              json.dumps(r, ensure_ascii=False)))
    try:
        from src.integrations.line_media_stats import get_line_media_stats
        print("  本轮计数        : %s" % json.dumps(
            get_line_media_stats().dump().get("outbound"), ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    if report.get("error"):
        print("  x %s" % report["error"])


def main() -> int:
    ap = argparse.ArgumentParser(description="LINE 媒体链路探针（默认只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--account", default="", help="指定 LINE account_id")
    ap.add_argument("--flow-mid", action="append", default=[],
                    help="额外要探 determineMediaMessageFlow 的 chat mid（可重复）")
    ap.add_argument("--send-self", action="store_true",
                    help="真发目标=自己的 mid（Keep memo）；仍需 --confirm")
    ap.add_argument("--to", default="", help="真发目标 mid（需配 --allow-peer）")
    ap.add_argument("--allow-peer", action="store_true",
                    help="允许把测试图发给真人会话（默认禁止）")
    ap.add_argument("--verify-msg", default="",
                    help="只读回读某条已发媒体（确认服务端登记了内容，非仅无异常）")
    ap.add_argument("--verify-box", default="",
                    help="回读所在会话 mid（缺省=自己的 mid）")
    ap.add_argument("--verify-inbound", default="",
                    help="用真实 OBS 对象验入站下载链（传 message id，只读）")
    ap.add_argument("--content-type", type=int, default=1,
                    help="--verify-inbound 的 contentType（1图 2视频 3音频 14文件）")
    ap.add_argument("--enrich", action="store_true",
                    help="入站验证顺带跑 VLM/ASR 识别（证明 AI 真能看见）")
    ap.add_argument("--caption", default="", help="随图补发的文字（默认不发）")
    ap.add_argument("--confirm", action="store_true", help="真的发送（否则只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.confirm and not (args.send_self or args.to):
        print("--confirm 需配 --send-self 或 --to（不猜目标）")
        return 2

    rc = 0
    for root in resolve_data_roots(args.data_root):
        try:
            rep = probe(root, args)
        except Exception as exc:  # noqa: BLE001
            rep = {"data_root": str(root), "error": "%s: %s"
                   % (type(exc).__name__, str(exc)[:300])}
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        else:
            _print(rep)
        if rep.get("error"):
            rc = 1
        snd = rep.get("send") or {}
        if snd.get("result") and not snd["result"].get("delivered"):
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
