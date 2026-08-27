# -*- coding: utf-8 -*-
"""人设一键备货就绪度（WP-6，2026-08-17）：档案/音色/相册/台词/说话指纹 五行聚合。

「建人设」的开箱体验升级：personas 页「备货」tab 一屏看清该人设四项资产
（档案 / 克隆音色 / 相册 / 预渲染台词库）+ 说话指纹（spoken_style 契约）各自
就绪没有、缺的给入口，汇总完成度。**纯只读聚合**——五个子系统各自的单一事实源
在哪，本模块就读哪（绝不另算一套）：

- 档案：PersonaManager 档案 dict（路由层取好传入）；
- 音色：``persona_voice.resolve_voice_cfg``（与 TTS 发送同一套解析）+
  参考音落盘存在性 + ``find_reference_text`` sidecar 逐字稿；
- 相册：``media_gap.collect_scene_supply``（注册相册 DB + FS 相册合并，TTL 缓存，
  与缺口报告同口径）；
- 台词库：``config/prerender_lines/<pid>.txt``（人设专属；``_common.txt`` 是共享
  基线，单独计数不顶专属）；
- 说话指纹：``platform/spoken_style/data/speech_prints.json`` 键 =
  ``resolve_spoken_name(persona)`` 逐字一致（规则契约：漏了=该人设只剩通用口语层）。

**适用性语义**（完成度分母只算适用项，防「永远到不了 100%」的假焦虑）：
- 相册行：人设显式 ``capabilities.photos: false``（运营关掉整条发图能力）→ 不适用；
  缺省/开 → 适用（备相册本就是开图能力的前置动作）;
- 指纹行：spoken_style 交付包不存在（未随部署）→ 不适用。

**刻意不做**：不自动往 ``speech_prints.json`` 写占位条目——占位会**消音**
「缺指纹」这个有用信号（空 guide 的占位＝人设只剩通用层，跟没有一样），而
本面板的点名行 + 一键复制模板片段已经承担了提醒职责；真值该由人写。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

#: 克隆声后端（音色行的「拟人化满配」判据；其余后端算「基础语音」）
CLONE_BACKENDS = frozenset({"avatar_clone", "minicpm_clone"})

#: 台词库目录 / 共享文件名——与 voice_prerender 同源（import 常量防漂移）
try:
    from src.ai.voice_prerender import COMMON_LINES_NAME, DEFAULT_LINES_DIR
except Exception:                                    # pragma: no cover - 极端兜底
    DEFAULT_LINES_DIR = "config/prerender_lines"
    COMMON_LINES_NAME = "_common.txt"


def count_lines_file(path: Any) -> int:
    """台词文件有效行数（跳过空行与 ``#`` 注释；文件缺失=0，绝不抛）。"""
    try:
        p = Path(path)
        if not p.is_file():
            return 0
        n = 0
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if s and not s.startswith("#"):
                n += 1
        return n
    except Exception:
        return 0


def speech_prints_file() -> Optional[Path]:
    """定位 speech_prints.json（复用 spoken_style_bridge 的包发现；缺包 None）。"""
    try:
        from src.ai.spoken_style_bridge import _find_platform_dir
        pdir = _find_platform_dir()
        if pdir is None:
            return None
        f = Path(pdir) / "spoken_style" / "data" / "speech_prints.json"
        return f if f.is_file() else None
    except Exception:
        return None


def load_speech_print_keys(path: Any = None) -> Optional[Set[str]]:
    """指纹键集合（``_`` 开头的说明键剔除）；包缺失/坏 JSON → None（=不适用）。

    P1-1（2026-08-18）起读**合并视图**（出厂 ∪ 数据区 overlay）——备货面板
    「保存到本实例」写进 overlay 后，本行就绪判定立即认账。显式 ``path`` 仍
    只读单文件（测试注入口，语义不变）。
    """
    try:
        if path:
            f = Path(path)
            if not f.is_file():
                return None
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return {str(k) for k in data.keys() if not str(k).startswith("_")}
        if speech_prints_file() is None:
            # 出厂件缺席（包未随部署）：overlay 有货也无消费者（L1/L4 都在包内），
            # 指纹行维持「不适用」——不给用户一个填了也不生效的假入口。
            return None
        from src.ai.speech_prints_overlay import merged_view
        data = merged_view()
        return {str(k) for k in data.keys() if not str(k).startswith("_")}
    except Exception:
        logger.debug("[persona_stock] speech_prints 读取失败", exc_info=True)
        return None


def speech_print_snippet(spoken_name: str) -> str:
    """缺指纹时给运营复制的条目模板（schema 对齐库内真实条目：print/catch/example）。"""
    entry = {
        "print": "（一句话说话习惯画像：语速/口头禅/接话方式/聊什么会话多）",
        "catch": ["（口头禅1）", "（口头禅2）"],
        "example": "（该角色 2-3 句口语语感范文）",
    }
    return json.dumps({str(spoken_name or "?"): entry}, ensure_ascii=False, indent=2)


def collect_stock_readiness(
    persona_id: str,
    persona: Optional[Dict[str, Any]],
    full_config: Optional[Dict[str, Any]],
    *,
    lines_dir: Any = None,
    prints_keys: Optional[Set[str]] = None,
    prints_available: Optional[bool] = None,
    supply: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """五行就绪度聚合（只读）。keyword 参数全部可注入＝可单测；路由层喂真实源。

    返回::

        {persona_id, items: {profile, voice, album, lines, speech_print},
         completion: 0.0-1.0, ready_count, applicable_count}

    每个 item：``{ready, applicable, ...detail}``；不适用项 ``ready`` 恒 False
    且不进完成度分母。
    """
    pid = str(persona_id or "").strip()
    cfg = full_config if isinstance(full_config, dict) else {}
    p = persona if isinstance(persona, dict) else None
    items: Dict[str, Dict[str, Any]] = {}

    # ── 1. 档案 ──────────────────────────────────────────────────────────
    missing_fields = []
    if p is not None:
        for field in ("name", "personality", "role"):
            if not str(p.get(field) or "").strip():
                missing_fields.append(field)
    items["profile"] = {
        "ready": p is not None,
        "applicable": True,
        "name": str((p or {}).get("name") or ""),
        "missing_fields": missing_fields,
    }

    # ── 2. 音色（与 TTS 发送同一套 resolve）──────────────────────────────
    voice: Dict[str, Any] = {"ready": False, "applicable": True, "backend": "",
                             "clone": False, "has_ref": False,
                             "has_transcript": False}
    try:
        from src.ai.persona_voice import resolve_voice_cfg
        vcfg = resolve_voice_cfg(pid, cfg) or {}
        vp = vcfg.get("voice_profile") if isinstance(
            vcfg.get("voice_profile"), dict) else {}
        backend = str(vcfg.get("backend") or vp.get("backend") or "").strip()
        voice["backend"] = backend
        voice["clone"] = backend in CLONE_BACKENDS
        # 参考音键正典=reference_audio_path（生产 voice_profile / avatar_voice
        # 消费口同款；2026-08-18 真机探针抓到首版误读 reference_audio——两个
        # 自洽夹具互相印证过了假绿，键契约门禁 test_key_contract_gates 由此而生，
        # 裸 reference_audio 无任何生产者、刻意不留兼容读）。值可为实例数据根
        # 相对路径（服务进程 CWD=数据根，is_file 正确；本模块经路由消费=服务进程）。
        ref = str(vp.get("reference_audio_path")
                  or vcfg.get("reference_audio_path") or "").strip()
        if ref:
            try:
                voice["has_ref"] = Path(ref).is_file()
            except Exception:
                voice["has_ref"] = False
            if voice["has_ref"]:
                try:
                    from src.ai.avatar_voice import find_reference_text
                    voice["has_transcript"] = bool(
                        str(find_reference_text(ref) or "").strip())
                except Exception:
                    voice["has_transcript"] = False
        if voice["clone"]:
            voice["ready"] = voice["has_ref"]
        else:
            # 非克隆后端（edge/openai/…）：配了 backend（缺省仍会有全局层 backend，
            # 故再看 voice/voice_profile 是否有人设级痕迹）→ 算「基础语音」就绪
            voice["ready"] = bool(backend) and bool(
                str(vcfg.get("voice") or vp.get("voice") or "").strip())
    except Exception:
        logger.debug("[persona_stock] voice 解析失败", exc_info=True)
    items["voice"] = voice

    # ── 3. 相册（显式关发图能力 → 不适用）────────────────────────────────
    caps = (p or {}).get("capabilities") or {}
    photos_off = caps.get("photos") is False
    sup = supply if isinstance(supply, dict) else {}
    own = sup.get(pid) or {}
    shared = sup.get("") or {}
    items["album"] = {
        "ready": (not photos_off) and sum(own.values()) > 0,
        "applicable": not photos_off,
        "count": int(sum(own.values())),
        "shared_count": int(sum(shared.values())),
        "scenes": sorted(k for k, v in own.items() if v),
    }

    # ── 4. 台词库（人设专属文件；_common 单独计数不顶专属）───────────────
    ldir = Path(lines_dir) if lines_dir else Path(DEFAULT_LINES_DIR)
    own_lines = count_lines_file(ldir / f"{pid}.txt") if pid else 0
    common_lines = count_lines_file(ldir / COMMON_LINES_NAME)
    items["lines"] = {
        "ready": own_lines > 0,
        "applicable": True,
        "count": own_lines,
        "common_count": common_lines,
    }

    # ── 5. 说话指纹（键=口称名逐字一致；包缺失=不适用）───────────────────
    keys = prints_keys
    if keys is None and prints_available is not False:
        keys = load_speech_print_keys()
    available = keys is not None if prints_available is None else bool(
        prints_available)
    spoken = ""
    if p is not None:
        try:
            from src.utils.persona_manager import PersonaManager
            spoken = str(PersonaManager.resolve_spoken_name(p) or "")
        except Exception:
            spoken = str(p.get("name") or "")
    hit = bool(available and keys and spoken and spoken in keys)
    items["speech_print"] = {
        "ready": hit,
        "applicable": available,
        "spoken_name": spoken,
        "snippet": "" if (hit or not available) else speech_print_snippet(spoken),
    }

    applicable = [k for k, v in items.items() if v.get("applicable")]
    ready = [k for k in applicable if items[k].get("ready")]
    return {
        "persona_id": pid,
        "items": items,
        "ready_count": len(ready),
        "applicable_count": len(applicable),
        "completion": (len(ready) / len(applicable)) if applicable else 0.0,
    }
