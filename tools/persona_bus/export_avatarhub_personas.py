# -*- coding: utf-8 -*-
"""export_avatarhub_personas.py — avatarhub 人设/角色库 → 人设总线归一化 JSON（P5，只读）。

数据源（全部只读，缺失即空导出 + stderr 警告，退出码 0）：
  <engine>/avatar_profiles.db      SQLite 主存储：profiles(name PK, data JSON, updated_at REAL)。
                                   data 可能带 enc:fernet:v1: 前缀（静置加密）——本脚本无密钥
                                   也绝不解密，该行按「存在但槽位未知」导出（tags 含 encrypted）。
  <engine>/avatar_profiles.json    遗留 JSON 存储（仅当 .db 不存在时作为回退读取）。
  <engine>/alltalk_tts/voices/     声音库 .wav（voice_name 槽位资产，指纹=文件 sha256 流式）。
  <engine>/avatar_kb.db            对话知识库：kb_docs(id, text, meta, emb)，meta.profile 归属。
  <engine>/声音包/<角色名>.txt      角色知识库源文件（引擎 _PROFILE_KB_FILES 同款路径约定）。
  <engine>/active_profile.txt      最近激活角色（tags: active）。
  <engine>/profile_usage.json      使用统计（raw.usage）。
  <engine>/data/body_photo/<名>.jpg 全身照存档（raw.has_body_photo）。

输出格式见 platform/identity/PERSONA_BUS.md §3（version=1，四槽位 face/voice/prompt/knowledge）。
铁律：资产本体（脸模/声纹/权重）与任何生物特征数据绝不进导出文件；fingerprint 只能是
      对资产字节的 sha256 摘要；raw 只放白名单标量元数据，字符串截断，绝不含文件内容。
纪律：绝对只读——对 engines/ 只 open(..., "rb"/"r") 与 SQLite mode=ro；--out 禁止落在被读目录内。
仅 Python 标准库。用法见 tools/persona_bus/README.md。
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
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # GBK 控制台防中文炸 print（与 tools/license_ledger 侧同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEM = "avatarhub"
SLOT_KEYS = ("face", "voice", "prompt", "knowledge")
ENC_MARKER = "enc:fernet:v1:"          # 与 avatar_hub.py::_ENC_MARKER 镜像
RAW_STR_MAX = 200                      # raw 内字符串截断上限（防 description 被塞长串）
_LEAK_RE = re.compile(r"[A-Za-z0-9+/=]{2000,}")   # 导出前自查：base64 长串即事故

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE_DIR = _REPO_ROOT / "engines" / "avatarhub"


def warn(msg: str) -> None:
    print(f"[export_avatarhub_personas] 警告: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(ts) -> "str | None":
    """unix 秒（int/float/数字串）→ ISO8601(UTC)；0/空/解析失败 → None。"""
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


# ── 指纹（sha256）─────────────────────────────────────────────────────


def sha256_file(path: Path) -> "str | None":
    """大文件流式 sha256；读失败 → None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        warn(f"读取资产失败（fingerprint 置 null）：{path}（{e}）")
        return None
    return h.hexdigest()


def sha256_b64(b64s: str) -> "str | None":
    """对 base64 内嵌资产的**解码后字节**做 sha256（与同字节落盘文件的指纹一致）。"""
    s = b64s.strip()
    if s.startswith("data:"):          # 防御：data URI 前缀剥掉再解
        s = s.partition(",")[2]
    try:
        return hashlib.sha256(base64.b64decode(s)).hexdigest()
    except (binascii.Error, ValueError):
        # 解不开就退化为对 b64 文本字节求摘要——仍是不可逆摘要，绝不落原文
        return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 只读 SQLite ───────────────────────────────────────────────────────


def _connect_ro(db_path: Path) -> "sqlite3.Connection | None":
    """mode=ro URI 打开；WAL 副文件受锁/缺失时回退：拷到临时目录再读（临时目录在
    系统 tmp，不碰 engines/）。任何失败 → None。"""
    try:
        return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        pass
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="persona_bus_ro_"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                shutil.copy2(src, tmpdir / src.name)
        return sqlite3.connect(f"file:{(tmpdir / db_path.name).as_posix()}?mode=ro",
                               uri=True)
    except (OSError, sqlite3.Error) as e:
        warn(f"SQLite 只读打开失败：{db_path}（{e}）")
        return None


def load_profiles_db(db_path: Path) -> "dict | None":
    """profiles 表 → {name: {"data": str, "updated_at": float}}；失败 → None。"""
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT name, data, updated_at FROM profiles ORDER BY name").fetchall()
        return {r[0]: {"data": r[1], "updated_at": r[2]} for r in rows}
    except sqlite3.Error as e:
        warn(f"{db_path} 读取失败：{e}")
        return None
    finally:
        conn.close()


def load_kb_counts(kb_path: Path) -> "dict | None":
    """avatar_kb.db 的 kb_docs → {profile: {"count": n, "digest": sha256}}。

    digest = 按 id 排序的 (id + "\\n" + text) 逐条拼接后的 sha256——稳定、不可逆，
    knowledge 槽位没有单一资产文件时以它作 fingerprint。
    """
    conn = _connect_ro(kb_path)
    if conn is None:
        return None
    per: dict = {}
    try:
        rows = conn.execute("SELECT id, text, meta FROM kb_docs").fetchall()
    except sqlite3.Error as e:
        warn(f"{kb_path} 读取失败：{e}")
        conn.close()
        return None
    conn.close()
    buckets: dict = {}
    for did, text, meta in rows:
        try:
            prof = (json.loads(meta) or {}).get("profile") or ""
        except (TypeError, ValueError):
            prof = ""
        if not prof:
            continue
        buckets.setdefault(prof, []).append((str(did), text or ""))
    for prof, docs in buckets.items():
        h = hashlib.sha256()
        for did, text in sorted(docs):
            h.update(did.encode("utf-8"))
            h.update(b"\n")
            h.update(text.encode("utf-8"))
            h.update(b"\n")
        per[prof] = {"count": len(docs), "digest": h.hexdigest()}
    return per


# ── raw 白名单（防泄漏的唯一姿势：只挑明确安全的字段，不做黑名单）───────


def _scalar(v):
    """raw 只收标量；字符串截断到 RAW_STR_MAX。不合格 → None（调用方丢弃）。"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v[:RAW_STR_MAX]
    return None


def _scalar_dict(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        sv = _scalar(v)
        if sv is not None:
            out[str(k)[:64]] = sv
    return out


def build_raw(p: dict, *, storage: str, updated_at=None, usage=None,
              kb_count: int = 0, has_body_photo: bool = False) -> dict:
    raw: dict = {"storage": storage}
    if updated_at is not None:
        raw["updated_at"] = to_iso(updated_at)
    # 字符串/标量字段白名单（全部是设置项/引用名，非内容非生物特征）
    for field in ("description", "voice_name", "hair_style", "makeup_style",
                  "tts_engine", "vc_engine", "lipsync_engine", "faceswap_engine",
                  "rvc_model", "dfm_model", "idle_video", "body_video",
                  "voicepack_spk"):
        v = _scalar(p.get(field))
        if v not in (None, ""):
            raw[field] = v
    for field in ("rvc_strict_mode", "use_styled_face", "allow_reference_preview"):
        if field in p:
            raw[field] = bool(p.get(field))
    # 计数（多资产槽位/话术只出数量，绝不出内容）
    counts = {
        "face_gallery_count": len(p.get("face_gallery_b64") or []),
        "fish_refs_count":    len(p.get("fish_refs") or []),
        "opener_count":       len(p.get("opener_phrases") or []),
        "filler_count":       len(p.get("filler_phrases") or []),
        "probe_count":        len(p.get("probe_sentences") or []),
        "kb_docs_count":      kb_count,
    }
    raw.update({k: v for k, v in counts.items() if v})
    emo = {k: len(v) for k, v in (p.get("emotion_refs") or {}).items()
           if isinstance(v, list) and v}
    if emo:
        raw["emotion_refs_count"] = emo
    for field in ("quality_axes", "voice_quality", "rvc_settings"):
        d = _scalar_dict(p.get(field) or {})
        if d:
            raw[field] = d
    if p.get("thumbnail_b64"):
        raw["has_thumbnail"] = True
    if p.get("face_styled_b64"):
        raw["has_styled_face"] = True
    if has_body_photo:
        raw["has_body_photo"] = True
    if usage and isinstance(usage, dict):
        raw["usage"] = _scalar_dict(usage)
    return raw


# ── 槽位归一化 ────────────────────────────────────────────────────────


def slot(present: bool, fingerprint=None, ref=None, version=None) -> dict:
    if not present:
        return {"present": False, "fingerprint": None, "ref": None, "version": None}
    return {"present": True, "fingerprint": fingerprint, "ref": ref,
            "version": version}


def face_slot(name: str, p: dict) -> dict:
    face_b64 = p.get("face_b64") or ""
    if not face_b64:
        return slot(False)
    return slot(True, fingerprint=sha256_b64(face_b64),
                ref=f"avatar_profiles.db#{name}#face_b64")


def voice_slot(name: str, p: dict, engine_dir: Path) -> dict:
    """voice_name（声音库文件）优先，其次行内 voice_b64（与 avatar_hub 消费序一致）。"""
    voice_name = (p.get("voice_name") or "").strip()
    if voice_name:
        wav = engine_dir / "alltalk_tts" / "voices" / f"{voice_name}.wav"
        if wav.is_file():
            return slot(True, fingerprint=sha256_file(wav),
                        ref=f"alltalk_tts/voices/{voice_name}.wav")
        # 引用了但文件丢失：若行内还有 voice_b64 用它兜底，否则按缺席（present 按实际存在）
    voice_b64 = p.get("voice_b64") or ""
    if voice_b64:
        return slot(True, fingerprint=sha256_b64(voice_b64),
                    ref=f"avatar_profiles.db#{name}#voice_b64")
    return slot(False)


def prompt_slot(name: str, p: dict) -> dict:
    sp = (p.get("system_prompt") or "").strip()
    if not sp:
        return slot(False)
    return slot(True, fingerprint=sha256_text(sp),
                ref=f"avatar_profiles.db#{name}#system_prompt")


def knowledge_slot(name: str, kb_info, engine_dir: Path) -> dict:
    """kb_docs（实际入库文档）优先；其次 声音包/<名>.txt 源文件。"""
    if kb_info and kb_info.get("count"):
        return slot(True, fingerprint=kb_info["digest"],
                    ref=f"avatar_kb.db#kb_docs?profile={name}")
    src = engine_dir / "声音包" / f"{name}.txt"
    if src.is_file():
        return slot(True, fingerprint=sha256_file(src),
                    ref=f"声音包/{name}.txt")
    return slot(False)


# ── 汇总 ─────────────────────────────────────────────────────────────


def make_persona(*, source_key: str, display_name: str, slots: dict,
                 tags=None, created_at=None, raw=None) -> dict:
    return {
        "source_key": source_key,
        "display_name": display_name,
        "customer_name": None,   # avatarhub v1 无客户绑定字段（PERSONA_BUS.md §3.2）
        "slots": slots,
        "tags": tags or [],
        "created_at": created_at,
        "raw": raw or {},
    }


def collect(engine_dir: Path) -> list:
    if not engine_dir.is_dir():
        warn(f"数据源目录不存在：{engine_dir}（输出空 personas）")
        return []

    # 主存储：avatar_profiles.db 优先，遗留 avatar_profiles.json 回退
    db_path = engine_dir / "avatar_profiles.db"
    json_path = engine_dir / "avatar_profiles.json"
    rows: "dict | None" = None
    storage = ""
    if db_path.is_file():
        rows = load_profiles_db(db_path)
        storage = "profiles_db"
    if rows is None and json_path.is_file():
        try:
            legacy = json.loads(json_path.read_text(encoding="utf-8-sig"))
            if isinstance(legacy, dict):
                rows = {n: {"data": json.dumps(d, ensure_ascii=False),
                            "updated_at": None} for n, d in legacy.items()}
                storage = "profiles_json"
        except (OSError, ValueError) as e:
            warn(f"{json_path} 解析失败：{e}")
    if rows is None:
        warn(f"{engine_dir} 下未找到人设主存储"
             "（avatar_profiles.db / avatar_profiles.json），输出空 personas")
        return []

    # 旁路元数据（缺失均可降级）
    active = ""
    try:
        active = (engine_dir / "active_profile.txt").read_text(
            encoding="utf-8").strip()
    except OSError:
        pass
    usage_map: dict = {}
    try:
        u = json.loads((engine_dir / "profile_usage.json").read_text(
            encoding="utf-8-sig"))
        if isinstance(u, dict):
            usage_map = u
    except (OSError, ValueError):
        pass
    kb_map: dict = {}
    kb_path = engine_dir / "avatar_kb.db"
    if kb_path.is_file():
        kb_map = load_kb_counts(kb_path) or {}

    personas = []
    for name in sorted(rows):
        row = rows[name]
        data = row.get("data")
        if isinstance(data, str) and data.startswith(ENC_MARKER):
            # 静置加密行：无密钥不解密（也绝不该解密）。按「存在但槽位未知」导出。
            personas.append(make_persona(
                source_key=name, display_name=name,
                slots={k: slot(False) for k in SLOT_KEYS},
                tags=["encrypted"] + (["active"] if name == active else []),
                created_at=None,
                raw={"storage": storage, "encrypted": True,
                     "updated_at": to_iso(row.get("updated_at"))},
            ))
            continue
        try:
            p = json.loads(data) if isinstance(data, str) else dict(data or {})
        except (TypeError, ValueError) as e:
            warn(f"角色 {name} 的 data 解析失败，已按空档导出：{e}")
            p = {}
        kb_info = kb_map.get(name)
        slots = {
            "face":      face_slot(name, p),
            "voice":     voice_slot(name, p, engine_dir),
            "prompt":    prompt_slot(name, p),
            "knowledge": knowledge_slot(name, kb_info, engine_dir),
        }
        tags = []
        if name == active:
            tags.append("active")
        if p.get("voicepack_spk"):
            tags.append("voicepack")
        personas.append(make_persona(
            source_key=name, display_name=name, slots=slots, tags=tags,
            created_at=to_iso(p.get("created_at")),
            raw=build_raw(
                p, storage=storage, updated_at=row.get("updated_at"),
                usage=usage_map.get(name),
                kb_count=(kb_info or {}).get("count", 0),
                has_body_photo=(engine_dir / "data" / "body_photo"
                                / f"{name}.jpg").is_file()),
        ))
    return personas


# ── 演示数据 ─────────────────────────────────────────────────────────


def demo_personas() -> list:
    """3 条演示数据（管道联调）：四槽齐全 / 部分槽位 / 静置加密行。"""
    now = int(time.time())
    fp = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()  # noqa: E731
    return [
        make_persona(
            source_key="演示-小雅", display_name="演示-小雅",
            slots={
                "face": slot(True, fingerprint=fp("demo-face-xiaoya"),
                             ref="avatar_profiles.db#演示-小雅#face_b64"),
                "voice": slot(True, fingerprint=fp("demo-voice-xiaoya"),
                              ref="alltalk_tts/voices/演示-小雅.wav"),
                "prompt": slot(True, fingerprint=fp("demo-prompt-xiaoya"),
                               ref="avatar_profiles.db#演示-小雅#system_prompt"),
                "knowledge": slot(True, fingerprint=fp("demo-kb-xiaoya"),
                                  ref="avatar_kb.db#kb_docs?profile=演示-小雅"),
            },
            tags=["active"], created_at=to_iso(now - 30 * 86400),
            raw={"storage": "demo", "demo": True, "description": "演示：四槽齐全",
                 "kb_docs_count": 12, "face_gallery_count": 3,
                 "quality_axes": {"cosine": 0.83}},
        ),
        make_persona(
            source_key="演示-云飞", display_name="演示-云飞",
            slots={
                "face": slot(False),
                "voice": slot(True, fingerprint=fp("demo-voice-yunfei"),
                              ref="avatar_profiles.db#演示-云飞#voice_b64"),
                "prompt": slot(True, fingerprint=fp("demo-prompt-yunfei"),
                               ref="avatar_profiles.db#演示-云飞#system_prompt"),
                "knowledge": slot(False),
            },
            tags=["voicepack"], created_at=to_iso(now - 7 * 86400),
            raw={"storage": "demo", "demo": True, "description": "演示：仅声音+人设",
                 "voicepack_spk": "spk_demo_0007", "usage": {"n": 12, "k": "activate"}},
        ),
        make_persona(
            source_key="演示-加密行", display_name="演示-加密行",
            slots={k: slot(False) for k in SLOT_KEYS},
            tags=["encrypted"], created_at=None,
            raw={"storage": "demo", "demo": True, "encrypted": True},
        ),
    ]


# ── 入口 ─────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="avatarhub 人设/角色库 → 人设总线归一化 JSON（只读）")
    ap.add_argument("--input", default="",
                    help=f"引擎根目录覆盖（默认 {DEFAULT_ENGINE_DIR}）")
    ap.add_argument("--out", default="avatarhub_personas.json",
                    help="输出 JSON 路径（默认 ./avatarhub_personas.json）")
    ap.add_argument("--demo", action="store_true",
                    help="不读真实数据，生成 3 条演示数据（管道联调）")
    args = ap.parse_args()

    engine_dir = Path(args.input).resolve() if args.input else DEFAULT_ENGINE_DIR
    out_path = Path(args.out).resolve()

    if args.demo:
        personas = demo_personas()
    else:
        # 只读纪律护栏：输出禁止落在被读引擎目录内
        if str(out_path).startswith(str(engine_dir) + os.sep):
            print(f"[export_avatarhub_personas] 错误: --out 不得位于被读目录内"
                  f"（{engine_dir}）", file=sys.stderr)
            return 2
        personas = collect(engine_dir)

    doc = {"version": 1, "source_system": SOURCE_SYSTEM,
           "exported_at": now_iso(), "personas": personas}
    payload = json.dumps(doc, ensure_ascii=False, indent=2)

    # 出厂自查（防泄漏最后一道闸）：raw 走白名单后这里理论上永不命中
    m = _LEAK_RE.search(json.dumps(personas, ensure_ascii=False))
    if m:
        print("[export_avatarhub_personas] 错误: 导出内容含疑似 base64 长串，"
              f"已拒绝写出（片段头 {m.group(0)[:32]}…）", file=sys.stderr)
        return 3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n", encoding="utf-8")
    n_slots = {k: sum(1 for p in personas if p["slots"][k]["present"])
               for k in SLOT_KEYS}
    print(f"[export_avatarhub_personas] 已导出 {len(personas)} 条 → {out_path}")
    print(f"[export_avatarhub_personas] 槽位分布: "
          + " ".join(f"{k}={v}" for k, v in n_slots.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
