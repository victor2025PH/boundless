"""Telegram 群成员提取域词条（后端 err.gm.* + 页面 gm_* 预留）。

后端路由响应经 ``tr(request, "err.gm.*")`` 收口零 CJK（过 route CJK ratchet 门禁）。
前端副驾组件 ``cp-tg-members`` 当前自带内联 zh/en（P0），挂载到宿主模板时再迁 data-i18n。
"""

ZH = {
    "err.gm.disabled": "群成员提取功能未开启（config.local.yaml → companion.group_members.enabled）",
    "err.gm.store_unavailable": "群成员库未就绪",
    "err.gm.readonly": "只读角色不可发起提取",
    "err.gm.bad_request": "缺少必要参数（account_id / group）",
    "err.gm.bad_filter": "过滤器无效（应为 all / spoke / spoke_no_admin）",
    "err.gm.client_unavailable": "该 Telegram 账号当前不在线或不可用",
    "err.gm.not_group": "该会话不是群/超级群，无法提取成员",
    "err.gm.job_not_found": "提取任务不存在",
    "err.gm.job_running": "该任务正在运行中",
    # ── ops-overview「🧲 群成员提取」卡 ──
    "ov2_s_gm": "群成员提取（多号限速拉「发言非管理员」入库 → 每日控量）",
    "ov2_gm_sub": "工具箱：多号进群、限速分批拉群成员入库、每天控量；只读提取，私聊触达是独立步骤。零成员且零任务时整卡隐藏",
    "ov2_gm_members": "已入库成员",
    "ov2_gm_groups": "覆盖群数",
    "ov2_gm_jobs": "提取任务（运行/总计）",
    # ── 收件箱工具箱入口卡（unified_inbox tools tab）──
    "inbox.tgm.title": "群成员提取",
    "inbox.tgm.desc": "多号进群 → 限速拉「发言且非管理员」的人入库 → 每日控量。只读提取；私聊触达是独立步骤。",
    "inbox.tgm.open": "🧲 打开管理台",
}

EN = {
    "err.gm.disabled": "Group member extraction is disabled (config.local.yaml -> companion.group_members.enabled)",
    "err.gm.store_unavailable": "Group member store is not ready",
    "err.gm.readonly": "Read-only role cannot start extraction",
    "err.gm.bad_request": "Missing required parameter (account_id / group)",
    "err.gm.bad_filter": "Invalid filter (expected all / spoke / spoke_no_admin)",
    "err.gm.client_unavailable": "This Telegram account is offline or unavailable",
    "err.gm.not_group": "This chat is not a group/supergroup; cannot extract members",
    "err.gm.job_not_found": "Extraction job not found",
    "err.gm.job_running": "This job is already running",
    # -- ops-overview "Group member extraction" card --
    "ov2_s_gm": "Group members (multi-account rate-limited pull of active non-admins -> daily cap)",
    "ov2_gm_sub": "Toolbox: multi-account join, rate-limited batched extraction into DB, daily cap; read-only extraction, outreach is a separate step. Hidden when zero members and zero jobs.",
    "ov2_gm_members": "Members in DB",
    "ov2_gm_groups": "Groups covered",
    "ov2_gm_jobs": "Extract jobs (running/total)",
    # -- inbox toolbox entry card --
    "inbox.tgm.title": "Group member extraction",
    "inbox.tgm.desc": "Multi-account join -> rate-limited pull of active non-admins -> daily cap. Read-only; outreach is a separate step.",
    "inbox.tgm.open": "🧲 Open console",
}
