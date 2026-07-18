# -*- coding: utf-8 -*-
"""persona_purge_agent.py — avatarhub「人设全域清除」执行器（PERSONA_BUS P5 闭环·引擎侧消费者）。

职责（platform/identity/PERSONA_BUS.md §5 全域清除协议的 avatarhub 落地）：
  轮询集团注册表待清除指令 → 按 source_key（= profiles 表主键 = 角色名，与
  tools/persona_bus/export_avatarhub_personas.py 导出口径完全一致）解析该角色在本机
  的全部资产 → 软删除（先移入回收缓冲）→ 回执 ack。

机器同步通道（website 侧已上线，以实现为准；PERSONA_BUS.md §5.1 草案写的 /ack 子路径
与 {ok, system, completed_at} 体在上线版中简化为同 URL + {purge_id, detail}）：
  GET  <base>/api/sync/personas/purges?system=avatarhub
       头 Authorization: Bearer <EVENT_INGEST_KEY>
       → {ok, system, count, purges:[{purge_id, persona_id, source_system, source_key,
                                       requested_at, slots:{face,voice,prompt,knowledge}}]}
  POST <base>/api/sync/personas/purges   体 {purge_id, detail}
       → {ok, purge_id, persona_id, target_system, already, all_acked, persona_status}

资产删除映射（source_key=<名> → 物理位置；与 PERSONA_BUS.md §5.3/§6、
tools/persona_bus/README.md 槽位映射同源。指令里的 slots 布尔仅是注册表标注，
全域清除=该角色全部本机资产，不按槽位挑食）：
  face / prompt / voice_b64   avatar_profiles.db → profiles 表该角色行（face_b64 /
                              face_gallery_b64 / face_styled_b64 / thumbnail_b64 /
                              system_prompt / voice_b64 / fish_refs / emotion_refs /
                              opener…全在行内 data JSON）→ 整行 DELETE；
                              遗留 avatar_profiles.json 存在该键则同步去键
  voice（声音库参考音）        alltalk_tts/voices/<voice_name>.wav 与 <名>.wav ——
                              仅当无其他角色行引用才删（专属参考音；共享/内建声线
                              跳过并记 skipped，防止误伤同事角色）
  voice（克隆产物）            voice_clones/*.wav 与 voice_clones/_trash/*.wav 中，
                              文件字节 sha256 与该角色 voice_b64 / fish_refs /
                              emotion_refs 解码字节相同的文件（§5.3-2 含软删回收站）
  knowledge                   avatar_kb.db → kb_docs 中 meta.profile=<名> 的行；
                              声音包/<名>.txt 与 声音包/<名>.wav（源文本与源音频）
  缓存 / 衍生物               voice_previews/<名>_*.wav（试听缓存）、
                              share_covers/<名>.png（OG 封面）、
                              opener_cache/<sha1(名)[:16]>.json（开场白预合成）、
                              data/body_photo/<名>.jpg（全身照）、
                              data/look_history/<名>/（存照/出片历史目录）、
                              active_profile.txt（指向该角色时，防重启自动复活激活）、
                              profile_usage.json 中该角色键（使用趋势数据）

软删除（回收缓冲）设计 —— PERSONA_BUS.md 未约定执行器侧回收期，本实现采用更稳妥
姿势，建议补进契约 §5.3（见交付报告）：
  --commit 时文件不直接 unlink，而是移入
      <引擎根>/secrets/purged_trash/<日期>/<名>__purge<purge_id>/
  并保留原相对路径结构；被删 sqlite 行 / JSON 键先做 JSON 快照存入同目录 db_rows/，
  另写 manifest.json 记录本次清单。secrets/ 已被引擎与仓库两级 .gitignore 覆盖，
  绝不入库。真正物理删除由运维定期清空 trash（客户删除权的最终兑现点，建议回收期
  ≤30 天并写入交付文档）。§5.3-5 防复活义务由「本执行器绝不自动恢复、导出器对已删
  行自然不再输出」满足。

安全铁律：
  * 删除范围严格限定该 source_key 对应资产；每个待删路径 resolve 后二次确认仍在
    引擎根内（防越界/穿越删除），source_key 先过角色名白名单正则（与
    avatar_hub.py P11-A2 同款），不合法即拒绝执行且不 ack。
  * 缺省 dry-run：只打印将删除的文件/记录清单与字节数，绝不删除、绝不 ack、
    不写任何文件（连 state 都不写）。
  * source_system != avatarhub 的指令不按角色名乱删（键语义不同，可能误伤同名
    角色），告警跳过留人工处理（不 ack）。
  * 单条指令失败不影响其他条；sqlite 删除走事务（BEGIN IMMEDIATE）；文件全部移妥
    才动数据库行（保住下轮重试所需的 voice_name/哈希元数据）。
  * fail-silent 仅限拉取阶段的网络/服务端错误（打印告警退出 0）；--commit 后删除
    失败必须明确报错且不 ack（--once 退出码 1），已删项不回滚，下轮幂等重试
    （找不到的项记 detail.missing，幂等视同已删，§5.3-3）。
  * ack detail 只带计数与相对路径/资产 ID 引用，不含资产内容、不含指纹全文。
  * 回执成功但网络中断时，detail 暂存 state 文件（pending_acks），下轮开场补发——
    绝不因网络问题重复删除或谎报状态。

用法（Windows PowerShell / 计划任务均可）：
  python persona_purge_agent.py                      # dry-run + --once（演练，默认）
  python persona_purge_agent.py --commit             # 真删（软删入 trash）+ 回执
  python persona_purge_agent.py --commit --loop 600  # 常驻轮询，600 秒一轮（§5.3-1 周期 ≤1h）
  python persona_purge_agent.py --selftest           # 线程内 mock 服务端 + 临时资产目录自检

仅 Python 标准库（urllib / sqlite3 / hashlib / argparse；selftest 另用 http.server / threading）。
纪律：本文件为 engines/avatarhub 下新增执行器，不 import 引擎其他模块、不改其他文件；
运行时仅在 --commit 下写引擎目录（且只写 secrets/ 与被清除资产本身）。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # GBK 控制台防中文炸 print（与 tools/persona_bus 导出器同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEM = "avatarhub"
AGENT_UA = "persona_purge_agent/1"
ENC_MARKER = "enc:fernet:v1:"       # 与 avatar_hub.py::_ENC_MARKER 镜像（静置加密行前缀）
SLOT_KEYS = ("face", "voice", "prompt", "knowledge")
DETAIL_LIST_MAX = 100               # ack detail 内各清单的截断上限
_P = "[persona_purge_agent]"

# 角色名白名单（与 avatar_hub.py P11-A2 _PROFILE_NAME_RE 同款）：
# 汉字/字母/数字/下划线开头结尾，中间可含 - 与空格，1-64 字符。天然排除 . / \ : 等路径字符。
_KEY_RE = re.compile(r"^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5\-\s]{0,62}[\w\u4e00-\u9fa5]$"
                     r"|^[\w\u4e00-\u9fa5]$")


def info(msg: str) -> None:
    print(f"{_P} {msg}")


def warn(msg: str) -> None:
    print(f"{_P} 警告: {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{_P} 错误: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_bytes(n) -> str:
    x = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if x < 1024 or unit == "GiB":
            return f"{int(x)} B" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{int(x)} B"


def _key_ok(skey: str) -> bool:
    """source_key 白名单：不过关的键一律拒绝执行（防路径穿越/越界删除的第一道闸）。"""
    if not skey or len(skey) > 64:
        return False
    if ".." in skey or any(c in skey for c in "/\\:*?\"<>|.\x00"):
        return False
    return bool(_KEY_RE.match(skey))


def _sha1_16(name: str) -> str:
    """opener_cache 文件名规则（avatar_hub.py::_opener_cache_path 同款）。"""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> "str | None":
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def sha256_b64d(b64s: str) -> "str | None":
    """base64 内嵌资产 → 解码后字节的 sha256（与同字节落盘文件指纹一致，用于回算克隆产物）。"""
    s = (b64s or "").strip()
    if s.startswith("data:"):
        s = s.partition(",")[2]
    try:
        return hashlib.sha256(base64.b64decode(s)).hexdigest()
    except (binascii.Error, ValueError):
        return None


def _dir_stats(path: Path) -> "tuple[int, int]":
    total = count = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
                count += 1
            except OSError:
                pass
    return total, count


def _cap(lst, n: int = DETAIL_LIST_MAX) -> list:
    lst = list(lst)
    if len(lst) > n:
        return lst[:n] + [f"...（共 {len(lst)} 条，已截断）"]
    return lst


# ── HTTP（机器同步通道，Bearer EVENT_INGEST_KEY）──────────────────────


def _http_json(url: str, key: str, method: str = "GET", body=None, timeout: int = 25):
    """→ (status_code, obj)；网络层失败 → (None, {"error": ...})。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": AGENT_UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            payload = None
        return e.code, payload
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, {"error": str(e)}


def get_purges(base: str, key: str) -> "list | None":
    """拉取本引擎待清除指令；任何失败 → None（fail-silent，调用方告警后退出 0）。"""
    url = (base.rstrip("/") + "/api/sync/personas/purges?system="
           + urllib.parse.quote(SOURCE_SYSTEM))
    code, obj = _http_json(url, key, "GET")
    if code is None:
        warn(f"拉取指令失败（网络层）：{obj.get('error')}")
        return None
    if code != 200 or not isinstance(obj, dict) or not obj.get("ok"):
        brief = json.dumps(obj, ensure_ascii=False)[:200] if obj else ""
        warn(f"拉取指令失败（HTTP {code}）：{brief}")
        return None
    purges = obj.get("purges")
    return purges if isinstance(purges, list) else []


def post_ack(base: str, key: str, purge_id: int, detail: dict):
    url = base.rstrip("/") + "/api/sync/personas/purges"
    return _http_json(url, key, "POST", {"purge_id": purge_id, "detail": detail},
                      timeout=30)


# ── 只读数据源（资产清单解析阶段绝不写引擎）─────────────────────────


def _connect_read(db_path: Path) -> "sqlite3.Connection | None":
    try:
        return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        pass
    try:  # ro URI 打不开（老 sqlite/WAL 锁）时退回普通连接，仅执行 SELECT
        return sqlite3.connect(str(db_path), timeout=5)
    except sqlite3.Error as e:
        warn(f"SQLite 打开失败：{db_path}（{e}）")
        return None


def load_profiles_rows(db_path: Path) -> "dict | None":
    """profiles 表 → {name: data_str}；失败 → None。"""
    conn = _connect_read(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT name, data FROM profiles").fetchall()
        return {str(r[0]): (r[1] if isinstance(r[1], str) else "") for r in rows}
    except sqlite3.Error as e:
        warn(f"{db_path.name} 读取失败：{e}")
        return None
    finally:
        conn.close()


def load_kb_rows(kb_path: Path, skey: str) -> "tuple[list, int] | None":
    """avatar_kb.db 中 meta.profile=skey 的行 → (ids, 约字节数)；失败 → None。"""
    conn = _connect_read(kb_path)
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT id, text, meta FROM kb_docs").fetchall()
    except sqlite3.Error as e:
        warn(f"{kb_path.name} 读取失败：{e}")
        conn.close()
        return None
    conn.close()
    ids, nbytes = [], 0
    for did, text, meta in rows:
        try:
            prof = (json.loads(meta) or {}).get("profile") or ""
        except (TypeError, ValueError):
            prof = ""
        if prof == skey:
            ids.append(str(did))
            nbytes += len((text or "").encode("utf-8", "ignore"))
            nbytes += len((meta or "").encode("utf-8", "ignore"))
    return ids, nbytes


def _iter_voice_b64(profile: dict):
    """该角色行内全部内嵌参考音 b64（voice_b64 + fish_refs 多段 + emotion_refs 情绪段）。"""
    if not isinstance(profile, dict):
        return
    if profile.get("voice_b64"):
        yield profile["voice_b64"]
    for r in (profile.get("fish_refs") or []):
        if isinstance(r, dict) and r.get("voice_b64"):
            yield r["voice_b64"]
    for segs in (profile.get("emotion_refs") or {}).values():
        for s in (segs or []):
            if isinstance(s, dict) and s.get("voice_b64"):
                yield s["voice_b64"]


# ── 资产清单解析（inventory：定位该 source_key 的全部本机资产）──────


def build_inventory(root: Path, skey: str) -> dict:
    inv = {"actions": [], "skipped": [], "notes": [], "missing": [],
           "have_row": False, "encrypted": False}
    act = inv["actions"]

    def add_file(path: Path, kind: str = "file") -> None:
        try:
            if kind == "dir":
                nbytes, nfiles = _dir_stats(path)
                act.append({"kind": "dir", "path": path, "bytes": nbytes, "count": nfiles,
                            "ref": path.relative_to(root).as_posix() + "/"})
            else:
                act.append({"kind": "file", "path": path, "bytes": path.stat().st_size,
                            "ref": path.relative_to(root).as_posix()})
        except OSError as e:
            inv["notes"].append(f"读取 {path.name} 失败：{e}")

    # 1) 主存储行（face/prompt/voice_b64 全在行内）+ 其他行的 voice_name（共享声线核对）
    prof_db = root / "avatar_profiles.db"
    profile: dict = {}
    others_voice: set = set()
    shared_unknown = False       # 存在加密/不可解析的其他行 → 声线共享关系核对不完整
    if prof_db.is_file():
        rows = load_profiles_rows(prof_db)
        if rows is None:
            inv["notes"].append("avatar_profiles.db 读取失败：本轮无法删角色行，仅按命名规则清理文件")
        else:
            for n, data in rows.items():
                enc = isinstance(data, str) and data.startswith(ENC_MARKER)
                if n == skey:
                    inv["have_row"] = True
                    inv["encrypted"] = enc
                    if not enc:
                        try:
                            profile = json.loads(data) or {}
                        except (TypeError, ValueError):
                            profile = {}
                            inv["notes"].append("角色行 data 解析失败：按命名规则清理")
                    act.append({"kind": "db_profiles", "path": prof_db, "rows": 1,
                                "bytes": len((data or "").encode("utf-8", "ignore")),
                                "ref": f"avatar_profiles.db#profiles#{skey}"})
                elif enc:
                    shared_unknown = True
                else:
                    try:
                        vn = (json.loads(data) or {}).get("voice_name") or ""
                    except (TypeError, ValueError):
                        vn = ""
                        shared_unknown = True
                    if str(vn).strip():
                        others_voice.add(str(vn).strip())
            if not inv["have_row"]:
                inv["missing"].append(f"avatar_profiles.db#profiles#{skey}")
    else:
        inv["missing"].append("avatar_profiles.db")
    if inv["encrypted"]:
        inv["notes"].append("角色行为静置加密（AVATARHUB_ENCRYPT_PROFILES）：无法回算 "
                            "voice_name/克隆指纹，仅按角色名命名规则清理文件")

    # 2) 遗留 JSON 主存储（仅当仍存在该键）
    legacy = root / "avatar_profiles.json"
    if legacy.is_file():
        try:
            ldata = json.loads(legacy.read_text(encoding="utf-8-sig"))
            if isinstance(ldata, dict) and skey in ldata:
                act.append({"kind": "json_key", "path": legacy,
                            "bytes": len(json.dumps(ldata[skey], ensure_ascii=False)
                                         .encode("utf-8")),
                            "ref": f"avatar_profiles.json#{skey}"})
        except (OSError, ValueError) as e:
            inv["notes"].append(f"avatar_profiles.json 解析失败：{e}")

    # 3) 声音库 wav：voice_name 引用 + 角色同名文件。专属才删（共享声线跳过）。
    voices_dir = root / "alltalk_tts" / "voices"
    cands = {skey}
    vn = str(profile.get("voice_name") or "").strip()
    if vn:
        cands.add(vn)
    for c in sorted(cands):
        wav = voices_dir / f"{c}.wav"
        if not wav.is_file():
            continue
        if c in others_voice:
            inv["skipped"].append({"ref": f"alltalk_tts/voices/{c}.wav",
                                   "reason": "仍被其他角色引用（共享声线，非专属）"})
        elif shared_unknown and c != skey:
            # 有加密行无法核对是否共享 → 保守跳过；角色同名 wav 按命名约定视为专属仍删
            inv["skipped"].append({"ref": f"alltalk_tts/voices/{c}.wav",
                                   "reason": "存在加密/不可解析角色行，无法确认专属，保守跳过"})
        else:
            add_file(wav)

    # 4) 克隆产物：内容 sha256 与行内参考音一致的文件（§5.3-2 含 voice_clones/_trash/ 同源）
    digests = set()
    for b64s in _iter_voice_b64(profile):
        h = sha256_b64d(b64s)
        if h:
            digests.add(h)
    if digests:
        for sub in ("voice_clones", "voice_clones/_trash"):
            d = root / Path(sub)
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.wav")):
                if sha256_file(f) in digests:
                    add_file(f)

    # 5) 名字键控的缓存 / 衍生物 / 知识源文件
    pv = root / "voice_previews"
    if pv.is_dir():
        for f in sorted(pv.glob(f"{skey}_*.wav")):
            add_file(f)
    for p in (root / "share_covers" / f"{skey}.png",
              root / "opener_cache" / f"{_sha1_16(skey)}.json",
              root / "data" / "body_photo" / f"{skey}.jpg",
              root / "声音包" / f"{skey}.txt",
              root / "声音包" / f"{skey}.wav"):
        if p.is_file():
            add_file(p)
    look = root / "data" / "look_history" / skey
    if look.is_dir():
        add_file(look, kind="dir")
    ap_file = root / "active_profile.txt"
    try:
        if ap_file.is_file() and ap_file.read_text(encoding="utf-8-sig").strip() == skey:
            add_file(ap_file)   # 防重启自动重激活「复活」已清除角色
    except OSError:
        pass

    # 6) 知识库行（kb_docs.meta.profile 归属）
    kb = root / "avatar_kb.db"
    if kb.is_file():
        got = load_kb_rows(kb, skey)
        if got is None:
            inv["notes"].append("avatar_kb.db 读取失败：知识库行本轮无法清理")
        elif got[0]:
            act.append({"kind": "db_kb", "path": kb, "rows": len(got[0]), "bytes": got[1],
                        "ref": f"avatar_kb.db#kb_docs?profile={skey}"})

    # 7) 使用趋势数据（§5.3-2 衍生物：质量基线/趋势）
    up = root / "profile_usage.json"
    if up.is_file():
        try:
            u = json.loads(up.read_text(encoding="utf-8-sig"))
            if isinstance(u, dict) and skey in u:
                act.append({"kind": "json_key", "path": up,
                            "bytes": len(json.dumps(u[skey], ensure_ascii=False)
                                         .encode("utf-8")),
                            "ref": f"profile_usage.json#{skey}"})
        except (OSError, ValueError) as e:
            inv["notes"].append(f"profile_usage.json 解析失败：{e}")
    return inv


# ── dry-run 演练输出 ─────────────────────────────────────────────────


def print_plan(skey: str, purge_id, inv: dict) -> None:
    total = sum(a["bytes"] for a in inv["actions"])
    n_f = sum(1 for a in inv["actions"] if a["kind"] == "file")
    n_d = sum(1 for a in inv["actions"] if a["kind"] == "dir")
    n_rows = sum(a.get("rows", 0) for a in inv["actions"]
                 if a["kind"] in ("db_profiles", "db_kb"))
    n_jk = sum(1 for a in inv["actions"] if a["kind"] == "json_key")
    info(f"[dry-run] purge_id={purge_id} source_key={skey} —— 演练，不删除、不回执：")
    for a in inv["actions"]:
        if a["kind"] == "file":
            print(f"    删文件   {a['ref']}  ({fmt_bytes(a['bytes'])})")
        elif a["kind"] == "dir":
            print(f"    删目录   {a['ref']}  ({a['count']} 个文件, {fmt_bytes(a['bytes'])})")
        elif a["kind"] in ("db_profiles", "db_kb"):
            print(f"    删数据行 {a['ref']}  ({a['rows']} 行, ~{fmt_bytes(a['bytes'])})")
        else:
            print(f"    删记录键 {a['ref']}  (~{fmt_bytes(a['bytes'])})")
    for s in inv["skipped"]:
        print(f"    跳过     {s['ref']} —— {s['reason']}")
    for m in inv["missing"]:
        print(f"    缺失     {m}（幂等：视同已删，回执时记 missing）")
    for n in inv["notes"]:
        print(f"    注       {n}")
    if not inv["actions"]:
        print("    （本机未发现该角色任何资产）")
    info(f"[dry-run] 合计：文件 {n_f} / 目录 {n_d} / 数据行 {n_rows} / 记录键 {n_jk}，"
         f"约 {fmt_bytes(total)}。加 --commit 才会真正删除并回执。")


# ── 执行（--commit）：软删入 trash + 事务删行 + 快照 ──────────────────


def _move_to_trash(root: Path, path: Path, tdir: Path) -> str:
    """resolve 后二次确认在引擎根内 → 移入 trash（保留相对路径结构）。返回相对路径。"""
    rp = Path(path).resolve()
    rel = rp.relative_to(root)           # 越界 → ValueError，由调用方按错误处理
    if rel.parts and rel.parts[0] == "secrets":
        raise RuntimeError("拒绝清理 secrets/ 内路径")
    dest = tdir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():                    # 上轮部分失败重试留下的重名 → 加后缀防覆盖
        for i in range(1, 1000):
            cand = dest.with_name(dest.name + f".dup{i}")
            if not cand.exists():
                dest = cand
                break
    shutil.move(str(rp), str(dest))
    return rel.as_posix()


def _snap_write(snap_dir: Path, name: str, obj) -> None:
    """删行前先落 JSON 快照（软删除的行级对应物）；快照写不出则调用方放弃删除。"""
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _purge_profiles_row(db_path: Path, skey: str, snap_dir: Path):
    con = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT name, data, updated_at FROM profiles WHERE name = ?",
                          (skey,)).fetchone()
        if row is None:
            con.execute("ROLLBACK")
            return "missing", 0
        _snap_write(snap_dir, f"avatar_profiles.profiles.{skey}.json",
                    {"name": row[0], "data": row[1], "updated_at": row[2]})
        n = con.execute("DELETE FROM profiles WHERE name = ?", (skey,)).rowcount
        con.execute("COMMIT")
        return "deleted", n
    except (sqlite3.Error, OSError) as e:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return "error", str(e)
    finally:
        con.close()


def _purge_kb_rows(kb_path: Path, skey: str, snap_dir: Path):
    con = sqlite3.connect(str(kb_path), timeout=10, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT id, text, meta FROM kb_docs").fetchall()
        hit = []
        for did, text, meta in rows:
            try:
                prof = (json.loads(meta) or {}).get("profile") or ""
            except (TypeError, ValueError):
                prof = ""
            if prof == skey:
                hit.append({"id": str(did), "text": text, "meta": meta})
        if not hit:
            con.execute("ROLLBACK")
            return "missing", 0
        _snap_write(snap_dir, f"avatar_kb.kb_docs.{skey}.json", hit)
        con.executemany("DELETE FROM kb_docs WHERE id = ?", [(h["id"],) for h in hit])
        con.execute("COMMIT")
        return "deleted", len(hit)
    except (sqlite3.Error, OSError) as e:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return "error", str(e)
    finally:
        con.close()


def _purge_json_key(path: Path, skey: str, snap_dir: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return "error", f"读取失败：{e}"
    if not isinstance(data, dict) or skey not in data:
        return "missing", 0
    try:
        _snap_write(snap_dir, f"{path.name}.{skey}.json", {skey: data[skey]})
        data.pop(skey)
        tmp = path.with_name(path.name + ".purge_tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)          # 原子替换，引擎并发读也只会看到旧/新完整版
        return "deleted", 1
    except OSError as e:
        return "error", str(e)


def execute_purge(root: Path, skey: str, inv: dict, tdir: Path) -> dict:
    res = {"deleted": [], "missing": list(inv["missing"]), "errors": [],
           "skipped": [f"{s['ref']}（{s['reason']}）" for s in inv["skipped"]],
           "files": 0, "dirs": 0, "bytes": 0, "db_rows": 0, "json_keys": 0}
    root = root.resolve()
    file_acts = [a for a in inv["actions"] if a["kind"] in ("file", "dir")]
    rest_acts = [a for a in inv["actions"] if a["kind"] not in ("file", "dir")]

    for a in file_acts:
        try:
            rel = _move_to_trash(root, a["path"], tdir)
            res["deleted"].append(rel + ("/" if a["kind"] == "dir" else ""))
            res["bytes"] += a["bytes"]
            res["dirs" if a["kind"] == "dir" else "files"] += 1
        except FileNotFoundError:
            res["missing"].append(a["ref"])       # 清单后被人删了 → 幂等视同已删
        except (OSError, RuntimeError, ValueError) as e:
            res["errors"].append(f"{a['ref']} 移入 trash 失败：{e}")
    if res["errors"]:
        # 保住角色行（voice_name/内嵌哈希 = 下轮重试重建清单的依据），已移项不回滚
        res["errors"].append("文件阶段有失败 → 本轮保留数据库行/记录键供下轮重试")
        return res

    snap_dir = tdir / "db_rows"
    for a in rest_acts:
        if a["kind"] == "db_profiles":
            st, n = _purge_profiles_row(a["path"], skey, snap_dir)
        elif a["kind"] == "db_kb":
            st, n = _purge_kb_rows(a["path"], skey, snap_dir)
        else:
            st, n = _purge_json_key(a["path"], skey, snap_dir)
        if st == "deleted":
            res["deleted"].append(a["ref"])
            res["bytes"] += a["bytes"]
            if a["kind"] == "json_key":
                res["json_keys"] += 1
            else:
                res["db_rows"] += n
        elif st == "missing":
            res["missing"].append(a["ref"])
        else:
            res["errors"].append(f"{a['ref']} 删除失败：{n}")
    return res


def build_detail(res: dict, trash_rel, inv: dict) -> dict:
    """ack detail：计数摘要 + 相对路径/资产 ID 清单。不含资产内容、不含指纹全文。"""
    return {
        "agent": AGENT_UA,
        "completed_at": now_iso(),
        "soft_delete": True,
        "trash": trash_rel,
        "summary": {"files": res["files"], "dirs": res["dirs"], "bytes": res["bytes"],
                    "db_rows": res["db_rows"], "json_keys": res["json_keys"],
                    "missing": len(res["missing"]), "skipped": len(res["skipped"]),
                    "errors": len(res["errors"])},
        "deleted": _cap(res["deleted"]),
        "missing": _cap(res["missing"]),
        "skipped": _cap(res["skipped"]),
        "errors": [],                    # 只有零失败才会走到 ack，这里恒为空（§5.1 形状对齐）
        "notes": inv["notes"][:10],
        "encrypted_row": bool(inv.get("encrypted")),
    }


def _write_manifest(tdir: Path, purge_id, skey: str, detail: dict) -> None:
    if not tdir.exists():
        return
    try:
        (tdir / "manifest.json").write_text(
            json.dumps({"purge_id": purge_id, "source_key": skey, **detail},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        warn(f"trash manifest 写入失败（不影响清除结果）：{e}")


# ── 状态文件（仅 --commit 写；记录已回执与待补发回执）─────────────────


def _load_state(path: Path) -> dict:
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("acked", {})
            st.setdefault("pending_acks", {})
            return st
    except (OSError, ValueError):
        pass
    return {"version": 1, "system": SOURCE_SYSTEM, "acked": {}, "pending_acks": {}}


def _save_state(path: Path, st: dict) -> None:
    st["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── 单轮：补发回执 → 拉指令 → 逐条处理 ───────────────────────────────


def _record_ack(state: dict, state_path: Path, pid_s: str, resp: dict, detail: dict) -> None:
    state["acked"][pid_s] = {"acked_at": now_iso(),
                             "all_acked": bool(resp.get("all_acked")),
                             "persona_status": resp.get("persona_status"),
                             "detail": detail}
    state["pending_acks"].pop(pid_s, None)
    _save_state(state_path, state)


def run_round(args) -> dict:
    """一轮轮询。返回 {"network_ok","planned","acked","pending","failed"}。
    failed = 拒绝执行/删除失败/校验不过（均不 ack）；网络类问题不计 failed。"""
    root = Path(args.root).resolve()
    out = {"network_ok": True, "planned": 0, "acked": 0, "pending": 0, "failed": 0}
    state = (_load_state(args.state_path) if args.commit
             else {"acked": {}, "pending_acks": {}})

    if args.commit and state["pending_acks"]:
        for pid_s in list(state["pending_acks"].keys()):
            detail = state["pending_acks"][pid_s]
            code, resp = post_ack(args.base, args.key, int(pid_s), detail)
            if code is None:
                warn(f"补发回执 purge_id={pid_s} 网络失败，保留待补")
                break
            if code == 200 and isinstance(resp, dict) and resp.get("ok"):
                _record_ack(state, args.state_path, pid_s, resp, detail)
                info(f"补发回执成功 purge_id={pid_s}（all_acked={resp.get('all_acked')} "
                     f"persona_status={resp.get('persona_status')}）")
                out["acked"] += 1
            elif code == 404:
                warn(f"补发回执 purge_id={pid_s}：注册表不认识该指令（404），放弃补发")
                state["pending_acks"].pop(pid_s, None)
                _save_state(args.state_path, state)
            else:
                warn(f"补发回执 purge_id={pid_s} 被拒（HTTP {code}），保留待补")

    purges = get_purges(args.base, args.key)
    if purges is None:
        out["network_ok"] = False
        return out
    if not purges:
        info("无待清除指令。")
        return out
    info(f"收到 {len(purges)} 条待清除指令。")

    for d in purges:
        if not isinstance(d, dict):
            err("指令不是对象，跳过（不回执）")
            out["failed"] += 1
            continue
        pid = d.get("purge_id")
        skey = str(d.get("source_key") or "")
        ssys = str(d.get("source_system") or "")
        slots = d.get("slots") if isinstance(d.get("slots"), dict) else {}
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            err(f"purge_id 非法：{pid!r}，跳过（不回执）")
            out["failed"] += 1
            continue
        pid_s = str(pid)

        if args.commit and pid_s in state["pending_acks"]:
            warn(f"purge_id={pid} 上轮已删除完成、回执未达 —— 仅补发回执，不重复删除")
            code, resp = post_ack(args.base, args.key, pid, state["pending_acks"][pid_s])
            if code == 200 and isinstance(resp, dict) and resp.get("ok"):
                _record_ack(state, args.state_path, pid_s, resp, state["acked"]
                            .get(pid_s, {}).get("detail") or {})
                out["acked"] += 1
            else:
                out["pending"] += 1
            continue
        if args.commit and pid_s in state["acked"]:
            warn(f"purge_id={pid} 本机已完成但注册表仍在下发 —— 重发既有回执")
            detail = state["acked"][pid_s].get("detail") or {"agent": AGENT_UA,
                                                             "note": "re-ack"}
            code, resp = post_ack(args.base, args.key, pid, detail)
            if code == 200 and isinstance(resp, dict) and resp.get("ok"):
                _record_ack(state, args.state_path, pid_s, resp, detail)
                out["acked"] += 1
            else:
                out["pending"] += 1
            continue

        if ssys != SOURCE_SYSTEM:
            err(f"purge_id={pid} source_system={ssys!r} ≠ {SOURCE_SYSTEM}：source_key 语义"
                "不同（可能误伤同名角色），拒绝执行，留待人工处理（不回执）")
            out["failed"] += 1
            continue
        if not _key_ok(skey):
            err(f"purge_id={pid} source_key={skey!r} 未过角色名白名单（防路径穿越/越界"
                "删除），拒绝执行（不回执）")
            out["failed"] += 1
            continue

        lit = ",".join(k for k in SLOT_KEYS if slots.get(k)) or "-"
        info(f"—— purge_id={pid} source_key={skey} slots[{lit}]"
             "（注册表标注仅供参考；清除范围=该角色全部本机资产）——")
        inv = build_inventory(root, skey)

        if not args.commit:
            print_plan(skey, pid, inv)
            out["planned"] += 1
            continue

        tdir = (root / "secrets" / "purged_trash" / time.strftime("%Y-%m-%d")
                / f"{skey}__purge{pid}")
        res = execute_purge(root, skey, inv, tdir)
        if res["errors"]:
            for e in res["errors"]:
                err(f"purge_id={pid} {e}")
            err(f"purge_id={pid} 本轮删除未全部成功：不回执（已删项不回滚，下轮幂等重试）")
            out["failed"] += 1
            continue
        trash_rel = tdir.relative_to(root).as_posix() if tdir.exists() else None
        detail = build_detail(res, trash_rel, inv)
        _write_manifest(tdir, pid, skey, detail)
        info(f"purge_id={pid} 清除完成：文件 {res['files']} / 目录 {res['dirs']} / "
             f"{fmt_bytes(res['bytes'])}，库行 {res['db_rows']}，记录键 {res['json_keys']}，"
             f"missing {len(res['missing'])}，skipped {len(res['skipped'])}"
             + (f"；已软删入 {trash_rel}" if trash_rel else ""))
        code, resp = post_ack(args.base, args.key, pid, detail)
        if code == 200 and isinstance(resp, dict) and resp.get("ok"):
            _record_ack(state, args.state_path, pid_s, resp, detail)
            info(f"回执成功 purge_id={pid}：all_acked={resp.get('all_acked')} "
                 f"persona_status={resp.get('persona_status')}")
            out["acked"] += 1
        elif code == 404:
            warn(f"回执 purge_id={pid}：注册表不认识该指令（404），不再补发")
        else:
            warn(f"回执未达 purge_id={pid}（HTTP {code}）：删除已完成，detail 暂存 "
                 "state，下轮开场补发")
            state["pending_acks"][pid_s] = detail
            _save_state(args.state_path, state)
            out["pending"] += 1
    return out


# ── selftest：线程内 mock 服务端 + 临时资产目录 ──────────────────────


def _selftest() -> int:  # noqa: C901（自检脚本，平铺直叙优先于圈复杂度）
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    fails: list = []

    def ck(cond, label: str) -> None:
        print(("  [PASS] " if cond else "  [FAIL] ") + label)
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="purge_agent_selftest_"))
    root = tmp / "avatarhub"
    key = "selftest-key"
    voice_bytes = b"RIFF#fake-voice#" + b"\x01\x02" * 300
    other_bytes = b"RIFF#other-voice#" + b"\x03" * 120
    v_b64 = base64.b64encode(voice_bytes).decode()

    # —— 临时 avatarhub 资产目录（假 profiles.db + 假 wav + 假 kb.db + 全套缓存）——
    (root / "alltalk_tts" / "voices").mkdir(parents=True)
    con = sqlite3.connect(str(root / "avatar_profiles.db"))
    con.execute("CREATE TABLE profiles (name TEXT PRIMARY KEY, data JSON NOT NULL, "
                "updated_at REAL NOT NULL)")
    con.execute("INSERT INTO profiles VALUES (?,?,?)", ("测试角色", json.dumps({
        "name": "测试角色", "description": "自检用",
        "face_b64": base64.b64encode(b"fake-face-jpeg").decode(),
        "voice_name": "测试角色", "voice_b64": v_b64,
        "system_prompt": "你是自检角色", "created_at": time.time(),
        "fish_refs": [{"voice_b64": v_b64, "text": "参考句"}],
        "emotion_refs": {"happy": [{"voice_b64": v_b64, "text": "开心"}]},
    }, ensure_ascii=False), time.time()))
    con.execute("INSERT INTO profiles VALUES (?,?,?)", ("旁观者", json.dumps({
        "name": "旁观者", "voice_name": "共享声线", "system_prompt": "旁观",
    }, ensure_ascii=False), time.time()))
    con.commit()
    con.close()
    kbc = sqlite3.connect(str(root / "avatar_kb.db"))
    kbc.execute("CREATE TABLE kb_docs (id TEXT PRIMARY KEY, text TEXT, meta TEXT, emb TEXT)")
    kbc.executemany("INSERT INTO kb_docs VALUES (?,?,?,?)", [
        ("d1", "知识A", json.dumps({"profile": "测试角色"}, ensure_ascii=False), ""),
        ("d2", "知识B", json.dumps({"profile": "测试角色"}, ensure_ascii=False), ""),
        ("d3", "旁观知识", json.dumps({"profile": "旁观者"}, ensure_ascii=False), ""),
    ])
    kbc.commit()
    kbc.close()
    (root / "alltalk_tts" / "voices" / "测试角色.wav").write_bytes(b"RIFF-lib-voice-A")
    (root / "alltalk_tts" / "voices" / "共享声线.wav").write_bytes(b"RIFF-lib-voice-B")
    (root / "voice_clones" / "_trash").mkdir(parents=True)
    (root / "voice_clones" / "克隆A.wav").write_bytes(voice_bytes)
    (root / "voice_clones" / "无关.wav").write_bytes(other_bytes)
    (root / "voice_clones" / "_trash" / "旧克隆.wav").write_bytes(voice_bytes)
    (root / "voice_previews").mkdir()
    (root / "voice_previews" / "测试角色_zh-cn.wav").write_bytes(b"RIFF-preview")
    (root / "share_covers").mkdir()
    (root / "share_covers" / "测试角色.png").write_bytes(b"PNG-fake-cover")
    (root / "opener_cache").mkdir()
    opener = root / "opener_cache" / f"{_sha1_16('测试角色')}.json"
    opener.write_text("{}", encoding="utf-8")
    (root / "data" / "body_photo").mkdir(parents=True)
    (root / "data" / "body_photo" / "测试角色.jpg").write_bytes(b"JPG-body")
    (root / "data" / "look_history" / "测试角色").mkdir(parents=True)
    (root / "data" / "look_history" / "测试角色" / "photo1.jpg").write_bytes(b"JPG-look")
    (root / "声音包").mkdir()
    (root / "声音包" / "测试角色.txt").write_text("知识源文本", encoding="utf-8")
    (root / "profile_usage.json").write_text(json.dumps(
        {"测试角色": {"activate": 5}, "旁观者": {"activate": 2}}, ensure_ascii=False),
        encoding="utf-8")
    (root / "active_profile.txt").write_text("测试角色", encoding="utf-8")

    # —— 线程内 mock 同步通道（GET 下发 / POST 回执，鉴权同真实协议）——
    class _H(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            return self.headers.get("Authorization", "") == f"Bearer {self.server.auth_key}"

        def do_GET(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/sync/personas/purges"):
                self._send(404, {"error": "not_found"})
                return
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            pend = [x for x in self.server.directives
                    if x["purge_id"] not in self.server.acked_ids]
            self._send(200, {"ok": True, "system": SOURCE_SYSTEM,
                             "count": len(pend), "purges": pend})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/sync/personas/purges"):
                self._send(404, {"error": "not_found"})
                return
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                self._send(400, {"error": "invalid_json"})
                return
            pid = body.get("purge_id")
            already = pid in self.server.acked_ids
            if not already:
                self.server.acks.append(body)
                self.server.acked_ids.add(pid)
            self._send(200, {"ok": True, "purge_id": pid, "persona_id": "prs_selftest",
                             "target_system": SOURCE_SYSTEM, "already": already,
                             "all_acked": True, "persona_status": "purged"})

        def log_message(self, *a) -> None:  # 静音访问日志
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    srv.auth_key = key
    srv.directives = [{"purge_id": 101, "persona_id": "prs_selftest",
                       "source_system": SOURCE_SYSTEM, "source_key": "测试角色",
                       "requested_at": now_iso(),
                       "slots": {k: True for k in SLOT_KEYS}}]
    srv.acks = []
    srv.acked_ids = set()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    args = argparse.Namespace(
        base=f"http://127.0.0.1:{srv.server_address[1]}", key=key, root=root,
        commit=False, state_path=root / "secrets" / "purge_agent_state.json")

    def _profiles_names() -> list:
        c = sqlite3.connect(str(root / "avatar_profiles.db"))
        try:
            return sorted(r[0] for r in c.execute("SELECT name FROM profiles"))
        finally:
            c.close()

    def _kb_ids() -> list:
        c = sqlite3.connect(str(root / "avatar_kb.db"))
        try:
            return sorted(r[0] for r in c.execute("SELECT id FROM kb_docs"))
        finally:
            c.close()

    target_paths = [
        root / "alltalk_tts" / "voices" / "测试角色.wav",
        root / "voice_clones" / "克隆A.wav",
        root / "voice_clones" / "_trash" / "旧克隆.wav",
        root / "voice_previews" / "测试角色_zh-cn.wav",
        root / "share_covers" / "测试角色.png",
        opener,
        root / "data" / "body_photo" / "测试角色.jpg",
        root / "data" / "look_history" / "测试角色",
        root / "声音包" / "测试角色.txt",
        root / "active_profile.txt",
    ]
    kept_paths = [
        root / "alltalk_tts" / "voices" / "共享声线.wav",
        root / "voice_clones" / "无关.wav",
    ]

    rc = 1
    try:
        info("—— selftest 1/4：dry-run 演练（必须不删、不 ack、不落盘）——")
        r = run_round(args)
        ck(r["planned"] == 1 and r["failed"] == 0, "dry-run 处理了 1 条指令且无失败")
        ck(all(p.exists() for p in target_paths + kept_paths), "dry-run 后所有资产原样健在")
        ck(_profiles_names() == ["旁观者", "测试角色"], "dry-run 未动 profiles 表")
        ck(_kb_ids() == ["d1", "d2", "d3"], "dry-run 未动 kb_docs 表")
        ck(not srv.acks, "dry-run 未发出任何 ack")
        ck(not (root / "secrets").exists(), "dry-run 未创建 state/trash")

        info("—— selftest 2/4：--commit（资产进 trash + ack + all_acked 推进）——")
        args.commit = True
        r = run_round(args)
        ck(r["acked"] == 1 and r["failed"] == 0, "commit 回执 1 条且无失败")
        ck(not any(p.exists() for p in target_paths), "该角色资产已全部离开原位")
        ck(all(p.exists() for p in kept_paths), "共享声线与无关克隆不受影响")
        ck(_profiles_names() == ["旁观者"], "profiles 仅剩旁观者（角色行已删）")
        ck(_kb_ids() == ["d3"], "kb_docs 仅剩旁观者文档")
        usage = json.loads((root / "profile_usage.json").read_text(encoding="utf-8"))
        ck(sorted(usage.keys()) == ["旁观者"], "profile_usage.json 已去除该角色键")
        trash = root / "secrets" / "purged_trash"
        ck(any(trash.rglob("测试角色_zh-cn.wav")), "trash 内可找回被移文件（软删除）")
        ck(any(trash.rglob("avatar_profiles.profiles.测试角色.json")), "trash 内有角色行快照")
        ck(any(trash.rglob("manifest.json")), "trash 内有 manifest 清单")
        ck(len(srv.acks) == 1 and srv.acks[0]["purge_id"] == 101, "注册表收到 1 条 ack")
        det = srv.acks[0].get("detail") or {}
        summ = det.get("summary") or {}
        ck(summ.get("db_rows") == 3, "detail 汇总：库行 3（角色行 1 + 知识 2）")
        ck(summ.get("files", 0) >= 8 and summ.get("dirs") == 1 and summ.get("bytes", 0) > 0,
           "detail 汇总：文件/目录/字节数合理")
        blob = json.dumps(det, ensure_ascii=False)
        ck(v_b64[:24] not in blob, "detail 不含资产内容（base64 片段不外泄）")
        ck(not re.search(r"[0-9a-f]{64}", blob), "detail 不含指纹全文（无 64 位 hex）")
        st = json.loads((root / "secrets" / "purge_agent_state.json")
                        .read_text(encoding="utf-8"))
        ck(st["acked"]["101"]["all_acked"] is True, "state 记录 all_acked=True（注册表推进）")
        ck(st["acked"]["101"]["persona_status"] == "purged", "state 记录 persona_status=purged")

        info("—— selftest 3/4：幂等（指令关闭后再轮询无新增动作）——")
        r = run_round(args)
        ck(r["acked"] == 0 and r["failed"] == 0, "无待办时静默通过")
        ck(len(srv.acks) == 1, "无重复 ack")

        info("—— selftest 4/4：危险指令拦截（越界 key / 异系统 key）——")
        srv.directives = [
            {"purge_id": 102, "persona_id": "prs_evil", "source_system": SOURCE_SYSTEM,
             "source_key": "..\\..\\越界", "requested_at": now_iso(), "slots": {}},
            {"purge_id": 103, "persona_id": "prs_other", "source_system": "chengjie",
             "source_key": "旁观者", "requested_at": now_iso(), "slots": {}},
        ]
        r = run_round(args)
        ck(r["failed"] == 2 and r["acked"] == 0, "两条危险指令均拒绝执行且不回执")
        ck(len(srv.acks) == 1, "危险指令未产生 ack")
        ck(_profiles_names() == ["旁观者"], "异系统指令未误删同名本地角色")

        rc = 1 if fails else 0
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        err(f"selftest 失败 {len(fails)} 项：{'；'.join(fails)}")
    else:
        info("selftest 全部通过（dry-run 不删不 ack；commit 资产进 trash + ack + all_acked 推进）。")
    return rc


# ── 入口 ─────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="avatarhub 人设全域清除执行器（PERSONA_BUS §5；缺省 dry-run 演练）")
    ap.add_argument("--base", default=(os.environ.get("PERSONA_SYNC_BASE", "").strip()
                                       or "https://bd2026.cc"),
                    help="注册表基址（默认 env PERSONA_SYNC_BASE 或 https://bd2026.cc）")
    ap.add_argument("--key", default=os.environ.get("EVENT_INGEST_KEY", "").strip(),
                    help="机器密钥（默认 env EVENT_INGEST_KEY，与 /api/collect 同一把）")
    ap.add_argument("--input", default="",
                    help="引擎根目录（默认自动定位本脚本所在的 engines/avatarhub）")
    ap.add_argument("--commit", action="store_true",
                    help="真正执行删除并回执（缺省为 dry-run 演练：只打印、不删、不 ack）")
    ap.add_argument("--once", action="store_true", help="只跑一轮（缺省行为）")
    ap.add_argument("--loop", type=int, default=0, metavar="秒",
                    help="常驻轮询，每 N 秒一轮（PERSONA_BUS §5.3-1 建议 300–900）")
    ap.add_argument("--state-file", default="",
                    help="状态文件（默认 <input>/secrets/purge_agent_state.json）")
    ap.add_argument("--selftest", action="store_true",
                    help="线程内 mock 服务端 + 临时资产目录自检（不碰真实数据与网络）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    root = Path(args.input).resolve() if args.input else Path(__file__).resolve().parent
    markers = ("avatar_hub.py", "avatar_profiles.db", "avatar_profiles.json")
    if not root.is_dir() or not any((root / m).exists() for m in markers):
        err(f"--input 看起来不是 avatarhub 引擎根（缺 avatar_hub.py / avatar_profiles.db）："
            f"{root} —— 拒绝在陌生目录执行清除")
        return 2
    if args.once and args.loop:
        err("--once 与 --loop 互斥")
        return 2
    if args.loop < 0:
        err("--loop 间隔必须为正整数秒")
        return 2
    if not args.key:
        err("缺少机器密钥：--key 或环境变量 EVENT_INGEST_KEY（与 /api/collect 同一把）")
        return 2

    ns = argparse.Namespace(
        base=args.base.rstrip("/"), key=args.key, root=root, commit=bool(args.commit),
        state_path=(Path(args.state_file).resolve() if args.state_file
                    else root / "secrets" / "purge_agent_state.json"))
    info(f"模式：{'提交（--commit：软删入 trash + 回执）' if ns.commit else '演练（dry-run：只打印，不删不回执）'}"
         f"；注册表：{ns.base}；引擎根：{root}")

    if args.loop:
        info(f"常驻轮询，每 {args.loop} 秒一轮（Ctrl+C 退出）。")
        try:
            while True:
                r = run_round(ns)
                if r["failed"]:
                    err(f"本轮 {r['failed']} 条指令执行失败（未回执），下轮重试。")
                time.sleep(args.loop)
        except KeyboardInterrupt:
            info("收到中断，退出。")
            return 0

    r = run_round(ns)
    if not r["network_ok"]:
        warn("本轮未能拉取指令（网络/服务端问题）：fail-silent，退出码 0。")
        return 0
    if ns.commit and r["failed"]:
        err(f"{r['failed']} 条指令执行失败：未回执，退出码 1（已删项不回滚，下轮幂等重试）。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
