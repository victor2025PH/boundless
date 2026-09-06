"""统一收件箱——主读路径路由域（巨石拆分 slice 37b）。

把 ``register_unified_inbox_routes`` 巨型闭包中物理分离的主读端点外移为
``register_read_routes(app, *, api_auth, config_manager=None)``，由主 register
在 chats 原位置调用：

- ``unified-inbox/chats``：会话列表（聚合 + contacts/SLA/tags/assignment 富集）
- ``unified-inbox/thread``：会话线程（store 读路径 + 入站自动翻译 + 出向原文富集）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 37b 端点契约断言）。

依赖全部朝下：aggregate 读路径族 + ``_enrich_outbound_originals``（slice 37b 下沉）、
services、sla、channel_adapters.status_via_adapters、normalizer、inbound_translate。
收 api_auth + config_manager（assignment / 入站翻译配置）。
"""

from __future__ import annotations

import logging
import asyncio
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from src.inbox.channel_adapters import status_via_adapters
from src.inbox.normalizer import (
    candidate_messages_from_source,
    conv_id,
    message_obj,
    name_is_real,
    store_row_to_chat,
)
from src.web.routes.unified_inbox_aggregate import (
    _INBOX_ADAPTERS,
    _account_status_map,
    _chats_for_listing,
    _collect_all_chats,
    _collect_chats_from_store,
    _enrich_outbound_originals,
    _ingest_thread_best_effort,
    _is_protocol_account,
    _overlay_store_identity,
    _read_from_store_enabled,
    _registry_active_map,
    _store_conv_as_chat,
    _thread_messages_from_store,
)
from src.web.routes.unified_inbox_services import (
    _contacts_store,
    _get_telegram_client,
    _get_translation_service,
    _inbox_store,
)
from src.web.routes.unified_inbox_sla import _sla_cfg
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# ── F3：资料面板就绪度观测（打开会话时按平台记文字身份完整度）──────────────
# 去重集：同一 conversation_id 每进程只记一次（避免自适应轮询/加载更早重复计数把 opens 撑爆），
# bounded 防内存无界（超上限后新会话不再记，仅内存保护——是采样 gauge 而非精确总量）。
_PANEL_SEEN: set = set()
_PANEL_SEEN_MAX = 5000


_name_is_real = name_is_real   # 兼容别名（口径已上移到 normalizer.name_is_real，F3/F4 共用）


def _record_panel_identity(chat: Optional[Dict[str, Any]]) -> None:
    """打开会话时记一次资料面板文字身份完整度（去重 + best-effort，绝不影响主流程）。"""
    if not chat:
        return
    try:
        cid = str(chat.get("conversation_id") or "")
        if not cid or cid in _PANEL_SEEN:
            return
        if len(_PANEL_SEEN) >= _PANEL_SEEN_MAX:
            return  # 超上限：新会话不再记（内存保护），已记的仍去重
        _PANEL_SEEN.add(cid)
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record_panel(
            str(chat.get("platform") or ""),
            has_name=_name_is_real(chat.get("name"), chat.get("chat_key")),
            has_username=bool(str(chat.get("username") or "").strip()),
            has_phone=bool(str(chat.get("phone") or "").strip()),
        )
    except Exception:
        logger.debug("[panel] 资料面板就绪度记录失败（已忽略）", exc_info=True)


def _merge_orchestrator_status(
    platform_status: Dict[str, Any], config_manager,
) -> None:
    """N4：把账号池编排器在管的 protocol/official 账号并入 platform_status。

    inbox 适配器只反映"单连接/RPA"运行态，**不含**扫码登入后由编排器拉起的协议多开号。
    若不并入，连接中心抽屉只会看到 A 线 config 账号（default），看不到扫码新增的号。
    """
    try:
        from src.integrations.account_orchestrator import get_orchestrator
        from src.integrations.account_registry import get_account_registry

        # P2：注册表 label 是账号「人格名」的权威来源（编排器 to_dict 不带 label）。
        # 取一份 label 映射，既给新并入条目命名，也回填既有条目的空 label，
        # 让前端账号切换条 / 会话角标显示用户起的人格名而非裸 account_id。
        label_map: Dict[str, str] = {}
        # P1 身份化：同一趟 registry.list() 里顺手收集自身资料（self_*），末尾注入 platform_status
        profile_map: Dict[str, Dict[str, str]] = {}
        # 可删性标记：只有注册表在册账号才能被 /api/accounts/*/remove 软删；
        # 适配器/config 来源的条目（telegram default、web 等）删除是空操作 →
        # 给前端 removable=False 隐藏删除/登出按钮。注册表读取失败时保持 None 不标注
        # （前端按可删处理，行为回落旧版）。
        registry_keys: Optional[set] = None
        registry_obj = None   # 身份可视化：留给下方 persona 解析复用（不逐号重建）
        # 产品口径（2026-08-01）：已登出(offline)账号**不进**聊天页 platform_status
        # （无 chip / 无会话）；历史留本机 store，同号重登后自然回显。账号管理走
        # /api/accounts（仍列 offline 供重登）。此处只收集 label/self_profile。
        offline_keys: set = set()
        try:
            from src.integrations.account_self_profile import (
                read_self_profile_from_meta,
            )
            registry_obj = get_account_registry()
            registry_keys = set()
            for row in registry_obj.list():
                key = f"{row.get('platform')}:{row.get('account_id')}"
                if str(row.get("status") or "") == "offline":
                    offline_keys.add(key)
                    continue  # 聊天页视角：当不存在
                registry_keys.add(key)
                lbl = str(row.get("label") or "")
                if lbl:
                    label_map[key] = lbl
                prof = read_self_profile_from_meta(row.get("meta") or {})
                if prof:
                    profile_map[key] = prof
        except Exception:
            registry_keys = None
            logger.debug("[chats] 读取注册表 label/self_profile 失败", exc_info=True)

        cfg = (config_manager.config if config_manager is not None else {}) or {}
        for oa in (get_orchestrator(cfg).status().get("accounts") or []):
            plat = oa.get("platform")
            aid = oa.get("account_id")
            if not plat or not aid:
                continue
            # 跳过 stopped 幽灵条目（已从注册表移除但仍滞留 _managed 的占位）；
            # 真正断线的 worker 状态为 error/starting，仍会进抽屉供重连。
            if oa.get("state") == "stopped":
                continue
            running = oa.get("state") == "running"
            key = f"{plat}:{aid}"
            label = label_map.get(key) or oa.get("label") or ""
            # P1 资料修改配套：透传编排器 worker 状态（仅编排器在管条目；适配器
            # 条目不动）——前端账号详情据此显示 starting/error 与失败原因摘要。
            state_detail = str((oa.get("worker") or {}).get("detail")
                               or oa.get("last_error") or "")[:120]
            existing = platform_status.get(key)
            if existing is None:
                platform_status[key] = {
                    "platform": plat,
                    "account_id": aid,
                    "running": running,
                    "label": label,
                    "mode": oa.get("mode") or "",
                    "state": oa.get("state"),
                    "state_detail": state_detail,
                }
            else:
                existing["running"] = bool(existing.get("running")) or running
                existing["state"] = oa.get("state")
                existing["state_detail"] = state_detail
                # 注册表 label 是用户显式起的人格名 → 覆盖适配器的通用 label
                if label_map.get(key):
                    existing["label"] = label_map[key]

        # 编排器/适配器残留的已登出号：聊天页强制摘掉（账号管理另路可见）
        for _ok in offline_keys:
            platform_status.pop(_ok, None)

        # 掉线时长 + 入站半死透传：外部 worker push 的会话健康表——
        # - unhealthy_since → 前端 _acctOfflineHint「已掉线多久」
        # - inbox_stalled / inbox_hint / e2ee_ratio → 账号卡半死态（P4：坐席不必
        #   另轮询 /metrics；与 unhealthy 同一次 dump）
        # 查找键必须与登记表 _key 同构。ensure_seeded：重启后灌 offline。
        sess_map: Dict[str, Any] = {}
        inbox_map: Dict[str, Any] = {}
        _sess_key = None
        _unhealthy = frozenset()
        try:
            from src.integrations.platform_session_health import (
                UNHEALTHY_STATUSES, ensure_seeded_from_registry,
                get_platform_session_health,
            )
            ensure_seeded_from_registry()
            hp = get_platform_session_health()
            _dump = hp.dump() or {}
            sess_map = _dump.get("sessions") or {}
            inbox_map = _dump.get("inbox_health") or {}
            _sess_key = hp._key
            _unhealthy = UNHEALTHY_STATUSES
        except Exception:
            sess_map = {}
            inbox_map = {}
            logger.debug("[chats] 读取平台会话健康表失败", exc_info=True)

        # 身份可视化：每号补生效人设 id/显示名（meta.persona_id → persona_ids[0] →
        # 默认配置），坐席据此一眼看清「哪个号、哪个人设在回」。resolver/PersonaManager
        # 任一不可用则字段留空；同一 pid 名字查一次（_pname_cache）。
        _resolve_pid = None
        try:
            from src.ai.persona_voice import resolve_account_persona_id as _resolve_pid
        except Exception:
            _resolve_pid = None
        _pm = None
        try:
            from src.utils.persona_manager import PersonaManager
            _pm = PersonaManager.get_instance()
        except Exception:
            _pm = None
        _pname_cache: Dict[str, str] = {}

        # 收尾：对所有 platform_status 条目（含未经编排器的 A 线 default）统一用
        # 注册表 label / self_* 覆盖，确保改名 + 真实身份对每个号都即时反映。
        # 关键：适配器可能用裸平台名当 key（如 telegram 适配器 key="telegram"，
        # account_id="default"），而 label_map/profile_map 用 "平台:账号" 组合键，
        # 故这里以条目自身 platform+account_id 派生查找键（回落到字典 key），防漏配。
        for k, v in platform_status.items():
            if not isinstance(v, dict):
                continue
            # 身份可视化：账号级人设 id/名（逐号软失败留空串，绝不让 chats 500）
            v["persona_id"] = ""
            v["persona_name"] = ""
            # #156（2026-09-03）：新账号不再自动补默认人设 → 账号栏要显式说
            # 「未选人设」并引导去选。判据是**注册表有没有人选过**，不看配置
            # 全局默认（那正是让「没人选过」看起来像「已选好」的东西）。
            # 未选期间 AI 不代答（effective_automation 同源封顶 review）。
            v["persona_unselected"] = False
            try:
                from src.ai.persona_voice import account_persona_unselected
                v["persona_unselected"] = bool(account_persona_unselected(
                    cfg, str(v.get("platform") or ""),
                    str(v.get("account_id") or "default"),
                    registry=registry_obj))
            except Exception:
                logger.debug("[chats] 未选人设判定失败 key=%s", k, exc_info=True)
            if _resolve_pid is not None:
                try:
                    _pid = _resolve_pid(
                        cfg, str(v.get("platform") or ""),
                        str(v.get("account_id") or "default"),
                        registry=registry_obj)
                    if _pid:
                        if _pid not in _pname_cache:
                            _nm = ""
                            try:
                                if _pm is not None:
                                    _nm = str((_pm.get_persona_by_id(_pid) or {})
                                              .get("name") or "")
                            except Exception:
                                _nm = ""
                            _pname_cache[_pid] = _nm or _pid   # 拿不到名字回落 id
                        v["persona_id"] = _pid
                        v["persona_name"] = _pname_cache[_pid]
                except Exception:
                    logger.debug("[chats] 账号人设解析失败 key=%s", k, exc_info=True)
            pkey = f"{v.get('platform') or ''}:{v.get('account_id') or ''}"
            lk = pkey if label_map.get(pkey) else k
            pk = pkey if profile_map.get(pkey) else k
            if label_map.get(lk):
                v["label"] = label_map[lk]
            if profile_map.get(pk):
                for _sk, _sv in profile_map[pk].items():
                    v[_sk] = _sv
            if registry_keys is not None:
                v["removable"] = pkey in registry_keys or k in registry_keys
            if _sess_key is not None:
                try:
                    _sk = _sess_key(
                        str(v.get("platform") or ""),
                        str(v.get("account_id") or ""))
                    if sess_map:
                        sess = sess_map.get(_sk) or {}
                        since = float(sess.get("unhealthy_since") or 0.0)
                        if str(sess.get("status") or "") in _unhealthy and since > 0:
                            v["unhealthy_since"] = float(since)
                    # P4：登录绿但入站半死（E2EE 等）——账号轨直接可见
                    if inbox_map:
                        ih = inbox_map.get(_sk) or {}
                        stall_since = float(ih.get("stall_since") or 0.0)
                        if stall_since > 0:
                            v["inbox_stalled"] = True
                            v["stall_since"] = stall_since
                            v["stall_kind"] = str(ih.get("stall_kind") or "")
                            v["inbox_hint"] = str(
                                ih.get("hint_code")
                                or ih.get("detail")
                                or "e2ee_relogin")[:64]
                            try:
                                _er = float(ih.get("e2ee_ratio"))
                                if _er >= 0.0:
                                    v["e2ee_ratio"] = _er
                            except (TypeError, ValueError):
                                pass
                except Exception:
                    logger.debug("[chats] 会话/入站健康透传失败", exc_info=True)
    except Exception:
        logger.debug("[chats] 并入编排器账号状态失败", exc_info=True)


def _local_day_start_ts(now: Optional[float] = None) -> float:
    """本机日历日 00:00 的 unix 时间戳（与前端 toDateString()「今日」口径对齐）。"""
    t = time.localtime(float(now if now is not None else time.time()))
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


# P8 attn 聚合近窗（秒）：SLA crit=「末条是对方且等待超阈」，等待无上界——不设近窗
# 则任何以对方消息收尾的沉睡死会话永远計 crit，红点从「现在要处理」蜕化成「历史欠账」。
_ATTN_LOOKBACK_SEC = 72 * 3600


def _handoff_tag() -> str:
    """「需人工」转接标签常量（与 protocol_autoreply.HANDOFF_TAG / 前端 _convNeedsHuman 同源）。

    兜底用 unicode 转义：本文件已密封 0 硬编码 CJK 响应文案（ratchet 门禁），
    字面量会被扫描计数。
    """
    try:
        from src.integrations.protocol_autoreply import HANDOFF_TAG
        return str(HANDOFF_TAG)
    except Exception:
        return "\u9700\u4eba\u5de5"


def _attn_aggregate_map(
    store: Any, *, crit_sec: float,
    now: Optional[float] = None, lookback_sec: float = _ATTN_LOOKBACK_SEC,
) -> Dict[str, int]:
    """按 ``platform:account_id`` 聚合「需人工关注」会话数（rail 红点分级脱离 top-N 窗口）。

    口径与前端窗口版（``sla_level=='crit' 或含「需人工」标签``）一致，外加两点修正：
    - 只看近 ``lookback_sec`` 内有动静的会话（见 _ATTN_LOOKBACK_SEC 注释）；
    - 排除已归档 / 搁置未到点——坐席已显式说「不用管/晚点管」的会话计红点属反向噪音
      （窗口版顺带计入是盲点，此处修正）。
    失败/空 store → {}（前端回落窗口内求和）。
    """
    out: Dict[str, int] = {}
    if store is None:
        return out
    ts_now = float(now if now is not None else time.time())
    try:
        convs = store.conversations_active_since(ts_now - float(lookback_sec)) or []
    except Exception:
        logger.debug("[chats] attn 聚合取会话失败", exc_info=True)
        return out
    if not convs:
        return out
    cids = [str(c.get("conversation_id") or "") for c in convs
            if c.get("conversation_id")]
    try:
        dirs = store.last_message_dirs(cids) or {}
    except Exception:
        dirs = {}
    try:
        meta = store.list_conv_tags_map(cids) or {}
    except Exception:
        meta = {}
    tag = _handoff_tag()
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        m = meta.get(cid) or {}
        if m.get("archived"):
            continue
        if float(m.get("snooze_until") or 0) > ts_now:
            continue
        info = dirs.get(cid) or {}
        crit = (str(info.get("direction") or "") == "in"
                and (ts_now - float(info.get("ts") or ts_now)) >= float(crit_sec))
        needs_human = any(tag in str(t) for t in (m.get("tags") or []))
        if not (crit or needs_human):
            continue
        key = f"{c.get('platform') or 'web'}:{c.get('account_id') or 'default'}"
        out[key] = int(out.get(key) or 0) + 1
    return out


def _unread_aggregate_maps(
    store: Any, *, include_archived: bool = False,
) -> tuple[Dict[str, int], Dict[str, int]]:
    """按**清单同一口径**聚合有效未读 → (by_account, by_platform)。

    by_account 键 = ``platform:account_id``；by_platform 为各账号合计。
    store 不可用/失败 → 两个空 dict（前端回落窗口内求和）。
    默认剔除已归档会话（与列表默认视图同一口径，#120）；
    ``include_archived=True``＝``inbox.badge.include_archived`` 显式回退旧口径。

    #159（2026-09-03 证据包 G4BYWH：steven 号三处显示 7、清单一条都没有）：
    此前直取 ``store.sum_effective_unread_by_account``，而清单在取行之后还要
    过一串清单专属剔除（删除墓碑 / 消息全被软删 / 协议号纯占位残值）——徽标
    少了这几道就成了幽灵数字。口径收进 ``src/inbox/unread_aggregate``，与
    「点数字直达那 N 条」用同一份 WHERE：点进去的条数与徽标恒等，对不上
    即真 bug 而非两套口径各说各话。新模块失败 → 回落旧聚合（宁可回到已知的
    偏大口径，也不让徽标整个消失）。
    """
    by_acct: Dict[str, int] = {}
    by_plat: Dict[str, int] = {}
    if store is None:
        return by_acct, by_plat
    try:
        from src.inbox.unread_aggregate import unread_maps
        maps = unread_maps(store, include_archived=include_archived)
        if maps is not None:
            # 空 dict 是**有效结果**（「确实一条未读都没有」＝#159 的正解），
            # 只有 None（聚合没跑成）才回落旧口径——否则幽灵数字原样复活。
            return maps
    except Exception:
        logger.debug("[chats] 清单口径未读聚合失败（回落旧聚合）", exc_info=True)
    try:
        raw = store.sum_effective_unread_by_account(
            include_archived=include_archived) or {}
    except TypeError:
        # 旧 store（无 include_archived 形参）：按其自身口径取数
        raw = store.sum_effective_unread_by_account() or {}
    except Exception:
        logger.debug("[chats] 有效未读聚合失败", exc_info=True)
        return {}, {}
    for (plat, aid), n in raw.items():
        n_i = int(n or 0)
        if n_i <= 0:
            continue
        p = str(plat or "web")
        a = str(aid or "default")
        by_acct[f"{p}:{a}"] = n_i
        by_plat[p] = int(by_plat.get(p) or 0) + n_i
    return by_acct, by_plat


def _badge_include_archived(config_manager) -> bool:
    """``inbox.badge.include_archived``（默认 False＝徽标剔除归档未读）。

    托管存量机升级即生效的新行为；True＝显式回退 #120 之前的全库口径
    （归档未读也计入主徽标）。读失败按默认（新行为）。
    """
    try:
        cfg = (getattr(config_manager, "config", None) or {})
        return bool(((cfg.get("inbox") or {}).get("badge") or {})
                    .get("include_archived", False))
    except Exception:
        return False


def _ai_skip_groups(config_manager) -> bool:
    """``inbox.auto_draft.skip_group_chats``（#222：群/频道是否被 AutoDraft 跳过）。

    前端群/频道视图据此显示「AI 不处理群消息，未读不计入账号角标」说明；读失败
    按 False（宁可不说明，不说错）。与 autodraft_helpers 读同一键。
    """
    try:
        cfg = (getattr(config_manager, "config", None) or {})
        return bool((((cfg.get("inbox") or {}).get("auto_draft") or {})
                     .get("skip_group_chats", False)))
    except Exception:
        return False


def _archived_unread_map(store: Any) -> Dict[str, Dict[str, int]]:
    """归档中的有效未读聚合 → ``{"platform:account_id": {convs, unread}}``。

    「被埋会话」告警（health_watchdog._check_buried_conversations）的界面化
    取数口：主徽标剔除归档未读后，这份 map 驱动工作台「归档中还有 N 条未读」
    入口——全库口径，不受 top-N 窗口截断。store 不可用/旧版无此方法 → {}
    （前端回落窗口内推导，行为同旧版）。
    """
    if store is None or not hasattr(store, "sum_archived_unread_by_account"):
        return {}
    try:
        raw = store.sum_archived_unread_by_account() or {}
    except Exception:
        logger.debug("[chats] 归档未读聚合失败", exc_info=True)
        return {}
    out: Dict[str, Dict[str, int]] = {}
    for (plat, aid), v in raw.items():
        n = int((v or {}).get("unread") or 0)
        if n <= 0:
            continue
        p = str(plat or "web")
        a = str(aid or "default")
        out[f"{p}:{a}"] = {"convs": int((v or {}).get("convs") or 0),
                           "unread": n}
    return out


def _accounts_summary_list(
    platform_status: Dict[str, Any],
    status_map: Dict[tuple, str],
    directory: Dict[tuple, Dict[str, float]],
    unread_map: Dict[str, int],
    attn_map: Dict[str, int],
    registry_active: Optional[Dict[tuple, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """账号真相单源（P0 2026-08-17）：dock「历史 N」与抽屉历史分区共同消费的全库账号名录。

    背景实录：顶部 dock 显示 10 个账号、管理抽屉只有 5 个——dock 从会话窗口反推出
    「幽灵号」而抽屉只读 platform_status，两边各算一套。本函数把四层账号一次收拢：

    - **已接入**（platform_status）：``paired=True``，status 按 running/state 归一为
      online / connecting / error / offline；
    - **注册表非活跃**（``_account_status_map``）：offline → ``logged_out``（可重登）、
      removed → ``removed``（软删只读）；
    - **注册表活跃但无运行通道**（``_registry_active_map``，P4 幽灵收编）：
      ``mode="desktop"`` → ``desktop``（桌面壳镜像号：消息经桌面端同步，刻意无云端
      worker——此前坠成 history_only 幽灵，与 ops「账号真相」卡的注册表口径打架）；
      其余 → ``registered``（在册但 worker 未接管，如 state=stopped 的滞留行）；
    - **仅历史**（会话库出现过、上三处都没有）：``history_only``——修后这才是
      真幽灵（绕过注册表在写会话），ops 卡 ghost 数与这里终于同一口径。

    unread/attn 沿用上游**已剔除 dead 账号**的聚合 map——logged_out/removed 恒为 0
    （不可回复的未读不亮灯），desktop/registered/history_only 的存量未读如实带出
    （前端展示为灰色汇总角标，绝不与在线号的红色 attn 混淆）。conv_count/last_ts
    来自全库名录，paired 账号同样带上（抽屉卡片/hover 富化可直接用）。纯函数，零 I/O。
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def _row(pl: str, aid: str, status: str, paired: bool,
             label: str = "") -> Dict[str, Any]:
        key = f"{pl}:{aid}"
        d = (directory or {}).get((pl, aid)) or {}
        dead = status in ("logged_out", "removed")
        row = {
            "platform": pl, "account_id": aid,
            "status": status, "paired": paired,
            "unread": 0 if dead else int((unread_map or {}).get(key, 0) or 0),
            "attn": 0 if dead else int((attn_map or {}).get(key, 0) or 0),
            "conv_count": int(d.get("count") or 0),
            "last_ts": float(d.get("last_ts") or 0.0),
        }
        if label:
            row["label"] = label
        return row

    for v in (platform_status or {}).values():
        if not isinstance(v, dict):
            continue
        pl = str(v.get("platform") or "web")
        aid = str(v.get("account_id") or "default")
        if (pl, aid) in seen:
            continue
        seen.add((pl, aid))
        if bool(v.get("running")):
            st = "online"
        else:
            state = str(v.get("state") or "")
            st = ("connecting" if state == "starting"
                  else "error" if state == "error" else "offline")
        out.append(_row(pl, aid, st, True))
    for (pl, aid), reg_st in (status_map or {}).items():
        p, a = str(pl or "web"), str(aid or "default")
        if (p, a) in seen:
            continue
        seen.add((p, a))
        out.append(_row(p, a, "logged_out" if reg_st == "offline" else "removed",
                        False))
    for (pl, aid), info in (registry_active or {}).items():
        p, a = str(pl or "web"), str(aid or "default")
        if (p, a) in seen:
            continue
        seen.add((p, a))
        mode = str((info or {}).get("mode") or "")
        out.append(_row(p, a, "desktop" if mode == "desktop" else "registered",
                        False, label=str((info or {}).get("label") or "")))
    for (pl, aid) in (directory or {}):
        p, a = str(pl or "web"), str(aid or "default")
        if (p, a) in seen:
            continue
        seen.add((p, a))
        out.append(_row(p, a, "history_only", False))
    return out


def _enrich_platform_status_value(
    platform_status: Dict[str, Any], store: Any,
    *, unread_by_account: Optional[Dict[str, int]] = None,
    attn_by_account: Optional[Dict[str, int]] = None,
) -> None:
    """P4：给账号卡注入价值信息字段（best-effort，失败不阻断列表）。

    - ``today_conv_count``：该账号今日有活动（last_ts ≥ 今日 0 点）的会话数；
    - ``unread``（P6）：该账号有效未读合计（全库口径，非 top-N 窗口）；
    - ``attn``（P8）：该账号「需人工关注」会话数（近窗 SLA crit + 需人工标签）；
    - Telegram 另附：``last_sync_ts``（内存快照 finished_at 与 registry 持久化取较大）、
      ``sync_state`` / ``sync_dialogs_done`` / ``sync_dialogs_total``（进行中进度）。
    """
    if not platform_status:
        return
    counts: Dict[Any, int] = {}
    if store is not None:
        try:
            counts = store.count_conversations_active_since(_local_day_start_ts()) or {}
        except Exception:
            logger.debug("[chats] 今日会话计数失败", exc_info=True)
            counts = {}
    unread_map = unread_by_account if unread_by_account is not None else {}
    if store is not None and unread_by_account is None:
        try:
            unread_map, _ = _unread_aggregate_maps(store)
        except Exception:
            unread_map = {}

    # 批量读注册表持久化同步时间（避免每个 TG 账号一次 get）
    persisted: Dict[str, float] = {}
    try:
        from src.integrations.account_registry import get_account_registry
        for row in get_account_registry().list(platform="telegram"):
            aid = str(row.get("account_id") or "")
            ts = float((row.get("meta") or {}).get("last_history_sync_ts") or 0)
            if aid and ts > 0:
                persisted[aid] = ts
    except Exception:
        logger.debug("[chats] 读持久化 sync 时间失败", exc_info=True)

    try:
        from src.web.routes.unified_inbox_account_routes import tg_history_sync_snapshot
    except Exception:
        tg_history_sync_snapshot = None  # type: ignore

    for _k, v in platform_status.items():
        if not isinstance(v, dict):
            continue
        plat = str(v.get("platform") or "")
        aid = str(v.get("account_id") or "")
        v["today_conv_count"] = int(counts.get((plat, aid), 0) or 0)
        v["unread"] = int(unread_map.get(f"{plat}:{aid}", 0) or 0)
        if attn_by_account is not None:
            v["attn"] = int(attn_by_account.get(f"{plat}:{aid}", 0) or 0)
        if plat != "telegram":
            continue
        snap: Dict[str, Any] = {}
        if tg_history_sync_snapshot is not None:
            try:
                snap = tg_history_sync_snapshot(aid) or {}
            except Exception:
                snap = {}
        mem_ts = float(snap.get("finished_at") or 0)
        last = max(mem_ts, float(persisted.get(aid) or 0))
        if last > 0:
            v["last_sync_ts"] = last
        state = str(snap.get("state") or "idle")
        if state == "running":
            v["sync_state"] = "running"
            v["sync_dialogs_done"] = int(snap.get("dialogs_done") or 0)
            v["sync_dialogs_total"] = int(snap.get("dialogs_total") or 0)
        elif state == "error" and snap.get("error"):
            v["sync_state"] = "error"
            v["sync_error"] = str(snap.get("error") or "")[:120]


def _enrich_chat_list(request: Request, chats: List[Dict[str, Any]], *, config_manager) -> None:
    """Best-effort 富集会话列表：contact 关联 / SLA / tags / 自动派单建议。"""
    try:
        cstore = _contacts_store(request)
        if cstore is not None and chats:
            pairs = [(str(c.get("platform") or ""), str(c.get("chat_key") or ""))
                     for c in chats]
            cmap = cstore.resolve_contacts_by_external(pairs)
            overdue = cstore.overdue_contact_ids()
            for c in chats:
                cid = cmap.get((str(c.get("platform") or ""),
                                str(c.get("chat_key") or "")))
                if cid:
                    c["contact_id"] = cid
                    c["follow_up_overdue"] = cid in overdue
    except Exception:
        logger.debug("会话列表 contact 关联失败（已忽略）", exc_info=True)

    try:
        ibx = _inbox_store(request)
        if ibx is not None and chats:
            sla = _sla_cfg(request)
            cids = [str(c.get("conversation_id") or "") for c in chats]
            dirs = ibx.last_message_dirs([x for x in cids if x])
            now = time.time()
            # P0+：已退出/已移除账号无法回复——再亮 SLA 红 chip 只会逼坐席去点
            # 打不开的发送框。行上缺 account_status 时回查注册表映射（live 路径兜底）。
            try:
                status_map = _account_status_map(request) or {}
            except Exception:
                status_map = {}
            for c in chats:
                info = dirs.get(str(c.get("conversation_id") or ""))
                # P2：顺手挂最后一条消息方向（in=对方/out=我方），供列表预览前缀，零额外查询
                c["last_direction"] = (info.get("direction") or "") if info else ""
                st = str(c.get("account_status") or "")
                if not st:
                    st = status_map.get(
                        (str(c.get("platform") or ""),
                         str(c.get("account_id") or "default")), "") or ""
                if info and info.get("direction") == "in":
                    wait = max(0, int(now - (info.get("ts") or now)))
                    c["unanswered_sec"] = wait
                    if st in ("offline", "removed"):
                        c["sla_breach"] = False
                        c["sla_level"] = ""
                    else:
                        c["sla_breach"] = wait >= sla["warn"]
                        c["sla_level"] = ("crit" if wait >= sla["crit"]
                                          else "warn" if wait >= sla["warn"]
                                          else "")
                else:
                    c["unanswered_sec"] = 0
                    c["sla_breach"] = False
                    c["sla_level"] = ""
    except Exception:
        logger.debug("会话列表 SLA 统计失败（已忽略）", exc_info=True)

    try:
        ibx2 = _inbox_store(request)
        if ibx2 is not None and chats:
            cids2 = [str(c.get("conversation_id") or "") for c in chats if c.get("conversation_id")]
            tags_map = ibx2.list_conv_tags_map(cids2)
            for c in chats:
                cid2 = str(c.get("conversation_id") or "")
                meta2 = tags_map.get(cid2, {})
                c["conv_tags"] = meta2.get("tags", [])
                c["archived"] = meta2.get("archived", False)
                # P0-companion：搁置到点（epoch 秒，0=未搁置）→ 前端「超时/待接管」视图隐藏 + header 搁置态
                c["snooze_until"] = meta2.get("snooze_until", 0)
                # 2026-08-17 官方级消息管理：置顶时刻（0=未置顶）→ 前端置顶恒排顶部 + 📌 徽标
                c["pinned_at"] = meta2.get("pinned_at", 0)
                # 实施74（实施69 P1-1）：「需人工」chip 悬停自解释（何时/为何/谁打的）
                _hm = meta2.get("handoff_meta") or {}
                if _hm:
                    c["handoff_meta"] = _hm
    except Exception:
        logger.debug("会话列表 tags 加载失败（已忽略）", exc_info=True)

    try:
        # B-2 风控可视：批量标记今日命中风控转人工(blocked)的会话，供列表高亮，
        # 与全自动安全条形成闭环（看到拦截数 → 列表一眼定位被拦会话）。单次 IN 查询。
        ibx3 = _inbox_store(request)
        if ibx3 is not None and chats and hasattr(ibx3, "conversations_blocked_counts"):
            from datetime import datetime as _dt
            _now = _dt.now()
            _since = _dt(_now.year, _now.month, _now.day).timestamp()
            cids3 = [str(c.get("conversation_id") or "") for c in chats if c.get("conversation_id")]
            blocked_map = ibx3.conversations_blocked_counts(cids3, since_ts=_since)
            for c in chats:
                n = blocked_map.get(str(c.get("conversation_id") or ""), 0)
                c["risk_blocked"] = int(n)
    except Exception:
        logger.debug("会话列表风控拦截标记失败（已忽略）", exc_info=True)

    try:
        # 身份可视化（2026-07-30）：行级「生效人设」。会话级覆写
        # （inbox.persona_conv_override）让一条会话以另一个人设出站，但列表行徽章
        # 只读 accountMeta 的账号级绑定 → 覆写会话在列表里显示错人设（与回复区
        # 身份条同一盲区，那边已由 _identBarUpgrade 修掉）。这里在每行附
        # eff_persona{id,name,tier}，前端徽章优先消费；字段缺失＝按账号级回落
        # （老前端/关开关/无覆写 全部零回归）。
        # 刻意只附 conv_override 档：account_profile 与前端 accountMeta 同源（附了
        # 是冗余）；domain/default 兜底档附上去会把「账号未绑人设」的黄点警示吞掉
        # （那是运营要看的配置缺口信号）。legacy peer 绑定（chat_binding）在
        # 「账号人设优先」修复后只对未绑账号生效、且属清理中旧债——known boundary，
        # 行徽章暂不表达（打开会话后身份条会说真话）。
        # 成本：PersonaManager 单例内存 dict，每行 1 次 get；开关关＝整块零开销。
        if chats:
            from src.ai.persona_voice import (
                conv_binding_key,
                conv_override_enabled,
            )
            cfg_full = (config_manager.config
                        if config_manager is not None else {}) or {}
            if conv_override_enabled(cfg_full):
                from src.utils.persona_manager import PersonaManager
                pm = PersonaManager.get_instance()
                brief_cache: Dict[str, Any] = {}

                def _eff_brief(pid: str):
                    """id→{id,name} 摘要（进程内按本次请求 memo）；profile 已删
                    → None（与 resolve_effective_persona「悬空引用视同未覆写」同语义）。"""
                    if pid not in brief_cache:
                        p = pm.get_persona_by_id(pid)
                        brief_cache[pid] = (
                            {"id": str(p.get("id") or pid),
                             "name": str(p.get("name") or pid)}
                            if isinstance(p, dict) else None)
                    return brief_cache[pid]

                for c in chats:
                    try:
                        plat = str(c.get("platform") or "")
                        acct = str(c.get("account_id") or "")
                        ck = str(c.get("chat_key") or "")
                        if not (plat and acct and ck):
                            continue
                        ref = pm.get_chat_binding_ref(
                            conv_binding_key(plat, acct, ck))
                        if not ref:
                            continue
                        b = _eff_brief(ref)
                        if b:
                            c["eff_persona"] = dict(b, tier="conv_override")
                    except Exception:
                        continue
    except Exception:
        logger.debug("会话列表生效人设富集失败（已忽略）", exc_info=True)

    try:
        from src.workspace.assignment import AssignmentService
        asvc = AssignmentService.from_config(
            (config_manager.config if config_manager is not None else {}) or {}
        )
        if asvc.enabled and chats:
            from src.workspace.agent_coordinator import AgentCoordinator
            coord = AgentCoordinator.from_request(request, config_manager)
            sugg = asvc.suggest_for_chats(
                chats=chats,
                presence=coord.list_presence(),
                claims=coord.list_claims(),
            )
            for c in chats:
                s = sugg.get(str(c.get("conversation_id") or ""))
                if s:
                    c["suggested_agent"] = s
    except Exception:
        logger.debug("会话列表自动派单建议失败（已忽略）", exc_info=True)


def register_read_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载主读路径端点（chats GET / thread GET）。"""

    @app.get("/api/unified-inbox/chats")
    async def api_unified_inbox_chats(
        request: Request, limit: int = 30, before_ts: float = 0,
        platform: str = "", account_id: str = "", include_hidden: int = 0,
    ):
        api_auth(request)

        def _deliver_paused_flag() -> str:
            """全局真发暂停原因（空串=未暂停）；异常按未暂停（fail-open）。"""
            try:
                from src.inbox.automation_mode import deliver_paused_reason
                return deliver_paused_reason(
                    getattr(config_manager, "config", None))
            except Exception:
                return ""

        def _deliver_paused_meta():
            """#142 应急态横幅数据：暂停元信息（谁关的/何时/几点自动恢复）+
            can_resume 能力位（主管角色，与一键恢复端点同闸）。未暂停/异常
            → None（前端不渲染横幅，旧后端零回归）。"""
            try:
                if not _deliver_paused_flag():
                    return None
                from src.inbox.autosend_gate_state import (
                    config_dir_from_manager, pause_meta,
                )
                from src.web.routes.unified_inbox_auth import _is_supervisor
                meta = pause_meta(
                    getattr(config_manager, "config", None),
                    config_dir_from_manager(config_manager)) or {}
                meta["can_resume"] = bool(_is_supervisor(request))
                return meta
            except Exception:
                logger.debug("[chats] deliver_paused_meta 失败（忽略）",
                             exc_info=True)
                return None
        # scoped 过滤参数规范化：platform 小写（store 落库口径即小写平台名）、
        # account_id 保大小写（协议号/RPA 账号 id 可能大小写敏感）。
        platform = str(platform or "").strip().lower()
        account_id = str(account_id or "").strip()
        scoped = bool(platform or account_id)
        # 历史账号只读视角（P0 2026-08-17）：已退出(offline)账号会话默认服务端
        # 过滤，抽屉「查看会话」需要看到 → include_hidden=1 放行（行带 read_only）。
        # **仅 account_id scoped 请求可开**：全局列表口径不变（防已退出号历史
        # 污染活跃工作台）。响应带 hidden_included=true 供前端特性探测旧后端。
        want_hidden = bool(include_hidden) and bool(account_id)

        # ── 十期：游标分页页（before_ts>0）——仅 store 有历史分页语义 ──
        # live 聚合是各平台「最近 N 条」快照，翻不出更旧的；分页页直接 store 读，
        # 首页已完成 ingest 旁路，这里无需再跑实时聚合。platform/account_id 透传
        # 后 scoped 翻页自然工作（无参数时行为与旧版逐字段一致）。
        if before_ts and float(before_ts) > 0:
            limit = max(5, min(100, int(limit or 30)))
            older = _collect_chats_from_store(
                request, limit=limit, before_ts=float(before_ts),
                platform=platform, account_id=account_id,
                include_hidden=want_hidden)
            older = older or []
            _enrich_chat_list(request, older, config_manager=config_manager)
            oldest = min((float(c.get("last_ts") or 0) for c in older), default=0.0)
            has_more = False
            store = _inbox_store(request)
            if store is not None and oldest > 0:
                try:
                    has_more = store.count_conversations_older_than(
                        oldest, platform=platform, account_id=account_id) > 0
                except Exception:
                    has_more = False
            return {
                "ok": True, "ts": time.time(), "chats": older,
                "has_more": has_more, "oldest_ts": oldest or None,
            }

        # ── scoped 首页（platform/account_id 非空）：直接按需查 store ──
        # 根因：前端全局轮询只拿最近 100 条（live 聚合快照 + top100 截断），
        # 点开某账号时其老会话不在窗口内 → 「看不到会话」。账号视角改为绕过
        # 全局截断、按 (platform, account_id) 直查持久事实源；limit 单独放宽到
        # 200（单账号列表本就该一次多拿些）。store 不可用 → 回落下方全量路径
        # （行为兼容绝不 500，回落响应形状与无参数版完全一致）。
        if scoped:
            slimit = max(5, min(200, int(limit or 30)))
            scoped_chats = _collect_chats_from_store(
                request, limit=slimit, platform=platform, account_id=account_id,
                include_hidden=want_hidden)
            if scoped_chats is not None:
                _enrich_chat_list(request, scoped_chats, config_manager=config_manager)
                oldest = min((float(c.get("last_ts") or 0) for c in scoped_chats),
                             default=0.0)
                has_more = False
                store = _inbox_store(request)
                if store is not None and oldest > 0:
                    try:
                        has_more = store.count_conversations_older_than(
                            oldest, platform=platform, account_id=account_id) > 0
                    except Exception:
                        has_more = False
                # 刻意不带 platform_status：账号切换是高频操作，省一趟 orchestrator
                # /adapter 状态聚合开销——前端全局轮询已持有该数据。
                return {
                    "ok": True, "ts": time.time(), "chats": scoped_chats,
                    "has_more": has_more, "oldest_ts": oldest or None,
                    "scope": {"platform": platform, "account_id": account_id},
                    # 特性探测标记：前端历史视角据此区分「后端已放行隐藏行」vs
                    # 「旧后端忽略了参数」（后者对已退出号给诚实空态而非装无会话）
                    "hidden_included": want_hidden,
                    "deliver_paused": _deliver_paused_flag(),
                    "deliver_paused_meta": _deliver_paused_meta(),
                }

        limit = max(5, min(100, int(limit or 30)))
        chats = _chats_for_listing(request, limit=limit)
        platform_status: Dict[str, Any] = status_via_adapters(request, _INBOX_ADAPTERS)
        _merge_orchestrator_status(platform_status, config_manager)
        _enrich_chat_list(request, chats, config_manager=config_manager)
        # 首页也回 has_more/oldest_ts（store 可用时），前端据此挂「加载更多」
        oldest = min((float(c.get("last_ts") or 0) for c in chats), default=0.0)
        has_more = False
        store = _inbox_store(request)
        if store is not None and oldest > 0:
            try:
                has_more = store.count_conversations_older_than(oldest) > 0
            except Exception:
                has_more = False
        # P4/P6/P8：账号卡价值信息 + 全库有效未读 + 近窗 attn 聚合（rail/chip 脱离 top-N）
        unread_by_account: Dict[str, int] = {}
        unread_by_platform: Dict[str, int] = {}
        try:
            unread_by_account, unread_by_platform = _unread_aggregate_maps(
                store, include_archived=_badge_include_archived(config_manager))
        except Exception:
            logger.debug("[chats] 未读聚合失败", exc_info=True)
        # 被埋会话界面化（#142 报障）：归档中的有效未读单列一份聚合，驱动
        # 「归档中还有 N 条未读」入口——徽标剔归档后，这些未读不能彻底隐身。
        archived_unread_by_account: Dict[str, Dict[str, int]] = {}
        try:
            archived_unread_by_account = _archived_unread_map(store)
        except Exception:
            logger.debug("[chats] 归档未读聚合失败", exc_info=True)
        attn_by_account: Dict[str, int] = {}
        try:
            attn_by_account = _attn_aggregate_map(
                store, crit_sec=_sla_cfg(request)["crit"])
        except Exception:
            logger.debug("[chats] attn 聚合失败", exc_info=True)
        # #222 口径统一：角标旁悬浮明细四桶（private / group / channel / archived）
        # ——主徽标仍只数私聊，但「群组 11 / 频道 2 / 归档 16」里各藏了多少未读要
        # 让坐席一眼看到；群视图角标直接读 group 桶。None＝聚合没跑成 → 不带字段。
        unread_breakdown_by_account: Optional[Dict[str, Dict[str, int]]] = None
        try:
            from src.inbox.unread_aggregate import unread_breakdown
            unread_breakdown_by_account = unread_breakdown(store)
        except Exception:
            logger.debug("[chats] 未读四桶明细失败", exc_info=True)
        # 已退出/已移除：聊天页完全不展示 → 平台徽章 + 账号级未读/attn 一并剔除
        # （历史未读仍在 store，同号重登后自然回显）。
        try:
            _dead = {f"{p}:{a}" for (p, a) in _account_status_map(request)}
            if _dead:
                if unread_breakdown_by_account:
                    unread_breakdown_by_account = {
                        k: v for k, v in unread_breakdown_by_account.items()
                        if k not in _dead}
                for _k in list(unread_by_account):
                    if _k not in _dead:
                        continue
                    _p = _k.split(":", 1)[0]
                    _left = int(unread_by_platform.get(_p) or 0) \
                        - int(unread_by_account[_k] or 0)
                    if _left > 0:
                        unread_by_platform[_p] = _left
                    else:
                        unread_by_platform.pop(_p, None)
                    unread_by_account.pop(_k, None)
                attn_by_account = {k: v for k, v in attn_by_account.items()
                                   if k not in _dead}
                archived_unread_by_account = {
                    k: v for k, v in archived_unread_by_account.items()
                    if k not in _dead}
        except Exception:
            logger.debug("[chats] 已退出账号聚合剔除失败", exc_info=True)
        try:
            _enrich_platform_status_value(
                platform_status, store, unread_by_account=unread_by_account,
                attn_by_account=attn_by_account)
        except Exception:
            logger.debug("[chats] platform_status 价值信息富集失败", exc_info=True)
        # P0（2026-08-17）账号真相单源：dock「历史 N」/抽屉历史分区同源消费；
        # 失败回空列表（前端特性探测：无此字段/空 → 回落客户端窗口推导）。
        accounts_summary: List[Dict[str, Any]] = []
        try:
            accounts_summary = _accounts_summary_list(
                platform_status,
                _account_status_map(request) or {},
                (store.account_directory() if store is not None
                 and hasattr(store, "account_directory") else {}),
                unread_by_account, attn_by_account,
                registry_active=_registry_active_map(request) or {})
        except Exception:
            logger.debug("[chats] 账号名录聚合失败", exc_info=True)
        return {
            "ok": True,
            "ts": time.time(),
            "chats": chats,
            "platform_status": platform_status,
            "has_more": has_more,
            "oldest_ts": oldest or None,
            # P6/P8：全库聚合（键 plat / plat:aid）；scoped/cursor 响应刻意不带
            "unread_by_platform": unread_by_platform,
            "unread_by_account": unread_by_account,
            # #142 被埋会话界面化：归档中的有效未读（键 plat:aid →
            # {convs, unread}，全库口径）。前端「归档中还有 N 条未读」入口
            # 消费；旧前端不识此键零影响，空 dict=无归档未读/旧 store。
            "archived_unread_by_account": archived_unread_by_account,
            "attn_by_account": attn_by_account,
            # #222：四桶明细（键 plat:aid → {private, group, channel, archived}）；
            # 聚合失败时不带字段，前端保持上一轮 / 无明细。
            **({"unread_breakdown_by_account": unread_breakdown_by_account}
               if unread_breakdown_by_account is not None else {}),
            # #222：AutoDraft 是否跳过群/频道（inbox.auto_draft.skip_group_chats）
            # ——前端据此在群/频道视图给「AI 不处理群消息」说明，不猜。
            "ai_skip_groups": _ai_skip_groups(config_manager),
            # P0 账号名录（全库口径；空列表=聚合失败，前端回落客户端推导）
            "accounts_summary": accounts_summary,
            # #12（2026-08-30）：全局真发暂停旗标（非空字符串=原因）。行级「AI」
            # 徽标据此叠加「已暂停真发」黄态；判定单点=deliver_paused_reason
            # （与 effective_automation ⑦ 层同一函数，绝不各算一套）。
            "deliver_paused": _deliver_paused_flag(),
            # #142：应急态横幅数据（谁关的/何时/自动恢复/能否一键恢复）。
            # None=未暂停；旧前端不识此键零影响。
            "deliver_paused_meta": _deliver_paused_meta(),
        }

    @app.post("/api/unified-inbox/mark-read")
    async def api_unified_inbox_mark_read(request: Request):
        """P0 未读可信化：坐席打开会话即写「已读水位」（last_read_ts）到持久层。

        为什么需要：协议号每轮 ``upsert_protocol_chats`` 会用手机端未读数覆盖
        ``conversations.unread``——此前前端 ``_clearUnread`` 只清浏览器内存角标，
        下一轮轮询数字原样回弹（用户报障「点开后数字还在/点开无消息」的根因之一）。
        这里把已读态落库，读路径据 last_read_ts 派生「有效未读」，永不回弹。

        body: ``{platform, account_id, chat_key}`` 或 ``{conversation_id}``；
        可选 ``read_ts``（缺省=该会话当前 last_ts）。**仅坐席主动打开时调用**——
        轮询刷新绝不可调，否则水位一路推到最新、未读永远归零。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = str((body or {}).get("conversation_id") or "").strip()
        if not cid:
            platform = str((body or {}).get("platform") or "").lower()
            account_id = str((body or {}).get("account_id") or "default")
            chat_key = str((body or {}).get("chat_key") or "").strip()
            if not platform or not chat_key:
                raise HTTPException(400, tr(request, "err.ws.field_required",
                                            field="chat_key"))
            cid = conv_id(platform, account_id, chat_key)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        read_ts_raw = (body or {}).get("read_ts")
        try:
            read_ts = float(read_ts_raw) if read_ts_raw is not None else None
        except (TypeError, ValueError):
            read_ts = None
        try:
            water = store.mark_conversation_read(cid, read_ts=read_ts)
        except Exception:
            logger.debug("[inbox] mark-read 落库失败 cid=%s", cid, exc_info=True)
            return {"ok": False, "conversation_id": cid}
        # P0 未读可信化 v2（2026-08-23）：把坐席「已读」同步回平台（read receipt）。
        # 协议号手机端未读残值是徽标回弹的持续供体——effective_unread 的
        # last_in_ts 闸门只是止血，平台侧清零才断根。默认关（已读回执是客户可见
        # 行为，按平台开关属产品决策；AI 自动回复链早已在发回执，这里补齐
        # 「人工阅读」缺口使两条链行为一致）。fire-and-forget：协议 worker 与本
        # 路由同在 web loop，create_task 即可，绝不阻塞水位写入的返回。
        try:
            from src.inbox import read_sync as _rs
            _cfg = (config_manager.config if config_manager is not None else {}) or {}
            _row = None
            try:
                _row = store.get_conversation(cid)
            except Exception:
                _row = None
            _plat = str((_row or {}).get("platform")
                        or (body or {}).get("platform") or "").lower()
            if _plat and _rs.push_enabled(_cfg, _plat):
                if int((_row or {}).get("unread") or 0) <= 0:
                    _rs.note_no_unread()   # 手机端已读净——不必打扰 worker
                elif _rs.should_push(cid):
                    _acct = str((_row or {}).get("account_id")
                                or (body or {}).get("account_id") or "default")
                    _ck = str((_row or {}).get("chat_key")
                              or (body or {}).get("chat_key") or "")
                    if _ck:
                        _rs.record_push(cid)

                        async def _push_receipt(p=_plat, a=_acct, k=_ck):
                            try:
                                from src.integrations.account_orchestrator import (
                                    get_orchestrator,
                                )
                                ok = bool(await get_orchestrator(_cfg)
                                          .mark_read(p, a, k))
                                _rs.note_result(ok)
                            except Exception:
                                _rs.note_result(False)
                                logger.debug(
                                    "[inbox] read-sync 平台回执失败 %s:%s",
                                    p, a, exc_info=True)

                        asyncio.create_task(_push_receipt())
        except Exception:
            logger.debug("[inbox] read-sync 决策失败（已忽略）", exc_info=True)
        return {"ok": True, "conversation_id": cid, "last_read_ts": water}

    @app.post("/api/unified-inbox/travel-state/clear")
    async def api_unified_inbox_travel_state_clear(request: Request):
        """#113：一键清除会话的「临时行程」派生状态——回归档案常驻地。

        行程状态本体是对 assistant 历史的扫描派生（self_claims travel 锚），
        无独立存储可删；本端点写 conversation_meta.travel_cleared_ts 水位，
        草稿链与 send-caps 可见面都按水位忽略更早的行程自述。
        body: ``{platform, account_id, chat_key}``。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        platform = str((body or {}).get("platform") or "").lower().strip()
        account_id = str((body or {}).get("account_id") or "default").strip()
        chat_key = str((body or {}).get("chat_key") or "").strip()
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="platform/chat_key"))
        store = _inbox_store(request)
        if store is None or not hasattr(store, "set_travel_cleared"):
            raise HTTPException(503, tr(request, "err.ws.store_unavailable"))
        cid = f"{platform}:{account_id}:{chat_key}"
        ok = bool(store.set_travel_cleared(cid))
        return {"ok": ok, "conversation_id": cid}

    @app.get("/api/unified-inbox/account-unread")
    async def api_unified_inbox_account_unread(
        request: Request, platform: str, account_id: str = "",
        limit: int = 200, scope: str = "private",
    ):
        """账号栏那个数字**具体是哪几条**（#159 幽灵未读，2026-09-03 G4BYWH）。

        坐席看到 7 却在清单里找不到，只能怀疑系统在骗人。徽标既然是个数字，
        就该点得开：本端点与徽标聚合共用同一份 WHERE（差别只在 SUM vs 明细），
        所以「点进去看到的条数」与徽标恒等——对不上就是真 bug，而不是两套
        口径各说各话。前端拿列表可直达那 N 条，也可据此一键清零
        （POST mark-account-read，同一水位机制）。

        query: ``platform``（必填）、``account_id``（空=该平台全部账号）、
        ``limit``（缺省 200，上限 500）、``scope``（#222：private | group |
        channel，与列表视图同名；缺省 private＝主徽标口径）。
        """
        api_auth(request)
        plat = str(platform or "").strip().lower()
        if not plat:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="platform"))
        acct = str(account_id or "").strip()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.unread_aggregate import normalize_scope
        sc = normalize_scope(scope)
        try:
            from src.inbox.unread_aggregate import unread_conversations
            convs = unread_conversations(
                store, plat, acct, limit=limit, scope=sc,
                include_archived=_badge_include_archived(config_manager))
        except Exception:
            logger.debug("[inbox] account-unread 明细失败 %s:%s", plat, acct,
                         exc_info=True)
            convs = []
        return {
            "ok": True, "platform": plat, "account_id": acct, "scope": sc,
            "conversations": convs,
            "unread_total": sum(int(c.get("unread") or 0) for c in convs),
            "count": len(convs),
        }

    @app.post("/api/unified-inbox/buried-mark-read")
    async def api_unified_inbox_buried_mark_read(request: Request):
        """被埋会话（归档着却有未读）一键标已读（#222 修法 3 的手动半边）。

        看门狗装载时只清「人工归档 + 无入站 > 72h」的存量；<72h 的留给横幅由
        坐席定夺——「取消归档」是把它们请回来，「标已读」是确认不再跟进。两者
        都在横幅上，缺后者时坐席只能逐条点开 N 个归档会话。口径与横幅 / 主徽标
        同一份 WHERE（``unread_aggregate.buried_conversations``，私聊 + 有效未读），
        与逐会话 mark-read 同一水位机制，永不回弹。
        body: ``{platform?, account_id?}``（空＝全部）。返回 ``{ok, cleared}``。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        plat = str((body or {}).get("platform") or "").strip().lower()
        acct = str((body or {}).get("account_id") or "").strip()
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            from src.inbox.unread_aggregate import sweep_buried_unread
            n = sweep_buried_unread(store, platform=plat, account_id=acct,
                                    reason="banner_mark_read")
        except Exception:
            logger.debug("[inbox] buried-mark-read 失败 %s:%s", plat, acct,
                         exc_info=True)
            n = 0
        return {"ok": True, "cleared": int(n), "platform": plat, "account_id": acct}

    @app.post("/api/unified-inbox/mark-account-read")
    async def api_unified_inbox_mark_account_read(request: Request):
        """按账号批量清未读（P2 账号真相闭环，2026-08-17）。

        场景＝历史/已退出账号的存量未读清账（抽屉历史行「清未读」按钮）：这些号
        只查档不可回复，灰色角标长期挂着变成新噪音。与逐会话 mark-read 同一水位
        机制（store.mark_account_read 批量推 last_read_ts），永不回弹。
        在册在线号同样可用（语义无害：把该号全部会话标记已读）。
        body: ``{platform, account_id}``。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        platform = str((body or {}).get("platform") or "").lower().strip()
        account_id = str((body or {}).get("account_id") or "").strip()
        if not platform or not account_id:
            raise HTTPException(400, tr(request, "err.ws.field_required",
                                        field="platform/account_id"))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            n = store.mark_account_read(platform, account_id)
        except Exception:
            logger.debug("[inbox] mark-account-read 失败 %s:%s",
                         platform, account_id, exc_info=True)
            return {"ok": False, "platform": platform,
                    "account_id": account_id}
        return {"ok": True, "platform": platform, "account_id": account_id,
                "marked": n}

    @app.post("/api/unified-inbox/conversations/delete")
    async def api_unified_inbox_conversation_delete(request: Request):
        """删除单个会话的全部本地数据（消息/草稿/分析/升级记录等，2026-08-16）。

        - 硬删 + 防复活墓碑：账号在线时目录同步每 ~5min 全量推侧栏占位，无墓碑
          删了就复活；对方**再发新消息**自动解除墓碑回显（绝不丢客户消息）。
        - 只删本机工作台数据，**不动平台侧**（手机/官方端的线程原样保留）。
        - 破坏性操作：拒 agent/viewer（与账号管理写口同一排除法——误删客户
          聊天记录不可恢复）；master/admin/桌面壳 Bearer 放行。
        - body: ``{conversation_id}`` 或 ``{platform, account_id, chat_key}``。
        """
        api_auth(request)
        try:
            role = str(request.session.get("role", "") or "")
        except Exception:
            role = ""
        if role in ("agent", "viewer"):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = str((body or {}).get("conversation_id") or "").strip()
        if not cid:
            platform = str((body or {}).get("platform") or "").lower()
            account_id = str((body or {}).get("account_id") or "default")
            chat_key = str((body or {}).get("chat_key") or "").strip()
            if not platform or not chat_key:
                raise HTTPException(400, tr(request, "err.ws.field_required",
                                            field="conversation_id"))
            cid = conv_id(platform, account_id, chat_key)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            actor = str(request.session.get("username", "") or "") or "api"
        except Exception:
            actor = "api"
        try:
            deleted = store.delete_conversation_data(cid, deleted_by=actor)
        except Exception:
            logger.error("[inbox] 会话删除失败 cid=%s", cid, exc_info=True)
            raise HTTPException(500, tr(request, "err.svc.inbox_not_ready"))
        logger.info("[inbox] 会话已删除 cid=%s by=%s rows=%s", cid, actor, deleted)
        # 运维台账（90 天可追溯）：谁删了哪个会话、清掉多少行。best-effort。
        try:
            from src.ops.ops_events import get_ops_event_store
            _oes = get_ops_event_store()
            if _oes is not None:
                _plat = cid.split(":", 1)[0]
                _oes.record(
                    platform=_plat, account_id=cid.split(":", 2)[1]
                    if cid.count(":") >= 2 else "", kind="conv_delete",
                    reason="ok", detail=f"cid={cid};by={actor};"
                    f"rows={sum(deleted.values())}")
        except Exception:
            logger.debug("[inbox] 会话删除审计落账失败（忽略）", exc_info=True)
        # 多窗口同步（2026-08-17）：删除广播 SSE——另一窗口的坐席不该继续对着
        # 已删除的会话打字。best-effort，失败不影响删除结果。
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("conversation_deleted", {
                "conversation_id": cid, "by": actor})
        except Exception:
            logger.debug("[inbox] conversation_deleted 事件发布失败", exc_info=True)
        return {"ok": True, "conversation_id": cid, "deleted": deleted,
                "total_rows": int(sum(deleted.values()))}

    @app.get("/api/unified-inbox/thread")
    async def api_unified_inbox_thread(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 50,
        before_ts: float = 0,
        history: int = 0,
    ):
        api_auth(request)
        platform = str(platform or "").lower()
        account_id = str(account_id or "default")
        chat_key = str(chat_key or "")
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        limit = max(1, min(500, int(limit or 50)))
        before = float(before_ts or 0) or None

        cid = conv_id(platform, account_id, chat_key)
        # 已登出：聊天页不开放线程（历史在 store，同号重登后同一 cid 可读）。
        # ``history=1``（历史账号只读视角 P0，2026-08-17）＝显式选择进只读查档
        # → 放行读取；写侧不受影响（发送路由对 dead 账号自有 409 兜底）。
        try:
            _st = (_account_status_map(request) or {}).get(
                (platform, account_id), "")
            if _st == "offline" and not history:
                raise HTTPException(
                    409, tr(request, "err.inbox.account_offline"))
        except HTTPException:
            raise
        except Exception:
            pass
        # 性能修复（根治「加载超时」）：**无条件先按 cid 直读持久层**——只要库里有该会话
        # 历史，就走这条快路(毫秒级)，**跳过**昂贵的全平台 live 聚合（_collect_all_chats
        # 遍历所有适配器 + telegram get_dialogs(100) + 写库，机器负载高时会拖到十几秒→前端
        # 超时）。不再依赖注册表 protocol 判定（该判定失败时旧逻辑会误落慢路径）。
        # 库为空（冷启/纯 live 账号首次）才回落下方完整 live 路径，保证不丢历史、行为兼容。
        target: Optional[Dict[str, Any]] = None
        out_msgs: List[Dict[str, Any]] = []
        _fast = _thread_messages_from_store(
            request, cid, limit=limit, before_ts=before,
        )
        if _fast:
            out_msgs = _fast
            target = _store_conv_as_chat(request, cid)

        if not out_msgs:
            chats = _collect_all_chats(request, limit=100)
            target = next(
                (
                    c for c in chats
                    if c.get("platform") == platform
                    and str(c.get("account_id") or "default") == account_id
                    and str(c.get("chat_key") or "") == chat_key
                ),
                None,
            )
            messages: List[Dict[str, Any]] = []
            if platform == "telegram" and before is None:
                client = _get_telegram_client(request)
                recent = getattr(client, "_recent_messages", None) if client is not None else []
                for idx, m in enumerate(list(recent or [])[-limit:]):
                    if str(m.get("chat_id") or "") != chat_key:
                        continue
                    messages.append(message_obj(
                        text=m.get("text") or "",
                        ts=m.get("ts") or 0,
                        direction="out" if m.get("is_self") else "in",
                        message_id=str(m.get("id") or m.get("message_id") or idx),
                        source=m,
                    ))
            if not messages and target:
                messages = candidate_messages_from_source(target.get("source") or {})
            if not messages and target:
                messages = list(target.get("messages") or [])
            _ingest_thread_best_effort(request, target, messages)
            out_msgs = messages[-limit:]
            store_preferred = (
                _read_from_store_enabled(request)
                or bool(target and target.get("from_store"))
                or _is_protocol_account(request, platform, account_id)
            )
            if store_preferred:
                stored_msgs = _thread_messages_from_store(
                    request, cid, limit=limit, before_ts=before,
                )
                if stored_msgs:
                    out_msgs = stored_msgs
                if target is None:
                    target = _store_conv_as_chat(request, cid)
            elif not out_msgs:
                stored_msgs = _thread_messages_from_store(request, cid, limit=limit)
                if stored_msgs:
                    out_msgs = stored_msgs
                if target is None:
                    target = _store_conv_as_chat(request, cid)

        translate_stats: Dict[str, Any] = {"enabled": False}
        try:
            from src.workspace.inbound_translate import enrich_inbound_translations
            # enrich 自带同步预算（最多 2 条 / 2.5s，其余转后台任务写库，前端轮询下一拍
            # 经 store overlay 取回）——常态毫秒级返回。外层 6s 是最后保险（单条引擎挂死
            # /store 异常），超时即返回原文，**绝不让加载卡死**。
            out_msgs, translate_stats = await asyncio.wait_for(
                enrich_inbound_translations(
                    request,
                    out_msgs,
                    conversation_id=cid,
                    config_manager=config_manager,
                    translation_svc=_get_translation_service(request),
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            logger.info("入站自动翻译超时(>6s)，本次返回原文（已译部分下次命中缓存）")
        except Exception:
            logger.debug("入站自动翻译失败（已忽略）", exc_info=True)

        _enrich_outbound_originals(request, cid, out_msgs)

        # F4：live 模式下 target 来自实时聚合（身份常为空），用 store 已持久身份「仅补空」富集，
        # 使 d.chat 携带最新昵称/头像 → 与前端 _mergePeerIdentity（F3）在 live 模式下也能合成。
        # store-backed 的 target（from_store）本就带身份，跳过避免多余回库。
        if target and not target.get("from_store"):
            _overlay_store_identity(request, [target])

        _record_panel_identity(target)   # F3：打开会话→记资料面板文字身份就绪度（去重）

        # B88：读取入口顺带探测「云端顶部 id vs 镜像最大 id」缺口并后台补拉
        # （重启窗漏的消息打开即自愈；per-cid 冷却在触发器内部，秒级轮询零放大）。
        # 快照随响应带出：state=error/capped 由前端在线程顶部显式提示（禁静默）。
        gap_probe: Dict[str, Any] = {}
        if platform == "telegram" and not history:
            try:
                from src.web.routes.unified_inbox_account_routes import (
                    maybe_probe_tg_thread_gap, tg_gap_probe_snapshot,
                )
                _gp_store = getattr(request.app.state, "inbox_store", None)
                if _gp_store is not None:
                    maybe_probe_tg_thread_gap(
                        request.app, _gp_store, account_id, chat_key)
                    gap_probe = tg_gap_probe_snapshot(cid)
            except Exception:
                logger.debug("[thread] 缺口探测触发失败（已忽略）", exc_info=True)
                gap_probe = {}

        resp = {
            "ok": True,
            "chat": target,
            "messages": out_msgs,
            "count": len(out_msgs),
            "has_more": len(out_msgs) >= limit,
            "oldest_ts": out_msgs[0].get("ts") if out_msgs else None,
            "auto_translate": translate_stats,
        }
        if gap_probe.get("state") not in (None, "", "idle"):
            resp["gap_probe"] = gap_probe
        return resp

    @app.get("/api/unified-inbox/conv-probe")
    async def api_unified_inbox_conv_probe(request: Request, cid: str = ""):
        """会话深链探针：按 conversation_id 直查持久库，答「这条引用指向哪、
        还能不能开」（2026-08-09，案例页「打开会话」落空事故的失败出路收口）。

        深链落空的四类真实原因（旧前端救援只覆盖第 2 类且一律误报「可能已归档」）：
        1. 引用的账号段/平台段拼错 → 按尾段 ``chat_key`` 反查全库，唯一同键会话
           即自动匹配（``exact=false`` + ``resolved_cid``，前端明示「已自动匹配」）；
        2. 会话沉在账号 200 条窗口外 → 本端点按 id 直查，无窗口限制；
        3. 会话所属账号已登出 → ``account_status=offline``（thread 端点对
           offline 会 409，前端如实提示而不是让坐席去归档视图白找）；
        4. 会话真的已归档 → ``archived=true``，前端照常打开并提示。
        找不到（演练残影/从未镜像）→ ``found=false``，前端给诚实文案。

        响应无本地化文案（纯数据，判词在前端 i18n）。
        """
        api_auth(request)
        cid = str(cid or "").strip()
        if not cid:
            raise HTTPException(
                400, tr(request, "err.ws.field_required", field="cid"))
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "found": False, "reason": "store_unavailable"}

        row = None
        exact = True
        try:
            row = store.get_conversation(cid)
        except Exception:
            row = None
        if not row:
            # 尾段 chat_key 反查（引用的平台/账号段可能是缺省猜测拼出来的）
            exact = False
            chat_key = cid.rsplit(":", 1)[-1].strip()
            want_plat = cid.split(":", 1)[0].strip().lower() if ":" in cid else ""
            cands: List[Dict[str, Any]] = []
            if chat_key and chat_key != cid:
                try:
                    cands = store.find_conversations_by_chat_key(chat_key, limit=10)
                except Exception:
                    cands = []
            if cands:
                same_plat = [c for c in cands
                             if str(c.get("platform") or "").lower() == want_plat]
                pick = (same_plat or cands)[0]   # 各自已按 last_ts DESC
                try:
                    row = store.get_conversation(
                        str(pick.get("conversation_id") or ""))
                except Exception:
                    row = None
        if not row:
            return {"ok": True, "found": False, "reason": "not_in_store"}

        resolved_cid = str(row.get("conversation_id") or cid)
        archived = False
        try:
            meta = store.get_conv_meta(resolved_cid) or {}
            archived = bool(meta.get("archived"))
        except Exception:
            archived = False
        st = ""
        try:
            st = (_account_status_map(request) or {}).get(
                (str(row.get("platform") or ""),
                 str(row.get("account_id") or "default")), "") or ""
        except Exception:
            st = ""

        chat = None
        if st != "offline":
            # offline 不给 chat：thread 端点对 offline 拒 409，注入了也打不开，
            # 前端按 account_status 出诚实文案。removed=只读历史，照常可开。
            try:
                mode = store.get_automation_mode(resolved_cid)
            except Exception:
                mode = "review"
            try:
                mc = store.count_messages(resolved_cid)
            except Exception:
                mc = 0
            chat = store_row_to_chat(
                row, automation_mode=mode, message_count=mc,
                read_only=(st == "removed"),
                account_status=st,
                can_send=(False if st else None),
            )
            try:
                _enrich_chat_list(request, [chat], config_manager=config_manager)
            except Exception:
                logger.debug("[conv-probe] enrich 失败（忽略）", exc_info=True)

        return {
            "ok": True,
            "found": True,
            "exact": exact,
            "resolved_cid": resolved_cid,
            "archived": archived,
            "account_status": st,
            "chat": chat,
        }
