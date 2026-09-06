# -*- coding: utf-8 -*-
"""人设「上线准备」清单（WP-6 备货就绪度 → #189 改口径，2026-09-05）。

personas 页「上线准备」tab 一屏看清该人设能不能开始接客：**清单四项**
档案 / 声音（或选不发语音）/ 相册（仅开启照片能力时）/ 绑定账号，缺的给入口，
汇总完成度。台词库（预渲染缓存）与说话指纹（spoken_style 包）是研发/运营侧
机制、不是用户功能（#189 skuio：台词库「专属 0/共享 11」把就绪度卡在 75%）——
两项仍聚合（``internal=True``）供运营面板/高级区读，但**不进清单也不进分母**。
**纯只读聚合**——各子系统的单一事实源在哪，本模块就读哪（绝不另算一套）：

- 档案：PersonaManager 档案 dict（路由层取好传入）；
- 音色：``persona_voice.resolve_voice_cfg``（与 TTS 发送同一套解析）+
  参考音落盘存在性 + ``find_reference_text`` sidecar 逐字稿；
- 相册：``media_gap.collect_scene_supply``（注册相册 DB + FS 相册合并，TTL 缓存，
  与缺口报告同口径）；
- 台词库：``config/prerender_lines/<pid>.txt``（人设专属；``_common.txt`` 是共享
  基线，单独计数不顶专属）；
- 说话指纹：``platform/spoken_style/data/speech_prints.json`` 键 =
  ``resolve_spoken_name(persona)`` 逐字一致（规则契约：漏了=该人设只剩通用口语层）。

- 绑定账号：运行时账号注册表 ``meta.persona_ids`` + config 各平台 ``persona_ids``
  + PersonaManager 会话绑定 + 全局默认人设（与 ``/api/personas/status`` 的
  ``profiles_in_use`` 同一组源，按单人设收敛）。

**适用性语义**（完成度分母只算适用项，防「永远到不了 100%」的假焦虑）：
- 声音行：人设 ``voice_profile.enabled: false`` 或合并后语音配置 ``enabled: false``
  （部署没开语音回复且人设未单独开）→ 「选了不发语音」，不适用；
- 相册行：``capabilities.photos`` 显式 true 才适用（发图能力缺省关——关着时备相册
  素材也发不出去，不该催人备）；
- 台词库 / 指纹行：``internal=True``，永不进分母；指纹另带 ``applicable``＝包是否
  随部署（前端只在包可用时于「高级」区渲染，不可用整行不出现）。

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


#: 清单项顺序（前端按此渲染；不在此列的 item 都是 internal）
CHECKLIST_ITEMS = ("profile", "voice", "album", "binding")

#: 运行时账号注册表里参与「绑定账号」判定的平台
_BINDING_PLATFORMS = ("telegram", "whatsapp", "line", "messenger")


def _cfg_account_pids(cfg: Dict[str, Any], section: str) -> Dict[str, Set[str]]:
    """config 段 ``<section>.accounts[].persona_ids`` + 扁平 ``<section>.persona_ids``
    → {account_id: {pid,...}}（扁平形态记到 ``default`` 槽）。绝不抛。"""
    out: Dict[str, Set[str]] = {}
    try:
        sec = cfg.get(section) or {}
        if not isinstance(sec, dict):
            return out
        for acc in (sec.get("accounts") or []):
            if not isinstance(acc, dict):
                continue
            aid = str(acc.get("account_id") or acc.get("adb_serial")
                      or acc.get("id") or "").strip()
            pids = {str(x).strip() for x in (acc.get("persona_ids") or []) if str(x).strip()}
            if aid and pids:
                out.setdefault(aid, set()).update(pids)
        flat = {str(x).strip() for x in (sec.get("persona_ids") or []) if str(x).strip()}
        if flat:
            out.setdefault("default", set()).update(flat)
    except Exception:
        pass
    return out


def _empty_usage() -> Dict[str, Any]:
    return {"account_count": 0, "chat_count": 0, "is_default": False, "accounts": []}


def collect_binding_usage_map(pm: Any = None,
                              full_config: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """四源一次扫完 → ``{persona_id: usage}``（L-2 #204：人设卡片胶囊按 N 个人设批量取）。

    与 ``collect_binding_usage`` 同一组源、同一语义（后者即本函数按 pid 取值）：
    运行时注册表各平台 ``meta.persona_ids``（removed 行跳过）、config 各平台
    ``persona_ids``、PersonaManager 会话绑定（引用式 ``_profile_ref`` / 内联 ``id``）、
    全局默认人设。任一源读失败按 0 计，绝不抛。
    """
    cfg = full_config if isinstance(full_config, dict) else {}
    seen: Dict[str, Set[tuple]] = {}
    chats: Dict[str, int] = {}
    default_pid = ""

    def _acc(pid: str, plat: str, aid: str) -> None:
        seen.setdefault(pid, set()).add((plat, aid))

    try:
        from src.integrations.account_registry import get_account_registry, parse_persona_ids
        reg = get_account_registry()
        for plat in _BINDING_PLATFORMS:
            for row in reg.list(plat) or []:
                if str(row.get("status") or "") == "removed":
                    continue
                aid = str(row.get("account_id") or "").strip()
                if not aid:
                    continue
                for pid in parse_persona_ids(row.get("meta")):
                    if str(pid or "").strip():
                        _acc(str(pid).strip(), plat, aid)
    except Exception:
        logger.debug("[persona_stock] 账号注册表读取失败（按 0 计）", exc_info=True)
    for section, plat in (("telegram", "telegram"), ("whatsapp_rpa", "whatsapp"),
                          ("messenger_rpa", "messenger")):
        for aid, pids in _cfg_account_pids(cfg, section).items():
            for pid in pids:
                _acc(pid, plat, aid)
    if pm is not None:
        try:
            for v in (pm.get_all_chat_bindings() or {}).values():
                if not isinstance(v, dict):
                    continue
                ref = str(v.get("_profile_ref") or "").strip() or str(v.get("id") or "").strip()
                if ref:
                    chats[ref] = chats.get(ref, 0) + 1
        except Exception:
            pass
        try:
            dom = getattr(pm, "_domain_persona", None)
            if isinstance(dom, dict) and str(dom.get("id") or ""):
                default_pid = str(dom.get("id"))
            elif not dom:
                dflt = pm.get_persona("")
                if isinstance(dflt, dict):
                    default_pid = str(dflt.get("id") or "")
        except Exception:
            pass
    out: Dict[str, Dict[str, Any]] = {}
    for pid in set(seen) | set(chats) | ({default_pid} if default_pid else set()):
        accounts = sorted(seen.get(pid, set()))
        out[pid] = {
            "account_count": len(accounts),
            "chat_count": int(chats.get(pid, 0)),
            "is_default": pid == default_pid,
            "accounts": [list(a) for a in accounts[:8]],
        }
    return out


def collect_binding_usage(persona_id: str, pm: Any = None,
                          full_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """该人设被哪些地方用着（只读；任一源读失败按 0 计，绝不抛）。

    返回 ``{account_count, chat_count, is_default, accounts: [(platform, account_id)…≤8]}``。
    源与 ``/api/personas/status.profiles_in_use`` 同一组（见 ``collect_binding_usage_map``）。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return _empty_usage()
    return dict(collect_binding_usage_map(pm, full_config).get(pid) or _empty_usage())


def collect_stock_readiness(
    persona_id: str,
    persona: Optional[Dict[str, Any]],
    full_config: Optional[Dict[str, Any]],
    *,
    lines_dir: Any = None,
    prints_keys: Optional[Set[str]] = None,
    prints_available: Optional[bool] = None,
    supply: Optional[Dict[str, Dict[str, int]]] = None,
    binding: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """「上线准备」聚合（只读）。keyword 参数全部可注入＝可单测；路由层喂真实源。

    返回::

        {persona_id, checklist: [profile, voice, album, binding],
         items: {profile, voice, album, binding, lines, speech_print},
         completion: 0.0-1.0, ready_count, applicable_count}

    每个 item：``{ready, applicable, ...detail}``；``internal=True`` 的项（lines /
    speech_print）与不适用项都不进完成度分母，不适用项 ``ready`` 恒 False。
    ``binding`` 缺省（None）＝路由层没喂 → 该行 ``applicable=False`` 不进分母
    （旧调用方零行为变化）。
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

    # ── 2. 声音（与 TTS 发送同一套 resolve；选了不发语音 → 不适用）──────────
    voice: Dict[str, Any] = {"ready": False, "applicable": True, "backend": "",
                             "clone": False, "has_ref": False,
                             "has_transcript": False, "voice_off": False}
    try:
        from src.ai.persona_voice import resolve_voice_cfg
        vcfg = resolve_voice_cfg(pid, cfg) or {}
        vp = vcfg.get("voice_profile") if isinstance(
            vcfg.get("voice_profile"), dict) else {}
        # #189「声音（或选不发语音）」：人设档案 voice_profile.enabled 显式 false，
        # 或合并后（全局 voice_reply/voice_output + 人设层）enabled 显式 false
        # ＝这个人设/部署就是不发语音，声音行不该催人配。
        _pvp = (p or {}).get("voice_profile") if isinstance((p or {}).get("voice_profile"), dict) else {}
        if _pvp.get("enabled") is False or (vcfg and vcfg.get("enabled") is False):
            voice["voice_off"] = True
            voice["applicable"] = False
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
        if not voice["applicable"]:
            voice["ready"] = False
    except Exception:
        logger.debug("[persona_stock] voice 解析失败", exc_info=True)
    items["voice"] = voice

    # ── 3. 相册（仅开启照片能力时适用；capabilities.photos 缺省关）──────────
    caps = (p or {}).get("capabilities") or {}
    photos_on = isinstance(caps, dict) and caps.get("photos") is True
    sup = supply if isinstance(supply, dict) else {}
    own = sup.get(pid) or {}
    shared = sup.get("") or {}
    items["album"] = {
        "ready": photos_on and sum(own.values()) > 0,
        "applicable": photos_on,
        "photos_on": photos_on,
        "count": int(sum(own.values())),
        "shared_count": int(sum(shared.values())),
        "scenes": sorted(k for k, v in own.items() if v),
    }

    # ── 4. 绑定账号（路由层喂 collect_binding_usage；没喂＝不适用不进分母）──
    b = binding if isinstance(binding, dict) else None
    items["binding"] = {
        "ready": bool(b) and (int(b.get("account_count") or 0) > 0
                              or int(b.get("chat_count") or 0) > 0
                              or bool(b.get("is_default"))),
        "applicable": b is not None,
        "account_count": int((b or {}).get("account_count") or 0),
        "chat_count": int((b or {}).get("chat_count") or 0),
        "is_default": bool((b or {}).get("is_default")),
        "accounts": list((b or {}).get("accounts") or []),
    }

    # ── 5. 台词库（internal：预渲染缓存，运营面板读；不进清单/分母）────────
    ldir = Path(lines_dir) if lines_dir else Path(DEFAULT_LINES_DIR)
    own_lines = count_lines_file(ldir / f"{pid}.txt") if pid else 0
    common_lines = count_lines_file(ldir / COMMON_LINES_NAME)
    items["lines"] = {
        "ready": own_lines > 0,
        "applicable": False,
        "internal": True,
        "count": own_lines,
        "common_count": common_lines,
    }

    # ── 6. 说话指纹（internal；applicable＝包随部署，前端据此决定高级区是否渲染）──
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
        "internal": True,
        "spoken_name": spoken,
        "snippet": "" if (hit or not available) else speech_print_snippet(spoken),
    }

    applicable = [k for k in CHECKLIST_ITEMS
                  if items[k].get("applicable") and not items[k].get("internal")]
    ready = [k for k in applicable if items[k].get("ready")]
    return {
        "persona_id": pid,
        "checklist": list(CHECKLIST_ITEMS),
        "items": items,
        "ready_count": len(ready),
        "applicable_count": len(applicable),
        "completion": (len(ready) / len(applicable)) if applicable else 0.0,
    }
