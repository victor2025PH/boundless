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

#172 / #169（J-3，2026-09-05）新增的真机定层能力（全部只读，除 ``--send-self``）::

    # 列某会话（缺省 Keep memo=自己）最近 N 条消息：id/类型/龄/是否 Letter Sealing/元数据键
    python tools/probe_line_media.py --list-recent 30 [--box U<mid>]
    # 把该会话里每一条媒体消息都走**生产同一个** download_line_media：HTTP 状态 / miss
    # 细因 / object_info 服务端状态 / 落盘 magic —— 手机先给自己发 10s 视频+语音+贴纸，
    # 再跑这一条，四类各自的真实路径与判定就全出来了
    python tools/probe_line_media.py --verify-recent-media [--box U<mid>] [--enrich]
    # 出站视频 / 语音（ffmpeg 现场合成 10s testsrc / 3s 正弦）发到 Keep memo 并**回读**
    # 验 magic（ftyp / OggS|M4A）——出站视频/语音此前从未真机跑过
    python tools/probe_line_media.py --send-self --kind video --confirm
    python tools/probe_line_media.py --send-self --kind voice --confirm
    # #169 上限实测：任意文件（如 30/60/100MB 的 mp4）作为 video 发到 Keep memo
    python tools/probe_line_media.py --send-self --kind video --file big.mp4 --max-mb 120 --confirm
    # 贴纸商店 CDN 链（零账号依赖）
    python tools/probe_line_media.py --sticker-check
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
    elif head[:4] == b"\x30\x26\xb2\x75":
        tag = "ASF"   # .wma / .wmv
    elif head[:5] == b"%PDF-":
        tag = "PDF"
    elif head[:4] == b"PK\x03\x04":
        tag = "ZIP"   # docx/xlsx/apk 等
    kind_l = str(kind or "").strip().lower()
    if kind_l in ("document", "file"):
        # 任意文件：没有「该长什么样」的先验，只要非空即算过（容器名仅供参考）
        return {"ok": True, "container": tag or "-", "reason": ""}
    if not tag:
        return {
            "ok": False, "container": "",
            "reason": "bad_magic head=%s" % head[:8].hex(),
        }
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


def _ffmpeg() -> str:
    try:
        from src.utils.ffmpeg_resolver import ffmpeg_path
        return ffmpeg_path() or ""
    except Exception:  # noqa: BLE001
        return ""


def _make_test_video(seconds: int = 10) -> str:
    """ffmpeg testsrc 合成 N 秒 H.264 小视频（带静音音轨，LINE 侧转码要有音轨才稳）。"""
    import subprocess
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg 不可用（resolver 找不到），无法合成测试视频")
    path = os.path.join(tempfile.gettempdir(), "line_media_probe_%ds.mp4" % seconds)
    subprocess.run(
        [ff, "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", "-shortest", path],
        check=True, timeout=120, capture_output=True)
    return path


def _make_test_voice(seconds: int = 3) -> str:
    """ffmpeg 正弦音合成 N 秒 OGG/Opus（与全平台 TTS 产物同容器；发送侧自会转 M4A）。"""
    import subprocess
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg 不可用（resolver 找不到），无法合成测试语音")
    path = os.path.join(tempfile.gettempdir(), "line_media_probe_%ds.ogg" % seconds)
    subprocess.run(
        [ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", str(seconds), "-c:a", "libopus", "-b:a", "32k", path],
        check=True, timeout=60, capture_output=True)
    return path


_MEDIA_CT_LABEL = {1: "image", 2: "video", 3: "voice", 7: "sticker", 14: "document"}


def summarize_message(m: Dict[str, Any], now_ms: Optional[float] = None) -> Dict[str, Any]:
    """一条 LINE 消息 → 定层要看的字段（纯函数）：龄 / Letter Sealing / SID / OID / 元数据键。"""
    from src.integrations.line_media import (
        is_e2ee_media, line_message_age_sec, obs_object_locator,
    )
    meta = m.get("contentMetadata") if isinstance(m.get("contentMetadata"), dict) else {}
    try:
        ct = int(m.get("contentType") or 0)
    except (TypeError, ValueError):
        ct = -1
    age = line_message_age_sec(m, None if now_ms is None else now_ms / 1000.0)
    item: Dict[str, Any] = {
        "id": str(m.get("id") or ""),
        "contentType": ct,
        "kind": _MEDIA_CT_LABEL.get(ct, "text" if ct == 0 else "other"),
        "from": str(m.get("from") or "")[:12],
        "age_h": round(age / 3600.0, 2) if age >= 0 else None,
        "e2ee": is_e2ee_media(m),
        "chunks": bool(m.get("chunks")),
        "e2eeVersion": meta.get("e2eeVersion"),
        "SID": meta.get("SID"), "OID": meta.get("OID"), "OBS_POP": meta.get("OBS_POP"),
        "meta_keys": sorted(str(k) for k in meta.keys()),
    }
    if ct in _MEDIA_CT_LABEL and ct != 7:
        sid, oid_path = obs_object_locator(m)
        item["obs_path"] = "/r/talk/%s/%s" % (sid, oid_path)
    return item


def _verify_media_message(client: Any, m: Dict[str, Any], account_id: str,
                          mcfg: Dict[str, Any], *, enrich: bool, cfg: Dict[str, Any]
                          ) -> Dict[str, Any]:
    """一条媒体消息走生产 ``download_line_media``（含 E2EE 解密 / 404 分档）+ magic + object_info。"""
    from src.integrations.line_media import download_line_media, obs_object_info
    item = summarize_message(m)
    out: Dict[str, Any] = {}
    kind, url = download_line_media(client, m, account_id, cfg=mcfg, out=out)
    item["kind"], item["media_ref"] = kind, url
    item["miss"] = {k: out.get(k) for k in ("reason", "http_status", "retryable", "expired",
                                            "obs_status", "path", "detail")}
    local = ""
    if url:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        local = static_media_ref_to_path(url) or ""
        item["local"] = local
        if local and os.path.isfile(local):
            item["bytes"] = os.path.getsize(local)
            item["magic"] = check_media_magic(local, kind)
        else:
            item["magic"] = {"ok": False, "container": "", "reason": "missing_file"}
    else:
        # 失败时把服务端对象状态单独打一遍（生产链只在 404 时问它）
        try:
            item["object_info"] = obs_object_info(client, m)
        except Exception as exc:  # noqa: BLE001
            item["object_info"] = {"error": str(exc)[:120]}
    if local and enrich and kind in ("image", "video", "voice", "audio", "sticker"):
        import asyncio
        from src.inbox.media_enrich import enrich_inbound_media_text
        try:
            text, desc = asyncio.run(enrich_inbound_media_text(
                media_type=kind, media_ref=url, config=cfg))
            item["ai_text"] = (text or "")[:200]
            item["vlm_desc"] = (desc or "")[:200]
        except Exception as exc:  # noqa: BLE001
            item["enrich_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    return item


_E2EE_SID = {"image": "emi", "video": "emv", "voice": "ema", "audio": "ema", "document": "emf"}
_E2EE_CT = {"image": 1, "video": 2, "voice": 3, "audio": 3, "document": 14}


def send_e2ee_media_probe(client: Any, to: str, path: str, kind: str) -> Dict[str, Any]:
    """按 LINE Chrome 客户端 ``nE()`` 的 e2ee-next 流程发一条 **Letter Sealing 媒体**（探针专用）。

    生产出站仍走 V1 明文占位流程（``send_line_media``）；这条只为把入站侧的
    ``talk/em*/<OID>`` + ``X-Talk-Meta`` + keyMaterial 解密链在真服务端上闭环验证
    （skuio 机上钧发来的图/视频就是这种消息，117 号手机对媒体没开 Letter Sealing、
    收不到样本——只能自己造一条）。步骤：

    1. ``ENC_KM`` = b64(随机 32 字节)；``encrypt_e2ee_media`` 出密文（视频另传 128KB
       分块哈希到 ``<OID>__ud-hash``）；
    2. ``POST /r/talk/<emi|emv|ema|emf>/reqid-<uuid>``，``X-Obs-Params``
       = b64({ver:"2.0", name, type:"file"})，鉴权用**加密 access token**；响应头
       ``x-obs-oid`` 即真 OID；
    3. 消息 ``contentMetadata`` 带 ``SID``/``OID``/``FILE_SIZE``，chunks 里封
       ``{"keyMaterial","fileName"}``（复用 okline E2EE 信道原语），``sendMessage``。
    """
    import base64 as _b64
    import json as _json
    import secrets
    import uuid
    from okline import e2ee_crypto as fr
    from okline.enums import EncryptedAccessTokenFeatureType
    from src.integrations.line_media import encrypt_e2ee_media, _media_hmac_input  # noqa: PLC2701

    kind = str(kind or "image").lower()
    sid = _E2EE_SID.get(kind, "emf")
    ct = _E2EE_CT.get(kind, 14)
    with open(path, "rb") as fh:
        plain = fh.read()
    enc_km = _b64.b64encode(secrets.token_bytes(32)).decode("ascii")
    blob = encrypt_e2ee_media(plain, enc_km, is_video=(kind == "video"))
    fname = os.path.basename(path)

    e2ee = getattr(client, "e2ee", None)
    if e2ee is None or not e2ee.is_ready():
        return {"delivered": False, "error": "e2ee_not_ready"}

    enc_token = client.get_encrypted_access_token(int(EncryptedAccessTokenFeatureType.OBS_GENERAL))
    tr = client.transport
    base = tr.config.obs_base

    def _post(oid: str, body: bytes, name: str) -> Any:
        headers = tr.base_headers()
        headers.pop("content-type", None)
        headers["X-Line-Access"] = enc_token
        headers["X-Obs-Params"] = _b64.b64encode(_json.dumps(
            {"ver": "2.0", "name": name, "type": "file"}, separators=(",", ":")
        ).encode("utf-8")).decode("ascii")
        return tr._send("POST", "%s/r/talk/%s/%s" % (base, sid, oid), headers=headers, data=body)  # noqa: SLF001

    import time as _t
    resp = _post("reqid-%s" % uuid.uuid4(), blob, str(int(_t.time() * 1000)))
    if resp.status_code >= 400:
        return {"delivered": False, "error": "obs_upload_http_%d" % resp.status_code,
                "body": (resp.text or "")[:200], "sid": sid}
    oid = resp.headers.get("x-obs-oid") or ""
    if not oid:
        return {"delivered": False, "error": "no_x_obs_oid", "sid": sid,
                "resp_headers": dict(resp.headers)}
    hash_status = None
    if kind == "video":
        # Chrome ``gv`` 的 chunkHashList：128KB 分块 SHA-256 拼接，传到 <OID>__ud-hash
        body = blob[:-32]
        hashes = _media_hmac_input(body, is_video=True)
        r2 = _post("%s__ud-hash" % oid, hashes, "chunk-hash")
        hash_status = r2.status_code

    # ── 消息：明文 JSON {keyMaterial, fileName} 封进 chunks，SID/OID 走明文元数据 ──
    channel, my_kid, peer_kid = e2ee._channel_for_send(to)  # noqa: SLF001
    plaintext = _json.dumps({"keyMaterial": enc_km, "fileName": fname},
                            separators=(",", ":")).encode("utf-8")
    ct_b64 = e2ee._bridge.e2ee_encrypt_v2(  # noqa: SLF001
        channel, to=to, frm=e2ee.my_mid, sender_key_id=my_kid, receiver_key_id=peer_kid,
        content_type=ct, sequence_number=e2ee._next_seq(),  # noqa: SLF001
        plaintext_b64=_b64.b64encode(plaintext).decode("ascii"))
    chunks = fr.build_chunks(_b64.b64decode(ct_b64), my_kid, peer_kid)
    meta: Dict[str, Any] = {"SID": sid, "OID": oid, "FILE_SIZE": str(len(plain))}
    if kind in ("video", "voice", "audio"):
        from src.integrations.line_media import _probe_duration_ms  # noqa: PLC2701
        meta["DURATION"] = str(_probe_duration_ms(path) or 0)
    msg = {"to": to, "toType": 0, "contentType": ct, "contentMetadata": meta}
    sealed = fr.build_e2ee_message(msg, chunks, 2)
    sent = client.send_message(sealed)
    mid = str((sent or {}).get("id") or "") if isinstance(sent, dict) else ""
    return {"delivered": bool(mid), "message_id": mid, "sid": sid, "oid": oid,
            "enc_km": enc_km, "cipher_bytes": len(blob), "plain_bytes": len(plain),
            "hash_upload_status": hash_status}


def sticker_check(mcfg: Dict[str, Any], stkid: str = "52002734",
                  pkgid: str = "11537") -> Dict[str, Any]:
    """贴纸链零账号依赖：商店 CDN 静态图 → 生产 download_line_media（sticker 分支）→ magic。

    缺省贴纸＝LINE 免费默认包 Brown & Cony（pkg 11537 / stk 52002734）。
    """
    from src.integrations.line_media import download_line_media
    fake = {"id": "probe-sticker", "contentType": 7,
            "contentMetadata": {"STKID": stkid, "STKPKGID": pkgid, "STKTXT": ""}}
    out: Dict[str, Any] = {}
    kind, url = download_line_media(None, fake, "probe", cfg=mcfg, out=out)
    item: Dict[str, Any] = {"stkid": stkid, "pkgid": pkgid, "kind": kind, "media_ref": url,
                            "miss": out.get("reason")}
    if url:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        local = static_media_ref_to_path(url) or ""
        item["local"] = local
        if local and os.path.isfile(local):
            item["bytes"] = os.path.getsize(local)
            item["magic"] = check_media_magic(local, "sticker")
    return item


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

    if getattr(args, "sticker_check", False):
        # 贴纸走商店 CDN，不需要账号——先做，数据根没号也能出结论
        report["sticker"] = sticker_check(mcfg, stkid=str(args.stkid or "52002734"),
                                          pkgid=str(args.stkpkgid or "11537"))
        st = report["sticker"]
        if not st.get("media_ref") or not (st.get("magic") or {}).get("ok"):
            report["error"] = "贴纸 CDN 链失败：%s" % (st.get("miss") or (st.get("magic") or {}).get("reason"))

    accounts = _registry_line_accounts(root)
    report["accounts"] = accounts
    if not accounts:
        if getattr(args, "sticker_check", False) and not report.get("error"):
            report["note"] = "该数据根下没有 LINE 账号（贴纸检查不需要账号，已完成）"
            return report
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

        # ── 只读：列最近消息 / 逐条媒体验证（#172 真机定层）────────────────────
        box = str(getattr(args, "box", "") or "").strip() or own_mid
        n_recent = int(getattr(args, "list_recent", 0) or 0)
        want_verify = bool(getattr(args, "verify_recent_media", False))
        if n_recent > 0 or want_verify:
            rows: List[Dict[str, Any]] = []
            try:
                rows = list(client.get_recent_messages(box, max(n_recent, 30) if want_verify
                                                      else n_recent) or [])
            except Exception as exc:  # noqa: BLE001
                report["recent_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            report["box"] = box
            if n_recent > 0:
                report["recent"] = [summarize_message(m) for m in rows[:n_recent]
                                    if isinstance(m, dict)]
            if want_verify:
                verified: List[Dict[str, Any]] = []
                for m in rows:
                    if not isinstance(m, dict):
                        continue
                    try:
                        ct = int(m.get("contentType") or 0)
                    except (TypeError, ValueError):
                        continue
                    if ct not in _MEDIA_CT_LABEL:
                        continue
                    verified.append(_verify_media_message(
                        client, m, acct["account_id"], mcfg, enrich=bool(args.enrich), cfg=cfg))
                report["verified_media"] = verified
                bad = [v for v in verified
                       if v.get("media_ref") and not (v.get("magic") or {}).get("ok")]
                if bad:
                    report["error"] = "有 %d 条媒体拿到 URL 但落盘未过 magic（见 verified_media）" % len(bad)

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
        kind = str(getattr(args, "kind", "") or "image").strip().lower()
        media_file = str(getattr(args, "file", "") or "").strip()
        if media_file:
            if not os.path.isfile(media_file):
                report["error"] = "--file 不存在：%s" % media_file
                return report
            path = media_file
        elif kind == "video":
            path = _make_test_video(10)
        elif kind in ("voice", "audio"):
            path = _make_test_voice(3)
        else:
            kind = "image"
            path = _make_test_image()
        send_cfg = dict(mcfg)
        max_mb = float(getattr(args, "max_mb", 0) or 0)
        if max_mb > 0:
            # #169 上限实测：放开出站体积护栏（只影响本次探针，不动生产配置）
            send_cfg["outbound_max_bytes"] = int(max_mb * 1024 * 1024)
        size = os.path.getsize(path)
        t0 = __import__("time").time()
        use_e2ee = bool(getattr(args, "e2ee", False))
        if use_e2ee:
            res = send_e2ee_media_probe(client, to, path, kind)
        else:
            res = send_line_media(
                client, to, media_path=path, media_type=kind,
                caption=str(args.caption or ""), cfg=send_cfg,
            )
        elapsed = round(__import__("time").time() - t0, 1)
        send_item: Dict[str, Any] = {"to": to, "kind": kind, "file": path, "bytes": size,
                                     "elapsed_s": elapsed, "e2ee": use_e2ee, "result": res}
        # 回读：拿刚发出去的 message id 走生产入站下载链 + magic（出站→入站闭环，
        # 出站视频/语音此前从未真机跑过——「delivered=True」不等于对方能播）。
        # E2EE 场景回读用**服务端返回的真消息**（含 chunks/SID/OID），与生产入站同形。
        mid = str((res or {}).get("message_id") or "")
        if res.get("delivered") and mid and not getattr(args, "no_readback", False):
            ct = {"image": 1, "video": 2, "voice": 3, "audio": 3}.get(kind, 1)
            fake: Dict[str, Any] = {"id": mid, "contentType": ct, "contentMetadata": {},
                                    "createdTime": str(int(__import__("time").time() * 1000))}
            if use_e2ee:
                try:
                    rows = client.get_recent_messages(to, 20) or []
                    hit = next((r for r in rows if str((r or {}).get("id") or "") == mid), None)
                    if hit is not None:
                        fake = dict(hit)
                        send_item["server_message"] = summarize_message(hit)
                    else:
                        send_item["server_message"] = "not_found_in_recent"
                except Exception as exc:  # noqa: BLE001
                    send_item["server_message"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            rb_cfg = dict(mcfg)
            if max_mb > 0:
                rb_cfg["inbound_max_bytes"] = int(max_mb * 1024 * 1024)
            rb = _verify_media_message(client, fake, acct["account_id"], rb_cfg,
                                       enrich=False, cfg=cfg)
            if use_e2ee and rb.get("media_ref"):
                # 解密后落盘的字节必须与原文件逐字节一致（不只是 magic 对）
                try:
                    with open(rb.get("local") or "", "rb") as fh:
                        got = fh.read()
                    with open(path, "rb") as fh:
                        want = fh.read()
                    rb["roundtrip_identical"] = (got == want)
                    if got != want:
                        report["error"] = "E2EE 回读解密产物与原文件不一致"
                except Exception as exc:  # noqa: BLE001
                    rb["roundtrip_identical"] = "err: %s" % str(exc)[:120]
            send_item["readback"] = rb
            if rb.get("media_ref") and not (rb.get("magic") or {}).get("ok"):
                report["error"] = "回读落盘未过 magic：%s" % (rb.get("magic") or {}).get("reason")
            elif not rb.get("media_ref"):
                report["error"] = "已送达但回读未取到（%s）" % (rb.get("miss") or {}).get("reason")
        report["send"] = send_item
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
    if report.get("recent") is not None:
        print("  -- 最近消息 box=%s（%d 条）--" % (report.get("box"), len(report["recent"])))
        for it in report["recent"]:
            print("    %s ct=%s %-8s from=%s age_h=%s e2ee=%s SID=%s OID=%s POP=%s meta=%s%s"
                  % (it["id"], it["contentType"], it["kind"], it["from"], it["age_h"],
                     it["e2ee"], it.get("SID"), it.get("OID"), it.get("OBS_POP"),
                     ",".join(it["meta_keys"]),
                     (" path=" + it["obs_path"]) if it.get("obs_path") else ""))
    if report.get("recent_error"):
        print("  x 读最近消息失败：%s" % report["recent_error"])
    vm = report.get("verified_media")
    if vm is not None:
        print("  -- 逐条媒体验证 box=%s（%d 条，生产 download_line_media）--"
              % (report.get("box"), len(vm)))
        for it in vm:
            mag = it.get("magic") or {}
            miss = it.get("miss") or {}
            oi = it.get("object_info") or {}
            if it.get("media_ref"):
                verdict = "OK   " if mag.get("ok") else "BAD  "
                tail = "bytes=%s magic=%s %s" % (it.get("bytes"), mag.get("container") or "-",
                                                  mag.get("reason") or "")
            else:
                verdict = "MISS "
                tail = "reason=%s http=%s retryable=%s obs_status=%s%s" % (
                    miss.get("reason"), miss.get("http_status"), miss.get("retryable"),
                    miss.get("obs_status") or oi.get("status") or "-",
                    (" info=" + json.dumps(oi, ensure_ascii=False)[:120]) if oi else "")
            print("    %s %s %-8s age_h=%s e2ee=%s path=%s | %s"
                  % (verdict, it["id"], it["kind"], it.get("age_h"), it.get("e2ee"),
                     miss.get("path") or it.get("obs_path") or "-", tail))
            if it.get("vlm_desc") is not None or it.get("enrich_error"):
                print("        VLM/ASR: %s" % (it.get("vlm_desc") or it.get("enrich_error")))
                print("        AI 文本: %s" % (it.get("ai_text") or ""))
    st = report.get("sticker")
    if st:
        mag = st.get("magic") or {}
        print("  -- 贴纸 CDN 链 stk=%s pkg=%s -> %s bytes=%s magic=%s %s --"
              % (st.get("stkid"), st.get("pkgid"),
                 ("OK" if mag.get("ok") else "FAIL"), st.get("bytes"),
                 mag.get("container") or "-", mag.get("reason") or st.get("miss") or ""))
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
        print("  -- 真发 → %s to=%s kind=%s e2ee=%s bytes=%s elapsed=%ss %s --"
              % (ok, snd.get("to"), snd.get("kind") or "image", snd.get("e2ee"),
                 snd.get("bytes"), snd.get("elapsed_s"), json.dumps(r, ensure_ascii=False)))
        if snd.get("server_message"):
            print("     服务端消息: %s" % json.dumps(snd["server_message"], ensure_ascii=False))
        rb = snd.get("readback")
        if rb:
            mag = rb.get("magic") or {}
            if rb.get("media_ref"):
                print("     回读: %s bytes=%s magic=%s %s ref=%s path=%s identical=%s"
                      % ("OK" if mag.get("ok") else "BAD", rb.get("bytes"),
                         mag.get("container") or "-", mag.get("reason") or "", rb.get("media_ref"),
                         (rb.get("miss") or {}).get("path"), rb.get("roundtrip_identical", "-")))
            else:
                print("     回读: MISS %s object_info=%s"
                      % (json.dumps(rb.get("miss"), ensure_ascii=False),
                         json.dumps(rb.get("object_info"), ensure_ascii=False)))
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
    # #172 / #169 真机定层
    ap.add_argument("--box", default="", help="--list-recent/--verify-recent-media 的会话 mid（缺省=自己）")
    ap.add_argument("--list-recent", type=int, default=0, metavar="N",
                    help="列该会话最近 N 条消息（id/类型/龄/Letter Sealing/元数据）")
    ap.add_argument("--verify-recent-media", action="store_true",
                    help="该会话每条媒体消息走生产 download_line_media（状态/miss 细因/magic）")
    ap.add_argument("--kind", default="image", choices=["image", "video", "voice"],
                    help="--send-self 发什么（视频/语音现场 ffmpeg 合成）")
    ap.add_argument("--file", default="", help="--send-self 用这个文件代替合成产物（#169 上限实测）")
    ap.add_argument("--max-mb", type=float, default=0.0,
                    help="本次探针放开出站/回读体积护栏到 N MB（不动生产配置）")
    ap.add_argument("--no-readback", action="store_true", help="真发后不回读")
    ap.add_argument("--e2ee", action="store_true",
                    help="--send-self 按 LINE Chrome e2ee-next 流程发 Letter Sealing 媒体（验入站解密链）")
    ap.add_argument("--sticker-check", action="store_true", help="贴纸商店 CDN 链自检（无需账号）")
    ap.add_argument("--stkid", default="52002734", help="--sticker-check 的 STKID")
    ap.add_argument("--stkpkgid", default="11537", help="--sticker-check 的 STKPKGID")
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
