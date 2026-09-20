# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 后端托管 supervisor 门禁（2026-09-19 P1）。全部离线：假 Popen / 假时钟 / 假心跳。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from src.integrations.wechat_pc import supervisor as S


# ── 纯函数 ──────────────────────────────────────────────────────────────────

def test_derive_state_matrix():
    now = 1000.0
    alive = {"alive": True, "readable": True, "tier": "semi"}
    blind = {"alive": True, "readable": False}
    stale = {"alive": False, "readable": True, "age_sec": 400}
    # 心跳在线：本进程托管 → online；别处起的 → attached
    assert S.derive_state(proc_alive=True, exit_code=None, started_at=990, presence=alive, now=now) == \
        {"state": "online", "reason": "", "attached": False}
    assert S.derive_state(proc_alive=False, exit_code=None, started_at=0, presence=alive, now=now)["attached"] is True
    assert S.derive_state(proc_alive=True, exit_code=None, started_at=now - 300, presence=blind, now=now)["state"] == "blind"
    # 首个心跳常带 readable=False（第一拍还没跑完）：宽限期内仍是 starting，不闪琥珀色
    assert S.derive_state(proc_alive=True, exit_code=None, started_at=990, presence=blind, now=now)["state"] == "starting"
    assert S.derive_state(proc_alive=False, exit_code=None, started_at=0, presence=blind, now=now)["state"] == "blind", \
        "别处起的进程读不到屏：没有宽限概念，直接 blind"
    # 进程在跑、还没心跳：宽限期内 starting，超时 error(no_heartbeat)
    assert S.derive_state(proc_alive=True, exit_code=None, started_at=now - 10, presence=None, now=now)["state"] == "starting"
    d = S.derive_state(proc_alive=True, exit_code=None, started_at=now - S.STARTING_GRACE_SEC - 1, presence=None, now=now)
    assert d == {"state": "error", "reason": "no_heartbeat", "attached": False}
    # 进程退出：0 → offline；非 0 → error
    assert S.derive_state(proc_alive=False, exit_code=0, started_at=0, presence=None, now=now)["state"] == "offline"
    assert S.derive_state(proc_alive=False, exit_code=2, started_at=0, presence=None, now=now) == \
        {"state": "error", "reason": "exit_2", "attached": False}
    # 没进程、心跳过期 → offline(heartbeat_stale)；什么都没有 → idle
    assert S.derive_state(proc_alive=False, exit_code=None, started_at=0, presence=stale, now=now)["reason"] == "heartbeat_stale"
    assert S.derive_state(proc_alive=False, exit_code=None, started_at=0, presence=None, now=now)["state"] == "idle"


def test_decide_autostart_and_backoff():
    base = dict(enabled=True, proc_alive=False, presence_alive=False, wechat_main_window=True, driver_ok=True,
                backoff_until=0.0, now=100.0)
    assert S.decide_autostart(**base) == S.ACT_START
    assert S.decide_autostart(**{**base, "enabled": False}) == S.ACT_SKIP
    assert S.decide_autostart(**{**base, "proc_alive": True}) == S.ACT_SKIP
    assert S.decide_autostart(**{**base, "presence_alive": True}) == S.ACT_SKIP, "别处已起的进程在线 → 不重复拉"
    assert S.decide_autostart(**{**base, "driver_ok": False}) == S.ACT_NO_DRIVER
    assert S.decide_autostart(**{**base, "backoff_until": 200.0}) == S.ACT_BACKOFF
    assert S.decide_autostart(**{**base, "wechat_main_window": False}) == S.ACT_WAIT_WECHAT, "微信没登录就等着，不空转拉进程"
    assert [S.next_backoff(i) for i in (0, 1, 2, 3, 4, 9)] == [0.0, 2.0, 10.0, 60.0, 300.0, 300.0]


def test_build_driver_command_has_no_tier_and_no_token():
    cmd = S.build_driver_command(python_exe="py.exe", backend_url="http://127.0.0.1:18799", account_id="wx-1",
                                 label="我的微信", config_file="C:/cfg/config.local.yaml", state_dir="C:/st")
    assert cmd[:4] == ["py.exe", "-u", "-m", "src.integrations.wechat_pc"]
    assert "--tier" not in cmd, "档位以配置文件为准并热生效，不再写死进命令行"
    assert "--token" not in cmd and "--token-file" not in cmd and S.TOKEN_ENV in cmd, "令牌走环境变量，不进命令行"
    assert "--config" in cmd and cmd[cmd.index("--config") + 1] == "C:/cfg/config.local.yaml"
    assert "--connected-days" not in cmd
    assert "--connected-days" in S.build_driver_command(python_exe="p", backend_url="u", account_id="a", label="l",
                                                        config_file="", state_dir="s", connected_days=30)


def test_build_driver_command_passes_window_binding_only_when_given():
    kw = dict(python_exe="p", backend_url="u", account_id="a", label="l", config_file="", state_dir="s", frozen=False)
    cmd = S.build_driver_command(**kw)
    assert "--hwnd" not in cmd and "--pid" not in cmd
    cmd = S.build_driver_command(window_pid=502, **kw)
    assert cmd[cmd.index("--pid") + 1] == "502" and "--hwnd" not in cmd
    cmd = S.build_driver_command(window_hwnd=1001, **kw)
    assert cmd[cmd.index("--hwnd") + 1] == "1001" and "--pid" not in cmd
    assert "--expect-wxid" not in S.build_driver_command(expect_wxid="  ", **kw)
    cmd = S.build_driver_command(expect_wxid="xb_2020", **kw)
    assert cmd[cmd.index("--expect-wxid") + 1] == "xb_2020"


def test_build_driver_command_frozen_uses_backend_exe_flag_not_dash_m():
    """打包态（PyInstaller backend.exe）没有 `python -m`：命令必须是 `backend.exe --wechat-pc-driver …`，
    且 main.py 首参分流到驱动入口（否则起出来的是第二个后端）。"""
    import re
    from pathlib import Path
    kw = dict(backend_url="http://127.0.0.1:18799", account_id="wx-1", label="我的微信",
              config_file="C:/cfg/config.local.yaml", state_dir="C:/st")
    frozen = S.build_driver_command(python_exe="C:/app/resources/backend/backend.exe", frozen=True, **kw)
    assert frozen[:2] == ["C:/app/resources/backend/backend.exe", S.FROZEN_DRIVER_FLAG]
    assert "-m" not in frozen and "-u" not in frozen and "src.integrations.wechat_pc" not in frozen
    src = S.build_driver_command(python_exe="py.exe", frozen=False, **kw)
    assert src[:4] == ["py.exe", "-u", "-m", "src.integrations.wechat_pc"]
    assert frozen[2:] == src[4:], "两种形态只差入口，驱动参数必须逐项相同"
    # main.py 分流：首参命中 → 交给 wechat_pc.__main__.main(argv[2:])，且在后端重 import 之前
    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    m = re.search(r'sys\.argv\[1\] == "--wechat-pc-driver"', main_src)
    assert m, "main.py 缺 --wechat-pc-driver 首参分流"
    assert main_src.index("sys.argv[1] == \"--wechat-pc-driver\"") < main_src.index("from src.ai.ai_client import AIClient")
    assert "from src.integrations.wechat_pc.__main__ import main as _wechat_pc_driver_main" in main_src
    # 打包脚本收 uiautomation 的 DLL（bin/*.dll 是数据文件，静态分析不带）
    bb = (Path(__file__).resolve().parents[1] / "desktop" / "build" / "build_backend.py").read_text(encoding="utf-8")
    m2 = re.search(r"^COLLECT_ALL\s*=\s*\[(.*?)\]", bb, re.M | re.S)
    assert m2 and "uiautomation" in m2.group(1), "build_backend.COLLECT_ALL 须含 uiautomation"


# ── 有状态：假 Popen ────────────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._rc: Optional[int] = None
        self.killed = False

    def poll(self):
        return self._rc

    def exit(self, rc: int) -> None:
        self._rc = rc

    def kill(self):
        self.killed = True
        self._rc = 1

    def wait(self, timeout=None):
        return self._rc


class _World:
    """假环境：时钟 / 心跳 / 微信窗口 / 拉起记录。"""

    def __init__(self, tmp_path) -> None:
        self.t = 1000.0
        self.presence: Optional[Dict[str, Any]] = None
        self.env: Dict[str, Any] = {"running": True, "main_window": True}
        self.ready: Dict[str, Any] = {"ok": True}
        self.spawned: List[Dict[str, Any]] = []
        self.procs: List[_FakeProc] = []
        self.tmp = tmp_path

    def popen(self, cmd, **kw):
        p = _FakeProc(4000 + len(self.procs))
        self.procs.append(p)
        self.spawned.append({"cmd": list(cmd), "env": dict(kw.get("env") or {}), "cwd": kw.get("cwd")})
        return p

    def sup(self, **over) -> S.WeChatPcSupervisor:
        kw = dict(engine_root=str(self.tmp), backend_url="http://127.0.0.1:1/", token_provider=lambda: "tok-1",
                  config_file=str(self.tmp / "config.local.yaml"), state_dir=str(self.tmp / "state"),
                  account_id="wx-1", label="L", presence_provider=lambda: self.presence, env_provider=lambda: self.env,
                  driver_ready_provider=lambda: self.ready, python_exe="py.exe", popen=self.popen, now=lambda: self.t)
        kw.update(over)
        return S.WeChatPcSupervisor(**kw)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    # 不真的 taskkill / Job Object
    monkeypatch.setattr(S.WeChatPcSupervisor, "_assign_job", lambda self, pid: None)
    monkeypatch.setattr(S.WeChatPcSupervisor, "_kill_tree", lambda self, proc: proc.kill())
    return _World(tmp_path)


def test_start_spawns_with_token_in_env_and_reports_starting_then_online(world):
    sup = world.sup()
    assert sup.status()["state"] == "idle"
    r = sup.start()
    assert r["ok"] and r["action"] == "start" and r["state"] == "starting" and r["pid"] == 4000
    sp = world.spawned[0]
    assert sp["env"][S.TOKEN_ENV] == "tok-1" and "tok-1" not in " ".join(sp["cmd"]), "令牌只在环境变量里"
    assert sp["cwd"] == str(world.tmp) and "--tier" not in sp["cmd"]
    assert (world.tmp / "state").is_dir(), "状态目录自动建"
    # 幂等
    assert sup.start()["action"] == "already_running" and len(world.spawned) == 1
    # 心跳来了 → online；微信收托盘 → blind
    world.presence = {"alive": True, "readable": True, "tier": "semi", "age_sec": 3}
    st = sup.status()
    assert st["state"] == "online" and st["managed"] is True and st["tier"] == "semi" and st["attached"] is False
    world.presence = {"alive": True, "readable": False, "tier": "semi"}
    assert sup.status()["state"] == "starting", "宽限期内首拍 readable=False 仍算 starting"
    world.t += S.STARTING_GRACE_SEC + 1
    assert sup.status()["state"] == "blind", "宽限期过了还读不到屏 → blind"


def test_attach_when_external_heartbeat_alive(world):
    world.presence = {"alive": True, "readable": True, "tier": "copilot"}
    sup = world.sup()
    r = sup.start()
    assert r["ok"] and r["action"] == "attach" and not world.spawned, "别处起的进程在线 → 不重复拉"
    assert sup.status()["attached"] is True and sup.status()["managed"] is False
    # 主人坚持重启：force 才拉
    assert sup.start(force=True)["action"] == "start" and len(world.spawned) == 1
    assert sup.stop()["action"] == "stop"


def test_quick_exit_counts_failure_and_backs_off(world):
    sup = world.sup()
    sup.start()
    world.procs[0].exit(2)
    world.t += 5
    st = sup.status()
    assert st["state"] == "error" and st["reason"] == "exit_2" and st["failures"] == 1 and st["backoff_sec"] == 2
    assert st["last_error"] == "exit_2"
    # 退避期内自启不拉；过了退避才拉；连续失败退避加长
    sup.set_autostart(True)
    assert sup.autostart_tick() == S.ACT_BACKOFF
    world.t += 3
    assert sup.autostart_tick() == S.ACT_START and len(world.spawned) == 2
    world.procs[1].exit(1)
    world.t += 1
    assert sup.status()["backoff_sec"] == 10
    # 正常运行很久后退出码 0 → offline，不计失败
    world.t += 20
    sup.autostart_tick()
    assert len(world.spawned) == 3
    world.t += S.QUICK_EXIT_SEC + 5
    world.procs[2].exit(0)
    st = sup.status()
    assert st["state"] == "offline" and st["failures"] == 2, "跑够久后正常退出不算起不来"


def test_stop_is_not_an_error_and_restart_forces(world):
    sup = world.sup()
    sup.start()
    r = sup.stop()
    assert r["ok"] and r["action"] == "stop" and world.procs[0].killed
    st = sup.status()
    assert st["state"] == "offline" and st["reason"] == "exit_0" and st["failures"] == 0 and st["managed"] is False
    assert sup.stop()["action"] == "not_managed"
    # restart：先停再强制起
    sup.start()
    assert sup.restart()["action"] == "start" and len(world.spawned) == 3 and world.procs[1].killed


def test_stop_hides_stale_heartbeat_until_a_newer_one_appears(world):
    """真机复现（2026-09-19）：stop 之后注册表心跳还要 ~1 分钟才过期，页面会闪「在线 · 由计划任务启动」，
    而且此时点「启动」会被当成 attach 而不真拉——必须把被停进程留下的最后一拍视为过期。"""
    sup = world.sup()
    sup.start()
    world.presence = {"alive": True, "readable": True, "tier": "copilot", "ts": 1000.0, "age_sec": 2}
    assert sup.status()["state"] == "online"
    sup.stop()
    world.t += 5
    st = sup.status()
    assert st["state"] == "offline" and st["attached"] is False and st["heartbeat"]["stale_after_stop"] is True
    # 停止后再点启动：必须真拉进程，而不是 attach 到一个已死进程的残留心跳
    assert sup.start()["action"] == "start" and len(world.spawned) == 2
    sup.stop()
    # 之后出现 ts 更新的心跳 → 是别处新起的进程 → 正常 attach
    world.presence = {"alive": True, "readable": True, "tier": "copilot", "ts": 1010.0, "age_sec": 1}
    st2 = sup.status()
    assert st2["state"] == "online" and st2["attached"] is True
    assert sup.start()["action"] == "attach" and len(world.spawned) == 2


def test_autostart_waits_for_wechat_main_window(world):
    sup = world.sup(autostart=True)
    world.env = {"running": True, "main_window": False, "login_window": True}
    assert sup.autostart_tick() == S.ACT_WAIT_WECHAT and not world.spawned, "登录窗还开着 → 等主人扫码"
    world.env = {"running": True, "main_window": True}
    assert sup.autostart_tick() == S.ACT_START and len(world.spawned) == 1
    assert sup.autostart_tick() == S.ACT_SKIP, "已在跑 → 不重复"
    world.ready = {"ok": False, "uiautomation": False}
    sup2 = world.sup(autostart=True)
    assert sup2.autostart_tick() == S.ACT_NO_DRIVER and len(world.spawned) == 1


def test_start_refuses_without_token_or_driver(world):
    sup = world.sup(token_provider=lambda: "")
    r = sup.start()
    assert not r["ok"] and r["reason"] == "admin_token_missing" and not world.spawned
    world.ready = {"ok": False, "uiautomation": False}
    r2 = world.sup().start()
    assert not r2["ok"] and r2["reason"] == "driver_not_ready" and r2["driver"]["uiautomation"] is False


def test_spawn_failure_is_reported_not_raised(world):
    def boom(cmd, **kw):
        raise OSError("no python")
    sup = world.sup(popen=boom)
    r = sup.start()
    assert not r["ok"] and r["reason"].startswith("spawn_failed") and r["backoff_sec"] == 2
    assert sup.status()["state"] == "idle"


def test_log_tail_reads_last_lines(world):
    sup = world.sup()
    (world.tmp / "state").mkdir()
    (world.tmp / "state" / "copilot.log").write_text("a\n\nb\nc\n", encoding="utf-8")
    assert sup.log_tail(2) == ["b", "c"] and sup.log_tail(10) == ["a", "b", "c"]
    assert world.sup(state_dir=str(world.tmp / "nope")).log_tail() == []


# ── 多账号：配置归一化 + 池 ─────────────────────────────────────────────────

def test_normalize_accounts_primary_first_and_dedup():
    blk = {"account_id": "wx-a", "label": "A", "window_pid": 111, "expect_wxid": "wxid_a",
           "accounts": [{"account_id": "wx-b", "window_hwnd": 222, "autostart": True},
                        {"account_id": "wx-a", "window_pid": 333},  # 同主账号 id → 合并进主账号，不重复
                        {"account_id": ""}, "junk", {"account_id": "wx-b"}]}
    rows = S.normalize_accounts(blk, default_account_id="wechat-pc", default_label="副驾")
    assert [r["account_id"] for r in rows] == ["wx-a", "wx-b"]
    assert rows[0]["window_pid"] == 333 and rows[0]["expect_wxid"] == "wxid_a" and rows[0]["label"] == "A"
    assert rows[1]["window_hwnd"] == 222 and rows[1]["autostart"] is True and rows[1]["label"] == "副驾 2"
    # 老单账号配置：块顶层零改动 → 恰好一行、默认 id
    one = S.normalize_accounts({"tier": "semi"}, default_account_id="wechat-pc", default_label="副驾")
    assert one == [{"account_id": "wechat-pc", "label": "副驾", "window_hwnd": 0, "window_pid": 0,
                    "expect_wxid": "", "autostart": False}]
    assert S.normalize_accounts({"window_hwnd": "abc", "window_pid": -3}, default_account_id="x", default_label="L")[0][
        "window_hwnd"] == 0


def test_account_log_name_primary_keeps_legacy_path():
    assert S.account_log_name("wechat-pc", "wechat-pc") == "copilot.log"
    assert S.account_log_name("", "wechat-pc") == "copilot.log"
    assert S.account_log_name("wx b/2", "wechat-pc") == "copilot.wx_b_2.log"


def test_pool_one_process_per_account_and_sync(world):
    made: List[str] = []

    def factory(row):
        made.append(row["account_id"])
        return world.sup(account_id=row["account_id"], label=row["label"], window_hwnd=row["window_hwnd"],
                         window_pid=row["window_pid"], expect_wxid=row["expect_wxid"],
                         log_name=S.account_log_name(row["account_id"], "wx-a"))

    pool = S.WeChatPcSupervisorPool(factory)
    rows = S.normalize_accounts({"account_id": "wx-a", "window_pid": 11,
                                 "accounts": [{"account_id": "wx-b", "window_pid": 22}]},
                                default_account_id="wechat-pc", default_label="L")
    r = pool.sync(rows)
    assert r["added"] == ["wx-a", "wx-b"] and pool.account_ids == ["wx-a", "wx-b"] and made == ["wx-a", "wx-b"]
    assert pool.primary is pool.get("") is pool.get("wx-a") and pool.get("nope") is None
    # 两个账号各拉一个子进程，命令行各绑各的 pid，日志各一份
    pool.get("wx-a").start(); pool.get("wx-b").start()
    cmds = [" ".join(s["cmd"]) for s in world.spawned]
    assert len(cmds) == 2 and "--pid 11" in cmds[0] and "--pid 22" in cmds[1]
    assert "--account-id wx-a" in cmds[0] and "--account-id wx-b" in cmds[1]
    assert str(pool.get("wx-a").log_path).endswith("copilot.log") and str(pool.get("wx-b").log_path).endswith("copilot.wx-b.log")
    sts = pool.status_all()
    assert [s["account_id"] for s in sts] == ["wx-a", "wx-b"] and all(s["managed"] for s in sts)
    # 改 B 的绑定 → updated（进程不自动重启，由调用方决定）；删 B → 停进程
    rows2 = S.normalize_accounts({"account_id": "wx-a", "window_pid": 11,
                                  "accounts": [{"account_id": "wx-b", "window_pid": 23}]},
                                 default_account_id="wechat-pc", default_label="L")
    assert pool.sync(rows2)["updated"] == ["wx-b"] and pool.get("wx-b").window_pid == 23
    r3 = pool.sync(rows2[:1])
    assert r3["removed"] == ["wx-b"] and pool.account_ids == ["wx-a"] and world.procs[1].killed
    assert len(made) == 2, "改绑定 / 删除都不重建 supervisor 对象"
