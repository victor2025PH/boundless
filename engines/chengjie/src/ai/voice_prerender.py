"""预渲染语音命中层 — 固定台词零延迟发送（AvatarHub Phase 2）。

夜间用 ``scripts/avatar_prerender.py``（Qwen3-TTS 7858，音色最像但 RTF≈2.8）把
每个人设的固定台词预合成为 OGG/Opus 语音条，落盘：

    assets/voices/<persona_id>/prerendered/<key>.ogg   # 语音条（已是 Telegram 格式）
    assets/voices/<persona_id>/prerendered/<key>.txt   # 台词原文（归一化后）

在线路径（TTSPipeline.synthesize）在合成前先查本层：命中 → 直接复用文件字节，
**零 GPU、零延迟、音色最像**（7858 质量 > 7852 在线档）。未命中 → 走正常合成，
零行为变化。

命中条件（防错发的四重校验）：
  1. 键匹配：sha1(归一化文本)[:8] 同名 .ogg 存在；
  2. 原文校验：sidecar .txt 内容与归一化文本**逐字相等**（防哈希碰撞/陈旧文件）；
  3. 参考音指纹（``_ref.json``）：人设换了参考音（新音色）→ 备货过期拒绝命中
     （回落现场合成=正确音色，零错声窗口；夜间渲染检测漂移自动整目录重渲）；
  4. 开关：``avatar_voice.enabled`` 且 ``avatar_voice.prerender.enabled``（默认开）。

缺口自动入库（Phase5）：``qualify_auto_stock``/``auto_stock_from_misses``——高频短句
缺口自动进 ``_common.txt``（数字/URL/敏感词/长度守卫 + 每日上限），夜间渲染兜底。

纯函数（可单测）：normalize_prerender_text / prerender_key / find_prerendered /
ref_content_fp / stock_is_stale / qualify_auto_stock。
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PRERENDER_DIRNAME = "prerendered"
DEFAULT_BASE_DIR = "assets/voices"


def normalize_prerender_text(text: str) -> str:
    """台词归一化（渲染与查询两侧必须走同一函数）。

    复用 ``clean_text_for_tts``（剔 emoji/折换行）后 strip——让「早安呀」与
    「早安呀 」「早安呀\\n」都命中同一条预渲染。
    """
    try:
        from src.ai.tts_pipeline import clean_text_for_tts
        t = clean_text_for_tts(str(text or ""))
    except Exception:
        t = str(text or "")
    return t.strip()


def prerender_key(text: str) -> str:
    """归一化文本 → 8 位 sha1 键（文件名）。空文本 → ""。"""
    t = normalize_prerender_text(text)
    if not t:
        return ""
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:8]


def find_prerendered(
    persona_id: str, text: str, *, base_dir: str = DEFAULT_BASE_DIR,
    ref_path: str = "",
) -> Optional[Path]:
    """查预渲染语音条。命中返回 .ogg 路径；未命中/校验失败 → None（绝不抛）。

    ``ref_path``：调用方传入该人设**当前**参考音路径时，做备货指纹比对——
    参考音已换（新音色）而备货还是旧音色 → 拒绝命中（回落现场合成，防发错声）。
    """
    pid = str(persona_id or "").strip()
    t = normalize_prerender_text(text)
    if not pid or not t:
        return None
    key = prerender_key(t)
    if not key:
        return None
    try:
        if ref_path and stock_is_stale(pid, ref_path, base_dir=base_dir):
            return None
        d = Path(base_dir) / pid / PRERENDER_DIRNAME
        ogg = d / f"{key}.ogg"
        txt = d / f"{key}.txt"
        if not (ogg.is_file() and ogg.stat().st_size > 0 and txt.is_file()):
            return None
        # 原文逐字校验（防哈希碰撞/手工放错文件）
        stored = txt.read_text(encoding="utf-8", errors="replace").strip()
        if normalize_prerender_text(stored) != t:
            return None
        return ogg
    except Exception:
        return None


# ── 参考音指纹 / 备货生命周期 ────────────────────────────────────────────────
# 人设换参考音（新音色）后，旧预渲染 clips 仍是旧音色——命中即「发错声音」事故。
# 生命周期闭环：渲染时把参考音**内容指纹**写进 _ref.json；命中层比对当前参考音
# 指纹，不一致 → 拒绝命中（回落现场合成=正确音色，零错声窗口）；夜间渲染检测到
# 指纹漂移 → 自动整目录重渲（等效 --force）+ 写新指纹，次日恢复零延迟命中。
REF_MANIFEST_NAME = "_ref.json"
# 内容 sha1 缓存：path -> (size, mtime, sha1)（命中热路每条消息都比对，不能重复读盘）
_REF_FP_CACHE: Dict = {}
_REF_FP_LOCK = None


def ref_content_fp(ref_path: str) -> str:
    """参考音**内容** sha1（按 size+mtime 进程级缓存）。文件缺失/失败 → ""。

    用内容哈希而非 (size,mtime)：文件被复制/touch 不误判换声，内容变了必然识别。
    """
    global _REF_FP_LOCK
    if _REF_FP_LOCK is None:
        import threading
        _REF_FP_LOCK = threading.Lock()
    try:
        p = Path(str(ref_path or ""))
        st = p.stat()
    except OSError:
        return ""
    key = str(p)
    # 缓存键用纳秒级 mtime：整数秒粒度会漏掉「同一秒内同大小的内容替换」
    mt = getattr(st, "st_mtime_ns", None) or int(st.st_mtime * 1e9)
    with _REF_FP_LOCK:
        hit = _REF_FP_CACHE.get(key)
        if hit and hit[0] == st.st_size and hit[1] == mt:
            return hit[2]
    try:
        digest = hashlib.sha1(p.read_bytes()).hexdigest()
    except Exception:
        return ""
    with _REF_FP_LOCK:
        _REF_FP_CACHE[key] = (st.st_size, mt, digest)
    return digest


def write_ref_manifest(
    persona_id: str, ref_path: str, *, base_dir: str = DEFAULT_BASE_DIR,
) -> None:
    """渲染完成后登记该人设备货对应的参考音指纹（best-effort）。"""
    try:
        fp = ref_content_fp(ref_path)
        if not fp:
            return
        d = Path(base_dir) / str(persona_id) / PRERENDER_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        import json
        (d / REF_MANIFEST_NAME).write_text(json.dumps({
            "ref_sha1": fp,
            "ref_path": str(ref_path),
            "rendered_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("[voice_prerender] 写 ref manifest 失败（忽略）", exc_info=True)


def read_ref_manifest(
    persona_id: str, *, base_dir: str = DEFAULT_BASE_DIR,
) -> Optional[dict]:
    """读备货指纹登记；无登记（legacy 目录）/损坏 → None。"""
    try:
        f = Path(base_dir) / str(persona_id) / PRERENDER_DIRNAME / REF_MANIFEST_NAME
        if not f.is_file():
            return None
        import json
        data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def stock_is_stale(
    persona_id: str, ref_path: str, *, base_dir: str = DEFAULT_BASE_DIR,
) -> bool:
    """备货是否过期（参考音已换而 clips 还是旧音色）。

    - 无登记（legacy，指纹机制上线前的备货）→ False（兼容放行；下次夜间渲染会补登记）。
    - 有登记且指纹一致 → False；不一致/当前参考音读不到 → True（宁可现场合成不发错声）。
    """
    m = read_ref_manifest(persona_id, base_dir=base_dir)
    if not m or not m.get("ref_sha1"):
        return False
    cur = ref_content_fp(ref_path)
    return (not cur) or (cur != str(m.get("ref_sha1")))


def copy_for_send(src: Path, out_dir: Path) -> Path:
    """把预渲染文件复制成一次性发送副本（调用方发送后会 unlink，原件必须保住）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / (
        f"prerender-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.ogg")
    shutil.copyfile(src, dst)
    return dst


DEFAULT_LINES_DIR = "config/prerender_lines"
COMMON_LINES_NAME = "_common.txt"


def read_prerender_lines(
    persona_id: str, *, lines_dir: str = DEFAULT_LINES_DIR,
) -> list:
    """读取人设台词库：``_common.txt``（全人设共用）+ ``<persona>.txt``（专属）。

    每行一条台词，``#`` 开头为注释；归一化后按键去重（先到先得）。
    目录/文件缺失 → 少合并一份，不抛。纯函数式（只读 IO）。
    """
    pid = str(persona_id or "").strip()
    out: list = []
    seen: set = set()
    d = Path(lines_dir)
    for name in (COMMON_LINES_NAME, f"{pid}.txt" if pid else ""):
        if not name:
            continue
        f = d / name
        if not f.is_file():
            continue
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            t = normalize_prerender_text(s)
            k = prerender_key(t)
            if t and k and k not in seen:
                seen.add(k)
                out.append(t)
    return out


_TARGET_RE = None


def sanitize_lines_target(target: str) -> str:
    """台词库写入目标净化：``_common`` 或人设 id（字母/数字/下划线/连字符）。

    防路径穿越（``../``、盘符、斜杠一律拒绝）。非法 → ""（调用方 400）。
    """
    global _TARGET_RE
    if _TARGET_RE is None:
        import re
        _TARGET_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
    t = str(target or "").strip() or "_common"
    return t if _TARGET_RE.match(t) else ""


def append_prerender_line(
    text: str, *, target: str = "_common",
    lines_dir: str = DEFAULT_LINES_DIR, max_chars: int = 60,
) -> dict:
    """把一条台词追加进台词库（缺口→备货的「一键入库」写入口）。

    - ``target``：``_common``（全人设，默认）或人设 id → ``<target>.txt``。
    - 归一化后按键去重：同文件已有同键台词 → ``{added: False, reason: "duplicate"}``。
    - 长度守卫 ``max_chars``（预渲染是短句体裁；超长是误操作）。
    返回 {ok, added, reason, target, text, file}；文件不存在自动创建。
    """
    tgt = sanitize_lines_target(target)
    if not tgt:
        return {"ok": False, "added": False, "reason": "bad_target", "target": target}
    t = normalize_prerender_text(text)
    if not t:
        return {"ok": False, "added": False, "reason": "empty_text", "target": tgt}
    if len(t) > max(1, int(max_chars)):
        return {"ok": False, "added": False, "reason": "too_long",
                "target": tgt, "text": t}
    key = prerender_key(t)
    d = Path(lines_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{tgt}.txt"
    existing: set = set()
    if f.is_file():
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    existing.add(prerender_key(normalize_prerender_text(s)))
        except Exception:
            pass
    if key in existing:
        return {"ok": True, "added": False, "reason": "duplicate",
                "target": tgt, "text": t, "file": str(f)}
    with f.open("a", encoding="utf-8") as fh:
        fh.write(t + "\n")
    return {"ok": True, "added": True, "reason": "", "target": tgt,
            "text": t, "file": str(f)}


def list_lines_files(*, lines_dir: str = DEFAULT_LINES_DIR) -> list:
    """台词库清单：[{target, lines, file}]（``_common`` 排最前）。"""
    d = Path(lines_dir)
    if not d.is_dir():
        return []
    out: list = []
    for f in sorted(d.glob("*.txt")):
        try:
            n = sum(
                1 for line in f.read_text(
                    encoding="utf-8", errors="replace").splitlines()
                if line.strip() and not line.strip().startswith("#"))
        except Exception:
            n = -1
        out.append({"target": f.stem, "lines": n, "file": str(f)})
    out.sort(key=lambda x: (x["target"] != "_common", x["target"]))
    return out


# ── 缺口自动入库（Phase5：去掉「运营点按钮」这个人）─────────────────────────
# 高频短句缺口自动进台词库。守卫（宁可漏进不可错进——错进的代价是把
# 一次性/含隐私痕迹的句子永久渲染成语音资产）：
_AUTO_STOCK_BLOCK = (
    "转账", "汇款", "打款", "密码", "验证码", "银行卡", "身份证",
    "微信号", "支付宝", "QQ号", "手机号", "地址",
)
_AUTO_STOCK_BAD_MARKS = ("http", "www.", "@", "{", "}", "[", "]")


def qualify_auto_stock(
    text: str, count: int, *, min_count: int = 5, max_chars: int = 16,
) -> tuple:
    """判定一条缺口台词能否自动入库。返回 (ok, reason)。纯函数。

    - ``count >= min_count``：真的高频（一次性措辞不进库）；
    - 长度 ≤ ``max_chars``：固定台词体裁；
    - 不含数字（日期/金额/号码类内容都不是固定台词，且可能带隐私痕迹）；
    - 不含 URL/@/占位符标记、不含敏感词（转账/验证码等——上游 persona_guard
      本就不该产出，这里是最后一道皮带）。
    """
    t = normalize_prerender_text(text)
    if not t:
        return False, "empty"
    if int(count) < int(min_count):
        return False, "below_threshold"
    if len(t) > int(max_chars):
        return False, "too_long"
    if any(ch.isdigit() for ch in t):
        return False, "has_digit"
    low = t.lower()
    if any(m in low for m in _AUTO_STOCK_BAD_MARKS):
        return False, "bad_marks"
    if any(b in t for b in _AUTO_STOCK_BLOCK):
        return False, "blocked_word"
    return True, ""


def auto_stock_from_misses(
    top_misses: list, *, min_count: int = 5, max_add: int = 10,
    lines_dir: str = DEFAULT_LINES_DIR, target: str = "_common",
) -> dict:
    """把达标的缺口台词批量写进台词库（自动入库执行器）。

    ``top_misses``：stats dump 的 ``[{text, n, personas?}, ...]``。
    单人设占比 ≥80% 的缺口写进该人设专属库（其它音色不浪费渲染），否则进公共库。
    返回 {added: [{text,target}], skipped: [{text,reason}]}；append 自带按键去重。
    """
    added: list = []
    skipped: list = []
    budget = max(0, int(max_add))
    for item in top_misses or []:
        if len(added) >= budget:
            break
        text = str((item or {}).get("text") or "")
        n = int((item or {}).get("n") or 0)
        ok, reason = qualify_auto_stock(text, n, min_count=min_count)
        if not ok:
            skipped.append({"text": text, "reason": reason})
            continue
        tgt = target
        personas = (item or {}).get("personas")
        if isinstance(personas, dict) and personas:
            total = sum(int(v) for v in personas.values()) or 1
            dom_pid, dom_n = max(personas.items(), key=lambda kv: int(kv[1]))
            if int(dom_n) / total >= 0.8 and sanitize_lines_target(dom_pid):
                tgt = dom_pid
        rv = append_prerender_line(text, target=tgt, lines_dir=lines_dir)
        if rv.get("ok") and rv.get("added"):
            added.append({"text": rv["text"], "target": rv["target"]})
            logger.info("[voice_prerender] 缺口自动入库: %r → %s (n=%d)",
                        rv["text"], rv["target"], n)
        else:
            skipped.append({"text": text, "reason": rv.get("reason") or "append_failed"})
    return {"added": added, "skipped": skipped}


def write_prerendered(
    persona_id: str, text: str, ogg_bytes_path: Path,
    *, base_dir: str = DEFAULT_BASE_DIR,
) -> Path:
    """CLI 渲染完成后落盘（移动 ogg + 写归一化原文 sidecar）。返回最终 .ogg 路径。"""
    t = normalize_prerender_text(text)
    key = prerender_key(t)
    if not key:
        raise ValueError("empty text")
    d = Path(base_dir) / str(persona_id) / PRERENDER_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"{key}.ogg"
    Path(ogg_bytes_path).replace(final)
    (d / f"{key}.txt").write_text(t, encoding="utf-8")
    return final
