# -*- coding: utf-8 -*-
"""export_huoke_personas.py — huoke 养号人设 → 人设总线归一化 JSON（P5，只读）。

数据源（全部只读，缺失即空导出 + stderr 警告，退出码 0）：
  <engine>/config/fb_target_personas.yaml   目标画像主存储：personas.<persona_key>
                                            （display 标签/国家/年龄段、L1 规则、VLM prompt、
                                            match_criteria）。source_key ＝ persona_key。
  <engine>/config/chat_messages.yaml        打招呼话术库：countries.<cc>（加友验证语/开场 DM/
                                            话术变体/评论模板），persona 经 country_code 关联；
                                            无国家块时回退顶层 legacy 全局话术（与引擎
                                            src/app_automation/fb_content_assets.py 消费序一致）。
                                            ⚠️ 顶层 device_referrals 节含真实引流账号
                                            （手机号 / TG id），本脚本绝不读取该节；出厂自查
                                            还会扫 +国际手机号样式，命中即拒写。
  <engine>/config/persona_knowledge.yaml    客群知识库：interest_topics.<CC> / group_keywords.<CC>
                                            （只进 raw 计数）。
  <engine>/config/personas.yaml             studio 养号内容人设（伪垂类账号人格：niche/tone/
                                            content_themes/hashtags）→ source_key 前缀 `studio:`。
  <engine>/data/fb_active_persona_override.json  运行时生效客群（tags: active）。
  <engine>/data/openclaw.db                 （可选，SQLite mode=ro）fb_target_personas 审计表的
                                            created_at/updated_at；fb_profile_insights 识别计数。

槽位映射（侦察结论，PERSONA_BUS.md §6 huoke 行的落地细化）：
  face      获客目标画像无自有头像资产 → 恒 present=false；
  voice     获客无声纹资产 → 恒 present=false；
  prompt    打招呼话术包：chat_messages.yaml#countries.<cc>（回退 #legacy），指纹＝话术块字节；
  knowledge 画像定义块（L1 规则/兴趣/关键词/match_criteria）＝ fb_target_personas.yaml
            #personas.<key> 块字节；persona_knowledge 的词表只进 raw 计数。
  studio 家族：prompt＝personas.yaml#personas.<key> 人设定义块；knowledge 恒缺席。

指纹约定：标准库没有 YAML 解析器，本脚本按缩进做**块提取**，fingerprint ＝ 该块文本
（行以 \n 规范连接、去尾空行）UTF-8 字节的 sha256 —— 与清除执行器
engines/huoke/src/persona_purge_agent.py 同一约定，注册表可跨导出对账。

铁律：话术原文/手机号/聊天记录/任何资产本体绝不进导出文件；fingerprint 只能是字节摘要；
      raw 走白名单（计数/风格标签/id/开关），字符串截断。
纪律：绝对只读——对 engines/ 只 open(..., "r"/"rb") 与 SQLite mode=ro；--out 禁止落在被读目录内。
仅 Python 标准库。用法见 tools/persona_bus/README.md（与 export_avatarhub_personas.py 同族）。
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

try:  # GBK 控制台防中文炸 print（与同目录 avatarhub 导出器同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_SYSTEM = "huoke"
SLOT_KEYS = ("face", "voice", "prompt", "knowledge")
STUDIO_PREFIX = "studio:"              # studio 内容人设家族的 source_key 前缀
RAW_STR_MAX = 200                      # raw 内字符串截断上限
_LEAK_RE = re.compile(r"[A-Za-z0-9+/=]{2000,}")   # base64 长串＝资产本体入文，事故
_PHONE_RE = re.compile(r"\+\d{8,}")               # 引流账号手机号样式（huoke 特有闸）
_ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE_DIR = _REPO_ROOT / "engines" / "huoke"

# chat_messages.yaml 顶层 legacy 话术节（无国家块时的回退，按文件出现顺序拼指纹）。
# 注意不含 device_referrals（真实引流账号，绝不读取）。
_LEGACY_SCRIPT_KEYS = ("friend_request_notes", "greeting_messages", "comment_templates",
                       "message_variants", "messages", "referral_line",
                       "referral_instagram", "referral_whatsapp", "referral_telegram")


def warn(msg: str) -> None:
    print(f"[export_huoke_personas] 警告: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(ts) -> "str | None":
    """unix 秒 → ISO8601(UTC)；0/空/解析失败 → None。"""
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


def iso_or_none(s) -> "str | None":
    """DB/文件里已是 ISO 样式的时间串→原样；不合样式→None。"""
    if isinstance(s, str) and _ISO_LIKE.match(s.strip()):
        return s.strip()
    return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 轻量 YAML 块提取（仅标准库；缩进扫描，不做完整解析）───────────────
#
# 适用对象是 huoke 的四个人设 YAML（层级规整、两空格缩进）。规则：
#   * 小节 = `key:` 行 + 体；体 = 其后所有「空行 / 更深缩进行 / 同缩进 dash 项」，
#     遇同缩进的其他内容（含注释）即终止；
#   * 块文本 = key 行起到体末（去尾空行），fingerprint 对其 UTF-8 字节求 sha256。


def read_lines(path: Path) -> "list[str] | None":
    """读文件 → 行列表（无行尾符）；缺失/读失败 → None（调用方警告降级）。"""
    try:
        return path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return None
    except OSError as e:
        warn(f"{path} 读取失败：{e}")
        return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not line.strip()


def _is_dash(line: str) -> bool:
    st = line.strip()
    return st == "-" or st.startswith("- ")


def _body_end(lines: "list[str]", key_idx: int, indent: int, end: int) -> int:
    """小节体终点（不含）：从 key 行下一行扫到首个「同/浅缩进的非 dash 内容」。"""
    j = key_idx + 1
    while j < end:
        ln = lines[j]
        if _is_blank(ln):
            j += 1
            continue
        ind = _indent(ln)
        if ind > indent or (ind == indent and _is_dash(ln)):
            j += 1
            continue
        break
    return j


def section(lines: "list[str]", key: str, indent: int = 0,
            start: int = 0, end: "int | None" = None) -> "tuple[int, int] | None":
    """在 [start,end) 找 `key:` 小节 → (key 行下标, 体终点)；找不到 → None。"""
    if lines is None:
        return None
    if end is None:
        end = len(lines)
    pat = re.compile(rf"^ {{{indent}}}{re.escape(key)}:(\s|$)")
    for i in range(start, end):
        if pat.match(lines[i]):
            return i, _body_end(lines, i, indent, end)
    return None


_CHILD_KEY_RE_CACHE: dict = {}


def children(lines: "list[str]", sec: "tuple[int, int]",
             child_indent: int) -> "dict[str, tuple[int, int]]":
    """小节体内、恰在 child_indent 缩进上的子块 → {子键: (子键行, 终点)}（保持文件序）。

    同缩进的注释/分隔线会关闭前一个子块（不并入任何块，指纹只含块自身行）。
    """
    key_re = _CHILD_KEY_RE_CACHE.get(child_indent)
    if key_re is None:
        key_re = re.compile(rf"^ {{{child_indent}}}([^\s#:][^:]*):(\s|$)")
        _CHILD_KEY_RE_CACHE[child_indent] = key_re
    out: "dict[str, tuple[int, int]]" = {}
    cur_key, cur_start = None, -1
    s, e = sec
    for i in range(s + 1, e):
        ln = lines[i]
        if _is_blank(ln):
            continue
        ind = _indent(ln)
        if ind < child_indent:
            if cur_key is not None:
                out[cur_key] = (cur_start, i)
                cur_key = None
            break
        if ind == child_indent:
            m = key_re.match(ln)
            if cur_key is not None:
                out[cur_key] = (cur_start, i)
                cur_key = None
            if m:
                cur_key, cur_start = m.group(1).strip(), i
    if cur_key is not None:
        out[cur_key] = (cur_start, e)
    return out


def block_text(lines: "list[str]", s: int, e: int) -> str:
    """块文本（含 key 行，去尾空行，\n 规范连接）——fingerprint 的字节来源。"""
    seg = lines[s:e]
    while seg and not seg[-1].strip():
        seg.pop()
    return "\n".join(seg)


def _clean_scalar(v: str) -> "str | None":
    v = (v or "").strip()
    if not v:
        return None
    if v[0] in "\"'":
        q = v[0]
        j = v.find(q, 1)
        return (v[1:j] if j > 0 else v.strip(q)) or None
    m = re.search(r"\s#", v)
    if m:
        v = v[: m.start()]
    return v.strip() or None


def scalar(lines: "list[str]", s: int, e: int, field: str,
           indent: int) -> "str | None":
    """块 [s,e) 内取 `field: <标量>`（剥引号/行内注释）。"""
    pat = re.compile(rf"^ {{{indent}}}{re.escape(field)}:\s*(.*)$")
    for i in range(s, e):
        m = pat.match(lines[i])
        if m:
            return _clean_scalar(m.group(1))
    return None


def to_int(v) -> "int | None":
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def count_dash(lines: "list[str]", s: int, e: int) -> int:
    return sum(1 for i in range(s, e) if _is_dash(lines[i]))


def count_regex(lines: "list[str]", s: int, e: int, pattern: str) -> int:
    pat = re.compile(pattern)
    return sum(1 for i in range(s, e) if pat.match(lines[i]))


def sub_count(lines, s, e, field: str, indent: int,
              pattern: "str | None" = None) -> int:
    """块内子字段的列表项计数（pattern 缺省数 dash 项）。子字段缺失 → 0。"""
    sec = section(lines, field, indent, s, e)
    if not sec:
        return 0
    if pattern:
        return count_regex(lines, sec[0] + 1, sec[1], pattern)
    return count_dash(lines, sec[0] + 1, sec[1])


def sub_items(lines, s, e, field: str, indent: int) -> "list[str]":
    """块内子字段的 dash 项文本（剥 `- ` 与引号）。"""
    sec = section(lines, field, indent, s, e)
    if not sec:
        return []
    out = []
    for i in range(sec[0] + 1, sec[1]):
        ln = lines[i]
        if _is_dash(ln):
            out.append(_clean_scalar(ln.strip()[2:]) or "")
    return [x for x in out if x]


def count_inline_lists(lines, s, e) -> int:
    """块内 `key: [a, b, c]` 行内列表的条目总数（persona_knowledge 计数用）。"""
    n = 0
    pat = re.compile(r"^\s*[^\s#:][^:]*:\s*\[(.*)\]\s*$")
    for i in range(s, e):
        m = pat.match(lines[i])
        if m and m.group(1).strip():
            n += m.group(1).count(",") + 1
    return n


# ── 只读 SQLite（openclaw.db 审计快照，缺失/无表均降级）────────────────


def _connect_ro(db_path: Path) -> "sqlite3.Connection | None":
    """mode=ro URI 打开；受 WAL 锁时拷到系统临时目录再读（不碰 engines/）。"""
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


def load_db_meta(db_path: Path) -> dict:
    """openclaw.db → {"audit": {persona_key: {created_at, updated_at}},
    "insights": {persona_key: (总判定数, 命中数)}}。库/表缺失 → 空 dict（合法降级）。"""
    if not db_path.is_file():
        return {}
    conn = _connect_ro(db_path)
    if conn is None:
        return {}
    meta: dict = {"audit": {}, "insights": {}}
    try:
        try:
            for k, ca, ua in conn.execute(
                    "SELECT persona_key, created_at, updated_at FROM fb_target_personas"):
                meta["audit"][str(k)] = {"created_at": iso_or_none(ca),
                                         "updated_at": iso_or_none(ua)}
        except sqlite3.Error:
            pass
        try:
            for k, n, m in conn.execute(
                    "SELECT persona_key, COUNT(*), COALESCE(SUM(match),0)"
                    " FROM fb_profile_insights GROUP BY persona_key"):
                meta["insights"][str(k)] = (int(n or 0), int(m or 0))
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return meta


# ── 槽位归一化 ────────────────────────────────────────────────────────


def slot(present: bool, fingerprint=None, ref=None, version=None) -> dict:
    if not present:
        return {"present": False, "fingerprint": None, "ref": None, "version": None}
    return {"present": True, "fingerprint": fingerprint, "ref": ref,
            "version": version}


def make_persona(*, source_key: str, display_name: str, slots: dict,
                 tags=None, created_at=None, raw=None) -> dict:
    return {
        "source_key": source_key,
        "display_name": display_name,
        "customer_name": None,   # huoke 人设无客户绑定字段（归属由控制台在注册表侧维护）
        "slots": slots,
        "tags": tags or [],
        "created_at": created_at,
        "raw": raw or {},
    }


# ── 话术包（prompt 槽）解析 ───────────────────────────────────────────


def resolve_prompt_slot(chat_lines, cc: str) -> "tuple[dict, dict]":
    """按国家码解析话术包 →（prompt 槽, 话术计数 raw 片段）。

    优先 countries.<cc> 块；缺国家块回退顶层 legacy 全局话术；两者皆无 → 缺席。
    指纹＝话术块文本字节 sha256（绝不外发原文，只发摘要+计数）。
    """
    if chat_lines is None:
        return slot(False), {}
    if cc:
        csec = section(chat_lines, "countries", 0)
        if csec:
            blk = children(chat_lines, csec, 2).get(cc)
            if blk:
                s, e = blk
                counts = {
                    "prompt_source": f"countries.{cc}",
                    "friend_note_count": sub_count(chat_lines, s, e,
                                                   "friend_request_notes", 4),
                    "greeting_count": sub_count(chat_lines, s, e,
                                                "greeting_messages", 4),
                    "comment_template_count": sub_count(chat_lines, s, e,
                                                        "comment_templates", 4),
                    "variant_count": sub_count(chat_lines, s, e, "message_variants",
                                               4, pattern=r"^\s*- id:"),
                }
                return slot(True, fingerprint=sha256_text(block_text(chat_lines, s, e)),
                            ref=f"config/chat_messages.yaml#countries.{cc}"), counts
    # legacy 顶层话术回退（与 fb_content_assets 的消费链一致）
    parts = []
    counts: dict = {}
    for key in _LEGACY_SCRIPT_KEYS:
        sec = section(chat_lines, key, 0)
        if sec:
            parts.append((sec[0], block_text(chat_lines, sec[0], sec[1])))
            if key == "greeting_messages":
                counts["greeting_count"] = count_dash(chat_lines, sec[0] + 1, sec[1])
            if key == "message_variants":
                counts["variant_count"] = count_regex(chat_lines, sec[0] + 1, sec[1],
                                                      r"^\s*- id:")
    if not parts:
        return slot(False), {}
    parts.sort()   # 按文件出现顺序拼接，指纹稳定
    counts["prompt_source"] = "legacy"
    return slot(True, fingerprint=sha256_text("\n".join(t for _, t in parts)),
                ref="config/chat_messages.yaml#legacy"), counts


# ── 采集：fb_target 家族（目标画像）─────────────────────────────────


def collect_fb_targets(engine_dir: Path, fb_lines, chat_lines, pk_lines,
                       active_key: str, db_meta: dict) -> list:
    sec = section(fb_lines, "personas", 0)
    if not sec:
        warn("fb_target_personas.yaml 缺 personas 节，目标画像家族输出为空")
        return []
    personas = []
    blocks = children(fb_lines, sec, 2)
    pk_topics = section(pk_lines, "interest_topics", 0) if pk_lines else None
    pk_groups = section(pk_lines, "group_keywords", 0) if pk_lines else None
    for key, (s, e) in blocks.items():
        block = block_text(fb_lines, s, e)
        country_code = scalar(fb_lines, s, e, "country_code", 4) or ""
        locale = scalar(fb_lines, s, e, "locale", 4) or ""
        if not country_code and "-" in locale:
            country_code = locale.split("-", 1)[1]
        cc = country_code.strip().lower()

        prompt_slot, script_counts = resolve_prompt_slot(chat_lines, cc)
        knowledge_slot = slot(True, fingerprint=sha256_text(block),
                              ref=f"config/fb_target_personas.yaml#personas.{key}")

        active_flag = (scalar(fb_lines, s, e, "active", 4) or "true").lower()
        tags = ["fb_target"]
        if key == active_key:
            tags.append("active")
        if active_flag == "false":
            tags.append("disabled")

        raw: dict = {"storage": "fb_target_personas_yaml",
                     "persona_family": "fb_target"}
        for field in ("country_code", "country_zh", "language", "locale", "gender",
                      "display_label", "short_label"):
            v = scalar(fb_lines, s, e, field, 4)
            if v:
                raw[field] = v[:RAW_STR_MAX]
        for field in ("age_min", "age_max"):
            iv = to_int(scalar(fb_lines, s, e, field, 4))
            if iv is not None:
                raw[field] = iv
        counts = {
            "interest_topics_count": sub_count(fb_lines, s, e, "interest_topics", 4),
            "seed_group_keywords_count": sub_count(fb_lines, s, e,
                                                   "seed_group_keywords", 4),
        }
        raw.update({k: v for k, v in counts.items() if v})
        l1 = section(fb_lines, "l1", 4, s, e)
        if l1:
            raw["l1_rules_count"] = count_regex(fb_lines, l1[0] + 1, l1[1],
                                                r"^\s*- kind:")
            th = to_int(scalar(fb_lines, l1[0], l1[1], "pass_threshold", 6))
            if th is not None:
                raw["l1_pass_threshold"] = th
        if section(fb_lines, "vlm_prompt", 4, s, e):
            raw["has_vlm_prompt"] = True
        if section(fb_lines, "match_criteria", 4, s, e):
            raw["has_match_criteria"] = True
        rp = sub_items(fb_lines, s, e, "referral_priority", 4)
        if rp:
            raw["referral_priority"] = ",".join(rp)[:RAW_STR_MAX]
        raw.update({k: v for k, v in script_counts.items() if v})
        if key == active_key:
            raw["is_default"] = True
        # persona_knowledge 客群词表（只计数）
        cc_upper = (country_code or "").strip().upper()
        for label, pk_sec in (("pk_interest_topics_count", pk_topics),
                              ("pk_group_keyword_items", pk_groups)):
            if not pk_sec:
                continue
            cblocks = children(pk_lines, pk_sec, 2)
            blk = cblocks.get(cc_upper) or cblocks.get("_default")
            if blk:
                n = count_inline_lists(pk_lines, blk[0] + 1, blk[1])
                if n:
                    raw[label] = n
        # openclaw.db 审计快照（存在才补）
        audit = (db_meta.get("audit") or {}).get(key) or {}
        created_at = audit.get("created_at")
        if audit.get("updated_at"):
            raw["updated_at"] = audit["updated_at"]
        ins = (db_meta.get("insights") or {}).get(key)
        if ins:
            raw["insights_total"], raw["insights_matched"] = ins

        personas.append(make_persona(
            source_key=key,
            display_name=scalar(fb_lines, s, e, "name", 4) or key,
            slots={"face": slot(False), "voice": slot(False),
                   "prompt": prompt_slot, "knowledge": knowledge_slot},
            tags=tags, created_at=created_at, raw=raw))
    return personas


# ── 采集：studio 家族（养号内容人设）────────────────────────────────


def collect_studio(studio_lines) -> list:
    sec = section(studio_lines, "personas", 0)
    if not sec:
        warn("personas.yaml 缺 personas 节，studio 家族输出为空")
        return []
    personas = []
    for key, (s, e) in children(studio_lines, sec, 2).items():
        block = block_text(studio_lines, s, e)
        raw: dict = {"storage": "personas_yaml", "persona_family": "studio"}
        for field in ("niche", "country", "language", "target_gender",
                      "target_age", "tone"):
            v = scalar(studio_lines, s, e, field, 4)
            if v:
                raw[field] = v[:RAW_STR_MAX]
        n_themes = sub_count(studio_lines, s, e, "content_themes", 4)
        if n_themes:
            raw["content_themes_count"] = n_themes
        n_tags = sub_count(studio_lines, s, e, "hashtags", 4)
        if n_tags:
            raw["hashtags_count"] = n_tags
        ps = section(studio_lines, "platform_strategy", 4, s, e)
        if ps:
            plats = re.findall(r"^ {6}([A-Za-z_]+):",
                               "\n".join(studio_lines[ps[0] + 1: ps[1]]), re.M)
            if plats:
                raw["platforms"] = ",".join(plats)[:RAW_STR_MAX]
        if section(studio_lines, "posting_schedule", 4, s, e):
            raw["has_posting_schedule"] = True
        personas.append(make_persona(
            source_key=STUDIO_PREFIX + key,
            display_name=scalar(studio_lines, s, e, "display_name", 4) or key,
            slots={"face": slot(False), "voice": slot(False),
                   "prompt": slot(True, fingerprint=sha256_text(block),
                                  ref=f"config/personas.yaml#personas.{key}"),
                   "knowledge": slot(False)},
            tags=["studio"], created_at=None, raw=raw))
    return personas


# ── 汇总 ─────────────────────────────────────────────────────────────


def read_active_key(engine_dir: Path, fb_lines) -> str:
    """当前生效客群：data/fb_active_persona_override.json 优先，否则 YAML default_persona。"""
    override = ""
    try:
        obj = json.loads((engine_dir / "data" / "fb_active_persona_override.json")
                         .read_text(encoding="utf-8-sig"))
        override = str((obj or {}).get("persona_key") or "").strip()
    except (OSError, ValueError):
        pass
    if override:
        return override
    if fb_lines:
        for ln in fb_lines:
            m = re.match(r"^default_persona:\s*(.+)$", ln)
            if m:
                return _clean_scalar(m.group(1)) or ""
    return ""


def collect(engine_dir: Path) -> list:
    if not engine_dir.is_dir():
        warn(f"数据源目录不存在：{engine_dir}（输出空 personas）")
        return []
    cfg = engine_dir / "config"
    fb_lines = read_lines(cfg / "fb_target_personas.yaml")
    chat_lines = read_lines(cfg / "chat_messages.yaml")
    pk_lines = read_lines(cfg / "persona_knowledge.yaml")
    studio_lines = read_lines(cfg / "personas.yaml")

    personas: list = []
    if fb_lines is None:
        warn(f"{cfg / 'fb_target_personas.yaml'} 缺失，目标画像家族输出为空")
    else:
        if chat_lines is None:
            warn(f"{cfg / 'chat_messages.yaml'} 缺失，prompt 槽将按缺席导出")
        active_key = read_active_key(engine_dir, fb_lines)
        db_meta = load_db_meta(engine_dir / "data" / "openclaw.db")
        personas += collect_fb_targets(engine_dir, fb_lines, chat_lines, pk_lines,
                                       active_key, db_meta)
    if studio_lines is None:
        warn(f"{cfg / 'personas.yaml'} 缺失，studio 家族输出为空")
    else:
        personas += collect_studio(studio_lines)
    if not personas:
        warn(f"{engine_dir} 下未采到任何人设（输出空 personas）")
    personas.sort(key=lambda p: (p["raw"].get("persona_family", ""), p["source_key"]))
    return personas


# ── 演示数据 ─────────────────────────────────────────────────────────


def demo_personas() -> list:
    """3 条演示数据（管道联调）：话术+画像齐全 / 无话术画像 / studio 内容人设。"""
    now = int(time.time())
    fp = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()  # noqa: E731
    return [
        make_persona(
            source_key="demo_jp_female", display_name="演示-日本中年女性画像",
            slots={
                "face": slot(False), "voice": slot(False),
                "prompt": slot(True, fingerprint=fp("demo-scripts-jp"),
                               ref="config/chat_messages.yaml#countries.jp"),
                "knowledge": slot(True, fingerprint=fp("demo-persona-block-jp"),
                                  ref="config/fb_target_personas.yaml"
                                      "#personas.demo_jp_female"),
            },
            tags=["fb_target", "active"], created_at=to_iso(now - 60 * 86400),
            raw={"storage": "demo", "demo": True, "persona_family": "fb_target",
                 "country_code": "JP", "language": "ja", "gender": "female",
                 "age_min": 37, "age_max": 60, "is_default": True,
                 "greeting_count": 8, "friend_note_count": 3, "variant_count": 2,
                 "l1_rules_count": 9, "l1_pass_threshold": 20,
                 "interest_topics_count": 10, "prompt_source": "countries.jp"},
        ),
        make_persona(
            source_key="demo_global_generic", display_name="演示-全球通用画像",
            slots={
                "face": slot(False), "voice": slot(False),
                "prompt": slot(False),
                "knowledge": slot(True, fingerprint=fp("demo-persona-block-global"),
                                  ref="config/fb_target_personas.yaml"
                                      "#personas.demo_global_generic"),
            },
            tags=["fb_target"], created_at=None,
            raw={"storage": "demo", "demo": True, "persona_family": "fb_target",
                 "language": "en", "interest_topics_count": 3,
                 "seed_group_keywords_count": 3},
        ),
        make_persona(
            source_key="studio:demo_lifestyle", display_name="演示-生活方式内容号",
            slots={
                "face": slot(False), "voice": slot(False),
                "prompt": slot(True, fingerprint=fp("demo-studio-block"),
                               ref="config/personas.yaml#personas.demo_lifestyle"),
                "knowledge": slot(False),
            },
            tags=["studio"], created_at=to_iso(now - 10 * 86400),
            raw={"storage": "demo", "demo": True, "persona_family": "studio",
                 "niche": "lifestyle_fitness", "language": "italian",
                 "tone": "energetic, motivational, friendly",
                 "content_themes_count": 5, "hashtags_count": 17,
                 "platforms": "tiktok,instagram,telegram"},
        ),
    ]


# ── 入口 ─────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="huoke 养号人设 → 人设总线归一化 JSON（只读）")
    ap.add_argument("--input", default="",
                    help=f"引擎根目录覆盖（默认 {DEFAULT_ENGINE_DIR}）")
    ap.add_argument("--out", default="huoke_personas.json",
                    help="输出 JSON 路径（默认 ./huoke_personas.json）")
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
            print(f"[export_huoke_personas] 错误: --out 不得位于被读目录内"
                  f"（{engine_dir}）", file=sys.stderr)
            return 2
        personas = collect(engine_dir)

    doc = {"version": 1, "source_system": SOURCE_SYSTEM,
           "exported_at": now_iso(), "personas": personas}
    payload = json.dumps(doc, ensure_ascii=False, indent=2)

    # 出厂自查（防泄漏最后一道闸）：base64 长串 + 手机号样式，命中即拒写
    text = json.dumps(personas, ensure_ascii=False)
    m = _LEAK_RE.search(text)
    if m:
        print("[export_huoke_personas] 错误: 导出内容含疑似 base64 长串，"
              f"已拒绝写出（片段头 {m.group(0)[:32]}…）", file=sys.stderr)
        return 3
    m = _PHONE_RE.search(text)
    if m:
        print("[export_huoke_personas] 错误: 导出内容含疑似国际手机号"
              f"（{m.group(0)[:4]}…），已拒绝写出", file=sys.stderr)
        return 3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n", encoding="utf-8")
    n_slots = {k: sum(1 for p in personas if p["slots"][k]["present"])
               for k in SLOT_KEYS}
    n_fb = sum(1 for p in personas if not p["source_key"].startswith(STUDIO_PREFIX))
    print(f"[export_huoke_personas] 已导出 {len(personas)} 条"
          f"（目标画像 {n_fb} + studio {len(personas) - n_fb}）→ {out_path}")
    print("[export_huoke_personas] 槽位分布: "
          + " ".join(f"{k}={v}" for k, v in n_slots.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
