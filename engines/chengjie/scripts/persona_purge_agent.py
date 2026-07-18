#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""persona_purge_agent.py — chengjie 人设全域清除执行器（P5，仅标准库）。

消费集团人设总线的机器通道（platform/identity/PERSONA_BUS.md §5，与 avatarhub 版同协议；
ack 端点按 website 上线版走同 URL POST——契约 §5.1 的 /ack 子路径在实现中被简化）：

    GET  <base>/api/sync/personas/purges?system=chengjie      （Bearer EVENT_INGEST_KEY）
    →  按指令删除本地人设资产（软删除：移入 config/purged_trash/<日期>/，保留回收期）
    →  POST <base>/api/sync/personas/purges  体 {purge_id, system, ok, completed_at, detail}
       （仅 --commit 且全部成功时；服务端只消费 purge_id/detail，其余为契约 §5.1 字段）

删除范围（严格限该 source_key＝人设 id，一个都不留，PERSONA_BUS.md §5.3）：
  声纹      config/voice_refs/<id>.<ext>（wav/mp3/m4a/ogg/flac + 同名 .txt 台词文本）；
            voice_profile.reference_audio_path 显式指向的文件（含同名 .txt 台词文本）——
            仅当其位于引擎根内且未被其他人设共用（共用→跳过并记 skipped，防误伤他人资产；
            引擎根外的路径不自动删，打印警告由运维手工处理）。
  人设配置  config/profiles_runtime.yaml 的 profiles.<id> 与 _history.<id> 原文块；
            config/personas.yaml 的 profiles.<id>（规范层，存在才处理）；
            config/bindings_runtime.yaml 中 id=<id> 的会话绑定（绑定内嵌人设卡快照）；
            config/persona_runtime.yaml（默认人设恰为该 id 时整文件）。
  专属知识  config/prerender_lines/<id>.txt（台词/话术库；共享 _common.txt 不动）。
  衍生缓存  assets/voices/<id>/（预渲染语音产物整目录）；
            config/persona_lora.json 的 <id> 项 + 其指向的引擎根内 LoRA 权重文件；
            persona_media.db 中 persona_id=<id> 的行（行先 dump 进回收站再删）及其媒体文件、
            assets/persona_media/<id>/ 与 src/web/static/persona_albums/<id>/ 整目录；
            deep_persona.db 的 persona_self_topics 中 persona_id=<id> 的行。

行为纪律：
  - 缺省 dry-run 演练：只打印将删清单，不动任何文件、不写 state、绝不 ack；
  - --commit 才真删（软删除进回收站）+ ack；删除有任何失败 → 报错**不 ack**（指令保留，
    已删的不回滚，下轮重试剩余项，PERSONA_BUS.md §5.3-4）；找不到的项记 missing，
    幂等视同已删，照常 ack；
  - ack detail 只放引擎根相对路径 / `库#表?键` 形式的资产引用，绝不放文件内容；
  - source_key 白名单校验（防路径穿越），全部文件操作钉死在引擎根内；回收站与 state
    文件已加 .gitignore（含显示名/路径的经营数据，不入库）。

用法（cron/计划任务每 5–15 分钟一次，与 uploader 同 cadence）::

    python scripts/persona_purge_agent.py                       # dry-run 演练（默认）
    python scripts/persona_purge_agent.py --commit --once       # 真删 + ack，单轮
    python scripts/persona_purge_agent.py --commit --loop 600   # 每 600s 轮询一轮
    python scripts/persona_purge_agent.py --selftest            # 本地 mock 自测（不连外网）

--base 缺省 env PERSONA_SYNC_BASE 或 https://bd2026.cc；--key 缺省 env EVENT_INGEST_KEY；
--input 引擎根覆盖（默认本文件上级目录）；--state-file 缺省 config/purge_agent_state.json。
"""
from __future__ import annotations

import argparse
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

try:  # GBK 控制台防中文炸 print（与 scripts/ledger_outbox.py 同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SYSTEM = "chengjie"
BASE_ENV = "PERSONA_SYNC_BASE"
KEY_ENV = "EVENT_INGEST_KEY"
DEFAULT_BASE = "https://bd2026.cc"
DEFAULT_ENGINE_ROOT = Path(__file__).resolve().parents[1]
STATE_REL = Path("config") / "purge_agent_state.json"
TRASH_REL = Path("config") / "purged_trash"
TIMEOUT_S = 10
VOICE_REF_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac")   # 引擎 discover_reference_audio 同款
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")  # source_key 白名单（防穿越）


def log(msg: str) -> None:
    print(f"[persona_purge_agent] {msg}")


def warn(msg: str) -> None:
    print(f"[persona_purge_agent] 警告: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── HTTP（机器通道，Bearer EVENT_INGEST_KEY）─────────────────────────


class SyncError(RuntimeError):
    """机器通道错误（网络 / 4xx / 5xx）：本轮终止，指令保留下轮重试。"""


def _http_json(url: str, key: str, *, method: str = "GET", body=None,
               timeout: float = TIMEOUT_S) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise SyncError(f"HTTP {exc.code} {method} {url}: {detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SyncError(f"{method} {url} 失败: {exc}") from None


def fetch_purges(base: str, key: str, timeout: float = TIMEOUT_S) -> list:
    url = (base.rstrip("/") + "/api/sync/personas/purges?"
           + urllib.parse.urlencode({"system": SYSTEM}))
    doc = _http_json(url, key, timeout=timeout)
    purges = doc.get("purges")
    return purges if isinstance(purges, list) else []


def post_ack(base: str, key: str, purge_id, detail: dict,
             timeout: float = TIMEOUT_S) -> dict:
    # 上线版 ack＝同 URL POST（与 avatarhub 版一致）；body 带齐契约 §5.1 字段，
    # 服务端只消费 purge_id/detail，多余字段按契约"未知字段保留不报错"。
    url = base.rstrip("/") + "/api/sync/personas/purges"
    body = {"purge_id": purge_id, "system": SYSTEM, "ok": True,
            "completed_at": now_iso(), "detail": detail}
    return _http_json(url, key, method="POST", body=body, timeout=timeout)


# ── YAML 原文块手术（无损切分，仅标准库；与导出器块扫描同构）───────────

_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
_CHILD_KEY_RE = re.compile(r"^  (['\"]?)([^\s:#'\"][^:'\"]*)\1:\s*(?:#.*)?$")


def _split_top(text: str) -> list:
    """→ [(top_key|None, 原文段)]，"".join(段) == 原文（无损）。"""
    segs, cur_key, cur = [], None, []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        m = _TOP_KEY_RE.match(bare) if (bare and not bare[0].isspace()) else None
        if m:
            if cur:
                segs.append((cur_key, "".join(cur)))
            cur_key, cur = m.group(1), [line]
        else:
            if cur_key is None and not cur:
                cur = [line]
            else:
                cur.append(line)
    if cur:
        segs.append((cur_key, "".join(cur)))
    return segs


def _split_children(section: str) -> list:
    """段内 2 空格缩进子块 → [(child_key|None, 原文块)]，无损。首元素为段头行。"""
    lines = section.splitlines(keepends=True)
    if not lines:
        return []
    segs, cur_key, cur = [(None, lines[0])], None, []
    for line in lines[1:]:
        m = _CHILD_KEY_RE.match(line.rstrip("\r\n"))
        if m:
            if cur:
                segs.append((cur_key, "".join(cur)))
            cur_key, cur = m.group(2).strip(), [line]
        else:
            if cur_key is None and not cur:
                segs.append((None, line))
            else:
                cur.append(line)
    if cur:
        segs.append((cur_key, "".join(cur)))
    return segs


def _block_field(block: str, key: str, indent: int) -> str:
    m = re.search(rf"^ {{{indent}}}{re.escape(key)}:[ \t]*(\S.*)$", block, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v.strip()


def remove_yaml_children(text: str, top_key: str, match) -> "tuple[str, dict]":
    """把 ``top_key`` 段内满足 ``match(child_key, block)`` 的子块整块移除。

    → (新文本, {child_key: 被移除原文块})。其余字节逐一保留（无损手术）。
    """
    removed: dict = {}
    out_segs = []
    for key, seg in _split_top(text):
        if key != top_key:
            out_segs.append(seg)
            continue
        kept = []
        for ckey, block in _split_children(seg):
            if ckey is not None and match(ckey, block):
                removed[ckey] = block
            else:
                kept.append(block)
        out_segs.append("".join(kept))
    return "".join(out_segs), removed


def atomic_write_text(path: Path, text: str) -> None:
    """utf-8 原样行尾原子写（tmp + replace），崩溃不留半个文件。"""
    tmp = path.with_suffix(path.suffix + ".purge_tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── 清除计划与执行（软删除进回收站）───────────────────────────────────


class PurgeRun:
    """单条清除指令在本引擎的执行现场。

    dry-run：只收集"将删清单"（planned），不动任何文件；
    commit：逐项软删除（移入回收站），任何失败进 errors → 调用方不得 ack。
    """

    def __init__(self, engine_root: Path, source_key: str, *,
                 commit: bool, trash_root: "Path | None" = None):
        self.root = engine_root.resolve()
        self.key = source_key
        self.commit = commit
        self.trash_dir = ((trash_root or (self.root / TRASH_REL))
                          / time.strftime("%Y%m%d") / source_key)
        self.deleted: list = []    # 已删（dry-run 下＝将删）资产引用
        self.missing: list = []    # 找不到＝早已不在（幂等视同已删）
        self.errors: list = []     # 失败项 + 原因（非空 → 不 ack）
        self.skipped: list = []    # 主动跳过（共用资产/根外路径），人工核查线索

    # — 基础动作 ——————————————————————————————————————————

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _inside_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _trash_dest(self, rel: str) -> Path:
        dest = self.trash_dir / rel
        if dest.exists():   # 同名冲突（重试残留）：追加时间后缀，绝不覆盖回收站
            dest = dest.with_name(dest.name + f".{int(time.time())}")
        return dest

    def trash_path(self, path: Path, ref: "str | None" = None) -> bool:
        """文件/目录 → 回收站（软删除）。不存在 → missing；失败 → errors。"""
        ref = ref or self._rel(path)
        if not path.exists():
            self.missing.append(ref)
            return False
        if not self._inside_root(path):
            self.skipped.append(f"{ref} (outside_root)")
            warn(f"{self.key}: {ref} 位于引擎根外，不自动删除，请人工处理")
            return False
        if not self.commit:
            self.deleted.append(ref)
            return True
        try:
            dest = self._trash_dest(self._rel(path))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            self.deleted.append(ref)
            return True
        except OSError as e:
            self.errors.append(f"{ref}: {e}")
            return False

    def trash_blob(self, rel_name: str, content: str, ref: str) -> bool:
        """被移除的配置块/DB 行 dump → 回收站文本（软删除的"删除物"留档）。"""
        if not self.commit:
            return True
        try:
            dest = self._trash_dest(rel_name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            return True
        except OSError as e:
            self.errors.append(f"{ref}: 回收站写入失败 {e}")
            return False

    # — 声纹参考音 ————————————————————————————————————————

    def _other_voice_refs(self) -> set:
        """其他人设显式引用的参考音（resolve 后绝对路径集合），防共用误伤。"""
        refs: set = set()
        for fname in ("profiles_runtime.yaml", "personas.yaml"):
            p = self.root / "config" / fname
            try:
                text = p.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            for key, seg in _split_top(text):
                if key != "profiles":
                    continue
                for ckey, block in _split_children(seg):
                    if ckey is None or ckey == self.key:
                        continue
                    ref = _block_field(block, "reference_audio_path", 6)
                    if ref:
                        q = Path(ref)
                        refs.add(((self.root / q) if not q.is_absolute() else q)
                                 .resolve())
        return refs

    def purge_voice(self) -> None:
        vr = self.root / "config" / "voice_refs"
        hit = False
        for ext in VOICE_REF_EXTS + (".txt",):   # 约定命名 = 该人设专属，连台词文本一起
            p = vr / f"{self.key}{ext}"
            if p.exists():
                hit = True
                self.trash_path(p)
        # 显式配置的 reference_audio_path（可能与约定命名不同）
        cfg_ref = ""
        p = self.root / "config" / "profiles_runtime.yaml"
        try:
            for key, seg in _split_top(p.read_text(encoding="utf-8-sig")):
                if key != "profiles":
                    continue
                for ckey, block in _split_children(seg):
                    if ckey == self.key:
                        cfg_ref = _block_field(block, "reference_audio_path", 6)
        except OSError:
            pass
        if cfg_ref:
            q = Path(cfg_ref)
            audio = (self.root / q) if not q.is_absolute() else q
            if audio.resolve() not in {(vr / f"{self.key}{e}").resolve()
                                       for e in VOICE_REF_EXTS}:
                if not self._inside_root(audio):
                    self.skipped.append(f"{cfg_ref} (outside_root)")
                    warn(f"{self.key}: 参考音 {cfg_ref} 位于引擎根外，请人工处理")
                elif audio.resolve() in self._other_voice_refs():
                    self.skipped.append(f"{self._rel(audio)} (shared)")
                    log(f"{self.key}: 参考音 {self._rel(audio)} 被其他人设共用，跳过"
                        "（资产债请照 persona_asset_lint 治理）")
                else:
                    hit = True
                    if audio.exists():
                        self.trash_path(audio)
                        sidecar = audio.with_suffix(".txt")
                        if sidecar.exists():
                            self.trash_path(sidecar)
                    else:
                        self.missing.append(self._rel(audio))
        if not hit and not cfg_ref:
            self.missing.append(f"config/voice_refs/{self.key}.*")

    # — 人设配置项（YAML 块手术）—————————————————————————————

    def _purge_yaml_blocks(self, fname: str, top_key: str, match, ref_fmt) -> None:
        path = self.root / "config" / fname
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return   # 文件不存在＝无此层配置，正常
        new_text, removed = remove_yaml_children(text, top_key, match)
        if not removed:
            self.missing.append(ref_fmt(self.key))
            return
        for ckey, block in removed.items():
            ref = ref_fmt(ckey)
            if not self.commit:
                self.deleted.append(ref)
                continue
            if not self.trash_blob(
                    f"config/{fname}#{top_key}.{ckey}.yaml", block, ref):
                return   # 回收站写不进 → 不改文件（errors 已记）
        if self.commit:
            try:
                atomic_write_text(path, new_text)
                self.deleted.extend(ref_fmt(k) for k in removed)
            except OSError as e:
                self.errors.append(f"config/{fname}: 重写失败 {e}")

    def purge_profile_entries(self) -> None:
        me = lambda ckey, _b: ckey == self.key                     # noqa: E731
        self._purge_yaml_blocks(
            "profiles_runtime.yaml", "profiles", me,
            lambda k: f"config/profiles_runtime.yaml#profiles.{k}")
        # 版本历史块（存在才算 missing 之外的事——历史缺席不记 missing）
        path = self.root / "config" / "profiles_runtime.yaml"
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            text = ""
        if re.search(rf"^  {re.escape(self.key)}:", text, re.M):
            new_text, removed = remove_yaml_children(text, "_history", me)
            if removed:
                ref = f"config/profiles_runtime.yaml#_history.{self.key}"
                if not self.commit:
                    self.deleted.append(ref)
                elif self.trash_blob(
                        f"config/profiles_runtime.yaml#_history.{self.key}.yaml",
                        removed[self.key], ref):
                    try:
                        atomic_write_text(path, new_text)
                        self.deleted.append(ref)
                    except OSError as e:
                        self.errors.append(f"{ref}: 重写失败 {e}")
        if (self.root / "config" / "personas.yaml").is_file():
            self._purge_yaml_blocks(
                "personas.yaml", "profiles", me,
                lambda k: f"config/personas.yaml#profiles.{k}")

    def purge_bindings(self) -> None:
        """会话绑定内嵌人设卡快照（bindings.<chat_id>.id == source_key）→ 整块移除。"""
        path = self.root / "config" / "bindings_runtime.yaml"
        if not path.is_file():
            return
        self._purge_yaml_blocks(
            "bindings_runtime.yaml", "bindings",
            lambda _ck, block: _block_field(block, "id", 4) == self.key,
            lambda k: f"config/bindings_runtime.yaml#bindings.{k}")
        # 该 match 无命中时会记一条 missing（键为 source_key 形式）——绑定缺席属正常，撤掉它
        tail = f"config/bindings_runtime.yaml#bindings.{self.key}"
        if tail in self.missing:
            self.missing.remove(tail)

    def purge_default_persona(self) -> None:
        """persona_runtime.yaml 默认人设恰为该 id → 整文件进回收站。"""
        path = self.root / "config" / "persona_runtime.yaml"
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return
        for key, seg in _split_top(text):
            if key == "default_persona" and _block_field(seg, "id", 2) == self.key:
                self.trash_path(path, "config/persona_runtime.yaml#default_persona")
                return

    # — 专属知识 / 衍生缓存 ————————————————————————————————

    def purge_knowledge(self) -> None:
        p = self.root / "config" / "prerender_lines" / f"{self.key}.txt"
        if p.exists():
            self.trash_path(p)
        else:
            self.missing.append(f"config/prerender_lines/{self.key}.txt")

    def purge_prerendered(self) -> None:
        d = self.root / "assets" / "voices" / self.key
        if d.exists():
            self.trash_path(d)

    def purge_lora(self) -> None:
        path = self.root / "config" / "persona_lora.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or self.key not in data:
            return
        entry = data.get(self.key) or {}
        ref = f"config/persona_lora.json#{self.key}"
        lora_file = str(entry.get("file") or "").strip() if isinstance(entry, dict) else ""
        if not self.commit:
            self.deleted.append(ref)
            if lora_file:
                q = Path(lora_file)
                self.trash_path((self.root / q) if not q.is_absolute() else q)
            return
        if not self.trash_blob(f"config/persona_lora.json#{self.key}.json",
                               json.dumps({self.key: entry}, ensure_ascii=False,
                                          indent=2), ref):
            return
        try:
            data.pop(self.key, None)
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
            self.deleted.append(ref)
        except OSError as e:
            self.errors.append(f"{ref}: 重写失败 {e}")
            return
        if lora_file:
            q = Path(lora_file)
            self.trash_path((self.root / q) if not q.is_absolute() else q)

    def purge_media(self) -> None:
        """persona_media.db 行（先 dump 后删）+ 媒体文件 + 每人设媒体目录。"""
        db = self.root / "config" / "persona_media.db"
        rows: list = []
        if db.is_file():
            try:
                conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
                try:
                    cur = conn.execute(
                        "SELECT * FROM persona_media WHERE persona_id=?", (self.key,))
                    cols = [c[0] for c in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                finally:
                    conn.close()
            except sqlite3.Error as e:
                self.errors.append(f"config/persona_media.db: 读取失败 {e}")
                return
        ref = f"config/persona_media.db#persona_media?persona_id={self.key}"
        if rows:
            for r in rows:
                fp = str(r.get("file_path") or "").strip()
                if fp:
                    q = Path(fp)
                    self.trash_path((self.root / q) if not q.is_absolute() else q)
            if not self.commit:
                self.deleted.append(f"{ref} ({len(rows)} rows)")
            elif self.trash_blob(f"config/persona_media.db#{self.key}.rows.json",
                                 json.dumps(rows, ensure_ascii=False, indent=2,
                                            default=str), ref):
                try:
                    conn = sqlite3.connect(str(db), timeout=5)
                    try:
                        conn.execute("DELETE FROM persona_media WHERE persona_id=?",
                                     (self.key,))
                        conn.commit()
                    finally:
                        conn.close()
                    self.deleted.append(f"{ref} ({len(rows)} rows)")
                except sqlite3.Error as e:
                    self.errors.append(f"{ref}: 删除失败 {e}")
        for d in (self.root / "assets" / "persona_media" / self.key,
                  self.root / "src" / "web" / "static" / "persona_albums" / self.key):
            if d.exists():
                self.trash_path(d)

    def purge_self_topics(self) -> None:
        db = self.root / "config" / "deep_persona.db"
        if not db.is_file():
            return
        ref = f"config/deep_persona.db#persona_self_topics?persona_id={self.key}"
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                try:
                    cur = conn.execute(
                        "SELECT * FROM persona_self_topics WHERE persona_id=?",
                        (self.key,))
                    cols = [c[0] for c in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                except sqlite3.OperationalError:
                    return   # 表还没建过（功能未开）——无资产
            finally:
                conn.close()
        except sqlite3.Error:
            return
        if not rows:
            return
        if not self.commit:
            self.deleted.append(f"{ref} ({len(rows)} rows)")
            return
        if not self.trash_blob(f"config/deep_persona.db#{self.key}.self_topics.json",
                               json.dumps(rows, ensure_ascii=False, indent=2,
                                          default=str), ref):
            return
        try:
            conn = sqlite3.connect(str(db), timeout=5)
            try:
                conn.execute("DELETE FROM persona_self_topics WHERE persona_id=?",
                             (self.key,))
                conn.commit()
            finally:
                conn.close()
            self.deleted.append(f"{ref} ({len(rows)} rows)")
        except sqlite3.Error as e:
            self.errors.append(f"{ref}: 删除失败 {e}")

    # — 执行 ————————————————————————————————————————————

    def run(self) -> dict:
        if not _KEY_RE.match(self.key):
            self.errors.append(f"source_key 非法（拒绝执行）: {self.key!r}")
        else:
            self.purge_voice()
            self.purge_prerendered()
            self.purge_knowledge()
            self.purge_media()
            self.purge_lora()
            self.purge_self_topics()
            # 配置项最后删：中途失败时下一轮仍能从配置重推导剩余资产
            self.purge_bindings()
            self.purge_default_persona()
            self.purge_profile_entries()
        detail: dict = {"deleted": self.deleted, "missing": self.missing,
                        "errors": self.errors}
        if self.skipped:
            detail["skipped"] = self.skipped
        return detail


# ── state 文件（观测用；幂等不依赖它）─────────────────────────────────


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    state = dict(state)
    state["note"] = ("persona_purge_agent 执行记录（观测用）。ack 幂等由集团侧保证，"
                     "删除本文件不影响正确性。")
    state["updated"] = now_iso()
    acked = state.get("acked")
    if isinstance(acked, dict) and len(acked) > 200:   # 防无限膨胀：留最近 200 条
        keep = sorted(acked, key=lambda k: str(acked[k].get("completed_at") or ""))
        for k in keep[:-200]:
            acked.pop(k, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ── 单轮主流程 ────────────────────────────────────────────────────────


def run_pass(*, base: str, key: str, engine_root: Path, commit: bool,
             state_file: Path, timeout: float = TIMEOUT_S,
             trash_root: "Path | None" = None) -> dict:
    """拉取 → 逐条执行 →（commit 且零失败时）ack。→ 摘要 dict（ok=本轮无失败）。"""
    summary = {"ok": True, "mode": "commit" if commit else "dry-run",
               "pending": 0, "acked": 0, "failed": 0, "error": None}
    if not (engine_root / "config").is_dir():
        summary["ok"] = False
        summary["error"] = f"引擎根目录不像 chengjie（缺 config/）：{engine_root}"
        warn(summary["error"])
        return summary
    try:
        purges = fetch_purges(base, key, timeout=timeout)
    except SyncError as e:
        summary["ok"] = False
        summary["error"] = str(e)
        warn(f"拉取清除指令失败：{e}")
        return summary
    summary["pending"] = len(purges)
    if not purges:
        log(f"无待办清除指令（{summary['mode']}）")
        return summary

    state = load_state(state_file) if commit else {}
    acked_map = state.setdefault("acked", {}) if commit else {}

    for p in purges:
        if not isinstance(p, dict):
            continue
        purge_id = p.get("purge_id")
        source_key = str(p.get("source_key") or "").strip()
        if str(p.get("source_system") or SYSTEM) != SYSTEM:
            warn(f"指令 {purge_id} 的 source_system={p.get('source_system')!r} "
                 "非本引擎，跳过")
            continue
        log(f"指令 purge_id={purge_id} source_key={source_key}"
            f"（{summary['mode']}）")
        run = PurgeRun(engine_root, source_key, commit=commit,
                       trash_root=trash_root)
        detail = run.run()
        verb = "已删" if commit else "将删"
        for ref in detail["deleted"]:
            log(f"  {verb}: {ref}")
        for ref in detail["missing"]:
            log(f"  缺席(视同已删): {ref}")
        for ref in detail.get("skipped", []):
            log(f"  跳过: {ref}")
        for msg in detail["errors"]:
            warn(f"  失败: {msg}")

        if detail["errors"]:
            summary["failed"] += 1
            summary["ok"] = False
            warn(f"指令 {purge_id} 有失败项，不 ack（已删的不回滚，下轮重试剩余项）")
            continue
        if not commit:
            log(f"  dry-run：不删除、不 ack（--commit 才执行）")
            continue
        try:
            resp = post_ack(base, key, purge_id, detail, timeout=timeout)
        except SyncError as e:
            summary["failed"] += 1
            summary["ok"] = False
            warn(f"指令 {purge_id} ack 失败：{e}（资产已入回收站，下轮重 ack）")
            continue
        summary["acked"] += 1
        acked_map[str(purge_id)] = {
            "source_key": source_key, "completed_at": now_iso(),
            "deleted": len(detail["deleted"]), "missing": len(detail["missing"]),
            "already": bool(resp.get("already")),
        }
        log(f"  ack ok（persona_status={resp.get('persona_status')}）")

    if commit:
        state["last_poll"] = now_iso()
        try:
            save_state(state_file, state)
        except OSError as e:
            warn(f"state 文件写入失败（不影响幂等）：{e}")
    return summary


# ── 自测：mock 机器通道 + 临时人设资产 ────────────────────────────────


def _selftest() -> int:
    import http.server
    import tempfile
    import threading

    failures: list = []

    def check(desc: str, ok) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    KEY = "selftest-key"

    class MockBus(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self):
            super().__init__(("127.0.0.1", 0), MockHandler)
            self.lock = threading.Lock()
            self.pending: list = []
            self.acks: list = []

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):   # 静音
            pass

        def _send(self, status: int, obj: dict) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {KEY}"

        def do_GET(self):
            srv: MockBus = self.server  # type: ignore[assignment]
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            if self.path != f"/api/sync/personas/purges?system={SYSTEM}":
                self._send(404, {"error": "not_found", "path": self.path})
                return
            with srv.lock:
                self._send(200, {"ok": True, "system": SYSTEM,
                                 "count": len(srv.pending), "purges": srv.pending})

        def do_POST(self):
            srv: MockBus = self.server  # type: ignore[assignment]
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            if self.path != "/api/sync/personas/purges":   # 上线版 ack＝同 URL POST
                self._send(404, {"error": "not_found", "path": self.path})
                return
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            with srv.lock:
                srv.acks.append(body)
                srv.pending = [p for p in srv.pending
                               if p["purge_id"] != body.get("purge_id")]
                self._send(200, {"ok": True, "purge_id": body.get("purge_id"),
                                 "already": False, "all_acked": True,
                                 "persona_status": "purged"})

    def build_engine(root: Path) -> None:
        cfg = root / "config"
        (cfg / "voice_refs").mkdir(parents=True)
        (cfg / "prerender_lines").mkdir()
        profiles = (
            "profiles:\n"
            "  purge_me:\n"
            "    id: purge_me\n"
            "    name: 清除对象\n"
            "    voice_profile:\n"
            "      enabled: true\n"
            "      reference_audio_path: config/voice_refs/shared_voice.wav\n"
            "  keep_me:\n"
            "    id: keep_me\n"
            "    name: 保留对象\n"
            "    voice_profile:\n"
            "      enabled: true\n"
            "      reference_audio_path: config/voice_refs/keep_me.wav\n"
            "  borrower:\n"
            "    id: borrower\n"
            "    voice_profile:\n"
            "      enabled: true\n"
            "      reference_audio_path: config/voice_refs/shared_voice.wav\n"
            "_history:\n"
            "  purge_me:\n"
            "  - ts: '2026-07-01T00:00:00Z'\n"
            "    persona:\n"
            "      id: purge_me\n"
            "updated_at: '2026-07-18T00:00:00Z'\n")
        (cfg / "profiles_runtime.yaml").write_text(profiles, encoding="utf-8")
        (cfg / "bindings_runtime.yaml").write_text(
            "bindings:\n"
            "  '111':\n"
            "    id: purge_me\n"
            "    name: 清除对象\n"
            "  '222':\n"
            "    id: keep_me\n", encoding="utf-8")
        for name in ("purge_me.wav", "purge_me.txt", "keep_me.wav",
                     "shared_voice.wav"):
            (cfg / "voice_refs" / name).write_bytes(b"RIFFfake" + name.encode())
        (cfg / "prerender_lines" / "purge_me.txt").write_text("你好\n", encoding="utf-8")
        (cfg / "prerender_lines" / "_common.txt").write_text("在的\n", encoding="utf-8")
        pre = root / "assets" / "voices" / "purge_me" / "prerendered"
        pre.mkdir(parents=True)
        (pre / "hello.ogg").write_bytes(b"OggSfake")
        (cfg / "persona_lora.json").write_text(json.dumps({
            "purge_me": {"file": "datasets/purge_me/lora.safetensors",
                         "trigger": "pm", "weight": 0.9},
            "keep_me": {"file": "datasets/keep_me/lora.safetensors",
                        "trigger": "km", "weight": 0.9}}), encoding="utf-8")
        lora = root / "datasets" / "purge_me"
        lora.mkdir(parents=True)
        (lora / "lora.safetensors").write_bytes(b"\x00weights")
        media_dir = root / "assets" / "persona_media" / "purge_me"
        media_dir.mkdir(parents=True)
        (media_dir / "a.jpg").write_bytes(b"\xff\xd8fakejpg")
        conn = sqlite3.connect(str(cfg / "persona_media.db"))
        conn.execute("CREATE TABLE persona_media (id TEXT PRIMARY KEY, "
                     "persona_id TEXT, media_type TEXT, file_path TEXT, "
                     "sha256 TEXT, enabled INTEGER, created_at REAL)")
        conn.execute("INSERT INTO persona_media VALUES "
                     "('m1','purge_me','photo','assets/persona_media/purge_me/a.jpg',"
                     "'', 1, 1.0)")
        conn.execute("INSERT INTO persona_media VALUES "
                     "('m2','keep_me','photo','assets/persona_media/keep_me/b.jpg',"
                     "'', 1, 2.0)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(str(cfg / "deep_persona.db"))
        conn.execute("CREATE TABLE persona_self_topics (persona_id TEXT, "
                     "topic TEXT, count INTEGER, last_ts REAL)")
        conn.execute("INSERT INTO persona_self_topics VALUES ('purge_me','大阪',3,1.0)")
        conn.execute("INSERT INTO persona_self_topics VALUES ('keep_me','抹茶',2,1.0)")
        conn.commit()
        conn.close()

    print("== persona_purge_agent 自测（--selftest，本地 mock 不连外网）==")
    server = MockBus()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        with tempfile.TemporaryDirectory(prefix="cj_purge_selftest_") as tmp:
            root = Path(tmp) / "engine"
            build_engine(root)
            state_file = root / "config" / "purge_agent_state.json"
            trash_root = root / TRASH_REL
            directive = {"purge_id": 101, "persona_id": "prs_test",
                         "source_system": SYSTEM, "source_key": "purge_me",
                         "requested_at": now_iso(),
                         "slots": {"face": True, "voice": True,
                                   "prompt": True, "knowledge": True}}
            server.pending = [dict(directive)]
            wav = root / "config" / "voice_refs" / "purge_me.wav"
            yaml_path = root / "config" / "profiles_runtime.yaml"

            print("[1/6] dry-run：只打印将删清单，不删不 ack、不写 state")
            s = run_pass(base=base, key=KEY, engine_root=root, commit=False,
                         state_file=state_file, trash_root=trash_root)
            check("dry-run ok 且 1 条待办", s["ok"] and s["pending"] == 1)
            check("未 ack（mock 仍挂起）",
                  not server.acks and len(server.pending) == 1)
            check("资产原封未动（wav/yaml/台词/媒体都在）",
                  wav.is_file()
                  and "purge_me:" in yaml_path.read_text(encoding="utf-8")
                  and (root / "config" / "prerender_lines" / "purge_me.txt").is_file()
                  and (root / "assets" / "persona_media" / "purge_me" / "a.jpg").is_file())
            check("state 未写、回收站未建",
                  not state_file.exists() and not trash_root.exists())

            print("[2/6] commit：软删除进回收站 + ack")
            s = run_pass(base=base, key=KEY, engine_root=root, commit=True,
                         state_file=state_file, trash_root=trash_root)
            check("commit ok 且 ack 1 条", s["ok"] and s["acked"] == 1)
            check("mock 收到 ack（ok=true 且 detail.deleted 非空）",
                  len(server.acks) == 1 and server.acks[0]["ok"] is True
                  and server.acks[0]["purge_id"] == 101
                  and server.acks[0]["detail"]["deleted"]
                  and not server.acks[0]["detail"]["errors"])
            trashed = list(trash_root.rglob("*"))
            check("voice_refs 音频+台词文本已入回收站（原位已无）",
                  not wav.exists()
                  and any(p.name == "purge_me.wav" for p in trashed)
                  and any(p.name == "purge_me.txt" for p in trashed))
            check("共用参考音 shared_voice.wav 未被误删（skipped 记录）",
                  (root / "config" / "voice_refs" / "shared_voice.wav").is_file()
                  and any("(shared)" in x for x in
                          server.acks[0]["detail"].get("skipped", [])))
            text = yaml_path.read_text(encoding="utf-8")
            check("profiles_runtime.yaml：purge_me 块（含 _history）已摘除，"
                  "keep_me/borrower 保留",
                  "purge_me:" not in text and "keep_me:" in text
                  and "borrower:" in text and "updated_at:" in text)
            bind = (root / "config" / "bindings_runtime.yaml").read_text(encoding="utf-8")
            check("绑定快照 '111' 已摘除，'222' 保留",
                  "'111'" not in bind and "'222'" in bind)
            check("专属台词库已删，_common.txt 保留",
                  not (root / "config" / "prerender_lines" / "purge_me.txt").exists()
                  and (root / "config" / "prerender_lines" / "_common.txt").is_file())
            check("预渲染目录 assets/voices/purge_me 已入回收站",
                  not (root / "assets" / "voices" / "purge_me").exists())
            conn = sqlite3.connect(str(root / "config" / "persona_media.db"))
            media_rows = conn.execute(
                "SELECT persona_id, COUNT(*) FROM persona_media GROUP BY persona_id"
            ).fetchall()
            conn.close()
            check("persona_media.db：purge_me 行已删、keep_me 保留，行 dump 在回收站",
                  media_rows == [("keep_me", 1)]
                  and not (root / "assets" / "persona_media" / "purge_me").exists()
                  and any(p.name.endswith(".rows.json") for p in trashed))
            lora = json.loads((root / "config" / "persona_lora.json")
                              .read_text(encoding="utf-8"))
            check("persona_lora.json：purge_me 项已摘除 + 权重文件入回收站，keep_me 保留",
                  "purge_me" not in lora and "keep_me" in lora
                  and not (root / "datasets" / "purge_me" / "lora.safetensors").exists())
            conn = sqlite3.connect(str(root / "config" / "deep_persona.db"))
            topics = conn.execute(
                "SELECT persona_id FROM persona_self_topics").fetchall()
            conn.close()
            check("deep_persona.db 自身见闻：purge_me 行已删、keep_me 保留",
                  topics == [("keep_me",)])
            st = load_state(state_file)
            check("state 已记录该 ack", "101" in (st.get("acked") or {}))

            print("[3/6] 幂等：重复同指令再 commit → 全 missing 照常 ack")
            server.pending = [dict(directive, purge_id=102)]
            s = run_pass(base=base, key=KEY, engine_root=root, commit=True,
                         state_file=state_file, trash_root=trash_root)
            check("重复执行 ok（幂等，无失败）", s["ok"] and s["acked"] == 1)
            d = server.acks[-1]["detail"]
            check("重复执行 detail：deleted 空、missing 非空、errors 空",
                  not d["deleted"] and d["missing"] and not d["errors"])

            print("[4/6] 删除失败 → 报错不 ack、指令保留")
            server.pending = [{"purge_id": 103, "source_system": SYSTEM,
                               "source_key": "keep_me", "requested_at": now_iso(),
                               "slots": {"voice": True, "prompt": True}}]
            date_dir = trash_root / time.strftime("%Y%m%d")
            sabotage = date_dir / "keep_me"
            sabotage.write_text("挡路文件：使回收站建目录失败", encoding="utf-8")
            n_acks = len(server.acks)
            s = run_pass(base=base, key=KEY, engine_root=root, commit=True,
                         state_file=state_file, trash_root=trash_root)
            check("失败轮 ok=False 且 failed=1", not s["ok"] and s["failed"] == 1)
            check("未 ack、指令仍挂起",
                  len(server.acks) == n_acks and len(server.pending) == 1)
            check("keep_me 资产原封未动（失败不半删配置）",
                  (root / "config" / "voice_refs" / "keep_me.wav").is_file()
                  and "keep_me:" in yaml_path.read_text(encoding="utf-8"))
            sabotage.unlink()
            s = run_pass(base=base, key=KEY, engine_root=root, commit=True,
                         state_file=state_file, trash_root=trash_root)
            check("排障后重试成功 ack", s["ok"] and s["acked"] == 1
                  and not server.pending)

            print("[5/6] source_key 白名单：路径穿越指令拒绝执行、不 ack")
            server.pending = [{"purge_id": 104, "source_system": SYSTEM,
                               "source_key": "../evil", "requested_at": now_iso(),
                               "slots": {}}]
            n_acks = len(server.acks)
            s = run_pass(base=base, key=KEY, engine_root=root, commit=True,
                         state_file=state_file, trash_root=trash_root)
            check("非法 source_key → failed 且不 ack",
                  not s["ok"] and s["failed"] == 1 and len(server.acks) == n_acks)
            server.pending = []

            print("[6/6] 错误密钥：401 → 本轮终止不崩溃")
            s = run_pass(base=base, key="wrong-key", engine_root=root, commit=True,
                         state_file=state_file, trash_root=trash_root)
            check("ok=False 且错误含 401", not s["ok"] and "401" in (s["error"] or ""))
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="chengjie 人设全域清除执行器（轮询集团机器通道；缺省 dry-run 演练）")
    ap.add_argument("--base", default="",
                    help=f"集团 API 基址（默认 env {BASE_ENV} 或 {DEFAULT_BASE}）")
    ap.add_argument("--key", default="",
                    help=f"机器密钥（默认 env {KEY_ENV}）")
    ap.add_argument("--input", default="",
                    help=f"引擎根目录覆盖（默认 {DEFAULT_ENGINE_ROOT}）")
    ap.add_argument("--commit", action="store_true",
                    help="真删（软删除进回收站）+ ack；缺省为 dry-run 只打印将删清单")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="单轮执行（默认）")
    g.add_argument("--loop", type=int, metavar="N",
                   help="常驻轮询：每 N 秒一轮（契约建议 300–900，上限 3600）")
    ap.add_argument("--state-file", default="",
                    help=f"执行记录文件（默认 <引擎根>/{STATE_REL.as_posix()}）")
    ap.add_argument("--selftest", action="store_true",
                    help="mock 机器通道 + 临时人设资产自测（不连外网、不碰真实数据）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    base = args.base or os.environ.get(BASE_ENV, "").strip() or DEFAULT_BASE
    key = args.key or os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"[persona_purge_agent] 错误: --key 必填（或设置环境变量 {KEY_ENV}）",
              file=sys.stderr)
        return 2
    engine_root = Path(args.input).resolve() if args.input else DEFAULT_ENGINE_ROOT
    state_file = (Path(args.state_file).resolve() if args.state_file
                  else engine_root / STATE_REL)

    if args.loop:
        interval = max(30, min(int(args.loop), 3600))
        log(f"常驻轮询：每 {interval}s 一轮（Ctrl+C 退出；"
            f"mode={'commit' if args.commit else 'dry-run'}）")
        try:
            while True:
                run_pass(base=base, key=key, engine_root=engine_root,
                         commit=args.commit, state_file=state_file)
                time.sleep(interval)
        except KeyboardInterrupt:
            log("收到中断，退出")
            return 0
    summary = run_pass(base=base, key=key, engine_root=engine_root,
                       commit=args.commit, state_file=state_file)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
