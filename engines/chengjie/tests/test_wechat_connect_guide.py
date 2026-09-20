# -*- coding: utf-8 -*-
"""实施97 · 接入引导页（微信客服五步 / 个人微信 PC 副驾六步）——纯逻辑门禁。

端到端（真 app + 假企微）见 ``test_wechat_e2e_parity.py::test_wechat_kf_connect_guide_backend``。
"""
from __future__ import annotations

import pytest

from src.integrations import wechat_kf_setup as S
from src.integrations.wechat_pc.env_check import parse_version, version_ok
from src.web.routes.wechat_pc_setup_routes import (
    DEFAULT_WORK_HOURS, bridge_enabled, bridge_patch_for_tier, policy_view, validate_policy_change,
)


def test_is_supervisor_treats_admin_bearer_flag_as_supervisor():
    """桌面壳托盘走 Bearer 管理员令牌、没有 session：_api_auth 打 auth_via_admin_token 后应算主管。"""
    from types import SimpleNamespace
    from src.web.routes.unified_inbox_auth import _is_supervisor

    class _Sess(dict):
        pass

    class _Req:
        def __init__(self, flag=False, role=""):
            self.state = SimpleNamespace(auth_via_admin_token=flag)
            self.scope = {"session": True}
            self.session = _Sess(role=role, username="x")

    assert _is_supervisor(_Req(flag=True, role="")) is True
    assert _is_supervisor(_Req(flag=False, role="master")) is True
    assert _is_supervisor(_Req(flag=False, role="agent")) is False
    assert _is_supervisor(_Req(flag=False, role="")) is False


def test_describe_kf_errcode_human_readable():
    ip = S.describe_kf_errcode(60020, egress_ip="1.2.3.4")
    assert ip["kind"] == "ip_not_allowed" and "1.2.3.4" in ip["fix"] and "可信 IP" in ip["fix"]
    assert S.describe_kf_errcode(40013)["kind"] == "invalid_corpid"
    assert S.describe_kf_errcode("40014")["kind"] == "invalid_secret"
    assert S.describe_kf_errcode(95014)["kind"] == "servicer_not_active"
    assert S.describe_kf_errcode(0)["kind"] == "ok"
    assert S.describe_kf_errcode(-1)["kind"] == "network"
    unk = S.describe_kf_errcode(123456)
    assert unk["kind"] == "api_error" and "123456" in unk["problem"]
    assert S.describe_kf_errcode("x")["kind"] == "network"


def test_parse_ip_text_accepts_public_ipv4_only():
    assert S.parse_ip_text("8.8.4.4\n") == "8.8.4.4"
    assert S.parse_ip_text('{"ip": "1.1.1.1"}', "json:ip") == "1.1.1.1"
    assert S.parse_ip_text("192.168.0.149") == "", "私网地址不是出站公网 IP"
    assert S.parse_ip_text("127.0.0.1") == "" and S.parse_ip_text("garbage") == "" and S.parse_ip_text("", "json:ip") == ""
    assert S.parse_ip_text("not json", "json:ip") == ""


def test_fetch_egress_ip_rotates_providers_and_caches():
    S.reset_egress_cache()
    calls = []

    def http_get(url, timeout):
        calls.append(url)
        if "ipify" in url:
            raise RuntimeError("timeout")
        if "ifconfig" in url:
            return "10.0.0.1"          # 私网 → 不认
        return "8.8.8.8"
    r = S.fetch_egress_ip(http_get, now=1000.0)
    assert r["ok"] and r["ip"] == "8.8.8.8" and "ip.sb" in r["source"] and len(calls) == 3
    r2 = S.fetch_egress_ip(lambda u, t: "0.0.0.0", now=1030.0)
    assert r2["cached"] is True and r2["ip"] == "8.8.8.8", "60 秒内走缓存，不再打网络"
    S.reset_egress_cache()
    r3 = S.fetch_egress_ip(lambda u, t: "", now=2000.0)
    assert not r3["ok"] and r3["ip"] == "" and r3["errors"]


def test_assemble_checks_shapes():
    ok_tok = {"ok": True, "errcode": 0}
    lst = {"ok": True, "data": {"account_list": [
        {"open_kfid": "wkA", "name": "客服A", "manage_privilege": True},
        {"open_kfid": "wkB", "name": "客服B", "manage_privilege": False}]}}
    out = S.assemble_checks(ok_tok, lst, egress_ip="1.1.1.1")
    assert out["ok"] and [c["id"] for c in out["checks"]] == ["credentials", "kf_permission", "manageable_accounts"]
    assert out["checks"][2]["count"] == 1 and [a["open_kfid"] for a in out["accounts"]] == ["wkA", "wkB"]
    # 凭证错：只有一项且带人话
    bad = S.assemble_checks({"ok": False, "errcode": 40013}, None)
    assert not bad["ok"] and len(bad["checks"]) == 1 and bad["checks"][0]["problem"] == "CorpID 不正确"
    # 令牌对但列表被 60020 拦：第二项失败，怎么办里带 IP
    ipfail = S.assemble_checks(ok_tok, {"ok": False, "errcode": 60020}, egress_ip="9.9.9.9")
    assert not ipfail["ok"] and ipfail["checks"][1]["ok"] is False and "9.9.9.9" in ipfail["checks"][1]["fix"]
    # 有账号但都不可管理 / 完全没账号：第三项失败但文案不同
    nomanage = S.assemble_checks(ok_tok, {"ok": True, "data": {"account_list": [{"open_kfid": "wkB", "manage_privilege": False}]}})
    assert nomanage["checks"][2]["ok"] is False and "勾选" in nomanage["checks"][2]["fix"]
    empty = S.assemble_checks(ok_tok, {"ok": True, "data": {"account_list": []}})
    assert empty["checks"][2]["ok"] is False and "新建" in empty["checks"][2]["fix"]


def test_normalize_kf_accounts_marks_bound():
    rows = S.normalize_kf_accounts([{"open_kfid": "wkA", "name": " 客服A ", "avatar": "https://a"}, {"open_kfid": ""}, "junk"],
                                   bound_ids={"wkA"})
    assert rows == [{"open_kfid": "wkA", "name": "客服A", "avatar": "https://a", "manage_privilege": False, "bound": True}]


def test_pc_version_parse_and_gate():
    assert parse_version("4.1.12.55") == (4, 1, 12, 55) and version_ok("4.1.12.55")
    assert not version_ok("3.9.12.51") and not version_ok("") and parse_version("abc") == ()
    assert version_ok("4.0")


def test_pc_voice_version_gate_is_4_1_9():
    from src.integrations.wechat_pc.env_check import voice_version_ok
    assert voice_version_ok("4.1.9") and voice_version_ok("4.1.12.55") and voice_version_ok("4.2.0")
    assert not voice_version_ok("4.1.8.20") and not voice_version_ok("4.0.6") and not voice_version_ok("3.9.12")
    assert not voice_version_ok("") and voice_version_ok("5.0")


def test_pc_manual_commands_and_paths_are_tier_free():
    """手动命令只留给局域网部署 / 高级区：不带 -Tier（配置文件权威），令牌只给路径。"""
    from src.web.routes.wechat_pc_setup_routes import bridge_paths_for, copilot_presence, manual_commands

    paths = bridge_paths_for("D:/inst/data/config/config.yaml", "D:/engine")
    assert paths["state_dir"].replace("\\", "/").endswith("inst/data/config/wechat_pc")
    assert paths["token_file"].replace("\\", "/").endswith("wechat_pc/TOKEN.txt")
    assert paths["config_file"].replace("\\", "/").endswith("config/config.local.yaml") and paths["engine_root"] == "D:/engine"
    c = manual_commands(engine_root="D:/engine", account_id="wx-1", backend_url="http://127.0.0.1:18799",
                        token_file=paths["token_file"], config_file=paths["config_file"], state_dir=paths["state_dir"])
    assert "wechat_pc_devlink.ps1" in c["command"] and "wechat_pc_autostart.ps1 -Install" in c["autostart_command"]
    for k in ("command", "autostart_command"):
        assert "-Tier" not in c[k] and "-AccountId wx-1" in c[k] and paths["token_file"] in c[k]

    class _Reg:
        def __init__(self, rows):
            self.rows = rows

        def list(self, platform=None):
            return self.rows

    import time as _t
    hb = {"bridge_heartbeat": {"ts": _t.time(), "tier": "semi", "stats": {"last_readable": True}}}
    reg = _Reg([{"account_id": "other", "label": "B", "meta": hb},
                {"account_id": "wx-1", "label": "A", "meta": {"bridge_heartbeat": {"ts": _t.time() - 5, "tier": "copilot"}}},
                {"account_id": "no-hb", "meta": {}}])
    assert copilot_presence(reg, "wx-1")["account_id"] == "wx-1", "优先匹配本 supervisor 的账号"
    assert copilot_presence(reg, "nope")["account_id"] == "other", "没匹配到就取第一个有心跳的"
    assert copilot_presence(_Reg([{"account_id": "x", "meta": {}}]), "x") is None
    assert copilot_presence(None, "x") is None


def test_pc_supervisor_binding_kwargs_from_config_block():
    """双开微信：``platform_login.wechat_pc.{window_hwnd,window_pid,expect_wxid}`` 直达驱动命令行；缺省/非法不绑。"""
    from src.integrations.wechat_pc.supervisor import build_driver_command
    from src.web.routes.wechat_pc_setup_routes import supervisor_binding_kwargs

    assert supervisor_binding_kwargs({}) == {"window_hwnd": 0, "window_pid": 0, "expect_wxid": ""}
    assert supervisor_binding_kwargs({"window_pid": "abc", "window_hwnd": -3, "expect_wxid": None}) == {
        "window_hwnd": 0, "window_pid": 0, "expect_wxid": ""}
    kw = supervisor_binding_kwargs({"window_pid": "502", "expect_wxid": " xb_2020 "})
    assert kw == {"window_hwnd": 0, "window_pid": 502, "expect_wxid": "xb_2020"}
    cmd = build_driver_command(python_exe="p", backend_url="u", account_id="a", label="l", config_file="",
                               state_dir="s", frozen=False, **kw)
    assert cmd[cmd.index("--pid") + 1] == "502" and cmd[cmd.index("--expect-wxid") + 1] == "xb_2020"
    assert "--hwnd" not in cmd


def test_pc_registry_value_to_exe_path():
    from src.integrations.wechat_pc.env_check import resolve_exe_from_registry_value as R
    assert R(r'"C:\Program Files\Tencent\Weixin\Weixin.exe"', "") == r"C:\Program Files\Tencent\Weixin\Weixin.exe"
    assert R(r'"C:\P\Weixin.exe",0', "") == r"C:\P\Weixin.exe"
    assert R(r"C:\Program Files\Tencent\Weixin\ ", "Weixin.exe").replace("\\", "/") == "C:/Program Files/Tencent/Weixin/Weixin.exe"
    assert R("", "Weixin.exe") == "" and R(None, "") == ""


def test_pc_classify_window_matches_real_4x_top_level_titles():
    """4.1.13 真机：顶层是 Qt51514QWindowIcon，主窗题「WeChat」/「微信」、登录窗题「Weixin」；mmui::* 不是顶层类名。
    置前与环境检测共用这一分类，否则托盘态永远找不到窗口（2026-09-19 联调发现）。"""
    from src.integrations.wechat_pc.env_check import classify_window as C
    assert C("Qt51514QWindowIcon", "WeChat") == "main" and C("Qt51514QWindowIcon", "微信") == "main"
    assert C("Qt51514QWindowIcon", "Weixin") == "login" and C("Qt51514QWindowIcon", "登录") == "login"
    assert C("mmui::MainWindow", "") == "main" and C("mmui::LoginWindow", "") == "login"
    assert C("Qt51514WxTrayIconMessageWindowClass", "WxTrayIconMessageWindow") == ""
    assert C("NativeHWNDHost", "Mode Indicator") == "" and C("", None) == ""
    # 二次联调实锤：英文界面登录窗标题也是「WeChat」（296×388）——同名靠尺寸分；最小化态尺寸 0 按标题给主窗
    assert C("Qt51514QWindowIcon", "WeChat", 296, 388) == "login"
    assert C("Qt51514QWindowIcon", "WeChat", 1280, 860) == "main" and C("Qt51514QWindowIcon", "微信", 0, 0) == "main"
    assert C("Qt51514QWindowIcon", "Weixin", 1280, 860) == "login", "登录标题不看尺寸"


def test_pc_window_kind_prefers_uia_class_over_rough_guess(monkeypatch):
    """window_kind：粗筛说 main、UIA 类名说 login → 以 UIA 为准；粗筛出局的窗不去问 UIA；UIA 缺席用粗筛。"""
    from src.integrations.wechat_pc import env_check as E
    from src.integrations.wechat_pc.win32_windows import TopWindow
    E._UIA_KIND_CACHE.clear()
    asked = []

    def fake_uia(hwnd, pid=0):
        asked.append(hwnd)
        return "login" if hwnd == 1 else ""
    monkeypatch.setattr(E, "_uia_kind", fake_uia)
    assert E.window_kind(TopWindow(1, 9, "Qt51514QWindowIcon", "WeChat", True, "", 1280, 860)) == "login"
    assert E.window_kind(TopWindow(2, 9, "Qt51514QWindowIcon", "WeChat", True, "", 1280, 860)) == "main"
    assert E.window_kind(TopWindow(3, 9, "IME", "Default IME", False)) == "" and 3 not in asked


def test_inbox_pc_card_has_in_place_start_and_focus_actions():
    """2026-09-19：收件箱账号卡「副驾离线 / 看不到窗口」直接给启动 / 还原按钮（主管专属），复用引导页端点，
    且所有新词条 zh/en 齐全；旧提示不再让人去跑 ps1。"""
    from pathlib import Path
    from src.web.i18n_packs import inbox_workspace as P
    html = (Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    assert "async function startPcCopilot(" in html and "/api/setup/wechat_pc/copilot/start" in html
    assert "async function focusPcWechat(" in html and "/api/setup/wechat_pc/launch-wechat" in html
    assert "bp.cls==='off'||bp.cls==='blind'" in html and "startPcCopilot" in html and "focusPcWechat" in html, "按状态换主 CTA"
    assert "{{ 'true' if is_supervisor else 'false' }}" in html, "主管门槛来自模板 ctx，不靠前端猜"
    for k in ("inbox.acct.pc_start", "inbox.acct.pc_starting", "inbox.acct.pc_start_ok", "inbox.acct.pc_start_fail",
              "inbox.acct.pc_start_no_driver", "inbox.acct.pc_start_no_token", "inbox.acct.pc_focus",
              "inbox.acct.pc_focus_ok", "inbox.acct.pc_focus_fail"):
        assert k in P.ZH and k in P.EN, k
    assert "devlink" not in P.ZH["inbox.acct.bridge_off_t"] and "devlink" not in P.EN["inbox.acct.bridge_off_t"]


def test_pc_guide_step3_test_message_has_explicit_received_state():
    """第 ③ 步「发一条测试消息」不再只滚一条旧消息：基线 last_ts 之后的对方新消息 → 收到了✓ + 漏斗事件 + rail 副文。"""
    from pathlib import Path
    from src.web.i18n_packs import connect_guide as P
    html = (Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "connect_guide.html").read_text(encoding="utf-8")
    assert "async function pollRecentPc(" in html and "state.recentBaseline" in html
    assert "track('test_msg_received')" in html
    assert "last_direction!=='out'" in html, "己方发出的消息不算收到"
    assert "'cg.rail.done_live_msg':'cg.rail.done_live'" in html
    for k in ("cg.pc.test_wait_online", "cg.pc.test_waiting", "cg.pc.test_got", "cg.pc.test_got_d", "cg.pc.test_go_ws", "cg.rail.done_live_msg"):
        assert k in P.ZH and k in P.EN, k


def test_pc_driver_policy_cfg_prefers_config_file_unless_tier_explicit(tmp_path, monkeypatch):
    """P0（2026-09-19）：驱动不再把默认 ``copilot`` 写死——不传 --tier 时以配置文件为准，引导页改档才能生效。"""
    from src.integrations.wechat_pc.__main__ import build_policy_cfg, load_pc_cfg, resolve_token
    from src.integrations.wechat_pc.policy import resolve_policy

    cfg = tmp_path / "config.local.yaml"
    cfg.write_text("platform_login:\n  wechat_pc:\n    tier: semi\n    work_hours: [10, 20]\n", encoding="utf-8")
    blk = load_pc_cfg(str(cfg))
    assert blk == {"tier": "semi", "work_hours": [10, 20]}
    assert load_pc_cfg(str(tmp_path / "missing.yaml")) == {} and load_pc_cfg("") == {}
    # 不传 tier：文件值胜出
    p = resolve_policy({"platform_login": {"wechat_pc": build_policy_cfg(blk)}})
    assert p.tier == "semi" and p.work_hours == (10, 20)
    # 显式 --tier 仍覆盖（旧脚本兼容）；--work-hours 坏格式忽略
    p2 = resolve_policy({"platform_login": {"wechat_pc": build_policy_cfg(blk, tier="copilot", work_hours="bad")}})
    assert p2.tier == "copilot" and p2.work_hours == (10, 20)
    # risk_ack 只加不减：文件已确认 + 命令行未给 → 仍为 True
    assert build_policy_cfg({"risk_ack": True}, risk_ack=False)["risk_ack"] is True
    assert build_policy_cfg({}, risk_ack=True, work_hours="0~24")["work_hours"] == [0, 24]
    # 令牌：显式 > 文件 > 环境变量 > admin
    tf = tmp_path / "TOKEN.txt"
    tf.write_text("  file-token \n", encoding="utf-8")
    monkeypatch.setenv("CHATX_ADMIN_TOKEN", "env-token")
    assert resolve_token("explicit", str(tf), "CHATX_ADMIN_TOKEN") == "explicit"
    assert resolve_token(None, str(tf), "CHATX_ADMIN_TOKEN") == "file-token"
    assert resolve_token(None, str(tmp_path / "nope.txt"), "CHATX_ADMIN_TOKEN") == "env-token"
    monkeypatch.delenv("CHATX_ADMIN_TOKEN")
    assert resolve_token(None, "", "CHATX_ADMIN_TOKEN") == "admin"


def test_pc_policy_view_and_validation():
    cur = policy_view({"platform_login": {"wechat_pc": {"tier": "semi", "work_hours": [0, 24]}}})
    assert cur == {"tier": "semi", "work_hours": [0, 24], "risk_ack": False, "risk_ack_by": "", "risk_ack_at": 0.0,
                   "autostart": False, "saved_at": 0.0, "bridge_enabled": False}
    from src.integrations.wechat_pc.policy import PcPolicy
    assert tuple(DEFAULT_WORK_HOURS) == PcPolicy().work_hours, "页面默认时段必须与驱动实际拒发时段同一个数"
    assert policy_view({})["tier"] == "copilot" and policy_view({})["work_hours"] == list(DEFAULT_WORK_HOURS)
    assert policy_view({"platform_login": {"wechat_pc": {"autostart": 1}}})["autostart"] is True
    assert policy_view({"platform_login": {"wechat_pc": {"saved_at": 1.5}}})["saved_at"] == 1.5, "页面据 saved_at>0 判第②步真完成"
    assert validate_policy_change({"tier": "semi"}, cur) == {"tier": "semi"}
    # 全自动必须知情同意（服务端强制，不信任前端）
    with pytest.raises(ValueError, match="risk_ack_required"):
        validate_policy_change({"tier": "auto_reply"}, cur)
    assert validate_policy_change({"tier": "auto_reply", "risk_ack": True}, cur) == {"tier": "auto_reply", "risk_ack": True}
    # 此前已确认过 → 不必重复勾选
    assert validate_policy_change({"tier": "auto_reply"}, {**cur, "risk_ack": True}) == {"tier": "auto_reply"}
    with pytest.raises(ValueError, match="bad_tier"):
        validate_policy_change({"tier": "root"}, cur)
    with pytest.raises(ValueError, match="bad_work_hours"):
        validate_policy_change({"tier": "semi", "work_hours": [22, 9]}, cur)
    assert validate_policy_change({"tier": "copilot", "work_hours": [8, 20]}, cur) == {"tier": "copilot", "work_hours": [8, 20]}


def test_pc_sending_tiers_open_desktop_bridge():
    """2026-09-19 实测：选了 semi/auto_reply 但 ``desktop_bridge`` 没开 → 手发 400「不支持的平台」、全自动只拟稿不发。
    保存档位必须顺手把桥打开；只开不关（Electron 内嵌账号也走这条桥）。"""
    off = {"inbox": {"l2_autosend": {"deliver": True}}}
    on = {"inbox": {"l2_autosend": {"deliver": True, "desktop_bridge": {"enabled": True, "review_mode": False}}}}
    assert bridge_enabled(off) is False and bridge_enabled({}) is False and bridge_enabled(on) is True
    assert bridge_patch_for_tier("copilot", off) is None, "只读档不需要出站通道"
    assert bridge_patch_for_tier("semi", off) == {"inbox": {"l2_autosend": {"desktop_bridge": {"enabled": True}}}}
    assert bridge_patch_for_tier("auto_reply", off) == {"inbox": {"l2_autosend": {"desktop_bridge": {"enabled": True}}}}
    assert bridge_patch_for_tier("auto_reply", on) is None, "已开不重复写"
    assert bridge_patch_for_tier("copilot", on) is None, "降档不关桥：别的桌面账号还在用"
    assert policy_view(on)["bridge_enabled"] is True and policy_view(off)["bridge_enabled"] is False

