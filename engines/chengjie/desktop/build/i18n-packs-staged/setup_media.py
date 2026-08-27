# -*- coding: utf-8 -*-
"""接入向导「多媒体能力」卡的自助修复词条（2026-07-31）。

背景：托管版此前对「后端未就绪」一律回一句「由服务端统一提供，如未生效请联系客服」
——而绝大多数情形只是**供给没发生**（设备令牌后到、或写 overlay 触发的热重载把内存
注入抹掉），重跑一次即可，根本不该开工单。这些键把那条死路换成用户点得动的按钮，
并按结构化原因码（``reason``，见 ``media_capability._backend_reason``）分型说话。

键族：
- setup.media.retry*      「重试接入」按钮与结果反馈
- setup.media.rsn_*        供给原因码人话（rsn_<reason>，见 media_capability）
- setup.media.mr_*         真流量「没看懂」的主因（mr_<reason>，见 media_enrich_stats）
- setup.media.h_enrich     真流量「没看懂」计数的健康行
"""

ZH = {
    "setup.media.retry": "重试接入",
    "setup.media.retrying": "正在接入…",
    "setup.media.retry_ok": "已接入，识图可用了",
    "setup.media.retry_fail": "仍未接入",
    "setup.media.rsn_no_token": "还没激活授权：先在设置里领取或填写授权，领完会自动接上",
    "setup.media.rsn_not_provisioned": "服务尚未接入——点「重试接入」即可，无需重启",
    "setup.media.rsn_user_backend": "检测到你自己配置的服务；未生效请确认该地址是否可达",
    "setup.media.rsn_self_hosted": "自建部署：请在配置里填写识图后端地址",
    "setup.media.rsn_no_local_module": "当前版本未包含本机识别模型，可接入云端识别服务",
    "setup.media.rsn_no_endpoint": "识别服务地址还没填",
    "setup.media.rsn_not_configured": "当前版本未包含该能力",
    "setup.media.mr_disabled": "开关没开",
    "setup.media.mr_no_backend": "后端没接上",
    "setup.media.mr_failed": "识别失败",
    "setup.media.mr_unresolved": "文件取不到",
    "setup.media.mr_unsupported": "类型不支持",
    "setup.media.mr_unknown": "原因不明",
    "setup.media.h_enrich": "近期媒体识别：{n} 条未看懂（{r}）",
}

EN = {
    "setup.media.retry": "Retry connect",
    "setup.media.retrying": "Connecting…",
    "setup.media.retry_ok": "Connected — image understanding is live",
    "setup.media.retry_fail": "Still not connected",
    "setup.media.rsn_no_token": "License not activated yet — claim or enter it in Settings and this connects automatically",
    "setup.media.rsn_not_provisioned": "Service not connected yet — click Retry connect, no restart needed",
    "setup.media.rsn_user_backend": "Using your own backend; if it isn't working, check that the address is reachable",
    "setup.media.rsn_self_hosted": "Self-hosted: configure the vision backend address in your config",
    "setup.media.rsn_no_local_module": "This build ships without an on-device model; connect a cloud recognition service instead",
    "setup.media.rsn_no_endpoint": "Recognition service address is empty",
    "setup.media.rsn_not_configured": "Not included in this build",
    "setup.media.mr_disabled": "switch is off",
    "setup.media.mr_no_backend": "backend not connected",
    "setup.media.mr_failed": "recognition failed",
    "setup.media.mr_unresolved": "file unavailable",
    "setup.media.mr_unsupported": "unsupported type",
    "setup.media.mr_unknown": "unknown",
    "setup.media.h_enrich": "Recent media: {n} not understood ({r})",
}
