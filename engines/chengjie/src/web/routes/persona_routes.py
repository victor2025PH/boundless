"""
Persona management API routes — extracted from admin.py.

Endpoints:
- GET  /api/persona           — get current persona for a chat
- GET  /api/persona/bindings  — list all chat-persona bindings
- POST /api/persona/bind      — bind a persona to a chat
- POST /api/persona/unbind    — unbind a persona from a chat
- POST /api/persona/update-default — update the default persona
- GET  /api/persona/preview-prompt — preview assembled system prompt
"""

from fastapi import Depends, HTTPException, Request
from src.web.web_i18n import tr

_ROLE_VIEWER = "viewer"
_ROLE_MASTER = "master"


def _save_binding_patch(cm, patch) -> bool:
    """账号人设绑定的**扁平键**持久化：最小 patch 落 config.local.yaml overlay
    （保住主 config.yaml 注释/结构）。兜底：方法缺失（简化 fake）或返回非 bool
    （MagicMock 桩）→ 整文件 save()。

    注意：仅限扁平 ``<platform>.persona_ids``。accounts **列表内**成员的改动
    不走此路——overlay 的 list 整体替换语义会把整个 accounts 快照固化进
    overlay，从此遮蔽运营在主配置里的账号增删（双源真相事故），那些调用点
    保持整文件 save()。"""
    saver = getattr(cm, "save_overlay_patch", None)
    ok = saver(patch) if callable(saver) else None
    if not isinstance(ok, bool):
        ok = cm.save()
    return bool(ok)


# ── legacy 债三件套（模块级：drafts_routes 的 metrics 也要用）────────────────

def legacy_binding_entries(pm) -> dict:
    """收集全部 legacy 绑定键 → {key: {profile_id, kind, inline_name}}。

    reference 优先于 inline（同键两处都有时以引用为准，与解析链一致）；
    3 段会话覆写键不算债，跳过。盘点 GET / 批量收官 / metrics 水位共用这一份口径。
    """
    from src.ai.persona_voice import is_conv_binding_key
    entries: dict = {}
    for k, pid in (getattr(pm, "_chat_bindings", {}) or {}).items():
        if is_conv_binding_key(k):
            continue
        entries[str(k)] = {"profile_id": str(pid or ""), "kind": "reference"}
    for k, p in (getattr(pm, "_chat_personas", {}) or {}).items():
        ks = str(k)
        if ks in entries or is_conv_binding_key(ks):
            continue
        entries[ks] = {
            "profile_id": str((p or {}).get("id") or ""),
            "kind": "inline",
            "inline_name": str((p or {}).get("name") or ""),
        }
    return entries


def mrpa_managed_cids(app) -> set:
    """Messenger RPA 托管的 PM 绑定键集合（合成 cid，best-effort）。

    Messenger RPA 的 per-chat 人设覆写以自己的 SQLite 为 SSOT，启动时经
    ``_warmup_pm_chat_bindings`` 回灌进 PM——键形态是 ``mrpa_chat_cid`` 合成的
    **纯数字**（与 TG 用户 id 形状无法区分）。这些键不是债：在 PM 侧删除会在
    下次重启被回灌，且 runner 对有绑定的 chat 会清空 account_persona_id 让
    绑定真赢（「被账号层压制」的判定对它们不成立）。清理链路必须把它们
    隔离出去。服务未注入 / 任一账号读取失败 → 尽力集合（宁可漏标不误标；
    漏标的兜底是「查无收件箱落点 → skip 留人工」）。
    """
    out: set = set()
    try:
        svc = getattr(getattr(app, "state", None), "messenger_rpa_service", None)
        if svc is None:
            return out
        from src.integrations.messenger_rpa.state_store import mrpa_chat_cid
        reg = getattr(svc, "_account_registry", None)
        merged = getattr(svc, "_merged_cfg", {}) or {}
        if reg is None:
            return out
        for ctx in reg.all_contexts():
            try:
                store = ctx.state_store()
                mc = ctx.merged_config(merged) or {}
                prefix = mc.get("chat_key_prefix") or "messenger_rpa"
                for ov in (store.list_chat_persona_overrides() or []):
                    chat_name = str((ov or {}).get("chat_name") or "").strip()
                    if chat_name:
                        out.add(str(mrpa_chat_cid(chat_name, prefix)))
            except Exception:
                continue
    except Exception:
        return out
    return out


def legacy_debt_snapshot(app) -> int:
    """剩余 legacy 债条数（排除 RPA 托管键）——metrics/ops 卡的活水位。

    进程计数器（persona_override_stats）只记「本次启动以来清了多少」，
    这里补「现在还剩多少」：债清零后若回升，说明有路径在重新制造
    peer-global 绑定，看板直接可见。
    """
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager.get_instance()
    entries = legacy_binding_entries(pm)
    managed = mrpa_managed_cids(app)
    return sum(1 for k in entries if k not in managed)


def register_persona_routes(app, auth_dep, audit_store=None, config_manager=None):
    """Register persona management API endpoints。config_manager 用于人设持久化。"""

    # ── 启动时加载（P5-D: 三层顺序）────────────────────────────────────────────
    # 1. config.yaml::personas.profiles  — 基础层
    # 2. personas.yaml                   — 规范运营定义（git 可追蹤，新层）
    # 3. profiles_runtime.yaml           — 会话运行时覆盖（最高优先）
    # 4. bindings_runtime.yaml           — 聊天绑定
    import logging as _logging
    _plog = _logging.getLogger("ai_chat_assistant.persona_routes")
    try:
        from pathlib import Path as _Path
        from src.utils.persona_manager import PersonaManager as _PM
        _pm_init = _PM.get_instance()
        _cfg = getattr(config_manager, "config", None) or {}
        _n1 = _pm_init.load_profiles_from_config(_cfg)             # layer 1: config base
        _n2 = 0
        if config_manager:
            _n2 = _pm_init.load_personas_canonical(config_manager)  # layer 2: canonical yaml
        _cp = getattr(config_manager, "config_path", None)
        _n3 = 0
        if _cp:
            _n3 = _pm_init.load_profiles_runtime(_Path(_cp), _cfg)    # layer 3: session overrides
            _pm_init.load_chat_bindings_runtime(_Path(_cp), _cfg)     # layer 4: bindings
        # 可观测：人设加载结果 + 语音 backend 体检。历史隐性事故——加载静默失败时
        # resolve 会回落默认 TTS、发出非克隆机器音（"声音太假"），过去无任何日志。
        # 这里把"加载了几个 / 几个配了真声克隆 backend"打到日志，便于排障。
        try:
            _all = _pm_init._profile_personas or {}
            _CLONE_BACKENDS = ("minicpm_clone", "avatar_clone", "fish_speech", "coqui_http", "elevenlabs", "xtts")
            _clone = sum(
                1 for _p in _all.values()
                if str(((_p or {}).get("voice_profile") or {}).get("backend") or "").strip().lower()
                in _CLONE_BACKENDS
            )
            _plog.info(
                "人设加载完成: config=%d canonical=%d runtime=%d；共 %d 个人设，%d 个配了克隆/真声 backend",
                _n1, _n2, _n3, len(_all), _clone,
            )
        except Exception:
            pass
    except Exception as _e:
        _plog.warning(
            "人设运行时加载失败（resolve 将回落默认 TTS，可能发出非克隆机器音）: %s",
            _e, exc_info=True,
        )

    # ── 人设使用计数账本（近7日活跃统计）───────────────────────────────────
    # DB 与 config.yaml 同目录（persona_usage.db）；init 失败只 debug——
    # 统计属旁路能力，绝不影响路由注册与回复链路（未 init 时模块内部会惰性回落默认路径）。
    try:
        _cp_usage = getattr(config_manager, "config_path", None) if config_manager else None
        if _cp_usage:
            from pathlib import Path as _UsagePath
            from src.utils import persona_usage as _persona_usage
            _persona_usage.init(_UsagePath(_cp_usage).parent / "persona_usage.db")
    except Exception:
        _plog.debug("persona_usage init 失败（忽略）", exc_info=True)

    @app.get("/api/persona")
    async def api_persona_get(request: Request, chat_id: str = "",
                               _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona(chat_id)
        return {
            "persona": persona,
            "chat_id": chat_id,
            "is_default": chat_id == "" or not pm.has_chat_binding(str(chat_id)),
        }

    @app.get("/api/persona/bindings")
    async def api_persona_bindings(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {"bindings": pm.get_all_chat_bindings()}

    def _check_write_role(request: Request):
        """Raises 403 if session role is viewer (read-only)."""
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.persona.readonly_no_edit"))

    def _check_master_role(request: Request):
        """Raises 403 unless session role is master."""
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role and role != _ROLE_MASTER:
            raise HTTPException(403, tr(request, "err.perm.master_only"))

    # ── 会话级人设覆写（2026-07-26 方案 A）共用小件 ──────────────────────
    def _live_config(request: Request) -> dict:
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        return getattr(cm, "config", None) or {}

    def _record_override_action(kind: str) -> None:
        """人设治理动作观测（bind/unbind/整号/legacy 清理；best-effort 不抛）。"""
        try:
            from src.ai.persona_override_stats import get_persona_override_stats
            get_persona_override_stats().record_action(kind)
        except Exception:
            pass

    def _record_override_fail(op: str, reason: str) -> None:
        """治理动作业务层拒绝观测（P1 换绑漏斗失败侧；best-effort 不抛）。

        与成功侧 record_action 合成「尝试→成功/失败按原因」漏斗，回答市场侧
        「多少人想用被挡住」。CSRF 中间件层拒绝不经路由，由 csrf_stats 另计。
        """
        try:
            from src.ai.persona_override_stats import get_persona_override_stats
            get_persona_override_stats().record_fail(op, reason)
        except Exception:
            pass

    def _parse_conv_ref(data: dict) -> tuple:
        """从请求体取 (platform, account_id, chat_key)。

        优先显式三元组；缺则解析 ``conversation_id``（platform:account:chat_key，
        chat_key 自身可含冒号 → split 限 2 刀）。返回 ("","","") 表示解析失败。
        """
        plat = str(data.get("platform") or "").strip()
        acct = str(data.get("account_id") or "").strip()
        ck = str(data.get("chat_key") or "").strip()
        if plat and acct and ck:
            return plat, acct, ck
        cid = str(data.get("conversation_id") or "").strip()
        parts = cid.split(":", 2)
        if len(parts) == 3 and all(p.strip() for p in parts):
            return parts[0].strip(), parts[1].strip(), parts[2].strip()
        return "", "", ""

    def _persona_brief(pm, pid: str) -> dict:
        """人设摘要（id/name/role/voice）——effective API 响应用，找不到返回 None。"""
        p = pm.get_persona_by_id(pid) if pid else None
        if not isinstance(p, dict):
            return None
        vp = p.get("voice_profile") or {}
        return {
            "id": str(p.get("id") or pid),
            "name": str(p.get("name") or pid),
            "role": str(p.get("role") or ""),
            "has_voice": bool(vp.get("enabled") or vp.get("voice") or vp.get("backend")),
        }

    @app.get("/api/persona/effective")
    async def api_persona_effective(
        request: Request,
        conversation_id: str = "",
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        _=Depends(auth_dep),
    ):
        """会话生效人设（与出站链同一 resolver 的读侧真相）。

        返回四层全景：effective（真正生效者+tier）/ conv（会话覆写）/
        account（账号人设）/ legacy（旧 peer-global 绑定）+ 冲突态 +
        弹窗口径素材（has_outbound）。前端 cp-persona 据此渲染"当前生效"横幅，
        彻底消灭「面板绑了陈默、出站却是林小雨」的两套真相。
        """
        data = {
            "conversation_id": conversation_id,
            "platform": platform, "account_id": account_id, "chat_key": chat_key,
        }
        plat, acct, ck = _parse_conv_ref(data)
        if not (plat and acct and ck):
            raise HTTPException(400, tr(request, "err.persona.conv_ref_required"))
        from src.ai.persona_voice import (
            conv_binding_key,
            conv_override_enabled,
            resolve_effective_persona,
        )
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        cfg = _live_config(request)
        enabled = conv_override_enabled(cfg)
        conv_key = conv_binding_key(plat, acct, ck)

        # 三个独立层（不含回落语义，纯粹"各层绑了谁"）
        conv_pid = pm.get_chat_binding_ref(conv_key)
        from src.ai.persona_voice import resolve_account_persona_id
        acct_pid = resolve_account_persona_id(cfg, plat, acct)
        legacy_pid = pm.get_chat_binding_ref(ck)
        if not legacy_pid:
            try:
                legacy_pid = str(
                    ((getattr(pm, "_chat_personas", {}) or {}).get(ck) or {}).get("id") or ""
                )
            except Exception:
                legacy_pid = ""

        # 生效者：与出站链同一 resolver；(pid="") → PersonaManager legacy/domain 链
        eff_pid, eff_tier = resolve_effective_persona(cfg, plat, acct, ck)
        if eff_pid:
            eff = _persona_brief(pm, eff_pid) or {"id": eff_pid, "name": eff_pid,
                                                  "role": "", "has_voice": False}
            eff["tier"] = eff_tier
        else:
            _p, _tier = pm.get_persona_with_tier(ck, "")
            eff = {
                "id": str((_p or {}).get("id") or ""),
                "name": str((_p or {}).get("name") or ""),
                "role": str((_p or {}).get("role") or ""),
                "has_voice": bool((( _p or {}).get("voice_profile") or {}).get("enabled")),
                "tier": _tier,
            }

        # 弹窗口径素材：这条会话是否已有出站历史（近 50 条内任一 out 即算）
        has_outbound = False
        try:
            store = getattr(request.app.state, "inbox_store", None)
            if store is not None:
                cid_full = f"{plat}:{acct}:{ck}"
                for m in store.list_recent_messages(cid_full, limit=50):
                    if str(m.get("direction") or "") == "out":
                        has_outbound = True
                        break
        except Exception:
            has_outbound = False

        # 语音灰度素材（换绑弹窗提示行）：allowlist 非空 = 名单外人设自动语音
        # 回落文本/通用声（与 voice_autosend.persona_allowed_for_voice 同口径）。
        _vb = ((cfg.get("inbox") or {}).get("l2_autosend") or {}).get("voice") or {}
        _allow_raw = _vb.get("persona_allowlist") or []
        if not isinstance(_allow_raw, (list, tuple)):
            _allow_raw = []
        voice_autosend = {
            "enabled": bool(_vb.get("enabled")),
            "allowlist": [str(x).strip() for x in _allow_raw if str(x).strip()],
        }

        return {
            "ok": True,
            "enabled": enabled,
            "platform": plat, "account_id": acct, "chat_key": ck,
            "conversation_id": f"{plat}:{acct}:{ck}",
            "effective": eff,
            "conv": _persona_brief(pm, conv_pid),
            "account": _persona_brief(pm, acct_pid),
            "legacy": _persona_brief(pm, legacy_pid),
            # 旧 peer-global 绑定存在但没赢（被账号/会话层压制）→ 前端黄条讲清原因
            "legacy_suppressed": bool(
                legacy_pid and eff.get("tier") not in ("chat_binding",)
                and legacy_pid != eff.get("id")
            ),
            "has_outbound": has_outbound,
            "voice_autosend": voice_autosend,
        }

    @app.post("/api/persona/bind")
    async def api_persona_bind(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        scope = str(data.get("scope") or "").strip().lower()
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        actor = request.session.get("username", "web_admin")

        # ── 会话级覆写（scope=conversation，2026-07-26 方案 A）────────────
        # 只影响 platform:account:chat_key 这一条会话；仅引用式（profile 必须存在），
        # 不收内联快照——覆写的语义就是"换成另一个已定义的人设"。
        if scope == "conversation":
            cfg = _live_config(request)
            from src.ai.persona_voice import conv_binding_key, conv_override_enabled
            if not conv_override_enabled(cfg):
                _record_override_fail("bind_conv", "disabled")
                raise HTTPException(
                    400, tr(request, "err.persona.conv_override_disabled"))
            plat, acct, ck = _parse_conv_ref(data)
            if not (plat and acct and ck):
                _record_override_fail("bind_conv", "badref")
                raise HTTPException(400, tr(request, "err.persona.conv_ref_required"))
            _pid = str(
                data.get("profile_id")
                or (data.get("persona") or {}).get("id") or ""
            ).strip()
            if not _pid or pm.get_persona_by_id(_pid) is None:
                _record_override_fail("bind_conv", "profile_missing")
                raise HTTPException(
                    404, tr(request, "err.persona.profile_not_found", name=_pid or "?"))
            key = conv_binding_key(plat, acct, ck)
            _prev = pm.get_chat_binding_ref(key)
            pm.bind_chat_persona_by_profile_id(key, _pid)
            try:
                cm = getattr(request.app.state, "config_manager", None) or config_manager
                pm.persist_chat_bindings(cm)
            except Exception:
                pass
            if audit_store:
                audit_store.log(actor, "persona_bind",
                              f"scope=conv conv={key} from={_prev or '-'} to={_pid}")
            _record_override_action("bind_conv")
            return {"ok": True, "scope": "conversation", "conversation_key": key,
                    "from": _prev or "", "to": _pid}

        # ── legacy peer-global 绑定（原语义不变，audit 补 from→to）────────
        chat_id = data.get("chat_id")
        persona_data = data.get("persona")
        if not chat_id or not persona_data:
            raise HTTPException(400, "chat_id and persona required")
        _prev_legacy = pm.get_chat_binding_ref(str(chat_id))
        # 优先"引用式"绑定（只存 profile_id，实时解析当前 profile 内容）：这样之后在页面编辑
        # 该人设，所有绑定会话立即跟随，不再出现"改了人设但会话还用旧快照"的冲突。
        # 仅当 persona 带合法 profile id 且该 profile 存在时走引用；否则回落旧内联快照（自定义/无 id）。
        _pid = str((persona_data or {}).get("id") or "").strip()
        if _pid and _pid in getattr(pm, "_profile_personas", {}):
            pm.bind_chat_persona_by_profile_id(str(chat_id), _pid)
        else:
            pm.bind_chat_persona(str(chat_id), persona_data)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_chat_bindings(cm)
        except Exception:
            pass
        if audit_store:
            audit_store.log(actor, "persona_bind",
                          f"chat={chat_id} from={_prev_legacy or '-'} "
                          f"name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.post("/api/persona/unbind")
    async def api_persona_unbind(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        scope = str(data.get("scope") or "").strip().lower()
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        actor = request.session.get("username", "web_admin")

        # ── 会话级覆写解除：只删 3 段键，legacy/账号层不动 ─────────────────
        if scope == "conversation":
            plat, acct, ck = _parse_conv_ref(data)
            if not (plat and acct and ck):
                _record_override_fail("unbind_conv", "badref")
                raise HTTPException(400, tr(request, "err.persona.conv_ref_required"))
            from src.ai.persona_voice import conv_binding_key
            key = conv_binding_key(plat, acct, ck)
            _prev = pm.get_chat_binding_ref(key)
            pm.unbind_chat_persona(key)
            try:
                cm = getattr(request.app.state, "config_manager", None) or config_manager
                pm.persist_chat_bindings(cm)
            except Exception:
                pass
            if audit_store:
                audit_store.log(actor, "persona_unbind",
                              f"scope=conv conv={key} from={_prev or '-'}")
            _record_override_action("unbind_conv")
            return {"ok": True, "scope": "conversation", "from": _prev or ""}

        chat_id = data.get("chat_id")
        if not chat_id:
            raise HTTPException(400, "chat_id required")
        _prev_legacy = pm.get_chat_binding_ref(str(chat_id))
        pm.unbind_chat_persona(str(chat_id))
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_chat_bindings(cm)
        except Exception:
            pass
        if audit_store:
            audit_store.log(actor, "persona_unbind",
                          f"chat={chat_id} from={_prev_legacy or '-'}")
        return {"ok": True}

    # ── 账号级整号换绑（写 account registry，QR 协议号的 SSOT）────────────
    @app.post("/api/persona/account-persona")
    async def api_account_persona_set(request: Request, _=Depends(auth_dep)):
        """给任意平台账号换绑/清除账号级人设（写 registry ``meta.persona_id``）。

        与既有 ``/api/personas/tg-account/{id}/assign-profile``（只写 config）互补：
        QR 扫码登录的协议号不在 config 里，registry 才是它们的人设 SSOT
        （``resolve_account_persona_id`` 第 1 优先级）。写入要点：
        - ``merge_meta=True`` 锁内原子合并——绝不整块覆盖 meta
          （2026-07-23 baileys 事故教训：别抹掉 session_string）；
        - ``persona_id_auto=False`` 显式绑定标记——config 目录同步
          （sync_to_account_registry）看到该标记后不会用 config 值刷回。
        Body: {platform, account_id, profile_id}  profile_id 空 = 清除回默认。
        权限：写权限即可（运营拍板：账号级换绑不限 master）。
        """
        _check_write_role(request)
        data = await request.json()
        plat = str(data.get("platform") or "").strip().lower()
        acct = str(data.get("account_id") or "").strip()
        profile_id = str(data.get("profile_id") or "").strip()
        if not plat or not acct:
            _record_override_fail("account_set", "badref")
            raise HTTPException(400, tr(request, "err.persona.account_ref_required"))
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            _record_override_fail("account_set", "profile_missing")
            raise HTTPException(
                404, tr(request, "err.persona.profile_not_found", name=profile_id))
        try:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        except Exception:
            registry = None
        if registry is None:
            _record_override_fail("account_set", "registry_unavailable")
            raise HTTPException(503, tr(request, "err.persona.registry_unavailable"))
        _prev = ""
        try:
            _row = registry.get(plat, acct) or {}
            _meta = _row.get("meta") or {}
            _prev = str(_meta.get("persona_id") or "").strip()
            if not _prev:
                for _p in (_meta.get("persona_ids") or []):
                    _prev = str(_p or "").strip()
                    if _prev:
                        break
        except Exception:
            _prev = ""
        registry.upsert(
            plat, acct,
            meta={
                "persona_id": profile_id,
                "persona_ids": [profile_id] if profile_id else [],
                "persona_id_auto": False,
            },
            merge_meta=True,
        )
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "account_persona_set",
                          f"{plat}:{acct} from={_prev or '-'} to={profile_id or '(cleared)'}")
        _record_override_action("account_set")
        return {"ok": True, "platform": plat, "account_id": acct,
                "from": _prev or "", "to": profile_id}

    # ── legacy peer-global 绑定清理（P3：升级为会话级 / 清除）────────────
    # 2026-07-24 双账号串音修复后，legacy 绑定在有账号人设时恒被压制=「活债」。
    # 这里给运营一个盘点+收官工具：看清每条 legacy 键落在哪些会话、是否还生效，
    # 按条升级成显式 3 段会话覆写（语义保真）或直接清除（认账号人设）。

    def _legacy_conversations(request: Request, key: str) -> list:
        """按 legacy 绑定键反查 inbox 会话落点（含压制态判定）。"""
        from src.ai.persona_voice import (
            conv_binding_key,
            conv_override_enabled,
            resolve_account_persona_id,
        )
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        cfg = _live_config(request)
        enabled = conv_override_enabled(cfg)
        store = getattr(request.app.state, "inbox_store", None)
        rows = []
        if store is not None and hasattr(store, "find_conversations_by_chat_key"):
            try:
                rows = store.find_conversations_by_chat_key(key) or []
            except Exception:
                rows = []
        out = []
        for r in rows:
            plat = str(r.get("platform") or "")
            acct = str(r.get("account_id") or "")
            ck = str(r.get("chat_key") or "")
            try:
                acct_pid = resolve_account_persona_id(cfg, plat, acct)
            except Exception:
                acct_pid = ""
            conv_pid = pm.get_chat_binding_ref(conv_binding_key(plat, acct, ck))
            out.append({
                "conversation_id": str(r.get("conversation_id") or ""),
                "platform": plat,
                "account_id": acct,
                "chat_key": ck,
                "display_name": str(r.get("display_name") or ""),
                "account_persona": acct_pid,
                "conv_override": conv_pid,
                # legacy 在这条会话没戏的两种情况：账号层有人设（7/24 起优先）
                # 或 已有会话覆写（开关开时优先级最高）
                "suppressed": bool(acct_pid or (conv_pid and enabled)),
            })
        return out

    @app.get("/api/persona/legacy-bindings")
    async def api_persona_legacy_bindings(request: Request, _=Depends(auth_dep)):
        """盘点全部 legacy peer-global 绑定（清理面板读侧）。

        逐条给出：绑定的 profile（stale=引用的 profile 已删除）、落在哪些
        inbox 会话（跨平台/账号）、每个落点是否被账号/会话层压制。
        3 段会话覆写键不在此列（它们是新语义，不是债）；Messenger RPA 托管键
        （rpa_managed=True）列出但不算债（debt_total 不含），处置须去 RPA 页。
        """
        from src.ai.persona_voice import conv_override_enabled
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        cfg = _live_config(request)
        enabled = conv_override_enabled(cfg)
        entries = legacy_binding_entries(pm)
        managed = mrpa_managed_cids(request.app)

        items = []
        for k in sorted(entries):
            info = entries[k]
            pid = info["profile_id"]
            prof = pm.get_persona_by_id(pid) if pid else None
            convs = _legacy_conversations(request, k)
            items.append({
                "key": k,
                "kind": info["kind"],
                "profile_id": pid,
                "profile_name": (
                    str((prof or {}).get("name") or "")
                    or info.get("inline_name") or pid or "?"
                ),
                # stale=引用式绑定指向已删除的 profile（只能清除，升不了级）
                "stale": info["kind"] == "reference" and bool(pid) and prof is None,
                "rpa_managed": k in managed,
                "conversations": convs,
                "suppressed_all": bool(convs) and all(
                    c["suppressed"] for c in convs),
            })
        debt_total = sum(1 for it in items if not it["rpa_managed"])
        return {"ok": True, "enabled": enabled, "total": len(items),
                "debt_total": debt_total, "items": items}

    @app.post("/api/persona/legacy-bindings/cleanup")
    async def api_persona_legacy_cleanup(request: Request, _=Depends(auth_dep)):
        """处理一条 legacy 绑定：``upgrade``（升级为会话级覆写）或 ``remove``（清除）。

        Body: {key, action: "upgrade"|"remove"}
        upgrade 语义保真：把 legacy「这个 peer 用这个人设」显式落到该 peer 的
        **每一条** inbox 会话（3 段覆写键），然后删 legacy 键——绑定从「被压制的
        全局暗债」变成「逐会话的显式覆写」。要求覆写开关开着（关着升级=静默失效，
        直接拒绝）且 profile 仍存在；stale/查无会话 → 只能 remove。
        """
        _check_write_role(request)
        data = await request.json()
        key = str(data.get("key") or "").strip()
        action = str(data.get("action") or "").strip().lower()
        from src.ai.persona_voice import (
            conv_binding_key,
            conv_override_enabled,
            is_conv_binding_key,
        )
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if not key or is_conv_binding_key(key):
            raise HTTPException(400, tr(request, "err.persona.legacy_key_invalid"))
        if action not in ("upgrade", "remove"):
            raise HTTPException(400, tr(request, "err.persona.legacy_action_invalid"))
        has_ref = bool(pm.get_chat_binding_ref(key))
        has_inline = key in (getattr(pm, "_chat_personas", {}) or {})
        if not (has_ref or has_inline):
            raise HTTPException(404, tr(request, "err.persona.legacy_not_found"))
        # Messenger RPA 托管键：SSOT 在 RPA 自己的 SQLite，PM 侧删除会在下次
        # 重启被回灌（且会打断运营在 RPA 页设的显式绑定）——拒绝，指路 RPA 页。
        if key in mrpa_managed_cids(request.app):
            raise HTTPException(
                400, tr(request, "err.persona.legacy_rpa_managed"))
        actor = request.session.get("username", "web_admin")

        upgraded: list = []
        if action == "upgrade":
            cfg = _live_config(request)
            if not conv_override_enabled(cfg):
                raise HTTPException(
                    400, tr(request, "err.persona.conv_override_disabled"))
            pid = pm.get_chat_binding_ref(key)
            if not pid and has_inline:
                pid = str(
                    ((getattr(pm, "_chat_personas", {}) or {}).get(key) or {})
                    .get("id") or ""
                )
            if not pid or pm.get_persona_by_id(pid) is None:
                raise HTTPException(
                    409, tr(request, "err.persona.legacy_stale_upgrade"))
            convs = _legacy_conversations(request, key)
            if not convs:
                raise HTTPException(
                    404, tr(request, "err.persona.legacy_no_conversations"))
            for c in convs:
                ckey = conv_binding_key(
                    c["platform"], c["account_id"], c["chat_key"])
                # 落点已有会话覆写＝比 legacy 更新的显式意图，升级不得踩掉
                #（否则新机制下运营刚设的覆写会被旧值静默覆盖）。
                if pm.get_chat_binding_ref(ckey):
                    continue
                pm.bind_chat_persona_by_profile_id(ckey, pid)
                upgraded.append(ckey)

        pm.unbind_chat_persona(key)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_chat_bindings(cm)
        except Exception:
            pass
        if audit_store:
            if action == "upgrade":
                audit_store.log(actor, "persona_legacy_upgrade",
                              f"key={key} convs={len(upgraded)}")
            else:
                audit_store.log(actor, "persona_legacy_remove", f"key={key}")
        _record_override_action(
            "legacy_upgrade" if action == "upgrade" else "legacy_remove")
        return {"ok": True, "action": action, "key": key,
                "upgraded": upgraded, "upgraded_count": len(upgraded)}

    @app.post("/api/persona/legacy-bindings/cleanup-all")
    async def api_persona_legacy_cleanup_all(request: Request,
                                             _=Depends(auth_dep)):
        """一键安全收官：按「行为保真」自动策略批量处置全部 legacy 绑定。

        与手动逐条不同（手动 upgrade=意图恢复——全落点落覆写让 legacy 人设
        重新生效），批量的硬不变量是 **执行前后每条会话的生效人设 100% 不变**，
        只消债不改行为。逐条决策：
        - 引用的 profile 已删（stale）→ 清除：解析链对它本就 fall-through=死配置；
        - 有落点且全部被压制 → 清除：账号/覆写层已接管，删掉不改变任何会话；
        - 仍有活落点（legacy 当前真在赢）且 profile 可解析 → 升级：**只给活落点**
          落 3 段覆写键（被压制落点不落——落了会反超账号层=行为改变），删 legacy 键；
        - 拿不准 → 跳过留人工：查无落点（no_conversations）/ 内联人设无法转引用
          （inline_unresolvable）/ 覆写开关关着无法升级（disabled）；
        - Messenger RPA 托管键（rpa_managed）→ 跳过：SSOT 在 RPA 的 SQLite，
          PM 侧删除会被重启回灌，处置入口在 Messenger RPA 页。
        按键排序逐条执行；同 peer 的多条变体键（如 ``777`` 与 ``line_rpa:777``）
        先处理者落覆写、后处理者自动判压制清除（planned 集合保证 dry_run 与
        真跑决策一致）。Body: {dry_run: bool}——dry_run=true 只出计划零副作用，
        前端确认框直接渲染该计划。
        """
        _check_write_role(request)
        data = await request.json()
        dry_run = bool(data.get("dry_run"))
        from src.ai.persona_voice import conv_binding_key, conv_override_enabled
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        cfg = _live_config(request)
        enabled = conv_override_enabled(cfg)
        actor = request.session.get("username", "web_admin")

        entries = legacy_binding_entries(pm)
        managed = mrpa_managed_cids(request.app)
        planned_conv: set = set()   # 本批已（计划）落的覆写键：级联判定用
        results: list = []
        summary = {"upgrade": 0, "remove": 0, "skip": 0, "conv_bindings": 0}
        mutated = False

        for key in sorted(entries):
            info = entries[key]
            if key in managed:
                summary["skip"] += 1
                results.append({
                    "key": key, "kind": info["kind"],
                    "profile_id": info["profile_id"],
                    "decision": "skip", "reason": "rpa_managed",
                    "conversations": 0, "live": 0, "upgraded": [],
                })
                continue
            pid = info["profile_id"]
            resolvable = bool(pid) and pm.get_persona_by_id(pid) is not None
            convs = _legacy_conversations(request, key)
            # 压制口径叠加本批 planned：先处理的变体键落了覆写，后处理的同落点
            # 条目在 dry_run（pm 未变）与真跑（pm 已变）下都必须判为被压制。
            live = []
            for c in convs:
                ckey = conv_binding_key(
                    c["platform"], c["account_id"], c["chat_key"])
                if c["suppressed"] or (enabled and ckey in planned_conv):
                    continue
                live.append((c, ckey))

            if info["kind"] == "reference" and not resolvable:
                decision, reason = "remove", "stale"
            elif convs and not live:
                decision, reason = "remove", "suppressed"
            elif live:
                if not enabled:
                    decision, reason = "skip", "disabled"
                elif not resolvable:
                    decision, reason = "skip", "inline_unresolvable"
                else:
                    decision, reason = "upgrade", "live"
            else:
                decision, reason = "skip", "no_conversations"

            upgraded_keys: list = []
            if decision == "upgrade":
                for _c, ckey in live:
                    planned_conv.add(ckey)
                    upgraded_keys.append(ckey)
                    if not dry_run:
                        pm.bind_chat_persona_by_profile_id(ckey, pid)
                if not dry_run:
                    pm.unbind_chat_persona(key)
                    mutated = True
            elif decision == "remove" and not dry_run:
                pm.unbind_chat_persona(key)
                mutated = True

            if not dry_run and decision in ("upgrade", "remove"):
                if audit_store:
                    if decision == "upgrade":
                        audit_store.log(
                            actor, "persona_legacy_upgrade",
                            f"key={key} convs={len(upgraded_keys)} via=auto")
                    else:
                        audit_store.log(actor, "persona_legacy_remove",
                                        f"key={key} reason={reason} via=auto")
                _record_override_action(
                    "legacy_upgrade" if decision == "upgrade"
                    else "legacy_remove")

            summary[decision] += 1
            summary["conv_bindings"] += len(upgraded_keys)
            results.append({
                "key": key, "kind": info["kind"], "profile_id": pid,
                "decision": decision, "reason": reason,
                "conversations": len(convs), "live": len(live),
                "upgraded": upgraded_keys,
            })

        if mutated:
            try:
                cm = (getattr(request.app.state, "config_manager", None)
                      or config_manager)
                pm.persist_chat_bindings(cm)
            except Exception:
                pass
            if audit_store:
                audit_store.log(
                    actor, "persona_legacy_auto",
                    f"upgrade={summary['upgrade']} remove={summary['remove']} "
                    f"skip={summary['skip']} convs={summary['conv_bindings']}")

        return {"ok": True, "dry_run": dry_run, "enabled": enabled,
                "total": len(results), "summary": summary, "results": results}

    @app.post("/api/persona/update-default")
    async def api_persona_update_default(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        persona_data = data.get("persona")
        if not persona_data:
            raise HTTPException(400, "persona data required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(persona_data)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            if cm:
                pm.persist_default_persona(persona_data, cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_update_default",
                          f"name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.get("/api/persona/preview-prompt")
    async def api_persona_preview_prompt(request: Request, chat_id: str = "",
                                          account_persona_id: str = "",
                                          _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        prompt = pm.build_system_prompt(
            chat_id=chat_id, account_persona_id=account_persona_id
        )
        return {"prompt": prompt, "chat_id": chat_id}

    # ── Profile store CRUD ────────────────────────────────────

    @app.get("/api/personas/profiles")
    async def api_profiles_list(request: Request, tag: str = "",
                                 _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if tag:
            matching = pm.get_profiles_by_tag(tag)
            ids = [p.get("id", "") for p in matching if p.get("id")]
            profiles = {p["id"]: p for p in matching if p.get("id")}
        else:
            ids = pm.list_profile_ids()
            profiles = {pid: pm.get_persona_by_id(pid) for pid in ids}
        summary = pm.list_profiles_summary() if not tag else [
            s for s in pm.list_profiles_summary() if s["id"] in set(ids)
        ]
        # 近7日使用量注入（usage_7d）：Studio 列表据此看出「哪些人设真的在产出回复」。
        # 统计属旁路能力，异常一律吞掉——列表本体不能因它挂。
        # usage_7d_default = 未绑定具名 profile、走域默认/兜底人设的回复条数（__default__ 键）；
        # usage_7d_total = 全部 AI 回复条数。二者让前端能呈现「兜底人设也在干活」，
        # 避免活跃排行只反映少数绑定人设（2026-07-20 复盘缺口修复的呈现侧）。
        usage_default = 0
        usage_total = 0
        try:
            from src.utils.persona_usage import counts as _usage_counts
            _uc = _usage_counts(7)
            for _s in summary:
                _s["usage_7d"] = int(_uc.get(_s["id"], 0))
            usage_default = int(_uc.get("__default__", 0))
            usage_total = int(sum(_uc.values()))
        except Exception:
            pass
        return {
            "profiles": profiles,
            "ids": ids,
            "summary": summary,
            "usage_7d_default": usage_default,
            "usage_7d_total": usage_total,
        }

    @app.get("/api/personas/profiles/{profile_id}")
    async def api_profile_get(profile_id: str, request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager, profile_rev
        pm = PersonaManager.get_instance()
        p = pm.get_persona_by_id(profile_id)
        if p is None:
            raise HTTPException(404, f"Profile '{profile_id}' not found")
        # rev＝乐观锁指纹：编辑器加载时记住，保存时带 expected_rev（P2 多开治理）
        return {"profile_id": profile_id, "persona": p, "rev": profile_rev(p)}

    @app.put("/api/personas/profiles/{profile_id}")
    async def api_profile_upsert(profile_id: str, request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        persona_data = data.get("persona")
        if not persona_data or not isinstance(persona_data, dict):
            raise HTTPException(400, "persona dict required")
        from src.utils.persona_manager import PersonaManager, profile_rev
        pm = PersonaManager.get_instance()
        # 乐观锁（P2 多开治理 2026-07-29）：编辑器保存带 expected_rev（加载时的内容
        # 指纹）——当前指纹不一致＝该人设在你编辑期间被其他窗口/同事改过（或删除），
        # 409 拒写防「几分钟的编辑静默覆盖别人的改动」。不带 expected_rev（老客户端/
        # 批量工具/导入）＝旧行为零破坏；前端 409 后可显式确认「仍要覆盖」再免键重发。
        expected_rev = str(data.get("expected_rev") or "")
        if expected_rev:
            _cur = pm.get_persona_by_id(profile_id)
            if _cur is None or profile_rev(_cur) != expected_rev:
                raise HTTPException(
                    409, tr(request, "err.persona.stale_rev"))
        # merge 语义（2026-07-27）：Studio 表单只重建部分字段 → merge=true 时与
        # 既有人设深合并落库，富人设字段（background/life_arc/tastes…）不再被
        # 整体替换抹掉；不带 merge 或人设不存在 → 维持整体替换旧契约。
        did_merge = False
        store_data = persona_data
        if bool(data.get("merge")):
            existing = pm.get_persona_by_id(profile_id)
            if existing:
                store_data = pm.deep_merge_profile(existing, persona_data)
                did_merge = True
        pm.upsert_profile(profile_id, store_data)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_upsert",
                          f"id={profile_id} name={persona_data.get('name','?')}")
        # 回传落库后的最新 rev，编辑器就地更新基线（免一次重取）
        _new_rev = ""
        try:
            _new_rev = profile_rev(pm.get_persona_by_id(profile_id))
        except Exception:
            pass
        return {"ok": True, "profile_id": profile_id, "merged": did_merge,
                "rev": _new_rev}

    @app.delete("/api/personas/profiles/{profile_id}")
    async def api_profile_delete(profile_id: str, request: Request, _=Depends(auth_dep)):
        """删除人设档案。

        **绑定护栏**（与 bio-doc 删除同源，2026-07-28 误删实锤）：档案被账号
        显式绑定时删除 = 绑定悬空 → 该账号**静默**回落默认人设（比丢传记更重：
        名字/口吻/记忆全换）。有绑定 → 409 列明账号；``?force=1`` 显式越过。
        """
        _check_write_role(request)
        from src.integrations.account_registry import persona_binding_refs
        force = str(request.query_params.get("force") or "").lower() in (
            "1", "true", "yes")
        refs = [] if force else persona_binding_refs(profile_id)
        if refs:
            raise HTTPException(
                409, tr(request, "err.persona.profile_bound",
                        accounts=", ".join(refs[:5]), n=len(refs)))
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        existed = pm.delete_profile(profile_id)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_delete", f"id={profile_id}")
        return {"ok": existed, "profile_id": profile_id}

    @app.get("/api/personas/profiles/{profile_id}/history")
    async def api_profile_history(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Return the version history for a profile (up to last 3 saves)."""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        history = pm.get_profile_history(profile_id)
        return {"profile_id": profile_id, "history": history, "count": len(history)}

    @app.post("/api/personas/profiles/{profile_id}/revert")
    async def api_profile_revert(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Revert a profile to its previous saved version."""
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        ok = pm.revert_profile(profile_id)
        if not ok:
            raise HTTPException(404, "No history available for this profile")
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_revert", f"profile_id={profile_id}")
        return {"ok": True, "profile_id": profile_id, "persona": pm.get_persona_by_id(profile_id)}

    @app.post("/api/personas/bulk-bind")
    async def api_bulk_bind(request: Request, _=Depends(auth_dep)):
        """Rebind all currently-bound chats to a target profile.

        Body: {profile_id: str, scope: str (default 'all_bindings'), dry_run: bool}
        """
        _check_write_role(request)
        data = await request.json()
        profile_id = data.get("profile_id", "")
        scope = data.get("scope", "all_bindings")
        dry_run = bool(data.get("dry_run", False))
        if not profile_id:
            raise HTTPException(400, "profile_id required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        try:
            result = pm.bulk_bind_by_profile(profile_id, scope=scope, dry_run=dry_run)
        except KeyError as e:
            raise HTTPException(404, str(e))
        if not dry_run:
            try:
                cm = getattr(request.app.state, "config_manager", None) or config_manager
                pm.persist_chat_bindings(cm)
            except Exception:
                pass
            actor = request.session.get("username", "web_admin")
            if audit_store:
                audit_store.log(actor, "persona_bulk_bind",
                              f"profile_id={profile_id} scope={scope} affected={result['affected']}")
        return {"ok": True, **result}

    @app.post("/api/personas/profiles/reload")
    async def api_profiles_reload(request: Request, _=Depends(auth_dep)):
        """Reload profiles from config.yaml at runtime (no restart needed)."""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        cfg = getattr(config_manager, "config", None) or {}
        count = pm.load_profiles_from_config(cfg)
        return {"ok": True, "loaded": count}

    @app.get("/api/personas/profiles/export")
    async def api_profiles_export(request: Request, _=Depends(auth_dep)):
        """Export all profiles as a JSON list (master only)."""
        _check_master_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profiles = [
            dict(p, id=pid)
            for pid, p in pm._profile_personas.items()
        ]
        return {"profiles": profiles, "count": len(profiles)}

    @app.post("/api/personas/profiles/import")
    async def api_profiles_import(request: Request, _=Depends(auth_dep)):
        """Import profiles from a JSON list (master only).

        Body: {profiles: [...], mode: 'merge'|'replace'}
        merge (default): add/overwrite individual profiles, keep others.
        replace: clear all profiles then load the new list.
        """
        _check_master_role(request)
        data = await request.json()
        profiles_in = data.get("profiles")
        mode = data.get("mode", "merge")
        if not isinstance(profiles_in, list):
            raise HTTPException(400, "profiles must be a JSON array")
        if mode not in ("merge", "replace"):
            raise HTTPException(400, "mode must be 'merge' or 'replace'")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if mode == "replace":
            for pid in list(pm._profile_personas.keys()):
                pm.delete_profile(pid)
        imported = 0
        errors = []
        for entry in profiles_in:
            if not isinstance(entry, dict):
                errors.append("skipped non-dict entry")
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid:
                errors.append("skipped entry without id")
                continue
            pm.upsert_profile(pid, entry)
            imported += 1
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profiles_import",
                          f"mode={mode} imported={imported} errors={len(errors)}")
        return {"ok": True, "imported": imported, "mode": mode, "errors": errors}

    # ── P5-D: Canonical config sync ──────────────────────────

    @app.post("/api/personas/sync-to-config")
    async def api_personas_sync_to_config(request: Request, _=Depends(auth_dep)):
        """Push all operator-owned PM profiles to personas.yaml (canonical config).

        Only profiles WITHOUT _mrpa_source are written — keeps the file clean.
        This file is git-trackable and loaded on next startup (layer 2 in load order).
        """
        _check_master_role(request)
        from src.utils.persona_manager import PersonaManager
        import time as _t
        pm = PersonaManager.get_instance()
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        if not cm or not hasattr(cm, "save_personas"):
            raise HTTPException(503, tr(request, "err.persona.save_unavailable"))
        operator_profiles = {
            pid: dict(p)
            for pid, p in pm._profile_personas.items()
            if not p.get("_mrpa_source")
        }
        data = {
            "profiles": operator_profiles,
            "updated_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
        }
        ok, msg = cm.save_personas(data)
        if not ok:
            raise HTTPException(500, msg)
        # P7-A: mark synced profiles as 'canonical' source immediately
        pm.mark_profiles_canonical(list(operator_profiles.keys()))
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "personas_sync_to_config",
                          f"profiles={len(operator_profiles)}")
        return {"ok": True, "message": msg, "profiles_written": len(operator_profiles)}

    # ── P8-B: Canonical diff endpoint ────────────────────────────

    @app.get("/api/personas/profiles/{profile_id}/diff-canonical")
    async def api_profile_diff_canonical(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Return a field-level diff between the PM's live version and the personas.yaml version.

        Response:
          {profile_id, has_canonical, has_pm_version, is_identical, canonical, current,
           diff: {added, removed, changed:[{field,from,to}], unchanged}}

        added   = keys in current but not in canonical (new fields added in Studio)
        removed = keys in canonical but not in current (fields removed since last sync)
        changed = keys in both but with different values
        unchanged = keys with identical values
        Internal/meta keys (_mrpa_source, id) are excluded from diff for readability.
        """
        from src.utils.persona_manager import PersonaManager
        import json as _json
        pm = PersonaManager.get_instance()
        cm = getattr(request.app.state, "config_manager", None) or config_manager

        current = pm.get_persona_by_id(profile_id)
        canonical: dict = {}
        try:
            if cm and hasattr(cm, "get_personas_config"):
                _pdata = cm.get_personas_config()
                canonical = ((_pdata or {}).get("profiles") or {}).get(profile_id) or {}
        except Exception:
            pass

        _SKIP = {"_mrpa_source"}
        _c_keys = {k for k in (canonical or {}) if k not in _SKIP}
        _p_keys = {k for k in (current or {}) if k not in _SKIP}

        added: dict = {}
        removed: dict = {}
        changed: list = []
        unchanged: list = []

        if current and canonical:
            for k in _p_keys - _c_keys:
                added[k] = (current or {}).get(k)
            for k in _c_keys - _p_keys:
                removed[k] = canonical.get(k)
            for k in _c_keys & _p_keys:
                cv = canonical.get(k)
                pv = (current or {}).get(k)
                # Compare via JSON serialisation to handle nested dicts/lists
                if _json.dumps(cv, sort_keys=True, ensure_ascii=False) == \
                   _json.dumps(pv, sort_keys=True, ensure_ascii=False):
                    unchanged.append(k)
                else:
                    changed.append({"field": k, "from": cv, "to": pv})

        is_identical = (not added and not removed and not changed)

        return {
            "profile_id": profile_id,
            "has_canonical": bool(canonical),
            "has_pm_version": bool(current),
            "is_identical": is_identical,
            "canonical": canonical if canonical else None,
            "current": dict(current) if current else None,
            "diff": {
                "added": added,
                "removed": removed,
                "changed": changed,
                "unchanged": unchanged,
            },
        }

    # ── P9-A: Cross-platform binding list for a specific profile ─

    @app.get("/api/personas/profiles/{profile_id}/bindings")
    async def api_profile_bindings(profile_id: str, request: Request, _=Depends(auth_dep)):
        """List all chats bound to a specific profile with platform detection.

        Response:
          {profile_id, total, by_platform: {tg_private, tg_group, line, mrpa, wa, other},
           bindings: [{chat_id, platform, binding_type}]}

        binding_type: 'reference' (P4 binding_ref) | 'inline' (legacy snapshot)
        """
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()

        def _detect_platform(cid: str) -> str:
            c = str(cid).lower()
            # 会话级覆写键（3 段 platform:account:chat_key）按首段归平台
            if c.count(":") >= 2:
                _head = c.split(":", 1)[0]
                if _head in ("telegram", "whatsapp", "line", "messenger", "web"):
                    return _head
            if c.startswith("line_rpa:") or c.startswith("line:"):
                return "line"
            if c.startswith("mrpa:") or c.startswith("messenger:"):
                return "mrpa"
            if c.startswith("wa:") or c.startswith("whatsapp:"):
                return "wa"
            # Telegram: group/channel = large negative int, private = positive int
            try:
                n = int(cid)
                return "tg_group" if n < 0 else "tg_private"
            except ValueError:
                return "other"

        bindings: list = []

        # Reference bindings (P4) — explicit chat_id → profile_id map
        for cid, pid in pm._chat_bindings.items():
            if str(pid) == str(profile_id):
                bindings.append({
                    "chat_id": cid,
                    "platform": _detect_platform(cid),
                    "binding_type": "reference",
                })

        # Inline / legacy snapshot bindings
        for cid, persona in pm._chat_personas.items():
            if str(persona.get("id", "")) == str(profile_id) and \
               cid not in pm._chat_bindings:
                bindings.append({
                    "chat_id": cid,
                    "platform": _detect_platform(cid),
                    "binding_type": "inline",
                })

        by_platform: dict = {}
        for b in bindings:
            p = b["platform"]
            by_platform[p] = by_platform.get(p, 0) + 1

        bindings.sort(key=lambda x: (x["platform"], x["chat_id"]))
        return {
            "profile_id": profile_id,
            "total": len(bindings),
            "by_platform": by_platform,
            "bindings": bindings,
        }

    # ── P9-B: System prompt preview for a profile ─────────────

    @app.get("/api/personas/profiles/{profile_id}/prompt-preview")
    async def api_profile_prompt_preview(
        profile_id: str,
        request: Request,
        detail: str = "full",
        _=Depends(auth_dep),
    ):
        """Return the assembled persona instruction block for a profile.

        ?detail=full   → full _format_persona_instructions output
        ?detail=compact → compact (token-optimised) variant
        ?detail=both    → both variants + character counts

        This is a debug/preview endpoint — no chat context, no domain prompt,
        no KB context. Shows exactly what the persona contributes to the LLM prompt.
        """
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profile = pm.get_persona_by_id(profile_id)
        if profile is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")

        full_text = pm._format_persona_instructions(profile)
        compact_text = pm._format_persona_compact(profile)

        if detail == "compact":
            return {
                "profile_id": profile_id,
                "profile_name": profile.get("name", profile_id),
                "detail": "compact",
                "prompt": compact_text,
                "char_count": len(compact_text),
            }
        if detail == "both":
            return {
                "profile_id": profile_id,
                "profile_name": profile.get("name", profile_id),
                "detail": "both",
                "full": full_text,
                "compact": compact_text,
                "full_chars": len(full_text),
                "compact_chars": len(compact_text),
            }
        # default: full
        return {
            "profile_id": profile_id,
            "profile_name": profile.get("name", profile_id),
            "detail": "full",
            "prompt": full_text,
            "char_count": len(full_text),
        }

    # ── P10-B: WhatsApp account persona assignment ────────────────

    @app.post("/api/personas/wa-account/{account_id}/assign-profile")
    async def api_wa_assign_profile(account_id: str, request: Request, _=Depends(auth_dep)):
        """Assign a persona profile to a WhatsApp RPA account.

        Body: {"profile_id": "..."}

        Mutates whatsapp_rpa.accounts[].persona_ids in config.yaml, then hot-reloads
        the matching WhatsAppRpaService runner so the change takes effect immediately.
        """
        _check_master_role(request)
        body = await request.json()
        profile_id = str(body.get("profile_id") or "").strip()

        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            raise HTTPException(404, f"profile '{profile_id}' not found in PersonaManager")

        cm = getattr(request.app.state, "config_manager", None) or config_manager
        wa_cfg: dict = (getattr(cm, "config", None) or {}).get("whatsapp_rpa") or {}
        if not wa_cfg:
            raise HTTPException(404, "whatsapp_rpa config not found")

        pids = [profile_id] if profile_id else []  # 空 = 清除（回默认人设）
        # Mutate in-memory config: find account or fallback to top-level
        accounts: list = wa_cfg.get("accounts") or []
        matched = False
        matched_flat = False
        for acc in accounts:
            aid = str(acc.get("id") or acc.get("account_id") or acc.get("adb_serial") or "")
            if aid == account_id:
                acc["persona_ids"] = pids
                matched = True
                break

        if not matched:
            if account_id in ("default", ""):
                # Single-account mode: set top-level persona_ids
                wa_cfg["persona_ids"] = pids
                matched = matched_flat = True

        if not matched:
            raise HTTPException(404, f"WA account '{account_id}' not found in config")

        # Persist：扁平键走 overlay 最小 patch；accounts 列表内改动保持整文件
        # save()（overlay list 整体替换会遮蔽主配置账号增删，见 _save_binding_patch）
        cm.config["whatsapp_rpa"] = wa_cfg
        if matched_flat:
            saved = _save_binding_patch(cm, {"whatsapp_rpa": {"persona_ids": pids}})
        else:
            saved = cm.save()

        # Hot-reload matching service runner (best-effort)
        reloaded = False
        try:
            _wa_svcs = getattr(request.app.state, "whatsapp_rpa_services", None) or []
            for svc in _wa_svcs:
                svc_aid = str(getattr(svc, "account_id", "default") or "")
                if svc_aid == account_id or (account_id == "default" and not svc_aid):
                    # Merge updated account config into merged config
                    _merged = svc.effective_config()
                    _merged["persona_ids"] = pids
                    svc.reconfigure(_merged)
                    reloaded = True
        except Exception as _e:
            pass  # non-fatal; config is already persisted

        return {
            "ok": True,
            "account_id": account_id,
            "profile_id": profile_id,
            "config_saved": saved,
            "runner_hot_reloaded": reloaded,
        }

    # ── 统一账号人设绑定：Telegram ────────────────────────────────
    @app.post("/api/personas/tg-account/{account_id}/assign-profile")
    async def api_tg_assign_profile(account_id: str, request: Request, _=Depends(auth_dep)):
        """给 Telegram 账号指定/更换/清除人设 profile。

        Body: {"profile_id": "..."}  —— profile_id 为空表示清除（回默认人设）。
        写 telegram.accounts[].persona_ids；单账号(default) 写扁平 telegram.persona_ids。
        """
        _check_write_role(request)
        body = await request.json()
        profile_id = str(body.get("profile_id") or "").strip()
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")

        cm = getattr(request.app.state, "config_manager", None) or config_manager
        if not cm:
            raise HTTPException(503, tr(request, "err.svc.config_manager_not_ready"))
        tg_cfg: dict = (getattr(cm, "config", None) or {}).get("telegram") or {}
        pids = [profile_id] if profile_id else []

        accounts = tg_cfg.get("accounts")
        matched = False
        matched_flat = False
        if isinstance(accounts, list) and accounts:
            for acc in accounts:
                aid = str(acc.get("id") or acc.get("account_id") or "").strip()
                if aid == account_id:
                    acc["persona_ids"] = pids
                    matched = True
                    break
        if not matched and (account_id in ("default", "") or not accounts):
            # 单账号 / default：写扁平槽（注册表 default 分支已支持读取）
            tg_cfg["persona_ids"] = pids
            matched = matched_flat = True
        if not matched:
            raise HTTPException(404, f"TG account '{account_id}' not found")

        cm.config["telegram"] = tg_cfg
        if matched_flat:
            saved = _save_binding_patch(cm, {"telegram": {"persona_ids": pids}})
        else:
            saved = cm.save()  # accounts 列表内改动：overlay 整列表会遮蔽主配置
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "tg_assign_profile",
                          f"account={account_id} profile={profile_id or '(cleared)'}")
        return {"ok": True, "account_id": account_id, "profile_id": profile_id,
                "config_saved": saved}

    # ── 统一账号人设绑定：Messenger RPA ───────────────────────────
    @app.post("/api/personas/mrpa-account/{account_id}/assign-profile")
    async def api_mrpa_assign_profile(account_id: str, request: Request, _=Depends(auth_dep)):
        """给 Messenger RPA 账号指定/更换/清除人设 profile。

        Body: {"profile_id": "..."}  —— 空表示清除。
        写 messenger_rpa.accounts[].persona_ids，best-effort 热重载 runner。
        """
        _check_write_role(request)
        body = await request.json()
        profile_id = str(body.get("profile_id") or "").strip()
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")

        cm = getattr(request.app.state, "config_manager", None) or config_manager
        if not cm:
            raise HTTPException(503, tr(request, "err.svc.config_manager_not_ready"))
        mrpa_cfg: dict = (getattr(cm, "config", None) or {}).get("messenger_rpa") or {}
        pids = [profile_id] if profile_id else []

        accounts = mrpa_cfg.get("accounts") or []
        matched = False
        matched_flat = False
        for acc in accounts:
            aid = str(acc.get("id") or acc.get("account_id") or acc.get("adb_serial") or "").strip()
            if aid == account_id:
                acc["persona_ids"] = pids
                matched = True
                break
        if not matched and account_id in ("default", ""):
            mrpa_cfg["persona_ids"] = pids
            matched = matched_flat = True
        if not matched:
            raise HTTPException(404, f"Messenger account '{account_id}' not found")

        cm.config["messenger_rpa"] = mrpa_cfg
        if matched_flat:
            saved = _save_binding_patch(cm, {"messenger_rpa": {"persona_ids": pids}})
        else:
            saved = cm.save()  # accounts 列表内改动：overlay 整列表会遮蔽主配置
        reloaded = False
        try:
            _svcs = getattr(request.app.state, "messenger_rpa_services", None) or []
            for svc in _svcs:
                svc_aid = str(getattr(svc, "account_id", "default") or "")
                if svc_aid == account_id or (account_id == "default" and not svc_aid):
                    if hasattr(svc, "effective_config") and hasattr(svc, "reconfigure"):
                        _merged = svc.effective_config()
                        _merged["persona_ids"] = pids
                        svc.reconfigure(_merged)
                        reloaded = True
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "mrpa_assign_profile",
                          f"account={account_id} profile={profile_id or '(cleared)'}")
        return {"ok": True, "account_id": account_id, "profile_id": profile_id,
                "config_saved": saved, "runner_hot_reloaded": reloaded}

    # ── P7-C: Promote mrpa-imported profile to operator-owned ────

    @app.post("/api/personas/profiles/{profile_id}/promote")
    async def api_profile_promote(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Remove _mrpa_source flag from a profile, making it operator-owned.

        After promotion the profile:
        - survives restarts (persisted to profiles_runtime.yaml)
        - shows source='studio' badge instead of 'mrpa'
        - is included in sync-to-config / persist_profiles
        - is no longer overwritten by Messenger RPA import on restart
        """
        _check_master_role(request)
        from src.utils.persona_manager import PersonaManager
        import copy as _copy
        pm = PersonaManager.get_instance()
        p = pm.get_persona_by_id(profile_id)
        if p is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")
        if not p.get("_mrpa_source"):
            return {"ok": True, "message": "该 profile 已是运营人设，无需升格", "already_operator": True}
        promoted = _copy.deepcopy(p)
        promoted.pop("_mrpa_source", None)  # strip the flag
        pm.upsert_profile(profile_id, promoted, _track_history=True)
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        try:
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_promote", f"profile_id={profile_id}")
        return {"ok": True, "message": f"'{profile_id}' 已升格为运营人设（source=studio）", "profile_id": profile_id}

    # ── Runtime status summary ────────────────────────────────

    @app.get("/api/personas/status")
    async def api_personas_status(request: Request, _=Depends(auth_dep)):
        """Runtime summary: profiles, bindings, TG + Messenger account persona routing."""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profile_ids = pm.list_profile_ids()
        bindings = pm.get_all_chat_bindings()

        # Telegram accounts
        tg_accounts: list = []
        try:
            from src.client.telegram_account_registry import TelegramAccountRegistry
            _tg_cfg = (getattr(config_manager, "config", None) or {}).get("telegram", {})
            _reg = TelegramAccountRegistry.from_config(_tg_cfg)
            for acc in _reg.all_contexts():
                primary_pid = acc.persona_ids[0] if acc.persona_ids else ""
                tg_accounts.append({
                    "account_id": acc.account_id,
                    "label": acc.label or acc.account_id,
                    "persona_ids": acc.persona_ids,
                    "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                })
        except Exception:
            pass

        # Messenger RPA accounts
        mrpa_accounts: list = []
        mrpa_reply_profiles: list = []
        mrpa_imported_count: int = 0
        try:
            from src.integrations.messenger_rpa.account_pool import AccountRegistry
            _mrpa_cfg = (getattr(config_manager, "config", None) or {}).get("messenger_rpa", {})
            _mreg = AccountRegistry.from_config(_mrpa_cfg, config_path="")
            for ctx in _mreg.all_contexts():
                primary_pid = ctx.persona_ids[0] if ctx.persona_ids else ""
                mrpa_accounts.append({
                    "account_id": ctx.account_id,
                    "label": ctx.label or ctx.account_id,
                    "persona_ids": ctx.persona_ids,
                    "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                })
            # Count reply_profiles imported into PM (marked with _mrpa_source)
            _rp_cfg = _mrpa_cfg.get("reply_profiles") or {}
            _rp_list = _rp_cfg.get("profiles") or [] if isinstance(_rp_cfg, dict) else []
            for _rp in _rp_list:
                if not isinstance(_rp, dict):
                    continue
                _rpid = str(_rp.get("id") or _rp.get("name") or "").strip()
                if not _rpid:
                    continue
                _pm_profile = pm.get_persona_by_id(_rpid)
                _imported = _pm_profile is not None and bool(
                    _pm_profile.get("_mrpa_source")
                )
                mrpa_reply_profiles.append({
                    "id": _rpid,
                    "name": (_rp.get("persona") or {}).get("name") or _rpid,
                    "imported": _imported,
                    "editable": _imported,
                })
                if _imported:
                    mrpa_imported_count += 1
        except Exception:
            pass

        # WhatsApp RPA accounts
        wa_accounts: list = []
        try:
            _wa_cfg = (getattr(config_manager, "config", None) or {}).get("whatsapp_rpa") or {}
            if isinstance(_wa_cfg, dict) and _wa_cfg.get("enabled"):
                _wa_accs = _wa_cfg.get("accounts") or []
                if _wa_accs:
                    for _acc in _wa_accs:
                        _aid = _acc.get("account_id") or _acc.get("adb_serial", "default")
                        _pids = list(_acc.get("persona_ids") or _wa_cfg.get("persona_ids") or [])
                        primary_pid = _pids[0] if _pids else ""
                        wa_accounts.append({
                            "account_id": _aid,
                            "label": _acc.get("label") or _aid,
                            "persona_ids": _pids,
                            "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                        })
                elif _wa_cfg.get("persona_ids"):
                    _pids = list(_wa_cfg.get("persona_ids") or [])
                    primary_pid = _pids[0] if _pids else ""
                    wa_accounts.append({
                        "account_id": "default",
                        "label": "WhatsApp (单账号)",
                        "persona_ids": _pids,
                        "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                    })
        except Exception:
            pass

        # Profiles in active use (across all accounts + chat bindings)
        used_ids: set = set()
        for acc in tg_accounts + mrpa_accounts + wa_accounts:
            used_ids.update(acc["persona_ids"])
        for p in bindings.values():
            pid = p.get("id", "") if isinstance(p, dict) else ""
            if pid:
                used_ids.add(pid)

        import datetime as _dt
        lca = pm._last_changed_at
        last_changed_iso = (
            _dt.datetime.fromtimestamp(lca, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if lca else ""
        )
        try:
            _role = request.session.get("role", "")
        except Exception:
            _role = ""

        # P7-B: source breakdown + canonical sync metadata
        summary = pm.list_profiles_summary()
        source_breakdown: dict = {}
        for s in summary:
            src = s.get("source", "studio")
            source_breakdown[src] = source_breakdown.get(src, 0) + 1
        unsynced_studio_count = source_breakdown.get("studio", 0)

        canonical_last_sync = ""
        canonical_last_sync_ts: float = 0.0
        try:
            if config_manager and hasattr(config_manager, "get_personas_config"):
                _pdata = config_manager.get_personas_config()
                canonical_last_sync = (_pdata or {}).get("updated_at", "")
        except Exception:
            pass
        # Also check in-session sync timestamp for more accurate "just synced" UX
        if pm._last_canonical_sync_at:
            canonical_last_sync_ts = pm._last_canonical_sync_at

        return {
            "profile_ids": profile_ids,
            "total_profiles": len(profile_ids),
            "total_bindings": len(bindings),
            "tg_accounts": tg_accounts,
            "mrpa_accounts": mrpa_accounts,
            "mrpa_reply_profiles": mrpa_reply_profiles,
            "mrpa_imported_count": mrpa_imported_count,
            "wa_accounts": wa_accounts,
            "profiles_in_use": list(used_ids),
            "domain_persona_name": pm._domain_persona.get("name", "") if pm._domain_persona else "",
            "last_changed_at": last_changed_iso,
            "last_changed_ts": lca,
            "viewer_mode": _role == _ROLE_VIEWER,
            # P7-B: source tracking
            "source_breakdown": source_breakdown,
            "unsynced_studio_count": unsynced_studio_count,
            "canonical_last_sync": canonical_last_sync,
            "canonical_last_sync_ts": canonical_last_sync_ts,
        }

    # ── S6-RULES: global_rules API ─────────────────────────────────────────

    @app.get("/api/persona/global-rules")
    async def api_global_rules_get(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager, profile_rev
        pm = PersonaManager.get_instance()
        rules = pm.get_global_rules()
        # rev＝乐观锁指纹（与人设档案同款；P2 多开治理）：编辑器加载时记住，保存回传
        # source＝落盘位置全景（P7-1 overlay 化）：读的是实例自己那份还是出厂默认、
        # 保存会写到哪——把「仓库出厂默认 vs 实例覆盖」的分叉对运维显式可见。
        out = {"ok": True, "rules": rules, "rev": profile_rev(rules)}
        try:
            out["source"] = pm.global_rules_source()
        except Exception:
            pass
        return out

    @app.put("/api/persona/global-rules")
    async def api_global_rules_save(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        rules = data.get("rules")
        if not rules or not isinstance(rules, dict):
            raise HTTPException(400, "rules dict required")
        from src.utils.persona_manager import PersonaManager, profile_rev
        pm = PersonaManager.get_instance()
        # 乐观锁（2026-07-29）：全局规则影响**所有人设**，被静默覆盖的破坏面比单个
        # 档案更大 → 带 expected_rev 时校验当前指纹，不一致即 409 拒写（前端确认后
        # 免键重发＝显式覆盖）。不带 expected_rev＝旧契约零破坏。
        expected_rev = str(data.get("expected_rev") or "")
        if expected_rev and profile_rev(pm.get_global_rules()) != expected_rev:
            raise HTTPException(409, tr(request, "err.persona.stale_rev"))
        ok = pm.save_global_rules(rules)
        if not ok:
            raise HTTPException(500, "Failed to save global_rules.yaml")
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "global_rules_save",
                          f"constraints={len(rules.get('reply_constraints', []))}")
        return {"ok": True, "rev": profile_rev(pm.get_global_rules())}

    @app.get("/api/persona/global-rules/backups")
    async def api_global_rules_backups(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {"ok": True, "backups": pm.list_backups()}

    @app.post("/api/persona/global-rules/restore/{slot}")
    async def api_global_rules_restore(slot: int, request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        ok = pm.restore_backup(slot)
        if not ok:
            raise HTTPException(404, f"Backup slot {slot} not found or restore failed")
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "global_rules_restore", f"slot={slot}")
        return {"ok": True, "rules": pm.get_global_rules()}

    @app.post("/api/persona/global-rules/preview")
    async def api_global_rules_preview(request: Request, _=Depends(auth_dep)):
        """Preview assembled constraint text from provided (unsaved) rules data."""
        from src.utils.persona_manager import PersonaManager
        body = await request.json()
        rules = body.get("rules")
        if not rules or not isinstance(rules, dict):
            raise HTTPException(400, "rules dict required")
        pm = PersonaManager.get_instance()
        platform = body.get("platform", "")
        text = pm.preview_constraints_text(rules, platform=platform)
        return {"ok": True, "text": text}
