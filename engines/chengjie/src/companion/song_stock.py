# -*- coding: utf-8 -*-
"""人设清唱备货（song stock）——「会哼两句」能力的纯函数核心（2026-08-22 P0）。

背景：客户点名「唱首歌」时，旧链路只能用说话声念歌词冒充（voice_autosend 的
peer_requested_voice 强制语音口径），是实录级穿帮。本模块 + hub 唱歌工作室
（AvatarHub `/api/song/cover` 带 ``dry_vocal=true``＝清唱干声、参考音缺省即人设
克隆音）+ 夜间预渲染（scripts/song_prerender.py）构成 P0 闭环：

  模板干声（原创/公版） --夜间--> hub SVC 换人设音色 --落盘--> 备货
  客户点名要歌 --运行时--> 命中备货直发（零现场合成/零 GPU）；无备货绝不冒充。

设计不变量（改动前先读）：
- **运行时绝不现场合成**：176 白天 VRAM 高水位 + 合成 30s+，现场合成＝把客户
  等成超时 + 与聊天语音抢卡。运行时只查本地备货文件，没有就如实回落文本链。
- **同一把声音**：备货由 hub 用人设注册克隆音转换；参考音换绑后由 CLI 的
  hub_ref_sha1 比对触发重渲（防「换声后发旧唱段」，与 voice_prerender 的
  stock_is_stale 同一事故语义）。
- 路径解析走 ``AITR_DATA_DIR`` → CWD（服务进程 CWD=实例数据根；测试 conftest
  已把 AITR_DATA_DIR 指向 tmp＝天然隔离）。CLI 场景由调用方显式传根
  （scripts/_data_root 契约），本模块所有入口都接受显式 root 覆写。
- 词表宁缺勿滥：误判「在要歌」会把正常聊天变成突兀塞歌，比漏判伤害大。

门禁：tests/test_song_stock.py / tests/test_song_autosend.py。
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import struct
import threading
import time
import wave
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ai_chat_assistant.song_stock")

# ── 路径契约 ────────────────────────────────────────────────────────────────
TEMPLATES_SUBDIR = os.path.join("config", "song_templates")
STOCK_SUBDIR = os.path.join("assets", "voices")   # <root>/<persona>/songs/
LEDGER_SUBDIR = os.path.join("config", "song_send_ledger.json")
MANIFEST_NAME = "manifest.json"
STOCK_EXTS = (".ogg", ".wav", ".mp3")

# 备货产物最小尺寸（B）：低于此大概率是错误产物/空壳（媒体产物验证纪律）。
MIN_STOCK_BYTES = 40_000


def data_root(explicit: Optional[Path] = None) -> Path:
    """数据根：显式实参 → ``AITR_DATA_DIR`` → CWD（服务进程 CWD=实例数据根）。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("AITR_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return Path.cwd()


def _int_or(v: Any, default: int) -> int:
    """显式 0 是合法值（0=关闭该闸）——绝不用 ``or`` 吞掉（daily_reply_budget=0
    ＝不限额 的同款语义教训，见 peer_bot_guard.budget_flags 门禁）。"""
    try:
        return int(v)
    except Exception:
        return default


def _float_or(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def resolve_singing_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """``companion.singing`` 配置合并视图（缺省全关；键名与 example 注释同源）。"""
    try:
        raw = ((cfg or {}).get("companion") or {}).get("singing") or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    out = {
        "enabled": bool(raw.get("enabled", False)),
        "daily_cap": _int_or(raw.get("daily_cap", 2), 2),
        "cooldown_hours": _float_or(raw.get("cooldown_hours", 6), 6.0),
        "repeat_window_days": _float_or(raw.get("repeat_window_days", 7), 7.0),
        "allow_lang_fallback": bool(raw.get("allow_lang_fallback", False)),
        "framing_line": bool(raw.get("framing_line", True)),
        "templates_dir": str(raw.get("templates_dir") or "").strip(),
        "stock_dir": str(raw.get("stock_dir") or "").strip(),
    }
    return out


def templates_dir(scfg: Optional[Dict[str, Any]] = None,
                  root: Optional[Path] = None) -> Path:
    d = (scfg or {}).get("templates_dir") if isinstance(scfg, dict) else ""
    return Path(d) if d else data_root(root) / TEMPLATES_SUBDIR


def stock_root(scfg: Optional[Dict[str, Any]] = None,
               root: Optional[Path] = None) -> Path:
    d = (scfg or {}).get("stock_dir") if isinstance(scfg, dict) else ""
    return Path(d) if d else data_root(root) / STOCK_SUBDIR


# ── 模板清单 ────────────────────────────────────────────────────────────────
@dataclass
class SongTemplate:
    """一条清唱模板（干声源 + 元数据）。``file`` 相对 templates_dir。"""
    id: str
    title: str
    file: str
    lyrics: str = ""
    lang: str = "zh"
    scene: str = ""          # birthday / goodnight / love / ""（通用）
    mood: str = ""
    gender: str = ""         # 源唱者声区提示 F/M/""（供 CLI 调门参考，pitch=auto 兜底）
    duration_sec: float = 0.0
    source: str = ""         # origin（原创/ACE 离线产）/ pd（公有领域）——版权台账字段
    enabled: bool = True

    def src_path(self, tdir: Path) -> Path:
        return Path(tdir) / self.file


def load_song_manifest(tdir: Optional[Path] = None,
                       root: Optional[Path] = None) -> List[SongTemplate]:
    """读模板清单（``manifest.json``）。坏行逐条跳过并 WARN，绝不抛。

    结构：``{"templates": [{id,title,file,lyrics,lang,scene,source,...}, ...]}``。
    """
    d = Path(tdir) if tdir else templates_dir(root=root)
    f = d / MANIFEST_NAME
    out: List[SongTemplate] = []
    try:
        if not f.is_file():
            return []
        data = json.loads(f.read_text(encoding="utf-8")) or {}
        rows = data.get("templates") or []
        if not isinstance(rows, list):
            return []
        seen: set = set()
        for i, row in enumerate(rows):
            try:
                if not isinstance(row, dict):
                    raise ValueError("row 不是对象")
                tid = str(row.get("id") or "").strip()
                title = str(row.get("title") or "").strip()
                fname = str(row.get("file") or "").strip()
                if not tid or not title or not fname:
                    raise ValueError("缺 id/title/file")
                # 路径消毒：模板音频必须落在 templates_dir 内（防 ../ 穿越）
                if ".." in fname.replace("\\", "/").split("/"):
                    raise ValueError("file 含路径穿越")
                if tid in seen:
                    raise ValueError("id 重复")
                seen.add(tid)
                out.append(SongTemplate(
                    id=tid, title=title, file=fname,
                    lyrics=str(row.get("lyrics") or ""),
                    lang=str(row.get("lang") or "zh").strip().lower() or "zh",
                    scene=str(row.get("scene") or "").strip().lower(),
                    mood=str(row.get("mood") or "").strip(),
                    gender=str(row.get("gender") or "").strip().upper(),
                    duration_sec=float(row.get("duration_sec") or 0.0),
                    source=str(row.get("source") or "").strip().lower(),
                    enabled=bool(row.get("enabled", True)),
                ))
            except Exception as e:  # noqa: BLE001
                logger.warning("[song_stock] manifest 第 %d 行无效被跳过: %s", i, e)
        return out
    except Exception:
        logger.warning("[song_stock] manifest 读取失败 %s", f, exc_info=True)
        return []


# ── 意图检测（保守窄口径） ───────────────────────────────────────────────────
# 排除优先：否定/劝停、以及「我（自己）去唱/在唱」类自述（KTV 场景）。
_NEG_RE = re.compile(
    r"别唱|不要唱|不用唱|别给我唱|唱什么唱|不想听.{0,4}唱|"
    r"我(?:刚|正在|在|去|要去|想去|们去?)\s*(?:KTV|ktv|K歌)?\s*唱")
# 请求形（第一/第二人称、即时）：唱首/唱个/唱两句/唱给我/给我唱/来一首/会唱歌吗/想听你唱…
_REQ_RES = [
    re.compile(r"唱\s*(?:首|个|個|一首|一支|支|段|一段|两句|兩句|几句|幾句)"),
    re.compile(r"(?:给|給)我唱|唱(?:给|給)我|唱(?:来|來)(?:听|聽)|唱.{0,4}(?:听听|聽聽)"),
    re.compile(r"(?:会|會|能|可以)唱歌|唱歌(?:给|給)我"),
    re.compile(r"想听你唱|想聽你唱|听你唱|聽你唱|你唱歌.{0,4}(?:好听|好聽|吗|嗎)"),
    re.compile(r"来一首|來一首|来首歌|來首歌|献唱|獻唱|清唱"),
    re.compile(r"\bsing\s+(?:me\s+|us\s+)?(?:a\s+)?(?:song|something|it)\b", re.I),
    re.compile(r"\bsing\s+(?:for|to)\s+me\b|\bcan\s+you\s+sing\b|\bhear\s+you\s+sing\b", re.I),
]


def detect_song_request(text: str) -> bool:
    """客户本条是否在**向你**要一段唱（纯函数，保守窄口径）。

    宁可漏判（回落文本闲聊，无伤）不可误判（突兀塞歌=机器感）。
    否定句/自述去唱歌一律不算。
    """
    t = str(text or "").strip()
    if not t or len(t) > 400:
        return False
    if _NEG_RE.search(t):
        return False
    return any(r.search(t) for r in _REQ_RES)


_SCENE_RES = [
    ("birthday", re.compile(r"生日|birthday", re.I)),
    ("goodnight", re.compile(r"摇篮曲|搖籃曲|哄我睡|晚安曲|睡前.{0,3}(?:歌|曲)|lullaby", re.I)),
]


# ── 双档检测：粘性窗 + 追加逼唱（实施66 P0-2，2026-08-23 实录事故） ──────────
# 事故：「给我唱首歌吧」（strict 命中被拒）→「不行，必须唱」（窄词表逃逸）→
# hint 消失 → LLM 文字假唱《月亮代表我的心》。机制教训：**同一信号在不同消费方
# 需要不同置信度**——触发真唱维持窄口径（误发歌=骚扰），防假唱防线用宽口径
# （误注入一句「别假唱」零伤害，漏注入=穿帮）。粘性窗与 bazi_context 同哲学。
_SONG_TS_KEY = "_song_req_ts"
_SONG_CNT_KEY = "_song_req_count"
SONG_STICKY_SEC = 30 * 60.0

# 催促形排除：含「唱」但不是要你唱的固定搭配（唱票/唱衰…）。
_URGE_EXCLUDE_RE = re.compile(
    r"唱票|唱衰|唱反调|唱反調|唱高调|唱高調|唱双簧|唱雙簧|唱红脸|唱白脸")
# 泛化催促（粘性窗内才有意义；不含「唱」字的追加逼促）。
_URGE_GENERIC_RE = re.compile(
    r"来一个|來一個|再来一个|快点|快點|快嘛|快呀|快啊|就一句|就一段|就一首|"
    r"必须|必須|说好的|說好的|要听|要聽|想听|想聽")


def song_sticky_active(user_context: Dict[str, Any], *,
                       now: Optional[float] = None) -> bool:
    """会话是否仍在「要歌」粘性窗内（A 线：``_song_req_ts`` 随 ContextStore 持久）。"""
    try:
        ts = float((user_context or {}).get(_SONG_TS_KEY) or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    n = float(now if now is not None else time.time())
    return 0 <= (n - ts) < SONG_STICKY_SEC


def touch_song_request(user_context: Dict[str, Any], *,
                       now: Optional[float] = None) -> int:
    """记一次要歌（strict/demand 命中时调用）：窗口内计数 +1（窗口外重置 1），
    刷新粘性时间戳。返回当前压力值（含本次）——供被动逼唱冷却豁免（P0-4）。"""
    n = float(now if now is not None else time.time())
    cnt = 1
    if song_sticky_active(user_context, now=n):
        try:
            cnt = int(user_context.get(_SONG_CNT_KEY) or 0) + 1
        except (TypeError, ValueError):
            cnt = 1
    user_context[_SONG_TS_KEY] = n
    user_context[_SONG_CNT_KEY] = cnt
    return cnt


def song_pressure(user_context: Dict[str, Any]) -> int:
    try:
        return max(1, int((user_context or {}).get(_SONG_CNT_KEY) or 1))
    except (TypeError, ValueError):
        return 1


def song_sticky_from_texts(texts: Any) -> bool:
    """B 线口径：最近入站文本里任一条 strict 命中＝粘性成立（调用方负责时间窗）。"""
    try:
        for t in list(texts or [])[:12]:
            if detect_song_request(str(t or "")):
                return True
    except Exception:
        return False
    return False


def song_topic_state(text: str, *, sticky: bool = False) -> str:
    """双档检测（纯函数）：``""``/``"loose"``/``"demand"``。

    - strict（detect_song_request）→ demand（无需粘性）；
    - 粘性窗内的**短句追加**：含「唱」→ demand（「不行，必须唱」）；
      仅泛化催促（「快点」「说好的」）→ loose（只武装防假唱，不触发真唱——
      催促也可能指别的事，宁多一轮确认不误塞歌）。
    否定句/超长句/固定搭配（唱票…）一律 ``""``。
    """
    t = str(text or "").strip()
    if not t or len(t) > 400:
        return ""
    if detect_song_request(t):
        return "demand"
    if not sticky:
        return ""
    if _NEG_RE.search(t):
        return ""
    if len(t) > 24:
        return ""
    if _URGE_EXCLUDE_RE.search(t):
        return ""
    if "唱" in t:
        return "demand"
    if _URGE_GENERIC_RE.search(t):
        return "loose"
    return ""


def requested_song_scene(text: str) -> str:
    """从请求文本抽场景提示（birthday/goodnight/''），供选曲偏好。纯函数。"""
    t = str(text or "")
    for scene, r in _SCENE_RES:
        if r.search(t):
            return scene
    return ""


# ── 选曲 ───────────────────────────────────────────────────────────────────
def pick_song(templates: List[SongTemplate], *, lang: str = "zh",
              scene_hint: str = "", exclude_ids: Optional[List[str]] = None,
              variety_key: str = "", day_key: str = "",
              allow_lang_fallback: bool = False) -> Optional[SongTemplate]:
    """按语言/场景/避重挑一首（纯函数，确定性轮换：同会话同日恒定、隔日换）。

    - 语言：优先精确匹配；无匹配且 ``allow_lang_fallback`` 才放宽到全部
      （默认不放宽——给英文客户唱中文歌是惊吓不是惊喜）。
    - 避重先于场景偏好：重复窗内发过的硬排除；剩余里再套场景 hint（场景曲
      被避重排掉时回落通用曲，不该因此整个不唱）。全被排除 → None（宁可不唱）。
    """
    pool = [t for t in (templates or []) if t.enabled]
    if not pool:
        return None
    lang_n = (lang or "zh").strip().lower()
    exact = [t for t in pool if t.lang == lang_n]
    pool = exact if exact else (pool if allow_lang_fallback else [])
    if not pool:
        return None
    ex = set(exclude_ids or [])
    pool = [t for t in pool if t.id not in ex]
    if not pool:
        return None
    if scene_hint:
        scoped = [t for t in pool if t.scene == scene_hint]
        if scoped:
            pool = scoped
    pool.sort(key=lambda t: t.id)
    idx = zlib.crc32(f"{variety_key}#{day_key}".encode("utf-8")) % len(pool)
    return pool[idx]


def find_stock_file(persona_id: str, template_id: str, *,
                    sroot: Optional[Path] = None,
                    root: Optional[Path] = None) -> Optional[Path]:
    """查该人设该模板的备货文件（.ogg 优先）。不存在/空壳 → None。"""
    pid = str(persona_id or "").strip()
    tid = str(template_id or "").strip()
    if not pid or not tid:
        return None
    base = (Path(sroot) if sroot else stock_root(root=root)) / pid / "songs"
    for ext in STOCK_EXTS:
        p = base / f"{tid}{ext}"
        try:
            if p.is_file() and p.stat().st_size >= MIN_STOCK_BYTES:
                return p
        except Exception:
            continue
    return None


# ── 草稿协同 hint（实施58 P1：「唱不了别假唱」——文本假唱的源头预防） ─────────
# 本能力的初始事故就是「说话声念歌词冒充唱」；开闸后文本链仍有两个假唱口：
# ① B 线会真唱时草稿只是失败兜底，LLM 不知道会写「我唱给你听：♪歌词♪」；
# ② 发不出（未开闸/无货/频控/语言不合/A 线未接短路）时 LLM 顺着情绪假唱或
# 空头承诺。与 photo 3b2 hint 同哲学：源头预防，出站承诺守卫仍是最后防线。
_HINT_FULFILL = (
    "对方在让你唱歌。系统会尝试直接发送一段真实的清唱语音；你现在写的这条"
    "文字只会在「唱段没能发出」时才发送。所以绝不要用文字打出歌词假装在唱，"
    "也不要写「等我唱/马上唱给你」这类承诺；就当这一轮发不出唱段，用人设"
    "口吻自然回应（先聊两句、卖个关子都行），也不要否认你会唱歌。")
_HINT_REFUSAL = (
    "对方在让你唱歌，但这一轮发不出真的唱段。绝对不要用文字打出歌词假装在"
    "唱，也不要承诺「我唱给你听/等我唱」这类马上兑现的话；用人设口吻自然"
    "回应——俏皮岔开、或说改天心情好了再唱都行，别否认自己会唱歌，也别提"
    "系统/功能这类字眼。")


def song_coherence_hint(cfg: Dict[str, Any], *, peer_text: str,
                        persona_id: str = "", conv_id: str = "",
                        can_deliver: bool = False,
                        now: Optional[float] = None,
                        recent_texts: Any = None,
                        demand_pressure: int = 1) -> str:
    """对方在要歌时给草稿/回复 LLM 的协同提示。三态：

    ``""``＝本条不是要歌；``_HINT_FULFILL``＝本轮预计会真发唱段（B 线短路，
    草稿只是失败兜底）；``_HINT_REFUSAL``＝发不出。判定镜像 ``autosend_song``
    闸序（enabled → 备货 → 频控(含逼唱豁免) → 语言/避重选曲）——漂移由门禁钉住。
    ``can_deliver=False``＝调用链没有唱歌短路，要歌一律 REFUSAL。
    ``recent_texts``（实施66 P0-2）＝最近入站文本（粘性证据），「不行，必须唱」
    这类追加逼唱经 ``song_topic_state`` 补判；loose（泛化催促）恒 REFUSAL
    （本轮不会真唱，先禁假唱）。persona/conv 缺席按 REFUSAL 保守（草稿反正在
    真唱成功时被丢弃，语义自洽）；任何异常同样保守回 REFUSAL。
    """
    text = str(peer_text or "")
    if not text:
        return ""
    state = song_topic_state(
        text, sticky=song_sticky_from_texts(recent_texts))
    if not state:
        return ""
    try:
        scfg = resolve_singing_cfg(cfg or {})
        if not can_deliver or not scfg.get("enabled", False):
            return _HINT_REFUSAL
        if state != "demand":
            return _HINT_REFUSAL
        pid = str(persona_id or "").strip()
        if not pid:
            return _HINT_REFUSAL
        sroot = stock_root(scfg)
        stocked = [t for t in load_song_manifest(templates_dir(scfg))
                   if find_stock_file(pid, t.id, sroot=sroot)]
        if not stocked:
            return _HINT_REFUSAL
        now_ts = float(now if now is not None else time.time())
        ledger = get_song_ledger()
        ok, _why = song_gate_verdict(
            scfg,
            today_count=ledger.count_since(conv_id, day_start_ts(now_ts)),
            last_ts=ledger.last_ts(conv_id), now=now_ts,
            demand_pressure=demand_pressure)
        if not ok:
            return _HINT_REFUSAL
        han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        alpha = sum(1 for c in text if c.isascii() and c.isalpha())
        lang = "en" if (han == 0 and alpha >= 4) else "zh"
        tmpl = pick_song(
            stocked, lang=lang,
            scene_hint=requested_song_scene(text),
            exclude_ids=ledger.recent_template_ids(
                conv_id, float(scfg.get("repeat_window_days", 7) or 7),
                now=now_ts),
            variety_key=conv_id,
            day_key=time.strftime("%Y%m%d", time.localtime(now_ts)),
            allow_lang_fallback=bool(scfg.get("allow_lang_fallback", False)))
        return _HINT_FULFILL if tmpl is not None else _HINT_REFUSAL
    except Exception:
        logger.debug("[song_stock] 协同 hint 判定异常，按 REFUSAL 保守",
                     exc_info=True)
        return _HINT_REFUSAL


# ── 止损话术（实施58 P3-1：音色一致性修复落地前的柔性框定） ──────────────────
# 唱歌嗓与说话嗓当前只贴三成（选角路遗留，修复走 RVC 主修线）——送达时带一句
# 真人也成立的铺垫，降穿帮锐度。诚实定位：这是止血不是修复；A1 音色闸过后
# 经 `framing_line: false` 一键退场。变体池 crc32(会话#日) 确定性轮换防复读。
_FRAMING_LINES = (
    "给你唱一段～我唱歌声音跟平时说话不太一样哈😊",
    "清唱了一小段，唱歌嗓和说话嗓有点差别，别笑我呀～",
    "哼了一段给你，唱起歌来声音会变一点点，习惯就好啦😄",
)


def framing_caption(scfg: Dict[str, Any], *, conv_id: str = "",
                    now: Optional[float] = None) -> str:
    """唱段语音消息的配文铺垫；``framing_line`` 关（默认开）→ 空串。纯函数。"""
    if not (scfg or {}).get("framing_line", True):
        return ""
    ts = float(now if now is not None else time.time())
    day = time.strftime("%Y%m%d", time.localtime(ts))
    idx = zlib.crc32(f"{conv_id}#{day}".encode("utf-8")) % len(_FRAMING_LINES)
    return _FRAMING_LINES[idx]


# ── 文字假唱检测/剥离（实施66 P0-3，出站最后防线） ───────────────────────────
# 实录（2026-08-23 22:50）：hint 词表被「不行，必须唱」逃逸后，LLM 四条文本演完
# 整套假唱（铺垫→「行，那我唱了，就一句啊」→「"月亮代表我的心～"……」→
# 「起鸡皮疙瘩」）。hint 是概率性防御（高压上下文里 LLM 会突破劝导），这里是
# 确定性防御：表演体特征组合检测 → 能真唱剥假唱段+发真唱段（把谎变真），
# 不能则整套剥离。特征刻意收窄（宁漏勿误伤）：
# - 表演宣告（第一人称起唱式；「我唱了三年京剧」类经历句被时长前瞻排除）
# - 唱词引用行（引号短句 + ～/♪ 或后接省略号；♪ 前缀行）
# - 表演自评（跑调/别笑/鸡皮疙瘩……只在有宣告/唱词时参与剥离）
_PERF_DECL_RE = re.compile(
    r"(?:(?:那|好|行)[,，、\s]*我(?:就|先|来|來)?唱"
    r"|(?<![你他她])我(?:就|先|来|來)唱"
    r"|(?<![你他她])我唱(?:了|咯|喽|嘍|啦|哈)"
    r"(?![一二三四五六七八九十百千0-9几幾多半年个個遍次])"
    r"|开始唱|開始唱|清唱一段|(?<![你他她])我唱(?:给|給)你(?:听|聽)"
    r"|唱一句(?:给|給)你)")
_PERF_LYRIC_RE = re.compile(
    r"[\"\u201c\u201e「『][^\"\u201d「」『』\n]{2,30}[~～♪♫][\"\u201d」』]"
    r"|[\"\u201c「『][^\"\u201d「」『』\n]{2,30}[\"\u201d」』]\s*(?:…|\.{3,}|……)"
    r"|♪\s*[^\s♪]{2,30}")
_PERF_REVIEW_RE = re.compile(
    r"跑调|跑調|走音|破音|(?:别|別|不许|不許|不准)笑|鸡皮疙瘩|雞皮疙瘩|"
    r"献丑|獻醜|唱完(?:了|啦)|嗓子(?:哑|啞|不行|废|廢)")


def detect_song_performance(text: str, *, song_context: bool = False) -> bool:
    """出站文本是否在**用文字表演唱歌**（纯函数）。

    无语境：宣告+唱词双证据才判（防误伤）；``song_context``（对方在要歌/粘性窗
    内）：唱词+自评 或 宣告+自评 也判。讨论歌词/夸对方唱歌/诚实拒唱（自评词
    单独出现）一律不判。
    """
    t = str(text or "")
    if not t or len(t) > 2000:
        return False
    decl = bool(_PERF_DECL_RE.search(t))
    lyric = bool(_PERF_LYRIC_RE.search(t))
    if decl and lyric:
        return True
    if song_context:
        review = bool(_PERF_REVIEW_RE.search(t))
        if lyric and review:
            return True
        if decl and review:
            return True
    return False


_SEG_SPLIT_RE = re.compile(r"(……|[。！？!?\n；;])")


def strip_song_performance(text: str) -> str:
    """句级剥离表演段（宣告句/唱词句；自评句仅当短句时剥）。

    只应在 ``detect_song_performance``＝True 后调用；剥空返回 ""（调用方换
    ``song_deflection_line``）。非表演句原样保留（含分隔符）。
    """
    raw = str(text or "")
    if not raw.strip():
        return raw
    parts = _SEG_SPLIT_RE.split(raw)
    out: List[str] = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        drop = False
        s = seg.strip()
        if s:
            if _PERF_DECL_RE.search(s) or _PERF_LYRIC_RE.search(s):
                drop = True
            elif _PERF_REVIEW_RE.search(s) and len(s) <= 16:
                drop = True
        if not drop and seg:
            out.append(seg + delim)
        i += 2
    return "".join(out).strip()


_SONG_DEFLECT = {
    "zh": ("哎呀，今天嗓子不在状态啦，先饶了我嘛，改天心情好唱给你～",
           "嗓子有点哑，今天就不献丑了，下次一定补你一段😊"),
    "en": ("My voice is a bit off today, I'll owe you a song~",
           "Not in singing shape right now, rain check on that song 😊"),
}


def song_deflection_line(lang: str = "zh", *, key: str = "",
                         now: Optional[float] = None) -> str:
    """假唱剥空后的台阶句（远期口径，不构成马上兑现承诺）。确定性轮换。"""
    pool = _SONG_DEFLECT.get((lang or "zh").strip().lower()[:2],
                             _SONG_DEFLECT["zh"])
    ts = float(now if now is not None else time.time())
    day = time.strftime("%Y%m%d", time.localtime(ts))
    return pool[zlib.crc32(f"{key}#{day}".encode("utf-8")) % len(pool)]


# ── 干声窗：裁唱段 + 峰值归一（2026-08-22 归因轮事故） ───────────────────────
# 事故：ACE 按 duration_s=30 出片，4 句词只唱前 11~16 秒；后半段是死静音
# （晚风）或分离器漏进来的二胡/伴奏（月光）。人耳听成「后半段没声/有乐器/
# 转换成品没声音」。机筛 clone_score 会对整段做归一化，空尾+漏乐照样打高分
# ——尺寸/峰值/声纹都不构成「这是一段完整清唱」的验证。
# 修法：能量窗裁到真唱段（静默终止 + 相对跌落抓「人声没了乐器顶上」）+
# 峰值归一到 -1.5dBFS + 立体声镜像（部分播放器对单声道 WAV 静音）。
_SUNG_FLOOR_DB = -42.0
_SUNG_STOP_GAP = 1.2
_SUNG_DROP_DB = 14.0
_SUNG_DROP_HOLD = 0.8
_SUNG_MIN_KEEP = 6.0
_SUNG_PAD = 0.35
_SUNG_FADE = 0.8
_SUNG_TARGET_DB = -1.5
_SUNG_MAX_GAIN = 30.0


def expected_sung_sec(lyrics: str) -> float:
    """按歌词估唱段时长（纯函数）。CJK 0.8s/字、拉丁词 0.5s，夹 [6, 32]。

    0.8s/字按慢板民谣实测校准（2026-08-22 选角轮：26 字唱了 ~19.5s≈0.75s/字；
    首版用说话语速 0.45s/字 → 硬帽 17s 把完整唱段拦腰切断，老板人耳实锤
    「没唱完，突然结束」）。硬帽是防漏乐的保险丝，不该咬到正常唱段——宁松勿紧。
    """
    text = str(lyrics or "")
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    latin = len(re.findall(r"[A-Za-z]+", text))
    est = cjk * 0.8 + latin * 0.5
    return max(6.0, min(32.0, est or 12.0))


def _pcm16_from_wav(data: bytes) -> Tuple[List[int], int, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2 or ch < 1 or fr < 8000 or not raw:
        raise ValueError("unsupported wav (need pcm16)")
    vals = list(struct.unpack(f"<{n * ch}h", raw))
    return vals, ch, fr


def _wav_stereo_pcm16(mono: List[int], fr: int) -> bytes:
    """单声道 → 立体声镜像 PCM16（Windows 部分播放器对 mono WAV 会静音）。"""
    frames = []
    for v in mono:
        v = max(-32768, min(32767, int(v)))
        frames.append(v)
        frames.append(v)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(fr)
        w.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return buf.getvalue()


def _downmix_mono(vals: List[int], ch: int) -> List[int]:
    if ch <= 1:
        return list(vals)
    return [int(sum(vals[i:i + ch]) / ch) for i in range(0, len(vals), ch)]


def frame_rms_db(mono: List[int], fr: int, frame_ms: float = 50.0) -> List[Tuple[float, float]]:
    """逐帧 RMS（dBFS）。纯函数。"""
    step = max(1, int(fr * frame_ms / 1000.0))
    out: List[Tuple[float, float]] = []
    for i in range(0, max(0, len(mono) - step + 1), step):
        seg = mono[i:i + step]
        rms = math.sqrt(sum(v * v for v in seg) / len(seg))
        db = 20.0 * math.log10(max(rms, 1e-9) / 32768.0)
        out.append((i / float(fr), db))
    return out


def find_sung_end(frames: List[Tuple[float, float]], *,
                  floor_db: float = _SUNG_FLOOR_DB,
                  stop_gap: float = _SUNG_STOP_GAP,
                  min_keep: float = _SUNG_MIN_KEEP,
                  drop_db: float = _SUNG_DROP_DB,
                  drop_hold: float = _SUNG_DROP_HOLD,
                  hard_cap: float = 0.0,
                  rel_after: float = 0.0) -> Tuple[float, str]:
    """从能量曲线找唱段终点。返回 (end_t, reason)。

    - ``silence``：连续静默 ≥stop_gap（A/C：词唱完后的空尾/空窗）
    - ``rel_drop``：仍有能量但比前 8 秒人声中位低 ≥drop_db，且持续 drop_hold
      （B：人声没了、二胡用同等能量顶上，没有绝对静默）。只在
      ``floor < db ≤ median-drop`` 带内计数——静默走上一支，气口不够长不切。
    - ``hard_cap``：歌词时长上限（双重保险，防漏乐一直响）
    - ``eof``：一直有声走到文件尾
    """
    if not frames:
        return 0.0, "empty"
    dt = frames[1][0] - frames[0][0] if len(frames) > 1 else 0.05
    early = [db for t, db in frames if t < 8.0 and db > floor_db]
    early.sort()
    # 用前 8 秒人声的偏响分位当基准（P75），避免气口把中位拉低、相对跌落失灵
    median = early[int(len(early) * 0.75)] if early else floor_db
    rel_start = max(min_keep, rel_after)
    started = False
    end_t = 0.0
    silent_run = 0.0
    drop_run = 0.0
    for t, db in frames:
        if db > floor_db:
            started = True
            end_t = t
            silent_run = 0.0
            if (t >= rel_start and early
                    and db <= median - drop_db):
                drop_run += dt
                if drop_run >= drop_hold:
                    return max(min_keep, t - drop_run + dt), "rel_drop"
            else:
                drop_run = 0.0
        else:
            drop_run = 0.0
            silent_run += dt
            if started and silent_run >= stop_gap and end_t >= min_keep:
                return end_t, "silence"
        if hard_cap > 0 and started and t >= hard_cap and end_t >= min_keep:
            return min(end_t, hard_cap), "hard_cap"
    return (end_t, "eof") if started else (0.0, "empty")


def _normalize_peak(mono: List[int], target_db: float = _SUNG_TARGET_DB,
                    max_gain: float = _SUNG_MAX_GAIN) -> Tuple[List[int], float]:
    peak = max((abs(v) for v in mono), default=1) or 1
    gain = (32768.0 * (10.0 ** (target_db / 20.0))) / peak
    gain = min(gain, max_gain)
    return [max(-32768, min(32767, int(v * gain))) for v in mono], gain


def prepare_dry_vocal(wav_bytes: bytes, *, lyrics: str = "",
                      min_keep: float = _SUNG_MIN_KEEP) -> Tuple[bytes, Dict[str, Any]]:
    """裁唱段 + 峰值归一 + 立体声镜像。失败回原字节，meta.ok=False。

    纯函数（除 wave 编解码）；调用方（CLI 备货）在送 hub 前与收成品后各跑一次。
    """
    meta: Dict[str, Any] = {"ok": False, "reason": "skip"}
    try:
        vals, ch, fr = _pcm16_from_wav(wav_bytes)
    except Exception as e:  # noqa: BLE001
        meta["error"] = str(e)[:120]
        return wav_bytes, meta
    mono = _downmix_mono(vals, ch)
    src_dur = len(mono) / float(fr) if fr else 0.0
    frames = frame_rms_db(mono, fr)
    est = expected_sung_sec(lyrics) if lyrics else 0.0
    cap = est * 1.45 if est else 0.0
    rel_after = (est * 0.55) if est else 8.0
    end_t, why = find_sung_end(
        frames, min_keep=min_keep, hard_cap=cap, rel_after=rel_after)
    if why == "eof" and src_dur >= min_keep:
        end_t = src_dur
    meta.update({
        "src_dur": round(src_dur, 2),
        "end_t": round(end_t, 2),
        "reason": why,
        "hard_cap": round(cap, 2),
        "src_ch": ch,
    })
    if end_t < min_keep:
        meta["error"] = f"sung_window_too_short:{end_t:.1f}"
        return wav_bytes, meta
    cut = min(len(mono), int((end_t + _SUNG_PAD) * fr))
    out = mono[:cut]
    nfade = min(int(_SUNG_FADE * fr), len(out))
    if nfade > 1:
        for i in range(nfade):
            idx = len(out) - nfade + i
            out[idx] = int(out[idx] * (1.0 - i / nfade))
    out, gain = _normalize_peak(out)
    meta["ok"] = True
    meta["gain"] = round(gain, 2)
    meta["out_dur"] = round(len(out) / float(fr), 2)
    return _wav_stereo_pcm16(out, fr), meta


# ── 发送账本（避重/频控；JSON 落数据根） ─────────────────────────────────────
class SongSendLedger:
    """每会话唱段发送账本（进程内单写者=autosend worker 线程；文件小、同步写）。

    结构 v1：``{"v":1, "convs": {conv_key: [{"ts": float, "tid": str}, ...]}}``；
    保存时按 ``keep_days`` 剪枝。坏文件按空账本处理（绝不因账本崩发送链）。
    """

    KEEP_DAYS = 30.0

    def __init__(self, path: Optional[Path] = None, *, root: Optional[Path] = None):
        self.path = Path(path) if path else (data_root(root) / LEDGER_SUBDIR)
        self._lock = threading.Lock()
        self._data: Dict[str, List[Dict[str, Any]]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
                convs = raw.get("convs") or {}
                if isinstance(convs, dict):
                    self._data = {
                        str(k): [e for e in v if isinstance(e, dict)]
                        for k, v in convs.items() if isinstance(v, list)}
        except Exception:
            logger.warning("[song_stock] 账本读取失败，按空账本继续 %s",
                           self.path, exc_info=True)
            self._data = {}

    def _save(self, now: float) -> None:
        try:
            floor = now - self.KEEP_DAYS * 86400.0
            pruned = {}
            for k, rows in self._data.items():
                kept = [r for r in rows if float(r.get("ts") or 0) >= floor]
                if kept:
                    pruned[k] = kept
            self._data = pruned
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": 1, "convs": self._data},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.warning("[song_stock] 账本写入失败（本次发送照常）", exc_info=True)

    def record(self, conv_key: str, template_id: str,
               now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._load()
            self._data.setdefault(str(conv_key), []).append(
                {"ts": ts, "tid": str(template_id)})
            self._save(ts)

    def recent_template_ids(self, conv_key: str, window_days: float,
                            now: Optional[float] = None) -> List[str]:
        ts = float(now if now is not None else time.time())
        floor = ts - float(window_days) * 86400.0
        with self._lock:
            self._load()
            rows = self._data.get(str(conv_key)) or []
            return [str(r.get("tid") or "") for r in rows
                    if float(r.get("ts") or 0) >= floor]

    def count_since(self, conv_key: str, since_ts: float) -> int:
        with self._lock:
            self._load()
            rows = self._data.get(str(conv_key)) or []
            return sum(1 for r in rows if float(r.get("ts") or 0) >= since_ts)

    def last_ts(self, conv_key: str) -> float:
        with self._lock:
            self._load()
            rows = self._data.get(str(conv_key)) or []
            return max((float(r.get("ts") or 0) for r in rows), default=0.0)


_LEDGER: Optional[SongSendLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_song_ledger() -> SongSendLedger:
    """进程级账本单例（路径按当时 AITR_DATA_DIR/CWD 解析；测试可直建实例注入）。"""
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = SongSendLedger()
        return _LEDGER


def song_gate_verdict(scfg: Dict[str, Any], *, today_count: int,
                      last_ts: float, now: float,
                      demand_pressure: int = 1) -> Tuple[bool, str]:
    """频控裁决（纯函数）：日上限 + 冷却。返回 (放行, 原因)。

    显式 0＝关闭该闸（cap=0 不限量、cooldown=0 无冷却）——勿用 ``or`` 回默认。
    ``demand_pressure``（实施66 P0-4）：同窗第 ≥2 次被动逼唱 → 豁免**冷却**
    （冷却防的是「主动塞歌骚扰」，不该拦「对方逼着唱」；encore 也是真需求）；
    日上限**不豁免**（唱歌总量的硬底）。放行原因 ``pressure_exempt`` 供观测。
    """
    cap = _int_or(scfg.get("daily_cap", 2), 2)
    if cap > 0 and today_count >= cap:
        return False, "capped_daily"
    cd = _float_or(scfg.get("cooldown_hours", 6), 6.0) * 3600.0
    if cd > 0 and last_ts > 0 and (now - last_ts) < cd:
        if _int_or(demand_pressure, 1) >= 2:
            return True, "pressure_exempt"
        return False, "capped_cooldown"
    return True, ""


def day_start_ts(now: float) -> float:
    """本地日 0 点时间戳（日上限口径=本地日历日，与回复额度守卫同感觉）。"""
    lt = time.localtime(now)
    return now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


# ── 观测 ───────────────────────────────────────────────────────────────────
class SongStats:
    """进程级计数（风格对齐 bazi_stats/avatar_voice_stats：轻量、绝不抛）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c: Dict[str, int] = {}
        self._by_template: Dict[str, int] = {}
        self._last_sent: Dict[str, Any] = {}

    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._c[key] = self._c.get(key, 0) + n

    def note_sent(self, template_id: str, persona_id: str) -> None:
        with self._lock:
            self._c["sent"] = self._c.get("sent", 0) + 1
            if len(self._by_template) < 200 or template_id in self._by_template:
                self._by_template[template_id] = self._by_template.get(template_id, 0) + 1
            self._last_sent = {"template_id": template_id,
                               "persona_id": persona_id, "ts": time.time()}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._c),
                "by_template": dict(self._by_template),
                "last_sent": dict(self._last_sent),
            }

    def dump_prom(self) -> str:
        """Prometheus 文本行（风格对齐 bazi_stats.dump_prom：零流量=零行）。"""
        with self._lock:
            lines = []
            for k, v in sorted(self._c.items()):
                key = re.sub(r"[^a-z0-9_]", "_", str(k).lower()) or "other"
                lines.append(f"singing_{key}_total {int(v)}")
            for tid, v in sorted(self._by_template.items()):
                t = re.sub(r"[^A-Za-z0-9_]", "_", str(tid)) or "other"
                lines.append(
                    f'singing_sent_by_template_total{{template="{t}"}} '
                    f"{int(v)}")
            return ("\n".join(lines) + "\n") if lines else ""


_STATS = SongStats()


def get_song_stats() -> SongStats:
    return _STATS


def metrics_snapshot() -> Dict[str, Any]:
    return _STATS.snapshot()


# ── 状态快照（avatar-status 消费；备货盘点带 TTL 防轮询扫盘） ─────────────────
_STATUS_CACHE: Dict[str, Any] = {"until": 0.0, "value": None}
_STATUS_TTL_SEC = 60.0


def singing_status_snapshot(cfg: Dict[str, Any], *,
                            root: Optional[Path] = None,
                            force: bool = False) -> Dict[str, Any]:
    """singing 能力状态（enabled/模板数/每人设备货数/计数器），60s TTL。"""
    now = time.monotonic()
    if (not force and _STATUS_CACHE.get("value") is not None
            and now < float(_STATUS_CACHE.get("until") or 0)):
        return dict(_STATUS_CACHE["value"])
    scfg = resolve_singing_cfg(cfg or {})
    out: Dict[str, Any] = {"enabled": scfg["enabled"]}
    try:
        templates = load_song_manifest(templates_dir(scfg, root))
        out["templates"] = len(templates)
        out["templates_enabled"] = sum(1 for t in templates if t.enabled)
        sroot = stock_root(scfg, root)
        stock: Dict[str, int] = {}
        if sroot.is_dir():
            for pdir in sorted(sroot.iterdir()):
                sdir = pdir / "songs"
                if not sdir.is_dir():
                    continue
                n = sum(1 for f in sdir.iterdir()
                        if f.suffix.lower() in STOCK_EXTS
                        and f.stat().st_size >= MIN_STOCK_BYTES)
                if n:
                    stock[pdir.name] = n
        out["stock"] = stock
        # 账本存在性（实施66 P0-5「零发送一眼可见」）：开闸多日 ledger 不存在
        # ＝一段都没唱出去过——比翻文件系统早发现结构性断链。
        led = get_song_ledger()
        out["ledger_exists"] = bool(Path(led.path).is_file())
    except Exception:
        logger.debug("[song_stock] 状态盘点失败（回空）", exc_info=True)
    out["stats"] = metrics_snapshot()
    _STATUS_CACHE["value"] = dict(out)
    _STATUS_CACHE["until"] = now + _STATUS_TTL_SEC
    return out


__all__ = [
    "SongTemplate", "SongSendLedger", "SongStats",
    "resolve_singing_cfg", "templates_dir", "stock_root", "data_root",
    "load_song_manifest", "detect_song_request", "requested_song_scene",
    "pick_song", "find_stock_file", "get_song_ledger", "get_song_stats",
    "song_gate_verdict", "day_start_ts", "metrics_snapshot",
    "singing_status_snapshot", "song_coherence_hint", "framing_caption",
    "expected_sung_sec", "frame_rms_db", "find_sung_end", "prepare_dry_vocal",
    "song_topic_state", "song_sticky_active", "song_sticky_from_texts",
    "touch_song_request", "song_pressure", "detect_song_performance",
    "strip_song_performance", "song_deflection_line", "SONG_STICKY_SEC",
    "MANIFEST_NAME", "STOCK_EXTS", "MIN_STOCK_BYTES",
]
