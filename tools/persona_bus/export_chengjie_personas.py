# -*- coding: utf-8 -*-
"""export_chengjie_personas.py — chengjie（智聊/通译共用引擎）AI 人设 → 人设总线归一化 JSON（P5，只读）。

数据源（全部只读，缺失即空导出 + stderr 警告，退出码 0；SQLite 一律 mode=ro）：
  <engine>/config/profiles_runtime.yaml     人设主存储（运行时层，最高优先）：profiles.<id> 人设卡
                                            + _history.<id> 版本历史（只计数）。
  <engine>/config/personas.yaml             规范运营层（仅当 runtime 层缺失时回退读取）。
  <engine>/config/voice_refs/<id>.<ext>     声纹参考音（voice 槽位主资产；wav/mp3/m4a/ogg/flac）。
  <engine>/config/prerender_lines/<id>.txt  人设专属台词/话术库（knowledge 槽位主资产）。
  <engine>/config/persona_media.db          人设相册注册表（face 槽位）：persona_media 表，
                                            主资产 = 最早的 enabled photo（行内已存 sha256）。
  <engine>/config/persona_lora.json         角色 LoRA 绑定注册表（raw.has_lora）。
  <engine>/config/bindings_runtime.yaml     会话绑定（tags 加 bound + raw.bindings_count）。
  <engine>/config/deep_persona.db           persona_self_topics 去标识话题计数（raw.self_topics_count）。
  <engine>/assets/voices/<id>/prerendered/  预渲染语音产物（raw.prerendered_count）。

槽位映射（PERSONA_BUS.md §6 chengjie 行的落地口径）：
  face      persona_media.db 中该 persona 最早 enabled photo（chengjie 无脸模，多数 persona
            此槽 present=false）；appearance 外貌锚点是文本配置 → raw.has_appearance_anchor。
  voice     voice_profile.reference_audio_path 指向的文件优先；缺配置按引擎约定自动发现
            config/voice_refs/<id>.<ext>（与 voice_live_routes.discover_reference_audio 同优先序）。
  prompt    人设卡＝profiles_runtime.yaml 中该 persona 的原文块（role/personality/speaking/
            identity/boundaries…），fingerprint = 块字节（CRLF→LF 归一）sha256。
  knowledge config/prerender_lines/<id>.txt 专属台词库。引擎级 knowledge_base.db / 术语库 /
            translation_memory.db 是共享资产、无 persona 归属字段，不进槽位。

解析策略：人设主存储是 YAML，标准库无解析器——本脚本内置**原文块扫描器**（顶层段 → 固定缩进
子块），prompt 指纹恒取原文块字节，与解析器无关；PyYAML（引擎环境自带）可用时仅用于精确抽取
display_name / tags / voice_profile 等字段，缺失自动降级为块内正则抽取（槽位判定不受影响）。

输出格式见 platform/identity/PERSONA_BUS.md §3（version=1，source_system="chengjie"）。
铁律：资产本体（声纹/照片/权重）与任何生物特征数据绝不进导出文件；fingerprint 只能是
      对资产字节的 sha256 摘要；raw 只放白名单标量元数据，字符串截断，绝不含文件内容。
纪律：绝对只读——对 engines/ 只 open(..., "rb"/"r") 与 SQLite mode=ro；--out 禁止落在被读目录内。
仅 Python 标准库。用法见 tools/persona_bus/README.md（--input/--out/--demo 与 avatarhub 版同约定）。
"""
from __future__ import annotations

import argparse
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

try:  # GBK 控制台防中文炸 print（与 export_avatarhub_personas 同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEM = "chengjie"
SLOT_KEYS = ("face", "voice", "prompt", "knowledge")
RAW_STR_MAX = 200                      # raw 内字符串截断上限
VOICE_REF_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac")  # 引擎 discover_reference_audio 同款
_LEAK_RE = re.compile(r"[A-Za-z0-9+/=]{2000,}")   # 导出前自查：base64 长串即事故

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE_DIR = _REPO_ROOT / "engines" / "chengjie"


def warn(msg: str) -> None:
    print(f"[export_chengjie_personas] 警告: {msg}", file=sys.stderr)


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
    """大文件流式 sha256（1 MiB 块）；读失败 → None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        warn(f"读取资产失败（fingerprint 置 null）：{path}（{e}）")
        return None
    return h.hexdigest()


def sha256_text(text: str) -> str:
    """文本资产指纹：CRLF→LF 归一后 utf-8 字节 sha256（跨行尾形态稳定）。"""
    return hashlib.sha256(
        text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


# ── 只读 SQLite ───────────────────────────────────────────────────────


def _connect_ro(db_path: Path) -> "sqlite3.Connection | None":
    """mode=ro URI 打开；WAL 副文件受锁/缺失时回退：拷到系统临时目录再读
    （绝不写 engines/）。任何失败 → None。"""
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


# ── YAML 原文块扫描（标准库通道；prompt 指纹的恒定来源）───────────────

_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$")
# 子块键行：固定 2 空格缩进，键可带单/双引号（如 bindings 的 '1144325634'）
_CHILD_KEY_RE = re.compile(r"^  (['\"]?)([^\s:#'\"][^:'\"]*)\1:\s*(?:#.*)?$")


def split_top_sections(text: str) -> "dict[str, str]":
    """顶层键 → 原文段（含键行，含其全部缩进内容行与注释）。

    极简 YAML 顶层切分：行首无缩进且形如 ``key:`` 的行开新段；``key: value``
    单行标量（如 updated_at）也各成一段。只为 profiles/_history/bindings
    这类"键→嵌套映射"文件设计，不是通用 YAML 解析。
    """
    sections: "dict[str, str]" = {}
    cur_key, cur_lines = None, []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if bare and not bare[0].isspace():
            m = _TOP_KEY_RE.match(bare)
            if m is None:
                m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s", bare)
                m = m2
            if m:
                if cur_key is not None:
                    sections[cur_key] = "".join(cur_lines)
                cur_key, cur_lines = m.group(1), [line]
                continue
        if cur_key is not None:
            cur_lines.append(line)
    if cur_key is not None:
        sections[cur_key] = "".join(cur_lines)
    return sections


def split_child_blocks(section_text: str) -> "dict[str, str]":
    """段内 2 空格缩进子键 → 原文块（含键行与其更深缩进内容）。

    块文本即「人设卡资产字节」：对它做 sha256 即 prompt 槽位指纹——
    不依赖任何 YAML 解析器，跨环境（有无 PyYAML）指纹一致。
    """
    blocks: "dict[str, str]" = {}
    lines = section_text.splitlines(keepends=True)
    cur_key, cur_lines = None, []
    for line in lines[1:]:   # 跳过段首的顶层键行
        m = _CHILD_KEY_RE.match(line.rstrip("\r\n"))
        if m:
            if cur_key is not None:
                blocks[cur_key] = "".join(cur_lines)
            cur_key, cur_lines = m.group(2).strip(), [line]
            continue
        if cur_key is not None:
            cur_lines.append(line)
    if cur_key is not None:
        blocks[cur_key] = "".join(cur_lines)
    return blocks


def _strip_scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v.strip()


def _block_field(block: str, key: str, indent: int = 4) -> str:
    """块内固定缩进单行标量抽取（无 PyYAML 降级通道）；抽不到 → ""。"""
    m = re.search(rf"^ {{{indent}}}{re.escape(key)}:[ \t]*(\S.*)$", block, re.M)
    return _strip_scalar(m.group(1)) if m else ""


def _block_list(block: str, key: str, indent: int = 4) -> "list[str]":
    """块内 ``key:`` 后连续 ``- item`` 行的列表抽取；抽不到 → []。"""
    m = re.search(rf"^ {{{indent}}}{re.escape(key)}:\s*$", block, re.M)
    if not m:
        return []
    items: "list[str]" = []
    for line in block[m.end():].splitlines():
        lm = re.match(rf"^ {{{indent}}}- (.+)$", line)
        if lm:
            items.append(_strip_scalar(lm.group(1)))
        elif line.strip() and not line.startswith(" " * (indent + 1)):
            break
    return items


def _block_sub(block: str, key: str, indent: int = 4) -> str:
    """块内 ``key:`` 子段原文（其后所有缩进更深的行）；无 → ""。"""
    m = re.search(rf"^ {{{indent}}}{re.escape(key)}:\s*$", block, re.M)
    if not m:
        return ""
    out = []
    for line in block[m.end():].splitlines(keepends=True):
        if line.strip() and not line.startswith(" " * (indent + 1)):
            break
        out.append(line)
    return "".join(out)


def try_load_yaml(text: str) -> "dict | None":
    """PyYAML 可选增强：引擎环境自带则精确解析；缺失/解析失败 → None（降级块扫描）。"""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else None
    except Exception as e:
        warn(f"PyYAML 解析失败，降级块扫描：{e}")
        return None


# ── 数据源读取 ────────────────────────────────────────────────────────


def load_persona_media(db_path: Path) -> "dict | None":
    """persona_media.db → {persona_id: {"photos": [(created_at, id, sha256,
    file_path), ...], "count": n, "video_count": n}}；失败 → None。"""
    conn = _connect_ro(db_path)
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id, persona_id, media_type, file_path, sha256, enabled, "
            "created_at FROM persona_media").fetchall()
    except sqlite3.Error as e:
        warn(f"{db_path} 读取失败：{e}")
        conn.close()
        return None
    conn.close()
    per: dict = {}
    for mid, pid, mtype, fpath, sha, enabled, created in rows:
        d = per.setdefault(str(pid), {"photos": [], "count": 0, "video_count": 0})
        if not enabled:
            continue
        d["count"] += 1
        if str(mtype) == "video":
            d["video_count"] += 1
        else:
            d["photos"].append((float(created or 0), str(mid),
                                str(sha or ""), str(fpath or "")))
    for d in per.values():
        d["photos"].sort()
    return per


def load_self_topic_counts(db_path: Path) -> dict:
    """deep_persona.db → {persona_id: 话题计数}；缺库/缺表一律 {}（可选旁路）。"""
    conn = _connect_ro(db_path)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT persona_id, COUNT(*) FROM persona_self_topics "
            "GROUP BY persona_id").fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def load_bindings_count(path: Path) -> dict:
    """bindings_runtime.yaml → {persona_id: 绑定会话数}；缺失/解析失败 → {}。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    counts: dict = {}
    data = try_load_yaml(text)
    if data is not None:
        for v in (data.get("bindings") or {}).values():
            pid = str((v or {}).get("id") or "").strip() if isinstance(v, dict) else ""
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        return counts
    section = split_top_sections(text).get("bindings", "")
    for block in split_child_blocks(section).values():
        pid = _block_field(block, "id")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def load_lora_registry(path: Path) -> dict:
    """persona_lora.json → {pid: entry}；缺失/损坏 → {}。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ── raw 白名单（只挑明确安全的标量，不做黑名单）───────────────────────


def _scalar(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v[:RAW_STR_MAX]
    return None


def build_raw(pid: str, prof: dict, *, storage: str, parser: str,
              engine_dir: Path, media_info=None, lora=None,
              bindings: int = 0, history: int = 0, self_topics: int = 0) -> dict:
    """raw 白名单：设置项/引用名/计数，绝不放人设卡全文、外貌锚点全文、文件内容。"""
    raw: dict = {"storage": storage, "parser": parser}
    for field in ("role", "gender"):
        v = _scalar(prof.get(field))
        if v not in (None, ""):
            raw[field] = v
    age = prof.get("age")
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        raw["age"] = int(age)
    elif isinstance(age, str) and age.isdigit():
        raw["age"] = int(age)
    if str(prof.get("appearance") or "").strip():
        raw["has_appearance_anchor"] = True   # 锚点全文属人设卡内容，只出布尔
    scenes = prof.get("selfie_scenes")
    if isinstance(scenes, list) and scenes:
        raw["selfie_scenes_count"] = len(scenes)
    beats = (prof.get("life_arc") or {}).get("beats") if isinstance(
        prof.get("life_arc"), dict) else None
    if isinstance(beats, list) and beats:
        raw["life_arc_beats_count"] = len(beats)
    vp = prof.get("voice_profile") if isinstance(prof.get("voice_profile"), dict) else {}
    if vp:
        if "enabled" in vp:
            raw["voice_enabled"] = bool(vp.get("enabled"))
        if "owner_consent" in vp:
            raw["voice_owner_consent"] = bool(vp.get("owner_consent"))
        for field in ("backend", "voice", "instruct_style", "emotion"):
            v = _scalar(vp.get(field))
            if v not in (None, ""):
                raw[f"voice_{field}"] = v
    pre_dir = engine_dir / "assets" / "voices" / pid / "prerendered"
    if pre_dir.is_dir():
        n = sum(1 for p in pre_dir.glob("*.ogg"))
        if n:
            raw["prerendered_count"] = n
    if media_info:
        if media_info.get("count"):
            raw["persona_media_count"] = media_info["count"]
        if media_info.get("video_count"):
            raw["persona_media_video_count"] = media_info["video_count"]
    if lora:
        raw["has_lora"] = True
    if bindings:
        raw["bindings_count"] = bindings
    if history:
        raw["history_count"] = history
    if self_topics:
        raw["self_topics_count"] = self_topics
    return raw


# ── 槽位归一化 ────────────────────────────────────────────────────────


def slot(present: bool, fingerprint=None, ref=None, version=None) -> dict:
    if not present:
        return {"present": False, "fingerprint": None, "ref": None, "version": None}
    return {"present": True, "fingerprint": fingerprint, "ref": ref,
            "version": version}


def face_slot(pid: str, media_info, engine_dir: Path) -> dict:
    """persona_media.db 最早 enabled photo 为主资产。chengjie 无脸模，
    无相册即 present=false（引擎典型形态）。"""
    photos = (media_info or {}).get("photos") or []
    for _created, mid, sha, fpath in photos:
        if re.fullmatch(r"[0-9a-f]{64}", sha or ""):
            # 行内 sha256 即导入时算好的文件字节摘要，直接用（文件可能已被整理挪动）
            return slot(True, fingerprint=sha, ref=f"persona_media.db#{mid}")
        if fpath:
            p = Path(fpath)
            if not p.is_absolute():
                p = engine_dir / fpath
            if p.is_file():
                return slot(True, fingerprint=sha256_file(p),
                            ref=f"persona_media.db#{mid}")
    return slot(False)


def voice_slot(pid: str, prof: dict, engine_dir: Path) -> dict:
    """voice_profile.reference_audio_path 优先；缺配置按引擎约定发现
    config/voice_refs/<id>.<ext>。present 按文件实际存在且非空判断。"""
    vp = prof.get("voice_profile") if isinstance(prof.get("voice_profile"), dict) else {}
    ref_cfg = str(vp.get("reference_audio_path") or "").strip()
    candidates: "list[tuple[Path, str]]" = []
    if ref_cfg:
        p = Path(ref_cfg)
        if not p.is_absolute():
            p = engine_dir / ref_cfg
        try:
            rel = p.resolve().relative_to(engine_dir.resolve()).as_posix()
        except ValueError:
            # 引擎根外的绝对路径：ref 用字段指针形式，不落绝对路径（可能含机器用户名）
            rel = f"profiles#{pid}#voice_profile.reference_audio_path"
        candidates.append((p, rel))
    for ext in VOICE_REF_EXTS:
        p = engine_dir / "config" / "voice_refs" / f"{pid}{ext}"
        candidates.append((p, f"config/voice_refs/{pid}{ext}"))
    for p, rel in candidates:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return slot(True, fingerprint=sha256_file(p), ref=rel)
        except OSError:
            continue
    return slot(False)


def prompt_slot(pid: str, block: str, storage_file: str) -> dict:
    """人设卡＝主存储 YAML 中该 persona 的原文块；指纹＝块字节（CRLF→LF）sha256。"""
    if not (block or "").strip():
        return slot(False)
    return slot(True, fingerprint=sha256_text(block),
                ref=f"config/{storage_file}#profiles.{pid}")


def knowledge_slot(pid: str, engine_dir: Path) -> dict:
    """专属台词/话术库 config/prerender_lines/<id>.txt（共享 _common.txt 不算）。"""
    p = engine_dir / "config" / "prerender_lines" / f"{pid}.txt"
    try:
        if p.is_file() and p.stat().st_size > 0:
            return slot(True, fingerprint=sha256_file(p),
                        ref=f"config/prerender_lines/{pid}.txt")
    except OSError:
        pass
    return slot(False)


# ── 汇总 ─────────────────────────────────────────────────────────────


def make_persona(*, source_key: str, display_name: str, slots: dict,
                 tags=None, created_at=None, raw=None) -> dict:
    return {
        "source_key": source_key,
        "display_name": display_name,
        "customer_name": None,   # chengjie 人设无客户绑定字段（PERSONA_BUS.md §3.2）
        "slots": slots,
        "tags": tags or [],
        "created_at": created_at,
        "raw": raw or {},
    }


def collect(engine_dir: Path) -> list:
    if not engine_dir.is_dir():
        warn(f"数据源目录不存在：{engine_dir}（输出空 personas）")
        return []
    cfg_dir = engine_dir / "config"

    # 人设主存储：profiles_runtime.yaml（运行时层）优先，personas.yaml（规范层）回退
    storage_file, storage = "", ""
    for fname, label in (("profiles_runtime.yaml", "profiles_runtime"),
                         ("personas.yaml", "personas_canonical")):
        if (cfg_dir / fname).is_file():
            storage_file, storage = fname, label
            break
    if not storage_file:
        warn(f"{cfg_dir} 下未找到人设主存储"
             "（profiles_runtime.yaml / personas.yaml），输出空 personas")
        return []
    try:
        text = (cfg_dir / storage_file).read_text(encoding="utf-8-sig")
    except OSError as e:
        warn(f"{cfg_dir / storage_file} 读取失败：{e}（输出空 personas）")
        return []

    sections = split_top_sections(text)
    blocks = split_child_blocks(sections.get("profiles", ""))
    if not blocks:
        warn(f"{storage_file} 中未发现 profiles 段或其为空（输出空 personas）")
        return []
    history_blocks = split_child_blocks(sections.get("_history", ""))

    doc = try_load_yaml(text)
    parser = "yaml" if doc is not None else "block_scan"
    yaml_profiles = (doc or {}).get("profiles") or {}
    yaml_history = (doc or {}).get("_history") or {}

    # 旁路元数据（缺失均降级）
    media_map = None
    media_db = cfg_dir / "persona_media.db"
    if media_db.is_file():
        media_map = load_persona_media(media_db)
    media_map = media_map or {}
    self_topics = load_self_topic_counts(cfg_dir / "deep_persona.db") \
        if (cfg_dir / "deep_persona.db").is_file() else {}
    bindings = load_bindings_count(cfg_dir / "bindings_runtime.yaml")
    lora_map = load_lora_registry(cfg_dir / "persona_lora.json")

    personas = []
    for pid in sorted(blocks):
        block = blocks[pid]
        prof = yaml_profiles.get(pid) if isinstance(yaml_profiles.get(pid), dict) else None
        if prof is None:   # 无 PyYAML：块内正则抽关键字段（槽位判定不受影响）
            vp_sub = _block_sub(block, "voice_profile")
            prof = {
                "name": _block_field(block, "name"),
                "role": _block_field(block, "role"),
                "age": _block_field(block, "age"),
                "gender": _block_field(block, "gender"),
                "appearance": _block_field(block, "appearance")
                or _block_sub(block, "appearance"),
                "tags": _block_list(block, "tags"),
                "selfie_scenes": _block_list(block, "selfie_scenes"),
                "voice_profile": {
                    "enabled": _block_field(vp_sub, "enabled", 6) == "true",
                    "owner_consent": _block_field(vp_sub, "owner_consent", 6) == "true",
                    "backend": _block_field(vp_sub, "backend", 6),
                    "voice": _block_field(vp_sub, "voice", 6),
                    "instruct_style": _block_field(vp_sub, "instruct_style", 6),
                    "emotion": _block_field(vp_sub, "emotion", 6),
                    "reference_audio_path":
                        _block_field(vp_sub, "reference_audio_path", 6),
                } if vp_sub else {},
            }
        if isinstance(yaml_history.get(pid), list):
            history_n = len(yaml_history[pid])
        else:
            history_n = len(re.findall(r"^  - ts:", history_blocks.get(pid, ""), re.M))
        media_info = media_map.get(pid)
        slots = {
            "face":      face_slot(pid, media_info, engine_dir),
            "voice":     voice_slot(pid, prof, engine_dir),
            "prompt":    prompt_slot(pid, block, storage_file),
            "knowledge": knowledge_slot(pid, engine_dir),
        }
        tags = [str(t)[:64] for t in (prof.get("tags") or []) if str(t).strip()]
        if bindings.get(pid):
            tags.append("bound")
        personas.append(make_persona(
            source_key=pid,
            display_name=str(prof.get("name") or "").strip() or pid,
            slots=slots, tags=tags,
            created_at=None,   # 主存储无 per-persona 创建时刻字段
            raw=build_raw(pid, prof, storage=storage, parser=parser,
                          engine_dir=engine_dir, media_info=media_info,
                          lora=lora_map.get(pid), bindings=bindings.get(pid, 0),
                          history=history_n, self_topics=self_topics.get(pid, 0)),
        ))
    return personas


# ── 演示数据 ─────────────────────────────────────────────────────────


def demo_personas() -> list:
    """3 条演示数据（管道联调）：四槽齐全 / 声音+人设（chengjie 典型形态）/ 仅人设卡。"""
    now = int(time.time())
    fp = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()  # noqa: E731
    return [
        make_persona(
            source_key="demo_xiaoyu", display_name="演示-小雨",
            slots={
                "face": slot(True, fingerprint=fp("demo-face-xiaoyu"),
                             ref="persona_media.db#demo0001"),
                "voice": slot(True, fingerprint=fp("demo-voice-xiaoyu"),
                              ref="config/voice_refs/demo_xiaoyu.wav"),
                "prompt": slot(True, fingerprint=fp("demo-prompt-xiaoyu"),
                               ref="config/profiles_runtime.yaml#profiles.demo_xiaoyu"),
                "knowledge": slot(True, fingerprint=fp("demo-kb-xiaoyu"),
                                  ref="config/prerender_lines/demo_xiaoyu.txt"),
            },
            tags=["年轻", "活泼", "bound"], created_at=to_iso(now - 30 * 86400),
            raw={"storage": "demo", "parser": "demo", "demo": True,
                 "role": "大学生 / 生活博主", "age": 22, "gender": "female",
                 "has_appearance_anchor": True, "selfie_scenes_count": 5,
                 "voice_enabled": True, "voice_backend": "avatar_clone",
                 "prerendered_count": 14, "persona_media_count": 6,
                 "bindings_count": 1},
        ),
        make_persona(
            source_key="demo_meiling", display_name="演示-美玲",
            slots={
                "face": slot(False),
                "voice": slot(True, fingerprint=fp("demo-voice-meiling"),
                              ref="config/voice_refs/demo_meiling.wav"),
                "prompt": slot(True, fingerprint=fp("demo-prompt-meiling"),
                               ref="config/profiles_runtime.yaml#profiles.demo_meiling"),
                "knowledge": slot(False),
            },
            tags=["职场", "专业"], created_at=to_iso(now - 7 * 86400),
            raw={"storage": "demo", "parser": "demo", "demo": True,
                 "role": "营销总监", "age": 35, "gender": "female",
                 "voice_enabled": True, "voice_backend": "avatar_clone",
                 "history_count": 2},
        ),
        make_persona(
            source_key="demo_minimal", display_name="演示-极简",
            slots={
                "face": slot(False),
                "voice": slot(False),
                "prompt": slot(True, fingerprint=fp("demo-prompt-minimal"),
                               ref="config/profiles_runtime.yaml#profiles.demo_minimal"),
                "knowledge": slot(False),
            },
            tags=[], created_at=None,
            raw={"storage": "demo", "parser": "demo", "demo": True},
        ),
    ]


# ── 入口 ─────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="chengjie AI 人设 → 人设总线归一化 JSON（只读）")
    ap.add_argument("--input", default="",
                    help=f"引擎根目录覆盖（默认 {DEFAULT_ENGINE_DIR}）")
    ap.add_argument("--out", default="chengjie_personas.json",
                    help="输出 JSON 路径（默认 ./chengjie_personas.json）")
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
            print(f"[export_chengjie_personas] 错误: --out 不得位于被读目录内"
                  f"（{engine_dir}）", file=sys.stderr)
            return 2
        personas = collect(engine_dir)

    doc = {"version": 1, "source_system": SOURCE_SYSTEM,
           "exported_at": now_iso(), "personas": personas}
    payload = json.dumps(doc, ensure_ascii=False, indent=2)

    # 出厂自查（防泄漏最后一道闸）：raw 走白名单后这里理论上永不命中
    m = _LEAK_RE.search(json.dumps(personas, ensure_ascii=False))
    if m:
        print("[export_chengjie_personas] 错误: 导出内容含疑似 base64 长串，"
              f"已拒绝写出（片段头 {m.group(0)[:32]}…）", file=sys.stderr)
        return 3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n", encoding="utf-8")
    n_slots = {k: sum(1 for p in personas if p["slots"][k]["present"])
               for k in SLOT_KEYS}
    print(f"[export_chengjie_personas] 已导出 {len(personas)} 条 → {out_path}")
    print(f"[export_chengjie_personas] 槽位分布: "
          + " ".join(f"{k}={v}" for k, v in n_slots.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
