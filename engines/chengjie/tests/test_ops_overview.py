"""E1/E3 运营总览纯函数测试：灯聚合 / 计费异常 / 总览装配。"""

from src.utils.ops_overview import (
    assemble_ops_overview,
    billing_anomalies,
    companion_config_light,
    orchestrator_worker_light,
    orchestrator_worker_problems,
    worst_light,
)


def test_worst_light_picks_most_severe():
    assert worst_light("green", "yellow") == "yellow"
    assert worst_light("green", "red", "yellow") == "red"
    assert worst_light("green", "green") == "green"


def test_worst_light_ignores_empty_and_unknown():
    assert worst_light("", "green") == "green"
    assert worst_light("", "") == ""
    assert worst_light("bogus", "yellow") == "yellow"


def test_billing_anomalies_over_seats_is_fail():
    billing = {
        "available": True,
        "reconcile": {"seats": 5, "active_agents": 8, "over_seats": 3},
        "charges": {},
    }
    issues = billing_anomalies(billing)
    codes = {i["code"]: i for i in issues}
    assert "over_seats" in codes
    assert codes["over_seats"]["severity"] == "fail"
    assert codes["over_seats"]["detail"]["over_seats"] == 3


def test_billing_anomalies_message_overage_is_warn():
    billing = {
        "available": True,
        "reconcile": {"over_seats": 0},
        "charges": {
            "message_overage_qty": 120,
            "message_overage_amount": 12.0,
            "currency": "USD",
        },
    }
    issues = billing_anomalies(billing)
    assert len(issues) == 1
    assert issues[0]["code"] == "message_overage"
    assert issues[0]["severity"] == "warn"


def test_billing_anomalies_empty_when_unavailable():
    assert billing_anomalies({"available": False, "reconcile": {"over_seats": 9}}) == []
    assert billing_anomalies(None) == []


def test_assemble_overall_light_is_worst_of_health_and_reliability():
    ov = assemble_ops_overview(
        health={"light": "green"},
        reliability={"light": "yellow", "score": 80},
    )
    assert ov["ok"] is True
    assert ov["overall_light"] == "yellow"
    assert ov["kpis"]["reliability_score"] == 80


def test_assemble_over_seats_escalates_overall_to_red():
    ov = assemble_ops_overview(
        health={"light": "green"},
        reliability={"light": "green"},
        billing={
            "available": True,
            "plan": "pro",
            "reconcile": {"over_seats": 2},
            "charges": {"total": 99.0, "currency": "USD"},
        },
    )
    assert ov["overall_light"] == "red"
    assert ov["kpis"]["over_seats"] == 2
    assert ov["kpis"]["billing_anomaly_count"] == 1
    assert ov["kpis"]["billing_total"] == 99.0


def test_assemble_surfaces_roi_kpis_and_sections():
    roi = {
        "business": {"leads": 10, "conversions": 3, "conversion_rate": 0.3},
        "automation": {"ai_share_pct": 65, "saved_hours": 4.0, "saved_money": 80.0},
    }
    ov = assemble_ops_overview(
        roi=roi,
        health={"light": "green"},
        reliability={"light": "green", "alert_count": 2},
        open_incidents=1,
    )
    k = ov["kpis"]
    assert k["leads"] == 10
    assert k["conversions"] == 3
    assert k["ai_share_pct"] == 65
    assert k["saved_money"] == 80.0
    assert k["open_alerts"] == 2
    assert k["open_incidents"] == 1
    assert ov["sections"]["roi"] is roi


def test_assemble_handles_all_empty():
    ov = assemble_ops_overview()
    assert ov["ok"] is True
    assert ov["overall_light"] == ""
    assert ov["billing_anomalies"] == []


# ── 陪伴能力配置健康接入总览 ───────────────────────────────────────────────

def test_companion_config_light_levels():
    assert companion_config_light(None) == ""
    assert companion_config_light({"summary": {"errors": 0, "warnings": 0}}) == "green"
    assert companion_config_light({"summary": {"errors": 0, "warnings": 2}}) == "yellow"
    assert companion_config_light({"summary": {"errors": 1, "warnings": 0}}) == "red"


def test_assemble_companion_errors_escalate_overall_to_red():
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        companion={"summary": {"errors": 1, "warnings": 0},
                   "consistency": [{"severity": "error", "message": "真发裸奔"}]},
    )
    assert ov["overall_light"] == "red"
    assert ov["kpis"]["companion_config_light"] == "red"
    assert ov["kpis"]["companion_config_errors"] == 1
    assert ov["sections"]["companion"]["summary"]["errors"] == 1


def test_assemble_companion_warnings_escalate_to_yellow_only():
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        companion={"summary": {"errors": 0, "warnings": 3}},
    )
    assert ov["overall_light"] == "yellow"
    assert ov["kpis"]["companion_config_warnings"] == 3


def test_assemble_companion_none_does_not_affect_overall():
    ov = assemble_ops_overview(health={"light": "green"}, reliability={"light": "green"})
    assert ov["overall_light"] == "green"
    assert ov["kpis"]["companion_config_light"] == ""


# ── 编排器 worker 健康接入总览（出站路由真信号；P2-b） ─────────────────────

def test_orchestrator_worker_light_levels():
    # 无 status / 无受管账号 → 不参与
    assert orchestrator_worker_light(None) == ""
    assert orchestrator_worker_light({}) == ""
    assert orchestrator_worker_light({"total": 0, "by_state": {}}) == ""
    # 全 running → 绿
    assert orchestrator_worker_light(
        {"total": 2, "by_state": {"running": 2}}) == "green"
    # 有 error 态 worker → 黄（降级，非全崩）
    assert orchestrator_worker_light(
        {"total": 2, "by_state": {"running": 1, "error": 1}}) == "yellow"


def test_orchestrator_worker_light_ignores_raw_fallback_rate():
    """关键不变量：回落率本身不上灯——RPA-only 部署 100% 回落是正常的，不应误报。"""
    # 只有回落率高、但编排器无 error worker → 不因回落率抬灯
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        orchestrator={"total": 1, "by_state": {"running": 1}},
        send_routes={"total": 100, "fallback_rate": 1.0,
                     "adapter_total": 100, "orchestrator_total": 0},
    )
    assert ov["overall_light"] == "green"
    assert ov["kpis"]["send_fallback_rate"] == 1.0  # 数值仍暴露（信息量）
    assert ov["kpis"]["orchestrator_worker_light"] == "green"


def test_assemble_orchestrator_error_escalates_to_yellow():
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        orchestrator={"total": 3, "by_state": {"running": 2, "error": 1}},
    )
    assert ov["overall_light"] == "yellow"
    assert ov["kpis"]["orchestrator_worker_light"] == "yellow"
    assert ov["kpis"]["orchestrator_workers_error"] == 1
    assert ov["kpis"]["orchestrator_workers_running"] == 2
    assert ov["sections"]["orchestrator"]["by_state"]["error"] == 1


def test_assemble_orchestrator_none_does_not_affect_overall():
    ov = assemble_ops_overview(health={"light": "green"}, reliability={"light": "green"})
    assert ov["overall_light"] == "green"
    assert ov["kpis"]["orchestrator_worker_light"] == ""
    assert ov["kpis"]["send_fallback_rate"] == 0.0
    assert ov["kpis"]["send_total"] == 0


# ── P6：编排器 worker 崩溃问题项（告警外发 + 总览灯升级共用口径） ──────────────

def test_orchestrator_worker_problems_severity_by_restarts():
    status = {
        "total": 3,
        "accounts": [
            {"platform": "telegram", "account_id": "a1", "state": "running"},
            {"platform": "line", "account_id": "a2", "state": "error", "restarts": 1,
             "last_error": "adb lost"},
            {"platform": "whatsapp", "account_id": "a3", "state": "error", "restarts": 5,
             "last_error": "session dead"},
        ],
    }
    probs = orchestrator_worker_problems(status)
    by_id = {p["id"]: p for p in probs}
    assert set(by_id) == {"line:a2", "whatsapp:a3"}  # running 不计入
    assert by_id["line:a2"]["status"] == "warn"       # restarts<3 → 瞬时抖动
    assert by_id["whatsapp:a3"]["status"] == "fail"   # restarts>=3 → 真实掉线
    assert by_id["whatsapp:a3"]["detail"] == "session dead"


def test_orchestrator_worker_problems_empty_cases():
    assert orchestrator_worker_problems(None) == []
    assert orchestrator_worker_problems({}) == []
    assert orchestrator_worker_problems({"accounts": []}) == []
    # 仅 by_state（无 accounts 明细）→ 无法逐条判定 → 空
    assert orchestrator_worker_problems({"total": 1, "by_state": {"error": 1}}) == []


def test_orchestrator_worker_light_red_on_persistent_crash():
    """accounts 里有持续崩溃（restarts>=3）→ 总览灯升级到 red（与 P6 告警同口径）。"""
    status = {
        "total": 2,
        "by_state": {"running": 1, "error": 1},
        "accounts": [
            {"platform": "telegram", "account_id": "a1", "state": "running"},
            {"platform": "line", "account_id": "a2", "state": "error", "restarts": 4},
        ],
    }
    assert orchestrator_worker_light(status) == "red"
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        orchestrator=status,
    )
    assert ov["overall_light"] == "red"
    assert ov["kpis"]["orchestrator_worker_light"] == "red"


def test_orchestrator_worker_light_yellow_on_transient_crash():
    status = {
        "total": 2,
        "by_state": {"running": 1, "error": 1},
        "accounts": [
            {"platform": "line", "account_id": "a2", "state": "error", "restarts": 1},
        ],
    }
    assert orchestrator_worker_light(status) == "yellow"


# ── P0-3/B9：入站自动译量接入总览（成本护栏观测） ───────────────────────────

def test_assemble_surfaces_inbound_translation_volume():
    it = {"translated": 42, "failed": 3, "by_source_lang": {"en": 30, "ja": 12},
          "trend": [{"day": "07-10", "translated": 42}]}
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        inbound_translation=it,
    )
    assert ov["kpis"]["inbound_xlate_translated"] == 42
    assert ov["kpis"]["inbound_xlate_failed"] == 3
    assert ov["sections"]["inbound_translation"] is it
    # 纯观测：不影响总览灯
    assert ov["overall_light"] == "green"


def test_assemble_inbound_translation_absent_defaults_zero():
    ov = assemble_ops_overview()
    assert ov["kpis"]["inbound_xlate_translated"] == 0
    assert ov["kpis"]["inbound_xlate_failed"] == 0
    assert ov["sections"]["inbound_translation"] == {}


# ── C6：授权/试用运营看板（纯观测，不参与总览灯） ───────────────────────────

def test_assemble_surfaces_license_trial_quota():
    lic = {
        "state": "active", "plan": "pro", "trial": True, "days_left": 12,
        "quota": {"included_chars": 50000, "used_chars": 12000, "remaining_chars": 38000},
    }
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"}, license=lic,
    )
    k = ov["kpis"]
    assert k["license_state"] == "active"
    assert k["license_plan"] == "pro"
    assert k["license_trial"] is True
    assert k["license_days_left"] == 12
    assert k["license_included_chars"] == 50000
    assert k["license_used_chars"] == 12000
    assert k["license_remaining_chars"] == 38000
    assert ov["sections"]["license"] is lic
    # 关键不变量：授权是商业状态，绝不参与系统健康灯
    assert ov["overall_light"] == "green"


def test_assemble_license_expired_does_not_touch_overall_light():
    lic = {"state": "expired", "plan": "pro", "trial": False,
           "quota": {"included_chars": 0, "used_chars": 0, "remaining_chars": None}}
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"}, license=lic,
    )
    assert ov["kpis"]["license_state"] == "expired"
    assert ov["overall_light"] == "green"   # 过期是商业事件，不抬健康灯


def test_assemble_license_absent_defaults_empty():
    ov = assemble_ops_overview()
    assert ov["kpis"]["license_state"] == ""
    assert ov["kpis"]["license_included_chars"] == 0
    assert ov["kpis"]["license_remaining_chars"] is None
    assert ov["sections"]["license"] == {}


# ── J1：「📄 人设导入」卡的长传记检索区（入库≠被用上，命中率才是真读数） ──────

def test_persona_import_card_renders_bio_retrieval_region():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="personaBioRetrieval"' in src
    assert "_pbRetrievalHtml" in src
    assert "pi.bio_retrieval" in src           # 复用既有 metrics 拉取，不另发请求


def test_persona_import_bio_retrieval_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_pi_bio_avg_hits", "ov2_pi_bio_embed_fail",
            "ov2_pi_bio_embed_warn", "ov2_pi_bio_empty", "ov2_pi_bio_hint",
            "ov2_pi_bio_hit_rate", "ov2_pi_bio_queries", "ov2_pi_bio_title")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「🧾 AI 价值周报」卡（2026-08-06 P1-3 消费面：value_report 聚合 → ops 可见） ──

def test_xlate_agent_feel_row_renders_and_registered():
    """「坐席体感」行三件套（2026-08-09 翻译提速批次）：div 声明 / loader 读
    ui-event 趋势 ``xl_batch_`` 前缀 / 挂进 xlate 卡 loaders——少任何一件都是
    静默缺陷（有 div 没 loader＝永远空白；有 loader 没注册＝白写；注册了没
    div＝白请求）。数据源=unified_inbox 批量补译前端埋点（同名分桶动作）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="xlateAgentFeel"' in src
    assert "async function loadXlateAgentFeel()" in src
    assert "prefix=xl_batch_" in src                              # loader 数据源
    assert "loadXlateTrend, loadXlateAgentFeel" in src            # 卡注册表登记
    # 埋点动作名与收件箱发射端同名（两文件漂移=看板静默空行）
    inbox = (Path(__file__).resolve().parents[1]
             / "src" / "web" / "templates" / "unified_inbox.html").read_text(
                 encoding="utf-8")
    for act in ("xl_batch_lt300", "xl_batch_lt1000", "xl_batch_lt3000",
                "xl_batch_slow", "xl_batch_fail", "xl_batch_fallback404"):
        assert act in src, f"ops 卡缺分桶 {act}"
        assert act in inbox, f"收件箱埋点缺分桶 {act}"


def test_group_members_card_renders_and_registered():
    """🧲 群成员提取卡三件套（section / loader / 注册表）+ 零流量藏卡不变量。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="gmembersSection"' in src
    assert "async function loadGroupMembers()" in src
    assert "/api/workspace/metrics" in src
    assert "anchor:'gmembersKpis'" in src
    assert "if(!gm || !gm.active){ opsHideCardEl(sec, 'gmembers', 'disabled'); return; }" in src


# ── 「😊 表情包」卡（2026-08-17 表情包主线：备货/发送形态读数面）──

def test_sticker_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 零流量隐藏惯例。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。sent_as 分桶是本卡
    存在理由——image 回退占比高＝目标平台原生贴纸能力缺口（WA 边车未升级 /
    LINE 自建包为主），没有它回退只能翻日志。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="stickersSection"' in src
    assert "async function loadStickers()" in src
    assert "d.stickers" in src                       # 数据源（metrics 段）
    assert "anchor:'stkKpis'" in src                 # 卡片注册表登记
    # 零备货且零发送 → 整卡隐藏惯例
    assert "if(!s || (!sends && !items)){ opsHideCardEl(sec, 'stk', 'no_data'); return; }" in src


def test_sticker_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_sticker_card import EN, ZH

    keys = ("ov2_s_stk", "ov2_stk_packs", "ov2_stk_items", "ov2_stk_sends",
            "ov2_stk_collects", "ov2_stk_by_plat", "ov2_stk_sent_as",
            "ov2_stk_as_native", "ov2_stk_as_image", "ov2_stk_fallback_hint",
            "ov2_js_stk_none")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_group_members_card_i18n_bilingual():
    from src.web.i18n_packs.group_members import EN, ZH

    keys = ("ov2_s_gm", "ov2_gm_sub", "ov2_gm_members", "ov2_gm_groups", "ov2_gm_jobs")
    for k in keys:
        assert k in ZH and ZH[k], f"ZH 缺 {k}"
        assert k in EN and EN[k], f"EN 缺 {k}"


def test_value_weekly_card_renders_and_registered():
    """卡片三件套必须齐：section 声明 / loader 读 value 段 / 注册表登记。

    历史教训（哑按钮/孤儿卡门禁的同款风险）：三者少任何一件都是静默缺陷——
    有 section 没注册＝永远 loading；有 loader 没 section＝白请求；注册了
    没 loader＝卡永远空。三断言钉成一个不变量。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="valueWeeklySection"' in src
    assert "async function loadValueWeekly()" in src
    assert "/api/report/weekly" in src           # loader 数据源
    assert "anchor:'valueWeeklyKpis'" in src     # P2 卡片注册表登记
    # 零流量整卡隐藏的站内惯例（value 缺失/全零 → hide + 上报 no_data）
    assert "if(!v.this_week || total === 0){ opsHideCardEl(sec, 'value', 'no_data'); return; }" in src


def test_value_weekly_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_value", "ov2_vw_sub", "ov2_vw_drafts", "ov2_vw_draft_sent",
            "ov2_vw_outreach", "ov2_vw_reply", "ov2_vw_out", "ov2_vw_in",
            "ov2_vw_status", "ov2_vw_levels", "ov2_vw_batches",
            "ov2_vw_wow_new", "ov2_vw_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「🪪 登录身份决议」卡（实施72 2026-08-27：换号检测的唯一 UI 面）───────────

def test_login_identity_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 隐藏语义 + 转正写口。

    隐藏语义要同时看 pending / audit / 自有号候选三个来源：隔离态是**瞬时**的
    （转正后 pending 归空），只按 pending 判隐藏会让「今天换过号」这件事在事后
    彻底不可见；候选行（下一阶段 P1）是第三源——对端命中归档账号却没登记
    own_fleet.extra，只此一处提示，藏了＝跨机自聊回路无人知晓。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="lIdentSection"' in src
    assert "async function loadLoginIdentity()" in src
    assert "/api/admin/account-identity/pending" in src       # loader 数据源
    assert "anchor:'lIdentKpis'" in src                       # 卡片注册表登记
    # 三源皆空才隐藏（缺一＝瞎）
    assert "if(!pend.length && !recent.length && !cands.length){" in src
    # 候选行消费 fleet_candidates 字段且只提示不写（无任何自动登记写口）
    assert "d.fleet_candidates" in src
    assert "ov2_li_fleet_title" in src
    # 有 pending 或候选都要亮黄（都是「需要人动手」的状态）
    assert "(pend.length || cands.length) ? 'yellow'" in src
    # 转正写口 + 角色收敛（后端拒 agent/viewer，前端不该给按钮）
    assert "/api/admin/account-identity/confirm" in src
    assert "if(!CAN_MANAGE_OPS) return;" in src
    # 动态行用事件委托而非逐行 onclick（动态点属性拼接门禁的同款坑）
    assert "data-lident-ok" in src


def test_login_identity_card_i18n_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_lident", "ov2_li_hint", "ov2_li_pending", "ov2_li_quar",
            "ov2_li_conf", "ov2_li_pending_title", "ov2_li_audit_title",
            "ov2_li_prev", "ov2_li_persona", "ov2_li_confirm_btn",
            "ov2_li_confirm_q", "ov2_li_confirm_ok", "ov2_li_ev_quar",
            "ov2_li_ev_conf",
            # 疑似自有号未登记（下一阶段 P1）
            "ov2_li_fleet", "ov2_li_fleet_title", "ov2_li_fleet_hint",
            "ov2_li_fleet_matched", "ov2_li_fleet_match_id",
            "ov2_li_fleet_match_name")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「🌐 一键代理」卡（2026-08-21 P2：托管订阅/续期/库存进看板）─────────────

def test_proxy_managed_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 隐藏语义。

    隐藏语义特别钉一条：功能关着**且从没有过订阅**才隐藏——关着但还有活跃
    订阅在计时必须照常显示（运营必须看得见正在倒计时的钱）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="pxmSection"' in src
    assert "async function loadProxyManaged()" in src
    assert "/api/proxies/managed/overview" in src      # loader 数据源
    assert "anchor:'pxmKpis'" in src                   # 卡片注册表登记
    assert "opsHideCardEl(sec, 'pxm', 'disabled')" in src
    # 隐藏必须同时看「功能关」与「有没有订阅」两个条件（缺一=瞎）
    assert "!cap.enabled && !(st.active||0) && !(st.expired_total||0)" in src


def test_proxy_managed_card_i18n_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_pxm", "ov2_px_active", "ov2_px_exp7", "ov2_px_billpend",
            "ov2_px_lowstock", "ov2_px_col_country", "ov2_px_col_kind",
            "ov2_px_col_days", "ov2_px_col_renew", "ov2_px_stock_line",
            "ov2_px_pending_line", "ov2_px_ok", "ov2_px_hint",
            "ov2_px_vendor_bal")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「🧾 账号真相」卡（2026-08-17 账号单源收口 P3：registry × 会话目录对账）──

def test_accounts_truth_card_renders_and_registered():
    """卡片三件套（section / 渲染函数 / 注册表）+ 共享 metrics 分发链两分支 + 隐藏惯例。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。这张卡是「顶栏 vs 面板
    账号数分裂」修复后的防回归读数面——幽灵账号（仅目录）持续增长＝有账号绕过
    注册表在收发。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="acctTruthSection"' in src
    assert "function renderAccountsTruth(d)" in src
    assert "d.accounts_truth" in src              # 数据源（共享 /api/workspace/metrics）
    assert "anchor:'acctTruthKpis'" in src        # 卡片注册表登记
    assert "ov2_at_desktop" in src                # 桌面镜像 KPI（与真幽灵分开）
    # 共享分发链两分支都要接：正常响应 + 403（无主管权限时清卡不留 loading）
    assert "renderAccountsTruth(d)" in src
    assert "renderAccountsTruth(null)" in src
    # 无数据（旧后端/registry 不可用/全零）整卡隐藏的站内惯例
    assert "if(!at || (!regTot && !dirTot)){ opsHideCardEl(sec, 'atruth', 'no_data')" in src


def test_accounts_truth_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_atruth", "ov2_at_registry", "ov2_at_dir",
            "ov2_at_ghost", "ov2_at_desktop", "ov2_at_status", "ov2_at_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「🌐 跨平台档案」卡（P2 2026-08-18：来源背景 → AI 记忆注入的读数面）──

def test_origin_profile_card_renders_and_registered():
    """卡片三件套（section / 渲染函数 / 注册表）+ 共享 metrics 分发链两分支 + 隐藏惯例。

    与 accounts_truth 卡同款不变量：三者少任何一件都是静默缺陷。读数回答三问：
    坐席在不在用（档案/导入量）、AI 吃没吃到（注入命中）、合并联动有没有跑（合流量）；
    开关关着但已有档案 → 亮黄（「填了 AI 用不上」比零数据更值得看见）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="originProfileSection"' in src
    assert "function renderOriginProfile(d)" in src
    assert "d.origin_profile" in src             # 数据源（共享 /api/workspace/metrics）
    assert "anchor:'originProfileKpis'" in src   # 卡片注册表登记
    # 共享分发链两分支：正常响应 + 403 清卡
    assert "renderOriginProfile(d)" in src
    assert "renderOriginProfile(null)" in src
    # 零数据整卡隐藏惯例
    assert "if(!op || (!profiles && !imports && !lookups)){ opsHideCardEl(sec, 'originp', 'no_data')" in src
    # 开关关但有档案 → 黄灯提醒
    assert "ov2_or_flag_off" in src


def test_origin_profile_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_origin", "ov2_or_hint", "ov2_or_profiles", "ov2_or_imports",
            "ov2_or_facts", "ov2_or_inject", "ov2_or_merge", "ov2_or_flag_off")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「🎁 邀请裂变」卡（2026-08-11 免费额度升级配套：官网 referral 台账聚合 → ops 可见）──

def test_referral_growth_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 未启用/零流量隐藏惯例。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。这张卡是「免费 100 万 +
    邀请裂变」增长引擎唯一的厂商侧读数面——没有它，裂变跑没跑起来只能去翻官网
    控制台。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="referralGrowthSection"' in src
    assert "async function loadReferralGrowth()" in src
    assert "/api/admin/referral-stats" in src            # loader 数据源（实例代理）
    assert "anchor:'referralGrowthKpis'" in src          # 卡片注册表登记
    # 未启用（配置默认关）与零流量都必须整卡隐藏，并上报原因
    assert "if(!d || !d.ok || !d.enabled){ opsHideCardEl(sec, 'referral', 'disabled'); return; }" in src
    assert "if(!(Number(d.codes)||0) && !(Number(d.registered)||0)){ opsHideCardEl(sec, 'referral', 'no_data'); return; }" in src


def test_referral_growth_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_refg", "ov2_rg_sub", "ov2_rg_codes", "ov2_rg_registered",
            "ov2_rg_qualified", "ov2_rg_invitee", "ov2_rg_inviter",
            "ov2_rg_granted", "ov2_rg_flagged", "ov2_rg_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「⏱️ 回复时延 SLO」卡（2026-08-09 P1-8 消费面：reply_latency 聚合 → ops 可见） ──

def test_reply_latency_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 零流量隐藏惯例。

    这张卡是「被吞回复等 11 分钟」事故的量化闭环：没有它，时延劣化只能靠
    客户抱怨发现。三件套少任何一件都是静默缺陷（与 value 卡同款不变量）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="replyLatencySection"' in src
    assert "async function loadReplyLatency()" in src
    assert "d.reply_latency" in src                  # loader 数据源（metrics 段）
    assert "anchor:'replyLatencyKpis'" in src        # 卡片注册表登记
    # 零流量整卡隐藏惯例（d1 与 d7 均无等待段 → display none）
    assert "if(!(Number(d1.episodes)||0) && !(Number(d7.episodes)||0))" in src


def test_reply_latency_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_rlat", "ov2_rl_sub", "ov2_rl_eps", "ov2_rl_p50",
            "ov2_rl_p95", "ov2_rl_unans", "ov2_rl_unans_short",
            "ov2_rl_p95_7d", "ov2_rl_buckets", "ov2_rl_byplat", "ov2_rl_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_entrance_slo_card_renders_and_registered():
    """入口可用性卡三件套（section / loader / 注册表）+ 数据源 + 无数据隐藏惯例。

    P2-1（2026-08-12 可靠性复盘）：8/12 两次红条查岗的量化闭环——服务端探测与
    坐席端 conn_* 回执双视角并排，差值=客户端侧损耗。三件套少任何一件都是
    静默缺陷（与 rlatency 卡同款不变量）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="entranceSloSection"' in src
    assert "async function loadEntranceSlo()" in src
    assert "d.entrance_slo" in src                   # loader 数据源（metrics 段）
    assert "anchor:'entranceSloKpis'" in src         # 卡片注册表登记
    # 无探测数据（非 117 部署形态/日志缺失）→ 整卡隐藏惯例
    assert "if(!(Number(sv.ticks)||0)){ opsHideCardEl(sec, 'eslo', 'no_data'); return; }" in src


def test_entrance_slo_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_eslo", "ov2_eslo_sub", "ov2_es_avail7", "ov2_es_avail30",
            "ov2_es_out7", "ov2_es_cli7", "ov2_es_clisplit",
            "ov2_es_cli_server", "ov2_es_cli_local", "ov2_es_cli_none",
            "ov2_es_recent", "ov2_es_ongoing", "ov2_es_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_entrance_slo_metrics_wired():
    """metrics 接线钉住：drafts_routes 必须把 entrance_slo 并进 /api/workspace/metrics
    （卡片的唯一数据源；接线丢了卡片会静默消失而不是报错）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(
               encoding="utf-8")
    assert "from src.ops.entrance_slo import entrance_slo_snapshot" in src
    assert 'metrics["entrance_slo"]' in src


# ── 「🔤 字符额度」卡（2026-08-16 用量页 v2 配套：char-usage 聚合 → ops 可见）──

def test_usage_quota_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 隐藏惯例 + 新字段容错。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。额外钉两条本卡特有约束：
    ① 非主管(403)/端点未装载(重启前 404)与「计量关闭且零消耗」都必须整卡隐藏
    （零流量不占版面）；② enforce 是后端重启后才有的新字段——typeof 判定缺席时，
    中间态会把 undefined 渲染进计量状态行。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="uqCharSection"' in src
    assert "async function loadUsageQuota()" in src
    assert "/api/users/char-usage" in src               # loader 数据源
    assert "anchor:'uqCharKpis'" in src                 # P2 卡片注册表登记
    # 计量关闭且零消耗 → 整卡隐藏（零流量惯例；403/404 由 !r.ok 分支覆盖）
    assert "if(d.enabled===false && monthTotal===0){ opsHideCardEl(sec, 'uqchar', 'disabled'); return; }" in src
    # enforce 新字段 undefined 容错（重启前只显示开/关，不显示硬限档位）
    assert "typeof d.enforce === 'boolean'" in src


def test_usage_quota_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_uq_title", "ov2_uq_sub", "ov2_uq_meter", "ov2_uq_on", "ov2_uq_off",
            "ov2_uq_enforce_on", "ov2_uq_enforce_soft", "ov2_uq_pool",
            "ov2_uq_unlimited", "ov2_uq_month", "ov2_uq_alerts", "ov2_uq_top")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 「💰 额度墙转化」卡（quotawall v2 P2，2026-08-21：拦截弹窗的营收漏斗读数面）──

def test_qw_funnel_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 隐藏惯例。

    与 value/msgops 卡同款不变量：三者少任何一件都是静默缺陷。额外钉：
    ① ops.ui_event_trend 关（enabled:false）→ 整卡隐藏（disabled）；
    ② 14 天窗口零 qw_ 事件 → 整卡隐藏（no_data，零流量不占版面）；
    ③ 旧后端/非主管 → opsHideHttp 统一口径。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="qwFunnelSection"' in src
    assert "async function loadQwFunnel()" in src
    assert "/api/admin/ui-event-trend?days=14&prefix=qw_" in src   # 数据源
    assert "anchor:'qwFunnelKpis'" in src                          # 注册表登记
    assert "opsHideCardEl(sec, 'qwfun', 'disabled')" in src
    assert "opsHideCardEl(sec, 'qwfun', 'no_data')" in src
    assert "opsHideHttp(sec, 'qwfun', r.status)" in src
    assert "qwfun:    {reason:'no_data'}" in src                   # 隐藏原因回落表


def test_qw_funnel_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_qwf_title", "ov2_qwf_sub", "ov2_qwf_shown", "ov2_qwf_buy",
            "ov2_qwf_credit", "ov2_qwf_voucher", "ov2_qwf_variants",
            "ov2_qwf_later", "ov2_qwf_low")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k
        assert ZH[k] != EN[k]


# ── 「🗑️ 消息管理审计」卡（2026-08-17 消息管理 P2：前台删除、后台留痕的读数面）──

def test_msg_ops_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 隐藏惯例。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。本卡额外钉：
    ① 旧后端 404 / 非主管 403 → `!r.ok` 整卡隐藏（模板热更先于重启的中间态自洽）；
    ② 零操作整卡隐藏（审计卡没数据就别占版面）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="msgOpsSection"' in src
    assert "async function loadMsgOps()" in src
    assert "/api/admin/msg-ops-stats" in src            # loader 数据源
    assert "anchor:'msgOpsKpis'" in src                 # P2 卡片注册表登记
    assert "if(total === 0){ opsHideCardEl(sec, 'msgops', 'no_data'); return; }" in src


def test_msg_ops_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_msg_ops_card import EN, ZH

    keys = ("ov2_s_msgops", "ov2_mo_sub", "ov2_mo_revoke", "ov2_mo_revoke_ok",
            "ov2_mo_del", "ov2_mo_clear", "ov2_mo_delconv", "ov2_mo_restore",
            "ov2_mo_fail_reasons", "ov2_mo_recent", "ov2_mo_col_time",
            "ov2_mo_col_kind", "ov2_mo_col_who", "ov2_mo_col_where",
            "ov2_mo_col_n", "ov2_mo_col_result", "ov2_mo_ok",
            "ov2_mo_k_msg_revoke", "ov2_mo_k_msg_delete_local",
            "ov2_mo_k_conv_clear", "ov2_mo_k_conv_clear_remote",
            "ov2_mo_k_conv_delete", "ov2_mo_k_msg_restore")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_msg_ops_kind_labels_cover_ledger_kinds():
    """ops 卡 kind 词条必须覆盖 msg-ops-stats 的 kinds 清单（漏一个=英文枚举上屏）。"""
    import ast
    import re
    from pathlib import Path

    from src.web.i18n_packs.ops_msg_ops_card import EN, ZH

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "unified_inbox_msgops_routes.py"
           ).read_text(encoding="utf-8")
    m = re.search(r"kinds\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "msg-ops-stats kinds list missing"
    kinds = ast.literal_eval("[" + m.group(1) + "]")
    assert "conv_clear_remote" in kinds
    for kind in kinds:
        key = f"ov2_mo_k_{kind}"
        assert ZH.get(key), key
        assert EN.get(key), key


# ── 隐藏能力清单（2026-08-18：页底长文 → 收起 chip + 原因分组）──

def test_hidden_cards_panel_not_a_title_wall():
    """可发现性还在，但禁止再把全部卡标题拼成一行灰字。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="opsHidPanel"' in src
    assert 'id="opsHidToggle"' in src
    assert 'id="opsHidTbChip"' in src
    assert "function opsSetCardHidden(" in src
    assert "function _opsRenderHidden(" in src
    assert "hiddenTitles.join" not in src
    assert 'id="opsHiddenNote"' not in src
    assert "data-hidkey" in src
    assert "_OPS_HIDE_HINT" in src


def test_hidden_cards_i18n_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = (
        "ov2_hid_summary", "ov2_hid_expand", "ov2_hid_collapse",
        "ov2_hid_grp_off", "ov2_hid_grp_wait", "ov2_hid_grp_healthy",
        "ov2_hid_grp_err", "ov2_hid_grp_noacc", "ov2_hid_grp_other",
        "ov2_hid_why_off", "ov2_hid_why_wait", "ov2_hid_why_healthy",
        "ov2_hid_why_err", "ov2_hid_why_noacc", "ov2_hid_why_other",
        "ov2_hid_open", "ov2_hid_shown",
    )
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k
        assert ZH[k] != EN[k]


def test_hidden_cards_reasons_wired_into_loaders():
    """结构化隐藏原因已接进高频隐藏卡的 loader（P1 收尾）。

    抽查四类语义各一例：disabled（rw）/ no_data（vision）/ healthy（buried，
    零异常才隐藏）/ no_access（403）。blurb 键与模板动态取键前缀成对存在。
    """
    from pathlib import Path

    from src.web.i18n_packs.ops_overview_page import EN, ZH

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert "opsHideCardEl(sec, 'rw', 'disabled')" in src
    assert "opsHideCardEl(sec, 'vision', 'no_data')" in src
    assert "opsHideCardEl(sec, 'buried', 'healthy')" in src
    assert "opsHideCardEl(sec, 'vision', 'no_access')" in src
    assert "'ov2_hidb_' + (c.key || '')" in src        # popover 动态取 blurb 键
    for k in ("ov2_hidb_vision", "ov2_hidb_buried", "ov2_hidb_rw", "ov2_hidb_temper"):
        assert ZH.get(k), k
        assert EN.get(k), k
    # P3 批：HTTP 态统一口径助手（403=no_access / 404=disabled / 其余=error）+
    # 收起态按原因拆解 + 零异常 chip 绿系皮肤。
    assert "function opsHideHttp(" in src
    assert "opsHideHttp(sec, 'uqchar', r.status)" in src
    assert "opsHideCardEl(sec, 'msgops', 'no_data')" in src
    assert "opsHideCardEl(sec, 'proactive', 'disabled')" in src
    assert "var byReason = {};" in src
    assert ".ops-hid-chip.healthy" in src


def test_boss_view_wired():
    """老板视图（焦点档）三件套：入口按钮 / bhide 隐藏语义 / 后台降频刷新。

    缺任一都是静默缺陷：没按钮＝档位不可达；bhide 不进分组计数＝空组不收、
    计数说谎；被藏卡不进低频刷新＝灯变红也永远不现身（焦点档变闭眼档）。
    筛选让位（_applyBossMode 读 opsFilter）防「搜得到却被焦点档吞掉」。
    """
    from pathlib import Path

    from src.web.i18n_packs.ops_overview_page import EN, ZH

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="opsBossBtn"' in src
    assert 'onclick="opsToggleBossView()"' in src
    assert "function opsToggleBossView(" in src
    assert "section[data-ops-bhide]{display:none!important}" in src
    assert "_condHidden(c) || _filtered(c) || _bossHidden(c)" in src
    assert "if(_bossHidden(c)){" in src                     # 低频后台刷新分支
    assert "localStorage.getItem('ops.boss.v1')" in src     # 跨会话记忆
    assert "getElementById('opsFilter')" in src             # 筛选让位判据
    # P6a：深链 ?view=boss/all 覆写初始档（不写 localStorage）+ 切换同步 URL +
    # 档位生效提示行（跨会话记忆的档位必须自我解释，否则复刻「卡去哪了」老困惑）
    assert "new URLSearchParams(location.search).get('view')" in src
    assert "u.searchParams.set('view', 'boss')" in src
    assert "history.replaceState(null, '', u)" in src
    assert 'id="opsBossNote"' in src
    assert "_opsHidFmt('ov2_view_boss_on', tucked)" in src
    for k in ("ov2_view_boss", "ov2_view_boss_t", "ov2_view_boss_on"):
        assert ZH.get(k), k
        assert EN.get(k), k
        assert ZH[k] != EN[k]


# ── 「🪙 Token 计费」卡（2026-08-19 Token 定价改版观测三件套之三）────────────────

def test_token_ledger_card_renders_and_registered():
    """卡片三件套（section / 渲染函数 / 注册表）+ 共享 metrics 分发链两分支 + 隐藏惯例。

    读数回答三问：钱包还剩多少（该不该买包）、今天/累计烧了多少（计费在不在跑）、
    enforce 开没开（耗尽后付费动作是否在降级）。enabled=false → 整卡隐藏（disabled）；
    余额耗尽且钱包曾注资 → 黄灯。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="tokLedgerSection"' in src
    assert "function renderTokenLedger(d)" in src
    assert "d.token_ledger" in src               # 数据源（共享 /api/workspace/metrics）
    assert "anchor:'tokLedgerKpis'" in src       # 卡片注册表登记
    # 共享分发链两分支：正常响应 + 403 清卡
    assert "renderTokenLedger(d)" in src
    assert "renderTokenLedger(null)" in src
    # 未启用整卡隐藏惯例 + 耗尽黄灯
    assert "opsHideCardEl(sec, 'tokled', tk ? 'disabled' : 'no_data')" in src
    assert "opsSetCardLight('tokled', exhausted ? 'yellow' : '')" in src


def test_token_ledger_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_tokled", "ov2_tok_balance", "ov2_tok_today", "ov2_tok_total",
            "ov2_tok_enforce", "ov2_tok_enforce_on", "ov2_tok_enforce_off",
            "ov2_tok_expired", "ov2_tok_by_action", "ov2_tok_shadow",
            "ov2_tok_fair_use")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_assistant_card_renders_and_registered():
    """🤖 AI 助手卡三件套（section / loader / 注册表）+ 数据源 + 隐藏惯例。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。本卡额外钉：
    ① 旧后端 404 / 非白名单 403 / 未启用 → 整卡隐藏（模板热更先于重启的
    中间态自洽）；② 灰度期启用后零流量**仍显示**（刻意反「零流量隐藏」惯例：
    语料条数就是就绪读数）；③ 语料 0 条或 LLM 未接线 → 黄灯。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="assistSection"' in src
    assert "async function loadAssistant()" in src
    assert "/api/assistant/health" in src                # loader 数据源
    assert "anchor:'assistKpis'" in src                  # 卡片注册表登记
    # 未启用/旧后端整卡隐藏惯例
    assert "if(!r.ok){ opsHideCardEl(sec, 'assist', 'disabled'); return; }" in src
    assert ("if(!d || !d.ok || !d.enabled){ opsHideCardEl(sec, 'assist', "
            "'disabled'); return; }") in src
    # 语料空/LLM 断线黄灯
    assert "opsSetCardLight('assist', (kb === 0 || !d.llm_wired) ? 'yellow' : '')" in src
    # miss 照单（本卡存在理由：运营补语料的入口）
    assert "ov2_as_miss" in src


def test_assistant_card_i18n_keys_bilingual():
    from src.web.i18n_packs.assistant_ball import EN, ZH

    keys = ("ov2_s_assist", "ov2_as_hint", "ov2_as_kb", "ov2_as_qa7",
            "ov2_as_rate", "ov2_as_fb", "ov2_as_reports", "ov2_as_rl",
            "ov2_as_miss", "ov2_as_nomiss")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_token_ledger_metrics_and_prom_wired():
    """drafts_routes 双出口接线：/api/workspace/metrics.token_ledger + Prometheus 段。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(
               encoding="utf-8")
    assert 'metrics["token_ledger"]' in src
    assert "from src.licensing.token_ledger import metrics_snapshot" in src
    assert "from src.licensing.token_ledger import dump_prom" in src


# ── 「🎨 手动出图」卡（2026-08-22 P2：坐席 AI 生成图片漏斗读数面）──

def test_image_gen_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 零流量隐藏惯例。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。这张卡是「模型被清空
    数小时无人知」事故的读数面收口——model_missing 失败码在此处涨=运维事故，
    vram_insufficient 涨=算力互挤，相册秒发占比=album-first 层省了多少 GPU。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="imgenSection"' in src
    assert "async function loadImageGen()" in src
    assert "d.image_gen || null" in src          # loader 数据源
    assert "anchor:'imgenKpis'" in src           # 注册表登记
    assert "opsHideCardEl(sec, 'imgen', 'no_data')" in src  # 零流量隐藏


def test_image_gen_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_image_gen_card import EN, ZH

    keys = ("ov2_s_imgen", "ov2_ig_total", "ov2_ig_okrate", "ov2_ig_p50",
            "ov2_ig_p95", "ov2_ig_sent", "ov2_ig_fails", "ov2_ig_srcsplit",
            "ov2_ig_src_gen", "ov2_ig_src_album", "ov2_ig_stock",
            "ov2_ig_cancel", "ov2_ig_none")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


def test_image_gen_metrics_and_prom_wired():
    """drafts_routes 双出口接线：metrics.image_gen + Prometheus image_gen_*。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "routes" / "drafts_routes.py").read_text(
               encoding="utf-8")
    assert 'metrics["image_gen"]' in src
    assert src.count("from src.web.image_gen_stats import get_image_gen_stats") >= 2


# ── 「🛑 紧急停发」卡（P1 2026-08-23 急停可见化：隐藏面板的常驻替身）──

def test_ksguard_card_renders_and_registered():
    """卡片三件套（section / loader / 注册表）+ 数据源 + 隐藏/灯语义。

    与 value 卡同款不变量：三者少任何一件都是静默缺陷。本卡存在理由：唯一的
    置位/解除面板在 /rpa-overview 而矩阵导航默认隐藏（ui_visibility）——急停
    生效时管理员必须在常驻可见的运营总览看得见、解得掉。额外钉：
    ① 无生效急停 → 整卡隐藏（healthy，安全网待命不占版面）；
    ② 全局冻结红灯 / 局部黄灯；
    ③ 解除按钮走事件委托 + CAN_MANAGE_OPS 显隐（服务端 DELETE 再闸）；
    ④ 来源判定与 kill_switch.freeze_source 同契约（auto_ 前缀 / ban_signal）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "templates" / "ops_overview.html").read_text(
               encoding="utf-8")
    assert 'id="ksGuardSection"' in src
    assert "async function loadKsGuard()" in src
    assert "/api/ops/kill-switch" in src                 # loader 数据源
    assert "anchor:'ksGuardKpis'" in src                 # 卡片注册表登记
    assert "opsHideCardEl(sec, 'ksguard', 'healthy')" in src   # 无急停隐藏
    assert "opsSetCardLight('ksguard', d.frozen?'red':'yellow')" in src
    assert "CAN_MANAGE_OPS" in src
    assert "ks-lift-btn" in src and "ksGuardLift(" in src      # 事件委托解除
    assert "auto_pause:" in src and "auto_ban:" in src         # 来源判定同契约


def test_ksguard_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_killswitch_card import EN, ZH

    keys = ("ov2_s_ksguard", "ov2_ks_active", "ov2_ks_global",
            "ov2_ks_global_on", "ov2_ks_global_off", "ov2_ks_auto",
            "ov2_ks_src_auto", "ov2_ks_src_manual", "ov2_ks_ttl_until",
            "ov2_ks_ttl_perm", "ov2_ks_lift_btn", "ov2_ks_lift_confirm",
            "ov2_ks_lift_fail", "ov2_ks_lift_fail_net", "ov2_ks_hint")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k
