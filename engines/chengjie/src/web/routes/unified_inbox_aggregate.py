"""统一收件箱——数据聚合 / 读路径 / 旁路 ingest（巨石拆分 slice 7）。

从 ``unified_inbox_routes.py`` 抽出的**列表/会话读路径与持久层旁路写入**族：
实时聚合（遍历 ChannelAdapter 注册表）、A1 灰度 store-backed 读视图、自动化模式
读写（持久优先回落进程内 dict）、best-effort ingest（冷启动不洪泛 SSE）。

依赖层级：仅依赖 services（_automation_store/_inbox_store）、helpers（AUTOMATION_MODES）
与 inbox 包内单一真源（channel_adapters/normalizer/ingest），不反向依赖 routes，故无
循环 import。``_INBOX_ADAPTERS`` 注册表实例（唯一使用者是本模块）一并下沉。
routes.py 等价重导出，对外引用路径保持不变。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Request

from src.inbox.channel_adapters import collect_chats_via_adapters, default_inbox_adapters
from src.inbox.ingest import ingest_collected_chats, ingest_thread
from src.inbox.normalizer import name_is_real, store_message_to_obj, store_row_to_chat
from src.web.routes.unified_inbox_helpers import AUTOMATION_MODES
from src.web.routes.unified_inbox_services import _automation_store, _inbox_store

logger = logging.getLogger(__name__)

# A2：渠道适配器注册表（模块级，无状态可复用）。新增渠道在 channel_adapters 注册即可。
_INBOX_ADAPTERS = default_inbox_adapters()


def _app_config_dict(request: Request) -> Dict[str, Any]:
    """读 config_manager.config（缺省 {}）——供 resolve_automation_mode 对齐全局档位。"""
    try:
        cm = getattr(request.app.state, "config_manager", None)
        cfg = getattr(cm, "config", None) if cm is not None else None
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _read_automation_mode(request: Request, conversation_id: str) -> str:
    """有效档位：显式设置 > 全局 auto_draft（与 A 线 / protocol 同源）。

    旧实现走 ``get_automation_mode``（无记录回落 review）→ UI 显示「草稿我审」
    而 A 线按全局 auto_ai 直发，坐席误以为已停自动。
    """
    store = _inbox_store(request)
    if store is not None:
        try:
            from src.inbox.automation_mode import resolve_automation_mode
            return resolve_automation_mode(
                store, conversation_id, _app_config_dict(request),
            )
        except Exception:
            logger.debug("resolve_automation_mode 失败，回落进程内 dict", exc_info=True)
    return _automation_store(request).get(conversation_id, "review")


def _write_automation_mode(request: Request, conversation_id: str, mode: str) -> int:
    """写入档位（source=human：UI 下拉是坐席的明示决定）；若降出 auto_ai，
    立即取消该会话待投递 L2 草稿。返回取消条数。"""
    cancelled = 0
    store = _inbox_store(request)
    if store is not None:
        try:
            prev = None
            try:
                prev = store.get_automation_mode_if_set(conversation_id)
            except Exception:
                prev = None
            try:
                store.set_automation_mode(conversation_id, mode, source="human")
            except TypeError:
                store.set_automation_mode(conversation_id, mode)
            from src.inbox.automation_mode import allows_direct_autosend
            if allows_direct_autosend(prev or "") and not allows_direct_autosend(mode):
                if hasattr(store, "cancel_pending_l2_drafts"):
                    cancelled = int(store.cancel_pending_l2_drafts(
                        conversation_id, decided_by="mode_downgraded") or 0)
            # Q-3（#264 D）：切到 manual / review / multi_choice → 连**在途**（已 resolve、正在
            # 拟人等待）的 L2 一起取消——cancel_pending 只够得着 pending，XBGPBN「切手动了还发」
            # 的那条正是等待中的 approved 稿。prev 未显式设置（全局 auto_ai）也要取消。
            if not allows_direct_autosend(mode):
                cancelled += _cancel_inflight_q3(request, conversation_id=conversation_id)
            return cancelled
        except Exception:
            logger.debug("inbox_store.set_automation_mode 失败，回落进程内 dict", exc_info=True)
    _automation_store(request)[conversation_id] = mode
    return cancelled


def _cancel_inflight_q3(request: Request, *, conversation_id: str = "",
                        platform: str = "", account_id: str = "",
                        by: str = "mode_switch") -> int:
    """Q-3（#264 D）：经 ``app.state.autosend_worker.cancel_inflight`` 取消范围内排队 + 在途
    L2 稿，返回条数（前端 toast「已取消 N 条待发 AI 消息」）。worker 缺席 / 旧版无方法 → 0。绝不抛。"""
    try:
        worker = getattr(request.app.state, "autosend_worker", None)
        if worker is None or not hasattr(worker, "cancel_inflight"):
            return 0
        return int(worker.cancel_inflight(
            conversation_id=str(conversation_id or ""), platform=str(platform or ""),
            account_id=str(account_id or ""), by=str(by or "mode_switch")) or 0)
    except Exception:
        logger.debug("[inflight] cancel 调用失败（忽略）", exc_info=True)
        return 0


def _agent_yield_state_q18(request: Request, conversation_id: str) -> Optional[Dict[str, Any]]:
    """Q-18 C（#292）：会话「AI 让位」状态（``worker.agent_yield_state``）——GET automation /
    reply_diagnosis / 会话头 ``ay-`` chip 同源。worker 缺席 / 旧版无方法 → None（前端不渲染）。绝不抛。"""
    try:
        worker = getattr(request.app.state, "autosend_worker", None)
        if worker is None or not hasattr(worker, "agent_yield_state"):
            return None
        st = dict(worker.agent_yield_state(str(conversation_id or "")) or {})
        try:
            from src.inbox.autosend_policy import AGENT_YIELD_WINDOW_SEC as _w
            st["window_sec"] = float(_w)
        except Exception:
            st["window_sec"] = 60.0
        return st
    except Exception:
        logger.debug("[autosend] agent_yield_state 读取失败（忽略）", exc_info=True)
        return None


def _resume_agent_yield_q18(request: Request, conversation_id: str, *,
                            by: str = "mode_select") -> Dict[str, Any]:
    """Q-18 C（#292）：切到 / 重选「全自动」或点会话头 chip = 明示接回——经
    ``worker.resume_agent_yield`` 清该会话 ``_agent_sent_ts / _agent_typing_ts`` + 取消 defer 立即放行
    （worker 落 ``[autosend] resume by=<by>``）。返回 ``{had_yield, released, by}``；worker 缺席 →
    ``had_yield=False``。绝不抛。"""
    out: Dict[str, Any] = {"had_yield": False, "released": 0, "by": str(by or "mode_select")}
    try:
        worker = getattr(request.app.state, "autosend_worker", None)
        if worker is None or not hasattr(worker, "resume_agent_yield"):
            return out
        r = worker.resume_agent_yield(str(conversation_id or ""), by=str(by or "mode_select"))
        if isinstance(r, dict):
            out.update(r)
        return out
    except Exception:
        logger.debug("[autosend] resume_agent_yield 调用失败（忽略）", exc_info=True)
        return out


def _ingest_best_effort(request: Request, chats: List[Dict[str, Any]]) -> None:
    """旁路写入持久层，并在首轮冷启动后开启实时 SSE 事件发布。

    首次调用时 publish_events=False（冷启动不洪泛），之后切换为 True；
    仅有真正新消息（store.ingest_batch n>0）时才发 inbox_message 事件。
    """
    store = _inbox_store(request)
    if store is None or not chats:
        return
    try:
        # 首轮冷启动：向 store 写入存量数据但不发事件（避免把历史消息全部推送）
        first_done = getattr(request.app.state, "_inbox_first_ingest_done", False)
        ingest_collected_chats(store, chats, publish_events=first_done)
        if not first_done:
            request.app.state._inbox_first_ingest_done = True
    except Exception:
        logger.debug("统一收件箱旁路写入失败（已忽略）", exc_info=True)


def _ingest_thread_best_effort(request: Request, chat: Optional[Dict[str, Any]],
                               messages: List[Dict[str, Any]]) -> None:
    store = _inbox_store(request)
    if store is None or not chat or not messages:
        return
    # Store-backed chat rows already came from InboxStore. Their `messages` field is
    # only a preview rebuilt from `last_text` and lacks platform_msg_id/direction
    # fidelity; writing it back creates fake `:h:` inbound duplicates.
    if chat.get("from_store"):
        return
    try:
        ingest_thread(store, chat, messages)
    except Exception:
        logger.debug("统一收件箱会话历史写入失败（已忽略）", exc_info=True)


def _collect_all_chats(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
    """从所有平台/账号收集最近对话，返回统一格式列表。

    A2：改为遍历 ChannelAdapter 注册表（src/inbox/channel_adapters.py）。
    新增渠道 = 新增一个适配器并注册，无需改本函数。各平台的取数/字段映射
    封装在各自适配器内，行为与抽取前一致。
    """
    out: List[Dict[str, Any]] = collect_chats_via_adapters(
        request, limit, _INBOX_ADAPTERS,
    )

    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    out = out[:limit * 4]
    # 旁路写入持久层（best-effort，不改读路径行为）
    _ingest_best_effort(request, out)
    for row in out:
        cid = str(row.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        row["automation_mode"] = mode if mode in AUTOMATION_MODES else "review"
    return out


def _is_protocol_account(request: Request, platform: str, account_id: str) -> bool:
    """该账号是否为 store-backed 模式（消息 push 落库、线程/列表按 store 读出）。

    含两类：``protocol``（编排器接管的真 worker）与 ``desktop``（桌面壳同步桥，无 worker）。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(platform, account_id)
        return bool(row and row.get("mode") in ("protocol", "desktop"))
    except Exception:
        return False


def _account_status_map(request: Request) -> Dict[tuple, str]:
    """注册表非活跃账号 → ``{(platform, account_id): "removed" | "offline"}``（打标/过滤用）。

    - ``removed``（软删）：历史只读展示、默认藏进「已移除」tab；``inbox.show_removed_history``
      关闭则不标（与 ProtocolInboxAdapter 隐藏行为对齐，发送侧另有 409 兜底）。
      ⚠ 修复（2026-07-31）：旧实现 ``_removed_account_keys`` 调 ``list()``——该方法默认
      ``include_removed=False`` 在 SQL 层就排除了 removed 行，再筛 ``status=='removed'``
      **恒得空集**，「已移除只读标记」自上线从未生效过。必须 ``include_removed=True``。
    - ``offline``（已登出）：**聊天页不展示**（会话/chip/未读全隐），消息仍留本机 store；
      同号 ``status→online``（重登）后列表自然回显，无需迁库。账号管理 ``/api/accounts``
      仍列出以便重登。
    查不到一律空 map（不拦不标，回落旧行为）。
    """
    out: Dict[tuple, str] = {}
    try:
        show_removed = True
        try:
            cm = getattr(request.app.state, "config_manager", None)
            cfg = (getattr(cm, "config", None) or {}) if cm is not None else {}
            show_removed = bool(((cfg.get("inbox") or {}) if isinstance(cfg, dict) else {})
                                .get("show_removed_history", True))
        except Exception:
            show_removed = True
        from src.integrations.account_registry import get_account_registry
        rows = get_account_registry().list(include_removed=True) or []
        for a in rows:
            st = str(a.get("status") or "")
            key = (str(a.get("platform") or ""), str(a.get("account_id") or ""))
            if st == "removed" and show_removed:
                out[key] = "removed"
            elif st == "offline":
                out[key] = "offline"
    except Exception:
        return {}
    return out


# 内置工作台号：会话库几乎必有、从不走账号注册表，不算「绕过注册表写会话」的幽灵。
_SYNTHETIC_PLATFORMS = frozenset({"web"})


def is_synthetic_account(platform: str, account_id: str = "") -> bool:
    """内置/占位账号（web 工作台等）——目录有、注册表无，不是接入泄漏。"""
    return str(platform or "").strip().lower() in _SYNTHETIC_PLATFORMS


def directory_ghost_keys(
    directory: Dict[tuple, Any],
    registry_keys: Any,
) -> List[tuple]:
    """真幽灵键：会话库有、注册表没有、且不是内置工作台号。

    P4 之后 ``tg-desktop`` 这类 ``mode=desktop`` 号已在注册表，不会进这里；
    这里剩下的才是「有账号绕过注册表在收发」的泄漏信号。
    """
    keys = set(registry_keys or [])
    out: List[tuple] = []
    for k in (directory or {}):
        pl, aid = str((k[0] if k else "") or ""), str((k[1] if k and len(k) > 1 else "") or "")
        if (pl, aid) in keys or is_synthetic_account(pl, aid):
            continue
        out.append((pl, aid))
    return out


def _registry_active_map(request: Request) -> Dict[tuple, Dict[str, str]]:
    """注册表**活跃但可能无运行通道**的账号 → ``{(platform, account_id): {mode, label}}``。

    P4（2026-08-17 幽灵账号收编）：``_account_status_map`` 只收 offline/removed 两态，
    而 ``mode="desktop"`` 的桌面壳镜像账号（如 tg-desktop，经 ``/api/desktop/ingest``
    首见即登记、status=online）**没有 worker、不进 platform_status**——三层桶都接不住，
    在 accounts_summary 里坠成 ``history_only`` 幽灵；同时 ops「账号真相」卡读注册表
    全量说 ghost=0，两个面互相打架。本 map 把「在册活跃」全量交给 summary 分类器，
    由它按「是否已在 platform_status」决定要不要补桶（desktop / registered）。
    查不到一律空 map（回落旧行为：这类号继续按 history_only 展示，不比修前更糟）。
    """
    out: Dict[tuple, Dict[str, Any]] = {}
    try:
        from src.integrations.account_registry import get_account_registry
        from src.web.desktop_bridge_presence import bridge_presence
        for a in (get_account_registry().list() or []):   # 默认不含 removed
            if str(a.get("status") or "") == "offline":
                continue   # 已登出走 _account_status_map 的 logged_out 桶
            key = (str(a.get("platform") or ""), str(a.get("account_id") or ""))
            info: Dict[str, Any] = {"mode": str(a.get("mode") or ""),
                                    "label": str(a.get("label") or "")}
            # 桥接驱动（PC 副驾）心跳 → 工作台能看出驱动进程是否还活着（实施97 线 B）
            pres = bridge_presence(a.get("meta") if isinstance(a.get("meta"), dict) else None)
            if pres:
                info["bridge"] = pres
            out[key] = info
    except Exception:
        return {}
    return out


def _read_from_store_enabled(request: Request) -> bool:
    """A1 读路径开关：``config.inbox.read_from_store``。

    **默认 True**（2026-07-31 改，原为 False）：灰度早已完成——``config.example.yaml``、
    桌面种子 ``config.desktop.min.yaml`` 与生产实例配置都显式 ``true``，于是 ``False``
    这条默认分支实际**只有陈旧升级配置**（该键出现之前装的）才会走到，造成「全新安装
    读 store / 升级安装读实时聚合」的静默分叉——后者身份贫乏，要靠 peer_identity 回读
    补齐裸号/空头像。把默认与既有部署对齐即消除分叉；显式 ``false`` 仍回实时聚合。
    （门禁：tests/test_seed_switch_upgrade_coverage.py 的 _EXEMPT 登记本处判据。）

    拿不到 config（测试/退化态）仍返回 False：那不是部署形态，保守回落实时聚合，
    不依赖 store 是否已有数据。
    """
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if not isinstance(cfg, dict):
        return False
    return bool((cfg.get("inbox") or {}).get("read_from_store", True))


def _collect_chats_from_store(
    request: Request,
    limit: int = 30,
    label_map: Optional[Dict[tuple, str]] = None,
    before_ts: Optional[float] = None,
    platform: str = "",
    account_id: str = "",
    include_hidden: bool = False,
) -> List[Dict[str, Any]]:
    """A1 读路径：直接从 InboxStore（持久事实源）读会话列表，映射回 chat dict 形状。

    ``label_map``：实时聚合派生的 {(platform, account_id): account_label} 友好名映射
    （store 不持久 account_label，借 live 同源回填，消除「列表显示账号 id」可视回归；
    store-only 历史账号 live 无对应项则回落 account_id——live 本也无其 label）。
    ``before_ts``：十期游标分页——只取 last_ts 更旧的会话（「加载更多」）。
    ``platform`` / ``account_id``：scoped 过滤直透 store（非空才生效、可组合）——
    前端全局列表只拿最近 top100，点开单账号看老会话必须按需查库，此处是唯一入口。
    ``include_hidden``（历史账号只读视角 P0，2026-08-17）：已退出(offline)账号的
    会话默认整批跳过（聊天页语义）；抽屉「查看会话」的账号历史视角需要看到它们
    → True 时不再跳过，行带 ``read_only=True``（composer 端上锁死；发送路由对
    dead 账号本就有 409 兜底）。**只应在 account_id scoped 请求下开启**，全局
    列表口径不变。返回 None 表示 store 不可用（调用方回落实时聚合）。
    """
    store = _inbox_store(request)
    if store is None:
        return None  # type: ignore[return-value]
    lmap = label_map or {}
    # removed=只读历史（进「已移除」tab）；offline=已登出 → 聊天页跳过（库内保留）。
    acct_status = _account_status_map(request)
    convs = store.list_conversations(
        limit=min(200, max(1, limit * 4)), before_ts=before_ts,
        platform=platform, account_id=account_id)
    # 置顶恒在首屏（2026-08-17 官方级消息管理）：top-N 时间窗截断对置顶会话不适用
    # ——老客户被新流量挤出窗口后「置顶」就名存实亡。仅首页（无 before_ts 游标）
    # 合并缺席的置顶行；分页页语义不变（置顶已在首页展示过）。best-effort。
    if before_ts is None:
        try:
            pinned_rows = store.list_pinned_conversations(
                platform=platform, account_id=account_id) or []
        except Exception:
            pinned_rows = []
        if pinned_rows:
            _seen = {str(c.get("conversation_id") or "") for c in convs}
            convs = [r for r in pinned_rows
                     if str(r.get("conversation_id") or "") not in _seen] + convs
    out: List[Dict[str, Any]] = []
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        try:
            mc = store.count_messages(cid)
        except Exception:
            mc = 0
        key = (str(c.get("platform") or ""), str(c.get("account_id") or "default"))
        st = acct_status.get(key, "")
        if st == "offline" and not include_hidden:
            continue  # 历史在 store，重登后同路径自动回显
        out.append(store_row_to_chat(
            c, automation_mode=mode, message_count=mc,
            account_label=lmap.get(key),
            # read_only 专指「软删只读历史」语义（前端据此归入已移除 tab）；
            # include_hidden（账号历史视角）下 offline 行同样置只读——已退出
            # 账号发不出消息，端上必须一致锁死 composer。
            read_only=(st == "removed" or (include_hidden and st == "offline")),
            account_status=st,
            can_send=(False if st else None),
        ))
    return out


def _apply_store_identity(chat: Dict[str, Any], row: Dict[str, Any]) -> List[str]:
    """把 store 行的身份列「仅补空 / 仅升级」到 live chat dict（原地修改）。

    返回**本次实际补齐的字段名列表**（``name``/``username``/``phone``/``avatar``，空列表=无变化，
    供 F5 回读观测按字段计数）：
    - ``name``：仅当 live 名不是真名（空/裸数字/等于 chat_key）且 store ``display_name`` 是真名时替换；
    - ``username``/``phone``/``avatar_url``：仅当 live 该字段缺失时补。绝不覆盖 live 已有的真名/值。
    """
    upgraded: List[str] = []
    ck = chat.get("chat_key")
    disp = str(row.get("display_name") or "").strip()
    if disp and name_is_real(disp, ck) and not name_is_real(chat.get("name"), ck):
        chat["name"] = disp
        upgraded.append("name")
    for fld in ("username", "phone", "avatar_url"):
        val = str(row.get(fld) or "").strip()
        if val and not str(chat.get(fld) or "").strip():
            chat[fld] = val
            upgraded.append("avatar" if fld == "avatar_url" else fld)
    return upgraded


# F5：回读补齐观测去重集——同一 conversation_id 每进程只记一次首补（避免轮询/开会话重复计数
# 把 rows 撑爆），bounded 防内存无界（超上限后新会话不再记，是采样 gauge 而非精确总量）。
_READBACK_SEEN: set = set()
_READBACK_SEEN_MAX = 5000


def _record_readback(platform: str, cid: str, fields: List[str]) -> None:
    """记一次「live 行被 store 身份回读补齐」（按 conversation_id 去重 + bounded + best-effort）。"""
    try:
        if not cid or not fields or cid in _READBACK_SEEN:
            return
        if len(_READBACK_SEEN) >= _READBACK_SEEN_MAX:
            return  # 超上限：新会话不再记（内存保护），已记的仍去重
        _READBACK_SEEN.add(cid)
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record_readback(platform, fields)
    except Exception:
        logger.debug("[identity] readback 观测记录失败（已忽略）", exc_info=True)


def _overlay_store_identity(
    request: Request, chats: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """F4：给实时聚合的 live 行用 store 已持久的身份列做「仅补空 / 仅升级」富集（原地）。

    背景：``read_from_store=false`` 时列表走实时聚合，多数 RPA 适配器只给 name（甚至裸 id），
    而 side-effect ingest 早已把 username/phone/avatar_url + 更优 display_name 落进 store
    ——"库里有、live 没喂出来"。本函数把它们补回 live 行，闭合审计发现的最后一个数据流缺口。
    身份已齐（有真名 + username + 头像）的行不查库（零 IO）；store 不可用/无匹配 → 原样返回；绝不抛。
    ``read_from_store=true`` 路径不经此（本就读 store）。
    """
    if not chats:
        return chats
    store = _inbox_store(request)
    if store is None:
        return chats
    for c in chats:
        try:
            if (name_is_real(c.get("name"), c.get("chat_key"))
                    and str(c.get("username") or "").strip()
                    and str(c.get("avatar_url") or "").strip()):
                continue  # 文字身份 + 头像都已具备 → 无需回库
            cid = str(c.get("conversation_id") or "")
            if not cid:
                continue
            row = store.get_conversation(cid)
            if row:
                upgraded = _apply_store_identity(c, row)
                if upgraded:
                    _record_readback(str(c.get("platform") or ""), cid, upgraded)
        except Exception:
            logger.debug("[identity] live 行 store 身份富集失败（已忽略）", exc_info=True)
    return chats


def _exclude_logged_out_chats(
    request: Request, chats: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """聊天页过滤：已登出(offline)账号的会话不进列表（全平台；历史留库）。"""
    if not chats:
        return chats
    try:
        sm = _account_status_map(request) or {}
    except Exception:
        return chats
    if not sm:
        return chats
    out: List[Dict[str, Any]] = []
    for c in chats:
        key = (str(c.get("platform") or ""),
               str(c.get("account_id") or "default"))
        if sm.get(key) == "offline":
            continue
        out.append(c)
    return out


def _exclude_hidden_web_chats(
    request: Request, chats: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """impl85 阶段4：入口隐藏时剔除 platform=web 会话行（含存量残留会话）。

    适配器侧 gate 只管 live 聚合；store-backed 视图会把库里的旧「在线顾问」会话
    原样列出（#42 截图正是这种残留），必须在双路径共同出口再滤一次。历史留库，
    只是不进列表（与登出账号过滤同哲学）。
    """
    if not chats:
        return chats
    try:
        from src.integrations.web_chat.service import web_entry_visible
        cm = getattr(request.app.state, "config_manager", None)
        cfg = (getattr(cm, "config", None) or {}) if cm is not None else {}
        if web_entry_visible(cfg):
            return chats
    except Exception:
        return chats
    return [c for c in chats if str(c.get("platform") or "") != "web"]


def _chats_for_listing(request: Request, limit: int = 30) -> List[Dict[str, Any]]:
    """收件箱列表数据源（A1 灰度）：

    - 始终先跑实时聚合 `_collect_all_chats`（同时旁路 ingest 进 store，保持 store 新鲜）；
    - flag 开 + store 可用：列表改用 store-backed 视图（跨平台/跨重启持久），
      实时聚合的副作用（ingest）已经发生；
    - 否则：返回实时聚合结果，并用 store 已持久身份「仅补空」富集（F4，闭合 live 模式身份缺口）。
    - 末尾统一剔除已登出账号会话 + 隐藏的 web 入口会话（live / store 双路径同口径）。
    """
    live = _collect_all_chats(request, limit=limit)
    if _read_from_store_enabled(request):
        # 借实时聚合结果派生 account_label 友好名映射，store 读路径回填以与 live 等价
        label_map = {
            (str(r.get("platform") or ""), str(r.get("account_id") or "default")):
                str(r.get("account_label") or "")
            for r in live if r.get("account_label")
        }
        stored = _collect_chats_from_store(request, limit=limit, label_map=label_map)
        if stored is not None:
            return _exclude_logged_out_chats(
                request, _exclude_hidden_web_chats(request, stored))
    return _exclude_logged_out_chats(
        request, _exclude_hidden_web_chats(
            request, _overlay_store_identity(request, live)))


def _thread_messages_from_store(
    request: Request, conversation_id: str, limit: int = 50,
    before_ts: Optional[float] = None,
) -> Optional[List[Dict[str, Any]]]:
    """A1 读路径收尾：从 InboxStore 读会话历史（持久事实源），映射回 thread 消息形状。

    返回 None=store 不可用；返回 []=store 中该会话无消息（调用方据此决定是否回落实时）。
    """
    store = _inbox_store(request)
    if store is None:
        return None
    try:
        # include_deleted=False（2026-08-17）：UI 线程读路径过滤「仅工作台删除」的
        # 软删行；业务口径消费方（回复时延/replied-after 护栏）走默认参不受影响。
        # 旧 store（未升级）无此形参 → TypeError 回落旧签名，行为兼容。
        try:
            rows = store.list_recent_messages(
                conversation_id, limit=limit, before_ts=before_ts,
                include_deleted=False,
            )
        except TypeError:
            rows = store.list_recent_messages(
                conversation_id, limit=limit, before_ts=before_ts,
            )
    except Exception:
        logger.debug("store thread 读取失败（已忽略）", exc_info=True)
        return None
    return [store_message_to_obj(r) for r in rows]


def _store_conv_as_chat(request: Request, conversation_id: str) -> Optional[Dict[str, Any]]:
    """从 store 取持久会话行并映射为 chat dict（thread 在实时源已无该会话时兜底 header）。"""
    store = _inbox_store(request)
    if store is None:
        return None
    try:
        row = store.get_conversation(conversation_id)
    except Exception:
        return None
    if not row:
        return None
    mode = _read_automation_mode(request, conversation_id)
    try:
        mc = store.count_messages(conversation_id)
    except Exception:
        mc = 0
    # 已登出：聊天页不开放会话头/历史（同号重登后走同一路径再可见）。
    # 已移除：仍可只读打开（「已移除」tab）。
    st = ""
    try:
        st = (_account_status_map(request) or {}).get(
            (str(row.get("platform") or ""),
             str(row.get("account_id") or "default")), "") or ""
    except Exception:
        st = ""
    if st == "offline":
        return None
    return store_row_to_chat(
        row, automation_mode=mode, message_count=mc,
        read_only=(st == "removed"),
        account_status=st,
        can_send=(False if st else None),
    )


def _enrich_outbound_originals(
    request: Request, conversation_id: str, msgs: List[Dict[str, Any]]
) -> None:
    """P1：为出向消息富集坐席输入的中文原文（读 outbound_translations 旁路表）。

    一击直发后实发为译文（消息正文），此处把对应的中文原文 + 翻译质量挂到消息上
    （字段 ``agent_original`` / ``agent_xlate``），供前端持久渲染出向双行。
    best-effort、原地修改，失败/无数据不影响 thread 返回。
    """
    ibx = _inbox_store(request)
    if ibx is None or not conversation_id or not msgs:
        return
    try:
        xmap = ibx.get_outbound_translations(conversation_id)
    except Exception:
        logger.debug("读取 outbound_translations 失败（忽略）", exc_info=True)
        return
    if not xmap:
        return
    import hashlib as _hl
    for m in msgs:
        if not isinstance(m, dict) or str(m.get("direction") or "") != "out":
            continue
        sent = str(m.get("text") or "").strip()
        if not sent:
            continue
        row = xmap.get(_hl.sha256(sent.encode("utf-8")).hexdigest()[:16])
        if not row:
            continue
        orig = str(row.get("original_text") or "").strip()
        if orig and orig != sent:
            m["agent_original"] = orig
            m["agent_xlate"] = {
                "target_lang": row.get("target_lang") or "",
                "source_lang": row.get("source_lang") or "",
                "provider": row.get("provider") or "",
                "error": row.get("error") or "",
            }


#: 出站行与坐席发送打点的时间就近匹配窗口（秒）。打点在发送路由成功那一刻，出站行 ts 是
#: 平台回执/镜像时刻，通常差零到几秒；边车镜像慢时可到十几秒。
AGENT_SENT_MATCH_SEC = 30.0


def _mark_agent_sent(
    request: Request, conversation_id: str, msgs: List[Dict[str, Any]],
    *, window_sec: float = AGENT_SENT_MATCH_SEC,
) -> int:
    """接力记忆二期（2026-09-12）：给**坐席人工发出**的出站行挂 ``sent_by="agent"``
    （+ ``agent_name``），前端在气泡时间戳旁标「人工」——坐席一眼分清「这条是我发的还是
    AI 发的」，切回全自动时也看得出 AI 将接过哪几条。

    三期起 ``messages.sent_by`` 列是事实源（发送路由打点 + 镜像落库时按 text_hash/media_ref
    精确认领，见 store._claim_agent_send_locked）——已标的行原样透传、已认领的打点不再参与。
    老行 / 认领失败的行回落到本函数的时间就近匹配（每个未认领打点至多标一行、先到先配、
    窗口 ``window_sec``）。纯装饰、best-effort、原地修改；打点缺失（RPA 手机端发出 / 老数据）
    就不标。返回本次新标记条数（不含列上已有的）。
    """
    ibx = _inbox_store(request)
    if ibx is None or not conversation_id or not msgs:
        return 0
    all_outs = [m for m in msgs if isinstance(m, dict) and str(m.get("direction") or "") == "out"
                and float(m.get("ts") or 0) > 0]
    if not all_outs:
        return 0
    try:
        lo = min(float(m.get("ts") or 0) for m in all_outs) - window_sec
        hi = max(float(m.get("ts") or 0) for m in all_outs) + window_sec
        all_sends = ibx.list_agent_sends(conversation_id, since_ts=lo, until_ts=hi)
    except Exception:
        logger.debug("读取 agent_sends 失败（忽略）", exc_info=True)
        return 0
    # 列上已标的行：把认领它的打点的坐席名带上（悬停「XX 手动发出」）
    by_mid = {str(s.get("claimed_mid") or ""): s for s in all_sends if s.get("claimed_mid")}
    for m in all_outs:
        if str(m.get("sent_by") or "") == "agent" and not m.get("agent_name"):
            s = by_mid.get(str(m.get("message_id") or ""))
            if s and s.get("agent_name"):
                m["agent_name"] = str(s["agent_name"])[:40]
    sends = [s for s in all_sends if not str(s.get("claimed_mid") or "")]
    # 回落池＝除「列上精确认领」以外的全部出站行（同一列表重复调用结果一致＝幂等）
    outs = [m for m in all_outs if str(m.get("message_id") or "") not in by_mid
            and str(m.get("sent_by") or "") != "ai"]          # 四期：AI 自动链行永不回落成「人工」
    if not sends or not outs:
        return 0
    outs.sort(key=lambda m: float(m.get("ts") or 0))
    used = set()
    marked = 0
    for s in sends:                       # 时间升序；每个未认领打点配最近的一条未配出站行
        st = float(s.get("ts") or 0)
        best, best_d = None, None
        for i, m in enumerate(outs):
            if i in used:
                continue
            d = abs(float(m.get("ts") or 0) - st)
            if d <= window_sec and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is None:
            continue
        used.add(best)
        outs[best]["sent_by"] = "agent"
        if s.get("agent_name"):
            outs[best]["agent_name"] = str(s["agent_name"])[:40]
        marked += 1
    return marked
