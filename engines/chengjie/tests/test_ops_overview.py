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
    # 零流量整卡隐藏的站内惯例（value 缺失/全零 → display none）
    assert "if(!v.this_week || total === 0){ sec.style.display='none'; return; }" in src


def test_value_weekly_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    keys = ("ov2_s_value", "ov2_vw_sub", "ov2_vw_drafts", "ov2_vw_draft_sent",
            "ov2_vw_outreach", "ov2_vw_reply", "ov2_vw_out", "ov2_vw_in",
            "ov2_vw_status", "ov2_vw_levels", "ov2_vw_batches",
            "ov2_vw_wow_new", "ov2_vw_hint")
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
